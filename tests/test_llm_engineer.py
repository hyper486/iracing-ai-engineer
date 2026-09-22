"""Synthetic model/service/loopback checks; no provider or simulator is contacted."""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from iracing_ai_engineer import cli
from iracing_ai_engineer.live_app import AppState, make_server
from iracing_ai_engineer.llm_engineer import (
    EngineerConfig,
    EngineerService,
    fallback_plan,
    render_plan,
    validate_plan,
)


def monitor():
    return {
        "contract_version": "live-monitor-v1", "record_type": "live_monitor_snapshot",
        "advisor_only": True, "executable": False,
        "source_kind": "SDK_LIVE", "status": "READY", "sequence": 1,
        "binding_sha256": "synthetic-binding-not-authentic",
        "context": {"player_control_state": "IN_CAR_PHYSICS", "sim_source_mode": "FULL",
                    "conflicts": []},
        "quality": {"status": "READY", "stale": False}, "interval_invalid_for_fuel": [],
        "telemetry": {"session_num": 0, "lap_number": 8, "on_pit_road": False},
    }


def fuel():
    return {
        "status": "READY", "current_fuel_l": 20.0, "valid_laps": 5, "required_laps": 5,
        "conservative_burn_l_per_lap": 2.0, "estimated_laps_remaining": 9,
        "fuel_needed_to_finish_l": 30.0, "fuel_to_add_l": 10.0, "minimum_stops": 1,
        "estimate_only": True, "advisor_only": True, "executable": False,
    }


def ready_state(clock=None):
    state = AppState(**({"clock": clock} if clock else {}))
    state.connection("CONNECTED")
    state.publish(monitor(), fuel(), None, "Race")
    return state


def wait_answer(service):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = service.snapshot()
        if result["answer"]:
            return result
        time.sleep(0.005)
    raise AssertionError("synthetic service did not finish")


class Planner:
    def __init__(self):
        self.calls = []

    def complete(self, context, question):
        self.calls.append((context, question))
        return fallback_plan(context, question)


@pytest.fixture
def services():
    created = []

    def make(source=None, **kwargs):
        service = EngineerService(source or ready_state().snapshot, **kwargs)
        created.append(service)
        return service

    yield make
    for service in created:
        service.close()


def test_cloud_plan_only_can_select_locally_computed_facts(services):
    client = Planner()
    service = services(config=EngineerConfig(provider="deepseek"), client=client)
    assert service.submit("还有多少油？")[0] == 202
    result = wait_answer(service)
    assert result["answer"]["origin"] == "deepseek"
    assert "20" in result["answer"]["text"]
    assert result["requests_used"] == 1 and result["live_acceptance"] is False
    assert result["answer"]["stale"] is False
    context = client.calls[0][0]
    assert "binding_sha256" not in str(context)
    assert "SessionInfo" not in str(context) and "csrf_token" not in str(context)


@pytest.mark.parametrize("provider", ["off", "deepseek"])
def test_disabled_or_missing_key_still_answers_locally_without_network(services, provider):
    service = services(config=EngineerConfig(provider=provider), environ={})
    assert service.snapshot()["status"] == ("DISABLED" if provider == "off" else "MISSING_KEY")
    assert service.submit("为什么不能给我进站策略？")[0] == 202
    result = wait_answer(service)
    assert result["answer"]["origin"] == "local_fallback"
    assert result["requests_used"] == 0


def test_invalid_key_configuration_is_safe_local_fallback(services):
    service = services(config=EngineerConfig(provider="deepseek"),
                       environ={"DEEPSEEK_API_KEY": "invalid header\nnot a real key"})
    assert service.snapshot()["status"] == "ERROR"
    assert service.snapshot()["error"] == "MODEL_CONFIGURATION_INVALID"
    service.submit("fuel")
    result = wait_answer(service)
    assert result["answer"]["origin"] == "local_fallback"
    assert result["requests_used"] == 0


def test_utf8_token_is_rejected_without_exception(services):
    assert services().authenticates("不合法") is False


def test_provider_failure_has_safe_fallback_and_no_error_body_leak(services):
    class Broken:
        def complete(self, context, question):
            raise RuntimeError("PRIVATE_NATIVE_PATH_AND_CREDENTIAL")

    service = services(config=EngineerConfig(provider="deepseek"), client=Broken())
    service.submit("fuel")
    result = wait_answer(service)
    assert result["answer"]["origin"] == "local_fallback"
    assert result["error"] == "MODEL_UNAVAILABLE_OR_INVALID_PLAN"
    assert "PRIVATE_NATIVE" not in str(result)


@pytest.mark.parametrize("plan", [
    None, [], {}, {"topic": "fuel", "fact_ids": ["invented"], "notice_ids": []},
    {"topic": "fuel", "fact_ids": [], "notice_ids": [], "text": "Box now"},
    {"topic": "controls", "fact_ids": [], "notice_ids": []},
    {"topic": "fuel", "fact_ids": [True], "notice_ids": []},
    {"topic": "fuel", "fact_ids": ["fuel.current", "fuel.current"], "notice_ids": []},
    {"topic": "fuel", "fact_ids": [], "notice_ids": ["invented"]},
])
def test_invalid_plan_cannot_publish_model_prose(plan):
    context = {"facts": [{"id": "fuel.current", "text": "20 L"}], "notices": []}
    with pytest.raises(ValueError, match="INVALID_PLAN"):
        validate_plan(plan, context)


def test_boundary_notices_cannot_be_removed_by_model():
    context = {
        "scope": "historical_session", "facts": [],
        "notices": [{"id": "WAIT", "text": "Mandatory limitation."}],
    }
    text = render_plan({"topic": "status", "fact_ids": [], "notice_ids": []}, context)
    assert "Mandatory limitation." in text and "历史复盘" in text


def test_single_inflight_request_does_not_block_sdk_publish_and_is_invalidated(services):
    entered, release = threading.Event(), threading.Event()

    class Delayed(Planner):
        def complete(self, context, question):
            entered.set()
            assert release.wait(2)
            return super().complete(context, question)

    state = ready_state()
    service = services(state.snapshot, config=EngineerConfig(provider="deepseek"), client=Delayed())
    service.submit("fuel")
    assert entered.wait(1)
    assert service.snapshot()["status"] == "BUSY"
    assert service.submit("another")[0] == 409
    before = time.monotonic()
    state.connection("DISCONNECTED")
    state.connection("CONNECTED")
    state.publish(monitor(), fuel(), None, "Race")
    assert time.monotonic() - before < 0.2
    release.set()
    result = wait_answer(service)
    assert result["answer"]["stale"] is True
    assert "20" not in result["answer"]["text"]


@pytest.mark.parametrize("change", ["lap", "refuel", "intermediate_hazard", "session", "stale"])
def test_answer_withdrawal_on_safety_change_even_after_recovery(services, change):
    clock = [100.0]
    state = ready_state(lambda: clock[0])
    service = services(state.snapshot, clock=lambda: clock[0])
    service.submit("fuel")
    assert wait_answer(service)["answer"]["stale"] is False
    updated, estimate = monitor(), fuel()
    if change == "lap":
        updated["telemetry"]["lap_number"] += 1
    elif change == "refuel":
        estimate["current_fuel_l"] = 30
    elif change == "intermediate_hazard":
        updated["interval_invalid_for_fuel"] = ["PIT_ROAD"]
    elif change == "session":
        updated["telemetry"]["session_num"] = 2
    elif change == "stale":
        clock[0] += 2.01
    if change != "stale":
        state.publish(updated, estimate, None, "Race")
    if change == "intermediate_hazard":
        state.publish(monitor(), fuel(), None, "Race")
    answer = service.snapshot()["answer"]
    assert answer["stale"] is True and "20" not in answer["text"]


def test_quiet_gap_invalidation_is_retained_even_without_http_poll(services):
    clock = [1.0]
    state = ready_state(lambda: clock[0])
    service = services(state.snapshot, clock=lambda: clock[0])
    service.submit("fuel")
    wait_answer(service)
    clock[0] += 3
    state.publish(monitor(), fuel(), None, "Race")
    assert service.snapshot()["answer"]["stale"] is True


def test_normal_updates_do_not_invalidate_every_half_second_but_ttl_does(services):
    clock = [0.0]
    state = ready_state(lambda: clock[0])
    service = services(state.snapshot, clock=lambda: clock[0])
    service.submit("fuel")
    wait_answer(service)
    clock[0] = 1.0
    estimate = fuel()
    estimate["current_fuel_l"] = 19.9
    updated = monitor()
    updated["sequence"] = 2
    updated["quality"].update(status="DEGRADED", dropped_ticks=1, issues=["DROPPED_TICKS"])
    updated["status"] = "DEGRADED"
    state.publish(updated, estimate, None, "Race")
    assert service.snapshot()["answer"]["stale"] is False
    clock[0] = 30.001
    state.publish(updated, estimate, None, "Race")
    assert service.snapshot()["answer"]["stale"] is True


def test_attempt_limit_rate_limit_and_local_fallback_after_budget(services):
    clock = [1.0]
    client = Planner()
    service = services(config=EngineerConfig(provider="deepseek", request_limit=1),
                       client=client, clock=lambda: clock[0])
    service.submit("fuel")
    wait_answer(service)
    assert service.submit("fuel")[0] == 429
    clock[0] += 10
    assert service.snapshot()["status"] == "BUDGET_EXHAUSTED"
    service.submit("fuel")
    assert wait_answer(service)["answer"]["origin"] == "local_fallback"
    assert len(client.calls) == 1


@pytest.mark.parametrize("question,scope", [
    (None, "live"), ("", "live"), (" " * 600, "live"), ("a" * 501, "live"),
    ("hello\x00", "live"), ("fuel", "wrong"), ([], "live"), ("fuel", []),
])
def test_bad_question_rejected_before_work(services, question, scope):
    service = services()
    assert service.submit(question, scope)[0] == 400
    assert service.snapshot()["requests_used"] == 0


def test_session_missing_and_stopped_service_reject(services):
    service = services()
    assert service.submit("driving", "session")[0] == 409
    service.close()
    assert service.submit("fuel")[0] == 409


@pytest.fixture
def http_server(services):
    state = ready_state()
    service = services(state.snapshot)
    server = make_server(state, port=0, engineer=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, service
    server.shutdown()
    server.server_close()
    thread.join(1)


def request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def question_headers(server, service):
    return {"Origin": f"http://127.0.0.1:{server.server_port}",
            "Content-Type": "application/json",
            "X-Engineer-Token": service.snapshot()["csrf_token"]}


def test_loopback_round_trip_and_state_excludes_credentials(http_server):
    server, service = http_server
    status, body = request(server, "GET", "/api/engineer")
    assert status == 200 and json.loads(body)["status"] == "DISABLED"
    status, body = request(server, "POST", "/api/engineer/question",
                           json.dumps({"question": "fuel"}), question_headers(server, service))
    assert status == 202
    assert wait_answer(service)["answer"]["origin"] == "local_fallback"
    status, body = request(server, "GET", "/api/report")
    assert "csrf_token" not in body.decode() and "DEEPSEEK_API_KEY" not in body.decode()
    assert status == 200


@pytest.mark.parametrize("changes,expected", [
    ({"Origin": "https://untrusted.example.invalid"}, 403),
    ({"Origin": None}, 403), ({"X-Engineer-Token": "wrong"}, 403),
    ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Host": "untrusted.example.invalid"}, 403),
    ({"Content-Type": "text/plain"}, 400), ({"Transfer-Encoding": "chunked"}, 400),
])
def test_cross_origin_csrf_and_body_type_refused(http_server, changes, expected):
    server, service = http_server
    headers = question_headers(server, service)
    headers.update(changes)
    headers = {key: value for key, value in headers.items() if value is not None}
    status, _ = request(server, "POST", "/api/engineer/question", '{"question":"fuel"}', headers)
    assert status == expected
    assert service.snapshot()["answer"] is None


@pytest.mark.parametrize("body", [
    '{"question":"fuel","question":"pit"}', '[]', '{"question":"fuel","path":"private"}',
    '{"question":false}', '{"question":"' + 'x' * 4096 + '"}',
])
def test_bad_post_payload_refused(http_server, body):
    server, service = http_server
    assert request(server, "POST", "/api/engineer/question", body,
                   question_headers(server, service))[0] == 400


def test_cli_llm_options_only_configure_backend(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "iracing_ai_engineer.live_app.run_live_app", lambda **kwargs: calls.append(kwargs)
    )
    assert cli.main(["live-app", "--llm-provider", "deepseek", "--llm-request-limit", "20",
                     "--llm-model", "deepseek-flash"]) == 0
    config = calls[0]["engineer_config"]
    assert config.provider == "deepseek" and config.request_limit == 20
    assert "api_key" not in repr(config)


@pytest.mark.parametrize("kwargs", [
    {"provider": "arbitrary"}, {"request_limit": 0}, {"request_limit": True},
    {"min_interval_s": 0}, {"timeout_s": 31}, {"timeout_s": float("nan")},
    {"model": "private\ncredential"},
])
def test_bad_config_refused(kwargs):
    with pytest.raises(ValueError):
        EngineerConfig(**kwargs)
