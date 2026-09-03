from __future__ import annotations

import copy

import pytest

from iracing_ai_engineer.driving_labels import (
    APPROVED,
    CANDIDATE_NOT_GOLDEN,
    DRIVING_LABEL_COMPARATOR_VERSION,
    DRIVING_LABEL_REGRESSION_CONTRACT_VERSION,
    HUMAN_REVIEW_ATTESTATION,
    PENDING_HUMAN_REVIEW,
    SELF_ATTESTED_NOT_AUTHENTICATED,
    WAIT_HUMAN_AUTHENTICATION,
    DrivingLabelsError,
    build_driving_label_candidate,
    canonical_sha256,
    regress_driving_labels,
    seal_driving_labels,
    validate_driving_labels,
)

TRACK_LENGTH_MM = 1_000_000
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


def _rehash_replay(payload: dict[str, object]) -> dict[str, object]:
    replay = copy.deepcopy(payload)
    input_evidence = replay["input_evidence"]
    assert isinstance(input_evidence, dict)
    context = replay["driving_context"]
    assert isinstance(context, dict)
    context["source_binding_sha256"] = canonical_sha256(input_evidence)
    context["context_sha256"] = canonical_sha256(
        {key: value for key, value in context.items() if key != "context_sha256"}
    )
    replay["driving_context_sha256"] = context["context_sha256"]
    pipeline = replay["pipeline"]
    assert isinstance(pipeline, dict)
    pipeline["pipeline_sha256"] = canonical_sha256(
        {key: value for key, value in pipeline.items() if key != "pipeline_sha256"}
    )
    replay["input_provenance_sha256"] = canonical_sha256(
        {
            "driving_context": context,
            "event_receipt": replay["event_receipt"],
            "input_evidence": input_evidence,
            "input_kind": replay["input_kind"],
            "normalized_input_receipt": replay["normalized_input_receipt"],
        }
    )
    model_output = replay["model_output"]
    assert isinstance(model_output, dict)
    replay["model_output_sha256"] = canonical_sha256(model_output)
    replay["model_semantic_sha256"] = canonical_sha256(
        {
            "driving_context": {
                "contract_version": context["contract_version"],
                "source_field": context["source_field"],
                "track_length_mm": context["track_length_mm"],
            },
            "lap_receipt": replay["lap_receipt"],
            "model_output": model_output,
            "pipeline": replay["pipeline"],
            "quality_gate": replay["quality_gate"],
            "readiness_status": replay["readiness_status"],
            "semantic_input_receipt": replay["semantic_input_receipt"],
        }
    )
    replay["driving_replay_sha256"] = canonical_sha256(
        {key: replay[key] for key in _REPLAY_BINDING_KEYS}
    )
    return replay


def _replay(*, absent_brake: bool = False) -> dict[str, object]:
    corners = [
        {
            "corner_id": "C01",
            "brake_start_m": 100.0,
            "exit_m": 250.0,
        },
        {
            "corner_id": "C02",
            "brake_start_m": 400.0,
            "exit_m": 550.0,
        },
        {
            "corner_id": "C08",
            "brake_start_m": 800.0,
            "exit_m": 990.0,
        },
    ]
    metrics = [
        {
            "corner_id": "C01",
            "lap_ordinal": 11,
            "brake_onset_m": None if absent_brake else 110.0,
            "apex_m": 180.0,
            "throttle_pickup_m": 200.0,
        },
        {
            "corner_id": "C02",
            "lap_ordinal": 11,
            # A per-lap onset may precede the group-derived window start.
            "brake_onset_m": 399.0,
            "apex_m": 500.0,
            "throttle_pickup_m": 510.0,
        },
        {
            "corner_id": "C08",
            "lap_ordinal": 11,
            "brake_onset_m": 800.0,
            "apex_m": 900.0,
            # The contract must not assume pickup always follows min-speed apex.
            "throttle_pickup_m": 850.0,
        },
    ]
    model_output: dict[str, object] = {
        "status": "READY",
        "reference": {"lap_ordinal": 11},
        "corners": corners,
        "corner_metrics": metrics,
    }
    input_evidence = {
        "source_id": "fixture-source",
        "session_id": "fixture-session",
        "source_sha256": "3" * 64,
    }
    context_material = {
        "availability": "AVAILABLE",
        "contract_version": "track-context-v1",
        "provenance": "IBT_SAME_HANDLE_SESSION_INFO",
        "source_binding_sha256": canonical_sha256(input_evidence),
        "source_field": "WeekendInfo.TrackLength",
        "status": "VERIFIED",
        "track_length_mm": TRACK_LENGTH_MM,
    }
    context_sha256 = canonical_sha256(context_material)
    pipeline_material: dict[str, object] = {
        "driving_algorithm_version": "distance-driving-v1",
        "driving_config": {"grid_step_m": 1.0},
        "normalized_telemetry_contract_version": "normalized-telemetry-v3",
    }
    pipeline = {
        **pipeline_material,
        "pipeline_sha256": canonical_sha256(pipeline_material),
    }
    normalized_input_receipt = {
        "contract_version": "normalized-telemetry-v3",
        "sample_count": 1_000,
        "samples_sha256": "2" * 64,
    }
    event_receipt = {"contract_version": "telemetry-events-v1", "sample_count": 1_000}
    lap_receipt = {"algorithm_version": "distance-wrap-v2", "lap_count": 3}
    semantic_input_receipt = {
        "contract_version": "driving-semantic-input-v1",
        "sample_count": 1_000,
        "samples_sha256": "8" * 64,
    }
    driving_context = {
        **context_material,
        "context_sha256": context_sha256,
    }
    input_provenance_sha256 = canonical_sha256(
        {
            "driving_context": driving_context,
            "event_receipt": event_receipt,
            "input_evidence": input_evidence,
            "input_kind": "ibt",
            "normalized_input_receipt": normalized_input_receipt,
        }
    )
    quality_gate = {"status": "PASS", "reasons": []}
    model_semantic_sha256 = canonical_sha256(
        {
            "driving_context": {
                "contract_version": context_material["contract_version"],
                "source_field": context_material["source_field"],
                "track_length_mm": TRACK_LENGTH_MM,
            },
            "lap_receipt": lap_receipt,
            "model_output": model_output,
            "pipeline": pipeline,
            "quality_gate": quality_gate,
            "readiness_status": "PASS",
            "semantic_input_receipt": semantic_input_receipt,
        }
    )
    replay = {
        "capabilities": {},
        "contract_version": "driving-model-replay-v1",
        "readiness_status": "PASS",
        "quality_gate": quality_gate,
        "driving_context": driving_context,
        "driving_context_sha256": context_sha256,
        "pipeline": pipeline,
        "normalized_input_receipt": normalized_input_receipt,
        "input_kind": "ibt",
        "input_evidence": input_evidence,
        "event_receipt": event_receipt,
        "lap_receipt": lap_receipt,
        "semantic_input_receipt": semantic_input_receipt,
        "input_provenance_sha256": input_provenance_sha256,
        "model_output": model_output,
        "model_output_sha256": canonical_sha256(model_output),
        "model_semantic_sha256": model_semantic_sha256,
        "recommendations": [],
        "series_evidence": {},
    }
    replay["driving_replay_sha256"] = canonical_sha256(
        {key: replay[key] for key in _REPLAY_BINDING_KEYS}
    )
    return replay


def _candidate(replay: dict[str, object] | None = None) -> dict[str, object]:
    return build_driving_label_candidate(
        replay or _replay(),
        label_set_id="audi-spa-v1",
        car_key="audir8lmsevo2gt3",
        track_key="spa",
        layout_key="grand-prix-pit",
    )


def _human_label(
    proposal: dict[str, object],
    *,
    ordinal: int,
    absent_brake: bool = False,
) -> dict[str, object]:
    window = proposal["detected_window"]
    events = proposal["events"]
    assert isinstance(window, dict)
    assert isinstance(events, dict)
    brake = (
        {"expectation": "ABSENT", "expected_mm": None, "tolerance_mm": None}
        if absent_brake
        else {
            "expectation": "PRESENT",
            "expected_mm": events["brake_onset_mm"],
            "tolerance_mm": 5_000,
        }
    )
    return {
        "label_id": f"spa-gp-{ordinal:02d}",
        "ordinal": ordinal,
        "source_proposal_id": proposal["proposal_id"],
        "decision": "CONFIRMED",
        "display_name": None,
        "detected_window": {
            "start": {
                "expected_mm": window["start_mm"],
                "tolerance_mm": 5_000,
            },
            "end": {
                "expected_mm": window["end_mm"],
                "tolerance_mm": 5_000,
            },
            "wraps_start_finish": window["wraps_start_finish"],
        },
        "events": {
            "brake_onset": brake,
            "apexes": [
                {
                    "expected_mm": events["apex_mm"],
                    "tolerance_mm": 5_000,
                }
            ],
            "throttle_pickup": {
                "expectation": "PRESENT",
                "expected_mm": events["throttle_pickup_mm"],
                "tolerance_mm": 5_000,
            },
        },
        "annotation_evidence": [
            {
                "kind": "BLIND_TELEMETRY_TRACE_REVIEW",
                "artifact_sha256": "6" * 64,
                "review_passes": 2,
            }
        ],
    }


def _finalize_self_attested_approved(
    payload: dict[str, object]
) -> dict[str, object]:
    """Finalize public checksums without claiming reviewer authentication."""

    finalized = copy.deepcopy(payload)
    candidate_material = {
        "candidate_basis": finalized["candidate_basis"],
        "contract_version": finalized["contract_version"],
        "label_set_id": finalized["label_set_id"],
        "proposals": finalized["proposals"],
        "revision": finalized["revision"],
        "subject": finalized["subject"],
        "tolerance_policy": finalized["tolerance_policy"],
    }
    candidate_sha256 = canonical_sha256(candidate_material)
    assert candidate_sha256 == finalized["candidate_payload_sha256"]
    labels_material = {
        "candidate_payload_sha256": candidate_sha256,
        "contract_version": finalized["contract_version"],
        "human_labels": finalized["human_labels"],
        "label_set_id": finalized["label_set_id"],
        "revision": finalized["revision"],
        "subject": finalized["subject"],
        "tolerance_policy": finalized["tolerance_policy"],
    }
    labels_sha256 = canonical_sha256(labels_material)
    finalized["labels_content_sha256"] = labels_sha256
    review = finalized["review"]
    assert isinstance(review, dict)
    review.update(
        {
            "status": APPROVED,
            "authenticity_status": SELF_ATTESTED_NOT_AUTHENTICATED,
            "reviewer_id": "human-reviewer-01",
            "reviewed_at_utc": "2026-08-08T12:00:00Z",
            "method": "two-pass blind telemetry trace review",
            "evidence_artifact_sha256": "7" * 64,
            "candidate_hidden_during_first_pass": True,
            "attestation": HUMAN_REVIEW_ATTESTATION,
            "candidate_payload_sha256": candidate_sha256,
            "labels_content_sha256": labels_sha256,
            "decision_reason": None,
        }
    )
    finalized["review_sha256"] = canonical_sha256(review)
    finalized["artifact_sha256"] = canonical_sha256(
        {key: value for key, value in finalized.items() if key != "artifact_sha256"}
    )
    return validate_driving_labels(finalized)


def _approved(replay: dict[str, object] | None = None) -> dict[str, object]:
    selected_replay = replay or _replay()
    payload = _candidate(selected_replay)
    proposals = payload["proposals"]
    assert isinstance(proposals, list)
    payload["human_labels"] = [
        _human_label(
            proposal,
            ordinal=index,
            absent_brake=(
                index == 1
                and selected_replay["model_output"]["corner_metrics"][0][
                    "brake_onset_m"
                ]
                is None
            ),
        )
        for index, proposal in enumerate(proposals, start=1)
    ]
    return _finalize_self_attested_approved(payload)


def test_candidate_is_deterministic_pending_only_and_not_golden():
    first = _candidate()
    second = _candidate()

    assert first == second
    assert first["review"]["status"] == PENDING_HUMAN_REVIEW
    assert first["candidate_basis"]["status"] == CANDIDATE_NOT_GOLDEN
    assert first["human_labels"] == []
    assert first["labels_content_sha256"] is None
    assert first["review_sha256"] is None
    assert len(first["proposals"]) == 3
    assert validate_driving_labels(first) == first


def test_candidate_freezes_window_and_labeled_lap_events_without_order_assumptions():
    candidate = _candidate()
    first, second, c08 = candidate["proposals"]

    assert first["detected_window"] == {
        "start_mm": 100_000,
        "end_mm": 250_000,
        "wraps_start_finish": False,
    }
    assert second["events"]["brake_onset_mm"] == 399_000
    assert second["detected_window"]["start_mm"] == 400_000
    assert c08["events"] == {
        "brake_onset_mm": 800_000,
        "apex_mm": 900_000,
        "throttle_pickup_mm": 850_000,
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["model_output"]["corners"][0].update(
                brake_start_m=101.0
            ),
            "model output hash mismatch",
        ),
        (
            lambda value: value.update(model_semantic_sha256="9" * 64),
            "model semantic hash mismatch",
        ),
        (
            lambda value: value["input_evidence"].update(source_sha256="9" * 64),
            "not bound to input evidence",
        ),
    ],
)
def test_candidate_generation_rejects_unbound_replay_hashes(mutation, message):
    replay = _replay()
    mutation(replay)

    with pytest.raises(DrivingLabelsError, match=message):
        _candidate(replay)


def test_pending_candidate_cannot_self_approve_or_carry_human_labels():
    candidate = _candidate()
    candidate["review"]["status"] = APPROVED
    with pytest.raises(DrivingLabelsError, match="only accepts pending candidates"):
        seal_driving_labels(candidate)

    pending = _candidate()
    pending["human_labels"] = [_human_label(pending["proposals"][0], ordinal=1)]
    with pytest.raises(DrivingLabelsError, match="pending candidate cannot carry human labels"):
        seal_driving_labels(pending)


def test_approved_validation_requires_blind_human_attestation_and_hashes():
    approved = _approved()
    assert validate_driving_labels(approved, require_approved=True) == approved
    assert (
        approved["review"]["authenticity_status"]
        == SELF_ATTESTED_NOT_AUTHENTICATED
    )
    with pytest.raises(DrivingLabelsError, match=WAIT_HUMAN_AUTHENTICATION):
        validate_driving_labels(approved, require_trusted=True)

    missing_attestation = copy.deepcopy(approved)
    missing_attestation["review"]["attestation"] = None
    missing_attestation["review_sha256"] = canonical_sha256(
        missing_attestation["review"]
    )
    missing_attestation["artifact_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in missing_attestation.items()
            if key != "artifact_sha256"
        }
    )
    with pytest.raises(DrivingLabelsError, match="attestation"):
        validate_driving_labels(missing_attestation)

    forged_authentication = copy.deepcopy(approved)
    forged_authentication["review"]["authenticity_status"] = "AUTHENTICATED"
    forged_authentication["review_sha256"] = canonical_sha256(
        forged_authentication["review"]
    )
    forged_authentication["artifact_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in forged_authentication.items()
            if key != "artifact_sha256"
        }
    )
    with pytest.raises(DrivingLabelsError, match="SELF_ATTESTED_NOT_AUTHENTICATED"):
        validate_driving_labels(forged_authentication)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(unexpected=True), "keys are invalid"),
        (
            lambda value: value["proposals"][0].update(ordinal=True),
            "plain integer",
        ),
        (
            lambda value: value["proposals"][1].update(ordinal=1),
            "contiguous and ordered",
        ),
        (
            lambda value: value["candidate_basis"].update(model_output_sha256="0" * 64),
            "candidate payload hash mismatch",
        ),
    ],
)
def test_strict_schema_and_candidate_hash_reject_tampering(mutation, message):
    candidate = _candidate()
    mutation(candidate)
    with pytest.raises(DrivingLabelsError, match=message):
        validate_driving_labels(candidate)


def test_approved_validation_rejects_unknown_proposal_and_excessive_tolerance():
    approved = _approved()
    unknown = copy.deepcopy(approved)
    unknown["human_labels"][0]["source_proposal_id"] = "candidate:missing"
    with pytest.raises(DrivingLabelsError, match="unknown proposal"):
        _finalize_self_attested_approved(unknown)

    excessive = copy.deepcopy(approved)
    excessive["human_labels"][0]["events"]["brake_onset"][
        "tolerance_mm"
    ] = 10_001
    with pytest.raises(DrivingLabelsError, match="at most 10000"):
        _finalize_self_attested_approved(excessive)


def test_pending_regression_waits_and_approved_exact_regression_passes():
    replay = _replay()
    waiting = regress_driving_labels(_candidate(replay), replay)
    passed = regress_driving_labels(_approved(replay), replay)

    assert waiting["status"] == "WAIT_HUMAN_LABELS"
    assert waiting["reasons"] == ["LABEL_SET_NOT_APPROVED"]
    assert passed["contract_version"] == DRIVING_LABEL_REGRESSION_CONTRACT_VERSION
    assert passed["comparator_version"] == DRIVING_LABEL_COMPARATOR_VERSION
    assert passed["comparator_status"] == "PASS"
    assert passed["status"] == WAIT_HUMAN_AUTHENTICATION
    assert passed["trusted_regression_status"] == WAIT_HUMAN_AUTHENTICATION
    assert passed["human_review_authenticity"] == {
        "authenticated": False,
        "reasons": [SELF_ATTESTED_NOT_AUTHENTICATED],
        "status": WAIT_HUMAN_AUTHENTICATION,
    }
    assert passed["summary"] == {
        "expected_corner_count": 3,
        "predicted_corner_count": 3,
        "passed_field_count": 15,
        "failed_field_count": 0,
    }


def test_regression_tolerance_is_inclusive_and_plus_one_fails_and_rehashes():
    baseline = _replay()
    labels = _approved(baseline)
    within = copy.deepcopy(baseline)
    within["model_output"]["corner_metrics"][0]["brake_onset_m"] = 115.0
    within = _rehash_replay(within)
    outside = copy.deepcopy(baseline)
    outside["model_output"]["corner_metrics"][0]["brake_onset_m"] = 115.001
    outside = _rehash_replay(outside)

    within_result = regress_driving_labels(labels, within)
    outside_result = regress_driving_labels(labels, outside)

    assert within_result["comparator_status"] == "PASS"
    assert within_result["status"] == WAIT_HUMAN_AUTHENTICATION
    assert outside_result["status"] == "FAIL"
    brake = outside_result["corner_results"][0]["field_results"][2]
    assert brake["error_mm"] == 5_001
    assert brake["status"] == "FAIL"
    assert (
        within_result["regression_result_sha256"]
        != outside_result["regression_result_sha256"]
    )


def test_regression_uses_circular_distance_at_start_finish():
    replay = _replay()
    labels = _approved(replay)
    labels["human_labels"][2]["events"]["throttle_pickup"].update(
        expected_mm=999_000,
        tolerance_mm=3_000,
    )
    labels = _finalize_self_attested_approved(labels)
    evaluated = copy.deepcopy(replay)
    evaluated["model_output"]["corner_metrics"][2]["throttle_pickup_m"] = 1.0
    evaluated = _rehash_replay(evaluated)

    result = regress_driving_labels(labels, evaluated)

    throttle = result["corner_results"][2]["field_results"][3]
    assert throttle["error_mm"] == 2_000
    assert result["comparator_status"] == "PASS"
    assert result["status"] == WAIT_HUMAN_AUTHENTICATION


def test_absent_event_is_a_real_negative_label():
    replay = _replay(absent_brake=True)
    labels = _approved(replay)

    passed = regress_driving_labels(labels, replay)
    present = copy.deepcopy(replay)
    present["model_output"]["corner_metrics"][0]["brake_onset_m"] = 110.0
    present = _rehash_replay(present)
    failed = regress_driving_labels(labels, present)

    assert passed["comparator_status"] == "PASS"
    assert passed["status"] == WAIT_HUMAN_AUTHENTICATION
    assert failed["status"] == "FAIL"
    assert failed["corner_results"][0]["field_results"][2]["expectation"] == (
        "ABSENT"
    )


def test_corner_count_and_fixed_order_fail_without_nearest_neighbor_remapping():
    replay = _replay()
    labels = _approved(replay)
    missing = copy.deepcopy(replay)
    missing["model_output"]["corners"].pop(1)
    missing = _rehash_replay(missing)
    reordered = copy.deepcopy(replay)
    reordered["model_output"]["corners"][0:2] = reversed(
        reordered["model_output"]["corners"][0:2]
    )
    reordered = _rehash_replay(reordered)

    missing_result = regress_driving_labels(labels, missing)
    reordered_result = regress_driving_labels(labels, reordered)

    assert missing_result["status"] == "FAIL"
    assert "CORNER_COUNT_MISMATCH" in missing_result["reasons"]
    assert reordered_result["status"] == "FAIL"
    assert reordered_result["corner_results"][0]["model_corner_id"] == "C02"


def test_multi_apex_label_fails_explicitly_against_single_apex_model():
    replay = _replay()
    labels = _approved(replay)
    labels["human_labels"][0]["events"]["apexes"].append(
        {"expected_mm": 190_000, "tolerance_mm": 5_000}
    )
    labels = _finalize_self_attested_approved(labels)

    result = regress_driving_labels(labels, replay)

    assert result["status"] == "FAIL"
    assert result["corner_results"][0]["reasons"] == ["APEX_COUNT_MISMATCH"]


def test_model_and_label_content_both_bind_regression_hash():
    replay = _replay()
    labels = _approved(replay)
    first = regress_driving_labels(labels, replay)

    changed_model = copy.deepcopy(replay)
    changed_model["model_output"]["corner_metrics"][0]["brake_onset_m"] = 111.0
    changed_model = _rehash_replay(changed_model)
    second = regress_driving_labels(labels, changed_model)

    changed_labels = copy.deepcopy(labels)
    changed_labels["human_labels"][0]["events"]["brake_onset"][
        "expected_mm"
    ] = 111_000
    changed_labels = _finalize_self_attested_approved(changed_labels)
    third = regress_driving_labels(changed_labels, changed_model)

    assert (
        first["comparator_status"]
        == second["comparator_status"]
        == third["comparator_status"]
        == "PASS"
    )
    assert (
        first["status"]
        == second["status"]
        == third["status"]
        == WAIT_HUMAN_AUTHENTICATION
    )
    assert len(
        {
            first["regression_result_sha256"],
            second["regression_result_sha256"],
            third["regression_result_sha256"],
        }
    ) == 3


def test_seal_refuses_to_refresh_or_modify_an_approved_artifact():
    approved = _approved()
    with pytest.raises(DrivingLabelsError, match="only accepts pending candidates"):
        seal_driving_labels(approved)

    modified = copy.deepcopy(approved)
    modified["human_labels"][0]["events"]["brake_onset"][
        "expected_mm"
    ] = 111_000
    with pytest.raises(DrivingLabelsError, match="only accepts pending candidates"):
        seal_driving_labels(modified)


@pytest.mark.parametrize("identity_field", ["source_id", "session_id"])
def test_regression_fails_for_a_fully_rehashed_different_source(identity_field):
    baseline = _replay()
    labels = _approved(baseline)
    evaluated = copy.deepcopy(baseline)
    evaluated["input_evidence"][identity_field] = f"different-{identity_field}"
    evaluated = _rehash_replay(evaluated)

    result = regress_driving_labels(labels, evaluated)

    assert result["status"] == "FAIL"
    assert result["reasons"] == ["PROVENANCE_MISMATCH"]
    assert result["corner_results"] == []
    assert result["baseline_comparison"][f"{identity_field}_match"] is False
    assert result["evaluated_input_identity"][identity_field] == (
        f"different-{identity_field}"
    )


def test_one_full_lap_model_coordinate_is_never_wrapped_to_zero():
    baseline = _replay()
    labels = _approved(baseline)
    evaluated = copy.deepcopy(baseline)
    evaluated["model_output"]["corner_metrics"][0]["brake_onset_m"] = 1_000.0
    evaluated = _rehash_replay(evaluated)

    result = regress_driving_labels(labels, evaluated)

    assert result["status"] == "FAIL"
    assert "MODEL_COORDINATE_INVALID:brake_onset_m" in (
        result["corner_results"][0]["reasons"]
    )
    assert result["corner_results"][0]["field_results"][2]["status"] == "FAIL"

    invalid_candidate_replay = copy.deepcopy(baseline)
    invalid_candidate_replay["model_output"]["corners"][0][
        "brake_start_m"
    ] = 1_000.0
    invalid_candidate_replay = _rehash_replay(invalid_candidate_replay)
    with pytest.raises(DrivingLabelsError, match="must be at most 999999"):
        _candidate(invalid_candidate_replay)


@pytest.mark.parametrize(
    ("metric_key", "absent_brake", "field_index"),
    [
        ("brake_onset_m", True, 2),
        ("throttle_pickup_m", False, 3),
    ],
)
def test_missing_event_key_is_not_treated_as_an_explicit_absent_value(
    metric_key, absent_brake, field_index
):
    baseline = _replay(absent_brake=absent_brake)
    labels = _approved(baseline)
    evaluated = copy.deepcopy(baseline)
    del evaluated["model_output"]["corner_metrics"][0][metric_key]
    evaluated = _rehash_replay(evaluated)

    result = regress_driving_labels(labels, evaluated)

    assert result["status"] == "FAIL"
    assert f"REQUIRED_MODEL_FIELD_MISSING:{metric_key}" in (
        result["corner_results"][0]["reasons"]
    )
    assert result["corner_results"][0]["field_results"][field_index][
        "status"
    ] == "FAIL"


def test_approved_mapping_cannot_swap_proposal_ids_and_preserve_track_order():
    swapped = copy.deepcopy(_approved())
    first = swapped["human_labels"][0]["source_proposal_id"]
    second = swapped["human_labels"][1]["source_proposal_id"]
    swapped["human_labels"][0]["source_proposal_id"] = second
    swapped["human_labels"][1]["source_proposal_id"] = first

    with pytest.raises(DrivingLabelsError, match="preserve proposal track order"):
        _finalize_self_attested_approved(swapped)


def test_pending_regression_receipt_binds_labels_and_evaluated_replay_identity():
    first_replay = _replay()
    first_labels = _candidate(first_replay)
    first = regress_driving_labels(first_labels, first_replay)

    second_replay = copy.deepcopy(first_replay)
    second_replay["model_output"]["corner_metrics"][0]["brake_onset_m"] = 111.0
    second_replay = _rehash_replay(second_replay)
    second_labels = _candidate(second_replay)
    second = regress_driving_labels(second_labels, second_replay)

    assert first["status"] == second["status"] == "WAIT_HUMAN_LABELS"
    assert first["label_artifact_sha256"] == first_labels["artifact_sha256"]
    assert first["candidate_payload_sha256"] == (
        first_labels["candidate_payload_sha256"]
    )
    assert first["evaluated_driving_replay_sha256"] == (
        first_replay["driving_replay_sha256"]
    )
    assert first["regression_result_sha256"] != second["regression_result_sha256"]


def test_replay_top_hash_and_exact_top_level_keys_are_enforced():
    tampered = _replay()
    tampered["recommendations"].append({"unbound": True})
    with pytest.raises(DrivingLabelsError, match="driving replay hash mismatch"):
        _candidate(tampered)

    extra = _replay()
    extra["unknown_top_level"] = True
    with pytest.raises(DrivingLabelsError, match="top-level keys are invalid"):
        _candidate(extra)


@pytest.mark.parametrize("contract_location", ["receipt", "pipeline"])
def test_replay_rejects_non_current_normalized_contract(contract_location):
    replay = _replay()
    if contract_location == "receipt":
        replay["normalized_input_receipt"]["contract_version"] = (
            "normalized-telemetry-v2"
        )
    else:
        replay["pipeline"]["normalized_telemetry_contract_version"] = (
            "normalized-telemetry-v2"
        )
    replay = _rehash_replay(replay)

    with pytest.raises(DrivingLabelsError, match="normalized telemetry contract|contract version"):
        _candidate(replay)
