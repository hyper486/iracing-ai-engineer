"""Read-only, fail-closed probe for iRacing's Windows shared-memory SDK."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import struct
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from .contracts import SDK_PROBE_CONTRACT_VERSION

SDK_TYPE_NAMES = {
    0: "char",
    1: "bool",
    2: "int32",
    3: "uint32_or_bitfield",
    4: "float32",
    5: "float64",
}
SDK_TYPE_SIZES = {0: 1, 1: 1, 2: 4, 3: 4, 4: 4, 5: 8}
SUPPORTED_PYIRSDK_VERSION = "1.3.6"
SDK_FIXED_HEADER_SIZE = 48
SDK_MAX_BUFFERS = 4
SDK_BUFFER_HEADER_SIZE = 16
SDK_HEADER_REGION_SIZE = SDK_FIXED_HEADER_SIZE + SDK_MAX_BUFFERS * SDK_BUFFER_HEADER_SIZE
SDK_VARIABLE_HEADER_SIZE = 144
SDK_SESSION_INFO_SNAPSHOT_ATTEMPTS = 3

REPLAY_FIELDS = (
    "IsReplayPlaying",
    "ReplayFrameNum",
    "ReplayFrameNumEnd",
    "ReplayPlaySpeed",
    "ReplaySessionNum",
    "ReplaySessionTime",
)
SESSION_CLOCK_FIELDS = ("SessionTime", "SessionTick", "SessionNum")
LAP_POSITION_FIELDS = (
    "Lap",
    "LapDistPct",
    "Speed",
    "PlayerCarIdx",
    "OnPitRoad",
    "PlayerTrackSurface",
)
DRIVING_CONTROL_FIELDS = LAP_POSITION_FIELDS + (
    "Throttle",
    "Brake",
    "SteeringWheelAngle",
    "Gear",
    "RPM",
    "IsOnTrack",
    "IsOnTrackCar",
)
FUEL_FIELDS = ("FuelLevel", "OnPitRoad", "PlayerCarInPitStall")
RACE_STRATEGY_FIELDS = SESSION_CLOCK_FIELDS + (
    "Lap",
    "LapDistPct",
    "FuelLevel",
    "OnPitRoad",
    "SessionFlags",
    "PitsOpen",
)
RACE_STRATEGY_REMAINING_FIELDS = ("SessionTimeRemain", "SessionLapsRemainEx")
OPPONENT_ARRAY_FIELDS = (
    "CarIdxLap",
    "CarIdxLapCompleted",
    "CarIdxLapDistPct",
    "CarIdxOnPitRoad",
    "CarIdxTrackSurface",
)
OPPONENT_FIELDS = ("PlayerCarIdx",) + OPPONENT_ARRAY_FIELDS
ENVIRONMENT_FIELDS = (
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
)
TIRE_FIELDS = ("PlayerTireCompound", "TireSetsUsed")
AUXILIARY_FIELDS = (
    "IsOnTrackCar",
    "FuelLevelPct",
    "FuelUsePerHour",
    "SessionTimeRemain",
    "SessionLapsRemainEx",
)
TARGET_FIELDS = tuple(
    dict.fromkeys(
        REPLAY_FIELDS
        + SESSION_CLOCK_FIELDS
        + DRIVING_CONTROL_FIELDS
        + FUEL_FIELDS
        + RACE_STRATEGY_FIELDS
        + OPPONENT_FIELDS
        + ENVIRONMENT_FIELDS
        + TIRE_FIELDS
        + AUXILIARY_FIELDS
    )
)

FIELD_EXPECTED_TYPES: dict[str, frozenset[int]] = {
    **{
        field: frozenset({1})
        for field in (
            "IsReplayPlaying",
            "IsOnTrack",
            "IsOnTrackCar",
            "OnPitRoad",
            "PlayerCarInPitStall",
            "PitsOpen",
            "CarIdxOnPitRoad",
        )
    },
    **{
        field: frozenset({2})
        for field in (
            "ReplayFrameNum",
            "ReplayFrameNumEnd",
            "ReplayPlaySpeed",
            "ReplaySessionNum",
            "SessionTick",
            "SessionNum",
            "Lap",
            "PlayerCarIdx",
            "PlayerTrackSurface",
            "Gear",
            "SessionLapsRemainEx",
            "CarIdxLap",
            "CarIdxLapCompleted",
            "CarIdxTrackSurface",
            "WeatherType",
            "WeatherVersion",
            "Skies",
            "PlayerTireCompound",
            "TireSetsUsed",
        )
    },
    "SessionFlags": frozenset({3}),
    **{
        field: frozenset({4, 5})
        for field in (
            "ReplaySessionTime",
            "SessionTime",
            "LapDistPct",
            "Speed",
            "Throttle",
            "Brake",
            "SteeringWheelAngle",
            "RPM",
            "FuelLevel",
            "FuelLevelPct",
            "FuelUsePerHour",
            "SessionTimeRemain",
            "CarIdxLapDistPct",
            "SessionTimeOfDay",
            "TrackTemp",
            "TrackTempCrew",
            "AirTemp",
            "WindVel",
            "WindDir",
            "RelativeHumidity",
            "Precipitation",
        )
    },
}
ARRAY_FIELDS = frozenset(OPPONENT_ARRAY_FIELDS)


class SdkProbeUnavailable(RuntimeError):
    """Raised when the Windows SDK transport cannot be reached."""


class SdkProbeConsistencyError(RuntimeError):
    """Raised when the SDK header or frozen-read contract is invalid."""


@dataclass(frozen=True)
class VariableDescriptor:
    name: str
    type_code: int
    dtype: str
    offset: int
    count: int
    count_as_time: bool
    unit: str
    description: str


@dataclass(frozen=True)
class RawSdkFrame:
    buffer_tick: int
    session_info_update: int
    values: dict[str, Any]
    read_errors: tuple[str, ...] = ()
    sim_mode_raw: Any = None
    captured_monotonic_s: float | None = None


def _bind_frame_sim_mode(
    frame: RawSdkFrame, sim_mode_raw: Any, session_info_update: int | None
) -> RawSdkFrame:
    return replace(
        frame,
        sim_mode_raw=(
            sim_mode_raw
            if session_info_update == frame.session_info_update
            else None
        ),
    )


@dataclass(frozen=True)
class ConnectionMeta:
    startup_ok: bool
    initialized: bool
    connected: bool
    header_version: int
    raw_header_status: int
    tick_rate_hz: int
    variable_count: int
    buffer_count: int
    buffer_len: int


@dataclass(frozen=True)
class _SdkLayout:
    version: int
    status: int
    tick_rate: int
    session_info_update: int
    session_info_len: int
    session_info_offset: int
    num_vars: int
    var_header_offset: int
    num_buf: int
    buf_len: int
    buffer_offsets: tuple[int, ...]
    schema_bytes: bytes

    @property
    def static_signature(self) -> tuple[Any, ...]:
        return (
            self.version,
            self.tick_rate,
            self.session_info_offset,
            self.num_vars,
            self.var_header_offset,
            self.num_buf,
            self.buf_len,
            self.buffer_offsets,
        )


def _parse_sdk_layout(shared_memory: Any) -> _SdkLayout:
    mapped_size = len(shared_memory)
    if mapped_size < SDK_HEADER_REGION_SIZE:
        raise SdkProbeConsistencyError("SDK shared memory is smaller than its header")
    raw_header = bytes(shared_memory[:SDK_HEADER_REGION_SIZE])
    (
        version,
        status,
        tick_rate,
        session_info_update,
        session_info_len,
        session_info_offset,
        num_vars,
        var_header_offset,
        num_buf,
        buf_len,
    ) = struct.unpack_from("<10i", raw_header, 0)
    current_buffer = raw_header[44]
    if version != 2:
        raise SdkProbeConsistencyError("unsupported SDK header version")
    if (
        not 1 <= tick_rate <= 360
        or not 1 <= num_buf <= SDK_MAX_BUFFERS
        or not 1 <= num_vars <= 4096
        or buf_len <= 0
        or session_info_len < 0
        or not 0 <= session_info_offset <= mapped_size
        or current_buffer >= num_buf
    ):
        raise SdkProbeConsistencyError("invalid SDK sampling layout")
    if session_info_len == 0:
        raise SdkProbeUnavailable("iRacing SDK SessionInfo is not ready")

    var_header_end = var_header_offset + num_vars * SDK_VARIABLE_HEADER_SIZE
    session_info_end = session_info_offset + session_info_len
    buffer_offsets = tuple(
        struct.unpack_from("<i", raw_header, SDK_FIXED_HEADER_SIZE + index * 16 + 4)[0]
        for index in range(num_buf)
    )
    ranges: list[tuple[str, int, int]] = [
        ("header", 0, SDK_HEADER_REGION_SIZE),
        ("variable_headers", var_header_offset, var_header_end),
        *(
            [("session_info", session_info_offset, session_info_end)]
            if session_info_len
            else []
        ),
        *[
            (f"buffer_{index}", offset, offset + buf_len)
            for index, offset in enumerate(buffer_offsets)
        ],
    ]
    if any(start < 0 or end <= start or end > mapped_size for _, start, end in ranges):
        raise SdkProbeConsistencyError("SDK sections extend outside shared memory")
    ordered_ranges = sorted(ranges, key=lambda item: (item[1], item[2]))
    if any(
        current[1] < previous[2]
        for previous, current in zip(ordered_ranges, ordered_ranges[1:], strict=False)
    ):
        raise SdkProbeConsistencyError("SDK sections overlap")
    schema_bytes = bytes(shared_memory[var_header_offset:var_header_end])
    if len(schema_bytes) != num_vars * SDK_VARIABLE_HEADER_SIZE:
        raise SdkProbeConsistencyError("SDK variable header table is truncated")
    return _SdkLayout(
        version=version,
        status=status,
        tick_rate=tick_rate,
        session_info_update=session_info_update,
        session_info_len=session_info_len,
        session_info_offset=session_info_offset,
        num_vars=num_vars,
        var_header_offset=var_header_offset,
        num_buf=num_buf,
        buf_len=buf_len,
        buffer_offsets=buffer_offsets,
        schema_bytes=schema_bytes,
    )


def _stable_sdk_layout(shared_memory: Any) -> _SdkLayout:
    first = _parse_sdk_layout(shared_memory)
    second = _parse_sdk_layout(shared_memory)
    if (
        first.static_signature != second.static_signature
        or first.session_info_update != second.session_info_update
        or first.session_info_len != second.session_info_len
        or first.schema_bytes != second.schema_bytes
    ):
        raise SdkProbeConsistencyError("SDK layout changed during validation")
    return second


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == 0:
            return 0.0
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return repr(value)


def _value_digest(value: Any) -> str:
    payload = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _all_finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(_all_finite(item) for item in value)
    return True


def _scalar_summary(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _jsonable(value)
    return None


def _descriptor_matches_field(field: str, descriptor: VariableDescriptor) -> bool:
    expected_types = FIELD_EXPECTED_TYPES.get(field)
    if expected_types is None or descriptor.type_code not in expected_types:
        return False
    if field in ARRAY_FIELDS:
        return descriptor.count > 1
    return descriptor.count == 1


def _value_matches_shape(field: str, descriptor: VariableDescriptor, value: Any) -> bool:
    if field in ARRAY_FIELDS:
        return isinstance(value, (list, tuple)) and len(value) == descriptor.count
    return descriptor.count == 1 and not isinstance(value, (list, tuple))


def _canonical_schema(descriptors: tuple[VariableDescriptor, ...]) -> list[dict[str, Any]]:
    return [asdict(item) for item in sorted(descriptors, key=lambda item: item.name)]


def schema_sha256(descriptors: tuple[VariableDescriptor, ...]) -> str:
    payload = json.dumps(
        _canonical_schema(descriptors), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_context(sim_mode_raw: Any, latest_values: dict[str, Any]) -> dict[str, Any]:
    normalized_mode = str(sim_mode_raw).strip().lower() if sim_mode_raw is not None else ""
    is_on_track = latest_values.get("IsOnTrack")
    is_on_track_car = latest_values.get("IsOnTrackCar")
    is_replay_playing = latest_values.get("IsReplayPlaying")
    evidence: list[str] = []
    conflicts: list[str] = []

    if normalized_mode == "replay":
        sim_source_mode = "REPLAY_FILE"
        player_control_state = "OUT_OF_CAR_OR_REPLAY_VIEW"
        evidence.append("WeekendInfo.SimMode=replay")
        if is_on_track is True:
            conflicts.append("REPLAY_FILE_WITH_IS_ON_TRACK_TRUE")
    elif normalized_mode == "full":
        sim_source_mode = "FULL"
        evidence.append("WeekendInfo.SimMode=full")
        if is_on_track is True and is_on_track_car is True and is_replay_playing is False:
            player_control_state = "IN_CAR_PHYSICS"
            evidence.append("IsOnTrack=true, IsOnTrackCar=true, IsReplayPlaying=false")
        elif is_on_track is True:
            player_control_state = "UNKNOWN"
            if is_replay_playing is True:
                conflicts.append("IS_ON_TRACK_WITH_REPLAY_PLAYING")
            if is_on_track_car is not True:
                conflicts.append("IS_ON_TRACK_WITHOUT_ON_TRACK_CAR")
        elif is_on_track_car is True and is_replay_playing is True:
            player_control_state = "OUT_OF_CAR_OR_REPLAY_VIEW"
            evidence.append("IsOnTrackCar=true and IsReplayPlaying=true")
        elif is_on_track_car is True:
            player_control_state = "TEAMMATE_OR_OUT_OF_CAR"
            evidence.append("IsOnTrack=false and IsOnTrackCar=true")
        else:
            player_control_state = "OUT_OF_CAR_OR_REPLAY_VIEW"
            evidence.append("IsOnTrack is not true")
    else:
        sim_source_mode = "UNKNOWN"
        player_control_state = "UNKNOWN"
        evidence.append("WeekendInfo.SimMode missing or unrecognized")

    confidence = "HIGH" if sim_source_mode != "UNKNOWN" and not conflicts else "LOW"
    return {
        "sim_mode_raw": sim_mode_raw,
        "sim_source_mode": sim_source_mode,
        "player_control_state": player_control_state,
        "is_on_track": is_on_track,
        "is_on_track_car": is_on_track_car,
        "is_replay_playing": is_replay_playing,
        "evidence": evidence,
        "conflicts": conflicts,
        "confidence": confidence,
    }


def _field_availability(
    descriptors: tuple[VariableDescriptor, ...],
    frames: tuple[RawSdkFrame, ...],
    source_advancing: bool,
) -> dict[str, dict[str, Any]]:
    descriptor_by_name = {item.name: item for item in descriptors}
    result: dict[str, dict[str, Any]] = {}
    for field in TARGET_FIELDS:
        descriptor = descriptor_by_name.get(field)
        if descriptor is None:
            result[field] = {"status": "ABSENT", "sample_count": 0}
            continue
        values = [frame.values[field] for frame in frames if field in frame.values]
        read_failed = any(field in frame.read_errors for frame in frames)
        invalid = (
            read_failed
            or not _descriptor_matches_field(field, descriptor)
            or any(
                not _all_finite(value) or not _value_matches_shape(field, descriptor, value)
                for value in values
            )
        )
        if invalid:
            status = "INVALID"
        elif not values:
            status = "DECLARED"
        elif source_advancing:
            status = "OBSERVED_SOURCE_ADVANCING"
        else:
            status = "READABLE"
        payload: dict[str, Any] = {
            "status": status,
            "sample_count": len(values),
            "declared_count": descriptor.count,
            "distinct_value_count": len({_value_digest(value) for value in values}),
        }
        if values:
            payload["first_scalar"] = _scalar_summary(values[0])
            payload["last_scalar"] = _scalar_summary(values[-1])
        result[field] = payload
    return result


def _capability(
    fields: tuple[str, ...],
    availability: dict[str, dict[str, Any]],
    *,
    sdk_ready: bool,
    context_trusted: bool,
    blocked_reason: str | None = None,
    required_any: tuple[str, ...] = (),
) -> dict[str, Any]:
    missing = [field for field in fields if availability[field]["status"] == "ABSENT"]
    invalid = [field for field in fields if availability[field]["status"] == "INVALID"]
    unreadable = [
        field
        for field in fields
        if availability[field]["status"] in {"ABSENT", "DECLARED", "INVALID"}
    ]
    any_readable = not required_any or any(
        availability[field]["status"] not in {"ABSENT", "DECLARED", "INVALID"}
        for field in required_any
    )
    if required_any and not any_readable:
        missing.extend(
            field for field in required_any if availability[field]["status"] == "ABSENT"
        )
        invalid.extend(
            field for field in required_any if availability[field]["status"] == "INVALID"
        )
    blocked_by_context = [blocked_reason] if blocked_reason and not context_trusted else []
    status = (
        "BLOCKED"
        if blocked_by_context or not sdk_ready or unreadable or not any_readable
        else "READY"
    )
    return {
        "status": status,
        "missing": missing,
        "invalid": invalid,
        "blocked_by_context": blocked_by_context,
        "evidence": [
            field
            for field in fields + required_any
            if availability[field]["status"] not in {"ABSENT", "DECLARED", "INVALID"}
        ],
        "reasons": (["SDK_NOT_READY"] if not sdk_ready else [])
        + (["REQUIRED_FIELDS_UNREADABLE"] if unreadable else [])
        + (["NO_REMAINING_DISTANCE_FIELD"] if not any_readable else []),
    }


def _validate_opponent_arrays(
    descriptors: tuple[VariableDescriptor, ...], frames: tuple[RawSdkFrame, ...]
) -> list[str]:
    descriptor_by_name = {item.name: item for item in descriptors}
    counts = {
        descriptor_by_name[field].count
        for field in OPPONENT_ARRAY_FIELDS
        if field in descriptor_by_name
    }
    problems: list[str] = []
    if len(counts) > 1:
        problems.append("OPPONENT_ARRAY_LENGTH_MISMATCH")
    if frames and counts and "PlayerCarIdx" in frames[-1].values:
        player_index = frames[-1].values["PlayerCarIdx"]
        count = next(iter(counts))
        if not isinstance(player_index, int) or not 0 <= player_index < count:
            problems.append("PLAYER_CAR_INDEX_OUT_OF_RANGE")
    elif frames:
        problems.append("PLAYER_CAR_INDEX_MISSING")
    return problems


def _context_signature(context: dict[str, Any]) -> tuple[Any, ...]:
    return (
        context["sim_source_mode"],
        context["player_control_state"],
        context["is_on_track"],
        context["is_on_track_car"],
        context["is_replay_playing"],
        tuple(context["conflicts"]),
    )


def _latest_context_epoch(
    frames: tuple[RawSdkFrame, ...],
) -> tuple[tuple[RawSdkFrame, ...], list[dict[str, Any]]]:
    contexts = [
        classify_context(frame.sim_mode_raw, frame.values)
        for frame in frames
    ]
    if not frames:
        return (), contexts
    latest_signature = _context_signature(contexts[-1])
    start = len(frames) - 1
    while start > 0 and _context_signature(contexts[start - 1]) == latest_signature:
        start -= 1
    return frames[start:], contexts


def _finite_clock_value(frame: RawSdkFrame, field: str) -> int | float | None:
    value = frame.values.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _latest_session_epoch(
    frames: tuple[RawSdkFrame, ...],
) -> tuple[tuple[RawSdkFrame, ...], bool, bool]:
    """Return the latest session epoch and whether identity/clock resets were seen."""
    start = 0
    identity_changed = False
    clock_reset = False
    for index, (previous, current) in enumerate(
        zip(frames, frames[1:], strict=False), start=1
    ):
        previous_session = _finite_clock_value(previous, "SessionNum")
        current_session = _finite_clock_value(current, "SessionNum")
        identity_boundary = (
            previous_session is not None
            and current_session is not None
            and current_session != previous_session
        )
        previous_tick = _finite_clock_value(previous, "SessionTick")
        current_tick = _finite_clock_value(current, "SessionTick")
        previous_time = _finite_clock_value(previous, "SessionTime")
        current_time = _finite_clock_value(current, "SessionTime")
        reset_boundary = (
            previous_tick is not None
            and current_tick is not None
            and current_tick < previous_tick
        ) or (
            previous_time is not None
            and current_time is not None
            and current_time < previous_time - 1e-6
        )
        if identity_boundary or reset_boundary:
            start = index
            identity_changed = identity_changed or identity_boundary
            clock_reset = clock_reset or reset_boundary
    return frames[start:], identity_changed, clock_reset


def _session_clock_advances(frames: tuple[RawSdkFrame, ...]) -> bool:
    if len(frames) < 2:
        return False
    session_nums = [_finite_clock_value(frame, "SessionNum") for frame in frames]
    ticks = [_finite_clock_value(frame, "SessionTick") for frame in frames]
    times = [_finite_clock_value(frame, "SessionTime") for frame in frames]
    if any(value is None for value in session_nums + ticks + times):
        return False
    if len(set(session_nums)) != 1:
        return False
    ticks_monotonic = all(
        current >= previous for previous, current in zip(ticks, ticks[1:], strict=False)
    )
    times_monotonic = all(
        current >= previous for previous, current in zip(times, times[1:], strict=False)
    )
    clock_progressed = ticks[-1] > ticks[0] or times[-1] > times[0]
    return ticks_monotonic and times_monotonic and clock_progressed


def build_probe_report(
    *,
    connection: ConnectionMeta,
    descriptors: tuple[VariableDescriptor, ...],
    frames: tuple[RawSdkFrame, ...],
    sim_mode_raw: Any,
    sample_duration_s: float,
    platform_name: str,
    include_full_schema: bool,
    ended_connected: bool | None = None,
    probe_start_monotonic_s: float | None = None,
    probe_end_monotonic_s: float | None = None,
    stale_after_s: float = 0.5,
) -> dict[str, Any]:
    if not math.isfinite(stale_after_s) or stale_after_s <= 0:
        raise ValueError("stale_after_s must be finite and positive")
    all_ticks = [frame.buffer_tick for frame in frames]
    context_frames, frame_contexts = _latest_context_epoch(frames)
    stable_frames, session_identity_changed, session_clock_reset = _latest_session_epoch(
        context_frames
    )
    context_ticks = [frame.buffer_tick for frame in context_frames]
    stable_ticks = [frame.buffer_tick for frame in stable_frames]
    ticks_strictly_advance = len(stable_ticks) >= 2 and all(
        current > previous
        for previous, current in zip(stable_ticks, stable_ticks[1:], strict=False)
    )
    ended_connected = connection.connected if ended_connected is None else ended_connected
    first_tick_delay_s: float | None = None
    max_inter_tick_gap_s: float | None = None
    seconds_since_last_tick: float | None = None
    continuity_checked = (
        probe_start_monotonic_s is not None and probe_end_monotonic_s is not None
    )
    timestamps = [frame.captured_monotonic_s for frame in frames]
    timestamps_valid = bool(frames) and all(
        value is not None and math.isfinite(value) for value in timestamps
    )
    timestamps_monotonic = timestamps_valid and all(
        current >= previous
        for previous, current in zip(timestamps, timestamps[1:], strict=False)
    )
    if continuity_checked and timestamps_monotonic:
        first_tick_delay_s = max(0.0, timestamps[0] - probe_start_monotonic_s)
        gaps = [
            current - previous
            for previous, current in zip(timestamps, timestamps[1:], strict=False)
        ]
        max_inter_tick_gap_s = max(gaps, default=0.0)
    if (
        probe_end_monotonic_s is not None
        and stable_frames
        and stable_frames[-1].captured_monotonic_s is not None
    ):
        seconds_since_last_tick = max(
            0.0, probe_end_monotonic_s - stable_frames[-1].captured_monotonic_s
        )
    stale_at_end = (
        seconds_since_last_tick is not None and seconds_since_last_tick > stale_after_s
    )
    source_gap_during_probe = bool(
        continuity_checked
        and timestamps_monotonic
        and (
            first_tick_delay_s > stale_after_s
            or max_inter_tick_gap_s > stale_after_s
        )
    )
    continuity_ok = (
        continuity_checked
        and timestamps_monotonic
        and not source_gap_during_probe
        and not stale_at_end
    )
    source_advancing = ticks_strictly_advance and ended_connected and continuity_ok
    context = (
        frame_contexts[-1]
        if frame_contexts
        else classify_context(sim_mode_raw, {})
    )
    availability = _field_availability(descriptors, stable_frames, source_advancing)
    context_fields_trusted = all(
        availability[field]["status"] not in {"ABSENT", "DECLARED", "INVALID"}
        for field in ("IsOnTrack", "IsOnTrackCar", "IsReplayPlaying")
    )
    context_trusted = (
        context["sim_source_mode"] == "FULL"
        and context["player_control_state"] == "IN_CAR_PHYSICS"
        and not context["conflicts"]
        and context_fields_trusted
    )
    context_reason = (
        None
        if context_trusted
        else f"{context['sim_source_mode']}:{context['player_control_state']}"
    )
    schema_count_matches = connection.variable_count == len(descriptors)
    sdk_ready = (
        connection.connected
        and connection.initialized
        and ended_connected
        and source_advancing
        and schema_count_matches
    )
    session_clock_ready = sdk_ready and _session_clock_advances(stable_frames)

    capabilities: dict[str, dict[str, Any]] = {
        "sdk_connection": {
            "status": "READY" if sdk_ready else "BLOCKED",
            "missing": [],
            "invalid": [],
            "blocked_by_context": [],
            "evidence": ["connected", "distinct_buffer_ticks"] if sdk_ready else [],
            "reasons": [] if sdk_ready else ["SDK_NOT_CONNECTED_OR_STALE"],
        },
        "session_clock": _capability(
            SESSION_CLOCK_FIELDS,
            availability,
            sdk_ready=session_clock_ready,
            context_trusted=True,
        ),
        "lap_position": _capability(
            LAP_POSITION_FIELDS,
            availability,
            sdk_ready=sdk_ready,
            context_trusted=context_trusted,
            blocked_reason=context_reason,
        ),
        "driving_controls": _capability(
            DRIVING_CONTROL_FIELDS,
            availability,
            sdk_ready=sdk_ready,
            context_trusted=context_trusted,
            blocked_reason=context_reason,
        ),
        "fuel_direct": _capability(
            FUEL_FIELDS,
            availability,
            sdk_ready=sdk_ready,
            context_trusted=context_trusted,
            blocked_reason=context_reason,
        ),
        "race_strategy_core": _capability(
            RACE_STRATEGY_FIELDS,
            availability,
            sdk_ready=session_clock_ready,
            context_trusted=context_trusted,
            blocked_reason=context_reason,
            required_any=RACE_STRATEGY_REMAINING_FIELDS,
        ),
        "opponent_tracking": _capability(
            OPPONENT_FIELDS,
            availability,
            sdk_ready=sdk_ready,
            context_trusted=context_trusted,
            blocked_reason=context_reason,
        ),
    }

    if context["sim_source_mode"] == "REPLAY_FILE":
        capabilities["replay_control_only"] = _capability(
            REPLAY_FIELDS,
            availability,
            sdk_ready=sdk_ready,
            context_trusted=True,
        )
    elif context["sim_source_mode"] == "FULL":
        capabilities["replay_control_only"] = {
            "status": "NOT_APPLICABLE",
            "missing": [],
            "invalid": [],
            "blocked_by_context": [],
            "evidence": [],
            "reasons": ["NOT_REPLAY_FILE_CONTEXT"],
        }
    else:
        capabilities["replay_control_only"] = {
            "status": "BLOCKED",
            "missing": [],
            "invalid": [],
            "blocked_by_context": [context_reason],
            "evidence": [],
            "reasons": ["SIM_MODE_UNVERIFIED"],
        }

    warnings: list[str] = []
    session_updates = {frame.session_info_update for frame in frames}
    session_nums = {_value_digest(frame.values.get("SessionNum")) for frame in frames}
    if len({_context_signature(item) for item in frame_contexts}) > 1:
        warnings.append("CONTEXT_CHANGED_DURING_PROBE")
    if len(session_updates) > 1:
        warnings.append("SESSION_INFO_CHANGED_DURING_PROBE")
    if len(session_nums) > 1 or session_identity_changed:
        warnings.append("SESSION_IDENTITY_CHANGED_DURING_PROBE")
    if session_clock_reset:
        warnings.append("SESSION_CLOCK_RESET_DURING_PROBE")
    opponent_problems = _validate_opponent_arrays(descriptors, frames)
    if opponent_problems:
        warnings.extend(opponent_problems)
        capabilities["opponent_tracking"]["status"] = "BLOCKED"
        capabilities["opponent_tracking"]["reasons"].extend(opponent_problems)
    if context["sim_source_mode"] == "REPLAY_FILE":
        warnings.append("REPLAY_FIELDS_ARE_PROBE_ONLY_NOT_GROUND_TRUTH")
    if not source_advancing:
        warnings.append("SDK_SOURCE_STALE_OR_TOO_FEW_DISTINCT_TICKS")
    if not ended_connected:
        warnings.append("DISCONNECTED_DURING_PROBE")
    if stale_at_end:
        warnings.append("SOURCE_STALE_AT_END")
    if source_gap_during_probe:
        warnings.append("SOURCE_GAP_DURING_PROBE")
    if not continuity_checked or not timestamps_monotonic:
        warnings.append("SOURCE_CONTINUITY_UNVERIFIABLE")
    if len(stable_ticks) >= 2 and not ticks_strictly_advance:
        warnings.append("BUFFER_TICK_DID_NOT_STRICTLY_ADVANCE")
    if not schema_count_matches:
        warnings.append("SCHEMA_VARIABLE_COUNT_MISMATCH")

    schema_payload: dict[str, Any] = {
        "schema_sha256": schema_sha256(descriptors),
        "variable_count": len(descriptors),
    }
    if include_full_schema:
        schema_payload["variables"] = _canonical_schema(descriptors)
    connection_payload = asdict(connection) | {
        "ended_connected": ended_connected,
        "first_buffer_tick": all_ticks[0] if all_ticks else None,
        "last_buffer_tick": all_ticks[-1] if all_ticks else None,
        "distinct_tick_count": len(set(all_ticks)),
        "stable_context_tick_count": len(set(context_ticks)),
        "stable_session_epoch_tick_count": len(set(stable_ticks)),
        "session_epoch_start_buffer_tick": stable_ticks[0] if stable_ticks else None,
        "first_tick_delay_s": first_tick_delay_s,
        "max_inter_tick_gap_s": max_inter_tick_gap_s,
        "seconds_since_last_tick": seconds_since_last_tick,
        "continuity_checked": continuity_checked,
        "sample_duration_s": sample_duration_s,
        "stale": not source_advancing,
    }
    report: dict[str, Any] = {
        "contract_version": SDK_PROBE_CONTRACT_VERSION,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "platform": platform_name,
        "transport": "live_shared_memory",
        "connection": connection_payload,
        "context": context,
        "schema": schema_payload,
        "field_availability": availability,
        "capabilities": capabilities,
        "probe_warnings": sorted(set(warnings)),
    }
    semantic = {
        key: value
        for key, value in report.items()
        if key not in {"created_at_utc", "platform"}
    }
    report["report_digest"] = hashlib.sha256(
        json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return report


class WindowsPyirsdkTransport:
    """Read-only pyirsdk transport with fail-closed shared-memory validation."""

    def __init__(self) -> None:
        if platform.system() != "Windows":
            raise SdkProbeUnavailable("live SDK probing requires Windows")
        try:
            import irsdk
        except ModuleNotFoundError as exc:
            raise SdkProbeUnavailable("pyirsdk 1.3.6 is not installed") from exc
        if irsdk.VERSION != SUPPORTED_PYIRSDK_VERSION:
            raise SdkProbeUnavailable(
                f"unsupported pyirsdk version {irsdk.VERSION}; "
                f"expected {SUPPORTED_PYIRSDK_VERSION}"
            )
        self._irsdk = irsdk
        self._client = irsdk.IRSDK(parse_yaml_async=False)
        self._startup_layout: _SdkLayout | None = None
        self._session_info_snapshot_update: int | None = None
        self._session_info_snapshot_payload: Mapping[str, object] | None = None

    def _close_client(self, client: Any, *, best_effort: bool = True) -> None:
        event_handle = getattr(client, "_data_valid_event", None)
        shared_memory = getattr(client, "_shared_mem", None)
        close_errors: list[Exception] = []
        try:
            client.unfreeze_var_buffer_latest()
        except Exception as exc:
            close_errors.append(exc)
        try:
            client.shutdown()
        except Exception as exc:
            close_errors.append(exc)
        if shared_memory is not None and getattr(client, "_shared_mem", None) is shared_memory:
            try:
                shared_memory.close()
            except Exception as exc:
                close_errors.append(exc)
            else:
                client._shared_mem = None
        if event_handle:
            try:
                closed = self._irsdk.ctypes.windll.kernel32.CloseHandle(event_handle)
                if closed is False:
                    raise OSError("CloseHandle returned false")
            except Exception as exc:
                close_errors.append(exc)
        if close_errors and not best_effort:
            raise SdkProbeConsistencyError("failed to close iRacing SDK resources") from (
                close_errors[0]
            )

    def _attach_once(self) -> tuple[Any, _SdkLayout]:
        kernel32 = self._irsdk.ctypes.windll.kernel32
        ctypes_module = self._irsdk.ctypes
        open_mapping = kernel32.OpenFileMappingW
        open_mapping.argtypes = [
            ctypes_module.c_uint32,
            ctypes_module.c_bool,
            ctypes_module.c_wchar_p,
        ]
        open_mapping.restype = ctypes_module.c_void_p
        open_event = kernel32.OpenEventW
        open_event.argtypes = [
            ctypes_module.c_uint32,
            ctypes_module.c_bool,
            ctypes_module.c_wchar_p,
        ]
        open_event.restype = ctypes_module.c_void_p
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [ctypes_module.c_void_p, ctypes_module.c_uint32]
        wait_for_single_object.restype = ctypes_module.c_uint32
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes_module.c_void_p]
        close_handle.restype = ctypes_module.c_bool

        mapping_handle = open_mapping(0x0004, False, self._irsdk.MEMMAPFILE)
        if not mapping_handle:
            raise SdkProbeUnavailable("iRacing SDK mapping is not ready")
        event_handle: Any = None
        shared_memory: Any = None
        client = self._irsdk.IRSDK(parse_yaml_async=False)
        try:
            event_handle = open_event(
                0x00100000, False, self._irsdk.DATAVALIDEVENTNAME
            )
            if not event_handle:
                raise SdkProbeUnavailable("iRacing SDK data event is not ready")
            if wait_for_single_object(event_handle, 32) != 0:
                raise SdkProbeUnavailable("iRacing SDK has no valid frame yet")
            try:
                shared_memory = self._irsdk.mmap.mmap(
                    0,
                    self._irsdk.MEMMAPFILESIZE,
                    self._irsdk.MEMMAPFILE,
                    access=self._irsdk.mmap.ACCESS_READ,
                )
            except OSError as exc:
                raise SdkProbeUnavailable("iRacing SDK mapping is not readable") from exc

            layout = _stable_sdk_layout(shared_memory)
            if layout.status != self._irsdk.StatusField.status_connected:
                raise SdkProbeUnavailable("iRacing SDK is not connected yet")
            header = self._irsdk.Header(shared_memory)
            after_header = _stable_sdk_layout(shared_memory)
            if (
                after_header.static_signature != layout.static_signature
                or after_header.schema_bytes != layout.schema_bytes
            ):
                raise SdkProbeConsistencyError("SDK layout changed while attaching")

            client._shared_mem = shared_memory
            client._header = header
            client._data_valid_event = event_handle
            client.is_initialized = True
            shared_memory = None
            event_handle = None
            return client, after_header
        finally:
            with suppress(Exception):
                close_handle(mapping_handle)
            if shared_memory is not None:
                with suppress(OSError):
                    shared_memory.close()
            if event_handle:
                with suppress(OSError):
                    close_handle(event_handle)

    def startup(self, timeout_s: float) -> ConnectionMeta:
        if not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError("timeout_s must be finite and non-negative")
        deadline = time.monotonic() + timeout_s
        first_attempt = True
        last_layout_error: SdkProbeConsistencyError | None = None
        while first_attempt or time.monotonic() < deadline:
            first_attempt = False
            remaining = max(0.0, deadline - time.monotonic())
            request_timeout = max(0.05, min(0.5, remaining or 0.05))
            try:
                with urlrequest.urlopen(
                    self._irsdk.SIM_STATUS_URL, timeout=request_timeout
                ) as response:
                    sim_running = "running:1" in response.read().decode("utf-8")
            except (OSError, UnicodeError, urlerror.URLError):
                sim_running = False
            if sim_running:
                candidate: Any = None
                try:
                    candidate, layout = self._attach_once()
                    self._close_client(self._client)
                    self._client = candidate
                    self._startup_layout = layout
                    self._session_info_snapshot_update = None
                    self._session_info_snapshot_payload = None
                    header = candidate._header
                    return ConnectionMeta(
                        startup_ok=True,
                        initialized=True,
                        connected=True,
                        header_version=header.version,
                        raw_header_status=header.status,
                        tick_rate_hz=header.tick_rate,
                        variable_count=header.num_vars,
                        buffer_count=header.num_buf,
                        buffer_len=header.buf_len,
                    )
                except SdkProbeConsistencyError as exc:
                    last_layout_error = exc
                    if candidate is not None:
                        self._close_client(candidate)
                except SdkProbeUnavailable:
                    if candidate is not None:
                        self._close_client(candidate)
            if time.monotonic() >= deadline:
                if last_layout_error is not None:
                    raise last_layout_error
                raise SdkProbeUnavailable("iRacing simulator SDK connection timed out")
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        if last_layout_error is not None:
            raise last_layout_error
        raise SdkProbeUnavailable("iRacing simulator SDK connection timed out")

    def _assert_schema_current(self) -> _SdkLayout:
        if self._startup_layout is None or self._client._shared_mem is None:
            raise SdkProbeConsistencyError("SDK transport is not initialized")
        current = _stable_sdk_layout(self._client._shared_mem)
        if (
            current.static_signature != self._startup_layout.static_signature
            or current.schema_bytes != self._startup_layout.schema_bytes
        ):
            raise SdkProbeConsistencyError("SDK schema changed during probe")
        return current

    @staticmethod
    def _decode_descriptor_string(raw: bytes) -> str:
        return raw.rstrip(b"\x00").decode("latin-1")

    def _descriptor_from_bytes(self, raw: bytes) -> VariableDescriptor:
        type_code, offset, count = struct.unpack_from("<3i", raw, 0)
        count_as_time = struct.unpack_from("<?", raw, 12)[0]
        return VariableDescriptor(
            name=self._decode_descriptor_string(raw[16:48]),
            type_code=type_code,
            dtype=SDK_TYPE_NAMES.get(type_code, f"unknown_{type_code}"),
            offset=offset,
            count=count,
            count_as_time=count_as_time,
            description=self._decode_descriptor_string(raw[48:112]),
            unit=self._decode_descriptor_string(raw[112:144]),
        )

    def descriptors(self) -> tuple[VariableDescriptor, ...]:
        layout = self._assert_schema_current()
        descriptors = tuple(
            self._descriptor_from_bytes(
                layout.schema_bytes[
                    index * SDK_VARIABLE_HEADER_SIZE : (index + 1)
                    * SDK_VARIABLE_HEADER_SIZE
                ]
            )
            for index in range(layout.num_vars)
        )
        _ = self._client._var_headers_dict
        self._assert_schema_current()
        if len({item.name for item in descriptors}) != len(descriptors):
            raise SdkProbeConsistencyError("duplicate SDK variable names")
        if any(
            not item.name
            or item.type_code not in SDK_TYPE_NAMES
            or not 1 <= item.count <= 4096
            or item.offset < 0
            or item.offset + SDK_TYPE_SIZES[item.type_code] * item.count
            > layout.buf_len
            for item in descriptors
        ):
            raise SdkProbeConsistencyError("invalid SDK variable descriptor")
        return descriptors

    def sim_mode(self) -> tuple[Any, int | None]:
        update_before = self._client.session_info_update
        binary_before = self._client._get_session_info_binary("WeekendInfo")
        weekend = self._client["WeekendInfo"]
        binary_after = self._client._get_session_info_binary("WeekendInfo")
        update_after = self._client.session_info_update
        if (
            update_before != update_after
            or not binary_before
            or binary_before != binary_after
            or not isinstance(weekend, dict)
        ):
            return None, None
        return weekend.get("SimMode"), update_after

    @staticmethod
    def _session_info_layout_identity(layout: _SdkLayout) -> tuple[Any, ...]:
        """Fields that must remain unchanged around a full SessionInfo copy."""

        return (
            layout.static_signature,
            layout.schema_bytes,
            layout.session_info_update,
            layout.session_info_len,
            layout.session_info_offset,
        )

    def _parse_session_info_snapshot(
        self, raw_session_info: bytes
    ) -> Mapping[str, object] | None:
        """Parse one stable full SessionInfo buffer using pyirsdk's safe rules."""

        utf8_signature = b"---\nWeekendInfo:\n Encoding: UTF8"
        is_utf8 = raw_session_info.startswith(utf8_signature)
        translated = raw_session_info.translate(
            None if is_utf8 else self._irsdk.YAML_TRANSLATER
        )
        yaml_source = re.sub(
            self._irsdk.YamlReader.NON_PRINTABLE,
            "",
            translated.rstrip(b"\x00").decode("utf-8" if is_utf8 else "cp1252"),
        )

        def escape_double_quoted_string(match: re.Match[str]) -> str:
            value = re.sub(r'(["\\])', r"\\\1", match.group("value"))
            return f'{match.group("key")}"{value}"'

        if is_utf8:
            yaml_source = re.sub(
                r'(?P<key>^\s*\w+: )"(?P<value>.*)"$',
                escape_double_quoted_string,
                yaml_source,
                flags=re.MULTILINE,
            )
            driver_fields = r"DriverSetupName"
        else:
            driver_fields = r"DriverSetupName|UserName|TeamName|AbbrevName|Initials"
        yaml_source = re.sub(
            rf"(?P<key>(?:{driver_fields}): )(?P<value>.+)",
            escape_double_quoted_string,
            yaml_source,
        )
        yaml_source = re.sub(
            r"(?P<key>\w+: )(?P<value>,.*)",
            escape_double_quoted_string,
            yaml_source,
        )

        parsed = self._irsdk.yaml.load(
            yaml_source,
            Loader=self._irsdk.CustomYamlSafeLoader,
        )
        return parsed if isinstance(parsed, Mapping) else None

    def session_info_snapshot(
        self,
    ) -> tuple[Mapping[str, object] | None, int | None]:
        """Return a stable full SessionInfo mapping, or no snapshot on uncertainty.

        The shared-memory writer can update both the header and YAML while it is
        being copied.  A snapshot is accepted only when three validated layouts
        surround two byte-identical full-buffer reads.  No SessionInfo content is
        emitted here; the collector owns privacy filtering before persistence.
        """

        current_update = getattr(self._client, "session_info_update", None)
        cached_update = getattr(self, "_session_info_snapshot_update", None)
        cached_payload = getattr(self, "_session_info_snapshot_payload", None)
        if (
            isinstance(current_update, int)
            and not isinstance(current_update, bool)
            and current_update == cached_update
            and cached_payload is not None
        ):
            return cached_payload, cached_update

        for _ in range(SDK_SESSION_INFO_SNAPSHOT_ATTEMPTS):
            try:
                before = self._assert_schema_current()
                start = before.session_info_offset
                end = start + before.session_info_len
                raw_before = bytes(self._client._shared_mem[start:end])

                between = self._assert_schema_current()
                second_start = between.session_info_offset
                second_end = second_start + between.session_info_len
                raw_after = bytes(self._client._shared_mem[second_start:second_end])
                after = self._assert_schema_current()
            except (
                BufferError,
                IndexError,
                OSError,
                SdkProbeConsistencyError,
                SdkProbeUnavailable,
                TypeError,
                ValueError,
                struct.error,
            ):
                continue

            identities = (
                self._session_info_layout_identity(before),
                self._session_info_layout_identity(between),
                self._session_info_layout_identity(after),
            )
            if identities[0] != identities[1] or identities[1] != identities[2]:
                continue
            if (
                len(raw_before) != after.session_info_len
                or len(raw_after) != after.session_info_len
                or raw_before != raw_after
            ):
                continue
            try:
                parsed = self._parse_session_info_snapshot(raw_after)
            except Exception:
                return None, None
            if parsed is None:
                return None, None
            self._session_info_snapshot_update = after.session_info_update
            self._session_info_snapshot_payload = parsed
            return parsed, after.session_info_update
        return None, None

    def _freeze_stable_latest(self) -> tuple[Any, int]:
        self._client.unfreeze_var_buffer_latest()
        for _ in range(3):
            self._client._wait_valid_data_event()
            candidates = sorted(
                self._client._header.var_buf,
                key=lambda item: item.tick_count,
                reverse=True,
            )
            for candidate in candidates:
                begin_before = candidate.tick_count_begin
                tick_before = candidate.tick_count
                offset_before = candidate._buf_offset
                if begin_before != tick_before:
                    continue
                candidate.freeze()
                tick_after = candidate.tick_count
                begin_after = candidate.tick_count_begin
                offset_after = candidate._buf_offset
                stable = (
                    begin_before
                    == tick_before
                    == tick_after
                    == begin_after
                    and offset_before == offset_after
                    and offset_before >= 0
                    and offset_before + self._client._header.buf_len
                    <= len(self._client._shared_mem)
                )
                if stable:
                    self._client._IRSDK__var_buffer_latest = candidate
                    return candidate, tick_before
                candidate.unfreeze()
        raise SdkProbeConsistencyError("could not copy a stable SDK buffer")

    def read_frozen(self, fields: tuple[str, ...]) -> RawSdkFrame:
        try:
            session_info_update_before = self._client.session_info_update
            _, buffer_tick = self._freeze_stable_latest()
            self._assert_schema_current()
            values: dict[str, Any] = {}
            errors: list[str] = []
            available = set(self._client.var_headers_names)
            for field in fields:
                if field not in available:
                    continue
                try:
                    values[field] = self._client[field]
                except (IndexError, KeyError, RuntimeError, struct.error, TypeError, ValueError):
                    errors.append(field)
            self._assert_schema_current()
            session_info_update_after = self._client.session_info_update
            if session_info_update_before != session_info_update_after:
                raise SdkProbeConsistencyError(
                    "SessionInfo changed during frozen telemetry read"
                )
            return RawSdkFrame(
                buffer_tick=buffer_tick,
                session_info_update=session_info_update_after,
                values=values,
                read_errors=tuple(errors),
                captured_monotonic_s=time.monotonic(),
            )
        finally:
            self._client.unfreeze_var_buffer_latest()

    @property
    def connected(self) -> bool:
        header = getattr(self._client, "_header", None)
        return bool(
            header is not None
            and getattr(self._client, "_data_valid_event", None)
            and header.status == self._irsdk.StatusField.status_connected
        )

    def close(self) -> None:
        try:
            self._close_client(self._client, best_effort=False)
        finally:
            self._startup_layout = None
            self._session_info_snapshot_update = None
            self._session_info_snapshot_payload = None


def probe_live_sdk(
    *,
    wait_seconds: float = 20.0,
    sample_seconds: float = 3.0,
    poll_seconds: float = 0.05,
    include_full_schema: bool = False,
) -> dict[str, Any]:
    if (
        not all(math.isfinite(value) for value in (wait_seconds, sample_seconds, poll_seconds))
        or wait_seconds < 0
        or sample_seconds <= 0
        or poll_seconds <= 0
    ):
        raise ValueError("probe durations must be positive (wait may be zero)")
    transport: WindowsPyirsdkTransport | None = None
    try:
        transport = WindowsPyirsdkTransport()
        connection = transport.startup(wait_seconds)
        descriptors = transport.descriptors()
        initial_sim_mode, _ = transport.sim_mode()
        fields = tuple(item.name for item in descriptors if item.name in TARGET_FIELDS)
        frames: list[RawSdkFrame] = []
        seen_ticks: set[int] = set()
        sample_started = time.monotonic()
        deadline = sample_started + sample_seconds
        while time.monotonic() < deadline and transport.connected:
            frame = transport.read_frozen(fields)
            frame_sim_mode, frame_sim_mode_update = transport.sim_mode()
            frame = _bind_frame_sim_mode(
                frame, frame_sim_mode, frame_sim_mode_update
            )
            if frame.buffer_tick not in seen_ticks:
                frames.append(frame)
                seen_ticks.add(frame.buffer_tick)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(poll_seconds, remaining))
        probe_end = time.monotonic()
        ended_connected = transport.connected
        elapsed = probe_end - sample_started
        return build_probe_report(
            connection=connection,
            descriptors=descriptors,
            frames=tuple(frames),
            sim_mode_raw=initial_sim_mode,
            sample_duration_s=round(elapsed, 6),
            platform_name=platform.platform(),
            include_full_schema=include_full_schema,
            ended_connected=ended_connected,
            probe_start_monotonic_s=sample_started,
            probe_end_monotonic_s=probe_end,
            stale_after_s=max(0.5, poll_seconds * 4),
        )
    except (SdkProbeConsistencyError, SdkProbeUnavailable):
        raise
    except Exception as exc:
        raise SdkProbeConsistencyError(f"SDK probe failed: {type(exc).__name__}") from exc
    finally:
        if transport is not None:
            with suppress(Exception):
                transport.close()
