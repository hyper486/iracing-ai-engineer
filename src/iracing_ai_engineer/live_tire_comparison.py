"""Conditional next-stint tire benefit, not wear safety or a pit recommendation.

The historical linear-age model is applied only to a driver's explicit new-set
origin and validated whole-lap fuel scenarios. No source labels are upgraded.
"""

from __future__ import annotations

import json
import math

from .live_strategy import _map, _same_typed_tree, validated_strategy
from .live_tire_age import BASIS, validated_tire_age
from .live_tire_calibration import validated_calibration

CONTRACT = "live-conditional-tire-comparison-v1"
LIMITS = ("按车手确认的四胎换新起点和历史线性模型，假设天气、设置、配方与干净驾驶条件不变。"
          "只比较相同油量的下段完整圈预算，不定位进站口，不合并出站交通或赛事规则。"
          "未来圈只核对观测边界，不代表联合条件或新旧胎对照已验证。"
          "区间不是统计置信度；不证明旧胎安全，不生成留胎或进站指令。")


def unavailable_tire_comparison(snapshot, reason="CALIBRATION_NOT_READY"):
    monitor = _map(snapshot.get("monitor"))
    return {"contract_version": CONTRACT, "advisor_only": True, "executable": False,
        "live_acceptance": False, "physical_wear": None, "race_recommendation": None,
        "basis": BASIS, "condition_assumption": "UNCHANGED_CLEAN_DRY_RUNNING",
        "geometry": "WHOLE_LAP_BUDGET_NOT_MAPPED_ENTRY", "event_rules": "UNVERIFIED",
        "binding_sha256": monitor.get("binding_sha256"),
        "monitor_sequence": monitor.get("sequence"),
        "session_time_us": monitor.get("session_time_us"),
        "calibration_revision": _map(snapshot.get("tire_calibration")).get("revision"),
        "configuration_revision": _map(snapshot.get("strategy_configuration")).get("revision"),
        "tire_revision": _map(snapshot.get("tire_age")).get("revision"),
        "model_sha256": None, "status": "WAIT", "reason": reason, "scenarios": []}


def _range(low, high):
    return [math.floor(low * 1e6) / 1e6, math.ceil(high * 1e6) / 1e6]


def project_tire_comparison(snapshot):
    result = unavailable_tire_comparison(snapshot)
    if snapshot.get("source_mode") != "LIVE":
        return result
    calibration = validated_calibration(snapshot)
    if calibration is None:
        return result
    if calibration.model_json is None:
        return {**result, "reason": "PERFORMANCE_MODEL_UNAVAILABLE"}
    result["model_sha256"] = calibration.model_sha256
    tire = validated_tire_age(snapshot)
    if tire is None or tire["origin"] is None:
        return {**result, "reason": "CONFIRMED_TIRE_ORIGIN_REQUIRED"}
    strategy = validated_strategy(snapshot)
    if strategy is None or strategy["status"] != "READY":
        return {**result, "reason": "FUEL_SCENARIOS_UNAVAILABLE"}
    if not strategy["scenarios"]:
        return {**result, "reason": "NO_FUEL_STOP_IN_BUDGET"}
    telemetry = snapshot["monitor"]["telemetry"]
    reasons = snapshot["monitor"]["reasons"]
    if (telemetry.get("pits_open") is not True or "READ_ERROR:PitsOpen" in reasons
            or type(telemetry.get("session_flags")) is not int
            or "READ_ERROR:SessionFlags" in reasons
            or telemetry["session_flags"] & (0x00330000 | 0x0008 | 0x0010 | 0x0100 | 0xC000)):
        return {**result, "reason": "PIT_PERMISSION_OR_FLAGS_UNRESOLVED"}
    model = json.loads(calibration.model_json)
    low, high = model["performance_age_slope_uncertainty_s_per_lap"]
    current_age = tire["counter_increase"]
    age_min, age_max = calibration.age_bounds
    fuel_min, fuel_max = calibration.fuel_bounds
    rows = []
    for endpoint, scenario in zip(("early", "late"), strategy["scenarios"], strict=False):
        laps, after = scenario["laps_from_now"], scenario["next_stint_laps"]
        age_at_pit = current_age + laps
        # Ages are completed-counter differences at future lap endpoints. The
        # new set spans 1..N; a pit exit or race start never supplied current_age.
        old_ages, new_ages = [age_at_pit + 1, age_at_pit + after], [1, after]
        future_fuel = [scenario["target_fuel_l"] - strategy["plan"]["burn_l_per_lap"] * (after - 1),
                       scenario["target_fuel_l"]]
        row = {"endpoint": endpoint, "laps_from_now": laps, "next_stint_laps": after,
            "further_stops": scenario["further_stops"], "current_counter_age": current_age,
            "age_at_pit": age_at_pit, "old_age_range": old_ages, "new_age_range": new_ages,
            "future_lap_start_fuel_range_l": _range(*future_fuel),
            "fuel_add_l": scenario["fuel_add_l"], "status": "WAIT", "reason": None,
            "performance_gain_range_s": None, "extra_tire_service_s": None,
            "net_gain_range_s": None, "balance": None}
        service = next((option for option in scenario["service_options"]
                        if option["service_option"] == "FOUR_TIRES"), None)
        if service is None:
            row["reason"] = "TIRE_SERVICE_ASSUMPTIONS_REQUIRED"
        elif not (age_min <= new_ages[0] <= new_ages[1] <= age_max
                  and age_min <= old_ages[0] <= old_ages[1] <= age_max):
            row["reason"] = "PROJECTED_AGES_OUTSIDE_MODEL_DOMAIN"
        elif not fuel_min - 1e-6 <= future_fuel[0] <= future_fuel[1] <= fuel_max + 1e-6:
            row["reason"] = "PROJECTED_FUEL_OUTSIDE_MODEL_DOMAIN"
        else:
            scale = age_at_pit * after
            cost = service["extra_tire_time_s"]
            net = [low * scale - cost, high * scale - cost]
            balance = ("BENEFIT_EXCEEDS_SERVICE" if net[0] > 1e-6 else
                       "SERVICE_EXCEEDS_BENEFIT" if net[1] < -1e-6 else "UNCERTAIN")
            row.update(status="CONDITIONAL", reason="MODEL_AND_USER_ASSUMPTIONS",
                performance_gain_range_s=_range(low * scale, high * scale),
                extra_tire_service_s=cost, net_gain_range_s=_range(*net), balance=balance)
        rows.append(row)
    available = any(row["status"] == "CONDITIONAL" for row in rows)
    return {**result, "status": "CONDITIONAL" if available else "WAIT",
            "reason": "CONDITIONAL_NEXT_STINT_COMPARISON" if available else "SCENARIOS_UNSUPPORTED",
            "scenarios": rows}


def validated_tire_comparison(snapshot):
    value = snapshot.get("tire_comparison")
    if type(value) is not dict:
        return None
    expected = (unavailable_tire_comparison(snapshot, "PROCESSING_ERROR")
                if value.get("reason") == "PROCESSING_ERROR" else project_tire_comparison(snapshot))
    return expected if _same_typed_tree(value, expected) else None


def tire_comparison_binding(snapshot):
    """Material state, not sub-second fuel drift; answers also have a short TTL."""
    value = _map(snapshot.get("tire_comparison"))
    rows = value.get("scenarios")
    rows = rows if type(rows) is list and len(rows) <= 2 else []
    return (value.get("status"), value.get("reason"), value.get("calibration_revision"),
            value.get("model_sha256"), _map(snapshot.get("car_context")).get("revision"),
            tuple((_map(row).get("status"), _map(row).get("reason"), _map(row).get("balance"),
                   _map(row).get("laps_from_now"), _map(row).get("next_stint_laps"))
                  for row in rows))


def tire_comparison_text(snapshot):
    """Return fixed locally rendered brief/details, including explicit WAITs."""
    value = validated_tire_comparison(snapshot)
    notices = {
        "CALIBRATION_NOT_READY": "校准未载入，或车型、设置、配方、天气与校准不符。",
        "PERFORMANCE_MODEL_UNAVAILABLE": "校准缺少通过留出验证的性能模型。",
        "CONFIRMED_TIRE_ORIGIN_REQUIRED": "尚无四胎换新确认后的连续计圈起点。",
        "FUEL_SCENARIOS_UNAVAILABLE": "有效燃油方案尚未就绪。",
        "NO_FUEL_STOP_IN_BUDGET": "当前燃油预算不需要进站；不额外发明换胎停车。",
        "PIT_PERMISSION_OR_FLAGS_UNRESOLVED": "进站许可或旗号尚不支持这项比较。",
        "TIRE_SERVICE_ASSUMPTIONS_REQUIRED": "请确认加油速率、四胎耗时与串行／并行作业。",
        "PROJECTED_AGES_OUTSIDE_MODEL_DOMAIN": "新胎或旧胎的未来计圈超出模型观测范围。",
        "PROJECTED_FUEL_OUTSIDE_MODEL_DOMAIN": "下段起始油量范围超出校准数据。",
        "PROCESSING_ERROR": "轮胎收益计算故障；其他功能仍独立运行。",
    }
    if value is None:
        return "换胎收益暂无可核对证据。", []
    if value["status"] != "CONDITIONAL":
        reason = value["reason"]
        if reason == "SCENARIOS_UNSUPPORTED" and value["scenarios"]:
            reason = value["scenarios"][0]["reason"]
        return "换胎收益未就绪：" + notices.get(reason, "当前证据不足。"), []
    details, brief = [], None
    def interval(bounds):
        return f"{math.floor(bounds[0] * 10) / 10:.1f} 至 {math.ceil(bounds[1] * 10) / 10:.1f}"
    for row in value["scenarios"]:
        label = f"假设再跑 {row['laps_from_now']} 整圈进站，随后 {row['next_stint_laps']} 圈"
        if row["status"] != "CONDITIONAL":
            details.append((row["endpoint"], label + "：" + notices[row["reason"]]))
            continue
        net = interval(row["net_gain_range_s"])
        details.append((row["endpoint"], label + "：四胎换新的模型时间收益 "
            + interval(row["performance_gain_range_s"]) + " 秒，"
            + f"额外作业 {row['extra_tire_service_s']:.1f} 秒，净收益 {net} 秒。"
            + f"下段之后仍有 {row['further_stops']} 次燃油停车预算。"))
        if brief is None:
            brief = label + f"，换新四胎净收益 {net} 秒。仅条件估计，磨损未知，不证明旧胎安全。"
    return brief, details
