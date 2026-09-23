"""Native lifecycle tests with synthetic readers; no game, HTTP or provider calls."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace

import pytest

from iracing_ai_engineer.desktop_controller import DesktopController
from iracing_ai_engineer.desktop_settings import DesktopSettings
from iracing_ai_engineer.llm_engineer import EngineerService


class Store:
    def __init__(self, path, settings=None):
        self.root = path / "desktop"
        self.settings = settings or DesktopSettings()
        self.saved = []

    def load(self):
        return self.settings

    def load_key(self):
        return None

    def save(self, settings, api_key=None):
        self.saved.append((settings, api_key))
        self.settings = settings


class Reader:
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.active = 0
        self.max_active = 0

    def __call__(self, state, stop, config, **kwargs):
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        state.connection("WAIT_SIM")
        stop.wait(10)
        self.active -= 1
        state.connection("STOPPED")


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("synthetic lifecycle timeout")
        time.sleep(0.005)


def test_native_voice_delegates_without_recursive_snapshot_or_key_access(
    monkeypatch, tmp_path, created,
):
    from iracing_ai_engineer import voice_service

    calls = []
    finished = threading.Event()

    class Voice:
        def __init__(self, source, submit, store, *, clock, spotter_source):
            self.source = source
            self.spotter_source = spotter_source
            self.submit = submit

        def start(self):
            calls.append("start")

        def snapshot(self):
            assert "voice" not in self.source()
            assert "engineer" not in self.spotter_source()
            assert "fuel" not in self.spotter_source()
            return {"status": "SYNTHETIC_VOICE"}

        def close(self):
            calls.append("close")
            assert finished.wait(2)

        def __getattr__(self, action):
            return lambda *args: calls.append(action)

    monkeypatch.setattr(voice_service, "VoiceService", Voice)
    controller = created(store=Store(tmp_path), reader=Reader(), voice_runtime=True)
    controller.start()
    assert controller.snapshot()["voice"] == {"status": "SYNTHETIC_VOICE"}
    controller.voice_configure({})
    for action in ("refresh_devices", "bind", "cancel_bind", "press", "release", "test", "stop"):
        getattr(controller, "voice_" + action)()
    controller.close()
    try:
        wait_for(lambda: "close" in calls)
        assert not controller.is_closed()
        with pytest.raises(ValueError, match="VOICE_UNAVAILABLE"):
            controller.voice_press()
    finally:
        finished.set()
    wait_for(controller.is_closed)
    assert calls == ["start", "configure", "refresh_devices", "bind", "cancel_bind",
                     "press", "release", "test", "stop", "close"]


def test_voice_runtime_is_opt_in_in_controller_tests(tmp_path, created):
    controller = created(store=Store(tmp_path), reader=Reader())
    assert "voice" not in controller.snapshot()
    with pytest.raises(ValueError, match="VOICE_UNAVAILABLE"):
        controller.voice_press()


@pytest.fixture
def created():
    controllers = []

    def make(**kwargs):
        controller = DesktopController(environ={}, **kwargs)
        controllers.append(controller)
        return controller

    yield make
    for controller in controllers:
        controller.close()
        wait_for(controller.is_closed)


def test_start_owns_one_reader_and_native_snapshot_has_no_token_or_key(tmp_path, created):
    reader = Reader()
    controller = created(store=Store(tmp_path), reader=reader)
    controller.start()
    controller.start()
    assert reader.entered.wait(1)
    assert len(reader.calls) == 1
    assert reader.calls[0]["record_directory"] == tmp_path / "captures"
    assert reader.calls[0]["record_max_bytes"] == 4 * 1024**3
    result = controller.snapshot()
    assert result["lifecycle"] == "RUNNING"
    assert "csrf_token" not in str(result) and "api_key" not in str(result)
    assert result["engineer"]["status"] == "DISABLED"


def test_local_fuel_query_bypasses_native_general_question_cooldown(tmp_path, created):
    clock = [10.0]
    controller = created(store=Store(tmp_path), reader=Reader(), clock=lambda: clock[0])
    controller.start()
    assert controller.submit("解释当前证据")[0] == 202
    # There need not be live data: the local route should explain its absence,
    # not wait ten seconds or call a provider to report it.
    assert controller.submit("还有多少油")[0] == 202
    assert controller.snapshot()["engineer"]["answer"]["origin"] == "local_live"
    assert controller.submit("还有多少油")[0] == 429
    clock[0] += 1
    assert controller.submit("还能跑几圈")[0] == 202
    assert controller.snapshot()["engineer"]["requests_used"] == 0


def test_close_is_nonblocking_stops_reader_and_clears_key(tmp_path, created):
    reader = Reader()
    controller = created(store=Store(tmp_path), reader=reader)
    controller.start()
    assert reader.entered.wait(1)
    controller._api_key = "SYNTHETIC_KEY"
    began = time.monotonic()
    controller.close()
    assert time.monotonic() - began < 0.2
    wait_for(controller.is_closed)
    assert reader.active == 0 and controller._api_key == ""
    assert controller.snapshot()["lifecycle"] == "STOPPED"
    assert controller.submit("fuel")[0] == 409


def test_setting_input_rejects_invalid_without_starting_job(tmp_path, created):
    controller = created(store=Store(tmp_path), reader=Reader())
    for provider, model, key, remember in (
        ("remote", "deepseek-flash", None, False),
        ("off", "bad model", None, False),
        ("deepseek", "deepseek-flash", "header\ninjection", False),
        ("off", "deepseek-flash", 1, False),
        ("off", "deepseek-flash", None, 1),
    ):
        with pytest.raises(ValueError):
            controller.configure(provider, model, key, remember)
    assert controller._job is None


def test_configure_keeps_secret_only_in_memory_and_persists_explicit_preference(tmp_path, created):
    store = Store(tmp_path)
    controller = created(store=store, reader=Reader())
    controller.configure("off", "deepseek-flash", "SYNTHETIC_KEY", False)
    wait_for(lambda: not controller._configuring)
    snapshot = controller.snapshot()
    assert snapshot["settings"]["key_configured"] is True
    assert "SYNTHETIC_KEY" not in str(snapshot)
    assert store.saved[-1][0].remember_key is False
    controller.configure("off", "deepseek-flash", None, True)
    wait_for(lambda: not controller._configuring)
    assert store.saved[-1][1] == "SYNTHETIC_KEY"
    assert store.saved[-1][0].remember_key is True


def test_reconfiguration_preserves_request_budget_and_does_not_restart_sdk(tmp_path, created):
    reader = Reader()
    controller = created(store=Store(tmp_path), reader=reader)
    controller.start()
    assert reader.entered.wait(1)
    controller._service._requests = 9
    controller.configure("off", "deepseek-flash", None, False)
    wait_for(lambda: not controller._configuring)
    assert controller.snapshot()["engineer"]["requests_used"] == 9
    assert len(reader.calls) == 1


def test_reader_configuration_switch_has_no_overlap_and_respects_budget(tmp_path, created):
    store, reader = Store(tmp_path), Reader()
    controller = created(store=store, reader=reader)
    controller.start()
    assert reader.entered.wait(1)
    controller._state.recording("RECORDING", 123)
    controller.set_recording(False)
    wait_for(lambda: not controller._configuring)
    assert len(reader.calls) == 2 and reader.max_active == 1
    assert reader.calls[-1]["record_directory"] is None
    assert reader.calls[-1]["record_max_bytes"] == 4 * 1024**3 - 123
    assert controller.snapshot()["telemetry"]["recording"]["bytes"] == 123
    assert store.saved[-1][0].recording_enabled is False


def test_missing_historical_file_cannot_replace_current_service(tmp_path, created):
    controller = created(store=Store(tmp_path), reader=Reader())
    previous = controller._service
    controller.load_session(tmp_path / "missing-private-session.json")
    wait_for(lambda: not controller._configuring)
    assert controller._service is previous
    result = controller.snapshot()
    assert str(tmp_path) not in str(result) and "missing-private" not in str(result)
    assert result["engineer"]["capabilities"]["session"] is False


def test_slow_settings_io_does_not_lock_ui_snapshot(tmp_path, created):
    entered, release = threading.Event(), threading.Event()

    class SlowStore(Store):
        def save(self, settings, api_key=None):
            entered.set()
            assert release.wait(2)
            super().save(settings, api_key)

    controller = created(store=SlowStore(tmp_path), reader=Reader())
    controller.configure("off", "deepseek-flash", None, False)
    assert entered.wait(1)
    began = time.monotonic()
    assert controller.snapshot()["engineer"]["status"] == "BUSY"
    assert time.monotonic() - began < 0.2
    assert controller.submit("fuel")[0] == 409
    release.set()
    wait_for(lambda: not controller._configuring)


def test_local_question_works_without_key_and_rate_limit_survives_reconfigure(tmp_path, created):
    clock = [0.0]
    controller = created(store=Store(tmp_path), reader=Reader(), clock=lambda: clock[0])
    assert controller.submit("fuel")[0] == 202
    wait_for(lambda: controller.snapshot()["engineer"]["answer"] is not None)
    controller.configure("off", "deepseek-flash", None, False)
    wait_for(lambda: not controller._configuring)
    assert controller.submit("fuel")[0] == 429
    clock[0] = 10
    assert controller.submit("fuel")[0] == 202


def test_settings_read_failure_starts_safe_defaults_without_leak(tmp_path, created):
    class BrokenStore(Store):
        def load(self):
            raise OSError("SYNTHETIC_PRIVATE_NATIVE_ERROR")

    controller = created(store=BrokenStore(tmp_path), reader=Reader())
    result = controller.snapshot()
    assert result["settings"]["provider"] == "off"
    assert result["settings"]["recording_enabled"] is False
    assert "SYNTHETIC_PRIVATE" not in str(result)


def test_recording_disabled_start_and_invalid_toggle(tmp_path, created):
    reader = Reader()
    store = Store(tmp_path, replace(DesktopSettings(), recording_enabled=False))
    controller = created(store=store, reader=reader)
    controller.start()
    assert reader.entered.wait(1)
    assert reader.calls[0]["record_directory"] is None
    with pytest.raises(ValueError):
        controller.set_recording("yes")


def test_unreadable_key_preserves_every_valid_nonsecret_preference(tmp_path, created):
    class BrokenKeyStore(Store):
        def load_key(self):
            raise OSError("SYNTHETIC_PRIVATE_KEY_ERROR")

    settings = DesktopSettings(
        provider="deepseek",
        model="synthetic-model",
        recording_enabled=False,
        remember_key=True,
    )
    reader = Reader()
    controller = created(store=BrokenKeyStore(tmp_path, settings), reader=reader)
    controller.start()
    assert reader.entered.wait(1)
    snapshot = controller.snapshot()
    for name in ("provider", "model", "recording_enabled", "remember_key"):
        assert snapshot["settings"][name] == getattr(settings, name)
    assert snapshot["settings"]["key_configured"] is False
    assert reader.calls[0]["record_directory"] is None
    assert "SYNTHETIC_PRIVATE_KEY_ERROR" not in str(snapshot)


def test_settings_read_failure_cannot_enable_recording_on_start(tmp_path, created):
    class BrokenStore(Store):
        def load(self):
            raise OSError("SYNTHETIC_PRIVATE_ERROR")

        def load_key(self):
            pytest.fail("Unreadable settings must not authorize loading a key")

    reader = Reader()
    controller = created(store=BrokenStore(tmp_path), reader=reader)
    controller.start()
    assert reader.entered.wait(1)
    assert reader.calls[0]["record_directory"] is None
    assert controller.snapshot()["telemetry"]["recording"]["status"] == "DISABLED"


class SlowFinalizingReader(Reader):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.stopping = threading.Event()

    def __call__(self, state, stop, config, **kwargs):
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        state.connection("WAIT_SIM")
        stop.wait(5)
        self.stopping.set()
        self.release.wait(5)
        self.active -= 1
        state.connection("STOPPED")


def test_close_waits_for_actual_reader_exit_past_notice_deadline(tmp_path, created):
    reader, clock = SlowFinalizingReader(), [0.0]
    controller = created(store=Store(tmp_path), reader=reader, clock=lambda: clock[0])
    controller.start()
    assert reader.entered.wait(1)
    controller.close()
    assert reader.stopping.wait(1)
    try:
        # Allow the service close to reach the join loop, then advance its clock.
        wait_for(lambda: not controller._service._worker.is_alive())

        def has_waiting_notice():
            clock[0] += 20.0
            return "关闭仍在等待" in controller.snapshot()["notice"]

        wait_for(has_waiting_notice)
        assert controller.snapshot()["lifecycle"] == "STOPPING"
        assert controller._reader.is_alive() and not controller.is_closed()
        controller.close()
        controller.start()
        assert len(reader.calls) == 1 and reader.max_active == 1
    finally:
        reader.release.set()
    wait_for(controller.is_closed)
    assert not controller._reader.is_alive()


def test_close_waits_for_inflight_settings_save_and_keeps_ui_responsive(tmp_path, created):
    entered, release = threading.Event(), threading.Event()

    class SlowStore(Store):
        def save(self, settings, api_key=None):
            entered.set()
            release.wait(5)
            super().save(settings, api_key)

    controller = created(store=SlowStore(tmp_path), reader=Reader())
    controller.configure("off", "deepseek-flash", "SYNTHETIC_KEY", False)
    assert entered.wait(1)
    controller.close()
    try:
        wait_for(lambda: not controller._service._worker.is_alive())
        began = time.monotonic()
        snapshot = controller.snapshot()
        assert time.monotonic() - began < 0.2
        assert snapshot["lifecycle"] == "STOPPING"
        assert not controller.is_closed() and controller._job.is_alive()
        assert "SYNTHETIC_KEY" not in str(snapshot)
    finally:
        release.set()
    wait_for(controller.is_closed)
    assert not controller._job.is_alive() and controller._api_key == ""


def test_close_during_recording_toggle_never_starts_a_second_reader(tmp_path, created):
    reader = SlowFinalizingReader()
    controller = created(store=Store(tmp_path), reader=reader)
    controller.start()
    assert reader.entered.wait(1)
    controller.set_recording(False)
    assert reader.stopping.wait(1)
    controller.close()
    try:
        assert not controller.is_closed()
        assert controller.snapshot()["lifecycle"] == "STOPPING"
    finally:
        reader.release.set()
    wait_for(controller.is_closed)
    assert len(reader.calls) == 1 and reader.max_active == 1
    assert not controller._job.is_alive() and not controller._reader.is_alive()


def test_failed_recording_preference_save_reports_effective_runtime_state(tmp_path, created):
    class BrokenSaveStore(Store):
        def save(self, settings, api_key=None):
            raise OSError("SYNTHETIC_PRIVATE_SAVE_ERROR")

    store, reader = BrokenSaveStore(tmp_path), Reader()
    controller = created(store=store, reader=reader)
    controller.start()
    assert reader.entered.wait(1)
    controller._state.recording("RECORDING", 123)
    controller.set_recording(False)
    wait_for(lambda: not controller._configuring)
    snapshot = controller.snapshot()
    assert snapshot["settings"]["recording_enabled"] is False
    assert snapshot["telemetry"]["recording"]["bytes"] == 123
    assert "开关已生效" in snapshot["notice"] and "保存失败" in snapshot["notice"]
    assert "SYNTHETIC_PRIVATE_SAVE_ERROR" not in str(snapshot)
    assert store.settings.recording_enabled is True  # Disk preference remains the old one.
    assert reader.calls[-1]["record_directory"] is None and reader.max_active == 1


def test_frozen_capture_junction_to_repository_disables_only_recording(
    tmp_path, created, monkeypatch
):
    import iracing_ai_engineer.desktop_settings as settings_module
    import iracing_ai_engineer.live_app_recording as recording_module

    checkout, private = tmp_path / "public-checkout", tmp_path / "private"
    checkout.mkdir()
    private.mkdir()
    (checkout / "AGENTS.md").write_text("Synthetic public repository fixture.\n")
    (checkout / "pyproject.toml").write_text("[project]\nname='synthetic'\n")
    alias = private / "captures"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(checkout), str(alias))
    else:
        alias.symlink_to(checkout, target_is_directory=True)
    frozen_root = tmp_path / "_MEI_synthetic" / "iracing_ai_engineer"
    monkeypatch.setattr(settings_module, "__file__", str(frozen_root / "desktop_settings.py"))
    monkeypatch.setattr(recording_module, "__file__", str(frozen_root / "live_app_recording.py"))
    reader = Reader()
    try:
        assert recording_module._public_checkout() is None
        controller = created(store=Store(private), reader=reader)
        controller.start()
        assert reader.entered.wait(1)
        assert reader.calls[0]["record_directory"] is None
        snapshot = controller.snapshot()
        assert snapshot["lifecycle"] == "RUNNING"
        assert snapshot["telemetry"]["connection"] == "WAIT_SIM"
        assert snapshot["telemetry"]["recording"]["status"] == "ERROR"
        assert "只读遥测监视继续运行" in snapshot["notice"]
        assert str(checkout) not in str(snapshot)
        assert list(checkout.glob("*.jsonl")) == []
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()


def test_exhausted_recording_budget_still_starts_one_monitor_only_reader(tmp_path, created):
    reader = Reader()
    controller = created(store=Store(tmp_path), reader=reader)
    controller._capture_bytes = 4 * 1024**3
    controller.start()
    assert reader.entered.wait(1)
    assert reader.calls[0]["record_directory"] is None
    snapshot = controller.snapshot()
    assert snapshot["telemetry"]["recording"] == {
        "status": "LIMIT_REACHED",
        "bytes": 4 * 1024**3,
    }
    assert snapshot["lifecycle"] == "RUNNING"


def test_close_waits_for_inflight_model_worker_before_declaring_closed(
    tmp_path, created, monkeypatch
):
    entered, release = threading.Event(), threading.Event()

    class BlockingPlanner:
        def complete(self, context, question):
            entered.set()
            release.wait(5)
            return {"topic": "status", "fact_ids": [], "notice_ids": []}

    client = BlockingPlanner()

    def service_factory(*args, **kwargs):
        return EngineerService(*args, client=client, **kwargs)

    controller = created(
        store=Store(tmp_path, DesktopSettings(provider="deepseek")),
        reader=Reader(), service_factory=service_factory,
    )
    original_close, waits = controller._service.close, []

    def tracked_close(*, wait=False):
        waits.append(wait)
        return original_close(wait=wait)

    monkeypatch.setattr(controller._service, "close", tracked_close)
    assert controller.submit("当前有哪些证据？")[0] == 202
    assert entered.wait(1)
    began = time.monotonic()
    controller.close()
    assert time.monotonic() - began < 0.2
    try:
        wait_for(lambda: controller._service._closed.is_set())
        assert waits == [True]
        assert controller._service._worker.is_alive()
        assert not controller.is_closed()
        assert controller.snapshot()["lifecycle"] == "STOPPING"
    finally:
        release.set()
    wait_for(controller.is_closed)
    assert not controller._service._worker.is_alive()
