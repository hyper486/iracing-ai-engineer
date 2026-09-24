"""Synthetic numeric comparisons, lifecycle and voice; no game or live acceptance."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from iracing_ai_engineer.fuel import FuelLapSample, estimate_fuel_strategy
from iracing_ai_engineer.live_app import AppState
from iracing_ai_engineer.live_queries import live_query_intent, render_live_query
from iracing_ai_engineer.live_strategy import (
    StrategyParameters,
    project_strategy,
    strategy_notice,
    validated_strategy,
)
from iracing_ai_engineer.llm_engineer import EngineerConfig, EngineerService, fallback_plan
from iracing_ai_engineer.llm_evidence import build_live_context


def fixtures(name):
    spec = importlib.util.spec_from_file_location(
        f"_live_strategy_{name}", Path(__file__).with_name(f"test_{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def evidence(*, amount=20.0, burn=2.0, reserve=2.0, remaining=15):
    value = fixtures("live_queries").snapshot()
    value["monitor"]["session_time_us"] = 120_000_000
    value["monitor"]["telemetry"].update(player_car_idx=0, fuel_level_l=amount,
                                        pits_open=True, session_flags=0)
    value["fuel"].update(current_fuel_l=amount, conservative_burn_l_per_lap=burn,
                         reserve_l=reserve, race_laps_to_go=remaining,
                         fuel_needed_to_finish_l=burn * remaining + reserve if remaining else 0,
                         tank_capacity_l=None, minimum_stops=None)
    value["fuel"]["fuel_shortfall_l"] = max(
        0.0, value["fuel"]["fuel_needed_to_finish_l"] - amount)
    return value


def configured(*, parameters=None, now=None, **kwargs):
    now = now if now is not None else [10.0]
    value = evidence(**kwargs)
    state = AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    state.publish(value["monitor"], value["fuel"], None, "Race")
    state.configure_strategy(parameters or StrategyParameters(50.0, 2.0, 20.0, 24.0))
    return state, value


def test_one_stop_uses_next_fill_not_cumulative_or_max_fill():
    state, _ = configured()
    value = state.snapshot()
    result = validated_strategy(value)
    assert result is not None and result["status"] == "READY"
    assert result["plan"]["fuel_stops"] == 1
    early, late = result["scenarios"]
    assert (early["laps_from_now"], late["laps_from_now"]) == (0, 9)
    assert early["target_fuel_l"] == 32 and late["target_fuel_l"] == 14
    assert early["next_stint_laps"] == 15 and late["next_stint_laps"] == 6
    assert early["fuel_add_l"] == late["fuel_add_l"] == 12
    assert early["pumping_time_s"] == 6
    assert early["total_loss_range_s"] == [26, 30]
    assert result["rejoin"] == result["tire_service"] == "UNAVAILABLE"
    assert result["parameters_provenance"] == "USER_RULE"
    assert result["event_rules"] == "UNVERIFIED" and result["live_acceptance"] is False


def test_multistop_next_dose_is_not_total_deficit():
    state, _ = configured(remaining=60)
    result = validated_strategy(state.snapshot())
    assert result["plan"]["fuel_stops"] == 3
    assert result["scenarios"][0]["fuel_add_l"] == 6
    assert result["scenarios"][0]["next_stint_laps"] == 12
    assert result["scenarios"][0]["further_stops"] == 2
    assert state.snapshot()["fuel"]["fuel_shortfall_l"] == 102


@pytest.mark.parametrize("capacity,amount,reserve,burn,remaining", [
    (50.0, 20.0, 2.0, 2.0, 60), (10.0, 4.0, 1.0, 2.0, 12),
    (50.0, 20.0, 2.0, 2.0, 0), (50.0, 20.0, 2.0, 2.0, 9),
    (50.0, 2.0, 2.0, 2.0, 5), (12.0, 12.0, 2.0, 2.0, 10),
    (4.0, 3.99, 0.0, 3.99, 2), (1.0, .2, 0.0, .3, 100000),
])
def test_shared_arithmetic_matches_offline_estimator(capacity, amount, reserve, burn, remaining):
    state, _ = configured(parameters=StrategyParameters(capacity), amount=amount,
                           reserve=reserve, burn=burn, remaining=remaining)
    live = validated_strategy(state.snapshot())
    offline = estimate_fuel_strategy(
        [FuelLapSample(burn, lap_time_s=100.0) for _ in range(5)],
        current_fuel_l=amount, tank_capacity_l=capacity, reserve_l=reserve,
        remaining_laps=remaining, refuel_rate_l_per_s=2.0)
    assert live["status"] == "READY"
    assert live["plan"]["fuel_stops"] == offline.minimum_pit_stops.value
    assert len(live["scenarios"]) <= 2
    for scenario in live["scenarios"]:
        assert scenario["arrival_fuel_l"] >= reserve - 1e-6
        assert scenario["target_fuel_l"] <= capacity + 1e-6
        assert scenario["target_fuel_l"] >= reserve + burn * scenario["next_stint_laps"] - 1e-6
        assert scenario["fuel_add_l"] >= 0
        assert scenario["pumping_time_s"] is None
        assert scenario["total_loss_range_s"] is None


@pytest.mark.parametrize("parameters", [
    {"tank_capacity_l": 0}, {"tank_capacity_l": True}, {"tank_capacity_l": float("nan")},
    {"tank_capacity_l": 1001}, {"tank_capacity_l": 10**1000},
    {"tank_capacity_l": 50, "refuel_rate_l_per_s": 0},
    {"tank_capacity_l": 50, "refuel_rate_l_per_s": float("inf")},
    {"tank_capacity_l": 50, "pit_loss_low_s": 20},
    {"tank_capacity_l": 50, "pit_loss_high_s": 25},
    {"tank_capacity_l": 50, "pit_loss_low_s": 30, "pit_loss_high_s": 20},
])
def test_invalid_parameters_rejected(parameters):
    with pytest.raises(ValueError, match="STRATEGY_PARAMETERS_INVALID"):
        StrategyParameters(**parameters)


@pytest.mark.parametrize("change,reason", [
    ("replay", "SOURCE_NOT_READY"), ("no_sequence", "SOURCE_NOT_READY"),
    ("practice", "RACE_HORIZON_UNAVAILABLE"), ("capacity", "CAPACITY_CONTRADICTS_FUEL"),
    ("missing_fuel", "FUEL_MODEL_UNAVAILABLE"), ("contradiction", "FUEL_MODEL_UNAVAILABLE"),
    ("pit", "FUEL_MODEL_UNAVAILABLE"), ("no_horizon", "RACE_HORIZON_UNAVAILABLE"),
    ("below_reserve", "ALREADY_BELOW_RESERVE"), ("unusable_tank", "TANK_CANNOT_COVER_A_LAP"),
])
def test_missing_or_contradictory_inputs_never_create_scenarios(change, reason):
    value = evidence()
    capacity, session_type = 50.0, "Race"
    monitor, fuel = value["monitor"], value["fuel"]
    if change == "replay":
        monitor["context"]["sim_source_mode"] = "REPLAY"
    elif change == "no_sequence":
        monitor.pop("sequence")
    elif change == "practice":
        session_type = "Practice"
    elif change == "capacity":
        capacity = 10.0
    elif change == "missing_fuel":
        monitor["reasons"] = ["READ_ERROR:FuelLevel"]
    elif change == "contradiction":
        fuel["current_fuel_l"] = 10.0
    elif change == "pit":
        monitor["interval_invalid_for_fuel"] = ["PIT_OR_OUT_LAP"]
    elif change == "no_horizon":
        fuel["fuel_needed_to_finish_l"] = 31.0
    elif change == "below_reserve":
        fuel["current_fuel_l"] = monitor["telemetry"]["fuel_level_l"] = 1.0
    elif change == "unusable_tank":
        capacity = 3.0
        fuel["current_fuel_l"] = monitor["telemetry"]["fuel_level_l"] = 3.0
    value = project_strategy(monitor, fuel, session_type, StrategyParameters(capacity), 1)
    assert value["reason"] == reason and value["status"] == "WAIT"
    assert value["plan"] is None and value["scenarios"] == []


@pytest.mark.parametrize("change", ["boolean", "number", "free_text", "scope", "inputs", "time",
                                   "scope_alias", "provenance"])
def test_projection_tampering_rejected(change):
    state, _ = configured()
    value = state.snapshot()
    if change == "boolean":
        value["strategy"]["executable"] = 0
    elif change == "number":
        value["strategy"]["scenarios"][0]["fuel_add_l"] += 1
    elif change == "free_text":
        value["strategy"]["private"] = "SYNTHETIC_PRIVATE_MARKER"
    elif change == "scope":
        value["monitor"]["telemetry"]["player_car_idx"] = 1
    elif change == "inputs":
        value["strategy_configuration"]["inputs"]["refuel_rate_l_per_s"] = 3.0
    elif change == "time":
        value["monitor"]["session_time_us"] += 1
    elif change == "scope_alias":
        value["strategy_configuration"]["scope"][0] = True
    elif change == "provenance":
        value["strategy_configuration"]["provenance"] = "SDK_DIRECT"
    assert validated_strategy(value) is None
    assert not any(fact["id"].startswith("strategy.")
                   for fact in build_live_context(value)["facts"])
    assert "PRIVATE" not in json.dumps(build_live_context(value))


@pytest.mark.parametrize("boundary", ["disconnect", "time", "session", "player", "source",
                                     "out_of_car", "interval", "analysis", "session_type"])
def test_config_is_revoked_and_does_not_revive_after_source_boundary(boundary):
    now = [10.0]
    state, value = configured(now=now)
    original = copy.deepcopy(value)
    revision = state.snapshot()["strategy_configuration"]["revision"]
    if boundary == "disconnect":
        state.connection("DISCONNECTED")
        state.connection("CONNECTED")
    elif boundary == "time":
        now[0] += 2.1
        assert state.snapshot()["strategy"] is None
    elif boundary == "analysis":
        state.invalidate_analysis(state.generation)
    else:
        if boundary == "session":
            value["monitor"]["telemetry"]["session_num"] = 1
        elif boundary == "player":
            value["monitor"]["telemetry"]["player_car_idx"] = 1
        elif boundary == "source":
            value["monitor"]["binding_sha256"] = "b" * 64
        elif boundary == "out_of_car":
            value["monitor"]["context"]["player_control_state"] = "OUT_OF_CAR"
        elif boundary == "interval":
            value["monitor"]["interval_invalid_for_fuel"] = ["OUT_OF_CAR_INTERVAL"]
        state.publish(value["monitor"], value["fuel"], None,
                      "Practice" if boundary == "session_type" else "Race")
    state.publish(original["monitor"], original["fuel"], None, "Race")
    latest = state.snapshot()
    assert latest["strategy_configuration"]["status"] == "UNCONFIGURED"
    assert latest["strategy_configuration"]["revision"] > revision
    assert latest["strategy"]["reason"] == "PARAMETERS_NOT_CONFIRMED"


def test_unconfigured_source_cannot_accept_parameters_and_clear_is_allowed():
    state = AppState()
    with pytest.raises(ValueError, match="STRATEGY_SOURCE_NOT_READY"):
        state.configure_strategy(StrategyParameters(50))
    state.configure_strategy(None)
    assert state.snapshot()["strategy_configuration"]["persisted"] is False


def test_projection_fault_is_latched_without_disabling_other_modules(monkeypatch):
    from iracing_ai_engineer import live_app

    state, value = configured()
    projector = live_app.project_strategy

    def fail(*_args):
        raise RuntimeError("SYNTHETIC_PRIVATE_MARKER")

    monkeypatch.setattr(live_app, "project_strategy", fail)
    state.publish(value["monitor"], value["fuel"], None, "Race", traffic={"test": True})
    result = state.snapshot()
    assert result["connection"] == "CONNECTED" and result["fuel"]["status"] == "READY"
    assert result["traffic"] == {"test": True} and "故障" in strategy_notice(result)
    assert "PRIVATE" not in json.dumps(result)
    monkeypatch.setattr(live_app, "project_strategy", projector)
    state.publish(value["monitor"], value["fuel"], None, "Race")
    assert state.snapshot()["strategy"]["status"] == "ERROR"
    state.configure_strategy(StrategyParameters(50))
    assert validated_strategy(state.snapshot())["status"] == "READY"
    assert "暂不可用" in strategy_notice({"strategy": {"reason": []}})


@pytest.mark.parametrize("question", ["比较进站方案", "进站窗口", "这次进站加多少油", "该进站了吗"])
def test_exact_strategy_questions_are_local_and_keep_assumptions(question):
    state, _ = configured()
    service = EngineerService(state.snapshot, EngineerConfig(provider="deepseek"), environ={})
    try:
        assert live_query_intent(question) in ("pit", "pit_plan")
        assert service.submit(question)[0] == 202
        result = service.snapshot()
        answer = result["answer"]
        assert result["requests_used"] == 0 and answer["origin"] == "local_live"
        assert "12.0 升" in answer["text"] and "26.0 至 30.0 秒" in answer["text"]
        assert "整圈后" in answer["spoken_text"] and "未定位进站口" in answer["spoken_text"]
        assert "手填" in answer["spoken_text"] and "非进站指令" in answer["spoken_text"]
        assert len(answer["spoken_text"]) <= 90
        plan = fallback_plan(build_live_context(state.snapshot()), "解释进站比较的依据")
        assert "strategy.window" in plan["fact_ids"] and len(plan["fact_ids"]) <= 6
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("boundary", ["config", "horizon", "refuel", "lap", "ttl", "clear"])
def test_pending_answer_cannot_survive_configuration_or_plan_changes(boundary):
    now = [10.0]
    state, value = configured(now=now)
    service = EngineerService(state.snapshot, clock=lambda: now[0], environ={})
    try:
        assert service.submit("比较进站方案")[0] == 202
        assert service.snapshot()["answer"]["stale"] is False
        if boundary == "config":
            state.configure_strategy(StrategyParameters(45))
        elif boundary == "clear":
            state.configure_strategy(None)
        else:
            if boundary == "horizon":
                value["fuel"].update(race_laps_to_go=14, fuel_needed_to_finish_l=30.0)
            elif boundary == "refuel":
                value["fuel"]["current_fuel_l"] += 1
                value["monitor"]["telemetry"]["fuel_level_l"] += 1
            elif boundary == "lap":
                value["monitor"]["telemetry"]["lap_number"] += 1
            elif boundary == "ttl":
                # Keep every publication fresh; isolate the answer's 10 s TTL.
                for _ in range(20):
                    now[0] += .5
                    state.publish(value["monitor"], value["fuel"], None, "Race")
                now[0] += .1
            state.publish(value["monitor"], value["fuel"], None, "Race")
        assert service.snapshot()["answer"]["stale"] is True
    finally:
        service.close(wait=True)


def test_parameters_and_comparison_are_not_required_for_observed_fuel_or_traffic():
    state, _ = configured()
    state.configure_strategy(None)
    context = build_live_context(state.snapshot())
    answer = render_live_query(context, "pit_plan")
    assert "容量" in answer["spoken_text"] and answer["fact_ids"] == []
    assert "20.00 升" in render_live_query(context, "amount")["spoken_text"]
    assert "允许本人进站" in render_live_query(context, "pit_permission")["spoken_text"]


def test_controller_applies_without_sdk_restart_provider_restart_or_disk_write(tmp_path):
    from iracing_ai_engineer.desktop_controller import DesktopController
    from iracing_ai_engineer.desktop_settings import DesktopSettings

    helper = fixtures("desktop_controller")
    store = helper.Store(tmp_path, DesktopSettings(recording_enabled=False))
    reader = helper.Reader()
    controller = DesktopController(store=store, reader=reader, environ={})
    try:
        controller.start()
        assert reader.entered.wait(1)
        value = evidence()
        controller._state.publish(value["monitor"], value["fuel"], None, "Race")
        original_service = controller._service
        controller.configure_strategy(asdict(StrategyParameters(50)))
        assert len(reader.calls) == 1 and store.saved == []
        assert controller._service is original_service
        assert controller.snapshot()["telemetry"]["strategy"]["status"] == "READY"
        controller.configure_strategy(None)
        assert controller.snapshot()["telemetry"]["strategy"]["scenarios"] == []
    finally:
        controller.close()
        helper.wait_for(controller.is_closed)


@pytest.mark.parametrize("invalidate", [False, True])
def test_ptt_speaks_local_comparison_unless_parameters_changed_during_synthesis(invalidate):
    import threading

    from iracing_ai_engineer.voice_service import VoiceService

    helper = fixtures("voice_service")
    state, _ = configured()
    entered, release = threading.Event(), threading.Event()
    audio, speech = helper.Audio(), helper.Speech()
    speech.recognize = lambda *_args, **_kwargs: {"text": "比较进站方案", "confidence": .9}

    def synthesize(text, **_kwargs):
        speech.synthesis.append(text)
        entered.set()
        assert release.wait(3)
        return b"SYNTHETIC_SPEECH"

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
        played = [wav for wav, _, _ in audio.plays if wav == b"SYNTHETIC_SPEECH"]
        assert len(played) == (0 if invalidate else 1)
        assert len(speech.synthesis) == 1 and "12.0 升" in speech.synthesis[0]
        assert service.snapshot()["requests_used"] == 0
    finally:
        release.set()
        voice.close()
        service.close(wait=True)


def test_local_stop_count_uses_complete_lap_budget_when_confirmed():
    state, _ = configured(remaining=60)
    answer = render_live_query(build_live_context(state.snapshot()), "stops")
    assert answer["fact_ids"] == ["strategy.window"]
    assert "3 停" in answer["spoken_text"] and "手填" in answer["spoken_text"]


def test_comparison_failure_has_a_fixed_local_voice_explanation(monkeypatch):
    from iracing_ai_engineer import live_app

    state, value = configured()
    monkeypatch.setattr(live_app, "project_strategy", lambda *_args: 1 / 0)
    state.publish(value["monitor"], value["fuel"], None, "Race")
    answer = render_live_query(build_live_context(state.snapshot()), "pit_plan")
    assert "故障" in answer["spoken_text"] and not answer["fact_ids"]


def test_synthetic_sdk_reader_publishes_comparison_from_the_real_learned_model(monkeypatch):
    helper = fixtures("live_app_reader")

    class ConfiguredState(helper.ObservedState):
        def __init__(self, clock):
            super().__init__(clock)
            self.applied = False

        def publish(self, *args, **kwargs):
            super().publish(*args, **kwargs)
            if not self.applied:
                try:
                    self.configure_strategy(StrategyParameters(50, 2, 20, 24))
                except ValueError:
                    return
                self.applied = True

    monkeypatch.setattr(helper, "ObservedState", ConfiguredState)
    state, [transport], _ = helper._run([{"frame_count": 1800, "session_type": "Race"}])
    admitted = [row for row in state.publications
                if row.get("strategy", {}).get("status") == "READY"]
    assert state.applied and admitted and transport.read_count == 1800
    result = validated_strategy(admitted[-1])
    assert result is not None and result["plan"]["burn_l_per_lap"] == pytest.approx(2.0)
    assert result["plan"]["horizon_basis"] == "TIMER_FASTEST_LAP_PLUS_MARGIN"
    assert result["plan"]["fuel_stops"] > 1
    assert "手填" in render_live_query(build_live_context(admitted[-1]), "pit_plan")["spoken_text"]
    assert state.snapshot()["strategy_configuration"]["status"] == "UNCONFIGURED"


@pytest.mark.parametrize("case", ["closed", "penalty", "caution", "no_stop"])
def test_short_comparison_keeps_pit_flags_and_no_stop_limits(case):
    state, value = configured(remaining=9 if case == "no_stop" else 15)
    if case == "closed":
        value["monitor"]["telemetry"]["pits_open"] = False
    elif case == "penalty":
        value["monitor"]["telemetry"]["session_flags"] = 0x00010000
    elif case == "caution":
        value["monitor"]["telemetry"]["session_flags"] = 0x0008
    state.publish(value["monitor"], value["fuel"], None, "Race")
    text = render_live_query(build_live_context(state.snapshot()), "pit_plan")["spoken_text"]
    assert len(text) <= 100
    assert {"closed": "不允许进站", "penalty": "处罚", "caution": "黄红旗",
            "no_stop": "未核验赛事强制进站"}[case] in text


def test_normal_fuel_decrease_does_not_continually_cancel_a_comparison():
    now = [10.0]
    state, value = configured(now=now, amount=20.8)
    service = EngineerService(state.snapshot, clock=lambda: now[0], environ={})
    try:
        assert service.submit("比较进站方案")[0] == 202
        for _ in range(3):
            now[0] += .5
            value["fuel"]["current_fuel_l"] -= .1
            value["fuel"]["fuel_shortfall_l"] += .1
            value["monitor"]["telemetry"]["fuel_level_l"] -= .1
            value["monitor"]["sequence"] += 1
            value["monitor"]["session_time_us"] += 500_000
            state.publish(value["monitor"], value["fuel"], None, "Race")
            assert service.snapshot()["answer"]["stale"] is False
    finally:
        service.close(wait=True)


def test_plan_boundary_between_polls_invalidates_even_after_apparent_recovery():
    state, value = configured()
    service = EngineerService(state.snapshot, environ={})
    try:
        assert service.submit("比较进站方案")[0] == 202
        changed = copy.deepcopy(value)
        changed["fuel"].update(race_laps_to_go=14, fuel_needed_to_finish_l=30.0,
                               fuel_shortfall_l=10.0)
        state.publish(changed["monitor"], changed["fuel"], None, "Race")
        state.publish(value["monitor"], value["fuel"], None, "Race")
        assert service.snapshot()["answer"]["stale"] is True
    finally:
        service.close(wait=True)
