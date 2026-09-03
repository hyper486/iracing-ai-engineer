"""Human-reviewed regression labels for the distance-domain driving model.

The contract separates model-generated proposals from self-attested review
content. Proposal generation can only create a pending artifact, and public
checksums cannot authenticate reviewer identity. Trusted regression therefore
remains closed until an external human-authentication mechanism exists.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .telemetry import TELEMETRY_CONTRACT_VERSION

DRIVING_LABELS_CONTRACT_VERSION = "driving-labels-v1"
DRIVING_LABEL_REGRESSION_CONTRACT_VERSION = "driving-label-regression-v1"
DRIVING_LABEL_COMPARATOR_VERSION = "ordered-circular-distance-v1"

PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
CANDIDATE_NOT_GOLDEN = "CANDIDATE_NOT_GOLDEN"
SELF_ATTESTED_NOT_AUTHENTICATED = "SELF_ATTESTED_NOT_AUTHENTICATED"
WAIT_HUMAN_AUTHENTICATION = "WAIT_HUMAN_AUTHENTICATION"

HUMAN_REVIEW_ATTESTATION = (
    "I independently reviewed the cited evidence; model proposals were not "
    "treated as ground truth."
)

DEFAULT_TOLERANCE_POLICY: dict[str, object] = {
    "minimum_mm": 1_000,
    "boundary_max_mm": 20_000,
    "brake_onset_max_mm": 10_000,
    "apex_max_mm": 15_000,
    "throttle_pickup_max_mm": 15_000,
    "derivation": "REPEATED_OR_INDEPENDENT_BLIND_REVIEW",
}

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TOP_LEVEL_KEYS = frozenset(
    {
        "contract_version",
        "label_set_id",
        "revision",
        "subject",
        "candidate_basis",
        "proposals",
        "human_labels",
        "tolerance_policy",
        "review",
        "candidate_payload_sha256",
        "labels_content_sha256",
        "review_sha256",
        "artifact_sha256",
    }
)
_SUBJECT_KEYS = frozenset(
    {
        "car_key",
        "track_key",
        "layout_key",
        "track_length_mm",
        "coordinate_system",
        "condition_scope",
    }
)
_CANDIDATE_BASIS_KEYS = frozenset(
    {
        "status",
        "source_id",
        "session_id",
        "input_kind",
        "input_provenance_sha256",
        "source_data_sha256",
        "normalized_samples_sha256",
        "driving_context_sha256",
        "model_output_sha256",
        "model_semantic_sha256",
        "pipeline_sha256",
        "grid_step_mm",
        "labeled_lap_ordinal",
    }
)
_PROPOSAL_KEYS = frozenset(
    {
        "proposal_id",
        "ordinal",
        "model_corner_id",
        "detected_window",
        "events",
    }
)
_PROPOSAL_EVENT_KEYS = frozenset(
    {"brake_onset_mm", "apex_mm", "throttle_pickup_mm"}
)
_WINDOW_KEYS = frozenset({"start_mm", "end_mm", "wraps_start_finish"})
_HUMAN_LABEL_KEYS = frozenset(
    {
        "label_id",
        "ordinal",
        "source_proposal_id",
        "decision",
        "display_name",
        "detected_window",
        "events",
        "annotation_evidence",
    }
)
_LABELED_WINDOW_KEYS = frozenset({"start", "end", "wraps_start_finish"})
_LABELED_EVENTS_KEYS = frozenset({"brake_onset", "apexes", "throttle_pickup"})
_EXPECTED_POINT_KEYS = frozenset({"expected_mm", "tolerance_mm"})
_EVENT_POINT_KEYS = frozenset({"expectation", "expected_mm", "tolerance_mm"})
_ANNOTATION_EVIDENCE_KEYS = frozenset(
    {"kind", "artifact_sha256", "review_passes"}
)
_REVIEW_KEYS = frozenset(
    {
        "status",
        "authenticity_status",
        "reviewer_id",
        "reviewed_at_utc",
        "method",
        "evidence_artifact_sha256",
        "candidate_hidden_during_first_pass",
        "attestation",
        "candidate_payload_sha256",
        "labels_content_sha256",
        "decision_reason",
    }
)
_ALLOWED_ANNOTATION_EVIDENCE = frozenset(
    {
        "BLIND_TELEMETRY_TRACE_REVIEW",
        "REPLAY_VIDEO_REVIEW",
        "INDEPENDENT_HUMAN_REVIEW",
    }
)
_ALLOWED_HUMAN_DECISIONS = frozenset(
    {"CONFIRMED", "CORRECTED", "REJECTED_PROPOSAL", "NEW_HUMAN_CORNER"}
)
_DRIVING_REPLAY_BINDING_KEYS = (
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
_BLOCKING_BASELINE_COMPARISONS = (
    "source_id_match",
    "session_id_match",
    "input_kind_match",
    "source_data_sha256_match",
    "normalized_samples_sha256_match",
    "input_provenance_sha256_match",
    "driving_context_sha256_match",
)


class DrivingLabelsError(ValueError):
    """Raised when a driving-label or regression contract is invalid."""


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise DrivingLabelsError("value is not canonical-JSON-safe") from exc


def canonical_sha256(value: object) -> str:
    """Return the contract's deterministic canonical-JSON SHA-256 digest."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _mapping(value: object, name: str, keys: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict:
        raise DrivingLabelsError(f"{name} must be a plain object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise DrivingLabelsError(
            f"{name} keys are invalid; missing={missing}, extra={extra}"
        )
    return value


def _list(value: object, name: str) -> list[Any]:
    if type(value) is not list:
        raise DrivingLabelsError(f"{name} must be a plain list")
    return value


def _string(value: object, name: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value.strip()):
        raise DrivingLabelsError(f"{name} must be a non-empty string")
    return value


def _identifier(value: object, name: str) -> str:
    text = _string(value, name)
    if _IDENTIFIER.fullmatch(text) is None:
        raise DrivingLabelsError(f"{name} is not a valid stable identifier")
    return text


def _run_identifier(value: object, name: str) -> str:
    text = _string(value, name)
    if (
        text != text.strip()
        or len(text.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in text)
    ):
        raise DrivingLabelsError(f"{name} is not a valid run identifier")
    return text


def _sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise DrivingLabelsError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _integer(
    value: object,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise DrivingLabelsError(f"{name} must be a plain integer")
    if minimum is not None and value < minimum:
        raise DrivingLabelsError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise DrivingLabelsError(f"{name} must be at most {maximum}")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _utc_timestamp(value: object, name: str) -> str:
    text = _string(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DrivingLabelsError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise DrivingLabelsError(f"{name} must use UTC")
    return text


def _meters_to_mm(value: object, name: str, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DrivingLabelsError(f"{name} must be a finite distance in metres")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise DrivingLabelsError(f"{name} must be a finite distance in metres")
    millimetres = numeric * 1_000.0
    rounded = round(millimetres)
    if abs(millimetres - rounded) > 1e-6:
        raise DrivingLabelsError(f"{name} must resolve to an integer millimetre")
    return int(rounded)


def _candidate_material(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "candidate_basis": payload["candidate_basis"],
        "contract_version": payload["contract_version"],
        "label_set_id": payload["label_set_id"],
        "proposals": payload["proposals"],
        "revision": payload["revision"],
        "subject": payload["subject"],
        "tolerance_policy": payload["tolerance_policy"],
    }


def _labels_material(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "candidate_payload_sha256": payload["candidate_payload_sha256"],
        "contract_version": payload["contract_version"],
        "human_labels": payload["human_labels"],
        "label_set_id": payload["label_set_id"],
        "revision": payload["revision"],
        "subject": payload["subject"],
        "tolerance_policy": payload["tolerance_policy"],
    }


def _artifact_material(payload: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "artifact_sha256"}


def _pipeline_sha256(pipeline: Mapping[str, object]) -> str:
    return canonical_sha256(
        {key: value for key, value in pipeline.items() if key != "pipeline_sha256"}
    )


def _validate_ready_replay(payload: Mapping[str, object]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise DrivingLabelsError("driving replay must be an object")
    expected_replay_keys = frozenset(
        (*_DRIVING_REPLAY_BINDING_KEYS, "driving_replay_sha256")
    )
    actual_replay_keys = frozenset(payload)
    if actual_replay_keys != expected_replay_keys:
        missing = sorted(expected_replay_keys - actual_replay_keys)
        extra = sorted(actual_replay_keys - expected_replay_keys)
        raise DrivingLabelsError(
            f"driving replay top-level keys are invalid; missing={missing}, extra={extra}"
        )
    if payload.get("contract_version") != "driving-model-replay-v1":
        raise DrivingLabelsError("driving replay contract version is invalid")
    if payload.get("readiness_status") != "PASS":
        raise DrivingLabelsError("driving replay must be ready")
    quality = payload.get("quality_gate")
    if type(quality) is not dict or quality.get("status") != "PASS":
        raise DrivingLabelsError("driving replay quality gate must pass")

    model_output = payload.get("model_output")
    if type(model_output) is not dict or model_output.get("status") != "READY":
        raise DrivingLabelsError("driving replay model output must be READY")
    expected_output_sha256 = _sha256(
        payload.get("model_output_sha256"), "driving replay model_output_sha256"
    )
    if canonical_sha256(model_output) != expected_output_sha256:
        raise DrivingLabelsError("driving replay model output hash mismatch")

    context = payload.get("driving_context")
    if type(context) is not dict or context.get("availability") != "AVAILABLE":
        raise DrivingLabelsError("driving replay track context must be available")
    track_length_mm = _integer(
        context.get("track_length_mm"),
        "driving replay track_length_mm",
        minimum=100_001,
        maximum=100_000_000,
    )
    context_sha256 = _sha256(
        payload.get("driving_context_sha256"),
        "driving replay driving_context_sha256",
    )
    if context.get("context_sha256") != context_sha256:
        raise DrivingLabelsError("driving replay context hash fields disagree")
    if canonical_sha256(
        {key: value for key, value in context.items() if key != "context_sha256"}
    ) != context_sha256:
        raise DrivingLabelsError("driving replay context hash mismatch")

    pipeline = payload.get("pipeline")
    if type(pipeline) is not dict:
        raise DrivingLabelsError("driving replay pipeline must be an object")
    pipeline_digest = _sha256(
        pipeline.get("pipeline_sha256"), "driving replay pipeline_sha256"
    )
    if _pipeline_sha256(pipeline) != pipeline_digest:
        raise DrivingLabelsError("driving replay pipeline hash mismatch")
    if (
        pipeline.get("normalized_telemetry_contract_version")
        != TELEMETRY_CONTRACT_VERSION
    ):
        raise DrivingLabelsError(
            "driving replay pipeline normalized telemetry contract is invalid"
        )
    driving_config = pipeline.get("driving_config")
    if type(driving_config) is not dict:
        raise DrivingLabelsError("driving replay driving_config must be an object")
    grid_step_mm = _meters_to_mm(
        driving_config.get("grid_step_m"), "driving replay grid_step_m"
    )
    assert grid_step_mm is not None
    if not 250 <= grid_step_mm <= 10_000:
        raise DrivingLabelsError("driving replay grid step is out of range")

    normalized = payload.get("normalized_input_receipt")
    if type(normalized) is not dict:
        raise DrivingLabelsError("normalized input receipt must be an object")
    if normalized.get("contract_version") != TELEMETRY_CONTRACT_VERSION:
        raise DrivingLabelsError("normalized input receipt contract version is invalid")
    normalized_sha256 = _sha256(
        normalized.get("samples_sha256"), "normalized samples_sha256"
    )
    input_evidence = payload.get("input_evidence")
    if type(input_evidence) is not dict:
        raise DrivingLabelsError("driving replay input_evidence must be an object")
    source_id = _run_identifier(input_evidence.get("source_id"), "source_id")
    session_id = _run_identifier(input_evidence.get("session_id"), "session_id")
    input_kind = payload.get("input_kind")
    if input_kind == "ibt":
        source_data_sha256 = _sha256(
            input_evidence.get("source_sha256"), "IBT source_sha256"
        )
    elif input_kind == "collector":
        source_data_sha256 = _sha256(
            input_evidence.get("records_sha256"), "collector records_sha256"
        )
    else:
        raise DrivingLabelsError("driving replay input_kind is invalid")
    if context.get("source_binding_sha256") != canonical_sha256(input_evidence):
        raise DrivingLabelsError("track context is not bound to input evidence")

    event_receipt = payload.get("event_receipt")
    if type(event_receipt) is not dict:
        raise DrivingLabelsError("driving replay event_receipt must be an object")
    input_provenance_sha256 = _sha256(
        payload.get("input_provenance_sha256"),
        "driving replay input_provenance_sha256",
    )
    input_provenance_binding = {
        "driving_context": context,
        "event_receipt": event_receipt,
        "input_evidence": input_evidence,
        "input_kind": input_kind,
        "normalized_input_receipt": normalized,
    }
    if canonical_sha256(input_provenance_binding) != input_provenance_sha256:
        raise DrivingLabelsError("driving replay input provenance hash mismatch")

    lap_receipt = payload.get("lap_receipt")
    semantic_input_receipt = payload.get("semantic_input_receipt")
    if type(lap_receipt) is not dict or type(semantic_input_receipt) is not dict:
        raise DrivingLabelsError("driving replay semantic receipts must be objects")
    model_semantic_sha256 = _sha256(
        payload.get("model_semantic_sha256"),
        "driving replay model_semantic_sha256",
    )
    semantic_binding = {
        "driving_context": {
            "contract_version": context.get("contract_version"),
            "source_field": context.get("source_field"),
            "track_length_mm": track_length_mm,
        },
        "lap_receipt": lap_receipt,
        "model_output": model_output,
        "pipeline": pipeline,
        "quality_gate": quality,
        "readiness_status": payload.get("readiness_status"),
        "semantic_input_receipt": semantic_input_receipt,
    }
    if canonical_sha256(semantic_binding) != model_semantic_sha256:
        raise DrivingLabelsError("driving replay model semantic hash mismatch")

    corners = _list(model_output.get("corners"), "driving replay corners")
    metrics = _list(model_output.get("corner_metrics"), "driving replay corner_metrics")
    reference = model_output.get("reference")
    if type(reference) is not dict:
        raise DrivingLabelsError("driving replay reference must be an object")
    reference_lap_ordinal = _integer(
        reference.get("lap_ordinal"),
        "driving replay reference lap ordinal",
        minimum=1,
    )
    replay_sha256 = _sha256(
        payload.get("driving_replay_sha256"),
        "driving replay driving_replay_sha256",
    )
    replay_binding = {key: payload[key] for key in _DRIVING_REPLAY_BINDING_KEYS}
    if canonical_sha256(replay_binding) != replay_sha256:
        raise DrivingLabelsError("driving replay hash mismatch")
    return {
        "payload": payload,
        "model_output": model_output,
        "corners": corners,
        "metrics": metrics,
        "track_length_mm": track_length_mm,
        "context_sha256": context_sha256,
        "pipeline_sha256": pipeline_digest,
        "grid_step_mm": grid_step_mm,
        "normalized_sha256": normalized_sha256,
        "source_id": source_id,
        "session_id": session_id,
        "input_kind": input_kind,
        "source_data_sha256": source_data_sha256,
        "input_provenance_sha256": input_provenance_sha256,
        "model_semantic_sha256": model_semantic_sha256,
        "model_output_sha256": expected_output_sha256,
        "driving_replay_sha256": replay_sha256,
        "reference_lap_ordinal": reference_lap_ordinal,
    }


def build_driving_label_candidate(
    driving_replay: Mapping[str, object],
    *,
    label_set_id: str,
    car_key: str,
    track_key: str,
    layout_key: str,
    condition_scope: str = "DRY_UNMATCHED_SMOKE_ONLY",
) -> dict[str, object]:
    """Freeze model proposals in a deterministic, never-approved artifact."""

    replay = _validate_ready_replay(driving_replay)
    label_set_id = _identifier(label_set_id, "label_set_id")
    car_key = _identifier(car_key, "car_key")
    track_key = _identifier(track_key, "track_key")
    layout_key = _identifier(layout_key, "layout_key")
    condition_scope = _string(condition_scope, "condition_scope")
    payload = replay["payload"]
    reference_lap_ordinal = replay["reference_lap_ordinal"]
    reference_metrics: dict[str, dict[str, object]] = {}
    for index, raw_metric in enumerate(replay["metrics"]):
        if type(raw_metric) is not dict:
            raise DrivingLabelsError(f"corner metric {index} must be an object")
        if raw_metric.get("lap_ordinal") != reference_lap_ordinal:
            continue
        corner_id = _string(raw_metric.get("corner_id"), f"corner metric {index} id")
        if corner_id in reference_metrics:
            raise DrivingLabelsError(f"duplicate reference metric for {corner_id}")
        reference_metrics[corner_id] = raw_metric

    proposals: list[dict[str, object]] = []
    seen_corner_ids: set[str] = set()
    previous_start = -1
    for index, raw_corner in enumerate(replay["corners"], start=1):
        if type(raw_corner) is not dict:
            raise DrivingLabelsError(f"corner {index} must be an object")
        corner_id = _string(raw_corner.get("corner_id"), f"corner {index} id")
        if corner_id in seen_corner_ids:
            raise DrivingLabelsError(f"duplicate model corner id {corner_id}")
        seen_corner_ids.add(corner_id)
        if corner_id not in reference_metrics:
            raise DrivingLabelsError(f"reference metric is missing for {corner_id}")
        metric = reference_metrics[corner_id]
        start_mm = _model_coordinate_mm(
            _required_field(raw_corner, "brake_start_m", f"corner {corner_id}"),
            f"{corner_id} start",
            replay["track_length_mm"],
        )
        end_mm = _model_coordinate_mm(
            _required_field(raw_corner, "exit_m", f"corner {corner_id}"),
            f"{corner_id} end",
            replay["track_length_mm"],
        )
        assert start_mm is not None and end_mm is not None
        if start_mm < previous_start:
            raise DrivingLabelsError("model corners are not ordered by track distance")
        previous_start = start_mm
        proposals.append(
            {
                "proposal_id": f"candidate:{corner_id.lower()}",
                "ordinal": index,
                "model_corner_id": corner_id,
                "detected_window": {
                    "start_mm": start_mm,
                    "end_mm": end_mm,
                    "wraps_start_finish": start_mm > end_mm,
                },
                "events": {
                    "brake_onset_mm": _model_coordinate_mm(
                        _required_field(
                            metric, "brake_onset_m", f"corner metric {corner_id}"
                        ),
                        f"{corner_id} brake onset",
                        replay["track_length_mm"],
                        allow_none=True,
                    ),
                    "apex_mm": _model_coordinate_mm(
                        _required_field(metric, "apex_m", f"corner metric {corner_id}"),
                        f"{corner_id} apex",
                        replay["track_length_mm"],
                    ),
                    "throttle_pickup_mm": _model_coordinate_mm(
                        _required_field(
                            metric,
                            "throttle_pickup_m",
                            f"corner metric {corner_id}",
                        ),
                        f"{corner_id} throttle pickup",
                        replay["track_length_mm"],
                        allow_none=True,
                    ),
                },
            }
        )
    if not proposals:
        raise DrivingLabelsError("driving replay contains no corner proposals")

    input_evidence = payload["input_evidence"]
    assert isinstance(input_evidence, dict)
    source_id = _run_identifier(input_evidence.get("source_id"), "source_id")
    session_id = _run_identifier(input_evidence.get("session_id"), "session_id")
    candidate: dict[str, object] = {
        "contract_version": DRIVING_LABELS_CONTRACT_VERSION,
        "label_set_id": label_set_id,
        "revision": 1,
        "subject": {
            "car_key": car_key,
            "track_key": track_key,
            "layout_key": layout_key,
            "track_length_mm": replay["track_length_mm"],
            "coordinate_system": "lap-distance-from-start-finish-v1",
            "condition_scope": condition_scope,
        },
        "candidate_basis": {
            "status": CANDIDATE_NOT_GOLDEN,
            "source_id": source_id,
            "session_id": session_id,
            "input_kind": payload["input_kind"],
            "input_provenance_sha256": _sha256(
                replay["input_provenance_sha256"], "input_provenance_sha256"
            ),
            "source_data_sha256": replay["source_data_sha256"],
            "normalized_samples_sha256": replay["normalized_sha256"],
            "driving_context_sha256": replay["context_sha256"],
            "model_output_sha256": _sha256(
                payload.get("model_output_sha256"), "model_output_sha256"
            ),
            "model_semantic_sha256": _sha256(
                replay["model_semantic_sha256"], "model_semantic_sha256"
            ),
            "pipeline_sha256": replay["pipeline_sha256"],
            "grid_step_mm": replay["grid_step_mm"],
            "labeled_lap_ordinal": reference_lap_ordinal,
        },
        "proposals": proposals,
        "human_labels": [],
        "tolerance_policy": copy.deepcopy(DEFAULT_TOLERANCE_POLICY),
        "review": {
            "status": PENDING_HUMAN_REVIEW,
            "authenticity_status": None,
            "reviewer_id": None,
            "reviewed_at_utc": None,
            "method": None,
            "evidence_artifact_sha256": None,
            "candidate_hidden_during_first_pass": None,
            "attestation": None,
            "candidate_payload_sha256": None,
            "labels_content_sha256": None,
            "decision_reason": None,
        },
        "candidate_payload_sha256": None,
        "labels_content_sha256": None,
        "review_sha256": None,
        "artifact_sha256": None,
    }
    candidate["candidate_payload_sha256"] = canonical_sha256(
        _candidate_material(candidate)
    )
    candidate["artifact_sha256"] = canonical_sha256(_artifact_material(candidate))
    return validate_driving_labels(candidate)


def _validate_tolerance_policy(value: object) -> dict[str, object]:
    policy = _mapping(
        value,
        "tolerance_policy",
        frozenset(DEFAULT_TOLERANCE_POLICY),
    )
    if policy != DEFAULT_TOLERANCE_POLICY:
        raise DrivingLabelsError("driving-labels-v1 tolerance policy is immutable")
    return policy


def _coordinate(value: object, name: str, track_length_mm: int) -> int:
    return _integer(value, name, minimum=0, maximum=track_length_mm - 1)


def _required_field(
    value: Mapping[str, object], key: str, container_name: str
) -> object:
    if key not in value:
        raise DrivingLabelsError(f"{container_name} is missing required field {key}")
    return value[key]


def _model_coordinate_mm(
    value: object,
    name: str,
    track_length_mm: int,
    *,
    allow_none: bool = False,
) -> int | None:
    coordinate = _meters_to_mm(value, name, allow_none=allow_none)
    if coordinate is None:
        return None
    return _coordinate(coordinate, name, track_length_mm)


def _validate_proposal(
    value: object,
    *,
    index: int,
    track_length_mm: int,
) -> dict[str, object]:
    proposal = _mapping(value, f"proposal {index}", _PROPOSAL_KEYS)
    _identifier(proposal["proposal_id"], f"proposal {index} id")
    _integer(proposal["ordinal"], f"proposal {index} ordinal", minimum=1)
    _string(proposal["model_corner_id"], f"proposal {index} model_corner_id")
    window = _mapping(
        proposal["detected_window"],
        f"proposal {index} detected_window",
        _WINDOW_KEYS,
    )
    start = _coordinate(window["start_mm"], f"proposal {index} start_mm", track_length_mm)
    end = _coordinate(window["end_mm"], f"proposal {index} end_mm", track_length_mm)
    wraps = window["wraps_start_finish"]
    if type(wraps) is not bool:
        raise DrivingLabelsError(f"proposal {index} wraps_start_finish must be boolean")
    if (wraps and start <= end) or (not wraps and start >= end):
        raise DrivingLabelsError(f"proposal {index} window/wrap flag is inconsistent")
    events = _mapping(
        proposal["events"], f"proposal {index} events", _PROPOSAL_EVENT_KEYS
    )
    for name, raw in events.items():
        if raw is not None:
            _coordinate(raw, f"proposal {index} {name}", track_length_mm)
    if events["apex_mm"] is None:
        raise DrivingLabelsError(f"proposal {index} apex_mm cannot be missing")
    return proposal


def _validate_expected_point(
    value: object,
    *,
    name: str,
    track_length_mm: int,
    minimum_tolerance_mm: int,
    maximum_tolerance_mm: int,
) -> dict[str, object]:
    point = _mapping(value, name, _EXPECTED_POINT_KEYS)
    _coordinate(point["expected_mm"], f"{name} expected_mm", track_length_mm)
    _integer(
        point["tolerance_mm"],
        f"{name} tolerance_mm",
        minimum=minimum_tolerance_mm,
        maximum=maximum_tolerance_mm,
    )
    return point


def _validate_event_point(
    value: object,
    *,
    name: str,
    track_length_mm: int,
    minimum_tolerance_mm: int,
    maximum_tolerance_mm: int,
) -> dict[str, object]:
    point = _mapping(value, name, _EVENT_POINT_KEYS)
    expectation = point["expectation"]
    if expectation not in {"PRESENT", "ABSENT"}:
        raise DrivingLabelsError(f"{name} expectation must be PRESENT or ABSENT")
    if expectation == "ABSENT":
        if point["expected_mm"] is not None or point["tolerance_mm"] is not None:
            raise DrivingLabelsError(f"{name} ABSENT point cannot carry a value")
    else:
        _coordinate(point["expected_mm"], f"{name} expected_mm", track_length_mm)
        _integer(
            point["tolerance_mm"],
            f"{name} tolerance_mm",
            minimum=minimum_tolerance_mm,
            maximum=maximum_tolerance_mm,
        )
    return point


def _validate_annotation_evidence(value: object, name: str) -> dict[str, object]:
    evidence = _mapping(value, name, _ANNOTATION_EVIDENCE_KEYS)
    if evidence["kind"] not in _ALLOWED_ANNOTATION_EVIDENCE:
        raise DrivingLabelsError(f"{name} kind is invalid")
    _sha256(evidence["artifact_sha256"], f"{name} artifact_sha256")
    _integer(evidence["review_passes"], f"{name} review_passes", minimum=1)
    return evidence


def _validate_human_label(
    value: object,
    *,
    index: int,
    track_length_mm: int,
    policy: Mapping[str, object],
) -> dict[str, object]:
    label = _mapping(value, f"human label {index}", _HUMAN_LABEL_KEYS)
    _identifier(label["label_id"], f"human label {index} label_id")
    decision = label["decision"]
    if decision not in _ALLOWED_HUMAN_DECISIONS:
        raise DrivingLabelsError(f"human label {index} decision is invalid")
    source_proposal_id = label["source_proposal_id"]
    if decision == "NEW_HUMAN_CORNER":
        if source_proposal_id is not None:
            raise DrivingLabelsError("new human corner cannot reference a proposal")
    else:
        _identifier(source_proposal_id, f"human label {index} source_proposal_id")
    _optional_string(label["display_name"], f"human label {index} display_name")
    evidence = _list(
        label["annotation_evidence"], f"human label {index} annotation_evidence"
    )
    total_review_passes = 0
    for evidence_index, raw_evidence in enumerate(evidence, start=1):
        validated = _validate_annotation_evidence(
            raw_evidence, f"human label {index} evidence {evidence_index}"
        )
        total_review_passes += int(validated["review_passes"])
    if total_review_passes < 2:
        raise DrivingLabelsError(
            f"human label {index} needs at least two review passes"
        )

    if decision == "REJECTED_PROPOSAL":
        if label["ordinal"] is not None:
            raise DrivingLabelsError("rejected proposal cannot carry a corner ordinal")
        if label["detected_window"] is not None or label["events"] is not None:
            raise DrivingLabelsError("rejected proposal cannot carry expected geometry")
        return label

    _integer(label["ordinal"], f"human label {index} ordinal", minimum=1)
    window = _mapping(
        label["detected_window"],
        f"human label {index} detected_window",
        _LABELED_WINDOW_KEYS,
    )
    start = _validate_expected_point(
        window["start"],
        name=f"human label {index} window start",
        track_length_mm=track_length_mm,
        minimum_tolerance_mm=int(policy["minimum_mm"]),
        maximum_tolerance_mm=int(policy["boundary_max_mm"]),
    )
    end = _validate_expected_point(
        window["end"],
        name=f"human label {index} window end",
        track_length_mm=track_length_mm,
        minimum_tolerance_mm=int(policy["minimum_mm"]),
        maximum_tolerance_mm=int(policy["boundary_max_mm"]),
    )
    wraps = window["wraps_start_finish"]
    if type(wraps) is not bool:
        raise DrivingLabelsError(
            f"human label {index} wraps_start_finish must be boolean"
        )
    start_mm = int(start["expected_mm"])
    end_mm = int(end["expected_mm"])
    if (wraps and start_mm <= end_mm) or (not wraps and start_mm >= end_mm):
        raise DrivingLabelsError(f"human label {index} window/wrap flag is inconsistent")

    events = _mapping(
        label["events"], f"human label {index} events", _LABELED_EVENTS_KEYS
    )
    _validate_event_point(
        events["brake_onset"],
        name=f"human label {index} brake_onset",
        track_length_mm=track_length_mm,
        minimum_tolerance_mm=int(policy["minimum_mm"]),
        maximum_tolerance_mm=int(policy["brake_onset_max_mm"]),
    )
    apexes = _list(events["apexes"], f"human label {index} apexes")
    if not apexes:
        raise DrivingLabelsError(f"human label {index} must contain an apex")
    for apex_index, apex in enumerate(apexes, start=1):
        _validate_expected_point(
            apex,
            name=f"human label {index} apex {apex_index}",
            track_length_mm=track_length_mm,
            minimum_tolerance_mm=int(policy["minimum_mm"]),
            maximum_tolerance_mm=int(policy["apex_max_mm"]),
        )
    _validate_event_point(
        events["throttle_pickup"],
        name=f"human label {index} throttle_pickup",
        track_length_mm=track_length_mm,
        minimum_tolerance_mm=int(policy["minimum_mm"]),
        maximum_tolerance_mm=int(policy["throttle_pickup_max_mm"]),
    )
    return label


def _validate_review(
    value: object,
    *,
    candidate_sha256: str,
    labels_sha256: str | None,
) -> dict[str, object]:
    review = _mapping(value, "review", _REVIEW_KEYS)
    status = review["status"]
    if status == PENDING_HUMAN_REVIEW:
        if any(review[key] is not None for key in _REVIEW_KEYS - {"status"}):
            raise DrivingLabelsError("pending review cannot carry approval fields")
        return review
    if status not in {APPROVED, REJECTED}:
        raise DrivingLabelsError("review status is invalid")
    if review["authenticity_status"] != SELF_ATTESTED_NOT_AUTHENTICATED:
        raise DrivingLabelsError(
            "review authenticity_status must remain SELF_ATTESTED_NOT_AUTHENTICATED"
        )
    _identifier(review["reviewer_id"], "review reviewer_id")
    _utc_timestamp(review["reviewed_at_utc"], "review reviewed_at_utc")
    _string(review["method"], "review method")
    _sha256(review["evidence_artifact_sha256"], "review evidence_artifact_sha256")
    if review["candidate_hidden_during_first_pass"] is not True:
        raise DrivingLabelsError("review must attest to a blind first pass")
    if review["attestation"] != HUMAN_REVIEW_ATTESTATION:
        raise DrivingLabelsError("review attestation is invalid")
    if review["candidate_payload_sha256"] != candidate_sha256:
        raise DrivingLabelsError("review candidate hash mismatch")
    if status == APPROVED:
        if labels_sha256 is None or review["labels_content_sha256"] != labels_sha256:
            raise DrivingLabelsError("review labels hash mismatch")
        if review["decision_reason"] is not None:
            raise DrivingLabelsError("approved review cannot carry a rejection reason")
    else:
        if review["labels_content_sha256"] is not None:
            raise DrivingLabelsError("rejected review cannot bind approved labels")
        _string(review["decision_reason"], "review decision_reason")
    return review


def _validate_artifact_structure(payload: dict[str, Any]) -> None:
    if payload["contract_version"] != DRIVING_LABELS_CONTRACT_VERSION:
        raise DrivingLabelsError("driving labels contract version is invalid")
    _identifier(payload["label_set_id"], "label_set_id")
    _integer(payload["revision"], "revision", minimum=1)
    subject = _mapping(payload["subject"], "subject", _SUBJECT_KEYS)
    for field in ("car_key", "track_key", "layout_key"):
        _identifier(subject[field], f"subject {field}")
    track_length_mm = _integer(
        subject["track_length_mm"],
        "subject track_length_mm",
        minimum=100_001,
        maximum=100_000_000,
    )
    if subject["coordinate_system"] != "lap-distance-from-start-finish-v1":
        raise DrivingLabelsError("subject coordinate_system is invalid")
    _string(subject["condition_scope"], "subject condition_scope")

    basis = _mapping(payload["candidate_basis"], "candidate_basis", _CANDIDATE_BASIS_KEYS)
    if basis["status"] != CANDIDATE_NOT_GOLDEN:
        raise DrivingLabelsError("candidate basis must remain CANDIDATE_NOT_GOLDEN")
    _run_identifier(basis["source_id"], "candidate source_id")
    _run_identifier(basis["session_id"], "candidate session_id")
    if basis["input_kind"] not in {"ibt", "collector"}:
        raise DrivingLabelsError("candidate input_kind is invalid")
    for field in (
        "input_provenance_sha256",
        "source_data_sha256",
        "normalized_samples_sha256",
        "driving_context_sha256",
        "model_output_sha256",
        "model_semantic_sha256",
        "pipeline_sha256",
    ):
        _sha256(basis[field], f"candidate {field}")
    _integer(basis["grid_step_mm"], "candidate grid_step_mm", minimum=250, maximum=10_000)
    _integer(basis["labeled_lap_ordinal"], "candidate labeled_lap_ordinal", minimum=1)

    policy = _validate_tolerance_policy(payload["tolerance_policy"])
    proposals = _list(payload["proposals"], "proposals")
    if not proposals:
        raise DrivingLabelsError("proposal list cannot be empty")
    proposal_ids: list[str] = []
    proposal_ordinals: list[int] = []
    for index, raw_proposal in enumerate(proposals, start=1):
        proposal = _validate_proposal(
            raw_proposal, index=index, track_length_mm=track_length_mm
        )
        proposal_ids.append(str(proposal["proposal_id"]))
        proposal_ordinals.append(int(proposal["ordinal"]))
    if len(set(proposal_ids)) != len(proposal_ids):
        raise DrivingLabelsError("proposal ids must be unique")
    if proposal_ordinals != list(range(1, len(proposals) + 1)):
        raise DrivingLabelsError("proposal ordinals must be contiguous and ordered")

    human_labels = _list(payload["human_labels"], "human_labels")
    validated_labels = [
        _validate_human_label(
            raw_label,
            index=index,
            track_length_mm=track_length_mm,
            policy=policy,
        )
        for index, raw_label in enumerate(human_labels, start=1)
    ]
    label_ids = [str(label["label_id"]) for label in validated_labels]
    if len(set(label_ids)) != len(label_ids):
        raise DrivingLabelsError("human label ids must be unique")
    reviewed_proposals = [
        str(label["source_proposal_id"])
        for label in validated_labels
        if label["source_proposal_id"] is not None
    ]
    if len(set(reviewed_proposals)) != len(reviewed_proposals):
        raise DrivingLabelsError("a proposal may be reviewed only once")
    if not set(reviewed_proposals).issubset(proposal_ids):
        raise DrivingLabelsError("human label references an unknown proposal")
    expected_labels = [
        label for label in validated_labels if label["decision"] != "REJECTED_PROPOSAL"
    ]
    expected_ordinals = [int(label["ordinal"]) for label in expected_labels]
    if expected_ordinals != list(range(1, len(expected_labels) + 1)):
        raise DrivingLabelsError("approved corner ordinals must be contiguous and ordered")
    proposal_ordinal_by_id = {
        str(proposal["proposal_id"]): int(proposal["ordinal"])
        for proposal in proposals
    }
    retained_source_ordinals = [
        proposal_ordinal_by_id[str(label["source_proposal_id"])]
        for label in expected_labels
        if label["decision"] in {"CONFIRMED", "CORRECTED"}
    ]
    if any(
        current <= previous
        for previous, current in zip(
            retained_source_ordinals,
            retained_source_ordinals[1:],
            strict=False,
        )
    ):
        raise DrivingLabelsError(
            "retained proposal mappings must preserve proposal track order"
        )


def seal_driving_labels(payload: Mapping[str, object]) -> dict[str, object]:
    """Recompute hashes for a still-pending model candidate only.

    Approved and rejected artifacts are immutable inputs from a separate
    self-attested review workflow. This helper intentionally refuses to
    create or refresh either human decision.
    """

    if type(payload) is not dict:
        raise DrivingLabelsError("driving labels must be a plain object")
    sealed = copy.deepcopy(payload)
    _mapping(sealed, "driving labels", _TOP_LEVEL_KEYS)
    review = sealed.get("review")
    if type(review) is not dict or review.get("status") != PENDING_HUMAN_REVIEW:
        raise DrivingLabelsError("seal_driving_labels only accepts pending candidates")
    _validate_artifact_structure(sealed)
    if sealed["human_labels"]:
        raise DrivingLabelsError("pending candidate cannot carry human labels")
    candidate_sha256 = canonical_sha256(_candidate_material(sealed))
    sealed["candidate_payload_sha256"] = candidate_sha256
    sealed["labels_content_sha256"] = None
    sealed["review_sha256"] = None
    sealed["artifact_sha256"] = canonical_sha256(_artifact_material(sealed))
    return validate_driving_labels(sealed)


def validate_driving_labels(
    payload: Mapping[str, object],
    *,
    require_approved: bool = False,
    require_trusted: bool = False,
) -> dict[str, object]:
    """Validate structure, workflow approval, and public integrity checksums.

    ``require_approved`` checks only the self-declared workflow state; it does
    not authenticate the reviewer. ``require_trusted`` additionally requires
    an authenticated trust anchor. Driving-labels-v1 defines no such anchor,
    so a self-attested approval fails closed at that gate.
    """

    if type(payload) is not dict:
        raise DrivingLabelsError("driving labels must be a plain object")
    validated = copy.deepcopy(payload)
    _mapping(validated, "driving labels", _TOP_LEVEL_KEYS)
    _validate_artifact_structure(validated)
    candidate_sha256 = _sha256(
        validated["candidate_payload_sha256"], "candidate_payload_sha256"
    )
    if canonical_sha256(_candidate_material(validated)) != candidate_sha256:
        raise DrivingLabelsError("candidate payload hash mismatch")
    review_status = validated["review"]["status"]
    labels_sha256: str | None = None
    if review_status == APPROVED:
        if not validated["human_labels"]:
            raise DrivingLabelsError("approved label set cannot be empty")
        reviewed = {
            label["source_proposal_id"]
            for label in validated["human_labels"]
            if label["source_proposal_id"] is not None
        }
        proposal_ids = {item["proposal_id"] for item in validated["proposals"]}
        if reviewed != proposal_ids:
            raise DrivingLabelsError("approved review must disposition every proposal")
        labels_sha256 = _sha256(
            validated["labels_content_sha256"], "labels_content_sha256"
        )
        if canonical_sha256(_labels_material(validated)) != labels_sha256:
            raise DrivingLabelsError("labels content hash mismatch")
    elif validated["labels_content_sha256"] is not None:
        raise DrivingLabelsError("non-approved artifact cannot carry labels content hash")
    _validate_review(
        validated["review"],
        candidate_sha256=candidate_sha256,
        labels_sha256=labels_sha256,
    )
    if review_status == PENDING_HUMAN_REVIEW:
        if validated["human_labels"]:
            raise DrivingLabelsError("pending candidate cannot carry human labels")
        if validated["review_sha256"] is not None:
            raise DrivingLabelsError("pending candidate cannot carry a review hash")
    else:
        review_sha256 = _sha256(validated["review_sha256"], "review_sha256")
        if canonical_sha256(validated["review"]) != review_sha256:
            raise DrivingLabelsError("review hash mismatch")
    artifact_sha256 = _sha256(validated["artifact_sha256"], "artifact_sha256")
    if canonical_sha256(_artifact_material(validated)) != artifact_sha256:
        raise DrivingLabelsError("artifact hash mismatch")
    if require_approved and review_status != APPROVED:
        raise DrivingLabelsError("WAIT_HUMAN_LABELS")
    if require_trusted:
        if review_status != APPROVED:
            raise DrivingLabelsError("WAIT_HUMAN_LABELS")
        raise DrivingLabelsError(WAIT_HUMAN_AUTHENTICATION)
    return validated


def _circular_error_mm(predicted: int, expected: int, track_length_mm: int) -> int:
    direct = abs(predicted - expected) % track_length_mm
    return min(direct, track_length_mm - direct)


def _point_result(
    *,
    field: str,
    predicted_mm: int | None,
    expected_mm: int | None,
    tolerance_mm: int | None,
    expectation: str,
    track_length_mm: int,
) -> dict[str, object]:
    if expectation == "ABSENT":
        status = "PASS" if predicted_mm is None else "FAIL"
        error_mm = None
    elif predicted_mm is None:
        status = "FAIL"
        error_mm = None
    else:
        assert expected_mm is not None and tolerance_mm is not None
        error_mm = _circular_error_mm(predicted_mm, expected_mm, track_length_mm)
        status = "PASS" if error_mm <= tolerance_mm else "FAIL"
    return {
        "field": field,
        "expectation": expectation,
        "expected_mm": expected_mm,
        "predicted_mm": predicted_mm,
        "tolerance_mm": tolerance_mm,
        "error_mm": error_mm,
        "status": status,
    }


def _present_result(
    *,
    field: str,
    predicted_mm: int,
    point: Mapping[str, object],
    track_length_mm: int,
) -> dict[str, object]:
    return _point_result(
        field=field,
        predicted_mm=predicted_mm,
        expected_mm=int(point["expected_mm"]),
        tolerance_mm=int(point["tolerance_mm"]),
        expectation="PRESENT",
        track_length_mm=track_length_mm,
    )


def _event_result(
    *,
    field: str,
    predicted_mm: int | None,
    point: Mapping[str, object],
    track_length_mm: int,
) -> dict[str, object]:
    expectation = str(point["expectation"])
    return _point_result(
        field=field,
        predicted_mm=predicted_mm,
        expected_mm=(int(point["expected_mm"]) if point["expected_mm"] is not None else None),
        tolerance_mm=(
            int(point["tolerance_mm"]) if point["tolerance_mm"] is not None else None
        ),
        expectation=expectation,
        track_length_mm=track_length_mm,
    )


def _baseline_comparison(
    basis: Mapping[str, object], replay: Mapping[str, object]
) -> dict[str, bool]:
    return {
        "source_id_match": basis["source_id"] == replay["source_id"],
        "session_id_match": basis["session_id"] == replay["session_id"],
        "input_kind_match": basis["input_kind"] == replay["input_kind"],
        "source_data_sha256_match": (
            basis["source_data_sha256"] == replay["source_data_sha256"]
        ),
        "normalized_samples_sha256_match": (
            basis["normalized_samples_sha256"] == replay["normalized_sha256"]
        ),
        "input_provenance_sha256_match": (
            basis["input_provenance_sha256"]
            == replay["input_provenance_sha256"]
        ),
        "driving_context_sha256_match": (
            basis["driving_context_sha256"] == replay["context_sha256"]
        ),
        "pipeline_sha256_match": (
            basis["pipeline_sha256"] == replay["pipeline_sha256"]
        ),
        "model_output_sha256_match": (
            basis["model_output_sha256"] == replay["model_output_sha256"]
        ),
        "model_semantic_sha256_match": (
            basis["model_semantic_sha256"] == replay["model_semantic_sha256"]
        ),
    }


def _evaluated_input_identity(replay: Mapping[str, object]) -> dict[str, object]:
    return {
        "driving_replay_sha256": replay["driving_replay_sha256"],
        "source_id": replay["source_id"],
        "session_id": replay["session_id"],
        "input_kind": replay["input_kind"],
        "source_data_sha256": replay["source_data_sha256"],
        "normalized_samples_sha256": replay["normalized_sha256"],
        "input_provenance_sha256": replay["input_provenance_sha256"],
        "driving_context_sha256": replay["context_sha256"],
    }


def _has_provenance_mismatch(comparison: Mapping[str, bool]) -> bool:
    return any(not comparison[key] for key in _BLOCKING_BASELINE_COMPARISONS)


def _human_review_authenticity(review_status: object) -> dict[str, object]:
    if review_status != APPROVED:
        return {
            "authenticated": False,
            "reasons": ["APPROVED_LABEL_SET_MISSING"],
            "status": "WAIT_HUMAN_LABELS",
        }
    return {
        "authenticated": False,
        "reasons": [SELF_ATTESTED_NOT_AUTHENTICATED],
        "status": WAIT_HUMAN_AUTHENTICATION,
    }


def _prediction_coordinate(
    source: Mapping[str, object],
    key: str,
    name: str,
    track_length_mm: int,
    *,
    allow_none: bool = False,
) -> tuple[int | None, str | None]:
    if key not in source:
        return None, f"REQUIRED_MODEL_FIELD_MISSING:{key}"
    try:
        coordinate = _model_coordinate_mm(
            source[key],
            name,
            track_length_mm,
            allow_none=allow_none,
        )
    except DrivingLabelsError:
        return None, f"MODEL_COORDINATE_INVALID:{key}"
    return coordinate, None


def _failed_prediction_result(
    *, field: str, point: Mapping[str, object], is_event: bool
) -> dict[str, object]:
    expectation = str(point["expectation"]) if is_event else "PRESENT"
    return {
        "field": field,
        "expectation": expectation,
        "expected_mm": point["expected_mm"],
        "predicted_mm": None,
        "tolerance_mm": point["tolerance_mm"],
        "error_mm": None,
        "status": "FAIL",
    }


def regress_driving_labels(
    labels: Mapping[str, object], driving_replay: Mapping[str, object]
) -> dict[str, object]:
    """Compare an approved label set to model output in fixed track order."""

    validated = validate_driving_labels(labels)
    replay = _validate_ready_replay(driving_replay)
    basis = validated["candidate_basis"]
    assert isinstance(basis, dict)
    baseline_comparison = _baseline_comparison(basis, replay)
    review_status = validated["review"]["status"]
    human_review_authenticity = _human_review_authenticity(review_status)
    common_binding: dict[str, object] = {
        "contract_version": DRIVING_LABEL_REGRESSION_CONTRACT_VERSION,
        "comparator_version": DRIVING_LABEL_COMPARATOR_VERSION,
        "label_artifact_sha256": validated["artifact_sha256"],
        "candidate_payload_sha256": validated["candidate_payload_sha256"],
        "labels_content_sha256": validated["labels_content_sha256"],
        "evaluated_driving_replay_sha256": replay["driving_replay_sha256"],
        "evaluated_input_identity": _evaluated_input_identity(replay),
        "baseline_comparison": baseline_comparison,
        "human_review_authenticity": human_review_authenticity,
        "evaluated_model_output_sha256": replay["model_output_sha256"],
        "evaluated_model_semantic_sha256": replay["model_semantic_sha256"],
        "evaluated_pipeline_sha256": replay["pipeline_sha256"],
    }
    if review_status != APPROVED:
        reasons = ["LABEL_SET_NOT_APPROVED"]
        if _has_provenance_mismatch(baseline_comparison):
            reasons.append("PROVENANCE_MISMATCH")
        binding: dict[str, object] = {
            **common_binding,
            "comparator_status": "NOT_RUN",
            "comparator_reasons": reasons,
            "status": "WAIT_HUMAN_LABELS",
            "trusted_regression_status": "WAIT_HUMAN_LABELS",
            "reasons": reasons,
            "summary": {
                "expected_corner_count": 0,
                "predicted_corner_count": len(replay["corners"]),
                "passed_field_count": 0,
                "failed_field_count": 0,
            },
            "corner_results": [],
        }
        return {**binding, "regression_result_sha256": canonical_sha256(binding)}

    subject = validated["subject"]
    assert isinstance(subject, dict)
    track_length_mm = int(subject["track_length_mm"])
    expected_labels = [
        label
        for label in validated["human_labels"]
        if label["decision"] != "REJECTED_PROPOSAL"
    ]
    predictions = replay["corners"]
    gate_reasons: list[str] = []
    if _has_provenance_mismatch(baseline_comparison):
        gate_reasons.append("PROVENANCE_MISMATCH")
    if replay["track_length_mm"] != track_length_mm:
        gate_reasons.append("TRACK_LENGTH_MISMATCH")
    if gate_reasons:
        binding = {
            **common_binding,
            "comparator_status": "FAIL",
            "comparator_reasons": gate_reasons,
            "status": "FAIL",
            "trusted_regression_status": "FAIL",
            "reasons": gate_reasons,
            "summary": {
                "expected_corner_count": len(expected_labels),
                "predicted_corner_count": len(predictions),
                "passed_field_count": 0,
                "failed_field_count": 0,
            },
            "corner_results": [],
        }
        return {**binding, "regression_result_sha256": canonical_sha256(binding)}

    reasons: list[str] = []
    if len(predictions) != len(expected_labels):
        reasons.append("CORNER_COUNT_MISMATCH")

    labeled_lap_ordinal = validated["candidate_basis"]["labeled_lap_ordinal"]
    metrics_by_corner: dict[str, dict[str, object]] = {}
    for raw_metric in replay["metrics"]:
        if type(raw_metric) is not dict:
            raise DrivingLabelsError("driving replay corner metric must be an object")
        if raw_metric.get("lap_ordinal") != labeled_lap_ordinal:
            continue
        corner_id = _string(raw_metric.get("corner_id"), "corner metric id")
        if corner_id in metrics_by_corner:
            raise DrivingLabelsError(f"duplicate labeled-lap metric for {corner_id}")
        metrics_by_corner[corner_id] = raw_metric

    corner_results: list[dict[str, object]] = []
    passed_fields = 0
    failed_fields = 0
    for label, raw_corner in zip(expected_labels, predictions, strict=False):
        if type(raw_corner) is not dict:
            raise DrivingLabelsError("driving replay corner must be an object")
        model_corner_id = _string(raw_corner.get("corner_id"), "model corner id")
        metric = metrics_by_corner.get(model_corner_id)
        field_results: list[dict[str, object]] = []
        corner_reasons: list[str] = []
        if metric is None:
            corner_reasons.append("LABELED_LAP_METRIC_MISSING")
            failed_fields += 1
        else:
            window = label["detected_window"]
            events = label["events"]
            start_mm, start_reason = _prediction_coordinate(
                raw_corner,
                "brake_start_m",
                "window start",
                track_length_mm,
            )
            end_mm, end_reason = _prediction_coordinate(
                raw_corner, "exit_m", "window end", track_length_mm
            )
            brake_mm, brake_reason = _prediction_coordinate(
                metric,
                "brake_onset_m",
                "brake onset",
                track_length_mm,
                allow_none=True,
            )
            throttle_mm, throttle_reason = _prediction_coordinate(
                metric,
                "throttle_pickup_m",
                "throttle pickup",
                track_length_mm,
                allow_none=True,
            )
            for prediction_reason in (
                start_reason,
                end_reason,
                brake_reason,
                throttle_reason,
            ):
                if prediction_reason is not None:
                    corner_reasons.append(prediction_reason)
            field_results.extend(
                [
                    (
                        _failed_prediction_result(
                            field="window_start",
                            point=window["start"],
                            is_event=False,
                        )
                        if start_reason is not None
                        else _present_result(
                            field="window_start",
                            predicted_mm=start_mm,
                            point=window["start"],
                            track_length_mm=track_length_mm,
                        )
                    ),
                    (
                        _failed_prediction_result(
                            field="window_end",
                            point=window["end"],
                            is_event=False,
                        )
                        if end_reason is not None
                        else _present_result(
                            field="window_end",
                            predicted_mm=end_mm,
                            point=window["end"],
                            track_length_mm=track_length_mm,
                        )
                    ),
                    (
                        _failed_prediction_result(
                            field="brake_onset",
                            point=events["brake_onset"],
                            is_event=True,
                        )
                        if brake_reason is not None
                        else _event_result(
                            field="brake_onset",
                            predicted_mm=brake_mm,
                            point=events["brake_onset"],
                            track_length_mm=track_length_mm,
                        )
                    ),
                    (
                        _failed_prediction_result(
                            field="throttle_pickup",
                            point=events["throttle_pickup"],
                            is_event=True,
                        )
                        if throttle_reason is not None
                        else _event_result(
                            field="throttle_pickup",
                            predicted_mm=throttle_mm,
                            point=events["throttle_pickup"],
                            track_length_mm=track_length_mm,
                        )
                    ),
                ]
            )
            apexes = events["apexes"]
            if len(apexes) != 1:
                corner_reasons.append("APEX_COUNT_MISMATCH")
                failed_fields += 1
            else:
                apex_mm, apex_reason = _prediction_coordinate(
                    metric, "apex_m", "apex", track_length_mm
                )
                if apex_reason is not None:
                    corner_reasons.append(apex_reason)
                field_results.append(
                    _failed_prediction_result(
                        field="apex:1", point=apexes[0], is_event=False
                    )
                    if apex_reason is not None
                    else _present_result(
                        field="apex:1",
                        predicted_mm=apex_mm,
                        point=apexes[0],
                        track_length_mm=track_length_mm,
                    )
                )
        passed_fields += sum(item["status"] == "PASS" for item in field_results)
        failed_fields += sum(item["status"] == "FAIL" for item in field_results)
        corner_status = (
            "PASS"
            if not corner_reasons and all(item["status"] == "PASS" for item in field_results)
            else "FAIL"
        )
        corner_results.append(
            {
                "label_id": label["label_id"],
                "ordinal": label["ordinal"],
                "model_corner_id": model_corner_id,
                "status": corner_status,
                "reasons": corner_reasons,
                "field_results": field_results,
            }
        )
    if len(expected_labels) > len(predictions):
        for label in expected_labels[len(predictions) :]:
            corner_results.append(
                {
                    "label_id": label["label_id"],
                    "ordinal": label["ordinal"],
                    "model_corner_id": None,
                    "status": "FAIL",
                    "reasons": ["MODEL_CORNER_MISSING"],
                    "field_results": [],
                }
            )
            failed_fields += 1
    if any(result["status"] == "FAIL" for result in corner_results):
        reasons.append("FIELD_TOLERANCE_FAILURE")
    reasons = list(dict.fromkeys(reasons))
    comparator_status = "PASS" if not reasons else "FAIL"
    trusted_status = (
        WAIT_HUMAN_AUTHENTICATION
        if comparator_status == "PASS"
        else comparator_status
    )
    binding = {
        **common_binding,
        "comparator_status": comparator_status,
        "comparator_reasons": reasons,
        "status": trusted_status,
        "trusted_regression_status": trusted_status,
        "reasons": (
            [SELF_ATTESTED_NOT_AUTHENTICATED]
            if comparator_status == "PASS"
            else reasons
        ),
        "summary": {
            "expected_corner_count": len(expected_labels),
            "predicted_corner_count": len(predictions),
            "passed_field_count": passed_fields,
            "failed_field_count": failed_fields,
        },
        "corner_results": corner_results,
    }
    return {**binding, "regression_result_sha256": canonical_sha256(binding)}


__all__ = [
    "APPROVED",
    "CANDIDATE_NOT_GOLDEN",
    "DEFAULT_TOLERANCE_POLICY",
    "DRIVING_LABEL_COMPARATOR_VERSION",
    "DRIVING_LABEL_REGRESSION_CONTRACT_VERSION",
    "DRIVING_LABELS_CONTRACT_VERSION",
    "DrivingLabelsError",
    "HUMAN_REVIEW_ATTESTATION",
    "PENDING_HUMAN_REVIEW",
    "REJECTED",
    "SELF_ATTESTED_NOT_AUTHENTICATED",
    "WAIT_HUMAN_AUTHENTICATION",
    "build_driving_label_candidate",
    "canonical_sha256",
    "regress_driving_labels",
    "seal_driving_labels",
    "validate_driving_labels",
]
