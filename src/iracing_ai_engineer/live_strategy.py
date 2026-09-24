"""Bounded live fuel-stop comparisons, explicitly conditional and advisor-only.

Use the existing whole-lap planning arithmetic without fabricating historical
lap samples or sealed offline receipts. Session-scoped user parameters are not
SDK observations, official event rules or measured pit calibration.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from .fuel import whole_lap_fuel_window

CONTRACT = "live-fuel-stop-comparison-v2"


def _map(value):
    return value if isinstance(value, Mapping) else {}


def _number(value, low=0, high=1000):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def _integer(value, high=100_000):
    return type(value) is int and 0 <= value <= high


@dataclass(frozen=True)
class StrategyParameters:
    tank_capacity_l: float
    refuel_rate_l_per_s: float | None = None
    pit_loss_low_s: float | None = None
    pit_loss_high_s: float | None = None
    pit_entry_fraction: float | None = None
    pit_exit_fraction: float | None = None
    complete_pit_loss_low_s: float | None = None
    complete_pit_loss_high_s: float | None = None
    tire_change_time_s: float | None = None
    fuel_tire_service_timing: str | None = None
    other_service_low_s: float | None = None
    other_service_high_s: float | None = None

    def __post_init__(self):
        if not _number(self.tank_capacity_l, .1):
            raise ValueError("STRATEGY_PARAMETERS_INVALID")
        if self.refuel_rate_l_per_s is not None and not _number(self.refuel_rate_l_per_s, .05, 50):
            raise ValueError("STRATEGY_PARAMETERS_INVALID")
        losses = (self.pit_loss_low_s, self.pit_loss_high_s)
        if losses != (None, None) and not (
            all(_number(value) for value in losses) and losses[0] <= losses[1]
        ):
            raise ValueError("STRATEGY_PARAMETERS_INVALID")
        tires = (self.tire_change_time_s, self.fuel_tire_service_timing)
        if tires != (None, None) and not (
            _number(tires[0], .1, 600) and type(tires[1]) is str
            and tires[1] in ("PARALLEL", "SEQUENTIAL")
            and self.refuel_rate_l_per_s is not None
        ):
            raise ValueError("STRATEGY_PARAMETERS_INVALID")
        other = (self.other_service_low_s, self.other_service_high_s)
        if other != (None, None) and not (
            all(_number(value, high=600) for value in other) and other[0] <= other[1]
            and self.refuel_rate_l_per_s is not None and self.pit_loss_low_s is not None
        ):
            raise ValueError("STRATEGY_PARAMETERS_INVALID")
        complete = (self.complete_pit_loss_low_s, self.complete_pit_loss_high_s)
        if complete != (None, None) and not (
            all(_number(value, .1, 600) for value in complete) and complete[0] <= complete[1]
            and other == (None, None)
        ):
            raise ValueError("STRATEGY_PARAMETERS_INVALID")
        geometry = (self.pit_entry_fraction, self.pit_exit_fraction)
        if (geometry != (None, None) or complete != (None, None)) and not (
            all(_number(value, high=1) and value < 1 for value in geometry)
            and geometry[0] != geometry[1]
            and (complete != (None, None) or other != (None, None))
        ):
            raise ValueError("STRATEGY_PARAMETERS_INVALID")


def service_cost_options(parameters, fuel_add_l):
    """Conditional service arithmetic, never tire condition or a service command.

    Other service is an explicitly supplied *additional*, non-overlapping cost
    covering every remaining overhead. Missing is not zero. Fixed complete-loss
    inputs are deliberately not mixed with this component model.
    """
    rate = parameters.refuel_rate_l_per_s
    if rate is None:
        return []
    fuel_time = fuel_add_l / rate
    options = [("FUEL_ONLY", 0.0)]
    if parameters.tire_change_time_s is not None:
        options.append(("FOUR_TIRES", parameters.tire_change_time_s))
    rows = []
    for name, tire_time in options:
        service = (fuel_time + tire_time if parameters.fuel_tire_service_timing == "SEQUENTIAL"
                   else max(fuel_time, tire_time))
        partial = ([parameters.pit_loss_low_s + service, parameters.pit_loss_high_s + service]
                   if parameters.pit_loss_low_s is not None else None)
        complete = ([partial[0] + parameters.other_service_low_s,
                     partial[1] + parameters.other_service_high_s]
                    if partial is not None and parameters.other_service_low_s is not None else None)
        rows.append({
            "service_option": name, "fuel_time_s": round(fuel_time, 6),
            "tire_time_s": tire_time, "stationary_service_time_s": round(service, 6),
            "extra_tire_time_s": round(service - fuel_time, 6),
            "partial_loss_range_s": [round(n, 6) for n in partial] if partial is not None else None,
            "complete_loss_range_s": ([round(n, 6) for n in complete]
                                      if complete is not None else None),
        })
    return rows


def source_scope(monitor, generation):
    telemetry = _map(monitor.get("telemetry"))
    binding = monitor.get("binding_sha256")
    if not (type(binding) is str and len(binding) == 64
            and all(char in "0123456789abcdef" for char in binding)
            and _integer(generation, 2**53)
            and _integer(telemetry.get("session_num"))
            and _integer(telemetry.get("player_car_idx"), 255)):
        return None
    return (generation, binding, telemetry["session_num"], telemetry["player_car_idx"])


def source_ready(monitor):
    context, quality = _map(monitor.get("context")), _map(monitor.get("quality"))
    reasons = monitor.get("reasons")
    return (
        monitor.get("contract_version") == "live-monitor-v1"
        and monitor.get("record_type") == "live_monitor_snapshot"
        and monitor.get("advisor_only") is True and monitor.get("executable") is False
        and monitor.get("source_kind") == "SDK_LIVE"
        and monitor.get("status") in ("READY", "DEGRADED")
        and quality.get("status") in ("READY", "DEGRADED") and quality.get("stale") is False
        and context.get("sim_source_mode") == "FULL"
        and context.get("player_control_state") == "IN_CAR_PHYSICS"
        and context.get("conflicts") == []
        and _integer(monitor.get("sequence"), 2**53)
        and _integer(monitor.get("session_time_us"), 2**53)
        and type(reasons) is list and all(type(reason) is str for reason in reasons)
    )


def project_strategy(monitor, fuel, session_type, parameters, revision):
    """At most two feasible boundary scenarios, never an executable pit action."""
    result = {
        "contract_version": CONTRACT, "advisor_only": True, "executable": False,
        "estimate_only": True, "live_acceptance": False,
        "source_kind": monitor.get("source_kind"), "binding_sha256": monitor.get("binding_sha256"),
        "monitor_sequence": monitor.get("sequence"),
        "session_time_us": monitor.get("session_time_us"),
        "configuration_revision": revision, "status": "WAIT", "reason": "SOURCE_NOT_READY",
        "parameters_provenance": "USER_RULE", "plan": None, "scenarios": [],
        "event_rules": "UNVERIFIED", "tire_service": "UNAVAILABLE", "rejoin": "UNAVAILABLE",
    }
    if not source_ready(monitor):
        return result
    if parameters is None:
        return {**result, "reason": "PARAMETERS_NOT_CONFIRMED"}
    if type(parameters) is not StrategyParameters:
        return {**result, "reason": "PARAMETERS_INVALID"}
    if session_type != "Race":
        return {**result, "reason": "RACE_HORIZON_UNAVAILABLE"}
    telemetry = _map(monitor.get("telemetry"))
    amount, burn, reserve = (fuel.get(key) for key in (
        "current_fuel_l", "conservative_burn_l_per_lap", "reserve_l"))
    valid, required = fuel.get("valid_laps"), fuel.get("required_laps")
    if not (
        fuel.get("status") == "READY" and fuel.get("advisor_only") is True
        and fuel.get("estimate_only") is True and fuel.get("executable") is False
        and monitor.get("interval_invalid_for_fuel") == []
        and "READ_ERROR:FuelLevel" not in monitor["reasons"]
        and _integer(valid) and _integer(required) and 2 <= required <= valid
        and _number(amount) and _number(burn, .000001) and _number(reserve)
        and _number(telemetry.get("fuel_level_l"))
        and math.isclose(amount, telemetry["fuel_level_l"], abs_tol=1e-6, rel_tol=1e-9)
    ):
        return {**result, "reason": "FUEL_MODEL_UNAVAILABLE"}
    capacity = parameters.tank_capacity_l
    if capacity <= reserve or amount > capacity or burn > capacity:
        return {**result, "reason": "CAPACITY_CONTRADICTS_FUEL"}
    horizon, basis, needed = (fuel.get(key) for key in (
        "race_laps_to_go", "race_horizon_basis", "fuel_needed_to_finish_l"))
    if not (_integer(horizon) and basis in (
        "SDK_LAPS_REMAINING", "TIMER_FASTEST_LAP_PLUS_MARGIN")
        and _number(needed, high=100_000_000)
        and math.isclose(needed, 0 if horizon == 0 else burn * horizon + reserve,
                         abs_tol=1e-6, rel_tol=1e-9)):
        return {**result, "reason": "RACE_HORIZON_UNAVAILABLE"}
    current, full, stops, window = whole_lap_fuel_window(
        current_fuel_l=amount, tank_capacity_l=capacity, reserve_l=reserve,
        burn_l_per_lap=burn, remaining_laps=horizon,
    )
    if stops is None:
        return {**result, "reason": "TANK_CANNOT_COVER_A_LAP"}
    if horizon > 0 and amount < reserve:
        return {**result, "reason": "ALREADY_BELOW_RESERVE"}
    result.update(status="READY", reason="CONDITIONAL_FUEL_COMPARISON", plan={
        "remaining_laps": horizon, "horizon_basis": basis, "reserve_l": reserve,
        "burn_l_per_lap": burn, "current_fuel_l": amount, "tank_capacity_l": capacity,
        "full_tank_laps": full, "current_fuel_laps": current,
        "fuel_stops": stops,
        "earliest_laps_from_now": window.earliest_lap_from_now if window else None,
        "latest_laps_from_now": window.latest_lap_from_now if window else None,
    })
    if window is None:
        result["reason"] = "NO_FUEL_STOP_IN_BUDGET"
        return result
    if parameters.tire_change_time_s is not None:
        result["tire_service"] = "CONDITIONAL_SERVICE_TIME_ONLY"
    # Compare only the feasible endpoints, not every lap of a long endurance
    # horizon. A next fill covers the minimum next stint preserving this stop
    # count; later fills may still be required. This is not cumulative deficit.
    for laps in dict.fromkeys((window.earliest_lap_from_now, window.latest_lap_from_now)):
        fuel_at_stop = amount - burn * laps
        next_stint = max(1, horizon - laps - (stops - 1) * full)
        target = reserve + burn * next_stint
        add = max(0.0, target - fuel_at_stop)
        if fuel_at_stop < reserve - 1e-6 or target > capacity + 1e-6:
            return {**result, "status": "WAIT", "reason": "NO_FEASIBLE_COMPARISON",
                    "plan": None, "scenarios": []}
        rate = parameters.refuel_rate_l_per_s
        pumping = add / rate if rate is not None else None
        loss = ([parameters.pit_loss_low_s + pumping, parameters.pit_loss_high_s + pumping]
                if pumping is not None and parameters.pit_loss_low_s is not None else None)
        result["scenarios"].append({
            "laps_from_now": laps, "arrival_fuel_l": round(fuel_at_stop, 6),
            "next_stint_laps": next_stint, "target_fuel_l": round(target, 6),
            "fuel_add_l": round(add, 6), "further_stops": stops - 1,
            "pumping_time_s": round(pumping, 6) if pumping is not None else None,
            "total_loss_range_s": [round(value, 6) for value in loss] if loss else None,
            "service_options": service_cost_options(parameters, add),
        })
    return result


def validated_strategy(snapshot):
    """Recompute fixed numeric scenarios and bind them to the current publication."""
    monitor, value = _map(snapshot.get("monitor")), snapshot.get("strategy")
    configuration = _map(snapshot.get("strategy_configuration"))
    if not isinstance(value, Mapping) or not _integer(configuration.get("revision"), 2**53):
        return None
    parameters, scope = None, None
    if configuration.get("status") == "ACTIVE":
        scope = source_scope(monitor, snapshot.get("generation"))
        if scope is None or not _same_typed_tree(configuration.get("scope"), list(scope)):
            return None
        try:
            inputs = configuration.get("inputs")
            if (type(inputs) is not dict
                    or set(inputs) != set(StrategyParameters.__dataclass_fields__)):
                return None
            parameters = StrategyParameters(**inputs)
        except (TypeError, ValueError):
            return None
    elif configuration.get("status") != "UNCONFIGURED":
        return None
    if not _same_typed_tree(configuration, configuration_projection(
        parameters, scope, configuration["revision"]
    )):
        return None
    expected = project_strategy(monitor, _map(snapshot.get("fuel")), snapshot.get("session_type"),
                                parameters, configuration["revision"])
    return expected if _same_typed_tree(value, expected) else None


def _same_typed_tree(value, expected):
    # Walk only the small expected tree. Bool/int aliases, added free text and
    # unexpected containers cannot pass a plain Python equality comparison.
    if type(value) is not type(expected):
        return False
    if type(expected) is dict:
        return value.keys() == expected.keys() and all(
            _same_typed_tree(value[key], item) for key, item in expected.items())
    if type(expected) is list:
        return len(value) == len(expected) and all(
            _same_typed_tree(item, other) for item, other in zip(value, expected, strict=True))
    return value == expected


def strategy_binding(snapshot):
    value = _map(snapshot.get("strategy"))
    plan = _map(value.get("plan"))
    return (snapshot.get("strategy_revision"),
            _map(snapshot.get("strategy_configuration")).get("revision"), value.get("status"),
            value.get("reason"), *(plan.get(key) for key in (
                "fuel_stops", "earliest_laps_from_now", "latest_laps_from_now",
                "remaining_laps", "horizon_basis", "burn_l_per_lap", "reserve_l")))


def strategy_notice(snapshot):
    value = _map(snapshot.get("strategy"))
    reason = value.get("reason")
    return {
        "SOURCE_NOT_READY": "进站比较等待新鲜的本人车内驾驶数据。",
        "PARAMETERS_NOT_CONFIRMED": "请先在本地策略设置确认本次连接的有效油箱容量。",
        "RACE_HORIZON_UNAVAILABLE": "当前没有已确认的比赛剩余赛程，暂不能计算进站窗口。",
        "FUEL_MODEL_UNAVAILABLE": "完整有效圈耗油模型未就绪，暂不能比较进站方案。",
        "CAPACITY_CONTRADICTS_FUEL": "所填油箱容量与当前油量、耗油或储备矛盾，请核对。",
        "ALREADY_BELOW_RESERVE": "当前燃油已低于配置储备，不能保证到达进站口；请核对游戏油量。",
        "TANK_CANNOT_COVER_A_LAP": "满油仍不足以覆盖一圈并保留储备，暂不能提供进站方案。",
        "PROCESSING_ERROR": "进站比较故障；近车、燃油和驾驶分析独立运行。",
    }.get(reason if type(reason) is str else None,
          "进站比较暂不可用，请核对本地策略设置和实时证据。")


def configuration_projection(parameters, scope, revision):
    return {"status": "ACTIVE" if parameters is not None else "UNCONFIGURED",
            "revision": revision, "scope": list(scope) if scope is not None else None,
            "inputs": asdict(parameters) if parameters is not None else None,
            "provenance": "USER_RULE", "persisted": False}
