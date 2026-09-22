from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import iracing_ai_engineer.collector as collector_module
import iracing_ai_engineer.live_app_recording as recording
from iracing_ai_engineer.adapters import open_collector_jsonl
from iracing_ai_engineer.collector import CollectorConsistencyError, CollectorSample
from iracing_ai_engineer.live_app_recording import AppRecorder
from iracing_ai_engineer.sdk_probe import RawSdkFrame, VariableDescriptor

IDENTITY = {"source_id": "private-source", "session_id": "private-session"}


def _sample(tick: int = 1) -> CollectorSample:
    descriptors = tuple(
        VariableDescriptor(
            name=name,
            type_code=code,
            dtype=dtype,
            offset=index * 8,
            count=1,
            count_as_time=False,
            unit="",
            description=name,
        )
        for index, (name, code, dtype) in enumerate(
            (
                ("SessionNum", 2, "int32"),
                ("SessionTick", 2, "int32"),
                ("SessionTime", 5, "float64"),
                ("FuelLevel", 4, "float32"),
            )
        )
    )
    return CollectorSample(
        frame=RawSdkFrame(
            buffer_tick=tick,
            session_info_update=1,
            values={
                "SessionNum": 0,
                "SessionTick": tick,
                "SessionTime": tick / 60,
                "FuelLevel": 40.0,
            },
            sim_mode_raw="full",
            captured_monotonic_s=tick / 60,
        ),
        descriptors=descriptors,
        tick_rate_hz=60,
        session_info={
            "WeekendInfo": {"SimMode": "full", "TrackLength": "5.0 km"},
            "DriverInfo": {"Drivers": [{"UserName": "SYNTHETIC PRIVATE DRIVER"}]},
        },
    )


def _records(directory: Path) -> list[dict[str, object]]:
    [path] = directory.glob("*.jsonl")
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def test_complete_clip_is_strictly_replayable_and_receipt_is_aggregate_only(tmp_path):
    recorder = AppRecorder(tmp_path, **IDENTITY)
    recorder.ingest(_sample())
    recorder.ingest(_sample(2))
    receipt = recorder.finish()
    assert receipt is not None
    [path] = tmp_path.glob("*.jsonl")
    with open_collector_jsonl(path) as run:
        assert len(list(run.samples)) == 2
    assert receipt["completion_status"] == "COMPLETE"
    assert receipt["frame_record_count"] == 2
    assert receipt["record_count"] == len(_records(tmp_path))
    assert receipt["byte_count"] == path.stat().st_size == recorder.byte_count
    assert all(type(value) is int for key, value in receipt.items() if key != "completion_status")
    assert "PRIVATE DRIVER" not in path.read_text(encoding="utf-8")
    assert "DriverInfo" not in _records(tmp_path)[2]["payload"]
    private_fields = {"path", "source_id", "session_id", "first_buffer_tick", "records_sha256"}
    assert not private_fields & receipt.keys()
    receipt["frame_record_count"] = -1
    assert recorder.finish()["frame_record_count"] == 2
    recorder.close()
    with pytest.raises(RuntimeError):
        recorder.ingest(_sample(3))


def test_close_keeps_prefix_without_complete_and_cannot_be_finished_later(tmp_path):
    recorder = AppRecorder(tmp_path, **IDENTITY)
    recorder.ingest(_sample())
    recorder.close()
    before = next(tmp_path.glob("*.jsonl")).read_bytes()
    assert not any(record["record_type"] == "collector_receipt" for record in _records(tmp_path))
    with pytest.raises(RuntimeError):
        recorder.finish()
    recorder.close()
    assert next(tmp_path.glob("*.jsonl")).read_bytes() == before


def test_empty_finish_has_no_complete_receipt(tmp_path):
    recorder = AppRecorder(tmp_path, **IDENTITY)
    assert recorder.finish() is None
    assert recorder.finish() is None
    assert recorder.byte_count == 0
    assert next(tmp_path.glob("*.jsonl")).read_bytes() == b""


def test_each_connection_gets_a_new_clip(tmp_path):
    first = AppRecorder(tmp_path, **IDENTITY)
    first.ingest(_sample())
    first.close()
    second = AppRecorder(tmp_path, **IDENTITY)
    second.ingest(_sample(2))
    second.finish()
    paths = list(tmp_path.glob("*.jsonl"))
    assert len(paths) == 2
    assert sum(b'"collector_receipt"' in path.read_bytes() for path in paths) == 1


def test_name_collision_never_reopens_or_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(recording, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    first = AppRecorder(tmp_path, **IDENTITY)
    first.ingest(_sample())
    first.close()
    before = next(tmp_path.glob("*.jsonl")).read_bytes()
    with pytest.raises(FileExistsError):
        AppRecorder(tmp_path, **IDENTITY)
    assert next(tmp_path.glob("*.jsonl")).read_bytes() == before


@pytest.mark.parametrize("budget", [True, False, 0, -1, 1.0, "512"])
def test_invalid_budget_is_rejected_before_creating_directory(tmp_path, budget):
    directory = tmp_path / "capture"
    with pytest.raises(ValueError, match="max_bytes"):
        AppRecorder(directory, max_bytes=budget, **IDENTITY)
    assert not directory.exists()


def test_budget_rejects_whole_record_before_writing_and_poison_prevents_completion(tmp_path):
    probe = AppRecorder(tmp_path / "probe", **IDENTITY)
    probe.ingest(_sample())
    probe.close()
    first_line = next((tmp_path / "probe").glob("*.jsonl")).read_bytes().splitlines(True)[0]
    directory = tmp_path / "bounded"
    recorder = AppRecorder(directory, max_bytes=len(first_line), **IDENTITY)
    try:
        with pytest.raises(CollectorConsistencyError, match="max_output_bytes"):
            recorder.ingest(_sample())
        assert next(directory.glob("*.jsonl")).read_bytes() == first_line
        assert recorder.byte_count == len(first_line)
        with pytest.raises(CollectorConsistencyError, match="RAW_RECORDING_FAILED"):
            recorder.finish()
    finally:
        recorder.close()


def test_receipt_is_also_subject_to_budget(tmp_path):
    probe = AppRecorder(tmp_path / "probe", **IDENTITY)
    probe.ingest(_sample())
    exact_prefix_size = probe.byte_count
    probe.close()
    directory = tmp_path / "bounded"
    recorder = AppRecorder(directory, max_bytes=exact_prefix_size, **IDENTITY)
    recorder.ingest(_sample())
    before = next(directory.glob("*.jsonl")).read_bytes()
    try:
        with pytest.raises(CollectorConsistencyError, match="max_output_bytes"):
            recorder.finish()
        with pytest.raises(CollectorConsistencyError, match="RAW_RECORDING_FAILED"):
            recorder.finish()
    finally:
        recorder.close()
    assert next(directory.glob("*.jsonl")).read_bytes() == before


def test_replay_mode_fails_without_complete(tmp_path):
    observation = _sample()
    replay = replace(observation, frame=replace(observation.frame, sim_mode_raw="replay"))
    recorder = AppRecorder(tmp_path, **IDENTITY)
    try:
        with pytest.raises(CollectorConsistencyError):
            recorder.ingest(replay)
        with pytest.raises(CollectorConsistencyError, match="RAW_RECORDING_FAILED"):
            recorder.finish()
    finally:
        recorder.close()
    assert next(tmp_path.glob("*.jsonl")).read_bytes() == b""


def test_fsync_each_record_and_no_finish_on_close(tmp_path, monkeypatch):
    calls = []
    original = collector_module.os.fsync

    def fsync(descriptor):
        calls.append(descriptor)
        return original(descriptor)

    monkeypatch.setattr(collector_module.os, "fsync", fsync)
    recorder = AppRecorder(tmp_path, **IDENTITY)
    recorder.ingest(_sample())
    assert len(calls) == len(_records(tmp_path))
    recorder.close()
    assert len(calls) == len(_records(tmp_path))


@pytest.mark.parametrize("operation", ["write", "fsync", "fstat"])
def test_io_failures_propagate_and_cannot_later_complete(tmp_path, monkeypatch, operation):
    recorder = AppRecorder(tmp_path, **IDENTITY)

    def fail(*_args):
        raise OSError("synthetic I/O failure")

    monkeypatch.setattr(collector_module.os, operation, fail)
    try:
        with pytest.raises(OSError, match="synthetic I/O failure"):
            recorder.ingest(_sample())
        assert recorder.byte_count == 0
        with pytest.raises(CollectorConsistencyError, match="RAW_RECORDING_FAILED"):
            recorder.finish()
    finally:
        recorder.close()


def test_short_write_failure_preserves_partial_prefix_without_reopening(tmp_path, monkeypatch):
    recorder = AppRecorder(tmp_path, **IDENTITY)
    original = collector_module.os.write
    calls = 0

    def fail_after_partial_write(descriptor, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(descriptor, payload[:7])
        raise OSError("synthetic disk full")

    monkeypatch.setattr(collector_module.os, "write", fail_after_partial_write)
    try:
        with pytest.raises(OSError, match="synthetic disk full"):
            recorder.ingest(_sample())
        with pytest.raises(CollectorConsistencyError, match="RAW_RECORDING_FAILED"):
            recorder.finish()
    finally:
        recorder.close()
    assert len(next(tmp_path.glob("*.jsonl")).read_bytes()) == 7
    assert recorder.byte_count == 0


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "public-checkout"
    root.mkdir()
    (root / "pyproject.toml").touch()
    (root / "AGENTS.md").touch()
    monkeypatch.setattr(recording, "__file__", str(root / "src" / "package" / "module.py"))
    return root


@pytest.mark.parametrize("suffix", ["", "captures", "nested/captures"])
def test_rejects_raw_directory_in_module_checkout_even_with_other_cwd(
    checkout, tmp_path, monkeypatch, suffix
):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CollectorConsistencyError, match="IN_PUBLIC_CHECKOUT"):
        AppRecorder(checkout / suffix, **IDENTITY)
    assert not list(checkout.rglob("*.jsonl"))


def test_rejects_symlink_alias_to_public_checkout(checkout, tmp_path):
    alias = tmp_path / "outside-link"
    try:
        alias.symlink_to(checkout, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is not available")
    with pytest.raises(CollectorConsistencyError, match="IN_PUBLIC_CHECKOUT"):
        AppRecorder(alias / "captures", **IDENTITY)
    assert not (checkout / "captures").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_rejects_windows_junction_to_public_checkout(checkout, tmp_path):
    import _winapi

    alias = tmp_path / "outside-junction"
    _winapi.CreateJunction(str(checkout), str(alias))
    try:
        with pytest.raises(CollectorConsistencyError, match="IN_PUBLIC_CHECKOUT"):
            AppRecorder(alias / "captures", **IDENTITY)
        assert not (checkout / "captures").exists()
    finally:
        alias.rmdir()
