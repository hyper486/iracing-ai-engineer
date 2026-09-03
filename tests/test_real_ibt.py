from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from iracing_ai_engineer.ibt import IbtFormatError, IbtReader, sha256_file
from iracing_ai_engineer.quality import analyze_ibt
from iracing_ai_engineer.replay import replay_ibt

SAMPLE = Path("data/raw/mclaren570sgt4_nurburgring combined 2025-05-24 23-49-52.ibt")
EXPECTED_SOURCE_SHA256 = "f7c4925cd064ee1d364ddbf0d45dad3cf7cb13859aa36eb955d09bcaf528547e"
EXPECTED_SCHEMA_SHA256 = "6085fd35487871a1553c3edcc24c4818943d076afcf17968fe8d00ba6351fe8d"
MANIFEST = json.loads(Path("data/manifest.json").read_text(encoding="utf-8"))

requires_data = pytest.mark.skipif(not SAMPLE.is_file(), reason="REQUIRES_DATA: frozen IBT absent")


@requires_data
def test_all_manifest_sources_match_size_hash_record_count_and_schema():
    for item in MANIFEST["files"]:
        path = Path("data/raw") / item["file_name"]
        assert path.stat().st_size == item["byte_size"]
        assert sha256_file(path) == item["sha256"]
        with IbtReader(path) as reader:
            assert reader.metadata.record_count == item["record_count"]
            assert reader.metadata.schema_sha256 == MANIFEST["common_ibt_contract"][
                "schema_sha256"
            ]
            assert reader.metadata.field_names_sha256 == MANIFEST["common_ibt_contract"][
                "field_names_sha256"
            ]


@requires_data
def test_real_source_and_schema_match_frozen_manifest():
    assert sha256_file(SAMPLE) == EXPECTED_SOURCE_SHA256
    with IbtReader(SAMPLE) as reader:
        assert reader.metadata.record_count == 80_012
        assert reader.metadata.tick_rate_hz == 60
        assert reader.metadata.variable_count == 277
        assert reader.metadata.trailing_bytes == 0
        assert reader.metadata.schema_sha256 == EXPECTED_SCHEMA_SHA256
        context = reader.public_session_context()
        assert context["track_name"] == "nurburgring combined"
        assert not any("driver" in key.lower() or "user" in key.lower() for key in context)


@requires_data
def test_pyirsdk_bounds_bug_is_blocked_by_adapter():
    with IbtReader(SAMPLE) as reader:
        with pytest.raises(IndexError):
            reader.get_record(-1, ("SessionTime",))
        with pytest.raises(IndexError):
            reader.get_record(reader.metadata.record_count, ("SessionTime",))


@requires_data
def test_path_replacement_cannot_mix_open_source_metadata(tmp_path):
    target = tmp_path / "source.ibt"
    replacement = tmp_path / "replacement.ibt"
    shutil.copyfile(SAMPLE, target)
    other = next(path for path in Path("data/raw").glob("*.ibt") if path != SAMPLE)
    shutil.copyfile(other, replacement)

    with IbtReader(target) as reader:
        opened_digest = reader.source_sha256
        opened_size = reader.metadata.file_size_bytes
        os.replace(replacement, target)

        assert reader.source_sha256 == opened_digest
        assert reader.metadata.file_size_bytes == opened_size
        reader.verify_source_unchanged()


@requires_data
def test_in_place_source_mutation_is_rejected(tmp_path):
    target = tmp_path / "mutable.ibt"
    shutil.copyfile(SAMPLE, target)

    with IbtReader(target) as reader:
        with target.open("r+b") as handle:
            handle.seek(-1, os.SEEK_END)
            original = handle.read(1)
            handle.seek(-1, os.SEEK_END)
            handle.write(bytes([original[0] ^ 0x01]))
            handle.flush()
        with pytest.raises(IbtFormatError, match="changed while it was being read"):
            reader.verify_source_unchanged()


def test_preflight_rejects_invalid_buffer_count_before_pyirsdk(tmp_path):
    target = tmp_path / "invalid-header.ibt"
    values = [2, 1, 60, 0, 0, 0, 1, 48, 100, 1, 0, 0]
    target.write_bytes(struct.pack("<12i", *values))

    with pytest.raises(IbtFormatError, match="invalid buffer count"):
        IbtReader(target).open()


def test_preflight_routes_rpy_away_from_ibt_parser(tmp_path, monkeypatch):
    target = tmp_path / "endurance.rpy"
    target.write_bytes(b"YLPR")

    def fail_if_parser_is_constructed():
        raise AssertionError("RPY must be rejected before constructing the IBT parser")

    monkeypatch.setattr("iracing_ai_engineer.ibt.irsdk.IBT", fail_if_parser_is_constructed)

    with pytest.raises(IbtFormatError, match=r"\.rpy/YLPR.*not IBT telemetry"):
        IbtReader(target).open()


@requires_data
def test_declared_tick_rate_mismatch_disables_replay_capability(tmp_path):
    target = tmp_path / "wrong-rate.ibt"
    shutil.copyfile(SAMPLE, target)
    with target.open("r+b") as handle:
        handle.seek(8)
        handle.write(struct.pack("<i", 120))

    report = analyze_ibt(target)

    assert not report.capabilities.replay_readable
    sampling_gate = next(gate for gate in report.gates if gate.gate_id == "sampling_consistency")
    assert sampling_gate.status == "FAIL"


@requires_data
def test_real_quality_report_is_capability_specific():
    report = analyze_ibt(SAMPLE)

    assert report.capabilities.replay_readable
    assert report.capabilities.lap_ready
    assert not report.capabilities.coaching_evidence_ready


@requires_data
def test_real_frame_hash_is_partition_invariant_and_manifest_matches():
    first = replay_ibt(SAMPLE, frame_hash_chunk_size=1)
    second = replay_ibt(SAMPLE, frame_hash_chunk_size=4096)

    assert first.replay_sha256 == second.replay_sha256
    assert first.normalized_frames_sha256 == second.normalized_frames_sha256
    candidate = MANIFEST["provisional_replay_receipt"]
    assert first.replay_sha256 == candidate["replay_sha256"]
    assert first.events_sha256 == candidate["events_sha256"]
    assert first.results_sha256 == candidate["results_sha256"]
    assert first.lap_algorithm_version == candidate["lap_algorithm_version"]
    assert first.structurally_complete_lap_count == candidate["structurally_complete_lap_count"]


@requires_data
def test_real_replay_digest_is_stable_across_fresh_processes():
    script = (
        "from iracing_ai_engineer.replay import replay_ibt; "
        f"print(replay_ibt({str(SAMPLE)!r}).replay_sha256)"
    )
    outputs = []
    for seed in ("1", "987654"):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = "src"
        environment["PYTHONHASHSEED"] = seed
        outputs.append(
            subprocess.check_output(
                [sys.executable, "-c", script],
                cwd=Path.cwd(),
                env=environment,
                text=True,
            ).strip()
        )

    assert outputs == [MANIFEST["provisional_replay_receipt"]["replay_sha256"]] * 2
