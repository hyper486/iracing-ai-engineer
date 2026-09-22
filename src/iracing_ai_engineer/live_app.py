"""Loopback experimental fuel display with an optional isolated LLM advisor.

The single reader owns SDK access. HTTP serves copies of bounded public state;
it cannot mutate the reader. The opt-in model sees allowlisted summaries only.
Fuel estimates and model explanations do not promote the M2/M3 gates.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from .collector import (
    CollectorConsistencyError,
    CollectorSample,
    _transport_session_info,
    validate_variable_descriptors,
)
from .dashboard_page import DASHBOARD_HTML
from .live_fuel import LiveFuelConfig, LiveFuelEngineer
from .live_monitor import LIVE_MONITOR_FIELDS, LiveMonitor, _expected_car_count
from .llm_engineer import EngineerConfig, EngineerService
from .runtime_clock import monotonic_now
from .sdk_probe import SdkProbeUnavailable, WindowsPyirsdkTransport
from .telemetry import SourceKind

FRESHNESS_S = 2.0
MAX_EVENTS_PER_CONNECTION = 50_000
LIMITATIONS = [
    "实验燃油估计；不是完整进站策略，不考虑交通、轮胎、处罚或赛事规则。",
    "语音默认关闭，只支持练习中的本地英文声音；比赛保持静音。",
    "本机只读：不启动游戏、不驾驶、不修改进站设置。",
]


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _engineer_safety_binding(monitor: Mapping) -> tuple:
    context, quality = monitor.get("context", {}), monitor.get("quality", {})
    return (
        monitor.get("binding_sha256"), monitor.get("source_kind"),
        monitor.get("status") in ("READY", "DEGRADED"),
        context.get("player_control_state"), context.get("sim_source_mode"),
        tuple(context.get("conflicts", [])), quality.get("stale"),
        quality.get("status") in ("READY", "DEGRADED"),
    )


def bound_session_type(payload: object, update: object, frame: object) -> str | None:
    """Admit only the SessionInfo record bound to this exact frame/update."""
    if not isinstance(payload, Mapping) or update != frame.session_info_update:
        return None
    session_num = frame.values.get("SessionNum")
    if type(session_num) is not int:
        return None
    info = payload.get("SessionInfo")
    sessions = info.get("Sessions") if isinstance(info, Mapping) else None
    if not isinstance(sessions, list):
        return None
    matches = [
        item
        for item in sessions
        if isinstance(item, Mapping)
        and type(item.get("SessionNum")) is int
        and item["SessionNum"] == session_num
    ]
    if len(matches) != 1:
        return None
    value = matches[0].get("SessionType")
    return value if value in ("Practice", "Race", "Qualify", "Lone Qualify", "Warmup") else None


class PracticeFuelSpeech:
    """Fact-only opt-in practice intents, not tactical race speech promotion."""

    def __init__(self) -> None:
        self._safe_since: float | None = None
        self._last_at = -math.inf
        self._intent: dict[str, Any] | None = None
        self._serial = 0

    def update(
        self, monitor: dict, fuel: dict, session_type: str | None, now: float
    ) -> dict | None:
        telemetry = monitor.get("telemetry", {})
        brake, steer, speed = (
            telemetry.get(key) for key in ("brake", "steering_angle_rad", "speed_mps")
        )
        safe = (
            session_type == "Practice"
            and monitor.get("source_kind") == "SDK_LIVE"
            and monitor.get("status") in ("READY", "DEGRADED")
            and monitor.get("context", {}).get("player_control_state") == "IN_CAR_PHYSICS"
            and monitor.get("quality", {}).get("stale") is False
            and not monitor.get("interval_invalid_for_fuel")
            and monitor.get("interval_unsafe_for_speech") == []
            and fuel.get("status") == "READY"
            and telemetry.get("car_left_right") == 1
            and all(_finite(value) for value in (brake, steer, speed))
            and brake <= 0.02
            and abs(steer) <= 0.05
            and speed >= 15
            and telemetry.get("on_pit_road") is False
        )
        if not safe:
            self._safe_since = None
            self._intent = None
            return None
        if self._safe_since is None:
            self._safe_since = now
        if self._intent is not None and now >= self._intent["deadline"]:
            self._intent = None
        amount, laps = fuel.get("current_fuel_l"), fuel.get("estimated_laps_remaining")
        if (
            now - self._safe_since >= 2
            and now - self._last_at >= 60
            and _finite(amount)
            and _finite(laps)
            and 0 <= amount <= 1000
            and 0 <= laps <= 1000
        ):
            self._serial += 1
            self._last_at = now
            self._intent = {
                "id": str(self._serial),
                "text": f"Fuel estimate. {amount:.1f} liters. About {laps:.1f} laps remaining.",
                "priority": "INFORMATION",
                "deadline": now + 2,
            }
        return copy.deepcopy(self._intent)


class AppState:
    """Thread-safe latest-state mailbox; no telemetry or driver-name history."""

    def __init__(self, *, clock: Callable[[], float] = monotonic_now) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._updated: float | None = None
        self._generation = 0
        self._engineer_revision = 0
        self._value: dict[str, Any] = {
            "contract_version": "experimental-live-fuel-app-v1",
            "connection": "WAIT_SIM",
            "monitor": None,
            "fuel": None,
            "speech": None,
            "source_mode": "LIVE",
            "session_type": None,
            "limitations": LIMITATIONS,
            "recording": {"status": "DISABLED", "bytes": 0},
        }
        self._summary: dict[str, Any] = {
            "contract_version": "experimental-live-fuel-summary-v1",
            "advisor_only": True,
            "executable": False,
            "live_acceptance": False,
            "snapshots_seen": 0,
            "connections": 0,
            "max_valid_fuel_laps": 0,
            "recording_bytes": 0,
        }

    def connection(self, status: str) -> None:
        if status not in ("WAIT_SIM", "CONNECTED", "DISCONNECTED", "ERROR", "STOPPED"):
            raise ValueError("invalid connection status")
        with self._lock:
            self._value.update(
                connection=status, monitor=None, fuel=None, speech=None, session_type=None
            )
            self._updated = None
            self._generation += 1
            if status == "CONNECTED":
                self._summary["connections"] += 1

    def recording(self, status: str, byte_count: int) -> None:
        with self._lock:
            self._value["recording"] = {"status": status, "bytes": byte_count}
            self._summary["recording_bytes"] = byte_count

    def publish(
        self, monitor: dict, fuel: dict, speech: dict | None, session_type: str | None
    ) -> None:
        with self._lock:
            previous = self._value.get("monitor") or {}
            previous_fuel = self._value.get("fuel") or {}
            old_level, new_level = previous_fuel.get("current_fuel_l"), fuel.get("current_fuel_l")
            if (
                (self._updated is not None and self.clock() - self._updated > FRESHNESS_S)
                or _engineer_safety_binding(previous) != _engineer_safety_binding(monitor)
                or self._value.get("session_type") != session_type
                or previous.get("telemetry", {}).get("session_num")
                != monitor.get("telemetry", {}).get("session_num")
                or previous_fuel.get("status") != fuel.get("status")
                or monitor.get("interval_invalid_for_fuel")
                or (_finite(old_level) and _finite(new_level) and new_level > old_level + 0.05)
            ):
                self._engineer_revision += 1
            self._updated = self.clock()
            self._value.update(
                connection="CONNECTED",
                monitor=copy.deepcopy(monitor),
                fuel=copy.deepcopy(fuel),
                speech=copy.deepcopy(speech),
                session_type=session_type,
            )
            self._summary["snapshots_seen"] += 1
            count = fuel.get("valid_laps", 0)
            if type(count) is int:
                self._summary["max_valid_fuel_laps"] = max(
                    self._summary["max_valid_fuel_laps"],
                    count,
                )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            value = copy.deepcopy(self._value)
            age = None if self._updated is None else max(0.0, self.clock() - self._updated)
            value.update(updated_age_s=age, generation=self._generation,
                         engineer_revision=self._engineer_revision)
            if value["connection"] == "CONNECTED" and (age is None or age > FRESHNESS_S):
                value.update(connection="DISCONNECTED", fuel=None, monitor=None, speech=None)
            intent = value.get("speech")
            if intent is not None:
                remaining = intent.pop("deadline") - self.clock()
                if remaining <= 0:
                    value["speech"] = None
                else:
                    intent["expires_in_s"] = remaining
                    intent["id"] = f"{self._generation}:{intent['id']}"
            return value

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {**self._summary, "limitations": list(LIMITATIONS)}


def _csp() -> str:
    directives = [
        "default-src 'none'",
        "connect-src 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'none'",
    ]
    for tag, policy in (("script", "script-src"), ("style", "style-src")):
        contents = re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", DASHBOARD_HTML, re.DOTALL)
        hashes = [
            "'sha256-"
            + base64.b64encode(hashlib.sha256(content.encode("utf-8")).digest()).decode("ascii")
            + "'"
            for content in contents
        ]
        directives.append(policy + " " + (" ".join(hashes) if hashes else "'none'"))
    return "; ".join(directives)


def make_server(
    state: AppState, port: int = 8765, *, engineer: EngineerService | None = None
) -> ThreadingHTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid port")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass  # No URLs, SDK errors or private paths in HTTP logs.

        def _respond(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", _csp())
            if self.path == "/api/report":
                self.send_header(
                    "Content-Disposition", 'attachment; filename="engineer-summary.json"'
                )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _get(self) -> None:
            host = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get_all("Origin", [])
            sites = self.headers.get_all("Sec-Fetch-Site", [])
            if (
                self.headers.get_all("Host", []) != [host]
                or origin not in ([], [f"http://{host}"])
                or sites not in ([], ["same-origin"], ["none"])
            ):
                self._respond(403, b"Forbidden", "text/plain")
            elif self.path == "/":
                self._respond(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path in ("/api/state", "/api/report"):
                payload = state.snapshot() if self.path == "/api/state" else state.report()
                self._respond(
                    200,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
                    "application/json; charset=utf-8",
                )
            elif self.path == "/api/engineer" and engineer is not None:
                self._respond(
                    200,
                    json.dumps(engineer.snapshot(), ensure_ascii=False, allow_nan=False).encode(),
                    "application/json; charset=utf-8",
                )
            else:
                self._respond(404, b"Not found", "text/plain")

        do_GET = _get
        do_HEAD = _get

        def do_POST(self) -> None:
            if self.path != "/api/engineer/question" or engineer is None:
                self._respond(405, b"Read only", "text/plain")
                return
            host = f"127.0.0.1:{self.server.server_port}"
            tokens = self.headers.get_all("X-Engineer-Token", [])
            if (
                self.headers.get_all("Host", []) != [host]
                or self.headers.get_all("Origin", []) != [f"http://{host}"]
                or self.headers.get_all("Sec-Fetch-Site", []) not in ([], ["same-origin"])
                or len(tokens) != 1 or not engineer.authenticates(tokens[0])
            ):
                self._respond(403, b'{"error":"FORBIDDEN"}', "application/json")
                return
            lengths = self.headers.get_all("Content-Length", [])
            if (
                self.headers.get_all("Content-Type", []) != ["application/json"]
                or self.headers.get_all("Transfer-Encoding", [])
                or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,4}", lengths[0])
                or not 1 <= int(lengths[0]) <= 4096
            ):
                self._respond(400, b'{"error":"INVALID_REQUEST"}', "application/json")
                return
            try:
                self.connection.settimeout(2.0)
                body = self.rfile.read(int(lengths[0]))
                if len(body) != int(lengths[0]):
                    raise ValueError("incomplete body")

                def unique(pairs: list) -> dict:
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("duplicate key")
                        result[key] = value
                    return result

                payload = json.loads(body, object_pairs_hook=unique)
                if not isinstance(payload, dict) or not {"question"} <= set(payload) <= {
                    "question", "scope"
                }:
                    raise ValueError("invalid payload")
                code, result = engineer.submit(payload["question"], payload.get("scope", "live"))
            except (ValueError, OSError, RecursionError):
                code, result = 400, {"error": "INVALID_REQUEST"}
            self._respond(code, json.dumps(result).encode(), "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def run_reader(
    state: AppState,
    stop: threading.Event,
    config: LiveFuelConfig,
    *,
    transport_factory: Callable = WindowsPyirsdkTransport,
    clock: Callable[[], float] = monotonic_now,
    record_directory: Path | None = None,
    record_max_bytes: int = 4 * 1024**3,
) -> None:
    """One owned reader, fresh model on reconnect, fixed five-second retries."""
    recording_disabled = record_directory is None
    recorded_bytes = 0
    connected_once = False
    if not recording_disabled:
        state.recording("WAIT_SIM", 0)
    while not stop.is_set():
        transport = recorder = None
        exhausted = False
        try:
            transport = transport_factory()
            connection = transport.startup(0)
            descriptors = tuple(transport.descriptors())
            validate_variable_descriptors(descriptors)
            available = {item.name for item in descriptors}
            if not {"SessionNum", "SessionTime", "SessionTick"} <= available:
                raise ValueError("missing core schema")
            selected = tuple(name for name in LIVE_MONITOR_FIELDS if name in available)
            fields = (
                tuple(item.name for item in descriptors) if not recording_disabled else selected
            )
            identifier = uuid4().hex
            monitor = LiveMonitor(
                source_id="local-fuel-app",
                session_id=identifier,
                sdk_tick_rate_hz=connection.tick_rate_hz,
                expected_source_kind=SourceKind.SDK_LIVE,
                expected_car_count=_expected_car_count(descriptors),
            )
            fuel_engineer, speech = LiveFuelEngineer(config), PracticeFuelSpeech()
            if not recording_disabled:
                from .live_app_recording import AppRecorder

                try:
                    recorder = AppRecorder(
                        record_directory,
                        source_id="local-fuel-app",
                        session_id=identifier,
                        max_bytes=record_max_bytes - recorded_bytes,
                    )
                    state.recording("RECORDING", recorded_bytes)
                except (OSError, ValueError, CollectorConsistencyError):
                    recording_disabled = True
                    state.recording("ERROR", recorded_bytes)
            state.connection("CONNECTED")
            connected_once = True
            next_snapshot = clock()
            while not stop.is_set():
                if not transport.connected:
                    raise ConnectionError("SDK disconnected")
                began = clock()
                frame = transport.read_frozen(fields)
                frame, metadata, scope = _transport_session_info(transport, frame)
                session_type = bound_session_type(metadata, frame.session_info_update, frame)
                if recorder is not None:
                    try:
                        # Leave headroom for a complete last metadata/sample group and receipt.
                        if recorded_bytes + recorder.byte_count >= record_max_bytes - 16 * 1024**2:
                            recorder.finish()
                            recorded_bytes += recorder.byte_count
                            recorder.close()
                            recorder = None
                            recording_disabled = True
                            state.recording("LIMIT_REACHED", recorded_bytes)
                        else:
                            recorder.ingest(
                                CollectorSample(
                                    frame, descriptors, connection.tick_rate_hz, metadata, scope
                                )
                            )
                            state.recording("RECORDING", recorded_bytes + recorder.byte_count)
                    except Exception:
                        recorded_bytes += recorder.byte_count
                        failed_recorder = recorder
                        recorder = None
                        recording_disabled = True
                        # Recording teardown must not terminate the SDK owner.
                        with suppress(Exception):
                            failed_recorder.close()
                        state.recording("ERROR", recorded_bytes)
                projected = replace(
                    frame,
                    values={name: frame.values[name] for name in selected if name in frame.values},
                    read_errors=tuple(name for name in frame.read_errors if name in selected),
                )
                monitor.feed(projected, observed_monotonic_s=clock())
                monitor.advance_time(clock())
                if monitor.event_count >= MAX_EVENTS_PER_CONNECTION:
                    exhausted = True
                    raise RuntimeError("event budget exhausted")
                if clock() >= next_snapshot and monitor.snapshot_pending:
                    snapshot = monitor.snapshot()
                    fuel = fuel_engineer.feed(snapshot, session_type=session_type)
                    intent = speech.update(snapshot, fuel, session_type, clock())
                    state.publish(snapshot, fuel, intent, session_type)
                    next_snapshot = clock() + 0.5
                stop.wait(max(0.0, 0.01 - (clock() - began)))
            if recorder is not None:
                recorder.finish()
        except SdkProbeUnavailable:
            state.connection("DISCONNECTED" if connected_once else "WAIT_SIM")
        except Exception:
            # Never publish exception strings: native/SDK paths and metadata are private.
            state.connection("ERROR" if exhausted else "DISCONNECTED")
        finally:
            if recorder is not None:
                recorded_bytes += recorder.byte_count
                try:
                    recorder.close()
                except Exception:
                    recording_disabled = True
                    state.recording("ERROR", recorded_bytes)
                else:
                    state.recording("WAIT_SIM", recorded_bytes)
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    state.connection("ERROR")
        if exhausted:
            return
        if not stop.is_set():
            stop.wait(5)
    state.connection("STOPPED")


def run_live_app(
    *,
    port: int = 8765,
    duration_s: float = 21600,
    config: LiveFuelConfig | None = None,
    record_directory: Path | None = None,
    record_max_bytes: int = 4 * 1024**3,
    engineer_config: EngineerConfig | None = None,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    if not _finite(duration_s) or not 0 < duration_s <= 43200:
        raise ValueError("duration must be between zero and twelve hours")
    if type(record_max_bytes) is not int or record_max_bytes < 32 * 1024**2:
        raise ValueError("recording budget must be at least 32 MiB")
    state, stop = AppState(), threading.Event()
    engineer = EngineerService(state.snapshot, engineer_config)
    try:
        server = make_server(state, port, engineer=engineer)
    except Exception:
        engineer.close()
        raise
    server.timeout = 0.5
    reader = threading.Thread(
        target=run_reader,
        args=(state, stop, config or LiveFuelConfig()),
        kwargs={"record_directory": record_directory, "record_max_bytes": record_max_bytes},
        daemon=True,
    )
    try:
        reader.start()
        if on_ready:
            on_ready(f"http://127.0.0.1:{server.server_port}/")
        deadline = monotonic_now() + duration_s
        while monotonic_now() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        engineer.close()
        server.server_close()
        reader.join(timeout=5)


__all__ = ["AppState", "PracticeFuelSpeech", "bound_session_type", "make_server", "run_live_app"]
