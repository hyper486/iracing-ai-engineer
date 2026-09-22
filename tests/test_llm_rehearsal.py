"""All rehearsal evidence is invented; only loopback HTTP is permitted."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from iracing_ai_engineer import crewchief_transport, llm_engineer, sdk_probe

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rehearse_llm.py"
SPEC = importlib.util.spec_from_file_location("_rehearse_llm", SCRIPT)
rehearsal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rehearsal)

EXPECTED_CHECKS = {
    "HTTP_FACTS_PRIVACY_CSRF", "INVALID_PLAN_LOCAL_FALLBACK", "STALE_RECONNECT_WITHDRAWAL",
    "MISSING_KEY_LOCAL_FALLBACK", "INFLIGHT_RATE_BUDGET_LIMITS",
}


@pytest.fixture
def offline_guard(monkeypatch):
    forbidden_calls = []
    connections = []

    def forbidden(*args, **kwargs):
        forbidden_calls.append(True)
        raise AssertionError("FORBIDDEN_EXTERNAL_IO")

    create_connection = socket.create_connection

    def loopback_only(address, *args, **kwargs):
        if address[0] != "127.0.0.1":
            return forbidden()
        connections.append(address)
        return create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", loopback_only)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    monkeypatch.setattr(llm_engineer, "DeepSeekClient", forbidden)
    monkeypatch.setattr(sdk_probe.WindowsPyirsdkTransport, "startup", forbidden)
    monkeypatch.setattr(crewchief_transport.WindowsCrewChiefTransport, "startup", forbidden)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "SYNTHETIC_ENV_KEY_MUST_NOT_BE_USED")
    return forbidden_calls, connections


def test_actual_http_rehearsal_passes_without_sdk_cloud_or_environment_key(offline_guard):
    report = rehearsal.run_rehearsal()
    assert report["status"] == "PASS"
    assert report["source_kind"] == "SYNTHETIC" and report["admission_simulation"] is True
    assert report["provider_called"] is False and report["sdk_accessed"] is False
    assert report["live_acceptance"] is False
    assert {check["id"] for check in report["checks"]} == EXPECTED_CHECKS
    assert all(check["status"] == "PASS" for check in report["checks"])
    forbidden_calls, connections = offline_guard
    assert not forbidden_calls and len(connections) >= 20
    serialized = json.dumps(report)
    for excluded in ("SYNTHETIC_ENV_KEY", "csrf_token", "synthetic_canary", "DO_NOT_FORWARD"):
        assert excluded not in serialized


@pytest.mark.parametrize("scenario", [
    "_normal", "_invalid_plans", "_withdrawal", "_missing_key", "_limits",
])
def test_scenario_failure_is_reported_without_exception_details(
    monkeypatch, scenario, offline_guard,
):
    def broken():
        raise RuntimeError("SYNTHETIC_SECRET_AND_HOST_PATH_NOT_PUBLIC")

    monkeypatch.setattr(rehearsal, scenario, broken)
    report = rehearsal.run_rehearsal()
    assert report["status"] == "FAIL"
    assert sum(check["status"] == "FAIL" for check in report["checks"]) == 1
    assert "SYNTHETIC_SECRET" not in json.dumps(report)


@pytest.mark.parametrize("fail", [False, True])
def test_harness_closes_owned_server_and_service_even_after_failure(fail, offline_guard):
    harness = None
    try:
        with rehearsal._harness() as harness:
            harness.planner.mode = "blocked"
            harness.submit()
            assert harness.planner.entered.wait(1.0)
            if fail:
                raise ValueError("EXPECTED_TEST_FAILURE")
    except ValueError:
        assert fail
    assert harness is not None
    assert not harness.thread.is_alive()
    assert harness.server.socket.fileno() == -1
    assert not harness.service._worker.is_alive()


def test_poll_has_a_deadline_even_if_service_never_answers(monkeypatch):
    harness = object.__new__(rehearsal._Harness)
    calls = []

    def pending():
        calls.append(True)
        return {"answer": None}

    clock = iter((100.0, 100.0, 102.0))
    monkeypatch.setattr(harness, "snapshot", pending)
    monkeypatch.setattr(rehearsal.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(rehearsal.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="ANSWER_TIMEOUT"):
        harness.answer()
    assert len(calls) == 1


@pytest.mark.parametrize("passed,exit_code", [(True, 0), (False, 1)])
def test_main_exit_code_agrees_with_public_json(monkeypatch, capsys, passed, exit_code):
    monkeypatch.setattr(rehearsal, "_bootstrap", lambda: None)
    report = rehearsal._report([{"id": "SYNTHETIC_TEST", "status": "PASS" if passed else "FAIL"}])
    monkeypatch.setattr(rehearsal, "run_rehearsal", lambda: report)
    assert rehearsal.main() == exit_code
    output = capsys.readouterr()
    assert json.loads(output.out) == report and output.err == ""


def test_setup_failure_does_not_print_a_host_path_or_traceback(monkeypatch, capsys):
    def broken():
        raise OSError("SYNTHETIC_PRIVATE_PATH")

    monkeypatch.setattr(rehearsal, "_bootstrap", broken)
    assert rehearsal.main() == 1
    output = capsys.readouterr()
    assert output.err == "" and "SYNTHETIC_PRIVATE_PATH" not in output.out
    assert json.loads(output.out)["checks"] == [{"id": "REHEARSAL_SETUP", "status": "FAIL"}]


def test_cli_bootstraps_from_another_directory_and_writes_no_capture(tmp_path):
    environment = dict(os.environ)
    environment["DEEPSEEK_API_KEY"] = "SYNTHETIC_ENV_KEY_MUST_NOT_BE_USED"
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report["status"] == "PASS" and report["source_kind"] == "SYNTHETIC"
    assert "SYNTHETIC_ENV_KEY" not in result.stdout and str(tmp_path) not in result.stdout
    assert list(tmp_path.iterdir()) == []
