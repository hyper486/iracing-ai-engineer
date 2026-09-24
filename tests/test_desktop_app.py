"""Packaged-entry self-test boundaries; no simulator and no real model."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from iracing_ai_engineer import desktop_app


def test_self_test_output_is_exclusive_and_not_an_overwrite(tmp_path):
    destination = tmp_path / "test-receipt.json"
    desktop_app._self_test_output(destination, {"status": "PASS"})
    with pytest.raises(FileExistsError):
        desktop_app._self_test_output(destination, {"status": "FAIL"})
    assert json.loads(destination.read_text()) == {"status": "PASS"}


def test_smoke_controller_never_presents_itself_as_a_live_source():
    controller = desktop_app._SelfTestController()
    try:
        assert controller.snapshot()["telemetry"]["source_mode"] == "SYNTHETIC_DEMO"
    finally:
        controller.close()


def test_self_test_cli_receipt_is_synthetic_and_exit_agrees(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_app, "run_self_test", lambda: {"status": "PASS"})
    path = tmp_path / "self-test.json"
    assert desktop_app.main(["--self-test", "--self-test-output", str(path)]) == 0
    assert json.loads(path.read_text()) == {"status": "PASS"}
    assert desktop_app.main(["--self-test", "--self-test-output", str(path)]) == 2


@pytest.mark.parametrize("args", [
    ["--ui-smoke-seconds", "0"], ["--ui-smoke-seconds", "121"],
    ["--ui-smoke-seconds", "nan"], ["--self-test-output", "not-created.json"],
])
def test_bad_args_do_not_start_native_ui(args):
    assert desktop_app.main(args) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_single_instance_pointer_sized_handle_and_release(monkeypatch):
    monkeypatch.setattr(desktop_app, "_MUTEX_NAME", "Local\\AEIS-synthetic-test-" + uuid4().hex)
    first, second, third = (desktop_app._SingleInstance() for _ in range(3))
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        first.close()
        assert third.acquire() is True
    finally:
        first.close()
        second.close()
        third.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop Tcl/Tk integration")
def test_real_native_self_test_without_key_sdk_or_network(monkeypatch):
    from iracing_ai_engineer import sdk_probe
    from iracing_ai_engineer.llm_client import DeepSeekClient

    def forbidden(*args, **kwargs):
        raise AssertionError("No simulator or provider calls are allowed")

    monkeypatch.setattr(sdk_probe.WindowsPyirsdkTransport, "startup", forbidden)
    monkeypatch.setattr(DeepSeekClient, "complete", forbidden)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "SYNTHETIC_ENV_KEY_NEVER_USED")
    result = desktop_app.run_self_test()
    assert result["status"] == "PASS"
    assert result["source_kind"] == "SYNTHETIC"
    assert result["native_gui"] is True
    assert not result["sdk_accessed"] and not result["provider_called"]
    assert not result["live_acceptance"]
    assert {item["id"] for item in result["checks"]} >= {
        "WINDOW_CLOSE_PROTOCOL", "SYNTHETIC_PROXIMITY_TRANSITIONS",
        "SYNTHETIC_LEARNED_FUEL_QUERY", "SYNTHETIC_REPEATED_CORNER_QUERY",
        "SYNTHETIC_FUEL_STOP_QUERY", "SYNTHETIC_RETAINED_STATE_BOUNDS",
        "SYNTHETIC_STINT_OBSERVATION_QUERY", "SYNTHETIC_RAW_PACE_QUERY",
        "SYNTHETIC_SERVICE_COST_QUERY",
        "SYNTHETIC_DRIVER_CONFIRMED_TIRES",
        "SYNTHETIC_TIRE_CAPTURE_REPLAY",
        "SYNTHETIC_TIRE_REVIEW_EXPORT",
    }
