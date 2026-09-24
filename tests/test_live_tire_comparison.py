"""Invented declarations and telemetry; no real model, service or race evidence."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace

import pytest
from test_live_car_context import metadata
from test_live_strategy import evidence
from test_live_tire_age import Rig
from test_llm_engineer import wait_answer
from test_tire_model_validation import pin, request_value, write_request

from iracing_ai_engineer.live_app import AppState
from iracing_ai_engineer.live_car_context import (
    CarMetadataProjector,
    LiveCarContext,
    car_context_binding,
)
from iracing_ai_engineer.live_queries import live_query_intent, render_live_query
from iracing_ai_engineer.live_strategy import StrategyParameters
from iracing_ai_engineer.live_tire_calibration import load_tire_calibration, validated_calibration
from iracing_ai_engineer.live_tire_comparison import (
    project_tire_comparison,
    tire_comparison_text,
    validated_tire_comparison,
)
from iracing_ai_engineer.llm_engineer import (
    EngineerConfig,
    EngineerService,
    fallback_plan,
    render_plan,
)
from iracing_ai_engineer.llm_evidence import build_live_context


@pytest.fixture
def runtime(tmp_path):
    rig, car, projector, meta = Rig(), LiveCarContext(), CarMetadataProjector(), metadata()
    state = AppState(clock=lambda: rig.tick / 60 + 1)
    state.connection("CONNECTED")
    rig.values.update(PlayerCarClass=27, TrackWetness=1, TrackTempCrew=30., AirTemp=21.,
                      WindVel=2., WindDir=1., Precipitation=0., FuelLevel=20.)

    def publish(**fuel_changes):
        frame = rig.frame
        car.feed(frame, projector.project(meta, frame.session_info_update, frame, "FULL"))
        monitor = rig.value["monitor"]
        fuel = evidence()["fuel"]  # Explicit synthetic fuel model, not learned SDK laps.
        fuel.update(fuel_changes)
        state.publish(monitor, fuel, None, "Race", tire_age=rig.tracker.snapshot(monitor),
                      car_context=car.snapshot(frame, monitor))
        return state.snapshot()

    rig.park()
    snapshot = publish()
    request = request_value.__wrapped__()
    setup = snapshot["car_context"]["identity"]["setup_sha256"]
    request["applicability"]["setup_sha256"] = setup
    for row in request["lap_contexts"]:
        row["setup_sha256"] = setup
    # Extend this invented fixture's actual observations, not a retained model's
    # bounds: its last measured fuel is 2 L and all times are regenerated.
    for split in ("training", "holdout"):
        for index, sample in enumerate(request[f"{split}_dataset"]["samples"]):
            early, late = sample["early_lap"], sample["late_lap"]
            late["fuel_start_l"] = 2.
            late["lap_time_s"] = (early["lap_time_s"] + (.20, .18, .22)[index]
                * (late["stint_age_laps"] - early["stint_age_laps"])
                + .03 * (late["fuel_start_l"] - early["fuel_start_l"]))
    pin(request)
    calibration = load_tire_calibration(write_request(request, tmp_path),
                                         expected_request_sha256=request["request_sha256"])
    state.set_tire_calibration(calibration,
        expected_binding=car_context_binding(snapshot, parked=True),
        expected_revision=snapshot["tire_calibration"]["revision"])
    rig.confirm()
    rig.exit()
    for lap in range(4, 10):
        rig.step(LapCompleted=lap)
    publish()
    state.configure_strategy(StrategyParameters(50., 2., 20., 24.,
        tire_change_time_s=10., fuel_tire_service_timing="PARALLEL"))
    return state, rig, publish, calibration


def test_production_state_combines_pinned_model_confirmed_origin_and_service(runtime):
    state, _, _, calibration = runtime
    snapshot = state.snapshot()
    value = validated_tire_comparison(snapshot)
    assert value is not None and value["status"] == "CONDITIONAL"
    assert value["model_sha256"] == calibration.model_sha256
    model = json.loads(calibration.model_json)
    low, high = model["performance_age_slope_uncertainty_s_per_lap"]
    first = value["scenarios"][0]
    assert first["current_counter_age"] == 6
    assert first["old_age_range"] == [7, 21] and first["new_age_range"] == [1, 15]
    assert first["extra_tire_service_s"] == 4
    assert first["performance_gain_range_s"] == pytest.approx([low * 90, high * 90], abs=1e-6)
    assert first["net_gain_range_s"] == pytest.approx([low * 90 - 4, high * 90 - 4], abs=1e-6)
    assert first["balance"] == "BENEFIT_EXCEEDS_SERVICE"
    assert value["physical_wear"] is None and value["race_recommendation"] is None
    assert value["live_acceptance"] is False and value["executable"] is False
    brief, details = tire_comparison_text(snapshot)
    assert "净收益" in brief and "不证明旧胎安全" in brief and len(details) == 2


@pytest.mark.parametrize("time,timing,balance", [
    (30., "PARALLEL", "SERVICE_EXCEEDS_BENEFIT"),
    (24., "PARALLEL", "UNCERTAIN"),
    (10., "SEQUENTIAL", "BENEFIT_EXCEEDS_SERVICE"),
])
def test_full_interval_not_midpoint_controls_balance_without_keep_advice(runtime,
                                                                       time, timing, balance):
    state, _, _, _ = runtime
    state.configure_strategy(StrategyParameters(50., 2., 20., 24.,
        tire_change_time_s=time, fuel_tire_service_timing=timing))
    value = state.snapshot()["tire_comparison"]
    assert value["scenarios"][0]["balance"] == balance
    assert value["race_recommendation"] is None
    assert "KEEP_TIRES" not in json.dumps(value)


@pytest.mark.parametrize("fault", ["origin", "model", "weather", "compound", "stale", "replay",
                                  "setup", "fuel", "ages", "service", "flags", "closed_pits"])
def test_unsupported_evidence_cannot_publish_a_numerical_comparison(runtime, fault):
    state, _, _, _ = runtime
    snapshot = state.snapshot()
    if fault == "origin":
        snapshot["tire_age"]["origin"] = None
        snapshot["tire_age"]["counter_increase"] = None
    elif fault == "model":
        model = snapshot["tire_calibration"]["selection"]["model"]
        model["performance_age_slope_s_per_lap"] += .1
    elif fault == "weather":
        snapshot["car_context"]["conditions"]["precipitation_pct"] = 1.
    elif fault == "compound":
        snapshot["monitor"]["telemetry"]["tire_compound"] = 1
    elif fault == "stale":
        snapshot["updated_age_s"] = 3.
    elif fault == "replay":
        snapshot["source_mode"] = "REPLAY"
    elif fault == "setup":
        snapshot["car_context"]["identity"]["setup_sha256"] = "f" * 64
    elif fault == "fuel":
        cal = state._tire_calibration
        state._tire_calibration = replace(cal, fuel_bounds=(40., 82.))
        snapshot = state.snapshot()
    elif fault == "ages":
        # A different real published age, not tampering with an origin hash.
        _, rig, publish, _ = runtime
        for lap in range(10, 20):
            rig.step(LapCompleted=lap)
        snapshot = publish()
    elif fault == "service":
        state.configure_strategy(StrategyParameters(50., 2., 20., 24.))
        snapshot = state.snapshot()
    elif fault == "flags":
        snapshot["monitor"]["telemetry"]["session_flags"] = 8
    else:
        snapshot["monitor"]["telemetry"]["pits_open"] = False
    value = project_tire_comparison(snapshot)
    assert value["status"] == "WAIT"
    assert all(row["net_gain_range_s"] is None for row in value["scenarios"])


def test_recomputed_surface_refuses_edited_gain_or_promoted_acceptance(runtime):
    original = runtime[0].snapshot()
    for key, value in (("physical_wear", 50), ("live_acceptance", True),
                       ("race_recommendation", "KEEP_TIRES")):
        changed = copy.deepcopy(original)
        changed["tire_comparison"][key] = value
        assert validated_tire_comparison(changed) is None
    original["tire_comparison"]["scenarios"][0]["net_gain_range_s"] = [99., 100.]
    assert validated_tire_comparison(original) is None
    assert not any(row["id"].startswith("tire.comparison_")
                   for row in build_live_context(original)["facts"])


def test_exact_local_question_and_normal_tire_question_share_grounded_facts(runtime):
    snapshot = runtime[0].snapshot()
    context = build_live_context(snapshot)
    assert live_query_intent("换胎值得吗？") == "tire_comparison"
    for intent in ("tire_comparison", "tire"):
        answer = render_live_query(context, intent)
        assert "净收益" in answer["spoken_text"]
        assert "不证明旧胎安全" in answer["spoken_text"]
        assert "天气" in answer["text"] and "赛事规则" in answer["text"]
        assert len(answer["spoken_text"]) <= 280


def test_local_answer_withdraws_on_calibration_clear_without_provider(runtime):
    state, _, _, _ = runtime
    service = EngineerService(state.snapshot, config=EngineerConfig())
    try:
        assert service.submit("比较换胎收益")[0] == 202
        answer = service.snapshot()["answer"]
        assert answer is not None and "净收益" in answer["spoken_text"]
        assert service.snapshot()["requests_used"] == 0
        state.set_tire_calibration(None)
        answer = service.snapshot()["answer"]
        assert answer["stale"] is True and "spoken_text" not in answer
        assert "净收益" not in answer["text"]
    finally:
        service.close()


@pytest.mark.parametrize("change", ["weather", "new_set", "lap", "pits", "flag", "time", "service"])
def test_numerical_voice_answer_is_revoked_and_cannot_revive(runtime, change):
    state, rig, publish, _ = runtime
    now = [0.]
    service = EngineerService(state.snapshot, clock=lambda: now[0], environ={})
    try:
        assert service.submit("轮胎怎么样")[0] == 202
        assert not service.snapshot()["answer"]["stale"]
        if change == "time":
            now[0] = 11.
        elif change == "service":
            state.configure_strategy(StrategyParameters(50., 2., 20., 24.,
                tire_change_time_s=30., fuel_tire_service_timing="PARALLEL"))
        else:
            rig.step(**{"weather": {"WindVel": 20.}, "new_set": {"TireSetsUsed": 1},
                        "lap": {"LapCompleted": 10}, "pits": {"PitsOpen": False},
                        "flag": {"SessionFlags": 8}}[change])
            publish()
        answer = service.snapshot()["answer"]
        assert answer["stale"] and "spoken_text" not in answer
        rig.step(WindVel=2., PitsOpen=True, SessionFlags=0)
        publish()
        assert service.snapshot()["answer"]["stale"]
    finally:
        service.close()


def test_unrelated_opponent_changes_do_not_withdraw_a_tire_only_answer(runtime):
    state, rig, publish, _ = runtime
    service = EngineerService(state.snapshot, environ={})
    try:
        service.submit("比较换胎收益")
        # Tire comparison does not claim any traffic-sensitive rejoin.
        rig.step(CarIdxLapDistPct=[0., .01, .02], CarLeftRight=2)
        publish()
        assert not service.snapshot()["answer"]["stale"]
    finally:
        service.close()


def test_arbitrary_question_fallback_and_provider_cannot_omit_limits(runtime):
    context = build_live_context(runtime[0].snapshot())
    plan = fallback_plan(context, "如果未来天气不变，轮胎时间收益怎么估算的")
    assert "tire.comparison_early" in plan["fact_ids"]
    # A model selects a number but attempts to omit assumptions/notices.
    text = render_plan({"topic": "strategy", "fact_ids": ["tire.comparison_early"],
                        "notice_ids": []}, context)
    assert "净收益" in text and "不证明旧胎安全" in text and "天气" in text
    encoded = json.dumps(context, ensure_ascii=False)
    assert "source_receipt_sha256" not in encoded and "setup_sha256" not in encoded


@pytest.mark.parametrize("fault", ["bounds", "bool", "extra", "oversized", "nested", "nan"])
def test_calibration_projection_has_strict_bounded_shape(runtime, fault):
    value = runtime[0].snapshot()
    assert validated_calibration(value) is not None
    selection = value["tire_calibration"]["selection"]
    if fault == "bounds":
        selection["age_bounds"] = [1, 23]  # Conflicts with pinned model.
    elif fault == "bool":
        selection["age_bounds"][0] = True
    elif fault == "extra":
        selection["extra"] = "untrusted"
    elif fault == "oversized":
        selection["model"]["physical_wear"] = "x" * 100_000
    elif fault == "nested":
        selection["model"]["physical_wear"] = selection  # Cycle is bounded pre-serialization.
    else:
        selection["fuel_bounds"][1] = float("nan")
    assert validated_calibration(value) is None


def test_model_work_does_not_hold_spotter_lock_and_fault_is_isolated(runtime, monkeypatch):
    import iracing_ai_engineer.live_app as app
    state = runtime[0]
    entered, done, probe = threading.Event(), threading.Event(), []

    def projection(_):
        entered.set()
        assert done.wait(2)
        raise RuntimeError("invented private exception must not escape")

    def spotter_probe():
        assert entered.wait(2)
        probe.append(state.spotter_snapshot())
        done.set()

    monkeypatch.setattr(app, "project_tire_comparison", projection)
    worker = threading.Thread(target=spotter_probe)
    worker.start()
    value = state.snapshot()
    worker.join(3)
    assert probe and not worker.is_alive()
    assert value["tire_comparison"]["reason"] == "PROCESSING_ERROR"
    context = build_live_context(value)
    assert any(row["id"] == "fuel.current" for row in context["facts"])
    assert "轮胎收益计算故障" in render_live_query(context, "tire_comparison")["text"]


def test_complete_stint_domain_is_required_not_a_partial_supported_prefix(runtime):
    state, rig, publish, _ = runtime
    rig.step(LapCompleted=10)
    rig.step(LapCompleted=11)
    value = publish()
    assert value["tire_comparison"]["status"] == "WAIT"
    assert all(row["reason"] == "PROJECTED_AGES_OUTSIDE_MODEL_DOMAIN"
               and row["net_gain_range_s"] is None for row in value["tire_comparison"]["scenarios"])


def test_one_supported_scenario_never_borrows_the_other_scenarios_numbers(runtime):
    state, _, _, calibration = runtime
    state._tire_calibration = replace(calibration, fuel_bounds=(2., 31.))
    value = state.snapshot()
    first, second = value["tire_comparison"]["scenarios"]
    assert first["reason"] == "PROJECTED_FUEL_OUTSIDE_MODEL_DOMAIN"
    assert first["net_gain_range_s"] is None and second["status"] == "CONDITIONAL"
    brief, details = tire_comparison_text(value)
    assert "再跑 9 整圈" in brief and "超出" in details[0][1]


def test_no_fuel_stop_does_not_invent_a_tire_only_stop(runtime):
    _, _, publish, _ = runtime
    value = publish(race_laps_to_go=5, fuel_needed_to_finish_l=12., fuel_shortfall_l=0.)
    assert value["tire_comparison"]["reason"] == "NO_FUEL_STOP_IN_BUDGET"


def test_new_tire_warmup_laps_must_be_inside_observed_age_range(runtime):
    state, _, _, calibration = runtime
    state._tire_calibration = replace(calibration, age_bounds=(2, 22))
    value = state.snapshot()["tire_comparison"]
    assert value["status"] == "WAIT"
    assert all(row["reason"] == "PROJECTED_AGES_OUTSIDE_MODEL_DOMAIN" for row in value["scenarios"])


def test_late_provider_fact_selection_cannot_restore_cleared_calibration(runtime):
    state = runtime[0]
    entered, release = threading.Event(), threading.Event()

    class Planner:
        def complete(self, context, question):
            entered.set()
            assert release.wait(2)
            return {"topic": "strategy", "fact_ids": ["tire.comparison_early"], "notice_ids": []}

    service = EngineerService(state.snapshot, EngineerConfig(provider="deepseek"),
                              client=Planner(), environ={})
    try:
        assert service.submit("解释这组换胎模型收益的假设")[0] == 202
        assert entered.wait(2)
        state.set_tire_calibration(None)
        release.set()
        answer = wait_answer(service)["answer"]
        assert answer["stale"] and "净收益" not in answer["text"]
    finally:
        release.set()
        service.close(wait=True)
