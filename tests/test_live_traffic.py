"""Invented positions only; no simulator, raw capture, device or provider access."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from iracing_ai_engineer import live_app
from iracing_ai_engineer.desktop_window import DesktopPresenter
from iracing_ai_engineer.live_monitor import LiveMonitor
from iracing_ai_engineer.live_queries import live_query_intent, render_live_query
from iracing_ai_engineer.live_traffic import (
    bound_track_length_mm,
    project_live_traffic,
    validated_traffic,
)
from iracing_ai_engineer.llm_engineer import EngineerConfig, EngineerService
from iracing_ai_engineer.llm_evidence import build_live_context


def fixtures(name):
    identifier = f"_traffic_{name}_fixtures"
    if identifier in sys.modules:
        return sys.modules[identifier]
    spec = importlib.util.spec_from_file_location(identifier,
                                                Path(__file__).with_name(f"test_{name}.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[identifier] = module
    spec.loader.exec_module(module)
    return module


def state(*, changes=None, read_errors=(), length=5_000_000, in_car=True):
    fixture = fixtures("live_monitor")
    monitor = LiveMonitor(source_id="synthetic", session_id="synthetic", sdk_tick_rate_hz=60)
    for tick in (100, 101):
        frame = fixture._clean_fuel_frame(tick, IsOnTrack=in_car, IsOnTrackCar=in_car,
                                         **(changes or {}))
        frame = replace(frame, read_errors=read_errors)
        monitor.feed(frame)
        snapshot = monitor.snapshot()
    traffic = project_live_traffic(monitor.latest_sample, snapshot, length, metadata_update=1)
    return {
        "contract_version": "experimental-live-fuel-app-v1", "source_mode": "LIVE",
        "connection": "CONNECTED", "generation": 1, "engineer_revision": 1,
        "updated_age_s": 0.0, "session_type": "Practice", "monitor": snapshot,
        "fuel": {"status": "LEARNING"}, "traffic": traffic,
    }


def test_bound_metric_track_metadata_never_admits_a_stale_or_untyped_value():
    frame = fixtures("live_monitor")._clean_fuel_frame(1)
    payload = {"WeekendInfo": {"TrackLength": "5.000 km"}}
    assert bound_track_length_mm(payload, 1, frame) == 5_000_000
    assert bound_track_length_mm({"WeekendInfo": {"TrackLength": "5000 m"}}, 1, frame) == 5_000_000
    for value in (None, True, 5000, "private text", "5 miles", "0 km", "1000 km"):
        assert bound_track_length_mm({"WeekendInfo": {"TrackLength": value}}, 1, frame) is None
    assert bound_track_length_mm({"WeekendInfo": {"TrackLength": "5 km"}}, 2, frame) is None
    assert bound_track_length_mm({"WeekendInfo": {"TrackLength": "5 km"}}, True, frame) is None


def test_circular_neighbors_cross_start_finish_without_becoming_race_order_or_time_gaps():
    value = state(changes={
        "LapDistPct": .99, "LapCompleted": 10, "Lap": 11,
        "CarIdxLapDistPct": [.99, .01, .9], "CarIdxTrackSurface": [3, 3, 3],
        "CarIdxLapCompleted": [10, 11, 10], "CarIdxLap": [11, 12, 11],
    })
    traffic = value["traffic"]
    assert validated_traffic(value) == traffic
    assert traffic["ahead"] == {"car_idx": 1, "distance_mm": 100_000, "completed_laps_delta": 1}
    assert traffic["behind"] == {"car_idx": 2, "distance_mm": 450_000, "completed_laps_delta": 0}
    context = build_live_context(value)
    assert context["capabilities"]["traffic"] == "PHYSICAL_OBSERVATION_ONLY"
    speech = render_live_query(context, "traffic")["spoken_text"]
    assert "100 米" in speech and "450 米" in speech and "不是秒差" in speech
    assert "领先一圈" not in speech and "car_idx" not in json.dumps(context)
    assert "binding_sha256" not in json.dumps(context)


@pytest.mark.parametrize("fraction", [.2, .199, .201])
def test_longitudinal_overlap_withholds_both_nearest_claims(fraction):
    value = state(changes={"CarIdxLapDistPct": [.2, fraction, -1.0]})
    traffic = value["traffic"]
    assert traffic["status"] == "AMBIGUOUS" and traffic["overlap_count"] == 1
    assert traffic["ahead"] is traffic["behind"] is None
    speech = render_live_query(build_live_context(value), "ahead")["spoken_text"]
    assert "前后关系暂不明确" in speech and "前方最近" not in speech


def test_only_eligible_on_track_cars_enter_map_and_no_cars_does_not_mean_clear():
    value = state(changes={"CarIdxOnPitRoad": [False, True, False]})
    assert value["traffic"]["eligible_count"] == 0
    assert value["traffic"]["excluded_count"] == 2
    speech = render_live_query(build_live_context(value), "traffic")["spoken_text"]
    assert "不代表赛道清空" in speech


@pytest.mark.parametrize("changes,read_errors,length,reason", [
    ({}, ("CarIdxLapDistPct",), 5_000_000, "TRAFFIC_READ_ERROR"),
    ({}, (), None, "BOUND_TRACK_LENGTH_UNAVAILABLE"),
    ({"CarIdxLapDistPct": [.2, .3]}, (), 5_000_000, "OPPONENT_ARRAYS_UNAVAILABLE"),
    ({"CarIdxOnPitRoad": [False, None, False]}, (), 5_000_000, "OPPONENT_ARRAYS_UNAVAILABLE"),
    ({"OnPitRoad": True}, (), 5_000_000, "PLAYER_NOT_ON_RACING_SURFACE"),
    ({"PlayerTrackSurface": 0}, (), 5_000_000, "PLAYER_NOT_ON_RACING_SURFACE"),
])
def test_missing_or_malformed_positions_cannot_become_a_clear_traffic_claim(
    changes, read_errors, length, reason,
):
    value = state(changes=changes, read_errors=read_errors, length=length)
    assert value["traffic"]["reason"] == reason
    assert validated_traffic(value) is None
    assert not any(row["id"].startswith("traffic.") for row in build_live_context(value)["facts"])


@pytest.mark.parametrize("change", ["stale", "replay", "spectator", "sequence", "time", "binding"])
def test_mixed_or_ineligible_source_cannot_reuse_a_traffic_projection(change):
    value = state()
    if change == "stale":
        value["updated_age_s"] = 2.01
    elif change == "replay":
        value["source_mode"] = "REPLAY"
    elif change == "spectator":
        value["monitor"]["context"]["player_control_state"] = "SPECTATOR"
    elif change == "sequence":
        value["traffic"]["monitor_sequence"] += 1
    elif change == "time":
        value["traffic"]["session_time_us"] += 1
    else:
        value["traffic"]["binding_sha256"] = "a" * 64
    assert not any(row["id"].startswith("traffic.") for row in build_live_context(value)["facts"])


@pytest.mark.parametrize("question,intent", [
    ("前后车情况？", "traffic"), ("前车多远？", "ahead"), ("后车多远？", "behind"),
    ("现在允许进站吗？", "pit_permission"), ("gap ahead", "ahead"),
])
def test_explicit_questions_use_local_situation_facts(question, intent):
    assert live_query_intent(question) == intent
    service = EngineerService(state)
    try:
        assert service.submit(question)[0] == 202
        answer = service.snapshot()["answer"]
        assert answer["origin"] == "local_live" and answer["stale"] is False
        assert len(answer["spoken_text"]) <= 280 and service.snapshot()["requests_used"] == 0
    finally:
        service.close(wait=True)


def test_traffic_survives_fuel_failure_but_changes_expire_even_with_same_fact_ids():
    value, now = state(), [10.0]
    service = EngineerService(lambda: copy.deepcopy(value), clock=lambda: now[0])
    try:
        service.submit("前后车情况")
        value["engineer_revision"] += 1  # Fuel learning/refueling must not revoke traffic.
        value["fuel"] = None
        assert service.snapshot()["answer"]["stale"] is False
        value["traffic"]["ahead"]["car_idx"] = 2
        assert service.snapshot()["answer"]["stale"] is True
        value = state()
        now[0] += 1
        service.submit("前后车情况")
        value["traffic"]["ahead"]["distance_mm"] = 4_900_000  # Same car crossed start/finish.
        assert service.snapshot()["answer"]["stale"] is True
        value = state()
        now[0] += 1
        service.submit("前后车情况")
        now[0] += 10.01
        assert service.snapshot()["answer"]["stale"] is True
        assert "spoken_text" not in service.snapshot()["answer"]
    finally:
        service.close(wait=True)


def test_a_published_traffic_interruption_cannot_revive_an_answer_between_reads():
    value, now = state(), [10.0]
    app = live_app.AppState(clock=lambda: now[0])
    app.connection("CONNECTED")

    def publish(payload):
        app.publish(payload["monitor"], {"status": "BLOCKED"}, None, "Practice",
                    traffic=payload["traffic"])

    publish(value)
    service = EngineerService(app.snapshot, clock=lambda: now[0])
    try:
        service.submit("前后车情况")
        original_revision = app.snapshot()["situation_revision"]
        # Normal distance/metadata updates and invalid fuel intervals cannot
        # interrupt traffic speech every half second.
        update = copy.deepcopy(value)
        update["traffic"]["ahead"]["distance_mm"] += 1_000
        update["traffic"]["metadata_update"] += 1
        update["monitor"]["interval_invalid_for_fuel"] = ["FUEL_LAP_DATA_MISSING_OR_INVALID"]
        publish(update)
        assert app.snapshot()["situation_revision"] == original_revision
        assert service.snapshot()["answer"]["stale"] is False
        lost = copy.deepcopy(value)
        lost["traffic"] = None
        publish(lost)
        publish(value)
        # No EngineerService read happened during the missing-data publication.
        assert service.snapshot()["answer"]["stale"] is True
        now[0] += 1
        service.submit("前后车情况")
        spectator = copy.deepcopy(value)
        spectator["monitor"]["context"]["player_control_state"] = "SPECTATOR"
        publish(spectator)
        publish(value)
        assert service.snapshot()["answer"]["stale"] is True
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("question", ["前后车情况", "前车多远", "后车多远", "现在允许进站吗"])
def test_unavailable_situation_notice_is_not_cancelled_by_repeated_bad_fuel_intervals(question):
    value, now = state(length=None), [10.0]
    value["monitor"]["reasons"].extend(["READ_ERROR:PitsOpen", "READ_ERROR:SessionFlags"])
    value["monitor"]["interval_invalid_for_fuel"] = ["FUEL_LAP_DATA_MISSING_OR_INVALID"]
    app = live_app.AppState(clock=lambda: now[0])
    app.connection("CONNECTED")

    def publish():
        app.publish(value["monitor"], {"status": "BLOCKED"}, None, "Practice",
                    traffic=value["traffic"])

    publish()
    service = EngineerService(app.snapshot, clock=lambda: now[0])
    try:
        assert service.submit(question)[0] == 202
        answer = service.snapshot()["answer"]
        assert answer["fact_ids"] == [] and answer["spoken_text"] and not answer["stale"]
        for _ in range(6):
            now[0] += 0.5
            publish()
            assert service.snapshot()["answer"]["stale"] is False
        value["monitor"]["reasons"].append("READ_ERROR:FuelLevel")
        publish()
        assert not build_live_context(app.snapshot())["facts"]
        assert service.snapshot()["answer"]["stale"] is False
        now[0] += 7.01
        publish()
        assert service.snapshot()["answer"]["stale"] is True
    finally:
        service.close(wait=True)


def test_published_pit_read_failure_cannot_revive_an_old_permission_between_polls():
    value = state()
    app = live_app.AppState()
    app.connection("CONNECTED")

    def publish():
        app.publish(value["monitor"], {"status": "BLOCKED"}, None, "Practice",
                    traffic=value["traffic"])

    publish()
    service = EngineerService(app.snapshot)
    try:
        service.submit("现在允许进站吗")
        assert service.snapshot()["answer"]["stale"] is False
        value["monitor"]["reasons"].append("READ_ERROR:PitsOpen")
        publish()
        value["monitor"]["reasons"].remove("READ_ERROR:PitsOpen")
        publish()
        assert service.snapshot()["answer"]["stale"] is True
    finally:
        service.close(wait=True)


@pytest.mark.parametrize("change", ["fuel_only", "flags", "neighbor", "elapsed"])
def test_cloud_situation_result_keeps_question_time_binding_not_completion_time(change):
    value, now = state(), [10.0]
    entered, release = threading.Event(), threading.Event()

    class Planner:
        def complete(self, context, question):
            entered.set()
            assert release.wait(3)
            return {"topic": "strategy", "fact_ids": ["traffic.ahead"], "notice_ids": []}

    service = EngineerService(lambda: copy.deepcopy(value), EngineerConfig(provider="deepseek"),
                              client=Planner(), clock=lambda: now[0])
    try:
        assert service.submit("解释当前前后车位置")[0] == 202
        assert entered.wait(1)
        if change == "fuel_only":
            value["fuel"] = None
            value["engineer_revision"] += 1
        elif change == "flags":
            value["monitor"]["telemetry"]["session_flags"] = 0x010000
        elif change == "neighbor":
            value = state(changes={"CarIdxLapDistPct": [.2, .4, .3],
                                   "CarIdxTrackSurface": [3, 3, 3]})
        else:
            now[0] += 10.01
        release.set()
        answer = fixtures("llm_engineer").wait_answer(service)["answer"]
        assert answer["origin"] == "deepseek"
        assert answer["stale"] is (change != "fuel_only")
        assert service.snapshot()["requests_used"] == 1
    finally:
        release.set()
        service.close(wait=True)


@pytest.mark.parametrize("field,corrupt", [
    ("monitor_sequence", True), ("session_time_us", -1), ("metadata_update", None),
    ("eligible_count", 257), ("excluded_count", -1), ("overlap_count", True),
    ("track_length_mm", 0), ("status", "PREDICTED"), ("advisor_only", 1),
    ("physical_observation_only", False),
])
def test_corrupt_projection_never_crosses_into_spoken_distances(field, corrupt):
    value = state()
    value["traffic"][field] = corrupt
    assert validated_traffic(value) is None
    answer = render_live_query(build_live_context(value), "traffic")
    assert "500 米" not in answer["spoken_text"]


def test_binding_shape_is_validated_even_when_both_projection_and_monitor_agree():
    value = state()
    value["traffic"]["binding_sha256"] = value["monitor"]["binding_sha256"] = "not-a-digest"
    assert validated_traffic(value) is None
    value = state()
    value["traffic"]["monitor_sequence"] = 1
    value["monitor"]["sequence"] = True
    assert validated_traffic(value) is None


def test_spoken_long_distances_are_approximate_kilometres_with_full_metres_in_text():
    value = state(changes={"CarIdxLapDistPct": [.2, .32145, .08674],
                           "CarIdxTrackSurface": [3, 3, 3]}, length=30_000_000)
    answer = render_live_query(build_live_context(value), "traffic")
    assert "约 3.6 公里" in answer["spoken_text"] and "约 3.4 公里" in answer["spoken_text"]
    assert "约 3644 米" in answer["text"] and "约 3398 米" in answer["text"]
    assert "不是秒差" in answer["spoken_text"]


def test_pit_question_combines_fuel_permission_and_physical_traffic_without_rejoin_claim():
    value = state()
    fuel = fixtures("live_queries").snapshot()["fuel"]
    value["fuel"] = {**fuel, "current_fuel_l": 42.0, "estimated_laps_remaining": 20,
                     "fuel_needed_to_finish_l": 62.0, "fuel_shortfall_l": 20.0,
                     "race_laps_to_go": 30}
    value["session_type"] = "Race"
    result = render_live_query(build_live_context(value), "pit")
    assert {"fuel.finish_balance", "fuel.range_laps", "pit.permission", "traffic.ahead"} <= set(
        result["fact_ids"])
    assert "上述车距不是出站后的车距" in result["text"]
    assert "进站时机仍待判断" in result["spoken_text"]
    assert "不能决定最佳进站圈" in result["text"]
    assert len(result["spoken_text"]) < len(result["text"])


def test_fault_and_missing_geometry_have_audible_fixed_reasons_not_private_errors():
    value = state()
    value["traffic"].update(status="UNAVAILABLE", reason="TRAFFIC_PROCESSING_ERROR")
    text = render_live_query(build_live_context(value), "traffic")["spoken_text"]
    assert "交通分析故障" in text
    value["traffic"]["reason"] = "BOUND_TRACK_LENGTH_UNAVAILABLE"
    assert "赛道长度" in render_live_query(build_live_context(value), "traffic")["spoken_text"]
    value["traffic"]["reason"] = "SYNTHETIC PRIVATE ERROR"
    assert "SYNTHETIC PRIVATE ERROR" not in json.dumps(build_live_context(value))


@pytest.mark.parametrize("flags,phrase", [
    (0, "未见上述"), (0x010000, "黑旗"), (0x100000, "维修"), (0x4000, "黄旗"),
])
def test_pit_permission_and_flags_are_facts_not_a_box_now_recommendation(flags, phrase):
    value = state(changes={"PitsOpen": False, "SessionFlags": flags})
    speech = render_live_query(build_live_context(value), "pit_permission")["spoken_text"]
    full = render_live_query(build_live_context(value), "pit_permission")["text"]
    assert "不允许本人进站" in speech and "进站时机仍待判断" in speech
    assert phrase in full and "不代表现在进站最优" in full
    value["monitor"]["reasons"].extend(["READ_ERROR:PitsOpen", "READ_ERROR:SessionFlags"])
    assert not any(row["id"].startswith("pit.") for row in build_live_context(value)["facts"])


def test_native_view_has_independent_traffic_health_and_distance_labels():
    value = state()
    service = EngineerService(lambda: copy.deepcopy(value))
    try:
        def view():
            return DesktopPresenter().project({
                "lifecycle": "RUNNING", "telemetry": value, "engineer": service.snapshot(),
            }, now=10)
        assert "前方约 500 m" in view().traffic and "不是秒差" in view().traffic
        value["updated_age_s"] = 2.01
        assert "过期" in view().traffic
    finally:
        service.close(wait=True)


def test_reader_binds_geometry_and_clears_current_traffic_on_disconnect(monkeypatch):
    fixture = fixtures("live_app_reader")
    original = fixture.FakeSdk.session_info_snapshot

    def metadata(self):
        payload, update = original(self)
        payload["WeekendInfo"]["TrackLength"] = "5 km"
        return payload, update

    monkeypatch.setattr(fixture.FakeSdk, "session_info_snapshot", metadata)
    recorded, _, _ = fixture._run([{"frame_count": 120}, {"frame_count": 61}])
    assert any(row["traffic"]["status"] == "READY" for row in recorded.publications)
    assert any(validated_traffic(row) is not None for row in recorded.publications)
    assert recorded.snapshot()["traffic"] is None
    assert all(row["traffic"] is None for row in recorded.transitions)
    assert recorded.report()["live_acceptance"] is False


def test_traffic_fault_is_latched_without_breaking_fuel_or_proximity(monkeypatch):
    calls = []

    def broken(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("SYNTHETIC PRIVATE ERROR")

    monkeypatch.setattr(live_app, "project_live_traffic", broken)
    recorded, _, _ = fixtures("live_app_reader")._run([{"frame_count": 1800}])
    assert len(calls) == 1
    assert any(row["fuel"]["status"] == "READY" for row in recorded.publications)
    assert all(row["traffic"]["reason"] == "TRAFFIC_PROCESSING_ERROR"
               for row in recorded.publications)
    assert "SYNTHETIC PRIVATE ERROR" not in json.dumps(recorded.publications)


def test_fake_ptt_to_real_service_speaks_traffic_even_when_fuel_is_unavailable():
    from iracing_ai_engineer.voice_service import VoiceService

    fixture = fixtures("voice_service")
    audio, speech, value = fixture.Audio(), fixture.Speech(), state()
    value["fuel"] = None
    speech.recognize = lambda *_args, **_kwargs: {"text": "前后车情况", "confidence": 0.9}
    service = EngineerService(lambda: copy.deepcopy(value))
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
        assert "500 米" in speech.synthesis[0] and "不是秒差" in speech.synthesis[0]
        assert service.snapshot()["requests_used"] == 0
    finally:
        voice.close()
        service.close(wait=True)
