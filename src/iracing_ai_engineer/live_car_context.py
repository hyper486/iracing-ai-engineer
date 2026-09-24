"""Bounded owned-car metadata/condition binding, not tire-model admission.

Raw setup content lives only in the private analysis queue. Public snapshots
contain a versioned fingerprint, numeric car id and numeric conditions, never
driver names, setup names, paths or raw setup values.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

from .adapters import _session_event_identity
from .engineer_session import canonical_sha256
from .live_strategy import _map, source_ready, source_scope
from .live_worker import payload_size
from .sdk_probe import classify_context

CONTRACT = "live-car-model-context-v1"
SETUP_METHOD = "sdk-car-setup-excluding-recorded-tire-measurements-v1"
FIELDS = ("PlayerCarClass", "TrackWetness", "TrackTempCrew", "AirTemp", "WindVel", "WindDir",
          "Precipitation")
_MEASUREMENTS = {"LastHotPressure", "LastTempsOMI", "LastTempsIMO", "TreadRemaining"}
_WHEELS = {"LeftFront", "RightFront", "LeftRear", "RightRear"}


def _int(value, low=0, high=2**53):
    return type(value) is int and low <= value <= high


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def _setup_copy(value):
    """Copy bounded plain JSON and omit only explicitly identified non-settings.

    Unknown fields stay in the fingerprint. Fuel settings, in-car dials and
    cosmetic settings are deliberately not assumed performance-irrelevant.
    """
    budget = [0, 0]

    def visit(item, path):
        budget[0] += 1
        if budget[0] > 1024 or len(path) > 8:
            raise ValueError("CAR_SETUP_LIMIT")
        if type(item) is dict:
            if len(item) > 128:
                raise ValueError("CAR_SETUP_LIMIT")
            result = {}
            for key, child in item.items():
                if type(key) is not str or not key or len(key) > 128:
                    raise ValueError("CAR_SETUP_INVALID")
                if (not path and key == "UpdateCount") or (
                    len(path) == 2 and path[0] == "Tires" and path[1] in _WHEELS
                    and key in _MEASUREMENTS
                ):
                    continue
                result[key] = visit(child, (*path, key))
            return result
        if type(item) is list:
            if len(item) > 128:
                raise ValueError("CAR_SETUP_LIMIT")
            return [visit(child, (*path, str(index))) for index, child in enumerate(item)]
        if (item is None or type(item) is bool
                or type(item) is str and len(item) <= 4096
                or _number(item, -2**53, 2**53)):
            if item is not None and (type(item) is not str or item.strip()):
                budget[1] += 1
            return item
        raise ValueError("CAR_SETUP_INVALID")

    if type(value) is not dict or not value or not any(key != "UpdateCount" for key in value):
        raise ValueError("CAR_SETUP_MISSING")
    result = visit(value, ())
    if budget[1] == 0:
        raise ValueError("CAR_SETUP_MISSING")
    payload_size(result, limit=256 * 1024)
    return result


class CarMetadataProjector:
    """Reader-side bounded projection cached by immutable transport update.

    Hashing stays on the analysis worker; no full DriverInfo is queued. The
    transport must not mutate a previously returned stable metadata snapshot.
    """

    def __init__(self):
        self._payload = self._key = self._result = None

    def project(self, payload, update, frame, scope):
        player, car_class = (frame.values.get(key) for key in ("PlayerCarIdx", "PlayerCarClass"))
        key = (update, player, car_class)
        if (scope != "FULL" or not isinstance(payload, Mapping)
                or not _int(update) or update != frame.session_info_update
                or not _int(player, high=255) or not _int(car_class, low=1)
                or set(frame.read_errors) & {"PlayerCarIdx", "PlayerCarClass"}):
            self._payload = self._key = self._result = None
            return None
        if self._payload is payload and self._key == key:
            return self._result
        self._payload, self._key, self._result = payload, key, None
        try:
            drivers = payload.get("DriverInfo")
            if not isinstance(drivers, Mapping) or drivers.get("DriverCarIdx") != player:
                return None
            rows = drivers.get("Drivers")
            if not _int(drivers.get("DriverCarIdx"), high=255) or type(rows) is not list:
                return None
            if not 1 <= len(rows) <= 256:
                return None
            matches = [row for row in rows if isinstance(row, Mapping)
                       and type(row.get("CarIdx")) is int and row["CarIdx"] == player]
            if len(matches) != 1:
                return None
            car = matches[0]
            if not _int(car.get("CarID"), low=1) or car.get("CarClassID") != car_class:
                return None
            if not _int(car.get("CarClassID"), low=1):
                return None
            identity, statuses = _session_event_identity(payload, scope="FULL")
            if any(value != "PRESENT" for value in statuses.values()):
                return None
            identity.update(car_class_id=car_class, provenance="SDK_DIRECT_SAME_SOURCE_CAPTURE")
            setup = _setup_copy(payload.get("CarSetup"))
            self._result = {"metadata_update": update, "player_car_idx": player,
                "car_model_id": car["CarID"], "event_identity": identity, "setup": setup}
        except (ValueError, TypeError, OverflowError):
            self._result = None
        return self._result


class LiveCarContext:
    def __init__(self):
        self.revision = 0
        self._metadata = self._signature = self._identity = None
        self._current = None

    def feed(self, frame, metadata):
        current = None
        context = classify_context(frame.sim_mode_raw, frame.values)
        owned = (context["sim_source_mode"] == "FULL"
                 and context["player_control_state"] == "IN_CAR_PHYSICS"
                 and not context["conflicts"])
        if (owned and type(metadata) is dict and metadata.get("metadata_update") ==
            frame.session_info_update and metadata.get("player_car_idx") ==
            frame.values.get("PlayerCarIdx")):
            if self._metadata is not metadata:
                self._metadata = metadata
                material = {"method": SETUP_METHOD, "car_model_id": metadata["car_model_id"],
                            "setup": metadata["setup"]}
                self._signature = {"car_model_id": metadata["car_model_id"],
                    "setup_sha256": canonical_sha256(material), "setup_method": SETUP_METHOD,
                    "identity_sha256": canonical_sha256(metadata["event_identity"])}
            current = self._signature
        identity = tuple(current.values()) if current else None
        if identity != self._identity:
            self.revision += 1
            self._identity = identity
        self._current = current

    def snapshot(self, frame, monitor):
        result = {"contract_version": CONTRACT, "revision": self.revision,
            "binding_sha256": monitor.get("binding_sha256"),
            "monitor_sequence": monitor.get("sequence"),
            "session_time_us": monitor.get("session_time_us"),
            "status": "WAIT", "reason": "CAR_METADATA_UNAVAILABLE", "identity": None,
            "conditions": None, "live_model_admitted": False, "live_acceptance": False}
        if self._current is None or not source_ready(monitor):
            return result
        result.update(status="BOUND", reason="METADATA_BOUND_NOT_MODEL_ADMITTED",
                      identity=dict(self._current))
        values = frame.values
        readings = {"air_temp_c": values.get("AirTemp"),
            "track_temp_c": values.get("TrackTempCrew"),
            "wind_speed_mps": values.get("WindVel"), "wind_direction_rad": values.get("WindDir"),
            "precipitation_pct": values.get("Precipitation"),
            "track_wetness": values.get("TrackWetness")}
        if (set(frame.read_errors) & set(FIELDS) or not _number(readings["air_temp_c"], -50, 60)
                or not _number(readings["track_temp_c"], -50, 100)
                or not _number(readings["wind_speed_mps"], 0, 100)
                or not _number(readings["wind_direction_rad"], 0, math.tau)
                or not _number(readings["precipitation_pct"], 0, 1)
                or not _int(readings["track_wetness"], high=7)):
            result["reason"] = "CONDITIONS_UNAVAILABLE"
        else:
            readings["precipitation_pct"] *= 100
            result["conditions"] = readings
        return result


def validated_car_context(snapshot):
    monitor = _map(snapshot.get("monitor"))
    value = _map(snapshot.get("car_context"))
    if (snapshot.get("connection") != "CONNECTED" or not source_ready(monitor)
            or not _number(snapshot.get("updated_age_s"), 0, 2)
            or value.get("binding_sha256") != monitor.get("binding_sha256")
            or value.get("monitor_sequence") != monitor.get("sequence")
            or value.get("session_time_us") != monitor.get("session_time_us")
            or value.get("status") != "BOUND" or value.get("contract_version") != CONTRACT
            or value.get("live_model_admitted") is not False
            or value.get("live_acceptance") is not False or not _int(value.get("revision"))):
        return None
    identity = value.get("identity")
    if (type(identity) is not dict or set(identity) != {
        "car_model_id", "setup_sha256", "setup_method", "identity_sha256"}
        or not _int(identity["car_model_id"], low=1) or identity["setup_method"] != SETUP_METHOD
        or any(type(identity[key]) is not str
               or re.fullmatch(r"[0-9a-f]{64}", identity[key]) is None
               for key in ("setup_sha256", "identity_sha256"))):
        return None
    return value


def car_context_binding(snapshot, *, parked=False):
    context = validated_car_context(snapshot)
    monitor = _map(snapshot.get("monitor"))
    scope = source_scope(monitor, snapshot.get("generation"))
    telemetry = monitor.get("telemetry") or {}
    if context is None or scope is None or (parked and not (
        telemetry.get("on_pit_road") is True
        and _number(telemetry.get("speed_mps"), 0, .1)
    )):
        return None
    identity = context["identity"]
    return (*scope, context["revision"], identity["car_model_id"], identity["setup_sha256"],
            identity["identity_sha256"])


def car_context_notice(snapshot):
    value = validated_car_context(snapshot)
    if value is None:
        return "校准绑定：车型／设置证据不完整；轮胎模型未接入。"
    identity = value["identity"]
    conditions = "条件信号完整" if value.get("conditions") is not None else "条件信号不完整"
    return (f"校准绑定：车型 ID {identity['car_model_id']}，"
            f"设置指纹 {identity['setup_sha256'][:12]}，"
            f"{conditions}。只核对当前设置，不代表模型已可用于比赛。")
