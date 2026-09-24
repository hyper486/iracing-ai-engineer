"""Mapped, source-bound pit scenarios; no pit-entry instruction or race ranking."""

from __future__ import annotations

import math
from collections.abc import Mapping

from .live_motion import validated_motion
from .live_strategy import (
    StrategyParameters,
    _map,
    _same_typed_tree,
    service_cost_options,
    validated_strategy,
)
from .rejoin_projection import phase_time, project_phase_rejoin

CONTRACT = "live-mapped-rejoin-v2"


def unavailable_rejoin(snapshot, reason="SOURCE_NOT_READY"):
    monitor = _map(snapshot.get("monitor"))
    return {
        "contract_version": CONTRACT,
        "advisor_only": True,
        "executable": False,
        "live_acceptance": False,
        "conditional_only": True,
        "source_kind": monitor.get("source_kind"),
        "binding_sha256": monitor.get("binding_sha256"),
        "monitor_sequence": monitor.get("sequence"),
        "session_time_us": monitor.get("session_time_us"),
        "configuration_revision": _map(snapshot.get("strategy_configuration")).get("revision"),
        "motion_revision": _map(snapshot.get("motion")).get("revision"),
        "status": "WAIT",
        "reason": reason,
        "scenarios": [],
        "assumptions_provenance": "USER_RULE",
        "event_rules": "UNVERIFIED",
    }


def project_rejoin(snapshot):
    result = unavailable_rejoin(snapshot)
    if _map(snapshot.get("motion")).get("reason") == "MOTION_PROCESSING_ERROR":
        return {**result, "reason": "MOTION_PROCESSING_ERROR"}
    strategy = validated_strategy(snapshot)
    if strategy is None or strategy["status"] != "READY":
        return {**result, "reason": "FUEL_SCENARIOS_UNAVAILABLE"}
    inputs = snapshot["strategy_configuration"].get("inputs") or {}
    if inputs.get("pit_entry_fraction") is None:
        return {**result, "reason": "COMPLETE_PIT_ASSUMPTIONS_REQUIRED"}
    plan = strategy["plan"]
    if plan["fuel_stops"] == 0:
        return {**result, "reason": "NO_FUEL_STOP_IN_BUDGET"}
    monitor = snapshot["monitor"]
    telemetry, reasons = monitor.get("telemetry", {}), monitor.get("reasons", [])
    if (
        telemetry.get("pits_open") is not True
        or "READ_ERROR:PitsOpen" in reasons
        or type(telemetry.get("session_flags")) is not int
        or "READ_ERROR:SessionFlags" in reasons
        or telemetry["session_flags"] & (0x00330000 | 0x0008 | 0x0010 | 0x0100 | 0xC000)
    ):
        return {**result, "reason": "PIT_PERMISSION_OR_FLAGS_UNRESOLVED"}
    motion = validated_motion(snapshot)
    if motion is None:
        reason = _map(snapshot.get("motion")).get("reason")
        return {
            **result,
            "reason": "MOTION_PROCESSING_ERROR"
            if reason == "MOTION_PROCESSING_ERROR"
            else "COMPLETE_LAP_MOTION_REQUIRED",
        }
    position = motion["player"]["progress_laps"]
    entry, exit_ = inputs["pit_entry_fraction"], inputs["pit_exit_fraction"]
    next_entry = math.ceil(position - entry - 1e-9) + entry
    first_distance = max(0.0, next_entry - position)
    first = max(0, math.ceil(plan["earliest_laps_from_now"] - first_distance - 1e-9))
    last = math.floor(plan["latest_laps_from_now"] - first_distance + 1e-9)
    if last < first:
        return {**result, "reason": "NO_REACHABLE_PIT_ENTRY_IN_FUEL_WINDOW"}
    parameters = StrategyParameters(**inputs)
    profiles = motion["player"]["lap_profiles"]
    rows = []
    for endpoint, extra in zip(("early", "late"), dict.fromkeys((first, last)), strict=False):
        entry_position = next_entry + extra
        entry_distance = max(0, entry_position - position)
        exit_position = entry_position + ((exit_ - entry) % 1)
        exit_distance = exit_position - position
        arrival = plan["current_fuel_l"] - plan["burn_l_per_lap"] * entry_distance
        next_stint = max(
            1.0,
            plan["remaining_laps"]
            - entry_distance
            - (plan["fuel_stops"] - 1) * plan["full_tank_laps"],
        )
        target = plan["reserve_l"] + plan["burn_l_per_lap"] * next_stint
        if arrival < plan["reserve_l"] - 1e-6 or target > plan["tank_capacity_l"] + 1e-6:
            return {**result, "reason": "MAPPED_FUEL_SCENARIO_INVALID"}
        row = {
            "entry_progress_laps": round(entry_position, 9),
            "exit_progress_laps": round(exit_position, 9),
            "distance_to_entry_laps": round(entry_distance, 9),
            "distance_to_exit_laps": round(exit_distance, 9),
            "arrival_fuel_l": round(arrival, 6),
            "fuel_add_l": round(max(0, target - arrival), 6),
            "target_fuel_l": round(target, 6),
            "next_stint_laps": round(next_stint, 9),
            "further_stops": plan["fuel_stops"] - 1,
            "endpoint": endpoint,
            "ahead": None,
            "behind": None,
            "status": "WAIT",
            "reason_codes": [],
        }
        if parameters.other_service_low_s is not None:
            options = service_cost_options(parameters, max(0, target - arrival))
            basis = "USER_COMPONENT_SUM"
        else:
            options = [{"service_option": "UNSPECIFIED", "complete_loss_range_s": [
                inputs["complete_pit_loss_low_s"], inputs["complete_pit_loss_high_s"]]}]
            basis = "USER_COMPLETE_TOTAL"
        for option in options:
            loss = option["complete_loss_range_s"]
            variant = {**row, "service_option": option["service_option"],
                       "loss_basis": basis, "complete_loss_range_s": loss}
            if loss is None or not .1 <= loss[0] <= loss[1] <= 600:
                variant["reason_codes"] = ["SERVICE_LOSS_OUT_OF_BOUNDS"]
            elif entry_distance > 2:
                variant["reason_codes"] = ["FORECAST_BEYOND_TWO_LAPS"]
            elif exit_distance >= plan["remaining_laps"]:
                variant["reason_codes"] = ["EXIT_AFTER_BUDGET_HORIZON"]
            elif max((phase_time(profile, exit_position) - phase_time(profile, position)) / 1e6
                     + loss[1] for profile in profiles) > 3 * min(
                         profile["elapsed_us"][-1] / 1e6 for profile in profiles):
                variant["reason_codes"] = ["FORECAST_TIME_HORIZON_EXCEEDED"]
            else:
                ahead, behind, reasons = project_phase_rejoin(
                    motion, exit_progress_laps=exit_position, loss_range_s=loss
                )
                variant.update(ahead=ahead, behind=behind, reason_codes=reasons,
                               status="READY" if not reasons else "WAIT")
            rows.append(variant)
    ready = any(row["status"] == "READY" for row in rows)
    return {
        **result,
        "scenarios": rows,
        "status": "READY" if ready else "WAIT",
        "reason": "CONDITIONAL_MAPPED_REJOIN" if ready else "REJOIN_SCENARIOS_UNCERTAIN",
    }


def validated_rejoin(snapshot):
    value = snapshot.get("rejoin")
    if not isinstance(value, Mapping):
        return None
    if value.get("reason") == "REJOIN_PROCESSING_ERROR":
        expected = unavailable_rejoin(snapshot, "REJOIN_PROCESSING_ERROR")
        return value if _same_typed_tree(value, expected) else None
    expected = project_rejoin(snapshot)
    return value if _same_typed_tree(value, expected) else None


def rejoin_binding(snapshot):
    value = _map(snapshot.get("rejoin"))
    rows = value.get("scenarios")
    if type(rows) is not list:
        rows = []

    def neighbor(row):
        if not isinstance(row, Mapping) or type(row.get("gap_range_s")) is not list:
            return None
        bounds = row["gap_range_s"]
        if len(bounds) != 2 or not all(type(n) in (int, float) and 0 <= n <= 2000 for n in bounds):
            return None
        # Material proximity changes withdraw old speech; minor drift does not
        # continually restart synthesis. An independent ten-second TTL remains.
        return (row.get("car_idx"), *(next((x for x in (2, 5, 10, 20) if n <= x), 99)
                                      for n in bounds))

    return (
        snapshot.get("rejoin_revision"),
        value.get("configuration_revision"),
        value.get("motion_revision"),
        value.get("status"),
        value.get("reason"),
        tuple(
            (
                row.get("entry_progress_laps"),
                row.get("service_option"), row.get("loss_basis"),
                row.get("status"),
                tuple(row["reason_codes"]) if type(row.get("reason_codes")) is list else (),
                neighbor(row.get("ahead")),
                neighbor(row.get("behind")),
            )
            for row in rows
            if isinstance(row, Mapping)
        ),
    )


def rejoin_notice(snapshot):
    reason = _map(snapshot.get("rejoin")).get("reason")
    return {
        "FUEL_SCENARIOS_UNAVAILABLE": "尚无有效补油方案，暂不能比较出站交通。",
        "COMPLETE_PIT_ASSUMPTIONS_REQUIRED": "请先确认进出站位置，以及完整总损失或完整服务分项。",
        "NO_FUEL_STOP_IN_BUDGET": "当前燃油预算不需要补油停站，暂不建立出站方案。",
        "PIT_PERMISSION_OR_FLAGS_UNRESOLVED": "进站许可或旗号尚不支持出站推演，请核对游戏。",
        "COMPLETE_LAP_MOTION_REQUIRED": "尚缺在赛道车辆的连续两圈配速轨迹，暂不能预测出站交通。",
        "MOTION_PROCESSING_ERROR": "出站轨迹分析故障；近车播报和燃油模块独立运行。",
        "NO_REACHABLE_PIT_ENTRY_IN_FUEL_WINDOW": "当前完整圈燃油窗口内没有可映射的进站口。",
        "MAPPED_FUEL_SCENARIO_INVALID": "进站口映射与燃油预算不一致，停止出站推演。",
        "REJOIN_SCENARIOS_UNCERTAIN": "出站邻车关系不确定或方案太远，暂不给出秒差。",
        "REJOIN_PROCESSING_ERROR": "出站预测故障；近车播报和燃油模块独立运行。",
    }.get(reason, "出站预测尚未就绪，等待新鲜的本人车内数据。")
