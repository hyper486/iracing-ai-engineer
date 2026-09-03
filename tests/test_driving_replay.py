from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator

import numpy as np
import pytest

import iracing_ai_engineer.adapters as adapters_module
from iracing_ai_engineer import cli
from iracing_ai_engineer.adapters import (
    CollectorInputEvidence,
    IbtInputEvidence,
    TrackContextAvailability,
    TrackContextEvidence,
    TrackContextProvenance,
    TrackContextStatus,
    ValidatedIbtRun,
)
from iracing_ai_engineer.collector import CollectorSample, collect_samples_to_jsonl
from iracing_ai_engineer.driving import DrivingAnalysisConfig
from iracing_ai_engineer.driving_model_replay import (
    DRIVING_MODEL_REPLAY_CONTRACT_VERSION,
    DrivingModelReplayError,
    _build_driving_model_replay_samples,
    build_driving_model_replay,
)
from iracing_ai_engineer.sdk_probe import RawSdkFrame, VariableDescriptor
from iracing_ai_engineer.telemetry import (
    SourceKind,
    TelemetrySample,
    normalize_sdk_frame,
)

TRACK_LENGTH_M = 1_200.0
TRACK_LENGTH_MM = 1_200_000
TICK_RATE_HZ = 60
_SOURCE_FIELD = "WeekendInfo.TrackLength"


def _canonical_digest(value: object) -> str:
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
    distance = np.arange(0.0, TRACK_LENGTH_M + 1.0, dtype=np.float64)
    speed = np.full_like(distance, 50.0)
    throttle = np.ones_like(distance)
    brake = np.zeros_like(distance)
    steering = np.zeros_like(distance)
    corners = [
        {
            "brake_onset": 180,
            "brake_release": 240,
            "apex": 260,
            "throttle_pickup": 270,
            "minimum_speed": 20.0,
            "recovery_end": 350,
        },
        {
            "brake_onset": 530,
            "brake_release": 590,
            "apex": 610,
            "throttle_pickup": 620,
            "minimum_speed": 20.0,
            "recovery_end": 700,
        },
        {
            "brake_onset": 880,
            "brake_release": 940,
            "apex": 960,
            "throttle_pickup": 970,
            "minimum_speed": 20.0,
            "recovery_end": 1_050,
        },
    ]
    if kind == "long_coast":
        corners[0].update(
            brake_onset=158,
            brake_release=218,
            throttle_pickup=292,
            minimum_speed=18.5,
            recovery_end=380,
        )
    elif kind != "baseline":
        raise AssertionError(f"unknown synthetic lap kind: {kind}")
    for corner in corners:
        _corner_profile(distance, speed, throttle, brake, **corner)
    for apex in (260, 610, 960):
        steering += 0.18 * np.exp(-0.5 * ((distance - apex) / 35.0) ** 2)
    elapsed = np.r_[
        0.0,
        np.cumsum(np.diff(distance) / ((speed[:-1] + speed[1:]) / 2.0)),
    ]
    return {
        "SessionTime": elapsed,
        "LapDistPct": distance / TRACK_LENGTH_M,
        "Speed": speed,
        "Throttle": throttle,
        "Brake": brake,
        "SteeringWheelAngle": steering,
    }


def _raw_frames(
    *,
    include_incidents: bool = True,
    drop_at: int | None = None,
) -> Iterator[tuple[int, dict[str, object]]]:
    # File boundaries are deliberately partial. The six interior laps contain
    # three reproducible baselines and two repeated long-coast examples.
    kinds = ("baseline",) * 4 + ("long_coast",) * 2 + ("baseline",) * 2
    sequence = 0
    session_offset = 0.0
    for lap_number, kind in enumerate(kinds, start=1):
        lap = _lap_profile(kind)
        for index in range(len(lap["SessionTime"])):
            shifted = int(drop_at is not None and sequence >= drop_at)
            values: dict[str, object] = {
                "SessionNum": 1,
                "SessionTick": 10_000 + sequence + shifted,
                "SessionTime": session_offset + float(lap["SessionTime"][index]),
                "Lap": lap_number,
                "LapCompleted": lap_number - 1,
                "LapDistPct": float(lap["LapDistPct"][index]),
                "Speed": float(lap["Speed"][index]),
                "Throttle": float(lap["Throttle"][index]),
                "Brake": float(lap["Brake"][index]),
                "SteeringWheelAngle": float(lap["SteeringWheelAngle"][index]),
                "OnPitRoad": False,
                "PlayerCarInPitStall": False,
                "PlayerTrackSurface": 3,
                "IsOnTrack": True,
                "IsOnTrackCar": True,
            }
            if include_incidents:
                values.update(
                    PlayerCarMyIncidentCount=0,
                    PlayerCarDriverIncidentCount=0,
                    PlayerCarTeamIncidentCount=0,
                )
            yield 20_000 + sequence + shifted, values
            sequence += 1
        session_offset += float(lap["SessionTime"][-1]) + 1.0 / TICK_RATE_HZ


def _normalized_samples(
    source_kind: SourceKind,
    *,
    include_incidents: bool = True,
    drop_at: int | None = None,
) -> Iterator[TelemetrySample]:
    source_id = (
        "driving-fixture-ibt"
        if source_kind is SourceKind.IBT_OFFLINE
        else "driving-fixture-live"
    )
    previous: TelemetrySample | None = None
    for buffer_tick, values in _raw_frames(
        include_incidents=include_incidents,
        drop_at=drop_at,
    ):
        sample = normalize_sdk_frame(
            values,
            source_id=source_id,
            session_id="driving-replay-session",
            source_kind=source_kind,
            buffer_tick=buffer_tick,
            captured_monotonic_s=(
                float(values["SessionTime"])
                if source_kind is SourceKind.SDK_LIVE
                else None
            ),
            previous=previous,
        )
        yield sample
        previous = sample


def _sample_count() -> int:
    return sum(1 for _ in _raw_frames())


def _collector_descriptors() -> tuple[VariableDescriptor, ...]:
    fields = (
        ("SessionNum", 2, "int32", 0),
        ("SessionTick", 2, "int32", 4),
        ("SessionTime", 4, "float32", 8),
        ("Lap", 2, "int32", 12),
        ("LapCompleted", 2, "int32", 16),
        ("LapDistPct", 4, "float32", 20),
        ("Speed", 4, "float32", 24),
        ("Throttle", 4, "float32", 28),
        ("Brake", 4, "float32", 32),
        ("SteeringWheelAngle", 4, "float32", 36),
        ("OnPitRoad", 1, "bool", 40),
        ("PlayerCarInPitStall", 1, "bool", 41),
        ("PlayerTrackSurface", 2, "int32", 44),
        ("IsOnTrack", 1, "bool", 48),
        ("IsOnTrackCar", 1, "bool", 49),
        ("PlayerCarMyIncidentCount", 2, "int32", 52),
        ("PlayerCarDriverIncidentCount", 2, "int32", 56),
        ("PlayerCarTeamIncidentCount", 2, "int32", 60),
    )
    return tuple(
        VariableDescriptor(name, type_code, dtype, offset, 1, False, "", "")
        for name, type_code, dtype, offset in fields
    )


def _input_evidence(
    source_kind: SourceKind,
    *,
    dropped_tick_count: int = 0,
) -> IbtInputEvidence | CollectorInputEvidence:
    count = _sample_count()
    if source_kind is SourceKind.IBT_OFFLINE:
        return IbtInputEvidence(
            source_id="driving-fixture-ibt",
            session_id="driving-replay-session",
            source_sha256="a" * 64,
            byte_size=1_234_567,
            record_count=count,
            tick_rate_hz=TICK_RATE_HZ,
        )
    return CollectorInputEvidence(
        source_id="driving-fixture-live",
        session_id="driving-replay-session",
        source_kind=SourceKind.SDK_LIVE,
        sim_mode="full",
        completion_status="COMPLETE",
        semantic_record_count=count + 4,
        records_sha256="b" * 64,
        frame_record_count=count,
        event_record_count=1 if dropped_tick_count else 0,
        schema_record_count=1,
        session_info_record_count=1,
        samples_seen=count,
        duplicate_sample_count=0,
        duplicate_conflict_count=0,
        dropped_tick_count=dropped_tick_count,
        stale_event_count=0,
        session_reset_count=0,
        schema_change_count=0,
        schema_epoch_count=1,
        session_epoch_count=1,
        first_buffer_tick=20_000,
        last_buffer_tick=20_000 + count - 1 + dropped_tick_count,
        tick_rate_hz_values=(TICK_RATE_HZ,),
    )


def _track_context(
    evidence: IbtInputEvidence | CollectorInputEvidence,
    *,
    available: bool = True,
) -> TrackContextEvidence:
    ibt = isinstance(evidence, IbtInputEvidence)
    return TrackContextEvidence(
        track_length_mm=TRACK_LENGTH_MM if available else None,
        source_field=_SOURCE_FIELD,
        availability=(
            TrackContextAvailability.AVAILABLE
            if available
            else TrackContextAvailability.UNAVAILABLE
        ),
        status=(
            TrackContextStatus.VERIFIED
            if available
            else TrackContextStatus.TRACK_LENGTH_MISSING
            if ibt
            else TrackContextStatus.SESSION_INFO_UNAVAILABLE
        ),
        provenance=(
            TrackContextProvenance.IBT_SAME_HANDLE_SESSION_INFO
            if ibt
            else TrackContextProvenance.COLLECTOR_VALIDATED_SNAPSHOT
        ),
        source_binding_sha256=_canonical_digest(evidence.to_dict()),
    )


def _build(
    source_kind: SourceKind,
    *,
    include_incidents: bool = True,
    track_available: bool = True,
    drop_at: int | None = None,
) -> dict[str, object]:
    dropped = int(drop_at is not None)
    evidence = _input_evidence(source_kind, dropped_tick_count=dropped)
    return _build_driving_model_replay_samples(
        _normalized_samples(
            source_kind,
            include_incidents=include_incidents,
            drop_at=drop_at,
        ),
        input_kind=("ibt" if source_kind is SourceKind.IBT_OFFLINE else "collector"),
        input_evidence=evidence,
        track_context=_track_context(evidence, available=track_available),
        tick_rate_hz=TICK_RATE_HZ,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
        config=DrivingAnalysisConfig(),
    )


def test_equivalent_ibt_and_collector_share_source_neutral_driving_semantics():
    ibt = _build(SourceKind.IBT_OFFLINE)
    collector = _build(SourceKind.SDK_LIVE)

    assert ibt["quality_gate"] == collector["quality_gate"] == {
        "reasons": [],
        "status": "PASS",
    }
    assert ibt["readiness_status"] == collector["readiness_status"] == "PASS"
    assert ibt["semantic_input_receipt"] == collector["semantic_input_receipt"]
    assert ibt["lap_receipt"] == collector["lap_receipt"]
    assert ibt["model_output"] == collector["model_output"]
    assert ibt["model_semantic_sha256"] == collector["model_semantic_sha256"]

    assert ibt["input_evidence"] != collector["input_evidence"]
    assert ibt["normalized_input_receipt"] != collector["normalized_input_receipt"]
    assert ibt["input_provenance_sha256"] != collector["input_provenance_sha256"]
    assert ibt["driving_replay_sha256"] != collector["driving_replay_sha256"]


@pytest.mark.parametrize(
    ("include_incidents", "track_available", "expected_reason"),
    [
        (False, True, "CLEANLINESS_UNOBSERVABLE"),
        (True, False, "TRACK_LENGTH_UNAVAILABLE"),
    ],
)
def test_missing_incident_or_track_context_waits_without_partial_advice(
    include_incidents: bool,
    track_available: bool,
    expected_reason: str,
):
    payload = _build(
        SourceKind.SDK_LIVE,
        include_incidents=include_incidents,
        track_available=track_available,
    )

    assert payload["readiness_status"] == "WAIT_DRIVING_DATA"
    assert payload["quality_gate"]["status"] == "DEGRADED"
    assert expected_reason in payload["quality_gate"]["reasons"]
    assert payload["capabilities"]["driving_model_shadow"]["status"] == "FAIL"
    assert payload["recommendations"] == []


def test_drop_failure_outranks_wait_driving_data():
    payload = _build(
        SourceKind.SDK_LIVE,
        include_incidents=False,
        drop_at=2_000,
    )

    assert payload["readiness_status"] == "FAIL"
    assert payload["quality_gate"]["status"] == "DEGRADED"
    assert "DROPPED_TICKS" in payload["quality_gate"]["reasons"]
    assert "CLEANLINESS_UNOBSERVABLE" in payload["quality_gate"]["reasons"]
    assert payload["capabilities"]["driving_model_shadow"]["status"] == "FAIL"
    assert payload["recommendations"] == []


def test_public_driving_replay_api_rejects_runs_outside_adapter_registry():
    with pytest.raises(DrivingModelReplayError, match="open validated telemetry adapter"):
        build_driving_model_replay(object(), config=DrivingAnalysisConfig())  # type: ignore[arg-type]

    evidence = _input_evidence(SourceKind.IBT_OFFLINE)
    assert isinstance(evidence, IbtInputEvidence)
    with pytest.raises(TypeError, match="only be created"):
        ValidatedIbtRun(
            evidence,
            _normalized_samples(SourceKind.IBT_OFFLINE),
            stale_after_s=0.5,
            opponent_error_policy="degrade",
            _token=object(),
        )

    forged = ValidatedIbtRun(
        evidence,
        _normalized_samples(SourceKind.IBT_OFFLINE),
        stale_after_s=0.5,
        opponent_error_policy="degrade",
        _token=adapters_module._VALIDATED_RUN_TOKEN,
    )
    with pytest.raises(DrivingModelReplayError, match="open validated telemetry adapter"):
        build_driving_model_replay(forged, config=DrivingAnalysisConfig())


def test_ready_advice_is_descriptive_non_executable_and_capability_gated():
    payload = _build(SourceKind.SDK_LIVE)

    assert payload["readiness_status"] == "PASS"
    assert payload["capabilities"]["driving_model_shadow"]["status"] == "PASS"
    assert payload["capabilities"]["personalized_coaching"] == {
        "blocked_claims": [
            "PERSONALIZED_ACTION",
            "CAUSAL_GAIN_CLAIM",
            "TRAIL_BRAKING_CLAIM",
        ],
        "confidence": "NONE",
        "contract_version": "inference-capability-v1",
        "estimate_available": False,
        "provenance": "UNKNOWN",
        "reasons": [
            "CONDITION_COHORT_NOT_ATTACHED",
            "MATCHED_CONTEXT_HISTORY_UNAVAILABLE",
            "HUMAN_CORNER_LABELS_MISSING",
        ],
        "status": "SKIP",
    }
    assert payload["capabilities"]["curb_guidance"]["status"] == "SKIP"
    assert payload["capabilities"]["current_tire_wear"]["status"] == "SKIP"
    assert payload["capabilities"]["traffic_model"]["status"] == "SKIP"
    assert payload["capabilities"]["race_coaching"] == {
        "reasons": [
            "SHADOW_ONLY",
            "PERSONALIZED_COACHING_UNAVAILABLE",
            "TRAFFIC_MODEL_NOT_IMPLEMENTED",
        ],
        "status": "BLOCKED",
    }

    recommendations = payload["recommendations"]
    assert recommendations
    for item in recommendations:
        assert item["claim_level"] == "descriptive"
        assert item["status"] == "SHADOW_ONLY"
        assert item["practice_only"] is True
        assert item["executable"] is False
        assert item["confidence_basis"]["external_validity"] == "UNKNOWN"
        assert item["confidence_basis"]["causal_validity"] == "NOT_CLAIMED"
        assert item["evidence_lap_ids"]
        assert all(
            evidence_id.startswith(payload["input_provenance_sha256"] + ":")
            for evidence_id in item["evidence_lap_ids"]
        )


def test_driving_replay_cli_reports_missing_ibt_as_machine_readable_wait_data(
    capsys,
    tmp_path,
):
    missing = tmp_path / "missing.ibt"

    exit_code = cli.main(
        [
            "driving-replay",
            str(missing),
            "--source-id",
            "missing-source",
            "--session-id",
            "missing-session",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert json.loads(captured.out) == {
        "contract_version": DRIVING_MODEL_REPLAY_CONTRACT_VERSION,
        "error": "WAIT_DATA",
        "message": str(missing),
    }
    assert captured.err.strip() == str(missing)


def test_driving_replay_cli_require_ready_rejects_wait_collector(
    capsys,
    tmp_path,
):
    collector_path = tmp_path / "driving-wait.jsonl"
    raw_frames = _raw_frames()
    observations = tuple(
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=buffer_tick,
                session_info_update=1,
                values=values,
                sim_mode_raw="full",
                captured_monotonic_s=float(values["SessionTime"]),
            ),
            descriptors=_collector_descriptors(),
            tick_rate_hz=TICK_RATE_HZ,
            session_info={"WeekendInfo": {"SimMode": "full"}},
        )
        for buffer_tick, values in (next(raw_frames), next(raw_frames))
    )
    collect_samples_to_jsonl(
        observations,
        collector_path,
        source_id="driving-cli-fixture",
        session_id="driving-cli-session",
    )

    exit_code = cli.main(
        ["driving-replay", str(collector_path), "--require-ready"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 5
    assert captured.err == ""
    assert payload["input_kind"] == "collector"
    assert payload["readiness_status"] == "WAIT_DRIVING_DATA"
    assert payload["quality_gate"]["status"] == "DEGRADED"
    assert "TRACK_LENGTH_UNAVAILABLE" in payload["quality_gate"]["reasons"]
    assert payload["capabilities"]["driving_model_shadow"]["status"] == "FAIL"
    assert payload["recommendations"] == []
