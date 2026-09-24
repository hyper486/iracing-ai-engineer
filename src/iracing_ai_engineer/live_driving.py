"""Bounded, incremental recent-lap coaching; no SDK, audio or simulator control.

The existing analysis owner collects compact numeric rows. A separate bounded
worker segments complete laps, resamples them once, and reuses the offline
descriptive model. No raw sample history, sealed receipt or causal claim is made.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
import threading
from array import array
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace

import numpy as np

from .condition_cohort import ConditionCohortConfig, _traffic_sample
from .driving import (
    DrivingAnalysisConfig,
    DrivingDataError,
    _analyze_resampled_laps,
    resample_clean_laps,
)
from .laps import CLEAN_DRIVING_MAX_GAP_S, CLEAN_DRIVING_MIN_TICK_COVERAGE, segment_laps
from .live_fuel import _UNSUITABLE_FLAGS
from .live_worker import FrameWorker
from .runtime_clock import monotonic_now
from .sdk_probe import OPPONENT_ARRAY_FIELDS, classify_context
from .telemetry import Presence, Provenance, QualityStatus, SourceKind

LIVE_DRIVING_CONTRACT = "live-recent-driving-v1"
MAX_ROWS = 120_000
MAX_LAPS = 12
MAX_LAP_SPAN = 6
TAIL_SECONDS = 0.3
MODEL_CONFIG = DrivingAnalysisConfig(grid_step_m=2.0)
CONDITIONS = ConditionCohortConfig()
COLUMNS = (
    "SessionTime", "SessionTick", "Lap", "LapCompleted", "LapDistPct", "Speed",
    "Throttle", "Brake", "SteeringWheelAngle", "OnPitRoad", "PlayerTrackSurface",
    "PlayerCarMyIncidentCount", "FuelLevel", "FuelLevelPct", "PlayerTireCompound",
    "TireSetsUsed", "TrackTempCrew", "AirTemp", "WindVel", "WindDir", "Precipitation",
    "CarLeftRight", "SessionFlags", "PlayerCarInPitStall", "PitstopActive", "SeparationM",
)
MAX_JOB_BYTES = MAX_ROWS * len(COLUMNS) * 8
_REQUIRED_READS = (set(COLUMNS) - {"SeparationM"}) | set(OPPONENT_ARRAY_FIELDS) | {
    "SessionNum", "PlayerCarIdx", "IsOnTrack", "IsOnTrackCar",
}


def _value(field, *, direct=True):
    if field.presence is not Presence.PRESENT:
        return None
    if direct and field.provenance is not Provenance.SDK_DIRECT:
        return None
    return field.value


def _row(sample, frame, length):
    groups = {
        "SessionTime": sample.session.session_time_s, "SessionTick": sample.session.session_tick,
        "Lap": sample.lap.lap_number, "LapCompleted": sample.lap.laps_completed,
        "LapDistPct": sample.lap.lap_distance_pct, "Speed": sample.lap.speed_mps,
        "Throttle": sample.controls.throttle, "Brake": sample.controls.brake,
        "SteeringWheelAngle": sample.controls.steering_angle_rad,
        "OnPitRoad": sample.pit.on_pit_road,
        "PlayerTrackSurface": sample.flags.player_track_surface,
        "PlayerCarMyIncidentCount": sample.incidents.player_car_my_incident_count,
        "FuelLevel": sample.fuel.level_l, "FuelLevelPct": sample.fuel.level_pct,
        "PlayerTireCompound": sample.tires.player_tire_compound,
        "TireSetsUsed": sample.tires.tire_sets_used,
        "TrackTempCrew": sample.environment.track_temp_crew_c,
        "AirTemp": sample.environment.air_temp_c, "WindVel": sample.environment.wind_velocity_mps,
        "WindDir": sample.environment.wind_direction_rad,
        "Precipitation": sample.environment.precipitation_fraction,
        "SessionFlags": sample.flags.session_flags, "PlayerCarInPitStall": sample.pit.in_pit_stall,
        "PitstopActive": sample.pit.pitstop_active,
    }
    values = {name: _value(field) for name, field in groups.items()}
    alongside = frame.values.get("CarLeftRight")
    if type(alongside) is not int or not 1 <= alongside <= 6:
        return None
    values["CarLeftRight"] = alongside
    if any(type(value) not in (int, float, bool) or not math.isfinite(value)
           or abs(value) > 2**53 for value in values.values()):
        return None
    if (not 0 <= values["LapDistPct"] <= 1 or not 0 <= values["FuelLevelPct"] <= 1
            or not 0 <= values["FuelLevel"] <= 1000 or values["SessionTime"] < 0):
        return None
    traffic = _traffic_sample(sample, track_length_mm=length)
    if traffic["availability"] != "AVAILABLE":
        return None
    separation = traffic["min_longitudinal_separation_mm"]
    # -1 is an explicit no-locatable-opponent sentinel, not a zero/clear claim.
    values["SeparationM"] = -1 if separation is None else separation / 1000
    return tuple(float(values[name]) for name in COLUMNS)


def _empty(reason="WAIT_COMPLETE_LAP"):
    return {
        "status": "WAIT_LAPS", "reason": reason, "completed_laps": None,
        "eligible_laps": 0, "retained_laps": 0, "retained_trace_bytes": 0,
        "reference_lap": None, "point": None,
    }


@dataclass(frozen=True)
class _LapJob:
    epoch: int
    sequence: int
    track_length_mm: int
    completed_laps: int
    rows: bytes


def _conditions(channels, lap):
    window = {key: value[lap.start_frame:lap.end_frame_exclusive]
              for key, value in channels.items()}
    if np.any(window["Speed"] < 1):
        return None, "STOPPED_OR_REVERSE_LAP"
    if (np.any(window["SessionFlags"].astype(np.int64) & (_UNSUITABLE_FLAGS | 1 | 0x010000))
            or np.any(window["PlayerCarInPitStall"]) or np.any(window["PitstopActive"])):
        return None, "FLAGS_OR_PIT_INTERVAL"
    separation = window["SeparationM"]
    if (np.any(window["CarLeftRight"] != 1)
            or np.any((separation >= 0) & (separation < CONDITIONS.traffic_clearance_mm / 1000))):
        return None, "TRAFFIC_AFFECTED_LAP"
    if (np.any(window["Precipitation"] > CONDITIONS.dry_precipitation_max_ppm / 1_000_000)
            or np.ptp(window["TrackTempCrew"]) > 2 or np.ptp(window["AirTemp"]) > 2):
        return None, "WEATHER_CHANGED_OR_WET"
    if any(len(np.unique(window[key])) != 1 for key in ("PlayerTireCompound", "TireSetsUsed")):
        return None, "TIRE_CONTEXT_CHANGED"
    if (np.any(window["FuelLevel"] - np.minimum.accumulate(window["FuelLevel"]) > 0.05)
            or np.any(window["FuelLevelPct"] - np.minimum.accumulate(window["FuelLevelPct"])
                      > CONDITIONS.fuel_refuel_jump_tolerance_ppm / 1_000_000)):
        return None, "REFUEL_OBSERVED"
    wind_x = window["WindVel"] * np.cos(window["WindDir"])
    wind_y = window["WindVel"] * np.sin(window["WindDir"])
    if math.hypot(float(np.ptp(wind_x)), float(np.ptp(wind_y))) > 2:
        return None, "WEATHER_CHANGED_OR_WET"
    return {
        "fuel_start": float(window["FuelLevelPct"][0]),
        "compound": int(window["PlayerTireCompound"][0]), "sets": int(window["TireSetsUsed"][0]),
        "track_min": float(np.min(window["TrackTempCrew"])),
        "track_max": float(np.max(window["TrackTempCrew"])),
        "air_min": float(np.min(window["AirTemp"])), "air_max": float(np.max(window["AirTemp"])),
        "wind_x": float(np.mean(wind_x)), "wind_y": float(np.mean(wind_y)),
    }, None


def _comparable(left, right):
    return (
        left["compound"] == right["compound"] and left["sets"] == right["sets"]
        and abs(left["fuel_start"] - right["fuel_start"])
        <= CONDITIONS.fuel_start_tolerance_ppm / 1_000_000
        and max(left["track_max"], right["track_max"]) - min(left["track_min"], right["track_min"])
        <= CONDITIONS.track_temp_tolerance_millic / 1000
        and max(left["air_max"], right["air_max"]) - min(left["air_min"], right["air_min"])
        <= CONDITIONS.air_temp_tolerance_millic / 1000
        and math.hypot(left["wind_x"] - right["wind_x"], left["wind_y"] - right["wind_y"])
        <= CONDITIONS.wind_vector_tolerance_mmps / 1000
    )


class _CornerModel:
    byte_count = 0

    def __init__(self, owner, tick_rate):
        self.owner, self.tick_rate = owner, tick_rate
        self.epoch = None
        self.last_completed = None
        self.laps = deque(maxlen=MAX_LAPS)

    def process(self, job):
        if not self.owner.current(job.epoch):
            return
        if job.epoch != self.epoch:
            self.epoch = job.epoch
            self.laps.clear()
            self.last_completed = None
        result = _empty()
        result["completed_laps"] = job.completed_laps
        matrix = np.frombuffer(job.rows, dtype=np.float64).reshape(-1, len(COLUMNS))
        channels = {key: matrix[:, index] for index, key in enumerate(COLUMNS)}
        complete = [lap for lap in segment_laps(channels, self.tick_rate)
                    if lap.structurally_complete]
        reason = "INCOMPLETE_OR_UNCLEAN_LAP"
        condition = None
        if (job.completed_laps < 1 or (self.last_completed is not None
                                      and job.completed_laps <= self.last_completed)):
            self.laps.clear()
            reason = "LAP_COUNTER_NOT_ADVANCING"
        elif len(complete) == 1 and complete[0].tick_coverage < CLEAN_DRIVING_MIN_TICK_COVERAGE:
            reason = "LAP_TICK_COVERAGE_LOW"
        elif len(complete) == 1 and complete[0].max_gap_s > CLEAN_DRIVING_MAX_GAP_S:
            reason = "LAP_SAMPLING_GAP"
        elif len(complete) == 1 and complete[0].clean_for_driving:
            condition, reason = _conditions(channels, complete[0])
        self.last_completed = job.completed_laps
        if condition is not None:
            observation = replace(complete[0], ordinal=job.completed_laps)
            try:
                resampled = resample_clean_laps(channels, [observation],
                                               track_length_m=job.track_length_mm / 1000,
                                               config=MODEL_CONFIG)[0]
            except DrivingDataError:
                condition, reason = None, "UNRESAMPLEABLE_LAP"
        if condition is not None:
            digest = hashlib.sha256(job.rows).hexdigest()
            self.laps.append((resampled, condition, digest))
            selected = [entry for entry in self.laps
                        if 0 <= job.completed_laps - entry[0].lap_ordinal <= MAX_LAP_SPAN
                        and _comparable(condition, entry[1])]
            # Every pair, not just the newest anchor, must meet the declared
            # observed-condition bounds. Drop oldest until the cohort agrees.
            while selected and any(not _comparable(a[1], b[1]) for a in selected for b in selected):
                selected.pop(0)
            result["eligible_laps"] = len(selected)
            reason = "NEED_COMPARABLE_LAPS"
            if len(selected) >= MODEL_CONFIG.min_clean_laps:
                analysis = _analyze_resampled_laps(tuple(row[0] for row in selected),
                                                   track_length_m=job.track_length_mm / 1000,
                                                   config=MODEL_CONFIG, max_corners=128)
                reason = "NO_REPRODUCIBLE_REFERENCE"
                if analysis.status == "READY":
                    result["reference_lap"] = analysis.reference.lap_ordinal
                    points = [point for point in analysis.diagnoses
                              if job.completed_laps in point.evidence_lap_ordinals]
                    result.update(status="NO_REPEAT", reason="NO_REPEATED_PATTERN_IN_LATEST_LAP")
                    reason = None
                    if points:
                        point = points[0]
                        corner = next(item for item in analysis.corners
                                      if item.corner_id == point.corner_id)
                        result.update(status="READY", reason="DESCRIPTIVE_PRACTICE_OPPORTUNITY",
                                      point={
                            "corner_id": point.corner_id, "diagnosis": point.diagnosis,
                            "approach_m": round(corner.approach_start_m),
                            "braking_zone_m": round(corner.brake_start_m),
                            "loss_s": round(point.estimated_loss_median_s, 3),
                            "evidence_laps": list(point.evidence_lap_ordinals),
                            "counterexample_laps": list(point.counterexample_lap_ordinals),
                            "evidence_sha256": hashlib.sha256("".join(
                                row[2] for row in selected).encode("ascii")).hexdigest(),
                        })
        else:
            result["status"] = "LAP_REJECTED"
        if reason is not None:
            result["reason"] = reason
        result["retained_laps"] = len(self.laps)
        result["retained_trace_bytes"] = sum(
            sum(getattr(row[0], field).nbytes for field in (
                "distance_m", "elapsed_time_s", "speed_mps", "throttle", "brake", "steering_rad"))
            for row in self.laps)
        self.owner.complete(job, result)

    def finish(self):
        pass

    def close(self):
        self.laps.clear()


class LiveDrivingEngineer:
    """Single-owner feed/snapshot, one bounded asynchronous lap-model worker."""

    def __init__(self, tick_rate_hz, *, clock=monotonic_now, worker_factory=FrameWorker,
                 max_rows=MAX_ROWS):
        if type(tick_rate_hz) is not int or not 1 <= tick_rate_hz <= 360:
            raise ValueError("COACHING_TICK_RATE_INVALID")
        if type(max_rows) is not int or not 100 <= max_rows <= MAX_ROWS:
            raise ValueError("COACHING_ROW_LIMIT_INVALID")
        self.clock, self.max_rows = clock, max_rows
        self._tick_rate = tick_rate_hz
        self._lock = threading.Lock()
        self._epoch = self._revision = self._sequence = 0
        self._result = _empty()
        self._failed = False
        self._identity = self._previous = self._buffer = self._pending = None
        self._history = deque(maxlen=math.ceil(TAIL_SECONDS * tick_rate_hz) + 2)
        self._worker = worker_factory(lambda: _CornerModel(self, tick_rate_hz),
                                      name="corner-model", on_failure=self.fail,
                                      max_frames=2, max_bytes=MAX_JOB_BYTES * 2,
                                      max_age_s=30.0, clock=clock)

    def current(self, epoch):
        with self._lock:
            return not self._failed and epoch == self._epoch

    def complete(self, job, result):
        with self._lock:
            if not self._failed and job.epoch == self._epoch:
                self._result = result
                self._revision += 1

    def fail(self):
        with self._lock:
            if self._failed:
                return
            self._failed = True
            self._result = _empty("COACHING_PROCESSING_ERROR")
            self._result["status"] = "ERROR"
            self._epoch += 1
            self._revision += 1

    def reset(self, reason):
        with self._lock:
            if not self._failed and (self._identity is not None or self._result["reason"] != reason
                    or self._result["point"] is not None):
                self._epoch += 1
                self._revision += 1
            if not self._failed:
                self._result = _empty(reason)
        self._identity = self._previous = self._buffer = self._pending = None
        self._history.clear()

    def feed(self, frame, sample, track_length_mm):
        if self._failed:
            self.reset("COACHING_PROCESSING_ERROR")
            return
        context = classify_context(frame.sim_mode_raw, frame.values)
        if (context["sim_source_mode"] != "FULL"
                or context["player_control_state"] != "IN_CAR_PHYSICS" or context["conflicts"]
                or _value(sample.source.source_kind, direct=False) is not SourceKind.SDK_LIVE):
            self.reset("SOURCE_NOT_READY")
            return
        dropped = _value(sample.quality.dropped_ticks, direct=False)
        if (type(track_length_mm) is not int or not 100_000 < track_length_mm <= 100_000_000
                or set(frame.read_errors) & _REQUIRED_READS
                or _value(sample.quality.status, direct=False) is QualityStatus.REJECTED
                or _value(sample.quality.stale, direct=False) is not False
                or type(dropped) is not int or dropped < 0):
            self.reset("DATA_OR_GEOMETRY_UNAVAILABLE")
            return
        if (dropped + 1) / self._tick_rate > CLEAN_DRIVING_MAX_GAP_S:
            self.reset("LAP_SAMPLING_GAP")
            return
        row = _row(sample, frame, track_length_mm)
        player = _value(sample.opponents.player_car_idx)
        session = _value(sample.session.session_num)
        if row is None or type(player) is not int or type(session) is not int:
            self.reset("REQUIRED_DATA_UNAVAILABLE")
            return
        if row[9] or row[10] != 3 or row[23] or row[24]:
            self.reset("PIT_OR_OFF_TRACK_INTERVAL")
            return
        identity = (session, player, track_length_mm, row[14], row[15])
        if self._identity != identity:
            self.reset("WAIT_COMPLETE_LAP")
            self._identity = identity
        previous = self._previous
        # Keep actual sparse rows, never synthesize missing frames. The model
        # admits a completed lap only through the unchanged offline >=99.9%
        # coverage gate. An isolated small gap must not erase every prior lap;
        # a larger tick OR simulation-time gap still resets the whole epoch.
        if previous is not None and (
            row[0] - previous[0] > CLEAN_DRIVING_MAX_GAP_S
            or (row[1] - previous[1]) / self._tick_rate > CLEAN_DRIVING_MAX_GAP_S
        ):
            self.reset("LAP_SAMPLING_GAP")
            self._identity = identity
            previous = None
        if previous is not None and (
            row[0] <= previous[0] or row[1] <= previous[1]
            or row[11] != previous[11] or row[3] < previous[3] or row[3] > previous[3] + 1
            or row[12] > previous[12] + 0.05
            or row[13] > previous[13] + CONDITIONS.fuel_refuel_jump_tolerance_ppm / 1_000_000
        ):
            self.reset("CONTINUITY_OR_STINT_CHANGED")
            self._identity = identity
            previous = None
        if self._pending is not None:
            data, end_time = self._pending
            data.extend(row)
            if len(data) // len(COLUMNS) > self.max_rows:
                self.reset("LAP_BUFFER_LIMIT")
                return
            if row[0] >= end_time + TAIL_SECONDS:
                self._sequence += 1
                job = _LapJob(self._epoch, self._sequence, track_length_mm, int(row[3]),
                              data.tobytes())
                self._pending = None
                self._worker.submit(job, size=len(job.rows) + 1024, observed_at=self.clock())
        crossing = previous is not None and previous[4] >= .97 and row[4] <= .03
        if crossing:
            if self._pending is not None:
                self.reset("AMBIGUOUS_LAP_BOUNDARY")
                return
            if self._buffer is not None:
                self._buffer.extend(row)
                self._pending = self._buffer, row[0]
            self._buffer = array("d", (value for point in self._history for value in point))
            self._buffer.extend(row)
        elif self._buffer is not None:
            self._buffer.extend(row)
        if self._buffer is not None and len(self._buffer) // len(COLUMNS) > self.max_rows:
            self.reset("LAP_BUFFER_LIMIT")
            return
        self._history.append(row)
        self._previous = row

    def snapshot(self, monitor):
        worker = self._worker.snapshot()
        with self._lock:
            result = copy.deepcopy(self._result)
            epoch, revision = self._epoch, self._revision
        return {
            "contract_version": LIVE_DRIVING_CONTRACT, "advisor_only": True, "executable": False,
            "source_kind": monitor.get("source_kind"),
            "binding_sha256": monitor.get("binding_sha256"),
            "epoch": epoch, "revision": revision, "track_length_mm": (
                self._identity[2] if self._identity is not None else None),
            "session_num": (monitor.get("telemetry") or {}).get("session_num"),
            "player_car_idx": (monitor.get("telemetry") or {}).get("player_car_idx"),
            "claim_level": "descriptive", "causal_gain_verified": False, "live_acceptance": False,
            "grid_step_m": MODEL_CONFIG.grid_step_m, "max_history_laps": MAX_LAPS,
            "max_rows_per_lap": self.max_rows,
            "minimum_comparable_laps": MODEL_CONFIG.min_clean_laps,
            "buffered_rows": ((len(self._buffer) if self._buffer is not None else 0)
                              + (len(self._pending[0]) if self._pending is not None else 0))
            // len(COLUMNS),
            "worker": worker, **result,
        }

    def close(self):
        self.reset("SOURCE_ENDED")
        self._worker.close()
        self._worker.join()


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _integer(value, low=0, high=2**53):
    return type(value) is int and low <= value <= high


def _digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validated_driving(snapshot):
    """Validate the bounded local projection, not the authenticity of telemetry.

    The caller must also enforce the current application's source freshness.
    A result describes only the latest completed lap; no older recommendation
    survives the next completed-lap counter or an invalidated collector epoch.
    """
    value = _mapping(snapshot.get("driving"))
    monitor = _mapping(snapshot.get("monitor"))
    telemetry = _mapping(monitor.get("telemetry"))
    worker = _mapping(value.get("worker"))
    if not (
        value.get("contract_version") == LIVE_DRIVING_CONTRACT
        and value.get("advisor_only") is True and value.get("executable") is False
        and value.get("source_kind") == monitor.get("source_kind") == "SDK_LIVE"
        and value.get("claim_level") == "descriptive"
        and value.get("causal_gain_verified") is False and value.get("live_acceptance") is False
        and _digest(value.get("binding_sha256"))
        and value.get("binding_sha256") == monitor.get("binding_sha256")
        and all(_integer(value.get(key)) for key in ("epoch", "revision"))
        and _integer(value.get("track_length_mm"), 100_001, 100_000_000)
        and all(_integer(value.get(key)) and value[key] == telemetry.get(key)
                and type(telemetry.get(key)) is int for key in ("session_num", "player_car_idx"))
        and worker.get("failed") is False and worker.get("status") == "RUNNING"
        and _integer(value.get("retained_laps"), 0, MAX_LAPS)
        and _integer(value.get("eligible_laps"), 0, value["retained_laps"])
        and value.get("status") in ("WAIT_LAPS", "LAP_REJECTED", "NO_REPEAT", "READY")
    ):
        return None
    completed = value.get("completed_laps")
    if completed is None:
        return value if (value["status"] == "WAIT_LAPS" and value.get("point") is None
                         and value["eligible_laps"] == 0) else None
    if not (_integer(completed, 1) and type(telemetry.get("laps_completed")) is int
            and completed == telemetry["laps_completed"]):
        return None
    if value["status"] != "READY":
        return value if value.get("point") is None else None
    point = _mapping(value.get("point"))
    reference, evidence, contrary = (value.get("reference_lap"), point.get("evidence_laps"),
                                     point.get("counterexample_laps"))
    if not (
        value["eligible_laps"] >= MODEL_CONFIG.min_clean_laps
        and _integer(reference, max(1, completed - MAX_LAP_SPAN), completed)
        and point.get("diagnosis") in (
            "LONG_COAST", "LATE_BRAKING_HURTS_EXIT", "THROTTLE_SECOND_LIFT")
        and type(point.get("corner_id")) is str
        and re.fullmatch(r"C(?:0[1-9]|[1-9][0-9]|1[01][0-9]|12[0-8])", point["corner_id"])
        and all(type(point.get(key)) in (int, float)
                and 0 <= point[key] <= value["track_length_mm"] / 1000
                for key in ("approach_m", "braking_zone_m"))
        and point["approach_m"] <= point["braking_zone_m"]
        and type(point.get("loss_s")) in (int, float) and 0 < point["loss_s"] <= 10_000
        and _digest(point.get("evidence_sha256"))
        and all(type(items) is list and len(items) <= value["eligible_laps"]
                and all(_integer(item, max(1, completed - MAX_LAP_SPAN), completed)
                        for item in items)
                and len(set(items)) == len(items) for items in (evidence, contrary))
        and len(evidence) >= MODEL_CONFIG.min_evidence_laps and completed in evidence
        and reference not in evidence and not set(evidence) & set(contrary)
    ):
        return None
    return value


def coaching_binding(snapshot):
    """Do not cancel a coaching answer on unrelated fuel/traffic publications."""
    value = _mapping(snapshot.get("driving"))
    point = _mapping(value.get("point"))
    return (value.get("epoch"), value.get("revision"), value.get("status"),
            value.get("completed_laps"), point.get("evidence_sha256"))


def driving_notice(snapshot):
    """Fixed health explanations; never render input/free-text error messages."""
    value = _mapping(snapshot.get("driving"))
    if value.get("status") == "ERROR":
        return "驾驶分析故障，重连后重试；近车与燃油模块独立运行。"
    reason = value.get("reason")
    if type(reason) is not str:
        return None
    return {
        "SOURCE_NOT_READY": "驾驶分析等待新鲜的本人车内驾驶数据。",
        "SOURCE_STALE": "驾驶遥测已过期，已撤回旧圈结论。",
        "DATA_OR_GEOMETRY_UNAVAILABLE": "驾驶分析缺少连续遥测或匹配赛道长度，暂不收圈。",
        "REQUIRED_DATA_UNAVAILABLE": "驾驶分析缺少必需的踏板、轮胎、天气或交通数据，暂不收圈。",
        "PIT_OR_OFF_TRACK_INTERVAL": "进站或离开赛道后需重新积累完整可比圈。",
        "CONTINUITY_OR_STINT_CHANGED": "驾驶数据或连续段已变化，需重新积累完整可比圈。",
        "LAP_BUFFER_LIMIT": "单圈采集达到内存上限，已丢弃该圈，不据此作驾驶建议。",
        "LAP_COUNTER_NOT_ADVANCING": "圈数未正常递增，已撤回旧圈结论。",
        "LAP_TICK_COVERAGE_LOW": "本圈遥测覆盖率不足 99.9%，不用于驾驶建议；请检查采集负载。",
        "LAP_SAMPLING_GAP": "驾驶遥测间隔超过 0.1 秒，需重新积累完整可比圈。",
    }.get(reason)
