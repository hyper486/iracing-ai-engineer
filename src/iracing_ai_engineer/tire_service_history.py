"""Reviewed tire-service labels, never inferred from pit exits or set counters.

Hashes bind an explicitly reviewed input; they do not authenticate the label's
truth. Only a same-capture replay may turn these labels into an age context.
No physical-wear, live-source or control capability is granted here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

HISTORY_VERSION = "reviewed-tire-service-history-v1"
AGE_BASIS = "REVIEWED_FULL_NEW_SET"
TIRE_STINT_CONTEXT_VERSION = "tire-stint-context-v2"
LABEL_KEYS = frozenset({
    "kind", "decision_tick", "laps_completed", "tire_compound", "tire_sets_used",
    "label_receipt_sha256",
})
_HISTORY_KEYS = frozenset({
    "contract_version", "history_sha256", "identity_sha256",
    "source_receipt_sha256", "provenance", "events",
})


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _sha(value: object) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("tire-service SHA-256 is invalid")
    return value


def validate_service_label(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != LABEL_KEYS:
        raise ValueError("tire-service label fields are invalid")
    result = dict(value)
    if type(result["kind"]) is not str or result["kind"] not in {
        "FULL_NEW_SET", "NO_TIRE_CHANGE", "PARTIAL_OR_UNKNOWN",
    }:
        raise ValueError("tire-service label kind is invalid")
    for key in ("decision_tick", "laps_completed", "tire_compound", "tire_sets_used"):
        if type(result[key]) is not int or result[key] < 0:
            raise ValueError(f"tire-service label {key} is invalid")
    _sha(result["label_receipt_sha256"])
    return result


def validate_service_history(
    value: object, *, expected_history_sha256: str,
    expected_identity_sha256: str, expected_source_receipt_sha256: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _HISTORY_KEYS:
        raise ValueError("tire-service history fields are invalid")
    result = dict(value)
    if (result["contract_version"] != HISTORY_VERSION
            or result["provenance"] != "REVIEWED_LABELS_NOT_SDK_SERVICE_CONTENTS"):
        raise ValueError("tire-service history contract or provenance is invalid")
    stored = _sha(result["history_sha256"])
    if stored != _sha(expected_history_sha256):
        raise ValueError("tire-service history differs from independent digest")
    if stored != digest({k: v for k, v in result.items() if k != "history_sha256"}):
        raise ValueError("tire-service history self hash differs")
    for key, expected in (("identity_sha256", expected_identity_sha256),
                          ("source_receipt_sha256", expected_source_receipt_sha256)):
        if result[key] != _sha(expected):
            raise ValueError(f"tire-service history {key} differs")
    events = result["events"]
    if type(events) is not list or not 1 <= len(events) <= 256:
        raise ValueError("tire-service history needs 1 to 256 reviewed events")
    previous_tick = -1
    previous_laps = -1
    labels: set[str] = set()
    clean = []
    for raw in events:
        label = validate_service_label(raw)
        if (label["decision_tick"] <= previous_tick
                or label["laps_completed"] < previous_laps):
            raise ValueError("tire-service labels must be ordered and disjoint")
        receipt = label["label_receipt_sha256"]
        if receipt in labels:
            raise ValueError("tire-service label receipt is reused")
        labels.add(receipt)
        previous_tick = label["decision_tick"]
        previous_laps = label["laps_completed"]
        clean.append(label)
    return {**result, "events": clean}


def validate_context_service_origin(context: Mapping[str, object]) -> None:
    """Additional v2 origin checks shared by both offline strategy consumers."""
    history = context.get("service_history")
    if history is None:
        if context.get("origin_kind") is not None or context.get("availability") == "AVAILABLE":
            raise ValueError("tire age requires reviewed full-new-set service history")
        return
    if not isinstance(history, Mapping):
        raise ValueError("tire-service history is invalid")
    validated = validate_service_history(
        history, expected_history_sha256=history.get("history_sha256"),
        expected_identity_sha256=context.get("identity_sha256"),
        expected_source_receipt_sha256=context.get("source_receipt_sha256"),
    )
    events = validated["events"]
    if any(event["decision_tick"] > context["decision_tick"] for event in events):
        raise ValueError("tire-service history includes an unobserved future label")
    origin_tick = context.get("origin_tick")
    if origin_tick is None:
        return
    matches = [event for event in events if event["decision_tick"] == origin_tick]
    if (len(matches) != 1 or matches[0]["kind"] != "FULL_NEW_SET"
            or context.get("origin_kind") != AGE_BASIS
            or matches[0]["laps_completed"] != context.get("origin_laps_completed")):
        raise ValueError("tire age origin is not a reviewed full-new-set label")
    if context.get("availability") != "AVAILABLE":
        return
    origin = matches[0]
    for event in events:
        if event["decision_tick"] <= origin_tick:
            continue
        if (event["kind"] != "NO_TIRE_CHANGE"
                or event["tire_compound"] != origin["tire_compound"]
                or event["tire_sets_used"] != origin["tire_sets_used"]):
            raise ValueError("tire age crosses an unresolved service or newer installation")
    if (context.get("current_tire_compound") != origin["tire_compound"]
            or context.get("tire_sets_used") != origin["tire_sets_used"]):
        raise ValueError("current tire selection differs from reviewed origin")
