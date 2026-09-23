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
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from .collector import (
    CollectorSample,
    _transport_session_info,
    validate_variable_descriptors,
)
from .dashboard_page import DASHBOARD_HTML
from .live_driving import LiveDrivingEngineer
from .live_fuel import LiveFuelConfig, LiveFuelEngineer
from .live_monitor import LIVE_MONITOR_FIELDS, LiveMonitor, _expected_car_count
from .live_traffic import (
    bound_track_length_mm,
    project_live_traffic,
    situation_binding,
    unavailable_traffic,
)
from .live_worker import FrameWorker, payload_size
from .llm_engineer import EngineerConfig, EngineerService
from .runtime_clock import monotonic_now
from .sdk_probe import SdkProbeUnavailable, WindowsPyirsdkTransport
from .spotter import SPOTTER_FIELDS, ProximitySpotter
from .telemetry import SourceKind

FRESHNESS_S = 2.0
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
        self._situation_revision = 0
        self._spotter = ProximitySpotter()
        self._spotter_failed = False
        self._workers = {}
        self._recording_base_bytes = 0
        self._value: dict[str, Any] = {
            "contract_version": "experimental-live-fuel-app-v1",
            "connection": "WAIT_SIM",
            "monitor": None,
            "fuel": None,
            "traffic": None,
            "driving": None,
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
                connection=status, monitor=None, fuel=None, traffic=None, driving=None, speech=None,
                session_type=None,
            )
            self._updated = None
            self._generation += 1
            if status == "CONNECTED":
                self._summary["connections"] += 1
            else:
                self._spotter.unavailable(status, now=self.clock())

    def start_spotter(self, tick_rate_hz: int) -> None:
        with self._lock:
            self._spotter = ProximitySpotter(tick_rate_hz=tick_rate_hz)
            self._spotter_failed = False

    def feed_spotter(self, frame) -> None:
        """Fast, non-audible branch before recording and slower display analysis.

        A detector fault latches its own error until reconnect. It must not stop
        SDK capture, replace missing data with zero, or expose native errors.
        """
        with self._lock:
            if self._spotter_failed:
                return
            try:
                # A snapshot may advance the detector while this caller waits
                # for the lock. Sample time here, not before acquiring it.
                self._spotter.feed(frame, now=self.clock())
            except Exception:
                self._spotter_failed = True
                self._spotter.unavailable("ERROR", now=self.clock())

    def spotter_audit(self) -> list[dict]:
        with self._lock:
            return self._spotter.audit()

    def spotter_snapshot(self) -> dict:
        """Fast consumer contract: no fuel/display freshness or LLM dependency."""
        with self._lock:
            return {
                "spotter": self._spotter.snapshot(now=self.clock()),
                "generation": self._generation, "connection": self._value["connection"],
                "source_mode": self._value["source_mode"],
            }

    def recording(self, status: str, byte_count: int) -> None:
        with self._lock:
            self._value["recording"] = {"status": status, "bytes": byte_count}
            self._summary["recording_bytes"] = byte_count

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def attach_worker(self, name, worker, *, recording_base_bytes=0):
        if name not in ("analysis", "recording"):
            raise ValueError("INVALID_WORKER")
        with self._lock:
            self._workers[name] = (self._generation, worker)
            if name == "recording":
                self._recording_base_bytes = recording_base_bytes

    def invalidate_analysis(self, generation):
        with self._lock:
            if generation == self._generation:
                self._updated = None
                self._engineer_revision += 1
                self._situation_revision += 1
                self._value.update(monitor=None, fuel=None, traffic=None, driving=None, speech=None)

    def _worker_status(self):
        # Never take a worker lock while holding the AppState lock. A worker's
        # small publish/failure callback may itself need AppState.
        with self._lock:
            workers = dict(self._workers)
            current, base = self._generation, self._recording_base_bytes
            connected = self._value["connection"] == "CONNECTED"
        rows = {name: {**worker.snapshot(), "generation": generation}
                for name, (generation, worker) in workers.items()}
        for name, row in rows.items():
            if name == "analysis" and row["generation"] != current and not row["done"]:
                row.update(status="WAIT_PREVIOUS", reason="PREVIOUS_SESSION_WORKER")
        recorder = rows.get("recording")
        recording = None
        if recorder is not None:
            status = {"STARTING": "STARTING", "RUNNING": "RECORDING",
                      "DRAINING": "DRAINING", "COMPLETE": "COMPLETE",
                      "INCOMPLETE": "WAIT_SIM", "ERROR": "ERROR"}[recorder["status"]]
            if recorder["status"] == "COMPLETE" and recorder["reason"] == "LIMIT_REACHED":
                status = "LIMIT_REACHED"
            elif recorder["status"] == "COMPLETE" and recorder["processed_frames"] == 0:
                status = "EMPTY"
            if (connected and recorder["generation"] != current and not recorder["failed"]
                    and recorder["reason"] != "LIMIT_REACHED"):
                status = "RESTART_REQUIRED"
            recording = {"status": status, "bytes": base + recorder["bytes"]}
        return rows, recording

    def publish(
        self, monitor: dict, fuel: dict, speech: dict | None, session_type: str | None,
        *, observed_at=None, generation=None, allowed=lambda: True, traffic=None, driving=None,
    ) -> None:
        with self._lock:
            if (generation is not None and generation != self._generation) or not allowed():
                return
            previous = self._value.get("monitor") or {}
            previous_fuel = self._value.get("fuel") or {}
            old_level, new_level = previous_fuel.get("current_fuel_l"), fuel.get("current_fuel_l")
            source_changed = (
                (self._updated is not None and self.clock() - self._updated > FRESHNESS_S)
                or _engineer_safety_binding(previous) != _engineer_safety_binding(monitor)
                or self._value.get("session_type") != session_type
                or previous.get("telemetry", {}).get("session_num")
                != monitor.get("telemetry", {}).get("session_num")
                or any(event.get("kind") in ("source_reset", "session_reset")
                       for event in monitor.get("events", []))
                or bool(set(monitor.get("interval_invalid_for_fuel", [])) & {
                    "OUT_OF_CAR_INTERVAL", "IDENTITY_CHANGED_INTERVAL", "NONLIVE_INTERVAL",
                    "SOURCE_STALE",
                })
            )
            if (source_changed or situation_binding(self._value)
                    != situation_binding({"monitor": monitor, "traffic": traffic})):
                self._situation_revision += 1
            if (
                source_changed or previous_fuel.get("status") != fuel.get("status")
                or monitor.get("interval_invalid_for_fuel")
                or (_finite(old_level) and _finite(new_level) and new_level > old_level + 0.05)
            ):
                self._engineer_revision += 1
            self._updated = self.clock() if observed_at is None else observed_at
            self._value.update(
                connection="CONNECTED",
                monitor=copy.deepcopy(monitor),
                fuel=copy.deepcopy(fuel),
                traffic=copy.deepcopy(traffic),
                driving=copy.deepcopy(driving),
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
        workers, recording = self._worker_status()
        with self._lock:
            value = copy.deepcopy(self._value)
            value["transport_connection"] = value["connection"]
            value["workers"] = workers
            if recording is not None:
                value["recording"] = recording
            value["spotter"] = self._spotter.snapshot(now=self.clock())
            age = None if self._updated is None else max(0.0, self.clock() - self._updated)
            analysis = workers.get("analysis")
            if analysis is not None and (
                analysis["failed"] or analysis["generation"] != self._generation
            ):
                # Defense in depth if a fault-notification callback could not run.
                value.update(monitor=None, fuel=None, traffic=None, driving=None, speech=None)
                age = None
            value.update(updated_age_s=age, generation=self._generation,
                         engineer_revision=self._engineer_revision,
                         situation_revision=self._situation_revision)
            if value["connection"] == "CONNECTED" and (age is None or age > FRESHNESS_S):
                value.update(connection="DISCONNECTED", fuel=None, monitor=None, traffic=None,
                             driving=None,
                             speech=None)
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
        workers, recording = self._worker_status()
        with self._lock:
            return {**self._summary, "workers": workers,
                    "recording_bytes": (self._summary["recording_bytes"] if recording is None
                                        else recording["bytes"]),
                    "limitations": list(LIMITATIONS),
                    "spotter": self._spotter.snapshot(now=self.clock())}


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


class _LiveAnalysis:
    """Single analysis owner; publishing remains source-generation/age bound."""

    byte_count = 0

    def __init__(self, state, config, *, identifier, tick_rate, car_count, generation, allowed):
        self._state, self._generation, self._allowed = state, generation, allowed
        self._monitor = LiveMonitor(
            source_id="local-fuel-app", session_id=identifier, sdk_tick_rate_hz=tick_rate,
            expected_source_kind=SourceKind.SDK_LIVE, expected_car_count=car_count,
        )
        self._fuel, self._speech = LiveFuelEngineer(config), PracticeFuelSpeech()
        try:
            self._driving = LiveDrivingEngineer(tick_rate, clock=state.clock)
        except Exception:
            # A coaching-worker startup failure cannot disable fuel/traffic.
            self._driving = None
        self._traffic_failed = False
        self._next_snapshot = -math.inf

    def process(self, item):
        frame, session_type, observed, track_length_mm = item
        progressed = self._monitor.feed(frame, observed_monotonic_s=observed)
        self._monitor.advance_time(observed)
        if progressed and self._driving is not None:
            try:
                self._driving.feed(frame, self._monitor.latest_sample, track_length_mm)
            except Exception:
                self._driving.fail()
        if observed >= self._next_snapshot and self._monitor.snapshot_pending:
            snapshot = self._monitor.snapshot()
            if (self._driving is not None
                    and snapshot.get("quality", {}).get("stale") is not False):
                self._driving.reset("SOURCE_STALE")
            fuel = self._fuel.feed(snapshot, session_type=session_type)
            intent = self._speech.update(snapshot, fuel, session_type, observed)
            traffic = unavailable_traffic(snapshot, "TRAFFIC_PROCESSING_ERROR")
            if not self._traffic_failed:
                try:
                    traffic = project_live_traffic(
                        self._monitor.latest_sample, snapshot, track_length_mm,
                        metadata_update=frame.session_info_update,
                    )
                except Exception:
                    # Isolate an analytical fault; no native errors or retry storm.
                    self._traffic_failed = True
            self._state.publish(snapshot, fuel, intent, session_type, observed_at=observed,
                                generation=self._generation, allowed=self._allowed, traffic=traffic,
                                driving=(self._driving.snapshot(snapshot)
                                         if self._driving is not None
                                         else {"status": "ERROR"}))
            self._next_snapshot = observed + 0.5

    def finish(self):
        pass

    def close(self):
        if self._driving is not None:
            self._driving.close()


class _RecordingSink:
    def __init__(self, directory, *, identifier, max_bytes):
        from .live_app_recording import AppRecorder

        self._recorder = AppRecorder(directory, source_id="local-fuel-app",
                                     session_id=identifier, max_bytes=max_bytes)

    @property
    def byte_count(self):
        return self._recorder.byte_count

    def process(self, sample):
        self._recorder.ingest(sample)

    def finish(self):
        self._recorder.finish()

    def close(self):
        self._recorder.close()


def _start_analysis(state, config, identifier, tick_rate, descriptors, worker_factory, clock):
    generation, holder = state.generation, {}
    car_count = _expected_car_count(descriptors)
    worker = worker_factory(
        lambda: _LiveAnalysis(state, config, identifier=identifier, tick_rate=tick_rate,
                              car_count=car_count, generation=generation,
                              allowed=lambda: holder["worker"].healthy),
        name="live-analysis", clock=clock, max_age_s=0.5,
        on_failure=lambda: state.invalidate_analysis(generation),
    )
    holder["worker"] = worker
    state.attach_worker("analysis", worker)
    return worker


def run_reader(
    state: AppState,
    stop: threading.Event,
    config: LiveFuelConfig,
    *,
    transport_factory: Callable = WindowsPyirsdkTransport,
    clock: Callable[[], float] = monotonic_now,
    record_directory: Path | None = None,
    record_max_bytes: int = 4 * 1024**3,
    worker_factory: Callable = FrameWorker,
) -> None:
    """One SDK owner and at most one worker per slow lane; never join in the loop.

    Workers own all recorder/analysis startup, work and teardown. They preserve
    order and fail explicitly on bounded queue overflow. Reconnection can keep
    proximity alive even if an old slow worker has not returned. Only final app
    shutdown joins those owners, after the SDK handle has already been closed.
    """
    recording_disabled = record_directory is None
    recorded_bytes = 0
    connected_once = False
    teardown_failed = False
    analysis_worker = recording_worker = None
    if not recording_disabled:
        state.recording("WAIT_SIM", 0)
    while not stop.is_set():
        transport = active_analysis = active_recording = None
        orderly = False
        try:
            transport = transport_factory()
            connection = transport.startup(0)
            descriptors = tuple(transport.descriptors())
            validate_variable_descriptors(descriptors)
            available = {item.name for item in descriptors}
            if not {"SessionNum", "SessionTime", "SessionTick"} <= available:
                raise ValueError("missing core schema")
            selected = tuple(dict.fromkeys(
                name for name in (*LIVE_MONITOR_FIELDS, *SPOTTER_FIELDS) if name in available
            ))
            identifier = uuid4().hex
            state.connection("CONNECTED")
            state.start_spotter(connection.tick_rate_hz)
            connected_once = True
            if analysis_worker is None or analysis_worker.done:
                active_analysis = analysis_worker = _start_analysis(
                    state, config, identifier, connection.tick_rate_hz, descriptors,
                    worker_factory, clock,
                )
            # An old blocked analysis owner is not replaced by another thread.
            # Its stale generation can never publish into this new connection.
            if not recording_disabled:
                if recording_worker is not None:
                    previous = recording_worker.snapshot()
                    if not previous["done"] or previous["failed"]:
                        recording_disabled = True
                    else:
                        recorded_bytes += previous["bytes"]
                if not recording_disabled:
                    active_recording = worker_factory(
                        lambda identifier=identifier, remaining=record_max_bytes - recorded_bytes:
                            _RecordingSink(record_directory, identifier=identifier,
                                           max_bytes=remaining),
                        name="private-recording", clock=clock,
                    )
                    recording_worker = active_recording
                    state.attach_worker("recording", active_recording,
                                        recording_base_bytes=recorded_bytes)
            # Descriptor strings are immutable and shared, but conservatively
            # account their retained footprint for every queued recording item.
            descriptor_bytes = 0
            if active_recording is not None:
                try:
                    descriptor_bytes = payload_size(tuple(
                        (item.name, item.dtype, item.unit, item.description,
                         item.count, item.offset)
                        for item in descriptors
                    ))
                except ValueError:
                    active_recording.fail_payload()
                    recording_disabled = True
            while not stop.is_set():
                if not transport.connected:
                    raise ConnectionError("SDK disconnected")
                if active_analysis is None and analysis_worker is not None and analysis_worker.done:
                    # A previous connection's blocked owner has finally exited.
                    # Start a fresh model, never reuse its old laps or answers.
                    active_analysis = analysis_worker = _start_analysis(
                        state, config, identifier, connection.tick_rate_hz, descriptors,
                        worker_factory, clock,
                    )
                began = clock()
                fields = (tuple(item.name for item in descriptors)
                          if active_recording is not None and not recording_disabled else selected)
                frame = transport.read_frozen(fields)
                frame, metadata, scope = _transport_session_info(transport, frame)
                session_type = bound_session_type(metadata, frame.session_info_update, frame)
                state.feed_spotter(frame)
                observed = clock()
                if active_recording is not None and not recording_disabled:
                    info = active_recording.snapshot()
                    if info["failed"]:
                        recording_disabled = True
                    elif (recorded_bytes + info["bytes"] + info["buffered_bytes"]
                          >= record_max_bytes - 16 * 1024**2):
                        # Stop admitting frames first, then drain all admitted
                        # work and seal exactly that prefix. No queue eviction.
                        active_recording.close(complete=True, reason="LIMIT_REACHED")
                        recording_disabled = True
                if active_recording is not None and not recording_disabled:
                    try:
                        size = descriptor_bytes + payload_size(
                            (frame.values, metadata, frame.read_errors, frame.sim_mode_raw))
                    except ValueError:
                        active_recording.fail_payload()
                        recording_disabled = True
                    else:
                        if not active_recording.submit(
                            CollectorSample(frame, descriptors, connection.tick_rate_hz,
                                            metadata, scope), size=size, observed_at=observed,
                        ):
                            recording_disabled = True
                if active_analysis is not None and active_analysis.healthy:
                    track_length_mm = bound_track_length_mm(metadata, frame.session_info_update,
                                                            frame)
                    projected = replace(
                        frame, values={name: frame.values[name]
                                       for name in selected if name in frame.values},
                        read_errors=tuple(name for name in frame.read_errors if name in selected),
                    )
                    try:
                        size = payload_size((projected.values, projected.read_errors,
                                             projected.sim_mode_raw, session_type, track_length_mm))
                    except ValueError:
                        active_analysis.fail_payload()
                    else:
                        active_analysis.submit((projected, session_type, observed, track_length_mm),
                                               size=size, observed_at=observed)
                stop.wait(max(0.0, 0.01 - (clock() - began)))
            orderly = True
        except SdkProbeUnavailable:
            state.connection("DISCONNECTED" if connected_once else "WAIT_SIM")
        except Exception:
            # Never publish exception strings: native/SDK paths and metadata are private.
            state.connection("DISCONNECTED")
        finally:
            for worker in (active_analysis, active_recording):
                if worker is not None:
                    worker.close(complete=orderly)
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    state.connection("ERROR")
                    teardown_failed = True
        if teardown_failed:
            break  # Do not accumulate SDK owners whose release was not acknowledged.
        if not stop.is_set():
            stop.wait(5)
    if not teardown_failed:
        state.connection("STOPPED")
    for worker in (analysis_worker, recording_worker):
        if worker is not None:
            worker.join()


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
