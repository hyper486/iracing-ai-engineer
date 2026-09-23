"""Synthetic loopback/in-process tests; never open a simulator SDK or audio device."""

from __future__ import annotations

import copy
import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from iracing_ai_engineer import cli, live_app
from iracing_ai_engineer.live_fuel import LiveFuelConfig


def _snapshot():
    return {
        "source_kind": "SDK_LIVE",
        "status": "READY",
        "sequence": 0,
        "context": {"player_control_state": "IN_CAR_PHYSICS", "sim_source_mode": "FULL"},
        "quality": {"stale": False},
        "interval_invalid_for_fuel": [],
        "interval_unsafe_for_speech": [],
        "telemetry": {
            "brake": 0.0,
            "steering_angle_rad": 0.0,
            "speed_mps": 40.0,
            "car_left_right": 1,
            "on_pit_road": False,
        },
    }


def _fuel():
    return {
        "status": "READY",
        "current_fuel_l": 20.0,
        "estimated_laps_remaining": 8.0,
        "valid_laps": 5,
    }


def test_mailbox_expires_without_any_more_producer_calls_and_copies_inputs():
    clock = [10.0]
    state = live_app.AppState(clock=lambda: clock[0])
    state.connection("CONNECTED")
    monitor, fuel = _snapshot(), _fuel()
    state.publish(monitor, fuel, None, "Practice")
    monitor["telemetry"]["brake"] = 1
    assert state.snapshot()["monitor"]["telemetry"]["brake"] == 0
    clock[0] = 12
    assert state.snapshot()["connection"] == "CONNECTED"
    clock[0] = 12.00001
    result = state.snapshot()
    assert result["connection"] == "DISCONNECTED"
    assert result["monitor"] is result["fuel"] is result["speech"] is None
    assert state.report()["max_valid_fuel_laps"] == 5


def test_disconnect_clears_estimates_and_connect_uses_new_generation():
    state = live_app.AppState()
    state.connection("CONNECTED")
    state.publish(_snapshot(), _fuel(), None, "Race")
    generation = state.snapshot()["generation"]
    state.connection("DISCONNECTED")
    assert state.snapshot()["fuel"] is None
    state.connection("CONNECTED")
    assert state.snapshot()["generation"] > generation
    assert state.snapshot()["monitor"] is None


@pytest.mark.parametrize("status,reason,failed,expected", [
    ("DRAINING", "SOURCE_ENDED", False, "RESTART_REQUIRED"),
    ("INCOMPLETE", "SOURCE_ENDED", False, "RESTART_REQUIRED"),
    ("COMPLETE", "LIMIT_REACHED", False, "LIMIT_REACHED"),
    ("ERROR", "CLOSE_FAILED", True, "ERROR"),
])
def test_old_recorder_health_is_not_current_capture(status, reason, failed, expected):
    state = live_app.AppState()
    state.connection("CONNECTED")
    row = {"status": status, "reason": reason, "failed": failed, "done": status != "DRAINING",
           "bytes": 13, "processed_frames": 1}
    state.attach_worker("recording", SimpleNamespace(snapshot=lambda: dict(row)),
                        recording_base_bytes=100)
    state.connection("DISCONNECTED")
    state.connection("CONNECTED")
    assert state.snapshot()["recording"] == {"status": expected, "bytes": 113}
    assert state.report()["recording_bytes"] == 113


def test_analysis_health_withdraws_existing_data_without_relying_on_failure_callback():
    state = live_app.AppState()
    state.connection("CONNECTED")
    state.publish(_snapshot(), _fuel(), None, "Race")
    row = {"status": "ERROR", "reason": "PROCESSING_FAILED", "failed": True, "done": False}
    state.attach_worker("analysis", SimpleNamespace(snapshot=lambda: dict(row)))
    result = state.snapshot()
    assert result["transport_connection"] == "CONNECTED"
    assert result["monitor"] is result["fuel"] is result["speech"] is None
    assert result["updated_age_s"] is None and result["connection"] == "DISCONNECTED"


def test_speech_is_practice_fact_only_safe_window_cooldown_and_no_queue():
    policy = live_app.PracticeFuelSpeech()
    monitor, fuel = _snapshot(), _fuel()
    assert policy.update(monitor, fuel, "Practice", 10) is None
    assert policy.update(monitor, fuel, "Practice", 11.999) is None
    first = policy.update(monitor, fuel, "Practice", 12)
    assert "estimate" in first["text"] and "box" not in first["text"].lower()
    assert policy.update(monitor, fuel, "Practice", 12.5) == first
    assert policy.update(monitor, fuel, "Practice", 14) is None
    assert policy.update(monitor, fuel, "Race", 72) is None
    assert policy.update(monitor, fuel, "Practice", 73) is None
    assert policy.update(monitor, fuel, "Practice", 75)["id"] != first["id"]


@pytest.mark.parametrize(
    "change",
    [
        {"brake": 0.03},
        {"steering_angle_rad": 0.1},
        {"speed_mps": 2},
        {"car_left_right": None},
        {"car_left_right": 2},
        {"on_pit_road": True},
    ],
)
def test_unsafe_road_state_cancels_active_speech(change):
    policy = live_app.PracticeFuelSpeech()
    snapshot = _snapshot()
    policy.update(snapshot, _fuel(), "Practice", 1)
    assert policy.update(snapshot, _fuel(), "Practice", 3)
    snapshot["telemetry"].update(change)
    assert policy.update(snapshot, _fuel(), "Practice", 3.1) is None


def test_mailbox_speech_deadline_not_extended_by_http_reads():
    clock = [10.0]
    state = live_app.AppState(clock=lambda: clock[0])
    state.connection("CONNECTED")
    intent = {"id": "1", "text": "Fuel estimate.", "priority": "INFORMATION", "deadline": 11}
    state.publish(_snapshot(), _fuel(), intent, "Practice")
    assert state.snapshot()["speech"]["expires_in_s"] == 1
    clock[0] = 11
    assert state.snapshot()["speech"] is None
    assert intent["deadline"] == 11


def test_unsafe_intermediate_tick_restarts_speech_safe_window():
    policy = live_app.PracticeFuelSpeech()
    snapshot = _snapshot()
    assert policy.update(snapshot, _fuel(), "Practice", 1) is None
    snapshot["interval_unsafe_for_speech"] = ["BRAKING_OR_UNKNOWN"]
    assert policy.update(snapshot, _fuel(), "Practice", 2.5) is None
    snapshot["interval_unsafe_for_speech"] = []
    assert policy.update(snapshot, _fuel(), "Practice", 3) is None
    assert policy.update(snapshot, _fuel(), "Practice", 4.9) is None
    assert policy.update(snapshot, _fuel(), "Practice", 5) is not None
    snapshot.pop("interval_unsafe_for_speech")
    assert policy.update(snapshot, _fuel(), "Practice", 5.1) is None


def test_session_type_requires_exact_update_unique_matching_numeric_session():
    frame = SimpleNamespace(session_info_update=3, values={"SessionNum": 2})
    payload = {"SessionInfo": {"Sessions": [{"SessionNum": 2, "SessionType": "Race"}]}}
    assert live_app.bound_session_type(payload, 3, frame) == "Race"
    assert live_app.bound_session_type(payload, 4, frame) is None
    assert live_app.bound_session_type(None, 3, frame) is None
    duplicate = copy.deepcopy(payload)
    duplicate["SessionInfo"]["Sessions"] *= 2
    assert live_app.bound_session_type(duplicate, 3, frame) is None
    payload["SessionInfo"]["Sessions"][0]["SessionNum"] = "2"
    assert live_app.bound_session_type(payload, 3, frame) is None


@pytest.fixture
def server():
    state = live_app.AppState()
    server = live_app.make_server(state, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _request(server, path="/api/state", *, method="GET", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_loopback_only_routes_and_csp(server):
    service, _ = server
    assert service.server_address[0] == "127.0.0.1"
    code, headers, body = _request(service)
    assert code == 200 and json.loads(body)["connection"] == "WAIT_SIM"
    assert headers["Cache-Control"] == "no-store"
    assert "'sha256-" in headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert not any(name.startswith("Access-Control") for name in headers)
    assert _request(service, "/")[0] == 200
    assert _request(service, "/api/state", method="HEAD")[2] == b""
    assert _request(service, "/api/report")[0] == 200
    assert _request(service, "/../pyproject.toml")[0] == 404
    assert _request(service, method="POST")[0] == 405


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"Origin": "null"},
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_http_rejects_rebinding_or_cross_origin_requests(server, headers):
    assert _request(server[0], headers=headers)[0] == 403


def test_failed_sdk_retries_clear_state_and_never_publish_exception_text():
    state = live_app.AppState()
    state.publish(_snapshot(), _fuel(), None, "Race")
    attempts = []

    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            assert seconds == 5
            self.stopped = True

    def factory():
        attempts.append(1)
        raise RuntimeError("synthetic private exception must never be sent to HTTP")

    live_app.run_reader(state, Stop(), LiveFuelConfig(), transport_factory=factory)
    assert len(attempts) == 1
    result = state.snapshot()
    assert result["connection"] == "STOPPED" and result["fuel"] is None
    assert "private exception" not in json.dumps(result)


def test_live_app_cli_maps_settings_without_starting_sdk(monkeypatch, capsys, tmp_path):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        kwargs["on_ready"]("http://127.0.0.1:8765/")

    monkeypatch.setattr(live_app, "run_live_app", run)
    assert (
        cli.main(
            [
                "live-app",
                "--duration-seconds",
                "10",
                "--reserve-liters",
                "3",
                "--record-directory",
                str(tmp_path),
                "--tank-capacity-liters",
                "90",
            ]
        )
        == 0
    )
    assert calls[0]["config"].reserve_l == 3
    assert calls[0]["config"].tank_capacity_l == 90
    assert calls[0]["record_directory"] == tmp_path
    assert json.loads(capsys.readouterr().out)["race_speech_enabled"] is False


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), 43201, True])
def test_app_rejects_unbounded_duration_without_starting_server(duration):
    with pytest.raises(ValueError):
        live_app.run_live_app(duration_s=duration)
