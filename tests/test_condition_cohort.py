from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import iracing_ai_engineer.adapters as adapters_module
from iracing_ai_engineer.adapters import (
    CollectorInputEvidence,
    IbtInputEvidence,
    TrackContextAvailability,
    TrackContextEvidence,
    TrackContextProvenance,
    TrackContextStatus,
    ValidatedIbtRun,
    open_ibt_telemetry,
)
from iracing_ai_engineer.condition_cohort import (
    CONDITION_COHORT_CONTRACT_VERSION,
    HUMAN_TRACK_STATE_ATTESTATION,
    ApprovedTrackStateLabelSet,
    ConditionCohortConfig,
    ConditionCohortError,
    _build_condition_cohort_samples,
    build_condition_cohort,
)
from iracing_ai_engineer.telemetry import (
    SourceKind,
    TelemetrySample,
    normalize_sdk_frame,
)

TRACK_LENGTH_MM = 1_000_000
TICK_RATE_HZ = 60
_SOURCE_FIELD = "WeekendInfo.TrackLength"


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _raw_frames(
    *,
    lap_count: int = 4,
    include_opponents: bool = True,
    close_opponent: bool = False,
    tire_set_by_lap: dict[int, int] | None = None,
    invalid_precipitation_lap: int | None = None,
    mid_lap_refuel_lap: int | None = None,
    gradual_refuel_lap: int | None = None,
    pit_exit_laps: set[int] | None = None,
    constant_conditions: bool = False,
) -> Iterator[tuple[int, dict[str, object]]]:
    sequence = 0
    tire_set_by_lap = tire_set_by_lap or {}
    pit_exit_laps = pit_exit_laps or set()
    for lap_number in range(1, lap_count + 1):
        for index in range(101):
            lap_pct = index / 100.0
            opponent_delta = 0.02 if close_opponent else 0.20
            opponent_pct = (lap_pct + opponent_delta) % 1.0
            on_pit_road = lap_number in pit_exit_laps and index < 50
            completed_pit_exits = sum(value < lap_number for value in pit_exit_laps)
            if lap_number in pit_exit_laps and index >= 50:
                completed_pit_exits += 1
            tire_sets_used = tire_set_by_lap.get(lap_number, 1 + completed_pit_exits)
            fuel_level_pct = 0.70 if constant_conditions else 0.70 - 0.01 * (lap_number - 1)
            if mid_lap_refuel_lap == lap_number and index >= 50:
                fuel_level_pct += 0.02
            if gradual_refuel_lap == lap_number:
                fuel_level_pct += index * 0.0001
            values: dict[str, object] = {
                "SessionNum": 1,
                "SessionTick": 10_000 + sequence,
                "SessionTime": sequence / TICK_RATE_HZ,
                "Lap": lap_number,
                "LapCompleted": lap_number - 1,
                "LapDistPct": lap_pct,
                "Speed": 50.0,
                "Throttle": 0.8,
                "Brake": 0.0,
                "SteeringWheelAngle": 0.0,
                "FuelLevel": 60.0 - sequence / 10_000,
                "FuelLevelPct": fuel_level_pct,
                "TrackTempCrew": (26.0 if constant_conditions else 26.0 + 0.1 * (lap_number - 1)),
                "AirTemp": (20.0 if constant_conditions else 20.0 + 0.1 * (lap_number - 1)),
                "Precipitation": (-0.1 if invalid_precipitation_lap == lap_number else 0.0),
                "WindVel": 1.0,
                "WindDir": 0.0,
                "PlayerTireCompound": 0,
                "TireSetsUsed": tire_sets_used,
                "OnPitRoad": on_pit_road,
                "PlayerCarInPitStall": False,
                "PlayerTrackSurface": 3,
                "IsOnTrack": True,
                "IsOnTrackCar": True,
                "PlayerCarMyIncidentCount": 0,
                "PlayerCarDriverIncidentCount": 0,
                "PlayerCarTeamIncidentCount": 0,
            }
            if include_opponents:
                values.update(
                    PlayerCarIdx=0,
                    CarIdxLap=[lap_number, lap_number],
                    CarIdxLapCompleted=[lap_number - 1, lap_number - 1],
                    CarIdxLapDistPct=[lap_pct, opponent_pct],
                    CarIdxOnPitRoad=[False, False],
                    CarIdxTrackSurface=[3, 3],
                )
            yield 20_000 + sequence, values
            sequence += 1


def _source_id(source_kind: SourceKind) -> str:
    return "condition-ibt" if source_kind is SourceKind.IBT_OFFLINE else "condition-live"


def _normalized_samples(
    source_kind: SourceKind,
    **frame_options: object,
) -> Iterator[TelemetrySample]:
    previous: TelemetrySample | None = None
    for buffer_tick, values in _raw_frames(**frame_options):
        sample = normalize_sdk_frame(
            values,
            source_id=_source_id(source_kind),
            session_id="condition-session",
            source_kind=source_kind,
            buffer_tick=buffer_tick,
            previous=previous,
        )
        yield sample
        previous = sample


def _sample_count(**frame_options: object) -> int:
    return sum(1 for _ in _raw_frames(**frame_options))


def _evidence(
    source_kind: SourceKind,
    *,
    frame_options: dict[str, object] | None = None,
) -> IbtInputEvidence | CollectorInputEvidence:
    count = _sample_count(**(frame_options or {}))
    if source_kind is SourceKind.IBT_OFFLINE:
        return IbtInputEvidence(
            source_id=_source_id(source_kind),
            session_id="condition-session",
            source_sha256="a" * 64,
            byte_size=123_456,
            record_count=count,
            tick_rate_hz=TICK_RATE_HZ,
        )
    return CollectorInputEvidence(
        source_id=_source_id(source_kind),
        session_id="condition-session",
        source_kind=SourceKind.SDK_LIVE,
        sim_mode="full",
        completion_status="COMPLETE",
        semantic_record_count=count + 3,
        records_sha256="b" * 64,
        frame_record_count=count,
        event_record_count=0,
        schema_record_count=1,
        session_info_record_count=1,
        samples_seen=count,
        duplicate_sample_count=0,
        duplicate_conflict_count=0,
        dropped_tick_count=0,
        stale_event_count=0,
        session_reset_count=0,
        schema_change_count=0,
        schema_epoch_count=1,
        session_epoch_count=1,
        first_buffer_tick=20_000,
        last_buffer_tick=20_000 + count - 1,
        tick_rate_hz_values=(TICK_RATE_HZ,),
    )


def _track_context(
    evidence: IbtInputEvidence | CollectorInputEvidence,
) -> TrackContextEvidence:
    is_ibt = isinstance(evidence, IbtInputEvidence)
    return TrackContextEvidence(
        track_length_mm=TRACK_LENGTH_MM,
        source_field=_SOURCE_FIELD,
        availability=TrackContextAvailability.AVAILABLE,
        status=TrackContextStatus.VERIFIED,
        provenance=(
            TrackContextProvenance.IBT_SAME_HANDLE_SESSION_INFO
            if is_ibt
            else TrackContextProvenance.COLLECTOR_VALIDATED_SNAPSHOT
        ),
        source_binding_sha256=_digest(evidence.to_dict()),
    )


def _labels(
    evidence: IbtInputEvidence | CollectorInputEvidence,
) -> ApprovedTrackStateLabelSet:
    return ApprovedTrackStateLabelSet.approved(
        source_binding_sha256=_digest(evidence.to_dict()),
        labels={ordinal: "DRY_STABLE" for ordinal in range(64)},
        reviewer_id="fixture-human-reviewer",
        reviewed_at_utc="2026-08-08T00:00:00Z",
        method="MANUAL_REPLAY_REVIEW",
        evidence_artifact_sha256="d" * 64,
        human_attestation=HUMAN_TRACK_STATE_ATTESTATION,
    )


def _build(
    source_kind: SourceKind,
    *,
    target_lap_ordinal: int = 1,
    labels: bool = True,
    config: ConditionCohortConfig | None = None,
    **frame_options: object,
) -> dict[str, object]:
    options = dict(frame_options)
    evidence = _evidence(source_kind, frame_options=options)
    return _build_condition_cohort_samples(
        _normalized_samples(source_kind, **options),
        input_kind=("ibt" if source_kind is SourceKind.IBT_OFFLINE else "collector"),
        input_evidence=evidence,
        track_context=_track_context(evidence),
        tick_rate_hz=TICK_RATE_HZ,
        target_lap_ordinal=target_lap_ordinal,
        track_state_labels=_labels(evidence) if labels else None,
        config=config,
    )


def test_equivalent_ibt_and_collector_share_semantics_but_not_provenance():
    config = ConditionCohortConfig(min_matched_laps=2)
    ibt = _build(SourceKind.IBT_OFFLINE, config=config)
    collector = _build(SourceKind.SDK_LIVE, config=config)

    assert ibt["readiness_status"] == collector["readiness_status"] == "PASS"
    assert ibt["condition_config_sha256"] == collector["condition_config_sha256"]
    assert ibt["condition_semantic_sha256"] == collector["condition_semantic_sha256"]
    assert ibt["lap_conditions"] == collector["lap_conditions"]
    assert ibt["pairs"] == collector["pairs"]

    assert ibt["condition_provenance_sha256"] != collector["condition_provenance_sha256"]
    assert ibt["condition_cohort_sha256"] != collector["condition_cohort_sha256"]
    assert ibt["normalized_input_receipt"] != collector["normalized_input_receipt"]


def test_single_unobserved_stint_cannot_fake_default_eight_lap_cohort():
    payload = _build(SourceKind.SDK_LIVE, lap_count=10)

    assert payload["contract_version"] == CONDITION_COHORT_CONTRACT_VERSION
    assert payload["readiness_status"] == "WAIT_MATCHED_LAPS"
    assert len(payload["matched_lap_ordinals"]) <= 3
    assert "INSUFFICIENT_MATCHED_LAPS" in payload["quality_gate"]["reasons"]
    assert payload["recommendations"] == []


def test_default_minimum_eight_is_reachable_across_observed_new_tire_stints():
    payload = _build(
        SourceKind.SDK_LIVE,
        lap_count=18,
        pit_exit_laps=set(range(2, 17, 2)),
        constant_conditions=True,
        target_lap_ordinal=2,
    )

    assert payload["readiness_status"] == "PASS"
    assert payload["trusted_readiness_status"] == "WAIT_HUMAN_AUTHENTICATION"
    assert payload["quality_gate"] == {
        "reasons": ["SELF_ATTESTED_NOT_AUTHENTICATED"],
        "status": "DEGRADED",
    }
    assert payload["capabilities"]["track_state_authenticity"] == {
        "authenticated": False,
        "reasons": ["SELF_ATTESTED_NOT_AUTHENTICATED"],
        "status": "WAIT_HUMAN_AUTHENTICATION",
    }
    assert payload["matched_lap_ordinals"] == list(range(2, 17, 2))
    assert len(payload["matched_lap_ordinals"]) == 8
    for ordinal in payload["matched_lap_ordinals"]:
        condition = next(
            item for item in payload["lap_conditions"] if item["lap_ordinal"] == ordinal
        )
        assert condition["tire_usage_context"]["stint_lap_age"] == 1
        assert condition["tire_usage_context"]["set_change_observed"] is True


def test_player_track_surface_never_substitutes_for_approved_track_state_label():
    payload = _build(
        SourceKind.SDK_LIVE,
        labels=False,
        config=ConditionCohortConfig(min_matched_laps=2),
    )

    assert payload["readiness_status"] == "WAIT_CONDITION_DATA"
    assert payload["quality_gate"]["status"] == "DEGRADED"
    assert "APPROVED_TRACK_STATE_LABEL_MISSING" in payload["quality_gate"]["reasons"]
    target_pair = next(item for item in payload["pairs"] if item["candidate_lap_ordinal"] == 1)
    assert target_pair["dimensions"]["track_state"] == {
        "reasons": ["APPROVED_TRACK_STATE_LABEL_MISSING"],
        "status": "UNAVAILABLE",
    }
    assert all(
        condition["track_state"]["availability"] == "UNAVAILABLE"
        for condition in payload["lap_conditions"]
    )


def test_same_stint_tire_context_rejects_non_adjacent_relative_age():
    payload = _build(
        SourceKind.SDK_LIVE,
        lap_count=5,
        config=ConditionCohortConfig(min_matched_laps=2),
    )

    pair = next(item for item in payload["pairs"] if item["candidate_lap_ordinal"] == 3)
    assert pair["disposition"] == "NO_MATCH"
    assert pair["dimensions"]["tire_usage_context"] == {
        "same_observed_stint": True,
        "stint_lap_age_delta": 2,
        "reasons": ["TIRE_USAGE_AGE_MISMATCH"],
        "status": "MISMATCHED",
    }
    assert payload["capabilities"]["current_tire_wear"]["status"] == "SKIP"
    assert payload["capabilities"]["current_tire_wear"]["estimate_available"] is False
    assert "wear" not in json.dumps(payload["lap_conditions"], sort_keys=True).lower()


@pytest.mark.parametrize(
    ("frame_options", "expected_status", "expected_reason"),
    [
        ({"include_opponents": False}, "WAIT_CONDITION_DATA", "OPPONENT_ARRAYS_MISSING"),
        ({"close_opponent": True}, "WAIT_MATCHED_LAPS", "TRAFFIC_CONTAMINATED"),
    ],
)
def test_traffic_is_only_a_proximity_gate_and_never_opens_traffic_model(
    frame_options: dict[str, object],
    expected_status: str,
    expected_reason: str,
):
    payload = _build(
        SourceKind.SDK_LIVE,
        config=ConditionCohortConfig(min_matched_laps=2),
        **frame_options,
    )

    assert payload["readiness_status"] == expected_status
    assert any(expected_reason in item["reasons"] for item in payload["pairs"])
    assert payload["capabilities"]["traffic_model"]["status"] == "SKIP"
    assert payload["capabilities"]["traffic_model"]["estimate_available"] is False
    serialized = json.dumps(payload, sort_keys=True)
    assert "traffic_loss_s" not in serialized
    assert "opponent_fuel" not in serialized


def test_invalid_environment_fails_before_missing_or_mismatch():
    payload = _build(
        SourceKind.SDK_LIVE,
        invalid_precipitation_lap=2,
        labels=False,
        include_opponents=False,
        config=ConditionCohortConfig(min_matched_laps=2),
    )

    assert payload["readiness_status"] == "FAIL"
    assert payload["quality_gate"]["status"] == "FAIL"
    assert "Precipitation_INVALID" in payload["quality_gate"]["reasons"]
    assert any(item["disposition"] == "FAIL" for item in payload["pairs"])
    assert payload["recommendations"] == []


def test_mid_lap_refuel_is_invalid_even_when_start_fuel_matches():
    payload = _build(
        SourceKind.SDK_LIVE,
        mid_lap_refuel_lap=2,
        config=ConditionCohortConfig(min_matched_laps=2),
    )

    assert payload["readiness_status"] == "FAIL"
    assert payload["quality_gate"]["status"] == "FAIL"
    assert "MID_LAP_REFUEL_OBSERVED" in payload["quality_gate"]["reasons"]
    target = next(item for item in payload["lap_conditions"] if item["lap_ordinal"] == 1)
    assert target["fuel_load"] == {
        "availability": "INVALID",
        "reasons": ["MID_LAP_REFUEL_OBSERVED"],
    }


def test_gradual_mid_lap_refuel_cannot_evade_the_running_minimum_gate():
    payload = _build(
        SourceKind.SDK_LIVE,
        gradual_refuel_lap=2,
        config=ConditionCohortConfig(min_matched_laps=2),
    )

    assert payload["readiness_status"] == "FAIL"
    assert payload["quality_gate"]["status"] == "FAIL"
    assert "MID_LAP_REFUEL_OBSERVED" in payload["quality_gate"]["reasons"]
    target = next(item for item in payload["lap_conditions"] if item["lap_ordinal"] == 1)
    assert target["fuel_load"] == {
        "availability": "INVALID",
        "reasons": ["MID_LAP_REFUEL_OBSERVED"],
    }


def test_approved_track_state_labels_require_explicit_human_review_evidence():
    evidence = _evidence(SourceKind.SDK_LIVE)
    labels = _labels(evidence)

    label_payload = labels.to_dict()
    assert label_payload["reviewer_id"] == "fixture-human-reviewer"
    assert label_payload["reviewed_at_utc"] == "2026-08-08T00:00:00Z"
    assert label_payload["method"] == "MANUAL_REPLAY_REVIEW"
    assert label_payload["evidence_artifact_sha256"] == "d" * 64
    assert label_payload["human_attestation"] == HUMAN_TRACK_STATE_ATTESTATION
    assert label_payload["authenticity_status"] == "SELF_ATTESTED_NOT_AUTHENTICATED"

    with pytest.raises(ConditionCohortError, match="fixed human attestation"):
        ApprovedTrackStateLabelSet.approved(
            source_binding_sha256=_digest(evidence.to_dict()),
            labels={1: "DRY_STABLE"},
            reviewer_id="fixture-human-reviewer",
            reviewed_at_utc="2026-08-08T00:00:00Z",
            method="MANUAL_REPLAY_REVIEW",
            evidence_artifact_sha256="d" * 64,
            human_attestation="model generated approval",
        )


def test_label_set_must_be_approved_and_bound_to_the_same_input_evidence():
    evidence = _evidence(SourceKind.SDK_LIVE)
    wrong_labels = ApprovedTrackStateLabelSet.approved(
        source_binding_sha256="c" * 64,
        labels={1: "DRY_STABLE", 2: "DRY_STABLE"},
        reviewer_id="fixture-human-reviewer",
        reviewed_at_utc="2026-08-08T00:00:00Z",
        method="MANUAL_REPLAY_REVIEW",
        evidence_artifact_sha256="d" * 64,
        human_attestation=HUMAN_TRACK_STATE_ATTESTATION,
    )

    with pytest.raises(ConditionCohortError, match="not bound"):
        _build_condition_cohort_samples(
            _normalized_samples(SourceKind.SDK_LIVE),
            input_kind="collector",
            input_evidence=evidence,
            track_context=_track_context(evidence),
            tick_rate_hz=TICK_RATE_HZ,
            target_lap_ordinal=1,
            track_state_labels=wrong_labels,
            config=ConditionCohortConfig(min_matched_laps=2),
        )


def test_public_api_rejects_non_active_or_forged_validated_runs():
    with pytest.raises(ConditionCohortError, match="open validated telemetry adapter"):
        build_condition_cohort(object(), target_lap_ordinal=1)  # type: ignore[arg-type]

    evidence = _evidence(SourceKind.IBT_OFFLINE)
    assert isinstance(evidence, IbtInputEvidence)
    forged = ValidatedIbtRun(
        evidence,
        _normalized_samples(SourceKind.IBT_OFFLINE),
        stale_after_s=0.5,
        opponent_error_policy="degrade",
        _token=adapters_module._VALIDATED_RUN_TOKEN,
    )
    with pytest.raises(ConditionCohortError, match="open validated telemetry adapter"):
        build_condition_cohort(forged, target_lap_ordinal=1)


def test_public_audi_sample_waits_without_fabricating_condition_evidence():
    path = Path("data/raw/audir8lmsevo2gt3_spa up.ibt")
    if not path.exists():
        pytest.skip("frozen public Audi IBT is not present")

    manifest = json.loads(Path("data/public_sources.json").read_text(encoding="utf-8"))
    asset = manifest["assets"][0]
    expected = asset["provisional_condition_cohort_receipt"]

    with open_ibt_telemetry(
        path,
        source_id="public-audi-r8-evo2-spa",
        session_id="public-fixture-2023-12-race",
    ) as run:
        payload = build_condition_cohort(run, target_lap_ordinal=11)

    for key in (
        "condition_cohort_sha256",
        "condition_config_sha256",
        "condition_provenance_sha256",
        "condition_semantic_sha256",
        "contract_version",
        "matched_lap_ordinals",
        "normalized_input_receipt",
        "pipeline",
        "quality_gate",
        "readiness_status",
        "target_lap_ordinal",
        "trusted_readiness_status",
    ):
        assert payload[key] == expected[key]
    assert payload["series_evidence"]["clean_lap_count"] == expected["clean_lap_count"]
    assert [item["lap_ordinal"] for item in payload["lap_conditions"]] == expected[
        "clean_lap_ordinals"
    ]
    assert payload["capabilities"]["track_state_authenticity"] == expected[
        "track_state_authenticity"
    ]
    assert payload["capabilities"]["current_tire_wear"]["status"] == "SKIP"
    assert payload["capabilities"]["traffic_model"]["status"] == "SKIP"
    assert payload["recommendations"] == []
