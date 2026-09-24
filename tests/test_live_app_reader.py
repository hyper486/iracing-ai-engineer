"""Synthetic SDK-to-monitor-to-fuel integration; never open the real simulator."""

from __future__ import annotations

import json
import math
import threading
import time
from types import SimpleNamespace

import pytest

from iracing_ai_engineer import live_app, live_app_recording
from iracing_ai_engineer.collector import CollectorConsistencyError, CollectorSample
from iracing_ai_engineer.live_fuel import LiveFuelConfig
from iracing_ai_engineer.live_worker import FrameWorker
from iracing_ai_engineer.sdk_probe import (
    SDK_TYPE_NAMES,
    SDK_TYPE_SIZES,
    RawSdkFrame,
    VariableDescriptor,
)

MIB = 1024**2


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Stop:
    def __init__(self):
        self.stopped = False
        self.retries = []

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        if seconds == 5:
            self.retries.append(seconds)
            # A broken reconnect loop must fail the test rather than hang it.
            assert len(self.retries) <= 5
        return self.stopped


class ObservedState(live_app.AppState):
    def __init__(self, clock):
        super().__init__(clock=clock)
        self.publications = []
        self.transitions = []
        self.recording_states = []

    def publish(self, monitor, fuel, speech, session_type, **kwargs):
        super().publish(monitor, fuel, speech, session_type, **kwargs)
        self.publications.append(self.snapshot())

    def connection(self, status):
        super().connection(status)
        self.transitions.append({**self.snapshot(), "requested_connection": status})

    def recording(self, status, byte_count):
        super().recording(status, byte_count)
        self.recording_states.append((status, byte_count))


def _values(tick, *, in_car=True):
    # Ten-second synthetic laps, 2 L/lap. The reader receives every 60 Hz tick;
    # the *real* LiveMonitor decides which frames become 2 Hz snapshots.
    distance = (tick + 150) / 600
    completed = math.floor(distance)
    return {
        "AirTemp": 20.0,
        "Brake": 0.0,
        "CarIdxLap": [completed + 1, completed + 1, 0],
        "CarIdxLapCompleted": [completed, completed, 0],
        "CarIdxLapDistPct": [distance % 1, 0.3, -1.0],
        "CarIdxOnPitRoad": [False, False, False],
        "CarIdxTrackSurface": [3, 3, -1],
        "CarLeftRight": 1,
        "FuelLevel": 40.0 - 2 * distance,
        "FuelLevelPct": 0.4,
        "Gear": 3,
        "IsOnTrack": in_car,
        "IsOnTrackCar": in_car,
        "IsReplayPlaying": False,
        "Lap": completed + 1,
        "LapCompleted": completed,
        "LapDistPct": distance % 1,
        "OnPitRoad": False,
        "PitstopActive": False,
        "PitsOpen": True,
        "PlayerCarIdx": 0,
        "PlayerCarInPitStall": False,
        "PlayerCarMyIncidentCount": 0,
        "PlayerTrackSurface": 3,
        "PlayerTireCompound": 0,
        "RPM": 4000.0,
        "SessionFlags": 4,
        "SessionLapsRemainEx": 32767,
        "SessionNum": 0,
        "SessionTick": tick,
        "SessionTime": tick / 60,
        "SessionTimeRemain": 600.0,
        "Speed": 40.0,
        "SteeringWheelAngle": 0.0,
        "Throttle": 0.5,
        "TireSetsUsed": 0,
        "TrackTemp": 29.0,
    }


def _descriptors():
    result = []
    offset = 0
    for name, value in _values(1).items():
        scalar = value[0] if isinstance(value, list) else value
        code = 1 if type(scalar) is bool else 2 if type(scalar) is int else 5
        count = len(value) if isinstance(value, list) else 1
        result.append(VariableDescriptor(
            name=name, type_code=code, dtype=SDK_TYPE_NAMES[code], offset=offset,
            count=count, count_as_time=False, unit="", description=name,
        ))
        offset += count * 8
    return tuple(result)


class FakeSdk:
    """Only the existing read-only transport protocol, with bound metadata."""

    def __init__(
        self, clock, stop, frame_count, *, stop_on_last=False,
        session_type="Practice", metadata_update=1, in_car=True, value_changes=None,
    ):
        self.clock = clock
        self.stop = stop
        self.frame_count = frame_count
        self.stop_on_last = stop_on_last
        self.session_type = session_type
        self.metadata_update = metadata_update
        self.in_car = in_car
        self.value_changes = value_changes or {}
        self.read_count = 0
        self.closed = False
        self.selected_fields = []

    def startup(self, wait_seconds):
        assert wait_seconds == 0
        return SimpleNamespace(tick_rate_hz=60)

    def descriptors(self):
        return _descriptors()

    @property
    def connected(self):
        return not self.closed and self.read_count < self.frame_count

    def read_frozen(self, fields):
        self.read_count += 1
        self.clock.now += 1 / 60
        values = _values(self.read_count, in_car=self.in_car)
        values.update(self.value_changes.get(self.read_count, {}))
        self.selected_fields.append(tuple(fields))
        frame = RawSdkFrame(
            buffer_tick=self.read_count, session_info_update=1,
            values={name: values[name] for name in fields}, sim_mode_raw="full",
            captured_monotonic_s=self.clock(),
        )
        if self.stop_on_last and self.read_count == self.frame_count:
            self.stop.stopped = True
        return frame

    def session_info_snapshot(self):
        return {
            "WeekendInfo": {"SimMode": "full"},
            "SessionInfo": {"Sessions": [{"SessionNum": 0, "SessionType": self.session_type}]},
            "DriverInfo": {"Drivers": [{"UserName": "SYNTHETIC PRIVATE PERSON"}]},
        }, self.metadata_update

    def sim_mode(self):
        return "full", 1

    def close(self):
        self.closed = True


def test_real_reader_pipeline_links_private_raw_capture_and_detector_replay(tmp_path):
    from iracing_ai_engineer.trial_audit import TrialAudit
    from iracing_ai_engineer.trial_replay import replay_trial

    clock, stop = Clock(), Stop()
    state = live_app.AppState(clock=clock)
    journal = TrialAudit(tmp_path / "trials", clock=clock)
    state.attach_trial(journal)
    sdk = FakeSdk(clock, stop, 12, stop_on_last=True,
                  value_changes={tick: {"CarLeftRight": 2} for tick in range(1, 13)})
    try:
        live_app.run_reader(state, stop, LiveFuelConfig(), transport_factory=lambda: sdk,
                            clock=clock, record_directory=tmp_path / "captures")
    finally:
        state.attach_trial(None)
        journal.close()
    assert sdk.closed
    [path] = (tmp_path / "trials").glob("*.jsonl")
    report = replay_trial(path, capture_directory=tmp_path / "captures")
    assert report["status"] == "REPLAY_MATCH" and report["frames"] == 12
    assert report["decisions"]["CANDIDATE"] == 1
    assert report["captures"] == {"OPEN": 1, "COMPLETE": 1}
    assert report["capture_byte_checks"] == {"MATCH": 1}
    assert report["heard"] is report["live_acceptance"] is False
    assert b"PRIVATE PERSON" not in path.read_bytes()


class PacedWorker(FrameWorker):
    """Synchronize synthetic clock advancement, still using the real worker.

    These numeric frame fixtures run faster than wall time. Async latency/fault
    tests separately use real Events and the unmodified production worker.
    """

    def submit(self, *args, **kwargs):
        result = super().submit(*args, **kwargs)
        assert self.wait_idle(3)
        return result

    def close(self, **kwargs):
        super().close(**kwargs)
        assert self.join(3)


@pytest.mark.parametrize("malformed", [False, True])
def test_raw_char_recording_and_failure_do_not_block_reader_analysis_or_proximity(
    tmp_path, malformed,
):
    from iracing_ai_engineer.capture_replay import replay_capture
    from iracing_ai_engineer.trial_audit import TrialAudit
    from iracing_ai_engineer.trial_replay import replay_trial

    octets = b"SYNTHETIC PRIVATE CHAR\x80\0\0"

    class CharSdk(FakeSdk):
        def descriptors(self):
            base = super().descriptors()
            offset = max(row.offset + row.count * SDK_TYPE_SIZES[row.type_code] for row in base)
            return (*base, VariableDescriptor("ExtraSdkText", 0, "char", offset,
                                              len(octets), False, "", "invented char field"))

    clock, stop = Clock(), Stop()
    state = ObservedState(clock)
    value = [bytes([item]) for item in octets]
    if malformed:
        value[-1] = b"too long"
    sdk = CharSdk(clock, stop, 12, stop_on_last=True, value_changes={
        tick: {"ExtraSdkText": value, "CarLeftRight": 2} for tick in range(1, 13)
    })
    journal = TrialAudit(tmp_path / "trials", clock=clock)
    state.attach_trial(journal)
    try:
        live_app.run_reader(state, stop, LiveFuelConfig(), transport_factory=lambda: sdk,
                            clock=clock, worker_factory=PacedWorker,
                            record_directory=tmp_path / "captures")
    finally:
        state.attach_trial(None)
        journal.close()
    assert sdk.closed and sdk.read_count == 12 and state.publications
    assert "PRIVATE CHAR" not in json.dumps(state.publications)
    [trial_path] = (tmp_path / "trials").glob("*.jsonl")
    trial = replay_trial(trial_path)
    assert trial["frames"] == 12 and trial["decisions"]["CANDIDATE"] == 1
    assert trial["live_acceptance"] is False
    assert b"PRIVATE CHAR" not in trial_path.read_bytes()
    [capture_path] = (tmp_path / "captures").glob("*.jsonl")
    rows = [json.loads(line) for line in capture_path.read_bytes().splitlines()]
    if malformed:
        assert state.snapshot()["recording"]["status"] == "ERROR"
        assert not any(row["record_type"] == "receipt" for row in rows)
    else:
        assert state.snapshot()["recording"]["status"] == "COMPLETE"
        frames = [row for row in rows if row["record_type"] == "frame"]
        assert len(frames) == 12
        assert all(row["values"]["ExtraSdkText"].encode("latin-1") == octets for row in frames)
        report = replay_capture(capture_path)
        assert report["frames"] == 12 and report["status"] == "RECOMPUTED"
        assert "PRIVATE CHAR" not in json.dumps(report)


class PollingSdk(FakeSdk):
    """Invented 60 Hz clock, immediate reads and 1.5 ms work; no SDK event model."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ticks = []

    def read_frozen(self, fields):
        self.read_count += 1
        tick = math.floor(self.clock() * 60 + 1e-8)
        self.ticks.append(tick)
        self.clock.now += .0015
        values = _values(tick)
        if self.stop_on_last and self.read_count == self.frame_count:
            self.stop.stopped = True
        return RawSdkFrame(
            buffer_tick=tick, session_info_update=1,
            values={name: values[name] for name in fields}, sim_mode_raw="full",
            captured_monotonic_s=self.clock(),
        )


def test_reader_pacing_with_invented_clock_exposes_coarse_timeout_gaps():
    def run(*, coarse):
        clock, stop = Clock(), Stop()
        clock.now = 0.0
        state = live_app.AppState(clock=clock)
        sdk = PollingSdk(clock, stop, 120, stop_on_last=True)
        requests = []

        def sleep(seconds):
            requests.append(seconds)
            clock.now += math.ceil(seconds / .015625) * .015625 if coarse else seconds

        live_app.run_reader(
            state, stop, LiveFuelConfig(), transport_factory=lambda: sdk,
            clock=clock, worker_factory=PacedWorker, sleeper=sleep,
        )
        assert sdk.closed and state.snapshot()["connection"] == "STOPPED"
        assert len(requests) == 119 and all(0 < value <= .01 for value in requests)
        assert stop.retries == [] and state.report()["live_acceptance"] is False
        return set(sdk.ticks)

    precise, coarse = run(coarse=False), run(coarse=True)
    assert precise == set(range(max(precise) + 1))
    assert coarse < set(range(max(coarse) + 1))


def test_reader_stop_during_short_sleep_closes_before_reading_another_frame():
    clock, stop = Clock(), Stop()
    clock.now = 0.0
    state = live_app.AppState(clock=clock)
    sdk = PollingSdk(clock, stop, 100)
    requests = []

    def sleep(seconds):
        requests.append(seconds)
        stop.stopped = True

    live_app.run_reader(
        state, stop, LiveFuelConfig(), transport_factory=lambda: sdk,
        clock=clock, worker_factory=PacedWorker, sleeper=sleep,
    )
    assert requests == [pytest.approx(.0085)]
    assert sdk.read_count == 1 and sdk.closed
    assert state.snapshot()["connection"] == "STOPPED" and stop.retries == []


def _run(specs, *, record_directory=None, record_max_bytes=32 * MIB):
    clock, stop = Clock(), Stop()
    state = ObservedState(clock)
    transports = [
        FakeSdk(clock, stop, **spec, stop_on_last=index == len(specs) - 1)
        for index, spec in enumerate(specs)
    ]
    pending = iter(transports)
    live_app.run_reader(
        state, stop, LiveFuelConfig(minimum_valid_laps=2),
        transport_factory=lambda: next(pending), clock=clock,
        record_directory=record_directory, record_max_bytes=record_max_bytes,
        worker_factory=PacedWorker,
    )
    assert all(transport.closed for transport in transports)
    assert state.snapshot()["connection"] == "STOPPED"
    return state, transports, stop


def test_real_monitor_to_fuel_learns_from_every_tick_and_emits_only_low_rate_state():
    state, [transport], stop = _run([{"frame_count": 1800}])
    snapshots = state.publications
    assert 50 <= len(snapshots) <= 61
    assert transport.read_count == 1800
    assert stop.retries == []
    # The first raw frame cannot yet establish continuity; no estimate is
    # allowed until the next real monitor interval confirms a progressing SDK.
    assert snapshots[0]["fuel"]["estimated_laps_remaining"] is None
    assert snapshots[1]["fuel"]["status"] == "LEARNING"
    ready = [entry for entry in snapshots if entry["fuel"]["status"] == "READY"]
    assert ready
    result = ready[-1]["fuel"]
    assert result["valid_laps"] == 2
    assert result["conservative_burn_l_per_lap"] == pytest.approx(2)
    assert result["estimated_laps_remaining"] == 15
    assert result["fuel_needed_to_finish_l"] is None
    assert result["estimate_only"] is result["advisor_only"] is True
    assert result["executable"] is False
    assert snapshots[0]["monitor"]["quality"]["dropped_ticks"] is None
    assert all(entry["monitor"]["quality"]["dropped_ticks"] == 0 for entry in snapshots[1:])
    assert [entry["monitor"]["sequence"] for entry in snapshots] == list(range(len(snapshots)))
    assert "SYNTHETIC PRIVATE PERSON" not in json.dumps(snapshots)
    assert state.report()["live_acceptance"] is False


def test_bad_raw_tick_between_display_snapshots_cannot_become_a_clean_fuel_lap():
    state, _, _ = _run([{
        "frame_count": 1800, "value_changes": {500: {"PlayerTrackSurface": 0}},
    }])
    polluted = [row for row in state.publications if row["monitor"]["interval_invalid_for_fuel"]]
    assert polluted
    assert any(
        row["monitor"]["telemetry"]["player_track_surface"] == 3
        and "OFF_TRACK_OR_UNKNOWN_SURFACE" in row["monitor"]["interval_invalid_for_fuel"]
        for row in polluted
    )
    assert any(row["fuel"]["reason_codes"] == ["INTERVAL_NOT_FUEL_ELIGIBLE"] for row in polluted)
    assert not any(row["fuel"]["status"] == "READY" for row in state.publications)
    assert state.publications[-1]["fuel"]["valid_laps"] == 1


def test_disconnect_removes_estimates_and_fresh_connection_does_not_reuse_learned_laps():
    state, transports, stop = _run([{"frame_count": 1800}, {"frame_count": 61}])
    first_generation = state.publications[0]["generation"]
    before = [row for row in state.publications if row["generation"] == first_generation]
    after = [row for row in state.publications if row["generation"] != first_generation]
    assert any(row["fuel"]["status"] == "READY" for row in before)
    assert after and all(row["fuel"]["valid_laps"] == 0 for row in after)
    assert all(row["fuel"]["estimated_laps_remaining"] is None for row in after)
    assert after[0]["monitor"]["sequence"] == 0
    assert before[0]["monitor"]["binding_sha256"] != after[0]["monitor"]["binding_sha256"]
    disconnected = [
        row for row in state.transitions if row["requested_connection"] == "DISCONNECTED"
    ]
    assert len(disconnected) == 1
    assert all(disconnected[0][field] is None for field in ("monitor", "fuel", "speech"))
    assert stop.retries == [5]
    assert len(transports) == 2


@pytest.mark.parametrize("metadata_update", [1, 2])
def test_session_type_comes_from_exact_bound_metadata_not_stale_previous_session(metadata_update):
    state, _, _ = _run([{
        "frame_count": 31, "session_type": "Race", "metadata_update": metadata_update,
    }])
    expected = "Race" if metadata_update == 1 else None
    assert all(row["session_type"] == expected for row in state.publications)
    assert all(row["speech"] is None for row in state.publications)


@pytest.mark.parametrize("error_type", [OSError, CollectorConsistencyError])
def test_recording_constructor_failure_is_disabled_once_but_monitor_survives_reconnect(
    tmp_path, monkeypatch, error_type
):
    attempts = []

    def broken_recorder(*args, **kwargs):
        attempts.append((args, kwargs))
        raise error_type("SYNTHETIC PRIVATE ERROR")

    monkeypatch.setattr(live_app_recording, "AppRecorder", broken_recorder)
    state, _, stop = _run(
        [{"frame_count": 61}, {"frame_count": 61}], record_directory=tmp_path
    )
    assert len(attempts) == 1
    assert len(state.publications) >= 4
    assert state.snapshot()["recording"] == {"status": "ERROR", "bytes": 0}
    assert stop.retries == [5]
    assert "SYNTHETIC PRIVATE ERROR" not in json.dumps(state.publications)


@pytest.mark.parametrize("error_type", [OSError, CollectorConsistencyError])
def test_recording_ingest_failure_preserves_count_and_never_recreates_recorder(
    tmp_path, monkeypatch, error_type
):
    recorders = []

    class BrokenRecorder:
        def __init__(self, *_args, **_kwargs):
            self.byte_count = 0
            self.closed = False
            self.finish_called = False
            recorders.append(self)

        def ingest(self, sample):
            assert isinstance(sample, CollectorSample)
            self.byte_count = 13
            raise error_type("SYNTHETIC PRIVATE WRITE ERROR")

        def finish(self):
            self.finish_called = True

        def close(self):
            self.closed = True

    monkeypatch.setattr(live_app_recording, "AppRecorder", BrokenRecorder)
    state, _, stop = _run(
        [{"frame_count": 61}, {"frame_count": 61}], record_directory=tmp_path
    )
    assert len(recorders) == 1
    assert recorders[0].closed and not recorders[0].finish_called
    assert len(state.publications) >= 4
    assert state.snapshot()["recording"] == {"status": "ERROR", "bytes": 13}
    assert stop.retries == [5]


def test_total_recording_budget_survives_reconnect_and_limit_keeps_monitor_running(
    tmp_path, monkeypatch
):
    recorders = []

    class BudgetRecorder:
        def __init__(self, directory, *, source_id, session_id, max_bytes):
            assert directory == tmp_path
            assert source_id and session_id
            self.max_bytes = max_bytes
            self.byte_count = 0
            self.ingests = 0
            self.finished = False
            self.closed = False
            recorders.append(self)

        def ingest(self, sample):
            assert isinstance(sample, CollectorSample)
            self.ingests += 1
            self.byte_count += 4 * MIB
            assert self.byte_count <= self.max_bytes

        def finish(self):
            self.finished = True
            self.byte_count += 128
            assert self.byte_count <= self.max_bytes
            return {"completion_status": "COMPLETE"}

        def close(self):
            self.closed = True

    monkeypatch.setattr(live_app_recording, "AppRecorder", BudgetRecorder)
    state, transports, stop = _run(
        [{"frame_count": 1}, {"frame_count": 61}, {"frame_count": 61}],
        record_directory=tmp_path, record_max_bytes=32 * MIB,
    )
    assert len(recorders) == 2  # Third connection must not restart a fresh budget.
    assert [item.max_bytes for item in recorders] == [32 * MIB, 28 * MIB]
    assert [item.ingests for item in recorders] == [1, 3]
    assert not recorders[0].finished and recorders[1].finished
    assert all(item.closed for item in recorders)
    expected_bytes = 16 * MIB + 128
    assert state.snapshot()["recording"] == {"status": "LIMIT_REACHED", "bytes": expected_bytes}
    assert state.report()["recording_bytes"] == expected_bytes
    assert sum(item.read_count for item in transports) == 123
    assert len(state.publications) >= 5
    assert stop.retries == [5, 5]


def test_real_recorder_disconnect_keeps_prefix_and_only_orderly_stop_finishes(tmp_path):
    state, _, stop = _run(
        [{"frame_count": 2}, {"frame_count": 2}], record_directory=tmp_path
    )
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 2
    clips = [[json.loads(line) for line in path.read_bytes().splitlines()] for path in files]
    complete = [clip for clip in clips if clip[-1]["record_type"] == "collector_receipt"]
    incomplete = [clip for clip in clips if clip[-1]["record_type"] != "collector_receipt"]
    assert len(complete) == len(incomplete) == 1
    assert complete[0][-1]["receipt"]["frame_record_count"] == 2
    assert incomplete[0][-1]["record_type"] == "frame"
    assert all(sum(row["record_type"] == "frame" for row in clip) == 2 for clip in clips)
    assert state.report()["recording_bytes"] == sum(path.stat().st_size for path in files)
    assert "SYNTHETIC PRIVATE PERSON" not in "".join(path.read_text() for path in files)
    assert stop.retries == [5]


def test_out_of_car_never_learns_or_speaks_in_the_real_monitor_chain():
    state, _, _ = _run([{"frame_count": 31, "in_car": False}])
    assert state.publications
    for row in state.publications:
        assert row["monitor"]["status"] == "WAIT_CAR"
        assert row["fuel"]["status"] == "WAIT_CAR"
        assert row["fuel"]["valid_laps"] == 0
        assert row["speech"] is None


def test_spotter_captures_a_short_pass_between_two_slow_display_snapshots():
    state, _, _ = _run([{
        "frame_count": 31,
        "value_changes": {tick: {"CarLeftRight": 2} for tick in (10, 11, 12)},
    }])
    assert all(row["monitor"]["telemetry"]["car_left_right"] == 1
               for row in state.publications)
    assert all(row["fuel"]["status"] != "READY" for row in state.publications)
    candidates = [row for row in state.spotter_audit() if row["decision"] == "CANDIDATE"]
    assert [row["kind"] for row in candidates] == ["CAR_LEFT", "ALL_CLEAR"]
    assert [row["tick"] for row in candidates] == [12, 22]
    assert all(row["audible"] is False for row in candidates)
    assert state.snapshot()["spotter"]["candidate"] is None


def test_spotter_fault_is_latched_locally_without_stopping_fuel_analysis(monkeypatch):
    attempts = []

    def broken(*_args, **_kwargs):
        attempts.append(1)
        raise ValueError("SYNTHETIC PRIVATE ERROR")

    monkeypatch.setattr(live_app.ProximitySpotter, "feed", broken)
    state, _, _ = _run([{"frame_count": 1800}])
    assert len(attempts) == 1
    assert any(row["fuel"]["status"] == "READY" for row in state.publications)
    assert all(row["spotter"]["status"] == "ERROR" for row in state.publications)
    assert "SYNTHETIC PRIVATE ERROR" not in json.dumps(state.report())
    assert "SYNTHETIC PRIVATE ERROR" not in json.dumps(state.spotter_audit())


@pytest.mark.parametrize("fail_ingest", [True, False])
def test_recorder_close_error_cannot_kill_reader_or_skip_sdk_teardown(
    tmp_path, monkeypatch, fail_ingest
):
    instances = []

    class CloseErrorRecorder:
        def __init__(self, *_args, **_kwargs):
            self.byte_count = 0
            instances.append(self)

        def ingest(self, sample):
            self.byte_count += 10
            if fail_ingest:
                raise OSError("synthetic write failure")

        def finish(self):
            raise OSError("synthetic finalization failure")

        def close(self):
            raise OSError("synthetic close failure")

    monkeypatch.setattr(live_app_recording, "AppRecorder", CloseErrorRecorder)
    state, transports, stop = _run(
        [{"frame_count": 61}, {"frame_count": 61}], record_directory=tmp_path
    )
    assert len(instances) == 1
    assert all(transport.closed for transport in transports)
    assert len(state.publications) >= 4
    assert state.snapshot()["recording"]["status"] == "ERROR"
    assert stop.retries == [5]


def _eventually(predicate):
    deadline = time.perf_counter() + 3
    while not predicate():
        if time.perf_counter() > deadline:
            raise AssertionError("SYNTHETIC_THREAD_TIMEOUT")
        time.sleep(0.005)


class GatedSdk(FakeSdk):
    """Admit the first worker call, then race synthetic ticks past that blocker."""

    def __init__(self, clock, stop, blocked):
        super().__init__(clock, stop, 151, stop_on_last=True,
                         value_changes={tick: {"CarLeftRight": 2} for tick in range(10, 31)})
        self.blocked = blocked
        self.at_tail, self.finish = threading.Event(), threading.Event()

    def read_frozen(self, fields):
        if self.read_count == 1:
            assert self.blocked.wait(3)
        if self.read_count == 150:
            self.at_tail.set()
            assert self.finish.wait(3)
        return super().read_frozen(fields)


@pytest.mark.parametrize("phase", ["startup", "ingest", "close"])
def test_blocked_recording_lane_keeps_sdk_proximity_and_fuel_running(
    tmp_path, monkeypatch, phase,
):
    blocked, release = threading.Event(), threading.Event()
    instances = []

    class BlockingRecorder:
        def __init__(self, *_args, **_kwargs):
            self.byte_count = 0
            self.finished = self.closed = False
            instances.append(self)
            if phase == "startup":
                blocked.set()
                assert release.wait(5)

        def ingest(self, _sample):
            if phase == "ingest":
                blocked.set()
                assert release.wait(5)
            if phase == "close":
                raise OSError("SYNTHETIC PRIVATE WRITE FAILURE")
            self.byte_count += 10

        def finish(self):
            self.finished = True

        def close(self):
            if phase == "close":
                blocked.set()
                assert release.wait(5)
            self.closed = True

    monkeypatch.setattr(live_app_recording, "AppRecorder", BlockingRecorder)
    clock, stop = Clock(), Stop()
    state = ObservedState(clock)
    sdk = GatedSdk(clock, stop, blocked)

    def factory(*args, **kwargs):
        cls = PacedWorker if kwargs["name"] == "live-analysis" else FrameWorker
        return cls(*args, **kwargs)

    reader = threading.Thread(target=live_app.run_reader, args=(state, stop, LiveFuelConfig()),
                              kwargs={"transport_factory": lambda: sdk, "clock": clock,
                                      "record_directory": tmp_path, "worker_factory": factory})
    reader.start()
    try:
        assert sdk.at_tail.wait(3)
        assert sdk.read_count == 150 and state.spotter_snapshot()["connection"] == "CONNECTED"
        assert len(state.publications) >= 4
        candidates = [row["kind"] for row in state.spotter_audit()
                      if row["decision"] == "CANDIDATE"]
        assert candidates == ["CAR_LEFT", "ALL_CLEAR"]
        health = state.snapshot()["workers"]["recording"]
        assert health["status"] == "ERROR" and not health["done"]
        assert health["reason"] == ("PROCESSING_FAILED" if phase == "close" else "QUEUE_OVERFLOW")
        assert health["buffered_bytes"] <= health["max_bytes"]
        assert health["buffered_frames"] <= health["max_frames"]
        assert 0 < health["peak_buffered_bytes"] <= health["max_bytes"]
        assert 0 < health["peak_buffered_frames"] <= health["max_frames"]
        sdk.finish.set()
        _eventually(lambda: sdk.closed)
        assert reader.is_alive()  # SDK closed, still truthfully waiting for the sink.
    finally:
        sdk.finish.set()
        release.set()
        reader.join(3)
    assert not reader.is_alive() and instances[0].closed and not instances[0].finished
    assert len(instances) == 1


@pytest.mark.parametrize("release_before_stop", [False, True])
def test_blocked_analysis_expires_locally_without_stopping_proximity_or_late_publication(
    monkeypatch, release_before_stop,
):
    blocked, release = threading.Event(), threading.Event()
    original = live_app.LiveMonitor.feed

    def blocked_feed(self, *args, **kwargs):
        blocked.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(live_app.LiveMonitor, "feed", blocked_feed)
    clock, stop = Clock(), Stop()
    state = live_app.AppState(clock=clock)
    sdk = GatedSdk(clock, stop, blocked)
    reader = threading.Thread(target=live_app.run_reader, args=(state, stop, LiveFuelConfig()),
                              kwargs={"transport_factory": lambda: sdk, "clock": clock})
    reader.start()
    try:
        assert sdk.at_tail.wait(3)
        assert sdk.read_count == 150
        health = state.snapshot()["workers"]["analysis"]
        assert health["status"] == "ERROR" and health["reason"] == "QUEUE_STALE"
        assert state.spotter_snapshot()["connection"] == "CONNECTED"
        assert state.spotter_snapshot()["spotter"]["status"] == "READY"
        assert state.snapshot()["fuel"] is None
        if release_before_stop:
            release.set()
            _eventually(lambda: state.snapshot()["workers"]["analysis"]["done"])
            assert state.spotter_snapshot()["connection"] == "CONNECTED"
            assert state.snapshot()["monitor"] is None  # Same-generation fault stays withdrawn.
        sdk.finish.set()
        _eventually(lambda: sdk.closed)
        if not release_before_stop:
            assert reader.is_alive()
    finally:
        sdk.finish.set()
        release.set()
        reader.join(3)
    assert not reader.is_alive()
    assert state.snapshot()["fuel"] is None and state.snapshot()["monitor"] is None
    assert state.snapshot()["connection"] == "STOPPED"


@pytest.mark.parametrize("recover", [False, True])
def test_reconnect_does_not_wait_for_or_duplicate_old_analysis_owner(monkeypatch, recover):
    blocked, release = threading.Event(), threading.Event()
    original = live_app.LiveMonitor.feed
    feeds = []

    def blocked_feed(self, *args, **kwargs):
        feeds.append(1)
        blocked.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(live_app.LiveMonitor, "feed", blocked_feed)
    clock, stop = Clock(), Stop()
    state = ObservedState(clock)
    first = FakeSdk(clock, stop, 1)
    second = GatedSdk(clock, stop, blocked)
    transports, workers = iter((first, second)), []

    def factory(*args, **kwargs):
        cls = PacedWorker if workers else FrameWorker
        worker = cls(*args, **kwargs)
        workers.append(worker)
        return worker

    # Make the initial frame reach its owner before the next synthetic connection.
    original_close = first.close

    def first_close():
        assert blocked.wait(3)
        original_close()

    first.close = first_close
    reader = threading.Thread(target=live_app.run_reader, args=(state, stop, LiveFuelConfig()),
                              kwargs={"transport_factory": lambda: next(transports),
                                      "clock": clock, "worker_factory": factory})
    reader.start()
    try:
        assert second.at_tail.wait(3)
        assert first.closed and second.read_count == 150 and len(workers) == 1
        assert state.spotter_snapshot()["connection"] == "CONNECTED"
        assert state.spotter_snapshot()["spotter"]["status"] == "READY"
        assert state.snapshot()["workers"]["analysis"]["status"] == "WAIT_PREVIOUS"
        release.set()
        assert workers[0].join(3)
        assert state.snapshot()["monitor"] is None  # The old generation cannot republish.
        if recover:
            # Continue beyond the tail gate so the reader sees the old owner's
            # actual exit and starts precisely one fresh model on this session.
            second.frame_count = 182
    finally:
        release.set()
        second.finish.set()
        reader.join(3)
    assert not reader.is_alive() and second.closed
    assert stop.retries == [5]
    assert len(workers) == (2 if recover else 1)
    if recover:
        current = [row for row in state.publications if row["monitor"] is not None]
        assert current and all(row["generation"] == state.generation - 1 for row in current)
        assert all(row["fuel"]["valid_laps"] == 0 for row in current)
    else:
        assert feeds == [1]


def test_failed_sdk_release_is_not_reported_as_clean_stop_or_restarted():
    clock, stop = Clock(), Stop()
    state = live_app.AppState(clock=clock)
    sdk = FakeSdk(clock, stop, 10, stop_on_last=True)
    opened = []

    def factory():
        opened.append(1)
        return sdk

    def broken_close():
        raise OSError("SYNTHETIC PRIVATE SDK CLOSE")

    sdk.close = broken_close
    live_app.run_reader(state, stop, LiveFuelConfig(), transport_factory=factory, clock=clock,
                        worker_factory=PacedWorker)
    assert opened == [1] and stop.retries == []
    assert state.snapshot()["connection"] == "ERROR"
    assert state.spotter_snapshot()["spotter"]["status"] == "ERROR"
    assert "PRIVATE" not in str(state.report())
