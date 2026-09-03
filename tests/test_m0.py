from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from iracing_ai_engineer import m0 as m0_module
from iracing_ai_engineer.adapters import open_collector_jsonl
from iracing_ai_engineer.cli import _event_replay_payload
from iracing_ai_engineer.collector import (
    CollectorSample,
    collect_samples_to_jsonl,
)
from iracing_ai_engineer.events import process_telemetry_events
from iracing_ai_engineer.fuel import FuelScenario
from iracing_ai_engineer.m0 import (
    M0AcceptanceError,
    _validate_model_output,
    accept_m0,
    validate_performance_receipt,
)
from iracing_ai_engineer.model_replay import build_fuel_model_replay
from iracing_ai_engineer.sdk_probe import RawSdkFrame, VariableDescriptor

INSTALLER_SHA = "a" * 64
LAUNCHER_SHA = "b" * 64
REQUIREMENTS_SHA = "c" * 64
WHEEL_SHA = "d" * 64
WHEELHOUSE_MANIFEST_SHA = "e" * 64


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _rehash_model_payload(payload: dict[str, object]) -> None:
    binding = {key: value for key, value in payload.items() if key != "fuel_replay_sha256"}
    encoded = json.dumps(
        binding,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["fuel_replay_sha256"] = hashlib.sha256(encoded).hexdigest()


def _rehash_event_receipt(receipt: dict[str, object]) -> None:
    binding = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    encoded = json.dumps(
        binding,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receipt["receipt_sha256"] = hashlib.sha256(encoded).hexdigest()


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


def _frame(index: int) -> dict[str, object]:
    lap_length_ticks = 200
    lap = 1 + index // lap_length_ticks
    return {
        "SessionNum": 1,
        "SessionTick": 10_000 + index,
        "SessionTime": index / 60.0,
        "Lap": lap,
        "LapCompleted": lap - 1,
        "LapDistPct": (index % lap_length_ticks) / lap_length_ticks,
        "Speed": 50.0,
        "FuelLevel": 60.0 - index * (4.0 / lap_length_ticks),
        "OnPitRoad": False,
        "PlayerCarInPitStall": False,
        "PlayerTrackSurface": 3,
    }


def _scenario() -> FuelScenario:
    return FuelScenario(
        current_fuel_l=20.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
        reserve_l=1.0,
        minimum_valid_laps=5,
    )


def _release_arguments() -> dict[str, str]:
    return {
        "expected_installer_sha256": INSTALLER_SHA,
        "expected_launcher_sha256": LAUNCHER_SHA,
        "expected_requirements_sha256": REQUIREMENTS_SHA,
        "expected_wheel_sha256": WHEEL_SHA,
        "expected_wheelhouse_manifest_sha256": WHEELHOUSE_MANIFEST_SHA,
    }


def _live_inputs(tmp_path: Path, *, drop_at: int | None = None) -> tuple[Path, Path, Path]:
    capture = tmp_path / "live.jsonl"
    descriptors = _descriptors()
    samples = (
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=20_000 + index + (1 if drop_at is not None and index >= drop_at else 0),
                session_info_update=1,
                values=_frame(index),
                sim_mode_raw="full",
                captured_monotonic_s=index / 60.0,
            ),
            descriptors=descriptors,
            tick_rate_hz=60,
            session_info={"WeekendInfo": {"SimMode": "full"}},
        )
        for index in range(9 * 200 + 1)
    )
    collector_receipt = collect_samples_to_jsonl(
        samples,
        capture,
        source_id="windows-fixture",
        session_id="m0-session",
    )

    install = tmp_path / "install-manifest.json"
    _write_json(
        install,
        {
            "binary_wheels_only": True,
            "cli_help": "PASS",
            "import_smoke": "PASS",
            "input_hashes_stable": "PASS",
            "install_contract_version": "windows-collector-install-v3",
            "installed_at_utc": "2026-08-08T00:00:00.0000000Z",
            "installed_packages_file": "installed-packages.txt",
            "installed_packages_sha256": "1" * 64,
            "installer_file": "install_collector.ps1",
            "installer_sha256": INSTALLER_SHA,
            "launcher_file": "run_live_collector.ps1",
            "launcher_sha256": LAUNCHER_SHA,
            "live_capture_validated": False,
            "package_index_disabled": True,
            "pip_check": "PASS",
            "python_architecture": "AMD64",
            "python_bits": 64,
            "python_version": "3.12.11",
            "requirements_file": "windows-runtime-requirements.txt",
            "requirements_sha256": REQUIREMENTS_SHA,
            "wheel_file": "iracing_ai_engineer-0.1.0-py3-none-any.whl",
            "wheel_sha256": WHEEL_SHA,
            "wheelhouse_contract_version": "windows-wheelhouse-manifest-v1",
            "wheelhouse_manifest_file": "windows-wheelhouse-manifest.json",
            "wheelhouse_manifest_sha256": WHEELHOUSE_MANIFEST_SHA,
            "wheelhouse_target": "cp312-cp312-win_amd64",
            "wheelhouse_total_bytes": 85_000_000,
            "wheelhouse_wheel_count": 15,
        },
    )

    launch = tmp_path / "live.jsonl.launch.json"
    _write_json(
        launch,
        {
            "capture_byte_size": capture.stat().st_size,
            "capture_file": capture.name,
            "capture_sha256": _sha256(capture),
            "collector_elapsed_seconds": 30.0,
            "collector_process_receipt": collector_receipt.to_dict(),
            "completed_at_utc": "2026-08-08T00:00:30Z",
            "contract_version": "windows-live-launch-v1",
            "install_contract_version": "windows-collector-install-v3",
            "install_manifest_file": install.name,
            "install_manifest_sha256": _sha256(install),
            "launcher_file": "run_live_collector.ps1",
            "launcher_sha256": LAUNCHER_SHA,
            "poll_seconds": 0.01,
            "requested_duration_seconds": 30.0,
            "session_id": "m0-session",
            "sim_process_id": 1234,
            "sim_session_id": 1,
            "source_id": "windows-fixture",
            "source_kind_request": "live",
            "stale_after_seconds": 0.5,
            "started_at_utc": "2026-08-08T00:00:00Z",
            "wait_seconds": 0.0,
            "wheel_sha256": WHEEL_SHA,
            "windows_session_id": 1,
        },
    )
    return capture, launch, install


def _performance_inputs(
    tmp_path: Path,
    capture_sha: str,
    *,
    sidecar_frame_time_ms: float = 10.2,
    maximum_regression_pct: float = 5.0,
) -> Path:
    scenario = tmp_path / "performance-scenario.json"
    _write_json(
        scenario,
        {"car": "Audi R8 LMS EVO II GT3", "track": "Spa", "weather": "fixed"},
    )
    scenario_sha = _sha256(scenario)
    artifacts: dict[str, Path] = {}
    for role, frame_time in (("baseline", 10.0), ("sidecar", sidecar_frame_time_ms)):
        path = tmp_path / f"{role}-frame-times.json"
        _write_json(
            path,
            {
                "contract_version": "sim-frame-time-series-v1",
                "frame_times_ms": [frame_time] * 300,
                "measurement_tool": "PresentMon-normalized",
                "role": role,
                "scenario_id": "spa-fixed-grid-v1",
                "scenario_sha256": scenario_sha,
            },
        )
        artifacts[role] = path
    receipt = tmp_path / "performance.json"
    _write_json(
        receipt,
        {
            "baseline": {
                "artifact_file": artifacts["baseline"].name,
                "artifact_sha256": _sha256(artifacts["baseline"]),
            },
            "contract_version": "sim-performance-ab-v2",
            "max_p95_regression_pct": maximum_regression_pct,
            "measurement_tool": "PresentMon-normalized",
            "scenario_file": scenario.name,
            "scenario_id": "spa-fixed-grid-v1",
            "scenario_sha256": scenario_sha,
            "sidecar": {
                "artifact_file": artifacts["sidecar"].name,
                "artifact_sha256": _sha256(artifacts["sidecar"]),
            },
            "telemetry_capture_sha256": capture_sha,
        },
    )
    return receipt


def test_m0_replays_real_collector_twice_and_waits_for_external_performance(
    tmp_path: Path,
):
    capture, launch, install = _live_inputs(tmp_path)

    payload = accept_m0(
        capture,
        launch_receipt_path=launch,
        install_manifest_path=install,
        scenario=_scenario(),
        **_release_arguments(),
    )

    assert payload["overall_gate"] == {
        "reasons": ["EXTERNAL_FIXED_SCENARIO_AB_RECEIPT_MISSING"],
        "status": "WAIT_PERFORMANCE",
    }
    assert payload["capture_gate"]["status"] == "PASS"
    assert payload["privacy_gate"]["status"] == "PASS"
    assert payload["event_replay"]["status"] == "PASS"
    assert payload["fuel_model_replay"]["traversal_status"] == "PASS"
    assert payload["fuel_model_replay"]["readiness_status"] == "PASS"
    assert payload["shared_pipeline_gate"]["status"] == "PASS"
    assert (
        payload["shared_pipeline_gate"]["normalized_input_receipt"]
        == payload["fuel_model_replay"]["normalized_input_receipt"]
    )
    assert payload["event_replay"]["fresh_process_count"] == 2
    assert payload["fuel_model_replay"]["fresh_process_count"] == 2
    assert len(set(payload["event_replay"]["python_hash_probes"].values())) == 2
    assert len(set(payload["fuel_model_replay"]["python_hash_probes"].values())) == 2


def test_m0_rejects_self_rehashed_fuel_event_receipt_that_differs_from_events(
    monkeypatch, tmp_path: Path
):
    capture, launch, install = _live_inputs(tmp_path)
    with open_collector_jsonl(capture) as run:
        evidence = run.evidence
        events, event_receipt = process_telemetry_events(run.samples)
    event_payload = _event_replay_payload(
        input_kind="collector",
        input_evidence=evidence.to_dict(),
        stale_after_s=0.5,
        event_receipt=event_receipt.to_dict(),
        events=None,
    )
    with open_collector_jsonl(capture) as run:
        model_payload = build_fuel_model_replay(run, scenario=_scenario())
    model_payload["event_receipt"]["accepted_sample_count"] -= 1
    model_payload["event_receipt"]["rejected_sample_count"] += 1
    _rehash_event_receipt(model_payload["event_receipt"])
    _rehash_model_payload(model_payload)

    outputs = iter(
        (
            (event_payload, "1" * 64, {"1": 1, "987654": 2}),
            (model_payload, "2" * 64, {"1": 1, "987654": 2}),
        )
    )
    monkeypatch.setattr(m0_module, "_double_replay", lambda *args, **kwargs: next(outputs))

    with pytest.raises(M0AcceptanceError, match="event receipts do not match"):
        accept_m0(
            capture,
            launch_receipt_path=launch,
            install_manifest_path=install,
            scenario=_scenario(),
            **_release_arguments(),
        )


def test_m0_pass_requires_raw_performance_artifacts(tmp_path: Path):
    capture, launch, install = _live_inputs(tmp_path)
    performance = _performance_inputs(tmp_path, _sha256(capture))

    payload = accept_m0(
        capture,
        launch_receipt_path=launch,
        install_manifest_path=install,
        performance_receipt_path=performance,
        scenario=_scenario(),
        **_release_arguments(),
    )

    assert payload["overall_gate"] == {"reasons": [], "status": "PASS"}
    assert payload["performance_gate"]["status"] == "PASS"
    assert payload["performance_gate"]["baseline"]["sample_count"] == 300
    assert payload["performance_gate"]["sidecar"]["p95_frame_time_ms"] == 10.2


def test_m0_independently_rejects_rehashed_v2_capability_and_recommendation_tampering(
    tmp_path: Path,
):
    capture, _, _ = _live_inputs(tmp_path)
    with open_collector_jsonl(capture) as run:
        payload = build_fuel_model_replay(run, scenario=_scenario())
        evidence = run.evidence

    missing_capabilities = json.loads(json.dumps(payload))
    del missing_capabilities["capabilities"]["current_tire_wear"]
    del missing_capabilities["capabilities"]["opponent_fuel"]
    del missing_capabilities["capabilities"]["traffic_model"]
    _rehash_model_payload(missing_capabilities)
    with pytest.raises(M0AcceptanceError, match="capabilities.*keys are invalid"):
        _validate_model_output(
            missing_capabilities,
            evidence=evidence,
            scenario=_scenario(),
            stale_after_s=0.5,
        )

    elevated_recommendation = json.loads(json.dumps(payload))
    elevated_recommendation["recommendations"][0]["confidence"] = "HIGH"
    del elevated_recommendation["recommendations"][0]["confidence_basis"]
    _rehash_model_payload(elevated_recommendation)
    with pytest.raises(M0AcceptanceError, match="fuel recommendation keys are invalid"):
        _validate_model_output(
            elevated_recommendation,
            evidence=evidence,
            scenario=_scenario(),
            stale_after_s=0.5,
        )


def test_performance_claim_fails_if_raw_artifact_changes(tmp_path: Path):
    capture, _, _ = _live_inputs(tmp_path)
    performance_path = _performance_inputs(tmp_path, _sha256(capture))
    performance = json.loads(performance_path.read_text(encoding="utf-8"))
    (tmp_path / "sidecar-frame-times.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(M0AcceptanceError, match="artifact SHA does not match"):
        validate_performance_receipt(
            performance,
            telemetry_capture_sha256=_sha256(capture),
            artifact_directory=tmp_path,
        )


def test_m0_rejects_lowered_threshold_and_unapproved_release(tmp_path: Path):
    capture, launch, install = _live_inputs(tmp_path)

    with pytest.raises(M0AcceptanceError, match="cannot be below"):
        accept_m0(
            capture,
            launch_receipt_path=launch,
            install_manifest_path=install,
            scenario=_scenario(),
            minimum_capture_s=1.0,
            **_release_arguments(),
        )

    unapproved = _release_arguments()
    unapproved["expected_wheel_sha256"] = "9" * 64
    with pytest.raises(M0AcceptanceError, match="approved release hashes"):
        accept_m0(
            capture,
            launch_receipt_path=launch,
            install_manifest_path=install,
            scenario=_scenario(),
            **unapproved,
        )


def test_m0_performance_failure_outranks_wait_data(tmp_path: Path):
    capture, launch, install = _live_inputs(tmp_path)
    performance = _performance_inputs(
        tmp_path,
        _sha256(capture),
        sidecar_frame_time_ms=11.0,
    )

    payload = accept_m0(
        capture,
        launch_receipt_path=launch,
        install_manifest_path=install,
        performance_receipt_path=performance,
        scenario=replace(_scenario(), minimum_valid_laps=20),
        **_release_arguments(),
    )

    assert payload["overall_gate"] == {
        "reasons": [
            "INSUFFICIENT_VALID_FUEL_LAPS",
            "P95_FRAME_TIME_REGRESSION_EXCEEDED",
        ],
        "status": "FAIL_PERFORMANCE",
    }


def test_m0_emits_fail_receipt_for_quality_drop(tmp_path: Path):
    capture, launch, install = _live_inputs(tmp_path, drop_at=1_000)

    payload = accept_m0(
        capture,
        launch_receipt_path=launch,
        install_manifest_path=install,
        scenario=_scenario(),
        **_release_arguments(),
    )

    assert payload["overall_gate"]["status"] == "FAIL"
    assert "DROPPED_TICKS" in payload["overall_gate"]["reasons"]
    assert payload["event_replay"]["status"] == "FAIL"
    assert payload["event_replay"]["quality_gate"]["status"] == "DEGRADED"
