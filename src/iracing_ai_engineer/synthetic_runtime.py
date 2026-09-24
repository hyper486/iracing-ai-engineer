"""Explicit, hardware-free exercise of the installed live numerical pipeline.

All frames are invented. Internal SDK-shaped tags exercise production
guards, not source authenticity. Only aggregate SYNTHETIC results escape this
module. The explicit capture self-test writes only invented temporary data;
there is no SDK transport, provider, credentials, microphone or playback.
Accelerated time is paced at lap-worker boundaries; this is not a latency test.
"""

from __future__ import annotations

import math
import os
import time
from collections import Counter
from dataclasses import replace

import numpy as np

from .live_app import AppState, _LiveAnalysis
from .live_driving import MAX_JOB_BYTES, MAX_LAPS, MAX_ROWS, validated_driving, validated_pace
from .live_fuel import LiveFuelConfig
from .live_pit_observation import pit_observation_draft, validated_pit_observation
from .live_rejoin import validated_rejoin
from .live_stint import validated_stint
from .live_strategy import StrategyParameters, validated_strategy
from .llm_engineer import EngineerService
from .sdk_probe import RawSdkFrame


def _profile(coast, rate):
    """Three invented corners, matching the existing model's regression shape."""
    distance = np.arange(1201, dtype=float)
    speed, throttle, brake = np.full(1201, 50.), np.ones(1201), np.zeros(1201)
    for onset, release, apex, pickup, minimum, end in (
        (158, 218, 260, 292, 18.5, 380) if coast else (180, 240, 260, 270, 20., 350),
        (530, 590, 610, 620, 20., 700), (880, 940, 960, 970, 20., 1050),
    ):
        brake[(distance >= onset) & (distance < release)] = .72
        throttle[(distance >= onset) & (distance < pickup)] = 0
        decel, accel = ((distance >= onset) & (distance <= apex),
                        (distance > apex) & (distance <= end))
        speed[decel] = np.linspace(50., minimum, int(decel.sum()))
        speed[accel] = np.linspace(minimum, 49., int(accel.sum()))
    steering = sum(.18 * np.exp(-.5 * ((distance - apex) / 35.) ** 2)
                   for apex in (260, 610, 960))
    elapsed = np.r_[0., np.cumsum(1 / ((speed[:-1] + speed[1:]) / 2))]
    duration = math.ceil(elapsed[-1] * rate) / rate
    times = np.arange(round(duration * rate)) / rate
    scaled = times * elapsed[-1] / duration
    return np.column_stack([np.interp(scaled, elapsed, channel)
                            for channel in (distance / 1200, speed, throttle, brake, steering)])


def synthetic_frames(laps, rate=60):
    """Streaming fixture with two cached lap profiles, not a race-long matrix."""
    if (type(laps) is not int or not 8 <= laps <= 3000
            or type(rate) is not int or rate not in (20, 60)):
        raise ValueError("SYNTHETIC_RUNTIME_ARGUMENT")
    profiles = (_profile(False, rate), _profile(True, rate))
    tick = 0
    for lap in range(laps + 1):
        profile = profiles[lap % 8 in (4, 5)]
        # Let the final tail close and the ordinary 2 Hz publisher expose its
        # result. Do not fabricate a publication by reusing the final frame.
        rows = profile if lap < laps else profile[:4 * rate]
        for row in rows:
            fraction, speed, throttle, brake, steering = map(float, row)
            # A refuel discontinuity every 50 laps exercises model/epoch reset.
            fuel = 70 - .2 * (lap % 50 + fraction)
            elapsed, observed = tick / rate, tick / rate + 1
            alongside = 2 if lap % 50 == 0 and .15 < fraction < .20 else 1
            values = {
                "SessionTick": tick, "SessionTime": elapsed, "SessionNum": 0,
                "Lap": lap + 1, "LapCompleted": lap, "LapDistPct": fraction,
                "Speed": speed, "Throttle": throttle, "Brake": brake,
                "SteeringWheelAngle": steering, "FuelLevel": fuel, "FuelLevelPct": fuel / 100,
                "SessionLapsRemainEx": max(1, 500 - lap % 500), "SessionTimeRemain": 86400.,
                "IsOnTrack": True, "IsOnTrackCar": True, "IsReplayPlaying": False,
                "OnPitRoad": False, "PitstopActive": False, "PlayerCarInPitStall": False,
                "PlayerTrackSurface": 3, "PlayerCarMyIncidentCount": 0, "PlayerCarIdx": 0,
                "PlayerTireCompound": 0, "TireSetsUsed": 0, "PitsOpen": True,
                "CarLeftRight": alongside, "SessionFlags": 0, "Gear": 3, "RPM": 4000.,
                "TrackTemp": 29., "TrackTempCrew": 29., "AirTemp": 20.,
                "WindVel": 1., "WindDir": 0., "Precipitation": 0.,
                "CarIdxLapDistPct": [fraction, (fraction + .4) % 1, -1.],
                "CarIdxLap": [lap + 1, math.floor(lap + fraction + .4) + 1, 0],
                "CarIdxLapCompleted": [lap, math.floor(lap + fraction + .4), 0],
                "CarIdxTrackSurface": [3, 3, -1], "CarIdxOnPitRoad": [False, False, False],
            }
            yield RawSdkFrame(buffer_tick=tick, session_info_update=1, values=values,
                              sim_mode_raw="full", captured_monotonic_s=observed)
            tick += 1


def process_private_bytes():
    """Current Windows private commit, not peak RSS or Python-only allocations."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x800)
    psapi = ctypes.WinDLL("psapi", use_last_error=True, winmode=0x800)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise RuntimeError("SYNTHETIC_MEMORY_UNAVAILABLE")
    return int(counters.PrivateUsage)


def synthetic_pit_visit_frames():
    """Uniform invented warmup and one stopped-service visit, including wrap."""
    base = next(synthetic_frames(8, rate=20))
    for tick in range(1921):
        i = tick - 1519
        pit = 0 < i <= 400
        position = (tick / 400 if i <= 0 else
                    3.7975 + .0015 * (min(i, 100) + max(i - 300, 0)) if pit else 4.1)
        values = {**base.values, "SessionTick": tick, "SessionTime": tick / 20,
                  "Lap": math.floor(position) + 1, "LapCompleted": math.floor(position),
                  "LapDistPct": position % 1, "OnPitRoad": pit,
                  "PitstopActive": pit and 100 <= i <= 300,
                  "PlayerCarInPitStall": pit and 100 <= i <= 300,
                  "FuelLevel": 50. if i == 401 else 30.,
                  "Speed": 0. if i == 401 or pit and 100 <= i <= 300 else 30.}
        yield replace(base, buffer_tick=tick, values=values, captured_monotonic_s=1 + tick / 20)


def write_synthetic_capture(path, frames, *, complete=True, metadata=True):
    """Explicit fixture writer; never records hardware or opens an existing file."""
    from itertools import chain

    from .collector import CollectorSample, JsonlHandleWriter, LiveCollector
    from .sdk_probe import FIELD_EXPECTED_TYPES, SDK_TYPE_NAMES, VariableDescriptor
    from .telemetry import SourceKind

    frames = iter(frames)
    first = next(frames)
    descriptors, offset = [], 0
    for name, value in first.values.items():
        scalar = value[0] if type(value) is list else value
        code = min(FIELD_EXPECTED_TYPES.get(name, {0 if type(scalar) is bytes else
                                                  1 if type(scalar) is bool else
                                                  2 if type(scalar) is int else 5}))
        count = len(value) if type(value) in (list, bytes) else 1
        descriptors.append(VariableDescriptor(name, code, SDK_TYPE_NAMES[code], offset, count,
                                               False, "", "invented fixture"))
        offset += 8 * count
    info = {"WeekendInfo": {"SimMode": "full", "TrackLength": "1.2 km"},
            "SessionInfo": {"Sessions": [{"SessionNum": 0, "SessionType": "Race"}]}}
    with path.open("x+b", buffering=0) as handle, JsonlHandleWriter(handle) as writer:
        collector = LiveCollector(writer, source_id="synthetic-only", session_id="synthetic-only",
                                  expected_source_kind=SourceKind.SDK_LIVE,
                                  include_driver_info=False)
        for frame in chain((first,), frames):
            collector.ingest(CollectorSample(frame, tuple(descriptors), 20,
                info if metadata else None, "FULL" if metadata else "UNAVAILABLE"))
        if complete:
            collector.finish()


def run_synthetic_capture_replay():
    """Actual sealed file -> validator -> owner -> historical-only summary."""
    import json
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from .capture_replay import replay_capture
    from .live_worker import payload_size

    passed = False
    try:
        octets = b"SYNTHETIC CHAR FIXTURE\x80\0\0"
        chars = {"SyntheticCharScalar": b"\xff",
                 "SyntheticCharArray": [bytes([value]) for value in octets]}
        payload_size(chars)  # Queue accounting must admit immutable SDK char bytes too.
        with TemporaryDirectory(prefix="aeis-synthetic-replay-") as directory:
            path = Path(directory) / "invented.jsonl"
            write_synthetic_capture(path, (replace(frame, values={**frame.values, **chars})
                                          for frame in synthetic_pit_visit_frames()))
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    if row["record_type"] == "frame":
                        saved_chars = row["values"]
                        break
                else:
                    raise ValueError("SYNTHETIC_CHAR_FRAME_MISSING")
            result = replay_capture(path)
            facts = result["latest"]["pit"]["facts"]
            passed = (result["status"] == "RECOMPUTED" and result["frames"] == 1921
                      and result["source_kind"] == "OFFLINE_REPLAY"
                      and not result["live_acceptance"] and not result["provider_called"]
                      and saved_chars["SyntheticCharScalar"].encode("latin-1") == b"\xff"
                      and saved_chars["SyntheticCharArray"].encode("latin-1") == octets
                      and "SYNTHETIC CHAR FIXTURE" not in json.dumps(result)
                      and any(row["id"] == "pit_observation.elapsed" for row in facts))
    except Exception:
        pass
    return {"id": "SYNTHETIC_CAPTURE_RECOMPUTATION", "status": "PASS" if passed else "FAIL"}


def run_synthetic_pit_observation():
    """Real owner, local query and reviewed-draft guard; output aggregates only."""
    now = [1.]
    state = AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    state.start_spotter(20)
    analysis = _LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic-pit-visit-only",
        tick_rate=20, car_count=3, generation=state.generation, allowed=lambda: True)
    service = EngineerService(state.snapshot, clock=lambda: now[0], environ={})
    passed = False
    try:
        for frame in synthetic_pit_visit_frames():
            now[0] = frame.captured_monotonic_s
            state.feed_spotter(frame)
            analysis.process((frame, "Race", now[0], 1_200_000))
            if analysis._driving is not None and not analysis._driving._worker.wait_idle(10):
                raise RuntimeError("SYNTHETIC_COACHING_TIMEOUT")
        value = state.snapshot()
        observed = validated_pit_observation(value)
        draft = pit_observation_draft(value)
        if (observed is None or observed["status"] != "OBSERVED" or draft is None
                or observed["observation"]["pit_road_elapsed_range_s"] != [19.95, 20.05]
                or state.strategy_inputs()[0] is not None
                or service.submit("这次进站用了多久")[0] != 202):
            raise RuntimeError("SYNTHETIC_PIT_OBSERVATION")
        answer = service.snapshot()["answer"]
        if (answer["stale"] or "pit_observation.elapsed" not in answer["fact_ids"]
                or service.snapshot()["requests_used"] != 0):
            raise RuntimeError("SYNTHETIC_PIT_QUERY")
        parameters = StrategyParameters(tank_capacity_l=100., **draft["inputs"])
        state.configure_strategy(parameters, pit_draft_binding=draft["binding"])
        if state.strategy_inputs()[0] != parameters:
            raise RuntimeError("SYNTHETIC_PIT_DRAFT")
        state.invalidate_analysis(state.generation)
        if not service.snapshot()["answer"]["stale"]:
            raise RuntimeError("SYNTHETIC_PIT_WITHDRAWAL")
        passed = True
    except Exception:
        pass
    finally:
        service.close(wait=True)
        analysis.close()
        state.connection("STOPPED")
    return {"id": "SYNTHETIC_PIT_OBSERVATION_DRAFT", "status": "PASS" if passed else "FAIL"}


def run_synthetic_runtime(*, laps=8, rate=60, progress=lambda _value: None):
    """Exercise real numerical owners; emit aggregates, never invented telemetry.

    A virtual-clock lap-worker barrier prevents acceleration from masquerading
    as live overload. SDK scheduling, disk writes, UI, STT, TTS and audio routing
    are outside this measurement and must be tested separately.
    """
    if (type(laps) is not int or not 8 <= laps <= 3000
            or type(rate) is not int or rate not in (20, 60)):
        raise ValueError("SYNTHETIC_RUNTIME_ARGUMENT")
    now, started = [1.0], time.monotonic()
    state = AppState(clock=lambda: now[0])
    state.connection("CONNECTED")
    state.start_spotter(rate)
    analysis = _LiveAnalysis(state, LiveFuelConfig(), identifier="synthetic-runtime-only",
                             tick_rate=rate, car_count=3, generation=state.generation,
                             allowed=lambda: True)
    service = EngineerService(state.snapshot, clock=lambda: now[0], environ={})
    peaks, counts = Counter(), Counter()
    memory_base = memory_peak = memory_last = None
    configured, last_lap, checked_revision = False, 0, -1
    last_query_at = -math.inf
    proximity_kinds = set()
    checks, passed, phase = [], False, "STARTUP"
    try:
        if analysis._driving is None:
            raise RuntimeError("SYNTHETIC_COACHING_STARTUP")
        for frame in synthetic_frames(laps, rate):
            phase = "NUMERICAL_PIPELINE"
            now[0] = frame.captured_monotonic_s
            state.feed_spotter(frame)
            analysis.process((frame, "Race", now[0], 1_200_000))
            counts["frames"] += 1
            peaks["pending_events"] = max(
                peaks["pending_events"], len(analysis._monitor._pending_events))
            worker = analysis._driving._worker
            if worker.snapshot()["buffered_frames"] and not worker.wait_idle(10):
                raise RuntimeError("SYNTHETIC_COACHING_TIMEOUT")
            if frame.buffer_tick % (rate // 2) != 0:
                continue
            value = state.snapshot()
            if not configured:
                try:
                    state.configure_strategy(StrategyParameters(
                        200., 2., 20., 25., .9, .1, tire_change_time_s=20.,
                        fuel_tire_service_timing="PARALLEL",
                        other_service_low_s=5., other_service_high_s=7.))
                    configured = True
                except ValueError:
                    pass
            driving = value.get("driving") or {}
            fuel = value.get("fuel") or {}
            peaks["fuel_history_laps"] = max(peaks["fuel_history_laps"], fuel.get("valid_laps", 0))
            for key in ("buffered_rows", "retained_laps", "retained_trace_bytes"):
                peaks[key] = max(peaks[key], driving.get(key, 0))
            health = driving.get("worker") or {}
            if health.get("failed"):
                raise RuntimeError("SYNTHETIC_COACHING_FAILED")
            for key in ("peak_buffered_frames", "peak_buffered_bytes"):
                peaks[key] = max(peaks[key], health.get(key, 0))
            peaks["spotter_audit_rows"] = max(
                peaks["spotter_audit_rows"], len(state._spotter.audit()))
            if analysis._motion is not None:
                peaks["motion_actors"] = max(peaks["motion_actors"], len(analysis._motion.actors))
                peaks["motion_profile_points"] = max(peaks["motion_profile_points"], sum(
                    sum(len(profile["elapsed_us"]) for profile in actor.profiles)
                    + len(actor.active or []) for actor in analysis._motion.actors.values()))
            for row in state._spotter.audit():
                if row["decision"] == "CANDIDATE":
                    proximity_kinds.add(row["kind"])
            query = None
            if (driving.get("revision") != checked_revision and driving.get("status") == "READY"
                    and now[0] - last_query_at >= 1.01):
                if validated_driving(value) is None:
                    raise RuntimeError("SYNTHETIC_COACHING_PROJECTION")
                checked_revision = driving["revision"]
                counts["coaching_results"] += 1
                query = ("哪里可以改进", "driving.practice")
            if fuel.get("status") == "READY":
                counts["fuel_ready_publications"] += 1
            strategy = validated_strategy(value)
            if strategy is not None and strategy["status"] == "READY":
                counts["strategy_ready_publications"] += 1
                if query is None and not counts["strategy.window"]:
                    query = ("比较进站方案", "strategy.window")
                elif (query is None and strategy["tire_service"] == "CONDITIONAL_SERVICE_TIME_ONLY"
                      and not counts["strategy.service_brief"]):
                    query = ("换胎会多花多久", "strategy.service_brief")
            if query is None and fuel.get("status") == "READY" and not counts["fuel.current"]:
                query = ("还有多少油", "fuel.current")
            stint = validated_stint(value)
            if query is None and stint is not None:
                if not counts["stint.observed"]:
                    query = ("这一段跑了多久", "stint.observed")
                elif stint["tire_observation"] is not None and not counts["tire.observed_context"]:
                    query = ("轮胎怎么样", "tire.observed_context")
            if query is None and validated_pace(value) is not None and not counts["tire.pace"]:
                query = ("配速变化", "tire.pace")
            rejoin = validated_rejoin(value)
            if rejoin is not None and rejoin["status"] == "READY":
                counts["rejoin_ready_publications"] += 1
                if query is None and not counts["rejoin.brief"]:
                    query = ("出站预测", "rejoin.brief")
            if query is not None and now[0] - last_query_at >= 1.01:
                # Ordinary local rate guards and fresh publications, not a
                # forced snapshot or query-clock jump. No audio is requested.
                phase = "LOCAL_QUERY"
                question, fact = query
                if service.submit(question)[0] != 202:
                    raise RuntimeError("SYNTHETIC_LOCAL_QUERY")
                answer = service.snapshot()["answer"]
                if answer["stale"] or fact not in answer["fact_ids"]:
                    raise RuntimeError("SYNTHETIC_LOCAL_QUERY")
                counts[fact] += 1
                last_query_at = now[0]
            lap = frame.values["LapCompleted"]
            if lap != last_lap:
                last_lap = lap
                memory_last = process_private_bytes()
                # Keep only scalars; do not introduce history in the profiler.
                if lap >= 24 and memory_last is not None:
                    memory_base = memory_last if memory_base is None else memory_base
                    memory_peak = max(memory_peak or memory_last, memory_last)
                if lap % 20 == 0:
                    progress({"source_kind": "SYNTHETIC", "completed_laps": lap,
                              "frames": counts["frames"], "simulated_seconds": now[0] - 1,
                              "private_bytes": memory_last, "live_acceptance": False})
        value = state.snapshot()
        phase = "REQUIRED_FEATURES_REACHED"
        if not (configured and counts["coaching_results"] and counts["fuel_ready_publications"]
                and counts["strategy_ready_publications"]
                and {"CAR_LEFT", "ALL_CLEAR"} <= proximity_kinds
                and all(counts[key] for key in ("fuel.current", "strategy.window",
                                                "driving.practice", "stint.observed",
                                                "tire.observed_context", "tire.pace",
                                                "rejoin.brief", "strategy.service_brief"))):
            raise RuntimeError("SYNTHETIC_LOOPS_NOT_REACHED")
        phase = "RETAINED_STATE_BOUNDS"
        if not (peaks["retained_laps"] <= MAX_LAPS and peaks["buffered_rows"] <= MAX_ROWS * 2
                and peaks["peak_buffered_frames"] <= 2
                and peaks["peak_buffered_bytes"] <= MAX_JOB_BYTES * 2
                and peaks["fuel_history_laps"] <= 50 and peaks["spotter_audit_rows"] <= 128
                and peaks["motion_actors"] <= 256
                and peaks["motion_profile_points"] <= 256 * (2 * 65 + 64)):
            raise RuntimeError("SYNTHETIC_RETAINED_STATE_LIMIT")
        if service.snapshot()["requests_used"]:
            raise RuntimeError("SYNTHETIC_PROVIDER_REQUEST")
        passed = True
        phase = "COMPLETE"
        checks = [{"id": name, "status": "PASS"} for name in (
            "SYNTHETIC_PROXIMITY_TRANSITIONS", "SYNTHETIC_LEARNED_FUEL_QUERY",
            "SYNTHETIC_REPEATED_CORNER_QUERY", "SYNTHETIC_FUEL_STOP_QUERY",
            "SYNTHETIC_STINT_OBSERVATION_QUERY", "SYNTHETIC_RAW_PACE_QUERY",
            "SYNTHETIC_MAPPED_REJOIN_QUERY",
            "SYNTHETIC_SERVICE_COST_QUERY",
            "SYNTHETIC_RETAINED_STATE_BOUNDS")]
    except Exception:
        checks.append({"id": "SYNTHETIC_NUMERICAL_RUNTIME", "status": "FAIL"})
    finally:
        service.close(wait=True)
        analysis.close()
        state.connection("STOPPED")
    memory_last = process_private_bytes()
    return {
        "contract_version": "synthetic-runtime-check-v1", "status": "PASS" if passed else "FAIL",
        "phase": phase,
        "source_kind": "SYNTHETIC", "live_acceptance": False, "sdk_accessed": False,
        "provider_called": False, "audio_io": False, "raw_capture_written": False,
        "virtual_time_paced": True, "requested_laps": laps, "tick_rate_hz": rate,
        "simulated_seconds": round(now[0] - 1, 3),
        "wall_seconds": round(time.monotonic() - started, 3),
        "counts": dict(counts), "peak_retained_state": dict(peaks),
        "proximity_kinds": sorted(proximity_kinds),
        "state_sample_period_s": 0.5, "memory_warmup_laps": 24,
        "private_bytes_at_warmup": memory_base, "private_bytes_peak_after_warmup": memory_peak,
        "private_bytes_final": memory_last,
        "private_growth_bytes": None if memory_base is None else memory_peak - memory_base,
        "checks": checks,
    }
