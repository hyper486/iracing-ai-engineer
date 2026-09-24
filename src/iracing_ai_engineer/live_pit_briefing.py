"""Same-action pit briefing, with explicitly incomplete tire-performance coverage.

On-demand only: no SDK, I/O, provider, controls or work on the Spotter path.
Whole-lap tire deltas are NOT moved into a fractional mapped pit scenario.
"""

from __future__ import annotations

import json
import math

from .engineer_session import canonical_sha256
from .live_rejoin import validated_rejoin
from .live_strategy import StrategyParameters, _map, service_cost_options, validated_strategy
from .live_tire_age import validated_tire_age
from .live_tire_calibration import validated_calibration

CONTRACT = "live-same-action-pit-briefing-v1"
LIMITS = ("同一进站位置、补油量和下段预算下比较服务与交通，不混用整圈方案。"
          "位置、服务耗时是用户假设；赛事规则未核验，交通区间不是统计置信度，缺失对手不等于安全。"
          "轮胎仅按车手确认的四胎换新起点和历史线性模型，假设设置、配方、天气及干净驾驶条件不变；"
          "未来油量沿用入口起算的燃油预算，并非维修区油耗实测。"
          "仅计出站后的完整圈，不按距离分摊首尾片段；这些片段、暖胎和交通中的收益未知。"
          "观测边界不证明联合条件或新旧胎对照已验证。不是整段净收益，不排序，不证明旧胎安全，"
          "不生成留胎或进站指令。")
_ACTION_FIELDS = ("entry_progress_laps", "exit_progress_laps", "distance_to_entry_laps",
                  "distance_to_exit_laps", "arrival_fuel_l", "fuel_add_l", "target_fuel_l",
                  "next_stint_laps", "further_stops")


def _interval(low, high):
    return [math.floor(low * 1e6) / 1e6, math.ceil(high * 1e6) / 1e6]


def _covered_tires(snapshot, action, strategy, calibration, tire):
    """Entire complete-lap block must fit both age and fuel marginal domains."""
    entry, exit_ = action["entry_progress_laps"], action["exit_progress_laps"]
    # All geometry has the mapped projection's 9-decimal quantization. Restore
    # that precision after addition, not a tolerance that invents another lap.
    end = round(entry + action["next_stint_laps"], 9)
    start, stop = math.ceil(exit_), math.floor(end)
    count = max(0, stop - start)
    result = {"status": "WAIT", "reason": "CALIBRATION_NOT_READY",
        "budget_end_progress_laps": end, "complete_laps": count,
        "complete_interval_laps": [start, stop] if count else None,
        "unmodeled_partial_laps": round(max(0., end - exit_ - count), 9),
        "old_age_range": None, "new_age_range": None, "lap_start_fuel_range_l": None,
        "covered_gain_range_s": None, "extra_tire_service_s": None,
        "covered_gain_minus_service_range_s": None, "net_stint_gain_range_s": None}
    parameters = StrategyParameters(**snapshot["strategy_configuration"]["inputs"])
    # Same dose as the mapped action, not the separate whole-lap endpoint.
    dose = max(0., strategy["plan"]["reserve_l"]
        + strategy["plan"]["burn_l_per_lap"] * action["next_stint_laps"]
        - (strategy["plan"]["current_fuel_l"]
           - strategy["plan"]["burn_l_per_lap"] * action["distance_to_entry_laps"]))
    service = next((row for row in service_cost_options(parameters, dose)
                    if row["service_option"] == "FOUR_TIRES"), None)
    if action["services"][0]["loss_basis"] != "USER_COMPONENT_SUM" or service is None:
        return {**result, "reason": "TIRE_SERVICE_ASSUMPTIONS_REQUIRED"}
    result["extra_tire_service_s"] = service["extra_tire_time_s"]
    if calibration is None or calibration.model_json is None:
        return result
    if tire is None or tire["origin"] is None:
        return {**result, "reason": "CONFIRMED_TIRE_ORIGIN_REQUIRED"}
    if not count:
        return {**result, "reason": "NO_COMPLETE_POST_EXIT_LAPS"}
    current = snapshot["monitor"]["telemetry"]["laps_completed"]
    age_delta = tire["counter_increase"] + math.floor(exit_) - current
    new_ages = [start + 1 - math.floor(exit_), stop - math.floor(exit_)]
    old_ages = [age + age_delta for age in new_ages]
    burn = strategy["plan"]["burn_l_per_lap"]
    fuels = [action["target_fuel_l"] - burn * (point - entry)
             for point in (stop - 1, start)]
    result.update(old_age_range=old_ages, new_age_range=new_ages,
                  lap_start_fuel_range_l=_interval(*fuels))
    if not all(calibration.age_bounds[0] <= ages[0] <= ages[1] <= calibration.age_bounds[1]
               for ages in (old_ages, new_ages)):
        return {**result, "reason": "PROJECTED_AGES_OUTSIDE_MODEL_DOMAIN"}
    if not calibration.fuel_bounds[0] - 1e-6 <= fuels[0] <= fuels[1] <= (
        calibration.fuel_bounds[1] + 1e-6
    ):
        return {**result, "reason": "PROJECTED_FUEL_OUTSIDE_MODEL_DOMAIN"}
    model = json.loads(calibration.model_json)
    low, high = model["performance_age_slope_uncertainty_s_per_lap"]
    scale, cost = age_delta * count, service["extra_tire_time_s"]
    return {**result, "status": "CONDITIONAL", "reason": "COMPLETE_LAP_COVERAGE_ONLY",
        "covered_gain_range_s": _interval(low * scale, high * scale),
        "covered_gain_minus_service_range_s": _interval(low * scale - cost, high * scale - cost)}


def project_pit_briefing(snapshot):
    """Project from validated same-publication owners; expose no private records."""
    result = {"contract_version": CONTRACT, "advisor_only": True, "executable": False,
        "live_acceptance": False, "physical_wear": None, "race_recommendation": None,
        "status": "WAIT", "reason": "MAPPED_SCENARIOS_UNAVAILABLE", "actions": []}
    age = snapshot.get("updated_age_s")
    if (snapshot.get("source_mode") != "LIVE" or snapshot.get("connection") != "CONNECTED"
            or type(age) not in (int, float) or not 0 <= age <= 2):
        return result
    rejoin, strategy = validated_rejoin(snapshot), validated_strategy(snapshot)
    if rejoin is None or strategy is None or strategy["status"] != "READY":
        return result
    if not rejoin["scenarios"]:
        return {**result, "reason": rejoin["reason"]}
    calibration, tire = validated_calibration(snapshot), validated_tire_age(snapshot)
    actions = {}
    for row in rejoin["scenarios"]:
        key = tuple(row[field] for field in _ACTION_FIELDS)
        if key not in actions:
            geometry = dict(zip(_ACTION_FIELDS, key, strict=True))
            actions[key] = {**geometry, "endpoint": row["endpoint"], "services": [],
                "action_id": canonical_sha256({**geometry,
                    "source": rejoin["binding_sha256"], "sequence": rejoin["monitor_sequence"],
                    "configuration": rejoin["configuration_revision"]})}
        # Each service carries its own independently projected traffic. No
        # tire benefit is subtracted from pit loss: it happens AFTER rejoin.
        actions[key]["services"].append({field: row[field] for field in (
            "service_option", "loss_basis", "complete_loss_range_s", "status", "reason_codes",
            "ahead", "behind")})
    for action in actions.values():
        action["tires"] = _covered_tires(snapshot, action, strategy, calibration, tire)
    return {**result, "status": "CONDITIONAL", "reason": "SAME_ACTION_PARTIAL_EVIDENCE",
            "excluded_opponents": snapshot["motion"]["excluded_count"],
            "actions": list(actions.values())}


def pit_briefing_binding(snapshot):
    """Only material coverage changes; existing strategy/rejoin/tire bindings
    and the 10 s answer TTL also apply. Never bind sub-second action IDs.
    """
    rows = _map(snapshot.get("rejoin")).get("scenarios")
    if type(rows) is not list or len(rows) > 4:
        return ()
    if not rows:
        return ()
    try:
        strategy = validated_strategy(snapshot)
        if strategy is None or strategy["status"] != "READY":
            return ("UNAVAILABLE",)
        calibration, tire = validated_calibration(snapshot), validated_tire_age(snapshot)
        coverage, seen = [], set()
        for row in rows:
            entry, exit_, count = (row[key] for key in (
                "entry_progress_laps", "exit_progress_laps", "next_stint_laps"))
            if not all(type(n) in (int, float) and 0 <= n <= 2_000_000
                       for n in (entry, exit_, count)):
                return ("INVALID",)
            if (entry, exit_, count) in seen:
                continue
            seen.add((entry, exit_, count))
            covered = _covered_tires(snapshot, {**row, "services": [row]},
                                     strategy, calibration, tire)
            coverage.append((math.floor(exit_), math.ceil(exit_),
                             math.floor(round(entry + count, 9)),
                             covered["status"], covered["reason"]))
        return tuple(coverage)
    except Exception:
        return ("INVALID",)


def _range_text(bounds):
    return f"{math.floor(bounds[0] * 10) / 10:.1f} 至 {math.ceil(bounds[1] * 10) / 10:.1f}"


def _traffic_text(service, *, speech=False):
    if service["status"] != "READY":
        return "交通未能确定"
    parts = []
    for side, name in (("ahead", "前车"), ("behind", "后车")):
        row = service[side]
        if row is None:
            parts.append(name + "未知")
        elif row["gap_range_s"][0] >= 30:
            parts.append(name + "30秒外")
        elif row["gap_range_s"][1] > 30:
            parts.append(name + "秒差范围较宽")
        else:
            low, high = row["gap_range_s"]
            interval = (f"{math.floor(low)}到{math.ceil(high)}" if speech
                        else _range_text([low, high]))
            parts.append(name + interval + "秒")
    return "、".join(parts)


def pit_briefing_voice_facts(value):
    """Separate short requests; never lengthen freshness to fit a monologue."""
    if value["status"] != "CONDITIONAL":
        return []
    action = value["actions"][0]
    tire = action["tires"]
    text = "换胎收益未就绪；请核对校准、胎组和观测范围。"
    if tire["status"] == "CONDITIONAL":
        low, high = tire["covered_gain_range_s"]
        text = (f"只计{tire['complete_laps']}整圈，"
                f"换胎收益约{math.floor(low)}到{math.ceil(high)}秒；片段和整段净收益未知。")
    rows = [("tires_brief", text)]
    for key, mode, name in (("fuel_traffic_brief", "FUEL_ONLY", "仅加油"),
                            ("tire_traffic_brief", "FOUR_TIRES", "换四胎")):
        service = next((row for row in action["services"] if row["service_option"] == mode), None)
        text = (name + "假设：" + _traffic_text(service, speech=True) + "；不是安全保证。"
                if service is not None else name + "交通未知；当前未区分服务方案。")
        rows.append((key, text))
    # Extreme user budgets cannot produce an arbitrarily long spoken number.
    return [(key, text if len(text) <= 55 else "这项条件范围太宽，暂不语音报告数字。")
            for key, text in rows]


def pit_briefing_text(value):
    """Internal fixed renderer: caller passes project_pit_briefing's result."""
    unavailable = {
        "COMPLETE_PIT_ASSUMPTIONS_REQUIRED": "请确认进出站位置与完整停站耗时。",
        "COMPLETE_LAP_MOTION_REQUIRED": "尚缺双方完整圈位置历史。",
        "NO_FUEL_STOP_IN_BUDGET": "当前燃油预算不需要停车。",
        "NO_REACHABLE_PIT_ENTRY_IN_FUEL_WINDOW": "燃油窗口内没有可达的已配置入口。",
        "PIT_PERMISSION_OR_FLAGS_UNRESOLVED": "进站许可或旗号尚未确认。",
        "PROCESSING_ERROR": "综合计算故障，其他功能独立运行。",
    }
    if value["status"] != "CONDITIONAL":
        return "综合进站未就绪：" + unavailable.get(value["reason"], "映射燃油及交通证据不足。"), []
    tire_wait = {
        "CALIBRATION_NOT_READY": "车型、设置及天气匹配的轮胎校准未就绪",
        "TIRE_SERVICE_ASSUMPTIONS_REQUIRED": "缺少可比较的轮胎作业耗时假设",
        "CONFIRMED_TIRE_ORIGIN_REQUIRED": "缺少车手四胎换新确认后的连续起点",
        "NO_COMPLETE_POST_EXIT_LAPS": "出站后预算没有可建模的完整圈",
        "PROJECTED_AGES_OUTSIDE_MODEL_DOMAIN": "未来新旧胎计圈超出观测范围",
        "PROJECTED_FUEL_OUTSIDE_MODEL_DOMAIN": "未来整圈起始油量超出观测范围",
    }
    names = {"FUEL_ONLY": "仅加油", "FOUR_TIRES": "加油并换四胎", "UNSPECIFIED": "未指定服务"}
    details, brief = [], None
    for action in value["actions"]:
        title = (f"入口尚距 {action['distance_to_entry_laps']:.3f} 圈，"
                 f"补油 {action['fuel_add_l']:.2f} 升")
        text = (title + f"；到站 {action['arrival_fuel_l']:.2f} 升，"
                + f"目标 {action['target_fuel_l']:.2f} 升。"
                + f"下段预算 {action['next_stint_laps']:.3f} 圈，"
                + f"其后仍有 {action['further_stops']} 停。")
        for service in action["services"]:
            text += (names[service["service_option"]] + "：完整耗时假设 "
                     + _range_text(service["complete_loss_range_s"]) + " 秒；"
                     + _traffic_text(service) + "。")
        tires = action["tires"]
        tire_brief = "轮胎收益未知"
        if tires["status"] == "CONDITIONAL":
            tire_brief = (f"仅 {tires['complete_laps']} 个完整圈收益 "
                          + _range_text(tires["covered_gain_range_s"]) + " 秒")
            text += (tire_brief + f"；额外换胎 {tires['extra_tire_service_s']:.1f} 秒；"
                     + "已覆盖收益减作业耗时 "
                     + _range_text(tires["covered_gain_minus_service_range_s"]) + " 秒。")
        else:
            text += "轮胎收益未知：" + tire_wait[tires["reason"]] + "。"
        text += f"未建模首尾片段共 {tires['unmodeled_partial_laps']:.3f} 圈；不是整段净收益。"
        details.append((action["endpoint"], text))
        if brief is None:
            cost = (f"，换胎多花{tires['extra_tire_service_s']:.1f}秒"
                    if tires["extra_tire_service_s"] is not None else "，换胎耗时未知")
            brief = (f"仅条件预算：补油约{action['fuel_add_l']:.1f}升" + cost
                     + "；整段收益未知。")
    return brief, details
