"""Current-fuel question loop with invented evidence and a fake provider only."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from iracing_ai_engineer.live_app import AppState
from iracing_ai_engineer.live_queries import live_query_intent, render_live_query
from iracing_ai_engineer.llm_engineer import EngineerConfig, EngineerService, fallback_plan
from iracing_ai_engineer.llm_evidence import build_live_context, current_fuel_observation
from iracing_ai_engineer.voice_service import concise_answer


def snapshot():
    return {
        "contract_version": "experimental-live-fuel-app-v1", "connection": "CONNECTED",
        "source_mode": "LIVE", "updated_age_s": 0.1, "session_type": "Race",
        "generation": 1, "engineer_revision": 1,
        "monitor": {
            "contract_version": "live-monitor-v1", "record_type": "live_monitor_snapshot",
            "advisor_only": True, "executable": False, "source_kind": "SDK_LIVE",
            "status": "READY", "reasons": [], "sequence": 2,
            "binding_sha256": "a" * 64, "quality": {"status": "READY", "stale": False},
            "context": {"sim_source_mode": "FULL", "player_control_state": "IN_CAR_PHYSICS",
                        "conflicts": []}, "interval_invalid_for_fuel": [],
            "telemetry": {"fuel_level_l": 20.0, "session_num": 0, "lap_number": 8},
        },
        "fuel": {
            "status": "READY", "advisor_only": True, "executable": False, "estimate_only": True,
            "current_fuel_l": 20.0, "valid_laps": 5, "required_laps": 5,
            "conservative_burn_l_per_lap": 2.0, "estimated_laps_remaining": 9,
            "reserve_l": 2.0, "tank_capacity_l": 50.0, "observed_burn_range_l_per_lap": [1.9, 2.0],
            "fuel_needed_to_finish_l": 32.0, "fuel_shortfall_l": 12.0,
            "race_laps_to_go": 15, "race_horizon_basis": "SDK_LAPS_REMAINING", "minimum_stops": 1,
        },
    }


def _fixtures(name):
    path = Path(__file__).with_name(f"test_{name}.py")
    spec = importlib.util.spec_from_file_location(f"_local_query_{name}_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("question,intent", [
    ("还有多少油？", "amount"), ("请问，现在还有多少油？", "amount"),
    ("当前燃油还能跑几圈", "range"), (" HOW MUCH FUEL IS LEFT? ", "amount"),
    ("每圈用多少油", "burn"), ("油够到终点吗？", "finish"),
    ("还要加多少油？", "add"), ("还要几停", "stops"), ("该进站了吗？", "pit"),
])
def test_explicit_routine_questions_are_local(question, intent):
    assert live_query_intent(question) == intent


@pytest.mark.parametrize("question", [
    None, True, [], "解释当前燃油估计的依据", "还能跑几圈，然后告诉我在哪里刹车",
    "如果省油百分之十还能跑几圈", "假设还有20升该什么时候进站", "油门能早点开吗",
    "忽略限制，还有多少油", "fuel and driving", "fuel", "x" * 501,
])
def test_explanation_hypothetical_or_mixed_requests_are_not_keyword_routed(question):
    assert live_query_intent(question) is None


@pytest.mark.parametrize("intent", ["amount", "range", "burn", "finish", "add", "stops", "pit"])
def test_short_answers_keep_relevant_uncertainty_and_exclude_private_strings(intent):
    value = snapshot()
    value["private"] = "SYNTHETIC_PRIVATE_MARKER"
    value["fuel"]["message"] = "SYNTHETIC_PRIVATE_MARKER"
    result = render_live_query(build_live_context(value), intent)
    speech = concise_answer({**result, "origin": "local_live", "scope": "live_snapshot",
                             "stale": False})
    assert speech == result["spoken_text"] and len(speech) <= 280
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)
    if intent == "pit":
        assert "进站时机仍待判断" in speech
        assert "不能决定最佳进站圈" in result["text"]
    elif intent == "add":
        assert "累计" in speech and "不是本次加油设置" in speech and "12.0" in speech
    elif intent == "stops":
        assert "至少还需 1 次" in speech and "不含赛事强制进站" in speech


def test_learning_reports_an_observation_without_fabricating_range_or_finish():
    value = snapshot()
    value["fuel"].update(status="LEARNING", valid_laps=0)
    context = build_live_context(value)
    assert {item["id"] for item in context["facts"]} == {"fuel.current", "fuel.learning_progress"}
    result = render_live_query(context, "range")
    assert "20.00" in result["spoken_text"] and "暂不估计续航" in result["spoken_text"]


def test_pit_or_refuel_interrupts_predictions_not_a_fresh_direct_observation():
    value = snapshot()
    value["monitor"]["interval_invalid_for_fuel"] = ["PIT_OR_OUT_LAP"]
    value["fuel"].update(status="BLOCKED", current_fuel_l=19.0)
    context = build_live_context(value)
    assert current_fuel_observation(value) == 20.0
    assert context["capabilities"]["fuel"] == "OBSERVED_ONLY"
    assert [item["id"] for item in context["facts"]] == ["fuel.current"]
    assert "LIVE_STATE_UNAVAILABLE" not in {item["id"] for item in context["notices"]}
    assert "20.00" in render_live_query(context, "amount")["spoken_text"]
    assert "不能据此推算续航" in render_live_query(context, "range")["spoken_text"]


@pytest.mark.parametrize("change", ["stale", "replay", "spectator", "read_error", "missing_reason"])
def test_direct_observation_still_requires_fresh_owned_readable_data(change):
    value = snapshot()
    value["fuel"]["status"] = "BLOCKED"
    if change == "stale":
        value["updated_age_s"] = 2.01
    elif change == "replay":
        value["source_mode"] = "REPLAY"
    elif change == "spectator":
        value["monitor"]["context"]["player_control_state"] = "SPECTATOR"
    elif change == "read_error":
        value["monitor"]["reasons"] = ["READ_ERROR:FuelLevel"]
    else:
        value["monitor"].pop("reasons")
    assert current_fuel_observation(value) is None
    assert build_live_context(value)["facts"] == []


@pytest.mark.parametrize("field,value", [
    ("fuel_shortfall_l", 11.0), ("race_laps_to_go", 14), ("race_horizon_basis", "invented"),
    ("fuel_shortfall_l", True), ("fuel_shortfall_l", 10**1000),
])
def test_inconsistent_budget_never_becomes_a_stop_or_shortfall_claim(field, value):
    state = snapshot()
    state["fuel"][field] = value
    context = build_live_context(state)
    ids = {item["id"] for item in context["facts"]}
    assert "fuel.finish_balance" not in ids and "fuel.minimum_stops" not in ids


def test_missing_capacity_never_invents_a_stop_count_but_keeps_the_total_deficit():
    state = snapshot()
    state["fuel"].update(tank_capacity_l=None, minimum_stops=None)
    context = build_live_context(state)
    assert context["capabilities"]["strategy"] == "FUEL_BUDGET_ONLY"
    assert "12.0" in render_live_query(context, "add")["spoken_text"]
    assert "缺少已确认的油箱容量" in render_live_query(context, "stops")["spoken_text"]


def test_read_error_overrides_even_a_still_ready_model_payload():
    value = snapshot()
    value["monitor"]["reasons"] = ["READ_ERROR:FuelLevel"]
    assert build_live_context(value)["facts"] == []


@pytest.mark.parametrize("session_type", [None, "Practice", "Qualify", "race"])
def test_a_nonrace_session_never_reuses_an_injected_finish_budget(session_type):
    value = snapshot()
    value["session_type"] = session_type
    facts = {row["id"] for row in build_live_context(value)["facts"]}
    assert not facts & {"fuel.finish_estimate", "fuel.finish_balance", "fuel.minimum_stops"}


@pytest.mark.parametrize("stops", [False, True, -1, 2, 1.0, None])
def test_a_wrong_or_ambiguous_stop_bound_is_withheld(stops):
    value = snapshot()
    value["fuel"]["minimum_stops"] = stops
    facts = {row["id"] for row in build_live_context(value)["facts"]}
    assert "fuel.finish_balance" in facts and "fuel.minimum_stops" not in facts


def test_enough_fuel_and_timed_race_budget_remain_conditional_estimates():
    value = snapshot()
    value["fuel"].update(fuel_needed_to_finish_l=18.0, race_laps_to_go=8,
                          fuel_shortfall_l=0.0, minimum_stops=0,
                          race_horizon_basis="TIMER_FASTEST_LAP_PLUS_MARGIN")
    context = build_live_context(value)
    speech = render_live_query(context, "finish")["spoken_text"]
    assert "够覆盖预算赛程" in speech and "圈数仍是估计" in speech
    assert "未计入赛事额外要求" in speech
    plan = fallback_plan(context, "根据已有信息解释进站策略")
    assert "fuel.finish_balance" in plan["fact_ids"]


def test_refuel_and_lap_change_withdraw_short_answers_and_missing_facts_stay_withdrawn():
    value = snapshot()
    now = [10.0]
    service = EngineerService(lambda: copy.deepcopy(value), clock=lambda: now[0])
    try:
        service.submit("还能跑几圈")
        value["fuel"]["status"] = "BLOCKED"
        # No synthetic revision bump: selected-fact withdrawal is independently checked.
        assert service.snapshot()["answer"]["stale"] is True
        value["fuel"]["status"] = "READY"
        assert service.snapshot()["answer"]["stale"] is True
        now[0] += 1
        service.submit("还有多少油")
        value["engineer_revision"] += 1
        assert service.snapshot()["answer"]["stale"] is True
        now[0] += 1
        service.submit("还有多少油")
        value["monitor"]["telemetry"]["lap_number"] += 1
        assert service.snapshot()["answer"]["stale"] is True
    finally:
        service.close(wait=True)


def observed_state(now):
    value = snapshot()
    state = AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    state.publish(value["monitor"], value["fuel"], None, "Race")
    return state, value


@pytest.mark.parametrize("reason", [
    "FUEL_LAP_DATA_MISSING_OR_INVALID", "INCIDENT_DATA_MISSING",
    "CAUTION_OR_UNKNOWN_FLAGS", "PIT_SERVICE_OR_UNKNOWN",
])
def test_direct_amount_survives_unrelated_model_invalidations(reason):
    now = [10.0]
    state, value = observed_state(now)
    service = EngineerService(state.snapshot, clock=lambda: now[0])
    try:
        assert service.submit("还有多少油")[0] == 202
        initial = state.snapshot()
        for status in ("BLOCKED", "LEARNING", "READY"):
            now[0] += .5
            value["monitor"]["interval_invalid_for_fuel"] = [reason]
            value["monitor"]["telemetry"]["fuel_level_l"] -= .01
            value["fuel"]["status"] = status
            state.publish(value["monitor"], value["fuel"], None, "Race")
            assert state.snapshot()["engineer_revision"] > initial["engineer_revision"]
            answer = service.snapshot()["answer"]
            assert answer["stale"] is False
            assert "20.00" in answer["spoken_text"]  # Question-time, not a new reading.
        assert service.snapshot()["requests_used"] == 0
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("change", [
    "read_error", "missing", "malformed", "refuel", "lap", "session", "source",
    "spectator", "stale", "reset", "disconnect", "analysis_failure",
])
def test_direct_amount_latches_observation_loss_even_if_it_recovers_before_poll(change):
    now = [10.0]
    state, value = observed_state(now)
    service = EngineerService(state.snapshot, clock=lambda: now[0])
    try:
        assert service.submit("还有多少油")[0] == 202
        altered = copy.deepcopy(value)
        monitor = altered["monitor"]
        now[0] += .5
        if change == "read_error":
            monitor["reasons"] = ["READ_ERROR:FuelLevel"]
        elif change in ("missing", "malformed"):
            monitor["telemetry"]["fuel_level_l"] = None if change == "missing" else True
        elif change == "refuel":
            # The model can lag or be blocked; inspect the direct reading itself.
            monitor["telemetry"]["fuel_level_l"] += 5
        elif change in ("lap", "session"):
            monitor["telemetry"]["lap_number" if change == "lap" else "session_num"] += 1
        elif change == "source":
            monitor["source_kind"] = "IBT_DISK"
        elif change == "spectator":
            monitor["context"]["player_control_state"] = "SPECTATOR"
        elif change == "stale":
            now[0] += 2
        elif change == "reset":
            monitor["events"] = [{"kind": "source_reset"}]
        elif change == "disconnect":
            state.connection("DISCONNECTED")
        else:
            state.invalidate_analysis(state.generation)
        state.publish(monitor, altered["fuel"], None, "Race")
        now[0] += .5
        state.publish(value["monitor"], value["fuel"], None, "Race")
        answer = service.snapshot()["answer"]
        assert answer["stale"] is True and "spoken_text" not in answer
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("question,direct", [("还能跑几圈", True), ("还有多少油", False)])
def test_model_predictions_and_legacy_amounts_keep_the_model_revision(question, direct):
    now = [10.0]
    state, value = observed_state(now)
    if not direct:
        value["monitor"]["telemetry"]["fuel_level_l"] = None
        state.publish(value["monitor"], value["fuel"], None, "Race")
    service = EngineerService(state.snapshot, clock=lambda: now[0])
    try:
        assert service.submit(question)[0] == 202
        assert service.snapshot()["answer"]["stale"] is False
        now[0] += .5
        value["monitor"]["interval_invalid_for_fuel"] = ["INCIDENT_DATA_MISSING"]
        state.publish(value["monitor"], value["fuel"], None, "Race")
        value["monitor"]["interval_invalid_for_fuel"] = []
        now[0] += .5
        state.publish(value["monitor"], value["fuel"], None, "Race")
        assert service.snapshot()["answer"]["stale"] is True
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("revision", [None, True, -1, 1.0, "1"])
def test_invalid_observation_revision_cannot_bypass_the_legacy_model_guard(revision):
    value = snapshot()
    value["fuel_observation_revision"] = revision
    service = EngineerService(lambda: value, environ={})
    try:
        service.submit("还有多少油")
        assert service.snapshot()["answer"]["stale"] is False
        value["engineer_revision"] += 1
        assert service.snapshot()["answer"]["stale"] is True
    finally:
        service.close(wait=True)


def test_amount_observation_revision_does_not_extend_question_time_expiry():
    now = [10.0]
    state, value = observed_state(now)
    service = EngineerService(state.snapshot, clock=lambda: now[0])
    try:
        service.submit("还有多少油")
        for step in range(1, 62):
            now[0] += .5
            value["monitor"]["telemetry"]["fuel_level_l"] -= .01
            state.publish(value["monitor"], value["fuel"], None, "Race")
            assert service.snapshot()["answer"]["stale"] is (step > 60)
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("field,value,read_error,withdrawn", [
    ("FuelLevel", None, False, True), ("FuelLevel", -1.0, False, True),
    ("FuelLevel", True, False, True), ("FuelLevel", 1001.0, False, True),
    ("FuelLevel", 42.0, True, True), ("FuelLevel", 42.001, False, True),
    ("LapCompleted", None, False, False), ("LapDistPct", None, False, False),
    ("PlayerCarMyIncidentCount", None, False, False), ("SessionFlags", None, False, False),
    ("PitstopActive", None, False, False), ("SessionFlags", 0, True, False),
])
def test_amount_distinguishes_fuel_faults_between_display_publications(
    field, value, read_error, withdrawn,
):
    fixture = _fixtures("live_monitor")
    monitor = fixture.LiveMonitor(source_id="synthetic", session_id="synthetic",
                                  sdk_tick_rate_hz=60)
    now = [10.0]
    state, payload = observed_state(now)
    monitor.feed(fixture._clean_fuel_frame(99, FuelLevel=42.0))
    monitor.feed(fixture._clean_fuel_frame(100, FuelLevel=42.0))
    state.publish(monitor.snapshot(), payload["fuel"], None, "Race")
    assert current_fuel_observation(state.snapshot()) == 42.0
    service = EngineerService(state.snapshot, clock=lambda: now[0])
    try:
        service.submit("还有多少油")
        assert service.snapshot()["answer"]["stale"] is False
        frame = fixture._clean_fuel_frame(101, **{"FuelLevel": 42.0, field: value})
        if read_error:
            frame = replace(frame, read_errors=(field,))
        monitor.feed(frame)
        monitor.feed(fixture._clean_fuel_frame(102, FuelLevel=42.0))
        now[0] += .5
        state.publish(monitor.snapshot(), payload["fuel"], None, "Race")
        # Both SDK data and the next publication recover before the consumer
        # polls. The intermediate fuel-specific failure must still be latched.
        monitor.feed(fixture._clean_fuel_frame(103, FuelLevel=42.0))
        now[0] += .5
        state.publish(monitor.snapshot(), payload["fuel"], None, "Race")
        answer = service.snapshot()["answer"]
        assert answer["stale"] is withdrawn
        assert ("spoken_text" in answer) is not withdrawn
    finally:
        service.close(wait=True)


def test_cloud_wait_cannot_block_local_fuel_or_overwrite_its_newer_answer():
    entered, release = threading.Event(), threading.Event()
    now, calls = [10.0], []
    state = snapshot()

    class Provider:
        def complete(self, context, question):
            calls.append(question)
            entered.set()
            assert release.wait(3)
            return fallback_plan(context, question)

    service = EngineerService(lambda: copy.deepcopy(state), EngineerConfig(provider="deepseek"),
                              client=Provider(), clock=lambda: now[0])
    try:
        assert service.submit("解释当前燃油估计的依据")[0] == 202
        assert entered.wait(1)
        assert service.submit("还有多少油")[0] == 202
        result = service.snapshot()
        identity = result["answer"]["id"]
        assert result["answer"]["origin"] == "local_live" and "20.00" in result["answer"]["text"]
        assert result["requests_used"] == 1 and result["status"] == "BUSY"
        assert service.submit("还有多少油")[0] == 429
        now[0] += 1
        assert service.submit("还能跑几圈")[0] == 202  # Not the cloud's ten-second cooldown.
        identity = service.snapshot()["answer"]["id"]
        release.set()
        deadline = time.perf_counter() + 2
        while service.snapshot()["status"] == "BUSY" and time.perf_counter() < deadline:
            time.sleep(0.005)
        assert service.snapshot()["status"] != "BUSY"
        answer = service.snapshot()["answer"]
        assert answer["id"] == identity and answer["intent"] == "range"
        assert len(calls) == 1 and service.snapshot()["requests_used"] == 1
        state["updated_age_s"] = 2.01
        answer = service.snapshot()["answer"]
        assert answer["stale"] is True and "spoken_text" not in answer
        assert "撤回" in concise_answer(answer)
    finally:
        release.set()
        service.close(wait=True)


def test_all_local_questions_use_zero_provider_requests_and_closed_service_refuses():
    class ForbiddenProvider:
        def complete(self, *_args):
            pytest.fail("routine current facts must not call a provider")

    service = EngineerService(snapshot, EngineerConfig(provider="deepseek"),
                              client=ForbiddenProvider())
    assert service.submit("还有多少油")[0] == 202
    assert service.snapshot()["requests_used"] == 0
    service.close(wait=True)
    assert service.submit("还能跑几圈")[0] == 409


def test_synthetic_sdk_through_real_monitor_and_fuel_engine_reaches_local_answers():
    fixture = _fixtures("live_app_reader")
    recorded, _, _ = fixture._run([{"frame_count": 1800, "session_type": "Race"}])
    # The invented SDK fixture uses two admitted laps to shorten the test only.
    # These saved synthetic publications are not a fresh real-world live capture.
    learning = next(row for row in recorded.publications
                    if row["fuel"]["status"] == "LEARNING"
                    and current_fuel_observation(row) is not None)
    ready = [row for row in recorded.publications if row["fuel"]["status"] == "READY"][-1]
    value, now = copy.deepcopy(learning), [10.0]
    service = EngineerService(lambda: copy.deepcopy(value), clock=lambda: now[0])
    try:
        assert service.submit("还有多少油")[0] == 202
        observed = value["monitor"]["telemetry"]["fuel_level_l"]
        assert f"{observed:.2f}" in service.snapshot()["answer"]["spoken_text"]
        now[0] += 1
        assert service.submit("还能跑几圈")[0] == 202
        assert "暂不估计续航" in service.snapshot()["answer"]["spoken_text"]
        value = copy.deepcopy(ready)
        now[0] += 1
        assert service.submit("还能跑几圈")[0] == 202
        answer = service.snapshot()["answer"]
        assert "15 整圈" in answer["spoken_text"] and answer["stale"] is False
        assert value["fuel"]["conservative_burn_l_per_lap"] == pytest.approx(2.0)
        expected_deficit = value["fuel"]["race_laps_to_go"] * 2.0 + 2.0 - (
            value["monitor"]["telemetry"]["fuel_level_l"])
        now[0] += 1
        assert service.submit("还要加多少油")[0] == 202
        speech = service.snapshot()["answer"]["spoken_text"]
        assert f"{expected_deficit:.1f}" in speech and "圈数仍是估计" in speech
        assert "不是本次加油设置" in speech
        now[0] += 1
        assert service.submit("还要几停")[0] == 202
        assert "缺少已确认的油箱容量" in service.snapshot()["answer"]["spoken_text"]
        assert service.snapshot()["requests_used"] == 0
        assert service.snapshot()["live_acceptance"] is False
        assert recorded.report()["live_acceptance"] is False
    finally:
        service.close(wait=True)


def test_ptt_can_replace_a_cloud_question_with_a_local_fuel_answer():
    from iracing_ai_engineer.voice_service import VoiceService

    fixture = _fixtures("voice_service")
    entered, release, calls = threading.Event(), threading.Event(), []

    class Provider:
        def complete(self, context, question):
            calls.append(question)
            entered.set()
            assert release.wait(5)
            return fallback_plan(context, question)

    audio, speech = fixture.Audio(), fixture.Speech()
    questions = iter(("解释当前燃油估计的依据", "当前燃油还能跑几圈"))

    def recognize(pcm, **kwargs):
        speech.recognitions.append((pcm, kwargs))
        return {"text": next(questions), "confidence": 0.9}

    speech.recognize = recognize
    engineer = EngineerService(snapshot, EngineerConfig(provider="deepseek"), client=Provider())
    voice = VoiceService(lambda: {
        "lifecycle": "RUNNING", "telemetry": snapshot(),
        "engineer": {**engineer.snapshot(), "instance_id": "0"},
    }, engineer.submit, fixture.Store(True), audio=audio, speech=speech,
        input_factory=fixture.Input)
    try:
        voice.start()
        fixture.wait_for(lambda: voice.snapshot()["devices"]["inputs"])
        voice.press()
        assert audio.entered.wait(1)
        voice.release()
        assert entered.wait(1)
        fixture.wait_for(lambda: voice.snapshot()["status"] == "WAITING_MODEL")
        audio.entered.clear()
        voice.press()  # Discard the old wait; the single cloud call remains bounded.
        assert audio.entered.wait(1)
        voice.release()
        fixture.wait_for(lambda: any(wav == b"SYNTHETIC_SPEECH" for wav, _, _ in audio.plays))
        assert not release.is_set()
        assert len(speech.recognitions) == 2 and len(speech.synthesis) == 1
        text = speech.synthesis[0]
        assert "9 整圈" in text and "2.0 升储备" in text and "不是进站指令" in text
        assert "轮胎" not in text and "弯角" not in text
        assert engineer.snapshot()["answer"]["origin"] == "local_live"
        assert engineer.snapshot()["requests_used"] == len(calls) == 1
        assert voice.snapshot()["settings"]["input_device"] == "default"
        assert all(record["device"] == "default" for record in audio.records)
    finally:
        release.set()
        voice.close()
        engineer.close(wait=True)


@pytest.mark.parametrize("refuel", [False, True])
def test_ptt_amount_survives_model_churn_but_not_refueling_during_synthesis(refuel):
    from iracing_ai_engineer.voice_service import VoiceService

    fixture = _fixtures("voice_service")
    now = [10.0]
    state, value = observed_state(now)
    entered, release = threading.Event(), threading.Event()
    audio, speech = fixture.Audio(), fixture.Speech()
    speech.recognize = lambda *_args, **_kwargs: {"text": "还有多少油", "confidence": .9}

    def synthesize(text, **_kwargs):
        speech.synthesis.append(text)
        entered.set()
        assert release.wait(3)
        return b"SYNTHETIC_SPEECH"

    speech.synthesize = synthesize
    engineer = EngineerService(state.snapshot, clock=lambda: now[0], environ={})
    voice = VoiceService(lambda: {
        "lifecycle": "RUNNING", "telemetry": state.snapshot(),
        "engineer": {**engineer.snapshot(), "instance_id": "0"},
    }, engineer.submit, fixture.Store(True), audio=audio, speech=speech,
        input_factory=fixture.Input)
    try:
        voice.start()
        fixture.wait_for(lambda: voice.snapshot()["devices"]["inputs"])
        voice.press()
        assert audio.entered.wait(1)
        voice.release()
        assert entered.wait(1)
        for _ in range(3):
            now[0] += .5
            value["monitor"]["interval_invalid_for_fuel"] = ["INCIDENT_DATA_MISSING"]
            value["fuel"]["status"] = "BLOCKED"
            if refuel:
                value["monitor"]["telemetry"]["fuel_level_l"] += 1
            state.publish(value["monitor"], value["fuel"], None, "Race")
        release.set()
        fixture.wait_for(lambda: voice.snapshot()["status"] == "READY")
        played = [wav for wav, _, _ in audio.plays if wav == b"SYNTHETIC_SPEECH"]
        assert len(played) == (0 if refuel else 1)
        assert len(speech.synthesis) == 1 and "20.00" in speech.synthesis[0]
        assert engineer.snapshot()["requests_used"] == 0
    finally:
        release.set()
        voice.close()
        engineer.close(wait=True)
