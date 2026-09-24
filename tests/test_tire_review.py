"""Synthetic explicit review only; never claims real service or human authentication."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from iracing_ai_engineer.adapters import open_collector_jsonl
from iracing_ai_engineer.capture_replay import (
    CaptureReplayError,
    replay_capture_for_tire_review,
)
from iracing_ai_engineer.engineer_session import canonical_sha256
from iracing_ai_engineer.retrieved_live_analysis import (
    _bound_event_identity_from_context,
    _build_tire_stint_context,
    _new_tire_stint_tracker,
    _track_tire_stint_sample,
)
from iracing_ai_engineer.synthetic_runtime import write_synthetic_tire_trial
from iracing_ai_engineer.tire_review import SKIP, TireReviewError, export_reviewed_tires
from iracing_ai_engineer.tire_service_history import validate_service_history


@pytest.fixture
def review(tmp_path):
    capture, journal = write_synthetic_tire_trial(tmp_path)
    report, plan = replay_capture_for_tire_review(capture, journal=journal)
    evidence = tmp_path / "explicitly-invented-service-evidence.txt"
    evidence.write_text("Invented fixture, not an actual service or human review.",
                        encoding="utf-8")
    decisions = [{"ordinal": i + 1, "kind": kind, "evidence_path": evidence,
                  "evidence_offset_s": i * 5} for i, kind in enumerate(
                      ("FULL_NEW_SET", "NO_TIRE_CHANGE", "PARTIAL_OR_UNKNOWN"))]
    return capture, journal, report, plan, decisions, tmp_path / "exports"


def export(review, **kwargs):
    capture, journal, _, plan, decisions, directory = review
    return export_reviewed_tires(capture, journal,
        **{"expected_plan_sha256": plan["plan_sha256"], "decisions": decisions,
           "attested": True, "directory": directory, **kwargs})


def load_history(review, result):
    path = review[-1] / result["directory_name"]
    return json.loads((path / "service-history.json").read_text("utf-8")), json.loads(
        (path / "review.json").read_text("utf-8"))


def test_plan_uses_exit_not_confirmation_ticks_and_same_adapter_identity(review):
    capture, journal, report, plan, _, _ = review
    assert [r["decision_tick"] for r in plan["exits"]] == [65, 165, 265]
    assert [r["visit_tick"] for r in plan["exits"]] == [10, 110, 210]
    assert [r["driver_assertion"] for r in plan["exits"]] == [
        "FULL_NEW_SET", "NO_TIRE_CHANGE", "PARTIAL_OR_UNKNOWN"]
    assert all(r["assertion_recomputed"] for r in plan["exits"])
    assert plan["status"] == "PENDING_HUMAN_REVIEW" and not plan["live_acceptance"]
    assert replay_capture_for_tire_review(capture, journal=journal) == (report, plan)
    assert "plan_sha256" not in json.dumps(report)
    with open_collector_jsonl(capture) as run:
        assert plan["source_receipt_sha256"] == canonical_sha256(run.evidence.to_dict())
        assert plan["identity_sha256"] == canonical_sha256(
            _bound_event_identity_from_context(run.event_identity_context.to_dict()))
    for private in (str(capture), str(journal), "player_car_idx", "session_id", "source_id"):
        assert private not in json.dumps(plan)


def context(capture, history):
    with open_collector_jsonl(capture) as run:
        identity = canonical_sha256(_bound_event_identity_from_context(
            run.event_identity_context.to_dict()))
        source = canonical_sha256(run.evidence.to_dict())
        validate_service_history(history, expected_history_sha256=history["history_sha256"],
            expected_identity_sha256=identity, expected_source_receipt_sha256=source)
        tracker = _new_tire_stint_tracker(history)
        for sample in run.samples:
            _track_tire_stint_sample(tracker, sample)
    return _build_tire_stint_context(tracker, identity_sha256=identity,
        source_receipt_sha256=source, expected_decision_tick=300)


def test_explicit_export_enters_existing_tire_history_but_partial_revokes_age(review):
    result = export(review)
    history, receipt = load_history(review, result)
    assert result["reviewed_events"] == 3 and not result["live_acceptance"]
    assert context(review[0], history)["status"] == "WAIT_STINT_ORIGIN"
    assert receipt["plan"] == review[3]
    assert receipt["history_sha256"] == history["history_sha256"]
    for row, label in zip(receipt["reviews"], history["events"], strict=True):
        assert row["review_sha256"] == canonical_sha256(
            {k: v for k, v in row.items() if k != "review_sha256"})
        assert label["label_receipt_sha256"] == row["review_sha256"]
        assert row["reviewer_authentication"] == "SELF_ATTESTED_NOT_AUTHENTICATED"
    assert str(review[4][0]["evidence_path"]) not in json.dumps(receipt)
    assert "Invented fixture" not in json.dumps(receipt)
    again = export(review)
    assert again["directory_name"] != result["directory_name"]
    assert load_history(review, again) == (history, receipt)


def test_human_can_correct_assertion_and_unreviewed_stop_cannot_preserve_age(review):
    decisions = copy.deepcopy(review[4])
    decisions[-1]["kind"] = "NO_TIRE_CHANGE"  # Explicit invented review, not copied assertion.
    result = export(review, decisions=decisions)
    history, _ = load_history(review, result)
    assert context(review[0], history)["stint_age_completed_laps"] == 3
    decisions[1].update(kind=SKIP, evidence_path=None, evidence_offset_s=None)
    result = export(review, decisions=decisions)
    history, receipt = load_history(review, result)
    assert result["unreviewed_events"] == 1 and len(receipt["reviews"]) == 3
    assert context(review[0], history)["status"] == "WAIT_STINT_ORIGIN"


@pytest.mark.parametrize("fault", ["no_attestation", "missing", "kind", "ordinal", "boolean",
                                  "no_evidence", "capture", "journal", "offset", "plan",
                                  "all_skip"])
def test_no_implicit_approval_or_wrong_binding_output(review, fault):
    decisions, kwargs = copy.deepcopy(review[4]), {}
    if fault == "no_attestation":
        kwargs["attested"] = False
    elif fault == "missing":
        decisions.pop()
    elif fault == "kind":
        decisions[0]["kind"] = ""
    elif fault in ("ordinal", "boolean"):
        decisions[0]["ordinal"] = 2 if fault == "ordinal" else True
    elif fault == "no_evidence":
        decisions[0]["evidence_path"] = None
    elif fault in ("capture", "journal"):
        decisions[0]["evidence_path"] = review[0 if fault == "capture" else 1]
    elif fault == "offset":
        decisions[0]["evidence_offset_s"] = True
    elif fault == "plan":
        kwargs["expected_plan_sha256"] = "0" * 64
    else:
        for row in decisions:
            row.update(kind=SKIP, evidence_path=None, evidence_offset_s=None)
    with pytest.raises(TireReviewError):
        export(review, decisions=decisions, **kwargs)
    assert not review[-1].exists()


def test_export_revalidates_source_and_cancellation_before_writes(review):
    with pytest.raises(TireReviewError, match="CANCELLED"):
        export(review, cancelled=lambda: True)
    capture = review[0]
    capture.write_bytes(capture.read_bytes()[:-1] + b" ")
    with pytest.raises(CaptureReplayError):
        export(review)
    assert not review[-1].exists()


def test_review_plan_requires_pair_and_caps_all_exits(review, monkeypatch):
    from iracing_ai_engineer import tire_review

    with pytest.raises(CaptureReplayError, match="TIRE_REVIEW_UNAVAILABLE"):
        replay_capture_for_tire_review(review[0], journal=None)
    monkeypatch.setattr(tire_review, "MAX_EXITS", 2)
    with pytest.raises(CaptureReplayError, match="TIRE_REVIEW_LIMIT"):
        replay_capture_for_tire_review(review[0], journal=review[1])


def test_evidence_hash_rejects_hardlinks_and_checkout_destinations(review):
    linked = review[4][0]["evidence_path"].with_name("hardlinked-evidence.txt")
    linked.hardlink_to(review[4][0]["evidence_path"])
    with pytest.raises(ValueError):
        export(review)
    linked.unlink()
    with pytest.raises(ValueError):
        export(review, directory=Path(__file__).resolve().parents[1] / "private-review-forbidden")


@pytest.mark.parametrize("source", [0, 1])
def test_copy_of_capture_or_journal_is_not_independent_service_evidence(review, source):
    copied = review[0].parent.parent / "copied-input.txt"
    copied.write_bytes(review[source].read_bytes())
    decisions = copy.deepcopy(review[4])
    decisions[0]["evidence_path"] = copied
    with pytest.raises(TireReviewError, match="INDEPENDENT_EVIDENCE"):
        export(review, decisions=decisions)
    assert not review[-1].exists()


def test_reused_video_is_hashed_once_and_total_evidence_is_bounded(review, monkeypatch):
    from iracing_ai_engineer import tire_review

    calls = []
    original = tire_review._evidence_digest
    def hash_file(path, *args):
        calls.append(path)
        return original(path, *args)
    monkeypatch.setattr(tire_review, "_evidence_digest", hash_file)
    export(review)
    assert calls.count(review[4][0]["evidence_path"]) == 1
    monkeypatch.setattr(tire_review, "MAX_EVIDENCE_BYTES", 1)
    with pytest.raises(ValueError, match="TOO_LARGE"):
        export(review)


def test_review_draft_refuses_multiple_sessions_and_does_not_bridge_gaps():
    from types import SimpleNamespace

    from iracing_ai_engineer.telemetry import SourceKind, normalize_sdk_frame
    from iracing_ai_engineer.tire_review import TireReviewDraft

    draft, previous = TireReviewDraft(), None
    for tick, pit in ((0, True), (2, False)):
        sample = normalize_sdk_frame({"SessionTick": tick, "SessionNum": 0,
            "SessionTime": tick / 60, "LapCompleted": 1, "OnPitRoad": pit,
            "PlayerTireCompound": 0, "TireSetsUsed": 1}, source_id="synthetic-review-gap",
            source_kind=SourceKind.REPLAY_SDK_PROXY, previous=previous)
        draft.feed(sample)
        previous = sample
    assert not draft.rows and draft.unavailable_frames
    with pytest.raises(TireReviewError, match="UNAVAILABLE"):
        draft.finish(SimpleNamespace(session_reset_count=1), None, "0" * 64)


def test_synthetic_export_selftest_and_demo_controller_never_export_real_review():
    from iracing_ai_engineer.desktop_app import _SelfTestController
    from iracing_ai_engineer.synthetic_runtime import run_synthetic_tire_review

    assert run_synthetic_tire_review() == {"id": "SYNTHETIC_TIRE_REVIEW_EXPORT", "status": "PASS"}
    controller = _SelfTestController()
    try:
        assert controller.tire_review_plan()["synthetic_demo"] is True
        with pytest.raises(ValueError, match="SELF_TEST_ONLY"):
            controller.export_tire_review(attested=True)
    finally:
        controller.close()
