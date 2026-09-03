"""Source-neutral replay of normalized telemetry through the driving model.

The public entry point accepts only an active adapter-created run.  Raw source
evidence, normalized samples, and the narrow track-length context therefore
remain inseparable.  IBT and collector inputs share the same semantic-input,
lap-segmentation, distance-domain model, and shadow-recommendation pipeline.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from typing import Any

import numpy as np

from .adapters import (
    CollectorInputEvidence,
    IbtInputEvidence,
    TrackContextAvailability,
    TrackContextEvidence,
    ValidatedCollectorRun,
    ValidatedIbtRun,
    _validated_run_state,
)
from .capabilities import unavailable_inference_capability
from .contracts import LAP_ALGORITHM_VERSION, NORMALIZATION_PROFILE_VERSION
from .driving import (
    DRIVING_ALGORITHM_VERSION,
    DrivingAnalysis,
    DrivingAnalysisConfig,
    analyze_driving,
    build_driving_shadow_recommendations,
)
from .events import EVENT_CONTRACT_VERSION, TelemetryEventPipeline
from .laps import LapObservation, segment_laps
from .model_replay import (
    FuelModelReplayError,
    _finite_float,
    _input_quality_reasons,
    _plain_bool,
    _plain_int,
    _quality_status,
    _sample_identity,
    _validate_input_evidence,
)
from .telemetry import (
    TELEMETRY_CONTRACT_VERSION,
    Presence,
    QualityStatus,
    TelemetryField,
    TelemetrySample,
)

DRIVING_MODEL_REPLAY_CONTRACT_VERSION = "driving-model-replay-v1"
DRIVING_FEATURE_PIPELINE_VERSION = "normalized-lap-driving-v1"
DRIVING_SEMANTIC_INPUT_CONTRACT_VERSION = "driving-semantic-input-v1"

_REQUIRED_CHANNELS = (
    "SessionTime",
    "SessionTick",
    "Lap",
    "LapDistPct",
    "Speed",
    "Throttle",
    "Brake",
    "SteeringWheelAngle",
    "OnPitRoad",
    "PlayerTrackSurface",
)
_OPTIONAL_CHANNELS = ("SessionNum", "LapCompleted")
_INCIDENT_CHANNELS = (
    "PlayerCarMyIncidentCount",
    "PlayerCarDriverIncidentCount",
    "PlayerCarTeamIncidentCount",
)
_ALL_CHANNELS = _REQUIRED_CHANNELS + _OPTIONAL_CHANNELS + _INCIDENT_CHANNELS
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
_FAIL_REASON_CODES = frozenset(
    {
        "CAPTURE_CLOCK_REGRESSION",
        "DRIVER_INFO_PERSISTED",
        "DROPPED_TICKS",
        "DUPLICATE_CONFLICTS",
        "EVENT_MODEL_ACCEPTANCE_MISMATCH",
        "INCIDENT_COUNT_REGRESSION",
        "INCONSISTENT_TELEMETRY",
        "LAP_DELTA_CLOSURE_FAILED",
        "LAP_SEGMENTATION_FAILED",
        "MULTIPLE_SESSION_EPOCHS",
        "MULTIPLE_SOURCE_EPOCHS",
        "NO_MODEL_SAMPLES",
        "NON_CLOSING_CORNER_PARTITION",
        "NORMALIZED_REJECTED_SAMPLES",
        "SDK_READ_ERRORS",
        "SCHEMA_CHANGED",
        "SESSION_RESET",
        "SOURCE_STALE_EVENTS",
        "TRACK_CONTEXT_CONFLICT",
    }
)


class DrivingModelReplayError(ValueError):
    """Raised when the shared driving replay contract is invalid."""


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
        raise DrivingModelReplayError(
            "driving model replay value is not canonical-JSON-safe"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validate_config(config: DrivingAnalysisConfig, top: int) -> None:
    if not isinstance(config, DrivingAnalysisConfig):
        raise DrivingModelReplayError("config must be a DrivingAnalysisConfig")
    if type(top) is not int or top < 1:
        raise DrivingModelReplayError("top must be a positive integer")

    values = asdict(config)
    integer_fields = {
        "min_clean_laps": 3,
        "min_reference_group_laps": 2,
        "min_evidence_laps": 1,
    }
    for name, minimum in integer_fields.items():
        value = values[name]
        if type(value) is not int or value < minimum:
            raise DrivingModelReplayError(
                f"driving config {name} must be a plain integer >= {minimum}"
            )
    for name, value in values.items():
        if name in integer_fields:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DrivingModelReplayError(f"driving config {name} must be numeric")
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise DrivingModelReplayError(
                f"driving config {name} must be finite and positive"
            )
    if not 0.25 <= config.grid_step_m <= 10.0:
        raise DrivingModelReplayError("driving config grid_step_m is out of range")
    if not 0 < config.fastest_group_fraction <= 1:
        raise DrivingModelReplayError(
            "driving config fastest_group_fraction is out of range"
        )
    if config.max_reference_duration_spread_fraction > 1:
        raise DrivingModelReplayError(
            "driving config max_reference_duration_spread_fraction is out of range"
        )
    if not 0 < config.brake_release_threshold < config.brake_threshold <= 1:
        raise DrivingModelReplayError("driving config brake thresholds are invalid")
    if not 0 < config.second_lift_threshold < config.throttle_pickup_threshold <= 1:
        raise DrivingModelReplayError("driving config throttle thresholds are invalid")


def _field_presence(field: TelemetryField[Any]) -> str:
    return field.presence.value


def _sample_fields(
    sample: TelemetrySample,
) -> dict[str, tuple[TelemetryField[Any], object | None]]:
    return {
        "SessionNum": (
            sample.session.session_num,
            _plain_int(sample.session.session_num),
        ),
        "SessionTick": (
            sample.session.session_tick,
            _plain_int(sample.session.session_tick),
        ),
        "SessionTime": (
            sample.session.session_time_s,
            _finite_float(sample.session.session_time_s),
        ),
        "Lap": (sample.lap.lap_number, _plain_int(sample.lap.lap_number)),
        "LapCompleted": (
            sample.lap.laps_completed,
            _plain_int(sample.lap.laps_completed),
        ),
        "LapDistPct": (
            sample.lap.lap_distance_pct,
            _finite_float(sample.lap.lap_distance_pct),
        ),
        "Speed": (sample.lap.speed_mps, _finite_float(sample.lap.speed_mps)),
        "Throttle": (
            sample.controls.throttle,
            _finite_float(sample.controls.throttle),
        ),
        "Brake": (sample.controls.brake, _finite_float(sample.controls.brake)),
        "SteeringWheelAngle": (
            sample.controls.steering_angle_rad,
            _finite_float(sample.controls.steering_angle_rad),
        ),
        "OnPitRoad": (
            sample.pit.on_pit_road,
            _plain_bool(sample.pit.on_pit_road),
        ),
        "PlayerTrackSurface": (
            sample.flags.player_track_surface,
            _plain_int(sample.flags.player_track_surface),
        ),
        "PlayerCarMyIncidentCount": (
            sample.incidents.player_car_my_incident_count,
            _plain_int(sample.incidents.player_car_my_incident_count),
        ),
        "PlayerCarDriverIncidentCount": (
            sample.incidents.player_car_driver_incident_count,
            _plain_int(sample.incidents.player_car_driver_incident_count),
        ),
        "PlayerCarTeamIncidentCount": (
            sample.incidents.player_car_team_incident_count,
            _plain_int(sample.incidents.player_car_team_incident_count),
        ),
    }


def _select_incident_channel(missing: Mapping[str, int]) -> str | None:
    return next((name for name in _INCIDENT_CHANNELS if missing[name] == 0), None)


def _numpy_channels(
    values: Mapping[str, list[object | None]],
    missing: Mapping[str, int],
    selected_incident_channel: str | None,
) -> dict[str, np.ndarray]:
    if any(missing[name] for name in _REQUIRED_CHANNELS):
        return {}
    channels: dict[str, np.ndarray] = {
        "SessionTime": np.asarray(values["SessionTime"], dtype=np.float64),
        "SessionTick": np.asarray(values["SessionTick"], dtype=np.int64),
        "Lap": np.asarray(values["Lap"], dtype=np.int64),
        "LapDistPct": np.asarray(values["LapDistPct"], dtype=np.float64),
        "Speed": np.asarray(values["Speed"], dtype=np.float64),
        "Throttle": np.asarray(values["Throttle"], dtype=np.float64),
        "Brake": np.asarray(values["Brake"], dtype=np.float64),
        "SteeringWheelAngle": np.asarray(
            values["SteeringWheelAngle"], dtype=np.float64
        ),
        "OnPitRoad": np.asarray(values["OnPitRoad"], dtype=np.bool_),
        "PlayerTrackSurface": np.asarray(
            values["PlayerTrackSurface"], dtype=np.int64
        ),
    }
    if missing["LapCompleted"] == 0:
        channels["LapCompleted"] = np.asarray(
            values["LapCompleted"], dtype=np.int64
        )
    if selected_incident_channel is not None:
        channels[selected_incident_channel] = np.asarray(
            values[selected_incident_channel], dtype=np.int64
        )
    return channels


def _semantic_input_receipt(
    values: Mapping[str, list[object | None]],
    presences: Mapping[str, list[str]],
    *,
    quality_statuses: list[str],
    quality_status_presences: list[str],
    dropped_ticks: list[int | None],
    dropped_tick_presences: list[str],
    quality_issues: list[list[str] | None],
    quality_issue_presences: list[str],
    incident_channel: str | None,
) -> dict[str, object]:
    modeled_count = len(quality_statuses)
    selected = incident_channel or _INCIDENT_CHANNELS[0]
    digest = hashlib.sha256()
    for index in range(modeled_count):
        row: dict[str, object] = {}
        for name in _OPTIONAL_CHANNELS + _REQUIRED_CHANNELS:
            row[name] = {
                "presence": presences[name][index],
                "value": values[name][index],
            }
        row["PlayerIncidentCount"] = {
            "presence": presences[selected][index],
            "value": values[selected][index],
        }
        row["QualityStatus"] = {
            "presence": quality_status_presences[index],
            "value": quality_statuses[index],
        }
        row["DroppedTicks"] = {
            "presence": dropped_tick_presences[index],
            "value": dropped_ticks[index],
        }
        row["QualityIssues"] = {
            "presence": quality_issue_presences[index],
            "value": quality_issues[index],
        }
        encoded = _canonical_json(row)
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
    return {
        "channels": list(_SEMANTIC_CHANNELS),
        "contract_version": DRIVING_SEMANTIC_INPUT_CONTRACT_VERSION,
        "sample_count": modeled_count,
        "samples_sha256": digest.hexdigest(),
    }


def _lap_receipt(
    laps: tuple[LapObservation, ...], sample_count: int
) -> dict[str, object]:
    return {
        "algorithm_version": LAP_ALGORITHM_VERSION,
        "clean_driving_lap_count": sum(lap.clean_for_driving for lap in laps),
        "cleanliness_observable_lap_count": sum(
            lap.cleanliness_observable for lap in laps
        ),
        "lap_count": len(laps),
        "laps_sha256": _digest([lap.to_dict() for lap in laps]),
        "modeled_sample_count": sample_count,
        "quality_complete_lap_count": sum(lap.quality_complete for lap in laps),
        "structurally_complete_lap_count": sum(
            lap.structurally_complete for lap in laps
        ),
    }


def _analysis_reason(reason: str) -> str:
    if reason.startswith("INSUFFICIENT_CLEAN_LAPS:"):
        return "INSUFFICIENT_CLEAN_LAPS"
    if reason == "NO_REPRODUCIBLE_BRAKING_ZONES":
        return reason
    if reason.startswith("NON_CLOSING_CORNER_PARTITION:"):
        return "NON_CLOSING_CORNER_PARTITION"
    if reason.startswith("LAP_DELTA_CLOSURE_FAILED:"):
        return "LAP_DELTA_CLOSURE_FAILED"
    if reason.startswith("MISSING_CHANNELS:"):
        return "MISSING_REQUIRED_CHANNEL:" + reason.partition(":")[2]
    if reason.startswith("INCONSISTENT_TELEMETRY:") and (
        "fastest-lap group is not reproducible" in reason
    ):
        return "REFERENCE_GROUP_NOT_REPRODUCIBLE"
    if reason.startswith("INCONSISTENT_TELEMETRY:"):
        return "INCONSISTENT_TELEMETRY"
    return "INCONSISTENT_TELEMETRY"


def _ready_analysis_invariant_reasons(
    analysis: DrivingAnalysis,
) -> list[str]:
    if analysis.status != "READY":
        return []
    reasons: list[str] = []
    eligible = set(analysis.eligible_lap_ordinals)
    reference = analysis.reference
    if (
        reference is None
        or reference.lap_ordinal not in eligible
        or not set(reference.fastest_group_lap_ordinals).issubset(eligible)
    ):
        reasons.append("INCONSISTENT_TELEMETRY")
    tolerance_m = max(1e-9, analysis.grid_step_m * 1e-6)
    expected_start = 0.0
    for corner in analysis.corners:
        if abs(corner.accounting_start_m - expected_start) > tolerance_m:
            reasons.append("NON_CLOSING_CORNER_PARTITION")
            break
        expected_start = corner.carry_end_m
    if not analysis.corners or abs(expected_start - analysis.track_length_m) > tolerance_m:
        reasons.append("NON_CLOSING_CORNER_PARTITION")
    if len(analysis.corner_metrics) != len(eligible) * len(analysis.corners):
        reasons.append("INCONSISTENT_TELEMETRY")
    closure_ordinals = [item.lap_ordinal for item in analysis.delta_closures]
    if (
        len(closure_ordinals) != len(eligible)
        or set(closure_ordinals) != eligible
        or any(not item.closed for item in analysis.delta_closures)
    ):
        reasons.append("LAP_DELTA_CLOSURE_FAILED")
    for diagnosis in analysis.diagnoses:
        evidence = set(diagnosis.evidence_lap_ordinals)
        counterexamples = set(diagnosis.counterexample_lap_ordinals)
        if (
            not evidence.issubset(eligible)
            or not counterexamples.issubset(eligible)
            or evidence.intersection(counterexamples)
        ):
            reasons.append("INCONSISTENT_TELEMETRY")
            break
    return list(dict.fromkeys(reasons))


def _reason_code(reason: str) -> str:
    return reason.partition(":")[0]


def _build_driving_model_replay_samples(
    samples: Iterable[TelemetrySample],
    *,
    input_kind: str,
    input_evidence: IbtInputEvidence | CollectorInputEvidence,
    track_context: TrackContextEvidence,
    tick_rate_hz: int,
    stale_after_s: float,
    opponent_error_policy: str,
    config: DrivingAnalysisConfig | None = None,
    top: int = 3,
) -> dict[str, object]:
    """Run one adapter-bound normalized source through the driving model."""

    selected_config = config or DrivingAnalysisConfig()
    _validate_config(selected_config, top)
    if input_kind not in {"ibt", "collector"}:
        raise DrivingModelReplayError("input_kind must be ibt or collector")
    if type(tick_rate_hz) is not int or not 1 <= tick_rate_hz <= 360:
        raise DrivingModelReplayError(
            "tick_rate_hz must be a plain integer from 1 to 360"
        )
    if (
        isinstance(stale_after_s, bool)
        or not isinstance(stale_after_s, (int, float))
        or not math.isfinite(stale_after_s)
        or stale_after_s <= 0
    ):
        raise DrivingModelReplayError(
            "stale_after_s must be finite and greater than zero"
        )
    if opponent_error_policy not in {"degrade", "reject"}:
        raise DrivingModelReplayError("opponent_error_policy is invalid")
    if type(track_context) is not TrackContextEvidence:
        raise DrivingModelReplayError(
            "track_context must come from a validated telemetry adapter"
        )
    try:
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
    except FuelModelReplayError as exc:
        raise DrivingModelReplayError(str(exc)) from exc
    if track_context.source_binding_sha256 != _digest(evidence_payload):
        raise DrivingModelReplayError("track context is not bound to input evidence")

    event_pipeline = TelemetryEventPipeline()
    values: dict[str, list[object | None]] = {name: [] for name in _ALL_CHANNELS}
    presences: dict[str, list[str]] = {name: [] for name in _ALL_CHANNELS}
    quality_statuses: list[str] = []
    quality_status_presences: list[str] = []
    dropped_ticks: list[int | None] = []
    dropped_tick_presences: list[str] = []
    quality_issues: list[list[str] | None] = []
    quality_issue_presences: list[str] = []
    quality_issue_counts: Counter[str] = Counter()
    normalized_digest = hashlib.sha256()
    modeled_sample_count = 0
    degraded_sample_count = 0
    normalized_dropped_tick_count = 0

    for sample in samples:
        if not isinstance(sample, TelemetrySample):
            raise DrivingModelReplayError(
                "samples must contain TelemetrySample values"
            )
        if _sample_identity(sample) != (
            evidence_source_id,
            evidence_session_id,
            evidence_source_kind,
        ):
            raise DrivingModelReplayError(
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
        row = _sample_fields(sample)
        for name in _ALL_CHANNELS:
            field, value = row[name]
            values[name].append(value)
            presences[name].append(_field_presence(field))

        dropped = _plain_int(sample.quality.dropped_ticks)
        dropped_ticks.append(dropped)
        dropped_tick_presences.append(_field_presence(sample.quality.dropped_ticks))
        if dropped is not None and dropped > 0:
            normalized_dropped_tick_count += dropped
        issues_field = sample.quality.issues
        issues_value: list[str] | None = None
        if (
            issues_field.presence is Presence.PRESENT
            and isinstance(issues_field.value, tuple)
            and all(type(item) is str for item in issues_field.value)
        ):
            issues_value = list(issues_field.value)
            quality_issue_counts.update(issues_field.value)
        quality_issues.append(issues_value)
        quality_issue_presences.append(_field_presence(issues_field))
        quality_statuses.append(status.value)
        quality_status_presences.append(_field_presence(sample.quality.status))
        modeled_sample_count += 1

    event_receipt = event_pipeline.finish().to_dict()
    if event_receipt["sample_count"] != expected_sample_count:
        raise DrivingModelReplayError(
            "normalized sample count does not match bound input evidence"
        )
    missing = {
        name: sum(item is None for item in channel_values)
        for name, channel_values in values.items()
    }
    incident_channel = _select_incident_channel(missing)
    channels = _numpy_channels(values, missing, incident_channel)
    semantic_input_receipt = _semantic_input_receipt(
        values,
        presences,
        quality_statuses=quality_statuses,
        quality_status_presences=quality_status_presences,
        dropped_ticks=dropped_ticks,
        dropped_tick_presences=dropped_tick_presences,
        quality_issues=quality_issues,
        quality_issue_presences=quality_issue_presences,
        incident_channel=incident_channel,
    )

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
    if incident_channel is None:
        reasons.append("CLEANLINESS_UNOBSERVABLE")
    if track_context.availability is not TrackContextAvailability.AVAILABLE:
        reasons.append("TRACK_LENGTH_UNAVAILABLE")

    incident_regressions: list[str] = []
    for name in _INCIDENT_CHANNELS:
        if missing[name] == 0 and np.any(
            np.diff(np.asarray(values[name], dtype=np.int64)) < 0
        ):
            incident_regressions.append(name)
    if incident_regressions:
        reasons.append("INCIDENT_COUNT_REGRESSION")

    event_hard_blockers = {
        "EVENT_MODEL_ACCEPTANCE_MISMATCH",
        "MULTIPLE_SESSION_EPOCHS",
        "MULTIPLE_SOURCE_EPOCHS",
        "NO_MODEL_SAMPLES",
        "NORMALIZED_REJECTED_SAMPLES",
    }
    can_segment = (
        bool(channels)
        and incident_channel is not None
        and track_context.availability is TrackContextAvailability.AVAILABLE
        and not event_hard_blockers.intersection(reasons)
    )
    laps: tuple[LapObservation, ...] = ()
    segmentation_error: str | None = None
    analysis: DrivingAnalysis | None = None
    model_output: dict[str, object] | None = None
    if can_segment:
        try:
            laps = segment_laps(channels, tick_rate_hz)
        except (KeyError, TypeError, ValueError) as exc:
            reasons.append("LAP_SEGMENTATION_FAILED")
            segmentation_error = f"{type(exc).__name__}:{exc}"
        else:
            assert track_context.track_length_mm is not None
            analysis = analyze_driving(
                channels,
                laps,
                track_length_m=track_context.track_length_mm / 1000.0,
                config=selected_config,
            )
            model_output = analysis.to_dict(include_traces=False)
            if analysis.status == "REFUSED":
                reasons.extend(_analysis_reason(item) for item in analysis.refusal_reasons)
            else:
                reasons.extend(_ready_analysis_invariant_reasons(analysis))

    reasons = list(dict.fromkeys(reasons))
    has_fail_reason = any(_reason_code(item) in _FAIL_REASON_CODES for item in reasons)
    model_ready = analysis is not None and analysis.status == "READY"
    if not reasons and model_ready:
        readiness_status = "PASS"
    elif has_fail_reason:
        readiness_status = "FAIL"
    else:
        readiness_status = "WAIT_DRIVING_DATA"
    quality_gate = {
        "reasons": reasons,
        "status": "PASS" if readiness_status == "PASS" else "DEGRADED",
    }

    normalized_input_receipt = {
        "contract_version": TELEMETRY_CONTRACT_VERSION,
        "sample_count": event_receipt["sample_count"],
        "samples_sha256": normalized_digest.hexdigest(),
    }
    driving_context = track_context.to_dict()
    driving_context_sha256 = track_context.context_sha256
    input_provenance_binding = {
        "driving_context": driving_context,
        "event_receipt": event_receipt,
        "input_evidence": evidence_payload,
        "input_kind": input_kind,
        "normalized_input_receipt": normalized_input_receipt,
    }
    input_provenance_sha256 = _digest(input_provenance_binding)
    lap_receipt = _lap_receipt(laps, modeled_sample_count)
    driving_config = asdict(selected_config)
    pipeline: dict[str, object] = {
        "driving_algorithm_version": DRIVING_ALGORITHM_VERSION,
        "driving_config": driving_config,
        "event_contract_version": EVENT_CONTRACT_VERSION,
        "feature_pipeline_version": DRIVING_FEATURE_PIPELINE_VERSION,
        "lap_algorithm_version": LAP_ALGORITHM_VERSION,
        "normalization": {
            "opponent_error_policy": opponent_error_policy,
            "profile_version": NORMALIZATION_PROFILE_VERSION,
            "stale_after_us": round(float(stale_after_s) * 1_000_000),
        },
        "normalized_telemetry_contract_version": TELEMETRY_CONTRACT_VERSION,
        "semantic_input_contract_version": (
            DRIVING_SEMANTIC_INPUT_CONTRACT_VERSION
        ),
        "tick_rate_hz": tick_rate_hz,
    }
    pipeline["pipeline_sha256"] = _digest(pipeline)
    model_output_sha256 = _digest(model_output)
    model_semantic_binding = {
        "driving_context": {
            "contract_version": track_context.contract_version,
            "source_field": track_context.source_field,
            "track_length_mm": track_context.track_length_mm,
        },
        "lap_receipt": lap_receipt,
        "model_output": model_output,
        "pipeline": pipeline,
        "quality_gate": quality_gate,
        "readiness_status": readiness_status,
        "semantic_input_receipt": semantic_input_receipt,
    }
    model_semantic_sha256 = _digest(model_semantic_binding)

    recommendations: list[dict[str, Any]] = []
    if readiness_status == "PASS" and analysis is not None:
        recommendations = build_driving_shadow_recommendations(
            analysis.diagnoses,
            evidence_prefix=input_provenance_sha256,
            top=top,
        )
        for recommendation in recommendations:
            recommendation["confidence_basis"] = {
                "causal_validity": "NOT_CLAIMED",
                "external_validity": "UNKNOWN",
            }
    evidence_ids = [
        f"{input_provenance_sha256}:{LAP_ALGORITHM_VERSION}:lap:{ordinal}"
        for ordinal in (analysis.eligible_lap_ordinals if analysis is not None else ())
    ]
    capabilities = {
        "curb_guidance": unavailable_inference_capability(
            reasons=("CURB_GEOMETRY_NOT_MODELED",),
            blocked_claims=("CURB_RECOMMENDATION",),
        ),
        "current_tire_wear": unavailable_inference_capability(
            reasons=("CURRENT_STINT_TIRE_WEAR_MODEL_NOT_IMPLEMENTED",),
            blocked_claims=("CURRENT_TIRE_WEAR_CLAIM",),
        ),
        "driving_model_shadow": {
            "evidence_ids": evidence_ids,
            "reasons": [] if readiness_status == "PASS" else reasons,
            "status": "PASS" if readiness_status == "PASS" else "FAIL",
        },
        "personalized_coaching": unavailable_inference_capability(
            reasons=(
                "CONDITION_COHORT_NOT_ATTACHED",
                "MATCHED_CONTEXT_HISTORY_UNAVAILABLE",
                "HUMAN_CORNER_LABELS_MISSING",
            ),
            blocked_claims=(
                "PERSONALIZED_ACTION",
                "CAUSAL_GAIN_CLAIM",
                "TRAIL_BRAKING_CLAIM",
            ),
        ),
        "race_coaching": {
            "reasons": [
                "SHADOW_ONLY",
                "PERSONALIZED_COACHING_UNAVAILABLE",
                "TRAFFIC_MODEL_NOT_IMPLEMENTED",
            ],
            "status": "BLOCKED",
        },
        "traffic_model": unavailable_inference_capability(
            reasons=("TRAFFIC_MODEL_NOT_IMPLEMENTED",),
            blocked_claims=("REJOIN_TRAFFIC_CLAIM",),
        ),
    }
    binding = {
        "capabilities": capabilities,
        "contract_version": DRIVING_MODEL_REPLAY_CONTRACT_VERSION,
        "driving_context": driving_context,
        "driving_context_sha256": driving_context_sha256,
        "event_receipt": event_receipt,
        "input_evidence": evidence_payload,
        "input_kind": input_kind,
        "input_provenance_sha256": input_provenance_sha256,
        "lap_receipt": lap_receipt,
        "model_output": model_output,
        "model_output_sha256": model_output_sha256,
        "model_semantic_sha256": model_semantic_sha256,
        "normalized_input_receipt": normalized_input_receipt,
        "pipeline": pipeline,
        "quality_gate": quality_gate,
        "readiness_status": readiness_status,
        "recommendations": recommendations,
        "semantic_input_receipt": semantic_input_receipt,
        "series_evidence": {
            "analysis_refusal_reasons": (
                list(analysis.refusal_reasons) if analysis is not None else []
            ),
            "degraded_sample_count": degraded_sample_count,
            "incident_regression_channels": incident_regressions,
            "incident_source_field": incident_channel,
            "missing_channel_sample_counts": dict(sorted(missing.items())),
            "modeled_sample_count": modeled_sample_count,
            "normalized_dropped_tick_count": normalized_dropped_tick_count,
            "quality_issue_counts": dict(sorted(quality_issue_counts.items())),
            "segmentation_error": segmentation_error,
        },
    }
    return {**binding, "driving_replay_sha256": _digest(binding)}


def build_driving_model_replay(
    run: ValidatedIbtRun | ValidatedCollectorRun,
    *,
    config: DrivingAnalysisConfig | None = None,
    top: int = 3,
) -> dict[str, object]:
    """Consume one active adapter-created run through the driving model."""

    if type(run) not in {ValidatedIbtRun, ValidatedCollectorRun}:
        raise DrivingModelReplayError(
            "run must come directly from an open validated telemetry adapter"
        )
    state = _validated_run_state(run)
    if state is None:
        raise DrivingModelReplayError(
            "run must come directly from an open validated telemetry adapter"
        )
    if type(run) is ValidatedIbtRun:
        if type(state.evidence) is not IbtInputEvidence:
            raise DrivingModelReplayError(
                "validated IBT run registry state is invalid"
            )
        input_kind = "ibt"
        tick_rate_hz = state.evidence.tick_rate_hz
    else:
        if type(state.evidence) is not CollectorInputEvidence:
            raise DrivingModelReplayError(
                "validated collector run registry state is invalid"
            )
        input_kind = "collector"
        rates = state.evidence.tick_rate_hz_values
        if len(rates) != 1:
            raise DrivingModelReplayError(
                "collector must expose exactly one SDK tick rate"
            )
        tick_rate_hz = rates[0]
    return _build_driving_model_replay_samples(
        state.samples,
        input_kind=input_kind,
        input_evidence=state.evidence,
        track_context=state.track_context,
        tick_rate_hz=tick_rate_hz,
        stale_after_s=state.stale_after_s,
        opponent_error_policy=state.opponent_error_policy,
        config=config,
        top=top,
    )
