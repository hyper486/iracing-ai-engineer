"""Invented reviewed labels for tests; never authentic SDK/live evidence."""

from iracing_ai_engineer.tire_service_history import AGE_BASIS, HISTORY_VERSION, digest


def service_label(tick=1, laps=0, compound=0, sets=1, kind="FULL_NEW_SET"):
    return {"decision_tick": tick, "laps_completed": laps, "tire_compound": compound,
            "tire_sets_used": sets, "kind": kind,
            "label_receipt_sha256": digest([tick, laps, compound, sets, kind])}


def service_history(events, identity="a" * 64, source="b" * 64):
    material = {"contract_version": HISTORY_VERSION, "identity_sha256": identity,
                "source_receipt_sha256": source,
                "provenance": "REVIEWED_LABELS_NOT_SDK_SERVICE_CONTENTS", "events": events}
    return {**material, "history_sha256": digest(material)}


def tire_context(age=4, compound=0, identity="a" * 64, source="b" * 64,
                 tick=100, origin_tick=1, origin_laps=0, sets=1):
    from iracing_ai_engineer.retrieved_live_analysis import _TIRE_PHYSICAL_WEAR_UNAVAILABLE

    history = service_history([
        service_label(origin_tick, origin_laps, compound, sets),
    ], identity, source)
    material = {"availability": "AVAILABLE", "contract_version": "tire-stint-context-v2",
                "current_laps_completed": origin_laps + age, "current_tire_compound": compound,
                "decision_tick": tick, "identity_sha256": identity, "on_pit_road": False,
                "origin_kind": AGE_BASIS, "origin_laps_completed": origin_laps,
                "origin_tick": origin_tick, "physical_wear": dict(_TIRE_PHYSICAL_WEAR_UNAVAILABLE),
                "reason_codes": [], "source_receipt_sha256": source,
                "status": "AVAILABLE_REVIEWED_TIRE_AGE", "stint_age_completed_laps": age,
                "tire_sets_used": sets, "service_history": history}
    return {**material, "context_sha256": digest(material)}
