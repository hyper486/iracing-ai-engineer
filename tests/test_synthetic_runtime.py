"""Invented aggregate diagnostics must not turn into live/hardware acceptance."""

from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest

from iracing_ai_engineer import synthetic_runtime as runtime


@pytest.mark.parametrize("rate", [20, 60])
def test_actual_numerical_pipeline_reaches_all_local_features_without_io(monkeypatch, rate):
    from iracing_ai_engineer import llm_engineer
    from iracing_ai_engineer.llm_client import DeepSeekClient
    from iracing_ai_engineer.sdk_probe import WindowsPyirsdkTransport

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Synthetic diagnostics must not call external systems")

    monkeypatch.setattr(WindowsPyirsdkTransport, "startup", forbidden)
    monkeypatch.setattr(DeepSeekClient, "complete", forbidden)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "SYNTHETIC_SECRET_MUST_NOT_APPEAR")
    original_context = llm_engineer.build_live_context
    def bounded_context(snapshot):
        context = original_context(snapshot)
        # The combined stop/coaching/stint features add allowlisted fact IDs,
        # not raw lap traces, identities or unchecked source strings.
        assert len(context["facts"]) <= 32
        encoded = json.dumps(context, allow_nan=False)
        assert len(encoded) < 10_000 and "SYNTHETIC_SECRET" not in encoded
        assert "SessionTick" not in encoded and "synthetic-runtime-only" not in encoded
        return context
    monkeypatch.setattr(llm_engineer, "build_live_context", bounded_context)
    before = set(threading.enumerate())
    result = runtime.run_synthetic_runtime(rate=rate)
    assert result["status"] == "PASS" and result["source_kind"] == "SYNTHETIC"
    assert result["phase"] == "COMPLETE" and result["virtual_time_paced"] is True
    assert result["proximity_kinds"] == ["ALL_CLEAR", "CAR_LEFT"]
    assert all(result["counts"][key] > 0 for key in (
        "frames", "fuel.current", "strategy.window", "driving.practice", "stint.observed",
        "tire.observed_context", "tire.pace"))
    assert all(result[key] is False for key in (
        "sdk_accessed", "provider_called", "audio_io", "raw_capture_written", "live_acceptance"))
    assert result["private_growth_bytes"] is None  # Short self-test is not a soak.
    assert len(result["checks"]) == 7 and not calls
    serialized = json.dumps(result)
    assert "SDK_LIVE" not in serialized and "SYNTHETIC_SECRET" not in serialized
    assert set(threading.enumerate()) <= before


def test_a_broken_spotter_cannot_get_a_pass_from_fuel_and_corner_success(monkeypatch):
    original = runtime.synthetic_frames

    def no_proximity(laps, rate):
        for frame in original(laps, rate):
            yield replace(frame, values={**frame.values, "CarLeftRight": 0})

    monkeypatch.setattr(runtime, "synthetic_frames", no_proximity)
    result = runtime.run_synthetic_runtime(rate=20)
    assert result["status"] == "FAIL"
    assert result["phase"] == "REQUIRED_FEATURES_REACHED"
    assert result["proximity_kinds"] == []


def test_private_exceptions_are_redacted_and_workers_are_closed(monkeypatch):
    before = set(threading.enumerate())

    def broken(*_args):
        raise RuntimeError("SYNTHETIC_PRIVATE_ERROR_MUST_NOT_APPEAR")

    monkeypatch.setattr(runtime._LiveAnalysis, "process", broken)
    result = runtime.run_synthetic_runtime()
    assert result["status"] == "FAIL" and result["phase"] == "NUMERICAL_PIPELINE"
    assert "PRIVATE_ERROR" not in json.dumps(result)
    assert set(threading.enumerate()) <= before


@pytest.mark.parametrize("kwargs", [
    {"laps": True}, {"laps": 5}, {"laps": 7}, {"laps": 3001}, {"laps": "8"},
    {"rate": True}, {"rate": 60.0}, {"rate": 120},
])
def test_invalid_runtime_arguments_do_not_start_workers(kwargs):
    with pytest.raises(ValueError, match="SYNTHETIC_RUNTIME_ARGUMENT"):
        runtime.run_synthetic_runtime(**kwargs)
    with pytest.raises(ValueError, match="SYNTHETIC_RUNTIME_ARGUMENT"):
        next(runtime.synthetic_frames(**{"laps": 8, **kwargs}))
