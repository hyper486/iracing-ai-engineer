from __future__ import annotations

import ctypes
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import iracing_ai_engineer.desktop_settings as settings_module
from iracing_ai_engineer.desktop_settings import DesktopSettings, SettingsError, SettingsStore

KEY = "synthetic-example-key"


@pytest.fixture
def crypto(monkeypatch):
    # Reversible mock only: no test labels this transformation as encryption.
    calls = []

    def protect(value):
        calls.append("protect")
        return bytes(byte ^ 0xA5 for byte in value)

    def unprotect(value):
        calls.append("unprotect")
        return bytes(byte ^ 0xA5 for byte in value)

    monkeypatch.setattr(settings_module, "_protect", protect)
    monkeypatch.setattr(settings_module, "_unprotect", unprotect)
    return calls


def _remember(**changes):
    return DesktopSettings(provider="deepseek", remember_key=True, **changes)


def test_default_store_is_private_and_read_only_until_explicit_save(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    store = SettingsStore()
    assert store.root == tmp_path / "iRacingAIEngineer" / "desktop"
    assert store.load() == DesktopSettings()
    assert store.load_key() is None
    assert not store.root.exists()
    with pytest.raises(AttributeError):
        store.root = tmp_path


def test_missing_or_relative_localappdata_never_falls_back_to_repo(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(SettingsError, match="^SETTINGS_ROOT_UNAVAILABLE$"):
        SettingsStore()
    monkeypatch.setenv("LOCALAPPDATA", ".")
    with pytest.raises(SettingsError, match="^SETTINGS_UNSAFE_PATH$"):
        SettingsStore()


def test_only_nonsecret_schema_is_saved_and_key_is_not_in_repr(tmp_path, crypto):
    store = SettingsStore(tmp_path / "desktop")
    store.save(_remember(recording_enabled=False), api_key=KEY)
    payload = (store.root / "settings.json").read_text()
    assert json.loads(payload) == {
        "contract_version": "desktop-settings-v1",
        "provider": "deepseek",
        "model": "deepseek-flash",
        "recording_enabled": False,
        "remember_key": True,
    }
    loaded = store.load()
    assert loaded.key_present is True
    assert "key_present" not in payload
    assert KEY not in payload and KEY not in repr(loaded) and KEY not in repr(store)
    assert KEY.encode() not in (store.root / "deepseek-key.dpapi").read_bytes()
    assert crypto == ["protect"]  # Presence detection must not decrypt the key.
    assert store.load_key() == KEY
    assert crypto == ["protect", "unprotect"]


def test_omitted_key_retains_protected_bytes_without_reencrypting(tmp_path, crypto):
    store = SettingsStore(tmp_path)
    store.save(_remember(), api_key=KEY)
    protected = (tmp_path / "deepseek-key.dpapi").read_bytes()
    store.save(_remember(recording_enabled=False), api_key=None)
    assert (tmp_path / "deepseek-key.dpapi").read_bytes() == protected
    assert crypto == ["protect"]
    assert store.load_key() == KEY


def test_opt_out_and_clear_remove_only_exact_owned_secret(tmp_path, crypto):
    store = SettingsStore(tmp_path)
    store.save(_remember(), api_key=KEY)
    other = tmp_path / "unrelated.dpapi"
    other.write_bytes(b"unrelated-data")
    store.save(DesktopSettings(), api_key=KEY)  # In-memory caller key is not persisted.
    assert not (tmp_path / "deepseek-key.dpapi").exists()
    assert store.load_key() is None
    assert store.load().key_present is False
    assert other.read_bytes() == b"unrelated-data"
    assert crypto == ["protect"]
    store.clear_key()  # Idempotent when the owned file is missing.
    assert other.exists()


def test_clear_does_not_delete_settings_or_need_plaintext(tmp_path, crypto, monkeypatch):
    store = SettingsStore(tmp_path)
    store.save(_remember(), api_key=KEY)
    monkeypatch.setattr(settings_module, "_unprotect", lambda _: pytest.fail("must not decrypt"))
    store.clear_key()
    assert store.load().remember_key is True
    assert store.load().key_present is False
    assert store.load_key() is None
    assert (tmp_path / "settings.json").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "unknown"},
        {"provider": True},
        {"model": "has spaces"},
        {"model": "x" * 65},
        {"model": 1},
        {"recording_enabled": 1},
        {"remember_key": 0},
    ],
)
def test_invalid_settings_fail_before_creating_files(tmp_path, changes):
    store = SettingsStore(tmp_path / "desktop")
    with pytest.raises(SettingsError, match="^SETTINGS_INVALID$"):
        store.save(replace(DesktopSettings(), **changes))
    assert not store.root.exists()


@pytest.mark.parametrize("key", ["", "a b", "a\nb", "a\x00b", "中文", "x" * 513, 123, True])
def test_key_format_matches_client_and_is_never_normalized_or_logged(tmp_path, key):
    store = SettingsStore(tmp_path / "desktop")
    with pytest.raises(SettingsError) as error:
        store.save(_remember(), api_key=key)
    assert str(error.value) == "KEY_INVALID"
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert not store.root.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"null",
        b"\xff",
        b'{"provider":"off","provider":"deepseek"}',
        b'{"contract_version":"desktop-settings-v1","provider":"off","model":"deepseek-flash",'
        b'"recording_enabled":true,"remember_key":false,"api_key":"synthetic-example-key"}',
    ],
)
def test_invalid_json_or_unknown_settings_are_not_silently_overwritten(tmp_path, raw):
    path = tmp_path / "settings.json"
    path.write_bytes(raw)
    with pytest.raises(SettingsError, match="^SETTINGS_INVALID$"):
        SettingsStore(tmp_path).load()
    assert path.read_bytes() == raw


def test_encryption_failure_never_writes_plaintext_or_changes_previous_settings(
    tmp_path, monkeypatch
):
    store = SettingsStore(tmp_path)
    store.save(DesktopSettings())
    old = (tmp_path / "settings.json").read_bytes()

    def fail(_value):
        raise OSError("PRIVATE_ERROR_SENTINEL")

    monkeypatch.setattr(settings_module, "_protect", fail)
    with pytest.raises(SettingsError) as error:
        store.save(_remember(), api_key=KEY)
    assert str(error.value) == "SETTINGS_IO_FAILED"
    assert error.value.__context__ is None
    assert (tmp_path / "settings.json").read_bytes() == old
    assert not (tmp_path / "deepseek-key.dpapi").exists()


def test_atomic_replace_failure_retains_complete_prior_settings(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path)
    store.save(DesktopSettings())
    before = (tmp_path / "settings.json").read_bytes()

    def fail(*_args):
        raise OSError("PRIVATE_ERROR_SENTINEL")

    monkeypatch.setattr(settings_module.os, "replace", fail)
    with pytest.raises(SettingsError) as error:
        store.save(DesktopSettings(recording_enabled=False))
    assert str(error.value) == "SETTINGS_IO_FAILED"
    assert error.value.__context__ is None
    assert (tmp_path / "settings.json").read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_fsync_failure_does_not_replace_existing_settings(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path)
    store.save(DesktopSettings())
    before = (tmp_path / "settings.json").read_bytes()

    def fail(_descriptor):
        raise OSError("PRIVATE_ERROR_SENTINEL")

    monkeypatch.setattr(settings_module.os, "fsync", fail)
    with pytest.raises(SettingsError, match="^SETTINGS_IO_FAILED$"):
        store.save(DesktopSettings(recording_enabled=False))
    assert (tmp_path / "settings.json").read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_rejects_public_checkout_before_writing_even_for_frozen_app_path(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname='synthetic'\n")
    (checkout / "AGENTS.md").write_text("Synthetic public checkout fixture.\n")
    monkeypatch.setattr(settings_module, "__file__", str(tmp_path / "frozen" / "module.py"))
    with pytest.raises(SettingsError, match="^SETTINGS_UNSAFE_PATH$"):
        SettingsStore(checkout / "desktop")
    assert not (checkout / "desktop").exists()


def test_actual_public_source_checkout_is_not_a_settings_directory():
    checkout = Path(settings_module.__file__).resolve().parents[2]
    assert (checkout / "AGENTS.md").is_file()
    with pytest.raises(SettingsError, match="^SETTINGS_UNSAFE_PATH$"):
        SettingsStore(checkout / "desktop-settings-rejected")
    assert not (checkout / "desktop-settings-rejected").exists()


@pytest.mark.parametrize("name", ["settings.json", "deepseek-key.dpapi"])
def test_rejects_directory_in_place_of_owned_file(tmp_path, name):
    store = SettingsStore(tmp_path)
    store.save(DesktopSettings())
    owned = tmp_path / name
    if owned.exists():
        owned.unlink()
    owned.mkdir()
    with pytest.raises(SettingsError, match="^SETTINGS_UNSAFE_PATH$"):
        store.save(_remember())
    assert owned.is_dir()


@pytest.mark.parametrize("name", ["settings.json", "deepseek-key.dpapi"])
def test_oversized_owned_files_refused_without_overwrite(tmp_path, name):
    path = tmp_path / name
    payload = b"x" * (settings_module._MAX_KEY_BYTES + 1)
    path.write_bytes(payload)
    with pytest.raises(SettingsError, match="^SETTINGS_UNSAFE_PATH$"):
        SettingsStore(tmp_path).save(_remember())
    assert path.read_bytes() == payload


def test_linked_secret_cannot_delete_or_read_another_file(tmp_path):
    external = tmp_path / "external.bin"
    external.write_bytes(b"unrelated-data")
    store = SettingsStore(tmp_path / "desktop")
    store.save(_remember())
    os.link(external, store.root / "deepseek-key.dpapi")
    with pytest.raises(SettingsError, match="^SETTINGS_UNSAFE_PATH$"):
        store.clear_key()
    with pytest.raises(SettingsError, match="^SETTINGS_UNSAFE_PATH$"):
        store.load_key()
    assert external.read_bytes() == b"unrelated-data"


def test_symlink_file_is_not_followed_or_deleted(tmp_path):
    external = tmp_path / "external.bin"
    external.write_bytes(b"unrelated-data")
    store = SettingsStore(tmp_path / "desktop")
    store.save(_remember())
    try:
        (store.root / "deepseek-key.dpapi").symlink_to(external)
    except OSError:
        pytest.skip("Windows symlink creation is unavailable")
    with pytest.raises(SettingsError, match="^SETTINGS_UNSAFE_PATH$"):
        store.clear_key()
    assert external.read_bytes() == b"unrelated-data"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_junction_root_is_refused_before_any_write(tmp_path):
    import _winapi

    destination, alias = tmp_path / "destination", tmp_path / "alias"
    destination.mkdir()
    _winapi.CreateJunction(str(destination), str(alias))
    try:
        with pytest.raises(SettingsError, match="^SETTINGS_UNSAFE_PATH$"):
            SettingsStore(alias / "desktop")
        assert list(destination.iterdir()) == []
    finally:
        alias.rmdir()


def test_plaintext_or_wrong_purpose_file_has_no_decryption_fallback(tmp_path, crypto):
    store = SettingsStore(tmp_path)
    store.save(_remember())
    keyfile = tmp_path / "deepseek-key.dpapi"
    keyfile.write_text(KEY)
    with pytest.raises(SettingsError, match="^KEY_PROTECTION_FAILED$"):
        store.load_key()
    assert crypto == []
    keyfile.write_bytes(settings_module._KEY_MAGIC + b"wrong-purpose")
    with pytest.raises(SettingsError, match="^KEY_PROTECTION_FAILED$"):
        store.load_key()


def test_nonwindows_has_no_plaintext_crypto_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module, "_WINDOWS", False)
    store = SettingsStore(tmp_path)
    with pytest.raises(SettingsError, match="^KEY_PROTECTION_FAILED$"):
        store.save(_remember(), api_key=KEY)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("success", [True, False])
def test_dpapi_prototypes_flags_and_buffer_cleanup(monkeypatch, success):
    outputs, incoming, seen, freed = [], [], [], []

    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    def crypt(data, description, entropy, reserved, prompt, flags, output):
        assert description is entropy is reserved is prompt is None
        seen.append(flags)
        incoming.append(data._obj)
        result = (ctypes.c_ubyte * 4)(1, 2, 3, 4)
        outputs.append(result)
        output._obj.cbData = 4
        output._obj.pbData = result
        return int(success)

    def local_free(pointer):
        assert type(pointer) is ctypes.c_void_p
        assert bytes(outputs[-1]) == b"\x00" * 4
        freed.append(pointer.value)
        return None

    protect, unprotect, free = Function(crypt), Function(crypt), Function(local_free)

    def dll(name, **kwargs):
        assert kwargs == {"use_last_error": True, "winmode": 0x00000800}
        return (
            SimpleNamespace(CryptProtectData=protect, CryptUnprotectData=unprotect)
            if name == "Crypt32.dll"
            else SimpleNamespace(LocalFree=free)
        )

    monkeypatch.setattr(settings_module, "_WINDOWS", True)
    monkeypatch.setattr(settings_module.ctypes, "WinDLL", dll, raising=False)
    for decrypt in (False, True):
        if success:
            assert settings_module._dpapi(b"synthetic", decrypt=decrypt) == bytes([1, 2, 3, 4])
        else:
            with pytest.raises(SettingsError, match="^KEY_PROTECTION_FAILED$"):
                settings_module._dpapi(b"synthetic", decrypt=decrypt)
        assert ctypes.string_at(incoming[-1].pbData, incoming[-1].cbData) == b"\x00" * 9
    assert seen == [1, 1]  # UI forbidden; current user, never local-machine scope.
    assert len(freed) == 2
    assert free.argtypes == [ctypes.c_void_p] and free.restype is ctypes.c_void_p
    assert protect.restype is ctypes.c_int and unprotect.restype is ctypes.c_int
    assert unprotect.argtypes[1] == ctypes.POINTER(ctypes.c_wchar_p)


@pytest.mark.skipif(os.name != "nt", reason="Real Windows DPAPI, synthetic isolated secret only")
def test_real_current_user_dpapi_roundtrip_and_tamper_rejection(tmp_path):
    store = SettingsStore(tmp_path)
    store.save(_remember(), api_key=KEY)
    keyfile = tmp_path / "deepseek-key.dpapi"
    protected = keyfile.read_bytes()
    assert KEY.encode() not in protected
    assert store.load().key_present is True
    assert store.load_key() == KEY
    keyfile.write_bytes(protected[: len(settings_module._KEY_MAGIC)] + b"tampered-ciphertext")
    with pytest.raises(SettingsError, match="^KEY_PROTECTION_FAILED$") as error:
        store.load_key()
    assert error.value.__context__ is None and error.value.__cause__ is None
    store.clear_key()
    assert not keyfile.exists()
