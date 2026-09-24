"""Invented paired files only. Hash agreement is not real tire-service evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from iracing_ai_engineer import capture_replay, tire_capture_replay
from iracing_ai_engineer.desktop_window import capture_report_text
from iracing_ai_engineer.synthetic_runtime import (
    run_synthetic_tire_capture_replay,
    synthetic_frames,
    write_synthetic_tire_trial,
)
from iracing_ai_engineer.tire_frame_binding import tire_frame_anchor, valid_frame_anchor
from iracing_ai_engineer.trial_replay import replay_trial


@pytest.fixture
def pair(tmp_path):
    return write_synthetic_tire_trial(tmp_path)


def rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def rewrite_journal(path, values):
    entries = [row for row in values if row["record"] == "entry"]
    for sequence, row in enumerate(entries, 1):
        row["sequence"] = sequence
    body = b"".join((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    for row in (values[0], *entries))
    footer = {**values[-1], "entries": len(entries),
              "stream_sha256": hashlib.sha256(body).hexdigest()}
    path.write_bytes(body + (json.dumps(footer) + "\n").encode())


def assertions(values):
    return [row["payload"] for row in values if row.get("lane") == "tire_confirmation"]


def test_real_recorder_journal_owner_join_is_repeatable_read_only_and_bounded(pair, monkeypatch):
    from iracing_ai_engineer import llm_client, sdk_probe, voice_service

    def forbidden(*_args, **_kwargs):
        raise AssertionError("UNEXPECTED_EXTERNAL_IO")

    for owner in (llm_client.DeepSeekClient, sdk_probe.WindowsPyirsdkTransport,
                  voice_service.VoiceService):
        monkeypatch.setattr(owner, "__init__", forbidden)
    capture, journal = pair
    before = (capture.read_bytes(), journal.read_bytes())
    result = capture_replay.replay_capture(capture, journal=journal)
    assert capture_replay.replay_capture(capture, journal=journal) == result
    assert (capture.read_bytes(), journal.read_bytes()) == before
    tires = result["tire_replay"]
    assert tires["applied"] == tires["assertions"] == 3 and tires["rejected"] == 0
    assert tires["states"][-1]["state"] == "UNKNOWN"
    assert any(row["counter_increase"] == 2 for row in tires["states"])
    assert any(row["state"] == "SUSPENDED_IN_PIT" for row in tires["states"])
    assert tires["capture_bytes_linked"] is True
    for flag in ("tire_service_verified", "original_scheduling_reproduced",
                 "physical_wear_available", "live_acceptance"):
        assert tires[flag] is False
    assert result["source_kind"] == "OFFLINE_REPLAY" and len(result["cards"]) <= 128
    assert "tire.driver_confirmed_age" in {
        row["id"] for card in result["cards"] for row in card["facts"]}
    text = json.dumps(result)
    for secret in ("frame_sha256", "capture_monotonic_us", "player_car_idx",
                   "SDK_LIVE", str(capture), str(journal), "synthetic-tire-replay-only"):
        assert secret not in text
    displayed = capture_report_text(result)
    assert "人工确认不等于" in displayed and "未复现原线程时序" in displayed
    assert "人工确认的胎组计圈" in displayed and "安装起点未知" in displayed


def test_packaged_synthetic_pair_check():
    assert run_synthetic_tire_capture_replay() == {
        "id": "SYNTHETIC_TIRE_CAPTURE_REPLAY", "status": "PASS"}


def test_raw_capture_without_journal_never_creates_confirmed_origin(pair):
    result = capture_replay.replay_capture(pair[0])
    assert result["tire_replay"] is None and "tire" not in result["latest"]


def test_renamed_but_identical_capture_is_matched_by_bytes_not_filename(pair):
    capture, journal = pair
    renamed = capture.with_name("renamed-synthetic-clip.jsonl")
    capture.rename(renamed)
    assert capture_replay.replay_capture(renamed, journal=journal)["tire_replay"]["applied"] == 3


@pytest.mark.parametrize("change,code", [
    ("capture_hash", "TIRE_CAPTURE_NOT_LINKED"), ("capture_size", "TIRE_CAPTURE_NOT_LINKED"),
    ("frame_hash", "TIRE_ASSERTION_UNMATCHED"), ("frame_clock", "TIRE_ASSERTION_UNMATCHED"),
    ("duplicate_anchor", "TIRE_ASSERTION_AMBIGUOUS"), ("legacy", "TIRE_ANCHOR_REQUIRED"),
])
def test_resealed_wrong_binding_never_returns_partial_tire_facts(pair, change, code):
    capture, journal = pair
    values = rows(journal)
    confirms = assertions(values)
    complete = next(row["payload"] for row in values
        if row.get("lane") == "capture" and row["payload"]["status"] == "COMPLETE")
    if change == "capture_hash":
        complete["sha256"] = "0" * 64
    elif change == "capture_size":
        complete["bytes"] += 1
    elif change == "frame_hash":
        confirms[0]["anchor"]["frame_sha256"] = "0" * 64
    elif change == "frame_clock":
        confirms[0]["anchor"]["capture_monotonic_us"] += 1
    elif change == "duplicate_anchor":
        confirms[1]["anchor"] = confirms[0]["anchor"].copy()
    else:
        values[0]["contract_version"] = "private-trial-audit-v2"
        for row in confirms:
            del row["anchor"]
    rewrite_journal(journal, values)
    assert replay_trial(journal)["tire_assertions"] == 3  # Still a valid assertion-only audit.
    with pytest.raises(capture_replay.CaptureReplayError, match=f"^{code}$"):
        capture_replay.replay_capture(capture, journal=journal)


def test_incomplete_journal_is_not_treated_as_a_complete_pair(pair):
    capture, journal = pair
    journal.write_bytes(b"\n".join(journal.read_bytes().splitlines()[:-1]) + b"\n")
    with pytest.raises(capture_replay.CaptureReplayError, match="TIRE_JOURNAL_INCOMPLETE"):
        capture_replay.replay_capture(capture, journal=journal)


def test_missing_unchanged_assertion_really_withdraws_old_origin(pair):
    capture, journal = pair
    values = rows(journal)
    values = [row for row in values if not (row.get("lane") == "tire_confirmation"
              and row["payload"]["assertion"]["kind"] == "NO_TIRE_CHANGE")]
    rewrite_journal(journal, values)
    result = capture_replay.replay_capture(capture, journal=journal)["tire_replay"]
    assert result["applied"] == 2
    assert not any(row["counter_increase"] == 2 for row in result["states"])


def test_current_owner_rejection_is_explicit_and_does_not_preserve_older_origin(pair):
    capture, journal = pair
    values = rows(journal)
    assertions(values)[1]["assertion"]["visit_tick"] = 0
    rewrite_journal(journal, values)
    result = capture_replay.replay_capture(capture, journal=journal)["tire_replay"]
    assert result["applied"] == 2 and result["rejected"] == 1
    assert any(row["reason"] == "REPLAY_ASSERTION_NOT_ADMITTED" for row in result["states"])
    assert not any(row["counter_increase"] == 2 for row in result["states"])


def test_extra_current_continuity_reset_cannot_be_undone_by_unchanged_assertion(pair, monkeypatch):
    original = capture_replay._LiveAnalysis.process

    def process(owner, item):
        original(owner, item)
        if item[0].buffer_tick == 82:
            owner._tire_age.reset("SOURCE_STALE")

    monkeypatch.setattr(capture_replay._LiveAnalysis, "process", process)
    result = capture_replay.replay_capture(pair[0], journal=pair[1])["tire_replay"]
    assert result["applied"] == 3 and result["current_owner_revision_substitutions"] > 0
    assert not any(row["state"] == "DRIVER_CONFIRMED_COUNTER" and row["frame"] > 82
                   for row in result["states"])


@pytest.mark.parametrize("limit", ["MAX_ASSERTIONS", "MAX_CAPTURES"])
def test_join_caps_fail_instead_of_silently_truncating_assertions(pair, monkeypatch, limit):
    monkeypatch.setattr(tire_capture_replay, limit, 0)
    with pytest.raises(capture_replay.CaptureReplayError, match="TIRE_REPLAY_LIMIT"):
        capture_replay.replay_capture(pair[0], journal=pair[1])


def test_state_history_evicts_but_retains_final_withdrawal(pair, monkeypatch):
    monkeypatch.setattr(tire_capture_replay, "MAX_STATES", 3)
    result = capture_replay.replay_capture(pair[0], journal=pair[1])["tire_replay"]
    assert len(result["states"]) == 3 and result["evicted_states"] > 0
    assert result["states"][-1]["state"] == "UNKNOWN"


def test_v1_pair_without_assertions_still_replays_without_inventing_an_origin(pair):
    capture, journal = pair
    values = [row for row in rows(journal) if row.get("lane") != "tire_confirmation"]
    values[0]["contract_version"] = "private-trial-audit-v1"
    rewrite_journal(journal, values)
    result = capture_replay.replay_capture(capture, journal=journal)
    assert result["tire_replay"]["status"] == "NO_ASSERTIONS" and "tire" not in result["latest"]


def test_pair_cancellation_exposes_no_partial_output(pair):
    calls = []

    def cancel():
        calls.append(True)
        return len(calls) > 20

    with pytest.raises(capture_replay.CaptureReplayError, match="^CANCELLED$"):
        capture_replay.replay_capture(pair[0], journal=pair[1], cancelled=cancel)


def test_anchor_is_fixed_numeric_projection_and_normalizes_negative_zero():
    frame = next(synthetic_frames(8))
    original = tire_frame_anchor(frame)
    assert valid_frame_anchor(original)
    changed = replace(frame, values={**frame.values, "SYNTHETIC_PRIVATE_NAME": "PRIVATE_TEXT"})
    assert tire_frame_anchor(changed) == original
    assert tire_frame_anchor(replace(frame, values={**frame.values, "Speed": -0.})) == (
        tire_frame_anchor(replace(frame, values={**frame.values, "Speed": 0.})))
    assert tire_frame_anchor(replace(frame, read_errors=("TireSetsUsed",))) != original


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "PRIVATE", [1], {"k": 1}])
def test_anchor_does_not_hash_arbitrary_non_numeric_contents(value):
    frame = next(synthetic_frames(8))
    with pytest.raises(ValueError, match="^TIRE_FRAME_ANCHOR_INVALID$"):
        tire_frame_anchor(replace(frame, values={**frame.values, "Speed": value}))


def test_user_facing_join_errors_remain_fixed_and_do_not_echo_paths():
    for code in tire_capture_replay.ERRORS:
        message = capture_report_text({"status": "REJECTED", "reason": code, "path": "PRIVATE"})
        assert "PRIVATE" not in message
