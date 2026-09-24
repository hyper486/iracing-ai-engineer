"""Invented metadata/SDK and calibration only; not real iRacing evidence."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace

import pytest
from test_desktop_controller import Store
from test_desktop_controller import created as _created_fixture
from test_live_app_reader import Clock, FakeSdk, Stop, _values
from test_tire_model_validation import pin, request_value, write_request
from test_tire_performance_model import _identity

from iracing_ai_engineer import live_app
from iracing_ai_engineer.live_car_context import (
    SETUP_METHOD,
    CarMetadataProjector,
    LiveCarContext,
    _setup_copy,
    car_context_binding,
    car_context_notice,
    validated_car_context,
)
from iracing_ai_engineer.live_fuel import LiveFuelConfig
from iracing_ai_engineer.live_tire_calibration import (
    calibration_status,
    load_tire_calibration,
)
from iracing_ai_engineer.sdk_probe import RawSdkFrame, VariableDescriptor


@pytest.fixture
def created():
    yield from _created_fixture.__wrapped__()


def metadata():
    identity = _identity()
    mapping = {"series_id": "SeriesID", "season_id": "SeasonID", "race_week": "RaceWeek",
               "track_id": "TrackID", "event_type": "EventType", "track_config": "TrackConfigName",
               "sim_build": "BuildVersion", "official": "Official"}
    return {"WeekendInfo": {"SimMode": "full", "TrackLength": "1.2 km",
                           **{target: identity[source] for source, target in mapping.items()}},
        "SessionInfo": {"Sessions": [{"SessionNum": 0, "SessionType": "Race"}]},
        "DriverInfo": {"DriverCarIdx": 0, "DriverSetupName": "PRIVATE SETUP NAME",
            "Drivers": [{"CarIdx": 1, "CarClassID": 27, "CarID": 456,
                         "UserName": "PRIVATE OPPONENT"},
                        {"CarIdx": 0, "CarClassID": 27, "CarID": 123,
                         "UserName": "PRIVATE DRIVER"}]},
        "CarSetup": {"UpdateCount": 2, "Chassis": {"Rear": {"WingAngle": "7 deg"}},
                     "Tires": {"LeftFront": {"StartingPressure": "145 kPa",
                                             "LastHotPressure": "165 kPa",
                                             "LastTempsOMI": "70, 70, 70",
                                             "TreadRemaining": "90%"}}}}


def frame(tick=1, *, parked=True, update=1):
    values = {**_values(tick), "PlayerCarClass": 27, "TrackTempCrew": 30.0,
              "WindVel": 2.0, "WindDir": 1.0, "Precipitation": 0.0, "TrackWetness": 1,
              "Speed": 0.0 if parked else 40.0, "OnPitRoad": parked,
              "CarIdxOnPitRoad": [parked, False, False], "PlayerCarInPitStall": parked}
    return RawSdkFrame(buffer_tick=tick, session_info_update=update, values=values,
                       sim_mode_raw="full", captured_monotonic_s=1000 + tick / 60)


@pytest.fixture
def live():
    clock, projector, meta = Clock(), CarMetadataProjector(), metadata()
    state = live_app.AppState(clock=clock)
    state.connection("CONNECTED")
    analysis = live_app._LiveAnalysis(state, LiveFuelConfig(), identifier="invented-car-context",
        tick_rate=60, car_count=3, generation=state.generation, allowed=lambda: True)
    last = 0

    def feed(tick=1, *, payload=meta, parked=True, update=1, errors=(), changes=None):
        nonlocal last
        # Initial 31 ticks establish the actual monitor's freshness; subsequent
        # calls keep 60 Hz continuity, never fabricate a ready monitor object.
        for current in range(last + 1, tick + 32):
            raw = frame(current, parked=parked, update=update)
            raw.values.update(changes or {})
            raw = replace(raw, read_errors=errors)
            clock.now = raw.captured_monotonic_s
            projected = projector.project(payload, update, raw, "FULL")
            analysis.process((raw, "Race", clock.now, 1_200_000, projected))
            last = current
        return state.snapshot()

    yield state, analysis, clock, feed, meta
    analysis.close()


def calibration_for(snapshot, tmp_path):
    request = request_value.__wrapped__()
    identity = snapshot["car_context"]["identity"]
    request["applicability"].update(car_model_id=identity["car_model_id"],
                                    setup_sha256=identity["setup_sha256"])
    for row in request["lap_contexts"]:
        row.update(car_model_id=identity["car_model_id"], setup_sha256=identity["setup_sha256"])
    pin(request)
    path = write_request(request, tmp_path)
    return load_tire_calibration(path, expected_request_sha256=request["request_sha256"])


def install(state, snapshot, calibration):
    state.set_tire_calibration(calibration,
                              expected_binding=car_context_binding(snapshot, parked=True),
                              expected_revision=snapshot["tire_calibration"]["revision"])


def test_real_owner_binds_player_not_first_driver_and_removes_private_content(live):
    state, _, _, feed, _ = live
    snapshot = feed()
    context = validated_car_context(snapshot)
    assert context is not None
    assert context["identity"]["car_model_id"] == 123
    assert context["identity"]["setup_method"] == SETUP_METHOD
    assert context["conditions"]["precipitation_pct"] == 0
    assert car_context_binding(snapshot, parked=True) is not None
    assert "车型 ID 123" in car_context_notice(snapshot)
    for forbidden in ("PRIVATE DRIVER", "PRIVATE OPPONENT", "PRIVATE SETUP", "WingAngle",
                      "StartingPressure", "TreadRemaining", "DriverInfo"):
        assert forbidden not in json.dumps(state.snapshot())
    assert snapshot["tire_calibration"]["status"] == "UNLOADED"


def test_metadata_projection_cache_and_tire_measurement_exclusions(live):
    _, _, _, feed, meta = live
    first = feed()["car_context"]
    altered = copy.deepcopy(meta)
    altered["CarSetup"]["UpdateCount"] += 1
    altered["CarSetup"]["Tires"]["LeftFront"].update(LastHotPressure="175 kPa",
        LastTempsOMI="90, 90, 90", TreadRemaining="80%")
    second = feed(32, payload=altered, update=2)["car_context"]
    assert first["identity"] == second["identity"]
    assert first["revision"] == second["revision"]
    projector = CarMetadataProjector()
    raw = frame()
    projection = projector.project(meta, 1, raw, "FULL")
    assert projection is projector.project(meta, 1, raw, "FULL")
    assert "PRIVATE" not in json.dumps(projection)


@pytest.mark.parametrize("fault", ["driver", "duplicate", "car", "class", "event", "scope",
                                  "update", "read_error", "boolean_index", "no_setup"])
def test_metadata_uncertainty_is_not_an_owned_car(fault):
    meta, raw, scope, update = metadata(), frame(), "FULL", 1
    if fault == "driver":
        meta["DriverInfo"]["DriverCarIdx"] = 1
    elif fault == "duplicate":
        meta["DriverInfo"]["Drivers"].append(meta["DriverInfo"]["Drivers"][-1])
    elif fault == "car":
        meta["DriverInfo"]["Drivers"][-1]["CarID"] = None
    elif fault == "class":
        meta["DriverInfo"]["Drivers"][-1]["CarClassID"] = 28
    elif fault == "event":
        del meta["WeekendInfo"]["BuildVersion"]
    elif fault == "scope":
        scope = "PARTIAL"
    elif fault == "update":
        update = 2
    elif fault == "read_error":
        raw = replace(raw, read_errors=("PlayerCarClass",))
    elif fault == "boolean_index":
        meta["DriverInfo"]["DriverCarIdx"] = False
    else:
        del meta["CarSetup"]
    assert CarMetadataProjector().project(meta, update, raw, scope) is None


@pytest.mark.parametrize("value", [None, {}, {"UpdateCount": 3}, {"X": float("nan")},
                                  {"X": list(range(129))}, {"X": "a" * 4097},
                                  {"Tires": {"LeftFront": {"TreadRemaining": "100%"}}}])
def test_unsupported_setup_is_bounded(value):
    with pytest.raises(ValueError):
        _setup_copy(value)


def test_unknown_or_real_settings_are_not_removed(live):
    _, _, _, feed, meta = live
    first = feed()["car_context"]
    altered = copy.deepcopy(meta)
    altered["CarSetup"]["Tires"]["LeftFront"]["StartingPressure"] = "155 kPa"
    second = feed(32, payload=altered, update=2)["car_context"]
    assert second["identity"]["setup_sha256"] != first["identity"]["setup_sha256"]
    assert second["revision"] > first["revision"]
    altered = copy.deepcopy(altered)
    altered["CarSetup"]["NewUnknownSetting"] = 1
    third = feed(64, payload=altered, update=3)["car_context"]
    assert third["identity"]["setup_sha256"] != second["identity"]["setup_sha256"]


def test_bound_model_is_only_a_context_check_and_clears_on_source_loss(live, tmp_path):
    state, _, clock, feed, _ = live
    snapshot = feed()
    calibration = calibration_for(snapshot, tmp_path)
    install(state, snapshot, calibration)
    status = state.snapshot()["tire_calibration"]
    assert status["status"] == "CONTEXT_MATCH_ONLY"
    assert not status["live_model_admitted"] and not status["live_acceptance"]
    assert not status["persisted"]
    clock.now += 3
    assert state.snapshot()["tire_calibration"]["status"] == "UNLOADED"
    assert not validated_car_context(state.snapshot())


@pytest.mark.parametrize("changes,reason", [
    ({"TrackWetness": 2}, "DRY_CONDITIONS_NOT_OBSERVED"),
    ({"Precipitation": .1}, "DRY_CONDITIONS_NOT_OBSERVED"),
    ({"TrackTempCrew": 50.0}, "OUTSIDE_OBSERVED_CONDITION_RANGES"),
    ({"WindDir": 2.0}, "OUTSIDE_OBSERVED_CONDITION_RANGES"),
    ({"PlayerTireCompound": 1}, "COMPOUND_MISMATCH"),
    ({"AirTemp": None}, "CONDITIONS_UNAVAILABLE"),
])
def test_current_conditions_are_checked_each_publication(live, tmp_path, changes, reason):
    state, _, _, feed, _ = live
    snapshot = feed()
    install(state, snapshot, calibration_for(snapshot, tmp_path))
    current = feed(32, changes=changes)
    assert current["tire_calibration"]["status"] == "WAIT"
    assert current["tire_calibration"]["reason"] == reason


def test_metadata_loss_and_recovery_between_publications_revokes_model(live, tmp_path):
    state, _, _, feed, _ = live
    snapshot = feed()
    install(state, snapshot, calibration_for(snapshot, tmp_path))
    feed(2, payload=None)
    feed(3)
    current = feed(32)
    assert current["car_context"]["status"] == "BOUND"
    assert current["tire_calibration"]["status"] == "UNLOADED"


def test_changed_setup_cannot_keep_or_reapply_old_calibration(live, tmp_path):
    state, _, _, feed, meta = live
    snapshot = feed()
    calibration = calibration_for(snapshot, tmp_path)
    install(state, snapshot, calibration)
    changed = copy.deepcopy(meta)
    changed["CarSetup"]["Chassis"]["Rear"]["WingAngle"] = "8 deg"
    current = feed(32, payload=changed, update=2)
    assert current["tire_calibration"]["status"] == "UNLOADED"
    with pytest.raises(ValueError):
        install(state, current, calibration)


def test_pending_selection_cannot_survive_explicit_clear_or_driving(live, tmp_path):
    state, _, _, feed, _ = live
    snapshot = feed()
    calibration = calibration_for(snapshot, tmp_path)
    state.set_tire_calibration(None)
    with pytest.raises(ValueError):
        install(state, snapshot, calibration)
    current = feed(32, parked=False)
    with pytest.raises(ValueError):
        install(state, current, calibration)


def test_analysis_metadata_fault_isolated_from_fuel(live, monkeypatch):
    _, analysis, _, feed, _ = live

    def fail(*args):
        raise RuntimeError("private processing fault")

    monkeypatch.setattr(analysis._car_context, "feed", fail)
    current = feed()
    assert current["car_context"] is None and current["fuel"] is not None
    assert "private processing fault" not in json.dumps(current)


def test_condition_signal_is_not_sdk_wear_or_prior_tire_temperature(live):
    _, _, _, feed, _ = live
    current = feed(changes={"TrackTempCrew": None})
    assert current["car_context"]["identity"] is not None
    assert current["car_context"]["conditions"] is None
    assert "条件信号不完整" in car_context_notice(current)


def test_renderer_refuses_forged_model_promotion_or_metadata_text(live):
    _, _, _, feed, _ = live
    current = feed()
    current["car_context"]["identity"]["setup_sha256"] = "PRIVATE SETUP NAME"
    assert "PRIVATE" not in car_context_notice(current)
    current["car_context"] = ["private"]
    assert "证据不完整" in car_context_notice(current)


@pytest.mark.parametrize("metadata_fault", [False, True])
def test_reader_projects_selected_car_fields_and_keeps_spotter_independent(monkeypatch,
                                                                          metadata_fault):
    from test_live_app_reader import PacedWorker

    clock, stop = Clock(), Stop()

    class CarSdk(FakeSdk):
        def descriptors(self):
            fields = super().descriptors()
            extras = []
            for i, name in enumerate(("PlayerCarClass", "TrackWetness", "TrackTempCrew", "WindVel",
                                      "WindDir", "Precipitation")):
                extras.append(VariableDescriptor(name=name, type_code=2 if i < 2 else 5,
                    dtype="int32" if i < 2 else "float64", offset=4096 + i * 8, count=1,
                    count_as_time=False, unit="", description="synthetic"))
            return (*fields, *extras)

        def read_frozen(self, fields):
            self.read_count += 1
            raw = frame(self.read_count)
            clock.now = raw.captured_monotonic_s
            if self.read_count == 40:
                stop.stopped = True
            return replace(raw, values={key: raw.values[key] for key in fields})

        def session_info_snapshot(self):
            return metadata(), 1

    sdk = CarSdk(clock, stop, 40)
    state = live_app.AppState(clock=clock)
    captured = []
    spotter_frames = []
    publish = state.publish
    spotter_feed = state.feed_spotter

    def spotter(raw):
        spotter_frames.append(raw.buffer_tick)
        return spotter_feed(raw)

    def capture(*args, **kwargs):
        publish(*args, **kwargs)
        captured.append(state.snapshot())

    monkeypatch.setattr(state, "publish", capture)
    monkeypatch.setattr(state, "feed_spotter", spotter)
    if metadata_fault:
        def broken(*args):
            raise RuntimeError("private metadata fault")
        monkeypatch.setattr(CarMetadataProjector, "project", broken)
    live_app.run_reader(state, stop, LiveFuelConfig(), transport_factory=lambda: sdk,
                        worker_factory=PacedWorker, clock=clock, sleeper=lambda _: None)
    assert captured and spotter_frames == list(range(1, 41))
    assert all(row["fuel"] is not None for row in captured)
    if not metadata_fault:
        assert any((row.get("car_context") or {}).get("identity", {})
                   and row["car_context"]["identity"]["car_model_id"] == 123 for row in captured)
    else:
        assert all(row["car_context"]["identity"] is None for row in captured)
    assert state.report()["snapshots_seen"] >= 1


def test_invalid_boolean_dry_state_cannot_match(live, tmp_path):
    _, _, _, feed, _ = live
    current = feed()
    calibration = calibration_for(current, tmp_path)
    current["car_context"]["conditions"]["track_wetness"] = True
    assert calibration_status(calibration, current, 0)["status"] == "WAIT"


def test_spectator_has_no_owned_car_context():
    raw = frame()
    projected = CarMetadataProjector().project(metadata(), 1, raw, "FULL")
    owner = LiveCarContext()
    owner.feed(raw, projected)
    raw.values.update(IsOnTrack=False, IsOnTrackCar=False)
    owner.feed(raw, projected)
    assert owner._current is None


def controller_for(live, tmp_path, created):
    state, _, clock, _, _ = live
    controller = created(store=Store(tmp_path), clock=clock)
    controller._state = state
    controller._lifecycle = "RUNNING"
    return controller


def test_native_controller_loads_checked_file_off_thread_without_provider_or_persistence(
    live, tmp_path, created,
):
    state, _, _, feed, _ = live
    snapshot = feed()
    calibration = calibration_for(snapshot, tmp_path)
    controller = controller_for(live, tmp_path, created)
    path = tmp_path / "synthetic-request.json"
    controller.load_tire_calibration(path, expected_request_sha256=calibration.request_sha256)
    controller._job.join(3)
    assert not controller._job.is_alive()
    assert state.snapshot()["tire_calibration"]["status"] == "CONTEXT_MATCH_ONLY"
    assert controller._service.snapshot()["requests_used"] == 0
    assert not controller._store.saved and controller._reader is None
    controller.load_tire_calibration(path, expected_request_sha256="0" * 64)
    controller._job.join(3)
    assert state.snapshot()["tire_calibration"]["status"] == "UNLOADED"
    assert str(path) not in controller.snapshot()["notice"]


@pytest.mark.parametrize("change", ["clear", "disconnect", "new_setup"])
def test_background_file_verification_cannot_restore_withdrawn_selection(
    live, tmp_path, created, monkeypatch, change,
):
    import iracing_ai_engineer.live_tire_calibration as module

    state, _, _, feed, meta = live
    calibration = calibration_for(feed(), tmp_path)
    controller = controller_for(live, tmp_path, created)
    entered, release = threading.Event(), threading.Event()

    def pending(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return calibration

    monkeypatch.setattr(module, "load_tire_calibration", pending)
    try:
        controller.load_tire_calibration(tmp_path / "not-read.json",
                                         expected_request_sha256=calibration.request_sha256)
        assert entered.wait(1)
        if change == "clear":
            controller.load_tire_calibration(None)
        elif change == "disconnect":
            state.connection("DISCONNECTED")
        else:
            changed = copy.deepcopy(meta)
            changed["CarSetup"]["Chassis"]["Rear"]["WingAngle"] = "8 deg"
            feed(32, payload=changed, update=2)
    finally:
        release.set()
        controller._job.join(3)
    assert state.snapshot()["tire_calibration"]["status"] == "UNLOADED"


def test_failed_holdout_request_is_not_a_native_calibration(live, tmp_path):
    calibration_for(live[3](), tmp_path)
    path = tmp_path / "synthetic-request.json"
    request = json.loads(path.read_text("utf-8"))
    request["holdout_dataset"]["samples"][0]["late_lap"]["lap_time_s"] += 10
    pin(request)
    write_request(request, tmp_path)
    with pytest.raises(ValueError, match="HOLDOUT_WAIT"):
        load_tire_calibration(path, expected_request_sha256=request["request_sha256"])


def test_frozen_check_recipe_is_invented_and_never_opens_sdk_or_network(monkeypatch):
    from iracing_ai_engineer.llm_client import DeepSeekClient
    from iracing_ai_engineer.sdk_probe import WindowsPyirsdkTransport
    from iracing_ai_engineer.synthetic_car_context import run_synthetic_car_context

    def forbidden(*args, **kwargs):
        raise AssertionError("No SDK or provider in synthetic check")

    monkeypatch.setattr(DeepSeekClient, "complete", forbidden)
    monkeypatch.setattr(WindowsPyirsdkTransport, "startup", forbidden)
    result = run_synthetic_car_context()
    assert result["status"] == "PASS" and result["source_kind"] == "SYNTHETIC"
    assert not result["sdk_accessed"] and not result["provider_called"]
    assert not result["live_acceptance"]
