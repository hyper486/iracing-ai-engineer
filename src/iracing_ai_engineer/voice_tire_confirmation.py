"""Exact local, two-utterance driver assertions. No simulator commands or LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .live_tire_age import TireConfirmation, confirmation_binding, validated_tire_age

REVIEW_SECONDS = 30.0
ACK_SECONDS = 2.0
LABELS = {"FULL_NEW_SET": "四条轮胎已经全部换新", "NO_TIRE_CHANGE": "本次没有换胎",
          "PARTIAL_OR_UNKNOWN": "部分换胎或换胎情况不确定"}
_PHRASES = {
    "记录四胎已换新": "FULL_NEW_SET", "记录四条轮胎已换新": "FULL_NEW_SET",
    "记录四条轮胎已经换新": "FULL_NEW_SET", "记录本次未换胎": "NO_TIRE_CHANGE",
    "记录四条轮胎全部换新": "FULL_NEW_SET",
    "记录本次没有换胎": "NO_TIRE_CHANGE", "记录换胎情况不确定": "PARTIAL_OR_UNKNOWN",
    "记录部分换胎": "PARTIAL_OR_UNKNOWN", "确认记录": "CONFIRM", "取消记录": "CANCEL",
}


def tire_voice_intent(text):
    if type(text) is not str or not 1 <= len(text) <= 100:
        return None
    # Do not strip question marks, negatives, qualifiers or arbitrary prefixes.
    return _PHRASES.get(re.sub(r"[\s，,。.!！]", "", text))


def voice_tire_binding(snapshot):
    if snapshot.get("lifecycle") != "RUNNING":
        return None
    return confirmation_binding(snapshot.get("telemetry") or {})


@dataclass(frozen=True)
class TireVoiceReview:
    kind: str
    binding: tuple
    expires_at: float
    armed: bool = False

    def current(self, snapshot, now):
        return now < self.expires_at and voice_tire_binding(snapshot) == self.binding


def tire_voice_acknowledged(snapshot, ticket):
    """Require the analysis owner's matching receipt, not its mailbox status."""
    value = snapshot.get("telemetry") or {}
    if (snapshot.get("lifecycle") != "RUNNING" or value.get("connection") != "CONNECTED"
            or value.get("source_mode") != "LIVE" or type(ticket) is not dict
            or set(ticket) != {"generation", "binding_sha256", "command"}
            or type(value.get("generation")) is not int
            or type(ticket.get("generation")) is not int
            or value.get("generation") != ticket.get("generation")
            or value.get("tire_confirmation_status") != "APPLIED"):
        return False
    age = value.get("updated_age_s")
    if type(age) not in (int, float) or not 0 <= age <= .75:
        return False
    observed = validated_tire_age(value)
    if observed is None or observed["binding_sha256"] != ticket.get("binding_sha256"):
        return False
    receipt, command = observed["confirmation"], ticket.get("command")
    try:
        if type(command) is not dict:
            return False
        TireConfirmation(**command)
    except (TypeError, ValueError):
        return False
    return (type(receipt) is dict
            and all(type(receipt.get(k)) is type(v) and receipt[k] == v
                    for k, v in command.items()))
