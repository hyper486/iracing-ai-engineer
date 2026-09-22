from __future__ import annotations

import io
import json
import threading
import time
import traceback
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from iracing_ai_engineer import llm_client
from iracing_ai_engineer.llm_client import DeepSeekClient, LLMError

_KEY = "unit-test-credential-not-real"
_CONTEXT = {
    "facts": [{"id": "fuel.current", "text": "Current fuel: 12 L."}],
    "notices": [{"id": "experimental", "text": "Experimental estimates only."}],
    "capabilities": {"fuel": "READY", "strategy": "UNAVAILABLE"},
}
_PLAN = {"topic": "fuel", "fact_ids": ["fuel.current"], "notice_ids": ["experimental"]}


def _body(content: object = None, *, reason: object = "stop", **message: object) -> bytes:
    return json.dumps({"choices": [{
        "finish_reason": reason,
        "message": {
            "role": "assistant", "content": json.dumps(_PLAN) if content is None else content,
            **message,
        },
    }]}).encode()


class FakeResponse(io.BytesIO):
    def __init__(
        self, body: bytes, *, status: int = 200, headers: dict | None = None,
        url: str = llm_client._ENDPOINT,
    ) -> None:
        super().__init__(body)
        self.status = status
        self.headers = {} if headers is None else headers
        self.url = url
        self.read_sizes: list[int] = []

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status

    def read1(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read1(size)


class FakeOpener:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[tuple[Request, float]] = []

    def open(self, request: Request, *, timeout: float):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _network(monkeypatch, response: FakeResponse | Exception) -> FakeOpener:
    opener = FakeOpener(response)

    def build(*handlers):
        assert len(handlers) == 1
        assert isinstance(handlers[0], llm_client._NoRedirect)
        return opener

    monkeypatch.setattr(llm_client, "build_opener", build)
    return opener


def _error(client: DeepSeekClient, expected: str, context=None, question="How is fuel?"):
    with pytest.raises(LLMError) as captured:
        client.complete(_CONTEXT if context is None else context, question)
    assert captured.value.code == expected
    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None
    assert _KEY not in str(captured.value)
    assert _KEY not in repr(captured.value)
    assert _KEY not in "".join(traceback.format_exception(captured.value))
    return captured.value


def test_fixed_endpoint_nonstreaming_json_plan_and_credential_header_only(monkeypatch) -> None:
    response = FakeResponse(_body(reasoning_content="Ignored internal reasoning"))
    opener = _network(monkeypatch, response)
    client = DeepSeekClient(_KEY)
    assert client.complete(_CONTEXT, "Where is my fuel?") == _PLAN
    request, timeout = opener.calls[0]
    assert request.full_url == "https://api.deepseek.com/chat/completions"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {_KEY}"
    assert _KEY.encode() not in request.data
    assert request.get_header("Accept-encoding") == "identity"
    payload = json.loads(request.data)
    assert payload["model"] == "deepseek-flash"
    assert payload["max_tokens"] == 512
    assert payload["stream"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 0
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert len(payload["messages"]) == 2
    assert payload["messages"][0]["role"] == "system"
    assert "at most 6" in payload["messages"][0]["content"]
    assert "at most 4" in payload["messages"][0]["content"]
    assert "untrusted data" in payload["messages"][0]["content"]
    assert json.loads(payload["messages"][1]["content"])["context"] == _CONTEXT
    assert 0 < timeout <= 12
    assert response.closed
    assert max(response.read_sizes) <= llm_client._READ_CHUNK_BYTES
    assert _KEY not in repr(client)


def test_model_is_validated_configurable_identifier_not_hardcoded_allowlist(monkeypatch) -> None:
    opener = _network(monkeypatch, FakeResponse(_body()))
    client = DeepSeekClient(_KEY, model="future-model_v2.1", max_tokens=1024, timeout_s=3)
    assert client.complete(_CONTEXT, "Status?") == _PLAN
    payload = json.loads(opener.calls[0][0].data)
    assert payload["model"] == "future-model_v2.1"
    assert payload["max_tokens"] == 1024
    assert opener.calls[0][1] <= 3


@pytest.mark.parametrize("updates", [
    {"api_key": ""}, {"api_key": None}, {"api_key": " key"},
    {"api_key": "key\r\nX-Foo: bad"}, {"api_key": "密钥"}, {"api_key": "x" * 513},
    {"model": ""}, {"model": "https://other.invalid"}, {"model": "x\ny"},
    {"model": "x" * 65}, {"model": True},
    {"timeout_s": 0}, {"timeout_s": 31}, {"timeout_s": float("nan")},
    {"timeout_s": float("inf")}, {"timeout_s": True},
    {"max_tokens": 0}, {"max_tokens": 1025}, {"max_tokens": True}, {"max_tokens": 512.0},
])
def test_config_errors_are_sanitized(updates: dict) -> None:
    with pytest.raises(LLMError, match="^LLM_CONFIG_INVALID$"):
        DeepSeekClient(**{"api_key": _KEY, **updates})


@pytest.mark.parametrize("code,expected", [
    (301, "LLM_REDIRECT_BLOCKED"), (302, "LLM_REDIRECT_BLOCKED"),
    (303, "LLM_REDIRECT_BLOCKED"), (307, "LLM_REDIRECT_BLOCKED"),
    (308, "LLM_REDIRECT_BLOCKED"), (401, "LLM_HTTP_AUTH"),
    (403, "LLM_HTTP_AUTH"), (429, "LLM_RATE_LIMIT"),
    (400, "LLM_HTTP_ERROR"), (500, "LLM_HTTP_ERROR"),
])
def test_http_errors_have_no_body_or_secret_and_are_not_retried(
    monkeypatch, code, expected,
) -> None:
    body = io.BytesIO(f"private provider body {_KEY}".encode())
    failure = HTTPError(
        f"https://example.invalid/{_KEY}", code, f"private message {_KEY}", {}, body,
    )
    opener = _network(monkeypatch, failure)
    _error(DeepSeekClient(_KEY), expected)
    assert len(opener.calls) == 1
    assert body.closed


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_handler_never_builds_redirected_authorization_request(code: int) -> None:
    original = Request(llm_client._ENDPOINT, headers={"Authorization": f"Bearer {_KEY}"})
    handler = llm_client._NoRedirect()
    assert handler.redirect_request(
        original, None, code, "redirect", {}, "https://example.invalid/collect",
    ) is None


def test_changed_response_url_is_rejected_even_if_custom_handler_returned_success(
    monkeypatch,
) -> None:
    response = FakeResponse(_body(), url="https://example.invalid/redirected")
    _network(monkeypatch, response)
    _error(DeepSeekClient(_KEY), "LLM_REDIRECT_BLOCKED")
    assert response.read_sizes == []
    assert response.closed


@pytest.mark.parametrize("failure,expected", [
    (TimeoutError(f"private {_KEY}"), "LLM_TIMEOUT"),
    (URLError(TimeoutError(f"private {_KEY}")), "LLM_TIMEOUT"),
    (URLError(f"private {_KEY}"), "LLM_NETWORK_ERROR"),
    (RuntimeError(f"private {_KEY}"), "LLM_NETWORK_ERROR"),
    (IncompleteRead(b"private partial body"), "LLM_RESPONSE_INCOMPLETE"),
])
def test_transport_errors_do_not_expose_provider_details(monkeypatch, failure, expected) -> None:
    _network(monkeypatch, failure)
    _error(DeepSeekClient(_KEY), expected)


@pytest.mark.parametrize("content,expected", [
    ("", "LLM_CONTENT_EMPTY"), (" \n\t", "LLM_CONTENT_EMPTY"),
    ("not json", "LLM_RESPONSE_INVALID"),
    ('{"topic":"fuel","topic":"status"}', "LLM_RESPONSE_INVALID"),
    ('{"fact_ids":[NaN]}', "LLM_RESPONSE_INVALID"),
    ('{"value":1e999}', "LLM_RESPONSE_INVALID"),
    ('{"value":Infinity}', "LLM_RESPONSE_INVALID"),
    ('{"value":-Infinity}', "LLM_RESPONSE_INVALID"),
    ("[]", "LLM_RESPONSE_INVALID"), ("null", "LLM_RESPONSE_INVALID"),
    ("true", "LLM_RESPONSE_INVALID"), (1, "LLM_RESPONSE_INVALID"),
    ([], "LLM_RESPONSE_INVALID"),
    ('{"value":"\\ud800"}', "LLM_RESPONSE_INVALID"),
    ('{"nested":{"same":1,"same":2}}', "LLM_RESPONSE_INVALID"),
    (json.dumps({"secret": _KEY}), "LLM_SENSITIVE_CONTENT"),
    ('{"topic":"fuel"' + " " * llm_client._MAX_CONTENT_BYTES, "LLM_CONTENT_TOO_LARGE"),
], ids=lambda value: type(value).__name__)
def test_invalid_or_sensitive_planner_content_never_returns(monkeypatch, content, expected) -> None:
    _network(monkeypatch, FakeResponse(_body(content)))
    _error(DeepSeekClient(_KEY), expected)


@pytest.mark.parametrize("reason", [None, "length", "tool_calls", "content_filter", "error"])
def test_only_complete_stop_reason_is_accepted(monkeypatch, reason) -> None:
    _network(monkeypatch, FakeResponse(_body(reason=reason)))
    _error(DeepSeekClient(_KEY), "LLM_RESPONSE_INCOMPLETE")


@pytest.mark.parametrize("body", [
    b"", b"<html>Bad gateway</html>", b"\xff",
    b'{"choices":[],"choices":[]}', b'{"choices":[],"value":NaN}',
    b'{"choices":[],"value":1e999}', b'{"choices":[]}', b'{"choices":{}}',
    b'{"choices":[{},{}]}', b'{"choices":[1]}',
    b'{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":null}}]}',
    _body(role="user"), _body(tool_calls=[{"name": "do_something"}]),
], ids=lambda value: type(value).__name__)
def test_invalid_provider_envelopes_fail_closed(monkeypatch, body) -> None:
    _network(monkeypatch, FakeResponse(body))
    _error(DeepSeekClient(_KEY), "LLM_RESPONSE_INVALID")


def test_reasoning_content_is_ignored_and_never_used_as_content_fallback(monkeypatch) -> None:
    response = FakeResponse(_body(reasoning_content=_KEY))
    _network(monkeypatch, response)
    assert DeepSeekClient(_KEY).complete(_CONTEXT, "Fuel?") == _PLAN
    _network(monkeypatch, FakeResponse(_body("", reasoning_content=json.dumps(_PLAN))))
    _error(DeepSeekClient(_KEY), "LLM_CONTENT_EMPTY")


def test_schema_authorization_is_left_to_local_service_not_assumed_from_json(monkeypatch) -> None:
    # Transport returns JSON, not a trusted response. The root-owned schema and
    # local renderer must reject unsupported fields/IDs before displaying anything.
    parsed = {"unsupported_field": "untrusted model text"}
    _network(monkeypatch, FakeResponse(_body(json.dumps(parsed))))
    assert DeepSeekClient(_KEY).complete(_CONTEXT, "Status?") == parsed


@pytest.mark.parametrize("headers,body,expected", [
    ({"Content-Length": "65537"}, b"", "LLM_RESPONSE_TOO_LARGE"),
    ({"Content-Length": "bad"}, b"", "LLM_RESPONSE_INVALID"),
    ({"Content-Length": "-1"}, b"", "LLM_RESPONSE_INVALID"),
    ({"Content-Length": "9999999999999999999"}, b"", "LLM_RESPONSE_INVALID"),
    ({"Content-Length": "100"}, b"{}", "LLM_RESPONSE_INCOMPLETE"),
    ({"Content-Length": "1"}, b"{}", "LLM_RESPONSE_INCOMPLETE"),
    ({"Content-Encoding": "gzip"}, b"compressed", "LLM_RESPONSE_INVALID"),
    ({}, b" " * 65537, "LLM_RESPONSE_TOO_LARGE"),
], ids=lambda value: type(value).__name__)
def test_response_size_encoding_and_truncation_bounds(monkeypatch, headers, body, expected) -> None:
    response = FakeResponse(body, headers=headers)
    _network(monkeypatch, response)
    _error(DeepSeekClient(_KEY), expected)
    assert response.closed
    assert all(0 < size <= 4096 for size in response.read_sizes)


@pytest.mark.parametrize("context,question,expected", [
    ([], "Fuel?", "LLM_INPUT_INVALID"), ({}, "", "LLM_INPUT_INVALID"),
    ({}, " \n", "LLM_INPUT_INVALID"), ({}, True, "LLM_INPUT_INVALID"),
    ({}, "问" * 1400, "LLM_INPUT_TOO_LARGE"),
    ({"value": "x" * 65537}, "Fuel?", "LLM_INPUT_TOO_LARGE"),
    ({"value": float("nan")}, "Fuel?", "LLM_INPUT_INVALID"),
    ({1: "bad key"}, "Fuel?", "LLM_INPUT_INVALID"),
    ({"value": object()}, "Fuel?", "LLM_INPUT_INVALID"),
    ({"value": _KEY}, "Fuel?", "LLM_SENSITIVE_CONTENT"),
    ({}, f"Here is {_KEY}", "LLM_SENSITIVE_CONTENT"),
    ({}, "\ud800", "LLM_INPUT_INVALID"),
    ({"values": [1] * 4097}, "Fuel?", "LLM_INPUT_INVALID"),
], ids=lambda value: type(value).__name__)
def test_input_bounds_fail_before_any_network(monkeypatch, context, question, expected) -> None:
    opener = _network(monkeypatch, FakeResponse(_body()))
    _error(DeepSeekClient(_KEY), expected, context=context, question=question)
    assert opener.calls == []


def test_cyclic_and_excessively_nested_inputs_are_rejected(monkeypatch) -> None:
    opener = _network(monkeypatch, FakeResponse(_body()))
    cyclic = {}
    cyclic["value"] = cyclic
    _error(DeepSeekClient(_KEY), "LLM_INPUT_INVALID", context=cyclic)
    assert opener.calls == []


def test_exact_response_and_content_byte_limits_are_allowed(monkeypatch) -> None:
    content = json.dumps(_PLAN)
    content += " " * (llm_client._MAX_CONTENT_BYTES - len(content.encode()))
    body = _body(content)
    body += b" " * (llm_client._MAX_RESPONSE_BYTES - len(body))
    response = FakeResponse(body, headers={"Content-Length": str(len(body))})
    _network(monkeypatch, response)
    assert DeepSeekClient(_KEY).complete(_CONTEXT, "Fuel?") == _PLAN
    assert response.closed


def test_credential_cannot_be_sent_as_a_model_identifier(monkeypatch) -> None:
    opener = _network(monkeypatch, FakeResponse(_body()))
    _error(DeepSeekClient(_KEY, model=_KEY), "LLM_SENSITIVE_CONTENT")
    assert opener.calls == []


def test_wall_deadline_covers_stalled_open_and_prevents_replacement_workers(monkeypatch) -> None:
    began = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    calls: list[object] = []

    class Response(FakeResponse):
        def close(self) -> None:
            super().close()
            closed.set()

    class SlowOpener:
        def open(self, request, *, timeout):
            calls.append(request)
            began.set()
            release.wait(2)
            return Response(_body())

    monkeypatch.setattr(llm_client, "build_opener", lambda *args: SlowOpener())
    client = DeepSeekClient(_KEY, timeout_s=0.05)
    started = time.perf_counter()
    try:
        _error(client, "LLM_TIMEOUT")
        assert began.is_set()
        assert time.perf_counter() - started < 0.75
        _error(client, "LLM_BUSY")
        assert len(calls) == 1
    finally:
        release.set()
    assert closed.wait(1)


def test_slow_drip_is_bounded_by_overall_deadline_and_releases_response(monkeypatch) -> None:
    closed = threading.Event()

    class Drip(FakeResponse):
        def read1(self, size: int = -1) -> bytes:
            time.sleep(0.005)
            return super().read1(1)

        def close(self) -> None:
            super().close()
            closed.set()

    _network(monkeypatch, Drip(_body()))
    client = DeepSeekClient(_KEY, timeout_s=0.05)
    started = time.perf_counter()
    _error(client, "LLM_TIMEOUT")
    assert time.perf_counter() - started < 0.75
    assert closed.wait(1)


@pytest.mark.parametrize("phase", ["parse", "close"])
def test_total_deadline_includes_parsing_and_nonblocking_caller_cleanup(monkeypatch, phase) -> None:
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    original_parse = llm_client._parse_object

    def wait_for_release() -> None:
        entered.set()
        release.wait(2)

    def slow_parse(value):
        wait_for_release()
        return original_parse(value)

    class Response(FakeResponse):
        def close(self) -> None:
            if phase == "close":
                wait_for_release()
            super().close()
            exited.set()

    if phase == "parse":
        monkeypatch.setattr(llm_client, "_parse_object", slow_parse)
    _network(monkeypatch, Response(_body()))
    client = DeepSeekClient(_KEY, timeout_s=0.05)
    started = time.perf_counter()
    try:
        _error(client, "LLM_TIMEOUT")
        assert entered.is_set()
        assert time.perf_counter() - started < 0.75
        _error(client, "LLM_BUSY")
    finally:
        release.set()
    assert exited.wait(1)


def test_unknown_error_message_is_not_echoed_and_client_repr_redacts_credentials() -> None:
    assert LLMError(_KEY).code == "LLM_ERROR"
    assert _KEY not in repr(LLMError(_KEY))
    assert _KEY not in repr(DeepSeekClient(_KEY))
