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
    assert len(buttons) == 2
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
    assert "method:" not in DASHBOARD_HTML
    assert "fetch('/api/state'" in DASHBOARD_HTML
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
};
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {
    textContent: '', hidden: false, disabled: false, style: {}, dataset: {}, attributes: {},
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
