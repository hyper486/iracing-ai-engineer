"""Invented phase tracks and snapshots only; no SDK, audio or live acceptance."""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from iracing_ai_engineer import live_app
from iracing_ai_engineer.live_fuel import LiveFuelConfig
from iracing_ai_engineer.live_monitor import LiveMonitor
from iracing_ai_engineer.live_motion import (
    LiveMotionTracker,
    _actor,
    unavailable_motion,
    validated_motion,
)
from iracing_ai_engineer.live_queries import live_query_intent, render_live_query
from iracing_ai_engineer.live_rejoin import project_rejoin, validated_rejoin
from iracing_ai_engineer.live_strategy import StrategyParameters, project_strategy
from iracing_ai_engineer.llm_engineer import EngineerService, fallback_plan
from iracing_ai_engineer.llm_evidence import build_live_context
from iracing_ai_engineer.rejoin_projection import phase_position, phase_time, project_phase_rejoin
from iracing_ai_engineer.synthetic_runtime import synthetic_frames


def fixtures(name):
    identifier = f"_rejoin_{name}"
    if identifier not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            identifier, Path(__file__).with_name(f"test_{name}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[identifier] = module
        spec.loader.exec_module(module)
    return sys.modules[identifier]


def parameters(**changes):
    return StrategyParameters(**{
        "tank_capacity_l": 50., "pit_entry_fraction": .8, "pit_exit_fraction": .05,
        "complete_pit_loss_low_s": 30., "complete_pit_loss_high_s": 31., **changes})


def evidence(**kwargs):
    state, _ = fixtures("live_strategy").configured(parameters=parameters(), **kwargs)
    value = state.snapshot()
    monitor = value["monitor"]
    monitor["session_time_us"] = 350_000_000
    monitor["telemetry"].update(laps_completed=3, lap_distance_pct=.5)
    # Reproject the fuel comparison at the same invented source time.
    state.publish(monitor, value["fuel"], None, "Race")
    value = state.snapshot()
    monitor = value["monitor"]
    profile = {"elapsed_us": [i * 1_562_500 for i in range(65)], "sampling_error_us": 20_000}
    rows = []
    for index, position in ((0, 3.5), (1, 3.8)):
        last = round(350_000_000 - (position % 1) * 100_000_000)
        windows = [[last - n * 100_000_000 - 10_000, last - n * 100_000_000 + 10_000]
                   for n in (2, 1, 0)]
        rows.append(_actor(index, position, windows, [profile, profile], 350_000_000))
    assert all(rows)
    value["motion"] = {**unavailable_motion(monitor, revision=1), "status": "READY",
                       "reason": "EMPIRICAL_WHOLE_LAP_PACE", "player": rows[0],
                       "opponents": rows[1:]}
    value["rejoin"] = project_rejoin(value)
    return value


class Rig:
    def __init__(self):
        self.base = next(synthetic_frames(8, rate=20))
        self.monitor = LiveMonitor(source_id="synthetic", session_id="synthetic",
                                   sdk_tick_rate_hz=20, expected_car_count=3)
        self.tracker = LiveMotionTracker(20)
        self.tick = -1

    def step(self, *, read_errors=(), **changes):
        self.tick += 1
        seconds = self.tick / 20
        position = seconds / 20
        other = position + .4
        values = {**self.base.values, "SessionTick": self.tick, "SessionTime": seconds,
                  "Lap": math.floor(position) + 1, "LapCompleted": math.floor(position),
                  "LapDistPct": position % 1,
                  "CarIdxLapCompleted": [math.floor(position), math.floor(other), 0],
                  "CarIdxLap": [math.floor(position) + 1, math.floor(other) + 1, 0],
                  "CarIdxLapDistPct": [position % 1, other % 1, -1.], **changes}
        frame = replace(self.base, buffer_tick=self.tick, values=values,
                        read_errors=read_errors, captured_monotonic_s=seconds + 1)
        assert self.monitor.feed(frame)
        self.tracker.feed(frame, self.monitor.latest_sample)
        monitor = self.monitor.snapshot()
        return {"monitor": monitor, "motion": self.tracker.snapshot(monitor)}


def test_normalized_frames_need_two_complete_laps_and_refresh_without_speech_churn():
    rig = Rig()
    for _ in range(1201):
        value = rig.step()
    assert validated_motion(value) is not None
    assert value["motion"]["status"] == "READY"
    revision = value["motion"]["revision"]
    for _ in range(800):
        value = rig.step()
    assert value["motion"]["revision"] == revision
    assert validated_motion(value) is not None
    for trace in rig.tracker.actors.values():
        assert len(trace.profiles) == 2 and len(trace.crossings) == 3
        assert len(trace.active) <= 64
    value = rig.step(read_errors=("CarIdxLapDistPct",))
    assert value["motion"]["status"] == "WAIT"
    assert not rig.tracker.actors


@pytest.mark.parametrize("change", ["time", "shape", "revision", "extra", "telemetry", "parked"])
def test_motion_rejects_stale_malformed_or_diverged_evidence(change):
    value = evidence()
    assert validated_motion(value) is not None
    if change == "time":
        value["monitor"]["session_time_us"] += 1
    elif change == "shape":
        value["motion"]["player"]["lap_profiles"][0]["elapsed_us"][20] = 0
    elif change == "revision":
        value["motion"]["revision"] = True
    elif change == "extra":
        value["motion"]["player"]["private"] = "SYNTHETIC_PRIVATE_MARKER"
    elif change == "telemetry":
        value["monitor"]["telemetry"] = None
    else:
        value["motion"]["player"]["progress_laps"] = 3.4
        value["monitor"]["telemetry"]["lap_distance_pct"] = .4
    assert validated_motion(value) is None
    assert project_rejoin(value)["status"] == "WAIT"


@pytest.mark.parametrize("position", [-1.75, 0., .01, .5, .999, 3.2, 10000.7])
def test_phase_inverse_supports_nonuniform_laps_and_wrap(position):
    profile = {"elapsed_us": [i * i * 1000 for i in range(65)]}
    assert phase_position(profile, phase_time(profile, position)) == pytest.approx(position)


def test_mapped_exit_uses_complete_loss_not_entry_or_partial_pumping_budget():
    value = evidence(amount=6.)
    result = validated_rejoin(value)
    assert result is not None and result["status"] == "READY"
    early, late = result["scenarios"]
    assert (early["entry_progress_laps"], early["exit_progress_laps"]) == (3.8, 4.05)
    assert early["distance_to_entry_laps"] == .3
    assert early["distance_to_exit_laps"] == .55
    assert early["complete_loss_range_s"] == [30., 31.]
    assert early["arrival_fuel_l"] == 5.4 and late["arrival_fuel_l"] == 3.4
    assert early["ahead"]["gap_range_s"][0] < 60 < early["ahead"]["gap_range_s"][1]
    assert early["behind"]["gap_range_s"][0] < 40 < early["behind"]["gap_range_s"][1]
    assert result["live_acceptance"] is False and result["executable"] is False
    assert result["assumptions_provenance"] == "USER_RULE"


def test_distant_endpoint_withdrawn_independently_and_lapped_order_irrelevant():
    value = evidence()
    assert value["rejoin"]["scenarios"][1]["reason_codes"] == ["FORECAST_BEYOND_TWO_LAPS"]
    motion = value["motion"]
    first = project_phase_rejoin(motion, exit_progress_laps=4.05, loss_range_s=(30., 31.))
    motion["opponents"][0]["progress_laps"] += 5
    assert project_phase_rejoin(motion, exit_progress_laps=4.05, loss_range_s=(30., 31.)) == first


def test_overlap_and_uncertain_neighbor_order_withdraw_gap_estimates():
    value = evidence()
    motion = value["motion"]
    a, b, reasons = project_phase_rejoin(motion, exit_progress_laps=4.05, loss_range_s=(69., 71.))
    assert a is b is None and "REJOIN_ZERO_CROSSING_WITHIN_UNCERTAINTY" in reasons
    motion["opponents"].append({**copy.deepcopy(motion["opponents"][0]), "car_idx": 2})
    a, b, reasons = project_phase_rejoin(motion, exit_progress_laps=4.05, loss_range_s=(30., 31.))
    assert a is b is None and "REJOIN_ORDER_AMBIGUOUS" in reasons


@pytest.mark.parametrize("changes", [
    {"pit_entry_fraction": None}, {"pit_exit_fraction": 1.}, {"pit_exit_fraction": .8},
    {"pit_entry_fraction": True}, {"complete_pit_loss_low_s": float("nan")},
    {"complete_pit_loss_high_s": 601}, {"complete_pit_loss_high_s": 20},
])
def test_incomplete_or_invalid_assumptions_rejected(changes):
    with pytest.raises(ValueError, match="STRATEGY_PARAMETERS_INVALID"):
        parameters(**changes)


@pytest.mark.parametrize("change", ["gap", "source", "config", "flags", "permission", "private"])
def test_rejoin_must_recompute_exactly_and_gate_permissions(change):
    value = evidence()
    assert validated_rejoin(value) is not None
    if change == "gap":
        value["rejoin"]["scenarios"][0]["ahead"]["gap_range_s"][0] += 1
    elif change == "source":
        value["monitor"]["binding_sha256"] = "b" * 64
    elif change == "config":
        value["strategy_configuration"]["inputs"]["pit_entry_fraction"] = .7
    elif change == "flags":
        value["monitor"]["telemetry"]["session_flags"] = 8
    elif change == "permission":
        value["monitor"]["telemetry"]["pits_open"] = False
    else:
        value["rejoin"]["private"] = "SYNTHETIC_PRIVATE_MARKER"
    assert validated_rejoin(value) is None


def test_multiclass_nearest_behind_is_physical_neighbor_not_fastest_arrival():
    motion = evidence()["motion"]
    slow = copy.deepcopy(motion["opponents"][0])
    fast = copy.deepcopy(slow)
    slow.update(progress_laps=3.4, car_idx=1)
    fast.update(progress_laps=3.3, car_idx=2)
    for actor, factor in ((slow, 2), (fast, .2)):
        for profile in actor["lap_profiles"]:
            profile["elapsed_us"] = [round(n * factor) for n in profile["elapsed_us"]]
    motion["opponents"] = [slow, fast]
    _, behind, reasons = project_phase_rejoin(motion, exit_progress_laps=3.5, loss_range_s=(0., 0.))
    assert not reasons and behind["car_idx"] == 1
    assert behind["gap_range_s"][0] < 20 < behind["gap_range_s"][1]


def test_nonuniform_phase_projection_is_not_uniform_lap_speed_extrapolation():
    motion = evidence()["motion"]
    original = project_phase_rejoin(motion, exit_progress_laps=4.05, loss_range_s=(30., 31.))
    for profile in motion["opponents"][0]["lap_profiles"]:
        profile["elapsed_us"] = [round((i / 64) ** 2 * 100_000_000) for i in range(65)]
    changed = project_phase_rejoin(motion, exit_progress_laps=4.05, loss_range_s=(30., 31.))
    assert not changed[2] and changed != original
    # Both profiles retain the same 100-second lap time but differ in shape.
    assert changed[0]["gap_range_s"][0] > original[0]["gap_range_s"][1]


def published(*, now=None, amount=20.):
    now = now if now is not None else [10.]
    state, _ = fixtures("live_strategy").configured(parameters=parameters(), now=now, amount=amount)
    value = evidence(amount=amount)
    publish(state, value)
    assert validated_rejoin(state.snapshot()) is not None
    return state, value


def publish(state, value):
    state.publish(value["monitor"], value["fuel"], None, "Race", motion=value["motion"],
                  rejoin=value["rejoin"],
                  rejoin_configuration_revision=value["strategy_configuration"]["revision"])


@pytest.mark.parametrize("question", ["出站预测", "进站后会落在哪", "where will i rejoin"])
def test_private_motion_stays_local_and_exact_queries_do_not_call_provider(question):
    state, value = published()
    value["private"] = "SYNTHETIC_PRIVATE_MARKER"
    context = build_live_context(value)
    plan = fallback_plan(context, "解释出站预测依据")
    assert "rejoin.assumptions" in plan["fact_ids"]
    encoded = json.dumps(context)
    assert all(word not in encoded for word in (
        "lap_profiles", "car_idx", "binding_sha256", "SYNTHETIC_PRIVATE_MARKER"))
    service = EngineerService(state.snapshot, environ={})
    try:
        assert live_query_intent(question) == "rejoin"
        assert service.submit(question)[0] == 202
        result = service.snapshot()
        assert result["requests_used"] == 0
        answer = result["answer"]
        assert answer["origin"] == "local_live" and not answer["stale"]
        assert len(answer["spoken_text"]) < 120
        assert "手填" in answer["spoken_text"] and "非指令" in answer["spoken_text"]
        assert "不是实测标定" in answer["text"] and "不能据此宣称出站畅通" in answer["text"]
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("boundary", ["lost", "config", "neighbor", "ttl", "stale"])
def test_answer_withdrawal_is_latched_and_short_lived(boundary):
    now = [10.]
    state, value = published(now=now)
    service = EngineerService(state.snapshot, clock=lambda: now[0], environ={})
    try:
        assert service.submit("出站预测")[0] == 202
        if boundary == "config":
            state.configure_strategy(parameters(pit_exit_fraction=.1))
            publish(state, value)
            assert state.snapshot()["rejoin"] is None  # Old in-flight job cannot publish.
        elif boundary in ("lost", "neighbor"):
            changed = copy.deepcopy(value)
            if boundary == "lost":
                changed["motion"] = unavailable_motion(changed["monitor"])
            else:
                changed["motion"]["opponents"][0]["car_idx"] = 2
            changed["rejoin"] = project_rejoin(changed)
            publish(state, changed)
            publish(state, value)  # Recovery before polling must not revive the old answer.
        elif boundary == "ttl":
            now[0] += 10.1
            publish(state, value)
        else:
            now[0] += 2.1
        answer = service.snapshot()["answer"]
        assert answer["stale"] and "spoken_text" not in answer
        assert "秒" not in answer["text"]
    finally:
        service.close(wait=True)


def test_normal_time_and_fuel_drift_does_not_cancel_a_short_answer():
    now = [10.]
    state, value = published(now=now, amount=20.8)
    service = EngineerService(state.snapshot, clock=lambda: now[0], environ={})
    try:
        assert service.submit("出站预测")[0] == 202
        for _ in range(5):
            now[0] += .1
            value["monitor"]["session_time_us"] += 100_000
            value["monitor"]["sequence"] += 1
            value["monitor"]["telemetry"]["lap_distance_pct"] += .001
            value["monitor"]["telemetry"]["fuel_level_l"] -= .002
            value["fuel"]["current_fuel_l"] -= .002
            value["fuel"]["fuel_shortfall_l"] += .002
            value["motion"].update(session_time_us=value["monitor"]["session_time_us"],
                                   monitor_sequence=value["monitor"]["sequence"])
            for actor in [value["motion"]["player"], *value["motion"]["opponents"]]:
                actor["progress_laps"] = round(actor["progress_laps"] + .001, 9)
            value["strategy"] = project_strategy(value["monitor"], value["fuel"], "Race",
                                                   parameters(),
                                                   value["strategy_configuration"]["revision"])
            value["rejoin"] = project_rejoin(value)
            assert value["rejoin"]["status"] == "READY"
            publish(state, value)
            assert not service.snapshot()["answer"]["stale"]
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("fault", ["startup", "feed", "snapshot", "projection"])
def test_rejoin_fault_is_visible_without_disabling_fuel_spotter_or_coaching(monkeypatch, fault):
    def broken(*_args, **_kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_ERROR")
    if fault == "startup":
        monkeypatch.setattr(live_app, "LiveMotionTracker", broken)
    elif fault == "projection":
        monkeypatch.setattr(live_app, "project_rejoin", broken)
    else:
        monkeypatch.setattr(LiveMotionTracker, fault, broken)
    now = [1.]
    state = live_app.AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    state.start_spotter(60)
    analysis = live_app._LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic",
                                      tick_rate=60, car_count=3, generation=state.generation,
                                      allowed=lambda: True)
    try:
        for frame in synthetic_frames(8):
            now[0] = frame.captured_monotonic_s
            state.feed_spotter(frame)
            analysis.process((frame, "Race", now[0], 1_200_000))
            if frame.buffer_tick >= 60:
                break
        value = state.snapshot()
        assert value["rejoin"]["status"] == "WAIT"
        assert "故障" in render_live_query(build_live_context(value), "rejoin")["spoken_text"]
        assert "fuel.current" in {item["id"] for item in build_live_context(value)["facts"]}
        assert value["spotter"]["status"] == "READY"
        assert value["driving"]["worker"]["failed"] is False
        assert "SYNTHETIC_PRIVATE" not in json.dumps(value)
    finally:
        analysis.close()
        state.connection("STOPPED")


@pytest.mark.parametrize("invalidate", [False, True])
def test_ptt_speaks_local_projection_unless_context_changed_while_synthesizing(invalidate):
    import threading

    from iracing_ai_engineer.voice_service import VoiceService

    helper = fixtures("voice_service")
    state, _ = published()
    entered, release = threading.Event(), threading.Event()
    audio, speech = helper.Audio(), helper.Speech()
    speech.recognize = lambda *_args, **_kwargs: {"text": "出站预测", "confidence": .9}

    def synthesize(text, **_kwargs):
        speech.synthesis.append(text)
        entered.set()
        assert release.wait(3)
        return b"SYNTHETIC_REJOIN_SPEECH"

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
            state.configure_strategy(None)
        release.set()
        helper.wait_for(lambda: voice.snapshot()["status"] == "READY")
        played = [wav for wav, _, _ in audio.plays if wav == b"SYNTHETIC_REJOIN_SPEECH"]
        assert len(played) == (0 if invalidate else 1)
        assert len(speech.synthesis) == 1 and "手填" in speech.synthesis[0]
        assert service.snapshot()["requests_used"] == 0
    finally:
        release.set()
        voice.close()
        service.close(wait=True)


def test_long_service_horizon_is_withdrawn_even_at_a_nearby_entry():
    value = evidence()
    value["strategy_configuration"]["inputs"].update(
        complete_pit_loss_low_s=500., complete_pit_loss_high_s=600.)
    config = StrategyParameters(**value["strategy_configuration"]["inputs"])
    value["strategy"] = project_strategy(value["monitor"], value["fuel"], "Race", config,
                                          value["strategy_configuration"]["revision"])
    result = project_rejoin(value)
    assert result["status"] == "WAIT"
    assert result["scenarios"][0]["reason_codes"] == ["FORECAST_TIME_HORIZON_EXCEEDED"]


@pytest.mark.parametrize("change", [
    {"OnPitRoad": True}, {"IsReplayPlaying": True}, {"PlayerCarMyIncidentCount": 1},
    {"SessionFlags": 8}, {"LapDistPct": .7}, {"CarIdxLapDistPct": [0., None, -1.]},
])
def test_motion_loses_readiness_on_pit_replay_incident_flag_teleport_or_missing_car(change):
    rig = Rig()
    rig.step()
    rig.step()
    value = rig.step(**change)
    assert value["motion"]["status"] == "WAIT"
    assert validated_motion(value) is None


def test_blocked_projection_holds_no_app_lock_and_stale_config_job_is_discarded(monkeypatch):
    import threading

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    now = [1.]
    state = live_app.AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    state.start_spotter(60)
    analysis = live_app._LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic",
                                      tick_rate=60, car_count=3, generation=state.generation,
                                      allowed=lambda: True)
    stream = synthetic_frames(8)
    for _ in range(35):
        frame = next(stream)
        now[0] = frame.captured_monotonic_s
        state.feed_spotter(frame)
        analysis.process((frame, "Race", now[0], 1_200_000))
    state.configure_strategy(parameters(tank_capacity_l=100.))
    original = live_app.project_rejoin

    def slow(value):
        entered.set()
        assert release.wait(3)
        return original(value)

    monkeypatch.setattr(live_app, "project_rejoin", slow)
    analysis._next_snapshot = 0
    frame = next(stream)
    now[0] = frame.captured_monotonic_s
    owner = threading.Thread(target=analysis.process, args=((frame, "Race", now[0], 1_200_000),))
    failures = []

    def independent():
        try:
            state.feed_spotter(frame)
            state.configure_strategy(parameters(tank_capacity_l=110.))
            assert state.snapshot()["spotter"]["status"] == "READY"
        except Exception as exc:
            failures.append(type(exc).__name__)
        finally:
            finished.set()

    probe = threading.Thread(target=independent)
    try:
        owner.start()
        assert entered.wait(1)
        probe.start()
        assert finished.wait(1) and not failures  # Not blocked by the projection owner's work.
        release.set()
        owner.join(2)
        assert not owner.is_alive() and state.snapshot()["rejoin"] is None
    finally:
        release.set()
        owner.join(3)
        if probe.ident is not None:
            probe.join(3)
        analysis.close()
        state.connection("STOPPED")


def test_native_projection_label_requires_current_verified_evidence():
    from iracing_ai_engineer.desktop_window import DesktopPresenter

    value = fixtures("desktop_window")._snapshot()
    value["telemetry"] = evidence()
    presenter = DesktopPresenter()
    assert "条件推演可用" in presenter.project(value, now=1.).rejoin
    value["telemetry"]["rejoin"]["scenarios"][0]["ahead"]["gap_range_s"][0] += 1
    assert "条件推演可用" not in presenter.project(value, now=1.1).rejoin
