"""Bound a sealed private assertion journal to one exact capture, then recompute.

This reuses the live tire owner under the capture replay's independent segments.
Original scheduling/revisions and actual service contents are not certified.
Only bounded fixed historical state rows escape this internal join.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace

from .live_tire_age import TireConfirmation
from .tire_frame_binding import tire_frame_anchor
from .trial_replay import _replay

MAX_ASSERTIONS = 1024
MAX_CAPTURES = 128
MAX_STATES = 128
ERRORS = frozenset(("TIRE_JOURNAL_INCOMPLETE", "TIRE_CAPTURE_NOT_LINKED", "TIRE_ANCHOR_REQUIRED",
                    "TIRE_ASSERTION_UNMATCHED", "TIRE_ASSERTION_AMBIGUOUS", "TIRE_REPLAY_LIMIT"))
_REASONS = frozenset((
    "SOURCE_NOT_READY", "REQUIRED_DATA_UNAVAILABLE", "CONTINUITY_CHANGED", "SOURCE_STALE",
    "INSTALLATION_NOT_CONFIRMED", "TIRE_CONTEXT_CHANGED", "SERVICE_NOT_CONFIRMED",
    "SERVICE_CHANGED_AFTER_CONFIRMATION", "DRIVER_CONFIRMED_OBSERVATION",
    "INSTALLATION_UNKNOWN", "AWAITING_OBSERVED_EXIT", "TIRE_PROCESSING_ERROR",
    "REPLAY_ASSERTION_NOT_ADMITTED", "CAPTURE_SEGMENT_BOUNDARY",
))


class TireReplayError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in ERRORS else "TIRE_CAPTURE_NOT_LINKED"
        super().__init__(self.code)


class TireReplayJoin:
    """Internal bounded plan; never expose visitor data before the journal seal."""

    def __init__(self, path, cancelled):
        self._captures, self._assertions = [], []
        self._pending, self._clocks, self._seen = {}, set(), set()
        self.states = deque(maxlen=MAX_STATES)
        self.state_count = self.applied = self.rejected = self.rebased = 0
        self.last_state = None
        report = _replay(path, None, cancelled, entry_visitor=self._visit)
        if report["complete"] is not True:
            raise TireReplayError("TIRE_JOURNAL_INCOMPLETE")

    def _visit(self, lane, payload):
        if lane == "capture" and payload["status"] == "COMPLETE":
            if len(self._captures) >= MAX_CAPTURES:
                raise TireReplayError("TIRE_REPLAY_LIMIT")
            self._captures.append(payload)
        elif lane == "tire_confirmation":
            if len(self._assertions) >= MAX_ASSERTIONS:
                raise TireReplayError("TIRE_REPLAY_LIMIT")
            self._assertions.append(payload)

    def select(self, digest, size):
        matches = [row for row in self._captures
                   if row["sha256"] == digest and row["bytes"] == size]
        if len(matches) != 1:
            raise TireReplayError("TIRE_CAPTURE_NOT_LINKED")
        generation = matches[0]["generation"]
        for row in self._assertions:
            if row["generation"] != generation:
                continue
            anchor = row.get("anchor")
            if anchor is None:
                raise TireReplayError("TIRE_ANCHOR_REQUIRED")
            key = (anchor["capture_monotonic_us"], anchor["frame_sha256"])
            if key in self._pending:
                raise TireReplayError("TIRE_ASSERTION_AMBIGUOUS")
            self._pending[key] = row["assertion"]
            self._clocks.add(key[0])
        self._captures.clear()
        self._assertions.clear()

    def after_frame(self, frame, captured_us, model, ordinal, segment):
        owner = model._tire_age
        if captured_us in self._clocks:
            anchor = tire_frame_anchor(frame, capture_monotonic_us=captured_us)
            key = (captured_us, anchor["frame_sha256"])
            if key in self._pending:
                if key in self._seen:
                    raise TireReplayError("TIRE_ASSERTION_AMBIGUOUS")
                self._seen.add(key)
                row = self._pending[key]
                command = TireConfirmation(**{
                    name: row[name] for name in TireConfirmation.__dataclass_fields__})
                point = owner.previous if owner is not None else None
                matches = point is not None and all(point[key] == row[target]
                    for key, target in (("tick", "decision_tick"), ("laps", "laps_completed"),
                                        ("session_num", "session_num"),
                                        ("player_car_idx", "player_car_idx")))
                receipt = None
                if matches:
                    # Recompute under the current owner's segment/revision,
                    # never impersonate the original UI/scheduling history.
                    revised = replace(command, revision=owner.revision)
                    receipt = owner.confirm(revised)
                    if receipt is not None and revised.revision != command.revision:
                        self.rebased += 1
                if receipt is None:
                    self.rejected += 1
                    if owner is not None:
                        # An unadmitted new/partial assertion must not leave an
                        # older origin in force past that recorded service.
                        owner.reset("REPLAY_ASSERTION_NOT_ADMITTED")
                else:
                    self.applied += 1
        self.observe(owner, ordinal, segment)

    def _append(self, row, signature):
        if signature != self.last_state:
            self.states.append(row)
            self.state_count += 1
            self.last_state = signature

    def boundary(self, ordinal, segment):
        if not self.states:
            return
        self._append({"frame": ordinal, "segment": segment, "lap_completed": None,
                      "state": "UNKNOWN", "reason": "CAPTURE_SEGMENT_BOUNDARY",
                      "counter_increase": None}, (segment, "BOUNDARY", ordinal))

    def observe(self, owner, ordinal, segment):
        point = owner.previous if owner is not None else None
        origin = owner.origin if owner is not None else None
        known = point is not None and not point["pit"] and origin is not None
        count = point["laps"] - origin["exit_laps"] if known else None
        reason = owner.reason if owner is not None else "TIRE_PROCESSING_ERROR"
        reason = reason if reason in _REASONS else "SOURCE_NOT_READY"
        state = "DRIVER_CONFIRMED_COUNTER" if known else "SUSPENDED_IN_PIT" if (
            point is not None and point["pit"]) else "UNKNOWN"
        revision = owner.revision if owner is not None else None
        self._append({"frame": ordinal, "segment": segment,
                      "lap_completed": point["laps"] if point is not None else None,
                      "state": state, "reason": reason, "counter_increase": count},
                     (segment, revision, state, reason, count))

    def finish(self):
        if len(self._seen) != len(self._pending):
            raise TireReplayError("TIRE_ASSERTION_UNMATCHED")
        return {
            "contract_version": "tire-capture-replay-v1", "capture_bytes_linked": True,
            "status": "RECOMPUTED_DRIVER_ASSERTIONS" if self._pending else "NO_ASSERTIONS",
            "assertions": len(self._pending), "applied": self.applied, "rejected": self.rejected,
            "current_owner_revision_substitutions": self.rebased,
            "states": list(self.states), "state_count": self.state_count,
            "evicted_states": max(0, self.state_count - len(self.states)),
            "original_scheduling_reproduced": False, "tire_service_verified": False,
            "physical_wear_available": False, "live_acceptance": False,
        }
