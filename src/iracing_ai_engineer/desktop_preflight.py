"""Read-only first-trial setup hints, never a hardware/driver acceptance gate.

Only applied preference enums are projected. No device access, credentials,
cloud calls, persistence or readiness inference from a device catalogue.
"""

from __future__ import annotations

import math
from collections.abc import Mapping


def _map(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def trial_setup(snapshot: object) -> dict[str, str]:
    value = _map(snapshot)
    voice = _map(value.get("voice"))
    settings = _map(voice.get("settings"))
    spotter = _map(voice.get("spotter"))

    def switch(name):
        item = settings.get(name)
        return "已开启" if item is True else "已关闭" if item is False else "尚未确认"

    def device(name, kind):
        selected = settings.get(name)
        if selected == "default":
            return f"{kind}：系统默认；每次收音/播放时重新解析。"
        if type(selected) is str and selected.startswith("sd-"):
            return f"{kind}：指定设备；不可用时不会自动换成其他设备。"
        return f"{kind}：设置尚未确认。"

    binding = _map(settings.get("binding"))
    key = binding.get("key")
    if binding.get("kind") == "keyboard" and key in ("F8", "F9", "F10", "F11", "F12"):
        ptt = f"按住 {key} 说话，松开后处理。"
    elif binding.get("kind") == "joystick":
        ptt = "按住已绑定的方向盘按钮说话，松开后处理。"
    else:
        ptt = "PTT 绑定尚未确认。"
    ptt += "先停车测试‘还有多少油’，核对最近识别文字和耳机回答。"

    volume = settings.get("volume")
    try:
        volume_ok = type(volume) in (int, float) and math.isfinite(volume) and 0 < volume <= 1
    except OverflowError:
        volume_ok = False
    if value.get("lifecycle") != "RUNNING":
        action = "本地服务未运行；等待启动完成，若已停止请重新打开应用。"
    elif not settings or voice.get("status") == "STARTING":
        action = "语音设置尚未载入；等候启动，若持续不变请检查“语音与 VR”。"
    elif settings.get("enabled") is not True or settings.get("spotter_enabled") is not True:
        action = "到“语音与 VR”分别开启按住说话和近车语音，再点“应用语音设置”。"
    elif not volume_ok:
        action = "音量为零或设置无效；在“语音与 VR”恢复音量并应用。"
    elif voice.get("binding_active") is True:
        action = "PTT 正在绑定；完成选择或取消绑定后再测试。"
    elif voice.get("status") in ("ERROR", "UNAVAILABLE", "OFF", "DISABLED", "CLOSED",
                                 "STOPPING") or spotter.get("status") in (
        "ERROR", "PAUSED", "OFF", "CLOSED", "STOPPING",
    ):
        action = "语音模块未就绪；查看“语音与 VR”的具体提示，检查设备后重新应用。"
    elif spotter.get("status") == "PREPARING":
        action = "近车短语音正在准备；完成后先停车试听和测试 PTT。"
    else:
        action = "先停车戴耳机试听并测试 PTT；再在本人驾驶中分别观察下方三个功能。"
    return {
        "action": action,
        "voice": f"已应用设置：按住说话{switch('enabled')} · 近车语音{switch('spotter_enabled')}。"
                 "修改勾选但未应用不会生效。",
        "devices": device("input_device", "麦克风") + "\n" + device("output_device", "耳机"),
        "ptt": ptt,
    }
