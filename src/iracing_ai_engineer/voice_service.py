"""Opt-in VR push-to-talk orchestration, independent of window focus.

Microphone audio exists only for a held PTT request, remains in memory, and is
recognized locally. Only the resulting text enters the existing advisor queue.
No audio, transcript, credentials or backend exceptions are logged here.
"""

from __future__ import annotations

import copy
import io
import math
import queue
import re
import struct
import threading
import wave
from collections.abc import Callable, Mapping

from .priority_audio import AudioPreempted, PriorityAudio
from .runtime_clock import monotonic_now
from .spotter_voice import SpotterVoice
from .voice_settings import VoiceSettingsStore, default_voice_settings, validate_voice_settings


def _map(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _finite(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _identity(snapshot: dict) -> tuple:
    engineer = _map(snapshot.get("engineer"))
    return engineer.get("instance_id"), _map(engineer.get("answer")).get("id")


def concise_answer(answer: dict) -> str:
    """Whole local-renderer sentences only; never speak a truncated number/clause."""
    if answer.get("stale") is not False:
        return "赛况已经变化，这次回答已撤回。请重新提问。"
    if answer.get("scope") != "live_snapshot":
        return "这是一份历史复盘，不是当前比赛指令。请在停车后查看历史报告。"
    text = answer.get("text")
    if type(text) is not str or not text:
        return "当前证据不足，暂时无法回答。"
    parts = re.findall(r"[^。！？\n]+[。！？]?", text[:12000])
    selected: list[str] = []
    count = 0
    for part in parts:
        sentence = part.strip()
        if not sentence:
            continue
        if count + len(sentence) > 260:
            break
        selected.append(sentence)
        count += len(sentence)
    body = "。".join(item.rstrip("。") for item in selected)
    if not body:
        return "回答较长，无法安全压缩。请在停车后查看完整依据。"
    return body + "。这是提问时的证据解读，不是持续更新的进站指令。"


def cue_wave(frequency: int = 880) -> bytes:
    """A short local earcon; no sound is played by constructing this WAV."""
    buffer = io.BytesIO()
    frames = 1600
    pcm = bytearray()
    for index in range(frames):
        envelope = min(1.0, index / 100, (frames - index) / 100)
        value = round(4500 * envelope * math.sin(2 * math.pi * frequency * index / 16000))
        pcm.extend(struct.pack("<h", value))
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(pcm)
    return buffer.getvalue()


def safe_fuel_snapshot(snapshot: dict) -> bool:
    telemetry = _map(snapshot.get("telemetry"))
    monitor = _map(telemetry.get("monitor"))
    data = _map(monitor.get("telemetry"))
    context, quality = _map(monitor.get("context")), _map(monitor.get("quality"))
    fuel = _map(telemetry.get("fuel"))
    age = telemetry.get("updated_age_s")
    brake, steer, speed = (data.get(key) for key in ("brake", "steering_angle_rad", "speed_mps"))
    return (
        snapshot.get("lifecycle") == "RUNNING" and telemetry.get("connection") == "CONNECTED"
        and telemetry.get("source_mode") == "LIVE" and monitor.get("source_kind") == "SDK_LIVE"
        and _finite(age) and 0 <= age <= 2
        and monitor.get("status") in ("READY", "DEGRADED")
        and quality.get("status") in ("READY", "DEGRADED") and quality.get("stale") is False
        and context.get("sim_source_mode") == "FULL"
        and context.get("player_control_state") == "IN_CAR_PHYSICS"
        and context.get("conflicts") == [] and not monitor.get("interval_invalid_for_fuel")
        and monitor.get("interval_unsafe_for_speech") == []
        and fuel.get("status") == "READY" and fuel.get("advisor_only") is True
        and fuel.get("estimate_only") is True and fuel.get("executable") is False
        and telemetry.get("session_type") in ("Practice", "Race")
        and data.get("on_pit_road") is False and data.get("car_left_right") == 1
        and all(_finite(value) for value in (brake, steer, speed))
        and 0 <= brake <= 0.02 and abs(steer) <= 0.05 and speed >= 15
    )


class FuelVoicePolicy:
    """Optional low-fuel facts, not pit commands or validated racing tactics."""

    def __init__(self):
        self._safe_since = None
        self._progress_at = 0.0
        self._sequence = None
        self._generation = None
        self._last_at = -math.inf

    def candidate(self, snapshot: dict, now: float) -> str | None:
        telemetry = _map(snapshot.get("telemetry"))
        sequence = _map(telemetry.get("monitor")).get("sequence")
        generation = telemetry.get("generation")
        if generation != self._generation:
            self._safe_since, self._sequence = None, None
            self._generation = generation
        if type(sequence) is not int or (self._sequence is not None and sequence < self._sequence):
            self._safe_since = None
            return None
        if sequence != self._sequence:
            self._sequence, self._progress_at = sequence, now
        if not safe_fuel_snapshot(snapshot) or now - self._progress_at > 2:
            self._safe_since = None
            return None
        if self._safe_since is None:
            self._safe_since = now
        fuel = telemetry["fuel"]
        amount, laps = fuel.get("current_fuel_l"), fuel.get("estimated_laps_remaining")
        if (now - self._safe_since < 2 or now - self._last_at < 90
                or not _finite(amount) or not 0 <= amount <= 1000
                or not _finite(laps) or not 0 <= laps <= 3):
            return None
        return (f"燃油偏低。当前约{amount:.1f}升，保守估计还能跑{laps:.1f}圈。"
                "只是估计，不是进站指令。")

    def mark_spoken(self, now: float) -> None:
        self._last_at = now


class VoiceService:
    """One bounded work queue; input/guard workers never run Tk operations."""

    def __init__(self, source: Callable[[], dict], submit: Callable, store,
                 *, audio=None, speech=None, input_factory=None, clock=monotonic_now,
                 spotter_source=None, spotter_speech=None):
        if audio is None:
            from .voice_audio import AudioIO
            audio = AudioIO()
        if speech is None:
            from .voice_speech import LocalSpeech
            speech = LocalSpeech()
        if input_factory is None:
            from .voice_inputs import InputPoller
            input_factory = InputPoller
        self._source, self._submit, self._clock = source, submit, clock
        self._audio, self._speech = PriorityAudio(audio, clock=clock), speech

        def fallback_spotter_source():
            value = source()
            return {**_map(value.get("telemetry")), "lifecycle": value.get("lifecycle")}

        self._spotter = SpotterVoice(spotter_source or fallback_spotter_source, self._audio,
                                     speech=spotter_speech, clock=clock)
        self._store = store if isinstance(store, VoiceSettingsStore) else VoiceSettingsStore(store)
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._settings = default_voice_settings()
        self._devices = {"inputs": [], "outputs": [], "recognizers": [], "voices": []}
        self._status, self._notice, self._transcript = "STARTING", "正在加载本机语音设置。", ""
        self._jobs = queue.Queue(maxsize=8)
        self._shutdown = threading.Event()
        self._cancel = threading.Event()
        self._release = threading.Event()
        self._epoch, self._held = 0, False
        self._pending_configurations = 0
        self._spotter_paused = False
        self._binding_revision = 0
        self._binding_pending = None
        self._input_error_revision = 0
        self._worker = self._guard_worker = None
        self._playing_guard = None
        self._poller_started = False
        self._policy = FuelVoicePolicy()
        self._input = input_factory(
            on_press=self.press, on_release=self.release,
            on_binding=self._bound, on_error=self._input_error,
        )

    def start(self) -> None:
        with self._lock:
            if self._worker is not None or self._shutdown.is_set():
                return
            self._status = "STARTING"
            self._worker = threading.Thread(target=self._run, name="vr-voice", daemon=True)
            self._guard_worker = threading.Thread(
                target=self._guard, name="vr-voice-guard", daemon=True,
            )
            self._jobs.put_nowait(("initialize", None))
            self._spotter.start()
            self._worker.start()
            self._guard_worker.start()

    def snapshot(self) -> dict:
        with self._lock:
            value = {
                "status": self._status, "notice": self._notice,
                "settings": copy.deepcopy(self._settings), "devices": copy.deepcopy(self._devices),
                "transcript": self._transcript,
                "binding_active": self._binding_pending is not None,
            }
        value["binding_active"] |= self._input.snapshot().get("binding_active") is True
        value["spotter"] = self._spotter.snapshot()
        return value

    def _put(self, action: str, payload=None):
        if self._shutdown.is_set():
            raise ValueError("VOICE_CLOSED")
        try:
            self._jobs.put_nowait((action, payload))
        except queue.Full:
            raise ValueError("VOICE_BUSY") from None

    def _invalidate(self):
        with self._lock:
            self._cancel.set()
            self._release.set()
            self._epoch += 1
            self._held = False
            self._playing_guard = None

    def configure(self, settings: dict) -> None:
        value = validate_voice_settings(settings)
        with self._lock:
            self._queue_configuration(value)
        self._cancel_speech()

    def _queue_configuration(self, value):
        """Caller holds the state lock; cancellation of inference happens outside."""
        self._invalidate()
        self._spotter.suspend("CONFIGURING")
        self._binding_revision += 1
        self._binding_pending = None
        self._input.cancel_bind()
        self._pending_configurations += 1
        try:
            self._put("configure", value)
        except Exception:
            self._pending_configurations -= 1
            raise
        self._spotter_paused = False

    def _cancel_speech(self):
        # LocalSpeech.cancel only signals its current inference. Reaping stays
        # in the recognition owner, never on the Tk/input callback thread.
        cancel = getattr(self._speech, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                self._set_status("ERROR", "本机识别取消未完成；本次问题不会提交，请检查语音服务。")

    def refresh_devices(self):
        self._put("refresh")

    def bind(self):
        with self._lock:
            self._invalidate()
            self._binding_revision += 1
            revision = self._binding_revision
            self._binding_pending = revision
            try:
                self._put("bind", revision)
            except Exception:
                self._binding_pending = None
                raise
        self._cancel_speech()

    def cancel_bind(self):
        with self._lock:
            self._binding_revision += 1
            self._binding_pending = None
            self._input.cancel_bind()
            if self._poller_started and not self._shutdown.is_set():
                # A selected edge may be waiting to deliver its callback. Restore
                # the configured target so a cancelled selection cannot take over.
                self._input.set_binding(self._settings["binding"])

    def _begin_bind(self, revision):
        with self._lock:
            if (self._shutdown.is_set() or revision != self._binding_pending
                    or self._pending_configurations):
                return
            if not self._poller_started:
                self._input.start(self._settings["binding"])
                self._poller_started = True
            self._input.begin_bind()

    def _bound(self, binding: dict):
        failed = False
        with self._lock:
            if (self._shutdown.is_set() or self._binding_pending is None
                    or self._pending_configurations):
                return
            observed = self._input.snapshot()
            if observed.get("binding_active") is True or observed.get("binding") != binding:
                # A callback can arrive after cancel + a newer begin_bind. Only
                # the poller's still-current completed edge may be persisted.
                return
            settings = {**self._settings, "binding": binding}
            try:
                self._queue_configuration(validate_voice_settings(settings))
            except Exception:
                failed = True
        self._cancel_speech()
        if failed:
            self._input_error("VOICE_BIND_FAILED")

    def _input_error(self, code: str):
        with self._lock:
            self._invalidate()
            if self._shutdown.is_set():
                pass
            elif code in ("INPUT_BINDING_CHANGED", "INPUT_CLOSED", "INPUT_DEVICE_CHANGED"):
                if self._status != "ERROR":
                    self._status = "READY" if self._settings["enabled"] else "OFF"
                self._notice = "按键监听已更新；本次收音已取消，请松开后重新按住说话。"
            else:
                self._input_error_revision += 1
                self._status = "ERROR"
                self._notice = "按键或方向盘连接不可用；当前收音已取消，请检查绑定。"
        self._cancel_speech()

    def press(self):
        with self._lock:
            if (self._held or not self._settings["enabled"] or self._shutdown.is_set()
                    or self._pending_configurations or self._binding_pending is not None):
                return
            self._invalidate()
            self._held = True
            self._cancel, self._release = threading.Event(), threading.Event()
            job = self._epoch, self._cancel, self._release, copy.deepcopy(self._settings)
        self._cancel_speech()
        try:
            self._put("listen", job)
        except ValueError:
            self._input_error("VOICE_BUSY")

    def release(self):
        with self._lock:
            self._held = False
            self._release.set()

    def test(self):
        with self._lock:
            if self._pending_configurations:
                raise ValueError("VOICE_BUSY")
            self._invalidate()
            self._cancel = threading.Event()
            payload = self._epoch, self._cancel, copy.deepcopy(self._settings)
        self._cancel_speech()
        self._put("test", payload)

    def stop(self):
        with self._lock:
            # Linearize with _apply: an older queued/save-in-flight request
            # must not undo the user's newer explicit stop.
            self._spotter_paused = True
            self._spotter.suspend()
            self._invalidate()
            if not self._shutdown.is_set() and self._status != "ERROR":
                self._status = "READY" if self._settings["enabled"] else "OFF"
            self._notice = "收音和播报已取消；已发出的模型请求不会重复发送。"
        self._cancel_speech()

    def _set_status(self, status, notice):
        with self._lock:
            if self._shutdown.is_set() and status not in ("STOPPING", "CLOSED"):
                return
            self._status, self._notice = status, notice

    def _refresh(self):
        if self._shutdown.is_set():
            return
        devices = self._audio.devices()
        if self._shutdown.is_set():
            return
        speech = self._speech.probe()
        with self._lock:
            if self._shutdown.is_set():
                return
            self._devices = {**devices, **speech}
            self._notice = "设备列表已刷新。未选择设备时，使用 Windows 系统默认输入和输出。"

    def _apply(self, settings, *, persist):
        if self._shutdown.is_set():
            return
        if persist:
            self._store.save(settings)
        with self._lock:
            if self._shutdown.is_set():
                return
            error_revision = self._input_error_revision
            self._settings = copy.deepcopy(settings)
            # Cache/rendering has its own worker. A missing STT model or broken
            # microphone must not prevent output-only proximity operation.
            if self._pending_configurations <= 1 and not self._spotter_paused:
                self._spotter.configure(settings)
            if settings["enabled"]:
                if self._poller_started:
                    self._input.set_binding(settings["binding"])
                else:
                    self._input.start(settings["binding"])
                    self._poller_started = True
        if not settings["enabled"] and self._poller_started:
            # close() may wait for a callback that takes our lock: never join
            # the input worker while holding the voice state lock.
            self._close_input()
        with self._lock:
            if self._input_error_revision == error_revision:
                self._set_status("READY" if settings["enabled"] else "OFF",
                                 "语音设置已应用。按住绑定按键，听到提示音后说话，松开后发送。"
                                 if settings["enabled"] else "按住说话已关闭，麦克风不会打开。")

    def _close_input(self):
        self._input.close()
        while self._input.snapshot().get("status") != "CLOSED":
            # InputPoller.close has a bounded join, so STOPPING is not CLOSED.
            # This runs only off the UI thread, and lets callbacks finish.
            threading.Event().wait(0.02)
            self._input.close()
        self._poller_started = False

    def _valid(self, epoch, cancel):
        return not cancel.is_set() and not self._shutdown.is_set() and epoch == self._epoch

    def _say(self, text: str, settings: dict, epoch: int, cancel, guard=None):
        if not self._valid(epoch, cancel):
            return
        wav = self._speech.synthesize(text, voice=settings["voice"])
        if not self._valid(epoch, cancel) or (guard is not None and not guard()):
            return
        playback = (guard, cancel, epoch)
        with self._lock:
            if not self._valid(epoch, cancel):
                return
            self._playing_guard = playback
        try:
            self._set_status("SPEAKING", "正在耳机播报；按住说话键可打断。")
            self._audio.play(
                wav, cancel, device=settings["output_device"], volume=settings["volume"],
            )
        finally:
            with self._lock:
                if self._playing_guard is playback:
                    self._playing_guard = None

    def _listen(self, job):
        epoch, cancel, release, settings = job
        if not self._valid(epoch, cancel) or release.is_set():
            return
        self._set_status("LISTENING", "听到提示音后说话，松开按键提交；最长 12 秒。")
        self._audio.play(
            cue_wave(), cancel, device=settings["output_device"], volume=settings["volume"],
        )
        if not self._valid(epoch, cancel) or release.is_set():
            return
        pcm = self._audio.record(release, device=settings["input_device"], max_seconds=12)
        if not self._valid(epoch, cancel):
            return
        if not release.is_set():
            # The duration cap is not the driver's release-to-send gesture. A
            # truncated utterance must not reach STT or the question queue.
            # Preserve _held until the real release, suppressing repeated presses.
            del pcm
            self._say("本次收音超过十二秒已取消。请松开按键后重新说话。", settings, epoch, cancel)
            if self._valid(epoch, cancel):
                self._set_status("READY", "本次收音超过 12 秒已取消；请松开按键后重新说话。")
            return
        if len(pcm) < 6400:
            self._say("没有听清，请按住按键再说一次。", settings, epoch, cancel)
            return
        self._audio.play(
            cue_wave(660), cancel, device=settings["output_device"], volume=settings["volume"],
        )
        if not self._valid(epoch, cancel):
            return
        self._set_status("RECOGNIZING", "正在本机识别语音；原始音频不外发、不写盘。")
        result = self._speech.recognize(pcm, culture=settings["culture"])
        del pcm
        if not self._valid(epoch, cancel):
            return
        text, confidence = result.get("text"), result.get("confidence")
        # This is a conservative acceptance gate, not a calibrated probability
        # that the recognizer understood the driver's intended question.
        if (type(text) is not str or not 1 <= len(text.strip()) <= 500
                or any(ord(c) < 32 for c in text) or not _finite(confidence) or confidence < 0.5):
            self._say("没有听清，请再说一次。", settings, epoch, cancel)
            return
        with self._lock:
            self._transcript = text.strip()
        before = _identity(self._source())
        # Linearize the final authorization with stop/configuration callbacks.
        # The submit operation only queues existing local work; no network wait.
        with self._lock:
            if (not self._valid(epoch, cancel) or not self._settings["enabled"]
                    or self._pending_configurations):
                return
            status, _ = self._submit(text.strip(), "live")
        if status != 202:
            self._say("工程师仍在处理，请稍后再问。", settings, epoch, cancel)
            return
        self._set_status("WAITING_MODEL", "问题已提交，正在整理回答。")
        deadline = self._clock() + 25
        while self._valid(epoch, cancel) and self._clock() < deadline:
            snapshot = self._source()
            identity = _identity(snapshot)
            answer = _map(_map(snapshot.get("engineer")).get("answer"))
            if identity != before and answer.get("id"):
                if answer.get("stale") is not False:
                    self._say("赛况已经变化，请重新提问。", settings, epoch, cancel)
                    return

                def still_current(expected=identity):
                    current = self._source()
                    current_answer = _map(_map(current.get("engineer")).get("answer"))
                    return (_identity(current) == expected
                            and current_answer.get("stale") is False
                            and current.get("lifecycle") == "RUNNING")

                self._say(concise_answer(dict(answer)), settings, epoch, cancel, still_current)
                return
            cancel.wait(0.05)
        if self._valid(epoch, cancel):
            self._say("回答暂时不可用，请稍后再问。", settings, epoch, cancel)

    def _auto(self):
        with self._lock:
            if (not self._settings["enabled"] or not self._settings["auto_fuel"]
                    or self._held or self._status != "READY" or self._shutdown.is_set()
                    or self._pending_configurations or self._binding_pending is not None):
                return
            settings, epoch = copy.deepcopy(self._settings), self._epoch
        snapshot = self._source()
        text = self._policy.candidate(snapshot, self._clock())
        if text is None:
            return
        cancel = threading.Event()
        with self._lock:
            if (epoch != self._epoch or self._held or self._shutdown.is_set()
                    or self._pending_configurations or self._binding_pending is not None
                    or self._status != "READY" or not self._settings["enabled"]
                    or not self._settings["auto_fuel"]):
                return
            self._cancel = cancel
        generation = _map(snapshot.get("telemetry")).get("generation")

        def still_safe():
            current = self._source()
            return (safe_fuel_snapshot(current)
                    and _map(current.get("telemetry")).get("generation") == generation)

        with self._lock:
            if not self._valid(epoch, cancel):
                return
            self._policy.mark_spoken(self._clock())
        self._say(text, settings, epoch, cancel, still_safe)

    def _run(self):
        while not self._shutdown.is_set():
            try:
                action, payload = self._jobs.get(timeout=0.2)
            except queue.Empty:
                action, payload = "auto", None
            try:
                if action == "initialize":
                    try:
                        settings = self._store.load()
                    except Exception:
                        settings = default_voice_settings()
                    self._apply(settings, persist=False)
                    self._refresh()
                elif action == "configure":
                    self._apply(payload, persist=True)
                elif action == "refresh":
                    self._refresh()
                elif action == "bind":
                    self._begin_bind(payload)
                elif action == "listen":
                    self._listen(payload)
                elif action == "test":
                    epoch, cancel, settings = payload
                    self._say("无线电检查。这是本地语音试听。", settings, epoch, cancel)
                elif action == "auto":
                    self._auto()
            except AudioPreempted:
                if action in ("listen", "test", "auto"):
                    self._invalidate()
                    if action == "listen":
                        self._spotter.interrupted_question()
                    self._set_status("READY" if self._settings["enabled"] else "OFF",
                                     "近车提示优先；本次提问或播报已取消，请松开按键后重新提问。")
                elif action in ("initialize", "refresh"):
                    self._set_status("READY" if self._settings["enabled"] else "OFF",
                                     "近车提示正在使用音频设备；请稍后刷新设备列表。")
            except Exception:
                cancelled = (action in ("listen", "test")
                             and not self._valid(payload[0], payload[1]))
                if not cancelled:
                    self._set_status("ERROR", "语音操作失败，请检查输入输出设备和本机语音模型。")
                else:
                    with self._lock:
                        if not self._shutdown.is_set() and self._status != "ERROR":
                            self._status = "READY" if self._settings["enabled"] else "OFF"
            else:
                with self._lock:
                    if (not self._shutdown.is_set() and self._status != "ERROR"
                            and (action != "auto" or self._status == "SPEAKING")):
                        self._status = "READY" if self._settings["enabled"] else "OFF"
            finally:
                if action == "configure":
                    with self._lock:
                        self._pending_configurations -= 1

    def _guard(self):
        while not self._shutdown.wait(0.1):
            with self._lock:
                playback = self._playing_guard
            if playback is not None:
                guard, cancel, epoch = playback
                try:
                    valid = self._valid(epoch, cancel) and (guard is None or guard())
                except Exception:
                    valid = False
                if not valid:
                    cancel.set()

    def close(self):
        with self._close_lock:
            if self._status == "CLOSED":
                return
            with self._lock:
                self._shutdown.set()
                self._invalidate()
                self._binding_revision += 1
                self._binding_pending = None
                self._set_status("STOPPING", "正在停止收音、播报与后台按键监听。")
            self._cancel_speech()
            self._spotter.close()
            self._input.close()
            for worker in (self._worker, self._guard_worker):
                if worker is not None and worker is not threading.current_thread():
                    worker.join()
            # No work may start input again once the owner worker has exited.
            self._close_input()
            speech_close = getattr(self._speech, "close", None)
            if callable(speech_close):
                speech_close()
            self._set_status("CLOSED", "语音已关闭。")


__all__ = ["VoiceService", "FuelVoicePolicy", "safe_fuel_snapshot", "concise_answer", "cue_wave"]
