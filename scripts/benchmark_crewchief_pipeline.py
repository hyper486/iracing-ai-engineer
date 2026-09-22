"""Synthetic CPU/durable-write benchmark; never connects to the simulator.

This measures the Python bridge and collector, excluding the native reader and
pipe latency. Timings are observations, not CI thresholds or live acceptance.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import statistics
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import yaml

from iracing_ai_engineer.collector import CollectorSample, JsonlAppendWriter, LiveCollector
from iracing_ai_engineer.crewchief_transport import PROTOCOL_VERSION, WindowsCrewChiefTransport


def synthetic_packet() -> bytes:
    """Invented data only: 335 fields, 20 arrays, and synthetic session metadata."""
    descriptors = []
    values = {}
    offset = 0
    for index in range(335):
        name = f"SyntheticField{index:03d}"
        count = 64 if index < 20 else 1
        descriptors.append({
            "name": name, "type_code": 4, "dtype": "float32", "offset": offset,
            "count": count, "count_as_time": False, "unit": "",
            "description": "Synthetic benchmark variable",
        })
        values[name] = [item / 128.0 for item in range(count)] if count > 1 else index / 2.0
        offset += 4 * count
    session_info = {
        "WeekendInfo": {"SimMode": "replay", "TrackName": "Synthetic benchmark"},
        "SessionInfo": {"Sessions": [{"SessionNum": 0, "SessionType": "Practice"}]},
        "DriverInfo": {"Drivers": [
            {"CarIdx": index, "UserName": f"Synthetic racer {index}", "CarNumber": str(index)}
            for index in range(64)
        ]},
    }
    return (json.dumps({
        "protocol": PROTOCOL_VERSION, "status": "ok",
        "connection": {
            "header_version": 2, "raw_header_status": 1, "tick_rate_hz": 60,
            "variable_count": len(descriptors), "buffer_count": 3, "buffer_len": offset,
        },
        "buffer_tick": 100, "session_info_update": 1,
        "session_info_b64": base64.b64encode(yaml.safe_dump(session_info).encode()).decode(),
        "descriptors": descriptors, "values": values, "read_errors": [],
    }, separators=(",", ":"), allow_nan=False) + "\n").encode()


def timing_summary(seconds: list[float]) -> dict[str, float]:
    ordered = sorted(seconds)
    return {
        "median_ms": round(statistics.median(ordered) * 1000, 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000, 3),
    }


def benchmark(iterations: int, tick_step: int = 3) -> dict[str, object]:
    if type(iterations) is not int or not 1 <= iterations <= 10_000:
        raise ValueError("iterations must be an integer from 1 to 10000")
    if type(tick_step) is not int or not 1 <= tick_step <= 60:
        raise ValueError("tick_step must be an integer from 1 to 60")
    packet = synthetic_packet()
    bridge_seconds: list[float] = []
    collector_seconds: list[float] = []
    pipeline_seconds: list[float] = []
    transport = WindowsCrewChiefTransport(Path("synthetic-only.exe"), _exchange=lambda _: packet)
    try:
        transport.startup(0)
        descriptors = transport.descriptors()
        fields = tuple(item.name for item in descriptors)
        for _ in range(3):
            transport.read_frozen(fields)
            transport.session_info_snapshot()
        with tempfile.TemporaryDirectory(prefix="aeis-synthetic-benchmark-") as directory:
            output = Path(directory) / "synthetic.jsonl"
            with JsonlAppendWriter(output) as writer:
                collector = LiveCollector(
                    writer, source_id="synthetic-benchmark", session_id="synthetic-session",
                    expected_source_kind="REPLAY_SDK_PROXY",
                )
                for index in range(iterations):
                    started = time.perf_counter()
                    frame = transport.read_frozen(fields)
                    metadata, _ = transport.session_info_snapshot()
                    decoded = time.perf_counter()
                    # Deterministic invented clocks/ticks make byte comparison reproducible.
                    frame = replace(
                        frame, buffer_tick=100 + index * tick_step,
                        captured_monotonic_s=index * tick_step / 60,
                    )
                    sample = CollectorSample(frame, descriptors, 60, metadata)
                    prepared = time.perf_counter()
                    collector.ingest(sample)
                    finished = time.perf_counter()
                    bridge_seconds.append(decoded - started)
                    collector_seconds.append(finished - prepared)
                    pipeline_seconds.append(finished - started)
                receipt = collector.finish()
            persisted = output.read_bytes()
    finally:
        transport.close()
    return {
        "evidence_kind": "SYNTHETIC_OFFLINE_NOT_LIVE",
        "native_reader_and_pipe_measured": False,
        "fsync_each_record": True,
        "iterations": iterations, "fields": len(fields), "tick_step": tick_step,
        "packet_bytes": len(packet), "persisted_bytes": len(persisted),
        "persisted_sha256": hashlib.sha256(persisted).hexdigest(),
        "receipt": receipt.to_dict(),
        "bridge_including_metadata_copy": timing_summary(bridge_seconds),
        "collector_ingest_including_fsync": timing_summary(collector_seconds),
        "combined_python_pipeline": timing_summary(pipeline_seconds),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--tick-step", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.iterations, args.tick_step), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
