"""Audit two driving diagnosis rules from a complete compact driving replay.

This reusable layer re-evaluates the frozen ``distance-driving-v1``
predicate stage from ``model_output.corner_metrics``.  It is deliberately a
``DERIVED_RULE_AUDIT``: the compact replay does not contain the resampled raw
traces needed to re-detect brake, throttle, or corner events.  The receipt
therefore never emits an action, expected gain, causal claim, or executable
recommendation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import statistics
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn

from . import corner_cards as _CORNER_VERIFIER

DIAGNOSIS_EVIDENCE_CONTRACT_VERSION = "offline-driving-diagnosis-evidence-v1"
DIAGNOSIS_RULE_POLICY_VERSION = "distance-driving-derived-rule-audit-v1"
MAX_INPUT_BYTES = 128 * 1024 * 1024

LATE_BRAKING_HURTS_EXIT = "LATE_BRAKING_HURTS_EXIT"
THROTTLE_SECOND_LIFT = "THROTTLE_SECOND_LIFT"
RULE_CODES = (LATE_BRAKING_HURTS_EXIT, THROTTLE_SECOND_LIFT)

NOT_OBSERVED = "NOT_OBSERVED"
WAIT_REPEATED_EVIDENCE = "WAIT_REPEATED_EVIDENCE"
NOT_EVALUABLE_REFERENCE_GATE = "NOT_EVALUABLE_REFERENCE_GATE"
NOT_EVALUABLE_REQUIRED_METRIC = "NOT_EVALUABLE_REQUIRED_METRIC"
RULE_SUPPORT_PRESENT_SHADOW = "RULE_SUPPORT_PRESENT_SHADOW"
_ASSESSMENT_STATUSES = (
    NOT_OBSERVED,
    WAIT_REPEATED_EVIDENCE,
    NOT_EVALUABLE_REFERENCE_GATE,
    NOT_EVALUABLE_REQUIRED_METRIC,
    RULE_SUPPORT_PRESENT_SHADOW,
)

_SHA256_CHARS = frozenset("0123456789abcdef")
_LATE_REQUIRED_METRICS = (
    "brake_onset_m",
    "brake_release_m",
    "throttle_pickup_m",
)
_SECOND_LIFT_REQUIRED_METRICS = ("throttle_pickup_m", "second_lift")
_LATE_COMPARISONS = (
    ("brake_onset_m", "m"),
    ("brake_release_m", "m"),
    ("apex_speed_mps", "m/s"),
    ("throttle_pickup_m", "m"),
    ("exit_speed_mps", "m/s"),
    ("carry_delta_s", "s"),
)
_SECOND_LIFT_COMPARISONS = (
    ("throttle_pickup_m", "m"),
    ("second_lift", "bool"),
    ("exit_speed_mps", "m/s"),
    ("total_segment_delta_s", "s"),
)
_PROMOTION_GATES = {
    "a_b_validation": "WAIT_AB_PRACTICE",
    "condition_matching": "WAIT_CONDITION_DATA",
    "golden_corner": "CANDIDATE_NOT_GOLDEN",
    "human_labels": "WAIT_HUMAN_LABELS",
}


class DiagnosisEvidenceError(ValueError):
    """Fail-closed error for an inadmissible replay or rule divergence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise DiagnosisEvidenceError(code, message)


def _canonical_json(value: object, *, newline: bool = False) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise DiagnosisEvidenceError(
            "CANONICAL_JSON_FAILED", "value is not canonical-JSON-safe"
        ) from exc
    return encoded + (b"\n" if newline else b"")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _SHA256_CHARS for character in value)
    ):
        _fail("INVALID_SHA256", f"{name} must be a lowercase SHA-256 digest")
    return value


def _strict_json(payload: bytes) -> object:
    if type(payload) is not bytes or not payload:
        _fail("INVALID_JSON", "input must be non-empty bytes")

    def reject_constant(value: str) -> NoReturn:
        _fail("INVALID_JSON", f"non-finite JSON number is forbidden: {value}")

    def exact_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _fail("INVALID_JSON", f"duplicate JSON key is forbidden: {key}")
            result[key] = value
        return result

    try:
        decoded = payload.decode("utf-8")
        return json.loads(
            decoded,
            object_pairs_hook=exact_pairs,
            parse_constant=reject_constant,
        )
    except DiagnosisEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise DiagnosisEvidenceError("INVALID_JSON", f"cannot parse input JSON: {exc}") from exc


def _load_corner_verifier() -> Any:
    """Return the package-relative verifier through the legacy helper name."""

    return _CORNER_VERIFIER


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("INVALID_RULE_INPUT", f"{name} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        _fail("INVALID_RULE_INPUT", f"{name} must be finite")
    return converted


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _predicate(
    *,
    name: str,
    observed: float,
    reference: float | None,
    boundary: float,
    operator: str,
    unit: str,
) -> dict[str, object]:
    if operator == ">=":
        margin = observed - boundary
    elif operator == "<=":
        margin = boundary - observed
    else:
        _fail("INTERNAL_RULE_ERROR", f"unsupported predicate operator: {operator}")
    if margin == 0.0:
        margin = 0.0
    return {
        "margin_to_pass": margin,
        "observed_value": observed,
        "operator": operator,
        "predicate": name,
        "reference_value": reference,
        "required_boundary": boundary,
        "status": "PASS" if margin >= 0.0 else "FAIL",
        "unit": unit,
    }


def _boolean_predicate(*, name: str, observed: bool) -> dict[str, object]:
    return {
        "margin_to_pass": None,
        "observed_value": observed,
        "operator": "IS",
        "predicate": name,
        "reference_value": None,
        "required_boundary": True,
        "status": "PASS" if observed is True else "FAIL",
        "unit": "bool",
    }


def _early_pickup_difference_m(config: Mapping[str, object]) -> float:
    position_delta = _number(
        config.get("event_position_difference_m"),
        "driving_config.event_position_difference_m",
    )
    return max(5.0, position_delta * 0.625)


def _policy(config: Mapping[str, object]) -> dict[str, object]:
    if type(config) is not dict:
        _fail("INVALID_RULE_INPUT", "driving_config must be a plain object")
    policy: dict[str, object] = {
        "feature_derivation": {
            "brake_release_threshold": _number(
                config.get("brake_release_threshold"), "brake_release_threshold"
            ),
            "brake_threshold": _number(config.get("brake_threshold"), "brake_threshold"),
            "event_sustain_m": _number(config.get("event_sustain_m"), "event_sustain_m"),
            "second_lift_threshold": _number(
                config.get("second_lift_threshold"), "second_lift_threshold"
            ),
            "throttle_pickup_threshold": _number(
                config.get("throttle_pickup_threshold"), "throttle_pickup_threshold"
            ),
        },
        "late_braking_hurts_exit": {
            "event_position_difference_m": _number(
                config.get("event_position_difference_m"), "event_position_difference_m"
            ),
            "minimum_carry_loss_s": _number(
                config.get("minimum_carry_loss_s"), "minimum_carry_loss_s"
            ),
            "minimum_loss_s": _number(config.get("minimum_loss_s"), "minimum_loss_s"),
            "speed_difference_mps": _number(
                config.get("speed_difference_mps"), "speed_difference_mps"
            ),
        },
        "minimum_evidence_laps": config.get("min_evidence_laps"),
        "policy_version": DIAGNOSIS_RULE_POLICY_VERSION,
        "rule_codes": list(RULE_CODES),
        "throttle_second_lift": {
            "early_pickup_difference_formula": (
                "max(5.0,event_position_difference_m*0.625)"
            ),
            "early_pickup_difference_m": _early_pickup_difference_m(config),
            "exit_speed_tolerance_mps": 0.2,
            "minimum_loss_s": _number(config.get("minimum_loss_s"), "minimum_loss_s"),
        },
    }
    minimum_evidence = policy["minimum_evidence_laps"]
    if type(minimum_evidence) is not int or minimum_evidence < 1:
        _fail("INVALID_RULE_INPUT", "min_evidence_laps must be a positive plain integer")
    return {**policy, "policy_sha256": canonical_sha256(policy)}


def _reference_gate(
    code: str, reference: Mapping[str, object]
) -> dict[str, object]:
    required = (
        _LATE_REQUIRED_METRICS
        if code == LATE_BRAKING_HURTS_EXIT
        else _SECOND_LIFT_REQUIRED_METRICS
    )
    missing: list[str] = []
    for name in required:
        value = reference.get(name)
        if name == "second_lift":
            if value is None:
                missing.append(name)
            elif type(value) is not bool:
                _fail("INVALID_RULE_INPUT", f"reference {name} must be boolean or null")
        elif _optional_number(value, f"reference {name}") is None:
            missing.append(name)
    if missing:
        return {
            "reasons": [f"REFERENCE_REQUIRED_METRIC_MISSING:{name}" for name in missing],
            "status": NOT_EVALUABLE_REQUIRED_METRIC,
        }
    if code == THROTTLE_SECOND_LIFT and reference["second_lift"] is not False:
        return {
            "reasons": ["REFERENCE_SECOND_LIFT_PRESENT"],
            "status": NOT_EVALUABLE_REFERENCE_GATE,
        }
    return {"reasons": [], "status": "PASS"}


def _late_predicates(
    observed: Mapping[str, object],
    reference: Mapping[str, object],
    config: Mapping[str, object],
) -> list[dict[str, object]]:
    position_delta = _number(config["event_position_difference_m"], "position delta")
    speed_delta = _number(config["speed_difference_mps"], "speed delta")
    onset = _number(observed["brake_onset_m"], "observed brake_onset_m")
    ref_onset = _number(reference["brake_onset_m"], "reference brake_onset_m")
    release = _number(observed["brake_release_m"], "observed brake_release_m")
    ref_release = _number(reference["brake_release_m"], "reference brake_release_m")
    pickup = _number(observed["throttle_pickup_m"], "observed throttle_pickup_m")
    ref_pickup = _number(reference["throttle_pickup_m"], "reference throttle_pickup_m")
    apex = _number(observed["apex_speed_mps"], "observed apex_speed_mps")
    ref_apex = _number(reference["apex_speed_mps"], "reference apex_speed_mps")
    exit_speed = _number(observed["exit_speed_mps"], "observed exit_speed_mps")
    ref_exit = _number(reference["exit_speed_mps"], "reference exit_speed_mps")
    return [
        _predicate(
            name="BRAKE_ONSET_LATER",
            observed=onset,
            reference=ref_onset,
            boundary=ref_onset + position_delta,
            operator=">=",
            unit="m",
        ),
        _predicate(
            name="BRAKE_RELEASE_LATER",
            observed=release,
            reference=ref_release,
            boundary=ref_release + position_delta,
            operator=">=",
            unit="m",
        ),
        _predicate(
            name="APEX_SPEED_LOWER",
            observed=apex,
            reference=ref_apex,
            boundary=ref_apex - speed_delta,
            operator="<=",
            unit="m/s",
        ),
        _predicate(
            name="THROTTLE_PICKUP_LATER",
            observed=pickup,
            reference=ref_pickup,
            boundary=ref_pickup + position_delta,
            operator=">=",
            unit="m",
        ),
        _predicate(
            name="EXIT_SPEED_LOWER",
            observed=exit_speed,
            reference=ref_exit,
            boundary=ref_exit - speed_delta,
            operator="<=",
            unit="m/s",
        ),
        _predicate(
            name="CARRY_LOSS_PRESENT",
            observed=_number(observed["carry_delta_s"], "observed carry_delta_s"),
            reference=None,
            boundary=_number(config["minimum_carry_loss_s"], "minimum carry loss"),
            operator=">=",
            unit="s",
        ),
        _predicate(
            name="SEGMENT_LOSS_PRESENT",
            observed=_number(
                observed["total_segment_delta_s"], "observed total_segment_delta_s"
            ),
            reference=None,
            boundary=_number(config["minimum_loss_s"], "minimum loss"),
            operator=">=",
            unit="s",
        ),
    ]


def _second_lift_predicates(
    observed: Mapping[str, object],
    reference: Mapping[str, object],
    config: Mapping[str, object],
) -> list[dict[str, object]]:
    pickup = _number(observed["throttle_pickup_m"], "observed throttle_pickup_m")
    ref_pickup = _number(reference["throttle_pickup_m"], "reference throttle_pickup_m")
    exit_speed = _number(observed["exit_speed_mps"], "observed exit_speed_mps")
    ref_exit = _number(reference["exit_speed_mps"], "reference exit_speed_mps")
    second_lift = observed["second_lift"]
    if type(second_lift) is not bool:
        _fail("INVALID_RULE_INPUT", "observed second_lift must be boolean")
    return [
        _boolean_predicate(name="SECOND_LIFT_PRESENT", observed=second_lift),
        _predicate(
            name="THROTTLE_PICKUP_EARLIER",
            observed=pickup,
            reference=ref_pickup,
            boundary=ref_pickup - _early_pickup_difference_m(config),
            operator="<=",
            unit="m",
        ),
        _predicate(
            name="EXIT_NOT_FASTER",
            observed=exit_speed,
            reference=ref_exit,
            boundary=ref_exit + 0.2,
            operator="<=",
            unit="m/s",
        ),
        _predicate(
            name="SEGMENT_LOSS_PRESENT",
            observed=_number(
                observed["total_segment_delta_s"], "observed total_segment_delta_s"
            ),
            reference=None,
            boundary=_number(config["minimum_loss_s"], "minimum loss"),
            operator=">=",
            unit="s",
        ),
    ]


def _lap_evidence(
    *,
    code: str,
    observed: Mapping[str, object],
    reference: Mapping[str, object],
    reference_gate: Mapping[str, object],
    config: Mapping[str, object],
) -> dict[str, object]:
    ordinal = observed.get("lap_ordinal")
    if type(ordinal) is not int or ordinal < 1:
        _fail("INVALID_RULE_INPUT", "lap ordinal must be a positive plain integer")
    if reference_gate["status"] != "PASS":
        return {
            "all_predicates_pass": None,
            "lap_ordinal": ordinal,
            "predicates": [],
            "reasons": list(reference_gate["reasons"]),
            "status": reference_gate["status"],
        }
    required = (
        _LATE_REQUIRED_METRICS
        if code == LATE_BRAKING_HURTS_EXIT
        else _SECOND_LIFT_REQUIRED_METRICS
    )
    missing: list[str] = []
    for name in required:
        value = observed.get(name)
        if name == "second_lift":
            if value is None:
                missing.append(name)
            elif type(value) is not bool:
                _fail("INVALID_RULE_INPUT", f"observed {name} must be boolean or null")
        elif _optional_number(value, f"observed {name}") is None:
            missing.append(name)
    if missing:
        return {
            "all_predicates_pass": None,
            "lap_ordinal": ordinal,
            "predicates": [],
            "reasons": [f"REQUIRED_METRIC_MISSING:{name}" for name in missing],
            "status": NOT_EVALUABLE_REQUIRED_METRIC,
        }
    predicates = (
        _late_predicates(observed, reference, config)
        if code == LATE_BRAKING_HURTS_EXIT
        else _second_lift_predicates(observed, reference, config)
    )
    passed = all(item["status"] == "PASS" for item in predicates)
    return {
        "all_predicates_pass": passed,
        "lap_ordinal": ordinal,
        "predicates": predicates,
        "reasons": [
            f"PREDICATE_FAILED:{item['predicate']}"
            for item in predicates
            if item["status"] == "FAIL"
        ],
        "status": "SUPPORT" if passed else "COUNTEREXAMPLE",
    }


def _expected_comparisons(
    *,
    code: str,
    support: Sequence[Mapping[str, object]],
    reference: Mapping[str, object],
) -> list[dict[str, object]]:
    fields = _LATE_COMPARISONS if code == LATE_BRAKING_HURTS_EXIT else _SECOND_LIFT_COMPARISONS
    comparisons: list[dict[str, object]] = []
    for field, unit in fields:
        if field == "second_lift":
            evidence_median = 1.0
            reference_value = float(bool(reference[field]))
        else:
            evidence_median = float(
                statistics.median(_number(item[field], f"support {field}") for item in support)
            )
            reference_value = _number(reference[field], f"reference {field}")
        comparisons.append(
            {
                "difference": evidence_median - reference_value,
                "evidence_median": evidence_median,
                "metric": field,
                "reference_value": reference_value,
                "unit": unit,
            }
        )
    return comparisons


def _same_number(left: object, right: object) -> bool:
    try:
        return math.isclose(
            _number(left, "comparison"),
            _number(right, "comparison"),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    except DiagnosisEvidenceError:
        return False


def _validate_source_diagnosis(
    *,
    code: str,
    corner_id: str,
    source: Mapping[str, object] | None,
    status: str,
    support_ordinals: Sequence[int],
    comparison_ordinals: Sequence[int],
    metric_by_ordinal: Mapping[int, Mapping[str, object]],
    reference: Mapping[str, object],
) -> None:
    should_exist = status == RULE_SUPPORT_PRESENT_SHADOW
    if (source is not None) != should_exist:
        _fail(
            "FAIL_MODEL_RULE_DIVERGENCE",
            f"{corner_id}/{code}: source diagnosis presence disagrees with reconstructed rule",
        )
    if source is None:
        return
    expected_evidence = list(support_ordinals)
    expected_counterexamples = [
        ordinal for ordinal in comparison_ordinals if ordinal not in set(support_ordinals)
    ]
    if (
        source.get("evidence_lap_ordinals") != expected_evidence
        or source.get("counterexample_lap_ordinals") != expected_counterexamples
    ):
        _fail(
            "FAIL_MODEL_RULE_DIVERGENCE",
            f"{corner_id}/{code}: source evidence partition disagrees with reconstructed rule",
        )
    support = [metric_by_ordinal[ordinal] for ordinal in support_ordinals]
    expected_comparisons = _expected_comparisons(
        code=code,
        support=support,
        reference=reference,
    )
    supplied = source.get("comparisons")
    if type(supplied) is not list or len(supplied) != len(expected_comparisons):
        _fail(
            "FAIL_MODEL_RULE_DIVERGENCE",
            f"{corner_id}/{code}: source comparisons disagree with reconstructed rule",
        )
    for actual, expected in zip(supplied, expected_comparisons, strict=True):
        if type(actual) is not dict or any(
            actual.get(name) != expected[name] for name in ("metric", "unit")
        ):
            _fail(
                "FAIL_MODEL_RULE_DIVERGENCE",
                f"{corner_id}/{code}: source comparison identity is invalid",
            )
        if any(
            not _same_number(actual.get(name), expected[name])
            for name in ("evidence_median", "reference_value", "difference")
        ):
            _fail(
                "FAIL_MODEL_RULE_DIVERGENCE",
                f"{corner_id}/{code}: source comparison values are not reconstructed",
            )
    expected_loss = float(
        statistics.median(
            max(0.0, _number(item["total_segment_delta_s"], "support total loss"))
            for item in support
        )
    )
    expected_confidence = "high" if len(support) >= 3 else "medium"
    expected_gain = [
        round(max(0.01, expected_loss * 0.4), 3),
        round(expected_loss, 3),
    ]
    supplied_gain = source.get("expected_gain_range_s")
    gain_matches = type(supplied_gain) is list and len(supplied_gain) == 2 and all(
        _same_number(actual, expected)
        for actual, expected in zip(supplied_gain, expected_gain, strict=True)
    )
    if (
        not _same_number(source.get("estimated_loss_median_s"), expected_loss)
        or source.get("confidence") != expected_confidence
        or not gain_matches
    ):
        _fail(
            "FAIL_MODEL_RULE_DIVERGENCE",
            f"{corner_id}/{code}: source loss, gain, or confidence is not reconstructed",
        )


def _corner_audit(
    *,
    code: str,
    corner_id: str,
    reference_ordinal: int,
    comparison_ordinals: Sequence[int],
    metric_by_ordinal: Mapping[int, Mapping[str, object]],
    source_diagnosis: Mapping[str, object] | None,
    config: Mapping[str, object],
) -> dict[str, object]:
    reference = metric_by_ordinal[reference_ordinal]
    gate = _reference_gate(code, reference)
    evidence = [
        _lap_evidence(
            code=code,
            observed=metric_by_ordinal[ordinal],
            reference=reference,
            reference_gate=gate,
            config=config,
        )
        for ordinal in comparison_ordinals
    ]
    support_ordinals = sorted(
        int(item["lap_ordinal"]) for item in evidence if item["status"] == "SUPPORT"
    )
    minimum_evidence = config["min_evidence_laps"]
    assert type(minimum_evidence) is int
    if gate["status"] != "PASS":
        status = str(gate["status"])
    elif len(support_ordinals) >= minimum_evidence:
        status = RULE_SUPPORT_PRESENT_SHADOW
    elif any(item["status"] == NOT_EVALUABLE_REQUIRED_METRIC for item in evidence):
        status = NOT_EVALUABLE_REQUIRED_METRIC
    elif support_ordinals:
        status = WAIT_REPEATED_EVIDENCE
    else:
        status = NOT_OBSERVED
    _validate_source_diagnosis(
        code=code,
        corner_id=corner_id,
        source=source_diagnosis,
        status=status,
        support_ordinals=support_ordinals,
        comparison_ordinals=comparison_ordinals,
        metric_by_ordinal=metric_by_ordinal,
        reference=reference,
    )
    return {
        "corner_id": corner_id,
        "evaluation_status": status,
        "lap_evidence": evidence,
        "reference_gate": gate,
        "reference_lap_ordinal": reference_ordinal,
        "required_support_count": minimum_evidence,
        "source_diagnosis_consistency": "PASS",
        "source_diagnosis_present": source_diagnosis is not None,
        "support_count": len(support_ordinals),
        "support_lap_ordinals": support_ordinals,
    }


def _source_identity(replay: Mapping[str, object]) -> dict[str, object]:
    evidence = replay["input_evidence"]
    if type(evidence) is not dict:
        _fail("INVALID_SOURCE_IDENTITY", "input_evidence must be a plain object")
    source_field = "source_sha256" if replay["input_kind"] == "ibt" else "records_sha256"
    return {
        "authenticity_status": evidence["authenticity_status"],
        "completion_status": evidence["completion_status"],
        "input_evidence_sha256": canonical_sha256(evidence),
        "input_kind": replay["input_kind"],
        "session_id": evidence["session_id"],
        "source_content_sha256": _sha256(evidence[source_field], source_field),
        "source_content_sha256_field": source_field,
        "source_id": evidence["source_id"],
        "source_kind": evidence["source_kind"],
    }


def _build_diagnosis_evidence(
    replay: object,
    *,
    serialized_sha256: str,
) -> dict[str, object]:
    serialized_sha256 = _sha256(serialized_sha256, "serialized replay SHA-256")
    try:
        validated = _CORNER_VERIFIER.validate_driving_replay(replay)
    except _CORNER_VERIFIER.CornerCardError as exc:
        raise DiagnosisEvidenceError(exc.code, f"source replay rejected: {exc}") from exc
    payload = validated["replay"]
    model = validated["model"]
    if type(payload) is not dict or type(model) is not dict:
        _fail("INVALID_RULE_INPUT", "validated replay objects are invalid")
    pipeline = payload["pipeline"]
    if (
        type(pipeline) is not dict
        or pipeline.get("driving_algorithm_version") != "distance-driving-v1"
    ):
        _fail("UNSUPPORTED_RULE_POLICY", "only distance-driving-v1 is supported")
    config = pipeline["driving_config"]
    if type(config) is not dict:
        _fail("INVALID_RULE_INPUT", "driving_config must be a plain object")
    policy = _policy(config)
    eligible = sorted(int(value) for value in validated["eligible"])
    reference_ordinal = int(validated["reference_ordinal"])
    comparison_ordinals = [value for value in eligible if value != reference_ordinal]
    corner_ids = [str(value) for value in validated["corner_ids"]]
    metrics = validated["metric_by_key"]
    diagnoses = validated["diagnoses"]
    if type(metrics) is not dict:
        _fail("INVALID_RULE_INPUT", "validated metric grid is invalid")
    source_by_key = {
        (str(item["diagnosis"]), str(item["corner_id"])): item
        for item in diagnoses
        if item["diagnosis"] in RULE_CODES
    }
    rule_audits: list[dict[str, object]] = []
    counts = {status: 0 for status in _ASSESSMENT_STATUSES}
    for code in RULE_CODES:
        corners: list[dict[str, object]] = []
        for corner_id in corner_ids:
            by_ordinal = {
                ordinal: metrics[(corner_id, ordinal)] for ordinal in eligible
            }
            audit = _corner_audit(
                code=code,
                corner_id=corner_id,
                reference_ordinal=reference_ordinal,
                comparison_ordinals=comparison_ordinals,
                metric_by_ordinal=by_ordinal,
                source_diagnosis=source_by_key.get((code, corner_id)),
                config=config,
            )
            counts[str(audit["evaluation_status"])] += 1
            corners.append(audit)
        rule_audits.append({"corners": corners, "diagnosis": code})

    normalized = payload["normalized_input_receipt"]
    context = payload["driving_context"]
    if type(normalized) is not dict or type(context) is not dict:
        _fail("INVALID_SOURCE_IDENTITY", "normalized receipt or driving context is invalid")
    input_receipt = {
        "driving_context_sha256": _sha256(
            payload["driving_context_sha256"], "driving_context_sha256"
        ),
        "driving_replay_canonical_sha256": canonical_sha256(payload),
        "driving_replay_serialized_sha256": serialized_sha256,
        "driving_replay_sha256": _sha256(
            payload["driving_replay_sha256"], "driving_replay_sha256"
        ),
        "input_provenance_sha256": _sha256(
            payload["input_provenance_sha256"], "input_provenance_sha256"
        ),
        "model_output_sha256": _sha256(payload["model_output_sha256"], "model_output_sha256"),
        "model_semantic_sha256": _sha256(
            payload["model_semantic_sha256"], "model_semantic_sha256"
        ),
        "normalized_samples_sha256": _sha256(
            normalized["samples_sha256"], "normalized samples_sha256"
        ),
        "pipeline_sha256": _sha256(pipeline["pipeline_sha256"], "pipeline_sha256"),
        "source_identity": _source_identity(payload),
        "track_length_mm": context["track_length_mm"],
    }
    binding: dict[str, object] = {
        "advisor_only": True,
        "claim_scope": "DERIVED_RULE_AUDIT",
        "contract_version": DIAGNOSIS_EVIDENCE_CONTRACT_VERSION,
        "executable": False,
        "execution_status": "COMPLETE",
        "input_receipt": input_receipt,
        "policy": policy,
        "promotion_gates": dict(_PROMOTION_GATES),
        "raw_telemetry_replayed": False,
        "recommendations": [],
        "reference": {
            "comparison_lap_ordinals": comparison_ordinals,
            "corner_ids": corner_ids,
            "eligible_lap_ordinals": eligible,
            "reference_lap_ordinal": reference_ordinal,
        },
        "rule_audits": rule_audits,
        "status": "SHADOW_ONLY",
        "summary": {
            "audited_corner_count": len(corner_ids),
            "audited_rule_count": len(RULE_CODES),
            "source_model_rule_consistency": "PASS",
            "status_counts": counts,
        },
    }
    return {**binding, "diagnosis_evidence_sha256": canonical_sha256(binding)}


def build_diagnosis_evidence(serialized_replay: bytes) -> dict[str, object]:
    """Strictly parse and audit one exact serialized driving replay."""

    if type(serialized_replay) is not bytes:
        raise TypeError("serialized_replay must be bytes")
    replay = _strict_json(serialized_replay)
    return _build_diagnosis_evidence(
        replay,
        serialized_sha256=hashlib.sha256(serialized_replay).hexdigest(),
    )


def _read_input(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DiagnosisEvidenceError(
            "INPUT_READ_FAILED", f"cannot open input safely: {exc}"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_INPUT_BYTES:
                _fail("INPUT_NOT_REGULAR", "input must be a bounded non-empty regular file")
            payload = handle.read(MAX_INPUT_BYTES + 1)
            after = os.fstat(handle.fileno())
            before_identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            after_identity = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if len(payload) != before.st_size or before_identity != after_identity:
                _fail("INPUT_CHANGED", "input changed while it was being read")
    except DiagnosisEvidenceError:
        raise
    except OSError as exc:
        raise DiagnosisEvidenceError("INPUT_READ_FAILED", f"cannot read input: {exc}") from exc
    return payload


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise DiagnosisEvidenceError(
            "OUTPUT_CREATE_FAILED", f"cannot create output: {exc}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        with suppress(OSError):
            path.unlink()
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("driving_replay", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        serialized = _read_input(args.driving_replay)
        report = build_diagnosis_evidence(serialized)
        encoded = _canonical_json(report, newline=True)
        if args.output is not None:
            _write_exclusive(args.output, encoded)
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
    except DiagnosisEvidenceError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
