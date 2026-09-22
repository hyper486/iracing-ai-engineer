from __future__ import annotations

import json
import math
from collections.abc import Callable
from copy import deepcopy

import pytest

from iracing_ai_engineer.live_fuel import LiveFuelConfig, LiveFuelEngineer


def _snapshot(step: int, **telemetry: object) -> dict[str, object]:
    # Synthetic 2 Hz display stream; boundaries fall halfway between snapshots.
    distance = (step + 0.5) / 20
    return {
        "record_type": "live_monitor_snapshot",
        "contract_version": "live-monitor-v1",
        "binding_sha256": "a" * 64,
        "sequence": step,
        "session_time_us": round((step + 0.5) * 500_000),
        "source_kind": "SDK_LIVE",
        "status": "READY",
        "context": {
            "sim_source_mode": "FULL", "player_control_state": "IN_CAR_PHYSICS",
            "conflicts": [],
        },
        "quality": {"status": "READY", "stale": False, "issues": [], "dropped_ticks": 0},
        "events": [],
        "interval_invalid_for_fuel": [],
        "telemetry": {
            "session_num": 0, "player_car_idx": 0,
            "fuel_level_l": 40.0 - 2.0 * distance,
            "laps_completed": math.floor(distance), "lap_distance_pct": distance % 1,
            "player_incident_count": 0, "player_track_surface": 3,
            "session_flags": 4, "is_on_track": True, "is_on_track_car": True,
            "on_pit_road": False, "in_pit_stall": False, "pitstop_active": False,
            "session_laps_remaining": 32767, "session_time_remaining_s": None,
            **telemetry,
        },
    }


def _engineer(**config: object) -> LiveFuelEngineer:
    return LiveFuelEngineer(LiveFuelConfig(minimum_valid_laps=2, **config))


def _drive(
    engineer: LiveFuelEngineer, start: int = 5, end: int = 60,
    *, alter: Callable[[dict[str, object]], None] | None = None,
    session_type: str | None = None, **telemetry: object,
) -> dict[str, object]:
    for step in range(start, end + 1):
        snapshot = _snapshot(step, **telemetry)
        if alter is not None:
            alter(snapshot)
        result = engineer.feed(snapshot, session_type=session_type)
    return result


def _assert_no_estimate(result: dict[str, object]) -> None:
    for field in (
        "conservative_burn_l_per_lap", "estimated_laps_remaining",
        "fuel_needed_to_finish_l", "fuel_to_add_l", "minimum_stops",
    ):
        assert result[field] is None, field
    assert result["estimate_only"] is True
    assert result["advisor_only"] is True
    assert result["executable"] is False


def test_defaults_are_conservative_and_validate_config() -> None:
    config = LiveFuelConfig()
    assert config.reserve_l == 2
    assert config.minimum_valid_laps == 5
    assert config.conservative_quantile == 0.9
    assert config.max_history_laps == 50
    assert config.tank_capacity_l is None
    assert config.timed_race_extra_laps == 1
    with pytest.raises(TypeError):
        LiveFuelEngineer({})


@pytest.mark.parametrize("config", [
    {"reserve_l": -1}, {"reserve_l": math.nan}, {"reserve_l": True},
    {"minimum_valid_laps": 1}, {"minimum_valid_laps": 2.0},
    {"minimum_valid_laps": True}, {"conservative_quantile": 0.49},
    {"conservative_quantile": math.inf}, {"conservative_quantile": True},
    {"max_history_laps": 4}, {"max_history_laps": 10_001},
    {"tank_capacity_l": 2}, {"tank_capacity_l": math.inf},
    {"tank_capacity_l": False}, {"timed_race_extra_laps": -1},
    {"timed_race_extra_laps": 1.0}, {"timed_race_extra_laps": True},
])
def test_config_rejects_ambiguous_or_unbounded_values(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        LiveFuelConfig(**config)


def test_first_partial_lap_never_counts_and_crossings_interpolate_fuel_and_time() -> None:
    engineer = _engineer()
    result = _drive(engineer, end=20)
    assert result["valid_laps"] == 0
    assert result["status"] == "LEARNING"
    result = _drive(engineer, start=21, end=40)
    assert result["valid_laps"] == 1
    _assert_no_estimate(result)
    result = _drive(engineer, start=41, end=60, session_type="Race",
                    session_time_remaining_s=10.0)
    assert result["status"] == "READY"
    assert result["valid_laps"] == 2
    assert result["conservative_burn_l_per_lap"] == pytest.approx(2)
    assert result["estimated_laps_remaining"] == 15
    assert result["fuel_needed_to_finish_l"] == pytest.approx(6)
    assert result["fuel_to_add_l"] is None
    # Crossings are at exactly 10/20/30 seconds, not the 10.25/20.25/30.25 samples.
    assert [sample.lap_time_s for sample in engineer._history] == pytest.approx([10, 10])


def test_fuel_boundary_interpolation_uses_actual_fraction_not_midpoint() -> None:
    engineer = _engineer()

    def alter(snapshot: dict[str, object]) -> None:
        telemetry = snapshot["telemetry"]
        # Slightly different crossing speeds/fractions with linearly related fuel.
        step = snapshot["sequence"]
        if step in (20, 40, 60):
            distance = step / 20 + 0.01
            telemetry["lap_distance_pct"] = 0.01
            telemetry["fuel_level_l"] = 40.0 - 2.0 * distance

    result = _drive(engineer, alter=alter)
    assert result["status"] == "READY"
    assert result["conservative_burn_l_per_lap"] == pytest.approx(2)


def test_output_is_fixed_private_safe_and_does_not_alias_input_or_previous_result() -> None:
    engineer = _engineer()
    snapshot = _snapshot(5)
    snapshot["private_field"] = "private detail must not be echoed"
    original = deepcopy(snapshot)
    result = engineer.feed(snapshot)
    assert snapshot == original
    assert set(result) == {
        "status", "reason_codes", "current_fuel_l", "valid_laps", "required_laps",
        "conservative_burn_l_per_lap", "estimated_laps_remaining",
        "fuel_needed_to_finish_l", "fuel_to_add_l", "minimum_stops", "message",
        "estimate_only", "advisor_only", "executable",
    }
    assert "private detail" not in json.dumps(result, allow_nan=False)
    result["reason_codes"].append("contaminated")
    result["current_fuel_l"] = 999
    assert "contaminated" not in engineer.feed(_snapshot(6))["reason_codes"]
    assert engineer.reset("private detail")["reason_codes"] == ["RESET"]


@pytest.mark.parametrize("session_type", [None, "Practice", "Qualify", "race", " Race"])
def test_finish_never_inferred_from_non_race_timer(session_type: str | None) -> None:
    result = _drive(_engineer(tank_capacity_l=50), session_type=session_type,
                    session_laps_remaining=10, session_time_remaining_s=100)
    assert result["status"] == "READY"
    assert result["fuel_needed_to_finish_l"] is None
    assert result["fuel_to_add_l"] is None
    assert result["minimum_stops"] is None
    assert "RACE_FINISH_UNCONFIRMED" in result["reason_codes"]


@pytest.mark.parametrize("remaining", [None, 32767, 32768, -1, 4.0, True, "4"])
def test_invalid_laps_and_unknown_time_do_not_invent_finish(remaining: object) -> None:
    result = _drive(_engineer(tank_capacity_l=50), session_type="Race",
                    session_laps_remaining=remaining)
    assert result["status"] == "READY"
    assert result["fuel_needed_to_finish_l"] is None
    assert result["fuel_to_add_l"] is None


@pytest.mark.parametrize("remaining", [None, math.inf, math.nan, -1, True, 604800, "10"])
def test_unknown_or_unlimited_time_does_not_invent_finish(remaining: object) -> None:
    result = _drive(_engineer(tank_capacity_l=50), session_type="Race",
                    session_time_remaining_s=remaining)
    assert result["fuel_needed_to_finish_l"] is None


def test_timed_finish_uses_fastest_full_lap_and_extra_boundary_lap() -> None:
    result = _drive(_engineer(tank_capacity_l=50), session_type="Race",
                    session_time_remaining_s=20.001)
    assert result["fuel_needed_to_finish_l"] == pytest.approx(10)  # 3 + 1 laps, reserve 2
    assert result["fuel_to_add_l"] == 0
    assert result["minimum_stops"] == 0


def test_known_lap_finish_is_full_laps_and_add_only_if_whole_finish_fits_tank() -> None:
    engineer = _engineer(tank_capacity_l=50)
    result = _drive(engineer, session_type="Race", session_laps_remaining=18)
    assert result["fuel_needed_to_finish_l"] == 38
    assert result["fuel_to_add_l"] == pytest.approx(4.05)
    assert result["minimum_stops"] == 1
    result = engineer.feed(_snapshot(61, session_laps_remaining=25), session_type="Race")
    assert result["fuel_needed_to_finish_l"] == 52
    assert result["fuel_to_add_l"] is None
    assert result["minimum_stops"] == 1
    assert "FINISH_EXCEEDS_ONE_TANK" in result["reason_codes"]


def test_missing_horizon_on_next_frame_does_not_reuse_last_finish() -> None:
    engineer = _engineer(tank_capacity_l=50)
    assert _drive(engineer, session_type="Race", session_laps_remaining=18)["fuel_to_add_l"]
    result = engineer.feed(_snapshot(61), session_type="Race")
    assert result["status"] == "READY"
    assert result["fuel_needed_to_finish_l"] is None
    assert result["fuel_to_add_l"] is None
    assert result["minimum_stops"] is None


@pytest.mark.parametrize("field,value", [
    ("on_pit_road", True), ("in_pit_stall", True), ("pitstop_active", True),
    ("player_track_surface", 0), ("player_incident_count", 1),
    ("session_flags", 0x4000), ("session_flags", 0x8), ("session_flags", 1),
])
def test_dirty_current_frame_invalidates_ready_plan_and_discards_lap(
    field: str, value: object,
) -> None:
    engineer = _engineer(tank_capacity_l=50)
    assert _drive(engineer, session_type="Race", session_laps_remaining=18)["status"] == "READY"
    result = engineer.feed(_snapshot(61, **{field: value}), session_type="Race")
    assert result["status"] == "BLOCKED"
    assert result["valid_laps"] == 2
    _assert_no_estimate(result)
    # The pit/out/dirty lap ends at crossing 4; it cannot become a third fuel sample.
    after = {"player_incident_count": 1} if field == "player_incident_count" else {}
    result = _drive(engineer, start=62, end=80, **after)
    assert result["valid_laps"] == 2
    result = _drive(engineer, start=81, end=100, **after)
    assert result["valid_laps"] == 3


@pytest.mark.parametrize("kind", [
    "pit_road_entered", "pit_road_exited", "pit_stall_entered", "pit_stall_exited",
    "quality_rejected", "source_stale", "source_resumed",
])
def test_entire_event_batch_blocks_even_if_last_frame_has_recovered(kind: str) -> None:
    engineer = _engineer()
    _drive(engineer)
    snapshot = _snapshot(61)
    snapshot["events"] = [{"kind": kind, "details": {}}]
    result = engineer.feed(snapshot)
    assert result["status"] == "BLOCKED"
    _assert_no_estimate(result)
    assert _drive(engineer, start=62, end=80)["valid_laps"] == 2


def test_interval_summary_and_transient_flags_cannot_be_hidden_by_clean_last_tick() -> None:
    for change in (
        {"interval_invalid_for_fuel": ["OFF_TRACK_INTERVAL"]},
        {"events": [{"kind": "flag_changed", "details": {
            "previous_flags": 0x4000, "current_flags": 4,
        }}]},
    ):
        engineer = _engineer()
        _drive(engineer)
        snapshot = _snapshot(61)
        snapshot.update(change)
        result = engineer.feed(snapshot)
        assert result["status"] == "BLOCKED"
        _assert_no_estimate(result)


def test_refuel_invalidates_plan_and_outlap_while_retaining_same_session_history() -> None:
    engineer = _engineer()
    _drive(engineer)
    result = engineer.feed(_snapshot(61, fuel_level_l=39))
    assert result["reason_codes"] == ["REFUEL_DETECTED"]
    assert result["valid_laps"] == 2
    _assert_no_estimate(result)
    result = engineer.feed(_snapshot(62, fuel_level_l=38.95))
    assert result["status"] == "LEARNING"
    _assert_no_estimate(result)
    assert result["valid_laps"] == 2


@pytest.mark.parametrize("change", [
    {"binding_sha256": "b" * 64},
    {"events": [{"kind": "session_reset"}]},
    {"events": [{"kind": "source_reset"}]},
    {"sequence": 0}, {"session_time_us": 100},
    {"telemetry": {"session_num": 1}},
    {"telemetry": {"player_car_idx": 1}},
    {"telemetry": {"laps_completed": 0}},
])
def test_session_source_slot_and_counter_resets_clear_all_history(
    change: dict[str, object],
) -> None:
    engineer = _engineer()
    _drive(engineer)
    snapshot = _snapshot(61)
    for key, value in change.items():
        if key == "telemetry":
            snapshot[key].update(value)
        else:
            snapshot[key] = value
    result = engineer.feed(snapshot)
    assert result["status"] == "BLOCKED"
    assert result["valid_laps"] == 0
    _assert_no_estimate(result)


def test_spectator_and_replay_sources_cannot_reuse_driver_fuel_history() -> None:
    for change in (
        {"source_kind": "REPLAY_SDK_PROXY"},
        {"context": {"sim_source_mode": "FULL", "conflicts": [],
                     "player_control_state": "OUT_OF_CAR_OR_REPLAY_VIEW"}},
    ):
        engineer = _engineer()
        _drive(engineer)
        snapshot = _snapshot(61)
        snapshot.update(change)
        result = engineer.feed(snapshot)
        assert result["status"] == "WAIT_CAR"
        assert result["current_fuel_l"] is None
        assert result["valid_laps"] == 0
        _assert_no_estimate(result)


@pytest.mark.parametrize("field,value", [
    ("fuel_level_l", None), ("fuel_level_l", True), ("fuel_level_l", math.nan),
    ("fuel_level_l", math.inf), ("fuel_level_l", -1), ("fuel_level_l", 51),
    ("lap_distance_pct", None), ("lap_distance_pct", True), ("lap_distance_pct", 1.1),
    ("laps_completed", 3.0), ("laps_completed", True),
    ("player_incident_count", None), ("player_incident_count", True),
    ("on_pit_road", None), ("in_pit_stall", 0), ("pitstop_active", None),
    ("player_track_surface", None), ("session_flags", None),
])
def test_missing_and_malformed_fields_do_not_reuse_estimates(field: str, value: object) -> None:
    engineer = _engineer(tank_capacity_l=50)
    _drive(engineer)
    result = engineer.feed(_snapshot(61, **{field: value}))
    assert result["status"] == "BLOCKED"
    _assert_no_estimate(result)
    if field == "fuel_level_l":
        assert result["current_fuel_l"] is None
    else:
        assert result["current_fuel_l"] is not None
    json.dumps(result, allow_nan=False)


def test_sparse_dropped_ticks_still_learn_but_real_snapshot_gap_rejects_lap() -> None:
    engineer = _engineer()

    def drop_one(snapshot: dict[str, object]) -> None:
        snapshot["status"] = "DEGRADED"
        snapshot["quality"] = {
            "status": "DEGRADED", "stale": False, "dropped_ticks": 1,
            "issues": ["DROPPED_TICKS"],
        }
        snapshot["events"] = [{"kind": "dropped_ticks", "details": {"count": 1}}]

    assert _drive(engineer, alter=drop_one)["status"] == "READY"
    snapshot = _snapshot(61)
    snapshot["session_time_us"] += 1_000_001  # 1.500001 seconds since previous snapshot
    result = engineer.feed(snapshot)
    assert result["reason_codes"] == ["SNAPSHOT_GAP"]
    _assert_no_estimate(result)


def test_exact_snapshot_gap_limit_and_sequence_discontinuity() -> None:
    engineer = _engineer()
    engineer.feed(_snapshot(5))
    snapshot = _snapshot(6)
    snapshot["session_time_us"] += 1_000_000  # Exactly 1.5 s is allowed.
    assert engineer.feed(snapshot)["status"] == "LEARNING"
    result = engineer.feed(_snapshot(10))
    assert result["reason_codes"] == ["SNAPSHOT_GAP"]


@pytest.mark.parametrize("updates", [
    {"laps_completed": 5}, {"lap_distance_pct": 0.001},
])
def test_lap_jump_or_unpaired_wrap_never_counts_as_completed_lap(
    updates: dict[str, object],
) -> None:
    engineer = _engineer()
    _drive(engineer)
    result = engineer.feed(_snapshot(61, **updates))
    assert result["reason_codes"] == ["LAP_POSITION_DISCONTINUITY"]
    assert result["valid_laps"] == 2
    _assert_no_estimate(result)


def test_stale_current_quality_and_external_watchdog_reset_clear_old_plan() -> None:
    engineer = _engineer()
    _drive(engineer)
    snapshot = _snapshot(61)
    snapshot["quality"]["stale"] = True
    result = engineer.feed(snapshot)
    assert result["status"] == "BLOCKED"
    _assert_no_estimate(result)
    result = engineer.reset("SOURCE_STALE")
    assert result["reason_codes"] == ["SOURCE_STALE"]
    assert result["valid_laps"] == 0
    assert result["current_fuel_l"] is None


def test_history_is_bounded_and_uses_same_nearest_rank_statistic_as_fuel_model() -> None:
    engineer = _engineer(max_history_laps=3)

    def varying_burn(snapshot: dict[str, object]) -> None:
        telemetry = snapshot["telemetry"]
        distance = telemetry["laps_completed"] + telemetry["lap_distance_pct"]
        completed = math.floor(distance)
        burns = [1, 1, 2, 3, 4, 5, 6]
        telemetry["fuel_level_l"] = (
            40 - sum(burns[:completed]) - burns[completed] * (distance % 1)
        )

    result = _drive(engineer, end=120, alter=varying_burn)
    assert result["valid_laps"] == 3
    assert len(engineer._history) == 3
    # At changing burn rates, line interpolation has its explicitly estimated
    # half-sample error; the conservative nearest rank is the maximum admitted.
    assert result["conservative_burn_l_per_lap"] == max(
        sample.fuel_burn_l for sample in engineer._history
    )
    assert result["conservative_burn_l_per_lap"] == pytest.approx(5)


def test_missing_interval_guard_or_identity_fail_closed() -> None:
    for field in ("interval_invalid_for_fuel", "binding_sha256", "session_time_us"):
        engineer = _engineer()
        _drive(engineer)
        snapshot = _snapshot(61)
        del snapshot[field]
        result = engineer.feed(snapshot)
        assert result["status"] == "BLOCKED"
        assert result["valid_laps"] == 0
        _assert_no_estimate(result)


@pytest.mark.parametrize("interval", [
    "OUT_OF_CAR_INTERVAL", "NONLIVE_INTERVAL", "IDENTITY_CHANGED_INTERVAL",
])
def test_transient_context_exit_clears_history_even_when_last_frame_is_live(interval: str) -> None:
    engineer = _engineer()
    _drive(engineer)
    snapshot = _snapshot(61)
    snapshot["interval_invalid_for_fuel"] = [interval]
    result = engineer.feed(snapshot)
    assert result["valid_laps"] == 0
    assert result["status"] == "BLOCKED"
    _assert_no_estimate(result)


@pytest.mark.parametrize("field", ["laps_completed", "player_incident_count"])
def test_counter_reset_cannot_hide_behind_missing_field_or_pit_interval(field: str) -> None:
    engineer = _engineer()
    _drive(engineer, player_incident_count=5)
    result = engineer.feed(_snapshot(
        61, **{"player_incident_count": 5, field: None}, on_pit_road=True,
    ))
    assert result["status"] == "BLOCKED"
    assert result["valid_laps"] == 2
    result = engineer.feed(_snapshot(
        62, **{"player_incident_count": 5, field: 0}, on_pit_road=True,
    ))
    assert result["reason_codes"] == ["LAP_OR_INCIDENT_COUNTER_RESET"]
    assert result["valid_laps"] == 0


def test_empirical_quantile_has_mean_floor_and_zero_burn_cannot_be_learned() -> None:
    engineer = _engineer(conservative_quantile=0.5)

    def varying(snapshot: dict[str, object]) -> None:
        telemetry = snapshot["telemetry"]
        distance = telemetry["laps_completed"] + telemetry["lap_distance_pct"]
        completed = math.floor(distance)
        burns = [1, 1, 1, 4, 1]
        telemetry["fuel_level_l"] = (
            40 - sum(burns[:completed]) - burns[completed] * (distance % 1)
        )

    result = _drive(engineer, end=80, alter=varying)
    assert result["valid_laps"] == 3
    burns = [sample.fuel_burn_l for sample in engineer._history]
    assert result["conservative_burn_l_per_lap"] == pytest.approx(sum(burns) / len(burns))
    result = _drive(_engineer(), fuel_level_l=40)
    assert result["valid_laps"] == 0
    _assert_no_estimate(result)


def test_reserve_is_not_available_range_and_empty_tank_does_not_suggest_negative_fuel() -> None:
    engineer = _engineer(tank_capacity_l=50)
    _drive(engineer)
    result = engineer.feed(_snapshot(61, fuel_level_l=1), session_type="Race")
    assert result["status"] == "READY"
    assert result["estimated_laps_remaining"] == 0
    assert result["fuel_to_add_l"] is None
    result = engineer.feed(_snapshot(62, fuel_level_l=0, session_laps_remaining=2),
                           session_type="Race")
    assert result["estimated_laps_remaining"] == 0
    assert result["fuel_to_add_l"] == 6
    assert result["minimum_stops"] == 1


def test_original_inputs_never_promote_replay_or_quality_rejected_to_ready() -> None:
    for change in (
        {"source_kind": "IBT_FILE"},
        {"source_kind": "SYNTHETIC"},
        {"quality": {"status": "REJECTED", "stale": False, "issues": []}},
        {"context": {"sim_source_mode": "FULL", "player_control_state": "IN_CAR_PHYSICS",
                     "conflicts": ["CONFLICT"]}},
    ):
        engineer = _engineer()
        _drive(engineer)
        snapshot = _snapshot(61)
        snapshot.update(change)
        result = engineer.feed(snapshot)
        assert result["status"] in {"WAIT_CAR", "BLOCKED"}
        _assert_no_estimate(result)


def test_stop_lower_bound_combines_fractional_stint_capacity_without_rounding_each_down() -> None:
    engineer = _engineer(tank_capacity_l=7.8)

    def small_tank(snapshot: dict[str, object]) -> None:
        snapshot["telemetry"]["fuel_level_l"] -= 32.2

    assert _drive(engineer, alter=small_tank)["valid_laps"] == 2
    assert engineer.feed(_snapshot(61, fuel_level_l=7.8))["status"] == "BLOCKED"
    for step in range(62, 81):
        result = engineer.feed(
            _snapshot(step, fuel_level_l=7.8 - 0.01 * (step - 61), session_laps_remaining=5),
            session_type="Race",
        )
    assert result["status"] == "READY"
    assert result["conservative_burn_l_per_lap"] == pytest.approx(2)
    # Current usable range is ~2.805 laps, full usable range 2.9 laps. Together
    # they cover five laps in one stop; floor(2.805) + floor(2.9) must not lose one.
    assert result["estimated_laps_remaining"] == 2
    assert result["minimum_stops"] == 1
    assert result["fuel_to_add_l"] is None  # Whole finish still exceeds one tank.


def test_stop_lower_bound_restores_reserve_during_first_fill_when_current_fuel_is_empty() -> None:
    engineer = _engineer(tank_capacity_l=50)
    _drive(engineer)
    result = engineer.feed(
        _snapshot(61, fuel_level_l=0, session_laps_remaining=24), session_type="Race",
    )
    assert result["status"] == "READY"
    assert result["fuel_needed_to_finish_l"] == 50
    assert result["fuel_to_add_l"] == 50
    assert result["minimum_stops"] == 1
