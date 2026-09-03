from __future__ import annotations

import copy
import hashlib
import importlib.util
import inspect
import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest

import iracing_ai_engineer.live_engineer_session as live_module
from iracing_ai_engineer.adapters import open_collector_jsonl_snapshot
from iracing_ai_engineer.collector import CollectorSample, collect_samples_to_jsonl
from iracing_ai_engineer.engineer_session import canonical_sha256
from iracing_ai_engineer.live_engineer_session import (
    LiveEngineerSessionError,
    build_live_engineer_session,
    replay_live_engineer_session,
    replay_retrieved_live_engineer_session,
    validate_live_engineer_session,
    write_live_engineer_session_exclusive,
    write_live_engineer_session_handle,
    write_retrieved_live_engineer_session_report_bundle,
)
from iracing_ai_engineer.live_supervisor import (
    FIXED_VERSION_ROOT,
    CaptureHandleIdentity,
    InstallAdmission,
    RuntimeTokenAdmission,
    _capture_handle_identity,
    build_live_analysis_authority,
)
from iracing_ai_engineer.sdk_probe import RawSdkFrame

RUN_ID = "20260823T220000Z"


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


def _load_paired_fixture_module() -> ModuleType:
    path = Path(__file__).with_name("test_engineer_session.py")
    spec = importlib.util.spec_from_file_location(
        "_live_engineer_paired_fixture", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_live_collector(
    path: Path,
    fixture: ModuleType,
    *,
    source_id: str = "aeis-windows-sdk",
    duplicate_mode: str | None = None,
    change_session_info_without_update: bool = False,
) -> list[dict[str, object]]:
    frames = fixture._paired_frames()  # noqa: SLF001
    descriptors = fixture._descriptors(frames[0])  # noqa: SLF001
    samples = [
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=20_000 + index,
                session_info_update=1,
                values=frame,
                sim_mode_raw="full",
                captured_monotonic_s=float(frame["SessionTime"]),
            ),
            descriptors=descriptors,
            tick_rate_hz=fixture.TICK_RATE_HZ,
            session_info={
                "WeekendInfo": {
                    "SimMode": "full",
                    "TrackLength": "300 m",
                    "TrackName": "R8 Live Synthetic Circuit",
                }
            },
        )
        for index, frame in enumerate(frames)
    ]
    if duplicate_mode == "benign":
        samples.append(samples[-1])
    elif duplicate_mode == "conflict":
        previous = samples[-1]
        changed = dict(previous.frame.values)
        changed["FuelLevel"] = float(changed["FuelLevel"]) + 0.25
        samples.append(
            CollectorSample(
                frame=RawSdkFrame(
                    buffer_tick=previous.frame.buffer_tick,
                    session_info_update=previous.frame.session_info_update,
                    values=changed,
                    sim_mode_raw=previous.frame.sim_mode_raw,
                    captured_monotonic_s=previous.frame.captured_monotonic_s,
                ),
                descriptors=previous.descriptors,
                tick_rate_hz=previous.tick_rate_hz,
                session_info=previous.session_info,
            )
        )
    elif duplicate_mode is not None:
        raise AssertionError("unsupported synthetic duplicate mode")
    if change_session_info_without_update:
        previous = samples[-1]
        samples[-1] = CollectorSample(
            frame=previous.frame,
            descriptors=previous.descriptors,
            tick_rate_hz=previous.tick_rate_hz,
            session_info={
                "WeekendInfo": {
                    "SimMode": "full",
                    "TrackLength": "300 m",
                    "TrackName": "Changed Without Update Counter",
                }
            },
        )
    collect_samples_to_jsonl(
        samples,
        path,
        source_id=source_id,
        session_id=f"live-{RUN_ID}",
        stale_after_s=1.0,
        fsync_each_record=False,
    )
    return frames


@contextmanager
def _active_run(raw_handle: object) -> Iterator[object]:
    descriptor = os.dup(raw_handle.fileno())  # type: ignore[attr-defined]
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as text:
        with open_collector_jsonl_snapshot(text, stale_after_s=1.0) as run:
            yield run


def _authority(raw_handle: object, *, filename: str) -> dict[str, object]:
    capture: CaptureHandleIdentity = _capture_handle_identity(  # noqa: SLF001
        raw_handle, filename=filename
    )
    admission = InstallAdmission(
        install_manifest_sha256="1" * 64,
        project_wheel_sha256="2" * 64,
        runtime_manifest_sha256="3" * 64,
        runtime_manifest_self_sha256="4" * 64,
        runtime_tree_sha256="5" * 64,
        security_tree_sha256="6" * 64,
        security_tree_object_count=2440,
        code_root=str(FIXED_VERSION_ROOT),
    )
    authority = build_live_analysis_authority(
        run_id=RUN_ID,
        capture=capture,
        simulator_identity={
            "process_id": 1234,
            "start_time_utc_ticks": 638_000_000_000_000_000,
            "windows_session_id": 1,
        },
        install_admission=admission,
        runtime_token_admission=RuntimeTokenAdmission(
            current_user_sid=(
                "S-1-5-21-0-0-0-1001"
            ),
            token_is_elevated=False,
            token_elevation_type="LIMITED",
            administrators_sid_enabled=False,
            integrity_level_rid=8192,
            least_privilege="PASS",
        ),
        preflight_receipt_sha256="7" * 64,
        preflight_production_semantic_digest="8" * 64,
    )
    return authority


@pytest.fixture(scope="module")
def synthetic_live_session(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, list[dict[str, object]], dict[str, object], dict[str, object]]:
    root = tmp_path_factory.mktemp("r8-live-engineer")
    capture = root / f"live-{RUN_ID}.jsonl"
    frames = _write_live_collector(capture, _load_paired_fixture_module())
    held_name = root / "held-original.jsonl"
    with _open_path_replacement_handle(capture) as raw:
        authority = _authority(raw, filename=capture.name)
        capture.rename(held_name)
        capture.write_bytes(b'{"attacker":"path replacement"}\n')
        with _active_run(raw) as run:
            receipt = build_live_engineer_session(
                run,
                raw,
                analysis_authority=authority,
                stale_after_s=1.0,
            )
        assert not raw.closed
    return held_name, frames, authority, receipt


def test_live_builder_has_no_caller_scenario_or_context_surface() -> None:
    assert set(inspect.signature(build_live_engineer_session).parameters) == {
        "run",
        "capture_handle",
        "analysis_authority",
        "stale_after_s",
    }


def test_same_handle_live_session_closes_and_remains_wait_only(
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    _, frames, authority, receipt = synthetic_live_session
    assert receipt["status"] == "WAIT_CAPABILITIES"
    assert receipt["attestation_status"] == "SELF_CONSISTENT_NOT_AUTHENTICATED"
    assert receipt["recommendations"] == []
    assert receipt["safety"] == {
        "audio_emitted_count": 0,
        "executable_true_count": 0,
        "live_recommendation_count": 0,
        "tactical_output_count": 0,
        "vehicle_control_enabled": False,
    }
    boundary = receipt["scenario_boundary"]
    assert boundary["scenario_role"] == "DEVELOPMENT_SMOKE_CONFIG_NOT_EVENT_TRUTH"
    assert boundary["fuel_pipeline_role"] == "FUEL_PIPELINE_CONTRACT_ONLY"
    assert boundary["official_event_rules"] is False
    assert boundary["race_recommendation"] == "BLOCKED"
    assert boundary["values_exposed_to_reader_facing_advice"] is False
    assert boundary["internal_fuel_scenario_persisted"] is False
    assert boundary["profile_supplies_complete_fuel_scenario"] is False
    assert boundary["profile_binding_role"] == "UNBOUND_CONFIG_BYTES_ONLY"
    assert boundary["dev_smoke_profile_byte_size"] == 662
    assert boundary["dev_smoke_profile_sha256"] == (
        "7706d831001dfdd1256cbf4101caecbd9e2675028c80e0a0dd69e05ad8423a25"
    )

    observed = receipt["observed_live_evidence"]
    assert observed["current_fuel"]["availability"] == "AVAILABLE"
    assert observed["current_fuel"]["value"] == frames[-1]["FuelLevel"]
    assert observed["horizon"]["laps_remaining"]["availability"] == "UNAVAILABLE"
    assert observed["horizon"]["time_remaining_s"]["availability"] == "UNAVAILABLE"
    assert observed["source_binding"]["source_kind"] == "SDK_LIVE"

    proof = receipt["pipeline_proof"]
    assert proof["internal_core_persisted"] is False
    assert proof["projection_role"] == "SAFE_PIPELINE_PROOF_NO_TACTICAL_CONTENT"
    assert proof["fresh_admission_count"] == 4
    assert proof["component_statuses"] == {
        "advisor_timeline": "WAIT_DATA",
        "engineer_session": "WAIT_DATA",
        "m2_strategy": "WAIT_CAPABILITIES",
        "official_rules": "WAIT_EVENT_RULES_IDENTITY",
        "pit_loss_calibration": "WAIT_MATCHED_PIT_LOSS_BASELINE",
        "race_recommendation": "BLOCKED",
        "service_labels": "WAIT_SERVICE_LABELS",
        "traffic": "WAIT_TRAFFIC_DATA",
    }

    closure = receipt["closure"]
    assert closure["capture_sha256"] == authority["capture_sha256"]
    assert closure["capture_file_id"] == authority["capture_file_id"]
    assert closure["source_content_sha256"] == observed["source_binding"][
        "records_sha256"
    ]
    assert closure["decision_tick"] == frames[-1]["SessionTick"]
    assert closure["decision_session_time_us"] == round(
        float(frames[-1]["SessionTime"]) * 1_000_000
    )
    assert validate_live_engineer_session(
        receipt,
        expected_live_engineer_session_sha256=receipt[
            "live_engineer_session_sha256"
        ],
        expected_capture_sha256=authority["capture_sha256"],
        expected_capture_byte_size=authority["capture_byte_size"],
        expected_analysis_authority_sha256=authority["authority_sha256"],
    ) == receipt


def test_retrieved_live_capture_writes_only_safe_report_projection(
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
    tmp_path: Path,
) -> None:
    capture, frames, authority, receipt = synthetic_live_session
    artifact = tmp_path / "retrieved-live-report.json"
    rendered = tmp_path / "retrieved-live-report.html"
    with capture.open("rb", buffering=0) as handle:
        summary = write_retrieved_live_engineer_session_report_bundle(
            handle,
            receipt,
            artifact,
            rendered,
            expected_live_engineer_session_sha256=receipt[
                "live_engineer_session_sha256"
            ],
            expected_remote_capture_sha256=authority["capture_sha256"],
            expected_remote_capture_byte_size=authority["capture_byte_size"],
            stale_after_s=1.0,
        )

    report = json.loads(artifact.read_text(encoding="utf-8"))
    assert summary == {
        "advisor_only": True,
        "artifact_path": str(artifact),
        "contract_version": "retrieved-live-session-report-write-v1",
        "engineer_session_sha256": report["engineer_session_binding"][
            "engineer_session_sha256"
        ],
        "html_path": str(rendered),
        "live_engineer_session_sha256": receipt[
            "live_engineer_session_sha256"
        ],
        "report_sha256": report["report_sha256"],
        "source_kind": "SDK_LIVE",
        "status": "WAIT_DATA",
        "vehicle_control_enabled": False,
    }
    assert report["sections"]["strategy"]["recommendations"] == []
    assert report["sections"]["fuel"]["strategy_numbers_exposed"] is False
    assert "recommendations" not in report["sections"]["fuel"]
    html_payload = rendered.read_bytes().lower()
    assert b"<script" not in html_payload
    assert b"http://" not in html_payload
    assert b"https://" not in html_payload
    assert not list(tmp_path.glob("*engineer-session*"))

    rejected_artifact = tmp_path / "rejected.json"
    rejected_html = tmp_path / "rejected.html"
    with capture.open("rb", buffering=0) as handle:
        with pytest.raises(LiveEngineerSessionError) as rejected:
            write_retrieved_live_engineer_session_report_bundle(
                handle,
                receipt,
                rejected_artifact,
                rejected_html,
                expected_live_engineer_session_sha256="0" * 64,
                expected_remote_capture_sha256=authority["capture_sha256"],
                expected_remote_capture_byte_size=authority["capture_byte_size"],
                stale_after_s=1.0,
            )
    assert rejected.value.code == "LIVE_REPLAY_MISMATCH"
    assert not rejected_artifact.exists() and not rejected_html.exists()


def test_persisted_projection_contains_no_internal_scenario_or_tactical_payload(
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    _, _, _, receipt = synthetic_live_session
    forbidden_keys = {
        "action",
        "change_tires",
        "current_fuel_l",
        "estimated_stationary_service_s",
        "fuel_add_l",
        "minimum_valid_laps",
        "recommendation",
        "refuel_rate_l_per_s",
        "remaining_laps",
        "reserve_l",
        "scenario",
        "scenario_values",
        "service_selection",
        "selected_service",
        "tank_capacity_l",
        "_validated_scenario_values",
    }

    def walk(value: object) -> Iterator[tuple[str, object]]:
        if type(value) is dict:
            for key, item in value.items():
                yield key, item
                yield from walk(item)
        elif type(value) is list:
            for item in value:
                yield from walk(item)

    pairs = list(walk(receipt))
    assert forbidden_keys.isdisjoint(key for key, _ in pairs)
    assert all(
        item == [] for key, item in pairs if key == "recommendations"
    )
    raw = json.dumps(
        receipt,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    for token in (
        b'"action":',
        b'"change_tires":',
        b'"current_fuel_l":20.0',
        b'"fuel_add_l":',
        b'"refuel_rate_l_per_s":2.0',
        b'"remaining_laps":10',
        b'"tank_capacity_l":120.0',
    ):
        assert token not in raw
    assert b'"values_exposed_to_reader_facing_advice":false' in raw
    assert b'"values_exposed_to_live_output"' not in raw


def test_live_replay_is_object_exact_from_same_held_file(
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    held_name, _, _, receipt = synthetic_live_session
    with held_name.open("r+b", buffering=0) as raw:
        with _active_run(raw) as run:
            rebuilt = replay_live_engineer_session(
                run, raw, receipt, stale_after_s=1.0
            )
        assert not raw.closed
    assert rebuilt == receipt


def test_original_replay_rejects_cross_host_identity_but_retrieved_replay_accepts(
    tmp_path: Path,
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    held_name, _, authority, receipt = synthetic_live_session
    downloaded = tmp_path / f"live-{RUN_ID}.jsonl"
    downloaded.write_bytes(held_name.read_bytes())
    with downloaded.open("rb", buffering=0) as raw:
        with _active_run(raw) as run:
            with pytest.raises(LiveEngineerSessionError) as original_error:
                replay_live_engineer_session(
                    run,
                    raw,
                    receipt,
                    stale_after_s=1.0,
                )
        assert original_error.value.code == "CAPTURE_AUTHORITY_MISMATCH"
        rebuilt = replay_retrieved_live_engineer_session(
            raw,
            receipt,
            expected_remote_capture_sha256=authority["capture_sha256"],
            expected_remote_capture_byte_size=authority["capture_byte_size"],
            stale_after_s=1.0,
        )
        assert not raw.closed
    assert rebuilt == receipt
    assert rebuilt["analysis_authority"]["capture_file_id"] == authority[
        "capture_file_id"
    ]


@pytest.mark.parametrize("mutation", ["bytes", "declared_sha", "declared_size"])
def test_retrieved_replay_rejects_wrong_bytes_size_or_sha(
    tmp_path: Path,
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
    mutation: str,
) -> None:
    held_name, _, authority, receipt = synthetic_live_session
    downloaded = tmp_path / f"{mutation}-{RUN_ID}.jsonl"
    payload = held_name.read_bytes()
    if mutation == "bytes":
        payload = payload[:-1] + (b" " if payload[-1:] != b" " else b"\n")
    downloaded.write_bytes(payload)
    expected_sha = (
        "f" * 64 if mutation == "declared_sha" else authority["capture_sha256"]
    )
    expected_size = (
        authority["capture_byte_size"] + 1
        if mutation == "declared_size"
        else authority["capture_byte_size"]
    )
    with downloaded.open("rb", buffering=0) as raw:
        with pytest.raises(LiveEngineerSessionError) as raised:
            replay_retrieved_live_engineer_session(
                raw,
                receipt,
                expected_remote_capture_sha256=expected_sha,
                expected_remote_capture_byte_size=expected_size,
                stale_after_s=1.0,
            )
    assert raised.value.code in {
        "CAPTURE_AUTHORITY_MISMATCH",
        "RETRIEVED_CAPTURE_MISMATCH",
    }


@pytest.mark.parametrize("mutation", ["rewrite", "link_count"])
def test_retrieved_replay_rejects_in_process_content_or_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
    mutation: str,
) -> None:
    held_name, _, authority, receipt = synthetic_live_session
    downloaded = tmp_path / f"drift-{mutation}-{RUN_ID}.jsonl"
    downloaded.write_bytes(held_name.read_bytes())
    real_builder = live_module._build_live_engineer_session_from_verified_handle

    def mutate_after_rebuild(*args: object, **kwargs: object) -> dict[str, object]:
        result = real_builder(*args, **kwargs)
        if mutation == "rewrite":
            with downloaded.open("r+b", buffering=0) as attacker:
                original = attacker.read(1)
                attacker.seek(0)
                attacker.write(b" " if original != b" " else b"{")
                attacker.flush()
                os.fsync(attacker.fileno())
        else:
            os.link(downloaded, tmp_path / "attacker-hardlink.jsonl")
        return result

    monkeypatch.setattr(
        live_module,
        "_build_live_engineer_session_from_verified_handle",
        mutate_after_rebuild,
    )
    with downloaded.open("rb", buffering=0) as raw:
        with pytest.raises(LiveEngineerSessionError) as raised:
            replay_retrieved_live_engineer_session(
                raw,
                receipt,
                expected_remote_capture_sha256=authority["capture_sha256"],
                expected_remote_capture_byte_size=authority["capture_byte_size"],
                stale_after_s=1.0,
            )
    assert raised.value.code in {
        "RETRIEVED_CAPTURE_CHANGED",
        "RETRIEVED_CAPTURE_INVALID",
    }


def test_retrieved_replay_is_hashseed_independent_and_object_exact(
    tmp_path: Path,
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    held_name, _, authority, receipt = synthetic_live_session
    downloaded = tmp_path / f"hashseed-{RUN_ID}.jsonl"
    downloaded.write_bytes(held_name.read_bytes())
    receipt_path = tmp_path / "analysis.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    program = """
import json
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
from iracing_ai_engineer.live_engineer_session import replay_retrieved_live_engineer_session

capture = pathlib.Path(sys.argv[3])
receipt = json.loads(pathlib.Path(sys.argv[4]).read_text(encoding='utf-8'))
with capture.open('rb', buffering=0) as raw:
    rebuilt = replay_retrieved_live_engineer_session(
        raw,
        receipt,
        expected_remote_capture_sha256=sys.argv[5],
        expected_remote_capture_byte_size=int(sys.argv[6]),
        stale_after_s=1.0,
    )
print(rebuilt['live_engineer_session_sha256'])
"""
    results: list[str] = []
    irsdk_spec = importlib.util.find_spec("irsdk")
    assert irsdk_spec is not None and irsdk_spec.origin is not None
    irsdk_root = str(Path(irsdk_spec.origin).parent)
    for seed in ("1", "777"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                program,
                str(Path(__file__).parents[1] / "src"),
                irsdk_root,
                str(downloaded),
                str(receipt_path),
                authority["capture_sha256"],
                str(authority["capture_byte_size"]),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=environment,
        )
        assert completed.returncode == 0, completed.stderr
        results.append(completed.stdout.strip())
    assert results == [receipt["live_engineer_session_sha256"]] * 2


def test_live_replay_rebuilt_mismatch_is_fail_closed_not_type_error(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    _, _, _, receipt = synthetic_live_session
    monkeypatch.setattr(
        live_module,
        "build_live_engineer_session",
        lambda *_args, **_kwargs: {"different": True},
    )
    with pytest.raises(LiveEngineerSessionError) as raised:
        replay_live_engineer_session(object(), object(), receipt)  # type: ignore[arg-type]
    assert raised.value.code == "LIVE_REPLAY_MISMATCH"


def test_active_run_cannot_be_joined_to_a_different_valid_capture(
    tmp_path: Path,
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    original, _, _, _ = synthetic_live_session
    other = tmp_path / f"live-{RUN_ID}.jsonl"
    _write_live_collector(
        other,
        _load_paired_fixture_module(),
        source_id="different-live-source",
    )
    with original.open("r+b", buffering=0) as original_raw:
        with other.open("r+b", buffering=0) as other_raw:
            authority = _authority(other_raw, filename=other.name)
            with _active_run(original_raw) as run:
                with pytest.raises(LiveEngineerSessionError) as raised:
                    build_live_engineer_session(
                        run,
                        other_raw,
                        analysis_authority=authority,
                        stale_after_s=1.0,
                    )
    assert raised.value.code == "CORE_ENGINEER_SESSION_FAILED"
    assert raised.value.__cause__ is not None
    assert raised.value.__cause__.code == "STRATEGY_CONTEXT_BUILD_FAILED"


@pytest.mark.parametrize(
    ("duplicate_mode", "accepted"),
    [("benign", True), ("conflict", False)],
)
def test_live_baseline_accepts_benign_duplicate_but_rejects_conflict(
    tmp_path: Path,
    duplicate_mode: str,
    accepted: bool,
) -> None:
    capture = tmp_path / f"{duplicate_mode}.jsonl"
    _write_live_collector(
        capture,
        _load_paired_fixture_module(),
        duplicate_mode=duplicate_mode,
    )
    with capture.open("r+b", buffering=0) as raw, _active_run(raw) as run:
        if accepted:
            evidence, _ = live_module._consume_live_baseline(run)  # noqa: SLF001
            assert evidence["duplicate_sample_count"] == 1
            assert evidence["duplicate_conflict_count"] == 0
            assert evidence["samples_seen"] == (
                evidence["frame_record_count"] + 1
            )
        else:
            with pytest.raises(LiveEngineerSessionError) as raised:
                live_module._consume_live_baseline(run)  # noqa: SLF001
            assert raised.value.code == "LIVE_SOURCE_NOT_CLEAN"


def test_live_baseline_rejects_session_info_change_without_update(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "session-info-without-update.jsonl"
    _write_live_collector(
        capture,
        _load_paired_fixture_module(),
        change_session_info_without_update=True,
    )
    with capture.open("r+b", buffering=0) as raw, _active_run(raw) as run:
        with pytest.raises(LiveEngineerSessionError) as raised:
            live_module._consume_live_baseline(run)  # noqa: SLF001
    assert raised.value.code == "LIVE_SOURCE_NOT_CLEAN"


@pytest.mark.parametrize(
    ("section", "field", "value", "code"),
    [
        ("safety", "live_recommendation_count", 1, "LIVE_REPLAY_MISMATCH"),
        ("scenario_boundary", "official_event_rules", True, "LIVE_REPLAY_MISMATCH"),
        (
            "analysis_authority",
            "code_trust_model",
            "PATH_CHECK_ONLY",
            "AUTHORITY_MISMATCH",
        ),
    ],
)
def test_rehashed_promotions_remain_rejected(
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
    section: str,
    field: str,
    value: object,
    code: str,
) -> None:
    _, _, _, receipt = synthetic_live_session
    tampered = copy.deepcopy(receipt)
    tampered[section][field] = value
    if section == "analysis_authority":
        authority_material = {
            key: item
            for key, item in tampered[section].items()
            if key != "authority_sha256"
        }
        tampered[section]["authority_sha256"] = canonical_sha256(authority_material)
        tampered["closure"]["analysis_authority_sha256"] = tampered[section][
            "authority_sha256"
        ]
    tampered["live_engineer_session_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in tampered.items()
            if key != "live_engineer_session_sha256"
        }
    )
    with pytest.raises(LiveEngineerSessionError) as raised:
        validate_live_engineer_session(
            tampered,
            expected_live_engineer_session_sha256=tampered[
                "live_engineer_session_sha256"
            ],
        )
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing_token", "SCHEMA_INVALID"),
        ("extra_legacy_field", "SCHEMA_INVALID"),
        ("old_v1_contract", "AUTHORITY_MISMATCH"),
    ],
)
def test_rehashed_legacy_or_nonexact_authority_is_rejected(
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
    mutation: str,
    expected_code: str,
) -> None:
    _, _, _, receipt = synthetic_live_session
    tampered = copy.deepcopy(receipt)
    authority = tampered["analysis_authority"]
    if mutation == "missing_token":
        del authority["runtime_token_admission"]
    elif mutation == "extra_legacy_field":
        authority["launch_receipt_sha256"] = "9" * 64
    else:
        authority["contract_version"] = "windows-live-analysis-authority-v1"
    authority["authority_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in authority.items()
            if key != "authority_sha256"
        }
    )
    tampered["closure"]["analysis_authority_sha256"] = authority[
        "authority_sha256"
    ]
    tampered["live_engineer_session_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in tampered.items()
            if key != "live_engineer_session_sha256"
        }
    )
    with pytest.raises(LiveEngineerSessionError) as raised:
        validate_live_engineer_session(tampered)
    assert raised.value.code == expected_code


def test_rehashed_observed_sentinel_cannot_fake_missing_provenance(
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    _, _, _, receipt = synthetic_live_session
    tampered = copy.deepcopy(receipt)
    field = tampered["observed_live_evidence"]["horizon"]["laps_remaining"]
    field["availability"] = "UNAVAILABLE_SENTINEL"
    field["presence"] = "MISSING"
    field["provenance"] = "UNKNOWN"
    field["value"] = 32767
    observed = tampered["observed_live_evidence"]
    observed["observed_live_evidence_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in observed.items()
            if key != "observed_live_evidence_sha256"
        }
    )
    tampered["closure"]["observed_live_evidence_sha256"] = observed[
        "observed_live_evidence_sha256"
    ]
    tampered["live_engineer_session_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in tampered.items()
            if key != "live_engineer_session_sha256"
        }
    )
    with pytest.raises(LiveEngineerSessionError) as raised:
        validate_live_engineer_session(tampered)
    assert raised.value.code == "OBSERVED_EVIDENCE_INVALID"


@pytest.mark.parametrize(
    "mutation",
    [
        "current_fuel_value",
        "captured_monotonic_value",
        "pits_open_value",
        "laps_remaining_value",
        "time_remaining_value",
        "current_fuel_source_fields",
        "laps_completed_range",
    ],
)
def test_fully_rehashed_available_observation_requires_exact_type_range_and_source(
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
    mutation: str,
) -> None:
    _, _, _, receipt = synthetic_live_session
    tampered = copy.deepcopy(receipt)
    observed = tampered["observed_live_evidence"]
    if mutation == "current_fuel_value":
        observed["current_fuel"]["value"] = "not-a-number"
    elif mutation == "captured_monotonic_value":
        observed["decision_clock"]["captured_monotonic"]["value"] = "bad"
    elif mutation == "pits_open_value":
        observed["pits_open"] = {
            "availability": "AVAILABLE",
            "presence": "PRESENT",
            "provenance": "SDK_DIRECT",
            "source_fields": ["PitsOpen"],
            "value": "false",
        }
    elif mutation == "laps_remaining_value":
        observed["horizon"]["laps_remaining"] = {
            "availability": "AVAILABLE",
            "presence": "PRESENT",
            "provenance": "SDK_DIRECT",
            "source_fields": ["SessionLapsRemainEx"],
            "value": 3.5,
        }
    elif mutation == "time_remaining_value":
        observed["horizon"]["time_remaining_s"] = {
            "availability": "AVAILABLE",
            "presence": "PRESENT",
            "provenance": "SDK_DIRECT",
            "source_fields": ["SessionTimeRemain"],
            "value": "soon",
        }
    elif mutation == "current_fuel_source_fields":
        observed["current_fuel"]["source_fields"] = ["FuelLevelPct"]
    else:
        observed["laps_completed"] = 32767
    observed["observed_live_evidence_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in observed.items()
            if key != "observed_live_evidence_sha256"
        }
    )
    tampered["closure"]["observed_live_evidence_sha256"] = observed[
        "observed_live_evidence_sha256"
    ]
    tampered["live_engineer_session_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in tampered.items()
            if key != "live_engineer_session_sha256"
        }
    )
    with pytest.raises(LiveEngineerSessionError) as raised:
        validate_live_engineer_session(tampered)
    assert raised.value.code == "OBSERVED_EVIDENCE_INVALID"


def test_fully_rehashed_outer_proof_cannot_hide_bad_lineage_self_hash(
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    _, _, _, receipt = synthetic_live_session
    tampered = copy.deepcopy(receipt)
    proof = tampered["pipeline_proof"]
    lineage = proof["input_lineage"]
    lineage["input_lineage_sha256"] = "9" * 64
    proof["pipeline_proof_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in proof.items()
            if key != "pipeline_proof_sha256"
        }
    )
    tampered["closure"]["input_lineage_sha256"] = "9" * 64
    tampered["live_engineer_session_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in tampered.items()
            if key != "live_engineer_session_sha256"
        }
    )
    with pytest.raises(LiveEngineerSessionError) as raised:
        validate_live_engineer_session(tampered)
    assert raised.value.code == "PIPELINE_PROOF_INVALID"


def test_rehashed_zero_windows_session_is_rejected_by_validation_and_replay(
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    held_name, _, _, receipt = synthetic_live_session
    tampered = copy.deepcopy(receipt)
    authority = tampered["analysis_authority"]
    authority["windows_session_id"] = 0
    authority["authority_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in authority.items()
            if key != "authority_sha256"
        }
    )
    tampered["closure"]["analysis_authority_sha256"] = authority[
        "authority_sha256"
    ]
    tampered["live_engineer_session_sha256"] = canonical_sha256(
        {
            key: item
            for key, item in tampered.items()
            if key != "live_engineer_session_sha256"
        }
    )
    with pytest.raises(LiveEngineerSessionError) as validate_error:
        validate_live_engineer_session(tampered)
    assert validate_error.value.code == "SCHEMA_INVALID"
    with held_name.open("r+b", buffering=0) as raw, _active_run(raw) as run:
        with pytest.raises(LiveEngineerSessionError) as replay_error:
            replay_live_engineer_session(
                run,
                raw,
                tampered,
                stale_after_s=1.0,
            )
    assert replay_error.value.code == "SCHEMA_INVALID"


def test_same_fd_writer_readback_keeps_handle_and_never_overwrites(
    tmp_path: Path,
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    _, _, _, receipt = synthetic_live_session
    output = tmp_path / "live-engineer-session.json"
    descriptor = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as raw:
        artifact = write_live_engineer_session_handle(raw, receipt)
        assert not raw.closed
        raw.seek(0)
        payload = raw.read()
        assert artifact["artifact_byte_size"] == len(payload)
        assert artifact["artifact_sha256"] == hashlib.sha256(payload).hexdigest()
        assert json.loads(payload) == receipt
        with pytest.raises(LiveEngineerSessionError) as nonempty:
            write_live_engineer_session_handle(raw, receipt)
        assert nonempty.value.code == "OUTPUT_HANDLE_INVALID"
    output_metadata = output.stat()
    if os.name == "nt":
        assert not (int(getattr(output_metadata, "st_file_attributes", 0)) & 0x400)
    else:
        assert output_metadata.st_mode & 0o777 == 0o600

    convenience = tmp_path / "local-convenience.json"
    created = write_live_engineer_session_exclusive(convenience, receipt)
    assert created["artifact_sha256"] == hashlib.sha256(
        convenience.read_bytes()
    ).hexdigest()
    with pytest.raises(LiveEngineerSessionError) as exists:
        write_live_engineer_session_exclusive(convenience, receipt)
    assert exists.value.code == "OUTPUT_CREATE_FAILED"


def test_partial_writer_failure_retains_same_file_and_never_unlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_live_session: tuple[
        Path, list[dict[str, object]], dict[str, object], dict[str, object]
    ],
) -> None:
    _, _, _, receipt = synthetic_live_session
    output = tmp_path / "partial-live-engineer-session.json"
    descriptor = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    real_write = os.write
    calls = 0

    def fail_after_prefix(fd: int, payload: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            view = memoryview(payload)
            return real_write(fd, view[: max(1, len(view) // 4)])
        raise OSError("synthetic partial write failure")

    with os.fdopen(descriptor, "r+b", buffering=0) as raw:
        opened = os.fstat(raw.fileno())
        monkeypatch.setattr(live_module.os, "write", fail_after_prefix)
        with pytest.raises(LiveEngineerSessionError) as raised:
            write_live_engineer_session_handle(raw, receipt)
        assert raised.value.code == "OUTPUT_WRITE_FAILED"
        assert not raw.closed
        retained = os.stat(output, follow_symlinks=False)
        assert (retained.st_dev, retained.st_ino) == (opened.st_dev, opened.st_ino)
        assert retained.st_size > 0
    assert output.exists()
