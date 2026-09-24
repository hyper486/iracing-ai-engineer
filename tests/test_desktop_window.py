"""Presentation and hidden native-Tk tests; no SDK, network or cloud calls."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path

import pytest

from iracing_ai_engineer.desktop_window import (
    DesktopPresenter,
    DesktopWindow,
    format_number,
    trial_report_text,
    validate_question,
    voice_choices,
)
from iracing_ai_engineer.voice_settings import validate_voice_settings

_MIC = "sd-" + "1" * 64
_HEADSET = "sd-" + "2" * 64
_SECOND_MIC = "sd-" + "3" * 64


def _snapshot() -> dict:
    return {
        "lifecycle": "RUNNING", "notice": "只读服务",
        "settings": {"provider": "off", "model": "deepseek-flash",
                     "remember_key": False, "recording_enabled": False,
                     "key_configured": True},
        "telemetry": {
            "connection": "CONNECTED", "source_mode": "LIVE", "generation": 1,
            "updated_age_s": 0.1,
            "monitor": {"sequence": 1, "status": "READY", "source_kind": "SDK_LIVE",
                        "quality": {"status": "READY", "stale": False}, "reasons": [],
                        "context": {"sim_source_mode": "FULL",
                                    "player_control_state": "IN_CAR_PHYSICS"}},
            "fuel": {"status": "READY", "advisor_only": True, "estimate_only": True,
                     "executable": False, "current_fuel_l": 21.2, "valid_laps": 3,
                     "required_laps": 3, "estimated_laps_remaining": 8,
                     "conservative_burn_l_per_lap": 2.5, "fuel_needed_to_finish_l": 40.0,
                     "fuel_to_add_l": 18.8, "minimum_stops": 1, "message": "仅作实验性估计。"},
            "recording": {"status": "DISABLED", "bytes": 0}, "limitations": [],
        },
        "engineer": {"status": "DISABLED", "error": None, "model": "deepseek-flash",
                     "requests_used": 0, "request_limit": 60, "retry_after_s": 0,
                     "capabilities": {"session": False}, "instance_id": 1,
                     "answer": None},
        "voice": {
            "status": "OFF", "notice": "语音默认关闭。", "transcript": "",
            "binding_active": False,
            "settings": {"enabled": False, "input_device": "default",
                         "output_device": "default", "volume": 0.7, "culture": "zh-CN",
                         "voice": "", "binding": {"kind": "keyboard", "key": "F9"},
                         "auto_fuel": False, "spotter_enabled": False},
            "devices": {"inputs": [{"id": _MIC, "name": "Desktop microphone"}],
                        "outputs": [{"id": _HEADSET, "name": "VR headphones"}],
                        "recognizers": [{"culture": "zh-CN", "name": "中文识别"}],
                        "voices": [{"culture": "zh-CN", "name": "Synthetic Chinese voice"}]},
        },
    }


@pytest.mark.parametrize("value", [
    None, True, False, "3", -1, float("nan"), float("inf"), 10**1000,
])
def test_invalid_measurements_are_not_rendered_as_zero(value) -> None:
    assert format_number(value, " L") == "—"


def test_numeric_formatting_and_question_validation() -> None:
    assert format_number(0, " L") == "0.0 L"
    assert format_number(3.14159, " L/圈", 2) == "3.14 L/圈"
    assert validate_question("  现在有什么证据？  ") == "现在有什么证据？"
    assert validate_question("字" * 500) == "字" * 500


def test_trial_report_fixed_text_separates_replay_playback_and_hearing():
    report = {"status": "REPLAY_MATCH", "frames": 300, "decisions": {"CANDIDATE": 2},
              "audio_outcomes": {"ATTEMPTED": 1, "PLAYBACK_STARTED": 1},
              "event_results": {"DETECTED_NO_RECORDED_ATTEMPT": 1},
              "health_transitions": {"SDK_SPOTTER_OFF": 1}, "unbound_audio_records": 0,
              "private": "NEVER DISPLAY THIS"}
    text = trial_report_text(report)
    assert "已调用音频输出：1" in text and "未记录播放尝试：1" in text
    assert "不等于功能验收" in text and "不能证明人耳听到" in text
    assert "关闭状态" in text and "NEVER DISPLAY THIS" not in text
    assert "不完整" in trial_report_text({"status": "INCOMPLETE_PREFIX"})
    assert "失败" in trial_report_text({"status": "REJECTED", "reason": "PRIVATE ERROR"})
    assert "PRIVATE ERROR" not in trial_report_text(
        {"status": "REJECTED", "reason": "PRIVATE ERROR"})


def test_trial_fault_is_visible_without_withdrawing_independent_proximity():
    value = _snapshot()
    value["telemetry"]["spotter"] = {"status": "READY"}
    value["telemetry"]["trial_audit"] = {"status": "ERROR"}
    view = DesktopPresenter().project(value, now=10)
    assert "日志故障" in view.recording
    assert "判定数据就绪" in view.spotter


def test_proximity_diagnostic_readiness_is_never_presented_as_working_audio():
    snapshot = _snapshot()
    snapshot["telemetry"]["spotter"] = {"status": "READY"}
    view = DesktopPresenter().project(snapshot, now=10.0)
    assert "判定数据就绪" in view.spotter
    assert "仅诊断" in view.spotter and "音频尚未接入" in view.spotter
    snapshot["telemetry"]["spotter"]["status"] = "STALE"
    view = DesktopPresenter().project(snapshot, now=10.0)
    assert "过期" in view.spotter


def test_cloud_busy_does_not_disable_local_fuel_questions_or_hide_local_answer():
    snapshot = _snapshot()
    snapshot["engineer"].update(
        status="BUSY", local_live_available=True, local_retry_after_s=0,
        answer={"id": "8", "origin": "local_live", "scope": "live_snapshot", "stale": False,
                "age_s": 0.1, "snapshot_was_valid": True, "text": "当前观测剩余燃油：20.00 升。"},
    )
    view = DesktopPresenter().project(snapshot, now=10)
    assert view.can_submit and "本地实时直答" in view.answer_header
    assert "20.00" in view.answer_text and "本地直答" in view.budget
    snapshot["engineer"]["local_live_available"] = False  # A settings change still gates input.
    assert not DesktopPresenter().project(snapshot, now=10).can_submit


@pytest.mark.parametrize("status,output,reason,expected", [
    ("OFF", "UNTESTED", "DISABLED", "近车语音未启用"),
    ("PREPARING", "UNTESTED", "PHRASE_CACHE", "正在预生成短语音"),
    ("READY", "UNTESTED", "DETECTOR_READY", "设备尚未实际播放验证"),
    ("READY", "STARTED_NOT_HEARING_CONFIRMED", "DETECTOR_READY", "未确认人耳听到"),
    ("WAIT_DATA", "START_DEADLINE_MISSED", "NEED_FRESH_IN_CAR_DATA", "提示已丢弃"),
    ("PAUSED", "UNTESTED", "ZERO_VOLUME", "音量为零"),
    ("ERROR", "FAILED", "AUDIO_DEVICE_MISSING", "近车语音故障"),
])
def test_detector_and_audio_health_remain_separate_without_implying_hearing(
    status, output, reason, expected,
):
    snapshot = _snapshot()
    snapshot["telemetry"]["spotter"] = {"status": "READY"}
    snapshot["voice"]["spotter"] = {
        "status": status, "output_status": output, "reason": reason,
    }
    view = DesktopPresenter().project(snapshot, now=10.0)
    assert "判定数据就绪" in view.spotter and expected in view.spotter
    assert "音频尚未接入" not in view.spotter


def test_analysis_failure_and_recording_overflow_are_not_presented_as_sdk_disconnection():
    snapshot = _snapshot()
    telemetry = snapshot["telemetry"]
    telemetry.update(connection="DISCONNECTED", transport_connection="CONNECTED",
                     monitor=None, fuel=None,
                     workers={"analysis": {"status": "ERROR", "reason": "QUEUE_STALE"},
                              "recording": {"status": "ERROR", "reason": "QUEUE_OVERFLOW"}},
                     recording={"status": "ERROR", "bytes": 1000})
    telemetry["spotter"] = {"status": "READY"}
    view = DesktopPresenter().project(snapshot, now=10.0)
    assert "采集已连接" in view.connection and "暂停" in view.quality
    assert "积压" in view.quality and "本段不完整" in view.recording
    assert "近车判定数据就绪" in view.spotter
    assert set(view.metrics.values()) == {"—"}


@pytest.mark.parametrize("status,expected", [
    ("STARTING", "初始化"), ("DRAINING", "尚未完成"), ("EMPTY", "未录到数据"),
    ("COMPLETE", "非驾驶验收"),
])
def test_recording_start_drain_empty_and_complete_have_distinct_labels(status, expected):
    snapshot = _snapshot()
    snapshot["telemetry"]["recording"] = {"status": status, "bytes": 0}
    assert expected in DesktopPresenter().project(snapshot, now=10).recording


@pytest.mark.parametrize("old_status", ["DRAINING", "INCOMPLETE"])
def test_old_recording_owner_is_not_presented_as_recording_the_new_connection(old_status):
    snapshot = _snapshot()
    snapshot["telemetry"].update(
        generation=3, recording={"status": "RESTART_REQUIRED", "bytes": 100},
        workers={"recording": {"status": old_status, "generation": 1}},
    )
    text = DesktopPresenter().project(snapshot, now=10).recording
    assert "本连接未录制" in text and "重启采集" in text


def test_voice_choices_keep_exact_ids_while_labels_are_unique_and_bounded() -> None:
    choices = voice_choices([
        {"id": "mic-a", "name": "Microphone"}, {"id": "mic-b", "name": "Microphone"},
        {"id": "mic-a", "name": "Duplicate identifier"}, {"id": True, "name": "Invalid"},
        {"id": "x" * 513, "name": "Invalid"}, None, {"id": "empty", "name": ""},
    ], field="id", default=("System default", "default"))
    assert choices == [("System default", "default"), ("Microphone", "mic-a"),
                       ("Microphone (2)", "mic-b")]
    assert voice_choices(None, field="id", default=("Default", "")) == [("Default", "")]
    assert voice_choices([{"culture": "zh-CN", "name": "Local voice"}],
                         field="name", default=("Default", ""))[-1] == (
                             "Local voice · zh-CN", "Local voice",
                         )


@pytest.mark.parametrize("question", [None, 1, " ", "x" * 501, "fuel\x00"])
def test_invalid_questions_fail_before_controller(question) -> None:
    with pytest.raises(ValueError, match="INVALID_QUESTION"):
        validate_question(question)


def test_ready_projection_and_frozen_sequence_clears_metrics() -> None:
    presenter, state = DesktopPresenter(), _snapshot()
    view = presenter.project(state, now=10.0)
    assert view.metrics["current"] == "21.2 L" and view.metrics["laps"] == "8.0 圈"
    assert view.learning == "3 / 3 圈" and view.progress == 100
    view = presenter.project(state, now=12.01)
    assert set(view.metrics.values()) == {"—"}
    assert view.tone == "bad"
    state["telemetry"]["monitor"]["sequence"] = 2
    assert presenter.project(state, now=12.1).metrics["current"] == "21.2 L"


@pytest.mark.parametrize("change", ["spectator", "replay", "old", "quality", "stopped"])
def test_unsafe_or_non_driving_state_withdraws_metrics(change: str) -> None:
    state = _snapshot()
    if change == "spectator":
        state["telemetry"]["monitor"]["context"]["player_control_state"] = (
            "OUT_OF_CAR_OR_REPLAY_VIEW"
        )
    elif change == "replay":
        state["telemetry"]["monitor"]["context"]["sim_source_mode"] = "REPLAY_FILE"
    elif change == "old":
        state["telemetry"]["updated_age_s"] = 2.01
    elif change == "quality":
        state["telemetry"]["monitor"]["quality"]["stale"] = True
    else:
        state["lifecycle"] = "STOPPING"
    view = DesktopPresenter().project(state, now=10)
    assert set(view.metrics.values()) == {"—"}


def test_demo_and_learning_are_not_promoted_to_real_estimates() -> None:
    state = _snapshot()
    state["telemetry"]["source_mode"] = "SYNTHETIC_DEMO"
    state["telemetry"]["fuel"]["status"] = "LEARNING"
    view = DesktopPresenter().project(state, now=10)
    assert "合成演示" in view.source and "不能作为真实验收" in view.context
    assert view.metrics["current"] == "21.2 L" and view.metrics["laps"] == "—"


def _answer(*, was_valid: bool = True, scope: str = "live_snapshot") -> dict:
    return {"id": "1", "text": "观测燃油 21.2 升。", "origin": "local_fallback",
            "scope": scope, "snapshot_was_valid": was_valid, "stale": False, "age_s": 1}


def test_stale_answer_withdrawal_is_latched_but_no_evidence_and_history_remain_readable() -> None:
    presenter, state = DesktopPresenter(), _snapshot()
    state["engineer"]["answer"] = _answer()
    assert "21.2" in presenter.project(state, now=10).answer_text
    state["telemetry"]["connection"] = "DISCONNECTED"
    assert "21.2" not in presenter.project(state, now=10.1).answer_text
    state["telemetry"]["connection"] = "CONNECTED"
    state["telemetry"]["monitor"]["sequence"] = 2
    assert "21.2" not in presenter.project(state, now=10.2).answer_text
    state["engineer"]["answer"] = {**_answer(was_valid=False), "id": "2", "text": "没有有效证据。"}
    state["telemetry"]["connection"] = "DISCONNECTED"
    view = presenter.project(state, now=10.3)
    assert view.answer_text == "没有有效证据。" and "无有效实时证据" in view.answer_header
    state["engineer"]["answer"] = {**_answer(scope="historical_session"), "id": "3"}
    assert "历史报告" in presenter.project(state, now=10.4).answer_header


def test_native_service_instance_namespaces_answer_ids_after_reconfiguration() -> None:
    state, presenter = _snapshot(), DesktopPresenter()
    state["engineer"]["answer"] = {**_answer(), "stale": True}
    assert "21.2" not in presenter.project(state, now=0).answer_text
    state["engineer"]["instance_id"] = 2
    state["engineer"]["answer"]["stale"] = False
    assert "21.2" in presenter.project(state, now=0.1).answer_text


def test_twenty_thousand_withdrawals_are_constant_space_and_cannot_revive():
    presenter = DesktopPresenter()
    for serial in range(1, 20_001):
        value = {"instance_id": "3", "answer": {
            **_answer(), "id": str(serial), "stale": True}}
        assert "21.2" not in presenter._answer(value, True)[1]
    assert presenter._answer_order == (3, 20_000)
    assert not any(isinstance(item, (list, set, dict)) for item in vars(presenter).values())
    # The earliest ID is still rejected, even after many more expired answers.
    assert "21.2" not in presenter._answer({"instance_id": 3, "answer": _answer()}, True)[1]
    fresh = {"instance_id": "4", "answer": _answer()}
    assert "21.2" in presenter._answer(fresh, True)[1]
    assert "21.2" not in presenter._answer(value, True)[1]
    assert "21.2" in presenter._answer(fresh, True)[1]  # Old epoch cannot poison new one.


@pytest.mark.parametrize("change", [
    {"origin": "deepseek"}, {"scope": "historical_session"}, {"snapshot_was_valid": False},
])
def test_reused_answer_identity_cannot_change_semantics_or_revive(change):
    presenter = DesktopPresenter()
    original = {"instance_id": 0, "answer": _answer()}
    assert "21.2" in presenter._answer(original, True)[1]
    changed = {"instance_id": 0, "answer": {**_answer(), **change}}
    assert "21.2" not in presenter._answer(changed, True)[1]
    assert "21.2" not in presenter._answer(original, True)[1]


@pytest.mark.parametrize("bad", [None, True, -1, "-1", "01", "１", "1" * 10000, 2**54])
@pytest.mark.parametrize("field", ["instance_id", "id"])
def test_malformed_answer_order_is_not_displayed(bad, field):
    value = {"instance_id": 0, "answer": _answer()}
    target = value if field == "instance_id" else value["answer"]
    target[field] = bad
    assert "21.2" not in DesktopPresenter()._answer(value, True)[1]


@pytest.mark.parametrize("status,allowed", [
    ("DISABLED", True), ("MISSING_KEY", True), ("ERROR", True), ("BUDGET_EXHAUSTED", True),
    ("READY", True), ("BUSY", False), ("RATE_LIMITED", False), ("unexpected", False),
])
def test_question_readiness_and_error_redaction(status, allowed) -> None:
    state = _snapshot()
    state["engineer"].update(status=status, error="raw private error not for display")
    view = DesktopPresenter().project(state, now=0)
    assert view.can_submit is allowed
    assert "raw private" not in view.engineer_error
    assert "本地解读" in view.engineer_error
    if status == "READY":
        assert "已配置" in view.engineer_status and "不代表 API 已验证" in view.engineer_status


class _Controller:
    def __init__(self):
        self.value = _snapshot()
        self.questions = []
        self.configurations = []
        self.sessions = []
        self.recordings = []
        self.close_calls = 0
        self.closed = False
        self.voice_calls = []
        self.voice_configurations = []

    def snapshot(self):
        return copy.deepcopy(self.value)

    def submit(self, question, scope="live"):
        self.questions.append((question, scope))
        self.value["engineer"]["status"] = "BUSY"
        return 202, {"accepted": True}

    def configure(self, **kwargs):
        self.configurations.append(kwargs)

    def load_session(self, path):
        self.sessions.append(path)

    def set_recording(self, enabled):
        self.recordings.append(enabled)

    def close(self):
        self.close_calls += 1
        self.value["lifecycle"] = "STOPPING"

    def is_closed(self):
        return self.closed

    def voice_configure(self, settings):
        self.voice_configurations.append(validate_voice_settings(settings))

    def voice_refresh_devices(self):
        self.voice_calls.append("refresh")

    def voice_bind(self):
        self.voice_calls.append("bind")
        self.value["voice"]["binding_active"] = True

    def voice_cancel_bind(self):
        self.voice_calls.append("cancel_bind")
        self.value["voice"]["binding_active"] = False

    def voice_press(self):
        self.voice_calls.append("press")

    def voice_release(self):
        self.voice_calls.append("release")

    def voice_test(self):
        self.voice_calls.append("test")

    def voice_stop(self):
        self.voice_calls.append("stop")


def _run_native_scenario(name: str) -> None:
    """One real Tcl/Tk interpreter per process, matching desktop production.

    Destroying a Tk root does not dispose of its Python/Tcl interpreter while
    pytest frames and widget/StringVar cycles retain references. Do not create
    subsequent interpreters in that host process, or mask initialization errors
    with retries. Each child must execute every scenario assertion and exit.
    """
    path = Path(__file__).resolve()
    environ = os.environ.copy()
    environ["PYTHONPATH"] = (
        str(path.parents[1] / "src") + os.pathsep + environ.get("PYTHONPATH", "")
    )
    environ["PYTHONIOENCODING"] = "utf-8"
    environ.pop("DEEPSEEK_API_KEY", None)
    result = subprocess.run(
        [sys.executable, str(path), name], cwd=path.parents[1], env=environ,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        check=False,
    )
    if result.returncode == 77 and sys.platform != "win32":
        pytest.skip("Native Tk display is unavailable on this test host")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == f"NATIVE_CASE_PASS:{name}", result.stdout + result.stderr


def _execute_native_scenario(name: str) -> None:
    case = _NATIVE_SCENARIOS[name]
    try:
        root = tk.Tk()
    except tk.TclError:
        if sys.platform == "win32":
            raise  # Native Windows failures must not be hidden as headless skips.
        raise SystemExit(77) from None
    root.withdraw()
    callback_errors = []
    root.report_callback_exception = lambda *error: callback_errors.append(error)
    window = None
    try:
        controller = _Controller()
        window = DesktopWindow(root, controller)
        root.update()
        case((root, controller, window))
        assert not callback_errors, callback_errors
    finally:
        if window is None or not window._destroyed:
            root.destroy()
    assert not callback_errors, callback_errors
    print(f"NATIVE_CASE_PASS:{name}")


def _native_settings(native_window) -> None:
    root, controller, window = native_window
    assert window._vars["metric_current"].get() == "21.2 L"
    assert window.key_var.get() == ""
    assert window.key_entry.cget("show") == "●"
    assert window.provider_var.get() is False
    assert "已配置密钥" in window._vars["key_status"].get()
    window.provider_var.set(True)
    window.model_var.set("test-model")
    window.remember_var.set(True)
    window.key_var.set("synthetic-test-key")
    window._configure()
    assert controller.configurations == [{"provider": "deepseek", "model": "test-model",
                                          "api_key": "synthetic-test-key", "remember_key": True}]
    assert window.key_var.get() == ""
    window._poll()
    assert window.model_var.get() == "test-model"  # Polling must not overwrite edits.
    window._configure()
    assert controller.configurations[-1]["api_key"] is None
    assert root.state() == "withdrawn"


def _native_question(native_window) -> None:
    _, controller, window = native_window
    for label, question in (("问本段", "这一段跑了多久"), ("问轮胎", "轮胎怎么样"),
                            ("问配速", "配速变化"), ("问出站", "出站预测")):
        button = next(item for item in window._question_buttons if item.cget("text") == label)
        button.invoke()
        assert controller.questions[-1] == (question, "live")
        controller.value["engineer"]["status"] = "DISABLED"
        window._poll()
    controller.questions.clear()
    window.question_text.delete("1.0", "end")
    window.question_text.insert("1.0", "根据证据解释燃油")
    window._submit()
    assert controller.questions == [("根据证据解释燃油", "live")]
    window._poll()
    assert all(button.instate(["disabled"]) for button in window._question_buttons)
    controller.value["engineer"].update(status="DISABLED", capabilities={"session": True})
    controller.value["engineer"]["answer"] = {
        **_answer(), "text": '<img src="not-loaded">仅显示文字',
    }
    window._poll()
    assert window.answer_text.get("1.0", "end-1c") == '<img src="not-loaded">仅显示文字'
    assert "回答已更新" in window.request_var.get()
    assert window.answer_text.cget("state") == "disabled"
    window._submit_history()
    assert controller.questions[-1][1] == "session"
    window._set_session(Path("synthetic-session.json"))
    assert controller.sessions == [Path("synthetic-session.json")]
    window.recording_var.set(True)
    window._recording()
    assert controller.recordings == [True]


def _native_close(native_window) -> None:
    root, controller, window = native_window
    window._request_close()
    assert controller.close_calls == 1 and not window._destroyed
    assert all(control.instate(["disabled"]) for control in window._controls)
    window._request_close()
    assert controller.close_calls == 1
    window._poll()
    assert root.winfo_exists() and not window._destroyed
    controller.closed = True
    window._poll()
    assert window._destroyed


def _native_validation(native_window) -> None:
    _, controller, window = native_window
    window.question_text.insert("1.0", "x" * 501)
    window._submit()
    assert controller.questions == [] and "500" in window.request_var.get()

    def invalid_config(**kwargs):
        raise ValueError("synthetic secret should never appear")

    controller.configure = invalid_config
    window.key_var.set("synthetic secret should never appear")
    window._configure()
    assert "synthetic secret" not in window.action_var.get()
    assert window.key_var.get() == ""


def _native_voice_defaults_and_configuration(native_window) -> None:
    _, controller, window = native_window
    assert [window.notebook.tab(tab, "text") for tab in window.notebook.tabs()][-2:] == [
        "语音与 VR", "采集复盘",
    ]
    assert not window.voice_enabled_var.get() and not window.voice_auto_fuel_var.get()
    assert window._vars["voice_status"].get() == "按住说话已关闭"
    assert window.voice_key_var.get() == "F9" and window.voice_volume_var.get() == 0.7
    assert controller.voice_calls == [] and controller.voice_configurations == []
    assert window.voice_test_button.instate(["disabled"])
    assert window.voice_ptt_button.instate(["disabled"])
    window._voice_press()
    assert controller.voice_calls == []
    for field, selected in (("input_device", _MIC),
                            ("output_device", _HEADSET),
                            ("voice", "Synthetic Chinese voice")):
        combo, variable = window._voice_menus[field]
        assert combo.instate(["readonly"])
        variable.set(next(label for label, identity in window._voice_options[field]
                          if identity == selected))
    window.voice_enabled_var.set(True)
    window.voice_volume_var.set(0.4)
    window.voice_key_var.set("F10")
    window._voice_keyboard_binding()
    window._poll()  # Server snapshot must not overwrite unsaved selections.
    assert window.voice_enabled_var.get() and window.voice_volume_var.get() == 0.4
    assert window._voice_selection("input_device") == _MIC
    window._voice_configure()
    assert controller.voice_configurations == [{
        "enabled": True, "input_device": _MIC, "output_device": _HEADSET,
        "voice": "Synthetic Chinese voice", "culture": "zh-CN", "volume": 0.4,
        "binding": {"kind": "keyboard", "key": "F10"}, "auto_fuel": False,
        "spotter_enabled": False,
    }]
    assert controller.voice_calls == []  # Applying never starts test audio or recording.
    controller.value["voice"]["settings"].update(controller.voice_configurations[-1])
    controller.value["voice"]["status"] = "READY"
    window._poll()
    window.voice_test_button.invoke()
    window.voice_stop_button.invoke()
    assert controller.voice_calls == ["test", "stop"]


def _native_voice_device_refresh_and_binding(native_window) -> None:
    _, controller, window = native_window
    controller.value["voice"]["settings"]["enabled"] = True
    window._poll()
    window._voice_call("voice_refresh_devices")
    controller.value["voice"]["devices"]["inputs"].append(
        {"id": _SECOND_MIC, "name": "Desktop microphone"}
    )
    window._poll()
    assert len(window._voice_options["input_device"]) == 3
    window._voice_menus["input_device"][1].set("Desktop microphone (2)")
    controller.value["voice"]["devices"]["inputs"] = []
    window._poll()
    assert window._voice_selection("input_device") == _SECOND_MIC
    assert "暂未枚举" in window._voice_menus["input_device"][1].get()
    controller.value["voice"]["devices"]["recognizers"].append(
        {"culture": "en-US", "name": "English recognition"}
    )
    controller.value["voice"]["devices"]["voices"].append(
        {"culture": "en-US", "name": "Synthetic English voice"}
    )
    window._poll()
    assert "Synthetic English voice" not in {
        identity for _, identity in window._voice_options["voice"]
    }
    window._voice_menus["culture"][1].set("English recognition · en-US")
    window._voice_culture_changed()
    assert window._voice_selection("voice") == ""
    assert "Synthetic Chinese voice" not in {
        identity for _, identity in window._voice_options["voice"]
    }
    window._voice_call("voice_bind")
    window._poll()
    assert window.voice_ptt_button.instate(["disabled"])
    binding = {"kind": "joystick", "guid": "a" * 32, "name": "Synthetic wheel", "button": 2}
    controller.value["voice"]["settings"]["binding"] = binding
    controller.value["voice"]["binding_active"] = False
    window._poll()
    assert window._voice_binding == binding and "方向盘" in window._vars["voice_binding"].get()
    window._voice_configure()
    assert controller.voice_configurations[-1]["binding"] == binding
    window._voice_call("voice_cancel_bind")
    assert controller.voice_calls == ["refresh", "bind", "cancel_bind"]


def _native_voice_ptt_release_and_close(native_window) -> None:
    root, controller, window = native_window
    controller.value["voice"]["settings"]["enabled"] = True
    window._poll()
    window._voice_press()
    window._voice_press()
    assert controller.voice_calls == ["press"]
    root.event_generate("<FocusOut>")
    root.update()
    assert controller.voice_calls == ["press", "release"]
    window._voice_release()
    assert controller.voice_calls == ["press", "release"]
    window._voice_press()
    window._request_close()
    assert controller.voice_calls == ["press", "release", "press", "release"]
    assert controller.close_calls == 1
    assert all(control.instate(["disabled"]) for control in window._voice_controls)
    window._voice_call("voice_test")
    assert "test" not in controller.voice_calls


def _native_voice_errors_and_unavailable(native_window) -> None:
    _, controller, window = native_window

    def fail(*_args):
        raise ValueError("synthetic raw private audio error")

    controller.voice_configure = fail
    window._voice_configure()
    assert "未能应用" in window.voice_action_var.get()
    assert "private" not in window.voice_action_var.get()
    controller.voice_refresh_devices = fail
    window._voice_call("voice_refresh_devices")
    assert "private" not in window.voice_action_var.get()
    controller.value.pop("voice")
    window._poll()
    assert all(control.instate(["disabled"]) for control in window._voice_controls)
    assert "尚未就绪" in window._vars["voice_status"].get()


def _native_voice_pending_binding_and_lost_snapshot(native_window) -> None:
    _, controller, window = native_window
    controller.value["voice"]["settings"]["enabled"] = True
    window._poll()
    controller.voice_bind = lambda: controller.voice_calls.append("bind")
    window._voice_call("voice_bind")
    window._poll()  # Background worker has not started binding yet.
    assert window._voice_binding_requested
    controller.value["voice"]["settings"]["binding"] = {
        "kind": "joystick", "guid": "a" * 32, "name": "Synthetic wheel", "button": 1,
    }
    window._poll()  # Binding completed entirely between two UI polls.
    assert window._voice_binding["kind"] == "joystick"
    assert not window._voice_binding_requested
    window._voice_press()
    controller.value.pop("voice")
    window._poll()
    assert controller.voice_calls[-2:] == ["press", "release"]
    assert not window._screen_ptt_held


def _native_voice_delayed_initial_settings(native_window) -> None:
    _, controller, window = native_window
    # Reproduce first render before asynchronous saved-settings load finishes.
    window._initialized_voice = False
    controller.value["voice"]["status"] = "STARTING"
    window._poll()
    assert not window._initialized_voice
    assert window.voice_apply_button.instate(["disabled"])
    controller.value["voice"]["settings"].update(
        enabled=True, input_device=_MIC, output_device=_HEADSET, volume=0.5,
        voice="Synthetic Chinese voice", binding={"kind": "keyboard", "key": "F12"},
    )
    controller.value["voice"]["status"] = "READY"
    window._poll()
    assert window._initialized_voice and window.voice_enabled_var.get()
    assert window._voice_selection("input_device") == _MIC
    assert window._voice_selection("output_device") == _HEADSET
    assert window.voice_key_var.get() == "F12" and window.voice_volume_var.get() == 0.5
    assert controller.voice_calls == []
    window.voice_enabled_var.set(False)
    window._poll()
    assert not window.voice_enabled_var.get()  # Subsequent polls preserve edits.


def _native_trial_replay(native_window):
    from unittest.mock import patch

    _, controller, window = native_window
    paths = []
    controller.replay_trial = paths.append
    with patch("iracing_ai_engineer.desktop_window.filedialog.askopenfilename",
               return_value="C:/Users/racer/trial-synthetic.jsonl"):
        window._load_trial()
    assert paths == [Path("C:/Users/racer/trial-synthetic.jsonl")]
    assert not controller.questions and not controller.voice_calls
    controller.value["trial_report"] = {"status": "INCOMPLETE_PREFIX", "frames": 12}
    window._poll()
    assert "日志不完整" in window.trial_text.get("1.0", "end")
    controller.value["trial_report"] = {"status": "REPLAY_MATCH", "frames": 30,
                                        "audio_outcomes": {"PLAYBACK_STARTED": 1}}
    window._poll()
    assert "已调用音频输出：1" in window.trial_text.get("1.0", "end")
    assert "不等于功能验收" in window.trial_text.get("1.0", "end")


def _native_strategy_configuration(native_window):
    from iracing_ai_engineer.live_strategy import StrategyParameters

    _, controller, window = native_window
    calls = []
    controller.configure_strategy = calls.append
    window._poll()
    assert all(not button.instate(["disabled"]) for button in window.strategy_buttons)
    window.strategy_vars["tank_capacity_l"].set("50")
    window.strategy_vars["refuel_rate_l_per_s"].set("2")
    window.strategy_vars["pit_loss_low_s"].set("20")
    window.strategy_vars["pit_loss_high_s"].set("24")
    window._configure_strategy()
    assert len(calls) == 1
    assert StrategyParameters(**calls[0]) == StrategyParameters(50, 2, 20, 24)
    assert "不会自动保存" in window.action_var.get()
    for name, number in (("pit_entry_fraction", ".8"), ("pit_exit_fraction", ".05"),
                         ("complete_pit_loss_low_s", "30"), ("complete_pit_loss_high_s", "35")):
        window.strategy_vars[name].set(number)
    window._configure_strategy()
    assert len(calls) == 2
    assert StrategyParameters(**calls[-1]) == StrategyParameters(50, 2, 20, 24, .8, .05, 30, 35)
    window.strategy_vars["tank_capacity_l"].set("nan")
    window._configure_strategy()
    assert len(calls) == 2 and "参数无效" in window.action_var.get()
    window._configure_strategy(clear=True)
    assert calls[-1] is None and all(not var.get() for var in window.strategy_vars.values())
    assert not controller.questions and not controller.voice_calls


def _native_pit_observation_draft(native_window):
    from iracing_ai_engineer.live_app import AppState, _LiveAnalysis
    from iracing_ai_engineer.live_fuel import LiveFuelConfig
    from iracing_ai_engineer.live_pit_observation import pit_observation_draft
    from iracing_ai_engineer.live_strategy import StrategyParameters
    from iracing_ai_engineer.synthetic_runtime import synthetic_pit_visit_frames

    _, controller, window = native_window
    now = [1.]
    state = AppState(clock=lambda: now[0])
    analysis = _LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic-native-pit",
        tick_rate=20, car_count=3, generation=state.generation, allowed=lambda: True)
    try:
        for frame in synthetic_pit_visit_frames():
            now[0] = frame.captured_monotonic_s
            analysis.process((frame, "Race", now[0], 1_200_000))
        controller.value["telemetry"] = state.snapshot()
        controller.pit_observation_draft = lambda: pit_observation_draft(state.snapshot())
        controller.configure_strategy = lambda inputs, **kwargs: state.configure_strategy(
            StrategyParameters(**inputs) if inputs is not None else None, **kwargs)
        window._poll()
        assert not window.pit_draft_button.instate(["disabled"])
        assert "已记录一次进站" in window._vars["pit_observation"].get()
        window.strategy_vars["tank_capacity_l"].set("100")
        window.strategy_vars["pit_loss_low_s"].set("5")
        window.strategy_vars["pit_loss_high_s"].set("6")
        window.pit_draft_button.invoke()
        assert state.strategy_inputs()[0] is None
        assert window.strategy_vars["tank_capacity_l"].get() == "100"
        assert window.strategy_vars["pit_loss_low_s"].get() == "5"
        assert "尚未应用" in window.pit_draft_notice.get()
        assert window.strategy_vars["complete_pit_loss_low_s"].get() == "13.7"
        window._configure_strategy()
        assert state.strategy_inputs()[0].complete_pit_loss_high_s == 14.3
        state.invalidate_analysis(state.generation)
        controller.value["telemetry"] = state.snapshot()
        window._poll()
        assert window.pit_draft_button.instate(["disabled"])
        window._configure_strategy()
        assert "新鲜遥测" in window.action_var.get()
        window._configure_strategy(clear=True)
        assert window._pit_draft_binding is None
        assert all(not variable.get() for variable in window.strategy_vars.values())
        assert not controller.questions and not controller.voice_calls
    finally:
        analysis.close()
        state.connection("STOPPED")


def _native_capture_replay(native_window):
    from unittest.mock import patch

    _, controller, window = native_window
    calls = []
    controller.replay_capture = calls.append
    controller.cancel_capture_replay = lambda: calls.append("cancel")
    with patch("iracing_ai_engineer.desktop_window.filedialog.askopenfilename",
               return_value="C:/Users/racer/capture-synthetic.jsonl"):
        window._load_capture()
    window._cancel_capture()
    assert calls == [Path("C:/Users/racer/capture-synthetic.jsonl"), "cancel"]
    controller.value["capture_report"] = {"status": "RUNNING"}
    window._poll()
    assert "后台校验并重算" in window.capture_text.get("1.0", "end")
    controller.value["capture_report"] = {"status": "REJECTED", "reason": "CANCELLED"}
    window._poll()
    assert "没有接受部分结果" in window.capture_text.get("1.0", "end")
    assert not controller.questions and not controller.voice_calls


_NATIVE_SCENARIOS = {
    "settings": _native_settings, "question": _native_question,
    "close": _native_close, "validation": _native_validation,
    "voice_defaults": _native_voice_defaults_and_configuration,
    "voice_devices": _native_voice_device_refresh_and_binding,
    "voice_ptt": _native_voice_ptt_release_and_close,
    "voice_errors": _native_voice_errors_and_unavailable,
    "voice_pending": _native_voice_pending_binding_and_lost_snapshot,
    "voice_initialization": _native_voice_delayed_initial_settings,
    "trial_replay": _native_trial_replay,
    "strategy_configuration": _native_strategy_configuration,
    "pit_observation_draft": _native_pit_observation_draft,
    "capture_replay": _native_capture_replay,
}


@pytest.mark.parametrize("scenario", list(_NATIVE_SCENARIOS))
def test_native_window_scenario_in_isolated_process(scenario: str) -> None:
    _run_native_scenario(scenario)


if __name__ == "__main__":
    _execute_native_scenario(sys.argv[1])
