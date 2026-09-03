"""Deterministic streaming events derived from normalized telemetry samples.

This module is deliberately source-agnostic: live SDK, IBT, and replay-proxy
samples all enter through :class:`~iracing_ai_engineer.telemetry.TelemetrySample`.
The state machine never substitutes a missing value with a false-y sentinel and
never carries transition state across a source, schema, or session boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any

from .telemetry import (
    Presence,
    QualityStatus,
    SourceKind,
    TelemetryField,
    TelemetrySample,
)

EVENT_CONTRACT_VERSION = "telemetry-events-v1"


class EventKind(StrEnum):
    """Stable event names shared by live and offline consumers."""

    SOURCE_STARTED = "source_started"
    SOURCE_RESET = "source_reset"
    SESSION_STARTED = "session_started"
    SESSION_RESET = "session_reset"
    QUALITY_REJECTED = "quality_rejected"
    SOURCE_STALE = "source_stale"
    SOURCE_RESUMED = "source_resumed"
    DROPPED_TICKS = "dropped_ticks"
    LAP_COMPLETED = "lap_completed"
    LAP_WRAP = "lap_wrap"
    PIT_ROAD_ENTERED = "pit_road_entered"
    PIT_ROAD_EXITED = "pit_road_exited"
    PIT_STALL_ENTERED = "pit_stall_entered"
    PIT_STALL_EXITED = "pit_stall_exited"
    FLAG_CHANGED = "flag_changed"


class EventPipelineError(ValueError):
    """Raised when the caller violates the streaming pipeline contract."""


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("event payload cannot contain NaN or infinity")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported event serialization type: {type(value).__name__}")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _freeze_detail(value: object) -> object:
    """Copy an event detail into a recursively immutable JSON value."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("event detail cannot contain NaN or infinity")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        return tuple(
            (str(key), _freeze_detail(item)) for key, item in sorted(value.items())
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_detail(item) for item in value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"event detail is not JSON-compatible: {type(value).__name__}")


def _thaw_detail(value: object) -> object:
    if isinstance(value, tuple):
        if all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            for item in value
        ):
            return {item[0]: _thaw_detail(item[1]) for item in value}
        return [_thaw_detail(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """One immutable transition at the input sample's record grain."""

    sequence: int
    kind: EventKind
    source_epoch: int
    session_epoch: int | None
    source_id: str | None
    source_kind: SourceKind | None
    session_id: str | None
    session_num: int | None
    session_tick: int | None
    session_time_us: int | None
    details: tuple[tuple[str, object], ...] = ()
    contract_version: str = EVENT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        if not isinstance(self.kind, EventKind):
            raise TypeError("kind must be an EventKind")
        if isinstance(self.source_epoch, bool) or self.source_epoch < 0:
            raise ValueError("source_epoch must be a non-negative integer")
        if self.session_epoch is not None and (
            isinstance(self.session_epoch, bool) or self.session_epoch < 0
        ):
            raise ValueError("session_epoch must be None or a non-negative integer")
        if not isinstance(self.details, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            for item in self.details
        ):
            raise TypeError("details must be an immutable tuple of key/value pairs")
        detail_names = tuple(item[0] for item in self.details)
        if detail_names != tuple(sorted(detail_names)) or len(detail_names) != len(
            set(detail_names)
        ):
            raise ValueError("detail keys must be sorted and unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "details": {key: _thaw_detail(value) for key, value in self.details},
            "kind": self.kind.value,
            "sequence": self.sequence,
            "session_epoch": self.session_epoch,
            "session_id": self.session_id,
            "session_num": self.session_num,
            "session_tick": self.session_tick,
            "session_time_us": self.session_time_us,
            "source_epoch": self.source_epoch,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value if self.source_kind is not None else None,
        }

    def to_json_line(self) -> str:
        return _canonical_json(self.to_dict()).decode("utf-8")

    def to_jsonl(self) -> str:
        return f"{self.to_json_line()}\n"


@dataclass(frozen=True, slots=True)
class TelemetryEventReceipt:
    """Deterministic terminal receipt for one event-pipeline run."""

    events_sha256: str
    receipt_sha256: str
    config_sha256: str
    sample_count: int
    accepted_sample_count: int
    rejected_sample_count: int
    event_count: int
    source_epoch_count: int
    session_epoch_count: int
    event_kind_counts: tuple[tuple[str, int], ...]
    contract_version: str = EVENT_CONTRACT_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted_sample_count": self.accepted_sample_count,
            "config_sha256": self.config_sha256,
            "contract_version": self.contract_version,
            "event_count": self.event_count,
            "event_kind_counts": dict(self.event_kind_counts),
            "events_sha256": self.events_sha256,
            "receipt_sha256": self.receipt_sha256,
            "rejected_sample_count": self.rejected_sample_count,
            "sample_count": self.sample_count,
            "session_epoch_count": self.session_epoch_count,
            "source_epoch_count": self.source_epoch_count,
        }

    def to_json_line(self) -> str:
        return _canonical_json(self.to_dict()).decode("utf-8")


def _present(field: TelemetryField[Any], expected_type: type[Any]) -> Any | None:
    if field.presence is not Presence.PRESENT:
        return None
    value = field.value
    if expected_type is int and isinstance(value, bool):
        return None
    return value if isinstance(value, expected_type) else None


def _present_bool(field: TelemetryField[bool]) -> bool | None:
    return _present(field, bool)


def _present_int(field: TelemetryField[int]) -> int | None:
    return _present(field, int)


def _present_float(field: TelemetryField[float]) -> float | None:
    value = _present(field, float)
    return value if value is not None and math.isfinite(value) else None


def _present_lap_distance(field: TelemetryField[float]) -> float | None:
    value = _present_float(field)
    # iRacing can cross the timing line a few ten-thousandths outside [0, 1].
    # Preserve that real wrap evidence while excluding the -1 out-of-car
    # sentinel and implausibly large excursions.
    return value if value is not None and -0.05 <= value <= 1.05 else None


class TelemetryEventPipeline:
    """Fail-closed streaming state machine for normalized telemetry.

    ``feed`` accepts either one sample or any finite iterable (a chunk).  Event
    sequence numbers and receipts depend only on sample order, not chunk sizes.
    """

    _LAP_WRAP_HIGH = 0.8
    _LAP_WRAP_LOW = 0.2

    def __init__(self) -> None:
        self._events: list[TelemetryEvent] = []
        self._finished = False
        self._receipt: TelemetryEventReceipt | None = None
        self._sample_count = 0
        self._accepted_count = 0
        self._rejected_count = 0
        self._source_count = 0
        self._session_count = 0

        self._source_key: tuple[str, SourceKind, str] | None = None
        self._source_epoch = -1
        self._session_token: tuple[str | None, int] | None = None
        self._session_epoch: int | None = None
        self._session_samples = 0

        self._last_session_tick: int | None = None
        self._last_session_time_s: float | None = None
        self._last_buffer_tick: int | None = None
        self._last_capture_s: float | None = None
        self._last_lap_number: int | None = None
        self._last_laps_completed: int | None = None
        self._last_lap_distance_pct: float | None = None
        self._last_on_pit_road: bool | None = None
        self._last_in_pit_stall: bool | None = None
        self._last_flags: int | None = None
        self._stale_state: bool | None = None

    @property
    def events(self) -> tuple[TelemetryEvent, ...]:
        return tuple(self._events)

    def _event(
        self,
        kind: EventKind,
        sample: TelemetrySample,
        *,
        details: Mapping[str, object] | None = None,
        source_id: str | None = None,
        source_kind: SourceKind | None = None,
        session_identity_override: tuple[str | None, int | None] | None = None,
    ) -> TelemetryEvent:
        if session_identity_override is None:
            session_id = _present(sample.session.session_id, str)
            session_num = _present_int(sample.session.session_num)
        else:
            session_id, session_num = session_identity_override
        event = TelemetryEvent(
            sequence=len(self._events),
            kind=kind,
            source_epoch=max(0, self._source_epoch),
            session_epoch=self._session_epoch,
            source_id=source_id,
            source_kind=source_kind,
            session_id=session_id,
            session_num=session_num,
            session_tick=_present_int(sample.session.session_tick),
            session_time_us=(
                None
                if (session_time := _present_float(sample.session.session_time_s)) is None
                else round(session_time * 1_000_000)
            ),
            details=tuple(
                (key, _freeze_detail(value))
                for key, value in sorted((details or {}).items())
            ),
        )
        self._events.append(event)
        return event

    def _clear_dynamic_state(self) -> None:
        self._last_lap_number = None
        self._last_laps_completed = None
        self._last_lap_distance_pct = None
        self._last_on_pit_road = None
        self._last_in_pit_stall = None
        self._last_flags = None

    def _clear_session_state(self) -> None:
        self._last_session_tick = None
        self._last_session_time_s = None
        self._last_buffer_tick = None
        self._last_capture_s = None
        self._stale_state = None
        self._session_samples = 0
        self._clear_dynamic_state()

    def _prime_dynamic_state(self, sample: TelemetrySample) -> None:
        self._last_lap_number = _present_int(sample.lap.lap_number)
        self._last_laps_completed = _present_int(sample.lap.laps_completed)
        self._last_lap_distance_pct = _present_lap_distance(
            sample.lap.lap_distance_pct
        )
        self._last_on_pit_road = _present_bool(sample.pit.on_pit_road)
        self._last_in_pit_stall = _present_bool(sample.pit.in_pit_stall)
        self._last_flags = _present_int(sample.flags.session_flags)

    def _clock_regressions(self, sample: TelemetrySample) -> tuple[str, ...]:
        checks = (
            (
                "SESSION_TICK_REGRESSION",
                self._last_session_tick,
                _present_int(sample.session.session_tick),
            ),
            (
                "SESSION_TIME_REGRESSION",
                self._last_session_time_s,
                _present_float(sample.session.session_time_s),
            ),
            (
                "BUFFER_TICK_REGRESSION",
                self._last_buffer_tick,
                _present_int(sample.session.sdk_buffer_tick),
            ),
            (
                "CAPTURE_TIME_REGRESSION",
                self._last_capture_s,
                _present_float(sample.session.captured_monotonic_s),
            ),
            (
                "LAP_NUMBER_REGRESSION",
                self._last_lap_number,
                _present_int(sample.lap.lap_number),
            ),
            (
                "LAPS_COMPLETED_REGRESSION",
                self._last_laps_completed,
                _present_int(sample.lap.laps_completed),
            ),
        )
        return tuple(
            reason
            for reason, previous, current in checks
            if previous is not None and current is not None and current < previous
        )

    def _update_clocks(self, sample: TelemetrySample) -> None:
        self._last_session_tick = _present_int(sample.session.session_tick)
        self._last_session_time_s = _present_float(sample.session.session_time_s)
        self._last_buffer_tick = _present_int(sample.session.sdk_buffer_tick)
        self._last_capture_s = _present_float(sample.session.captured_monotonic_s)

    def _source_metadata(
        self, sample: TelemetrySample
    ) -> tuple[str | None, SourceKind | None, tuple[str, ...]]:
        source_id = _present(sample.source.source_id, str)
        source_kind = _present(sample.source.source_kind, SourceKind)
        issues: list[str] = []
        if source_id is None or not source_id:
            issues.append("SOURCE_ID_UNAVAILABLE")
            source_id = None
        if source_kind is None:
            issues.append("SOURCE_KIND_UNAVAILABLE")
        if not isinstance(sample.contract_version, str) or not sample.contract_version:
            issues.append("SCHEMA_ID_UNAVAILABLE")
        return source_id, source_kind, tuple(issues)

    def _feed_one(self, sample: TelemetrySample) -> tuple[TelemetryEvent, ...]:
        if not isinstance(sample, TelemetrySample):
            raise TypeError("event pipeline input must be a TelemetrySample")
        start_index = len(self._events)
        self._sample_count += 1
        source_id, source_kind, metadata_issues = self._source_metadata(sample)
        if metadata_issues:
            self._source_key = None
            self._session_token = None
            self._session_epoch = None
            self._clear_session_state()
            self._rejected_count += 1
            self._event(
                EventKind.QUALITY_REJECTED,
                sample,
                details={"issues": metadata_issues},
                source_id=source_id,
                source_kind=source_kind,
            )
            return tuple(self._events[start_index:])

        if source_id is None or source_kind is None:  # pragma: no cover - guarded above
            raise AssertionError("validated source metadata became unavailable")
        current_source = (source_id, source_kind, sample.contract_version)
        source_boundary = current_source != self._source_key
        if source_boundary:
            if self._source_key is not None:
                previous_id, previous_kind, previous_schema = self._source_key
                previous_session_identity = self._session_token or (None, None)
                reasons: list[str] = []
                if source_id != previous_id:
                    reasons.append("SOURCE_ID_CHANGED")
                if source_kind is not previous_kind:
                    reasons.append("SOURCE_KIND_CHANGED")
                if sample.contract_version != previous_schema:
                    reasons.append("SCHEMA_CHANGED")
                self._event(
                    EventKind.SOURCE_RESET,
                    sample,
                    details={
                        "previous_source_id": previous_id,
                        "previous_source_kind": previous_kind.value,
                        "previous_schema": previous_schema,
                        "reasons": tuple(reasons),
                    },
                    source_id=previous_id,
                    source_kind=previous_kind,
                    session_identity_override=previous_session_identity,
                )
            self._source_epoch += 1
            self._source_count += 1
            self._source_key = current_source
            self._session_token = None
            self._session_epoch = None
            self._clear_session_state()
            self._event(
                EventKind.SOURCE_STARTED,
                sample,
                details={"schema": sample.contract_version},
                source_id=source_id,
                source_kind=source_kind,
            )

        session_num = _present_int(sample.session.session_num)
        session_id = _present(sample.session.session_id, str)
        session_token = None if session_num is None else (session_id, session_num)
        identity_reasons: list[str] = []
        if self._session_token is not None and session_token is not None:
            previous_id, previous_num = self._session_token
            if previous_num != session_num:
                identity_reasons.append("SESSION_NUM_CHANGED")
            if previous_id is not None and session_id is not None and previous_id != session_id:
                identity_reasons.append("SESSION_ID_CHANGED")
        quality_status = _present(sample.quality.status, QualityStatus)
        quality_issues_value = _present(sample.quality.issues, tuple)
        quality_issues = (
            tuple(item for item in quality_issues_value if isinstance(item, str))
            if quality_issues_value is not None
            else ()
        )
        regressions = () if source_boundary else self._clock_regressions(sample)
        external_reason_aliases = {
            "SESSIONTICK_REGRESSION": "SESSION_TICK_REGRESSION",
            "SESSIONTIME_REGRESSION": "SESSION_TIME_REGRESSION",
        }
        external_boundary_reasons = tuple(
            external_reason_aliases.get(reason, reason)
            for issue in quality_issues
            if issue.startswith("CONTINUITY_BOUNDARY:")
            for reason in (issue.removeprefix("CONTINUITY_BOUNDARY:"),)
        )
        reset_reasons = tuple(
            dict.fromkeys(
                (*identity_reasons, *regressions, *external_boundary_reasons)
            )
        )
        session_boundary = source_boundary or self._session_token is None or bool(reset_reasons)
        if reset_reasons:
            previous_session_identity = self._session_token or (None, None)
            self._event(
                EventKind.SESSION_RESET,
                sample,
                details={"reasons": reset_reasons},
                source_id=source_id,
                source_kind=source_kind,
                session_identity_override=previous_session_identity,
            )
            self._clear_session_state()
            self._session_token = None
        if session_boundary and session_token is not None:
            self._session_epoch = 0 if self._session_epoch is None else self._session_epoch + 1
            self._session_count += 1
            self._session_token = session_token
            self._event(
                EventKind.SESSION_STARTED,
                sample,
                source_id=source_id,
                source_kind=source_kind,
            )
        elif (
            session_token is not None
            and self._session_token is not None
            and self._session_token[0] is None
            and session_token[0] is not None
        ):
            # SessionInfo identity can arrive after telemetry starts.  Enrich
            # the token without inventing a reset, so later identity changes
            # remain detectable.
            self._session_token = session_token

        rejection_reasons = set(regressions)
        if quality_status is None:
            rejection_reasons.add("QUALITY_STATUS_UNAVAILABLE")
        elif quality_status is QualityStatus.REJECTED:
            rejection_reasons.update(quality_issues or ("NORMALIZED_QUALITY_REJECTED",))
        if session_num is None:
            rejection_reasons.add("SESSION_NUM_UNAVAILABLE")
        if rejection_reasons:
            self._rejected_count += 1
            self._event(
                EventKind.QUALITY_REJECTED,
                sample,
                details={"issues": tuple(sorted(rejection_reasons))},
                source_id=source_id,
                source_kind=source_kind,
            )
        else:
            self._accepted_count += 1

        stale = _present_bool(sample.quality.stale)
        stale_transition = False
        if stale is not None:
            if stale and self._stale_state is not True:
                self._event(
                    EventKind.SOURCE_STALE,
                    sample,
                    source_id=source_id,
                    source_kind=source_kind,
                )
                stale_transition = True
            elif not stale and self._stale_state is True:
                self._event(
                    EventKind.SOURCE_RESUMED,
                    sample,
                    source_id=source_id,
                    source_kind=source_kind,
                )
                stale_transition = True
            self._stale_state = stale

        dropped_ticks = _present_int(sample.quality.dropped_ticks)
        if dropped_ticks is not None and dropped_ticks > 0:
            self._event(
                EventKind.DROPPED_TICKS,
                sample,
                details={"count": dropped_ticks},
                source_id=source_id,
                source_kind=source_kind,
            )

        continuity_unknown = self._session_samples > 0 and (
            stale is None or dropped_ticks is None
        )
        suppress_transitions = bool(
            session_boundary
            or rejection_reasons
            or stale
            or stale_transition
            or (dropped_ticks is not None and dropped_ticks > 0)
            or continuity_unknown
        )
        if rejection_reasons:
            # A rejected sample is not trustworthy transition evidence and must
            # not seed the next accepted sample's edge detector.
            self._clear_dynamic_state()
        elif suppress_transitions:
            self._clear_dynamic_state()
            self._prime_dynamic_state(sample)
        else:
            self._observe_transitions(sample, source_id, source_kind)

        self._update_clocks(sample)
        self._session_samples += 1
        return tuple(self._events[start_index:])

    def _observe_transitions(
        self,
        sample: TelemetrySample,
        source_id: str,
        source_kind: SourceKind,
    ) -> None:
        lap_number = _present_int(sample.lap.lap_number)
        laps_completed = _present_int(sample.lap.laps_completed)
        lap_distance = _present_lap_distance(sample.lap.lap_distance_pct)

        if laps_completed is not None:
            if self._last_laps_completed is not None and laps_completed > self._last_laps_completed:
                self._event(
                    EventKind.LAP_COMPLETED,
                    sample,
                    details={
                        "completed_count": laps_completed - self._last_laps_completed,
                        "current_laps_completed": laps_completed,
                        "previous_laps_completed": self._last_laps_completed,
                    },
                    source_id=source_id,
                    source_kind=source_kind,
                )
            self._last_laps_completed = laps_completed
        else:
            self._last_laps_completed = None

        if (
            lap_distance is not None
            and self._last_lap_distance_pct is not None
            and self._last_lap_distance_pct >= self._LAP_WRAP_HIGH
            and lap_distance <= self._LAP_WRAP_LOW
        ):
            self._event(
                EventKind.LAP_WRAP,
                sample,
                details={
                    "current_lap_distance_pct": lap_distance,
                    "previous_lap_distance_pct": self._last_lap_distance_pct,
                },
                source_id=source_id,
                source_kind=source_kind,
            )
        self._last_lap_distance_pct = lap_distance
        self._last_lap_number = lap_number

        self._observe_bool_transition(
            sample,
            current=_present_bool(sample.pit.on_pit_road),
            previous_name="_last_on_pit_road",
            entered=EventKind.PIT_ROAD_ENTERED,
            exited=EventKind.PIT_ROAD_EXITED,
            source_id=source_id,
            source_kind=source_kind,
        )
        self._observe_bool_transition(
            sample,
            current=_present_bool(sample.pit.in_pit_stall),
            previous_name="_last_in_pit_stall",
            entered=EventKind.PIT_STALL_ENTERED,
            exited=EventKind.PIT_STALL_EXITED,
            source_id=source_id,
            source_kind=source_kind,
        )

        flags = _present_int(sample.flags.session_flags)
        if flags is not None:
            if self._last_flags is not None and flags != self._last_flags:
                self._event(
                    EventKind.FLAG_CHANGED,
                    sample,
                    details={
                        "changed_mask": flags ^ self._last_flags,
                        "current_flags": flags,
                        "previous_flags": self._last_flags,
                    },
                    source_id=source_id,
                    source_kind=source_kind,
                )
            self._last_flags = flags
        else:
            self._last_flags = None

    def _observe_bool_transition(
        self,
        sample: TelemetrySample,
        *,
        current: bool | None,
        previous_name: str,
        entered: EventKind,
        exited: EventKind,
        source_id: str,
        source_kind: SourceKind,
    ) -> None:
        previous = getattr(self, previous_name)
        if current is not None and previous is not None and current != previous:
            self._event(
                entered if current else exited,
                sample,
                source_id=source_id,
                source_kind=source_kind,
            )
        setattr(self, previous_name, current)

    def feed(
        self, samples: TelemetrySample | Iterable[TelemetrySample]
    ) -> tuple[TelemetryEvent, ...]:
        """Consume one sample or one chunk and return only newly emitted events."""

        if self._finished:
            raise RuntimeError("event pipeline is already finished")
        if isinstance(samples, TelemetrySample):
            return self._feed_one(samples)
        if isinstance(samples, (str, bytes, bytearray)) or not isinstance(
            samples, Iterable
        ):
            raise TypeError("feed expects a TelemetrySample or an iterable of samples")
        emitted: list[TelemetryEvent] = []
        for sample in samples:
            emitted.extend(self._feed_one(sample))
        return tuple(emitted)

    def feed_chunk(self, samples: Iterable[TelemetrySample]) -> tuple[TelemetryEvent, ...]:
        """Explicit chunk-oriented alias for :meth:`feed`."""

        return self.feed(samples)

    def finish(self) -> TelemetryEventReceipt:
        """Freeze the pipeline and return an idempotent deterministic receipt."""

        if self._receipt is not None:
            return self._receipt
        self._finished = True
        event_payload = [event.to_dict() for event in self._events]
        events_sha256 = hashlib.sha256(_canonical_json(event_payload)).hexdigest()
        config = {
            "contract_version": EVENT_CONTRACT_VERSION,
            "lap_wrap_high_ppm": round(self._LAP_WRAP_HIGH * 1_000_000),
            "lap_wrap_low_ppm": round(self._LAP_WRAP_LOW * 1_000_000),
            "mode": "streaming-fail-closed-v1",
        }
        config_sha256 = hashlib.sha256(_canonical_json(config)).hexdigest()
        counts = tuple(
            sorted(Counter(event.kind.value for event in self._events).items())
        )
        payload = {
            "accepted_sample_count": self._accepted_count,
            "config_sha256": config_sha256,
            "contract_version": EVENT_CONTRACT_VERSION,
            "event_count": len(self._events),
            "event_kind_counts": dict(counts),
            "events_sha256": events_sha256,
            "rejected_sample_count": self._rejected_count,
            "sample_count": self._sample_count,
            "session_epoch_count": self._session_count,
            "source_epoch_count": self._source_count,
        }
        receipt_sha256 = hashlib.sha256(_canonical_json(payload)).hexdigest()
        self._receipt = TelemetryEventReceipt(
            events_sha256=events_sha256,
            receipt_sha256=receipt_sha256,
            config_sha256=config_sha256,
            sample_count=self._sample_count,
            accepted_sample_count=self._accepted_count,
            rejected_sample_count=self._rejected_count,
            event_count=len(self._events),
            source_epoch_count=self._source_count,
            session_epoch_count=self._session_count,
            event_kind_counts=counts,
        )
        return self._receipt


def process_telemetry_events(
    samples: Iterable[TelemetrySample],
) -> tuple[tuple[TelemetryEvent, ...], TelemetryEventReceipt]:
    """Run a complete stream through the shared event state machine."""

    pipeline = TelemetryEventPipeline()
    pipeline.feed(samples)
    receipt = pipeline.finish()
    return pipeline.events, receipt


run_event_pipeline = process_telemetry_events
