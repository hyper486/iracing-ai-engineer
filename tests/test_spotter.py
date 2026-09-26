"""Invented SDK frames only. Never connect to a simulator or play audio."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from iracing_ai_engineer.sdk_probe import RawSdkFrame
from iracing_ai_engineer.spotter import PHRASES, ProximitySpotter, SpotterConfig


def frame(tick, code=1, *, changes=None, **kwargs):
    values = {
        "SessionNum": 0, "SessionTick": tick, "SessionTime": tick / 60,
        "PlayerCarIdx": 0, "CarLeftRight": code,
        "IsOnTrack": True, "IsOnTrackCar": True, "IsReplayPlaying": False,
        "OnPitRoad": False, "PlayerCarInPitStall": False,
    }
    values.update(changes or {})
    options = {"sim_mode_raw": "full", "captured_monotonic_s": 100 + tick / 60}
    options.update(kwargs)
    return RawSdkFrame(buffer_tick=tick, session_info_update=1, values=values, **options)


def feed(model, tick, code=1, **kwargs):
    sample = frame(tick, code, **kwargs)
    now = 100 + tick / 60
    model.feed(sample, now=now)
    return model.snapshot(now=now)


def kinds(model):
    return [row["kind"] for row in model.audit() if row["decision"] == "CANDIDATE"]


@pytest.mark.parametrize("code,kind", [
    (2, "CAR_LEFT"), (3, "CAR_RIGHT"), (4, "CARS_BOTH_SIDES"),
    (5, "TWO_CARS_LEFT"), (6, "TWO_CARS_RIGHT"),
])
def test_valid_proximity_does_not_need_fuel_opponent_arrays_or_completed_laps(code, kind):
    model = ProximitySpotter()
    assert feed(model, 1, code)["status"] == "ACQUIRING"
    assert feed(model, 2, code)["candidate"] is None
    result = feed(model, 3, code)
    assert result["status"] == "READY"
    assert result["candidate"]["kind"] == kind
    assert result["candidate"]["text"] == PHRASES[kind]
    assert result["candidate"]["expires_in_s"] == pytest.approx(0.75)
    assert result["audible"] is result["live_acceptance"] is False
    assert result["audio_status"] == "NOT_CONNECTED"


def test_starting_on_clear_track_never_claims_a_previously_occupied_side_cleared():
    model = ProximitySpotter()
    for tick in range(1, 31):
        result = feed(model, tick)
    assert result["status"] == "READY"
    assert result["candidate"] is None
    assert kinds(model) == []


@pytest.mark.parametrize("offset", [-1000, 1000])
def test_independent_buffer_and_session_clock_origins_preserve_proximity_transitions(offset):
    model = ProximitySpotter()
    for tick in range(2000, 2013):
        code = 2 if tick < 2003 else 1
        sample = replace(frame(tick, code), buffer_tick=tick + offset)
        model.feed(sample, now=sample.captured_monotonic_s)
    result = model.snapshot(now=sample.captured_monotonic_s)
    assert result["status"] == "READY"
    assert result["candidate"]["kind"] == "ALL_CLEAR"
    assert kinds(model) == ["CAR_LEFT", "ALL_CLEAR"]


@pytest.mark.parametrize("buffer_tick", [None, True, False, -1, 1.0, "1", 2**31])
def test_invalid_publication_clock_cannot_arm(buffer_tick):
    model = ProximitySpotter()
    sample = replace(frame(1, 2), buffer_tick=buffer_tick)
    model.feed(sample, now=sample.captured_monotonic_s)
    assert model.snapshot(now=sample.captured_monotonic_s)["reason"] == "CORE_FIELD_INVALID"
    assert not kinds(model)


@pytest.mark.parametrize("buffer_tick,session_tick,expected", [
    (1003, 4, "CONFLICTING_DUPLICATE"),
    (1004, 3, "CONFLICTING_DUPLICATE"),
    (1002, 4, "NEED_PROGRESSING_TICKS"),
    (1100, 4, "NEED_PROGRESSING_TICKS"),
])
def test_each_independent_clock_must_remain_fresh_and_progressing(
    buffer_tick, session_tick, expected,
):
    model = ProximitySpotter()
    for tick in range(1, 4):
        sample = replace(frame(tick, 2), buffer_tick=tick + 1000)
        model.feed(sample, now=sample.captured_monotonic_s)
    assert kinds(model) == ["CAR_LEFT"]
    sample = replace(frame(4, 1, changes={"SessionTick": session_tick}),
                     buffer_tick=buffer_tick)
    model.feed(sample, now=sample.captured_monotonic_s)
    result = model.snapshot(now=sample.captured_monotonic_s)
    assert result["reason"] == expected
    assert result["candidate"] is None
    assert kinds(model) == ["CAR_LEFT"]
    if buffer_tick == 1002:
        assert any(row["reason"] == "TIMELINE_REGRESSION" for row in model.audit())
    elif buffer_tick == 1100:
        assert any(row["reason"] == "CONTINUITY_GAP" for row in model.audit())


def test_clear_requires_sustained_confirmation_and_new_hazard_withdraws_it_immediately():
    model = ProximitySpotter()
    for tick in range(1, 4):
        feed(model, tick, 2)
    for tick in range(4, 13):
        assert feed(model, tick, 1)["candidate"] is None
    assert feed(model, 13, 1)["candidate"]["kind"] == "ALL_CLEAR"
    assert feed(model, 14, 3)["candidate"] is None
    feed(model, 15, 3)
    assert feed(model, 16, 3)["candidate"]["kind"] == "CAR_RIGHT"
    assert kinds(model) == ["CAR_LEFT", "ALL_CLEAR", "CAR_RIGHT"]


def test_single_tick_clear_flicker_does_not_emit_clear_or_duplicate_hazard():
    model = ProximitySpotter()
    for tick in range(1, 4):
        feed(model, tick, 2)
    feed(model, 4, 1)
    for tick in range(5, 25):
        feed(model, tick, 2)
    assert kinds(model) == ["CAR_LEFT"]


@pytest.mark.parametrize("invalid", [None, True, False, "2", -1, 7, 2.0, float("nan")])
def test_invalid_proximity_is_not_clear_or_a_truthy_enum(invalid):
    model = ProximitySpotter()
    for tick in range(1, 4):
        feed(model, tick, 2)
    result = feed(model, 4, invalid)
    assert result["status"] == "UNAVAILABLE"
    assert result["candidate"] is None
    for tick in range(5, 25):
        feed(model, tick, 1)
    assert kinds(model) == ["CAR_LEFT"]


def test_sdk_off_is_not_clear():
    result = feed(ProximitySpotter(), 1, 0)
    assert result["status"] == "UNAVAILABLE"
    assert result["reason"] == "SDK_SPOTTER_OFF"


@pytest.mark.parametrize("changes", [
    {"IsOnTrack": False}, {"IsOnTrack": 1}, {"IsOnTrackCar": False},
    {"IsReplayPlaying": True}, {"OnPitRoad": True}, {"OnPitRoad": None},
    {"PlayerCarInPitStall": True}, {"PlayerCarIdx": "0"}, {"PlayerCarIdx": True},
    {"SessionNum": -1}, {"SessionTime": "1"}, {"SessionTime": float("inf")},
    {"SessionTick": 99},
])
def test_invalid_context_cannot_create_an_alert(changes):
    model = ProximitySpotter()
    for tick in range(1, 20):
        result = feed(model, tick, 2, changes=changes)
        assert result["status"] != "READY"
        assert result["candidate"] is None
    assert not kinds(model)


@pytest.mark.parametrize("mode", [None, "replay", "full ", True, "unknown"])
def test_replay_and_unknown_modes_never_arm(mode):
    model = ProximitySpotter()
    assert feed(model, 1, 2, sim_mode_raw=mode)["reason"] == "NOT_LIVE_SOURCE"


def test_critical_read_error_rejects_but_unrelated_error_does_not():
    model = ProximitySpotter()
    for tick in range(1, 4):
        result = feed(model, tick, 2, read_errors=("FuelLevel", "CarIdxLapDistPct"))
    assert result["candidate"]["kind"] == "CAR_LEFT"
    assert feed(model, 4, 2, read_errors=("CarLeftRight",))["reason"] == "FIELD_READ_ERROR"


def test_duplicate_ticks_do_not_refresh_readiness_or_emit_hold_reminders():
    model = ProximitySpotter()
    for tick in range(1, 4):
        feed(model, tick, 2)
    frozen = frame(3, 2)
    for elapsed in (0.1, 0.2, 0.3):
        now = frozen.captured_monotonic_s + elapsed
        model.feed(replace(frozen, captured_monotonic_s=now), now=now)
    result = model.snapshot(now=now)
    assert result["status"] == "STALE"
    assert result["candidate"] is None
    assert kinds(model) == ["CAR_LEFT"]


def test_conflicting_duplicate_invalidates_pending_speech():
    model = ProximitySpotter()
    for tick in range(1, 4):
        feed(model, tick, 2)
    result = feed(model, 3, 3)
    assert result["reason"] == "CONFLICTING_DUPLICATE"
    assert result["candidate"] is None


@pytest.mark.parametrize("changes", [{"SessionNum": 1}, {"PlayerCarIdx": 2}])
def test_new_session_or_player_does_not_inherit_clear_transition(changes):
    model = ProximitySpotter()
    for tick in range(1, 4):
        feed(model, tick, 2)
    epoch = model.snapshot(now=100.05)["epoch"]
    for tick in range(4, 25):
        result = feed(model, tick, 1, changes=changes)
    assert result["epoch"] > epoch
    assert kinds(model) == ["CAR_LEFT"]


def test_gap_drops_candidate_and_reacquires_without_clear():
    model = ProximitySpotter()
    for tick in range(1, 4):
        feed(model, tick, 2)
    assert feed(model, 30, 1)["status"] == "ACQUIRING"
    for tick in range(31, 45):
        feed(model, tick, 1)
    assert kinds(model) == ["CAR_LEFT"]
    assert any(row["reason"] == "CONTINUITY_GAP" for row in model.audit())


def test_repeat_is_bounded_and_expired_candidates_are_not_a_backlog():
    model = ProximitySpotter()
    for tick in range(1, 310):
        result = feed(model, tick, 2)
        if tick == 60:
            assert result["candidate"] is None
    assert kinds(model) == ["CAR_LEFT", "STILL_LEFT"]
    assert result["candidate"]["kind"] == "STILL_LEFT"
    assert any(row["decision"] == "EXPIRED" for row in model.audit())


@pytest.mark.parametrize("captured", [None, float("nan"), float("inf"), 99.0, 101.0])
def test_missing_future_or_old_capture_timestamp_is_rejected(captured):
    result = feed(ProximitySpotter(), 1, 2, captured_monotonic_s=captured)
    assert result["status"] == "STALE"


def test_freshness_is_measured_from_capture_not_the_later_consumer_timestamp():
    model = ProximitySpotter()
    for tick in range(1, 4):
        sample = frame(tick, 2)
        model.feed(sample, now=sample.captured_monotonic_s + 0.2)
    assert model.snapshot(now=100.25)["candidate"]["kind"] == "CAR_LEFT"
    assert model.snapshot(now=100.301)["status"] == "STALE"


def test_capture_regression_is_not_treated_as_continuous_observation():
    model = ProximitySpotter()
    for tick in range(1, 4):
        feed(model, tick, 2)
    result = feed(model, 4, 1, captured_monotonic_s=100.04)
    assert result["status"] == "ACQUIRING"
    assert any(row["reason"] == "CAPTURE_TIME_NOT_PROGRESSING" for row in model.audit())
    assert result["candidate"] is None


def test_clock_regression_invalidates_and_does_not_rearm_on_old_time():
    model = ProximitySpotter()
    for tick in range(1, 4):
        feed(model, tick, 2)
    for now in (99.0, 99.5, 100.0):
        result = model.snapshot(now=now)
        assert result["reason"] == "CLOCK_REGRESSION"
        assert result["candidate"] is None


def test_disconnect_clears_readiness_without_inventing_a_clear_event():
    model = ProximitySpotter()
    for tick in range(1, 4):
        feed(model, tick, 2)
    model.unavailable("DISCONNECTED", now=100.06)
    assert model.snapshot(now=100.06)["candidate"] is None
    assert kinds(model) == ["CAR_LEFT"]


def test_app_state_samples_observation_time_inside_the_shared_snapshot_lock():
    from iracing_ai_engineer.live_app import AppState

    observed_under_lock = []

    def clock():
        observed_under_lock.append(state._lock.locked())
        return 100.05

    state = AppState(clock=clock)
    for tick in range(1, 4):
        state.feed_spotter(frame(tick, 2))
    assert state.snapshot()["spotter"]["candidate"]["kind"] == "CAR_LEFT"
    assert observed_under_lock and all(observed_under_lock)


def test_replay_is_deterministic_bounded_and_does_not_echo_unrelated_private_fields():
    def run():
        model = ProximitySpotter()
        for tick in range(1, 1600):
            feed(model, tick, 2 if (tick // 10) % 2 else 3, changes={
                "UserName": "SYNTHETIC PRIVATE PERSON", "OpponentLapTime": "bad string",
            })
        return model.snapshot(now=100 + 1599 / 60), model.audit()

    first, second = run(), run()
    assert first == second
    result, audit = first
    assert result["audit_count"] > 128 and len(audit) == 128
    assert all(row["audible"] is False for row in audit)
    assert "SYNTHETIC PRIVATE PERSON" not in json.dumps(first)
    assert "bad string" not in json.dumps(first)


@pytest.mark.parametrize("rate", [0, -1, True, 60.0, 361])
def test_tick_rate_validation(rate):
    with pytest.raises(ValueError, match="INVALID_SPOTTER_TICK_RATE"):
        ProximitySpotter(tick_rate_hz=rate)


@pytest.mark.parametrize("kwargs", [
    {"max_age_s": float("nan")}, {"occupied_confirm_s": -1},
    {"clear_confirm_s": True}, {"repeat_s": 0}, {"intent_ttl_s": 10},
])
def test_config_validation(kwargs):
    with pytest.raises(ValueError, match="INVALID_SPOTTER_CONFIG"):
        SpotterConfig(**kwargs)
