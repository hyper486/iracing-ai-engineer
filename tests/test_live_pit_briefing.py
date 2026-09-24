"""Invented streaming-owner evidence only; no SDK, provider or physical audio."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace

import pytest

from iracing_ai_engineer.live_pit_briefing import (
    LIMITS,
    _covered_tires,
    pit_briefing_binding,
    pit_briefing_text,
    pit_briefing_voice_facts,
    project_pit_briefing,
)
from iracing_ai_engineer.live_queries import live_query_intent, render_live_query
from iracing_ai_engineer.live_rejoin import project_rejoin
from iracing_ai_engineer.live_strategy import StrategyParameters, project_strategy
from iracing_ai_engineer.live_tire_age import validated_tire_age
from iracing_ai_engineer.live_tire_calibration import validated_calibration
from iracing_ai_engineer.live_tire_comparison import project_tire_comparison
from iracing_ai_engineer.llm_engineer import EngineerService, fallback_plan, render_plan
from iracing_ai_engineer.llm_evidence import build_live_context
from iracing_ai_engineer.synthetic_tire_comparison import _case, run_synthetic_pit_briefing


@pytest.fixture(scope="module")
def learned():
    with _case(mapped=True) as (state, _, _):
        yield state.snapshot()


@pytest.fixture
def snapshot(learned):
    return copy.deepcopy(learned)


def reproject(value, **parameters):
    config = value["strategy_configuration"]
    config["inputs"] = asdict(StrategyParameters(**{**config["inputs"], **parameters}))
    config["revision"] += 1
    value["strategy"] = project_strategy(value["monitor"], value["fuel"], "Race",
        StrategyParameters(**config["inputs"]), config["revision"])
    value["rejoin"] = project_rejoin(value)
    value["tire_comparison"] = project_tire_comparison(value)
    return project_pit_briefing(value)


def test_streamed_owners_produce_same_action_not_whole_lap_endpoint(snapshot):
    value = project_pit_briefing(snapshot)
    assert value["status"] == "CONDITIONAL" and len(value["actions"]) == 2
    early, late = value["actions"]
    assert early["action_id"] != late["action_id"]
    assert early["next_stint_laps"] != snapshot["strategy"]["scenarios"][0]["next_stint_laps"]
    tire = early["tires"]
    assert tire["complete_interval_laps"] == [10, 23] and tire["complete_laps"] == 13
    assert tire["new_age_range"] == [2, 14] and tire["old_age_range"] == [11, 23]
    assert tire["unmodeled_partial_laps"] == pytest.approx(1.051367399)
    slope = json.loads(validated_calibration(snapshot).model_json)[
        "performance_age_slope_uncertainty_s_per_lap"]
    assert tire["covered_gain_range_s"] == pytest.approx([n * 9 * 13 for n in slope], abs=1e-6)
    assert tire["covered_gain_range_s"] != snapshot["tire_comparison"]["scenarios"][0][
        "performance_gain_range_s"]
    for index, action in enumerate(value["actions"]):
        for offset, service in enumerate(action["services"]):
            original = snapshot["rejoin"]["scenarios"][index * 2 + offset]
            assert service == {key: original[key] for key in service}
        assert action["tires"]["net_stint_gain_range_s"] is None
    assert value["physical_wear"] is None and value["race_recommendation"] is None
    assert value["live_acceptance"] is False and value["executable"] is False


@pytest.mark.parametrize("entry,exit_,first,new_start", [
    (.9, .1, 10, 2), (.9, 0., 9, 1), (.8, .95, 9, 2), (.1, .4, 10, 2),
])
def test_wrap_and_exact_finish_line_have_distinct_counter_conventions(
    snapshot, entry, exit_, first, new_start,
):
    value = reproject(snapshot, pit_entry_fraction=entry, pit_exit_fraction=exit_)
    row = value["actions"][0]
    tire = row["tires"]
    assert tire["complete_interval_laps"][0] == first
    assert tire["new_age_range"][0] == new_start
    assert tire["complete_laps"] + tire["unmodeled_partial_laps"] == pytest.approx(
        tire["budget_end_progress_laps"] - row["exit_progress_laps"])


@pytest.mark.parametrize("end,exit_,count,partial", [
    (10., 9., 1, 0.), (10., 9.1, 0, .9), (9.8, 9.1, 0, .7),
    (9., 9.1, 0, 0.), (10.000000001, 9.000000001, 0, 1.),
    (11., 9.000000001, 1, .999999999),
])
def test_only_wholly_covered_laps_not_double_counted_fragments(
    snapshot, end, exit_, count, partial,
):
    action = project_pit_briefing(snapshot)["actions"][0]
    action.update(exit_progress_laps=exit_, next_stint_laps=end - action["entry_progress_laps"])
    value = _covered_tires(snapshot, action, snapshot["strategy"],
                           validated_calibration(snapshot), validated_tire_age(snapshot))
    assert value["complete_laps"] == count
    assert value["unmodeled_partial_laps"] == pytest.approx(partial)
    assert value["net_stint_gain_range_s"] is None
    if not count:
        assert value["reason"] == "NO_COMPLETE_POST_EXIT_LAPS"
        assert value["covered_gain_range_s"] is None


@pytest.mark.parametrize("domain", ["age", "fuel"])
def test_unsupported_complete_block_is_not_truncated_to_supported_prefix(snapshot, domain):
    calibration = validated_calibration(snapshot)
    if domain == "fuel":
        calibration = replace(calibration, fuel_bounds=(1., 4.2))
    action = project_pit_briefing(snapshot)["actions"][0]
    if domain == "age":
        action["next_stint_laps"] = 40.
    value = _covered_tires(snapshot, action, snapshot["strategy"],
                           calibration, validated_tire_age(snapshot))
    assert value["reason"] == ("PROJECTED_AGES_OUTSIDE_MODEL_DOMAIN" if domain == "age"
                               else "PROJECTED_FUEL_OUTSIDE_MODEL_DOMAIN")
    assert value["covered_gain_range_s"] is None
    assert value["covered_gain_minus_service_range_s"] is None


@pytest.mark.parametrize("parallel", [True, False])
def test_extra_service_cancels_common_uncertain_costs_not_independent_bounds(snapshot, parallel):
    value = reproject(snapshot, tire_change_time_s=10.,
                      fuel_tire_service_timing="PARALLEL" if parallel else "SEQUENTIAL",
                      pit_loss_high_s=60., other_service_high_s=50.)
    action = value["actions"][0]
    expected = 10. - action["fuel_add_l"] / 2 if parallel else 10.
    assert action["tires"]["extra_tire_service_s"] == pytest.approx(expected, abs=1e-6)
    low, high = action["tires"]["covered_gain_range_s"]
    assert action["tires"]["covered_gain_minus_service_range_s"] == pytest.approx(
        [low - expected, high - expected], abs=2e-6)


def test_fixed_total_retains_briefing_without_inventing_tire_service(snapshot):
    value = reproject(snapshot, other_service_low_s=None, other_service_high_s=None,
                      complete_pit_loss_low_s=30., complete_pit_loss_high_s=35.)
    first = value["actions"][0]
    assert len(first["services"]) == 1
    assert first["services"][0]["service_option"] == "UNSPECIFIED"
    assert first["tires"]["reason"] == "TIRE_SERVICE_ASSUMPTIONS_REQUIRED"
    assert first["tires"]["extra_tire_service_s"] is None
    assert "换胎耗时未知" in pit_briefing_text(value)[0]
    assert "未区分服务" in dict(pit_briefing_voice_facts(value))["fuel_traffic_brief"]


def test_missing_calibration_does_not_hide_same_action_fuel_service_or_traffic(snapshot):
    snapshot["tire_calibration"] = None
    value = project_pit_briefing(snapshot)
    action = value["actions"][0]
    assert value["status"] == "CONDITIONAL"
    assert all(row["status"] == "READY" for row in action["services"])
    assert action["tires"]["covered_gain_range_s"] is None
    assert action["tires"]["extra_tire_service_s"] is not None
    assert "收益未就绪" in dict(pit_briefing_voice_facts(value))["tires_brief"]


def test_all_uncertain_traffic_retains_fuel_and_tires_without_claiming_clear_track(snapshot):
    value = reproject(snapshot, pit_loss_low_s=500., pit_loss_high_s=510.)
    assert snapshot["rejoin"]["status"] == "WAIT"
    assert value["status"] == "CONDITIONAL"
    assert all(service["status"] == "WAIT" for action in value["actions"]
               for service in action["services"])
    briefs = dict(pit_briefing_voice_facts(value))
    assert "交通未能确定" in briefs["fuel_traffic_brief"]
    assert "收益约" in briefs["tires_brief"]


@pytest.mark.parametrize("fault", ["dose", "variant", "neighbor", "config", "stale", "replay"])
def test_invalid_join_or_stale_source_cannot_publish_briefing(snapshot, fault):
    row = snapshot["rejoin"]["scenarios"][1]
    if fault == "dose":
        row["fuel_add_l"] += 1
    elif fault == "variant":
        row["service_option"] = "FUEL_ONLY"
    elif fault == "neighbor":
        row["ahead"]["gap_range_s"] = [20., 21.]
    elif fault == "config":
        snapshot["strategy_configuration"]["revision"] += 1
    elif fault == "stale":
        snapshot["updated_age_s"] = 3.
    else:
        snapshot["source_mode"] = "REPLAY"
    assert project_pit_briefing(snapshot)["actions"] == []


@pytest.mark.parametrize("change", ["calibration", "motion", "config", "coverage", "ttl"])
def test_combined_answer_withdraws_and_cannot_revive(snapshot, change):
    now, original = [0.], copy.deepcopy(snapshot)
    service = EngineerService(lambda: snapshot, clock=lambda: now[0], environ={})
    try:
        assert service.submit("综合进站方案")[0] == 202
        assert not service.snapshot()["answer"]["stale"]
        if change == "calibration":
            snapshot["tire_calibration"] = None
        elif change == "motion":
            snapshot["motion"] = None
        elif change == "config":
            reproject(snapshot, tire_change_time_s=10.)
        elif change == "coverage":
            # A changed coverage boundary is material even if a prior producer
            # has not yet advanced its other revisions. Invalid joins fail too.
            snapshot["rejoin"]["scenarios"][0]["next_stint_laps"] += 1
        else:
            now[0] = 11.
        answer = service.snapshot()["answer"]
        assert answer["stale"] and "spoken_text" not in answer
        snapshot.clear()
        snapshot.update(original)
        assert service.snapshot()["answer"]["stale"]
    finally:
        service.close()


def test_coverage_binding_ignores_small_distance_drift_not_validating_its_source(snapshot):
    before = pit_briefing_binding(snapshot)
    # Unit check of semantic binding only, not a source-validity assertion.
    # The exact recomputation rejects any independently modified mapped row.
    snapshot["rejoin"]["scenarios"][0]["distance_to_entry_laps"] -= .001
    assert pit_briefing_binding(snapshot) == before
    assert project_pit_briefing(snapshot)["actions"] == []


def test_fault_is_local_and_model_cannot_omit_limits_or_receive_private_inputs(
    snapshot, monkeypatch,
):
    context = build_live_context(snapshot)
    plan = fallback_plan(context, "解释一下综合进站方案")
    assert "pit_briefing.early" in plan["fact_ids"]
    text = render_plan({"topic": "strategy", "fact_ids": ["pit_briefing.early"],
                        "notice_ids": []}, context)
    assert LIMITS in text
    encoded = json.dumps(context)
    assert all(word not in encoded for word in (
        "lap_profiles", "car_idx", "action_id", "sha256", "lap_contexts", "sample_id"))
    import iracing_ai_engineer.llm_evidence as evidence

    def broken(_):
        raise RuntimeError("PRIVATE_FAULT")

    monkeypatch.setattr(evidence, "project_pit_briefing", broken)
    context = build_live_context(snapshot)
    assert "fuel.current" in {row["id"] for row in context["facts"]}
    answer = render_live_query(context, "pit_briefing")
    assert "综合计算故障" in answer["spoken_text"]
    assert "PRIVATE_FAULT" not in json.dumps(context)


def test_local_short_answer_and_frozen_production_check(snapshot):
    assert live_query_intent("综合进站方案？") == "pit_briefing"
    assert live_query_intent("假设换胎2秒的综合进站方案") is None
    answer = render_live_query(build_live_context(snapshot), "pit_briefing")
    assert 0 < len(answer["spoken_text"]) <= 280 and LIMITS in answer["text"]
    assert "整段收益未知" in answer["spoken_text"] and len(answer["spoken_text"]) < 45
    assert run_synthetic_pit_briefing()["status"] == "PASS"


def test_fake_ptt_recognition_to_local_answer_to_selected_output(snapshot):
    from test_voice_service import Audio, Input, Speech, Store, wait_for

    from iracing_ai_engineer.voice_service import VoiceService

    audio, speech, store = Audio(), Speech(), Store(True)
    speech.recognize = lambda *_args, **_kwargs: {"text": "综合进站方案", "confidence": .99}
    service = EngineerService(lambda: snapshot, environ={})
    voice = VoiceService(lambda: {"lifecycle": "RUNNING", "telemetry": snapshot,
        "engineer": service.snapshot()}, service.submit, store,
        audio=audio, speech=speech, input_factory=Input)
    try:
        voice.start()
        wait_for(lambda: voice.snapshot()["status"] == "READY")
        voice.press()
        assert audio.entered.wait(2)
        voice.release()
        wait_for(lambda: any(row[0] == b"SYNTHETIC_SPEECH" for row in audio.plays))
        assert "整段收益未知" in speech.synthesis[-1]
        assert service.snapshot()["requests_used"] == 0
        assert audio.records[-1]["device"] == "default"
    finally:
        voice.close()
        service.close()


@pytest.mark.parametrize("question,intent,expected", [
    ("综合换胎收益", "pit_briefing_tires", "13整圈"),
    ("综合仅加油交通", "pit_briefing_fuel_traffic", "仅加油假设"),
    ("综合换胎交通", "pit_briefing_tire_traffic", "换四胎假设"),
])
def test_short_voice_subquestions_retain_same_action_evidence_and_expiry(
    snapshot, question, intent, expected,
):
    assert live_query_intent(question) == intent
    now = [0.]
    service = EngineerService(lambda: snapshot, clock=lambda: now[0], environ={})
    try:
        assert service.submit(question)[0] == 202
        value = service.snapshot()
        assert value["requests_used"] == 0
        assert expected in value["answer"]["spoken_text"]
        assert len(value["answer"]["spoken_text"]) <= 55
        assert "不是整段净收益" in value["answer"]["text"]
        now[0] = 10.01
        assert service.snapshot()["answer"]["stale"]
    finally:
        service.close()
