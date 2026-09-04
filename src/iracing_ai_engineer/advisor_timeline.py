"""Bind source-admitted M2/M3 evidence to replayable shadow speech policy.

The public builder consumes an *active* opaque telemetry run. It recomputes
the normalized-stream digest, event receipt, and SessionTick -> SessionTime
clock from that same run. M2 receipts require separately pinned digests and
are fully re-admitted. M3 is rebuilt from separately pinned serialized
driving replay bytes. No caller-supplied clock or self-hash can promote data.

This module emits no prose, audio, network traffic, or simulator control.
Admitted M2 candidates enter the shadow speech policy independently of driving
diagnosis promotion. Missing strategy evidence remains a P3 ``WAIT_DATA``;
driving gates and all timing/mute requirements remain intact.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NoReturn

from . import m2_strategy as _M2
from .adapters import (
    CollectorInputEvidence,
    IbtInputEvidence,
    ValidatedCollectorRun,
    ValidatedIbtRun,
    _validated_run_state,
)
from .driving_diagnosis import (
    _PROMOTION_GATES,
    DIAGNOSIS_EVIDENCE_CONTRACT_VERSION,
    DiagnosisEvidenceError,
    build_diagnosis_evidence,
)
from .events import TelemetryEventPipeline
from .model_replay import FuelModelReplayError, _sample_identity, _validate_input_evidence
from .speech_policy import (
    MESSAGE_TEMPLATE_ID,
    MessageClass,
    PolicyBinding,
    Priority,
    SpeechEnvelope,
    SpeechPolicyConfig,
    SpeechPolicyError,
    SpeechRefresh,
    process_speech_policy_run,
    replay_speech_policy,
)
from .telemetry import Presence, SourceKind, TelemetrySample

ADVISOR_TIMELINE_CONTRACT_VERSION = "advisor-timeline-v3"
ADVISOR_CLOCK_RECEIPT_CONTRACT_VERSION = "advisor-clock-receipt-v1"
DEFAULT_LEASE_DURATION_US = 2_000_000

_SHA256_CHARS = frozenset("0123456789abcdef")
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
_STABLE_M2_KEYS = (
    "event_receipt_sha256",
    "fuel_replay_sha256",
    "m1_receipt_sha256",
    "model_semantic_sha256",
    "normalized_samples_sha256",
    "sample_count",
    "session_id",
    "source_id",
    "source_kind",
    "source_sha256",
)
_CLOCK_RECEIPT_KEYS = frozenset(
    {
        "authenticity_status",
        "bindings",
        "clock_receipt_sha256",
        "completion_status",
        "contract_version",
        "input_evidence_sha256",
        "input_kind",
        "session_epoch",
        "source_binding",
        "source_content_sha256_field",
        "source_epoch",
        "tick_rate_hz",
    }
)
_CLOCK_BINDING_KEYS = frozenset({"decision_tick", "m2_receipt_sha256", "session_time_us"})
_DIAGNOSIS_KEYS = frozenset(
    {
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
)
_DIAGNOSIS_INPUT_KEYS = frozenset(
    {
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
)
_DIAGNOSIS_SOURCE_KEYS = frozenset(
    {
        "authenticity_status",
        "completion_status",
        "input_evidence_sha256",
        "input_kind",
        "session_id",
        "source_content_sha256",
        "source_content_sha256_field",
        "source_id",
        "source_kind",
    }
)
_ACTION_KEYS = frozenset(
    {
        "change_tires",
        "estimated_stationary_service_s",
        "estimated_total_pit_loss_s",
        "fuel_add_l",
        "recommended_lap_from_now",
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
_VALID_UNTIL_KEYS = frozenset(
    {
        "context_sha256",
        "pit_entry_deadline_laps_completed",
        "recompute_after_decision_tick",
        "rules_lineage_sha256",
        "session_epoch",
        "source_epoch",
    }
)
_EVENT_IDENTITY_OUTPUT_KEYS = frozenset(set(_M2._IDENTITY_KEYS) | {"identity_sha256"})
_HORIZON_KEYS = frozenset(
    {
        "branches",
        "horizon_sha256",
        "kind",
        "one_more_lap_status",
        "reason_codes",
        "status",
    }
)
_HORIZON_BRANCH_KEYS = frozenset({"branch_id", "condition", "laps_to_go"})
_CALIBRATION_OUTPUT_KEYS = frozenset({"calibrated_model", "observed_m1"})
_OBSERVED_CALIBRATION_KEYS = frozenset(
    {
        "complete_stint_count",
        "observed_net_tank_changes",
        "pit_lane_loss_s",
        "pit_road_elapsed_s",
        "reason_codes",
        "refuel_rate_l_per_s",
        "service_active_elapsed_s",
        "service_content_model",
        "stall_elapsed_s",
        "status",
    }
)
_OBSERVED_TANK_CHANGE_KEYS = frozenset(
    {
        "end_fuel_level_l",
        "interpretation",
        "provenance",
        "start_fuel_level_l",
        "value_l",
    }
)
_OBSERVED_CALIBRATION_REASONS = [
    "PIT_ROAD_ELAPSED_IS_NOT_COUNTERFACTUAL_PIT_LOSS",
    "PITSTOPACTIVE_DOES_NOT_IDENTIFY_SERVICE_CONTENT",
    "TANK_ENDPOINT_DELTA_IS_NOT_DELIVERED_FUEL",
]
_RULES_BINDING_KEYS = frozenset(
    {
        "exact_selector_match",
        "official_event_rules",
        "profile_id",
        "profile_sha256",
        "profile_version",
        "reason_codes",
        "rules_lineage_sha256",
        "source_document_sha256",
        "status",
    }
)
_TRAFFIC_OUTPUT_KEYS = frozenset({"estimate", "input", "status"})
_LEGACY_TRAFFIC_OUTPUT_KEYS = frozenset({"input", "status"})
_CAPABILITY_KEYS = frozenset({"reason_codes", "status"})
_BASE_CAPABILITY_NAMES = frozenset(
    {
        "event_rules_identity",
        "one_more_lap",
        "pit_loss_calibration",
        "pit_open_and_penalty_state",
        "service_labels",
        "strategy_data",
        "traffic_data",
    }
)
_V2_BASE_CAPABILITY_NAMES = frozenset(
    set(_BASE_CAPABILITY_NAMES) | {"tire_strategy"}
)
_CAPABILITY_NAMES = frozenset(set(_BASE_CAPABILITY_NAMES) | {"lifecycle", "race_recommendation"})
_V2_CAPABILITY_NAMES = frozenset(
    set(_V2_BASE_CAPABILITY_NAMES) | {"lifecycle", "race_recommendation"}
)
_QUALITY_GATE_KEYS = frozenset({"reason_codes", "status"})
_STRATEGY_WAIT_REASONS = frozenset(
    {
        "DISTANCE_HORIZON_UNAVAILABLE",
        "FUEL_TO_END_EXCEEDS_TANK",
        "MULTI_STOP_RULE_NOT_SUPPORTED",
        "NO_COMMON_PIT_WINDOW_ACROSS_HORIZON_BRANCHES",
        "RECOMMENDED_STOP_CONSUMES_RESERVE",
        "TANK_CANNOT_COVER_ONE_CONSERVATIVE_LAP",
        "V1_REQUIRES_EXACTLY_ONE_FUEL_STOP_IN_EVERY_BRANCH",
    }
)
_TIMELINE_KEYS = frozenset(
    {
        "advisor_only",
        "advisor_timeline_sha256",
        "bridge_policy",
        "clock_receipt",
        "contract_version",
        "diagnosis_evidence_sha256",
        "execution_mode",
        "input_binding",
        "safety",
        "speech_policy_run",
        "status",
        "summary",
    }
)


class AdvisorTimelineError(ValueError):
    """Fail-closed bridge error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _fail(code: str, message: str) -> NoReturn:
    raise AdvisorTimelineError(code, message)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise AdvisorTimelineError(
            "CANONICAL_JSON_FAILED", "value is not canonical-JSON-safe"
        ) from exc


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _mapping(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        _fail("SCHEMA_INVALID", f"{name} must be an object")
    return value


def _exact_mapping(value: object, keys: frozenset[str] | set[str], name: str) -> dict[str, object]:
    result = _mapping(value, name)
    if set(result) != set(keys):
        _fail("SCHEMA_INVALID", f"{name} keys are invalid")
    return result


def _list(value: object, name: str) -> list[object]:
    if type(value) is not list:
        _fail("SCHEMA_INVALID", f"{name} must be an array")
    return value


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _SHA256_CHARS for character in value)
    ):
        _fail("DIGEST_INVALID", f"{name} must be a lowercase SHA-256 digest")
    return value


def _identifier(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in value)
    ):
        _fail("SCHEMA_INVALID", f"{name} is not a valid identifier")
    return value


def _plain_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail("SCHEMA_INVALID", f"{name} must be a plain integer >= {minimum}")
    return value


def _finite_number(value: object, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("SCHEMA_INVALID", f"{name} must be numeric")
    converted = float(value)
    if not math.isfinite(converted) or converted < minimum:
        _fail("SCHEMA_INVALID", f"{name} must be finite and >= {minimum}")
    return converted


def _expected_digests(values: Sequence[str], *, count: int, name: str) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        _fail("SCHEMA_INVALID", f"{name} must be a digest sequence")
    if len(values) != count:
        _fail("DIGEST_BINDING_INVALID", f"{name} count mismatch")
    return tuple(_sha256(item, f"{name}[{index}]") for index, item in enumerate(values))


@dataclass(frozen=True, slots=True)
class DecisionClockBinding:
    """One source-derived M2 decision clock edge."""

    m2_receipt_sha256: str
    decision_tick: int
    session_time_us: int

    def __post_init__(self) -> None:
        _sha256(self.m2_receipt_sha256, "clock M2 receipt SHA-256")
        _plain_int(self.decision_tick, "clock decision_tick")
        _plain_int(self.session_time_us, "clock session_time_us")

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_tick": self.decision_tick,
            "m2_receipt_sha256": self.m2_receipt_sha256,
            "session_time_us": self.session_time_us,
        }


def _raw_decision_ticks(
    receipts: Sequence[Mapping[str, object]],
) -> tuple[int, ...]:
    result: list[int] = []
    for index, value in enumerate(receipts):
        receipt = _mapping(value, f"M2 receipt[{index}]")
        context = _mapping(receipt.get("strategy_context"), "M2 strategy context")
        observation = _mapping(context.get("observation"), "M2 observation")
        result.append(_plain_int(observation.get("decision_tick"), "M2 decision tick"))
    return tuple(result)


def _clock_from_active_run(
    run: ValidatedIbtRun | ValidatedCollectorRun,
    receipts: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if type(run) not in {ValidatedIbtRun, ValidatedCollectorRun}:
        _fail("RUN_NOT_ACTIVE", "run must be an active adapter-created validated run")
    state = _validated_run_state(run)
    if state is None or state.samples is not run.samples or state.evidence is not run.evidence:
        _fail("RUN_NOT_ACTIVE", "run must be an active adapter-created validated run")

    if type(run) is ValidatedIbtRun:
        if type(state.evidence) is not IbtInputEvidence:
            _fail("RUN_NOT_ACTIVE", "validated IBT registry state is invalid")
        input_kind = "ibt"
        tick_rate_hz = state.evidence.tick_rate_hz
        source_digest_field = "source_sha256"
    else:
        if type(state.evidence) is not CollectorInputEvidence:
            _fail("RUN_NOT_ACTIVE", "validated collector registry state is invalid")
        rates = state.evidence.tick_rate_hz_values
        if len(rates) != 1:
            _fail("CLOCK_SOURCE_INVALID", "collector must bind exactly one tick rate")
        input_kind = "collector"
        tick_rate_hz = rates[0]
        source_digest_field = "records_sha256"
    try:
        evidence, source_id, session_id, source_kind, expected_count = _validate_input_evidence(
            state.evidence,
            input_kind=input_kind,
            tick_rate_hz=tick_rate_hz,
        )
    except FuelModelReplayError as exc:
        raise AdvisorTimelineError("CLOCK_SOURCE_INVALID", str(exc)) from exc

    ticks = _raw_decision_ticks(receipts)
    targets = set(ticks)
    tick_times: dict[int, int] = {}
    normalized = hashlib.sha256()
    event_pipeline = TelemetryEventPipeline()
    sample_count = 0
    for sample in state.samples:
        if not isinstance(sample, TelemetrySample):
            _fail("CLOCK_SOURCE_INVALID", "run contains a non-telemetry sample")
        if _sample_identity(sample) != (source_id, session_id, source_kind):
            _fail("CLOCK_SOURCE_INVALID", "sample identity differs from run evidence")
        encoded = sample.to_json_line().encode("utf-8")
        normalized.update(len(encoded).to_bytes(8, "little"))
        normalized.update(encoded)
        try:
            event_pipeline.feed(sample)
        except (TypeError, ValueError) as exc:
            raise AdvisorTimelineError("CLOCK_SOURCE_INVALID", str(exc)) from exc
        sample_count += 1

        tick_field = sample.session.session_tick
        if tick_field.presence is not Presence.PRESENT or type(tick_field.value) is not int:
            continue
        tick = tick_field.value
        if tick not in targets:
            continue
        time_field = sample.session.session_time_s
        if (
            time_field.presence is not Presence.PRESENT
            or isinstance(time_field.value, bool)
            or not isinstance(time_field.value, (int, float))
            or not math.isfinite(float(time_field.value))
            or float(time_field.value) < 0.0
        ):
            _fail("CLOCK_BINDING_INVALID", f"decision tick {tick} lacks SessionTime")
        time_us = int(round(float(time_field.value) * 1_000_000))
        prior = tick_times.get(tick)
        if prior is not None and prior != time_us:
            _fail(
                "CLOCK_BINDING_INVALID",
                f"decision tick {tick} has conflicting times",
            )
        tick_times[tick] = time_us

    try:
        event_receipt = event_pipeline.finish().to_dict()
    except (TypeError, ValueError) as exc:
        raise AdvisorTimelineError("CLOCK_SOURCE_INVALID", str(exc)) from exc
    if sample_count != expected_count or event_receipt.get("sample_count") != sample_count:
        _fail("CLOCK_SOURCE_INVALID", "run sample count does not close to evidence")
    if (
        event_receipt.get("source_epoch_count") != 1
        or event_receipt.get("session_epoch_count") != 1
    ):
        _fail("CLOCK_SOURCE_EPOCH_INVALID", "one timeline requires exactly one epoch")
    missing_ticks = [tick for tick in ticks if tick not in tick_times]
    if missing_ticks:
        _fail("CLOCK_BINDING_INVALID", f"decision ticks absent from run: {missing_ticks}")

    source_binding = {
        "event_receipt_sha256": _sha256(
            event_receipt.get("receipt_sha256"), "event receipt SHA-256"
        ),
        "normalized_samples_sha256": normalized.hexdigest(),
        "sample_count": sample_count,
        "session_id": session_id,
        "source_id": source_id,
        "source_kind": source_kind.value,
        "source_sha256": _sha256(evidence[source_digest_field], "source SHA-256"),
    }
    bindings = [
        DecisionClockBinding(
            m2_receipt_sha256=_sha256(
                _mapping(receipt, "M2 receipt").get("m2_strategy_receipt_sha256"),
                "M2 receipt SHA-256",
            ),
            decision_tick=tick,
            session_time_us=tick_times[tick],
        ).to_dict()
        for receipt, tick in zip(receipts, ticks, strict=True)
    ]
    base: dict[str, object] = {
        "authenticity_status": evidence["authenticity_status"],
        "bindings": bindings,
        "completion_status": evidence["completion_status"],
        "contract_version": ADVISOR_CLOCK_RECEIPT_CONTRACT_VERSION,
        "input_evidence_sha256": canonical_sha256(evidence),
        "input_kind": input_kind,
        "session_epoch": 1,
        "source_binding": source_binding,
        "source_content_sha256_field": source_digest_field,
        "source_epoch": 1,
        "tick_rate_hz": tick_rate_hz,
    }
    return {**base, "clock_receipt_sha256": canonical_sha256(base)}


def _validate_clock_receipt(
    value: object,
    receipts: Sequence[Mapping[str, object]],
    *,
    expected_digest: str | None = None,
) -> tuple[dict[str, object], dict[str, object], tuple[DecisionClockBinding, ...]]:
    receipt = _exact_mapping(value, _CLOCK_RECEIPT_KEYS, "clock receipt")
    if receipt.get("contract_version") != ADVISOR_CLOCK_RECEIPT_CONTRACT_VERSION:
        _fail("CLOCK_BINDING_INVALID", "unsupported clock receipt contract")
    stored = _sha256(receipt.get("clock_receipt_sha256"), "clock receipt SHA-256")
    if expected_digest is not None and stored != _sha256(
        expected_digest, "expected clock receipt SHA-256"
    ):
        _fail(
            "CLOCK_BINDING_INVALID",
            "clock receipt failed independent digest binding",
        )
    material = {key: item for key, item in receipt.items() if key != "clock_receipt_sha256"}
    if canonical_sha256(material) != stored:
        _fail("CLOCK_BINDING_INVALID", "clock receipt self hash mismatch")
    source = _exact_mapping(
        receipt.get("source_binding"), _SOURCE_BINDING_KEYS, "clock source binding"
    )
    for key in (
        "event_receipt_sha256",
        "normalized_samples_sha256",
        "source_sha256",
    ):
        _sha256(source.get(key), f"clock source binding {key}")
    for key in ("source_id", "session_id", "source_kind"):
        _identifier(source.get(key), f"clock source binding {key}")
    _plain_int(source.get("sample_count"), "clock sample_count", minimum=1)
    input_kind = receipt.get("input_kind")
    if input_kind == "ibt":
        expected_kind = SourceKind.IBT_OFFLINE.value
        expected_field = "source_sha256"
        expected_authenticity = "HASHED_LOCAL_FILE_NOT_AUTHENTICATED"
    elif input_kind == "collector":
        if source.get("source_kind") not in {
            SourceKind.SDK_LIVE.value,
            SourceKind.REPLAY_SDK_PROXY.value,
        }:
            _fail("CLOCK_BINDING_INVALID", "collector source kind is invalid")
        expected_kind = str(source["source_kind"])
        expected_field = "records_sha256"
        expected_authenticity = "SELF_CONSISTENT_NOT_AUTHENTICATED"
    else:
        _fail("CLOCK_BINDING_INVALID", "clock input_kind is invalid")
    if (
        source.get("source_kind") != expected_kind
        or receipt.get("source_content_sha256_field") != expected_field
        or receipt.get("authenticity_status") != expected_authenticity
        or receipt.get("completion_status") != "COMPLETE"
    ):
        _fail("CLOCK_BINDING_INVALID", "clock source metadata is invalid")
    _sha256(receipt.get("input_evidence_sha256"), "input evidence SHA-256")
    tick_rate = _plain_int(receipt.get("tick_rate_hz"), "clock tick_rate_hz", minimum=1)
    if tick_rate > 360:
        _fail("CLOCK_BINDING_INVALID", "clock tick rate exceeds 360 Hz")
    if receipt.get("source_epoch") != 1 or receipt.get("session_epoch") != 1:
        _fail("CLOCK_BINDING_INVALID", "clock receipt epoch is invalid")

    raw_bindings = _list(receipt.get("bindings"), "clock bindings")
    if len(raw_bindings) != len(receipts):
        _fail("CLOCK_BINDING_INVALID", "clock/M2 binding count mismatch")
    clocks: list[DecisionClockBinding] = []
    last_tick = -1
    last_time = -1
    for index, (raw, m2_receipt) in enumerate(zip(raw_bindings, receipts, strict=True)):
        item = _exact_mapping(raw, _CLOCK_BINDING_KEYS, f"clock binding[{index}]")
        clock = DecisionClockBinding(
            m2_receipt_sha256=item["m2_receipt_sha256"],  # type: ignore[arg-type]
            decision_tick=item["decision_tick"],  # type: ignore[arg-type]
            session_time_us=item["session_time_us"],  # type: ignore[arg-type]
        )
        m2 = _mapping(m2_receipt, f"M2 receipt[{index}]")
        observation = _mapping(
            _mapping(m2.get("strategy_context"), "M2 strategy context").get("observation"),
            "M2 observation",
        )
        if clock.m2_receipt_sha256 != m2.get(
            "m2_strategy_receipt_sha256"
        ) or clock.decision_tick != observation.get("decision_tick"):
            _fail("CLOCK_BINDING_INVALID", "clock binding does not match M2 receipt")
        if clock.decision_tick <= last_tick or clock.session_time_us <= last_time:
            _fail("CLOCK_BINDING_INVALID", "clock tick/time is not strictly monotonic")
        last_tick = clock.decision_tick
        last_time = clock.session_time_us
        clocks.append(clock)
    return receipt, source, tuple(clocks)


def _reason_codes(value: object, name: str) -> list[str]:
    raw = _list(value, name)
    if any(type(item) is not str or not item for item in raw) or len(raw) != len(set(raw)):
        _fail("M2_RECEIPT_INVALID", f"{name} must contain unique non-empty strings")
    return [str(item) for item in raw]


def _validate_rules_binding(
    value: object,
    *,
    identity: Mapping[str, object],
) -> dict[str, object]:
    rules = _exact_mapping(value, _RULES_BINDING_KEYS, "M2 rules binding")
    if (
        type(rules.get("exact_selector_match")) is not bool
        or type(rules.get("official_event_rules")) is not bool
    ):
        _fail("M2_RECEIPT_INVALID", "M2 rules booleans are invalid")
    reasons = _reason_codes(rules.get("reason_codes"), "M2 rules reasons")
    lineage = _sha256(rules.get("rules_lineage_sha256"), "M2 rules lineage")
    base = {key: item for key, item in rules.items() if key != "rules_lineage_sha256"}
    if canonical_sha256(base) != lineage:
        _fail("M2_RECEIPT_INVALID", "M2 rules lineage self hash mismatch")

    profile_values = (
        rules.get("profile_id"),
        rules.get("profile_sha256"),
        rules.get("profile_version"),
        rules.get("source_document_sha256"),
    )
    if all(item is None for item in profile_values):
        expected_missing = {
            "exact_selector_match": False,
            "official_event_rules": False,
            "profile_id": None,
            "profile_sha256": None,
            "profile_version": None,
            "reason_codes": ["RULES_PROFILE_MISSING"],
            "source_document_sha256": None,
            "status": "WAIT_EVENT_RULES_IDENTITY",
        }
        if base != expected_missing:
            _fail("M2_RECEIPT_INVALID", "missing M2 rules profile semantics are invalid")
        return rules
    if any(item is None for item in profile_values):
        _fail("M2_RECEIPT_INVALID", "M2 rules profile binding is partial")
    _identifier(rules.get("profile_id"), "M2 rules profile id")
    _sha256(rules.get("profile_sha256"), "M2 rules profile SHA-256")
    _plain_int(rules.get("profile_version"), "M2 rules profile version", minimum=1)
    _sha256(rules.get("source_document_sha256"), "M2 rules source SHA-256")

    identity_complete = all(identity.get(key) is not None for key in _M2._SELECTOR_KEYS) and all(
        type(identity.get(key)) is int and int(identity[key]) > 0
        for key in ("series_id", "season_id", "track_id", "car_class_id")
    )
    exact = rules["exact_selector_match"]
    if rules.get("official_event_rules") != exact:
        _fail("M2_RECEIPT_INVALID", "M2 official rules status is not selector-derived")
    if not identity_complete:
        expected_status = "WAIT_EVENT_RULES_IDENTITY"
        expected_reasons = ["EVENT_IDENTITY_INCOMPLETE"]
        expected_exact = False
    elif exact:
        expected_status = "PASS_VERIFIED_OFFICIAL_EXACT_MATCH"
        expected_reasons = []
        expected_exact = True
    else:
        expected_status = "WAIT_EVENT_RULES_IDENTITY"
        expected_reasons = ["EVENT_SELECTOR_MISMATCH"]
        expected_exact = False
    if (
        exact is not expected_exact
        or rules.get("status") != expected_status
        or reasons != expected_reasons
    ):
        _fail("M2_RECEIPT_INVALID", "M2 rules selector semantics are invalid")
    return rules


def _validate_horizon(
    value: object,
    *,
    context_horizon: Mapping[str, object],
    rules_binding: Mapping[str, object],
) -> dict[str, object]:
    horizon = _exact_mapping(value, _HORIZON_KEYS, "M2 horizon")
    _sha256(horizon.get("horizon_sha256"), "M2 horizon SHA-256")
    _reason_codes(horizon.get("reason_codes"), "M2 horizon reasons")
    for index, raw in enumerate(_list(horizon.get("branches"), "M2 horizon branches")):
        branch = _exact_mapping(raw, _HORIZON_BRANCH_KEYS, f"M2 horizon branch[{index}]")
        _identifier(branch.get("branch_id"), "M2 horizon branch id")
        _identifier(branch.get("condition"), "M2 horizon branch condition")
        _plain_int(branch.get("laps_to_go"), "M2 horizon branch laps")

    # The receipt intentionally stores only a rules-profile digest, not the
    # profile body. Enumerate the two supported finish-rule semantics and the
    # no-profile path, then require an exact upstream-derived horizon shape.
    profile_candidates: list[Mapping[str, object] | None] = [None]
    if rules_binding.get("profile_sha256") is not None:
        profile_candidates.extend(
            [
                {"official_rules": {"finish_rule": "LAP_LIMITED"}},
                {"official_rules": {"finish_rule": "TIMED_LEADER_CROSSING"}},
            ]
        )
    candidates = [
        _M2._build_horizon(
            context_horizon,
            rules_binding=rules_binding,
            rules_profile=profile,
        )
        for profile in profile_candidates
    ]
    if horizon not in candidates:
        _fail("M2_RECEIPT_INVALID", "M2 horizon is not context/rules-derived")
    return horizon


def _validate_observed_calibration(value: object) -> dict[str, object]:
    observed = _exact_mapping(
        value,
        _OBSERVED_CALIBRATION_KEYS,
        "M2 observed M1 calibration",
    )
    if (
        observed.get("status") != "OBSERVED_SAMPLE_ONLY"
        or observed.get("pit_lane_loss_s") is not None
        or observed.get("refuel_rate_l_per_s") is not None
        or observed.get("service_content_model") is not None
        or observed.get("reason_codes") != _OBSERVED_CALIBRATION_REASONS
    ):
        _fail("M2_RECEIPT_INVALID", "M2 observed calibration boundary is invalid")
    _plain_int(
        observed.get("complete_stint_count"),
        "M2 observed complete stint count",
    )
    for field in (
        "pit_road_elapsed_s",
        "service_active_elapsed_s",
        "stall_elapsed_s",
    ):
        for index, item in enumerate(_list(observed.get(field), f"M2 observed {field}")):
            _finite_number(item, f"M2 observed {field}[{index}]")
    for index, raw in enumerate(
        _list(observed.get("observed_net_tank_changes"), "M2 observed tank changes")
    ):
        change = _exact_mapping(
            raw,
            _OBSERVED_TANK_CHANGE_KEYS,
            f"M2 observed tank change[{index}]",
        )
        if (
            change.get("interpretation")
            != "OBSERVED_ENDPOINT_TANK_LEVEL_DIFFERENCE_NOT_DELIVERED_FUEL"
            or change.get("provenance") != "SDK_DIRECT_ENDPOINT_DIFFERENCE"
        ):
            _fail("M2_RECEIPT_INVALID", "M2 observed tank change semantics are invalid")
        for field in ("end_fuel_level_l", "start_fuel_level_l", "value_l"):
            item = change.get(field)
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                _fail("M2_RECEIPT_INVALID", f"M2 observed tank change {field} is invalid")
            if not math.isfinite(float(item)):
                _fail("M2_RECEIPT_INVALID", f"M2 observed tank change {field} is invalid")
    return observed


def _capability(value: object, name: str) -> dict[str, object]:
    result = _exact_mapping(value, _CAPABILITY_KEYS, f"M2 capability {name}")
    _identifier(result.get("status"), f"M2 capability {name} status")
    _reason_codes(result.get("reason_codes"), f"M2 capability {name} reasons")
    return result


def _validate_tire_strategy_surface(
    value: object,
    *,
    context: Mapping[str, Any],
    horizon: Mapping[str, object],
    rules_binding: Mapping[str, object],
) -> dict[str, object]:
    strategy = _exact_mapping(
        value,
        set(_M2._TIRE_STRATEGY_KEYS),
        "M2 tire strategy",
    )
    status = _identifier(strategy.get("status"), "M2 tire-strategy status")
    reasons = _reason_codes(
        strategy.get("reason_codes"),
        "M2 tire-strategy reasons",
    )
    change_tires = strategy.get("change_tires")
    if change_tires is not None and type(change_tires) is not bool:
        _fail("M2_RECEIPT_INVALID", "M2 tire-strategy action is invalid")
    belief_value = strategy.get("belief")
    if belief_value is None:
        if status == "PASS_RULE_MANDATED_TIRE_CHANGE":
            if (
                change_tires is not True
                or reasons
                or rules_binding.get("status")
                != "PASS_VERIFIED_OFFICIAL_EXACT_MATCH"
            ):
                _fail(
                    "M2_RECEIPT_INVALID",
                    "rule-mandated tire strategy is internally inconsistent",
                )
        elif status == "WAIT_TIRE_STRATEGY_PREREQUISITES":
            allowed = {
                "COMMON_ONE_STOP_PLAN_REQUIRED",
                "MATCHED_PIT_CALIBRATION_REQUIRED",
                "MATCHED_SERVICE_LABELS_REQUIRED",
                "MULTI_STOP_RULE_NOT_SUPPORTED",
                "VERIFIED_EVENT_RULES_REQUIRED",
            }
            if change_tires is not None or not reasons or any(
                reason not in allowed for reason in reasons
            ):
                _fail(
                    "M2_RECEIPT_INVALID",
                    "tire-strategy prerequisite WAIT is invalid",
                )
        elif status == "WAIT_TIRE_PERFORMANCE_MODEL":
            if (
                change_tires is not None
                or reasons != ["TIRE_PERFORMANCE_MODEL_REQUIRED"]
                or context.get("_tire_model") is not None
            ):
                _fail(
                    "M2_RECEIPT_INVALID",
                    "missing tire-performance model WAIT is invalid",
                )
        else:
            tire_stint = context.get("_tire_stint")
            if (
                not isinstance(tire_stint, Mapping)
                or tire_stint.get("availability") == "AVAILABLE"
                or status != tire_stint.get("status")
                or reasons != tire_stint.get("reason_codes")
                or change_tires is not None
            ):
                _fail(
                    "M2_RECEIPT_INVALID",
                    "tire-stint WAIT is not context-derived",
                )
        return strategy

    belief = _exact_mapping(
        belief_value,
        set(_M2._TIRE_PERFORMANCE_BELIEF_KEYS),
        "M2 tire-performance belief",
    )
    if rules_binding.get("status") != "PASS_VERIFIED_OFFICIAL_EXACT_MATCH":
        _fail(
            "M2_RECEIPT_INVALID",
            "M2 tire belief lacks verified event rules",
        )
    if (
        belief.get("contract_version")
        != _M2.TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION
        or belief.get("method_version")
        != _M2.TIRE_PERFORMANCE_BELIEF_METHOD_VERSION
        or belief.get("advisor_only") is not True
        or belief.get("physical_wear") != _M2._TIRE_PHYSICAL_WEAR_UNAVAILABLE
    ):
        _fail("M2_RECEIPT_INVALID", "M2 tire belief safety boundary is invalid")
    belief_sha = _sha256(
        belief.get("belief_sha256"),
        "M2 tire-performance belief SHA-256",
    )
    if canonical_sha256(
        {key: item for key, item in belief.items() if key != "belief_sha256"}
    ) != belief_sha:
        _fail("M2_RECEIPT_INVALID", "M2 tire belief self hash mismatch")
    model = context.get("_tire_model")
    calibration = context.get("_calibration")
    tire_stint = context.get("_tire_stint")
    if not all(
        isinstance(item, Mapping) for item in (model, calibration, tire_stint)
    ):
        _fail("M2_RECEIPT_INVALID", "M2 tire belief lacks validated inputs")
    scenario = _exact_mapping(
        belief.get("scenario"),
        set(_M2._TIRE_PERFORMANCE_SCENARIO_KEYS),
        "M2 tire-performance scenario",
    )
    timing = scenario.get("fuel_tire_service_timing")
    if timing not in {"PARALLEL", "SEQUENTIAL"}:
        _fail("M2_RECEIPT_INVALID", "M2 tire service timing is invalid")
    rebuilt = _M2._build_tire_performance_belief(
        model,
        calibration,
        tire_stint,
        branch_plan={
            "fuel_add_l": scenario.get("fuel_add_l"),
            "recommended_lap_from_now": scenario.get("laps_until_pit"),
        },
        horizon_branches=_list(horizon.get("branches"), "M2 horizon branches"),
        fuel_tire_service_timing=str(timing),
    )
    if belief != rebuilt:
        _fail("M2_RECEIPT_INVALID", "M2 tire belief is not reproducible")
    expected = (
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
            "reason_codes": belief.get("reason_codes"),
            "status": belief.get("status"),
        }
    )
    if strategy != expected:
        _fail("M2_RECEIPT_INVALID", "M2 tire strategy is not belief-derived")
    return strategy


def _validate_m2_surface(
    receipt: Mapping[str, object],
    *,
    context: Mapping[str, Any],
    recommendations: Sequence[object],
) -> None:
    context_version = context.get("_context_version")
    is_v2 = context_version == _M2.CONTEXT_V2_CONTRACT_VERSION
    expected_receipt_contract = (
        _M2.CONTRACT_V2_VERSION if is_v2 else _M2.CONTRACT_VERSION
    )
    if receipt.get("contract_version") != expected_receipt_contract:
        _fail("M2_RECEIPT_INVALID", "M2 receipt/context versions disagree")
    identity = _exact_mapping(
        receipt.get("event_identity"),
        _EVENT_IDENTITY_OUTPUT_KEYS,
        "M2 event identity output",
    )
    expected_identity = {
        **_mapping(context.get("_identity"), "validated M2 identity"),
        "identity_sha256": context.get("_identity_sha256"),
    }
    if identity != expected_identity:
        _fail("M2_RECEIPT_INVALID", "M2 event identity is not context-derived")

    policy = _exact_mapping(
        receipt.get("strategy_policy"),
        set(_M2._POLICY_KEYS),
        "M2 strategy policy output",
    )
    vehicle = _exact_mapping(
        receipt.get("vehicle_context"),
        set(_M2._VEHICLE_KEYS),
        "M2 vehicle context output",
    )
    if policy != context.get("_policy") or vehicle != context.get("_vehicle"):
        _fail("M2_RECEIPT_INVALID", "M2 policy/vehicle output is not context-derived")

    calibration = _exact_mapping(
        receipt.get("calibration"),
        _CALIBRATION_OUTPUT_KEYS,
        "M2 calibration output",
    )
    if calibration.get("calibrated_model") != context.get("_calibration"):
        _fail("M2_RECEIPT_INVALID", "M2 calibrated model is not context-derived")
    _validate_observed_calibration(calibration.get("observed_m1"))

    rules = _validate_rules_binding(receipt.get("rules_binding"), identity=identity)
    horizon = _validate_horizon(
        receipt.get("horizon"),
        context_horizon=_mapping(context.get("_horizon"), "validated M2 horizon input"),
        rules_binding=rules,
    )
    tire_strategy = (
        _validate_tire_strategy_surface(
            receipt.get("tire_strategy"),
            context=context,
            horizon=horizon,
            rules_binding=rules,
        )
        if is_v2
        else None
    )
    traffic = _mapping(receipt.get("traffic_rejoin"), "M2 traffic output")
    traffic_keys = set(traffic)
    if traffic_keys not in {_TRAFFIC_OUTPUT_KEYS, _LEGACY_TRAFFIC_OUTPUT_KEYS}:
        _fail("SCHEMA_INVALID", "M2 traffic output keys are invalid")
    extended_traffic_output = traffic_keys == _TRAFFIC_OUTPUT_KEYS
    action_rejoin = traffic.get("estimate")
    if action_rejoin is not None:
        action_rejoin_map = _mapping(action_rejoin, "M2 action-bound rejoin estimate")
        service = _mapping(
            action_rejoin_map.get("service_scenario"),
            "M2 action-bound rejoin service scenario",
        )
        timing = service.get("fuel_tire_service_timing")
        if timing not in {"PARALLEL", "SEQUENTIAL"}:
            _fail("M2_RECEIPT_INVALID", "M2 rejoin service timing is invalid")
        calibration_model = context.get("_calibration")
        context_traffic = context.get("_traffic")
        if not isinstance(calibration_model, Mapping) or not isinstance(
            context_traffic, Mapping
        ):
            _fail("M2_RECEIPT_INVALID", "M2 rejoin estimate lacks source inputs")
        stationary = _finite_number(
            service.get("stationary_service_s"), "M2 rejoin stationary service"
        )
        expected_action = {
            "change_tires": service.get("change_tires"),
            "estimated_stationary_service_s": stationary,
            "estimated_total_pit_loss_s": round(
                float(calibration_model["pit_lane_loss_s"]) + stationary,
                6,
            ),
            "fuel_add_l": service.get("fuel_add_l"),
            "recommended_lap_from_now": _plain_int(
                service.get("recommended_lap_from_now"), "M2 rejoin pit horizon"
            ),
        }
        rebuilt_rejoin = _M2._build_action_bound_rejoin(
            context_traffic,
            calibration_model,
            expected_action,
            fuel_tire_service_timing=str(timing),
        )
        if rebuilt_rejoin != action_rejoin_map:
            _fail("M2_RECEIPT_INVALID", "M2 rejoin estimate is not reproducible")
    action_estimate_available = (
        isinstance(action_rejoin, Mapping)
        and action_rejoin.get("estimate_available") is True
    )
    expected_traffic = {
        "input": context.get("_traffic"),
        "status": (
            "PASS_TRAFFIC_DATA"
            if action_estimate_available
            else "WAIT_REJOIN_ESTIMATE"
            if context.get("_traffic") is not None
            else "WAIT_TRAFFIC_DATA"
        ),
    }
    if extended_traffic_output:
        expected_traffic["estimate"] = action_rejoin
    elif action_rejoin is not None or isinstance(context.get("_traffic"), dict) and context[
        "_traffic"
    ].get("motion_context") is not None:
        _fail("M2_RECEIPT_INVALID", "motion-derived traffic requires extended output")
    if traffic != expected_traffic:
        _fail("M2_RECEIPT_INVALID", "M2 traffic output is not context-derived")

    capability_names = _V2_CAPABILITY_NAMES if is_v2 else _CAPABILITY_NAMES
    base_capability_names = (
        _V2_BASE_CAPABILITY_NAMES if is_v2 else _BASE_CAPABILITY_NAMES
    )
    capabilities = _exact_mapping(
        receipt.get("capabilities"),
        capability_names,
        "M2 capabilities",
    )
    parsed = {name: _capability(capabilities[name], name) for name in capability_names}
    observation = _mapping(context.get("_observation"), "validated M2 observation")
    dynamic_reasons: list[str] = []
    dynamic_status = "PASS_PIT_OPEN_AND_PENALTY_STATE"
    if observation.get("stale") or observation.get("reset") or observation.get("schema_changed"):
        dynamic_reasons.append("STALE_RESET_OR_SCHEMA_CHANGE")
        dynamic_status = "WAIT_PIT_OPEN_AND_PENALTY_STATE"
    if observation.get("pits_open") is not True:
        dynamic_reasons.append(
            "PITS_CLOSED" if observation.get("pits_open") is False else "PITS_OPEN_UNKNOWN"
        )
        dynamic_status = (
            "BLOCKED_PITS_CLOSED"
            if observation.get("pits_open") is False
            else "WAIT_PIT_OPEN_AND_PENALTY_STATE"
        )
    if observation.get("penalty_state") != "CLEAR":
        dynamic_reasons.append(
            "ACTIVE_PENALTY"
            if observation.get("penalty_state") == "ACTIVE"
            else "PENALTY_STATE_UNKNOWN"
        )
        dynamic_status = (
            "BLOCKED_ACTIVE_PENALTY"
            if observation.get("penalty_state") == "ACTIVE"
            else "WAIT_PIT_OPEN_AND_PENALTY_STATE"
        )
    calibration_model = context.get("_calibration")
    expected_capabilities = {
        "event_rules_identity": {
            "reason_codes": rules["reason_codes"],
            "status": rules["status"],
        },
        "one_more_lap": {
            "reason_codes": horizon["reason_codes"],
            "status": horizon["one_more_lap_status"],
        },
        "pit_loss_calibration": (
            {"reason_codes": [], "status": "PASS_CALIBRATED"}
            if calibration_model is not None
            else {
                "reason_codes": ["M1_PIT_ELAPSED_IS_NOT_PIT_LOSS"],
                "status": "WAIT_MATCHED_PIT_LOSS_BASELINE",
            }
        ),
        "pit_open_and_penalty_state": {
            "reason_codes": dynamic_reasons,
            "status": dynamic_status,
        },
        "service_labels": (
            {
                "reason_codes": [],
                "status": "PASS_SERVICE_LABELS",
            }
            if isinstance(calibration_model, dict)
            and calibration_model.get("service_labels_available") is True
            else (
                {
                    "reason_codes": ["CALIBRATION_LACKS_SERVICE_LABELS"],
                    "status": "WAIT_SERVICE_LABELS",
                }
                if calibration_model is not None
                else {
                    "reason_codes": ["M1_SERVICE_CONTENTS_UNOBSERVABLE"],
                    "status": "WAIT_SERVICE_LABELS",
                }
            )
        ),
        "traffic_data": (
            {"reason_codes": [], "status": "PASS_TRAFFIC_DATA"}
            if action_estimate_available
            else {
                "reason_codes": list(action_rejoin["reason_codes"]),
                "status": "WAIT_REJOIN_ESTIMATE",
            }
            if isinstance(action_rejoin, Mapping)
            else {
                "reason_codes": [
                    (
                        "REJOIN_ESTIMATOR_REQUIRED"
                        if calibration_model is not None
                        and context["_traffic"].get("motion_context") is None
                        else "ACTION_BOUND_REJOIN_ESTIMATE_REQUIRED"
                        if calibration_model is not None
                        else "PIT_LOSS_CALIBRATION_REQUIRED_FOR_REJOIN_ESTIMATE"
                    )
                ],
                "status": "WAIT_REJOIN_ESTIMATE",
            }
            if context.get("_traffic") is not None
            else {
                "reason_codes": ["REJOIN_TRAFFIC_INPUT_UNAVAILABLE"],
                "status": "WAIT_TRAFFIC_DATA",
            }
        ),
    }
    if tire_strategy is not None:
        expected_capabilities["tire_strategy"] = {
            "reason_codes": tire_strategy["reason_codes"],
            "status": tire_strategy["status"],
        }
    for name, expected in expected_capabilities.items():
        if parsed[name] != expected:
            _fail("M2_RECEIPT_INVALID", f"M2 capability {name} is not derived")

    strategy = parsed["strategy_data"]
    if not str(horizon["status"]).startswith("PASS_"):
        expected_strategy = {
            "reason_codes": ["DISTANCE_HORIZON_UNAVAILABLE"],
            "status": "WAIT_STRATEGY_DATA",
        }
        if strategy != expected_strategy:
            _fail("M2_RECEIPT_INVALID", "M2 strategy capability ignores horizon WAIT")
    elif strategy.get("status") == "PASS_COMMON_ONE_STOP_PLAN":
        if strategy.get("reason_codes") != []:
            _fail("M2_RECEIPT_INVALID", "passing M2 strategy capability has reasons")
    elif strategy.get("status") == "WAIT_STRATEGY_DATA":
        reasons = strategy.get("reason_codes")
        if (
            type(reasons) is not list
            or len(reasons) != 1
            or reasons[0] not in _STRATEGY_WAIT_REASONS
        ):
            _fail("M2_RECEIPT_INVALID", "M2 strategy WAIT reason is unsupported")
    else:
        _fail("M2_RECEIPT_INVALID", "M2 strategy capability status is invalid")

    required_passes = {
        "event_rules_identity": "PASS_VERIFIED_OFFICIAL_EXACT_MATCH",
        "pit_loss_calibration": "PASS_CALIBRATED",
        "service_labels": "PASS_SERVICE_LABELS",
        "traffic_data": "PASS_TRAFFIC_DATA",
        "pit_open_and_penalty_state": "PASS_PIT_OPEN_AND_PENALTY_STATE",
        "strategy_data": "PASS_COMMON_ONE_STOP_PLAN",
    }
    hard_gates_pass = all(
        parsed[name].get("status") == status for name, status in required_passes.items()
    ) and str(parsed["one_more_lap"].get("status")).startswith(
        ("PASS_", "NOT_APPLICABLE")
    )
    if is_v2:
        hard_gates_pass = hard_gates_pass and parsed["tire_strategy"].get(
            "status"
        ) in {
            "PASS_RULE_MANDATED_TIRE_CHANGE",
            "PASS_MODEL_SELECTED_TIRE_CHANGE",
        }
    if bool(recommendations) != hard_gates_pass:
        _fail("M2_RECEIPT_INVALID", "M2 recommendation presence disagrees with gates")

    wait_statuses = [
        str(parsed[name]["status"])
        for name in sorted(base_capability_names)
        if not str(parsed[name]["status"]).startswith(("PASS_", "NOT_APPLICABLE"))
    ]
    expected_race = {
        "reason_codes": [] if recommendations else wait_statuses,
        "status": "PASS_SHADOW_CONTRACT" if recommendations else "BLOCKED",
    }
    if not recommendations and not wait_statuses:
        expected_race["reason_codes"] = ["NO_ADMISSIBLE_STRATEGY_CANDIDATE"]
    if parsed["race_recommendation"] != expected_race or parsed["lifecycle"] != {
        "reason_codes": [],
        "status": "PASS_OPTIMISTIC_CONCURRENCY",
    }:
        _fail("M2_RECEIPT_INVALID", "M2 terminal capabilities are invalid")

    quality = _exact_mapping(
        receipt.get("quality_gate"),
        _QUALITY_GATE_KEYS,
        "M2 quality gate",
    )
    expected_quality = {
        "reason_codes": [] if recommendations else wait_statuses,
        "status": "PASS_SHADOW_CONTRACT" if recommendations else "WAIT_CAPABILITIES",
    }
    if quality != expected_quality:
        _fail("M2_RECEIPT_INVALID", "M2 quality gate is not capability-derived")


def _validate_recommendation(
    recommendation: object,
    *,
    receipt: Mapping[str, object],
    context: Mapping[str, Any],
    previous_id: str | None,
) -> str:
    is_v2 = receipt.get("contract_version") == _M2.CONTRACT_V2_VERSION
    item = _exact_mapping(recommendation, set(_M2._RECOMMENDATION_KEYS), "M2 recommendation")
    if (
        item.get("kind") != "M2_STRATEGY_CANDIDATE"
        or item.get("status") != "SHADOW_ONLY"
        or item.get("claim_scope") != "GATED_OFFLINE_CONTRACT_CANDIDATE"
        or item.get("confidence") != "LOW"
        or item.get("executable") is not False
        or item.get("practice_only") is not False
        or item.get("expected_gain_range_s") is not None
        or item.get("reason")
        != "Latest common fuel-feasible lap across every admitted horizon branch."
        or item.get("risk")
        != [
            "NOT_M2_ACCEPTED",
            "NOT_LIVE_PROVEN",
            "NOT_R7_ATTESTED",
            "SHADOW_ONLY",
        ]
    ):
        _fail("M2_RECEIPT_INVALID", "M2 recommendation boundary is invalid")
    action = _exact_mapping(item.get("action"), _ACTION_KEYS, "M2 recommendation action")
    if type(action.get("change_tires")) is not bool:
        _fail("M2_RECEIPT_INVALID", "change_tires must be boolean")
    if is_v2:
        tire_strategy = _mapping(
            receipt.get("tire_strategy"),
            "M2 tire strategy",
        )
        if (
            action.get("change_tires") is not True
            or tire_strategy.get("change_tires") is not True
            or tire_strategy.get("status")
            not in {
                "PASS_RULE_MANDATED_TIRE_CHANGE",
                "PASS_MODEL_SELECTED_TIRE_CHANGE",
            }
        ):
            _fail(
                "M2_RECEIPT_INVALID",
                "v2 recommendation lacks an admitted tire-change decision",
            )
    action_numbers: dict[str, float] = {}
    for key in (
        "estimated_stationary_service_s",
        "estimated_total_pit_loss_s",
        "fuel_add_l",
    ):
        action_numbers[key] = _finite_number(action.get(key), f"M2 action {key}")
    laps = _plain_int(action.get("recommended_lap_from_now"), "recommended lap")
    if laps > 100:
        _fail("M2_RECOMMENDATION_UNMAPPABLE", "pit-window lap exceeds speech schema")
    traffic_output = _mapping(receipt.get("traffic_rejoin"), "M2 traffic output")
    action_rejoin = _mapping(traffic_output.get("estimate"), "M2 action-bound rejoin estimate")
    service = _mapping(action_rejoin.get("service_scenario"), "M2 rejoin service scenario")
    expected_service_action = {
        "change_tires": action["change_tires"],
        "fuel_add_l": action_numbers["fuel_add_l"],
        "recommended_lap_from_now": laps,
        "stationary_service_s": action_numbers["estimated_stationary_service_s"],
    }
    if type(service.get("change_tires")) is not bool or any(
        service.get(key) != expected for key, expected in expected_service_action.items()
    ):
        _fail("M2_RECEIPT_INVALID", "M2 rejoin service does not match recommended action")
    timing = service.get("fuel_tire_service_timing")
    if timing not in {"PARALLEL", "SEQUENTIAL"}:
        _fail("M2_RECEIPT_INVALID", "M2 rejoin service timing is invalid")
    if is_v2 and isinstance(tire_strategy.get("belief"), Mapping):
        scenario = _mapping(tire_strategy["belief"].get("scenario"), "M2 tire scenario")
        if timing != scenario.get("fuel_tire_service_timing"):
            _fail("M2_RECEIPT_INVALID", "M2 rejoin and tire service timing disagree")
    calibration = context.get("_calibration")
    if type(calibration) is not dict:
        _fail("M2_RECEIPT_INVALID", "M2 recommendation lacks calibrated service model")
    fuel_time = action_numbers["fuel_add_l"] / float(calibration["refuel_rate_l_per_s"])
    tire_time = (
        float(calibration["tire_change_time_s"]) if action.get("change_tires") is True else 0.0
    )
    expected_stationary = round(
        fuel_time + tire_time if timing == "SEQUENTIAL" else max(fuel_time, tire_time), 6
    )
    if not math.isclose(
        action_numbers["estimated_stationary_service_s"],
        expected_stationary,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        _fail("M2_RECEIPT_INVALID", "M2 stationary service time is not calibrated")
    expected_total = round(
        float(calibration["pit_lane_loss_s"]) + action_numbers["estimated_stationary_service_s"],
        6,
    )
    if not math.isclose(
        action_numbers["estimated_total_pit_loss_s"],
        expected_total,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        _fail("M2_RECEIPT_INVALID", "M2 total pit loss is not calibrated")
    vehicle = _mapping(context.get("_vehicle"), "validated M2 vehicle")
    if action_numbers["fuel_add_l"] > float(vehicle["tank_capacity_l"]) + 1e-9:
        _fail("M2_RECEIPT_INVALID", "M2 fuel addition exceeds tank capacity")
    horizon = _mapping(receipt.get("horizon"), "M2 horizon")
    branches = _list(horizon.get("branches"), "M2 horizon branches")
    if any(
        laps
        > _plain_int(
            _mapping(branch, "M2 horizon branch").get("laps_to_go"),
            "M2 horizon laps_to_go",
        )
        for branch in branches
    ):
        _fail("M2_RECEIPT_INVALID", "M2 pit lap lies outside a horizon branch")

    basis = _exact_mapping(
        item.get("recommendation_basis"),
        _RECOMMENDATION_BASIS_V2_KEYS if is_v2 else _RECOMMENDATION_BASIS_KEYS,
        "M2 recommendation basis",
    )
    if basis.get("action") != action:
        _fail("M2_RECEIPT_INVALID", "M2 basis/action mismatch")
    for key in (
        "calibration_model_sha256",
        "event_identity_sha256",
        "fuel_model_semantic_sha256",
        "horizon_sha256",
        "rejoin_estimate_semantic_sha256",
        "rules_lineage_sha256",
        "strategy_policy_sha256",
        "traffic_semantic_sha256",
    ):
        _sha256(basis.get(key), f"M2 basis {key}")
    if is_v2:
        _sha256(
            basis.get("tire_strategy_semantic_sha256"),
            "M2 basis tire_strategy_semantic_sha256",
        )
    input_binding = _mapping(receipt.get("input_binding"), "M2 input binding")
    event_identity = _mapping(receipt.get("event_identity"), "M2 event identity")
    horizon = _mapping(receipt.get("horizon"), "M2 horizon")
    rules = _mapping(receipt.get("rules_binding"), "M2 rules binding")
    policy = _mapping(receipt.get("strategy_policy"), "M2 strategy policy")
    traffic = context.get("_traffic")
    if type(traffic) is not dict:
        _fail("M2_RECEIPT_INVALID", "candidate requires admitted traffic evidence")
    exact_rejoin_sha256 = (
        action_rejoin.get("estimate_sha256")
        if isinstance(action_rejoin, Mapping)
        else traffic.get("traffic_sha256")
    )
    expected_rejoin_semantic_sha256 = (
        _M2._rejoin_semantic_sha256(action_rejoin)
        if isinstance(action_rejoin, Mapping)
        else _M2._traffic_semantic_sha256(traffic)
    )
    expected_basis = {
        "action": action,
        "calibration_model_sha256": context.get("_calibration_sha256"),
        "event_identity_sha256": event_identity.get("identity_sha256"),
        "fuel_model_semantic_sha256": input_binding.get("model_semantic_sha256"),
        "horizon_sha256": horizon.get("horizon_sha256"),
        "rejoin_estimate_semantic_sha256": expected_rejoin_semantic_sha256,
        "rules_lineage_sha256": rules.get("rules_lineage_sha256"),
        "session_id": input_binding.get("session_id"),
        "source_id": input_binding.get("source_id"),
        "strategy_policy_sha256": canonical_sha256(policy),
        "traffic_semantic_sha256": _M2._traffic_semantic_sha256(traffic),
    }
    if is_v2:
        tire_strategy = _mapping(receipt.get("tire_strategy"), "M2 tire strategy")
        expected_basis["tire_strategy_semantic_sha256"] = (
            _M2._tire_strategy_semantic_sha256(tire_strategy)
        )
    if basis != expected_basis:
        _fail("M2_RECEIPT_INVALID", "M2 recommendation basis is not reproducible")
    recommendation_id = _identifier(item.get("recommendation_id"), "recommendation id")
    if recommendation_id != f"m2-strategy:{canonical_sha256(basis)}":
        _fail("M2_RECEIPT_INVALID", "M2 recommendation id is not semantic")
    expected_supersedes = previous_id if previous_id != recommendation_id else None
    if item.get("supersedes_id") != expected_supersedes:
        _fail("M2_RECEIPT_INVALID", "M2 recommendation supersedes id is invalid")
    expected_evidence = [
        f"fuel-replay:{input_binding['fuel_replay_sha256']}",
        f"m1-pit-stint:{input_binding['m1_receipt_sha256']}",
        f"strategy-context:{input_binding['context_sha256']}",
        f"rules-profile:{rules.get('profile_sha256')}",
        f"rejoin-estimate:{exact_rejoin_sha256}",
    ]
    if is_v2:
        tire_strategy = _mapping(receipt.get("tire_strategy"), "M2 tire strategy")
        expected_evidence.append(
            f"tire-strategy:{canonical_sha256(tire_strategy)}"
        )
    if item.get("evidence_ids") != expected_evidence:
        _fail("M2_RECEIPT_INVALID", "M2 recommendation evidence ids are invalid")
    observation = _mapping(
        _mapping(receipt.get("strategy_context"), "M2 strategy context").get("observation"),
        "M2 observation",
    )
    valid_until = _exact_mapping(item.get("valid_until"), _VALID_UNTIL_KEYS, "M2 valid_until")
    expected_valid_until = {
        "context_sha256": input_binding["context_sha256"],
        "pit_entry_deadline_laps_completed": _plain_int(
            observation.get("laps_completed"), "M2 laps_completed"
        )
        + laps,
        "recompute_after_decision_tick": _plain_int(
            observation.get("decision_tick"), "M2 decision_tick"
        )
        + 1,
        "rules_lineage_sha256": rules.get("rules_lineage_sha256"),
        "session_epoch": observation.get("session_epoch"),
        "source_epoch": observation.get("source_epoch"),
    }
    if valid_until != expected_valid_until:
        _fail("M2_RECEIPT_INVALID", "M2 recommendation validity is invalid")
    return recommendation_id


def _validate_lifecycle_events(
    lifecycle: Mapping[str, object],
    *,
    previous_id: str | None,
    current_id: str | None,
) -> None:
    events = list(_list(lifecycle.get("events"), "M2 lifecycle events"))
    if previous_id is not None and previous_id != current_id:
        if not events:
            _fail("M2_LIFECYCLE_INVALID", "M2 revocation event is missing")
        revoke = _exact_mapping(
            events.pop(0),
            frozenset({"event", "reason_codes", "recommendation_id"}),
            "M2 REVOKE event",
        )
        reasons = _list(revoke.get("reason_codes"), "M2 revoke reasons")
        if (
            revoke.get("event") != "REVOKE"
            or revoke.get("recommendation_id") != previous_id
            or not reasons
            or any(type(item) is not str or not item for item in reasons)
            or len(reasons) != len(set(reasons))
        ):
            _fail("M2_LIFECYCLE_INVALID", "M2 REVOKE payload is invalid")
    if current_id is not None and current_id != previous_id:
        if len(events) != 1:
            _fail("M2_LIFECYCLE_INVALID", "M2 ISSUE event is missing")
        issue = _exact_mapping(
            events[0],
            frozenset({"event", "recommendation_id", "supersedes_id"}),
            "M2 ISSUE event",
        )
        if issue != {
            "event": "ISSUE",
            "recommendation_id": current_id,
            "supersedes_id": previous_id,
        }:
            _fail("M2_LIFECYCLE_INVALID", "M2 ISSUE payload is invalid")
    elif current_id is not None:
        if len(events) != 1:
            _fail("M2_LIFECYCLE_INVALID", "M2 NO_CHANGE event is missing")
        unchanged = _exact_mapping(
            events[0],
            frozenset({"event", "recommendation_id", "reason_codes"}),
            "M2 NO_CHANGE event",
        )
        if unchanged != {
            "event": "NO_CHANGE",
            "recommendation_id": current_id,
            "reason_codes": ["ACTIVE_STRATEGY_UNCHANGED"],
        }:
            _fail("M2_LIFECYCLE_INVALID", "active NO_CHANGE payload is invalid")
    elif previous_id is None:
        if len(events) != 1:
            _fail("M2_LIFECYCLE_INVALID", "inactive NO_CHANGE event is missing")
        unchanged = _exact_mapping(
            events[0],
            frozenset({"event", "recommendation_id", "reason_codes"}),
            "M2 NO_CHANGE event",
        )
        if unchanged != {
            "event": "NO_CHANGE",
            "recommendation_id": None,
            "reason_codes": ["NO_ACTIVE_RECOMMENDATION"],
        }:
            _fail("M2_LIFECYCLE_INVALID", "inactive NO_CHANGE payload is invalid")
    elif events:
        _fail("M2_LIFECYCLE_INVALID", "revocation has unexpected extra events")


def _validate_m2_receipts(
    values: Sequence[Mapping[str, object]],
    expected_digests: Sequence[str],
    *,
    source_binding: Mapping[str, object],
    source_epoch: int,
    session_epoch: int,
) -> tuple[dict[str, object], ...]:
    validated: list[dict[str, object]] = []
    previous: dict[str, object] | None = None
    previous_id: str | None = None
    for index, (value, expected) in enumerate(zip(values, expected_digests, strict=True)):
        raw_receipt = _mapping(value, f"M2 receipt[{index}]")
        receipt_contract = raw_receipt.get("contract_version")
        if receipt_contract == _M2.CONTRACT_VERSION:
            output_keys = set(_M2._OUTPUT_KEYS)
        elif receipt_contract == _M2.CONTRACT_V2_VERSION:
            output_keys = set(_M2._OUTPUT_V2_KEYS)
        else:
            _fail("M2_RECEIPT_INVALID", "M2 receipt contract is unsupported")
        receipt = _exact_mapping(raw_receipt, output_keys, f"M2 receipt[{index}]")
        stored = _sha256(receipt.get("m2_strategy_receipt_sha256"), "M2 self SHA-256")
        if stored != expected:
            _fail("M2_RECEIPT_INVALID", "M2 receipt failed independent digest binding")
        lifecycle = _exact_mapping(
            receipt.get("lifecycle"), set(_M2._LIFECYCLE_KEYS), "M2 lifecycle"
        )
        revision = _plain_int(lifecycle.get("state_revision"), "M2 revision", minimum=1)
        point = _exact_mapping(
            lifecycle.get("observation_point"),
            set(_M2._OBSERVATION_POINT_KEYS),
            "M2 observation point",
        )
        sentinel = {
            "decision_tick": _plain_int(point.get("decision_tick"), "M2 decision tick") + 1,
            "session_epoch": session_epoch,
            "source_epoch": source_epoch,
        }
        try:
            _M2._validate_previous(
                receipt,
                expected_previous_receipt_sha256=expected,
                expected_previous_revision=revision,
                source_binding=source_binding,
                observation=sentinel,
            )
            input_binding = _M2._exact_mapping(
                receipt.get("input_binding"),
                _M2._INPUT_BINDING_KEYS,
                "M2 input binding",
            )
            context = _M2._validate_context(
                receipt.get("strategy_context"),
                expected_strategy_context_sha256=input_binding["context_sha256"],
                expected_source_binding=source_binding,
            )
        except _M2.M2StrategyReceiptError as exc:
            raise AdvisorTimelineError("M2_RECEIPT_INVALID", str(exc)) from exc
        observation = _mapping(context.get("_observation"), "validated M2 observation")
        if (
            observation.get("source_epoch") != source_epoch
            or observation.get("session_epoch") != session_epoch
            or point
            != {
                "decision_tick": observation.get("decision_tick"),
                "session_epoch": session_epoch,
                "source_epoch": source_epoch,
            }
        ):
            _fail("M2_LIFECYCLE_INVALID", "M2 epoch/observation point mismatch")
        if any(input_binding.get(key) != source_binding.get(key) for key in _SOURCE_BINDING_KEYS):
            _fail("CROSS_RECEIPT_LINEAGE_MISMATCH", "M2 does not match active run")
        if previous is None:
            if revision != 1 or lifecycle.get("previous_state_sha256") is not None:
                _fail("M2_LIFECYCLE_INVALID", "first M2 receipt must start revision 1")
        else:
            previous_lifecycle = _mapping(previous.get("lifecycle"), "previous lifecycle")
            if revision != _plain_int(
                previous_lifecycle.get("state_revision"), "previous revision"
            ) + 1 or lifecycle.get("previous_state_sha256") != previous.get(
                "m2_strategy_receipt_sha256"
            ):
                _fail("M2_LIFECYCLE_INVALID", "M2 CAS chain is not contiguous")
            previous_input = _mapping(previous.get("input_binding"), "previous input binding")
            if any(input_binding.get(key) != previous_input.get(key) for key in _STABLE_M2_KEYS):
                _fail("CROSS_RECEIPT_LINEAGE_MISMATCH", "M2 source lineage changed")
            previous_point = _mapping(
                previous_lifecycle.get("observation_point"),
                "previous observation point",
            )
            if int(point["decision_tick"]) <= _plain_int(
                previous_point.get("decision_tick"), "previous decision tick"
            ):
                _fail("M2_LIFECYCLE_INVALID", "M2 decision tick is not monotonic")

        recommendations = _list(receipt.get("recommendations"), "M2 recommendations")
        if len(recommendations) > 1:
            _fail("M2_RECEIPT_INVALID", "M2 supports at most one candidate")
        _validate_m2_surface(
            receipt,
            context=context,
            recommendations=recommendations,
        )
        current_id = (
            _validate_recommendation(
                recommendations[0],
                receipt=receipt,
                context=context,
                previous_id=previous_id,
            )
            if recommendations
            else None
        )
        if lifecycle.get("active_recommendation_id") != current_id:
            _fail("M2_LIFECYCLE_INVALID", "M2 active recommendation id mismatch")
        _validate_lifecycle_events(lifecycle, previous_id=previous_id, current_id=current_id)
        validated.append(receipt)
        previous = receipt
        previous_id = current_id
    return tuple(validated)


def _validate_diagnosis_receipt(
    value: object,
    *,
    expected_digest: str,
    source_binding: Mapping[str, object],
    clock_receipt: Mapping[str, object],
    expected_serialized_sha256: str,
) -> tuple[dict[str, object], bool]:
    receipt = _exact_mapping(value, _DIAGNOSIS_KEYS, "diagnosis receipt")
    stored = _sha256(receipt.get("diagnosis_evidence_sha256"), "diagnosis self SHA-256")
    if stored != expected_digest:
        _fail(
            "DIAGNOSIS_RECEIPT_INVALID",
            "diagnosis failed independent digest binding",
        )
    material = {key: item for key, item in receipt.items() if key != "diagnosis_evidence_sha256"}
    if canonical_sha256(material) != stored:
        _fail("DIAGNOSIS_RECEIPT_INVALID", "diagnosis self hash mismatch")
    if (
        receipt.get("contract_version") != DIAGNOSIS_EVIDENCE_CONTRACT_VERSION
        or receipt.get("advisor_only") is not True
        or receipt.get("claim_scope") != "DERIVED_RULE_AUDIT"
        or receipt.get("execution_status") != "COMPLETE"
        or receipt.get("status") != "SHADOW_ONLY"
        or receipt.get("executable") is not False
        or receipt.get("raw_telemetry_replayed") is not False
        or receipt.get("recommendations") != []
    ):
        _fail("DIAGNOSIS_RECEIPT_INVALID", "diagnosis safety boundary is invalid")
    input_receipt = _exact_mapping(
        receipt.get("input_receipt"),
        _DIAGNOSIS_INPUT_KEYS,
        "diagnosis input receipt",
    )
    for key in _DIAGNOSIS_INPUT_KEYS - {"source_identity", "track_length_mm"}:
        _sha256(input_receipt.get(key), f"diagnosis input {key}")
    if input_receipt.get("driving_replay_serialized_sha256") != expected_serialized_sha256:
        _fail("DIAGNOSIS_RECEIPT_INVALID", "diagnosis serialized replay mismatch")
    _plain_int(
        input_receipt.get("track_length_mm"),
        "diagnosis track length",
        minimum=1,
    )
    source = _exact_mapping(
        input_receipt.get("source_identity"),
        _DIAGNOSIS_SOURCE_KEYS,
        "diagnosis source",
    )
    expected_source = {
        "authenticity_status": clock_receipt.get("authenticity_status"),
        "completion_status": clock_receipt.get("completion_status"),
        "input_evidence_sha256": clock_receipt.get("input_evidence_sha256"),
        "input_kind": clock_receipt.get("input_kind"),
        "session_id": source_binding.get("session_id"),
        "source_content_sha256": source_binding.get("source_sha256"),
        "source_content_sha256_field": clock_receipt.get("source_content_sha256_field"),
        "source_id": source_binding.get("source_id"),
        "source_kind": source_binding.get("source_kind"),
    }
    if source != expected_source:
        _fail("CROSS_RECEIPT_LINEAGE_MISMATCH", "diagnosis source differs from run")
    if input_receipt.get("normalized_samples_sha256") != source_binding.get(
        "normalized_samples_sha256"
    ):
        _fail(
            "CROSS_RECEIPT_LINEAGE_MISMATCH",
            "diagnosis normalized stream differs",
        )
    gates = _exact_mapping(
        receipt.get("promotion_gates"),
        set(_PROMOTION_GATES),
        "diagnosis promotion gates",
    )
    if gates != dict(_PROMOTION_GATES):
        _fail("DIAGNOSIS_RECEIPT_INVALID", "diagnosis promotion gates were promoted")
    diagnosis_ready = all(
        type(value) is str and not value.startswith("WAIT_") for value in gates.values()
    )
    return receipt, diagnosis_ready


def _rebuild_diagnosis(
    serialized_driving_replay: bytes,
    *,
    expected_serialized_sha256: str,
) -> tuple[dict[str, object], str]:
    if type(serialized_driving_replay) is not bytes or not serialized_driving_replay:
        _fail("SCHEMA_INVALID", "serialized driving replay must be non-empty bytes")
    expected = _sha256(
        expected_serialized_sha256,
        "expected driving replay serialized SHA-256",
    )
    if hashlib.sha256(serialized_driving_replay).hexdigest() != expected:
        _fail(
            "DIAGNOSIS_RECEIPT_INVALID",
            "driving replay failed independent digest binding",
        )
    try:
        diagnosis = build_diagnosis_evidence(serialized_driving_replay)
    except DiagnosisEvidenceError as exc:
        raise AdvisorTimelineError("DIAGNOSIS_RECEIPT_INVALID", str(exc)) from exc
    return diagnosis, expected


def _desired_envelope(
    receipt: Mapping[str, object],
    *,
    binding: PolicyBinding,
    session_time_us: int,
    lease_duration_us: int,
    supersedes: str | None,
    tactical_allowed: bool,
) -> SpeechEnvelope:
    recommendations = _list(receipt.get("recommendations"), "M2 recommendations")
    if not tactical_allowed or not recommendations:
        message_class = MessageClass.OVERLAY_INFO
        scalar_params = (("state", "WAIT_DATA"),)
    else:
        recommendation = _mapping(recommendations[0], "M2 recommendation")
        action = _mapping(recommendation.get("action"), "M2 recommendation action")
        laps = _plain_int(action.get("recommended_lap_from_now"), "recommended lap")
        if laps == 0:
            message_class = MessageClass.BOX_THIS_LAP
            scalar_params = ()
        else:
            message_class = MessageClass.WINDOW_OPENING_SOON
            scalar_params = (("laps", laps),)
    return SpeechEnvelope(
        binding=binding,
        message_class=message_class,
        template_id=MESSAGE_TEMPLATE_ID[message_class],
        scalar_params=scalar_params,
        conflict_key="m2_strategy",
        evidence_sha256=_sha256(receipt.get("m2_strategy_receipt_sha256"), "M2 self SHA-256"),
        issued_session_time_us=session_time_us,
        valid_until_session_time_us=session_time_us + lease_duration_us,
        supersedes_content_revision_sha256=supersedes,
        executable=False,
    )


def _build_from_admitted(
    receipts: Sequence[Mapping[str, object]],
    clocks: Sequence[DecisionClockBinding],
    clock_receipt: Mapping[str, object],
    diagnosis: Mapping[str, object],
    *,
    diagnosis_ready: bool,
    config: SpeechPolicyConfig,
    lease_duration_us: int,
) -> dict[str, object]:
    source = _mapping(clock_receipt.get("source_binding"), "clock source binding")
    policy_binding = PolicyBinding(
        source_id=_identifier(source.get("source_id"), "source_id"),
        session_id=_identifier(source.get("session_id"), "session_id"),
        source_epoch=_plain_int(clock_receipt.get("source_epoch"), "source_epoch"),
        session_epoch=_plain_int(clock_receipt.get("session_epoch"), "session_epoch"),
    )
    policy_inputs: list[SpeechEnvelope | SpeechRefresh] = []
    active: SpeechEnvelope | None = None
    tactical_observations = 0
    overlay_observations = 0
    upstream_candidates = 0
    for receipt, clock in zip(receipts, clocks, strict=True):
        has_candidate = bool(_list(receipt.get("recommendations"), "M2 recommendations"))
        upstream_candidates += int(has_candidate)
        tactical = has_candidate
        tactical_observations += int(tactical)
        overlay_observations += int(not tactical)
        desired = _desired_envelope(
            receipt,
            binding=policy_binding,
            session_time_us=clock.session_time_us,
            lease_duration_us=lease_duration_us,
            supersedes=(active.content_revision_sha256 if active is not None else None),
            tactical_allowed=True,
        )
        if active is not None and desired.content_revision_sha256 == active.content_revision_sha256:
            refresh = SpeechRefresh(
                binding=policy_binding,
                conflict_key=active.conflict_key,
                expected_content_revision_sha256=active.content_revision_sha256,
                previous_envelope_sha256=active.envelope_sha256,
                evidence_sha256=desired.evidence_sha256,
                session_time_us=clock.session_time_us,
                valid_until_session_time_us=desired.valid_until_session_time_us,
                executable=False,
            )
            policy_inputs.append(refresh)
            active = replace(
                active,
                evidence_sha256=refresh.evidence_sha256,
                valid_until_session_time_us=refresh.valid_until_session_time_us,
            )
        else:
            policy_inputs.append(desired)
            active = desired
        if config.muted and active.priority is not Priority.P3:
            active = None
    try:
        run = process_speech_policy_run(policy_binding, policy_inputs, config=config)
        replayed = replay_speech_policy(policy_binding, run.input_records, config=config)
    except SpeechPolicyError as exc:
        raise AdvisorTimelineError("SPEECH_POLICY_REJECTED", str(exc)) from exc
    if replayed.to_dict() != run.to_dict():
        _fail("SPEECH_REPLAY_MISMATCH", "persisted speech inputs do not replay exactly")
    run_value = run.to_dict()
    if any(
        item.get("audible") is not False or item.get("executable") is not False
        for section in ("decisions", "events")
        for item in _list(run_value.get(section), f"speech {section}")
        if type(item) is dict
    ):
        _fail("SPEECH_SAFETY_INVALID", "speech output crossed the shadow boundary")
    active_snapshots = _list(run_value.get("final_active_envelopes"), "final active envelopes")
    final_active_tactical = 0
    for snapshot in active_snapshots:
        envelope = _mapping(
            _mapping(snapshot, "active envelope snapshot").get("envelope"),
            "active envelope",
        )
        final_active_tactical += int(envelope.get("priority") != Priority.P3.value)
    latest_has_candidate = bool(
        _list(receipts[-1].get("recommendations"), "latest M2 recommendations")
    )
    if not latest_has_candidate:
        status = "WAIT_DATA"
    elif config.muted:
        status = "SHADOW_CANDIDATE_MUTED"
    elif final_active_tactical:
        status = "SHADOW_ACTIVE_CANDIDATE"
    else:
        status = "SHADOW_CANDIDATE_SUPPRESSED"
    diagnosis_sha = _sha256(
        diagnosis.get("diagnosis_evidence_sha256"), "diagnosis evidence SHA-256"
    )
    serialized_sha = _sha256(
        _mapping(diagnosis.get("input_receipt"), "diagnosis input receipt").get(
            "driving_replay_serialized_sha256"
        ),
        "driving replay serialized SHA-256",
    )
    base: dict[str, object] = {
        "advisor_only": True,
        "bridge_policy": {
            "diagnosis_promotion_required": False,
            "driving_diagnosis_ready": diagnosis_ready,
            "lease_duration_us": lease_duration_us,
            "missing_strategy_state": "WAIT_DATA",
            "tactical_timing_evidence_synthesized": False,
        },
        "clock_receipt": dict(clock_receipt),
        "contract_version": ADVISOR_TIMELINE_CONTRACT_VERSION,
        "diagnosis_evidence_sha256": diagnosis_sha,
        "execution_mode": "SHADOW_ONLY",
        "input_binding": {
            "diagnosis_evidence_sha256": diagnosis_sha,
            "driving_replay_serialized_sha256": serialized_sha,
            "event_receipt_sha256": source["event_receipt_sha256"],
            "m2_receipt_sha256": [item["m2_strategy_receipt_sha256"] for item in receipts],
            "normalized_samples_sha256": source["normalized_samples_sha256"],
            "sample_count": source["sample_count"],
            "session_id": source["session_id"],
            "source_id": source["source_id"],
            "source_kind": source["source_kind"],
            "source_sha256": source["source_sha256"],
        },
        "safety": {
            "audible": False,
            "executable": False,
            "renderer_present": False,
            "vehicle_control_enabled": False,
        },
        "speech_policy_run": run_value,
        "status": status,
        "summary": {
            "decision_count": len(run.decisions),
            "final_active_tactical_count": final_active_tactical,
            "lifecycle_event_count": len(run.events),
            "m2_observation_count": len(receipts),
            "overlay_observation_count": overlay_observations,
            "tactical_observation_count": tactical_observations,
            "upstream_tactical_candidate_count": upstream_candidates,
        },
    }
    return {**base, "advisor_timeline_sha256": canonical_sha256(base)}


def build_advisor_timeline(
    run: ValidatedIbtRun | ValidatedCollectorRun,
    m2_receipts: Sequence[Mapping[str, object]],
    serialized_driving_replay: bytes,
    *,
    expected_m2_receipt_sha256s: Sequence[str],
    expected_driving_replay_serialized_sha256: str,
    config: SpeechPolicyConfig | None = None,
    lease_duration_us: int = DEFAULT_LEASE_DURATION_US,
) -> dict[str, object]:
    """Build a deterministic timeline from one active, inseparable source run."""

    if not isinstance(m2_receipts, Sequence) or isinstance(m2_receipts, (str, bytes)):
        _fail("SCHEMA_INVALID", "M2 receipts must be a sequence")
    if not m2_receipts:
        _fail("SCHEMA_INVALID", "at least one M2 receipt is required")
    expected_m2 = _expected_digests(
        expected_m2_receipt_sha256s,
        count=len(m2_receipts),
        name="expected M2 receipt digests",
    )
    for index, (receipt, expected) in enumerate(zip(m2_receipts, expected_m2, strict=True)):
        stored = _sha256(
            _mapping(receipt, f"M2 receipt[{index}]").get("m2_strategy_receipt_sha256"),
            f"M2 receipt[{index}] SHA-256",
        )
        if stored != expected:
            _fail(
                "M2_RECEIPT_INVALID",
                "M2 receipt failed independent digest binding",
            )
    diagnosis, expected_driving = _rebuild_diagnosis(
        serialized_driving_replay,
        expected_serialized_sha256=expected_driving_replay_serialized_sha256,
    )
    lease = _plain_int(lease_duration_us, "lease_duration_us", minimum=1)
    selected_config = config or SpeechPolicyConfig()
    if not isinstance(selected_config, SpeechPolicyConfig):
        _fail("SCHEMA_INVALID", "config must be SpeechPolicyConfig")

    clock_receipt = _clock_from_active_run(run, m2_receipts)
    _, source_binding, clocks = _validate_clock_receipt(clock_receipt, m2_receipts)
    receipts = _validate_m2_receipts(
        m2_receipts,
        expected_m2,
        source_binding=source_binding,
        source_epoch=int(clock_receipt["source_epoch"]),
        session_epoch=int(clock_receipt["session_epoch"]),
    )
    diagnosis_sha = _sha256(
        diagnosis.get("diagnosis_evidence_sha256"), "diagnosis evidence SHA-256"
    )
    diagnosis, diagnosis_ready = _validate_diagnosis_receipt(
        diagnosis,
        expected_digest=diagnosis_sha,
        source_binding=source_binding,
        clock_receipt=clock_receipt,
        expected_serialized_sha256=expected_driving,
    )
    return _build_from_admitted(
        receipts,
        clocks,
        clock_receipt,
        diagnosis,
        diagnosis_ready=diagnosis_ready,
        config=selected_config,
        lease_duration_us=lease,
    )


def validate_advisor_timeline(
    value: object,
    m2_receipts: Sequence[Mapping[str, object]],
    serialized_driving_replay: bytes,
    *,
    expected_m2_receipt_sha256s: Sequence[str],
    expected_driving_replay_serialized_sha256: str,
    expected_clock_receipt_sha256: str,
) -> dict[str, object]:
    """Rebuild and validate a persisted timeline without reopening the source.

    This validates deterministic closure from independently pinned M2 receipts
    and serialized driving-replay bytes. Source authenticity still belongs to
    the active-run build boundary.
    """

    timeline = _exact_mapping(value, _TIMELINE_KEYS, "advisor timeline")
    if timeline.get("contract_version") != ADVISOR_TIMELINE_CONTRACT_VERSION:
        _fail("TIMELINE_INVALID", "unsupported advisor timeline contract")
    stored = _sha256(timeline.get("advisor_timeline_sha256"), "timeline self SHA-256")
    material = {key: item for key, item in timeline.items() if key != "advisor_timeline_sha256"}
    if canonical_sha256(material) != stored:
        _fail("TIMELINE_INVALID", "advisor timeline self hash mismatch")
    if not isinstance(m2_receipts, Sequence) or isinstance(m2_receipts, (str, bytes)):
        _fail("SCHEMA_INVALID", "M2 receipts must be a sequence")
    if not m2_receipts:
        _fail("SCHEMA_INVALID", "at least one M2 receipt is required")
    expected_m2 = _expected_digests(
        expected_m2_receipt_sha256s,
        count=len(m2_receipts),
        name="expected M2 receipt digests",
    )
    diagnosis, serialized_sha = _rebuild_diagnosis(
        serialized_driving_replay,
        expected_serialized_sha256=expected_driving_replay_serialized_sha256,
    )
    expected_diagnosis = _sha256(
        diagnosis.get("diagnosis_evidence_sha256"),
        "rebuilt diagnosis evidence SHA-256",
    )
    clock_receipt, source_binding, clocks = _validate_clock_receipt(
        timeline.get("clock_receipt"),
        m2_receipts,
        expected_digest=expected_clock_receipt_sha256,
    )
    receipts = _validate_m2_receipts(
        m2_receipts,
        expected_m2,
        source_binding=source_binding,
        source_epoch=int(clock_receipt["source_epoch"]),
        session_epoch=int(clock_receipt["session_epoch"]),
    )
    diagnosis, diagnosis_ready = _validate_diagnosis_receipt(
        diagnosis,
        expected_digest=expected_diagnosis,
        source_binding=source_binding,
        clock_receipt=clock_receipt,
        expected_serialized_sha256=serialized_sha,
    )
    speech_run = _mapping(timeline.get("speech_policy_run"), "speech policy run")
    config_value = _exact_mapping(
        speech_run.get("config"),
        frozenset(
            {
                "global_cooldown_us",
                "max_timing_gap_us",
                "muted",
                "per_conflict_cooldown_us",
                "stable_consecutive_samples",
                "stable_duration_us",
            }
        ),
        "speech policy config",
    )
    try:
        config = SpeechPolicyConfig(**config_value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise AdvisorTimelineError("TIMELINE_INVALID", str(exc)) from exc
    bridge = _mapping(timeline.get("bridge_policy"), "bridge policy")
    lease = _plain_int(bridge.get("lease_duration_us"), "lease_duration_us", minimum=1)
    rebuilt = _build_from_admitted(
        receipts,
        clocks,
        clock_receipt,
        diagnosis,
        diagnosis_ready=diagnosis_ready,
        config=config,
        lease_duration_us=lease,
    )
    if rebuilt != timeline:
        _fail("TIMELINE_INVALID", "persisted timeline does not reproduce exactly")
    return rebuilt


__all__ = [
    "ADVISOR_CLOCK_RECEIPT_CONTRACT_VERSION",
    "ADVISOR_TIMELINE_CONTRACT_VERSION",
    "AdvisorTimelineError",
    "DecisionClockBinding",
    "build_advisor_timeline",
    "canonical_sha256",
    "validate_advisor_timeline",
]
