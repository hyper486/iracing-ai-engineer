"""Fail-closed condition matching for one adapter-bound telemetry run.

The matcher deliberately separates directly comparable conditions from
unavailable inference.  Tire telemetry compares the same compound at nearby
relative stint ages; cross-stint comparisons require observed pit-exit and
set-change boundaries.  ``TireSetsUsed`` is boundary evidence, never a current
wear estimate.  Opponent arrays are used only as an observed
longitudinal-proximity exclusion gate; they never estimate traffic loss,
opponent fuel, or rejoin outcomes.  A condition-matching ``PASS`` is a shadow
data-comparability result: self-attested human labels remain blocked by the
separate track-state authenticity gate.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from statistics import median
from typing import Any, Literal

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
from .laps import LapObservation, segment_laps
from .model_replay import (
    FuelModelReplayError,
    _input_quality_reasons,
    _quality_status,
    _sample_identity,
    _validate_input_evidence,
)
from .telemetry import (
    TELEMETRY_CONTRACT_VERSION,
    OpponentSet,
    Presence,
    QualityStatus,
    TelemetryField,
    TelemetrySample,
)

CONDITION_COHORT_CONTRACT_VERSION = "condition-cohort-v1"
CONDITION_LABEL_SET_CONTRACT_VERSION = "condition-label-set-v1"
CONDITION_FEATURE_PIPELINE_VERSION = "normalized-condition-cohort-v1"
HUMAN_TRACK_STATE_ATTESTATION = (
    "I attest that a human reviewed the cited evidence artifact and assigned "
    "these track-state labels without using model output."
)

_TRACK_STATE_LABELS = frozenset({"DRY_STABLE", "DAMP", "WET", "CHANGING"})
_INCIDENT_CHANNELS = (
    "PlayerCarMyIncidentCount",
    "PlayerCarDriverIncidentCount",
    "PlayerCarTeamIncidentCount",
)
_SEGMENT_REQUIRED = (
    "SessionTime",
    "SessionTick",
    "Lap",
    "LapDistPct",
    "Speed",
    "OnPitRoad",
    "PlayerTrackSurface",
)
_CONDITION_FIELDS = (
    "TrackTempCrew",
    "AirTemp",
    "Precipitation",
    "WindVel",
    "WindDir",
    "FuelLevelPct",
    "PlayerTireCompound",
    "TireSetsUsed",
)
_ALL_FIELDS = _SEGMENT_REQUIRED + ("LapCompleted",) + _INCIDENT_CHANNELS + (_CONDITION_FIELDS)
_FAIL_INPUT_REASONS = frozenset(
    {
        "CAPTURE_CLOCK_REGRESSION",
        "DRIVER_INFO_PERSISTED",
        "DROPPED_TICKS",
        "DUPLICATE_CONFLICTS",
        "INCIDENT_COUNT_INVALID",
        "INCIDENT_COUNT_REGRESSION",
        "LAP_SEGMENTATION_FAILED",
        "NORMALIZED_REJECTED_SAMPLES",
        "SCHEMA_CHANGED",
        "SDK_READ_ERRORS",
        "SEGMENTATION_CHANNEL_INVALID",
        "SESSION_RESET",
        "SOURCE_STALE_EVENTS",
    }
)


class ConditionCohortError(ValueError):
    """Raised when provenance or caller inputs violate the cohort contract."""


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
        raise ConditionCohortError("condition cohort value is not canonical-JSON-safe") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validate_sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ConditionCohortError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _plain_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ConditionCohortError(
            f"{name} must be a plain integer greater than or equal to {minimum}"
        )
    return value


@dataclass(frozen=True, slots=True)
class ConditionCohortConfig:
    """Frozen integer-unit thresholds for target-lap matching."""

    min_matched_laps: int = 8
    track_temp_tolerance_millic: int = 2_000
    air_temp_tolerance_millic: int = 2_000
    wind_vector_tolerance_mmps: int = 2_000
    dry_precipitation_max_ppm: int = 1_000
    fuel_start_tolerance_ppm: int = 50_000
    fuel_refuel_jump_tolerance_ppm: int = 100
    traffic_clearance_mm: int = 100_000
    max_tire_usage_lap_delta: int = 1

    def __post_init__(self) -> None:
        _plain_int(self.min_matched_laps, "min_matched_laps", minimum=2)
        for name in (
            "track_temp_tolerance_millic",
            "air_temp_tolerance_millic",
            "wind_vector_tolerance_mmps",
            "dry_precipitation_max_ppm",
            "fuel_start_tolerance_ppm",
            "fuel_refuel_jump_tolerance_ppm",
            "max_tire_usage_lap_delta",
        ):
            _plain_int(getattr(self, name), name)
        _plain_int(self.traffic_clearance_mm, "traffic_clearance_mm", minimum=1)
        if self.dry_precipitation_max_ppm > 1_000_000:
            raise ConditionCohortError("dry_precipitation_max_ppm must not exceed 1000000")
        if self.fuel_start_tolerance_ppm > 1_000_000:
            raise ConditionCohortError("fuel_start_tolerance_ppm must not exceed 1000000")
        if self.fuel_refuel_jump_tolerance_ppm > 1_000_000:
            raise ConditionCohortError("fuel_refuel_jump_tolerance_ppm must not exceed 1000000")


@dataclass(frozen=True, slots=True)
class ApprovedTrackStateLabelSet:
    """Source-bound human-reviewed track-state labels at lap ordinal grain."""

    source_binding_sha256: str
    lap_labels: tuple[tuple[int, str], ...]
    reviewer_id: str
    reviewed_at_utc: str
    method: Literal["MANUAL_REPLAY_REVIEW"]
    evidence_artifact_sha256: str
    human_attestation: str
    approval_status: Literal["APPROVED"] = "APPROVED"
    authenticity_status: Literal["SELF_ATTESTED_NOT_AUTHENTICATED"] = (
        "SELF_ATTESTED_NOT_AUTHENTICATED"
    )
    provenance: Literal["HUMAN_REVIEW"] = "HUMAN_REVIEW"
    contract_version: str = CONDITION_LABEL_SET_CONTRACT_VERSION
    label_set_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_sha256(
            self.source_binding_sha256,
            "track-state label source_binding_sha256",
        )
        _validate_sha256(
            self.evidence_artifact_sha256,
            "track-state label evidence_artifact_sha256",
        )
        if (
            type(self.reviewer_id) is not str
            or not self.reviewer_id
            or self.reviewer_id != self.reviewer_id.strip()
            or len(self.reviewer_id.encode("utf-8")) > 128
            or any(ord(character) < 32 for character in self.reviewer_id)
        ):
            raise ConditionCohortError("track-state reviewer_id is invalid")
        if type(self.reviewed_at_utc) is not str or not self.reviewed_at_utc.endswith("Z"):
            raise ConditionCohortError(
                "track-state reviewed_at_utc must be canonical UTC ending in Z"
            )
        try:
            reviewed_at = datetime.fromisoformat(self.reviewed_at_utc.removesuffix("Z") + "+00:00")
        except ValueError as exc:
            raise ConditionCohortError("track-state reviewed_at_utc is invalid") from exc
        canonical_reviewed_at = (
            reviewed_at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        if canonical_reviewed_at != self.reviewed_at_utc:
            raise ConditionCohortError(
                "track-state reviewed_at_utc must use second precision in UTC"
            )
        if self.method != "MANUAL_REPLAY_REVIEW":
            raise ConditionCohortError("track-state label method must be MANUAL_REPLAY_REVIEW")
        if self.human_attestation != HUMAN_TRACK_STATE_ATTESTATION:
            raise ConditionCohortError("track-state label set requires the fixed human attestation")
        if self.approval_status != "APPROVED":
            raise ConditionCohortError("track-state label set must be APPROVED")
        if self.authenticity_status != "SELF_ATTESTED_NOT_AUTHENTICATED":
            raise ConditionCohortError("track-state label set authenticity_status is invalid")
        if self.provenance != "HUMAN_REVIEW":
            raise ConditionCohortError("track-state label set provenance must be HUMAN_REVIEW")
        if self.contract_version != CONDITION_LABEL_SET_CONTRACT_VERSION:
            raise ConditionCohortError("track-state label contract version is invalid")
        if type(self.lap_labels) is not tuple:
            raise ConditionCohortError("track-state lap_labels must be a tuple")
        normalized: list[tuple[int, str]] = []
        for item in self.lap_labels:
            if type(item) is not tuple or len(item) != 2:
                raise ConditionCohortError("track-state labels must be (lap_ordinal, label) tuples")
            ordinal, label = item
            _plain_int(ordinal, "track-state lap ordinal")
            if type(label) is not str or label not in _TRACK_STATE_LABELS:
                raise ConditionCohortError("track-state label is invalid")
            normalized.append((ordinal, label))
        if normalized != sorted(normalized) or len({x[0] for x in normalized}) != len(normalized):
            raise ConditionCohortError("track-state labels must be sorted with unique lap ordinals")
        material = {
            "approval_status": self.approval_status,
            "authenticity_status": self.authenticity_status,
            "contract_version": self.contract_version,
            "evidence_artifact_sha256": self.evidence_artifact_sha256,
            "human_attestation": self.human_attestation,
            "lap_labels": [list(item) for item in self.lap_labels],
            "method": self.method,
            "provenance": self.provenance,
            "reviewed_at_utc": self.reviewed_at_utc,
            "reviewer_id": self.reviewer_id,
            "source_binding_sha256": self.source_binding_sha256,
        }
        object.__setattr__(self, "label_set_sha256", _digest(material))

    @classmethod
    def approved(
        cls,
        *,
        source_binding_sha256: str,
        labels: Mapping[int, str],
        reviewer_id: str,
        reviewed_at_utc: str,
        method: Literal["MANUAL_REPLAY_REVIEW"],
        evidence_artifact_sha256: str,
        human_attestation: str,
    ) -> ApprovedTrackStateLabelSet:
        if not isinstance(labels, Mapping):
            raise TypeError("labels must be a mapping")
        return cls(
            source_binding_sha256=source_binding_sha256,
            lap_labels=tuple(sorted(labels.items())),
            reviewer_id=reviewer_id,
            reviewed_at_utc=reviewed_at_utc,
            method=method,
            evidence_artifact_sha256=evidence_artifact_sha256,
            human_attestation=human_attestation,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ApprovedTrackStateLabelSet:
        """Validate one exact JSON-compatible label-set artifact."""

        if type(value) is not dict:
            raise ConditionCohortError("track-state label artifact must be one JSON object")
        expected_keys = {
            "approval_status",
            "authenticity_status",
            "contract_version",
            "evidence_artifact_sha256",
            "human_attestation",
            "label_set_sha256",
            "lap_labels",
            "method",
            "provenance",
            "reviewed_at_utc",
            "reviewer_id",
            "source_binding_sha256",
        }
        actual_keys = set(value)
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            raise ConditionCohortError(
                f"track-state label artifact keys mismatch; missing={missing}, extra={extra}"
            )
        raw_labels = value["lap_labels"]
        if type(raw_labels) is not list:
            raise ConditionCohortError("track-state lap_labels must be a JSON array")
        lap_labels: list[tuple[int, str]] = []
        for item in raw_labels:
            if type(item) is not list or len(item) != 2:
                raise ConditionCohortError("track-state labels must be two-item JSON arrays")
            lap_labels.append((item[0], item[1]))
        artifact = cls(
            source_binding_sha256=value["source_binding_sha256"],
            lap_labels=tuple(lap_labels),
            reviewer_id=value["reviewer_id"],
            reviewed_at_utc=value["reviewed_at_utc"],
            method=value["method"],
            evidence_artifact_sha256=value["evidence_artifact_sha256"],
            human_attestation=value["human_attestation"],
            approval_status=value["approval_status"],
            authenticity_status=value["authenticity_status"],
            provenance=value["provenance"],
            contract_version=value["contract_version"],
        )
        supplied_sha256 = _validate_sha256(
            value["label_set_sha256"], "track-state label label_set_sha256"
        )
        if supplied_sha256 != artifact.label_set_sha256:
            raise ConditionCohortError("track-state label_set_sha256 mismatch")
        return artifact

    def to_dict(self) -> dict[str, object]:
        return {
            "approval_status": self.approval_status,
            "authenticity_status": self.authenticity_status,
            "contract_version": self.contract_version,
            "evidence_artifact_sha256": self.evidence_artifact_sha256,
            "human_attestation": self.human_attestation,
            "label_set_sha256": self.label_set_sha256,
            "lap_labels": [list(item) for item in self.lap_labels],
            "method": self.method,
            "provenance": self.provenance,
            "reviewed_at_utc": self.reviewed_at_utc,
            "reviewer_id": self.reviewer_id,
            "source_binding_sha256": self.source_binding_sha256,
        }

    def label_for(self, lap_ordinal: int) -> str | None:
        return dict(self.lap_labels).get(lap_ordinal)


def _field_snapshot(field: TelemetryField[Any]) -> tuple[str, object | None]:
    value = field.value
    if hasattr(value, "value") and type(value).__module__ == "enum":
        value = value.value
    return field.presence.value, value


def _sample_fields(
    sample: TelemetrySample,
) -> dict[str, tuple[str, object | None]]:
    fields = {
        "SessionTime": sample.session.session_time_s,
        "SessionTick": sample.session.session_tick,
        "Lap": sample.lap.lap_number,
        "LapCompleted": sample.lap.laps_completed,
        "LapDistPct": sample.lap.lap_distance_pct,
        "Speed": sample.lap.speed_mps,
        "OnPitRoad": sample.pit.on_pit_road,
        "PlayerTrackSurface": sample.flags.player_track_surface,
        "PlayerCarMyIncidentCount": (sample.incidents.player_car_my_incident_count),
        "PlayerCarDriverIncidentCount": (sample.incidents.player_car_driver_incident_count),
        "PlayerCarTeamIncidentCount": (sample.incidents.player_car_team_incident_count),
        "TrackTempCrew": sample.environment.track_temp_crew_c,
        "AirTemp": sample.environment.air_temp_c,
        "Precipitation": sample.environment.precipitation_fraction,
        "WindVel": sample.environment.wind_velocity_mps,
        "WindDir": sample.environment.wind_direction_rad,
        "FuelLevelPct": sample.fuel.level_pct,
        "PlayerTireCompound": sample.tires.player_tire_compound,
        "TireSetsUsed": sample.tires.tire_sets_used,
    }
    return {name: _field_snapshot(value) for name, value in fields.items()}


def _plain_float(value: object | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _plain_integer(value: object | None) -> int | None:
    return value if type(value) is int else None


def _plain_boolean(value: object | None) -> bool | None:
    return value if type(value) is bool else None


def _traffic_sample(
    sample: TelemetrySample,
    *,
    track_length_mm: int | None,
) -> dict[str, object]:
    if track_length_mm is None:
        return {
            "availability": "UNAVAILABLE",
            "min_longitudinal_separation_mm": None,
            "reasons": ["TRACK_LENGTH_UNAVAILABLE"],
        }
    opponents: OpponentSet = sample.opponents
    if opponents.presence is Presence.MISSING:
        return {
            "availability": "UNAVAILABLE",
            "min_longitudinal_separation_mm": None,
            "reasons": ["OPPONENT_ARRAYS_MISSING"],
        }
    if opponents.presence is Presence.INVALID or opponents.issues:
        return {
            "availability": "INVALID",
            "min_longitudinal_separation_mm": None,
            "reasons": ["OPPONENT_ARRAYS_INVALID"],
        }
    player_pct = _plain_float(sample.lap.lap_distance_pct.value)
    if sample.lap.lap_distance_pct.presence is not Presence.PRESENT or player_pct is None:
        return {
            "availability": "INVALID",
            "min_longitudinal_separation_mm": None,
            "reasons": ["PLAYER_LAP_DISTANCE_INVALID"],
        }
    if not 0.0 <= player_pct <= 1.0:
        return {
            "availability": "INVALID",
            "min_longitudinal_separation_mm": None,
            "reasons": ["PLAYER_LAP_DISTANCE_OUT_OF_RANGE"],
        }

    separations: list[int] = []
    for opponent in opponents.entries:
        for opponent_field, name in (
            (opponent.track_surface, "CarIdxTrackSurface"),
            (opponent.on_pit_road, "CarIdxOnPitRoad"),
        ):
            if opponent_field.presence is Presence.INVALID:
                return {
                    "availability": "INVALID",
                    "min_longitudinal_separation_mm": None,
                    "reasons": [f"{name}_INVALID"],
                }
            if opponent_field.presence is Presence.MISSING:
                return {
                    "availability": "UNAVAILABLE",
                    "min_longitudinal_separation_mm": None,
                    "reasons": [f"{name}_MISSING"],
                }
        surface = _plain_integer(opponent.track_surface.value)
        on_pit = _plain_boolean(opponent.on_pit_road.value)
        if surface != 3 or on_pit is not False:
            continue
        distance_field = opponent.lap_distance_pct
        if distance_field.presence is Presence.MISSING:
            return {
                "availability": "UNAVAILABLE",
                "min_longitudinal_separation_mm": None,
                "reasons": ["CarIdxLapDistPct_MISSING"],
            }
        opponent_pct = _plain_float(distance_field.value)
        if distance_field.presence is Presence.INVALID or opponent_pct is None:
            return {
                "availability": "INVALID",
                "min_longitudinal_separation_mm": None,
                "reasons": ["CarIdxLapDistPct_INVALID"],
            }
        if not 0.0 <= opponent_pct <= 1.0:
            return {
                "availability": "INVALID",
                "min_longitudinal_separation_mm": None,
                "reasons": ["CarIdxLapDistPct_OUT_OF_RANGE"],
            }
        delta = abs((opponent_pct % 1.0) - (player_pct % 1.0))
        circular = min(delta, 1.0 - delta)
        separations.append(round(circular * track_length_mm))
    return {
        "availability": "AVAILABLE",
        "min_longitudinal_separation_mm": min(separations) if separations else None,
        "reasons": [],
    }


def _availability(
    presences: Mapping[str, list[str]],
    names: tuple[str, ...],
    start: int,
    end: int,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    status = "AVAILABLE"
    for name in names:
        window = presences[name][start:end]
        if any(value == Presence.INVALID.value for value in window):
            status = "INVALID"
            reasons.append(f"{name}_INVALID")
        elif any(value != Presence.PRESENT.value for value in window):
            if status != "INVALID":
                status = "UNAVAILABLE"
            reasons.append(f"{name}_MISSING")
    return status, reasons


def _median_fixed(values: list[object | None], scale: int) -> int:
    numeric = [_plain_float(value) for value in values]
    if any(value is None for value in numeric):
        raise AssertionError("available numeric field lost its value")
    return round(float(median(value for value in numeric if value is not None)) * scale)


def _weather_observation(
    values: Mapping[str, list[object | None]],
    presences: Mapping[str, list[str]],
    start: int,
    end: int,
) -> dict[str, object]:
    names = ("TrackTempCrew", "AirTemp", "Precipitation", "WindVel", "WindDir")
    availability, reasons = _availability(presences, names, start, end)
    if availability != "AVAILABLE":
        return {"availability": availability, "reasons": reasons}
    track_temp = _median_fixed(values["TrackTempCrew"][start:end], 1_000)
    air_temp = _median_fixed(values["AirTemp"][start:end], 1_000)
    precipitation_values = [_plain_float(value) for value in values["Precipitation"][start:end]]
    wind_velocities = [_plain_float(value) for value in values["WindVel"][start:end]]
    wind_directions = [_plain_float(value) for value in values["WindDir"][start:end]]
    if any(value is None for value in precipitation_values + wind_velocities + wind_directions):
        return {
            "availability": "INVALID",
            "reasons": ["WEATHER_VALUE_INVALID"],
        }
    precipitation_ppm = round(
        max(value for value in precipitation_values if value is not None) * 1_000_000
    )
    count = len(wind_velocities)
    wind_x = round(
        sum(
            velocity * math.cos(direction)
            for velocity, direction in zip(wind_velocities, wind_directions, strict=True)
            if velocity is not None and direction is not None
        )
        * 1_000
        / count
    )
    wind_y = round(
        sum(
            velocity * math.sin(direction)
            for velocity, direction in zip(wind_velocities, wind_directions, strict=True)
            if velocity is not None and direction is not None
        )
        * 1_000
        / count
    )
    return {
        "air_temp_millic": air_temp,
        "availability": "AVAILABLE",
        "precipitation_max_ppm": precipitation_ppm,
        "reasons": [],
        "track_temp_millic": track_temp,
        "wind_x_mmps": wind_x,
        "wind_y_mmps": wind_y,
    }


def _fuel_observation(
    values: Mapping[str, list[object | None]],
    presences: Mapping[str, list[str]],
    start: int,
    end: int,
    config: ConditionCohortConfig,
) -> dict[str, object]:
    availability, reasons = _availability(presences, ("FuelLevelPct",), start, end)
    if availability != "AVAILABLE":
        return {"availability": availability, "reasons": reasons}
    fuel_values = [_plain_float(value) for value in values["FuelLevelPct"][start:end]]
    if any(value is None or not 0.0 <= value <= 1.0 for value in fuel_values):
        return {"availability": "INVALID", "reasons": ["FuelLevelPct_INVALID"]}
    fuel_ppm = [round(value * 1_000_000) for value in fuel_values if value is not None]
    running_minimum = fuel_ppm[0]
    for value in fuel_ppm[1:]:
        if value - running_minimum > config.fuel_refuel_jump_tolerance_ppm:
            return {
                "availability": "INVALID",
                "reasons": ["MID_LAP_REFUEL_OBSERVED"],
            }
        running_minimum = min(running_minimum, value)
    return {
        "availability": "AVAILABLE",
        "fuel_start_fraction_ppm": fuel_ppm[0],
        "reasons": [],
    }


def _tire_observation(
    values: Mapping[str, list[object | None]],
    presences: Mapping[str, list[str]],
    stint_epochs: list[int],
    stint_metadata: Mapping[int, Mapping[str, object]],
    start: int,
    end: int,
) -> dict[str, object]:
    names = ("PlayerTireCompound", "TireSetsUsed", "LapCompleted")
    availability, reasons = _availability(presences, names, start, end)
    if availability != "AVAILABLE":
        return {"availability": availability, "reasons": reasons}
    compounds = values["PlayerTireCompound"][start:end]
    sets_used = values["TireSetsUsed"][start:end]
    laps_completed = values["LapCompleted"][start:end]
    epochs = stint_epochs[start:end]
    if (
        any(_plain_integer(value) is None for value in compounds + sets_used + laps_completed)
        or len(set(compounds)) != 1
        or len(set(sets_used)) != 1
        or len(set(laps_completed)) != 1
        or len(set(epochs)) != 1
    ):
        return {
            "availability": "INVALID",
            "reasons": ["TIRE_CONTEXT_CHANGED_WITHIN_LAP"],
        }
    epoch = epochs[0]
    metadata = stint_metadata.get(epoch)
    if metadata is None or metadata["base_laps_completed"] is None:
        return {
            "availability": "UNAVAILABLE",
            "reasons": ["STINT_LAP_AGE_UNAVAILABLE"],
        }
    stint_lap_age = laps_completed[0] - metadata["base_laps_completed"]
    if type(stint_lap_age) is not int or stint_lap_age < 0:
        return {
            "availability": "INVALID",
            "reasons": ["STINT_LAP_AGE_INVALID"],
        }
    return {
        "availability": "AVAILABLE",
        "player_tire_compound": compounds[0],
        "reasons": [],
        "set_change_observed": metadata["set_change_observed"],
        "stint_epoch": epoch,
        "stint_lap_age": stint_lap_age,
        "stint_origin_observed": metadata["origin_observed"],
        "tire_sets_used": sets_used[0],
    }


def _traffic_observation(
    traffic_samples: list[dict[str, object]],
    start: int,
    end: int,
) -> dict[str, object]:
    window = traffic_samples[start:end]
    if any(item["availability"] == "INVALID" for item in window):
        return {
            "availability": "INVALID",
            "min_longitudinal_separation_mm": None,
            "reasons": sorted(
                {
                    reason
                    for item in window
                    if item["availability"] == "INVALID"
                    for reason in item["reasons"]
                }
            ),
        }
    if any(item["availability"] != "AVAILABLE" for item in window):
        return {
            "availability": "UNAVAILABLE",
            "min_longitudinal_separation_mm": None,
            "reasons": sorted(
                {
                    reason
                    for item in window
                    if item["availability"] != "AVAILABLE"
                    for reason in item["reasons"]
                }
            ),
        }
    distances = [
        item["min_longitudinal_separation_mm"]
        for item in window
        if item["min_longitudinal_separation_mm"] is not None
    ]
    return {
        "availability": "AVAILABLE",
        "min_longitudinal_separation_mm": min(distances) if distances else None,
        "reasons": [],
    }


def _track_state_observation(
    labels: ApprovedTrackStateLabelSet | None,
    ordinal: int,
) -> dict[str, object]:
    if labels is None:
        return {
            "availability": "UNAVAILABLE",
            "label": None,
            "reasons": ["APPROVED_TRACK_STATE_LABEL_MISSING"],
        }
    label = labels.label_for(ordinal)
    if label is None:
        return {
            "availability": "UNAVAILABLE",
            "label": None,
            "reasons": ["APPROVED_TRACK_STATE_LAP_LABEL_MISSING"],
        }
    return {"availability": "AVAILABLE", "label": label, "reasons": []}


def _lap_observation(
    lap: LapObservation,
    *,
    values: Mapping[str, list[object | None]],
    presences: Mapping[str, list[str]],
    stint_epochs: list[int],
    stint_metadata: Mapping[int, Mapping[str, object]],
    traffic_samples: list[dict[str, object]],
    labels: ApprovedTrackStateLabelSet | None,
    config: ConditionCohortConfig,
) -> dict[str, object]:
    start, end = lap.start_frame, lap.end_frame_exclusive
    observation = {
        "fuel_load": _fuel_observation(values, presences, start, end, config),
        "lap_ordinal": lap.ordinal,
        "tire_usage_context": _tire_observation(
            values,
            presences,
            stint_epochs,
            stint_metadata,
            start,
            end,
        ),
        "track_state": _track_state_observation(labels, lap.ordinal),
        "traffic_proximity": _traffic_observation(traffic_samples, start, end),
        "weather": _weather_observation(values, presences, start, end),
    }
    observation["lap_condition_semantic_sha256"] = _digest(observation)
    return observation


def _availability_comparison(
    target: Mapping[str, object], candidate: Mapping[str, object]
) -> dict[str, object] | None:
    availability = {target["availability"], candidate["availability"]}
    if "INVALID" in availability:
        return {
            "reasons": sorted(set(target["reasons"]) | set(candidate["reasons"])),
            "status": "INVALID",
        }
    if "UNAVAILABLE" in availability:
        return {
            "reasons": sorted(set(target["reasons"]) | set(candidate["reasons"])),
            "status": "UNAVAILABLE",
        }
    return None


def _compare_weather(
    target: Mapping[str, object],
    candidate: Mapping[str, object],
    config: ConditionCohortConfig,
) -> dict[str, object]:
    unavailable = _availability_comparison(target, candidate)
    if unavailable is not None:
        return unavailable
    reasons: list[str] = []
    if (
        target["precipitation_max_ppm"] > config.dry_precipitation_max_ppm
        or candidate["precipitation_max_ppm"] > config.dry_precipitation_max_ppm
    ):
        reasons.append("DRY_WEATHER_GATE_FAILED")
    if (
        abs(target["track_temp_millic"] - candidate["track_temp_millic"])
        > config.track_temp_tolerance_millic
    ):
        reasons.append("TRACK_TEMP_MISMATCH")
    if (
        abs(target["air_temp_millic"] - candidate["air_temp_millic"])
        > config.air_temp_tolerance_millic
    ):
        reasons.append("AIR_TEMP_MISMATCH")
    wind_delta = math.isqrt(
        (target["wind_x_mmps"] - candidate["wind_x_mmps"]) ** 2
        + (target["wind_y_mmps"] - candidate["wind_y_mmps"]) ** 2
    )
    if wind_delta > config.wind_vector_tolerance_mmps:
        reasons.append("WIND_VECTOR_MISMATCH")
    return {"reasons": reasons, "status": "MISMATCHED" if reasons else "MATCHED"}


def _compare_track_state(
    target: Mapping[str, object], candidate: Mapping[str, object]
) -> dict[str, object]:
    unavailable = _availability_comparison(target, candidate)
    if unavailable is not None:
        return unavailable
    if target["label"] != "DRY_STABLE" or candidate["label"] != "DRY_STABLE":
        return {"reasons": ["TRACK_STATE_NOT_DRY_STABLE"], "status": "MISMATCHED"}
    return {"reasons": [], "status": "MATCHED"}


def _compare_fuel(
    target: Mapping[str, object],
    candidate: Mapping[str, object],
    config: ConditionCohortConfig,
) -> dict[str, object]:
    unavailable = _availability_comparison(target, candidate)
    if unavailable is not None:
        return unavailable
    difference = abs(target["fuel_start_fraction_ppm"] - candidate["fuel_start_fraction_ppm"])
    return {
        "difference_ppm": difference,
        "reasons": (
            ["FUEL_START_LOAD_MISMATCH"] if difference > config.fuel_start_tolerance_ppm else []
        ),
        "status": ("MISMATCHED" if difference > config.fuel_start_tolerance_ppm else "MATCHED"),
    }


def _compare_tires(
    target: Mapping[str, object],
    candidate: Mapping[str, object],
    *,
    config: ConditionCohortConfig,
) -> dict[str, object]:
    unavailable = _availability_comparison(target, candidate)
    if unavailable is not None:
        return unavailable
    if target["player_tire_compound"] != candidate["player_tire_compound"]:
        return {"reasons": ["TIRE_COMPOUND_MISMATCH"], "status": "MISMATCHED"}
    same_stint = target["stint_epoch"] == candidate["stint_epoch"]
    if same_stint and target["tire_sets_used"] != candidate["tire_sets_used"]:
        return {
            "reasons": ["TIRE_SET_CHANGED_WITHOUT_STINT_BOUNDARY"],
            "status": "INVALID",
        }
    if not same_stint and not (
        target["stint_origin_observed"]
        and candidate["stint_origin_observed"]
        and target["set_change_observed"]
        and candidate["set_change_observed"]
    ):
        return {
            "reasons": ["CROSS_STINT_SET_CONTEXT_UNOBSERVED"],
            "status": "UNAVAILABLE",
        }
    age_delta = abs(target["stint_lap_age"] - candidate["stint_lap_age"])
    reasons = ["TIRE_USAGE_AGE_MISMATCH"] if age_delta > config.max_tire_usage_lap_delta else []
    return {
        "same_observed_stint": same_stint,
        "stint_lap_age_delta": age_delta,
        "reasons": reasons,
        "status": "MISMATCHED" if reasons else "MATCHED",
    }


def _compare_traffic(
    target: Mapping[str, object],
    candidate: Mapping[str, object],
    config: ConditionCohortConfig,
) -> dict[str, object]:
    unavailable = _availability_comparison(target, candidate)
    if unavailable is not None:
        return unavailable

    def clear(value: Mapping[str, object]) -> bool:
        separation = value["min_longitudinal_separation_mm"]
        return separation is None or separation >= config.traffic_clearance_mm

    reasons = [] if clear(target) and clear(candidate) else ["TRAFFIC_CONTAMINATED"]
    return {"reasons": reasons, "status": "MISMATCHED" if reasons else "MATCHED"}


def _pair_receipt(
    target: Mapping[str, object],
    candidate: Mapping[str, object],
    config: ConditionCohortConfig,
) -> dict[str, object]:
    candidate_ordinal = candidate["lap_ordinal"]
    dimensions = {
        "fuel_load": _compare_fuel(target["fuel_load"], candidate["fuel_load"], config),
        "tire_usage_context": _compare_tires(
            target["tire_usage_context"],
            candidate["tire_usage_context"],
            config=config,
        ),
        "track_state": _compare_track_state(target["track_state"], candidate["track_state"]),
        "traffic_proximity": _compare_traffic(
            target["traffic_proximity"], candidate["traffic_proximity"], config
        ),
        "weather": _compare_weather(target["weather"], candidate["weather"], config),
    }
    statuses = {value["status"] for value in dimensions.values()}
    if "INVALID" in statuses:
        disposition = "FAIL"
    elif "MISMATCHED" in statuses:
        disposition = "NO_MATCH"
    elif "UNAVAILABLE" in statuses:
        disposition = "WAIT_CONDITION_DATA"
    else:
        disposition = "MATCH"
    reasons = sorted(
        {reason for dimension in dimensions.values() for reason in dimension["reasons"]}
    )
    return {
        "candidate_lap_ordinal": candidate_ordinal,
        "dimensions": dimensions,
        "disposition": disposition,
        "reasons": reasons,
    }


def _stint_context(
    on_pit_road: list[object | None],
    laps_completed: list[object | None],
    tire_sets_used: list[object | None],
) -> tuple[list[int], dict[int, dict[str, object]]]:
    epochs: list[int] = []
    epoch = 0
    previous = False
    for raw in on_pit_road:
        current = _plain_boolean(raw)
        if current is None:
            current = previous
        if previous and not current:
            epoch += 1
        epochs.append(epoch)
        previous = current
    starts: dict[int, int] = {}
    for index, value in enumerate(epochs):
        starts.setdefault(value, index)
    metadata: dict[int, dict[str, object]] = {}
    previous_initial_set: int | None = None
    for value, index in sorted(starts.items()):
        initial_set = _plain_integer(tire_sets_used[index])
        base_laps_completed = _plain_integer(laps_completed[index])
        metadata[value] = {
            "base_laps_completed": base_laps_completed,
            "initial_tire_sets_used": initial_set,
            "origin_observed": value > 0,
            "set_change_observed": (
                value > 0
                and initial_set is not None
                and previous_initial_set is not None
                and initial_set == previous_initial_set + 1
            ),
        }
        previous_initial_set = initial_set
    return epochs, metadata


def _semantic_opponent_row(sample: TelemetrySample) -> dict[str, object]:
    return {
        "entries": [
            {
                "car_idx": _field_snapshot(item.car_idx),
                "lap_distance_pct": _field_snapshot(item.lap_distance_pct),
                "lap_number": _field_snapshot(item.lap_number),
                "laps_completed": _field_snapshot(item.laps_completed),
                "on_pit_road": _field_snapshot(item.on_pit_road),
                "track_surface": _field_snapshot(item.track_surface),
            }
            for item in sample.opponents.entries
        ],
        "issues": list(sample.opponents.issues),
        "player_car_idx": _field_snapshot(sample.opponents.player_car_idx),
        "presence": sample.opponents.presence.value,
    }


def _build_condition_cohort_samples(
    samples: Iterable[TelemetrySample],
    *,
    input_kind: Literal["ibt", "collector"],
    input_evidence: IbtInputEvidence | CollectorInputEvidence,
    track_context: TrackContextEvidence,
    tick_rate_hz: int,
    target_lap_ordinal: int,
    track_state_labels: ApprovedTrackStateLabelSet | None = None,
    config: ConditionCohortConfig | None = None,
) -> dict[str, object]:
    """Testable core; production callers must use :func:`build_condition_cohort`."""

    selected_config = config or ConditionCohortConfig()
    if not isinstance(selected_config, ConditionCohortConfig):
        raise TypeError("config must be a ConditionCohortConfig")
    _plain_int(target_lap_ordinal, "target_lap_ordinal")
    if type(track_context) is not TrackContextEvidence:
        raise ConditionCohortError("track_context must come from a validated telemetry adapter")
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
        raise ConditionCohortError(str(exc)) from exc
    evidence_binding_sha256 = _digest(evidence_payload)
    if track_context.source_binding_sha256 != evidence_binding_sha256:
        raise ConditionCohortError("track context is not bound to input evidence")
    if track_state_labels is not None:
        if type(track_state_labels) is not ApprovedTrackStateLabelSet:
            raise TypeError("track_state_labels must be an ApprovedTrackStateLabelSet or None")
        if track_state_labels.source_binding_sha256 != evidence_binding_sha256:
            raise ConditionCohortError("track-state label set is not bound to input evidence")

    values: dict[str, list[object | None]] = {name: [] for name in _ALL_FIELDS}
    presences: dict[str, list[str]] = {name: [] for name in _ALL_FIELDS}
    traffic_samples: list[dict[str, object]] = []
    normalized_digest = hashlib.sha256()
    semantic_digest = hashlib.sha256()
    sample_count = 0
    modeled_sample_count = 0
    rejected_sample_count = 0
    normalized_dropped_tick_count = 0
    invalid_presence_counts: dict[str, int] = {name: 0 for name in _ALL_FIELDS}

    track_length_mm = (
        track_context.track_length_mm
        if track_context.availability is TrackContextAvailability.AVAILABLE
        else None
    )
    for sample in samples:
        if not isinstance(sample, TelemetrySample):
            raise ConditionCohortError("samples must contain TelemetrySample values")
        if _sample_identity(sample) != (
            evidence_source_id,
            evidence_session_id,
            evidence_source_kind,
        ):
            raise ConditionCohortError(
                "normalized sample identity/source_kind does not match input_evidence"
            )
        encoded = sample.to_json_line().encode("utf-8")
        normalized_digest.update(len(encoded).to_bytes(8, "little"))
        normalized_digest.update(encoded)
        sample_count += 1
        status = _quality_status(sample)
        if status is QualityStatus.REJECTED:
            rejected_sample_count += 1
            continue
        dropped = sample.quality.dropped_ticks
        if dropped.presence is Presence.PRESENT and type(dropped.value) is int:
            normalized_dropped_tick_count += dropped.value
        snapshots = _sample_fields(sample)
        for name, (presence, value) in snapshots.items():
            values[name].append(value)
            presences[name].append(presence)
            if presence == Presence.INVALID.value:
                invalid_presence_counts[name] += 1
        traffic = _traffic_sample(sample, track_length_mm=track_length_mm)
        traffic_samples.append(traffic)
        semantic_row = {
            "fields": {
                name: {"presence": presence, "value": value}
                for name, (presence, value) in snapshots.items()
            },
            "opponents": _semantic_opponent_row(sample),
            "traffic_sample": traffic,
        }
        semantic_encoded = _canonical_json(semantic_row)
        semantic_digest.update(len(semantic_encoded).to_bytes(8, "little"))
        semantic_digest.update(semantic_encoded)
        modeled_sample_count += 1

    if sample_count != expected_sample_count:
        raise ConditionCohortError("normalized sample count does not match bound input evidence")

    reasons = _input_quality_reasons(evidence_payload)
    if rejected_sample_count:
        reasons.append("NORMALIZED_REJECTED_SAMPLES")
    if normalized_dropped_tick_count:
        reasons.append("DROPPED_TICKS")
    invalid_incidents = [name for name in _INCIDENT_CHANNELS if invalid_presence_counts[name]]
    if invalid_incidents:
        reasons.append("INCIDENT_COUNT_INVALID")
    if any(invalid_presence_counts[name] for name in _SEGMENT_REQUIRED):
        reasons.append("SEGMENTATION_CHANNEL_INVALID")

    missing_counts = {
        name: sum(value is None for value in channel) for name, channel in values.items()
    }
    segment_channels: dict[str, np.ndarray] = {}
    missing_segment = [name for name in _SEGMENT_REQUIRED if missing_counts[name]]
    incident_channel = next(
        (name for name in _INCIDENT_CHANNELS if missing_counts[name] == 0), None
    )
    if missing_segment:
        reasons.extend(f"MISSING_REQUIRED_CHANNEL:{name}" for name in missing_segment)
    if incident_channel is None:
        reasons.append("CLEANLINESS_UNOBSERVABLE")
    if not missing_segment and incident_channel is not None and modeled_sample_count:
        segment_channels = {
            "SessionTime": np.asarray(values["SessionTime"], dtype=np.float64),
            "SessionTick": np.asarray(values["SessionTick"], dtype=np.int64),
            "Lap": np.asarray(values["Lap"], dtype=np.int64),
            "LapDistPct": np.asarray(values["LapDistPct"], dtype=np.float64),
            "Speed": np.asarray(values["Speed"], dtype=np.float64),
            "OnPitRoad": np.asarray(values["OnPitRoad"], dtype=np.bool_),
            "PlayerTrackSurface": np.asarray(values["PlayerTrackSurface"], dtype=np.int64),
        }
        if missing_counts["LapCompleted"] == 0:
            segment_channels["LapCompleted"] = np.asarray(values["LapCompleted"], dtype=np.int64)
        for name in _INCIDENT_CHANNELS:
            if missing_counts[name] == 0:
                incident_values = np.asarray(values[name], dtype=np.int64)
                if np.any(np.diff(incident_values) < 0):
                    reasons.append("INCIDENT_COUNT_REGRESSION")
                segment_channels[name] = incident_values

    laps: tuple[LapObservation, ...] = ()
    segmentation_error: str | None = None
    if segment_channels:
        try:
            laps = segment_laps(segment_channels, tick_rate_hz)
        except (KeyError, TypeError, ValueError) as exc:
            reasons.append("LAP_SEGMENTATION_FAILED")
            segmentation_error = f"{type(exc).__name__}:{exc}"
    reasons = list(dict.fromkeys(reasons))

    stint_epochs, stint_metadata = _stint_context(
        values["OnPitRoad"],
        values["LapCompleted"],
        values["TireSetsUsed"],
    )
    eligible_laps = tuple(lap for lap in laps if lap.clean_for_driving)
    lap_conditions = [
        _lap_observation(
            lap,
            values=values,
            presences=presences,
            stint_epochs=stint_epochs,
            stint_metadata=stint_metadata,
            traffic_samples=traffic_samples,
            labels=track_state_labels,
            config=selected_config,
        )
        for lap in eligible_laps
    ]
    by_ordinal = {item["lap_ordinal"]: item for item in lap_conditions}
    target = by_ordinal.get(target_lap_ordinal)
    pairs = (
        [_pair_receipt(target, candidate, selected_config) for candidate in lap_conditions]
        if target is not None
        else []
    )
    matched_ordinals = [
        item["candidate_lap_ordinal"] for item in pairs if item["disposition"] == "MATCH"
    ]
    failed_pair_reasons = sorted(
        {reason for item in pairs if item["disposition"] == "FAIL" for reason in item["reasons"]}
    )
    waiting_pair_reasons = sorted(
        {
            reason
            for item in pairs
            if item["disposition"] == "WAIT_CONDITION_DATA"
            for reason in item["reasons"]
        }
    )

    has_input_failure = any(reason in _FAIL_INPUT_REASONS for reason in reasons)
    if has_input_failure or any(item["disposition"] == "FAIL" for item in pairs):
        readiness_status = "FAIL"
        reasons.extend(failed_pair_reasons)
    elif target is None:
        readiness_status = "WAIT_CONDITION_DATA"
        reasons.append("TARGET_LAP_NOT_CLEAN_OR_UNAVAILABLE")
    elif len(matched_ordinals) >= selected_config.min_matched_laps:
        readiness_status = "PASS"
    elif any(item["disposition"] == "WAIT_CONDITION_DATA" for item in pairs):
        readiness_status = "WAIT_CONDITION_DATA"
        reasons.extend(waiting_pair_reasons)
        reasons.append("INSUFFICIENT_MATCHED_LAPS")
    else:
        readiness_status = "WAIT_MATCHED_LAPS"
        reasons.append("INSUFFICIENT_MATCHED_LAPS")
    reasons = list(dict.fromkeys(reasons))

    config_payload = asdict(selected_config)
    condition_config_sha256 = _digest(
        {
            "contract_version": CONDITION_COHORT_CONTRACT_VERSION,
            "feature_pipeline_version": CONDITION_FEATURE_PIPELINE_VERSION,
            "matcher_config": config_payload,
        }
    )
    semantic_binding = {
        "condition_config_sha256": condition_config_sha256,
        "contract_version": CONDITION_COHORT_CONTRACT_VERSION,
        "lap_algorithm_version": LAP_ALGORITHM_VERSION,
        "lap_conditions": lap_conditions,
        "matched_lap_ordinals": matched_ordinals,
        "pairs": pairs,
        "readiness_status": readiness_status,
        "semantic_input_sha256": semantic_digest.hexdigest(),
        "target_lap_ordinal": target_lap_ordinal,
        "track_length_mm": track_length_mm,
    }
    condition_semantic_sha256 = _digest(semantic_binding)
    normalized_input_receipt = {
        "contract_version": TELEMETRY_CONTRACT_VERSION,
        "sample_count": sample_count,
        "samples_sha256": normalized_digest.hexdigest(),
    }
    provenance_binding = {
        "input_evidence": evidence_payload,
        "input_kind": input_kind,
        "normalized_input_receipt": normalized_input_receipt,
        "track_context": track_context.to_dict(),
        "track_state_label_set": (
            track_state_labels.to_dict() if track_state_labels is not None else None
        ),
    }
    condition_provenance_sha256 = _digest(provenance_binding)

    track_state_authenticity = (
        {
            "authenticated": False,
            "reasons": ["APPROVED_TRACK_STATE_LABEL_MISSING"],
            "status": "WAIT_CONDITION_DATA",
        }
        if track_state_labels is None
        else {
            "authenticated": False,
            "reasons": ["SELF_ATTESTED_NOT_AUTHENTICATED"],
            "status": "WAIT_HUMAN_AUTHENTICATION",
        }
    )
    trusted_readiness_status = (
        "WAIT_HUMAN_AUTHENTICATION"
        if readiness_status == "PASS" and track_state_authenticity["status"] != "PASS"
        else readiness_status
    )
    quality_reasons = list(dict.fromkeys([*reasons, *track_state_authenticity["reasons"]]))

    target_traffic = target["traffic_proximity"] if target is not None else None
    target_tires = target["tire_usage_context"] if target is not None else None
    capabilities = {
        "condition_matching": {
            "matched_lap_ordinals": matched_ordinals,
            "reasons": reasons,
            "status": readiness_status,
        },
        "current_tire_wear": unavailable_inference_capability(
            reasons=("CURRENT_STINT_TIRE_WEAR_MODEL_NOT_IMPLEMENTED",),
            blocked_claims=("CURRENT_TIRE_WEAR_CLAIM",),
        ),
        "observed_proximity_gate": {
            "estimate_available": False,
            "reasons": (
                target_traffic["reasons"]
                if target_traffic is not None
                else ["TARGET_LAP_UNAVAILABLE"]
            ),
            "status": (
                "PASS"
                if target_traffic is not None
                and target_traffic["availability"] == "AVAILABLE"
                and (
                    target_traffic["min_longitudinal_separation_mm"] is None
                    or target_traffic["min_longitudinal_separation_mm"]
                    >= selected_config.traffic_clearance_mm
                )
                else "WAIT"
                if target_traffic is None or target_traffic["availability"] == "UNAVAILABLE"
                else "FAIL"
            ),
        },
        "personalized_coaching": unavailable_inference_capability(
            reasons=(
                "HUMAN_CORNER_LABELS_MISSING",
                "CAUSAL_VALIDITY_NOT_ESTABLISHED",
                "TRACK_STATE_LABELS_NOT_AUTHENTICATED",
            )
            if readiness_status == "PASS"
            else (
                "CONDITION_COHORT_NOT_READY",
                "HUMAN_CORNER_LABELS_MISSING",
                "CAUSAL_VALIDITY_NOT_ESTABLISHED",
                "TRACK_STATE_LABELS_NOT_AUTHENTICATED",
            ),
            blocked_claims=("PERSONALIZED_ACTION", "CAUSAL_GAIN_CLAIM"),
        ),
        "tire_usage_context_gate": {
            "estimate_available": False,
            "reasons": (
                target_tires["reasons"] if target_tires is not None else ["TARGET_LAP_UNAVAILABLE"]
            ),
            "status": (
                "PASS"
                if target_tires is not None and target_tires["availability"] == "AVAILABLE"
                else "WAIT"
                if target_tires is None or target_tires["availability"] == "UNAVAILABLE"
                else "FAIL"
            ),
        },
        "track_state_authenticity": track_state_authenticity,
        "traffic_model": unavailable_inference_capability(
            reasons=("TRAFFIC_MODEL_NOT_IMPLEMENTED",),
            blocked_claims=("REJOIN_TRAFFIC_CLAIM", "TRAFFIC_LOSS_CLAIM"),
        ),
    }
    binding = {
        "capabilities": capabilities,
        "condition_config_sha256": condition_config_sha256,
        "condition_provenance_sha256": condition_provenance_sha256,
        "condition_semantic_sha256": condition_semantic_sha256,
        "contract_version": CONDITION_COHORT_CONTRACT_VERSION,
        "input_evidence": evidence_payload,
        "input_kind": input_kind,
        "lap_conditions": lap_conditions,
        "matched_lap_ordinals": matched_ordinals,
        "matcher_config": config_payload,
        "normalized_input_receipt": normalized_input_receipt,
        "pairs": pairs,
        "pipeline": {
            "feature_pipeline_version": CONDITION_FEATURE_PIPELINE_VERSION,
            "lap_algorithm_version": LAP_ALGORITHM_VERSION,
            "normalization_profile_version": NORMALIZATION_PROFILE_VERSION,
            "normalized_telemetry_contract_version": TELEMETRY_CONTRACT_VERSION,
            "tick_rate_hz": tick_rate_hz,
        },
        "quality_gate": {
            "reasons": quality_reasons,
            "status": ("FAIL" if readiness_status == "FAIL" else "DEGRADED"),
        },
        "readiness_status": readiness_status,
        "recommendations": [],
        "series_evidence": {
            "clean_lap_count": len(eligible_laps),
            "incident_source_field": incident_channel,
            "invalid_presence_counts": {
                name: count for name, count in sorted(invalid_presence_counts.items()) if count
            },
            "missing_channel_sample_counts": dict(sorted(missing_counts.items())),
            "modeled_sample_count": modeled_sample_count,
            "normalized_dropped_tick_count": normalized_dropped_tick_count,
            "rejected_sample_count": rejected_sample_count,
            "segmentation_error": segmentation_error,
        },
        "target_lap_ordinal": target_lap_ordinal,
        "track_context": track_context.to_dict(),
        "track_state_label_set": (
            track_state_labels.to_dict() if track_state_labels is not None else None
        ),
        "trusted_readiness_status": trusted_readiness_status,
    }
    return {**binding, "condition_cohort_sha256": _digest(binding)}


def build_condition_cohort(
    run: ValidatedIbtRun | ValidatedCollectorRun,
    *,
    target_lap_ordinal: int,
    track_state_labels: ApprovedTrackStateLabelSet | None = None,
    config: ConditionCohortConfig | None = None,
) -> dict[str, object]:
    """Build a cohort from one active adapter-created validated run only."""

    if type(run) not in {ValidatedIbtRun, ValidatedCollectorRun}:
        raise ConditionCohortError(
            "run must come directly from an open validated telemetry adapter"
        )
    state = _validated_run_state(run)
    if state is None:
        raise ConditionCohortError(
            "run must come directly from an open validated telemetry adapter"
        )
    if type(run) is ValidatedIbtRun:
        if type(state.evidence) is not IbtInputEvidence:
            raise ConditionCohortError("validated IBT run evidence is invalid")
        input_kind: Literal["ibt", "collector"] = "ibt"
        tick_rate_hz = state.evidence.tick_rate_hz
    else:
        if type(state.evidence) is not CollectorInputEvidence:
            raise ConditionCohortError("validated collector run evidence is invalid")
        input_kind = "collector"
        rates = state.evidence.tick_rate_hz_values
        if len(rates) != 1:
            raise ConditionCohortError("collector evidence must bind exactly one tick rate")
        tick_rate_hz = rates[0]
    return _build_condition_cohort_samples(
        state.samples,
        input_kind=input_kind,
        input_evidence=state.evidence,
        track_context=state.track_context,
        tick_rate_hz=tick_rate_hz,
        target_lap_ordinal=target_lap_ordinal,
        track_state_labels=track_state_labels,
        config=config,
    )
