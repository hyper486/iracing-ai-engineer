"""Deterministic shadow-mode orchestration for offline IBT analysis."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from .capabilities import unavailable_inference_capability
from .contracts import (
    FUEL_MODEL_VERSION,
    LAP_ALGORITHM_VERSION,
    SHADOW_REPORT_CONTRACT_VERSION,
)
from .driving import (
    DRIVING_ALGORITHM_VERSION,
    REQUIRED_CHANNELS,
    DrivingAnalysisConfig,
    analyze_driving,
    build_driving_shadow_recommendations,
)
from .fuel import (
    FuelScenario,
    FuelStrategyResult,
    build_fuel_shadow_recommendation,
    estimate_fuel_strategy,
)
from .ibt import IbtReader
from .laps import LapObservation
from .quality import QualityReport, analyze_ibt
from .replay import ReplayReceipt, replay_ibt

AnalysisSelection = Literal["fuel", "driving", "all"]

_TRACK_LENGTH = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(km|m)\s*$", re.IGNORECASE)
_TRAFFIC_CHANNELS = {"CarIdxLap", "CarIdxLapDistPct", "CarIdxOnPitRoad"}
_TIRE_WEAR_CHANNELS = {
    f"{corner}wear{position}"
    for corner in ("LF", "RF", "LR", "RR")
    for position in ("L", "M", "R")
}


class ShadowReportError(RuntimeError):
    """Raised when an offline source cannot produce a truthful shadow report."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _parse_track_length_m(raw: object) -> float:
    if not isinstance(raw, str):
        raise ShadowReportError("TRACK_LENGTH_UNAVAILABLE")
    match = _TRACK_LENGTH.fullmatch(raw)
    if match is None:
        raise ShadowReportError(f"UNSUPPORTED_TRACK_LENGTH:{raw}")
    value = float(match.group(1))
    if match.group(2).lower() == "km":
        value *= 1000.0
    if value <= 100.0:
        raise ShadowReportError("TRACK_LENGTH_OUT_OF_RANGE")
    return value


def _lap_id(source_sha256: str, lap: LapObservation) -> str:
    return f"{source_sha256}:{lap.algorithm_version}:lap:{lap.ordinal}"


def _ordinal_lap_id(source_sha256: str, ordinal: int) -> str:
    return f"{source_sha256}:{LAP_ALGORITHM_VERSION}:lap:{ordinal}"


def _capability(
    status: Literal["PASS", "FAIL", "SKIP", "BLOCKED"],
    reasons: tuple[str, ...] = (),
    evidence_lap_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "status": status,
        "reasons": list(reasons),
        "evidence_lap_ids": list(evidence_lap_ids),
    }


def _fuel_facts(report: QualityReport) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for lap in report.laps:
        if not lap.fuel_eligible or lap.fuel_burn_l is None:
            continue
        lap_id = _lap_id(report.source_sha256, lap)
        facts.append(
            {
                "fact_id": f"fuel_burn:{lap_id}",
                "lap_id": lap_id,
                "metric": "fuel_burn",
                "provenance": "DERIVED",
                "source_fields": ["FuelLevel"],
                "unit": "ml_per_lap",
                "value": round(lap.fuel_burn_l * 1000.0),
            }
        )
    return facts


def _fuel_outputs(
    report: QualityReport,
    scenario: FuelScenario,
) -> tuple[FuelStrategyResult, list[dict[str, Any]], list[dict[str, Any]]]:
    result = estimate_fuel_strategy(report.laps, **scenario.model_kwargs())
    if not result.ready or result.burn is None:
        return result, [], []

    evidence_ids = [
        _lap_id(report.source_sha256, lap)
        for lap in report.laps
        if lap.fuel_eligible and lap.fuel_burn_l is not None
    ]
    estimates: list[dict[str, Any]] = [
        {
            "confidence": result.burn.confidence.upper(),
            "estimate_id": "fuel:mean_burn",
            "evidence_ids": evidence_ids,
            "method_version": FUEL_MODEL_VERSION,
            "metric": "mean_fuel_burn",
            "provenance": "DERIVED",
            "unit": "ml_per_lap",
            "value": round(result.burn.mean_l_per_lap * 1000.0),
        },
        {
            "confidence": result.burn.confidence.upper(),
            "estimate_id": "fuel:conservative_burn",
            "evidence_ids": evidence_ids,
            "method_version": FUEL_MODEL_VERSION,
            "metric": "conservative_fuel_burn",
            "provenance": "INFERRED",
            "quantile_ppm": round(result.burn.conservative_quantile * 1_000_000),
            "unit": "ml_per_lap",
            "value": round(result.burn.conservative_l_per_lap * 1000.0),
        },
    ]
    if result.conservative_fuel_to_end_l is not None:
        estimates.append(
            {
                "confidence": result.burn.confidence.upper(),
                "estimate_id": "fuel:to_end_conservative",
                "evidence_ids": evidence_ids,
                "method_version": FUEL_MODEL_VERSION,
                "metric": "conservative_fuel_to_end",
                "provenance": "INFERRED",
                "unit": "ml",
                "value": round(result.conservative_fuel_to_end_l.value * 1000.0),
            }
        )

    recommendation = build_fuel_shadow_recommendation(
        result,
        evidence_ids=evidence_ids,
        scenario_sha256=_digest(scenario.to_dict()),
        scenario_provenance=scenario.provenance,
    )
    recommendations = [recommendation] if recommendation is not None else []
    return result, estimates, recommendations


def _parent_replay(receipt: ReplayReceipt) -> dict[str, Any]:
    return {
        "lap_algorithm_version": receipt.lap_algorithm_version,
        "quality_profile_version": receipt.quality_profile_version,
        "replay_contract_version": receipt.replay_contract_version,
        "replay_sha256": receipt.replay_sha256,
    }


def build_shadow_report(
    path: str | Path,
    *,
    analysis: AnalysisSelection = "all",
    fuel_scenario: FuelScenario | None = None,
    grid_step_m: float = 1.0,
    top: int = 3,
) -> dict[str, Any]:
    """Build a deterministic, non-executable offline engineer report."""

    if analysis not in {"fuel", "driving", "all"}:
        raise ValueError(f"unsupported analysis selection: {analysis}")
    if top < 1:
        raise ValueError("top must be positive")
    if analysis in {"fuel", "all"} and fuel_scenario is None:
        raise ShadowReportError("FUEL_SCENARIO_REQUIRED")

    quality = analyze_ibt(path)
    parent = replay_ibt(path)
    if quality.source_sha256 != parent.source_sha256:
        raise ShadowReportError("SOURCE_HASH_MISMATCH")
    track_length_m = _parse_track_length_m(quality.session_context.get("track_length"))

    with IbtReader(path) as reader:
        available = set(reader.variable_names)
        driving_channels = (
            reader.get_channels(tuple(name for name in REQUIRED_CHANNELS if name in available))
            if analysis in {"driving", "all"}
            else {}
        )
        reader.verify_source_unchanged()

    requested = ("fuel", "driving") if analysis == "all" else (analysis,)
    facts = _fuel_facts(quality) if "fuel" in requested else []
    estimates: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    model_outputs: dict[str, Any] = {}

    if "fuel" in requested:
        assert fuel_scenario is not None
        fuel_result, fuel_estimates, fuel_recommendations = _fuel_outputs(
            quality, fuel_scenario
        )
        fuel_payload = fuel_result.to_dict()
        if fuel_payload.get("current_fuel_l") is not None:
            fuel_payload["current_fuel_l"]["label"] = fuel_scenario.provenance.lower()
        if fuel_scenario.remaining_laps is not None and fuel_payload.get(
            "remaining_laps"
        ) is not None:
            fuel_payload["remaining_laps"]["label"] = fuel_scenario.provenance.lower()
        model_outputs["fuel"] = fuel_payload
        estimates.extend(fuel_estimates)
        recommendations.extend(fuel_recommendations)
        fuel_lap_ids = tuple(item["lap_id"] for item in facts)
        fuel_capability = _capability(
            "PASS" if fuel_result.ready else "FAIL",
            tuple(fuel_result.reason_codes),
            fuel_lap_ids,
        )
    else:
        fuel_capability = _capability("SKIP", ("NOT_REQUESTED",))

    if "driving" in requested:
        driving = analyze_driving(
            driving_channels,
            quality.laps,
            track_length_m=track_length_m,
            config=DrivingAnalysisConfig(grid_step_m=grid_step_m),
        )
        compact_driving = driving.to_dict()
        compact_driving.pop("corner_metrics", None)
        compact_driving.pop("diagnoses", None)
        model_outputs["driving"] = compact_driving
        recommendations.extend(
            build_driving_shadow_recommendations(
                driving.diagnoses,
                evidence_prefix=quality.source_sha256,
                top=top,
            )
        )
        driving_ids = tuple(
            _ordinal_lap_id(quality.source_sha256, ordinal)
            for ordinal in driving.eligible_lap_ordinals
        )
        driving_capability = _capability(
            "PASS" if driving.status == "READY" else "FAIL",
            tuple(driving.refusal_reasons),
            driving_ids,
        )
    else:
        driving_capability = _capability("SKIP", ("NOT_REQUESTED",))

    traffic_reason = (
        "TRAFFIC_MODEL_NOT_IMPLEMENTED"
        if _TRAFFIC_CHANNELS.issubset(available)
        else "CARIDX_ARRAYS_ABSENT"
    )
    tire_reason = (
        "WEAR_IS_REMOVED_SET_PIT_SNAPSHOT"
        if _TIRE_WEAR_CHANNELS & available
        else "TIRE_WEAR_CHANNELS_ABSENT"
    )
    capabilities = {
        "current_tire_wear": unavailable_inference_capability(
            reasons=(tire_reason,),
            blocked_claims=("CURRENT_TIRE_WEAR_CLAIM",),
        ),
        "driving_analysis_smoke": driving_capability,
        "fuel_model_smoke": fuel_capability,
        "personalized_coaching": _capability(
            "SKIP", ("CONDITION_COHORT_NOT_ATTACHED", "TRAFFIC_UNOBSERVABLE")
        ),
        "opponent_fuel": unavailable_inference_capability(
            reasons=("OPPONENT_FUEL_NOT_EXPOSED_BY_SDK",),
            blocked_claims=("OPPONENT_FUEL_CLAIM",),
        ),
        "race_recommendation": _capability(
            "BLOCKED",
            (
                "OFFLINE_SHADOW_MODE",
                "EVENT_RULES_PROFILE_MISSING",
                traffic_reason,
            ),
        ),
        "traffic_model": unavailable_inference_capability(
            reasons=(traffic_reason,),
            blocked_claims=("REJOIN_TRAFFIC_CLAIM",),
        ),
    }
    suppressions = [
        {
            "blocks": ["CURRENT_TIRE_WEAR_CLAIM"],
            "code": tire_reason,
            "scope": "tires",
        },
        {
            "blocks": ["PERSONALIZED_ACTION", "CURB_RECOMMENDATION"],
            "code": "CONDITION_COHORT_NOT_ATTACHED",
            "scope": "driving",
        },
        {
            "blocks": ["OPPONENT_FUEL_CLAIM"],
            "code": "OPPONENT_FUEL_NOT_EXPOSED_BY_SDK",
            "scope": "opponents",
        },
        {
            "blocks": ["PRODUCTION_PIT_COMMAND", "REJOIN_TRAFFIC_CLAIM"],
            "code": traffic_reason,
            "scope": "strategy",
        },
        {
            "blocks": ["PRODUCTION_PIT_COMMAND", "SPEECH"],
            "code": "OFFLINE_SHADOW_MODE",
            "scope": "strategy",
        },
    ]
    config = {
        "analyses": list(requested),
        "fuel_scenario": fuel_scenario.to_dict() if fuel_scenario else None,
        "grid_step_mm": round(grid_step_m * 1000.0),
        "top": top,
    }
    payload: dict[str, Any] = {
        "capabilities": capabilities,
        "config": config,
        "context": {
            "category": quality.session_context.get("category"),
            "event_type": quality.session_context.get("event_type"),
            "track_config_name": quality.session_context.get("track_config_name"),
            "track_length_m": track_length_m,
            "track_name": quality.session_context.get("track_name"),
        },
        "contract_version": SHADOW_REPORT_CONTRACT_VERSION,
        "estimates": sorted(estimates, key=lambda item: item["estimate_id"]),
        "execution_mode": "SHADOW",
        "facts": sorted(facts, key=lambda item: item["fact_id"]),
        "model_outputs": model_outputs,
        "model_versions": {
            "driving": DRIVING_ALGORITHM_VERSION,
            "fuel": FUEL_MODEL_VERSION,
        },
        "parent_replay": _parent_replay(parent),
        "recommendations": sorted(
            recommendations, key=lambda item: item["recommendation_id"]
        ),
        "source": {
            "canonical_schema_sha256": quality.metadata.canonical_schema_sha256,
            "reader_contract_version": quality.metadata.reader_contract_version,
            "source_mode": "IBT",
            "source_sha256": quality.source_sha256,
        },
        "suppressions": sorted(
            suppressions, key=lambda item: (item["scope"], item["code"])
        ),
    }
    payload["receipt"] = {
        "capabilities_sha256": _digest(payload["capabilities"]),
        "config_sha256": _digest(config),
        "estimates_sha256": _digest(payload["estimates"]),
        "facts_sha256": _digest(payload["facts"]),
        "model_outputs_sha256": _digest(model_outputs),
        "recommendations_sha256": _digest(payload["recommendations"]),
        "suppressions_sha256": _digest(payload["suppressions"]),
        "analysis_sha256": _digest(payload),
    }
    return payload
