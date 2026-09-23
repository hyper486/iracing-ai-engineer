"""Bounded, observation-only current traffic; not collision or rejoin prediction.

Reuse normalized SDK presence/provenance and the offline adapter's fixed-point
geometry without manufacturing a sealed collector receipt for a live snapshot.
No identities, raw metadata, speed-to-gap conversion or simulator commands.
"""

from __future__ import annotations

from collections.abc import Mapping

from .adapters import (
    _LAP_POSITION_SCALE,
    TelemetryAdapterError,
    _lap_distance_mm,
    _lap_position_ppb,
    _parse_track_length_mm,
)
from .telemetry import Presence, Provenance, TelemetrySample

TRAFFIC_CONTRACT_VERSION = "live-traffic-observation-v1"
TRAFFIC_ANSWER_TTL_S = 10.0
MAX_OPPONENTS = 256
OVERLAP_MM = 5_000
_REQUIRED_READS = {
    "PlayerCarIdx", "LapDistPct", "PlayerTrackSurface", "OnPitRoad",
    "CarIdxLapDistPct", "CarIdxOnPitRoad", "CarIdxTrackSurface",
}


def bound_track_length_mm(payload, update, frame):
    """Only reduce the exact bound metadata; never return or cache free text."""
    if (not isinstance(payload, Mapping) or type(update) is not int or update < 0
            or update != frame.session_info_update):
        return None
    weekend = payload.get("WeekendInfo")
    if not isinstance(weekend, Mapping):
        return None
    try:
        return _parse_track_length_mm(weekend.get("TrackLength"))
    except TelemetryAdapterError:
        return None


def _direct(field):
    return (field.value if field.presence is Presence.PRESENT
            and field.provenance is Provenance.SDK_DIRECT else None)


def unavailable_traffic(monitor, reason, *, metadata_update=None, track_length_mm=None):
    return {
        "contract_version": TRAFFIC_CONTRACT_VERSION, "advisor_only": True,
        "executable": False, "physical_observation_only": True,
        "source_kind": monitor.get("source_kind"),
        "binding_sha256": monitor.get("binding_sha256"),
        "monitor_sequence": monitor.get("sequence"),
        "session_time_us": monitor.get("session_time_us"),
        "metadata_update": metadata_update, "track_length_mm": track_length_mm,
        "status": "UNAVAILABLE", "reason": reason,
        "eligible_count": None, "excluded_count": None, "overlap_count": None,
        "ahead": None, "behind": None,
    }


def project_live_traffic(sample: TelemetrySample, monitor: Mapping, track_length_mm,
                         *, metadata_update=None):
    result = unavailable_traffic(monitor, "SOURCE_NOT_READY", metadata_update=metadata_update,
                                 track_length_mm=track_length_mm)
    context, quality = monitor.get("context", {}), monitor.get("quality", {})
    reasons = monitor.get("reasons")
    if not (
        monitor.get("source_kind") == "SDK_LIVE" and monitor.get("status") in ("READY", "DEGRADED")
        and quality.get("stale") is False and quality.get("status") in ("READY", "DEGRADED")
        and context.get("sim_source_mode") == "FULL"
        and context.get("player_control_state") == "IN_CAR_PHYSICS"
        and context.get("conflicts") == [] and type(reasons) is list
    ):
        return result
    if any(f"READ_ERROR:{field}" in reasons for field in _REQUIRED_READS):
        return {**result, "reason": "TRAFFIC_READ_ERROR"}
    if (_direct(sample.flags.player_track_surface) != 3
            or _direct(sample.pit.on_pit_road) is not False):
        return {**result, "reason": "PLAYER_NOT_ON_RACING_SURFACE"}
    if type(track_length_mm) is not int or not 100_000 < track_length_mm <= 100_000_000:
        return {**result, "reason": "BOUND_TRACK_LENGTH_UNAVAILABLE", "track_length_mm": None}
    opponents = sample.opponents
    player, position = _direct(opponents.player_car_idx), _direct(sample.lap.lap_distance_pct)
    if (type(player) is not int or not 0 <= player <= MAX_OPPONENTS
            or type(position) not in (int, float) or not 0 <= position <= 1):
        return {**result, "reason": "PLAYER_POSITION_UNAVAILABLE"}
    if (opponents.presence is not Presence.PRESENT
            or opponents.provenance is not Provenance.SDK_DIRECT or opponents.issues
            or len(opponents.entries) > MAX_OPPONENTS):
        return {**result, "reason": "OPPONENT_ARRAYS_UNAVAILABLE"}
    player_position = _lap_position_ppb(position)
    player_laps = _direct(sample.lap.laps_completed)
    ahead, behind, seen = [], [], set()
    eligible = excluded = overlaps = 0
    for opponent in opponents.entries:
        index = opponent.car_idx.value
        surface, in_pits, fraction = (_direct(field) for field in (
            opponent.track_surface, opponent.on_pit_road, opponent.lap_distance_pct))
        if (opponent.car_idx.presence is not Presence.PRESENT
                or opponent.car_idx.provenance is not Provenance.DERIVED
                or type(index) is not int or not 0 <= index <= MAX_OPPONENTS
                or index == player or index in seen
                or type(surface) is not int or type(in_pits) is not bool
                or type(fraction) not in (int, float)):
            return {**result, "reason": "OPPONENT_FIELDS_UNAVAILABLE"}
        seen.add(index)
        if surface != 3 or in_pits or not 0 <= fraction <= 1:
            excluded += 1
            continue
        eligible += 1
        other_position = _lap_position_ppb(fraction)
        forward = (other_position - player_position) % _LAP_POSITION_SCALE
        backward = (player_position - other_position) % _LAP_POSITION_SCALE
        forward_mm, backward_mm = (_lap_distance_mm(units, track_length_mm)
                                   for units in (forward, backward))
        if forward == 0 or min(forward_mm, backward_mm) <= OVERLAP_MM:
            overlaps += 1
            continue
        other_laps = _direct(opponent.laps_completed)
        # A completed-lap counter difference is not necessarily a lapped car:
        # two cars straddling start/finish can still be only a few metres apart.
        delta = (other_laps - player_laps if type(other_laps) is int and other_laps >= 0
                 and type(player_laps) is int and player_laps >= 0 else None)
        for target, distance in ((ahead, forward_mm), (behind, backward_mm)):
            target.append({"car_idx": index, "distance_mm": distance,
                           "completed_laps_delta": delta})
    result.update(status="AMBIGUOUS" if overlaps else "READY",
                  reason="LONGITUDINAL_OVERLAP" if overlaps else "OBSERVATION_ONLY",
                  eligible_count=eligible, excluded_count=excluded, overlap_count=overlaps)
    if not overlaps:
        for name, candidates in (("ahead", ahead), ("behind", behind)):
            result[name] = min(candidates, key=lambda row: (row["distance_mm"], row["car_idx"])) \
                if candidates else None
    return result


def validated_traffic(snapshot):
    """Check the projection's identity, shape and bounds before any UI/LLM use.

    The caller still admits connected/fresh/in-car source state. A local binding
    is consistency evidence, not authentication of the original SDK producer.
    """
    value, monitor = snapshot.get("traffic"), snapshot.get("monitor")
    if not isinstance(value, Mapping) or not isinstance(monitor, Mapping):
        return None
    length = value.get("track_length_mm")
    binding = value.get("binding_sha256")
    if not (
        value.get("contract_version") == TRAFFIC_CONTRACT_VERSION
        and value.get("advisor_only") is True and value.get("executable") is False
        and value.get("physical_observation_only") is True
        and value.get("source_kind") == monitor.get("source_kind") == "SDK_LIVE"
        and type(binding) is str and len(binding) == 64
        and all(character in "0123456789abcdef" for character in binding)
        and binding == monitor.get("binding_sha256")
        and type(value.get("monitor_sequence")) is int and value["monitor_sequence"] >= 0
        and type(monitor.get("sequence")) is int
        and value["monitor_sequence"] == monitor.get("sequence")
        and type(value.get("session_time_us")) is int and value["session_time_us"] >= 0
        and type(monitor.get("session_time_us")) is int
        and value["session_time_us"] == monitor.get("session_time_us")
        and type(value.get("metadata_update")) is int and value["metadata_update"] >= 0
        and type(length) is int and 100_000 < length <= 100_000_000
        and value.get("status") in ("READY", "AMBIGUOUS")
        and all(type(value.get(key)) is int and 0 <= value[key] <= MAX_OPPONENTS
                for key in ("eligible_count", "excluded_count", "overlap_count"))
        and value["eligible_count"] + value["excluded_count"] <= MAX_OPPONENTS
        and value["overlap_count"] <= value["eligible_count"]
    ):
        return None
    telemetry = monitor.get("telemetry")
    if not isinstance(telemetry, Mapping):
        return None
    player = telemetry.get("player_car_idx")
    if type(player) is not int or not 0 <= player <= MAX_OPPONENTS:
        return None
    if value["status"] == "AMBIGUOUS":
        return value if value["overlap_count"] > 0 and all(
            value.get(key) is None for key in ("ahead", "behind")) else None
    if value["overlap_count"] != 0:
        return None
    for name in ("ahead", "behind"):
        row = value.get(name)
        if value["eligible_count"] == 0:
            if row is not None:
                return None
        elif not (
            isinstance(row, Mapping) and type(row.get("car_idx")) is int
            and 0 <= row["car_idx"] <= MAX_OPPONENTS and row["car_idx"] != player
            and type(row.get("distance_mm")) is int and OVERLAP_MM < row["distance_mm"] <= length
        ):
            return None
    return value


def situation_binding(snapshot):
    monitor = snapshot.get("monitor") or {}
    telemetry = monitor.get("telemetry") or {}
    reasons = monitor.get("reasons")
    readable = type(reasons) is list and all(type(reason) is str for reason in reasons)
    traffic = validated_traffic(snapshot)
    identity = None
    if traffic is not None:
        identity = (traffic["status"], traffic["track_length_mm"],
                    traffic["eligible_count"], traffic["excluded_count"], traffic["overlap_count"],
                    tuple((name, (row["car_idx"], row["distance_mm"] <=
                                  traffic["track_length_mm"] / 2) if row else None)
                          for name in ("ahead", "behind") for row in (traffic.get(name),)))
    return (snapshot.get("situation_revision"), identity,
            telemetry.get("player_car_idx"), telemetry.get("on_pit_road"),
            telemetry.get("player_track_surface"),
            telemetry.get("pits_open")
            if readable and "READ_ERROR:PitsOpen" not in reasons else None,
            telemetry.get("session_flags")
            if readable and "READ_ERROR:SessionFlags" not in reasons else None)
