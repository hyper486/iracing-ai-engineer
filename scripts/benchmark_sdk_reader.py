"""Synthetic SDK decode and optional local timer diagnostic, never live evidence."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import threading
import time

from iracing_ai_engineer.collector import _transport_session_info
from iracing_ai_engineer.live_app import _pace_reader
from iracing_ai_engineer.live_worker import payload_size
from iracing_ai_engineer.sdk_probe import _value_digest
from iracing_ai_engineer.synthetic_sdk import synthetic_sdk


def _summary(values):
    ordered = sorted(values)
    return {"median_ms": round(statistics.median(ordered) * 1000, 4),
            "p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1] * 1000, 4)}


def benchmark(iterations=1000, *, wait_samples=0, include_chars=False):
    if type(iterations) is not int or not 1 <= iterations <= 10_000:
        raise ValueError("SYNTHETIC_SDK_ARGUMENT")
    if type(wait_samples) is not int or not 0 <= wait_samples <= 300:
        raise ValueError("SYNTHETIC_SDK_ARGUMENT")
    if type(include_chars) is not bool:
        raise ValueError("SYNTHETIC_SDK_ARGUMENT")
    timings = {key: [] for key in ("read_frozen", "metadata", "queue_accounting")}
    with synthetic_sdk(include_chars=include_chars) as (transport, descriptors):
        fields = tuple(row.name for row in descriptors)
        for _ in range(3):
            frame = transport.read_frozen(fields)
            _transport_session_info(transport, frame)
        digest = _value_digest(frame.values)
        for _ in range(iterations):
            before = time.perf_counter()
            frame = transport.read_frozen(fields)
            decoded = time.perf_counter()
            frame, metadata, _ = _transport_session_info(transport, frame)
            bound = time.perf_counter()
            payload_size((frame.values, metadata, frame.read_errors, frame.sim_mode_raw))
            accounted = time.perf_counter()
            timings["read_frozen"].append(decoded - before)
            timings["metadata"].append(bound - decoded)
            timings["queue_accounting"].append(accounted - bound)
            if frame.read_errors or _value_digest(frame.values) != digest:
                raise RuntimeError("SYNTHETIC_SDK_MISMATCH")
    waits = {key: [] for key in ("event_wait", "reader_sleep")}
    stop = threading.Event()
    for index in range(wait_samples):
        # Alternate first position to reduce (not eliminate) order effects.
        order = tuple(waits) if index % 2 == 0 else tuple(reversed(waits))
        for key in order:
            before = time.perf_counter()
            if key == "event_wait":
                stop.wait(0.01)
            else:
                _pace_reader(stop, before, time.perf_counter, time.sleep)
            waits[key].append(time.perf_counter() - before)
    return {"evidence_kind": "SYNTHETIC_CPU_AND_TIMER_ONLY", "sdk_accessed": False,
            "live_acceptance": False, "event_wait_measured": False,
            "durable_recording_measured": False, "iterations": iterations,
            "char_fields_included": include_chars,
            "fields": len(fields), "arrays": 20, "array_size": 64,
            "values_sha256": digest, "timing": {key: _summary(value)
                                                 for key, value in timings.items()},
            "poll_timer": {"samples_each": wait_samples, "target_period_ms": 10,
                           "measured": bool(wait_samples),
                           "timing": {key: _summary(value) for key, value in waits.items()
                                      if value}}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--wait-samples", type=int, default=0,
                        help="Optional idle timer comparison, 0..300 samples each; no SDK wait.")
    parser.add_argument("--include-chars", action="store_true",
                        help="Include raw byte-valued SDK char fields in CPU/queue checks.")
    args = parser.parse_args()
    print(json.dumps(benchmark(args.iterations, wait_samples=args.wait_samples,
                               include_chars=args.include_chars), indent=2))
