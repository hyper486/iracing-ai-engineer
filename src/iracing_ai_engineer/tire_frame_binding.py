"""Fixed-field confirmation anchor shared by live recording and private replay."""

from __future__ import annotations

import hashlib
import math

from .collector import _canonical_json, _capture_us

FIELDS = tuple(sorted((
    "SessionTick", "SessionTime", "SessionNum", "PlayerCarIdx", "LapCompleted",
    "OnPitRoad", "IsOnTrack", "IsOnTrackCar", "IsReplayPlaying", "PlayerTireCompound",
    "TireSetsUsed", "Speed", "PlayerCarInPitStall", "PitstopActive",
)))


def valid_frame_anchor(value):
    return (type(value) is dict and set(value) == {"capture_monotonic_us", "frame_sha256"}
            and type(value["capture_monotonic_us"]) is int
            and 0 <= value["capture_monotonic_us"] <= 2**53
            and type(value["frame_sha256"]) is str and len(value["frame_sha256"]) == 64
            and all(c in "0123456789abcdef" for c in value["frame_sha256"]))


def tire_frame_anchor(frame, *, capture_monotonic_us=None):
    """Hash only bounded scalar fields; never persist an SDK dictionary/text.

    Collector serialization canonicalizes floating negative zero. Reproduce it
    here; replay may supply the stored integer clock to avoid float round trips.
    """
    captured = _capture_us(frame) if capture_monotonic_us is None else capture_monotonic_us
    if (type(captured) is not int or not 0 <= captured <= 2**53
            or type(frame.buffer_tick) is not int or not 0 <= frame.buffer_tick <= 2**53
            or type(frame.session_info_update) is not int
            or not 0 <= frame.session_info_update <= 2**53):
        raise ValueError("TIRE_FRAME_ANCHOR_INVALID")
    values = []
    for name in FIELDS:
        value = frame.values.get(name)
        if (value is not None and type(value) is not bool
                and not (type(value) in (int, float) and -2**53 <= value <= 2**53
                         and math.isfinite(value))):
            raise ValueError("TIRE_FRAME_ANCHOR_INVALID")
        values.append(0.0 if type(value) is float and value == 0. else value)
    projection = {
        "capture_monotonic_us": captured, "buffer_tick": frame.buffer_tick,
        "session_info_update": frame.session_info_update, "values": values,
        "sim_mode": "full" if type(frame.sim_mode_raw) is str
        and frame.sim_mode_raw.casefold() == "full" else "other",
        "read_errors": [name for name in FIELDS if name in frame.read_errors],
    }
    return {"capture_monotonic_us": captured, "frame_sha256": hashlib.sha256(
        b"tire-confirmation-frame-v1\0" + _canonical_json(projection)).hexdigest()}
