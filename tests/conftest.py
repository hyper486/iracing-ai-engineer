from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture
def two_wrap_channels() -> dict[str, np.ndarray]:
    distance = np.concatenate(
        (
            np.array([0.98, 0.99, 0.005]),
            np.linspace(0.02, 0.99, 60),
            np.array([0.005, 0.02]),
        )
    )
    count = len(distance)
    lap = np.ones(count, dtype=np.int32)
    lap[2:] = 2
    lap[63:] = 3
    completed = np.zeros(count, dtype=np.int32)
    completed[2:] = 1
    completed[63:] = 2
    return {
        "SessionTime": np.arange(count, dtype=np.float64) / 60.0,
        "SessionTick": np.arange(10_000, 10_000 + count, dtype=np.int64),
        "Lap": lap,
        "LapCompleted": completed,
        "LapDistPct": distance,
        "Speed": np.full(count, 45.0),
        "Throttle": np.full(count, 0.7),
        "Brake": np.zeros(count),
        "SteeringWheelAngle": np.linspace(-0.2, 0.2, count),
        "Gear": np.full(count, 4, dtype=np.int32),
        "RPM": np.full(count, 5_000.0),
        "FuelLevel": np.linspace(50.0, 48.0, count),
        "OnPitRoad": np.zeros(count, dtype=bool),
        "PlayerCarInPitStall": np.zeros(count, dtype=bool),
        "PlayerTrackSurface": np.full(count, 3, dtype=np.int32),
        "PlayerCarMyIncidentCount": np.zeros(count, dtype=np.int32),
    }
