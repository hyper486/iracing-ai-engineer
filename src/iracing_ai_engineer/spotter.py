"""Independent tick-level proximity decisions; no audio or simulator controls.

Only the SDK's CarLeftRight enum is interpreted. Fuel, opponent lap times and
lap-distance arrays are deliberately not prerequisites. Decisions are NOT
playback receipts and this module never promotes a live acceptance gate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter, deque
from dataclasses import asdict, dataclass
from itertools import islice

from .sdk_probe import RawSdkFrame

SPOTTER_CONTRACT_VERSION = "proximity-decisions-v1"
SPOTTER_FIELDS = (
    "SessionNum", "SessionTick", "SessionTime", "PlayerCarIdx", "CarLeftRight",
    "IsOnTrack", "IsOnTrackCar", "IsReplayPlaying", "OnPitRoad", "PlayerCarInPitStall",
)

# Public SDK enum meanings (pyirsdk 1.3.6); no upstream application logic/assets.
_KINDS = {
    1: "ALL_CLEAR", 2: "CAR_LEFT", 3: "CAR_RIGHT", 4: "CARS_BOTH_SIDES",
    5: "TWO_CARS_LEFT", 6: "TWO_CARS_RIGHT",
}
PHRASES = {
    "ALL_CLEAR": "两侧已清空。", "CAR_LEFT": "左侧有车。", "CAR_RIGHT": "右侧有车。",
    "CARS_BOTH_SIDES": "两侧都有车。", "TWO_CARS_LEFT": "左侧有两辆车。",
    "TWO_CARS_RIGHT": "右侧有两辆车。", "STILL_LEFT": "左侧仍有车。",
    "STILL_RIGHT": "右侧仍有车。", "STILL_BOTH": "两侧仍有车。",
}


def _finite(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True, slots=True)
class SpotterConfig:
    max_age_s: float = 0.25
    occupied_confirm_s: float = 0.03
    clear_confirm_s: float = 0.15
    repeat_s: float = 5.0
    intent_ttl_s: float = 0.75

    def __post_init__(self):
        bounds = {
            "max_age_s": (0.05, 0.5), "occupied_confirm_s": (0.0, 0.1),
            "clear_confirm_s": (0.05, 0.5), "repeat_s": (2.0, 30.0),
            "intent_ttl_s": (0.05, 1.0),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if not _finite(value) or not low <= value <= high:
                raise ValueError("INVALID_SPOTTER_CONFIG")


class ProximitySpotter:
    """Single-owner deterministic state machine with a bounded, nonsecret audit.

    Consumers must revalidate snapshot readiness/epoch/candidate immediately
    before output. The latest candidate is a superseding mailbox, not a queue
    of past instructions. Duplicate ticks never refresh the freshness clock.
    """

    def __init__(self, *, tick_rate_hz: int = 60, config: SpotterConfig | None = None):
        if type(tick_rate_hz) is not int or not 1 <= tick_rate_hz <= 360:
            raise ValueError("INVALID_SPOTTER_TICK_RATE")
        self.config = config or SpotterConfig()
        self.tick_rate_hz = tick_rate_hz
        self._status, self._reason = "WAIT_SIM", "NO_FRAME"
        self._origin = self._now = self._progress_at = None
        self._identity = self._last = None
        self._stable = self._pending = None
        self._pending_since = None
        self._intent = None
        self._last_intent_at = -math.inf
        self._epoch = self._serial = self._audit_serial = 0
        self._counts = Counter()
        self._audit = deque(maxlen=128)
        self._digest = hashlib.sha256()
        self._digest.update(json.dumps({
            "contract": SPOTTER_CONTRACT_VERSION, "config": asdict(self.config),
            "tick_rate_hz": tick_rate_hz,
        }, sort_keys=True, separators=(",", ":")).encode())

    def _record(self, decision: str, *, kind: str | None = None, tick: int | None = None):
        self._audit_serial += 1
        row = {
            "sequence": self._audit_serial, "epoch": self._epoch, "decision": decision,
            "reason": self._reason, "kind": kind, "tick": tick,
            "at_s": round(max(0.0, (self._now or 0) - (self._origin or 0)), 6),
            "audible": False,
        }
        self._digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
        self._audit.append(row)
        self._counts[decision] += 1

    def _health(self, status: str, reason: str):
        if (status, reason) != (self._status, self._reason):
            self._status, self._reason = status, reason
            self._record("HEALTH")

    def _withdraw(self, decision: str):
        if self._intent is not None:
            self._record(decision, kind=self._intent["kind"], tick=self._intent["tick"])
            self._intent = None

    def _lose(self, status: str, reason: str):
        self._health(status, reason)
        self._withdraw("INVALIDATED")
        if self._last is not None or self._stable is not None:
            self._epoch += 1
        self._last = self._stable = self._pending = self._pending_since = None
        self._progress_at = None

    def _clock(self, now: float) -> bool:
        if not _finite(now) or now < 0:
            raise ValueError("INVALID_SPOTTER_CLOCK")
        if self._origin is None:
            self._origin = now
        if self._now is not None and now < self._now:
            self._lose("ERROR", "CLOCK_REGRESSION")
            # Preserve the high-water clock: old samples cannot re-arm the model.
            return False
        self._now = now
        return True

    def unavailable(self, status: str, *, now: float):
        """Fixed public reasons only; exception strings/paths never enter audit."""
        if status not in ("WAIT_SIM", "DISCONNECTED", "STOPPED", "ERROR"):
            raise ValueError("INVALID_SPOTTER_STATUS")
        if self._clock(now):
            self._lose(status, status)

    def advance_time(self, now: float):
        if not self._clock(now):
            return
        if self._progress_at is not None and now - self._progress_at > self.config.max_age_s:
            self._lose("STALE", "NO_FRESH_TICK")
        if self._intent is not None and now >= self._intent["deadline"]:
            self._withdraw("EXPIRED")

    def _admission(self, frame: RawSdkFrame, now: float) -> tuple[str, str] | None:
        values = frame.values
        captured = frame.captured_monotonic_s
        if not _finite(captured) or not 0 <= now - captured <= self.config.max_age_s:
            return "STALE", "FRAME_TIMESTAMP"
        if any(name in frame.read_errors for name in SPOTTER_FIELDS):
            return "UNAVAILABLE", "FIELD_READ_ERROR"
        for name, maximum in (("SessionNum", 1_000_000), ("PlayerCarIdx", 4095),
                              ("SessionTick", 2**31 - 1)):
            value = values.get(name)
            if type(value) is not int or not 0 <= value <= maximum:
                return "UNAVAILABLE", "CORE_FIELD_INVALID"
        # The publication-buffer counter and the payload's session counter
        # have independent origins. A stable frozen SDK read does not imply
        # equal absolute values; require each clock to progress below instead.
        if type(frame.buffer_tick) is not int or not 0 <= frame.buffer_tick <= 2**31 - 1:
            return "UNAVAILABLE", "CORE_FIELD_INVALID"
        session_time = values.get("SessionTime")
        if not _finite(session_time) or session_time < 0:
            return "UNAVAILABLE", "SESSION_TIME_INVALID"
        if type(frame.sim_mode_raw) is not str or frame.sim_mode_raw.casefold() != "full":
            return "WAIT_CAR", "NOT_LIVE_SOURCE"
        expected = {
            "IsOnTrack": True, "IsOnTrackCar": True, "IsReplayPlaying": False,
            "OnPitRoad": False, "PlayerCarInPitStall": False,
        }
        if any(values.get(key) is not value for key, value in expected.items()):
            return "WAIT_CAR", "NOT_DRIVING_ON_TRACK"
        code = values.get("CarLeftRight")
        if type(code) is not int or code not in range(7):
            return "UNAVAILABLE", "PROXIMITY_FIELD_INVALID"
        if code == 0:
            return "UNAVAILABLE", "SDK_SPOTTER_OFF"
        return None

    def feed(self, frame: RawSdkFrame, *, now: float):
        if not self._clock(now):
            return
        self._counts["FRAMES"] += 1
        refused = self._admission(frame, now)
        if refused:
            self._lose(*refused)
            return
        values = frame.values
        identity = values["SessionNum"], values["PlayerCarIdx"]
        current = (values["SessionTick"], values["SessionTime"], values["CarLeftRight"],
                   frame.buffer_tick)
        tick, session_time, code, buffer_tick = current
        if self._identity is not None and identity != self._identity:
            self._lose("ACQUIRING", "CONTEXT_CHANGED")
        self._identity = identity
        if self._last is not None:
            old_tick, old_time, _, old_buffer_tick = self._last
            if tick == old_tick or buffer_tick == old_buffer_tick:
                self._counts["DUPLICATES"] += 1
                if current != self._last:
                    self._lose("UNAVAILABLE", "CONFLICTING_DUPLICATE")
                else:
                    self.advance_time(now)
                return
            if tick < old_tick or buffer_tick < old_buffer_tick or session_time <= old_time:
                self._lose("ACQUIRING", "TIMELINE_REGRESSION")
            elif frame.captured_monotonic_s <= self._progress_at:
                self._lose("ACQUIRING", "CAPTURE_TIME_NOT_PROGRESSING")
            elif (now - self._progress_at > self.config.max_age_s
                  or session_time - old_time > self.config.max_age_s
                  or (tick - old_tick) / self.tick_rate_hz > self.config.max_age_s
                  or (buffer_tick - old_buffer_tick) / self.tick_rate_hz > self.config.max_age_s):
                self._lose("ACQUIRING", "CONTINUITY_GAP")
        if self._last is None:
            self._last, self._progress_at = current, frame.captured_monotonic_s
            self._pending, self._pending_since = code, session_time
            self._health("ACQUIRING", "NEED_PROGRESSING_TICKS")
            return
        self._last, self._progress_at = current, frame.captured_monotonic_s
        self._health("READY", "PROXIMITY_OBSERVED")
        if self._intent is not None and code != self._intent["state"]:
            self._withdraw("SUPERSEDED")
        if code != self._pending:
            self._pending, self._pending_since = code, session_time
        confirmed = session_time - self._pending_since
        delay = self.config.clear_confirm_s if code == 1 else self.config.occupied_confirm_s
        if code != self._stable and confirmed + 1e-9 >= delay:
            previous, self._stable = self._stable, code
            # Starting/recovering on an empty track is not a "clear" transition.
            if code != 1 or previous is not None:
                self._emit(_KINDS[code], code, tick, now)
        elif (code == self._stable and code != 1
              and now - self._last_intent_at >= self.config.repeat_s):
            kind = "STILL_BOTH" if code == 4 else "STILL_LEFT" if code in (2, 5) else "STILL_RIGHT"
            self._emit(kind, code, tick, now)
        self.advance_time(now)

    def _emit(self, kind: str, state: int, tick: int, now: float):
        self._withdraw("SUPERSEDED")
        self._serial += 1
        self._intent = {
            "sequence": self._serial, "epoch": self._epoch, "kind": kind,
            "state": state, "tick": tick, "text": PHRASES[kind], "priority": "PROXIMITY",
            "created_at": now, "deadline": now + self.config.intent_ttl_s,
        }
        self._last_intent_at = now
        self._record("CANDIDATE", kind=kind, tick=tick)

    def snapshot(self, *, now: float) -> dict:
        self.advance_time(now)
        intent = copy.deepcopy(self._intent)
        if intent is not None:
            intent["expires_in_s"] = max(0.0, intent.pop("deadline") - now)
            intent["age_s"] = max(0.0, now - intent.pop("created_at"))
        return {
            "contract_version": SPOTTER_CONTRACT_VERSION, "status": self._status,
            "reason": self._reason, "epoch": self._epoch, "candidate": intent,
            "car_left_right": None if self._last is None else self._last[2],
            "updated_age_s": None if self._progress_at is None else now - self._progress_at,
            "event_count": self._serial, "audit_count": self._audit_serial,
            "audit_sha256": self._digest.hexdigest(), "counts": dict(self._counts),
            "advisor_only": True, "audible": False, "live_acceptance": False,
            "audio_status": "NOT_CONNECTED",
            "config": asdict(self.config), "tick_rate_hz": self.tick_rate_hz,
        }

    def audit(self) -> list[dict]:
        return copy.deepcopy(list(self._audit))

    def trace_cursor(self):
        """Single-owner journal cursor; does not advance detector time."""
        return self._audit_serial, self._now, self._serial

    def audit_since(self, sequence):
        if type(sequence) is not int:
            raise ValueError("SPOTTER_AUDIT_CURSOR_GAP")
        count = self._audit_serial - sequence
        if not 0 <= count <= len(self._audit):
            raise ValueError("SPOTTER_AUDIT_CURSOR_GAP")
        return copy.deepcopy(list(islice(self._audit, len(self._audit) - count, None)))
