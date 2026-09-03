from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from iracing_ai_engineer.adapters import (
    ValidatedCollectorRun,
    ValidatedIbtRun,
    open_collector_jsonl,
    open_ibt_telemetry,
)
from iracing_ai_engineer.collector import CollectorSample, collect_samples_to_jsonl
from iracing_ai_engineer.condition_cohort import (
    HUMAN_TRACK_STATE_ATTESTATION,
    ApprovedTrackStateLabelSet,
    ConditionCohortConfig,
    build_condition_cohort,
)
from iracing_ai_engineer.driving import DrivingAnalysisConfig
from iracing_ai_engineer.driving_model_replay import build_driving_model_replay
from iracing_ai_engineer.events import process_telemetry_events
from iracing_ai_engineer.fuel import FuelScenario
from iracing_ai_engineer.model_replay import build_fuel_model_replay
from iracing_ai_engineer.pit_stint import (
    PitStintReceiptError,
    build_pit_stint_receipt,
)
from iracing_ai_engineer.sdk_probe import (
    SDK_TYPE_SIZES,
    RawSdkFrame,
    VariableDescriptor,
)

TRACK_LENGTH_M = 300.0
TICK_RATE_HZ = 60
SESSION_ID = "paired-adapter-session"


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _corner_profile(
    distance: np.ndarray,
    speed: np.ndarray,
    throttle: np.ndarray,
    brake: np.ndarray,
    *,
    brake_onset: int,
    brake_release: int,
    apex: int,
    throttle_pickup: int,
    minimum_speed: float,
    recovery_end: int,
) -> None:
    brake[(distance >= brake_onset) & (distance < brake_release)] = 0.72
    throttle[(distance >= brake_onset) & (distance < throttle_pickup)] = 0.0
    braking = (distance >= brake_onset) & (distance <= apex)
    acceleration = (distance > apex) & (distance <= recovery_end)
    speed[braking] = np.linspace(50.0, minimum_speed, int(np.sum(braking)))
    speed[acceleration] = np.linspace(
        minimum_speed,
        49.0,
        int(np.sum(acceleration)),
    )


def _lap_profile(kind: str) -> dict[str, np.ndarray]:
    distance = np.arange(0.0, TRACK_LENGTH_M + 3.0, 3.0, dtype=np.float64)
    speed = np.full_like(distance, 50.0)
    throttle = np.ones_like(distance)
    brake = np.zeros_like(distance)
    steering = np.zeros_like(distance)
    corners = [[81, 120, 132, 141, 32.0, 189]]
    if kind == "long_coast":
        corners[0] = [60, 99, 132, 162, 30.0, 210]
    for onset, release, apex, pickup, minimum, recovery in corners:
        _corner_profile(
            distance,
            speed,
            throttle,
            brake,
            brake_onset=int(onset),
            brake_release=int(release),
            apex=int(apex),
            throttle_pickup=int(pickup),
            minimum_speed=float(minimum),
            recovery_end=int(recovery),
        )
    for apex in (132,):
        steering += 0.18 * np.exp(-0.5 * ((distance - apex) / 35.0) ** 2)
    elapsed = np.r_[
        0.0,
        np.cumsum(np.diff(distance) / ((speed[:-1] + speed[1:]) / 2.0)),
    ]
    return {
        "Brake": brake,
        "LapDistPct": distance / TRACK_LENGTH_M,
        "SessionTime": elapsed,
        "Speed": speed,
        "SteeringWheelAngle": steering,
        "Throttle": throttle,
    }


def _paired_frames() -> list[dict[str, object]]:
    kinds = ("baseline",) * 4 + ("long_coast",) * 2 + ("baseline",)
    frames: list[dict[str, object]] = []
    session_offset = 0.0
    for lap_number, kind in enumerate(kinds, start=1):
        profile = _lap_profile(kind)
        for index, lap_pct_value in enumerate(profile["LapDistPct"]):
            lap_pct = float(lap_pct_value)
            sequence = len(frames)
            opponent_pct = (lap_pct + 0.4) % 1.0
            frames.append(
                {
                    "SessionNum": 1,
                    "SessionTick": 10_000 + sequence,
                    "SessionTime": session_offset + float(profile["SessionTime"][index]),
                    "Lap": lap_number,
                    "LapCompleted": lap_number - 1,
                    "LapDistPct": lap_pct,
                    "Speed": float(profile["Speed"][index]),
                    "Throttle": float(profile["Throttle"][index]),
                    "Brake": float(profile["Brake"][index]),
                    "SteeringWheelAngle": float(profile["SteeringWheelAngle"][index]),
                    "FuelLevel": 60.0 - 4.0 * (lap_number - 1 + lap_pct),
                    "FuelLevelPct": 0.70 - 0.005 * (lap_number - 1 + lap_pct),
                    "TrackTempCrew": 26.0,
                    "AirTemp": 20.0,
                    "Precipitation": 0.0,
                    "WindVel": 1.0,
                    "WindDir": 0.0,
                    "PlayerTireCompound": 0,
                    "TireSetsUsed": 1,
                    "OnPitRoad": False,
                    "PlayerCarInPitStall": False,
                    "PlayerTrackSurface": 3,
                    "IsOnTrack": True,
                    "IsOnTrackCar": True,
                    "PlayerCarMyIncidentCount": 0,
                    "PlayerCarDriverIncidentCount": 0,
                    "PlayerCarTeamIncidentCount": 0,
                    "PlayerCarIdx": 0,
                    "CarIdxLap": [lap_number, lap_number],
                    "CarIdxLapCompleted": [lap_number - 1, lap_number - 1],
                    "CarIdxLapDistPct": [lap_pct, opponent_pct],
                    "CarIdxOnPitRoad": [False, False],
                    "CarIdxTrackSurface": [3, 3],
                }
            )
        session_offset += float(profile["SessionTime"][-1]) + 1.0 / TICK_RATE_HZ
    return frames


def _paired_pit_frames() -> list[dict[str, object]]:
    frames = [dict(frame) for frame in _paired_frames()[:8]]
    states = (
        (False, False, False, 5.0),
        (True, False, False, 5.0),
        (True, False, True, 5.0),
        (True, True, True, 8.0),
        (True, True, False, 10.0),
        (True, False, False, 10.0),
        (False, False, False, 10.0),
        (False, False, False, 9.9),
    )
    for index, (frame, (on_road, in_stall, active, fuel_l)) in enumerate(
        zip(frames, states, strict=True)
    ):
        frame.update(
            {
                "FuelLevel": fuel_l,
                "OnPitRoad": on_road,
                "PitstopActive": active,
                "PlayerCarInPitStall": in_stall,
                "SessionTick": 10_000 + index,
                "SessionTime": index / TICK_RATE_HZ,
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
        item = _descriptor(name, value, offset)
        result.append(item)
        offset += SDK_TYPE_SIZES[item.type_code] * item.count
    return tuple(result)


class _StubIbtReader:
    def __init__(self, frames: list[dict[str, object]]) -> None:
        self.frames = frames
        self.variable_names = tuple(frames[0])
        self.variables = _descriptors(frames[0])
        self.metadata = SimpleNamespace(
            file_size_bytes=987_654,
            record_count=len(frames),
            tick_rate_hz=TICK_RATE_HZ,
        )
        self.source_sha256 = "a" * 64
        self.verified = False

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
                    "TrackName": "Synthetic Shared Adapter Circuit",
                }
            },
        )
        for index, frame in enumerate(frames)
    )
    collect_samples_to_jsonl(
        samples,
        path,
        source_id="paired-collector-source",
        session_id=SESSION_ID,
        stale_after_s=1.0,
        fsync_each_record=False,
    )


def _open_ibt(
    path: Path,
    frames: list[dict[str, object]],
):
    return open_ibt_telemetry(
        path,
        source_id="paired-ibt-source",
        session_id=SESSION_ID,
        stale_after_s=1.0,
        reader_factory=lambda _: _StubIbtReader(frames),
    )


def _open_collector(path: Path):
    return open_collector_jsonl(path, stale_after_s=1.0)


def _fuel_scenario() -> FuelScenario:
    return FuelScenario(
        current_fuel_l=20.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
        reserve_l=1.0,
        minimum_valid_laps=5,
    )


def _m1_trust_roots(
    run: ValidatedIbtRun | ValidatedCollectorRun,
) -> tuple[str, str, str]:
    samples = list(run.samples)
    normalized = hashlib.sha256()
    for sample in samples:
        encoded = sample.to_json_line().encode("utf-8")
        normalized.update(len(encoded).to_bytes(8, "little"))
        normalized.update(encoded)
    _, event_receipt = process_telemetry_events(samples)
    evidence = run.evidence.to_dict()
    source_field = "source_sha256" if "source_sha256" in evidence else "records_sha256"
    return (
        str(evidence[source_field]),
        normalized.hexdigest(),
        event_receipt.receipt_sha256,
    )


def _source_neutral_events(events: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for event in events:
        payload = event.to_dict()
        del payload["source_id"]
        del payload["source_kind"]
        result.append(payload)
    return result


def _labels(run: ValidatedIbtRun | ValidatedCollectorRun) -> ApprovedTrackStateLabelSet:
    return ApprovedTrackStateLabelSet.approved(
        source_binding_sha256=_canonical_sha256(run.evidence.to_dict()),
        labels={ordinal: "DRY_STABLE" for ordinal in range(64)},
        reviewer_id="paired-e2e-reviewer",
        reviewed_at_utc="2026-08-08T00:00:00Z",
        method="MANUAL_REPLAY_REVIEW",
        evidence_artifact_sha256="d" * 64,
        human_attestation=HUMAN_TRACK_STATE_ATTESTATION,
    )


def test_public_adapters_share_event_and_fuel_semantics(tmp_path: Path) -> None:
    frames = _paired_frames()
    collector_path = tmp_path / "paired.jsonl"
    _write_collector(collector_path, frames)

    with _open_ibt(tmp_path / "paired.ibt", frames) as run:
        ibt_events, ibt_event_receipt = process_telemetry_events(run.samples)
        ibt_evidence = run.evidence.to_dict()
    with _open_collector(collector_path) as run:
        collector_events, collector_event_receipt = process_telemetry_events(run.samples)
        collector_evidence = run.evidence.to_dict()

    ibt_event_semantics = _source_neutral_events(ibt_events)
    collector_event_semantics = _source_neutral_events(collector_events)
    assert ibt_event_semantics == collector_event_semantics
    assert _canonical_sha256(ibt_event_semantics) == _canonical_sha256(collector_event_semantics)
    ibt_semantic_receipt = ibt_event_receipt.to_dict()
    collector_semantic_receipt = collector_event_receipt.to_dict()
    for key in ("events_sha256", "receipt_sha256"):
        del ibt_semantic_receipt[key]
        del collector_semantic_receipt[key]
    assert ibt_semantic_receipt == collector_semantic_receipt
    assert ibt_event_receipt.events_sha256 != collector_event_receipt.events_sha256
    assert ibt_event_receipt.receipt_sha256 != collector_event_receipt.receipt_sha256
    assert ibt_evidence != collector_evidence

    with _open_ibt(tmp_path / "paired.ibt", frames) as run:
        ibt_fuel = build_fuel_model_replay(run, scenario=_fuel_scenario())
    with _open_collector(collector_path) as run:
        collector_fuel = build_fuel_model_replay(run, scenario=_fuel_scenario())

    assert (
        ibt_fuel["quality_gate"]
        == collector_fuel["quality_gate"]
        == {
            "reasons": [],
            "status": "PASS",
        }
    )
    assert ibt_fuel["lap_receipt"] == collector_fuel["lap_receipt"]
    assert ibt_fuel["model_output"] == collector_fuel["model_output"]
    assert ibt_fuel["model_semantic_sha256"] == collector_fuel["model_semantic_sha256"]
    assert ibt_fuel["input_evidence"] != collector_fuel["input_evidence"]
    assert ibt_fuel["normalized_input_receipt"] != collector_fuel["normalized_input_receipt"]
    assert ibt_fuel["fuel_replay_sha256"] != collector_fuel["fuel_replay_sha256"]


def test_public_adapters_share_m1_pit_stint_semantics_and_separate_provenance(
    tmp_path: Path,
) -> None:
    frames = _paired_pit_frames()
    collector_path = tmp_path / "paired-pit.jsonl"
    _write_collector(collector_path, frames)

    with _open_ibt(tmp_path / "paired-pit.ibt", frames) as run:
        assert run.evidence.tick_rate_hz == TICK_RATE_HZ
        ibt_source, ibt_normalized, ibt_events = _m1_trust_roots(run)
    with _open_ibt(tmp_path / "paired-pit.ibt", frames) as run:
        ibt_m1 = build_pit_stint_receipt(
            run,
            expected_source_sha256=ibt_source,
            expected_normalized_samples_sha256=ibt_normalized,
            expected_event_receipt_sha256=ibt_events,
        )

    with _open_collector(collector_path) as run:
        assert run.evidence.tick_rate_hz_values == (TICK_RATE_HZ,)
        collector_source, collector_normalized, collector_events = _m1_trust_roots(run)
    with _open_collector(collector_path) as run:
        collector_m1 = build_pit_stint_receipt(
            run,
            expected_source_sha256=collector_source,
            expected_normalized_samples_sha256=collector_normalized,
            expected_event_receipt_sha256=collector_events,
        )

    semantic_fields = (
        "capabilities",
        "incomplete_interval_counts",
        "pit_cycles",
        "quality_gate",
        "recommendations",
        "service_contents",
        "status",
        "stints",
        "summary",
    )
    assert {key: ibt_m1[key] for key in semantic_fields} == {
        key: collector_m1[key] for key in semantic_fields
    }
    assert ibt_m1["summary"]["pit_cycle_count"] == 1
    assert ibt_m1["summary"]["service_episode_count"] == 1

    assert ibt_m1["input_evidence"]["source_kind"] == "IBT_OFFLINE"
    assert collector_m1["input_evidence"]["source_kind"] == "SDK_LIVE"
    assert ibt_m1["input_binding"]["source_sha256"] == ibt_source
    assert collector_m1["input_binding"]["source_sha256"] == collector_source
    assert ibt_m1["normalized_input_receipt"] != collector_m1[
        "normalized_input_receipt"
    ]
    assert ibt_m1["upstream_event_receipt"] != collector_m1[
        "upstream_event_receipt"
    ]
    assert ibt_m1["pit_stint_receipt_sha256"] != collector_m1[
        "pit_stint_receipt_sha256"
    ]


def test_m1_rejects_16_67hz_session_time_against_declared_60hz_for_both_adapters(
    tmp_path: Path,
) -> None:
    frames = _paired_pit_frames()
    for index, frame in enumerate(frames):
        frame["SessionTime"] = index * 0.06
    collector_path = tmp_path / "paired-pit-rate-mismatch.jsonl"
    _write_collector(collector_path, frames)

    run_factories = (
        lambda: _open_ibt(tmp_path / "paired-pit-rate-mismatch.ibt", frames),
        lambda: _open_collector(collector_path),
    )
    for open_run in run_factories:
        with open_run() as run:
            source, normalized, events = _m1_trust_roots(run)
        with open_run() as run, pytest.raises(PitStintReceiptError) as raised:
            build_pit_stint_receipt(
                run,
                expected_source_sha256=source,
                expected_normalized_samples_sha256=normalized,
                expected_event_receipt_sha256=events,
            )
        assert raised.value.code == "TIMING_RATE_MISMATCH"


def test_public_adapters_share_driving_and_condition_semantics(tmp_path: Path) -> None:
    frames = _paired_frames()
    collector_path = tmp_path / "paired.jsonl"
    _write_collector(collector_path, frames)
    driving_config = DrivingAnalysisConfig(grid_step_m=3.0)

    with _open_ibt(tmp_path / "paired.ibt", frames) as run:
        ibt_driving = build_driving_model_replay(run, config=driving_config)
    with _open_collector(collector_path) as run:
        collector_driving = build_driving_model_replay(run, config=driving_config)

    assert ibt_driving["readiness_status"] == collector_driving["readiness_status"] == "PASS"
    assert ibt_driving["semantic_input_receipt"] == collector_driving["semantic_input_receipt"]
    assert ibt_driving["lap_receipt"] == collector_driving["lap_receipt"]
    assert ibt_driving["model_output"] == collector_driving["model_output"]
    assert ibt_driving["model_semantic_sha256"] == collector_driving["model_semantic_sha256"]
    assert ibt_driving["normalized_input_receipt"] != collector_driving["normalized_input_receipt"]
    assert ibt_driving["input_provenance_sha256"] != collector_driving["input_provenance_sha256"]
    assert ibt_driving["driving_replay_sha256"] != collector_driving["driving_replay_sha256"]

    condition_config = ConditionCohortConfig(min_matched_laps=2)

    def condition_builder(
        run: ValidatedIbtRun | ValidatedCollectorRun,
    ) -> dict[str, object]:
        return build_condition_cohort(
            run,
            target_lap_ordinal=2,
            track_state_labels=_labels(run),
            config=condition_config,
        )

    with _open_ibt(tmp_path / "paired.ibt", frames) as run:
        ibt_condition = condition_builder(run)
    with _open_collector(collector_path) as run:
        collector_condition = condition_builder(run)

    assert ibt_condition["readiness_status"] == collector_condition["readiness_status"] == "PASS"
    assert (
        ibt_condition["condition_config_sha256"] == collector_condition["condition_config_sha256"]
    )
    assert ibt_condition["lap_conditions"] == collector_condition["lap_conditions"]
    assert ibt_condition["pairs"] == collector_condition["pairs"]
    assert (
        ibt_condition["condition_semantic_sha256"]
        == collector_condition["condition_semantic_sha256"]
    )
    assert (
        ibt_condition["normalized_input_receipt"] != collector_condition["normalized_input_receipt"]
    )
    assert (
        ibt_condition["condition_provenance_sha256"]
        != collector_condition["condition_provenance_sha256"]
    )
    assert (
        ibt_condition["condition_cohort_sha256"] != collector_condition["condition_cohort_sha256"]
    )
