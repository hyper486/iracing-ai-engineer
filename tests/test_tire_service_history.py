"""Synthetic tire-service source/age regressions; not hardware acceptance."""

from __future__ import annotations

import copy

import pytest
from tire_service_fixtures import service_history, service_label, tire_context

from iracing_ai_engineer import retrieved_live_analysis as analysis
from iracing_ai_engineer.telemetry import SourceKind, normalize_sdk_frame
from iracing_ai_engineer.tire_service_history import (
    AGE_BASIS,
    digest,
    validate_context_service_origin,
    validate_service_history,
)


def _frame(tick, laps=0, pit=False, sets=1, compound=0):
    return {"SessionTick": tick, "SessionNum": 1, "SessionTime": tick / 60,
            "LapCompleted": laps, "OnPitRoad": pit, "TireSetsUsed": sets,
            "PlayerTireCompound": compound}


def _context(frames, events=None):
    history = service_history(events) if events else None
    tracker = analysis._new_tire_stint_tracker(history)
    previous = None
    for frame in frames:
        sample = normalize_sdk_frame(frame, source_id="invented-tire-service-test",
                                     source_kind=SourceKind.REPLAY_SDK_PROXY, previous=previous)
        analysis._track_tire_stint_sample(tracker, sample)
        previous = sample
    return analysis._build_tire_stint_context(
        tracker, identity_sha256="a" * 64, source_receipt_sha256="b" * 64,
        expected_decision_tick=frames[-1]["SessionTick"])


def _base():
    return [_frame(0, pit=True), _frame(1), _frame(2, 1), _frame(3, 2)]


def test_only_reviewed_captured_full_set_origin_produces_age():
    frames = _base()
    unlabeled = _context(frames)
    assert unlabeled["stint_age_completed_laps"] is None
    assert unlabeled["origin_kind"] is None
    result = _context(frames, [service_label()])
    assert result["status"] == "AVAILABLE_REVIEWED_TIRE_AGE"
    assert result["stint_age_completed_laps"] == 2
    assert result["origin_kind"] == AGE_BASIS
    assert result["physical_wear"]["estimate_available"] is False


def test_reviewed_fuel_only_stop_preserves_tire_age_not_stint_age():
    frames = _base() + [_frame(4, 2, True), _frame(5, 2), _frame(6, 3)]
    result = _context(frames, [service_label(), service_label(5, 2, kind="NO_TIRE_CHANGE")])
    assert result["stint_age_completed_laps"] == 3
    assert result["origin_tick"] == 1


@pytest.mark.parametrize("kind", [None, "PARTIAL_OR_UNKNOWN"])
def test_unreviewed_or_partial_stop_revokes_age_even_with_unchanged_set_counter(kind):
    frames = _base() + [_frame(4, 2, True), _frame(5, 2), _frame(6, 3)]
    events = [service_label()]
    if kind:
        events.append(service_label(5, 2, kind=kind))
    result = _context(frames, events)
    assert result["availability"] == "UNAVAILABLE"
    assert result["stint_age_completed_laps"] is None


def test_later_reviewed_full_set_replaces_origin_without_demanding_counter_increment():
    # Set-allocation counters are corroboration, never the source of new-set truth.
    frames = _base() + [_frame(4, 2, True), _frame(5, 2), _frame(6, 3)]
    result = _context(frames, [service_label(), service_label(5, 2)])
    assert result["stint_age_completed_laps"] == 1
    assert result["origin_tick"] == 5


def test_no_change_label_cannot_create_an_origin():
    result = _context(_base(), [service_label(kind="NO_TIRE_CHANGE")])
    assert result["stint_age_completed_laps"] is None


@pytest.mark.parametrize("key,value", [
    ("decision_tick", 2), ("laps_completed", 1), ("tire_compound", 1), ("tire_sets_used", 2),
])
def test_label_must_match_a_real_captured_exit_and_its_fields(key, value):
    label = service_label()
    label[key] = value
    result = _context(_base(), [label])
    assert result["availability"] == "INVALID"
    assert result["stint_age_completed_laps"] is None


def test_first_frame_label_cannot_claim_an_unseen_exit():
    result = _context(_base()[1:], [service_label()])
    assert result["availability"] == "INVALID"


@pytest.mark.parametrize("key", ["SessionTick", "LapCompleted", "OnPitRoad",
                                 "PlayerTireCompound", "TireSetsUsed"])
def test_missing_channel_revokes_age_across_resume(key):
    frames = _base()
    del frames[2][key]
    result = _context(frames, [service_label()])
    assert result["stint_age_completed_laps"] is None


@pytest.mark.parametrize("fault", [
    "dropped_tick", "session", "compound", "counter", "lap_regression",
])
def test_continuity_and_selection_changes_prevent_stale_age(fault):
    frames = _base()
    if fault == "dropped_tick":
        frames[-1] = _frame(5, 2)
    elif fault == "session":
        frames[-1]["SessionNum"] = 2
    elif fault == "compound":
        frames[-1]["PlayerTireCompound"] = 1
    elif fault == "counter":
        frames[-1]["TireSetsUsed"] = 2
    else:
        frames[-1]["LapCompleted"] = 0
    result = _context(frames, [service_label()])
    assert result["stint_age_completed_laps"] is None


def test_new_reviewed_installation_can_recover_after_channel_gap():
    frames = _base() + [_frame(4, 2, True), _frame(5, 2), _frame(6, 3)]
    del frames[2]["TireSetsUsed"]
    result = _context(frames, [service_label(), service_label(5, 2)])
    assert result["stint_age_completed_laps"] == 1


@pytest.mark.parametrize("pin", ["expected_history_sha256", "expected_identity_sha256",
                                 "expected_source_receipt_sha256"])
def test_reviewed_labels_remain_independently_pinned(pin):
    history = service_history([service_label()])
    kwargs = {"expected_history_sha256": history["history_sha256"],
              "expected_identity_sha256": "a" * 64, "expected_source_receipt_sha256": "b" * 64}
    kwargs[pin] = "0" * 64
    with pytest.raises(ValueError, match="differs"):
        validate_service_history(history, **kwargs)


@pytest.mark.parametrize("mutation", [
    "counter", "partial", "newer_set", "source", "origin", "future",
])
def test_context_cannot_rehash_a_history_into_an_incompatible_age(mutation):
    context = tire_context()
    history = context["service_history"]
    if mutation == "counter":
        context["tire_sets_used"] = 2
    elif mutation in {"partial", "newer_set"}:
        history["events"].append(service_label(2, kind=(
            "PARTIAL_OR_UNKNOWN" if mutation == "partial" else "FULL_NEW_SET")))
    elif mutation == "source":
        history["source_receipt_sha256"] = "c" * 64
    elif mutation == "origin":
        context["origin_kind"] = "OBSERVED_PIT_EXIT"
    else:
        history["events"].append(service_label(101, kind="NO_TIRE_CHANGE"))
    history["history_sha256"] = digest({k: v for k, v in history.items() if k != "history_sha256"})
    with pytest.raises(ValueError):
        validate_context_service_origin(context)


@pytest.mark.parametrize("mutation", [
    "empty", "duplicate_tick", "duplicate_label", "bool_laps", "kind",
])
def test_service_history_rejects_bad_labels_even_after_rehash(mutation):
    history = service_history([service_label()])
    if mutation == "empty":
        history["events"] = []
    elif mutation in {"duplicate_tick", "duplicate_label"}:
        event = copy.deepcopy(history["events"][0])
        if mutation == "duplicate_label":
            event["decision_tick"] = 2
        history["events"].append(event)
    elif mutation == "bool_laps":
        history["events"][0]["laps_completed"] = True
    else:
        history["events"][0]["kind"] = "TIRES_REQUESTED"
    history["history_sha256"] = digest({k: v for k, v in history.items() if k != "history_sha256"})
    with pytest.raises(ValueError):
        validate_service_history(history, expected_history_sha256=history["history_sha256"],
                                 expected_identity_sha256="a" * 64,
                                 expected_source_receipt_sha256="b" * 64)


def test_legacy_pit_origin_context_is_not_admitted_by_either_validator():
    from iracing_ai_engineer import m2_strategy

    context = tire_context()
    context["origin_kind"] = "OBSERVED_PIT_EXIT"
    context["context_sha256"] = digest({k: v for k, v in context.items() if k != "context_sha256"})
    with pytest.raises(analysis.RetrievedLiveAnalysisError):
        analysis.validate_tire_stint_context(
            context, expected_context_sha256=context["context_sha256"],
            expected_identity_sha256="a" * 64, expected_source_receipt_sha256="b" * 64,
            expected_decision_tick=100)
    with pytest.raises(m2_strategy.M2StrategyReceiptError):
        m2_strategy._validate_tire_stint_context(
            context, identity_sha256="a" * 64, decision_tick=100,
            expected_source_receipt_sha256="b" * 64)


def test_persistent_invalid_sequence_has_bounded_reason_storage():
    tracker = analysis._new_tire_stint_tracker(service_history([service_label()]))
    previous = None
    for tick in range(2000):
        sample = normalize_sdk_frame(
            _frame(tick, pit=tick == 0, sets=1 if tick < 2 else 2),
            source_id="invented-bounded-test", source_kind=SourceKind.REPLAY_SDK_PROXY,
            previous=previous)
        analysis._track_tire_stint_sample(tracker, sample)
        previous = sample
    assert len(tracker["invalid_reasons"]) == 1
    assert len(tracker["matched_service_ticks"]) == 1
