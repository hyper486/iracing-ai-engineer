"""Strict input adapters for the normalized telemetry contract.

Both adapters expose :class:`TelemetrySample` objects plus narrow,
source-bound context evidence.  Raw IBT session metadata and collector
``session_info`` records are never returned, so ``SessionInfo``/``DriverInfo``
cannot accidentally enter the analytical data plane.  Collector runs may also
expose a fixed nine-field event-identity projection; it contains no driver,
member, team, setup, or free-form session data.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import weakref
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO

from .collector import (
    COLLECTOR_CONTRACT_VERSION,
    CollectorConsistencyError,
    validate_variable_descriptors,
)
from .ibt import IbtReader
from .sdk_probe import VariableDescriptor, schema_sha256
from .telemetry import (
    Presence,
    Provenance,
    SourceKind,
    TelemetryNormalizationError,
    TelemetrySample,
    normalize_sdk_frame,
)

# This is an explicit privacy and compatibility boundary, rather than "all
# fields in the source".  In particular, neither SessionInfo nor DriverInfo is
# part of the normalized frame contract.
NORMALIZED_SDK_FIELDS = (
    "SessionNum",
    "SessionTick",
    "SessionTime",
    "SessionTimeRemain",
    "SessionLapsRemainEx",
    "Lap",
    "LapCompleted",
    "LapDistPct",
    "Speed",
    "Throttle",
    "Brake",
    "Clutch",
    "SteeringWheelAngle",
    "Gear",
    "RPM",
    "FuelLevel",
    "FuelLevelPct",
    "FuelUsePerHour",
    "SessionTimeOfDay",
    "TrackTemp",
    "TrackTempCrew",
    "AirTemp",
    "WeatherType",
    "WeatherVersion",
    "Skies",
    "WindVel",
    "WindDir",
    "RelativeHumidity",
    "Precipitation",
    "PlayerTireCompound",
    "TireSetsUsed",
    "OnPitRoad",
    "PlayerCarInPitStall",
    "PitstopActive",
    "PitsOpen",
    "SessionFlags",
    "PlayerTrackSurface",
    "IsOnTrack",
    "IsOnTrackCar",
    "PlayerCarMyIncidentCount",
    "PlayerCarDriverIncidentCount",
    "PlayerCarTeamIncidentCount",
    "PlayerCarIdx",
    "CarIdxLap",
    "CarIdxLapCompleted",
    "CarIdxLapDistPct",
    "CarIdxOnPitRoad",
    "CarIdxTrackSurface",
)
TRACK_CONTEXT_CONTRACT_VERSION = "track-context-v1"
EVENT_IDENTITY_CONTEXT_CONTRACT_VERSION = "event-identity-context-v1"
TRAFFIC_OBSERVATION_CONTEXT_CONTRACT_VERSION = "traffic-observation-context-v1"
_TRACK_LENGTH_SOURCE_FIELD = "WeekendInfo.TrackLength"
_TRACK_LENGTH_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?) (km|m)")
_EVENT_IDENTITY_KEYS = (
    "series_id",
    "season_id",
    "race_week",
    "track_id",
    "car_class_id",
    "event_type",
    "track_config",
    "sim_build",
    "official",
)
_SESSION_EVENT_IDENTITY_KEYS = tuple(
    key for key in _EVENT_IDENTITY_KEYS if key != "car_class_id"
)
_EVENT_IDENTITY_SOURCE_FIELDS = {
    "series_id": "WeekendInfo.SeriesID",
    "season_id": "WeekendInfo.SeasonID",
    "race_week": "WeekendInfo.RaceWeek",
    "track_id": "WeekendInfo.TrackID",
    "car_class_id": "PlayerCarClass",
    "event_type": "WeekendInfo.EventType",
    "track_config": "WeekendInfo.TrackConfigName",
    "sim_build": "WeekendInfo.BuildVersion",
    "official": "WeekendInfo.Official",
}
_EVENT_IDENTITY_WEEKEND_KEYS = {
    "series_id": "SeriesID",
    "season_id": "SeasonID",
    "race_week": "RaceWeek",
    "track_id": "TrackID",
    "event_type": "EventType",
    "track_config": "TrackConfigName",
    "sim_build": "BuildVersion",
    "official": "Official",
}
_EVENT_IDENTITY_INTEGER_KEYS = frozenset(
    {"series_id", "season_id", "race_week", "track_id", "car_class_id"}
)
_EVENT_IDENTITY_STRING_KEYS = frozenset(
    {"event_type", "track_config", "sim_build"}
)
_EVENT_FIELD_STATUSES = frozenset({"PRESENT", "MISSING", "INVALID", "UNAVAILABLE"})
_TRAFFIC_OBSERVATION_SOURCE_FIELDS = (
    "SessionTick",
    "LapCompleted",
    "LapDistPct",
    "PlayerCarIdx",
    "CarIdxLapCompleted",
    "CarIdxLapDistPct",
    "CarIdxOnPitRoad",
    "CarIdxTrackSurface",
    _TRACK_LENGTH_SOURCE_FIELD,
)
_LAP_POSITION_SCALE = 1_000_000_000
_NORMALIZED_FIELD_SET = frozenset(NORMALIZED_SDK_FIELDS)
_COLLECTOR_SNAPSHOT_MEMORY_LIMIT = 64 * 1024 * 1024
_MAX_COLLECTOR_RECORD_CHARS = 64 * 1024 * 1024
_MAX_CAPTURE_MONOTONIC_US = (1 << 63) - 1
_DRIVER_INFO_KEY = "driverinfo"
_OPPONENT_ARRAY_FIELDS = frozenset(
    {
        "CarIdxLap",
        "CarIdxLapCompleted",
        "CarIdxLapDistPct",
        "CarIdxOnPitRoad",
        "CarIdxTrackSurface",
    }
)
_COLLECTOR_RECORD_TYPES = frozenset(
    {"run", "schema", "session_info", "event", "frame", "collector_receipt"}
)
_ENVELOPE_KEYS = frozenset(
    {"collector_contract_version", "record_type", "sequence"}
)
_RECORD_KEYS = {
    "run": _ENVELOPE_KEYS
    | {"source_id", "session_id", "sim_mode", "source_kind"},
    "schema": _ENVELOPE_KEYS
    | {
        "effective_buffer_tick",
        "schema_epoch",
        "schema_sha256",
        "tick_rate_hz",
        "variables",
    },
    "session_info": _ENVELOPE_KEYS
    | {
        "buffer_tick",
        "payload",
        "payload_scope",
        "payload_status",
        "redacted_paths",
        "schema_epoch",
        "session_epoch",
        "session_info_sha256",
        "session_info_update",
    },
    "event": _ENVELOPE_KEYS
    | {
        "buffer_tick",
        "capture_monotonic_us",
        "details",
        "event_kind",
        "schema_epoch",
        "session_epoch",
    },
    "frame": _ENVELOPE_KEYS
    | {
        "buffer_tick",
        "capture_monotonic_us",
        "read_errors",
        "schema_epoch",
        "session_epoch",
        "session_info_update",
        "sim_mode_raw",
        "values",
    },
    "collector_receipt": _ENVELOPE_KEYS | {"receipt"},
}
_DESCRIPTOR_KEYS = frozenset(
    {
        "name",
        "type_code",
        "dtype",
        "offset",
        "count",
        "count_as_time",
        "unit",
        "description",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "records_sha256",
        "completion_status",
        "semantic_record_count",
        "run_record_count",
        "frame_record_count",
        "event_record_count",
        "schema_record_count",
        "session_info_record_count",
        "samples_seen",
        "duplicate_sample_count",
        "duplicate_conflict_count",
        "dropped_tick_count",
        "stale_event_count",
        "session_reset_count",
        "schema_change_count",
        "schema_epoch_count",
        "session_epoch_count",
        "first_buffer_tick",
        "last_buffer_tick",
        "collector_contract_version",
    }
)
_RESET_REASONS = frozenset(
    {
        "BUFFER_TICK_REGRESSION",
        "SESSION_INFO_UPDATE_REGRESSION",
        "SESSION_NUM_CHANGED",
        "SESSIONTICK_REGRESSION",
        "SESSIONTIME_REGRESSION",
    }
)
_EVENT_DETAIL_KEYS = {
    "capture_clock_regression": frozenset({"previous_capture_monotonic_us"}),
    "source_stale": frozenset({"stale_after_us", "stale_for_us"}),
    "source_resumed": frozenset({"stale_for_us"}),
    "session_info_changed_without_update": frozenset({"session_info_update"}),
    "schema_changed": frozenset(
        {
            "previous_schema_sha256",
            "previous_tick_rate_hz",
            "schema_sha256",
            "tick_rate_hz",
        }
    ),
    "session_reset": frozenset({"reasons"}),
    "duplicate_sample": frozenset(
        {"conflict", "current_payload_sha256", "previous_payload_sha256"}
    ),
    "duplicate_tick_conflict": frozenset(
        {
            "buffer_tick",
            "current_payload_sha256",
            "previous_payload_sha256",
        }
    ),
    "tick_drop": frozenset(
        {"current_buffer_tick", "missing_tick_count", "previous_buffer_tick"}
    ),
}


class TelemetryAdapterError(ValueError):
    """Raised when a source cannot satisfy its declared input contract."""


class _IbtReaderLike(AbstractContextManager[Any], Protocol):
    @property
    def variable_names(self) -> tuple[str, ...]: ...

    @property
    def variables(self) -> tuple[Any, ...]: ...

    @property
    def metadata(self) -> Any: ...

    @property
    def source_sha256(self) -> str: ...

    def get_channels(self, names: Sequence[str]) -> dict[str, Any]: ...

    def public_session_context(self) -> Mapping[str, object]: ...

    def verify_source_unchanged(self) -> None: ...


def _validate_identifier(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(
            f"{name} must be a non-empty identifier without outer whitespace"
        )
    if len(value.encode("utf-8")) > 256 or any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} is not a reasonable identifier")
    return value


def _plain_int(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TelemetryAdapterError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise TelemetryAdapterError(f"{name} must be at least {minimum}")
    return value


def _sequence_length(value: object, name: str) -> int:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise TelemetryAdapterError(f"{name} is not an indexed channel")
    try:
        return len(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise TelemetryAdapterError(f"{name} is not an indexed channel") from exc


def _opponent_count_from_variables(variables: Sequence[Any]) -> int | None:
    counts: set[int] = set()
    for descriptor in variables:
        if getattr(descriptor, "name", None) not in _OPPONENT_ARRAY_FIELDS:
            continue
        count = getattr(descriptor, "count", None)
        counts.add(_plain_int(count, f"{descriptor.name}.count", minimum=1))
    if len(counts) > 1:
        raise TelemetryAdapterError("opponent array channel counts disagree in schema")
    return next(iter(counts)) if counts else None


def _iter_open_ibt_samples(
    reader: _IbtReaderLike,
    *,
    source_id: str,
    session_id: str,
    stale_after_s: float = 0.5,
    opponent_error_policy: Literal["degrade", "reject"] = "degrade",
) -> Iterator[TelemetrySample]:
    normalized_source_id = _validate_identifier(source_id, "source_id")
    normalized_session_id = _validate_identifier(session_id, "session_id")
    available = frozenset(reader.variable_names)
    selected = tuple(name for name in NORMALIZED_SDK_FIELDS if name in available)
    channels = reader.get_channels(selected)
    if set(channels) != set(selected):
        raise TelemetryAdapterError("IBT bulk reader returned an unexpected channel set")

    record_count = _plain_int(reader.metadata.record_count, "IBT record_count", minimum=1)
    for name, channel in channels.items():
        if _sequence_length(channel, f"IBT channel {name}") != record_count:
            raise TelemetryAdapterError(
                f"IBT channel {name} length does not match record_count"
            )

    expected_car_count = _opponent_count_from_variables(reader.variables)
    previous: TelemetrySample | None = None
    for index in range(record_count):
        try:
            frame = {name: channel[index] for name, channel in channels.items()}
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise TelemetryAdapterError(
                f"IBT channel indexing failed at record {index}"
            ) from exc
        sample = normalize_sdk_frame(
            frame,
            source_id=normalized_source_id,
            session_id=normalized_session_id,
            source_kind=SourceKind.IBT_OFFLINE,
            buffer_tick=index,
            previous=previous,
            stale_after_s=stale_after_s,
            expected_car_count=expected_car_count,
            opponent_error_policy=opponent_error_policy,
        )
        yield sample
        previous = sample


def iter_ibt_samples(
    path: str | Path,
    *,
    source_id: str,
    session_id: str,
    stale_after_s: float = 0.5,
    opponent_error_policy: Literal["degrade", "reject"] = "degrade",
    reader_factory: Callable[[str | Path], _IbtReaderLike] | None = None,
) -> Iterator[TelemetrySample]:
    """Yield normalized IBT records from one open file descriptor.

    ``source_id`` and ``session_id`` are mandatory because neither a pathname
    nor private IBT session metadata is a reliable analytical identifier.  The
    function reads only the normalized telemetry allowlist and never parses or
    exports ``SessionInfo``/``DriverInfo``.
    """

    factory = reader_factory if reader_factory is not None else IbtReader
    with factory(path) as reader:
        yield from _iter_open_ibt_samples(
            reader,
            source_id=source_id,
            session_id=session_id,
            stale_after_s=stale_after_s,
            opponent_error_policy=opponent_error_policy,
        )
        verifier = getattr(reader, "verify_source_unchanged", None)
        if callable(verifier):
            verifier()


@dataclass(frozen=True, slots=True)
class IbtInputEvidence:
    """Evidence derived from the same open IBT handle as normalized samples."""

    source_id: str
    session_id: str
    source_sha256: str
    byte_size: int
    record_count: int
    tick_rate_hz: int
    source_kind: SourceKind = SourceKind.IBT_OFFLINE
    completion_status: str = "COMPLETE"
    authenticity_status: str = "HASHED_LOCAL_FILE_NOT_AUTHENTICATED"

    def __post_init__(self) -> None:
        _validate_identifier(self.source_id, "IBT evidence source_id")
        _validate_identifier(self.session_id, "IBT evidence session_id")
        _sha256_text(self.source_sha256, "IBT evidence source_sha256")
        _plain_int(self.byte_size, "IBT evidence byte_size", minimum=1)
        _plain_int(self.record_count, "IBT evidence record_count", minimum=1)
        tick_rate = _plain_int(
            self.tick_rate_hz, "IBT evidence tick_rate_hz", minimum=1
        )
        if tick_rate > 360:
            raise TelemetryAdapterError("IBT evidence tick_rate_hz must not exceed 360")
        if self.source_kind is not SourceKind.IBT_OFFLINE:
            raise TelemetryAdapterError("IBT evidence source_kind must be IBT_OFFLINE")
        if self.completion_status != "COMPLETE":
            raise TelemetryAdapterError("IBT evidence completion_status must be COMPLETE")
        if self.authenticity_status != "HASHED_LOCAL_FILE_NOT_AUTHENTICATED":
            raise TelemetryAdapterError("IBT evidence authenticity_status is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "authenticity_status": self.authenticity_status,
            "byte_size": self.byte_size,
            "completion_status": self.completion_status,
            "record_count": self.record_count,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "source_sha256": self.source_sha256,
            "tick_rate_hz": self.tick_rate_hz,
        }


class TrackContextAvailability(StrEnum):
    """Whether a validated run carries a usable physical track length."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class TrackContextStatus(StrEnum):
    """Why track context is or is not admissible."""

    VERIFIED = "VERIFIED"
    TRACK_LENGTH_MISSING = "TRACK_LENGTH_MISSING"
    SESSION_INFO_PARTIAL = "SESSION_INFO_PARTIAL"
    SESSION_INFO_UNAVAILABLE = "SESSION_INFO_UNAVAILABLE"
    SESSION_INFO_MISSING = "SESSION_INFO_MISSING"


class TrackContextProvenance(StrEnum):
    """The validated source boundary that supplied the context."""

    IBT_SAME_HANDLE_SESSION_INFO = "IBT_SAME_HANDLE_SESSION_INFO"
    COLLECTOR_VALIDATED_SNAPSHOT = "COLLECTOR_VALIDATED_SNAPSHOT"


class EventIdentityAvailability(StrEnum):
    """Whether every source field needed by the M2 selector is usable."""

    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class EventIdentityStatus(StrEnum):
    """Why a collector event-identity projection is or is not complete."""

    VERIFIED = "VERIFIED"
    FIELDS_MISSING = "FIELDS_MISSING"
    FIELDS_INVALID = "FIELDS_INVALID"
    SESSION_INFO_PARTIAL = "SESSION_INFO_PARTIAL"
    SESSION_INFO_UNAVAILABLE = "SESSION_INFO_UNAVAILABLE"
    SESSION_INFO_MISSING = "SESSION_INFO_MISSING"


class EventIdentityProvenance(StrEnum):
    """The validated source boundary supplying the fixed identity projection."""

    COLLECTOR_VALIDATED_SNAPSHOT = "COLLECTOR_VALIDATED_SNAPSHOT"


class TrafficObservationAvailability(StrEnum):
    """Whether the latest validated frame supports a physical traffic map."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


class TrafficObservationStatus(StrEnum):
    """Why a same-capture traffic observation is or is not usable."""

    VERIFIED = "VERIFIED"
    SOURCE_STALE = "SOURCE_STALE"
    TRACK_CONTEXT_UNAVAILABLE = "TRACK_CONTEXT_UNAVAILABLE"
    DECISION_TICK_UNAVAILABLE = "DECISION_TICK_UNAVAILABLE"
    PLAYER_POSITION_UNAVAILABLE = "PLAYER_POSITION_UNAVAILABLE"
    PLAYER_POSITION_INVALID = "PLAYER_POSITION_INVALID"
    OPPONENTS_MISSING = "OPPONENTS_MISSING"
    OPPONENTS_INVALID = "OPPONENTS_INVALID"
    OPPONENT_FIELDS_MISSING = "OPPONENT_FIELDS_MISSING"
    OPPONENT_FIELDS_INVALID = "OPPONENT_FIELDS_INVALID"


class TrafficObservationProvenance(StrEnum):
    """The validated source boundary supplying the traffic projection."""

    COLLECTOR_VALIDATED_SNAPSHOT = "COLLECTOR_VALIDATED_SNAPSHOT"


def _context_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise TelemetryAdapterError("track context is not canonical-JSON-safe") from exc


def _parse_track_length_mm(raw: object) -> int:
    if type(raw) is not str or not 1 <= len(raw) <= 64:
        raise TelemetryAdapterError(
            f"{_TRACK_LENGTH_SOURCE_FIELD} must be a short metric length string"
        )
    match = _TRACK_LENGTH_PATTERN.fullmatch(raw)
    if match is None:
        raise TelemetryAdapterError(
            f"{_TRACK_LENGTH_SOURCE_FIELD} must use '<number> km' or '<number> m'"
        )
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:  # pragma: no cover - regex constrains the input
        raise TelemetryAdapterError(
            f"{_TRACK_LENGTH_SOURCE_FIELD} is not a valid decimal length"
        ) from exc
    factor = Decimal(1_000_000 if match.group(2) == "km" else 1_000)
    millimetres = amount * factor
    if millimetres != millimetres.to_integral_value():
        raise TelemetryAdapterError(
            f"{_TRACK_LENGTH_SOURCE_FIELD} has sub-millimetre precision"
        )
    normalized = int(millimetres)
    if not 100_000 < normalized <= 100_000_000:
        raise TelemetryAdapterError(
            f"{_TRACK_LENGTH_SOURCE_FIELD} must be greater than 100 m and at most 100 km"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class TrackContextEvidence:
    """Narrow, source-bound track context safe for the analytical plane."""

    track_length_mm: int | None
    source_field: str
    availability: TrackContextAvailability
    status: TrackContextStatus
    provenance: TrackContextProvenance
    source_binding_sha256: str
    contract_version: str = TRACK_CONTEXT_CONTRACT_VERSION
    context_sha256: str = dataclass_field(init=False)

    def __post_init__(self) -> None:
        if self.source_field != _TRACK_LENGTH_SOURCE_FIELD:
            raise TelemetryAdapterError("track context source_field is invalid")
        if not isinstance(self.availability, TrackContextAvailability):
            raise TypeError("track context availability is invalid")
        if not isinstance(self.status, TrackContextStatus):
            raise TypeError("track context status is invalid")
        if not isinstance(self.provenance, TrackContextProvenance):
            raise TypeError("track context provenance is invalid")
        _sha256_text(
            self.source_binding_sha256,
            "track context source_binding_sha256",
        )
        if self.contract_version != TRACK_CONTEXT_CONTRACT_VERSION:
            raise TelemetryAdapterError("track context contract version is invalid")
        if self.availability is TrackContextAvailability.AVAILABLE:
            length = _plain_int(
                self.track_length_mm,
                "track context track_length_mm",
                minimum=100_001,
            )
            if length > 100_000_000:
                raise TelemetryAdapterError(
                    "track context track_length_mm must not exceed 100 km"
                )
            if self.status is not TrackContextStatus.VERIFIED:
                raise TelemetryAdapterError(
                    "available track context must have VERIFIED status"
                )
        elif self.track_length_mm is not None:
            raise TelemetryAdapterError(
                "unavailable track context cannot carry a track length"
            )
        elif self.status is TrackContextStatus.VERIFIED:
            raise TelemetryAdapterError(
                "unavailable track context cannot have VERIFIED status"
            )
        material = {
            "availability": self.availability.value,
            "contract_version": self.contract_version,
            "provenance": self.provenance.value,
            "source_binding_sha256": self.source_binding_sha256,
            "source_field": self.source_field,
            "status": self.status.value,
            "track_length_mm": self.track_length_mm,
        }
        object.__setattr__(
            self,
            "context_sha256",
            hashlib.sha256(_context_json(material)).hexdigest(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "context_sha256": self.context_sha256,
            "contract_version": self.contract_version,
            "provenance": self.provenance.value,
            "source_binding_sha256": self.source_binding_sha256,
            "source_field": self.source_field,
            "status": self.status.value,
            "track_length_mm": self.track_length_mm,
        }


def _event_identity_text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise TelemetryAdapterError(f"event identity {name} is invalid")
    return value


def _validate_event_identity_value(name: str, value: object) -> object:
    if name in _EVENT_IDENTITY_INTEGER_KEYS:
        return _plain_int(value, f"event identity {name}", minimum=0)
    if name in _EVENT_IDENTITY_STRING_KEYS:
        return _event_identity_text(value, name)
    if name == "official":
        if type(value) is not bool:
            raise TelemetryAdapterError("event identity official must be boolean")
        return value
    raise AssertionError(f"unknown event identity field: {name}")


@dataclass(frozen=True, slots=True)
class EventIdentityContextEvidence:
    """Privacy-safe event identity extracted from one validated collector run.

    Values are limited to the exact M2 event selector.  ``DriverInfo`` is not
    retained or returned; the player's class comes from the direct
    ``PlayerCarClass`` telemetry variable saved by the existing collector.
    """

    identity: tuple[tuple[str, object | None], ...]
    field_statuses: tuple[tuple[str, str], ...]
    session_info_scope: str | None
    session_info_update: int | None
    availability: EventIdentityAvailability
    status: EventIdentityStatus
    provenance: EventIdentityProvenance
    source_binding_sha256: str
    contract_version: str = EVENT_IDENTITY_CONTEXT_CONTRACT_VERSION
    context_sha256: str = dataclass_field(init=False)

    def __post_init__(self) -> None:
        if tuple(name for name, _ in self.identity) != _EVENT_IDENTITY_KEYS:
            raise TelemetryAdapterError("event identity keys are invalid")
        if tuple(name for name, _ in self.field_statuses) != _EVENT_IDENTITY_KEYS:
            raise TelemetryAdapterError("event identity field-status keys are invalid")
        statuses = dict(self.field_statuses)
        if any(value not in _EVENT_FIELD_STATUSES for value in statuses.values()):
            raise TelemetryAdapterError("event identity field status is invalid")
        values = dict(self.identity)
        for name in _EVENT_IDENTITY_KEYS:
            value = values[name]
            if statuses[name] == "PRESENT":
                _validate_event_identity_value(name, value)
            elif value is not None:
                raise TelemetryAdapterError(
                    "unavailable event identity field cannot carry a value"
                )
        if self.session_info_scope not in {None, "FULL", "PARTIAL", "UNAVAILABLE"}:
            raise TelemetryAdapterError("event identity session-info scope is invalid")
        if self.session_info_update is not None:
            _plain_int(
                self.session_info_update,
                "event identity session_info_update",
                minimum=0,
            )
        if (self.session_info_scope is None) != (self.session_info_update is None):
            raise TelemetryAdapterError(
                "event identity session-info scope/update availability disagrees"
            )
        if not isinstance(self.availability, EventIdentityAvailability):
            raise TypeError("event identity availability is invalid")
        if not isinstance(self.status, EventIdentityStatus):
            raise TypeError("event identity status is invalid")
        if not isinstance(self.provenance, EventIdentityProvenance):
            raise TypeError("event identity provenance is invalid")
        _sha256_text(
            self.source_binding_sha256,
            "event identity source_binding_sha256",
        )
        if self.contract_version != EVENT_IDENTITY_CONTEXT_CONTRACT_VERSION:
            raise TelemetryAdapterError("event identity contract version is invalid")
        present_count = sum(value == "PRESENT" for value in statuses.values())
        invalid_count = sum(value == "INVALID" for value in statuses.values())
        if self.availability is EventIdentityAvailability.AVAILABLE:
            if present_count != len(_EVENT_IDENTITY_KEYS):
                raise TelemetryAdapterError(
                    "available event identity must contain every field"
                )
            if self.status is not EventIdentityStatus.VERIFIED:
                raise TelemetryAdapterError(
                    "available event identity must have VERIFIED status"
                )
        elif self.availability is EventIdentityAvailability.PARTIAL:
            if not 0 < present_count < len(_EVENT_IDENTITY_KEYS):
                raise TelemetryAdapterError(
                    "partial event identity must contain some fields"
                )
            expected_status = (
                EventIdentityStatus.FIELDS_INVALID
                if invalid_count
                else EventIdentityStatus.FIELDS_MISSING
            )
            if self.status is not expected_status:
                raise TelemetryAdapterError("partial event identity status is invalid")
        else:
            if present_count:
                raise TelemetryAdapterError(
                    "unavailable event identity cannot contain present fields"
                )
            expected_status = (
                EventIdentityStatus.FIELDS_INVALID
                if invalid_count
                else EventIdentityStatus.SESSION_INFO_PARTIAL
                if self.session_info_scope == "PARTIAL"
                else EventIdentityStatus.SESSION_INFO_UNAVAILABLE
                if self.session_info_scope == "UNAVAILABLE"
                else EventIdentityStatus.SESSION_INFO_MISSING
                if self.session_info_scope is None
                else EventIdentityStatus.FIELDS_MISSING
            )
            if self.status is not expected_status:
                raise TelemetryAdapterError(
                    "unavailable event identity status is invalid"
                )
        invalid_fields = [
            name for name in _EVENT_IDENTITY_KEYS if statuses[name] == "INVALID"
        ]
        missing_fields = [
            name
            for name in _EVENT_IDENTITY_KEYS
            if statuses[name] in {"MISSING", "UNAVAILABLE"}
        ]
        material = {
            "availability": self.availability.value,
            "contract_version": self.contract_version,
            "field_statuses": statuses,
            "identity": values,
            "invalid_fields": invalid_fields,
            "missing_fields": missing_fields,
            "provenance": self.provenance.value,
            "session_info_scope": self.session_info_scope,
            "session_info_update": self.session_info_update,
            "source_binding_sha256": self.source_binding_sha256,
            "source_fields": dict(_EVENT_IDENTITY_SOURCE_FIELDS),
            "status": self.status.value,
        }
        object.__setattr__(
            self,
            "context_sha256",
            hashlib.sha256(_context_json(material)).hexdigest(),
        )

    def to_dict(self) -> dict[str, object]:
        statuses = dict(self.field_statuses)
        material: dict[str, object] = {
            "availability": self.availability.value,
            "contract_version": self.contract_version,
            "field_statuses": statuses,
            "identity": dict(self.identity),
            "invalid_fields": [
                name for name in _EVENT_IDENTITY_KEYS if statuses[name] == "INVALID"
            ],
            "missing_fields": [
                name
                for name in _EVENT_IDENTITY_KEYS
                if statuses[name] in {"MISSING", "UNAVAILABLE"}
            ],
            "provenance": self.provenance.value,
            "session_info_scope": self.session_info_scope,
            "session_info_update": self.session_info_update,
            "source_binding_sha256": self.source_binding_sha256,
            "source_fields": dict(_EVENT_IDENTITY_SOURCE_FIELDS),
            "status": self.status.value,
        }
        return {**material, "context_sha256": self.context_sha256}


@dataclass(frozen=True, slots=True)
class TrafficNeighborEvidence:
    """One privacy-safe car-index neighbor on the circular track map."""

    car_idx: int
    distance_mm: int
    lap_position_ppb: int
    race_lap_delta: int | None

    def __post_init__(self) -> None:
        _plain_int(self.car_idx, "traffic neighbor car_idx", minimum=0)
        _plain_int(self.distance_mm, "traffic neighbor distance_mm", minimum=1)
        position = _plain_int(
            self.lap_position_ppb,
            "traffic neighbor lap_position_ppb",
            minimum=0,
        )
        if position >= _LAP_POSITION_SCALE:
            raise TelemetryAdapterError(
                "traffic neighbor lap_position_ppb must be below one lap"
            )
        if self.race_lap_delta is not None:
            _plain_int(self.race_lap_delta, "traffic neighbor race_lap_delta")

    def to_dict(self) -> dict[str, object]:
        return {
            "car_idx": self.car_idx,
            "distance_mm": self.distance_mm,
            "lap_position_ppb": self.lap_position_ppb,
            "race_lap_delta": self.race_lap_delta,
        }


@dataclass(frozen=True, slots=True)
class TrafficObservationContextEvidence:
    """Latest-frame physical traffic evidence from one validated capture.

    This contract deliberately stops at directly observed circular track
    distance.  It does not claim a time gap or a post-pit rejoin estimate.
    Car indices are retained, while names, customer IDs, teams, and the raw
    ``DriverInfo`` tree remain outside the analytical plane.
    """

    decision_tick: int | None
    player_car_idx: int | None
    player_lap_position_ppb: int | None
    track_length_mm: int | None
    eligible_opponent_count: int
    excluded_opponent_count: int
    overlap_opponent_count: int
    excluded_reason_counts: tuple[tuple[str, int], ...]
    nearest_ahead: TrafficNeighborEvidence | None
    nearest_behind: TrafficNeighborEvidence | None
    reasons: tuple[str, ...]
    availability: TrafficObservationAvailability
    status: TrafficObservationStatus
    provenance: TrafficObservationProvenance
    track_context_sha256: str
    source_binding_sha256: str
    contract_version: str = TRAFFIC_OBSERVATION_CONTEXT_CONTRACT_VERSION
    context_sha256: str = dataclass_field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.availability, TrafficObservationAvailability):
            raise TypeError("traffic observation availability is invalid")
        if not isinstance(self.status, TrafficObservationStatus):
            raise TypeError("traffic observation status is invalid")
        if not isinstance(self.provenance, TrafficObservationProvenance):
            raise TypeError("traffic observation provenance is invalid")
        if self.contract_version != TRAFFIC_OBSERVATION_CONTEXT_CONTRACT_VERSION:
            raise TelemetryAdapterError(
                "traffic observation contract version is invalid"
            )
        _sha256_text(
            self.track_context_sha256,
            "traffic observation track_context_sha256",
        )
        _sha256_text(
            self.source_binding_sha256,
            "traffic observation source_binding_sha256",
        )
        eligible = _plain_int(
            self.eligible_opponent_count,
            "traffic observation eligible_opponent_count",
            minimum=0,
        )
        excluded = _plain_int(
            self.excluded_opponent_count,
            "traffic observation excluded_opponent_count",
            minimum=0,
        )
        overlaps = _plain_int(
            self.overlap_opponent_count,
            "traffic observation overlap_opponent_count",
            minimum=0,
        )
        if overlaps > eligible:
            raise TelemetryAdapterError(
                "traffic overlap count exceeds eligible opponent count"
            )
        reason_names = tuple(name for name, _ in self.excluded_reason_counts)
        if reason_names != tuple(sorted(set(reason_names))):
            raise TelemetryAdapterError(
                "traffic exclusion reasons must be sorted and unique"
            )
        exclusion_total = 0
        for name, count in self.excluded_reason_counts:
            _event_identity_text(name, "traffic exclusion reason")
            exclusion_total += _plain_int(
                count,
                f"traffic exclusion reason {name}",
                minimum=1,
            )
        if exclusion_total != excluded:
            raise TelemetryAdapterError(
                "traffic exclusion counts do not equal excluded opponents"
            )
        if (
            type(self.reasons) is not tuple
            or any(type(reason) is not str or not reason for reason in self.reasons)
            or len(self.reasons) != len(set(self.reasons))
        ):
            raise TelemetryAdapterError(
                "traffic observation reasons must be unique strings"
            )

        if self.decision_tick is not None:
            _plain_int(self.decision_tick, "traffic observation decision_tick")
        if self.player_car_idx is not None:
            _plain_int(self.player_car_idx, "traffic observation player_car_idx", minimum=0)
        if self.player_lap_position_ppb is not None:
            position = _plain_int(
                self.player_lap_position_ppb,
                "traffic observation player_lap_position_ppb",
                minimum=0,
            )
            if position >= _LAP_POSITION_SCALE:
                raise TelemetryAdapterError(
                    "traffic player lap position must be below one lap"
                )
        if self.track_length_mm is not None:
            length = _plain_int(
                self.track_length_mm,
                "traffic observation track_length_mm",
                minimum=100_001,
            )
            if length > 100_000_000:
                raise TelemetryAdapterError(
                    "traffic observation track length must not exceed 100 km"
                )

        neighbors = (self.nearest_ahead, self.nearest_behind)
        if any(
            neighbor is not None and not isinstance(neighbor, TrafficNeighborEvidence)
            for neighbor in neighbors
        ):
            raise TypeError("traffic neighbors are invalid")
        for neighbor in neighbors:
            if neighbor is None:
                continue
            if self.player_car_idx is not None and neighbor.car_idx == self.player_car_idx:
                raise TelemetryAdapterError(
                    "traffic neighbor cannot be the player car"
                )
            if (
                self.track_length_mm is not None
                and neighbor.distance_mm > self.track_length_mm
            ):
                raise TelemetryAdapterError(
                    "traffic neighbor distance exceeds one track lap"
                )

        if self.availability is TrafficObservationAvailability.AVAILABLE:
            if (
                self.status is not TrafficObservationStatus.VERIFIED
                or self.reasons
                or self.decision_tick is None
                or self.player_car_idx is None
                or self.player_lap_position_ppb is None
                or self.track_length_mm is None
            ):
                raise TelemetryAdapterError(
                    "available traffic observation lacks verified core fields"
                )
            nonoverlap = eligible - overlaps
            if nonoverlap and any(neighbor is None for neighbor in neighbors):
                raise TelemetryAdapterError(
                    "available non-overlap traffic needs ahead and behind neighbors"
                )
            if not nonoverlap and any(neighbor is not None for neighbor in neighbors):
                raise TelemetryAdapterError(
                    "traffic without non-overlap cars cannot carry neighbors"
                )
        else:
            if self.status is TrafficObservationStatus.VERIFIED or not self.reasons:
                raise TelemetryAdapterError(
                    "unusable traffic observation needs an explicit reason"
                )
            if eligible or excluded or overlaps or self.excluded_reason_counts:
                raise TelemetryAdapterError(
                    "unusable traffic observation cannot carry opponent counts"
                )
            if any(neighbor is not None for neighbor in neighbors):
                raise TelemetryAdapterError(
                    "unusable traffic observation cannot carry neighbors"
                )

        material = self._material()
        object.__setattr__(
            self,
            "context_sha256",
            hashlib.sha256(_context_json(material)).hexdigest(),
        )

    def _material(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "contract_version": self.contract_version,
            "decision_tick": self.decision_tick,
            "eligible_opponent_count": self.eligible_opponent_count,
            "excluded_opponent_count": self.excluded_opponent_count,
            "excluded_reason_counts": dict(self.excluded_reason_counts),
            "nearest_ahead": (
                None if self.nearest_ahead is None else self.nearest_ahead.to_dict()
            ),
            "nearest_behind": (
                None if self.nearest_behind is None else self.nearest_behind.to_dict()
            ),
            "overlap_opponent_count": self.overlap_opponent_count,
            "player_car_idx": self.player_car_idx,
            "player_lap_position_ppb": self.player_lap_position_ppb,
            "provenance": self.provenance.value,
            "reasons": list(self.reasons),
            "source_binding_sha256": self.source_binding_sha256,
            "source_fields": list(_TRAFFIC_OBSERVATION_SOURCE_FIELDS),
            "status": self.status.value,
            "track_context_sha256": self.track_context_sha256,
            "track_length_mm": self.track_length_mm,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._material(), "context_sha256": self.context_sha256}


def _evidence_binding_sha256(evidence: object) -> str:
    serializer = getattr(evidence, "to_dict", None)
    if not callable(serializer):
        raise TypeError("validated input evidence must provide to_dict()")
    return hashlib.sha256(_context_json(serializer())).hexdigest()


def _track_context_evidence(
    *,
    raw_track_length: object | None,
    missing_status: TrackContextStatus,
    provenance: TrackContextProvenance,
    input_evidence: object,
) -> TrackContextEvidence:
    binding = _evidence_binding_sha256(input_evidence)
    if raw_track_length is None:
        return TrackContextEvidence(
            track_length_mm=None,
            source_field=_TRACK_LENGTH_SOURCE_FIELD,
            availability=TrackContextAvailability.UNAVAILABLE,
            status=missing_status,
            provenance=provenance,
            source_binding_sha256=binding,
        )
    return TrackContextEvidence(
        track_length_mm=_parse_track_length_mm(raw_track_length),
        source_field=_TRACK_LENGTH_SOURCE_FIELD,
        availability=TrackContextAvailability.AVAILABLE,
        status=TrackContextStatus.VERIFIED,
        provenance=provenance,
        source_binding_sha256=binding,
    )


def _ibt_track_context(
    reader: _IbtReaderLike,
    evidence: IbtInputEvidence,
) -> TrackContextEvidence:
    provider = getattr(reader, "public_session_context", None)
    if not callable(provider):
        raise TelemetryAdapterError("IBT reader cannot provide public session context")
    try:
        context = provider()
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
        raise TelemetryAdapterError("cannot read IBT public session context") from exc
    if not isinstance(context, Mapping):
        raise TelemetryAdapterError("IBT public session context must be a mapping")
    return _track_context_evidence(
        raw_track_length=context.get("track_length"),
        missing_status=TrackContextStatus.TRACK_LENGTH_MISSING,
        provenance=TrackContextProvenance.IBT_SAME_HANDLE_SESSION_INFO,
        input_evidence=evidence,
    )


_VALIDATED_RUN_TOKEN = object()


@dataclass(frozen=True, slots=True)
class _ValidatedRunState:
    evidence: object
    samples: Iterator[TelemetrySample]
    track_context: TrackContextEvidence
    event_identity_context: EventIdentityContextEvidence | None
    traffic_observation_context: TrafficObservationContextEvidence | None
    stale_after_s: float
    opponent_error_policy: Literal["degrade", "reject"]


_VALIDATED_RUN_REGISTRY: weakref.WeakKeyDictionary[object, _ValidatedRunState] = (
    weakref.WeakKeyDictionary()
)


def _register_validated_run(run: object, state: _ValidatedRunState) -> None:
    if not isinstance(state.track_context, TrackContextEvidence):
        raise TypeError("validated run track context is invalid")
    if state.track_context.source_binding_sha256 != _evidence_binding_sha256(
        state.evidence
    ):
        raise TelemetryAdapterError("track context is not bound to input evidence")
    if state.event_identity_context is not None and (
        state.event_identity_context.source_binding_sha256
        != _evidence_binding_sha256(state.evidence)
    ):
        raise TelemetryAdapterError(
            "event identity context is not bound to input evidence"
        )
    if state.traffic_observation_context is not None and (
        state.traffic_observation_context.source_binding_sha256
        != _evidence_binding_sha256(state.evidence)
    ):
        raise TelemetryAdapterError(
            "traffic observation context is not bound to input evidence"
        )
    _VALIDATED_RUN_REGISTRY[run] = state


def _unregister_validated_run(run: object) -> None:
    _VALIDATED_RUN_REGISTRY.pop(run, None)


def _validated_run_state(run: object) -> _ValidatedRunState | None:
    return _VALIDATED_RUN_REGISTRY.get(run)


class ValidatedIbtRun:
    """Opaque open-handle-bound IBT source created only by the adapter."""

    __slots__ = (
        "__weakref__",
        "_evidence",
        "_opponent_error_policy",
        "_samples",
        "_stale_after_s",
    )

    def __init__(
        self,
        evidence: IbtInputEvidence,
        samples: Iterator[TelemetrySample],
        *,
        stale_after_s: float,
        opponent_error_policy: Literal["degrade", "reject"],
        _token: object,
    ) -> None:
        if _token is not _VALIDATED_RUN_TOKEN:
            raise TypeError("ValidatedIbtRun can only be created by open_ibt_telemetry")
        object.__setattr__(self, "_evidence", evidence)
        object.__setattr__(self, "_samples", samples)
        object.__setattr__(self, "_stale_after_s", stale_after_s)
        object.__setattr__(self, "_opponent_error_policy", opponent_error_policy)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("validated runs are immutable")

    @property
    def evidence(self) -> IbtInputEvidence:
        return self._evidence

    @property
    def samples(self) -> Iterator[TelemetrySample]:
        return self._samples

    @property
    def stale_after_s(self) -> float:
        return self._stale_after_s

    @property
    def opponent_error_policy(self) -> Literal["degrade", "reject"]:
        return self._opponent_error_policy

    @property
    def track_context(self) -> TrackContextEvidence:
        return get_validated_track_context(self)

    def _adapter_token_is_valid(self) -> bool:
        return _validated_run_state(self) is not None


@contextmanager
def open_ibt_telemetry(
    path: str | Path,
    *,
    source_id: str,
    session_id: str,
    stale_after_s: float = 0.5,
    opponent_error_policy: Literal["degrade", "reject"] = "degrade",
    reader_factory: Callable[[str | Path], _IbtReaderLike] | None = None,
) -> Iterator[ValidatedIbtRun]:
    """Bind IBT identity, digest, metadata, and samples to one open handle.

    Holding the original descriptor prevents a symlink or rename swap from
    making evidence describe one file while the model consumes another.  A
    final mmap digest check also rejects in-place mutation during consumption.
    """

    normalized_source_id = _validate_identifier(source_id, "source_id")
    normalized_session_id = _validate_identifier(session_id, "session_id")
    factory = reader_factory if reader_factory is not None else IbtReader
    with factory(path) as reader:
        metadata = reader.metadata
        evidence = IbtInputEvidence(
            source_id=normalized_source_id,
            session_id=normalized_session_id,
            source_sha256=reader.source_sha256,
            byte_size=_plain_int(
                metadata.file_size_bytes, "IBT file_size_bytes", minimum=1
            ),
            record_count=_plain_int(
                metadata.record_count, "IBT record_count", minimum=1
            ),
            tick_rate_hz=_plain_int(
                metadata.tick_rate_hz, "IBT tick_rate_hz", minimum=1
            ),
        )
        track_context = _ibt_track_context(reader, evidence)
        try:
            run = ValidatedIbtRun(
                evidence=evidence,
                samples=_iter_open_ibt_samples(
                    reader,
                    source_id=normalized_source_id,
                    session_id=normalized_session_id,
                    stale_after_s=stale_after_s,
                    opponent_error_policy=opponent_error_policy,
                ),
                stale_after_s=stale_after_s,
                opponent_error_policy=opponent_error_policy,
                _token=_VALIDATED_RUN_TOKEN,
            )
            _register_validated_run(
                run,
                _ValidatedRunState(
                    evidence=evidence,
                    samples=run.samples,
                    track_context=track_context,
                    event_identity_context=None,
                    traffic_observation_context=None,
                    stale_after_s=stale_after_s,
                    opponent_error_policy=opponent_error_policy,
                ),
            )
            yield run
        finally:
            if "run" in locals():
                _unregister_validated_run(run)
            reader.verify_source_unchanged()


def _reject_json_constant(value: str) -> None:
    raise TelemetryAdapterError(f"non-standard JSON constant is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TelemetryAdapterError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_record(line: str, line_number: int) -> dict[str, object]:
    if len(line) > _MAX_COLLECTOR_RECORD_CHARS:
        raise TelemetryAdapterError(
            f"collector record at line {line_number} exceeds the size limit"
        )
    if not line.endswith("\n"):
        raise TelemetryAdapterError(
            f"incomplete collector JSON line at line {line_number}"
        )
    try:
        value = json.loads(
            line,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except TelemetryAdapterError:
        raise
    except (json.JSONDecodeError, OverflowError, RecursionError, UnicodeError) as exc:
        raise TelemetryAdapterError(f"invalid collector JSON at line {line_number}") from exc
    if not isinstance(value, dict):
        raise TelemetryAdapterError(f"collector record at line {line_number} is not an object")
    return value


@contextmanager
def _collector_handle(path: str | Path) -> Iterator[TextIO]:
    try:
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            yield handle
    except (OSError, UnicodeError) as exc:
        raise TelemetryAdapterError(f"cannot read collector JSONL: {path}") from exc


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise TelemetryAdapterError("collector record is not canonical-JSON-safe") from exc


def _collector_identifier(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise TelemetryAdapterError(f"collector run {label} is invalid")
    if len(value.encode("utf-8")) > 256 or any(ord(character) < 32 for character in value):
        raise TelemetryAdapterError(f"collector run {label} is invalid")
    return value


def _optional_plain_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _plain_int(value, name, minimum=0)


def _capture_monotonic_us(value: object, name: str) -> int | None:
    capture_us = _optional_plain_int(value, name)
    if capture_us is not None and capture_us > _MAX_CAPTURE_MONOTONIC_US:
        raise TelemetryAdapterError(f"{name} exceeds signed 64-bit microseconds")
    return capture_us


def _normalized_session_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _driver_info_key_count(value: object) -> int:
    if isinstance(value, Mapping):
        return sum(
            (1 if _normalized_session_key(key) == _DRIVER_INFO_KEY else 0)
            + _driver_info_key_count(item)
            for key, item in value.items()
            if isinstance(key, str)
        )
    if isinstance(value, list):
        return sum(_driver_info_key_count(item) for item in value)
    return 0


def _redacted_driver_info_path_count(paths: Sequence[str]) -> int:
    return sum(
        _normalized_session_key(path.rsplit(".", 1)[-1]) == _DRIVER_INFO_KEY
        for path in paths
    )


def _sha256_text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TelemetryAdapterError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _require_exact_keys(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    parts: list[str] = []
    if missing:
        parts.append(f"missing {', '.join(missing)}")
    if extra:
        parts.append(f"unexpected {', '.join(extra)}")
    raise TelemetryAdapterError(f"{label} fields are invalid: {'; '.join(parts)}")


def _descriptor_from_record(value: object, *, index: int) -> VariableDescriptor:
    if not isinstance(value, dict) or set(value) != _DESCRIPTOR_KEYS:
        raise TelemetryAdapterError(f"schema variable {index} has an invalid descriptor")
    name = value["name"]
    type_code = value["type_code"]
    dtype = value["dtype"]
    offset = value["offset"]
    count = value["count"]
    count_as_time = value["count_as_time"]
    unit = value["unit"]
    description = value["description"]
    if not isinstance(name, str) or not name:
        raise TelemetryAdapterError(f"schema variable {index} has an invalid name")
    if _plain_int(type_code, f"schema variable {name} type_code", minimum=0) > 5:
        raise TelemetryAdapterError(f"schema variable {name} has an unknown type_code")
    _plain_int(offset, f"schema variable {name} offset", minimum=0)
    _plain_int(count, f"schema variable {name} count", minimum=1)
    if not isinstance(count_as_time, bool):
        raise TelemetryAdapterError(f"schema variable {name} count_as_time must be boolean")
    if not all(isinstance(item, str) for item in (dtype, unit, description)):
        raise TelemetryAdapterError(f"schema variable {name} text fields must be strings")
    return VariableDescriptor(
        name=name,
        type_code=type_code,
        dtype=dtype,
        offset=offset,
        count=count,
        count_as_time=count_as_time,
        unit=unit,
        description=description,
    )


@dataclass(frozen=True, slots=True)
class _Schema:
    names: frozenset[str]
    expected_car_count: int | None
    digest: str
    tick_rate_hz: int
    effective_buffer_tick: int


@dataclass(frozen=True, slots=True)
class CollectorInputEvidence:
    """Validated, self-consistent collector provenance and quality evidence.

    The embedded receipt is not a signature.  This object proves that one
    snapshotted file is internally consistent; authenticity needs an external
    immutable hash or signature anchor.
    """

    source_id: str
    session_id: str
    source_kind: SourceKind
    sim_mode: str
    completion_status: str
    semantic_record_count: int
    records_sha256: str
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
    tick_rate_hz_values: tuple[int, ...] = ()
    first_capture_monotonic_us: int | None = None
    last_capture_monotonic_us: int | None = None
    capture_span_us: int | None = None
    capture_clock_regression_count: int = 0
    read_error_frame_count: int = 0
    read_error_field_count: int = 0
    driver_info_key_count: int = 0
    redacted_driver_info_path_count: int = 0
    session_info_scope_counts: tuple[tuple[str, int], ...] = ()
    collector_contract_version: str = COLLECTOR_CONTRACT_VERSION
    authenticity_status: str = "SELF_CONSISTENT_NOT_AUTHENTICATED"

    def __post_init__(self) -> None:
        _collector_identifier(self.source_id, "source_id")
        _collector_identifier(self.session_id, "session_id")
        if self.source_kind not in {SourceKind.SDK_LIVE, SourceKind.REPLAY_SDK_PROXY}:
            raise TelemetryAdapterError("collector evidence source_kind is invalid")
        expected_mode = (
            "full" if self.source_kind is SourceKind.SDK_LIVE else "replay"
        )
        if self.sim_mode != expected_mode:
            raise TelemetryAdapterError(
                "collector evidence sim_mode and source_kind do not agree"
            )
        if self.completion_status not in {"COMPLETE", "INCOMPLETE_RECOVERY"}:
            raise TelemetryAdapterError("collector evidence completion_status is invalid")
        if self.collector_contract_version != COLLECTOR_CONTRACT_VERSION:
            raise TelemetryAdapterError("collector evidence contract version is invalid")
        if self.authenticity_status != "SELF_CONSISTENT_NOT_AUTHENTICATED":
            raise TelemetryAdapterError("collector evidence authenticity_status is invalid")
        _sha256_text(self.records_sha256, "collector evidence records_sha256")
        for name in (
            "semantic_record_count",
            "frame_record_count",
            "event_record_count",
            "schema_record_count",
            "session_info_record_count",
            "samples_seen",
            "duplicate_sample_count",
            "duplicate_conflict_count",
            "dropped_tick_count",
            "stale_event_count",
            "session_reset_count",
            "schema_change_count",
            "schema_epoch_count",
            "session_epoch_count",
            "capture_clock_regression_count",
            "read_error_frame_count",
            "read_error_field_count",
            "driver_info_key_count",
            "redacted_driver_info_path_count",
        ):
            _plain_int(getattr(self, name), f"collector evidence {name}", minimum=0)
        if self.completion_status == "COMPLETE" and self.frame_record_count < 1:
            raise TelemetryAdapterError("COMPLETE collector evidence requires frames")
        first_tick = _optional_plain_int(
            self.first_buffer_tick, "collector evidence first_buffer_tick"
        )
        last_tick = _optional_plain_int(
            self.last_buffer_tick, "collector evidence last_buffer_tick"
        )
        if (first_tick is None) != (last_tick is None):
            raise TelemetryAdapterError("collector evidence buffer tick bounds disagree")
        if (
            first_tick is not None
            and last_tick is not None
            and last_tick < first_tick
            and self.session_reset_count == 0
        ):
            raise TelemetryAdapterError("collector evidence buffer tick bounds regress")
        first_capture = _capture_monotonic_us(
            self.first_capture_monotonic_us,
            "collector evidence first_capture_monotonic_us",
        )
        last_capture = _capture_monotonic_us(
            self.last_capture_monotonic_us,
            "collector evidence last_capture_monotonic_us",
        )
        if (first_capture is None) != (last_capture is None):
            raise TelemetryAdapterError("collector evidence capture bounds disagree")
        if self.capture_span_us is not None:
            span = _plain_int(
                self.capture_span_us,
                "collector evidence capture_span_us",
                minimum=0,
            )
            if (
                first_capture is None
                or last_capture is None
                or self.capture_clock_regression_count
                or span != last_capture - first_capture
            ):
                raise TelemetryAdapterError("collector evidence capture_span_us is invalid")
        if type(self.tick_rate_hz_values) is not tuple:
            raise TelemetryAdapterError("collector evidence tick rates must be a tuple")
        if self.tick_rate_hz_values != tuple(sorted(set(self.tick_rate_hz_values))):
            raise TelemetryAdapterError(
                "collector evidence tick rates must be sorted and unique"
            )
        for tick_rate in self.tick_rate_hz_values:
            if type(tick_rate) is not int or not 1 <= tick_rate <= 360:
                raise TelemetryAdapterError("collector evidence tick rate is invalid")
        if type(self.session_info_scope_counts) is not tuple:
            raise TelemetryAdapterError(
                "collector evidence session_info_scope_counts must be a tuple"
            )
        scopes: list[str] = []
        for scope, count in self.session_info_scope_counts:
            if scope not in {"FULL", "PARTIAL", "UNAVAILABLE"}:
                raise TelemetryAdapterError("collector evidence session scope is invalid")
            _plain_int(count, f"collector evidence {scope} scope count", minimum=0)
            scopes.append(scope)
        if scopes != sorted(set(scopes)):
            raise TelemetryAdapterError(
                "collector evidence session scopes must be sorted and unique"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "authenticity_status": self.authenticity_status,
            "collector_contract_version": self.collector_contract_version,
            "completion_status": self.completion_status,
            "capture_clock_regression_count": self.capture_clock_regression_count,
            "capture_span_us": self.capture_span_us,
            "driver_info_key_count": self.driver_info_key_count,
            "dropped_tick_count": self.dropped_tick_count,
            "duplicate_conflict_count": self.duplicate_conflict_count,
            "duplicate_sample_count": self.duplicate_sample_count,
            "event_record_count": self.event_record_count,
            "first_buffer_tick": self.first_buffer_tick,
            "first_capture_monotonic_us": self.first_capture_monotonic_us,
            "frame_record_count": self.frame_record_count,
            "last_buffer_tick": self.last_buffer_tick,
            "last_capture_monotonic_us": self.last_capture_monotonic_us,
            "read_error_field_count": self.read_error_field_count,
            "read_error_frame_count": self.read_error_frame_count,
            "records_sha256": self.records_sha256,
            "redacted_driver_info_path_count": self.redacted_driver_info_path_count,
            "samples_seen": self.samples_seen,
            "schema_change_count": self.schema_change_count,
            "schema_epoch_count": self.schema_epoch_count,
            "schema_record_count": self.schema_record_count,
            "semantic_record_count": self.semantic_record_count,
            "session_epoch_count": self.session_epoch_count,
            "session_id": self.session_id,
            "session_info_record_count": self.session_info_record_count,
            "session_info_scope_counts": dict(self.session_info_scope_counts),
            "session_reset_count": self.session_reset_count,
            "sim_mode": self.sim_mode,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "stale_event_count": self.stale_event_count,
            "tick_rate_hz_values": list(self.tick_rate_hz_values),
        }


class ValidatedCollectorRun:
    """Opaque validated collector snapshot created only by the adapter."""

    __slots__ = (
        "__weakref__",
        "_evidence",
        "_opponent_error_policy",
        "_samples",
        "_stale_after_s",
    )

    def __init__(
        self,
        evidence: CollectorInputEvidence,
        samples: Iterator[TelemetrySample],
        *,
        stale_after_s: float,
        opponent_error_policy: Literal["degrade", "reject"],
        _token: object,
    ) -> None:
        if _token is not _VALIDATED_RUN_TOKEN:
            raise TypeError("ValidatedCollectorRun can only be created by its adapter")
        object.__setattr__(self, "_evidence", evidence)
        object.__setattr__(self, "_samples", samples)
        object.__setattr__(self, "_stale_after_s", stale_after_s)
        object.__setattr__(self, "_opponent_error_policy", opponent_error_policy)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("validated runs are immutable")

    @property
    def evidence(self) -> CollectorInputEvidence:
        return self._evidence

    @property
    def samples(self) -> Iterator[TelemetrySample]:
        return self._samples

    @property
    def stale_after_s(self) -> float:
        return self._stale_after_s

    @property
    def opponent_error_policy(self) -> Literal["degrade", "reject"]:
        return self._opponent_error_policy

    @property
    def track_context(self) -> TrackContextEvidence:
        return get_validated_track_context(self)

    @property
    def event_identity_context(self) -> EventIdentityContextEvidence:
        return get_validated_event_identity_context(self)

    @property
    def traffic_observation_context(self) -> TrafficObservationContextEvidence:
        return get_validated_traffic_observation_context(self)

    def _adapter_token_is_valid(self) -> bool:
        return _validated_run_state(self) is not None


def get_validated_track_context(
    run: ValidatedIbtRun | ValidatedCollectorRun,
) -> TrackContextEvidence:
    """Return context only while an adapter-created run is active."""

    if type(run) not in {ValidatedIbtRun, ValidatedCollectorRun}:
        raise TypeError("run must be a validated IBT or collector run")
    state = _validated_run_state(run)
    if state is None:
        raise TelemetryAdapterError(
            "track context requires an active adapter-created validated run"
        )
    return state.track_context


def get_validated_event_identity_context(
    run: ValidatedCollectorRun,
) -> EventIdentityContextEvidence:
    """Return the fixed identity projection while a collector run is active."""

    if type(run) is not ValidatedCollectorRun:
        raise TypeError("run must be a validated collector run")
    state = _validated_run_state(run)
    if state is None or state.event_identity_context is None:
        raise TelemetryAdapterError(
            "event identity requires an active adapter-created collector run"
        )
    return state.event_identity_context


def get_validated_traffic_observation_context(
    run: ValidatedCollectorRun,
) -> TrafficObservationContextEvidence:
    """Return latest-frame traffic evidence while a validated run is active."""

    if type(run) is not ValidatedCollectorRun:
        raise TypeError("run must be a validated collector run")
    state = _validated_run_state(run)
    if state is None or state.traffic_observation_context is None:
        raise TelemetryAdapterError(
            "traffic observation requires an active adapter-created collector run"
        )
    return state.traffic_observation_context


@dataclass(frozen=True, slots=True)
class _RawFrameEvidence:
    buffer_tick: int
    session_info_update: int
    values: dict[str, object]
    payload_sha256: str


def _finite_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not (-float("inf") < value < float("inf")):
        return None
    return value


def _reset_reasons(
    previous: _RawFrameEvidence,
    *,
    buffer_tick: int,
    session_info_update: int,
    values: Mapping[str, object],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if buffer_tick < previous.buffer_tick:
        reasons.append("BUFFER_TICK_REGRESSION")
    if session_info_update < previous.session_info_update:
        reasons.append("SESSION_INFO_UPDATE_REGRESSION")
    previous_session = _finite_number(previous.values.get("SessionNum"))
    current_session = _finite_number(values.get("SessionNum"))
    if (
        previous_session is not None
        and current_session is not None
        and current_session != previous_session
    ):
        reasons.append("SESSION_NUM_CHANGED")
    for field in ("SessionTick", "SessionTime"):
        before = _finite_number(previous.values.get(field))
        after = _finite_number(values.get(field))
        if before is not None and after is not None and after < before:
            reasons.append(f"{field.upper()}_REGRESSION")
    return tuple(reasons)


def _full_session_track_length(payload: Mapping[str, object]) -> object | None:
    weekend = payload.get("WeekendInfo")
    if weekend is None:
        return None
    if not isinstance(weekend, Mapping):
        raise TelemetryAdapterError("FULL session_info WeekendInfo must be an object")
    return weekend.get("TrackLength")


def _event_identity_field(name: str, raw: object) -> tuple[str, object | None]:
    if name in _EVENT_IDENTITY_INTEGER_KEYS:
        if type(raw) is int and raw >= 0:
            return "PRESENT", raw
        return "INVALID", None
    if name in _EVENT_IDENTITY_STRING_KEYS:
        if (
            type(raw) is str
            and raw
            and raw == raw.strip()
            and len(raw.encode("utf-8")) <= 256
            and not any(ord(character) < 32 for character in raw)
        ):
            return "PRESENT", raw
        return "INVALID", None
    if name == "official":
        if type(raw) is bool:
            return "PRESENT", raw
        if type(raw) is int and raw in {0, 1}:
            return "PRESENT", bool(raw)
        return "INVALID", None
    raise AssertionError(f"unknown event identity field: {name}")


def _session_event_identity(
    payload: Mapping[str, object] | None,
    *,
    scope: str,
) -> tuple[dict[str, object | None], dict[str, str]]:
    values = {name: None for name in _SESSION_EVENT_IDENTITY_KEYS}
    unavailable_status = "MISSING" if scope == "FULL" else "UNAVAILABLE"
    statuses = {
        name: unavailable_status for name in _SESSION_EVENT_IDENTITY_KEYS
    }
    if scope != "FULL" or payload is None:
        return values, statuses
    weekend = payload.get("WeekendInfo")
    if weekend is None:
        return values, statuses
    if not isinstance(weekend, Mapping):
        raise TelemetryAdapterError("FULL session_info WeekendInfo must be an object")
    for name, source_key in _EVENT_IDENTITY_WEEKEND_KEYS.items():
        if source_key not in weekend:
            continue
        statuses[name], values[name] = _event_identity_field(
            name, weekend[source_key]
        )
    return values, statuses


def _traffic_observation_failure(
    *,
    input_evidence: CollectorInputEvidence,
    track_context: TrackContextEvidence,
    availability: TrafficObservationAvailability,
    status: TrafficObservationStatus,
    reason: str,
    decision_tick: int | None = None,
    player_car_idx: int | None = None,
    player_lap_position_ppb: int | None = None,
) -> TrafficObservationContextEvidence:
    return TrafficObservationContextEvidence(
        decision_tick=decision_tick,
        player_car_idx=player_car_idx,
        player_lap_position_ppb=player_lap_position_ppb,
        track_length_mm=track_context.track_length_mm,
        eligible_opponent_count=0,
        excluded_opponent_count=0,
        overlap_opponent_count=0,
        excluded_reason_counts=(),
        nearest_ahead=None,
        nearest_behind=None,
        reasons=(reason,),
        availability=availability,
        status=status,
        provenance=TrafficObservationProvenance.COLLECTOR_VALIDATED_SNAPSHOT,
        track_context_sha256=track_context.context_sha256,
        source_binding_sha256=_evidence_binding_sha256(input_evidence),
    )


def _lap_position_ppb(value: float) -> int:
    return round((value % 1.0) * _LAP_POSITION_SCALE) % _LAP_POSITION_SCALE


def _traffic_observation_context_evidence(
    sample: TelemetrySample,
    *,
    track_context: TrackContextEvidence,
    input_evidence: CollectorInputEvidence,
) -> TrafficObservationContextEvidence:
    """Project direct latest-frame positions without estimating rejoin time."""

    tick_field = sample.session.session_tick
    decision_tick = (
        tick_field.value
        if tick_field.presence is Presence.PRESENT
        and tick_field.provenance is Provenance.SDK_DIRECT
        and type(tick_field.value) is int
        and tick_field.value >= 0
        else None
    )
    stale = sample.quality.stale
    if stale.presence is Presence.PRESENT and stale.value is True:
        return _traffic_observation_failure(
            input_evidence=input_evidence,
            track_context=track_context,
            availability=TrafficObservationAvailability.INVALID,
            status=TrafficObservationStatus.SOURCE_STALE,
            reason="LATEST_FRAME_STALE",
            decision_tick=decision_tick,
        )
    if decision_tick is None:
        return _traffic_observation_failure(
            input_evidence=input_evidence,
            track_context=track_context,
            availability=TrafficObservationAvailability.UNAVAILABLE,
            status=TrafficObservationStatus.DECISION_TICK_UNAVAILABLE,
            reason="SESSION_TICK_NOT_SDK_DIRECT_PRESENT",
        )
    if (
        track_context.availability is not TrackContextAvailability.AVAILABLE
        or track_context.track_length_mm is None
    ):
        return _traffic_observation_failure(
            input_evidence=input_evidence,
            track_context=track_context,
            availability=TrafficObservationAvailability.UNAVAILABLE,
            status=TrafficObservationStatus.TRACK_CONTEXT_UNAVAILABLE,
            reason=track_context.status.value,
            decision_tick=decision_tick,
        )

    player_index_field = sample.opponents.player_car_idx
    player_pct_field = sample.lap.lap_distance_pct
    player_missing = (
        player_index_field.presence is Presence.MISSING
        or player_pct_field.presence is Presence.MISSING
    )
    player_valid = (
        player_index_field.presence is Presence.PRESENT
        and player_index_field.provenance is Provenance.SDK_DIRECT
        and type(player_index_field.value) is int
        and player_index_field.value >= 0
        and player_pct_field.presence is Presence.PRESENT
        and player_pct_field.provenance is Provenance.SDK_DIRECT
        and isinstance(player_pct_field.value, (int, float))
        and not isinstance(player_pct_field.value, bool)
        and 0.0 <= float(player_pct_field.value) <= 1.0
    )
    if not player_valid:
        return _traffic_observation_failure(
            input_evidence=input_evidence,
            track_context=track_context,
            availability=(
                TrafficObservationAvailability.UNAVAILABLE
                if player_missing
                else TrafficObservationAvailability.INVALID
            ),
            status=(
                TrafficObservationStatus.PLAYER_POSITION_UNAVAILABLE
                if player_missing
                else TrafficObservationStatus.PLAYER_POSITION_INVALID
            ),
            reason=(
                "PLAYER_POSITION_FIELDS_MISSING"
                if player_missing
                else "PLAYER_POSITION_FIELDS_INVALID"
            ),
            decision_tick=decision_tick,
        )
    player_car_idx = int(player_index_field.value)
    player_position = _lap_position_ppb(float(player_pct_field.value))

    opponents = sample.opponents
    if opponents.presence is Presence.MISSING:
        return _traffic_observation_failure(
            input_evidence=input_evidence,
            track_context=track_context,
            availability=TrafficObservationAvailability.UNAVAILABLE,
            status=TrafficObservationStatus.OPPONENTS_MISSING,
            reason="OPPONENT_ARRAYS_MISSING",
            decision_tick=decision_tick,
            player_car_idx=player_car_idx,
            player_lap_position_ppb=player_position,
        )
    if (
        opponents.presence is Presence.INVALID
        or opponents.provenance is not Provenance.SDK_DIRECT
        or opponents.issues
    ):
        return _traffic_observation_failure(
            input_evidence=input_evidence,
            track_context=track_context,
            availability=TrafficObservationAvailability.INVALID,
            status=TrafficObservationStatus.OPPONENTS_INVALID,
            reason="OPPONENT_ARRAYS_INVALID",
            decision_tick=decision_tick,
            player_car_idx=player_car_idx,
            player_lap_position_ppb=player_position,
        )

    required_fields = tuple(
        field
        for opponent in opponents.entries
        for field in (
            opponent.car_idx,
            opponent.lap_distance_pct,
            opponent.on_pit_road,
            opponent.track_surface,
        )
    )
    if any(field.presence is Presence.MISSING for field in required_fields):
        return _traffic_observation_failure(
            input_evidence=input_evidence,
            track_context=track_context,
            availability=TrafficObservationAvailability.UNAVAILABLE,
            status=TrafficObservationStatus.OPPONENT_FIELDS_MISSING,
            reason="REQUIRED_OPPONENT_FIELD_MISSING",
            decision_tick=decision_tick,
            player_car_idx=player_car_idx,
            player_lap_position_ppb=player_position,
        )
    if any(field.presence is not Presence.PRESENT for field in required_fields):
        return _traffic_observation_failure(
            input_evidence=input_evidence,
            track_context=track_context,
            availability=TrafficObservationAvailability.INVALID,
            status=TrafficObservationStatus.OPPONENT_FIELDS_INVALID,
            reason="REQUIRED_OPPONENT_FIELD_INVALID",
            decision_tick=decision_tick,
            player_car_idx=player_car_idx,
            player_lap_position_ppb=player_position,
        )

    player_laps = sample.lap.laps_completed
    player_laps_completed = (
        player_laps.value
        if player_laps.presence is Presence.PRESENT
        and player_laps.provenance is Provenance.SDK_DIRECT
        and type(player_laps.value) is int
        else None
    )
    excluded: Counter[str] = Counter()
    ahead: list[tuple[int, int, TrafficNeighborEvidence]] = []
    behind: list[tuple[int, int, TrafficNeighborEvidence]] = []
    eligible = 0
    overlaps = 0
    track_length_mm = track_context.track_length_mm
    if track_length_mm is None:  # pragma: no cover - availability invariant
        raise AssertionError("available track context lost its track length")
    for opponent in opponents.entries:
        if (
            opponent.car_idx.provenance is not Provenance.DERIVED
            or opponent.lap_distance_pct.provenance is not Provenance.SDK_DIRECT
            or opponent.on_pit_road.provenance is not Provenance.SDK_DIRECT
            or opponent.track_surface.provenance is not Provenance.SDK_DIRECT
            or type(opponent.car_idx.value) is not int
            or not isinstance(opponent.lap_distance_pct.value, (int, float))
            or isinstance(opponent.lap_distance_pct.value, bool)
            or type(opponent.on_pit_road.value) is not bool
            or type(opponent.track_surface.value) is not int
        ):
            return _traffic_observation_failure(
                input_evidence=input_evidence,
                track_context=track_context,
                availability=TrafficObservationAvailability.INVALID,
                status=TrafficObservationStatus.OPPONENT_FIELDS_INVALID,
                reason="REQUIRED_OPPONENT_FIELD_PROVENANCE_OR_TYPE_INVALID",
                decision_tick=decision_tick,
                player_car_idx=player_car_idx,
                player_lap_position_ppb=player_position,
            )
        if opponent.track_surface.value != 3:
            excluded["NOT_ON_TRACK_SURFACE"] += 1
            continue
        if opponent.on_pit_road.value is True:
            excluded["ON_PIT_ROAD"] += 1
            continue
        opponent_pct = float(opponent.lap_distance_pct.value)
        if not 0.0 <= opponent_pct <= 1.0:
            excluded["LAP_DISTANCE_OUT_OF_RANGE"] += 1
            continue

        eligible += 1
        car_idx = int(opponent.car_idx.value)
        opponent_position = _lap_position_ppb(opponent_pct)
        ahead_units = (opponent_position - player_position) % _LAP_POSITION_SCALE
        behind_units = (player_position - opponent_position) % _LAP_POSITION_SCALE
        if ahead_units == 0:
            overlaps += 1
            continue
        opponent_laps = opponent.laps_completed
        race_lap_delta = (
            int(opponent_laps.value) - int(player_laps_completed)
            if player_laps_completed is not None
            and opponent_laps.presence is Presence.PRESENT
            and opponent_laps.provenance is Provenance.SDK_DIRECT
            and type(opponent_laps.value) is int
            else None
        )

        def distance_mm(units: int) -> int:
            rounded = (
                units * track_length_mm + (_LAP_POSITION_SCALE // 2)
            ) // _LAP_POSITION_SCALE
            return max(1, min(track_length_mm, rounded))

        ahead_neighbor = TrafficNeighborEvidence(
            car_idx=car_idx,
            distance_mm=distance_mm(ahead_units),
            lap_position_ppb=opponent_position,
            race_lap_delta=race_lap_delta,
        )
        behind_neighbor = TrafficNeighborEvidence(
            car_idx=car_idx,
            distance_mm=distance_mm(behind_units),
            lap_position_ppb=opponent_position,
            race_lap_delta=race_lap_delta,
        )
        ahead.append((ahead_neighbor.distance_mm, car_idx, ahead_neighbor))
        behind.append((behind_neighbor.distance_mm, car_idx, behind_neighbor))

    nearest_ahead = min(ahead)[2] if ahead else None
    nearest_behind = min(behind)[2] if behind else None
    return TrafficObservationContextEvidence(
        decision_tick=decision_tick,
        player_car_idx=player_car_idx,
        player_lap_position_ppb=player_position,
        track_length_mm=track_length_mm,
        eligible_opponent_count=eligible,
        excluded_opponent_count=sum(excluded.values()),
        overlap_opponent_count=overlaps,
        excluded_reason_counts=tuple(sorted(excluded.items())),
        nearest_ahead=nearest_ahead,
        nearest_behind=nearest_behind,
        reasons=(),
        availability=TrafficObservationAvailability.AVAILABLE,
        status=TrafficObservationStatus.VERIFIED,
        provenance=TrafficObservationProvenance.COLLECTOR_VALIDATED_SNAPSHOT,
        track_context_sha256=track_context.context_sha256,
        source_binding_sha256=_evidence_binding_sha256(input_evidence),
    )


class _CollectorValidator:
    def __init__(
        self,
        *,
        stale_after_s: float,
        opponent_error_policy: Literal["degrade", "reject"],
        require_receipt: bool,
    ) -> None:
        self.stale_after_s = stale_after_s
        self.opponent_error_policy = opponent_error_policy
        self.require_receipt = require_receipt

        self.expected_sequence = 0
        self.digest = hashlib.sha256()
        self.receipt_seen = False
        self.run_seen = False
        self.source_id: str | None = None
        self.session_id: str | None = None
        self.source_kind: SourceKind | None = None
        self.sim_mode: str | None = None

        self.schemas: dict[int, _Schema] = {}
        self.current_schema_epoch = -1
        self.current_session_epoch = 0
        self.previous_sample: TelemetrySample | None = None
        self.previous_sample_epoch: int | None = None
        self.last_raw_frame: _RawFrameEvidence | None = None
        self.last_frame_epoch: int | None = None

        self.frame_count = 0
        self.event_count = 0
        self.session_info_count = 0
        self.duplicate_count = 0
        self.duplicate_conflict_count = 0
        self.dropped_tick_count = 0
        self.stale_event_count = 0
        self.session_reset_count = 0
        self.schema_change_count = 0
        self.first_buffer_tick: int | None = None
        self.last_buffer_tick: int | None = None
        self.first_capture_monotonic_us: int | None = None
        self.last_capture_monotonic_us: int | None = None
        self.capture_clock_regression_count = 0
        self.read_error_frame_count = 0
        self.read_error_field_count = 0
        self.driver_info_key_count = 0
        self.redacted_driver_info_path_count = 0
        self.session_info_scope_counts: Counter[str] = Counter()
        self.full_track_length_seen = False
        self.full_track_length_missing = False
        self.track_length_mm: int | None = None
        self.event_identity_values: dict[str, object | None] = {
            name: None for name in _EVENT_IDENTITY_KEYS
        }
        self.event_identity_statuses: dict[str, str] = {
            name: "MISSING" for name in _EVENT_IDENTITY_KEYS
        }
        self.event_identity_session_info_scope: str | None = None
        self.event_identity_session_info_update: int | None = None
        self.stable_event_identity_values: dict[str, object] = {}
        self.source_stale = False

        self.pending_schema_change: tuple[_Schema, _Schema] | None = None
        self.pending_duplicate_conflict: tuple[str, str, int] | None = None
        self.pending_drop_frame: int | None = None
        self.pending_session_info: tuple[int, int] | None = None
        self.pending_reset: tuple[tuple[str, ...], int] | None = None
        self.pending_reset_stage: Literal["session_info", "frame"] | None = None

    def _reset_event_identity_epoch(self) -> None:
        self.event_identity_values = {
            name: None for name in _EVENT_IDENTITY_KEYS
        }
        self.event_identity_statuses = {
            name: "MISSING" for name in _EVENT_IDENTITY_KEYS
        }
        self.event_identity_session_info_scope = None
        self.event_identity_session_info_update = None
        self.stable_event_identity_values = {}

    def _observe_session_event_identity(
        self,
        payload: Mapping[str, object] | None,
        *,
        scope: str,
        update: int,
    ) -> None:
        values, statuses = _session_event_identity(payload, scope=scope)
        for name in _SESSION_EVENT_IDENTITY_KEYS:
            if statuses[name] != "PRESENT":
                continue
            current = values[name]
            if name in self.stable_event_identity_values and (
                self.stable_event_identity_values[name] != current
            ):
                raise TelemetryAdapterError(
                    f"event identity {name} changed within one session epoch"
                )
            if current is None:  # pragma: no cover - PRESENT invariant
                raise AssertionError("present event identity field lost its value")
            self.stable_event_identity_values[name] = current
        for name in _SESSION_EVENT_IDENTITY_KEYS:
            self.event_identity_values[name] = values[name]
            self.event_identity_statuses[name] = statuses[name]
        self.event_identity_session_info_scope = scope
        self.event_identity_session_info_update = update

    def _observe_player_car_class(
        self,
        values: Mapping[str, object],
        read_errors: Sequence[str],
    ) -> None:
        name = "car_class_id"
        if "PlayerCarClass" in read_errors:
            status, value = "INVALID", None
        elif "PlayerCarClass" not in values:
            status, value = "MISSING", None
        else:
            status, value = _event_identity_field(name, values["PlayerCarClass"])
        if status == "PRESENT":
            if name in self.stable_event_identity_values and (
                self.stable_event_identity_values[name] != value
            ):
                raise TelemetryAdapterError(
                    "event identity car_class_id changed within one session epoch"
                )
            if value is None:  # pragma: no cover - PRESENT invariant
                raise AssertionError("present car class lost its value")
            self.stable_event_identity_values[name] = value
        self.event_identity_values[name] = value
        self.event_identity_statuses[name] = status

    def _check_pending_order(self, record_type: str, record: Mapping[str, object]) -> None:
        event_kind = record.get("event_kind") if record_type == "event" else None
        if self.pending_schema_change is not None and event_kind != "schema_changed":
            raise TelemetryAdapterError(
                "schema record must be followed by its schema_changed event"
            )
        if (
            self.pending_duplicate_conflict is not None
            and event_kind != "duplicate_tick_conflict"
        ):
            raise TelemetryAdapterError(
                "conflicting duplicate must be followed by duplicate_tick_conflict"
            )
        if self.pending_drop_frame is not None and record_type != "frame":
            raise TelemetryAdapterError("tick_drop event must be followed by its frame")
        if self.pending_session_info is not None and record_type != "session_info":
            raise TelemetryAdapterError(
                "session_info_changed_without_update must be followed by session_info"
            )
        if self.pending_reset_stage == "session_info" and record_type != "session_info":
            raise TelemetryAdapterError("session_reset must be followed by session_info")
        if self.pending_reset_stage == "frame" and record_type != "frame":
            raise TelemetryAdapterError(
                "reset session_info must be followed by its first frame"
            )

    def process(
        self, record: dict[str, object], *, line_number: int
    ) -> TelemetrySample | None:
        if self.receipt_seen:
            raise TelemetryAdapterError("collector receipt must be the terminal record")
        if record.get("collector_contract_version") != COLLECTOR_CONTRACT_VERSION:
            raise TelemetryAdapterError(
                f"collector contract version mismatch at line {line_number}"
            )
        record_type = record.get("record_type")
        if type(record_type) is not str or record_type not in _COLLECTOR_RECORD_TYPES:
            raise TelemetryAdapterError(
                f"unknown collector record type at line {line_number}"
            )
        _require_exact_keys(record, _RECORD_KEYS[record_type], f"{record_type} record")
        sequence = _plain_int(record["sequence"], "collector sequence", minimum=0)
        if sequence != self.expected_sequence:
            raise TelemetryAdapterError(
                f"collector sequence {sequence} is out of order; "
                f"expected {self.expected_sequence}"
            )
        if not self.run_seen and (record_type != "run" or sequence != 0):
            raise TelemetryAdapterError(
                "first collector record must be a run record at sequence 0"
            )
        if self.run_seen and record_type == "run":
            raise TelemetryAdapterError("collector JSONL contains more than one run record")

        self._check_pending_order(record_type, record)
        if record_type == "collector_receipt":
            self._read_receipt(record)
            self.receipt_seen = True
            return None

        encoded = _canonical_json(record)
        self.digest.update(len(encoded).to_bytes(8, "little"))
        self.digest.update(encoded)
        self.expected_sequence += 1

        if record_type == "run":
            self._read_run(record)
            return None
        if record_type == "schema":
            self._read_schema(record)
            return None
        if self.current_schema_epoch < 0:
            raise TelemetryAdapterError(
                f"{record_type} record appears before the first schema"
            )
        if record_type == "session_info":
            self._read_session_info(record)
            return None
        if record_type == "event":
            self._read_event(record)
            return None
        if record_type == "frame":
            return self._read_frame(record, line_number=line_number)
        raise AssertionError("validated collector record type was not dispatched")

    def _read_run(self, record: Mapping[str, object]) -> None:
        self.source_id = _collector_identifier(record["source_id"], "source_id")
        self.session_id = _collector_identifier(record["session_id"], "session_id")
        mode = record["sim_mode"]
        if type(mode) is not str or mode not in {"full", "replay"}:
            raise TelemetryAdapterError("collector run sim_mode must be full or replay")
        expected_kind = (
            SourceKind.SDK_LIVE if mode == "full" else SourceKind.REPLAY_SDK_PROXY
        )
        raw_kind = record["source_kind"]
        try:
            source_kind = SourceKind(raw_kind)
        except (TypeError, ValueError) as exc:
            raise TelemetryAdapterError("collector run source_kind is invalid") from exc
        if source_kind is not expected_kind:
            raise TelemetryAdapterError(
                "collector run sim_mode and source_kind do not agree"
            )
        self.sim_mode = mode
        self.source_kind = source_kind
        self.run_seen = True

    def _read_schema(self, record: Mapping[str, object]) -> None:
        expected_epoch = self.current_schema_epoch + 1
        epoch = _plain_int(record["schema_epoch"], "schema_epoch", minimum=0)
        if epoch != expected_epoch:
            raise TelemetryAdapterError(
                f"schema epoch {epoch} is out of order; expected {expected_epoch}"
            )
        tick_rate = _plain_int(record["tick_rate_hz"], "tick_rate_hz", minimum=1)
        if tick_rate > 360:
            raise TelemetryAdapterError("tick_rate_hz must not exceed 360")
        effective_tick = _plain_int(
            record["effective_buffer_tick"], "effective_buffer_tick", minimum=0
        )
        variables_raw = record["variables"]
        if type(variables_raw) is not list or not variables_raw:
            raise TelemetryAdapterError("schema variables must be a non-empty list")
        descriptors = tuple(
            _descriptor_from_record(item, index=index)
            for index, item in enumerate(variables_raw)
        )
        names = [item.name for item in descriptors]
        if names != sorted(names):
            raise TelemetryAdapterError("schema variables are not in canonical name order")
        try:
            validate_variable_descriptors(descriptors)
        except CollectorConsistencyError as exc:
            raise TelemetryAdapterError(f"invalid collector schema: {exc}") from exc
        declared_digest = _sha256_text(record["schema_sha256"], "schema_sha256")
        if declared_digest != schema_sha256(descriptors):
            raise TelemetryAdapterError("schema_sha256 does not match schema variables")
        schema = _Schema(
            names=frozenset(names),
            expected_car_count=_opponent_count_from_variables(descriptors),
            digest=declared_digest,
            tick_rate_hz=tick_rate,
            effective_buffer_tick=effective_tick,
        )
        if self.current_schema_epoch >= 0:
            self.pending_schema_change = (
                self.schemas[self.current_schema_epoch],
                schema,
            )
        self.schemas[epoch] = schema
        self.current_schema_epoch = epoch

    def _epochs(
        self, record: Mapping[str, object], *, label: str
    ) -> tuple[int, int]:
        schema_epoch = _plain_int(
            record["schema_epoch"], f"{label} schema_epoch", minimum=0
        )
        session_epoch = _plain_int(
            record["session_epoch"], f"{label} session_epoch", minimum=0
        )
        if schema_epoch != self.current_schema_epoch or schema_epoch not in self.schemas:
            raise TelemetryAdapterError(
                f"{label} references unknown schema epoch {schema_epoch}"
            )
        return schema_epoch, session_epoch

    def _observe_full_track_length(self, payload: Mapping[str, object]) -> None:
        raw_track_length = _full_session_track_length(payload)
        self.full_track_length_seen = True
        if raw_track_length is None:
            if self.track_length_mm is not None:
                raise TelemetryAdapterError(
                    "FULL session_info track length availability changed"
                )
            self.full_track_length_missing = True
            return
        parsed = _parse_track_length_mm(raw_track_length)
        if self.full_track_length_missing:
            raise TelemetryAdapterError(
                "FULL session_info track length availability changed"
            )
        if self.track_length_mm is not None and parsed != self.track_length_mm:
            raise TelemetryAdapterError(
                "FULL session_info track length changed within the run"
            )
        self.track_length_mm = parsed

    def _read_session_info(self, record: Mapping[str, object]) -> None:
        _, session_epoch = self._epochs(record, label="session_info")
        if session_epoch != self.current_session_epoch:
            raise TelemetryAdapterError("session_info session_epoch is out of order")
        buffer_tick = _plain_int(
            record["buffer_tick"], "session_info buffer_tick", minimum=0
        )
        update = _plain_int(
            record["session_info_update"], "session_info_update", minimum=0
        )
        scope = record["payload_scope"]
        status = record["payload_status"]
        payload = record["payload"]
        if type(scope) is not str or scope not in {"FULL", "PARTIAL", "UNAVAILABLE"}:
            raise TelemetryAdapterError("session_info payload_scope is invalid")
        if scope == "UNAVAILABLE":
            if status != "UNAVAILABLE" or payload is not None:
                raise TelemetryAdapterError(
                    "UNAVAILABLE session_info scope/status/payload disagree"
                )
        elif status != "PRESENT" or type(payload) is not dict:
            raise TelemetryAdapterError(
                "present session_info scope/status/payload disagree"
            )
        redacted = record["redacted_paths"]
        if (
            type(redacted) is not list
            or any(type(item) is not str or not item for item in redacted)
            or redacted != sorted(set(redacted))
        ):
            raise TelemetryAdapterError(
                "session_info redacted_paths must be sorted unique strings"
            )
        self.session_info_scope_counts[scope] += 1
        self.driver_info_key_count += _driver_info_key_count(payload)
        self.redacted_driver_info_path_count += _redacted_driver_info_path_count(
            redacted
        )
        declared_digest = _sha256_text(
            record["session_info_sha256"], "session_info_sha256"
        )
        actual_digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
        if declared_digest != actual_digest:
            raise TelemetryAdapterError(
                "session_info_sha256 does not match the session_info payload"
            )
        if scope == "FULL":
            if not isinstance(payload, Mapping):  # pragma: no cover - checked above
                raise AssertionError("FULL session_info lost its object payload")
            self._observe_full_track_length(payload)
        self._observe_session_event_identity(
            payload if isinstance(payload, Mapping) else None,
            scope=scope,
            update=update,
        )
        if self.pending_session_info is not None:
            expected_tick, expected_update = self.pending_session_info
            if (buffer_tick, update) != (expected_tick, expected_update):
                raise TelemetryAdapterError(
                    "session_info does not match its preceding change event"
                )
            self.pending_session_info = None
        if self.pending_reset_stage == "session_info":
            if self.pending_reset is None or buffer_tick != self.pending_reset[1]:
                raise TelemetryAdapterError(
                    "reset session_info does not match session_reset buffer_tick"
                )
            self.pending_reset_stage = "frame"
        self.session_info_count += 1

    def _read_event(self, record: Mapping[str, object]) -> None:
        _, session_epoch = self._epochs(record, label="event")
        kind = record["event_kind"]
        if type(kind) is not str or kind not in _EVENT_DETAIL_KEYS:
            raise TelemetryAdapterError("collector event_kind is unknown")
        details = record["details"]
        if type(details) is not dict:
            raise TelemetryAdapterError("collector event details must be an object")
        _require_exact_keys(details, _EVENT_DETAIL_KEYS[kind], f"{kind} event details")
        buffer_tick = _plain_int(record["buffer_tick"], "event buffer_tick", minimum=0)
        _capture_monotonic_us(
            record["capture_monotonic_us"], "event capture_monotonic_us"
        )

        if kind == "session_reset":
            if session_epoch != self.current_session_epoch + 1:
                raise TelemetryAdapterError("session_reset epoch is out of order")
            if self.last_raw_frame is None:
                raise TelemetryAdapterError("session_reset appears before the first frame")
            reasons = details["reasons"]
            if (
                type(reasons) is not list
                or not reasons
                or any(type(item) is not str or item not in _RESET_REASONS for item in reasons)
                or len(reasons) != len(set(reasons))
            ):
                raise TelemetryAdapterError("session_reset reasons are invalid")
            self.current_session_epoch = session_epoch
            self.session_reset_count += 1
            self.source_stale = False
            self._reset_event_identity_epoch()
            self.pending_reset = (tuple(reasons), buffer_tick)
            self.pending_reset_stage = "session_info"
        elif session_epoch != self.current_session_epoch:
            raise TelemetryAdapterError(f"{kind} event session_epoch is out of order")
        elif kind == "schema_changed":
            if self.pending_schema_change is None:
                raise TelemetryAdapterError("schema_changed has no preceding schema record")
            previous, current = self.pending_schema_change
            expected = {
                "previous_schema_sha256": previous.digest,
                "previous_tick_rate_hz": previous.tick_rate_hz,
                "schema_sha256": current.digest,
                "tick_rate_hz": current.tick_rate_hz,
            }
            if details != expected or buffer_tick != current.effective_buffer_tick:
                raise TelemetryAdapterError(
                    "schema_changed details do not match adjacent schema records"
                )
            self.pending_schema_change = None
            self.schema_change_count += 1
        elif kind == "capture_clock_regression":
            _plain_int(
                details["previous_capture_monotonic_us"],
                "previous_capture_monotonic_us",
                minimum=0,
            )
            self.capture_clock_regression_count += 1
        elif kind == "source_stale":
            stale_after = _plain_int(details["stale_after_us"], "stale_after_us", minimum=0)
            stale_for = _plain_int(details["stale_for_us"], "stale_for_us", minimum=0)
            if stale_for <= stale_after or self.source_stale:
                raise TelemetryAdapterError("source_stale event is inconsistent")
            self.source_stale = True
            self.stale_event_count += 1
        elif kind == "source_resumed":
            _plain_int(details["stale_for_us"], "stale_for_us", minimum=0)
            if not self.source_stale:
                raise TelemetryAdapterError("source_resumed appears without source_stale")
            self.source_stale = False
        elif kind == "session_info_changed_without_update":
            update = _plain_int(
                details["session_info_update"], "session_info_update", minimum=0
            )
            self.pending_session_info = (buffer_tick, update)
        elif kind == "duplicate_sample":
            if self.last_raw_frame is None or self.last_frame_epoch != session_epoch:
                raise TelemetryAdapterError("duplicate_sample has no frame in its epoch")
            conflict = details["conflict"]
            if type(conflict) is not bool:
                raise TelemetryAdapterError("duplicate_sample conflict must be boolean")
            current_digest = _sha256_text(
                details["current_payload_sha256"], "current_payload_sha256"
            )
            previous_digest = _sha256_text(
                details["previous_payload_sha256"], "previous_payload_sha256"
            )
            if (
                buffer_tick != self.last_raw_frame.buffer_tick
                or previous_digest != self.last_raw_frame.payload_sha256
                or conflict != (current_digest != previous_digest)
            ):
                raise TelemetryAdapterError("duplicate_sample evidence is inconsistent")
            self.duplicate_count += 1
            if conflict:
                self.pending_duplicate_conflict = (
                    previous_digest,
                    current_digest,
                    buffer_tick,
                )
        elif kind == "duplicate_tick_conflict":
            if self.pending_duplicate_conflict is None:
                raise TelemetryAdapterError(
                    "duplicate_tick_conflict has no conflicting duplicate_sample"
                )
            previous_digest, current_digest, expected_tick = (
                self.pending_duplicate_conflict
            )
            expected = {
                "buffer_tick": expected_tick,
                "current_payload_sha256": current_digest,
                "previous_payload_sha256": previous_digest,
            }
            if details != expected or buffer_tick != expected_tick:
                raise TelemetryAdapterError(
                    "duplicate_tick_conflict evidence is inconsistent"
                )
            self.pending_duplicate_conflict = None
            self.duplicate_conflict_count += 1
        elif kind == "tick_drop":
            if self.last_raw_frame is None or self.last_frame_epoch != session_epoch:
                raise TelemetryAdapterError("tick_drop has no previous frame in its epoch")
            previous_tick = _plain_int(
                details["previous_buffer_tick"], "previous_buffer_tick", minimum=0
            )
            current_tick = _plain_int(
                details["current_buffer_tick"], "current_buffer_tick", minimum=0
            )
            missing = _plain_int(
                details["missing_tick_count"], "missing_tick_count", minimum=1
            )
            if (
                previous_tick != self.last_raw_frame.buffer_tick
                or current_tick != buffer_tick
                or current_tick - previous_tick - 1 != missing
            ):
                raise TelemetryAdapterError("tick_drop evidence is inconsistent")
            self.pending_drop_frame = current_tick
            self.dropped_tick_count += missing
        self.event_count += 1

    def _read_frame(
        self, record: Mapping[str, object], *, line_number: int
    ) -> TelemetrySample:
        schema_epoch, session_epoch = self._epochs(record, label="frame")
        if session_epoch != self.current_session_epoch:
            raise TelemetryAdapterError("frame session_epoch is out of order")
        buffer_tick = _plain_int(record["buffer_tick"], "frame buffer_tick", minimum=0)
        capture_us = _capture_monotonic_us(
            record["capture_monotonic_us"], "frame capture_monotonic_us"
        )
        update = _plain_int(
            record["session_info_update"], "frame session_info_update", minimum=0
        )
        if record["sim_mode_raw"] != self.sim_mode:
            raise TelemetryAdapterError("frame sim_mode_raw does not match the run record")
        read_errors = record["read_errors"]
        if (
            type(read_errors) is not list
            or any(type(item) is not str for item in read_errors)
            or len(read_errors) != len(set(read_errors))
        ):
            raise TelemetryAdapterError(
                "frame read_errors must be a unique list of strings"
            )
        if read_errors:
            self.read_error_frame_count += 1
            self.read_error_field_count += len(read_errors)
        values = record["values"]
        if type(values) is not dict:
            raise TelemetryAdapterError("frame values must be an object")
        self._observe_player_car_class(values, read_errors)
        schema = self.schemas[schema_epoch]
        unknown = sorted(set(values) - schema.names)
        if unknown:
            raise TelemetryAdapterError(
                f"frame fields absent from schema: {', '.join(unknown)}"
            )
        unknown_errors = sorted(set(read_errors) - schema.names)
        if unknown_errors:
            raise TelemetryAdapterError(
                f"frame read_errors absent from schema: {', '.join(unknown_errors)}"
            )
        if (
            self.last_frame_epoch == session_epoch
            and self.last_raw_frame is not None
            and buffer_tick <= self.last_raw_frame.buffer_tick
        ):
            raise TelemetryAdapterError(
                "frame buffer_tick is not increasing within session epoch"
            )
        if self.pending_drop_frame is not None:
            if buffer_tick != self.pending_drop_frame:
                raise TelemetryAdapterError("frame does not match preceding tick_drop")
            self.pending_drop_frame = None
        continuity_boundary_reasons: tuple[str, ...] = ()
        if self.pending_reset_stage == "frame":
            if self.pending_reset is None or buffer_tick != self.pending_reset[1]:
                raise TelemetryAdapterError("frame does not match preceding session_reset")
            if self.last_raw_frame is None:
                raise AssertionError("pending reset lost its previous frame")
            actual_reasons = _reset_reasons(
                self.last_raw_frame,
                buffer_tick=buffer_tick,
                session_info_update=update,
                values=values,
            )
            if actual_reasons != self.pending_reset[0]:
                raise TelemetryAdapterError(
                    "session_reset reasons do not match adjacent frames"
                )
            continuity_boundary_reasons = actual_reasons
            self.pending_reset = None
            self.pending_reset_stage = None

        payload_digest = hashlib.sha256(
            _canonical_json(
                {
                    "buffer_tick": buffer_tick,
                    "read_errors": read_errors,
                    "values": values,
                }
            )
        ).hexdigest()
        # A transport read error means the persisted value, if any, is not
        # admissible evidence.  Excluding it makes required-field failures
        # visible to normalization instead of allowing a stale value to feed a
        # model; the run-level evidence still records the explicit SDK error.
        filtered = {
            name: value
            for name, value in values.items()
            if name in _NORMALIZED_FIELD_SET and name not in read_errors
        }
        if self.source_id is None or self.session_id is None or self.source_kind is None:
            raise AssertionError("collector run identity was not bound before a frame")
        previous = (
            self.previous_sample
            if self.previous_sample_epoch == session_epoch
            else None
        )
        try:
            sample = normalize_sdk_frame(
                filtered,
                source_id=self.source_id,
                session_id=self.session_id,
                source_kind=self.source_kind,
                buffer_tick=buffer_tick,
                captured_monotonic_s=(
                    None if capture_us is None else capture_us / 1_000_000
                ),
                previous=previous,
                stale_after_s=self.stale_after_s,
                expected_car_count=schema.expected_car_count,
                opponent_error_policy=self.opponent_error_policy,
                continuity_boundary_reasons=continuity_boundary_reasons,
            )
        except (
            OverflowError,
            RecursionError,
            TelemetryNormalizationError,
            TypeError,
            ValueError,
        ) as exc:
            raise TelemetryAdapterError(
                f"collector frame normalization failed at line {line_number}: {exc}"
            ) from exc

        self.previous_sample = sample
        self.previous_sample_epoch = session_epoch
        self.last_raw_frame = _RawFrameEvidence(
            buffer_tick=buffer_tick,
            session_info_update=update,
            values=dict(values),
            payload_sha256=payload_digest,
        )
        self.last_frame_epoch = session_epoch
        self.frame_count += 1
        if self.first_buffer_tick is None:
            self.first_buffer_tick = buffer_tick
        self.last_buffer_tick = buffer_tick
        if capture_us is not None:
            if self.first_capture_monotonic_us is None:
                self.first_capture_monotonic_us = capture_us
            self.last_capture_monotonic_us = capture_us
        return sample

    def _read_receipt(self, record: Mapping[str, object]) -> None:
        if self.pending_schema_change is not None:
            raise TelemetryAdapterError("receipt follows an incomplete schema change")
        if self.pending_duplicate_conflict is not None:
            raise TelemetryAdapterError("receipt follows an incomplete duplicate conflict")
        if self.pending_drop_frame is not None:
            raise TelemetryAdapterError("receipt follows an incomplete tick_drop")
        if self.pending_session_info is not None:
            raise TelemetryAdapterError("receipt follows an incomplete session_info change")
        if self.pending_reset_stage is not None:
            raise TelemetryAdapterError("receipt follows an incomplete session_reset")
        if self.frame_count == 0 or not self.schemas:
            raise TelemetryAdapterError("COMPLETE collector receipt requires a frame and schema")
        receipt = record["receipt"]
        if type(receipt) is not dict:
            raise TelemetryAdapterError("collector_receipt payload must be an object")
        _require_exact_keys(receipt, _RECEIPT_KEYS, "collector receipt")
        if receipt["collector_contract_version"] != COLLECTOR_CONTRACT_VERSION:
            raise TelemetryAdapterError("collector receipt contract version mismatch")
        if receipt["completion_status"] != "COMPLETE":
            raise TelemetryAdapterError("collector receipt is not COMPLETE")
        _sha256_text(receipt["records_sha256"], "receipt records_sha256")
        integer_fields = (
            "semantic_record_count",
            "run_record_count",
            "frame_record_count",
            "event_record_count",
            "schema_record_count",
            "session_info_record_count",
            "samples_seen",
            "duplicate_sample_count",
            "duplicate_conflict_count",
            "dropped_tick_count",
            "stale_event_count",
            "session_reset_count",
            "schema_change_count",
            "schema_epoch_count",
            "session_epoch_count",
        )
        for name in integer_fields:
            _plain_int(receipt[name], f"receipt {name}", minimum=0)
        first_tick = _optional_plain_int(
            receipt["first_buffer_tick"], "receipt first_buffer_tick"
        )
        last_tick = _optional_plain_int(
            receipt["last_buffer_tick"], "receipt last_buffer_tick"
        )
        expected: dict[str, object] = {
            "records_sha256": self.digest.hexdigest(),
            "semantic_record_count": self.expected_sequence,
            "run_record_count": 1,
            "frame_record_count": self.frame_count,
            "event_record_count": self.event_count,
            "schema_record_count": len(self.schemas),
            "session_info_record_count": self.session_info_count,
            "samples_seen": self.frame_count + self.duplicate_count,
            "duplicate_sample_count": self.duplicate_count,
            "duplicate_conflict_count": self.duplicate_conflict_count,
            "dropped_tick_count": self.dropped_tick_count,
            "stale_event_count": self.stale_event_count,
            "session_reset_count": self.session_reset_count,
            "schema_change_count": self.schema_change_count,
            "schema_epoch_count": len(self.schemas),
            "session_epoch_count": self.current_session_epoch + 1,
            "first_buffer_tick": self.first_buffer_tick,
            "last_buffer_tick": self.last_buffer_tick,
        }
        if first_tick != self.first_buffer_tick or last_tick != self.last_buffer_tick:
            raise TelemetryAdapterError("collector receipt buffer tick bounds mismatch")
        for name, expected_value in expected.items():
            if receipt[name] != expected_value:
                raise TelemetryAdapterError(f"collector receipt {name} mismatch")
        if self.schema_change_count != len(self.schemas) - 1:
            raise TelemetryAdapterError("collector schema change accounting mismatch")
        if self.session_reset_count != self.current_session_epoch:
            raise TelemetryAdapterError("collector session reset accounting mismatch")

    def track_context_evidence(
        self,
        input_evidence: CollectorInputEvidence,
    ) -> TrackContextEvidence:
        if self.full_track_length_seen:
            status = TrackContextStatus.TRACK_LENGTH_MISSING
        elif self.session_info_scope_counts["PARTIAL"]:
            status = TrackContextStatus.SESSION_INFO_PARTIAL
        elif self.session_info_scope_counts["UNAVAILABLE"]:
            status = TrackContextStatus.SESSION_INFO_UNAVAILABLE
        else:
            status = TrackContextStatus.SESSION_INFO_MISSING
        if self.track_length_mm is not None:
            availability = TrackContextAvailability.AVAILABLE
            status = TrackContextStatus.VERIFIED
        else:
            availability = TrackContextAvailability.UNAVAILABLE
        return TrackContextEvidence(
            track_length_mm=self.track_length_mm,
            source_field=_TRACK_LENGTH_SOURCE_FIELD,
            availability=availability,
            status=status,
            provenance=TrackContextProvenance.COLLECTOR_VALIDATED_SNAPSHOT,
            source_binding_sha256=_evidence_binding_sha256(input_evidence),
        )

    def event_identity_context_evidence(
        self,
        input_evidence: CollectorInputEvidence,
    ) -> EventIdentityContextEvidence:
        statuses = self.event_identity_statuses
        present_count = sum(value == "PRESENT" for value in statuses.values())
        invalid_count = sum(value == "INVALID" for value in statuses.values())
        if present_count == len(_EVENT_IDENTITY_KEYS):
            availability = EventIdentityAvailability.AVAILABLE
            status = EventIdentityStatus.VERIFIED
        elif present_count:
            availability = EventIdentityAvailability.PARTIAL
            status = (
                EventIdentityStatus.FIELDS_INVALID
                if invalid_count
                else EventIdentityStatus.FIELDS_MISSING
            )
        else:
            availability = EventIdentityAvailability.UNAVAILABLE
            if invalid_count:
                status = EventIdentityStatus.FIELDS_INVALID
            elif self.event_identity_session_info_scope == "PARTIAL":
                status = EventIdentityStatus.SESSION_INFO_PARTIAL
            elif self.event_identity_session_info_scope == "UNAVAILABLE":
                status = EventIdentityStatus.SESSION_INFO_UNAVAILABLE
            elif self.event_identity_session_info_scope is None:
                status = EventIdentityStatus.SESSION_INFO_MISSING
            else:
                status = EventIdentityStatus.FIELDS_MISSING
        return EventIdentityContextEvidence(
            identity=tuple(
                (name, self.event_identity_values[name])
                for name in _EVENT_IDENTITY_KEYS
            ),
            field_statuses=tuple(
                (name, self.event_identity_statuses[name])
                for name in _EVENT_IDENTITY_KEYS
            ),
            session_info_scope=self.event_identity_session_info_scope,
            session_info_update=self.event_identity_session_info_update,
            availability=availability,
            status=status,
            provenance=EventIdentityProvenance.COLLECTOR_VALIDATED_SNAPSHOT,
            source_binding_sha256=_evidence_binding_sha256(input_evidence),
        )

    def traffic_observation_context_evidence(
        self,
        input_evidence: CollectorInputEvidence,
        track_context: TrackContextEvidence,
    ) -> TrafficObservationContextEvidence:
        if self.previous_sample is None:
            raise TelemetryAdapterError(
                "traffic observation requires a normalized collector frame"
            )
        return _traffic_observation_context_evidence(
            self.previous_sample,
            track_context=track_context,
            input_evidence=input_evidence,
        )

    def finish(self) -> CollectorInputEvidence:
        if not self.run_seen:
            raise TelemetryAdapterError("collector JSONL has no run record")
        if self.pending_schema_change is not None:
            raise TelemetryAdapterError("collector JSONL ends during a schema change")
        if self.pending_duplicate_conflict is not None:
            raise TelemetryAdapterError("collector JSONL ends during a duplicate conflict")
        if self.pending_drop_frame is not None:
            raise TelemetryAdapterError("collector JSONL ends during a tick_drop transaction")
        if self.pending_session_info is not None:
            raise TelemetryAdapterError("collector JSONL ends during a session_info change")
        if self.pending_reset_stage is not None:
            raise TelemetryAdapterError("collector JSONL ends during a session reset")
        if self.require_receipt and not self.receipt_seen:
            raise TelemetryAdapterError("collector JSONL has no terminal receipt")
        if self.source_id is None or self.session_id is None or self.source_kind is None:
            raise AssertionError("validated run identity is incomplete")
        if self.sim_mode is None:
            raise AssertionError("validated run sim_mode is incomplete")
        capture_span_us = None
        if (
            self.capture_clock_regression_count == 0
            and self.first_capture_monotonic_us is not None
            and self.last_capture_monotonic_us is not None
            and self.last_capture_monotonic_us >= self.first_capture_monotonic_us
        ):
            capture_span_us = (
                self.last_capture_monotonic_us
                - self.first_capture_monotonic_us
            )
        return CollectorInputEvidence(
            source_id=self.source_id,
            session_id=self.session_id,
            source_kind=self.source_kind,
            sim_mode=self.sim_mode,
            completion_status=(
                "COMPLETE" if self.receipt_seen else "INCOMPLETE_RECOVERY"
            ),
            semantic_record_count=self.expected_sequence,
            records_sha256=self.digest.hexdigest(),
            frame_record_count=self.frame_count,
            event_record_count=self.event_count,
            schema_record_count=len(self.schemas),
            session_info_record_count=self.session_info_count,
            samples_seen=self.frame_count + self.duplicate_count,
            duplicate_sample_count=self.duplicate_count,
            duplicate_conflict_count=self.duplicate_conflict_count,
            dropped_tick_count=self.dropped_tick_count,
            stale_event_count=self.stale_event_count,
            session_reset_count=self.session_reset_count,
            schema_change_count=self.schema_change_count,
            schema_epoch_count=len(self.schemas),
            session_epoch_count=self.current_session_epoch + 1,
            first_buffer_tick=self.first_buffer_tick,
            last_buffer_tick=self.last_buffer_tick,
            tick_rate_hz_values=tuple(
                self.schemas[index].tick_rate_hz for index in sorted(self.schemas)
            ),
            first_capture_monotonic_us=self.first_capture_monotonic_us,
            last_capture_monotonic_us=self.last_capture_monotonic_us,
            capture_span_us=capture_span_us,
            capture_clock_regression_count=self.capture_clock_regression_count,
            read_error_frame_count=self.read_error_frame_count,
            read_error_field_count=self.read_error_field_count,
            driver_info_key_count=self.driver_info_key_count,
            redacted_driver_info_path_count=self.redacted_driver_info_path_count,
            session_info_scope_counts=tuple(
                sorted(self.session_info_scope_counts.items())
            ),
        )


def _new_collector_validator(
    *,
    stale_after_s: float,
    opponent_error_policy: Literal["degrade", "reject"],
    require_receipt: bool,
) -> _CollectorValidator:
    if not isinstance(stale_after_s, (int, float)) or isinstance(stale_after_s, bool):
        raise ValueError("stale_after_s must be numeric")
    if not 0 < stale_after_s < float("inf"):
        raise ValueError("stale_after_s must be finite and positive")
    if opponent_error_policy not in {"degrade", "reject"}:
        raise ValueError("opponent_error_policy must be degrade or reject")
    if type(require_receipt) is not bool:
        raise ValueError("require_receipt must be a plain boolean")
    return _CollectorValidator(
        stale_after_s=float(stale_after_s),
        opponent_error_policy=opponent_error_policy,
        require_receipt=require_receipt,
    )


def _validate_collector_handle(
    handle: TextIO,
    *,
    stale_after_s: float,
    opponent_error_policy: Literal["degrade", "reject"],
    require_receipt: bool,
) -> tuple[
    CollectorInputEvidence,
    TrackContextEvidence,
    EventIdentityContextEvidence,
    TrafficObservationContextEvidence,
]:
    handle.seek(0)
    validator = _new_collector_validator(
        stale_after_s=stale_after_s,
        opponent_error_policy=opponent_error_policy,
        require_receipt=require_receipt,
    )
    for line_number, raw_line in enumerate(handle, start=1):
        if not raw_line.strip():
            raise TelemetryAdapterError(
                f"blank collector record at line {line_number}"
            )
        record = _load_record(raw_line, line_number)
        # Normalization happens here too.  This first pass intentionally
        # discards samples so no caller can consume a prefix before the
        # terminal receipt and every later record have been verified.
        validator.process(record, line_number=line_number)
    evidence = validator.finish()
    track_context = validator.track_context_evidence(evidence)
    return (
        evidence,
        track_context,
        validator.event_identity_context_evidence(evidence),
        validator.traffic_observation_context_evidence(evidence, track_context),
    )


@contextmanager
def _collector_snapshot(path: str | Path) -> Iterator[TextIO]:
    """Copy one path-open into a process-controlled snapshot before parsing.

    A second open of a caller-controlled path would permit a FIFO, symlink, or
    rename race to substitute different bytes after validation.  The spool is
    immutable by convention after this copy and rolls to a private temporary
    file for larger captures.
    """

    try:
        with _collector_handle(path) as source, tempfile.SpooledTemporaryFile(
            mode="w+t",
            max_size=_COLLECTOR_SNAPSHOT_MEMORY_LIMIT,
            encoding="utf-8",
            newline="",
        ) as snapshot:
            while chunk := source.read(1024 * 1024):
                snapshot.write(chunk)
            snapshot.flush()
            snapshot.seek(0)
            yield snapshot
    except TelemetryAdapterError:
        raise
    except (OSError, OverflowError, RecursionError, UnicodeError) as exc:
        raise TelemetryAdapterError(
            f"cannot snapshot collector JSONL: {path}"
        ) from exc


def _iter_collector_snapshot_samples(
    handle: TextIO,
    *,
    baseline_evidence: CollectorInputEvidence,
    baseline_track_context: TrackContextEvidence,
    baseline_event_identity_context: EventIdentityContextEvidence,
    baseline_traffic_observation_context: TrafficObservationContextEvidence,
    stale_after_s: float,
    opponent_error_policy: Literal["degrade", "reject"],
    require_receipt: bool,
) -> Iterator[TelemetrySample]:
    validator = _new_collector_validator(
        stale_after_s=stale_after_s,
        opponent_error_policy=opponent_error_policy,
        require_receipt=require_receipt,
    )
    handle.seek(0)
    for line_number, raw_line in enumerate(handle, start=1):
        if not raw_line.strip():
            raise TelemetryAdapterError(
                f"blank collector record at line {line_number}"
            )
        record = _load_record(raw_line, line_number)
        sample = validator.process(record, line_number=line_number)
        if sample is not None:
            yield sample
    repeated_evidence = validator.finish()
    repeated_track_context = validator.track_context_evidence(repeated_evidence)
    repeated_event_identity_context = validator.event_identity_context_evidence(
        repeated_evidence
    )
    repeated_traffic_observation_context = (
        validator.traffic_observation_context_evidence(
            repeated_evidence,
            repeated_track_context,
        )
    )
    if (
        repeated_evidence != baseline_evidence
        or repeated_track_context != baseline_track_context
        or repeated_event_identity_context != baseline_event_identity_context
        or repeated_traffic_observation_context
        != baseline_traffic_observation_context
    ):  # pragma: no cover - deterministic same-snapshot guard
        raise TelemetryAdapterError(
            "collector snapshot changed between validation passes"
        )


@contextmanager
def open_collector_jsonl_snapshot(
    handle: TextIO,
    *,
    stale_after_s: float = 0.5,
    opponent_error_policy: Literal["degrade", "reject"] = "degrade",
    require_receipt: bool = True,
) -> Iterator[ValidatedCollectorRun]:
    """Validate one caller-owned, seekable collector snapshot handle.

    The handle is never closed.  Callers that need path-race resistance must
    first copy one ``O_NOFOLLOW`` source descriptor into a private snapshot and
    keep that snapshot open for every validation/analysis pass.
    """

    if not hasattr(handle, "read") or not hasattr(handle, "seek"):
        raise TypeError("collector snapshot handle must be readable and seekable")
    if getattr(handle, "closed", True):
        raise TelemetryAdapterError("collector snapshot handle is closed")
    (
        baseline,
        track_context,
        event_identity_context,
        traffic_observation_context,
    ) = _validate_collector_handle(
        handle,
        stale_after_s=stale_after_s,
        opponent_error_policy=opponent_error_policy,
        require_receipt=require_receipt,
    )
    run = ValidatedCollectorRun(
        evidence=baseline,
        samples=_iter_collector_snapshot_samples(
            handle,
            baseline_evidence=baseline,
            baseline_track_context=track_context,
            baseline_event_identity_context=event_identity_context,
            baseline_traffic_observation_context=traffic_observation_context,
            stale_after_s=stale_after_s,
            opponent_error_policy=opponent_error_policy,
            require_receipt=require_receipt,
        ),
        stale_after_s=stale_after_s,
        opponent_error_policy=opponent_error_policy,
        _token=_VALIDATED_RUN_TOKEN,
    )
    _register_validated_run(
        run,
        _ValidatedRunState(
            evidence=baseline,
            samples=run.samples,
            track_context=track_context,
            event_identity_context=event_identity_context,
            traffic_observation_context=traffic_observation_context,
            stale_after_s=stale_after_s,
            opponent_error_policy=opponent_error_policy,
        ),
    )
    try:
        yield run
    finally:
        _unregister_validated_run(run)


@contextmanager
def open_collector_jsonl(
    path: str | Path,
    *,
    stale_after_s: float = 0.5,
    opponent_error_policy: Literal["degrade", "reject"] = "degrade",
    require_receipt: bool = True,
) -> Iterator[ValidatedCollectorRun]:
    """Open one immutable validated snapshot with evidence and normalized frames.

    Source and session identities are bound by the sequence-zero ``run``
    record; callers cannot relabel a collected run.  Completed receipts are
    required by default.  ``require_receipt=False`` is an explicit recovery
    mode for a newline-complete crash prefix, which is still fully parsed and
    normalized before any sample is yielded.
    """

    with _collector_snapshot(path) as handle, open_collector_jsonl_snapshot(
        handle,
        stale_after_s=stale_after_s,
        opponent_error_policy=opponent_error_policy,
        require_receipt=require_receipt,
    ) as run:
        yield run


def iter_collector_jsonl_samples(
    path: str | Path,
    *,
    stale_after_s: float = 0.5,
    opponent_error_policy: Literal["degrade", "reject"] = "degrade",
    require_receipt: bool = True,
) -> Iterator[TelemetrySample]:
    """Yield frames from one fully validated process-controlled snapshot."""

    with open_collector_jsonl(
        path,
        stale_after_s=stale_after_s,
        opponent_error_policy=opponent_error_policy,
        require_receipt=require_receipt,
    ) as run:
        yield from run.samples
