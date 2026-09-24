"""Reader pacing is a polling safeguard, not authentic SDK-rate acceptance."""

import pytest

from iracing_ai_engineer.live_app import _pace_reader


class Stop:
    def __init__(self, stopped=False):
        self.stopped = stopped

    def is_set(self):
        return self.stopped

    def wait(self, _seconds):
        pytest.fail("The connected reader must not use a coarse Event timeout")


@pytest.mark.parametrize("elapsed,expected", [
    (0, .01), (.003, .007), (.0099, .0001), (.01, None), (.025, None), (-.01, .01),
])
def test_pacing_only_fills_remaining_budget_and_caps_request(elapsed, expected):
    requests = []
    _pace_reader(Stop(), 0, lambda: elapsed, requests.append)
    if expected is None:
        assert requests == []
    else:
        assert requests == [pytest.approx(expected)]
        assert requests[0] <= .01


def test_already_stopped_reader_never_sleeps():
    requests = []
    _pace_reader(Stop(stopped=True), 0, lambda: 0, requests.append)
    assert requests == []


def test_stop_during_sleep_is_not_followed_by_another_wait_or_spin():
    stop, requests = Stop(), []

    def sleeper(seconds):
        requests.append(seconds)
        stop.stopped = True

    _pace_reader(stop, 0, lambda: 0, sleeper)
    _pace_reader(stop, 0, lambda: 0, sleeper)
    assert requests == [.01]
