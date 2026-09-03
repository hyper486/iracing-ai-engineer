"""One provenance-bound, advisor-only offline engineer demonstration.

The demo is intentionally an orchestration contract rather than a new model.
It runs the existing IBT-only shadow report and the source-neutral fuel,
driving, and condition pipelines, then proves that their inputs describe the
same immutable recording.  A pending driving-label candidate is validated and
bound to the current driving replay without being promoted to human truth.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .adapters import open_ibt_telemetry
from .capabilities import INFERENCE_CAPABILITY_CONTRACT_VERSION
from .condition_cohort import (
    CONDITION_COHORT_CONTRACT_VERSION,
    build_condition_cohort,
)
from .contracts import SHADOW_REPORT_CONTRACT_VERSION
from .driving_labels import (
    CANDIDATE_NOT_GOLDEN,
    DRIVING_LABELS_CONTRACT_VERSION,
    PENDING_HUMAN_REVIEW,
    validate_driving_labels,
)
from .driving_model_replay import (
    DRIVING_MODEL_REPLAY_CONTRACT_VERSION,
    build_driving_model_replay,
)
from .fuel import FuelScenario
from .model_replay import FUEL_MODEL_REPLAY_CONTRACT_VERSION, build_fuel_model_replay
from .shadow import build_shadow_report

OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION = "offline-engineer-demo-v1"

_HEX_DIGITS = frozenset("0123456789abcdef")
_CANDIDATE_BASIS_KEYS = (
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
)
_UNAVAILABLE_CAPABILITY_KEYS = frozenset(
    {
        "blocked_claims",
        "confidence",
        "contract_version",
        "estimate_available",
        "provenance",
        "reasons",
        "status",
    }
)

RunOpener = Callable[..., AbstractContextManager[object]]
ShadowBuilder = Callable[..., dict[str, object]]
FuelBuilder = Callable[..., dict[str, object]]
DrivingBuilder = Callable[..., dict[str, object]]
ConditionBuilder = Callable[..., dict[str, object]]
LabelValidator = Callable[..., dict[str, object]]


class OfflineEngineerDemoError(ValueError):
    """Raised when component evidence cannot form one honest demo receipt."""


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
        raise OfflineEngineerDemoError("offline demo value is not canonical-JSON-safe") from exc


def canonical_sha256(value: object) -> str:
    """Return the deterministic digest used by ``offline-engineer-demo-v1``."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise OfflineEngineerDemoError(f"{name} must be a plain object")
    return value


def _list(value: object, name: str) -> list[Any]:
    if type(value) is not list:
        raise OfflineEngineerDemoError(f"{name} must be a JSON array")
    return value


def _string(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise OfflineEngineerDemoError(f"{name} must be a non-empty string")
    return value


def _identifier(value: object, name: str) -> str:
    text = _string(value, name)
    if (
        text != text.strip()
        or len(text.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in text)
    ):
        raise OfflineEngineerDemoError(f"{name} is not a valid bound identifier")
    return text


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise OfflineEngineerDemoError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _require_equal(actual: object, expected: object, name: str) -> None:
    if actual != expected:
        raise OfflineEngineerDemoError(f"{name} mismatch")


def _plain_target_lap(value: object) -> int:
    if type(value) is not int or value < 1:
        raise OfflineEngineerDemoError("target_lap_ordinal must be a positive integer")
    return value


def _grid_step_mm(driving: Mapping[str, object]) -> int:
    pipeline = _mapping(driving.get("pipeline"), "driving pipeline")
    config = _mapping(pipeline.get("driving_config"), "driving config")
    raw = config.get("grid_step_m")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise OfflineEngineerDemoError("driving grid_step_m must be numeric")
    metres = float(raw)
    if not math.isfinite(metres):
        raise OfflineEngineerDemoError("driving grid_step_m must be finite")
    millimetres = metres * 1_000.0
    rounded = round(millimetres)
    if abs(millimetres - rounded) > 1e-6:
        raise OfflineEngineerDemoError("driving grid_step_m must resolve to integer millimetres")
    return int(rounded)


def _copy_gate(value: object, name: str) -> dict[str, object]:
    gate = _mapping(value, name)
    _string(gate.get("status"), f"{name} status")
    reasons = _list(gate.get("reasons"), f"{name} reasons")
    if any(type(reason) is not str or not reason for reason in reasons):
        raise OfflineEngineerDemoError(f"{name} reasons must be non-empty strings")
    return copy.deepcopy(gate)


def _copy_unavailable_capability(value: object, name: str) -> dict[str, object]:
    capability = _mapping(value, name)
    if set(capability) != _UNAVAILABLE_CAPABILITY_KEYS:
        raise OfflineEngineerDemoError(f"{name} keys are invalid")
    if (
        capability.get("contract_version") != INFERENCE_CAPABILITY_CONTRACT_VERSION
        or capability.get("status") != "SKIP"
        or capability.get("estimate_available") is not False
        or capability.get("confidence") != "NONE"
        or capability.get("provenance") != "UNKNOWN"
    ):
        raise OfflineEngineerDemoError(f"{name} must remain explicitly unavailable")
    for field in ("reasons", "blocked_claims"):
        values = _list(capability.get(field), f"{name} {field}")
        if not values or any(type(item) is not str or not item for item in values):
            raise OfflineEngineerDemoError(f"{name} {field} must contain non-empty strings")
    return copy.deepcopy(capability)


def _copy_non_estimate_gate(value: object, name: str) -> dict[str, object]:
    capability = _mapping(value, name)
    if capability.get("estimate_available") is not False:
        raise OfflineEngineerDemoError(f"{name} must not expose an estimate")
    _string(capability.get("status"), f"{name} status")
    reasons = _list(capability.get("reasons"), f"{name} reasons")
    if any(type(reason) is not str or not reason for reason in reasons):
        raise OfflineEngineerDemoError(f"{name} reasons must be non-empty strings")
    return copy.deepcopy(capability)


def _recommendations(value: object, name: str) -> list[dict[str, object]]:
    items = _list(value, f"{name} recommendations")
    result: list[dict[str, object]] = []
    for index, raw in enumerate(items):
        item = _mapping(raw, f"{name} recommendation {index}")
        if item.get("executable") is not False:
            raise OfflineEngineerDemoError(
                f"{name} recommendation {index} is executable or lacks an explicit false gate"
            )
        result.append(copy.deepcopy(item))
    return result


def _input_evidence(component: Mapping[str, object], name: str) -> dict[str, Any]:
    if component.get("input_kind") != "ibt":
        raise OfflineEngineerDemoError(f"{name} must use the IBT input kind")
    evidence = _mapping(component.get("input_evidence"), f"{name} input evidence")
    _identifier(evidence.get("source_id"), f"{name} source_id")
    _identifier(evidence.get("session_id"), f"{name} session_id")
    _sha256(evidence.get("source_sha256"), f"{name} source_sha256")
    return evidence


def _normalized_receipt(component: Mapping[str, object], name: str) -> dict[str, Any]:
    receipt = _mapping(
        component.get("normalized_input_receipt"),
        f"{name} normalized input receipt",
    )
    _string(receipt.get("contract_version"), f"{name} normalized contract")
    sample_count = receipt.get("sample_count")
    if type(sample_count) is not int or sample_count < 1:
        raise OfflineEngineerDemoError(f"{name} normalized sample_count must be a positive integer")
    _sha256(receipt.get("samples_sha256"), f"{name} normalized samples_sha256")
    return receipt


def _track_length_mm(context: Mapping[str, object], name: str) -> int:
    value = context.get("track_length_mm")
    if type(value) is not int or value <= 100_000:
        raise OfflineEngineerDemoError(f"{name} track_length_mm is invalid")
    return value


def _candidate_binding(
    labels: Mapping[str, object],
    driving: Mapping[str, object],
    *,
    source_id: str,
    session_id: str,
    target_lap_ordinal: int,
    input_evidence: Mapping[str, object],
    normalized_receipt: Mapping[str, object],
    track_context: Mapping[str, object],
) -> dict[str, object]:
    review = _mapping(labels.get("review"), "driving-label review")
    if review.get("status") != PENDING_HUMAN_REVIEW:
        raise OfflineEngineerDemoError(
            "offline demo requires a PENDING_HUMAN_REVIEW label candidate"
        )
    if review.get("authenticity_status") is not None:
        raise OfflineEngineerDemoError(
            "pending driving-label candidate cannot carry authentication"
        )
    if labels.get("human_labels") != []:
        raise OfflineEngineerDemoError("pending driving-label candidate cannot carry human labels")

    model_output = _mapping(driving.get("model_output"), "driving model output")
    reference = _mapping(model_output.get("reference"), "driving reference")
    reference_lap_ordinal = _plain_target_lap(reference.get("lap_ordinal"))
    _require_equal(
        reference_lap_ordinal,
        target_lap_ordinal,
        "condition target lap/driving reference",
    )
    pipeline = _mapping(driving.get("pipeline"), "driving pipeline")
    expected = {
        "status": CANDIDATE_NOT_GOLDEN,
        "source_id": source_id,
        "session_id": session_id,
        "input_kind": "ibt",
        "input_provenance_sha256": _sha256(
            driving.get("input_provenance_sha256"),
            "driving input_provenance_sha256",
        ),
        "source_data_sha256": _sha256(
            input_evidence.get("source_sha256"),
            "driving source_data_sha256",
        ),
        "normalized_samples_sha256": _sha256(
            normalized_receipt.get("samples_sha256"),
            "driving normalized_samples_sha256",
        ),
        "driving_context_sha256": _sha256(
            driving.get("driving_context_sha256"),
            "driving context sha256",
        ),
        "model_output_sha256": _sha256(
            driving.get("model_output_sha256"),
            "driving model_output_sha256",
        ),
        "model_semantic_sha256": _sha256(
            driving.get("model_semantic_sha256"),
            "driving model_semantic_sha256",
        ),
        "pipeline_sha256": _sha256(
            pipeline.get("pipeline_sha256"),
            "driving pipeline_sha256",
        ),
        "grid_step_mm": _grid_step_mm(driving),
        "labeled_lap_ordinal": reference_lap_ordinal,
    }
    basis = _mapping(labels.get("candidate_basis"), "driving-label candidate basis")
    comparisons = {f"{key}_match": basis.get(key) == expected[key] for key in _CANDIDATE_BASIS_KEYS}
    if not all(comparisons.values()):
        mismatches = sorted(key for key, matched in comparisons.items() if not matched)
        raise OfflineEngineerDemoError(f"driving-label candidate basis mismatch: {mismatches}")
    subject = _mapping(labels.get("subject"), "driving-label subject")
    _require_equal(
        subject.get("track_length_mm"),
        _track_length_mm(track_context, "shared"),
        "driving-label track context",
    )
    return {
        "comparisons": comparisons,
        "status": "PASS",
    }


def build_offline_engineer_demo(
    path: Path,
    *,
    source_id: str,
    session_id: str,
    fuel_scenario: FuelScenario,
    target_lap_ordinal: int,
    pending_label_payload: Mapping[str, object],
    open_ibt_run: RunOpener = open_ibt_telemetry,
    shadow_builder: ShadowBuilder = build_shadow_report,
    fuel_builder: FuelBuilder = build_fuel_model_replay,
    driving_builder: DrivingBuilder = build_driving_model_replay,
    condition_builder: ConditionBuilder = build_condition_cohort,
    label_validator: LabelValidator = validate_driving_labels,
) -> dict[str, object]:
    """Run and bind every offline Audi/Spa MVP component in shadow mode.

    Each source-neutral model receives a fresh, independently active IBT
    adapter context.  Expected condition and human-review waits remain visible
    gates and do not make a successfully completed demonstration an error.
    """

    if not isinstance(path, Path):
        raise OfflineEngineerDemoError("path must be a pathlib.Path")
    bound_source_id = _identifier(source_id, "source_id")
    bound_session_id = _identifier(session_id, "session_id")
    if not isinstance(fuel_scenario, FuelScenario):
        raise OfflineEngineerDemoError("fuel_scenario must be a FuelScenario")
    target_lap = _plain_target_lap(target_lap_ordinal)
    scenario = fuel_scenario.to_dict()
    scenario_sha256 = canonical_sha256(scenario)

    shadow = _mapping(
        shadow_builder(
            path,
            analysis="all",
            fuel_scenario=fuel_scenario,
        ),
        "shadow report",
    )
    with open_ibt_run(
        path,
        source_id=bound_source_id,
        session_id=bound_session_id,
    ) as run:
        fuel = _mapping(
            fuel_builder(run, scenario=fuel_scenario),
            "shared fuel replay",
        )
    with open_ibt_run(
        path,
        source_id=bound_source_id,
        session_id=bound_session_id,
    ) as run:
        driving = _mapping(driving_builder(run), "shared driving replay")
    with open_ibt_run(
        path,
        source_id=bound_source_id,
        session_id=bound_session_id,
    ) as run:
        condition = _mapping(
            condition_builder(run, target_lap_ordinal=target_lap),
            "condition cohort",
        )
    labels = _mapping(
        label_validator(pending_label_payload),
        "validated driving-label candidate",
    )

    _require_equal(
        shadow.get("contract_version"),
        SHADOW_REPORT_CONTRACT_VERSION,
        "shadow contract version",
    )
    _require_equal(
        fuel.get("contract_version"),
        FUEL_MODEL_REPLAY_CONTRACT_VERSION,
        "fuel contract version",
    )
    _require_equal(
        driving.get("contract_version"),
        DRIVING_MODEL_REPLAY_CONTRACT_VERSION,
        "driving contract version",
    )
    _require_equal(
        condition.get("contract_version"),
        CONDITION_COHORT_CONTRACT_VERSION,
        "condition contract version",
    )
    _require_equal(
        labels.get("contract_version"),
        DRIVING_LABELS_CONTRACT_VERSION,
        "driving-label contract version",
    )
    _require_equal(shadow.get("execution_mode"), "SHADOW", "shadow execution mode")

    fuel_evidence = _input_evidence(fuel, "fuel")
    driving_evidence = _input_evidence(driving, "driving")
    condition_evidence = _input_evidence(condition, "condition")
    _require_equal(driving_evidence, fuel_evidence, "fuel/driving raw input evidence")
    _require_equal(condition_evidence, fuel_evidence, "fuel/condition raw input evidence")
    _require_equal(
        fuel_evidence.get("source_id"),
        bound_source_id,
        "bound source_id",
    )
    _require_equal(
        fuel_evidence.get("session_id"),
        bound_session_id,
        "bound session_id",
    )
    source_sha256 = _sha256(fuel_evidence.get("source_sha256"), "source_sha256")
    shadow_source = _mapping(shadow.get("source"), "shadow source")
    _require_equal(shadow_source.get("source_mode"), "IBT", "shadow source mode")
    _require_equal(
        shadow_source.get("source_sha256"),
        source_sha256,
        "shadow/shared raw source",
    )

    fuel_normalized = _normalized_receipt(fuel, "fuel")
    driving_normalized = _normalized_receipt(driving, "driving")
    condition_normalized = _normalized_receipt(condition, "condition")
    _require_equal(
        driving_normalized,
        fuel_normalized,
        "fuel/driving normalized input receipt",
    )
    _require_equal(
        condition_normalized,
        fuel_normalized,
        "fuel/condition normalized input receipt",
    )

    _require_equal(fuel.get("scenario"), scenario, "shared fuel scenario")
    _require_equal(
        fuel.get("scenario_sha256"),
        scenario_sha256,
        "shared fuel scenario hash",
    )
    shadow_config = _mapping(shadow.get("config"), "shadow config")
    _require_equal(
        shadow_config.get("fuel_scenario"),
        scenario,
        "shadow fuel scenario",
    )

    fuel_event_receipt = _mapping(fuel.get("event_receipt"), "fuel event receipt")
    driving_event_receipt = _mapping(driving.get("event_receipt"), "driving event receipt")
    _require_equal(
        driving_event_receipt,
        fuel_event_receipt,
        "fuel/driving event receipt",
    )

    driving_context = _mapping(driving.get("driving_context"), "driving context")
    condition_context = _mapping(condition.get("track_context"), "condition track context")
    _require_equal(
        condition_context,
        driving_context,
        "driving/condition track context",
    )
    track_length_mm = _track_length_mm(driving_context, "shared")
    _require_equal(
        driving_context.get("source_binding_sha256"),
        canonical_sha256(fuel_evidence),
        "track context source binding",
    )
    shadow_context = _mapping(shadow.get("context"), "shadow context")
    shadow_track_length = shadow_context.get("track_length_m")
    if isinstance(shadow_track_length, bool) or not isinstance(shadow_track_length, (int, float)):
        raise OfflineEngineerDemoError("shadow track_length_m must be numeric")
    shadow_track_mm = float(shadow_track_length) * 1_000.0
    if not math.isfinite(shadow_track_mm) or abs(shadow_track_mm - round(shadow_track_mm)) > 1e-6:
        raise OfflineEngineerDemoError("shadow track_length_m must resolve to integer millimetres")
    _require_equal(round(shadow_track_mm), track_length_mm, "shadow/shared track length")
    _require_equal(
        condition.get("target_lap_ordinal"),
        target_lap,
        "condition target lap",
    )

    baseline_binding = _candidate_binding(
        labels,
        driving,
        source_id=bound_source_id,
        session_id=bound_session_id,
        target_lap_ordinal=target_lap,
        input_evidence=fuel_evidence,
        normalized_receipt=fuel_normalized,
        track_context=driving_context,
    )

    shadow_recommendations = _recommendations(shadow.get("recommendations"), "shadow")
    fuel_recommendations = _recommendations(fuel.get("recommendations"), "fuel")
    driving_recommendations = _recommendations(driving.get("recommendations"), "driving")
    condition_recommendations = _recommendations(condition.get("recommendations"), "condition")
    if condition_recommendations:
        raise OfflineEngineerDemoError("condition cohort must not emit driving recommendations")

    shadow_capabilities = _mapping(shadow.get("capabilities"), "shadow capabilities")
    fuel_capabilities = _mapping(fuel.get("capabilities"), "fuel capabilities")
    driving_capabilities = _mapping(driving.get("capabilities"), "driving capabilities")
    condition_capabilities = _mapping(condition.get("capabilities"), "condition capabilities")
    shadow_fuel_gate = _copy_gate(shadow_capabilities.get("fuel_model_smoke"), "shadow fuel gate")
    shadow_driving_gate = _copy_gate(
        shadow_capabilities.get("driving_analysis_smoke"), "shadow driving gate"
    )
    fuel_gate = _copy_gate(fuel_capabilities.get("fuel_model_shadow"), "shared fuel gate")
    driving_gate = _copy_gate(
        driving_capabilities.get("driving_model_shadow"), "shared driving gate"
    )
    component_race_gates = {
        "fuel": _copy_gate(
            fuel_capabilities.get("race_recommendation"),
            "fuel race recommendation gate",
        ),
        "shadow": _copy_gate(
            shadow_capabilities.get("race_recommendation"),
            "shadow race recommendation gate",
        ),
        "driving": _copy_gate(
            driving_capabilities.get("race_coaching"),
            "driving race coaching gate",
        ),
    }
    if any(gate["status"] != "BLOCKED" for gate in component_race_gates.values()):
        raise OfflineEngineerDemoError("component race recommendation gates must remain BLOCKED")
    unavailable_capabilities = {
        "shadow": {
            name: _copy_unavailable_capability(
                shadow_capabilities.get(name), f"shadow {name} capability"
            )
            for name in ("current_tire_wear", "opponent_fuel", "traffic_model")
        },
        "fuel": {
            name: _copy_unavailable_capability(
                fuel_capabilities.get(name), f"fuel {name} capability"
            )
            for name in ("current_tire_wear", "opponent_fuel", "traffic_model")
        },
        "driving": {
            name: _copy_unavailable_capability(
                driving_capabilities.get(name), f"driving {name} capability"
            )
            for name in (
                "curb_guidance",
                "current_tire_wear",
                "personalized_coaching",
                "traffic_model",
            )
        },
        "condition": {
            name: _copy_unavailable_capability(
                condition_capabilities.get(name), f"condition {name} capability"
            )
            for name in ("current_tire_wear", "personalized_coaching", "traffic_model")
        },
    }
    condition_non_estimate_gates = {
        name: _copy_non_estimate_gate(condition_capabilities.get(name), f"condition {name}")
        for name in ("observed_proximity_gate", "tire_usage_context_gate")
    }
    condition_quality_gate = _copy_gate(condition.get("quality_gate"), "condition quality gate")
    condition_status = _string(condition.get("readiness_status"), "condition readiness_status")
    trusted_condition_status = _string(
        condition.get("trusted_readiness_status"),
        "condition trusted_readiness_status",
    )
    offline_pass = all(
        gate["status"] == "PASS"
        for gate in (shadow_fuel_gate, shadow_driving_gate, fuel_gate, driving_gate)
    )
    offline_reasons = [] if offline_pass else ["FUEL_OR_DRIVING_SHADOW_GATE_NOT_READY"]

    shadow_receipt = _mapping(shadow.get("receipt"), "shadow receipt")
    shadow_analysis_sha256 = _sha256(
        shadow_receipt.get("analysis_sha256"), "shadow analysis_sha256"
    )
    component_hashes = {
        "condition_cohort_sha256": _sha256(
            condition.get("condition_cohort_sha256"),
            "condition_cohort_sha256",
        ),
        "condition_config_sha256": _sha256(
            condition.get("condition_config_sha256"),
            "condition_config_sha256",
        ),
        "condition_provenance_sha256": _sha256(
            condition.get("condition_provenance_sha256"),
            "condition_provenance_sha256",
        ),
        "condition_semantic_sha256": _sha256(
            condition.get("condition_semantic_sha256"),
            "condition_semantic_sha256",
        ),
        "driving_model_output_sha256": _sha256(
            driving.get("model_output_sha256"), "driving model_output_sha256"
        ),
        "driving_model_semantic_sha256": _sha256(
            driving.get("model_semantic_sha256"),
            "driving model_semantic_sha256",
        ),
        "driving_replay_sha256": _sha256(
            driving.get("driving_replay_sha256"), "driving_replay_sha256"
        ),
        "fuel_model_output_sha256": _sha256(
            fuel.get("model_output_sha256"), "fuel model_output_sha256"
        ),
        "fuel_model_semantic_sha256": _sha256(
            fuel.get("model_semantic_sha256"), "fuel model_semantic_sha256"
        ),
        "fuel_replay_sha256": _sha256(fuel.get("fuel_replay_sha256"), "fuel_replay_sha256"),
        "label_artifact_sha256": _sha256(labels.get("artifact_sha256"), "label artifact_sha256"),
        "label_candidate_payload_sha256": _sha256(
            labels.get("candidate_payload_sha256"),
            "label candidate_payload_sha256",
        ),
        "shadow_analysis_sha256": shadow_analysis_sha256,
    }

    shadow_suppressions = _list(shadow.get("suppressions"), "shadow suppressions")
    if any(type(item) is not dict for item in shadow_suppressions):
        raise OfflineEngineerDemoError("shadow suppressions must contain plain objects")

    binding: dict[str, object] = {
        "advisor_only": True,
        "component_hashes": component_hashes,
        "contract_version": OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION,
        "execution_mode": "SHADOW",
        "execution_status": "COMPLETE",
        "gates": {
            "condition_data": {
                "reasons": copy.deepcopy(condition_quality_gate["reasons"]),
                "status": condition_status,
            },
            "condition_trust": {
                "reasons": copy.deepcopy(condition_quality_gate["reasons"]),
                "status": trusted_condition_status,
            },
            "driving_shadow": driving_gate,
            "label_trust": {
                "reasons": ["LABEL_SET_NOT_APPROVED"],
                "status": "WAIT_HUMAN_LABELS",
            },
            "offline_demo": {
                "reasons": offline_reasons,
                "status": "PASS" if offline_pass else "DEGRADED",
            },
            "personalized_coaching": {
                "reasons": [
                    "CONDITION_COHORT_NOT_TRUSTED",
                    "HUMAN_CORNER_LABELS_NOT_TRUSTED",
                ],
                "status": "BLOCKED",
            },
            "race_recommendation": {
                "reasons": ["ADVISOR_ONLY", "OFFLINE_SHADOW_MODE"],
                "status": "BLOCKED",
            },
            "shared_fuel_shadow": fuel_gate,
            "shadow_driving": shadow_driving_gate,
            "shadow_fuel": shadow_fuel_gate,
        },
        "input_binding": {
            "baseline_binding": baseline_binding,
            "event_receipt": copy.deepcopy(fuel_event_receipt),
            "input_evidence": copy.deepcopy(fuel_evidence),
            "input_kind": "ibt",
            "normalized_input_receipt": copy.deepcopy(fuel_normalized),
            "scenario": copy.deepcopy(scenario),
            "scenario_sha256": scenario_sha256,
            "track_context": copy.deepcopy(driving_context),
        },
        "recommendations": {
            "shared_driving": driving_recommendations,
            "shared_fuel": fuel_recommendations,
            "shadow": shadow_recommendations,
        },
        "unavailable_capabilities": {
            **unavailable_capabilities,
            "condition_non_estimate_gates": condition_non_estimate_gates,
            "component_race_gates": component_race_gates,
        },
        "receipts": {
            "condition": {
                key: component_hashes[key]
                for key in (
                    "condition_cohort_sha256",
                    "condition_config_sha256",
                    "condition_provenance_sha256",
                    "condition_semantic_sha256",
                )
            },
            "driving": {
                "contract_version": driving["contract_version"],
                "driving_replay_sha256": component_hashes["driving_replay_sha256"],
                "model_output_sha256": component_hashes["driving_model_output_sha256"],
                "model_semantic_sha256": component_hashes["driving_model_semantic_sha256"],
            },
            "fuel": {
                "contract_version": fuel["contract_version"],
                "fuel_replay_sha256": component_hashes["fuel_replay_sha256"],
                "model_output_sha256": component_hashes["fuel_model_output_sha256"],
                "model_semantic_sha256": component_hashes["fuel_model_semantic_sha256"],
                "scenario_sha256": scenario_sha256,
            },
            "labels": {
                "artifact_sha256": component_hashes["label_artifact_sha256"],
                "candidate_payload_sha256": component_hashes["label_candidate_payload_sha256"],
                "contract_version": labels["contract_version"],
                "review_authenticity_status": None,
                "review_status": PENDING_HUMAN_REVIEW,
                "trusted_status": "WAIT_HUMAN_LABELS",
            },
            "shadow": copy.deepcopy(shadow_receipt),
        },
        "suppressions": {
            "condition": {
                "reasons": copy.deepcopy(condition_quality_gate["reasons"]),
                "status": trusted_condition_status,
            },
            "labels": {
                "reasons": ["LABEL_SET_NOT_APPROVED"],
                "status": "WAIT_HUMAN_LABELS",
            },
            "shadow": copy.deepcopy(shadow_suppressions),
        },
    }
    return {**binding, "demo_sha256": canonical_sha256(binding)}


__all__ = [
    "OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION",
    "OfflineEngineerDemoError",
    "build_offline_engineer_demo",
    "canonical_sha256",
]
