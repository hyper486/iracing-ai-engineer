"""Single audio owner with urgent cancellation, never a backlog of race calls."""

from __future__ import annotations

import threading

from .runtime_clock import monotonic_now


class AudioPreempted(ValueError):
    def __init__(self):
        super().__init__("AUDIO_PREEMPTED")


class _LinkedStop(threading.Event):
    """Keep driver release separate from a higher-priority cancellation.

    Backends require an Event. Polling also observes the caller's Event without
    a relay thread and without turning cancellation into release-to-send.
    """

    def __init__(self, parent, priority=1):
        super().__init__()
        self.parent = parent
        self.priority = priority

    def is_set(self):
        return super().is_set() or self.parent.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else monotonic_now() + max(0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - monotonic_now()
            if remaining is not None and remaining <= 0:
                break
            super().wait(0.01 if remaining is None else min(0.01, remaining))
        return self.is_set()


class PriorityAudio:
    """Serialize all device operations; an urgent waiter cancels the low owner.

    A cancelled backend must actually return before the next operation begins.
    A stuck owner therefore causes bounded urgent expiry, never overlapping
    streams or a false release acknowledgment. Normal operations never queue.
    """

    def __init__(self, backend, *, clock=monotonic_now):
        self._backend, self._clock = backend, clock
        self._condition = threading.Condition()
        self._active = None
        self._urgent_waiting = False

    def _normal(self, operation, parent, *, priority=1):
        token = _LinkedStop(parent, priority)
        with self._condition:
            if (self._active is not None and self._active.priority < priority
                    and not self._urgent_waiting):
                # PTT can interrupt a deferred informational notice, but never
                # a proximity call. Wait for actual output ownership release.
                self._active.set()
                deadline = monotonic_now() + 0.25
                while self._active is not None and not self._urgent_waiting:
                    if token.is_set() or monotonic_now() >= deadline:
                        break
                    self._condition.wait(0.01)
            if self._active is not None or self._urgent_waiting:
                raise AudioPreempted()
            self._active = token
        try:
            if token.is_set():
                return None
            result = operation(token)
            if threading.Event.is_set(token):
                # Includes a partial microphone buffer: it must be discarded.
                # A simultaneous real key release must not authorize that prefix.
                raise AudioPreempted()
            return result
        finally:
            with self._condition:
                self._active = None
                self._condition.notify_all()

    def devices(self):
        return self._normal(lambda _: self._backend.devices(), threading.Event())

    def record(self, stop, **kwargs):
        return self._normal(lambda token: self._backend.record(token, **kwargs), stop) or b""

    def play(self, wav, stop, **kwargs):
        self._normal(lambda token: self._backend.play(wav, token, **kwargs), stop)

    def background_play(self, wav, stop, **kwargs):
        self._normal(lambda token: self._backend.play(wav, token, **kwargs), stop, priority=0)

    def urgent(self, wav, stop, *, deadline, guard, on_started, device, volume):
        """Return false for withdrawn/expired calls; hardware failures propagate.

        Only one proximity worker calls this API. The guard is also passed into
        the production player for revalidation after potentially slow device open.
        """
        token = _LinkedStop(stop, priority=2)
        wall_deadline = monotonic_now() + 0.25

        def admitted():
            return (not token.is_set() and self._clock() < deadline
                    and monotonic_now() < wall_deadline and guard())

        with self._condition:
            if self._urgent_waiting:
                raise AudioPreempted()
            self._urgent_waiting = True
        try:
            with self._condition:
                if not admitted():
                    return False
                if self._active is not None:
                    self._active.set()
                while self._active is not None:
                    if not admitted():
                        return False
                    self._condition.wait(0.01)
                if not admitted():
                    return False
                self._active = token
            try:
                self._backend.play(wav, token, device=device, volume=volume,
                                   start_guard=admitted, on_started=on_started)
                return not token.is_set()
            finally:
                with self._condition:
                    self._active = None
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._urgent_waiting = False
                self._condition.notify_all()
