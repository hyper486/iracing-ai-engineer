from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from iracing_ai_engineer.m0 import validate_performance_receipt

_SPEC = importlib.util.spec_from_file_location(
    "make_performance_receipt",
    Path("scripts/windows/make_performance_receipt.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_receipt = _MODULE.build_receipt


def _csv(path: Path, value: float) -> None:
    path.write_text(
        "FrameTimeMs,Ignored\n"
        + "".join(f"{value},{index}\n" for index in range(300)),
        encoding="utf-8",
    )


def test_raw_csvs_produce_an_m0_verifiable_performance_receipt(tmp_path: Path):
    baseline = tmp_path / "baseline.csv"
    sidecar = tmp_path / "sidecar.csv"
    capture = tmp_path / "live.jsonl"
    scenario = tmp_path / "scenario.json"
    _csv(baseline, 10.0)
    _csv(sidecar, 10.2)
    capture.write_bytes(b"collector fixture\n")
    scenario.write_text('{"track":"Spa","weather":"fixed"}\n', encoding="utf-8")
    output = tmp_path / "performance"

    build_receipt(
        baseline_csv=baseline,
        sidecar_csv=sidecar,
        frame_time_column="FrameTimeMs",
        telemetry_capture=capture,
        scenario_path=scenario,
        scenario_id="spa-fixed-v1",
        measurement_tool="PresentMon-normalized",
        maximum_regression_pct=5.0,
        output_directory=output,
    )
    raw_receipt = json.loads(
        (output / "performance-receipt.json").read_text(encoding="utf-8")
    )
    capture_sha = hashlib.sha256(capture.read_bytes()).hexdigest()
    validated = validate_performance_receipt(
        raw_receipt,
        telemetry_capture_sha256=capture_sha,
        artifact_directory=output,
    )

    assert validated["status"] == "PASS"
    assert validated["baseline"]["sample_count"] == 300
    assert validated["baseline"]["p95_frame_time_ms"] == 10.0
    assert validated["sidecar"]["p95_frame_time_ms"] == 10.2
