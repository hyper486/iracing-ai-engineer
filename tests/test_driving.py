from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pytest

import iracing_ai_engineer.driving as driving_module
from iracing_ai_engineer.driving import (
    DrivingAnalysisConfig,
    analyze_driving,
    resample_clean_laps,
    select_reference_lap,
)
from iracing_ai_engineer.laps import LapObservation

TRACK_LENGTH_M = 1_200.0


def _corner_profile(
    distance: np.ndarray,
    speed: np.ndarray,
    throttle: np.ndarray,
    brake: np.ndarray,
    *,
    brake_onset: int,
    brake_release: int,
    apex: int,
    throttle_pickup: int,
    minimum_speed: float,
    recovery_end: int,
) -> None:
    brake[(distance >= brake_onset) & (distance < brake_release)] = 0.72
    throttle[(distance >= brake_onset) & (distance < throttle_pickup)] = 0.0

    braking = (distance >= brake_onset) & (distance <= apex)
    acceleration = (distance > apex) & (distance <= recovery_end)
    speed[braking] = np.linspace(50.0, minimum_speed, int(np.sum(braking)))
    speed[acceleration] = np.linspace(
        minimum_speed, 49.0, int(np.sum(acceleration))
    )


def _lap_channels(kind: str) -> dict[str, np.ndarray]:
    distance = np.arange(0.0, TRACK_LENGTH_M + 1.0, dtype=np.float64)
    speed = np.full_like(distance, 50.0)
    throttle = np.ones_like(distance)
    brake = np.zeros_like(distance)
    steering = np.zeros_like(distance)

    first = {
        "brake_onset": 180,
        "brake_release": 240,
        "apex": 260,
        "throttle_pickup": 270,
        "minimum_speed": 20.0,
        "recovery_end": 350,
    }
    second = {
        "brake_onset": 530,
        "brake_release": 590,
        "apex": 610,
        "throttle_pickup": 620,
        "minimum_speed": 20.0,
        "recovery_end": 700,
    }
    third = {
        "brake_onset": 880,
        "brake_release": 940,
        "apex": 960,
        "throttle_pickup": 970,
        "minimum_speed": 20.0,
        "recovery_end": 1_050,
    }
    if kind == "long_coast":
        first.update(
            brake_onset=158,
            brake_release=218,
            throttle_pickup=292,
            minimum_speed=18.5,
            recovery_end=380,
        )
    elif kind == "late_brake":
        second.update(
            brake_onset=552,
            brake_release=612,
            apex=625,
            throttle_pickup=648,
            minimum_speed=17.5,
            recovery_end=760,
        )
    elif kind not in {"baseline", "second_lift"}:
        raise AssertionError(f"unknown synthetic lap kind: {kind}")

    _corner_profile(distance, speed, throttle, brake, **first)
    _corner_profile(distance, speed, throttle, brake, **second)
    _corner_profile(distance, speed, throttle, brake, **third)
    if kind == "second_lift":
        throttle[(distance >= 950) & (distance < 980)] = 0.70
        throttle[(distance >= 980) & (distance < 1_000)] = 0.08
        speed[(distance >= 980) & (distance <= 1_100)] -= np.linspace(
            0.0, 4.0, 121
        )

    for apex in (260, 610, 960):
        steering += 0.18 * np.exp(-0.5 * ((distance - apex) / 35.0) ** 2)

    segment_time = np.diff(distance) / (
        (speed[:-1] + speed[1:]) / 2.0
    )
    elapsed = np.r_[0.0, np.cumsum(segment_time)]
    return {
        "SessionTime": elapsed,
        "LapDistPct": distance / TRACK_LENGTH_M,
        "Speed": speed,
        "Throttle": throttle,
        "Brake": brake,
        "SteeringWheelAngle": steering,
    }


def _observation(ordinal: int, start: int, end: int, start_time: float, duration: float):
    return LapObservation(
        ordinal=ordinal,
        start_frame=start,
        end_frame_exclusive=end,
        start_time_s=start_time,
        end_time_s=start_time + duration,
        duration_s=duration,
        source_lap_start=ordinal,
        source_lap_end=ordinal,
        start_boundary="strong_wrap",
        end_boundary="strong_wrap",
        boundary_confidence="high",
        distance_coverage_laps=1.0,
        tick_coverage=1.0,
        missing_ticks=0,
        max_gap_s=0.02,
        duplicate_time_steps=0,
        time_regressions=0,
        tick_regressions=0,
        structurally_complete=True,
        quality_complete=True,
        cleanliness_observable=True,
        clean_for_driving=True,
        fuel_eligible=True,
        on_pit_road_fraction=0.0,
        off_track_fraction=0.0,
        incident_delta=0,
        fuel_start_l=100.0,
        fuel_end_l=96.0,
        fuel_burn_l=4.0,
        tags=(),
        invalid_reasons=(),
    )


def _synthetic_session():
    kinds = ["baseline"] * 6 + ["long_coast"] * 2
    kinds += ["late_brake"] * 2 + ["second_lift"] * 2
    chunks: dict[str, list[np.ndarray]] = {}
    observations: list[LapObservation] = []
    frame = 0
    session_offset = 0.0
    for ordinal, kind in enumerate(kinds, start=1):
        lap = _lap_channels(kind)
        duration = float(lap["SessionTime"][-1])
        lap["SessionTime"] = lap["SessionTime"] + session_offset
        for name, values in lap.items():
            chunks.setdefault(name, []).append(values)
        observations.append(
            _observation(
                ordinal,
                frame,
                frame + len(lap["SessionTime"]),
                session_offset,
                duration,
            )
        )
        frame += len(lap["SessionTime"])
        session_offset += duration + 0.5
    return (
        {name: np.concatenate(values) for name, values in chunks.items()},
        tuple(observations),
    )


def test_resampling_uses_one_exact_fixed_distance_grid():
    channels, observations = _synthetic_session()

    laps = resample_clean_laps(
        channels,
        observations,
        track_length_m=TRACK_LENGTH_M,
        config=DrivingAnalysisConfig(grid_step_m=2.0),
    )

    assert len(laps) == 12
    assert np.array_equal(laps[0].distance_m, np.arange(0.0, 1_202.0, 2.0))
    assert all(np.array_equal(item.distance_m, laps[0].distance_m) for item in laps)
    assert all(item.elapsed_time_s[0] == 0.0 for item in laps)
    assert all(item.elapsed_time_s[-1] == item.duration_s for item in laps)
    assert not laps[0].distance_m.flags.writeable


def test_reference_is_real_representative_lap_from_fastest_group():
    channels, observations = _synthetic_session()
    laps = resample_clean_laps(
        channels, observations, track_length_m=TRACK_LENGTH_M
    )

    reference = select_reference_lap(laps)

    assert reference.lap_ordinal in reference.fastest_group_lap_ordinals
    assert set(reference.fastest_group_lap_ordinals).issubset(set(range(1, 7)))
    assert reference.duration_spread_fraction == 0.0
    assert reference.trace_median_absolute_error_s < 1e-12


def test_analysis_detects_segments_deltas_and_three_evidence_rules():
    channels, observations = _synthetic_session()

    report = analyze_driving(
        channels, observations, track_length_m=TRACK_LENGTH_M
    )

    assert report.status == "READY"
    assert report.refusal_reasons == ()
    assert len(report.corners) == 3
    assert len(report.corner_metrics) == len(report.corners) * len(observations)
    assert len(report.delta_closures) == len(observations)
    assert all(item.closed for item in report.delta_closures)
    assert all(abs(item.residual_s) <= 1e-12 for item in report.delta_closures)
    for closure in report.delta_closures:
        assert abs(
            closure.summed_window_delta_s - closure.actual_lap_delta_s
        ) <= closure.tolerance_s

    assert report.corners[0].accounting_start_m == 0.0
    assert report.corners[-1].carry_end_m == TRACK_LENGTH_M
    assert all(
        left.carry_end_m == right.accounting_start_m
        for left, right in zip(report.corners[:-1], report.corners[1:], strict=True)
    )
    assert all(
        abs(
            item.accounted_window_delta_s
            - (item.approach_delta_s + item.local_delta_s + item.carry_delta_s)
        )
        <= 1e-12
        for item in report.corner_metrics
    )
    assert {item.diagnosis for item in report.diagnoses} == {
        "LONG_COAST",
        "LATE_BRAKING_HURTS_EXIT",
        "THROTTLE_SECOND_LIFT",
    }

    diagnoses = {item.diagnosis: item for item in report.diagnoses}
    assert diagnoses["LONG_COAST"].evidence_lap_ordinals == (7, 8)
    assert diagnoses["LATE_BRAKING_HURTS_EXIT"].evidence_lap_ordinals == (9, 10)
    assert diagnoses["THROTTLE_SECOND_LIFT"].evidence_lap_ordinals == (11, 12)
    assert all(item.claim_level == "descriptive" for item in report.diagnoses)
    assert all(item.practice_only for item in report.diagnoses)
    assert all(item.comparisons for item in report.diagnoses)
    assert all(item.estimated_loss_median_s > 0 for item in report.diagnoses)

    reference_ordinal = report.reference.lap_ordinal
    reference_metrics = [
        item for item in report.corner_metrics if item.lap_ordinal == reference_ordinal
    ]
    assert len(reference_metrics) == 3
    assert all(abs(item.local_delta_s) < 1e-12 for item in reference_metrics)
    assert all(abs(item.carry_delta_s) < 1e-12 for item in reference_metrics)
    assert any(item.lap_delta_s > 0.0 for item in report.lap_summaries)


def test_analysis_refuses_too_few_clean_laps_without_partial_output():
    channels, observations = _synthetic_session()

    report = analyze_driving(
        channels, observations[:2], track_length_m=TRACK_LENGTH_M
    )

    assert report.status == "REFUSED"
    assert report.refusal_reasons == ("INSUFFICIENT_CLEAN_LAPS:2<3",)
    assert report.laps == ()
    assert report.corner_metrics == ()
    assert report.diagnoses == ()


@pytest.mark.parametrize("pickup_m", [240, 230], ids=["at-release", "overlap"])
def test_zero_coast_reference_preserves_repeated_long_coast_diagnosis(pickup_m):
    channels, observations = _synthetic_session()
    for lap in observations[:6]:
        channels["Throttle"][lap.start_frame + pickup_m : lap.start_frame + 270] = 1.0

    report = analyze_driving(channels, observations, track_length_m=TRACK_LENGTH_M)

    assert report.status == "READY"
    reference = next(
        item
        for item in report.corner_metrics
        if item.corner_id == "C01" and item.lap_ordinal == report.reference.lap_ordinal
    )
    assert reference.brake_release_m == 240.0
    assert reference.throttle_pickup_m == pickup_m
    assert reference.coast_distance_m == 0.0
    diagnosis = next(item for item in report.diagnoses if item.diagnosis == "LONG_COAST")
    assert diagnosis.evidence_lap_ordinals == (7, 8)
    comparison = next(item for item in diagnosis.comparisons if item.metric == "coast_distance_m")
    assert comparison.reference_value == 0.0
    assert comparison.evidence_median == 74.0


@pytest.mark.parametrize("missing_event", ["brake", "throttle"])
def test_unobserved_coast_endpoint_stays_unavailable(missing_event):
    channels, observations = _synthetic_session()
    # One non-reference lap loses an event; the other laps still establish
    # the corner and its reference trace.
    lap = observations[-1]
    channel = "Brake" if missing_event == "brake" else "Throttle"
    channels[channel][lap.start_frame : lap.start_frame + 530] = 0.0

    report = analyze_driving(channels, observations, track_length_m=TRACK_LENGTH_M)

    assert report.status == "READY"
    metric = next(
        item
        for item in report.corner_metrics
        if item.corner_id == "C01" and item.lap_ordinal == lap.ordinal
    )
    if missing_event == "brake":
        assert metric.brake_release_m is None
    else:
        assert metric.throttle_pickup_m is None
    assert metric.coast_distance_m is None


@pytest.mark.parametrize("dab_start_m", [430, 1140], ids=["between-corners", "after-last-corner"])
def test_skipped_straight_brake_dab_preserves_lap_analysis(dab_start_m):
    channels, observations = _synthetic_session()
    baseline = analyze_driving(channels, observations, track_length_m=TRACK_LENGTH_M)
    for lap in observations:
        channels["Brake"][
            lap.start_frame + dab_start_m : lap.start_frame + dab_start_m + 15
        ] = 0.2

    report = analyze_driving(channels, observations, track_length_m=TRACK_LENGTH_M)

    assert report.status == "READY"
    assert len(report.corners) == len(baseline.corners) == 3
    assert report.diagnoses == baseline.diagnoses
    assert report.corners[0].accounting_start_m == 0.0
    assert report.corners[-1].carry_end_m == TRACK_LENGTH_M
    assert all(
        left.carry_end_m == right.accounting_start_m
        for left, right in zip(report.corners[:-1], report.corners[1:], strict=True)
    )
    assert all(item.closed and abs(item.residual_s) <= 1e-12 for item in report.delta_closures)


def test_braking_across_finish_line_preserves_full_lap_accounting():
    channels, observations = _synthetic_session()
    shifted_observations = []
    for lap in observations:
        start, end = lap.start_frame, lap.end_frame_exclusive
        # Shift the first braking event across zero distance, preserving one
        # complete periodic lap and reconstructing its time from the speed.
        for name in ("Speed", "Throttle", "Brake", "SteeringWheelAngle"):
            shifted = np.roll(channels[name][start : end - 1], -200)
            channels[name][start:end] = np.r_[shifted, shifted[0]]
        speed = channels["Speed"][start:end]
        elapsed = np.r_[0.0, np.cumsum(1.0 / ((speed[:-1] + speed[1:]) / 2.0))]
        channels["SessionTime"][start:end] = lap.start_time_s + elapsed
        shifted_observations.append(
            replace(
                lap,
                duration_s=float(elapsed[-1]),
                end_time_s=lap.start_time_s + float(elapsed[-1]),
            )
        )

    report = analyze_driving(
        channels, shifted_observations, track_length_m=TRACK_LENGTH_M
    )

    assert report.status == "READY"
    assert len(report.corners) == 3
    assert report.corners[0].brake_start_m == 0.0
    assert report.corners[-1].carry_end_m == TRACK_LENGTH_M
    assert len(report.delta_closures) == len(observations)
    assert any(item.actual_lap_delta_s > 0.0 for item in report.delta_closures)
    assert all(item.closed and abs(item.residual_s) <= 1e-12 for item in report.delta_closures)


def test_analysis_refuses_non_reproducible_fastest_group():
    channels, observations = _synthetic_session()
    selected = list(observations[:3])
    first = selected[0]
    first_time = channels["SessionTime"][first.start_frame : first.end_frame_exclusive]
    start_time = float(first_time[0])
    first_time[:] = start_time + (first_time - start_time) * 0.80
    new_duration = first.duration_s * 0.80
    selected[0] = replace(
        first,
        duration_s=new_duration,
        end_time_s=first.start_time_s + new_duration,
    )

    report = analyze_driving(
        channels, selected, track_length_m=TRACK_LENGTH_M
    )

    assert report.status == "REFUSED"
    assert report.refusal_reasons[0].startswith(
        "INCONSISTENT_TELEMETRY:fastest-lap group is not reproducible"
    )
    assert report.diagnoses == ()


def test_analysis_refuses_missing_control_channel():
    channels, observations = _synthetic_session()
    del channels["Brake"]

    report = analyze_driving(
        channels, observations, track_length_m=TRACK_LENGTH_M
    )

    assert report.status == "REFUSED"
    assert report.refusal_reasons == ("MISSING_CHANNELS:Brake",)
    assert report.diagnoses == ()


def test_analysis_refuses_malformed_control_channel():
    channels, observations = _synthetic_session()
    channels["Brake"] = channels["Brake"][:, np.newaxis]

    report = analyze_driving(
        channels, observations, track_length_m=TRACK_LENGTH_M
    )

    assert report.status == "REFUSED"
    assert report.refusal_reasons == (
        "INCONSISTENT_TELEMETRY:driving channels must be one-dimensional "
        "numeric arrays: Brake",
    )
    assert report.diagnoses == ()


def test_analysis_refuses_a_non_closing_corner_partition(monkeypatch):
    channels, observations = _synthetic_session()
    accepted = analyze_driving(
        channels, observations, track_length_m=TRACK_LENGTH_M
    )
    broken = (
        replace(accepted.corners[0], accounting_start_m=10.0),
        *accepted.corners[1:],
    )

    monkeypatch.setattr(
        driving_module,
        "detect_corner_segments",
        lambda laps, reference, config: broken,
    )
    report = analyze_driving(
        channels, observations, track_length_m=TRACK_LENGTH_M
    )

    assert report.status == "REFUSED"
    assert report.refusal_reasons[0].startswith("NON_CLOSING_CORNER_PARTITION:")
    assert report.delta_closures == ()
    assert report.diagnoses == ()


def test_report_serialization_excludes_large_traces_by_default():
    channels, observations = _synthetic_session()
    report = analyze_driving(
        channels, observations, track_length_m=TRACK_LENGTH_M
    )

    compact = report.to_dict()
    with_traces = report.to_dict(include_traces=True)

    assert "laps" not in compact
    assert len(with_traces["laps"]) == 12
    json.dumps(compact)
    json.dumps(with_traces)
