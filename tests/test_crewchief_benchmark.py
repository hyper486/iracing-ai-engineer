"""Check synthetic benchmark reproducibility, not machine-dependent speed."""

import json
import runpy
from pathlib import Path

import pytest

BENCHMARK = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "benchmark_crewchief_pipeline.py")
)


def test_synthetic_pipeline_retains_preoptimization_bytes_and_receipt(monkeypatch):
    syncs = []
    monkeypatch.setattr("iracing_ai_engineer.collector.os.fsync", syncs.append)
    result = BENCHMARK["benchmark"](5)
    assert result["evidence_kind"] == "SYNTHETIC_OFFLINE_NOT_LIVE"
    assert result["native_reader_and_pipe_measured"] is False
    assert result["fsync_each_record"] is True
    assert result["fields"] == 335
    assert result["packet_bytes"] == 80535
    assert result["persisted_bytes"] == 158209
    # Golden values measured using unmodified baseline 95e0542 and invented data.
    assert result["persisted_sha256"] == (
        "21cd25cfe87997ff9a103fb4da0007a7e29d758fe5d577f5694fe516741d1c4f"
    )
    assert result["receipt"]["records_sha256"] == (
        "b50a89198cec96c4508b8af51b8e3802dd77dbc149d3e63b9b6a94de33d5e4b6"
    )
    assert result["receipt"]["frame_record_count"] == 5
    assert result["receipt"]["dropped_tick_count"] == 8
    assert result["receipt"]["semantic_record_count"] == 12
    assert len(syncs) == 13  # Every semantic record plus the receipt.


def test_benchmark_packet_is_explicitly_synthetic_and_deterministic():
    first = BENCHMARK["synthetic_packet"]()
    assert first == BENCHMARK["synthetic_packet"]()
    packet = json.loads(first)
    assert len(packet["descriptors"]) == 335
    assert all(name.startswith("SyntheticField") for name in packet["values"])


@pytest.mark.parametrize("iterations,tick_step", [(0, 3), (10001, 3), (True, 3), (5, 0)])
def test_benchmark_rejects_unbounded_or_invalid_parameters(iterations, tick_step):
    with pytest.raises(ValueError):
        BENCHMARK["benchmark"](iterations, tick_step)
