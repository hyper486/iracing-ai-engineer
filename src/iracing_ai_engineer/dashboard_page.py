"""Self-contained, read-only dashboard with fail-closed local practice speech."""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>AEIS · 本地比赛工程师</title>
<style>
:root{color-scheme:dark;font-family:system-ui,"Microsoft YaHei",sans-serif;
background:#0b1118;color:#edf3f8;font-synthesis:none}
*{box-sizing:border-box}body{margin:0}main{max-width:1100px;margin:auto;padding:32px 24px}
header,.row{display:flex;align-items:center;justify-content:space-between;gap:18px}
h1{font-size:clamp(23px,4vw,32px);margin:5px 0 10px;letter-spacing:-.04em}
h2{font-size:17px;margin:0 0 14px}p{line-height:1.65}small,.muted{color:#a9b8c9}
.eyebrow{color:#79d9c5;font-size:12px;letter-spacing:.14em}
.badge{display:inline-flex;align-items:center;border:1px solid #3b5064;border-radius:30px;
padding:7px 12px;font-size:12px;white-space:nowrap;background:#142130}
.badge[data-tone="good"]{color:#9df0d3;border-color:#34785e;background:#122e26}
.badge[data-tone="warn"]{color:#ffe1a3;border-color:#84652e;background:#352b17}
.badge[data-tone="bad"]{color:#ffb2b2;border-color:#884242;background:#391d22}
.notice{border:1px solid #88612a;background:#2d2418;border-radius:12px;
padding:13px 17px;color:#ffe0a7;margin:22px 0 18px;font-size:14px;line-height:1.65}
.demo{border-color:#9d75c9;background:#281e37;color:#e5c7ff}
[hidden]{display:none!important}.panel{background:#111c28;border:1px solid #253749;
border-radius:15px;padding:22px;margin-bottom:16px}
.overview{display:grid;grid-template-columns:1.5fr 1fr;gap:24px}
.status-title{font-size:23px;font-weight:650;margin:4px 0 9px}
.explanation{color:#bdc9d7;margin:0;min-height:48px;font-size:14px}
.learning{border-left:1px solid #2d3d4e;padding-left:24px}
.learning-value{font-size:22px;margin:9px 0 12px;font-variant-numeric:tabular-nums}
.track{height:6px;background:#283949;border-radius:5px;overflow:hidden;margin-bottom:10px}
.fill{height:100%;width:0;background:#79d9c5;transition:width .3s}
.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:16px 0}
.metric{background:#111c28;border:1px solid #253749;border-radius:14px;padding:20px}
.metric-label{font-size:13px;color:#adbdcd}.metric-value{font-size:34px;font-weight:600;
letter-spacing:-.035em;font-variant-numeric:tabular-nums;margin:12px 0 9px}
.metric-note{font-size:12px;color:#91a5ba;line-height:1.5}
.bottom{display:grid;grid-template-columns:1fr 1fr;gap:16px}.bottom .panel{margin:0}
button{border:1px solid #65867e;border-radius:9px;padding:10px 14px;color:#ecfff8;
background:#23463c;font:inherit;font-size:13px;cursor:pointer}
button.secondary{background:#1b2735;border-color:#596b7d}
button:disabled{opacity:.55;cursor:not-allowed}a{color:#a4e4d5;text-underline-offset:4px}
button:focus-visible,a:focus-visible{outline:3px solid #cce9ff;
outline-offset:4px}.buttons{display:flex;gap:9px;flex-wrap:wrap;margin:16px 0}
ul{padding-left:19px;color:#b9c7d6;font-size:13px;line-height:1.8;margin-bottom:0}
.speech-line{font-size:13px;line-height:1.7;min-height:44px;color:#adbdcd}
footer{margin-top:20px;color:#889caf;font-size:12px;line-height:1.7}
@media(max-width:720px){main{padding:23px 16px}header{align-items:flex-start}
.overview,.bottom{grid-template-columns:1fr}.learning{border-left:0;border-top:1px solid #2d3d4e;
padding:17px 0 0}.metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.metric{padding:17px}.metric-value{font-size:29px}.panel{padding:19px}}
@media(prefers-reduced-motion:reduce){.fill{transition:none}}
</style>
</head>
<body>
<main>
<header>
<div><div class="eyebrow">AEIS / LOCAL RACE ENGINEER</div>
<h1>本地比赛工程师</h1><small>只读遥测 · 建议辅助 · 不控制赛车或进站设置</small></div>
<span class="badge" id="source-badge">数据源未确认</span>
</header>
<div class="notice demo" id="demo-banner" hidden>
合成演示数据 · 非真实 iRacing 遥测，不代表实车验证通过；演示模式不播报。
</div>
<div class="notice">实验燃油估计，不是完整比赛策略。尚未综合轮胎衰减、出站交通、
黄旗与对手进站；请自行确认比赛规则、油量和进站操作。</div>
<section class="panel overview" aria-label="连接和学习状态">
<div>
<div class="row"><span class="badge" id="connection" data-tone="warn"
role="status" aria-live="polite">等待本地服务</span>
<small id="freshness">尚无新鲜数据</small></div>
<div class="status-title" id="context">等待 iRacing</div>
<p class="explanation" id="advice" role="status" aria-live="polite">
连接后，请完成有效完整圈以学习油耗。观战与回放不会产生驾驶建议。</p>
</div>
<div class="learning"><small>完整有效圈 · 油耗学习</small>
<div class="learning-value" id="learning-count">— / — 圈</div>
<div class="track" id="learning-progress" role="progressbar" aria-label="油耗学习进度"
aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="fill"
id="learning-fill"></div></div>
<small id="learning-note">无效圈、进站圈或中断数据不能代替完整圈。</small></div>
</section>
<section class="metrics" aria-label="实验燃油估计">
<div class="metric"><div class="metric-label">当前燃油</div>
<div class="metric-value" id="fuel-current">—</div>
<div class="metric-note">遥测读数 / 升</div></div>
<div class="metric"><div class="metric-label">保守预计续航</div>
<div class="metric-value" id="fuel-laps">—</div>
<div class="metric-note">估计可跑圈数，非保证</div></div>
<div class="metric"><div class="metric-label">至终点预计需油</div>
<div class="metric-value" id="fuel-finish">—</div>
<div class="metric-note">剩余赛程的总需求 / 升</div></div>
<div class="metric"><div class="metric-label">预计还需补充</div>
<div class="metric-value" id="fuel-add">—</div>
<div class="metric-note">仅单箱可完成时显示；需配置油箱容量。非本次加油指令。</div></div>
<div class="metric"><div class="metric-label">保守单圈油耗</div>
<div class="metric-value" id="fuel-burn">—</div>
<div class="metric-note">已学习圈的估计 / 升每圈</div></div>
<div class="metric"><div class="metric-label">燃油进站次数下限</div>
<div class="metric-value" id="fuel-stops">—</div>
<div class="metric-note">仅按燃油计算的算术下界，不是进站计划</div></div>
</section>
<div class="bottom">
<section class="panel" aria-label="本地练习语音">
<div class="row"><h2>本地练习语音</h2><span class="badge" id="voice-badge">默认静音</span></div>
<small>仅设备本地英文声音，不发送音频文本到云端。只用于练习；比赛暂静音。</small>
<div class="buttons"><button id="voice-enable" type="button"
aria-label="启用本地练习语音" aria-pressed="false" disabled>启用练习语音</button>
<button id="voice-mute" class="secondary" type="button"
aria-label="立即静音并取消当前播报">立即静音</button></div>
<div class="speech-line" id="voice-status" role="status" aria-live="polite">
正在检查设备本地声音。默认不播放；切换到后台后自动静音，需重新启用。
</div>
</section>
<section class="panel" aria-label="数据质量和边界">
<h2>数据质量与边界</h2><small id="quality">尚无可用数据</small>
<ul id="issues"><li>等待数据质量检查。</li></ul>
<p class="speech-line" id="recording">本地记录状态：等待服务</p>
<a href="/api/report" download="aeis-session-summary.json"
aria-label="下载本次安全聚合摘要 JSON">保存本次摘要</a>
</section>
</div>
<footer>此面板不发送模拟器、车辆或进站黑盒控制指令。
连接中断、数据过期或退出驾驶状态时，旧估计会被撤下；语音不会排队补播。
真实驾驶验收尚待完成。</footer>
</main>
<script>
(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  const put = (id, value) => { byId(id).textContent = value; };
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  const metricIds = ['fuel-current', 'fuel-laps', 'fuel-finish', 'fuel-add',
    'fuel-burn', 'fuel-stops'];
  const synth = window.speechSynthesis;
  let current = null, receivedAt = 0, requestTransit = 0, inFlight = false;
  let enabled = false, voice = null, activeSpeech = null, generation = null;
  let progressSequence = null, progressAt = 0;
  const seenSpeech = new Set();
  const fresh = () => current && current.connection === 'CONNECTED'
    && finite(current.updated_age_s) && current.updated_age_s >= 0
    && current.updated_age_s + requestTransit + (performance.now() - receivedAt) / 1000 <= 2
    && progressSequence !== null && performance.now() - progressAt <= 2000;
  const cancelSpeech = () => {
    if (activeSpeech) {
      activeSpeech.utterance.onend = null;
      activeSpeech.utterance.onerror = null;
      activeSpeech.utterance.onstart = null;
      put('voice-status', '当前播报已取消；不会补播过期消息。');
    }
    if (synth) synth.cancel();
    activeSpeech = null;
  };
  function mute(reason) {
    enabled = false;
    cancelSpeech();
    byId('voice-enable').setAttribute('aria-pressed', 'false');
    put('voice-badge', '已静音');
    put('voice-status', reason);
  }
  function findVoice() {
    voice = synth && synth.getVoices().find((item) => item.localService === true
      && /^en(?:-|$)/i.test(item.lang));
    byId('voice-enable').disabled = !voice;
    if (!voice) mute('未发现本地英文声音，语音不可用；不会改用云端声音。');
    else if (!enabled) put('voice-status',
      '本地英文声音可用。默认静音；点击启用后，只播报新收到且有效的练习提示。');
  }
  function safeDriving() {
    const monitor = current && current.monitor;
    const telemetry = monitor && monitor.telemetry;
    return fresh() && current.source_mode === 'LIVE' && monitor
      && current.session_type === 'Practice'
      && monitor.source_kind === 'SDK_LIVE'
      && monitor.context && monitor.context.sim_source_mode === 'FULL'
      && monitor.context.player_control_state === 'IN_CAR_PHYSICS'
      && monitor.quality && monitor.quality.stale === false
      && ['READY', 'DEGRADED'].includes(monitor.quality.status)
      && ['READY', 'DEGRADED'].includes(monitor.status)
      && Array.isArray(monitor.interval_unsafe_for_speech)
      && monitor.interval_unsafe_for_speech.length === 0
      && telemetry && finite(telemetry.brake) && telemetry.brake >= 0
      && telemetry.brake <= 0.02 && finite(telemetry.steering_angle_rad)
      && Math.abs(telemetry.steering_angle_rad) <= 0.05
      && finite(telemetry.speed_mps) && telemetry.speed_mps >= 15
      && telemetry.car_left_right === 1 && telemetry.on_pit_road === false
      && current.fuel && current.fuel.status === 'READY'
      && current.fuel.advisor_only === true && current.fuel.estimate_only === true
      && current.fuel.executable === false;
  }
  function speakLatest() {
    const message = current && current.speech;
    const id = message && typeof message.id === 'string' && message.id.length > 0 ?
      String(generation) + ':' + message.id : null;
    const valid = id && typeof message.text === 'string' && message.text.length > 0
      && message.text.length <= 240 && finite(message.expires_in_s)
      && message.expires_in_s > requestTransit;
    if (!safeDriving() || document.hidden || !enabled || !voice) {
      if (id) seenSpeech.add(id);
      cancelSpeech();
      return;
    }
    // The server TTL is a latest-start deadline, not a mid-sentence stop time.
    // Active speech is still checked against every new safe state and a 12 s cap.
    if (!valid) {
      if (id) seenSpeech.add(id);
      return;
    }
    const expiry = receivedAt + (message.expires_in_s - requestTransit) * 1000;
    if (performance.now() >= expiry) {
      seenSpeech.add(id);
      return;
    }
    if (activeSpeech && activeSpeech.id === id) return;
    if (seenSpeech.has(id)) return;
    seenSpeech.add(id);
    cancelSpeech();
    const utterance = new SpeechSynthesisUtterance(message.text);
    utterance.voice = voice;
    utterance.lang = voice.lang;
    utterance.rate = 1;
    activeSpeech = {id, utterance, startBy: expiry, started: false,
      startedAt: performance.now()};
    const speakingId = id;
    utterance.onstart = () => {
      if (!activeSpeech || activeSpeech.id !== speakingId) return;
      if (performance.now() >= activeSpeech.startBy || !safeDriving()
        || !enabled || document.hidden) {
        cancelSpeech();
      } else {
        activeSpeech.started = true;
        activeSpeech.startedAt = performance.now();
      }
    };
    utterance.onend = () => {
      if (activeSpeech && activeSpeech.id === speakingId) activeSpeech = null;
    };
    utterance.onerror = () => mute('本地播报失败，已静音；不会切换到云端声音。');
    put('voice-status', message.text);
    synth.speak(utterance);
  }
  function clearEstimates() {
    metricIds.forEach((id) => put(id, '—'));
    put('learning-count', '— / — 圈');
    byId('learning-fill').style.width = '0%';
    byId('learning-progress').setAttribute('aria-valuenow', '0');
  }
  function listIssues(values) {
    const list = byId('issues');
    list.replaceChildren();
    values.slice(0, 12).forEach((value) => {
      const item = document.createElement('li');
      item.textContent = String(value);
      list.appendChild(item);
    });
  }
  function unavailable(label, detail) {
    put('connection', label);
    byId('connection').dataset.tone = 'bad';
    put('freshness', '旧建议已撤下');
    put('context', '当前无可用实时建议');
    put('advice', detail);
    put('quality', '不可用 · 请勿依据旧数据做决定');
    if (!current) put('recording', '本地记录状态：服务失联，状态未确认');
    clearEstimates();
    cancelSpeech();
    put('voice-status', '实时数据不可用，播报已停止；等待新的有效提示。');
    listIssues(['连接或数据新鲜度未通过检查。', '仅提供建议，不控制赛车或进站设置。']);
  }
  function number(id, value, unit, digits = 1) {
    put(id, finite(value) && value >= 0 ? value.toFixed(digits) + unit : '—');
  }
  function render() {
    const state = current;
    const demo = state && state.source_mode === 'SYNTHETIC_DEMO';
    byId('demo-banner').hidden = !demo;
    put('source-badge', demo ? '合成演示 · 非实测' :
      state && state.source_mode === 'LIVE' ? '实时 SDK 数据源' : '数据源未确认');
    if (!fresh()) {
      const labels = {WAIT_SIM: '等待 iRacing', DISCONNECTED: '遥测已断开',
        ERROR: '采集出现错误', STOPPED: '采集已停止'};
      unavailable(state && labels[state.connection] || '数据已过期 / 服务失联',
        '实时数据不可用。旧燃油估计与播报已取消；请查看模拟器自身信息。');
      speakLatest();
      return;
    }
    const monitor = state.monitor;
    const fuel = state.fuel;
    const driving = monitor && monitor.context
      && monitor.context.player_control_state === 'IN_CAR_PHYSICS'
      && monitor.context.sim_source_mode === 'FULL';
    const usable = monitor && ['READY', 'DEGRADED'].includes(monitor.status)
      && monitor.quality && monitor.quality.stale === false
      && ['READY', 'DEGRADED'].includes(monitor.quality.status) && driving;
    put('connection', '遥测已连接');
    byId('connection').dataset.tone = usable ? 'good' : 'warn';
    put('freshness', '数据年龄 ' + (state.updated_age_s + requestTransit).toFixed(2) + ' 秒');
    put('context', !monitor ? '等待有效帧' : monitor.status === 'BLOCKED' ? '数据质量阻断' :
      !driving ? '观战 / 回放 / 未进入驾驶' : demo ? '合成驾驶演示' : '正在驾驶 · 实验估计');
    put('quality', monitor && monitor.quality ?
      '质量状态：' + String(monitor.quality.status || '未知') : '尚无质量结果');
    const issues = monitor && Array.isArray(monitor.reasons) ? monitor.reasons : [];
    const reasons = fuel && Array.isArray(fuel.reason_codes) ? fuel.reason_codes : [];
    const limits = Array.isArray(state.limitations) ? state.limitations : [];
    const allIssues = [...new Set([...issues, ...reasons, ...limits])];
    listIssues(allIssues.length ? allIssues : ['未报告新的质量问题；不代表完整策略已验收。']);
    clearEstimates();
    if (!usable || !fuel) {
      put('advice', !driving ? '观战和回放仅验证连接，不提供自己或朋友的燃油驾驶建议。' :
        '当前证据不足，燃油估计已撤下；等待有效的新数据。');
      speakLatest();
      return;
    }
    put('advice', typeof fuel.message === 'string' ? fuel.message : '正在等待有效完整圈。');
    const validLaps = Number.isInteger(fuel.valid_laps) && fuel.valid_laps >= 0 ?
      fuel.valid_laps : null;
    const required = Number.isInteger(fuel.required_laps) && fuel.required_laps > 0 ?
      fuel.required_laps : null;
    put('learning-count', (validLaps === null ? '—' : validLaps) + ' / ' +
      (required === null ? '—' : required) + ' 圈');
    const progress = validLaps !== null && required !== null ?
      Math.min(100, validLaps / required * 100) : 0;
    byId('learning-fill').style.width = progress + '%';
    byId('learning-progress').setAttribute('aria-valuenow', String(Math.round(progress)));
    number('fuel-current', fuel.current_fuel_l, ' L');
    if (fuel.advisor_only === true && fuel.executable === false && fuel.estimate_only === true
      && fuel.status === 'READY') {
      number('fuel-laps', fuel.estimated_laps_remaining, ' 圈');
      number('fuel-finish', fuel.fuel_needed_to_finish_l, ' L');
      number('fuel-add', fuel.fuel_to_add_l, ' L');
      number('fuel-burn', fuel.conservative_burn_l_per_lap, ' L', 2);
      number('fuel-stops', fuel.minimum_stops, ' 次', 0);
    }
    speakLatest();
  }
  async function poll() {
    if (inFlight) return;
    inFlight = true;
    const controller = new AbortController();
    const started = performance.now();
    const timeout = setTimeout(() => {
      controller.abort();
      current = null;
      unavailable('本地服务响应超时', '未收到及时响应，旧估计与语音已取消。');
    }, 900);
    try {
      const response = await fetch('/api/state', {cache: 'no-store',
        credentials: 'same-origin', signal: controller.signal});
      if (!response.ok) throw new Error('State unavailable');
      const state = await response.json();
      if (controller.signal.aborted) return;
      if (!state || typeof state !== 'object' || !Number.isInteger(state.generation)
        || !['LIVE', 'SYNTHETIC_DEMO'].includes(state.source_mode)) {
        throw new Error('Invalid state');
      }
      if (generation !== state.generation) {
        cancelSpeech();
        seenSpeech.clear();
        progressSequence = null;
        generation = state.generation;
      }
      const sequence = state.monitor && state.monitor.sequence;
      if (Number.isInteger(sequence) && sequence >= 0) {
        if (progressSequence !== null && sequence < progressSequence) {
          throw new Error('Snapshot sequence regressed');
        }
        if (sequence !== progressSequence) {
          progressSequence = sequence;
          progressAt = performance.now();
        }
      }
      current = state;
      receivedAt = performance.now();
      requestTransit = (receivedAt - started) / 1000;
      const recording = state.recording;
      const recordLabels = {DISABLED: '未启用', RECORDING: '仅保存在本机',
        WAIT_SIM: '等待模拟器；新连接后自动录制',
        ERROR: '记录已停止：写入错误（面板继续工作）',
        LIMIT_REACHED: '记录已停止：已达容量上限（面板继续工作）'};
      put('recording', '本地记录状态：' +
        (recording && recordLabels[recording.status] || '未知') +
        (recording && finite(recording.bytes) && recording.bytes >= 0 ?
          ' · ' + (recording.bytes / 1048576).toFixed(1) + ' MiB' : ''));
      render();
    } catch (_) {
      current = null;
      unavailable('本地服务失联', '读取状态失败，旧估计与语音已取消。请检查本地服务。');
    } finally {
      clearTimeout(timeout);
      inFlight = false;
    }
  }
  byId('voice-enable').addEventListener('click', () => {
    findVoice();
    if (!voice || document.hidden) return;
    enabled = true;
    byId('voice-enable').setAttribute('aria-pressed', 'true');
    put('voice-badge', '本地语音已启用');
    put('voice-status', '仅等待新的有效提示；不会补播静音期间的旧消息。');
  });
  byId('voice-mute').addEventListener('click', () => mute('已静音，当前及排队播报已取消。'));
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) mute('页面已切到后台，自动静音；返回后请手动重新启用。');
  });
  window.addEventListener('pagehide', () => mute('页面已离开，播报已取消。'));
  if (synth) synth.addEventListener('voiceschanged', findVoice);
  findVoice();
  setInterval(() => {
    if (current && !fresh()) render();
    if (activeSpeech && ((!activeSpeech.started && performance.now() >= activeSpeech.startBy)
      || performance.now() - activeSpeech.startedAt >= 12000
      || !safeDriving() || document.hidden)) cancelSpeech();
  }, 100);
  setInterval(poll, 500);
  poll();
})();
</script>
</body>
</html>
"""
