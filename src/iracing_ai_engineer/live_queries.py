"""Exact routine questions answered locally from allowlisted current evidence.

This is a small intent vocabulary, not keyword routing of arbitrary requests.
Mixed, hypothetical and explanatory questions continue to the grounded planner.
It never calculates a maneuver, changes a pit setting or calls a provider.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

LOCAL_QUERY_INTERVAL_S = 1.0

_QUERIES = {
    "amount": (
        "还有多少油", "还剩多少油", "我还有多少油", "现在还有多少油", "当前还有多少油",
        "剩余油量", "当前油量", "现在油量", "剩余燃油", "当前燃油", "当前燃油状态",
        "how much fuel", "how much fuel is left", "how much fuel do i have", "fuel level",
    ),
    "range": (
        "还能跑几圈", "我还能跑几圈", "还能跑多少圈", "油还能跑几圈", "燃油还能跑几圈",
        "当前燃油还能跑几圈", "现在的油还能跑几圈", "剩下的油还能跑几圈",
        "how many laps of fuel", "how many laps of fuel do i have", "fuel range",
    ),
    "burn": (
        "每圈耗油多少", "一圈耗多少油", "一圈用多少油", "每圈用多少油", "油耗多少",
        "当前油耗", "燃油消耗", "fuel consumption", "fuel per lap",
    ),
    "finish": (
        "油够到终点吗", "油够跑完吗", "燃油够到终点吗", "能不加油跑完吗", "油够跑到结束吗",
        "do i have enough fuel", "do i have enough fuel to finish", "fuel to finish",
    ),
    "add": (
        "还缺多少油", "还需要多少油", "还要加多少油", "需要加多少油", "加多少油",
        "how much fuel do i need", "how much fuel to add",
    ),
    "stops": (
        "还要几停", "还要进站几次", "还需要几次加油", "还要加几次油",
        "how many fuel stops", "how many stops left",
    ),
    "pit": (
        "该进站了吗", "现在该进站吗", "我要进站吗", "什么时候进站", "何时进站",
        "什么时候加油", "when should i pit", "should i pit now", "pit window",
    ),
    "pit_plan": ("比较进站方案", "进站方案", "进站计划", "进站窗口", "这次进站加多少油",
                 "下一次进站加多少油", "pit plan", "pit comparison"),
    "service": ("换胎会多花多久", "换胎多花多久", "比较换胎耗时", "比较进站服务",
                "tire service time", "tyre service time"),
    "rejoin": ("出站预测", "进站后会落在哪", "出站后前后车情况", "预测出站交通",
               "rejoin prediction", "where will i rejoin"),
    "pit_observation": ("这次进站用了多久", "本次进站耗时", "上次进站用了多久",
                        "进站耗时", "last pit duration"),
    "traffic": ("前后车情况", "周围车辆情况", "周围的车在哪里", "附近车辆情况", "交通情况",
                "traffic report", "cars around me"),
    "ahead": ("前车多远", "前车离我多远", "前面车在哪", "前车在哪里", "gap ahead"),
    "behind": ("后车多远", "后车离我多远", "后面车在哪", "后车在哪里", "gap behind"),
    "pit_permission": ("现在允许进站吗", "维修区开放吗", "进站通道开放吗", "are pits open"),
    "driving": ("哪里丢时间", "哪里可以改进", "我哪里可以改进", "我该练什么", "驾驶建议",
                "弯道分析", "driving advice", "where am i losing time"),
    "stint": ("这一段跑了多久", "这段跑了几圈", "当前stint", "本段情况", "stint status"),
    "tire": ("轮胎怎么样", "轮胎状态", "轮胎磨损多少", "该换胎了吗", "这套胎还能跑几圈",
             "tire status", "tyre status", "should i change tires"),
    "pace": ("配速变化", "配速怎么样", "最近配速怎么样", "pace trend"),
}


def _normalized(question: str) -> str:
    return re.sub(r"[\s，,。.!！?？]", "", question.casefold())


_INDEX = {_normalized(phrase): intent for intent, phrases in _QUERIES.items() for phrase in phrases}


def live_query_intent(question: object) -> str | None:
    if type(question) is not str or not 1 <= len(question) <= 100:
        return None
    # Strip a single courtesy prefix, not arbitrary sentence content.
    normalized = _normalized(question)
    for prefix in ("请问", "告诉我", "帮我看看"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return _INDEX.get(normalized)


def _situation_speech(facts, intent, fallback):
    """Short local facts fit a short-lived situation; the full text keeps detail."""
    if intent == "stint" and "stint.observed" in facts:
        fact = facts["stint.observed"]
        observed = re.search(r"([0-9.]+) 分钟，计圈增加 ([0-9]+)", fact)
        if observed:
            label = "观测出站后约" if fact.startswith("上次观测出站后") else "仅观测约"
            pit = "当前在站内。" if "当前在进站通道" in fact else ""
            return (f"{pit}{label} {float(observed[1]):.0f} 分钟，计圈增加 {observed[2]}；"
                    "不是胎龄。")
    if intent in ("tire", "pace"):
        confirmed = re.search(r"计圈增加 ([0-9]+)", facts.get("tire.driver_confirmed_age", ""))
        if intent == "tire" and confirmed:
            return (f"按车手四胎换新确认，出站后计圈增加 {confirmed[1]}；"
                    "不是磨损读数，是否值得换胎仍证据不足。")
        counter = re.search(r"计圈增加 ([0-9]+)", facts.get("tire.observed_context", ""))
        if intent == "tire" and counter:
            return f"计圈增加 {counter[1]}，胎组计数未变；不是胎龄，换胎证据不足。"
        pace = re.search(r"中位配速([慢快]) ([0-9.]+) 秒", facts.get("tire.pace", ""))
        if pace:
            limit = "换胎证据不足" if intent == "tire" else "非胎耗结论"
            return (f"近三圈比前三圈中位配速{pace[1]} {pace[2]} 秒；未修正油重，{limit}。")
        return ("配速比较需六个连续可比圈；目前证据不足。" if intent == "pace"
                else "证据不足，暂不判断胎耗或换胎。")
    if intent == "driving" and "driving.location" in facts:
        # Render only fixed-template fact text; no model-authored prose.
        location = facts["driving.location"].split("的参考刹车区在", 1)[-1].rstrip("。")
        practice = facts["driving.practice"].split("，", 1)[0].rstrip("。")
        loss = re.search(r"中位时间损失约 ([0-9.]+) 秒", facts.get("driving.loss", ""))
        delta = f"观测该段慢 {loss[1]} 秒。" if loss else ""
        pattern = facts["driving.pattern"].replace("重复观察到", "反复")
        return location + "附近，" + delta + pattern + practice + "。不保证提速。"
    if intent in ("traffic", "ahead", "behind"):
        if "traffic.overlap" in facts:
            return "车辆纵向相距不超过五米，前后关系暂不明确。"
        if "traffic.coverage" in facts:
            return "当前无可定位对手，不代表赛道清空。"
        names = ("ahead", "behind") if intent == "traffic" else (intent,)
        parts = [facts[f"traffic.{name}"].replace("可用数据中，沿赛道", "")
                 .replace("最近车辆约", "约").removesuffix("。")
                 for name in names if f"traffic.{name}" in facts]
        if parts:
            # Only shorten this locally rendered numeric template. Approximate
            # kilometres keep long distances speakable; full text retains metres.
            def kilometres(match):
                metres = int(match[1])
                return f"{metres / 1000:.1f} 公里" if metres >= 1000 else match[0]
            brief = re.sub(r"([0-9]+) 米", kilometres, "，".join(parts))
            return "提问时，" + brief + "。不是秒差。"
    if intent in ("pit", "pit_permission"):
        permission = facts.get("pit.permission", "进站许可未确认。")
        parts = [permission.replace("SDK 当前显示", "")]
        flags = facts.get("pit.flags", "")
        if "存在黑旗" in flags:
            parts.append("处罚或维修旗号，需核对。")
        elif "黄旗、红旗" in flags:
            parts.append("有黄红旗或安全车旗号。")
        elif intent == "pit" and "不允许" not in permission:
            estimate = facts.get("fuel.range_laps", "续航未就绪。")
            parts.append(estimate.replace("扣除已配置的储备油量后，", ""))
        parts.append("进站时机仍待判断。")
        return "".join(parts)
    return fallback


def render_live_query(context: Mapping, intent: str) -> dict:
    """Use trusted fact templates; never source messages or question text."""
    if intent not in _QUERIES or context.get("scope") != "live_snapshot":
        raise ValueError("INVALID_LIVE_QUERY")
    facts = {item["id"]: item["text"] for item in context["facts"]}
    if intent == "service":
        chosen = [key for key in ("strategy.service_brief", "strategy.early_service",
                                  "strategy.late_service") if key in facts]
        spoken = facts.get("strategy.service_brief",
            "服务比较未就绪；需有效比赛燃油方案，并确认加油速率、四轮换胎耗时及并行或串行。")
        body = "\n".join(facts[key] for key in chosen if key != "strategy.service_brief") or spoken
        return {"topic": "strategy", "fact_ids": chosen, "spoken_text": spoken,
                "text": body + "\n这是服务时间假设；不会操作车辆或进站设置。", "intent": intent}
    if intent == "pit_observation":
        notices = {item["id"]: item["text"] for item in context.get("notices", [])}
        chosen = [key for key in ("pit_observation.brief", "pit_observation.elapsed",
                                  "pit_observation.baseline", "pit_observation.fuel_change",
                                  "pit_observation.limits") if key in facts]
        spoken = facts.get("pit_observation.brief", notices.get(
            "PIT_OBSERVATION_UNAVAILABLE", "进站观测尚未就绪。"))
        body = "\n".join(facts[key] for key in chosen if key != "pit_observation.brief") or spoken
        return {"topic": "strategy", "fact_ids": chosen, "spoken_text": spoken,
                "text": body + "\n这是历史观测；不会操作车辆或进站设置。", "intent": intent}
    if intent == "rejoin":
        notices = {item["id"]: item["text"] for item in context.get("notices", [])}
        chosen = [key for key in ("rejoin.brief", "rejoin.early", "rejoin.late",
                                  "rejoin.early_fuel", "rejoin.early_tires",
                                  "rejoin.late_fuel", "rejoin.late_tires",
                                  "rejoin.assumptions") if key in facts]
        spoken = facts.get("rejoin.brief", notices.get("REJOIN_UNAVAILABLE", "出站预测未就绪。"))
        body = "\n".join(facts[key] for key in chosen if key != "rejoin.brief") or spoken
        if "REJOIN_CONDITIONAL" in notices:
            body += "\n" + notices["REJOIN_CONDITIONAL"]
        if len(spoken) > 280:
            raise ValueError("LIVE_QUERY_RENDER_LIMIT")
        return {"topic": "strategy", "fact_ids": chosen, "spoken_text": spoken,
                "text": body + "\n这是提问时的条件推演；不会操作车辆或进站设置。",
                "intent": intent}
    if intent == "pit_plan" or (intent == "pit" and "strategy.window" in facts):
        return _render_pit_comparison(context, facts, intent)
    preferences = {
        "amount": ("fuel.current",),
        "range": ("fuel.range_laps", "fuel.reserve"),
        "burn": ("fuel.burn_per_lap", "fuel.sample_laps", "fuel.observed_burn_range"),
        "finish": ("fuel.finish_balance", "fuel.horizon"),
        "add": ("fuel.finish_balance", "fuel.horizon"),
        "stops": ("strategy.window",) if "strategy.window" in facts else ("fuel.minimum_stops",),
        "pit": ("pit.permission", "pit.flags", "fuel.finish_balance", "fuel.range_laps",
                "traffic.ahead", "traffic.behind", "traffic.overlap"),
        "traffic": ("traffic.ahead", "traffic.behind", "traffic.overlap", "traffic.coverage"),
        "ahead": ("traffic.ahead", "traffic.overlap", "traffic.coverage"),
        "behind": ("traffic.behind", "traffic.overlap", "traffic.coverage"),
        "pit_permission": ("pit.permission", "pit.flags"),
        "driving": ("driving.location", "driving.loss", "driving.pattern", "driving.practice"),
        "stint": ("stint.observed",),
        "tire": ("tire.driver_confirmed_age", "tire.observed_context", "tire.pace"),
        "pace": ("tire.pace",),
    }
    chosen = [key for key in preferences[intent] if key in facts]
    if intent == "tire" and "tire.driver_confirmed_age" in chosen:
        chosen = [key for key in chosen if key != "tire.observed_context"]
    # A reserve alone is not an answer about range; it is a configuration value.
    if chosen == ["fuel.reserve"]:
        chosen = []
    fallback = None
    if not chosen:
        if intent in ("stint", "tire", "pace"):
            fallback = ("当前本段观测未就绪。" if intent == "stint" else
                        "当前缺少轮胎连续观测或六个连续干净可比圈，配速比较尚未就绪。")
        elif intent == "driving":
            if "driving.learning_progress" in facts:
                chosen = ["driving.learning_progress"]
            else:
                notices = {item["id"]: item["text"] for item in context.get("notices", [])}
                fallback = notices.get("DRIVING_UNAVAILABLE", "当前驾驶证据不足，暂不作建议。")
        elif intent in ("traffic", "ahead", "behind"):
            notices = {item["id"]: item["text"] for item in context.get("notices", [])}
            fallback = notices.get("TRAFFIC_UNAVAILABLE",
                                   "前后车距暂不可用；需新鲜的本人位置、对手数组及匹配赛道长度。")
        elif intent == "pit_permission":
            fallback = "当前没有有效的进站许可或旗号数据，请核对游戏提示。"
        elif "fuel.learning_progress" in facts and intent != "amount":
            chosen = [key for key in ("fuel.current", "fuel.learning_progress") if key in facts]
            fallback = "耗油仍在学习，暂不估计续航或进站。"
        elif intent in ("finish", "add", "stops", "pit") and "fuel.current" in facts:
            chosen = ["fuel.current"]
            fallback = ("缺少已确认的油箱容量，不能推算最少补油次数。" if intent == "stops"
                        and "fuel.finish_balance" in facts else
                        "当前没有完整的比赛终点燃油预算，不能给出加油或进站数量。")
        elif "fuel.current" in facts:
            chosen = ["fuel.current"]
            fallback = "当前耗油模型不可用，不能据此推算续航。"
        else:
            fallback = "没有新鲜且已确认属于本人驾驶的燃油数据。"
    body = "".join(facts[key] for key in chosen)
    if fallback:
        body += fallback
    elif intent == "driving" and "driving.location" in facts:
        body += "这是观测与练习假设，不保证提速，也不能推断路肩或路线。"
    elif intent == "range":
        body += "这是耗油估计，不是进站指令。"
    elif intent in ("finish", "add"):
        body += "这是到终点的累计燃油预算，不是本次加油设置；未计入赛事额外要求。"
    elif intent == "pit":
        if "fuel.range_laps" not in facts:
            body += "当前燃油续航也未就绪。"
        body += "仍不能决定最佳进站圈；缺少匹配的进站损失、赛事规则与出站交通预测。"
        if any(key.startswith("traffic.") for key in chosen):
            body += "上述车距不是出站后的车距。"
    elif intent in ("traffic", "ahead", "behind"):
        body = "提问时，" + body + "这是物理车距，不是秒差、比赛排名或出站预测。"
    elif intent == "pit_permission":
        if "pit.permission" not in facts:
            body += "当前进站许可尚未确认。"
        body += "这是现场状态，不代表现在进站最优，也不代替赛事规则。"
    elif intent == "stops":
        body += ("仅手填参数下的完整圈预算，未定位进站口，不含赛事强制进站。"
                 if "strategy.window" in chosen else "") + "不代表应当现在进站。"
    if intent in ("tire", "pace"):
        body += "不能据此估计轮胎剩余寿命或决定换胎；还需匹配的轮胎表现与服务证据。"
    if len(body) > 280:
        raise ValueError("LIVE_QUERY_RENDER_LIMIT")
    spoken = _situation_speech(facts, intent, body)
    return {
        "topic": "driving" if intent == "driving" else "strategy" if intent in (
            "pit", "stops", "add", "pit_permission", "traffic", "ahead", "behind",
            "stint", "tire", "pace") else "fuel",
        "fact_ids": chosen, "spoken_text": spoken,
        "text": body + "\n这是提问时的证据解读；不会操作车辆或进站设置。",
        "intent": intent,
    }


def _render_pit_comparison(context, facts, intent):
    notices = {item["id"]: item["text"] for item in context.get("notices", [])}
    if "strategy.window" not in facts:
        body = notices.get("STRATEGY_UNAVAILABLE", "进站比较未就绪，请核对本地策略设置。")
        chosen, spoken = [], body
    else:
        chosen = [key for key in (
            "pit.permission", "pit.flags", "strategy.brief", "strategy.window",
            "strategy.early", "strategy.late",
            "strategy.early_time", "strategy.late_time", "strategy.assumptions", "fuel.horizon",
            "strategy.early_service", "strategy.late_service",
        ) if key in facts]
        body = "\n".join(facts[key] for key in chosen if key != "strategy.brief")
        # Detailed hypothetical fills stay visible; the VR answer reports the
        # budget window and limitations in one short utterance, never a command.
        spoken = facts["strategy.brief"]
        permission = facts.get("pit.permission", "")
        if "不允许" in permission:
            spoken = "当前不允许进站。" + spoken
        elif not permission:
            spoken = "进站许可未确认。" + spoken
        body += "\n" + notices["STRATEGY_CONDITIONAL"]
        if "存在黑旗" in facts.get("pit.flags", ""):
            spoken = "有处罚或维修旗号，请核对。" + spoken
        elif "黄旗、红旗" in facts.get("pit.flags", ""):
            spoken = "有黄红旗或安全车旗号，请核对。" + spoken
    if len(spoken) > 280:
        raise ValueError("LIVE_QUERY_RENDER_LIMIT")
    return {"topic": "strategy", "fact_ids": chosen, "spoken_text": spoken,
            "text": body + "\n这是提问时的证据解读；不会操作车辆或进站设置。",
            "intent": intent}
