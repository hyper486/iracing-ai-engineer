"""Private bounded trial journal; disk work never runs on SDK/audio owners.

This is a consistency/reproduction record, not authentication of the simulator
or proof that a human heard audio. No transcript, microphone PCM, credentials,
driver identity or free-text backend error belongs in this format.
"""

from __future__ import annotations

import copy
import math
import re
import threading
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .collector import JsonlHandleWriter, _canonical_json
from .desktop_settings import SettingsStore
from .live_tire_age import valid_confirmation_receipt
from .live_worker import FrameWorker, payload_size
from .runtime_clock import monotonic_now
from .spotter import SPOTTER_FIELDS, SpotterConfig

TRIAL_CONTRACT = "private-trial-audit-v2"
MAX_TRIAL_BYTES = 1024**3
MAX_ENTRY_BYTES = 64 * 1024
AUDIO_STATES = frozenset(("OFF", "PREPARING", "WAIT_DATA", "READY", "PLAYING", "PAUSED",
                          "ERROR", "STOPPING", "CLOSED"))
AUDIO_REASONS = frozenset((
    "DISABLED", "ZERO_VOLUME", "PHRASE_CACHE", "USER_PAUSED", "CONFIGURING", "CLOSING",
    "CLOSED", "NEED_FRESH_IN_CAR_DATA", "DETECTOR_READY", "PHRASE_CACHE_FAILED", "AUDIO_FAILED",
    "AUDIO_UNAVAILABLE", "AUDIO_BUSY", "AUDIO_INPUT_INVALID", "AUDIO_DEVICE_MISSING",
    "AUDIO_DEVICE_AMBIGUOUS", "AUDIO_RECORD_FAILED", "AUDIO_PLAY_FAILED", "AUDIO_INPUT_OVERFLOW",
    "AUDIO_OUTPUT_UNDERRUN", "AUDIO_TIMEOUT", "UNKNOWN",
))
AUDIO_KINDS = frozenset(("ALL_CLEAR", "CAR_LEFT", "CAR_RIGHT", "CARS_BOTH_SIDES",
                         "TWO_CARS_LEFT", "TWO_CARS_RIGHT", "STILL_LEFT", "STILL_RIGHT",
                         "STILL_BOTH", "DATA_LOST", "PTT_CANCELLED"))
AUDIO_OUTCOMES = frozenset(("HEALTH", "ATTEMPTED", "PLAYBACK_STARTED", "PLAYBACK_COMPLETED",
                            "CANCELLED", "DROPPED_BEFORE_START", "START_DEADLINE_MISSED",
                            "NOTICE_PREEMPTED", "FAILED", "PLAYBACK_ERROR"))
AUDIO_OUTPUTS = frozenset(("UNTESTED", "STARTED_NOT_HEARING_CONFIRMED",
                           "START_DEADLINE_MISSED", "FAILED"))
DETECTOR_REASONS = frozenset((
    "WAIT_SIM", "DISCONNECTED", "STOPPED", "ERROR", "NO_FRAME", "CLOCK_REGRESSION",
    "NO_FRESH_TICK", "FRAME_TIMESTAMP", "FIELD_READ_ERROR", "CORE_FIELD_INVALID",
    "TICK_MISMATCH", "SESSION_TIME_INVALID", "NOT_LIVE_SOURCE", "NOT_DRIVING_ON_TRACK",
    "PROXIMITY_FIELD_INVALID", "SDK_SPOTTER_OFF", "CONTEXT_CHANGED", "CONFLICTING_DUPLICATE",
    "TIMELINE_REGRESSION", "CAPTURE_TIME_NOT_PROGRESSING", "CONTINUITY_GAP",
    "NEED_PROGRESSING_TICKS", "PROXIMITY_OBSERVED",
))
_BOOL_FIELDS = frozenset(("IsOnTrack", "IsOnTrackCar", "IsReplayPlaying", "OnPitRoad",
                          "PlayerCarInPitStall"))
_INTEGER_FIELDS = frozenset(("SessionNum", "SessionTick", "PlayerCarIdx", "CarLeftRight"))


def _number(value):
    return type(value) in (int, float) and -2**53 <= value <= 2**53 and math.isfinite(value)


def validate_projection(lane, payload):
    """Allow only the fixed, numeric/enum diagnostic surface before persistence.

    Replay additionally validates types, continuity and detector recomputation.
    This projection gate prevents accidental future raw dictionary/text writes.
    """
    def keys(value, expected):
        if type(value) is not dict or set(value) != set(expected.split()):
            raise ValueError("TRIAL_PROJECTION_INVALID")

    def choice(value, allowed):
        if type(value) is not str or value not in allowed:
            raise ValueError("TRIAL_PROJECTION_INVALID")

    def numeric(*values):
        if any(value is not None and type(value) is not bool and not _number(value)
               for value in values):
            raise ValueError("TRIAL_PROJECTION_INVALID")

    if lane == "detector":
        if type(payload) is dict and payload.get("operation") == "RESET":
            keys(payload, "operation generation tick_rate_hz config")
            keys(payload["config"],
                 "max_age_s occupied_confirm_s clear_confirm_s repeat_s intent_ttl_s")
            numeric(payload["generation"], payload["tick_rate_hz"], *payload["config"].values())
            return
        keys(payload, "operation generation now prior_clock raised frame unavailable_status "
                      "decisions event_count")
        choice(payload["operation"], ("FEED", "TIME", "UNAVAILABLE"))
        numeric(*(payload[key] for key in ("generation", "now", "prior_clock", "raised",
                                          "event_count")))
        if payload["unavailable_status"] is not None:
            choice(payload["unavailable_status"], ("WAIT_SIM", "DISCONNECTED", "STOPPED", "ERROR"))
        if payload["frame"] is not None:
            frame = payload["frame"]
            keys(frame, "values buffer_tick captured_monotonic_s sim_mode read_errors")
            if type(frame["values"]) is not list or len(frame["values"]) != len(SPOTTER_FIELDS):
                raise ValueError("TRIAL_PROJECTION_INVALID")
            numeric(*frame["values"], frame["buffer_tick"], frame["captured_monotonic_s"])
            choice(frame["sim_mode"], ("full", "other"))
            if type(frame["read_errors"]) is not list or len(frame["read_errors"]) > 10:
                raise ValueError("TRIAL_PROJECTION_INVALID")
            for name in frame["read_errors"]:
                choice(name, SPOTTER_FIELDS)
        if type(payload["decisions"]) is not list or len(payload["decisions"]) > 8:
            raise ValueError("TRIAL_PROJECTION_INVALID")
        for row in payload["decisions"]:
            keys(row, "sequence epoch decision reason kind tick at_s audible")
            choice(row["decision"], ("HEALTH", "CANDIDATE", "SUPERSEDED", "INVALIDATED", "EXPIRED"))
            choice(row["reason"], DETECTOR_REASONS)
            if row["kind"] is not None:
                choice(row["kind"], AUDIO_KINDS)
            numeric(row["sequence"], row["epoch"], row["tick"], row["at_s"], row["audible"])
    elif lane == "audio":
        keys(payload, "sequence revision now outcome status reason output_status kind event_id "
                      "start_delay_ms enabled muted output_selection heard live_acceptance")
        for name, allowed in (("outcome", AUDIO_OUTCOMES), ("status", AUDIO_STATES),
                              ("reason", AUDIO_REASONS | AUDIO_KINDS),
                              ("output_status", AUDIO_OUTPUTS),
                              ("output_selection", ("default", "selected", "unset"))):
            choice(payload[name], allowed)
        if payload["kind"] is not None:
            choice(payload["kind"], AUDIO_KINDS)
        if payload["event_id"] is not None:
            if type(payload["event_id"]) is not list or len(payload["event_id"]) != 3:
                raise ValueError("TRIAL_PROJECTION_INVALID")
            numeric(*payload["event_id"])
        numeric(*(payload[key] for key in ("sequence", "revision", "now", "start_delay_ms",
                                          "enabled", "muted", "heard", "live_acceptance")))
    elif lane == "tire_confirmation":
        keys(payload, "generation assertion")
        if (type(payload["generation"]) is not int or not 0 <= payload["generation"] <= 2**53
                or not valid_confirmation_receipt(payload["assertion"])):
            raise ValueError("TRIAL_PROJECTION_INVALID")
    elif lane == "capture":
        keys(payload, "status generation capture_id bytes sha256")
        choice(payload["status"], ("OPEN", "COMPLETE", "INCOMPLETE", "EMPTY"))
        numeric(payload["generation"], payload["bytes"])
        for key, length in (("capture_id", 32), ("sha256", 64)):
            value = payload[key]
            if type(value) is not str or not re.fullmatch("[0-9a-f]{" + str(length) + "}", value):
                raise ValueError("TRIAL_PROJECTION_INVALID")
    else:
        raise ValueError("TRIAL_PROJECTION_INVALID")


def frame_projection(frame):
    """Preserve admission semantics while omitting arbitrary SDK/private values.

    Invalid typed fields become null; both fail the same detector admission.
    A numeric value outside this representable trace domain fails journaling,
    not the detector. It must not be silently relabeled replay-equivalent.
    """
    values = []
    for name in SPOTTER_FIELDS:
        value = frame.values.get(name)
        if name in _BOOL_FIELDS:
            value = value if type(value) is bool else None
        elif name in _INTEGER_FIELDS:
            if type(value) is int and abs(value) > 2**53:
                raise ValueError("TRIAL_FRAME_LIMIT")
            value = value if type(value) is int else None
        else:
            if type(value) in (int, float) and math.isfinite(value) and not _number(value):
                raise ValueError("TRIAL_FRAME_LIMIT")
            value = value if _number(value) else None
        values.append(value)
    captured = frame.captured_monotonic_s
    if type(captured) in (int, float) and math.isfinite(captured) and not _number(captured):
        raise ValueError("TRIAL_FRAME_LIMIT")
    buffer_tick = frame.buffer_tick
    if type(buffer_tick) is int and abs(buffer_tick) > 2**53:
        raise ValueError("TRIAL_FRAME_LIMIT")
    return {
        "values": values,
        "buffer_tick": buffer_tick if type(buffer_tick) is int else None,
        "captured_monotonic_s": captured if _number(captured) else None,
        "sim_mode": ("full" if type(frame.sim_mode_raw) is str
                     and frame.sim_mode_raw.casefold() == "full" else "other"),
        "read_errors": [name for name in SPOTTER_FIELDS if name in frame.read_errors],
    }


class _Journal:
    """The single file owner; reuse the guarded descriptor writer and its cap."""

    def __init__(self, directory, identifier, maximum, on_limit=lambda: None):
        store = SettingsStore(Path(directory))
        store._check_root(create=True)
        self._handle = (store.root / f"trial-{identifier}.jsonl").open("x+b", buffering=0)
        self._entries = 0
        self._on_limit = on_limit
        try:
            self._writer = JsonlHandleWriter(self._handle, fsync_each_record=False,
                                            max_output_bytes=maximum)
            self._writer.__enter__()
            self._write({
                "record": "header", "contract_version": TRIAL_CONTRACT, "run_id": identifier,
                "advisor_only": True, "executable": False, "live_acceptance": False,
                "heard": False, "source_authenticity": "UNVERIFIED",
            })
        except BaseException:
            self._handle.close()
            raise

    @property
    def byte_count(self):
        return self._writer.byte_size

    def _write(self, record):
        if len(_canonical_json(record)) + 1 > self._writer.max_output_bytes - self.byte_count:
            self._on_limit()
            raise ValueError("TRIAL_FILE_LIMIT")
        self._writer.write(record)

    def process(self, item):
        lane, payload = item
        self._write({"record": "entry", "sequence": self._entries + 1,
                     "lane": lane, "payload": payload})
        self._entries += 1

    def finish(self):
        self._write({
            "record": "footer", "entries": self._entries,
            "stream_sha256": self._writer.capture_sha256, "completion": "COMPLETE",
            "live_acceptance": False, "heard": False,
        })
        self._writer.close()

    def close(self):
        try:
            self._writer.close()
        finally:
            self._handle.close()


class TrialAudit:
    """Bounded nonblocking producer API; errors latch without affecting callers.

    Payloads are locally constructed projections, not user/provider inputs.
    Validation during replay still treats every byte of a journal as untrusted.
    """

    def __init__(self, directory, *, max_bytes=MAX_TRIAL_BYTES, clock=monotonic_now,
                 worker_factory=FrameWorker):
        if type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_TRIAL_BYTES:
            raise ValueError("TRIAL_BUDGET_INVALID")
        self._clock = clock
        self._maximum = max_bytes
        self._limit = threading.Event()
        self._id = uuid4().hex
        self._worker = worker_factory(lambda: _Journal(directory, self._id, max_bytes,
                                                       self._limit.set),
                                      name="private-trial-audit", max_frames=4096,
                                      max_bytes=32 * 1024**2, clock=clock)

    def offer(self, lane, payload):
        try:
            if lane not in ("detector", "audio", "capture", "tire_confirmation"):
                raise ValueError("TRIAL_LANE_INVALID")
            size = payload_size(payload, limit=MAX_ENTRY_BYTES)
            validate_projection(lane, payload)
            return self._worker.submit((lane, copy.deepcopy(payload)), size=size,
                                       observed_at=self._clock())
        except Exception:
            self.fail()
            return False

    def fail(self):
        self._worker.fail_payload()

    def snapshot(self):
        value = self._worker.snapshot()
        if self._limit.is_set():
            value.update(status="LIMIT_REACHED", reason="FILE_LIMIT", failed=True)
        return {**value, "contract_version": TRIAL_CONTRACT,
                "file_name": f"trial-{self._id}.jsonl", "heard": False,
                "live_acceptance": False, "max_file_bytes": self._maximum}

    def close(self, *, complete=True):
        self._worker.close(complete=complete)
        self._worker.join()


def detector_reset(generation, tick_rate, config=None):
    return {"operation": "RESET", "generation": generation, "tick_rate_hz": tick_rate,
            "config": asdict(config or SpotterConfig())}
