"""Invented independent clocks; no simulator, audio or live-acceptance claim."""

from dataclasses import replace

import pytest

from iracing_ai_engineer.live_monitor import LiveMonitor
from iracing_ai_engineer.live_motion import LiveMotionTracker
from iracing_ai_engineer.live_pit_observation import LivePitObservationTracker
from iracing_ai_engineer.live_stint import LiveStintTracker
from iracing_ai_engineer.live_tire_age import LiveTireAgeTracker
from iracing_ai_engineer.synthetic_runtime import synthetic_frames

OWNERS = (LiveMotionTracker, LivePitObservationTracker, LiveStintTracker, LiveTireAgeTracker)


def prepared(owner_class, *, buffer_origin=2000, session_origin=1000):
    owner = owner_class(60)
    monitor = LiveMonitor(source_id="synthetic", session_id="synthetic", sdk_tick_rate_hz=60)
    base = next(synthetic_frames(8))
    for index in range(3):
        seconds = 10 + index / 60
        frame = replace(base, buffer_tick=buffer_origin + index,
                        captured_monotonic_s=seconds + 1,
                        values={**base.values, "SessionTick": session_origin + index,
                                "SessionTime": seconds})
        monitor.feed(frame)
        owner.feed(frame, monitor.latest_sample)
    assert owner.previous is not None
    return owner, monitor, frame


@pytest.mark.parametrize("owner_class", OWNERS)
@pytest.mark.parametrize("origins", [(0, 1000), (1000, 0)])
def test_each_owner_accepts_both_orders_of_independently_based_clocks(owner_class, origins):
    owner, _, frame = prepared(owner_class, buffer_origin=origins[0], session_origin=origins[1])
    assert owner._buffer_tick == frame.buffer_tick
    assert owner._buffer_tick != frame.values["SessionTick"]
    assert not owner.failed


@pytest.mark.parametrize("owner_class", OWNERS)
@pytest.mark.parametrize("buffer_tick", [2002, 2001, 2018, None, True, -1, 2**31, 2003.0])
def test_each_owner_discards_old_context_on_bad_publication_progress(owner_class, buffer_tick):
    owner, monitor, frame = prepared(owner_class)
    revision = owner.revision
    next_frame = replace(frame, buffer_tick=2003,
                         captured_monotonic_s=frame.captured_monotonic_s + 1 / 60,
                         values={**frame.values, "SessionTick": 1003, "SessionTime": 10.05})
    monitor.feed(next_frame)
    # Isolate the owner's clock guard from the monitor's duplicate guard.
    owner.feed(replace(next_frame, buffer_tick=buffer_tick), monitor.latest_sample)
    assert owner.revision > revision
    if type(buffer_tick) is not int or not 0 <= buffer_tick <= 2**31 - 1:
        assert owner.previous is None and owner._buffer_tick is None
    else:
        assert owner._buffer_tick == buffer_tick
        if isinstance(owner, LivePitObservationTracker):
            assert owner.active is owner.observation is None
        elif isinstance(owner, LiveMotionTracker):
            assert all(not trace.profiles for trace in owner.actors.values())
        elif isinstance(owner, LiveTireAgeTracker):
            assert owner.origin is owner.confirmation is None
