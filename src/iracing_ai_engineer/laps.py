"""Conservative lap segmentation for offline iRacing telemetry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

import numpy as np

from .contracts import LAP_ALGORITHM_VERSION


@dataclass(frozen=True)
class LapBoundary:
    frame_index: int
    time_s: float
    kind: str
    confidence: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class LapObservation:
    ordinal: int
    start_frame: int
    end_frame_exclusive: int
    start_time_s: float
    end_time_s: float
    duration_s: float
    source_lap_start: int | None
    source_lap_end: int | None
    start_boundary: str
    end_boundary: str
    boundary_confidence: str
    distance_coverage_laps: float
    tick_coverage: float
    missing_ticks: int
    max_gap_s: float
    duplicate_time_steps: int
    time_regressions: int
    tick_regressions: int | None
    structurally_complete: bool
    quality_complete: bool
    cleanliness_observable: bool
    clean_for_driving: bool
    fuel_eligible: bool
    on_pit_road_fraction: float
    off_track_fraction: float | None
    incident_delta: int | None
    fuel_start_l: float | None
    fuel_end_l: float | None
    fuel_burn_l: float | None
    tags: tuple[str, ...]
    invalid_reasons: tuple[str, ...]
    algorithm_version: str = LAP_ALGORITHM_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _require_1d(channels: Mapping[str, np.ndarray], name: str) -> np.ndarray:
    if name not in channels:
        raise KeyError(f"missing lap channel: {name}")
    values = np.asarray(channels[name])
    if values.ndim != 1:
        raise ValueError(f"lap channel {name} must be one-dimensional")
    return values


def _counter_delta(channels: Mapping[str, np.ndarray], index: int) -> int | None:
    deltas: list[int] = []
    for name in ("Lap", "LapCompleted"):
        if name in channels:
            values = np.asarray(channels[name])
            deltas.append(int(values[index]) - int(values[index - 1]))
    if not deltas:
        return None
    if any(value < 0 or value > 1 for value in deltas):
        return next(value for value in deltas if value < 0 or value > 1)
    if 1 in deltas:
        return 1
    return max(deltas)


def detect_boundaries(
    channels: Mapping[str, np.ndarray], tick_rate_hz: int
) -> tuple[LapBoundary, ...]:
    time_s = _require_1d(channels, "SessionTime").astype(np.float64, copy=False)
    distance = _require_1d(channels, "LapDistPct").astype(np.float64, copy=False)
    if len(time_s) != len(distance):
        raise ValueError("lap channels have inconsistent record counts")

    boundaries: list[LapBoundary] = [
        LapBoundary(0, float(time_s[0]), "file_start", "low", ("PARTIAL_START",))
    ]
    last_crossing = -10 * tick_rate_hz
    evidence_window = max(1, round(0.25 * tick_rate_hz))
    strong_wrap_indices = set(
        (np.flatnonzero((distance[:-1] >= 0.97) & (distance[1:] <= 0.03)) + 1).tolist()
    )
    counter_increment_indices = {
        index for index in range(1, len(distance)) if _counter_delta(channels, index) == 1
    }

    def evidence_near(indices: set[int], index: int) -> bool:
        return any(abs(candidate - index) <= evidence_window for candidate in indices)

    for index in range(1, len(distance)):
        previous = float(distance[index - 1])
        current = float(distance[index])
        dt = float(time_s[index] - time_s[index - 1])
        counter_delta = _counter_delta(channels, index)

        if not np.isfinite(dt) or dt < 0:
            boundaries.append(
                LapBoundary(index, float(time_s[index]), "source_reset", "high", ("TIME_RESET",))
            )
            continue

        if not np.isfinite(previous) or not np.isfinite(current):
            continue

        strong_wrap = previous >= 0.97 and current <= 0.03 and current - previous < -0.90
        if strong_wrap and index - last_crossing > max(1, int(0.5 * tick_rate_hz)):
            forward_delta = (1.0 - previous) + current
            fraction = (1.0 - previous) / forward_delta if forward_delta > 0 else 1.0
            crossing_time = float(time_s[index - 1] + fraction * dt)
            tags: list[str] = []
            confidence = "high"
            if not evidence_near(counter_increment_indices, index):
                tags.append("COUNTER_DISTANCE_MISMATCH")
                confidence = "medium"
            boundaries.append(
                LapBoundary(index, crossing_time, "strong_wrap", confidence, tuple(tags))
            )
            last_crossing = index
            continue

        if counter_delta == 1:
            if evidence_near(strong_wrap_indices, index):
                continue
            near_line = previous <= 0.03 and current <= 0.03
            speed = np.asarray(channels["Speed"]) if "Speed" in channels else None
            stationary = bool(
                speed is not None
                and max(abs(float(speed[index - 1])), abs(float(speed[index]))) < 1.0
            )
            in_pit_stall = bool(
                "PlayerCarInPitStall" in channels
                and (
                    channels["PlayerCarInPitStall"][index - 1]
                    or channels["PlayerCarInPitStall"][index]
                )
            )
            if near_line and (stationary or in_pit_stall):
                boundaries.append(
                    LapBoundary(
                        index,
                        float(time_s[index]),
                        "source_reset",
                        "high",
                        ("STATIONARY_COUNTER_CHANGE",),
                    )
                )
            elif near_line and index - last_crossing > max(1, int(0.5 * tick_rate_hz)):
                boundaries.append(
                    LapBoundary(
                        index,
                        float(time_s[index]),
                        "counter_only",
                        "medium",
                        ("COUNTER_ONLY_BOUNDARY",),
                    )
                )
                last_crossing = index
            elif not strong_wrap:
                boundaries.append(
                    LapBoundary(
                        index,
                        float(time_s[index]),
                        "source_reset",
                        "high",
                        ("COUNTER_DISTANCE_MISMATCH",),
                    )
                )
            continue

        if counter_delta is not None and (counter_delta < 0 or counter_delta > 1):
            boundaries.append(
                LapBoundary(
                    index,
                    float(time_s[index]),
                    "source_reset",
                    "high",
                    ("LAP_COUNTER_RESET_OR_JUMP",),
                )
            )
            continue

        if abs(current - previous) > 0.02:
            boundaries.append(
                LapBoundary(
                    index,
                    float(time_s[index]),
                    "source_reset",
                    "high",
                    ("TELEPORT_OR_DISTANCE_RESET",),
                )
            )
            continue

    boundaries.append(
        LapBoundary(
            len(time_s),
            float(time_s[-1]),
            "file_end",
            "low",
            ("PARTIAL_END",),
        )
    )

    deduplicated: list[LapBoundary] = []
    for boundary in sorted(boundaries, key=lambda item: (item.frame_index, item.kind)):
        if deduplicated and boundary.frame_index == deduplicated[-1].frame_index:
            if boundary.kind == "file_end":
                continue
            deduplicated[-1] = boundary
        else:
            deduplicated.append(boundary)
    return tuple(deduplicated)


def _distance_coverage(distance: np.ndarray, start: int, end: int) -> float:
    endpoint = min(end + 1, len(distance))
    values = distance[start:endpoint].astype(np.float64, copy=False)
    if len(values) < 2 or not np.all(np.isfinite(values)):
        return 0.0
    delta = np.diff(values)
    delta = np.where(delta < -0.90, delta + 1.0, delta)
    return float(np.sum(delta))


def _max_true_run(mask: np.ndarray) -> int:
    longest = current = 0
    for value in mask:
        if bool(value):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def segment_laps(
    channels: Mapping[str, np.ndarray], tick_rate_hz: int
) -> tuple[LapObservation, ...]:
    time_s = _require_1d(channels, "SessionTime").astype(np.float64, copy=False)
    distance = _require_1d(channels, "LapDistPct").astype(np.float64, copy=False)
    if tick_rate_hz <= 0:
        raise ValueError("tick_rate_hz must be positive")
    lengths = {len(np.asarray(values)) for values in channels.values()}
    if lengths != {len(time_s)}:
        raise ValueError("lap channels have inconsistent record counts")

    boundaries = detect_boundaries(channels, tick_rate_hz)
    observations: list[LapObservation] = []
    def is_trusted(boundary: LapBoundary) -> bool:
        return (
            boundary.kind == "strong_wrap"
            and "COUNTER_DISTANCE_MISMATCH" not in boundary.tags
        )

    for ordinal, (start_boundary, end_boundary) in enumerate(
        zip(boundaries[:-1], boundaries[1:], strict=True)
    ):
        start = start_boundary.frame_index
        end = end_boundary.frame_index
        if end <= start:
            continue
        sample_end = min(end, len(time_s))
        time_window = time_s[start:sample_end]
        if len(time_window) == 0:
            continue
        timing_end = min(end + 1, len(time_s))
        dt = np.diff(time_s[start:timing_end])
        finite_dt = dt[np.isfinite(dt)]
        positive_dt = finite_dt[finite_dt > 0]
        duplicate_time_steps = int(np.sum(finite_dt == 0))
        time_regressions = int(np.sum(~np.isfinite(dt) | (dt < 0)))
        tick_regressions: int | None = None
        if "SessionTick" in channels:
            tick = np.asarray(channels["SessionTick"], dtype=np.int64)[start:timing_end]
            tick_delta = np.diff(tick)
            valid_tick_delta = tick_delta[tick_delta > 0]
            tick_regressions = int(np.sum(tick_delta <= 0))
            missing_ticks = int(np.sum(np.maximum(0, valid_tick_delta - 1)))
            observed_intervals = len(valid_tick_delta)
            tick_gap_s = (
                float(np.max(valid_tick_delta) / tick_rate_hz)
                if len(valid_tick_delta)
                else 0.0
            )
        else:
            implied_steps = np.rint(positive_dt * tick_rate_hz).astype(int)
            missing_ticks = int(np.sum(np.maximum(0, implied_steps - 1)))
            observed_intervals = len(positive_dt)
            tick_gap_s = 0.0
        time_gap_s = float(np.max(positive_dt)) if len(positive_dt) else 0.0
        max_gap_s = max(time_gap_s, tick_gap_s)
        tick_coverage = (
            observed_intervals / (observed_intervals + missing_ticks)
            if observed_intervals + missing_ticks
            else 0.0
        )

        distance_coverage = _distance_coverage(distance, start, end)
        structural = (
            is_trusted(start_boundary)
            and is_trusted(end_boundary)
            and 0.98 <= distance_coverage <= 1.02
        )
        reasons: list[str] = []
        tags = list(start_boundary.tags + end_boundary.tags)
        if not structural:
            reasons.append("PARTIAL_OR_DISTANCE_INCOMPLETE")
        if max_gap_s > 0.25:
            reasons.append("DATA_GAP")
        if tick_coverage < 0.995:
            reasons.append("LOW_TICK_COVERAGE")
        if duplicate_time_steps:
            reasons.append("TIME_DUPLICATE")
        if time_regressions:
            reasons.append("TIME_REGRESSION")
        if tick_regressions:
            reasons.append("TICK_REGRESSION")

        on_pit = np.asarray(channels.get("OnPitRoad", np.zeros(len(time_s), dtype=bool)))[
            start:sample_end
        ].astype(bool, copy=False)
        on_pit_fraction = float(np.mean(on_pit)) if len(on_pit) else 0.0
        if np.any(on_pit):
            tags.append("PIT_LAP")

        off_track_fraction: float | None = None
        if "PlayerTrackSurface" in channels:
            surface = np.asarray(channels["PlayerTrackSurface"])[start:sample_end]
            off_track_fraction = float(np.mean(surface != 3)) if len(surface) else 0.0
            if np.any(surface != 3):
                reasons.append("NON_TRACK_SURFACE")

        lap_distance_window = distance[start : min(end + 1, len(distance))]
        distance_delta = np.diff(lap_distance_window)
        reverse_mask = (distance_delta < -1e-5) & (distance_delta > -0.90)
        if _max_true_run(reverse_mask) >= round(0.5 * tick_rate_hz):
            reasons.append("SUSTAINED_REVERSE")

        incident_delta: int | None = None
        incident_channel_present = False
        incident_channel_observable = False
        for incident_name in (
            "PlayerCarMyIncidentCount",
            "PlayerCarDriverIncidentCount",
            "PlayerCarTeamIncidentCount",
        ):
            if incident_name not in channels:
                continue
            incident_channel_present = True
            incidents = np.asarray(channels[incident_name])[start:sample_end]
            if (
                incidents.ndim != 1
                or not len(incidents)
                or incidents.dtype.kind not in "biuf"
                or not np.all(np.isfinite(incidents))
            ):
                reasons.append("INCIDENT_COUNT_INVALID")
                continue
            incident_channel_observable = True
            channel_delta = int(incidents[-1]) - int(incidents[0])
            if incident_delta is None:
                # Preserve the documented My -> Driver -> Team priority for the
                # reported scalar without assuming the counters are equivalent
                # during team-driver changes.
                incident_delta = channel_delta
            if np.any(np.diff(incidents.astype(np.float64, copy=False)) < 0):
                reasons.append("INCIDENT_COUNT_REGRESSION")
            if channel_delta > 0:
                reasons.append("INCIDENT_COUNT_INCREASED")

        cleanliness_observable = (
            "OnPitRoad" in channels
            and "PlayerTrackSurface" in channels
            and incident_channel_present
            and incident_channel_observable
        )
        if not cleanliness_observable:
            reasons.append("CLEANLINESS_UNOBSERVABLE")

        speed = np.asarray(channels.get("Speed", np.ones(len(time_s))))[start:sample_end]
        if "PlayerTrackSurface" in channels:
            stationary_on_track = (speed < 1.0) & (
                np.asarray(channels["PlayerTrackSurface"])[start:sample_end] == 3
            )
        else:
            stationary_on_track = speed < 1.0
        if _max_true_run(stationary_on_track) > 2 * tick_rate_hz:
            reasons.append("ABNORMAL_STOP")

        fuel_start: float | None = None
        fuel_end: float | None = None
        fuel_burn: float | None = None
        refuel_jump = False
        if "FuelLevel" in channels:
            fuel = np.asarray(channels["FuelLevel"], dtype=np.float64)[start:sample_end]
            if len(fuel) and np.all(np.isfinite(fuel)):
                fuel_start = float(fuel[0])
                fuel_end = float(fuel[-1])
                fuel_burn = fuel_start - fuel_end
                refuel_jump = bool(np.any(np.diff(fuel) > 0.05))
                if refuel_jump:
                    tags.append("REFUEL_DETECTED")

        driving_disqualifiers = {
            "DATA_GAP",
            "LOW_TICK_COVERAGE",
            "NON_TRACK_SURFACE",
            "INCIDENT_COUNT_INCREASED",
            "INCIDENT_COUNT_INVALID",
            "INCIDENT_COUNT_REGRESSION",
            "ABNORMAL_STOP",
            "SUSTAINED_REVERSE",
            "TIME_DUPLICATE",
            "TIME_REGRESSION",
            "TICK_REGRESSION",
            "CLEANLINESS_UNOBSERVABLE",
        }
        confidence = (
            "high"
            if start_boundary.confidence == end_boundary.confidence == "high"
            else "medium"
            if structural
            else "low"
        )
        quality_complete = (
            structural
            and tick_coverage >= 0.995
            and max_gap_s <= 0.25
            and time_regressions == 0
            and (tick_regressions is None or tick_regressions == 0)
        )
        clean_for_driving = (
            quality_complete
            and confidence == "high"
            and cleanliness_observable
            and not np.any(on_pit)
            and tick_coverage >= 0.999
            and max_gap_s <= 0.10
            and not driving_disqualifiers.intersection(reasons)
        )
        fuel_eligible = (
            quality_complete
            and "OnPitRoad" in channels
            and "PlayerCarInPitStall" in channels
            and not np.any(on_pit)
            and fuel_burn is not None
            and fuel_burn > 0.05
            and not refuel_jump
            and max_gap_s <= 0.25
        )

        lap_values = np.asarray(channels["Lap"])[start:sample_end] if "Lap" in channels else []
        source_lap_start = int(lap_values[0]) if len(lap_values) else None
        source_lap_end = int(lap_values[-1]) if len(lap_values) else None
        observations.append(
            LapObservation(
                ordinal=ordinal,
                start_frame=start,
                end_frame_exclusive=sample_end,
                start_time_s=float(start_boundary.time_s),
                end_time_s=float(end_boundary.time_s),
                duration_s=float(end_boundary.time_s - start_boundary.time_s),
                source_lap_start=source_lap_start,
                source_lap_end=source_lap_end,
                start_boundary=start_boundary.kind,
                end_boundary=end_boundary.kind,
                boundary_confidence=confidence,
                distance_coverage_laps=distance_coverage,
                tick_coverage=tick_coverage,
                missing_ticks=missing_ticks,
                max_gap_s=max_gap_s,
                duplicate_time_steps=duplicate_time_steps,
                time_regressions=time_regressions,
                tick_regressions=tick_regressions,
                structurally_complete=structural,
                quality_complete=quality_complete,
                cleanliness_observable=cleanliness_observable,
                clean_for_driving=clean_for_driving,
                fuel_eligible=fuel_eligible,
                on_pit_road_fraction=on_pit_fraction,
                off_track_fraction=off_track_fraction,
                incident_delta=incident_delta,
                fuel_start_l=fuel_start,
                fuel_end_l=fuel_end,
                fuel_burn_l=fuel_burn,
                tags=tuple(dict.fromkeys(tags)),
                invalid_reasons=tuple(dict.fromkeys(reasons)),
            )
        )
    return tuple(observations)
