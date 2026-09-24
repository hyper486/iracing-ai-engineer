"""First-trial hints report applied preferences, never inferred acceptance."""

import copy

import pytest

from iracing_ai_engineer.desktop_preflight import trial_setup
from iracing_ai_engineer.voice_settings import default_voice_settings


def snapshot():
    return {"lifecycle": "RUNNING", "voice": {
        "settings": {**default_voice_settings(), "enabled": True, "spotter_enabled": True},
        "status": "READY", "spotter": {"status": "WAIT_DATA"},
    }}


def test_initial_applied_preferences_and_no_mutation_or_private_fields():
    value = snapshot()
    value["voice"]["settings"].update(enabled=False, spotter_enabled=False)
    value["settings"] = {"key": "PRIVATE_KEY"}
    value["voice"].update(notice="PRIVATE_NOTICE", transcript="PRIVATE_TRANSCRIPT")
    original = copy.deepcopy(value)
    result = trial_setup(value)
    assert "按住说话已关闭" in result["voice"] and "近车语音已关闭" in result["voice"]
    assert "分别开启" in result["action"] and "应用" in result["action"]
    assert "系统默认" in result["devices"] and "F9" in result["ptt"]
    assert "PRIVATE" not in str(result)
    assert value == original


@pytest.mark.parametrize("value", [None, [], {}, {"voice": None}, {"voice": []}])
def test_unavailable_is_not_configured_or_accepted(value):
    result = trial_setup(value)
    assert "未运行" in result["action"]
    assert "尚未确认" in result["voice"] and "尚未确认" in result["devices"]


@pytest.mark.parametrize("item", ["enabled", "spotter_enabled"])
@pytest.mark.parametrize("setting", [None, False, "true", 1])
def test_each_switch_must_be_explicitly_enabled(item, setting):
    value = snapshot()
    value["voice"]["settings"][item] = setting
    assert "分别开启" in trial_setup(value)["action"]


@pytest.mark.parametrize("volume", [0, -1, True, None, "1", float("nan"), float("inf"), 10**999])
def test_muted_and_invalid_volumes_have_an_action(volume):
    value = snapshot()
    value["voice"]["settings"]["volume"] = volume
    assert "音量为零或设置无效" in trial_setup(value)["action"]


@pytest.mark.parametrize("owner,state,expected", [
    ("voice", "STARTING", "尚未载入"), ("voice", "ERROR", "模块未就绪"),
    ("voice", "OFF", "模块未就绪"), ("voice", "CLOSED", "模块未就绪"),
    ("spotter", "ERROR", "模块未就绪"), ("spotter", "PAUSED", "模块未就绪"),
    ("spotter", "PREPARING", "正在准备"),
])
def test_module_states_do_not_claim_hardware_readiness(owner, state, expected):
    value = snapshot()
    target = value["voice"] if owner == "voice" else value["voice"]["spotter"]
    target["status"] = state
    assert expected in trial_setup(value)["action"]


def test_binding_and_device_preferences_without_exposing_names_or_claiming_availability():
    value = snapshot()
    value["voice"]["settings"].update(
        input_device="sd-" + "1" * 64, output_device="sd-" + "2" * 64,
        binding={"kind": "joystick", "name": "PRIVATE_NAME"},
    )
    value["voice"]["binding_active"] = True
    result = trial_setup(value)
    assert "正在绑定" in result["action"] and "方向盘" in result["ptt"]
    assert result["devices"].count("不可用时不会自动换") == 2
    assert "PRIVATE" not in str(result) and "sd-" not in str(result)
    value["voice"]["settings"]["binding"] = {"kind": "keyboard", "key": "PRIVATE_KEY"}
    assert "绑定尚未确认" in trial_setup(value)["ptt"]


def test_ready_software_and_playback_event_still_require_human_trial():
    value = snapshot()
    value["voice"]["spotter"] = {"status": "READY",
                                 "output_status": "STARTED_NOT_HEARING_CONFIRMED"}
    value["telemetry"] = {"fuel": {"status": "ERROR"}, "spotter": {"status": "READY"}}
    result = trial_setup(value)
    assert "停车戴耳机试听" in result["action"]
    assert "本人驾驶" in result["action"]
    assert "通过" not in str(result) and "已听到" not in str(result)
