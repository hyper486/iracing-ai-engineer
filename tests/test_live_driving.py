"""Invented laps only; SDK_LIVE tags here do not establish live acceptance."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from iracing_ai_engineer import live_app, live_driving
from iracing_ai_engineer.laps import segment_laps
from iracing_ai_engineer.live_driving import (
    COLUMNS,
    LiveDrivingEngineer,
    _CornerModel,
    _LapJob,
    validated_driving,
)
from iracing_ai_engineer.live_fuel import LiveFuelConfig
from iracing_ai_engineer.live_monitor import LiveMonitor
from iracing_ai_engineer.live_queries import live_query_intent, render_live_query
from iracing_ai_engineer.llm_engineer import EngineerConfig, EngineerService
from iracing_ai_engineer.llm_evidence import build_live_context


def fixtures(name):
    identifier = f"_coaching_{name}_fixtures"
    if identifier in sys.modules:
        return sys.modules[identifier]
    spec = importlib.util.spec_from_file_location(identifier,
                                                Path(__file__).with_name(f"test_{name}.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[identifier] = module
    spec.loader.exec_module(module)
    return module


def rows(kinds, rate=20):
    """Uniform SDK cadence; first and last laps are partial capture guards."""
    output, elapsed, tick = [], 0.0, 100
    for ordinal, kind in enumerate(kinds, 1):
        lap = fixtures("driving")._lap_channels(kind)
        duration = np.ceil(lap["SessionTime"][-1] * rate) / rate
        times = np.arange(round(duration * rate)) / rate
        scaled = times * lap["SessionTime"][-1] / duration
        channels = {name: np.interp(scaled, lap["SessionTime"], values)
                    for name, values in lap.items() if name != "SessionTime"}
        channels.update({
            "SessionTime": times + elapsed, "SessionTick": np.arange(len(times)) + tick,
            "Lap": np.full(len(times), ordinal), "LapCompleted": np.full(len(times), ordinal - 1),
        })
        defaults = {
            "OnPitRoad": 0, "PlayerTrackSurface": 3, "PlayerCarMyIncidentCount": 0,
            "FuelLevel": 70, "FuelLevelPct": .7, "PlayerTireCompound": 0, "TireSetsUsed": 0,
            "TrackTempCrew": 29, "AirTemp": 20, "WindVel": 1, "WindDir": 0, "Precipitation": 0,
            "CarLeftRight": 1, "SessionFlags": 0, "PlayerCarInPitStall": 0,
            "PitstopActive": 0, "SeparationM": 400,
        }
        for key, value in defaults.items():
            channels[key] = np.full(len(times), value, dtype=float)
        channels["FuelLevel"] -= .2 * (ordinal + channels["LapDistPct"])
        channels["FuelLevelPct"] = channels["FuelLevel"] / 100
        output.append(np.column_stack([channels[key] for key in COLUMNS]))
        elapsed += duration
        tick += len(times)
    # Keep only 0.5 s of the final lap, enough for delayed counter binding.
    return np.concatenate([*output[:-1], output[-1][:rate // 2]])


def jobs(matrix, epoch=0):
    boundaries = np.flatnonzero(np.diff(matrix[:, 4]) < -.9) + 1
    return [_LapJob(epoch, index + 1, 1_200_000, int(matrix[end, 3]),
                    matrix[max(0, start - 8):end + 8].tobytes())
            for index, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:], strict=True))]


class Owner:
    def __init__(self):
        self.epoch, self.result = 0, None

    def current(self, epoch):
        return epoch == self.epoch

    def complete(self, job, result):
        if self.current(job.epoch):
            self.result = result


def projection(result):
    value = fixtures("live_queries").snapshot()
    value["monitor"]["telemetry"].update(laps_completed=result["completed_laps"], player_car_idx=0)
    value["driving"] = {
        "contract_version": live_driving.LIVE_DRIVING_CONTRACT,
        "advisor_only": True, "executable": False, "live_acceptance": False,
        "source_kind": "SDK_LIVE", "binding_sha256": "a" * 64, "epoch": 0, "revision": 6,
        "track_length_mm": 1_200_000, "session_num": 0, "player_car_idx": 0,
        "claim_level": "descriptive", "causal_gain_verified": False,
        "worker": {"status": "RUNNING", "failed": False}, **result,
    }
    return value


def model_result(kind="long_coast"):
    owner = Owner()
    model = _CornerModel(owner, 20)
    data = rows(["baseline"] * 4 + [kind] * 2 + ["baseline"])
    for job in jobs(data):
        model.process(job)
    return owner.result


@pytest.mark.parametrize("kind,diagnosis", [
    ("long_coast", "LONG_COAST"), ("late_brake", "LATE_BRAKING_HURTS_EXIT"),
    ("second_lift", "THROTTLE_SECOND_LIFT"),
])
def test_uniform_complete_laps_produce_existing_repeated_patterns(kind, diagnosis):
    result = model_result(kind)
    assert result["status"] == "READY", result
    assert result["point"]["diagnosis"] == diagnosis
    assert result["point"]["evidence_laps"] == [5, 6]
    assert result["reference_lap"] in (2, 3, 4)
    assert result["eligible_laps"] == result["retained_laps"] == 5
    assert result["retained_trace_bytes"] == 5 * 601 * 6 * 8
    value = projection(result)
    assert validated_driving(value) == value["driving"]
    context = build_live_context(value)
    assert context["capabilities"]["driving"] == "RECENT_LAP_OBSERVATION_ONLY"
    assert "DRIVING_UNAVAILABLE" not in {item["id"] for item in context["notices"]}
    answer = render_live_query(context, "driving")
    assert "练习假设" in answer["spoken_text"] and "不保证提速" in answer["spoken_text"]
    assert len(answer["spoken_text"]) < 100 and len(answer["text"]) < 320
    assert "中位时间损失" in answer["text"]


def test_one_off_and_latest_improvement_do_not_repeat_an_old_recommendation():
    owner = Owner()
    model = _CornerModel(owner, 20)
    data = rows(["baseline"] * 4 + ["long_coast"] * 2 + ["baseline"] * 2)
    values = []
    for job in jobs(data):
        model.process(job)
        values.append(copy.deepcopy(owner.result))
    assert values[-3]["status"] == "NO_REPEAT"
    assert values[-2]["status"] == "READY"
    assert values[-1]["status"] == "NO_REPEAT"
    assert values[-1]["point"] is None


@pytest.mark.parametrize("channel,value,reason", [
    ("CarLeftRight", 2, "TRAFFIC_AFFECTED_LAP"),
    ("SeparationM", 40, "TRAFFIC_AFFECTED_LAP"),
    ("Precipitation", .2, "WEATHER_CHANGED_OR_WET"),
    ("SessionFlags", 8, "FLAGS_OR_PIT_INTERVAL"),
    ("PlayerCarInPitStall", 1, "FLAGS_OR_PIT_INTERVAL"),
])
def test_latest_contaminated_lap_withholds_prior_points(channel, value, reason):
    owner, data = Owner(), rows(["baseline"] * 4 + ["long_coast"] * 2 + ["baseline"])
    model = _CornerModel(owner, 20)
    data[data[:, 2] == 6, COLUMNS.index(channel)] = value
    for job in jobs(data):
        model.process(job)
    assert owner.result["status"] == "LAP_REJECTED"
    assert owner.result["reason"] == reason
    assert owner.result["point"] is None


def frames(matrix):
    integer = {"SessionTick", "Lap", "LapCompleted", "PlayerTrackSurface",
               "PlayerCarMyIncidentCount", "PlayerTireCompound", "TireSetsUsed",
               "CarLeftRight", "SessionFlags"}
    boolean = {"OnPitRoad", "PlayerCarInPitStall", "PitstopActive"}
    for row in matrix:
        values = {name: int(value) if name in integer else bool(value) if name in boolean
                  else float(value) for name, value in zip(COLUMNS, row, strict=True)
                  if name != "SeparationM"}
        fraction = values["LapDistPct"]
        values.update(CarIdxLapDistPct=[fraction, (fraction + .4) % 1, -1.0],
                      CarIdxLap=[values["Lap"], values["Lap"], 0],
                      CarIdxLapCompleted=[values["LapCompleted"], values["LapCompleted"], 0],
                      UserName="SYNTHETIC_PRIVATE_MARKER")
        frame = fixtures("live_monitor")._clean_fuel_frame(values["SessionTick"], **values)
        yield replace(frame, captured_monotonic_s=values["SessionTime"] + 1)


@pytest.mark.parametrize("session_type", ["Practice", "Offline Testing"])
def test_sdk_shaped_frames_reach_native_query_without_provider_or_fuel_dependency(session_type):
    # Genuine production owners; invented 20 Hz SDK frames, no simulator access.
    now = [1.0]
    state = live_app.AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    state.start_spotter(20)
    analysis = live_app._LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic",
                                    tick_rate=20, car_count=3, generation=state.generation,
                                    allowed=lambda: True)
    try:
        data = rows(["baseline"] * 4 + ["long_coast"] * 2 + ["baseline"])
        if session_type == "Offline Testing":
            data[:, COLUMNS.index("SessionFlags")] = 0x10000200
        for frame in frames(data):
            if session_type == "Offline Testing":
                frame = replace(frame, values={**frame.values, "SessionState": 4})
            now[0] = frame.values["SessionTime"] + 1
            state.feed_spotter(frame)
            analysis.process((frame, session_type, now[0], 1_200_000))
            # Fast replay cannot overflow a wall-clock lap worker; this wait is
            # test-only, never inside the SDK producer or production analysis.
            if frame.values["LapDistPct"] < .04:
                assert analysis._driving._worker.wait_idle(5)
        assert analysis._driving._worker.wait_idle(5)
        last = state.snapshot()
        # Publish one final snapshot as the production 2 Hz owner would do.
        monitor = analysis._monitor.snapshot()
        state.publish(monitor, last["fuel"], None, session_type,
                      driving=analysis._driving.snapshot(monitor))
        snapshot = state.snapshot()
        assert snapshot["driving"]["status"] == "READY", snapshot["driving"]
        assert validated_driving(snapshot) is not None
        assert snapshot["spotter"]["status"] == "READY"
        snapshot["monitor"]["interval_invalid_for_fuel"] = ["MISSING_FUEL_DATA"]
        snapshot["fuel"] = {"status": "BLOCKED"}
        service = EngineerService(lambda: snapshot, EngineerConfig(provider="off"),
                                  clock=lambda: now[0], environ={})
        try:
            assert service.submit("哪里可以改进？")[1]["route"] == "local_live"
            answer = service.snapshot()["answer"]
            assert answer["stale"] is False and answer["topic"] == "driving"
            assert "练习假设" in answer["spoken_text"]
            assert service.snapshot()["requests_used"] == 0
            assert "SYNTHETIC_PRIVATE" not in json.dumps(build_live_context(snapshot))
        finally:
            service.close(wait=True)
    finally:
        analysis.close()


@pytest.mark.parametrize("sparse", [False, True])
def test_live_collector_uses_existing_whole_lap_coverage_gate(sparse):
    data = rows(["baseline"] * 4 + ["long_coast"] * 2 + ["baseline"], rate=60)
    keep = np.ones(len(data), dtype=bool)
    for lap in range(1, 7):
        indices = np.flatnonzero(data[:, 2] == lap)
        # One missing tick per lap is acceptable only if the unchanged offline
        # gate admits it. Sustained 96% coverage must still reject the lap.
        keep[indices[20:-20:25] if sparse else indices[len(indices) // 2]] = False
    data = data[keep]
    channels = {name: data[:, index] for index, name in enumerate(COLUMNS)}
    complete = [lap for lap in segment_laps(channels, 60) if lap.structurally_complete]
    assert len(complete) == 5
    assert all(lap.clean_for_driving is (not sparse) for lap in complete)
    assert all(lap.missing_ticks > 0 for lap in complete)
    monitor = LiveMonitor(source_id="synthetic", session_id="synthetic", sdk_tick_rate_hz=60)
    collector = LiveDrivingEngineer(60)
    try:
        for frame in frames(data):
            monitor.feed(frame)
            collector.feed(frame, monitor.latest_sample, 1_200_000)
            if frame.values["LapDistPct"] < .04:
                assert collector._worker.wait_idle(5)
        assert collector._worker.wait_idle(5)
        result = collector.snapshot(monitor.snapshot())
        assert result["worker"]["processed_frames"] == 5
        if sparse:
            assert result["status"] == "LAP_REJECTED", result
            assert result["reason"] == "LAP_TICK_COVERAGE_LOW"
            assert result["retained_laps"] == 0 and result["point"] is None
            assert "99.9%" in live_driving.driving_notice({"driving": result})
        else:
            assert result["status"] == "READY", result
            assert result["retained_laps"] == 5
            assert result["point"]["diagnosis"] == "LONG_COAST"
            assert result["point"]["evidence_laps"] == [5, 6]
        assert result["live_acceptance"] is False
    finally:
        collector.close()


@pytest.mark.parametrize("clock_gap", [False, True])
def test_collector_still_resets_on_gaps_above_existing_driving_limit(clock_gap):
    data = rows(["baseline"] * 3, rate=60)
    boundary = np.flatnonzero(np.diff(data[:, 4]) < -.9)[0] + 1
    sequence = list(frames(data[boundary - 8:boundary + 12]))
    monitor = LiveMonitor(source_id="synthetic", session_id="synthetic", sdk_tick_rate_hz=60)
    collector = LiveDrivingEngineer(60)
    try:
        for frame in sequence[:-1]:
            monitor.feed(frame)
            collector.feed(frame, monitor.latest_sample, 1_200_000)
        before = collector.snapshot(monitor.snapshot())
        assert before["buffered_rows"] > 0
        frame = sequence[-1]
        if clock_gap:
            frame = replace(frame, values={**frame.values,
                                           "SessionTime": frame.values["SessionTime"] + .15})
        else:
            frame = replace(frame, buffer_tick=frame.buffer_tick + 10,
                            values={**frame.values,
                                    "SessionTick": frame.values["SessionTick"] + 10})
        monitor.feed(frame)
        collector.feed(frame, monitor.latest_sample, 1_200_000)
        after = collector.snapshot(monitor.snapshot())
        assert after["epoch"] > before["epoch"]
        assert after["buffered_rows"] == 0 and after["point"] is None
    finally:
        collector.close()


@pytest.mark.parametrize("change", ["race", "state", "read_error"])
def test_offline_lap_context_change_discards_partial_coaching_buffer(change):
    data = rows(["baseline"] * 3, rate=60)
    boundary = np.flatnonzero(np.diff(data[:, 4]) < -.9)[0] + 1
    sequence = list(frames(data[boundary - 8:boundary + 12]))
    monitor = LiveMonitor(source_id="synthetic", session_id="synthetic", sdk_tick_rate_hz=60)
    collector = LiveDrivingEngineer(60)
    try:
        for frame in sequence[:-1]:
            frame = replace(frame, values={**frame.values, "SessionState": 4,
                                            "SessionFlags": 0x200})
            monitor.feed(frame, session_type="Offline Testing")
            collector.feed(frame, monitor.latest_sample, 1_200_000,
                           session_type="Offline Testing")
        before = collector.snapshot(monitor.snapshot())
        assert before["buffered_rows"] > 0
        frame = replace(sequence[-1], values={**sequence[-1].values, "SessionState": 4,
                                               "SessionFlags": 0x200})
        session_type = "Race" if change == "race" else "Offline Testing"
        if change == "state":
            frame = replace(frame, values={**frame.values, "SessionState": 3})
        elif change == "read_error":
            frame = replace(frame, read_errors=("SessionState",))
        monitor.feed(frame, session_type=session_type)
        collector.feed(frame, monitor.latest_sample, 1_200_000, session_type=session_type)
        after = collector.snapshot(monitor.snapshot())
        assert after["epoch"] > before["epoch"]
        assert after["buffered_rows"] == 0 and after["point"] is None
    finally:
        collector.close()


@pytest.mark.parametrize("query", ["哪里可以改进？", "哪里丢时间", "我该练什么", "driving advice"])
def test_exact_driving_question_routes_locally(query):
    assert live_query_intent(query) == "driving"


def test_cached_model_epoch_cannot_revive_a_reset_result():
    owner, model = Owner(), None
    model = _CornerModel(owner, 20)
    job = jobs(rows(["baseline"] * 3))[0]
    owner.epoch = 1
    model.process(job)
    assert owner.result is None and not model.laps
    model.process(replace(job, epoch=1))
    assert owner.result["retained_laps"] == 1


@pytest.mark.parametrize("channel,change", [
    ("FuelLevelPct", -.06), ("TrackTempCrew", 3), ("AirTemp", 3), ("WindVel", 3),
    ("PlayerTireCompound", 1), ("TireSetsUsed", 1),
])
def test_different_observed_conditions_do_not_borrow_an_unmatched_reference(channel, change):
    owner, data = Owner(), rows(["baseline"] * 4 + ["long_coast"] * 2 + ["baseline"])
    data[data[:, 2] >= 5, COLUMNS.index(channel)] += change
    model = _CornerModel(owner, 20)
    for job in jobs(data):
        model.process(job)
    assert owner.result["status"] == "WAIT_LAPS"
    assert owner.result["eligible_laps"] == 2 and owner.result["point"] is None


@pytest.mark.parametrize("channel,value", [
    ("OnPitRoad", 1), ("PlayerTrackSurface", 0), ("PlayerCarMyIncidentCount", 1),
    ("Speed", 0), ("SessionTick", -1),
])
def test_dirty_or_discontinuous_lap_is_not_admitted_to_the_model(channel, value):
    owner, data = Owner(), rows(["baseline"] * 4 + ["long_coast"] * 2 + ["baseline"])
    index = np.flatnonzero(data[:, 2] == 6)[200]
    data[index, COLUMNS.index(channel)] = value
    model = _CornerModel(owner, 20)
    for job in jobs(data):
        model.process(job)
    assert owner.result["status"] == "LAP_REJECTED", owner.result
    assert owner.result["point"] is None


@pytest.mark.parametrize("channel,value", [("WindVel", 5), ("TrackTempCrew", 32)])
def test_mid_lap_weather_change_is_not_hidden_by_its_average(channel, value):
    owner, data = Owner(), rows(["baseline"] * 4 + ["long_coast"] * 2 + ["baseline"])
    data[np.flatnonzero(data[:, 2] == 6)[200:400], COLUMNS.index(channel)] = value
    model = _CornerModel(owner, 20)
    for job in jobs(data):
        model.process(job)
    assert owner.result["reason"] == "WEATHER_CHANGED_OR_WET"


def test_resampled_history_and_eligible_span_are_bounded():
    owner, model = Owner(), None
    model = _CornerModel(owner, 20)
    for job in jobs(rows(["baseline"] * 17)):
        model.process(job)
    assert len(model.laps) == owner.result["retained_laps"] == 12
    assert owner.result["eligible_laps"] == 7
    assert owner.result["retained_trace_bytes"] == 12 * 601 * 6 * 8
    model.close()
    assert not model.laps


@pytest.mark.parametrize("change", [
    {"OnPitRoad": True}, {"PlayerTrackSurface": 0}, {"IsOnTrack": False, "IsOnTrackCar": False},
    {"PlayerCarMyIncidentCount": 1}, {"PlayerTireCompound": 1}, {"TireSetsUsed": 1},
    {"FuelLevel": 90}, {"FuelLevelPct": .9}, {"SessionNum": 1}, {"PlayerCarIdx": 1},
    {"TrackTempCrew": None}, {"CarLeftRight": 0}, {"CarIdxLapDistPct": None},
])
def test_collector_invalidates_current_epoch_and_rows_on_source_or_stint_change(change):
    data = rows(["baseline"] * 3)
    boundary = np.flatnonzero(np.diff(data[:, 4]) < -.9)[0] + 1
    sequence = list(frames(data[boundary - 8:boundary + 12]))
    monitor = LiveMonitor(source_id="synthetic", session_id="synthetic", sdk_tick_rate_hz=20)
    collector = LiveDrivingEngineer(20)
    try:
        for frame in sequence[:-1]:
            monitor.feed(frame)
            collector.feed(frame, monitor.latest_sample, 1_200_000)
        before = collector.snapshot(monitor.snapshot())
        assert before["buffered_rows"] > 0
        frame = replace(sequence[-1], values={**sequence[-1].values, **change})
        monitor.feed(frame)
        collector.feed(frame, monitor.latest_sample, 1_200_000)
        after = collector.snapshot(monitor.snapshot())
        assert after["epoch"] > before["epoch"]
        assert after["buffered_rows"] == 0 and after["point"] is None
    finally:
        collector.close()


def test_row_bound_drops_the_partial_lap_instead_of_growing_forever():
    data = rows(["baseline"] * 3)
    boundary = np.flatnonzero(np.diff(data[:, 4]) < -.9)[0] + 1
    monitor = LiveMonitor(source_id="synthetic", session_id="synthetic", sdk_tick_rate_hz=20)
    collector = LiveDrivingEngineer(20, max_rows=100)
    try:
        seen = []
        for frame in frames(data[boundary - 8:boundary + 130]):
            monitor.feed(frame)
            collector.feed(frame, monitor.latest_sample, 1_200_000)
            seen.append(collector.snapshot(monitor.snapshot()))
        assert any(row["reason"] == "LAP_BUFFER_LIMIT" for row in seen)
        assert max(row["buffered_rows"] for row in seen) <= 100
        assert not any(row["worker"]["submitted_frames"] for row in seen)
    finally:
        collector.close()


@pytest.mark.parametrize("field,value", [
    ("contract_version", "wrong"), ("advisor_only", False), ("executable", True),
    ("source_kind", "IBT_DISK"), ("binding_sha256", "b" * 64), ("epoch", True),
    ("revision", -1), ("completed_laps", 5), ("track_length_mm", 10**1000),
    ("session_num", True), ("player_car_idx", 2), ("claim_level", "causal"),
    ("causal_gain_verified", True), ("live_acceptance", True), ("retained_laps", 13),
    ("eligible_laps", 100), ("reference_lap", 5), ("worker", {"failed": True}),
    ("point", None),
])
def test_malformed_or_stale_projection_never_supplies_practice_facts(field, value):
    snapshot = projection(model_result())
    snapshot["driving"][field] = value
    assert validated_driving(snapshot) is None
    assert "driving.practice" not in {item["id"] for item in build_live_context(snapshot)["facts"]}


@pytest.mark.parametrize("field,value", [
    ("diagnosis", "TAKE_MORE_CURB"), ("corner_id", "private prose"), ("loss_s", float("nan")),
    ("loss_s", float("inf")), ("loss_s", True), ("loss_s", -1), ("loss_s", 10**1000),
    ("approach_m", -1), ("braking_zone_m", 1500), ("evidence_sha256", "unsafe"),
    ("evidence_laps", [6]), ("evidence_laps", [5, 5]), ("evidence_laps", [2, 3]),
    ("counterexample_laps", [6]), ("counterexample_laps", "private prose"),
])
def test_malformed_point_is_never_rendered(field, value):
    snapshot = projection(model_result())
    snapshot["driving"]["point"][field] = value
    assert validated_driving(snapshot) is None


@pytest.mark.parametrize("change", ["stale", "spectator", "disconnected", "replay"])
def test_even_a_valid_point_requires_current_owned_live_source(change):
    snapshot = projection(model_result())
    if change == "stale":
        snapshot["updated_age_s"] = 2.1
    elif change == "spectator":
        snapshot["monitor"]["context"]["player_control_state"] = "SPECTATOR"
    elif change == "disconnected":
        snapshot["connection"] = "DISCONNECTED"
    else:
        snapshot["source_mode"] = "REPLAY"
    assert not any(item["id"].startswith("driving.")
                   for item in build_live_context(snapshot)["facts"])


def test_coaching_answer_ignores_unrelated_fuel_and_traffic_but_withdraws_on_new_evidence():
    snapshot = projection(model_result())
    service = EngineerService(lambda: snapshot, environ={})
    try:
        service.submit("哪里丢时间")
        assert service.snapshot()["answer"]["stale"] is False
        snapshot["engineer_revision"] += 1
        snapshot["situation_revision"] = 100
        snapshot["fuel"] = {"status": "BLOCKED"}
        assert service.snapshot()["answer"]["stale"] is False
        snapshot["driving"]["revision"] += 1
        assert service.snapshot()["answer"]["stale"] is True
        snapshot["driving"]["revision"] -= 1
        assert service.snapshot()["answer"]["stale"] is True
        assert "spoken_text" not in service.snapshot()["answer"]
    finally:
        service.close(wait=True)


def test_unavailable_coaching_notice_is_local_and_not_coupled_to_fuel():
    snapshot = fixtures("live_queries").snapshot()
    snapshot["driving"] = {"status": "ERROR"}
    service = EngineerService(lambda: snapshot, environ={})
    try:
        service.submit("我该练什么")
        assert "驾驶分析故障" in service.snapshot()["answer"]["spoken_text"]
        snapshot["engineer_revision"] += 1
        assert service.snapshot()["answer"]["stale"] is False
        assert service.snapshot()["requests_used"] == 0
    finally:
        service.close(wait=True)


def test_blocking_model_and_queue_failure_do_not_block_fuel_or_spotter(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = _CornerModel.process

    def slow(self, job):
        entered.set()
        assert release.wait(10)
        return original(self, job)

    monkeypatch.setattr(_CornerModel, "process", slow)
    now = [1.0]
    state = live_app.AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    state.start_spotter(20)
    analysis = live_app._LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic",
                                    tick_rate=20, car_count=3, generation=state.generation,
                                    allowed=lambda: True)
    worker = analysis._driving._worker
    try:
        job = jobs(rows(["baseline"] * 3), epoch=analysis._driving._epoch)[0]
        assert worker.submit(job, size=len(job.rows), observed_at=now[0])
        assert entered.wait(2)
        data = rows(["baseline"] * 2)[:20]
        start = time.monotonic()
        for frame in frames(data):
            now[0] = frame.values["SessionTime"] + 1
            state.feed_spotter(frame)
            analysis.process((frame, "Practice", now[0], 1_200_000))
        assert time.monotonic() - start < 2
        assert state.snapshot()["fuel"]["current_fuel_l"] > 0
        assert state.spotter_snapshot()["spotter"]["status"] == "READY"
        assert worker.submit(job, size=len(job.rows), observed_at=now[0])
        assert not worker.submit(job, size=len(job.rows), observed_at=now[0])
        assert worker.snapshot()["reason"] == "QUEUE_OVERFLOW"
        assert analysis._driving.snapshot({})["status"] == "ERROR"
        analysis._driving.reset("SOURCE_STALE")
        assert analysis._driving.snapshot({})["status"] == "ERROR"
        closer = threading.Thread(target=analysis.close)
        closer.start()
        closer.join(.05)
        assert closer.is_alive() and not worker.done
        release.set()
        closer.join(3)
        assert not closer.is_alive() and worker.done
        assert analysis._driving.snapshot({})["point"] is None
    finally:
        release.set()
        analysis.close()


def test_repeated_completed_counter_cannot_manufacture_repeated_evidence():
    owner, data = Owner(), rows(["baseline"] * 4 + ["long_coast"] * 2 + ["baseline"])
    model = _CornerModel(owner, 20)
    for job in jobs(data):
        model.process(replace(job, completed_laps=2))
    assert owner.result["reason"] == "LAP_COUNTER_NOT_ADVANCING"
    assert owner.result["point"] is None and not model.laps


@pytest.mark.parametrize("fault", ["startup", "feed", "model"])
def test_coaching_fault_stays_visible_without_poisoning_other_analysis(monkeypatch, fault):
    def broken(*_args, **_kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_ERROR")

    if fault == "startup":
        monkeypatch.setattr(live_app, "LiveDrivingEngineer", broken)
    elif fault == "feed":
        monkeypatch.setattr(LiveDrivingEngineer, "feed", broken)
    else:
        monkeypatch.setattr(_CornerModel, "process", broken)
    now = [1.0]
    state = live_app.AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    state.start_spotter(20)
    analysis = live_app._LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic",
                                    tick_rate=20, car_count=3, generation=state.generation,
                                    allowed=lambda: True)
    try:
        for frame in frames(rows(["baseline"] * 4)):
            now[0] = frame.values["SessionTime"] + 1
            state.feed_spotter(frame)
            analysis.process((frame, "Practice", now[0], 1_200_000))
            if fault == "model" and frame.values["LapDistPct"] < .04:
                assert analysis._driving._worker.wait_idle(3)
        value = state.snapshot()
        assert value["driving"]["status"] == "ERROR"
        assert value["fuel"]["current_fuel_l"] > 0 and value["monitor"] is not None
        assert value["traffic"]["status"] == "READY"
        assert state.spotter_snapshot()["spotter"]["status"] == "READY"
        answer = render_live_query(build_live_context(value), "driving")
        assert "驾驶分析故障" in answer["spoken_text"]
        assert "SYNTHETIC_PRIVATE_ERROR" not in json.dumps(value)
    finally:
        analysis.close()


@pytest.mark.parametrize("change", ["fuel_only", "revision", "timeout", "completed_lap"])
def test_delayed_cloud_coaching_binds_question_time_not_completion(change):
    value, now = projection(model_result()), [10.0]
    entered, release = threading.Event(), threading.Event()

    class Planner:
        def complete(self, context, question):
            entered.set()
            assert release.wait(3)
            return {"topic": "driving", "fact_ids": ["driving.pattern", "driving.practice"],
                    "notice_ids": []}

    service = EngineerService(lambda: copy.deepcopy(value), EngineerConfig(provider="deepseek"),
                              client=Planner(), clock=lambda: now[0], environ={})
    try:
        assert service.submit("解释驾驶建议的依据")[0] == 202
        assert entered.wait(1)
        if change == "fuel_only":
            value["engineer_revision"] += 1
        elif change == "revision":
            value["driving"]["revision"] += 1
        elif change == "timeout":
            now[0] += 30.01
        else:
            value["monitor"]["telemetry"]["laps_completed"] += 1
        release.set()
        answer = fixtures("llm_engineer").wait_answer(service)["answer"]
        assert answer["origin"] == "deepseek"
        assert answer["stale"] is (change != "fuel_only")
    finally:
        release.set()
        service.close(wait=True)


def test_native_view_has_explicit_evidence_and_fixed_missing_channel_health():
    from iracing_ai_engineer.desktop_window import DesktopPresenter

    value = projection(model_result())

    def view():
        return DesktopPresenter().project({"lifecycle": "RUNNING", "telemetry": value}, now=1)

    assert "2 圈重复" in view().driving and "不保证提速" in view().driving
    value["driving"].update(status="WAIT_LAPS", reason="REQUIRED_DATA_UNAVAILABLE",
                            track_length_mm=None, point=None)
    assert "缺少必需" in view().driving
    assert "缺少必需" in render_live_query(build_live_context(value), "driving")["spoken_text"]
    value["updated_age_s"] = 2.1
    assert "2 圈重复" not in view().driving


def test_full_fuel_traffic_and_coaching_context_is_still_bounded_and_private():
    value = fixtures("live_traffic").state()
    fuel = fixtures("live_queries").snapshot()["fuel"]
    value["fuel"] = {**fuel, "current_fuel_l": 42.0, "estimated_laps_remaining": 20,
                     "fuel_needed_to_finish_l": 62.0, "fuel_shortfall_l": 20.0,
                     "race_laps_to_go": 30}
    value["session_type"] = "Race"
    coach = projection(model_result())["driving"]
    coach["binding_sha256"] = value["monitor"]["binding_sha256"]
    value["monitor"]["telemetry"]["laps_completed"] = coach["completed_laps"]
    value["driving"] = coach
    value["driving"]["private_message"] = "SYNTHETIC_PRIVATE_MARKER"
    context = build_live_context(value)
    fixtures("llm_evidence")._assert_bounded(context)
    assert len(context["facts"]) == 18
    assert "SYNTHETIC_PRIVATE" not in json.dumps(context)
    assert "evidence_sha256" not in json.dumps(context)


def test_fake_ptt_to_real_service_speaks_the_grounded_practice_point():
    from iracing_ai_engineer.voice_service import VoiceService

    fixture = fixtures("voice_service")
    audio, speech, value = fixture.Audio(), fixture.Speech(), projection(model_result())
    speech.recognize = lambda *_args, **_kwargs: {"text": "哪里可以改进", "confidence": 0.9}
    service = EngineerService(lambda: copy.deepcopy(value), environ={})
    voice = VoiceService(lambda: {
        "lifecycle": "RUNNING", "telemetry": value,
        "engineer": {**service.snapshot(), "instance_id": "0"},
    }, service.submit, fixture.Store(True), audio=audio, speech=speech, input_factory=fixture.Input)
    try:
        voice.start()
        fixture.wait_for(lambda: voice.snapshot()["devices"]["inputs"])
        voice.press()
        assert audio.entered.wait(1)
        voice.release()
        fixture.wait_for(lambda: bool(speech.synthesis))
        assert "练习假设" in speech.synthesis[0] and "不保证提速" in speech.synthesis[0]
        assert service.snapshot()["requests_used"] == 0
    finally:
        voice.close()
        service.close(wait=True)
