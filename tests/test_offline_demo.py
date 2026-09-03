from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_driving_labels import _rehash_replay, _replay

from iracing_ai_engineer.capabilities import unavailable_inference_capability
from iracing_ai_engineer.driving_labels import build_driving_label_candidate
from iracing_ai_engineer.fuel import FuelScenario
from iracing_ai_engineer.offline_demo import (
    OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION,
    OfflineEngineerDemoError,
    build_offline_engineer_demo,
    canonical_sha256,
)

PATH = Path("fixture.ibt")
SOURCE_ID = "fixture-source"
SESSION_ID = "fixture-session"
TARGET_LAP = 11


def _sha(name: str) -> str:
    return canonical_sha256({"fixture": name})


def _scenario() -> FuelScenario:
    return FuelScenario(
        current_fuel_l=20.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
        reserve_l=1.0,
    )


def _recommendation(kind: str) -> dict[str, object]:
    return {
        "confidence": "LOW",
        "evidence_ids": [f"fixture:{kind}"],
        "executable": False,
        "kind": kind,
        "recommendation_id": f"fixture:{kind}",
        "status": "SHADOW_ONLY",
    }


def _unavailable(reason: str, claim: str) -> dict[str, object]:
    return unavailable_inference_capability(
        reasons=(reason,),
        blocked_claims=(claim,),
    )


def _components() -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    scenario = _scenario()
    driving = _replay()
    driving["capabilities"] = {
        "curb_guidance": _unavailable("CURB_GEOMETRY_NOT_MODELED", "CURB_RECOMMENDATION"),
        "current_tire_wear": _unavailable(
            "CURRENT_STINT_TIRE_WEAR_MODEL_NOT_IMPLEMENTED", "CURRENT_TIRE_WEAR_CLAIM"
        ),
        "driving_model_shadow": {"reasons": [], "status": "PASS"},
        "personalized_coaching": _unavailable("HUMAN_CORNER_LABELS_MISSING", "PERSONALIZED_ACTION"),
        "race_coaching": {"reasons": ["SHADOW_ONLY"], "status": "BLOCKED"},
        "traffic_model": _unavailable("TRAFFIC_MODEL_NOT_IMPLEMENTED", "REJOIN_TRAFFIC_CLAIM"),
    }
    driving["recommendations"] = [_recommendation("DRIVING_CANDIDATE")]
    driving = _rehash_replay(driving)
    labels = build_driving_label_candidate(
        driving,
        label_set_id="fixture-labels-v1",
        car_key="fixture-car",
        track_key="fixture-track",
        layout_key="fixture-layout",
    )

    evidence = copy.deepcopy(driving["input_evidence"])
    normalized = copy.deepcopy(driving["normalized_input_receipt"])
    event_receipt = copy.deepcopy(driving["event_receipt"])
    track_context = copy.deepcopy(driving["driving_context"])
    scenario_payload = scenario.to_dict()
    components: dict[str, dict[str, object]] = {
        "shadow": {
            "capabilities": {
                "current_tire_wear": _unavailable(
                    "TIRE_WEAR_CHANNELS_ABSENT", "CURRENT_TIRE_WEAR_CLAIM"
                ),
                "driving_analysis_smoke": {"reasons": [], "status": "PASS"},
                "fuel_model_smoke": {"reasons": [], "status": "PASS"},
                "opponent_fuel": _unavailable(
                    "OPPONENT_FUEL_NOT_EXPOSED_BY_SDK", "OPPONENT_FUEL_CLAIM"
                ),
                "race_recommendation": {
                    "reasons": ["OFFLINE_SHADOW_MODE"],
                    "status": "BLOCKED",
                },
                "traffic_model": _unavailable("CARIDX_ARRAYS_ABSENT", "REJOIN_TRAFFIC_CLAIM"),
            },
            "config": {"fuel_scenario": scenario_payload},
            "context": {"track_length_m": track_context["track_length_mm"] / 1_000},
            "contract_version": "shadow-report-v2",
            "execution_mode": "SHADOW",
            "recommendations": [
                _recommendation("FUEL_PLAN_CANDIDATE"),
                _recommendation("DRIVING_CANDIDATE"),
            ],
            "receipt": {
                "analysis_sha256": _sha("shadow-analysis"),
                "capabilities_sha256": _sha("shadow-capabilities"),
                "config_sha256": _sha("shadow-config"),
                "estimates_sha256": _sha("shadow-estimates"),
                "facts_sha256": _sha("shadow-facts"),
                "model_outputs_sha256": _sha("shadow-models"),
                "recommendations_sha256": _sha("shadow-recommendations"),
                "suppressions_sha256": _sha("shadow-suppressions"),
            },
            "source": {
                "source_mode": "IBT",
                "source_sha256": evidence["source_sha256"],
            },
            "suppressions": [
                {
                    "blocks": ["PRODUCTION_PIT_COMMAND"],
                    "code": "OFFLINE_SHADOW_MODE",
                    "scope": "strategy",
                }
            ],
        },
        "fuel": {
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
                    "reasons": ["SHADOW_ONLY"],
                    "status": "BLOCKED",
                },
                "traffic_model": _unavailable(
                    "TRAFFIC_MODEL_NOT_IMPLEMENTED", "REJOIN_TRAFFIC_CLAIM"
                ),
            },
            "contract_version": "fuel-model-replay-v2",
            "event_receipt": event_receipt,
            "fuel_replay_sha256": _sha("fuel-replay"),
            "input_evidence": evidence,
            "input_kind": "ibt",
            "model_output_sha256": _sha("fuel-model-output"),
            "model_semantic_sha256": _sha("fuel-model-semantic"),
            "normalized_input_receipt": normalized,
            "recommendations": [_recommendation("FUEL_PLAN_CANDIDATE")],
            "scenario": scenario_payload,
            "scenario_sha256": canonical_sha256(scenario_payload),
        },
        "driving": driving,
        "condition": {
            "capabilities": {
                "current_tire_wear": _unavailable(
                    "CURRENT_STINT_TIRE_WEAR_MODEL_NOT_IMPLEMENTED",
                    "CURRENT_TIRE_WEAR_CLAIM",
                ),
                "observed_proximity_gate": {
                    "estimate_available": False,
                    "reasons": ["OPPONENT_ARRAYS_MISSING"],
                    "status": "WAIT",
                },
                "personalized_coaching": _unavailable(
                    "HUMAN_CORNER_LABELS_MISSING", "PERSONALIZED_ACTION"
                ),
                "tire_usage_context_gate": {
                    "estimate_available": False,
                    "reasons": ["TIRE_USAGE_CONTEXT_UNAVAILABLE"],
                    "status": "WAIT",
                },
                "traffic_model": _unavailable(
                    "TRAFFIC_MODEL_NOT_IMPLEMENTED", "REJOIN_TRAFFIC_CLAIM"
                ),
            },
            "condition_cohort_sha256": _sha("condition-cohort"),
            "condition_config_sha256": _sha("condition-config"),
            "condition_provenance_sha256": _sha("condition-provenance"),
            "condition_semantic_sha256": _sha("condition-semantic"),
            "contract_version": "condition-cohort-v1",
            "input_evidence": copy.deepcopy(evidence),
            "input_kind": "ibt",
            "normalized_input_receipt": copy.deepcopy(normalized),
            "quality_gate": {
                "reasons": [
                    "APPROVED_TRACK_STATE_LABEL_MISSING",
                    "OPPONENT_ARRAYS_MISSING",
                    "INSUFFICIENT_MATCHED_LAPS",
                ],
                "status": "DEGRADED",
            },
            "readiness_status": "WAIT_CONDITION_DATA",
            "recommendations": [],
            "target_lap_ordinal": TARGET_LAP,
            "track_context": track_context,
            "trusted_readiness_status": "WAIT_CONDITION_DATA",
        },
    }
    return components, labels


def _run(
    components: dict[str, dict[str, object]],
    labels: dict[str, object],
) -> tuple[dict[str, object], list[str]]:
    calls: list[str] = []

    def shadow_builder(path, *, analysis, fuel_scenario):
        assert path == PATH
        assert analysis == "all"
        assert fuel_scenario == _scenario()
        calls.append("shadow")
        return copy.deepcopy(components["shadow"])

    @contextmanager
    def opener(path, *, source_id, session_id):
        assert path == PATH
        assert source_id == SOURCE_ID
        assert session_id == SESSION_ID
        calls.append("open")
        try:
            yield object()
        finally:
            calls.append("close")

    def fuel_builder(run, *, scenario):
        assert run is not None
        assert scenario == _scenario()
        calls.append("fuel")
        return copy.deepcopy(components["fuel"])

    def driving_builder(run):
        assert run is not None
        calls.append("driving")
        return copy.deepcopy(components["driving"])

    def condition_builder(run, *, target_lap_ordinal):
        assert run is not None
        assert target_lap_ordinal == TARGET_LAP
        calls.append("condition")
        return copy.deepcopy(components["condition"])

    payload = build_offline_engineer_demo(
        PATH,
        source_id=SOURCE_ID,
        session_id=SESSION_ID,
        fuel_scenario=_scenario(),
        target_lap_ordinal=TARGET_LAP,
        pending_label_payload=labels,
        open_ibt_run=opener,
        shadow_builder=shadow_builder,
        fuel_builder=fuel_builder,
        driving_builder=driving_builder,
        condition_builder=condition_builder,
    )
    return payload, calls


def test_success_runs_each_real_component_boundary_in_order_and_binds_one_demo():
    components, labels = _components()

    payload, calls = _run(components, labels)

    assert calls == [
        "shadow",
        "open",
        "fuel",
        "close",
        "open",
        "driving",
        "close",
        "open",
        "condition",
        "close",
    ]
    assert payload["contract_version"] == OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION
    assert payload["execution_status"] == "COMPLETE"
    assert payload["execution_mode"] == "SHADOW"
    assert payload["advisor_only"] is True
    assert payload["input_binding"]["baseline_binding"]["status"] == "PASS"
    assert all(payload["input_binding"]["baseline_binding"]["comparisons"].values())
    assert payload["demo_sha256"] == canonical_sha256(
        {key: value for key, value in payload.items() if key != "demo_sha256"}
    )


def test_expected_condition_and_human_waits_are_honest_completed_demo_states():
    components, labels = _components()

    payload, _ = _run(components, labels)

    gates = payload["gates"]
    assert gates["offline_demo"] == {"reasons": [], "status": "PASS"}
    assert gates["shared_fuel_shadow"]["status"] == "PASS"
    assert gates["driving_shadow"]["status"] == "PASS"
    assert gates["condition_data"]["status"] == "WAIT_CONDITION_DATA"
    assert gates["condition_trust"]["status"] == "WAIT_CONDITION_DATA"
    assert gates["label_trust"] == {
        "reasons": ["LABEL_SET_NOT_APPROVED"],
        "status": "WAIT_HUMAN_LABELS",
    }
    assert gates["personalized_coaching"]["status"] == "BLOCKED"
    assert gates["race_recommendation"]["status"] == "BLOCKED"
    assert payload["receipts"]["labels"]["review_authenticity_status"] is None
    assert payload["receipts"]["labels"]["trusted_status"] == "WAIT_HUMAN_LABELS"
    assert payload["suppressions"]["condition"]["status"] == "WAIT_CONDITION_DATA"
    assert payload["suppressions"]["labels"]["status"] == "WAIT_HUMAN_LABELS"
    assert all(
        item["executable"] is False
        for recommendations in payload["recommendations"].values()
        for item in recommendations
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda components: components["condition"]["normalized_input_receipt"].update(
                samples_sha256="9" * 64
            ),
            "normalized input receipt mismatch",
        ),
        (
            lambda components: components["condition"]["input_evidence"].update(
                source_sha256="9" * 64
            ),
            "raw input evidence mismatch",
        ),
        (
            lambda components: components["fuel"]["scenario"].update(
                reserve_l={"value": 2.0, "provenance": "USER_RULE"}
            ),
            "shared fuel scenario mismatch",
        ),
        (
            lambda components: components["condition"]["track_context"].update(
                track_length_mm=1_001_000
            ),
            "track context mismatch",
        ),
    ],
)
def test_cross_component_input_mismatches_fail_closed(mutation, message):
    components, labels = _components()
    mutation(components)

    with pytest.raises(OfflineEngineerDemoError, match=message):
        _run(components, labels)


def test_valid_but_different_pending_candidate_basis_fails_closed():
    components, _ = _components()
    other_replay = copy.deepcopy(components["driving"])
    other_replay["input_evidence"]["source_id"] = "other-source"
    other_replay = _rehash_replay(other_replay)
    other_labels = build_driving_label_candidate(
        other_replay,
        label_set_id="other-labels-v1",
        car_key="fixture-car",
        track_key="fixture-track",
        layout_key="fixture-layout",
    )

    with pytest.raises(OfflineEngineerDemoError, match="candidate basis mismatch"):
        _run(components, other_labels)


def test_condition_target_must_equal_driving_reference_and_candidate_lap():
    components, _ = _components()
    other_replay = copy.deepcopy(components["driving"])
    other_replay["model_output"]["reference"]["lap_ordinal"] = 12
    for metric in other_replay["model_output"]["corner_metrics"]:
        metric["lap_ordinal"] = 12
    other_replay = _rehash_replay(other_replay)
    components["driving"] = other_replay
    other_labels = build_driving_label_candidate(
        other_replay,
        label_set_id="other-reference-v1",
        car_key="fixture-car",
        track_key="fixture-track",
        layout_key="fixture-layout",
    )

    with pytest.raises(
        OfflineEngineerDemoError,
        match="condition target lap/driving reference mismatch",
    ):
        _run(components, other_labels)


@pytest.mark.parametrize("component", ["shadow", "fuel", "driving"])
def test_any_executable_recommendation_fails_closed(component):
    components, labels = _components()
    components[component]["recommendations"][0]["executable"] = True

    with pytest.raises(OfflineEngineerDemoError, match="recommendation 0 is executable"):
        _run(components, labels)


@pytest.mark.parametrize(
    ("component", "capability"),
    [
        ("shadow", "current_tire_wear"),
        ("fuel", "opponent_fuel"),
        ("driving", "curb_guidance"),
        ("condition", "traffic_model"),
    ],
)
def test_unavailable_capabilities_cannot_be_deleted_or_promoted(component, capability):
    components, labels = _components()
    removed = copy.deepcopy(components)
    del removed[component]["capabilities"][capability]
    with pytest.raises(OfflineEngineerDemoError, match="must be a plain object"):
        _run(removed, labels)

    promoted = copy.deepcopy(components)
    promoted[component]["capabilities"][capability].update(
        status="PASS",
        estimate_available=True,
        confidence="HIGH",
        provenance="INFERRED",
    )
    with pytest.raises(OfflineEngineerDemoError, match="explicitly unavailable"):
        _run(promoted, labels)


def test_demo_hash_and_serialized_output_are_deterministic():
    first_components, first_labels = _components()
    second_components, second_labels = _components()

    first, _ = _run(first_components, first_labels)
    second, _ = _run(second_components, second_labels)

    assert first == second
    assert first["demo_sha256"] == second["demo_sha256"]
    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second,
        sort_keys=True,
        separators=(",", ":"),
    )
