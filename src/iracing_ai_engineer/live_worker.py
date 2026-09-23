"""Bounded single-owner work lanes; slow sinks never run on the SDK reader.

Queue failure is terminal for the lane. No silent oldest-frame eviction, false
COMPLETE, retry storm, or replacement thread while its predecessor still runs.
This isolates blocking I/O, not the Python GIL or a hung native SDK operation.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from contextlib import suppress

from .runtime_clock import monotonic_now


def payload_size(value, *, limit=16 * 1024**2):
    """Conservative retained JSON-object size, without encoding/copying telemetry.

    Only frozen transport-owned JSON-shaped inputs are admitted. Generous Python
    object overhead and UTF-32 string accounting bound the retained queue; this
    is not a claim about serialized capture bytes or total process RSS.
    """
    total, nodes = 0, 0
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 16 or nodes > 200_000:
            raise ValueError("PAYLOAD_LIMIT")
        if item is None or type(item) in (bool, int, float):
            total += 128 + (item.bit_length() // 8 if type(item) is int else 0)
        elif type(item) is str:
            total += 128 + len(item) * 4
        elif type(item) in (dict, list, tuple):
            total += 256 + len(item) * 128
            if total > limit:
                raise ValueError("PAYLOAD_LIMIT")
            if type(item) is dict:
                pending.extend((child, depth + 1) for pair in item.items() for child in pair)
            else:
                pending.extend((child, depth + 1) for child in item)
        else:
            raise ValueError("PAYLOAD_TYPE")
        if total > limit:
            raise ValueError("PAYLOAD_LIMIT")
    return total


class FrameWorker:
    """Own factory/process/finish/close on one thread, including file teardown.

    Sinks implement process(item), finish(), close(), and integer byte_count.
    The producer only takes short bookkeeping locks. Work includes an observation
    timestamp so queue delay cannot be disguised as fresh analysis. Shutdown may
    drain a bounded prefix; errors/overflow never finalize it as COMPLETE.
    """

    def __init__(self, factory, *, name, on_failure=lambda: None, max_frames=128,
                 max_bytes=16 * 1024**2, max_age_s=None, clock=monotonic_now):
        if any(type(value) is not int or value < 1 for value in (max_frames, max_bytes)):
            raise ValueError("WORKER_LIMIT_INVALID")
        if (max_age_s is not None and (type(max_age_s) not in (int, float)
                or not math.isfinite(max_age_s) or max_age_s <= 0)):
            raise ValueError("WORKER_AGE_INVALID")
        self._factory, self._on_failure, self._clock = factory, on_failure, clock
        self._max_frames, self._max_bytes, self._max_age = max_frames, max_bytes, max_age_s
        self._condition = threading.Condition()
        self._queue = deque()
        self._bytes = self._active_size = 0
        self._peak_bytes = self._peak_frames = 0
        self._submitted = self._processed = self._discarded = self._rejected = 0
        self._active_at = None
        self._failed = self._closing = self._complete = False
        self._status, self._reason = "STARTING", "STARTUP"
        self._resource = None
        self._committed_bytes = 0
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def done(self):
        return self._done.is_set() and not self._thread.is_alive()

    @property
    def healthy(self):
        with self._condition:
            return not self._failed

    def snapshot(self):
        with self._condition:
            return {
                "status": self._status, "reason": self._reason,
                "failed": self._failed, "done": self.done,
                "submitted_frames": self._submitted, "processed_frames": self._processed,
                "discarded_frames": self._discarded,
                "rejected_frames": self._rejected,
                "buffered_frames": len(self._queue) + bool(self._active_size),
                "buffered_bytes": self._bytes,
                "peak_buffered_frames": self._peak_frames, "peak_buffered_bytes": self._peak_bytes,
                "max_frames": self._max_frames, "max_bytes": self._max_bytes,
                "bytes": self._committed_bytes,
                "live_acceptance": False,
            }

    def _fail(self, reason):
        with self._condition:
            if self._failed:
                return
            self._failed = self._closing = True
            self._status, self._reason = "ERROR", reason
            self._discarded += len(self._queue)
            self._queue.clear()
            self._bytes = self._active_size
            self._condition.notify_all()
        # A diagnostic callback cannot kill the SDK owner or skip teardown.
        with suppress(Exception):
            self._on_failure()

    def submit(self, item, *, size, observed_at):
        if type(size) is not int or size < 1:
            self._fail("PAYLOAD_INVALID")
            return False
        if (type(observed_at) not in (int, float) or observed_at < 0
                or observed_at > self._clock() or not math.isfinite(observed_at)):
            self._fail("OBSERVATION_INVALID")
            return False
        with self._condition:
            if self._closing or self._failed:
                return False
            oldest = (self._active_at if self._active_size else
                      self._queue[0][2] if self._queue else None)
            if (self._max_age is not None and oldest is not None
                    and self._clock() - oldest > self._max_age):
                reason = "QUEUE_STALE"
                self._rejected += 1
            elif (len(self._queue) + bool(self._active_size) >= self._max_frames
                    or self._bytes + size > self._max_bytes):
                # This rejected observation is part of the incomplete boundary.
                reason = "QUEUE_OVERFLOW"
                self._rejected += 1
            else:
                self._queue.append((item, size, observed_at))
                self._bytes += size
                self._peak_bytes = max(self._peak_bytes, self._bytes)
                self._peak_frames = max(self._peak_frames,
                                        len(self._queue) + bool(self._active_size))
                self._submitted += 1
                self._condition.notify_all()
                return True
        self._fail(reason)
        return False

    def fail_payload(self):
        self._fail("PAYLOAD_LIMIT")

    def close(self, *, complete=False, reason="SOURCE_ENDED"):
        with self._condition:
            if not self._closing:
                self._closing, self._complete = True, complete
                self._status, self._reason = "DRAINING", reason
            self._condition.notify_all()

    def join(self, timeout=None):
        self._thread.join(timeout)
        return self.done

    def wait_idle(self, timeout):
        """Test/diagnostic synchronization only; never used by the SDK producer."""
        deadline = monotonic_now() + timeout
        with self._condition:
            while self._bytes:
                remaining = deadline - monotonic_now()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _refresh_bytes(self):
        # Even status access belongs to the sink owner. A future slow accessor
        # must not move file I/O back into the producer or the desktop poller.
        try:
            value = self._resource.byte_count
            if type(value) is not int or value < self._committed_bytes:
                raise ValueError("INVALID_BYTE_COUNT")
            with self._condition:
                self._committed_bytes = value
        except Exception:
            self._fail("BYTE_COUNT_FAILED")

    def _run(self):
        try:
            try:
                self._resource = self._factory()
            except Exception:
                self._fail("STARTUP_FAILED")
                return
            self._refresh_bytes()
            with self._condition:
                if not self._closing:
                    self._status, self._reason = "RUNNING", "READY"
            while True:
                with self._condition:
                    while not self._queue and not self._closing:
                        self._condition.wait()
                    if not self._queue:
                        break
                    item, size, observed_at = self._queue.popleft()
                    self._active_size = size
                    self._active_at = observed_at
                processed = False
                try:
                    if self._max_age is not None and self._clock() - observed_at > self._max_age:
                        self._fail("QUEUE_STALE")
                    else:
                        self._resource.process(item)
                        processed = True
                        with self._condition:
                            self._processed += 1
                except Exception:
                    self._fail("PROCESSING_FAILED")
                finally:
                    self._refresh_bytes()
                    with self._condition:
                        if not processed:
                            self._discarded += 1
                        self._bytes -= size
                        self._active_size = 0
                        self._active_at = None
                        self._condition.notify_all()
                if self._failed:
                    break
            if self._complete and not self._failed:
                try:
                    self._resource.finish()
                except Exception:
                    self._fail("FINALIZE_FAILED")
        finally:
            try:
                if self._resource is not None:
                    self._resource.close()
            except Exception:
                self._fail("CLOSE_FAILED")
            finally:
                if self._resource is not None:
                    self._refresh_bytes()
            with self._condition:
                if not self._failed:
                    self._status = "COMPLETE" if self._complete else "INCOMPLETE"
                self._done.set()
                self._condition.notify_all()
