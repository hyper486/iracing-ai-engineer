from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import irsdk
import pytest

from iracing_ai_engineer import cli, live_preflight
from iracing_ai_engineer.collector import CollectorConsistencyError
from iracing_ai_engineer.live_preflight import (
    LIVE_PREFLIGHT_CONTRACT_VERSION,
    LivePreflightError,
    PreflightEvidenceClass,
    _analyze_snapshot,
    _base_payload,
    _policy,
    _sealed_capture_snapshot,
    _simulator_identity,
    _validate_receipt_against_analysis,
    _with_receipt_hash,
    _with_semantic_digest,
    run_live_preflight_transport,
)
from iracing_ai_engineer.sdk_probe import (
    SDK_TYPE_NAMES,
    SDK_TYPE_SIZES,
    RawSdkFrame,
    SdkProbeUnavailable,
    VariableDescriptor,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _frame_values(index: int, *, in_car: bool = True) -> dict[str, object]:
    tick = 1_000 + index
    return {
        "SessionTime": 10.0 + index / 60.0,
        "SessionTick": tick,
        "SessionNum": 1,
        "Lap": 1,
        "LapDistPct": 0.2 + index / 6_000.0,
        "Speed": 50.0,
        "Throttle": 0.5,
        "Brake": 0.0,
        "SteeringWheelAngle": 0.0,
        "FuelLevel": 50.0 - index / 10_000.0,
        "OnPitRoad": False,
        "PlayerCarInPitStall": False,
        "PlayerTrackSurface": 3,
        "IsOnTrack": in_car,
        "IsOnTrackCar": in_car,
        "PlayerCarMyIncidentCount": 0,
    }


def _descriptors(values: dict[str, object]) -> tuple[VariableDescriptor, ...]:
    result: list[VariableDescriptor] = []
    offset = 0
    for name, value in values.items():
        if type(value) is bool:
            type_code = 1
        elif type(value) is int:
            type_code = 2
        elif type(value) is float:
            type_code = 4
        else:  # pragma: no cover - fixture invariant
            raise AssertionError(type(value))
        result.append(
            VariableDescriptor(
                name=name,
                type_code=type_code,
                dtype=SDK_TYPE_NAMES[type_code],
                offset=offset,
                count=1,
                count_as_time=False,
                unit="",
                description=name,
            )
        )
        offset += SDK_TYPE_SIZES[type_code]
    return tuple(result)


class SyntheticTransport:
    def __init__(
        self,
        *,
        track_length: str | None = "7.004 km",
        dropped_tick_at: int | None = None,
        final_in_car: bool = True,
        capture_interval_s: float = 1 / 60,
    ) -> None:
        self.index = 0
        self.closed = False
        self.frames: list[RawSdkFrame] = []
        buffer_tick = 5_000
        for index in range(61):
            if index:
                buffer_tick += 2 if index == dropped_tick_at else 1
            self.frames.append(
                RawSdkFrame(
                    buffer_tick=buffer_tick,
                    session_info_update=1,
                    values=_frame_values(
                        index,
                        in_car=final_in_car or index < 60,
                    ),
                    captured_monotonic_s=index * capture_interval_s,
                )
            )
        self.schema = _descriptors(self.frames[0].values)
        weekend: dict[str, object] = {"SimMode": "full"}
        if track_length is not None:
            weekend["TrackLength"] = track_length
        self.session_info = {
            "WeekendInfo": weekend,
            "DriverInfo": {"Drivers": [{"UserName": "Synthetic Private Person"}]},
        }

    def startup(self, timeout_s: float) -> SimpleNamespace:
        assert timeout_s == 0.0
        return SimpleNamespace(tick_rate_hz=60)

    def descriptors(self) -> tuple[VariableDescriptor, ...]:
        return self.schema

    def read_frozen(self, fields: tuple[str, ...]) -> RawSdkFrame:
        assert fields == tuple(item.name for item in self.schema)
        frame = self.frames[self.index]
        self.index += 1
        return frame

    def sim_mode(self) -> tuple[str, int]:
        return "full", 1

    def session_info_snapshot(self):
        return self.session_info, 1

    @property
    def connected(self) -> bool:
        return not self.closed and self.index < len(self.frames)

    def close(self) -> None:
        self.closed = True


class SessionInfoDriftTransport(SyntheticTransport):
    def session_info_snapshot(self):
        snapshot = deepcopy(self.session_info)
        snapshot["WeekendInfo"]["TrackName"] = (
            "Before" if self.index <= 30 else "After"
        )
        return snapshot, 1


class BenignDuplicateTransport(SyntheticTransport):
    def __init__(self) -> None:
        super().__init__()
        self.frames.insert(31, self.frames[30])


def _run_synthetic(path: Path, transport: SyntheticTransport | None = None):
    actual_transport = transport or SyntheticTransport()
    path.parent.mkdir(parents=True, exist_ok=True)
    return run_live_preflight_transport(
        actual_transport,
        path,
        source_id="synthetic-preflight-source",
        session_id="synthetic-preflight-session",
        wait_seconds=0.0,
        duration_s=1.0,
        poll_seconds=0.01,
        stale_after_s=0.5,
        max_reads=len(actual_transport.frames),
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )


def _rehash(receipt: dict[str, object]) -> dict[str, object]:
    semantic = dict(receipt)
    semantic.pop("receipt_sha256", None)
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(semantic)).hexdigest()
    return receipt


def test_synthetic_canary_exercises_full_pipeline_but_can_never_start_live_capture(
    tmp_path: Path,
):
    output = tmp_path / "preflight-synthetic.jsonl"

    receipt = _run_synthetic(output)

    assert receipt["contract_version"] == LIVE_PREFLIGHT_CONTRACT_VERSION
    assert receipt["evidence_class"] == "SYNTHETIC_TEST_ONLY"
    assert receipt["status"] == "SYNTHETIC_TEST_ONLY"
    assert receipt["would_pass_real_gates"] is True
    assert receipt["can_start_live_capture"] is False
    assert receipt["live_acceptance_eligible"] is False
    assert receipt["production_transport_attested"] is False
    assert receipt["advisor_only"] is True
    assert receipt["vehicle_control_enabled"] is False
    assert receipt["advice_generated"] is False
    assert receipt["recommendations"] == []
    assert {gate["status"] for gate in receipt["gates"].values()} == {"PASS"}
    assert receipt["collector_evidence"]["source_kind"] == "SDK_LIVE"
    assert receipt["collector_evidence"]["dropped_tick_count"] == 0
    assert receipt["track_context"]["status"] == "VERIFIED"
    assert receipt["event_receipt"]["sample_count"] == 61
    assert receipt["event_receipt"] == receipt["driving_summary"]["event_receipt"]
    assert receipt["driving_summary"]["readiness_status"] == "WAIT_DRIVING_DATA"
    assert "Synthetic Private Person" not in output.read_text(encoding="utf-8")

    digest = receipt["receipt_sha256"]
    semantic = dict(receipt)
    del semantic["receipt_sha256"]
    assert digest == hashlib.sha256(_canonical(semantic)).hexdigest()


def test_same_update_full_session_info_drift_is_not_a_real_preflight_pass(
    tmp_path: Path,
):
    receipt = _run_synthetic(
        tmp_path / "preflight-session-info-drift.jsonl",
        SessionInfoDriftTransport(),
    )

    evidence = receipt["collector_evidence"]
    assert evidence["duplicate_sample_count"] == 0
    assert evidence["event_record_count"] == 1
    assert "UNEXPECTED_COLLECTOR_EVENTS" in receipt["gates"][
        "collector_quality"
    ]["reasons"]
    assert receipt["would_pass_real_gates"] is False


def test_benign_same_tick_duplicate_is_the_only_admitted_collector_event(
    tmp_path: Path,
):
    receipt = _run_synthetic(
        tmp_path / "preflight-benign-duplicate.jsonl",
        BenignDuplicateTransport(),
    )

    evidence = receipt["collector_evidence"]
    assert evidence["duplicate_sample_count"] == 1
    assert evidence["event_record_count"] == 1
    assert receipt["gates"]["collector_quality"]["status"] == "PASS"
    assert receipt["would_pass_real_gates"] is True


def test_supervisor_preflight_cap_plus_one_fails_before_record_and_cannot_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    capture = tmp_path / "preflight-cap-attack.jsonl"
    monkeypatch.setattr(live_preflight, "SUPERVISOR_PREFLIGHT_MAX_BYTES", 1)
    with capture.open("x+b", buffering=0) as handle:
        with pytest.raises(CollectorConsistencyError, match="max_output_bytes"):
            live_preflight._run_core_handle(
                SyntheticTransport(),
                handle,
                capture_filename=capture.name,
                source_id="cap-test",
                session_id="cap-test",
                simulator_identity={
                    "process_id": 1,
                    "start_time_utc_ticks": 2,
                    "windows_session_id": 1,
                },
                transport_attestation={"test": True},
                wait_seconds=0.0,
                duration_s=1.0,
                poll_seconds=0.01,
                stale_after_s=0.5,
                monotonic=lambda: 0.0,
                sleep=lambda _seconds: None,
            )
        assert os.fstat(handle.fileno()).st_size == 0
    assert not (tmp_path / "ready-cap-test.json").exists()


@pytest.mark.parametrize(
    ("transport", "gate", "reason"),
    [
        (
            SyntheticTransport(track_length=None),
            "full_session_context",
            "TRACK_LENGTH_UNAVAILABLE",
        ),
        (SyntheticTransport(dropped_tick_at=30), "collector_quality", "DROPPED_TICKS"),
        (
            SyntheticTransport(final_in_car=False),
            "capture_context",
            "FINAL_SAMPLE_NOT_IN_CAR",
        ),
    ],
)
def test_synthetic_canary_preserves_wait_reasons_without_promoting(
    tmp_path: Path,
    transport: SyntheticTransport,
    gate: str,
    reason: str,
):
    receipt = _run_synthetic(tmp_path / "preflight-blocked.jsonl", transport)

    assert receipt["status"] == "SYNTHETIC_TEST_ONLY"
    assert receipt["would_pass_real_gates"] is False
    assert receipt["can_start_live_capture"] is False
    assert reason in receipt["gates"][gate]["reasons"]


def test_synthetic_receipt_is_byte_deterministic_across_directories(tmp_path: Path):
    first = _run_synthetic(tmp_path / "one" / "preflight-same.jsonl")
    second = _run_synthetic(tmp_path / "two" / "preflight-same.jsonl")

    assert _canonical(first) == _canonical(second)
    assert (tmp_path / "one" / "preflight-same.jsonl").read_bytes() == (
        tmp_path / "two" / "preflight-same.jsonl"
    ).read_bytes()


def test_fixture_cannot_claim_real_sdk_precheck(tmp_path: Path):
    output = tmp_path / "preflight-forbidden.jsonl"

    with pytest.raises(TypeError, match="evidence_class"):
        run_live_preflight_transport(
            SyntheticTransport(),
            output,
            source_id="fixture",
            session_id="fixture",
            evidence_class=PreflightEvidenceClass.REAL_SDK_PRECHECK_ONLY,
        )

    assert not output.exists()


def test_synthetic_receipt_can_never_satisfy_capture_start_admission(tmp_path: Path):
    output = tmp_path / "preflight-synthetic-admission.jsonl"
    receipt = _run_synthetic(output)

    receipt = deepcopy(receipt)
    receipt.update(
        {
            "admission_recomputed": True,
            "can_start_live_capture": True,
            "evidence_class": "REAL_SDK_PRECHECK_ONLY",
            "production_transport_attested": True,
            "status": "PASS",
        }
    )
    receipt["production_semantic_digest"] = "0" * 64
    _rehash(receipt)

    assert not hasattr(live_preflight, "run_windows_live_preflight")
    assert not hasattr(live_preflight, "validate_live_preflight_start_receipt")
    with pytest.raises(LivePreflightError, match="fresh externally closed"):
        live_preflight._run_windows_live_preflight_cli_only(
            tmp_path / "preflight-cannot-promote.jsonl",
            source_id="aeis-precheck",
            session_id="cannot-promote",
            expected_sim_process_id=123,
            expected_sim_start_time_utc_ticks=456,
            expected_windows_session_id=1,
        )


def test_rehashed_synthetic_cannot_enter_real_through_public_api(tmp_path: Path):
    output = tmp_path / "preflight-validator.jsonl"
    receipt = deepcopy(_run_synthetic(output))
    receipt.update(
        {
            "admission_recomputed": True,
            "can_start_live_capture": True,
            "evidence_class": "REAL_SDK_PRECHECK_ONLY",
            "production_transport_attested": True,
            "status": "PASS",
        }
    )
    receipt["production_semantic_digest"] = "0" * 64
    _rehash(receipt)

    rerun = run_live_preflight_transport(
        SyntheticTransport(),
        tmp_path / "preflight-public-rerun.jsonl",
        source_id="synthetic-preflight-source",
        session_id="synthetic-preflight-session",
        wait_seconds=0.0,
        duration_s=1.0,
        max_reads=61,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )
    assert receipt["status"] == "PASS"
    assert rerun["status"] == "SYNTHETIC_TEST_ONLY"
    assert rerun["production_semantic_digest"] is None


@pytest.mark.parametrize(
    "name",
    ["live-20260823T010203Z.jsonl", "canary.jsonl", "preflight-.jsonl"],
)
def test_preflight_refuses_canonical_or_ambiguous_output_names(
    tmp_path: Path, name: str
):
    with pytest.raises(LivePreflightError, match="noncanonical"):
        _run_synthetic(tmp_path / name)


def test_production_entrypoint_is_cli_private_and_requires_isolated_runtime(
    tmp_path: Path,
):
    assert not hasattr(live_preflight, "run_windows_live_preflight")
    assert not hasattr(live_preflight, "validate_live_preflight_start_receipt")
    with pytest.raises(LivePreflightError, match="python -I -B"):
        live_preflight._run_windows_live_preflight_cli_only(
            tmp_path / "preflight-library-call.jsonl",
            source_id="aeis-precheck",
            session_id="library-call",
            expected_sim_process_id=123,
            expected_sim_start_time_utc_ticks=456,
            expected_windows_session_id=1,
        )


def _cli_receipt(status: str) -> dict[str, object]:
    return {
        "can_start_live_capture": status == "PASS",
        "contract_version": LIVE_PREFLIGHT_CONTRACT_VERSION,
        "evidence_class": "REAL_SDK_PRECHECK_ONLY",
        "live_acceptance_eligible": False,
        "receipt_sha256": "a" * 64,
        "status": status,
    }


def test_cli_turns_mid_capture_sdk_loss_into_fail_closed_execution_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    def fail(*_args, **_kwargs):
        raise SdkProbeUnavailable("SDK disappeared after output creation")

    monkeypatch.setattr(
        "iracing_ai_engineer.live_preflight._run_windows_live_preflight_cli_only",
        fail,
    )

    actual = cli.main(
        [
            "live-preflight",
            str(tmp_path / "preflight-cli-partial.jsonl"),
            "--source-id",
            "aeis-precheck",
            "--session-id",
            "preflight-cli-partial",
            "--expected-sim-process-id",
            "123",
            "--expected-sim-start-time-utc-ticks",
            "456",
            "--expected-windows-session-id",
            "1",
        ]
    )

    captured = capsys.readouterr()
    assert actual == 3
    assert json.loads(captured.out)["error"] == "LIVE_PREFLIGHT_ERROR"
    assert "SDK disappeared" in captured.err


@pytest.mark.parametrize(("status", "exit_code"), [("PASS", 0), ("WAIT", 5)])
def test_cli_maps_preflight_status_without_exposing_synthetic_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    exit_code: int,
):
    observed: dict[str, object] = {}

    def run(path, **kwargs):
        observed.update({"path": path, **kwargs})
        return _cli_receipt(status)

    monkeypatch.setattr(
        "iracing_ai_engineer.live_preflight._run_windows_live_preflight_cli_only",
        run,
    )
    output = tmp_path / "preflight-cli.jsonl"

    actual = cli.main(
        [
            "live-preflight",
            str(output),
            "--source-id",
            "aeis-precheck",
            "--session-id",
            "preflight-cli",
            "--expected-sim-process-id",
            "123",
            "--expected-sim-start-time-utc-ticks",
            "456",
            "--expected-windows-session-id",
            "1",
            "--wait-seconds",
            "3",
            "--duration-seconds",
            "30",
        ]
    )

    assert actual == exit_code
    assert json.loads(capsys.readouterr().out) == _cli_receipt(status)
    assert observed == {
        "path": output,
        "source_id": "aeis-precheck",
        "session_id": "preflight-cli",
        "expected_sim_process_id": 123,
        "expected_sim_start_time_utc_ticks": 456,
        "expected_windows_session_id": 1,
        "wait_seconds": 3.0,
        "duration_s": 30.0,
        "poll_seconds": 0.01,
        "stale_after_s": 0.5,
    }
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            [
                "live-preflight",
                str(output),
                "--source-id",
                "x",
                "--session-id",
                "y",
                "--evidence-class",
                "SYNTHETIC_TEST_ONLY",
            ]
        )


@pytest.mark.parametrize("invalid_session_id", [0, -1, True])
def test_preflight_simulator_identity_requires_positive_interactive_session(
    invalid_session_id: object,
):
    assert _simulator_identity(123, 456, 2)["windows_session_id"] == 2
    with pytest.raises(LivePreflightError, match="windows_session_id"):
        _simulator_identity(123, 456, invalid_session_id)


_TEST_IDENTITY = _simulator_identity(123, 456, 1)
_TEST_ATTESTATION = {"fixture": "FROZEN_TEST_CAPABILITY"}


def _internal_real_fixture(path: Path):
    _run_synthetic(path)
    with _sealed_capture_snapshot(path) as snapshot:
        analysis = _analyze_snapshot(
            snapshot,
            expected_source_id="synthetic-preflight-source",
            expected_session_id="synthetic-preflight-session",
            policy=_policy(1.0, 0.5),
        )
    payload = _base_payload(
        analysis,
        evidence_class=PreflightEvidenceClass.REAL_SDK_PRECHECK_ONLY,
        simulator_identity=_TEST_IDENTITY,
        transport_attestation=_TEST_ATTESTATION,
    )
    receipt = _with_receipt_hash(_with_semantic_digest(payload))
    return analysis, receipt


def _redigest(receipt: dict[str, object]) -> dict[str, object]:
    semantic = deepcopy(receipt)
    semantic.pop("receipt_sha256", None)
    semantic["production_semantic_digest"] = None
    return _with_receipt_hash(_with_semantic_digest(semantic))


def _set_nested(
    value: dict[str, object], path: tuple[str, ...], replacement: object
) -> None:
    current = value
    for key in path[:-1]:
        nested = current[key]
        assert type(nested) is dict
        current = nested
    current[path[-1]] = replacement


def test_private_validator_accepts_only_exact_recomputed_snapshot(tmp_path: Path):
    analysis, receipt = _internal_real_fixture(
        tmp_path / "preflight-internal-pass.jsonl"
    )

    assert (
        _validate_receipt_against_analysis(
            receipt,
            analysis,
            simulator_identity=_TEST_IDENTITY,
            transport_attestation=_TEST_ATTESTATION,
        )
        == receipt
    )


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("collector_evidence", "dropped_tick_count"), 1),
        (("collector_evidence", "tick_rate_hz_values"), []),
        (("capture_context", "sample_count"), 60),
        (("track_context", "track_length_mm"), 7_004_001),
        (("event_receipt", "rejected_sample_count"), 1),
        (("driving_summary", "readiness_status"), "FAIL"),
        (("observation", "frame_interval_count"), 59),
        (("policy", "minimum_frame_rate_ratio_numerator"), 10),
        (("required_field_audit", "missing_or_invalid_counts"), {}),
        (("advisor_only",), 1),
    ],
)
def test_even_correctly_redigested_nested_claims_cannot_override_recomputation(
    tmp_path: Path,
    path: tuple[str, ...],
    replacement: object,
):
    analysis, receipt = _internal_real_fixture(
        tmp_path / f"preflight-nested-{path[-1]}.jsonl"
    )
    tampered = deepcopy(receipt)
    _set_nested(tampered, path, replacement)

    with pytest.raises(LivePreflightError, match="recomput"):
        _validate_receipt_against_analysis(
            _redigest(tampered),
            analysis,
            simulator_identity=_TEST_IDENTITY,
            transport_attestation=_TEST_ATTESTATION,
        )


def test_exact_root_and_nested_schema_reject_extra_keys(tmp_path: Path):
    analysis, receipt = _internal_real_fixture(tmp_path / "preflight-extra-key.jsonl")
    root_extra = deepcopy(receipt)
    root_extra["unexpected"] = None
    with pytest.raises(LivePreflightError, match="exact schema"):
        _validate_receipt_against_analysis(
            _redigest(root_extra),
            analysis,
            simulator_identity=_TEST_IDENTITY,
            transport_attestation=_TEST_ATTESTATION,
        )

    nested_extra = deepcopy(receipt)
    nested_extra["collector_evidence"]["unexpected"] = None
    with pytest.raises(LivePreflightError, match="recomputation"):
        _validate_receipt_against_analysis(
            _redigest(nested_extra),
            analysis,
            simulator_identity=_TEST_IDENTITY,
            transport_attestation=_TEST_ATTESTATION,
        )


def test_same_size_capture_mutation_is_reparsed_not_accepted_by_claim_hash(
    tmp_path: Path,
):
    output = tmp_path / "preflight-same-size.jsonl"
    _analysis, _receipt = _internal_real_fixture(output)
    original = output.read_bytes()
    changed = original.replace(
        b'"source_id":"synthetic-preflight-source"',
        b'"source_id":"synthetic-preflight-sourcf"',
        1,
    )
    assert changed != original and len(changed) == len(original)
    output.write_bytes(changed)

    with pytest.raises(ValueError), _sealed_capture_snapshot(output) as snapshot:
        _analyze_snapshot(
            snapshot,
            expected_source_id="synthetic-preflight-source",
            expected_session_id="synthetic-preflight-session",
            policy=_policy(1.0, 0.5),
        )


def test_capture_symlink_is_rejected_before_snapshot(tmp_path: Path):
    target = tmp_path / "target.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "preflight-link.jsonl"
    try:
        os.symlink(target, link)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation unavailable")

    with (
        pytest.raises(LivePreflightError, match="plain non-reparse"),
        _sealed_capture_snapshot(link),
    ):
        pass


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows share mode blocks renaming the held capture descriptor",
)
def test_path_swap_during_descriptor_snapshot_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output = tmp_path / "preflight-swap.jsonl"
    _run_synthetic(output)
    original = output.read_bytes()
    backup = tmp_path / "old.jsonl"
    real_read = os.read
    swapped = False

    def swapping_read(descriptor: int, count: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            output.rename(backup)
            output.write_bytes(original)
        return real_read(descriptor, count)

    monkeypatch.setattr(os, "read", swapping_read)
    with (
        pytest.raises(LivePreflightError, match="changed during snapshot"),
        _sealed_capture_snapshot(output),
    ):
        pass


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows share mode blocks renaming the held capture descriptor",
)
def test_path_swap_after_snapshot_read_but_before_admission_is_rejected(tmp_path: Path):
    output = tmp_path / "preflight-post-read-swap.jsonl"
    _run_synthetic(output)
    original = output.read_bytes()
    backup = tmp_path / "post-read-old.jsonl"

    with (
        pytest.raises(LivePreflightError, match="sealed-snapshot analysis"),
        _sealed_capture_snapshot(output),
    ):
        output.rename(backup)
        output.write_bytes(original)


def test_integer_observed_rate_policy_has_exact_nine_tenths_boundary(tmp_path: Path):
    exact = run_live_preflight_transport(
        SyntheticTransport(),
        tmp_path / "preflight-rate-exact.jsonl",
        source_id="synthetic-preflight-source",
        session_id="synthetic-preflight-session",
        wait_seconds=0.0,
        duration_s=1.0,
        poll_seconds=0.01,
        stale_after_s=0.5,
        max_reads=55,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )
    below = run_live_preflight_transport(
        SyntheticTransport(capture_interval_s=1 / 53),
        tmp_path / "preflight-rate-below.jsonl",
        source_id="synthetic-preflight-source",
        session_id="synthetic-preflight-session",
        wait_seconds=0.0,
        duration_s=1.0,
        poll_seconds=0.01,
        stale_after_s=0.5,
        max_reads=55,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    assert exact["observation"] == {
        "capture_span_us": 900_000,
        "frame_interval_count": 54,
        "tick_rate_hz": 60,
    }
    assert exact["gates"]["collector_quality"]["status"] == "PASS"
    assert (
        "OBSERVED_FRAME_RATE_TOO_LOW" in below["gates"]["collector_quality"]["reasons"]
    )


def test_powershell_integer_span_formula_matches_python_for_every_allowed_second():
    for duration_s in range(10, 121):
        duration_us = duration_s * 1_000_000
        quotient, remainder = divmod(duration_us * 9, 10)
        powershell_divrem_result = quotient + (1 if remainder else 0)
        assert (
            powershell_divrem_result
            == _policy(float(duration_s), 0.5).minimum_capture_span_us
        )


def test_public_proxy_and_subclass_transports_remain_synthetic(tmp_path: Path):
    class TransportSubclass(SyntheticTransport):
        pass

    class TransportProxy:
        def __init__(self) -> None:
            self._inner = SyntheticTransport()

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    for name, transport in (
        ("subclass", TransportSubclass()),
        ("proxy", TransportProxy()),
    ):
        receipt = run_live_preflight_transport(
            transport,
            tmp_path / f"preflight-{name}.jsonl",
            source_id="synthetic-preflight-source",
            session_id="synthetic-preflight-session",
            wait_seconds=0.0,
            duration_s=1.0,
            max_reads=61,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
        )
        assert receipt["evidence_class"] == "SYNTHETIC_TEST_ONLY"
        assert receipt["status"] == "SYNTHETIC_TEST_ONLY"
        assert receipt["production_semantic_digest"] is None
        assert receipt["can_start_live_capture"] is False


def test_module_global_transport_replacement_cannot_change_frozen_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    called = False

    class Replacement:
        def __init__(self) -> None:
            nonlocal called
            called = True

    monkeypatch.setattr(live_preflight, "WindowsPyirsdkTransport", Replacement)
    closure = inspect.getclosurevars(
        live_preflight._run_windows_live_preflight_cli_only
    ).nonlocals
    assert called is False
    assert closure["transport_type"] is live_preflight._FROZEN_WINDOWS_TRANSPORT_TYPE
    assert not hasattr(live_preflight, "_make_cli_production_runner")
    with pytest.raises(LivePreflightError, match="python -I -B"):
        live_preflight._run_windows_live_preflight_cli_only(
            tmp_path / "preflight-frozen-transport.jsonl",
            source_id="aeis-precheck",
            session_id="frozen-transport",
            expected_sim_process_id=123,
            expected_sim_start_time_utc_ticks=456,
            expected_windows_session_id=1,
            wait_seconds=0.0,
        )
    assert called is False


def test_module_global_core_replacement_cannot_mint_a_production_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    forged_core_called = False

    def forged_core(*_args, **_kwargs):
        nonlocal forged_core_called
        forged_core_called = True
        return {"status": "PASS", "can_start_live_capture": True}

    monkeypatch.setattr(live_preflight, "_run_core", forged_core)
    closure = inspect.getclosurevars(
        live_preflight._run_windows_live_preflight_cli_only
    ).nonlocals
    assert closure["frozen_run_core"] is not forged_core
    with pytest.raises(LivePreflightError, match="python -I -B"):
        live_preflight._run_windows_live_preflight_cli_only(
            tmp_path / "preflight-frozen-core.jsonl",
            source_id="aeis-precheck",
            session_id="frozen-core",
            expected_sim_process_id=123,
            expected_sim_start_time_utc_ticks=456,
            expected_windows_session_id=1,
            wait_seconds=0.0,
        )
    assert forged_core_called is False


def test_production_runner_closure_contains_no_authenticity_key():
    closure = inspect.getclosurevars(
        live_preflight._run_windows_live_preflight_cli_only
    )
    names = set(closure.nonlocals) | set(closure.globals)
    assert not any(
        marker in name.casefold()
        for name in names
        for marker in ("hmac", "key", "secret", "token")
    )
    source = Path(live_preflight.__file__).read_text(encoding="utf-8")
    assert "import hmac" not in source
    assert "import secrets" not in source
    assert "SEMANTIC_INTEGRITY_ONLY" in source


def test_transport_class_method_replacement_is_detected_before_production_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    transport_type = live_preflight._FROZEN_WINDOWS_TRANSPORT_TYPE
    monkeypatch.setattr(transport_type, "startup", lambda *_args: None)

    with pytest.raises(LivePreflightError, match="namespace was replaced"):
        live_preflight._assert_frozen_transport_class()
    with pytest.raises(LivePreflightError):
        live_preflight._run_windows_live_preflight_cli_only(
            tmp_path / "preflight-class-replacement.jsonl",
            source_id="aeis-precheck",
            session_id="class-replacement",
            expected_sim_process_id=123,
            expected_sim_start_time_utc_ticks=456,
            expected_windows_session_id=1,
        )
    assert not (tmp_path / "preflight-class-replacement.jsonl").exists()


def test_fresh_isolated_child_rejects_transport_class_method_replacement(
    tmp_path: Path,
):
    source_root = Path("src").resolve()
    candidates = (
        Path(sys.prefix) / "Lib" / "site-packages",
        *Path(sys.prefix).glob("lib/python*/site-packages"),
    )
    site_packages = next(path.resolve() for path in candidates if path.is_dir())
    irsdk_source = Path(irsdk.__file__).resolve()
    irsdk_import_root = next(
        (parent for parent in (irsdk_source, *irsdk_source.parents) if parent.suffix == ".whl"),
        irsdk_source.parent,
    )
    output = tmp_path / "preflight-isolated-class-attack.jsonl"
    code = f"""
import sys
sys.path[:0] = [{str(source_root)!r}, {str(site_packages)!r}, {str(irsdk_import_root)!r}]
import iracing_ai_engineer.live_preflight as module
module._FROZEN_WINDOWS_TRANSPORT_TYPE.startup = lambda *_args: None
try:
    module._run_windows_live_preflight_cli_only(
        {str(output)!r},
        source_id='aeis-precheck',
        session_id='class-attack',
        expected_sim_process_id=123,
        expected_sim_start_time_utc_ticks=456,
        expected_windows_session_id=1,
        wait_seconds=0.0,
    )
except module.LivePreflightError as exc:
    if 'namespace was replaced' not in str(exc):
        raise
else:
    raise SystemExit('transport replacement reached a production result')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not output.exists()
