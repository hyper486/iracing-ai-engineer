from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

import iracing_ai_engineer.corner_cards as _PACKAGE_MODULE
from iracing_ai_engineer.adapters import open_ibt_telemetry
from iracing_ai_engineer.driving_model_replay import build_driving_model_replay

_SPEC = importlib.util.spec_from_file_location(
    "build_offline_corner_cards",
    Path("scripts/build_offline_corner_cards.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

CornerCardError = _MODULE.CornerCardError
build_corner_cards = _MODULE.build_corner_cards
canonical_sha256 = _MODULE.canonical_sha256

_REPLAY_BINDING_KEYS = (
    "capabilities",
    "contract_version",
    "driving_context",
    "driving_context_sha256",
    "event_receipt",
    "input_evidence",
    "input_kind",
    "input_provenance_sha256",
    "lap_receipt",
    "model_output",
    "model_output_sha256",
    "model_semantic_sha256",
    "normalized_input_receipt",
    "pipeline",
    "quality_gate",
    "readiness_status",
    "recommendations",
    "semantic_input_receipt",
    "series_evidence",
)


def test_compatibility_wrapper_reexports_the_complete_package_surface() -> None:
    exported = {
        name: value
        for name, value in vars(_PACKAGE_MODULE).items()
        if not name.startswith("__")
    }

    assert exported
    assert all(vars(_MODULE).get(name) is value for name, value in exported.items())
    assert _MODULE.build_corner_cards.__module__ == "iracing_ai_engineer.corner_cards"


def _metric(corner_id: str, lap: int, loss: float, start_delta: float) -> dict[str, object]:
    local = loss * 0.6
    carry = loss - local
    return {
        "accounted_window_delta_s": loss,
        "apex_m": {"C01": 180.0, "C02": 520.0, "C08": 900.0}[corner_id],
        "apex_speed_mps": 25.0,
        "approach_delta_s": 0.0,
        "brake_onset_m": {"C01": 100.0, "C02": 400.0, "C08": 800.0}[corner_id],
        "brake_release_m": {"C01": 150.0, "C02": 470.0, "C08": 860.0}[corner_id],
        "carry_delta_s": carry,
        "coast_distance_m": 10.0,
        "corner_id": corner_id,
        "delta_at_accounting_start_s": start_delta,
        "delta_at_apex_s": start_delta + local / 2,
        "delta_at_entry_s": start_delta,
        "delta_at_exit_s": start_delta + local,
        "entry_speed_mps": 50.0,
        "exit_speed_mps": 45.0,
        "lap_ordinal": lap,
        "local_delta_s": local,
        "second_lift": False,
        "throttle_pickup_m": {"C01": 210.0, "C02": 560.0, "C08": 930.0}[corner_id],
        "total_segment_delta_s": local + carry,
    }


def _rehash(replay: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(replay)
    event_receipt = result["event_receipt"]
    assert isinstance(event_receipt, dict)
    event_receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in event_receipt.items() if key != "receipt_sha256"}
    )
    input_evidence = result["input_evidence"]
    assert isinstance(input_evidence, dict)
    context = result["driving_context"]
    assert isinstance(context, dict)
    context["source_binding_sha256"] = canonical_sha256(input_evidence)
    context["context_sha256"] = canonical_sha256(
        {key: value for key, value in context.items() if key != "context_sha256"}
    )
    result["driving_context_sha256"] = context["context_sha256"]
    pipeline = result["pipeline"]
    assert isinstance(pipeline, dict)
    pipeline["pipeline_sha256"] = canonical_sha256(
        {key: value for key, value in pipeline.items() if key != "pipeline_sha256"}
    )
    result["input_provenance_sha256"] = canonical_sha256(
        {
            "driving_context": context,
            "event_receipt": result["event_receipt"],
            "input_evidence": input_evidence,
            "input_kind": result["input_kind"],
            "normalized_input_receipt": result["normalized_input_receipt"],
        }
    )
    prefix = str(result["input_provenance_sha256"])
    model = result["model_output"]
    assert isinstance(model, dict)
    eligible = model["eligible_lap_ordinals"]
    assert isinstance(eligible, list)
    capabilities = result["capabilities"]
    assert isinstance(capabilities, dict)
    driving_capability = capabilities["driving_model_shadow"]
    assert isinstance(driving_capability, dict)
    driving_capability["evidence_ids"] = [
        f"{prefix}:distance-wrap-v2:lap:{ordinal}" for ordinal in eligible
    ]
    diagnoses = model["diagnoses"]
    assert isinstance(diagnoses, list)
    diagnosis_by_key = {(item["corner_id"], item["diagnosis"]): item for item in diagnoses}
    recommendations = result["recommendations"]
    assert isinstance(recommendations, list)
    for recommendation in recommendations:
        key = (recommendation.get("corner_id"), recommendation.get("diagnosis"))
        diagnosis = diagnosis_by_key.get(key)
        if diagnosis is None:
            continue
        recommendation["evidence_lap_ids"] = [
            f"{prefix}:distance-wrap-v2:lap:{ordinal}"
            for ordinal in diagnosis["evidence_lap_ordinals"]
        ]
        recommendation["counterexample_lap_ids"] = [
            f"{prefix}:distance-wrap-v2:lap:{ordinal}"
            for ordinal in diagnosis["counterexample_lap_ordinals"]
        ]
    result["model_output_sha256"] = canonical_sha256(model)
    result["model_semantic_sha256"] = canonical_sha256(
        {
            "driving_context": {
                "contract_version": context["contract_version"],
                "source_field": context["source_field"],
                "track_length_mm": context["track_length_mm"],
            },
            "lap_receipt": result["lap_receipt"],
            "model_output": model,
            "pipeline": pipeline,
            "quality_gate": result["quality_gate"],
            "readiness_status": result["readiness_status"],
            "semantic_input_receipt": result["semantic_input_receipt"],
        }
    )
    result["driving_replay_sha256"] = canonical_sha256(
        {key: result[key] for key in _REPLAY_BINDING_KEYS}
    )
    return result


def _replay() -> dict[str, object]:
    eligible = [2, 4, 5, 11]
    reference = 11
    corner_losses = {
        2: {"C01": 0.15, "C02": 0.25, "C08": 0.40},
        4: {"C01": 0.10, "C02": 0.20, "C08": 0.30},
        # This lap loses time in C01 but is a counterexample to the LONG_COAST
        # pattern.  Loss evidence and action evidence must therefore stay separate.
        5: {"C01": 0.08, "C02": 0.15, "C08": 0.20},
        11: {"C01": 0.0, "C02": 0.0, "C08": 0.0},
    }
    corners = [
        {
            "accounting_start_m": 0.0,
            "apex_m": 180.0,
            "approach_start_m": 20.0,
            "brake_end_m": 150.0,
            "brake_start_m": 100.0,
            "carry_end_m": 400.0,
            "corner_id": "C01",
            "exit_m": 250.0,
        },
        {
            "accounting_start_m": 400.0,
            "apex_m": 520.0,
            "approach_start_m": 320.0,
            "brake_end_m": 470.0,
            "brake_start_m": 400.0,
            "carry_end_m": 800.0,
            "corner_id": "C02",
            "exit_m": 600.0,
        },
        {
            "accounting_start_m": 800.0,
            "apex_m": 900.0,
            "approach_start_m": 720.0,
            "brake_end_m": 860.0,
            "brake_start_m": 800.0,
            "carry_end_m": 1_000.0,
            "corner_id": "C08",
            "exit_m": 950.0,
        },
    ]
    metrics: list[dict[str, object]] = []
    lap_deltas: dict[int, float] = {}
    for lap in eligible:
        start = 0.0
        for corner_id in ("C01", "C02", "C08"):
            loss = corner_losses[lap][corner_id]
            metrics.append(_metric(corner_id, lap, loss, start))
            start += loss
        lap_deltas[lap] = start

    diagnosis = {
        "action": (
            "Practice moving the brake point later in small steps while keeping one "
            "continuous release-to-throttle transition."
        ),
        "claim_level": "descriptive",
        "comparisons": [
            {
                "difference": -12.0,
                "evidence_median": 88.0,
                "metric": "brake_onset_m",
                "reference_value": 100.0,
                "unit": "m",
            },
            {
                "difference": 15.0,
                "evidence_median": 25.0,
                "metric": "coast_distance_m",
                "reference_value": 10.0,
                "unit": "m",
            },
            {
                "difference": 0.0,
                "evidence_median": 25.0,
                "metric": "apex_speed_mps",
                "reference_value": 25.0,
                "unit": "m/s",
            },
            {
                "difference": 0.0,
                "evidence_median": 45.0,
                "metric": "exit_speed_mps",
                "reference_value": 45.0,
                "unit": "m/s",
            },
            {
                "difference": 0.125,
                "evidence_median": 0.125,
                "metric": "total_segment_delta_s",
                "reference_value": 0.0,
                "unit": "s",
            },
        ],
        "confidence": "medium",
        "corner_id": "C01",
        "counterexample_lap_ordinals": [5],
        "diagnosis": "LONG_COAST",
        "estimated_loss_median_s": 0.10,
        "evidence_lap_ordinals": [2, 4],
        "expected_gain_range_s": [0.04, 0.10],
        "practice_only": True,
    }
    model = {
        "algorithm_version": "distance-driving-v1",
        "corner_metrics": metrics,
        "corners": corners,
        "delta_closures": [
            {
                "actual_lap_delta_s": lap_deltas[lap],
                "closed": True,
                "lap_ordinal": lap,
                "residual_s": 0.0,
                "summed_window_delta_s": lap_deltas[lap],
                "tolerance_s": 1e-6,
            }
            for lap in eligible
        ],
        "diagnoses": [diagnosis],
        "eligible_lap_ordinals": eligible,
        "grid_step_m": 1.0,
        "lap_summaries": [
            {
                "duration_s": 100.0 + lap_deltas[lap],
                "in_fastest_group": lap in {5, 11},
                "is_reference": lap == reference,
                "lap_delta_s": lap_deltas[lap],
                "lap_ordinal": lap,
            }
            for lap in eligible
        ],
        "reference": {
            "duration_spread_fraction": 0.01,
            "fastest_group_lap_ordinals": [5, 11],
            "lap_ordinal": reference,
            "trace_median_absolute_error_s": 0.01,
        },
        "refusal_reasons": [],
        "status": "READY",
        "track_length_m": 1_000.0,
    }
    input_evidence = {
        "authenticity_status": "HASHED_LOCAL_FILE_NOT_AUTHENTICATED",
        "byte_size": 1_000,
        "completion_status": "COMPLETE",
        "record_count": 10_000,
        "session_id": "fixture-session",
        "source_id": "fixture-source",
        "source_kind": "IBT_OFFLINE",
        "source_sha256": "3" * 64,
        "tick_rate_hz": 60,
    }
    context = {
        "availability": "AVAILABLE",
        "context_sha256": "0" * 64,
        "contract_version": "track-context-v1",
        "provenance": "IBT_SAME_HANDLE_SESSION_INFO",
        "source_binding_sha256": "0" * 64,
        "source_field": "WeekendInfo.TrackLength",
        "status": "VERIFIED",
        "track_length_mm": 1_000_000,
    }
    pipeline = {
        "driving_algorithm_version": "distance-driving-v1",
        "driving_config": {
            "apex_search_after_brake_m": 240.0,
            "approach_window_m": 80.0,
            "brake_release_threshold": 0.03,
            "brake_threshold": 0.08,
            "event_position_difference_m": 8.0,
            "event_sustain_m": 4.0,
            "fastest_group_fraction": 0.4,
            "grid_step_m": 1.0,
            "lap_delta_closure_tolerance_s": 1e-6,
            "long_coast_difference_m": 15.0,
            "max_brake_gap_m": 12.0,
            "max_corner_exit_after_apex_m": 180.0,
            "max_reference_duration_spread_fraction": 0.03,
            "min_braking_zone_m": 8.0,
            "min_clean_laps": 3,
            "min_evidence_laps": 2,
            "min_reference_group_laps": 3,
            "minimum_carry_loss_s": 0.03,
            "minimum_loss_s": 0.04,
            "second_lift_threshold": 0.25,
            "speed_difference_mps": 0.4,
            "throttle_pickup_threshold": 0.5,
        },
        "event_contract_version": "telemetry-events-v1",
        "feature_pipeline_version": "normalized-lap-driving-v1",
        "lap_algorithm_version": "distance-wrap-v2",
        "normalization": {
            "opponent_error_policy": "degrade",
            "profile_version": "normalized-sdk-adapter-v3",
            "stale_after_us": 500_000,
        },
        "normalized_telemetry_contract_version": "normalized-telemetry-v3",
        "pipeline_sha256": "0" * 64,
        "semantic_input_contract_version": "driving-semantic-input-v1",
        "tick_rate_hz": 60,
    }

    def unavailable(reasons: list[str], blocked_claims: list[str]) -> dict[str, object]:
        return {
            "blocked_claims": blocked_claims,
            "confidence": "NONE",
            "contract_version": "inference-capability-v1",
            "estimate_available": False,
            "provenance": "UNKNOWN",
            "reasons": reasons,
            "status": "SKIP",
        }

    replay: dict[str, object] = {
        "capabilities": {
            "curb_guidance": unavailable(["CURB_GEOMETRY_NOT_MODELED"], ["CURB_RECOMMENDATION"]),
            "current_tire_wear": unavailable(
                ["CURRENT_STINT_TIRE_WEAR_MODEL_NOT_IMPLEMENTED"],
                ["CURRENT_TIRE_WEAR_CLAIM"],
            ),
            "driving_model_shadow": {"evidence_ids": [], "reasons": [], "status": "PASS"},
            "personalized_coaching": unavailable(
                [
                    "CONDITION_COHORT_NOT_ATTACHED",
                    "MATCHED_CONTEXT_HISTORY_UNAVAILABLE",
                    "HUMAN_CORNER_LABELS_MISSING",
                ],
                ["PERSONALIZED_ACTION", "CAUSAL_GAIN_CLAIM", "TRAIL_BRAKING_CLAIM"],
            ),
            "race_coaching": {
                "reasons": [
                    "SHADOW_ONLY",
                    "PERSONALIZED_COACHING_UNAVAILABLE",
                    "TRAFFIC_MODEL_NOT_IMPLEMENTED",
                ],
                "status": "BLOCKED",
            },
            "traffic_model": unavailable(
                ["TRAFFIC_MODEL_NOT_IMPLEMENTED"], ["REJOIN_TRAFFIC_CLAIM"]
            ),
        },
        "contract_version": "driving-model-replay-v1",
        "driving_context": context,
        "driving_context_sha256": "0" * 64,
        "event_receipt": {
            "accepted_sample_count": 10_000,
            "config_sha256": "4" * 64,
            "contract_version": "telemetry-events-v1",
            "event_count": 0,
            "event_kind_counts": {},
            "events_sha256": "5" * 64,
            "receipt_sha256": "0" * 64,
            "rejected_sample_count": 0,
            "sample_count": 10_000,
            "session_epoch_count": 1,
            "source_epoch_count": 1,
        },
        "input_evidence": input_evidence,
        "input_kind": "ibt",
        "input_provenance_sha256": "0" * 64,
        "lap_receipt": {
            "algorithm_version": "distance-wrap-v2",
            "clean_driving_lap_count": 4,
            "cleanliness_observable_lap_count": 4,
            "lap_count": 4,
            "laps_sha256": "6" * 64,
            "modeled_sample_count": 10_000,
            "quality_complete_lap_count": 4,
            "structurally_complete_lap_count": 4,
        },
        "model_output": model,
        "model_output_sha256": "0" * 64,
        "model_semantic_sha256": "0" * 64,
        "normalized_input_receipt": {
            "contract_version": "normalized-telemetry-v3",
            "sample_count": 10_000,
            "samples_sha256": "2" * 64,
        },
        "pipeline": pipeline,
        "quality_gate": {"reasons": [], "status": "PASS"},
        "readiness_status": "PASS",
        "recommendations": [
            {
                "action": diagnosis["action"],
                "claim_level": "descriptive",
                "confidence": "MEDIUM",
                "confidence_basis": {
                    "causal_validity": "NOT_CLAIMED",
                    "external_validity": "UNKNOWN",
                },
                "corner_id": "C01",
                "counterexample_lap_ids": [],
                "diagnosis": "LONG_COAST",
                "estimated_loss_us": 100_000,
                "evidence_lap_ids": [],
                "executable": False,
                "expected_gain_range_us": [40_000, 100_000],
                "kind": "DRIVING_CANDIDATE",
                "metric_comparisons": diagnosis["comparisons"],
                "practice_only": True,
                "recommendation_id": "driving:C01:LONG_COAST",
                "status": "SHADOW_ONLY",
            }
        ],
        "semantic_input_receipt": {
            "channels": [
                "SessionNum",
                "SessionTick",
                "SessionTime",
                "Lap",
                "LapCompleted",
                "LapDistPct",
                "Speed",
                "Throttle",
                "Brake",
                "SteeringWheelAngle",
                "OnPitRoad",
                "PlayerTrackSurface",
                "PlayerIncidentCount",
                "QualityStatus",
                "DroppedTicks",
                "QualityIssues",
            ],
            "contract_version": "driving-semantic-input-v1",
            "sample_count": 10_000,
            "samples_sha256": "8" * 64,
        },
        "series_evidence": {
            "analysis_refusal_reasons": [],
            "degraded_sample_count": 0,
            "incident_regression_channels": [],
            "incident_source_field": "PlayerCarMyIncidentCount",
            "missing_channel_sample_counts": {},
            "modeled_sample_count": 10_000,
            "normalized_dropped_tick_count": 0,
            "quality_issue_counts": {},
            "segmentation_error": None,
        },
        "driving_replay_sha256": "0" * 64,
    }
    return _rehash(replay)


def test_builds_ranked_top_three_without_inventing_actions():
    report = build_corner_cards(_replay())

    assert report["status"] == "SHADOW_ONLY"
    assert report["advisor_only"] is True
    assert report["ranking"]["card_count"] == 3
    cards = report["cards"]
    assert [card["corner_id"] for card in cards] == ["C08", "C02", "C01"]
    assert [card["loss_summary"]["median_accounted_window_delta_s"] for card in cards] == [
        pytest.approx(0.30),
        pytest.approx(0.20),
        pytest.approx(0.10),
    ]
    for card in cards:
        assert card["claim_level"] == "descriptive"
        assert card["confidence"] == "LOW"
        assert card["corner_identity_status"] == "CANDIDATE_NOT_GOLDEN"
        assert card["status"] == "SHADOW_ONLY"
        assert card["executable"] is False
        assert card["practice_only"] is True
        assert all(item["phase_closed"] for item in card["per_lap_evidence"])

    for card in cards[:2]:
        assert card["action"] is None
        assert card["action_evidence_lap_ids"] == []
        assert card["action_counterexample_lap_ids"] == []
        assert card["diagnosis"] is None
        assert card["expected_gain_range_s"] is None
        assert card["suppress_reasons"] == [
            "NO_SUPPORTED_ACTION_DIAGNOSIS",
            "WAIT_CONDITION_DATA",
            "WAIT_HUMAN_LABELS",
            "CANDIDATE_NOT_GOLDEN",
        ]
    assert cards[2]["diagnosis"] == "LONG_COAST"
    assert cards[2]["action"] == (
        "Practice moving the brake point later in small steps while keeping one "
        "continuous release-to-throttle transition."
    )
    assert [item.rsplit(":", 1)[-1] for item in cards[2]["loss_evidence_lap_ids"]] == [
        "2",
        "4",
        "5",
    ]
    assert cards[2]["loss_counterexample_lap_ids"] == []
    assert [item.rsplit(":", 1)[-1] for item in cards[2]["action_evidence_lap_ids"]] == [
        "2",
        "4",
    ]
    assert [item.rsplit(":", 1)[-1] for item in cards[2]["action_counterexample_lap_ids"]] == ["5"]


def test_does_not_fill_card_quota_with_unsupported_windows():
    replay = _replay()
    model = replay["model_output"]
    assert isinstance(model, dict)
    metrics = model["corner_metrics"]
    assert isinstance(metrics, list)
    for metric in metrics:
        if metric["corner_id"] in {"C01", "C02"} and metric["lap_ordinal"] != 11:
            metric["accounted_window_delta_s"] = 0.01
            metric["local_delta_s"] = 0.006
            metric["carry_delta_s"] = 0.004
            metric["total_segment_delta_s"] = 0.01
    for lap in (2, 4, 5):
        lap_metrics = [metric for metric in metrics if metric["lap_ordinal"] == lap]
        total = sum(float(metric["accounted_window_delta_s"]) for metric in lap_metrics)
        for summary in model["lap_summaries"]:
            if summary["lap_ordinal"] == lap:
                summary["lap_delta_s"] = total
                summary["duration_s"] = 100.0 + total
        for closure in model["delta_closures"]:
            if closure["lap_ordinal"] == lap:
                closure["actual_lap_delta_s"] = total
                closure["summed_window_delta_s"] = total
    replay = _rehash(replay)

    report = build_corner_cards(replay)

    assert [card["corner_id"] for card in report["cards"]] == ["C08"]
    assert report["ranking"]["card_count"] == 1


@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("model_output_sha256", "MODEL_OUTPUT_SHA256_MISMATCH"),
        ("driving_replay_sha256", "DRIVING_REPLAY_SHA256_MISMATCH"),
    ],
)
def test_hash_tampering_fails_closed(field: str, expected_code: str):
    replay = _replay()
    replay[field] = "f" * 64
    if field == "model_output_sha256":
        replay["driving_replay_sha256"] = canonical_sha256(
            {key: replay[key] for key in _REPLAY_BINDING_KEYS}
        )

    with pytest.raises(CornerCardError) as error:
        build_corner_cards(replay)

    assert error.value.code == expected_code


def test_metric_tamper_fails_model_digest_even_if_outer_receipt_is_rehashed():
    replay = _replay()
    model = replay["model_output"]
    assert isinstance(model, dict)
    metrics = model["corner_metrics"]
    assert isinstance(metrics, list)
    metrics[0]["accounted_window_delta_s"] = 9.0
    replay["driving_replay_sha256"] = canonical_sha256(
        {key: replay[key] for key in _REPLAY_BINDING_KEYS}
    )

    with pytest.raises(CornerCardError) as error:
        build_corner_cards(replay)

    assert error.value.code == "MODEL_OUTPUT_SHA256_MISMATCH"


def test_non_pass_gate_fails_even_with_self_consistent_hashes():
    replay = _replay()
    replay["quality_gate"] = {"reasons": ["WAIT"], "status": "DEGRADED"}
    replay["readiness_status"] = "WAIT_DRIVING_DATA"
    replay = _rehash(replay)

    with pytest.raises(CornerCardError) as error:
        build_corner_cards(replay)

    assert error.value.code == "QUALITY_GATE_NOT_PASS"


def test_false_lap_closure_fails_even_with_self_consistent_hashes():
    replay = _replay()
    model = replay["model_output"]
    assert isinstance(model, dict)
    closures = model["delta_closures"]
    assert isinstance(closures, list)
    closures[0]["closed"] = False
    replay = _rehash(replay)

    with pytest.raises(CornerCardError) as error:
        build_corner_cards(replay)

    assert error.value.code == "LAP_DELTA_CLOSURE_FAILED"


def test_lap_closure_cannot_relax_the_pipeline_tolerance():
    replay = _replay()
    model = replay["model_output"]
    assert isinstance(model, dict)
    closures = model["delta_closures"]
    assert isinstance(closures, list)
    closures[0]["tolerance_s"] = 1.0
    replay = _rehash(replay)

    with pytest.raises(CornerCardError) as error:
        build_corner_cards(replay)

    assert error.value.code == "LAP_DELTA_CLOSURE_FAILED"


def test_lap_closure_cannot_self_consistently_relax_both_tolerances():
    replay = _replay()
    pipeline = replay["pipeline"]
    model = replay["model_output"]
    assert isinstance(pipeline, dict) and isinstance(model, dict)
    config = pipeline["driving_config"]
    closures = model["delta_closures"]
    assert isinstance(config, dict) and isinstance(closures, list)
    config["lap_delta_closure_tolerance_s"] = 100.0
    for closure in closures:
        closure["tolerance_s"] = 100.0
    replay = _rehash(replay)

    with pytest.raises(CornerCardError) as error:
        build_corner_cards(replay)

    assert error.value.code == "LAP_DELTA_CLOSURE_FAILED"


def test_lap_delta_is_recomputed_from_reference_duration():
    replay = _replay()
    model = replay["model_output"]
    assert isinstance(model, dict)
    summaries = model["lap_summaries"]
    assert isinstance(summaries, list)
    summaries[0]["duration_s"] = float(summaries[0]["duration_s"]) + 1.0
    replay = _rehash(replay)

    with pytest.raises(CornerCardError) as error:
        build_corner_cards(replay)

    assert error.value.code == "LAP_DELTA_CLOSURE_FAILED"


def test_unknown_diagnosis_is_rejected_even_after_complete_rehash():
    replay = _replay()
    model = replay["model_output"]
    assert isinstance(model, dict)
    diagnoses = model["diagnoses"]
    assert isinstance(diagnoses, list)
    diagnoses[0]["diagnosis"] = "ARBITRARY_UNSUPPORTED_RULE"
    diagnoses[0]["action"] = "Invented action."
    replay["recommendations"] = []
    replay = _rehash(replay)

    with pytest.raises(CornerCardError) as error:
        build_corner_cards(replay)

    assert error.value.code == "DIAGNOSIS_INVALID"


def test_diagnosis_without_bound_recommendation_does_not_emit_action():
    replay = _replay()
    replay["recommendations"] = []
    replay = _rehash(replay)

    card = next(item for item in build_corner_cards(replay)["cards"] if item["corner_id"] == "C01")

    assert card["action"] is None
    assert card["diagnosis"] is None
    assert card["action_evidence_lap_ids"] == []
    assert card["suppress_reasons"] == [
        "NO_SUPPORTED_ACTION_DIAGNOSIS",
        "WAIT_CONDITION_DATA",
        "WAIT_HUMAN_LABELS",
        "CANDIDATE_NOT_GOLDEN",
    ]


def test_metric_order_does_not_change_cards_and_repeated_output_is_deterministic():
    first_replay = _replay()
    second_replay = copy.deepcopy(first_replay)
    model = second_replay["model_output"]
    assert isinstance(model, dict)
    metrics = model["corner_metrics"]
    assert isinstance(metrics, list)
    metrics.reverse()
    second_replay = _rehash(second_replay)

    first = build_corner_cards(first_replay)
    repeated = build_corner_cards(first_replay)
    reordered = build_corner_cards(second_replay)

    assert first == repeated
    assert first["cards"] == reordered["cards"]
    assert json.dumps(first, allow_nan=False, sort_keys=True, separators=(",", ":")) == (
        json.dumps(repeated, allow_nan=False, sort_keys=True, separators=(",", ":"))
    )


def test_strict_json_rejects_duplicate_keys_and_nonfinite_values():
    with pytest.raises(CornerCardError) as duplicate:
        _MODULE._strict_json(b'{"x":1,"x":2}')
    with pytest.raises(CornerCardError) as nonfinite:
        _MODULE._strict_json(b'{"x":NaN}')

    assert duplicate.value.code == "INVALID_JSON"
    assert nonfinite.value.code == "INVALID_JSON"


def test_cli_writes_the_same_canonical_receipt_exclusively(tmp_path: Path, capfd):
    source = tmp_path / "driving-replay.json"
    output = tmp_path / "corner-cards.json"
    source.write_text(
        json.dumps(_replay(), allow_nan=False, sort_keys=True),
        encoding="utf-8",
    )

    assert _MODULE.main([str(source), "--output", str(output)]) == 0
    first = capfd.readouterr()

    assert first.err == ""
    assert output.read_text(encoding="utf-8") == first.out
    payload = json.loads(first.out)
    assert payload["corner_cards_sha256"] == canonical_sha256(
        {key: value for key, value in payload.items() if key != "corner_cards_sha256"}
    )

    assert _MODULE.main([str(source), "--output", str(output)]) == 3
    second = capfd.readouterr()
    assert second.out == ""
    assert second.err.startswith("OUTPUT_CREATE_FAILED:")


PUBLIC_MANIFEST = json.loads(Path("data/public_sources.json").read_text(encoding="utf-8"))
PUBLIC_SAMPLE = Path(PUBLIC_MANIFEST["assets"][0]["local_path"])
requires_public_ibt = pytest.mark.skipif(
    not PUBLIC_SAMPLE.is_file(), reason="REQUIRES_DATA: public Audi/Spa IBT absent"
)


@requires_public_ibt
def test_real_audi_spa_cards_surface_recurrent_losses_without_fabricated_actions():
    with open_ibt_telemetry(
        PUBLIC_SAMPLE,
        source_id="public-audi-r8-evo2-spa",
        session_id="public-fixture-2023-12-race",
    ) as run:
        replay = build_driving_model_replay(run)
    # Exercise the actual script boundary: CLI JSON serialization normalizes
    # dataclass tuple fields to JSON arrays before this verifier consumes them.
    replay = json.loads(json.dumps(replay, allow_nan=False, sort_keys=True))

    report = build_corner_cards(replay)
    cards = report["cards"]

    assert [card["corner_id"] for card in cards] == ["C08", "C02", "C01"]
    assert [
        round(card["loss_summary"]["median_accounted_window_delta_s"], 3) for card in cards
    ] == [0.294, 0.23, 0.163]
    assert [card["loss_summary"]["positive_lap_count"] for card in cards] == [5, 5, 4]
    assert [card["action"] is not None for card in cards] == [False, False, True]
    assert cards[2]["diagnosis"] == "LONG_COAST"
    assert all(card["executable"] is False for card in cards)
