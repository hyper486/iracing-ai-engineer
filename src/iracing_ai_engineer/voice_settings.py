"""Strict nonsecret, local-only preferences for the native VR voice channel."""

from __future__ import annotations

import copy
import json
import math
import re

from .desktop_settings import SettingsStore


def default_voice_settings() -> dict:
    return {
        "enabled": False, "input_device": "default", "output_device": "default",
        "volume": 0.7, "culture": "zh-CN", "voice": "",
        "binding": {"kind": "keyboard", "key": "F9"}, "auto_fuel": False,
    }


def validate_voice_settings(value: object) -> dict:
    if type(value) is not dict or set(value) != set(default_voice_settings()):
        raise ValueError("VOICE_SETTINGS_INVALID")
    if any(type(value[key]) is not bool for key in ("enabled", "auto_fuel")):
        raise ValueError("VOICE_SETTINGS_INVALID")
    volume = value["volume"]
    if type(volume) not in (int, float) or not 0 <= volume <= 1 or not math.isfinite(volume):
        raise ValueError("VOICE_SETTINGS_INVALID")
    for key in ("input_device", "output_device"):
        device = value[key]
        if type(device) is not str or (
            device != "default" and re.fullmatch(r"sd-[a-f0-9]{64}", device) is None
        ):
            raise ValueError("VOICE_SETTINGS_INVALID")
    for key, maximum in (("culture", 32), ("voice", 256)):
        text = value[key]
        if type(text) is not str or len(text) > maximum or any(ord(c) < 32 for c in text):
            raise ValueError("VOICE_SETTINGS_INVALID")
    if re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z]{2,8})?", value["culture"]) is None:
        raise ValueError("VOICE_SETTINGS_INVALID")
    binding = value["binding"]
    if type(binding) is not dict:
        raise ValueError("VOICE_SETTINGS_INVALID")
    if binding.get("kind") == "keyboard":
        if (set(binding) != {"kind", "key"} or type(binding["key"]) is not str
                or binding["key"] not in {
            "F8", "F9", "F10", "F11", "F12",
        }):
            raise ValueError("VOICE_SETTINGS_INVALID")
    elif binding.get("kind") == "joystick":
        if (set(binding) != {"kind", "guid", "name", "button"}
                or type(binding["button"]) is not int or not 0 <= binding["button"] < 256
                or type(binding["guid"]) is not str
                or re.fullmatch(r"[a-fA-F0-9]{32}", binding["guid"]) is None
                or type(binding["name"]) is not str or not 1 <= len(binding["name"]) <= 256
                or any(ord(c) < 32 for c in binding["name"])):
            raise ValueError("VOICE_SETTINGS_INVALID")
    else:
        raise ValueError("VOICE_SETTINGS_INVALID")
    return copy.deepcopy(value)


class VoiceSettingsStore:
    """Reuse the desktop store's guarded atomic I/O without touching its key files."""

    def __init__(self, store: SettingsStore):
        self._store = store

    def load(self) -> dict:
        return self._store.load_voice()

    def save(self, settings: dict) -> None:
        self._store.save_voice(settings)


def decode_voice_settings(payload: bytes | None) -> dict:
    if payload is None:
        return default_voice_settings()

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("VOICE_SETTINGS_INVALID")
            value[key] = item
        return value

    try:
        envelope = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
        if (type(envelope) is not dict or set(envelope) != {"version", "settings"}
                or envelope["version"] != "native-voice-v1"):
            raise ValueError("VOICE_SETTINGS_INVALID")
        return validate_voice_settings(envelope["settings"])
    except Exception:
        raise ValueError("VOICE_SETTINGS_INVALID") from None
