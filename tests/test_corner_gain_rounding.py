"""Rounded descriptive gain estimates must stay inside observed loss bounds."""

from __future__ import annotations

import copy
import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

from iracing_ai_engineer import corner_cards, engineer_session
from iracing_ai_engineer.driving import (
    CornerLapMetrics,
    DrivingAnalysisConfig,
    generate_diagnoses,
)


def _reference_metric() -> CornerLapMetrics:
    return CornerLapMetrics(
        corner_id="C01", lap_ordinal=1,
        brake_onset_m=100.0, brake_release_m=120.0, entry_speed_mps=50.0,
        apex_m=150.0, apex_speed_mps=25.0, throttle_pickup_m=160.0,
        exit_speed_mps=40.0, coast_distance_m=5.0, second_lift=False,
        delta_at_accounting_start_s=0.0, delta_at_entry_s=0.0, delta_at_apex_s=0.0,
        delta_at_exit_s=0.0, approach_delta_s=0.0, local_delta_s=0.0,
        carry_delta_s=0.0, total_segment_delta_s=0.0, accounted_window_delta_s=0.0,
    )


@pytest.mark.parametrize("loss", [
    0.0, 0.0001, 0.0095, 0.01, 0.04, 0.04049, 0.04051,
    0.1245, 0.1249, 0.1234, 0.4, 0.9999, 1.2349999,
])
def test_generated_gain_bounds_never_exceed_unrounded_evidence(loss: float) -> None:
    reference = _reference_metric()
    evidence = [
        replace(
            reference, lap_ordinal=ordinal, brake_onset_m=80.0,
            brake_release_m=100.0, coast_distance_m=30.0,
            total_segment_delta_s=loss, accounted_window_delta_s=loss,
        )
        for ordinal in (2, 3, 4)
    ]
    diagnoses = generate_diagnoses(
        [reference, *evidence, replace(reference, lap_ordinal=5)],
        reference_lap_ordinal=1,
        config=DrivingAnalysisConfig(minimum_loss_s=0.0),
    )
    assert len(diagnoses) == 1
    diagnosis = diagnoses[0]
    assert diagnosis.diagnosis == "LONG_COAST"
    assert diagnosis.estimated_loss_median_s == loss
    low, high = diagnosis.expected_gain_range_s
    assert 0 <= low <= high <= loss
    assert high == min(loss, round(loss, 3))
    assert low == min(high, round(max(0.01, loss * 0.4), 3))
    assert diagnosis.evidence_lap_ordinals == (2, 3, 4)
    assert diagnosis.counterexample_lap_ordinals == (5,)
    assert diagnosis.claim_level == "descriptive" and diagnosis.practice_only is True


def _fixtures():
    path = Path(__file__).with_name("test_engineer_session.py")
    spec = importlib.util.spec_from_file_location("_corner_gain_session_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def long_coast_receipt(tmp_path_factory):
    fixtures = _fixtures()
    original = fixtures._profile
    kinds = iter(["baseline"] * 4 + ["long_coast"] * 3)

    def profile(_kind):
        return original(next(kinds))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fixtures, "_profile", profile)
        frames = fixtures._paired_frames()
    capture = tmp_path_factory.mktemp("corner-gain-rounding") / "synthetic.jsonl"
    fixtures._write_collector(capture, frames)
    source = engineer_session._build_source_components(
        capture, input_kind="collector", source_id=None, session_id=None,
        scenario=fixtures._scenario(), stale_after_s=1.0, opponent_error_policy="degrade",
    )
    diagnoses = source.driving_replay["model_output"]["diagnoses"]
    assert diagnoses and any(item["diagnosis"] == "LONG_COAST" for item in diagnoses)
    # This exact generated capture exposed the original producer/validator mismatch.
    assert any(round(item["estimated_loss_median_s"], 3) > item["estimated_loss_median_s"]
               for item in diagnoses)
    return engineer_session.build_engineer_session(
        capture, input_kind="collector", scenario=fixtures._scenario(),
        strategy_context=fixtures._context(source, decision_tick=int(frames[-1]["SessionTick"])),
        stale_after_s=1.0,
    )


def test_generated_long_coast_capture_builds_and_revalidates_receipt(long_coast_receipt) -> None:
    receipt = long_coast_receipt
    replay = receipt["components"]["driving_replay"]
    diagnoses = replay["model_output"]["diagnoses"]
    for diagnosis in diagnoses:
        low, high = diagnosis["expected_gain_range_s"]
        assert 0 <= low <= high <= diagnosis["estimated_loss_median_s"]
        assert diagnosis["claim_level"] == "descriptive"
        assert diagnosis["practice_only"] is True
    assert receipt["execution_mode"] == "SHADOW_ONLY"
    assert receipt["status"] == "WAIT_DATA"
    assert engineer_session.validate_engineer_session(receipt) == receipt


def test_validator_still_rejects_materially_inflated_gain(long_coast_receipt) -> None:
    replay = copy.deepcopy(long_coast_receipt["components"]["driving_replay"])
    diagnosis = replay["model_output"]["diagnoses"][0]
    diagnosis["expected_gain_range_s"][1] = diagnosis["estimated_loss_median_s"] + 0.0001
    replay["model_output_sha256"] = corner_cards.canonical_sha256(replay["model_output"])
    with pytest.raises(corner_cards.CornerCardError, match="expected gain range is invalid"):
        corner_cards._validate_model(
            replay, replay["driving_context"], replay["pipeline"],
            replay["pipeline"]["driving_config"]["min_evidence_laps"],
        )
