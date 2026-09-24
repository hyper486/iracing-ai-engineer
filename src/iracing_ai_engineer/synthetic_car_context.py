"""Frozen runtime binding check with invented frames and a test-only calibration.

No SDK, provider, calibration evidence file, microphone or output device. This
checks native context matching/withdrawal, not the truth of a tire model.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import islice

from .live_app import AppState, _LiveAnalysis
from .live_car_context import CarMetadataProjector, car_context_binding
from .live_fuel import LiveFuelConfig
from .live_tire_calibration import TireCalibration
from .synthetic_runtime import synthetic_frames


def run_synthetic_car_context():
    now = [0.]
    state = AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    analysis = _LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic-car-binding-only",
        tick_rate=60, car_count=3, generation=state.generation, allowed=lambda: True)
    projector = CarMetadataProjector()
    metadata = {"WeekendInfo": {"SeriesID": 501, "SeasonID": 601, "RaceWeek": 12,
        "TrackID": 101, "EventType": "Race", "TrackConfigName": "Invented circuit",
        "BuildVersion": "synthetic-only", "Official": True},
        "DriverInfo": {"DriverCarIdx": 0, "Drivers": [{"CarIdx": 0, "CarID": 123,
                                                       "CarClassID": 27}]},
        "CarSetup": {"Chassis": {"WingSetting": 7}, "UpdateCount": 1}}
    matched = condition_withdrawn = setup_withdrawn = False
    try:
        for index, raw in enumerate(islice(synthetic_frames(8), 132)):
            now[0] = raw.captured_monotonic_s
            if index == 96:
                metadata = {**metadata, "CarSetup": {"Chassis": {"WingSetting": 8},
                                                      "UpdateCount": 2}}
            values = {**raw.values, "PlayerCarClass": 27, "TrackWetness": 1, "Speed": 0.,
                "OnPitRoad": True, "PlayerCarInPitStall": True,
                "CarIdxOnPitRoad": [True, False, False], "WindVel": 1. if index < 64 else 10.}
            frame = replace(raw, values=values, session_info_update=1 if index < 96 else 2)
            metadata_projection = projector.project(metadata, frame.session_info_update, frame,
                                                     "FULL")
            analysis.process((frame, "Race", now[0], 1_200_000, metadata_projection))
            snapshot = state.snapshot()
            if index == 32:
                identity = snapshot["car_context"]["identity"]
                # An invented prevalidated object, not a real reviewed model.
                calibration = TireCalibration("a" * 64, "b" * 64, identity["car_model_id"],
                    identity["setup_sha256"], identity["identity_sha256"], 0,
                    (("air_temp_c", 20., 22.), ("track_temp_c", 29., 31.),
                     ("wind_x_mps", 1., 4.), ("wind_y_mps", 0., 4.)))
                state.set_tire_calibration(calibration,
                    expected_binding=car_context_binding(snapshot, parked=True),
                    expected_revision=snapshot["tire_calibration"]["revision"])
                matched = state.snapshot()["tire_calibration"]["status"] == "CONTEXT_MATCH_ONLY"
            if index == 94:
                condition_withdrawn = snapshot["tire_calibration"]["reason"] == (
                    "OUTSIDE_OBSERVED_CONDITION_RANGES")
            if index == 131:
                setup_withdrawn = snapshot["tire_calibration"]["status"] == "UNLOADED"
        passed = matched and condition_withdrawn and setup_withdrawn
    finally:
        analysis.close()
    return {"status": "PASS" if passed else "FAIL", "source_kind": "SYNTHETIC",
        "sdk_accessed": False, "provider_called": False, "live_acceptance": False,
        "checks": [{"id": "SYNTHETIC_TIRE_CALIBRATION_BINDING",
                    "status": "PASS" if passed else "FAIL"}]}
