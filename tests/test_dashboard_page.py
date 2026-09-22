"""Browser-free checks of the static page and its fail-closed JavaScript state."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from iracing_ai_engineer.dashboard_page import DASHBOARD_HTML


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def test_page_is_self_contained_read_only_and_accessible() -> None:
    page = _PageParser()
    page.feed(DASHBOARD_HTML)
    ids = [attrs["id"] for _, attrs in page.elements if "id" in attrs]
    assert len(ids) == len(set(ids))
    assert ("html", {"lang": "zh-CN"}) in page.elements
    assert not any(tag in {"iframe", "form", "audio", "video"} for tag, _ in page.elements)
    assert not any("src" in attrs for _, attrs in page.elements)
    links = [attrs for tag, attrs in page.elements if tag == "a"]
    assert len(links) == 1
    assert links[0]["href"] == "/api/report"
    assert links[0]["download"] == "aeis-session-summary.json"
    buttons = [attrs for tag, attrs in page.elements if tag == "button"]
    assert len(buttons) == 7
    assert all(attrs["aria-label"] for attrs in buttons)
    assert all(attrs["type"] == "button" for attrs in buttons)
    assert "默认静音" in DASHBOARD_HTML
    assert "合成演示数据" in DASHBOARD_HTML
    assert "不是完整比赛策略" in DASHBOARD_HTML
    assert "非本次加油指令" in DASHBOARD_HTML
    assert "仅单箱可完成时显示；需配置油箱容量" in DASHBOARD_HTML
    assert "仅按燃油计算的算术下界，不是进站计划" in DASHBOARD_HTML
    assert "全程累计估计" not in DASHBOARD_HTML
    assert "innerHTML" not in DASHBOARD_HTML
    assert "eval(" not in DASHBOARD_HTML
    assert DASHBOARD_HTML.count("method: 'POST'") == 1
    assert "fetch('/api/state'" in DASHBOARD_HTML
    assert "fetch('/api/engineer'" in DASHBOARD_HTML
    assert "fetch('/api/engineer/question'" in DASHBOARD_HTML
    assert "'X-Engineer-Token': token" in DASHBOARD_HTML
    assert "localStorage" not in DASHBOARD_HTML
    assert "文字回复不会自动播报" in DASHBOARD_HTML
    assert "请勿输入姓名、账号或其他身份信息" in DASHBOARD_HTML
    assert "item.localService === true" in DASHBOARD_HTML


_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const assert = require('node:assert/strict');
const nodes = new Map(), intervals = [], timeouts = new Map(), calls = [];
const events = {}, windowEvents = {};
let nextTimeout = 0;
const state = {
  now: 0, result: null, failure: false, pending: false, spoken: [], cancelled: 0,
  voices: [{lang: 'en-US', localService: true}], autoStart: true,
  engineerFailure: false, engineerPending: false, postPending: false, postStatus: 202,
  postError: {error: 'RATE_LIMITED'},
  engineer: {enabled: false, provider: 'deepseek', model: 'test-model', status: 'DISABLED',
    requests_used: 0, request_limit: 60, min_interval_s: 10, csrf_token: 'test-csrf',
    answer: null, error: null, capabilities: {session: false}},
};
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {
    textContent: '', value: '', hidden: false, disabled: false,
    style: {}, dataset: {}, attributes: {},
    children: [], callbacks: {},
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(key, value) { this.callbacks[key] = value; },
    replaceChildren() { this.children = []; },
    appendChild(value) { this.children.push(value); },
  });
  return nodes.get(id);
}
const document = {
  hidden: false, getElementById: node, createElement: () => node(Symbol()),
  addEventListener: (key, value) => { events[key] = value; },
};
const speechSynthesis = {
  getVoices: () => state.voices,
  cancel: () => { state.cancelled++; },
  speak: (value) => {
    state.spoken.push(value);
    if (state.autoStart && value.onstart) value.onstart();
  },
  addEventListener: (key, value) => { windowEvents[key] = value; },
};
const context = {
  assert, state, nodes, intervals, timeouts, events, windowEvents, node, calls,
  document, performance: {now: () => state.now}, AbortController,
  window: {speechSynthesis, addEventListener: (key, value) => { windowEvents[key] = value; }},
  SpeechSynthesisUtterance: function(text) { this.text = text; },
  setInterval: (fn, delay) => { intervals.push({fn, delay}); },
  setTimeout: (fn) => { timeouts.set(++nextTimeout, fn); return nextTimeout; },
  clearTimeout: (id) => { timeouts.delete(id); },
  fetch: async (url, options) => {
    calls.push({url, options});
    if (url === '/api/engineer') {
      if (state.engineerPending) return new Promise(() => {});
      if (state.engineerFailure) throw Error('engineer offline');
      return {ok: true, json: async () => JSON.parse(JSON.stringify(state.engineer))};
    }
    if (url === '/api/engineer/question') {
      if (state.postPending) return new Promise(() => {});
      return {ok: state.postStatus === 202, status: state.postStatus,
        json: async () => state.postError};
    }
    if (state.pending) return new Promise(() => {});
    if (state.failure) throw Error('offline');
    return {ok: true, json: async () => state.result};
  },
};
context.payload = () => ({
  connection: 'CONNECTED', updated_age_s: 0.1, generation: 1, source_mode: 'LIVE',
  session_type: 'Practice',
  monitor: {status: 'READY', source_kind: 'SDK_LIVE', reasons: [], sequence: 0,
    interval_unsafe_for_speech: [],
    telemetry: {brake: 0, steering_angle_rad: 0, speed_mps: 40,
      car_left_right: 1, on_pit_road: false},
    context: {sim_source_mode: 'FULL', player_control_state: 'IN_CAR_PHYSICS'},
    quality: {stale: false, status: 'READY'}},
  fuel: {status: 'READY', current_fuel_l: 31.2, valid_laps: 3, required_laps: 3,
    estimated_laps_remaining: 10.1, fuel_needed_to_finish_l: 44.2, fuel_to_add_l: 13,
    conservative_burn_l_per_lap: 3.1, minimum_stops: 1, message: 'estimate',
    advisor_only: true, estimate_only: true, executable: false, reason_codes: []},
  speech: {id: 'one', text: 'Fuel estimate, ten laps remaining.', expires_in_s: 1,
    priority: 'NORMAL'}, limitations: [],
});
const scope = vm.createContext(context);
vm.runInContext(input.script, scope);
(async () => {
  await new Promise((resolve) => setImmediate(resolve));
  await vm.runInContext('(async () => {' + input.scenario + '})()', scope);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


def _javascript(scenario: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser-free dashboard JavaScript checks")
    scripts = re.findall(r"<script>(.*?)</script>", DASHBOARD_HTML, flags=re.DOTALL)
    assert len(scripts) == 1
    result = subprocess.run(
        [node, "-e", _HARNESS],
        input=json.dumps({"script": scripts[0], "scenario": scenario}),
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_live_data_rendering_is_text_only_and_speech_starts_muted() -> None:
    _javascript(r"""
      state.result = payload();
      state.result.fuel.message = '<img src=x onerror=alert(1)>';
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(node('fuel-current').textContent, '31.2 L');
      assert.equal(node('fuel-laps').textContent, '10.1 圈');
      assert.equal(node('advice').textContent, '<img src=x onerror=alert(1)>');
      assert.equal(node('learning-progress').attributes['aria-valuenow'], '100');
      assert.equal(state.spoken.length, 0);
      assert.equal(calls[0].url, '/api/state');
      assert.equal(calls[0].options.cache, 'no-store');
    """)


@pytest.mark.parametrize("mode", ["age", "failure", "timeout"])
def test_stale_or_unavailable_data_clears_all_estimates_and_speech(mode: str) -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      node('voice-enable').callbacks.click();
      state.result.speech.id = 'new';
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(state.spoken.length, 1);
    """ + {
        "age": "state.now = 2100; intervals.find((item) => item.delay === 100).fn();",
        "failure": "state.failure = true; await intervals.find((i) => i.delay === 500).fn();",
        "timeout": """
          state.pending = true;
          intervals.find((item) => item.delay === 500).fn();
          for (const timeout of timeouts.values()) timeout();
        """,
    }[mode] + r"""
      for (const id of ['fuel-current', 'fuel-laps', 'fuel-finish', 'fuel-add',
          'fuel-burn', 'fuel-stops']) assert.equal(node(id).textContent, '—');
      assert.equal(node('connection').dataset.tone, 'bad');
      assert.ok(state.cancelled > 0);
    """)


def test_speech_is_new_only_deduplicated_local_and_finishes_without_ttl_cutoff() -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      node('voice-enable').callbacks.click();
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(state.spoken.length, 0);
      state.result.speech.id = 'two';
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(state.spoken.length, 1);
      assert.equal(state.spoken[0].voice.localService, true);
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(state.spoken.length, 1);
      const before = state.cancelled;
      state.result.speech = null;
      for (let step = 1; step <= 5; step++) {
        state.now = step * 500;
        state.result.monitor.sequence += 1;
        await intervals.find((item) => item.delay === 500).fn();
        intervals.find((item) => item.delay === 100).fn();
      }
      assert.equal(state.cancelled, before);
      assert.equal(state.spoken.length, 1);
      state.spoken[0].onend();
      state.result.speech = payload().speech;
      state.result.speech.id = 'two';
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(state.spoken.length, 1);
      node('voice-mute').callbacks.click();
      assert.equal(node('voice-enable').attributes['aria-pressed'], 'false');
    """)


@pytest.mark.parametrize("condition", [
    "state.result.source_mode = 'SYNTHETIC_DEMO';",
    "state.result.session_type = 'Race';",
    "state.result.session_type = null;",
    "state.result.monitor.context.sim_source_mode = 'REPLAY_FILE';",
    "state.result.monitor.context.player_control_state = 'OUT_OF_CAR_OR_REPLAY_VIEW';",
    "state.result.monitor.source_kind = 'REPLAY_SDK_PROXY';",
    "state.result.monitor.quality.stale = true;",
    "state.result.monitor.quality.status = 'REJECTED';",
    "state.result.monitor.interval_unsafe_for_speech = ['RECENT_BRAKING'];",
    "state.result.monitor.interval_unsafe_for_speech = null;",
    "state.result.monitor.telemetry.brake = 0.2;",
    "state.result.monitor.telemetry.steering_angle_rad = -0.1;",
    "state.result.monitor.telemetry.speed_mps = 10;",
    "state.result.monitor.telemetry.car_left_right = 2;",
    "state.result.monitor.telemetry.on_pit_road = true;",
    "state.result.fuel.status = 'LEARNING';",
    "state.result.fuel.status = 'BLOCKED';",
    "state.result.fuel.executable = true;",
    "state.result.connection = 'DISCONNECTED';",
    "state.result.speech.expires_in_s = 0;",
    "document.hidden = true; events.visibilitychange();",
])
def test_speech_refuses_unsafe_contexts(condition: str) -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      node('voice-enable').callbacks.click();
      state.result.speech.id = 'new';
    """ + condition + r"""
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(state.spoken.length, 0);
    """)


def test_only_cloud_or_non_english_voices_disable_speech() -> None:
    _javascript(r"""
      state.voices = [{lang: 'en-US', localService: false},
        {lang: 'zh-CN', localService: true}];
      windowEvents.voiceschanged();
      assert.equal(node('voice-enable').disabled, true);
      assert.match(node('voice-status').textContent, /不会改用云端/);
      node('voice-enable').callbacks.click();
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(state.spoken.length, 0);
    """)


def test_demo_is_never_labelled_as_real_live_evidence() -> None:
    _javascript(r"""
      state.result = payload();
      state.result.source_mode = 'SYNTHETIC_DEMO';
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(node('demo-banner').hidden, false);
      assert.match(node('source-badge').textContent, /合成演示/);
      assert.match(node('context').textContent, /合成驾驶演示/);
      state.now = 2100;
      intervals.find((item) => item.delay === 100).fn();
      assert.equal(node('demo-banner').hidden, false);
      assert.match(node('source-badge').textContent, /合成演示/);
    """)


@pytest.mark.parametrize("status", ["LEARNING", "BLOCKED", "WAIT_CAR", "UNEXPECTED"])
def test_non_ready_fuel_status_cannot_show_estimates(status: str) -> None:
    _javascript(r"""
      state.result = payload();
    """ + f"state.result.fuel.status = {json.dumps(status)};" + r"""
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(node('fuel-current').textContent, '31.2 L');
      assert.equal(node('fuel-laps').textContent, '—');
      assert.equal(node('fuel-add').textContent, '—');
    """)


def test_recording_error_does_not_hide_valid_panel_data() -> None:
    _javascript(r"""
      state.result = payload();
      state.result.recording = {status: 'ERROR', bytes: 1048576};
      await intervals.find((item) => item.delay === 500).fn();
      assert.match(node('recording').textContent, /写入错误/);
      assert.match(node('recording').textContent, /1.0 MiB/);
      assert.equal(node('fuel-current').textContent, '31.2 L');
    """)


def test_repeated_snapshot_cannot_extend_freshness_and_new_frame_recovers() -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      state.now = 1500;
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(node('fuel-current').textContent, '31.2 L');
      state.now = 2100;
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(node('fuel-current').textContent, '—');
      assert.equal(node('connection').dataset.tone, 'bad');
      state.result.monitor.sequence = 1;
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(node('fuel-current').textContent, '31.2 L');
    """)


def test_sequence_regression_requires_new_connection_generation() -> None:
    _javascript(r"""
      state.result = payload();
      state.result.monitor.sequence = 8;
      await intervals.find((item) => item.delay === 500).fn();
      state.result.monitor.sequence = 0;
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(node('fuel-current').textContent, '—');
      state.result.generation = 2;
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(node('fuel-current').textContent, '31.2 L');
    """)


def test_expired_unstarted_intent_is_never_reissued_after_expiry() -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      node('voice-enable').callbacks.click();
      state.result.speech = {id: 'expired', text: 'Old estimate.', expires_in_s: 0};
      await intervals.find((item) => item.delay === 500).fn();
      state.result.speech.expires_in_s = 2;
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(state.spoken.length, 0);
    """)


def test_local_engine_cannot_start_queued_speech_after_start_deadline() -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      node('voice-enable').callbacks.click();
      state.autoStart = false;
      state.result.speech.id = 'delayed';
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(state.spoken.length, 1);
      const before = state.cancelled;
      state.now = 1100;
      intervals.find((item) => item.delay === 100).fn();
      assert.ok(state.cancelled > before);
      assert.equal(state.spoken[0].onstart, null);
    """)


@pytest.mark.parametrize("unsafe", [
    "state.result.monitor.interval_unsafe_for_speech = ['RECENT_BRAKING'];",
    "state.result.monitor.telemetry.brake = 0.3;",
    "state.result.monitor.telemetry.steering_angle_rad = -0.08;",
    "state.result.monitor.telemetry.car_left_right = 2;",
])
def test_active_speech_is_immediately_cancelled_when_new_state_is_unsafe(unsafe: str) -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      node('voice-enable').callbacks.click();
      state.result.speech.id = 'active';
      await intervals.find((item) => item.delay === 500).fn();
      const before = state.cancelled;
      state.result.speech = null;
      state.now = 500;
      state.result.monitor.sequence += 1;
    """ + unsafe + r"""
      await intervals.find((item) => item.delay === 500).fn();
      assert.ok(state.cancelled > before);
      assert.equal(state.spoken[0].onend, null);
    """)


def test_active_speech_has_twelve_second_watchdog_despite_fresh_safe_samples() -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      node('voice-enable').callbacks.click();
      state.result.speech.id = 'active';
      await intervals.find((item) => item.delay === 500).fn();
      const before = state.cancelled;
      state.result.speech = null;
      for (let second = 1; second <= 11; second++) {
        state.now = second * 1000;
        state.result.monitor.sequence += 1;
        await intervals.find((item) => item.delay === 500).fn();
        intervals.find((item) => item.delay === 100).fn();
        assert.equal(state.cancelled, before);
      }
      state.now = 12000;
      state.result.monitor.sequence += 1;
      await intervals.find((item) => item.delay === 500).fn();
      intervals.find((item) => item.delay === 100).fn();
      assert.ok(state.cancelled > before);
    """)


def test_new_intent_replaces_active_speech_and_detaches_cancelled_callbacks() -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      node('voice-enable').callbacks.click();
      state.result.speech.id = 'active';
      await intervals.find((item) => item.delay === 500).fn();
      const before = state.cancelled;
      state.result.speech.id = 'replacement';
      await intervals.find((item) => item.delay === 500).fn();
      assert.equal(state.spoken.length, 2);
      assert.ok(state.cancelled > before);
      assert.equal(state.spoken[0].onerror, null);
      assert.equal(state.spoken[0].onend, null);
      assert.equal(node('voice-enable').attributes['aria-pressed'], 'true');
    """)


def test_engineer_never_auto_submits_and_posts_only_typed_question_with_token() -> None:
    _javascript(r"""
      assert.equal(calls.filter((call) => call.options.method === 'POST').length, 0);
      assert.ok(calls.some((call) => call.url === '/api/engineer'));
      node('engineer-question').value = '  现在油量够吗？  ';
      await node('engineer-send').callbacks.click();
      const post = calls.find((call) => call.options.method === 'POST');
      assert.equal(post.url, '/api/engineer/question');
      assert.equal(post.options.credentials, 'same-origin');
      assert.equal(post.options.headers['X-Engineer-Token'], 'test-csrf');
      assert.equal(post.options.headers['Content-Type'], 'application/json');
      assert.deepEqual(JSON.parse(post.options.body), {question: '现在油量够吗？', scope: 'live'});
      assert.match(node('engineer-request').textContent, /问题已接收/);
      assert.equal(node('engineer-send').disabled, true);
    """)


@pytest.mark.parametrize("status", ["DISABLED", "MISSING_KEY", "BUDGET_EXHAUSTED", "ERROR"])
def test_engineer_local_fallback_remains_available_without_cloud(status: str) -> None:
    _javascript(f"state.engineer.status = {json.dumps(status)};" + r"""
      state.engineer.enabled = false;
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(node('engineer-fuel').disabled, false);
      await node('engineer-fuel').callbacks.click();
      const post = calls.find((call) => call.options.method === 'POST');
      assert.match(JSON.parse(post.options.body).question, /燃油/);
      assert.equal(state.spoken.length, 0);
    """)


@pytest.mark.parametrize("status", ["BUSY", "RATE_LIMITED"])
def test_engineer_busy_or_rate_limited_cannot_send(status: str) -> None:
    _javascript(f"state.engineer.status = {json.dumps(status)};" + r"""
      state.engineer.retry_after_s = 3;
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(node('engineer-strategy').disabled, true);
      await node('engineer-strategy').callbacks.click();
      assert.equal(calls.filter((call) => call.options.method === 'POST').length, 0);
      state.now = 3100;
      state.engineer.status = 'MISSING_KEY';
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(node('engineer-strategy').disabled, false);
    """)


@pytest.mark.parametrize("question", ["   ", "x" * 501])
def test_engineer_rejects_empty_or_oversized_questions_without_post(question: str) -> None:
    _javascript(f"node('engineer-question').value = {json.dumps(question)};" + r"""
      await node('engineer-send').callbacks.click();
      assert.match(node('engineer-request').textContent, /1 到 500/);
      assert.equal(calls.filter((call) => call.options.method === 'POST').length, 0);
    """)


def test_engineer_polling_failure_is_isolated_from_telemetry() -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      state.engineerFailure = true;
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(node('fuel-current').textContent, '31.2 L');
      assert.equal(node('connection').dataset.tone, 'good');
      assert.equal(node('engineer-send').disabled, true);
      assert.match(node('engineer-request').textContent, /问答服务暂不可用/);
    """)


def test_engineer_answer_is_text_only_scope_labelled_and_never_spoken() -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      node('voice-enable').callbacks.click();
      state.engineer.model = '<b>model</b>';
      state.engineer.answer = {id: 'a1', topic: 'fuel',
        text: '<img src=x onerror=alert(1)>', origin: 'deepseek',
        scope: 'live_snapshot', age_s: 1, stale: false};
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(node('engineer-answer-body').textContent, '<img src=x onerror=alert(1)>');
      assert.equal(node('engineer-answer-body').children.length, 0);
      assert.match(node('engineer-origin').textContent, /DeepSeek · <b>model/);
      assert.match(node('engineer-answer-meta').textContent, /提问时的快照/);
      assert.match(node('engineer-answer-meta').textContent, /1 秒前/);
      assert.equal(state.spoken.length, 0);
    """)


@pytest.mark.parametrize("reason", ["server", "telemetry", "missing_age"])
def test_engineer_stale_live_answer_withdraws_old_numbers(reason: str) -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      state.engineer.answer = {id: 'a1', topic: 'fuel', text: 'Fuel 47.2 liters.',
        origin: 'local_fallback', scope: 'live_snapshot', age_s: 1, stale: false};
      await intervals.find((item) => item.delay === 1000).fn();
      assert.match(node('engineer-answer-body').textContent, /47.2/);
    """ + {
        "server": """
          state.engineer.answer.stale = true;
          await intervals.find((item) => item.delay === 1000).fn();
        """,
        "telemetry": """
          state.failure = true;
          await intervals.find((item) => item.delay === 500).fn();
          assert.equal(node('connection').dataset.tone, 'bad');
        """,
        "missing_age": """
          state.engineer.answer.age_s = null;
          await intervals.find((item) => item.delay === 1000).fn();
        """,
    }[reason] + r"""
      assert.equal(node('engineer-answer').dataset.stale, 'true');
      assert.match(node('engineer-answer-body').textContent, /旧数字与建议已撤回/);
      assert.doesNotMatch(node('engineer-answer-body').textContent, /47.2/);
    """)


def test_engineer_historical_session_answer_survives_disconnect_with_clear_scope() -> None:
    _javascript(r"""
      state.engineer.capabilities.session = true;
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(node('engineer-session').hidden, false);
      await node('engineer-session').callbacks.click();
      const post = calls.find((call) => call.options.method === 'POST');
      assert.equal(JSON.parse(post.options.body).scope, 'session');
      state.engineer.answer = {id: 'a1', topic: 'review', text: '历史数据质量不足。',
        origin: 'local_fallback', scope: 'historical_session', age_s: 5, stale: false};
      await intervals.find((item) => item.delay === 1000).fn();
      assert.match(node('engineer-answer-body').textContent, /历史数据质量不足/);
      assert.match(node('engineer-answer-meta').textContent, /本次会话复盘 · 非实时指令/);
      assert.match(node('engineer-origin').textContent, /本地规则解读 · 未调用模型/);
      assert.match(node('engineer-request').textContent, /回答已更新/);
    """)


def test_engineer_session_request_is_blocked_when_capability_absent() -> None:
    _javascript(r"""
      assert.equal(node('engineer-session').hidden, true);
      await node('engineer-session').callbacks.click();
      assert.equal(calls.filter((call) => call.options.method === 'POST').length, 0);
    """)


@pytest.mark.parametrize("pending", ["engineerPending", "postPending"])
def test_engineer_get_and_post_do_not_overlap_or_retry_ambiguous_submission(pending: str) -> None:
    start = {
        "engineerPending": """
          state.engineerPending = true;
          intervals.find((item) => item.delay === 1000).fn();
        """,
        "postPending": """
          state.postPending = true;
          node('engineer-question').value = '有什么依据？';
          node('engineer-send').callbacks.click();
        """,
    }[pending]
    _javascript(start + r"""
      const before = calls.filter((call) => call.url.startsWith('/api/engineer')).length;
      intervals.find((item) => item.delay === 1000).fn();
      node('engineer-driving').callbacks.click();
      assert.equal(calls.filter((call) => call.url.startsWith('/api/engineer')).length, before);
      for (const timeout of timeouts.values()) timeout();
      intervals.find((item) => item.delay === 1000).fn();
      assert.equal(calls.filter((call) => call.url.startsWith('/api/engineer')).length, before);
      assert.match(node('engineer-request').textContent, /不会自动重发/);
    """)


def test_engineer_http_rejection_is_not_retried_and_preserves_question() -> None:
    _javascript(r"""
      state.postStatus = 400;
      state.postError = {error: 'INVALID_QUESTION'};
      node('engineer-question').value = '保留问题';
      await node('engineer-send').callbacks.click();
      assert.match(node('engineer-request').textContent, /1 到 500/);
      assert.equal(node('engineer-question').value, '保留问题');
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(calls.filter((call) => call.options.method === 'POST').length, 1);
    """)


def test_engineer_quick_questions_and_client_cooldown() -> None:
    _javascript(r"""
      await node('engineer-strategy').callbacks.click();
      assert.match(node('engineer-question').value, /进站判断/);
      await node('engineer-driving').callbacks.click();
      assert.equal(calls.filter((call) => call.options.method === 'POST').length, 1);
      state.now = 11000;
      await intervals.find((item) => item.delay === 1000).fn();
      await node('engineer-driving').callbacks.click();
      assert.match(node('engineer-question').value, /驾驶分析/);
      assert.equal(calls.filter((call) => call.options.method === 'POST').length, 2);
    """)


def test_withdrawn_live_answer_does_not_reappear_after_reconnect_without_new_answer() -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      state.engineer.answer = {id: 'old', topic: 'fuel', text: 'Fuel 47.2 liters.',
        origin: 'local_fallback', scope: 'live_snapshot', age_s: 1, stale: false};
      await intervals.find((item) => item.delay === 1000).fn();
      assert.match(node('engineer-answer-body').textContent, /47.2/);
      state.failure = true;
      await intervals.find((item) => item.delay === 500).fn();
      state.failure = false;
      state.result.generation += 1;
      await intervals.find((item) => item.delay === 500).fn();
      await intervals.find((item) => item.delay === 1000).fn();
      assert.doesNotMatch(node('engineer-answer-body').textContent, /47.2/);
      state.engineer.answer.id = 'new';
      state.engineer.answer.text = 'Current evidence is limited.';
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(node('engineer-answer-body').textContent, 'Current evidence is limited.');
    """)


def test_no_evidence_explanation_remains_visible_without_live_sdk() -> None:
    _javascript(r"""
      state.engineer.answer = {id: 'a1', topic: 'fuel', text: '没有有效遥测，无法判断油量。',
        origin: 'local_fallback', scope: 'live_snapshot', age_s: 1, stale: false,
        snapshot_was_valid: false};
      await intervals.find((item) => item.delay === 1000).fn();
      assert.match(node('engineer-answer-body').textContent, /没有有效遥测/);
      assert.match(node('engineer-answer-meta').textContent, /无有效实时证据/);
      assert.equal(node('connection').dataset.tone, 'bad');
      state.engineer.answer.stale = true;
      await intervals.find((item) => item.delay === 1000).fn();
      assert.match(node('engineer-answer-body').textContent, /旧数字与建议已撤回/);
    """)


def test_engineer_restart_token_namespaces_reused_answer_ids() -> None:
    _javascript(r"""
      state.result = payload();
      await intervals.find((item) => item.delay === 500).fn();
      state.engineer.answer = {id: '1', topic: 'fuel', text: 'Old answer.',
        origin: 'local_fallback', scope: 'live_snapshot', age_s: 1, stale: true};
      await intervals.find((item) => item.delay === 1000).fn();
      assert.match(node('engineer-answer-body').textContent, /旧数字与建议已撤回/);
      state.engineer.csrf_token = 'new-instance-token';
      state.engineer.answer.text = 'New answer.';
      state.engineer.answer.stale = false;
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(node('engineer-answer-body').textContent, 'New answer.');
    """)


@pytest.mark.parametrize("error", [
    "MODEL_UNAVAILABLE_OR_INVALID_PLAN", "MODEL_CONFIGURATION_INVALID", "private-provider-debug",
])
def test_engineer_configured_status_and_safe_fallback_error_disclosure(error: str) -> None:
    _javascript(r"""
      state.engineer.status = 'READY';
    """ + f"state.engineer.error = {json.dumps(error)};" + r"""
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(node('engineer-backend').textContent, 'DeepSeek 已配置');
      assert.equal(node('engineer-error').hidden, false);
      assert.match(node('engineer-error').textContent, /本地解读/);
      assert.doesNotMatch(node('engineer-error').textContent, /private-provider-debug/);
      if (state.engineer.error === 'MODEL_CONFIGURATION_INVALID') {
        assert.match(node('engineer-error').textContent, /密钥配置格式有误/);
      } else {
        assert.match(node('engineer-error').textContent, /不会自动重试/);
      }
      state.engineer.error = null;
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(node('engineer-error').hidden, true);
      assert.equal(node('engineer-error').textContent, '');
    """)


def test_engineer_chinese_answer_and_topic_are_preserved_without_model_speech() -> None:
    _javascript(r"""
      state.engineer.answer = {id: 'zh1', topic: 'driving', text: '当前缺少有效弯角证据。',
        origin: 'local_fallback', scope: 'live_snapshot', age_s: 1, stale: false,
        snapshot_was_valid: false};
      await intervals.find((item) => item.delay === 1000).fn();
      assert.equal(node('engineer-answer-body').textContent, '当前缺少有效弯角证据。');
      assert.match(node('engineer-answer-meta').textContent, /驾驶/);
      assert.doesNotMatch(node('engineer-answer-meta').textContent, /driving/);
      assert.equal(state.spoken.length, 0);
    """)
