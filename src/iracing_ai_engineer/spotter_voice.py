"""Opt-in cached proximity audio, independent of STT, LLM and fuel readiness.

Playback receipts describe software/device calls, never confirmed human hearing.
No simulator commands, microphone access, network calls or raw telemetry writes.
"""

from __future__ import annotations

import copy
import io
import math
import sys
import threading
import wave
from array import array
from collections import Counter, deque
from contextlib import suppress

from .priority_audio import AudioPreempted
from .runtime_clock import monotonic_now
from .spotter import PHRASES, SPOTTER_CONTRACT_VERSION
from .trial_audit import AUDIO_KINDS, AUDIO_REASONS
from .voice_audio import AudioError, _decode_wav

_STATES = {
    "ALL_CLEAR": {1}, "CAR_LEFT": {2}, "CAR_RIGHT": {3}, "CARS_BOTH_SIDES": {4},
    "TWO_CARS_LEFT": {5}, "TWO_CARS_RIGHT": {6}, "STILL_LEFT": {2, 5},
    "STILL_RIGHT": {3, 6}, "STILL_BOTH": {4},
}
_NOTICES = {
    "PTT_CANCELLED": "刚才的提问已取消，请重新说。",
    "DATA_LOST": "近车提示暂不可用，请注意观察。",
}


def trim_phrase_silence(raw):
    """Trim only exact-zero PCM at both ends of locally generated fixed phrases.

    Keep 20 ms margins. Never remove internal pauses, nonzero consonants or
    arbitrary user's recordings; an entirely silent generated phrase is invalid.
    """
    pcm, rate, channels = _decode_wav(raw)
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    frames = len(samples) // channels
    start, end = 0, frames
    while start < end and not any(samples[start * channels:(start + 1) * channels]):
        start += 1
    if start == end:
        raise ValueError("SILENT_PHRASE")
    while end > start and not any(samples[(end - 1) * channels:end * channels]):
        end -= 1
    margin = round(rate * 0.02)
    start, end = max(0, start - margin), min(frames, end + margin)
    if start == 0 and end == frames:
        return raw
    target = io.BytesIO()
    with wave.open(target, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm[start * channels * 2:end * channels * 2])
    return target.getvalue()


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _context(snapshot):
    if type(snapshot) is not dict or snapshot.get("lifecycle") != "RUNNING":
        return None
    if snapshot.get("connection") != "CONNECTED" or snapshot.get("source_mode") != "LIVE":
        return None
    data = snapshot.get("spotter")
    generation = snapshot.get("generation")
    if (type(data) is not dict or data.get("contract_version") != SPOTTER_CONTRACT_VERSION
            or type(generation) is not int or generation < 0
            or type(data.get("epoch")) is not int or data["epoch"] < 0):
        return None
    return generation, data["epoch"]


def _ready(snapshot):
    if _context(snapshot) is None:
        return False
    data = snapshot["spotter"]
    age = data.get("updated_age_s")
    return (data.get("status") == "READY" and data.get("advisor_only") is True
            and _finite(age) and 0 <= age <= 0.25
            and type(data.get("car_left_right")) is int
            and data["car_left_right"] in range(1, 7))


def _candidate(snapshot):
    if not _ready(snapshot):
        return None
    data = snapshot["spotter"]
    item = data.get("candidate")
    if type(item) is not dict:
        return None
    kind, sequence, ttl = item.get("kind"), item.get("sequence"), item.get("expires_in_s")
    state = item.get("state")
    if (type(kind) is not str or kind not in _STATES
            or type(sequence) is not int or sequence < 1
            or type(item.get("epoch")) is not int or item["epoch"] != data["epoch"]
            or type(state) is not int or state not in _STATES[kind]
            or state != data["car_left_right"] or not _finite(ttl) or not 0 < ttl <= 1):
        return None
    return {"id": (*_context(snapshot), sequence), "kind": kind, "state": state, "ttl": ttl}


class SpotterVoice:
    """One cache/playback owner and a separate 20 ms cancellation guard.

    Only fixed phrases enter the cache; source-provided text is never spoken.
    Input settings do not imply consent: spotter_enabled is independent, off by
    default, and existing v1 voice preferences migrate with it disabled.
    """

    def __init__(self, source, audio, *, speech=None, clock=monotonic_now, audit_sink=None):
        if speech is None:
            from .voice_windows import WindowsSpeech
            speech = WindowsSpeech()
        self._source, self._audio, self._speech, self._clock = source, audio, speech, clock
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._shutdown, self._wake = threading.Event(), threading.Event()
        self._worker = self._guard_worker = None
        self._revision = 0
        self._settings = None
        self._cache = None
        self._active = None
        self._seen = None
        self._notice_pending = False
        self._notice_serial = 0
        self._clear_since = self._lost_since = None
        self._armed = self._loss_reported = False
        self._last_loss_notice = -math.inf
        self._status, self._reason = "OFF", "DISABLED"
        self._output = "UNTESTED"
        self._audit = deque(maxlen=128)
        self._counts = Counter()
        self._origin = self._clock()
        self._audit_sink, self._trace_serial = audit_sink, 0

    def _trace(self, outcome="HEALTH", event=None, *, start_delay_ms=None, reason=None):
        if self._audit_sink is None:
            return
        self._trace_serial += 1
        settings = self._settings or {}
        reason = self._reason if reason is None else reason
        # Never pass device names, voice names, settings, PCM, or exception text.
        with suppress(Exception):
            self._audit_sink({
                "sequence": self._trace_serial, "revision": self._revision, "now": self._clock(),
                "outcome": outcome, "status": self._status,
                "reason": reason if reason in AUDIO_REASONS | AUDIO_KINDS else "UNKNOWN",
                "output_status": self._output,
                "kind": None if event is None else event["kind"],
                "event_id": None if event is None else list(event["id"]),
                "start_delay_ms": start_delay_ms,
                "enabled": settings.get("spotter_enabled") is True,
                "muted": settings.get("volume", 0) <= 0,
                "output_selection": ("unset" if not settings else "default"
                                     if settings.get("output_device") == "default" else "selected"),
                "heard": False, "live_acceptance": False,
            })

    def trace_state(self):
        """Bind current audio health at the start of a new private trial segment."""
        with self._lock:
            self._trace()

    def start(self):
        with self._lock:
            if self._worker is not None or self._shutdown.is_set():
                return
            self._worker = threading.Thread(target=self._run, name="proximity-audio", daemon=True)
            self._guard_worker = threading.Thread(
                target=self._guard, name="proximity-audio-guard", daemon=True,
            )
            self._worker.start()
            self._guard_worker.start()

    def _record(self, outcome, event=None, *, start_delay_ms=None):
        self._counts[outcome] += 1
        self._audit.append({
            "outcome": outcome, "kind": None if event is None else event["kind"],
            "event_id": None if event is None else list(event["id"]),
            "at_s": round(max(0, self._clock() - self._origin), 6),
            "heard": False, "live_acceptance": False,
            "start_delay_ms": start_delay_ms,
        })
        self._trace(outcome, event, start_delay_ms=start_delay_ms)

    def _cancel_locked(self):
        if self._active is not None:
            self._active["cancel"].set()

    def configure(self, settings):
        with self._lock:
            if self._shutdown.is_set():
                return
            self._cancel_locked()
            self._revision += 1
            self._settings = {
                key: settings[key]
                for key in ("spotter_enabled", "voice", "output_device", "volume")
            }
            self._cache, self._seen = None, None
            self._clear_since = self._lost_since = None
            self._armed = self._loss_reported = self._notice_pending = False
            self._output = "UNTESTED"
            if not settings["spotter_enabled"]:
                self._status, self._reason = "OFF", "DISABLED"
            elif settings["volume"] <= 0:
                self._status, self._reason = "PAUSED", "ZERO_VOLUME"
            else:
                self._status, self._reason = "PREPARING", "PHRASE_CACHE"
            self._trace()
            self._wake.set()

    def suspend(self, reason="USER_PAUSED"):
        with self._lock:
            self._cancel_locked()
            self._revision += 1
            self._settings = self._cache = None
            self._notice_pending = False
            self._status, self._reason = "PAUSED", reason
            self._trace()
            self._wake.set()

    def interrupted_question(self):
        with self._lock:
            self._notice_pending = True
            self._notice_serial += 1
            self._clear_since = None
            self._wake.set()

    def snapshot(self):
        with self._lock:
            return {
                "status": self._status, "reason": self._reason,
                "output_status": self._output, "cached_phrases": len(self._cache or {}),
                "counts": dict(self._counts), "audit": copy.deepcopy(list(self._audit)),
                "heard": False, "live_acceptance": False,
            }

    def _current(self, revision):
        return not self._shutdown.is_set() and revision == self._revision

    def _prepare(self, revision, settings):
        # Prime the lazy audio import and device catalog before declaring the
        # phrase lane ready. Cold enumeration must not consume the first alert's
        # start deadline. Enumeration alone is not a playback/hearing test.
        catalog = self._audio.devices()
        outputs = catalog.get("outputs", [])
        selector = settings["output_device"]
        if not outputs or (selector != "default"
                           and sum(item.get("id") == selector for item in outputs) != 1):
            raise AudioError("AUDIO_DEVICE_MISSING")
        cache = {}
        for kind, text in {**PHRASES, **_NOTICES}.items():
            if not self._current(revision):
                return
            raw = trim_phrase_silence(self._speech.synthesize(
                text, voice=settings["voice"], rate=0 if kind in _NOTICES else 3,
            ))
            pcm, rate, channels = _decode_wav(raw)
            limit = 6 if kind in _NOTICES else 3
            if not any(pcm) or len(pcm) > limit * rate * channels * 2:
                raise ValueError("PHRASE_TOO_LONG")
            cache[kind] = raw
            if sum(map(len, cache.values())) > 2 * 1024**2:
                raise ValueError("PHRASE_CACHE_TOO_LARGE")
        with self._lock:
            if self._current(revision):
                self._cache = cache
                self._status, self._reason = "WAIT_DATA", "NEED_FRESH_IN_CAR_DATA"
                self._trace()

    def _valid_event(self, revision, event, *, starting):
        if not self._current(revision):
            return False
        snapshot = self._source()
        if not _ready(snapshot) or _context(snapshot) != event["id"][:2]:
            return False
        if snapshot["spotter"]["car_left_right"] != event["state"]:
            return False
        if not starting:
            # TTL is a start-by deadline. A still-supported short phrase can
            # finish after it, but never after fresh occupancy/context changes.
            return True
        current = _candidate(snapshot)
        return current is not None and current["id"] == event["id"]

    def _play(self, revision, settings, event, guard, *, urgent=True):
        cancel = threading.Event()
        playback = {"cancel": cancel, "guard": guard, "started": False}
        with self._lock:
            if not self._current(revision):
                return
            self._active = playback
            raw = self._cache[event["kind"]]
            self._status, self._reason = "PLAYING", event["kind"]
            attempted_at = self._clock()
            self._record("ATTEMPTED", event)

        def started():
            with self._lock:
                playback["started"] = True
                self._output = "STARTED_NOT_HEARING_CONFIRMED"
                self._record("PLAYBACK_STARTED", event,
                             start_delay_ms=round(max(0, self._clock() - attempted_at) * 1000, 1))

        def can_start():
            return (not cancel.is_set() and self._current(revision)
                    and self._clock() < event["deadline"] and guard(True))

        try:
            if urgent:
                self._audio.urgent(
                    raw, cancel, deadline=event["deadline"], guard=lambda: guard(True),
                    on_started=started, device=settings["output_device"], volume=settings["volume"],
                )
            elif can_start():
                self._audio.background_play(
                    raw, cancel, start_guard=can_start, on_started=started,
                    device=settings["output_device"], volume=settings["volume"],
                )
            with self._lock:
                outcome = ("PLAYBACK_COMPLETED" if playback["started"] and not cancel.is_set()
                           else "CANCELLED" if playback["started"] else "DROPPED_BEFORE_START")
                self._record(outcome, event)
            if (not playback["started"] and self._clock() >= event["deadline"]
                    and self._current(revision) and guard(False)):
                with self._lock:
                    if self._current(revision):
                        self._output = "START_DEADLINE_MISSED"
                        self._record("START_DEADLINE_MISSED", event)
            return outcome == "PLAYBACK_COMPLETED"
        except AudioPreempted:
            with self._lock:
                self._record("NOTICE_PREEMPTED", event)
                if (self._current(revision)
                        and event.get("notice_serial") == self._notice_serial):
                    self._notice_pending = False
            raise
        except Exception as error:
            with self._lock:
                self._trace("PLAYBACK_ERROR", event,
                            reason=error.code if isinstance(error, AudioError) else "AUDIO_FAILED")
            raise
        finally:
            with self._lock:
                if self._active is playback:
                    self._active = None
                if self._current(revision) and self._status == "PLAYING":
                    self._status, self._reason = "WAIT_DATA", "NEED_FRESH_IN_CAR_DATA"
                    self._trace()

    def _step(self, revision, settings):
        snapshot = self._source()
        now = self._clock()
        with self._lock:
            if not self._current(revision):
                return
            event = self._choose(snapshot, now)
        if event is None:
            return

        def guard(starting):
            if event["kind"] == "DATA_LOST":
                latest = self._source()
                return (self._current(revision) and latest.get("lifecycle") == "RUNNING"
                        and latest.get("spotter", {}).get("status") in (
                            "STALE", "UNAVAILABLE", "ERROR", "DISCONNECTED"))
            return self._valid_event(revision, event,
                                     starting=starting and event["kind"] != "PTT_CANCELLED")

        complete = self._play(revision, settings, event, guard,
                              urgent=event["kind"] != "PTT_CANCELLED")
        if complete and event["kind"] == "PTT_CANCELLED":
            with self._lock:
                if (self._current(revision)
                        and event["notice_serial"] == self._notice_serial):
                    self._notice_pending = False

    def _choose(self, snapshot, now):
        """Called under the state lock; never performs device I/O or synthesis."""
        ready = _ready(snapshot)
        event = _candidate(snapshot)
        previous = self._status, self._reason
        self._status = "READY" if ready else "WAIT_DATA"
        self._reason = "DETECTOR_READY" if ready else "NEED_FRESH_IN_CAR_DATA"
        if previous != (self._status, self._reason):
            self._trace()
        if ready:
            self._armed, self._lost_since, self._loss_reported = True, None, False
        else:
            self._clear_since = None
            status = snapshot.get("spotter", {}).get("status") if type(snapshot) is dict else None
            if self._armed and status in ("STALE", "UNAVAILABLE", "ERROR", "DISCONNECTED"):
                if self._lost_since is None:
                    self._lost_since = now
                if now - self._lost_since >= 1 and not self._loss_reported:
                    self._loss_reported = True
                    if now - self._last_loss_notice < 30:
                        return
                    self._last_loss_notice = now
                    return {"id": (-1, -1, 0), "kind": "DATA_LOST", "deadline": now + 0.25}
            else:
                self._armed, self._lost_since = False, None
            return
        if event is not None and event["id"] != self._seen:
            self._seen = event["id"]
            event["deadline"] = now + min(0.25, event["ttl"])
            return event
        if snapshot["spotter"]["car_left_right"] != 1:
            self._clear_since = None
        elif self._notice_pending:
            if self._clear_since is None:
                self._clear_since = now
            if now - self._clear_since >= 1:
                return {"id": (*_context(snapshot), 0), "kind": "PTT_CANCELLED",
                        "state": 1, "deadline": now + 0.25, "notice_serial": self._notice_serial}
        return None

    def _run(self):
        while not self._shutdown.is_set():
            with self._lock:
                revision, settings, cache = self._revision, self._settings, self._cache
                allowed = self._status not in ("OFF", "PAUSED", "ERROR", "STOPPING", "CLOSED")
            try:
                if allowed and settings is not None:
                    if cache is None:
                        self._prepare(revision, settings)
                    else:
                        self._step(revision, settings)
            except AudioPreempted:
                # A deferred PTT notice can wait for the current microphone owner.
                pass
            except Exception as error:
                with self._lock:
                    if self._current(revision):
                        self._status = "ERROR"
                        self._reason = (error.code if isinstance(error, AudioError)
                                        else "PHRASE_CACHE_FAILED" if cache is None
                                        else "AUDIO_FAILED")
                        self._output = "FAILED"
                        self._record("FAILED")
            self._wake.wait(0.02)
            self._wake.clear()

    def _guard(self):
        while not self._shutdown.wait(0.02):
            with self._lock:
                playback = self._active
            if playback is not None:
                try:
                    valid = playback["guard"](not playback["started"])
                except Exception:
                    valid = False
                if not valid:
                    playback["cancel"].set()

    def close(self):
        with self._close_lock:
            self._shutdown.set()
            self.suspend("CLOSING")
            with self._lock:
                self._status = "STOPPING"
            for worker in (self._worker, self._guard_worker):
                if worker is not None:
                    worker.join()
            with self._lock:
                self._status, self._reason = "CLOSED", "CLOSED"
                self._trace()
