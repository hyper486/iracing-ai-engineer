"""Normalize two frame-time CSVs into an auditable M0 performance receipt."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path

PERFORMANCE_CONTRACT_VERSION = "sim-performance-ab-v2"
SERIES_CONTRACT_VERSION = "sim-frame-time-series-v1"


class PerformanceReceiptError(ValueError):
    """Raised when raw frame-time evidence cannot be normalized safely."""


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bytes(path: Path, label: str, *, maximum: int = 128 * 1024 * 1024) -> bytes:
    if path.is_symlink():
        raise PerformanceReceiptError(f"{label} must be a regular non-link file")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise PerformanceReceiptError(f"{label} must be a regular non-link file")
    payload = path.read_bytes()
    if not payload or len(payload) > maximum:
        raise PerformanceReceiptError(f"{label} has an invalid byte size")
    return payload


def _file_sha256(path: Path, label: str) -> str:
    if path.is_symlink():
        raise PerformanceReceiptError(f"{label} must be a regular non-link file")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise PerformanceReceiptError(f"{label} must be a regular non-link file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _frame_times(payload: bytes, column: str, label: str) -> list[float]:
    try:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(text.splitlines(), strict=True)
        if reader.fieldnames is None or column not in reader.fieldnames:
            raise PerformanceReceiptError(f"{label} does not contain column {column!r}")
        values: list[float] = []
        for row_number, row in enumerate(reader, start=2):
            raw = row.get(column)
            try:
                value = float(raw) if raw is not None else math.nan
            except ValueError as exc:
                raise PerformanceReceiptError(
                    f"{label} row {row_number} has a non-numeric frame time"
                ) from exc
            if not math.isfinite(value) or value <= 0:
                raise PerformanceReceiptError(
                    f"{label} row {row_number} has an invalid frame time"
                )
            values.append(value)
    except (UnicodeError, csv.Error) as exc:
        raise PerformanceReceiptError(f"{label} is not valid UTF-8 CSV") from exc
    if len(values) < 300:
        raise PerformanceReceiptError(f"{label} requires at least 300 frame times")
    return values


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def build_receipt(
    *,
    baseline_csv: Path,
    sidecar_csv: Path,
    frame_time_column: str,
    telemetry_capture: Path,
    scenario_path: Path,
    scenario_id: str,
    measurement_tool: str,
    maximum_regression_pct: float,
    output_directory: Path,
) -> dict[str, object]:
    if not frame_time_column or not scenario_id or not measurement_tool:
        raise PerformanceReceiptError("column, scenario ID, and measurement tool are required")
    if not math.isfinite(maximum_regression_pct) or not 0 <= maximum_regression_pct <= 5:
        raise PerformanceReceiptError("maximum regression must be between 0 and 5 percent")
    scenario_raw = _read_bytes(scenario_path, "scenario", maximum=8 * 1024 * 1024)
    try:
        scenario_value = json.loads(scenario_raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise PerformanceReceiptError("scenario must be valid JSON") from exc
    if type(scenario_value) is not dict:
        raise PerformanceReceiptError("scenario JSON root must be an object")
    scenario_payload = _canonical_json(scenario_value)
    scenario_sha = _sha256(scenario_payload)
    capture_sha = _file_sha256(telemetry_capture, "telemetry capture")

    series_payloads: dict[str, bytes] = {}
    series_counts: dict[str, int] = {}
    for role, path in (("baseline", baseline_csv), ("sidecar", sidecar_csv)):
        values = _frame_times(
            _read_bytes(path, f"{role} CSV"),
            frame_time_column,
            f"{role} CSV",
        )
        series_payloads[role] = _canonical_json(
            {
                "contract_version": SERIES_CONTRACT_VERSION,
                "frame_times_ms": values,
                "measurement_tool": measurement_tool,
                "role": role,
                "scenario_id": scenario_id,
                "scenario_sha256": scenario_sha,
            }
        )
        series_counts[role] = len(values)

    output = output_directory.absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    scenario_output = output / "performance-scenario.json"
    baseline_output = output / "baseline-frame-times.json"
    sidecar_output = output / "sidecar-frame-times.json"
    receipt_output = output / "performance-receipt.json"
    _write_exclusive(scenario_output, scenario_payload)
    _write_exclusive(baseline_output, series_payloads["baseline"])
    _write_exclusive(sidecar_output, series_payloads["sidecar"])
    receipt = {
        "baseline": {
            "artifact_file": baseline_output.name,
            "artifact_sha256": _sha256(series_payloads["baseline"]),
        },
        "contract_version": PERFORMANCE_CONTRACT_VERSION,
        "max_p95_regression_pct": maximum_regression_pct,
        "measurement_tool": measurement_tool,
        "scenario_file": scenario_output.name,
        "scenario_id": scenario_id,
        "scenario_sha256": scenario_sha,
        "sidecar": {
            "artifact_file": sidecar_output.name,
            "artifact_sha256": _sha256(series_payloads["sidecar"]),
        },
        "telemetry_capture_sha256": capture_sha,
    }
    receipt_payload = _canonical_json(receipt)
    _write_exclusive(receipt_output, receipt_payload)
    return {
        "baseline_sample_count": series_counts["baseline"],
        "output_directory": str(output),
        "performance_receipt_file": receipt_output.name,
        "performance_receipt_sha256": _sha256(receipt_payload),
        "sidecar_sample_count": series_counts["sidecar"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-csv", type=Path, required=True)
    parser.add_argument("--sidecar-csv", type=Path, required=True)
    parser.add_argument("--frame-time-column", required=True)
    parser.add_argument("--telemetry-capture", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--measurement-tool", required=True)
    parser.add_argument("--maximum-regression-pct", type=float, default=5.0)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    receipt = build_receipt(
        baseline_csv=args.baseline_csv,
        sidecar_csv=args.sidecar_csv,
        frame_time_column=args.frame_time_column,
        telemetry_capture=args.telemetry_capture,
        scenario_path=args.scenario,
        scenario_id=args.scenario_id,
        measurement_tool=args.measurement_tool,
        maximum_regression_pct=args.maximum_regression_pct,
        output_directory=args.output_directory,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
