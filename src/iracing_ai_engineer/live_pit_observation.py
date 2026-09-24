"""One edge-supported pit visit and a historical phase-baseline comparison.

SDK pit-road transitions are not surveyed merge geometry. Elapsed road time is
an observation; counterfactual net loss is an estimate, not calibrated future
service cost. No service contents or delivered-fuel claim is inferred.
"""

from __future__ import annotations

import copy
import math
from itertools import product

from .live_motion import _progress, _Trace, profile_bounds
from .live_stint import _int, _value
from .live_strategy import _map, _number, _same_typed_tree, source_ready, source_scope
from .rejoin_projection import phase_time
from .sdk_probe import classify_context
from .telemetry import QualityStatus, SourceKind

CONTRACT = "live-pit-observation-v1"
_CORE = {"SessionTick", "SessionTime", "SessionNum", "PlayerCarIdx", "LapCompleted",
         "LapDistPct", "OnPitRoad", "PlayerTrackSurface", "SessionFlags",
         "PlayerCarMyIncidentCount", "IsOnTrack", "IsOnTrackCar", "IsReplayPlaying"}
_FLAGS = 0x00330000 | 0x0008 | 0x0010 | 0x0100 | 0xC000


def unavailable_pit_observation(monitor, reason="SOURCE_NOT_READY", *, revision=0):
    telemetry = _map(monitor.get("telemetry"))
    return {"contract_version": CONTRACT, "advisor_only": True, "executable": False,
            "live_acceptance": False, "calibrated": False,
            "source_kind": monitor.get("source_kind"),
            "binding_sha256": monitor.get("binding_sha256"),
            "monitor_sequence": monitor.get("sequence"),
            "session_time_us": monitor.get("session_time_us"), "revision": revision,
            "session_num": telemetry.get("session_num"),
            "player_car_idx": telemetry.get("player_car_idx"),
            "status": "WAIT", "reason": reason, "observation": None}


def _valid_point(point):
    return type(point) is dict and set(point) == {
        "tick", "time_us", "progress_laps", "pit", "fuel_l", "service", "stall"
    } and (
        _int(point["tick"]) and _int(point["time_us"])
        and _number(point["progress_laps"], high=1_000_001)
        and type(point["pit"]) is bool
        and (point["fuel_l"] is None or _number(point["fuel_l"]))
        and all(point[key] is None or type(point[key]) is bool for key in ("service", "stall"))
    )


def _edge(edge, start):
    if type(edge) is not list or len(edge) != 2 or not all(_valid_point(row) for row in edge):
        return False
    a, b = edge
    return (a["pit"] is not start and b["pit"] is start and a["tick"] < b["tick"]
            and 0 < b["time_us"] - a["time_us"] <= 250_000
            and 0 <= b["progress_laps"] - a["progress_laps"] <= (
                b["time_us"] - a["time_us"]) / 5_000_000 + 1e-9)


def _reference(trace, now):
    if trace is None or profile_bounds(trace.crossings, trace.profiles, now) is None:
        return None
    return {"crossing_windows_us": copy.deepcopy(trace.crossings),
            "lap_profiles": copy.deepcopy(trace.profiles),
            "last_crossing_lap": math.floor(trace.position)}


def derive_observation(record):
    """Exact numeric projection of the producer's bounded, completed visit."""
    if type(record) is not dict or set(record) != {
        "entry", "exit", "reference", "max_gap_us", "service_flags", "stall_flags"
    }:
        return None
    if (not _edge(record["entry"], True) or not _edge(record["exit"], False)
            or not _int(record["max_gap_us"], 250_000) or record["max_gap_us"] == 0
            or any(record[key] not in ("OBSERVED_ACTIVE", "NOT_OBSERVED_ACTIVE", "UNKNOWN")
                   for key in ("service_flags", "stall_flags"))):
        return None
    entry, exit_ = record["entry"], record["exit"]
    if (entry[1]["tick"] >= exit_[0]["tick"] or any(
            edge[1]["time_us"] - edge[0]["time_us"] > record["max_gap_us"]
            for edge in (entry, exit_))):
        return None
    elapsed = [(exit_[0]["time_us"] - entry[1]["time_us"]) / 1e6,
               (exit_[1]["time_us"] - entry[0]["time_us"]) / 1e6]
    distance = [exit_[0]["progress_laps"] - entry[1]["progress_laps"],
                exit_[1]["progress_laps"] - entry[0]["progress_laps"]]
    if not (0 < elapsed[0] <= elapsed[1] <= 1800 and 0 < distance[0] <= distance[1] < 1):
        return None
    reference, baseline, loss = record["reference"], None, None
    if reference is not None:
        if (type(reference) is not dict or set(reference) != {
            "crossing_windows_us", "lap_profiles", "last_crossing_lap"
        } or not _int(reference["last_crossing_lap"], 1_000_000)
                or reference["last_crossing_lap"] != math.floor(entry[0]["progress_laps"])
                or type(reference["crossing_windows_us"]) is not list
                or type(reference["lap_profiles"]) is not list
                or profile_bounds(reference["crossing_windows_us"], reference["lap_profiles"],
                                  entry[0]["time_us"]) is None):
            return None
        travel = []
        for profile, begin, end in product(reference["lap_profiles"], entry, exit_):
            seconds = (phase_time(profile, end["progress_laps"])
                       - phase_time(profile, begin["progress_laps"])) / 1e6
            error = 4 * profile["sampling_error_us"] / 1e6
            travel.extend((max(0, seconds - error), seconds + error))
        baseline = [round(min(travel), 6), round(max(travel), 6)]
        # Negative values remain negative: a doubtful baseline is not repaired
        # by clipping it into a seemingly useful positive calibration.
        loss = [round(elapsed[0] - baseline[1], 6), round(elapsed[1] - baseline[0], 6)]
    fuel = (None if entry[1]["fuel_l"] is None or exit_[1]["fuel_l"] is None else
            round(exit_[1]["fuel_l"] - entry[1]["fuel_l"], 6))
    fractions = [round(((edge[0]["progress_laps"] + edge[1]["progress_laps"]) / 2) % 1, 9)
                 for edge in (entry, exit_)]
    # A review draft requires observed service/stall activity, but those booleans
    # still cannot identify repairs, tire replacement or delivered fuel.
    review = (loss is not None and .1 <= loss[0] <= loss[1] <= 600
              and fractions[0] != fractions[1]
              and record["service_flags"] == record["stall_flags"] == "OBSERVED_ACTIVE"
              and exit_[1]["service"] is False and exit_[1]["stall"] is False)
    return {"record": copy.deepcopy(record), "pit_road_elapsed_range_s": elapsed,
            "entry_fraction": fractions[0], "exit_fraction": fractions[1],
            "baseline_track_elapsed_range_s": baseline, "net_loss_estimate_range_s": loss,
            "observed_net_tank_change_l": fuel, "review_draft_available": review,
            "geometry": "SDK_PIT_ROAD_EDGE_BRACKETS", "service_contents": "UNKNOWN",
            "baseline_method": ("TWO_PRECEDING_OBSERVED_PHASE_PROFILES"
                                if baseline else "UNAVAILABLE")}


class LivePitObservationTracker:
    """One prior point, one visit, one result and two baseline profiles; no I/O."""

    def __init__(self, tick_rate_hz):
        if not _int(tick_rate_hz, 360) or tick_rate_hz < 1:
            raise ValueError("PIT_OBSERVATION_TICK_RATE_INVALID")
        self.tick_rate = tick_rate_hz
        self.revision = 0
        self.failed = False
        self.reason = "SOURCE_NOT_READY"
        self.identity = self.previous = self.incidents = None
        self.trace = self.active = self.observation = None

    def reset(self, reason):
        reason = "PIT_OBSERVATION_PROCESSING_ERROR" if self.failed else reason
        if self.previous is not None or self.observation is not None or reason != self.reason:
            self.revision += 1
        self.reason = reason
        self.identity = self.previous = self.incidents = None
        self.trace = self.active = self.observation = None

    def fail(self):
        self.failed = True
        self.reset("PIT_OBSERVATION_PROCESSING_ERROR")

    def feed(self, frame, sample):
        if self.failed:
            return
        context = classify_context(frame.sim_mode_raw, frame.values)
        issues = _value(sample.quality.issues, direct=False) or ()
        if (context["sim_source_mode"] != "FULL"
                or context["player_control_state"] != "IN_CAR_PHYSICS" or context["conflicts"]
                or _value(sample.source.source_kind, direct=False) is not SourceKind.SDK_LIVE
                or _value(sample.quality.status, direct=False) is QualityStatus.REJECTED
                or _value(sample.quality.stale, direct=False) is not False
                or set(frame.read_errors) & _CORE
                or any(issue in {"SOURCE_BOUNDARY", "SESSION_BOUNDARY"}
                       or str(issue).startswith("CONTINUITY_BOUNDARY:") for issue in issues)):
            self.reset("SOURCE_NOT_READY")
            return
        tick, seconds, session, player, pit, flags, incidents, surface = (
            _value(sample.session.session_tick), _value(sample.session.session_time_s),
            _value(sample.session.session_num), _value(sample.opponents.player_car_idx),
            _value(sample.pit.on_pit_road), _value(sample.flags.session_flags),
            _value(sample.incidents.player_car_my_incident_count),
            _value(sample.flags.player_track_surface))
        progress = _progress(_value(sample.lap.laps_completed), _value(sample.lap.lap_distance_pct))
        if not (_int(tick) and frame.buffer_tick == tick and _number(seconds, high=10_000_000)
                and _int(session, 100_000) and _int(player, 255) and progress is not None
                and type(pit) is bool and _int(flags, 2**32 - 1) and not flags & _FLAGS
                and _int(incidents, 1_000_000) and type(surface) is int
                and (surface in (1, 2, 3) if pit else surface == 3)):
            self.reset("PIT_OBSERVATION_REQUIRED_DATA_UNAVAILABLE")
            return
        optional = {}
        for key, field, sdk in (("fuel_l", sample.fuel.level_l, "FuelLevel"),
                                ("service", sample.pit.pitstop_active, "PitstopActive"),
                                ("stall", sample.pit.in_pit_stall, "PlayerCarInPitStall")):
            value = _value(field) if sdk not in frame.read_errors else None
            optional[key] = value if (_number(value) if key == "fuel_l" else
                                      type(value) is bool) else None
        point = {"tick": tick, "time_us": round(seconds * 1e6),
                 "progress_laps": round(progress, 9), "pit": pit, **optional}
        identity = (_value(sample.source.source_id, direct=False),
                    _value(sample.session.session_id, direct=False), session, player)
        previous = self.previous
        if self.identity != identity or (previous is not None and (
            not 0 < tick - previous["tick"] <= self.tick_rate * .25
            or not 0 < point["time_us"] - previous["time_us"] <= 250_000
            or not -1e-9 <= progress - previous["progress_laps"] <= (
                point["time_us"] - previous["time_us"]) / 5_000_000 + 1e-9
            or incidents != self.incidents
        )):
            self.reset("PIT_OBSERVATION_CONTINUITY_CHANGED")
            previous = None
        self.identity, self.incidents = identity, incidents
        if previous is not None and not previous["pit"] and pit:
            self.observation = None
            self.active = {"entry": [previous, point], "reference": _reference(
                self.trace, previous["time_us"]), "max_gap_us": 0,
                "service_flags": "NOT_OBSERVED_ACTIVE", "stall_flags": "NOT_OBSERVED_ACTIVE"}
            self.revision += 1
        if self.active is not None:
            self.active["max_gap_us"] = max(self.active["max_gap_us"],
                                             point["time_us"] - previous["time_us"])
            for key in ("service", "stall"):
                state = key + "_flags"
                if point[key] is None:
                    self.active[state] = "UNKNOWN"
                elif point[key] and self.active[state] != "UNKNOWN":
                    self.active[state] = "OBSERVED_ACTIVE"
            if point["time_us"] - self.active["entry"][0]["time_us"] > 1_800_000_000:
                self.active = None
                self.reason = "PIT_OBSERVATION_TOO_LONG"
            elif not pit:
                record = {**self.active, "exit": [previous, point]}
                self.observation = derive_observation(record)
                self.active = None
                self.reason = ("PIT_VISIT_OBSERVED" if self.observation is not None
                               else "PIT_OBSERVATION_INCOMPLETE")
                self.revision += 1
        if pit:
            self.trace = None
            if self.active is not None:
                self.reason = "PIT_VISIT_IN_PROGRESS"
            elif previous is None:
                self.reason = "PIT_OBSERVATION_STARTED_INSIDE"
        elif self.trace is None:
            self.trace = _Trace(progress, point["time_us"])
        else:
            self.trace.advance(progress, point["time_us"])
        self.previous = point

    def snapshot(self, monitor):
        result = unavailable_pit_observation(monitor, self.reason, revision=self.revision)
        telemetry = _map(monitor.get("telemetry"))
        if not (source_ready(monitor) and self.previous is not None
                and self.previous["time_us"] == monitor.get("session_time_us")
                and self.identity[2:] == (telemetry.get("session_num"),
                                         telemetry.get("player_car_idx"))):
            return unavailable_pit_observation(
                monitor, "SOURCE_NOT_READY" if self.observation is not None else self.reason,
                revision=self.revision)
        return ({**result, "status": "OBSERVED", "observation": copy.deepcopy(self.observation)}
                if self.observation is not None else result)


def validated_pit_observation(snapshot):
    value, monitor = _map(snapshot.get("pit_observation")), _map(snapshot.get("monitor"))
    if (not source_ready(monitor) or source_scope(monitor, 0) is None
            or not _int(value.get("revision"))):
        return None
    reason = value.get("reason")
    if type(reason) is not str or reason not in _NOTICES:
        return None
    expected = unavailable_pit_observation(monitor, reason, revision=value["revision"])
    if value.get("status") == "OBSERVED":
        observation = _map(value.get("observation"))
        derived = derive_observation(observation.get("record"))
        if (derived is None or reason != "PIT_VISIT_OBSERVED"
                or derived["record"]["exit"][1]["time_us"] > monitor["session_time_us"]):
            return None
        expected.update(status="OBSERVED", observation=derived)
    return value if _same_typed_tree(value, expected) else None


def pit_observation_binding(snapshot):
    value = _map(snapshot.get("pit_observation"))
    return (snapshot.get("pit_observation_revision"), value.get("revision"),
            value.get("status"), value.get("reason"))


_NOTICES = {
    "SOURCE_NOT_READY": "进站观测等待新鲜的本人车内数据。",
    "PIT_OBSERVATION_REQUIRED_DATA_UNAVAILABLE": "进站观测缺少必要信号，或旗号与赛道状态不支持。",
    "PIT_OBSERVATION_CONTINUITY_CHANGED": "进站观测已重置，需连续记录一次完整进出站。",
    "PIT_OBSERVATION_PROCESSING_ERROR": "进站观测故障；燃油与近车播报独立运行，请重连。",
    "PIT_OBSERVATION_TOO_LONG": "本次进站超出观测时限，未生成损失估计。",
    "PIT_OBSERVATION_STARTED_INSIDE": "中途接入站内，缺少进入边界，不能计算完整进站耗时。",
    "PIT_VISIT_IN_PROGRESS": "正在观测进站过程，出站后再比较耗时。",
    "PIT_OBSERVATION_INCOMPLETE": "进出站边界不完整或位置异常，未生成损失估计。",
    "SOURCE_STALE": "遥测已过期，进站观测已撤回。",
    "PIT_VISIT_OBSERVED": "已记录一次进站；历史基线估计不是未来服务标定。",
}


def pit_observation_notice(snapshot):
    value = validated_pit_observation(snapshot)
    return _NOTICES[value["reason"]] if value is not None else _NOTICES["SOURCE_NOT_READY"]


def pit_observation_draft(snapshot):
    """Only populate editable fields while stopped; never apply strategy state."""
    value = validated_pit_observation(snapshot)
    telemetry = _map(_map(snapshot.get("monitor")).get("telemetry"))
    monitor = _map(snapshot.get("monitor"))
    if not (value is not None and value["status"] == "OBSERVED"
            and snapshot.get("contract_version") == "experimental-live-fuel-app-v1"
            and snapshot.get("connection") == "CONNECTED"
            and snapshot.get("source_mode") == "LIVE"
            and _number(snapshot.get("updated_age_s"), high=2)
            and _number(telemetry.get("speed_mps"), high=.1)
            and "READ_ERROR:Speed" not in monitor.get("reasons", [])
            and value["observation"]["review_draft_available"]):
        return None
    observation = value["observation"]
    loss = observation["net_loss_estimate_range_s"]
    return {"binding": (snapshot.get("generation"), value["binding_sha256"],
                         value["session_num"], value["player_car_idx"], value["revision"],
                         snapshot.get("pit_observation_revision")),
            "inputs": {"pit_entry_fraction": observation["entry_fraction"],
            "pit_exit_fraction": observation["exit_fraction"],
            "complete_pit_loss_low_s": math.floor(loss[0] * 10) / 10,
            "complete_pit_loss_high_s": math.ceil(loss[1] * 10) / 10}}
