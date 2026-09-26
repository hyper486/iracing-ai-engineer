"""Driver-confirmed installation observation, not SDK service truth or wear.

The analysis owner alone updates this constant-space tracker. A pit exit or a
tire counter change never creates an origin. The offline REVIEWED_FULL_NEW_SET
contract is intentionally separate; these assertions cannot calibrate a model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .live_stint import _CORE, _int, _map, _value
from .live_strategy import source_ready, source_scope
from .sdk_probe import classify_context
from .telemetry import QualityStatus, SourceKind

CONTRACT = "driver-confirmed-tire-observation-v1"
BASIS = "DRIVER_CONFIRMED_FULL_NEW_SET"
KINDS = ("FULL_NEW_SET", "NO_TIRE_CHANGE", "PARTIAL_OR_UNKNOWN")
_TIRES = {"PlayerTireCompound", "TireSetsUsed"}
_SERVICE = {"Speed", "PlayerCarInPitStall", "PitstopActive"}


@dataclass(frozen=True)
class TireConfirmation:
    kind: str
    revision: int
    visit_tick: int
    requested_tick: int
    compound: int
    sets_used: int

    def __post_init__(self):
        if (type(self.kind) is not str or self.kind not in KINDS
                or not all(_int(value) for value in (
                    self.revision, self.visit_tick, self.requested_tick))
                or self.visit_tick > self.requested_tick
                or not _int(self.compound, 1000) or not _int(self.sets_used, 100_000)):
            raise ValueError("TIRE_CONFIRMATION_INVALID")


def unavailable_tire_age(monitor, reason="SOURCE_NOT_READY", *, revision=0):
    return {
        "contract_version": CONTRACT, "advisor_only": True, "executable": False,
        "live_acceptance": False, "physical_wear": None, "physical_tire_age": None,
        "basis": BASIS, "source_kind": monitor.get("source_kind"),
        "binding_sha256": monitor.get("binding_sha256"),
        "monitor_sequence": monitor.get("sequence"),
        "session_time_us": monitor.get("session_time_us"),
        "revision": revision, "status": "WAIT", "reason": reason,
        "point": None, "visit_tick": None, "can_confirm": False,
        "confirmation": None, "origin": None, "counter_increase": None,
    }


class LiveTireAgeTracker:
    def __init__(self, tick_rate_hz):
        if not _int(tick_rate_hz, 360) or tick_rate_hz < 1:
            raise ValueError("TIRE_TICK_RATE_INVALID")
        self.tick_rate, self.revision, self.failed = tick_rate_hz, 0, False
        self.previous = self.identity = self.visit_tick = None
        self._buffer_tick = None
        self.origin = self.confirmation = None
        self.departed_stall = False
        self.reason = "INSTALLATION_NOT_CONFIRMED"

    def reset(self, reason):
        reason = "TIRE_PROCESSING_ERROR" if self.failed else reason
        if self.previous is not None or self.reason != reason:
            self.revision += 1
        self.previous = self.identity = self.visit_tick = None
        self._buffer_tick = None
        self.origin = self.confirmation = None
        self.departed_stall = False
        self.reason = reason

    def fail(self):
        self.failed = True
        self.reset("TIRE_PROCESSING_ERROR")

    def feed(self, frame, sample):
        if self.failed:
            return
        context = classify_context(frame.sim_mode_raw, frame.values)
        if (context["sim_source_mode"] != "FULL"
                or context["player_control_state"] != "IN_CAR_PHYSICS" or context["conflicts"]
                or _value(sample.source.source_kind, direct=False) is not SourceKind.SDK_LIVE
                or _value(sample.quality.status, direct=False) is QualityStatus.REJECTED
                or _value(sample.quality.stale, direct=False) is not False
                or set(frame.read_errors) & (_CORE | _TIRES)):
            self.reset("SOURCE_NOT_READY")
            return
        tick, seconds, laps, pit, session, player, compound, sets = (
            _value(sample.session.session_tick), _value(sample.session.session_time_s),
            _value(sample.lap.laps_completed), _value(sample.pit.on_pit_road),
            _value(sample.session.session_num), _value(sample.opponents.player_car_idx),
            _value(sample.tires.player_tire_compound), _value(sample.tires.tire_sets_used),
        )
        if not (_int(tick) and _int(laps, 1_000_000) and _int(session, 100_000)
                and _int(player, 255) and _int(compound, 1000) and _int(sets, 100_000)
                and type(seconds) in (int, float) and 0 <= seconds <= 10_000_000
                and type(pit) is bool and _int(frame.buffer_tick, 2**31 - 1)):
            self.reset("REQUIRED_DATA_UNAVAILABLE")
            return
        speed, stall, active = (_value(sample.lap.speed_mps), _value(sample.pit.in_pit_stall),
                                _value(sample.pit.pitstop_active))
        service_valid = (not set(frame.read_errors) & _SERVICE and type(stall) is bool
                         and type(active) is bool and type(speed) in (int, float)
                         and 0 <= speed <= 200)
        point = {"tick": tick, "time_us": round(seconds * 1e6), "laps": laps,
                 "pit": pit, "session_num": session, "player_car_idx": player,
                 "compound": compound, "sets_used": sets,
                 "parked": bool(service_valid and pit and stall and not active and speed <= .1)}
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
        if previous is not None and (compound, sets) != (
                previous["compound"], previous["sets_used"]):
            self.origin = self.confirmation = None
            self.reason = "TIRE_CONTEXT_CHANGED"
            self.revision += 1
        if pit and previous is not None and not previous["pit"]:
            self.visit_tick, self.confirmation = tick, None
            self.departed_stall = False
            self.reason = "SERVICE_NOT_CONFIRMED"
            self.revision += 1
        if (pit and previous is not None and previous["pit"]
                and point["parked"] != previous["parked"]):
            # A two-utterance review must not survive a brief service restart,
            # departure or missing service field between its two snapshots.
            self.revision += 1
        if (pit or (previous is not None and previous["pit"])) and self.confirmation is not None:
            if not service_valid or active or (self.departed_stall and stall):
                self.origin = self.confirmation = None
                self.reason = "SERVICE_CHANGED_AFTER_CONFIRMATION"
                self.revision += 1
            elif not stall:
                self.departed_stall = True
        if not pit and previous is not None and previous["pit"]:
            confirmation = self.confirmation
            if confirmation is not None and confirmation["kind"] == "FULL_NEW_SET":
                self.origin = {"confirmation": confirmation.copy(), "exit_tick": tick,
                               "exit_laps": laps}
            elif confirmation is None or confirmation["kind"] != "NO_TIRE_CHANGE":
                self.origin = None
            self.visit_tick = None
            self.reason = "DRIVER_CONFIRMED_OBSERVATION" if self.origin else "INSTALLATION_UNKNOWN"
            self.revision += 1
        self.previous = point

    def confirm(self, command):
        """Consume once, on the analysis owner, after a fresh progressing frame.

        Returns a numeric/enum assertion receipt, not a service receipt. Rejected
        commands have no effect and cannot be replayed after a later recovery.
        """
        point = self.previous
        if not (type(command) is TireConfirmation and not self.failed and point
                and point["parked"] and self.visit_tick is not None
                and command.revision == self.revision and command.visit_tick == self.visit_tick
                and command.compound == point["compound"]
                and command.sets_used == point["sets_used"]
                and 0 <= point["tick"] - command.requested_tick <= self.tick_rate):
            return None
        receipt = {**asdict(command), "decision_tick": point["tick"],
                   "laps_completed": point["laps"], "session_num": point["session_num"],
                   "player_car_idx": point["player_car_idx"]}
        self.confirmation = receipt
        self.departed_stall = False
        if command.kind != "NO_TIRE_CHANGE":
            self.origin = None
        self.reason = "AWAITING_OBSERVED_EXIT"
        self.revision += 1
        return receipt.copy()

    def snapshot(self, monitor):
        result = unavailable_tire_age(monitor, self.reason, revision=self.revision)
        point, telemetry = self.previous, _map(monitor.get("telemetry"))
        if not (source_ready(monitor) and point is not None
                and point["time_us"] == monitor.get("session_time_us")
                and all(point[key] == telemetry.get(target) for key, target in (
                    ("laps", "laps_completed"), ("pit", "on_pit_road"),
                    ("session_num", "session_num"), ("player_car_idx", "player_car_idx"),
                    ("compound", "tire_compound"), ("sets_used", "tire_sets_used")))):
            return result
        origin = self.origin if not point["pit"] else None
        return {**result, "status": "OBSERVED", "point": point.copy(),
                "visit_tick": self.visit_tick,
                "can_confirm": point["parked"] and self.visit_tick is not None,
                "confirmation": self.confirmation.copy() if self.confirmation else None,
                "origin": ({**origin, "confirmation": origin["confirmation"].copy()}
                           if origin else None),
                "counter_increase": point["laps"] - origin["exit_laps"] if origin else None}


def valid_confirmation_receipt(value):
    if type(value) is not dict or set(value) != {
        "kind", "revision", "visit_tick", "requested_tick", "compound", "sets_used",
        "decision_tick", "laps_completed", "session_num", "player_car_idx",
    }:
        return False
    try:
        TireConfirmation(**{key: value[key] for key in TireConfirmation.__dataclass_fields__})
    except (TypeError, ValueError):
        return False
    return (_int(value["decision_tick"])
            and 0 <= value["decision_tick"] - value["requested_tick"] <= 360
            and _int(value["laps_completed"], 1_000_000)
            and _int(value["session_num"], 100_000) and _int(value["player_car_idx"], 255))


def validated_tire_age(snapshot):
    value, monitor = _map(snapshot.get("tire_age")), _map(snapshot.get("monitor"))
    telemetry = _map(monitor.get("telemetry"))
    point = _map(value.get("point"))
    if not (source_ready(monitor) and set(value) == set(unavailable_tire_age({}))
            and value["contract_version"] == CONTRACT and value["advisor_only"] is True
            and value["executable"] is False and value["live_acceptance"] is False
            and value["physical_wear"] is None and value["physical_tire_age"] is None
            and value["basis"] == BASIS and _int(value["revision"])
            and value["status"] == "OBSERVED" and value["source_kind"] == "SDK_LIVE"
            and all(value[key] == monitor.get(key) for key in (
                "binding_sha256", "session_time_us"))
            and type(value["binding_sha256"]) is str and len(value["binding_sha256"]) == 64
            and all(c in "0123456789abcdef" for c in value["binding_sha256"])
            and _int(value["session_time_us"]) and _int(value["monitor_sequence"])
            and value["monitor_sequence"] == monitor.get("sequence")
            and set(point) == {"tick", "time_us", "laps", "pit", "session_num", "player_car_idx",
                              "compound", "sets_used", "parked"}
            and all(_int(point[key], maximum) for key, maximum in (
                ("tick", 2**53), ("time_us", 2**53), ("laps", 1_000_000),
                ("session_num", 100_000), ("player_car_idx", 255),
                ("compound", 1000), ("sets_used", 100_000)))
            and point["time_us"] == value["session_time_us"]
            and type(point["pit"]) is bool and type(point["parked"]) is bool
            and all(type(telemetry.get(target)) is type(point[key])
                    and point[key] == telemetry[target] for key, target in (
                        ("laps", "laps_completed"), ("pit", "on_pit_road"),
                        ("session_num", "session_num"), ("player_car_idx", "player_car_idx"),
                        ("compound", "tire_compound"), ("sets_used", "tire_sets_used")))
            and not any(f"READ_ERROR:{name}" in monitor["reasons"] for name in _CORE | _TIRES)):
        return None
    visit = value["visit_tick"]
    if visit is not None and not (_int(visit) and visit <= point["tick"] and point["pit"]):
        return None
    speed = telemetry.get("speed_mps")
    parked = (point["pit"] and telemetry.get("in_pit_stall") is True
              and telemetry.get("pitstop_active") is False
              and type(speed) in (int, float) and 0 <= speed <= .1
              and not any(f"READ_ERROR:{name}" in monitor["reasons"] for name in _SERVICE))
    if point["parked"] is not parked or value["can_confirm"] is not (parked and visit is not None):
        return None
    def matches(receipt):
        return (valid_confirmation_receipt(receipt) and receipt["decision_tick"] <= point["tick"]
                and receipt["revision"] < value["revision"]
                and receipt["laps_completed"] <= point["laps"]
                and all(receipt[key] == point[key] for key in (
                    "compound", "sets_used", "session_num", "player_car_idx")))
    confirmation = value["confirmation"]
    if confirmation is not None and (not matches(confirmation) or (
            visit is not None and confirmation["visit_tick"] != visit)):
        return None
    origin = value["origin"]
    if origin is None:
        return value if value["counter_increase"] is None else None
    if not (type(origin) is dict and set(origin) == {"confirmation", "exit_tick", "exit_laps"}
            and not point["pit"] and visit is None and matches(origin["confirmation"])
            and origin["confirmation"]["kind"] == "FULL_NEW_SET"
            and _int(origin["exit_tick"]) and _int(origin["exit_laps"], 1_000_000)
            and origin["confirmation"]["decision_tick"] < origin["exit_tick"] <= point["tick"]
            and origin["confirmation"]["laps_completed"] <= origin["exit_laps"] <= point["laps"]
            and _int(value["counter_increase"])
            and value["counter_increase"] == point["laps"] - origin["exit_laps"]
            and confirmation is not None
            and confirmation["kind"] in ("FULL_NEW_SET", "NO_TIRE_CHANGE")
            and (confirmation == origin["confirmation"] or (
                confirmation["kind"] == "NO_TIRE_CHANGE"
                and confirmation["visit_tick"] > origin["exit_tick"]))):
        return None
    return value


def confirmation_command(snapshot, kind):
    value = validated_tire_age(snapshot)
    age = snapshot.get("updated_age_s")
    if not (snapshot.get("connection") == "CONNECTED"
            and type(age) in (int, float) and 0 <= age <= .75
            and value is not None and value["can_confirm"]):
        raise ValueError("TIRE_CONFIRMATION_NOT_READY")
    point = value["point"]
    return TireConfirmation(kind, value["revision"], value["visit_tick"], point["tick"],
                            point["compound"], point["sets_used"])


def confirmation_binding(snapshot):
    """Bind a parked review to this connection and uninterrupted service state."""
    if snapshot.get("source_mode") != "LIVE":
        return None
    scope = source_scope(_map(snapshot.get("monitor")), snapshot.get("generation"))
    if scope is None:
        return None
    try:
        command = confirmation_command(snapshot, "FULL_NEW_SET")
    except ValueError:
        return None
    return (*scope, command.revision, command.visit_tick, command.compound, command.sets_used)


def tire_age_binding(snapshot):
    value = validated_tire_age(snapshot)
    return ((value["revision"], value["point"]["pit"], value["counter_increase"])
            if value else None)


def tire_age_notice(snapshot):
    value = validated_tire_age(snapshot)
    if value is None:
        return ("确认胎组观测：分析故障，请重连。" if _map(snapshot.get("tire_age")).get("reason")
                == "TIRE_PROCESSING_ERROR" else "确认胎组观测：等待新鲜连续数据。")
    if value["origin"] is not None:
        return (f"车手确认四胎换新并出站后，计圈增加 {value['counter_increase']}；"
                "基于人工确认，不是 SDK 换胎证明、完整行驶圈数或磨损。")
    if value["confirmation"] is not None and value["point"]["pit"]:
        return "已记录本次人工确认；等待连续出站，当前不提供胎龄。"
    return "胎组安装起点未知；需观测进站后在停车位、服务结束时人工确认。"
