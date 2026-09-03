"""Build a fail-closed, package-external pit-plan shadow receipt.

The input is a complete ``fuel-model-replay-v2`` JSON artifact that has
already traversed the shared telemetry, event, lap, and fuel pipeline.  This
script revalidates the artifact's intrinsic receipts against an independently
supplied expected replay digest, then applies an optional versioned event-rules
profile.

The current implementation intentionally supports only an explicitly marked
``DEVELOPMENT_SMOKE`` rules profile.  Missing or unknown real event rules
produce ``WAIT_EVENT_RULES`` and no recommendation.  Even the smoke path is
``FUEL_FEASIBILITY_ONLY``, ``SHADOW_ONLY``, and non-executable; traffic and
rejoin claims remain unavailable.  The output is a post-admission derived
artifact and is not part of the frozen r7 Windows/Mac trust chain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn

PIT_PLAN_CONTRACT_VERSION = "offline-pit-plan-v1"
RULES_CONTRACT_VERSION = "endurance-event-rules-v1"
FUEL_REPLAY_CONTRACT_VERSION = "fuel-model-replay-v2"
EVENT_RECEIPT_CONTRACT_VERSION = "telemetry-events-v1"
INFERENCE_CAPABILITY_CONTRACT_VERSION = "inference-capability-v1"
NORMALIZED_TELEMETRY_CONTRACT_VERSION = "normalized-telemetry-v3"
NORMALIZATION_PROFILE_VERSION = "normalized-sdk-adapter-v3"
FUEL_FEATURE_PIPELINE_VERSION = "normalized-lap-fuel-v1"
FUEL_MODEL_VERSION = "fuel-strategy-v1"
LAP_ALGORITHM_VERSION = "distance-wrap-v2"
COLLECTOR_CONTRACT_VERSION = "live-collector-v2"
MAX_INPUT_BYTES = 16 * 1024 * 1024

_SHA256_CHARS = frozenset("0123456789abcdef")
_FUEL_REPLAY_BINDING_KEYS = (
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
_FUEL_REPLAY_KEYS = frozenset((*_FUEL_REPLAY_BINDING_KEYS, "fuel_replay_sha256"))
_PIT_PLAN_BINDING_KEYS = (
    "advisor_only",
    "attestation_status",
    "capabilities",
    "contract_version",
    "derivation_status",
    "execution_mode",
    "input_binding",
    "lifecycle_events",
    "plan_scope",
    "quality_gate",
    "recommendations",
    "rules_binding",
    "service_alternatives",
)
_PIT_PLAN_KEYS = frozenset((*_PIT_PLAN_BINDING_KEYS, "pit_plan_sha256"))
_EVENT_RECEIPT_KEYS = frozenset(
    {
        "accepted_sample_count",
        "config_sha256",
        "contract_version",
        "event_count",
        "event_kind_counts",
        "events_sha256",
        "receipt_sha256",
        "rejected_sample_count",
        "sample_count",
        "session_epoch_count",
        "source_epoch_count",
    }
)
_PIPELINE_KEYS = frozenset(
    {
        "config_sha256",
        "event_contract_version",
        "feature_pipeline_version",
        "fuel_model_version",
        "lap_algorithm_version",
        "normalization",
        "normalized_telemetry_contract_version",
        "tick_rate_hz",
    }
)
_NORMALIZATION_KEYS = frozenset({"opponent_error_policy", "profile_version", "stale_after_us"})
_SCENARIO_REQUIRED_KEYS = frozenset(
    {
        "conservative_quantile",
        "current_fuel_l",
        "minimum_valid_laps",
        "refuel_rate_l_per_s",
        "reserve_l",
        "tank_capacity_l",
        "timed_race_extra_laps",
    }
)
_MODEL_KEYS = frozenset(
    {
        "burn",
        "conservative_fuel_to_end_l",
        "cumulative_refuel_time_to_end_s",
        "cumulative_refuel_to_end_l",
        "current_fuel_l",
        "mean_fuel_to_end_l",
        "minimum_pit_stops",
        "next_pit_window",
        "reason_codes",
        "rejection_counts",
        "remaining_laps",
        "safe_laps_on_current_fuel",
        "safe_laps_on_full_tank",
        "status",
    }
)
_BURN_KEYS = frozenset(
    {
        "accepted_laps",
        "coefficient_of_variation",
        "confidence",
        "conservative_l_per_lap",
        "conservative_quantile",
        "label",
        "maximum_l_per_lap",
        "mean_l_per_lap",
        "minimum_l_per_lap",
        "rejected_laps",
        "source_label",
        "standard_deviation_l_per_lap",
    }
)
_LAP_RECEIPT_KEYS = frozenset(
    {
        "algorithm_version",
        "fuel_eligible_lap_count",
        "lap_count",
        "laps_sha256",
        "modeled_sample_count",
        "quality_complete_lap_count",
        "structurally_complete_lap_count",
    }
)
_NORMALIZED_RECEIPT_KEYS = frozenset({"contract_version", "sample_count", "samples_sha256"})
_SERIES_EVIDENCE_KEYS = frozenset(
    {
        "degraded_sample_count",
        "missing_channel_sample_counts",
        "modeled_sample_count",
        "normalized_dropped_tick_count",
        "quality_issue_counts",
        "segmentation_error",
    }
)
_FUEL_CHANNELS = frozenset(
    {
        "FuelLevel",
        "Lap",
        "LapCompleted",
        "LapDistPct",
        "OnPitRoad",
        "PlayerCarInPitStall",
        "PlayerTrackSurface",
        "SessionTick",
        "SessionTime",
        "Speed",
    }
)
_UPSTREAM_CAPABILITY_KEYS = frozenset(
    {
        "current_tire_wear",
        "fuel_model_shadow",
        "opponent_fuel",
        "race_recommendation",
        "traffic_model",
    }
)
_UPSTREAM_RECOMMENDATION_KEYS = frozenset(
    {
        "action",
        "claim_level",
        "confidence",
        "confidence_basis",
        "evidence_ids",
        "executable",
        "kind",
        "practice_only",
        "recommendation_id",
        "scenario_sha256",
        "status",
    }
)
_IBT_EVIDENCE_KEYS = frozenset(
    {
        "authenticity_status",
        "byte_size",
        "completion_status",
        "record_count",
        "session_id",
        "source_id",
        "source_kind",
        "source_sha256",
        "tick_rate_hz",
    }
)
_COLLECTOR_COUNT_KEYS = frozenset(
    {
        "capture_clock_regression_count",
        "driver_info_key_count",
        "dropped_tick_count",
        "duplicate_conflict_count",
        "duplicate_sample_count",
        "event_record_count",
        "frame_record_count",
        "read_error_field_count",
        "read_error_frame_count",
        "redacted_driver_info_path_count",
        "samples_seen",
        "schema_change_count",
        "schema_epoch_count",
        "schema_record_count",
        "semantic_record_count",
        "session_epoch_count",
        "session_info_record_count",
        "session_reset_count",
        "stale_event_count",
    }
)
_COLLECTOR_EVIDENCE_KEYS = _COLLECTOR_COUNT_KEYS | frozenset(
    {
        "authenticity_status",
        "capture_span_us",
        "collector_contract_version",
        "completion_status",
        "first_buffer_tick",
        "first_capture_monotonic_us",
        "last_buffer_tick",
        "last_capture_monotonic_us",
        "records_sha256",
        "session_id",
        "session_info_scope_counts",
        "sim_mode",
        "source_id",
        "source_kind",
        "tick_rate_hz_values",
    }
)
_INPUT_BINDING_KEYS = frozenset(
    {
        "event_receipt_sha256",
        "expected_fuel_replay_sha256",
        "fuel_replay_contract_version",
        "fuel_replay_sha256",
        "input_evidence_sha256",
        "input_kind",
        "input_lineage_sha256",
        "model_output_sha256",
        "model_semantic_sha256",
        "normalized_input_contract_version",
        "normalized_sample_count",
        "normalized_samples_sha256",
        "scenario_sha256",
        "session_id",
        "source_id",
        "source_kind",
    }
)
_RULES_BINDING_KEYS = frozenset(
    {
        "official_event_rules",
        "profile_id",
        "profile_status",
        "rules_lineage_sha256",
        "rules_sha256",
    }
)
_PLAN_RECOMMENDATION_KEYS = frozenset(
    {
        "action",
        "alternatives",
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
        "alternative_id",
        "evidence_ids",
        "fuel_add_l",
        "input_lineage_sha256",
        "model_semantic_sha256",
        "normalized_samples_sha256",
        "recommended_lap_from_now",
        "rules_lineage_sha256",
        "rules_sha256",
        "scenario_sha256",
        "session_id",
        "source_id",
        "source_kind",
    }
)
_SERVICE_ALTERNATIVE_KEYS = frozenset(
    {
        "alternative_id",
        "change_tires",
        "fuel_add_l",
        "fuel_mode",
        "fuel_service_time_s",
        "pit_lane_loss_s",
        "provenance",
        "service_timing",
        "stationary_service_time_s",
        "tire_service_time_s",
        "total_pit_loss_s",
    }
)
_RECOMMENDATION_REASON = (
    "Latest fuel-feasible lap under an unbound non-official DEVELOPMENT_SMOKE rules profile."
)
_RULES_KEYS = frozenset(
    {
        "contract_version",
        "official_event_rules",
        "profile_id",
        "profile_status",
        "provenance",
        "scope",
        "selection_policy",
        "service_rules",
    }
)
_SCOPE_KEYS = frozenset({"car", "event", "track"})
_SERVICE_RULE_KEYS = frozenset(
    {
        "fuel_tire_service_timing",
        "no_tire_service_allowed",
        "pit_lane_loss_s",
        "refuel_rate_l_per_s",
        "tank_capacity_l",
        "tire_change_required",
        "tire_change_time_s",
    }
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
_VALID_UNTIL_PREDICATES = (
    "NEXT_LAP_COMPLETED",
    "FUEL_OBSERVATION_CHANGED",
    "PIT_STATE_CHANGED",
    "EVENT_RULES_PROFILE_CHANGED",
    "SOURCE_STALE_RESET_OR_SCHEMA_CHANGED",
)


class PitPlanError(ValueError):
    """Raised when an input cannot support an honest pit-plan receipt."""


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
        raise PitPlanError("value is not canonical-JSON-safe") from exc


def canonical_sha256(value: object) -> str:
    """Return the canonical digest used by this post-processor."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise PitPlanError(f"{name} must be a plain object")
    return value


def _exact_mapping(value: object, keys: frozenset[str], name: str) -> dict[str, Any]:
    result = _mapping(value, name)
    if set(result) != keys:
        raise PitPlanError(f"{name} keys are invalid")
    return result


def _list(value: object, name: str) -> list[Any]:
    if type(value) is not list:
        raise PitPlanError(f"{name} must be an array")
    return value


def _identifier(value: object, name: str, *, maximum_bytes: int = 256) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum_bytes
        or any(ord(character) < 32 for character in value)
    ):
        raise PitPlanError(f"{name} must be a non-empty plain string")
    return value


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _SHA256_CHARS for character in value)
    ):
        raise PitPlanError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _plain_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise PitPlanError(f"{name} must be an integer >= {minimum}")
    return value


def _finite_number(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PitPlanError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise PitPlanError(f"{name} must be finite")
    if positive and number <= 0.0:
        raise PitPlanError(f"{name} must be positive")
    if minimum is not None and number < minimum:
        raise PitPlanError(f"{name} must be >= {minimum}")
    return number


def _close(left: float, right: float, name: str, *, tolerance: float = 1e-6) -> None:
    if not math.isclose(left, right, rel_tol=1e-9, abs_tol=tolerance):
        raise PitPlanError(f"{name} does not close")


def _optional_plain_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _plain_int(value, name)


def _unique_codes(value: object, name: str) -> list[str]:
    items = _list(value, name)
    normalized = [_identifier(item, f"{name} item", maximum_bytes=128) for item in items]
    if len(normalized) != len(set(normalized)):
        raise PitPlanError(f"{name} must contain unique values")
    return normalized


def _strict_json_load(path: Path, *, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PitPlanError(f"{label} must be a regular file")
        if metadata.st_size <= 0 or metadata.st_size > MAX_INPUT_BYTES:
            raise PitPlanError(f"{label} size is outside the accepted range")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) != metadata.st_size:
            raise PitPlanError(f"{label} changed or could not be read completely")
    finally:
        os.close(descriptor)

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PitPlanError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda value: _raise_nonfinite(label, value),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PitPlanError(f"{label} is not strict UTF-8 JSON") from exc
    return _mapping(payload, label)


def _raise_nonfinite(label: str, value: str) -> NoReturn:
    raise PitPlanError(f"{label} contains non-finite JSON value {value}")


def _validate_unavailable_capability(value: object, name: str) -> dict[str, Any]:
    capability = _mapping(value, name)
    if set(capability) != _UNAVAILABLE_CAPABILITY_KEYS:
        raise PitPlanError(f"{name} keys are invalid")
    if (
        capability.get("contract_version") != INFERENCE_CAPABILITY_CONTRACT_VERSION
        or capability.get("status") != "SKIP"
        or capability.get("estimate_available") is not False
        or capability.get("confidence") != "NONE"
        or capability.get("provenance") != "UNKNOWN"
    ):
        raise PitPlanError(f"{name} must remain explicitly unavailable")
    if not _unique_codes(capability.get("reasons"), f"{name}.reasons"):
        raise PitPlanError(f"{name} must contain at least one reason")
    if not _unique_codes(capability.get("blocked_claims"), f"{name}.blocked_claims"):
        raise PitPlanError(f"{name} must block at least one claim")
    return capability


def _validate_event_receipt(value: object) -> dict[str, Any]:
    receipt = _mapping(value, "event_receipt")
    if set(receipt) != _EVENT_RECEIPT_KEYS:
        raise PitPlanError("event_receipt keys are invalid")
    if receipt.get("contract_version") != EVENT_RECEIPT_CONTRACT_VERSION:
        raise PitPlanError("event_receipt contract_version is unsupported")
    for name in (
        "sample_count",
        "accepted_sample_count",
        "rejected_sample_count",
        "event_count",
        "source_epoch_count",
        "session_epoch_count",
    ):
        _plain_int(receipt.get(name), f"event_receipt.{name}")
    if (
        receipt["accepted_sample_count"] + receipt["rejected_sample_count"]
        != receipt["sample_count"]
    ):
        raise PitPlanError("event_receipt sample counts do not close")
    counts = _mapping(receipt.get("event_kind_counts"), "event_kind_counts")
    if any(
        type(key) is not str or type(count) is not int or count < 0 for key, count in counts.items()
    ):
        raise PitPlanError("event_kind_counts are invalid")
    if sum(counts.values()) != receipt["event_count"]:
        raise PitPlanError("event_kind_counts do not close to event_count")
    for name in ("config_sha256", "events_sha256", "receipt_sha256"):
        _sha256(receipt.get(name), f"event_receipt.{name}")
    receipt_binding = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if canonical_sha256(receipt_binding) != receipt["receipt_sha256"]:
        raise PitPlanError("event_receipt receipt_sha256 mismatch")
    return receipt


def _validate_pipeline(value: object) -> tuple[dict[str, Any], int]:
    pipeline = _exact_mapping(value, _PIPELINE_KEYS, "pipeline")
    expected = {
        "event_contract_version": EVENT_RECEIPT_CONTRACT_VERSION,
        "feature_pipeline_version": FUEL_FEATURE_PIPELINE_VERSION,
        "fuel_model_version": FUEL_MODEL_VERSION,
        "lap_algorithm_version": LAP_ALGORITHM_VERSION,
        "normalized_telemetry_contract_version": NORMALIZED_TELEMETRY_CONTRACT_VERSION,
    }
    if any(pipeline[key] != expected_value for key, expected_value in expected.items()):
        raise PitPlanError("pipeline contract values are unsupported")
    tick_rate = _plain_int(pipeline.get("tick_rate_hz"), "pipeline.tick_rate_hz", minimum=1)
    if tick_rate > 360:
        raise PitPlanError("pipeline.tick_rate_hz must not exceed 360")
    normalization = _exact_mapping(
        pipeline.get("normalization"), _NORMALIZATION_KEYS, "pipeline.normalization"
    )
    if (
        normalization.get("opponent_error_policy") != "degrade"
        or normalization.get("profile_version") != NORMALIZATION_PROFILE_VERSION
    ):
        raise PitPlanError("pipeline normalization values are unsupported")
    _plain_int(
        normalization.get("stale_after_us"), "pipeline.normalization.stale_after_us", minimum=1
    )
    claimed = _sha256(pipeline.get("config_sha256"), "pipeline.config_sha256")
    if (
        canonical_sha256({key: item for key, item in pipeline.items() if key != "config_sha256"})
        != claimed
    ):
        raise PitPlanError("pipeline config_sha256 mismatch")
    return pipeline, tick_rate


def _validate_input_evidence(
    value: object,
    *,
    input_kind: str,
    tick_rate_hz: int,
) -> dict[str, Any]:
    keys = _IBT_EVIDENCE_KEYS if input_kind == "ibt" else _COLLECTOR_EVIDENCE_KEYS
    evidence = _exact_mapping(value, keys, "input_evidence")
    _identifier(evidence.get("source_id"), "input_evidence.source_id")
    _identifier(evidence.get("session_id"), "input_evidence.session_id")
    if evidence.get("completion_status") != "COMPLETE":
        raise PitPlanError("input_evidence must be COMPLETE")
    if input_kind == "ibt":
        if (
            evidence.get("source_kind") != "IBT_OFFLINE"
            or evidence.get("authenticity_status") != "HASHED_LOCAL_FILE_NOT_AUTHENTICATED"
        ):
            raise PitPlanError("IBT input_evidence identity or authenticity is invalid")
        _plain_int(evidence.get("byte_size"), "input_evidence.byte_size", minimum=1)
        _plain_int(evidence.get("record_count"), "input_evidence.record_count", minimum=1)
        if (
            _plain_int(evidence.get("tick_rate_hz"), "input_evidence.tick_rate_hz", minimum=1)
            != tick_rate_hz
        ):
            raise PitPlanError("input_evidence tick rate does not match pipeline")
        _sha256(evidence.get("source_sha256"), "input_evidence.source_sha256")
        return evidence

    source_kind = evidence.get("source_kind")
    expected_mode = {"SDK_LIVE": "full", "REPLAY_SDK_PROXY": "replay"}.get(source_kind)
    if (
        expected_mode is None
        or evidence.get("sim_mode") != expected_mode
        or evidence.get("authenticity_status") != "SELF_CONSISTENT_NOT_AUTHENTICATED"
        or evidence.get("collector_contract_version") != COLLECTOR_CONTRACT_VERSION
    ):
        raise PitPlanError("collector input_evidence identity or contract is invalid")
    for key in _COLLECTOR_COUNT_KEYS:
        _plain_int(evidence.get(key), f"input_evidence.{key}")
    if evidence["frame_record_count"] < 1:
        raise PitPlanError("collector input_evidence must contain frames")
    _sha256(evidence.get("records_sha256"), "input_evidence.records_sha256")
    rates = _list(evidence.get("tick_rate_hz_values"), "input_evidence.tick_rate_hz_values")
    if rates != [tick_rate_hz]:
        raise PitPlanError("collector input_evidence tick rates do not match pipeline")
    first_tick = _optional_plain_int(
        evidence.get("first_buffer_tick"), "input_evidence.first_buffer_tick"
    )
    last_tick = _optional_plain_int(
        evidence.get("last_buffer_tick"), "input_evidence.last_buffer_tick"
    )
    first_capture = _optional_plain_int(
        evidence.get("first_capture_monotonic_us"), "input_evidence.first_capture_monotonic_us"
    )
    last_capture = _optional_plain_int(
        evidence.get("last_capture_monotonic_us"), "input_evidence.last_capture_monotonic_us"
    )
    if (first_tick is None) != (last_tick is None) or (first_capture is None) != (
        last_capture is None
    ):
        raise PitPlanError("collector input_evidence bounds are inconsistent")
    span = evidence.get("capture_span_us")
    if span is not None:
        measured = _plain_int(span, "input_evidence.capture_span_us")
        if (
            first_capture is None
            or last_capture is None
            or measured != last_capture - first_capture
        ):
            raise PitPlanError("collector input_evidence capture span is inconsistent")
    scopes = _mapping(
        evidence.get("session_info_scope_counts"), "input_evidence.session_info_scope_counts"
    )
    if set(scopes) - {"FULL", "PARTIAL", "UNAVAILABLE"}:
        raise PitPlanError("collector session_info_scope_counts keys are invalid")
    for key, count in scopes.items():
        _plain_int(count, f"input_evidence.session_info_scope_counts.{key}")
    return evidence


def _validate_scenario(
    value: object,
) -> tuple[dict[str, Any], dict[str, float], dict[str, str]]:
    scenario = _mapping(value, "scenario")
    allowed = _SCENARIO_REQUIRED_KEYS | {
        "remaining_laps",
        "remaining_time_s",
        "reference_lap_time_s",
    }
    if not _SCENARIO_REQUIRED_KEYS.issubset(scenario) or set(scenario) - allowed:
        raise PitPlanError("scenario keys are invalid")
    if ("remaining_laps" in scenario) == ("remaining_time_s" in scenario):
        raise PitPlanError("scenario must carry exactly one race-distance input")
    provenance_by_field: dict[str, str] = {}
    values: dict[str, float] = {}
    integer_fields = {"minimum_valid_laps", "remaining_laps", "timed_race_extra_laps"}
    positive_fields = {"refuel_rate_l_per_s", "reference_lap_time_s", "tank_capacity_l"}
    for name, raw in scenario.items():
        field = _exact_mapping(raw, frozenset({"provenance", "value"}), f"scenario.{name}")
        field_provenance = _identifier(field.get("provenance"), f"scenario.{name}.provenance")
        if field_provenance not in {"USER_RULE", "SDK_DIRECT"}:
            raise PitPlanError(f"scenario.{name}.provenance is invalid")
        provenance_by_field[name] = field_provenance
        if name in integer_fields:
            minimum = 2 if name == "minimum_valid_laps" else 0
            values[name] = float(
                _plain_int(field.get("value"), f"scenario.{name}.value", minimum=minimum)
            )
        else:
            values[name] = _finite_number(
                field.get("value"),
                f"scenario.{name}.value",
                minimum=0.0,
                positive=name in positive_fields,
            )
    if not 0.5 <= values["conservative_quantile"] <= 1.0:
        raise PitPlanError("scenario conservative_quantile is outside [0.5, 1]")
    if values["current_fuel_l"] > values["tank_capacity_l"]:
        raise PitPlanError("scenario current fuel exceeds tank capacity")
    if values["reserve_l"] >= values["tank_capacity_l"]:
        raise PitPlanError("scenario reserve must be below tank capacity")
    return scenario, values, provenance_by_field


def _labeled_number(
    model: Mapping[str, object],
    name: str,
    *,
    unit: str,
    label: str,
    integer: bool = False,
) -> float:
    field = _mapping(model.get(name), f"model_output.{name}")
    if set(field) != {"label", "unit", "value"}:
        raise PitPlanError(f"model_output.{name} keys are invalid")
    if field.get("unit") != unit:
        raise PitPlanError(f"model_output.{name} unit mismatch")
    if field.get("label") != label:
        raise PitPlanError(f"model_output.{name} label mismatch")
    if integer:
        return float(_plain_int(field.get("value"), f"model_output.{name}.value"))
    return _finite_number(field.get("value"), f"model_output.{name}.value", minimum=0.0)


def _validate_lap_receipt(value: object) -> dict[str, Any]:
    receipt = _exact_mapping(value, _LAP_RECEIPT_KEYS, "lap_receipt")
    if receipt.get("algorithm_version") != LAP_ALGORITHM_VERSION:
        raise PitPlanError("lap_receipt algorithm_version is unsupported")
    counts = {
        key: _plain_int(receipt.get(key), f"lap_receipt.{key}")
        for key in _LAP_RECEIPT_KEYS
        if key not in {"algorithm_version", "laps_sha256"}
    }
    _sha256(receipt.get("laps_sha256"), "lap_receipt.laps_sha256")
    if not (
        counts["fuel_eligible_lap_count"]
        <= counts["quality_complete_lap_count"]
        <= counts["structurally_complete_lap_count"]
        <= counts["lap_count"]
    ):
        raise PitPlanError("lap_receipt lap counts are inconsistent")
    return receipt


def _validate_normalized_receipt(value: object) -> dict[str, Any]:
    receipt = _exact_mapping(value, _NORMALIZED_RECEIPT_KEYS, "normalized_input_receipt")
    if receipt.get("contract_version") != NORMALIZED_TELEMETRY_CONTRACT_VERSION:
        raise PitPlanError("normalized input contract is unsupported")
    _plain_int(receipt.get("sample_count"), "normalized_input_receipt.sample_count", minimum=1)
    _sha256(receipt.get("samples_sha256"), "normalized_input_receipt.samples_sha256")
    return receipt


def _count_mapping(value: object, name: str) -> dict[str, Any]:
    result = _mapping(value, name)
    for key, count in result.items():
        _identifier(key, f"{name} key", maximum_bytes=128)
        _plain_int(count, f"{name}.{key}")
    return result


def _validate_series_evidence(value: object) -> dict[str, Any]:
    series = _exact_mapping(value, _SERIES_EVIDENCE_KEYS, "series_evidence")
    for name in ("degraded_sample_count", "modeled_sample_count", "normalized_dropped_tick_count"):
        _plain_int(series.get(name), f"series_evidence.{name}")
    missing = _mapping(
        series.get("missing_channel_sample_counts"), "series_evidence.missing_channel_sample_counts"
    )
    if set(missing) != _FUEL_CHANNELS:
        raise PitPlanError("series_evidence missing-channel keys are invalid")
    _count_mapping(missing, "series_evidence.missing_channel_sample_counts")
    _count_mapping(series.get("quality_issue_counts"), "series_evidence.quality_issue_counts")
    if series.get("segmentation_error") is not None:
        raise PitPlanError("PASS fuel replay cannot carry a segmentation error")
    return series


def _validate_model_output(
    value: object,
    *,
    scenario_values: Mapping[str, float],
    scenario_provenance_by_field: Mapping[str, str],
    lap_receipt: Mapping[str, object],
) -> dict[str, float]:
    model = _exact_mapping(value, _MODEL_KEYS, "model_output")
    if model.get("status") != "ready" or model.get("reason_codes") != []:
        raise PitPlanError("fuel model output is not ready")
    rejection_counts = _list(model.get("rejection_counts"), "model_output.rejection_counts")
    rejection_names: list[str] = []
    for index, raw in enumerate(rejection_counts):
        pair = _list(raw, f"model_output.rejection_counts[{index}]")
        if len(pair) != 2:
            raise PitPlanError("model_output rejection count entry is invalid")
        rejection_names.append(_identifier(pair[0], "model_output rejection reason"))
        _plain_int(pair[1], "model_output rejection count", minimum=1)
    if rejection_names != sorted(set(rejection_names)):
        raise PitPlanError("model_output rejection counts are not sorted and unique")

    burn = _exact_mapping(model.get("burn"), _BURN_KEYS, "model_output.burn")
    accepted = _plain_int(burn.get("accepted_laps"), "model_output.burn.accepted_laps", minimum=1)
    rejected = _plain_int(burn.get("rejected_laps"), "model_output.burn.rejected_laps")
    if (
        accepted != lap_receipt["fuel_eligible_lap_count"]
        or accepted + rejected != lap_receipt["lap_count"]
    ):
        raise PitPlanError("model_output burn lap counts do not close to lap_receipt")
    if sum(pair[1] for pair in rejection_counts) != rejected:
        raise PitPlanError("model_output rejection counts do not close")
    if burn.get("label") != "derived" or burn.get("source_label") != "observed":
        raise PitPlanError("model_output burn labels are invalid")
    if burn.get("confidence") not in {"low", "medium", "high"}:
        raise PitPlanError("model_output burn confidence is invalid")
    mean_burn = _finite_number(
        burn.get("mean_l_per_lap"), "model_output.burn.mean_l_per_lap", positive=True
    )
    conservative_burn = _finite_number(
        burn.get("conservative_l_per_lap"),
        "model_output.burn.conservative_l_per_lap",
        positive=True,
    )
    minimum_burn = _finite_number(
        burn.get("minimum_l_per_lap"), "model_output.burn.minimum_l_per_lap", positive=True
    )
    maximum_burn = _finite_number(
        burn.get("maximum_l_per_lap"), "model_output.burn.maximum_l_per_lap", positive=True
    )
    deviation = _finite_number(
        burn.get("standard_deviation_l_per_lap"),
        "model_output.burn.standard_deviation_l_per_lap",
        minimum=0.0,
    )
    coefficient = _finite_number(
        burn.get("coefficient_of_variation"),
        "model_output.burn.coefficient_of_variation",
        minimum=0.0,
    )
    quantile = _finite_number(
        burn.get("conservative_quantile"), "model_output.burn.conservative_quantile", minimum=0.0
    )
    if not minimum_burn <= mean_burn <= conservative_burn <= maximum_burn:
        raise PitPlanError("model_output burn statistics are inconsistent")
    _close(deviation / mean_burn, coefficient, "model_output burn coefficient")
    _close(
        quantile, scenario_values["conservative_quantile"], "model/scenario conservative quantile"
    )
    if accepted < int(scenario_values["minimum_valid_laps"]):
        raise PitPlanError("model_output accepted laps violate scenario minimum")

    current_label = scenario_provenance_by_field["current_fuel_l"].lower()
    current = _labeled_number(model, "current_fuel_l", unit="L", label=current_label)
    remaining_label = (
        scenario_provenance_by_field["remaining_laps"].lower()
        if "remaining_laps" in scenario_values
        else "derived"
    )
    remaining = _labeled_number(
        model, "remaining_laps", unit="laps", label=remaining_label, integer=True
    )
    fields = {
        "mean_to_end": _labeled_number(model, "mean_fuel_to_end_l", unit="L", label="estimated"),
        "conservative_to_end": _labeled_number(
            model, "conservative_fuel_to_end_l", unit="L", label="estimated"
        ),
        "safe_current": _labeled_number(
            model, "safe_laps_on_current_fuel", unit="laps", label="estimated", integer=True
        ),
        "safe_full": _labeled_number(
            model, "safe_laps_on_full_tank", unit="laps", label="estimated", integer=True
        ),
        "minimum_stops": _labeled_number(
            model, "minimum_pit_stops", unit="stops", label="estimated", integer=True
        ),
        "cumulative_refuel": _labeled_number(
            model, "cumulative_refuel_to_end_l", unit="L", label="estimated"
        ),
        "cumulative_refuel_time": _labeled_number(
            model, "cumulative_refuel_time_to_end_s", unit="s", label="estimated"
        ),
    }
    if "remaining_laps" not in scenario_values:
        raise PitPlanError("offline pit-plan v1 requires a remaining_laps scenario")
    _close(current, scenario_values["current_fuel_l"], "model/scenario current fuel")
    _close(remaining, scenario_values["remaining_laps"], "model/scenario remaining laps")
    tank = scenario_values["tank_capacity_l"]
    reserve = scenario_values["reserve_l"]
    safe_current = math.floor((max(0.0, current - reserve) + 1e-12) / conservative_burn)
    safe_full = math.floor(((tank - reserve) + 1e-12) / conservative_burn)
    if int(fields["safe_current"]) != safe_current or int(fields["safe_full"]) != safe_full:
        raise PitPlanError("model_output safe laps do not close")
    expected_to_end = 0.0 if int(remaining) == 0 else conservative_burn * int(remaining) + reserve
    expected_mean_to_end = 0.0 if int(remaining) == 0 else mean_burn * int(remaining) + reserve
    expected_refuel = max(0.0, expected_to_end - current)
    _close(fields["mean_to_end"], expected_mean_to_end, "model mean fuel to end")
    _close(fields["conservative_to_end"], expected_to_end, "model conservative fuel to end")
    _close(fields["cumulative_refuel"], expected_refuel, "model cumulative refuel")
    _close(
        fields["cumulative_refuel_time"],
        expected_refuel / scenario_values["refuel_rate_l_per_s"],
        "model cumulative refuel time",
    )
    if int(remaining) <= safe_current:
        expected_stops = 0
    else:
        if safe_full < 1:
            raise PitPlanError("model tank cannot cover one conservative lap")
        expected_stops = math.ceil((int(remaining) - safe_current) / safe_full)
    if int(fields["minimum_stops"]) != expected_stops:
        raise PitPlanError("model minimum pit stops do not close")
    pit_window = model.get("next_pit_window")
    if expected_stops == 0:
        if pit_window is not None:
            raise PitPlanError("zero-stop model must not carry a pit window")
    else:
        window = _exact_mapping(
            pit_window,
            frozenset({"earliest_lap_from_now", "label", "latest_lap_from_now"}),
            "model_output.next_pit_window",
        )
        if window.get("label") != "estimated":
            raise PitPlanError("next_pit_window label is invalid")
        earliest = _plain_int(
            window.get("earliest_lap_from_now"), "next_pit_window.earliest_lap_from_now"
        )
        latest = _plain_int(
            window.get("latest_lap_from_now"), "next_pit_window.latest_lap_from_now"
        )
        expected_earliest = max(0, int(remaining) - expected_stops * safe_full)
        if earliest != expected_earliest or latest != safe_current:
            raise PitPlanError("next_pit_window does not close to the fuel model")
    return {
        **fields,
        "conservative_burn": conservative_burn,
        "current_fuel": current,
        "remaining_laps": remaining,
    }


def _validate_upstream_gates(
    replay: Mapping[str, object],
    *,
    model: Mapping[str, object],
    normalized: Mapping[str, object],
    pipeline: Mapping[str, object],
    scenario_sha256: str,
) -> None:
    capabilities = _exact_mapping(
        replay.get("capabilities"), _UPSTREAM_CAPABILITY_KEYS, "capabilities"
    )
    expected_unavailable = {
        "current_tire_wear": (
            "CURRENT_STINT_TIRE_WEAR_MODEL_NOT_IMPLEMENTED",
            "CURRENT_TIRE_WEAR_CLAIM",
        ),
        "opponent_fuel": ("OPPONENT_FUEL_NOT_EXPOSED_BY_SDK", "OPPONENT_FUEL_CLAIM"),
        "traffic_model": ("TRAFFIC_MODEL_NOT_IMPLEMENTED", "REJOIN_TRAFFIC_CLAIM"),
    }
    for name, (reason, claim) in expected_unavailable.items():
        capability = _validate_unavailable_capability(
            capabilities.get(name), f"capabilities.{name}"
        )
        if capability["reasons"] != [reason] or capability["blocked_claims"] != [claim]:
            raise PitPlanError(f"capabilities.{name} semantics are invalid")
    if capabilities.get("fuel_model_shadow") != {"reasons": [], "status": "PASS"}:
        raise PitPlanError("fuel_model_shadow capability is invalid")
    if capabilities.get("race_recommendation") != {
        "reasons": ["SHADOW_ONLY", "EVENT_RULES_PROFILE_MISSING", "TRAFFIC_MODEL_NOT_IMPLEMENTED"],
        "status": "BLOCKED",
    }:
        raise PitPlanError("fuel replay race recommendation gate is invalid")

    recommendations = _list(replay.get("recommendations"), "recommendations")
    if len(recommendations) != 1:
        raise PitPlanError("ready fuel replay must contain exactly one shadow recommendation")
    recommendation = _exact_mapping(
        recommendations[0], _UPSTREAM_RECOMMENDATION_KEYS, "recommendations[0]"
    )
    expected_action = {
        "cumulative_refuel_to_end": model.get("cumulative_refuel_to_end_l"),
        "minimum_pit_stops": model.get("minimum_pit_stops"),
        "next_pit_window": model.get("next_pit_window"),
    }
    burn = _mapping(model.get("burn"), "model_output.burn")
    scenario = _mapping(replay.get("scenario"), "scenario")
    scenario_provenance = (
        "USER_RULE"
        if any(field["provenance"] == "USER_RULE" for field in scenario.values())
        else "SDK_DIRECT"
    )
    expected_basis = {
        "historical_burn_stability": str(burn["confidence"]).upper(),
        "overall_plan": "LOW_BECAUSE_EVENT_RULES_AND_TRAFFIC_ARE_UNAVAILABLE",
        "scenario_inputs": scenario_provenance,
    }
    fixed = {
        "action": expected_action,
        "claim_level": "scenario_estimate",
        "confidence": "LOW",
        "confidence_basis": expected_basis,
        "executable": False,
        "kind": "FUEL_PLAN_CANDIDATE",
        "practice_only": False,
        "recommendation_id": "fuel:shadow_plan",
        "scenario_sha256": scenario_sha256,
        "status": "SHADOW_ONLY",
    }
    if any(recommendation[key] != expected for key, expected in fixed.items()):
        raise PitPlanError("fuel replay recommendation semantics are invalid")
    evidence_ids = _list(recommendation.get("evidence_ids"), "recommendation evidence_ids")
    prefix = f"{normalized['samples_sha256']}:{pipeline['lap_algorithm_version']}:lap:"
    accepted = int(burn["accepted_laps"])
    if (
        len(evidence_ids) != accepted
        or len(evidence_ids) != len(set(evidence_ids))
        or any(
            type(item) is not str
            or not item.startswith(prefix)
            or not item.removeprefix(prefix).isdigit()
            or int(item.removeprefix(prefix)) <= 0
            for item in evidence_ids
        )
    ):
        raise PitPlanError("fuel replay recommendation evidence IDs are invalid")


def _validate_fuel_replay(
    replay_value: object,
    *,
    expected_fuel_replay_sha256: str,
) -> dict[str, Any]:
    replay = _mapping(replay_value, "fuel replay")
    if set(replay) != _FUEL_REPLAY_KEYS:
        raise PitPlanError("fuel replay top-level keys are invalid")
    if replay.get("contract_version") != FUEL_REPLAY_CONTRACT_VERSION:
        raise PitPlanError("fuel replay contract_version is unsupported")
    expected = _sha256(expected_fuel_replay_sha256, "expected_fuel_replay_sha256")
    replay_sha = _sha256(replay.get("fuel_replay_sha256"), "fuel_replay_sha256")
    if replay_sha != expected:
        raise PitPlanError("fuel replay does not match the independent expected digest")
    binding = {key: replay[key] for key in _FUEL_REPLAY_BINDING_KEYS}
    if canonical_sha256(binding) != replay_sha:
        raise PitPlanError("fuel_replay_sha256 mismatch")

    event_receipt = _validate_event_receipt(replay.get("event_receipt"))
    pipeline, tick_rate_hz = _validate_pipeline(replay.get("pipeline"))
    input_kind = replay.get("input_kind")
    if input_kind not in {"ibt", "collector"}:
        raise PitPlanError("fuel replay input_kind is invalid")
    input_evidence = _validate_input_evidence(
        replay.get("input_evidence"), input_kind=input_kind, tick_rate_hz=tick_rate_hz
    )
    scenario, scenario_values, scenario_provenance_by_field = _validate_scenario(
        replay.get("scenario")
    )
    scenario_sha = _sha256(replay.get("scenario_sha256"), "scenario_sha256")
    if canonical_sha256(scenario) != scenario_sha:
        raise PitPlanError("scenario_sha256 mismatch")
    lap_receipt = _validate_lap_receipt(replay.get("lap_receipt"))
    normalized = _validate_normalized_receipt(replay.get("normalized_input_receipt"))
    series = _validate_series_evidence(replay.get("series_evidence"))
    evidence_count_key = "record_count" if input_kind == "ibt" else "frame_record_count"
    sample_count = event_receipt["sample_count"]
    if not (
        sample_count
        == input_evidence[evidence_count_key]
        == normalized["sample_count"]
        == lap_receipt["modeled_sample_count"]
        == series["modeled_sample_count"]
        == event_receipt["accepted_sample_count"]
    ):
        raise PitPlanError("fuel replay sample counts do not close across receipts")
    model_values = _validate_model_output(
        replay.get("model_output"),
        scenario_values=scenario_values,
        scenario_provenance_by_field=scenario_provenance_by_field,
        lap_receipt=lap_receipt,
    )
    model = _mapping(replay.get("model_output"), "model_output")
    model_output_sha = _sha256(replay.get("model_output_sha256"), "model_output_sha256")
    if canonical_sha256(model) != model_output_sha:
        raise PitPlanError("model_output_sha256 mismatch")
    semantic_binding = {
        "lap_receipt": replay["lap_receipt"],
        "model_output": model,
        "pipeline": pipeline,
        "scenario": scenario,
    }
    if canonical_sha256(semantic_binding) != _sha256(
        replay.get("model_semantic_sha256"), "model_semantic_sha256"
    ):
        raise PitPlanError("model_semantic_sha256 mismatch")

    quality = _mapping(replay.get("quality_gate"), "quality_gate")
    if set(quality) != {"reasons", "status"}:
        raise PitPlanError("quality_gate keys are invalid")
    reasons = _unique_codes(quality.get("reasons"), "quality_gate.reasons")
    if quality.get("status") != "PASS" or reasons:
        raise PitPlanError("fuel replay quality gate is not PASS")
    _validate_upstream_gates(
        replay,
        model=model,
        normalized=normalized,
        pipeline=pipeline,
        scenario_sha256=scenario_sha,
    )
    validated = dict(replay)
    validated["_validated_model_values"] = model_values
    validated["_validated_scenario_values"] = scenario_values
    return validated


def _validate_rules(value: object | None) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return None, None
    rules = _mapping(value, "event rules")
    if set(rules) != _RULES_KEYS:
        raise PitPlanError("event rules top-level keys are invalid")
    if rules.get("contract_version") != RULES_CONTRACT_VERSION:
        raise PitPlanError("event rules contract_version is unsupported")
    _identifier(rules.get("profile_id"), "event rules profile_id")
    status = rules.get("profile_status")
    if status not in {"UNKNOWN", "DEVELOPMENT_SMOKE"}:
        raise PitPlanError("event rules profile_status is unsupported")
    scope = _mapping(rules.get("scope"), "event rules scope")
    if set(scope) != _SCOPE_KEYS:
        raise PitPlanError("event rules scope keys are invalid")
    for name in sorted(_SCOPE_KEYS):
        _identifier(scope.get(name), f"event rules scope.{name}")

    if status == "UNKNOWN":
        if (
            rules.get("official_event_rules") is not False
            or rules.get("provenance") != "UNKNOWN"
            or rules.get("selection_policy") is not None
            or rules.get("service_rules") is not None
        ):
            raise PitPlanError("UNKNOWN event rules must not carry service claims")
        return rules, canonical_sha256(rules)

    if rules.get("official_event_rules") is not False or rules.get("provenance") != "USER_RULE":
        raise PitPlanError("DEVELOPMENT_SMOKE rules must be non-official USER_RULE values")
    if any(scope[name] != "UNBOUND_DEVELOPMENT_SMOKE" for name in _SCOPE_KEYS):
        raise PitPlanError("DEVELOPMENT_SMOKE rules must remain unbound")
    if rules.get("selection_policy") != "LATEST_FEASIBLE_FUEL_ONLY":
        raise PitPlanError("DEVELOPMENT_SMOKE selection_policy is unsupported")
    service = _mapping(rules.get("service_rules"), "event rules service_rules")
    if set(service) != _SERVICE_RULE_KEYS:
        raise PitPlanError("event rules service_rules keys are invalid")
    if service.get("fuel_tire_service_timing") not in {"SEQUENTIAL", "CONCURRENT"}:
        raise PitPlanError("fuel_tire_service_timing is unsupported")
    if type(service.get("no_tire_service_allowed")) is not bool:
        raise PitPlanError("no_tire_service_allowed must be boolean")
    if type(service.get("tire_change_required")) is not bool:
        raise PitPlanError("tire_change_required must be boolean")
    if service["tire_change_required"] and service["no_tire_service_allowed"]:
        raise PitPlanError("required tire changes cannot allow no-tire service")
    for name in ("pit_lane_loss_s", "tire_change_time_s"):
        _finite_number(service.get(name), f"service_rules.{name}", minimum=0.0)
    for name in ("refuel_rate_l_per_s", "tank_capacity_l"):
        _finite_number(service.get(name), f"service_rules.{name}", positive=True)
    return rules, canonical_sha256(rules)


def _unavailable(*, reasons: Sequence[str], blocked_claims: Sequence[str]) -> dict[str, object]:
    return {
        "blocked_claims": list(blocked_claims),
        "confidence": "NONE",
        "contract_version": INFERENCE_CAPABILITY_CONTRACT_VERSION,
        "estimate_available": False,
        "provenance": "UNKNOWN",
        "reasons": list(reasons),
        "status": "SKIP",
    }


def _valid_until() -> dict[str, object]:
    return {
        "mode": "RECOMPUTE_ON_ANY",
        "predicates": list(_VALID_UNTIL_PREDICATES),
    }


def _service_time(fuel_time_s: float, tire_time_s: float, timing: str) -> float:
    return fuel_time_s + tire_time_s if timing == "SEQUENTIAL" else max(fuel_time_s, tire_time_s)


def _build_service_alternatives(
    *,
    fuel_at_box_l: float,
    fuel_to_end_add_l: float,
    rules: Mapping[str, object],
) -> list[dict[str, object]]:
    service = _mapping(rules["service_rules"], "event rules service_rules")
    tank_capacity_l = float(service["tank_capacity_l"])
    refuel_rate = float(service["refuel_rate_l_per_s"])
    full_fuel_add_l = max(0.0, tank_capacity_l - fuel_at_box_l)
    tire_modes = [True]
    if service["no_tire_service_allowed"] and not service["tire_change_required"]:
        tire_modes.insert(0, False)
    alternatives: list[dict[str, object]] = []
    for fuel_mode, fuel_add_l in (
        ("FUEL_TO_END", fuel_to_end_add_l),
        ("FULL_FUEL", full_fuel_add_l),
    ):
        for change_tires in tire_modes:
            fuel_time = fuel_add_l / refuel_rate
            tire_time = float(service["tire_change_time_s"]) if change_tires else 0.0
            stationary = _service_time(
                fuel_time,
                tire_time,
                str(service["fuel_tire_service_timing"]),
            )
            alternative_id = f"{fuel_mode.lower()}:{'change_tires' if change_tires else 'no_tires'}"
            alternatives.append(
                {
                    "alternative_id": alternative_id,
                    "change_tires": change_tires,
                    "fuel_add_l": round(fuel_add_l, 6),
                    "fuel_mode": fuel_mode,
                    "fuel_service_time_s": round(fuel_time, 6),
                    "pit_lane_loss_s": round(float(service["pit_lane_loss_s"]), 6),
                    "provenance": "USER_RULE_DEVELOPMENT_SMOKE",
                    "service_timing": service["fuel_tire_service_timing"],
                    "stationary_service_time_s": round(stationary, 6),
                    "tire_service_time_s": round(tire_time, 6),
                    "total_pit_loss_s": round(float(service["pit_lane_loss_s"]) + stationary, 6),
                }
            )
    return alternatives


def _current_input_binding(
    replay: Mapping[str, object], *, expected_fuel_replay_sha256: str
) -> dict[str, object]:
    evidence = _mapping(replay.get("input_evidence"), "input_evidence")
    normalized = _mapping(replay.get("normalized_input_receipt"), "normalized_input_receipt")
    base: dict[str, object] = {
        "event_receipt_sha256": replay["event_receipt"]["receipt_sha256"],
        "expected_fuel_replay_sha256": expected_fuel_replay_sha256,
        "fuel_replay_contract_version": replay["contract_version"],
        "fuel_replay_sha256": replay["fuel_replay_sha256"],
        "input_evidence_sha256": canonical_sha256(evidence),
        "input_kind": replay["input_kind"],
        "model_output_sha256": replay["model_output_sha256"],
        "model_semantic_sha256": replay["model_semantic_sha256"],
        "normalized_input_contract_version": normalized["contract_version"],
        "normalized_sample_count": normalized["sample_count"],
        "normalized_samples_sha256": normalized["samples_sha256"],
        "scenario_sha256": replay["scenario_sha256"],
        "session_id": evidence["session_id"],
        "source_id": evidence["source_id"],
        "source_kind": evidence["source_kind"],
    }
    return {**base, "input_lineage_sha256": canonical_sha256(base)}


def _current_rules_binding(
    rules: Mapping[str, object] | None,
    *,
    rules_sha256: str | None,
) -> dict[str, object]:
    base: dict[str, object] = {
        "official_event_rules": rules.get("official_event_rules") if rules is not None else False,
        "profile_id": rules.get("profile_id") if rules is not None else None,
        "profile_status": rules.get("profile_status") if rules is not None else "MISSING",
        "rules_sha256": rules_sha256,
    }
    return {**base, "rules_lineage_sha256": canonical_sha256(base)}


def _validate_input_binding(value: object, name: str) -> dict[str, Any]:
    binding = _exact_mapping(value, _INPUT_BINDING_KEYS, name)
    for key in (
        "event_receipt_sha256",
        "expected_fuel_replay_sha256",
        "fuel_replay_sha256",
        "input_evidence_sha256",
        "input_lineage_sha256",
        "model_output_sha256",
        "model_semantic_sha256",
        "normalized_samples_sha256",
        "scenario_sha256",
    ):
        _sha256(binding.get(key), f"{name}.{key}")
    if binding["expected_fuel_replay_sha256"] != binding["fuel_replay_sha256"]:
        raise PitPlanError(f"{name} independent replay digest does not match")
    if (
        binding.get("fuel_replay_contract_version") != FUEL_REPLAY_CONTRACT_VERSION
        or binding.get("normalized_input_contract_version") != NORMALIZED_TELEMETRY_CONTRACT_VERSION
        or binding.get("input_kind") not in {"ibt", "collector"}
    ):
        raise PitPlanError(f"{name} contract values are invalid")
    _plain_int(binding.get("normalized_sample_count"), f"{name}.normalized_sample_count", minimum=1)
    for key in ("source_id", "session_id"):
        _identifier(binding.get(key), f"{name}.{key}")
    expected_kind = {
        "ibt": {"IBT_OFFLINE"},
        "collector": {"SDK_LIVE", "REPLAY_SDK_PROXY"},
    }[binding["input_kind"]]
    if binding.get("source_kind") not in expected_kind:
        raise PitPlanError(f"{name}.source_kind is invalid")
    base = {key: item for key, item in binding.items() if key != "input_lineage_sha256"}
    if canonical_sha256(base) != binding["input_lineage_sha256"]:
        raise PitPlanError(f"{name}.input_lineage_sha256 mismatch")
    return binding


def _validate_rules_binding(value: object, name: str) -> dict[str, Any]:
    binding = _exact_mapping(value, _RULES_BINDING_KEYS, name)
    if binding.get("official_event_rules") is not False:
        raise PitPlanError(f"{name} cannot claim official rules")
    status = binding.get("profile_status")
    if status not in {"MISSING", "UNKNOWN", "DEVELOPMENT_SMOKE"}:
        raise PitPlanError(f"{name}.profile_status is invalid")
    if status == "MISSING":
        if binding.get("profile_id") is not None or binding.get("rules_sha256") is not None:
            raise PitPlanError(f"{name} missing profile carries a rules claim")
    else:
        _identifier(binding.get("profile_id"), f"{name}.profile_id")
        _sha256(binding.get("rules_sha256"), f"{name}.rules_sha256")
    _sha256(binding.get("rules_lineage_sha256"), f"{name}.rules_lineage_sha256")
    base = {key: item for key, item in binding.items() if key != "rules_lineage_sha256"}
    if canonical_sha256(base) != binding["rules_lineage_sha256"]:
        raise PitPlanError(f"{name}.rules_lineage_sha256 mismatch")
    return binding


def _validate_previous_plan(
    value: object,
    *,
    current_input_binding: Mapping[str, object],
) -> dict[str, Any]:
    plan = _mapping(value, "previous pit plan")
    if set(plan) != _PIT_PLAN_KEYS:
        raise PitPlanError("previous pit plan top-level keys are invalid")
    if plan.get("contract_version") != PIT_PLAN_CONTRACT_VERSION:
        raise PitPlanError("previous pit plan contract_version is unsupported")
    digest = _sha256(plan.get("pit_plan_sha256"), "previous pit_plan_sha256")
    binding = {key: item for key, item in plan.items() if key != "pit_plan_sha256"}
    if canonical_sha256(binding) != digest:
        raise PitPlanError("previous pit_plan_sha256 mismatch")
    if plan.get("attestation_status") != "NOT_R7_ATTESTED":
        raise PitPlanError("previous pit plan attestation boundary is invalid")
    if (
        plan.get("advisor_only") is not True
        or plan.get("derivation_status") != "POST_ADMISSION_DERIVED"
        or plan.get("execution_mode") != "SHADOW"
    ):
        raise PitPlanError("previous pit plan advisor-only boundary is invalid")
    previous_input = _validate_input_binding(plan.get("input_binding"), "previous input_binding")
    _validate_rules_binding(plan.get("rules_binding"), "previous rules_binding")
    identity_keys = ("input_kind", "source_kind", "source_id", "session_id")
    if any(previous_input[key] != current_input_binding[key] for key in identity_keys):
        raise PitPlanError("previous pit plan source/session identity mismatch")
    quality = _exact_mapping(
        plan.get("quality_gate"), frozenset({"reasons", "status"}), "previous quality_gate"
    )
    quality_reasons = _unique_codes(quality.get("reasons"), "previous quality_gate.reasons")
    if quality.get("status") not in {
        "PASS_DEVELOPMENT_SMOKE",
        "WAIT_EVENT_RULES",
        "WAIT_STRATEGY_DATA",
    }:
        raise PitPlanError("previous quality_gate status is invalid")
    if quality["status"] == "PASS_DEVELOPMENT_SMOKE" and quality_reasons != [
        "NON_OFFICIAL_DEVELOPMENT_SMOKE_ONLY"
    ]:
        raise PitPlanError("previous PASS quality_gate reasons are invalid")
    capabilities = _mapping(plan.get("capabilities"), "previous capabilities")
    race_gate = _mapping(
        capabilities.get("race_recommendation"),
        "previous race recommendation gate",
    )
    if race_gate.get("status") != "BLOCKED":
        raise PitPlanError("previous race recommendation must remain BLOCKED")
    recommendations = _list(plan.get("recommendations"), "previous recommendations")
    if len(recommendations) > 1:
        raise PitPlanError("previous pit plan has multiple active recommendations")
    alternatives = _list(plan.get("service_alternatives"), "previous service alternatives")
    alternative_by_id: dict[str, Mapping[str, object]] = {}
    for index, item in enumerate(alternatives):
        alternative = _exact_mapping(
            item, _SERVICE_ALTERNATIVE_KEYS, f"previous service alternative {index}"
        )
        alternative_id = _identifier(
            alternative.get("alternative_id"), f"previous service alternative {index} id"
        )
        if alternative_id in alternative_by_id:
            raise PitPlanError("previous service alternative IDs are not unique")
        if type(alternative.get("change_tires")) is not bool:
            raise PitPlanError("previous service alternative change_tires is invalid")
        if (
            alternative.get("fuel_mode") not in {"FUEL_TO_END", "FULL_FUEL"}
            or alternative.get("provenance") != "USER_RULE_DEVELOPMENT_SMOKE"
        ):
            raise PitPlanError("previous service alternative semantics are invalid")
        if alternative.get("service_timing") not in {"SEQUENTIAL", "CONCURRENT"}:
            raise PitPlanError("previous service alternative timing is invalid")
        for key in (
            "fuel_add_l",
            "fuel_service_time_s",
            "pit_lane_loss_s",
            "stationary_service_time_s",
            "tire_service_time_s",
            "total_pit_loss_s",
        ):
            _finite_number(alternative.get(key), f"previous service alternative {key}", minimum=0.0)
        alternative_by_id[alternative_id] = alternative
    for value in recommendations:
        recommendation = _exact_mapping(value, _PLAN_RECOMMENDATION_KEYS, "previous recommendation")
        if (
            recommendation.get("executable") is not False
            or recommendation.get("status") != "SHADOW_ONLY"
            or recommendation.get("claim_scope") != "FUEL_FEASIBILITY_ONLY"
        ):
            raise PitPlanError("previous recommendation is not a safe shadow candidate")
        if any(
            recommendation[key] != expected
            for key, expected in {
                "confidence": "LOW",
                "expected_gain_range_s": None,
                "kind": "PIT_PLAN_CANDIDATE",
                "practice_only": False,
                "reason": _RECOMMENDATION_REASON,
                "risk": [
                    "DEVELOPMENT_SMOKE_NOT_EVENT_TRUTH",
                    "TRAFFIC_AND_REJOIN_UNAVAILABLE",
                    "TIRE_PERFORMANCE_UNMODELED",
                    "NOT_R7_ATTESTED",
                ],
            }.items()
        ):
            raise PitPlanError("previous recommendation fixed semantics are invalid")
        _identifier(
            recommendation.get("recommendation_id"),
            "previous recommendation_id",
        )
        basis = _exact_mapping(
            recommendation.get("recommendation_basis"),
            _RECOMMENDATION_BASIS_KEYS,
            "previous recommendation_basis",
        )
        for key in (
            "input_lineage_sha256",
            "model_semantic_sha256",
            "normalized_samples_sha256",
            "rules_lineage_sha256",
            "rules_sha256",
            "scenario_sha256",
        ):
            _sha256(basis.get(key), f"previous recommendation_basis.{key}")
        for key in ("source_id", "session_id", "source_kind", "alternative_id"):
            _identifier(basis.get(key), f"previous recommendation_basis.{key}")
        _finite_number(
            basis.get("fuel_add_l"), "previous recommendation_basis.fuel_add_l", minimum=0.0
        )
        _plain_int(
            basis.get("recommended_lap_from_now"),
            "previous recommendation_basis.recommended_lap_from_now",
        )
        if recommendation["recommendation_id"] != f"pit-plan:{canonical_sha256(basis)}":
            raise PitPlanError("previous recommendation_id does not bind its basis")
        if any(
            basis[key] != previous_input[key]
            for key in (
                "input_lineage_sha256",
                "model_semantic_sha256",
                "normalized_samples_sha256",
                "scenario_sha256",
                "session_id",
                "source_id",
                "source_kind",
            )
        ):
            raise PitPlanError("previous recommendation basis/input lineage mismatch")
        previous_rules = _mapping(plan["rules_binding"], "previous rules_binding")
        if (
            basis["rules_lineage_sha256"] != previous_rules["rules_lineage_sha256"]
            or basis["rules_sha256"] != previous_rules["rules_sha256"]
            or recommendation["evidence_ids"] != basis["evidence_ids"]
        ):
            raise PitPlanError("previous recommendation basis/rules lineage mismatch")
        action = _exact_mapping(
            recommendation.get("action"),
            frozenset({"fuel_add_l", "recommended_lap_from_now", "service_alternative_id"}),
            "previous recommendation action",
        )
        if action != {
            "fuel_add_l": basis["fuel_add_l"],
            "recommended_lap_from_now": basis["recommended_lap_from_now"],
            "service_alternative_id": basis["alternative_id"],
        }:
            raise PitPlanError("previous recommendation action/basis mismatch")
        selected = alternative_by_id.get(str(action["service_alternative_id"]))
        if selected is None or selected["fuel_add_l"] != action["fuel_add_l"]:
            raise PitPlanError("previous recommendation selected alternative mismatch")
        alternative_ids = _list(
            recommendation.get("alternatives"), "previous recommendation alternatives"
        )
        if alternative_ids != list(alternative_by_id):
            raise PitPlanError("previous recommendation alternatives do not bind service records")
        evidence_ids = _unique_codes(
            recommendation.get("evidence_ids"), "previous recommendation evidence_ids"
        )
        if evidence_ids != basis["evidence_ids"]:
            raise PitPlanError("previous recommendation evidence/basis mismatch")
        supersedes_id = recommendation.get("supersedes_id")
        if supersedes_id is not None:
            _identifier(supersedes_id, "previous recommendation supersedes_id")
        valid_until = _exact_mapping(
            recommendation.get("valid_until"),
            frozenset({"mode", "predicates"}),
            "previous recommendation valid_until",
        )
        if valid_until.get("mode") != "RECOMPUTE_ON_ANY" or valid_until.get("predicates") != list(
            _VALID_UNTIL_PREDICATES
        ):
            raise PitPlanError("previous recommendation valid_until is invalid")
    if bool(recommendations) != (quality["status"] == "PASS_DEVELOPMENT_SMOKE"):
        raise PitPlanError("previous recommendation/quality status is inconsistent")
    return plan


def _lineage_change_reasons(
    previous: Mapping[str, object] | None,
    current_input: Mapping[str, object],
    current_rules: Mapping[str, object],
) -> list[str]:
    if previous is None:
        return []
    old_input = _mapping(previous["input_binding"], "previous input_binding")
    old_rules = _mapping(previous["rules_binding"], "previous rules_binding")
    reasons: list[str] = []
    if any(
        old_input[key] != current_input[key]
        for key in ("input_evidence_sha256", "normalized_sample_count", "normalized_samples_sha256")
    ):
        reasons.append("NORMALIZED_INPUT_LINEAGE_CHANGED")
    if old_input["scenario_sha256"] != current_input["scenario_sha256"]:
        reasons.append("SCENARIO_CHANGED")
    if any(
        old_input[key] != current_input[key]
        for key in ("model_output_sha256", "model_semantic_sha256")
    ):
        reasons.append("MODEL_SEMANTICS_CHANGED")
    if any(
        old_input[key] != current_input[key]
        for key in ("event_receipt_sha256", "fuel_replay_sha256")
    ):
        reasons.append("UPSTREAM_FUEL_RECEIPT_CHANGED")
    if old_rules["rules_lineage_sha256"] != current_rules["rules_lineage_sha256"]:
        reasons.append("EVENT_RULES_PROFILE_CHANGED")
    return reasons


def _revoke_event(
    recommendation_id: str,
    *,
    reasons: Sequence[str],
    previous: Mapping[str, object],
    current_input: Mapping[str, object],
    current_rules: Mapping[str, object],
) -> dict[str, object]:
    previous_input = _mapping(previous["input_binding"], "previous input_binding")
    previous_rules = _mapping(previous["rules_binding"], "previous rules_binding")
    return {
        "current_input_lineage_sha256": current_input["input_lineage_sha256"],
        "current_rules_lineage_sha256": current_rules["rules_lineage_sha256"],
        "event": "REVOKE",
        "previous_input_lineage_sha256": previous_input["input_lineage_sha256"],
        "previous_rules_lineage_sha256": previous_rules["rules_lineage_sha256"],
        "reason_codes": list(dict.fromkeys(reasons)),
        "recommendation_id": recommendation_id,
        "session_id": current_input["session_id"],
        "source_id": current_input["source_id"],
    }


def _active_recommendation_id(previous: Mapping[str, object] | None) -> str | None:
    if previous is None:
        return None
    recommendations = _list(previous.get("recommendations"), "previous recommendations")
    if not recommendations:
        return None
    recommendation = _mapping(recommendations[0], "previous recommendation")
    return _identifier(recommendation.get("recommendation_id"), "previous recommendation_id")


def build_pit_plan(
    fuel_replay_value: object,
    *,
    expected_fuel_replay_sha256: str,
    rules_value: object | None = None,
    previous_plan_value: object | None = None,
) -> dict[str, object]:
    """Return a deterministic shadow plan or an explicit WAIT receipt."""

    replay = _validate_fuel_replay(
        fuel_replay_value,
        expected_fuel_replay_sha256=expected_fuel_replay_sha256,
    )
    rules, rules_sha256 = _validate_rules(rules_value)
    input_binding = _current_input_binding(
        replay, expected_fuel_replay_sha256=expected_fuel_replay_sha256
    )
    rules_binding = _current_rules_binding(rules, rules_sha256=rules_sha256)
    previous = (
        _validate_previous_plan(
            previous_plan_value,
            current_input_binding=input_binding,
        )
        if previous_plan_value is not None
        else None
    )
    previous_id = _active_recommendation_id(previous)
    lineage_reasons = _lineage_change_reasons(previous, input_binding, rules_binding)
    replay_sha = str(replay["fuel_replay_sha256"])

    capabilities: dict[str, object] = {
        "current_tire_wear": _unavailable(
            reasons=("CURRENT_STINT_TIRE_WEAR_MODEL_NOT_IMPLEMENTED",),
            blocked_claims=("CURRENT_TIRE_WEAR_CLAIM",),
        ),
        "opponent_fuel": _unavailable(
            reasons=("OPPONENT_FUEL_NOT_EXPOSED_BY_SDK",),
            blocked_claims=("OPPONENT_FUEL_CLAIM",),
        ),
        "race_recommendation": {
            "reasons": [
                "SHADOW_ONLY",
                "FUEL_FEASIBILITY_ONLY",
                "TRAFFIC_AND_REJOIN_UNAVAILABLE",
                "NOT_R7_ATTESTED",
            ],
            "status": "BLOCKED",
        },
        "rejoin_prediction": _unavailable(
            reasons=("REJOIN_TRAFFIC_MODEL_NOT_IMPLEMENTED",),
            blocked_claims=("REJOIN_POSITION_CLAIM", "REJOIN_GAP_CLAIM"),
        ),
        "traffic_model": _unavailable(
            reasons=("TRAFFIC_MODEL_NOT_IMPLEMENTED",),
            blocked_claims=("REJOIN_TRAFFIC_CLAIM", "TACTICAL_PIT_TIMING_CLAIM"),
        ),
    }
    recommendations: list[dict[str, object]] = []
    lifecycle_events: list[dict[str, object]] = []
    alternatives: list[dict[str, object]] = []

    rules_status = rules_binding["profile_status"]
    if rules_status != "DEVELOPMENT_SMOKE":
        status = "WAIT_EVENT_RULES"
        reasons = ["EVENT_RULES_PROFILE_MISSING_OR_UNKNOWN"]
        capabilities["event_rules"] = {
            "reasons": reasons,
            "status": "WAIT_EVENT_RULES",
        }
        capabilities["fuel_feasibility"] = {
            "reasons": reasons,
            "status": "BLOCKED",
        }
        if previous_id is not None:
            assert previous is not None
            lifecycle_events.append(
                _revoke_event(
                    previous_id,
                    reasons=(*lineage_reasons, "EVENT_RULES_BECAME_UNAVAILABLE"),
                    previous=previous,
                    current_input=input_binding,
                    current_rules=rules_binding,
                )
            )
        plan_scope = None
    else:
        assert rules is not None
        model = _mapping(replay["model_output"], "model_output")
        model_values = _mapping(replay["_validated_model_values"], "validated model values")
        scenario_values = _mapping(
            replay["_validated_scenario_values"], "validated scenario values"
        )
        service = _mapping(rules["service_rules"], "event rules service_rules")
        scenario_tank = float(scenario_values["tank_capacity_l"])
        scenario_rate = float(scenario_values["refuel_rate_l_per_s"])
        if not math.isclose(scenario_tank, float(service["tank_capacity_l"]), abs_tol=1e-9):
            raise PitPlanError("event rules tank capacity conflicts with fuel scenario")
        if not math.isclose(scenario_rate, float(service["refuel_rate_l_per_s"]), abs_tol=1e-9):
            raise PitPlanError("event rules refuel rate conflicts with fuel scenario")

        minimum_stops = int(model_values["minimum_stops"])
        pit_window_value = model.get("next_pit_window")
        if minimum_stops != 1 or type(pit_window_value) is not dict:
            status = "WAIT_STRATEGY_DATA"
            reasons = ["DEVELOPMENT_SMOKE_V1_REQUIRES_EXACTLY_ONE_STOP_AND_A_PIT_WINDOW"]
            capabilities["event_rules"] = {
                "official_event_rules": False,
                "profile_status": "DEVELOPMENT_SMOKE",
                "reasons": ["NON_OFFICIAL_DEVELOPMENT_SMOKE_ONLY"],
                "status": "DEVELOPMENT_SMOKE",
            }
            capabilities["fuel_feasibility"] = {
                "reasons": reasons,
                "status": "BLOCKED",
            }
            if previous_id is not None:
                assert previous is not None
                lifecycle_events.append(
                    _revoke_event(
                        previous_id,
                        reasons=(
                            *lineage_reasons,
                            "ONE_STOP_FEASIBILITY_PRECONDITION_CHANGED",
                        ),
                        previous=previous,
                        current_input=input_binding,
                        current_rules=rules_binding,
                    )
                )
            plan_scope = "FUEL_FEASIBILITY_ONLY"
        else:
            pit_window = _mapping(pit_window_value, "model_output.next_pit_window")
            if set(pit_window) != {
                "earliest_lap_from_now",
                "label",
                "latest_lap_from_now",
            }:
                raise PitPlanError("next_pit_window keys are invalid")
            earliest = _plain_int(
                pit_window.get("earliest_lap_from_now"),
                "next_pit_window.earliest_lap_from_now",
            )
            latest = _plain_int(
                pit_window.get("latest_lap_from_now"),
                "next_pit_window.latest_lap_from_now",
            )
            if latest < earliest:
                raise PitPlanError("next_pit_window is reversed")
            current_fuel_l = float(model_values["current_fuel"])
            remaining_laps = int(model_values["remaining_laps"])
            safe_current_laps = int(model_values["safe_current"])
            if latest > remaining_laps or latest > safe_current_laps:
                raise PitPlanError("next_pit_window exceeds remaining or safe-current laps")
            conservative_burn = float(model_values["conservative_burn"])
            reserve_l = float(scenario_values["reserve_l"])
            recommended_lap = latest
            fuel_at_box = current_fuel_l - conservative_burn * recommended_lap
            if fuel_at_box < reserve_l - 1e-6:
                raise PitPlanError("recommended pit lap would consume the bound reserve")
            laps_after_box = remaining_laps - recommended_lap
            fuel_needed_after_box = conservative_burn * laps_after_box + reserve_l
            fuel_to_end_add = fuel_needed_after_box - fuel_at_box
            if fuel_to_end_add < -1e-9:
                raise PitPlanError("one-stop fuel calculation produced negative refuel")
            _close(
                fuel_to_end_add,
                float(model_values["cumulative_refuel"]),
                "pit-plan/model cumulative refuel",
            )
            if fuel_at_box + fuel_to_end_add > scenario_tank + 1e-9:
                raise PitPlanError("one-stop fuel-to-end plan exceeds tank capacity")
            alternatives = _build_service_alternatives(
                fuel_at_box_l=fuel_at_box,
                fuel_to_end_add_l=fuel_to_end_add,
                rules=rules,
            )
            fuel_to_end_options = [
                item for item in alternatives if item["fuel_mode"] == "FUEL_TO_END"
            ]
            no_tire_options = [
                item for item in fuel_to_end_options if item["change_tires"] is False
            ]
            selected = min(
                no_tire_options or fuel_to_end_options,
                key=lambda item: float(item["total_pit_loss_s"]),
            )
            evidence_ids = [
                f"fuel-replay:{replay_sha}",
                f"event-receipt:{replay['event_receipt']['receipt_sha256']}",
                f"event-rules:{rules_sha256}",
            ]
            recommendation_basis = {
                "alternative_id": selected["alternative_id"],
                "evidence_ids": evidence_ids,
                "fuel_add_l": selected["fuel_add_l"],
                "input_lineage_sha256": input_binding["input_lineage_sha256"],
                "model_semantic_sha256": input_binding["model_semantic_sha256"],
                "normalized_samples_sha256": input_binding["normalized_samples_sha256"],
                "recommended_lap_from_now": recommended_lap,
                "rules_lineage_sha256": rules_binding["rules_lineage_sha256"],
                "rules_sha256": rules_sha256,
                "scenario_sha256": input_binding["scenario_sha256"],
                "session_id": input_binding["session_id"],
                "source_id": input_binding["source_id"],
                "source_kind": input_binding["source_kind"],
            }
            recommendation_id = f"pit-plan:{canonical_sha256(recommendation_basis)}"
            supersedes_id = previous_id if previous_id != recommendation_id else None
            recommendation = {
                "action": {
                    "fuel_add_l": selected["fuel_add_l"],
                    "recommended_lap_from_now": recommended_lap,
                    "service_alternative_id": selected["alternative_id"],
                },
                "alternatives": [item["alternative_id"] for item in alternatives],
                "claim_scope": "FUEL_FEASIBILITY_ONLY",
                "confidence": "LOW",
                "evidence_ids": evidence_ids,
                "executable": False,
                "expected_gain_range_s": None,
                "kind": "PIT_PLAN_CANDIDATE",
                "practice_only": False,
                "reason": _RECOMMENDATION_REASON,
                "recommendation_basis": recommendation_basis,
                "recommendation_id": recommendation_id,
                "risk": [
                    "DEVELOPMENT_SMOKE_NOT_EVENT_TRUTH",
                    "TRAFFIC_AND_REJOIN_UNAVAILABLE",
                    "TIRE_PERFORMANCE_UNMODELED",
                    "NOT_R7_ATTESTED",
                ],
                "status": "SHADOW_ONLY",
                "supersedes_id": supersedes_id,
                "valid_until": _valid_until(),
            }
            recommendations = [recommendation]
            if previous_id is None:
                lifecycle_events.append(
                    {
                        "event": "ISSUE",
                        "recommendation_id": recommendation_id,
                    }
                )
            elif previous_id != recommendation_id:
                assert previous is not None
                if not lineage_reasons:
                    raise PitPlanError("changed recommendation lacks a comparable lineage change")
                lifecycle_events.extend(
                    (
                        _revoke_event(
                            previous_id,
                            reasons=lineage_reasons,
                            previous=previous,
                            current_input=input_binding,
                            current_rules=rules_binding,
                        ),
                        {
                            "event": "ISSUE",
                            "recommendation_id": recommendation_id,
                            "supersedes_id": previous_id,
                        },
                    )
                )
            else:
                lifecycle_events.append(
                    {
                        "event": "NO_CHANGE",
                        "recommendation_id": recommendation_id,
                    }
                )
            status = "PASS_DEVELOPMENT_SMOKE"
            reasons = ["NON_OFFICIAL_DEVELOPMENT_SMOKE_ONLY"]
            plan_scope = "FUEL_FEASIBILITY_ONLY"
            capabilities["event_rules"] = {
                "official_event_rules": False,
                "profile_status": "DEVELOPMENT_SMOKE",
                "reasons": reasons,
                "status": "DEVELOPMENT_SMOKE",
            }
            capabilities["fuel_feasibility"] = {
                "reasons": [],
                "status": "PASS_DEVELOPMENT_SMOKE",
            }

    binding: dict[str, object] = {
        "advisor_only": True,
        "attestation_status": "NOT_R7_ATTESTED",
        "capabilities": capabilities,
        "contract_version": PIT_PLAN_CONTRACT_VERSION,
        "derivation_status": "POST_ADMISSION_DERIVED",
        "execution_mode": "SHADOW",
        "input_binding": input_binding,
        "lifecycle_events": lifecycle_events,
        "plan_scope": plan_scope,
        "quality_gate": {
            "reasons": reasons,
            "status": status,
        },
        "recommendations": recommendations,
        "rules_binding": rules_binding,
        "service_alternatives": alternatives,
    }
    return {**binding, "pit_plan_sha256": canonical_sha256(binding)}


def _exclusive_write(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            os.unlink(path)
        raise
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fuel_replay", type=Path)
    parser.add_argument(
        "--expected-fuel-replay-sha256",
        required=True,
        help="Independent admitted fuel_replay_sha256 trust root.",
    )
    parser.add_argument("--rules", type=Path)
    parser.add_argument("--previous-plan", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        replay = _strict_json_load(args.fuel_replay, label="fuel replay")
        rules = (
            _strict_json_load(args.rules, label="event rules") if args.rules is not None else None
        )
        previous = (
            _strict_json_load(args.previous_plan, label="previous pit plan")
            if args.previous_plan is not None
            else None
        )
        result = build_pit_plan(
            replay,
            expected_fuel_replay_sha256=args.expected_fuel_replay_sha256,
            rules_value=rules,
            previous_plan_value=previous,
        )
        serialized = _canonical_json(result) + b"\n"
        if args.output is not None:
            _exclusive_write(args.output, serialized)
        sys.stdout.buffer.write(serialized)
        sys.stdout.buffer.flush()
        return 0 if result["quality_gate"]["status"] == "PASS_DEVELOPMENT_SMOKE" else 5
    except (OSError, PitPlanError) as exc:
        error = {
            "contract_version": PIT_PLAN_CONTRACT_VERSION,
            "error": str(exc),
            "status": "FAIL",
        }
        sys.stdout.buffer.write(_canonical_json(error) + b"\n")
        sys.stdout.buffer.flush()
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
