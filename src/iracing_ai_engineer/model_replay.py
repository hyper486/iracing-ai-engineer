"""Source-neutral replay of normalized telemetry through the fuel model.

Both IBT and live-collector adapters emit :class:`TelemetrySample`.  This
module is intentionally unaware of either file format: it consumes that shared
contract once, feeds the streaming event state machine, builds conservative lap
observations, and runs the same deterministic fuel strategy model.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from .adapters import (
    CollectorInputEvidence,
    IbtInputEvidence,
    ValidatedCollectorRun,
    ValidatedIbtRun,
    _validated_run_state,
)
from .capabilities import unavailable_inference_capability
from .contracts import (
    FUEL_MODEL_VERSION,
    LAP_ALGORITHM_VERSION,
    NORMALIZATION_PROFILE_VERSION,
)
from .events import EVENT_CONTRACT_VERSION, TelemetryEventPipeline
from .fuel import (
    FuelScenario,
    build_fuel_shadow_recommendation,
    estimate_fuel_strategy,
)
from .laps import LapObservation, segment_laps
from .telemetry import (
    TELEMETRY_CONTRACT_VERSION,
    Presence,
    QualityStatus,
    SourceKind,
    TelemetryField,
    TelemetrySample,
)

FUEL_MODEL_REPLAY_CONTRACT_VERSION = "fuel-model-replay-v2"
FUEL_FEATURE_PIPELINE_VERSION = "normalized-lap-fuel-v1"

_REQUIRED_CHANNELS = (
    "SessionTime",
    "SessionTick",
    "Lap",
    "LapDistPct",
    "FuelLevel",
    "OnPitRoad",
    "PlayerCarInPitStall",
)
_OPTIONAL_CHANNELS = ("LapCompleted", "Speed", "PlayerTrackSurface")
_ALL_CHANNELS = _REQUIRED_CHANNELS + _OPTIONAL_CHANNELS
_HEX_DIGITS = frozenset("0123456789abcdef")
_COMMON_EVIDENCE_FIELDS = frozenset(
    {
        "authenticity_status",
        "completion_status",
        "session_id",
        "source_id",
        "source_kind",
    }
)
_IBT_EVIDENCE_FIELDS = _COMMON_EVIDENCE_FIELDS | {
    "byte_size",
    "record_count",
    "source_sha256",
    "tick_rate_hz",
}
_COLLECTOR_QUALITY_COUNT_FIELDS = (
    "capture_clock_regression_count",
    "driver_info_key_count",
    "dropped_tick_count",
    "duplicate_conflict_count",
    "read_error_frame_count",
    "schema_change_count",
    "session_reset_count",
    "stale_event_count",
)
_COLLECTOR_EVIDENCE_FIELDS = _COMMON_EVIDENCE_FIELDS | {
    "capture_span_us",
    "collector_contract_version",
    "duplicate_sample_count",
    "event_record_count",
    "first_buffer_tick",
    "first_capture_monotonic_us",
    "frame_record_count",
    "last_buffer_tick",
    "last_capture_monotonic_us",
    "read_error_field_count",
    "records_sha256",
    "redacted_driver_info_path_count",
    "samples_seen",
    "schema_epoch_count",
    "schema_record_count",
    "semantic_record_count",
    "session_epoch_count",
    "session_info_record_count",
    "session_info_scope_counts",
    "sim_mode",
    "tick_rate_hz_values",
    *_COLLECTOR_QUALITY_COUNT_FIELDS,
}


class FuelModelReplayError(ValueError):
    """Raised when the shared model replay contract itself is invalid."""


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
        raise FuelModelReplayError("fuel model replay value is not canonical-JSON-safe") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _plain_count(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise FuelModelReplayError(f"{name} must be a plain integer >= {minimum}")
    return value


def _identifier(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise FuelModelReplayError(f"{name} is not a valid bound identifier")
    return value


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise FuelModelReplayError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _validate_input_evidence(
    input_evidence: IbtInputEvidence | CollectorInputEvidence,
    *,
    input_kind: str,
    tick_rate_hz: int,
) -> tuple[dict[str, object], str, str, SourceKind, int]:
    expected_type = IbtInputEvidence if input_kind == "ibt" else CollectorInputEvidence
    if type(input_evidence) is not expected_type:
        raise FuelModelReplayError(
            f"{input_kind} replay requires validated {expected_type.__name__}"
        )
    evidence = input_evidence.to_dict()
    required = (
        _IBT_EVIDENCE_FIELDS
        if input_kind == "ibt"
        else _COLLECTOR_EVIDENCE_FIELDS
    )
    unexpected = sorted(set(evidence) - required)
    missing = sorted(required - set(evidence))
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise FuelModelReplayError(
            "input_evidence fields are invalid: " + "; ".join(details)
        )
    if evidence["completion_status"] != "COMPLETE":
        raise FuelModelReplayError("fuel model replay requires COMPLETE input evidence")
    source_id = _identifier(evidence["source_id"], "input_evidence.source_id")
    session_id = _identifier(
        evidence["session_id"], "input_evidence.session_id"
    )
    try:
        source_kind = SourceKind(evidence["source_kind"])
    except (TypeError, ValueError) as exc:
        raise FuelModelReplayError("input_evidence.source_kind is invalid") from exc

    if input_kind == "ibt":
        if source_kind is not SourceKind.IBT_OFFLINE:
            raise FuelModelReplayError("IBT evidence must declare IBT_OFFLINE source_kind")
        if evidence["authenticity_status"] != (
            "HASHED_LOCAL_FILE_NOT_AUTHENTICATED"
        ):
            raise FuelModelReplayError("IBT authenticity_status is invalid")
        _plain_count(evidence["byte_size"], "input_evidence.byte_size", minimum=1)
        expected_count = _plain_count(
            evidence["record_count"],
            "input_evidence.record_count",
            minimum=1,
        )
        evidence_tick_rate = _plain_count(
            evidence["tick_rate_hz"],
            "input_evidence.tick_rate_hz",
            minimum=1,
        )
        if evidence_tick_rate != tick_rate_hz:
            raise FuelModelReplayError("IBT evidence tick rate does not match pipeline")
        _sha256(evidence["source_sha256"], "input_evidence.source_sha256")
    else:
        if source_kind not in {SourceKind.SDK_LIVE, SourceKind.REPLAY_SDK_PROXY}:
            raise FuelModelReplayError(
                "collector evidence must declare SDK_LIVE or REPLAY_SDK_PROXY"
            )
        if evidence["authenticity_status"] != (
            "SELF_CONSISTENT_NOT_AUTHENTICATED"
        ):
            raise FuelModelReplayError("collector authenticity_status is invalid")
        expected_count = _plain_count(
            evidence["frame_record_count"],
            "input_evidence.frame_record_count",
            minimum=1,
        )
        for field in _COLLECTOR_QUALITY_COUNT_FIELDS:
            _plain_count(evidence[field], f"input_evidence.{field}")
        rates = evidence["tick_rate_hz_values"]
        if (
            type(rates) not in {list, tuple}
            or len(rates) != 1
            or type(rates[0]) is not int
            or rates[0] != tick_rate_hz
        ):
            raise FuelModelReplayError(
                "collector evidence must bind exactly the pipeline tick rate"
            )
        _sha256(evidence["records_sha256"], "input_evidence.records_sha256")
    if not 1 <= tick_rate_hz <= 360:
        raise FuelModelReplayError("evidence tick rate must be from 1 to 360")
    return evidence, source_id, session_id, source_kind, expected_count


def _sample_identity(
    sample: TelemetrySample,
) -> tuple[str | None, str | None, SourceKind | None]:
    source_id = (
        sample.source.source_id.value
        if sample.source.source_id.presence is Presence.PRESENT
        and type(sample.source.source_id.value) is str
        else None
    )
    session_id = (
        sample.session.session_id.value
        if sample.session.session_id.presence is Presence.PRESENT
        and type(sample.session.session_id.value) is str
        else None
    )
    source_kind = (
        sample.source.source_kind.value
        if sample.source.source_kind.presence is Presence.PRESENT
        and isinstance(sample.source.source_kind.value, SourceKind)
        else None
    )
    return source_id, session_id, source_kind


def _finite_float(field: TelemetryField[Any]) -> float | None:
    if field.presence is not Presence.PRESENT:
        return None
    value = field.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _plain_int(field: TelemetryField[Any]) -> int | None:
    if field.presence is not Presence.PRESENT or type(field.value) is not int:
        return None
    return field.value


def _plain_bool(field: TelemetryField[Any]) -> bool | None:
    if field.presence is not Presence.PRESENT or type(field.value) is not bool:
        return None
    return field.value


def _quality_status(sample: TelemetrySample) -> QualityStatus:
    field = sample.quality.status
    if field.presence is Presence.PRESENT and isinstance(field.value, QualityStatus):
        return field.value
    return QualityStatus.REJECTED


def _channel_values(sample: TelemetrySample) -> dict[str, object | None]:
    return {
        "SessionTime": _finite_float(sample.session.session_time_s),
        "SessionTick": _plain_int(sample.session.session_tick),
        "Lap": _plain_int(sample.lap.lap_number),
        "LapCompleted": _plain_int(sample.lap.laps_completed),
        "LapDistPct": _finite_float(sample.lap.lap_distance_pct),
        "FuelLevel": _finite_float(sample.fuel.level_l),
        "OnPitRoad": _plain_bool(sample.pit.on_pit_road),
        "PlayerCarInPitStall": _plain_bool(sample.pit.in_pit_stall),
        "Speed": _finite_float(sample.lap.speed_mps),
        "PlayerTrackSurface": _plain_int(sample.flags.player_track_surface),
    }


def _numpy_channels(
    values: Mapping[str, list[object | None]],
) -> tuple[dict[str, np.ndarray], dict[str, int]]:
    missing = {
        name: sum(item is None for item in items)
        for name, items in values.items()
    }
    if any(missing[name] for name in _REQUIRED_CHANNELS):
        return {}, missing

    channels: dict[str, np.ndarray] = {
        "SessionTime": np.asarray(values["SessionTime"], dtype=np.float64),
        "SessionTick": np.asarray(values["SessionTick"], dtype=np.int64),
        "Lap": np.asarray(values["Lap"], dtype=np.int64),
        "LapDistPct": np.asarray(values["LapDistPct"], dtype=np.float64),
        "FuelLevel": np.asarray(values["FuelLevel"], dtype=np.float64),
        "OnPitRoad": np.asarray(values["OnPitRoad"], dtype=np.bool_),
        "PlayerCarInPitStall": np.asarray(
            values["PlayerCarInPitStall"], dtype=np.bool_
        ),
    }
    if missing["LapCompleted"] == 0:
        channels["LapCompleted"] = np.asarray(
            values["LapCompleted"], dtype=np.int64
        )
    if missing["Speed"] == 0:
        channels["Speed"] = np.asarray(values["Speed"], dtype=np.float64)
    if missing["PlayerTrackSurface"] == 0:
        channels["PlayerTrackSurface"] = np.asarray(
            values["PlayerTrackSurface"], dtype=np.int64
        )
    return channels, missing


def _lap_receipt(laps: tuple[LapObservation, ...], sample_count: int) -> dict[str, object]:
    payload = [lap.to_dict() for lap in laps]
    return {
        "algorithm_version": LAP_ALGORITHM_VERSION,
        "fuel_eligible_lap_count": sum(lap.fuel_eligible for lap in laps),
        "lap_count": len(laps),
        "laps_sha256": _digest(payload),
        "modeled_sample_count": sample_count,
        "quality_complete_lap_count": sum(lap.quality_complete for lap in laps),
        "structurally_complete_lap_count": sum(
            lap.structurally_complete for lap in laps
        ),
    }


def _input_quality_reasons(input_evidence: Mapping[str, object]) -> list[str]:
    reasons: list[str] = []
    if input_evidence.get("completion_status") != "COMPLETE":
        reasons.append("INPUT_NOT_COMPLETE")
    for field, reason in (
        ("duplicate_conflict_count", "DUPLICATE_CONFLICTS"),
        ("dropped_tick_count", "DROPPED_TICKS"),
        ("stale_event_count", "SOURCE_STALE_EVENTS"),
        ("schema_change_count", "SCHEMA_CHANGED"),
        ("session_reset_count", "SESSION_RESET"),
        ("capture_clock_regression_count", "CAPTURE_CLOCK_REGRESSION"),
        ("read_error_frame_count", "SDK_READ_ERRORS"),
        ("driver_info_key_count", "DRIVER_INFO_PERSISTED"),
    ):
        value = input_evidence.get(field, 0)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            reasons.append(reason)
    return reasons


def _build_fuel_model_replay_samples(
    samples: Iterable[TelemetrySample],
    *,
    input_kind: str,
    input_evidence: IbtInputEvidence | CollectorInputEvidence,
    tick_rate_hz: int,
    stale_after_s: float,
    opponent_error_policy: str,
    scenario: FuelScenario,
) -> dict[str, object]:
    """Run one normalized source through events, lap features, and fuel model."""

    if input_kind not in {"ibt", "collector"}:
        raise FuelModelReplayError("input_kind must be ibt or collector")
    if type(tick_rate_hz) is not int or not 1 <= tick_rate_hz <= 360:
        raise FuelModelReplayError("tick_rate_hz must be a plain integer from 1 to 360")
    if not isinstance(input_evidence, (IbtInputEvidence, CollectorInputEvidence)):
        raise FuelModelReplayError(
            "input_evidence must come from a validated telemetry adapter"
        )
    if (
        isinstance(stale_after_s, bool)
        or not isinstance(stale_after_s, (int, float))
        or not math.isfinite(stale_after_s)
        or stale_after_s <= 0
    ):
        raise FuelModelReplayError("stale_after_s must be finite and greater than zero")
    if not isinstance(scenario, FuelScenario):
        raise FuelModelReplayError("scenario must be a FuelScenario")
    if opponent_error_policy not in {"degrade", "reject"}:
        raise FuelModelReplayError("opponent_error_policy is invalid")

    (
        evidence_payload,
        evidence_source_id,
        evidence_session_id,
        evidence_source_kind,
        expected_sample_count,
    ) = _validate_input_evidence(
        input_evidence,
        input_kind=input_kind,
        tick_rate_hz=tick_rate_hz,
    )

    # Validate the scenario even when source quality later blocks lap modeling.
    estimate_fuel_strategy((), **scenario.model_kwargs())
    scenario_payload = scenario.to_dict()
    scenario_sha256 = _digest(scenario_payload)

    event_pipeline = TelemetryEventPipeline()
    values: dict[str, list[object | None]] = {
        name: [] for name in _ALL_CHANNELS
    }
    modeled_sample_count = 0
    degraded_sample_count = 0
    normalized_dropped_tick_count = 0
    quality_issue_counts: Counter[str] = Counter()
    normalized_digest = hashlib.sha256()
    for sample in samples:
        if not isinstance(sample, TelemetrySample):
            raise FuelModelReplayError("samples must contain TelemetrySample values")
        if _sample_identity(sample) != (
            evidence_source_id,
            evidence_session_id,
            evidence_source_kind,
        ):
            raise FuelModelReplayError(
                "normalized sample identity/source_kind does not match input_evidence"
            )
        encoded_sample = sample.to_json_line().encode("utf-8")
        normalized_digest.update(len(encoded_sample).to_bytes(8, "little"))
        normalized_digest.update(encoded_sample)
        event_pipeline.feed(sample)
        status = _quality_status(sample)
        if status is QualityStatus.REJECTED:
            continue
        if status is QualityStatus.DEGRADED:
            degraded_sample_count += 1
        dropped = _plain_int(sample.quality.dropped_ticks)
        if dropped is not None and dropped > 0:
            normalized_dropped_tick_count += dropped
        issues = sample.quality.issues
        if (
            issues.presence is Presence.PRESENT
            and isinstance(issues.value, tuple)
            and all(type(item) is str for item in issues.value)
        ):
            quality_issue_counts.update(issues.value)
        row = _channel_values(sample)
        for name in _ALL_CHANNELS:
            values[name].append(row[name])
        modeled_sample_count += 1

    event_receipt = event_pipeline.finish().to_dict()
    if event_receipt["sample_count"] != expected_sample_count:
        raise FuelModelReplayError(
            "normalized sample count does not match bound input evidence"
        )
    channels, missing = _numpy_channels(values)
    reasons = _input_quality_reasons(evidence_payload)
    if normalized_dropped_tick_count:
        reasons.append("DROPPED_TICKS")
    if event_receipt["rejected_sample_count"]:
        reasons.append("NORMALIZED_REJECTED_SAMPLES")
    if event_receipt["source_epoch_count"] != 1:
        reasons.append("MULTIPLE_SOURCE_EPOCHS")
    if event_receipt["session_epoch_count"] != 1:
        reasons.append("MULTIPLE_SESSION_EPOCHS")
    if event_receipt["accepted_sample_count"] != modeled_sample_count:
        reasons.append("EVENT_MODEL_ACCEPTANCE_MISMATCH")
    if modeled_sample_count == 0:
        reasons.append("NO_MODEL_SAMPLES")
    for name in _REQUIRED_CHANNELS:
        if missing[name]:
            reasons.append(f"MISSING_REQUIRED_CHANNEL:{name}")

    hard_blockers = {
        "EVENT_MODEL_ACCEPTANCE_MISMATCH",
        "MULTIPLE_SESSION_EPOCHS",
        "MULTIPLE_SOURCE_EPOCHS",
        "NO_MODEL_SAMPLES",
        "NORMALIZED_REJECTED_SAMPLES",
    }
    can_model = bool(channels) and not hard_blockers.intersection(reasons)
    laps: tuple[LapObservation, ...] = ()
    model_output: dict[str, object] | None = None
    if can_model:
        try:
            laps = segment_laps(channels, tick_rate_hz)
        except (KeyError, TypeError, ValueError) as exc:
            reasons.append("LAP_SEGMENTATION_FAILED")
            can_model = False
            segmentation_error = f"{type(exc).__name__}:{exc}"
        else:
            segmentation_error = None
            result = estimate_fuel_strategy(laps, **scenario.model_kwargs())
            model_output = result.to_dict()
            if model_output.get("current_fuel_l") is not None:
                model_output["current_fuel_l"]["label"] = scenario_payload[
                    "current_fuel_l"
                ]["provenance"].lower()
            if scenario.remaining_laps is not None and model_output.get(
                "remaining_laps"
            ) is not None:
                model_output["remaining_laps"]["label"] = scenario_payload[
                    "remaining_laps"
                ]["provenance"].lower()
            if not result.ready:
                reasons.extend(result.reason_codes)
    else:
        segmentation_error = None

    reasons = list(dict.fromkeys(reasons))
    lap_receipt = _lap_receipt(laps, modeled_sample_count)
    pipeline_config: dict[str, object] = {
        "event_contract_version": EVENT_CONTRACT_VERSION,
        "feature_pipeline_version": FUEL_FEATURE_PIPELINE_VERSION,
        "fuel_model_version": FUEL_MODEL_VERSION,
        "lap_algorithm_version": LAP_ALGORITHM_VERSION,
        "normalization": {
            "opponent_error_policy": opponent_error_policy,
            "profile_version": NORMALIZATION_PROFILE_VERSION,
            "stale_after_us": round(float(stale_after_s) * 1_000_000),
        },
        "normalized_telemetry_contract_version": TELEMETRY_CONTRACT_VERSION,
        "tick_rate_hz": tick_rate_hz,
    }
    pipeline_config["config_sha256"] = _digest(pipeline_config)
    model_output_sha256 = _digest(model_output)
    model_semantic_binding = {
        "lap_receipt": lap_receipt,
        "model_output": model_output,
        "pipeline": pipeline_config,
        "scenario": scenario_payload,
    }
    model_semantic_sha256 = _digest(model_semantic_binding)
    normalized_input_receipt = {
        "contract_version": TELEMETRY_CONTRACT_VERSION,
        "sample_count": event_receipt["sample_count"],
        "samples_sha256": normalized_digest.hexdigest(),
    }

    evidence_ids = [
        f"{normalized_input_receipt['samples_sha256']}:{LAP_ALGORITHM_VERSION}:lap:{lap.ordinal}"
        for lap in laps
        if lap.fuel_eligible
    ]
    quality_gate = {
        "reasons": reasons,
        "status": "PASS" if not reasons and model_output is not None else "DEGRADED",
    }
    model_ready = (
        quality_gate["status"] == "PASS"
        and model_output is not None
        and model_output.get("status") == "ready"
    )
    recommendation = (
        build_fuel_shadow_recommendation(
            result,
            evidence_ids=evidence_ids,
            scenario_sha256=scenario_sha256,
            scenario_provenance=scenario.provenance,
        )
        if model_ready
        else None
    )
    recommendations = [recommendation] if recommendation is not None else []
    capabilities = {
        "current_tire_wear": unavailable_inference_capability(
            reasons=("CURRENT_STINT_TIRE_WEAR_MODEL_NOT_IMPLEMENTED",),
            blocked_claims=("CURRENT_TIRE_WEAR_CLAIM",),
        ),
        "fuel_model_shadow": {
            "status": "PASS" if model_ready else "FAIL",
            "reasons": [] if model_ready else reasons,
        },
        "opponent_fuel": unavailable_inference_capability(
            reasons=("OPPONENT_FUEL_NOT_EXPOSED_BY_SDK",),
            blocked_claims=("OPPONENT_FUEL_CLAIM",),
        ),
        "race_recommendation": {
            "status": "BLOCKED",
            "reasons": [
                "SHADOW_ONLY",
                "EVENT_RULES_PROFILE_MISSING",
                "TRAFFIC_MODEL_NOT_IMPLEMENTED",
            ],
        },
        "traffic_model": unavailable_inference_capability(
            reasons=("TRAFFIC_MODEL_NOT_IMPLEMENTED",),
            blocked_claims=("REJOIN_TRAFFIC_CLAIM",),
        ),
    }
    binding = {
        "capabilities": capabilities,
        "contract_version": FUEL_MODEL_REPLAY_CONTRACT_VERSION,
        "event_receipt": event_receipt,
        "input_evidence": evidence_payload,
        "input_kind": input_kind,
        "lap_receipt": lap_receipt,
        "model_output": model_output,
        "model_output_sha256": model_output_sha256,
        "model_semantic_sha256": model_semantic_sha256,
        "normalized_input_receipt": normalized_input_receipt,
        "pipeline": pipeline_config,
        "quality_gate": quality_gate,
        "recommendations": recommendations,
        "scenario": scenario_payload,
        "scenario_sha256": scenario_sha256,
        "series_evidence": {
            "degraded_sample_count": degraded_sample_count,
            "missing_channel_sample_counts": dict(sorted(missing.items())),
            "modeled_sample_count": modeled_sample_count,
            "normalized_dropped_tick_count": normalized_dropped_tick_count,
            "quality_issue_counts": dict(sorted(quality_issue_counts.items())),
            "segmentation_error": segmentation_error,
        },
    }
    return {**binding, "fuel_replay_sha256": _digest(binding)}


def build_fuel_model_replay(
    run: ValidatedIbtRun | ValidatedCollectorRun,
    *,
    scenario: FuelScenario,
) -> dict[str, object]:
    """Consume one inseparable adapter-validated run through the fuel model.

    Raw samples, source evidence, collector sideband quality, and normalization
    configuration arrive on the same opaque run object.  Accepting them as
    separate arguments would allow a caller to pair genuine samples with
    forged drop, read-error, duplicate-conflict, or privacy counters.
    """

    if type(run) not in {ValidatedIbtRun, ValidatedCollectorRun}:
        raise FuelModelReplayError(
            "run must come directly from an open validated telemetry adapter"
        )
    state = _validated_run_state(run)
    if state is None:
        raise FuelModelReplayError(
            "run must come directly from an open validated telemetry adapter"
        )
    if type(run) is ValidatedIbtRun:
        if type(state.evidence) is not IbtInputEvidence:
            raise FuelModelReplayError("validated IBT run registry state is invalid")
        input_kind = "ibt"
        tick_rate_hz = state.evidence.tick_rate_hz
    else:
        if type(state.evidence) is not CollectorInputEvidence:
            raise FuelModelReplayError("validated collector run registry state is invalid")
        input_kind = "collector"
        rates = state.evidence.tick_rate_hz_values
        if len(rates) != 1:
            raise FuelModelReplayError(
                "collector must expose exactly one SDK tick rate"
            )
        tick_rate_hz = rates[0]
    return _build_fuel_model_replay_samples(
        state.samples,
        input_kind=input_kind,
        input_evidence=state.evidence,
        tick_rate_hz=tick_rate_hz,
        stale_after_s=state.stale_after_s,
        opponent_error_policy=state.opponent_error_policy,
        scenario=scenario,
    )
