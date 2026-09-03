from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "build_offline_pit_plan",
    Path("scripts/build_offline_pit_plan.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

PitPlanError = _MODULE.PitPlanError
build_pit_plan = _MODULE.build_pit_plan
canonical_sha256 = _MODULE.canonical_sha256
main = _MODULE.main

_REPLAY_BINDING_KEYS = (
    "capabilities",
    "contract_version",
    "event_receipt",
    "input_evidence",
    "input_kind",
    "lap_receipt",
    "model_output",
    "model_output_sha256",
    "model_semantic_sha256",
    "normalized_input_receipt",
    "pipeline",
    "quality_gate",
    "recommendations",
    "scenario",
    "scenario_sha256",
    "series_evidence",
)


def _unavailable(reason: str, claim: str) -> dict[str, object]:
    return {
        "blocked_claims": [claim],
        "confidence": "NONE",
        "contract_version": "inference-capability-v1",
        "estimate_available": False,
        "provenance": "UNKNOWN",
        "reasons": [reason],
        "status": "SKIP",
    }


def _scenario_field(value: object) -> dict[str, object]:
    return {"provenance": "USER_RULE", "value": value}


def _rehash(replay: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(replay)
    scenario = result["scenario"]
    model = result["model_output"]
    pipeline = result["pipeline"]
    assert isinstance(scenario, dict)
    assert isinstance(model, dict)
    assert isinstance(pipeline, dict)
    pipeline["config_sha256"] = canonical_sha256(
        {key: value for key, value in pipeline.items() if key != "config_sha256"}
    )
    result["scenario_sha256"] = canonical_sha256(scenario)
    recommendations = result["recommendations"]
    normalized = result["normalized_input_receipt"]
    assert isinstance(recommendations, list)
    assert isinstance(normalized, dict)
    if recommendations and isinstance(recommendations[0], dict):
        recommendations[0]["action"] = {
            "cumulative_refuel_to_end": model["cumulative_refuel_to_end_l"],
            "minimum_pit_stops": model["minimum_pit_stops"],
            "next_pit_window": model["next_pit_window"],
        }
        recommendations[0]["scenario_sha256"] = result["scenario_sha256"]
        burn = model["burn"]
        assert isinstance(burn, dict)
        recommendations[0]["confidence_basis"] = {
            "historical_burn_stability": str(burn["confidence"]).upper(),
            "overall_plan": "LOW_BECAUSE_EVENT_RULES_AND_TRAFFIC_ARE_UNAVAILABLE",
            "scenario_inputs": "USER_RULE",
        }
        recommendations[0]["evidence_ids"] = [
            f"{normalized['samples_sha256']}:distance-wrap-v2:lap:{index}"
            for index in range(1, int(burn["accepted_laps"]) + 1)
        ]
    result["model_output_sha256"] = canonical_sha256(model)
    result["model_semantic_sha256"] = canonical_sha256(
        {
            "lap_receipt": result["lap_receipt"],
            "model_output": model,
            "pipeline": pipeline,
            "scenario": scenario,
        }
    )
    result["fuel_replay_sha256"] = canonical_sha256(
        {key: result[key] for key in _REPLAY_BINDING_KEYS}
    )
    return result


def _fuel_replay() -> dict[str, object]:
    event_receipt: dict[str, object] = {
        "accepted_sample_count": 600,
        "config_sha256": "1" * 64,
        "contract_version": "telemetry-events-v1",
        "event_count": 12,
        "event_kind_counts": {
            "lap_completed": 10,
            "session_started": 1,
            "source_started": 1,
        },
        "events_sha256": "2" * 64,
        "rejected_sample_count": 0,
        "sample_count": 600,
        "session_epoch_count": 1,
        "source_epoch_count": 1,
    }
    event_receipt["receipt_sha256"] = canonical_sha256(event_receipt)
    scenario = {
        "conservative_quantile": _scenario_field(0.9),
        "current_fuel_l": _scenario_field(20.0),
        "minimum_valid_laps": _scenario_field(5),
        "refuel_rate_l_per_s": _scenario_field(2.0),
        "remaining_laps": _scenario_field(10),
        "reserve_l": _scenario_field(1.0),
        "tank_capacity_l": _scenario_field(120.0),
        "timed_race_extra_laps": _scenario_field(1),
    }
    model = {
        "burn": {
            "accepted_laps": 8,
            "coefficient_of_variation": 0.08 / 3.9,
            "confidence": "high",
            "conservative_l_per_lap": 4.0,
            "conservative_quantile": 0.9,
            "label": "derived",
            "maximum_l_per_lap": 4.1,
            "mean_l_per_lap": 3.9,
            "minimum_l_per_lap": 3.8,
            "rejected_laps": 2,
            "source_label": "observed",
            "standard_deviation_l_per_lap": 0.08,
        },
        "conservative_fuel_to_end_l": {
            "label": "estimated",
            "unit": "L",
            "value": 41.0,
        },
        "cumulative_refuel_time_to_end_s": {
            "label": "estimated",
            "unit": "s",
            "value": 10.5,
        },
        "cumulative_refuel_to_end_l": {
            "label": "estimated",
            "unit": "L",
            "value": 21.0,
        },
        "current_fuel_l": {"label": "user_rule", "unit": "L", "value": 20.0},
        "mean_fuel_to_end_l": {"label": "estimated", "unit": "L", "value": 40.0},
        "minimum_pit_stops": {"label": "estimated", "unit": "stops", "value": 1},
        "next_pit_window": {
            "earliest_lap_from_now": 0,
            "label": "estimated",
            "latest_lap_from_now": 4,
        },
        "reason_codes": [],
        "rejection_counts": [["INELIGIBLE_LAP", 2]],
        "remaining_laps": {"label": "user_rule", "unit": "laps", "value": 10},
        "safe_laps_on_current_fuel": {
            "label": "estimated",
            "unit": "laps",
            "value": 4,
        },
        "safe_laps_on_full_tank": {
            "label": "estimated",
            "unit": "laps",
            "value": 29,
        },
        "status": "ready",
    }
    replay: dict[str, object] = {
        "capabilities": {
            "current_tire_wear": _unavailable(
                "CURRENT_STINT_TIRE_WEAR_MODEL_NOT_IMPLEMENTED",
                "CURRENT_TIRE_WEAR_CLAIM",
            ),
            "fuel_model_shadow": {"reasons": [], "status": "PASS"},
            "opponent_fuel": _unavailable(
                "OPPONENT_FUEL_NOT_EXPOSED_BY_SDK", "OPPONENT_FUEL_CLAIM"
            ),
            "race_recommendation": {
                "reasons": [
                    "SHADOW_ONLY",
                    "EVENT_RULES_PROFILE_MISSING",
                    "TRAFFIC_MODEL_NOT_IMPLEMENTED",
                ],
                "status": "BLOCKED",
            },
            "traffic_model": _unavailable("TRAFFIC_MODEL_NOT_IMPLEMENTED", "REJOIN_TRAFFIC_CLAIM"),
        },
        "contract_version": "fuel-model-replay-v2",
        "event_receipt": event_receipt,
        "input_evidence": {
            "authenticity_status": "HASHED_LOCAL_FILE_NOT_AUTHENTICATED",
            "byte_size": 1000,
            "completion_status": "COMPLETE",
            "record_count": 600,
            "session_id": "session",
            "source_id": "source",
            "source_kind": "IBT_OFFLINE",
            "source_sha256": "3" * 64,
            "tick_rate_hz": 60,
        },
        "input_kind": "ibt",
        "lap_receipt": {
            "algorithm_version": "distance-wrap-v2",
            "fuel_eligible_lap_count": 8,
            "lap_count": 10,
            "laps_sha256": "4" * 64,
            "modeled_sample_count": 600,
            "quality_complete_lap_count": 10,
            "structurally_complete_lap_count": 10,
        },
        "model_output": model,
        "model_output_sha256": "0" * 64,
        "model_semantic_sha256": "0" * 64,
        "normalized_input_receipt": {
            "contract_version": "normalized-telemetry-v3",
            "sample_count": 600,
            "samples_sha256": "5" * 64,
        },
        "pipeline": {
            "config_sha256": "0" * 64,
            "event_contract_version": "telemetry-events-v1",
            "feature_pipeline_version": "normalized-lap-fuel-v1",
            "fuel_model_version": "fuel-strategy-v1",
            "lap_algorithm_version": "distance-wrap-v2",
            "normalization": {
                "opponent_error_policy": "degrade",
                "profile_version": "normalized-sdk-adapter-v3",
                "stale_after_us": 500000,
            },
            "normalized_telemetry_contract_version": "normalized-telemetry-v3",
            "tick_rate_hz": 60,
        },
        "quality_gate": {"reasons": [], "status": "PASS"},
        "recommendations": [
            {
                "action": {},
                "claim_level": "scenario_estimate",
                "confidence": "LOW",
                "confidence_basis": {},
                "evidence_ids": ["lap:1"],
                "executable": False,
                "kind": "FUEL_PLAN_CANDIDATE",
                "practice_only": False,
                "recommendation_id": "fuel:shadow_plan",
                "scenario_sha256": "0" * 64,
                "status": "SHADOW_ONLY",
            }
        ],
        "scenario": scenario,
        "scenario_sha256": "0" * 64,
        "series_evidence": {
            "degraded_sample_count": 0,
            "missing_channel_sample_counts": {
                "FuelLevel": 0,
                "Lap": 0,
                "LapCompleted": 0,
                "LapDistPct": 0,
                "OnPitRoad": 0,
                "PlayerCarInPitStall": 0,
                "PlayerTrackSurface": 0,
                "SessionTick": 0,
                "SessionTime": 0,
                "Speed": 0,
            },
            "modeled_sample_count": 600,
            "normalized_dropped_tick_count": 0,
            "quality_issue_counts": {},
            "segmentation_error": None,
        },
    }
    return _rehash(replay)


def _smoke_rules() -> dict[str, object]:
    return {
        "contract_version": "endurance-event-rules-v1",
        "official_event_rules": False,
        "profile_id": "development-smoke-unbound-v1",
        "profile_status": "DEVELOPMENT_SMOKE",
        "provenance": "USER_RULE",
        "scope": {
            "car": "UNBOUND_DEVELOPMENT_SMOKE",
            "event": "UNBOUND_DEVELOPMENT_SMOKE",
            "track": "UNBOUND_DEVELOPMENT_SMOKE",
        },
        "selection_policy": "LATEST_FEASIBLE_FUEL_ONLY",
        "service_rules": {
            "fuel_tire_service_timing": "SEQUENTIAL",
            "no_tire_service_allowed": True,
            "pit_lane_loss_s": 25.0,
            "refuel_rate_l_per_s": 2.0,
            "tank_capacity_l": 120.0,
            "tire_change_required": False,
            "tire_change_time_s": 30.0,
        },
    }


def _unknown_rules() -> dict[str, object]:
    return {
        "contract_version": "endurance-event-rules-v1",
        "official_event_rules": False,
        "profile_id": "unknown-v1",
        "profile_status": "UNKNOWN",
        "provenance": "UNKNOWN",
        "scope": {"car": "UNKNOWN", "event": "UNKNOWN", "track": "UNKNOWN"},
        "selection_policy": None,
        "service_rules": None,
    }


def _build(
    replay: dict[str, object] | None = None,
    *,
    rules: dict[str, object] | None = None,
    previous: dict[str, object] | None = None,
) -> dict[str, object]:
    selected = replay or _fuel_replay()
    return build_pit_plan(
        selected,
        expected_fuel_replay_sha256=str(selected["fuel_replay_sha256"]),
        rules_value=rules,
        previous_plan_value=previous,
    )


def test_development_smoke_plan_is_shadow_only_and_fuel_feasibility_only():
    plan = _build(rules=_smoke_rules())

    assert plan["quality_gate"] == {
        "reasons": ["NON_OFFICIAL_DEVELOPMENT_SMOKE_ONLY"],
        "status": "PASS_DEVELOPMENT_SMOKE",
    }
    assert plan["derivation_status"] == "POST_ADMISSION_DERIVED"
    assert plan["attestation_status"] == "NOT_R7_ATTESTED"
    assert plan["plan_scope"] == "FUEL_FEASIBILITY_ONLY"
    assert len(plan["service_alternatives"]) == 4
    recommendation = plan["recommendations"][0]
    assert recommendation["status"] == "SHADOW_ONLY"
    assert recommendation["executable"] is False
    assert recommendation["claim_scope"] == "FUEL_FEASIBILITY_ONLY"
    assert recommendation["action"]["recommended_lap_from_now"] == 4
    assert recommendation["action"]["fuel_add_l"] == pytest.approx(21.0)
    assert recommendation["action"]["service_alternative_id"] == "fuel_to_end:no_tires"
    assert recommendation["recommendation_basis"]["source_id"] == "source"
    assert recommendation["recommendation_basis"]["session_id"] == "session"
    assert (
        recommendation["recommendation_basis"]["input_lineage_sha256"]
        == (plan["input_binding"]["input_lineage_sha256"])
    )
    assert plan["input_binding"]["source_kind"] == "IBT_OFFLINE"
    assert recommendation["supersedes_id"] is None
    assert recommendation["valid_until"]["predicates"] == [
        "NEXT_LAP_COMPLETED",
        "FUEL_OBSERVATION_CHANGED",
        "PIT_STATE_CHANGED",
        "EVENT_RULES_PROFILE_CHANGED",
        "SOURCE_STALE_RESET_OR_SCHEMA_CHANGED",
    ]
    assert plan["lifecycle_events"] == [
        {"event": "ISSUE", "recommendation_id": recommendation["recommendation_id"]}
    ]
    assert plan["capabilities"]["race_recommendation"]["status"] == "BLOCKED"
    assert plan["capabilities"]["traffic_model"]["estimate_available"] is False
    assert plan["capabilities"]["rejoin_prediction"]["estimate_available"] is False
    assert plan["pit_plan_sha256"] == canonical_sha256(
        {key: value for key, value in plan.items() if key != "pit_plan_sha256"}
    )
    assert plan == _build(rules=_smoke_rules())


@pytest.mark.parametrize("rules", [None, _unknown_rules()])
def test_missing_or_unknown_rules_waits_without_recommendation(rules):
    plan = _build(rules=rules)

    assert plan["quality_gate"]["status"] == "WAIT_EVENT_RULES"
    assert plan["recommendations"] == []
    assert plan["service_alternatives"] == []
    assert plan["plan_scope"] is None
    assert plan["capabilities"]["event_rules"]["status"] == "WAIT_EVENT_RULES"
    assert plan["capabilities"]["race_recommendation"]["status"] == "BLOCKED"


def test_independent_expected_digest_rejects_self_consistent_rehash():
    replay = _fuel_replay()
    admitted = str(replay["fuel_replay_sha256"])
    model = replay["model_output"]
    assert isinstance(model, dict)
    current = model["current_fuel_l"]
    assert isinstance(current, dict)
    current["value"] = 19.0
    tampered = _rehash(replay)

    with pytest.raises(PitPlanError, match="independent expected digest"):
        build_pit_plan(
            tampered,
            expected_fuel_replay_sha256=admitted,
            rules_value=_smoke_rules(),
        )


def test_inner_event_receipt_is_revalidated_after_outer_rehash():
    replay = _fuel_replay()
    event = replay["event_receipt"]
    assert isinstance(event, dict)
    event["event_count"] = 13
    tampered = _rehash(replay)

    with pytest.raises(PitPlanError, match="event_kind_counts do not close"):
        _build(tampered, rules=_smoke_rules())


@pytest.mark.parametrize(
    "path",
    [
        ("pipeline",),
        ("pipeline", "normalization"),
        ("scenario",),
        ("model_output",),
        ("model_output", "burn"),
        ("input_evidence",),
        ("lap_receipt",),
        ("normalized_input_receipt",),
        ("series_evidence",),
        ("series_evidence", "missing_channel_sample_counts"),
        ("capabilities",),
        ("recommendations", 0),
    ],
)
def test_fully_rehashed_unexpected_nested_key_is_rejected(path):
    replay = _fuel_replay()
    node: object = replay
    for part in path:
        node = node[part]
    assert isinstance(node, dict)
    node["unexpected"] = "self-consistently-rehashed"
    tampered = _rehash(replay)

    with pytest.raises(PitPlanError, match="keys are invalid"):
        _build(tampered, rules=_smoke_rules())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("current_fuel_l", 19.0, "model/scenario current fuel"),
        ("safe_laps_on_current_fuel", 5, "safe laps do not close"),
        ("cumulative_refuel_to_end_l", 20.0, "cumulative refuel"),
    ],
)
def test_fully_rehashed_physical_model_mismatch_is_rejected(field, value, message):
    replay = _fuel_replay()
    model = replay["model_output"]
    assert isinstance(model, dict)
    labeled = model[field]
    assert isinstance(labeled, dict)
    labeled["value"] = value

    with pytest.raises(PitPlanError, match=message):
        _build(_rehash(replay), rules=_smoke_rules())


def test_executable_upstream_recommendation_is_rejected_after_rehash():
    replay = _fuel_replay()
    recommendations = replay["recommendations"]
    assert isinstance(recommendations, list)
    recommendations[0]["executable"] = True
    tampered = _rehash(replay)

    with pytest.raises(PitPlanError, match="recommendation semantics are invalid"):
        _build(tampered, rules=_smoke_rules())


def test_rules_must_match_fuel_scenario_and_remain_non_official():
    mismatched = _smoke_rules()
    mismatched["service_rules"]["refuel_rate_l_per_s"] = 3.0
    with pytest.raises(PitPlanError, match="refuel rate conflicts"):
        _build(rules=mismatched)

    official = _smoke_rules()
    official["official_event_rules"] = True
    with pytest.raises(PitPlanError, match="non-official USER_RULE"):
        _build(rules=official)


def test_lifecycle_supersedes_changed_plan_and_revokes_when_rules_disappear():
    replay = _fuel_replay()
    first = _build(replay, rules=_smoke_rules())
    first_id = first["recommendations"][0]["recommendation_id"]

    changed_rules = _smoke_rules()
    changed_rules["service_rules"]["pit_lane_loss_s"] = 26.0
    second = _build(replay, rules=changed_rules, previous=first)
    recommendation = second["recommendations"][0]
    assert recommendation["supersedes_id"] == first_id
    assert [item["event"] for item in second["lifecycle_events"]] == ["REVOKE", "ISSUE"]
    assert second["lifecycle_events"][0]["reason_codes"] == ["EVENT_RULES_PROFILE_CHANGED"]
    assert second["lifecycle_events"][1]["supersedes_id"] == first_id

    revoked = _build(replay, rules=None, previous=second)
    assert revoked["recommendations"] == []
    event = revoked["lifecycle_events"][0]
    assert event["event"] == "REVOKE"
    assert event["recommendation_id"] == recommendation["recommendation_id"]
    assert event["reason_codes"] == [
        "EVENT_RULES_PROFILE_CHANGED",
        "EVENT_RULES_BECAME_UNAVAILABLE",
    ]


def test_same_plan_emits_no_change_and_tampered_previous_is_rejected():
    first = _build(rules=_smoke_rules())
    repeated = _build(rules=_smoke_rules(), previous=first)
    assert repeated["lifecycle_events"][0]["event"] == "NO_CHANGE"

    tampered = copy.deepcopy(first)
    tampered["quality_gate"]["status"] = "PASS"
    with pytest.raises(PitPlanError, match="previous pit_plan_sha256 mismatch"):
        _build(rules=_smoke_rules(), previous=tampered)

    self_rehashed = copy.deepcopy(first)
    self_rehashed["recommendations"][0]["executable"] = True
    self_rehashed["pit_plan_sha256"] = canonical_sha256(
        {key: value for key, value in self_rehashed.items() if key != "pit_plan_sha256"}
    )
    with pytest.raises(PitPlanError, match="not a safe shadow candidate"):
        _build(rules=_smoke_rules(), previous=self_rehashed)

    elevated = copy.deepcopy(first)
    elevated["recommendations"][0]["confidence"] = "HIGH"
    elevated["pit_plan_sha256"] = canonical_sha256(
        {key: value for key, value in elevated.items() if key != "pit_plan_sha256"}
    )
    with pytest.raises(PitPlanError, match="fixed semantics are invalid"):
        _build(rules=_smoke_rules(), previous=elevated)

    unexpected_binding = copy.deepcopy(first)
    unexpected_binding["input_binding"]["unexpected"] = True
    unexpected_binding["pit_plan_sha256"] = canonical_sha256(
        {key: value for key, value in unexpected_binding.items() if key != "pit_plan_sha256"}
    )
    with pytest.raises(PitPlanError, match="previous input_binding keys are invalid"):
        _build(rules=_smoke_rules(), previous=unexpected_binding)


def test_previous_plan_from_different_source_or_session_fails_closed():
    first = _build(rules=_smoke_rules())
    for identity_key in ("source_id", "session_id"):
        replay = _fuel_replay()
        evidence = replay["input_evidence"]
        assert isinstance(evidence, dict)
        evidence[identity_key] = f"different-{identity_key}"
        changed = _rehash(replay)
        with pytest.raises(PitPlanError, match="source/session identity mismatch"):
            _build(changed, rules=_smoke_rules(), previous=first)


def test_same_identity_changed_normalized_lineage_is_explicitly_superseded():
    first = _build(rules=_smoke_rules())
    replay = _fuel_replay()
    normalized = replay["normalized_input_receipt"]
    assert isinstance(normalized, dict)
    normalized["samples_sha256"] = "6" * 64
    changed = _rehash(replay)
    second = _build(changed, rules=_smoke_rules(), previous=first)

    revoke = second["lifecycle_events"][0]
    assert revoke["event"] == "REVOKE"
    assert "NORMALIZED_INPUT_LINEAGE_CHANGED" in revoke["reason_codes"]
    assert revoke["previous_input_lineage_sha256"] != revoke["current_input_lineage_sha256"]


def test_concurrent_service_tie_prefers_no_tire_when_tires_are_unmodeled():
    rules = _smoke_rules()
    rules["service_rules"]["fuel_tire_service_timing"] = "CONCURRENT"
    rules["service_rules"]["tire_change_time_s"] = 1.0
    plan = _build(rules=rules)

    recommendation = plan["recommendations"][0]
    assert recommendation["action"]["service_alternative_id"] == "fuel_to_end:no_tires"


def test_multi_stop_is_explicit_wait_not_a_fabricated_allocation():
    replay = _fuel_replay()
    model = replay["model_output"]
    scenario = replay["scenario"]
    assert isinstance(model, dict)
    assert isinstance(scenario, dict)
    stops = model["minimum_pit_stops"]
    remaining = model["remaining_laps"]
    assert isinstance(stops, dict)
    assert isinstance(remaining, dict)
    stops["value"] = 2
    remaining["value"] = 40
    scenario["remaining_laps"]["value"] = 40
    model["mean_fuel_to_end_l"]["value"] = 157.0
    model["conservative_fuel_to_end_l"]["value"] = 161.0
    model["cumulative_refuel_to_end_l"]["value"] = 141.0
    model["cumulative_refuel_time_to_end_s"]["value"] = 70.5
    replay = _rehash(replay)

    plan = _build(replay, rules=_smoke_rules())
    assert plan["quality_gate"]["status"] == "WAIT_STRATEGY_DATA"
    assert plan["recommendations"] == []
    assert plan["service_alternatives"] == []


def test_cli_wait_exit_and_exclusive_deterministic_smoke_output(tmp_path: Path, capfd):
    replay = _fuel_replay()
    replay_path = tmp_path / "fuel.json"
    rules_path = tmp_path / "rules.json"
    output_path = tmp_path / "plan.json"
    replay_path.write_text(json.dumps(replay), encoding="utf-8")
    rules_path.write_text(json.dumps(_smoke_rules()), encoding="utf-8")

    exit_code = main(
        [
            str(replay_path),
            "--expected-fuel-replay-sha256",
            str(replay["fuel_replay_sha256"]),
            "--rules",
            str(rules_path),
            "--output",
            str(output_path),
        ]
    )
    stdout = capfd.readouterr().out.encode("utf-8")
    assert exit_code == 0
    assert output_path.read_bytes() == stdout
    second_exit = main(
        [
            str(replay_path),
            "--expected-fuel-replay-sha256",
            str(replay["fuel_replay_sha256"]),
            "--rules",
            str(rules_path),
            "--output",
            str(output_path),
        ]
    )
    second_error = json.loads(capfd.readouterr().out)
    assert second_exit == 3
    assert "File exists" in second_error["error"]
    assert output_path.read_bytes() == stdout

    wait_code = main(
        [
            str(replay_path),
            "--expected-fuel-replay-sha256",
            str(replay["fuel_replay_sha256"]),
        ]
    )
    wait = json.loads(capfd.readouterr().out)
    assert wait_code == 5
    assert wait["quality_gate"]["status"] == "WAIT_EVENT_RULES"
    assert wait["recommendations"] == []


def test_strict_loader_rejects_duplicate_and_nonfinite_json(tmp_path: Path, capfd):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"contract_version":"x","contract_version":"y"}', encoding="utf-8")
    code = main(
        [
            str(duplicate),
            "--expected-fuel-replay-sha256",
            "0" * 64,
        ]
    )
    assert code == 3
    assert "duplicate JSON key" in json.loads(capfd.readouterr().out)["error"]

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"x":NaN}', encoding="utf-8")
    code = main(
        [
            str(nonfinite),
            "--expected-fuel-replay-sha256",
            "0" * 64,
        ]
    )
    assert code == 3
    assert "non-finite JSON value" in json.loads(capfd.readouterr().out)["error"]
