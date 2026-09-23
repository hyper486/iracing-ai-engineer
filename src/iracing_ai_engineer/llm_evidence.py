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
            _entry("DRIVING_UNAVAILABLE", "当前入口尚不提供实时弯角或驾驶技巧建议。"),
            _entry("TIRE_UNAVAILABLE", "当前入口尚不提供实时胎耗或换胎建议。"),
        ]
    )
    snapshot = _mapping(snapshot)
    _live_situation_facts(result, snapshot)
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
