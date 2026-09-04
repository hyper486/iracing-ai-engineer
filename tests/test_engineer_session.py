from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import iracing_ai_engineer.engineer_session as _MODULE
from iracing_ai_engineer.adapters import (
    ValidatedCollectorRun,
    ValidatedIbtRun,
    open_ibt_telemetry,
)
from iracing_ai_engineer.collector import CollectorSample, collect_samples_to_jsonl
from iracing_ai_engineer.fuel import FuelScenario
from iracing_ai_engineer.m2_strategy import canonical_sha256 as m2_sha256
from iracing_ai_engineer.sdk_probe import (
    SDK_TYPE_SIZES,
    RawSdkFrame,
    VariableDescriptor,
)

TRACK_LENGTH_M = 300.0
TICK_RATE_HZ = 60
SESSION_ID = "engineer-session-paired"


@contextmanager
def _open_path_replacement_handle(path: Path) -> Iterator[io.FileIO]:
    """Open a test handle that permits a Windows rename attack explicitly."""

    if os.name != "nt":
        with path.open("r+b", buffering=0) as handle:
            yield handle
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    raw = create_file(
        str(path),
        0x80000000 | 0x40000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00000080 | 0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if raw in (None, invalid):
        raise OSError(ctypes.get_last_error(), "CreateFileW test open failed")
    try:
        descriptor = msvcrt.open_osfhandle(
            int(raw), os.O_RDWR | int(getattr(os, "O_BINARY", 0))
        )
    except Exception:
        close_handle(raw)
        raise
    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        yield handle


def _profile(kind: str) -> dict[str, np.ndarray]:
    distance = np.arange(0.0, TRACK_LENGTH_M + 3.0, 3.0, dtype=np.float64)
    speed = np.full_like(distance, 50.0)
    throttle = np.ones_like(distance)
    brake = np.zeros_like(distance)
    steering = 0.18 * np.exp(-0.5 * ((distance - 132.0) / 35.0) ** 2)
    if kind == "long_coast":
        onset, release, apex, pickup, minimum, recovery = 60, 99, 132, 162, 30.0, 210
    else:
        onset, release, apex, pickup, minimum, recovery = 81, 120, 132, 141, 32.0, 189
    brake[(distance >= onset) & (distance < release)] = 0.72
    throttle[(distance >= onset) & (distance < pickup)] = 0.0
    braking = (distance >= onset) & (distance <= apex)
    acceleration = (distance > apex) & (distance <= recovery)
    speed[braking] = np.linspace(50.0, minimum, int(np.sum(braking)))
    speed[acceleration] = np.linspace(minimum, 49.0, int(np.sum(acceleration)))
    elapsed = np.r_[
        0.0,
        np.cumsum(np.diff(distance) / ((speed[:-1] + speed[1:]) / 2.0)),
    ]
    return {
        "Brake": brake,
        "Distance": distance,
        "Elapsed": elapsed,
        "Speed": speed,
        "SteeringWheelAngle": steering,
        "Throttle": throttle,
    }


def _paired_frames() -> list[dict[str, object]]:
    kinds = ("baseline",) * 7
    frames: list[dict[str, object]] = []
    for lap_number, kind in enumerate(kinds, start=1):
        profile = _profile(kind)
        elapsed = profile["Elapsed"]
        time_grid = np.arange(
            0.0,
            float(elapsed[-1]) + 0.5 / TICK_RATE_HZ,
            1.0 / TICK_RATE_HZ,
        )
        distance = np.interp(time_grid, elapsed, profile["Distance"])
        speed = np.interp(time_grid, elapsed, profile["Speed"])
        throttle = np.interp(time_grid, elapsed, profile["Throttle"])
        brake = np.interp(time_grid, elapsed, profile["Brake"])
        steering = np.interp(time_grid, elapsed, profile["SteeringWheelAngle"])
        for index in range(len(time_grid)):
            sequence = len(frames)
            lap_pct = min(float(distance[index]) / TRACK_LENGTH_M, 1.0)
            opponent_pct = (lap_pct + 0.4) % 1.0
            frames.append(
                {
                    "AirTemp": 20.0,
                    "Brake": float(brake[index]),
                    "CarIdxLap": [lap_number, lap_number],
                    "CarIdxLapCompleted": [lap_number - 1, lap_number - 1],
                    "CarIdxLapDistPct": [lap_pct, opponent_pct],
                    "CarIdxOnPitRoad": [False, False],
                    "CarIdxTrackSurface": [3, 3],
                    "FuelLevel": 60.0 - 4.0 * (lap_number - 1 + lap_pct),
                    "FuelLevelPct": 0.70 - 0.005 * (lap_number - 1 + lap_pct),
                    "IsOnTrack": True,
                    "IsOnTrackCar": True,
                    "Lap": lap_number,
                    "LapCompleted": lap_number - 1,
                    "LapDistPct": lap_pct,
                    "OnPitRoad": False,
                    "PlayerCarDriverIncidentCount": 0,
                    "PlayerCarIdx": 0,
                    "PlayerCarInPitStall": False,
                    "PlayerCarMyIncidentCount": 0,
                    "PlayerCarTeamIncidentCount": 0,
                    "PlayerTireCompound": 0,
                    "PlayerTrackSurface": 3,
                    "PitstopActive": False,
                    "Precipitation": 0.0,
                    "SessionNum": 1,
                    "SessionTick": 10_000 + sequence,
                    "SessionTime": sequence / TICK_RATE_HZ,
                    "Speed": float(speed[index]),
                    "SteeringWheelAngle": float(steering[index]),
                    "Throttle": float(throttle[index]),
                    "TireSetsUsed": 1,
                    "TrackTempCrew": 26.0,
                    "WindDir": 0.0,
                    "WindVel": 1.0,
                }
            )
    return frames


def _descriptor(name: str, value: object, offset: int) -> VariableDescriptor:
    count = len(value) if isinstance(value, list) else 1
    scalar = value[0] if isinstance(value, list) else value
    if isinstance(scalar, bool):
        type_code, dtype = 1, "bool"
    elif isinstance(scalar, int):
        type_code, dtype = 2, "int32"
    else:
        type_code, dtype = 4, "float32"
    return VariableDescriptor(
        name=name,
        type_code=type_code,
        dtype=dtype,
        offset=offset,
        count=count,
        count_as_time=False,
        unit="",
        description=name,
    )


def _descriptors(frame: dict[str, object]) -> tuple[VariableDescriptor, ...]:
    result: list[VariableDescriptor] = []
    offset = 0
    for name, value in frame.items():
        descriptor = _descriptor(name, value, offset)
        result.append(descriptor)
        offset += SDK_TYPE_SIZES[descriptor.type_code] * descriptor.count
    return tuple(result)


class _StubIbtReader:
    instances: list[_StubIbtReader] = []

    def __init__(
        self, frames: list[dict[str, object]], *, source_sha256: str = "a" * 64
    ) -> None:
        self.frames = frames
        self.variable_names = tuple(frames[0])
        self.variables = _descriptors(frames[0])
        self.metadata = SimpleNamespace(
            file_size_bytes=987_654,
            record_count=len(frames),
            tick_rate_hz=TICK_RATE_HZ,
        )
        self.source_sha256 = source_sha256
        self.verified = False
        self.instances.append(self)

    def __enter__(self) -> _StubIbtReader:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get_channels(self, names: tuple[str, ...]) -> dict[str, list[object]]:
        return {name: [frame[name] for frame in self.frames] for name in names}

    def public_session_context(self) -> dict[str, object]:
        return {"track_length": "300 m"}

    def verify_source_unchanged(self) -> None:
        self.verified = True


def _write_collector(path: Path, frames: list[dict[str, object]]) -> None:
    descriptors = _descriptors(frames[0])
    samples = (
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=20_000 + index,
                session_info_update=1,
                values=frame,
                sim_mode_raw="full",
                captured_monotonic_s=float(frame["SessionTime"]),
            ),
            descriptors=descriptors,
            tick_rate_hz=TICK_RATE_HZ,
            session_info={
                "WeekendInfo": {
                    "SimMode": "full",
                    "TrackLength": "300 m",
                    "TrackName": "Engineer Session Synthetic Circuit",
                }
            },
        )
        for index, frame in enumerate(frames)
    )
    collect_samples_to_jsonl(
        samples,
        path,
        source_id="engineer-session-collector",
        session_id=SESSION_ID,
        stale_after_s=1.0,
        fsync_each_record=False,
    )


def _scenario() -> FuelScenario:
    return FuelScenario(
        current_fuel_l=20.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
        reserve_l=1.0,
        minimum_valid_laps=5,
    )


def _context(
    source: _MODULE._SourceComponents,
    *,
    decision_tick: int,
) -> dict[str, object]:
    lineage = source.input_lineage
    material: dict[str, object] = {
        "calibration_model": None,
        "contract_version": "offline-m2-strategy-context-v1",
        "event_identity": {
            "car_class_id": 5,
            "event_type": "Race",
            "official": False,
            "provenance": "CONTRACT_FIXTURE",
            "race_week": 3,
            "season_id": 2,
            "series_id": 1,
            "sim_build": "engineer-session-fixture",
            "track_config": "Synthetic",
            "track_id": 4,
        },
        "horizon": {
            "kind": "LAPS",
            "laps_remaining": 10,
            "leader_eta_to_next_crossing_s": None,
            "player_is_leader": None,
            "provenance": "CONTRACT_FIXTURE",
            "reference_lap_time_s": None,
            "time_remaining_s": None,
        },
        "observation": {
            "decision_tick": decision_tick,
            "laps_completed": 7,
            "penalty_state": None,
            "pits_open": True,
            "reset": False,
            "schema_changed": False,
            "session_epoch": 1,
            "source_epoch": 1,
            "stale": False,
        },
        "source_binding": {
            "event_receipt_sha256": lineage["event_receipt_sha256"],
            "normalized_samples_sha256": lineage["normalized_samples_sha256"],
            "sample_count": lineage["sample_count"],
            "session_id": lineage["session_id"],
            "source_id": lineage["source_id"],
            "source_kind": lineage["source_kind"],
            "source_sha256": lineage["source_content_sha256"],
        },
        "strategy_policy": {
            "conservative_quantile": 0.9,
            "reserve_l": 1.0,
            "selection_policy": "LATEST_COMMON_FUEL_FEASIBLE",
        },
        "traffic_rejoin": None,
        "vehicle_context": {
            "provenance": "CONTRACT_FIXTURE",
            "tank_capacity_l": 120.0,
        },
    }
    return {**material, "context_sha256": m2_sha256(material)}


def _fully_rehash_m1_attack(
    session: dict[str, object], *, attack: str
) -> dict[str, object]:
    """Model an attacker who updates every receipt hash above a forged M1."""

    tampered = copy.deepcopy(session)
    components = tampered["components"]
    m1 = components["m1_pit_stint"]
    old_m1_sha = m1["pit_stint_receipt_sha256"]
    if attack == "input_binding_extra":
        m1["input_binding"]["attacker"] = True
        m1["input_binding"]["input_lineage_sha256"] = _MODULE.canonical_sha256(
            {
                key: value
                for key, value in m1["input_binding"].items()
                if key != "input_lineage_sha256"
            }
        )
    elif attack == "normalization_profile_change":
        m1["input_binding"]["normalization_profile"]["stale_after_us"] += 1
        m1["input_binding"]["input_lineage_sha256"] = _MODULE.canonical_sha256(
            {
                key: value
                for key, value in m1["input_binding"].items()
                if key != "input_lineage_sha256"
            }
        )
    elif attack == "human_validation_pass":
        m1["capabilities"]["human_validation"] = {
            "reasons": [],
            "status": "PASS_DATA",
        }
    else:  # pragma: no cover - test helper contract
        raise AssertionError(attack)
    m1["pit_stint_receipt_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in m1.items()
            if key != "pit_stint_receipt_sha256"
        }
    )
    new_m1_sha = m1["pit_stint_receipt_sha256"]
    assert new_m1_sha != old_m1_sha

    m2 = components["m2_strategy"]
    old_m2_sha = m2["m2_strategy_receipt_sha256"]
    m2["input_binding"]["m1_receipt_sha256"] = new_m1_sha
    m2["input_binding"]["input_lineage_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in m2["input_binding"].items()
            if key != "input_lineage_sha256"
        }
    )
    m2["m2_strategy_receipt_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in m2.items()
            if key != "m2_strategy_receipt_sha256"
        }
    )
    new_m2_sha = m2["m2_strategy_receipt_sha256"]
    assert new_m2_sha != old_m2_sha

    timeline = components["advisor_timeline"]
    old_timeline_sha = timeline["advisor_timeline_sha256"]
    timeline["input_binding"]["m2_receipt_sha256"] = [new_m2_sha]
    clock = timeline["clock_receipt"]
    for binding in clock["bindings"]:
        assert binding["m2_receipt_sha256"] == old_m2_sha
        binding["m2_receipt_sha256"] = new_m2_sha
    clock["clock_receipt_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in clock.items()
            if key != "clock_receipt_sha256"
        }
    )
    timeline["advisor_timeline_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in timeline.items()
            if key != "advisor_timeline_sha256"
        }
    )
    assert timeline["advisor_timeline_sha256"] != old_timeline_sha

    tampered["component_hashes"] = _MODULE._component_hashes(components)
    tampered["admission_receipt"] = _MODULE._admission_receipt(
        components, tampered["input_lineage"]
    )
    old_projection = {
        key: value
        for key, value in m1.items()
        if key
        not in {
            "input_binding",
            "input_evidence",
            "normalized_input_receipt",
            "pit_stint_receipt_sha256",
            "upstream_event_receipt",
        }
    }
    semantics = tampered["semantic_hashes"]
    semantics["m1_pit_stint_semantic_sha256"] = _MODULE.canonical_sha256(
        old_projection
    )
    semantics["source_neutral_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in semantics.items()
            if key != "source_neutral_sha256"
        }
    )
    tampered["engineer_session_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in tampered.items()
            if key != "engineer_session_sha256"
        }
    )
    return tampered


@contextmanager
def _synthetic_open_input(
    original,
    frames: list[dict[str, object]],
    input_path: str | Path,
    *,
    input_kind: str,
    source_id: str | None,
    session_id: str | None,
    stale_after_s: float,
    opponent_error_policy: str,
) -> Iterator[ValidatedIbtRun | ValidatedCollectorRun]:
    if input_kind == "ibt":
        assert source_id is not None and session_id is not None
        with open_ibt_telemetry(
            input_path,
            source_id=source_id,
            session_id=session_id,
            stale_after_s=stale_after_s,
            opponent_error_policy=opponent_error_policy,
            reader_factory=lambda _: _StubIbtReader(frames),
        ) as run:
            yield run
    else:
        with original(
            input_path,
            input_kind=input_kind,
            source_id=source_id,
            session_id=session_id,
            stale_after_s=stale_after_s,
            opponent_error_policy=opponent_error_policy,
        ) as run:
            yield run


@pytest.fixture(scope="module")
def paired_frames() -> list[dict[str, object]]:
    return _paired_frames()


def _assert_component_tampering_is_rejected(
    session: dict[str, object],
) -> None:
    safety_tamper = copy.deepcopy(session)
    safety_tamper["safety"]["network_accessed"] = True
    safety_tamper["engineer_session_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in safety_tamper.items()
            if key != "engineer_session_sha256"
        }
    )
    with pytest.raises(_MODULE.EngineerSessionError) as unsafe:
        _MODULE.validate_engineer_session(safety_tamper)
    assert unsafe.value.code == "SAFETY_BOUNDARY_INVALID"

    m2_tamper = copy.deepcopy(session)
    m2 = m2_tamper["components"]["m2_strategy"]
    m2["event_identity"]["attacker"] = True
    m2["m2_strategy_receipt_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in m2.items()
            if key != "m2_strategy_receipt_sha256"
        }
    )
    m2_tamper["component_hashes"]["m2_strategy"] = m2[
        "m2_strategy_receipt_sha256"
    ]
    m2_tamper["engineer_session_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in m2_tamper.items()
            if key != "engineer_session_sha256"
        }
    )
    with pytest.raises(_MODULE.EngineerSessionError) as m2_error:
        _MODULE.validate_engineer_session(m2_tamper)
    assert m2_error.value.code == "COMPONENT_REPLAY_MISMATCH"

    m3_tamper = copy.deepcopy(session)
    diagnosis = m3_tamper["components"]["driving_diagnosis"]
    diagnosis["policy"]["attacker"] = True
    diagnosis["diagnosis_evidence_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in diagnosis.items()
            if key != "diagnosis_evidence_sha256"
        }
    )
    m3_tamper["component_hashes"]["driving_diagnosis"] = diagnosis[
        "diagnosis_evidence_sha256"
    ]
    m3_tamper["engineer_session_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in m3_tamper.items()
            if key != "engineer_session_sha256"
        }
    )
    with pytest.raises(_MODULE.EngineerSessionError) as m3_error:
        _MODULE.validate_engineer_session(m3_tamper)
    assert m3_error.value.code == "COMPONENT_REPLAY_MISMATCH"

    clock_tamper = copy.deepcopy(session)
    timeline = clock_tamper["components"]["advisor_timeline"]
    clock = timeline["clock_receipt"]
    clock["bindings"][0]["session_time_us"] += 1
    clock["clock_receipt_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in clock.items()
            if key != "clock_receipt_sha256"
        }
    )
    timeline["advisor_timeline_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in timeline.items()
            if key != "advisor_timeline_sha256"
        }
    )
    clock_tamper["component_hashes"]["advisor_timeline"] = timeline[
        "advisor_timeline_sha256"
    ]
    admission = clock_tamper["admission_receipt"]
    admission["advisor_clock_receipt_sha256"] = clock["clock_receipt_sha256"]
    admission["passes"][3]["component_sha256"] = timeline[
        "advisor_timeline_sha256"
    ]
    admission["admission_receipt_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in admission.items()
            if key != "admission_receipt_sha256"
        }
    )
    clock_tamper["engineer_session_sha256"] = _MODULE.canonical_sha256(
        {
            key: value
            for key, value in clock_tamper.items()
            if key != "engineer_session_sha256"
        }
    )
    with pytest.raises(_MODULE.EngineerSessionError) as clock_error:
        _MODULE.validate_engineer_session(clock_tamper)
    assert clock_error.value.code == "ADVISOR_TIMELINE_INVALID"


def test_paired_ibt_and_collector_core_share_semantics_and_split_provenance(
    tmp_path: Path,
    monkeypatch,
    paired_frames,
):
    collector_path = tmp_path / "paired.jsonl"
    _write_collector(collector_path, paired_frames)
    original = _MODULE._open_input

    @contextmanager
    def open_input(*args, **kwargs):
        with _synthetic_open_input(original, paired_frames, *args, **kwargs) as run:
            yield run

    monkeypatch.setattr(_MODULE, "_open_input", open_input)
    _StubIbtReader.instances.clear()
    ibt = _MODULE._build_source_components(
        tmp_path / "fixture.ibt",
        input_kind="ibt",
        source_id="engineer-session-ibt",
        session_id=SESSION_ID,
        scenario=_scenario(),
        stale_after_s=1.0,
        opponent_error_policy="degrade",
    )
    collector = _MODULE._build_source_components(
        collector_path,
        input_kind="collector",
        source_id=None,
        session_id=None,
        scenario=_scenario(),
        stale_after_s=1.0,
        opponent_error_policy="degrade",
    )

    assert len(_StubIbtReader.instances) == 3
    assert all(instance.verified for instance in _StubIbtReader.instances)
    assert ibt.fuel_replay["model_semantic_sha256"] == collector.fuel_replay[
        "model_semantic_sha256"
    ]
    assert ibt.driving_replay["model_semantic_sha256"] == collector.driving_replay[
        "model_semantic_sha256"
    ]
    assert ibt.input_lineage["input_lineage_sha256"] != collector.input_lineage[
        "input_lineage_sha256"
    ]
    assert ibt.input_lineage["source_kind"] == "IBT_OFFLINE"
    assert collector.input_lineage["source_kind"] == "SDK_LIVE"

    decision_tick = int(paired_frames[-1]["SessionTick"])
    _StubIbtReader.instances.clear()
    ibt_session = _MODULE.build_engineer_session(
        tmp_path / "fixture.ibt",
        input_kind="ibt",
        source_id="engineer-session-ibt",
        session_id=SESSION_ID,
        scenario=_scenario(),
        strategy_context=_context(ibt, decision_tick=decision_tick),
        stale_after_s=1.0,
    )
    assert len(_StubIbtReader.instances) == 4
    assert all(instance.verified for instance in _StubIbtReader.instances)
    collector_session = _MODULE.build_engineer_session(
        collector_path,
        input_kind="collector",
        scenario=_scenario(),
        strategy_context=_context(collector, decision_tick=decision_tick),
        stale_after_s=1.0,
    )

    assert ibt_session["semantic_hashes"] == collector_session["semantic_hashes"]
    assert ibt_session["input_lineage"] != collector_session["input_lineage"]
    assert ibt_session["status"] == collector_session["status"] == "WAIT_DATA"
    ibt_m1 = ibt_session["components"]["m1_pit_stint"]
    collector_m1 = collector_session["components"]["m1_pit_stint"]
    ibt_m1_semantics = _MODULE._m1_semantic_projection(ibt_m1)
    collector_m1_semantics = _MODULE._m1_semantic_projection(collector_m1)
    assert ibt_m1_semantics == collector_m1_semantics
    assert ibt_m1_semantics["input_binding"] == {
        "normalization_profile": {
            "opponent_error_policy": "degrade",
            "profile_version": "normalized-sdk-adapter-v3",
            "stale_after_us": 1_000_000,
        }
    }
    assert ibt_m1_semantics["input_evidence"] == {
        "completion_status": "COMPLETE",
        "sample_count": len(paired_frames),
        "tick_rate_hz": TICK_RATE_HZ,
    }
    assert set(ibt_m1_semantics["normalized_input_receipt"]) == {
        "contract_version",
        "sample_count",
    }
    assert set(ibt_m1_semantics["upstream_event_receipt"]) == {
        "accepted_sample_count",
        "config_sha256",
        "contract_version",
        "event_count",
        "event_kind_counts",
        "rejected_sample_count",
        "sample_count",
        "session_epoch_count",
        "source_epoch_count",
    }
    assert ibt_m1 != collector_m1
    assert ibt_m1["pit_stint_receipt_sha256"] != collector_m1[
        "pit_stint_receipt_sha256"
    ]
    assert ibt_session["semantic_hashes"]["m1_pit_stint_semantic_sha256"] == (
        collector_session["semantic_hashes"]["m1_pit_stint_semantic_sha256"]
    )
    for session in (ibt_session, collector_session):
        components = session["components"]
        assert session["admission_receipt"]["fresh_admission_count"] == 4
        assert [
            item["consumer"] for item in session["admission_receipt"]["passes"]
        ] == [
            "fuel_model_replay",
            "driving_model_replay",
            "m1_pit_stint",
            "advisor_timeline_clock",
        ]
        assert components["m2_strategy"]["recommendations"] == []
        assert components["driving_diagnosis"]["recommendations"] == []
        assert components["advisor_timeline"]["summary"][
            "tactical_observation_count"
        ] == 0
        assert components["advisor_timeline"]["speech_policy_run"][
            "decisions"
        ] == []
        assert _MODULE.validate_engineer_session(
            session,
            expected_engineer_session_sha256=session[
                "engineer_session_sha256"
            ],
        ) == session
        assert "DriverInfo" not in _MODULE._canonical_json(session).decode("utf-8")

    output = tmp_path / "engineer-session.json"
    _MODULE.write_engineer_session_exclusive(output, ibt_session)
    assert output.read_bytes() == _MODULE._persisted_json(ibt_session)
    output_metadata = output.stat()
    assert stat.S_ISREG(output_metadata.st_mode)
    if os.name == "nt":
        assert not (int(getattr(output_metadata, "st_file_attributes", 0)) & 0x400)
    else:
        assert output_metadata.st_mode & 0o777 == 0o600
    with pytest.raises(_MODULE.EngineerSessionError) as exists:
        _MODULE.write_engineer_session_exclusive(output, ibt_session)
    assert exists.value.code == "OUTPUT_CREATE_FAILED"

    attack_codes = {
        "human_validation_pass": "M1_RECEIPT_INVALID",
        "input_binding_extra": "M1_RECEIPT_INVALID",
        "normalization_profile_change": "INPUT_LINEAGE_MISMATCH",
    }
    for attack, expected_code in attack_codes.items():
        fully_rehashed = _fully_rehash_m1_attack(ibt_session, attack=attack)
        with pytest.raises(_MODULE.EngineerSessionError) as m1_error:
            _MODULE.validate_engineer_session(
                fully_rehashed,
                expected_engineer_session_sha256=fully_rehashed[
                    "engineer_session_sha256"
                ],
            )
        assert m1_error.value.code == expected_code

    if os.name == "nt":
        windows_output = tmp_path / "windows-create-new-lock.json"
        displaced_windows_output = tmp_path / "windows-create-new-lock.displaced"
        original_fsync = os.fsync
        replacement_was_blocked = False
        replacement_was_observed = False

        def attempt_windows_replacement(descriptor: int) -> None:
            nonlocal replacement_was_blocked, replacement_was_observed
            original_fsync(descriptor)
            try:
                windows_output.rename(displaced_windows_output)
            except OSError:
                replacement_was_blocked = True
            else:
                replacement_was_observed = True
                windows_output.write_bytes(b"attacker replacement\n")

        with monkeypatch.context() as patcher:
            patcher.setattr(os, "fsync", attempt_windows_replacement)
            if replacement_was_observed:
                raise AssertionError("replacement state cannot precede the writer")
            try:
                _MODULE.write_engineer_session_exclusive(windows_output, ibt_session)
            except _MODULE.EngineerSessionError as changed:
                assert replacement_was_observed
                assert changed.code == "OUTPUT_PATH_CHANGED"
            else:
                assert replacement_was_blocked
                assert windows_output.read_bytes() == _MODULE._persisted_json(
                    ibt_session
                )
        _assert_component_tampering_is_rejected(ibt_session)
        return

    replacement = b"attacker replacement\n"
    original_fsync = os.fsync

    def unlink_must_not_run(*_: object, **__: object) -> None:
        raise AssertionError("failure cleanup must never unlink")

    name_swap_output = tmp_path / "name-swap-success.json"
    displaced_name_output = tmp_path / "name-swap-success.displaced"
    name_swap_calls = 0

    def swap_name_after_file_fsync(descriptor: int) -> None:
        nonlocal name_swap_calls
        original_fsync(descriptor)
        name_swap_calls += 1
        if name_swap_calls == 1:
            name_swap_output.rename(displaced_name_output)
            name_swap_output.write_bytes(replacement)

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "fsync", swap_name_after_file_fsync)
        with pytest.raises(_MODULE.EngineerSessionError) as changed:
            _MODULE.write_engineer_session_exclusive(name_swap_output, ibt_session)
    assert changed.value.code == "OUTPUT_PATH_CHANGED"
    assert name_swap_output.read_bytes() == replacement
    assert displaced_name_output.read_bytes() == _MODULE._persisted_json(ibt_session)

    parent_swap = tmp_path / "parent-swap-success"
    parent_swap.mkdir()
    displaced_parent = tmp_path / "parent-swap-success.displaced"
    parent_swap_output = parent_swap / "session.json"
    parent_swap_calls = 0

    def swap_parent_after_directory_fsync(descriptor: int) -> None:
        nonlocal parent_swap_calls
        original_fsync(descriptor)
        parent_swap_calls += 1
        if parent_swap_calls == 2:
            parent_swap.rename(displaced_parent)
            parent_swap.mkdir()
            parent_swap_output.write_bytes(replacement)

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "fsync", swap_parent_after_directory_fsync)
        with pytest.raises(_MODULE.EngineerSessionError) as changed:
            _MODULE.write_engineer_session_exclusive(parent_swap_output, ibt_session)
    assert changed.value.code == "OUTPUT_PATH_CHANGED"
    assert parent_swap_output.read_bytes() == replacement
    assert (displaced_parent / "session.json").read_bytes() == (
        _MODULE._persisted_json(ibt_session)
    )

    name_failure_output = tmp_path / "name-swap-failure.json"
    displaced_failure_output = tmp_path / "name-swap-failure.displaced"

    def swap_name_then_fail(_: int) -> None:
        name_failure_output.rename(displaced_failure_output)
        name_failure_output.write_bytes(replacement)
        raise OSError("injected file fsync failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "fsync", swap_name_then_fail)
        patcher.setattr(os, "unlink", unlink_must_not_run)
        with pytest.raises(OSError, match="injected file fsync failure"):
            _MODULE.write_engineer_session_exclusive(
                name_failure_output, ibt_session
            )
    assert name_failure_output.read_bytes() == replacement
    assert displaced_failure_output.is_file()

    failure_parent = tmp_path / "parent-swap-failure"
    failure_parent.mkdir()
    displaced_failure_parent = tmp_path / "parent-swap-failure.displaced"
    failure_parent_output = failure_parent / "session.json"
    parent_failure_calls = 0

    def swap_parent_then_fail(descriptor: int) -> None:
        nonlocal parent_failure_calls
        parent_failure_calls += 1
        if parent_failure_calls == 1:
            original_fsync(descriptor)
            return
        failure_parent.rename(displaced_failure_parent)
        failure_parent.mkdir()
        failure_parent_output.write_bytes(replacement)
        raise OSError("injected directory fsync failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "fsync", swap_parent_then_fail)
        with pytest.raises(OSError, match="injected directory fsync failure"):
            _MODULE.write_engineer_session_exclusive(
                failure_parent_output, ibt_session
            )
    assert failure_parent_output.read_bytes() == replacement
    assert (displaced_failure_parent / "session.json").is_file()

    residual_output = tmp_path / "failed-write-residue.json"

    def fail_without_replacement(_: int) -> None:
        raise OSError("injected residue fsync failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "fsync", fail_without_replacement)
        patcher.setattr(os, "unlink", unlink_must_not_run)
        with pytest.raises(OSError, match="injected residue fsync failure"):
            _MODULE.write_engineer_session_exclusive(residual_output, ibt_session)
    assert residual_output.read_bytes() == _MODULE._persisted_json(ibt_session)

    same_inode_output = tmp_path / "same-inode-content-swap.json"
    expected_payload = _MODULE._persisted_json(ibt_session)
    same_size_replacement = b"X" + expected_payload[1:]
    same_inode_calls = 0

    def modify_same_inode_after_directory_fsync(descriptor: int) -> None:
        nonlocal same_inode_calls
        original_fsync(descriptor)
        same_inode_calls += 1
        if same_inode_calls == 2:
            with same_inode_output.open("r+b") as replacement_handle:
                replacement_handle.write(same_size_replacement)

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "fsync", modify_same_inode_after_directory_fsync)
        with pytest.raises(_MODULE.EngineerSessionError) as content_changed:
            _MODULE.write_engineer_session_exclusive(same_inode_output, ibt_session)
    assert content_changed.value.code == "OUTPUT_CONTENT_CHANGED"
    assert same_inode_output.read_bytes() == same_size_replacement

    original_close = os.close
    close_name_output = tmp_path / "close-hook-name-swap.json"
    displaced_close_name = tmp_path / "close-hook-name-swap.displaced"
    close_name_calls = 0

    def swap_name_when_writer_closes(descriptor: int) -> None:
        nonlocal close_name_calls
        original_close(descriptor)
        close_name_calls += 1
        if close_name_calls == 2:
            close_name_output.rename(displaced_close_name)
            close_name_output.write_bytes(replacement)

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "close", swap_name_when_writer_closes)
        with pytest.raises(_MODULE.EngineerSessionError) as close_changed:
            _MODULE.write_engineer_session_exclusive(
                close_name_output, ibt_session
            )
    assert close_changed.value.code == "OUTPUT_PATH_CHANGED"
    assert close_name_output.read_bytes() == replacement
    assert displaced_close_name.read_bytes() == _MODULE._persisted_json(
        ibt_session
    )

    close_parent = tmp_path / "close-hook-parent-swap"
    close_parent.mkdir()
    displaced_close_parent = tmp_path / "close-hook-parent-swap.displaced"
    close_parent_output = close_parent / "session.json"
    close_parent_calls = 0

    def swap_parent_when_writer_closes(descriptor: int) -> None:
        nonlocal close_parent_calls
        original_close(descriptor)
        close_parent_calls += 1
        if close_parent_calls == 3:
            close_parent.rename(displaced_close_parent)
            close_parent.mkdir()
            close_parent_output.write_bytes(replacement)

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "close", swap_parent_when_writer_closes)
        with pytest.raises(_MODULE.EngineerSessionError) as close_changed:
            _MODULE.write_engineer_session_exclusive(
                close_parent_output, ibt_session
            )
    assert close_changed.value.code == "OUTPUT_PATH_CHANGED"
    assert close_parent_output.read_bytes() == replacement
    assert (displaced_close_parent / "session.json").read_bytes() == (
        _MODULE._persisted_json(ibt_session)
    )

    _assert_component_tampering_is_rejected(ibt_session)


def test_collector_snapshot_builder_uses_four_dups_and_survives_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paired_frames: list[dict[str, object]],
) -> None:
    capture = tmp_path / "same-handle.jsonl"
    displaced = tmp_path / "same-handle.displaced.jsonl"
    _write_collector(capture, paired_frames)
    decision_tick = int(paired_frames[-1]["SessionTick"])
    duplicate_calls = 0
    original_dup = os.dup

    def counted_dup(descriptor: int) -> int:
        nonlocal duplicate_calls
        duplicate_calls += 1
        return original_dup(descriptor)

    def context_builder(lineage: Mapping[str, object]) -> Mapping[str, object]:
        source = SimpleNamespace(input_lineage=dict(lineage))
        return _context(source, decision_tick=decision_tick)

    with _open_path_replacement_handle(capture) as snapshot:
        capture.rename(displaced)
        capture.write_bytes(b'{"attacker":"path replacement"}\n')
        monkeypatch.setattr(os, "dup", counted_dup)
        monkeypatch.setattr(
            _MODULE,
            "open_collector_jsonl",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("snapshot builder must never reopen a pathname")
            ),
        )
        receipt = _MODULE.build_engineer_session_from_collector_snapshot(
            snapshot,
            scenario=_scenario(),
            strategy_context_builder=context_builder,
            stale_after_s=1.0,
        )
        assert snapshot.closed is False

    assert duplicate_calls == 4
    assert receipt["input_lineage"]["source_content_sha256"] != hashlib.sha256(
        capture.read_bytes()
    ).hexdigest()
    assert receipt["admission_receipt"]["fresh_admission_count"] == 4
    assert receipt["components"]["m2_strategy"]["recommendations"] == []
    assert _MODULE.validate_engineer_session(receipt) == receipt


def test_collector_snapshot_final_full_hash_rejects_same_length_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paired_frames: list[dict[str, object]],
) -> None:
    capture = tmp_path / "same-length-rewrite.jsonl"
    _write_collector(capture, paired_frames)
    decision_tick = int(paired_frames[-1]["SessionTick"])

    def context_builder(lineage: Mapping[str, object]) -> Mapping[str, object]:
        source = SimpleNamespace(input_lineage=dict(lineage))
        return _context(source, decision_tick=decision_tick)

    original_builder = _MODULE._build_engineer_session_from_opener
    original_identity = _MODULE._descriptor_identity
    with capture.open("r+b", buffering=0) as snapshot:
        frozen_identity = original_identity(os.fstat(snapshot.fileno()))

        def identity_with_frozen_times(
            metadata: os.stat_result,
        ) -> tuple[int, int, int, int, int, int]:
            current = original_identity(metadata)
            return (*current[:4], frozen_identity[4], frozen_identity[5])

        def build_then_rewrite(*args: object, **kwargs: object) -> dict[str, object]:
            receipt = original_builder(*args, **kwargs)
            snapshot.seek(0)
            first = snapshot.read(1)
            assert first
            snapshot.seek(0)
            snapshot.write(b"[" if first != b"[" else b"{")
            snapshot.flush()
            os.fsync(snapshot.fileno())
            assert snapshot.seek(0, os.SEEK_END) == frozen_identity[3]
            return receipt

        monkeypatch.setattr(_MODULE, "_descriptor_identity", identity_with_frozen_times)
        monkeypatch.setattr(
            _MODULE, "_build_engineer_session_from_opener", build_then_rewrite
        )
        with pytest.raises(_MODULE.EngineerSessionError) as raised:
            _MODULE.build_engineer_session_from_collector_snapshot(
                snapshot,
                scenario=_scenario(),
                strategy_context_builder=context_builder,
                stale_after_s=1.0,
            )
        assert snapshot.closed is False
    assert raised.value.code == "COLLECTOR_SNAPSHOT_CHANGED"


def test_collector_session_is_cross_hashseed_deterministic(
    tmp_path: Path,
    paired_frames,
):
    collector_path = tmp_path / "hashseed.jsonl"
    _write_collector(collector_path, paired_frames)
    source = _MODULE._build_source_components(
        collector_path,
        input_kind="collector",
        source_id=None,
        session_id=None,
        scenario=_scenario(),
        stale_after_s=1.0,
        opponent_error_policy="degrade",
    )
    context_path = tmp_path / "strategy-context.json"
    context_path.write_text(
        json.dumps(
            _context(source, decision_tick=int(paired_frames[-1]["SessionTick"])),
            allow_nan=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    code = f"""
import hashlib
import json
from pathlib import Path
import iracing_ai_engineer.engineer_session as module
from iracing_ai_engineer.fuel import FuelScenario
receipt = module.build_engineer_session(
    Path({str(collector_path)!r}),
    input_kind="collector",
    scenario=FuelScenario(
        current_fuel_l=20.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
        reserve_l=1.0,
        minimum_valid_laps=5,
    ),
    strategy_context=json.loads(Path({str(context_path)!r}).read_text()),
    stale_after_s=1.0,
)
print(json.dumps({{
    "artifact_sha256": hashlib.sha256(module._persisted_json(receipt)).hexdigest(),
    "self_sha256": receipt["engineer_session_sha256"],
}}, sort_keys=True))
"""
    outputs = []
    for seed in ("1", "987654"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        environment["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path.cwd(),
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]


def test_pinned_hatchling_wheel_contains_and_isolates_engineer_session(
    tmp_path: Path,
):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the locked Hatchling build check")
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert project["build-system"] == {
        "build-backend": "hatchling.build",
        "requires": ["hatchling==1.31.0"],
    }

    output_dir = tmp_path / "dist"
    environment = os.environ.copy()
    environment["UV_NO_PROGRESS"] = "1"
    completed = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(output_dir)],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheels = sorted(output_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        assert "iracing_ai_engineer/engineer_session.py" in archive.namelist()
        assert "iracing_ai_engineer/pit_stint.py" in archive.namelist()

    import_environment = os.environ.copy()
    import_environment.pop("PYTHONPATH", None)
    imported = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                f"import sys; sys.path.insert(0, {str(wheels[0])!r}); "
                "import iracing_ai_engineer.engineer_session as module; "
                "import iracing_ai_engineer.pit_stint as pit_stint; "
                "print(module.ENGINEER_SESSION_CONTRACT_VERSION); "
                "print(module.build_engineer_session.__module__); "
                "print(pit_stint.validate_pit_stint_receipt.__module__); "
                "print(module.__file__)"
            ),
        ],
        cwd=tmp_path,
        env=import_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    imported_lines = imported.stdout.splitlines()
    assert imported_lines[:3] == [
        "engineer-session-v1",
        "iracing_ai_engineer.engineer_session",
        "iracing_ai_engineer.pit_stint",
    ]
    assert len(imported_lines) == 4
    assert Path(imported_lines[3]) == wheels[0] / "iracing_ai_engineer" / "engineer_session.py"


@pytest.mark.skipif(
    not Path("data/raw/audir8lmsevo2gt3_spa up.ibt").is_file(),
    reason="REQUIRES_DATA: public Audi/Spa IBT absent",
)
def test_real_audi_core_reproduces_every_frozen_component_hash():
    m2_frozen = json.loads(
        Path("data/derived/audi-spa-offline-m2-strategy-v1.json").read_text(
            encoding="utf-8"
        )
    )
    session = _MODULE.build_engineer_session(
        Path("data/raw/audir8lmsevo2gt3_spa up.ibt"),
        input_kind="ibt",
        source_id="public-audi-r8-evo2-spa",
        session_id="public-fixture-2023-12-race",
        scenario=_scenario(),
        strategy_context=m2_frozen["strategy_context"],
        stale_after_s=0.5,
    )
    components = session["components"]

    assert components["fuel_replay"]["fuel_replay_sha256"] == (
        "1f3b642c43dd6b7cd16e433dee3f26335f9aecd0c83950e02b706a2f79c3a65c"
    )
    assert components["driving_replay"]["driving_replay_sha256"] == (
        "c5a8f19f156c57c3951e112df24ad3e3f07956961b78c68fe972a534955ebb82"
    )
    assert components["m1_pit_stint"]["pit_stint_receipt_sha256"] == (
        "76a7cec5cf255cd1d7f8fb9e46847b3cae515c8ad3c14acccfffdb0280b906d9"
    )
    assert components["m2_strategy"] == m2_frozen
    assert components["corner_cards"] == json.loads(
        Path("data/derived/audi-spa-corner-cards-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert components["driving_diagnosis"] == json.loads(
        Path("data/derived/audi-spa-driving-diagnosis-evidence-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert _MODULE._persisted_json(components["driving_replay"]) == Path(
        "data/derived/audi-spa-driving-replay-v1.json"
    ).read_bytes()
    for component, artifact in (
        ("m1_pit_stint", "data/derived/audi-spa-offline-pit-stint-v1.json"),
        ("m2_strategy", "data/derived/audi-spa-offline-m2-strategy-v1.json"),
        ("corner_cards", "data/derived/audi-spa-corner-cards-v1.json"),
        (
            "driving_diagnosis",
            "data/derived/audi-spa-driving-diagnosis-evidence-v1.json",
        ),
    ):
        assert _MODULE._canonical_json(
            components[component], newline=True
        ) == Path(artifact).read_bytes()
    timeline = components["advisor_timeline"]
    assert session["status"] == timeline["status"] == "WAIT_DATA"
    assert timeline["advisor_timeline_sha256"] == (
        "e85bd0bf2574f0b5614078369eb10572658dc62d8e030217a1ea3a56f4f649a9"
    )
    assert timeline["clock_receipt"]["clock_receipt_sha256"] == (
        "f490e0096080c36232d9a10bc62102a768ad491278be306aa584358c081e0cf5"
    )
    assert timeline["summary"]["tactical_observation_count"] == 0
    assert timeline["speech_policy_run"]["decisions"] == []
    assert timeline["clock_receipt"]["bindings"] == [
        {
            "decision_tick": 332_490,
            "m2_receipt_sha256": (
                "72e5265ca6aea84c8d747640bf2cd0a99a2a6430817ccd0163121f4a8a973fb4"
            ),
            "session_time_us": 2_554_216_667,
        }
    ]
