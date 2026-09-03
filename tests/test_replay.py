from __future__ import annotations

import numpy as np

from iracing_ai_engineer.replay import normalized_frames_sha256


def test_same_frames_have_same_digest_across_chunk_sizes(two_wrap_channels):
    order = tuple(two_wrap_channels)

    first = normalized_frames_sha256(two_wrap_channels, channel_order=order, chunk_size=1)
    second = normalized_frames_sha256(two_wrap_channels, channel_order=order, chunk_size=4096)

    assert first == second


def test_one_frame_change_changes_digest(two_wrap_channels):
    order = tuple(two_wrap_channels)
    first = normalized_frames_sha256(two_wrap_channels, channel_order=order)
    changed = {name: values.copy() for name, values in two_wrap_channels.items()}
    changed["Throttle"][20] = 0.5

    assert normalized_frames_sha256(changed, channel_order=order) != first


def test_negative_zero_is_canonicalized():
    positive = {"value": np.array([0.0], dtype=np.float64)}
    negative = {"value": np.array([-0.0], dtype=np.float64)}

    assert normalized_frames_sha256(positive, channel_order=("value",)) == (
        normalized_frames_sha256(negative, channel_order=("value",))
    )


def test_nan_payload_is_canonicalized():
    first_nan = np.array([np.nan], dtype=np.float64)
    second_nan = np.array([np.nan], dtype=np.float64)
    first_nan.view(np.uint64)[0] = 0x7FF8000000000001
    second_nan.view(np.uint64)[0] = 0x7FF8000000000010

    assert normalized_frames_sha256(
        {"value": first_nan}, channel_order=("value",)
    ) == normalized_frames_sha256({"value": second_nan}, channel_order=("value",))


def test_missing_and_zero_are_distinct():
    missing = {"value": np.array([None], dtype=object)}
    zero = {"value": np.array([0], dtype=np.int64)}

    assert normalized_frames_sha256(missing, channel_order=("value",)) != (
        normalized_frames_sha256(zero, channel_order=("value",))
    )
