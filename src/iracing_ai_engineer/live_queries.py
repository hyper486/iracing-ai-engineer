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


def render_live_query(context: Mapping, intent: str) -> dict:
    """Use trusted fact templates; never source messages or question text."""
    if intent not in _QUERIES or context.get("scope") != "live_snapshot":
        raise ValueError("INVALID_LIVE_QUERY")
    facts = {item["id"]: item["text"] for item in context["facts"]}
    preferences = {
        "amount": ("fuel.current",),
        "range": ("fuel.range_laps", "fuel.reserve"),
        "burn": ("fuel.burn_per_lap", "fuel.sample_laps", "fuel.observed_burn_range"),
        "finish": ("fuel.finish_balance", "fuel.horizon"),
        "add": ("fuel.finish_balance", "fuel.horizon"),
        "stops": ("fuel.minimum_stops",),
        "pit": ("fuel.finish_balance", "fuel.range_laps", "fuel.reserve"),
    }
    chosen = [key for key in preferences[intent] if key in facts]
    # A reserve alone is not an answer about range; it is a configuration value.
    if chosen == ["fuel.reserve"]:
        chosen = []
    fallback = None
    if not chosen:
        if "fuel.learning_progress" in facts and intent != "amount":
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
    elif intent == "range":
        body += "这是耗油估计，不是进站指令。"
    elif intent in ("finish", "add"):
        body += "这是到终点的累计燃油预算，不是本次加油设置；未计入赛事额外要求。"
    elif intent == "pit":
        body += "仅凭油量不能决定最佳进站圈，还缺交通、进站损失和赛事规则证据。"
    elif intent == "stops":
        body += "不代表应当现在进站。"
    if len(body) > 280:
        raise ValueError("LIVE_QUERY_RENDER_LIMIT")
    return {
        "topic": "strategy" if intent in ("pit", "stops", "add") else "fuel",
        "fact_ids": chosen, "spoken_text": body,
        "text": body + "\n这是提问时的证据解读；不会操作车辆或进站设置。",
        "intent": intent,
    }
