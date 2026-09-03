from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace

import pytest

import iracing_ai_engineer.adapters as adapters_module
from iracing_ai_engineer import cli
from iracing_ai_engineer.adapters import (
    CollectorInputEvidence,
    IbtInputEvidence,
    ValidatedCollectorRun,
    ValidatedIbtRun,
    open_collector_jsonl,
)
from iracing_ai_engineer.collector import CollectorSample, collect_samples_to_jsonl
from iracing_ai_engineer.fuel import FuelScenario
from iracing_ai_engineer.model_replay import (
    _build_fuel_model_replay_samples,
    build_fuel_model_replay,
)
from iracing_ai_engineer.sdk_probe import RawSdkFrame, VariableDescriptor
from iracing_ai_engineer.telemetry import SourceKind, TelemetrySample, normalize_sdk_frame


def _frame(index: int, *, include_fuel: bool = True) -> dict[str, object]:
    lap_length_ticks = 60
    lap = 1 + index // lap_length_ticks
    values: dict[str, object] = {
        "SessionNum": 1,
        "SessionTick": 10_000 + index,
        "SessionTime": index / 60.0,
        "Lap": lap,
        "LapCompleted": lap - 1,
        "LapDistPct": (index % lap_length_ticks) / lap_length_ticks,
        "Speed": 50.0,
        "OnPitRoad": False,
        "PlayerCarInPitStall": False,
        "PlayerTrackSurface": 3,
    }
    if include_fuel:
        values["FuelLevel"] = 60.0 - index * (4.0 / lap_length_ticks)
    return values


def _normalized_samples(
    source_kind: SourceKind,
    *,
    include_fuel: bool = True,
) -> Iterator[TelemetrySample]:
    previous = None
    for index in range(9 * 60 + 1):
        sample = normalize_sdk_frame(
            _frame(index, include_fuel=include_fuel),
            source_id=f"fixture-{source_kind.value.casefold()}",
            session_id="shared-model-session",
            source_kind=source_kind,
            buffer_tick=20_000 + index,
            captured_monotonic_s=(
                index / 60.0 if source_kind is SourceKind.SDK_LIVE else None
            ),
            previous=previous,
        )
        yield sample
        previous = sample


def _scenario() -> FuelScenario:
    return FuelScenario(
        current_fuel_l=20.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
        reserve_l=1.0,
        minimum_valid_laps=5,
    )


def _input_evidence(
    source_kind: SourceKind,
) -> IbtInputEvidence | CollectorInputEvidence:
    if source_kind is SourceKind.IBT_OFFLINE:
        return IbtInputEvidence(
            source_id="fixture-ibt_offline",
            session_id="shared-model-session",
            source_sha256="a" * 64,
            byte_size=123_456,
            record_count=9 * 60 + 1,
            tick_rate_hz=60,
        )
    return CollectorInputEvidence(
        source_id="fixture-sdk_live",
        session_id="shared-model-session",
        source_kind=SourceKind.SDK_LIVE,
        sim_mode="full",
        completion_status="COMPLETE",
        semantic_record_count=9 * 60 + 4,
        records_sha256="b" * 64,
        frame_record_count=9 * 60 + 1,
        event_record_count=0,
        schema_record_count=1,
        session_info_record_count=1,
        samples_seen=9 * 60 + 1,
        duplicate_sample_count=0,
        duplicate_conflict_count=0,
        dropped_tick_count=0,
        stale_event_count=0,
        session_reset_count=0,
        schema_change_count=0,
        schema_epoch_count=1,
        session_epoch_count=1,
        first_buffer_tick=20_000,
        last_buffer_tick=20_000 + 9 * 60,
        tick_rate_hz_values=(60,),
    )


def test_equivalent_ibt_and_live_samples_share_fuel_model_semantics():
    ibt = _build_fuel_model_replay_samples(
        _normalized_samples(SourceKind.IBT_OFFLINE),
        input_kind="ibt",
        input_evidence=_input_evidence(SourceKind.IBT_OFFLINE),
        tick_rate_hz=60,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
        scenario=_scenario(),
    )
    live = _build_fuel_model_replay_samples(
        _normalized_samples(SourceKind.SDK_LIVE),
        input_kind="collector",
        input_evidence=_input_evidence(SourceKind.SDK_LIVE),
        tick_rate_hz=60,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
        scenario=_scenario(),
    )

    assert ibt["quality_gate"] == live["quality_gate"] == {
        "reasons": [],
        "status": "PASS",
    }
    assert ibt["capabilities"]["fuel_model_shadow"]["status"] == "PASS"
    assert live["capabilities"]["fuel_model_shadow"]["status"] == "PASS"
    assert ibt["model_output"] == live["model_output"]
    assert ibt["lap_receipt"] == live["lap_receipt"]
    assert ibt["model_semantic_sha256"] == live["model_semantic_sha256"]
    assert ibt["fuel_replay_sha256"] != live["fuel_replay_sha256"]
    assert all(not item["executable"] for item in live["recommendations"])
    assert live["capabilities"]["opponent_fuel"] == {
        "blocked_claims": ["OPPONENT_FUEL_CLAIM"],
        "confidence": "NONE",
        "contract_version": "inference-capability-v1",
        "estimate_available": False,
        "provenance": "UNKNOWN",
        "reasons": ["OPPONENT_FUEL_NOT_EXPOSED_BY_SDK"],
        "status": "SKIP",
    }
    assert live["capabilities"]["current_tire_wear"]["provenance"] == "UNKNOWN"
    assert live["capabilities"]["traffic_model"]["provenance"] == "UNKNOWN"
    assert live["recommendations"][0]["confidence"] == "LOW"
    assert live["recommendations"][0]["confidence_basis"] == {
        "historical_burn_stability": "HIGH",
        "overall_plan": "LOW_BECAUSE_EVENT_RULES_AND_TRAFFIC_ARE_UNAVAILABLE",
        "scenario_inputs": "USER_RULE",
    }


def test_missing_required_fuel_channel_fails_closed_without_a_plan():
    payload = _build_fuel_model_replay_samples(
        _normalized_samples(SourceKind.SDK_LIVE, include_fuel=False),
        input_kind="collector",
        input_evidence=_input_evidence(SourceKind.SDK_LIVE),
        tick_rate_hz=60,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
        scenario=_scenario(),
    )

    assert payload["model_output"] is None
    assert payload["recommendations"] == []
    assert payload["capabilities"]["fuel_model_shadow"]["status"] == "FAIL"
    assert "MISSING_REQUIRED_CHANNEL:FuelLevel" in payload["quality_gate"][
        "reasons"
    ]


def test_public_model_api_rejects_samples_and_evidence_outside_adapter_run():
    with pytest.raises(ValueError, match="open validated telemetry adapter"):
        build_fuel_model_replay(
            object(),  # type: ignore[arg-type]
            scenario=_scenario(),
        )

    evidence = _input_evidence(SourceKind.IBT_OFFLINE)
    assert isinstance(evidence, IbtInputEvidence)
    with pytest.raises(TypeError, match="only be created"):
        ValidatedIbtRun(
            evidence,
            _normalized_samples(SourceKind.SDK_LIVE),
            stale_after_s=0.5,
            opponent_error_policy="degrade",
            _token=object(),
        )


def test_normalized_receipt_not_caller_raw_hash_anchors_model_evidence_ids():
    first_evidence = _input_evidence(SourceKind.IBT_OFFLINE)
    assert isinstance(first_evidence, IbtInputEvidence)
    second_evidence = replace(first_evidence, source_sha256="d" * 64)
    first = _build_fuel_model_replay_samples(
        _normalized_samples(SourceKind.IBT_OFFLINE),
        input_kind="ibt",
        input_evidence=first_evidence,
        tick_rate_hz=60,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
        scenario=_scenario(),
    )
    second = _build_fuel_model_replay_samples(
        _normalized_samples(SourceKind.IBT_OFFLINE),
        input_kind="ibt",
        input_evidence=second_evidence,
        tick_rate_hz=60,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
        scenario=_scenario(),
    )

    assert first["normalized_input_receipt"] == second["normalized_input_receipt"]
    assert first["recommendations"][0]["evidence_ids"] == second["recommendations"][0][
        "evidence_ids"
    ]
    assert first["fuel_replay_sha256"] != second["fuel_replay_sha256"]


def test_normalized_drop_blocks_capability_and_shadow_plan_for_both_inputs():
    samples = list(_normalized_samples(SourceKind.IBT_OFFLINE))
    gap_index = 120
    gap_frame = _frame(gap_index)
    gap_frame["SessionTick"] = int(gap_frame["SessionTick"]) + 1
    samples[gap_index] = normalize_sdk_frame(
        gap_frame,
        source_id="fixture-ibt_offline",
        session_id="shared-model-session",
        source_kind=SourceKind.IBT_OFFLINE,
        buffer_tick=20_000 + gap_index,
        previous=samples[gap_index - 1],
    )
    for index in range(gap_index + 1, len(samples)):
        frame = _frame(index)
        frame["SessionTick"] = int(frame["SessionTick"]) + 1
        samples[index] = normalize_sdk_frame(
            frame,
            source_id="fixture-ibt_offline",
            session_id="shared-model-session",
            source_kind=SourceKind.IBT_OFFLINE,
            buffer_tick=20_000 + index,
            previous=samples[index - 1],
        )

    payload = _build_fuel_model_replay_samples(
        samples,
        input_kind="ibt",
        input_evidence=_input_evidence(SourceKind.IBT_OFFLINE),
        tick_rate_hz=60,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
        scenario=_scenario(),
    )

    assert payload["model_output"]["status"] == "ready"
    assert payload["quality_gate"] == {
        "reasons": ["DROPPED_TICKS"],
        "status": "DEGRADED",
    }
    assert payload["capabilities"]["fuel_model_shadow"]["status"] == "FAIL"
    assert payload["recommendations"] == []


def _descriptors() -> tuple[VariableDescriptor, ...]:
    fields = (
        ("SessionNum", 2, "int32", 0),
        ("SessionTick", 2, "int32", 4),
        ("SessionTime", 4, "float32", 8),
        ("Lap", 2, "int32", 12),
        ("LapCompleted", 2, "int32", 16),
        ("LapDistPct", 4, "float32", 20),
        ("Speed", 4, "float32", 24),
        ("FuelLevel", 4, "float32", 28),
        ("OnPitRoad", 1, "bool", 32),
        ("PlayerCarInPitStall", 1, "bool", 33),
        ("PlayerTrackSurface", 2, "int32", 36),
    )
    return tuple(
        VariableDescriptor(name, type_code, dtype, offset, 1, False, "", "")
        for name, type_code, dtype, offset in fields
    )


def test_fuel_replay_cli_runs_real_collector_adapter_and_model(
    capsys, tmp_path
):
    path = tmp_path / "shared-model.jsonl"
    descriptors = _descriptors()
    samples = (
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=20_000 + index,
                session_info_update=1,
                values=_frame(index),
                sim_mode_raw="full",
                captured_monotonic_s=index / 60.0,
            ),
            descriptors=descriptors,
            tick_rate_hz=60,
            session_info={"WeekendInfo": {"SimMode": "full"}},
        )
        for index in range(9 * 60 + 1)
    )
    collect_samples_to_jsonl(
        samples,
        path,
        source_id="windows-fixture",
        session_id="shared-model-session",
    )

    exit_code = cli.main(
        [
            "fuel-replay",
            str(path),
            "--current-fuel-l",
            "20",
            "--tank-capacity-l",
            "120",
            "--refuel-rate-lps",
            "2",
            "--remaining-laps",
            "10",
            "--require-ready",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert payload["input_kind"] == "collector"
    assert payload["input_evidence"]["source_id"] == "windows-fixture"
    assert payload["input_evidence"]["tick_rate_hz_values"] == [60]
    assert payload["input_evidence"]["capture_span_us"] == 9_000_000
    assert payload["model_output"]["status"] == "ready"
    assert payload["quality_gate"] == {"reasons": [], "status": "PASS"}


def test_fuel_replay_cli_require_ready_rejects_complete_capture_with_drop(
    capsys, tmp_path
):
    path = tmp_path / "shared-model-drop.jsonl"
    descriptors = _descriptors()
    samples = (
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=20_000 + index + (1 if index >= 120 else 0),
                session_info_update=1,
                values=_frame(index),
                sim_mode_raw="full",
                captured_monotonic_s=index / 60.0,
            ),
            descriptors=descriptors,
            tick_rate_hz=60,
            session_info={"WeekendInfo": {"SimMode": "full"}},
        )
        for index in range(9 * 60 + 1)
    )
    collect_samples_to_jsonl(
        samples,
        path,
        source_id="windows-fixture",
        session_id="shared-model-session",
    )

    exit_code = cli.main(
        [
            "fuel-replay",
            str(path),
            "--current-fuel-l",
            "20",
            "--tank-capacity-l",
            "120",
            "--refuel-rate-lps",
            "2",
            "--remaining-laps",
            "10",
            "--require-ready",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 5
    assert payload["input_evidence"]["dropped_tick_count"] == 1
    assert payload["quality_gate"] == {
        "reasons": ["DROPPED_TICKS"],
        "status": "DEGRADED",
    }
    assert payload["capabilities"]["fuel_model_shadow"]["status"] == "FAIL"
    assert payload["recommendations"] == []

    with open_collector_jsonl(path) as run:
        forged = replace(run.evidence, dropped_tick_count=0)
        with pytest.raises(TypeError, match="only be created"):
            ValidatedCollectorRun(
                forged,
                run.samples,
                stale_after_s=run.stale_after_s,
                opponent_error_policy=run.opponent_error_policy,
                _token=object(),
            )
        forged_run = ValidatedCollectorRun(
            forged,
            run.samples,
            stale_after_s=run.stale_after_s,
            opponent_error_policy=run.opponent_error_policy,
            _token=adapters_module._VALIDATED_RUN_TOKEN,
        )
        with pytest.raises(ValueError, match="open validated telemetry adapter"):
            build_fuel_model_replay(forged_run, scenario=_scenario())

        object.__setattr__(run, "_evidence", forged)
        protected = build_fuel_model_replay(run, scenario=_scenario())
        assert protected["quality_gate"]["status"] == "DEGRADED"
        assert protected["capabilities"]["fuel_model_shadow"]["status"] == "FAIL"
        assert protected["recommendations"] == []


def test_fuel_replay_cli_requires_ibt_identity(capsys):
    exit_code = cli.main(
        [
            "fuel-replay",
            "fixture.ibt",
            "--current-fuel-l",
            "20",
            "--tank-capacity-l",
            "120",
            "--refuel-rate-lps",
            "2",
            "--remaining-laps",
            "10",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "requires --source-id and --session-id" in captured.err
    assert json.loads(captured.out)["error"] == "FUEL_MODEL_REPLAY_ERROR"
