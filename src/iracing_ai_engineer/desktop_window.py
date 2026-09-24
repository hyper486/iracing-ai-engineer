"""Native Tk/ttk advisor UI. No browser, HTTP transport or simulator controls."""

from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

from .live_driving import driving_notice, validated_driving
from .live_rejoin import rejoin_notice, validated_rejoin
from .live_stint import stint_notice
from .live_strategy import StrategyParameters, strategy_notice, validated_strategy
from .live_traffic import validated_traffic
from .llm_evidence import _live_frame_ready
from .runtime_clock import monotonic_now

_DASH = "—"
_BG = "#0b1118"
_PANEL = "#152130"
_TEXT = "#edf3f8"
_MUTED = "#b0c0d1"
_ACCENT = "#9ae4ce"
_WARNING = "#ffda96"
_ERROR = "#ffabab"


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _text(value: object, default: str = "", limit: int = 1000) -> str:
    return value[:limit] if type(value) is str else default


def _finite(value: object) -> bool:
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _answer_counter(value: object) -> int | None:
    """The native controller owns canonical, monotonically increasing IDs."""
    if type(value) is str:
        if not 1 <= len(value) <= 16 or not value.isascii() or not value.isdecimal():
            return None
        number = int(value)
        if str(number) != value:
            return None
        value = number
    return value if type(value) is int and 0 <= value <= 2**53 else None


def format_number(value: object, suffix: str = "", digits: int = 1) -> str:
    """Unknown, invalid and negative measurements remain visibly absent."""
    return f"{value:.{digits}f}{suffix}" if _finite(value) else _DASH


def trial_report_text(report: object) -> str:
    """Fixed report vocabulary: no file contents or provider errors are displayed."""
    value = _mapping(report)
    labels = {
        "REPLAY_MATCH": "近车判定重算一致（不等于功能验收）",
        "NO_DETECTOR_DATA": "没有采集到可回放的近车输入",
        "INCOMPLETE_PREFIX": "日志不完整，仅核对已保存前缀，不能确认全程",
        "REJECTED": "日志校验失败或文件不可用，未接受其结果",
    }
    if type(value.get("status")) is not str or value["status"] not in labels:
        return "选择已结束的本机 trial-*.jsonl 日志；不会播放音频或调用模型。"
    result = [labels[value["status"]]]
    if value.get("status") != "REJECTED":
        outcomes = _mapping(value.get("audio_outcomes"))
        events = _mapping(value.get("event_results"))
        decisions = _mapping(value.get("decisions"))
        for label, count in (
            ("输入帧", value.get("frames")), ("判定候选", decisions.get("CANDIDATE", 0)),
            ("尝试播放", outcomes.get("ATTEMPTED", 0)),
            ("已调用音频输出", outcomes.get("PLAYBACK_STARTED", 0)),
            ("记录到播放错误", outcomes.get("PLAYBACK_ERROR", 0)),
            ("音频服务故障", outcomes.get("FAILED", 0)),
            ("已判定但未记录播放尝试", events.get("DETECTED_NO_RECORDED_ATTEMPT", 0)),
            ("无法关联的音频记录", value.get("unbound_audio_records")),
        ):
            if type(count) is int and 0 <= count <= 2**53:
                result.append(f"{label}：{count}")
        reasons = _mapping(value.get("health_transitions"))
        for code, text in (
            ("SDK_SPOTTER_OFF", "SDK 近车字段为关闭状态"),
            ("NOT_DRIVING_ON_TRACK", "未处于本人赛道驾驶状态"),
            ("FIELD_READ_ERROR", "近车字段读取失败"),
            ("NO_FRESH_TICK", "近车输入停止更新"),
        ):
            if type(reasons.get(code)) is int and reasons[code] > 0:
                result.append("曾观察到：" + text)
        audio_reasons = _mapping(value.get("audio_health_records"))
        for code, text in (
            ("DISABLED", "近车语音未启用"), ("ZERO_VOLUME", "音量为零"),
            ("PHRASE_CACHE_FAILED", "短语音准备失败"),
            ("AUDIO_DEVICE_MISSING", "所选输出设备不可用"),
            ("AUDIO_DEVICE_AMBIGUOUS", "输出设备选择不唯一"),
            ("AUDIO_OUTPUT_UNDERRUN", "音频输出中断"),
        ):
            if type(audio_reasons.get(code)) is int and audio_reasons[code] > 0:
                result.append("音频记录曾显示：" + text)
    result.append("仅本地记录核对；原始采集文件未在此校验，不能证明人耳听到或真实驾驶验收。")
    return "\n".join(result)


def validate_question(question: object) -> str:
    """Mirror the service's bounded text input without sending or saving it."""
    if type(question) is not str:
        raise ValueError("INVALID_QUESTION")
    result = question.strip()
    if not 1 <= len(result) <= 500 or any(ord(c) < 32 and c not in "\n\t" for c in result):
        raise ValueError("INVALID_QUESTION")
    return result


def voice_choices(items: object, *, field: str, default: tuple[str, str]) -> list[tuple[str, str]]:
    """Friendly readonly labels map to exact local identifiers, never vice versa."""
    result = [default]
    seen = {default[1]}
    labels = {default[0]}
    if not isinstance(items, (list, tuple)):
        return result
    for item in items[:200]:
        item = _mapping(item)
        identity, name = item.get(field), item.get("name")
        if (type(identity) is not str or not identity or len(identity) > 512
                or identity in seen or type(name) is not str or not name):
            continue
        label = name[:160]
        culture = _text(item.get("culture"), limit=40)
        if culture:
            label += f" · {culture}"
        candidate, index = label, 2
        while candidate in labels:
            candidate, index = f"{label} ({index})", index + 1
        result.append((candidate, identity))
        seen.add(identity)
        labels.add(candidate)
    return result


@dataclass(frozen=True)
class DesktopView:
    lifecycle: str
    connection: str
    source: str
    context: str
    quality: str
    tone: str
    advice: str
    metrics: dict[str, str]
    learning: str
    progress: float
    recording: str
    issues: str
    engineer_status: str
    engineer_error: str
    budget: str
    can_submit: bool
    session_available: bool
    answer_header: str
    answer_text: str
    notice: str
    spotter: str
    traffic: str
    driving: str
    strategy: str
    stint: str
    rejoin: str


class DesktopPresenter:
    """Pure presentation plus a local freshness and withdrawn-answer latch."""

    def __init__(self) -> None:
        self._generation: int | None = None
        self._sequence: int | None = None
        self._progress_at = 0.0
        # Answer serials and service epochs are monotonic. One high-water mark
        # rejects every older answer, without a race-long set of expired IDs.
        self._answer_order: tuple[int, int] | None = None
        self._answer_signature: tuple[str, str, bool] | None = None
        self._answer_withdrawn = False

    def _fresh(self, telemetry: Mapping, now: float) -> bool:
        generation = telemetry.get("generation")
        sequence = _mapping(telemetry.get("monitor")).get("sequence")
        if type(generation) is not int or generation < 0:
            return False
        if generation != self._generation:
            self._generation, self._sequence = generation, None
        if type(sequence) is not int or sequence < 0:
            return False
        if self._sequence is not None and sequence < self._sequence:
            return False
        if sequence != self._sequence:
            self._sequence, self._progress_at = sequence, now
        age = telemetry.get("updated_age_s")
        return (
            telemetry.get("connection") == "CONNECTED"
            and _finite(age) and age <= 2.0
            and 0 <= now - self._progress_at <= 2.0
        )

    def _answer(self, engineer: Mapping, fresh: bool) -> tuple[str, str]:
        answer = _mapping(engineer.get("answer"))
        origin, scope = answer.get("origin"), answer.get("scope")
        identity = answer.get("id")
        serial = _answer_counter(identity)
        instance = engineer.get("instance_id", 0)  # Synthetic controller has no epoch.
        epoch = _answer_counter(instance)
        if (
            type(identity) is not str or serial is None or serial < 1 or epoch is None
            or origin not in ("deepseek", "local_fallback", "local_live")
            or scope not in ("live_snapshot", "historical_session")
            or type(answer.get("text")) is not str
        ):
            return "尚无回答", (
                "可启用按住说话（PTT）；文字输入请停车后使用。仅解释证据，不操作模拟器。"
            )
        key = (epoch, serial)
        signature = (origin, scope, answer.get("snapshot_was_valid") is not False)
        age = answer.get("age_s")
        invalid = (
            answer.get("stale") is not False or not _finite(age)
            or (scope == "live_snapshot" and answer.get("snapshot_was_valid") is not False
                and not fresh)
        )
        if self._answer_order is None or key > self._answer_order:
            self._answer_order, self._answer_signature = key, signature
            self._answer_withdrawn = False
        if key == self._answer_order:
            self._answer_withdrawn |= invalid or signature != self._answer_signature
        # An out-of-order old packet must neither revive nor poison the newer
        # current answer. A reused ID with a different scope/origin fails closed.
        stale = invalid or key < self._answer_order or self._answer_withdrawn
        origin_label = {
            "local_live": "本地实时直答 · 未调用模型",
            "local_fallback": "本地规则解读 · 未调用模型",
        }.get(origin, "DeepSeek · " + _text(engineer.get("model"), "模型未确认", 80))
        if stale:
            scope_label = "已过期 · 正文已撤回"
        elif scope == "historical_session":
            scope_label = "本次历史报告 · 非实时指令"
        elif answer.get("snapshot_was_valid") is False:
            scope_label = "无有效实时证据 · 仅说明能力边界"
        else:
            scope_label = "提问时的快照 · 非持续策略"
        header = f"{origin_label}\n{scope_label}"
        if _finite(age):
            header += f" · {age:.0f} 秒前"
        body = (
            "这条回答依据的状态已失效，旧数字与建议已撤回。请重新提问以获取当前依据。"
            if stale else _text(answer.get("text"), limit=12000)
        )
        return header, body

    def project(self, snapshot: object, *, now: float) -> DesktopView:
        value = _mapping(snapshot)
        telemetry = _mapping(value.get("telemetry"))
        engineer = _mapping(value.get("engineer"))
        lifecycle = _text(value.get("lifecycle"), "ERROR")
        fresh = self._fresh(telemetry, now) and lifecycle == "RUNNING"
        monitor = _mapping(telemetry.get("monitor"))
        quality = _mapping(monitor.get("quality"))
        context = _mapping(monitor.get("context"))
        fuel = _mapping(telemetry.get("fuel"))
        demo = telemetry.get("source_mode") == "SYNTHETIC_DEMO"
        source = "合成演示 · 非真实遥测" if demo else (
            "实时 SDK 数据源 · 尚非驾驶验收" if telemetry.get("source_mode") == "LIVE"
            else "数据源未确认"
        )
        in_car = (context.get("sim_source_mode") == "FULL"
                  and context.get("player_control_state") == "IN_CAR_PHYSICS")
        usable = (
            fresh and in_car and monitor.get("status") in ("READY", "DEGRADED")
            and quality.get("status") in ("READY", "DEGRADED")
            and quality.get("stale") is False
            and (demo or monitor.get("source_kind") == "SDK_LIVE")
        )
        labels = {"WAIT_SIM": "等待 iRacing", "DISCONNECTED": "遥测已断开",
                  "ERROR": "采集出现错误", "STOPPED": "采集已停止"}
        connection = "遥测已连接" if fresh else labels.get(
            telemetry.get("connection"), "数据过期 / 服务不可用"
        )
        workers = _mapping(telemetry.get("workers"))
        analysis_health = _mapping(workers.get("analysis"))
        if not fresh and telemetry.get("transport_connection") == "CONNECTED":
            connection = "采集已连接 · 分析暂无有效数据"
        quality_text = "质量：" + _text(quality.get("status"), "未知")
        analysis_labels = {
            "STARTING": "分析正在初始化", "WAIT_PREVIOUS": "等待上一段分析退出，近车状态单独显示",
            "DRAINING": "分析正在收尾", "ERROR": "分析已暂停，近车状态单独显示",
        }
        if analysis_health.get("status") in analysis_labels:
            quality_text = analysis_labels[analysis_health["status"]]
            if analysis_health.get("reason") in ("QUEUE_OVERFLOW", "QUEUE_STALE"):
                quality_text += "（队列积压或数据过期）"
        context_label = "正在驾驶 · 实验燃油估计" if usable else (
            "观战 / 回放 / 未进入驾驶" if fresh and not in_car else "无可用实时建议"
        )
        if demo and usable:
            context_label = "合成驾驶演示 · 不能作为真实验收"
        metrics = dict.fromkeys(("current", "laps", "burn", "finish", "add", "stops"), _DASH)
        learning, progress = "— / — 圈", 0.0
        advice = "当前证据不足，旧估计已撤下；请查看模拟器自身信息。"
        if usable:
            metrics["current"] = format_number(fuel.get("current_fuel_l"), " L")
            valid, required = fuel.get("valid_laps"), fuel.get("required_laps")
            if type(valid) is int and valid >= 0 and type(required) is int and required > 0:
                learning = f"{valid} / {required} 圈"
                progress = min(100.0, 100.0 * valid / required)
            advice = _text(fuel.get("message"), "正在等待有效完整圈。", 600)
            if (fuel.get("status") == "READY" and fuel.get("advisor_only") is True
                    and fuel.get("estimate_only") is True and fuel.get("executable") is False):
                for key, field, unit, digits in (
                    ("laps", "estimated_laps_remaining", " 圈", 1),
                    ("burn", "conservative_burn_l_per_lap", " L/圈", 2),
                    ("finish", "fuel_needed_to_finish_l", " L", 1),
                    ("add", "fuel_to_add_l", " L", 1),
                    ("stops", "minimum_stops", " 次", 0),
                ):
                    metrics[key] = format_number(fuel.get(field), unit, digits)
        recording = _mapping(telemetry.get("recording"))
        record_labels = {"DISABLED": "记录未启用", "RECORDING": "正在仅向本机记录",
                         "WAIT_SIM": "等待模拟器后自动记录", "ERROR": "记录异常停止，本段不完整",
                         "STARTING": "正在初始化本机记录", "DRAINING": "记录正在收尾，尚未完成",
                         "COMPLETE": "已完成本段记录，非驾驶验收",
                         "EMPTY": "未录到数据",
                         "RESTART_REQUIRED": "本连接未录制，请待上段结束后重启采集",
                         "LIMIT_REACHED": "已达记录容量上限"}
        record_text = record_labels.get(recording.get("status"), "记录状态未确认")
        record_health = _mapping(workers.get("recording"))
        if recording.get("status") == "RESTART_REQUIRED":
            pass  # Do not disguise an old worker's cleanup as current recording.
        elif record_health.get("reason") == "QUEUE_OVERFLOW":
            record_text = "记录队列已满，本段不完整；采集仍可继续"
        elif record_health.get("status") == "INCOMPLETE":
            record_text = "上段记录未完成，保留已写入部分"
        elif (record_health.get("status") == "DRAINING"
              and record_health.get("generation") != telemetry.get("generation")):
            record_text = "正在结束上一段记录，当前未录制"
        if _finite(recording.get("bytes")):
            record_text += f" · {recording['bytes'] / 1048576:.1f} MiB"
        trial = _mapping(telemetry.get("trial_audit"))
        trial_labels = {
            "RUNNING": "诊断日志正在记录", "STARTING": "诊断日志初始化中",
            "ERROR": "诊断日志故障，需重启应用恢复记录；近车判定独立运行",
            "LIMIT_REACHED": "诊断日志已达容量上限，本段可能不完整",
            "DRAINING": "诊断日志正在收尾",
            "COMPLETE": "上段诊断日志已完成，非驾驶验收", "INCOMPLETE": "上段诊断日志不完整",
            "DISABLED": "诊断日志未启用",
        }
        if trial:
            record_text += "\n" + trial_labels.get(trial.get("status"), "诊断日志状态未知")
        reasons: list[str] = []
        for source_list in (monitor.get("reasons"), fuel.get("reason_codes"),
                            telemetry.get("limitations")):
            if isinstance(source_list, (list, tuple)):
                reasons.extend(_text(item, limit=180) for item in source_list if type(item) is str)
        issue_text = "\n".join(dict.fromkeys(reasons))[:2400] or "尚未报告质量结果。"
        status = engineer.get("status")
        engineer_labels = {
            "DISABLED": "云端未启用 · 可用本地解读", "MISSING_KEY": "缺少密钥 · 可用本地解读",
            "READY": "DeepSeek 已配置（不代表 API 已验证）", "BUSY": "正在整理回答",
            "RATE_LIMITED": "请稍后再次提问", "BUDGET_EXHAUSTED": "云端额度已用尽 · 可用本地解读",
            "ERROR": "模型配置或调用异常 · 可用本地解读",
        }
        engineer_error = ""
        if engineer.get("error"):
            engineer_error = (
                "请检查本机密钥配置格式；当前使用本地解读，不显示原始错误内容。"
                if engineer.get("error") == "MODEL_CONFIGURATION_INVALID" else
                "模型调用失败或响应不合格，已使用本地解读；不会自动重试。"
            )
        used, limit = engineer.get("requests_used"), engineer.get("request_limit")
        budget = (f"云端调用 {used} / {limit} · 本地解读不占云端额度"
                  if type(used) is int and type(limit) is int else "云端额度：未知")
        retry = engineer.get("retry_after_s")
        if _finite(retry) and retry > 0:
            budget += f" · 常规问答再等 {math.ceil(retry)} 秒"
        local_ready = (engineer.get("local_live_available") is True
                       and engineer.get("local_retry_after_s") == 0)
        if engineer.get("local_live_available") is True:
            budget += " · 常用燃油/交通问题本地直答"
        header, answer = self._answer(engineer, fresh)
        proximity = _mapping(telemetry.get("spotter"))
        proximity_status = proximity.get("status")
        proximity_labels = {
            "WAIT_SIM": "等待模拟器", "ACQUIRING": "确认连续遥测中",
            "WAIT_CAR": "等待本人上车并离开维修区", "READY": "近车判定数据就绪",
            "STALE": "遥测已过期，判定暂停", "UNAVAILABLE": "必要字段不可用",
            "DISCONNECTED": "连接已断开", "ERROR": "判定模块异常",
            "STOPPED": "已停止",
        }
        proximity_text = proximity_labels.get(proximity_status, "尚未启动")
        if lifecycle != "RUNNING":
            proximity_text = "已停止" if lifecycle in ("STOPPED", "CLOSED") else "尚未就绪"
        audio = _mapping(_mapping(value.get("voice")).get("spotter"))
        audio_labels = {
            "OFF": "近车语音未启用", "PREPARING": "正在预生成短语音",
            "WAIT_DATA": "语音已预备，等待驾驶数据", "READY": "近车语音已预备",
            "PLAYING": "正在输出近车语音", "PAUSED": "近车语音已暂停，请重新应用设置",
            "ERROR": "近车语音故障，请检查输出设备后重新应用设置",
            "STOPPING": "正在停止近车语音", "CLOSED": "近车语音已关闭",
        }
        audio_text = audio_labels.get(audio.get("status"), "当前仅诊断，近车音频尚未接入")
        if audio.get("status") in ("READY", "WAIT_DATA"):
            audio_text += {
                "UNTESTED": "（设备尚未实际播放验证）",
                "STARTED_NOT_HEARING_CONFIRMED": "（已调用播放，未确认人耳听到）",
                "START_DEADLINE_MISSED": "（最近一次音频启动超时，提示已丢弃）",
                "FAILED": "（输出故障）",
            }.get(audio.get("output_status"), "（输出状态未知）")
        if audio.get("reason") == "ZERO_VOLUME":
            audio_text = "音量为零，近车语音已暂停"
        spotter = f"Spotter：{proximity_text} · {audio_text}"
        traffic_text = "交通观测：数据未就绪或已过期；不是近车清空提示。"
        if fresh and _live_frame_ready(telemetry):
            traffic = validated_traffic(telemetry)
            if traffic is not None:
                if traffic["status"] == "AMBIGUOUS":
                    traffic_text = "交通观测：有车辆纵向距离不超过 5 米，前后关系不明确。"
                elif traffic["eligible_count"] == 0:
                    traffic_text = "交通观测：没有可定位的在赛道对手，不代表赛道清空。"
                else:
                    ahead = traffic["ahead"]["distance_mm"] / 1000
                    behind = traffic["behind"]["distance_mm"] / 1000
                    traffic_text = (f"交通观测：可用数据中前方约 {ahead:.0f} m，"
                                    f"后方约 {behind:.0f} m"
                                    " · 沿赛道距离，不是秒差或出站预测。")
            elif (_mapping(telemetry.get("traffic")).get("reason")
                  == "BOUND_TRACK_LENGTH_UNAVAILABLE"):
                traffic_text = "交通观测：缺少与当前帧匹配的赛道长度，暂不计算车距。"
            elif _mapping(telemetry.get("traffic")).get("reason") == "TRAFFIC_PROCESSING_ERROR":
                traffic_text = "交通观测：分析故障；燃油与近车模块独立运行，重连后重试。"
        driving_text = "驾驶分析：等待本人完整可比圈；不会根据单圈猜测驾驶问题。"
        if fresh and _live_frame_ready(telemetry):
            driving = validated_driving(telemetry)
            if driving is not None:
                driving_text = (f"驾驶分析：近期可比圈 {driving['eligible_laps']} · "
                                "尚无重复证据，可按住说话询问‘哪里可以改进’。")
                if driving["status"] == "READY":
                    point = driving["point"]
                    driving_text = (f"驾驶分析：{point['corner_id']} · "
                                    f"{len(point['evidence_laps'])} 圈重复"
                                    f" · 观测中位损失 {point['loss_s']:.2f} s；"
                                    "练习假设，不保证提速。")
                elif driving["status"] == "LAP_REJECTED":
                    driving_text = "驾驶分析：最近圈未通过完整性或条件筛选，暂不作建议。"
            elif driving_notice(telemetry) is not None:
                driving_text = driving_notice(telemetry)
        strategy_text = "进站比较：等待新鲜的本人车内驾驶数据。"
        rejoin_text = rejoin_notice({})
        if fresh and _live_frame_ready(telemetry):
            strategy = validated_strategy(telemetry)
            if strategy is not None and strategy["status"] == "READY":
                plan = strategy["plan"]
                strategy_text = (f"进站比较：完整圈燃油预算 {plan['fuel_stops']} 停"
                                 " · 可询问‘比较进站方案’；仅手填参数下的估计，不是进站指令。")
            else:
                strategy_text = strategy_notice(telemetry)
            rejoin = validated_rejoin(telemetry)
            rejoin_text = ("出站预测：条件推演可用，可问‘出站预测’；"
                           "历史配速与手填假设，不保证畅通。"
                           if rejoin is not None and rejoin["status"] == "READY" else
                           rejoin_notice({"rejoin": rejoin}) if rejoin is not None else rejoin_text)
        return DesktopView(
            lifecycle=lifecycle, connection=connection, source=source, context=context_label,
            quality=quality_text,
            tone="good" if usable else "warn" if fresh else "bad", advice=advice,
            metrics=metrics, learning=learning, progress=progress, recording=record_text,
            issues=issue_text, engineer_status=engineer_labels.get(status, "问答服务未连接"),
            engineer_error=engineer_error, budget=budget,
            can_submit=lifecycle == "RUNNING" and (local_ready or status in (
                "READY", "MISSING_KEY", "DISABLED", "BUDGET_EXHAUSTED", "ERROR"
            )),
            session_available=_mapping(engineer.get("capabilities")).get("session") is True,
            answer_header=header, answer_text=answer,
            notice=_text(value.get("notice"), limit=600),
            spotter=spotter, traffic=traffic_text, driving=driving_text, strategy=strategy_text,
            stint=stint_notice(telemetry if fresh and _live_frame_ready(telemetry) else {}),
            rejoin=rejoin_text,
        )


class DesktopWindow:
    """All widgets and controller invocations stay on the Tk main thread.

    The controller owns asynchronous SDK/LLM work. Its methods must return
    promptly, including configuration, loading and shutdown requests.
    """

    def __init__(self, root: tk.Tk, controller: Any) -> None:
        self.root, self.controller = root, controller
        self.presenter = DesktopPresenter()
        self._closing = False
        self._destroyed = False
        self._after: str | None = None
        self._initialized_settings = False
        self._initialized_voice = False
        self._voice_available = False
        self._voice_enabled = False
        self._screen_ptt_held = False
        self._voice_binding_requested = False
        self._voice_binding_seen_active = False
        self._voice_binding_before_request: dict[str, object] = {}
        self._voice_binding: dict[str, object] = {"kind": "keyboard", "key": "F9"}
        self._voice_menus: dict[str, tuple[ttk.Combobox, tk.StringVar]] = {}
        self._voice_options: dict[str, list[tuple[str, str]]] = {}
        self._voice_selected = {"input_device": "default", "output_device": "default",
                                "culture": "zh-CN", "voice": ""}
        self._voice_controls: list[ttk.Widget] = []
        self._view: DesktopView | None = None
        self._answer_key: tuple[object, object] | None = None
        self._pending_answer_key: tuple[object, object] | None = None
        self._pending_question = False
        self._controls: list[ttk.Widget] = []
        self._question_buttons: list[ttk.Button] = []
        self._vars: dict[str, tk.StringVar] = {}
        self.provider_var = tk.BooleanVar(root, False)
        self.model_var = tk.StringVar(root, "deepseek-flash")
        self.key_var = tk.StringVar(root, "")
        self.remember_var = tk.BooleanVar(root, False)
        self.recording_var = tk.BooleanVar(root, False)
        self.strategy_vars = {key: tk.StringVar(root, "")
                              for key in StrategyParameters.__dataclass_fields__}
        self.strategy_buttons = []
        self.request_var = tk.StringVar(
            root, "文字问题不会自动提交或朗读；语音请到“语音与 VR”启用。"
        )
        self.action_var = tk.StringVar(root, "")
        self.voice_enabled_var = tk.BooleanVar(root, False)
        self.voice_auto_fuel_var = tk.BooleanVar(root, False)
        self.voice_spotter_var = tk.BooleanVar(root, False)
        self.voice_volume_var = tk.DoubleVar(root, 0.7)
        self.voice_key_var = tk.StringVar(root, "F9")
        self.voice_action_var = tk.StringVar(root, "默认关闭；应用语音设置后才启用按住说话。")
        root.title("AEIS · 原生比赛工程师（实验版）")
        root.geometry("1100x800")
        root.minsize(860, 680)
        root.configure(background=_BG)
        root.protocol("WM_DELETE_WINDOW", self._request_close)
        root.bind("<Destroy>", self._on_destroy, add="+")
        root.bind("<ButtonRelease-1>", self._voice_release, add="+")
        root.bind("<FocusOut>", self._voice_release, add="+")
        root.bind("<Unmap>", self._voice_release, add="+")
        self._style()
        self._build()
        self._after = root.after(0, self._poll)

    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), background=_BG,
                        foreground=_TEXT)
        style.configure("TFrame", background=_BG)
        style.configure("Panel.TFrame", background=_PANEL)
        style.configure("TLabel", background=_BG, foreground=_TEXT)
        style.configure("Muted.TLabel", foreground=_MUTED)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Metric.TLabel", background=_PANEL,
                        font=("Microsoft YaHei UI", 25, "bold"))
        style.configure("Panel.TLabel", background=_PANEL, foreground=_MUTED)
        style.configure("Warn.TLabel", foreground=_WARNING)
        style.configure("TButton", background="#253d4c", foreground=_TEXT, padding=(12, 7))
        style.map("TButton", background=[("active", "#35586b"), ("disabled", "#17222d")],
                  foreground=[("disabled", "#788a9c")])
        style.configure("TCheckbutton", background=_BG, foreground=_TEXT)
        style.map("TCheckbutton", background=[("active", _BG)],
                  foreground=[("disabled", "#788a9c")])
        style.configure("TEntry", fieldbackground=_PANEL, foreground=_TEXT,
                        insertcolor=_TEXT, padding=7)
        style.configure("TCombobox", fieldbackground=_PANEL, foreground=_TEXT, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", _PANEL)],
                  foreground=[("readonly", _TEXT)])
        style.configure("TNotebook", background=_BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=_PANEL, foreground=_MUTED, padding=(18, 9))
        style.map("TNotebook.Tab", background=[("selected", "#29483f")],
                  foreground=[("selected", _TEXT)])
        style.configure("Horizontal.TProgressbar", background=_ACCENT, troughcolor=_PANEL)

    def _label(self, parent, key: str, *, style: str = "TLabel", **kwargs):
        variable = tk.StringVar(self.root, "")
        self._vars[key] = variable
        return ttk.Label(parent, textvariable=variable, style=style, **kwargs)

    def _button(self, parent, text: str, command, *, question: bool = False):
        button = ttk.Button(parent, text=text, command=command)
        self._controls.append(button)
        if question:
            self._question_buttons.append(button)
        return button

    @staticmethod
    def _readonly_text(parent, *, height: int):
        frame = ttk.Frame(parent)
        text = tk.Text(frame, height=height, wrap="word", state="disabled", background=_PANEL,
                       foreground=_TEXT, relief="flat", padx=12, pady=10,
                       font=("Microsoft YaHei UI", 10), selectbackground="#35586b")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return frame, text

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="本地比赛工程师", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="原生 Windows 窗口 · 只读遥测 · 可选后台按住说话（PTT）",
                  style="Muted.TLabel").pack(anchor="w", pady=(3, 9))
        connection_row = ttk.Frame(outer)
        connection_row.pack(fill="x", pady=2)
        self.connection_label = self._label(connection_row, "connection", wraplength=600)
        self.connection_label.pack(side="left")
        self._label(connection_row, "lifecycle", style="Warn.TLabel").pack(side="right")
        self._label(outer, "spotter", style="Warn.TLabel", wraplength=920).pack(
            anchor="w", pady=3,
        )
        self._label(outer, "traffic", style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=3)
        self._label(outer, "driving", style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=3)
        self._label(outer, "strategy", style="Muted.TLabel", wraplength=920).pack(
            anchor="w", pady=3)
        self._label(outer, "stint", style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=3)
        self._label(outer, "rejoin", style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=3)
        self._label(outer, "source", style="Muted.TLabel").pack(anchor="w", pady=3)
        self._label(outer, "notice", style="Warn.TLabel", wraplength=980).pack(anchor="w", pady=4)
        ttk.Label(outer, textvariable=self.action_var, style="Warn.TLabel",
                  wraplength=980).pack(anchor="w")
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True, pady=(8, 0))
        live, engineer, settings, voice = (ttk.Frame(self.notebook) for _ in range(4))
        self.notebook.add(live, text="实时燃油与质量")
        self.notebook.add(engineer, text="工程师问答与复盘")
        self.notebook.add(settings, text="模型与本地设置")
        self.notebook.add(voice, text="语音与 VR")
        self._build_live(self._scroll_page(live))
        self._build_engineer(self._scroll_page(engineer))
        self._build_settings(self._scroll_page(settings))
        self._build_voice(self._scroll_page(voice))

    @staticmethod
    def _scroll_page(parent):
        canvas = tk.Canvas(parent, background=_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(canvas, padding=16)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(
            scrollregion=canvas.bbox("all")
        ))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        def wheel(event):
            # Wheel events target the frame/label under the pointer, not the
            # covered canvas. Keep native scrolling inside text/selection inputs.
            widget = event.widget
            if widget.winfo_class() in ("Text", "TCombobox", "Listbox", "TEntry", "TScale"):
                return None
            while widget is not None:
                if widget in (content, canvas):
                    canvas.yview_scroll(-int(event.delta / 120), "units")
                    return "break"
                widget = getattr(widget, "master", None)
            return None

        parent.winfo_toplevel().bind("<MouseWheel>", wheel, add="+")
        return content

    def _build_live(self, parent) -> None:
        self._label(parent, "context").pack(anchor="w")
        self._label(parent, "advice", wraplength=950, style="Muted.TLabel").pack(anchor="w", pady=6)
        metrics = ttk.Frame(parent)
        metrics.pack(fill="x", pady=7)
        for index, (key, title, note) in enumerate((
            ("current", "当前燃油", "遥测读数 / 升"),
            ("laps", "保守预计续航", "估计圈数，非保证"),
            ("burn", "保守单圈油耗", "完整有效圈的估计"),
            ("finish", "至终点预计需油", "剩余赛程总需求"),
            ("add", "预计还需补充", "仅单箱可完成时显示；需配置油箱容量"),
            ("stops", "燃油进站次数下限", "算术下界，不是进站计划"),
        )):
            panel = ttk.Frame(metrics, style="Panel.TFrame", padding=13)
            panel.grid(row=index // 3, column=index % 3, sticky="nsew", padx=5, pady=5)
            metrics.columnconfigure(index % 3, weight=1, uniform="metric")
            ttk.Label(panel, text=title, style="Panel.TLabel").pack(anchor="w")
            self._label(panel, "metric_" + key, style="Metric.TLabel").pack(anchor="w", pady=5)
            ttk.Label(panel, text=note, style="Panel.TLabel", wraplength=270).pack(anchor="w")
        learning = ttk.Frame(parent)
        learning.pack(fill="x", pady=5)
        ttk.Label(learning, text="完整有效圈 · 油耗学习：").pack(side="left")
        self._label(learning, "learning").pack(side="left")
        self.progress = ttk.Progressbar(learning, maximum=100, mode="determinate", length=200)
        self.progress.pack(side="right", padx=5)
        self._label(parent, "recording", style="Muted.TLabel").pack(anchor="w", pady=3)
        self._label(parent, "quality", style="Muted.TLabel").pack(anchor="w")
        frame, self.issues_text = self._readonly_text(parent, height=3)
        frame.pack(fill="both", expand=True, pady=6)
        ttk.Label(parent, text="实验估计，不是完整轮胎、交通或多停策略；不会设置加油或操控赛车。",
                  wraplength=950, style="Warn.TLabel").pack(anchor="w")

    def _build_engineer(self, parent) -> None:
        self._label(parent, "engineer_status").pack(anchor="w")
        self._label(parent, "engineer_error", wraplength=950, style="Warn.TLabel").pack(anchor="w")
        self._label(parent, "budget", style="Muted.TLabel").pack(anchor="w", pady=4)
        ttk.Label(parent, text="文字输入请停车后使用；驾驶中可选后台 PTT。"
                  "云端仅接收筛选后的工程摘要和问题；请勿输入身份信息。",
                  wraplength=950, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(parent, text="你的问题（最多 500 字）：").pack(anchor="w", pady=(9, 4))
        self.question_text = tk.Text(parent, height=3, wrap="word", background=_PANEL,
                                     foreground=_TEXT, insertbackground=_TEXT, relief="flat",
                                     font=("Microsoft YaHei UI", 10), padx=10, pady=8)
        self.question_text.pack(fill="x")
        self.question_text.bind("<Control-Return>", self._keyboard_submit)
        buttons = ttk.Frame(parent)
        buttons.pack(fill="x", pady=8)
        self._button(buttons, "发送问题", self._submit, question=True).pack(
            side="left", padx=(0, 5)
        )
        for label, question in (
            ("问燃油", "当前燃油还能跑几圈？"),
            ("问策略", "该进站了吗？"),
            ("比较补油", "比较进站方案"),
            ("问交通", "前后车情况？"),
            ("问驾驶", "哪里可以改进？"),
        ):
            self._button(buttons, label, lambda q=question: self._quick(q),
                         question=True).pack(side="left", padx=5)
        stint_buttons = ttk.Frame(parent)
        stint_buttons.pack(fill="x", pady=(0, 8))
        for label, question in (("问本段", "这一段跑了多久"), ("问轮胎", "轮胎怎么样"),
                                ("问配速", "配速变化"), ("问出站", "出站预测")):
            self._button(stint_buttons, label, lambda q=question: self._quick(q),
                         question=True).pack(side="left", padx=(0, 10))
        ttk.Label(parent, textvariable=self.request_var, wraplength=950,
                  style="Muted.TLabel").pack(anchor="w")
        history = ttk.Frame(parent)
        history.pack(fill="x", pady=8)
        self._button(history, "导入会话报告…", self._load_session).pack(side="left", padx=(0, 5))
        self._button(history, "清除历史报告", lambda: self._set_session(None)).pack(
            side="left", padx=5
        )
        self.history_button = self._button(history, "按历史报告提问", self._submit_history,
                                           question=True)
        self.history_button.pack(side="left", padx=5)
        self._label(parent, "answer_header", style="Muted.TLabel", wraplength=950).pack(anchor="w")
        frame, self.answer_text = self._readonly_text(parent, height=7)
        frame.pack(fill="both", expand=True, pady=(7, 0))

    def _build_settings(self, parent) -> None:
        ttk.Label(parent, text="本次连接的燃油进站比较 · 停车后设置").pack(anchor="w")
        ttk.Label(parent, text="进车后应用；换车、换会话、退车或断线后需重新确认，不自动保存。\n"
                  "容量请使用本场规则允许的有效容量。出站四项可一起留空；手填假设不是实测标定。",
                  style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=6)
        grid = ttk.Frame(parent)
        grid.pack(fill="x")
        for row, (key, label) in enumerate((
            ("tank_capacity_l", "有效油箱容量（升，必填）"),
            ("refuel_rate_l_per_s", "加油速率（升/秒，可选）"),
            ("pit_loss_low_s", "通道损失下限（秒，不含驻站）"),
            ("pit_loss_high_s", "通道损失上限（秒，与下限一起填）"),
            ("pit_entry_fraction", "进站口圈长比例（0 至小于 1）"),
            ("pit_exit_fraction", "出站口圈长比例（0 至小于 1）"),
            ("complete_pit_loss_low_s", "完整进站损失下限（秒）"),
            ("complete_pit_loss_high_s", "完整进站损失上限（秒）"),
        )):
            column, row = (row // 4) * 2, row % 4
            ttk.Label(grid, text=label).grid(row=row, column=column, sticky="w", pady=3)
            entry = ttk.Entry(grid, textvariable=self.strategy_vars[key], width=10)
            entry.grid(row=row, column=column + 1, padx=10, pady=3)
            self._controls.append(entry)
        buttons = ttk.Frame(parent)
        buttons.pack(anchor="w", pady=8)
        for label, callback in (("确认本次策略参数", self._configure_strategy),
                                 ("清除策略参数", lambda: self._configure_strategy(clear=True))):
            button = self._button(buttons, label, callback)
            button.pack(side="left", padx=(0, 8))
            self.strategy_buttons.append(button)
        ttk.Label(parent, text="右侧完整损失须包含所有停站服务，并相对留在赛道行驶计算；"
                  "须覆盖所比较的补油量，不能填左侧不含驻站的通道损失。\n"
                  "出站预测还需所有在赛道车辆两圈连续轨迹；赛事规则未核验，不设置游戏油量。",
                  style="Muted.TLabel",
                  wraplength=920).pack(anchor="w")
        ttk.Separator(parent).pack(fill="x", pady=12)
        ttk.Label(parent, text="模型设置影响文字与语音识别后的问题解读，不改变模拟器。",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        enable = ttk.Checkbutton(parent, text="启用 DeepSeek 云端问答（默认关闭）",
                                 variable=self.provider_var)
        enable.pack(anchor="w")
        self._controls.append(enable)
        ttk.Label(parent, text="模型名称").pack(anchor="w", pady=(14, 4))
        model = ttk.Entry(parent, textvariable=self.model_var, width=45)
        model.pack(anchor="w")
        self._controls.append(model)
        ttk.Label(parent, text="API 密钥（不回显；留空保留本次已有密钥）").pack(
            anchor="w", pady=(14, 4)
        )
        self.key_entry = ttk.Entry(parent, textvariable=self.key_var, show="●", width=55)
        self.key_entry.pack(anchor="w")
        self._controls.append(self.key_entry)
        self._label(parent, "key_status", style="Muted.TLabel").pack(anchor="w", pady=4)
        remember = ttk.Checkbutton(parent, text="将密钥加密保存在本机 Windows 账户下",
                                   variable=self.remember_var)
        remember.pack(anchor="w", pady=5)
        self._controls.append(remember)
        ttk.Label(parent, text="取消勾选并应用会删除此前保存的加密副本；"
                  "本次进程中的密钥可继续使用。",
                  wraplength=920, style="Muted.TLabel").pack(anchor="w")
        self._button(parent, "应用模型设置", self._configure).pack(anchor="w", pady=12)
        ttk.Separator(parent).pack(fill="x", pady=12)
        self.recording_check = ttk.Checkbutton(
            parent, text="将原始遥测及近车诊断日志记录到本机（隐私数据，不上传）",
            variable=self.recording_var, command=self._recording,
        )
        self.recording_check.pack(anchor="w", pady=4)
        self._controls.append(self.recording_check)
        ttk.Label(parent, text="切换记录会安全重启采集连接；不会启动、关闭或控制 iRacing。\n"
                  "诊断日志不保存麦克风录音、识别文本或密钥；每次启动最多 1 GiB，原始遥测另计。\n"
                  "语音在“语音与 VR”页单独启用。没有密钥、云端异常或额度耗尽时仍可使用本地解读。",
                  wraplength=920, style="Muted.TLabel").pack(anchor="w", pady=8)
        self._button(parent, "回放近车诊断日志…", self._load_trial).pack(anchor="w", pady=6)
        frame, self.trial_text = self._readonly_text(parent, height=10)
        frame.pack(fill="both", expand=True, pady=6)

    def _build_voice(self, parent) -> None:
        self._label(parent, "voice_status", style="Warn.TLabel").pack(anchor="w")
        self._label(parent, "voice_notice", wraplength=920).pack(anchor="w", pady=4)
        ttk.Label(parent, text="可在 VR / 游戏后台使用：按住 F9 或已绑定的方向盘按钮说话，"
                  "说完松开，等待处理与播报提示。无需切回本窗口。\n"
                  "Quest 头显与桌面麦克风可分开选择；未选择时使用系统默认输入 / 输出。",
                  wraplength=920, style="Muted.TLabel").pack(anchor="w", pady=5)
        enabled = ttk.Checkbutton(parent, text="启用按住说话与后台 PTT（默认关闭，需点击应用）",
                                  variable=self.voice_enabled_var)
        enabled.pack(anchor="w", pady=5)
        self._voice_controls.append(enabled)
        spotter = ttk.Checkbutton(parent, text="启用近车语音（本地优先播报，不依赖麦克风或模型）",
                                  variable=self.voice_spotter_var)
        spotter.pack(anchor="w", pady=3)
        self._voice_controls.append(spotter)
        device_grid = ttk.Frame(parent)
        device_grid.pack(fill="x", pady=7)
        device_grid.columnconfigure(1, weight=1)
        for row, (field, label) in enumerate((
            ("input_device", "麦克风输入"), ("output_device", "播报输出"),
            ("culture", "Windows 识别语言"), ("voice", "Windows 本地播报声音"),
        )):
            ttk.Label(device_grid, text=label).grid(
                row=row, column=0, sticky="w", pady=4, padx=(0, 12)
            )
            variable = tk.StringVar(self.root, "")
            combo = ttk.Combobox(device_grid, textvariable=variable, state="readonly", width=52)
            combo.grid(row=row, column=1, sticky="ew", pady=4)
            self._voice_menus[field] = (combo, variable)
            self._voice_controls.append(combo)
            if field == "culture":
                combo.bind("<<ComboboxSelected>>", self._voice_culture_changed)
        self._voice_button(parent, "刷新本机设备与声音", "voice_refresh_devices").pack(anchor="w")
        volume_row = ttk.Frame(parent)
        volume_row.pack(fill="x", pady=8)
        ttk.Label(volume_row, text="播报音量").pack(side="left", padx=(0, 12))
        scale = ttk.Scale(volume_row, from_=0, to=1, variable=self.voice_volume_var,
                          command=self._voice_volume_label, length=280)
        scale.pack(side="left")
        self._voice_controls.append(scale)
        self._label(volume_row, "voice_volume").pack(side="left", padx=8)
        self._voice_volume_label()
        binding_row = ttk.Frame(parent)
        binding_row.pack(fill="x", pady=5)
        ttk.Label(binding_row, text="后台 PTT 快捷键").pack(side="left", padx=(0, 12))
        self.voice_key_combo = ttk.Combobox(binding_row, textvariable=self.voice_key_var,
                                            values=("F8", "F9", "F10", "F11", "F12"),
                                            state="readonly", width=8)
        self.voice_key_combo.pack(side="left")
        self.voice_key_combo.bind("<<ComboboxSelected>>", self._voice_keyboard_binding)
        self._voice_controls.append(self.voice_key_combo)
        self._voice_button(binding_row, "绑定方向盘按钮", "voice_bind").pack(side="left", padx=8)
        self._voice_button(binding_row, "取消绑定", "voice_cancel_bind").pack(side="left")
        self._label(parent, "voice_binding", style="Muted.TLabel").pack(anchor="w", pady=3)
        ttk.Label(parent, text="请选择未与游戏功能冲突的按键；绑定仅监听按钮，不向游戏发送按键。",
                  wraplength=920, style="Muted.TLabel").pack(anchor="w")
        auto_fuel = ttk.Checkbutton(
            parent, text="自动燃油提醒（默认关闭，仅低负荷直线的受限事实提醒）",
            variable=self.voice_auto_fuel_var,
        )
        auto_fuel.pack(anchor="w", pady=8)
        self._voice_controls.append(auto_fuel)
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=5)
        self.voice_apply_button = self._button(actions, "应用语音设置", self._voice_configure)
        self.voice_apply_button.pack(side="left", padx=(0, 8))
        self._voice_controls.append(self.voice_apply_button)
        self.voice_test_button = self._voice_button(actions, "试听（点击才播放）", "voice_test")
        self.voice_test_button.pack(side="left", padx=(0, 8))
        self.voice_stop_button = self._voice_button(actions, "停止播报", "voice_stop")
        self.voice_stop_button.pack(side="left", padx=(0, 8))
        self.voice_ptt_button = ttk.Button(actions, text="按住此处说话（测试）")
        self.voice_ptt_button.pack(side="left")
        self.voice_ptt_button.bind("<ButtonPress-1>", self._voice_press)
        self.voice_ptt_button.bind("<ButtonRelease-1>", self._voice_release)
        self._voice_controls.append(self.voice_ptt_button)
        self._controls.extend(item for item in self._voice_controls if item not in self._controls)
        ttk.Label(parent, textvariable=self.voice_action_var, wraplength=920,
                  style="Warn.TLabel").pack(anchor="w", pady=5)
        self._label(parent, "voice_transcript", wraplength=920, style="Muted.TLabel").pack(
            anchor="w"
        )
        ttk.Label(parent, text="麦克风仅在按住说话时打开；原始音频不写盘、不发送到云端。"
                  "识别后的问题是否外发，遵循模型页的云端开关。请勿口述身份或密钥。\n"
                  "自动燃油提醒不是完整策略。设备选择与语音不会控制赛车或修改系统麦克风权限。",
                  wraplength=920, style="Muted.TLabel").pack(anchor="w", pady=8)

    def _voice_button(self, parent, text: str, method: str):
        button = self._button(parent, text, lambda: self._voice_call(method))
        self._voice_controls.append(button)
        return button

    def _voice_volume_label(self, _value=None) -> None:
        with suppress(tk.TclError):
            value = self.voice_volume_var.get()
            label = f"{value:.0%}" if _finite(value) and value <= 1 else "音量无效"
            self._vars["voice_volume"].set(label)

    def _voice_keyboard_binding(self, _event=None) -> None:
        self._voice_binding = {"kind": "keyboard", "key": self.voice_key_var.get()}
        self._voice_binding_label()

    def _voice_culture_changed(self, _event=None) -> None:
        # A named voice selected for another language must not silently carry over.
        self._voice_menus["voice"][1].set("")
        self._voice_selected["voice"] = ""
        self._poll()

    def _voice_binding_label(self) -> None:
        if self._voice_binding.get("kind") == "keyboard":
            label = "PTT 按键：" + _text(self._voice_binding.get("key"), "F9", 8)
        else:
            label = "PTT：已选择方向盘按钮"
        self._vars["voice_binding"].set(label + " · 修改快捷键后需应用")

    def _voice_selection(self, field: str) -> str:
        _, variable = self._voice_menus[field]
        for label, identity in self._voice_options.get(field, []):
            if variable.get() == label:
                return identity
        return self._voice_selected[field]

    def _render_voice(self, value: object, *, all_disabled: bool) -> None:
        voice = _mapping(value)
        settings, devices = _mapping(voice.get("settings")), _mapping(voice.get("devices"))
        self._voice_available = bool(voice) and callable(
            getattr(self.controller, "voice_configure", None)
        )
        self._voice_enabled = settings.get("enabled") is True
        self._spotter_enabled = settings.get("spotter_enabled") is True
        if settings and not self._initialized_voice and voice.get("status") != "STARTING":
            self.voice_enabled_var.set(self._voice_enabled)
            self.voice_auto_fuel_var.set(settings.get("auto_fuel") is True)
            self.voice_spotter_var.set(settings.get("spotter_enabled") is True)
            volume = settings.get("volume")
            self.voice_volume_var.set(volume if _finite(volume) and volume <= 1 else 0.7)
            self._voice_volume_label()
            for field, default in self._voice_selected.items():
                self._voice_selected[field] = _text(settings.get(field), default, 512)
                self._voice_menus[field][1].set("")
            self._voice_binding = dict(_mapping(settings.get("binding"))) or self._voice_binding
            key = self._voice_binding.get("key")
            self.voice_key_var.set(key if key in ("F8", "F9", "F10", "F11", "F12") else "F9")
            self._initialized_voice = True
        if self._voice_binding_requested:
            self._voice_binding_seen_active |= voice.get("binding_active") is True
            binding = dict(_mapping(settings.get("binding")))
            if (voice.get("binding_active") is False and binding
                    and (self._voice_binding_seen_active
                         or binding != self._voice_binding_before_request)):
                self._voice_binding = binding
                self._voice_binding_requested = False
        for field, group, identity_field, default in (
            ("input_device", "inputs", "id", ("系统默认输入（麦克风）", "default")),
            ("output_device", "outputs", "id", ("系统默认输出（耳机 / 扬声器）", "default")),
            ("culture", "recognizers", "culture", ("中文识别（zh-CN）", "zh-CN")),
            ("voice", "voices", "name", ("系统本地声音（自动选择）", "")),
        ):
            selected = self._voice_selection(field)
            items = devices.get(group)
            if field == "voice" and isinstance(items, (list, tuple)):
                culture = self._voice_selection("culture")
                items = [item for item in items if _mapping(item).get("culture") == culture]
            options = voice_choices(items, field=identity_field, default=default)
            if selected not in {identity for _, identity in options}:
                options.append(("已选项目（暂未枚举）", selected))
            self._voice_selected[field] = selected
            self._voice_options[field] = options
            combo, variable = self._voice_menus[field]
            combo.configure(values=[label for label, _ in options])
            variable.set(next(label for label, identity in options if identity == selected))
        statuses = {"OFF": "按住说话已关闭", "DISABLED": "按住说话已关闭",
                    "READY": "语音已就绪 · 等待按住说话",
                    "LISTENING": "正在收音 · 松开结束", "RECOGNIZING": "正在识别问题",
                    "WAITING_MODEL": "正在整理工程师回答", "SPEAKING": "正在播报",
                    "BINDING": "等待方向盘按钮", "STARTING": "正在启动本地语音",
                    "ERROR": "语音需要检查", "UNAVAILABLE": "本机语音不可用",
                    "STOPPING": "正在关闭语音", "CLOSED": "语音已停止"}
        self._vars["voice_status"].set(statuses.get(voice.get("status"), "语音服务尚未就绪"))
        self._vars["voice_notice"].set(_text(voice.get("notice"), limit=600))
        self._vars["voice_transcript"].set("最近识别：" + _text(voice.get("transcript"), "—", 500))
        self._voice_binding_label()
        if voice.get("binding_active") is True:
            self._vars["voice_binding"].set("正在等待方向盘按钮；可取消绑定。")
        disabled = (all_disabled or not self._voice_available or not self._initialized_voice
                    or voice.get("status") in ("STARTING", "STOPPING", "CLOSED"))
        for control in self._voice_controls:
            control.state(["disabled"] if disabled else ["!disabled"])
        if disabled or not self._voice_enabled or voice.get("binding_active") is True:
            self.voice_ptt_button.state(["disabled"])
            self._voice_release()
        if disabled or not (self._voice_enabled or self._spotter_enabled):
            self.voice_test_button.state(["disabled"])

    def _voice_configure(self) -> None:
        if self._closing or not self._voice_available or not self._initialized_voice:
            return
        try:
            volume = self.voice_volume_var.get()
            if not _finite(volume) or volume > 1:
                raise ValueError("INVALID_VOLUME")
            self.controller.voice_configure({
                "enabled": self.voice_enabled_var.get(),
                **{field: self._voice_selection(field) for field in self._voice_menus},
                "volume": volume, "binding": dict(self._voice_binding),
                "auto_fuel": self.voice_auto_fuel_var.get(),
                "spotter_enabled": self.voice_spotter_var.get(),
            })
        except Exception:
            self.voice_action_var.set("语音设置未能应用；请检查设备、语言与按键，不显示原始错误。")
        else:
            self.voice_action_var.set("已提交设置；不会自动收音。近车语音启用后可自动播报赛况。")

    def _voice_call(self, method: str) -> bool:
        if self._closing or not self._voice_available or not self._initialized_voice:
            return False
        if method == "voice_press" and not self._voice_enabled:
            return False
        if method == "voice_test" and not (self._voice_enabled or self._spotter_enabled):
            return False
        try:
            getattr(self.controller, method)()
        except Exception:
            self.voice_action_var.set("语音操作未完成，请查看设备与本地语音状态；不会自动重试。")
            return False
        if method == "voice_bind":
            self._voice_release()
            self._voice_binding_requested = True
            self._voice_binding_seen_active = False
            self._voice_binding_before_request = dict(self._voice_binding)
        elif method == "voice_cancel_bind":
            self._voice_binding_requested = False
        self.voice_action_var.set({
            "voice_refresh_devices": "正在后台刷新本机设备与声音。",
            "voice_bind": "请按要绑定的方向盘按钮；可点击取消绑定。",
            "voice_cancel_bind": "已请求取消绑定，请核对当前 PTT 按键。",
            "voice_test": "已请求播放本地试听。", "voice_stop": "已请求停止播报。",
            "voice_press": "按住说话，松开后等待处理。",
            "voice_release": "已松开，等待识别与处理提示。",
        }.get(method, "已提交语音操作。"))
        return True

    def _voice_press(self, _event=None):
        if (not self._screen_ptt_held and not self.voice_ptt_button.instate(["disabled"])
                and self._voice_call("voice_press")):
            self._screen_ptt_held = True
        return "break"

    def _voice_release(self, _event=None):
        if self._screen_ptt_held:
            self._screen_ptt_held = False
            # A disappeared/error snapshot must not strand our held microphone.
            try:
                self.controller.voice_release()
            except Exception:
                self.voice_action_var.set("收音结束请求未完成，请关闭语音并检查本地服务。")
            else:
                self.voice_action_var.set("已松开，等待识别与处理提示。")

    @staticmethod
    def _replace_text(widget: tk.Text, text: str) -> None:
        if widget.get("1.0", "end-1c") == text:
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _poll(self) -> None:
        if self._after is not None:
            with suppress(tk.TclError):
                self.root.after_cancel(self._after)
        self._after = None
        if self._destroyed:
            return
        if self._closing:
            try:
                closed = self.controller.is_closed()
            except Exception:
                closed = False
            if closed:
                self._destroyed = True
                self.root.destroy()
                return
        try:
            snapshot = self.controller.snapshot()
            view = self.presenter.project(snapshot, now=monotonic_now())
        except Exception:
            snapshot = {}
            view = self.presenter.project({"lifecycle": "ERROR"}, now=monotonic_now())
            self.action_var.set("无法读取本地状态；旧估计与回答已撤回，不显示原始异常。")
        self._view = view
        engineer = _mapping(_mapping(snapshot).get("engineer"))
        answer = _mapping(engineer.get("answer"))
        self._answer_key = ((engineer.get("instance_id"), answer.get("id"))
                            if type(answer.get("id")) is str else None)
        if (self._pending_question and self._answer_key is not None
                and self._answer_key != self._pending_answer_key):
            self._pending_question = False
            self.request_var.set("回答已更新；请核对依据范围、时间与限制。")
        settings = _mapping(_mapping(snapshot).get("settings"))
        if settings and not self._initialized_settings:
            self.provider_var.set(settings.get("provider") == "deepseek")
            self.model_var.set(_text(settings.get("model"), "deepseek-flash", 80))
            self.remember_var.set(settings.get("remember_key") is True)
            self.recording_var.set(settings.get("recording_enabled") is True)
            self._initialized_settings = True
        self._vars["key_status"].set(
            "已配置密钥（不显示内容）" if settings.get("key_configured") is True
            else "尚未确认已配置密钥；无密钥时使用本地解读。"
        )
        for key in ("connection", "spotter", "traffic", "driving", "strategy", "stint", "rejoin",
                    "source", "context",
                    "advice", "learning", "recording",
                    "quality", "engineer_status", "engineer_error", "budget", "answer_header",
                    "notice"):
            self._vars[key].set(getattr(view, key))
        lifecycle = {"STARTING": "正在启动本地服务", "RUNNING": "只读服务运行中",
                     "STOPPING": "正在安全关闭采集与模型任务", "STOPPED": "本地服务已停止",
                     "ERROR": "本地服务需要检查"}
        self._vars["lifecycle"].set(lifecycle["STOPPING"] if self._closing else
                                     lifecycle.get(view.lifecycle, "服务状态未确认"))
        self.connection_label.configure(foreground={"good": _ACCENT, "warn": _WARNING,
                                                    "bad": _ERROR}[view.tone])
        for key, text in view.metrics.items():
            self._vars["metric_" + key].set(text)
        self.progress["value"] = view.progress
        self._replace_text(self.issues_text, view.issues)
        self._replace_text(self.answer_text, view.answer_text)
        self._replace_text(self.trial_text, trial_report_text(snapshot.get("trial_report")))
        all_disabled = self._closing or view.lifecycle in ("STOPPING", "STOPPED")
        for control in self._controls:
            control.state(["disabled"] if all_disabled else ["!disabled"])
        for button in self._question_buttons:
            button.state(["!disabled"] if view.can_submit and not all_disabled else ["disabled"])
        if not view.session_available:
            self.history_button.state(["disabled"])
        if not callable(getattr(self.controller, "set_recording", None)):
            self.recording_check.state(["disabled"])
        if not callable(getattr(self.controller, "configure_strategy", None)):
            for button in self.strategy_buttons:
                button.state(["disabled"])
        self.question_text.configure(state="disabled" if all_disabled else "normal")
        self._render_voice(_mapping(snapshot).get("voice"),
                           all_disabled=all_disabled or view.lifecycle != "RUNNING")
        self._after = self.root.after(250, self._poll)

    def _submit(self, scope: str = "live") -> None:
        if self._closing or self._view is None or not self._view.can_submit:
            return
        if scope == "session" and not self._view.session_available:
            return
        try:
            question = validate_question(self.question_text.get("1.0", "end-1c"))
        except ValueError:
            self.request_var.set("请输入 1 到 500 字的问题，不包含控制字符。")
            return
        self._pending_question = True
        self._pending_answer_key = self._answer_key
        try:
            code, response = self.controller.submit(question, scope=scope)
        except Exception:
            self.request_var.set("提问未完成；不会自动重试，请查看本地服务状态。")
            return
        if code == 202:
            self.request_var.set("问题已接收，等待回答；不会自动重发或朗读。")
            for button in self._question_buttons:
                button.state(["disabled"])
        else:
            self._pending_question = False
            code_label = {"BUSY": "上一条问题仍在处理。",
                          "RATE_LIMITED": "提问较频繁，请稍后重试。",
                          "NO_SESSION_ARTIFACT": "请先导入有效会话报告。",
                          "INVALID_QUESTION": "请输入有效的 1 到 500 字问题。"}
            self.request_var.set(code_label.get(_mapping(response).get("error"),
                                               "本次请求未完成；不会自动重试。"))

    def _keyboard_submit(self, _event):
        self._submit()
        return "break"

    def _quick(self, question: str) -> None:
        if self._closing or self._view is None or not self._view.can_submit:
            return
        self.question_text.delete("1.0", "end")
        self.question_text.insert("1.0", question)
        self._submit()

    def _submit_history(self) -> None:
        if self._closing:
            return
        if not self.question_text.get("1.0", "end-1c").strip():
            self.question_text.insert("1.0", "请复盘本次历史会话的有效证据、质量问题和分析边界。")
        self._submit("session")

    def _configure_strategy(self, *, clear=False) -> None:
        if self._closing:
            return
        try:
            parameters = (None if clear else {
                key: float(variable.get().strip()) if variable.get().strip() else None
                for key, variable in self.strategy_vars.items()
            })
            if parameters is not None:
                StrategyParameters(**parameters)
            self.controller.configure_strategy(parameters)
        except ValueError as exc:
            self.action_var.set("请先进入本人车辆并等待新鲜遥测，再确认本次参数。"
                                if str(exc) == "STRATEGY_SOURCE_NOT_READY" else
                                "参数无效：容量 0.1–1000 升，速率 0.05–50 升/秒，"
                                "通道上下限 0–1000 秒；出站四项须一起填："
                                "位置 0≤比例<1 且互异，完整损失 0.1–600 秒，低≤高。")
        except Exception:
            self.action_var.set("策略参数未能应用；未重启采集或改动游戏设置。")
        else:
            self.action_var.set("本次策略参数已清除。" if clear else
                                "参数已应用于本次连接，可询问‘比较进站方案’或‘出站预测’；不会自动保存。")
            if clear:
                for variable in self.strategy_vars.values():
                    variable.set("")

    def _configure(self) -> None:
        if self._closing:
            return
        key = self.key_var.get().strip() or None
        try:
            self.controller.configure(
                provider="deepseek" if self.provider_var.get() else "off",
                model=self.model_var.get().strip(), api_key=key,
                remember_key=self.remember_var.get(),
            )
        except ValueError:
            self.action_var.set("模型或密钥设置格式无效，请检查；原始错误与密钥不会显示。")
        except Exception:
            self.action_var.set("设置未能应用，请查看本地服务状态。")
        else:
            self.action_var.set("已提交设置，正在后台应用；密钥输入框已清空。")
        finally:
            self.key_var.set("")

    def _load_session(self) -> None:
        if self._closing:
            return
        name = filedialog.askopenfilename(
            parent=self.root, title="选择工程师会话报告（非原始遥测）",
            filetypes=[("工程师会话 JSON", "*.json")],
        )
        if name:
            self._set_session(Path(name))

    def _set_session(self, path: Path | None) -> None:
        if self._closing:
            return
        try:
            self.controller.load_session(path)
        except Exception:
            self.action_var.set("未能加载会话报告，请检查格式和本地服务状态。")
        else:
            self.action_var.set("正在后台验证会话报告。" if path else "已请求清除历史报告。")

    def _load_trial(self) -> None:
        if self._closing:
            return
        directory = getattr(self.controller, "trial_directory", None)
        name = filedialog.askopenfilename(
            parent=self.root, title="选择已结束的本机近车诊断日志（不会播放声音）",
            filetypes=[("本机诊断 JSONL", "trial-*.jsonl")],
            **({"initialdir": str(directory)} if isinstance(directory, Path) else {}),
        )
        if name:
            try:
                self.controller.replay_trial(Path(name))
            except Exception:
                self.action_var.set("暂时无法回放，请等待当前后台操作完成。")
            else:
                self.action_var.set("正在后台核对日志；不播放声音、不调用模型。")

    def _recording(self) -> None:
        if self._closing:
            return
        try:
            self.controller.set_recording(self.recording_var.get())
        except Exception:
            self.action_var.set("记录设置未能应用，请查看本地服务状态。")
        else:
            self.action_var.set("正在后台切换本机记录，采集可能短暂重连。")

    def _request_close(self) -> None:
        if self._closing:
            return
        self._voice_release()
        self._closing = True
        self._vars["lifecycle"].set("正在安全关闭采集与模型任务，请稍候…")
        for control in self._controls:
            control.state(["disabled"])
        self.question_text.configure(state="disabled")
        try:
            self.controller.close()
        except Exception:
            self._closing = False
            self.action_var.set("关闭请求未完成，请稍后再次关闭；不会强制遗留后台任务。")

    def _on_destroy(self, event) -> None:
        if event.widget is not self.root:
            return
        self._destroyed = True
        if self._after is not None:
            with suppress(tk.TclError):
                self.root.after_cancel(self._after)
            self._after = None

    def run(self) -> None:
        self.root.mainloop()


__all__ = ["DesktopPresenter", "DesktopView", "DesktopWindow", "format_number", "validate_question",
           "voice_choices"]
