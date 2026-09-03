"""Build provenance-bound Top-3 loss cards from a complete driving replay.

This reusable reporting layer consumes the
already frozen ``driving-model-replay-v1`` output, verifies its internal
receipts and closed loss partition, and emits descriptive shadow cards.  It
does not create a new driving diagnosis or promote model corner IDs to human
authenticated corner names.
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

CORNER_CARD_CONTRACT_VERSION = "offline-corner-cards-v1"
CORNER_CARD_RANKING_VERSION = "median-accounted-loss-v1"
DRIVING_REPLAY_CONTRACT_VERSION = "driving-model-replay-v1"
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_CARDS = 3

_SHA256_CHARS = frozenset("0123456789abcdef")
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
_REPLAY_KEYS = frozenset((*_REPLAY_BINDING_KEYS, "driving_replay_sha256"))
_MODEL_KEYS = frozenset(
    {
        "algorithm_version",
        "corner_metrics",
        "corners",
        "delta_closures",
        "diagnoses",
        "eligible_lap_ordinals",
        "grid_step_m",
        "lap_summaries",
        "reference",
        "refusal_reasons",
        "status",
        "track_length_m",
    }
)
_CORNER_KEYS = frozenset(
    {
        "accounting_start_m",
        "apex_m",
        "approach_start_m",
        "brake_end_m",
        "brake_start_m",
        "carry_end_m",
        "corner_id",
        "exit_m",
    }
)
_METRIC_KEYS = frozenset(
    {
        "accounted_window_delta_s",
        "apex_m",
        "apex_speed_mps",
        "approach_delta_s",
        "brake_onset_m",
        "brake_release_m",
        "carry_delta_s",
        "coast_distance_m",
        "corner_id",
        "delta_at_accounting_start_s",
        "delta_at_apex_s",
        "delta_at_entry_s",
        "delta_at_exit_s",
        "entry_speed_mps",
        "exit_speed_mps",
        "lap_ordinal",
        "local_delta_s",
        "second_lift",
        "throttle_pickup_m",
        "total_segment_delta_s",
    }
)
_DIAGNOSIS_KEYS = frozenset(
    {
        "action",
        "claim_level",
        "comparisons",
        "confidence",
        "corner_id",
        "counterexample_lap_ordinals",
        "diagnosis",
        "estimated_loss_median_s",
        "evidence_lap_ordinals",
        "expected_gain_range_s",
        "practice_only",
    }
)
_COMPARISON_KEYS = frozenset({"difference", "evidence_median", "metric", "reference_value", "unit"})
_DRIVING_CONFIG_KEYS = frozenset(
    {
        "apex_search_after_brake_m",
        "approach_window_m",
        "brake_release_threshold",
        "brake_threshold",
        "event_position_difference_m",
        "event_sustain_m",
        "fastest_group_fraction",
        "grid_step_m",
        "lap_delta_closure_tolerance_s",
        "long_coast_difference_m",
        "max_brake_gap_m",
        "max_corner_exit_after_apex_m",
        "max_reference_duration_spread_fraction",
        "min_braking_zone_m",
        "min_clean_laps",
        "min_evidence_laps",
        "min_reference_group_laps",
        "minimum_carry_loss_s",
        "minimum_loss_s",
        "second_lift_threshold",
        "speed_difference_mps",
        "throttle_pickup_threshold",
    }
)
_SEMANTIC_CHANNELS = (
    "SessionNum",
    "SessionTick",
    "SessionTime",
    "Lap",
    "LapCompleted",
    "LapDistPct",
    "Speed",
    "Throttle",
    "Brake",
    "SteeringWheelAngle",
    "OnPitRoad",
    "PlayerTrackSurface",
    "PlayerIncidentCount",
    "QualityStatus",
    "DroppedTicks",
    "QualityIssues",
)
_DIAGNOSIS_ACTIONS = {
    "LONG_COAST": (
        "Practice moving the brake point later in small steps while keeping one "
        "continuous release-to-throttle transition."
    ),
    "LATE_BRAKING_HURTS_EXIT": (
        "Brake slightly earlier and shorter, then release pressure earlier to "
        "prioritize minimum speed and the exit."
    ),
    "THROTTLE_SECOND_LIFT": (
        "Use a slightly later but stable single throttle application in practice "
        "instead of an early application followed by a lift."
    ),
}
_DIAGNOSIS_COMPARISONS = {
    "LONG_COAST": (
        ("brake_onset_m", "m"),
        ("coast_distance_m", "m"),
        ("apex_speed_mps", "m/s"),
        ("exit_speed_mps", "m/s"),
        ("total_segment_delta_s", "s"),
    ),
    "LATE_BRAKING_HURTS_EXIT": (
        ("brake_onset_m", "m"),
        ("brake_release_m", "m"),
        ("apex_speed_mps", "m/s"),
        ("throttle_pickup_m", "m"),
        ("exit_speed_mps", "m/s"),
        ("carry_delta_s", "s"),
    ),
    "THROTTLE_SECOND_LIFT": (
        ("throttle_pickup_m", "m"),
        ("second_lift", "bool"),
        ("exit_speed_mps", "m/s"),
        ("total_segment_delta_s", "s"),
    ),
}
_RECOMMENDATION_KEYS = frozenset(
    {
        "action",
        "claim_level",
        "confidence",
        "confidence_basis",
        "corner_id",
        "counterexample_lap_ids",
        "diagnosis",
        "estimated_loss_us",
        "evidence_lap_ids",
        "executable",
        "expected_gain_range_us",
        "kind",
        "metric_comparisons",
        "practice_only",
        "recommendation_id",
        "status",
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
_NO_ACTION_REASONS = (
    "NO_SUPPORTED_ACTION_DIAGNOSIS",
    "WAIT_CONDITION_DATA",
    "WAIT_HUMAN_LABELS",
    "CANDIDATE_NOT_GOLDEN",
)
_PROMOTION_BLOCKERS = (
    "WAIT_CONDITION_DATA",
    "WAIT_HUMAN_LABELS",
    "CANDIDATE_NOT_GOLDEN",
)


class CornerCardError(ValueError):
    """A stable fail-closed error raised for an inadmissible replay."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise CornerCardError(code, message)


def _canonical_json(value: object, *, newline: bool = False) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError):
        _fail("NON_CANONICAL_VALUE", "value is not canonical-JSON-safe")
    return encoded + (b"\n" if newline else b"")


def canonical_sha256(value: object) -> str:
    """Return the digest used by both driving replay and card receipts."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _strict_json(payload: bytes) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _fail("INVALID_JSON", f"input contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> NoReturn:
        _fail("INVALID_JSON", f"input contains invalid constant {value!r}")

    try:
        return json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except CornerCardError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("INVALID_JSON", f"input is not strict UTF-8 JSON: {exc}")


def _object(
    value: object,
    label: str,
    *,
    keys: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("INVALID_RECEIPT", f"{label} must be a plain object")
    result = value
    if keys is not None and set(result) != set(keys):
        _fail("INVALID_RECEIPT", f"{label} has unexpected or missing keys")
    return result


def _array(value: object, label: str) -> list[Any]:
    if type(value) is not list:
        _fail("INVALID_RECEIPT", f"{label} must be an array")
    return value


def _text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 for character in value)
    ):
        _fail("INVALID_RECEIPT", f"{label} must be a bounded non-empty string")
    return value


def _sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _SHA256_CHARS for character in value)
    ):
        _fail("INVALID_RECEIPT", f"{label} must be a lowercase SHA-256 digest")
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        suffix = f" >= {minimum}" if minimum is not None else ""
        _fail("INVALID_RECEIPT", f"{label} must be a plain integer{suffix}")
    return value


def _number(
    value: object,
    label: str,
    *,
    minimum: float | None = None,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("INVALID_RECEIPT", f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        _fail("INVALID_RECEIPT", f"{label} must be finite")
    if positive and result <= 0:
        _fail("INVALID_RECEIPT", f"{label} must be positive")
    if minimum is not None and result < minimum:
        _fail("INVALID_RECEIPT", f"{label} must be at least {minimum}")
    return result


def _optional_number(value: object, label: str) -> float | None:
    return None if value is None else _number(value, label)


def _ordinal_array(
    value: object,
    label: str,
    *,
    allowed: set[int] | None = None,
) -> tuple[int, ...]:
    items = tuple(_integer(item, f"{label} item", minimum=1) for item in _array(value, label))
    if len(items) != len(set(items)):
        _fail("INVALID_RECEIPT", f"{label} contains duplicate ordinals")
    if allowed is not None and not set(items).issubset(allowed):
        _fail("INVALID_RECEIPT", f"{label} contains an ineligible ordinal")
    return items


def _text_array(value: object, label: str, *, unique: bool = True) -> tuple[str, ...]:
    items = tuple(_text(item, f"{label} item") for item in _array(value, label))
    if unique and len(items) != len(set(items)):
        _fail("INVALID_RECEIPT", f"{label} contains duplicates")
    return items


def _validate_input_evidence(
    replay: Mapping[str, object], pipeline: Mapping[str, object]
) -> dict[str, Any]:
    kind = replay.get("input_kind")
    tick_rate = int(pipeline["tick_rate_hz"])
    if kind == "ibt":
        evidence = _object(
            replay.get("input_evidence"),
            "IBT input_evidence",
            keys=frozenset(
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
            ),
        )
        if (
            evidence.get("authenticity_status") != "HASHED_LOCAL_FILE_NOT_AUTHENTICATED"
            or evidence.get("completion_status") != "COMPLETE"
            or evidence.get("source_kind") != "IBT_OFFLINE"
        ):
            _fail("INVALID_RECEIPT", "IBT input evidence status is invalid")
        _integer(evidence.get("byte_size"), "IBT byte_size", minimum=1)
        _integer(evidence.get("record_count"), "IBT record_count", minimum=1)
        if _integer(evidence.get("tick_rate_hz"), "IBT tick_rate_hz", minimum=1) != tick_rate:
            _fail("INVALID_RECEIPT", "IBT tick rate does not match pipeline")
        _sha256(evidence.get("source_sha256"), "IBT source_sha256")
    elif kind == "collector":
        collector_keys = frozenset(
            {
                "authenticity_status",
                "capture_clock_regression_count",
                "capture_span_us",
                "collector_contract_version",
                "completion_status",
                "driver_info_key_count",
                "dropped_tick_count",
                "duplicate_conflict_count",
                "duplicate_sample_count",
                "event_record_count",
                "first_buffer_tick",
                "first_capture_monotonic_us",
                "frame_record_count",
                "last_buffer_tick",
                "last_capture_monotonic_us",
                "read_error_field_count",
                "read_error_frame_count",
                "records_sha256",
                "redacted_driver_info_path_count",
                "samples_seen",
                "schema_change_count",
                "schema_epoch_count",
                "schema_record_count",
                "semantic_record_count",
                "session_epoch_count",
                "session_id",
                "session_info_record_count",
                "session_info_scope_counts",
                "session_reset_count",
                "sim_mode",
                "source_id",
                "source_kind",
                "stale_event_count",
                "tick_rate_hz_values",
            }
        )
        evidence = _object(
            replay.get("input_evidence"), "collector input_evidence", keys=collector_keys
        )
        if (
            evidence.get("authenticity_status") != "SELF_CONSISTENT_NOT_AUTHENTICATED"
            or evidence.get("collector_contract_version") != "live-collector-v2"
            or evidence.get("completion_status") != "COMPLETE"
            or evidence.get("source_kind") not in {"SDK_LIVE", "REPLAY_SDK_PROXY"}
        ):
            _fail("INVALID_RECEIPT", "collector input evidence status is invalid")
        expected_mode = "full" if evidence["source_kind"] == "SDK_LIVE" else "replay"
        if evidence.get("sim_mode") != expected_mode:
            _fail("INVALID_RECEIPT", "collector source kind and sim mode disagree")
        _sha256(evidence.get("records_sha256"), "collector records_sha256")
        for name in collector_keys - {
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
        }:
            _integer(evidence.get(name), f"collector {name}", minimum=0)
        if int(evidence["frame_record_count"]) < 1:
            _fail("INVALID_RECEIPT", "collector evidence contains no frames")
        for name in (
            "capture_span_us",
            "first_buffer_tick",
            "first_capture_monotonic_us",
            "last_buffer_tick",
            "last_capture_monotonic_us",
        ):
            if evidence[name] is not None:
                _integer(evidence[name], f"collector {name}", minimum=0)
        rates = _array(evidence.get("tick_rate_hz_values"), "collector tick rates")
        if rates != [tick_rate]:
            _fail("INVALID_RECEIPT", "collector tick rate does not match pipeline")
        scopes = _object(evidence.get("session_info_scope_counts"), "collector scopes")
        if not set(scopes).issubset({"FULL", "PARTIAL", "UNAVAILABLE"}):
            _fail("INVALID_RECEIPT", "collector session scopes are invalid")
        for name, count in scopes.items():
            _integer(count, f"collector scope {name}", minimum=0)
    else:
        _fail("INVALID_RECEIPT", "input_kind must be ibt or collector")
    _text(evidence.get("source_id"), "input_evidence.source_id")
    _text(evidence.get("session_id"), "input_evidence.session_id")
    return evidence


def _validate_supporting_receipts(
    replay: Mapping[str, object],
    pipeline: Mapping[str, object],
    eligible: tuple[int, ...],
) -> None:
    evidence = _validate_input_evidence(replay, pipeline)
    event = _object(
        replay.get("event_receipt"),
        "event_receipt",
        keys=frozenset(
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
        ),
    )
    if event.get("contract_version") != "telemetry-events-v1":
        _fail("INVALID_RECEIPT", "event receipt contract is invalid")
    for name in ("config_sha256", "events_sha256", "receipt_sha256"):
        _sha256(event.get(name), f"event_receipt.{name}")
    counts = {
        name: _integer(event.get(name), f"event_receipt.{name}", minimum=0)
        for name in (
            "accepted_sample_count",
            "event_count",
            "rejected_sample_count",
            "sample_count",
            "session_epoch_count",
            "source_epoch_count",
        )
    }
    if (
        counts["accepted_sample_count"] + counts["rejected_sample_count"] != counts["sample_count"]
        or counts["rejected_sample_count"] != 0
        or counts["session_epoch_count"] != 1
        or counts["source_epoch_count"] != 1
    ):
        _fail("INVALID_RECEIPT", "event receipt counts do not close for a PASS replay")
    kinds = _object(event.get("event_kind_counts"), "event kind counts")
    measured_events = 0
    for name, count in kinds.items():
        _text(name, "event kind")
        measured_events += _integer(count, f"event kind {name}", minimum=0)
    if measured_events != counts["event_count"]:
        _fail("INVALID_RECEIPT", "event kind counts do not close")
    if event["receipt_sha256"] != canonical_sha256(
        {key: value for key, value in event.items() if key != "receipt_sha256"}
    ):
        _fail("INVALID_RECEIPT", "event receipt digest does not match")

    normalized = _object(
        replay.get("normalized_input_receipt"),
        "normalized_input_receipt",
        keys=frozenset({"contract_version", "sample_count", "samples_sha256"}),
    )
    semantic = _object(
        replay.get("semantic_input_receipt"),
        "semantic_input_receipt",
        keys=frozenset({"channels", "contract_version", "sample_count", "samples_sha256"}),
    )
    if (
        normalized.get("contract_version") != "normalized-telemetry-v3"
        or semantic.get("contract_version") != "driving-semantic-input-v1"
        or semantic.get("channels") != list(_SEMANTIC_CHANNELS)
    ):
        _fail("INVALID_RECEIPT", "normalized or semantic receipt contract is invalid")
    _sha256(normalized.get("samples_sha256"), "normalized samples SHA")
    _sha256(semantic.get("samples_sha256"), "semantic samples SHA")
    normalized_count = _integer(
        normalized.get("sample_count"), "normalized sample count", minimum=1
    )
    semantic_count = _integer(semantic.get("sample_count"), "semantic sample count", minimum=1)
    if (
        normalized_count != counts["sample_count"]
        or semantic_count != counts["accepted_sample_count"]
    ):
        _fail("INVALID_RECEIPT", "event and semantic sample counts disagree")
    source_count = (
        int(evidence["record_count"])
        if replay["input_kind"] == "ibt"
        else int(evidence["frame_record_count"])
    )
    if source_count != counts["sample_count"]:
        _fail("INVALID_RECEIPT", "source evidence and event sample counts disagree")

    lap = _object(
        replay.get("lap_receipt"),
        "lap_receipt",
        keys=frozenset(
            {
                "algorithm_version",
                "clean_driving_lap_count",
                "cleanliness_observable_lap_count",
                "lap_count",
                "laps_sha256",
                "modeled_sample_count",
                "quality_complete_lap_count",
                "structurally_complete_lap_count",
            }
        ),
    )
    if lap.get("algorithm_version") != "distance-wrap-v2":
        _fail("INVALID_RECEIPT", "lap receipt algorithm is invalid")
    _sha256(lap.get("laps_sha256"), "lap receipt digest")
    lap_count = _integer(lap.get("lap_count"), "lap_count", minimum=1)
    for name in (
        "clean_driving_lap_count",
        "cleanliness_observable_lap_count",
        "quality_complete_lap_count",
        "structurally_complete_lap_count",
    ):
        count = _integer(lap.get(name), f"lap_receipt.{name}", minimum=0)
        if count > lap_count:
            _fail("INVALID_RECEIPT", "lap receipt count exceeds lap_count")
    if (
        int(lap["clean_driving_lap_count"]) != len(eligible)
        or _integer(lap.get("modeled_sample_count"), "lap modeled sample count", minimum=1)
        != semantic_count
    ):
        _fail("INVALID_RECEIPT", "lap receipt does not bind the modeled laps/samples")

    series = _object(
        replay.get("series_evidence"),
        "series_evidence",
        keys=frozenset(
            {
                "analysis_refusal_reasons",
                "degraded_sample_count",
                "incident_regression_channels",
                "incident_source_field",
                "missing_channel_sample_counts",
                "modeled_sample_count",
                "normalized_dropped_tick_count",
                "quality_issue_counts",
                "segmentation_error",
            }
        ),
    )
    if _text_array(series.get("analysis_refusal_reasons"), "analysis refusal reasons"):
        _fail("INVALID_RECEIPT", "PASS replay cannot contain analysis refusal reasons")
    if _text_array(series.get("incident_regression_channels"), "incident regressions"):
        _fail("INVALID_RECEIPT", "PASS replay cannot contain incident regressions")
    _text(series.get("incident_source_field"), "incident source field")
    if series.get("segmentation_error") is not None:
        _fail("INVALID_RECEIPT", "PASS replay cannot contain a segmentation error")
    for name in ("degraded_sample_count", "normalized_dropped_tick_count"):
        _integer(series.get(name), f"series_evidence.{name}", minimum=0)
    if (
        _integer(series.get("modeled_sample_count"), "series modeled sample count", minimum=1)
        != semantic_count
        or int(series["normalized_dropped_tick_count"]) != 0
    ):
        _fail("INVALID_RECEIPT", "series evidence does not match PASS modeled samples")
    for label in ("missing_channel_sample_counts", "quality_issue_counts"):
        values = _object(series.get(label), label)
        for name, count in values.items():
            _text(name, f"{label} key")
            _integer(count, f"{label}.{name}", minimum=0)


def _validate_capabilities(
    value: object,
    *,
    evidence_ids: list[str],
) -> None:
    capabilities = _object(
        value,
        "capabilities",
        keys=frozenset(
            {
                "curb_guidance",
                "current_tire_wear",
                "driving_model_shadow",
                "personalized_coaching",
                "race_coaching",
                "traffic_model",
            }
        ),
    )
    expected_unavailable = {
        "curb_guidance": (
            ["CURB_GEOMETRY_NOT_MODELED"],
            ["CURB_RECOMMENDATION"],
        ),
        "current_tire_wear": (
            ["CURRENT_STINT_TIRE_WEAR_MODEL_NOT_IMPLEMENTED"],
            ["CURRENT_TIRE_WEAR_CLAIM"],
        ),
        "personalized_coaching": (
            [
                "CONDITION_COHORT_NOT_ATTACHED",
                "MATCHED_CONTEXT_HISTORY_UNAVAILABLE",
                "HUMAN_CORNER_LABELS_MISSING",
            ],
            ["PERSONALIZED_ACTION", "CAUSAL_GAIN_CLAIM", "TRAIL_BRAKING_CLAIM"],
        ),
        "traffic_model": (
            ["TRAFFIC_MODEL_NOT_IMPLEMENTED"],
            ["REJOIN_TRAFFIC_CLAIM"],
        ),
    }
    for name, (reasons, blocked_claims) in expected_unavailable.items():
        item = _object(
            capabilities.get(name), f"{name} capability", keys=_UNAVAILABLE_CAPABILITY_KEYS
        )
        expected = {
            "blocked_claims": blocked_claims,
            "confidence": "NONE",
            "contract_version": "inference-capability-v1",
            "estimate_available": False,
            "provenance": "UNKNOWN",
            "reasons": reasons,
            "status": "SKIP",
        }
        if item != expected:
            _fail("CAPABILITY_GATE_INVALID", f"{name} capability is not fail-closed")
    driving = _object(
        capabilities.get("driving_model_shadow"),
        "driving_model_shadow capability",
        keys=frozenset({"evidence_ids", "reasons", "status"}),
    )
    if driving != {"evidence_ids": evidence_ids, "reasons": [], "status": "PASS"}:
        _fail("CAPABILITY_GATE_INVALID", "driving shadow capability is invalid")
    race = _object(
        capabilities.get("race_coaching"),
        "race_coaching capability",
        keys=frozenset({"reasons", "status"}),
    )
    if race != {
        "reasons": [
            "SHADOW_ONLY",
            "PERSONALIZED_COACHING_UNAVAILABLE",
            "TRAFFIC_MODEL_NOT_IMPLEMENTED",
        ],
        "status": "BLOCKED",
    }:
        _fail("CAPABILITY_GATE_INVALID", "race coaching capability is invalid")


def _validate_recommendations(
    value: object,
    *,
    diagnoses: tuple[dict[str, Any], ...],
    evidence_prefix: str,
    lap_algorithm: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    diagnosis_by_key = {
        (str(item["corner_id"]), str(item["diagnosis"])): item for item in diagnoses
    }
    supported: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw in enumerate(_array(value, "recommendations")):
        recommendation = _object(raw, f"recommendation {index}", keys=_RECOMMENDATION_KEYS)
        corner_id = _text(recommendation.get("corner_id"), "recommendation corner_id")
        code = _text(recommendation.get("diagnosis"), "recommendation diagnosis")
        key = (corner_id, code)
        diagnosis = diagnosis_by_key.get(key)
        if diagnosis is None or key in supported:
            _fail("DIAGNOSIS_INVALID", "recommendation is not uniquely bound to a diagnosis")
        expected_evidence = [
            _lap_id(evidence_prefix, lap_algorithm, int(ordinal))
            for ordinal in diagnosis["evidence_lap_ordinals"]
        ]
        expected_counterexamples = [
            _lap_id(evidence_prefix, lap_algorithm, int(ordinal))
            for ordinal in diagnosis["counterexample_lap_ordinals"]
        ]
        expected = {
            "action": diagnosis["action"],
            "claim_level": "descriptive",
            "confidence": str(diagnosis["confidence"]).upper(),
            "confidence_basis": {
                "causal_validity": "NOT_CLAIMED",
                "external_validity": "UNKNOWN",
            },
            "corner_id": corner_id,
            "counterexample_lap_ids": expected_counterexamples,
            "diagnosis": code,
            "estimated_loss_us": round(float(diagnosis["estimated_loss_median_s"]) * 1_000_000),
            "evidence_lap_ids": expected_evidence,
            "executable": False,
            "expected_gain_range_us": [
                round(float(item) * 1_000_000) for item in diagnosis["expected_gain_range_s"]
            ],
            "kind": "DRIVING_CANDIDATE",
            "metric_comparisons": diagnosis["comparisons"],
            "practice_only": True,
            "recommendation_id": f"driving:{corner_id}:{code}",
            "status": "SHADOW_ONLY",
        }
        if recommendation != expected:
            _fail("DIAGNOSIS_INVALID", "recommendation does not exactly bind its diagnosis")
        supported[key] = diagnosis
    return supported


def _validate_context(replay: Mapping[str, object]) -> dict[str, Any]:
    context = _object(
        replay.get("driving_context"),
        "driving_context",
        keys=frozenset(
            {
                "availability",
                "context_sha256",
                "contract_version",
                "provenance",
                "source_binding_sha256",
                "source_field",
                "status",
                "track_length_mm",
            }
        ),
    )
    if (
        context.get("availability") != "AVAILABLE"
        or context.get("status") != "VERIFIED"
        or context.get("contract_version") != "track-context-v1"
        or context.get("source_field") != "WeekendInfo.TrackLength"
    ):
        _fail("TRACK_CONTEXT_NOT_VERIFIED", "driving track context is not verified")
    _integer(context.get("track_length_mm"), "track_length_mm", minimum=100_001)
    source_binding = _sha256(context.get("source_binding_sha256"), "source_binding_sha256")
    input_evidence = _object(replay.get("input_evidence"), "input_evidence")
    if source_binding != canonical_sha256(input_evidence):
        _fail(
            "SOURCE_BINDING_SHA256_MISMATCH",
            "track context source binding does not match input evidence",
        )
    context_sha = _sha256(context.get("context_sha256"), "context_sha256")
    context_material = {key: value for key, value in context.items() if key != "context_sha256"}
    if context_sha != canonical_sha256(context_material):
        _fail("CONTEXT_SHA256_MISMATCH", "track context digest does not match")
    if replay.get("driving_context_sha256") != context_sha:
        _fail("CONTEXT_SHA256_MISMATCH", "outer driving context digest does not match")
    return context


def _validate_pipeline(replay: Mapping[str, object]) -> tuple[dict[str, Any], float, int]:
    pipeline = _object(
        replay.get("pipeline"),
        "pipeline",
        keys=frozenset(
            {
                "pipeline_sha256",
                "driving_algorithm_version",
                "driving_config",
                "event_contract_version",
                "feature_pipeline_version",
                "lap_algorithm_version",
                "normalization",
                "normalized_telemetry_contract_version",
                "semantic_input_contract_version",
                "tick_rate_hz",
            }
        ),
    )
    pipeline_sha = _sha256(pipeline.get("pipeline_sha256"), "pipeline_sha256")
    material = {key: value for key, value in pipeline.items() if key != "pipeline_sha256"}
    if pipeline_sha != canonical_sha256(material):
        _fail("PIPELINE_SHA256_MISMATCH", "pipeline digest does not match")
    if (
        pipeline.get("lap_algorithm_version") != "distance-wrap-v2"
        or pipeline.get("driving_algorithm_version") != "distance-driving-v1"
        or pipeline.get("event_contract_version") != "telemetry-events-v1"
        or pipeline.get("feature_pipeline_version") != "normalized-lap-driving-v1"
        or pipeline.get("normalized_telemetry_contract_version") != "normalized-telemetry-v3"
        or pipeline.get("semantic_input_contract_version") != "driving-semantic-input-v1"
    ):
        _fail("PIPELINE_VERSION_MISMATCH", "driving pipeline versions are not supported")
    tick_rate = _integer(pipeline.get("tick_rate_hz"), "pipeline.tick_rate_hz", minimum=1)
    if tick_rate > 360:
        _fail("INVALID_RECEIPT", "pipeline.tick_rate_hz must not exceed 360")
    normalization = _object(
        pipeline.get("normalization"),
        "pipeline.normalization",
        keys=frozenset({"opponent_error_policy", "profile_version", "stale_after_us"}),
    )
    if (
        normalization.get("opponent_error_policy") not in {"degrade", "reject"}
        or normalization.get("profile_version") != "normalized-sdk-adapter-v3"
    ):
        _fail("PIPELINE_VERSION_MISMATCH", "normalization profile is not supported")
    _integer(normalization.get("stale_after_us"), "normalization.stale_after_us", minimum=1)

    config = _object(pipeline.get("driving_config"), "driving_config", keys=_DRIVING_CONFIG_KEYS)
    grid_step = _number(config.get("grid_step_m"), "driving_config.grid_step_m", positive=True)
    if not 0.25 <= grid_step <= 10.0:
        _fail("INVALID_RECEIPT", "driving_config.grid_step_m is out of range")
    _integer(config.get("min_clean_laps"), "driving_config.min_clean_laps", minimum=3)
    _integer(
        config.get("min_reference_group_laps"),
        "driving_config.min_reference_group_laps",
        minimum=2,
    )
    minimum_loss = _number(
        config.get("minimum_loss_s"), "driving_config.minimum_loss_s", positive=True
    )
    minimum_evidence = _integer(
        config.get("min_evidence_laps"),
        "driving_config.min_evidence_laps",
        minimum=1,
    )
    for name in _DRIVING_CONFIG_KEYS - {
        "grid_step_m",
        "min_clean_laps",
        "min_reference_group_laps",
        "min_evidence_laps",
        "minimum_loss_s",
    }:
        _number(config.get(name), f"driving_config.{name}", positive=True)
    fastest_fraction = float(config["fastest_group_fraction"])
    if fastest_fraction > 1.0:
        _fail("INVALID_RECEIPT", "driving_config.fastest_group_fraction exceeds one")
    for name in (
        "brake_threshold",
        "brake_release_threshold",
        "throttle_pickup_threshold",
        "second_lift_threshold",
        "max_reference_duration_spread_fraction",
    ):
        if float(config[name]) > 1.0:
            _fail("INVALID_RECEIPT", f"driving_config.{name} exceeds one")
    if float(config["brake_release_threshold"]) >= float(config["brake_threshold"]):
        _fail("INVALID_RECEIPT", "brake release threshold must be below brake threshold")
    return pipeline, minimum_loss, minimum_evidence


def _validate_model(
    replay: Mapping[str, object],
    context: Mapping[str, object],
    pipeline: Mapping[str, object],
    minimum_evidence: int,
) -> tuple[
    dict[str, Any],
    tuple[int, ...],
    int,
    tuple[str, ...],
    dict[tuple[str, int], dict[str, Any]],
    tuple[dict[str, Any], ...],
]:
    model = _object(replay.get("model_output"), "model_output", keys=_MODEL_KEYS)
    expected_model_sha = _sha256(replay.get("model_output_sha256"), "model_output_sha256")
    if canonical_sha256(model) != expected_model_sha:
        _fail("MODEL_OUTPUT_SHA256_MISMATCH", "model output digest does not match")
    if model.get("status") != "READY" or model.get("refusal_reasons") != []:
        _fail("MODEL_NOT_READY", "driving model output is not READY")
    if model.get("algorithm_version") != pipeline.get("driving_algorithm_version"):
        _fail("PIPELINE_VERSION_MISMATCH", "model and pipeline algorithm versions differ")
    track_length = _number(model.get("track_length_m"), "track_length_m", positive=True)
    if abs(track_length * 1_000 - int(context["track_length_mm"])) > 1e-6:
        _fail("TRACK_LENGTH_MISMATCH", "model and source-bound track lengths differ")
    grid_step = _number(model.get("grid_step_m"), "grid_step_m", positive=True)
    driving_config = _object(pipeline.get("driving_config"), "driving_config")
    if grid_step != float(driving_config["grid_step_m"]):
        _fail("PIPELINE_VERSION_MISMATCH", "model and pipeline grid steps differ")
    closure_tolerance = _number(
        driving_config.get("lap_delta_closure_tolerance_s"),
        "driving_config.lap_delta_closure_tolerance_s",
        positive=True,
    )
    if closure_tolerance > 1e-6:
        _fail(
            "LAP_DELTA_CLOSURE_FAILED",
            "lap closure tolerance exceeds the verifier safety bound",
        )

    eligible = _ordinal_array(model.get("eligible_lap_ordinals"), "eligible laps")
    if len(eligible) < 2:
        _fail("INSUFFICIENT_COMPARISON_LAPS", "at least two eligible laps are required")
    eligible_set = set(eligible)
    reference = _object(
        model.get("reference"),
        "reference",
        keys=frozenset(
            {
                "duration_spread_fraction",
                "fastest_group_lap_ordinals",
                "lap_ordinal",
                "trace_median_absolute_error_s",
            }
        ),
    )
    reference_ordinal = _integer(reference.get("lap_ordinal"), "reference lap", minimum=1)
    if reference_ordinal not in eligible_set:
        _fail("REFERENCE_LAP_INVALID", "reference lap is not eligible")
    _ordinal_array(
        reference.get("fastest_group_lap_ordinals"),
        "fastest group laps",
        allowed=eligible_set,
    )
    _number(reference.get("duration_spread_fraction"), "reference duration spread", minimum=0)
    _number(
        reference.get("trace_median_absolute_error_s"),
        "reference trace error",
        minimum=0,
    )

    corners_raw = _array(model.get("corners"), "corners")
    if not corners_raw:
        _fail("NO_CORNER_WINDOWS", "driving model contains no corner windows")
    corner_ids: list[str] = []
    expected_start = 0.0
    partition_tolerance = max(1e-9, grid_step * 1e-6)
    for index, raw in enumerate(corners_raw):
        corner = _object(raw, f"corner {index}", keys=_CORNER_KEYS)
        corner_id = _text(corner.get("corner_id"), f"corner {index} ID")
        if corner_id in corner_ids:
            _fail("CORNER_PARTITION_INVALID", "corner IDs are not unique")
        start = _number(corner.get("accounting_start_m"), f"{corner_id} accounting start")
        carry_end = _number(corner.get("carry_end_m"), f"{corner_id} carry end")
        if abs(start - expected_start) > partition_tolerance or carry_end <= start:
            _fail("CORNER_PARTITION_INVALID", "corner accounting windows do not partition")
        for field in (
            "approach_start_m",
            "brake_start_m",
            "brake_end_m",
            "apex_m",
            "exit_m",
        ):
            _number(corner.get(field), f"{corner_id} {field}")
        corner_ids.append(corner_id)
        expected_start = carry_end
    if abs(expected_start - track_length) > partition_tolerance:
        _fail("CORNER_PARTITION_INVALID", "final corner window does not end at track length")

    summaries: dict[int, dict[str, Any]] = {}
    for raw in _array(model.get("lap_summaries"), "lap_summaries"):
        summary = _object(
            raw,
            "lap summary",
            keys=frozenset(
                {"duration_s", "in_fastest_group", "is_reference", "lap_delta_s", "lap_ordinal"}
            ),
        )
        ordinal = _integer(summary.get("lap_ordinal"), "lap summary ordinal", minimum=1)
        if ordinal not in eligible_set or ordinal in summaries:
            _fail("LAP_SUMMARY_INVALID", "lap summaries do not match eligible laps")
        _number(summary.get("duration_s"), "lap duration", positive=True)
        _number(summary.get("lap_delta_s"), "lap delta")
        if (
            type(summary.get("is_reference")) is not bool
            or type(summary.get("in_fastest_group")) is not bool
        ):
            _fail("LAP_SUMMARY_INVALID", "lap summary flags must be booleans")
        if bool(summary["is_reference"]) != (ordinal == reference_ordinal):
            _fail("LAP_SUMMARY_INVALID", "reference flag does not match reference lap")
        summaries[ordinal] = summary
    if set(summaries) != eligible_set:
        _fail("LAP_SUMMARY_INVALID", "lap summaries are incomplete")
    reference_duration = float(summaries[reference_ordinal]["duration_s"])
    for summary in summaries.values():
        recomputed_delta = float(summary["duration_s"]) - reference_duration
        if abs(recomputed_delta - float(summary["lap_delta_s"])) > 1e-6:
            _fail(
                "LAP_DELTA_CLOSURE_FAILED",
                "lap delta does not close to the reference lap duration",
            )

    closures: dict[int, dict[str, Any]] = {}
    for raw in _array(model.get("delta_closures"), "delta_closures"):
        closure = _object(
            raw,
            "delta closure",
            keys=frozenset(
                {
                    "actual_lap_delta_s",
                    "closed",
                    "lap_ordinal",
                    "residual_s",
                    "summed_window_delta_s",
                    "tolerance_s",
                }
            ),
        )
        ordinal = _integer(closure.get("lap_ordinal"), "closure lap ordinal", minimum=1)
        if ordinal not in eligible_set or ordinal in closures:
            _fail("LAP_DELTA_CLOSURE_FAILED", "closures do not match eligible laps")
        actual = _number(closure.get("actual_lap_delta_s"), "actual lap delta")
        summed = _number(closure.get("summed_window_delta_s"), "summed window delta")
        residual = _number(closure.get("residual_s"), "closure residual")
        tolerance = _number(closure.get("tolerance_s"), "closure tolerance", positive=True)
        if (
            tolerance != closure_tolerance
            or closure.get("closed") is not True
            or abs(residual) > tolerance
            or abs((summed - actual) - residual) > max(tolerance, 1e-9)
            or abs(actual - float(summaries[ordinal]["lap_delta_s"])) > max(tolerance, 1e-9)
        ):
            _fail("LAP_DELTA_CLOSURE_FAILED", "stored lap loss closure is invalid")
        closures[ordinal] = closure
    if set(closures) != eligible_set:
        _fail("LAP_DELTA_CLOSURE_FAILED", "lap closures are incomplete")

    metric_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for raw in _array(model.get("corner_metrics"), "corner_metrics"):
        metric = _object(raw, "corner metric", keys=_METRIC_KEYS)
        corner_id = _text(metric.get("corner_id"), "metric corner ID")
        ordinal = _integer(metric.get("lap_ordinal"), "metric lap ordinal", minimum=1)
        key = (corner_id, ordinal)
        if corner_id not in corner_ids or ordinal not in eligible_set or key in metric_by_key:
            _fail("CORNER_METRICS_INVALID", "corner metric grid is invalid")
        for field in (
            "entry_speed_mps",
            "apex_m",
            "apex_speed_mps",
            "exit_speed_mps",
            "delta_at_accounting_start_s",
            "delta_at_entry_s",
            "delta_at_apex_s",
            "delta_at_exit_s",
            "approach_delta_s",
            "local_delta_s",
            "carry_delta_s",
            "total_segment_delta_s",
            "accounted_window_delta_s",
        ):
            _number(metric.get(field), f"metric {corner_id}/{ordinal} {field}")
        for field in (
            "brake_onset_m",
            "brake_release_m",
            "throttle_pickup_m",
            "coast_distance_m",
        ):
            _optional_number(metric.get(field), f"metric {corner_id}/{ordinal} {field}")
        if metric.get("second_lift") is not None and type(metric.get("second_lift")) is not bool:
            _fail("CORNER_METRICS_INVALID", "second_lift must be boolean or null")
        approach = float(metric["approach_delta_s"])
        local = float(metric["local_delta_s"])
        carry = float(metric["carry_delta_s"])
        total = float(metric["total_segment_delta_s"])
        accounted = float(metric["accounted_window_delta_s"])
        if (
            abs((local + carry) - total) > 1e-8
            or abs((approach + local + carry) - accounted) > 1e-8
        ):
            _fail("CORNER_PHASE_CLOSURE_FAILED", "corner phase deltas do not close")
        metric_by_key[key] = metric
    expected_metric_keys = {
        (corner_id, ordinal) for corner_id in corner_ids for ordinal in eligible
    }
    if set(metric_by_key) != expected_metric_keys:
        _fail("CORNER_METRICS_INVALID", "corner metric grid is incomplete")

    for ordinal in eligible:
        summed = math.fsum(
            float(metric_by_key[(corner_id, ordinal)]["accounted_window_delta_s"])
            for corner_id in corner_ids
        )
        tolerance = float(closures[ordinal]["tolerance_s"])
        if abs(summed - float(closures[ordinal]["summed_window_delta_s"])) > max(tolerance, 1e-9):
            _fail("LAP_DELTA_CLOSURE_FAILED", "corner metrics do not match lap closure")

    diagnoses: list[dict[str, Any]] = []
    diagnosis_keys: set[tuple[str, str]] = set()
    non_reference = eligible_set - {reference_ordinal}
    for raw in _array(model.get("diagnoses"), "diagnoses"):
        diagnosis = _object(raw, "diagnosis", keys=_DIAGNOSIS_KEYS)
        corner_id = _text(diagnosis.get("corner_id"), "diagnosis corner ID")
        if corner_id not in corner_ids:
            _fail("DIAGNOSIS_INVALID", "diagnosis references an unknown corner")
        code = _text(diagnosis.get("diagnosis"), "diagnosis code")
        if code not in _DIAGNOSIS_ACTIONS:
            _fail("DIAGNOSIS_INVALID", "diagnosis code is not produced by the frozen rule set")
        diagnosis_key = (corner_id, code)
        if diagnosis_key in diagnosis_keys:
            _fail("DIAGNOSIS_INVALID", "diagnosis keys are not unique")
        diagnosis_keys.add(diagnosis_key)
        action = _text(diagnosis.get("action"), "diagnosis action")
        if action != _DIAGNOSIS_ACTIONS[code]:
            _fail("DIAGNOSIS_INVALID", "diagnosis action does not match the frozen rule set")
        if (
            diagnosis.get("claim_level") != "descriptive"
            or diagnosis.get("practice_only") is not True
        ):
            _fail("DIAGNOSIS_INVALID", "diagnosis exceeds descriptive practice scope")
        if diagnosis.get("confidence") not in {"medium", "high"}:
            _fail("DIAGNOSIS_INVALID", "diagnosis confidence is invalid")
        evidence = _ordinal_array(
            diagnosis.get("evidence_lap_ordinals"),
            "diagnosis evidence laps",
            allowed=non_reference,
        )
        counterexamples = _ordinal_array(
            diagnosis.get("counterexample_lap_ordinals"),
            "diagnosis counterexample laps",
            allowed=non_reference,
        )
        if len(evidence) < minimum_evidence or set(evidence).intersection(counterexamples):
            _fail(
                "DIAGNOSIS_INVALID",
                "diagnosis evidence is insufficient or overlaps counterexamples",
            )
        if set(counterexamples) != non_reference - set(evidence):
            _fail("DIAGNOSIS_INVALID", "diagnosis counterexamples are incomplete")
        loss = _number(
            diagnosis.get("estimated_loss_median_s"),
            "diagnosis estimated loss",
            minimum=0,
        )
        gain = _array(diagnosis.get("expected_gain_range_s"), "expected gain range")
        if len(gain) != 2:
            _fail("DIAGNOSIS_INVALID", "expected gain range must contain two values")
        low = _number(gain[0], "expected gain lower bound", minimum=0)
        high = _number(gain[1], "expected gain upper bound", minimum=0)
        if low > high or high > loss + 1e-9:
            _fail("DIAGNOSIS_INVALID", "expected gain range is invalid")
        comparisons = _array(diagnosis.get("comparisons"), "diagnosis comparisons")
        expected_comparisons = _DIAGNOSIS_COMPARISONS[code]
        if len(comparisons) != len(expected_comparisons):
            _fail("DIAGNOSIS_INVALID", "diagnosis comparisons do not match the frozen rule set")
        for comparison_raw, (expected_metric, expected_unit) in zip(
            comparisons, expected_comparisons, strict=True
        ):
            comparison = _object(comparison_raw, "diagnosis comparison", keys=_COMPARISON_KEYS)
            if (
                _text(comparison.get("metric"), "comparison metric") != expected_metric
                or _text(comparison.get("unit"), "comparison unit") != expected_unit
            ):
                _fail("DIAGNOSIS_INVALID", "diagnosis comparison metric is invalid")
            for field in ("evidence_median", "reference_value", "difference"):
                _number(comparison.get(field), f"comparison {field}")
            if (
                abs(
                    float(comparison["evidence_median"])
                    - float(comparison["reference_value"])
                    - float(comparison["difference"])
                )
                > 1e-9
            ):
                _fail("DIAGNOSIS_INVALID", "diagnosis comparison does not close")
        diagnoses.append(diagnosis)

    return (
        model,
        eligible,
        reference_ordinal,
        tuple(corner_ids),
        metric_by_key,
        tuple(diagnoses),
    )


def validate_driving_replay(replay: object) -> dict[str, Any]:
    """Validate every receipt layer needed by the loss-card report."""

    payload = _object(replay, "driving replay", keys=_REPLAY_KEYS)
    if payload.get("contract_version") != DRIVING_REPLAY_CONTRACT_VERSION:
        _fail("CONTRACT_VERSION_MISMATCH", "unsupported driving replay contract")
    replay_sha = _sha256(payload.get("driving_replay_sha256"), "driving_replay_sha256")
    binding = {key: payload[key] for key in _REPLAY_BINDING_KEYS}
    if canonical_sha256(binding) != replay_sha:
        _fail("DRIVING_REPLAY_SHA256_MISMATCH", "complete driving replay digest does not match")

    quality = _object(
        payload.get("quality_gate"),
        "quality_gate",
        keys=frozenset({"reasons", "status"}),
    )
    if quality != {"reasons": [], "status": "PASS"} or payload.get("readiness_status") != "PASS":
        _fail("QUALITY_GATE_NOT_PASS", "driving replay quality/readiness gate is not PASS")

    context = _validate_context(payload)
    pipeline, minimum_loss, minimum_evidence = _validate_pipeline(payload)
    model_data = _validate_model(payload, context, pipeline, minimum_evidence)
    _validate_supporting_receipts(payload, pipeline, model_data[1])

    input_kind = payload.get("input_kind")
    input_binding = {
        "driving_context": context,
        "event_receipt": payload["event_receipt"],
        "input_evidence": payload["input_evidence"],
        "input_kind": input_kind,
        "normalized_input_receipt": payload["normalized_input_receipt"],
    }
    input_sha = _sha256(payload.get("input_provenance_sha256"), "input provenance SHA")
    if canonical_sha256(input_binding) != input_sha:
        _fail("INPUT_PROVENANCE_SHA256_MISMATCH", "input provenance digest does not match")

    semantic_binding = {
        "driving_context": {
            "contract_version": context["contract_version"],
            "source_field": context["source_field"],
            "track_length_mm": context["track_length_mm"],
        },
        "lap_receipt": payload["lap_receipt"],
        "model_output": payload["model_output"],
        "pipeline": payload["pipeline"],
        "quality_gate": payload["quality_gate"],
        "readiness_status": payload["readiness_status"],
        "semantic_input_receipt": payload["semantic_input_receipt"],
    }
    semantic_sha = _sha256(payload.get("model_semantic_sha256"), "model semantic SHA")
    if canonical_sha256(semantic_binding) != semantic_sha:
        _fail("MODEL_SEMANTIC_SHA256_MISMATCH", "model semantic digest does not match")

    lap_algorithm = str(pipeline["lap_algorithm_version"])
    evidence_ids = [_lap_id(input_sha, lap_algorithm, int(ordinal)) for ordinal in model_data[1]]
    _validate_capabilities(payload.get("capabilities"), evidence_ids=evidence_ids)
    supported_actions = _validate_recommendations(
        payload.get("recommendations"),
        diagnoses=model_data[5],
        evidence_prefix=input_sha,
        lap_algorithm=lap_algorithm,
    )

    return {
        "context": context,
        "diagnoses": model_data[5],
        "eligible": model_data[1],
        "input_provenance_sha256": input_sha,
        "metric_by_key": model_data[4],
        "minimum_evidence_laps": minimum_evidence,
        "minimum_loss_s": minimum_loss,
        "model": model_data[0],
        "model_semantic_sha256": semantic_sha,
        "reference_ordinal": model_data[2],
        "replay": payload,
        "replay_sha256": replay_sha,
        "corner_ids": model_data[3],
        "supported_actions": supported_actions,
    }


def _lap_id(prefix: str, lap_algorithm: str, ordinal: int) -> str:
    return f"{prefix}:{lap_algorithm}:lap:{ordinal}"


def build_corner_cards(replay: object, *, top: int = MAX_CARDS) -> dict[str, object]:
    """Return at most three descriptive, non-executable corner loss cards."""

    if type(top) is not int or not 1 <= top <= MAX_CARDS:
        raise ValueError(f"top must be a plain integer from 1 to {MAX_CARDS}")
    validated = validate_driving_replay(replay)
    payload = validated["replay"]
    assert isinstance(payload, dict)
    pipeline = payload["pipeline"]
    assert isinstance(pipeline, dict)
    lap_algorithm = str(pipeline["lap_algorithm_version"])
    prefix = str(validated["input_provenance_sha256"])
    eligible = tuple(validated["eligible"])
    reference = int(validated["reference_ordinal"])
    comparison_laps = tuple(ordinal for ordinal in eligible if ordinal != reference)
    minimum_loss = float(validated["minimum_loss_s"])
    minimum_evidence = int(validated["minimum_evidence_laps"])
    metric_by_key = validated["metric_by_key"]
    assert isinstance(metric_by_key, dict)
    supported_actions = validated["supported_actions"]
    assert isinstance(supported_actions, dict)

    candidates: list[tuple[float, str, list[dict[str, Any]], list[int]]] = []
    for corner_id in validated["corner_ids"]:
        metrics = [metric_by_key[(corner_id, ordinal)] for ordinal in comparison_laps]
        losses = [float(metric["accounted_window_delta_s"]) for metric in metrics]
        support = [
            metric
            for metric in metrics
            if float(metric["accounted_window_delta_s"]) >= minimum_loss
        ]
        median_loss = float(statistics.median(losses))
        if median_loss < minimum_loss or len(support) < minimum_evidence:
            continue
        candidates.append(
            (median_loss, str(corner_id), metrics, [int(item["lap_ordinal"]) for item in support])
        )
    candidates.sort(key=lambda item: (-item[0], item[1]))

    cards: list[dict[str, object]] = []
    for rank, (median_loss, corner_id, metrics, support_ordinals) in enumerate(
        candidates[:top], start=1
    ):
        corner_diagnoses = [
            item
            for (action_corner_id, _), item in supported_actions.items()
            if action_corner_id == corner_id
        ]
        corner_diagnoses.sort(
            key=lambda item: (-float(item["estimated_loss_median_s"]), str(item["diagnosis"]))
        )
        diagnosis = corner_diagnoses[0] if corner_diagnoses else None
        per_lap: list[dict[str, object]] = []
        for metric in sorted(metrics, key=lambda item: int(item["lap_ordinal"])):
            approach = float(metric["approach_delta_s"])
            local = float(metric["local_delta_s"])
            carry = float(metric["carry_delta_s"])
            accounted = float(metric["accounted_window_delta_s"])
            residual = approach + local + carry - accounted
            per_lap.append(
                {
                    "accounted_window_delta_s": accounted,
                    "approach_delta_s": approach,
                    "carry_delta_s": carry,
                    "lap_id": _lap_id(prefix, lap_algorithm, int(metric["lap_ordinal"])),
                    "lap_ordinal": int(metric["lap_ordinal"]),
                    "local_delta_s": local,
                    "phase_closed": abs(residual) <= 1e-8,
                    "phase_residual_s": residual,
                }
            )
        positive_ordinals = [
            int(metric["lap_ordinal"])
            for metric in metrics
            if float(metric["accounted_window_delta_s"]) > 0
        ]
        counterexample_ordinals = [
            int(metric["lap_ordinal"])
            for metric in metrics
            if float(metric["accounted_window_delta_s"]) <= 0
        ]
        cards.append(
            {
                "action": str(diagnosis["action"]) if diagnosis is not None else None,
                "claim_level": "descriptive",
                "confidence": "LOW",
                "confidence_basis": {
                    "causal_validity": "NOT_CLAIMED",
                    "condition_matching": "WAIT_CONDITION_DATA",
                    "corner_identity": "CANDIDATE_NOT_GOLDEN",
                    "external_validity": "UNKNOWN",
                    "human_labels": "WAIT_HUMAN_LABELS",
                },
                "corner_id": corner_id,
                "corner_identity_status": "CANDIDATE_NOT_GOLDEN",
                "action_counterexample_lap_ids": [
                    _lap_id(prefix, lap_algorithm, int(ordinal))
                    for ordinal in (
                        diagnosis["counterexample_lap_ordinals"] if diagnosis is not None else []
                    )
                ],
                "action_evidence_lap_ids": [
                    _lap_id(prefix, lap_algorithm, int(ordinal))
                    for ordinal in (
                        diagnosis["evidence_lap_ordinals"] if diagnosis is not None else []
                    )
                ],
                "diagnosis": str(diagnosis["diagnosis"]) if diagnosis is not None else None,
                "diagnosis_confidence": (
                    str(diagnosis["confidence"]).upper() if diagnosis is not None else None
                ),
                "loss_counterexample_lap_ids": [
                    _lap_id(prefix, lap_algorithm, ordinal) for ordinal in counterexample_ordinals
                ],
                "loss_evidence_lap_ids": [
                    _lap_id(prefix, lap_algorithm, ordinal) for ordinal in support_ordinals
                ],
                "executable": False,
                "expected_gain_range_s": (
                    list(diagnosis["expected_gain_range_s"]) if diagnosis is not None else None
                ),
                "kind": "DRIVING_LOSS_CARD",
                "loss_summary": {
                    "comparison_lap_count": len(metrics),
                    "median_accounted_window_delta_s": median_loss,
                    "median_approach_delta_s": float(
                        statistics.median(float(item["approach_delta_s"]) for item in metrics)
                    ),
                    "median_carry_delta_s": float(
                        statistics.median(float(item["carry_delta_s"]) for item in metrics)
                    ),
                    "median_local_delta_s": float(
                        statistics.median(float(item["local_delta_s"]) for item in metrics)
                    ),
                    "positive_lap_count": len(positive_ordinals),
                    "positive_lap_fraction": len(positive_ordinals) / len(metrics),
                    "supporting_lap_count": len(support_ordinals),
                },
                "per_lap_evidence": per_lap,
                "practice_only": True,
                "promotion_blockers": list(_PROMOTION_BLOCKERS),
                "rank": rank,
                "recommendation_id": f"driving-loss:{corner_id}",
                "status": "SHADOW_ONLY",
                "suppress_reasons": (
                    list(_NO_ACTION_REASONS) if diagnosis is None else list(_PROMOTION_BLOCKERS)
                ),
            }
        )

    binding: dict[str, object] = {
        "advisor_only": True,
        "cards": cards,
        "contract_version": CORNER_CARD_CONTRACT_VERSION,
        "execution_status": "COMPLETE",
        "gates": {
            "condition_data": "WAIT_CONDITION_DATA",
            "corner_identity": "CANDIDATE_NOT_GOLDEN",
            "human_labels": "WAIT_HUMAN_LABELS",
            "source_driving_replay": "PASS",
        },
        "input_receipt": {
            "driving_replay_sha256": validated["replay_sha256"],
            "input_provenance_sha256": validated["input_provenance_sha256"],
            "model_output_sha256": payload["model_output_sha256"],
            "model_semantic_sha256": validated["model_semantic_sha256"],
        },
        "ranking": {
            "card_count": len(cards),
            "maximum_cards": top,
            "minimum_evidence_laps": minimum_evidence,
            "minimum_loss_s": minimum_loss,
            "ranking_metric": "median_accounted_window_delta_s",
            "ranking_version": CORNER_CARD_RANKING_VERSION,
        },
        "reference": {
            "comparison_lap_ids": [
                _lap_id(prefix, lap_algorithm, ordinal) for ordinal in comparison_laps
            ],
            "reference_lap_id": _lap_id(prefix, lap_algorithm, reference),
            "reference_lap_ordinal": reference,
        },
        "status": "SHADOW_ONLY",
    }
    return {**binding, "corner_cards_sha256": canonical_sha256(binding)}


def _read_input(path: Path) -> object:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CornerCardError("INPUT_READ_FAILED", f"cannot open input safely: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_INPUT_BYTES:
                _fail("INPUT_NOT_REGULAR", "input must be a bounded non-empty regular file")
            payload = handle.read(MAX_INPUT_BYTES + 1)
            after = os.fstat(handle.fileno())
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if len(payload) != before.st_size or identity_before != identity_after:
                _fail("INPUT_CHANGED", "input changed while it was being read")
    except CornerCardError:
        raise
    except OSError as exc:
        raise CornerCardError("INPUT_READ_FAILED", f"cannot read input: {exc}") from exc
    return _strict_json(payload)


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CornerCardError("OUTPUT_CREATE_FAILED", f"cannot create output: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        with suppress(OSError):
            path.unlink()
        raise


def _top_value(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("top must be an integer") from exc
    if not 1 <= parsed <= MAX_CARDS:
        raise argparse.ArgumentTypeError(f"top must be from 1 to {MAX_CARDS}")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("driving_replay", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--top", type=_top_value, default=MAX_CARDS)
    args = parser.parse_args(argv)
    try:
        replay = _read_input(args.driving_replay)
        report = build_corner_cards(replay, top=args.top)
        encoded = _canonical_json(report, newline=True)
        if args.output is not None:
            _write_exclusive(args.output, encoded)
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
    except CornerCardError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
