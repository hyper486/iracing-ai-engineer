"""Invented edge streams, fake PTT and native controls only; no live acceptance."""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import sys
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import pytest

from iracing_ai_engineer import live_app
from iracing_ai_engineer.live_fuel import LiveFuelConfig
from iracing_ai_engineer.live_monitor import LiveMonitor
from iracing_ai_engineer.live_pit_observation import (
    LivePitObservationTracker,
    derive_observation,
    pit_observation_draft,
    validated_pit_observation,
)
from iracing_ai_engineer.live_queries import live_query_intent, render_live_query
from iracing_ai_engineer.live_strategy import StrategyParameters
from iracing_ai_engineer.llm_engineer import EngineerService, fallback_plan
from iracing_ai_engineer.llm_evidence import build_live_context
from iracing_ai_engineer.synthetic_runtime import synthetic_frames


@lru_cache
def fixtures(name):
    spec = importlib.util.spec_from_file_location(
        f"_pit_observation_{name}", Path(__file__).with_name(f"test_{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Rig:
    def __init__(self):
        self.base = next(synthetic_frames(8, rate=20))
        self.monitor = LiveMonitor(source_id="synthetic", session_id="synthetic",
                                   sdk_tick_rate_hz=20, expected_car_count=3)
        self.tracker = LivePitObservationTracker(20)
        self.tick = -1
        self.frame = None

    def step(self, position=None, *, read_errors=(), **changes):
        self.tick += 1
        seconds = self.tick / 20
        position = seconds / 20 if position is None else position
        values = {**self.base.values, "SessionTick": self.tick, "SessionTime": seconds,
                  "Lap": math.floor(position) + 1, "LapCompleted": math.floor(position),
                  "LapDistPct": position % 1, "FuelLevel": 30., "Speed": 0.,
                  "PitstopActive": False, "PlayerCarInPitStall": False, **changes}
        self.frame = replace(self.base, buffer_tick=self.tick, values=values,
                             read_errors=read_errors, captured_monotonic_s=seconds + 1)
        assert self.monitor.feed(self.frame)
        self.tracker.feed(self.frame, self.monitor.latest_sample)
        value = fixtures("live_queries").snapshot()
        value.update(fuel={"status": "BLOCKED"}, traffic=None)
        value["monitor"] = self.monitor.snapshot()
        value["pit_observation"] = self.tracker.snapshot(value["monitor"])
        return value

    def warm(self):
        for _ in range(1520):
            self.step()

    def visit(self, *, service=True, missing=(), interrupt=None):
        origin = self.tracker.previous["progress_laps"]
        for i in range(1, 401):
            travelled = .0015 * (min(i, 100) + max(i - 300, 0))
            changes = {"OnPitRoad": True, "PitstopActive": service and 100 <= i <= 300,
                       "PlayerCarInPitStall": service and 100 <= i <= 300}
            if interrupt is not None and i == 150:
                changes.update(interrupt)
            self.step(origin + travelled,
                      read_errors=changes.pop("read_errors", missing), **changes)
        return self.step(origin + .3025, FuelLevel=50.)


@pytest.fixture(scope="module")
def completed():
    rig = Rig()
    rig.warm()
    return rig.visit()


def test_completed_visit_keeps_elapsed_net_loss_and_fuel_change_distinct(completed):
    value = validated_pit_observation(completed)
    assert value is not None and value["status"] == "OBSERVED"
    observation = value["observation"]
    assert observation["pit_road_elapsed_range_s"] == [19.95, 20.05]
    assert observation["baseline_track_elapsed_range_s"] == pytest.approx([5.77, 6.25])
    assert observation["net_loss_estimate_range_s"] == pytest.approx([13.7, 14.28])
    assert observation["observed_net_tank_change_l"] == 20.
    assert observation["review_draft_available"] is True
    assert value["calibrated"] is value["live_acceptance"] is value["executable"] is False
    assert observation["service_contents"] == "UNKNOWN"
    assert observation["entry_fraction"] == pytest.approx(.79825)
    assert observation["exit_fraction"] == pytest.approx(.09875)


@pytest.mark.parametrize("option", [
    "no_baseline", "drive_through", "service_missing", "fuel_missing"])
def test_incomplete_service_or_baseline_does_not_fabricate_full_cost(option):
    rig = Rig()
    if option == "no_baseline":
        rig.step()
        rig.step()
    else:
        rig.warm()
    value = rig.visit(service=option != "drive_through", missing=(
        ("PitstopActive",) if option == "service_missing" else
        ("FuelLevel",) if option == "fuel_missing" else ()))
    observation = validated_pit_observation(value)["observation"]
    assert observation["pit_road_elapsed_range_s"] == [19.95, 20.05]
    if option == "no_baseline":
        assert observation["net_loss_estimate_range_s"] is None
    if option == "fuel_missing":
        assert observation["observed_net_tank_change_l"] is None
        assert pit_observation_draft(value) is not None
    else:
        assert pit_observation_draft(value) is None


@pytest.mark.parametrize("change", [
    {"read_errors": ("OnPitRoad",)}, {"SessionFlags": 8},
    {"PlayerCarMyIncidentCount": 1}, {"IsReplayPlaying": True},
    {"PlayerCarIdx": 1}, {"SessionNum": 1}, {"SessionTime": 500.},
    {"IsOnTrack": False, "IsOnTrackCar": False}, {"LapDistPct": .9},
])
def test_interrupted_visit_cannot_resume_from_a_safe_final_frame(change):
    rig = Rig()
    rig.step()
    rig.step()
    value = rig.visit(interrupt=change)
    assert value["pit_observation"]["status"] == "WAIT"
    assert pit_observation_draft(value) is None


def test_started_inside_requires_a_new_complete_visit():
    rig = Rig()
    rig.step(OnPitRoad=True)
    value = rig.step(OnPitRoad=True)
    assert value["pit_observation"]["reason"] == "PIT_OBSERVATION_STARTED_INSIDE"
    rig.step()
    value = rig.visit()
    assert value["pit_observation"]["status"] == "OBSERVED"
    assert value["pit_observation"]["observation"]["net_loss_estimate_range_s"] is None


@pytest.mark.parametrize("change", ["extra", "time", "sequence", "source", "player", "session",
                                  "number", "point", "profiles", "revision", "future"])
def test_tampered_or_diverged_projection_is_rejected(completed, change):
    value = copy.deepcopy(completed)
    observed = value["pit_observation"]
    observation = observed["observation"]
    if change == "extra":
        observation["private"] = "SYNTHETIC_PRIVATE_MARKER"
    elif change == "time":
        value["monitor"]["session_time_us"] += 1
    elif change == "sequence":
        value["monitor"]["sequence"] += 1
    elif change == "source":
        value["monitor"]["binding_sha256"] = "a" * 64
    elif change in ("player", "session"):
        key = "player_car_idx" if change == "player" else "session_num"
        value["monitor"]["telemetry"][key] += 1
    elif change == "number":
        observation["net_loss_estimate_range_s"][0] += 1
    elif change == "point":
        observation["record"]["entry"][0]["progress_laps"] = float("nan")
    elif change == "profiles":
        observation["record"]["reference"]["lap_profiles"][0]["elapsed_us"][10] = 0
    elif change == "revision":
        observed["revision"] = True
    else:
        observed["session_time_us"] = value["monitor"]["session_time_us"] = 1
    assert validated_pit_observation(value) is None
    assert pit_observation_draft(value) is None
    assert not any(row["id"].startswith("pit_observation.")
                   for row in build_live_context(value)["facts"])


@pytest.mark.parametrize("change", ["moving", "speed_error", "stale", "offline", "contract"])
def test_draft_requires_current_stopped_owned_source(completed, change):
    value = copy.deepcopy(completed)
    if change == "moving":
        value["monitor"]["telemetry"]["speed_mps"] = .11
    elif change == "speed_error":
        value["monitor"]["reasons"].append("READ_ERROR:Speed")
    elif change == "stale":
        value["updated_age_s"] = 2.01
    elif change == "offline":
        value["connection"] = "DISCONNECTED"
    else:
        value["contract_version"] = "other"
    assert pit_observation_draft(value) is None


def test_negative_counterfactual_is_not_clipped_into_a_positive_draft(completed):
    record = copy.deepcopy(completed["pit_observation"]["observation"]["record"])
    for point in record["exit"]:
        point["time_us"] -= 16_000_000
    observation = derive_observation(record)
    assert observation["net_loss_estimate_range_s"][1] < 0
    assert observation["review_draft_available"] is False


def test_query_uses_fixed_local_facts_and_not_future_strategy(completed):
    assert live_query_intent("请问，这次进站用了多久？") == "pit_observation"
    assert live_query_intent("假设这次进站用了多久") is None
    value = copy.deepcopy(completed)
    value["private"] = "SYNTHETIC_PRIVATE_MARKER"
    context = build_live_context(value)
    answer = render_live_query(context, "pit_observation")
    assert "站内耗时" in answer["spoken_text"] and len(answer["spoken_text"]) < 80
    assert "不是未来进站损失" in answer["spoken_text"]
    assert "边界外减速与加速损失" in answer["text"]
    assert "不是加油机实际交付量" in answer["text"]
    assert "SYNTHETIC_PRIVATE" not in json.dumps(context)
    assert "record" not in json.dumps(context)
    assert fallback_plan(context, "解释进站耗时的依据")["fact_ids"][0] == "pit_observation.elapsed"


def publish(state, value):
    state.publish(value["monitor"], value["fuel"], None, "Race",
                  pit_observation=value["pit_observation"])


@pytest.mark.parametrize("change", ["fuel", "ttl", "new_visit", "invalidate", "gap"])
def test_local_answer_binding_and_ttl_do_not_depend_on_fuel_model(completed, change):
    value = copy.deepcopy(completed)
    now = [10.]
    state = live_app.AppState(clock=lambda: now[0])
    publish(state, value)
    service = EngineerService(state.snapshot, clock=lambda: now[0], environ={})
    try:
        assert service.submit("这次进站用了多久")[0] == 202
        assert service.snapshot()["requests_used"] == 0
        assert service.snapshot()["answer"]["origin"] == "local_live"
        if change == "fuel":
            value["monitor"]["interval_invalid_for_fuel"] = ["REFUEL_INTERVAL"]
            value["fuel"]["status"] = "LEARNING"
        elif change == "ttl":
            now[0] += 10.1
        elif change == "new_visit":
            value["pit_observation"]["revision"] += 1
        elif change == "gap":
            now[0] += 2.1  # Do not poll during the gap; recovery must still latch it.
        else:
            state.invalidate_analysis(state.generation)
        publish(state, value)
        assert service.snapshot()["answer"]["stale"] is (change != "fuel")
    finally:
        service.close(wait=True)


def test_draft_is_explicit_revision_bound_and_does_not_apply_or_persist(completed):
    state = live_app.AppState(clock=lambda: 10.)
    publish(state, completed)
    before = state.strategy_inputs()
    draft = pit_observation_draft(state.snapshot())
    assert state.strategy_inputs() == before
    inputs = draft["inputs"]
    assert inputs["complete_pit_loss_low_s"] <= 13.7
    assert inputs["complete_pit_loss_high_s"] >= 14.28
    parameters = StrategyParameters(tank_capacity_l=100., **inputs)
    state.configure_strategy(parameters, pit_draft_binding=draft["binding"])
    assert state.strategy_inputs()[0] == parameters
    value = copy.deepcopy(completed)
    value["pit_observation"]["revision"] += 1
    publish(state, value)
    before = state.strategy_inputs()
    with pytest.raises(ValueError, match="PIT_DRAFT_EXPIRED"):
        state.configure_strategy(parameters, pit_draft_binding=draft["binding"])
    assert state.strategy_inputs() == before


def test_draft_cannot_recover_after_transient_analysis_loss(completed):
    state = live_app.AppState(clock=lambda: 10.)
    publish(state, completed)
    draft = pit_observation_draft(state.snapshot())
    state.invalidate_analysis(state.generation)
    publish(state, completed)
    with pytest.raises(ValueError, match="PIT_DRAFT_EXPIRED"):
        state.configure_strategy(StrategyParameters(tank_capacity_l=100., **draft["inputs"]),
                                  pit_draft_binding=draft["binding"])


@pytest.mark.parametrize("fault", ["startup", "feed", "snapshot"])
def test_fault_isolated_from_spotter_fuel_and_coaching(monkeypatch, fault):
    def broken(*_args, **_kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_MARKER")
    if fault == "startup":
        monkeypatch.setattr(live_app, "LivePitObservationTracker", broken)
    else:
        monkeypatch.setattr(LivePitObservationTracker, fault, broken)
    now = [1.]
    state = live_app.AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    state.start_spotter(20)
    analysis = live_app._LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic",
        tick_rate=20, car_count=3, generation=state.generation, allowed=lambda: True)
    try:
        for frame in synthetic_frames(8, rate=20):
            now[0] = frame.captured_monotonic_s
            state.feed_spotter(frame)
            analysis.process((frame, "Race", now[0], 1_200_000))
            if frame.buffer_tick > 40:
                break
        value = state.snapshot()
        answer = render_live_query(build_live_context(value), "pit_observation")
        assert "故障" in answer["spoken_text"]
        assert value["spotter"]["status"] == "READY"
        assert value["driving"]["worker"]["failed"] is False
        assert "fuel.current" in {row["id"] for row in build_live_context(value)["facts"]}
        assert "SYNTHETIC_PRIVATE" not in json.dumps(value)
    finally:
        analysis.close()
        state.connection("STOPPED")


@pytest.mark.parametrize("invalidate", [False, True])
def test_ptt_speaks_locally_and_discards_lost_observation_while_synthesizing(completed, invalidate):
    import threading

    from iracing_ai_engineer.voice_service import VoiceService

    helper = fixtures("voice_service")
    state = live_app.AppState()
    publish(state, completed)
    entered, release = threading.Event(), threading.Event()
    audio, speech = helper.Audio(), helper.Speech()
    speech.recognize = lambda *_args, **_kwargs: {"text": "这次进站用了多久", "confidence": .9}

    def synthesize(text, **_kwargs):
        speech.synthesis.append(text)
        entered.set()
        assert release.wait(3)
        return b"SYNTHETIC_PIT_OBSERVATION_SPEECH"

    speech.synthesize = synthesize
    service = EngineerService(state.snapshot, environ={})
    voice = VoiceService(lambda: {
        "lifecycle": "RUNNING", "telemetry": state.snapshot(),
        "engineer": {**service.snapshot(), "instance_id": "0"},
    }, service.submit, helper.Store(True), audio=audio, speech=speech, input_factory=helper.Input)
    try:
        voice.start()
        helper.wait_for(lambda: voice.snapshot()["devices"]["inputs"])
        voice.press()
        assert audio.entered.wait(1)
        voice.release()
        assert entered.wait(1)
        if invalidate:
            state.invalidate_analysis(state.generation)
            publish(state, completed)
        release.set()
        helper.wait_for(lambda: voice.snapshot()["status"] == "READY")
        played = [wav for wav, _, _ in audio.plays if wav == b"SYNTHETIC_PIT_OBSERVATION_SPEECH"]
        assert len(played) == (0 if invalidate else 1)
        assert len(speech.synthesis) == 1 and "不是未来进站损失" in speech.synthesis[0]
        assert service.snapshot()["requests_used"] == 0
    finally:
        release.set()
        voice.close()
        service.close(wait=True)


@pytest.mark.parametrize("variant", [None, True, [], {}, "bad", float("inf"), 10**1000])
def test_malformed_numeric_record_fields_never_raise(completed, variant):
    original = completed["pit_observation"]["observation"]["record"]
    for key in original:
        if key == "reference" and variant is None:
            continue  # Missing baseline can legitimately retain elapsed time.
        record = copy.deepcopy(original)
        record[key] = variant
        assert derive_observation(record) is None
    for key in original["entry"][0]:
        record = copy.deepcopy(original)
        record["entry"][0][key] = variant
        # Optional fields may be unknown, or bool for the two actual flags.
        if (variant is None and key in ("fuel_l", "service", "stall")) or (
                type(variant) is bool and key in ("service", "stall")):
            continue
        assert derive_observation(record) is None


def test_frozen_numerical_owner_pit_query_and_reviewed_draft_check():
    from iracing_ai_engineer.synthetic_runtime import run_synthetic_pit_observation

    assert run_synthetic_pit_observation() == {
        "id": "SYNTHETIC_PIT_OBSERVATION_DRAFT", "status": "PASS"}


@pytest.mark.parametrize("bad", [None, True, float("nan"), 10**1000])
def test_malformed_monitor_reasons_fail_closed_in_draft(completed, bad):
    value = copy.deepcopy(completed)
    value["monitor"]["reasons"] = bad
    assert validated_pit_observation(value) is None
    assert pit_observation_draft(value) is None
    assert not any(row["id"].startswith("pit_observation.")
                   for row in build_live_context(value)["facts"])


def test_controller_draft_read_and_confirmation_do_not_save_restart_or_call_cloud(
    completed, tmp_path,
):
    from iracing_ai_engineer.desktop_controller import DesktopController

    helper = fixtures("desktop_controller")
    reader, store = helper.Reader(), helper.Store(tmp_path)
    controller = DesktopController(store=store, reader=reader, environ={})
    try:
        controller.start()
        assert reader.entered.wait(1)
        helper.wait_for(lambda: controller.snapshot()["lifecycle"] == "RUNNING")
        publish(controller._state, completed)
        calls = len(reader.calls)
        draft = controller.pit_observation_draft()
        assert draft is not None
        assert controller._state.strategy_inputs()[0] is None
        controller.configure_strategy({"tank_capacity_l": 100., **draft["inputs"]},
                                      pit_draft_binding=draft["binding"])
        assert len(reader.calls) == calls == 1 and not store.saved
        assert controller.snapshot()["engineer"]["requests_used"] == 0
    finally:
        controller.close()
        helper.wait_for(controller.is_closed)
    assert controller.pit_observation_draft() is None


def test_result_and_profiles_are_bounded_and_cleared_after_loss():
    rig = Rig()
    rig.warm()
    first = rig.visit()
    revision = first["pit_observation"]["revision"]
    origin = rig.tracker.previous["progress_laps"]
    for i in range(1, 4001):
        value = rig.step(origin + i / 400)
        assert len(rig.tracker.trace.profiles) <= 2
        assert len(rig.tracker.trace.crossings) <= 3
        assert len(rig.tracker.trace.active or []) <= 64
        assert rig.tracker.active is None
    assert value["pit_observation"]["revision"] == revision
    second = rig.visit()
    assert second["pit_observation"]["revision"] > revision
    assert len(second["pit_observation"]["observation"]["record"]["reference"]["lap_profiles"]) == 2
    value = rig.step(rig.tracker.previous["progress_laps"], read_errors=("OnPitRoad",))
    assert value["pit_observation"]["observation"] is None
    assert rig.tracker.trace is rig.tracker.active is rig.tracker.observation is None
