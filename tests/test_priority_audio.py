"""Synthetic backends only: never open microphones, speakers or game APIs."""

import threading
import time

import pytest

from iracing_ai_engineer.priority_audio import AudioPreempted, PriorityAudio


class Backend:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.active = 0
        self.peak = 0
        self.ignore_cancel = False
        self.driver_release = None

    def devices(self):
        return {"inputs": [], "outputs": []}

    def record(self, stop, **_kwargs):
        self.play("long", stop)
        return b"partial microphone buffer"

    def play(self, wav, stop, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            self.calls.append(wav)
            if wav == "long":
                self.entered.set()
                if self.ignore_cancel:
                    assert self.release.wait(2)
                else:
                    assert stop.wait(2)
                if self.driver_release is not None:
                    self.driver_release.set()
            elif kwargs.get("start_guard", lambda: True)():
                kwargs.get("on_started", lambda: None)()
        finally:
            self.active -= 1


def low_thread(arbiter, stop, *, record=False):
    outcomes = []

    def run():
        try:
            outcomes.append(arbiter.record(stop) if record else arbiter.play("long", stop))
        except AudioPreempted:
            outcomes.append("PREEMPTED")

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcomes


@pytest.mark.parametrize("record", [False, True])
@pytest.mark.parametrize("simultaneous_release", [False, True])
def test_urgent_cancels_low_owner_waits_for_release_and_discards_partial_recording(
    record, simultaneous_release,
):
    backend, stop = Backend(), threading.Event()
    if simultaneous_release:
        backend.driver_release = stop
    arbiter = PriorityAudio(backend)
    worker, outcomes = low_thread(arbiter, stop, record=record)
    assert backend.entered.wait(1)
    started = []
    assert arbiter.urgent("left", threading.Event(), deadline=time.perf_counter() + 1,
                          guard=lambda: True, on_started=lambda: started.append(True),
                          device="default", volume=0.7)
    worker.join(1)
    assert not worker.is_alive()
    assert outcomes == ["PREEMPTED"] and started == [True]
    assert backend.peak == 1 and backend.calls == ["long", "left"]
    assert stop.is_set() is simultaneous_release


def test_stuck_owner_does_not_allow_overlap_or_late_urgent_backlog():
    backend = Backend()
    backend.ignore_cancel = True
    arbiter = PriorityAudio(backend)
    worker, outcomes = low_thread(arbiter, threading.Event())
    assert backend.entered.wait(1)
    try:
        assert not arbiter.urgent("left", threading.Event(), deadline=time.perf_counter() + 0.05,
                                  guard=lambda: True, on_started=lambda: None,
                                  device="default", volume=0.7)
        with pytest.raises(AudioPreempted):
            arbiter.play("normal", threading.Event())
    finally:
        backend.release.set()
        worker.join(1)
    assert outcomes == ["PREEMPTED"]
    assert backend.calls == ["long"] and backend.peak == 1


@pytest.mark.parametrize("cancelled,valid", [(True, True), (False, False)])
def test_withdrawn_urgent_call_neither_preempts_nor_opens_audio(cancelled, valid):
    backend, stop = Backend(), threading.Event()
    if cancelled:
        stop.set()
    arbiter = PriorityAudio(backend)
    assert not arbiter.urgent("left", stop, deadline=time.perf_counter() + 1,
                              guard=lambda: valid, on_started=lambda: None,
                              device="default", volume=0.7)
    assert backend.calls == []


def test_real_driver_release_returns_complete_recording_not_preemption():
    backend, stop = Backend(), threading.Event()
    arbiter = PriorityAudio(backend)
    worker, outcomes = low_thread(arbiter, stop, record=True)
    assert backend.entered.wait(1)
    stop.set()
    worker.join(1)
    assert outcomes == [b"partial microphone buffer"]


def test_new_ptt_cue_can_interrupt_background_notice_without_overlapping():
    backend, arbiter = Backend(), None
    arbiter = PriorityAudio(backend)
    outcomes = []

    def notice():
        try:
            arbiter.background_play("long", threading.Event())
        except AudioPreempted:
            outcomes.append("CANCELLED")

    worker = threading.Thread(target=notice)
    worker.start()
    assert backend.entered.wait(1)
    arbiter.play("ptt-cue", threading.Event())
    worker.join(1)
    assert outcomes == ["CANCELLED"]
    assert backend.calls == ["long", "ptt-cue"] and backend.peak == 1
