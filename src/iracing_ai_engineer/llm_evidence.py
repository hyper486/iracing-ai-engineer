"""Small, locally rendered evidence contexts; never forward source/free text.

This is a projection, not an authenticity or live-advice acceptance mechanism.
Historical receipt validation replays derived components but does not reopen or
authenticate the original telemetry. Nothing here promotes shadow advice.
"""

from __future__ import annotations

import json
import math
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .engineer_session import validate_engineer_session
from .live_driving import driving_notice, validated_driving, validated_pace
from .live_pit_briefing import LIMITS as PIT_BRIEFING_LIMITS
from .live_pit_briefing import pit_briefing_text, pit_briefing_voice_facts, project_pit_briefing
from .live_pit_observation import pit_observation_notice, validated_pit_observation
from .live_rejoin import rejoin_notice, validated_rejoin
from .live_stint import validated_stint
from .live_strategy import strategy_notice, validated_strategy
from .live_tire_age import tire_age_notice, validated_tire_age
from .live_tire_comparison import LIMITS as TIRE_COMPARISON_LIMITS
from .live_tire_comparison import tire_comparison_text, validated_tire_comparison
from .live_traffic import validated_traffic

LLM_CONTEXT_CONTRACT_VERSION = "engineer-llm-context-v1"
MAX_SESSION_BYTES = 32 * 1024 * 1024


class LlmEvidenceError(ValueError):
    """A fixed public-safe error code, without the input path or exception."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object, *, maximum: float = 1_000.0) -> bool:
    # Bound before isfinite so enormous JSON integers cannot overflow float().
    return type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value)


def _integer(value: object, *, maximum: int = 10_000) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _entry(identifier: str, text: str) -> dict[str, str]:
    return {"id": identifier, "text": text}


def _base(scope: str) -> dict[str, Any]:
    return {
        "contract_version": LLM_CONTEXT_CONTRACT_VERSION,
        "scope": scope,
        "facts": [],
        "notices": [],
        "capabilities": dict.fromkeys(("fuel", "strategy", "driving", "tire", "traffic"),
                                      "UNAVAILABLE"),
    }


def _live_frame_ready(snapshot: Mapping) -> bool:
    monitor = _mapping(snapshot.get("monitor"))
    context, quality = _mapping(monitor.get("context")), _mapping(monitor.get("quality"))
    return (
        snapshot.get("contract_version") == "experimental-live-fuel-app-v1"
        and snapshot.get("connection") == "CONNECTED"
        and snapshot.get("source_mode") == "LIVE"
        and _number(snapshot.get("updated_age_s"), maximum=2.0)
        and monitor.get("contract_version") == "live-monitor-v1"
        and monitor.get("record_type") == "live_monitor_snapshot"
        and monitor.get("advisor_only") is True and monitor.get("executable") is False
        and monitor.get("source_kind") == "SDK_LIVE"
        and monitor.get("status") in ("READY", "DEGRADED")
        and context.get("sim_source_mode") == "FULL"
        and context.get("player_control_state") == "IN_CAR_PHYSICS"
        and context.get("conflicts") == []
        and quality.get("status") in ("READY", "DEGRADED") and quality.get("stale") is False
    )


def current_fuel_observation(snapshot: Mapping) -> float | None:
    """A fresh SDK observation does not require a trained fuel-burn model.

    Pit/refuel intervals can invalidate extrapolation without erasing a present
    FuelLevel observation. Read errors, source age and driver context still gate it.
    """
    snapshot = _mapping(snapshot)
    if not _live_frame_ready(snapshot):
        return None
    monitor = _mapping(snapshot.get("monitor"))
    reasons = monitor.get("reasons")
    if type(reasons) is not list or any(type(reason) is not str for reason in reasons):
        return None
    amount = _mapping(monitor.get("telemetry")).get("fuel_level_l")
    if "READ_ERROR:FuelLevel" in reasons or not _number(amount):
        return None
    return float(amount)


def _live_situation_facts(result: dict, snapshot: Mapping) -> None:
    if not _live_frame_ready(snapshot):
        return
    monitor = _mapping(snapshot.get("monitor"))
    telemetry, reasons = _mapping(monitor.get("telemetry")), monitor.get("reasons")
    if type(reasons) is not list or any(type(reason) is not str for reason in reasons):
        return
    permission = telemetry.get("pits_open")
    if type(permission) is bool and "READ_ERROR:PitsOpen" not in reasons:
        result["facts"].append(_entry("pit.permission", "SDK 当前显示允许本人进站。" if permission
                                      else "SDK 当前显示不允许本人进站。"))
    flags = telemetry.get("session_flags")
    if _integer(flags, maximum=2**32 - 1) and "READ_ERROR:SessionFlags" not in reasons:
        if flags & 0x00330000:
            result["facts"].append(_entry("pit.flags",
                                        "存在黑旗、取消资格或维修相关旗号，需先核对游戏提示。"))
        elif flags & (0x0008 | 0x0010 | 0x0100 | 0x4000 | 0x8000):
            result["facts"].append(_entry("pit.flags", "当前有黄旗、红旗或安全车相关旗号。"))
        else:
            result["facts"].append(_entry("pit.flags",
                                        "当前未见上述处罚、维修或黄红旗位；不代表规则已核验。"))
    traffic = validated_traffic(snapshot)
    if traffic is None:
        reason = _mapping(snapshot.get("traffic")).get("reason")
        if reason == "TRAFFIC_PROCESSING_ERROR":
            result["notices"].append(_entry("TRAFFIC_UNAVAILABLE",
                                          "本地交通分析故障，前后车距暂不可用；近车模块独立运行。"))
        elif reason == "BOUND_TRACK_LENGTH_UNAVAILABLE":
            result["notices"].append(_entry("TRAFFIC_UNAVAILABLE",
                                          "尚无与当前帧匹配的赛道长度，暂不能计算前后车距。"))
        return
    result["capabilities"]["traffic"] = "PHYSICAL_OBSERVATION_ONLY"
    if traffic["status"] == "AMBIGUOUS":
        result["facts"].append(_entry("traffic.overlap",
                                    "有车辆纵向位置相距不超过五米，前后关系暂不明确。"))
    elif traffic["eligible_count"] == 0:
        result["facts"].append(_entry("traffic.coverage",
                                    "当前没有可定位的在赛道对手，不代表赛道清空。"))
    else:
        for name, label in (("ahead", "前方"), ("behind", "后方")):
            distance = traffic[name]["distance_mm"] / 1000
            result["facts"].append(_entry(f"traffic.{name}",
                                        f"可用数据中，沿赛道{label}最近车辆约 {distance:.0f} 米。"))
    result["notices"].append(_entry("TRAFFIC_OBSERVATION_ONLY",
                                  "车距只是纵向观测，不是秒差、比赛排名或出站预测，也不表示并排清空。"))


_LIVE_DRIVING_PATTERNS = {
    "LONG_COAST": ("重复观察到松油至刹车之间滑行较长。",
                   "练习假设：缩短松油到刹车的空档，不直接要求推迟刹车。"),
    "LATE_BRAKING_HURTS_EXIT": ("重复观察到较晚刹车与较慢出弯同时出现。",
                                "练习假设：尝试稍早刹车，观察出弯速度是否改善。"),
    "THROTTLE_SECOND_LIFT": ("重复观察到开油后再次收油。",
                             "练习假设：更渐进地开油，观察再次收油是否减少。"),
}


def _live_driving_facts(result: dict, snapshot: Mapping) -> None:
    if not _live_frame_ready(snapshot):
        return
    driving = validated_driving(snapshot)
    if driving is None:
        message = driving_notice(snapshot)
        if message is not None:
            for notice in result["notices"]:
                if notice["id"] == "DRIVING_UNAVAILABLE":
                    notice["text"] = message
        return
    if driving["status"] != "READY":
        count = driving["eligible_laps"]
        message = f"本次有 {count} 个近期可比完整圈，尚无可播报的重复弯道证据。"
        if driving["status"] == "LAP_REJECTED":
            message = "刚完成圈未通过完整性或条件筛选，暂不作驾驶建议。"
        elif driving["status"] == "NO_REPEAT":
            message = "刚完成圈未出现符合阈值的重复失时模式，不强行给建议。"
        result["facts"].append(_entry("driving.learning_progress", message))
        return
    result["notices"][:] = [item for item in result["notices"]
                            if item["id"] != "DRIVING_UNAVAILABLE"]
    result["notices"].append(_entry("DRIVING_OBSERVATION_ONLY",
        "驾驶结论只描述近期同一连续段内、观测燃油与天气相近的圈；胎龄与抓地未证明相同。"
        "练习方向不是因果结论或圈速收益保证；不推断路肩、路线或循迹刹车。"))
    result["capabilities"]["driving"] = "RECENT_LAP_OBSERVATION_ONLY"
    point = driving["point"]
    pattern, practice = _LIVE_DRIVING_PATTERNS[point["diagnosis"]]
    result["facts"].extend([
        _entry("driving.location", f"最近完成第 {driving['completed_laps']} 圈，自动分段 "
               f"{point['corner_id']} 的参考刹车区在起跑线后约 {point['braking_zone_m']:.0f} 米。"),
        _entry("driving.loss", f"{len(point['evidence_laps'])} 圈重复观测，相比实际参考圈 "
               f"{driving['reference_lap']}，该段中位时间损失约 {point['loss_s']:.2f} 秒。"),
        _entry("driving.pattern", pattern), _entry("driving.practice", practice),
    ])


def _fuel_budget_facts(result: dict, fuel: Mapping, amount: float, burn: float) -> None:
    """Optional richer evidence from the live model; no external strings survive."""
    facts = result["facts"]
    reserve = fuel.get("reserve_l")
    if _number(reserve):
        facts.append(_entry("fuel.reserve", f"燃油估计已预留 {reserve:.1f} 升储备。"))
    observed = fuel.get("observed_burn_range_l_per_lap")
    if (type(observed) is list and len(observed) == 2
            and all(_number(value) for value in observed)
            and 0 < observed[0] <= burn <= observed[1]):
        facts.append(_entry("fuel.observed_burn_range",
                            f"有效圈观测耗油范围为 {observed[0]:.2f} 至 {observed[1]:.2f} 升，"
                            "不是未来保证范围。"))
    needed, laps, basis = (fuel.get(name) for name in (
        "fuel_needed_to_finish_l", "race_laps_to_go", "race_horizon_basis",
    ))
    if not (_number(needed, maximum=100_000) and _integer(laps, maximum=100_000)
            and _number(reserve) and basis in (
                "SDK_LAPS_REMAINING", "TIMER_FASTEST_LAP_PLUS_MARGIN")):
        return
    # A stale/mixed model payload cannot turn an arbitrary deficit into advice.
    expected = 0.0 if laps == 0 else burn * laps + reserve
    if not math.isclose(needed, expected, rel_tol=1e-9, abs_tol=1e-6):
        return
    deficit = max(0.0, needed - amount)
    if not (_number(fuel.get("fuel_shortfall_l"), maximum=100_000)
            and math.isclose(fuel["fuel_shortfall_l"], deficit, rel_tol=1e-9, abs_tol=1e-6)):
        return
    if result["capabilities"]["strategy"] == "UNAVAILABLE":
        result["capabilities"]["strategy"] = "FUEL_BUDGET_ONLY"
    facts.append(_entry("fuel.finish_balance",
                        f"按燃油模型，跑到结束累计还缺约 {deficit:.1f} 升。" if deficit > 0
                        else "按燃油模型，当前油量够覆盖预算赛程。"))
    facts.append(_entry("fuel.horizon",
                        f"按 SDK 剩余圈数预算 {laps} 圈。" if basis == "SDK_LAPS_REMAINING"
                        else f"按剩余时间、已观测最快圈和附加圈预算 {laps} 圈，终点圈数仍是估计。"))
    capacity, stops = fuel.get("tank_capacity_l"), fuel.get("minimum_stops")
    if _integer(stops) and ((deficit == 0 and stops == 0) or (
        _number(capacity) and capacity > reserve and amount <= capacity and stops > 0
    )):
        # Independently check this lower bound; it is not a mandatory-stop rule.
        stop_bound = (0.0 if deficit == 0 else
                      max(0.0, burn * laps - max(0.0, amount - reserve)) / (capacity - reserve))
        if not math.isfinite(stop_bound):
            return
        expected_stops = 0 if deficit == 0 else max(1, math.ceil(stop_bound))
        if stops == expected_stops:
            facts.append(_entry("fuel.minimum_stops",
                                f"仅按燃油预算，至少还需 {stops} 次补油；不含赛事强制进站。"))


def _live_strategy_facts(result, snapshot):
    value = validated_strategy(snapshot) if _live_frame_ready(snapshot) else None
    if value is None or value["status"] != "READY":
        for notice in result["notices"]:
            if notice["id"] == "STRATEGY_UNAVAILABLE":
                notice["text"] = strategy_notice(snapshot if _live_frame_ready(snapshot) else
                                                    {"strategy": {"reason": "SOURCE_NOT_READY"}})
        return
    plan = value["plan"]
    result["capabilities"]["strategy"] = "CONDITIONAL_FUEL_COMPARISON"
    result["notices"][:] = [item for item in result["notices"]
                            if item["id"] != "STRATEGY_UNAVAILABLE"]
    result["notices"].append(_entry("STRATEGY_CONDITIONAL",
        "燃油比较本身使用本次连接手填参数及完整圈预算；未定位进站口，不是进站圈指令。"
        "未核验赛事规则或物理胎耗；服务耗时仍是手填假设，不保证最优。"))
    facts = result["facts"]
    window = (f"从提问位置起的完整圈预算：{plan['fuel_stops']} 停，首停可行区间为 "
              f"{plan['earliest_laps_from_now']} 至 {plan['latest_laps_from_now']} 整圈后。"
              if plan["fuel_stops"] else "完整圈燃油预算无需再补油，不代表免除赛事强制进站。")
    facts.append(_entry("strategy.window", window))
    early = value["scenarios"][0] if value["scenarios"] else None
    brief = (f"手填燃油预算：首停 {plan['earliest_laps_from_now']} 到 "
             f"{plan['latest_laps_from_now']} 整圈后，早方案补 {early['fuel_add_l']:.1f} 升。"
             "未定位进站口，非进站指令。" if early is not None else
             "按手填燃油预算无需补油；未核验赛事强制进站。")
    facts.append(_entry("strategy.brief", brief))
    facts.append(_entry("strategy.assumptions",
        f"使用手填有效油箱容量 {plan['tank_capacity_l']:.1f} 升、储备 {plan['reserve_l']:.1f} 升"
        f"及模型耗油 {plan['burn_l_per_lap']:.3f} 升/圈，预算剩余 {plan['remaining_laps']} 圈。"))
    for name, row in zip(("early", "late"), value["scenarios"], strict=False):
        facts.append(_entry(f"strategy.{name}",
            f"若 {row['laps_from_now']} 整圈后补油：届时约 {row['arrival_fuel_l']:.1f} 升，"
            f"补 {row['fuel_add_l']:.1f} 升至 {row['target_fuel_l']:.1f} 升，"
            f"覆盖下一段 {row['next_stint_laps']} 圈；之后还有 {row['further_stops']} 停。"))
        loss = row["total_loss_range_s"]
        if loss is not None:
            facts.append(_entry(f"strategy.{name}_time",
                f"{row['laps_from_now']} 整圈后方案，按手填速率和通道损失估算，"
                f"仅通道与泵油时间损失 {loss[0]:.1f} 至 {loss[1]:.1f} 秒；不含其他停站服务。"))
        options = row["service_options"]
        if len(options) == 2:
            fuel_only, tires = options
            timing = snapshot["strategy_configuration"]["inputs"]["fuel_tire_service_timing"]
            label = "并行" if timing == "PARALLEL" else "串行"
            complete = tires["complete_loss_range_s"]
            limit = (f"含手填其他开销后，两方案完整损失分别为 "
                     f"{fuel_only['complete_loss_range_s'][0]:.1f}–"
                     f"{fuel_only['complete_loss_range_s'][1]:.1f} 秒和 "
                     f"{complete[0]:.1f}–{complete[1]:.1f} 秒。" if complete is not None else
                     "未确认其他开销，不能把此驻站时间当成完整进站损失。")
            facts.append(_entry(f"strategy.{name}_service",
                f"{row['laps_from_now']} 整圈后方案，按手填{label}服务："
                f"仅补油驻站 {fuel_only['stationary_service_time_s']:.1f} 秒；"
                f"补油加四轮换胎驻站 {tires['stationary_service_time_s']:.1f} 秒，"
                f"额外 {tires['extra_tire_time_s']:.1f} 秒。" + limit
                + "不判断轮胎是否安全、是否该换或换后能快多少。"))
            if not any(item["id"] == "strategy.service_brief" for item in facts):
                facts.append(_entry("strategy.service_brief",
                    f"手填{label}服务：早方案补油需 {fuel_only['fuel_time_s']:.1f} 秒，"
                    f"四轮换胎使驻站额外 {tires['extra_tire_time_s']:.1f} 秒。"
                    "只是耗时，不是换胎建议。"))


def _live_rejoin_facts(result, snapshot):
    value = validated_rejoin(snapshot) if _live_frame_ready(snapshot) else None
    if value is None or value["status"] != "READY":
        result["notices"].append(_entry("REJOIN_UNAVAILABLE", rejoin_notice(
            {"rejoin": value} if value is not None else {})))
        return
    inputs = snapshot["strategy_configuration"]["inputs"]
    facts = result["facts"]
    result["notices"].append(_entry("REJOIN_CONDITIONAL",
        "出站推演以手填进出站位置、完整进站损失及历史分段配速为条件，不是实测标定、"
        "置信区间、比赛排名或进站指令。不能保证其他车未来不进站或不改变配速。"))
    cost_assumption = (
        "完整损失逐方案计算为通道净损失，加补油/换胎作业，再加额外且不重叠的其他开销；"
        "仅补油是算术对照，不是安全留胎建议。"
        if inputs["other_service_low_s"] is not None else
        f"含所有停站服务的固定总损失为 {inputs['complete_pit_loss_low_s']:.1f} 至 "
        f"{inputs['complete_pit_loss_high_s']:.1f} 秒，须覆盖所比较的补油量；"
        "此固定范围不区分换胎方案。")
    facts.append(_entry("rejoin.assumptions",
        f"手填进站口位于圈长 {inputs['pit_entry_fraction']:.4f}，"
        f"出站口位于 {inputs['pit_exit_fraction']:.4f}；相对留在赛道行驶，"
        + cost_assumption +
        "使用当前在赛道各车最近两圈的分段时间轨迹；"
        f"未纳入 {snapshot['motion']['excluded_count']} 个站内或无赛道位置槽位，"
        "不能据此宣称出站畅通；未核验赛事规则或物理胎耗。"))

    def short_gap(side, bounds):
        if bounds[0] >= 30:
            return side + "30秒外"
        if bounds[1] > 30:
            return side + "秒差范围较宽"
        return f"{side}{math.floor(bounds[0])}至{math.ceil(bounds[1])}秒"

    for row in value["scenarios"]:
        timing = "早方案" if row["endpoint"] == "early" else "晚方案"
        option = row["service_option"]
        suffix, service_label = {"UNSPECIFIED": ("", ""),
                                 "FUEL_ONLY": ("_fuel", "仅补油对照"),
                                 "FOUR_TIRES": ("_tires", "补油加四轮换胎")}[option]
        name = row["endpoint"] + suffix
        label = f"{timing}{service_label}（约 {row['distance_to_entry_laps']:.2f} 圈后到进站口）"
        if row["status"] != "READY":
            facts.append(_entry(f"rejoin.{name}", label + "：暂不能确定出站邻车关系或距离过远。"))
            continue
        ahead, behind = row["ahead"]["gap_range_s"], row["behind"]["gap_range_s"]
        gaps = (f"出站前车约 {math.floor(ahead[0])} 至 {math.ceil(ahead[1])} 秒，"
                f"后车约 {math.floor(behind[0])} 至 {math.ceil(behind[1])} 秒")
        facts.append(_entry(f"rejoin.{name}", label + "：" + gaps
            + f"；补油预算 {row['fuel_add_l']:.1f} 升，之后还有 {row['further_stops']} 停。"
            + f"完整损失假设 {row['complete_loss_range_s'][0]:.1f} 至 "
            f"{row['complete_loss_range_s'][1]:.1f} 秒。"
            "秒差是沿赛道分段行驶时间，不代表名次。"))
        if not any(item["id"] == "rejoin.brief" for item in facts):
            facts.append(_entry("rejoin.brief", f"手填{timing}{service_label}，"
                + short_gap("前", ahead) + "，" + short_gap("后", behind)
                + ("。不判断留胎，非指令。" if suffix else "。非指令。")))


def _live_pit_observation_facts(result, snapshot):
    value = validated_pit_observation(snapshot) if _live_frame_ready(snapshot) else None
    if value is None or value["status"] != "OBSERVED":
        result["notices"].append(_entry("PIT_OBSERVATION_UNAVAILABLE",
            pit_observation_notice(snapshot if value is not None else {})))
        return
    observation = value["observation"]
    def interval(values):
        # Expand decimal presentation; never narrow edge/sampling uncertainty.
        return f"{math.floor(values[0] * 10) / 10:.1f} 到 {math.ceil(values[1] * 10) / 10:.1f}"
    elapsed = interval(observation["pit_road_elapsed_range_s"])
    facts = result["facts"]
    facts.append(_entry("pit_observation.brief", f"本次站内耗时 {elapsed} 秒，不是未来进站损失。"))
    facts.append(_entry("pit_observation.elapsed",
        f"SDK 进出站边界之间历时 {elapsed} 秒，包含站内停留；不是完整进站损失标定。"))
    loss = observation["net_loss_estimate_range_s"]
    if loss is not None:
        facts.append(_entry("pit_observation.baseline",
            f"减去此前两圈同路段的历史用时后，净损失估计为 {interval(loss)} 秒。"
            "历史圈不保证与本次油重、轮胎、天气或交通匹配。"))
    fuel = observation["observed_net_tank_change_l"]
    if fuel is not None:
        facts.append(_entry("pit_observation.fuel_change",
            f"入站后至出站采样的油箱净变化 {fuel:+.2f} 升；不是加油机实际交付量。"))
    facts.append(_entry("pit_observation.limits",
        "仅当前连续观测段内最近一次完整进站；SDK 边界不保证是真实合流口。"
        "不含边界外减速与加速损失，不推断换胎、维修或下一次服务耗时。"))
    result["notices"].append(_entry("PIT_OBSERVATION_NOT_CALIBRATION",
        "进站观测仅覆盖历史 SDK 进出站边界；不含边界外损失，"
        "未匹配下次油重、轮胎、服务或交通，不是未来完整进站损失标定。"))


def _live_stint_facts(result, snapshot):
    if not _live_frame_ready(snapshot):
        return
    tire_age = validated_tire_age(snapshot)
    if tire_age is not None and tire_age["origin"] is not None:
        result["facts"].append(_entry("tire.driver_confirmed_age", tire_age_notice(snapshot)))
    value = validated_stint(snapshot)
    if value is not None:
        stint = value["stint"]
        label = ("上次观测出站后" if stint["origin_kind"] == "OBSERVED_PIT_EXIT"
                 else "仅连续观察区间")
        pit = "当前在进站通道。" if value["on_pit_road"] else ""
        result["facts"].append(_entry("stint.observed",
            f"{label} {stint['elapsed_s'] / 60:.1f} 分钟，计圈增加 {stint['counter_increase']}。"
            + pit + "中途接入不代表完整 stint，出站也不代表已换胎。"))
        tires = value["tire_observation"]
        if tires is not None:
            result["facts"].append(_entry("tire.observed_context",
                f"轮胎配方与用胎计数未变的连续观察区间为 {tires['elapsed_s'] / 60:.1f} 分钟，"
                f"计圈增加 {tires['counter_increase']}；这不是完整胎龄或磨损读数。"))
            result["capabilities"]["tire"] = "COUNTER_OBSERVATION_ONLY"
    pace = validated_pace(snapshot)
    if pace is not None:
        first, last = pace["laps"][0]["lap"], pace["laps"][-1]["lap"]
        trend = "慢" if pace["delta_s"] >= 0 else "快"
        result["facts"].append(_entry("tire.pace",
            f"第 {last - 2}–{last} 圈相比第 {first}–{first + 2} 圈，"
            f"干净可比圈中位配速{trend} {abs(pace['delta_s']):.2f} 秒，"
            f"起始油量中位变化 {pace['fuel_delta_l']:+.2f} 升；未做油重修正，不能归因为胎耗。"))
        result["capabilities"]["tire"] = "RAW_PACE_OBSERVATION_ONLY"


def _live_tire_comparison_facts(result, snapshot):
    value = validated_tire_comparison(snapshot) if _live_frame_ready(snapshot) else None
    brief, details = tire_comparison_text(snapshot if value is not None else {})
    if value is None or value["status"] != "CONDITIONAL":
        result["notices"].append(_entry("TIRE_COMPARISON_UNAVAILABLE", brief))
        return
    result["facts"].append(_entry("tire.comparison_brief", brief))
    result["facts"].extend(_entry(f"tire.comparison_{endpoint}", text)
                           for endpoint, text in details)
    result["facts"].append(_entry("tire.comparison_limits", TIRE_COMPARISON_LIMITS))
    result["notices"].append(_entry("TIRE_COMPARISON_CONDITIONAL", TIRE_COMPARISON_LIMITS))
    result["capabilities"]["tire"] = "CONDITIONAL_PERFORMANCE_COMPARISON"


def _live_pit_briefing_facts(result, snapshot):
    try:
        value = project_pit_briefing(snapshot if _live_frame_ready(snapshot) else {})
        brief, details = pit_briefing_text(value)
        voice_facts = pit_briefing_voice_facts(value)
    except Exception:
        # Optional combined analysis must not disable any independent lane.
        value = {"status": "WAIT", "reason": "PROCESSING_ERROR"}
        brief, details = pit_briefing_text(value)
        voice_facts = []
    if value["status"] != "CONDITIONAL":
        result["notices"].append(_entry("PIT_BRIEFING_UNAVAILABLE", brief))
        return
    result["facts"].append(_entry("pit_briefing.brief", brief))
    result["facts"].extend(_entry(f"pit_briefing.{key}", text) for key, text in voice_facts)
    result["facts"].extend(_entry(f"pit_briefing.{endpoint}", text)
                           for endpoint, text in details)
    result["facts"].append(_entry("pit_briefing.limits", PIT_BRIEFING_LIMITS))
    result["notices"].append(_entry("PIT_BRIEFING_CONDITIONAL", PIT_BRIEFING_LIMITS))


def build_live_context(snapshot: Mapping[str, object]) -> dict[str, Any]:
    """Project fresh observations and separately admitted fuel-budget estimates.

    This accepts the local application's snapshot, not arbitrary external
    telemetry. Readiness/context must agree before any fuel number is exposed.
    No caller strings, messages, telemetry dictionaries or identifiers survive.
    """
    result = _base("live_snapshot")
    notices = result["notices"]
    notices.extend(
        [
            _entry("ESTIMATE_ONLY", "续航与终点燃油预算是实验性估计，不是进站指令。"),
            _entry("STRATEGY_UNAVAILABLE", "当前入口尚不提供实时进站、交通或出站策略。"),
            _entry("DRIVING_UNAVAILABLE", "当前缺少近期完整可比圈与重复弯道证据，驾驶建议未就绪。"),
            _entry("TIRE_UNAVAILABLE", "当前入口尚不提供实时胎耗或换胎建议。"),
        ]
    )
    snapshot = _mapping(snapshot)
    _live_situation_facts(result, snapshot)
    _live_driving_facts(result, snapshot)
    _live_strategy_facts(result, snapshot)
    _live_rejoin_facts(result, snapshot)
    _live_pit_observation_facts(result, snapshot)
    _live_stint_facts(result, snapshot)
    _live_tire_comparison_facts(result, snapshot)
    _live_pit_briefing_facts(result, snapshot)
    monitor = _mapping(snapshot.get("monitor"))
    fuel = _mapping(snapshot.get("fuel"))
    direct = current_fuel_observation(snapshot)
    if direct is not None:
        result["facts"].append(_entry("fuel.current", f"当前观测剩余燃油：{direct:.2f} 升。"))
        result["capabilities"]["fuel"] = "OBSERVED_ONLY"
    reasons = monitor.get("reasons", [])
    scope_ready = (
        _live_frame_ready(snapshot)
        and type(reasons) is list and "READ_ERROR:FuelLevel" not in reasons
        and monitor.get("interval_invalid_for_fuel") == []
        and fuel.get("estimate_only") is True
        and fuel.get("advisor_only") is True
        and fuel.get("executable") is False
    )
    if not scope_ready:
        notices.append(
            _entry("LIVE_STATE_UNAVAILABLE", "没有新鲜且已确认处于车内驾驶的实时燃油证据。")
            if direct is None else
            _entry("FUEL_ESTIMATE_UNAVAILABLE", "当前油量读数可用；本区间不能据此推算续航。")
        )
        return result

    valid, required = fuel.get("valid_laps"), fuel.get("required_laps")
    counts_valid = _integer(valid) and _integer(required) and required >= 2
    if fuel.get("status") == "LEARNING" and counts_valid and valid < required:
        result["capabilities"]["fuel"] = "LEARNING"
        if direct is None and _number(fuel.get("current_fuel_l")):
            result["facts"].append(_entry(
                "fuel.current", f"当前观测剩余燃油：{fuel['current_fuel_l']:.2f} 升。"))
        result["facts"].append(
            _entry(
                "fuel.learning_progress", f"已采纳 {valid} 个有效完整圈，至少需要 {required} 圈。"
            )
        )
        notices.append(_entry("FUEL_LEARNING", "仍需更多完整有效圈来学习耗油。"))
        return result
    amount = fuel.get("current_fuel_l")
    burn = fuel.get("conservative_burn_l_per_lap")
    laps = fuel.get("estimated_laps_remaining")
    if not (
        fuel.get("status") == "READY"
        and counts_valid
        and valid >= required
        and _number(amount)
        and _number(burn)
        and burn > 0
        and _integer(laps)
        and (direct is None or math.isclose(direct, amount, abs_tol=1e-6))
    ):
        notices.append(_entry("FUEL_UNAVAILABLE", "完整有效的燃油估计暂不可用。"))
        return result

    result["capabilities"]["fuel"] = "ESTIMATE_AVAILABLE"
    if direct is None:
        result["facts"].append(_entry("fuel.current", f"当前观测剩余燃油：{amount:.2f} 升。"))
    result["facts"].extend(
        [
            _entry("fuel.burn_per_lap", f"估计保守耗油：每圈 {burn:.3f} 升。"),
            _entry("fuel.range_laps", f"扣除已配置的储备油量后，估计还能完成 {laps} 整圈。"),
            _entry("fuel.sample_laps", f"燃油模型已积累 {valid} 个完整有效圈。"),
        ]
    )
    # Only an exact, locally bound race session permits the model's finish
    # estimate. Do not expose fuel-add/stops outputs as tactical instructions.
    needed = fuel.get("fuel_needed_to_finish_l")
    if snapshot.get("session_type") == "Race" and _number(needed, maximum=100_000):
        result["facts"].append(
            _entry("fuel.finish_estimate", f"模型估计跑至比赛结束需要 {needed:.2f} 升燃油。")
        )
        _fuel_budget_facts(result, fuel, amount, burn)
    else:
        # Non-race questions can still explain reserve and observed variation,
        # but a practice timer never supplies a finish horizon.
        _fuel_budget_facts(result, {**fuel, "race_horizon_basis": None}, amount, burn)
    return result


_STRATEGY_GATES = {
    "event_rules_identity": (
        "PASS_VERIFIED_OFFICIAL_EXACT_MATCH",
        "STRATEGY_RULES_WITHHELD",
        "历史报告缺少与该赛事精确匹配的已核验规则。",
    ),
    "pit_loss_calibration": (
        "PASS_CALIBRATED",
        "STRATEGY_CALIBRATION_WITHHELD",
        "历史报告缺少匹配条件下的进站损失标定。",
    ),
    "service_labels": (
        "PASS_SERVICE_LABELS",
        "STRATEGY_SERVICE_WITHHELD",
        "历史报告缺少已确认的进站服务标签。",
    ),
    "traffic_data": (
        "PASS_TRAFFIC_DATA",
        "STRATEGY_TRAFFIC_WITHHELD",
        "历史报告缺少与具体进站动作绑定的交通及出站估计。",
    ),
    "pit_open_and_penalty_state": (
        "PASS_PIT_OPEN_AND_PENALTY_STATE",
        "STRATEGY_PIT_STATE_WITHHELD",
        "历史报告未确认维修区开放且无待处理处罚。",
    ),
    "strategy_data": (
        "PASS_COMMON_ONE_STOP_PLAN",
        "STRATEGY_PLAN_WITHHELD",
        "历史报告缺少在各赛程分支下均可行的一停燃油方案。",
    ),
}

_DRIVING_PATTERNS = {
    "LONG_COAST": "重复证据显示：较长滑行与该区段的时间损失相关。",
    "LATE_BRAKING_HURTS_EXIT": ("重复证据显示：较晚刹车与较慢的出弯相关。"),
    "THROTTLE_SECOND_LIFT": ("重复证据显示：开油后再次收油与该区段的时间损失相关。"),
}


def _corner_location(components: Mapping[str, object], card: Mapping[str, object]) -> str | None:
    model = _mapping(_mapping(components.get("driving_replay")).get("model_output"))
    corners, length = model.get("corners"), model.get("track_length_m")
    identifier = card.get("corner_id")
    if (
        type(corners) is not list
        or type(identifier) is not str
        or not (_number(length, maximum=100_000) and length > 0)
    ):
        return None
    matches = [
        (index, _mapping(corner))
        for index, corner in enumerate(corners, start=1)
        if _mapping(corner).get("corner_id") == identifier
    ]
    if len(matches) != 1:
        return None
    ordinal, corner = matches[0]
    start, end = corner.get("brake_start_m"), corner.get("exit_m")
    accounting_start, accounting_end = corner.get("accounting_start_m"), corner.get("carry_end_m")
    if not (
        _integer(ordinal)
        and all(
            _number(value, maximum=length)
            for value in (start, end, accounting_start, accounting_end)
        )
        and accounting_start <= start < end <= accounting_end
    ):
        return None
    return (
        f"对应模型沿赛道检测的第 {ordinal} 个候选弯，参考圈窗口位于距起终点 "
        f"{start:.1f}–{end:.1f} 米；损失统计窗口为 {accounting_start:.1f}–{accounting_end:.1f} 米。"
        "这是模型定位，不是官方弯号或刹车点指令。"
    )


def _historical_context(validated: Mapping[str, object]) -> dict[str, Any]:
    """Internal projection; caller must first validate/replay the whole receipt."""
    result = _base("historical_session")
    facts, notices, capabilities = result["facts"], result["notices"], result["capabilities"]
    notices.extend(
        [
            _entry("NOT_CURRENT_STATE", "这是历史场次分析，不代表当前比赛状态。"),
            _entry(
                "SELF_CONSISTENT_NOT_AUTHENTICATED",
                ("报告结构、证据绑定及派生结果已重算核验；原始遥测真实性未独立认证。"),
            ),
            _entry("SHADOW_ONLY", "历史结论仅用于影子分析，不构成实时执行指令。"),
            _entry("ESTIMATE_ONLY", "模型数值是估计，不保证结果，也不证明因果收益。"),
            _entry("FUEL_UNAVAILABLE", "开发冒烟测试中的燃油输出不作为策略证据展示。"),
            _entry(
                "DRIVING_PROMOTION_WITHHELD",
                (
                    "驾驶结论仅描述练习证据；条件匹配、标准弯角、"
                    "可信人工标签及练习 A/B 验证尚未完成。"
                ),
            ),
            _entry("TRAIL_CURB_UNAVAILABLE", "尚无通过验证的循迹刹车或路肩使用处方。"),
            _entry("PHYSICAL_WEAR_UNAVAILABLE", "轮胎性能模型不等同于实际磨损测量。"),
        ]
    )
    components = _mapping(validated.get("components"))
    strategy = _mapping(components.get("m2_strategy"))
    gates = _mapping(strategy.get("capabilities"))
    for gate, (passed, identifier, text) in _STRATEGY_GATES.items():
        if _mapping(gates.get(gate)).get("status") != passed:
            notices.append(_entry(identifier, text))
    recommendations = strategy.get("recommendations")
    if (
        type(recommendations) is list
        and 0 < len(recommendations) <= 10
        and _mapping(strategy.get("quality_gate")).get("status") == "PASS_SHADOW_CONTRACT"
        and all(
            _mapping(item).get("kind") == "M2_STRATEGY_CANDIDATE"
            and _mapping(item).get("status") == "SHADOW_ONLY"
            and _mapping(item).get("executable") is False
            for item in recommendations
        )
    ):
        capabilities["strategy"] = "HISTORICAL_EVIDENCE"
        facts.append(
            _entry(
                "strategy.candidate",
                ("历史报告中有通过离线门槛的策略候选，但不是当前的进站、加油或换胎指令。"),
            )
        )
        # Render only bounded scalar costs of the already gated historical
        # candidate; never forward its action dict, pit lap or service choices.
        action = _mapping(_mapping(recommendations[0]).get("action"))
        loss = action.get("estimated_total_pit_loss_s")
        service = action.get("estimated_stationary_service_s")
        if _number(loss, maximum=3_600) and _number(service, maximum=3_600) and service <= loss:
            facts.extend(
                [
                    _entry(
                        "strategy.pit_loss_estimate", f"历史候选方案估计总进站损失 {loss:.3f} 秒。"
                    ),
                    _entry("strategy.service_estimate", f"其中估计静止服务时间 {service:.3f} 秒。"),
                ]
            )
    else:
        notices.append(_entry("STRATEGY_UNAVAILABLE", "尚无通过全部门槛的历史策略候选。"))

    cards = _mapping(components.get("corner_cards")).get("cards")
    if type(cards) is list:
        for index, value in enumerate(cards[:3], start=1):
            card = _mapping(value)
            loss = _mapping(card.get("loss_summary"))
            delta = loss.get("median_accounted_window_delta_s")
            support = loss.get("supporting_lap_count")
            if not (
                card.get("kind") == "DRIVING_LOSS_CARD"
                and card.get("claim_level") == "descriptive"
                and card.get("status") == "SHADOW_ONLY"
                and card.get("practice_only") is True
                and card.get("executable") is False
                and _number(delta, maximum=3_600)
                and delta > 0
                and _integer(support)
                and support >= 2
            ):
                continue
            capabilities["driving"] = "HISTORICAL_EVIDENCE"
            facts.append(
                _entry(
                    f"driving.corner_{index}.loss",
                    (
                        f"历史损失排名第 {index} 的候选弯：计时窗口损失中位数 {delta:.3f} 秒，"
                        f"有 {support} 个对比圈支持。"
                    ),
                )
            )
            location = _corner_location(components, card)
            if location is not None:
                facts.append(_entry(f"driving.corner_{index}.location", location))
            diagnosis = card.get("diagnosis")
            if type(diagnosis) is str and diagnosis in _DRIVING_PATTERNS:
                facts.append(
                    _entry(f"driving.corner_{index}.pattern", _DRIVING_PATTERNS[diagnosis])
                )
    if capabilities["driving"] == "UNAVAILABLE":
        notices.append(_entry("DRIVING_UNAVAILABLE", "尚无重复证据支持的历史弯角损失结论。"))

    belief = _mapping(_mapping(strategy.get("tire_strategy")).get("belief"))
    scenario = _mapping(belief.get("scenario"))
    interval = scenario.get("keep_tires_time_loss_range_s")
    service = scenario.get("incremental_tire_service_s")
    if (
        belief.get("estimate_available") is True
        and belief.get("advisor_only") is True
        and type(interval) is list
        and len(interval) == 2
        and all(_number(item, maximum=100_000) for item in interval)
        and interval[0] <= interval[1]
        and _number(service, maximum=3_600)
    ):
        capabilities["tire"] = "HISTORICAL_EVIDENCE"
        facts.extend(
            [
                _entry(
                    "tire.performance_loss",
                    (
                        "在历史模型的特定场景下，继续使用原轮胎的估计性能损失区间为 "
                        f"{interval[0]:.3f} 至 {interval[1]:.3f} 秒。"
                    ),
                ),
                _entry(
                    "tire.incremental_service",
                    (
                        f"历史模型估计额外换胎服务耗时 {service:.3f} 秒；"
                        "不据此直接决定换胎或不换胎。"
                    ),
                ),
            ]
        )
    else:
        notices.append(_entry("TIRE_UNAVAILABLE", "历史轮胎性能估计区间暂不可用。"))
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON constant")


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def load_session_context(path: Path) -> dict[str, Any]:
    """Read one bounded regular receipt, validate/replay it, then redact by construction.

    No raw telemetry is reopened. A matching self-hash is not an independently
    retained trust anchor, and is deliberately never described as authentication.
    Invalid/changed/unreadable inputs fail closed, without leaking their paths.
    """
    try:
        source = Path(path)
        before = source.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or source.is_symlink()
            or getattr(before, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or before.st_size > MAX_SESSION_BYTES
        ):
            raise ValueError("not a bounded regular receipt")
        with source.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if _file_identity(before) != _file_identity(opened):
                raise ValueError("receipt changed")
            raw = handle.read(MAX_SESSION_BYTES + 1)
            after = os.fstat(handle.fileno())
            path_after = source.lstat()
            if len(raw) > MAX_SESSION_BYTES or (
                _file_identity(opened) != _file_identity(after)
                or _file_identity(before) != _file_identity(path_after)
                # Windows stat/fstat can report different ctime semantics. Check
                # ctime changes within each observation API, not across APIs.
                or opened.st_ctime_ns != after.st_ctime_ns
                or before.st_ctime_ns != path_after.st_ctime_ns
            ):
                raise ValueError("receipt changed")
    except OSError:
        raise LlmEvidenceError("SESSION_CONTEXT_UNAVAILABLE") from None
    except (TypeError, ValueError):
        raise LlmEvidenceError("SESSION_CONTEXT_INVALID") from None
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        validated = validate_engineer_session(value)
        return _historical_context(validated)
    except Exception:
        # The existing replay stack includes several validators. Even an
        # unexpected nested shape/error must not escape with source text or a
        # pathname at this external-file trust boundary. Interrupts still pass.
        raise LlmEvidenceError("SESSION_CONTEXT_INVALID") from None
