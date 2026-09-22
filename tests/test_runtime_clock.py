"""Synthetic clock regressions only; no simulator, native process or live evidence."""

from __future__ import annotations

import base64
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from iracing_ai_engineer import (
    collector,
    crewchief_transport,
    live_monitor,
    live_preflight,
    runtime_clock,
    sdk_probe,
)


@dataclass
class _SyntheticClock:
    value: float = 1_000.0
    coarse_reads: int = 0

    def high_resolution(self) -> float:
        return self.value

    def coarse(self) -> float:
        self.coarse_reads += 1
        # A deliberately different origin and frozen/coarse Windows clock.
        return 42.0


@pytest.fixture
def synthetic_clock(monkeypatch: pytest.MonkeyPatch) -> _SyntheticClock:
    clock = _SyntheticClock()
    # All production modules were imported above, before either monkeypatch.
    monkeypatch.setattr(runtime_clock.time, "perf_counter", clock.high_resolution)
    monkeypatch.setattr(runtime_clock.time, "monotonic", clock.coarse)
    return clock


def _synthetic_packet(tick: int) -> bytes:
    values = {
        "AirTemp": 20.0,
        "Brake": 0.0,
        "CarIdxLap": [1, 1, 0],
        "CarIdxLapCompleted": [0, 0, 0],
        "CarIdxLapDistPct": [0.2, 0.3, -1.0],
        "CarIdxOnPitRoad": [False, False, False],
        "CarIdxTrackSurface": [3, 3, -1],
        "FuelLevel": 42.0,
        "FuelLevelPct": 0.42,
        "Gear": 3,
        "IsOnTrack": True,
        "IsOnTrackCar": True,
        "IsReplayPlaying": False,
        "Lap": 1,
        "LapCompleted": 0,
        "LapDistPct": 0.2,
        "OnPitRoad": False,
        "PitsOpen": True,
        "PlayerCarIdx": 0,
        "PlayerCarInPitStall": False,
        "PlayerTireCompound": 0,
        "RPM": 4_000.0,
        "SessionFlags": 0,
        "SessionLapsRemainEx": 20,
        "SessionNum": 0,
        "SessionTick": tick,
        "SessionTime": tick / 60.0,
        "SessionTimeRemain": 3_600.0,
        "Speed": 40.0,
        "SteeringWheelAngle": 0.0,
        "Throttle": 0.5,
        "TireSetsUsed": 0,
        "TrackTemp": 29.0,
    }
    descriptors = []
    offset = 0
    for name, value in sorted(values.items()):
        scalar = value[0] if isinstance(value, list) else value
        kind = 1 if type(scalar) is bool else 2 if type(scalar) is int else 4
        if name == "SessionTime":
            kind = 5
        elif name == "SessionFlags":
            kind = 3
        count = len(value) if isinstance(value, list) else 1
        descriptors.append({
            "name": name,
            "type_code": kind,
            "dtype": sdk_probe.SDK_TYPE_NAMES[kind],
            "offset": offset,
            "count": count,
            "count_as_time": False,
            "unit": "",
            "description": "Synthetic clock regression field",
        })
        offset += sdk_probe.SDK_TYPE_SIZES[kind] * count
    session_info = b"---\nWeekendInfo:\n Encoding: UTF8\n SimMode: full\n"
    packet = {
        "protocol": crewchief_transport.PROTOCOL_VERSION,
        "status": "ok",
        "connection": {
            "header_version": 2,
            "raw_header_status": 1,
            "tick_rate_hz": 60,
            "variable_count": len(descriptors),
            "buffer_count": 3,
            "buffer_len": offset,
        },
        "buffer_tick": tick,
        "session_info_update": 7,
        "session_info_b64": base64.b64encode(session_info).decode("ascii"),
        "descriptors": descriptors,
        "values": values,
        "read_errors": [],
    }
    return (json.dumps(packet, allow_nan=False) + "\n").encode("utf-8")


def _synthetic_transport(
    clock: _SyntheticClock, *schedule: tuple[int, float]
) -> crewchief_transport.WindowsCrewChiefTransport:
    responses = iter(schedule)

    def exchange(timeout_s: float) -> bytes:
        assert timeout_s > 0
        tick, clock.value = next(responses)
        return _synthetic_packet(tick)

    # The exchange seam cannot open an SDK map or launch this nonexistent helper.
    return crewchief_transport.WindowsCrewChiefTransport(
        Path("synthetic-clock-reader.exe"), _exchange=exchange,
    )


def _monitor() -> live_monitor.LiveMonitor:
    return live_monitor.LiveMonitor(
        source_id="synthetic-clock-source",
        session_id="synthetic-clock-session",
        sdk_tick_rate_hz=60,
        expected_car_count=3,
    )


@pytest.mark.parametrize(
    "run",
    [
        collector._collect_transport_to_writer,
        collector.collect_transport_to_jsonl,
        collector.collect_transport_to_jsonl_handle,
        live_monitor.monitor_live_transport,
        live_preflight._run_core,
        live_preflight._run_core_handle,
        live_preflight.run_live_preflight_transport,
    ],
    ids=lambda run: run.__name__,
)
def test_runtime_deadlines_default_to_the_shared_clock(run: Callable[..., object]) -> None:
    assert inspect.signature(run).parameters["monotonic"].default is runtime_clock.monotonic_now


@pytest.mark.parametrize("module", [crewchief_transport, sdk_probe])
def test_transport_capture_and_deadline_clocks_share_the_same_function(module) -> None:
    assert module.monotonic_now is runtime_clock.monotonic_now


def test_clock_returns_raw_counter_without_epsilon_or_regression_repair(synthetic_clock) -> None:
    values = (1_000.0001, 1_000.0001, 999.0)
    observed = []
    for value in values:
        synthetic_clock.value = value
        observed.append(runtime_clock.monotonic_now())
    assert observed == list(values)
    assert synthetic_clock.coarse_reads == 0


def test_startup_frame_keeps_its_original_high_resolution_timestamp(synthetic_clock) -> None:
    transport = _synthetic_transport(synthetic_clock, (100, 1_000.0001), (101, 1_000.0002))
    try:
        transport.startup(0)
        synthetic_clock.value = 1_000.00015
        first = transport.read_frozen(("SessionTick",))
        second = transport.read_frozen(("SessionTick",))
        assert first.captured_monotonic_s == 1_000.0001
        assert second.captured_monotonic_s == 1_000.0002
        assert (first.buffer_tick, second.buffer_tick) == (100, 101)
        assert synthetic_clock.coarse_reads == 0
    finally:
        transport.close()


def test_default_monitor_accepts_distinct_ticks_inside_one_coarse_clock_bucket(
    synthetic_clock,
) -> None:
    transport = _synthetic_transport(
        synthetic_clock, (100, 1_000.0001), (101, 1_000.0002), (102, 1_000.0003),
    )
    snapshots = []
    receipt = live_monitor.monitor_live_transport(
        transport,
        emit=snapshots.append,
        source_id="synthetic-clock-source",
        session_id="synthetic-clock-session",
        wait_seconds=0,
        max_reads=3,
        snapshot_seconds=0.00001,
        sleep=lambda _: None,
    )
    assert receipt.frame_count == receipt.event_receipt.accepted_sample_count == 3
    assert receipt.event_receipt.rejected_sample_count == 0
    assert receipt.duplicate_frame_count == receipt.dropped_tick_count == 0
    assert receipt.final_status == "READY"
    assert all(snapshot["status"] != "BLOCKED" for snapshot in snapshots)
    assert all(snapshot["advisor_only"] and not snapshot["executable"] for snapshot in snapshots)
    assert not any(
        issue in {"CAPTURE_TIME_REGRESSION", "SOURCE_STALE"}
        for snapshot in snapshots
        for issue in snapshot["quality"]["issues"]
    )
    assert synthetic_clock.coarse_reads == 0
    assert not transport.connected


@pytest.mark.parametrize("next_capture", [1_000.0, 999.9], ids=["frozen", "regressed"])
def test_real_capture_clock_freeze_or_regression_remains_rejected(
    synthetic_clock, next_capture: float,
) -> None:
    transport = _synthetic_transport(synthetic_clock, (100, 1_000.0), (101, next_capture))
    monitor = _monitor()
    try:
        transport.startup(0)
        fields = tuple(descriptor.name for descriptor in transport.descriptors())
        assert monitor.feed(transport.read_frozen(fields), observed_monotonic_s=1_001.0)
        monitor.snapshot()
        # Independent progressing observations isolate a broken capture clock.
        assert monitor.feed(transport.read_frozen(fields), observed_monotonic_s=1_001.1)
        rejected = monitor.snapshot()
        assert rejected["status"] == "BLOCKED"
        assert rejected["quality"]["stale"] is True
        assert "CAPTURE_TIME_REGRESSION" in rejected["quality"]["issues"]
        assert "SOURCE_STALE" in rejected["quality"]["issues"]
        assert any(event["kind"] == "quality_rejected" for event in rejected["events"])
        assert monitor.finish().event_receipt.rejected_sample_count == 1
        assert synthetic_clock.coarse_reads == 0
    finally:
        transport.close()


def test_default_monitor_rejects_actual_observation_clock_regression(synthetic_clock) -> None:
    transport = _synthetic_transport(synthetic_clock, (100, 1_000.0), (101, 999.9))
    with pytest.raises(live_monitor.LiveMonitorError, match="OBSERVATION_TIME_REGRESSION"):
        live_monitor.monitor_live_transport(
            transport,
            emit=lambda _: None,
            source_id="synthetic-clock-source",
            session_id="synthetic-clock-session",
            wait_seconds=0,
            max_reads=2,
            sleep=lambda _: None,
        )
    assert not transport.connected
    assert synthetic_clock.coarse_reads == 0


def test_frozen_sdk_buffer_still_expires_despite_a_progressing_high_resolution_clock(
    synthetic_clock,
) -> None:
    transport = _synthetic_transport(synthetic_clock, (100, 1_000.0), (100, 1_000.6))
    monitor = _monitor()
    try:
        transport.startup(0)
        fields = tuple(descriptor.name for descriptor in transport.descriptors())
        assert monitor.feed(transport.read_frozen(fields))
        monitor.snapshot()
        assert not monitor.feed(transport.read_frozen(fields))
        stale = monitor.snapshot()
        assert stale["status"] == "BLOCKED"
        assert stale["quality"]["stale"] is True
        assert "SOURCE_STALE" in stale["quality"]["issues"]
        receipt = monitor.finish()
        assert receipt.frame_count == receipt.duplicate_frame_count == 1
        assert synthetic_clock.coarse_reads == 0
    finally:
        transport.close()


def test_explicit_monitor_clock_injection_is_preserved(synthetic_clock) -> None:
    transport = _synthetic_transport(synthetic_clock, (100, 1_000.0), (101, 1_000.01))
    observations = []

    def observation_clock() -> float:
        value = synthetic_clock.value + 20.0
        observations.append(value)
        return value

    receipt = live_monitor.monitor_live_transport(
        transport,
        emit=lambda _: None,
        source_id="synthetic-clock-source",
        session_id="synthetic-clock-session",
        wait_seconds=0,
        max_reads=2,
        monotonic=observation_clock,
        sleep=lambda _: None,
    )
    assert observations and min(observations) >= 1_020.0
    assert receipt.frame_count == receipt.event_receipt.accepted_sample_count == 2
    assert receipt.final_status == "READY"
    assert synthetic_clock.coarse_reads == 0
