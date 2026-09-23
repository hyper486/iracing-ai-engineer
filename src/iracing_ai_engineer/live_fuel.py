"""Experimental, advisor-only fuel estimates from low-rate monitor snapshots.

This is not the 60 Hz clean-lap or M2 strategy admission path. Two observed
start/finish crossings bound each fuel sample; linear interpolation estimates
fuel and time at the line. Sparse SDK tick loss is tolerated, but bad intervals,
pit/out laps and uncertain continuity cannot become fuel evidence. Callers must
reset on a stopped/stale stream: ``feed`` cannot detect absence of future calls.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from statistics import fmean

from .fuel import FuelLapSample, _nearest_rank

_MAX_SNAPSHOT_GAP_US = 1_500_000
# SDK yellow/red, waving yellow, pre-green/caution, DQ/repair and start lights.
_UNSUITABLE_FLAGS = (
    0x0008 | 0x0010 | 0x0100 | 0x0200 | 0x0400 | 0x4000 | 0x8000
    | 0x020000 | 0x100000 | 0x200000 | 0x20000000 | 0x40000000
)
_RESET_EVENTS = frozenset({"source_reset", "session_reset"})
_PIT_EVENTS = frozenset({
    "pit_road_entered", "pit_road_exited", "pit_stall_entered", "pit_stall_exited",
})
_BAD_EVENTS = frozenset({"quality_rejected", "source_stale", "source_resumed"})
_RESET_REASONS = frozenset({
    "RESET", "SOURCE_STALE", "SOURCE_STOPPED", "SOURCE_RESET", "SESSION_RESET",
    "SDK_UNAVAILABLE", "MONITOR_STOPPED", "MONITOR_ERROR", "DISCONNECTED",
})


def _number(value: object, *, minimum: float = 0.0) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) and result >= minimum else None


def _integer(value: object, *, maximum: int = 2**63 - 1) -> int | None:
    return value if type(value) is int and 0 <= value <= maximum else None


@dataclass(frozen=True, slots=True)
class LiveFuelConfig:
    reserve_l: float = 2.0
    minimum_valid_laps: int = 5
    conservative_quantile: float = 0.9
    max_history_laps: int = 50
    tank_capacity_l: float | None = None
    timed_race_extra_laps: int = 1

    def __post_init__(self) -> None:
        if _number(self.reserve_l) is None:
            raise ValueError("reserve_l must be finite and non-negative")
        quantile = _number(self.conservative_quantile)
        if quantile is None or not 0.5 <= quantile <= 1.0:
            raise ValueError("conservative_quantile must be between 0.5 and 1")
        if _integer(self.minimum_valid_laps, maximum=10_000) is None or (
            self.minimum_valid_laps < 2
        ):
            raise ValueError("minimum_valid_laps must be an integer from 2 to 10000")
        if _integer(self.max_history_laps, maximum=10_000) is None or (
            self.max_history_laps < self.minimum_valid_laps
        ):
            raise ValueError("max_history_laps must cover minimum_valid_laps, up to 10000")
        if _integer(self.timed_race_extra_laps, maximum=100) is None:
            raise ValueError("timed_race_extra_laps must be an integer from 0 to 100")
        if self.tank_capacity_l is not None:
            capacity = _number(self.tank_capacity_l)
            if capacity is None or capacity <= self.reserve_l:
                raise ValueError("tank_capacity_l must be finite and exceed reserve_l")


@dataclass(frozen=True, slots=True)
class _Point:
    time_us: int
    completed: int
    fraction: float
    fuel_l: float
    incidents: int


@dataclass(frozen=True, slots=True)
class _Boundary:
    time_us: float
    fuel_l: float


_DEFAULT_CONFIG = LiveFuelConfig()


class LiveFuelEngineer:
    """Bounded, single-session fuel learning; every output is a fresh projection.

    ``session_type`` must be independently bound to this snapshot's SessionInfo
    by the caller. Only the exact SDK value ``Race`` enables finish estimates.
    Current range is *whole* laps with the reserve preserved. Finish estimates
    round up to full laps and are not a rule/traffic-aware race strategy.
    """

    def __init__(self, config: LiveFuelConfig = _DEFAULT_CONFIG) -> None:
        if not isinstance(config, LiveFuelConfig):
            raise TypeError("config must be LiveFuelConfig")
        self.config = config
        self._history: deque[FuelLapSample] = deque(maxlen=config.max_history_laps)
        self._identity: tuple[str, int, int] | None = None
        self._sequence: int | None = None
        self._time_us: int | None = None
        self._previous: _Point | None = None
        self._anchor: _Boundary | None = None
        self._completed: int | None = None
        self._incidents: int | None = None

    def _clear(self) -> None:
        self._history.clear()
        self._identity = None
        self._sequence = None
        self._time_us = None
        self._previous = None
        self._anchor = None
        self._completed = None
        self._incidents = None

    def _result(
        self, status: str, reasons: list[str], fuel_l: float | None = None,
    ) -> dict[str, object]:
        messages = {
            "WAIT_CAR": "Waiting for live in-car telemetry.",
            "BLOCKED": "Fuel estimate unavailable for this interval.",
            "LEARNING": (
                f"Learning fuel use: {len(self._history)}/{self.config.minimum_valid_laps}"
                " complete valid laps."
            ),
            "READY": "Experimental fuel estimate; not a pit instruction.",
        }
        return {
            "status": status,
            "reason_codes": list(dict.fromkeys(reasons)),
            "current_fuel_l": fuel_l,
            "valid_laps": len(self._history),
            "required_laps": self.config.minimum_valid_laps,
            "conservative_burn_l_per_lap": None,
            "estimated_laps_remaining": None,
            "fuel_needed_to_finish_l": None,
            "fuel_to_add_l": None,
            "minimum_stops": None,
            "reserve_l": self.config.reserve_l,
            "tank_capacity_l": self.config.tank_capacity_l,
            "observed_burn_range_l_per_lap": None,
            "race_laps_to_go": None,
            "race_horizon_basis": None,
            "fuel_shortfall_l": None,
            "message": messages[status],
            "estimate_only": True,
            "advisor_only": True,
            "executable": False,
        }

    def reset(self, reason: str = "RESET") -> dict[str, object]:
        """Invalidate all state, without echoing arbitrary caller text."""
        self._clear()
        code = reason if type(reason) is str and reason in _RESET_REASONS else "RESET"
        return self._result("WAIT_CAR" if code == "RESET" else "BLOCKED", [code])

    def _interrupt(
        self, reason: str, fuel_l: float | None, *, clear_history: bool = False,
    ) -> dict[str, object]:
        self._previous = None
        self._anchor = None
        if clear_history:
            self._history.clear()
        return self._result("BLOCKED", [reason], fuel_l)

    def feed(
        self, snapshot: Mapping[str, object], *, session_type: str | None = None,
    ) -> dict[str, object]:
        """Consume one *new* privacy-safe monitor snapshot, never raw SDK data."""
        if not isinstance(snapshot, Mapping) or (
            snapshot.get("record_type") != "live_monitor_snapshot"
            or snapshot.get("contract_version") != "live-monitor-v1"
        ):
            self._clear()
            return self._result("BLOCKED", ["INVALID_MONITOR_SNAPSHOT"])
        telemetry = snapshot.get("telemetry")
        context = snapshot.get("context")
        quality = snapshot.get("quality")
        events = snapshot.get("events")
        interval = snapshot.get("interval_invalid_for_fuel")
        if (
            not isinstance(telemetry, Mapping) or not isinstance(context, Mapping)
            or not isinstance(quality, Mapping) or type(events) is not list
            or any(not isinstance(event, Mapping) for event in events)
            or type(interval) is not list or any(type(item) is not str for item in interval)
        ):
            self._clear()
            return self._result("BLOCKED", ["MISSING_MONITOR_EVIDENCE"])
        binding = snapshot.get("binding_sha256")
        session_num = _integer(telemetry.get("session_num"))
        car_idx = _integer(telemetry.get("player_car_idx"), maximum=63)
        sequence = _integer(snapshot.get("sequence"))
        time_us = _integer(snapshot.get("session_time_us"))
        if (
            type(binding) is not str or len(binding) != 64
            or any(character not in "0123456789abcdef" for character in binding)
            or session_num is None or car_idx is None or sequence is None or time_us is None
        ):
            self._clear()
            return self._result("BLOCKED", ["MISSING_STREAM_IDENTITY_OR_CLOCK"])
        identity = (binding, session_num, car_idx)
        kinds = [event.get("kind") for event in events]
        if any(type(kind) is not str for kind in kinds):
            return self._interrupt("INVALID_EVENT_EVIDENCE", None)
        identity_changed = self._identity is not None and identity != self._identity
        reset_event = any(kind in _RESET_EVENTS for kind in kinds)
        if identity_changed or reset_event:
            self._clear()
        self._identity = identity
        fuel_l = _number(telemetry.get("fuel_level_l"))
        capacity = self.config.tank_capacity_l
        if capacity is not None and fuel_l is not None and fuel_l > capacity:
            fuel_l = None

        if (
            snapshot.get("source_kind") != "SDK_LIVE"
            or context.get("sim_source_mode") != "FULL"
        ):
            self._clear()
            return self._result("WAIT_CAR", ["LIVE_SOURCE_REQUIRED"])
        if context.get("player_control_state") != "IN_CAR_PHYSICS":
            self._clear()
            return self._result("WAIT_CAR", ["LIVE_IN_CAR_REQUIRED"])

        old_sequence, old_time = self._sequence, self._time_us
        self._sequence, self._time_us = sequence, time_us
        if identity_changed or reset_event:
            return self._interrupt("SESSION_OR_SOURCE_RESET", fuel_l)
        if old_sequence is not None and sequence <= old_sequence:
            return self._interrupt("SNAPSHOT_SEQUENCE_RESET", fuel_l, clear_history=True)
        if old_time is not None and time_us <= old_time:
            return self._interrupt("SESSION_CLOCK_NOT_ADVANCING", fuel_l, clear_history=True)
        if (
            (old_time is not None and time_us - old_time > _MAX_SNAPSHOT_GAP_US)
            or (old_sequence is not None and sequence != old_sequence + 1)
        ):
            return self._interrupt("SNAPSHOT_GAP", fuel_l)
        if (
            snapshot.get("status") not in ("READY", "DEGRADED")
            or context.get("conflicts") != []
            or quality.get("status") not in ("READY", "DEGRADED")
            or quality.get("stale") is not False
            or any(kind in _BAD_EVENTS for kind in kinds)
        ):
            return self._interrupt("SOURCE_QUALITY_UNSUITABLE", None)
        completed = _integer(telemetry.get("laps_completed"))
        incidents = _integer(telemetry.get("player_incident_count"))
        counter_reset = (
            completed is not None and self._completed is not None and completed < self._completed
        ) or (
            incidents is not None and self._incidents is not None and incidents < self._incidents
        )
        # Keep counter continuity across rejected/pit/missing-value intervals;
        # discarding a lap must not hide a later garage/session counter reset.
        if completed is not None:
            self._completed = completed
        if incidents is not None:
            self._incidents = incidents
        if counter_reset:
            return self._interrupt("LAP_OR_INCIDENT_COUNTER_RESET", fuel_l, clear_history=True)
        if interval:
            return self._interrupt(
                "INTERVAL_NOT_FUEL_ELIGIBLE", fuel_l,
                clear_history=bool({
                    "OUT_OF_CAR_INTERVAL", "NONLIVE_INTERVAL", "IDENTITY_CHANGED_INTERVAL",
                } & set(interval)),
            )
        if any(kind in _PIT_EVENTS for kind in kinds):
            return self._interrupt("PIT_OR_OUT_LAP", fuel_l)
        if fuel_l is None:
            return self._interrupt("FUEL_UNAVAILABLE_OR_INVALID", None)

        fraction = _number(telemetry.get("lap_distance_pct"))
        flags = _integer(telemetry.get("session_flags"), maximum=2**32 - 1)
        surface = _integer(telemetry.get("player_track_surface"), maximum=3)
        if completed is None or fraction is None or fraction > 1.0 or incidents is None:
            return self._interrupt("LAP_OR_INCIDENT_EVIDENCE_UNAVAILABLE", fuel_l)
        if flags is None or surface is None:
            return self._interrupt("TRACK_OR_FLAG_EVIDENCE_UNAVAILABLE", fuel_l)
        if any(telemetry.get(key) is not True for key in ("is_on_track", "is_on_track_car")):
            return self._interrupt("LIVE_IN_CAR_REQUIRED", fuel_l, clear_history=True)
        pit_values = [telemetry.get(key) for key in (
            "on_pit_road", "in_pit_stall", "pitstop_active",
        )]
        if any(type(value) is not bool for value in pit_values):
            return self._interrupt("PIT_STATE_UNAVAILABLE", fuel_l)
        if any(pit_values):
            return self._interrupt("PIT_OR_OUT_LAP", fuel_l)
        if surface != 3:
            return self._interrupt("OFF_TRACK_LAP", fuel_l)
        if flags & 1:
            return self._interrupt("CHECKERED_FLAG", fuel_l)
        if flags & _UNSUITABLE_FLAGS:
            return self._interrupt("FLAGGED_LAP", fuel_l)
        for event in events:
            if event.get("kind") == "flag_changed":
                details = event.get("details")
                if not isinstance(details, Mapping):
                    return self._interrupt("INVALID_EVENT_EVIDENCE", fuel_l)
                flag_values = [_integer(details.get(key), maximum=2**32 - 1) for key in (
                    "previous_flags", "current_flags",
                )]
                if any(value is None for value in flag_values) or any(
                    value is not None and value & (_UNSUITABLE_FLAGS | 1)
                    for value in flag_values
                ):
                    return self._interrupt("FLAGGED_LAP", fuel_l)

        point = _Point(time_us, completed, fraction, fuel_l, incidents)
        previous = self._previous
        self._previous = point
        if previous is not None:
            if incidents != previous.incidents:
                return self._interrupt("INCIDENT_LAP", fuel_l)
            if fuel_l > previous.fuel_l:
                return self._interrupt("REFUEL_DETECTED", fuel_l)
            lap_delta = completed - previous.completed
            distance = lap_delta + fraction - previous.fraction
            if lap_delta > 1 or distance < 0 or distance > 0.5 or (
                lap_delta == 1 and not (previous.fraction >= 0.5 and fraction <= 0.5)
            ):
                return self._interrupt("LAP_POSITION_DISCONTINUITY", fuel_l)
            if lap_delta == 1:
                if distance <= 0:
                    return self._interrupt("LAP_POSITION_DISCONTINUITY", fuel_l)
                weight = (1.0 - previous.fraction) / distance
                boundary = _Boundary(
                    previous.time_us + weight * (time_us - previous.time_us),
                    previous.fuel_l + weight * (fuel_l - previous.fuel_l),
                )
                if self._anchor is not None:
                    burn = self._anchor.fuel_l - boundary.fuel_l
                    lap_time = (boundary.time_us - self._anchor.time_us) / 1_000_000
                    if not math.isfinite(burn) or burn <= 0 or lap_time <= 0:
                        return self._interrupt("INVALID_COMPLETED_LAP", fuel_l)
                    self._history.append(FuelLapSample(burn, lap_time_s=lap_time))
                self._anchor = boundary

        if self._anchor is None:
            return self._result("LEARNING", ["WAITING_FOR_CLEAN_LAP_BOUNDARY"], fuel_l)
        if len(self._history) < self.config.minimum_valid_laps:
            return self._result("LEARNING", ["INSUFFICIENT_VALID_FUEL_LAPS"], fuel_l)
        return self._estimate(fuel_l, telemetry, session_type)

    def _estimate(
        self, fuel_l: float, telemetry: Mapping[str, object], session_type: str | None,
    ) -> dict[str, object]:
        burns = [sample.fuel_burn_l for sample in self._history]
        # Admitted samples are finite positive numbers, not optional/invalid fuel rows.
        try:
            burn = max(fmean(burns), _nearest_rank(burns, self.config.conservative_quantile))
        except OverflowError:
            return self._interrupt("ESTIMATE_OUT_OF_RANGE", fuel_l)
        usable = max(0.0, fuel_l - self.config.reserve_l)
        range_laps = usable / burn
        if not math.isfinite(range_laps):
            return self._interrupt("ESTIMATE_OUT_OF_RANGE", fuel_l)
        result = self._result("READY", ["UNCALIBRATED_FUEL_ESTIMATE"], fuel_l)
        result["conservative_burn_l_per_lap"] = burn
        result["estimated_laps_remaining"] = math.floor(range_laps + 1e-12)
        result["observed_burn_range_l_per_lap"] = [min(burns), max(burns)]
        reasons = result["reason_codes"]
        assert isinstance(reasons, list)
        laps_to_go: int | None = None
        if session_type == "Race":
            laps = _integer(telemetry.get("session_laps_remaining"), maximum=32766)
            remaining_s = _number(telemetry.get("session_time_remaining_s"))
            if laps is not None and laps > 0:
                # Do not subtract current lap fraction: the SDK count is not distance.
                laps_to_go = laps
                result["race_horizon_basis"] = "SDK_LAPS_REMAINING"
            elif remaining_s is not None and 0 <= remaining_s < 604800:
                fastest = min(sample.lap_time_s for sample in self._history)
                laps_to_go = math.ceil(remaining_s / fastest) + self.config.timed_race_extra_laps
                result["race_horizon_basis"] = "TIMER_FASTEST_LAP_PLUS_MARGIN"
        if laps_to_go is None:
            reasons.append("RACE_FINISH_UNCONFIRMED")
        else:
            needed = 0.0 if laps_to_go == 0 else burn * laps_to_go + self.config.reserve_l
            if not math.isfinite(needed):
                return self._interrupt("ESTIMATE_OUT_OF_RANGE", fuel_l)
            result["fuel_needed_to_finish_l"] = needed
            result["race_laps_to_go"] = laps_to_go
            result["fuel_shortfall_l"] = max(0.0, needed - fuel_l)
            capacity = self.config.tank_capacity_l
            if needed <= fuel_l:
                result["minimum_stops"] = 0
            elif capacity is not None:
                # Fuel arithmetic, not per-stint whole-lap rounding: fractions
                # from different stints can together cover another whole lap.
                # An already below-reserve car can restore its reserve at the
                # first fill; do not charge that restoration as another stop.
                usable_deficit = max(0.0, burn * laps_to_go - usable)
                stop_bound = usable_deficit / (capacity - self.config.reserve_l)
                if not math.isfinite(stop_bound):
                    return self._interrupt("ESTIMATE_OUT_OF_RANGE", fuel_l)
                result["minimum_stops"] = max(1, math.ceil(stop_bound))
            if capacity is not None and needed <= capacity:
                result["fuel_to_add_l"] = max(0.0, needed - fuel_l)
            elif capacity is None:
                reasons.append("TANK_CAPACITY_UNCONFIGURED")
            else:
                reasons.append("FINISH_EXCEEDS_ONE_TANK")
        result["message"] = (
            f"Estimated {burn:.2f} L/lap; {result['estimated_laps_remaining']} whole laps"
            " of fuel with reserve. Experimental estimate only."
        )
        return result


__all__ = ["LiveFuelConfig", "LiveFuelEngineer"]
