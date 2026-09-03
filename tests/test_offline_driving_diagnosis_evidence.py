from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import iracing_ai_engineer.driving_diagnosis as _PACKAGE_MODULE

_SPEC = importlib.util.spec_from_file_location(
    "build_offline_driving_diagnosis_evidence",
    Path("scripts/build_offline_driving_diagnosis_evidence.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

DiagnosisEvidenceError = _MODULE.DiagnosisEvidenceError
build_diagnosis_evidence = _MODULE.build_diagnosis_evidence
canonical_sha256 = _MODULE.canonical_sha256

REAL_REPLAY = Path("data/derived/audi-spa-driving-replay-v1.json")
if not REAL_REPLAY.is_file():
    pytest.skip(
        "REQUIRES_DATA: frozen public Audi/Spa derived replay is absent",
        allow_module_level=True,
    )
REAL_BYTES = REAL_REPLAY.read_bytes()
REAL_SERIALIZED_SHA256 = "b1825535c80d316b5379ae646c9710383050637ff7c335e9fe056a1d29010adf"
REAL_CANONICAL_SHA256 = "a54567a532f25693c1b82a8eacfbf11bc82fb7af94d1fdbb42eb774cb004b1ea"
REAL_REPLAY_SHA256 = "c5a8f19f156c57c3951e112df24ad3e3f07956961b78c68fe972a534955ebb82"
REAL_MODEL_OUTPUT_SHA256 = "f7a7165b19dfa08f1576b3f2e495cfbedb2011aa32317b91d3eb725967af3195"
REAL_MODEL_SEMANTIC_SHA256 = "74f6f52d5743260cbdcedaa59a0e0620afb1d8c8987195009e31f7cb86399df6"
REAL_PIPELINE_SHA256 = "f499f2dbb34fafbe5a1428c3336f7370fbd29bbf0154e701c4301767bc93c90e"
REAL_SOURCE_SHA256 = "754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36"

_REPLAY_BINDING_KEYS = _MODULE._CORNER_VERIFIER._REPLAY_BINDING_KEYS


def test_compatibility_wrapper_reexports_the_complete_package_surface() -> None:
    exported = {
        name: value
        for name, value in vars(_PACKAGE_MODULE).items()
        if not name.startswith("__")
    }

    assert exported
    assert all(vars(_MODULE).get(name) is value for name, value in exported.items())
    assert (
        _MODULE.build_diagnosis_evidence.__module__
        == "iracing_ai_engineer.driving_diagnosis"
    )
    assert _MODULE.importlib.util is importlib.util


def test_package_zipimport_isolation_does_not_depend_on_scripts(tmp_path: Path) -> None:
    wheel = tmp_path / "iracing_ai_engineer-0.1.0-py3-none-any.whl"
    package_root = Path("src/iracing_ai_engineer")
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("__init__.py", "corner_cards.py", "driving_diagnosis.py"):
            archive.write(package_root / name, f"iracing_ai_engineer/{name}")

    code = f"""
import hashlib
import json
from pathlib import Path
from iracing_ai_engineer import corner_cards, driving_diagnosis
root = Path({str(Path.cwd())!r})
source = (root / 'data/derived/audi-spa-driving-replay-v1.json').read_bytes()
corner = corner_cards._canonical_json(
    corner_cards.build_corner_cards(json.loads(source)), newline=True
)
diagnosis = driving_diagnosis._canonical_json(
    driving_diagnosis.build_diagnosis_evidence(source), newline=True
)
print(json.dumps({{
    'corner_file': corner_cards.__file__,
    'corner_sha256': hashlib.sha256(corner).hexdigest(),
    'diagnosis_file': driving_diagnosis.__file__,
    'diagnosis_sha256': hashlib.sha256(diagnosis).hexdigest(),
    'relative_verifier': driving_diagnosis._CORNER_VERIFIER is corner_cards,
}}, sort_keys=True))
"""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(wheel)!r});" + code,
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    imported = json.loads(completed.stdout)

    assert imported == {
        "corner_file": str(wheel / "iracing_ai_engineer" / "corner_cards.py"),
        "corner_sha256": "ea569e10989fc614577a072fef367817c301db233bd776e731e3f9054813ef22",
        "diagnosis_file": str(wheel / "iracing_ai_engineer" / "driving_diagnosis.py"),
        "diagnosis_sha256": "5430af296eeca439cdd566fe1fd636c61e010569b0b9088bb526543670c2e482",
        "relative_verifier": True,
    }


def _serialize(replay: dict[str, object]) -> bytes:
    return json.dumps(
        replay,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _replay() -> dict[str, object]:
    payload = json.loads(REAL_BYTES)
    assert isinstance(payload, dict)
    return payload


def _rehash(replay: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(replay)
    digest = _MODULE._CORNER_VERIFIER.canonical_sha256
    event_receipt = result["event_receipt"]
    context = result["driving_context"]
    evidence = result["input_evidence"]
    pipeline = result["pipeline"]
    model = result["model_output"]
    capabilities = result["capabilities"]
    assert all(
        isinstance(value, dict)
        for value in (event_receipt, context, evidence, pipeline, model, capabilities)
    )
    event_receipt["receipt_sha256"] = digest(
        {key: value for key, value in event_receipt.items() if key != "receipt_sha256"}
    )
    context["source_binding_sha256"] = digest(evidence)
    context["context_sha256"] = digest(
        {key: value for key, value in context.items() if key != "context_sha256"}
    )
    result["driving_context_sha256"] = context["context_sha256"]
    pipeline["pipeline_sha256"] = digest(
        {key: value for key, value in pipeline.items() if key != "pipeline_sha256"}
    )
    result["input_provenance_sha256"] = digest(
        {
            "driving_context": context,
            "event_receipt": event_receipt,
            "input_evidence": evidence,
            "input_kind": result["input_kind"],
            "normalized_input_receipt": result["normalized_input_receipt"],
        }
    )
    eligible = model["eligible_lap_ordinals"]
    assert isinstance(eligible, list)
    driving_capability = capabilities["driving_model_shadow"]
    assert isinstance(driving_capability, dict)
    prefix = result["input_provenance_sha256"]
    driving_capability["evidence_ids"] = [
        f"{prefix}:distance-wrap-v2:lap:{ordinal}" for ordinal in eligible
    ]
    result["model_output_sha256"] = digest(model)
    result["model_semantic_sha256"] = digest(
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
    result["driving_replay_sha256"] = digest(
        {key: result[key] for key in _REPLAY_BINDING_KEYS}
    )
    return result


def _metric_map(replay: dict[str, object], corner_id: str) -> dict[int, dict[str, object]]:
    model = replay["model_output"]
    assert isinstance(model, dict)
    metrics = model["corner_metrics"]
    assert isinstance(metrics, list)
    return {
        int(item["lap_ordinal"]): item
        for item in metrics
        if isinstance(item, dict) and item["corner_id"] == corner_id
    }


def _audit(report: dict[str, object], code: str, corner_id: str) -> dict[str, object]:
    rules = report["rule_audits"]
    assert isinstance(rules, list)
    rule = next(item for item in rules if item["diagnosis"] == code)
    return next(item for item in rule["corners"] if item["corner_id"] == corner_id)


def _support_late_c02(replay: dict[str, object]) -> None:
    model = replay["model_output"]
    pipeline = replay["pipeline"]
    assert isinstance(model, dict) and isinstance(pipeline, dict)
    config = pipeline["driving_config"]
    assert isinstance(config, dict)
    metrics = _metric_map(replay, "C02")
    reference = metrics[11]
    for ordinal in (4, 5):
        item = metrics[ordinal]
        item["brake_onset_m"] = reference["brake_onset_m"] + config[
            "event_position_difference_m"
        ]
        item["brake_release_m"] = reference["brake_release_m"] + config[
            "event_position_difference_m"
        ]
        item["apex_speed_mps"] = reference["apex_speed_mps"] - config[
            "speed_difference_mps"
        ]
        item["throttle_pickup_m"] = reference["throttle_pickup_m"] + config[
            "event_position_difference_m"
        ]
        item["exit_speed_mps"] = reference["exit_speed_mps"] - config[
            "speed_difference_mps"
        ]


def _late_diagnosis(replay: dict[str, object], evidence: list[int]) -> dict[str, object]:
    metrics = _metric_map(replay, "C02")
    reference = metrics[11]
    support = [metrics[ordinal] for ordinal in evidence]
    loss = float(statistics.median(float(item["total_segment_delta_s"]) for item in support))
    comparisons = _MODULE._expected_comparisons(
        code=_MODULE.LATE_BRAKING_HURTS_EXIT,
        support=support,
        reference=reference,
    )
    return {
        "action": (
            "Brake slightly earlier and shorter, then release pressure earlier to "
            "prioritize minimum speed and the exit."
        ),
        "claim_level": "descriptive",
        "comparisons": comparisons,
        "confidence": "high" if len(evidence) >= 3 else "medium",
        "corner_id": "C02",
        "counterexample_lap_ordinals": [
            ordinal for ordinal in (2, 4, 5, 9, 10, 16) if ordinal not in evidence
        ],
        "diagnosis": _MODULE.LATE_BRAKING_HURTS_EXIT,
        "estimated_loss_median_s": loss,
        "evidence_lap_ordinals": evidence,
        "expected_gain_range_s": [
            round(max(0.01, loss * 0.4), 3),
            round(loss, 3),
        ],
        "practice_only": True,
    }


def _append_diagnosis(replay: dict[str, object], diagnosis: dict[str, object]) -> None:
    model = replay["model_output"]
    assert isinstance(model, dict)
    diagnoses = model["diagnoses"]
    assert isinstance(diagnoses, list)
    diagnoses.append(diagnosis)


def _walk_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value)
        for child in value.values():
            keys.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_walk_keys(child))
    return keys


def test_real_audi_exact_evidence_and_wait_boundaries() -> None:
    report = build_diagnosis_evidence(REAL_BYTES)

    assert report["summary"] == {
        "audited_corner_count": 8,
        "audited_rule_count": 2,
        "source_model_rule_consistency": "PASS",
        "status_counts": {
            "NOT_EVALUABLE_REFERENCE_GATE": 3,
            "NOT_EVALUABLE_REQUIRED_METRIC": 0,
            "NOT_OBSERVED": 11,
            "RULE_SUPPORT_PRESENT_SHADOW": 0,
            "WAIT_REPEATED_EVIDENCE": 2,
        },
    }
    for corner_id in report["reference"]["corner_ids"]:
        late = _audit(report, _MODULE.LATE_BRAKING_HURTS_EXIT, corner_id)
        assert late["evaluation_status"] == "NOT_OBSERVED"
        assert late["support_lap_ordinals"] == []

    expected_second = {
        "C01": ("NOT_OBSERVED", []),
        "C02": ("NOT_EVALUABLE_REFERENCE_GATE", []),
        "C03": ("WAIT_REPEATED_EVIDENCE", [2]),
        "C04": ("NOT_OBSERVED", []),
        "C05": ("NOT_OBSERVED", []),
        "C06": ("NOT_EVALUABLE_REFERENCE_GATE", []),
        "C07": ("WAIT_REPEATED_EVIDENCE", [9]),
        "C08": ("NOT_EVALUABLE_REFERENCE_GATE", []),
    }
    for corner_id, (status, laps) in expected_second.items():
        audit = _audit(report, _MODULE.THROTTLE_SECOND_LIFT, corner_id)
        assert audit["evaluation_status"] == status
        assert audit["support_lap_ordinals"] == laps
    second_audits = next(
        item
        for item in report["rule_audits"]
        if item["diagnosis"] == _MODULE.THROTTLE_SECOND_LIFT
    )["corners"]
    assert sum(item["support_count"] for item in second_audits) == 2
    assert not any(
        item["evaluation_status"] == "RULE_SUPPORT_PRESENT_SHADOW"
        for item in second_audits
    )


def test_real_c02_lap2_cannot_be_called_late_braking() -> None:
    report = build_diagnosis_evidence(REAL_BYTES)
    c02 = _audit(report, _MODULE.LATE_BRAKING_HURTS_EXIT, "C02")
    lap2 = next(item for item in c02["lap_evidence"] if item["lap_ordinal"] == 2)
    onset = next(item for item in lap2["predicates"] if item["predicate"] == "BRAKE_ONSET_LATER")

    assert onset == {
        "margin_to_pass": -73.0,
        "observed_value": 2188.0,
        "operator": ">=",
        "predicate": "BRAKE_ONSET_LATER",
        "reference_value": 2253.0,
        "required_boundary": 2261.0,
        "status": "FAIL",
        "unit": "m",
    }
    assert sum(item["status"] == "PASS" for item in lap2["predicates"]) == 6


def test_exact_schema_hashes_source_identity_and_no_promotion_fields() -> None:
    report = build_diagnosis_evidence(REAL_BYTES)
    assert set(report) == {
        "advisor_only",
        "claim_scope",
        "contract_version",
        "diagnosis_evidence_sha256",
        "executable",
        "execution_status",
        "input_receipt",
        "policy",
        "promotion_gates",
        "raw_telemetry_replayed",
        "recommendations",
        "reference",
        "rule_audits",
        "status",
        "summary",
    }
    assert report["diagnosis_evidence_sha256"] == canonical_sha256(
        {key: value for key, value in report.items() if key != "diagnosis_evidence_sha256"}
    )
    assert set(report["reference"]) == {
        "comparison_lap_ordinals",
        "corner_ids",
        "eligible_lap_ordinals",
        "reference_lap_ordinal",
    }
    assert set(report["summary"]) == {
        "audited_corner_count",
        "audited_rule_count",
        "source_model_rule_consistency",
        "status_counts",
    }
    assert set(report["policy"]) == {
        "feature_derivation",
        "late_braking_hurts_exit",
        "minimum_evidence_laps",
        "policy_sha256",
        "policy_version",
        "rule_codes",
        "throttle_second_lift",
    }
    for rule in report["rule_audits"]:
        assert set(rule) == {"corners", "diagnosis"}
        for corner in rule["corners"]:
            assert set(corner) == {
                "corner_id",
                "evaluation_status",
                "lap_evidence",
                "reference_gate",
                "reference_lap_ordinal",
                "required_support_count",
                "source_diagnosis_consistency",
                "source_diagnosis_present",
                "support_count",
                "support_lap_ordinals",
            }
            assert set(corner["reference_gate"]) == {"reasons", "status"}
            for lap in corner["lap_evidence"]:
                assert set(lap) == {
                    "all_predicates_pass",
                    "lap_ordinal",
                    "predicates",
                    "reasons",
                    "status",
                }
                for predicate in lap["predicates"]:
                    assert set(predicate) == {
                        "margin_to_pass",
                        "observed_value",
                        "operator",
                        "predicate",
                        "reference_value",
                        "required_boundary",
                        "status",
                        "unit",
                    }
    receipt = report["input_receipt"]
    assert set(receipt) == {
        "driving_context_sha256",
        "driving_replay_canonical_sha256",
        "driving_replay_serialized_sha256",
        "driving_replay_sha256",
        "input_provenance_sha256",
        "model_output_sha256",
        "model_semantic_sha256",
        "normalized_samples_sha256",
        "pipeline_sha256",
        "source_identity",
        "track_length_mm",
    }
    assert receipt["driving_replay_serialized_sha256"] == REAL_SERIALIZED_SHA256
    assert receipt["driving_replay_canonical_sha256"] == REAL_CANONICAL_SHA256
    assert receipt["driving_replay_sha256"] == REAL_REPLAY_SHA256
    assert receipt["model_output_sha256"] == REAL_MODEL_OUTPUT_SHA256
    assert receipt["model_semantic_sha256"] == REAL_MODEL_SEMANTIC_SHA256
    assert receipt["pipeline_sha256"] == REAL_PIPELINE_SHA256
    assert receipt["source_identity"] == {
        "authenticity_status": "HASHED_LOCAL_FILE_NOT_AUTHENTICATED",
        "completion_status": "COMPLETE",
        "input_evidence_sha256": canonical_sha256(_replay()["input_evidence"]),
        "input_kind": "ibt",
        "session_id": "public-fixture-2023-12-race",
        "source_content_sha256": REAL_SOURCE_SHA256,
        "source_content_sha256_field": "source_sha256",
        "source_id": "public-audi-r8-evo2-spa",
        "source_kind": "IBT_OFFLINE",
    }
    assert report["claim_scope"] == "DERIVED_RULE_AUDIT"
    assert report["raw_telemetry_replayed"] is False
    assert report["recommendations"] == []
    assert report["executable"] is False
    assert report["promotion_gates"] == {
        "a_b_validation": "WAIT_AB_PRACTICE",
        "condition_matching": "WAIT_CONDITION_DATA",
        "golden_corner": "CANDIDATE_NOT_GOLDEN",
        "human_labels": "WAIT_HUMAN_LABELS",
    }
    assert not {
        "action",
        "causal_claim",
        "confidence",
        "expected_gain_range_s",
        "expected_gain_s",
    }.intersection(_walk_keys(report))


def test_late_rule_exact_inclusive_boundaries() -> None:
    config = {
        "event_position_difference_m": 8.0,
        "minimum_carry_loss_s": 0.03,
        "minimum_loss_s": 0.04,
        "speed_difference_mps": 0.4,
    }
    reference = {
        "brake_onset_m": 100.0,
        "brake_release_m": 150.0,
        "apex_speed_mps": 30.0,
        "throttle_pickup_m": 200.0,
        "exit_speed_mps": 40.0,
    }
    observed = {
        "brake_onset_m": 108.0,
        "brake_release_m": 158.0,
        "apex_speed_mps": 29.6,
        "throttle_pickup_m": 208.0,
        "exit_speed_mps": 39.6,
        "carry_delta_s": 0.03,
        "total_segment_delta_s": 0.04,
    }

    predicates = _MODULE._late_predicates(observed, reference, config)
    assert all(item["status"] == "PASS" and item["margin_to_pass"] == 0.0 for item in predicates)

    outside = copy.deepcopy(observed)
    outside["brake_onset_m"] = 107.999999
    predicates = _MODULE._late_predicates(outside, reference, config)
    assert predicates[0]["status"] == "FAIL"


def test_second_lift_dynamic_early_boundary_and_exact_types() -> None:
    config = {"event_position_difference_m": 16.0, "minimum_loss_s": 0.04}
    reference = {"throttle_pickup_m": 100.0, "exit_speed_mps": 40.0}
    observed = {
        "throttle_pickup_m": 90.0,
        "second_lift": True,
        "exit_speed_mps": 40.2,
        "total_segment_delta_s": 0.04,
    }

    assert _MODULE._early_pickup_difference_m(config) == 10.0
    assert _MODULE._early_pickup_difference_m({"event_position_difference_m": 8.0}) == 5.0
    predicates = _MODULE._second_lift_predicates(observed, reference, config)
    assert all(item["status"] == "PASS" for item in predicates)
    assert predicates[1]["required_boundary"] == 90.0

    outside = copy.deepcopy(observed)
    outside["throttle_pickup_m"] = 90.000001
    assert _MODULE._second_lift_predicates(outside, reference, config)[1]["status"] == "FAIL"
    truthy_integer = copy.deepcopy(observed)
    truthy_integer["second_lift"] = 1
    with pytest.raises(DiagnosisEvidenceError, match="must be boolean"):
        _MODULE._second_lift_predicates(truthy_integer, reference, config)


def test_one_support_stays_wait_and_two_same_corner_supports_need_source_diagnosis() -> None:
    real = build_diagnosis_evidence(REAL_BYTES)
    assert _audit(real, _MODULE.THROTTLE_SECOND_LIFT, "C03")["evaluation_status"] == (
        "WAIT_REPEATED_EVIDENCE"
    )

    replay = _replay()
    _support_late_c02(replay)
    replay = _rehash(replay)
    with pytest.raises(DiagnosisEvidenceError) as error:
        build_diagnosis_evidence(_serialize(replay))
    assert error.value.code == "FAIL_MODEL_RULE_DIVERGENCE"
    assert "presence disagrees" in str(error.value)


def test_two_same_corner_supports_and_matching_source_remain_shadow_only() -> None:
    replay = _replay()
    _support_late_c02(replay)
    _append_diagnosis(replay, _late_diagnosis(replay, [4, 5]))
    replay = _rehash(replay)

    report = build_diagnosis_evidence(_serialize(replay))
    audit = _audit(report, _MODULE.LATE_BRAKING_HURTS_EXIT, "C02")

    assert audit["evaluation_status"] == "RULE_SUPPORT_PRESENT_SHADOW"
    assert audit["support_lap_ordinals"] == [4, 5]
    assert audit["source_diagnosis_present"] is True
    assert report["recommendations"] == []
    assert report["executable"] is False
    assert report["promotion_gates"] == {
        "a_b_validation": "WAIT_AB_PRACTICE",
        "condition_matching": "WAIT_CONDITION_DATA",
        "golden_corner": "CANDIDATE_NOT_GOLDEN",
        "human_labels": "WAIT_HUMAN_LABELS",
    }


def test_self_consistent_false_source_diagnosis_fails_model_rule_divergence() -> None:
    replay = _replay()
    _append_diagnosis(replay, _late_diagnosis(replay, [4, 5]))
    replay = _rehash(replay)

    with pytest.raises(DiagnosisEvidenceError) as error:
        build_diagnosis_evidence(_serialize(replay))

    assert error.value.code == "FAIL_MODEL_RULE_DIVERGENCE"


def test_self_consistent_source_comparison_tamper_fails_model_rule_divergence() -> None:
    replay = _replay()
    _support_late_c02(replay)
    diagnosis = _late_diagnosis(replay, [4, 5])
    comparisons = diagnosis["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[0]["evidence_median"] += 0.000001
    comparisons[0]["reference_value"] += 0.000001
    _append_diagnosis(replay, diagnosis)
    replay = _rehash(replay)

    with pytest.raises(DiagnosisEvidenceError) as error:
        build_diagnosis_evidence(_serialize(replay))

    assert error.value.code == "FAIL_MODEL_RULE_DIVERGENCE"
    assert "comparison values" in str(error.value)


def test_self_consistent_source_gain_tamper_fails_model_rule_divergence() -> None:
    replay = _replay()
    _support_late_c02(replay)
    diagnosis = _late_diagnosis(replay, [4, 5])
    gain = diagnosis["expected_gain_range_s"]
    assert isinstance(gain, list)
    gain[0] += 0.001
    _append_diagnosis(replay, diagnosis)
    replay = _rehash(replay)

    with pytest.raises(DiagnosisEvidenceError) as error:
        build_diagnosis_evidence(_serialize(replay))

    assert error.value.code == "FAIL_MODEL_RULE_DIVERGENCE"
    assert "source loss, gain, or confidence" in str(error.value)


def test_missing_candidate_metric_is_not_a_counterexample_or_not_observed() -> None:
    replay = _replay()
    metrics = _metric_map(replay, "C03")
    metrics[2]["throttle_pickup_m"] = None
    replay = _rehash(replay)

    report = build_diagnosis_evidence(_serialize(replay))
    audit = _audit(report, _MODULE.THROTTLE_SECOND_LIFT, "C03")
    lap2 = next(item for item in audit["lap_evidence"] if item["lap_ordinal"] == 2)

    assert audit["evaluation_status"] == "NOT_EVALUABLE_REQUIRED_METRIC"
    assert lap2["status"] == "NOT_EVALUABLE_REQUIRED_METRIC"
    assert lap2["all_predicates_pass"] is None
    assert lap2["predicates"] == []
    assert lap2["reasons"] == ["REQUIRED_METRIC_MISSING:throttle_pickup_m"]


def test_two_supports_take_precedence_over_an_unusable_third_candidate() -> None:
    replay = _replay()
    _support_late_c02(replay)
    metrics = _metric_map(replay, "C02")
    metrics[2]["brake_onset_m"] = None
    _append_diagnosis(replay, _late_diagnosis(replay, [4, 5]))
    replay = _rehash(replay)

    report = build_diagnosis_evidence(_serialize(replay))
    audit = _audit(report, _MODULE.LATE_BRAKING_HURTS_EXIT, "C02")
    lap2 = next(item for item in audit["lap_evidence"] if item["lap_ordinal"] == 2)

    assert audit["evaluation_status"] == "RULE_SUPPORT_PRESENT_SHADOW"
    assert audit["support_lap_ordinals"] == [4, 5]
    assert lap2["status"] == "NOT_EVALUABLE_REQUIRED_METRIC"
    assert lap2["all_predicates_pass"] is None


def test_missing_reference_metric_differs_from_reference_pattern_gate() -> None:
    replay = _replay()
    metrics = _metric_map(replay, "C03")
    metrics[11]["throttle_pickup_m"] = None
    replay = _rehash(replay)

    report = build_diagnosis_evidence(_serialize(replay))
    missing = _audit(report, _MODULE.THROTTLE_SECOND_LIFT, "C03")
    gated = _audit(report, _MODULE.THROTTLE_SECOND_LIFT, "C02")

    assert missing["evaluation_status"] == "NOT_EVALUABLE_REQUIRED_METRIC"
    assert missing["reference_gate"] == {
        "reasons": ["REFERENCE_REQUIRED_METRIC_MISSING:throttle_pickup_m"],
        "status": "NOT_EVALUABLE_REQUIRED_METRIC",
    }
    assert gated["evaluation_status"] == "NOT_EVALUABLE_REFERENCE_GATE"
    assert gated["reference_gate"] == {
        "reasons": ["REFERENCE_SECOND_LIFT_PRESENT"],
        "status": "NOT_EVALUABLE_REFERENCE_GATE",
    }


@pytest.mark.parametrize(
    "payload",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"",
    ],
)
def test_strict_json_rejects_duplicates_nonfinite_and_empty(payload: bytes) -> None:
    with pytest.raises(DiagnosisEvidenceError) as error:
        build_diagnosis_evidence(payload)
    assert error.value.code == "INVALID_JSON"


def test_exact_replay_schema_and_hashes_fail_closed() -> None:
    unknown = _replay()
    unknown["unexpected"] = True
    with pytest.raises(DiagnosisEvidenceError):
        build_diagnosis_evidence(_serialize(unknown))

    tampered = _replay()
    model = tampered["model_output"]
    assert isinstance(model, dict)
    metrics = model["corner_metrics"]
    assert isinstance(metrics, list)
    metrics[0]["apex_speed_mps"] += 1.0
    with pytest.raises(DiagnosisEvidenceError) as error:
        build_diagnosis_evidence(_serialize(tampered))
    assert error.value.code == "DRIVING_REPLAY_SHA256_MISMATCH"

    bool_number = _replay()
    model = bool_number["model_output"]
    assert isinstance(model, dict)
    metrics = model["corner_metrics"]
    assert isinstance(metrics, list)
    metrics[0]["apex_speed_mps"] = True
    bool_number = _rehash(bool_number)
    with pytest.raises(DiagnosisEvidenceError) as error:
        build_diagnosis_evidence(_serialize(bool_number))
    assert error.value.code == "INVALID_RECEIPT"


def test_metric_order_changes_input_identity_but_not_rule_audit_semantics() -> None:
    first = build_diagnosis_evidence(REAL_BYTES)
    replay = _replay()
    model = replay["model_output"]
    assert isinstance(model, dict)
    metrics = model["corner_metrics"]
    assert isinstance(metrics, list)
    metrics.reverse()
    replay = _rehash(replay)
    second = build_diagnosis_evidence(_serialize(replay))

    assert first["rule_audits"] == second["rule_audits"]
    assert first["summary"] == second["summary"]
    assert first["input_receipt"]["driving_replay_canonical_sha256"] != second[
        "input_receipt"
    ]["driving_replay_canonical_sha256"]


def test_eligible_order_is_canonicalized_before_source_evidence_parity() -> None:
    replay = _replay()
    _support_late_c02(replay)
    _append_diagnosis(replay, _late_diagnosis(replay, [4, 5]))
    model = replay["model_output"]
    assert isinstance(model, dict)
    eligible = model["eligible_lap_ordinals"]
    assert isinstance(eligible, list)
    eligible.reverse()
    replay = _rehash(replay)

    report = build_diagnosis_evidence(_serialize(replay))
    audit = _audit(report, _MODULE.LATE_BRAKING_HURTS_EXIT, "C02")

    assert report["reference"]["eligible_lap_ordinals"] == [2, 4, 5, 9, 10, 11, 16]
    assert report["reference"]["comparison_lap_ordinals"] == [2, 4, 5, 9, 10, 16]
    assert audit["support_lap_ordinals"] == [4, 5]
    assert audit["evaluation_status"] == "RULE_SUPPORT_PRESENT_SHADOW"


def test_hash_seed_processes_are_byte_identical() -> None:
    command = [
        sys.executable,
        "scripts/build_offline_driving_diagnosis_evidence.py",
        str(REAL_REPLAY),
    ]
    outputs: list[bytes] = []
    for seed in ("1", "987654"):
        environment = dict(os.environ)
        environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": seed})
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            cwd=Path.cwd(),
            env=environment,
        )
        assert result.returncode == 0, result.stderr.decode()
        assert result.stderr == b""
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    parsed = json.loads(outputs[0])
    assert parsed["diagnosis_evidence_sha256"] == canonical_sha256(
        {key: value for key, value in parsed.items() if key != "diagnosis_evidence_sha256"}
    )


def test_cli_writes_same_canonical_receipt_exclusively(tmp_path: Path, capfd) -> None:
    source = tmp_path / "driving-replay.json"
    output = tmp_path / "diagnosis-evidence.json"
    source.write_bytes(REAL_BYTES)

    assert _MODULE.main([str(source), "--output", str(output)]) == 0
    first = capfd.readouterr()
    assert first.err == ""
    assert output.read_text(encoding="utf-8") == first.out
    assert hashlib.sha256(source.read_bytes()).hexdigest() == REAL_SERIALIZED_SHA256

    assert _MODULE.main([str(source), "--output", str(output)]) == 3
    second = capfd.readouterr()
    assert second.out == ""
    assert second.err.startswith("OUTPUT_CREATE_FAILED:")
