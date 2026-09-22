"""Optional raw SDK transport over an owned, read-only Crew Chief reader.

The reader receives only ``snapshot`` on stdin. It is not the installed Crew
Chief application, and this adapter has no simulator or pit-control command.
Raw session metadata stays in memory until the collector applies its existing
privacy filter. Child output and exceptions are never reflected in errors.
"""

from __future__ import annotations

import base64
import copy
import json
import math
import platform
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .collector import CollectorConsistencyError, validate_variable_descriptors
from .runtime_clock import monotonic_now
from .sdk_probe import (
    SDK_TYPE_SIZES,
    SUPPORTED_PYIRSDK_VERSION,
    ConnectionMeta,
    RawSdkFrame,
    SdkProbeConsistencyError,
    SdkProbeUnavailable,
    VariableDescriptor,
    _parse_session_info_snapshot,
)

PROTOCOL_VERSION = "crewchief-readonly-v1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SESSION_INFO_BYTES = 8 * 1024 * 1024
READ_TIMEOUT_S = 2.0
_OK_KEYS = {
    "protocol", "status", "connection", "buffer_tick", "session_info_update",
    "session_info_b64", "descriptors", "values", "read_errors",
}
_CONNECTION_KEYS = {
    "header_version", "raw_header_status", "tick_rate_hz", "variable_count",
    "buffer_count", "buffer_len",
}
_DESCRIPTOR_KEYS = {
    "name", "type_code", "dtype", "offset", "count", "count_as_time", "unit",
    "description",
}
_DESCRIPTOR_FIELD_TYPES = (
    ("name", str), ("type_code", int), ("dtype", str), ("offset", int),
    ("count", int), ("count_as_time", bool), ("unit", str), ("description", str),
)


class _ReaderUnavailable(SdkProbeUnavailable):
    """An IPC failure is terminal, unlike a not-yet-connected simulator."""


def _invalid(code: str = "CREWCHIEF_PROTOCOL_INVALID") -> SdkProbeConsistencyError:
    return SdkProbeConsistencyError(code)


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _invalid()


def _validate_session_tree(value: object) -> None:
    # SafeLoader forbids Python objects but can still create recursive aliases.
    # Bound traversal, nesting and scalar types before exposing it to consumers.
    stack = [(value, 0, frozenset())]
    visited = 0
    while stack:
        current, depth, ancestors = stack.pop()
        visited += 1
        if visited > 100_000 or depth > 32:
            raise _invalid("CREWCHIEF_SESSION_INFO_INVALID")
        if type(current) in {dict, list}:
            identity = id(current)
            if identity in ancestors:
                raise _invalid("CREWCHIEF_SESSION_INFO_INVALID")
            ancestry = ancestors | {identity}
            if type(current) is dict:
                if any(type(key) is not str for key in current):
                    raise _invalid("CREWCHIEF_SESSION_INFO_INVALID")
                children = current.values()
            else:
                children = current
            stack.extend((child, depth + 1, ancestry) for child in children)
        elif (
            current is not None and type(current) not in {str, int, bool, float}
            or type(current) is float and not math.isfinite(current)
        ):
            raise _invalid("CREWCHIEF_SESSION_INFO_INVALID")


def _session_info(encoded: object) -> Mapping[str, object]:
    if type(encoded) is not str or len(encoded) > 4 * ((MAX_SESSION_INFO_BYTES + 2) // 3):
        raise _invalid("CREWCHIEF_SESSION_INFO_INVALID")
    try:
        import irsdk

        if irsdk.VERSION != SUPPORTED_PYIRSDK_VERSION:
            raise _invalid("CREWCHIEF_SESSION_PARSER_VERSION_UNSUPPORTED")
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > MAX_SESSION_INFO_BYTES:
            raise _invalid("CREWCHIEF_SESSION_INFO_INVALID")
        parsed = _parse_session_info_snapshot(raw, irsdk)
        if not isinstance(parsed, Mapping):
            raise _invalid("CREWCHIEF_SESSION_INFO_INVALID")
        _validate_session_tree(parsed)
        return parsed
    except Exception:
        raise _invalid("CREWCHIEF_SESSION_INFO_INVALID") from None


def _scalar(value: object, type_code: int) -> bool | int | float:
    if type_code == 1:
        if type(value) is not bool:
            raise _invalid("CREWCHIEF_VALUE_INVALID")
        return value
    if type_code == 2:
        return _integer(value, -(2**31), 2**31 - 1)
    if type_code == 3:
        return _integer(value, 0, 2**32 - 1)
    if type(value) not in {int, float}:
        raise _invalid("CREWCHIEF_VALUE_INVALID")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise _invalid("CREWCHIEF_VALUE_INVALID") from None
    if not math.isfinite(number) or type_code == 4 and abs(number) > 3.4028234663852886e38:
        raise _invalid("CREWCHIEF_VALUE_INVALID")
    return number


def _value(value: object, descriptor: VariableDescriptor) -> object:
    if descriptor.type_code == 0:
        if type(value) is not str:
            raise _invalid("CREWCHIEF_VALUE_INVALID")
        try:
            if len(value.encode("latin-1")) > descriptor.count:
                raise _invalid("CREWCHIEF_VALUE_INVALID")
        except UnicodeError:
            raise _invalid("CREWCHIEF_VALUE_INVALID") from None
        return value
    if descriptor.count == 1:
        return _scalar(value, descriptor.type_code)
    if type(value) is not list or len(value) != descriptor.count:
        raise _invalid("CREWCHIEF_VALUE_INVALID")
    return tuple(_scalar(item, descriptor.type_code) for item in value)


@dataclass(frozen=True)
class _Snapshot:
    connection: ConnectionMeta
    descriptors: tuple[VariableDescriptor, ...]
    frame: RawSdkFrame
    session_info: Mapping[str, object]


@dataclass(slots=True)
class _MetadataCache:
    """One validated schema and one exact SessionInfo payload per transport."""

    schema_key: tuple[tuple[object, ...], ...] | None = None
    descriptors: tuple[VariableDescriptor, ...] = ()
    session_b64: str | None = None
    session_info: Mapping[str, object] | None = None

    def clear(self) -> None:
        self.schema_key = None
        self.descriptors = ()
        self.session_b64 = None
        self.session_info = None


def _descriptor_key(rows: list[object]) -> tuple[tuple[object, ...], ...]:
    # Check exact types *before* comparing keys: True == 1 == 1.0 must never
    # turn a malformed incoming descriptor into a previously validated schema.
    key: list[tuple[object, ...]] = []
    for row in rows:
        if type(row) is not dict or set(row) != _DESCRIPTOR_KEYS:
            raise _invalid("CREWCHIEF_SCHEMA_INVALID")
        values: list[object] = []
        for name, expected_type in _DESCRIPTOR_FIELD_TYPES:
            value = row[name]
            if type(value) is not expected_type:
                raise _invalid("CREWCHIEF_SCHEMA_INVALID")
            values.append(value)
        key.append(tuple(values))
    return tuple(key)


def _decode_snapshot(
    line: bytes, *, captured_monotonic_s: float, _cache: _MetadataCache | None = None,
) -> _Snapshot:
    # Standalone protocol callers remain stateless. Only a transport supplies
    # its own bounded cache; no untrusted packet can populate a global cache.
    if type(line) is not bytes or len(line) > MAX_RESPONSE_BYTES or not line.endswith(b"\n"):
        raise _invalid()
    try:
        packet = json.loads(
            line.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise _invalid() from None
    if type(packet) is not dict or packet.get("protocol") != PROTOCOL_VERSION:
        raise _invalid()
    status = packet.get("status")
    if type(status) is not str:
        raise _invalid()
    if status in {"unavailable", "inconsistent"}:
        code = packet.get("error_code")
        if (
            set(packet) != {"protocol", "status", "error_code"}
            or type(code) is not str
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None
        ):
            raise _invalid()
        if status == "unavailable":
            raise SdkProbeUnavailable("CREWCHIEF_SDK_UNAVAILABLE")
        raise _invalid("CREWCHIEF_SDK_INCONSISTENT")
    if status != "ok" or set(packet) != _OK_KEYS:
        raise _invalid()
    connection = packet["connection"]
    if type(connection) is not dict or set(connection) != _CONNECTION_KEYS:
        raise _invalid()
    header = _integer(connection["header_version"], 2, 2)
    header_status = _integer(connection["raw_header_status"], 0, 2**31 - 1)
    if not header_status & 1:
        raise SdkProbeUnavailable("CREWCHIEF_SDK_DISCONNECTED")
    meta = ConnectionMeta(
        startup_ok=True, initialized=True, connected=True,
        header_version=header, raw_header_status=header_status,
        tick_rate_hz=_integer(connection["tick_rate_hz"], 1, 360),
        variable_count=_integer(connection["variable_count"], 1, 4096),
        buffer_count=_integer(connection["buffer_count"], 1, 4),
        buffer_len=_integer(connection["buffer_len"], 1, 4 * 1024 * 1024),
    )
    raw_descriptors = packet["descriptors"]
    if type(raw_descriptors) is not list or len(raw_descriptors) != meta.variable_count:
        raise _invalid("CREWCHIEF_SCHEMA_INVALID")
    schema_key = _descriptor_key(raw_descriptors)
    if _cache is not None and _cache.schema_key == schema_key:
        schema = _cache.descriptors
    else:
        schema = tuple(VariableDescriptor(**row) for row in raw_descriptors)
        try:
            validate_variable_descriptors(schema)
        except (CollectorConsistencyError, TypeError, ValueError, UnicodeError):
            raise _invalid("CREWCHIEF_SCHEMA_INVALID") from None
    if any(
        item.offset + SDK_TYPE_SIZES[item.type_code] * item.count > meta.buffer_len
        or item.count > 4096
        for item in schema
    ):
        raise _invalid("CREWCHIEF_SCHEMA_INVALID")
    names = {item.name for item in schema}
    values, errors = packet["values"], packet["read_errors"]
    if (
        type(values) is not dict or not set(values) <= names
        or type(errors) is not list or any(type(item) is not str for item in errors)
        or len(errors) != len(set(errors)) or not set(errors) <= names
    ):
        raise _invalid("CREWCHIEF_VALUE_INVALID")
    decoded: dict[str, Any] = {}
    for descriptor in schema:
        name = descriptor.name
        if name in errors:
            if values.get(name) is not None:
                raise _invalid("CREWCHIEF_VALUE_INVALID")
            continue
        if name not in values:
            raise _invalid("CREWCHIEF_MISSING_FIELD")
        decoded[name] = _value(values[name], descriptor)
    update = _integer(packet["session_info_update"], 0, 2**31 - 1)
    session_b64 = packet["session_info_b64"]
    if (
        _cache is not None and type(session_b64) is str
        and session_b64 == _cache.session_b64 and _cache.session_info is not None
    ):
        session_info = _cache.session_info
    else:
        session_info = _session_info(session_b64)
    weekend = session_info.get("WeekendInfo")
    sim_mode = weekend.get("SimMode") if isinstance(weekend, Mapping) else None
    frame = RawSdkFrame(
        buffer_tick=_integer(packet["buffer_tick"], 0, 2**63 - 1),
        session_info_update=update, values=decoded, read_errors=tuple(errors),
        sim_mode_raw=sim_mode, captured_monotonic_s=captured_monotonic_s,
    )
    if _cache is not None:
        # Commit only after the entire packet is valid. Exact original base64
        # also detects changed YAML with an unchanged update counter; changed
        # counters with identical YAML still bind to the current frame below.
        _cache.schema_key = schema_key
        _cache.descriptors = schema
        _cache.session_b64 = session_b64
        _cache.session_info = session_info
    return _Snapshot(meta, schema, frame, session_info)


class _ReaderProcess:
    """One owned process and one bounded worker; timeouts terminate both I/O ends."""

    def __init__(self, reader_path: Path) -> None:
        try:
            self._process = subprocess.Popen(
                [str(reader_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False, bufsize=64 * 1024,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, ValueError):
            raise _ReaderUnavailable("CREWCHIEF_READER_START_FAILED") from None
        self._requests: queue.Queue[bool | None] = queue.Queue(maxsize=1)
        self._responses: queue.Queue[bytes | Exception] = queue.Queue(maxsize=1)
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._serve, daemon=True)
        self._worker.start()

    @property
    def alive(self) -> bool:
        return not self._closed.is_set() and self._process.poll() is None

    def _serve(self) -> None:
        while not self._closed.is_set():
            request = self._requests.get()
            if request is None or self._closed.is_set():
                return
            try:
                assert self._process.stdin is not None and self._process.stdout is not None
                self._process.stdin.write(b"snapshot\n")
                self._process.stdin.flush()
                line = self._process.stdout.readline(MAX_RESPONSE_BYTES + 1)
                if not line:
                    raise _ReaderUnavailable("CREWCHIEF_READER_EOF")
                if len(line) > MAX_RESPONSE_BYTES or not line.endswith(b"\n"):
                    raise _invalid("CREWCHIEF_READER_RESPONSE_INVALID")
                result: bytes | Exception = line
            except Exception as exc:
                result = (
                    exc if isinstance(exc, (SdkProbeUnavailable, SdkProbeConsistencyError))
                    else _ReaderUnavailable("CREWCHIEF_READER_IO_FAILED")
                )
            if not self._closed.is_set():
                self._responses.put_nowait(result)
            if isinstance(result, Exception):
                return

    def exchange(self, timeout_s: float) -> bytes:
        with self._lock:
            if not self.alive:
                raise _ReaderUnavailable("CREWCHIEF_READER_EXITED")
            self._requests.put_nowait(True)
            try:
                result = self._responses.get(timeout=timeout_s)
            except queue.Empty:
                self.close()
                raise _ReaderUnavailable("CREWCHIEF_READER_TIMEOUT") from None
            if isinstance(result, Exception):
                self.close()
                raise result from None
            return result

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with suppress(queue.Full):
            self._requests.put_nowait(None)
        if self._process.poll() is None:
            with suppress(OSError):
                self._process.kill()
        with suppress(OSError, subprocess.TimeoutExpired):
            self._process.wait(timeout=0.5)
        self._worker.join(timeout=0.5)
        # Do not acquire a BufferedReader lock if a failed kill left I/O blocked.
        if not self._worker.is_alive():
            for pipe in (self._process.stdin, self._process.stdout):
                if pipe is not None:
                    with suppress(OSError):
                        pipe.close()


class WindowsCrewChiefTransport:
    """Opt-in transport for the dedicated ``crewchief-readonly-v1`` executable.

    ``_exchange`` exists only for offline protocol tests, is never selected by
    CLI, and is not evidence of a real simulator connection.
    """

    def __init__(
        self, reader_path: Path, *, _exchange: Callable[[float], bytes] | None = None
    ) -> None:
        if not isinstance(reader_path, Path):
            raise TypeError("reader_path must be a Path")
        if _exchange is None:
            if platform.system() != "Windows":
                raise SdkProbeUnavailable("CREWCHIEF_READER_REQUIRES_WINDOWS")
            if not reader_path.is_absolute() or reader_path.suffix.lower() != ".exe":
                raise SdkProbeUnavailable("CREWCHIEF_READER_PATH_INVALID")
            if not reader_path.is_file():
                raise SdkProbeUnavailable("CREWCHIEF_READER_NOT_FOUND")
        elif not callable(_exchange):
            raise TypeError("_exchange must be callable")
        self._reader_path = reader_path
        self._test_exchange = _exchange
        self._reader: _ReaderProcess | None = None
        self._metadata_cache = _MetadataCache()
        self._startup_snapshot: _Snapshot | None = None
        self._latest: _Snapshot | None = None
        self._initial_pending = False
        self._closed = False
        self._connected = False

    def _read(self, timeout_s: float) -> _Snapshot:
        if self._closed:
            raise _ReaderUnavailable("CREWCHIEF_READER_CLOSED")
        try:
            if self._test_exchange is not None:
                line = self._test_exchange(timeout_s)
            else:
                if self._reader is None:
                    self._reader = _ReaderProcess(self._reader_path)
                line = self._reader.exchange(timeout_s)
            captured_at = monotonic_now()
            return _decode_snapshot(
                line, captured_monotonic_s=captured_at, _cache=self._metadata_cache,
            )
        except SdkProbeConsistencyError:
            self.close()
            raise
        except SdkProbeUnavailable:
            self._connected = False
            self._latest = None
            self._metadata_cache.clear()
            raise
        except Exception:
            self.close()
            raise _ReaderUnavailable("CREWCHIEF_READER_IO_FAILED") from None

    def startup(self, timeout_s: float) -> ConnectionMeta:
        if (
            type(timeout_s) not in {int, float} or not math.isfinite(timeout_s)
            or timeout_s < 0
        ):
            raise ValueError("timeout_s must be finite and non-negative")
        if self._startup_snapshot is not None:
            raise _invalid("CREWCHIEF_READER_ALREADY_STARTED")
        deadline = monotonic_now() + timeout_s
        first_attempt = True
        while True:
            if not first_attempt and monotonic_now() >= deadline:
                self.close()
                raise SdkProbeUnavailable("CREWCHIEF_SDK_CONNECTION_TIMEOUT")
            first_attempt = False
            remaining = max(0.0, deadline - monotonic_now())
            try:
                # Zero means one bounded attempt, not a 50 ms cold-start budget.
                request_timeout = READ_TIMEOUT_S if timeout_s == 0 else min(
                    READ_TIMEOUT_S, max(0.001, remaining)
                )
                snapshot = self._read(request_timeout)
            except _ReaderUnavailable:
                self.close()
                raise
            except SdkProbeConsistencyError:
                self.close()
                raise
            except SdkProbeUnavailable:
                if monotonic_now() >= deadline:
                    self.close()
                    raise SdkProbeUnavailable("CREWCHIEF_SDK_CONNECTION_TIMEOUT") from None
                time.sleep(min(0.05, max(0.0, deadline - monotonic_now())))
                continue
            self._startup_snapshot = self._latest = snapshot
            self._initial_pending = self._connected = True
            return snapshot.connection

    def _active_schema(self) -> tuple[VariableDescriptor, ...]:
        if self._startup_snapshot is None or self._closed:
            raise _invalid("CREWCHIEF_READER_NOT_INITIALIZED")
        return self._startup_snapshot.descriptors

    def descriptors(self) -> tuple[VariableDescriptor, ...]:
        # Frozen dataclasses still have mutable __dict__ objects and permit
        # object.__setattr__. Do not expose descriptors shared by startup and
        # the cache. Internal reads avoid making these public copies per tick.
        return tuple(replace(item) for item in self._active_schema())

    def read_frozen(self, fields: tuple[str, ...]) -> RawSdkFrame:
        schema = self._active_schema()
        if (
            type(fields) is not tuple or any(type(name) is not str for name in fields)
            or len(fields) != len(set(fields))
            or not set(fields) <= {item.name for item in schema}
        ):
            raise _invalid("CREWCHIEF_FIELDS_INVALID")
        if self._initial_pending:
            snapshot = self._startup_snapshot
            self._initial_pending = False
        else:
            try:
                snapshot = self._read(READ_TIMEOUT_S)
            except (SdkProbeUnavailable, SdkProbeConsistencyError):
                self.close()
                raise
        assert snapshot is not None and self._startup_snapshot is not None
        if (
            snapshot.connection != self._startup_snapshot.connection
            or snapshot.descriptors != schema
        ):
            self.close()
            raise _invalid("CREWCHIEF_SCHEMA_CHANGED")
        self._latest = snapshot
        self._connected = True
        frame = snapshot.frame
        return RawSdkFrame(
            buffer_tick=frame.buffer_tick, session_info_update=frame.session_info_update,
            values={name: frame.values[name] for name in fields if name in frame.values},
            read_errors=tuple(name for name in frame.read_errors if name in fields),
            sim_mode_raw=copy.deepcopy(frame.sim_mode_raw),
            captured_monotonic_s=frame.captured_monotonic_s,
        )

    def sim_mode(self) -> tuple[Any, int | None]:
        if self._latest is None or self._closed:
            return None, None
        return (
            copy.deepcopy(self._latest.frame.sim_mode_raw),
            self._latest.frame.session_info_update,
        )

    def session_info_snapshot(self) -> tuple[Mapping[str, object] | None, int | None]:
        if self._latest is None or self._closed:
            return None, None
        return copy.deepcopy(self._latest.session_info), self._latest.frame.session_info_update

    @property
    def connected(self) -> bool:
        return (
            not self._closed and self._connected
            and (self._reader is None or self._reader.alive)
        )

    def close(self) -> None:
        self._closed = True
        self._connected = False
        self._latest = None
        self._startup_snapshot = None
        self._initial_pending = False
        self._metadata_cache.clear()
        if self._reader is not None:
            self._reader.close()


__all__ = ["WindowsCrewChiefTransport"]
