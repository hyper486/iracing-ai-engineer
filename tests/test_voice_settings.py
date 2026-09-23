"""Voice preferences are separate from existing credentials and model settings."""

import copy
import json

import pytest

from iracing_ai_engineer.desktop_settings import DesktopSettings, SettingsStore
from iracing_ai_engineer.voice_settings import (
    VoiceSettingsStore,
    decode_voice_settings,
    default_voice_settings,
    validate_voice_settings,
)


def test_defaults_follow_system_and_do_not_enable_microphone(tmp_path):
    store = SettingsStore(tmp_path / "desktop")
    voice = VoiceSettingsStore(store)
    assert voice.load() == default_voice_settings()
    assert not store.root.exists()
    assert not voice.load()["enabled"]
    assert voice.load()["input_device"] == voice.load()["output_device"] == "default"


def test_voice_save_does_not_modify_existing_model_or_encrypted_key(tmp_path):
    store = SettingsStore(tmp_path)
    store.save(DesktopSettings(provider="deepseek"))
    before = (tmp_path / "settings.json").read_bytes()
    key_path = tmp_path / "deepseek-key.dpapi"
    key_path.write_bytes(b"SYNTHETIC_UNREAD_ENCRYPTED_PLACEHOLDER")
    voice = VoiceSettingsStore(store)
    value = {**default_voice_settings(), "enabled": True, "input_device": "sd-" + "a" * 64}
    voice.save(value)
    assert voice.load() == value
    assert (tmp_path / "settings.json").read_bytes() == before
    assert key_path.read_bytes() == b"SYNTHETIC_UNREAD_ENCRYPTED_PLACEHOLDER"


@pytest.mark.parametrize("key,value", [
    ("enabled", 1), ("auto_fuel", "yes"), ("input_device", "device-index-3"),
    ("output_device", "C:/private/example"), ("volume", float("nan")),
    ("volume", 2), ("volume", True), ("culture", "$(bad)"), ("voice", "x\ny"),
    ("voice", "x" * 257), ("binding", {"kind": "keyboard", "key": "A"}),
    ("binding", {"kind": "keyboard", "key": []}),
    ("binding", {"kind": "joystick", "guid": "bad", "name": "Wheel", "button": 0}),
    ("binding", {"kind": "joystick", "guid": "a" * 32, "name": "Wheel", "button": True}),
])
def test_invalid_voice_settings_fail_closed(key, value):
    with pytest.raises(ValueError, match="VOICE_SETTINGS_INVALID"):
        validate_voice_settings({**default_voice_settings(), key: value})


def test_unknown_key_and_duplicate_json_rejected():
    with pytest.raises(ValueError):
        validate_voice_settings({**default_voice_settings(), "secret": "not-admitted"})
    with pytest.raises(ValueError):
        decode_voice_settings(b'{"version":"native-voice-v1","version":"duplicate"}')


def test_validation_returns_an_independent_binding_copy():
    original = default_voice_settings()
    value = validate_voice_settings(original)
    value["binding"]["key"] = "F8"
    assert original == default_voice_settings()
    original["binding"] = {"kind": "joystick", "guid": "a" * 32, "name": "Wheel", "button": 2}
    assert validate_voice_settings(original) == copy.deepcopy(original)


def test_v1_migration_preserves_all_preferences_without_enabling_new_audio():
    previous = {**default_voice_settings(), "enabled": True, "volume": 0.4}
    previous.pop("spotter_enabled")
    migrated = decode_voice_settings(json.dumps({
        "version": "native-voice-v1", "settings": previous,
    }).encode())
    assert migrated == {**previous, "spotter_enabled": False}


@pytest.mark.parametrize("version,spotter", [
    ("native-voice-v1", True), ("native-voice-v2", "true"), ("native-voice-v2", 1),
])
def test_new_preference_cannot_be_smuggled_through_legacy_or_invalid_values(version, spotter):
    with pytest.raises(ValueError):
        decode_voice_settings(json.dumps({
            "version": version,
            "settings": {**default_voice_settings(), "spotter_enabled": spotter},
        }).encode())
