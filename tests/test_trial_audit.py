"""Invented frames and fake audio only; no SDK, microphone, speaker or cloud."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import replace

import pytest

from iracing_ai_engineer import trial_audit
from iracing_ai_engineer.live_app import AppState
from iracing_ai_engineer.live_worker import FrameWorker
from iracing_ai_engineer.priority_audio import PriorityAudio
from iracing_ai_engineer.sdk_probe import RawSdkFrame
from iracing_ai_engineer.spotter import SPOTTER_FIELDS
from iracing_ai_engineer.spotter_voice import SpotterVoice
from iracing_ai_engineer.trial_audit import TrialAudit, frame_projection
from iracing_ai_engineer.trial_replay import TrialReplayError, main, replay_trial
from iracing_ai_engineer.voice_audio import AudioError
from iracing_ai_engineer.voice_service import cue_wave
from iracing_ai_engineer.voice_settings import default_voice_settings


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def frame(tick, code=2, **changes):
    values = {"SessionNum": 0, "SessionTick": tick, "SessionTime": tick / 60,
              "PlayerCarIdx": 0, "CarLeftRight": code, "IsOnTrack": True,
              "IsOnTrackCar": True, "IsReplayPlaying": False, "OnPitRoad": False,
              "PlayerCarInPitStall": False}
    values.update(changes)
    return RawSdkFrame(tick, 1, values, sim_mode_raw="full",
                       captured_monotonic_s=100 + tick / 60)


class Trial:
    def __init__(self, path, **kwargs):
        self.clock = Clock()
        self.journal = TrialAudit(path, clock=self.clock, **kwargs)
        self.state = AppState(clock=self.clock)
        self.state.attach_trial(self.journal)
        self.state.connection("CONNECTED")
        self.state.start_spotter(60)
        self.path = path / self.journal.snapshot()["file_name"]

    def feed(self, tick, code=2, **changes):
        self.clock.now = 100 + tick / 60
        self.state.feed_spotter(frame(tick, code, **changes))

    def close(self, *, complete=True):
        self.state.attach_trial(None)
        self.journal.close(complete=complete)


@pytest.fixture
def trial(tmp_path):
    value = Trial(tmp_path)
    yield value
    value.close()


def read_rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def rewrite(path, rows):
    raw = b"".join((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                   for row in rows[:-1])
    footer = {**rows[-1], "stream_sha256": hashlib.sha256(raw).hexdigest()}
    path.write_bytes(raw + (json.dumps(footer) + "\n").encode())


def test_complete_round_trip_binds_detector_and_footer_without_acceptance(trial):
    for tick in range(1, 5):
        trial.feed(tick)
    trial.close()
    report = replay_trial(trial.path)
    assert report["status"] == "REPLAY_MATCH"
    assert report["frames"] == 4 and report["detector_segments"] == 1
    assert report["decisions"]["CANDIDATE"] == 1
    assert report["events_tail"][0]["event_id"] == [1, 0, 1]
    assert report["event_results"] == {"DETECTED_NO_RECORDED_ATTEMPT": 1}
    assert not any(report[key] for key in ("heard", "live_acceptance", "audio_played"))
    assert report["source_kind"] == "OFFLINE_REPLAY"
    assert report["source_authenticity"] == "UNVERIFIED"
    assert trial.journal.snapshot()["status"] == "COMPLETE"


def test_initial_silent_poll_origin_and_high_water_clock_are_reproduced(trial):
    trial.state.spotter_snapshot()  # First clock observation must not be omitted.
    trial.clock.now += 0.001
    trial.state.spotter_snapshot()  # Subsequent silent polls can be omitted.
    for tick in range(1, 4):
        trial.feed(tick)
        trial.clock.now += 0.001
        trial.state.spotter_snapshot()
    trial.clock.now += 0.5
    trial.state.spotter_snapshot()  # Time-driven staleness has no SDK frame.
    trial.close()
    report = replay_trial(trial.path)
    assert report["status"] == "REPLAY_MATCH"
    assert report["health_transitions"]["NO_FRESH_TICK"] == 1
    assert report["events_tail"][0]["withdrawal"] == "INVALIDATED"
    assert report["operations"] == 5


def test_clock_regression_duplicate_context_and_reconnect_match(trial):
    for tick in range(1, 6):
        trial.feed(tick)
    trial.feed(5)  # Duplicate does not refresh freshness.
    trial.clock.now -= 0.001
    trial.state.spotter_snapshot()
    for tick in range(6, 10):
        trial.feed(tick, PlayerCarIdx=1)
    trial.state.connection("DISCONNECTED")
    trial.state.connection("CONNECTED")
    trial.state.start_spotter(60)
    for tick in range(10, 14):
        trial.feed(tick, 3)
    trial.state.connection("STOPPED")
    trial.close()
    report = replay_trial(trial.path)
    assert report["status"] == "REPLAY_MATCH" and report["detector_segments"] == 2
    assert report["health_transitions"]["CLOCK_REGRESSION"] == 1
    assert {row["event_id"][0] for row in report["events_tail"]} == {1, 3}


@pytest.mark.parametrize("name,value,reason", [
    ("CarLeftRight", 0, "SDK_SPOTTER_OFF"),
    ("CarLeftRight", True, "PROXIMITY_FIELD_INVALID"),
    ("CarLeftRight", "private arbitrary text", "PROXIMITY_FIELD_INVALID"),
    ("SessionNum", None, "CORE_FIELD_INVALID"),
    ("SessionTick", True, "CORE_FIELD_INVALID"),
    ("SessionTime", float("nan"), "SESSION_TIME_INVALID"),
    ("SessionTime", float("inf"), "SESSION_TIME_INVALID"),
    ("SessionTime", -1, "SESSION_TIME_INVALID"),
    ("OnPitRoad", 0, "NOT_DRIVING_ON_TRACK"),
    ("IsOnTrack", 1, "NOT_DRIVING_ON_TRACK"),
    ("IsReplayPlaying", True, "NOT_DRIVING_ON_TRACK"),
])
def test_invalid_fields_preserve_admission_reason(trial, name, value, reason):
    trial.feed(1, **{name: value})
    trial.close()
    report = replay_trial(trial.path)
    assert reason in report["health_transitions"]
    assert b"private arbitrary text" not in trial.path.read_bytes()


@pytest.mark.parametrize("change,reason", [
    ({"buffer_tick": True}, "TICK_MISMATCH"),
    ({"captured_monotonic_s": None}, "FRAME_TIMESTAMP"),
    ({"captured_monotonic_s": float("nan")}, "FRAME_TIMESTAMP"),
    ({"sim_mode_raw": {"private": "value"}}, "NOT_LIVE_SOURCE"),
    ({"read_errors": ("CarLeftRight", "unrelated private field")}, "FIELD_READ_ERROR"),
])
def test_metadata_projection_replays(trial, change, reason):
    trial.clock.now = 100 + 1 / 60
    trial.state.feed_spotter(replace(frame(1), **change))
    trial.close()
    assert reason in replay_trial(trial.path)["health_transitions"]
    assert b"private" not in trial.path.read_bytes().replace(b"private-trial-audit-v1", b"")


def test_projection_never_copies_extra_sdk_values_or_identity():
    value = frame_projection(frame(1, DriverInfo={"name": "PRIVATE"}, FuelLevel=123))
    assert len(value["values"]) == len(SPOTTER_FIELDS)
    assert "PRIVATE" not in str(value) and "FuelLevel" not in str(value)


@pytest.mark.parametrize("lane", ["detector", "audio", "capture", "unknown"])
def test_arbitrary_text_or_raw_dictionary_cannot_enter_private_journal(tmp_path, lane):
    journal = TrialAudit(tmp_path)
    assert not journal.offer(lane, {"transcript": "SYNTHETIC SECRET"})
    journal.close()
    assert journal.snapshot()["failed"]
    [path] = tmp_path.glob("*.jsonl")
    assert b"SYNTHETIC SECRET" not in path.read_bytes()


@pytest.mark.parametrize("stage,reason", [
    ("header", "STARTUP_FAILED"), ("entry", "PROCESSING_FAILED"), ("footer", "FINALIZE_FAILED"),
])
def test_io_failure_latches_only_diagnostics_without_exposing_private_error(
    monkeypatch, tmp_path, stage, reason,
):
    original = trial_audit.JsonlHandleWriter.write

    def failed_write(self, record):
        if record["record"] == stage:
            raise OSError("SYNTHETIC PRIVATE DISK FAILURE")
        return original(self, record)

    monkeypatch.setattr(trial_audit.JsonlHandleWriter, "write", failed_write)
    trial = Trial(tmp_path)
    for tick in range(1, 4):
        trial.feed(tick)
    assert trial.state.spotter_snapshot()["spotter"]["candidate"]["kind"] == "CAR_LEFT"
    trial.close()
    snapshot = trial.journal.snapshot()
    assert snapshot["failed"] and snapshot["reason"] == reason
    assert "PRIVATE DISK" not in str(snapshot)
    assert not any(row["record"] == "footer" for row in read_rows(trial.path))


def test_out_of_trace_domain_fails_journal_not_detector(trial):
    trial.feed(1, SessionTime=2**54)
    assert trial.state.spotter_snapshot()["spotter"]["status"] == "ACQUIRING"
    trial.close()
    assert trial.journal.snapshot()["failed"] is True
    assert not any(row["record"] == "footer" for row in read_rows(trial.path))


def test_more_than_memory_tail_survives_on_disk(trial):
    for tick in range(1, 902):
        trial.feed(tick, 2 if (tick - 1) // 3 % 2 else 3)
    assert len(trial.state.spotter_audit()) == 128
    trial.close()
    result = replay_trial(trial.path)
    assert result["status"] == "REPLAY_MATCH"
    assert result["decisions"]["CANDIDATE"] == 300
    assert len(result["events_tail"]) == 128
    assert sum(result["event_results"].values()) == 300


def test_correlation_memory_has_explicit_limit_and_eviction_count(trial, monkeypatch):
    from iracing_ai_engineer import trial_replay

    monkeypatch.setattr(trial_replay, "_PENDING_LIMIT", 8)
    for tick in range(1, 37):
        trial.feed(tick, 2 if (tick - 1) // 3 % 2 else 3)
    trial.close()
    result = replay_trial(trial.path)
    assert result["decisions"]["CANDIDATE"] == 12
    assert result["correlation_evictions"] == 4 and result["correlation_limit"] == 8
    assert sum(result["event_results"].values()) == 12


class Speech:
    def synthesize(self, _text, **_kwargs):
        return cue_wave(800)


class Audio:
    fail = False

    def devices(self):
        return {"outputs": [{"id": "synthetic"}]}

    def urgent(self, _raw, _cancel, **kwargs):
        if self.fail:
            raise AudioError("AUDIO_DEVICE_MISSING")
        kwargs["on_started"]()


def test_actual_voice_callbacks_correlate_without_audio_or_private_settings(trial):
    voice = SpotterVoice(lambda: {**trial.state.spotter_snapshot(), "lifecycle": "RUNNING"},
                         Audio(), speech=Speech(), clock=trial.clock,
                         audit_sink=trial.state.audit_audio)
    settings = {**default_voice_settings(), "spotter_enabled": True, "voice": "PRIVATE VOICE"}
    voice.configure(settings)
    voice._prepare(voice._revision, voice._settings)
    for tick in range(1, 4):
        trial.feed(tick)
    voice._step(voice._revision, voice._settings)
    voice.close()
    trial.close()
    report = replay_trial(trial.path)
    row = report["events_tail"][0]
    assert row["result"] == "SOFTWARE_COMPLETED_NOT_HEARING_CONFIRMED"
    assert row["audio_outcomes"] == ["ATTEMPTED", "PLAYBACK_STARTED", "PLAYBACK_COMPLETED"]
    assert row["start_delay_ms"] == 0
    assert report["unbound_audio_records"] == 0
    assert b"PRIVATE VOICE" not in trial.path.read_bytes()


def test_playback_error_is_bound_to_candidate_not_just_global_failure(trial):
    audio = Audio()
    audio.fail = True
    voice = SpotterVoice(lambda: {**trial.state.spotter_snapshot(), "lifecycle": "RUNNING"},
                         audio, speech=Speech(), clock=trial.clock,
                         audit_sink=trial.state.audit_audio)
    settings = {**default_voice_settings(), "spotter_enabled": True}
    voice.configure(settings)
    voice._prepare(voice._revision, voice._settings)
    for tick in range(1, 4):
        trial.feed(tick)
    with pytest.raises(AudioError):
        voice._step(voice._revision, voice._settings)
    voice.close()
    trial.close()
    report = replay_trial(trial.path)
    assert report["event_results"] == {"PLAYBACK_ERROR": 1}
    assert report["audio_outcomes"]["PLAYBACK_ERROR"] == 1


def test_disabled_audio_is_an_observation_not_an_inferred_hearing_result(trial):
    voice = SpotterVoice(lambda: {}, Audio(), speech=Speech(), clock=trial.clock,
                         audit_sink=trial.state.audit_audio)
    voice.configure(default_voice_settings())
    for tick in range(1, 4):
        trial.feed(tick)
    voice.close()
    trial.close()
    row = replay_trial(trial.path)["events_tail"][0]
    assert row["audio_health_at_detection"]["reason"] == "DISABLED"
    assert row["audio_outcomes"] == []
    assert row["result"] == "DETECTED_NO_RECORDED_ATTEMPT"


def test_diagnostic_callback_exception_does_not_break_audio():
    def fail(_payload):
        raise RuntimeError("private detail")

    voice = SpotterVoice(lambda: {}, PriorityAudio(Audio()), speech=Speech(), audit_sink=fail)
    voice.configure(default_voice_settings())
    voice.trace_state()
    voice.close()
    assert voice.snapshot()["status"] == "CLOSED"


def test_prefix_without_footer_and_partial_last_write_are_not_complete(trial):
    trial.feed(1)
    trial.close(complete=False)
    assert replay_trial(trial.path)["status"] == "INCOMPLETE_PREFIX"
    with trial.path.open("ab") as handle:
        handle.write(b'{"record":"entry"')
    assert replay_trial(trial.path)["status"] == "INCOMPLETE_PREFIX"


@pytest.mark.parametrize("mutation", [
    "hash", "boolean_sequence", "extra_key", "accepted", "wrong_decision", "wrong_prior_clock",
    "wrong_frame", "duplicate_key", "trailing", "nan", "huge_line", "raised", "wrong_generation",
])
def test_modified_malformed_or_forged_journals_fail_closed(trial, mutation):
    for tick in range(1, 4):
        trial.feed(tick)
    trial.close()
    rows = read_rows(trial.path)
    if mutation == "hash":
        rows[-1]["stream_sha256"] = "0" * 64
        trial.path.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in rows))
    elif mutation == "duplicate_key":
        raw = trial.path.read_bytes().replace(b'"heard":false', b'"heard":false,"heard":false', 1)
        trial.path.write_bytes(raw)
    elif mutation == "trailing":
        with trial.path.open("ab") as handle:
            handle.write(b"\n")
    elif mutation == "nan":
        rows[2]["payload"]["now"] = float("nan")
        rewrite(trial.path, rows)
    elif mutation == "huge_line":
        trial.path.write_bytes(b"x" * (32 * 1024 + 1))
    else:
        if mutation == "boolean_sequence":
            rows[1]["sequence"] = True
        elif mutation == "extra_key":
            rows[0]["api_key"] = "private"
        elif mutation == "accepted":
            rows[0]["live_acceptance"] = True
        elif mutation == "wrong_decision":
            rows[2]["payload"]["decisions"] = []
        elif mutation == "wrong_prior_clock":
            rows[2]["payload"]["prior_clock"] = 99
        elif mutation == "wrong_frame":
            rows[2]["payload"]["frame"]["values"][4] = 0
        elif mutation == "raised":
            rows[2]["payload"]["raised"] = True
        elif mutation == "wrong_generation":
            rows[2]["payload"]["generation"] = 3
        rewrite(trial.path, rows)
    with pytest.raises(TrialReplayError) as caught:
        replay_trial(trial.path)
    assert "private" not in str(caught.value)
    assert caught.value.__context__ is None


def test_link_files_and_repo_directories_are_refused(trial, tmp_path):
    trial.feed(1)
    trial.close()
    hardlink = tmp_path / "copy.jsonl"
    os.link(trial.path, hardlink)
    with pytest.raises(TrialReplayError, match="TRIAL_FILE_UNSAFE"):
        replay_trial(hardlink)
    hardlink.unlink()
    repo = tmp_path / "public-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    (repo / "AGENTS.md").write_text("", encoding="utf-8")
    copied = repo / "trial.jsonl"
    copied.write_bytes(trial.path.read_bytes())
    with pytest.raises(TrialReplayError):
        replay_trial(copied)


def test_capture_manifest_is_linked_but_not_implicitly_authenticated(trial, tmp_path):
    trial.feed(1)
    identifier = "a" * 32
    raw = b"synthetic capture byte link only\n"
    path = tmp_path / f"capture-{identifier}.jsonl"
    path.write_bytes(raw)
    base = {"generation": 1, "capture_id": identifier, "bytes": 0,
            "sha256": hashlib.sha256(b"").hexdigest()}
    trial.state.audit_capture({**base, "status": "OPEN"})
    trial.state.audit_capture({**base, "status": "COMPLETE", "bytes": len(raw),
                               "sha256": hashlib.sha256(raw).hexdigest()})
    trial.close()
    assert replay_trial(trial.path)["capture_byte_checks"] == {"NOT_CHECKED": 1}
    result = replay_trial(trial.path, capture_directory=tmp_path)
    assert result["capture_byte_checks"] == {"MATCH": 1}
    assert not result["live_acceptance"] and result["source_authenticity"] == "UNVERIFIED"
    path.write_bytes(raw.upper())
    assert replay_trial(trial.path, capture_directory=tmp_path)["capture_byte_checks"] == {
        "MISMATCH": 1}


def test_tiny_budget_latches_failure_while_proximity_continues(tmp_path):
    trial = Trial(tmp_path, max_bytes=1024)
    try:
        for tick in range(1, 11):
            trial.feed(tick)
        assert trial.state.spotter_snapshot()["spotter"]["status"] == "READY"
    finally:
        trial.close()
    snapshot = trial.journal.snapshot()
    assert snapshot["failed"] and snapshot["max_file_bytes"] == 1024
    assert snapshot["status"] == "LIMIT_REACHED" and snapshot["reason"] == "FILE_LIMIT"
    assert snapshot["bytes"] <= 1024
    assert replay_trial(trial.path)["status"] == "INCOMPLETE_PREFIX"


def test_slow_sink_overflow_and_close_wait_do_not_run_on_producer(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Sink:
        byte_count = 0

        def __init__(self, *_args):
            entered.set()
            assert release.wait(3)

        def process(self, _item):
            pass

        def finish(self):
            raise AssertionError("overflow must never finalize")

        def close(self):
            pass

    def worker(factory, **kwargs):
        return FrameWorker(factory, **{**kwargs, "max_frames": 2})

    monkeypatch.setattr(trial_audit, "_Journal", Sink)
    trial = Trial(tmp_path, worker_factory=worker)
    assert entered.wait(1)
    before = time.perf_counter()
    for tick in range(1, 4):
        trial.feed(tick)
    assert time.perf_counter() - before < 0.5
    assert trial.journal.snapshot()["reason"] == "QUEUE_OVERFLOW"
    assert trial.state.spotter_snapshot()["spotter"]["status"] == "READY"
    closer = threading.Thread(target=trial.close)
    closer.start()
    try:
        closer.join(0.05)
        assert closer.is_alive() and not trial.journal.snapshot()["done"]
    finally:
        release.set()
        closer.join(2)
    assert not closer.is_alive() and trial.journal.snapshot()["done"]


def test_cli_is_read_only_and_reports_nonacceptance(trial, capsys):
    trial.feed(1)
    trial.close()
    assert main([str(trial.path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["heard"] is report["audio_played"] is report["live_acceptance"] is False
    assert main([str(trial.path.with_name("missing.jsonl"))]) == 2
    assert "missing.jsonl" not in capsys.readouterr().out


def test_replay_cancellation_and_file_mutation_never_return_matching_report(trial, monkeypatch):
    from iracing_ai_engineer import trial_replay

    trial.feed(1)
    trial.close()
    with pytest.raises(TrialReplayError, match="TRIAL_CANCELLED"):
        replay_trial(trial.path, cancelled=lambda: True)
    original = trial_replay._Replay.detector

    def change_file(self, item):
        original(self, item)
        with trial.path.open("ab") as handle:
            handle.write(b"\n")

    monkeypatch.setattr(trial_replay._Replay, "detector", change_file)
    with pytest.raises(TrialReplayError, match="TRIAL_FILE_CHANGED"):
        replay_trial(trial.path)
