"""Constant-space observed stint progress, never physical tire age or wear.

A pit exit starts a new driving stint but does not reset tire-counter history.
Joining mid-run gives only a partial observation. Missing/changed counters
start another observation; neither counter equality nor change proves grip,
the age of every tire, or that a full fresh set was installed.
"""

from __future__ import annotations

from collections.abc import Mapping

from .live_strategy import source_ready
from .sdk_probe import classify_context
from .telemetry import Presence, Provenance, QualityStatus, SourceKind

CONTRACT = "live-stint-observation-v1"
_CORE = {"SessionTick", "SessionTime", "SessionNum", "PlayerCarIdx", "LapCompleted",
         "OnPitRoad", "IsOnTrack", "IsOnTrackCar", "IsReplayPlaying"}


def _map(value):
    return value if isinstance(value, Mapping) else {}


def _int(value, maximum=2**53):
    return type(value) is int and 0 <= value <= maximum


def _value(field, *, direct=True):
    return field.value if (field.presence is Presence.PRESENT
                           and (not direct or field.provenance is Provenance.SDK_DIRECT)) else None


def unavailable_stint(monitor, reason="SOURCE_NOT_READY", *, revision=0):
    return {
        "contract_version": CONTRACT, "advisor_only": True, "executable": False,
        "live_acceptance": False, "physical_tire_age": None, "physical_wear": None,
        "source_kind": monitor.get("source_kind"),
        "binding_sha256": monitor.get("binding_sha256"),
        "monitor_sequence": monitor.get("sequence"),
        "session_time_us": monitor.get("session_time_us"),
        "revision": revision, "status": "WAIT", "reason": reason,
        "stint": None, "tire_observation": None,
    }


class LiveStintTracker:
    """One analysis owner; holds two origins and the latest numeric point."""

    def __init__(self, tick_rate_hz):
        if not _int(tick_rate_hz, 360) or tick_rate_hz < 1:
            raise ValueError("STINT_TICK_RATE_INVALID")
        self.tick_rate = tick_rate_hz
        self.revision = 0
        self.reason = "SOURCE_NOT_READY"
        self.previous = self.identity = self.stint = self.tires = None
        self._buffer_tick = None
        self.failed = False

    def reset(self, reason):
        if self.failed:
            reason = "STINT_PROCESSING_ERROR"
        if self.previous is not None or self.reason != reason:
            self.revision += 1
        self.previous = self.identity = self.stint = self.tires = None
        self._buffer_tick = None
        self.reason = reason

    def fail(self):
        self.failed = True
        self.reset("STINT_PROCESSING_ERROR")

    def feed(self, frame, sample):
        if self.failed:
            return
        context = classify_context(frame.sim_mode_raw, frame.values)
        if (context["sim_source_mode"] != "FULL"
                or context["player_control_state"] != "IN_CAR_PHYSICS" or context["conflicts"]
                or _value(sample.source.source_kind, direct=False) is not SourceKind.SDK_LIVE
                or _value(sample.quality.status, direct=False) is QualityStatus.REJECTED
                or _value(sample.quality.stale, direct=False) is not False
                or set(frame.read_errors) & _CORE):
            self.reset("SOURCE_NOT_READY")
            return
        tick, seconds, laps, pit, session, player = (
            _value(sample.session.session_tick), _value(sample.session.session_time_s),
            _value(sample.lap.laps_completed), _value(sample.pit.on_pit_road),
            _value(sample.session.session_num), _value(sample.opponents.player_car_idx),
        )
        if not (_int(tick) and _int(laps, 1_000_000) and _int(session, 100_000)
                and _int(player, 255)
                and type(seconds) in (int, float) and 0 <= seconds <= 10_000_000
                and type(pit) is bool and _int(frame.buffer_tick, 2**31 - 1)):
            self.reset("REQUIRED_DATA_UNAVAILABLE")
            return
        point = {"tick": tick, "time_us": round(seconds * 1_000_000),
                 "laps": laps, "pit": pit}
        identity = (_value(sample.session.session_id, direct=False), session, player)
        previous = self.previous
        if self.identity != identity or (previous is not None and (
            tick <= previous["tick"] or point["time_us"] <= previous["time_us"]
            or (tick - previous["tick"]) / self.tick_rate > .25
            or self._buffer_tick is None
            or not 0 < frame.buffer_tick - self._buffer_tick <= self.tick_rate * .25
            or point["time_us"] - previous["time_us"] > 250_000
            or laps < previous["laps"] or laps > previous["laps"] + 1
        )):
            self.reset("CONTINUITY_CHANGED")
            previous = None
        self.identity = identity
        self._buffer_tick = frame.buffer_tick
        if self.stint is None or (previous is not None and previous["pit"] and not pit):
            self.stint = {"origin_time_us": point["time_us"], "origin_laps": laps,
                          "origin_kind": "OBSERVED_PIT_EXIT" if previous is not None
                          and previous["pit"] and not pit else "PARTIAL_OBSERVATION"}
            self.revision += 1
        if previous is not None and previous["pit"] != pit:
            self.revision += 1
        compound, sets = (_value(sample.tires.player_tire_compound),
                          _value(sample.tires.tire_sets_used))
        if (not _int(compound, 1000) or not _int(sets, 100_000)
                or set(frame.read_errors) & {"PlayerTireCompound", "TireSetsUsed"}):
            if self.tires is not None:
                self.revision += 1
            self.tires = None
        elif self.tires is None or (compound, sets) != (
                self.tires["compound"], self.tires["sets_used"]):
            self.tires = {"compound": compound, "sets_used": sets,
                          "origin_time_us": point["time_us"], "origin_laps": laps}
            self.revision += 1
        self.previous, self.reason = point, "OBSERVED_ONLY"

    def snapshot(self, monitor):
        result = unavailable_stint(monitor, self.reason, revision=self.revision)
        point, telemetry = self.previous, _map(monitor.get("telemetry"))
        if not (source_ready(monitor) and point is not None
                and point["time_us"] == monitor.get("session_time_us")
                and point["laps"] == telemetry.get("laps_completed")
                and point["pit"] is telemetry.get("on_pit_road")
                and self.identity[1:] == (telemetry.get("session_num"),
                                          telemetry.get("player_car_idx"))):
            return result
        def elapsed(origin):
            return {**origin, "elapsed_s": (point["time_us"] - origin["origin_time_us"]) / 1e6,
                    "counter_increase": point["laps"] - origin["origin_laps"]}
        return {**result, "status": "OBSERVED", "on_pit_road": point["pit"],
                "session_num": self.identity[1], "player_car_idx": self.identity[2],
                "stint": elapsed(self.stint),
                "tire_observation": elapsed(self.tires) if self.tires is not None else None}


def validated_stint(snapshot):
    """Bind numeric observation endpoints to the current publication."""
    value, monitor = _map(snapshot.get("stint")), _map(snapshot.get("monitor"))
    telemetry = _map(monitor.get("telemetry"))
    if not (source_ready(monitor) and value.get("contract_version") == CONTRACT
            and value.get("advisor_only") is True and value.get("executable") is False
            and value.get("live_acceptance") is False
            and all(key in value and value[key] is None
                    for key in ("physical_wear", "physical_tire_age"))
            and "tire_observation" in value and value.get("status") == "OBSERVED"
            and value.get("reason") == "OBSERVED_ONLY" and _int(value.get("revision"))
            and value.get("source_kind") == monitor.get("source_kind") == "SDK_LIVE"
            and type(value.get("binding_sha256")) is str
            and value["binding_sha256"] == monitor.get("binding_sha256")
            and len(value["binding_sha256"]) == 64
            and all(c in "0123456789abcdef" for c in value["binding_sha256"])
            and _int(value.get("session_time_us"))
            and value["session_time_us"] == monitor.get("session_time_us")
            and _int(value.get("monitor_sequence"))
            and value["monitor_sequence"] == monitor.get("sequence")
            and not any(f"READ_ERROR:{name}" in monitor["reasons"] for name in _CORE)
            and all(_int(value.get(key)) and type(telemetry.get(key)) is int
                    and value[key] == telemetry[key] for key in ("session_num", "player_car_idx"))
            and _int(value.get("session_num"), 100_000)
            and _int(value.get("player_car_idx"), 255)
            and _int(telemetry.get("laps_completed"), 1_000_000)
            and type(value.get("on_pit_road")) is bool
            and value["on_pit_road"] is telemetry.get("on_pit_road")):
        return None
    for key in ("stint", "tire_observation"):
        origin = value.get(key)
        if origin is None and key == "tire_observation":
            continue
        origin = _map(origin)
        if not (_int(origin.get("origin_time_us")) and _int(origin.get("origin_laps"))
                and origin["origin_time_us"] <= value["session_time_us"]
                and origin["origin_laps"] <= telemetry["laps_completed"]
                and _int(origin.get("counter_increase"))
                and origin["counter_increase"] == (
                    telemetry["laps_completed"] - origin["origin_laps"])
                and type(origin.get("elapsed_s")) in (int, float)
                and origin["elapsed_s"] == (
                    value["session_time_us"] - origin["origin_time_us"]) / 1e6):
            return None
        if key == "stint":
            if origin.get("origin_kind") not in ("PARTIAL_OBSERVATION", "OBSERVED_PIT_EXIT"):
                return None
        elif not (_int(origin.get("compound"), 1000) and _int(origin.get("sets_used"), 100_000)
                  and not any(f"READ_ERROR:{name}" in monitor["reasons"]
                              for name in ("PlayerTireCompound", "TireSetsUsed"))
                  and type(telemetry.get("tire_compound")) is int
                  and type(telemetry.get("tire_sets_used")) is int
                  and origin["compound"] == telemetry.get("tire_compound")
                  and origin["sets_used"] == telemetry.get("tire_sets_used")):
            return None
    return value


def stint_binding(snapshot):
    value = validated_stint(snapshot)
    if value is None:
        return None
    return (value["revision"], value["on_pit_road"],
            _map(_map(snapshot.get("monitor")).get("telemetry")).get("laps_completed"))


def stint_notice(snapshot):
    value = validated_stint(snapshot)
    if value is None:
        return ("本段观测：分析故障，请重连后重试。" if _map(snapshot.get("stint")).get("reason")
                == "STINT_PROCESSING_ERROR" else "本段观测：等待新鲜的本人车内数据。")
    stint = value["stint"]
    label = "出站后" if stint["origin_kind"] == "OBSERVED_PIT_EXIT" else "仅已观察区间"
    pit = "，当前在进站通道" if value["on_pit_road"] else ""
    return (f"本段观测：{label} {stint['elapsed_s'] / 60:.1f} 分钟，"
            f"计圈增加 {stint['counter_increase']}{pit}；不是轮胎年龄。")
