"""Invented calibration + learned fuel + confirmed origin + local voice query.

Only the aggregate SYNTHETIC check escapes. Dataset labels and internal SDK-like
tags test production contracts, not authenticity, hearing, or race acceptance.
No SDK startup, provider request, actual calibration, or simulator controls.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from .engineer_session import canonical_sha256 as digest
from .live_app import AppState, _LiveAnalysis
from .live_car_context import CarMetadataProjector, car_context_binding
from .live_fuel import LiveFuelConfig
from .live_strategy import StrategyParameters
from .live_tire_calibration import load_tire_calibration
from .live_tire_comparison import validated_tire_comparison
from .llm_engineer import EngineerService
from .retrieved_live_analysis import (
    MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION,
    build_tire_performance_model,
)
from .synthetic_runtime import synthetic_frames
from .tire_model_validation import REQUEST_VERSION


def _request(event_identity, identity):
    """Wholly invented independent labels, lap times, fuel effect and conditions."""
    fuel = {"seconds_per_liter": .03, "seconds_per_liter_uncertainty": [.025, .035],
            "source_receipt_sha256": digest("synthetic fuel source"),
            "status": "CALIBRATED_FUEL_LOAD_EFFECT"}
    fuel["model_sha256"] = digest(fuel)
    scope = {"car_model_id": identity["car_model_id"], "setup_sha256": identity["setup_sha256"],
             "review_receipt_sha256": digest("synthetic car/setup review")}
    request, contexts = {"contract_version": REQUEST_VERSION, "applicability": scope}, []
    for split in ("training", "holdout"):
        samples = []
        for index, (early_age, late_age, early_fuel, slope) in enumerate(
            ((1, 30, 4., .20), (2, 31, 3.8, .18), (1, 32, 4.2, .22))
        ):
            label = f"synthetic-{split}-{index}"
            sample = {"sample_id": label, "stint_id": label,
                **{key: digest([label, key]) for key in (
                    "condition_match_receipt_sha256", "label_receipt_sha256",
                    "source_receipt_sha256")},
                "tire_installation": {"decision_tick": 1, "laps_completed": 5,
                    "tire_compound": 0, "tire_sets_used": index, "kind": "FULL_NEW_SET",
                    "label_receipt_sha256": digest([label, "origin"])}}
            for role, age, amount, lap_time in (
                ("early_lap", early_age, early_fuel, 100. + index),
                ("late_lap", late_age, .2,
                 100. + index + slope * (late_age - early_age) + .03 * (.2 - early_fuel)),
            ):
                sample[role] = {"fuel_start_l": amount, "lap_id": f"{label}-{role}",
                    "lap_time_s": lap_time, "stint_age_laps": age, "laps_completed": 5 + age,
                    "session_tick": 100 + age * 100}
                contexts.append({"split": split,
                    "source_receipt_sha256": sample["source_receipt_sha256"],
                    "session_tick": sample[role]["session_tick"],
                    "car_model_id": scope["car_model_id"], "setup_sha256": scope["setup_sha256"],
                    "air_temp_c": 20., "track_temp_c": 29., "wind_speed_mps": 1.,
                    "wind_direction_rad": 0., "precipitation_pct": 0.,
                    "track_state": "REVIEWED_DRY"})
            samples.append(sample)
        dataset = {"contract_version": MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION,
            "dataset_id": f"synthetic-{split}", "dataset_version": 1,
            "event_identity": event_identity, "fuel_load_model": fuel,
            "samples": samples, "tire_compound": 0}
        dataset["dataset_sha256"] = digest(dataset)
        request[f"{split}_dataset"] = dataset
    training = request["training_dataset"]
    request["training_model_sha256"] = build_tire_performance_model(training,
        expected_dataset_sha256=training["dataset_sha256"])["model_sha256"]
    request["lap_contexts"] = contexts
    request["request_sha256"] = digest(request)
    return request


@contextmanager
def _case(*, mapped=False):
    now = [1.]
    state = AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    analysis = _LiveAnalysis(state, LiveFuelConfig(reserve_l=.2),
        identifier="synthetic-tire-benefit-only", tick_rate=60, car_count=3,
        generation=state.generation, allowed=lambda: True)
    service = EngineerService(state.snapshot, clock=lambda: now[0], environ={})
    projector = CarMetadataProjector()
    metadata = {"WeekendInfo": {"SeriesID": 501, "SeasonID": 601, "RaceWeek": 12,
        "TrackID": 101, "EventType": "Race", "TrackConfigName": "Invented circuit",
        "BuildVersion": "synthetic-only", "Official": True},
        "DriverInfo": {"DriverCarIdx": 0, "Drivers": [{"CarIdx": 0, "CarID": 123,
                                                       "CarClassID": 27}]},
        "CarSetup": {"Chassis": {"WingSetting": 7}, "UpdateCount": 1}}
    try:
        with TemporaryDirectory(prefix="aeis-synthetic-tire-comparison-") as directory:
            for raw in synthetic_frames(8):
                tick = raw.buffer_tick
                values = {**raw.values, "PlayerCarClass": 27, "TrackWetness": 1,
                    "OnPitRoad": 10 <= tick < 65, "PlayerCarInPitStall": 12 <= tick < 50,
                    "PitstopActive": 12 <= tick < 20, "Speed": 0. if 12 <= tick < 50 else
                    raw.values["Speed"], "SessionLapsRemainEx": 15,
                    "FuelLevel": 3. - .2 * (raw.values["LapCompleted"] + raw.values["LapDistPct"])}
                frame = replace(raw, values=values)
                now[0] = frame.captured_monotonic_s
                selected = projector.project(metadata, frame.session_info_update, frame, "FULL")
                analysis.process((frame, "Race", now[0], 1_200_000, selected))
                if tick == 35:
                    snapshot = state.snapshot()
                    request = _request(selected["event_identity"],
                                       snapshot["car_context"]["identity"])
                    path = Path(directory) / "invented-request.json"
                    with path.open("x", encoding="utf-8") as handle:
                        json.dump(request, handle)
                    calibration = load_tire_calibration(path,
                        expected_request_sha256=request["request_sha256"])
                    state.set_tire_calibration(calibration,
                        expected_binding=car_context_binding(snapshot, parked=True),
                        expected_revision=snapshot["tire_calibration"]["revision"])
                    state.confirm_tire_service("FULL_NEW_SET")
                if mapped and tick == 70:
                    state.configure_strategy(StrategyParameters(4., 2., 20., 24.,
                        pit_entry_fraction=.9, pit_exit_fraction=.1, other_service_low_s=1.,
                        other_service_high_s=2., tire_change_time_s=1.,
                        fuel_tire_service_timing="PARALLEL"))
            if not mapped:
                state.configure_strategy(StrategyParameters(4., 2., 20., 24.,
                    tire_change_time_s=1., fuel_tire_service_timing="PARALLEL"))
            yield state, service, now
    finally:
        service.close(wait=True)
        analysis.close()
        state.connection("STOPPED")


def run_synthetic_tire_comparison():
    passed = False
    try:
        with _case() as (state, service, _):
            value = validated_tire_comparison(state.snapshot())
            if (value is None or value["status"] != "CONDITIONAL"
                    or value["scenarios"][0]["net_gain_range_s"][0] <= 0
                    or value["live_acceptance"] or value["physical_wear"] is not None
                    or service.submit("比较换胎收益")[0] != 202):
                raise ValueError("SYNTHETIC_TIRE_COMPARISON_FAILED")
            answer = service.snapshot()["answer"]
            if (answer["stale"] or "净收益" not in answer["spoken_text"]
                    or "不证明旧胎安全" not in answer["spoken_text"]
                    or service.snapshot()["requests_used"] != 0):
                raise ValueError("SYNTHETIC_TIRE_COMPARISON_QUERY_FAILED")
            state.set_tire_calibration(None)
            answer = service.snapshot()["answer"]
            passed = answer["stale"] and "spoken_text" not in answer
    except Exception:
        pass
    return {"id": "SYNTHETIC_CONDITIONAL_TIRE_COMPARISON", "status": "PASS" if passed else "FAIL"}


def run_synthetic_pit_briefing():
    from .live_pit_briefing import project_pit_briefing

    passed = False
    try:
        with _case(mapped=True) as (state, service, _):
            value = project_pit_briefing(state.snapshot())
            first = value["actions"][0]
            if (value["status"] != "CONDITIONAL" or len(first["services"]) != 2
                    or not all(row["status"] == "READY" for row in first["services"])
                    or first["tires"]["covered_gain_range_s"][0] <= 0
                    or first["tires"]["complete_laps"] < 1
                    or first["tires"]["unmodeled_partial_laps"] <= 0
                    or first["tires"]["net_stint_gain_range_s"] is not None
                    or service.submit("综合进站方案")[0] != 202):
                raise ValueError("SYNTHETIC_PIT_BRIEFING_FAILED")
            answer = service.snapshot()["answer"]
            if (answer["stale"] or "整段收益未知" not in answer["spoken_text"]
                    or "完整圈收益" not in answer["text"]
                    or "仅加油" not in answer["text"] or "加油并换四胎" not in answer["text"]
                    or service.snapshot()["requests_used"] != 0):
                raise ValueError("SYNTHETIC_PIT_BRIEFING_QUERY_FAILED")
            state.set_tire_calibration(None)
            answer = service.snapshot()["answer"]
            passed = answer["stale"] and "spoken_text" not in answer
    except Exception:
        pass
    return {"id": "SYNTHETIC_SAME_ACTION_PIT_BRIEFING", "status": "PASS" if passed else "FAIL"}
