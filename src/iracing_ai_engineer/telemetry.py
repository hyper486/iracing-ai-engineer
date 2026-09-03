"""Immutable normalized telemetry contract for live SDK and offline samples.

The contract deliberately separates a value from its presence and provenance.
Normalizers never substitute zero, ``False``, or another sentinel for an absent
or invalid SDK channel.  Downstream strategy and coaching code can therefore
gate on evidence instead of accidentally treating schema drift as telemetry.
"""

from __future__ import annotations

import json
import math
import operator
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum
from numbers import Real
from typing import Any, Literal

TELEMETRY_CONTRACT_VERSION = "normalized-telemetry-v3"
TELEMETRY_PARQUET_SCHEMA_VERSION = "normalized-telemetry-parquet-v3"


class Provenance(StrEnum):
    """How a normalized value became available."""

    SDK_DIRECT = "SDK_DIRECT"
    DERIVED = "DERIVED"
    PIT_SNAPSHOT = "PIT_SNAPSHOT"
    INFERRED = "INFERRED"
    USER_RULE = "USER_RULE"
    UNKNOWN = "UNKNOWN"


class Presence(StrEnum):
    """Whether a field may be used as evidence."""

    PRESENT = "PRESENT"
    MISSING = "MISSING"
    INVALID = "INVALID"


class QualityStatus(StrEnum):
    """Fail-closed disposition of one normalized sample."""

    READY = "READY"
    DEGRADED = "DEGRADED"
    REJECTED = "REJECTED"


class SourceKind(StrEnum):
    SDK_LIVE = "SDK_LIVE"
    IBT_OFFLINE = "IBT_OFFLINE"
    REPLAY_SDK_PROXY = "REPLAY_SDK_PROXY"


class TelemetryNormalizationError(ValueError):
    """Raised when strict normalization cannot preserve field alignment."""


def _contains_non_finite(value: object) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, (tuple, list)):
        return any(_contains_non_finite(item) for item in value)
    if isinstance(value, Mapping):
        return any(_contains_non_finite(item) for item in value.values())
    return False


@dataclass(frozen=True, slots=True)
class TelemetryField[T]:
    """A typed value plus explicit availability and lineage."""

    value: T | None
    presence: Presence
    provenance: Provenance
    source_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.presence, Presence):
            raise TypeError("presence must be a Presence enum")
        if not isinstance(self.provenance, Provenance):
            raise TypeError("provenance must be a Provenance enum")
        if not isinstance(self.source_fields, tuple) or any(
            not isinstance(name, str) or not name for name in self.source_fields
        ):
            raise TypeError("source_fields must be a tuple of non-empty strings")
        if len(self.source_fields) != len(set(self.source_fields)):
            raise ValueError("source_fields must not contain duplicates")
        if self.presence is Presence.PRESENT:
            if self.value is None:
                raise ValueError("a PRESENT field must carry a value")
            if _contains_non_finite(self.value):
                raise ValueError("a PRESENT field cannot contain NaN or infinity")
        elif self.value is not None:
            raise ValueError("a MISSING or INVALID field must not carry a value")
        if self.presence is Presence.MISSING and self.provenance is not Provenance.UNKNOWN:
            raise ValueError("a MISSING field must use UNKNOWN provenance")

    @classmethod
    def present(
        cls,
        value: T,
        provenance: Provenance,
        *source_fields: str,
    ) -> TelemetryField[T]:
        return cls(value, Presence.PRESENT, provenance, tuple(source_fields))

    @classmethod
    def missing(cls) -> TelemetryField[T]:
        return cls(None, Presence.MISSING, Provenance.UNKNOWN)

    @classmethod
    def invalid(
        cls,
        provenance: Provenance,
        *source_fields: str,
    ) -> TelemetryField[T]:
        return cls(None, Presence.INVALID, provenance, tuple(source_fields))


@dataclass(frozen=True, slots=True)
class SourceTelemetry:
    source_id: TelemetryField[str]
    source_kind: TelemetryField[SourceKind]


@dataclass(frozen=True, slots=True)
class SessionTelemetry:
    session_id: TelemetryField[str]
    session_num: TelemetryField[int]
    session_tick: TelemetryField[int]
    sdk_buffer_tick: TelemetryField[int]
    session_time_s: TelemetryField[float]
    captured_monotonic_s: TelemetryField[float]
    session_time_remaining_s: TelemetryField[float]
    session_laps_remaining: TelemetryField[int]


@dataclass(frozen=True, slots=True)
class LapTelemetry:
    lap_number: TelemetryField[int]
    laps_completed: TelemetryField[int]
    lap_distance_pct: TelemetryField[float]
    speed_mps: TelemetryField[float]


@dataclass(frozen=True, slots=True)
class ControlTelemetry:
    throttle: TelemetryField[float]
    brake: TelemetryField[float]
    clutch: TelemetryField[float]
    steering_angle_rad: TelemetryField[float]
    gear: TelemetryField[int]
    rpm: TelemetryField[float]


@dataclass(frozen=True, slots=True)
class FuelTelemetry:
    level_l: TelemetryField[float]
    level_pct: TelemetryField[float]
    use_per_hour_l: TelemetryField[float]


@dataclass(frozen=True, slots=True)
class EnvironmentTelemetry:
    """Direct environmental channels used to bind comparable conditions."""

    session_time_of_day_s: TelemetryField[float]
    track_temp_c: TelemetryField[float]
    track_temp_crew_c: TelemetryField[float]
    air_temp_c: TelemetryField[float]
    weather_type: TelemetryField[int]
    weather_version: TelemetryField[int]
    skies: TelemetryField[int]
    wind_velocity_mps: TelemetryField[float]
    wind_direction_rad: TelemetryField[float]
    relative_humidity_fraction: TelemetryField[float]
    precipitation_fraction: TelemetryField[float]


@dataclass(frozen=True, slots=True)
class TireTelemetry:
    """Direct player tire-selection and set-use channels."""

    player_tire_compound: TelemetryField[int]
    tire_sets_used: TelemetryField[int]


@dataclass(frozen=True, slots=True)
class PitTelemetry:
    on_pit_road: TelemetryField[bool]
    in_pit_stall: TelemetryField[bool]
    pitstop_active: TelemetryField[bool]
    pits_open: TelemetryField[bool]


@dataclass(frozen=True, slots=True)
class FlagTelemetry:
    session_flags: TelemetryField[int]
    player_track_surface: TelemetryField[int]
    is_on_track: TelemetryField[bool]
    is_on_track_car: TelemetryField[bool]


@dataclass(frozen=True, slots=True)
class IncidentTelemetry:
    """Raw player incident counters without cross-field substitution."""

    player_car_my_incident_count: TelemetryField[int]
    player_car_driver_incident_count: TelemetryField[int]
    player_car_team_incident_count: TelemetryField[int]


@dataclass(frozen=True, slots=True)
class OpponentTelemetry:
    car_idx: TelemetryField[int]
    lap_number: TelemetryField[int]
    laps_completed: TelemetryField[int]
    lap_distance_pct: TelemetryField[float]
    on_pit_road: TelemetryField[bool]
    track_surface: TelemetryField[int]


@dataclass(frozen=True, slots=True)
class OpponentSet:
    presence: Presence
    provenance: Provenance
    player_car_idx: TelemetryField[int]
    entries: tuple[OpponentTelemetry, ...] = ()
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.presence, Presence):
            raise TypeError("opponent presence must be a Presence enum")
        if not isinstance(self.provenance, Provenance):
            raise TypeError("opponent provenance must be a Provenance enum")
        if not isinstance(self.entries, tuple) or not isinstance(self.issues, tuple):
            raise TypeError("opponent entries and issues must be tuples")
        if self.presence is not Presence.PRESENT and self.entries:
            raise ValueError("invalid or missing opponents cannot carry aligned entries")
        if self.presence is Presence.MISSING and self.provenance is not Provenance.UNKNOWN:
            raise ValueError("missing opponents must use UNKNOWN provenance")
        if len(self.issues) != len(set(self.issues)):
            raise ValueError("opponent issues must not contain duplicates")


@dataclass(frozen=True, slots=True)
class QualityTelemetry:
    stale: TelemetryField[bool]
    dropped_ticks: TelemetryField[int]
    status: TelemetryField[QualityStatus]
    issues: TelemetryField[tuple[str, ...]]


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical telemetry cannot contain NaN or infinity")
        return 0.0 if value == 0.0 else value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported telemetry serialization type: {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    """One immutable sample at the SDK/IBT record grain."""

    source: SourceTelemetry
    session: SessionTelemetry
    lap: LapTelemetry
    controls: ControlTelemetry
    fuel: FuelTelemetry
    environment: EnvironmentTelemetry
    tires: TireTelemetry
    pit: PitTelemetry
    flags: FlagTelemetry
    incidents: IncidentTelemetry
    quality: QualityTelemetry
    opponents: OpponentSet
    contract_version: str = TELEMETRY_CONTRACT_VERSION

    def to_dict(self) -> dict[str, object]:
        """Return a nested, JSON-safe record with explicit field metadata."""

        payload = _jsonable(self)
        if not isinstance(payload, dict):  # pragma: no cover - dataclass invariant
            raise TypeError("telemetry serialization did not produce a mapping")
        return payload

    def to_json_line(self) -> str:
        """Return one deterministic JSONL record without a trailing newline."""

        return _canonical_json(self)

    def to_jsonl(self) -> str:
        """Return one deterministic JSONL record including its line terminator."""

        return f"{self.to_json_line()}\n"

    def to_parquet_row(self) -> dict[str, object]:
        """Return a deterministic scalar-only row suitable for Parquet writers.

        Ordinary fields expand into ``value``, ``presence``, ``provenance``, and
        ``source_fields_json`` columns.  Variable-length opponent records are
        stored as canonical JSON so that missing and empty arrays retain one
        stable physical schema across a session.
        """

        row: dict[str, object] = {
            "contract_version": self.contract_version,
            "parquet_schema_version": TELEMETRY_PARQUET_SCHEMA_VERSION,
        }

        def add_field(prefix: str, item: TelemetryField[Any]) -> None:
            value = _jsonable(item.value)
            if isinstance(value, (list, dict)):
                value = _canonical_json(value)
            row[f"{prefix}__value"] = value
            row[f"{prefix}__presence"] = item.presence.value
            row[f"{prefix}__provenance"] = item.provenance.value
            row[f"{prefix}__source_fields_json"] = _canonical_json(item.source_fields)

        for group_name in (
            "source",
            "session",
            "lap",
            "controls",
            "fuel",
            "environment",
            "tires",
            "pit",
            "flags",
            "incidents",
            "quality",
        ):
            group = getattr(self, group_name)
            for item in fields(group):
                add_field(f"{group_name}.{item.name}", getattr(group, item.name))

        add_field("opponents.player_car_idx", self.opponents.player_car_idx)
        row["opponents__presence"] = self.opponents.presence.value
        row["opponents__provenance"] = self.opponents.provenance.value
        row["opponents__entries_json"] = _canonical_json(self.opponents.entries)
        row["opponents__issues_json"] = _canonical_json(self.opponents.issues)
        return dict(sorted(row.items()))


_PARQUET_VALUE_DTYPES = {
    "controls.brake": "Float64",
    "controls.clutch": "Float64",
    "controls.gear": "Int64",
    "controls.rpm": "Float64",
    "controls.steering_angle_rad": "Float64",
    "controls.throttle": "Float64",
    "environment.air_temp_c": "Float64",
    "environment.precipitation_fraction": "Float64",
    "environment.relative_humidity_fraction": "Float64",
    "environment.session_time_of_day_s": "Float64",
    "environment.skies": "Int64",
    "environment.track_temp_c": "Float64",
    "environment.track_temp_crew_c": "Float64",
    "environment.weather_type": "Int64",
    "environment.weather_version": "Int64",
    "environment.wind_direction_rad": "Float64",
    "environment.wind_velocity_mps": "Float64",
    "flags.is_on_track": "Boolean",
    "flags.is_on_track_car": "Boolean",
    "flags.player_track_surface": "Int64",
    "flags.session_flags": "Int64",
    "fuel.level_l": "Float64",
    "fuel.level_pct": "Float64",
    "fuel.use_per_hour_l": "Float64",
    "incidents.player_car_driver_incident_count": "Int64",
    "incidents.player_car_my_incident_count": "Int64",
    "incidents.player_car_team_incident_count": "Int64",
    "lap.lap_distance_pct": "Float64",
    "lap.lap_number": "Int64",
    "lap.laps_completed": "Int64",
    "lap.speed_mps": "Float64",
    "opponents.player_car_idx": "Int64",
    "pit.in_pit_stall": "Boolean",
    "pit.on_pit_road": "Boolean",
    "pit.pits_open": "Boolean",
    "pit.pitstop_active": "Boolean",
    "quality.dropped_ticks": "Int64",
    "quality.issues": "String",
    "quality.stale": "Boolean",
    "quality.status": "String",
    "session.captured_monotonic_s": "Float64",
    "session.sdk_buffer_tick": "Int64",
    "session.session_id": "String",
    "session.session_laps_remaining": "Int64",
    "session.session_num": "Int64",
    "session.session_tick": "Int64",
    "session.session_time_remaining_s": "Float64",
    "session.session_time_s": "Float64",
    "source.source_id": "String",
    "source.source_kind": "String",
    "tires.player_tire_compound": "Int64",
    "tires.tire_sets_used": "Int64",
}


def telemetry_parquet_schema() -> Any:
    """Return the versioned physical Polars schema for telemetry row groups.

    Writers must supply this schema instead of inferring each batch.  In
    particular, the first sample intentionally has null sequence-quality
    values; explicit Boolean/Int64 types keep later row groups compatible.
    """

    import polars as pl

    dtype_by_name = {
        "Boolean": pl.Boolean,
        "Float64": pl.Float64,
        "Int64": pl.Int64,
        "String": pl.String,
    }
    schema: dict[str, Any] = {
        "contract_version": pl.String,
        "parquet_schema_version": pl.String,
    }
    for prefix, dtype_name in _PARQUET_VALUE_DTYPES.items():
        schema[f"{prefix}__value"] = dtype_by_name[dtype_name]
        schema[f"{prefix}__presence"] = pl.String
        schema[f"{prefix}__provenance"] = pl.String
        schema[f"{prefix}__source_fields_json"] = pl.String
    schema.update(
        {
            "opponents__entries_json": pl.String,
            "opponents__issues_json": pl.String,
            "opponents__presence": pl.String,
            "opponents__provenance": pl.String,
        }
    )
    return pl.Schema(dict(sorted(schema.items())))


def _scalar_item(value: object) -> object:
    """Unbox numpy-style scalar objects without importing an array library."""

    item = getattr(value, "item", None)
    if callable(item):
        try:
            scalar = item()
        except (TypeError, ValueError):
            return value
        if not isinstance(scalar, (tuple, list, Mapping)):
            return scalar
    return value


def _normalize_int(value: object) -> int | None:
    value = _scalar_item(value)
    if isinstance(value, bool):
        return None
    try:
        return int(operator.index(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_float(value: object) -> float | None:
    value = _scalar_item(value)
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    normalized = float(value)
    if not math.isfinite(normalized):
        return None
    return 0.0 if normalized == 0.0 else normalized


def _normalize_bool(value: object) -> bool | None:
    value = _scalar_item(value)
    return value if isinstance(value, bool) else None


class _FieldReader:
    def __init__(self, frame: Mapping[str, object]) -> None:
        self.frame = frame
        self.issues: set[str] = set()

    def _read(
        self,
        name: str,
        parser: Any,
        *,
        minimum: float | int | None = None,
        maximum: float | int | None = None,
    ) -> TelemetryField[Any]:
        if name not in self.frame:
            return TelemetryField.missing()
        value = parser(self.frame[name])
        if value is None or (minimum is not None and value < minimum) or (
            maximum is not None and value > maximum
        ):
            self.issues.add(f"INVALID_FIELD:{name}")
            return TelemetryField.invalid(Provenance.SDK_DIRECT, name)
        return TelemetryField.present(value, Provenance.SDK_DIRECT, name)

    def integer(
        self,
        name: str,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> TelemetryField[int]:
        return self._read(name, _normalize_int, minimum=minimum, maximum=maximum)

    def floating(
        self,
        name: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
        boundary_tolerance: float = 0.0,
    ) -> TelemetryField[float]:
        if not math.isfinite(boundary_tolerance) or boundary_tolerance < 0:
            raise ValueError("boundary_tolerance must be finite and non-negative")

        def normalize_bounded(value: object) -> float | None:
            normalized = _normalize_float(value)
            if normalized is None:
                return None
            if (
                minimum is not None
                and minimum - boundary_tolerance <= normalized < minimum
            ):
                return minimum
            if (
                maximum is not None
                and maximum < normalized <= maximum + boundary_tolerance
            ):
                return maximum
            return normalized

        return self._read(
            name,
            normalize_bounded,
            minimum=minimum,
            maximum=maximum,
        )

    def boolean(self, name: str) -> TelemetryField[bool]:
        return self._read(name, _normalize_bool)


_OPPONENT_ARRAY_FIELDS = (
    "CarIdxLap",
    "CarIdxLapCompleted",
    "CarIdxLapDistPct",
    "CarIdxOnPitRoad",
    "CarIdxTrackSurface",
)


def _array_values(value: object) -> tuple[object, ...] | None:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return None
    if not hasattr(value, "__len__") or not hasattr(value, "__getitem__"):
        return None
    try:
        length = len(value)  # type: ignore[arg-type]
        return tuple(value[index] for index in range(length))  # type: ignore[index]
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _opponent_element(
    values: dict[str, tuple[object, ...]],
    field_name: str,
    index: int,
    parser: Any,
    issues: set[str],
) -> TelemetryField[Any]:
    if field_name not in values:
        return TelemetryField.missing()
    parsed = parser(values[field_name][index])
    if parsed is None:
        issues.add(f"INVALID_OPPONENT_VALUE:{field_name}[{index}]")
        return TelemetryField.invalid(Provenance.SDK_DIRECT, field_name)
    return TelemetryField.present(parsed, Provenance.SDK_DIRECT, field_name)


def _normalize_opponents(
    frame: Mapping[str, object],
    player_car_idx: TelemetryField[int],
    *,
    expected_car_count: int | None,
    error_policy: Literal["degrade", "reject"],
) -> OpponentSet:
    present_names = tuple(name for name in _OPPONENT_ARRAY_FIELDS if name in frame)
    if not present_names:
        return OpponentSet(
            Presence.MISSING,
            Provenance.UNKNOWN,
            player_car_idx,
        )

    arrays: dict[str, tuple[object, ...]] = {}
    structural_issues: set[str] = set()
    for name in present_names:
        values = _array_values(frame[name])
        if values is None:
            structural_issues.add(f"OPPONENT_NOT_ARRAY:{name}")
        else:
            arrays[name] = values

    lengths = {len(values) for values in arrays.values()}
    if len(arrays) != len(present_names) or len(lengths) != 1:
        structural_issues.add("OPPONENT_ARRAY_LENGTH_MISMATCH")
    elif expected_car_count is not None and next(iter(lengths)) != expected_car_count:
        structural_issues.add("OPPONENT_ARRAY_EXPECTED_LENGTH_MISMATCH")

    array_length = next(iter(lengths)) if len(lengths) == 1 else None
    if player_car_idx.presence is not Presence.PRESENT:
        structural_issues.add("PLAYER_CAR_IDX_UNAVAILABLE")
    elif array_length is not None and not 0 <= player_car_idx.value < array_length:
        structural_issues.add("PLAYER_CAR_IDX_OUT_OF_RANGE")
    if array_length == 0:
        structural_issues.add("OPPONENT_ARRAY_EMPTY")

    if structural_issues:
        issue_tuple = tuple(sorted(structural_issues))
        if error_policy == "reject":
            raise TelemetryNormalizationError(";".join(issue_tuple))
        return OpponentSet(
            Presence.INVALID,
            Provenance.SDK_DIRECT,
            player_car_idx,
            issues=issue_tuple,
        )

    if array_length is None or player_car_idx.value is None:  # pragma: no cover
        raise AssertionError("validated opponent arrays lost their dimensions")

    element_issues: set[str] = set()
    entries: list[OpponentTelemetry] = []
    for index in range(array_length):
        if index == player_car_idx.value:
            continue
        entries.append(
            OpponentTelemetry(
                car_idx=TelemetryField.present(
                    index,
                    Provenance.DERIVED,
                    *present_names,
                ),
                lap_number=_opponent_element(
                    arrays, "CarIdxLap", index, _normalize_int, element_issues
                ),
                laps_completed=_opponent_element(
                    arrays,
                    "CarIdxLapCompleted",
                    index,
                    _normalize_int,
                    element_issues,
                ),
                lap_distance_pct=_opponent_element(
                    arrays,
                    "CarIdxLapDistPct",
                    index,
                    _normalize_float,
                    element_issues,
                ),
                on_pit_road=_opponent_element(
                    arrays,
                    "CarIdxOnPitRoad",
                    index,
                    _normalize_bool,
                    element_issues,
                ),
                track_surface=_opponent_element(
                    arrays,
                    "CarIdxTrackSurface",
                    index,
                    _normalize_int,
                    element_issues,
                ),
            )
        )
    return OpponentSet(
        Presence.PRESENT,
        Provenance.SDK_DIRECT,
        player_car_idx,
        tuple(entries),
        tuple(sorted(element_issues)),
    )


def _provided_int(
    value: object | None,
    source_name: str,
    issues: set[str],
    *,
    minimum: int | None = None,
) -> TelemetryField[int]:
    if value is None:
        return TelemetryField.missing()
    parsed = _normalize_int(value)
    if parsed is None or (minimum is not None and parsed < minimum):
        issues.add(f"INVALID_CAPTURE_METADATA:{source_name}")
        return TelemetryField.invalid(Provenance.SDK_DIRECT, source_name)
    return TelemetryField.present(parsed, Provenance.SDK_DIRECT, source_name)


def _provided_float(
    value: object | None,
    source_name: str,
    issues: set[str],
) -> TelemetryField[float]:
    if value is None:
        return TelemetryField.missing()
    parsed = _normalize_float(value)
    if parsed is None:
        issues.add(f"INVALID_CAPTURE_METADATA:{source_name}")
        return TelemetryField.invalid(Provenance.DERIVED, source_name)
    return TelemetryField.present(parsed, Provenance.DERIVED, source_name)


def _same_present_value(
    first: TelemetryField[Any], second: TelemetryField[Any]
) -> bool:
    return (
        first.presence is Presence.PRESENT
        and second.presence is Presence.PRESENT
        and first.value == second.value
    )


def _sequence_quality(
    current: SessionTelemetry,
    previous: TelemetrySample | None,
    *,
    source_id: str,
    source_kind: SourceKind,
    stale_after_s: float,
    issues: set[str],
) -> tuple[TelemetryField[bool], TelemetryField[int]]:
    if previous is None:
        issues.add("STALE_UNASSESSED")
        return TelemetryField.missing(), TelemetryField.missing()
    previous_source_id = previous.source.source_id
    previous_source_kind = previous.source.source_kind
    if (
        previous_source_id.presence is not Presence.PRESENT
        or previous_source_id.value != source_id
        or previous_source_kind.presence is not Presence.PRESENT
        or previous_source_kind.value is not source_kind
    ):
        issues.add("SOURCE_BOUNDARY")
        return TelemetryField.missing(), TelemetryField.missing()
    if not _same_present_value(
        current.session_id, previous.session.session_id
    ) and (
        current.session_id.presence is Presence.PRESENT
        or previous.session.session_id.presence is Presence.PRESENT
    ):
        issues.add("SESSION_BOUNDARY")
        return TelemetryField.missing(), TelemetryField.missing()
    if not _same_present_value(current.session_num, previous.session.session_num):
        issues.add("SESSION_BOUNDARY")
        return TelemetryField.missing(), TelemetryField.missing()

    stale_evidence: list[bool] = []
    stale_sources: list[str] = []
    drop_candidates: list[tuple[str, int]] = []
    drop_regression_sources: list[str] = []

    previous_tick = previous.session.session_tick
    if (
        current.session_tick.presence is Presence.PRESENT
        and previous_tick.presence is Presence.PRESENT
    ):
        tick_delta = current.session_tick.value - previous_tick.value
        stale_sources.append("SessionTick")
        if tick_delta < 0:
            issues.add("SESSION_TICK_REGRESSION")
            stale_evidence.append(True)
            drop_regression_sources.append("SessionTick")
        else:
            stale_evidence.append(tick_delta == 0)
            drop_candidates.append(("SessionTick", max(0, tick_delta - 1)))

    previous_buffer_tick = previous.session.sdk_buffer_tick
    if (
        current.sdk_buffer_tick.presence is Presence.PRESENT
        and previous_buffer_tick.presence is Presence.PRESENT
    ):
        buffer_delta = current.sdk_buffer_tick.value - previous_buffer_tick.value
        stale_sources.append("buffer_tick")
        stale_evidence.append(buffer_delta <= 0)
        if buffer_delta < 0:
            issues.add("BUFFER_TICK_REGRESSION")
            drop_regression_sources.append("buffer_tick")
        else:
            drop_candidates.append(("buffer_tick", max(0, buffer_delta - 1)))

    previous_capture = previous.session.captured_monotonic_s
    if (
        current.captured_monotonic_s.presence is Presence.PRESENT
        and previous_capture.presence is Presence.PRESENT
    ):
        capture_delta = current.captured_monotonic_s.value - previous_capture.value
        stale_sources.append("captured_monotonic_s")
        if capture_delta <= 0:
            issues.add("CAPTURE_TIME_REGRESSION")
            stale_evidence.append(True)
        else:
            stale_evidence.append(capture_delta > stale_after_s)

    if not stale_evidence:
        issues.add("STALE_UNASSESSED")
        stale = TelemetryField.missing()
    else:
        stale = TelemetryField.present(
            any(stale_evidence),
            Provenance.DERIVED,
            *dict.fromkeys(stale_sources),
        )
    if drop_regression_sources:
        dropped = TelemetryField.invalid(
            Provenance.DERIVED,
            *drop_regression_sources,
        )
    elif drop_candidates:
        candidate_counts = {count for _, count in drop_candidates}
        if len(candidate_counts) > 1:
            issues.add("TICK_DELTA_DISAGREEMENT")
        dropped = TelemetryField.present(
            max(candidate_counts),
            Provenance.DERIVED,
            *(name for name, _ in drop_candidates),
        )
    else:
        dropped = TelemetryField.missing()
    return stale, dropped


def normalize_sdk_frame(
    frame: Mapping[str, object],
    *,
    source_id: str,
    session_id: str | None = None,
    source_kind: SourceKind = SourceKind.SDK_LIVE,
    buffer_tick: object | None = None,
    captured_monotonic_s: object | None = None,
    previous: TelemetrySample | None = None,
    stale_after_s: float = 0.5,
    expected_car_count: int | None = None,
    opponent_error_policy: Literal["degrade", "reject"] = "degrade",
    continuity_boundary_reasons: tuple[str, ...] = (),
) -> TelemetrySample:
    """Normalize one SDK mapping without filling absent or invalid channels.

    ``previous`` enables stale and dropped-tick assessment.  Array structure or
    ``PlayerCarIdx`` errors never emit potentially shifted opponent records:
    default behavior marks the whole opponent set invalid, while ``reject``
    raises :class:`TelemetryNormalizationError`.
    """

    if not isinstance(frame, Mapping):
        raise TypeError("frame must be a mapping")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id must be a non-empty string")
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise ValueError("session_id must be None or a non-empty string")
    if not isinstance(source_kind, SourceKind):
        raise TypeError("source_kind must be a SourceKind enum")
    if not isinstance(stale_after_s, Real) or isinstance(stale_after_s, bool):
        raise TypeError("stale_after_s must be a finite positive number")
    stale_after_s = float(stale_after_s)
    if not math.isfinite(stale_after_s) or stale_after_s <= 0:
        raise ValueError("stale_after_s must be a finite positive number")
    if expected_car_count is not None and (
        isinstance(expected_car_count, bool)
        or not isinstance(expected_car_count, int)
        or expected_car_count <= 0
    ):
        raise ValueError("expected_car_count must be a positive integer")
    if opponent_error_policy not in {"degrade", "reject"}:
        raise ValueError("opponent_error_policy must be 'degrade' or 'reject'")
    if previous is not None and not isinstance(previous, TelemetrySample):
        raise TypeError("previous must be a TelemetrySample or None")
    if (
        type(continuity_boundary_reasons) is not tuple
        or any(
            type(reason) is not str
            or not reason
            or len(reason) > 128
            or any(ord(character) < 32 for character in reason)
            for reason in continuity_boundary_reasons
        )
        or len(continuity_boundary_reasons) != len(set(continuity_boundary_reasons))
    ):
        raise ValueError(
            "continuity_boundary_reasons must be unique non-empty plain strings"
        )

    reader = _FieldReader(frame)
    metadata_issues: set[str] = set()
    session = SessionTelemetry(
        session_id=(
            TelemetryField.present(session_id, Provenance.USER_RULE)
            if session_id is not None
            else TelemetryField.missing()
        ),
        session_num=reader.integer("SessionNum"),
        session_tick=reader.integer("SessionTick", minimum=0),
        sdk_buffer_tick=_provided_int(
            buffer_tick,
            "buffer_tick",
            metadata_issues,
            minimum=0,
        ),
        session_time_s=reader.floating("SessionTime"),
        captured_monotonic_s=_provided_float(
            captured_monotonic_s,
            "captured_monotonic_s",
            metadata_issues,
        ),
        session_time_remaining_s=reader.floating("SessionTimeRemain"),
        session_laps_remaining=reader.integer("SessionLapsRemainEx"),
    )
    lap = LapTelemetry(
        lap_number=reader.integer("Lap", minimum=0),
        laps_completed=reader.integer("LapCompleted", minimum=0),
        lap_distance_pct=reader.floating("LapDistPct", minimum=-1.0, maximum=1.1),
        speed_mps=reader.floating("Speed", minimum=0.0),
    )
    controls = ControlTelemetry(
        throttle=reader.floating(
            "Throttle", minimum=0.0, maximum=1.0, boundary_tolerance=1e-6
        ),
        brake=reader.floating(
            "Brake", minimum=0.0, maximum=1.0, boundary_tolerance=1e-6
        ),
        clutch=reader.floating(
            "Clutch", minimum=0.0, maximum=1.0, boundary_tolerance=1e-6
        ),
        steering_angle_rad=reader.floating("SteeringWheelAngle"),
        gear=reader.integer("Gear"),
        rpm=reader.floating("RPM", minimum=0.0),
    )
    fuel = FuelTelemetry(
        level_l=reader.floating("FuelLevel", minimum=0.0),
        level_pct=reader.floating(
            "FuelLevelPct", minimum=0.0, maximum=1.0, boundary_tolerance=1e-6
        ),
        use_per_hour_l=reader.floating("FuelUsePerHour", minimum=0.0),
    )
    environment = EnvironmentTelemetry(
        session_time_of_day_s=reader.floating("SessionTimeOfDay", minimum=0.0),
        track_temp_c=reader.floating("TrackTemp"),
        track_temp_crew_c=reader.floating("TrackTempCrew"),
        air_temp_c=reader.floating("AirTemp"),
        weather_type=reader.integer("WeatherType", minimum=0),
        weather_version=reader.integer("WeatherVersion", minimum=0),
        skies=reader.integer("Skies", minimum=0),
        wind_velocity_mps=reader.floating("WindVel", minimum=0.0),
        wind_direction_rad=reader.floating("WindDir"),
        relative_humidity_fraction=reader.floating(
            "RelativeHumidity",
            minimum=0.0,
            maximum=1.0,
            boundary_tolerance=1e-6,
        ),
        precipitation_fraction=reader.floating(
            "Precipitation",
            minimum=0.0,
            maximum=1.0,
            boundary_tolerance=1e-6,
        ),
    )
    tires = TireTelemetry(
        player_tire_compound=reader.integer("PlayerTireCompound", minimum=0),
        tire_sets_used=reader.integer("TireSetsUsed", minimum=0),
    )
    pit = PitTelemetry(
        on_pit_road=reader.boolean("OnPitRoad"),
        in_pit_stall=reader.boolean("PlayerCarInPitStall"),
        pitstop_active=reader.boolean("PitstopActive"),
        pits_open=reader.boolean("PitsOpen"),
    )
    flags = FlagTelemetry(
        session_flags=reader.integer("SessionFlags", minimum=0),
        player_track_surface=reader.integer("PlayerTrackSurface"),
        is_on_track=reader.boolean("IsOnTrack"),
        is_on_track_car=reader.boolean("IsOnTrackCar"),
    )
    incidents = IncidentTelemetry(
        player_car_my_incident_count=reader.integer(
            "PlayerCarMyIncidentCount", minimum=0
        ),
        player_car_driver_incident_count=reader.integer(
            "PlayerCarDriverIncidentCount", minimum=0
        ),
        player_car_team_incident_count=reader.integer(
            "PlayerCarTeamIncidentCount", minimum=0
        ),
    )
    player_car_idx = reader.integer("PlayerCarIdx")
    opponents = _normalize_opponents(
        frame,
        player_car_idx,
        expected_car_count=expected_car_count,
        error_policy=opponent_error_policy,
    )

    issue_set = set(reader.issues) | metadata_issues | set(opponents.issues)
    issue_set.update(
        f"CONTINUITY_BOUNDARY:{reason}"
        for reason in continuity_boundary_reasons
    )
    stale, dropped_ticks = _sequence_quality(
        session,
        previous,
        source_id=source_id,
        source_kind=source_kind,
        stale_after_s=stale_after_s,
        issues=issue_set,
    )

    critical = {
        "SessionNum": session.session_num,
        "SessionTick": session.session_tick,
        "SessionTime": session.session_time_s,
    }
    rejected = False
    for name, item in critical.items():
        if item.presence is not Presence.PRESENT:
            issue_set.add(f"CORE_{item.presence.value}:{name}")
            rejected = True
    if stale.presence is Presence.PRESENT and stale.value:
        issue_set.add("SOURCE_STALE")
        rejected = True
    if {
        "SESSION_TICK_REGRESSION",
        "BUFFER_TICK_REGRESSION",
        "CAPTURE_TIME_REGRESSION",
    } & issue_set:
        rejected = True

    dropped_count = dropped_ticks.value if dropped_ticks.presence is Presence.PRESENT else 0
    degraded = bool(
        issue_set
        or opponents.presence is Presence.INVALID
        or dropped_count > 0
    )
    if dropped_count > 0:
        issue_set.add(f"DROPPED_TICKS:{dropped_count}")
    status = (
        QualityStatus.REJECTED
        if rejected
        else QualityStatus.DEGRADED
        if degraded
        else QualityStatus.READY
    )
    issue_tuple = tuple(sorted(issue_set))
    quality = QualityTelemetry(
        stale=stale,
        dropped_ticks=dropped_ticks,
        status=TelemetryField.present(status, Provenance.DERIVED),
        issues=TelemetryField.present(issue_tuple, Provenance.DERIVED),
    )
    return TelemetrySample(
        source=SourceTelemetry(
            source_id=TelemetryField.present(source_id, Provenance.USER_RULE),
            source_kind=TelemetryField.present(source_kind, Provenance.USER_RULE),
        ),
        session=session,
        lap=lap,
        controls=controls,
        fuel=fuel,
        environment=environment,
        tires=tires,
        pit=pit,
        flags=flags,
        incidents=incidents,
        quality=quality,
        opponents=opponents,
    )
