"""Bounded asynchronous, evidence-selecting engineer. No model-authored claims.

The provider chooses a plan of allowlisted fact IDs. Local rendering owns every
number and sentence, including non-optional limitations. This is deliberately
not an unrestricted chatbot or an authority for simulator actions.
"""

from __future__ import annotations

import copy
import math
import os
import queue
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .llm_client import DeepSeekClient, LLMError
from .llm_evidence import build_live_context, load_session_context
from .runtime_clock import monotonic_now

TOPICS = ("fuel", "strategy", "driving", "status")
ANSWER_TTL_S = 30.0
MAX_QUESTION_CHARS = 500


@dataclass(frozen=True)
class EngineerConfig:
    provider: str = "off"
    model: str = "deepseek-flash"
    request_limit: int = 60
    min_interval_s: float = 10.0
    timeout_s: float = 12.0
    session_artifact: Path | None = None

    def __post_init__(self) -> None:
        if self.provider not in ("off", "deepseek"):
            raise ValueError("invalid engineer provider")
        if type(self.request_limit) is not int or not 1 <= self.request_limit <= 500:
            raise ValueError("invalid request limit")
        for value, low, high in (
            (self.min_interval_s, 1, 300), (self.timeout_s, 1, 30)
        ):
            if (
                type(value) not in (int, float) or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise ValueError("invalid engineer timing")
        # Same conservative identifier contract as the transport, even when off.
        if (
            not isinstance(self.model, str) or not 1 <= len(self.model) <= 64
            or not self.model[0].isascii() or not self.model[0].isalnum()
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
                   for c in self.model)
        ):
            raise ValueError("invalid model identifier")


def _topic(question: str) -> str:
    text = question.lower()
    for topic, words in (
        ("driving", ("弯", "刹车", "油门", "路肩", "循迹", "驾驶", "brak", "corner", "throttle")),
        ("strategy", ("策略", "进站", "轮胎", "交通", "回场", "pit", "tire", "tyre", "traffic")),
        ("fuel", ("油", "fuel", "laps", "几圈")),
    ):
        if any(word in text for word in words):
            return topic
    return "status"


def fallback_plan(context: Mapping, question: str) -> dict:
    topic = _topic(question)
    facts = context["facts"]
    prefixes = ("strategy.", "tire.") if topic == "strategy" else (f"{topic}.",)
    selected = [item["id"] for item in facts if item["id"].startswith(prefixes)]
    if topic == "status":
        selected = [item["id"] for item in facts]
    return {"topic": topic, "fact_ids": selected[:6], "notice_ids": []}


def validate_plan(plan: object, context: Mapping) -> dict:
    """Reject extra prose, fabricated values/IDs and malformed provider responses."""
    if not isinstance(plan, dict) or set(plan) != {"topic", "fact_ids", "notice_ids"}:
        raise ValueError("INVALID_PLAN")
    if type(plan["topic"]) is not str or plan["topic"] not in TOPICS:
        raise ValueError("INVALID_PLAN")
    for field, source, limit in (("fact_ids", "facts", 6), ("notice_ids", "notices", 4)):
        values = plan[field]
        allowed = {item["id"] for item in context[source]}
        if (
            not isinstance(values, list) or len(values) > limit
            or any(type(value) is not str for value in values)
        ):
            raise ValueError("INVALID_PLAN")
        if len(set(values)) != len(values) or not set(values) <= allowed:
            raise ValueError("INVALID_PLAN")
    return copy.deepcopy(plan)


def render_plan(plan: Mapping, context: Mapping) -> str:
    """All prose comes from trusted templates, never the model or receipt text."""
    title = {
        "fuel": "燃油解释", "strategy": "策略与轮胎", "driving": "驾驶分析", "status": "工程状态",
    }
    scope = "历史复盘，不代表当前赛况。" if context["scope"] == "historical_session" else (
        "基于提问时快照，不是持续更新的进站指令。"
    )
    by_id = {item["id"]: item["text"] for item in context["facts"]}
    lines = [title[plan["topic"]] + "：" + scope]
    lines.extend(by_id[key] for key in plan["fact_ids"])
    if not plan["fact_ids"]:
        lines.append("这一问题目前没有可引用的有效证据；不能推断具体操作或成绩收益。")
    # Limitations are mandatory even if the model attempts to omit them.
    lines.extend(item["text"] for item in context["notices"])
    return "\n".join(lines)


def _binding(snapshot: Mapping) -> tuple:
    monitor = snapshot.get("monitor") or {}
    telemetry = monitor.get("telemetry") or {}
    return (
        snapshot.get("generation"), snapshot.get("engineer_revision"),
        snapshot.get("session_type"), telemetry.get("session_num"),
        telemetry.get("lap_number"), monitor.get("binding_sha256"),
    )


class EngineerService:
    """One bounded job, one provider client, no SDK calls and no disk chat log."""

    def __init__(
        self,
        snapshot: Callable[[], dict],
        config: EngineerConfig | None = None,
        *,
        client: Any = None,
        clock: Callable[[], float] = monotonic_now,
        environ: Mapping[str, str] | None = None,
        initial_requests_used: int = 0,
    ) -> None:
        if type(initial_requests_used) is not int or not 0 <= initial_requests_used <= 500:
            raise ValueError("invalid initial request count")
        self.config = config or EngineerConfig()
        self._source, self._clock = snapshot, clock
        self._lock = threading.Lock()
        self._jobs: queue.Queue = queue.Queue(maxsize=1)
        self._closed = threading.Event()
        self._token = secrets.token_urlsafe(32)
        self._session = (
            load_session_context(self.config.session_artifact)
            if self.config.session_artifact is not None else None
        )
        env = os.environ if environ is None else environ
        key = env.get("DEEPSEEK_API_KEY", "") if self.config.provider == "deepseek" else ""
        self._client = client
        self._configuration_error = None
        if self._client is None and key.strip():
            try:
                self._client = DeepSeekClient(
                    key.strip(), model=self.config.model,
                    timeout_s=self.config.timeout_s, max_tokens=512,
                )
            except LLMError:
                self._configuration_error = "MODEL_CONFIGURATION_INVALID"
        self._requests = initial_requests_used
        self._last_request = -math.inf
        self._busy = False
        self._answer: dict | None = None
        self._answer_binding: tuple | None = None
        self._answer_at = 0.0
        self._answer_was_valid = False
        self._answer_invalidated = False
        self._error: str | None = self._configuration_error
        self._serial = 0
        self._worker = threading.Thread(target=self._run, name="engineer-llm", daemon=True)
        self._worker.start()

    def authenticates(self, token: str) -> bool:
        return (
            isinstance(token, str) and token.isascii()
            and secrets.compare_digest(self._token, token)
        )

    def _base_status(self) -> str:
        if self.config.provider == "off":
            return "DISABLED"
        if self._client is None:
            return "ERROR" if self._configuration_error else "MISSING_KEY"
        if self._requests >= self.config.request_limit:
            return "BUDGET_EXHAUSTED"
        return "READY"

    def snapshot(self) -> dict:
        current = self._source()
        context = build_live_context(current)
        now = self._clock()
        with self._lock:
            answer = copy.deepcopy(self._answer)
            if answer is not None:
                age = max(0.0, now - self._answer_at)
                stale = answer["scope"] == "live_snapshot" and (
                    self._answer_invalidated or age > ANSWER_TTL_S
                    or _binding(current) != self._answer_binding
                    or (self._answer_was_valid and not self._valid_live(current))
                )
                answer.update(age_s=round(age, 1), stale=stale)
                if stale:
                    self._answer_invalidated = True
                    answer["text"] = "这份回答已撤回：数据过期、圈次或会话状态已变化，请重新提问。"
            retry_after = max(0.0, self.config.min_interval_s - (now - self._last_request))
            status = "BUSY" if self._busy else (
                "RATE_LIMITED" if retry_after else self._base_status()
            )
            return {
                "contract_version": "engineer-llm-service-v1",
                "enabled": self.config.provider == "deepseek",
                "provider": "deepseek", "model": self.config.model,
                "status": status, "requests_used": self._requests,
                "request_limit": self.config.request_limit,
                "min_interval_s": self.config.min_interval_s,
                "retry_after_s": round(retry_after, 1), "csrf_token": self._token,
                "answer": answer, "error": self._error,
                "capabilities": {**context["capabilities"], "session": self._session is not None},
                "advisor_only": True, "executable": False, "live_acceptance": False,
            }

    @staticmethod
    def _valid_live(snapshot: Mapping) -> bool:
        monitor = snapshot.get("monitor") or {}
        return (
            snapshot.get("connection") == "CONNECTED"
            and snapshot.get("source_mode") == "LIVE"
            and monitor.get("source_kind") == "SDK_LIVE"
            and monitor.get("status") in ("READY", "DEGRADED")
            and monitor.get("quality", {}).get("stale") is False
            and monitor.get("context", {}).get("player_control_state") == "IN_CAR_PHYSICS"
            and not monitor.get("interval_invalid_for_fuel")
        )

    def submit(self, question: object, scope: object = "live") -> tuple[int, dict]:
        if (
            type(question) is not str or not 1 <= len(question.strip()) <= MAX_QUESTION_CHARS
            or any(ord(char) < 32 and char not in "\n\t" for char in question)
            or scope not in ("live", "session")
        ):
            return 400, {"error": "INVALID_QUESTION"}
        if scope == "session" and self._session is None:
            return 409, {"error": "NO_SESSION_ARTIFACT"}
        state = self._source()
        context = self._session if scope == "session" else build_live_context(state)
        now = self._clock()
        with self._lock:
            if self._closed.is_set():
                return 409, {"error": "STOPPED"}
            if self._busy:
                return 409, {"error": "BUSY"}
            if now - self._last_request < self.config.min_interval_s:
                return 429, {"error": "RATE_LIMITED"}
            use_cloud = self._base_status() == "READY"
            self._last_request = now
            self._busy = True
            self._answer = None
            self._error = self._configuration_error
            self._serial += 1
            if use_cloud:
                self._requests += 1
            self._jobs.put_nowait((
                self._serial, question.strip(), copy.deepcopy(context), _binding(state),
                now, use_cloud, self._valid_live(state),
            ))
        return 202, {"accepted": True}

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                job = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            serial, question, context, binding, requested_at, use_cloud, was_valid = job
            plan = fallback_plan(context, question)
            origin, error = "local_fallback", None
            if use_cloud:
                try:
                    plan = validate_plan(self._client.complete(context, question), context)
                    origin = "deepseek"
                except Exception:
                    # Do not expose native errors, request text, credentials or provider bodies.
                    error = "MODEL_UNAVAILABLE_OR_INVALID_PLAN"
            answer = {
                "id": str(serial), "topic": plan["topic"], "text": render_plan(plan, context),
                "origin": origin, "scope": context["scope"],
                "snapshot_was_valid": was_valid,
            }
            with self._lock:
                if not self._closed.is_set():
                    self._answer = answer
                    self._answer_binding = binding
                    self._answer_at = requested_at
                    self._answer_was_valid = was_valid
                    self._answer_invalidated = False
                    self._error = error or self._configuration_error
                self._busy = False
            self._jobs.task_done()

    def close(self, *, wait: bool = False) -> None:
        self._closed.set()
        self._worker.join(timeout=None if wait else 0.3)


__all__ = ["EngineerConfig", "EngineerService", "fallback_plan", "render_plan", "validate_plan"]
