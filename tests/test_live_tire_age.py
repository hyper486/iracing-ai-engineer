"""Invented telemetry/driver assertions only; no SDK or real service acceptance."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from iracing_ai_engineer import live_app
from iracing_ai_engineer.live_fuel import LiveFuelConfig
from iracing_ai_engineer.live_monitor import LiveMonitor
from iracing_ai_engineer.live_queries import render_live_query
from iracing_ai_engineer.live_tire_age import (
    BASIS,
    LiveTireAgeTracker,
    TireConfirmation,
    confirmation_command,
    tire_age_binding,
    tire_age_notice,
    validated_tire_age,
)
from iracing_ai_engineer.llm_engineer import _binding, _selected_binding
from iracing_ai_engineer.llm_evidence import build_live_context
from iracing_ai_engineer.synthetic_runtime import run_synthetic_tire_confirmation, synthetic_frames
from iracing_ai_engineer.trial_audit import TrialAudit, detector_reset, validate_projection
from iracing_ai_engineer.trial_replay import replay_trial


class Rig:
    def __init__(self):
        self.tracker = LiveTireAgeTracker(60)
        self.monitor = LiveMonitor(source_id="synthetic", session_id="synthetic",
                                   sdk_tick_rate_hz=60, expected_car_count=3)
        self.base = next(synthetic_frames(8))
        self.values = {**self.base.values, "LapCompleted": 3, "Lap": 4,
                       "PlayerCarInPitStall": False, "PitstopActive": False,
                       "PlayerTireCompound": 0, "TireSetsUsed": 0}
        self.tick = 0
        self.monitor.feed(self.base)
        self.tracker.feed(self.base, self.monitor.latest_sample)
        self.value = None

    def step(self, *, read_errors=(), tick_step=1, **changes):
        self.tick += tick_step
        self.values.update(SessionTick=self.tick, SessionTime=self.tick / 60, **changes)
        if "LapCompleted" in changes:
            self.values["Lap"] = self.values["LapCompleted"] + 1
        frame = replace(self.base, buffer_tick=self.tick, values=self.values.copy(),
                        read_errors=read_errors, captured_monotonic_s=self.tick / 60 + 1)
        assert self.monitor.feed(frame)
        self.tracker.feed(frame, self.monitor.latest_sample)
        monitor = self.monitor.snapshot()
        self.value = {"contract_version": "experimental-live-fuel-app-v1",
                      "source_mode": "LIVE", "connection": "CONNECTED", "generation": 1,
                      "updated_age_s": 0., "monitor": monitor,
                      "tire_age": self.tracker.snapshot(monitor)}
        return self.value

    def park(self):
        self.step(OnPitRoad=False, PlayerCarInPitStall=False, Speed=30.)
        return self.step(OnPitRoad=True, PlayerCarInPitStall=True, Speed=0.)

    def confirm(self, kind="FULL_NEW_SET"):
        command = confirmation_command(self.value, kind)
        receipt = self.tracker.confirm(command)
        assert receipt is not None
        return receipt

    def exit(self):
        return self.step(OnPitRoad=False, PlayerCarInPitStall=False, Speed=30.)

    def known(self):
        self.park()
        self.confirm()
        return self.exit()


def test_no_automatic_installation_on_attach_counter_change_or_pit_exit():
    rig = Rig()
    assert rig.step()["tire_age"]["origin"] is None
    rig.park()
    assert rig.exit()["tire_age"]["origin"] is None
    assert rig.step(TireSetsUsed=1)["tire_age"]["origin"] is None
    rig.park()
    rig.confirm("NO_TIRE_CHANGE")
    assert rig.exit()["tire_age"]["origin"] is None


def test_full_confirmation_then_observed_exit_counts_only_crossings_since_exit():
    rig = Rig()
    rig.park()
    receipt = rig.confirm()
    pending = rig.step()
    assert validated_tire_age(pending)["origin"] is None
    assert "等待连续出站" in tire_age_notice(pending)
    at_exit = rig.exit()
    assert at_exit["tire_age"]["origin"]["confirmation"] == receipt
    value = rig.step(LapCompleted=4)
    age = validated_tire_age(value)
    assert age["counter_increase"] == 1 and age["basis"] == BASIS
    assert age["physical_wear"] is None and age["physical_tire_age"] is None
    answer = render_live_query(build_live_context(value), "tire")
    assert "tire.driver_confirmed_age" in answer["fact_ids"]
    assert "车手" in answer["spoken_text"] and "证据不足" in answer["spoken_text"]


@pytest.mark.parametrize("kind", [None, "FULL_NEW_SET", "NO_TIRE_CHANGE", "PARTIAL_OR_UNKNOWN"])
def test_subsequent_visit_requires_explicit_confirmation(kind):
    rig = Rig()
    original = rig.known()["tire_age"]["origin"]
    rig.step(LapCompleted=4)
    rig.park()
    if kind:
        rig.confirm(kind)
    value = validated_tire_age(rig.exit())
    if kind == "NO_TIRE_CHANGE":
        assert value["origin"] == original and value["counter_increase"] == 1
    elif kind == "FULL_NEW_SET":
        assert value["origin"] != original and value["counter_increase"] == 0
    else:
        assert value["origin"] is None and value["counter_increase"] is None


@pytest.mark.parametrize("kind", ["FULL_NEW_SET", "PARTIAL_OR_UNKNOWN"])
def test_correcting_new_or_unknown_service_to_unchanged_cannot_restore_old_origin(kind):
    rig = Rig()
    rig.known()
    rig.park()
    rig.confirm(kind)
    rig.step()
    rig.confirm("NO_TIRE_CHANGE")
    assert rig.exit()["tire_age"]["origin"] is None


@pytest.mark.parametrize("changes", [
    {"Speed": .11}, {"PlayerCarInPitStall": False}, {"PitstopActive": True},
    {"PitstopActive": None}, {"Speed": None}, {"PlayerTireCompound": None},
    {"read_errors": ("Speed",)}, {"read_errors": ("PitstopActive",)},
    {"read_errors": ("PlayerCarInPitStall",)},
])
def test_no_confirmation_while_moving_servicing_or_fields_unknown(changes):
    rig = Rig()
    rig.park()
    value = rig.step(**changes)
    with pytest.raises(ValueError, match="TIRE_CONFIRMATION_NOT_READY"):
        confirmation_command(value, "FULL_NEW_SET")


def test_attach_already_in_stall_does_not_claim_an_observed_pit_visit():
    rig = Rig()
    rig.step(OnPitRoad=True, PlayerCarInPitStall=True, Speed=0.)
    with pytest.raises(ValueError, match="TIRE_CONFIRMATION_NOT_READY"):
        confirmation_command(rig.value, "FULL_NEW_SET")


@pytest.mark.parametrize("changes", [
    {"tick_step": 16}, {"LapCompleted": 0}, {"LapCompleted": 8},
    {"SessionNum": 1}, {"PlayerCarIdx": 1}, {"IsReplayPlaying": True},
    {"IsOnTrackCar": False}, {"PlayerTireCompound": 1}, {"TireSetsUsed": 1},
    {"TireSetsUsed": None}, {"read_errors": ("OnPitRoad",)},
    {"read_errors": ("TireSetsUsed",)},
])
def test_interruption_withdraws_age_and_cannot_silently_restore(changes):
    rig = Rig()
    original = rig.known()
    old = copy.deepcopy(rig.values)
    interrupted = rig.step(**changes)
    assert interrupted["tire_age"]["origin"] is None
    old.pop("SessionTick")
    old.pop("SessionTime")
    recovered = rig.step(**old)
    assert recovered["tire_age"]["origin"] is None
    assert tire_age_binding(recovered) != tire_age_binding(original)


@pytest.mark.parametrize("changes", [
    {"PitstopActive": True}, {"read_errors": ("PitstopActive",)},
    {"PlayerTireCompound": 1}, {"TireSetsUsed": 1},
])
def test_pending_confirmation_lost_after_service_or_tire_context_changes(changes):
    rig = Rig()
    rig.park()
    rig.confirm()
    rig.step(**changes)
    assert rig.exit()["tire_age"]["origin"] is None


def test_leaving_then_reentering_stall_revokes_the_service_confirmation():
    rig = Rig()
    rig.park()
    rig.confirm()
    rig.step(PlayerCarInPitStall=False, Speed=5.)
    rig.step(PlayerCarInPitStall=True, Speed=0.)
    assert rig.exit()["tire_age"]["origin"] is None


@pytest.mark.parametrize("change", ["duplicate", "reset", "late", "moved"])
def test_analysis_owner_rechecks_and_consumes_only_current_command(change):
    rig = Rig()
    rig.park()
    command = confirmation_command(rig.value, "FULL_NEW_SET")
    if change == "duplicate":
        assert rig.tracker.confirm(command)
    elif change == "reset":
        rig.tracker.reset("SOURCE_STALE")
        rig.step()
    elif change == "late":
        for _ in range(61):
            rig.step()
    else:
        rig.step(Speed=1.)
    assert rig.tracker.confirm(command) is None


@pytest.mark.parametrize("age", [None, True, -.1, .751, float("nan"), float("inf")])
def test_stale_app_observation_cannot_accept_confirmation(age):
    rig = Rig()
    value = rig.park()
    value["updated_age_s"] = age
    with pytest.raises(ValueError):
        confirmation_command(value, "FULL_NEW_SET")


@pytest.mark.parametrize("path,bad", [
    (("basis",), "REVIEWED_FULL_NEW_SET"), (("physical_wear",), .9),
    (("physical_tire_age",), 4), (("revision",), True), (("binding_sha256",), "b" * 64),
    (("monitor_sequence",), 999), (("point", "tick"), True),
    (("point", "laps"), 100), (("point", "parked"), True),
    (("origin", "exit_laps"), -1), (("origin", "exit_tick"), 9999),
    (("origin", "confirmation", "kind"), "NO_TIRE_CHANGE"),
    (("origin", "confirmation", "compound"), 2),
    (("origin", "confirmation", "decision_tick"), 9999),
    (("counter_increase",), 17), (("confirmation", "player_car_idx"), 9),
    (("visit_tick",), 0), (("can_confirm",), True),
])
def test_mutated_projection_cannot_reach_question_facts(path, bad):
    value = Rig().known()
    target = value["tire_age"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    assert validated_tire_age(value) is None
    assert "tire.driver_confirmed_age" not in {
        row["id"] for row in build_live_context(value)["facts"]}


def test_every_projection_key_is_required():
    pristine = Rig().known()
    assert validated_tire_age(pristine)
    for key in pristine["tire_age"]:
        value = copy.deepcopy(pristine)
        del value["tire_age"][key]
        assert validated_tire_age(value) is None, key


def test_confirmed_age_is_bound_for_ordinary_and_cloud_selected_tire_facts():
    rig = Rig()
    before = rig.known()
    after = rig.step(LapCompleted=4)
    for intent in ("tire", None):
        assert _selected_binding(_binding(before), ["tire.driver_confirmed_age"], intent) != (
            _selected_binding(_binding(after), ["tire.driver_confirmed_age"], intent))


@pytest.mark.parametrize("fault", ["startup", "feed", "snapshot", "confirm"])
def test_tire_analysis_fault_isolated_from_fuel_spotter_and_driving(monkeypatch, fault):
    def broken(*_args, **_kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_ERROR")
    if fault == "startup":
        monkeypatch.setattr(live_app, "LiveTireAgeTracker", broken)
    else:
        monkeypatch.setattr(LiveTireAgeTracker, fault, broken)
    state = live_app.AppState(clock=lambda: now[0])
    now = [1.]
    state.connection("CONNECTED")
    state.start_spotter(60)
    analysis = live_app._LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic",
                                      tick_rate=60, car_count=3, generation=state.generation,
                                      allowed=lambda: True)
    try:
        for frame in synthetic_frames(8):
            now[0] = frame.captured_monotonic_s
            state.feed_spotter(frame)
            if fault == "confirm" and frame.buffer_tick == 10:
                state._tire_confirmation = TireConfirmation("FULL_NEW_SET", 0, 1, 9, 0, 0)
            analysis.process((frame, "Race", now[0], 1_200_000))
            if frame.buffer_tick >= 60:
                break
        value = state.snapshot()
        assert value["tire_age"]["reason"] == "TIRE_PROCESSING_ERROR"
        assert "fuel.current" in {row["id"] for row in build_live_context(value)["facts"]}
        assert value["spotter"]["status"] == "READY"
        assert value["driving"]["worker"]["failed"] is False
        assert "SYNTHETIC_PRIVATE" not in json.dumps(value)
    finally:
        analysis.close()


def test_mailbox_has_one_slot_and_disconnect_discards_unconsumed_confirmation():
    rig = Rig()
    value = rig.park()
    state = live_app.AppState(clock=lambda: 10.)
    state.connection("CONNECTED")
    state.publish(value["monitor"], {}, None, "Race", tire_age=value["tire_age"])
    state.confirm_tire_service("FULL_NEW_SET")
    assert state.snapshot()["tire_confirmation_status"] == "QUEUED"
    with pytest.raises(ValueError):
        state.confirm_tire_service("FULL_NEW_SET")
    generation = state.generation
    assert state.take_tire_confirmation(generation + 1) is None
    state.connection("DISCONNECTED")
    assert state.take_tire_confirmation(generation) is None
    assert state.take_tire_confirmation(state.generation) is None


def test_private_assertion_journal_records_bounded_enums_not_service_proof(tmp_path):
    rig = Rig()
    rig.park()
    receipt = rig.confirm()
    journal = TrialAudit(tmp_path)
    path = tmp_path / journal.snapshot()["file_name"]
    journal.offer("detector", detector_reset(1, 60))
    journal.offer("tire_confirmation", {"generation": 1, "assertion": receipt})
    journal.close(complete=True)
    report = replay_trial(path)
    assert report["tire_assertions"] == 1 and report["tire_age_recomputed"] is False
    assert report["tire_service_verified"] is False and report["live_acceptance"] is False
    assert report["tire_assertions_tail"][0]["assertion"] == receipt
    with pytest.raises(ValueError):
        validate_projection("tire_confirmation", {"generation": 1,
                            "assertion": {**receipt, "transcript": "SYNTHETIC_PRIVATE_TEXT"}})


def test_real_mailbox_owner_local_query_and_withdrawal_synthetic_self_test():
    assert run_synthetic_tire_confirmation() == {
        "id": "SYNTHETIC_DRIVER_CONFIRMED_TIRES", "status": "PASS"}


def test_synthetic_self_test_rejects_silent_dropped_assertion(monkeypatch):
    monkeypatch.setattr(LiveTireAgeTracker, "confirm", lambda *_args: None)
    assert run_synthetic_tire_confirmation()["status"] == "FAIL"


def test_conflicting_exit_service_fields_cannot_commit_pending_origin():
    rig = Rig()
    rig.park()
    rig.confirm()
    assert rig.step(OnPitRoad=False, PlayerCarInPitStall=False, PitstopActive=True)[
        "tire_age"]["origin"] is None
