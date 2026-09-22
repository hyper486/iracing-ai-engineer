"""Offline HTTP admission rehearsal, never simulator or provider acceptance.

Run with the repository Python: ``python scripts/rehearse_llm.py``. The fixture
uses SDK_LIVE *only* to simulate admission through the real evidence gate; it
is invented data, never a capture, and the report is always SYNTHETIC.
"""

from __future__ import annotations

import copy
import http.client
import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def _bootstrap() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    if not (source_root / "iracing_ai_engineer" / "live_app.py").is_file():
        raise RuntimeError("SOURCE_UNAVAILABLE")
    sys.path.insert(0, str(source_root))


def _fixture() -> tuple[dict, dict]:
    # SDK_LIVE here is an admission simulation, NOT authentic live evidence.
    monitor = {
        "contract_version": "live-monitor-v1", "record_type": "live_monitor_snapshot",
        "advisor_only": True, "executable": False,
        "source_kind": "SDK_LIVE", "status": "READY", "sequence": 1,
        "binding_sha256": "synthetic-rehearsal-not-authentic",
        "context": {"player_control_state": "IN_CAR_PHYSICS", "sim_source_mode": "FULL",
                    "conflicts": []},
        "quality": {"status": "READY", "stale": False}, "interval_invalid_for_fuel": [],
        "telemetry": {"session_num": 0, "lap_number": 8, "on_pit_road": False},
        "SessionInfo": {"synthetic_canary": "DO_NOT_FORWARD_SYNTHETIC_SESSION"},
        "untrusted_text": "DO_NOT_FORWARD_SYNTHETIC_FREE_TEXT",
    }
    fuel = {
        "status": "READY", "current_fuel_l": 20.0, "valid_laps": 5, "required_laps": 5,
        "conservative_burn_l_per_lap": 2.0, "estimated_laps_remaining": 9,
        "fuel_needed_to_finish_l": 30.0, "fuel_to_add_l": 10.0, "minimum_stops": 1,
        "estimate_only": True, "advisor_only": True, "executable": False,
    }
    return monitor, fuel


class _Planner:
    """In-process fake. No transport, credentials, or provider calls exist here."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.mode = "valid"
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, context: dict, question: str) -> dict:
        from iracing_ai_engineer.llm_engineer import fallback_plan

        self.calls.append(copy.deepcopy(context))
        self.entered.set()
        if self.mode == "blocked" and not self.release.wait(2.0):
            raise RuntimeError("FAKE_PLANNER_TIMEOUT")
        if self.mode == "malformed":
            return {"text": "DO_NOT_RENDER_SYNTHETIC_MODEL_PROSE"}
        if self.mode == "hallucinated":
            return {"topic": "fuel", "fact_ids": ["invented.fact"], "notice_ids": []}
        return fallback_plan(context, question)


class _Harness:
    def __init__(self, *, missing_key: bool = False, request_limit: int = 10) -> None:
        from iracing_ai_engineer.live_app import AppState
        from iracing_ai_engineer.llm_engineer import EngineerConfig, EngineerService

        self.now = 100.0
        self.planner = _Planner()
        self.state = AppState(clock=lambda: self.now)
        self.state.connection("CONNECTED")
        self.refresh()
        # Explicit empty environment: never inspect a user's configured key.
        self.service = EngineerService(
            self.state.snapshot,
            EngineerConfig(provider="deepseek", request_limit=request_limit, min_interval_s=1.0),
            client=None if missing_key else self.planner,
            clock=lambda: self.now, environ={},
        )
        self.server = None
        self.thread = None
        self.token = ""

    def refresh(self) -> None:
        self.state.publish(*_fixture(), None, "Race")

    def advance(self) -> None:
        self.now += 1.01
        self.refresh()

    def request(self, method: str, path: str, *, token: str | None = None) -> tuple[int, dict]:
        port = self.server.server_port
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
        headers = {"Origin": f"http://127.0.0.1:{port}"}
        body = None
        if method == "POST":
            headers.update({"Content-Type": "application/json",
                            "X-Engineer-Token": self.token if token is None else token})
            body = json.dumps({"question": "How much fuel remains?", "scope": "live"})
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(65_537)
            _require(len(raw) <= 65_536)
            result = json.loads(raw)
            _require(type(result) is dict)
            return response.status, result
        finally:
            connection.close()

    def snapshot(self) -> dict:
        status, result = self.request("GET", "/api/engineer")
        _require(status == 200)
        return result

    def submit(self) -> None:
        status, result = self.request("POST", "/api/engineer/question")
        _require(status == 202 and result.get("accepted") is True)

    def answer(self) -> dict:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            result = self.snapshot()
            if result.get("answer") is not None:
                return result
            time.sleep(0.005)
        raise RuntimeError("ANSWER_TIMEOUT")


@contextmanager
def _harness(**kwargs):
    from iracing_ai_engineer.live_app import make_server

    harness = _Harness(**kwargs)
    try:
        harness.server = make_server(harness.state, port=0, engineer=harness.service)
        # Never print a handler traceback (which can contain host-local paths).
        harness.server.handle_error = lambda request, address: None
        harness.thread = threading.Thread(
            target=harness.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        harness.thread.start()
        harness.token = harness.snapshot()["csrf_token"]
        yield harness
    finally:
        harness.planner.release.set()
        if harness.server is not None:
            if harness.thread is not None and harness.thread.is_alive():
                harness.server.shutdown()
            harness.server.server_close()
        if harness.thread is not None:
            harness.thread.join(1.0)
        harness.service.close()


def _require(condition: bool) -> None:
    if not condition:
        raise RuntimeError("CHECK_FAILED")


def _normal() -> None:
    from iracing_ai_engineer.llm_engineer import fallback_plan, render_plan

    with _harness() as harness:
        _require(harness.request("POST", "/api/engineer/question", token="invalid")[0] == 403)
        _require(not harness.planner.calls)
        harness.submit()
        result = harness.answer()
        _require(len(harness.planner.calls) == 1)
        context = harness.planner.calls[0]
        _require(context["capabilities"]["fuel"] == "ESTIMATE_AVAILABLE")
        ids = {fact["id"] for fact in context["facts"]}
        _require({"fuel.current", "fuel.burn_per_lap", "fuel.range_laps"} <= ids)
        serialized = json.dumps(context)
        for excluded in ("DO_NOT_FORWARD", "SessionInfo", "binding_sha256", "csrf_token"):
            _require(excluded not in serialized)
        expected = render_plan(fallback_plan(context, "How much fuel remains?"), context)
        _require(result["answer"]["text"] == expected)
        _require(result["answer"]["origin"] == "deepseek")  # Fake planner branch only.
        _require(result["answer"]["stale"] is False and result["live_acceptance"] is False)
        _require(result["advisor_only"] is True and result["executable"] is False)


def _invalid_plans() -> None:
    with _harness() as harness:
        for mode in ("malformed", "hallucinated"):
            harness.planner.mode = mode
            harness.submit()
            result = harness.answer()
            _require(result["answer"]["origin"] == "local_fallback")
            _require(result["error"] == "MODEL_UNAVAILABLE_OR_INVALID_PLAN")
            _require("DO_NOT_RENDER" not in result["answer"]["text"])
            _require("invented.fact" not in result["answer"]["text"])
            harness.advance()


def _withdrawal() -> None:
    with _harness() as harness:
        harness.submit()
        original = harness.answer()["answer"]["text"]
        harness.now += 2.01
        stale = harness.snapshot()["answer"]
        _require(stale["stale"] is True and stale["text"] != original)
        harness.refresh()
        _require(harness.snapshot()["answer"]["stale"] is True)
        harness.submit()
        _require(harness.answer()["answer"]["stale"] is False)
        harness.state.connection("DISCONNECTED")
        harness.state.connection("CONNECTED")
        harness.refresh()
        withdrawn = harness.snapshot()["answer"]
        _require(withdrawn["stale"] is True and withdrawn["text"] != original)


def _missing_key() -> None:
    with _harness(missing_key=True) as harness:
        _require(harness.snapshot()["status"] == "MISSING_KEY")
        harness.submit()
        result = harness.answer()
        _require(result["answer"]["origin"] == "local_fallback")
        _require(result["requests_used"] == 0 and not harness.planner.calls)


def _limits() -> None:
    with _harness(request_limit=1) as harness:
        harness.planner.mode = "blocked"
        harness.submit()
        _require(harness.planner.entered.wait(1.0))
        status, result = harness.request("POST", "/api/engineer/question")
        _require(status == 409 and result.get("error") == "BUSY")
        harness.planner.release.set()
        _require(harness.answer()["requests_used"] == 1)
        status, result = harness.request("POST", "/api/engineer/question")
        _require(status == 429 and result.get("error") == "RATE_LIMITED")
        harness.advance()
        _require(harness.snapshot()["status"] == "BUDGET_EXHAUSTED")
        harness.submit()
        result = harness.answer()
        _require(result["answer"]["origin"] == "local_fallback")
        _require(result["requests_used"] == 1 and len(harness.planner.calls) == 1)


def run_rehearsal() -> dict:
    """Return only fixed public-safe check codes, never state or exception text."""
    checks = []
    for check_id, scenario in (
        ("HTTP_FACTS_PRIVACY_CSRF", _normal),
        ("INVALID_PLAN_LOCAL_FALLBACK", _invalid_plans),
        ("STALE_RECONNECT_WITHDRAWAL", _withdrawal),
        ("MISSING_KEY_LOCAL_FALLBACK", _missing_key),
        ("INFLIGHT_RATE_BUDGET_LIMITS", _limits),
    ):
        try:
            scenario()
        except Exception:
            # An unexpected exception is failure, but its text is not public output.
            checks.append({"id": check_id, "status": "FAIL"})
        else:
            checks.append({"id": check_id, "status": "PASS"})
    return _report(checks)


def _report(checks: list[dict]) -> dict:
    return {
        "contract_version": "engineer-llm-rehearsal-v1",
        "source_kind": "SYNTHETIC", "admission_simulation": True,
        "live_acceptance": False, "provider_called": False, "sdk_accessed": False,
        "status": "PASS" if checks and all(c["status"] == "PASS" for c in checks) else "FAIL",
        "checks": checks,
        "limitations": [
            "Invented SDK_LIVE-tagged fixture exercises admission only; no capture is written.",
            "Fake planner and local loopback HTTP only; no keys or external network are used.",
            "Does not validate provider connectivity, simulator telemetry, or race advice.",
        ],
    }


def main() -> int:
    try:
        _bootstrap()
        report = run_rehearsal()
    except Exception:
        report = _report([{"id": "REHEARSAL_SETUP", "status": "FAIL"}])
    print(json.dumps(report, ensure_ascii=True, allow_nan=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
