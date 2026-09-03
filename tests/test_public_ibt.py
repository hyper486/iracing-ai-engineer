from __future__ import annotations

import json
from pathlib import Path

import pytest

from iracing_ai_engineer.adapters import iter_ibt_samples, open_ibt_telemetry
from iracing_ai_engineer.driving_labels import validate_driving_labels
from iracing_ai_engineer.driving_model_replay import build_driving_model_replay
from iracing_ai_engineer.events import process_telemetry_events
from iracing_ai_engineer.ibt import IbtReader, sha256_file
from iracing_ai_engineer.model_replay import build_fuel_model_replay
from iracing_ai_engineer.quality import analyze_ibt
from iracing_ai_engineer.replay import replay_ibt
from iracing_ai_engineer.shadow import FuelScenario, build_shadow_report

MANIFEST = json.loads(Path("data/public_sources.json").read_text(encoding="utf-8"))
ASSET = MANIFEST["assets"][0]
SAMPLE = Path(ASSET["local_path"])

requires_data = pytest.mark.skipif(
    not SAMPLE.is_file(), reason="REQUIRES_DATA: public Audi/Spa IBT absent"
)


def _scenario(candidate: dict[str, object]) -> FuelScenario:
    scenario = candidate["scenario"]
    assert isinstance(scenario, dict)
    return FuelScenario(
        current_fuel_l=scenario["current_fuel_l"],
        tank_capacity_l=scenario["tank_capacity_l"],
        refuel_rate_l_per_s=scenario["refuel_rate_l_per_s"],
        remaining_laps=scenario["remaining_laps"],
        reserve_l=scenario["reserve_l"],
    )


@pytest.fixture(scope="module")
def public_shadow_report():
    if not SAMPLE.is_file():
        pytest.skip("REQUIRES_DATA: public Audi/Spa IBT absent")
    return build_shadow_report(
        SAMPLE,
        fuel_scenario=_scenario(ASSET["provisional_shadow_receipt"]),
    )


@pytest.fixture(scope="module")
def public_shared_fuel_report():
    if not SAMPLE.is_file():
        pytest.skip("REQUIRES_DATA: public Audi/Spa IBT absent")
    candidate = ASSET["provisional_shared_fuel_model_receipt"]
    with open_ibt_telemetry(
        SAMPLE,
        source_id=candidate["source_id"],
        session_id=candidate["session_id"],
    ) as run:
        return build_fuel_model_replay(run, scenario=_scenario(candidate))


@pytest.fixture(scope="module")
def public_shared_driving_report():
    if not SAMPLE.is_file():
        pytest.skip("REQUIRES_DATA: public Audi/Spa IBT absent")
    candidate = ASSET["provisional_shared_driving_model_receipt"]
    with open_ibt_telemetry(
        SAMPLE,
        source_id=candidate["source_id"],
        session_id=candidate["session_id"],
    ) as run:
        return build_driving_model_replay(run)


def test_public_manifest_uses_pinned_source_and_separates_capability_claims():
    upstream = ASSET["upstream"]
    disposition = ASSET["disposition"]

    assert len(upstream["repository_commit"]) == 40
    assert upstream["repository_commit"] in upstream["download_url"]
    assert upstream["lfs_object_sha256"] == ASSET["sha256"]
    assert upstream["repository_license"] == "Apache-2.0"
    assert disposition["status"] == "ADMITTED_FOR_LOCAL_OFFLINE_MVP"
    assert disposition["redistribution_status"] == "REFERENCE_ONLY"
    assert disposition["rights_status"] == "REPO_ASSERTED_BUT_THIRD_PARTY_RIGHTS_UNVERIFIED"
    assert "personalized coaching evidence" in disposition["blocked_claims"]
    assert not ASSET["privacy"]["driver_info_exported"]
    assert ASSET["privacy"]["raw_contains_driver_info"]
    assert not ASSET["privacy"]["raw_redistribution_allowed"]


def test_public_offline_demo_receipt_is_frozen_and_preserves_wait_gates():
    receipt = ASSET["provisional_offline_demo_receipt"]

    assert receipt["contract_version"] == "offline-engineer-demo-v1"
    assert receipt["execution_mode"] == "SHADOW"
    assert receipt["execution_status"] == "COMPLETE"
    assert receipt["advisor_only"] is True
    assert receipt["demo_sha256"] == (
        "fedebe9bea03be2767ac5a29f3373255d68fb7fff352aeda627d31ff40a26323"
    )
    assert receipt["serialized_byte_size"] == 29_670
    assert receipt["serialized_sha256"] == (
        "8fb7aa2023a06009f27139e43706706063a35300174cd30bbb374a4879199e86"
    )
    assert receipt["reproduction"] == {
        "byte_identical": True,
        "process_count": 2,
        "python_hash_seeds": ["1", "987654"],
    }
    assert receipt["gates"] == {
        "condition_trust": "WAIT_CONDITION_DATA",
        "driving_shadow": "PASS",
        "label_trust": "WAIT_HUMAN_LABELS",
        "offline_demo": "PASS",
        "personalized_coaching": "BLOCKED",
        "race_recommendation": "BLOCKED",
        "shared_fuel_shadow": "PASS",
    }
    assert (
        receipt["normalized_input_receipt"]
        == ASSET["provisional_shared_fuel_model_receipt"]["normalized_input_receipt"]
    )


def test_public_audi_driving_label_candidate_matches_manifest_and_stays_pending():
    candidate_path = Path("data/labels/candidates/audi-spa-v1.candidate.json")
    candidate = validate_driving_labels(json.loads(candidate_path.read_text(encoding="utf-8")))
    expected = ASSET["provisional_driving_label_candidate"]

    assert candidate["contract_version"] == expected["contract_version"]
    assert candidate["label_set_id"] == expected["label_set_id"]
    assert candidate["artifact_sha256"] == expected["artifact_sha256"]
    assert candidate["candidate_payload_sha256"] == expected["candidate_payload_sha256"]
    assert candidate["candidate_basis"]["labeled_lap_ordinal"] == expected["labeled_lap_ordinal"]
    assert candidate["candidate_basis"]["model_output_sha256"] == expected["model_output_sha256"]
    assert (
        candidate["candidate_basis"]["model_semantic_sha256"] == expected["model_semantic_sha256"]
    )
    assert len(candidate["proposals"]) == expected["proposal_count"]
    assert candidate["review"]["status"] == expected["review_status"]
    assert candidate["review"]["authenticity_status"] is expected["review_authenticity_status"]
    assert candidate["human_labels"] == []


@requires_data
def test_public_audi_source_matches_frozen_integrity_and_schema():
    assert SAMPLE.stat().st_size == ASSET["byte_size"]
    assert sha256_file(SAMPLE) == ASSET["sha256"]

    with IbtReader(SAMPLE) as reader:
        metadata = reader.metadata
        contract = ASSET["ibt_contract"]
        assert metadata.record_count == contract["record_count"]
        assert metadata.tick_rate_hz == contract["tick_rate_hz"]
        assert metadata.variable_count == contract["variable_count"]
        assert metadata.record_size_bytes == contract["record_size_bytes"]
        assert metadata.schema_sha256 == contract["schema_sha256"]
        assert metadata.trailing_bytes == 0

        context = reader.public_session_context()
        expected = ASSET["session_context"]
        assert context["track_name"] == expected["track_name"]
        assert context["track_display_name"] == expected["track_display_name"]
        assert context["sim_build"] == expected["sim_build"]
        assert not any("driver" in key.lower() or "user" in key.lower() for key in context)


@requires_data
def test_public_audi_capabilities_and_replay_match_receipts():
    report = analyze_ibt(SAMPLE)
    quality = ASSET["quality_receipt"]

    assert report.summary_row()["structural_laps"] == quality["structurally_complete_lap_count"]
    assert report.summary_row()["clean_driving_laps"] == quality["clean_driving_lap_count"]
    assert report.summary_row()["fuel_eligible_laps"] == quality["fuel_eligible_lap_count"]
    assert report.capabilities.fuel_ready
    assert report.capabilities.driving_ready
    assert not report.capabilities.coaching_evidence_ready

    replay = replay_ibt(SAMPLE, frame_hash_chunk_size=4096)
    expected = ASSET["replay_receipt"]
    assert replay.replay_sha256 == expected["replay_sha256"]
    assert replay.normalized_frames_sha256 == expected["normalized_frames_sha256"]
    assert replay.events_sha256 == expected["events_sha256"]
    assert replay.results_sha256 == expected["results_sha256"]


@requires_data
def test_public_audi_shadow_report_matches_frozen_candidate_receipt(
    public_shadow_report,
):
    candidate = ASSET["provisional_shadow_receipt"]
    report = public_shadow_report

    assert report["receipt"] == candidate["receipt"]
    assert report["capabilities"]["fuel_model_smoke"]["status"] == "PASS"
    assert report["capabilities"]["driving_analysis_smoke"]["status"] == "PASS"
    assert report["capabilities"]["personalized_coaching"]["status"] == "SKIP"
    assert report["capabilities"]["traffic_model"]["status"] == "SKIP"
    assert report["capabilities"]["current_tire_wear"]["status"] == "SKIP"
    assert report["capabilities"]["opponent_fuel"]["status"] == "SKIP"
    for capability in ("current_tire_wear", "opponent_fuel", "traffic_model"):
        assert report["capabilities"][capability]["provenance"] == "UNKNOWN"
        assert report["capabilities"][capability]["estimate_available"] is False
        assert report["capabilities"][capability]["confidence"] == "NONE"
    assert report["capabilities"]["race_recommendation"]["status"] == "BLOCKED"
    fuel = report["model_outputs"]["fuel"]
    assert fuel["burn"]["accepted_laps"] == 15
    assert fuel["burn"]["conservative_l_per_lap"] == pytest.approx(3.9986486434936523)
    assert fuel["conservative_fuel_to_end_l"]["value"] == pytest.approx(40.98648643493652)
    assert fuel["minimum_pit_stops"]["value"] == 1
    assert fuel["next_pit_window"] == {
        "earliest_lap_from_now": 0,
        "label": "estimated",
        "latest_lap_from_now": 4,
    }
    assert all(not item["executable"] for item in report["recommendations"])
    assert all(
        item["practice_only"]
        for item in report["recommendations"]
        if item["kind"] == "DRIVING_CANDIDATE"
    )


@requires_data
def test_public_audi_normalized_streaming_events_match_frozen_candidate_receipt():
    candidate = ASSET["provisional_normalized_event_receipt"]
    events, receipt = process_telemetry_events(
        iter_ibt_samples(
            SAMPLE,
            source_id=candidate["source_id"],
            session_id=candidate["session_id"],
        )
    )

    assert receipt.to_dict() == candidate["receipt"]
    assert candidate["event_replay_contract_version"] == "event-replay-v1"
    assert candidate["event_replay_sha256"] == (
        "9ce5eb1e38608309b285cc0e33a8eaa7c8994856ada73f9cfea403a7d85d60f9"
    )
    assert candidate["quality_gate"] == {"reasons": [], "status": "PASS"}
    assert {event.source_id for event in events} == {candidate["source_id"]}
    assert {event.session_id for event in events} == {candidate["session_id"]}


@requires_data
def test_public_audi_shared_fuel_model_matches_frozen_candidate_receipt(
    public_shared_fuel_report,
):
    candidate = ASSET["provisional_shared_fuel_model_receipt"]
    report = public_shared_fuel_report

    assert report["fuel_replay_sha256"] == candidate["fuel_replay_sha256"]
    assert report["model_semantic_sha256"] == candidate["model_semantic_sha256"]
    assert report["model_output_sha256"] == candidate["model_output_sha256"]
    assert report["scenario_sha256"] == candidate["scenario_sha256"]
    assert report["normalized_input_receipt"] == candidate["normalized_input_receipt"]
    assert report["pipeline"] == candidate["pipeline"]
    assert report["lap_receipt"] == candidate["lap_receipt"]
    assert report["quality_gate"] == {"reasons": [], "status": "PASS"}
    assert report["capabilities"]["fuel_model_shadow"]["status"] == "PASS"
    assert report["capabilities"]["race_recommendation"]["status"] == "BLOCKED"
    assert report["capabilities"]["opponent_fuel"]["provenance"] == "UNKNOWN"
    assert report["capabilities"]["current_tire_wear"]["provenance"] == "UNKNOWN"
    assert report["capabilities"]["traffic_model"]["provenance"] == "UNKNOWN"
    fuel = report["model_output"]
    assert fuel["burn"]["accepted_laps"] == 15
    assert fuel["burn"]["conservative_l_per_lap"] == pytest.approx(3.9986486434936523)
    assert fuel["conservative_fuel_to_end_l"]["value"] == pytest.approx(40.98648643493652)
    assert fuel["minimum_pit_stops"]["value"] == 1
    assert fuel["next_pit_window"] == {
        "earliest_lap_from_now": 0,
        "label": "estimated",
        "latest_lap_from_now": 4,
    }
    assert report["recommendations"][0]["confidence"] == "LOW"
    assert report["recommendations"][0]["confidence_basis"]["historical_burn_stability"] == ("HIGH")


@requires_data
def test_public_audi_shared_driving_model_matches_frozen_candidate_receipt(
    public_shared_driving_report,
):
    candidate = ASSET["provisional_shared_driving_model_receipt"]
    report = public_shared_driving_report

    for field in (
        "contract_version",
        "driving_context",
        "driving_replay_sha256",
        "input_provenance_sha256",
        "lap_receipt",
        "model_output_sha256",
        "model_semantic_sha256",
        "normalized_input_receipt",
        "pipeline",
        "quality_gate",
        "readiness_status",
        "semantic_input_receipt",
    ):
        assert report[field] == candidate[field]

    model = report["model_output"]
    expected = candidate["model_summary"]
    assert model["status"] == expected["status"] == "READY"
    assert model["eligible_lap_ordinals"] == expected["eligible_lap_ordinals"]
    assert model["reference"]["lap_ordinal"] == expected["reference_lap_ordinal"]
    assert len(model["corners"]) == expected["corner_count"]
    assert len(model["diagnoses"]) == expected["diagnosis_count"]
    assert all(item["closed"] for item in model["delta_closures"])
    assert [item["recommendation_id"] for item in report["recommendations"]] == (
        expected["diagnosis_ids"]
    )
    assert report["capabilities"]["driving_model_shadow"]["status"] == "PASS"
    for capability in (
        "curb_guidance",
        "current_tire_wear",
        "personalized_coaching",
        "traffic_model",
    ):
        assert report["capabilities"][capability]["status"] == "SKIP"
        assert report["capabilities"][capability]["provenance"] == "UNKNOWN"
    assert report["capabilities"]["race_coaching"]["status"] == "BLOCKED"
    assert report["series_evidence"]["incident_source_field"] == ("PlayerCarMyIncidentCount")
    for incident_channel in (
        "PlayerCarMyIncidentCount",
        "PlayerCarDriverIncidentCount",
        "PlayerCarTeamIncidentCount",
    ):
        assert report["series_evidence"]["missing_channel_sample_counts"][incident_channel] == 0
    recommendation = report["recommendations"][0]
    assert recommendation["claim_level"] == "descriptive"
    assert recommendation["practice_only"] is True
    assert recommendation["executable"] is False
    assert recommendation["confidence_basis"] == {
        "causal_validity": "NOT_CLAIMED",
        "external_validity": "UNKNOWN",
    }
    assert all(
        evidence_id.startswith(report["input_provenance_sha256"] + ":")
        for evidence_id in recommendation["evidence_lap_ids"]
    )


@requires_data
def test_public_audi_shadow_and_shared_paths_agree_on_fuel_plan_semantics(
    public_shadow_report,
    public_shared_fuel_report,
):
    shadow_plan = next(
        item
        for item in public_shadow_report["recommendations"]
        if item["kind"] == "FUEL_PLAN_CANDIDATE"
    )
    shared_plan = public_shared_fuel_report["recommendations"][0]
    semantic_fields = (
        "action",
        "claim_level",
        "confidence",
        "confidence_basis",
        "executable",
        "kind",
        "practice_only",
        "recommendation_id",
        "scenario_sha256",
        "status",
    )

    assert {key: shadow_plan[key] for key in semantic_fields} == {
        key: shared_plan[key] for key in semantic_fields
    }
    assert [item.rsplit(":", 1)[-1] for item in shadow_plan["evidence_ids"]] == [
        item.rsplit(":", 1)[-1] for item in shared_plan["evidence_ids"]
    ]
