"""Native desktop lifecycle. No HTTP server, browser or simulator controls."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from pathlib import Path

from .desktop_settings import DesktopSettings, SettingsStore
from .live_app import AppState, run_reader
from .live_fuel import LiveFuelConfig
from .live_queries import live_query_intent
from .llm_client import DeepSeekClient, LLMError
from .llm_engineer import EngineerConfig, EngineerService
from .runtime_clock import monotonic_now
from .trial_audit import MAX_TRIAL_BYTES, TrialAudit


class DesktopController:
    """Exactly one SDK reader; reconfiguration and close run off the Tk thread.

    Initial bounded preference/DPAPI reads remain synchronous before the window
    starts; subsequent settings writes and receipt validation are background work.
    """

    def __init__(
        self,
        *,
        store: SettingsStore | None = None,
        environ: Mapping[str, str] | None = None,
        reader: Callable = run_reader,
        service_factory: Callable = EngineerService,
        clock: Callable[[], float] = monotonic_now,
        voice_runtime: bool = False,
    ) -> None:
        self._store = store or SettingsStore()
        self._clock = clock
        self._lock = threading.RLock()
        self._state = AppState(clock=clock)
        self._reader_function = reader
        self._factory = service_factory
        self._closed = threading.Event()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._job: threading.Thread | None = None
        self._close_thread: threading.Thread | None = None
        self._configuring = False
        self._closing = False
        self._started = False
        self._session_path: Path | None = None
        self._last_question_at = -float("inf")
        self._capture_bytes = 0
        self._trial = None
        self._trial_bytes = 0
        self._trial_failed = False
        self._last_trial = {"status": "DISABLED", "reason": "NOT_STARTED", "bytes": 0}
        self._trial_report = None
        self._service_epoch = 0
        self._notice = "原生桌面窗口；不会启动游戏或发送车辆、进站控制指令。"
        self._lifecycle = "STOPPED"
        env = os.environ if environ is None else environ
        saved_key = None
        try:
            self._settings = self._store.load()
        except Exception:
            # Unknown recording consent must never become an enabled default.
            self._settings = DesktopSettings(recording_enabled=False)
            self._notice = "本机设置不可用；已关闭原始录制并使用本地解读，请检查设置。"
        else:
            if self._settings.remember_key:
                try:
                    saved_key = self._store.load_key()
                except Exception:
                    # A DPAPI problem is not permission to discard valid privacy preferences.
                    self._notice = (
                        "加密密钥不可用；已保留录制等本机设置，云端可能需要重新输入密钥。"
                    )
        self._api_key = saved_key or env.get("DEEPSEEK_API_KEY", "")
        self._service = self._new_service(self._settings, self._api_key, None, 0)
        self._voice = None
        if voice_runtime:
            from .voice_service import VoiceService
            self._voice = VoiceService(self._core_snapshot, self.submit, self._store, clock=clock,
                                       spotter_source=self._spotter_snapshot,
                                       audit_sink=self._state.audit_audio)

    def _new_service(self, settings: DesktopSettings, key: str, path: Path | None, used: int):
        return self._factory(
            self._state.snapshot,
            EngineerConfig(provider=settings.provider, model=settings.model, session_artifact=path),
            environ={"DEEPSEEK_API_KEY": key},
            clock=self._clock,
            initial_requests_used=used,
        )

    def _start_reader(self) -> str | None:
        self._stop = threading.Event()
        # Runtime recording is private, never relative to the executable/checkout.
        directory, warning = None, None
        remaining = max(0, 4 * 1024**3 - self._capture_bytes)
        if remaining < 32 * 1024**2:
            self._state.recording("LIMIT_REACHED", 0)
            warning = "本次原始录制已达容量上限；只读遥测监视继续运行。"
        elif not self._settings.recording_enabled:
            self._state.recording("DISABLED", 0)
        else:
            try:
                # Constructor validation performs no writes. Check the sibling
                # capture directory itself, including frozen builds and junctions.
                directory = SettingsStore(self._store.root.parent / "captures").root
            except Exception:
                self._state.recording("ERROR", 0)
                warning = "录制目录未通过隐私检查；已停用录制，只读遥测监视继续运行。"
        if warning:
            self._notice = warning
        if directory is not None and not self._trial_failed:
            budget = max(0, MAX_TRIAL_BYTES - self._trial_bytes)
            if budget < 1024:
                self._last_trial = {"status": "LIMIT_REACHED", "reason": "SESSION_BUDGET"}
            else:
                try:
                    self._trial = TrialAudit(self._store.root.parent / "trials",
                                             max_bytes=budget, clock=self._clock)
                    self._state.attach_trial(self._trial)
                except Exception:
                    self._last_trial = {"status": "ERROR", "reason": "STARTUP_FAILED"}
        self._reader = threading.Thread(
            target=self._read,
            args=(self._stop, directory, remaining),
            name="desktop-sdk-reader",
            daemon=True,
        )
        self._reader.start()
        return warning

    def _finish_trial(self):
        # Called only on the background lifecycle owner, after SDK owners exit.
        with self._lock:
            journal = self._trial
            self._state.attach_trial(None)
        if journal is not None:
            journal.close()
            with self._lock:
                self._last_trial = journal.snapshot()
                self._trial_bytes += self._last_trial["bytes"]
                # A short failed write may contain uncommitted bytes. Do not
                # repeatedly allocate new clips against the committed-only
                # budget after a fault; restart is required for this lane.
                self._trial_failed = self._last_trial["failed"]
                self._trial = None

    def _read(self, stop: threading.Event, directory: Path | None, budget: int) -> None:
        try:
            self._reader_function(
                self._state,
                stop,
                LiveFuelConfig(),
                record_directory=directory,
                record_max_bytes=budget,
            )
        except Exception:
            self._state.connection("ERROR")
            with self._lock:
                if not self._closing:
                    self._notice = "遥测读取已停止；请关闭后重新打开应用，原始错误未公开。"

    def start(self) -> None:
        with self._lock:
            if self._started or self._closing:
                return
            self._started = True
            self._lifecycle = "STARTING"
            self._start_reader()
            self._lifecycle = "RUNNING"
        if self._voice is not None:
            self._voice.trace_state()
            self._voice.start()

    def _core_snapshot(self) -> dict:
        with self._lock:
            telemetry = self._state.snapshot()
            # These copies belong to the native consumer, not the legacy browser
            # practice-only speech path. Do not weaken the browser speech policy.
            telemetry["limitations"] = [
                "实验燃油估计；不是完整进站策略，不考虑交通、轮胎、处罚或赛事规则。",
                "原生语音需主动启用；本人实时证据失效时撤回播报。",
                "本机只读：不启动游戏、不驾驶、不修改进站设置。",
            ]
            engineer = self._service.snapshot()
            engineer.pop("csrf_token", None)  # There is no HTTP question endpoint in this process.
            engineer["instance_id"] = str(self._service_epoch)
            retry = max(0.0, 10 - (self._clock() - self._last_question_at))
            if retry and engineer["status"] not in ("BUSY", "RATE_LIMITED"):
                engineer.update(status="RATE_LIMITED", retry_after_s=round(retry, 1))
            if self._configuring:
                engineer["status"] = "BUSY"
                engineer["local_live_available"] = False
            recording = telemetry.get("recording") or {}
            telemetry["trial_audit"] = {
                **(self._trial.snapshot() if self._trial else self._last_trial),
                "enabled": self._trial is not None,
                "session_bytes": self._trial_bytes,
                "heard": False, "live_acceptance": False,
            }
            amount = recording.get("bytes", 0)
            if type(amount) is int and amount >= 0:
                recording["bytes"] = min(4 * 1024**3, self._capture_bytes + amount)
            return {
                "telemetry": telemetry,
                "engineer": engineer,
                "lifecycle": self._lifecycle,
                "notice": self._notice,
                "trial_report": self._trial_report,
                "settings": {
                    **asdict(self._settings),
                    "key_configured": bool(self._api_key),
                },
            }

    def snapshot(self) -> dict:
        value = self._core_snapshot()
        if self._voice is not None:
            value["voice"] = self._voice.snapshot()
        return value

    def _spotter_snapshot(self) -> dict:
        # Never enter EngineerService or copy its answers on the urgent path.
        with self._lock:
            lifecycle = self._lifecycle
        return {**self._state.spotter_snapshot(), "lifecycle": lifecycle}

    def _voice_service(self):
        if self._voice is None or self._closing:
            raise ValueError("VOICE_UNAVAILABLE")
        return self._voice

    def voice_configure(self, settings: dict):
        self._voice_service().configure(settings)

    def voice_refresh_devices(self):
        self._voice_service().refresh_devices()

    def voice_bind(self):
        self._voice_service().bind()

    def voice_cancel_bind(self):
        self._voice_service().cancel_bind()

    def voice_press(self):
        self._voice_service().press()

    def voice_release(self):
        self._voice_service().release()

    def voice_test(self):
        self._voice_service().test()

    def voice_stop(self):
        self._voice_service().stop()

    def submit(self, question: object, scope: object = "live") -> tuple[int, dict]:
        with self._lock:
            if self._closing or self._configuring:
                return 409, {"error": "BUSY"}
            local = scope == "live" and live_query_intent(question) is not None
            if not local and self._clock() - self._last_question_at < 10:
                return 429, {"error": "RATE_LIMITED"}
            result = self._service.submit(question, scope)
            if result[0] == 202 and not local:
                self._last_question_at = self._clock()
            return result

    def _schedule(self, action: Callable[[], None]) -> None:
        with self._lock:
            if self._closing or self._configuring or self._service.snapshot()["status"] == "BUSY":
                raise ValueError("DESKTOP_BUSY")
            self._configuring = True
            self._notice = "正在后台应用设置；遥测读取与窗口保持运行。"

            def run():
                try:
                    action()
                except Exception:
                    with self._lock:
                        if not self._closing:
                            self._notice = "设置未能应用；请检查输入或本机文件，未公开原始错误。"
                finally:
                    with self._lock:
                        self._configuring = False

            self._job = threading.Thread(target=run, name="desktop-settings", daemon=True)
            self._job.start()

    def _replace_service(
        self, settings: DesktopSettings, key: str, path: Path | None, *, persist: bool
    ) -> None:
        used = self._service.snapshot()["requests_used"]
        replacement = self._new_service(settings, key, path, used)
        try:
            if self._closing:
                return
            if persist:
                self._store.save(settings, api_key=key or None)
            with self._lock:
                if self._closing:
                    return
                previous = self._service
                self._service = replacement
                self._service_epoch += 1
                replacement = None
                self._settings = settings
                self._api_key = key
                self._session_path = path
                self._notice = (
                    "设置已应用；密钥不显示、不记录，也不会放进可执行文件。"
                    if persist
                    else ("历史报告已验证并载入，仅用于历史复盘。" if path else "历史报告已卸载。")
                )
            previous.close()
        finally:
            if replacement is not None:
                replacement.close()

    def configure(self, provider: str, model: str, api_key: str | None, remember_key: bool) -> None:
        EngineerConfig(provider=provider, model=model)
        if type(remember_key) is not bool or (api_key is not None and type(api_key) is not str):
            raise ValueError("INVALID_SETTINGS")
        key = self._api_key if api_key is None else api_key.strip()
        if key:
            try:
                DeepSeekClient(key, model=model)  # Constructor validation only; never a request.
            except LLMError:
                raise ValueError("INVALID_SETTINGS") from None
        settings = replace(
            self._settings,
            provider=provider,
            model=model,
            remember_key=remember_key,
        )
        self._schedule(
            lambda: self._replace_service(
                settings,
                key,
                self._session_path,
                persist=True,
            )
        )

    def load_session(self, path: Path | None) -> None:
        if path is not None and not isinstance(path, Path):
            raise ValueError("INVALID_SESSION_PATH")
        self._schedule(
            lambda: self._replace_service(
                self._settings,
                self._api_key,
                path,
                persist=False,
            )
        )

    @property
    def trial_directory(self) -> Path:
        return self._store.root.parent / "trials"

    def replay_trial(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ValueError("INVALID_TRIAL_PATH")

        def replay():
            from .trial_replay import TrialReplayError, replay_trial
            try:
                report = replay_trial(path, cancelled=lambda: self._closing)
            except TrialReplayError as error:
                report = {"status": "REJECTED", "reason": error.code,
                          "heard": False, "live_acceptance": False}
            with self._lock:
                self._trial_report = report
                self._notice = "本地诊断回放结束；未播放音频，不代表真实驾驶验收。"

        self._schedule(replay)

    def set_recording(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("INVALID_RECORDING_SETTING")
        if enabled == self._settings.recording_enabled:
            return

        def change():
            self._stop.set()
            if self._reader:
                self._reader.join(timeout=8)
                if self._reader.is_alive():
                    with self._lock:
                        self._notice = (
                            "记录停止仍在等待本机写入；没有启动第二个读取器，请稍后关闭应用。"
                        )
                    return
            self._finish_trial()
            with self._lock:
                if self._closing:
                    return
                amount = self._state.snapshot().get("recording", {}).get("bytes", 0)
                if type(amount) is int and amount >= 0:
                    self._capture_bytes += amount
                self._state.recording("DISABLED", 0)
                settings = replace(self._settings, recording_enabled=enabled)
                self._settings = settings
                warning = self._start_reader()
                self._notice = warning or "记录设置已应用；读取器已重连，燃油学习重新开始。"
            if self._voice is not None:
                self._voice.trace_state()
            try:
                key = self._api_key
                self._store.save(settings, api_key=key or None)
            except Exception:
                with self._lock:
                    if not self._closing:
                        self._notice = (
                            "本次录制开关已生效，但本机保存失败；下次启动可能恢复旧设置。"
                            + (warning or "")
                        )

        self._schedule(change)

    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            self._lifecycle = "STOPPING"
            self._notice = "正在停止读取并完成本机记录，请稍候。"
            self._stop.set()

        def finish():
            if self._voice is not None:
                self._voice.close()
            self._service.close(wait=True)
            waiting_since = self._clock()
            while True:
                with self._lock:
                    workers = (self._reader, self._job)
                for worker in workers:
                    if worker is not None and worker.is_alive():
                        worker.join(timeout=0.1)
                if all(worker is None or not worker.is_alive() for worker in workers):
                    break
                if self._clock() - waiting_since >= 8:
                    with self._lock:
                        self._notice = (
                            "关闭仍在等待采集或本机保存完成；窗口保持等待，不会启动第二个读取器。"
                        )
            with self._lock:
                self._api_key = ""
                self._state.connection("STOPPED")
                self._notice = "正在完成本机诊断日志；窗口等待写入线程实际退出。"
            self._finish_trial()
            with self._lock:
                self._lifecycle = "STOPPED"
                self._closed.set()

        self._close_thread = threading.Thread(target=finish, name="desktop-close", daemon=True)
        self._close_thread.start()

    def is_closed(self) -> bool:
        return self._closed.is_set()


__all__ = ["DesktopController"]
