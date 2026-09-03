"""Deterministic, fail-closed fuel and pit-window planning.

The model intentionally stays small: it only consumes completed-lap fuel burn
observations, rejects pit/invalid laps, and projects a conservative stint plan.
It does not infer caution periods, traffic, weather, or fuel-saving behaviour.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from statistics import fmean, pstdev
from typing import Any, Literal

from .laps import LapObservation

EvidenceLabel = Literal["observed", "derived", "estimated"]
PlanStatus = Literal["ready", "not_ready"]
ConfidenceLevel = Literal["low", "medium", "high"]
ScenarioProvenance = Literal["USER_RULE", "SDK_DIRECT"]

_FUEL_SCENARIO_VALUE_FIELDS = frozenset(
    {
        "conservative_quantile",
        "current_fuel_l",
        "minimum_valid_laps",
        "reference_lap_time_s",
        "refuel_rate_l_per_s",
        "remaining_laps",
        "remaining_time_s",
        "reserve_l",
        "tank_capacity_l",
        "timed_race_extra_laps",
    }
)


@dataclass(frozen=True)
class FuelLapSample:
    """Minimal fuel input for callers that do not have ``LapObservation`` objects."""

    fuel_burn_l: float | None
    lap_time_s: float | None = None
    valid: bool = True
    pit_lap: bool = False


@dataclass(frozen=True)
class FuelScenario:
    """User- or SDK-sourced inputs required to project a fuel plan."""

    current_fuel_l: float
    tank_capacity_l: float
    refuel_rate_l_per_s: float
    remaining_laps: int | None = None
    remaining_time_s: float | None = None
    reference_lap_time_s: float | None = None
    reserve_l: float = 1.0
    conservative_quantile: float = 0.90
    minimum_valid_laps: int = 5
    timed_race_extra_laps: int = 1
    provenance: ScenarioProvenance = "USER_RULE"
    provenance_overrides: tuple[tuple[str, ScenarioProvenance], ...] = ()

    def __post_init__(self) -> None:
        """Reject ambiguous or mutable per-field evidence labels.

        ``provenance`` remains the conservative aggregate label used by the
        recommendation-confidence contract.  ``provenance_overrides`` exists
        only so a mixed scenario can retain the narrower origin of individual
        values (for example SDK-direct current fuel plus user-rule tank size).
        A tuple-of-tuples keeps the frozen scenario deterministic and avoids a
        caller mutating a retained mapping after its digest was computed.
        """

        if self.provenance not in {"USER_RULE", "SDK_DIRECT"}:
            raise ValueError("fuel scenario provenance is invalid")
        if type(self.provenance_overrides) is not tuple:
            raise TypeError("fuel scenario provenance_overrides must be a tuple")
        seen: set[str] = set()
        for item in self.provenance_overrides:
            if type(item) is not tuple or len(item) != 2:
                raise TypeError("fuel scenario provenance override must be a pair")
            name, provenance = item
            if type(name) is not str or name not in _FUEL_SCENARIO_VALUE_FIELDS:
                raise ValueError("fuel scenario provenance override field is invalid")
            if name in seen:
                raise ValueError("fuel scenario provenance override field is duplicated")
            if provenance not in {"USER_RULE", "SDK_DIRECT"}:
                raise ValueError("fuel scenario provenance override value is invalid")
            seen.add(name)
        overrides = dict(self.provenance_overrides)
        payload = asdict(self)
        effective = {
            overrides.get(name, self.provenance)
            for name, value in payload.items()
            if name not in {"provenance", "provenance_overrides"} and value is not None
        }
        conservative = "USER_RULE" if "USER_RULE" in effective else "SDK_DIRECT"
        if self.provenance != conservative:
            raise ValueError(
                "fuel scenario aggregate provenance is not conservative"
            )

    def model_kwargs(self) -> dict[str, Any]:
        return {
            "current_fuel_l": self.current_fuel_l,
            "tank_capacity_l": self.tank_capacity_l,
            "refuel_rate_l_per_s": self.refuel_rate_l_per_s,
            "remaining_laps": self.remaining_laps,
            "remaining_time_s": self.remaining_time_s,
            "reference_lap_time_s": self.reference_lap_time_s,
            "reserve_l": self.reserve_l,
            "conservative_quantile": self.conservative_quantile,
            "minimum_valid_laps": self.minimum_valid_laps,
            "timed_race_extra_laps": self.timed_race_extra_laps,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        overrides = dict(self.provenance_overrides)
        return {
            key: {
                "value": value,
                "provenance": overrides.get(key, self.provenance),
            }
            for key, value in payload.items()
            if key not in {"provenance", "provenance_overrides"} and value is not None
        }


@dataclass(frozen=True)
class LabeledNumber:
    """A number carrying an explicit evidence/projection label and unit."""

    value: int | float
    unit: str
    label: EvidenceLabel


@dataclass(frozen=True)
class FuelBurnSummary:
    """Derived statistics computed only from admitted, observed laps."""

    accepted_laps: int
    rejected_laps: int
    mean_l_per_lap: float
    conservative_l_per_lap: float
    conservative_quantile: float
    minimum_l_per_lap: float
    maximum_l_per_lap: float
    standard_deviation_l_per_lap: float
    coefficient_of_variation: float
    confidence: ConfidenceLevel
    source_label: Literal["observed"] = "observed"
    label: Literal["derived"] = "derived"


@dataclass(frozen=True)
class PitWindow:
    """Feasible next-stop window, expressed as completed laps from now."""

    earliest_lap_from_now: int
    latest_lap_from_now: int
    label: Literal["estimated"] = "estimated"


@dataclass(frozen=True)
class FuelStrategyResult:
    """A ready plan or an explicitly unavailable, fail-closed result."""

    status: PlanStatus
    reason_codes: tuple[str, ...]
    rejection_counts: tuple[tuple[str, int], ...]
    current_fuel_l: LabeledNumber | None = None
    burn: FuelBurnSummary | None = None
    remaining_laps: LabeledNumber | None = None
    mean_fuel_to_end_l: LabeledNumber | None = None
    conservative_fuel_to_end_l: LabeledNumber | None = None
    safe_laps_on_current_fuel: LabeledNumber | None = None
    safe_laps_on_full_tank: LabeledNumber | None = None
    minimum_pit_stops: LabeledNumber | None = None
    cumulative_refuel_to_end_l: LabeledNumber | None = None
    cumulative_refuel_time_to_end_s: LabeledNumber | None = None
    next_pit_window: PitWindow | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def to_dict(self) -> dict[str, object]:
        """Return a nested structure accepted directly by ``json.dumps``."""

        return asdict(self)


def build_fuel_shadow_recommendation(
    result: FuelStrategyResult,
    *,
    evidence_ids: Sequence[str],
    scenario_sha256: str,
    scenario_provenance: Literal["USER_RULE", "SDK_DIRECT"],
) -> dict[str, object] | None:
    """Build one conservative, non-executable fuel-plan candidate.

    Historical burn stability never becomes overall strategy confidence while
    event rules and traffic remain unavailable. Both IBT-only and normalized
    live/replay paths call this function so they cannot make contradictory
    confidence claims for the same model result.
    """

    if not isinstance(result, FuelStrategyResult):
        raise TypeError("result must be a FuelStrategyResult")
    if not result.ready or result.burn is None:
        return None
    normalized_ids = tuple(evidence_ids)
    if (
        any(
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value.encode("utf-8")) > 512
            or any(ord(character) < 32 for character in value)
            for value in normalized_ids
        )
        or len(normalized_ids) != len(set(normalized_ids))
    ):
        raise ValueError("evidence_ids must be unique non-empty plain strings")
    if (
        type(scenario_sha256) is not str
        or len(scenario_sha256) != 64
        or any(character not in "0123456789abcdef" for character in scenario_sha256)
    ):
        raise ValueError("scenario_sha256 must be a lowercase SHA-256 digest")
    if scenario_provenance not in {"USER_RULE", "SDK_DIRECT"}:
        raise ValueError("scenario_provenance is invalid")

    return {
        "action": {
            "cumulative_refuel_to_end": asdict(result.cumulative_refuel_to_end_l)
            if result.cumulative_refuel_to_end_l is not None
            else None,
            "minimum_pit_stops": asdict(result.minimum_pit_stops)
            if result.minimum_pit_stops is not None
            else None,
            "next_pit_window": asdict(result.next_pit_window)
            if result.next_pit_window is not None
            else None,
        },
        "claim_level": "scenario_estimate",
        "confidence": "LOW",
        "confidence_basis": {
            "historical_burn_stability": result.burn.confidence.upper(),
            "overall_plan": "LOW_BECAUSE_EVENT_RULES_AND_TRAFFIC_ARE_UNAVAILABLE",
            "scenario_inputs": scenario_provenance,
        },
        "evidence_ids": list(normalized_ids),
        "executable": False,
        "kind": "FUEL_PLAN_CANDIDATE",
        "practice_only": False,
        "recommendation_id": "fuel:shadow_plan",
        "scenario_sha256": scenario_sha256,
        "status": "SHADOW_ONLY",
    }


@dataclass(frozen=True)
class _NormalizedSample:
    burn_l: float | None
    lap_time_s: float | None
    valid: bool
    pit_lap: bool


def _normalize_sample(sample: FuelLapSample | LapObservation) -> _NormalizedSample:
    if isinstance(sample, FuelLapSample):
        return _NormalizedSample(
            burn_l=sample.fuel_burn_l,
            lap_time_s=sample.lap_time_s,
            valid=sample.valid,
            pit_lap=sample.pit_lap,
        )
    if isinstance(sample, LapObservation):
        pit_lap = sample.on_pit_road_fraction > 0.0 or "PIT_LAP" in sample.tags
        return _NormalizedSample(
            burn_l=sample.fuel_burn_l,
            lap_time_s=sample.duration_s,
            valid=sample.fuel_eligible,
            pit_lap=pit_lap,
        )
    raise TypeError("observations must contain FuelLapSample or LapObservation")


def _nearest_rank(values: list[float], quantile: float) -> float:
    """Return a deterministic empirical quantile that is itself observed."""

    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _not_ready(
    *reason_codes: str,
    current_fuel_l: float,
    rejection_counts: Counter[str] | None = None,
    burn: FuelBurnSummary | None = None,
) -> FuelStrategyResult:
    counts = rejection_counts or Counter()
    return FuelStrategyResult(
        status="not_ready",
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        rejection_counts=tuple(sorted(counts.items())),
        current_fuel_l=LabeledNumber(current_fuel_l, "L", "observed"),
        burn=burn,
    )


def estimate_fuel_strategy(
    observations: Iterable[FuelLapSample | LapObservation],
    *,
    current_fuel_l: float,
    tank_capacity_l: float,
    refuel_rate_l_per_s: float,
    remaining_laps: int | None = None,
    remaining_time_s: float | None = None,
    reference_lap_time_s: float | None = None,
    reserve_l: float = 1.0,
    conservative_quantile: float = 0.90,
    minimum_valid_laps: int = 3,
    timed_race_extra_laps: int = 1,
) -> FuelStrategyResult:
    """Estimate fuel-to-end and the feasible next pit window.

    Exactly one of ``remaining_laps`` or ``remaining_time_s`` must be supplied.
    For a timed race, the fastest admitted observed lap (or the explicit
    ``reference_lap_time_s``) converts time to laps, and
    ``timed_race_extra_laps`` protects the final-lap boundary.

    The pit window is expressed as laps completed *from now* before the next
    stop. Its earliest edge preserves enough full-tank range for the minimum
    number of remaining stops; its latest edge preserves the configured fuel
    reserve on the current stint. ``cumulative_refuel_to_end_l`` is the total
    across every remaining stop, not a next-stop fuel command.

    The empirical conservative burn uses nearest-rank quantiles with the mean
    as a floor, avoiding distributional assumptions. Confidence is ``high``
    for at least eight laps with at most 5% coefficient of variation,
    ``medium`` for at least five laps with at most 10%, and ``low`` otherwise.
    """

    _validate_inputs(
        current_fuel_l=current_fuel_l,
        tank_capacity_l=tank_capacity_l,
        refuel_rate_l_per_s=refuel_rate_l_per_s,
        remaining_laps=remaining_laps,
        remaining_time_s=remaining_time_s,
        reference_lap_time_s=reference_lap_time_s,
        reserve_l=reserve_l,
        conservative_quantile=conservative_quantile,
        minimum_valid_laps=minimum_valid_laps,
        timed_race_extra_laps=timed_race_extra_laps,
    )

    accepted_burns: list[float] = []
    accepted_lap_times: list[float] = []
    rejection_counts: Counter[str] = Counter()
    normalized = tuple(_normalize_sample(sample) for sample in observations)

    for sample in normalized:
        if sample.pit_lap:
            rejection_counts["PIT_LAP"] += 1
            continue
        if not sample.valid:
            rejection_counts["INELIGIBLE_LAP"] += 1
            continue
        if sample.burn_l is None:
            rejection_counts["MISSING_FUEL_BURN"] += 1
            continue
        burn = float(sample.burn_l)
        if not math.isfinite(burn):
            rejection_counts["NONFINITE_FUEL_BURN"] += 1
            continue
        if burn <= 0.0:
            rejection_counts["NONPOSITIVE_FUEL_BURN"] += 1
            continue
        if burn > tank_capacity_l:
            rejection_counts["FUEL_BURN_EXCEEDS_TANK"] += 1
            continue

        accepted_burns.append(burn)
        if sample.lap_time_s is not None:
            lap_time = float(sample.lap_time_s)
            if math.isfinite(lap_time) and lap_time > 0.0:
                accepted_lap_times.append(lap_time)

    if len(accepted_burns) < minimum_valid_laps:
        return _not_ready(
            "INSUFFICIENT_VALID_FUEL_LAPS",
            current_fuel_l=current_fuel_l,
            rejection_counts=rejection_counts,
        )

    mean_burn = fmean(accepted_burns)
    empirical_quantile = _nearest_rank(accepted_burns, conservative_quantile)
    planning_burn = max(mean_burn, empirical_quantile)
    standard_deviation = pstdev(accepted_burns)
    coefficient_of_variation = standard_deviation / mean_burn
    if len(accepted_burns) >= 8 and coefficient_of_variation <= 0.05:
        confidence: ConfidenceLevel = "high"
    elif len(accepted_burns) >= 5 and coefficient_of_variation <= 0.10:
        confidence = "medium"
    else:
        confidence = "low"
    burn_summary = FuelBurnSummary(
        accepted_laps=len(accepted_burns),
        rejected_laps=len(normalized) - len(accepted_burns),
        mean_l_per_lap=mean_burn,
        conservative_l_per_lap=planning_burn,
        conservative_quantile=conservative_quantile,
        minimum_l_per_lap=min(accepted_burns),
        maximum_l_per_lap=max(accepted_burns),
        standard_deviation_l_per_lap=standard_deviation,
        coefficient_of_variation=coefficient_of_variation,
        confidence=confidence,
    )

    if remaining_laps is not None:
        laps_to_go = remaining_laps
        remaining_label: EvidenceLabel = "observed"
    else:
        lap_time = reference_lap_time_s
        if lap_time is None:
            if not accepted_lap_times:
                return _not_ready(
                    "MISSING_LAP_TIME_FOR_TIMED_RACE",
                    current_fuel_l=current_fuel_l,
                    rejection_counts=rejection_counts,
                    burn=burn_summary,
                )
            lap_time = min(accepted_lap_times)
        laps_to_go = math.ceil(float(remaining_time_s) / lap_time) + timed_race_extra_laps
        remaining_label = "derived"

    usable_current = max(0.0, current_fuel_l - reserve_l)
    usable_full_tank = tank_capacity_l - reserve_l
    safe_current_laps = math.floor((usable_current + 1e-12) / planning_burn)
    safe_full_tank_laps = math.floor((usable_full_tank + 1e-12) / planning_burn)

    if laps_to_go > safe_current_laps and safe_full_tank_laps < 1:
        return _not_ready(
            "TANK_CANNOT_COVER_ONE_CONSERVATIVE_LAP",
            current_fuel_l=current_fuel_l,
            rejection_counts=rejection_counts,
            burn=burn_summary,
        )

    if laps_to_go == 0:
        mean_to_end = 0.0
        conservative_to_end = 0.0
    else:
        mean_to_end = mean_burn * laps_to_go + reserve_l
        conservative_to_end = planning_burn * laps_to_go + reserve_l
    additional_fuel = max(0.0, conservative_to_end - current_fuel_l)

    if laps_to_go <= safe_current_laps:
        pit_stops = 0
        pit_window = None
    else:
        laps_after_current_stint = laps_to_go - safe_current_laps
        pit_stops = math.ceil(laps_after_current_stint / safe_full_tank_laps)
        earliest = max(0, laps_to_go - pit_stops * safe_full_tank_laps)
        latest = safe_current_laps
        if earliest > latest:
            return _not_ready(
                "NO_FEASIBLE_PIT_WINDOW",
                current_fuel_l=current_fuel_l,
                rejection_counts=rejection_counts,
                burn=burn_summary,
            )
        pit_window = PitWindow(earliest_lap_from_now=earliest, latest_lap_from_now=latest)

    return FuelStrategyResult(
        status="ready",
        reason_codes=(),
        rejection_counts=tuple(sorted(rejection_counts.items())),
        current_fuel_l=LabeledNumber(current_fuel_l, "L", "observed"),
        burn=burn_summary,
        remaining_laps=LabeledNumber(laps_to_go, "laps", remaining_label),
        mean_fuel_to_end_l=LabeledNumber(mean_to_end, "L", "estimated"),
        conservative_fuel_to_end_l=LabeledNumber(conservative_to_end, "L", "estimated"),
        safe_laps_on_current_fuel=LabeledNumber(safe_current_laps, "laps", "estimated"),
        safe_laps_on_full_tank=LabeledNumber(safe_full_tank_laps, "laps", "estimated"),
        minimum_pit_stops=LabeledNumber(pit_stops, "stops", "estimated"),
        cumulative_refuel_to_end_l=LabeledNumber(additional_fuel, "L", "estimated"),
        cumulative_refuel_time_to_end_s=LabeledNumber(
            additional_fuel / refuel_rate_l_per_s, "s", "estimated"
        ),
        next_pit_window=pit_window,
    )


def _validate_inputs(
    *,
    current_fuel_l: float,
    tank_capacity_l: float,
    refuel_rate_l_per_s: float,
    remaining_laps: int | None,
    remaining_time_s: float | None,
    reference_lap_time_s: float | None,
    reserve_l: float,
    conservative_quantile: float,
    minimum_valid_laps: int,
    timed_race_extra_laps: int,
) -> None:
    numeric_values = {
        "current_fuel_l": current_fuel_l,
        "tank_capacity_l": tank_capacity_l,
        "refuel_rate_l_per_s": refuel_rate_l_per_s,
        "reserve_l": reserve_l,
    }
    for name, value in numeric_values.items():
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if tank_capacity_l <= 0.0:
        raise ValueError("tank_capacity_l must be positive")
    if current_fuel_l < 0.0 or current_fuel_l > tank_capacity_l:
        raise ValueError("current_fuel_l must be between zero and tank capacity")
    if reserve_l < 0.0 or reserve_l >= tank_capacity_l:
        raise ValueError("reserve_l must be non-negative and below tank capacity")
    if refuel_rate_l_per_s <= 0.0:
        raise ValueError("refuel_rate_l_per_s must be positive")
    if not 0.5 <= conservative_quantile <= 1.0:
        raise ValueError("conservative_quantile must be between 0.5 and 1.0")
    if minimum_valid_laps < 2:
        raise ValueError("minimum_valid_laps must be at least two")
    if timed_race_extra_laps < 0:
        raise ValueError("timed_race_extra_laps must be non-negative")

    if (remaining_laps is None) == (remaining_time_s is None):
        raise ValueError("provide exactly one of remaining_laps or remaining_time_s")
    if remaining_laps is not None:
        if isinstance(remaining_laps, bool) or not isinstance(remaining_laps, int):
            raise ValueError("remaining_laps must be an integer")
        if remaining_laps < 0:
            raise ValueError("remaining_laps must be non-negative")
    if remaining_time_s is not None and (
        not math.isfinite(remaining_time_s) or remaining_time_s < 0.0
    ):
        raise ValueError("remaining_time_s must be finite and non-negative")
    if reference_lap_time_s is not None and (
        not math.isfinite(reference_lap_time_s) or reference_lap_time_s <= 0.0
    ):
        raise ValueError("reference_lap_time_s must be finite and positive")
