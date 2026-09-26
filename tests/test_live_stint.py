"""Invented inputs only: stint observations must not become physical tire age."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from iracing_ai_engineer import live_app, live_driving
from iracing_ai_engineer.desktop_window import DesktopPresenter
from iracing_ai_engineer.live_fuel import LiveFuelConfig
from iracing_ai_engineer.live_monitor import LiveMonitor
from iracing_ai_engineer.live_queries import live_query_intent, render_live_query
from iracing_ai_engineer.live_stint import LiveStintTracker, stint_notice, validated_stint
from iracing_ai_engineer.llm_engineer import EngineerConfig, EngineerService, fallback_plan
from iracing_ai_engineer.llm_evidence import build_live_context
from iracing_ai_engineer.synthetic_runtime import synthetic_frames


def fixtures(name):
    identifier = f"_stint_{name}_fixtures"
    if identifier not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            identifier, Path(__file__).with_name(f"test_{name}.py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[identifier] = module
        spec.loader.exec_module(module)
    return sys.modules[identifier]


class Rig:
    def __init__(self, buffer_offset=0):
        self.buffer_offset = buffer_offset
        self.tracker = LiveStintTracker(60)
        self.monitor = LiveMonitor(source_id="synthetic", session_id="synthetic",
                                   sdk_tick_rate_hz=60, expected_car_count=3)
        self.base = next(synthetic_frames(8))
        self.base = replace(self.base, buffer_tick=self.base.buffer_tick + buffer_offset)
        self.tick = 0
        # The first SDK sample has no timing predecessor. Prime the normalizer,
        # without promoting that initial unknown-freshness sample to evidence.
        self.monitor.feed(self.base)
        self.tracker.feed(self.base, self.monitor.latest_sample)

    def step(self, *, read_errors=(), **changes):
        self.tick += 1
        values = {**self.base.values, "SessionTick": self.tick,
                  "SessionTime": self.tick / 60, **changes}
        if "LapCompleted" in changes and "Lap" not in changes:
            values["Lap"] = values["LapCompleted"] + 1
        frame = replace(self.base, buffer_tick=self.tick + self.buffer_offset, values=values,
                        read_errors=read_errors, captured_monotonic_s=self.tick / 60 + 1)
        assert self.monitor.feed(frame)
        self.tracker.feed(frame, self.monitor.latest_sample)
        value = fixtures("live_queries").snapshot()
        value["monitor"] = self.monitor.snapshot()
        value["stint"] = self.tracker.snapshot(value["monitor"])
        return value


@pytest.mark.parametrize("buffer_offset", [0, 1000])
def test_partial_attachment_is_an_observation_not_full_stint_or_tire_age(buffer_offset):
    rig = Rig(buffer_offset)
    first = rig.step(LapCompleted=12)
    value = rig.step(LapCompleted=12)
    observed = validated_stint(value)
    assert observed is not None
    assert observed["stint"]["origin_kind"] == "PARTIAL_OBSERVATION"
    assert observed["stint"]["counter_increase"] == 0
    assert observed["stint"]["elapsed_s"] == .016666
    assert observed["tire_observation"]["elapsed_s"] == .016666
    assert first["stint"]["revision"] == observed["revision"]
    assert observed["physical_tire_age"] is observed["physical_wear"] is None
    text = render_live_query(build_live_context(value), "stint")
    assert "仅连续观察区间" in text["text"] and "不是胎龄" in text["spoken_text"]
    assert "不代表完整 stint" in text["text"] and "不代表已换胎" in text["text"]


@pytest.mark.parametrize("buffer_offset", [0, 1000])
def test_fuel_only_pit_exit_resets_stint_but_preserves_tire_observation(buffer_offset):
    rig = Rig(buffer_offset)
    first = rig.step()
    rig.step(OnPitRoad=True, FuelLevel=65.)
    value = rig.step(OnPitRoad=False, FuelLevel=70.)
    observed = validated_stint(value)
    assert observed is not None
    assert observed["stint"]["origin_kind"] == "OBSERVED_PIT_EXIT"
    assert observed["stint"]["elapsed_s"] == 0
    assert observed["tire_observation"]["origin_time_us"] == (
        first["stint"]["tire_observation"]["origin_time_us"])
    assert observed["tire_observation"]["elapsed_s"] > 0
    answer = render_live_query(build_live_context(value), "tire")
    assert "不是完整胎龄或磨损读数" in answer["text"]
    assert "不是胎龄，换胎证据不足" in answer["spoken_text"]


@pytest.mark.parametrize("field", ["TireSetsUsed", "PlayerTireCompound"])
def test_changed_or_temporarily_missing_tire_context_does_not_reset_stint(field):
    rig = Rig()
    first = rig.step()
    changed = rig.step(**{field: 1})
    assert validated_stint(changed) is not None
    assert changed["stint"]["stint"]["origin_time_us"] == first["stint"]["stint"]["origin_time_us"]
    assert changed["stint"]["tire_observation"]["elapsed_s"] == 0
    unavailable = rig.step(read_errors=(field,), **{field: 1})
    assert validated_stint(unavailable) is not None
    assert unavailable["stint"]["tire_observation"] is None
    recovered = rig.step(**{field: 1})
    assert recovered["stint"]["tire_observation"]["elapsed_s"] == 0
    assert recovered["stint"]["revision"] > unavailable["stint"]["revision"]
    assert recovered["stint"]["stint"]["origin_time_us"] == (
        first["stint"]["stint"]["origin_time_us"])


@pytest.mark.parametrize("kind", ["gap", "player", "session", "lap_regression", "lap_jump",
                                  "read_error", "spectator", "replay", "time_regression"])
def test_discontinuity_cannot_reuse_old_origins(kind):
    rig = Rig()
    original = rig.step(LapCompleted=2)
    changes = {"LapCompleted": 2}
    if kind == "gap":
        rig.tick += 30
    elif kind == "player":
        changes["PlayerCarIdx"] = 1
    elif kind == "session":
        changes["SessionNum"] = 1
    elif kind == "lap_regression":
        changes["LapCompleted"] = 1
    elif kind == "lap_jump":
        changes["LapCompleted"] = 5
    elif kind == "read_error":
        changes["read_errors"] = ("OnPitRoad",)
    elif kind == "spectator":
        changes.update(IsOnTrack=False, IsOnTrackCar=False)
    elif kind == "replay":
        changes["IsReplayPlaying"] = True
    else:
        changes["SessionTime"] = 0.
    interrupted = rig.step(**changes)
    rig.step(LapCompleted=2)
    recovered = rig.step(LapCompleted=2)
    assert interrupted["stint"]["revision"] > original["stint"]["revision"]
    assert recovered["stint"]["stint"] is not None
    assert recovered["stint"]["stint"]["origin_time_us"] != (
        original["stint"]["stint"]["origin_time_us"])
    assert recovered["stint"]["stint"]["origin_kind"] == "PARTIAL_OBSERVATION"


@pytest.mark.parametrize("path,bad", [
    (("stint", "physical_wear"), .2), (("stint", "physical_tire_age"), 3),
    (("stint", "revision"), True), (("stint", "session_time_us"), True),
    (("stint", "monitor_sequence"), 999), (("stint", "binding_sha256"), "b" * 64),
    (("stint", "session_num"), 1), (("stint", "player_car_idx"), 1),
    (("stint", "on_pit_road"), 0), (("stint", "stint", "origin_time_us"), 10**1000),
    (("stint", "stint", "origin_laps"), True),
    (("stint", "stint", "elapsed_s"), float("nan")),
    (("stint", "stint", "elapsed_s"), float("inf")),
    (("stint", "stint", "elapsed_s"), 100.),
    (("stint", "stint", "counter_increase"), 99),
    (("stint", "stint", "origin_kind"), "FRESH_TIRES"),
    (("stint", "tire_observation", "compound"), True),
    (("stint", "tire_observation", "sets_used"), 1),
    (("monitor", "reasons"), ["READ_ERROR:LapCompleted"]),
    (("monitor", "reasons"), ["READ_ERROR:TireSetsUsed"]),
    (("monitor", "quality", "stale"), True),
    (("monitor", "telemetry", "tire_sets_used"), False),
])
def test_malformed_or_cross_bound_projections_cannot_be_rendered(path, bad):
    value = Rig().step()
    node = value
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = bad
    assert validated_stint(value) is None
    assert not any(item["id"].startswith(("stint.", "tire."))
                   for item in build_live_context(value)["facts"])


def test_every_required_contract_key_is_checked_before_fact_rendering():
    pristine = Rig().step()
    assert validated_stint(pristine) is not None
    for key in pristine["stint"]:
        value = copy.deepcopy(pristine)
        del value["stint"][key]
        assert validated_stint(value) is None, key
        assert not any(item["id"].startswith(("stint.", "tire."))
                       for item in build_live_context(value)["facts"]), key


@pytest.mark.parametrize("kind", ["disconnected", "replay", "stale", "analysis_fault"])
def test_app_scope_withholds_old_values_and_reports_wait(kind):
    value = Rig().step()
    if kind == "disconnected":
        value["connection"] = "DISCONNECTED"
    elif kind == "replay":
        value["source_mode"] = "REPLAY"
    elif kind == "stale":
        value["updated_age_s"] = 10.
    else:
        value["analysis"] = {"status": "ERROR"}
        value["stint"] = None  # AppState invalidates all analysis projections.
    assert not any(item["id"].startswith(("stint.", "tire."))
                   for item in build_live_context(value)["facts"])
    ui = fixtures("desktop_window")._snapshot()
    ui["telemetry"] = value
    assert "等待" in DesktopPresenter().project(ui, now=10.).stint


def test_failure_remains_visible_across_source_staleness_and_recovery():
    rig = Rig()
    rig.step()
    rig.tracker.fail()
    rig.tracker.reset("SOURCE_STALE")
    value = rig.step()
    assert value["stint"]["reason"] == "STINT_PROCESSING_ERROR"
    assert "故障" in stint_notice(value)
    assert validated_stint(value) is None


@pytest.mark.parametrize("field", ["OnPitRoad", "TireSetsUsed"])
def test_brief_read_loss_between_publications_still_revises_the_observation(field):
    now = [1.]
    state = live_app.AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    analysis = live_app._LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic",
                                      tick_rate=60, car_count=3, generation=state.generation,
                                      allowed=lambda: True)
    before = None
    try:
        for frame in synthetic_frames(8):
            now[0] = frame.captured_monotonic_s
            if frame.buffer_tick == 40:
                frame = replace(frame, read_errors=(field,))
            analysis.process((frame, "Race", now[0], 1_200_000))
            if frame.buffer_tick == 35:
                before = state.snapshot()["stint"]
            if frame.buffer_tick == 65:
                break
        after = validated_stint(state.snapshot())
        assert before is not None and after is not None
        assert after["revision"] > before["revision"]
        assert after["tire_observation"]["origin_time_us"] > (
            before["tire_observation"]["origin_time_us"])
        if field == "TireSetsUsed":
            assert after["stint"]["origin_time_us"] == before["stint"]["origin_time_us"]
    finally:
        analysis.close()
        state.connection("STOPPED")


@pytest.mark.parametrize("fault", ["startup", "feed", "snapshot"])
def test_stint_fault_does_not_disable_fuel_spotter_or_coaching(monkeypatch, fault):
    def broken(*_args, **_kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_ERROR")
    if fault == "startup":
        monkeypatch.setattr(live_app, "LiveStintTracker", broken)
    else:
        monkeypatch.setattr(LiveStintTracker, fault, broken)
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
        assert value["stint"]["reason"] == "STINT_PROCESSING_ERROR"
        assert "fuel.current" in {item["id"] for item in build_live_context(value)["facts"]}
        assert value["spotter"]["status"] == "READY"
        assert value["driving"]["worker"]["failed"] is False
        assert "SYNTHETIC_PRIVATE" not in json.dumps(value)
    finally:
        analysis.close()
        state.connection("STOPPED")


@pytest.fixture(scope="module")
def pace_projection():
    fixture = fixtures("live_driving")
    owner = fixture.Owner()
    model = live_driving._CornerModel(owner, 20)
    for job in fixture.jobs(fixture.rows(["baseline"] * 4 + ["long_coast"] * 3 + ["baseline"])):
        model.process(job)
    value = fixture.projection(owner.result)
    assert value["driving"]["eligible_laps"] == 6
    assert live_driving.validated_pace(value) is not None
    return value


def test_six_clean_laps_expose_raw_medians_not_causal_tire_degradation(pace_projection):
    value = copy.deepcopy(pace_projection)
    pace = live_driving.validated_pace(value)
    assert pace["delta_s"] > 0 and pace["fuel_delta_l"] < 0
    assert [row["lap"] for row in pace["laps"]] == list(range(2, 8))
    assert pace["attribution"] == "UNRESOLVED" and pace["fuel_corrected"] is False
    context = build_live_context(value)
    assert context["capabilities"]["tire"] == "RAW_PACE_OBSERVATION_ONLY"
    text = render_live_query(context, "pace")
    assert text["fact_ids"] == ["tire.pace"]
    assert "起始油量中位变化" in text["text"] and "未做油重修正" in text["text"]
    assert "非胎耗结论" in text["spoken_text"] and "不能归因为胎耗" in text["text"]
    assert len(text["spoken_text"]) < 40


def test_raw_median_handles_one_outlier_and_faster_recent_pace():
    laps = [{"lap": n + 1, "duration_s": seconds, "fuel_start_l": 70 - n}
            for n, seconds in enumerate([90., 90., 100., 89., 89., 95.])]
    pace = live_driving.pace_comparison(laps)
    assert pace["delta_s"] == -1 and pace["fuel_delta_l"] == -3
    assert pace["baseline_median_s"] == 90 and pace["recent_median_s"] == 89


@pytest.mark.parametrize("kind", ["traffic", "rain", "tyres", "refuel", "gap", "counter_gap"])
def test_an_intervening_excluded_lap_cannot_be_skipped_in_pace_comparison(kind):
    fixture = fixtures("live_driving")
    owner = fixture.Owner()
    model = live_driving._CornerModel(owner, 20)
    data = fixture.rows(["baseline"] * 9)
    middle = data[:, 2] == 4
    channel = {"traffic": ("CarLeftRight", 2), "rain": ("Precipitation", .4),
               "tyres": ("TireSetsUsed", 1)}.get(kind)
    if channel:
        data[middle, live_driving.COLUMNS.index(channel[0])] = channel[1]
    elif kind == "refuel":
        data[middle & (data[:, 4] > .5), live_driving.COLUMNS.index("FuelLevel")] += 10
    elif kind == "gap":
        data = data[~(middle & (data[:, 4] > .4) & (data[:, 4] < .5))]
    prepared = fixture.jobs(data)
    if kind == "counter_gap":
        prepared = [job for job in prepared if job.completed_laps != 4]
    for job in prepared:
        model.process(job)
    assert owner.result["pace"] is None


@pytest.mark.parametrize("kind", ["bool", "nan", "huge", "missing", "nonconsecutive",
                                  "tamper", "attribution", "stale_lap", "identity", "epoch"])
def test_pace_projection_revalidates_numeric_summary_and_current_ownership(pace_projection, kind):
    value = copy.deepcopy(pace_projection)
    pace = value["driving"]["pace"]
    if kind in ("bool", "nan", "huge"):
        pace["laps"][0]["duration_s"] = {"bool": True, "nan": float("nan"), "huge": 10**1000}[kind]
    elif kind == "missing":
        pace["laps"].pop()
    elif kind == "nonconsecutive":
        pace["laps"][1]["lap"] += 1
    elif kind == "tamper":
        pace["delta_s"] += .1
    elif kind == "attribution":
        pace["attribution"] = "TIRE_WEAR"
    elif kind == "stale_lap":
        value["monitor"]["telemetry"]["laps_completed"] += 1
    elif kind == "identity":
        value["driving"]["player_car_idx"] = 1
    else:
        value["driving"]["epoch"] = True
    assert live_driving.validated_pace(value) is None
    assert "tire.pace" not in {item["id"] for item in build_live_context(value)["facts"]}


@pytest.mark.parametrize("question,intent", [("这一段跑了多久", "stint"), ("轮胎怎么样", "tire"),
                                             ("配速变化", "pace"), ("该换胎了吗", "tire")])
def test_new_routine_questions_are_local_even_with_provider_configured(question, intent,
                                                                     pace_projection):
    value = Rig().step()
    if intent == "pace":
        value = copy.deepcopy(pace_projection)
    class NoProvider:
        def complete(self, *_args, **_kwargs):
            raise AssertionError("Exact live questions must remain local")
    service = EngineerService(lambda: value, EngineerConfig(provider="deepseek"),
                              client=NoProvider(), environ={})
    try:
        assert live_query_intent(question) == intent
        assert service.submit(question)[0] == 202
        answer = service.snapshot()["answer"]
        assert answer["origin"] == "local_live" and answer["stale"] is False
        assert answer["fact_ids"] and len(answer["spoken_text"]) < 40
        assert service.snapshot()["requests_used"] == 0
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("intent", ["stint", "tire", "pace"])
def test_unready_observation_answers_are_short_without_substituting_fuel(intent):
    context = build_live_context(fixtures("live_queries").snapshot())
    answer = render_live_query(context, intent)
    assert len(answer["spoken_text"]) < 30 and answer["fact_ids"] == []
    assert "油" not in answer["spoken_text"]


@pytest.mark.parametrize("question", ["为什么轮胎表现下降", "解释本段观测的依据", "分析近期配速"])
def test_free_form_offline_fallback_keeps_relevant_observations(question, pace_projection):
    value = Rig().step()
    context = build_live_context(value)
    context["facts"] += [item for item in build_live_context(pace_projection)["facts"]
                         if item["id"] == "tire.pace"]
    assert live_query_intent(question) is None
    plan = fallback_plan(context, question)
    assert plan["topic"] == "strategy" and "tire.pace" in plan["fact_ids"]


@pytest.mark.parametrize("intent,question", [("stint", "这一段跑了多久"), ("tire", "轮胎怎么样"),
                                           ("pace", "配速变化")])
def test_answers_ignore_unrelated_churn_but_expire_after_ten_seconds(intent, question,
                                                                  pace_projection):
    value = Rig().step() if intent != "pace" else copy.deepcopy(pace_projection)
    now = [10.]
    service = EngineerService(lambda: value, clock=lambda: now[0], environ={})
    try:
        assert service.submit(question)[0] == 202
        value["engineer_revision"] += 1
        value["fuel"]["status"] = "LEARNING"
        value["traffic"] = None
        assert service.snapshot()["answer"]["stale"] is False
        now[0] = 20.01
        assert service.snapshot()["answer"]["stale"] is True
        now[0] = 11.
        assert service.snapshot()["answer"]["stale"] is True
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("intent,question", [("stint", "这一段跑了多久"), ("tire", "轮胎怎么样"),
                                           ("pace", "配速变化")])
def test_own_module_boundary_retracts_answer_permanently(intent, question, pace_projection):
    value = Rig().step() if intent != "pace" else copy.deepcopy(pace_projection)
    service = EngineerService(lambda: value, environ={})
    try:
        assert service.submit(question)[0] == 202
        module = value["driving"] if intent == "pace" else value["stint"]
        module["revision"] += 1
        assert service.snapshot()["answer"]["stale"] is True
        module["revision"] -= 1
        assert service.snapshot()["answer"]["stale"] is True
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("question", ["这一段跑了多久", "轮胎怎么样", "配速变化"])
def test_fake_ptt_reads_the_real_local_answer_without_a_provider(question, pace_projection):
    from iracing_ai_engineer.voice_service import VoiceService

    fixture = fixtures("voice_service")
    audio, speech = fixture.Audio(), fixture.Speech()
    value = copy.deepcopy(pace_projection) if question == "配速变化" else Rig().step()
    speech.recognize = lambda *_args, **_kwargs: {"text": question, "confidence": 0.9}
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
        assert speech.synthesis[0] == service.snapshot()["answer"]["spoken_text"]
        assert len(speech.synthesis[0]) < 100 and service.snapshot()["requests_used"] == 0
        assert "不是胎龄" in speech.synthesis[0] or "非胎耗结论" in speech.synthesis[0]
    finally:
        voice.close()
        service.close(wait=True)
