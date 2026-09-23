"""Private desktop preferences and opt-in Windows current-user DPAPI storage.

DPAPI is not protection from another process running as the same Windows user.
Native buffers are cleared best-effort; Python strings are not zeroizable.
Files are individually atomic, not a transaction spanning both settings/key.
No credential discovery, plaintext fallback or environment persistence occurs.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from uuid import uuid4

_SETTINGS_FILE = "settings.json"
_KEY_FILE = "deepseek-key.dpapi"
_MAX_SETTINGS_BYTES = 4096
_MAX_KEY_BYTES = 16 * 1024
_KEY_MAGIC = b"IRACING-AIE-DPAPI-v1\x00"
_PLAINTEXT_MAGIC = b"iracing-ai-engineer/desktop/deepseek/v1\x00"
_WINDOWS = os.name == "nt"
_ERROR_CODES = frozenset(
    {
        "SETTINGS_INVALID",
        "SETTINGS_IO_FAILED",
        "SETTINGS_ROOT_UNAVAILABLE",
        "SETTINGS_UNSAFE_PATH",
        "KEY_INVALID",
        "KEY_PROTECTION_FAILED",
    }
)


class SettingsError(ValueError):
    """Only a fixed code, without paths, keys or an original exception chain."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _ERROR_CODES else "SETTINGS_IO_FAILED"
        super().__init__(self.code)


def _safe_errors(default_code: str):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            code = None
            try:
                return function(*args, **kwargs)
            except SettingsError as error:
                code = error.code
            except Exception:
                code = default_code
            # Outside the except block, so even __context__ has no private text.
            raise SettingsError(code)

        return call

    return decorate


@dataclass(frozen=True)
class DesktopSettings:
    provider: str = "off"
    model: str = "deepseek-flash"
    recording_enabled: bool = True
    remember_key: bool = False
    key_present: bool = field(default=False, init=False, compare=False)


def _validate_settings(settings: object) -> DesktopSettings:
    if type(settings) is not DesktopSettings or (
        type(settings.provider) is not str
        or settings.provider not in ("off", "deepseek")
        or type(settings.model) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", settings.model) is None
        or type(settings.recording_enabled) is not bool
        or type(settings.remember_key) is not bool
    ):
        raise SettingsError("SETTINGS_INVALID")
    return settings


def _key_bytes(key: object) -> bytes:
    if type(key) is not str or re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", key) is None:
        raise SettingsError("KEY_INVALID")
    return key.encode("ascii")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


@_safe_errors("KEY_PROTECTION_FAILED")
def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if not _WINDOWS or type(data) is not bytes or not 0 < len(data) <= _MAX_KEY_BYTES:
        raise SettingsError("KEY_PROTECTION_FAILED")
    # Load only system DLLs, not a same-named library beside the desktop EXE.
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True, winmode=0x00000800)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True, winmode=0x00000800)
    protect, unprotect, free = (
        crypt32.CryptProtectData,
        crypt32.CryptUnprotectData,
        kernel32.LocalFree,
    )
    blob_pointer = ctypes.POINTER(_DataBlob)
    protect.argtypes = [
        blob_pointer,
        ctypes.c_wchar_p,
        blob_pointer,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        blob_pointer,
    ]
    unprotect.argtypes = [
        blob_pointer,
        ctypes.POINTER(ctypes.c_wchar_p),
        blob_pointer,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        blob_pointer,
    ]
    protect.restype = unprotect.restype = ctypes.c_int
    # Explicit pointer-sized signatures are required on 64-bit Windows.
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    incoming = _DataBlob(len(data), buffer)
    outgoing = _DataBlob()
    try:
        function = unprotect if decrypt else protect
        # UI_FORBIDDEN only: never CRYPTPROTECT_LOCAL_MACHINE. Null description
        # on unprotect also avoids allocating a second native output buffer.
        if not function(
            ctypes.byref(incoming), None, None, None, None, 0x1, ctypes.byref(outgoing)
        ):
            raise SettingsError("KEY_PROTECTION_FAILED")
        if not outgoing.pbData or not 0 < outgoing.cbData <= _MAX_KEY_BYTES:
            raise SettingsError("KEY_PROTECTION_FAILED")
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if outgoing.pbData:
            if 0 < outgoing.cbData <= _MAX_KEY_BYTES:
                ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
            if free(ctypes.cast(outgoing.pbData, ctypes.c_void_p)):
                raise SettingsError("KEY_PROTECTION_FAILED")


def _protect(data: bytes) -> bytes:
    return _dpapi(data, decrypt=False)


def _unprotect(data: bytes) -> bytes:
    return _dpapi(data, decrypt=True)


def _reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    # Windows lstat/fstat ctime semantics can differ; compare ctime only between
    # repeated calls to the same observation API below.
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise SettingsError("SETTINGS_INVALID")
        value[key] = item
    return value


class SettingsStore:
    """Own desktop settings, nonsecret voice.json and one DPAPI key file.

    Disabling remembering deletes only that exact encrypted file. Existing
    symlinks, junctions, hardlinks, oversized or nonregular files are refused.
    Atomic file replacement does not defend against a hostile same-user process.
    """

    @_safe_errors("SETTINGS_ROOT_UNAVAILABLE")
    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            appdata = os.environ.get("LOCALAPPDATA")
            if not appdata:
                raise SettingsError("SETTINGS_ROOT_UNAVAILABLE")
            root = Path(appdata) / "iRacingAIEngineer" / "desktop"
        original = Path(root)
        if not original.is_absolute():
            raise SettingsError("SETTINGS_UNSAFE_PATH")
        self._root = original
        self._check_root()
        self._root = original.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _check_root(self, *, create: bool = False) -> None:
        root = self._root
        for parent in (root, *root.parents):
            try:
                metadata = parent.lstat()
            except FileNotFoundError:
                continue
            if _reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise SettingsError("SETTINGS_UNSAFE_PATH")
            # Also works when a packaged executable has no source checkout.
            if (parent / "pyproject.toml").is_file() and (parent / "AGENTS.md").is_file():
                raise SettingsError("SETTINGS_UNSAFE_PATH")
        resolved = root.resolve()
        for parent in Path(__file__).resolve().parents:
            if (parent / "pyproject.toml").is_file() and (parent / "AGENTS.md").is_file():
                if resolved.is_relative_to(parent):
                    raise SettingsError("SETTINGS_UNSAFE_PATH")
                break
        if create:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._check_root()

    def _metadata(self, name: str, maximum: int) -> os.stat_result | None:
        self._check_root()
        try:
            metadata = (self.root / name).lstat()
        except FileNotFoundError:
            return None
        if (
            _reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
        ):
            raise SettingsError("SETTINGS_UNSAFE_PATH")
        return metadata

    def _read(self, name: str, maximum: int) -> bytes | None:
        before = self._metadata(name, maximum)
        if before is None:
            return None
        path = self.root / name
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _identity(before) != _identity(opened) or opened.st_nlink != 1:
                raise SettingsError("SETTINGS_UNSAFE_PATH")
            payload = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
            path_after = self._metadata(name, maximum)
            if (
                len(payload) > maximum
                or path_after is None
                or _identity(opened) != _identity(after)
                or opened.st_ctime_ns != after.st_ctime_ns
                or _identity(before) != _identity(path_after)
                or before.st_ctime_ns != path_after.st_ctime_ns
            ):
                raise SettingsError("SETTINGS_UNSAFE_PATH")
        return payload

    def _atomic_write(self, name: str, payload: bytes, maximum: int) -> None:
        if len(payload) > maximum:
            raise SettingsError("SETTINGS_INVALID")
        self._check_root(create=True)
        before = self._metadata(name, maximum)
        target = self.root / name
        temporary = self.root / f".{name}.{uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
        owned = None
        try:
            with os.fdopen(descriptor, "wb") as handle:
                owned = os.fstat(handle.fileno())
                if handle.write(payload) != len(payload):
                    raise OSError("short write")
                handle.flush()
                os.fsync(handle.fileno())
                owned = os.fstat(handle.fileno())
            current = self._metadata(name, maximum)
            if (before is None) != (current is None) or (
                before is not None
                and current is not None
                and (
                    _identity(before) != _identity(current)
                    or before.st_ctime_ns != current.st_ctime_ns
                )
            ):
                raise SettingsError("SETTINGS_UNSAFE_PATH")
            if _identity(temporary.lstat()) != _identity(owned):
                raise SettingsError("SETTINGS_UNSAFE_PATH")
            os.replace(temporary, target)
        finally:
            # Never delete a replaced pathname or another actor's file.
            with suppress(OSError):
                current = temporary.lstat()
                if owned is not None and (
                    current.st_dev == owned.st_dev and current.st_ino == owned.st_ino
                ):
                    temporary.unlink()

    @_safe_errors("SETTINGS_IO_FAILED")
    def load(self) -> DesktopSettings:
        payload = self._read(_SETTINGS_FILE, _MAX_SETTINGS_BYTES)
        if payload is None:
            return DesktopSettings()
        try:
            value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
            if (
                type(value) is not dict
                or set(value)
                != {"contract_version", "provider", "model", "recording_enabled", "remember_key"}
                or value["contract_version"] != "desktop-settings-v1"
            ):
                raise SettingsError("SETTINGS_INVALID")
            settings = _validate_settings(
                DesktopSettings(
                    **{
                        key: value[key]
                        for key in ("provider", "model", "recording_enabled", "remember_key")
                    }
                )
            )
        except Exception:
            raise SettingsError("SETTINGS_INVALID") from None
        present = settings.remember_key and self._metadata(_KEY_FILE, _MAX_KEY_BYTES) is not None
        object.__setattr__(settings, "key_present", present)
        return settings

    @_safe_errors("SETTINGS_IO_FAILED")
    def save(self, settings: DesktopSettings, api_key: str | None = None) -> None:
        settings = _validate_settings(settings)
        key = None if api_key is None else _key_bytes(api_key)
        self._check_root()
        if settings.remember_key and key is not None:
            protected = _protect(_PLAINTEXT_MAGIC + key)
            if type(protected) is not bytes or not protected:
                raise SettingsError("KEY_PROTECTION_FAILED")
            self._atomic_write(_KEY_FILE, _KEY_MAGIC + protected, _MAX_KEY_BYTES)
        elif settings.remember_key:
            self._metadata(_KEY_FILE, _MAX_KEY_BYTES)  # Retain only an ordinary owned file.
        payload = json.dumps(
            {
                "contract_version": "desktop-settings-v1",
                "provider": settings.provider,
                "model": settings.model,
                "recording_enabled": settings.recording_enabled,
                "remember_key": settings.remember_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._atomic_write(_SETTINGS_FILE, payload, _MAX_SETTINGS_BYTES)
        if not settings.remember_key:
            self.clear_key()

    @_safe_errors("SETTINGS_IO_FAILED")
    def load_key(self) -> str | None:
        if not self.load().remember_key:
            return None
        payload = self._read(_KEY_FILE, _MAX_KEY_BYTES)
        if payload is None:
            return None
        if not payload.startswith(_KEY_MAGIC):
            raise SettingsError("KEY_PROTECTION_FAILED")
        plain = _unprotect(payload[len(_KEY_MAGIC) :])
        if type(plain) is not bytes or not plain.startswith(_PLAINTEXT_MAGIC):
            raise SettingsError("KEY_PROTECTION_FAILED")
        try:
            key = plain[len(_PLAINTEXT_MAGIC) :].decode("ascii")
        except UnicodeError:
            raise SettingsError("KEY_INVALID") from None
        _key_bytes(key)
        return key

    @_safe_errors("SETTINGS_IO_FAILED")
    def clear_key(self) -> None:
        metadata = self._metadata(_KEY_FILE, _MAX_KEY_BYTES)
        if metadata is not None:
            (self.root / _KEY_FILE).unlink()

    @_safe_errors("SETTINGS_IO_FAILED")
    def load_voice(self) -> dict:
        from .voice_settings import decode_voice_settings

        return decode_voice_settings(self._read("voice.json", _MAX_SETTINGS_BYTES))

    @_safe_errors("SETTINGS_IO_FAILED")
    def save_voice(self, settings: dict) -> None:
        from .voice_settings import validate_voice_settings

        value = validate_voice_settings(settings)
        payload = json.dumps(
            {"version": "native-voice-v2", "settings": value},
            ensure_ascii=True, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")
        self._atomic_write("voice.json", payload, _MAX_SETTINGS_BYTES)


__all__ = ["DesktopSettings", "SettingsError", "SettingsStore"]
