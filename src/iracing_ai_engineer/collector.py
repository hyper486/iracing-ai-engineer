"""Append-only live telemetry collection with deterministic audit receipts.

The collector is deliberately separated from iRacing control.  A transport may
only supply frozen telemetry frames and metadata; the collector never sends a
command back to the simulator.  The state machine is also usable with an
arbitrary ``CollectorSample`` iterable, which keeps offline tests independent of
Windows and leaves a clean boundary for a future Parquet writer.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import stat
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

from .sdk_probe import (
    SDK_TYPE_NAMES,
    SDK_TYPE_SIZES,
    RawSdkFrame,
    VariableDescriptor,
    _bind_frame_sim_mode,
    schema_sha256,
)
from .telemetry import SourceKind

COLLECTOR_CONTRACT_VERSION = "live-collector-v2"
DEFAULT_STALE_AFTER_S = 0.5
R8_MAX_CAPTURE_BYTES = 8 * 1024**3
_DRIVER_INFO_KEY = "driverinfo"
_MAX_SCHEMA_VARIABLES = 4_096
_MAX_DESCRIPTOR_COUNT = 65_536
_MAX_BUFFER_BYTES = 256 * 1024 * 1024
_DESCRIPTOR_TEXT_BYTE_LIMITS = {
    "name": 32,
    "dtype": 32,
    "unit": 32,
    "description": 64,
}
_RECORD_ENVELOPE_KEYS = frozenset(
    {"collector_contract_version", "record_type", "sequence"}
)


class CollectorConsistencyError(RuntimeError):
    """Raised when a sample cannot be represented truthfully."""


class SessionInfoPayloadScope(StrEnum):
    """How complete the SessionInfo payload is for a collected sample."""

    FULL = "FULL"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class CollectorRecordWriter(Protocol):
    """Minimal writer boundary; a future Parquet sink can implement this."""

    def write(self, record: Mapping[str, object]) -> None:
        """Durably append one complete logical record."""


class ReadOnlySdkTransport(Protocol):
    """The read-only subset of a live SDK transport used by the collector."""

    def startup(self, timeout_s: float) -> Any: ...

    def descriptors(self) -> tuple[VariableDescriptor, ...]: ...

    def read_frozen(self, fields: tuple[str, ...]) -> RawSdkFrame: ...

    def sim_mode(self) -> tuple[Any, int | None]: ...

    @property
    def connected(self) -> bool: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class CollectorSample:
    """One frozen SDK observation and the schema that decodes it."""

    frame: RawSdkFrame
    descriptors: tuple[VariableDescriptor, ...]
    tick_rate_hz: int
    session_info: Mapping[str, object] | None = None
    session_info_scope: SessionInfoPayloadScope | str | None = None


@dataclass(frozen=True)
class CollectorReceipt:
    """Path- and wall-clock-independent receipt for one collector run."""

    records_sha256: str
    completion_status: str
    semantic_record_count: int
    run_record_count: int
    frame_record_count: int
    event_record_count: int
    schema_record_count: int
    session_info_record_count: int
    samples_seen: int
    duplicate_sample_count: int
    duplicate_conflict_count: int
    dropped_tick_count: int
    stale_event_count: int
    session_reset_count: int
    schema_change_count: int
    schema_epoch_count: int
    session_epoch_count: int
    first_buffer_tick: int | None
    last_buffer_tick: int | None
    collector_contract_version: str = COLLECTOR_CONTRACT_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _normalized_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _json_safe(value: object) -> object:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CollectorConsistencyError("collector JSON cannot contain non-finite floats")
        return 0.0 if value == 0.0 else value
    if isinstance(value, bytes):
        raise CollectorConsistencyError("collector JSON cannot contain bytes")
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise CollectorConsistencyError(
                    "collector JSON mapping keys must be plain strings"
                )
            result[key] = _json_safe(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if type(value) in {str, int, bool} or value is None:
        return value
    raise CollectorConsistencyError(
        f"unsupported JSON value in collector sample: {type(value).__name__}"
    )


def _plain_nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise CollectorConsistencyError(f"{label} must be a plain non-negative integer")
    return value


def _validate_descriptor_text(value: object, label: str, *, allow_empty: bool) -> str:
    if type(value) is not str:
        raise CollectorConsistencyError(f"descriptor {label} must be plain text")
    if not allow_empty and not value:
        raise CollectorConsistencyError(f"descriptor {label} must not be empty")
    if value != value.strip() and label in {"name", "dtype"}:
        raise CollectorConsistencyError(f"descriptor {label} must not have outer whitespace")
    if any(ord(character) < 32 for character in value):
        raise CollectorConsistencyError(f"descriptor {label} contains control characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CollectorConsistencyError(f"descriptor {label} is not valid UTF-8 text") from exc
    if len(encoded) > _DESCRIPTOR_TEXT_BYTE_LIMITS[label]:
        raise CollectorConsistencyError(
            f"descriptor {label} exceeds {_DESCRIPTOR_TEXT_BYTE_LIMITS[label]} UTF-8 bytes"
        )
    return value


def validate_variable_descriptors(
    descriptors: tuple[VariableDescriptor, ...],
) -> None:
    """Fail closed on schemas that cannot be safely serialized and replayed."""

    if type(descriptors) is not tuple or not descriptors:
        raise CollectorConsistencyError("collector schema must be a non-empty tuple")
    if len(descriptors) > _MAX_SCHEMA_VARIABLES:
        raise CollectorConsistencyError("collector schema has an unreasonable variable count")

    names: set[str] = set()
    spans: list[tuple[int, int, str]] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, VariableDescriptor):
            raise CollectorConsistencyError(
                "collector schema entries must be VariableDescriptor values"
            )
        name = _validate_descriptor_text(descriptor.name, "name", allow_empty=False)
        dtype = _validate_descriptor_text(descriptor.dtype, "dtype", allow_empty=False)
        _validate_descriptor_text(descriptor.unit, "unit", allow_empty=True)
        _validate_descriptor_text(descriptor.description, "description", allow_empty=True)
        if name in names:
            raise CollectorConsistencyError(f"collector schema contains duplicate name {name!r}")
        names.add(name)

        if type(descriptor.type_code) is not int or descriptor.type_code not in SDK_TYPE_NAMES:
            raise CollectorConsistencyError(f"descriptor {name} has an unknown type_code")
        if dtype != SDK_TYPE_NAMES[descriptor.type_code]:
            raise CollectorConsistencyError(
                f"descriptor {name} dtype does not match type_code"
            )
        if type(descriptor.count) is not int or not 1 <= descriptor.count <= _MAX_DESCRIPTOR_COUNT:
            raise CollectorConsistencyError(f"descriptor {name} has an invalid count")
        offset = _plain_nonnegative_int(descriptor.offset, f"descriptor {name} offset")
        if type(descriptor.count_as_time) is not bool:
            raise CollectorConsistencyError(
                f"descriptor {name} count_as_time must be a plain boolean"
            )
        byte_end = offset + SDK_TYPE_SIZES[descriptor.type_code] * descriptor.count
        if byte_end > _MAX_BUFFER_BYTES:
            raise CollectorConsistencyError(f"descriptor {name} exceeds the buffer boundary")
        spans.append((offset, byte_end, name))

    previous_end = -1
    previous_name = ""
    for offset, byte_end, name in sorted(spans):
        if offset < previous_end:
            raise CollectorConsistencyError(
                f"descriptor byte ranges overlap: {previous_name} and {name}"
            )
        previous_end = byte_end
        previous_name = name


def _resolved_session_info_scope(sample: CollectorSample) -> SessionInfoPayloadScope:
    raw_scope = sample.session_info_scope
    if raw_scope is None:
        scope = (
            SessionInfoPayloadScope.FULL
            if sample.session_info is not None
            else SessionInfoPayloadScope.UNAVAILABLE
        )
    else:
        try:
            scope = SessionInfoPayloadScope(raw_scope)
        except (TypeError, ValueError) as exc:
            raise CollectorConsistencyError("invalid SessionInfo payload scope") from exc
    if sample.session_info is None and scope is not SessionInfoPayloadScope.UNAVAILABLE:
        raise CollectorConsistencyError("missing SessionInfo must use UNAVAILABLE scope")
    if sample.session_info is not None and scope is SessionInfoPayloadScope.UNAVAILABLE:
        raise CollectorConsistencyError("present SessionInfo cannot use UNAVAILABLE scope")
    return scope


@dataclass(frozen=True)
class _PreparedSample:
    values: dict[str, object]
    session_info: dict[str, object] | None
    redacted_paths: tuple[str, ...]
    session_info_scope: SessionInfoPayloadScope


def _prepare_collector_sample(
    sample: CollectorSample,
    *,
    include_driver_info: bool,
) -> _PreparedSample:
    if not isinstance(sample, CollectorSample):
        raise CollectorConsistencyError("collector input must be a CollectorSample")
    if not isinstance(sample.frame, RawSdkFrame):
        raise CollectorConsistencyError("collector sample frame must be a RawSdkFrame")
    _plain_nonnegative_int(sample.frame.buffer_tick, "buffer_tick")
    _plain_nonnegative_int(sample.frame.session_info_update, "session_info_update")
    if type(sample.tick_rate_hz) is not int or not 1 <= sample.tick_rate_hz <= 360:
        raise CollectorConsistencyError("tick_rate_hz must be a plain integer from 1 to 360")
    validate_variable_descriptors(sample.descriptors)
    descriptor_names = {descriptor.name for descriptor in sample.descriptors}
    if not isinstance(sample.frame.values, Mapping):
        raise CollectorConsistencyError("frame values must be a mapping")
    safe_values = _json_safe(sample.frame.values)
    if not isinstance(safe_values, dict):
        raise CollectorConsistencyError("frame values root must be a mapping")
    unknown_fields = sorted(set(safe_values) - descriptor_names)
    if unknown_fields:
        raise CollectorConsistencyError(
            f"frame fields absent from schema: {', '.join(unknown_fields)}"
        )
    if type(sample.frame.read_errors) is not tuple:
        raise CollectorConsistencyError("read_errors must be a tuple of schema field names")
    if any(type(field) is not str for field in sample.frame.read_errors):
        raise CollectorConsistencyError("read_errors must contain only plain strings")
    if len(set(sample.frame.read_errors)) != len(sample.frame.read_errors):
        raise CollectorConsistencyError("read_errors contains duplicate fields")
    unknown_errors = sorted(set(sample.frame.read_errors) - descriptor_names)
    if unknown_errors:
        raise CollectorConsistencyError(
            f"read_errors fields absent from schema: {', '.join(unknown_errors)}"
        )
    _capture_us(sample.frame)
    scope = _resolved_session_info_scope(sample)
    sanitized, redacted = _sanitize_session_info(
        sample.session_info,
        include_driver_info=include_driver_info,
    )
    return _PreparedSample(
        values=safe_values,
        session_info=sanitized,
        redacted_paths=redacted,
        session_info_scope=scope,
    )


def validate_collector_sample(
    sample: CollectorSample,
    *,
    include_driver_info: bool = False,
) -> None:
    """Validate all sample-controlled bytes before a writer sees a record."""

    _prepare_collector_sample(sample, include_driver_info=include_driver_info)


def _sanitize_session_info(
    value: Mapping[str, object] | None,
    *,
    include_driver_info: bool,
) -> tuple[dict[str, object] | None, tuple[str, ...]]:
    if value is None:
        return None, ()
    redacted: list[str] = []

    def visit(item: object, path: tuple[str, ...]) -> object:
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for raw_key, raw_value in item.items():
                if type(raw_key) is not str:
                    raise CollectorConsistencyError(
                        "SessionInfo mapping keys must be plain strings"
                    )
                key = raw_key
                child_path = (*path, key)
                if not include_driver_info and _normalized_key(key) == _DRIVER_INFO_KEY:
                    redacted.append(".".join(child_path))
                    continue
                result[key] = visit(raw_value, child_path)
            return result
        if isinstance(item, (list, tuple)):
            return [visit(child, (*path, str(index))) for index, child in enumerate(item)]
        return _json_safe(item)

    sanitized = visit(value, ())
    if not isinstance(sanitized, dict):  # defensive: the input annotation promises a mapping
        raise CollectorConsistencyError("SessionInfo root must be a mapping")
    return sanitized, tuple(sorted(redacted))


def _finite_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _capture_us(frame: RawSdkFrame) -> int | None:
    value = frame.captured_monotonic_s
    if value is None:
        return None
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise CollectorConsistencyError("captured_monotonic_s must be finite and non-negative")
    capture_us = round(value * 1_000_000)
    if capture_us > (1 << 63) - 1:
        raise CollectorConsistencyError(
            "captured_monotonic_s exceeds signed 64-bit microseconds"
        )
    return capture_us


def _telemetry_payload_digest(frame: RawSdkFrame) -> str:
    """Hash raw-buffer content, excluding independently changing metadata."""

    payload = {
        "buffer_tick": frame.buffer_tick,
        "read_errors": list(frame.read_errors),
        "values": _json_safe(frame.values),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _schema_payload(
    descriptors: tuple[VariableDescriptor, ...],
) -> tuple[str, list[dict[str, object]]]:
    validate_variable_descriptors(descriptors)
    variables = [asdict(item) for item in sorted(descriptors, key=lambda item: item.name)]
    return schema_sha256(descriptors), variables


class JsonlAppendWriter:
    """Exclusively create a run file and durably write canonical JSON lines."""

    def __init__(self, path: str | Path, *, fsync_each_record: bool = True) -> None:
        if type(fsync_each_record) is not bool:
            raise ValueError("fsync_each_record must be a plain boolean")
        self.path = Path(path)
        self.fsync_each_record = fsync_each_record
        self._handle: Any = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("xb", buffering=0)
        return self

    def write(self, record: Mapping[str, object]) -> None:
        if self._handle is None:
            raise RuntimeError("JSONL writer is not open")
        safe_record = _json_safe(record)
        if not isinstance(safe_record, dict):
            raise CollectorConsistencyError("collector record root must be a mapping")
        payload = _canonical_json(safe_record) + b"\n"
        written = self._handle.write(payload)
        if written != len(payload):
            raise OSError(
                f"short JSONL write: wrote {written!r} of {len(payload)} bytes"
            )
        self._handle.flush()
        if self.fsync_each_record:
            os.fsync(self._handle.fileno())

    def close(self) -> None:
        if self._handle is not None:
            handle = self._handle
            self._handle = None
            handle.close()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class JsonlHandleWriter:
    """Write canonical JSONL to one caller-owned, empty binary descriptor.

    The writer never closes ``handle`` and never resolves a pathname.  It is
    intended for a supervisor that CreateNew-opens the canonical capture once,
    keeps that same object open through collection and analysis, and prevents
    concurrent use while this writer is active.
    """

    def __init__(
        self,
        handle: object,
        *,
        fsync_each_record: bool = True,
        max_output_bytes: int = R8_MAX_CAPTURE_BYTES,
    ) -> None:
        if type(fsync_each_record) is not bool:
            raise ValueError("fsync_each_record must be a plain boolean")
        if type(max_output_bytes) is not int or max_output_bytes < 1:
            raise ValueError("max_output_bytes must be a positive plain integer")
        self.handle = handle
        self.fsync_each_record = fsync_each_record
        self.max_output_bytes = max_output_bytes
        self._descriptor: int | None = None
        self._identity: tuple[int, int, int] | None = None
        self._byte_size = 0
        self._digest = hashlib.sha256()
        self._active = False

    @staticmethod
    def _stable_identity(metadata: os.stat_result) -> tuple[int, int, int]:
        return (metadata.st_dev, metadata.st_ino, metadata.st_mode)

    def _validate_descriptor(self, *, expected_size: int) -> os.stat_result:
        if self._descriptor is None or self._identity is None:
            raise RuntimeError("JSONL handle writer is not open")
        metadata = os.fstat(self._descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or self._stable_identity(metadata) != self._identity
            or metadata.st_nlink != 1
            or metadata.st_size != expected_size
            or os.lseek(self._descriptor, 0, os.SEEK_CUR) != expected_size
        ):
            raise CollectorConsistencyError(
                "caller-owned JSONL handle identity, size, or position changed"
            )
        return metadata

    def __enter__(self) -> Self:
        if self._active or self._descriptor is not None:
            raise RuntimeError("JSONL handle writer is already open")
        if type(self.handle) is not io.FileIO:
            raise TypeError("JSONL handle must be an unbuffered binary io.FileIO")
        if (
            self.handle.closed
            or not self.handle.readable()
            or not self.handle.writable()
            or not self.handle.seekable()
        ):
            raise CollectorConsistencyError(
                "JSONL handle must be open, readable, writable, and seekable"
            )
        descriptor = self.handle.fileno()
        if type(descriptor) is not int or descriptor < 0:
            raise CollectorConsistencyError(
                "JSONL handle descriptor must be a non-negative integer"
            )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != 0
        ):
            raise CollectorConsistencyError(
                "JSONL handle must be one empty, singly linked regular file"
            )
        if os.lseek(descriptor, 0, os.SEEK_CUR) != 0:
            raise CollectorConsistencyError("JSONL handle must start at byte zero")
        self._descriptor = descriptor
        self._identity = self._stable_identity(metadata)
        self._active = True
        return self

    @property
    def byte_size(self) -> int:
        return self._byte_size

    @property
    def capture_sha256(self) -> str:
        return self._digest.hexdigest()

    def write(self, record: Mapping[str, object]) -> None:
        if not self._active or self._descriptor is None:
            raise RuntimeError("JSONL handle writer is not open")
        safe_record = _json_safe(record)
        if not isinstance(safe_record, dict):
            raise CollectorConsistencyError("collector record root must be a mapping")
        payload = _canonical_json(safe_record) + b"\n"
        self._validate_descriptor(expected_size=self._byte_size)
        if len(payload) > self.max_output_bytes - self._byte_size:
            raise CollectorConsistencyError(
                "caller-owned JSONL output would exceed max_output_bytes"
            )
        remaining = memoryview(payload)
        while remaining:
            written = os.write(self._descriptor, remaining)
            if written <= 0:
                raise OSError("short JSONL handle write")
            remaining = remaining[written:]
        self._byte_size += len(payload)
        self._digest.update(payload)
        if self.fsync_each_record:
            os.fsync(self._descriptor)
        self._validate_descriptor(expected_size=self._byte_size)

    def close(self) -> None:
        if not self._active:
            return
        assert self._descriptor is not None
        try:
            os.fsync(self._descriptor)
            self._validate_descriptor(expected_size=self._byte_size)
        finally:
            self._active = False
            self._descriptor = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.close()
            return
        descriptor = self._descriptor
        self._active = False
        self._descriptor = None
        if descriptor is not None:
            try:
                os.fsync(descriptor)
            except OSError as close_error:
                if exc_value is not None:
                    exc_value.add_note(
                        "JSONL handle fsync after failure also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )


def _validated_run_identifier(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a non-empty plain string")
    if value != value.strip():
        raise ValueError(f"{label} must not have outer whitespace")
    if len(value.encode("utf-8")) > 256 or any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} is not a reasonable identifier")
    return value


def _validated_expected_source_kind(
    value: SourceKind | str | None,
) -> SourceKind | None:
    if value is None:
        return None
    try:
        kind = SourceKind(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected_source_kind is invalid") from exc
    if kind not in {SourceKind.SDK_LIVE, SourceKind.REPLAY_SDK_PROXY}:
        raise ValueError("live collector cannot emit the requested source kind")
    return kind


def _source_kind_from_sim_mode(raw_mode: object) -> tuple[str, SourceKind]:
    if type(raw_mode) is not str:
        raise CollectorConsistencyError("sim_mode_raw must be stable 'full' or 'replay'")
    mode = raw_mode.strip().casefold()
    if mode == "full":
        return mode, SourceKind.SDK_LIVE
    if mode == "replay":
        return mode, SourceKind.REPLAY_SDK_PROXY
    raise CollectorConsistencyError("sim_mode_raw must be stable 'full' or 'replay'")


class LiveCollector:
    """Stateful detector and recorder for a stream of frozen SDK samples."""

    def __init__(
        self,
        writer: CollectorRecordWriter,
        *,
        source_id: str,
        session_id: str,
        expected_source_kind: SourceKind | str | None = None,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        include_driver_info: bool = False,
    ) -> None:
        if not math.isfinite(stale_after_s) or stale_after_s <= 0:
            raise ValueError("stale_after_s must be finite and positive")
        self._writer = writer
        self._source_id = _validated_run_identifier(source_id, "source_id")
        self._session_id = _validated_run_identifier(session_id, "session_id")
        self._expected_source_kind = _validated_expected_source_kind(expected_source_kind)
        self._stale_after_us = round(stale_after_s * 1_000_000)
        self._include_driver_info = include_driver_info
        self._digest = hashlib.sha256()
        self._sequence = 0
        self._finished = False
        self._receipt: CollectorReceipt | None = None
        self._run_written = False
        self._source_kind: SourceKind | None = None
        self._sim_mode: str | None = None

        self._schema_digest: str | None = None
        self._tick_rate_hz: int | None = None
        self._schema_epoch = -1
        self._session_epoch = 0
        self._last_frame: RawSdkFrame | None = None
        self._last_frame_payload_digest: str | None = None
        self._last_progress_us: int | None = None
        self._last_session_update: int | None = None
        self._last_session_payload_digest: str | None = None
        self._last_session_scope: SessionInfoPayloadScope | None = None
        self._stale = False

        self._frame_records = 0
        self._event_records = 0
        self._schema_records = 0
        self._session_info_records = 0
        self._samples_seen = 0
        self._duplicates = 0
        self._duplicate_conflicts = 0
        self._dropped_ticks = 0
        self._stale_events = 0
        self._session_resets = 0
        self._schema_changes = 0
        self._first_tick: int | None = None
        self._last_tick: int | None = None

    def _append(self, record_type: str, payload: Mapping[str, object]) -> None:
        collisions = sorted(set(payload) & _RECORD_ENVELOPE_KEYS)
        if collisions:
            raise CollectorConsistencyError(
                f"record payload collides with envelope keys: {', '.join(collisions)}"
            )
        record = {
            "collector_contract_version": COLLECTOR_CONTRACT_VERSION,
            "record_type": record_type,
            "sequence": self._sequence,
            **payload,
        }
        safe_record = _json_safe(record)
        if not isinstance(safe_record, dict):
            raise CollectorConsistencyError("collector record root must be a mapping")
        encoded = _canonical_json(safe_record)
        self._writer.write(safe_record)
        self._digest.update(len(encoded).to_bytes(8, "little"))
        self._digest.update(encoded)
        self._sequence += 1

    def _write_run(self, mode: str, source_kind: SourceKind) -> None:
        self._append(
            "run",
            {
                "session_id": self._session_id,
                "sim_mode": mode,
                "source_id": self._source_id,
                "source_kind": source_kind.value,
            },
        )
        self._run_written = True
        self._sim_mode = mode
        self._source_kind = source_kind

    def _event(
        self,
        kind: str,
        frame: RawSdkFrame,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self._append(
            "event",
            {
                "buffer_tick": frame.buffer_tick,
                "capture_monotonic_us": _capture_us(frame),
                "details": dict(details or {}),
                "event_kind": kind,
                "schema_epoch": self._schema_epoch,
                "session_epoch": self._session_epoch,
            },
        )
        self._event_records += 1

    def _write_schema(
        self,
        sample: CollectorSample,
        digest: str,
        variables: list[dict[str, object]],
    ) -> None:
        self._append(
            "schema",
            {
                "effective_buffer_tick": sample.frame.buffer_tick,
                "schema_epoch": self._schema_epoch,
                "schema_sha256": digest,
                "tick_rate_hz": sample.tick_rate_hz,
                "variables": variables,
            },
        )
        self._schema_records += 1

    @staticmethod
    def _reset_reasons(previous: RawSdkFrame, current: RawSdkFrame) -> tuple[str, ...]:
        reasons: list[str] = []
        if current.buffer_tick < previous.buffer_tick:
            reasons.append("BUFFER_TICK_REGRESSION")
        if current.session_info_update < previous.session_info_update:
            reasons.append("SESSION_INFO_UPDATE_REGRESSION")
        previous_session = _finite_number(previous.values.get("SessionNum"))
        current_session = _finite_number(current.values.get("SessionNum"))
        if (
            previous_session is not None
            and current_session is not None
            and current_session != previous_session
        ):
            reasons.append("SESSION_NUM_CHANGED")
        for field in ("SessionTick", "SessionTime"):
            before = _finite_number(previous.values.get(field))
            after = _finite_number(current.values.get(field))
            if before is not None and after is not None and after < before:
                reasons.append(f"{field.upper()}_REGRESSION")
        return tuple(reasons)

    def _observe_staleness(self, frame: RawSdkFrame, *, progressing: bool) -> None:
        capture_us = _capture_us(frame)
        if capture_us is None:
            return
        if self._last_progress_us is not None and capture_us < self._last_progress_us:
            self._event(
                "capture_clock_regression",
                frame,
                details={"previous_capture_monotonic_us": self._last_progress_us},
            )
            self._last_progress_us = capture_us if progressing else self._last_progress_us
            return
        if self._last_progress_us is None:
            if progressing:
                self._last_progress_us = capture_us
            return
        stale_for_us = capture_us - self._last_progress_us
        if stale_for_us > self._stale_after_us and not self._stale:
            self._event(
                "source_stale",
                frame,
                details={
                    "stale_after_us": self._stale_after_us,
                    "stale_for_us": stale_for_us,
                },
            )
            self._stale = True
            self._stale_events += 1
        if progressing:
            if self._stale:
                self._event(
                    "source_resumed",
                    frame,
                    details={"stale_for_us": stale_for_us},
                )
                self._stale = False
            self._last_progress_us = capture_us

    def _write_session_info(
        self,
        sample: CollectorSample,
        prepared: _PreparedSample,
        *,
        force: bool,
    ) -> None:
        sanitized = prepared.session_info
        payload_digest = hashlib.sha256(_canonical_json(sanitized)).hexdigest()
        update_changed = sample.frame.session_info_update != self._last_session_update
        payload_changed = payload_digest != self._last_session_payload_digest
        scope_changed = prepared.session_info_scope != self._last_session_scope
        if not (force or update_changed or payload_changed or scope_changed):
            return
        if (
            not force
            and not update_changed
            and payload_changed
            and self._last_session_payload_digest is not None
        ):
            self._event(
                "session_info_changed_without_update",
                sample.frame,
                details={"session_info_update": sample.frame.session_info_update},
            )
        self._append(
            "session_info",
            {
                "buffer_tick": sample.frame.buffer_tick,
                "payload": sanitized,
                "payload_scope": prepared.session_info_scope.value,
                "payload_status": "PRESENT" if sanitized is not None else "UNAVAILABLE",
                "redacted_paths": list(prepared.redacted_paths),
                "schema_epoch": self._schema_epoch,
                "session_epoch": self._session_epoch,
                "session_info_sha256": payload_digest,
                "session_info_update": sample.frame.session_info_update,
            },
        )
        self._session_info_records += 1
        self._last_session_update = sample.frame.session_info_update
        self._last_session_payload_digest = payload_digest
        self._last_session_scope = prepared.session_info_scope

    def ingest(self, sample: CollectorSample) -> None:
        if self._finished:
            raise RuntimeError("collector is already finished")
        prepared = _prepare_collector_sample(
            sample,
            include_driver_info=self._include_driver_info,
        )
        schema_digest, variables = _schema_payload(sample.descriptors)
        mode, source_kind = _source_kind_from_sim_mode(sample.frame.sim_mode_raw)
        if self._expected_source_kind is not None and source_kind is not self._expected_source_kind:
            raise CollectorConsistencyError(
                "observed sim mode does not match expected_source_kind"
            )
        if self._run_written and (mode != self._sim_mode or source_kind is not self._source_kind):
            raise CollectorConsistencyError("sim mode changed within one collector run")
        frame_payload = {
            "buffer_tick": sample.frame.buffer_tick,
            "capture_monotonic_us": _capture_us(sample.frame),
            "read_errors": list(sample.frame.read_errors),
            "schema_epoch": self._schema_epoch if self._run_written else 0,
            "session_epoch": self._session_epoch,
            "session_info_update": sample.frame.session_info_update,
            "sim_mode_raw": mode,
            "values": prepared.values,
        }
        _json_safe(frame_payload)
        frame_payload_digest = _telemetry_payload_digest(sample.frame)

        if not self._run_written:
            self._write_run(mode, source_kind)
        self._samples_seen += 1
        if self._schema_digest is None:
            self._schema_epoch = 0
            self._schema_digest = schema_digest
            self._write_schema(sample, schema_digest, variables)
            self._tick_rate_hz = sample.tick_rate_hz
        elif (
            schema_digest != self._schema_digest
            or sample.tick_rate_hz != self._tick_rate_hz
        ):
            previous_digest = self._schema_digest
            previous_tick_rate_hz = self._tick_rate_hz
            self._schema_epoch += 1
            self._schema_digest = schema_digest
            self._tick_rate_hz = sample.tick_rate_hz
            self._schema_changes += 1
            self._write_schema(sample, schema_digest, variables)
            self._event(
                "schema_changed",
                sample.frame,
                details={
                    "previous_schema_sha256": previous_digest,
                    "previous_tick_rate_hz": previous_tick_rate_hz,
                    "schema_sha256": schema_digest,
                    "tick_rate_hz": sample.tick_rate_hz,
                },
            )

        reset_reasons = (
            self._reset_reasons(self._last_frame, sample.frame)
            if self._last_frame is not None
            else ()
        )
        if reset_reasons:
            self._session_epoch += 1
            self._session_resets += 1
            self._event(
                "session_reset",
                sample.frame,
                details={"reasons": list(reset_reasons)},
            )
            self._stale = False
            self._last_progress_us = None
            self._last_session_update = None
            self._last_session_payload_digest = None
            self._last_session_scope = None

        duplicate = (
            self._last_frame is not None
            and not reset_reasons
            and sample.frame.buffer_tick == self._last_frame.buffer_tick
        )
        frame_payload["schema_epoch"] = self._schema_epoch
        frame_payload["session_epoch"] = self._session_epoch
        self._write_session_info(
            sample,
            prepared,
            force=self._last_frame is None or bool(reset_reasons),
        )
        if duplicate:
            self._duplicates += 1
            self._observe_staleness(sample.frame, progressing=False)
            conflict = self._last_frame_payload_digest != frame_payload_digest
            self._event(
                "duplicate_sample",
                sample.frame,
                details={
                    "conflict": conflict,
                    "current_payload_sha256": frame_payload_digest,
                    "previous_payload_sha256": self._last_frame_payload_digest,
                },
            )
            if conflict:
                self._duplicate_conflicts += 1
                self._event(
                    "duplicate_tick_conflict",
                    sample.frame,
                    details={
                        "buffer_tick": sample.frame.buffer_tick,
                        "current_payload_sha256": frame_payload_digest,
                        "previous_payload_sha256": self._last_frame_payload_digest,
                    },
                )
            return

        self._observe_staleness(sample.frame, progressing=True)
        if self._last_frame is not None and not reset_reasons:
            tick_delta = sample.frame.buffer_tick - self._last_frame.buffer_tick
            if tick_delta > 1:
                missing = tick_delta - 1
                self._dropped_ticks += missing
                self._event(
                    "tick_drop",
                    sample.frame,
                    details={
                        "current_buffer_tick": sample.frame.buffer_tick,
                        "missing_tick_count": missing,
                        "previous_buffer_tick": self._last_frame.buffer_tick,
                    },
                )

        self._append("frame", frame_payload)
        self._frame_records += 1
        if self._first_tick is None:
            self._first_tick = sample.frame.buffer_tick
        self._last_tick = sample.frame.buffer_tick
        self._last_frame_payload_digest = frame_payload_digest
        self._last_frame = sample.frame

    def finish(self) -> CollectorReceipt:
        if self._receipt is not None:
            return self._receipt
        if self._frame_records == 0:
            self._finished = True
            raise CollectorConsistencyError(
                "collector cannot emit a COMPLETE receipt without a frame"
            )
        self._finished = True
        receipt = CollectorReceipt(
            records_sha256=self._digest.hexdigest(),
            completion_status="COMPLETE",
            semantic_record_count=self._sequence,
            run_record_count=1,
            frame_record_count=self._frame_records,
            event_record_count=self._event_records,
            schema_record_count=self._schema_records,
            session_info_record_count=self._session_info_records,
            samples_seen=self._samples_seen,
            duplicate_sample_count=self._duplicates,
            duplicate_conflict_count=self._duplicate_conflicts,
            dropped_tick_count=self._dropped_ticks,
            stale_event_count=self._stale_events,
            session_reset_count=self._session_resets,
            schema_change_count=self._schema_changes,
            schema_epoch_count=self._schema_epoch + 1,
            session_epoch_count=self._session_epoch + 1,
            first_buffer_tick=self._first_tick,
            last_buffer_tick=self._last_tick,
        )
        record = {
            "collector_contract_version": COLLECTOR_CONTRACT_VERSION,
            "record_type": "collector_receipt",
            "sequence": self._sequence,
            "receipt": receipt.to_dict(),
        }
        safe_record = _json_safe(record)
        if not isinstance(safe_record, dict):
            raise CollectorConsistencyError("collector receipt root must be a mapping")
        _canonical_json(safe_record)
        self._writer.write(safe_record)
        self._receipt = receipt
        return receipt


def collect_samples(
    samples: Iterable[CollectorSample],
    writer: CollectorRecordWriter,
    *,
    source_id: str,
    session_id: str,
    expected_source_kind: SourceKind | str | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    include_driver_info: bool = False,
) -> CollectorReceipt:
    """Collect an already-frozen sample stream into an injected writer."""

    collector = LiveCollector(
        writer,
        source_id=source_id,
        session_id=session_id,
        expected_source_kind=expected_source_kind,
        stale_after_s=stale_after_s,
        include_driver_info=include_driver_info,
    )
    for sample in samples:
        collector.ingest(sample)
    return collector.finish()


def collect_samples_to_jsonl(
    samples: Iterable[CollectorSample],
    path: str | Path,
    *,
    source_id: str,
    session_id: str,
    expected_source_kind: SourceKind | str | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    include_driver_info: bool = False,
    fsync_each_record: bool = True,
) -> CollectorReceipt:
    """Exclusively create one JSONL run and flush each complete record."""

    _validated_run_identifier(source_id, "source_id")
    _validated_run_identifier(session_id, "session_id")
    _validated_expected_source_kind(expected_source_kind)
    with JsonlAppendWriter(path, fsync_each_record=fsync_each_record) as writer:
        return collect_samples(
            samples,
            writer,
            source_id=source_id,
            session_id=session_id,
            expected_source_kind=expected_source_kind,
            stale_after_s=stale_after_s,
            include_driver_info=include_driver_info,
        )


def collect_samples_to_jsonl_handle(
    samples: Iterable[CollectorSample],
    handle: object,
    *,
    source_id: str,
    session_id: str,
    expected_source_kind: SourceKind | str | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    include_driver_info: bool = False,
    fsync_each_record: bool = True,
    max_output_bytes: int = R8_MAX_CAPTURE_BYTES,
) -> CollectorReceipt:
    """Collect into one caller-owned CreateNew handle without closing it."""

    _validated_run_identifier(source_id, "source_id")
    _validated_run_identifier(session_id, "session_id")
    _validated_expected_source_kind(expected_source_kind)
    with JsonlHandleWriter(
        handle,
        fsync_each_record=fsync_each_record,
        max_output_bytes=max_output_bytes,
    ) as writer:
        return collect_samples(
            samples,
            writer,
            source_id=source_id,
            session_id=session_id,
            expected_source_kind=expected_source_kind,
            stale_after_s=stale_after_s,
            include_driver_info=include_driver_info,
        )


def _transport_session_info(
    transport: ReadOnlySdkTransport,
    frame: RawSdkFrame,
) -> tuple[RawSdkFrame, Mapping[str, object] | None, SessionInfoPayloadScope]:
    def partial_fallback() -> tuple[
        RawSdkFrame, Mapping[str, object] | None, SessionInfoPayloadScope
    ]:
        sim_mode, update = transport.sim_mode()
        bound = _bind_frame_sim_mode(frame, sim_mode, update)
        if update != frame.session_info_update:
            return bound, None, SessionInfoPayloadScope.UNAVAILABLE
        return (
            bound,
            {"WeekendInfo": {"SimMode": sim_mode}},
            SessionInfoPayloadScope.PARTIAL,
        )

    provider = getattr(transport, "session_info_snapshot", None)
    if callable(provider):
        result = provider()
        if not isinstance(result, tuple) or len(result) != 2:
            raise CollectorConsistencyError(
                "session_info_snapshot must return (mapping, update_counter)"
            )
        payload, update = result
        if update != frame.session_info_update:
            return partial_fallback()
        if payload is not None and not isinstance(payload, Mapping):
            raise CollectorConsistencyError("SessionInfo snapshot must be a mapping")
        if payload is None:
            return partial_fallback()
        weekend_info = payload.get("WeekendInfo")
        sim_mode = (
            weekend_info.get("SimMode")
            if isinstance(weekend_info, Mapping)
            else frame.sim_mode_raw
        )
        if sim_mode is None:
            fallback_mode, fallback_update = transport.sim_mode()
            if fallback_update == frame.session_info_update:
                sim_mode = fallback_mode
        bound = _bind_frame_sim_mode(frame, sim_mode, update)
        return bound, payload, SessionInfoPayloadScope.FULL

    return partial_fallback()


def _collect_transport_to_writer(
    transport: ReadOnlySdkTransport,
    writer_context: object,
    *,
    source_id: str,
    session_id: str,
    expected_source_kind: SourceKind | str | None = None,
    wait_seconds: float = 20.0,
    duration_s: float = 60.0,
    poll_seconds: float = 0.01,
    fields: Sequence[str] | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    include_driver_info: bool = False,
    max_reads: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> CollectorReceipt:
    """Collect from a read-only transport into one prepared writer context."""

    _validated_run_identifier(source_id, "source_id")
    _validated_run_identifier(session_id, "session_id")
    _validated_expected_source_kind(expected_source_kind)
    durations = (wait_seconds, duration_s, poll_seconds, stale_after_s)
    if not all(math.isfinite(value) for value in durations):
        raise ValueError("collector durations must be finite")
    if wait_seconds < 0 or duration_s <= 0 or poll_seconds <= 0 or stale_after_s <= 0:
        raise ValueError("collector durations must be positive (wait may be zero)")
    if max_reads is not None and max_reads <= 0:
        raise ValueError("max_reads must be positive")

    primary_error: BaseException | None = None
    try:
        connection = transport.startup(wait_seconds)
        initial_descriptors = transport.descriptors()
        validate_variable_descriptors(initial_descriptors)
        selected_fields = (
            tuple(fields)
            if fields is not None
            else tuple(descriptor.name for descriptor in initial_descriptors)
        )
        if not selected_fields or any(type(field) is not str for field in selected_fields):
            raise ValueError("collector fields must be non-empty plain strings")
        if len(set(selected_fields)) != len(selected_fields):
            raise ValueError("collector fields must be unique")
        unknown = sorted(set(selected_fields) - {item.name for item in initial_descriptors})
        if unknown:
            raise CollectorConsistencyError(
                f"requested fields absent from schema: {', '.join(unknown)}"
            )
        tick_rate_hz = connection.tick_rate_hz
        deadline = monotonic() + duration_s
        reads = 0
        with writer_context as writer:
            collector = LiveCollector(
                writer,
                source_id=source_id,
                session_id=session_id,
                expected_source_kind=expected_source_kind,
                stale_after_s=stale_after_s,
                include_driver_info=include_driver_info,
            )
            while True:
                if monotonic() >= deadline:
                    break
                if not transport.connected:
                    raise CollectorConsistencyError(
                        "SDK disconnected before the requested capture duration completed"
                    )
                frame = transport.read_frozen(selected_fields)
                frame, session_info, session_info_scope = _transport_session_info(
                    transport, frame
                )
                collector.ingest(
                    CollectorSample(
                        frame=frame,
                        descriptors=initial_descriptors,
                        tick_rate_hz=tick_rate_hz,
                        session_info=session_info,
                        session_info_scope=session_info_scope,
                    )
                )
                reads += 1
                if max_reads is not None and reads >= max_reads:
                    break
                remaining = deadline - monotonic()
                if remaining > 0:
                    sleep(min(poll_seconds, remaining))
            return collector.finish()
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            transport.close()
        except Exception as close_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                "transport.close() also failed: "
                f"{type(close_error).__name__}: {close_error}"
            )


def collect_transport_to_jsonl(
    transport: ReadOnlySdkTransport,
    path: str | Path,
    *,
    source_id: str,
    session_id: str,
    expected_source_kind: SourceKind | str | None = None,
    wait_seconds: float = 20.0,
    duration_s: float = 60.0,
    poll_seconds: float = 0.01,
    fields: Sequence[str] | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    include_driver_info: bool = False,
    fsync_each_record: bool = True,
    max_reads: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> CollectorReceipt:
    """Collect from a read-only transport into one exclusively created path."""

    return _collect_transport_to_writer(
        transport,
        JsonlAppendWriter(path, fsync_each_record=fsync_each_record),
        source_id=source_id,
        session_id=session_id,
        expected_source_kind=expected_source_kind,
        wait_seconds=wait_seconds,
        duration_s=duration_s,
        poll_seconds=poll_seconds,
        fields=fields,
        stale_after_s=stale_after_s,
        include_driver_info=include_driver_info,
        max_reads=max_reads,
        monotonic=monotonic,
        sleep=sleep,
    )


def collect_transport_to_jsonl_handle(
    transport: ReadOnlySdkTransport,
    handle: object,
    *,
    source_id: str,
    session_id: str,
    expected_source_kind: SourceKind | str | None = None,
    wait_seconds: float = 20.0,
    duration_s: float = 60.0,
    poll_seconds: float = 0.01,
    fields: Sequence[str] | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    include_driver_info: bool = False,
    fsync_each_record: bool = True,
    max_output_bytes: int = R8_MAX_CAPTURE_BYTES,
    max_reads: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> CollectorReceipt:
    """Collect into one caller-owned CreateNew handle and leave it open."""

    return _collect_transport_to_writer(
        transport,
        JsonlHandleWriter(
            handle,
            fsync_each_record=fsync_each_record,
            max_output_bytes=max_output_bytes,
        ),
        source_id=source_id,
        session_id=session_id,
        expected_source_kind=expected_source_kind,
        wait_seconds=wait_seconds,
        duration_s=duration_s,
        poll_seconds=poll_seconds,
        fields=fields,
        stale_after_s=stale_after_s,
        include_driver_info=include_driver_info,
        max_reads=max_reads,
        monotonic=monotonic,
        sleep=sleep,
    )
