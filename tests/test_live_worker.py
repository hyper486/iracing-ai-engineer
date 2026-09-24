"""Bounded fake sinks only: no real SDK, microphone or speaker."""

import threading
import time

import pytest

from iracing_ai_engineer.live_worker import FrameWorker, payload_size


class Sink:
    def __init__(self):
        self.rows = []
        self.finished = self.closed = False
        self.byte_count = 0
        self.threads = []
        self.entered, self.proceed = threading.Event(), threading.Event()
        self.block = False
        self.fail = None

    def process(self, item):
        self.threads.append(threading.get_ident())
        self.entered.set()
        if self.block:
            assert self.proceed.wait(3)
        if self.fail == "process":
            raise ValueError("SYNTHETIC PRIVATE FAILURE")
        self.rows.append(item)
        self.byte_count += 3

    def finish(self):
        self.threads.append(threading.get_ident())
        if self.fail == "finish":
            raise ValueError("SYNTHETIC PRIVATE FAILURE")
        self.finished = True

    def close(self):
        self.threads.append(threading.get_ident())
        self.closed = True
        if self.fail == "close":
            raise ValueError("SYNTHETIC PRIVATE FAILURE")


def test_all_work_and_teardown_have_one_owner_and_orderly_stop_drains_all_frames():
    sink, constructors = Sink(), []

    def factory():
        constructors.append(threading.get_ident())
        return sink

    worker = FrameWorker(factory, name="synthetic-test")
    for index in range(20):
        assert worker.submit(index, size=100, observed_at=time.perf_counter())
    worker.close(complete=True)
    assert worker.join(3)
    assert sink.rows == list(range(20)) and sink.finished and sink.closed
    assert set(constructors + sink.threads) == {constructors[0]}
    assert constructors[0] != threading.get_ident()
    state = worker.snapshot()
    assert state["status"] == "COMPLETE" and state["buffered_bytes"] == 0
    assert state["processed_frames"] == state["submitted_frames"] == 20


@pytest.mark.parametrize("bytes_limit,frames_limit", [(150, 128), (10000, 1)])
def test_overflow_is_fast_explicit_and_cannot_finish_incomplete_capture(bytes_limit, frames_limit):
    sink, failures = Sink(), []
    sink.block = True
    worker = FrameWorker(lambda: sink, name="synthetic-blocked", max_bytes=bytes_limit,
                         max_frames=frames_limit, on_failure=lambda: failures.append(True))
    try:
        assert worker.submit(1, size=100, observed_at=time.perf_counter())
        assert sink.entered.wait(1)
        before = time.perf_counter()
        assert not worker.submit(2, size=100, observed_at=time.perf_counter())
        assert time.perf_counter() - before < 0.2
        assert not worker.done and worker.snapshot()["status"] == "ERROR"
        assert worker.snapshot()["rejected_frames"] == 1
        assert worker.snapshot()["buffered_bytes"] == 100
        worker.close(complete=True)
    finally:
        sink.proceed.set()
        assert worker.join(3)
    assert failures == [True] and not sink.finished and sink.closed
    assert worker.snapshot()["processed_frames"] == 1
    assert worker.snapshot()["discarded_frames"] == 0


def test_queue_age_expires_even_while_processing_owner_is_stuck():
    sink, now = Sink(), [1.0]
    sink.block = True
    worker = FrameWorker(lambda: sink, name="synthetic-stale", clock=lambda: now[0], max_age_s=0.5)
    try:
        worker.submit(1, size=10, observed_at=1.0)
        assert sink.entered.wait(1)
        now[0] = 1.6
        assert not worker.submit(2, size=10, observed_at=1.6)
        assert worker.snapshot()["reason"] == "QUEUE_STALE"
    finally:
        sink.proceed.set()
        assert worker.join(3)
    assert not sink.finished


@pytest.mark.parametrize("failure,code", [
    ("process", "PROCESSING_FAILED"), ("finish", "FINALIZE_FAILED"), ("close", "CLOSE_FAILED"),
])
def test_sink_faults_are_private_safe_latched_and_always_attempt_teardown(failure, code):
    sink = Sink()
    sink.fail = failure
    worker = FrameWorker(lambda: sink, name="synthetic-error")
    worker.submit(1, size=10, observed_at=time.perf_counter())
    worker.close(complete=True)
    assert worker.join(3)
    result = worker.snapshot()
    assert result["status"] == "ERROR" and result["reason"] == code and sink.closed
    assert "PRIVATE" not in str(result)


def test_startup_failure_does_not_open_retry_loop_or_claim_completion():
    def fail():
        raise OSError("SYNTHETIC PRIVATE PATH")

    worker = FrameWorker(fail, name="synthetic-startup")
    assert worker.join(3)
    assert worker.snapshot()["reason"] == "STARTUP_FAILED"
    assert not worker.submit(1, size=1, observed_at=time.perf_counter())


def test_aborted_capture_drains_prefix_but_never_finishes_it():
    sink = Sink()
    worker = FrameWorker(lambda: sink, name="synthetic-incomplete")
    worker.submit(1, size=10, observed_at=time.perf_counter())
    worker.close(complete=False)
    assert worker.join(3)
    assert sink.rows == [1] and sink.closed and not sink.finished
    assert worker.snapshot()["status"] == "INCOMPLETE"


@pytest.mark.parametrize("value", ["x" * 1000, [0] * 1000, 1 << 10000])
def test_payload_size_rejects_oversize_objects_before_queueing(value):
    with pytest.raises(ValueError, match="PAYLOAD_LIMIT"):
        payload_size(value, limit=500)


def test_payload_size_rejects_cycles_unknown_objects_and_accepts_transport_json():
    cycle = []
    cycle.append(cycle)
    for value in (cycle, object()):
        with pytest.raises(ValueError):
            payload_size(value)
    assert payload_size({"flags": [1, 2], "mode": "full", "missing": None}) > 0


def test_immutable_sdk_bytes_are_bounded_but_mutable_buffers_still_fail():
    assert payload_size(b"\0\xff") == 130
    assert payload_size([b"\0", b"\xff"]) > 260
    with pytest.raises(ValueError, match="PAYLOAD_LIMIT"):
        payload_size(b"x" * 1000, limit=500)
    for value in (bytearray(b"x"), memoryview(b"x")):
        with pytest.raises(ValueError, match="PAYLOAD_TYPE"):
            payload_size(value)


@pytest.mark.parametrize("observed", [None, True, -1, float("nan"), float("inf"), 2.0])
def test_missing_invalid_or_future_observation_cannot_become_fresh_work(observed):
    sink = Sink()
    worker = FrameWorker(lambda: sink, name="synthetic-clock", clock=lambda: 1.0)
    assert not worker.submit(1, size=10, observed_at=observed)
    assert worker.join(3)
    assert worker.snapshot()["reason"] == "OBSERVATION_INVALID" and sink.rows == []


def test_failure_callback_cannot_escape_into_producer_or_prevent_teardown():
    sink = Sink()

    def fail():
        raise RuntimeError("SYNTHETIC PRIVATE CALLBACK")

    worker = FrameWorker(lambda: sink, name="synthetic-callback", on_failure=fail)
    worker.fail_payload()
    assert worker.join(3)
    assert worker.snapshot()["status"] == "ERROR" and sink.closed and not sink.finished


def test_slow_sink_status_accessor_never_runs_on_the_producer_or_status_poller():
    entered, release = threading.Event(), threading.Event()
    owners = []

    class StatusSink:
        @property
        def byte_count(self):
            owners.append(threading.get_ident())
            entered.set()
            assert release.wait(3)
            return 0

        def process(self, _item):
            raise AssertionError("failed lane must discard queued work")

        def finish(self):
            raise AssertionError("incomplete prefix must not be finalized")

        def close(self):
            owners.append(threading.get_ident())

    worker = FrameWorker(StatusSink, name="synthetic-status", max_frames=1)
    try:
        assert entered.wait(1)
        assert worker.submit(1, size=10, observed_at=time.perf_counter())
        assert not worker.submit(2, size=10, observed_at=time.perf_counter())
        state = worker.snapshot()
        assert state["status"] == "ERROR" and not state["done"] and state["bytes"] == 0
    finally:
        release.set()
        assert worker.join(3)
    assert len(set(owners)) == 1 and owners[0] != threading.get_ident()
    assert worker.snapshot()["discarded_frames"] == 1


def test_partial_committed_bytes_are_still_reported_after_a_processing_error():
    sink = Sink()

    def partial(_item):
        sink.byte_count = 7
        raise OSError("SYNTHETIC PRIVATE WRITE")

    sink.process = partial
    worker = FrameWorker(lambda: sink, name="synthetic-partial")
    assert worker.submit(1, size=10, observed_at=time.perf_counter())
    worker.close(complete=True)
    assert worker.join(3)
    assert worker.snapshot()["bytes"] == 7 and worker.snapshot()["status"] == "ERROR"
    assert sink.closed and not sink.finished
