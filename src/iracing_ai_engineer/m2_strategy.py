"""Build a package-external, fail-closed M2 strategy receipt.

This contract deliberately does not modify the frozen live/r7 path.  It binds
an already admitted ``fuel-model-replay-v2`` receipt, an
``offline-m1-pit-stint-v1`` receipt, and a separately admitted decision
context.  Official-rule status is derived only from an exact event selector
and an independently supplied source-document digest; no input boolean can
promote a rules profile.

The real Audi/Spa fixture is expected to remain a WAIT receipt with no
recommendation.  Synthetic fixtures may exercise the complete shadow
lifecycle, but every candidate remains advisor-only and non-executable.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn

from .rejoin_projection import (
    REJOIN_CONTRACT_VERSION,
    REJOIN_METHOD_VERSION,
    project_physical_rejoin,
)

CONTRACT_VERSION = "offline-m2-strategy-receipt-v1"
CONTRACT_V2_VERSION = "offline-m2-strategy-receipt-v2"
CONTEXT_CONTRACT_VERSION = "offline-m2-strategy-context-v1"
CONTEXT_V2_CONTRACT_VERSION = "offline-m2-strategy-context-v2"
RULES_PROFILE_CONTRACT_VERSION = "event-rules-profile-v2"
FUEL_REPLAY_CONTRACT_VERSION = "fuel-model-replay-v2"
M1_CONTRACT_VERSION = "offline-m1-pit-stint-v1"
MATCHED_PIT_CALIBRATION_METHOD_VERSION = "matched-pit-service-median-v1"
TIRE_PERFORMANCE_MODEL_CONTRACT_VERSION = "tire-performance-model-v1"
TIRE_PERFORMANCE_METHOD_VERSION = "fuel-adjusted-disjoint-pair-envelope-v1"
TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION = "tire-performance-belief-v1"
TIRE_PERFORMANCE_BELIEF_METHOD_VERSION = "linear-age-service-tradeoff-v1"
TIRE_STINT_CONTEXT_CONTRACT_VERSION = "tire-stint-context-v1"
MAX_INPUT_BYTES = 32 * 1024 * 1024

_SHA256_CHARS = frozenset("0123456789abcdef")
_M1_KEYS = frozenset(
    {
        "advisor_only",
        "attestation_status",
        "capabilities",
        "contract_version",
        "derivation_status",
        "execution_mode",
        "incomplete_interval_counts",
        "input_binding",
        "input_evidence",
        "normalized_input_receipt",
        "pit_cycles",
        "pit_stint_receipt_sha256",
        "quality_gate",
        "recommendations",
        "service_contents",
        "status",
        "stints",
        "summary",
        "upstream_event_receipt",
    }
)
_SOURCE_BINDING_KEYS = frozenset(
    {
        "event_receipt_sha256",
        "normalized_samples_sha256",
        "sample_count",
        "session_id",
        "source_id",
        "source_kind",
        "source_sha256",
    }
)
_IDENTITY_KEYS = frozenset(
    {
        "car_class_id",
        "event_type",
        "official",
        "provenance",
        "race_week",
        "season_id",
        "series_id",
        "sim_build",
        "track_config",
        "track_id",
    }
)
_SELECTOR_KEYS = frozenset(_IDENTITY_KEYS - {"official", "provenance"})
_OBSERVATION_KEYS = frozenset(
    {
        "decision_tick",
        "laps_completed",
        "penalty_state",
        "pits_open",
        "reset",
        "schema_changed",
        "session_epoch",
        "source_epoch",
        "stale",
    }
)
_HORIZON_INPUT_KEYS = frozenset(
    {
        "kind",
        "laps_remaining",
        "leader_eta_to_next_crossing_s",
        "player_is_leader",
        "provenance",
        "reference_lap_time_s",
        "time_remaining_s",
    }
)
_VEHICLE_KEYS = frozenset({"provenance", "tank_capacity_l"})
_POLICY_KEYS = frozenset({"conservative_quantile", "reserve_l", "selection_policy"})
_CALIBRATION_KEYS = frozenset(
    {
        "identity_sha256",
        "method_version",
        "model_sha256",
        "pit_lane_loss_s",
        "pit_lane_loss_uncertainty_s",
        "refuel_rate_l_per_s",
        "sample_count",
        "service_labels_available",
        "source_receipt_sha256",
        "status",
        "tire_change_time_s",
    }
)
_TRAFFIC_MOTION_KEYS = frozenset(
    {
        "availability",
        "contract_version",
        "decision_tick",
        "identity_sha256",
        "motion_sha256",
        "observation_window_s",
        "opponents",
        "player",
        "reason_codes",
        "source_receipt_sha256",
        "status",
        "traffic_map_revision_sha256",
    }
)
_TRAFFIC_MOTION_ACTOR_KEYS = frozenset(
    {
        "car_idx",
        "point_count",
        "rate_laps_per_s",
        "rate_range_laps_per_s",
    }
)
_TRAFFIC_MOTION_OPPONENT_KEYS = frozenset(
    set(_TRAFFIC_MOTION_ACTOR_KEYS) | {"current_signed_lap_delta"}
)
_TRAFFIC_KEYS = frozenset(
    {
        "estimate_available",
        "identity_sha256",
        "map_revision_sha256",
        "motion_context",
        "motion_context_sha256",
        "observed_at_decision_tick",
        "rejoin_gap_range_s",
        "source_receipt_sha256",
        "status",
        "traffic_sha256",
    }
)
_CONTEXT_KEYS = frozenset(
    {
        "calibration_model",
        "context_sha256",
        "contract_version",
        "event_identity",
        "horizon",
        "observation",
        "source_binding",
        "strategy_policy",
        "traffic_rejoin",
        "vehicle_context",
    }
)
_CONTEXT_V2_KEYS = frozenset(
    set(_CONTEXT_KEYS) | {"tire_performance_model", "tire_stint_context"}
)
_TIRE_PERFORMANCE_MODEL_KEYS = frozenset(
    {
        "advisor_only",
        "contract_version",
        "estimate_available",
        "fuel_load_model_sha256",
        "identity_sha256",
        "independent_stint_count",
        "max_supported_stint_age_laps",
        "method_version",
        "model_sha256",
        "pair_count",
        "performance_age_slope_s_per_lap",
        "performance_age_slope_uncertainty_s_per_lap",
        "physical_wear",
        "source_receipt_sha256",
        "status",
        "tire_compound",
    }
)
_TIRE_STINT_CONTEXT_KEYS = frozenset(
    {
        "availability",
        "context_sha256",
        "contract_version",
        "current_laps_completed",
        "current_tire_compound",
        "decision_tick",
        "identity_sha256",
        "on_pit_road",
        "origin_kind",
        "origin_laps_completed",
        "origin_tick",
        "physical_wear",
        "reason_codes",
        "source_receipt_sha256",
        "status",
        "stint_age_completed_laps",
        "tire_sets_used",
    }
)
_TIRE_PHYSICAL_WEAR_KEYS = frozenset(
    {
        "estimate_available",
        "measured_current_set",
        "provenance",
        "reason_codes",
        "status",
    }
)
_TIRE_PHYSICAL_WEAR_UNAVAILABLE = {
    "estimate_available": False,
    "measured_current_set": False,
    "provenance": "UNKNOWN",
    "reason_codes": [
        "NO_CURRENT_SET_DIRECT_WEAR_MEASUREMENT",
        "PERFORMANCE_SLOPE_IS_NOT_PHYSICAL_WEAR",
    ],
    "status": "SKIP_CURRENT_PHYSICAL_WEAR",
}
_TIRE_PERFORMANCE_BELIEF_KEYS = frozenset(
    {
        "advisor_only",
        "belief_sha256",
        "calibration_model_sha256",
        "contract_version",
        "estimate_available",
        "identity_sha256",
        "method_version",
        "model_sha256",
        "performance_preference",
        "physical_wear",
        "reason_codes",
        "scenario",
        "source_receipt_sha256",
        "status",
    }
)
_TIRE_PERFORMANCE_SCENARIO_KEYS = frozenset(
    {
        "age_at_pit_laps",
        "current_stint_age_laps",
        "current_stint_context_sha256",
        "current_tire_compound",
        "fuel_add_l",
        "fuel_tire_service_timing",
        "incremental_tire_service_s",
        "keep_tires_time_loss_range_s",
        "laps_after_pit",
        "laps_until_pit",
        "max_projected_tire_age_laps",
    }
)
_TIRE_STRATEGY_KEYS = frozenset(
    {
        "belief",
        "change_tires",
        "reason_codes",
        "status",
    }
)
_RULES_PROFILE_KEYS = frozenset(
    {
        "contract_version",
        "official_rules",
        "profile_id",
        "profile_sha256",
        "profile_version",
        "selector",
        "source",
    }
)
_RULES_SOURCE_KEYS = frozenset({"authority", "document_id", "document_sha256"})
_OFFICIAL_RULE_KEYS = frozenset(
    {
        "finish_rule",
        "fuel_tire_service_timing",
        "minimum_pit_stops",
        "no_tire_service_allowed",
        "tire_change_required",
    }
)
_OUTPUT_KEYS = frozenset(
    {
        "advisor_only",
        "attestation_status",
        "calibration",
        "capabilities",
        "contract_version",
        "derivation_status",
        "event_identity",
        "execution_mode",
        "horizon",
        "input_binding",
        "lifecycle",
        "m2_strategy_receipt_sha256",
        "quality_gate",
        "recommendations",
        "rules_binding",
        "strategy_context",
        "strategy_policy",
        "traffic_rejoin",
        "vehicle_context",
    }
)
_OUTPUT_V2_KEYS = frozenset(set(_OUTPUT_KEYS) | {"tire_strategy"})
_INPUT_BINDING_KEYS = frozenset(
    {
        "context_sha256",
        "event_receipt_sha256",
        "fuel_replay_sha256",
        "input_lineage_sha256",
        "m1_receipt_sha256",
        "model_semantic_sha256",
        "normalized_samples_sha256",
        "sample_count",
        "session_id",
        "source_id",
        "source_kind",
        "source_sha256",
    }
)
_LIFECYCLE_KEYS = frozenset(
    {
        "active_recommendation_id",
        "events",
        "observation_point",
        "previous_state_sha256",
        "state_revision",
    }
)
_OBSERVATION_POINT_KEYS = frozenset({"decision_tick", "session_epoch", "source_epoch"})
_RECOMMENDATION_KEYS = frozenset(
    {
        "action",
        "claim_scope",
        "confidence",
        "evidence_ids",
        "executable",
        "expected_gain_range_s",
        "kind",
        "practice_only",
        "reason",
        "recommendation_basis",
        "recommendation_id",
        "risk",
        "status",
        "supersedes_id",
        "valid_until",
    }
)
_RECOMMENDATION_BASIS_KEYS = frozenset(
    {
        "action",
        "calibration_model_sha256",
        "event_identity_sha256",
        "fuel_model_semantic_sha256",
        "horizon_sha256",
        "rejoin_estimate_semantic_sha256",
        "rules_lineage_sha256",
        "session_id",
        "source_id",
        "strategy_policy_sha256",
        "traffic_semantic_sha256",
    }
)
_RECOMMENDATION_BASIS_V2_KEYS = frozenset(
    set(_RECOMMENDATION_BASIS_KEYS) | {"tire_strategy_semantic_sha256"}
)


class M2StrategyReceiptError(ValueError):
    """Raised when an input cannot satisfy the M2 receipt contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise M2StrategyReceiptError(code, message)


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
        raise M2StrategyReceiptError(
            "CANONICAL_JSON_FAILED", "value is not canonical-JSON-safe"
        ) from exc
    return encoded + (b"\n" if newline else b"")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("SCHEMA_INVALID", f"{name} must be an object")
    return value


def _exact_mapping(value: object, keys: frozenset[str], name: str) -> dict[str, Any]:
    result = _mapping(value, name)
    if set(result) != keys:
        _fail("SCHEMA_INVALID", f"{name} keys are invalid")
    return result


def _list(value: object, name: str) -> list[Any]:
    if type(value) is not list:
        _fail("SCHEMA_INVALID", f"{name} must be an array")
    return value


def _identifier(value: object, name: str, *, maximum_bytes: int = 256) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum_bytes
        or any(ord(character) < 32 for character in value)
    ):
        _fail("SCHEMA_INVALID", f"{name} is not a valid identifier")
    return value


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _SHA256_CHARS for character in value)
    ):
        _fail("DIGEST_INVALID", f"{name} must be a lowercase SHA-256 digest")
    return value


def _plain_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail("SCHEMA_INVALID", f"{name} must be a plain integer >= {minimum}")
    return value


def _optional_plain_int(value: object, name: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return _plain_int(value, name, minimum=minimum)


def _finite_number(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("SCHEMA_INVALID", f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        _fail("SCHEMA_INVALID", f"{name} must be finite")
    if positive and number <= 0:
        _fail("SCHEMA_INVALID", f"{name} must be positive")
    if minimum is not None and number < minimum:
        _fail("SCHEMA_INVALID", f"{name} must be >= {minimum}")
    return number


def _optional_number(
    value: object,
    name: str,
    *,
    minimum: float = 0.0,
    positive: bool = False,
) -> float | None:
    if value is None:
        return None
    return _finite_number(value, name, minimum=minimum, positive=positive)


def _close(left: float, right: float, name: str, *, tolerance: float = 1e-6) -> None:
    if not math.isclose(left, right, rel_tol=0.0, abs_tol=tolerance):
        _fail("LINEAGE_MISMATCH", f"{name} does not close")


def _load_pit_plan_dependency() -> Any:
    module_name = f"{__package__}.pit_plan"
    if importlib.util.find_spec(module_name) is None:
        raise RuntimeError("cannot load offline pit-plan validator")

    from . import pit_plan

    return pit_plan


_PIT_PLAN = _load_pit_plan_dependency()


def _validate_fuel_replay(
    value: object, *, expected_fuel_replay_sha256: str
) -> dict[str, Any]:
    expected = _sha256(expected_fuel_replay_sha256, "expected fuel replay SHA-256")
    try:
        replay = _PIT_PLAN._validate_fuel_replay(  # noqa: SLF001
            value,
            expected_fuel_replay_sha256=expected,
        )
    except Exception as exc:
        _fail("FUEL_REPLAY_INVALID", str(exc))
    if replay.get("contract_version") != FUEL_REPLAY_CONTRACT_VERSION:
        _fail("FUEL_REPLAY_INVALID", "fuel replay contract is unsupported")
    return replay


def _validate_unknown_service_contents(value: object, name: str) -> None:
    contents = _mapping(value, name)
    if set(contents) != {"delivered_fuel", "driver_swap", "repairs", "tire_service"}:
        _fail("M1_RECEIPT_INVALID", f"{name} keys are invalid")
    for key, raw in contents.items():
        item = _mapping(raw, f"{name}.{key}")
        if (
            item.get("availability") != "UNAVAILABLE"
            or item.get("estimate_available") is not False
            or item.get("provenance") != "UNKNOWN"
            or item.get("status") != "SKIP_NOT_OBSERVABLE"
        ):
            _fail("M1_RECEIPT_INVALID", f"{name}.{key} promoted unavailable evidence")


def _validate_m1_receipt(
    value: object, *, expected_m1_receipt_sha256: str
) -> dict[str, Any]:
    receipt = _exact_mapping(value, _M1_KEYS, "M1 receipt")
    expected = _sha256(expected_m1_receipt_sha256, "expected M1 receipt SHA-256")
    stored = _sha256(receipt.get("pit_stint_receipt_sha256"), "M1 self SHA-256")
    if stored != expected:
        _fail("M1_RECEIPT_INVALID", "M1 receipt does not match independent digest")
    binding = {
        key: item for key, item in receipt.items() if key != "pit_stint_receipt_sha256"
    }
    if canonical_sha256(binding) != stored:
        _fail("M1_RECEIPT_INVALID", "M1 receipt self hash mismatch")
    if (
        receipt.get("contract_version") != M1_CONTRACT_VERSION
        or receipt.get("advisor_only") is not True
        or receipt.get("attestation_status") != "NOT_R7_ATTESTED"
        or receipt.get("derivation_status") != "POST_ADMISSION_PACKAGE_EXTERNAL"
        or receipt.get("execution_mode") != "SHADOW_ONLY"
        or receipt.get("status") != "CANDIDATE_NOT_GOLDEN"
        or receipt.get("recommendations") != []
    ):
        _fail("M1_RECEIPT_INVALID", "M1 safety boundary is invalid")
    quality = _mapping(receipt.get("quality_gate"), "M1 quality gate")
    if quality != {"reasons": [], "status": "PASS"}:
        _fail("M1_RECEIPT_INVALID", "M1 quality gate is not PASS")
    _validate_unknown_service_contents(
        receipt.get("service_contents"), "M1 service contents"
    )
    return receipt


def _source_binding_from_inputs(
    fuel: Mapping[str, object], m1: Mapping[str, object]
) -> dict[str, object]:
    input_kind = fuel.get("input_kind")
    if input_kind not in {"ibt", "collector"}:
        _fail("CROSS_RECEIPT_LINEAGE_MISMATCH", "fuel input kind is invalid")
    fuel_evidence = _mapping(fuel.get("input_evidence"), "fuel input evidence")
    fuel_normalized = _mapping(
        fuel.get("normalized_input_receipt"), "fuel normalized receipt"
    )
    fuel_event = _mapping(fuel.get("event_receipt"), "fuel event receipt")
    m1_evidence = _mapping(m1.get("input_evidence"), "M1 input evidence")
    m1_normalized = _mapping(
        m1.get("normalized_input_receipt"), "M1 normalized receipt"
    )
    m1_event = _mapping(m1.get("upstream_event_receipt"), "M1 event receipt")
    source_kind = fuel_evidence.get("source_kind")
    if input_kind == "ibt":
        if source_kind != "IBT_OFFLINE":
            _fail(
                "CROSS_RECEIPT_LINEAGE_MISMATCH",
                "IBT input kind requires IBT_OFFLINE evidence",
            )
        source_digest_key = "source_sha256"
    else:
        if source_kind not in {"SDK_LIVE", "REPLAY_SDK_PROXY"}:
            _fail(
                "CROSS_RECEIPT_LINEAGE_MISMATCH",
                "collector input kind requires collector source evidence",
            )
        source_digest_key = "records_sha256"
    comparisons = {
        "source_id": (fuel_evidence.get("source_id"), m1_evidence.get("source_id")),
        "session_id": (fuel_evidence.get("session_id"), m1_evidence.get("session_id")),
        "source_kind": (
            fuel_evidence.get("source_kind"),
            m1_evidence.get("source_kind"),
        ),
        "source_sha256": (
            fuel_evidence.get(source_digest_key),
            m1_evidence.get(source_digest_key),
        ),
        "normalized_samples_sha256": (
            fuel_normalized.get("samples_sha256"),
            m1_normalized.get("samples_sha256"),
        ),
        "event_receipt_sha256": (
            fuel_event.get("receipt_sha256"),
            m1_event.get("receipt_sha256"),
        ),
    }
    for name, (left, right) in comparisons.items():
        if left != right:
            _fail("CROSS_RECEIPT_LINEAGE_MISMATCH", f"fuel/M1 {name} mismatch")
    if fuel_evidence != m1_evidence:
        _fail(
            "CROSS_RECEIPT_LINEAGE_MISMATCH",
            "fuel/M1 input evidence objects differ",
        )
    if fuel_normalized.get("sample_count") != m1_normalized.get("sample_count"):
        _fail("CROSS_RECEIPT_LINEAGE_MISMATCH", "fuel/M1 sample count mismatch")
    result = {name: pair[0] for name, pair in comparisons.items()}
    result["sample_count"] = fuel_normalized.get("sample_count")
    for key in ("source_id", "session_id", "source_kind"):
        _identifier(result[key], f"source binding {key}")
    for key in ("source_sha256", "normalized_samples_sha256", "event_receipt_sha256"):
        _sha256(result[key], f"source binding {key}")
    _plain_int(result["sample_count"], "source binding sample_count", minimum=1)
    return result


def _validate_identity(value: object) -> tuple[dict[str, Any], str]:
    identity = _exact_mapping(value, _IDENTITY_KEYS, "event identity")
    official = identity.get("official")
    if official is not None and type(official) is not bool:
        _fail("CONTEXT_INVALID", "event identity official must be boolean or null")
    for key in ("series_id", "season_id", "race_week", "track_id", "car_class_id"):
        _optional_plain_int(identity.get(key), f"event identity {key}")
    for key in ("event_type", "track_config", "sim_build"):
        if identity.get(key) is not None:
            _identifier(identity.get(key), f"event identity {key}")
    if identity.get("provenance") not in {
        "SDK_DIRECT_SAME_SOURCE_CAPTURE",
        "SDK_DIRECT_SAME_SOURCE_SESSION_INFO",
        "CONTRACT_FIXTURE",
    }:
        _fail("CONTEXT_INVALID", "event identity provenance is unsupported")
    return identity, canonical_sha256(identity)


def _validate_calibration_model(
    value: object | None, *, identity_sha256: str
) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return None, None
    model = _exact_mapping(value, _CALIBRATION_KEYS, "calibration model")
    stored = _sha256(model.get("model_sha256"), "calibration model SHA-256")
    material = {key: item for key, item in model.items() if key != "model_sha256"}
    if canonical_sha256(material) != stored:
        _fail("CONTEXT_INVALID", "calibration model self hash mismatch")
    if (
        model.get("status") != "CALIBRATED_MATCHED_BASELINE"
        or model.get("identity_sha256") != identity_sha256
    ):
        _fail("CONTEXT_INVALID", "calibration model identity/status is invalid")
    _identifier(model.get("method_version"), "calibration method version")
    _sha256(model.get("source_receipt_sha256"), "calibration source receipt")
    sample_count = _plain_int(model.get("sample_count"), "calibration sample count")
    if sample_count < 3:
        _fail("CONTEXT_INVALID", "calibration needs at least three matched samples")
    loss = _finite_number(
        model.get("pit_lane_loss_s"), "calibrated pit-lane loss", minimum=0.0
    )
    uncertainty = _list(
        model.get("pit_lane_loss_uncertainty_s"), "pit-lane loss uncertainty"
    )
    if len(uncertainty) != 2:
        _fail("CONTEXT_INVALID", "pit-lane loss uncertainty must have two values")
    low = _finite_number(uncertainty[0], "pit-lane loss uncertainty low", minimum=0.0)
    high = _finite_number(uncertainty[1], "pit-lane loss uncertainty high", minimum=0.0)
    if not low <= loss <= high:
        _fail("CONTEXT_INVALID", "pit-lane loss is outside its uncertainty range")
    _finite_number(
        model.get("refuel_rate_l_per_s"), "calibrated refuel rate", positive=True
    )
    _finite_number(model.get("tire_change_time_s"), "calibrated tire time", minimum=0.0)
    if type(model.get("service_labels_available")) is not bool:
        _fail("CONTEXT_INVALID", "service_labels_available must be boolean")
    return model, stored


def _validate_tire_physical_wear_boundary(value: object, name: str) -> None:
    physical = _exact_mapping(value, _TIRE_PHYSICAL_WEAR_KEYS, name)
    if physical != _TIRE_PHYSICAL_WEAR_UNAVAILABLE:
        _fail("CONTEXT_INVALID", f"{name} made a current physical-wear claim")


def _validate_tire_performance_model(
    value: object | None,
    *,
    identity_sha256: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return None, None
    model = _exact_mapping(
        value,
        _TIRE_PERFORMANCE_MODEL_KEYS,
        "tire-performance model",
    )
    if (
        model.get("contract_version") != TIRE_PERFORMANCE_MODEL_CONTRACT_VERSION
        or model.get("method_version") != TIRE_PERFORMANCE_METHOD_VERSION
        or model.get("advisor_only") is not True
    ):
        _fail("CONTEXT_INVALID", "tire-performance model contract is unsupported")
    stored = _sha256(model.get("model_sha256"), "tire-performance model SHA-256")
    material = {key: item for key, item in model.items() if key != "model_sha256"}
    if canonical_sha256(material) != stored:
        _fail("CONTEXT_INVALID", "tire-performance model self hash mismatch")
    if model.get("identity_sha256") != identity_sha256:
        _fail("CONTEXT_INVALID", "tire-performance model identity mismatch")
    _sha256(model.get("source_receipt_sha256"), "tire-performance source receipt")
    _sha256(model.get("fuel_load_model_sha256"), "tire fuel-load model SHA-256")
    pair_count = _plain_int(
        model.get("pair_count"), "tire-performance pair count", minimum=3
    )
    stint_count = _plain_int(
        model.get("independent_stint_count"),
        "tire-performance independent stint count",
        minimum=3,
    )
    if pair_count != stint_count:
        _fail("CONTEXT_INVALID", "tire-performance pairs are not disjoint by stint")
    _plain_int(
        model.get("max_supported_stint_age_laps"),
        "maximum supported tire age",
        minimum=2,
    )
    _plain_int(model.get("tire_compound"), "tire-performance compound")
    central = _finite_number(
        model.get("performance_age_slope_s_per_lap"),
        "tire-performance age slope",
    )
    bounds = _list(
        model.get("performance_age_slope_uncertainty_s_per_lap"),
        "tire-performance age-slope uncertainty",
    )
    if len(bounds) != 2:
        _fail("CONTEXT_INVALID", "tire-performance uncertainty needs two bounds")
    low = _finite_number(bounds[0], "tire-performance slope low")
    high = _finite_number(bounds[1], "tire-performance slope high")
    if not low <= central <= high:
        _fail("CONTEXT_INVALID", "tire-performance slope is outside its interval")
    expected_status = (
        "PASS_SHADOW_POSITIVE_DEGRADATION"
        if low > 0.0
        else "WAIT_POSITIVE_DEGRADATION_NOT_OBSERVED"
        if high <= 0.0
        else "WAIT_DEGRADATION_SIGN_AMBIGUOUS"
    )
    if (
        model.get("status") != expected_status
        or model.get("estimate_available")
        is not (expected_status == "PASS_SHADOW_POSITIVE_DEGRADATION")
    ):
        _fail(
            "CONTEXT_INVALID",
            "tire-performance availability is not slope-derived",
        )
    _validate_tire_physical_wear_boundary(
        model.get("physical_wear"),
        "tire-performance physical-wear boundary",
    )
    return model, stored


def _validate_tire_stint_context(
    value: object,
    *,
    identity_sha256: str,
    decision_tick: int,
    expected_source_receipt_sha256: str | None,
) -> tuple[dict[str, Any], str]:
    context = _exact_mapping(
        value,
        _TIRE_STINT_CONTEXT_KEYS,
        "tire-stint context",
    )
    if context.get("contract_version") != TIRE_STINT_CONTEXT_CONTRACT_VERSION:
        _fail("CONTEXT_INVALID", "tire-stint context contract is unsupported")
    stored = _sha256(context.get("context_sha256"), "tire-stint context SHA-256")
    material = {key: item for key, item in context.items() if key != "context_sha256"}
    if canonical_sha256(material) != stored:
        _fail("CONTEXT_INVALID", "tire-stint context self hash mismatch")
    if context.get("identity_sha256") != identity_sha256:
        _fail("CONTEXT_INVALID", "tire-stint identity mismatch")
    source_sha = _sha256(
        context.get("source_receipt_sha256"),
        "tire-stint source receipt SHA-256",
    )
    if (
        expected_source_receipt_sha256 is not None
        and source_sha != expected_source_receipt_sha256
    ):
        _fail("CONTEXT_INVALID", "tire-stint and traffic source receipts differ")
    if context.get("decision_tick") != decision_tick:
        _fail("CONTEXT_INVALID", "tire-stint observation is stale")
    _validate_tire_physical_wear_boundary(
        context.get("physical_wear"),
        "tire-stint physical-wear boundary",
    )
    reasons = _list(context.get("reason_codes"), "tire-stint reason codes")
    if (
        any(type(item) is not str or not item for item in reasons)
        or reasons != sorted(set(reasons))
    ):
        _fail("CONTEXT_INVALID", "tire-stint reasons are invalid")

    current_keys = (
        "current_laps_completed",
        "current_tire_compound",
        "on_pit_road",
        "tire_sets_used",
    )
    current_values = [context.get(key) for key in current_keys]
    current_available = all(item is not None for item in current_values)
    if any(item is not None for item in current_values) and not current_available:
        _fail("CONTEXT_INVALID", "tire-stint current fields are partial")
    if current_available:
        _plain_int(context.get("current_laps_completed"), "current tire laps")
        _plain_int(context.get("current_tire_compound"), "current tire compound")
        _plain_int(context.get("tire_sets_used"), "current tire-set count")
        if type(context.get("on_pit_road")) is not bool:
            _fail("CONTEXT_INVALID", "current tire pit-road state is invalid")

    origin_keys = ("origin_kind", "origin_laps_completed", "origin_tick")
    origin_values = [context.get(key) for key in origin_keys]
    origin_available = all(item is not None for item in origin_values)
    if any(item is not None for item in origin_values) and not origin_available:
        _fail("CONTEXT_INVALID", "tire-stint origin fields are partial")
    origin_laps = 0
    if origin_available:
        if context.get("origin_kind") not in {
            "OBSERVED_PIT_EXIT",
            "OBSERVED_ZERO_COMPLETED_LAPS",
        }:
            _fail("CONTEXT_INVALID", "tire-stint origin kind is invalid")
        origin_laps = _plain_int(
            context.get("origin_laps_completed"), "tire-stint origin laps"
        )
        if _plain_int(context.get("origin_tick"), "tire-stint origin tick") > decision_tick:
            _fail("CONTEXT_INVALID", "tire-stint origin is in the future")

    availability = context.get("availability")
    if availability == "AVAILABLE":
        if (
            context.get("status") != "AVAILABLE_OBSERVED_STINT_AGE"
            or reasons
            or not current_available
            or not origin_available
            or context.get("on_pit_road") is not False
        ):
            _fail("CONTEXT_INVALID", "available tire-stint context is invalid")
        age = _plain_int(
            context.get("stint_age_completed_laps"),
            "tire-stint completed age",
        )
        if age != int(context["current_laps_completed"]) - origin_laps:
            _fail("CONTEXT_INVALID", "tire-stint age is not origin-derived")
    elif availability == "UNAVAILABLE":
        if (
            context.get("status")
            not in {
                "WAIT_CURRENT_TIRE_CHANNELS",
                "WAIT_PLAYER_ON_PIT_ROAD",
                "WAIT_STINT_ORIGIN",
                "WAIT_TIRE_CHANNEL_CONTINUITY",
            }
            or not reasons
            or context.get("stint_age_completed_laps") is not None
        ):
            _fail("CONTEXT_INVALID", "waiting tire-stint context is invalid")
    elif availability == "INVALID":
        if (
            context.get("status") != "INVALID_TIRE_STINT_SEQUENCE"
            or not reasons
            or context.get("stint_age_completed_laps") is not None
        ):
            _fail("CONTEXT_INVALID", "invalid tire-stint context is invalid")
    else:
        _fail("CONTEXT_INVALID", "tire-stint availability is invalid")
    return context, stored


def _validate_motion_actor(
    value: object,
    *,
    opponent: bool,
    label: str,
) -> dict[str, Any]:
    actor = _exact_mapping(
        value,
        _TRAFFIC_MOTION_OPPONENT_KEYS if opponent else _TRAFFIC_MOTION_ACTOR_KEYS,
        label,
    )
    _plain_int(actor.get("car_idx"), f"{label} car index", minimum=0)
    if _plain_int(actor.get("point_count"), f"{label} point count") < 5:
        _fail("CONTEXT_INVALID", f"{label} has too few motion points")
    rate = _finite_number(actor.get("rate_laps_per_s"), f"{label} rate", positive=True)
    bounds = _list(actor.get("rate_range_laps_per_s"), f"{label} rate range")
    if len(bounds) != 2:
        _fail("CONTEXT_INVALID", f"{label} rate range must have two values")
    low = _finite_number(bounds[0], f"{label} rate low", positive=True)
    high = _finite_number(bounds[1], f"{label} rate high", positive=True)
    if not low <= rate <= high <= 0.2:
        _fail("CONTEXT_INVALID", f"{label} rate range is inconsistent")
    if opponent:
        _finite_number(
            actor.get("current_signed_lap_delta"),
            f"{label} signed lap delta",
        )
    return actor


def _validate_traffic_motion(
    value: object | None,
    *,
    identity_sha256: str,
    decision_tick: int,
    source_receipt_sha256: str,
    map_revision_sha256: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return None, None
    motion = _exact_mapping(value, _TRAFFIC_MOTION_KEYS, "traffic motion context")
    stored = _sha256(motion.get("motion_sha256"), "traffic motion SHA-256")
    material = {key: item for key, item in motion.items() if key != "motion_sha256"}
    if canonical_sha256(material) != stored:
        _fail("CONTEXT_INVALID", "traffic motion self hash mismatch")
    if (
        motion.get("contract_version") != "traffic-motion-context-v1"
        or motion.get("availability") != "AVAILABLE"
        or motion.get("status") != "VERIFIED_TIME_DOMAIN_MOTION"
        or motion.get("reason_codes") != []
        or motion.get("identity_sha256") != identity_sha256
        or motion.get("decision_tick") != decision_tick
        or motion.get("source_receipt_sha256") != source_receipt_sha256
        or motion.get("traffic_map_revision_sha256") != map_revision_sha256
    ):
        _fail("CONTEXT_INVALID", "traffic motion lineage/status is invalid")
    if _finite_number(
        motion.get("observation_window_s"),
        "traffic motion observation window",
        positive=True,
    ) < 2.0:
        _fail("CONTEXT_INVALID", "traffic motion window is too short")
    player = _validate_motion_actor(
        motion.get("player"), opponent=False, label="traffic motion player"
    )
    opponents = _list(motion.get("opponents"), "traffic motion opponents")
    if not opponents:
        _fail("CONTEXT_INVALID", "traffic motion has no opponents")
    seen: list[int] = []
    for index, raw in enumerate(opponents):
        opponent = _validate_motion_actor(
            raw,
            opponent=True,
            label=f"traffic motion opponent {index}",
        )
        car_idx = int(opponent["car_idx"])
        if car_idx == player["car_idx"]:
            _fail("CONTEXT_INVALID", "traffic motion contains the player")
        seen.append(car_idx)
    if seen != sorted(set(seen)):
        _fail("CONTEXT_INVALID", "traffic motion opponents are not unique and sorted")
    return motion, stored


def _validate_traffic(
    value: object | None,
    *,
    identity_sha256: str,
    decision_tick: int,
    calibration_available: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return None, None
    traffic = _exact_mapping(value, _TRAFFIC_KEYS, "traffic/rejoin input")
    stored = _sha256(traffic.get("traffic_sha256"), "traffic SHA-256")
    material = {key: item for key, item in traffic.items() if key != "traffic_sha256"}
    if canonical_sha256(material) != stored:
        _fail("CONTEXT_INVALID", "traffic self hash mismatch")
    if traffic.get("identity_sha256") != identity_sha256:
        _fail("CONTEXT_INVALID", "traffic identity/status is invalid")
    if (
        _plain_int(traffic.get("observed_at_decision_tick"), "traffic observation tick")
        != decision_tick
    ):
        _fail("CONTEXT_INVALID", "traffic observation is stale")
    source_receipt_sha256 = _sha256(
        traffic.get("source_receipt_sha256"), "traffic source receipt"
    )
    map_revision_sha256 = _sha256(
        traffic.get("map_revision_sha256"), "traffic map revision"
    )
    motion_context_sha256 = traffic.get("motion_context_sha256")
    if motion_context_sha256 is not None:
        _sha256(motion_context_sha256, "traffic motion context")
    motion, motion_sha256 = _validate_traffic_motion(
        traffic.get("motion_context"),
        identity_sha256=identity_sha256,
        decision_tick=decision_tick,
        source_receipt_sha256=source_receipt_sha256,
        map_revision_sha256=map_revision_sha256,
    )
    if motion_context_sha256 != motion_sha256:
        _fail("CONTEXT_INVALID", "traffic motion object and digest disagree")
    estimate_available = traffic.get("estimate_available")
    status = traffic.get("status")
    if estimate_available is True and status == "AVAILABLE":
        if motion_context_sha256 is None:
            _fail("CONTEXT_INVALID", "available traffic lacks motion evidence")
        gap = _list(traffic.get("rejoin_gap_range_s"), "rejoin gap range")
        if len(gap) != 2:
            _fail("CONTEXT_INVALID", "rejoin gap range must have two values")
        low = _finite_number(gap[0], "rejoin gap low", minimum=0.0)
        high = _finite_number(gap[1], "rejoin gap high", minimum=0.0)
        if high < low:
            _fail("CONTEXT_INVALID", "rejoin gap range is reversed")
    elif (
        estimate_available is False
        and status
        == (
            (
                "OBSERVED_ONLY_WAIT_ACTION_BOUND_REJOIN"
                if motion_context_sha256 is not None
                else "OBSERVED_ONLY_WAIT_REJOIN_MODEL"
            )
            if calibration_available
            else "OBSERVED_ONLY_WAIT_PIT_LOSS"
        )
        and traffic.get("rejoin_gap_range_s") is None
    ):
        pass
    else:
        _fail("CONTEXT_INVALID", "traffic identity/status is invalid")
    return traffic, stored


def _validate_context(
    value: object,
    *,
    expected_strategy_context_sha256: str,
    expected_source_binding: Mapping[str, object],
) -> dict[str, Any]:
    raw_context = _mapping(value, "strategy context")
    context_version = raw_context.get("contract_version")
    if context_version == CONTEXT_CONTRACT_VERSION:
        context_keys = _CONTEXT_KEYS
    elif context_version == CONTEXT_V2_CONTRACT_VERSION:
        context_keys = _CONTEXT_V2_KEYS
    else:
        _fail("CONTEXT_INVALID", "strategy context contract is unsupported")
    context = _exact_mapping(raw_context, context_keys, "strategy context")
    expected = _sha256(
        expected_strategy_context_sha256, "expected strategy context SHA-256"
    )
    stored = _sha256(context.get("context_sha256"), "strategy context SHA-256")
    if stored != expected:
        _fail("CONTEXT_INVALID", "strategy context does not match independent digest")
    material = {key: item for key, item in context.items() if key != "context_sha256"}
    if canonical_sha256(material) != stored:
        _fail("CONTEXT_INVALID", "strategy context self hash mismatch")

    source = _exact_mapping(
        context.get("source_binding"), _SOURCE_BINDING_KEYS, "context source binding"
    )
    if source != dict(expected_source_binding):
        _fail("CROSS_RECEIPT_LINEAGE_MISMATCH", "context source binding mismatch")
    identity, identity_sha = _validate_identity(context.get("event_identity"))

    observation = _exact_mapping(
        context.get("observation"), _OBSERVATION_KEYS, "context observation"
    )
    for key in ("decision_tick", "laps_completed", "source_epoch", "session_epoch"):
        _plain_int(observation.get(key), f"context observation {key}")
    for key in ("stale", "reset", "schema_changed"):
        if type(observation.get(key)) is not bool:
            _fail("CONTEXT_INVALID", f"context observation {key} must be boolean")
    if (
        observation.get("pits_open") is not None
        and type(observation.get("pits_open")) is not bool
    ):
        _fail("CONTEXT_INVALID", "pits_open must be boolean or null")
    if observation.get("penalty_state") not in {None, "CLEAR", "ACTIVE"}:
        _fail("CONTEXT_INVALID", "penalty_state is invalid")

    horizon = _exact_mapping(
        context.get("horizon"), _HORIZON_INPUT_KEYS, "context horizon"
    )
    if horizon.get("kind") not in {"LAPS", "TIMED"}:
        _fail("CONTEXT_INVALID", "horizon kind is invalid")
    if horizon.get("provenance") not in {
        "SDK_DIRECT",
        "SDK_DIRECT_AND_DERIVED",
        "CONTRACT_FIXTURE",
        "USER_RULE",
    }:
        _fail("CONTEXT_INVALID", "horizon provenance is invalid")
    _optional_plain_int(horizon.get("laps_remaining"), "horizon laps_remaining")
    _optional_number(
        horizon.get("leader_eta_to_next_crossing_s"),
        "horizon leader_eta_to_next_crossing_s",
    )
    _optional_number(
        horizon.get("reference_lap_time_s"),
        "horizon reference_lap_time_s",
        positive=True,
    )
    # iRacing can expose -1 as an unavailable time-remaining sentinel.  Keep
    # it representable so the capability layer can emit WAIT rather than
    # turning missing evidence into a schema failure.
    _optional_number(
        horizon.get("time_remaining_s"),
        "horizon time_remaining_s",
        minimum=-1.0,
    )
    if (
        horizon.get("player_is_leader") is not None
        and type(horizon.get("player_is_leader")) is not bool
    ):
        _fail("CONTEXT_INVALID", "player_is_leader must be boolean or null")

    vehicle = _exact_mapping(
        context.get("vehicle_context"), _VEHICLE_KEYS, "vehicle context"
    )
    _finite_number(
        vehicle.get("tank_capacity_l"), "vehicle tank capacity", positive=True
    )
    if vehicle.get("provenance") not in {"SDK_DIRECT", "USER_RULE", "CONTRACT_FIXTURE"}:
        _fail("CONTEXT_INVALID", "vehicle provenance is invalid")

    policy = _exact_mapping(
        context.get("strategy_policy"), _POLICY_KEYS, "strategy policy"
    )
    if policy.get("selection_policy") != "LATEST_COMMON_FUEL_FEASIBLE":
        _fail("CONTEXT_INVALID", "strategy selection policy is unsupported")
    reserve = _finite_number(policy.get("reserve_l"), "strategy reserve", minimum=0.0)
    quantile = _finite_number(
        policy.get("conservative_quantile"), "strategy conservative quantile"
    )
    if not 0.5 <= quantile <= 1.0 or reserve >= float(vehicle["tank_capacity_l"]):
        _fail("CONTEXT_INVALID", "strategy policy values are invalid")

    calibration, calibration_sha = _validate_calibration_model(
        context.get("calibration_model"), identity_sha256=identity_sha
    )
    if (
        context_version == CONTEXT_V2_CONTRACT_VERSION
        and calibration is not None
        and (
            calibration.get("method_version")
            != MATCHED_PIT_CALIBRATION_METHOD_VERSION
            or calibration.get("service_labels_available") is True
            and float(calibration["tire_change_time_s"]) <= 0.0
        )
    ):
        _fail(
            "CONTEXT_INVALID",
            "v2 tire strategy requires a valid matched pit/service calibration",
        )
    traffic, traffic_sha = _validate_traffic(
        context.get("traffic_rejoin"),
        identity_sha256=identity_sha,
        decision_tick=int(observation["decision_tick"]),
        calibration_available=calibration is not None,
    )
    tire_model: dict[str, Any] | None = None
    tire_model_sha: str | None = None
    tire_stint: dict[str, Any] | None = None
    tire_stint_sha: str | None = None
    if context_version == CONTEXT_V2_CONTRACT_VERSION:
        tire_model, tire_model_sha = _validate_tire_performance_model(
            context.get("tire_performance_model"),
            identity_sha256=identity_sha,
        )
        tire_stint, tire_stint_sha = _validate_tire_stint_context(
            context.get("tire_stint_context"),
            identity_sha256=identity_sha,
            decision_tick=int(observation["decision_tick"]),
            expected_source_receipt_sha256=(
                str(traffic["source_receipt_sha256"])
                if traffic is not None
                else None
            ),
        )
    validated = dict(context)
    validated["_context_version"] = context_version
    validated["_context_public_keys"] = context_keys
    validated["_identity"] = identity
    validated["_identity_sha256"] = identity_sha
    validated["_observation"] = observation
    validated["_horizon"] = horizon
    validated["_vehicle"] = vehicle
    validated["_policy"] = policy
    validated["_calibration"] = calibration
    validated["_calibration_sha256"] = calibration_sha
    validated["_traffic"] = traffic
    validated["_traffic_sha256"] = traffic_sha
    validated["_tire_model"] = tire_model
    validated["_tire_model_sha256"] = tire_model_sha
    validated["_tire_stint"] = tire_stint
    validated["_tire_stint_sha256"] = tire_stint_sha
    return validated


def _validate_rules_profile(
    value: object | None,
    *,
    expected_rules_profile_sha256: str | None,
    expected_rules_source_sha256: str | None,
    identity: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, Any] | None]:
    if value is None:
        if (
            expected_rules_profile_sha256 is not None
            or expected_rules_source_sha256 is not None
        ):
            _fail("RULES_PROFILE_INVALID", "rules digests supplied without a profile")
        base: dict[str, object] = {
            "exact_selector_match": False,
            "official_event_rules": False,
            "profile_id": None,
            "profile_sha256": None,
            "profile_version": None,
            "reason_codes": ["RULES_PROFILE_MISSING"],
            "source_document_sha256": None,
            "status": "WAIT_EVENT_RULES_IDENTITY",
        }
        return {**base, "rules_lineage_sha256": canonical_sha256(base)}, None

    profile = _exact_mapping(value, _RULES_PROFILE_KEYS, "rules profile")
    if profile.get("contract_version") != RULES_PROFILE_CONTRACT_VERSION:
        _fail("RULES_PROFILE_INVALID", "rules profile contract is unsupported")
    expected_profile = _sha256(
        expected_rules_profile_sha256, "expected rules profile SHA-256"
    )
    stored_profile = _sha256(profile.get("profile_sha256"), "rules profile SHA-256")
    if stored_profile != expected_profile:
        _fail(
            "RULES_PROFILE_INVALID", "rules profile does not match independent digest"
        )
    profile_material = {
        key: item for key, item in profile.items() if key != "profile_sha256"
    }
    if canonical_sha256(profile_material) != stored_profile:
        _fail("RULES_PROFILE_INVALID", "rules profile self hash mismatch")
    _identifier(profile.get("profile_id"), "rules profile id")
    _plain_int(profile.get("profile_version"), "rules profile version", minimum=1)

    selector = _exact_mapping(profile.get("selector"), _SELECTOR_KEYS, "rules selector")
    for key in ("series_id", "season_id", "track_id", "car_class_id"):
        _plain_int(selector.get(key), f"rules selector {key}", minimum=1)
    _plain_int(selector.get("race_week"), "rules selector race_week")
    for key in ("event_type", "track_config", "sim_build"):
        _identifier(selector.get(key), f"rules selector {key}")
    source = _exact_mapping(profile.get("source"), _RULES_SOURCE_KEYS, "rules source")
    if source.get("authority") != "IRACING_OFFICIAL":
        _fail("RULES_PROFILE_INVALID", "rules source authority is unsupported")
    _identifier(source.get("document_id"), "rules source document id")
    expected_source = _sha256(
        expected_rules_source_sha256, "expected official rules source SHA-256"
    )
    source_sha = _sha256(source.get("document_sha256"), "rules source document SHA-256")
    if source_sha != expected_source:
        _fail("RULES_PROFILE_INVALID", "official rules source digest mismatch")

    official_rules = _exact_mapping(
        profile.get("official_rules"), _OFFICIAL_RULE_KEYS, "official rules"
    )
    if official_rules.get("finish_rule") not in {
        "LAP_LIMITED",
        "TIMED_LEADER_CROSSING",
    }:
        _fail("RULES_PROFILE_INVALID", "finish_rule is unsupported")
    if official_rules.get("fuel_tire_service_timing") not in {
        "SEQUENTIAL",
        "CONCURRENT",
    }:
        _fail("RULES_PROFILE_INVALID", "service timing is unsupported")
    for key in ("no_tire_service_allowed", "tire_change_required"):
        if type(official_rules.get(key)) is not bool:
            _fail("RULES_PROFILE_INVALID", f"official rule {key} must be boolean")
    if official_rules["tire_change_required"] is official_rules[
        "no_tire_service_allowed"
    ]:
        _fail(
            "RULES_PROFILE_INVALID",
            "tire-change requirement and no-tire allowance are inconsistent",
        )
    if (
        not official_rules["tire_change_required"]
        and not official_rules["no_tire_service_allowed"]
    ):
        _fail("RULES_PROFILE_INVALID", "service rule has no legal tire choice")
    _plain_int(official_rules.get("minimum_pit_stops"), "minimum pit stops")

    reasons: list[str] = []
    selector_values = {key: identity.get(key) for key in _SELECTOR_KEYS}
    identity_complete = all(
        selector_values[key] is not None for key in _SELECTOR_KEYS
    ) and all(
        type(selector_values[key]) is int and int(selector_values[key]) > 0
        for key in ("series_id", "season_id", "track_id", "car_class_id")
    )
    exact_match = identity_complete and selector == selector_values
    if not identity_complete:
        reasons.append("EVENT_IDENTITY_INCOMPLETE")
    elif not exact_match:
        reasons.append("EVENT_SELECTOR_MISMATCH")
    # ``official_event_rules`` is deliberately not copied from a caller-owned
    # boolean (including the observed session's ``official`` field).  The
    # authority, independently supplied document digest, and exact selector
    # match above are the only promotion path.  The session's official flag is
    # retained as descriptive event identity, not as a rule-source attestation.
    verified = exact_match
    base = {
        "exact_selector_match": exact_match,
        "official_event_rules": verified,
        "profile_id": profile["profile_id"],
        "profile_sha256": stored_profile,
        "profile_version": profile["profile_version"],
        "reason_codes": reasons,
        "source_document_sha256": source_sha,
        "status": (
            "PASS_VERIFIED_OFFICIAL_EXACT_MATCH"
            if verified
            else "WAIT_EVENT_RULES_IDENTITY"
        ),
    }
    return {**base, "rules_lineage_sha256": canonical_sha256(base)}, profile


def _build_horizon(
    horizon_input: Mapping[str, object],
    *,
    rules_binding: Mapping[str, object],
    rules_profile: Mapping[str, object] | None,
) -> dict[str, object]:
    kind = str(horizon_input["kind"])
    reasons: list[str] = []
    branches: list[dict[str, object]] = []
    if kind == "LAPS":
        laps = horizon_input.get("laps_remaining")
        # 32767 is the observed iRacing unavailable/sentinel value, not a
        # 32k-lap race horizon.
        if type(laps) is int and 0 <= laps < 32767:
            branches = [
                {
                    "branch_id": "LAPS_EXACT",
                    "condition": "SESSION_LAPS_REMAINING",
                    "laps_to_go": laps,
                }
            ]
            status = "PASS_LAP_HORIZON"
            one_more_status = "NOT_APPLICABLE"
        else:
            reasons.append("LAPS_REMAINING_SENTINEL_OR_UNAVAILABLE")
            status = "WAIT_ONE_MORE_LAP_DATA"
            one_more_status = "WAIT_ONE_MORE_LAP_DATA"
        if rules_profile is not None and rules_binding["official_event_rules"] is True:
            official = _mapping(rules_profile["official_rules"], "official rules")
            if official["finish_rule"] != "LAP_LIMITED":
                branches = []
                reasons.append("FINISH_RULE_HORIZON_MISMATCH")
                status = "WAIT_ONE_MORE_LAP_DATA"
                one_more_status = "WAIT_ONE_MORE_LAP_DATA"
    else:
        required = {
            "time_remaining_s": horizon_input.get("time_remaining_s"),
            "reference_lap_time_s": horizon_input.get("reference_lap_time_s"),
            "leader_eta_to_next_crossing_s": horizon_input.get(
                "leader_eta_to_next_crossing_s"
            ),
        }
        ready = (
            horizon_input.get("player_is_leader") is True
            and all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in required.values()
            )
            and rules_profile is not None
            and rules_binding["official_event_rules"] is True
            and _mapping(rules_profile["official_rules"], "official rules").get(
                "finish_rule"
            )
            == "TIMED_LEADER_CROSSING"
        )
        if ready:
            remaining = float(required["time_remaining_s"])
            lap_time = float(required["reference_lap_time_s"])
            eta = float(required["leader_eta_to_next_crossing_s"])
            if remaining < 0 or lap_time <= 0 or eta < 0:
                ready = False
        if ready:
            base_laps = 1 + max(0, math.ceil((remaining - eta) / lap_time))
            branches = [
                {
                    "branch_id": "BASE",
                    "condition": "FIRST_LEADER_CROSSING_AT_OR_AFTER_EXPIRY",
                    "laps_to_go": base_laps,
                },
                {
                    "branch_id": "ONE_MORE",
                    "condition": "LEADER_CROSSING_BOUNDARY_UNCERTAINTY",
                    "laps_to_go": base_laps + 1,
                },
            ]
            status = "PASS_TIMED_BRANCH_SET"
            one_more_status = "PASS_BRANCH_SET"
        else:
            reasons.append("LEADER_CROSSING_OR_FINISH_RULE_UNAVAILABLE")
            status = "WAIT_ONE_MORE_LAP_DATA"
            one_more_status = "WAIT_ONE_MORE_LAP_DATA"
    material = {
        "branches": branches,
        "kind": kind,
        "one_more_lap_status": one_more_status,
        "reason_codes": reasons,
        "status": status,
    }
    return {**material, "horizon_sha256": canonical_sha256(material)}


def _observed_m1_calibration(m1: Mapping[str, object]) -> dict[str, object]:
    road_elapsed: list[float] = []
    stall_elapsed: list[float] = []
    service_elapsed: list[float] = []
    tank_changes: list[dict[str, object]] = []
    for raw_cycle in _list(m1.get("pit_cycles"), "M1 pit cycles"):
        cycle = _mapping(raw_cycle, "M1 pit cycle")
        road = _mapping(cycle.get("pit_road"), "M1 pit road")
        road_elapsed.append(
            round(
                _finite_number(
                    road.get("duration_s"), "M1 pit-road duration", minimum=0.0
                ),
                6,
            )
        )
        for raw_stall in _list(cycle.get("pit_stall_intervals"), "M1 stall intervals"):
            stall = _mapping(raw_stall, "M1 stall")
            stall_elapsed.append(
                round(
                    _finite_number(
                        stall.get("duration_s"), "M1 stall duration", minimum=0.0
                    ),
                    6,
                )
            )
        for raw_service in _list(cycle.get("service_episodes"), "M1 service episodes"):
            service = _mapping(raw_service, "M1 service")
            service_elapsed.append(
                round(
                    _finite_number(
                        service.get("duration_s"), "M1 service duration", minimum=0.0
                    ),
                    6,
                )
            )
            change = _mapping(
                service.get("observed_net_tank_change"), "M1 observed tank change"
            )
            if (
                change.get("interpretation")
                != "OBSERVED_ENDPOINT_TANK_LEVEL_DIFFERENCE_NOT_DELIVERED_FUEL"
            ):
                _fail("M1_RECEIPT_INVALID", "M1 tank delta interpretation is invalid")
            tank_changes.append(dict(change))
            _validate_unknown_service_contents(
                service.get("service_contents"), "M1 episode service contents"
            )
    summary = _mapping(m1.get("summary"), "M1 summary")
    return {
        "complete_stint_count": _plain_int(
            summary.get("complete_stint_count"), "M1 complete stint count"
        ),
        "observed_net_tank_changes": tank_changes,
        "pit_lane_loss_s": None,
        "pit_road_elapsed_s": road_elapsed,
        "reason_codes": [
            "PIT_ROAD_ELAPSED_IS_NOT_COUNTERFACTUAL_PIT_LOSS",
            "PITSTOPACTIVE_DOES_NOT_IDENTIFY_SERVICE_CONTENT",
            "TANK_ENDPOINT_DELTA_IS_NOT_DELIVERED_FUEL",
        ],
        "refuel_rate_l_per_s": None,
        "service_active_elapsed_s": service_elapsed,
        "service_content_model": None,
        "stall_elapsed_s": stall_elapsed,
        "status": "OBSERVED_SAMPLE_ONLY",
    }


def _capability(status: str, reasons: Sequence[str] = ()) -> dict[str, object]:
    return {"reason_codes": list(dict.fromkeys(reasons)), "status": status}


def _build_branch_plan(
    *,
    branches: Sequence[Mapping[str, object]],
    model_values: Mapping[str, object],
    scenario_values: Mapping[str, object],
    policy: Mapping[str, object],
    vehicle: Mapping[str, object],
) -> tuple[dict[str, object] | None, list[str]]:
    conservative_burn = float(model_values["conservative_burn"])
    current_fuel = float(model_values["current_fuel"])
    reserve = float(policy["reserve_l"])
    tank = float(vehicle["tank_capacity_l"])
    _close(reserve, float(scenario_values["reserve_l"]), "policy/fuel reserve")
    _close(
        float(policy["conservative_quantile"]),
        float(scenario_values["conservative_quantile"]),
        "policy/fuel quantile",
    )
    _close(
        tank, float(scenario_values["tank_capacity_l"]), "vehicle/fuel tank capacity"
    )
    safe_current = math.floor(
        (max(0.0, current_fuel - reserve) + 1e-12) / conservative_burn
    )
    safe_full = math.floor(((tank - reserve) + 1e-12) / conservative_burn)
    if safe_full < 1:
        return None, ["TANK_CANNOT_COVER_ONE_CONSERVATIVE_LAP"]
    windows: list[tuple[int, int, int]] = []
    for branch in branches:
        laps = int(branch["laps_to_go"])
        if laps <= safe_current:
            stops = 0
            earliest = latest = 0
        else:
            stops = math.ceil((laps - safe_current) / safe_full)
            earliest = max(0, laps - stops * safe_full)
            latest = safe_current
        if stops != 1:
            return None, ["V1_REQUIRES_EXACTLY_ONE_FUEL_STOP_IN_EVERY_BRANCH"]
        windows.append((earliest, latest, laps))
    common_earliest = max(window[0] for window in windows)
    common_latest = min(window[1] for window in windows)
    if common_earliest > common_latest:
        return None, ["NO_COMMON_PIT_WINDOW_ACROSS_HORIZON_BRANCHES"]
    recommended = common_latest
    fuel_at_box = current_fuel - conservative_burn * recommended
    if fuel_at_box < reserve - 1e-6:
        return None, ["RECOMMENDED_STOP_CONSUMES_RESERVE"]
    additions = []
    for _, _, laps in windows:
        needed_after = conservative_burn * (laps - recommended) + reserve
        additions.append(max(0.0, needed_after - fuel_at_box))
    fuel_add = max(additions)
    if fuel_at_box + fuel_add > tank + 1e-9:
        return None, ["FUEL_TO_END_EXCEEDS_TANK"]
    return {
        "branch_fuel_add_l": [round(value, 6) for value in additions],
        "common_earliest_lap_from_now": common_earliest,
        "common_latest_lap_from_now": common_latest,
        "fuel_add_l": round(fuel_add, 6),
        "fuel_at_box_l": round(fuel_at_box, 6),
        "recommended_lap_from_now": recommended,
    }, []


def _build_action_bound_rejoin(
    traffic: Mapping[str, object],
    calibration: Mapping[str, object],
    action: Mapping[str, object],
    *,
    fuel_tire_service_timing: str,
) -> dict[str, object] | None:
    motion = traffic.get("motion_context")
    if not isinstance(motion, Mapping):
        return None
    stationary = float(action["estimated_stationary_service_s"])
    uncertainty = _list(
        calibration.get("pit_lane_loss_uncertainty_s"),
        "calibrated pit-loss uncertainty",
    )
    loss_low = float(uncertainty[0]) + stationary
    loss_high = float(uncertainty[1]) + stationary
    pit_lap = _plain_int(action.get("recommended_lap_from_now"), "rejoin pit lap")
    nearest_ahead, nearest_behind, reasons = project_physical_rejoin(
        motion,
        loss_range_s=(loss_low, loss_high),
        recommended_lap_from_now=pit_lap,
    )
    available = not reasons
    source_receipt_sha256 = canonical_sha256(
        {
            "calibration_model_sha256": calibration["model_sha256"],
            "motion_context_sha256": motion["motion_sha256"],
        }
    )
    service = {
        "change_tires": action["change_tires"],
        "fuel_add_l": action["fuel_add_l"],
        "fuel_tire_service_timing": fuel_tire_service_timing,
        "recommended_lap_from_now": pit_lap,
        "stationary_service_s": action["estimated_stationary_service_s"],
        "total_pit_loss_range_s": [round(loss_low, 6), round(loss_high, 6)],
    }
    material: dict[str, object] = {
        "calibration_model_sha256": calibration["model_sha256"],
        "contract_version": REJOIN_CONTRACT_VERSION,
        "decision_tick": traffic["observed_at_decision_tick"],
        "estimate_available": available,
        "identity_sha256": traffic["identity_sha256"],
        "method_version": REJOIN_METHOD_VERSION,
        "motion_context_sha256": motion["motion_sha256"],
        "nearest_ahead": nearest_ahead,
        "nearest_behind": nearest_behind,
        "reason_codes": reasons,
        "service_scenario": service,
        "source_receipt_sha256": source_receipt_sha256,
        "status": (
            "AVAILABLE_STABLE_BRACKET"
            if available
            else "WAIT_AMBIGUOUS_REJOIN_ORDER"
        ),
    }
    return {**material, "estimate_sha256": canonical_sha256(material)}


def _traffic_semantic_sha256(traffic: Mapping[str, object]) -> str:
    material = {
        key: value
        for key, value in traffic.items()
        if key
        not in {
            "motion_context",
            "motion_context_sha256",
            "observed_at_decision_tick",
            "traffic_sha256",
        }
    }
    motion = traffic.get("motion_context")
    if isinstance(motion, Mapping):
        material["motion_context"] = {
            key: value
            for key, value in motion.items()
            if key not in {"decision_tick", "motion_sha256"}
        }
    else:
        material["motion_context"] = None
    return canonical_sha256(material)


def _rejoin_semantic_sha256(estimate: Mapping[str, object]) -> str:
    return canonical_sha256(
        {
            key: value
            for key, value in estimate.items()
            if key
            not in {
                "decision_tick",
                "estimate_sha256",
                "motion_context_sha256",
                "source_receipt_sha256",
            }
        }
    )


def _build_tire_performance_belief(
    model: Mapping[str, object],
    calibration: Mapping[str, object],
    tire_stint: Mapping[str, object],
    *,
    branch_plan: Mapping[str, object],
    horizon_branches: Sequence[object],
    fuel_tire_service_timing: str,
) -> dict[str, object]:
    current_age = _plain_int(
        tire_stint.get("stint_age_completed_laps"),
        "current tire-stint age",
    )
    current_compound = _plain_int(
        tire_stint.get("current_tire_compound"),
        "current tire compound",
    )
    until_pit = _plain_int(
        branch_plan.get("recommended_lap_from_now"),
        "laps until pit",
    )
    maximum_horizon_laps = max(
        _plain_int(
            _mapping(raw, "tire horizon branch").get("laps_to_go"),
            "tire horizon branch laps",
        )
        for raw in horizon_branches
    )
    after_pit = maximum_horizon_laps - until_pit
    if after_pit < 0:  # pragma: no cover - branch-plan invariant
        raise AssertionError("pit recommendation exceeds its horizon")
    fuel_add = _finite_number(
        branch_plan.get("fuel_add_l"),
        "tire-strategy fuel addition",
        minimum=0.0,
    )
    if fuel_tire_service_timing not in {"PARALLEL", "SEQUENTIAL"}:
        _fail("CONTEXT_INVALID", "tire service timing is unsupported")

    fuel_time = fuel_add / float(calibration["refuel_rate_l_per_s"])
    tire_time = float(calibration["tire_change_time_s"])
    tire_stationary = (
        fuel_time + tire_time
        if fuel_tire_service_timing == "SEQUENTIAL"
        else max(fuel_time, tire_time)
    )
    incremental_service = round(tire_stationary - fuel_time, 6)
    age_at_pit = current_age + until_pit
    maximum_projected_age = age_at_pit + after_pit
    loss_range: list[float] | None = None
    estimate_available = False
    preference = "WAIT"
    reasons: list[str] = []
    if model.get("estimate_available") is not True:
        status = str(model["status"])
        reasons.append(status)
    elif int(model["tire_compound"]) != current_compound:
        status = "WAIT_TIRE_COMPOUND_MISMATCH"
        reasons.append("TIRE_PERFORMANCE_MODEL_COMPOUND_MISMATCH")
    elif maximum_projected_age > int(model["max_supported_stint_age_laps"]):
        status = "WAIT_TIRE_MODEL_EXTRAPOLATION"
        reasons.append("PROJECTED_TIRE_AGE_EXCEEDS_CALIBRATION")
    else:
        slope_bounds = _list(
            model["performance_age_slope_uncertainty_s_per_lap"],
            "tire-performance slope uncertainty",
        )
        scale = age_at_pit * after_pit
        loss_range = [
            round(float(slope_bounds[0]) * scale, 6),
            round(float(slope_bounds[1]) * scale, 6),
        ]
        estimate_available = True
        if loss_range[0] > incremental_service:
            status = "PASS_SHADOW_CHANGE_TIRES"
            preference = "CHANGE_TIRES"
        elif loss_range[1] < incremental_service:
            status = "WAIT_PHYSICAL_WEAR_FOR_NO_TIRE_SERVICE"
            preference = "KEEP_TIRES"
            reasons.append("CURRENT_PHYSICAL_WEAR_REQUIRED_FOR_KEEP_TIRES")
        else:
            status = "WAIT_PERFORMANCE_SERVICE_TRADEOFF"
            preference = "AMBIGUOUS"
            reasons.append("TIRE_SERVICE_TRADEOFF_INTERVAL_OVERLAP")
    scenario = {
        "age_at_pit_laps": age_at_pit,
        "current_stint_age_laps": current_age,
        "current_stint_context_sha256": tire_stint["context_sha256"],
        "current_tire_compound": current_compound,
        "fuel_add_l": round(fuel_add, 6),
        "fuel_tire_service_timing": fuel_tire_service_timing,
        "incremental_tire_service_s": incremental_service,
        "keep_tires_time_loss_range_s": loss_range,
        "laps_after_pit": after_pit,
        "laps_until_pit": until_pit,
        "max_projected_tire_age_laps": maximum_projected_age,
    }
    material: dict[str, object] = {
        "advisor_only": True,
        "calibration_model_sha256": calibration["model_sha256"],
        "contract_version": TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION,
        "estimate_available": estimate_available,
        "identity_sha256": model["identity_sha256"],
        "method_version": TIRE_PERFORMANCE_BELIEF_METHOD_VERSION,
        "model_sha256": model["model_sha256"],
        "performance_preference": preference,
        "physical_wear": dict(_TIRE_PHYSICAL_WEAR_UNAVAILABLE),
        "reason_codes": reasons,
        "scenario": scenario,
        "source_receipt_sha256": tire_stint["source_receipt_sha256"],
        "status": status,
    }
    return {**material, "belief_sha256": canonical_sha256(material)}


def _build_tire_strategy(
    *,
    context_version: str,
    rules_binding: Mapping[str, object],
    rules_profile: Mapping[str, object] | None,
    branch_plan: Mapping[str, object] | None,
    horizon: Mapping[str, object],
    calibration_model: Mapping[str, object] | None,
    tire_model: Mapping[str, object] | None,
    tire_stint: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if context_version == CONTEXT_CONTRACT_VERSION:
        return None
    if context_version != CONTEXT_V2_CONTRACT_VERSION:  # pragma: no cover - validator
        raise AssertionError("unsupported validated context version")

    prerequisite_reasons: list[str] = []
    if (
        rules_profile is None
        or rules_binding.get("official_event_rules") is not True
    ):
        prerequisite_reasons.append("VERIFIED_EVENT_RULES_REQUIRED")
    if branch_plan is None:
        prerequisite_reasons.append("COMMON_ONE_STOP_PLAN_REQUIRED")
    if calibration_model is None:
        prerequisite_reasons.append("MATCHED_PIT_CALIBRATION_REQUIRED")
    elif calibration_model.get("service_labels_available") is not True:
        prerequisite_reasons.append("MATCHED_SERVICE_LABELS_REQUIRED")
    if prerequisite_reasons:
        return {
            "belief": None,
            "change_tires": None,
            "reason_codes": prerequisite_reasons,
            "status": "WAIT_TIRE_STRATEGY_PREREQUISITES",
        }

    assert rules_profile is not None
    assert branch_plan is not None
    assert calibration_model is not None
    official_rules = _mapping(rules_profile["official_rules"], "official rules")
    if int(official_rules["minimum_pit_stops"]) > 1:
        return {
            "belief": None,
            "change_tires": None,
            "reason_codes": ["MULTI_STOP_RULE_NOT_SUPPORTED"],
            "status": "WAIT_TIRE_STRATEGY_PREREQUISITES",
        }
    if official_rules["tire_change_required"] is True:
        return {
            "belief": None,
            "change_tires": True,
            "reason_codes": [],
            "status": "PASS_RULE_MANDATED_TIRE_CHANGE",
        }
    if tire_stint is None:  # pragma: no cover - v2 context invariant
        raise AssertionError("v2 context lacks a validated tire-stint context")
    if tire_stint.get("availability") != "AVAILABLE":
        return {
            "belief": None,
            "change_tires": None,
            "reason_codes": list(tire_stint["reason_codes"]),
            "status": str(tire_stint["status"]),
        }
    if tire_model is None:
        return {
            "belief": None,
            "change_tires": None,
            "reason_codes": ["TIRE_PERFORMANCE_MODEL_REQUIRED"],
            "status": "WAIT_TIRE_PERFORMANCE_MODEL",
        }
    timing = (
        "SEQUENTIAL"
        if official_rules["fuel_tire_service_timing"] == "SEQUENTIAL"
        else "PARALLEL"
    )
    belief = _build_tire_performance_belief(
        tire_model,
        calibration_model,
        tire_stint,
        branch_plan=branch_plan,
        horizon_branches=_list(horizon["branches"], "horizon branches"),
        fuel_tire_service_timing=timing,
    )
    if belief["status"] == "PASS_SHADOW_CHANGE_TIRES":
        return {
            "belief": belief,
            "change_tires": True,
            "reason_codes": [],
            "status": "PASS_MODEL_SELECTED_TIRE_CHANGE",
        }
    return {
        "belief": belief,
        "change_tires": None,
        "reason_codes": list(belief["reason_codes"]),
        "status": belief["status"],
    }


def _tire_strategy_semantic_sha256(tire_strategy: Mapping[str, object]) -> str:
    belief = tire_strategy.get("belief")
    if not isinstance(belief, Mapping):
        return canonical_sha256(tire_strategy)
    belief_material = {
        key: value
        for key, value in belief.items()
        if key not in {"belief_sha256", "source_receipt_sha256"}
    }
    scenario = _mapping(belief_material["scenario"], "tire belief scenario")
    belief_material["scenario"] = {
        key: value
        for key, value in scenario.items()
        if key != "current_stint_context_sha256"
    }
    return canonical_sha256(
        {
            **tire_strategy,
            "belief": belief_material,
        }
    )


def _validate_tire_strategy_shape(value: object) -> dict[str, Any]:
    strategy = _exact_mapping(value, _TIRE_STRATEGY_KEYS, "tire strategy")
    status = _identifier(strategy.get("status"), "tire-strategy status")
    reasons = _list(strategy.get("reason_codes"), "tire-strategy reasons")
    if any(type(item) is not str or not item for item in reasons) or len(
        reasons
    ) != len(set(reasons)):
        _fail("PREVIOUS_STATE_INVALID", "tire-strategy reasons are invalid")
    change_tires = strategy.get("change_tires")
    if change_tires is not None and type(change_tires) is not bool:
        _fail("PREVIOUS_STATE_INVALID", "tire-strategy action is invalid")
    belief_value = strategy.get("belief")
    if belief_value is None:
        if status == "PASS_RULE_MANDATED_TIRE_CHANGE":
            if change_tires is not True or reasons:
                _fail(
                    "PREVIOUS_STATE_INVALID",
                    "rule-mandated tire strategy is inconsistent",
                )
        elif status.startswith("PASS_") or change_tires is not None or not reasons:
            _fail("PREVIOUS_STATE_INVALID", "tire-strategy WAIT is inconsistent")
        return strategy

    belief = _exact_mapping(
        belief_value,
        _TIRE_PERFORMANCE_BELIEF_KEYS,
        "tire-performance belief",
    )
    if (
        belief.get("contract_version") != TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION
        or belief.get("method_version") != TIRE_PERFORMANCE_BELIEF_METHOD_VERSION
        or belief.get("advisor_only") is not True
    ):
        _fail("PREVIOUS_STATE_INVALID", "tire-performance belief contract is invalid")
    belief_sha = _sha256(
        belief.get("belief_sha256"), "tire-performance belief SHA-256"
    )
    if canonical_sha256(
        {key: item for key, item in belief.items() if key != "belief_sha256"}
    ) != belief_sha:
        _fail("PREVIOUS_STATE_INVALID", "tire-performance belief self hash mismatch")
    for key in (
        "calibration_model_sha256",
        "identity_sha256",
        "model_sha256",
        "source_receipt_sha256",
    ):
        _sha256(belief.get(key), f"tire-performance belief {key}")
    _validate_tire_physical_wear_boundary(
        belief.get("physical_wear"),
        "tire-performance belief physical-wear boundary",
    )
    belief_reasons = _list(
        belief.get("reason_codes"), "tire-performance belief reasons"
    )
    if any(type(item) is not str or not item for item in belief_reasons) or len(
        belief_reasons
    ) != len(set(belief_reasons)):
        _fail("PREVIOUS_STATE_INVALID", "tire-performance belief reasons are invalid")
    scenario = _exact_mapping(
        belief.get("scenario"),
        _TIRE_PERFORMANCE_SCENARIO_KEYS,
        "tire-performance belief scenario",
    )
    for key in (
        "age_at_pit_laps",
        "current_stint_age_laps",
        "current_tire_compound",
        "laps_after_pit",
        "laps_until_pit",
        "max_projected_tire_age_laps",
    ):
        _plain_int(scenario.get(key), f"tire-performance scenario {key}")
    _sha256(
        scenario.get("current_stint_context_sha256"),
        "tire-performance current-stint context",
    )
    for key in ("fuel_add_l", "incremental_tire_service_s"):
        _finite_number(
            scenario.get(key),
            f"tire-performance scenario {key}",
            minimum=0.0,
        )
    if scenario.get("fuel_tire_service_timing") not in {"PARALLEL", "SEQUENTIAL"}:
        _fail("PREVIOUS_STATE_INVALID", "tire-performance timing is invalid")
    loss_range = scenario.get("keep_tires_time_loss_range_s")
    if loss_range is not None:
        bounds = _list(loss_range, "tire keep-performance loss range")
        if len(bounds) != 2:
            _fail("PREVIOUS_STATE_INVALID", "tire loss range needs two bounds")
        low = _finite_number(bounds[0], "tire loss low", minimum=0.0)
        high = _finite_number(bounds[1], "tire loss high", minimum=0.0)
        if high < low:
            _fail("PREVIOUS_STATE_INVALID", "tire loss range is reversed")
    expected_strategy = (
        {
            "belief": belief,
            "change_tires": True,
            "reason_codes": [],
            "status": "PASS_MODEL_SELECTED_TIRE_CHANGE",
        }
        if belief.get("status") == "PASS_SHADOW_CHANGE_TIRES"
        else {
            "belief": belief,
            "change_tires": None,
            "reason_codes": belief_reasons,
            "status": belief.get("status"),
        }
    )
    if strategy != expected_strategy:
        _fail("PREVIOUS_STATE_INVALID", "tire strategy is not belief-derived")
    return strategy


def _context_keys_for_version(version: object) -> frozenset[str]:
    if version == CONTEXT_CONTRACT_VERSION:
        return _CONTEXT_KEYS
    if version == CONTEXT_V2_CONTRACT_VERSION:
        return _CONTEXT_V2_KEYS
    _fail("CONTEXT_INVALID", "strategy context contract is unsupported")


def _output_keys_for_version(version: object) -> frozenset[str]:
    if version == CONTRACT_VERSION:
        return _OUTPUT_KEYS
    if version == CONTRACT_V2_VERSION:
        return _OUTPUT_V2_KEYS
    _fail("PREVIOUS_STATE_INVALID", "M2 receipt contract is unsupported")


def _validate_previous(
    value: object | None,
    *,
    expected_previous_receipt_sha256: str | None,
    expected_previous_revision: int | None,
    source_binding: Mapping[str, object],
    observation: Mapping[str, object],
) -> dict[str, Any] | None:
    if value is None:
        if (
            expected_previous_receipt_sha256 is not None
            or expected_previous_revision is not None
        ):
            _fail(
                "PREVIOUS_STATE_INVALID",
                "previous-state expectations supplied without state",
            )
        return None
    raw_previous = _mapping(value, "previous M2 receipt")
    previous_contract = raw_previous.get("contract_version")
    previous = _exact_mapping(
        raw_previous,
        _output_keys_for_version(previous_contract),
        "previous M2 receipt",
    )
    expected = _sha256(
        expected_previous_receipt_sha256, "expected previous M2 receipt SHA-256"
    )
    stored = _sha256(
        previous.get("m2_strategy_receipt_sha256"), "previous M2 self SHA-256"
    )
    if stored != expected:
        _fail("PREVIOUS_STATE_INVALID", "previous state failed optimistic concurrency")
    material = {
        key: item
        for key, item in previous.items()
        if key != "m2_strategy_receipt_sha256"
    }
    if canonical_sha256(material) != stored:
        _fail("PREVIOUS_STATE_INVALID", "previous M2 self hash mismatch")
    if (
        previous.get("advisor_only") is not True
        or previous.get("attestation_status") != "NOT_R7_ATTESTED"
        or previous.get("derivation_status") != "POST_ADMISSION_PACKAGE_EXTERNAL"
        or previous.get("execution_mode") != "SHADOW_ONLY"
    ):
        _fail("PREVIOUS_STATE_INVALID", "previous M2 safety boundary is invalid")
    previous_input = _exact_mapping(
        previous.get("input_binding"),
        _INPUT_BINDING_KEYS,
        "previous input binding",
    )
    previous_lineage = _sha256(
        previous_input.get("input_lineage_sha256"),
        "previous input lineage SHA-256",
    )
    if (
        canonical_sha256(
            {
                key: item
                for key, item in previous_input.items()
                if key != "input_lineage_sha256"
            }
        )
        != previous_lineage
    ):
        _fail("PREVIOUS_STATE_INVALID", "previous input lineage self hash mismatch")
    for key in _SOURCE_BINDING_KEYS:
        if previous_input.get(key) != source_binding.get(key):
            _fail("PREVIOUS_STATE_INVALID", "previous source lineage mismatch")
    for key in (
        "context_sha256",
        "fuel_replay_sha256",
        "m1_receipt_sha256",
        "model_semantic_sha256",
    ):
        _sha256(previous_input.get(key), f"previous input binding {key}")
    raw_previous_context = _mapping(
        previous.get("strategy_context"), "previous strategy context"
    )
    previous_context = _exact_mapping(
        raw_previous_context,
        _context_keys_for_version(raw_previous_context.get("contract_version")),
        "previous strategy context",
    )
    expected_previous_contract = (
        CONTRACT_V2_VERSION
        if previous_context.get("contract_version") == CONTEXT_V2_CONTRACT_VERSION
        else CONTRACT_VERSION
    )
    if previous_contract != expected_previous_contract:
        _fail("PREVIOUS_STATE_INVALID", "previous M2/context versions disagree")
    previous_context_sha = _sha256(
        previous_context.get("context_sha256"),
        "previous strategy context SHA-256",
    )
    if (
        previous_context_sha != previous_input["context_sha256"]
        or canonical_sha256(
            {
                key: item
                for key, item in previous_context.items()
                if key != "context_sha256"
            }
        )
        != previous_context_sha
    ):
        _fail("PREVIOUS_STATE_INVALID", "previous strategy context hash mismatch")
    try:
        _validate_context(
            previous_context,
            expected_strategy_context_sha256=previous_context_sha,
            expected_source_binding=source_binding,
        )
        if previous_contract == CONTRACT_V2_VERSION:
            _validate_tire_strategy_shape(previous.get("tire_strategy"))
    except M2StrategyReceiptError as exc:
        if exc.code == "PREVIOUS_STATE_INVALID":
            raise
        _fail("PREVIOUS_STATE_INVALID", f"previous M2 evidence is invalid: {exc}")
    lifecycle = _exact_mapping(
        previous.get("lifecycle"), _LIFECYCLE_KEYS, "previous lifecycle"
    )
    revision = _plain_int(
        lifecycle.get("state_revision"), "previous state revision", minimum=1
    )
    if (
        type(expected_previous_revision) is not int
        or expected_previous_revision != revision
    ):
        _fail(
            "PREVIOUS_STATE_INVALID", "previous revision failed optimistic concurrency"
        )
    point = _exact_mapping(
        lifecycle.get("observation_point"),
        _OBSERVATION_POINT_KEYS,
        "previous observation point",
    )
    if point.get("source_epoch") != observation.get("source_epoch") or point.get(
        "session_epoch"
    ) != observation.get("session_epoch"):
        _fail("PREVIOUS_STATE_INVALID", "previous lifecycle epoch mismatch")
    previous_tick = _plain_int(point.get("decision_tick"), "previous decision tick")
    if int(observation["decision_tick"]) <= previous_tick:
        _fail("PREVIOUS_STATE_INVALID", "decision tick is not monotonic")
    _list(lifecycle.get("events"), "previous lifecycle events")
    recommendations = _list(previous.get("recommendations"), "previous recommendations")
    if len(recommendations) > 1:
        _fail("PREVIOUS_STATE_INVALID", "previous state has multiple recommendations")
    active = lifecycle.get("active_recommendation_id")
    if recommendations:
        recommendation = _exact_mapping(
            recommendations[0], _RECOMMENDATION_KEYS, "previous recommendation"
        )
        recommendation_id = _identifier(
            recommendation.get("recommendation_id"), "previous recommendation id"
        )
        if (
            recommendation_id != active
            or not recommendation_id.startswith("m2-strategy:")
            or len(recommendation_id) != len("m2-strategy:") + 64
            or any(
                character not in _SHA256_CHARS
                for character in recommendation_id.removeprefix("m2-strategy:")
            )
            or recommendation.get("executable") is not False
            or recommendation.get("status") != "SHADOW_ONLY"
        ):
            _fail("PREVIOUS_STATE_INVALID", "previous active recommendation is invalid")
    elif active is not None:
        _fail("PREVIOUS_STATE_INVALID", "previous active id lacks a recommendation")
    quality = _exact_mapping(
        previous.get("quality_gate"),
        frozenset({"reason_codes", "status"}),
        "previous quality gate",
    )
    quality_reasons = _list(quality.get("reason_codes"), "previous quality reasons")
    if any(type(item) is not str for item in quality_reasons):
        _fail("PREVIOUS_STATE_INVALID", "previous quality reasons are invalid")
    if recommendations:
        if quality != {"reason_codes": [], "status": "PASS_SHADOW_CONTRACT"}:
            _fail("PREVIOUS_STATE_INVALID", "active previous state is not PASS")
    elif quality.get("status") != "WAIT_CAPABILITIES":
        _fail("PREVIOUS_STATE_INVALID", "inactive previous state is not WAIT")
    return previous


def build_m2_strategy_receipt(
    fuel_replay_value: object,
    m1_receipt_value: object,
    strategy_context_value: object,
    *,
    expected_fuel_replay_sha256: str,
    expected_m1_receipt_sha256: str,
    expected_strategy_context_sha256: str,
    rules_profile_value: object | None = None,
    expected_rules_profile_sha256: str | None = None,
    expected_rules_source_sha256: str | None = None,
    previous_receipt_value: object | None = None,
    expected_previous_receipt_sha256: str | None = None,
    expected_previous_revision: int | None = None,
) -> dict[str, object]:
    """Return one deterministic shadow receipt or an explicit capability WAIT."""

    fuel = _validate_fuel_replay(
        fuel_replay_value,
        expected_fuel_replay_sha256=expected_fuel_replay_sha256,
    )
    m1 = _validate_m1_receipt(
        m1_receipt_value,
        expected_m1_receipt_sha256=expected_m1_receipt_sha256,
    )
    source_binding = _source_binding_from_inputs(fuel, m1)
    context = _validate_context(
        strategy_context_value,
        expected_strategy_context_sha256=expected_strategy_context_sha256,
        expected_source_binding=source_binding,
    )
    identity = _mapping(context["_identity"], "validated identity")
    identity_sha = str(context["_identity_sha256"])
    context_version = str(context["_context_version"])
    observation = _mapping(context["_observation"], "validated observation")
    vehicle = _mapping(context["_vehicle"], "validated vehicle")
    policy = _mapping(context["_policy"], "validated policy")
    calibration_model = context["_calibration"]
    traffic = context["_traffic"]
    tire_model = context["_tire_model"]
    tire_stint = context["_tire_stint"]

    rules_binding, rules_profile = _validate_rules_profile(
        rules_profile_value,
        expected_rules_profile_sha256=expected_rules_profile_sha256,
        expected_rules_source_sha256=expected_rules_source_sha256,
        identity=identity,
    )
    horizon = _build_horizon(
        _mapping(context["_horizon"], "validated horizon"),
        rules_binding=rules_binding,
        rules_profile=rules_profile,
    )
    observed_calibration = _observed_m1_calibration(m1)

    capabilities: dict[str, object] = {}
    capabilities["event_rules_identity"] = _capability(
        str(rules_binding["status"]),
        rules_binding["reason_codes"],  # type: ignore[arg-type]
    )
    capabilities["one_more_lap"] = _capability(
        str(horizon["one_more_lap_status"]),
        horizon["reason_codes"],  # type: ignore[arg-type]
    )
    if calibration_model is None:
        capabilities["pit_loss_calibration"] = _capability(
            "WAIT_MATCHED_PIT_LOSS_BASELINE",
            ("M1_PIT_ELAPSED_IS_NOT_PIT_LOSS",),
        )
        capabilities["service_labels"] = _capability(
            "WAIT_SERVICE_LABELS",
            ("M1_SERVICE_CONTENTS_UNOBSERVABLE",),
        )
    else:
        capabilities["pit_loss_calibration"] = _capability("PASS_CALIBRATED")
        if (
            _mapping(calibration_model, "calibration model")["service_labels_available"]
            is True
        ):
            capabilities["service_labels"] = _capability("PASS_SERVICE_LABELS")
        else:
            capabilities["service_labels"] = _capability(
                "WAIT_SERVICE_LABELS", ("CALIBRATION_LACKS_SERVICE_LABELS",)
            )
    dynamic_reasons: list[str] = []
    dynamic_status = "PASS_PIT_OPEN_AND_PENALTY_STATE"
    if observation["stale"] or observation["reset"] or observation["schema_changed"]:
        dynamic_reasons.append("STALE_RESET_OR_SCHEMA_CHANGE")
        dynamic_status = "WAIT_PIT_OPEN_AND_PENALTY_STATE"
    if observation["pits_open"] is not True:
        dynamic_reasons.append(
            "PITS_CLOSED" if observation["pits_open"] is False else "PITS_OPEN_UNKNOWN"
        )
        dynamic_status = (
            "BLOCKED_PITS_CLOSED"
            if observation["pits_open"] is False
            else "WAIT_PIT_OPEN_AND_PENALTY_STATE"
        )
    if observation["penalty_state"] != "CLEAR":
        dynamic_reasons.append(
            "ACTIVE_PENALTY"
            if observation["penalty_state"] == "ACTIVE"
            else "PENALTY_STATE_UNKNOWN"
        )
        dynamic_status = (
            "BLOCKED_ACTIVE_PENALTY"
            if observation["penalty_state"] == "ACTIVE"
            else "WAIT_PIT_OPEN_AND_PENALTY_STATE"
        )
    capabilities["pit_open_and_penalty_state"] = _capability(
        dynamic_status, dynamic_reasons
    )

    model_values = _mapping(fuel["_validated_model_values"], "fuel model values")
    scenario_values = _mapping(
        fuel["_validated_scenario_values"], "fuel scenario values"
    )
    branch_plan: dict[str, object] | None = None
    strategy_reasons: list[str] = []
    if str(horizon["status"]).startswith("PASS_"):
        branch_plan, strategy_reasons = _build_branch_plan(
            branches=_list(horizon["branches"], "horizon branches"),
            model_values=model_values,
            scenario_values=scenario_values,
            policy=policy,
            vehicle=vehicle,
        )
    else:
        strategy_reasons = ["DISTANCE_HORIZON_UNAVAILABLE"]
    capabilities["strategy_data"] = _capability(
        (
            "PASS_COMMON_ONE_STOP_PLAN"
            if branch_plan is not None
            else "WAIT_STRATEGY_DATA"
        ),
        strategy_reasons,
    )

    tire_strategy = _build_tire_strategy(
        context_version=context_version,
        rules_binding=rules_binding,
        rules_profile=rules_profile,
        branch_plan=branch_plan,
        horizon=horizon,
        calibration_model=(
            _mapping(calibration_model, "calibration model")
            if isinstance(calibration_model, dict)
            else None
        ),
        tire_model=(
            _mapping(tire_model, "tire-performance model")
            if isinstance(tire_model, dict)
            else None
        ),
        tire_stint=(
            _mapping(tire_stint, "tire-stint context")
            if isinstance(tire_stint, dict)
            else None
        ),
    )
    if tire_strategy is not None:
        capabilities["tire_strategy"] = _capability(
            str(tire_strategy["status"]),
            _list(tire_strategy["reason_codes"], "tire-strategy reasons"),
        )

    provisional_action: dict[str, object] | None = None
    action_rejoin_estimate: dict[str, object] | None = None
    official_rules: dict[str, Any] | None = (
        _mapping(rules_profile["official_rules"], "official rules")
        if rules_profile is not None
        and rules_binding["official_event_rules"] is True
        else None
    )
    tire_action_ready = (
        context_version == CONTEXT_CONTRACT_VERSION
        or tire_strategy is not None
        and tire_strategy["status"]
        in {
            "PASS_RULE_MANDATED_TIRE_CHANGE",
            "PASS_MODEL_SELECTED_TIRE_CHANGE",
        }
    )
    if (
        branch_plan is not None
        and official_rules is not None
        and isinstance(calibration_model, dict)
        and calibration_model.get("service_labels_available") is True
        and tire_action_ready
    ):
        calibration = _mapping(calibration_model, "calibration model")
        if int(official_rules["minimum_pit_stops"]) > 1:
            capabilities["strategy_data"] = _capability(
                "WAIT_STRATEGY_DATA", ("MULTI_STOP_RULE_NOT_SUPPORTED",)
            )
        else:
            change_tires = (
                bool(official_rules["tire_change_required"])
                if context_version == CONTEXT_CONTRACT_VERSION
                else True
            )
            fuel_time = float(branch_plan["fuel_add_l"]) / float(
                calibration["refuel_rate_l_per_s"]
            )
            tire_time = (
                float(calibration["tire_change_time_s"]) if change_tires else 0.0
            )
            stationary = (
                fuel_time + tire_time
                if official_rules["fuel_tire_service_timing"] == "SEQUENTIAL"
                else max(fuel_time, tire_time)
            )
            provisional_action = {
                "change_tires": change_tires,
                "estimated_stationary_service_s": round(stationary, 6),
                "estimated_total_pit_loss_s": round(
                    float(calibration["pit_lane_loss_s"]) + stationary, 6
                ),
                "fuel_add_l": branch_plan["fuel_add_l"],
                "recommended_lap_from_now": branch_plan[
                    "recommended_lap_from_now"
                ],
            }
            if isinstance(traffic, dict):
                rejoin_timing = (
                    "SEQUENTIAL"
                    if official_rules["fuel_tire_service_timing"] == "SEQUENTIAL"
                    else "PARALLEL"
                )
                action_rejoin_estimate = _build_action_bound_rejoin(
                    traffic,
                    calibration,
                    provisional_action,
                    fuel_tire_service_timing=rejoin_timing,
                )

    action_traffic_estimate_available = (
        isinstance(action_rejoin_estimate, dict)
        and action_rejoin_estimate.get("estimate_available") is True
    )
    # Only the estimate reproduced for this exact action can authorize it.
    # Legacy input estimates remain descriptive and cannot override a WAIT.
    traffic_estimate_available = action_traffic_estimate_available
    if traffic is None:
        capabilities["traffic_data"] = _capability(
            "WAIT_TRAFFIC_DATA", ("REJOIN_TRAFFIC_INPUT_UNAVAILABLE",)
        )
    elif traffic_estimate_available:
        capabilities["traffic_data"] = _capability("PASS_TRAFFIC_DATA")
    elif action_rejoin_estimate is not None:
        capabilities["traffic_data"] = _capability(
            "WAIT_REJOIN_ESTIMATE",
            _list(
                action_rejoin_estimate["reason_codes"],
                "action-bound rejoin reasons",
            ),
        )
    elif calibration_model is None:
        capabilities["traffic_data"] = _capability(
            "WAIT_REJOIN_ESTIMATE",
            ("PIT_LOSS_CALIBRATION_REQUIRED_FOR_REJOIN_ESTIMATE",),
        )
    elif traffic.get("motion_context") is None:
        capabilities["traffic_data"] = _capability(
            "WAIT_REJOIN_ESTIMATE", ("REJOIN_ESTIMATOR_REQUIRED",)
        )
    else:
        capabilities["traffic_data"] = _capability(
            "WAIT_REJOIN_ESTIMATE", ("ACTION_BOUND_REJOIN_ESTIMATE_REQUIRED",)
        )

    required_passes = {
        "event_rules_identity": "PASS_VERIFIED_OFFICIAL_EXACT_MATCH",
        "pit_loss_calibration": "PASS_CALIBRATED",
        "service_labels": "PASS_SERVICE_LABELS",
        "traffic_data": "PASS_TRAFFIC_DATA",
        "pit_open_and_penalty_state": "PASS_PIT_OPEN_AND_PENALTY_STATE",
        "strategy_data": "PASS_COMMON_ONE_STOP_PLAN",
    }
    hard_gates_pass = all(
        _mapping(capabilities[name], f"capability {name}")["status"] == status
        for name, status in required_passes.items()
    ) and str(
        _mapping(capabilities["one_more_lap"], "one-more capability")["status"]
    ).startswith(
        ("PASS_", "NOT_APPLICABLE")
    ) and (
        context_version == CONTEXT_CONTRACT_VERSION
        or _mapping(capabilities["tire_strategy"], "tire capability")["status"]
        in {
            "PASS_RULE_MANDATED_TIRE_CHANGE",
            "PASS_MODEL_SELECTED_TIRE_CHANGE",
        }
    )

    previous = _validate_previous(
        previous_receipt_value,
        expected_previous_receipt_sha256=expected_previous_receipt_sha256,
        expected_previous_revision=expected_previous_revision,
        source_binding=source_binding,
        observation=observation,
    )
    previous_lifecycle = (
        _mapping(previous["lifecycle"], "previous lifecycle")
        if previous is not None
        else None
    )
    previous_id = (
        previous_lifecycle.get("active_recommendation_id")
        if previous_lifecycle is not None
        else None
    )

    recommendations: list[dict[str, object]] = []
    candidate_id: str | None = None
    if hard_gates_pass:
        assert branch_plan is not None
        assert provisional_action is not None
        assert official_rules is not None
        action = provisional_action
        exact_rejoin_estimate_sha256 = (
            action_rejoin_estimate["estimate_sha256"]
            if action_traffic_estimate_available
            and action_rejoin_estimate is not None
            else None
        )
        if exact_rejoin_estimate_sha256 is None:  # pragma: no cover - gate invariant
            raise AssertionError("passing traffic gate lacks a rejoin estimate")
        rejoin_estimate_semantic_sha256 = (
            _rejoin_semantic_sha256(action_rejoin_estimate)
            if action_rejoin_estimate is not None
            else _traffic_semantic_sha256(traffic)
        )
        semantic_basis = {
                "action": action,
                "calibration_model_sha256": context["_calibration_sha256"],
                "event_identity_sha256": identity_sha,
                "fuel_model_semantic_sha256": fuel["model_semantic_sha256"],
                "horizon_sha256": horizon["horizon_sha256"],
                "rules_lineage_sha256": rules_binding["rules_lineage_sha256"],
                "rejoin_estimate_semantic_sha256": (
                    rejoin_estimate_semantic_sha256
                ),
                "session_id": source_binding["session_id"],
                "source_id": source_binding["source_id"],
                "strategy_policy_sha256": canonical_sha256(policy),
                # Freshness is enforced against the current decision tick by
                # the context validator.  Excluding only that tick and the
                # envelope self hash keeps an unchanged traffic estimate from
                # spuriously creating a new recommendation ID every frame.
                "traffic_semantic_sha256": _traffic_semantic_sha256(traffic),
        }
        if tire_strategy is not None:
            semantic_basis["tire_strategy_semantic_sha256"] = (
                _tire_strategy_semantic_sha256(tire_strategy)
            )
        candidate_id = f"m2-strategy:{canonical_sha256(semantic_basis)}"
        evidence_ids = [
            f"fuel-replay:{fuel['fuel_replay_sha256']}",
            f"m1-pit-stint:{m1['pit_stint_receipt_sha256']}",
            f"strategy-context:{context['context_sha256']}",
            f"rules-profile:{rules_binding['profile_sha256']}",
            f"rejoin-estimate:{exact_rejoin_estimate_sha256}",
        ]
        if tire_strategy is not None:
            evidence_ids.append(
                f"tire-strategy:{canonical_sha256(tire_strategy)}"
            )
        recommendation = {
                "action": action,
                "claim_scope": "GATED_OFFLINE_CONTRACT_CANDIDATE",
                "confidence": "LOW",
                "evidence_ids": evidence_ids,
                "executable": False,
                "expected_gain_range_s": None,
                "kind": "M2_STRATEGY_CANDIDATE",
                "practice_only": False,
                "reason": "Latest common fuel-feasible lap across every admitted horizon branch.",
                "recommendation_basis": semantic_basis,
                "recommendation_id": candidate_id,
                "risk": [
                    "NOT_M2_ACCEPTED",
                    "NOT_LIVE_PROVEN",
                    "NOT_R7_ATTESTED",
                    "SHADOW_ONLY",
                ],
                "status": "SHADOW_ONLY",
                "supersedes_id": previous_id if previous_id != candidate_id else None,
                "valid_until": {
                    "context_sha256": context["context_sha256"],
                    "pit_entry_deadline_laps_completed": int(
                        observation["laps_completed"]
                    )
                    + int(branch_plan["recommended_lap_from_now"]),
                    "recompute_after_decision_tick": int(observation["decision_tick"])
                    + 1,
                    "rules_lineage_sha256": rules_binding["rules_lineage_sha256"],
                    "session_epoch": observation["session_epoch"],
                    "source_epoch": observation["source_epoch"],
                },
        }
        recommendations = [recommendation]

    wait_statuses = [
        str(_mapping(value, f"capability {name}")["status"])
        for name, value in sorted(capabilities.items())
        if not str(_mapping(value, f"capability {name}")["status"]).startswith(
            ("PASS_", "NOT_APPLICABLE")
        )
    ]
    if not hard_gates_pass:
        recommendations = []
        candidate_id = None
    capabilities["race_recommendation"] = _capability(
        "PASS_SHADOW_CONTRACT" if recommendations else "BLOCKED",
        (
            ()
            if recommendations
            else tuple(wait_statuses or ("NO_ADMISSIBLE_STRATEGY_CANDIDATE",))
        ),
    )
    capabilities["lifecycle"] = _capability("PASS_OPTIMISTIC_CONCURRENCY")

    lifecycle_events: list[dict[str, object]] = []
    revoke_reasons = wait_statuses or ["STRATEGY_BASIS_CHANGED"]
    if previous_id is not None and previous_id != candidate_id:
        lifecycle_events.append(
            {
                "event": "REVOKE",
                "reason_codes": list(dict.fromkeys(revoke_reasons)),
                "recommendation_id": previous_id,
            }
        )
    if candidate_id is not None and candidate_id != previous_id:
        lifecycle_events.append(
            {
                "event": "ISSUE",
                "recommendation_id": candidate_id,
                "supersedes_id": previous_id,
            }
        )
    elif candidate_id is not None and candidate_id == previous_id:
        lifecycle_events.append(
            {
                "event": "NO_CHANGE",
                "recommendation_id": candidate_id,
                "reason_codes": ["ACTIVE_STRATEGY_UNCHANGED"],
            }
        )
    elif previous_id is None:
        lifecycle_events.append(
            {
                "event": "NO_CHANGE",
                "recommendation_id": None,
                "reason_codes": ["NO_ACTIVE_RECOMMENDATION"],
            }
        )

    previous_sha = (
        previous["m2_strategy_receipt_sha256"] if previous is not None else None
    )
    revision = (
        int(_mapping(previous["lifecycle"], "previous lifecycle")["state_revision"]) + 1
        if previous is not None
        else 1
    )
    lifecycle = {
        "active_recommendation_id": candidate_id,
        "events": lifecycle_events,
        "observation_point": {
            "decision_tick": observation["decision_tick"],
            "session_epoch": observation["session_epoch"],
            "source_epoch": observation["source_epoch"],
        },
        "previous_state_sha256": previous_sha,
        "state_revision": revision,
    }

    input_base = {
        "context_sha256": context["context_sha256"],
        "event_receipt_sha256": source_binding["event_receipt_sha256"],
        "fuel_replay_sha256": fuel["fuel_replay_sha256"],
        "m1_receipt_sha256": m1["pit_stint_receipt_sha256"],
        "model_semantic_sha256": fuel["model_semantic_sha256"],
        "normalized_samples_sha256": source_binding["normalized_samples_sha256"],
        "sample_count": source_binding["sample_count"],
        "session_id": source_binding["session_id"],
        "source_id": source_binding["source_id"],
        "source_kind": source_binding["source_kind"],
        "source_sha256": source_binding["source_sha256"],
    }
    input_binding = {
        **input_base,
        "input_lineage_sha256": canonical_sha256(input_base),
    }
    quality_reasons = wait_statuses
    quality_gate = {
        "reason_codes": quality_reasons,
        "status": "PASS_SHADOW_CONTRACT" if recommendations else "WAIT_CAPABILITIES",
    }
    binding: dict[str, object] = {
        "advisor_only": True,
        "attestation_status": "NOT_R7_ATTESTED",
        "calibration": {
            "calibrated_model": calibration_model,
            "observed_m1": observed_calibration,
        },
        "capabilities": capabilities,
        "contract_version": (
            CONTRACT_V2_VERSION
            if context_version == CONTEXT_V2_CONTRACT_VERSION
            else CONTRACT_VERSION
        ),
        "derivation_status": "POST_ADMISSION_PACKAGE_EXTERNAL",
        "event_identity": {
            **identity,
            "identity_sha256": identity_sha,
        },
        "execution_mode": "SHADOW_ONLY",
        "horizon": horizon,
        "input_binding": input_binding,
        "lifecycle": lifecycle,
        "quality_gate": quality_gate,
        "recommendations": recommendations,
        "rules_binding": rules_binding,
        "strategy_context": {
            key: context[key]
            for key in _context_keys_for_version(context_version)
        },
        "strategy_policy": dict(policy),
        "traffic_rejoin": {
            "estimate": action_rejoin_estimate,
            "input": traffic,
            "status": (
                "PASS_TRAFFIC_DATA"
                if traffic_estimate_available
                else "WAIT_REJOIN_ESTIMATE"
                if traffic is not None
                else "WAIT_TRAFFIC_DATA"
            ),
        },
        "vehicle_context": dict(vehicle),
    }
    if tire_strategy is not None:
        binding["tire_strategy"] = tire_strategy
    return {
        **binding,
        "m2_strategy_receipt_sha256": canonical_sha256(binding),
    }


def _reject_constant(value: str) -> NoReturn:
    _fail("INPUT_JSON_INVALID", f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("INPUT_JSON_INVALID", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_load(path: Path, *, label: str) -> dict[str, object]:
    try:
        if path.stat().st_size > MAX_INPUT_BYTES:
            _fail("INPUT_TOO_LARGE", f"{label} exceeds {MAX_INPUT_BYTES} bytes")
        text = path.read_text(encoding="utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except M2StrategyReceiptError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M2StrategyReceiptError(
            "INPUT_READ_FAILED", f"cannot read {label}: {exc}"
        ) from exc
    return _mapping(value, label)


def _exclusive_write(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise M2StrategyReceiptError(
            "OUTPUT_CREATE_FAILED", f"cannot create output exclusively: {exc}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fuel_replay", type=Path)
    parser.add_argument("m1_receipt", type=Path)
    parser.add_argument("strategy_context", type=Path)
    parser.add_argument("--expected-fuel-replay-sha256", required=True)
    parser.add_argument("--expected-m1-receipt-sha256", required=True)
    parser.add_argument("--expected-strategy-context-sha256", required=True)
    parser.add_argument("--rules-profile", type=Path)
    parser.add_argument("--expected-rules-profile-sha256")
    parser.add_argument("--expected-rules-source-sha256")
    parser.add_argument("--previous-receipt", type=Path)
    parser.add_argument("--expected-previous-receipt-sha256")
    parser.add_argument("--expected-previous-revision", type=int)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        fuel = _strict_json_load(args.fuel_replay, label="fuel replay")
        m1 = _strict_json_load(args.m1_receipt, label="M1 receipt")
        context = _strict_json_load(args.strategy_context, label="strategy context")
        rules = (
            _strict_json_load(args.rules_profile, label="rules profile")
            if args.rules_profile is not None
            else None
        )
        previous = (
            _strict_json_load(args.previous_receipt, label="previous M2 receipt")
            if args.previous_receipt is not None
            else None
        )
        receipt = build_m2_strategy_receipt(
            fuel,
            m1,
            context,
            expected_fuel_replay_sha256=args.expected_fuel_replay_sha256,
            expected_m1_receipt_sha256=args.expected_m1_receipt_sha256,
            expected_strategy_context_sha256=args.expected_strategy_context_sha256,
            rules_profile_value=rules,
            expected_rules_profile_sha256=args.expected_rules_profile_sha256,
            expected_rules_source_sha256=args.expected_rules_source_sha256,
            previous_receipt_value=previous,
            expected_previous_receipt_sha256=args.expected_previous_receipt_sha256,
            expected_previous_revision=args.expected_previous_revision,
        )
        encoded = _canonical_json(receipt, newline=True)
        if args.output is not None:
            _exclusive_write(args.output, encoded)
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0 if receipt["quality_gate"]["status"] == "PASS_SHADOW_CONTRACT" else 5  # type: ignore[index]
    except M2StrategyReceiptError as exc:
        error = {
            "contract_version": CONTRACT_VERSION,
            "error": str(exc),
            "status": "FAIL",
        }
        print(f"{exc.code}: {json.dumps(error, sort_keys=True)}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
