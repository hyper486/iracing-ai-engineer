"""Build a fail-closed pit/service/stint evidence receipt.

This M1 layer consumes only an opaque adapter-created ``ValidatedIbtRun`` or
``ValidatedCollectorRun``.  It binds the exact source, normalized stream, and
shared telemetry-event receipt before publishing edge-supported pit-road,
stall, and ``PitstopActive`` intervals.

The receipt is descriptive and advisor-only.  ``FuelLevel`` endpoints support
an observed net tank-level change; they do not reveal delivered fuel.  Tire,
repair, driver-swap, and other service contents remain explicitly unavailable.
No recommendation is emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from .adapters import (
    CollectorInputEvidence,
    IbtInputEvidence,
    TelemetryAdapterError,
    ValidatedCollectorRun,
    ValidatedIbtRun,
    _validated_run_state,
    open_ibt_telemetry,
)
from .events import EventKind, TelemetryEvent, TelemetryEventPipeline
from .ibt import IbtFormatError
from .telemetry import (
    TELEMETRY_CONTRACT_VERSION,
    Presence,
    QualityStatus,
    SourceKind,
    TelemetryField,
    TelemetrySample,
)

PIT_STINT_CONTRACT_VERSION = "offline-m1-pit-stint-v1"
NORMALIZATION_PROFILE_VERSION = "normalized-sdk-adapter-v3"
EVENT_CONTRACT_VERSION = "telemetry-events-v1"

_SHA256_CHARS = frozenset("0123456789abcdef")
_RESET_EVENT_KINDS = frozenset(
    {
        EventKind.SOURCE_RESET,
        EventKind.SESSION_RESET,
        EventKind.QUALITY_REJECTED,
        EventKind.SOURCE_STALE,
        EventKind.SOURCE_RESUMED,
        EventKind.DROPPED_TICKS,
    }
)
_EDGE_EVENT_KIND = {
    ("pit_road", True): EventKind.PIT_ROAD_ENTERED,
    ("pit_road", False): EventKind.PIT_ROAD_EXITED,
    ("pit_stall", True): EventKind.PIT_STALL_ENTERED,
    ("pit_stall", False): EventKind.PIT_STALL_EXITED,
}
_COLLECTOR_BLOCKING_QUALITY_FIELDS = (
    "duplicate_conflict_count",
    "dropped_tick_count",
    "stale_event_count",
    "schema_change_count",
    "session_reset_count",
    "capture_clock_regression_count",
    "read_error_frame_count",
    "driver_info_key_count",
)
_TIMING_RATE_TOLERANCE_S = 2e-6

_RECEIPT_KEYS = frozenset(
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
_INPUT_BINDING_KEYS = frozenset(
    {
        "event_receipt_sha256",
        "expected_event_receipt_sha256",
        "expected_normalized_samples_sha256",
        "expected_source_sha256",
        "input_evidence_sha256",
        "input_lineage_sha256",
        "normalization_profile",
        "normalized_samples_sha256",
        "source_sha256",
    }
)
_NORMALIZATION_PROFILE_KEYS = frozenset(
    {"opponent_error_policy", "profile_version", "stale_after_us"}
)
_NORMALIZED_RECEIPT_KEYS = frozenset(
    {"contract_version", "sample_count", "samples_sha256"}
)
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
_COLLECTOR_EVIDENCE_KEYS = frozenset(
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
_SAMPLE_REF_KEYS = frozenset(
    {
        "frame_index",
        "lap_number",
        "laps_completed",
        "session_tick",
        "session_time_s",
        "shared_event_sequence",
    }
)
_STINT_KEYS = frozenset(
    {
        "duration_s",
        "end",
        "end_boundary",
        "observed_end_tank_level_l",
        "observed_endpoint_provenance",
        "observed_laps_completed_delta",
        "observed_laps_completed_delta_availability",
        "observed_start_tank_level_l",
        "observed_tank_level_availability",
        "start",
        "start_boundary",
        "status",
        "stint_id",
    }
)
_PIT_CYCLE_KEYS = frozenset(
    {"pit_cycle_id", "pit_road", "pit_stall_intervals", "service_episodes"}
)
_PIT_ROAD_KEYS = frozenset({"duration_s", "enter", "exit", "true_frame_count"})
_STALL_KEYS = frozenset(
    {"duration_s", "enter", "exit", "stall_interval_id", "true_frame_count"}
)
_SERVICE_KEYS = frozenset(
    {
        "active_frame_count",
        "duration_s",
        "end_edge",
        "observed_net_tank_change",
        "service_contents",
        "service_episode_id",
        "stall_support",
        "start",
    }
)
_TANK_CHANGE_KEYS = frozenset(
    {
        "end_fuel_level_l",
        "interpretation",
        "provenance",
        "start_fuel_level_l",
        "value_l",
    }
)
_STALL_SUPPORT_KEYS = frozenset(
    {
        "overlap_duration_s",
        "service_starts_before_stall_s",
        "stall_extends_after_service_s",
        "stall_interval_id",
        "status",
    }
)
_SERVICE_CONTENT_NAMES = frozenset(
    {"delivered_fuel", "driver_swap", "repairs", "tire_service"}
)
_SERVICE_CONTENT_KEYS = frozenset(
    {"availability", "blocked_claim", "estimate_available", "provenance", "status"}
)
_INCOMPLETE_COUNT_KEYS = frozenset({"pit_road", "pit_stall", "service", "stint"})
_SUMMARY_KEYS = frozenset(
    {
        "complete_stint_count",
        "partial_stint_count",
        "pit_cycle_count",
        "service_episode_count",
    }
)
_CAPABILITY_NAMES = frozenset(
    {
        "complete_stint_analysis",
        "human_validation",
        "pit_and_service_detection",
        "service_contents",
    }
)
_CAPABILITY_KEYS = frozenset({"reasons", "status"})
_QUALITY_GATE_KEYS = frozenset({"reasons", "status"})
_QUALITY_REASONS = frozenset(
    {
        "DROPPED_TICKS_OR_UNKNOWN",
        "IBT_FRAME_GAP",
        "INPUT_IDENTITY_MISMATCH",
        "MULTIPLE_SESSION_EPOCHS",
        "MULTIPLE_SOURCE_EPOCHS",
        "NORMALIZED_SAMPLE_REJECTED",
        "PIT_ROAD_INTERVAL_INVALID",
        "PIT_STALL_INTERVAL_INVALID",
        "PIT_STALL_OUTSIDE_COMPLETE_PIT_ROAD",
        "REQUIRED_CHANNEL_MISSING_OR_INVALID",
        "SERVICE_INTERVAL_INVALID",
        "SERVICE_OUTSIDE_COMPLETE_PIT_ROAD",
        "SHARED_EVENT_CONTINUITY_BREAK",
        "SHARED_EVENT_EDGE_MISMATCH",
        "SHARED_EVENT_EPOCH_CHANGED",
        "SOURCE_STALE_OR_UNKNOWN",
        "STINT_TIME_REGRESSION",
    }
)
_BLOCKED_SERVICE_CLAIMS = {
    "delivered_fuel": "DELIVERED_FUEL_QUANTITY",
    "driver_swap": "DRIVER_SWAP",
    "repairs": "REPAIR_CONTENTS",
    "tire_service": "TIRE_CHANGE_OR_COMPOUND",
}


class PitStintReceiptError(ValueError):
    """Raised when evidence cannot satisfy the offline M1 contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise PitStintReceiptError(code, message)


def _canonical_json(value: object, *, newline: bool = False) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise PitStintReceiptError(
            "CANONICAL_JSON_FAILED", "receipt is not canonical-JSON-safe"
        ) from exc
    return encoded + (b"\n" if newline else b"")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _SHA256_CHARS for character in value)
    ):
        _fail("INVALID_TRUST_ROOT", f"{label} must be a lowercase SHA-256 digest")
    return value


def _json_object_copy(value: object, label: str) -> dict[str, object]:
    try:
        copied = json.loads(_canonical_json(value))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise PitStintReceiptError(
            "CANONICAL_JSON_FAILED", f"{label} cannot be copied as JSON"
        ) from exc
    if type(copied) is not dict:
        _fail("SCHEMA_INVALID", f"{label} must be a plain object")
    return copied


def _mapping(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        _fail("SCHEMA_INVALID", f"{label} must be a plain object")
    return value


def _exact_mapping(
    value: object, keys: frozenset[str], label: str
) -> dict[str, object]:
    result = _mapping(value, label)
    if set(result) != keys:
        _fail("SCHEMA_INVALID", f"{label} keys are invalid")
    return result


def _list(value: object, label: str) -> list[object]:
    if type(value) is not list:
        _fail("SCHEMA_INVALID", f"{label} must be a plain array")
    return value


def _plain_int(
    value: object, label: str, *, minimum: int | None = 0
) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        suffix = "a plain integer" if minimum is None else f"a plain integer >= {minimum}"
        _fail("SCHEMA_INVALID", f"{label} must be {suffix}")
    return value


def _optional_plain_int(
    value: object, label: str, *, minimum: int | None = 0
) -> int | None:
    if value is None:
        return None
    return _plain_int(value, label, minimum=minimum)


def _finite_number(
    value: object,
    label: str,
    *,
    minimum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("SCHEMA_INVALID", f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        _fail("SCHEMA_INVALID", f"{label} is outside its finite range")
    return number


def _optional_number(
    value: object, label: str, *, minimum: float | None = None
) -> float | None:
    if value is None:
        return None
    return _finite_number(value, label, minimum=minimum)


def _identifier(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in value)
    ):
        _fail("SCHEMA_INVALID", f"{label} is not a valid identifier")
    return value


def _reason_list(
    value: object,
    label: str,
    *,
    allowed: frozenset[str] | None = None,
) -> list[object]:
    reasons = _list(value, label)
    if (
        any(type(reason) is not str or not reason for reason in reasons)
        or len(reasons) != len(set(reasons))
        or (allowed is not None and any(reason not in allowed for reason in reasons))
    ):
        _fail("SCHEMA_INVALID", f"{label} contains invalid reason codes")
    return reasons


def _close_number(
    actual: object,
    expected: float,
    label: str,
    *,
    tolerance: float = 3e-6,
) -> float:
    number = _finite_number(actual, label)
    if not math.isclose(number, expected, rel_tol=0.0, abs_tol=tolerance):
        _fail("DERIVATION_INVALID", f"{label} does not close")
    return number


def _validate_input_evidence(
    value: object,
) -> tuple[dict[str, object], str, int, int, str]:
    evidence = _mapping(value, "input_evidence")
    source_kind_raw = evidence.get("source_kind")
    if source_kind_raw == SourceKind.IBT_OFFLINE.value:
        evidence = _exact_mapping(evidence, _IBT_EVIDENCE_KEYS, "input_evidence")
        try:
            parsed: IbtInputEvidence | CollectorInputEvidence = IbtInputEvidence(
                source_id=evidence["source_id"],  # type: ignore[arg-type]
                session_id=evidence["session_id"],  # type: ignore[arg-type]
                source_sha256=evidence["source_sha256"],  # type: ignore[arg-type]
                byte_size=evidence["byte_size"],  # type: ignore[arg-type]
                record_count=evidence["record_count"],  # type: ignore[arg-type]
                tick_rate_hz=evidence["tick_rate_hz"],  # type: ignore[arg-type]
                source_kind=SourceKind.IBT_OFFLINE,
                completion_status=evidence["completion_status"],  # type: ignore[arg-type]
                authenticity_status=evidence["authenticity_status"],  # type: ignore[arg-type]
            )
        except (TelemetryAdapterError, TypeError, ValueError) as exc:
            raise PitStintReceiptError(
                "INPUT_EVIDENCE_INVALID", f"IBT input evidence is invalid: {exc}"
            ) from exc
        if parsed.to_dict() != evidence:
            _fail("INPUT_EVIDENCE_INVALID", "IBT input evidence does not round-trip")
        return (
            evidence,
            "ibt",
            parsed.record_count,
            parsed.tick_rate_hz,
            parsed.source_sha256,
        )

    evidence = _exact_mapping(evidence, _COLLECTOR_EVIDENCE_KEYS, "input_evidence")
    if type(source_kind_raw) is not str or source_kind_raw not in {
        SourceKind.SDK_LIVE.value,
        SourceKind.REPLAY_SDK_PROXY.value,
    }:
        _fail("INPUT_EVIDENCE_INVALID", "collector source_kind is invalid")
    rates = _list(evidence.get("tick_rate_hz_values"), "input_evidence.tick rates")
    scopes = _mapping(
        evidence.get("session_info_scope_counts"),
        "input_evidence.session_info_scope_counts",
    )
    if set(scopes) - {"FULL", "PARTIAL", "UNAVAILABLE"}:
        _fail("INPUT_EVIDENCE_INVALID", "collector session scope keys are invalid")
    try:
        parsed = CollectorInputEvidence(
            source_id=evidence["source_id"],  # type: ignore[arg-type]
            session_id=evidence["session_id"],  # type: ignore[arg-type]
            source_kind=SourceKind(source_kind_raw),
            sim_mode=evidence["sim_mode"],  # type: ignore[arg-type]
            completion_status=evidence["completion_status"],  # type: ignore[arg-type]
            semantic_record_count=evidence["semantic_record_count"],  # type: ignore[arg-type]
            records_sha256=evidence["records_sha256"],  # type: ignore[arg-type]
            frame_record_count=evidence["frame_record_count"],  # type: ignore[arg-type]
            event_record_count=evidence["event_record_count"],  # type: ignore[arg-type]
            schema_record_count=evidence["schema_record_count"],  # type: ignore[arg-type]
            session_info_record_count=evidence["session_info_record_count"],  # type: ignore[arg-type]
            samples_seen=evidence["samples_seen"],  # type: ignore[arg-type]
            duplicate_sample_count=evidence["duplicate_sample_count"],  # type: ignore[arg-type]
            duplicate_conflict_count=evidence["duplicate_conflict_count"],  # type: ignore[arg-type]
            dropped_tick_count=evidence["dropped_tick_count"],  # type: ignore[arg-type]
            stale_event_count=evidence["stale_event_count"],  # type: ignore[arg-type]
            session_reset_count=evidence["session_reset_count"],  # type: ignore[arg-type]
            schema_change_count=evidence["schema_change_count"],  # type: ignore[arg-type]
            schema_epoch_count=evidence["schema_epoch_count"],  # type: ignore[arg-type]
            session_epoch_count=evidence["session_epoch_count"],  # type: ignore[arg-type]
            first_buffer_tick=evidence["first_buffer_tick"],  # type: ignore[arg-type]
            last_buffer_tick=evidence["last_buffer_tick"],  # type: ignore[arg-type]
            tick_rate_hz_values=tuple(rates),  # type: ignore[arg-type]
            first_capture_monotonic_us=evidence["first_capture_monotonic_us"],  # type: ignore[arg-type]
            last_capture_monotonic_us=evidence["last_capture_monotonic_us"],  # type: ignore[arg-type]
            capture_span_us=evidence["capture_span_us"],  # type: ignore[arg-type]
            capture_clock_regression_count=evidence["capture_clock_regression_count"],  # type: ignore[arg-type]
            read_error_frame_count=evidence["read_error_frame_count"],  # type: ignore[arg-type]
            read_error_field_count=evidence["read_error_field_count"],  # type: ignore[arg-type]
            driver_info_key_count=evidence["driver_info_key_count"],  # type: ignore[arg-type]
            redacted_driver_info_path_count=evidence["redacted_driver_info_path_count"],  # type: ignore[arg-type]
            session_info_scope_counts=tuple(sorted(scopes.items())),  # type: ignore[arg-type]
            collector_contract_version=evidence["collector_contract_version"],  # type: ignore[arg-type]
            authenticity_status=evidence["authenticity_status"],  # type: ignore[arg-type]
        )
    except (TelemetryAdapterError, TypeError, ValueError) as exc:
        raise PitStintReceiptError(
            "INPUT_EVIDENCE_INVALID", f"collector input evidence is invalid: {exc}"
        ) from exc
    if parsed.to_dict() != evidence:
        _fail("INPUT_EVIDENCE_INVALID", "collector input evidence does not round-trip")
    if parsed.completion_status != "COMPLETE" or len(parsed.tick_rate_hz_values) != 1:
        _fail("INPUT_EVIDENCE_INVALID", "collector evidence is not a complete M1 input")
    if any(getattr(parsed, field) != 0 for field in _COLLECTOR_BLOCKING_QUALITY_FIELDS):
        _fail("INPUT_EVIDENCE_INVALID", "collector blocking quality is not zero")
    return (
        evidence,
        "collector",
        parsed.frame_record_count,
        parsed.tick_rate_hz_values[0],
        parsed.records_sha256,
    )


def _validate_normalized_receipt(
    value: object, *, sample_count: int
) -> dict[str, object]:
    receipt = _exact_mapping(
        value, _NORMALIZED_RECEIPT_KEYS, "normalized_input_receipt"
    )
    if receipt.get("contract_version") != TELEMETRY_CONTRACT_VERSION:
        _fail("NORMALIZED_RECEIPT_INVALID", "normalized contract is unsupported")
    if (
        _plain_int(
            receipt.get("sample_count"), "normalized sample_count", minimum=1
        )
        != sample_count
    ):
        _fail("NORMALIZED_RECEIPT_INVALID", "normalized sample count does not close")
    _sha256(receipt.get("samples_sha256"), "normalized samples SHA-256")
    return receipt


def _validate_event_receipt(
    value: object, *, sample_count: int
) -> dict[str, object]:
    receipt = _exact_mapping(value, _EVENT_RECEIPT_KEYS, "upstream_event_receipt")
    if receipt.get("contract_version") != EVENT_CONTRACT_VERSION:
        _fail("EVENT_RECEIPT_INVALID", "event contract is unsupported")
    counts = {
        name: _plain_int(receipt.get(name), f"event receipt {name}")
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
        counts["sample_count"] != sample_count
        or counts["accepted_sample_count"] + counts["rejected_sample_count"]
        != sample_count
    ):
        _fail("EVENT_RECEIPT_INVALID", "event sample counts do not close")
    kind_counts = _mapping(
        receipt.get("event_kind_counts"), "event receipt event_kind_counts"
    )
    known_kinds = {kind.value for kind in EventKind}
    if set(kind_counts) - known_kinds:
        _fail("EVENT_RECEIPT_INVALID", "event receipt contains unknown event kinds")
    parsed_kind_count = 0
    for kind, count in kind_counts.items():
        if type(kind) is not str:
            _fail("EVENT_RECEIPT_INVALID", "event kind name is invalid")
        parsed_kind_count += _plain_int(count, f"event kind {kind}")
    if parsed_kind_count != counts["event_count"]:
        _fail("EVENT_RECEIPT_INVALID", "event kind counts do not close")
    for name in ("config_sha256", "events_sha256", "receipt_sha256"):
        _sha256(receipt.get(name), f"event receipt {name}")
    expected_config_sha256 = canonical_sha256(
        {
            "contract_version": EVENT_CONTRACT_VERSION,
            "lap_wrap_high_ppm": 800_000,
            "lap_wrap_low_ppm": 200_000,
            "mode": "streaming-fail-closed-v1",
        }
    )
    if receipt["config_sha256"] != expected_config_sha256:
        _fail("EVENT_RECEIPT_INVALID", "event pipeline config is unsupported")
    material = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    if canonical_sha256(material) != receipt["receipt_sha256"]:
        _fail("EVENT_RECEIPT_INVALID", "event receipt self hash mismatch")
    return receipt


def _validate_input_binding(
    value: object,
    *,
    evidence: Mapping[str, object],
    normalized: Mapping[str, object],
    event: Mapping[str, object],
    source_digest: str,
) -> dict[str, object]:
    binding = _exact_mapping(value, _INPUT_BINDING_KEYS, "input_binding")
    profile = _exact_mapping(
        binding.get("normalization_profile"),
        _NORMALIZATION_PROFILE_KEYS,
        "input_binding.normalization_profile",
    )
    if (
        profile.get("profile_version") != NORMALIZATION_PROFILE_VERSION
        or type(profile.get("opponent_error_policy")) is not str
        or profile.get("opponent_error_policy") not in {"degrade", "reject"}
    ):
        _fail("INPUT_BINDING_INVALID", "normalization profile is unsupported")
    _plain_int(profile.get("stale_after_us"), "normalization stale_after_us", minimum=1)
    expected = {
        "event_receipt_sha256": event["receipt_sha256"],
        "expected_event_receipt_sha256": event["receipt_sha256"],
        "expected_normalized_samples_sha256": normalized["samples_sha256"],
        "expected_source_sha256": source_digest,
        "input_evidence_sha256": canonical_sha256(evidence),
        "normalized_samples_sha256": normalized["samples_sha256"],
        "source_sha256": source_digest,
    }
    for key, expected_value in expected.items():
        if _sha256(binding.get(key), f"input_binding.{key}") != expected_value:
            _fail("INPUT_BINDING_INVALID", f"input_binding.{key} does not close")
    lineage = _sha256(
        binding.get("input_lineage_sha256"), "input_binding.input_lineage_sha256"
    )
    material = {
        key: item for key, item in binding.items() if key != "input_lineage_sha256"
    }
    if canonical_sha256(material) != lineage:
        _fail("INPUT_BINDING_INVALID", "input lineage self hash mismatch")
    return binding


def _validate_sample_ref(
    value: object,
    label: str,
    *,
    sample_count: int,
    event_count: int,
) -> dict[str, object]:
    ref = _exact_mapping(value, _SAMPLE_REF_KEYS, label)
    frame_index = _plain_int(ref.get("frame_index"), f"{label}.frame_index")
    if frame_index >= sample_count:
        _fail("REFERENCE_INVALID", f"{label}.frame_index is outside the input")
    _optional_plain_int(ref.get("lap_number"), f"{label}.lap_number", minimum=None)
    _optional_plain_int(
        ref.get("laps_completed"), f"{label}.laps_completed", minimum=None
    )
    _plain_int(ref.get("session_tick"), f"{label}.session_tick", minimum=None)
    _finite_number(ref.get("session_time_s"), f"{label}.session_time_s")
    sequence = _optional_plain_int(
        ref.get("shared_event_sequence"), f"{label}.shared_event_sequence"
    )
    if sequence is not None and sequence >= event_count:
        _fail("REFERENCE_INVALID", f"{label} event sequence is outside the receipt")
    return ref


def _ref_duration(start: Mapping[str, object], end: Mapping[str, object]) -> float:
    return float(end["session_time_s"]) - float(start["session_time_s"])


def _validate_unknown_service_contents(value: object, label: str) -> None:
    contents = _exact_mapping(value, _SERVICE_CONTENT_NAMES, label)
    for name in sorted(_SERVICE_CONTENT_NAMES):
        content = _exact_mapping(
            contents.get(name), _SERVICE_CONTENT_KEYS, f"{label}.{name}"
        )
        expected = {
            "availability": "UNAVAILABLE",
            "blocked_claim": _BLOCKED_SERVICE_CLAIMS[name],
            "estimate_available": False,
            "provenance": "UNKNOWN",
            "status": "SKIP_NOT_OBSERVABLE",
        }
        if content != expected:
            _fail(
                "SERVICE_CONTENTS_INVALID",
                f"{label}.{name} promoted unavailable evidence",
            )


def _validate_stints(
    value: object,
    *,
    input_kind: str,
    tick_rate_hz: int,
    sample_count: int,
    event_count: int,
) -> tuple[list[object], int]:
    stints = _list(value, "stints")
    complete_count = 0
    for index, raw in enumerate(stints, start=1):
        label = f"stints[{index - 1}]"
        stint = _exact_mapping(raw, _STINT_KEYS, label)
        if stint.get("stint_id") != f"stint:{index}":
            _fail("STINT_INVALID", f"{label}.stint_id is not sequential")
        start = _validate_sample_ref(
            stint.get("start"),
            f"{label}.start",
            sample_count=sample_count,
            event_count=event_count,
        )
        end = _validate_sample_ref(
            stint.get("end"),
            f"{label}.end",
            sample_count=sample_count,
            event_count=event_count,
        )
        frame_delta = int(end["frame_index"]) - int(start["frame_index"])
        if frame_delta < 0:
            _fail("STINT_INVALID", f"{label} frame order regresses")
        status = stint.get("status")
        start_boundary = stint.get("start_boundary")
        end_boundary = stint.get("end_boundary")
        if type(start_boundary) is not str or start_boundary not in {
            "FILE_START",
            "CONTINUITY_START",
            "ROAD_EXIT",
        }:
            _fail("STINT_INVALID", f"{label}.start_boundary is invalid")
        if type(end_boundary) is not str or end_boundary not in {
            "CONTINUITY_BREAK",
            "FILE_END",
            "ROAD_ENTER",
        }:
            _fail("STINT_INVALID", f"{label}.end_boundary is invalid")
        if type(status) is not str:
            _fail("STINT_INVALID", f"{label}.status is invalid")
        expected_pair = {
            "COMPLETE": ("ROAD_EXIT", "ROAD_ENTER"),
            "PARTIAL_START": ("FILE_START", "ROAD_ENTER"),
            "PARTIAL_END": ("ROAD_EXIT", "FILE_END"),
        }.get(status)
        if expected_pair is not None:
            if (start_boundary, end_boundary) != expected_pair:
                _fail("STINT_INVALID", f"{label} status and boundaries disagree")
        elif status != "PARTIAL_CONTINUITY":
            _fail("STINT_INVALID", f"{label}.status is invalid")
        if status == "COMPLETE":
            complete_count += 1
        duration = (
            _ref_duration(start, end)
            if status == "COMPLETE" or input_kind == "collector"
            else frame_delta / tick_rate_hz
        )
        _close_number(
            stint.get("duration_s"), _round_s(duration), f"{label}.duration_s"
        )

        start_tank = _optional_number(
            stint.get("observed_start_tank_level_l"),
            f"{label}.observed_start_tank_level_l",
            minimum=0.0,
        )
        end_tank = _optional_number(
            stint.get("observed_end_tank_level_l"),
            f"{label}.observed_end_tank_level_l",
            minimum=0.0,
        )
        tank_available = stint.get("observed_tank_level_availability")
        tank_provenance = stint.get("observed_endpoint_provenance")
        if tank_available == "AVAILABLE":
            if start_tank is None or end_tank is None or tank_provenance != "SDK_DIRECT":
                _fail("STINT_INVALID", f"{label} tank evidence is inconsistent")
        elif tank_available == "UNAVAILABLE":
            if start_tank is not None or end_tank is not None or tank_provenance != "UNKNOWN":
                _fail("STINT_INVALID", f"{label} unavailable tank evidence was promoted")
        else:
            _fail("STINT_INVALID", f"{label} tank availability is invalid")

        lap_delta = _optional_plain_int(
            stint.get("observed_laps_completed_delta"),
            f"{label}.observed_laps_completed_delta",
        )
        lap_availability = stint.get("observed_laps_completed_delta_availability")
        start_laps = start["laps_completed"]
        end_laps = end["laps_completed"]
        derived_laps = (
            int(end_laps) - int(start_laps)
            if type(start_laps) is int
            and type(end_laps) is int
            and int(end_laps) >= int(start_laps)
            else None
        )
        if (
            (derived_laps is not None and lap_availability != "AVAILABLE")
            or (derived_laps is None and lap_availability != "UNAVAILABLE")
            or lap_delta != derived_laps
        ):
            _fail("STINT_INVALID", f"{label} lap delta does not close")
    return stints, complete_count


def _validate_cycles(
    value: object,
    *,
    sample_count: int,
    event_count: int,
) -> tuple[list[object], int]:
    cycles = _list(value, "pit_cycles")
    stall_ordinal = 0
    service_ordinal = 0
    for cycle_index, raw in enumerate(cycles, start=1):
        label = f"pit_cycles[{cycle_index - 1}]"
        cycle = _exact_mapping(raw, _PIT_CYCLE_KEYS, label)
        if cycle.get("pit_cycle_id") != f"pit-cycle:{cycle_index}":
            _fail("PIT_CYCLE_INVALID", f"{label}.pit_cycle_id is not sequential")
        road = _exact_mapping(cycle.get("pit_road"), _PIT_ROAD_KEYS, f"{label}.pit_road")
        road_start = _validate_sample_ref(
            road.get("enter"),
            f"{label}.pit_road.enter",
            sample_count=sample_count,
            event_count=event_count,
        )
        road_end = _validate_sample_ref(
            road.get("exit"),
            f"{label}.pit_road.exit",
            sample_count=sample_count,
            event_count=event_count,
        )
        road_frames = int(road_end["frame_index"]) - int(road_start["frame_index"])
        if road_frames <= 0:
            _fail("PIT_CYCLE_INVALID", f"{label}.pit_road frame order is invalid")
        _close_number(
            road.get("duration_s"),
            _round_s(_ref_duration(road_start, road_end)),
            f"{label}.pit_road.duration_s",
        )
        if _plain_int(
            road.get("true_frame_count"), f"{label}.pit_road.true_frame_count", minimum=1
        ) != road_frames:
            _fail("PIT_CYCLE_INVALID", f"{label}.pit_road frame count does not close")

        stalls = _list(cycle.get("pit_stall_intervals"), f"{label}.pit_stall_intervals")
        parsed_stalls: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
        for stall_index, stall_raw in enumerate(stalls):
            stall_ordinal += 1
            stall_label = f"{label}.pit_stall_intervals[{stall_index}]"
            stall = _exact_mapping(stall_raw, _STALL_KEYS, stall_label)
            stall_id = f"pit-stall:{stall_ordinal}"
            if stall.get("stall_interval_id") != stall_id:
                _fail("PIT_CYCLE_INVALID", f"{stall_label} id is not sequential")
            start = _validate_sample_ref(
                stall.get("enter"),
                f"{stall_label}.enter",
                sample_count=sample_count,
                event_count=event_count,
            )
            end = _validate_sample_ref(
                stall.get("exit"),
                f"{stall_label}.exit",
                sample_count=sample_count,
                event_count=event_count,
            )
            frames = int(end["frame_index"]) - int(start["frame_index"])
            if not (
                int(road_start["frame_index"])
                <= int(start["frame_index"])
                < int(end["frame_index"])
                <= int(road_end["frame_index"])
            ):
                _fail("PIT_CYCLE_INVALID", f"{stall_label} escapes its pit road")
            _close_number(
                stall.get("duration_s"),
                _round_s(_ref_duration(start, end)),
                f"{stall_label}.duration_s",
            )
            if _plain_int(
                stall.get("true_frame_count"), f"{stall_label}.true_frame_count", minimum=1
            ) != frames:
                _fail("PIT_CYCLE_INVALID", f"{stall_label} frame count does not close")
            parsed_stalls[stall_id] = (start, end)

        services = _list(cycle.get("service_episodes"), f"{label}.service_episodes")
        for service_index, service_raw in enumerate(services):
            service_ordinal += 1
            service_label = f"{label}.service_episodes[{service_index}]"
            service = _exact_mapping(service_raw, _SERVICE_KEYS, service_label)
            if service.get("service_episode_id") != f"service:{service_ordinal}":
                _fail("PIT_CYCLE_INVALID", f"{service_label} id is not sequential")
            start = _validate_sample_ref(
                service.get("start"),
                f"{service_label}.start",
                sample_count=sample_count,
                event_count=event_count,
            )
            end = _validate_sample_ref(
                service.get("end_edge"),
                f"{service_label}.end_edge",
                sample_count=sample_count,
                event_count=event_count,
            )
            frames = int(end["frame_index"]) - int(start["frame_index"])
            if not (
                int(road_start["frame_index"])
                <= int(start["frame_index"])
                < int(end["frame_index"])
                <= int(road_end["frame_index"])
            ):
                _fail("PIT_CYCLE_INVALID", f"{service_label} escapes its pit road")
            _close_number(
                service.get("duration_s"),
                _round_s(_ref_duration(start, end)),
                f"{service_label}.duration_s",
            )
            if _plain_int(
                service.get("active_frame_count"),
                f"{service_label}.active_frame_count",
                minimum=1,
            ) != frames:
                _fail("PIT_CYCLE_INVALID", f"{service_label} frame count does not close")

            tank = _exact_mapping(
                service.get("observed_net_tank_change"),
                _TANK_CHANGE_KEYS,
                f"{service_label}.observed_net_tank_change",
            )
            start_fuel = _finite_number(
                tank.get("start_fuel_level_l"),
                f"{service_label}.start_fuel_level_l",
                minimum=0.0,
            )
            end_fuel = _finite_number(
                tank.get("end_fuel_level_l"),
                f"{service_label}.end_fuel_level_l",
                minimum=0.0,
            )
            if (
                tank.get("interpretation")
                != "OBSERVED_ENDPOINT_TANK_LEVEL_DIFFERENCE_NOT_DELIVERED_FUEL"
                or tank.get("provenance") != "SDK_DIRECT_ENDPOINT_DIFFERENCE"
            ):
                _fail("PIT_CYCLE_INVALID", f"{service_label} tank provenance is invalid")
            _close_number(
                tank.get("value_l"),
                _round_s(end_fuel - start_fuel),
                f"{service_label}.observed_net_tank_change.value_l",
            )
            _validate_unknown_service_contents(
                service.get("service_contents"), f"{service_label}.service_contents"
            )

            support = _exact_mapping(
                service.get("stall_support"),
                _STALL_SUPPORT_KEYS,
                f"{service_label}.stall_support",
            )
            overlaps: list[tuple[float, str]] = []
            for stall_id, (stall_start, stall_end) in parsed_stalls.items():
                overlap = min(
                    float(end["session_time_s"]),
                    float(stall_end["session_time_s"]),
                ) - max(
                    float(start["session_time_s"]),
                    float(stall_start["session_time_s"]),
                )
                if overlap > 0:
                    overlaps.append((overlap, stall_id))
            if not overlaps:
                if support != {
                    "overlap_duration_s": 0.0,
                    "service_starts_before_stall_s": None,
                    "stall_extends_after_service_s": None,
                    "stall_interval_id": None,
                    "status": "NO_POSITIVE_OVERLAP",
                }:
                    _fail("PIT_CYCLE_INVALID", f"{service_label} stall support is invalid")
            else:
                maximum_overlap, expected_stall_id = max(
                    overlaps, key=lambda item: item[0]
                )
                if (
                    support.get("status") != "POSITIVE_OVERLAP"
                    or support.get("stall_interval_id") != expected_stall_id
                ):
                    _fail("PIT_CYCLE_INVALID", f"{service_label} stall support does not close")
                stall_start, stall_end = parsed_stalls[expected_stall_id]
                _close_number(
                    support.get("overlap_duration_s"),
                    _round_s(maximum_overlap),
                    f"{service_label}.stall_support.overlap_duration_s",
                )
                _close_number(
                    support.get("service_starts_before_stall_s"),
                    _round_s(
                        max(
                            0.0,
                            float(stall_start["session_time_s"])
                            - float(start["session_time_s"]),
                        )
                    ),
                    f"{service_label}.stall_support.service_starts_before_stall_s",
                )
                _close_number(
                    support.get("stall_extends_after_service_s"),
                    _round_s(
                        max(
                            0.0,
                            float(stall_end["session_time_s"])
                            - float(end["session_time_s"]),
                        )
                    ),
                    f"{service_label}.stall_support.stall_extends_after_service_s",
                )
    return cycles, service_ordinal


def _validate_capabilities(
    value: object,
    *,
    quality_reasons: list[object],
    complete_stint_count: int,
    pit_cycle_count: int,
    service_episode_count: int,
) -> None:
    capabilities = _exact_mapping(value, _CAPABILITY_NAMES, "capabilities")
    if quality_reasons:
        detection = {"reasons": quality_reasons, "status": "WAIT_DATA_QUALITY"}
    elif pit_cycle_count == 0:
        detection = {
            "reasons": ["NO_COMPLETE_PIT_ROAD_INTERVAL_OBSERVED"],
            "status": "WAIT_PIT_SAMPLE",
        }
    elif service_episode_count == 0:
        detection = {
            "reasons": ["NO_COMPLETE_SERVICE_EPISODE_OBSERVED"],
            "status": "WAIT_SERVICE_SAMPLE",
        }
    else:
        detection = {"reasons": [], "status": "PASS_DATA"}
    expected = {
        "complete_stint_analysis": {
            "reasons": [] if complete_stint_count else ["NO_COMPLETE_STINT_OBSERVED"],
            "status": "PASS_DATA" if complete_stint_count else "WAIT_COMPLETE_STINT",
        },
        "human_validation": {
            "reasons": ["PIT_AND_SERVICE_EDGES_NOT_HUMAN_LABELED"],
            "status": "WAIT_HUMAN_LABELS",
        },
        "pit_and_service_detection": detection,
        "service_contents": {
            "reasons": [
                "PITSTOPACTIVE_DOES_NOT_IDENTIFY_SERVICE_CONTENTS",
                "TIRE_SET_OR_COMPOUND_VALUES_DO_NOT_PROVE_A_TIRE_CHANGE",
                "FUELLEVEL_ENDPOINT_DELTA_IS_NOT_DELIVERED_FUEL",
            ],
            "status": "SKIP_NOT_OBSERVABLE",
        },
    }
    for name in sorted(_CAPABILITY_NAMES):
        capability = _exact_mapping(
            capabilities.get(name), _CAPABILITY_KEYS, f"capabilities.{name}"
        )
        _reason_list(capability.get("reasons"), f"capabilities.{name}.reasons")
        if capability != expected[name]:
            _fail("CAPABILITY_INVALID", f"capabilities.{name} does not close")


def validate_pit_stint_receipt(
    value: object,
    *,
    expected_pit_stint_receipt_sha256: str | None = None,
) -> dict[str, object]:
    """Validate the complete persisted M1 receipt and every derived gate.

    The receipt cannot independently re-prove the source bytes, but this
    validator rejects unknown nested fields, broken digest closure, promoted
    capabilities, and inconsistent pit/service/stint derivations.
    """

    receipt = _exact_mapping(
        _json_object_copy(value, "pit/stint receipt"),
        _RECEIPT_KEYS,
        "pit/stint receipt",
    )
    stored = _sha256(
        receipt.get("pit_stint_receipt_sha256"), "pit/stint receipt SHA-256"
    )
    if expected_pit_stint_receipt_sha256 is not None and stored != _sha256(
        expected_pit_stint_receipt_sha256,
        "expected pit/stint receipt SHA-256",
    ):
        _fail("RECEIPT_SHA256_MISMATCH", "pit/stint receipt failed its trust root")
    material = {
        key: item for key, item in receipt.items() if key != "pit_stint_receipt_sha256"
    }
    if canonical_sha256(material) != stored:
        _fail("RECEIPT_SHA256_MISMATCH", "pit/stint receipt self hash mismatch")
    if (
        receipt.get("contract_version") != PIT_STINT_CONTRACT_VERSION
        or receipt.get("advisor_only") is not True
        or receipt.get("attestation_status") != "NOT_R7_ATTESTED"
        or receipt.get("derivation_status") != "POST_ADMISSION_PACKAGE_EXTERNAL"
        or receipt.get("execution_mode") != "SHADOW_ONLY"
        or receipt.get("status") != "CANDIDATE_NOT_GOLDEN"
        or receipt.get("recommendations") != []
    ):
        _fail("SAFETY_BOUNDARY_INVALID", "pit/stint safety boundary is invalid")

    evidence, input_kind, sample_count, tick_rate_hz, source_digest = (
        _validate_input_evidence(receipt.get("input_evidence"))
    )
    normalized = _validate_normalized_receipt(
        receipt.get("normalized_input_receipt"), sample_count=sample_count
    )
    event = _validate_event_receipt(
        receipt.get("upstream_event_receipt"), sample_count=sample_count
    )
    _validate_input_binding(
        receipt.get("input_binding"),
        evidence=evidence,
        normalized=normalized,
        event=event,
        source_digest=source_digest,
    )

    quality = _exact_mapping(
        receipt.get("quality_gate"), _QUALITY_GATE_KEYS, "quality_gate"
    )
    quality_reasons = _reason_list(
        quality.get("reasons"), "quality_gate.reasons", allowed=_QUALITY_REASONS
    )
    if quality.get("status") != ("DEGRADED" if quality_reasons else "PASS"):
        _fail("QUALITY_GATE_INVALID", "quality gate status does not close")
    derived_event_reasons = {
        "MULTIPLE_SESSION_EPOCHS": event["session_epoch_count"] != 1,
        "MULTIPLE_SOURCE_EPOCHS": event["source_epoch_count"] != 1,
        "NORMALIZED_SAMPLE_REJECTED": event["rejected_sample_count"] != 0,
    }
    for reason, required in derived_event_reasons.items():
        if (reason in quality_reasons) != required:
            _fail("QUALITY_GATE_INVALID", f"quality reason {reason} does not close")
    if input_kind == "collector" and "IBT_FRAME_GAP" in quality_reasons:
        _fail("QUALITY_GATE_INVALID", "collector input cannot claim an IBT frame gap")

    stints, complete_stint_count = _validate_stints(
        receipt.get("stints"),
        input_kind=input_kind,
        tick_rate_hz=tick_rate_hz,
        sample_count=sample_count,
        event_count=int(event["event_count"]),
    )
    cycles, service_episode_count = _validate_cycles(
        receipt.get("pit_cycles"),
        sample_count=sample_count,
        event_count=int(event["event_count"]),
    )
    _validate_unknown_service_contents(receipt.get("service_contents"), "service_contents")

    incomplete = _exact_mapping(
        receipt.get("incomplete_interval_counts"),
        _INCOMPLETE_COUNT_KEYS,
        "incomplete_interval_counts",
    )
    for name in sorted(_INCOMPLETE_COUNT_KEYS):
        _plain_int(incomplete.get(name), f"incomplete_interval_counts.{name}")
    partial_stint_count = len(stints) - complete_stint_count
    if int(incomplete["stint"]) < partial_stint_count:
        _fail("DERIVATION_INVALID", "incomplete stint count under-reports partial stints")

    summary = _exact_mapping(receipt.get("summary"), _SUMMARY_KEYS, "summary")
    expected_summary = {
        "complete_stint_count": complete_stint_count,
        "partial_stint_count": partial_stint_count,
        "pit_cycle_count": len(cycles),
        "service_episode_count": service_episode_count,
    }
    for name in sorted(_SUMMARY_KEYS):
        _plain_int(summary.get(name), f"summary.{name}")
    if summary != expected_summary:
        _fail("DERIVATION_INVALID", "summary does not close to receipt contents")
    _validate_capabilities(
        receipt.get("capabilities"),
        quality_reasons=quality_reasons,
        complete_stint_count=complete_stint_count,
        pit_cycle_count=len(cycles),
        service_episode_count=service_episode_count,
    )
    return receipt


def _present(field: TelemetryField[Any], expected: type[Any]) -> Any | None:
    if field.presence is not Presence.PRESENT:
        return None
    value = field.value
    if expected is int and isinstance(value, bool):
        return None
    return value if isinstance(value, expected) else None


def _present_float(field: TelemetryField[float]) -> float | None:
    value = _present(field, float)
    return value if value is not None and math.isfinite(value) else None


def _round_s(value: float) -> float:
    return round(value, 6)


def _event_sequence(
    emitted: Sequence[TelemetryEvent], kind: EventKind
) -> int | None:
    matches = [event.sequence for event in emitted if event.kind is kind]
    return matches[0] if len(matches) == 1 else None


def _sample_ref(
    sample: TelemetrySample,
    frame_index: int,
    *,
    shared_event_sequence: int | None = None,
) -> dict[str, object]:
    session_time = _present_float(sample.session.session_time_s)
    session_tick = _present(sample.session.session_tick, int)
    lap_number = _present(sample.lap.lap_number, int)
    laps_completed = _present(sample.lap.laps_completed, int)
    if session_time is None or session_tick is None:
        _fail("REFERENCE_FIELD_MISSING", "a sample reference lacks time or tick")
    return {
        "frame_index": frame_index,
        "lap_number": lap_number,
        "laps_completed": laps_completed,
        "session_tick": session_tick,
        "session_time_s": _round_s(session_time),
        "shared_event_sequence": shared_event_sequence,
    }


@dataclass(slots=True)
class _OpenInterval:
    start: dict[str, object]
    edge_supported: bool
    true_frame_count: int = 1
    start_fuel_l: float | None = None


@dataclass(slots=True)
class _OpenRoad:
    interval: _OpenInterval
    stalls: list[dict[str, object]] = field(default_factory=list)
    services: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class _OpenStint:
    start: dict[str, object]
    start_boundary: str
    start_tank_level_l: float | None


class _PitStintAnalyzer:
    """Small edge detector layered on top of the shared event state machine."""

    def __init__(
        self,
        *,
        tick_rate_hz: int,
        partial_session_time_required: bool,
    ) -> None:
        if type(tick_rate_hz) is not int or not 1 <= tick_rate_hz <= 360:
            raise ValueError("tick_rate_hz must be a plain integer from 1 to 360")
        if type(partial_session_time_required) is not bool:
            raise TypeError("partial_session_time_required must be a plain bool")
        self.tick_rate_hz = tick_rate_hz
        self.partial_session_time_required = partial_session_time_required
        self.previous_values: dict[str, bool | None] = {
            "pit_road": None,
            "pit_stall": None,
            "service": None,
        }
        self.open_road: _OpenRoad | None = None
        self.open_stall: _OpenInterval | None = None
        self.open_service: _OpenInterval | None = None
        self.open_stint: _OpenStint | None = None
        self.pit_cycles: list[dict[str, object]] = []
        self.stints: list[dict[str, object]] = []
        self.quality_reasons: list[str] = []
        self.incomplete_interval_counts = {
            "pit_road": 0,
            "pit_stall": 0,
            "service": 0,
            "stint": 0,
        }
        self.first_ref: dict[str, object] | None = None
        self.last_ref: dict[str, object] | None = None
        self.last_safe_ref: dict[str, object] | None = None
        self.last_safe_tank_level_l: float | None = None
        self.last_safe_session_time_s: float | None = None
        self._stint_ordinal = 0
        self._stall_ordinal = 0
        self._service_ordinal = 0
        # Only interval-boundary frames need their unrounded SessionTime retained.
        # Public sample references stay frozen at six decimals, while interval
        # math uses the source value so rounding cannot create false drift.
        self._edge_session_times_s: dict[int, float] = {}

    def _frame_duration_s(
        self, start: Mapping[str, object], end: Mapping[str, object]
    ) -> float:
        frame_delta = int(end["frame_index"]) - int(start["frame_index"])
        return frame_delta / self.tick_rate_hz

    def _raw_session_time_s(self, ref: Mapping[str, object]) -> float:
        frame_index = int(ref["frame_index"])
        return self._edge_session_times_s.get(
            frame_index,
            float(ref["session_time_s"]),
        )

    def _session_duration_s(
        self, start: Mapping[str, object], end: Mapping[str, object]
    ) -> float:
        frame_delta = int(end["frame_index"]) - int(start["frame_index"])
        tick_delta = int(end["session_tick"]) - int(start["session_tick"])
        session_delta = self._raw_session_time_s(end) - self._raw_session_time_s(
            start
        )
        frame_duration = frame_delta / self.tick_rate_hz
        tick_duration = tick_delta / self.tick_rate_hz
        if (
            frame_delta <= 0
            or tick_delta <= 0
            or not math.isfinite(session_delta)
            or session_delta <= 0
            or abs(session_delta - frame_duration) > _TIMING_RATE_TOLERANCE_S
            or abs(session_delta - tick_duration) > _TIMING_RATE_TOLERANCE_S
        ):
            _fail(
                "TIMING_RATE_MISMATCH",
                (
                    "SessionTime boundary duration does not agree with both "
                    "frame and SessionTick durations at the declared tick rate"
                ),
            )
        return session_delta

    def _add_quality_reason(self, reason: str) -> None:
        if reason not in self.quality_reasons:
            self.quality_reasons.append(reason)

    def _close_stint(
        self,
        end: dict[str, object],
        *,
        end_boundary: str,
        end_tank_level_l: float | None,
        forced_status: str | None = None,
    ) -> None:
        if self.open_stint is None:
            return
        start = self.open_stint.start
        if forced_status is not None:
            status = forced_status
        elif self.open_stint.start_boundary == "ROAD_EXIT" and end_boundary == "ROAD_ENTER":
            status = "COMPLETE"
        elif self.open_stint.start_boundary == "FILE_START" and end_boundary == "ROAD_ENTER":
            status = "PARTIAL_START"
        elif self.open_stint.start_boundary == "ROAD_EXIT" and end_boundary == "FILE_END":
            status = "PARTIAL_END"
        else:
            status = "PARTIAL_CONTINUITY"
        # Collector partial stints must close their declared timing rate even
        # when no pit edge exists.  IBT file/continuity partials retain the
        # frozen v1 frame-span convention; COMPLETE stints always use edges.
        duration = (
            self._session_duration_s(start, end)
            if status == "COMPLETE" or self.partial_session_time_required
            else self._frame_duration_s(start, end)
        )
        if duration < 0:
            self._add_quality_reason("STINT_TIME_REGRESSION")
            self.incomplete_interval_counts["stint"] += 1
            self.open_stint = None
            return
        start_laps = start["laps_completed"]
        end_laps = end["laps_completed"]
        laps_delta = (
            end_laps - start_laps
            if type(start_laps) is int
            and type(end_laps) is int
            and end_laps >= start_laps
            else None
        )
        tank_levels_available = (
            self.open_stint.start_tank_level_l is not None
            and end_tank_level_l is not None
            and math.isfinite(self.open_stint.start_tank_level_l)
            and math.isfinite(end_tank_level_l)
        )
        self._stint_ordinal += 1
        self.stints.append(
            {
                "duration_s": _round_s(duration),
                "end": end,
                "end_boundary": end_boundary,
                "observed_end_tank_level_l": (
                    round(end_tank_level_l, 6) if tank_levels_available else None
                ),
                "observed_endpoint_provenance": (
                    "SDK_DIRECT" if tank_levels_available else "UNKNOWN"
                ),
                "observed_laps_completed_delta": laps_delta,
                "observed_laps_completed_delta_availability": (
                    "AVAILABLE" if laps_delta is not None else "UNAVAILABLE"
                ),
                "observed_start_tank_level_l": (
                    round(self.open_stint.start_tank_level_l, 6)
                    if tank_levels_available
                    else None
                ),
                "observed_tank_level_availability": (
                    "AVAILABLE" if tank_levels_available else "UNAVAILABLE"
                ),
                "start": start,
                "start_boundary": self.open_stint.start_boundary,
                "status": status,
                "stint_id": f"stint:{self._stint_ordinal}",
            }
        )
        if status != "COMPLETE" and "PARTIAL" in status:
            self.incomplete_interval_counts["stint"] += 1
        self.open_stint = None

    def break_continuity(
        self,
        reason: str,
        *,
        boundary_ref: dict[str, object] | None = None,
        boundary_tank_level_l: float | None = None,
        boundary_session_time_s: float | None = None,
    ) -> None:
        """Discard open transition state so no interval can bridge bad data."""

        self._add_quality_reason(reason)
        if self.open_road is not None:
            self.incomplete_interval_counts["pit_road"] += 1
        if self.open_stall is not None:
            self.incomplete_interval_counts["pit_stall"] += 1
        if self.open_service is not None:
            self.incomplete_interval_counts["service"] += 1
        if self.open_stint is not None and (
            boundary_ref is not None or self.last_safe_ref is not None
        ):
            selected_ref = boundary_ref if boundary_ref is not None else self.last_safe_ref
            selected_tank_level = (
                boundary_tank_level_l
                if boundary_ref is not None
                else self.last_safe_tank_level_l
            )
            selected_session_time = (
                boundary_session_time_s
                if boundary_ref is not None
                else self.last_safe_session_time_s
            )
            assert selected_ref is not None
            if selected_session_time is not None:
                self._edge_session_times_s[
                    int(selected_ref["frame_index"])
                ] = selected_session_time
            self._close_stint(
                selected_ref,
                end_boundary="CONTINUITY_BREAK",
                end_tank_level_l=selected_tank_level,
                forced_status="PARTIAL_CONTINUITY",
            )
        self.open_road = None
        self.open_stall = None
        self.open_service = None
        self.open_stint = None
        self.previous_values = {"pit_road": None, "pit_stall": None, "service": None}

    def _start_interval(
        self,
        name: str,
        ref: dict[str, object],
        *,
        edge_supported: bool,
        fuel_l: float,
    ) -> None:
        interval = _OpenInterval(
            start=ref,
            edge_supported=edge_supported,
            start_fuel_l=fuel_l if name == "service" else None,
        )
        if name == "pit_road":
            self.open_road = _OpenRoad(interval=interval)
        elif name == "pit_stall":
            self.open_stall = interval
        else:
            self.open_service = interval

    def _complete_service(
        self, ref: dict[str, object], *, end_fuel_l: float
    ) -> None:
        interval = self.open_service
        self.open_service = None
        if interval is None:
            return
        if not interval.edge_supported:
            self.incomplete_interval_counts["service"] += 1
            return
        duration = self._session_duration_s(interval.start, ref)
        if duration <= 0 or interval.start_fuel_l is None:
            self._add_quality_reason("SERVICE_INTERVAL_INVALID")
            return
        self._service_ordinal += 1
        service = {
            "active_frame_count": interval.true_frame_count,
            "duration_s": _round_s(duration),
            "end_edge": ref,
            "observed_net_tank_change": {
                "end_fuel_level_l": round(end_fuel_l, 6),
                "interpretation": (
                    "OBSERVED_ENDPOINT_TANK_LEVEL_DIFFERENCE_NOT_DELIVERED_FUEL"
                ),
                "provenance": "SDK_DIRECT_ENDPOINT_DIFFERENCE",
                "start_fuel_level_l": round(interval.start_fuel_l, 6),
                "value_l": round(end_fuel_l - interval.start_fuel_l, 6),
            },
            "service_contents": _unknown_service_contents(),
            "service_episode_id": f"service:{self._service_ordinal}",
            "stall_support": {
                "overlap_duration_s": 0.0,
                "stall_extends_after_service_s": None,
                "stall_interval_id": None,
                "status": "NO_POSITIVE_OVERLAP",
                "service_starts_before_stall_s": None,
            },
            "start": interval.start,
        }
        if self.open_road is None:
            self._add_quality_reason("SERVICE_OUTSIDE_COMPLETE_PIT_ROAD")
            return
        road_start = float(self.open_road.interval.start["session_time_s"])
        if float(interval.start["session_time_s"]) < road_start:
            self._add_quality_reason("SERVICE_OUTSIDE_COMPLETE_PIT_ROAD")
            return
        self.open_road.services.append(service)

    def _complete_stall(self, ref: dict[str, object]) -> None:
        interval = self.open_stall
        self.open_stall = None
        if interval is None:
            return
        if not interval.edge_supported:
            self.incomplete_interval_counts["pit_stall"] += 1
            return
        duration = self._session_duration_s(interval.start, ref)
        if duration <= 0:
            self._add_quality_reason("PIT_STALL_INTERVAL_INVALID")
            return
        self._stall_ordinal += 1
        stall = {
            "duration_s": _round_s(duration),
            "enter": interval.start,
            "exit": ref,
            "stall_interval_id": f"pit-stall:{self._stall_ordinal}",
            "true_frame_count": interval.true_frame_count,
        }
        if self.open_road is None:
            self._add_quality_reason("PIT_STALL_OUTSIDE_COMPLETE_PIT_ROAD")
            return
        self.open_road.stalls.append(stall)

    def _attach_stall_support(
        self,
        service: dict[str, object], stalls: Sequence[dict[str, object]]
    ) -> None:
        service_start_ref = service["start"]
        service_end_ref = service["end_edge"]
        assert isinstance(service_start_ref, Mapping)
        assert isinstance(service_end_ref, Mapping)
        service_start = self._raw_session_time_s(service_start_ref)
        service_end = self._raw_session_time_s(service_end_ref)
        candidates: list[tuple[float, dict[str, object]]] = []
        for stall in stalls:
            stall_start_ref = stall["enter"]
            stall_end_ref = stall["exit"]
            assert isinstance(stall_start_ref, Mapping)
            assert isinstance(stall_end_ref, Mapping)
            overlap_start_ref = max(
                (service_start_ref, stall_start_ref),
                key=self._raw_session_time_s,
            )
            overlap_end_ref = min(
                (service_end_ref, stall_end_ref),
                key=self._raw_session_time_s,
            )
            if self._raw_session_time_s(overlap_end_ref) > self._raw_session_time_s(
                overlap_start_ref
            ):
                candidates.append(
                    (
                        self._session_duration_s(overlap_start_ref, overlap_end_ref),
                        stall,
                    )
                )
        if not candidates:
            return
        overlap, stall = max(candidates, key=lambda item: item[0])
        stall_start_ref = stall["enter"]
        stall_end_ref = stall["exit"]
        assert isinstance(stall_start_ref, Mapping)
        assert isinstance(stall_end_ref, Mapping)
        stall_start = self._raw_session_time_s(stall_start_ref)
        stall_end = self._raw_session_time_s(stall_end_ref)
        stall_extension = (
            self._session_duration_s(service_end_ref, stall_end_ref)
            if stall_end > service_end
            else 0.0
        )
        service_lead = (
            self._session_duration_s(service_start_ref, stall_start_ref)
            if stall_start > service_start
            else 0.0
        )
        service["stall_support"] = {
            "overlap_duration_s": _round_s(overlap),
            "stall_extends_after_service_s": _round_s(stall_extension),
            "stall_interval_id": stall["stall_interval_id"],
            "status": "POSITIVE_OVERLAP",
            "service_starts_before_stall_s": _round_s(service_lead),
        }

    def _complete_road(self, ref: dict[str, object]) -> None:
        road = self.open_road
        self.open_road = None
        if road is None:
            return
        if not road.interval.edge_supported:
            self.incomplete_interval_counts["pit_road"] += 1
            return
        road_start = int(road.interval.start["frame_index"])
        road_end = int(ref["frame_index"])
        if road_end <= road_start:
            self._add_quality_reason("PIT_ROAD_INTERVAL_INVALID")
            return
        road_duration = self._session_duration_s(road.interval.start, ref)
        valid_stalls = [
            stall
            for stall in road.stalls
            if road_start <= int(stall["enter"]["frame_index"])  # type: ignore[index]
            < int(stall["exit"]["frame_index"])  # type: ignore[index]
            <= road_end
        ]
        valid_services = [
            service
            for service in road.services
            if road_start <= int(service["start"]["frame_index"])  # type: ignore[index]
            < int(service["end_edge"]["frame_index"])  # type: ignore[index]
            <= road_end
        ]
        if len(valid_stalls) != len(road.stalls):
            self._add_quality_reason("PIT_STALL_OUTSIDE_COMPLETE_PIT_ROAD")
        if len(valid_services) != len(road.services):
            self._add_quality_reason("SERVICE_OUTSIDE_COMPLETE_PIT_ROAD")
        for service in valid_services:
            self._attach_stall_support(service, valid_stalls)
        ordinal = len(self.pit_cycles) + 1
        self.pit_cycles.append(
            {
                "pit_cycle_id": f"pit-cycle:{ordinal}",
                "pit_road": {
                    "duration_s": _round_s(road_duration),
                    "enter": road.interval.start,
                    "exit": ref,
                    "true_frame_count": road.interval.true_frame_count,
                },
                "pit_stall_intervals": valid_stalls,
                "service_episodes": valid_services,
            }
        )

    def process(
        self,
        sample: TelemetrySample,
        frame_index: int,
        emitted: Sequence[TelemetryEvent],
        *,
        values: Mapping[str, bool],
        fuel_l: float,
    ) -> None:
        base_ref = _sample_ref(sample, frame_index)
        raw_session_time_s = _present_float(sample.session.session_time_s)
        assert raw_session_time_s is not None
        continuity_start = all(
            value is None for value in self.previous_values.values()
        )
        if continuity_start or any(
            previous is not None and previous != values[name]
            for name, previous in self.previous_values.items()
        ):
            self._edge_session_times_s[frame_index] = raw_session_time_s
        if self.first_ref is None:
            self.first_ref = base_ref
        self.last_ref = base_ref

        # Start-of-continuity truth without a preceding false edge is partial.
        if continuity_start:
            for name, current in values.items():
                if current:
                    self._start_interval(
                        name,
                        base_ref,
                        edge_supported=False,
                        fuel_l=fuel_l,
                    )
                self.previous_values[name] = current
            if not values["pit_road"]:
                boundary = "FILE_START" if frame_index == 0 else "CONTINUITY_START"
                self.open_stint = _OpenStint(base_ref, boundary, fuel_l)
            self.last_safe_ref = base_ref
            self.last_safe_tank_level_l = fuel_l
            self.last_safe_session_time_s = raw_session_time_s
            return

        edge_refs: dict[tuple[str, bool], dict[str, object]] = {}
        for name in ("pit_road", "pit_stall"):
            previous = self.previous_values[name]
            current = values[name]
            if previous is not None and previous != current:
                event_kind = _EDGE_EVENT_KIND[(name, current)]
                sequence = _event_sequence(emitted, event_kind)
                if sequence is None:
                    self._add_quality_reason("SHARED_EVENT_EDGE_MISMATCH")
                edge_refs[(name, current)] = _sample_ref(
                    sample,
                    frame_index,
                    shared_event_sequence=sequence,
                )

        # Road entry bounds the prior off-road stint and must exist before
        # same-frame stall/service starts are considered.
        if self.previous_values["pit_road"] is False and values["pit_road"] is True:
            ref = edge_refs[("pit_road", True)]
            self._close_stint(
                ref,
                end_boundary="ROAD_ENTER",
                end_tank_level_l=fuel_l,
            )
            self._start_interval(
                "pit_road", ref, edge_supported=True, fuel_l=fuel_l
            )

        if self.previous_values["pit_stall"] is False and values["pit_stall"] is True:
            self._start_interval(
                "pit_stall",
                edge_refs[("pit_stall", True)],
                edge_supported=True,
                fuel_l=fuel_l,
            )
        if self.previous_values["service"] is False and values["service"] is True:
            self._start_interval(
                "service", base_ref, edge_supported=True, fuel_l=fuel_l
            )

        if self.previous_values["service"] is True and values["service"] is False:
            self._complete_service(base_ref, end_fuel_l=fuel_l)
        if self.previous_values["pit_stall"] is True and values["pit_stall"] is False:
            self._complete_stall(edge_refs[("pit_stall", False)])
        if self.previous_values["pit_road"] is True and values["pit_road"] is False:
            ref = edge_refs[("pit_road", False)]
            self._complete_road(ref)
            self.open_stint = _OpenStint(ref, "ROAD_EXIT", fuel_l)

        for name, current in values.items():
            interval = {
                "pit_road": self.open_road.interval if self.open_road is not None else None,
                "pit_stall": self.open_stall,
                "service": self.open_service,
            }[name]
            # The opening frame already counted as one.
            if current and self.previous_values[name] is True and interval is not None:
                interval.true_frame_count += 1
            self.previous_values[name] = current
        self.last_safe_ref = base_ref
        self.last_safe_tank_level_l = fuel_l
        self.last_safe_session_time_s = raw_session_time_s

    def finish(self) -> None:
        if self.open_road is not None:
            self.incomplete_interval_counts["pit_road"] += 1
        if self.open_stall is not None:
            self.incomplete_interval_counts["pit_stall"] += 1
        if self.open_service is not None:
            self.incomplete_interval_counts["service"] += 1
        if self.open_stint is not None and self.last_safe_ref is not None:
            if self.last_safe_session_time_s is not None:
                self._edge_session_times_s[
                    int(self.last_safe_ref["frame_index"])
                ] = self.last_safe_session_time_s
            self._close_stint(
                self.last_safe_ref,
                end_boundary="FILE_END",
                end_tank_level_l=self.last_safe_tank_level_l,
            )
        self.open_road = None
        self.open_stall = None
        self.open_service = None


def _unavailable_service_content(blocked_claim: str) -> dict[str, object]:
    return {
        "availability": "UNAVAILABLE",
        "blocked_claim": blocked_claim,
        "estimate_available": False,
        "provenance": "UNKNOWN",
        "status": "SKIP_NOT_OBSERVABLE",
    }


def _unknown_service_contents() -> dict[str, object]:
    return {
        "delivered_fuel": _unavailable_service_content("DELIVERED_FUEL_QUANTITY"),
        "driver_swap": _unavailable_service_content("DRIVER_SWAP"),
        "repairs": _unavailable_service_content("REPAIR_CONTENTS"),
        "tire_service": _unavailable_service_content("TIRE_CHANGE_OR_COMPOUND"),
    }


def _required_values(
    sample: TelemetrySample,
) -> tuple[dict[str, bool], float] | None:
    values = {
        "pit_road": _present(sample.pit.on_pit_road, bool),
        "pit_stall": _present(sample.pit.in_pit_stall, bool),
        "service": _present(sample.pit.pitstop_active, bool),
    }
    fuel_l = _present_float(sample.fuel.level_l)
    session_time = _present_float(sample.session.session_time_s)
    session_tick = _present(sample.session.session_tick, int)
    buffer_tick = _present(sample.session.sdk_buffer_tick, int)
    if (
        any(value is None for value in values.values())
        or fuel_l is None
        or session_time is None
        or session_tick is None
        or buffer_tick is None
    ):
        return None
    return {key: bool(value) for key, value in values.items()}, fuel_l


def _continuity_reasons(
    sample: TelemetrySample,
    *,
    frame_index: int,
    evidence: IbtInputEvidence | CollectorInputEvidence,
    emitted: Sequence[TelemetryEvent],
) -> list[str]:
    reasons: list[str] = []
    source_id = _present(sample.source.source_id, str)
    session_id = _present(sample.session.session_id, str)
    source_kind = _present(sample.source.source_kind, SourceKind)
    if (source_id, session_id, source_kind) != (
        evidence.source_id,
        evidence.session_id,
        evidence.source_kind,
    ):
        reasons.append("INPUT_IDENTITY_MISMATCH")
    status = _present(sample.quality.status, QualityStatus)
    stale = _present(sample.quality.stale, bool)
    dropped = _present(sample.quality.dropped_ticks, int)
    if status is None or status is QualityStatus.REJECTED:
        reasons.append("NORMALIZED_SAMPLE_REJECTED")
    if stale is True or (stale is None and frame_index > 0):
        reasons.append("SOURCE_STALE_OR_UNKNOWN")
    if (dropped is not None and dropped > 0) or (dropped is None and frame_index > 0):
        reasons.append("DROPPED_TICKS_OR_UNKNOWN")
    buffer_tick = _present(sample.session.sdk_buffer_tick, int)
    # IBT normalization synthesizes a zero-based buffer tick, so an index
    # mismatch is an independent gap signal.  Collector ticks retain their
    # original SDK values; their gaps/resets are already bound by the adapter,
    # sample quality fields, and shared event pipeline.
    if type(evidence) is IbtInputEvidence and buffer_tick != frame_index:
        reasons.append("IBT_FRAME_GAP")
    if any(event.kind in _RESET_EVENT_KINDS for event in emitted):
        reasons.append("SHARED_EVENT_CONTINUITY_BREAK")
    if frame_index > 0 and any(
        event.kind in {EventKind.SOURCE_STARTED, EventKind.SESSION_STARTED}
        for event in emitted
    ):
        reasons.append("SHARED_EVENT_EPOCH_CHANGED")
    if _required_values(sample) is None:
        reasons.append("REQUIRED_CHANNEL_MISSING_OR_INVALID")
    return list(dict.fromkeys(reasons))


def _build_receipt_from_samples(
    samples: Iterable[TelemetrySample],
    *,
    input_evidence: IbtInputEvidence | CollectorInputEvidence,
    expected_source_sha256: str,
    expected_normalized_samples_sha256: str,
    expected_event_receipt_sha256: str,
    stale_after_s: float,
    opponent_error_policy: str,
) -> dict[str, object]:
    """Private test seam; the public builder requires an opaque adapter run."""

    expected_source = _sha256(expected_source_sha256, "expected source SHA-256")
    expected_normalized = _sha256(
        expected_normalized_samples_sha256, "expected normalized samples SHA-256"
    )
    expected_event = _sha256(
        expected_event_receipt_sha256, "expected event receipt SHA-256"
    )
    if type(input_evidence) is IbtInputEvidence:
        source_digest = input_evidence.source_sha256
        expected_sample_count = input_evidence.record_count
        tick_rate_hz = input_evidence.tick_rate_hz
        if input_evidence.source_kind is not SourceKind.IBT_OFFLINE:
            _fail("SOURCE_KIND_MISMATCH", "IBT M1 receipt requires IBT_OFFLINE")
    elif type(input_evidence) is CollectorInputEvidence:
        source_digest = input_evidence.records_sha256
        expected_sample_count = input_evidence.frame_record_count
        if input_evidence.source_kind not in {
            SourceKind.SDK_LIVE,
            SourceKind.REPLAY_SDK_PROXY,
        }:
            _fail(
                "SOURCE_KIND_MISMATCH",
                "collector M1 receipt requires SDK_LIVE or REPLAY_SDK_PROXY",
            )
        if len(input_evidence.tick_rate_hz_values) != 1:
            _fail(
                "INVALID_INPUT_EVIDENCE",
                "collector M1 receipt requires exactly one SDK tick rate",
            )
        blocking_quality_fields = tuple(
            field
            for field in _COLLECTOR_BLOCKING_QUALITY_FIELDS
            if getattr(input_evidence, field) != 0
        )
        if blocking_quality_fields:
            _fail(
                "COLLECTOR_SIDEBAND_QUALITY_FAILED",
                (
                    "collector sideband quality is inadmissible for pit/service/stint "
                    f"publication: {','.join(blocking_quality_fields)}"
                ),
            )
        tick_rate_hz = input_evidence.tick_rate_hz_values[0]
    else:
        _fail(
            "INVALID_INPUT_EVIDENCE",
            "input evidence must be exact IbtInputEvidence or CollectorInputEvidence",
        )
    if source_digest != expected_source:
        _fail(
            "SOURCE_SHA256_MISMATCH",
            (
                "validated IBT source digest does not match trust root"
                if type(input_evidence) is IbtInputEvidence
                else "validated collector records digest does not match trust root"
            ),
        )
    if input_evidence.completion_status != "COMPLETE":
        _fail("INPUT_NOT_COMPLETE", "M1 receipt requires COMPLETE input evidence")
    if (
        isinstance(stale_after_s, bool)
        or not isinstance(stale_after_s, (int, float))
        or not math.isfinite(float(stale_after_s))
        or stale_after_s <= 0
    ):
        _fail("INVALID_NORMALIZATION", "stale_after_s must be finite and positive")
    if opponent_error_policy not in {"degrade", "reject"}:
        _fail("INVALID_NORMALIZATION", "opponent_error_policy is invalid")

    normalized_digest = hashlib.sha256()
    event_pipeline = TelemetryEventPipeline()
    analyzer = _PitStintAnalyzer(
        tick_rate_hz=tick_rate_hz,
        partial_session_time_required=(
            type(input_evidence) is CollectorInputEvidence
        ),
    )
    sample_count = 0
    for frame_index, sample in enumerate(samples):
        if not isinstance(sample, TelemetrySample):
            _fail("INVALID_SAMPLE", "validated stream contains a non-TelemetrySample value")
        encoded = sample.to_json_line().encode("utf-8")
        normalized_digest.update(len(encoded).to_bytes(8, "little"))
        normalized_digest.update(encoded)
        emitted = event_pipeline.feed(sample)
        reasons = _continuity_reasons(
            sample,
            frame_index=frame_index,
            evidence=input_evidence,
            emitted=emitted,
        )
        if reasons:
            boundary_ref = None
            boundary_tank_level_l = _present_float(sample.fuel.level_l)
            boundary_session_time_s = _present_float(
                sample.session.session_time_s
            )
            if (
                "REQUIRED_CHANNEL_MISSING_OR_INVALID" in reasons
                and boundary_tank_level_l is None
                and boundary_session_time_s is not None
                and _present(sample.session.session_tick, int) is not None
            ):
                boundary_ref = _sample_ref(sample, frame_index)
            for reason in reasons:
                analyzer.break_continuity(
                    reason,
                    boundary_ref=boundary_ref,
                    boundary_tank_level_l=boundary_tank_level_l,
                    boundary_session_time_s=boundary_session_time_s,
                )
            required = _required_values(sample)
            if required is not None and not {
                "INPUT_IDENTITY_MISMATCH",
                "NORMALIZED_SAMPLE_REJECTED",
                "SOURCE_STALE_OR_UNKNOWN",
                "DROPPED_TICKS_OR_UNKNOWN",
                "IBT_FRAME_GAP",
            }.intersection(reasons):
                values, fuel_l = required
                analyzer.process(
                    sample,
                    frame_index,
                    emitted,
                    values=values,
                    fuel_l=fuel_l,
                )
        else:
            required = _required_values(sample)
            assert required is not None
            values, fuel_l = required
            analyzer.process(
                sample,
                frame_index,
                emitted,
                values=values,
                fuel_l=fuel_l,
            )
        sample_count += 1
    analyzer.finish()
    event_receipt = event_pipeline.finish().to_dict()
    normalized_receipt = {
        "contract_version": TELEMETRY_CONTRACT_VERSION,
        "sample_count": sample_count,
        "samples_sha256": normalized_digest.hexdigest(),
    }
    if sample_count != expected_sample_count:
        _fail("SAMPLE_COUNT_MISMATCH", "normalized samples do not close to input evidence")
    if normalized_receipt["samples_sha256"] != expected_normalized:
        _fail(
            "NORMALIZED_SHA256_MISMATCH",
            "normalized stream digest does not match the independent trust root",
        )
    if event_receipt["receipt_sha256"] != expected_event:
        _fail(
            "EVENT_RECEIPT_SHA256_MISMATCH",
            "shared telemetry-event receipt does not match the independent trust root",
        )
    if event_receipt["sample_count"] != sample_count:
        _fail("EVENT_SAMPLE_COUNT_MISMATCH", "shared event receipt sample count does not close")

    quality_reasons = list(analyzer.quality_reasons)
    if (
        event_receipt["rejected_sample_count"]
        and "NORMALIZED_SAMPLE_REJECTED" not in quality_reasons
    ):
        quality_reasons.append("NORMALIZED_SAMPLE_REJECTED")
    if event_receipt["source_epoch_count"] != 1:
        quality_reasons.append("MULTIPLE_SOURCE_EPOCHS")
    if event_receipt["session_epoch_count"] != 1:
        quality_reasons.append("MULTIPLE_SESSION_EPOCHS")
    quality_reasons = list(dict.fromkeys(quality_reasons))
    complete_stint_count = sum(stint["status"] == "COMPLETE" for stint in analyzer.stints)
    service_count = sum(
        len(cycle["service_episodes"]) for cycle in analyzer.pit_cycles
    )
    if quality_reasons:
        detection_status = "WAIT_DATA_QUALITY"
        detection_reasons = quality_reasons
    elif not analyzer.pit_cycles:
        detection_status = "WAIT_PIT_SAMPLE"
        detection_reasons = ["NO_COMPLETE_PIT_ROAD_INTERVAL_OBSERVED"]
    elif not service_count:
        detection_status = "WAIT_SERVICE_SAMPLE"
        detection_reasons = ["NO_COMPLETE_SERVICE_EPISODE_OBSERVED"]
    else:
        detection_status = "PASS_DATA"
        detection_reasons = []
    capabilities = {
        "complete_stint_analysis": {
            "reasons": [] if complete_stint_count else ["NO_COMPLETE_STINT_OBSERVED"],
            "status": "PASS_DATA" if complete_stint_count else "WAIT_COMPLETE_STINT",
        },
        "human_validation": {
            "reasons": ["PIT_AND_SERVICE_EDGES_NOT_HUMAN_LABELED"],
            "status": "WAIT_HUMAN_LABELS",
        },
        "pit_and_service_detection": {
            "reasons": detection_reasons,
            "status": detection_status,
        },
        "service_contents": {
            "reasons": [
                "PITSTOPACTIVE_DOES_NOT_IDENTIFY_SERVICE_CONTENTS",
                "TIRE_SET_OR_COMPOUND_VALUES_DO_NOT_PROVE_A_TIRE_CHANGE",
                "FUELLEVEL_ENDPOINT_DELTA_IS_NOT_DELIVERED_FUEL",
            ],
            "status": "SKIP_NOT_OBSERVABLE",
        },
    }
    input_evidence_payload = input_evidence.to_dict()
    input_binding = {
        "event_receipt_sha256": event_receipt["receipt_sha256"],
        "expected_event_receipt_sha256": expected_event,
        "expected_normalized_samples_sha256": expected_normalized,
        "expected_source_sha256": expected_source,
        "input_evidence_sha256": canonical_sha256(input_evidence_payload),
        "normalization_profile": {
            "opponent_error_policy": opponent_error_policy,
            "profile_version": NORMALIZATION_PROFILE_VERSION,
            "stale_after_us": round(float(stale_after_s) * 1_000_000),
        },
        "normalized_samples_sha256": normalized_receipt["samples_sha256"],
        # Frozen v1 field name: for collector input this binds records_sha256.
        "source_sha256": source_digest,
    }
    input_binding["input_lineage_sha256"] = canonical_sha256(input_binding)
    binding: dict[str, object] = {
        "advisor_only": True,
        "attestation_status": "NOT_R7_ATTESTED",
        "capabilities": capabilities,
        "contract_version": PIT_STINT_CONTRACT_VERSION,
        "derivation_status": "POST_ADMISSION_PACKAGE_EXTERNAL",
        "execution_mode": "SHADOW_ONLY",
        "incomplete_interval_counts": analyzer.incomplete_interval_counts,
        "input_binding": input_binding,
        "input_evidence": input_evidence_payload,
        "normalized_input_receipt": normalized_receipt,
        "pit_cycles": analyzer.pit_cycles,
        "quality_gate": {
            "reasons": quality_reasons,
            "status": "PASS" if not quality_reasons else "DEGRADED",
        },
        "recommendations": [],
        "service_contents": _unknown_service_contents(),
        "status": "CANDIDATE_NOT_GOLDEN",
        "stints": analyzer.stints,
        "summary": {
            "complete_stint_count": complete_stint_count,
            "partial_stint_count": len(analyzer.stints) - complete_stint_count,
            "pit_cycle_count": len(analyzer.pit_cycles),
            "service_episode_count": service_count,
        },
        "upstream_event_receipt": event_receipt,
    }
    receipt = {**binding, "pit_stint_receipt_sha256": canonical_sha256(binding)}
    return validate_pit_stint_receipt(
        receipt,
        expected_pit_stint_receipt_sha256=receipt["pit_stint_receipt_sha256"],
    )


def build_pit_stint_receipt(
    run: ValidatedIbtRun | ValidatedCollectorRun,
    *,
    expected_source_sha256: str,
    expected_normalized_samples_sha256: str,
    expected_event_receipt_sha256: str,
) -> dict[str, object]:
    """Consume one active adapter-created run into an M1 receipt."""

    if type(run) not in {ValidatedIbtRun, ValidatedCollectorRun}:
        _fail(
            "UNVALIDATED_INPUT",
            "run must come directly from an open validated telemetry adapter",
        )
    state = _validated_run_state(run)
    expected_evidence_type = (
        IbtInputEvidence if type(run) is ValidatedIbtRun else CollectorInputEvidence
    )
    if state is None or type(state.evidence) is not expected_evidence_type:
        _fail(
            "UNVALIDATED_INPUT",
            "run must come directly from an open validated telemetry adapter",
        )
    if state.samples is not run.samples or state.evidence is not run.evidence:
        _fail("VALIDATED_RUN_STATE_MISMATCH", "validated run registry state is inconsistent")
    return _build_receipt_from_samples(
        state.samples,
        input_evidence=state.evidence,
        expected_source_sha256=expected_source_sha256,
        expected_normalized_samples_sha256=expected_normalized_samples_sha256,
        expected_event_receipt_sha256=expected_event_receipt_sha256,
        stale_after_s=state.stale_after_s,
        opponent_error_policy=state.opponent_error_policy,
    )


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise PitStintReceiptError(
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
    parser.add_argument("ibt", type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--expected-normalized-samples-sha256", required=True)
    parser.add_argument("--expected-event-receipt-sha256", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with open_ibt_telemetry(
            args.ibt,
            source_id=args.source_id,
            session_id=args.session_id,
        ) as run:
            receipt = build_pit_stint_receipt(
                run,
                expected_source_sha256=args.expected_source_sha256,
                expected_normalized_samples_sha256=(
                    args.expected_normalized_samples_sha256
                ),
                expected_event_receipt_sha256=args.expected_event_receipt_sha256,
            )
        encoded = _canonical_json(receipt, newline=True)
        if args.output is not None:
            _write_exclusive(args.output, encoded)
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0 if receipt["quality_gate"]["status"] == "PASS" else 5  # type: ignore[index]
    except (IbtFormatError, OSError, PitStintReceiptError, TelemetryAdapterError) as exc:
        code = exc.code if isinstance(exc, PitStintReceiptError) else "INPUT_READ_FAILED"
        error = {
            "contract_version": PIT_STINT_CONTRACT_VERSION,
            "error": str(exc),
            "status": "FAIL",
        }
        print(f"{code}: {json.dumps(error, sort_keys=True)}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
