"""Deterministic, distance-domain driving analysis for clean offline laps.

The module deliberately produces descriptive practice candidates rather than
causal coaching claims.  Callers must pass laps that have already gone through
the conservative quality/cleanliness gates in :mod:`iracing_ai_engineer.laps`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from math import ceil
from typing import Any, Literal

import numpy as np

from .contracts import LAP_ALGORITHM_VERSION
from .laps import LapObservation

DRIVING_ALGORITHM_VERSION = "distance-driving-v1"

REQUIRED_CHANNELS = (
    "SessionTime",
    "LapDistPct",
    "Speed",
    "Throttle",
    "Brake",
    "SteeringWheelAngle",
)


class DrivingDataError(ValueError):
    """Raised by lower-level helpers when a declared clean lap is unusable."""


@dataclass(frozen=True)
class DrivingAnalysisConfig:
    """Thresholds for the intentionally small and explainable v1 rule set."""

    grid_step_m: float = 1.0
    min_clean_laps: int = 3
    fastest_group_fraction: float = 0.40
    min_reference_group_laps: int = 3
    max_reference_duration_spread_fraction: float = 0.03
    min_evidence_laps: int = 2
    brake_threshold: float = 0.08
    brake_release_threshold: float = 0.03
    throttle_pickup_threshold: float = 0.50
    second_lift_threshold: float = 0.25
    min_braking_zone_m: float = 8.0
    max_brake_gap_m: float = 12.0
    approach_window_m: float = 80.0
    apex_search_after_brake_m: float = 240.0
    max_corner_exit_after_apex_m: float = 180.0
    event_sustain_m: float = 4.0
    event_position_difference_m: float = 8.0
    long_coast_difference_m: float = 15.0
    speed_difference_mps: float = 0.4
    minimum_loss_s: float = 0.04
    minimum_carry_loss_s: float = 0.03
    lap_delta_closure_tolerance_s: float = 1e-6


@dataclass(frozen=True)
class ResampledLap:
    """A clean lap monotonically interpolated onto a shared spatial grid."""

    lap_ordinal: int
    duration_s: float
    distance_m: np.ndarray
    elapsed_time_s: np.ndarray
    speed_mps: np.ndarray
    throttle: np.ndarray
    brake: np.ndarray
    steering_rad: np.ndarray


@dataclass(frozen=True)
class ReferenceSelection:
    lap_ordinal: int
    fastest_group_lap_ordinals: tuple[int, ...]
    duration_spread_fraction: float
    trace_median_absolute_error_s: float


@dataclass(frozen=True)
class CornerSegment:
    corner_id: str
    accounting_start_m: float
    approach_start_m: float
    brake_start_m: float
    brake_end_m: float
    apex_m: float
    exit_m: float
    carry_end_m: float


@dataclass(frozen=True)
class LapSummary:
    lap_ordinal: int
    duration_s: float
    lap_delta_s: float
    is_reference: bool
    in_fastest_group: bool


@dataclass(frozen=True)
class LapDeltaClosure:
    """Receipt proving that disjoint corner/carry windows close to lap delta."""

    lap_ordinal: int
    actual_lap_delta_s: float
    summed_window_delta_s: float
    residual_s: float
    tolerance_s: float
    closed: bool


@dataclass(frozen=True)
class CornerLapMetrics:
    corner_id: str
    lap_ordinal: int
    brake_onset_m: float | None
    brake_release_m: float | None
    entry_speed_mps: float
    apex_m: float
    apex_speed_mps: float
    throttle_pickup_m: float | None
    exit_speed_mps: float
    coast_distance_m: float | None
    second_lift: bool | None
    delta_at_accounting_start_s: float
    delta_at_entry_s: float
    delta_at_apex_s: float
    delta_at_exit_s: float
    approach_delta_s: float
    local_delta_s: float
    carry_delta_s: float
    total_segment_delta_s: float
    accounted_window_delta_s: float


@dataclass(frozen=True)
class MetricComparison:
    metric: str
    evidence_median: float
    reference_value: float
    difference: float
    unit: str


@dataclass(frozen=True)
class DrivingDiagnosis:
    corner_id: str
    diagnosis: Literal[
        "LONG_COAST",
        "LATE_BRAKING_HURTS_EXIT",
        "THROTTLE_SECOND_LIFT",
    ]
    claim_level: Literal["descriptive"]
    action: str
    evidence_lap_ordinals: tuple[int, ...]
    counterexample_lap_ordinals: tuple[int, ...]
    comparisons: tuple[MetricComparison, ...]
    estimated_loss_median_s: float
    expected_gain_range_s: tuple[float, float]
    confidence: Literal["medium", "high"]
    practice_only: bool = True


@dataclass(frozen=True)
class DrivingAnalysis:
    status: Literal["READY", "REFUSED"]
    refusal_reasons: tuple[str, ...]
    track_length_m: float
    grid_step_m: float
    eligible_lap_ordinals: tuple[int, ...]
    reference: ReferenceSelection | None
    laps: tuple[ResampledLap, ...]
    lap_summaries: tuple[LapSummary, ...]
    delta_closures: tuple[LapDeltaClosure, ...]
    corners: tuple[CornerSegment, ...]
    corner_metrics: tuple[CornerLapMetrics, ...]
    diagnoses: tuple[DrivingDiagnosis, ...]
    algorithm_version: str = DRIVING_ALGORITHM_VERSION

    def to_dict(self, *, include_traces: bool = False) -> dict[str, Any]:
        """Return a JSON-friendly representation; traces are opt-in due to size."""

        payload: dict[str, Any] = {
            "status": self.status,
            "refusal_reasons": list(self.refusal_reasons),
            "track_length_m": self.track_length_m,
            "grid_step_m": self.grid_step_m,
            "eligible_lap_ordinals": list(self.eligible_lap_ordinals),
            "reference": asdict(self.reference) if self.reference else None,
            "lap_summaries": [asdict(item) for item in self.lap_summaries],
            "delta_closures": [asdict(item) for item in self.delta_closures],
            "corners": [asdict(item) for item in self.corners],
            "corner_metrics": [asdict(item) for item in self.corner_metrics],
            "diagnoses": [asdict(item) for item in self.diagnoses],
            "algorithm_version": self.algorithm_version,
        }
        if include_traces:
            payload["laps"] = [
                {
                    "lap_ordinal": lap.lap_ordinal,
                    "duration_s": lap.duration_s,
                    "distance_m": lap.distance_m.tolist(),
                    "elapsed_time_s": lap.elapsed_time_s.tolist(),
                    "speed_mps": lap.speed_mps.tolist(),
                    "throttle": lap.throttle.tolist(),
                    "brake": lap.brake.tolist(),
                    "steering_rad": lap.steering_rad.tolist(),
                }
                for lap in self.laps
            ]
        return payload


def build_driving_shadow_recommendations(
    diagnoses: Sequence[DrivingDiagnosis],
    *,
    evidence_prefix: str,
    top: int = 3,
) -> list[dict[str, Any]]:
    """Convert descriptive diagnoses into non-executable shadow candidates.

    ``evidence_prefix`` is deliberately supplied by the caller.  The legacy
    IBT shadow report binds it to the immutable source digest, while the shared
    live/offline replay binds it to the normalized-stream digest.  The model
    wording and units therefore remain identical without pretending that raw
    IBT and collector provenance are interchangeable.
    """

    if (
        type(evidence_prefix) is not str
        or len(evidence_prefix) != 64
        or any(character not in "0123456789abcdef" for character in evidence_prefix)
    ):
        raise ValueError("evidence_prefix must be a lowercase SHA-256 digest")
    if type(top) is not int or top < 1:
        raise ValueError("top must be a positive integer")

    def lap_id(ordinal: int) -> str:
        return f"{evidence_prefix}:{LAP_ALGORITHM_VERSION}:lap:{ordinal}"

    recommendations: list[dict[str, Any]] = []
    for item in diagnoses[:top]:
        if not isinstance(item, DrivingDiagnosis):
            raise TypeError("diagnoses must contain DrivingDiagnosis values")
        recommendations.append(
            {
                "action": item.action,
                "claim_level": item.claim_level,
                "confidence": item.confidence.upper(),
                "corner_id": item.corner_id,
                "counterexample_lap_ids": [
                    lap_id(ordinal) for ordinal in item.counterexample_lap_ordinals
                ],
                "diagnosis": item.diagnosis,
                "estimated_loss_us": round(item.estimated_loss_median_s * 1_000_000),
                "evidence_lap_ids": [
                    lap_id(ordinal) for ordinal in item.evidence_lap_ordinals
                ],
                "executable": False,
                "expected_gain_range_us": [
                    round(value * 1_000_000) for value in item.expected_gain_range_s
                ],
                "kind": "DRIVING_CANDIDATE",
                "metric_comparisons": [asdict(value) for value in item.comparisons],
                "practice_only": True,
                "recommendation_id": f"driving:{item.corner_id}:{item.diagnosis}",
                "status": "SHADOW_ONLY",
            }
        )
    return recommendations


def _validate_config(config: DrivingAnalysisConfig, track_length_m: float) -> None:
    if not np.isfinite(track_length_m) or track_length_m <= 100.0:
        raise ValueError("track_length_m must be finite and greater than 100 m")
    if not np.isfinite(config.grid_step_m) or not 0.25 <= config.grid_step_m <= 10.0:
        raise ValueError("grid_step_m must be between 0.25 and 10 m")
    if config.min_clean_laps < 3:
        raise ValueError("min_clean_laps must be at least 3")
    if config.min_reference_group_laps < 2:
        raise ValueError("min_reference_group_laps must be at least 2")
    if not 0 < config.fastest_group_fraction <= 1:
        raise ValueError("fastest_group_fraction must be in (0, 1]")
    if config.min_evidence_laps < 1:
        raise ValueError("min_evidence_laps must be positive")
    if (
        not np.isfinite(config.lap_delta_closure_tolerance_s)
        or config.lap_delta_closure_tolerance_s <= 0
    ):
        raise ValueError("lap_delta_closure_tolerance_s must be finite and positive")


def _distance_grid(track_length_m: float, grid_step_m: float) -> np.ndarray:
    grid = np.arange(0.0, track_length_m, grid_step_m, dtype=np.float64)
    if not len(grid) or grid[-1] != track_length_m:
        grid = np.append(grid, track_length_m)
    return grid


def _readonly(values: np.ndarray) -> np.ndarray:
    values.setflags(write=False)
    return values


def _resample_lap(
    channels: Mapping[str, np.ndarray],
    lap: LapObservation,
    grid: np.ndarray,
    track_length_m: float,
) -> ResampledLap:
    start = lap.start_frame
    end = lap.end_frame_exclusive
    if start < 0 or end <= start:
        raise DrivingDataError(f"lap {lap.ordinal}: invalid frame range")
    record_count = len(np.asarray(channels["SessionTime"]))
    if end > record_count:
        raise DrivingDataError(f"lap {lap.ordinal}: frame range exceeds channel records")

    sliced = {
        name: np.asarray(channels[name])[start:end].astype(np.float64, copy=False)
        for name in REQUIRED_CHANNELS
    }
    lengths = {len(values) for values in sliced.values()}
    if len(lengths) != 1 or next(iter(lengths), 0) < 10:
        raise DrivingDataError(f"lap {lap.ordinal}: inconsistent or too-short channels")
    if not all(np.all(np.isfinite(values)) for values in sliced.values()):
        raise DrivingDataError(f"lap {lap.ordinal}: non-finite driving values")

    elapsed = sliced["SessionTime"] - float(lap.start_time_s)
    if np.any(np.diff(elapsed) <= 0):
        raise DrivingDataError(f"lap {lap.ordinal}: SessionTime is not strictly increasing")
    if not np.isfinite(lap.duration_s) or lap.duration_s <= 0:
        raise DrivingDataError(f"lap {lap.ordinal}: invalid boundary duration")
    boundary_tail_s = float(lap.duration_s - elapsed[-1])
    if not -0.1 <= float(elapsed[0]) <= 0.25 or not -0.1 <= boundary_tail_s <= 0.25:
        raise DrivingDataError(f"lap {lap.ordinal}: boundary timing conflicts with frames")

    distance = np.clip(sliced["LapDistPct"], 0.0, 1.0) * track_length_m
    boundary_tolerance = max(2.0 * float(grid[1] - grid[0]), 0.03 * track_length_m)
    if distance[0] > boundary_tolerance or distance[-1] < track_length_m - boundary_tolerance:
        raise DrivingDataError(f"lap {lap.ordinal}: start/finish coverage is insufficient")
    reverse_distance = float(np.sum(np.maximum(0.0, -np.diff(distance))))
    if reverse_distance > max(5.0, 0.005 * track_length_m):
        raise DrivingDataError(f"lap {lap.ordinal}: excessive reverse distance")

    monotonic_distance = np.maximum.accumulate(distance)
    keep = np.r_[True, np.diff(monotonic_distance) > 1e-9]
    if float(np.mean(keep)) < 0.95:
        raise DrivingDataError(f"lap {lap.ordinal}: distance is not sufficiently monotonic")
    source_distance = monotonic_distance[keep]
    source_elapsed = elapsed[keep]
    source_values = {
        name: values[keep]
        for name, values in sliced.items()
        if name not in {"SessionTime", "LapDistPct"}
    }

    if source_distance[0] > 0.0:
        source_distance = np.r_[0.0, source_distance]
        source_elapsed = np.r_[0.0, source_elapsed]
        source_values = {name: np.r_[values[0], values] for name, values in source_values.items()}
    else:
        source_elapsed[0] = 0.0
    if source_distance[-1] < track_length_m:
        source_distance = np.r_[source_distance, track_length_m]
        source_elapsed = np.r_[source_elapsed, float(lap.duration_s)]
        source_values = {name: np.r_[values, values[-1]] for name, values in source_values.items()}
    else:
        source_elapsed[-1] = float(lap.duration_s)

    if np.any(np.diff(source_elapsed) <= 0):
        raise DrivingDataError(f"lap {lap.ordinal}: boundary timing conflicts with frames")

    return ResampledLap(
        lap_ordinal=lap.ordinal,
        duration_s=float(lap.duration_s),
        distance_m=_readonly(grid.copy()),
        elapsed_time_s=_readonly(np.interp(grid, source_distance, source_elapsed)),
        speed_mps=_readonly(np.interp(grid, source_distance, source_values["Speed"])),
        throttle=_readonly(np.interp(grid, source_distance, source_values["Throttle"])),
        brake=_readonly(np.interp(grid, source_distance, source_values["Brake"])),
        steering_rad=_readonly(
            np.interp(grid, source_distance, source_values["SteeringWheelAngle"])
        ),
    )


def resample_clean_laps(
    channels: Mapping[str, np.ndarray],
    laps: Sequence[LapObservation],
    *,
    track_length_m: float,
    config: DrivingAnalysisConfig | None = None,
) -> tuple[ResampledLap, ...]:
    """Resample declared clean, complete laps onto one exact distance grid.

    Invalid/non-clean observations are ignored.  A lap carrying a clean label
    but failing spatial or timing invariants raises :class:`DrivingDataError`.
    """

    selected_config = config or DrivingAnalysisConfig()
    _validate_config(selected_config, track_length_m)
    missing = sorted(set(REQUIRED_CHANNELS) - set(channels))
    if missing:
        raise DrivingDataError("missing driving channels: " + ",".join(missing))
    arrays = {name: np.asarray(channels[name]) for name in REQUIRED_CHANNELS}
    malformed = [
        name
        for name, values in arrays.items()
        if values.ndim != 1 or values.dtype.kind not in "biuf"
    ]
    if malformed:
        raise DrivingDataError(
            "driving channels must be one-dimensional numeric arrays: "
            + ",".join(sorted(malformed))
        )
    channel_lengths = {len(values) for values in arrays.values()}
    if len(channel_lengths) != 1:
        raise DrivingDataError("driving channels have inconsistent record counts")

    eligible = sorted(
        (
            lap
            for lap in laps
            if lap.structurally_complete and lap.quality_complete and lap.clean_for_driving
        ),
        key=lambda item: (item.ordinal, item.start_frame),
    )
    ordinals = [item.ordinal for item in eligible]
    if len(set(ordinals)) != len(ordinals):
        raise DrivingDataError("eligible lap ordinals are not unique")
    grid = _distance_grid(track_length_m, selected_config.grid_step_m)
    return tuple(
        _resample_lap(channels, lap, grid, track_length_m) for lap in eligible
    )


def select_reference_lap(
    laps: Sequence[ResampledLap], config: DrivingAnalysisConfig | None = None
) -> ReferenceSelection:
    """Choose a real, central lap from the fastest group, never a composite."""

    selected_config = config or DrivingAnalysisConfig()
    if len(laps) < selected_config.min_clean_laps:
        raise DrivingDataError(
            f"need at least {selected_config.min_clean_laps} clean laps; got {len(laps)}"
        )
    grid_lengths = {len(lap.distance_m) for lap in laps}
    if len(grid_lengths) != 1:
        raise DrivingDataError("resampled laps do not share one distance grid")

    fastest_count = max(
        selected_config.min_reference_group_laps,
        ceil(len(laps) * selected_config.fastest_group_fraction),
    )
    fastest_count = min(fastest_count, len(laps))
    fastest = tuple(
        sorted(laps, key=lambda item: (item.duration_s, item.lap_ordinal))[:fastest_count]
    )
    durations = np.asarray([item.duration_s for item in fastest], dtype=np.float64)
    duration_median = float(np.median(durations))
    spread = float((np.max(durations) - np.min(durations)) / duration_median)
    if spread > selected_config.max_reference_duration_spread_fraction:
        raise DrivingDataError(
            "fastest-lap group is not reproducible: "
            f"duration spread {spread:.4f} exceeds "
            f"{selected_config.max_reference_duration_spread_fraction:.4f}"
        )

    median_trace = np.median(
        np.stack([item.elapsed_time_s for item in fastest], axis=0), axis=0
    )
    scores = {
        item.lap_ordinal: float(np.mean(np.abs(item.elapsed_time_s - median_trace)))
        for item in fastest
    }
    reference = min(
        fastest,
        key=lambda item: (scores[item.lap_ordinal], item.duration_s, item.lap_ordinal),
    )
    return ReferenceSelection(
        lap_ordinal=reference.lap_ordinal,
        fastest_group_lap_ordinals=tuple(item.lap_ordinal for item in fastest),
        duration_spread_fraction=spread,
        trace_median_absolute_error_s=scores[reference.lap_ordinal],
    )


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    padded = np.r_[False, mask.astype(bool, copy=False), False]
    changes = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    return list(zip(starts.tolist(), ends.tolist(), strict=True))


def _bridge_short_gaps(mask: np.ndarray, maximum_gap_samples: int) -> np.ndarray:
    bridged = mask.astype(bool, copy=True)
    for start, end in _runs(~bridged):
        if start > 0 and end < len(bridged) and end - start <= maximum_gap_samples:
            bridged[start:end] = True
    return bridged


def _moving_average(values: np.ndarray, window_samples: int) -> np.ndarray:
    if window_samples <= 1:
        return values.astype(np.float64, copy=True)
    window_samples = min(window_samples, len(values))
    kernel = np.ones(window_samples, dtype=np.float64) / window_samples
    left = window_samples // 2
    right = window_samples - 1 - left
    padded = np.pad(values, (left, right), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def _first_sustained(mask: np.ndarray, start: int, end: int, count: int) -> int | None:
    if end <= start:
        return None
    for run_start, run_end in _runs(mask[start:end]):
        if run_end - run_start >= count:
            return start + run_start
    return None


def detect_corner_segments(
    laps: Sequence[ResampledLap],
    reference: ReferenceSelection,
    config: DrivingAnalysisConfig | None = None,
) -> tuple[CornerSegment, ...]:
    """Learn a basic braking/corner template from the reproducible fast group."""

    selected_config = config or DrivingAnalysisConfig()
    by_ordinal = {lap.lap_ordinal: lap for lap in laps}
    try:
        reference_group = [by_ordinal[item] for item in reference.fastest_group_lap_ordinals]
    except KeyError as exc:
        raise DrivingDataError("reference group contains an unavailable lap") from exc
    if not reference_group:
        raise DrivingDataError("reference group is empty")
    grid = reference_group[0].distance_m
    step = float(np.median(np.diff(grid)))
    median_brake = np.median(np.stack([item.brake for item in reference_group]), axis=0)
    median_speed = np.median(np.stack([item.speed_mps for item in reference_group]), axis=0)
    median_throttle = np.median(
        np.stack([item.throttle for item in reference_group]), axis=0
    )
    smoothed_brake = _moving_average(median_brake, max(1, round(4.0 / step)))
    braking = smoothed_brake >= selected_config.brake_threshold
    braking = _bridge_short_gaps(
        braking, max(1, round(selected_config.max_brake_gap_m / step))
    )
    minimum_samples = max(2, round(selected_config.min_braking_zone_m / step))
    brake_runs = [item for item in _runs(braking) if item[1] - item[0] >= minimum_samples]

    corners: list[CornerSegment] = []
    for index, (brake_start, brake_end) in enumerate(brake_runs):
        next_brake = brake_runs[index + 1][0] if index + 1 < len(brake_runs) else len(grid) - 1
        apex_limit = min(
            next_brake,
            len(grid) - 1,
            brake_end + max(1, round(selected_config.apex_search_after_brake_m / step)),
        )
        if apex_limit <= brake_start + 1:
            continue
        apex = brake_start + int(np.argmin(median_speed[brake_start : apex_limit + 1]))
        if apex <= brake_start:
            continue

        entry_speed = float(median_speed[brake_start])
        apex_speed = float(median_speed[apex])
        recovery_speed = apex_speed + 0.85 * max(0.0, entry_speed - apex_speed)
        exit_limit = min(
            next_brake,
            len(grid) - 1,
            apex + max(1, round(selected_config.max_corner_exit_after_apex_m / step)),
        )
        stable_count = max(2, round(selected_config.event_sustain_m / step))
        exit_mask = (median_throttle >= 0.70) & (median_speed >= recovery_speed)
        exit_index = _first_sustained(exit_mask, apex, exit_limit + 1, stable_count)
        if exit_index is None:
            exit_index = exit_limit
        if exit_index <= apex:
            continue

        approach_start = max(
            0, brake_start - max(1, round(selected_config.approach_window_m / step))
        )
        corners.append(
            CornerSegment(
                corner_id=f"C{len(corners) + 1:02d}",
                accounting_start_m=(
                    0.0 if not corners else float(grid[brake_start])
                ),
                approach_start_m=float(grid[approach_start]),
                brake_start_m=float(grid[brake_start]),
                brake_end_m=float(grid[min(brake_end, len(grid) - 1)]),
                apex_m=float(grid[apex]),
                exit_m=float(grid[exit_index]),
                carry_end_m=float(grid[max(exit_index, next_brake)]),
            )
        )
    return tuple(corners)


def _grid_index(grid: np.ndarray, distance_m: float) -> int:
    return int(np.clip(np.searchsorted(grid, distance_m, side="left"), 0, len(grid) - 1))


def _corner_lap_metrics(
    lap: ResampledLap,
    reference_lap: ResampledLap,
    corner: CornerSegment,
    config: DrivingAnalysisConfig,
) -> CornerLapMetrics:
    grid = lap.distance_m
    step = float(np.median(np.diff(grid)))
    accounting_start = _grid_index(grid, corner.accounting_start_m)
    approach = _grid_index(grid, corner.approach_start_m)
    template_brake = _grid_index(grid, corner.brake_start_m)
    apex_template = _grid_index(grid, corner.apex_m)
    exit_index = _grid_index(grid, corner.exit_m)
    carry_end = _grid_index(grid, corner.carry_end_m)
    carry_end = max(carry_end, exit_index)
    sustain = max(2, round(config.event_sustain_m / step))

    brake_onset = _first_sustained(
        lap.brake >= config.brake_threshold,
        approach,
        min(exit_index + 1, len(grid)),
        sustain,
    )
    release: int | None = None
    if brake_onset is not None:
        brake_active = np.flatnonzero(
            lap.brake[brake_onset : min(exit_index + 1, len(grid))]
            > config.brake_release_threshold
        )
        if len(brake_active):
            release = min(brake_onset + int(brake_active[-1]) + 1, exit_index)

    apex_start = brake_onset if brake_onset is not None else template_brake
    apex_end = max(apex_start + 1, exit_index)
    apex = apex_start + int(np.argmin(lap.speed_mps[apex_start : apex_end + 1]))
    pickup_search_start = max(apex_start, apex - round(60.0 / step))
    pickup = _first_sustained(
        lap.throttle >= config.throttle_pickup_threshold,
        pickup_search_start,
        min(carry_end + 1, len(grid)),
        sustain,
    )

    coast_distance: float | None = None
    if release is not None and pickup is not None and pickup > release:
        coast = (lap.brake[release:pickup] <= config.brake_release_threshold) & (
            lap.throttle[release:pickup] <= 0.15
        )
        coast_distance = float(np.sum(coast) * step)

    second_lift: bool | None = None
    if pickup is not None:
        search_start = min(pickup + sustain, carry_end)
        lift_mask = lap.throttle <= config.second_lift_threshold
        second_lift = (
            _first_sustained(lift_mask, search_start, carry_end, sustain) is not None
        )

    delta = lap.elapsed_time_s - reference_lap.elapsed_time_s
    delta_entry = float(delta[template_brake])
    delta_apex = float(delta[apex_template])
    delta_exit = float(delta[exit_index])
    delta_carry = float(delta[carry_end])
    delta_accounting_start = float(delta[accounting_start])
    entry_index = brake_onset if brake_onset is not None else template_brake
    return CornerLapMetrics(
        corner_id=corner.corner_id,
        lap_ordinal=lap.lap_ordinal,
        brake_onset_m=float(grid[brake_onset]) if brake_onset is not None else None,
        brake_release_m=float(grid[release]) if release is not None else None,
        entry_speed_mps=float(lap.speed_mps[entry_index]),
        apex_m=float(grid[apex]),
        apex_speed_mps=float(lap.speed_mps[apex]),
        throttle_pickup_m=float(grid[pickup]) if pickup is not None else None,
        exit_speed_mps=float(lap.speed_mps[exit_index]),
        coast_distance_m=coast_distance,
        second_lift=second_lift,
        delta_at_accounting_start_s=delta_accounting_start,
        delta_at_entry_s=delta_entry,
        delta_at_apex_s=delta_apex,
        delta_at_exit_s=delta_exit,
        approach_delta_s=delta_entry - delta_accounting_start,
        local_delta_s=delta_exit - delta_entry,
        carry_delta_s=delta_carry - delta_exit,
        total_segment_delta_s=delta_carry - delta_entry,
        accounted_window_delta_s=delta_carry - delta_accounting_start,
    )


def _has_numbers(item: CornerLapMetrics, names: Sequence[str]) -> bool:
    return all(getattr(item, name) is not None for name in names)


def _comparison(
    evidence: Sequence[CornerLapMetrics],
    reference: CornerLapMetrics,
    name: str,
    unit: str,
) -> MetricComparison:
    observed = float(np.median([float(getattr(item, name)) for item in evidence]))
    reference_value = float(getattr(reference, name))
    return MetricComparison(name, observed, reference_value, observed - reference_value, unit)


def _diagnosis(
    *,
    corner_id: str,
    code: Literal["LONG_COAST", "LATE_BRAKING_HURTS_EXIT", "THROTTLE_SECOND_LIFT"],
    action: str,
    evidence: Sequence[CornerLapMetrics],
    all_non_reference: Sequence[CornerLapMetrics],
    comparisons: tuple[MetricComparison, ...],
) -> DrivingDiagnosis:
    losses = np.asarray(
        [max(0.0, item.total_segment_delta_s) for item in evidence], dtype=np.float64
    )
    loss = float(np.median(losses))
    return DrivingDiagnosis(
        corner_id=corner_id,
        diagnosis=code,
        claim_level="descriptive",
        action=action,
        evidence_lap_ordinals=tuple(sorted(item.lap_ordinal for item in evidence)),
        counterexample_lap_ordinals=tuple(
            sorted(
                item.lap_ordinal
                for item in all_non_reference
                if item.lap_ordinal not in {evidence_item.lap_ordinal for evidence_item in evidence}
            )
        ),
        comparisons=comparisons,
        estimated_loss_median_s=loss,
        expected_gain_range_s=(round(max(0.01, loss * 0.4), 3), round(loss, 3)),
        confidence="high" if len(evidence) >= 3 else "medium",
    )


def generate_diagnoses(
    metrics: Sequence[CornerLapMetrics],
    reference_lap_ordinal: int,
    config: DrivingAnalysisConfig | None = None,
) -> tuple[DrivingDiagnosis, ...]:
    """Apply the three v1 evidence rules to per-corner measurements."""

    selected_config = config or DrivingAnalysisConfig()
    corner_ids = tuple(dict.fromkeys(item.corner_id for item in metrics))
    diagnoses: list[DrivingDiagnosis] = []
    for corner_id in corner_ids:
        corner_metrics = [item for item in metrics if item.corner_id == corner_id]
        references = [
            item for item in corner_metrics if item.lap_ordinal == reference_lap_ordinal
        ]
        if len(references) != 1:
            raise DrivingDataError(f"{corner_id}: expected exactly one reference metric")
        reference = references[0]
        others = [item for item in corner_metrics if item.lap_ordinal != reference_lap_ordinal]
        position_delta = selected_config.event_position_difference_m
        speed_delta = selected_config.speed_difference_mps

        long_coast: list[CornerLapMetrics] = []
        if _has_numbers(reference, ("brake_onset_m", "coast_distance_m")):
            long_coast = [
                item
                for item in others
                if _has_numbers(item, ("brake_onset_m", "coast_distance_m"))
                and float(item.brake_onset_m) <= float(reference.brake_onset_m) - position_delta
                and float(item.coast_distance_m)
                >= float(reference.coast_distance_m) + selected_config.long_coast_difference_m
                and item.apex_speed_mps <= reference.apex_speed_mps + 0.2
                and item.exit_speed_mps <= reference.exit_speed_mps + 0.2
                and item.total_segment_delta_s >= selected_config.minimum_loss_s
            ]
        if len(long_coast) >= selected_config.min_evidence_laps:
            diagnoses.append(
                _diagnosis(
                    corner_id=corner_id,
                    code="LONG_COAST",
                    action=(
                        "Practice moving the brake point later in small steps while keeping "
                        "one continuous release-to-throttle transition."
                    ),
                    evidence=long_coast,
                    all_non_reference=others,
                    comparisons=(
                        _comparison(long_coast, reference, "brake_onset_m", "m"),
                        _comparison(long_coast, reference, "coast_distance_m", "m"),
                        _comparison(long_coast, reference, "apex_speed_mps", "m/s"),
                        _comparison(long_coast, reference, "exit_speed_mps", "m/s"),
                        _comparison(long_coast, reference, "total_segment_delta_s", "s"),
                    ),
                )
            )

        late_braking: list[CornerLapMetrics] = []
        late_required = (
            "brake_onset_m",
            "brake_release_m",
            "throttle_pickup_m",
        )
        if _has_numbers(reference, late_required):
            late_braking = [
                item
                for item in others
                if _has_numbers(item, late_required)
                and float(item.brake_onset_m) >= float(reference.brake_onset_m) + position_delta
                and float(item.brake_release_m)
                >= float(reference.brake_release_m) + position_delta
                and item.apex_speed_mps <= reference.apex_speed_mps - speed_delta
                and float(item.throttle_pickup_m)
                >= float(reference.throttle_pickup_m) + position_delta
                and item.exit_speed_mps <= reference.exit_speed_mps - speed_delta
                and item.carry_delta_s >= selected_config.minimum_carry_loss_s
                and item.total_segment_delta_s >= selected_config.minimum_loss_s
            ]
        if len(late_braking) >= selected_config.min_evidence_laps:
            diagnoses.append(
                _diagnosis(
                    corner_id=corner_id,
                    code="LATE_BRAKING_HURTS_EXIT",
                    action=(
                        "Brake slightly earlier and shorter, then release pressure earlier "
                        "to prioritize minimum speed and the exit."
                    ),
                    evidence=late_braking,
                    all_non_reference=others,
                    comparisons=(
                        _comparison(late_braking, reference, "brake_onset_m", "m"),
                        _comparison(late_braking, reference, "brake_release_m", "m"),
                        _comparison(late_braking, reference, "apex_speed_mps", "m/s"),
                        _comparison(late_braking, reference, "throttle_pickup_m", "m"),
                        _comparison(late_braking, reference, "exit_speed_mps", "m/s"),
                        _comparison(late_braking, reference, "carry_delta_s", "s"),
                    ),
                )
            )

        second_lift: list[CornerLapMetrics] = []
        if (
            _has_numbers(reference, ("throttle_pickup_m",))
            and reference.second_lift is False
        ):
            second_lift = [
                item
                for item in others
                if _has_numbers(item, ("throttle_pickup_m",))
                and item.second_lift is True
                and float(item.throttle_pickup_m)
                <= float(reference.throttle_pickup_m) - max(5.0, position_delta * 0.625)
                and item.exit_speed_mps <= reference.exit_speed_mps + 0.2
                and item.total_segment_delta_s >= selected_config.minimum_loss_s
            ]
        if len(second_lift) >= selected_config.min_evidence_laps:
            comparisons = (
                _comparison(second_lift, reference, "throttle_pickup_m", "m"),
                MetricComparison(
                    "second_lift",
                    1.0,
                    float(bool(reference.second_lift)),
                    1.0,
                    "bool",
                ),
                _comparison(second_lift, reference, "exit_speed_mps", "m/s"),
                _comparison(second_lift, reference, "total_segment_delta_s", "s"),
            )
            diagnoses.append(
                _diagnosis(
                    corner_id=corner_id,
                    code="THROTTLE_SECOND_LIFT",
                    action=(
                        "Use a slightly later but stable single throttle application in "
                        "practice instead of an early application followed by a lift."
                    ),
                    evidence=second_lift,
                    all_non_reference=others,
                    comparisons=comparisons,
                )
            )

    priority = {
        "LATE_BRAKING_HURTS_EXIT": 0,
        "LONG_COAST": 1,
        "THROTTLE_SECOND_LIFT": 2,
    }
    return tuple(
        sorted(
            diagnoses,
            key=lambda item: (
                -item.estimated_loss_median_s,
                item.corner_id,
                priority[item.diagnosis],
            ),
        )
    )


def _corner_partition_error(
    corners: Sequence[CornerSegment], track_length_m: float, tolerance_m: float
) -> str | None:
    """Return why accounting windows do not form one exact ordered partition."""

    if not corners:
        return "no corner windows"
    expected_start = 0.0
    for corner in corners:
        if abs(corner.accounting_start_m - expected_start) > tolerance_m:
            return (
                f"{corner.corner_id} starts at {corner.accounting_start_m:.6f} m; "
                f"expected {expected_start:.6f} m"
            )
        if corner.carry_end_m <= corner.accounting_start_m:
            return f"{corner.corner_id} has a non-positive accounting window"
        expected_start = corner.carry_end_m
    if abs(expected_start - track_length_m) > tolerance_m:
        return (
            f"final window ends at {expected_start:.6f} m; "
            f"expected {track_length_m:.6f} m"
        )
    return None


def _lap_delta_closures(
    laps: Sequence[ResampledLap],
    metrics: Sequence[CornerLapMetrics],
    reference_lap: ResampledLap,
    tolerance_s: float,
) -> tuple[LapDeltaClosure, ...]:
    receipts: list[LapDeltaClosure] = []
    for lap in laps:
        actual = float(lap.duration_s - reference_lap.duration_s)
        summed = float(
            np.sum(
                [
                    item.accounted_window_delta_s
                    for item in metrics
                    if item.lap_ordinal == lap.lap_ordinal
                ],
                dtype=np.float64,
            )
        )
        residual = summed - actual
        receipts.append(
            LapDeltaClosure(
                lap_ordinal=lap.lap_ordinal,
                actual_lap_delta_s=actual,
                summed_window_delta_s=summed,
                residual_s=residual,
                tolerance_s=tolerance_s,
                closed=abs(residual) <= tolerance_s,
            )
        )
    return tuple(receipts)


def _refused(
    reasons: Sequence[str],
    track_length_m: float,
    config: DrivingAnalysisConfig,
    eligible_lap_ordinals: Sequence[int] = (),
) -> DrivingAnalysis:
    return DrivingAnalysis(
        status="REFUSED",
        refusal_reasons=tuple(dict.fromkeys(reasons)),
        track_length_m=float(track_length_m),
        grid_step_m=config.grid_step_m,
        eligible_lap_ordinals=tuple(eligible_lap_ordinals),
        reference=None,
        laps=(),
        lap_summaries=(),
        delta_closures=(),
        corners=(),
        corner_metrics=(),
        diagnoses=(),
    )


def analyze_driving(
    channels: Mapping[str, np.ndarray],
    laps: Sequence[LapObservation],
    *,
    track_length_m: float,
    config: DrivingAnalysisConfig | None = None,
) -> DrivingAnalysis:
    """Run the complete v1 offline driving analysis.

    Telemetry insufficiency returns ``status="REFUSED"`` with no traces,
    metrics, or diagnoses. Invalid configuration remains a programming error
    and raises :class:`ValueError`.
    """

    selected_config = config or DrivingAnalysisConfig()
    _validate_config(selected_config, track_length_m)
    missing = sorted(set(REQUIRED_CHANNELS) - set(channels))
    if missing:
        return _refused(
            ("MISSING_CHANNELS:" + ",".join(missing),), track_length_m, selected_config
        )
    eligible_ordinals = tuple(
        sorted(
            item.ordinal
            for item in laps
            if item.structurally_complete and item.quality_complete and item.clean_for_driving
        )
    )
    if len(eligible_ordinals) < selected_config.min_clean_laps:
        return _refused(
            (
                f"INSUFFICIENT_CLEAN_LAPS:{len(eligible_ordinals)}"
                f"<{selected_config.min_clean_laps}",
            ),
            track_length_m,
            selected_config,
            eligible_ordinals,
        )
    try:
        resampled = resample_clean_laps(
            channels, laps, track_length_m=track_length_m, config=selected_config
        )
        reference = select_reference_lap(resampled, selected_config)
        corners = detect_corner_segments(resampled, reference, selected_config)
    except DrivingDataError as exc:
        return _refused(
            ("INCONSISTENT_TELEMETRY:" + str(exc),),
            track_length_m,
            selected_config,
            eligible_ordinals,
        )
    if not corners:
        return _refused(
            ("NO_REPRODUCIBLE_BRAKING_ZONES",),
            track_length_m,
            selected_config,
            eligible_ordinals,
        )
    partition_error = _corner_partition_error(
        corners,
        track_length_m,
        max(1e-9, selected_config.grid_step_m * 1e-6),
    )
    if partition_error is not None:
        return _refused(
            ("NON_CLOSING_CORNER_PARTITION:" + partition_error,),
            track_length_m,
            selected_config,
            eligible_ordinals,
        )

    by_ordinal = {item.lap_ordinal: item for item in resampled}
    reference_lap = by_ordinal[reference.lap_ordinal]
    lap_summaries = tuple(
        LapSummary(
            lap_ordinal=item.lap_ordinal,
            duration_s=item.duration_s,
            lap_delta_s=item.duration_s - reference_lap.duration_s,
            is_reference=item.lap_ordinal == reference.lap_ordinal,
            in_fastest_group=item.lap_ordinal in reference.fastest_group_lap_ordinals,
        )
        for item in resampled
    )
    metrics = tuple(
        _corner_lap_metrics(lap, reference_lap, corner, selected_config)
        for corner in corners
        for lap in resampled
    )
    closures = _lap_delta_closures(
        resampled,
        metrics,
        reference_lap,
        selected_config.lap_delta_closure_tolerance_s,
    )
    failed_closures = [item for item in closures if not item.closed]
    if failed_closures:
        failure = failed_closures[0]
        return _refused(
            (
                "LAP_DELTA_CLOSURE_FAILED:"
                f"lap={failure.lap_ordinal},residual_s={failure.residual_s:.12g}",
            ),
            track_length_m,
            selected_config,
            eligible_ordinals,
        )
    diagnoses = generate_diagnoses(metrics, reference.lap_ordinal, selected_config)
    return DrivingAnalysis(
        status="READY",
        refusal_reasons=(),
        track_length_m=float(track_length_m),
        grid_step_m=selected_config.grid_step_m,
        eligible_lap_ordinals=eligible_ordinals,
        reference=reference,
        laps=resampled,
        lap_summaries=lap_summaries,
        delta_closures=closures,
        corners=corners,
        corner_metrics=metrics,
        diagnoses=diagnoses,
    )
