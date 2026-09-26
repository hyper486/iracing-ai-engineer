"""Invented sealed files only; no game, identities, cloud or audio devices."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from itertools import islice

import pytest

from iracing_ai_engineer import capture_replay as module
from iracing_ai_engineer.desktop_window import capture_report_text
from iracing_ai_engineer.synthetic_runtime import (
    run_synthetic_capture_replay,
    synthetic_frames,
    synthetic_pit_visit_frames,
    write_synthetic_capture,
)


def short_frames():
    return list(islice(synthetic_frames(8, rate=20), 25))


def capture(tmp_path, frames=None, **kwargs):
    path = tmp_path / "capture-synthetic.jsonl"
    write_synthetic_capture(path, short_frames() if frames is None else frames, **kwargs)
    return path


def test_real_fuel_corner_pipeline_is_repeatable_private_and_historical(tmp_path, monkeypatch):
    from iracing_ai_engineer import llm_client, sdk_probe, voice_service

    def forbidden(*_args, **_kwargs):
        raise AssertionError("UNEXPECTED_IO")

    monkeypatch.setattr(llm_client.DeepSeekClient, "__init__", forbidden)
    monkeypatch.setattr(sdk_probe.WindowsPyirsdkTransport, "__init__", forbidden)
    monkeypatch.setattr(voice_service.VoiceService, "__init__", forbidden)
    path = capture(tmp_path, synthetic_frames(8, rate=20))
    before = path.read_bytes()
    report = module.replay_capture(path)
    assert module.replay_capture(path) == report
    assert path.read_bytes() == before
    assert report["source_kind"] == "OFFLINE_REPLAY"
    assert report["source_authenticity"] == "UNVERIFIED"
    for flag in ("live_acceptance", "heard", "audio_played", "provider_called", "sdk_accessed"):
        assert report[flag] is False
    facts = {fact["id"] for card in report["cards"] for fact in card["facts"]}
    assert {"fuel.burn_per_lap", "driving.practice", "tire.pace"} <= facts
    assert report["latest"]["fuel"]["lap_completed"] == 8
    assert len(report["cards"]) <= module.MAX_CARDS
    text = json.dumps(report, ensure_ascii=False)
    for forbidden_text in ("SDK_LIVE", "synthetic-only", str(path), "SessionInfo", "speech"):
        assert forbidden_text not in text
    rendered = capture_report_text(report)
    assert "历史重算" in rendered and "练习假设" in rendered
    assert "原采集未保存手填策略参数" in rendered


def test_actual_completed_pit_has_one_card_and_frozen_check(tmp_path):
    report = module.replay_capture(capture(tmp_path, synthetic_pit_visit_frames()))
    cards = [row for row in report["cards"] if row["group"] == "pit"]
    assert len(cards) == 1 and cards[0]["segment"] == 1
    assert "19.9 到 20.1" in cards[0]["facts"][0]["text"]
    assert run_synthetic_capture_replay()["status"] == "PASS"


def test_sealed_pit_recompute_accepts_independently_based_publication_clock(tmp_path):
    frames = (replace(row, buffer_tick=row.buffer_tick + 1000)
              for row in synthetic_pit_visit_frames())
    report = module.replay_capture(capture(tmp_path, frames))
    cards = [row for row in report["cards"] if row["group"] == "pit"]
    assert len(cards) == 1 and cards[0]["segment"] == 1
    assert "19.9 到 20.1" in cards[0]["facts"][0]["text"]
    assert report["source_kind"] == "OFFLINE_REPLAY"
    assert not report["live_acceptance"] and not report["heard"]


def test_actual_app_recorder_full_schema_is_supported_not_just_selected_fields(tmp_path):
    from iracing_ai_engineer.collector import CollectorSample
    from iracing_ai_engineer.live_app_recording import AppRecorder
    from iracing_ai_engineer.sdk_probe import VariableDescriptor

    # The native recorder stores every SDK field; the analyst uses an allowlist.
    values = {"SessionNum": 0, "SessionTick": 0, "SessionTime": 0.}
    values.update({f"InventedExtra{i}": 42. for i in range(400)})
    descriptors = tuple(VariableDescriptor(name, 2 if type(value) is int else 5,
        "int32" if type(value) is int else "float64", index * 8, 1, False, "", "")
        for index, (name, value) in enumerate(values.items()))
    recorder = AppRecorder(tmp_path, source_id="synthetic", session_id="synthetic")
    try:
        for row in short_frames():
            recorder.ingest(CollectorSample(replace(row, values={**values,
                "SessionTime": row.values["SessionTime"], "SessionTick": row.buffer_tick}),
                descriptors, 20, None, "UNAVAILABLE"))
        recorder.finish()
    finally:
        recorder.close()
    report = module.replay_capture(next(tmp_path.glob("capture-*.jsonl")))
    assert report["frames"] == 25 and report["status"] == "RECOMPUTED"
    assert "InventedExtra" not in json.dumps(report)


@pytest.mark.parametrize("case", [
    "prefix", "tampered", "trailing", "invalid_json", "duplicate_key",
])
def test_unsealed_or_changed_semantics_never_return_partial_facts(tmp_path, case):
    path = capture(tmp_path, complete=case != "prefix")
    if case == "tampered":
        path.write_bytes(path.read_bytes().replace(b'"FuelLevel":70.0', b'"FuelLevel":60.0', 1))
    elif case == "trailing":
        with path.open("ab") as handle:
            handle.write(b'{}\n')
    elif case == "invalid_json":
        path.write_bytes(b'not-json\n')
    elif case == "duplicate_key":
        path.write_bytes(b'{"record_type":"run","record_type":"frame"}\n')
    with pytest.raises(module.CaptureReplayError, match="^INVALID$"):
        module.replay_capture(path)


@pytest.mark.parametrize("case", ["clock", "gap", "duplicate", "session_num", "session_clock"])
def test_collector_discontinuities_separate_evidence_segments(tmp_path, case):
    frames = short_frames()
    if case == "clock":
        frames[12:] = [replace(row, captured_monotonic_s=row.captured_monotonic_s - .3)
                       for row in frames[12:]]
    elif case == "gap":
        frames[12:] = [replace(row, captured_monotonic_s=row.captured_monotonic_s + 1)
                       for row in frames[12:]]
    elif case == "duplicate":
        frames.insert(12, replace(frames[11], values={**frames[11].values, "FuelLevel": 20.}))
    elif case == "session_num":
        frames[12:] = [replace(row, values={**row.values, "SessionNum": 1})
                       for row in frames[12:]]
    else:
        frames[12:] = [replace(row, values={**row.values, "SessionTime": row.values[
            "SessionTime"] - .3}) for row in frames[12:]]
    report = module.replay_capture(capture(tmp_path, frames))
    assert report["segments"] == 2
    assert report["events"]
    assert {row["segment"] for row in report["cards"]} == {1, 2}


@pytest.mark.parametrize("first_rate", [20, 60])
def test_actual_collector_schema_change_resets_owner(tmp_path, first_rate):
    from iracing_ai_engineer.adapters import open_collector_jsonl
    from iracing_ai_engineer.collector import CollectorSample, JsonlHandleWriter, LiveCollector
    from iracing_ai_engineer.sdk_probe import VariableDescriptor

    path = tmp_path / "schema-synthetic.jsonl"
    descriptors = (VariableDescriptor("SessionTime", 5, "float64", 0, 1, False, "", ""),)
    with path.open("x+b", buffering=0) as handle, JsonlHandleWriter(handle) as writer:
        collector = LiveCollector(writer, source_id="synthetic", session_id="synthetic")
        for index, frame in enumerate(short_frames()):
            if index == 12:
                descriptors = (replace(descriptors[0], offset=8),)
            collector.ingest(CollectorSample(replace(frame, values={"SessionTime": index / 20}),
                                              descriptors, first_rate if index < 12 else 20,
                                              None, "UNAVAILABLE"))
        collector.finish()
    with open_collector_jsonl(path) as run:
        assert run.evidence.tick_rate_hz_values == tuple(sorted({first_rate, 20}))
    report = module.replay_capture(path)
    assert report["events"]["schema_changed"] == 1 and report["segments"] == 2


def test_replay_sdk_origin_is_not_passed_off_as_driving(tmp_path):
    from iracing_ai_engineer.collector import CollectorSample, JsonlHandleWriter, LiveCollector
    from iracing_ai_engineer.sdk_probe import VariableDescriptor

    path = tmp_path / "replay-synthetic.jsonl"
    descriptors = (VariableDescriptor("SessionTime", 5, "float64", 0, 1, False, "", ""),)
    frame = replace(short_frames()[0], sim_mode_raw="replay", values={"SessionTime": 0.})
    with path.open("x+b", buffering=0) as handle, JsonlHandleWriter(handle) as writer:
        collector = LiveCollector(writer, source_id="synthetic", session_id="synthetic")
        collector.ingest(CollectorSample(frame, descriptors, 20, None, "UNAVAILABLE"))
        collector.finish()
    with pytest.raises(module.CaptureReplayError, match="SOURCE_UNSUPPORTED"):
        module.replay_capture(path)


def test_missing_clock_is_not_invented(tmp_path):
    rows = [replace(row, captured_monotonic_s=None) for row in short_frames()]
    with pytest.raises(module.CaptureReplayError, match="CLOCK_REQUIRED"):
        module.replay_capture(capture(tmp_path, rows))


def test_missing_metadata_withholds_corner_geometry(tmp_path):
    result = module.replay_capture(capture(tmp_path, metadata=False))
    assert "fuel" in result["latest"]
    assert all(fact["id"] != "driving.location" for card in result["cards"]
               for fact in card["facts"])


@pytest.mark.parametrize("limit", ["file", "line", "schema"])
def test_bounded_input(tmp_path, monkeypatch, limit):
    path = capture(tmp_path)
    monkeypatch.setattr(module, {"file": "MAX_CAPTURE_BYTES", "line": "MAX_LINE_BYTES",
                                "schema": "MAX_SCHEMAS"}[limit], 1 if limit != "schema" else 0)
    with pytest.raises(module.CaptureReplayError, match="TOO_LARGE"):
        module.replay_capture(path)


def test_cancellation_releases_owner_without_partial_output(tmp_path, monkeypatch):
    path = capture(tmp_path)
    closed, calls = [], []
    original = module._LiveAnalysis.close

    def close(owner):
        closed.append(True)
        original(owner)

    monkeypatch.setattr(module._LiveAnalysis, "close", close)
    def cancelled():
        calls.append(True)
        return len(calls) > 15

    with pytest.raises(module.CaptureReplayError, match="CANCELLED"):
        module.replay_capture(path, cancelled=cancelled)
    assert closed
    with path.open("ab"):
        pass


def test_identity_postcheck_precedes_report_publication(tmp_path, monkeypatch):
    path = capture(tmp_path)
    original = module._plain_file

    @contextmanager
    def changed(*args):
        with original(*args) as opened:
            yield opened
        raise module.TrialReplayError("TRIAL_FILE_CHANGED")

    monkeypatch.setattr(module, "_plain_file", changed)
    with pytest.raises(module.CaptureReplayError, match="FILE_CHANGED"):
        module.replay_capture(path)


def test_hardlink_and_public_checkout_are_rejected(tmp_path):
    from pathlib import Path

    path = capture(tmp_path)
    link = tmp_path / "alias.jsonl"
    os.link(path, link)
    with pytest.raises(module.CaptureReplayError, match="FILE_UNSAFE"):
        module.replay_capture(path)
    with pytest.raises(module.CaptureReplayError):
        module.replay_capture(Path(__file__))


def test_history_cap_does_not_grow_with_lap_count(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_CARDS", 3)
    result = module.replay_capture(capture(tmp_path, synthetic_pit_visit_frames()))
    assert len(result["cards"]) == 3 and result["evicted_cards"] > 0
    assert len(result["latest"]) <= 4


@pytest.mark.parametrize("report", [None, {}, {"status": "REJECTED", "reason": "PRIVATE"},
                                   {"status": "RECOMPUTED", "text": "PRIVATE"}])
def test_display_never_echoes_unvalidated_file_errors(report):
    assert "PRIVATE" not in capture_report_text(report)
