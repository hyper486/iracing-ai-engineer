"""Invented flag/context combinations, not recorded live-acceptance evidence."""

import pytest

from iracing_ai_engineer.session_flags import UNSUITABLE_LAP_FLAGS, unsuitable_lap_flags


@pytest.mark.parametrize("session_type,state", [
    (None, 4), ("Practice", 4), ("Race", 4), ("offline testing", 4),
    ("Offline Testing", None), ("Offline Testing", True), ("Offline Testing", 4.0),
    ("Offline Testing", "4"), *[("Offline Testing", state) for state in (0, 1, 2, 3, 5, 6)],
])
def test_unknown_race_and_non_racing_contexts_keep_all_guards(session_type, state):
    assert unsuitable_lap_flags(session_type, state) == UNSUITABLE_LAP_FLAGS


def test_active_offline_testing_only_exempts_the_pre_green_bit():
    mask = unsuitable_lap_flags("Offline Testing", 4)
    assert mask ^ UNSUITABLE_LAP_FLAGS == 0x0200
    assert mask & (0x0008 | 0x4000 | 0x100000 | 0x20000000)
