"""Bounded whole-lap pace observations for conditional, non-control rejoin use.

Two completed laps per currently on-track actor, never an instantaneous-speed
extrapolation. Crossing-time brackets retain sampling uncertainty. The resulting
empirical range is a scenario assumption, not calibrated forecast coverage.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from .live_stint import _int, _value
from .live_strategy import _map, _same_typed_tree, source_ready
from .rejoin_projection import phase_time
from .sdk_probe import classify_context
from .telemetry import Presence, Provenance, QualityStatus, SourceKind

CONTRACT = "live-whole-lap-motion-v1"
MAX_CARS = 256
BINS = 64
_READS = {
    "SessionTick",
    "SessionTime",
    "SessionNum",
    "PlayerCarIdx",
    "LapCompleted",
    "LapDistPct",
    "OnPitRoad",
    "PlayerTrackSurface",
    "IsOnTrack",
    "IsOnTrackCar",
    "IsReplayPlaying",
    "CarIdxLapCompleted",
    "CarIdxLapDistPct",
    "CarIdxOnPitRoad",
    "CarIdxTrackSurface",
    "SessionFlags",
    "PlayerCarMyIncidentCount",
}


def unavailable_motion(monitor, reason="SOURCE_NOT_READY", *, revision=0):
    return {
        "contract_version": CONTRACT,
        "advisor_only": True,
        "executable": False,
        "live_acceptance": False,
        "source_kind": monitor.get("source_kind"),
        "binding_sha256": monitor.get("binding_sha256"),
        "monitor_sequence": monitor.get("sequence"),
        "session_time_us": monitor.get("session_time_us"),
        "revision": revision,
        "status": "WAIT",
        "reason": reason,
        "player": None,
        "opponents": [],
        "excluded_count": 0,
    }


def _progress(laps, fraction):
    return (
        laps + fraction
        if _int(laps, 1_000_000) and type(fraction) in (int, float) and 0 <= fraction < 1
        else None
    )


def profile_bounds(crossings, profiles, now):
    """Validate two completed observed profiles, independent of the current phase."""
    if len(crossings) != 3 or len(profiles) != 2:
        return None
    if any(
        not isinstance(pair, list)
        or len(pair) != 2
        or not all(_int(t) for t in pair)
        or not 0 <= pair[1] - pair[0] <= 250_000
        or pair[1] > now
        for pair in crossings
    ):
        return None
    durations = [(b[0] - a[1], b[1] - a[0]) for a, b in zip(crossings, crossings[1:], strict=False)]
    low, high = min(pair[0] for pair in durations), max(pair[1] for pair in durations)
    if not 5_000_000 <= low <= high <= 1_200_000_000 or high / low > 1.25:
        return None
    # A stale pace cannot survive a parked/missing actor for another full lap.
    if now - crossings[-1][1] > high * 1.25:
        return None
    for index_in_history, profile in enumerate(profiles):
        if not isinstance(profile, Mapping) or set(profile) != {"elapsed_us", "sampling_error_us"}:
            return None
        times, error = profile["elapsed_us"], profile["sampling_error_us"]
        if (
            type(times) is not list
            or len(times) != BINS + 1
            or not all(_int(value, 1_200_000_000) for value in times)
            or times[0] != 0
            or any(a >= b for a, b in zip(times, times[1:], strict=False))
            or not _int(error, 250_000)
            or error == 0
            or not durations[index_in_history][0] <= times[-1] <= durations[index_in_history][1]
        ):
            return None
    return low, high


def _actor(index, progress, crossings, profiles, now):
    bounds = profile_bounds(crossings, profiles, now)
    if bounds is None:
        return None
    _low, high = bounds
    observed_phase = now - sum(crossings[-1]) / 2
    predicted_phases = [phase_time(profile, progress % 1) for profile in profiles]
    tolerance = max(1_000_000, high * .01)
    if not min(predicted_phases) - tolerance <= observed_phase <= max(predicted_phases) + tolerance:
        return None
    return {
        "car_idx": index,
        "progress_laps": round(progress, 9),
        "crossing_windows_us": [list(pair) for pair in crossings],
        "lap_profiles": [
            {
                "elapsed_us": list(profile["elapsed_us"]),
                "sampling_error_us": profile["sampling_error_us"],
            }
            for profile in profiles
        ],
        "rate_range_laps_per_s": [round(1e6 / high, 9), round(1e6 / bounds[0], 9)],
    }


class _Trace:
    """Two completed 64-bin phase profiles plus one partial lap, never raw laps."""

    def __init__(self, position, now):
        self.position, self.time = position, now
        self.crossings, self.profiles = [], []
        self.active = self.start = None
        self.error = 0

    def advance(self, position, now):
        delta, elapsed = position - self.position, now - self.time
        if not -1e-9 <= delta <= elapsed / 5_000_000:
            self.__init__(position, now)
            return True
        self.error = max(self.error, elapsed)
        for boundary in range(
            math.floor(self.position * BINS) + 1, math.floor(position * BINS) + 1
        ):
            crossing = round(self.time + elapsed * (boundary / BINS - self.position) / delta)
            if boundary % BINS == 0:
                self.crossings = [*self.crossings[-2:], [self.time, now]]
                if self.active is not None and len(self.active) == BINS:
                    self.profiles = [
                        *self.profiles[-1:],
                        {
                            "elapsed_us": [*self.active, crossing - self.start],
                            "sampling_error_us": self.error,
                        },
                    ]
                self.active, self.start, self.error = [0], crossing, elapsed
            elif self.active is not None:
                self.active.append(crossing - self.start)
        self.position, self.time = position, now
        # Normal profile refreshes must not repeatedly cancel queued speech.
        # Continuity resets above do invalidate it; projected neighbor/gap changes
        # have their own semantic binding and a short independent answer TTL.
        return False


class LiveMotionTracker:
    """Fixed-size per-car phase observations; one analysis owner, no I/O."""

    def __init__(self, tick_rate_hz):
        if not _int(tick_rate_hz, 360) or tick_rate_hz < 1:
            raise ValueError("MOTION_TICK_RATE_INVALID")
        self.tick_rate = tick_rate_hz
        self.revision = 0
        self.failed = False
        self.reason = "SOURCE_NOT_READY"
        self.identity = self.previous = None
        self.actors = {}
        self.excluded = 0

    def reset(self, reason):
        reason = "MOTION_PROCESSING_ERROR" if self.failed else reason
        if self.previous is not None or reason != self.reason:
            self.revision += 1
        self.reason, self.previous, self.identity = reason, None, None
        self.actors.clear()
        self.excluded = 0

    def fail(self):
        self.failed = True
        self.reset("MOTION_PROCESSING_ERROR")

    def feed(self, frame, sample):
        if self.failed:
            return
        context = classify_context(frame.sim_mode_raw, frame.values)
        issues = _value(sample.quality.issues, direct=False) or ()
        if (
            context["sim_source_mode"] != "FULL"
            or context["player_control_state"] != "IN_CAR_PHYSICS"
            or context["conflicts"]
            or _value(sample.source.source_kind, direct=False) is not SourceKind.SDK_LIVE
            or _value(sample.quality.status, direct=False) is QualityStatus.REJECTED
            or _value(sample.quality.stale, direct=False) is not False
            or set(frame.read_errors) & _READS
            or any(
                issue in {"SOURCE_BOUNDARY", "SESSION_BOUNDARY"}
                or str(issue).startswith("CONTINUITY_BOUNDARY:")
                for issue in issues
            )
        ):
            self.reset("SOURCE_NOT_READY")
            return
        tick, seconds, session, player = (
            _value(sample.session.session_tick),
            _value(sample.session.session_time_s),
            _value(sample.session.session_num),
            _value(sample.opponents.player_car_idx),
        )
        incidents = _value(sample.incidents.player_car_my_incident_count)
        flags = _value(sample.flags.session_flags)
        progress = _progress(_value(sample.lap.laps_completed), _value(sample.lap.lap_distance_pct))
        if (
            not _int(tick)
            or not _int(session, 100_000)
            or not _int(player, 255)
            or type(seconds) not in (int, float)
            or not 0 <= seconds <= 10_000_000
            or frame.buffer_tick != tick
            or progress is None
            or not _int(incidents, 1_000_000)
            or not _int(flags, 2**32 - 1)
            or flags & (0x00330000 | 0x0008 | 0x0010 | 0x0100 | 0xC000)
            or _value(sample.pit.on_pit_road) is not False
            or _value(sample.flags.player_track_surface) != 3
        ):
            self.reset("PLAYER_MOTION_UNAVAILABLE")
            return
        opponents = sample.opponents
        if (
            opponents.presence is not Presence.PRESENT
            or opponents.provenance is not Provenance.SDK_DIRECT
            or opponents.issues
            or len(opponents.entries) >= MAX_CARS
        ):
            self.reset("OPPONENT_MOTION_UNAVAILABLE")
            return
        current, seen, excluded = {player: progress}, {player}, 0
        for row in opponents.entries:
            index = _value(row.car_idx, direct=False)
            if (
                not _int(index, 255)
                or index in seen
                or row.car_idx.provenance is not Provenance.DERIVED
            ):
                self.reset("OPPONENT_MOTION_UNAVAILABLE")
                return
            seen.add(index)
            pit, surface = _value(row.on_pit_road), _value(row.track_surface)
            if type(pit) is not bool or type(surface) is not int:
                self.reset("OPPONENT_MOTION_UNAVAILABLE")
                return
            if pit or surface != 3:
                excluded += 1
                continue
            other = _progress(_value(row.laps_completed), _value(row.lap_distance_pct))
            if other is None:
                self.reset("OPPONENT_MOTION_UNAVAILABLE")
                return
            current[index] = other
        now = round(seconds * 1e6)
        identity = (_value(sample.session.session_id, direct=False), session, player,
                    _value(sample.source.source_id, direct=False))
        if self.identity != identity or (
            self.previous is not None
            and (
                not 0 < tick - self.previous[0] <= self.tick_rate * 0.25
                or not 0 < now - self.previous[1] <= 250_000
                or incidents != self.previous[2]
            )
        ):
            self.reset("CONTINUITY_CHANGED")
        self.identity = identity
        if set(current) != set(self.actors) or excluded != self.excluded:
            self.revision += 1
        actors = {}
        for index, position in current.items():
            old = self.actors.get(index)
            if old is None:
                old = _Trace(position, now)
            elif old.advance(position, now):
                self.revision += 1
            actors[index] = old
        self.actors, self.excluded = actors, excluded
        self.previous, self.reason = (tick, now, incidents), "TWO_COMPLETE_LAPS_REQUIRED"

    def snapshot(self, monitor):
        result = unavailable_motion(monitor, self.reason, revision=self.revision)
        if not (
            source_ready(monitor)
            and self.previous is not None
            and self.previous[1] == monitor.get("session_time_us")
        ):
            return result
        result["excluded_count"] = self.excluded
        player = _map(monitor.get("telemetry")).get("player_car_idx")
        if player != self.identity[2]:
            return result
        rows = [
            _actor(index, trace.position, trace.crossings, trace.profiles, self.previous[1])
            for index, trace in sorted(self.actors.items())
        ]
        if any(row is None for row in rows):
            return result
        if len(rows) < 2:
            return {**result, "reason": "NO_ON_TRACK_OPPONENTS"}
        return {
            **result,
            "status": "READY",
            "reason": "EMPIRICAL_WHOLE_LAP_PACE",
            "player": next(row for row in rows if row["car_idx"] == player),
            "opponents": [row for row in rows if row["car_idx"] != player],
        }


def validated_motion(snapshot):
    value, monitor = snapshot.get("motion"), snapshot.get("monitor")
    if (
        not isinstance(value, Mapping)
        or not isinstance(monitor, Mapping)
        or not source_ready(monitor)
    ):
        return None
    if not (
        _int(value.get("revision"))
        and _int(value.get("excluded_count"), 255)
        and value.get("status") == "READY"
        and type(value.get("opponents")) is list
        and 1 <= len(value["opponents"]) <= 255
    ):
        return None
    rows = [value.get("player"), *value["opponents"]]
    if len(rows) + value["excluded_count"] > MAX_CARS:
        return None
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping) or not _int(row.get("car_idx"), 255):
            return None
        index, progress, windows = (
            row["car_idx"],
            row.get("progress_laps"),
            row.get("crossing_windows_us"),
        )
        if (
            index in seen
            or type(progress) not in (int, float)
            or not 0 <= progress < 1_000_001
            or type(windows) is not list
        ):
            return None
        seen.add(index)
        profiles = row.get("lap_profiles")
        if type(profiles) is not list:
            return None
        expected = _actor(index, progress, windows, profiles, monitor["session_time_us"])
        if expected is None or not _same_typed_tree(row, expected):
            return None
    telemetry = _map(monitor.get("telemetry"))
    player_progress = _progress(telemetry.get("laps_completed"), telemetry.get("lap_distance_pct"))
    if (
        player_progress is None
        or rows[0]["car_idx"] != telemetry.get("player_car_idx")
        or abs(rows[0]["progress_laps"] - player_progress) > 1e-8
        or [row["car_idx"] for row in rows[1:]] != sorted(seen - {rows[0]["car_idx"]})
    ):
        return None
    expected = {
        **unavailable_motion(monitor, revision=value["revision"]),
        "status": "READY",
        "reason": "EMPIRICAL_WHOLE_LAP_PACE",
        "player": rows[0],
        "opponents": rows[1:],
        "excluded_count": value["excluded_count"],
    }
    return value if _same_typed_tree(value, expected) else None
