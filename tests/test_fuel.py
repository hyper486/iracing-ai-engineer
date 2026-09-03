from __future__ import annotations

import json
from dataclasses import replace

import pytest

from iracing_ai_engineer.fuel import (
    FuelLapSample,
    FuelScenario,
    build_fuel_shadow_recommendation,
    estimate_fuel_strategy,
)
from iracing_ai_engineer.laps import LapObservation


def _samples(*burns: float, lap_time_s: float = 100.0) -> list[FuelLapSample]:
    return [FuelLapSample(burn, lap_time_s=lap_time_s) for burn in burns]


def _lap_observation(**updates: object) -> LapObservation:
    base = LapObservation(
        ordinal=1,
        start_frame=0,
        end_frame_exclusive=6000,
        start_time_s=0.0,
        end_time_s=100.0,
        duration_s=100.0,
        source_lap_start=1,
        source_lap_end=2,
        start_boundary="strong_wrap",
        end_boundary="strong_wrap",
        boundary_confidence="high",
        distance_coverage_laps=1.0,
        tick_coverage=1.0,
        missing_ticks=0,
        max_gap_s=1 / 60,
        duplicate_time_steps=0,
        time_regressions=0,
        tick_regressions=0,
        structurally_complete=True,
        quality_complete=True,
        cleanliness_observable=True,
        clean_for_driving=True,
        fuel_eligible=True,
        on_pit_road_fraction=0.0,
        off_track_fraction=0.0,
        incident_delta=0,
        fuel_start_l=50.0,
        fuel_end_l=47.8,
        fuel_burn_l=2.2,
        tags=(),
        invalid_reasons=(),
    )
    return replace(base, **updates)


def test_mixed_fuel_scenario_preserves_each_input_origin() -> None:
    scenario = FuelScenario(
        current_fuel_l=42.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
        provenance="USER_RULE",
        provenance_overrides=(
            ("current_fuel_l", "SDK_DIRECT"),
            ("remaining_laps", "SDK_DIRECT"),
        ),
    )

    payload = scenario.to_dict()
    assert payload["current_fuel_l"] == {
        "provenance": "SDK_DIRECT",
        "value": 42.0,
    }
    assert payload["remaining_laps"] == {
        "provenance": "SDK_DIRECT",
        "value": 10,
    }
    assert payload["tank_capacity_l"] == {
        "provenance": "USER_RULE",
        "value": 120.0,
    }
    assert "provenance_overrides" not in payload


@pytest.mark.parametrize(
    "overrides, error",
    [
        ([('current_fuel_l', 'SDK_DIRECT')], TypeError),
        ((("unknown", "SDK_DIRECT"),), ValueError),
        (
            (
                ("current_fuel_l", "SDK_DIRECT"),
                ("current_fuel_l", "USER_RULE"),
            ),
            ValueError,
        ),
        ((("current_fuel_l", "INFERRED"),), ValueError),
    ],
)
def test_fuel_scenario_rejects_ambiguous_provenance_overrides(
    overrides: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        FuelScenario(
            current_fuel_l=42.0,
            tank_capacity_l=120.0,
            refuel_rate_l_per_s=2.0,
            remaining_laps=10,
            provenance_overrides=overrides,  # type: ignore[arg-type]
        )


def test_fuel_scenario_rejects_an_optimistic_aggregate_origin() -> None:
    with pytest.raises(
        ValueError, match="aggregate provenance is not conservative"
    ):
        FuelScenario(
            current_fuel_l=42.0,
            tank_capacity_l=120.0,
            refuel_rate_l_per_s=2.0,
            remaining_laps=10,
            provenance="SDK_DIRECT",
            provenance_overrides=(("tank_capacity_l", "USER_RULE"),),
        )


def test_lap_strategy_excludes_pit_and_invalid_samples_and_labels_evidence():
    observations = [
        *_samples(2.0, 2.2, 2.4),
        FuelLapSample(8.0, pit_lap=True),
        FuelLapSample(1.0, valid=False),
    ]

    result = estimate_fuel_strategy(
        observations,
        current_fuel_l=5.0,
        tank_capacity_l=10.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=6,
        reserve_l=1.0,
    )

    assert result.ready
    assert result.burn is not None
    assert result.burn.source_label == "observed"
    assert result.burn.label == "derived"
    assert result.burn.accepted_laps == 3
    assert result.burn.rejected_laps == 2
    assert result.burn.mean_l_per_lap == pytest.approx(2.2)
    assert result.burn.conservative_l_per_lap == pytest.approx(2.4)
    assert result.burn.standard_deviation_l_per_lap == pytest.approx(0.1632993162)
    assert result.burn.coefficient_of_variation == pytest.approx(0.074226962)
    assert result.burn.confidence == "low"
    assert result.rejection_counts == (("INELIGIBLE_LAP", 1), ("PIT_LAP", 1))

    assert result.current_fuel_l is not None
    assert result.current_fuel_l.value == 5.0
    assert result.current_fuel_l.label == "observed"
    assert result.remaining_laps is not None
    assert result.remaining_laps.value == 6
    assert result.remaining_laps.label == "observed"
    assert result.mean_fuel_to_end_l is not None
    assert result.mean_fuel_to_end_l.value == pytest.approx(14.2)
    assert result.conservative_fuel_to_end_l is not None
    assert result.conservative_fuel_to_end_l.value == pytest.approx(15.4)
    assert result.conservative_fuel_to_end_l.label == "estimated"
    assert result.safe_laps_on_current_fuel is not None
    assert result.safe_laps_on_current_fuel.value == 1
    assert result.safe_laps_on_full_tank is not None
    assert result.safe_laps_on_full_tank.value == 3
    assert result.minimum_pit_stops is not None
    assert result.minimum_pit_stops.value == 2
    assert result.cumulative_refuel_to_end_l is not None
    assert result.cumulative_refuel_to_end_l.value == pytest.approx(10.4)
    assert result.cumulative_refuel_time_to_end_s is not None
    assert result.cumulative_refuel_time_to_end_s.value == pytest.approx(5.2)
    assert result.next_pit_window is not None
    assert result.next_pit_window.earliest_lap_from_now == 0
    assert result.next_pit_window.latest_lap_from_now == 1
    assert result.next_pit_window.label == "estimated"


def test_no_stop_or_window_when_current_fuel_safely_reaches_finish():
    result = estimate_fuel_strategy(
        _samples(2.0, 2.0, 2.0),
        current_fuel_l=9.0,
        tank_capacity_l=10.0,
        refuel_rate_l_per_s=1.0,
        remaining_laps=3,
        reserve_l=1.0,
    )

    assert result.ready
    assert result.minimum_pit_stops is not None
    assert result.minimum_pit_stops.value == 0
    assert result.cumulative_refuel_to_end_l is not None
    assert result.cumulative_refuel_to_end_l.value == 0.0
    assert result.next_pit_window is None


def test_one_stop_window_has_earliest_and_latest_feasible_lap():
    result = estimate_fuel_strategy(
        _samples(2.0, 2.0, 2.0),
        current_fuel_l=6.0,
        tank_capacity_l=10.0,
        refuel_rate_l_per_s=1.0,
        remaining_laps=5,
        reserve_l=1.0,
    )

    assert result.next_pit_window is not None
    assert result.next_pit_window.earliest_lap_from_now == 1
    assert result.next_pit_window.latest_lap_from_now == 2
    assert result.minimum_pit_stops is not None
    assert result.minimum_pit_stops.value == 1


def test_timed_race_uses_fastest_admitted_lap_plus_extra_lap():
    observations = [
        FuelLapSample(2.0, lap_time_s=100.0),
        FuelLapSample(2.1, lap_time_s=105.0),
        FuelLapSample(2.2, lap_time_s=98.0),
    ]
    result = estimate_fuel_strategy(
        observations,
        current_fuel_l=20.0,
        tank_capacity_l=30.0,
        refuel_rate_l_per_s=2.0,
        remaining_time_s=250.0,
        reserve_l=1.0,
    )

    assert result.ready
    assert result.remaining_laps is not None
    assert result.remaining_laps.value == 4
    assert result.remaining_laps.label == "derived"


def test_explicit_reference_lap_time_supports_minimal_samples_without_durations():
    result = estimate_fuel_strategy(
        [FuelLapSample(2.0), FuelLapSample(2.1), FuelLapSample(2.2)],
        current_fuel_l=20.0,
        tank_capacity_l=30.0,
        refuel_rate_l_per_s=2.0,
        remaining_time_s=250.0,
        reference_lap_time_s=100.0,
    )

    assert result.ready
    assert result.remaining_laps is not None
    assert result.remaining_laps.value == 4


def test_insufficient_data_fails_closed_without_projections():
    result = estimate_fuel_strategy(
        [FuelLapSample(2.0), FuelLapSample(2.1), FuelLapSample(2.2, pit_lap=True)],
        current_fuel_l=10.0,
        tank_capacity_l=20.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=5,
    )

    assert not result.ready
    assert result.status == "not_ready"
    assert result.reason_codes == ("INSUFFICIENT_VALID_FUEL_LAPS",)
    assert result.current_fuel_l is not None
    assert result.current_fuel_l.label == "observed"
    assert result.burn is None
    assert result.remaining_laps is None
    assert result.conservative_fuel_to_end_l is None
    assert result.next_pit_window is None


def test_timed_race_without_lap_time_fails_closed_after_burn_summary():
    result = estimate_fuel_strategy(
        [FuelLapSample(2.0), FuelLapSample(2.1), FuelLapSample(2.2)],
        current_fuel_l=10.0,
        tank_capacity_l=20.0,
        refuel_rate_l_per_s=2.0,
        remaining_time_s=300.0,
    )

    assert not result.ready
    assert result.reason_codes == ("MISSING_LAP_TIME_FOR_TIMED_RACE",)
    assert result.burn is not None
    assert result.remaining_laps is None
    assert result.minimum_pit_stops is None


def test_tank_that_cannot_cover_one_conservative_lap_fails_closed():
    result = estimate_fuel_strategy(
        _samples(4.0, 4.0, 4.0),
        current_fuel_l=4.0,
        tank_capacity_l=4.0,
        refuel_rate_l_per_s=1.0,
        remaining_laps=2,
        reserve_l=1.0,
    )

    assert not result.ready
    assert result.reason_codes == ("TANK_CANNOT_COVER_ONE_CONSERVATIVE_LAP",)
    assert result.next_pit_window is None


def test_zero_remaining_laps_requires_no_fuel_or_stop_even_below_reserve():
    result = estimate_fuel_strategy(
        _samples(2.0, 2.1, 2.2),
        current_fuel_l=0.2,
        tank_capacity_l=20.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=0,
        reserve_l=1.0,
    )

    assert result.ready
    assert result.mean_fuel_to_end_l is not None
    assert result.mean_fuel_to_end_l.value == 0.0
    assert result.conservative_fuel_to_end_l is not None
    assert result.conservative_fuel_to_end_l.value == 0.0
    assert result.minimum_pit_stops is not None
    assert result.minimum_pit_stops.value == 0
    assert result.cumulative_refuel_to_end_l is not None
    assert result.cumulative_refuel_to_end_l.value == 0.0
    assert result.next_pit_window is None


def test_lap_observation_adapter_respects_fuel_eligibility_and_pit_tags():
    observations = [
        _lap_observation(fuel_burn_l=2.0),
        _lap_observation(fuel_burn_l=2.2),
        _lap_observation(fuel_burn_l=2.4),
        _lap_observation(
            fuel_burn_l=None,
            fuel_eligible=False,
            on_pit_road_fraction=0.1,
            tags=("PIT_LAP",),
        ),
    ]

    result = estimate_fuel_strategy(
        observations,
        current_fuel_l=10.0,
        tank_capacity_l=20.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=4,
    )

    assert result.ready
    assert result.burn is not None
    assert result.burn.accepted_laps == 3
    assert result.rejection_counts == (("PIT_LAP", 1),)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"remaining_time_s": 100.0}, "exactly one"),
        ({"remaining_laps": None}, "exactly one"),
        ({"current_fuel_l": 21.0}, "between zero"),
        ({"refuel_rate_l_per_s": 0.0}, "must be positive"),
        ({"conservative_quantile": 0.4}, "between 0.5"),
    ],
)
def test_invalid_configuration_is_rejected(updates: dict[str, object], message: str):
    arguments: dict[str, object] = {
        "current_fuel_l": 10.0,
        "tank_capacity_l": 20.0,
        "refuel_rate_l_per_s": 2.0,
        "remaining_laps": 5,
    }
    arguments.update(updates)

    with pytest.raises(ValueError, match=message):
        estimate_fuel_strategy(_samples(2.0, 2.1, 2.2), **arguments)


def test_repeated_runs_are_exactly_deterministic():
    kwargs = {
        "current_fuel_l": 8.0,
        "tank_capacity_l": 20.0,
        "refuel_rate_l_per_s": 2.0,
        "remaining_laps": 8,
    }
    observations = _samples(2.13, 2.08, 2.21, 2.17)

    assert estimate_fuel_strategy(observations, **kwargs) == estimate_fuel_strategy(
        observations, **kwargs
    )


def test_result_to_dict_is_json_serializable_and_keeps_labels():
    result = estimate_fuel_strategy(
        _samples(2.0, 2.1, 2.2),
        current_fuel_l=10.0,
        tank_capacity_l=20.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=5,
    )

    payload = json.loads(json.dumps(result.to_dict(), sort_keys=True))

    assert payload["status"] == "ready"
    assert payload["current_fuel_l"]["label"] == "observed"
    assert payload["burn"]["label"] == "derived"
    assert payload["conservative_fuel_to_end_l"]["label"] == "estimated"


def test_shadow_recommendation_separates_burn_stability_from_plan_confidence():
    result = estimate_fuel_strategy(
        _samples(*(2.0 for _ in range(8))),
        current_fuel_l=10.0,
        tank_capacity_l=20.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=8,
        minimum_valid_laps=5,
    )

    recommendation = build_fuel_shadow_recommendation(
        result,
        evidence_ids=("lap:1", "lap:2"),
        scenario_sha256="a" * 64,
        scenario_provenance="USER_RULE",
    )

    assert recommendation is not None
    assert result.burn is not None and result.burn.confidence == "high"
    assert recommendation["confidence"] == "LOW"
    assert recommendation["confidence_basis"] == {
        "historical_burn_stability": "HIGH",
        "overall_plan": "LOW_BECAUSE_EVENT_RULES_AND_TRAFFIC_ARE_UNAVAILABLE",
        "scenario_inputs": "USER_RULE",
    }
    assert recommendation["scenario_sha256"] == "a" * 64
    assert recommendation["executable"] is False
