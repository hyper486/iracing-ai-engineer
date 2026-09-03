"""Finalize and independently replay one retrieved SDK-live analysis bundle.

The producer-side :mod:`live_engineer_session` receipt is deliberately a
WAIT-only proof.  This module keeps that proof immutable and uses it as the
source-authenticity boundary for a separate, local, advisor-only analysis.

The analysis profile contains user/event configuration only.  Current fuel and
the decision clock always come from the object-exactly replayed SDK-live
capture.  A distance horizon is taken from SDK telemetry when available and
can otherwise fall back to one explicitly hashed user-rule value.  Missing
rules, calibration, traffic, penalty, or driving evidence remains a visible
WAIT; this layer never promotes those capabilities by configuration alone.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from statistics import median
from typing import NoReturn

from .engineer_session import (
    EngineerSessionError,
    _collector_snapshot_opener,
    build_engineer_session_from_collector_snapshot,
    canonical_sha256,
    validate_engineer_session,
    write_engineer_session_exclusive,
)
from .fuel import FuelScenario
from .live_engineer_session import (
    LiveEngineerSessionError,
    replay_retrieved_live_engineer_session,
    validate_live_engineer_session,
)
from .session_report import (
    EngineerSessionReportError,
    build_engineer_session_report,
    render_engineer_session_report_html,
    validate_engineer_session_report,
    write_engineer_session_report_bundle_exclusive,
)
from .telemetry import Presence, Provenance

RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION = (
    "retrieved-live-analysis-profile-v1"
)
RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION = (
    "retrieved-live-analysis-bundle-v1"
)
MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION = (
    "matched-pit-calibration-dataset-v1"
)
MATCHED_PIT_CALIBRATION_METHOD_VERSION = "matched-pit-service-median-v1"
MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION = (
    "matched-tire-performance-dataset-v1"
)
TIRE_PERFORMANCE_MODEL_CONTRACT_VERSION = "tire-performance-model-v1"
TIRE_PERFORMANCE_METHOD_VERSION = "fuel-adjusted-disjoint-pair-envelope-v1"
TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION = "tire-performance-belief-v1"
TIRE_PERFORMANCE_BELIEF_METHOD_VERSION = "linear-age-service-tradeoff-v1"
TIRE_STINT_CONTEXT_CONTRACT_VERSION = "tire-stint-context-v1"
TRAFFIC_MOTION_CONTEXT_CONTRACT_VERSION = "traffic-motion-context-v1"
TIME_DOMAIN_REJOIN_ESTIMATE_CONTRACT_VERSION = "time-domain-rejoin-estimate-v1"
TIME_DOMAIN_REJOIN_METHOD_VERSION = "relative-progress-envelope-v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CALIBRATION_MAX_INPUT_BYTES = 16 * 1024 * 1024
_CALIBRATION_DATASET_KEYS = frozenset(
    {
        "contract_version",
        "dataset_id",
        "dataset_sha256",
        "dataset_version",
        "event_identity",
        "samples",
    }
)
_CALIBRATION_IDENTITY_KEYS = frozenset(
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
_CALIBRATION_SAMPLE_KEYS = frozenset(
    {
        "fuel_delivered_l",
        "fuel_service_elapsed_s",
        "label_receipt_sha256",
        "matched_track_segment_elapsed_s",
        "pit_road_elapsed_s",
        "sample_id",
        "source_receipt_sha256",
        "stationary_service_elapsed_s",
        "tire_change_elapsed_s",
    }
)
_CALIBRATION_MODEL_KEYS = frozenset(
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
_CALIBRATION_IDENTITY_PROVENANCE = frozenset(
    {
        "CONTRACT_FIXTURE",
        "SDK_DIRECT_SAME_SOURCE_CAPTURE",
        "SDK_DIRECT_SAME_SOURCE_SESSION_INFO",
    }
)
_TIRE_PERFORMANCE_DATASET_KEYS = frozenset(
    {
        "contract_version",
        "dataset_id",
        "dataset_sha256",
        "dataset_version",
        "event_identity",
        "fuel_load_model",
        "samples",
        "tire_compound",
    }
)
_TIRE_FUEL_LOAD_MODEL_KEYS = frozenset(
    {
        "model_sha256",
        "seconds_per_liter",
        "seconds_per_liter_uncertainty",
        "source_receipt_sha256",
        "status",
    }
)
_TIRE_PERFORMANCE_SAMPLE_KEYS = frozenset(
    {
        "condition_match_receipt_sha256",
        "early_lap",
        "label_receipt_sha256",
        "late_lap",
        "sample_id",
        "source_receipt_sha256",
        "stint_id",
    }
)
_TIRE_PERFORMANCE_LAP_KEYS = frozenset(
    {"fuel_start_l", "lap_id", "lap_time_s", "stint_age_laps"}
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
_TIRE_PHYSICAL_WEAR_KEYS = frozenset(
    {
        "estimate_available",
        "measured_current_set",
        "provenance",
        "reason_codes",
        "status",
    }
)
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
_TRAFFIC_MOTION_WINDOW_S = 10.0
_TRAFFIC_MOTION_MIN_WINDOW_S = 2.0
_TRAFFIC_MOTION_MIN_POINTS = 5
_TRAFFIC_MOTION_MAX_RATE_LAPS_PER_S = 0.2
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
_REJOIN_ESTIMATE_KEYS = frozenset(
    {
        "calibration_model_sha256",
        "contract_version",
        "decision_tick",
        "estimate_available",
        "estimate_sha256",
        "identity_sha256",
        "method_version",
        "motion_context_sha256",
        "nearest_ahead",
        "nearest_behind",
        "reason_codes",
        "service_scenario",
        "source_receipt_sha256",
        "status",
    }
)
_REJOIN_NEIGHBOR_KEYS = frozenset({"car_idx", "gap_range_s"})
_REJOIN_SERVICE_KEYS = frozenset(
    {
        "change_tires",
        "fuel_add_l",
        "fuel_tire_service_timing",
        "stationary_service_s",
        "total_pit_loss_range_s",
    }
)
_PROFILE_KEYS = frozenset(
    {
        "analysis_profile_sha256",
        "contract_version",
        "fuel_model",
        "horizon_fallback",
        "profile_id",
        "profile_version",
    }
)
_FUEL_MODEL_KEYS = frozenset(
    {
        "conservative_quantile",
        "minimum_valid_laps",
        "refuel_rate_l_per_s",
        "reserve_l",
        "tank_capacity_l",
        "timed_race_extra_laps",
    }
)
_HORIZON_FALLBACK_KEYS = frozenset(
    {"reference_lap_time_s", "remaining_laps", "remaining_time_s"}
)
_BUNDLE_KEYS = frozenset(
    {
        "advisor_only",
        "analysis_profile_binding",
        "bundle_receipt_sha256",
        "capture_binding",
        "contract_version",
        "engineer_session_binding",
        "horizon_binding",
        "readiness",
        "report_binding",
        "rules_binding",
        "safety",
        "source_binding",
        "status",
    }
)
_PROFILE_BINDING_KEYS = frozenset(
    {"contract_version", "profile_id", "profile_sha256", "profile_version"}
)
_CAPTURE_BINDING_KEYS = frozenset(
    {
        "capture_byte_size",
        "capture_sha256",
        "live_analysis_authority_sha256",
        "live_engineer_session_sha256",
        "observed_live_evidence_sha256",
    }
)
_ENGINEER_BINDING_KEYS = frozenset(
    {
        "byte_size",
        "component_hashes",
        "contract_version",
        "file_sha256",
        "semantic_hashes",
        "session_sha256",
        "status",
    }
)
_HORIZON_BINDING_KEYS = frozenset(
    {"fuel_scenario_sha256", "source", "strategy_context_sha256"}
)
_READINESS_KEYS = frozenset(
    {
        "blocker_codes",
        "driving_practice_available",
        "practice_action_count",
        "report_status",
        "strategy_advice_available",
        "strategy_recommendation_count",
    }
)
_REPORT_BINDING_KEYS = frozenset(
    {
        "artifact_byte_size",
        "artifact_file_sha256",
        "contract_version",
        "html_byte_size",
        "html_file_sha256",
        "report_sha256",
    }
)
_RULES_BINDING_KEYS = frozenset(
    {
        "expected_profile_sha256",
        "expected_source_document_sha256",
        "official_event_rules",
        "profile_present",
        "status",
    }
)
_SOURCE_BINDING_KEYS = frozenset(
    {
        "event_receipt_sha256",
        "input_evidence_sha256",
        "input_lineage_sha256",
        "normalized_samples_sha256",
        "records_sha256",
        "sample_count",
        "session_id",
        "source_id",
        "source_kind",
    }
)
_COMPONENT_HASH_KEYS = frozenset(
    {
        "advisor_timeline",
        "corner_cards",
        "driving_diagnosis",
        "driving_replay",
        "fuel_replay",
        "m1_pit_stint",
        "m2_strategy",
    }
)
_SEMANTIC_HASH_KEYS = frozenset(
    {
        "driving_model_semantic_sha256",
        "fuel_model_semantic_sha256",
        "m1_pit_stint_semantic_sha256",
        "source_neutral_sha256",
    }
)
_SAFETY = {
    "audio_emitted": False,
    "executable": False,
    "network_accessed": False,
    "pit_black_box_control_enabled": False,
    "vehicle_control_enabled": False,
}
_BUNDLE_STATUSES = frozenset(
    {
        "ADVICE_READY_BOTH",
        "DRIVING_ADVICE_READY_STRATEGY_WAIT",
        "EVIDENCE_ONLY_WAIT_ADVICE",
        "STRATEGY_ADVICE_READY_DRIVING_WAIT",
        "WAIT_ADVICE",
    }
)
_HORIZON_SOURCES = frozenset(
    {"SDK_DIRECT_LAPS", "SDK_DIRECT_TIME", "USER_RULE_LAPS", "USER_RULE_TIME"}
)
_ACTIVE_PENALTY_FLAG_MASK = 0x00330000


class RetrievedLiveAnalysisError(ValueError):
    """Fail-closed error for retrieved live analysis finalization."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PitCalibrationError(ValueError):
    """Raised when matched calibration evidence fails closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TirePerformanceError(ValueError):
    """Raised when tire-performance evidence or projection fails closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise RetrievedLiveAnalysisError(code, message)


def _mapping(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        _fail("SCHEMA_INVALID", f"{name} must be a JSON object")
    return value


def _exact(value: object, keys: frozenset[str], name: str) -> dict[str, object]:
    result = _mapping(value, name)
    if set(result) != keys:
        _fail("SCHEMA_INVALID", f"{name} keys are invalid")
    return result


def _sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail("SCHEMA_INVALID", f"{name} must be a lowercase SHA-256 digest")
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
        _fail("SCHEMA_INVALID", f"{name} must be an integer >= {minimum}")
    return value


def _finite_number(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("SCHEMA_INVALID", f"{name} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        _fail("SCHEMA_INVALID", f"{name} must be finite")
    if positive and converted <= 0.0:
        _fail("SCHEMA_INVALID", f"{name} must be positive")
    if minimum is not None and converted < minimum:
        _fail("SCHEMA_INVALID", f"{name} must be >= {minimum}")
    return converted


def _optional_number(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    positive: bool = False,
) -> float | None:
    if value is None:
        return None
    return _finite_number(value, name, minimum=minimum, positive=positive)


def _optional_int(value: object, name: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return _plain_int(value, name, minimum=minimum)


def _json_copy(value: object, name: str) -> dict[str, object]:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise RetrievedLiveAnalysisError(
            "SCHEMA_INVALID", f"{name} is not stable JSON"
        ) from exc
    return _mapping(decoded, name)


def _persisted_json(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise RetrievedLiveAnalysisError(
            "OUTPUT_SERIALIZATION_FAILED", "analysis artifact is not stable JSON"
        ) from exc


def _strict_json_bytes(payload: bytes, name: str) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise RetrievedLiveAnalysisError(
            "ARTIFACT_INVALID", f"{name} is not strict UTF-8 JSON"
        ) from exc
    result = _mapping(value, name)
    if _persisted_json(result) != payload:
        _fail("ARTIFACT_INVALID", f"{name} bytes are not canonical persisted JSON")
    return result


def validate_retrieved_live_analysis_profile(
    value: object,
    *,
    expected_analysis_profile_sha256: str,
) -> dict[str, object]:
    """Validate one exact, independently bound user/event analysis profile."""

    profile = _exact(
        _json_copy(value, "analysis profile"),
        _PROFILE_KEYS,
        "analysis profile",
    )
    if profile.get("contract_version") != (
        RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION
    ):
        _fail("PROFILE_INVALID", "analysis profile contract is unsupported")
    _identifier(profile.get("profile_id"), "analysis profile id")
    _plain_int(profile.get("profile_version"), "analysis profile version", minimum=1)
    stored = _sha256(
        profile.get("analysis_profile_sha256"), "analysis profile SHA-256"
    )
    expected = _sha256(
        expected_analysis_profile_sha256,
        "expected analysis profile SHA-256",
    )
    if stored != expected:
        _fail("PROFILE_INVALID", "analysis profile differs from independent digest")
    material = {
        key: item
        for key, item in profile.items()
        if key != "analysis_profile_sha256"
    }
    if canonical_sha256(material) != stored:
        _fail("PROFILE_INVALID", "analysis profile self hash differs")

    fuel = _exact(profile.get("fuel_model"), _FUEL_MODEL_KEYS, "fuel model profile")
    tank = _finite_number(
        fuel.get("tank_capacity_l"), "tank capacity", positive=True
    )
    reserve = _finite_number(fuel.get("reserve_l"), "fuel reserve", minimum=0.0)
    if reserve >= tank:
        _fail("PROFILE_INVALID", "fuel reserve must be below tank capacity")
    _finite_number(
        fuel.get("refuel_rate_l_per_s"), "refuel rate", positive=True
    )
    quantile = _finite_number(
        fuel.get("conservative_quantile"), "fuel quantile"
    )
    if not 0.5 <= quantile <= 1.0:
        _fail("PROFILE_INVALID", "fuel quantile must be between 0.5 and 1.0")
    _plain_int(
        fuel.get("minimum_valid_laps"), "minimum valid fuel laps", minimum=2
    )
    _plain_int(
        fuel.get("timed_race_extra_laps"),
        "timed race extra laps",
    )

    fallback = _exact(
        profile.get("horizon_fallback"),
        _HORIZON_FALLBACK_KEYS,
        "horizon fallback",
    )
    laps = _optional_int(fallback.get("remaining_laps"), "fallback remaining laps")
    timed = _optional_number(
        fallback.get("remaining_time_s"),
        "fallback remaining time",
        minimum=0.0,
    )
    _optional_number(
        fallback.get("reference_lap_time_s"),
        "fallback reference lap time",
        positive=True,
    )
    if laps is not None and timed is not None:
        _fail("PROFILE_INVALID", "horizon fallback may select at most one race kind")
    return profile


def build_retrieved_live_analysis_profile(
    *,
    profile_id: str,
    profile_version: int,
    tank_capacity_l: float,
    refuel_rate_l_per_s: float,
    reserve_l: float = 1.0,
    conservative_quantile: float = 0.9,
    minimum_valid_laps: int = 5,
    timed_race_extra_laps: int = 1,
    remaining_laps: int | None = None,
    remaining_time_s: float | None = None,
    reference_lap_time_s: float | None = None,
) -> dict[str, object]:
    """Build one canonical profile; no current telemetry value is accepted."""

    material: dict[str, object] = {
        "contract_version": RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION,
        "fuel_model": {
            "conservative_quantile": conservative_quantile,
            "minimum_valid_laps": minimum_valid_laps,
            "refuel_rate_l_per_s": refuel_rate_l_per_s,
            "reserve_l": reserve_l,
            "tank_capacity_l": tank_capacity_l,
            "timed_race_extra_laps": timed_race_extra_laps,
        },
        "horizon_fallback": {
            "reference_lap_time_s": reference_lap_time_s,
            "remaining_laps": remaining_laps,
            "remaining_time_s": remaining_time_s,
        },
        "profile_id": profile_id,
        "profile_version": profile_version,
    }
    profile = {**material, "analysis_profile_sha256": canonical_sha256(material)}
    return validate_retrieved_live_analysis_profile(
        profile,
        expected_analysis_profile_sha256=str(profile["analysis_profile_sha256"]),
    )


def _observed_available(value: object, name: str) -> object | None:
    field = _mapping(value, name)
    if field.get("availability") != "AVAILABLE":
        return None
    return field.get("value")


def _resolve_scenario(
    observed: Mapping[str, object],
    profile: Mapping[str, object],
) -> tuple[FuelScenario, dict[str, object]]:
    fuel_field = _mapping(observed.get("current_fuel"), "observed current fuel")
    current_raw = _observed_available(fuel_field, "observed current fuel")
    if current_raw is None:
        _fail(
            "CURRENT_FUEL_UNAVAILABLE",
            "SDK-live current fuel is not available; user configuration cannot replace it",
        )
    current = _finite_number(current_raw, "observed current fuel", minimum=0.0)

    model = _mapping(profile.get("fuel_model"), "fuel model profile")
    fallback = _mapping(profile.get("horizon_fallback"), "horizon fallback")
    horizon = _mapping(observed.get("horizon"), "observed horizon")
    observed_laps = _observed_available(
        horizon.get("laps_remaining"), "observed laps remaining"
    )
    observed_time = _observed_available(
        horizon.get("time_remaining_s"), "observed time remaining"
    )
    reference_lap_time = _optional_number(
        fallback.get("reference_lap_time_s"),
        "fallback reference lap time",
        positive=True,
    )
    if observed_laps is not None:
        remaining_laps = _plain_int(observed_laps, "observed remaining laps")
        remaining_time_s = None
        source = "SDK_DIRECT_LAPS"
        context_reference = None
    elif observed_time is not None:
        remaining_laps = None
        remaining_time_s = _finite_number(
            observed_time, "observed remaining time", minimum=0.0
        )
        source = "SDK_DIRECT_TIME"
        context_reference = reference_lap_time
    else:
        fallback_laps = _optional_int(
            fallback.get("remaining_laps"), "fallback remaining laps"
        )
        fallback_time = _optional_number(
            fallback.get("remaining_time_s"),
            "fallback remaining time",
            minimum=0.0,
        )
        if fallback_laps is not None:
            remaining_laps = fallback_laps
            remaining_time_s = None
            source = "USER_RULE_LAPS"
            context_reference = None
        elif fallback_time is not None:
            remaining_laps = None
            remaining_time_s = fallback_time
            source = "USER_RULE_TIME"
            context_reference = reference_lap_time
        else:
            _fail(
                "HORIZON_UNAVAILABLE",
                "SDK-live horizon is unavailable and the profile has no fallback",
            )

    scenario = FuelScenario(
        current_fuel_l=current,
        tank_capacity_l=float(model["tank_capacity_l"]),
        refuel_rate_l_per_s=float(model["refuel_rate_l_per_s"]),
        remaining_laps=remaining_laps,
        remaining_time_s=remaining_time_s,
        reference_lap_time_s=context_reference,
        reserve_l=float(model["reserve_l"]),
        conservative_quantile=float(model["conservative_quantile"]),
        minimum_valid_laps=int(model["minimum_valid_laps"]),
        timed_race_extra_laps=int(model["timed_race_extra_laps"]),
        # The scenario mixes SDK-direct values with a user profile.  The
        # conservative aggregate label must therefore remain USER_RULE.
        provenance="USER_RULE",
        provenance_overrides=(
            ("current_fuel_l", "SDK_DIRECT"),
            (
                "remaining_laps" if remaining_laps is not None else "remaining_time_s",
                "SDK_DIRECT" if source.startswith("SDK_DIRECT_") else "USER_RULE",
            ),
        ),
    )
    context_inputs = {
        "horizon_kind": "LAPS" if remaining_laps is not None else "TIMED",
        "horizon_provenance": (
            "SDK_DIRECT" if source.startswith("SDK_DIRECT_") else "USER_RULE"
        ),
        "horizon_source": source,
        "laps_remaining": remaining_laps,
        "reference_lap_time_s": context_reference,
        "time_remaining_s": remaining_time_s,
    }
    return scenario, context_inputs


def _motion_direct_number(
    field: object,
    *,
    integer: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
) -> int | float | None:
    if (
        getattr(field, "presence", None) is not Presence.PRESENT
        or getattr(field, "provenance", None) is not Provenance.SDK_DIRECT
    ):
        return None
    value = getattr(field, "value", None)
    if integer:
        if type(value) is not int:
            return None
        converted: int | float = value
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        converted = float(value)
        if not math.isfinite(converted):
            return None
    if minimum is not None and converted < minimum:
        return None
    if maximum is not None and converted > maximum:
        return None
    return converted


def _motion_direct_bool(field: object) -> bool | None:
    if (
        getattr(field, "presence", None) is not Presence.PRESENT
        or getattr(field, "provenance", None) is not Provenance.SDK_DIRECT
        or type(getattr(field, "value", None)) is not bool
    ):
        return None
    return bool(field.value)


def _tire_stint_point(sample: object) -> tuple[dict[str, object] | None, list[str]]:
    fields = {
        "SESSION_TICK": _motion_direct_number(
            sample.session.session_tick, integer=True, minimum=0.0
        ),
        "LAPS_COMPLETED": _motion_direct_number(
            sample.lap.laps_completed, integer=True, minimum=0.0
        ),
        "TIRE_COMPOUND": _motion_direct_number(
            sample.tires.player_tire_compound, integer=True, minimum=0.0
        ),
        "TIRE_SETS_USED": _motion_direct_number(
            sample.tires.tire_sets_used, integer=True, minimum=0.0
        ),
        "ON_PIT_ROAD": _motion_direct_bool(sample.pit.on_pit_road),
    }
    missing = [f"{name}_UNAVAILABLE" for name, value in fields.items() if value is None]
    if missing:
        return None, missing
    return (
        {
            "decision_tick": int(fields["SESSION_TICK"]),
            "laps_completed": int(fields["LAPS_COMPLETED"]),
            "on_pit_road": bool(fields["ON_PIT_ROAD"]),
            "tire_compound": int(fields["TIRE_COMPOUND"]),
            "tire_sets_used": int(fields["TIRE_SETS_USED"]),
        },
        [],
    )


def _new_tire_stint_tracker() -> dict[str, object]:
    return {
        "gap_since_origin": False,
        "invalid_reasons": [],
        "last_missing_reasons": [],
        "last_point": None,
        "origin": None,
        "previous_point": None,
    }


def _track_tire_stint_sample(tracker: dict[str, object], sample: object) -> None:
    point, missing = _tire_stint_point(sample)
    if point is None:
        tracker["last_point"] = None
        tracker["last_missing_reasons"] = missing
        tracker["previous_point"] = None
        if tracker.get("origin") is not None:
            tracker["gap_since_origin"] = True
        return
    tracker["last_missing_reasons"] = []
    previous = tracker.get("previous_point")
    origin = tracker.get("origin")
    invalid = tracker["invalid_reasons"]
    if not isinstance(invalid, list):  # pragma: no cover - private invariant
        raise AssertionError("tire tracker lost invalid-reason storage")
    if isinstance(previous, Mapping):
        if int(point["decision_tick"]) <= int(previous["decision_tick"]):
            invalid.append("TIRE_STINT_TICK_NOT_MONOTONIC")
        if int(point["laps_completed"]) < int(previous["laps_completed"]):
            invalid.append("TIRE_STINT_LAP_COUNT_REGRESSION")
        if previous["on_pit_road"] is True and point["on_pit_road"] is False:
            origin = {**point, "origin_kind": "OBSERVED_PIT_EXIT"}
            tracker["origin"] = origin
            tracker["gap_since_origin"] = False
    if origin is None and point["on_pit_road"] is False and point["laps_completed"] == 0:
        origin = {**point, "origin_kind": "OBSERVED_ZERO_COMPLETED_LAPS"}
        tracker["origin"] = origin
        tracker["gap_since_origin"] = False
    if isinstance(origin, Mapping) and point["on_pit_road"] is False:
        if int(point["tire_sets_used"]) != int(origin["tire_sets_used"]):
            invalid.append("TIRE_SET_CHANGED_WITHOUT_OBSERVED_PIT_EXIT")
        if int(point["tire_compound"]) != int(origin["tire_compound"]):
            invalid.append("TIRE_COMPOUND_CHANGED_WITHOUT_OBSERVED_PIT_EXIT")
    tracker["last_point"] = point
    tracker["previous_point"] = point


def _build_tire_stint_context(
    tracker: Mapping[str, object],
    *,
    identity_sha256: str,
    source_receipt_sha256: str,
    expected_decision_tick: int,
) -> dict[str, object]:
    last = tracker.get("last_point")
    origin = tracker.get("origin")
    invalid = sorted(set(str(item) for item in tracker.get("invalid_reasons", [])))
    last_missing = sorted(
        set(str(item) for item in tracker.get("last_missing_reasons", []))
    )
    reasons: list[str] = []
    availability = "UNAVAILABLE"
    status = "WAIT_CURRENT_TIRE_CHANNELS"
    age: int | None = None
    if invalid:
        availability = "INVALID"
        status = "INVALID_TIRE_STINT_SEQUENCE"
        reasons = invalid
    elif not isinstance(last, Mapping):
        reasons = last_missing or ["CURRENT_TIRE_CHANNELS_UNAVAILABLE"]
    elif int(last["decision_tick"]) != expected_decision_tick:
        reasons = ["CURRENT_TIRE_CONTEXT_NOT_AT_DECISION_TICK"]
    elif last["on_pit_road"] is True:
        status = "WAIT_PLAYER_ON_PIT_ROAD"
        reasons = ["PLAYER_ON_PIT_ROAD"]
    elif not isinstance(origin, Mapping):
        status = "WAIT_STINT_ORIGIN"
        reasons = ["CURRENT_STINT_ORIGIN_NOT_OBSERVED"]
    elif tracker.get("gap_since_origin") is True:
        status = "WAIT_TIRE_CHANNEL_CONTINUITY"
        reasons = ["TIRE_CHANNEL_GAP_AFTER_STINT_ORIGIN"]
    else:
        age = int(last["laps_completed"]) - int(origin["laps_completed"])
        if age < 0:
            availability = "INVALID"
            status = "INVALID_TIRE_STINT_SEQUENCE"
            reasons = ["CURRENT_STINT_AGE_NEGATIVE"]
            age = None
        else:
            availability = "AVAILABLE"
            status = "AVAILABLE_OBSERVED_STINT_AGE"
    material: dict[str, object] = {
        "availability": availability,
        "contract_version": TIRE_STINT_CONTEXT_CONTRACT_VERSION,
        "current_laps_completed": (
            int(last["laps_completed"]) if isinstance(last, Mapping) else None
        ),
        "current_tire_compound": (
            int(last["tire_compound"]) if isinstance(last, Mapping) else None
        ),
        "decision_tick": expected_decision_tick,
        "identity_sha256": identity_sha256,
        "on_pit_road": bool(last["on_pit_road"]) if isinstance(last, Mapping) else None,
        "origin_kind": str(origin["origin_kind"]) if isinstance(origin, Mapping) else None,
        "origin_laps_completed": (
            int(origin["laps_completed"]) if isinstance(origin, Mapping) else None
        ),
        "origin_tick": (
            int(origin["decision_tick"]) if isinstance(origin, Mapping) else None
        ),
        "physical_wear": dict(_TIRE_PHYSICAL_WEAR_UNAVAILABLE),
        "reason_codes": reasons,
        "source_receipt_sha256": source_receipt_sha256,
        "status": status,
        "stint_age_completed_laps": age,
        "tire_sets_used": (
            int(last["tire_sets_used"]) if isinstance(last, Mapping) else None
        ),
    }
    context = {**material, "context_sha256": canonical_sha256(material)}
    return validate_tire_stint_context(
        context,
        expected_context_sha256=str(context["context_sha256"]),
        expected_identity_sha256=identity_sha256,
        expected_source_receipt_sha256=source_receipt_sha256,
        expected_decision_tick=expected_decision_tick,
    )


def validate_tire_stint_context(
    value: object,
    *,
    expected_context_sha256: str,
    expected_identity_sha256: str,
    expected_source_receipt_sha256: str,
    expected_decision_tick: int,
) -> dict[str, object]:
    """Validate a same-capture current-stint-age observation."""

    context = _exact(
        _json_copy(value, "tire-stint context"),
        _TIRE_STINT_CONTEXT_KEYS,
        "tire-stint context",
    )
    if context.get("contract_version") != TIRE_STINT_CONTEXT_CONTRACT_VERSION:
        _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint contract is unsupported")
    stored = _sha256(context.get("context_sha256"), "tire-stint context SHA-256")
    if stored != _sha256(
        expected_context_sha256, "expected tire-stint context SHA-256"
    ):
        _fail(
            "TIRE_STINT_CONTEXT_INVALID",
            "tire-stint context differs from independent digest",
        )
    material = {key: item for key, item in context.items() if key != "context_sha256"}
    if canonical_sha256(material) != stored:
        _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint context self hash differs")
    for key, expected, label in (
        ("identity_sha256", expected_identity_sha256, "identity"),
        ("source_receipt_sha256", expected_source_receipt_sha256, "source receipt"),
    ):
        if context.get(key) != _sha256(expected, f"expected tire-stint {label}"):
            _fail("TIRE_STINT_CONTEXT_INVALID", f"tire-stint {label} differs")
    if (
        type(expected_decision_tick) is not int
        or expected_decision_tick < 0
        or context.get("decision_tick") != expected_decision_tick
    ):
        _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint decision tick differs")
    physical = _exact(
        context.get("physical_wear"),
        _TIRE_PHYSICAL_WEAR_KEYS,
        "tire-stint physical-wear boundary",
    )
    if physical != _TIRE_PHYSICAL_WEAR_UNAVAILABLE:
        _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint context made a wear claim")
    reasons = context.get("reason_codes")
    if (
        type(reasons) is not list
        or any(type(item) is not str or not item for item in reasons)
        or reasons != sorted(set(reasons))
    ):
        _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint reasons are invalid")
    current_keys = (
        "current_laps_completed",
        "current_tire_compound",
        "on_pit_road",
        "tire_sets_used",
    )
    current_values = [context.get(key) for key in current_keys]
    current_available = all(value is not None for value in current_values)
    if any(value is not None for value in current_values) and not current_available:
        _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint current fields are partial")
    if current_available:
        _plain_int(context.get("current_laps_completed"), "tire-stint current laps")
        _plain_int(context.get("current_tire_compound"), "tire-stint compound")
        _plain_int(context.get("tire_sets_used"), "tire-stint sets used")
        if type(context.get("on_pit_road")) is not bool:
            _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint pit state is invalid")
    origin_keys = ("origin_kind", "origin_laps_completed", "origin_tick")
    origin_values = [context.get(key) for key in origin_keys]
    origin_available = all(value is not None for value in origin_values)
    if any(value is not None for value in origin_values) and not origin_available:
        _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint origin fields are partial")
    origin_laps = 0
    if origin_available:
        if context.get("origin_kind") not in {
            "OBSERVED_PIT_EXIT",
            "OBSERVED_ZERO_COMPLETED_LAPS",
        }:
            _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint origin kind is invalid")
        origin_laps = _plain_int(
            context.get("origin_laps_completed"), "tire-stint origin laps"
        )
        origin_tick = _plain_int(context.get("origin_tick"), "tire-stint origin tick")
        if origin_tick > expected_decision_tick:
            _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint origin is in the future")
    if context.get("availability") == "AVAILABLE":
        if (
            context.get("status") != "AVAILABLE_OBSERVED_STINT_AGE"
            or reasons
            or not current_available
            or not origin_available
            or context.get("on_pit_road") is not False
        ):
            _fail("TIRE_STINT_CONTEXT_INVALID", "available tire-stint context is invalid")
        age = _plain_int(
            context.get("stint_age_completed_laps"), "tire-stint completed age"
        )
        if age != int(context["current_laps_completed"]) - origin_laps:
            _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint age is not origin-derived")
    elif context.get("availability") == "UNAVAILABLE":
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
            _fail("TIRE_STINT_CONTEXT_INVALID", "waiting tire-stint context is invalid")
    elif context.get("availability") == "INVALID":
        if (
            context.get("status") != "INVALID_TIRE_STINT_SEQUENCE"
            or not reasons
            or context.get("stint_age_completed_laps") is not None
        ):
            _fail("TIRE_STINT_CONTEXT_INVALID", "invalid tire-stint context is invalid")
    else:
        _fail("TIRE_STINT_CONTEXT_INVALID", "tire-stint availability is invalid")
    return context


def _traffic_motion_point(sample: object) -> dict[str, object] | None:
    """Return one privacy-safe progress point from SDK-direct fields only."""

    quality = getattr(sample, "quality", None)
    stale = getattr(quality, "stale", None)
    if (
        getattr(stale, "presence", None) is Presence.PRESENT
        and getattr(stale, "value", None) is True
    ):
        return None
    session = getattr(sample, "session", None)
    lap = getattr(sample, "lap", None)
    pit = getattr(sample, "pit", None)
    opponents = getattr(sample, "opponents", None)
    session_time = _motion_direct_number(
        getattr(session, "session_time_s", None), minimum=0.0
    )
    decision_tick = _motion_direct_number(
        getattr(session, "session_tick", None), integer=True, minimum=0.0
    )
    player_laps = _motion_direct_number(
        getattr(lap, "laps_completed", None), integer=True, minimum=0.0
    )
    player_pct = _motion_direct_number(
        getattr(lap, "lap_distance_pct", None), minimum=0.0, maximum=1.0
    )
    player_car_idx = _motion_direct_number(
        getattr(opponents, "player_car_idx", None), integer=True, minimum=0.0
    )
    on_pit_road = _motion_direct_bool(getattr(pit, "on_pit_road", None))
    if (
        session_time is None
        or decision_tick is None
        or player_laps is None
        or player_pct is None
        or player_car_idx is None
        or on_pit_road is None
        or bool(on_pit_road)
        or getattr(opponents, "presence", None) is not Presence.PRESENT
        or getattr(opponents, "provenance", None) is not Provenance.SDK_DIRECT
        or getattr(opponents, "issues", ())
    ):
        return None

    opponent_progress: dict[int, float] = {}
    for opponent in getattr(opponents, "entries", ()):
        car_idx_field = getattr(opponent, "car_idx", None)
        car_idx = getattr(car_idx_field, "value", None)
        if (
            getattr(car_idx_field, "presence", None) is not Presence.PRESENT
            or getattr(car_idx_field, "provenance", None) is not Provenance.DERIVED
            or type(car_idx) is not int
            or car_idx < 0
        ):
            continue
        laps = _motion_direct_number(
            getattr(opponent, "laps_completed", None), integer=True, minimum=0.0
        )
        pct = _motion_direct_number(
            getattr(opponent, "lap_distance_pct", None),
            minimum=0.0,
            maximum=1.0,
        )
        opponent_on_pit = _motion_direct_bool(
            getattr(opponent, "on_pit_road", None)
        )
        surface = _motion_direct_number(
            getattr(opponent, "track_surface", None), integer=True
        )
        if (
            laps is None
            or pct is None
            or opponent_on_pit is None
            or bool(opponent_on_pit)
            or surface != 3
            or car_idx == player_car_idx
        ):
            continue
        opponent_progress[car_idx] = float(laps) + float(pct)
    return {
        "decision_tick": int(decision_tick),
        "opponents": opponent_progress,
        "player_car_idx": int(player_car_idx),
        "player_progress_laps": float(player_laps) + float(player_pct),
        "session_time_s": float(session_time),
    }


def _motion_actor(
    points: list[dict[str, object]],
    *,
    car_idx: int,
    opponent: bool,
) -> dict[str, object] | None:
    progress_points: list[tuple[float, float]] = []
    for point in points:
        time_s = float(point["session_time_s"])
        if opponent:
            opponents = point["opponents"]
            if not isinstance(opponents, dict) or car_idx not in opponents:
                continue
            progress = float(opponents[car_idx])
        else:
            if point["player_car_idx"] != car_idx:
                continue
            progress = float(point["player_progress_laps"])
        progress_points.append((time_s, progress))
    if len(progress_points) < _TRAFFIC_MOTION_MIN_POINTS:
        return None
    last_time, last_progress = progress_points[-1]
    rates: list[float] = []
    for time_s, progress in progress_points[:-1]:
        elapsed = last_time - time_s
        if elapsed < _TRAFFIC_MOTION_MIN_WINDOW_S:
            continue
        rate = (last_progress - progress) / elapsed
        if 0.0 < rate <= _TRAFFIC_MOTION_MAX_RATE_LAPS_PER_S:
            rates.append(rate)
    if len(rates) < 3:
        return None
    low = min(rates)
    high = max(rates)
    middle = float(median(rates))
    return {
        "car_idx": car_idx,
        "point_count": len(progress_points),
        "rate_laps_per_s": round(middle, 9),
        "rate_range_laps_per_s": [round(low, 9), round(high, 9)],
    }


def _unavailable_motion_context(
    *,
    decision_tick: int | None,
    identity_sha256: str,
    reason: str,
    source_receipt_sha256: str,
    traffic_map_revision_sha256: str,
) -> dict[str, object]:
    material: dict[str, object] = {
        "availability": "UNAVAILABLE",
        "contract_version": TRAFFIC_MOTION_CONTEXT_CONTRACT_VERSION,
        "decision_tick": decision_tick,
        "identity_sha256": identity_sha256,
        "observation_window_s": None,
        "opponents": [],
        "player": None,
        "reason_codes": [reason],
        "source_receipt_sha256": source_receipt_sha256,
        "status": "WAIT_TIME_DOMAIN_MOTION",
        "traffic_map_revision_sha256": traffic_map_revision_sha256,
    }
    return {**material, "motion_sha256": canonical_sha256(material)}


def _build_traffic_motion_context(
    points_value: object,
    traffic_context_value: object,
    *,
    identity_sha256: str,
    source_receipt_sha256: str,
) -> dict[str, object]:
    points = list(points_value) if isinstance(points_value, (list, tuple, deque)) else []
    traffic = _mapping(traffic_context_value, "traffic observation context")
    map_revision = _sha256(
        traffic.get("context_sha256"), "traffic observation context SHA-256"
    )
    decision_tick = traffic.get("decision_tick")
    if type(decision_tick) is not int:
        decision_tick = None
    if traffic.get("availability") != "AVAILABLE" or traffic.get("status") != "VERIFIED":
        return _unavailable_motion_context(
            decision_tick=decision_tick,
            identity_sha256=identity_sha256,
            reason="TRAFFIC_MAP_UNAVAILABLE",
            source_receipt_sha256=source_receipt_sha256,
            traffic_map_revision_sha256=map_revision,
        )
    if not points or points[-1].get("decision_tick") != decision_tick:
        return _unavailable_motion_context(
            decision_tick=decision_tick,
            identity_sha256=identity_sha256,
            reason="LATEST_MOTION_POINT_UNAVAILABLE",
            source_receipt_sha256=source_receipt_sha256,
            traffic_map_revision_sha256=map_revision,
        )
    first_time = float(points[0]["session_time_s"])
    last_time = float(points[-1]["session_time_s"])
    window = last_time - first_time
    if window < _TRAFFIC_MOTION_MIN_WINDOW_S:
        return _unavailable_motion_context(
            decision_tick=decision_tick,
            identity_sha256=identity_sha256,
            reason="MOTION_WINDOW_TOO_SHORT",
            source_receipt_sha256=source_receipt_sha256,
            traffic_map_revision_sha256=map_revision,
        )
    player_idx = int(points[-1]["player_car_idx"])
    player = _motion_actor(points, car_idx=player_idx, opponent=False)
    if player is None:
        return _unavailable_motion_context(
            decision_tick=decision_tick,
            identity_sha256=identity_sha256,
            reason="PLAYER_RATE_UNAVAILABLE",
            source_receipt_sha256=source_receipt_sha256,
            traffic_map_revision_sha256=map_revision,
        )
    latest_opponents = points[-1]["opponents"]
    if not isinstance(latest_opponents, dict):
        latest_opponents = {}
    opponent_rows: list[dict[str, object]] = []
    player_progress = float(points[-1]["player_progress_laps"])
    for car_idx in sorted(latest_opponents):
        actor = _motion_actor(points, car_idx=int(car_idx), opponent=True)
        if actor is None:
            continue
        opponent_rows.append(
            {
                **actor,
                "current_signed_lap_delta": round(
                    float(latest_opponents[car_idx]) - player_progress,
                    9,
                ),
            }
        )
    if not opponent_rows:
        return _unavailable_motion_context(
            decision_tick=decision_tick,
            identity_sha256=identity_sha256,
            reason="NO_OPPONENT_RATE_AVAILABLE",
            source_receipt_sha256=source_receipt_sha256,
            traffic_map_revision_sha256=map_revision,
        )
    material = {
        "availability": "AVAILABLE",
        "contract_version": TRAFFIC_MOTION_CONTEXT_CONTRACT_VERSION,
        "decision_tick": decision_tick,
        "identity_sha256": identity_sha256,
        "observation_window_s": round(window, 6),
        "opponents": opponent_rows,
        "player": player,
        "reason_codes": [],
        "source_receipt_sha256": source_receipt_sha256,
        "status": "VERIFIED_TIME_DOMAIN_MOTION",
        "traffic_map_revision_sha256": map_revision,
    }
    result = {**material, "motion_sha256": canonical_sha256(material)}
    return validate_traffic_motion_context(
        result,
        expected_motion_sha256=str(result["motion_sha256"]),
        expected_source_receipt_sha256=source_receipt_sha256,
        expected_traffic_map_revision_sha256=map_revision,
        expected_identity_sha256=identity_sha256,
        expected_decision_tick=decision_tick,
    )


def _validate_motion_actor(
    value: object,
    *,
    opponent: bool,
    label: str,
) -> dict[str, object]:
    actor = _exact(
        value,
        _TRAFFIC_MOTION_OPPONENT_KEYS if opponent else _TRAFFIC_MOTION_ACTOR_KEYS,
        label,
    )
    _plain_int(actor.get("car_idx"), f"{label} car index")
    _plain_int(actor.get("point_count"), f"{label} point count")
    if int(actor["point_count"]) < _TRAFFIC_MOTION_MIN_POINTS:
        _fail("REJOIN_MOTION_INVALID", f"{label} has too few motion points")
    rate = _finite_number(actor.get("rate_laps_per_s"), f"{label} rate", positive=True)
    if rate > _TRAFFIC_MOTION_MAX_RATE_LAPS_PER_S:
        _fail("REJOIN_MOTION_INVALID", f"{label} rate exceeds sanity ceiling")
    bounds = actor.get("rate_range_laps_per_s")
    if type(bounds) is not list or len(bounds) != 2:
        _fail("REJOIN_MOTION_INVALID", f"{label} rate range is invalid")
    low = _finite_number(bounds[0], f"{label} rate low", positive=True)
    high = _finite_number(bounds[1], f"{label} rate high", positive=True)
    if not low <= rate <= high <= _TRAFFIC_MOTION_MAX_RATE_LAPS_PER_S:
        _fail("REJOIN_MOTION_INVALID", f"{label} rate range is inconsistent")
    if opponent:
        _finite_number(
            actor.get("current_signed_lap_delta"),
            f"{label} signed lap delta",
        )
    return actor


def validate_traffic_motion_context(
    value: object,
    *,
    expected_motion_sha256: str,
    expected_source_receipt_sha256: str,
    expected_traffic_map_revision_sha256: str,
    expected_identity_sha256: str,
    expected_decision_tick: int,
) -> dict[str, object]:
    """Validate one short-window, same-capture traffic motion receipt."""

    context = _exact(
        _json_copy(value, "traffic motion context"),
        _TRAFFIC_MOTION_KEYS,
        "traffic motion context",
    )
    if context.get("contract_version") != TRAFFIC_MOTION_CONTEXT_CONTRACT_VERSION:
        _fail("REJOIN_MOTION_INVALID", "traffic motion contract is unsupported")
    stored = _sha256(context.get("motion_sha256"), "traffic motion SHA-256")
    if stored != _sha256(expected_motion_sha256, "expected traffic motion SHA-256"):
        _fail("REJOIN_MOTION_INVALID", "traffic motion differs from independent digest")
    material = {key: item for key, item in context.items() if key != "motion_sha256"}
    if canonical_sha256(material) != stored:
        _fail("REJOIN_MOTION_INVALID", "traffic motion self hash differs")
    if context.get("identity_sha256") != _sha256(
        expected_identity_sha256, "expected traffic motion identity"
    ):
        _fail("REJOIN_MOTION_INVALID", "traffic motion identity differs")
    if context.get("source_receipt_sha256") != _sha256(
        expected_source_receipt_sha256, "expected traffic motion source receipt"
    ):
        _fail("REJOIN_MOTION_INVALID", "traffic motion source differs")
    if context.get("traffic_map_revision_sha256") != _sha256(
        expected_traffic_map_revision_sha256,
        "expected traffic map revision",
    ):
        _fail("REJOIN_MOTION_INVALID", "traffic motion map revision differs")
    if context.get("decision_tick") != _plain_int(
        expected_decision_tick, "expected traffic motion decision tick"
    ):
        _fail("REJOIN_MOTION_INVALID", "traffic motion decision tick differs")
    reasons = context.get("reason_codes")
    opponents = context.get("opponents")
    if type(reasons) is not list or any(type(item) is not str or not item for item in reasons):
        _fail("REJOIN_MOTION_INVALID", "traffic motion reasons are invalid")
    if len(reasons) != len(set(reasons)) or type(opponents) is not list:
        _fail("REJOIN_MOTION_INVALID", "traffic motion arrays are invalid")
    if context.get("availability") == "AVAILABLE":
        if context.get("status") != "VERIFIED_TIME_DOMAIN_MOTION" or reasons:
            _fail("REJOIN_MOTION_INVALID", "available traffic motion status is invalid")
        window = _finite_number(
            context.get("observation_window_s"),
            "traffic motion observation window",
            positive=True,
        )
        if window < _TRAFFIC_MOTION_MIN_WINDOW_S:
            _fail("REJOIN_MOTION_INVALID", "traffic motion window is too short")
        player = _validate_motion_actor(
            context.get("player"), opponent=False, label="traffic motion player"
        )
        if not opponents:
            _fail("REJOIN_MOTION_INVALID", "traffic motion lacks opponents")
        seen: list[int] = []
        for index, item in enumerate(opponents):
            opponent_row = _validate_motion_actor(
                item,
                opponent=True,
                label=f"traffic motion opponent {index}",
            )
            car_idx = int(opponent_row["car_idx"])
            if car_idx == player["car_idx"]:
                _fail("REJOIN_MOTION_INVALID", "traffic motion includes the player as opponent")
            seen.append(car_idx)
        if seen != sorted(set(seen)):
            _fail("REJOIN_MOTION_INVALID", "traffic motion opponents are not unique and sorted")
    elif context.get("availability") == "UNAVAILABLE":
        if (
            context.get("status") != "WAIT_TIME_DOMAIN_MOTION"
            or not reasons
            or context.get("observation_window_s") is not None
            or context.get("player") is not None
            or opponents
        ):
            _fail("REJOIN_MOTION_INVALID", "unavailable traffic motion boundary is invalid")
    else:
        _fail("REJOIN_MOTION_INVALID", "traffic motion availability is invalid")
    return context


def _nearest_stable_neighbor(
    candidates: list[dict[str, object]],
) -> tuple[dict[str, object] | None, bool]:
    if not candidates:
        return None, True
    ordered = sorted(
        candidates,
        key=lambda item: (
            (float(item["gap_range_s"][0]) + float(item["gap_range_s"][1])) / 2.0,
            int(item["car_idx"]),
        ),
    )
    winner = ordered[0]
    winner_high = float(winner["gap_range_s"][1])
    if any(winner_high >= float(item["gap_range_s"][0]) for item in ordered[1:]):
        return None, False
    return winner, True


def validate_time_domain_rejoin_estimate(
    value: object,
    *,
    expected_estimate_sha256: str,
    expected_identity_sha256: str,
    expected_motion_sha256: str,
    expected_calibration_model_sha256: str,
) -> dict[str, object]:
    estimate = _exact(
        _json_copy(value, "time-domain rejoin estimate"),
        _REJOIN_ESTIMATE_KEYS,
        "time-domain rejoin estimate",
    )
    if estimate.get("contract_version") != TIME_DOMAIN_REJOIN_ESTIMATE_CONTRACT_VERSION:
        _fail("REJOIN_ESTIMATE_INVALID", "rejoin estimate contract is unsupported")
    if estimate.get("method_version") != TIME_DOMAIN_REJOIN_METHOD_VERSION:
        _fail("REJOIN_ESTIMATE_INVALID", "rejoin estimate method is unsupported")
    stored = _sha256(estimate.get("estimate_sha256"), "rejoin estimate SHA-256")
    if stored != _sha256(expected_estimate_sha256, "expected rejoin estimate SHA-256"):
        _fail("REJOIN_ESTIMATE_INVALID", "rejoin estimate differs from independent digest")
    material = {key: item for key, item in estimate.items() if key != "estimate_sha256"}
    if canonical_sha256(material) != stored:
        _fail("REJOIN_ESTIMATE_INVALID", "rejoin estimate self hash differs")
    for key, expected, label in (
        ("identity_sha256", expected_identity_sha256, "identity"),
        ("motion_context_sha256", expected_motion_sha256, "motion context"),
        (
            "calibration_model_sha256",
            expected_calibration_model_sha256,
            "calibration model",
        ),
    ):
        if estimate.get(key) != _sha256(expected, f"expected rejoin {label}"):
            _fail("REJOIN_ESTIMATE_INVALID", f"rejoin estimate {label} differs")
    _plain_int(estimate.get("decision_tick"), "rejoin estimate decision tick")
    _sha256(estimate.get("source_receipt_sha256"), "rejoin source receipt")
    reasons = estimate.get("reason_codes")
    if type(reasons) is not list or any(type(item) is not str or not item for item in reasons):
        _fail("REJOIN_ESTIMATE_INVALID", "rejoin estimate reasons are invalid")
    service = _exact(
        estimate.get("service_scenario"),
        _REJOIN_SERVICE_KEYS,
        "rejoin service scenario",
    )
    if type(service.get("change_tires")) is not bool:
        _fail("REJOIN_ESTIMATE_INVALID", "rejoin tire choice is invalid")
    _finite_number(service.get("fuel_add_l"), "rejoin fuel addition", minimum=0.0)
    _finite_number(
        service.get("stationary_service_s"), "rejoin stationary service", minimum=0.0
    )
    if service.get("fuel_tire_service_timing") not in {"PARALLEL", "SEQUENTIAL"}:
        _fail("REJOIN_ESTIMATE_INVALID", "rejoin service timing is invalid")
    loss = service.get("total_pit_loss_range_s")
    if type(loss) is not list or len(loss) != 2:
        _fail("REJOIN_ESTIMATE_INVALID", "rejoin pit-loss range is invalid")
    loss_low = _finite_number(loss[0], "rejoin pit-loss low", minimum=0.0)
    loss_high = _finite_number(loss[1], "rejoin pit-loss high", minimum=0.0)
    if loss_high < loss_low:
        _fail("REJOIN_ESTIMATE_INVALID", "rejoin pit-loss range is reversed")
    for name in ("nearest_ahead", "nearest_behind"):
        neighbor = estimate.get(name)
        if neighbor is None:
            continue
        row = _exact(neighbor, _REJOIN_NEIGHBOR_KEYS, f"rejoin {name}")
        _plain_int(row.get("car_idx"), f"rejoin {name} car index")
        gap = row.get("gap_range_s")
        if type(gap) is not list or len(gap) != 2:
            _fail("REJOIN_ESTIMATE_INVALID", f"rejoin {name} gap range is invalid")
        low = _finite_number(gap[0], f"rejoin {name} gap low", minimum=0.0)
        high = _finite_number(gap[1], f"rejoin {name} gap high", minimum=0.0)
        if high < low:
            _fail("REJOIN_ESTIMATE_INVALID", f"rejoin {name} gap range is reversed")
    if estimate.get("estimate_available") is True:
        if (
            estimate.get("status") != "AVAILABLE_STABLE_BRACKET"
            or reasons
            or (
                estimate.get("nearest_ahead") is None
                and estimate.get("nearest_behind") is None
            )
        ):
            _fail("REJOIN_ESTIMATE_INVALID", "available rejoin estimate is inconsistent")
    elif estimate.get("estimate_available") is False:
        if (
            estimate.get("status") != "WAIT_AMBIGUOUS_REJOIN_ORDER"
            or not reasons
            or estimate.get("nearest_ahead") is not None
            or estimate.get("nearest_behind") is not None
        ):
            _fail("REJOIN_ESTIMATE_INVALID", "waiting rejoin estimate is inconsistent")
    else:
        _fail("REJOIN_ESTIMATE_INVALID", "rejoin estimate availability is invalid")
    return estimate


def build_time_domain_rejoin_estimate(
    motion_context_value: object,
    calibration_model_value: object,
    *,
    expected_motion_sha256: str,
    expected_motion_source_receipt_sha256: str,
    expected_traffic_map_revision_sha256: str,
    expected_calibration_model_sha256: str,
    expected_calibration_source_receipt_sha256: str,
    expected_identity_sha256: str,
    expected_decision_tick: int,
    fuel_add_l: float,
    change_tires: bool,
    fuel_tire_service_timing: str,
) -> dict[str, object]:
    """Project a pit-action-specific rejoin bracket from same-capture motion."""

    motion = validate_traffic_motion_context(
        motion_context_value,
        expected_motion_sha256=expected_motion_sha256,
        expected_source_receipt_sha256=expected_motion_source_receipt_sha256,
        expected_traffic_map_revision_sha256=expected_traffic_map_revision_sha256,
        expected_identity_sha256=expected_identity_sha256,
        expected_decision_tick=expected_decision_tick,
    )
    if motion.get("availability") != "AVAILABLE":
        _fail("REJOIN_MOTION_UNAVAILABLE", "traffic motion is not available")
    try:
        calibration = validate_matched_pit_calibration_model(
            calibration_model_value,
            expected_model_sha256=expected_calibration_model_sha256,
            expected_identity_sha256=expected_identity_sha256,
            expected_source_receipt_sha256=expected_calibration_source_receipt_sha256,
        )
    except PitCalibrationError as exc:
        raise RetrievedLiveAnalysisError(
            "REJOIN_CALIBRATION_INVALID",
            f"rejoin calibration failed closed: {exc.code}: {exc}",
        ) from exc
    fuel_add = _finite_number(fuel_add_l, "rejoin fuel addition", minimum=0.0)
    if type(change_tires) is not bool:
        _fail("REJOIN_SCENARIO_INVALID", "rejoin tire choice must be boolean")
    if fuel_tire_service_timing not in {"PARALLEL", "SEQUENTIAL"}:
        _fail("REJOIN_SCENARIO_INVALID", "rejoin service timing is unsupported")
    fuel_time = fuel_add / float(calibration["refuel_rate_l_per_s"])
    tire_time = float(calibration["tire_change_time_s"]) if change_tires else 0.0
    stationary = round(
        fuel_time + tire_time
        if fuel_tire_service_timing == "SEQUENTIAL"
        else max(fuel_time, tire_time),
        6,
    )
    pit_uncertainty = calibration["pit_lane_loss_uncertainty_s"]
    if not isinstance(pit_uncertainty, list):  # pragma: no cover - validator invariant
        raise AssertionError("validated calibration lost its uncertainty")
    loss_low = float(pit_uncertainty[0]) + stationary
    loss_high = float(pit_uncertainty[1]) + stationary
    player = _mapping(motion.get("player"), "traffic motion player")
    player_rates = player.get("rate_range_laps_per_s")
    if not isinstance(player_rates, list):  # pragma: no cover - validator invariant
        raise AssertionError("validated player motion lost its rate range")
    player_low, player_high = map(float, player_rates)
    ahead_candidates: list[dict[str, object]] = []
    behind_candidates: list[dict[str, object]] = []
    ambiguity = False
    for raw in motion["opponents"]:
        opponent = _mapping(raw, "traffic motion opponent")
        delta = float(opponent["current_signed_lap_delta"])
        if delta > 0.0:
            current_low = delta / player_high
            current_high = delta / player_low
        elif delta < 0.0:
            opponent_rates = opponent["rate_range_laps_per_s"]
            if not isinstance(opponent_rates, list):  # pragma: no cover
                raise AssertionError("validated opponent motion lost its rate range")
            opponent_low, opponent_high = map(float, opponent_rates)
            magnitude = abs(delta)
            current_low = -(magnitude / opponent_low)
            current_high = -(magnitude / opponent_high)
        else:
            current_low = current_high = 0.0
        projected_low = current_low + loss_low
        projected_high = current_high + loss_high
        if projected_low <= 0.0 <= projected_high:
            ambiguity = True
            continue
        if projected_low > 0.0:
            ahead_candidates.append(
                {
                    "car_idx": int(opponent["car_idx"]),
                    "gap_range_s": [
                        round(projected_low, 6),
                        round(projected_high, 6),
                    ],
                }
            )
        else:
            behind_candidates.append(
                {
                    "car_idx": int(opponent["car_idx"]),
                    "gap_range_s": [
                        round(abs(projected_high), 6),
                        round(abs(projected_low), 6),
                    ],
                }
            )
    nearest_ahead, ahead_stable = _nearest_stable_neighbor(ahead_candidates)
    nearest_behind, behind_stable = _nearest_stable_neighbor(behind_candidates)
    reasons: list[str] = []
    if ambiguity:
        reasons.append("REJOIN_ZERO_CROSSING_WITHIN_UNCERTAINTY")
    if not ahead_stable:
        reasons.append("REJOIN_AHEAD_ORDER_AMBIGUOUS")
    if not behind_stable:
        reasons.append("REJOIN_BEHIND_ORDER_AMBIGUOUS")
    if nearest_ahead is None and nearest_behind is None and not reasons:
        reasons.append("NO_REJOIN_NEIGHBOR_AVAILABLE")
    available = not reasons
    source_receipt_sha256 = canonical_sha256(
        {
            "calibration_model_sha256": calibration["model_sha256"],
            "motion_context_sha256": motion["motion_sha256"],
        }
    )
    service = {
        "change_tires": change_tires,
        "fuel_add_l": round(fuel_add, 6),
        "fuel_tire_service_timing": fuel_tire_service_timing,
        "stationary_service_s": round(stationary, 6),
        "total_pit_loss_range_s": [round(loss_low, 6), round(loss_high, 6)],
    }
    material: dict[str, object] = {
        "calibration_model_sha256": calibration["model_sha256"],
        "contract_version": TIME_DOMAIN_REJOIN_ESTIMATE_CONTRACT_VERSION,
        "decision_tick": motion["decision_tick"],
        "estimate_available": available,
        "identity_sha256": expected_identity_sha256,
        "method_version": TIME_DOMAIN_REJOIN_METHOD_VERSION,
        "motion_context_sha256": motion["motion_sha256"],
        "nearest_ahead": nearest_ahead if available else None,
        "nearest_behind": nearest_behind if available else None,
        "reason_codes": reasons,
        "service_scenario": service,
        "source_receipt_sha256": source_receipt_sha256,
        "status": (
            "AVAILABLE_STABLE_BRACKET"
            if available
            else "WAIT_AMBIGUOUS_REJOIN_ORDER"
        ),
    }
    estimate = {**material, "estimate_sha256": canonical_sha256(material)}
    return validate_time_domain_rejoin_estimate(
        estimate,
        expected_estimate_sha256=str(estimate["estimate_sha256"]),
        expected_identity_sha256=expected_identity_sha256,
        expected_motion_sha256=expected_motion_sha256,
        expected_calibration_model_sha256=expected_calibration_model_sha256,
    )


def _bound_event_identity_from_context(
    event_context_value: object,
) -> dict[str, object]:
    event_context = _mapping(event_context_value, "event identity context")
    event_identity = _mapping(
        event_context.get("identity"), "event identity projection"
    )
    expected_identity_keys = {
        "car_class_id",
        "event_type",
        "official",
        "race_week",
        "season_id",
        "series_id",
        "sim_build",
        "track_config",
        "track_id",
    }
    if set(event_identity) != expected_identity_keys:
        _fail("SOURCE_CLOSURE_MISMATCH", "event identity projection is invalid")
    identity_provenance = (
        "SDK_DIRECT_SAME_SOURCE_CAPTURE"
        if any(value is not None for value in event_identity.values())
        else "SDK_DIRECT_SAME_SOURCE_SESSION_INFO"
    )
    return {**event_identity, "provenance": identity_provenance}


def _same_capture_strategy_evidence(
    capture_handle: object,
    observed: Mapping[str, object],
    *,
    expected_capture_sha256: str,
    expected_capture_byte_size: int,
    stale_after_s: float,
) -> dict[str, object]:
    """Read only the strategy-safe projection from the already held capture.

    This is a fresh path-free admission of the same descriptor used by the
    producer proof and the full engineer session.  The adapter exposes only a
    fixed event-identity projection; raw SessionInfo and DriverInfo never leave
    the adapter boundary.
    """

    open_input, bound_identity, bound_sha256 = _collector_snapshot_opener(
        capture_handle,
        stale_after_s=stale_after_s,
        opponent_error_policy="degrade",
    )
    if bound_sha256 != expected_capture_sha256:
        _fail(
            "SOURCE_CLOSURE_MISMATCH",
            "strategy evidence capture differs from the producer digest",
        )
    if bound_identity[3] != expected_capture_byte_size:
        _fail(
            "SOURCE_CLOSURE_MISMATCH",
            "strategy evidence capture differs from the producer byte size",
        )
    with open_input() as run:
        input_evidence = run.evidence.to_dict()
        event_context = run.event_identity_context.to_dict()
        traffic_observation_context = run.traffic_observation_context.to_dict()
        last_sample = None
        motion_points: deque[dict[str, object]] = deque()
        tire_stint_tracker = _new_tire_stint_tracker()
        for sample in run.samples:
            last_sample = sample
            _track_tire_stint_sample(tire_stint_tracker, sample)
            point = _traffic_motion_point(sample)
            if point is None:
                continue
            if (
                motion_points
                and float(point["session_time_s"])
                < float(motion_points[-1]["session_time_s"])
            ):
                motion_points.clear()
            motion_points.append(point)
            cutoff = float(point["session_time_s"]) - _TRAFFIC_MOTION_WINDOW_S
            while (
                motion_points
                and float(motion_points[0]["session_time_s"]) < cutoff
            ):
                motion_points.popleft()
    if last_sample is None:
        _fail("SOURCE_CLOSURE_MISMATCH", "strategy evidence has no live sample")

    observed_source = _mapping(
        observed.get("source_binding"), "observed source binding"
    )
    evidence_source = {
        "records_sha256": input_evidence.get("records_sha256"),
        "session_id": input_evidence.get("session_id"),
        "source_id": input_evidence.get("source_id"),
        "source_kind": input_evidence.get("source_kind"),
    }
    if evidence_source != observed_source:
        _fail(
            "SOURCE_CLOSURE_MISMATCH",
            "strategy evidence and producer proof source identities differ",
        )
    input_evidence_sha256 = canonical_sha256(input_evidence)
    if input_evidence_sha256 != observed.get("input_evidence_sha256"):
        _fail(
            "SOURCE_CLOSURE_MISMATCH",
            "strategy evidence and producer proof input evidence differ",
        )
    if event_context.get("source_binding_sha256") != input_evidence_sha256:
        _fail(
            "SOURCE_CLOSURE_MISMATCH",
            "event identity is not bound to strategy input evidence",
        )
    if (
        traffic_observation_context.get("source_binding_sha256")
        != input_evidence_sha256
    ):
        _fail(
            "SOURCE_CLOSURE_MISMATCH",
            "traffic observation is not bound to strategy input evidence",
        )
    bound_event_identity = _bound_event_identity_from_context(event_context)
    traffic_motion_context = _build_traffic_motion_context(
        motion_points,
        traffic_observation_context,
        identity_sha256=canonical_sha256(bound_event_identity),
        source_receipt_sha256=input_evidence_sha256,
    )

    decision = _mapping(observed.get("decision_clock"), "observed decision clock")
    decision_tick = decision.get("decision_tick")
    session_tick = last_sample.session.session_tick
    if (
        session_tick.presence is not Presence.PRESENT
        or session_tick.provenance is not Provenance.SDK_DIRECT
        or session_tick.value != decision_tick
    ):
        _fail(
            "SOURCE_CLOSURE_MISMATCH",
            "strategy evidence decision tick differs from the producer proof",
        )
    tire_stint_context = _build_tire_stint_context(
        tire_stint_tracker,
        identity_sha256=canonical_sha256(bound_event_identity),
        source_receipt_sha256=input_evidence_sha256,
        expected_decision_tick=int(decision_tick),
    )

    flags = last_sample.flags.session_flags
    penalty_state: str | None = None
    penalty_status = flags.presence.value
    session_flags: int | None = None
    if flags.presence is Presence.PRESENT:
        if (
            flags.provenance is not Provenance.SDK_DIRECT
            or type(flags.value) is not int
            or flags.value < 0
        ):
            _fail(
                "SOURCE_CLOSURE_MISMATCH",
                "strategy penalty evidence lost SDK-direct provenance",
            )
        session_flags = flags.value
        penalty_state = (
            "ACTIVE"
            if session_flags & _ACTIVE_PENALTY_FLAG_MASK
            else "CLEAR"
        )
    material: dict[str, object] = {
        "active_penalty_flag_mask": _ACTIVE_PENALTY_FLAG_MASK,
        "decision_tick": decision_tick,
        "event_identity_context": event_context,
        "input_evidence_sha256": input_evidence_sha256,
        "penalty_source_field": "SessionFlags",
        "penalty_state": penalty_state,
        "penalty_status": penalty_status,
        "session_flags": session_flags,
        "source_binding": evidence_source,
        "tire_stint_context": tire_stint_context,
        "traffic_motion_context": traffic_motion_context,
        "traffic_observation_context": traffic_observation_context,
    }
    return {
        **material,
        "strategy_capture_evidence_sha256": canonical_sha256(material),
    }


def _context_builder(
    observed: Mapping[str, object],
    profile: Mapping[str, object],
    context_inputs: Mapping[str, object],
    strategy_evidence: Mapping[str, object],
    *,
    calibration_model: Mapping[str, object] | None,
    expected_calibration_model_sha256: str | None,
    expected_calibration_source_receipt_sha256: str | None,
    tire_performance_model: Mapping[str, object] | None,
    expected_tire_performance_model_sha256: str | None,
    expected_tire_performance_source_receipt_sha256: str | None,
) -> Callable[[Mapping[str, object]], Mapping[str, object]]:
    if calibration_model is None:
        if (
            expected_calibration_model_sha256 is not None
            or expected_calibration_source_receipt_sha256 is not None
        ):
            _fail(
                "CALIBRATION_INPUT_INVALID",
                "calibration digests require an exact calibration model",
            )
        calibration_copy = None
    else:
        if (
            expected_calibration_model_sha256 is None
            or expected_calibration_source_receipt_sha256 is None
        ):
            _fail(
                "CALIBRATION_INPUT_INVALID",
                "calibration model and both independent digests are required together",
            )
        calibration_copy = _json_copy(calibration_model, "calibration model")

    if tire_performance_model is None:
        if (
            expected_tire_performance_model_sha256 is not None
            or expected_tire_performance_source_receipt_sha256 is not None
        ):
            _fail(
                "TIRE_PERFORMANCE_INPUT_INVALID",
                "tire-performance digests require an exact model",
            )
        tire_performance_copy = None
    else:
        if (
            expected_tire_performance_model_sha256 is None
            or expected_tire_performance_source_receipt_sha256 is None
        ):
            _fail(
                "TIRE_PERFORMANCE_INPUT_INVALID",
                "tire-performance model and both independent digests are required together",
            )
        tire_performance_copy = _json_copy(
            tire_performance_model,
            "tire-performance model",
        )

    def build(lineage_value: Mapping[str, object]) -> Mapping[str, object]:
        lineage = _mapping(_json_copy(lineage_value, "input lineage"), "input lineage")
        observed_source = _mapping(
            observed.get("source_binding"), "observed source binding"
        )
        expected_observed = {
            "records_sha256": lineage.get("source_content_sha256"),
            "session_id": lineage.get("session_id"),
            "source_id": lineage.get("source_id"),
            "source_kind": lineage.get("source_kind"),
        }
        if observed_source != expected_observed:
            _fail(
                "SOURCE_CLOSURE_MISMATCH",
                "live proof and fresh engineer-session admissions differ",
            )
        if lineage.get("source_kind") != "SDK_LIVE":
            _fail("SOURCE_CLOSURE_MISMATCH", "analysis source is not SDK_LIVE")

        strategy_source = _mapping(
            strategy_evidence.get("source_binding"),
            "strategy evidence source binding",
        )
        if strategy_source != expected_observed:
            _fail(
                "SOURCE_CLOSURE_MISMATCH",
                "strategy evidence and fresh engineer-session admissions differ",
            )
        event_context = _mapping(
            strategy_evidence.get("event_identity_context"),
            "event identity context",
        )
        bound_identity = _bound_event_identity_from_context(event_context)
        identity_sha256 = canonical_sha256(bound_identity)

        validated_calibration: dict[str, object] | None = None
        if calibration_copy is not None:
            try:
                validated_calibration = validate_matched_pit_calibration_model(
                    calibration_copy,
                    expected_model_sha256=str(expected_calibration_model_sha256),
                    expected_identity_sha256=identity_sha256,
                    expected_source_receipt_sha256=str(
                        expected_calibration_source_receipt_sha256
                    ),
                )
            except PitCalibrationError as exc:
                raise RetrievedLiveAnalysisError(
                    "CALIBRATION_INPUT_INVALID",
                    f"matched calibration failed closed: {exc.code}: {exc}",
                ) from exc
            profile_refuel_rate = _finite_number(
                _mapping(profile.get("fuel_model"), "fuel model profile").get(
                    "refuel_rate_l_per_s"
                ),
                "profile refuel rate",
                positive=True,
            )
            if profile_refuel_rate != float(
                validated_calibration["refuel_rate_l_per_s"]
            ):
                _fail(
                    "CALIBRATION_INPUT_INVALID",
                    "analysis profile and matched calibration refuel rates differ",
                )

        validated_tire_performance: dict[str, object] | None = None
        if tire_performance_copy is not None:
            try:
                validated_tire_performance = validate_tire_performance_model(
                    tire_performance_copy,
                    expected_model_sha256=str(
                        expected_tire_performance_model_sha256
                    ),
                    expected_identity_sha256=identity_sha256,
                    expected_source_receipt_sha256=str(
                        expected_tire_performance_source_receipt_sha256
                    ),
                )
            except TirePerformanceError as exc:
                raise RetrievedLiveAnalysisError(
                    "TIRE_PERFORMANCE_INPUT_INVALID",
                    f"tire-performance model failed closed: {exc.code}: {exc}",
                ) from exc

        traffic_context = _mapping(
            strategy_evidence.get("traffic_observation_context"),
            "traffic observation context",
        )
        motion_context = _mapping(
            strategy_evidence.get("traffic_motion_context"),
            "traffic motion context",
        )
        validated_motion = validate_traffic_motion_context(
            motion_context,
            expected_motion_sha256=str(motion_context.get("motion_sha256")),
            expected_source_receipt_sha256=str(
                strategy_evidence.get("input_evidence_sha256")
            ),
            expected_traffic_map_revision_sha256=str(
                traffic_context.get("context_sha256")
            ),
            expected_identity_sha256=canonical_sha256(bound_identity),
            expected_decision_tick=int(
                _mapping(
                    observed.get("decision_clock"), "observed decision clock"
                )["decision_tick"]
            ),
        )
        motion_context_sha256 = (
            validated_motion["motion_sha256"]
            if validated_motion.get("availability") == "AVAILABLE"
            else None
        )
        traffic_rejoin: dict[str, object] | None = None
        if traffic_context.get("availability") == "AVAILABLE":
            if (
                traffic_context.get("status") != "VERIFIED"
                or traffic_context.get("decision_tick")
                != _mapping(
                    observed.get("decision_clock"), "observed decision clock"
                ).get("decision_tick")
            ):
                _fail(
                    "SOURCE_CLOSURE_MISMATCH",
                    "available traffic observation is not current and verified",
                )
            traffic_material: dict[str, object] = {
                "estimate_available": False,
                "identity_sha256": canonical_sha256(bound_identity),
                "map_revision_sha256": traffic_context["context_sha256"],
                "motion_context": (
                    validated_motion
                    if validated_motion.get("availability") == "AVAILABLE"
                    else None
                ),
                "motion_context_sha256": motion_context_sha256,
                "observed_at_decision_tick": traffic_context["decision_tick"],
                "rejoin_gap_range_s": None,
                "source_receipt_sha256": traffic_context[
                    "source_binding_sha256"
                ],
                "status": (
                    (
                        "OBSERVED_ONLY_WAIT_ACTION_BOUND_REJOIN"
                        if motion_context_sha256 is not None
                        else "OBSERVED_ONLY_WAIT_REJOIN_MODEL"
                    )
                    if validated_calibration is not None
                    else "OBSERVED_ONLY_WAIT_PIT_LOSS"
                ),
            }
            traffic_rejoin = {
                **traffic_material,
                "traffic_sha256": canonical_sha256(traffic_material),
            }

        decision = _mapping(observed.get("decision_clock"), "observed decision clock")
        tire_stint_raw = _mapping(
            strategy_evidence.get("tire_stint_context"),
            "same-capture tire-stint context",
        )
        try:
            tire_stint_context = validate_tire_stint_context(
                tire_stint_raw,
                expected_context_sha256=str(tire_stint_raw.get("context_sha256")),
                expected_identity_sha256=identity_sha256,
                expected_source_receipt_sha256=str(
                    strategy_evidence.get("input_evidence_sha256")
                ),
                expected_decision_tick=int(decision["decision_tick"]),
            )
        except RetrievedLiveAnalysisError:
            raise
        except (TypeError, ValueError) as exc:
            raise RetrievedLiveAnalysisError(
                "TIRE_STINT_CONTEXT_INVALID",
                f"same-capture tire-stint context failed closed: {exc}",
            ) from exc
        pits = _mapping(observed.get("pits_open"), "observed pits-open state")
        pits_value = _observed_available(pits, "observed pits-open state")
        fuel = _mapping(profile.get("fuel_model"), "fuel model profile")
        material: dict[str, object] = {
            "calibration_model": validated_calibration,
            "contract_version": "offline-m2-strategy-context-v2",
            "event_identity": bound_identity,
            "horizon": {
                "kind": context_inputs["horizon_kind"],
                "laps_remaining": context_inputs["laps_remaining"],
                "leader_eta_to_next_crossing_s": None,
                "player_is_leader": None,
                "provenance": context_inputs["horizon_provenance"],
                "reference_lap_time_s": context_inputs[
                    "reference_lap_time_s"
                ],
                "time_remaining_s": context_inputs["time_remaining_s"],
            },
            "observation": {
                "decision_tick": decision["decision_tick"],
                "laps_completed": observed["laps_completed"],
                "penalty_state": strategy_evidence.get("penalty_state"),
                "pits_open": pits_value,
                "reset": False,
                "schema_changed": False,
                "session_epoch": 1,
                "source_epoch": 1,
                "stale": False,
            },
            "source_binding": {
                "event_receipt_sha256": lineage["event_receipt_sha256"],
                "normalized_samples_sha256": lineage[
                    "normalized_samples_sha256"
                ],
                "sample_count": lineage["sample_count"],
                "session_id": lineage["session_id"],
                "source_id": lineage["source_id"],
                "source_kind": lineage["source_kind"],
                "source_sha256": lineage["source_content_sha256"],
            },
            "strategy_policy": {
                "conservative_quantile": fuel["conservative_quantile"],
                "reserve_l": fuel["reserve_l"],
                "selection_policy": "LATEST_COMMON_FUEL_FEASIBLE",
            },
            "tire_performance_model": validated_tire_performance,
            "tire_stint_context": tire_stint_context,
            "traffic_rejoin": traffic_rejoin,
            "vehicle_context": {
                "provenance": "USER_RULE",
                "tank_capacity_l": fuel["tank_capacity_l"],
            },
        }
        return {**material, "context_sha256": canonical_sha256(material)}

    return build


def _component_error(label: str, exc: BaseException) -> RetrievedLiveAnalysisError:
    code = getattr(exc, "code", type(exc).__name__)
    return RetrievedLiveAnalysisError(
        "ANALYSIS_COMPONENT_FAILED", f"{label} failed closed: {code}: {exc}"
    )


def _build_analysis(
    capture_handle: object,
    live_session: Mapping[str, object],
    analysis_profile: Mapping[str, object],
    *,
    expected_live_engineer_session_sha256: str,
    expected_remote_capture_sha256: str,
    expected_remote_capture_byte_size: int,
    expected_analysis_profile_sha256: str,
    calibration_model: Mapping[str, object] | None,
    expected_calibration_model_sha256: str | None,
    expected_calibration_source_receipt_sha256: str | None,
    tire_performance_model: Mapping[str, object] | None,
    expected_tire_performance_model_sha256: str | None,
    expected_tire_performance_source_receipt_sha256: str | None,
    rules_profile: Mapping[str, object] | None,
    expected_rules_profile_sha256: str | None,
    expected_rules_source_sha256: str | None,
    previous_m2_receipt: Mapping[str, object] | None,
    expected_previous_m2_sha256: str | None,
    expected_previous_revision: int | None,
    stale_after_s: float,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    profile = validate_retrieved_live_analysis_profile(
        analysis_profile,
        expected_analysis_profile_sha256=expected_analysis_profile_sha256,
    )
    try:
        expected_live = validate_live_engineer_session(
            live_session,
            expected_live_engineer_session_sha256=(
                expected_live_engineer_session_sha256
            ),
            expected_capture_sha256=expected_remote_capture_sha256,
            expected_capture_byte_size=expected_remote_capture_byte_size,
        )
        replayed_live = replay_retrieved_live_engineer_session(
            capture_handle,
            expected_live,
            expected_remote_capture_sha256=expected_remote_capture_sha256,
            expected_remote_capture_byte_size=expected_remote_capture_byte_size,
            stale_after_s=stale_after_s,
        )
    except (LiveEngineerSessionError, OSError, ValueError) as exc:
        raise _component_error("retrieved live proof replay", exc) from exc
    if replayed_live != expected_live:
        _fail("LIVE_REPLAY_MISMATCH", "retrieved live proof is not object-exact")

    observed = _mapping(
        replayed_live.get("observed_live_evidence"), "observed live evidence"
    )
    scenario, context_inputs = _resolve_scenario(observed, profile)
    try:
        strategy_evidence = _same_capture_strategy_evidence(
            capture_handle,
            observed,
            expected_capture_sha256=expected_remote_capture_sha256,
            expected_capture_byte_size=expected_remote_capture_byte_size,
            stale_after_s=stale_after_s,
        )
        session = build_engineer_session_from_collector_snapshot(
            capture_handle,
            scenario=scenario,
            strategy_context_builder=_context_builder(
                observed,
                profile,
                context_inputs,
                strategy_evidence,
                calibration_model=calibration_model,
                expected_calibration_model_sha256=(
                    expected_calibration_model_sha256
                ),
                expected_calibration_source_receipt_sha256=(
                    expected_calibration_source_receipt_sha256
                ),
                tire_performance_model=tire_performance_model,
                expected_tire_performance_model_sha256=(
                    expected_tire_performance_model_sha256
                ),
                expected_tire_performance_source_receipt_sha256=(
                    expected_tire_performance_source_receipt_sha256
                ),
            ),
            expected_snapshot_sha256=expected_remote_capture_sha256,
            expected_snapshot_byte_size=expected_remote_capture_byte_size,
            rules_profile=rules_profile,
            expected_rules_profile_sha256=expected_rules_profile_sha256,
            expected_rules_source_sha256=expected_rules_source_sha256,
            previous_m2_receipt=previous_m2_receipt,
            expected_previous_m2_sha256=expected_previous_m2_sha256,
            expected_previous_revision=expected_previous_revision,
            stale_after_s=stale_after_s,
        )
        session = validate_engineer_session(
            session,
            expected_engineer_session_sha256=str(
                session["engineer_session_sha256"]
            ),
        )
        report = build_engineer_session_report(
            session,
            expected_engineer_session_sha256=str(
                session["engineer_session_sha256"]
            ),
        )
    except (
        EngineerSessionError,
        EngineerSessionReportError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise _component_error("full engineer-session analysis", exc) from exc

    lineage = _mapping(session.get("input_lineage"), "engineer session lineage")
    live_closure = _mapping(replayed_live.get("closure"), "live proof closure")
    closure_pairs = {
        "event_receipt_sha256": "event_receipt_sha256",
        "input_evidence_sha256": "input_evidence_sha256",
        "input_lineage_sha256": "input_lineage_sha256",
        "normalized_samples_sha256": "normalized_samples_sha256",
        "sample_count": "sample_count",
        "session_id": "session_id",
        "source_content_sha256": "source_content_sha256",
        "source_id": "source_id",
        "source_kind": "source_kind",
    }
    for lineage_key, closure_key in closure_pairs.items():
        if lineage.get(lineage_key) != live_closure.get(closure_key):
            _fail(
                "SOURCE_CLOSURE_MISMATCH",
                f"live proof and engineer session {lineage_key} differ",
            )
    if lineage.get("source_kind") != "SDK_LIVE":
        _fail("SOURCE_CLOSURE_MISMATCH", "engineer session is not SDK_LIVE")
    return session, report, replayed_live, context_inputs


def _bundle_status(readiness: Mapping[str, object]) -> str:
    strategy = readiness.get("strategy_advice_available") is True
    driving = readiness.get("driving_practice_available") is True
    if strategy and driving:
        return "ADVICE_READY_BOTH"
    if strategy:
        return "STRATEGY_ADVICE_READY_DRIVING_WAIT"
    if driving:
        return "DRIVING_ADVICE_READY_STRATEGY_WAIT"
    if readiness.get("report_status") == "EVIDENCE_ONLY":
        return "EVIDENCE_ONLY_WAIT_ADVICE"
    return "WAIT_ADVICE"


def _build_bundle_receipt(
    *,
    profile: Mapping[str, object],
    live: Mapping[str, object],
    session: Mapping[str, object],
    report: Mapping[str, object],
    context_inputs: Mapping[str, object],
    session_bytes: bytes,
    report_bytes: bytes,
    html_bytes: bytes,
) -> dict[str, object]:
    authority = _mapping(live.get("analysis_authority"), "live analysis authority")
    observed = _mapping(live.get("observed_live_evidence"), "observed evidence")
    lineage = _mapping(session.get("input_lineage"), "engineer session lineage")
    components = _mapping(session.get("components"), "engineer session components")
    fuel = _mapping(components.get("fuel_replay"), "fuel replay")
    strategy = _mapping(components.get("m2_strategy"), "M2 strategy")
    strategy_rules = _mapping(strategy.get("rules_binding"), "M2 rules binding")
    sections = _mapping(report.get("sections"), "report sections")
    strategy_section = _mapping(sections.get("strategy"), "report strategy section")
    driving_section = _mapping(sections.get("driving"), "report driving section")
    strategy_recommendations = strategy_section.get("recommendations")
    if type(strategy_recommendations) is not list:
        _fail("REPORT_INVALID", "report strategy recommendations are invalid")
    practice_count = _plain_int(
        driving_section.get("practice_action_count"), "practice action count"
    )
    blockers = report.get("blockers")
    if type(blockers) is not list:
        _fail("REPORT_INVALID", "report blockers are invalid")
    blocker_codes: list[str] = []
    for raw in blockers:
        row = _mapping(raw, "report blocker")
        domain = _identifier(row.get("domain"), "blocker domain")
        code = _identifier(row.get("code"), "blocker code")
        blocker_codes.append(f"{domain}:{code}")
    blocker_codes = sorted(set(blocker_codes))
    readiness = {
        "blocker_codes": blocker_codes,
        "driving_practice_available": practice_count > 0,
        "practice_action_count": practice_count,
        "report_status": report.get("status"),
        "strategy_advice_available": bool(strategy_recommendations),
        "strategy_recommendation_count": len(strategy_recommendations),
    }
    orchestration = _mapping(
        session.get("orchestration_inputs"), "engineer orchestration inputs"
    )
    rules_present = orchestration.get("rules_profile") is not None
    rules_binding = {
        "expected_profile_sha256": orchestration.get(
            "expected_rules_profile_sha256"
        ),
        "expected_source_document_sha256": orchestration.get(
            "expected_rules_source_sha256"
        ),
        "official_event_rules": strategy_rules.get("official_event_rules"),
        "profile_present": rules_present,
        "status": strategy_rules.get("status"),
    }
    base: dict[str, object] = {
        "advisor_only": True,
        "analysis_profile_binding": {
            "contract_version": profile["contract_version"],
            "profile_id": profile["profile_id"],
            "profile_sha256": profile["analysis_profile_sha256"],
            "profile_version": profile["profile_version"],
        },
        "capture_binding": {
            "capture_byte_size": authority["capture_byte_size"],
            "capture_sha256": authority["capture_sha256"],
            "live_analysis_authority_sha256": authority["authority_sha256"],
            "live_engineer_session_sha256": live[
                "live_engineer_session_sha256"
            ],
            "observed_live_evidence_sha256": observed[
                "observed_live_evidence_sha256"
            ],
        },
        "contract_version": RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION,
        "engineer_session_binding": {
            "byte_size": len(session_bytes),
            "component_hashes": session["component_hashes"],
            "contract_version": session["contract_version"],
            "file_sha256": hashlib.sha256(session_bytes).hexdigest(),
            "semantic_hashes": session["semantic_hashes"],
            "session_sha256": session["engineer_session_sha256"],
            "status": session["status"],
        },
        "horizon_binding": {
            "fuel_scenario_sha256": fuel["scenario_sha256"],
            "source": context_inputs["horizon_source"],
            "strategy_context_sha256": orchestration[
                "strategy_context_sha256"
            ],
        },
        "readiness": readiness,
        "report_binding": {
            "artifact_byte_size": len(report_bytes),
            "artifact_file_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "contract_version": report["contract_version"],
            "html_byte_size": len(html_bytes),
            "html_file_sha256": hashlib.sha256(html_bytes).hexdigest(),
            "report_sha256": report["report_sha256"],
        },
        "rules_binding": rules_binding,
        "safety": dict(_SAFETY),
        "source_binding": {
            "event_receipt_sha256": lineage["event_receipt_sha256"],
            "input_evidence_sha256": lineage["input_evidence_sha256"],
            "input_lineage_sha256": lineage["input_lineage_sha256"],
            "normalized_samples_sha256": lineage[
                "normalized_samples_sha256"
            ],
            "records_sha256": lineage["source_content_sha256"],
            "sample_count": lineage["sample_count"],
            "session_id": lineage["session_id"],
            "source_id": lineage["source_id"],
            "source_kind": lineage["source_kind"],
        },
        "status": _bundle_status(readiness),
    }
    return {**base, "bundle_receipt_sha256": canonical_sha256(base)}


def validate_retrieved_live_analysis_receipt(
    value: object,
    *,
    expected_bundle_receipt_sha256: str | None = None,
) -> dict[str, object]:
    """Validate one persisted bundle receipt without reopening its artifacts."""

    receipt = _exact(
        _json_copy(value, "analysis bundle receipt"),
        _BUNDLE_KEYS,
        "analysis bundle receipt",
    )
    if (
        receipt.get("contract_version")
        != RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION
        or receipt.get("advisor_only") is not True
        or receipt.get("safety") != _SAFETY
    ):
        _fail("BUNDLE_INVALID", "analysis bundle contract or safety boundary differs")
    stored = _sha256(
        receipt.get("bundle_receipt_sha256"), "analysis bundle receipt SHA-256"
    )
    if expected_bundle_receipt_sha256 is not None and stored != _sha256(
        expected_bundle_receipt_sha256,
        "expected analysis bundle receipt SHA-256",
    ):
        _fail("BUNDLE_INVALID", "analysis bundle differs from independent digest")
    material = {
        key: item
        for key, item in receipt.items()
        if key != "bundle_receipt_sha256"
    }
    if canonical_sha256(material) != stored:
        _fail("BUNDLE_INVALID", "analysis bundle self hash differs")

    profile = _exact(
        receipt.get("analysis_profile_binding"),
        _PROFILE_BINDING_KEYS,
        "analysis profile binding",
    )
    if profile.get("contract_version") != (
        RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION
    ):
        _fail("BUNDLE_INVALID", "analysis profile binding contract differs")
    _identifier(profile.get("profile_id"), "bound analysis profile id")
    _plain_int(profile.get("profile_version"), "bound profile version", minimum=1)
    _sha256(profile.get("profile_sha256"), "bound analysis profile SHA-256")

    capture = _exact(
        receipt.get("capture_binding"),
        _CAPTURE_BINDING_KEYS,
        "capture binding",
    )
    _plain_int(capture.get("capture_byte_size"), "capture byte size", minimum=1)
    for key in _CAPTURE_BINDING_KEYS - {"capture_byte_size"}:
        _sha256(capture.get(key), f"capture binding {key}")

    engineer = _exact(
        receipt.get("engineer_session_binding"),
        _ENGINEER_BINDING_KEYS,
        "engineer session binding",
    )
    _plain_int(engineer.get("byte_size"), "engineer session byte size", minimum=1)
    for key in ("file_sha256", "session_sha256"):
        _sha256(engineer.get(key), f"engineer session {key}")
    if engineer.get("contract_version") != "engineer-session-v1":
        _fail("BUNDLE_INVALID", "engineer session contract differs")
    _identifier(engineer.get("status"), "engineer session status")
    component_hashes = _exact(
        engineer.get("component_hashes"),
        _COMPONENT_HASH_KEYS,
        "component hash table",
    )
    semantic_hashes = _exact(
        engineer.get("semantic_hashes"),
        _SEMANTIC_HASH_KEYS,
        "semantic hash table",
    )
    for name, digest in (*component_hashes.items(), *semantic_hashes.items()):
        _sha256(digest, f"bound hash {name}")

    horizon = _exact(
        receipt.get("horizon_binding"),
        _HORIZON_BINDING_KEYS,
        "horizon binding",
    )
    if horizon.get("source") not in _HORIZON_SOURCES:
        _fail("BUNDLE_INVALID", "horizon source is invalid")
    _sha256(horizon.get("fuel_scenario_sha256"), "fuel scenario SHA-256")
    _sha256(horizon.get("strategy_context_sha256"), "strategy context SHA-256")

    readiness = _exact(
        receipt.get("readiness"), _READINESS_KEYS, "analysis readiness"
    )
    for key in ("driving_practice_available", "strategy_advice_available"):
        if type(readiness.get(key)) is not bool:
            _fail("BUNDLE_INVALID", f"readiness {key} must be boolean")
    practice_count = _plain_int(
        readiness.get("practice_action_count"), "practice action count"
    )
    strategy_count = _plain_int(
        readiness.get("strategy_recommendation_count"),
        "strategy recommendation count",
    )
    if (practice_count > 0) != readiness["driving_practice_available"]:
        _fail("BUNDLE_INVALID", "driving readiness/count differ")
    if (strategy_count > 0) != readiness["strategy_advice_available"]:
        _fail("BUNDLE_INVALID", "strategy readiness/count differ")
    blockers = readiness.get("blocker_codes")
    if (
        type(blockers) is not list
        or any(type(item) is not str or not item for item in blockers)
        or blockers != sorted(set(blockers))
    ):
        _fail("BUNDLE_INVALID", "readiness blocker codes are invalid")
    _identifier(readiness.get("report_status"), "report readiness status")
    expected_status = _bundle_status(readiness)
    if receipt.get("status") != expected_status or expected_status not in _BUNDLE_STATUSES:
        _fail("BUNDLE_INVALID", "analysis bundle status differs from readiness")

    report = _exact(
        receipt.get("report_binding"),
        _REPORT_BINDING_KEYS,
        "report binding",
    )
    if report.get("contract_version") != "engineer-session-report-v1":
        _fail("BUNDLE_INVALID", "report contract differs")
    for key in ("artifact_byte_size", "html_byte_size"):
        _plain_int(report.get(key), f"report {key}", minimum=1)
    for key in ("artifact_file_sha256", "html_file_sha256", "report_sha256"):
        _sha256(report.get(key), f"report {key}")

    rules = _exact(
        receipt.get("rules_binding"), _RULES_BINDING_KEYS, "rules binding"
    )
    if type(rules.get("profile_present")) is not bool or type(
        rules.get("official_event_rules")
    ) is not bool:
        _fail("BUNDLE_INVALID", "rules booleans are invalid")
    for key in ("expected_profile_sha256", "expected_source_document_sha256"):
        digest = rules.get(key)
        if digest is not None:
            _sha256(digest, f"rules {key}")
    if rules["profile_present"] != (
        rules["expected_profile_sha256"] is not None
        and rules["expected_source_document_sha256"] is not None
    ):
        _fail("BUNDLE_INVALID", "rules presence and digests differ")
    _identifier(rules.get("status"), "rules status")

    source = _exact(
        receipt.get("source_binding"), _SOURCE_BINDING_KEYS, "source binding"
    )
    if source.get("source_kind") != "SDK_LIVE":
        _fail("BUNDLE_INVALID", "bundle source is not SDK_LIVE")
    for key in ("session_id", "source_id"):
        _identifier(source.get(key), f"source binding {key}")
    _plain_int(source.get("sample_count"), "source sample count", minimum=1)
    for key in _SOURCE_BINDING_KEYS - {
        "sample_count",
        "session_id",
        "source_id",
        "source_kind",
    }:
        _sha256(source.get(key), f"source binding {key}")
    return receipt


def _validate_bundle_paths(paths: tuple[Path, ...]) -> None:
    normalized = [os.path.normcase(str(path.absolute())) for path in paths]
    if len(normalized) != len(set(normalized)):
        _fail("OUTPUT_PATH_INVALID", "all analysis bundle outputs must differ")
    for path in paths:
        if path.name in {"", ".", ".."}:
            _fail("OUTPUT_PATH_INVALID", "every output must name one file")
        if path.exists() or path.is_symlink():
            _fail("OUTPUT_CREATE_FAILED", f"output already exists: {path.name}")


def _write_bytes_exclusive(path: Path, payload: bytes, label: str) -> None:
    try:
        absolute_parent = path.parent.absolute()
        resolved_parent = path.parent.resolve(strict=True)
        parent_metadata = os.lstat(absolute_parent)
    except OSError as exc:
        raise RetrievedLiveAnalysisError(
            "OUTPUT_PARENT_OPEN_FAILED", f"cannot inspect output parent: {exc}"
        ) from exc
    if (
        os.path.normcase(str(absolute_parent))
        != os.path.normcase(str(resolved_parent))
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or int(getattr(parent_metadata, "st_file_attributes", 0)) & 0x400
    ):
        _fail("OUTPUT_PARENT_OPEN_FAILED", "output parent is not one real directory")
    output = absolute_parent / path.name
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(output, flags, 0o600)
        except OSError as exc:
            raise RetrievedLiveAnalysisError(
                "OUTPUT_CREATE_FAILED", f"cannot CreateNew {label}: {path.name}"
            ) from exc
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or int(getattr(metadata, "st_file_attributes", 0)) & 0x400
        ):
            _fail("OUTPUT_CREATE_FAILED", f"new {label} is not a regular file")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(f"short write while persisting {label}")
            remaining = remaining[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if after.st_size != len(payload) or after.st_nlink != 1:
            _fail("OUTPUT_CONTENT_CHANGED", f"{label} metadata changed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        readback = bytearray()
        while len(readback) < len(payload):
            chunk = os.read(descriptor, min(1_048_576, len(payload) - len(readback)))
            if not chunk:
                break
            readback.extend(chunk)
        if bytes(readback) != payload:
            _fail("OUTPUT_CONTENT_CHANGED", f"{label} readback differs")
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _calibration_fail(code: str, message: str) -> NoReturn:
    raise PitCalibrationError(code, message)


def _calibration_json_copy(value: object, name: str) -> dict[str, object]:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise PitCalibrationError(
            "SCHEMA_INVALID", f"{name} is not canonical-JSON-safe"
        ) from exc
    if type(decoded) is not dict:
        _calibration_fail("SCHEMA_INVALID", f"{name} must be an object")
    return decoded


def _calibration_exact(
    value: object,
    keys: frozenset[str],
    name: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _calibration_fail("SCHEMA_INVALID", f"{name} keys are invalid")
    return value


def _calibration_list(value: object, name: str) -> list[object]:
    if type(value) is not list:
        _calibration_fail("SCHEMA_INVALID", f"{name} must be an array")
    return value


def _calibration_identifier(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in value)
    ):
        _calibration_fail("SCHEMA_INVALID", f"{name} is not a valid identifier")
    return value


def _calibration_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _calibration_fail(
            "SCHEMA_INVALID", f"{name} must be a lowercase SHA-256 digest"
        )
    return value


def _calibration_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _calibration_fail(
            "SCHEMA_INVALID", f"{name} must be an integer >= {minimum}"
        )
    return value


def _calibration_number(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _calibration_fail("SCHEMA_INVALID", f"{name} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        _calibration_fail("SCHEMA_INVALID", f"{name} must be finite")
    if positive and converted <= 0.0:
        _calibration_fail("SCHEMA_INVALID", f"{name} must be positive")
    if minimum is not None and converted < minimum:
        _calibration_fail("SCHEMA_INVALID", f"{name} must be >= {minimum}")
    return converted


def _validate_calibration_identity(value: object) -> dict[str, object]:
    identity = _calibration_exact(
        value, _CALIBRATION_IDENTITY_KEYS, "event identity"
    )
    for key in ("series_id", "season_id", "track_id", "car_class_id"):
        _calibration_int(identity.get(key), f"event identity {key}", minimum=1)
    _calibration_int(identity.get("race_week"), "event identity race_week")
    for key in ("event_type", "track_config", "sim_build"):
        _calibration_identifier(identity.get(key), f"event identity {key}")
    if type(identity.get("official")) is not bool:
        _calibration_fail(
            "SCHEMA_INVALID", "event identity official must be boolean"
        )
    if identity.get("provenance") not in _CALIBRATION_IDENTITY_PROVENANCE:
        _calibration_fail(
            "SCHEMA_INVALID", "event identity provenance is unsupported"
        )
    return identity


def validate_matched_pit_calibration_dataset(
    value: object,
    *,
    expected_dataset_sha256: str,
) -> dict[str, object]:
    """Validate exact matched observations against an independent digest."""

    dataset = _calibration_exact(
        _calibration_json_copy(value, "matched pit calibration dataset"),
        _CALIBRATION_DATASET_KEYS,
        "matched pit calibration dataset",
    )
    if dataset.get("contract_version") != (
        MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION
    ):
        _calibration_fail(
            "DATASET_INVALID", "matched pit calibration contract is unsupported"
        )
    _calibration_identifier(dataset.get("dataset_id"), "calibration dataset id")
    _calibration_int(
        dataset.get("dataset_version"), "calibration dataset version", minimum=1
    )
    stored = _calibration_sha256(
        dataset.get("dataset_sha256"), "calibration dataset SHA-256"
    )
    expected = _calibration_sha256(
        expected_dataset_sha256, "expected calibration dataset SHA-256"
    )
    if stored != expected:
        _calibration_fail(
            "DATASET_INVALID", "calibration dataset differs from independent digest"
        )
    material = {key: item for key, item in dataset.items() if key != "dataset_sha256"}
    if canonical_sha256(material) != stored:
        _calibration_fail("DATASET_INVALID", "calibration dataset self hash differs")
    _validate_calibration_identity(dataset.get("event_identity"))

    samples = _calibration_list(dataset.get("samples"), "calibration samples")
    if len(samples) < 3:
        _calibration_fail(
            "INSUFFICIENT_MATCHED_SAMPLES",
            "at least three matched samples are required",
        )
    sample_ids: set[str] = set()
    source_receipts: set[str] = set()
    label_receipts: set[str] = set()
    for index, raw in enumerate(samples):
        sample = _calibration_exact(
            raw, _CALIBRATION_SAMPLE_KEYS, f"calibration samples[{index}]"
        )
        sample_id = _calibration_identifier(
            sample.get("sample_id"), f"sample {index} id"
        )
        source = _calibration_sha256(
            sample.get("source_receipt_sha256"),
            f"sample {index} source receipt",
        )
        label = _calibration_sha256(
            sample.get("label_receipt_sha256"),
            f"sample {index} label receipt",
        )
        if sample_id in sample_ids:
            _calibration_fail(
                "DATASET_INVALID", "calibration sample ids must be unique"
            )
        if source in source_receipts:
            _calibration_fail(
                "DATASET_INVALID", "calibration source receipts must be independent"
            )
        if label in label_receipts:
            _calibration_fail(
                "DATASET_INVALID", "calibration label receipts must be independent"
            )
        sample_ids.add(sample_id)
        source_receipts.add(source)
        label_receipts.add(label)

        pit_road = _calibration_number(
            sample.get("pit_road_elapsed_s"),
            f"sample {index} pit-road elapsed",
            positive=True,
        )
        baseline = _calibration_number(
            sample.get("matched_track_segment_elapsed_s"),
            f"sample {index} matched track-segment elapsed",
            positive=True,
        )
        stationary = _calibration_number(
            sample.get("stationary_service_elapsed_s"),
            f"sample {index} stationary service elapsed",
            minimum=0.0,
        )
        delivered = _calibration_number(
            sample.get("fuel_delivered_l"),
            f"sample {index} delivered fuel",
            positive=True,
        )
        fuel_elapsed = _calibration_number(
            sample.get("fuel_service_elapsed_s"),
            f"sample {index} fuel-service elapsed",
            positive=True,
        )
        tire_elapsed = _calibration_number(
            sample.get("tire_change_elapsed_s"),
            f"sample {index} tire-change elapsed",
            positive=True,
        )
        if stationary >= pit_road:
            _calibration_fail(
                "MATCH_INVALID",
                f"sample {index} stationary service must be shorter than pit-road elapsed",
            )
        if pit_road - stationary < baseline:
            _calibration_fail(
                "MATCH_INVALID",
                f"sample {index} produces a negative counterfactual pit-lane loss",
            )
        if delivered / fuel_elapsed > 20.0:
            _calibration_fail(
                "MATCH_INVALID", f"sample {index} refuel rate is implausibly high"
            )
        if fuel_elapsed > stationary + 1e-9:
            _calibration_fail(
                "MATCH_INVALID",
                f"sample {index} fuel service exceeds total stationary service",
            )
        if tire_elapsed > stationary + 1e-9:
            _calibration_fail(
                "MATCH_INVALID",
                f"sample {index} tire service exceeds total stationary service",
            )
    return dataset


def build_matched_pit_calibration_model(
    dataset_value: object,
    *,
    expected_dataset_sha256: str,
) -> dict[str, object]:
    """Return the exact M2 calibration-model shape from matched evidence."""

    dataset = validate_matched_pit_calibration_dataset(
        dataset_value,
        expected_dataset_sha256=expected_dataset_sha256,
    )
    identity = _validate_calibration_identity(dataset["event_identity"])
    losses: list[float] = []
    rates: list[float] = []
    tire_times: list[float] = []
    for raw in _calibration_list(dataset["samples"], "calibration samples"):
        sample = _calibration_exact(raw, _CALIBRATION_SAMPLE_KEYS, "calibration sample")
        pit_road = float(sample["pit_road_elapsed_s"])
        stationary = float(sample["stationary_service_elapsed_s"])
        baseline = float(sample["matched_track_segment_elapsed_s"])
        losses.append((pit_road - stationary) - baseline)
        rates.append(
            float(sample["fuel_delivered_l"])
            / float(sample["fuel_service_elapsed_s"])
        )
        tire_times.append(float(sample["tire_change_elapsed_s"]))

    material: dict[str, object] = {
        "identity_sha256": canonical_sha256(identity),
        "method_version": MATCHED_PIT_CALIBRATION_METHOD_VERSION,
        "pit_lane_loss_s": round(float(median(losses)), 6),
        "pit_lane_loss_uncertainty_s": [
            round(min(losses), 6),
            round(max(losses), 6),
        ],
        "refuel_rate_l_per_s": round(float(median(rates)), 6),
        "sample_count": len(losses),
        "service_labels_available": True,
        "source_receipt_sha256": dataset["dataset_sha256"],
        "status": "CALIBRATED_MATCHED_BASELINE",
        "tire_change_time_s": round(float(median(tire_times)), 6),
    }
    model = {**material, "model_sha256": canonical_sha256(material)}
    return validate_matched_pit_calibration_model(
        model,
        expected_model_sha256=str(model["model_sha256"]),
        expected_identity_sha256=str(model["identity_sha256"]),
        expected_source_receipt_sha256=str(dataset["dataset_sha256"]),
    )


def validate_matched_pit_calibration_model(
    value: object,
    *,
    expected_model_sha256: str,
    expected_identity_sha256: str,
    expected_source_receipt_sha256: str,
) -> dict[str, object]:
    """Validate a persisted M2-shaped calibration model independently."""

    model = _calibration_exact(
        _calibration_json_copy(value, "matched pit calibration model"),
        _CALIBRATION_MODEL_KEYS,
        "matched pit calibration model",
    )
    stored = _calibration_sha256(
        model.get("model_sha256"), "calibration model SHA-256"
    )
    if stored != _calibration_sha256(
        expected_model_sha256, "expected calibration model SHA-256"
    ):
        _calibration_fail(
            "MODEL_INVALID", "calibration model differs from independent digest"
        )
    material = {key: item for key, item in model.items() if key != "model_sha256"}
    if canonical_sha256(material) != stored:
        _calibration_fail("MODEL_INVALID", "calibration model self hash differs")
    if model.get("status") != "CALIBRATED_MATCHED_BASELINE":
        _calibration_fail("MODEL_INVALID", "calibration model status is invalid")
    if model.get("method_version") != MATCHED_PIT_CALIBRATION_METHOD_VERSION:
        _calibration_fail("MODEL_INVALID", "calibration model method is unsupported")
    if model.get("service_labels_available") is not True:
        _calibration_fail(
            "MODEL_INVALID", "calibration model lacks verified service labels"
        )
    identity = _calibration_sha256(
        model.get("identity_sha256"), "calibration identity SHA-256"
    )
    if identity != _calibration_sha256(
        expected_identity_sha256, "expected identity SHA-256"
    ):
        _calibration_fail("MODEL_INVALID", "calibration model identity differs")
    source = _calibration_sha256(
        model.get("source_receipt_sha256"), "calibration source receipt"
    )
    if source != _calibration_sha256(
        expected_source_receipt_sha256,
        "expected calibration source receipt",
    ):
        _calibration_fail("MODEL_INVALID", "calibration source receipt differs")
    _calibration_int(model.get("sample_count"), "calibration sample count", minimum=3)
    loss = _calibration_number(
        model.get("pit_lane_loss_s"), "pit-lane loss", minimum=0.0
    )
    uncertainty = _calibration_list(
        model.get("pit_lane_loss_uncertainty_s"), "pit-loss uncertainty"
    )
    if len(uncertainty) != 2:
        _calibration_fail(
            "MODEL_INVALID", "pit-loss uncertainty must have two bounds"
        )
    low = _calibration_number(
        uncertainty[0], "pit-loss uncertainty low", minimum=0.0
    )
    high = _calibration_number(
        uncertainty[1], "pit-loss uncertainty high", minimum=0.0
    )
    if not low <= loss <= high:
        _calibration_fail(
            "MODEL_INVALID", "pit-loss estimate lies outside its uncertainty"
        )
    _calibration_number(
        model.get("refuel_rate_l_per_s"), "refuel rate", positive=True
    )
    _calibration_number(
        model.get("tire_change_time_s"), "tire-change time", positive=True
    )
    return model


def _tire_fail(code: str, message: str) -> NoReturn:
    raise TirePerformanceError(code, message)


def _tire_json_copy(value: object, name: str) -> dict[str, object]:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise TirePerformanceError(
            "SCHEMA_INVALID", f"{name} is not canonical-JSON-safe"
        ) from exc
    if type(decoded) is not dict:
        _tire_fail("SCHEMA_INVALID", f"{name} must be an object")
    return decoded


def _tire_exact(
    value: object,
    keys: frozenset[str],
    name: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _tire_fail("SCHEMA_INVALID", f"{name} keys are invalid")
    return value


def _tire_list(value: object, name: str) -> list[object]:
    if type(value) is not list:
        _tire_fail("SCHEMA_INVALID", f"{name} must be an array")
    return value


def _tire_identifier(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in value)
    ):
        _tire_fail("SCHEMA_INVALID", f"{name} is not a valid identifier")
    return value


def _tire_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _tire_fail("SCHEMA_INVALID", f"{name} must be a lowercase SHA-256 digest")
    return value


def _tire_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _tire_fail("SCHEMA_INVALID", f"{name} must be an integer >= {minimum}")
    return value


def _tire_number(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _tire_fail("SCHEMA_INVALID", f"{name} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        _tire_fail("SCHEMA_INVALID", f"{name} must be finite")
    if positive and converted <= 0.0:
        _tire_fail("SCHEMA_INVALID", f"{name} must be positive")
    if minimum is not None and converted < minimum:
        _tire_fail("SCHEMA_INVALID", f"{name} must be >= {minimum}")
    if maximum is not None and converted > maximum:
        _tire_fail("SCHEMA_INVALID", f"{name} must be <= {maximum}")
    return converted


def _validate_tire_identity(value: object) -> dict[str, object]:
    try:
        return _validate_calibration_identity(value)
    except PitCalibrationError as exc:
        raise TirePerformanceError(exc.code, str(exc)) from exc


def _validate_tire_fuel_load_model(value: object) -> dict[str, object]:
    model = _tire_exact(
        value,
        _TIRE_FUEL_LOAD_MODEL_KEYS,
        "tire-performance fuel-load model",
    )
    stored = _tire_sha256(
        model.get("model_sha256"), "tire-performance fuel-load model SHA-256"
    )
    material = {key: item for key, item in model.items() if key != "model_sha256"}
    if canonical_sha256(material) != stored:
        _tire_fail("DATASET_INVALID", "fuel-load model self hash differs")
    if model.get("status") != "CALIBRATED_FUEL_LOAD_EFFECT":
        _tire_fail("DATASET_INVALID", "fuel-load model status is invalid")
    _tire_sha256(
        model.get("source_receipt_sha256"), "fuel-load model source receipt"
    )
    central = _tire_number(
        model.get("seconds_per_liter"),
        "fuel-load seconds per liter",
        minimum=0.0,
        maximum=1.0,
    )
    bounds = _tire_list(
        model.get("seconds_per_liter_uncertainty"),
        "fuel-load seconds-per-liter uncertainty",
    )
    if len(bounds) != 2:
        _tire_fail(
            "DATASET_INVALID",
            "fuel-load seconds-per-liter uncertainty must have two bounds",
        )
    low = _tire_number(
        bounds[0],
        "fuel-load seconds-per-liter uncertainty low",
        minimum=0.0,
        maximum=1.0,
    )
    high = _tire_number(
        bounds[1],
        "fuel-load seconds-per-liter uncertainty high",
        minimum=0.0,
        maximum=1.0,
    )
    if not low <= central <= high:
        _tire_fail(
            "DATASET_INVALID", "fuel-load estimate lies outside its uncertainty"
        )
    return model


def _validate_tire_pair_lap(value: object, name: str) -> dict[str, object]:
    lap = _tire_exact(value, _TIRE_PERFORMANCE_LAP_KEYS, name)
    _tire_identifier(lap.get("lap_id"), f"{name} id")
    _tire_int(lap.get("stint_age_laps"), f"{name} stint age")
    _tire_number(
        lap.get("lap_time_s"), f"{name} lap time", minimum=20.0, maximum=1200.0
    )
    _tire_number(
        lap.get("fuel_start_l"), f"{name} fuel start", minimum=0.0, maximum=500.0
    )
    return lap


def validate_matched_tire_performance_dataset(
    value: object,
    *,
    expected_dataset_sha256: str,
) -> dict[str, object]:
    """Validate disjoint, condition-matched tire-age pairs and their lineage."""

    dataset = _tire_exact(
        _tire_json_copy(value, "matched tire-performance dataset"),
        _TIRE_PERFORMANCE_DATASET_KEYS,
        "matched tire-performance dataset",
    )
    if dataset.get("contract_version") != (
        MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION
    ):
        _tire_fail(
            "DATASET_INVALID", "matched tire-performance contract is unsupported"
        )
    _tire_identifier(dataset.get("dataset_id"), "tire-performance dataset id")
    _tire_int(
        dataset.get("dataset_version"), "tire-performance dataset version", minimum=1
    )
    stored = _tire_sha256(
        dataset.get("dataset_sha256"), "tire-performance dataset SHA-256"
    )
    expected = _tire_sha256(
        expected_dataset_sha256, "expected tire-performance dataset SHA-256"
    )
    if stored != expected:
        _tire_fail(
            "DATASET_INVALID",
            "tire-performance dataset differs from independent digest",
        )
    material = {key: item for key, item in dataset.items() if key != "dataset_sha256"}
    if canonical_sha256(material) != stored:
        _tire_fail("DATASET_INVALID", "tire-performance dataset self hash differs")
    _validate_tire_identity(dataset.get("event_identity"))
    _tire_int(dataset.get("tire_compound"), "tire compound")
    _validate_tire_fuel_load_model(dataset.get("fuel_load_model"))

    samples = _tire_list(dataset.get("samples"), "tire-performance samples")
    if len(samples) < 3:
        _tire_fail(
            "INSUFFICIENT_MATCHED_STINTS",
            "at least three disjoint matched-stint pairs are required",
        )
    sample_ids: set[str] = set()
    stint_ids: set[str] = set()
    label_receipts: set[str] = set()
    condition_receipts: set[str] = set()
    lap_ids: set[str] = set()
    for index, raw in enumerate(samples):
        sample = _tire_exact(
            raw,
            _TIRE_PERFORMANCE_SAMPLE_KEYS,
            f"tire-performance samples[{index}]",
        )
        sample_id = _tire_identifier(
            sample.get("sample_id"), f"tire-performance sample {index} id"
        )
        stint_id = _tire_identifier(
            sample.get("stint_id"), f"tire-performance sample {index} stint id"
        )
        label = _tire_sha256(
            sample.get("label_receipt_sha256"),
            f"tire-performance sample {index} label receipt",
        )
        condition = _tire_sha256(
            sample.get("condition_match_receipt_sha256"),
            f"tire-performance sample {index} condition-match receipt",
        )
        _tire_sha256(
            sample.get("source_receipt_sha256"),
            f"tire-performance sample {index} source receipt",
        )
        if sample_id in sample_ids:
            _tire_fail("DATASET_INVALID", "tire-performance sample ids must be unique")
        if stint_id in stint_ids:
            _tire_fail(
                "DATASET_INVALID", "tire-performance stint ids must be independent"
            )
        if label in label_receipts:
            _tire_fail(
                "DATASET_INVALID", "tire-performance label receipts must be independent"
            )
        if condition in condition_receipts:
            _tire_fail(
                "DATASET_INVALID",
                "tire-performance condition receipts must be independent",
            )
        sample_ids.add(sample_id)
        stint_ids.add(stint_id)
        label_receipts.add(label)
        condition_receipts.add(condition)

        early = _validate_tire_pair_lap(
            sample.get("early_lap"), f"tire-performance sample {index} early lap"
        )
        late = _validate_tire_pair_lap(
            sample.get("late_lap"), f"tire-performance sample {index} late lap"
        )
        for lap in (early, late):
            lap_id = str(lap["lap_id"])
            if lap_id in lap_ids:
                _tire_fail(
                    "DATASET_INVALID",
                    "tire-performance lap ids must be globally disjoint",
                )
            lap_ids.add(lap_id)
        age_delta = int(late["stint_age_laps"]) - int(early["stint_age_laps"])
        if age_delta < 2:
            _tire_fail(
                "PAIR_INVALID",
                f"tire-performance sample {index} spans fewer than two completed laps",
            )
        if float(late["fuel_start_l"]) > float(early["fuel_start_l"]) + 1e-9:
            _tire_fail(
                "PAIR_INVALID",
                f"tire-performance sample {index} gains fuel within one stint",
            )
    return dataset


def build_tire_performance_model(
    dataset_value: object,
    *,
    expected_dataset_sha256: str,
) -> dict[str, object]:
    """Build a robust shadow tire-age slope without making a wear claim."""

    dataset = validate_matched_tire_performance_dataset(
        dataset_value,
        expected_dataset_sha256=expected_dataset_sha256,
    )
    identity = _validate_tire_identity(dataset["event_identity"])
    fuel_model = _validate_tire_fuel_load_model(dataset["fuel_load_model"])
    fuel_central = float(fuel_model["seconds_per_liter"])
    fuel_bounds = _tire_list(
        fuel_model["seconds_per_liter_uncertainty"],
        "fuel-load uncertainty",
    )
    fuel_low, fuel_high = map(float, fuel_bounds)
    central_slopes: list[float] = []
    lower_slopes: list[float] = []
    upper_slopes: list[float] = []
    maximum_age = 0
    samples = _tire_list(dataset["samples"], "tire-performance samples")
    for index, raw in enumerate(samples):
        sample = _tire_exact(
            raw, _TIRE_PERFORMANCE_SAMPLE_KEYS, "tire-performance sample"
        )
        early = _tire_exact(
            sample["early_lap"], _TIRE_PERFORMANCE_LAP_KEYS, "early tire lap"
        )
        late = _tire_exact(
            sample["late_lap"], _TIRE_PERFORMANCE_LAP_KEYS, "late tire lap"
        )
        age_delta = int(late["stint_age_laps"]) - int(early["stint_age_laps"])
        time_delta = float(late["lap_time_s"]) - float(early["lap_time_s"])
        fuel_delta = float(late["fuel_start_l"]) - float(early["fuel_start_l"])
        central = (time_delta - fuel_central * fuel_delta) / age_delta
        endpoints = (
            (time_delta - fuel_low * fuel_delta) / age_delta,
            (time_delta - fuel_high * fuel_delta) / age_delta,
        )
        low, high = min(endpoints), max(endpoints)
        if max(abs(low), abs(central), abs(high)) > 5.0:
            _tire_fail(
                "PAIR_INVALID",
                f"tire-performance sample {index} has an implausible age slope",
            )
        central_slopes.append(central)
        lower_slopes.append(low)
        upper_slopes.append(high)
        maximum_age = max(maximum_age, int(late["stint_age_laps"]))

    central_slope = round(float(median(central_slopes)), 6)
    low_slope = round(min(lower_slopes), 6)
    high_slope = round(max(upper_slopes), 6)
    if low_slope > 0.0:
        estimate_available = True
        status = "PASS_SHADOW_POSITIVE_DEGRADATION"
    elif high_slope <= 0.0:
        estimate_available = False
        status = "WAIT_POSITIVE_DEGRADATION_NOT_OBSERVED"
    else:
        estimate_available = False
        status = "WAIT_DEGRADATION_SIGN_AMBIGUOUS"
    material: dict[str, object] = {
        "advisor_only": True,
        "contract_version": TIRE_PERFORMANCE_MODEL_CONTRACT_VERSION,
        "estimate_available": estimate_available,
        "fuel_load_model_sha256": fuel_model["model_sha256"],
        "identity_sha256": canonical_sha256(identity),
        "independent_stint_count": len(samples),
        "max_supported_stint_age_laps": maximum_age,
        "method_version": TIRE_PERFORMANCE_METHOD_VERSION,
        "pair_count": len(samples),
        "performance_age_slope_s_per_lap": central_slope,
        "performance_age_slope_uncertainty_s_per_lap": [low_slope, high_slope],
        "physical_wear": dict(_TIRE_PHYSICAL_WEAR_UNAVAILABLE),
        "source_receipt_sha256": dataset["dataset_sha256"],
        "status": status,
        "tire_compound": dataset["tire_compound"],
    }
    model = {**material, "model_sha256": canonical_sha256(material)}
    return validate_tire_performance_model(
        model,
        expected_model_sha256=str(model["model_sha256"]),
        expected_identity_sha256=str(model["identity_sha256"]),
        expected_source_receipt_sha256=str(dataset["dataset_sha256"]),
    )


def validate_tire_performance_model(
    value: object,
    *,
    expected_model_sha256: str,
    expected_identity_sha256: str,
    expected_source_receipt_sha256: str,
) -> dict[str, object]:
    """Validate one persisted performance-age model and its no-wear boundary."""

    model = _tire_exact(
        _tire_json_copy(value, "tire-performance model"),
        _TIRE_PERFORMANCE_MODEL_KEYS,
        "tire-performance model",
    )
    if model.get("contract_version") != TIRE_PERFORMANCE_MODEL_CONTRACT_VERSION:
        _tire_fail("MODEL_INVALID", "tire-performance model contract is unsupported")
    if model.get("method_version") != TIRE_PERFORMANCE_METHOD_VERSION:
        _tire_fail("MODEL_INVALID", "tire-performance model method is unsupported")
    if model.get("advisor_only") is not True:
        _tire_fail("MODEL_INVALID", "tire-performance model is not advisor-only")
    stored = _tire_sha256(
        model.get("model_sha256"), "tire-performance model SHA-256"
    )
    if stored != _tire_sha256(
        expected_model_sha256, "expected tire-performance model SHA-256"
    ):
        _tire_fail(
            "MODEL_INVALID", "tire-performance model differs from independent digest"
        )
    material = {key: item for key, item in model.items() if key != "model_sha256"}
    if canonical_sha256(material) != stored:
        _tire_fail("MODEL_INVALID", "tire-performance model self hash differs")
    identity = _tire_sha256(
        model.get("identity_sha256"), "tire-performance identity SHA-256"
    )
    if identity != _tire_sha256(
        expected_identity_sha256, "expected tire-performance identity SHA-256"
    ):
        _tire_fail("MODEL_INVALID", "tire-performance model identity differs")
    source = _tire_sha256(
        model.get("source_receipt_sha256"), "tire-performance source receipt"
    )
    if source != _tire_sha256(
        expected_source_receipt_sha256,
        "expected tire-performance source receipt",
    ):
        _tire_fail("MODEL_INVALID", "tire-performance source receipt differs")
    _tire_sha256(
        model.get("fuel_load_model_sha256"),
        "tire-performance fuel-load model SHA-256",
    )
    pair_count = _tire_int(
        model.get("pair_count"), "tire-performance pair count", minimum=3
    )
    stint_count = _tire_int(
        model.get("independent_stint_count"),
        "tire-performance independent stint count",
        minimum=3,
    )
    if pair_count != stint_count:
        _tire_fail("MODEL_INVALID", "tire-performance pairs are not disjoint by stint")
    _tire_int(
        model.get("max_supported_stint_age_laps"),
        "maximum supported tire age",
        minimum=2,
    )
    _tire_int(model.get("tire_compound"), "tire-performance compound")
    central = _tire_number(
        model.get("performance_age_slope_s_per_lap"),
        "tire-performance age slope",
    )
    bounds = _tire_list(
        model.get("performance_age_slope_uncertainty_s_per_lap"),
        "tire-performance age-slope uncertainty",
    )
    if len(bounds) != 2:
        _tire_fail("MODEL_INVALID", "tire-performance uncertainty must have two bounds")
    low = _tire_number(bounds[0], "tire-performance slope low")
    high = _tire_number(bounds[1], "tire-performance slope high")
    if not low <= central <= high:
        _tire_fail("MODEL_INVALID", "tire-performance slope lies outside uncertainty")
    expected_status = (
        "PASS_SHADOW_POSITIVE_DEGRADATION"
        if low > 0.0
        else "WAIT_POSITIVE_DEGRADATION_NOT_OBSERVED"
        if high <= 0.0
        else "WAIT_DEGRADATION_SIGN_AMBIGUOUS"
    )
    expected_available = expected_status == "PASS_SHADOW_POSITIVE_DEGRADATION"
    if (
        model.get("status") != expected_status
        or model.get("estimate_available") is not expected_available
    ):
        _tire_fail("MODEL_INVALID", "tire-performance availability is not slope-derived")
    physical = _tire_exact(
        model.get("physical_wear"),
        _TIRE_PHYSICAL_WEAR_KEYS,
        "tire-performance physical-wear boundary",
    )
    if physical != _TIRE_PHYSICAL_WEAR_UNAVAILABLE:
        _tire_fail("MODEL_INVALID", "tire-performance model made a physical-wear claim")
    return model


def validate_tire_performance_belief(
    value: object,
    *,
    expected_belief_sha256: str,
    expected_model_sha256: str,
    expected_calibration_model_sha256: str,
    expected_identity_sha256: str,
    expected_source_receipt_sha256: str,
) -> dict[str, object]:
    """Validate one action-bound tire-service tradeoff projection."""

    belief = _tire_exact(
        _tire_json_copy(value, "tire-performance belief"),
        _TIRE_PERFORMANCE_BELIEF_KEYS,
        "tire-performance belief",
    )
    if belief.get("contract_version") != TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION:
        _tire_fail("BELIEF_INVALID", "tire-performance belief contract is unsupported")
    if belief.get("method_version") != TIRE_PERFORMANCE_BELIEF_METHOD_VERSION:
        _tire_fail("BELIEF_INVALID", "tire-performance belief method is unsupported")
    if belief.get("advisor_only") is not True:
        _tire_fail("BELIEF_INVALID", "tire-performance belief is not advisor-only")
    stored = _tire_sha256(
        belief.get("belief_sha256"), "tire-performance belief SHA-256"
    )
    if stored != _tire_sha256(
        expected_belief_sha256, "expected tire-performance belief SHA-256"
    ):
        _tire_fail(
            "BELIEF_INVALID", "tire-performance belief differs from independent digest"
        )
    material = {key: item for key, item in belief.items() if key != "belief_sha256"}
    if canonical_sha256(material) != stored:
        _tire_fail("BELIEF_INVALID", "tire-performance belief self hash differs")
    for key, expected, label in (
        ("model_sha256", expected_model_sha256, "model"),
        (
            "calibration_model_sha256",
            expected_calibration_model_sha256,
            "calibration model",
        ),
        ("identity_sha256", expected_identity_sha256, "identity"),
        ("source_receipt_sha256", expected_source_receipt_sha256, "source receipt"),
    ):
        if belief.get(key) != _tire_sha256(expected, f"expected tire belief {label}"):
            _tire_fail("BELIEF_INVALID", f"tire-performance belief {label} differs")
    physical = _tire_exact(
        belief.get("physical_wear"),
        _TIRE_PHYSICAL_WEAR_KEYS,
        "tire-performance belief physical-wear boundary",
    )
    if physical != _TIRE_PHYSICAL_WEAR_UNAVAILABLE:
        _tire_fail("BELIEF_INVALID", "tire-performance belief made a wear claim")
    reasons = _tire_list(belief.get("reason_codes"), "tire belief reasons")
    if any(type(item) is not str or not item for item in reasons) or reasons != list(
        dict.fromkeys(reasons)
    ):
        _tire_fail("BELIEF_INVALID", "tire-performance belief reasons are invalid")
    scenario = _tire_exact(
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
        _tire_int(scenario.get(key), f"tire belief scenario {key}")
    _tire_sha256(
        scenario.get("current_stint_context_sha256"),
        "tire belief current-stint context SHA-256",
    )
    _tire_number(
        scenario.get("fuel_add_l"), "tire belief fuel addition", minimum=0.0
    )
    _tire_number(
        scenario.get("incremental_tire_service_s"),
        "tire belief incremental service",
        minimum=0.0,
    )
    if scenario.get("fuel_tire_service_timing") not in {"PARALLEL", "SEQUENTIAL"}:
        _tire_fail("BELIEF_INVALID", "tire belief service timing is invalid")
    loss_range = scenario.get("keep_tires_time_loss_range_s")
    if loss_range is not None:
        values = _tire_list(loss_range, "tire belief keep-tires loss range")
        if len(values) != 2:
            _tire_fail("BELIEF_INVALID", "keep-tires loss range must have two bounds")
        low = _tire_number(values[0], "keep-tires loss low", minimum=0.0)
        high = _tire_number(values[1], "keep-tires loss high", minimum=0.0)
        if high < low:
            _tire_fail("BELIEF_INVALID", "keep-tires loss range is reversed")
    preference = belief.get("performance_preference")
    status = belief.get("status")
    available = belief.get("estimate_available")
    valid_boundaries = {
        "PASS_SHADOW_CHANGE_TIRES": (True, "CHANGE_TIRES", []),
        "WAIT_PHYSICAL_WEAR_FOR_NO_TIRE_SERVICE": (
            True,
            "KEEP_TIRES",
            ["CURRENT_PHYSICAL_WEAR_REQUIRED_FOR_KEEP_TIRES"],
        ),
        "WAIT_PERFORMANCE_SERVICE_TRADEOFF": (
            True,
            "AMBIGUOUS",
            ["TIRE_SERVICE_TRADEOFF_INTERVAL_OVERLAP"],
        ),
    }
    if status in valid_boundaries:
        expected_available, expected_preference, expected_reasons = valid_boundaries[
            str(status)
        ]
        if (
            available is not expected_available
            or preference != expected_preference
            or reasons != expected_reasons
            or loss_range is None
        ):
            _tire_fail("BELIEF_INVALID", "tire belief decision boundary is invalid")
    elif (
        type(status) is str
        and status.startswith("WAIT_")
        and available is False
        and preference == "WAIT"
        and reasons
        and loss_range is None
    ):
        pass
    else:
        _tire_fail("BELIEF_INVALID", "tire belief availability/status is invalid")
    return belief


def build_tire_performance_belief(
    model_value: object,
    calibration_model_value: object,
    *,
    expected_model_sha256: str,
    expected_model_source_receipt_sha256: str,
    expected_calibration_model_sha256: str,
    expected_calibration_source_receipt_sha256: str,
    expected_identity_sha256: str,
    current_stint_context_sha256: str,
    current_source_receipt_sha256: str,
    current_stint_age_laps: int,
    current_tire_compound: int,
    laps_until_pit: int,
    laps_after_pit: int,
    fuel_add_l: float,
    fuel_tire_service_timing: str,
) -> dict[str, object]:
    """Compare new-tire service time with an age-slope performance envelope.

    A performance preference for keeping the current set remains blocked until
    a separate physical-wear safety belief exists. A clear preference for a new
    set can pass in shadow because it does not depend on retaining an unknown
    current physical state.
    """

    model = validate_tire_performance_model(
        model_value,
        expected_model_sha256=expected_model_sha256,
        expected_identity_sha256=expected_identity_sha256,
        expected_source_receipt_sha256=expected_model_source_receipt_sha256,
    )
    try:
        calibration = validate_matched_pit_calibration_model(
            calibration_model_value,
            expected_model_sha256=expected_calibration_model_sha256,
            expected_identity_sha256=expected_identity_sha256,
            expected_source_receipt_sha256=(
                expected_calibration_source_receipt_sha256
            ),
        )
    except PitCalibrationError as exc:
        raise TirePerformanceError(
            "CALIBRATION_INVALID",
            f"tire belief calibration failed closed: {exc.code}: {exc}",
        ) from exc
    identity_sha = _tire_sha256(
        expected_identity_sha256, "expected tire belief identity SHA-256"
    )
    context_sha = _tire_sha256(
        current_stint_context_sha256, "current tire-stint context SHA-256"
    )
    current_source = _tire_sha256(
        current_source_receipt_sha256, "current tire-stint source receipt"
    )
    current_age = _tire_int(
        current_stint_age_laps, "current tire-stint age"
    )
    current_compound = _tire_int(current_tire_compound, "current tire compound")
    until_pit = _tire_int(laps_until_pit, "laps until pit")
    after_pit = _tire_int(laps_after_pit, "laps after pit")
    fuel_add = _tire_number(fuel_add_l, "tire belief fuel addition", minimum=0.0)
    if fuel_tire_service_timing not in {"PARALLEL", "SEQUENTIAL"}:
        _tire_fail("SCENARIO_INVALID", "tire belief service timing is unsupported")
    fuel_time = fuel_add / float(calibration["refuel_rate_l_per_s"])
    tire_time = float(calibration["tire_change_time_s"])
    no_tire_stationary = fuel_time
    tire_stationary = (
        fuel_time + tire_time
        if fuel_tire_service_timing == "SEQUENTIAL"
        else max(fuel_time, tire_time)
    )
    incremental_service = round(tire_stationary - no_tire_stationary, 6)
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
        slope_bounds = _tire_list(
            model["performance_age_slope_uncertainty_s_per_lap"],
            "tire-performance model slope uncertainty",
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
        "current_stint_context_sha256": context_sha,
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
        "identity_sha256": identity_sha,
        "method_version": TIRE_PERFORMANCE_BELIEF_METHOD_VERSION,
        "model_sha256": model["model_sha256"],
        "performance_preference": preference,
        "physical_wear": dict(_TIRE_PHYSICAL_WEAR_UNAVAILABLE),
        "reason_codes": reasons,
        "scenario": scenario,
        "source_receipt_sha256": current_source,
        "status": status,
    }
    belief = {**material, "belief_sha256": canonical_sha256(material)}
    return validate_tire_performance_belief(
        belief,
        expected_belief_sha256=str(belief["belief_sha256"]),
        expected_model_sha256=str(model["model_sha256"]),
        expected_calibration_model_sha256=str(calibration["model_sha256"]),
        expected_identity_sha256=identity_sha,
        expected_source_receipt_sha256=current_source,
    )


def _read_calibration_dataset(path: Path) -> dict[str, object]:
    def unique(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        payload = path.read_bytes()
        if len(payload) > _CALIBRATION_MAX_INPUT_BYTES:
            _calibration_fail(
                "INPUT_TOO_LARGE",
                f"calibration dataset exceeds {_CALIBRATION_MAX_INPUT_BYTES} bytes",
            )
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except PitCalibrationError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise PitCalibrationError(
            "INPUT_READ_FAILED", f"cannot read calibration dataset: {exc}"
        ) from exc
    if type(value) is not dict:
        _calibration_fail("SCHEMA_INVALID", "calibration dataset must be an object")
    return value


def _read_tire_performance_dataset(path: Path) -> dict[str, object]:
    def unique(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        payload = path.read_bytes()
        if len(payload) > _CALIBRATION_MAX_INPUT_BYTES:
            _tire_fail(
                "INPUT_TOO_LARGE",
                f"tire-performance dataset exceeds {_CALIBRATION_MAX_INPUT_BYTES} bytes",
            )
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except TirePerformanceError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise TirePerformanceError(
            "INPUT_READ_FAILED", f"cannot read tire-performance dataset: {exc}"
        ) from exc
    if type(value) is not dict:
        _tire_fail("SCHEMA_INVALID", "tire-performance dataset must be an object")
    return value


def write_matched_pit_calibration_model_exclusive(
    dataset_path: str | Path,
    output_path: str | Path,
    *,
    expected_dataset_sha256: str,
) -> dict[str, object]:
    dataset = _read_calibration_dataset(Path(dataset_path))
    model = build_matched_pit_calibration_model(
        dataset,
        expected_dataset_sha256=expected_dataset_sha256,
    )
    output = Path(output_path)
    if output.name in {"", ".", ".."} or output.exists() or output.is_symlink():
        _calibration_fail("OUTPUT_CREATE_FAILED", "calibration model output is not new")
    try:
        _write_bytes_exclusive(
            output,
            _persisted_json(model),
            "matched pit calibration model",
        )
    except RetrievedLiveAnalysisError as exc:
        raise PitCalibrationError(exc.code, str(exc)) from exc
    return model


def write_tire_performance_model_exclusive(
    dataset_path: str | Path,
    output_path: str | Path,
    *,
    expected_dataset_sha256: str,
) -> dict[str, object]:
    """Validate a pinned dataset and CreateNew-write one tire model."""

    dataset = _read_tire_performance_dataset(Path(dataset_path))
    model = build_tire_performance_model(
        dataset,
        expected_dataset_sha256=expected_dataset_sha256,
    )
    output = Path(output_path)
    if output.name in {"", ".", ".."} or output.exists() or output.is_symlink():
        _tire_fail("OUTPUT_CREATE_FAILED", "tire-performance model output is not new")
    try:
        _write_bytes_exclusive(
            output,
            _persisted_json(model),
            "tire-performance model",
        )
    except RetrievedLiveAnalysisError as exc:
        raise TirePerformanceError(exc.code, str(exc)) from exc
    return model


def write_retrieved_live_analysis_profile_exclusive(
    path: str | Path,
    profile: Mapping[str, object],
    *,
    expected_analysis_profile_sha256: str,
) -> dict[str, object]:
    """Validate and CreateNew-write one canonical analysis profile."""

    validated = validate_retrieved_live_analysis_profile(
        profile,
        expected_analysis_profile_sha256=expected_analysis_profile_sha256,
    )
    output = Path(path)
    if output.name in {"", ".", ".."} or output.exists() or output.is_symlink():
        _fail("OUTPUT_CREATE_FAILED", "analysis profile output is not new")
    _write_bytes_exclusive(output, _persisted_json(validated), "analysis profile")
    return validated


def write_retrieved_live_analysis_bundle_exclusive(
    capture_handle: object,
    live_session: Mapping[str, object],
    analysis_profile: Mapping[str, object],
    engineer_session_path: str | Path,
    report_artifact_path: str | Path,
    report_html_path: str | Path,
    bundle_receipt_path: str | Path,
    *,
    expected_live_engineer_session_sha256: str,
    expected_remote_capture_sha256: str,
    expected_remote_capture_byte_size: int,
    expected_analysis_profile_sha256: str,
    calibration_model: Mapping[str, object] | None = None,
    expected_calibration_model_sha256: str | None = None,
    expected_calibration_source_receipt_sha256: str | None = None,
    tire_performance_model: Mapping[str, object] | None = None,
    expected_tire_performance_model_sha256: str | None = None,
    expected_tire_performance_source_receipt_sha256: str | None = None,
    rules_profile: Mapping[str, object] | None = None,
    expected_rules_profile_sha256: str | None = None,
    expected_rules_source_sha256: str | None = None,
    previous_m2_receipt: Mapping[str, object] | None = None,
    expected_previous_m2_sha256: str | None = None,
    expected_previous_revision: int | None = None,
    stale_after_s: float = 0.5,
) -> dict[str, object]:
    """Build and CreateNew-write a complete SDK-live analysis bundle."""

    outputs = tuple(
        Path(value)
        for value in (
            engineer_session_path,
            report_artifact_path,
            report_html_path,
            bundle_receipt_path,
        )
    )
    _validate_bundle_paths(outputs)
    profile = validate_retrieved_live_analysis_profile(
        analysis_profile,
        expected_analysis_profile_sha256=expected_analysis_profile_sha256,
    )
    session, report, live, context_inputs = _build_analysis(
        capture_handle,
        live_session,
        profile,
        expected_live_engineer_session_sha256=(
            expected_live_engineer_session_sha256
        ),
        expected_remote_capture_sha256=expected_remote_capture_sha256,
        expected_remote_capture_byte_size=expected_remote_capture_byte_size,
        expected_analysis_profile_sha256=expected_analysis_profile_sha256,
        calibration_model=calibration_model,
        expected_calibration_model_sha256=expected_calibration_model_sha256,
        expected_calibration_source_receipt_sha256=(
            expected_calibration_source_receipt_sha256
        ),
        tire_performance_model=tire_performance_model,
        expected_tire_performance_model_sha256=(
            expected_tire_performance_model_sha256
        ),
        expected_tire_performance_source_receipt_sha256=(
            expected_tire_performance_source_receipt_sha256
        ),
        rules_profile=rules_profile,
        expected_rules_profile_sha256=expected_rules_profile_sha256,
        expected_rules_source_sha256=expected_rules_source_sha256,
        previous_m2_receipt=previous_m2_receipt,
        expected_previous_m2_sha256=expected_previous_m2_sha256,
        expected_previous_revision=expected_previous_revision,
        stale_after_s=stale_after_s,
    )
    session_bytes = _persisted_json(session)
    report_bytes = _persisted_json(report)
    html_bytes = render_engineer_session_report_html(
        report,
        session,
        expected_report_sha256=str(report["report_sha256"]),
        expected_engineer_session_sha256=str(session["engineer_session_sha256"]),
    )
    receipt = validate_retrieved_live_analysis_receipt(
        _build_bundle_receipt(
            profile=profile,
            live=live,
            session=session,
            report=report,
            context_inputs=context_inputs,
            session_bytes=session_bytes,
            report_bytes=report_bytes,
            html_bytes=html_bytes,
        )
    )
    try:
        write_engineer_session_exclusive(outputs[0], session)
        write_engineer_session_report_bundle_exclusive(
            outputs[1],
            outputs[2],
            report,
            session,
            expected_report_sha256=str(report["report_sha256"]),
            expected_engineer_session_sha256=str(
                session["engineer_session_sha256"]
            ),
        )
        _write_bytes_exclusive(
            outputs[3], _persisted_json(receipt), "analysis bundle receipt"
        )
    except (
        EngineerSessionError,
        EngineerSessionReportError,
        RetrievedLiveAnalysisError,
        OSError,
        ValueError,
    ):
        # Successfully created earlier files remain as forensic residues.  No
        # pathname is unlinked after a potential directory-entry race.
        raise
    return receipt


def verify_retrieved_live_analysis_bundle(
    capture_handle: object,
    live_session: Mapping[str, object],
    analysis_profile: Mapping[str, object],
    engineer_session_bytes: bytes,
    report_artifact_bytes: bytes,
    report_html_bytes: bytes,
    bundle_receipt: Mapping[str, object],
    *,
    expected_live_engineer_session_sha256: str,
    expected_remote_capture_sha256: str,
    expected_remote_capture_byte_size: int,
    expected_analysis_profile_sha256: str,
    expected_bundle_receipt_sha256: str,
    calibration_model: Mapping[str, object] | None = None,
    expected_calibration_model_sha256: str | None = None,
    expected_calibration_source_receipt_sha256: str | None = None,
    tire_performance_model: Mapping[str, object] | None = None,
    expected_tire_performance_model_sha256: str | None = None,
    expected_tire_performance_source_receipt_sha256: str | None = None,
    rules_profile: Mapping[str, object] | None = None,
    expected_rules_profile_sha256: str | None = None,
    expected_rules_source_sha256: str | None = None,
    previous_m2_receipt: Mapping[str, object] | None = None,
    expected_previous_m2_sha256: str | None = None,
    expected_previous_revision: int | None = None,
    stale_after_s: float = 0.5,
) -> dict[str, object]:
    """Object-exactly replay every semantic and byte binding in one bundle."""

    profile = validate_retrieved_live_analysis_profile(
        analysis_profile,
        expected_analysis_profile_sha256=expected_analysis_profile_sha256,
    )
    receipt = validate_retrieved_live_analysis_receipt(
        bundle_receipt,
        expected_bundle_receipt_sha256=expected_bundle_receipt_sha256,
    )
    persisted_session = _strict_json_bytes(
        engineer_session_bytes, "persisted engineer session"
    )
    persisted_report = _strict_json_bytes(
        report_artifact_bytes, "persisted report artifact"
    )
    rebuilt_session, rebuilt_report, live, context_inputs = _build_analysis(
        capture_handle,
        live_session,
        profile,
        expected_live_engineer_session_sha256=(
            expected_live_engineer_session_sha256
        ),
        expected_remote_capture_sha256=expected_remote_capture_sha256,
        expected_remote_capture_byte_size=expected_remote_capture_byte_size,
        expected_analysis_profile_sha256=expected_analysis_profile_sha256,
        calibration_model=calibration_model,
        expected_calibration_model_sha256=expected_calibration_model_sha256,
        expected_calibration_source_receipt_sha256=(
            expected_calibration_source_receipt_sha256
        ),
        tire_performance_model=tire_performance_model,
        expected_tire_performance_model_sha256=(
            expected_tire_performance_model_sha256
        ),
        expected_tire_performance_source_receipt_sha256=(
            expected_tire_performance_source_receipt_sha256
        ),
        rules_profile=rules_profile,
        expected_rules_profile_sha256=expected_rules_profile_sha256,
        expected_rules_source_sha256=expected_rules_source_sha256,
        previous_m2_receipt=previous_m2_receipt,
        expected_previous_m2_sha256=expected_previous_m2_sha256,
        expected_previous_revision=expected_previous_revision,
        stale_after_s=stale_after_s,
    )
    if persisted_session != rebuilt_session:
        _fail("SESSION_REPLAY_MISMATCH", "engineer session is not object-exact")
    validated_report = validate_engineer_session_report(
        persisted_report,
        persisted_session,
        expected_report_sha256=str(persisted_report.get("report_sha256")),
        expected_engineer_session_sha256=str(
            persisted_session.get("engineer_session_sha256")
        ),
    )
    if validated_report != rebuilt_report:
        _fail("REPORT_REPLAY_MISMATCH", "report artifact is not object-exact")
    expected_html = render_engineer_session_report_html(
        validated_report,
        persisted_session,
        expected_report_sha256=str(validated_report["report_sha256"]),
        expected_engineer_session_sha256=str(
            persisted_session["engineer_session_sha256"]
        ),
    )
    if report_html_bytes != expected_html:
        _fail("REPORT_REPLAY_MISMATCH", "report HTML is not byte-exact")
    expected_receipt = _build_bundle_receipt(
        profile=profile,
        live=live,
        session=persisted_session,
        report=validated_report,
        context_inputs=context_inputs,
        session_bytes=engineer_session_bytes,
        report_bytes=report_artifact_bytes,
        html_bytes=report_html_bytes,
    )
    if receipt != expected_receipt:
        _fail("BUNDLE_REPLAY_MISMATCH", "bundle receipt does not reproduce exactly")
    return receipt


__all__ = [
    "MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION",
    "MATCHED_PIT_CALIBRATION_METHOD_VERSION",
    "MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION",
    "PitCalibrationError",
    "RETRIEVED_LIVE_ANALYSIS_BUNDLE_CONTRACT_VERSION",
    "RETRIEVED_LIVE_ANALYSIS_PROFILE_CONTRACT_VERSION",
    "RetrievedLiveAnalysisError",
    "TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION",
    "TIRE_PERFORMANCE_BELIEF_METHOD_VERSION",
    "TIRE_PERFORMANCE_METHOD_VERSION",
    "TIRE_PERFORMANCE_MODEL_CONTRACT_VERSION",
    "TIRE_STINT_CONTEXT_CONTRACT_VERSION",
    "TIME_DOMAIN_REJOIN_ESTIMATE_CONTRACT_VERSION",
    "TIME_DOMAIN_REJOIN_METHOD_VERSION",
    "TRAFFIC_MOTION_CONTEXT_CONTRACT_VERSION",
    "build_matched_pit_calibration_model",
    "build_retrieved_live_analysis_profile",
    "build_tire_performance_belief",
    "build_tire_performance_model",
    "build_time_domain_rejoin_estimate",
    "TirePerformanceError",
    "validate_retrieved_live_analysis_profile",
    "validate_retrieved_live_analysis_receipt",
    "validate_matched_pit_calibration_dataset",
    "validate_matched_pit_calibration_model",
    "validate_matched_tire_performance_dataset",
    "validate_tire_performance_belief",
    "validate_tire_performance_model",
    "validate_tire_stint_context",
    "validate_time_domain_rejoin_estimate",
    "validate_traffic_motion_context",
    "verify_retrieved_live_analysis_bundle",
    "write_retrieved_live_analysis_bundle_exclusive",
    "write_retrieved_live_analysis_profile_exclusive",
    "write_matched_pit_calibration_model_exclusive",
    "write_tire_performance_model_exclusive",
]
