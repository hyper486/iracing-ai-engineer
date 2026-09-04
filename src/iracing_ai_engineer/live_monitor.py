"""Privacy-safe live state bridge for overlays and future speech consumers.

The monitor reads only the public iRacing SDK through the existing read-only
transport.  It normalizes every distinct SDK tick so quality and transition
state are not inferred from a low-frequency display sample, then emits a
bounded status projection at a slower cadence.  It never renders speech,
persists raw telemetry, or exposes a vehicle or pit-control path.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .collector import ReadOnlySdkTransport, validate_variable_descriptors
from .events import TelemetryEvent, TelemetryEventPipeline, TelemetryEventReceipt
from .sdk_probe import (
    OPPONENT_ARRAY_FIELDS,
    TARGET_FIELDS,
    RawSdkFrame,
    _bind_frame_sim_mode,
    classify_context,
)
from .telemetry import (
    Presence,
    QualityStatus,
    SourceKind,
    TelemetryField,
    TelemetrySample,
    normalize_sdk_frame,
)

LIVE_MONITOR_CONTRACT_VERSION = "live-monitor-v1"

LIVE_MONITOR_FIELDS = tuple(
    dict.fromkeys(
        TARGET_FIELDS
        + (
            "LapCompleted",
            "Clutch",
            "PitstopActive",
            "PlayerCarMyIncidentCount",
            "PlayerCarDriverIncidentCount",
            "PlayerCarTeamIncidentCount",
        )
    )
)

_CORE_FIELDS = frozenset(("SessionNum", "SessionTick", "SessionTime"))


class LiveMonitorError(ValueError):
    """Fail-closed live-monitor contract or runtime error."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(code if message is None else f"{code}: {message}")


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LiveMonitorError("NONFINITE_VALUE")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported live-monitor value: {type(value).__name__}")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _field_value[T](field: TelemetryField[T]) -> T | None:
    return field.value if field.presence is Presence.PRESENT else None


def _source_kind_from_mode(raw_mode: object) -> tuple[str, SourceKind]:
    if not isinstance(raw_mode, str) or raw_mode != raw_mode.strip():
        raise LiveMonitorError("SIM_MODE_UNSTABLE", "SimMode must be stable full or replay")
    mode = raw_mode.casefold()
    if mode == "full":
        return mode, SourceKind.SDK_LIVE
    if mode == "replay":
        return mode, SourceKind.REPLAY_SDK_PROXY
    raise LiveMonitorError("SIM_MODE_UNSUPPORTED", "SimMode must be full or replay")


def _run_identifier(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise LiveMonitorError(f"{label.upper()}_INVALID")
    return value


def _frame_digest(frame: RawSdkFrame) -> str:
    return _sha256(
        {
            "read_errors": list(frame.read_errors),
            "session_info_update": frame.session_info_update,
            "sim_mode_raw": frame.sim_mode_raw,
            "values": frame.values,
        }
    )


def _event_projection(event: TelemetryEvent) -> dict[str, object]:
    payload = event.to_dict()
    return {
        "details": payload["details"],
        "event_sequence": event.sequence,
        "kind": event.kind.value,
        "session_time_us": event.session_time_us,
    }


@dataclass(frozen=True, slots=True)
class LiveMonitorReceipt:
    binding_sha256: str
    event_receipt: TelemetryEventReceipt
    final_status: str
    frame_count: int
    duplicate_frame_count: int
    dropped_tick_count: int
    in_car_snapshot_count: int
    sdk_tick_rate_hz: int
    snapshot_count: int
    snapshots_sha256: str
    source_kind_counts: tuple[tuple[str, int], ...]
    status_counts: tuple[tuple[str, int], ...]
    receipt_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "advisor_only": True,
            "binding_sha256": self.binding_sha256,
            "contract_version": LIVE_MONITOR_CONTRACT_VERSION,
            "dropped_tick_count": self.dropped_tick_count,
            "duplicate_frame_count": self.duplicate_frame_count,
            "event_receipt": self.event_receipt.to_dict(),
            "executable": False,
            "final_status": self.final_status,
            "frame_count": self.frame_count,
            "in_car_snapshot_count": self.in_car_snapshot_count,
            "receipt_sha256": self.receipt_sha256,
            "sdk_tick_rate_hz": self.sdk_tick_rate_hz,
            "snapshot_count": self.snapshot_count,
            "snapshots_sha256": self.snapshots_sha256,
            "source_kind_counts": dict(self.source_kind_counts),
            "status_counts": dict(self.status_counts),
        }


class LiveMonitor:
    """Normalize every distinct frame and emit bounded display snapshots."""

    def __init__(
        self,
        *,
        source_id: str,
        session_id: str,
        sdk_tick_rate_hz: int,
        expected_source_kind: SourceKind | None = None,
        stale_after_s: float = 0.5,
        expected_car_count: int | None = None,
    ) -> None:
        source_id = _run_identifier(source_id, "source_id")
        session_id = _run_identifier(session_id, "session_id")
        if (
            isinstance(sdk_tick_rate_hz, bool)
            or not isinstance(sdk_tick_rate_hz, int)
            or sdk_tick_rate_hz <= 0
        ):
            raise LiveMonitorError("TICK_RATE_INVALID")
        if expected_source_kind is not None and expected_source_kind not in {
            SourceKind.SDK_LIVE,
            SourceKind.REPLAY_SDK_PROXY,
        }:
            raise LiveMonitorError("EXPECTED_SOURCE_KIND_INVALID")
        if (
            isinstance(stale_after_s, bool)
            or not isinstance(stale_after_s, (int, float))
            or not math.isfinite(float(stale_after_s))
            or stale_after_s <= 0
        ):
            raise LiveMonitorError("STALE_THRESHOLD_INVALID")
        if expected_car_count is not None and (
            isinstance(expected_car_count, bool)
            or not isinstance(expected_car_count, int)
            or expected_car_count <= 0
        ):
            raise LiveMonitorError("CAR_COUNT_INVALID")

        self._source_id = source_id
        self._session_id = session_id
        self._sdk_tick_rate_hz = sdk_tick_rate_hz
        self._expected_source_kind = expected_source_kind
        self._stale_after_s = float(stale_after_s)
        self._expected_car_count = expected_car_count
        self._binding_sha256 = _sha256(
            {"session_id": session_id, "source_id": source_id}
        )
        self._events = TelemetryEventPipeline()
        self._pending_events: list[TelemetryEvent] = []
        self._latest_sample: TelemetrySample | None = None
        self._latest_context: dict[str, Any] | None = None
        self._latest_read_errors: tuple[str, ...] = ()
        self._latest_source_kind: SourceKind | None = None
        self._latest_buffer_tick: int | None = None
        self._latest_frame_digest: str | None = None
        self._last_snapshot_buffer_tick: int | None = None
        self._snapshot_hasher = hashlib.sha256()
        self._snapshot_count = 0
        self._frame_count = 0
        self._duplicate_frame_count = 0
        self._dropped_tick_count = 0
        self._in_car_snapshot_count = 0
        self._status_counts: Counter[str] = Counter()
        self._source_kind_counts: Counter[str] = Counter()
        self._final_status: str | None = None
        self._receipt: LiveMonitorReceipt | None = None

    @property
    def latest_buffer_tick(self) -> int | None:
        return self._latest_buffer_tick

    @property
    def last_snapshot_buffer_tick(self) -> int | None:
        return self._last_snapshot_buffer_tick

    def feed(self, frame: RawSdkFrame) -> bool:
        """Consume one frozen frame; return false for a same-tick duplicate."""

        if self._receipt is not None:
            raise RuntimeError("live monitor is already finished")
        if not isinstance(frame, RawSdkFrame):
            raise TypeError("frame must be a RawSdkFrame")
        frame_digest = _frame_digest(frame)
        if frame.buffer_tick == self._latest_buffer_tick:
            if frame_digest != self._latest_frame_digest:
                raise LiveMonitorError(
                    "DUPLICATE_CONFLICT",
                    "same SDK buffer tick carried different telemetry",
                )
            self._duplicate_frame_count += 1
            return False

        _, source_kind = _source_kind_from_mode(frame.sim_mode_raw)
        if (
            self._expected_source_kind is not None
            and source_kind is not self._expected_source_kind
        ):
            raise LiveMonitorError(
                "SOURCE_KIND_MISMATCH",
                f"expected {self._expected_source_kind.value}, observed {source_kind.value}",
            )

        sample = normalize_sdk_frame(
            frame.values,
            source_id=self._source_id,
            session_id=self._session_id,
            source_kind=source_kind,
            buffer_tick=frame.buffer_tick,
            captured_monotonic_s=frame.captured_monotonic_s,
            previous=self._latest_sample,
            stale_after_s=self._stale_after_s,
            expected_car_count=self._expected_car_count,
        )
        emitted = self._events.feed(sample)
        self._pending_events.extend(emitted)
        self._latest_sample = sample
        self._latest_context = classify_context(frame.sim_mode_raw, frame.values)
        self._latest_read_errors = tuple(sorted(set(frame.read_errors)))
        self._latest_source_kind = source_kind
        self._latest_buffer_tick = frame.buffer_tick
        self._latest_frame_digest = frame_digest
        self._frame_count += 1
        self._source_kind_counts[source_kind.value] += 1
        dropped = _field_value(sample.quality.dropped_ticks)
        if isinstance(dropped, int) and not isinstance(dropped, bool):
            self._dropped_tick_count += dropped
        return True

    def _status(self) -> tuple[str, tuple[str, ...]]:
        if self._latest_sample is None or self._latest_context is None:
            raise LiveMonitorError("NO_FRAME")
        sample = self._latest_sample
        context = self._latest_context
        quality = _field_value(sample.quality.status)
        issues = _field_value(sample.quality.issues) or ()
        conflicts = tuple(str(item) for item in context["conflicts"])
        core_read_errors = tuple(
            f"READ_ERROR:{name}"
            for name in self._latest_read_errors
            if name in _CORE_FIELDS
        )
        other_read_errors = tuple(
            f"READ_ERROR:{name}"
            for name in self._latest_read_errors
            if name not in _CORE_FIELDS
        )
        player_state = str(context["player_control_state"])
        sim_source_mode = str(context["sim_source_mode"])
        reasons: list[str] = [*conflicts, *core_read_errors]

        if (
            quality is QualityStatus.REJECTED
            or conflicts
            or core_read_errors
            or sim_source_mode == "UNKNOWN"
        ):
            status = "BLOCKED"
        elif player_state != "IN_CAR_PHYSICS":
            status = "WAIT_CAR"
            reasons.extend((sim_source_mode, player_state))
        elif quality is QualityStatus.READY and not other_read_errors:
            status = "READY"
        else:
            status = "DEGRADED"

        reasons.extend(str(item) for item in issues)
        reasons.extend(other_read_errors)
        return status, tuple(dict.fromkeys(reasons))

    def snapshot(self) -> dict[str, object]:
        """Return one self-hashed privacy-safe projection of the latest state."""

        if self._receipt is not None:
            raise RuntimeError("live monitor is already finished")
        if (
            self._latest_sample is None
            or self._latest_context is None
            or self._latest_source_kind is None
            or self._latest_buffer_tick is None
        ):
            raise LiveMonitorError("NO_FRAME")
        if self._last_snapshot_buffer_tick == self._latest_buffer_tick:
            raise LiveMonitorError("NO_NEW_FRAME")

        sample = self._latest_sample
        context = self._latest_context
        status, reasons = self._status()
        session_time_s = _field_value(sample.session.session_time_s)
        quality_status = _field_value(sample.quality.status)
        issues = _field_value(sample.quality.issues) or ()
        opponents = sample.opponents
        in_car = context["player_control_state"] == "IN_CAR_PHYSICS"
        if in_car:
            self._in_car_snapshot_count += 1
        self._status_counts[status] += 1

        base: dict[str, object] = {
            "advisor_only": True,
            "binding_sha256": self._binding_sha256,
            "context": {
                "confidence": context["confidence"],
                "conflicts": list(context["conflicts"]),
                "evidence": list(context["evidence"]),
                "player_control_state": context["player_control_state"],
                "sim_source_mode": context["sim_source_mode"],
            },
            "contract_version": LIVE_MONITOR_CONTRACT_VERSION,
            "events": [_event_projection(event) for event in self._pending_events],
            "executable": False,
            "opponents": {
                "array_status": opponents.presence.value,
                "slot_count": len(opponents.entries),
            },
            "quality": {
                "dropped_ticks": _field_value(sample.quality.dropped_ticks),
                "issues": list(issues),
                "stale": _field_value(sample.quality.stale),
                "status": quality_status.value if quality_status is not None else None,
            },
            "reasons": list(reasons),
            "record_type": "live_monitor_snapshot",
            "sequence": self._snapshot_count,
            "session_time_us": (
                round(session_time_s * 1_000_000)
                if isinstance(session_time_s, (int, float))
                else None
            ),
            "source_kind": self._latest_source_kind.value,
            "status": status,
            "telemetry": {
                "air_temp_c": _field_value(sample.environment.air_temp_c),
                "brake": _field_value(sample.controls.brake),
                "fuel_level_l": _field_value(sample.fuel.level_l),
                "fuel_level_pct": _field_value(sample.fuel.level_pct),
                "gear": _field_value(sample.controls.gear),
                "in_pit_stall": _field_value(sample.pit.in_pit_stall),
                "is_on_track": _field_value(sample.flags.is_on_track),
                "is_on_track_car": _field_value(sample.flags.is_on_track_car),
                "lap_distance_pct": _field_value(sample.lap.lap_distance_pct),
                "lap_number": _field_value(sample.lap.lap_number),
                "laps_completed": _field_value(sample.lap.laps_completed),
                "on_pit_road": _field_value(sample.pit.on_pit_road),
                "pits_open": _field_value(sample.pit.pits_open),
                "rpm": _field_value(sample.controls.rpm),
                "session_flags": _field_value(sample.flags.session_flags),
                "session_laps_remaining": _field_value(
                    sample.session.session_laps_remaining
                ),
                "session_time_remaining_s": _field_value(
                    sample.session.session_time_remaining_s
                ),
                "speed_mps": _field_value(sample.lap.speed_mps),
                "steering_angle_rad": _field_value(
                    sample.controls.steering_angle_rad
                ),
                "throttle": _field_value(sample.controls.throttle),
                "tire_compound": _field_value(sample.tires.player_tire_compound),
                "tire_sets_used": _field_value(sample.tires.tire_sets_used),
                "track_temp_c": _field_value(sample.environment.track_temp_c),
            },
        }
        snapshot = {**base, "snapshot_sha256": _sha256(base)}
        self._snapshot_hasher.update(_canonical_json(snapshot) + b"\n")
        self._snapshot_count += 1
        self._last_snapshot_buffer_tick = self._latest_buffer_tick
        self._pending_events.clear()
        self._final_status = status
        return snapshot

    def finish(self) -> LiveMonitorReceipt:
        """Freeze the monitor and return a self-hashed terminal receipt."""

        if self._receipt is not None:
            return self._receipt
        if self._snapshot_count == 0 or self._final_status is None:
            raise LiveMonitorError("NO_SNAPSHOT")
        if self._last_snapshot_buffer_tick != self._latest_buffer_tick:
            raise LiveMonitorError("FINAL_SNAPSHOT_REQUIRED")
        event_receipt = self._events.finish()
        base: dict[str, object] = {
            "advisor_only": True,
            "binding_sha256": self._binding_sha256,
            "contract_version": LIVE_MONITOR_CONTRACT_VERSION,
            "dropped_tick_count": self._dropped_tick_count,
            "duplicate_frame_count": self._duplicate_frame_count,
            "event_receipt": event_receipt.to_dict(),
            "executable": False,
            "final_status": self._final_status,
            "frame_count": self._frame_count,
            "in_car_snapshot_count": self._in_car_snapshot_count,
            "sdk_tick_rate_hz": self._sdk_tick_rate_hz,
            "snapshot_count": self._snapshot_count,
            "snapshots_sha256": self._snapshot_hasher.hexdigest(),
            "source_kind_counts": dict(sorted(self._source_kind_counts.items())),
            "status_counts": dict(sorted(self._status_counts.items())),
        }
        self._receipt = LiveMonitorReceipt(
            binding_sha256=self._binding_sha256,
            event_receipt=event_receipt,
            final_status=self._final_status,
            frame_count=self._frame_count,
            duplicate_frame_count=self._duplicate_frame_count,
            dropped_tick_count=self._dropped_tick_count,
            in_car_snapshot_count=self._in_car_snapshot_count,
            sdk_tick_rate_hz=self._sdk_tick_rate_hz,
            snapshot_count=self._snapshot_count,
            snapshots_sha256=self._snapshot_hasher.hexdigest(),
            source_kind_counts=tuple(sorted(self._source_kind_counts.items())),
            status_counts=tuple(sorted(self._status_counts.items())),
            receipt_sha256=_sha256(base),
        )
        return self._receipt


def _expected_car_count(descriptors: tuple[object, ...]) -> int | None:
    counts = {
        descriptor.count
        for descriptor in descriptors
        if getattr(descriptor, "name", None) in OPPONENT_ARRAY_FIELDS
    }
    return next(iter(counts)) if len(counts) == 1 else None


def monitor_live_transport(
    transport: ReadOnlySdkTransport,
    *,
    emit: Callable[[dict[str, object]], None],
    source_id: str,
    session_id: str,
    expected_source_kind: SourceKind | None = None,
    wait_seconds: float = 20.0,
    duration_s: float = 30.0,
    poll_seconds: float = 0.01,
    snapshot_seconds: float = 0.5,
    stale_after_s: float = 0.5,
    max_reads: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> LiveMonitorReceipt:
    """Run a finite read-only monitor session and stream bounded snapshots."""

    if not callable(emit):
        raise TypeError("emit must be callable")
    durations = (wait_seconds, duration_s, poll_seconds, snapshot_seconds, stale_after_s)
    valid_duration_types = all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in durations
    )
    if not valid_duration_types:
        raise LiveMonitorError("DURATION_TYPE_INVALID")
    if not all(math.isfinite(float(value)) for value in durations):
        raise LiveMonitorError("DURATION_NONFINITE")
    if wait_seconds < 0 or any(
        value <= 0
        for value in (duration_s, poll_seconds, snapshot_seconds, stale_after_s)
    ):
        raise LiveMonitorError("DURATION_INVALID")
    if max_reads is not None and (
        isinstance(max_reads, bool) or not isinstance(max_reads, int) or max_reads <= 0
    ):
        raise LiveMonitorError("MAX_READS_INVALID")

    primary_error: BaseException | None = None
    try:
        connection = transport.startup(float(wait_seconds))
        descriptors = tuple(transport.descriptors())
        validate_variable_descriptors(descriptors)
        available = {descriptor.name for descriptor in descriptors}
        missing_core = tuple(sorted(_CORE_FIELDS - available))
        if missing_core:
            raise LiveMonitorError(
                "CORE_SCHEMA_MISSING",
                ",".join(missing_core),
            )
        selected_fields = tuple(
            field for field in LIVE_MONITOR_FIELDS if field in available
        )
        monitor = LiveMonitor(
            source_id=source_id,
            session_id=session_id,
            sdk_tick_rate_hz=connection.tick_rate_hz,
            expected_source_kind=expected_source_kind,
            stale_after_s=stale_after_s,
            expected_car_count=_expected_car_count(descriptors),
        )
        deadline = monotonic() + duration_s
        reads = 0
        next_snapshot_at: float | None = None
        while monotonic() < deadline:
            if not transport.connected:
                raise LiveMonitorError(
                    "SDK_DISCONNECTED",
                    "SDK disconnected before the requested monitor duration completed",
                )
            read_started_at = monotonic()
            frame = transport.read_frozen(selected_fields)
            sim_mode, sim_mode_update = transport.sim_mode()
            frame = _bind_frame_sim_mode(frame, sim_mode, sim_mode_update)
            is_new = monitor.feed(frame)
            reads += 1
            now = monotonic()
            if is_new and (next_snapshot_at is None or now >= next_snapshot_at):
                emit(monitor.snapshot())
                next_snapshot_at = now + snapshot_seconds
            if max_reads is not None and reads >= max_reads:
                break
            remaining = deadline - now
            until_next_poll = poll_seconds - (now - read_started_at)
            if remaining > 0 and until_next_poll > 0:
                sleep(min(until_next_poll, remaining))

        if monitor.latest_buffer_tick is None:
            raise LiveMonitorError("NO_FRAME")
        if monitor.last_snapshot_buffer_tick != monitor.latest_buffer_tick:
            emit(monitor.snapshot())
        return monitor.finish()
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


__all__ = [
    "LIVE_MONITOR_CONTRACT_VERSION",
    "LIVE_MONITOR_FIELDS",
    "LiveMonitor",
    "LiveMonitorError",
    "LiveMonitorReceipt",
    "monitor_live_transport",
]
