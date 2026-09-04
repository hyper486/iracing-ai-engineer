from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from iracing_ai_engineer import cli, live_monitor
from iracing_ai_engineer.live_monitor import (
    LIVE_MONITOR_CONTRACT_VERSION,
    LiveMonitor,
    LiveMonitorError,
    monitor_live_transport,
)
from iracing_ai_engineer.sdk_probe import (
    SDK_TYPE_NAMES,
    RawSdkFrame,
    SdkProbeUnavailable,
    VariableDescriptor,
)
from iracing_ai_engineer.telemetry import SourceKind


def _values(
    tick: int,
    *,
    in_car: bool,
    session_tick: int | None = None,
) -> dict[str, object]:
    return {
        "AirTemp": 20.0,
        "Brake": 0.0,
        "CarIdxLap": [1, 1, 0],
        "CarIdxLapCompleted": [0, 0, 0],
        "CarIdxLapDistPct": [0.2, 0.3, -1.0],
        "CarIdxOnPitRoad": [False, False, False],
        "CarIdxTrackSurface": [3, 3, -1],
        "FuelLevel": 42.0,
        "FuelLevelPct": 0.42,
        "Gear": 3 if in_car else 0,
        "IsOnTrack": in_car,
        "IsOnTrackCar": in_car,
        "IsReplayPlaying": False,
        "Lap": 1,
        "LapCompleted": 0,
        "LapDistPct": 0.2,
        "OnPitRoad": False,
        "PitsOpen": True,
        "PlayerCarIdx": 0,
        "PlayerCarInPitStall": False,
        "PlayerTireCompound": 0,
        "RPM": 4_000.0 if in_car else 0.0,
        "SessionFlags": 0,
        "SessionLapsRemainEx": 20,
        "SessionNum": 0,
        "SessionTick": tick if session_tick is None else session_tick,
        "SessionTime": tick / 60.0,
        "SessionTimeRemain": 3_600.0,
        "Speed": 40.0 if in_car else 0.0,
        "SteeringWheelAngle": 0.0,
        "Throttle": 0.5 if in_car else 0.0,
        "TireSetsUsed": 0,
        "TrackTemp": 29.0,
        "UserName": "Private Driver",
    }


def _frame(
    tick: int,
    *,
    in_car: bool,
    sim_mode: str = "full",
    session_tick: int | None = None,
) -> RawSdkFrame:
    return RawSdkFrame(
        buffer_tick=tick,
        session_info_update=1,
        values=_values(tick, in_car=in_car, session_tick=session_tick),
        sim_mode_raw=sim_mode,
        captured_monotonic_s=tick / 60.0,
    )


def _descriptor(name: str, offset: int, count: int) -> VariableDescriptor:
    return VariableDescriptor(
        name=name,
        type_code=4,
        dtype=SDK_TYPE_NAMES[4],
        offset=offset,
        count=count,
        count_as_time=False,
        unit="",
        description=name,
    )


def _descriptors(values: dict[str, object]) -> tuple[VariableDescriptor, ...]:
    result: list[VariableDescriptor] = []
    offset = 0
    for name, value in sorted(values.items()):
        if name == "UserName":
            continue
        count = len(value) if isinstance(value, list) else 1
        result.append(_descriptor(name, offset, count))
        offset += 4 * count
    return tuple(result)


def _snapshot_digest(snapshot: dict[str, object]) -> str:
    material = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    payload = json.dumps(
        material,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def test_monitor_projects_wait_then_ready_without_private_identity() -> None:
    monitor = LiveMonitor(
        source_id="local-monitor",
        session_id="test-session",
        sdk_tick_rate_hz=60,
    )

    assert monitor.feed(_frame(100, in_car=False)) is True
    waiting = monitor.snapshot()
    assert monitor.feed(_frame(101, in_car=True)) is True
    ready = monitor.snapshot()
    receipt = monitor.finish()

    assert waiting["status"] == "WAIT_CAR"
    assert waiting["context"]["player_control_state"] == "OUT_OF_CAR_OR_REPLAY_VIEW"
    assert ready["status"] == "READY"
    assert ready["context"]["player_control_state"] == "IN_CAR_PHYSICS"
    assert ready["telemetry"]["fuel_level_l"] == 42.0
    assert ready["telemetry"]["speed_mps"] == 40.0
    assert ready["snapshot_sha256"] == _snapshot_digest(ready)
    assert "Private Driver" not in json.dumps((waiting, ready, receipt.to_dict()))
    assert "source_id" not in json.dumps((waiting, ready, receipt.to_dict()))
    assert receipt.frame_count == 2
    assert receipt.snapshot_count == 2
    assert receipt.in_car_snapshot_count == 1
    assert receipt.final_status == "READY"
    assert receipt.dropped_tick_count == 0
    assert receipt.to_dict()["advisor_only"] is True
    assert receipt.to_dict()["executable"] is False


def test_monitor_receipts_are_deterministic() -> None:
    def run() -> tuple[list[dict[str, object]], dict[str, object]]:
        monitor = LiveMonitor(
            source_id="local-monitor",
            session_id="test-session",
            sdk_tick_rate_hz=60,
        )
        snapshots: list[dict[str, object]] = []
        for tick, in_car in ((100, False), (101, True)):
            monitor.feed(_frame(tick, in_car=in_car))
            snapshots.append(monitor.snapshot())
        return snapshots, monitor.finish().to_dict()

    assert run() == run()


def test_monitor_counts_same_tick_duplicates_without_reprocessing() -> None:
    monitor = LiveMonitor(
        source_id="local-monitor",
        session_id="test-session",
        sdk_tick_rate_hz=60,
    )
    first = _frame(100, in_car=False)
    assert monitor.feed(first) is True
    assert monitor.feed(first) is False
    monitor.snapshot()

    receipt = monitor.finish()
    assert receipt.frame_count == 1
    assert receipt.duplicate_frame_count == 1


def test_monitor_requires_a_new_frame_for_each_snapshot() -> None:
    monitor = LiveMonitor(
        source_id="local-monitor",
        session_id="test-session",
        sdk_tick_rate_hz=60,
    )
    monitor.feed(_frame(100, in_car=False))
    monitor.snapshot()

    with pytest.raises(LiveMonitorError, match="NO_NEW_FRAME"):
        monitor.snapshot()


def test_monitor_requires_final_frame_projection_before_receipt() -> None:
    monitor = LiveMonitor(
        source_id="local-monitor",
        session_id="test-session",
        sdk_tick_rate_hz=60,
    )
    monitor.feed(_frame(100, in_car=False))
    monitor.snapshot()
    monitor.feed(_frame(101, in_car=True))

    with pytest.raises(LiveMonitorError, match="FINAL_SNAPSHOT_REQUIRED"):
        monitor.finish()


def test_monitor_rejects_same_tick_with_changed_telemetry() -> None:
    monitor = LiveMonitor(
        source_id="local-monitor",
        session_id="test-session",
        sdk_tick_rate_hz=60,
    )
    monitor.feed(_frame(100, in_car=False))
    conflict = _frame(100, in_car=False)
    conflict.values["FuelLevel"] = 41.0

    with pytest.raises(LiveMonitorError, match="DUPLICATE_CONFLICT"):
        monitor.feed(conflict)


def test_session_info_refresh_is_not_a_duplicate_telemetry_conflict() -> None:
    monitor = LiveMonitor(source_id="local", session_id="practice", sdk_tick_rate_hz=60)
    frame = _frame(100, in_car=True)
    monitor.feed(frame)
    monitor.snapshot()
    refreshed = replace(
        frame, session_info_update=2, captured_monotonic_s=frame.captured_monotonic_s + 0.01
    )

    assert monitor.feed(refreshed) is False
    assert monitor.snapshot_pending is False
    changed = replace(refreshed, values={**refreshed.values, "FuelLevel": 41.0})
    with pytest.raises(LiveMonitorError, match="DUPLICATE_CONFLICT"):
        monitor.feed(changed)
    receipt = monitor.finish()
    assert receipt.frame_count == 1
    assert receipt.duplicate_frame_count == 1


def test_frozen_buffer_becomes_blocked_and_requires_a_final_health_snapshot() -> None:
    monitor = LiveMonitor(source_id="local", session_id="practice", sdk_tick_rate_hz=60)
    first = _frame(100, in_car=True)
    monitor.feed(first)
    snapshots = [monitor.snapshot()]
    last = _frame(101, in_car=True)
    monitor.feed(last)
    snapshots.append(monitor.snapshot())
    assert snapshots[-1]["status"] == "READY"
    progress_at = last.captured_monotonic_s
    assert monitor.feed(replace(last, captured_monotonic_s=progress_at + 0.5)) is False
    assert not monitor.snapshot_pending
    monitor.feed(replace(last, captured_monotonic_s=progress_at + 0.51))

    with pytest.raises(LiveMonitorError, match="FINAL_SNAPSHOT_REQUIRED"):
        monitor.finish()
    snapshots.append(monitor.snapshot())
    blocked = snapshots[-1]
    assert blocked["status"] == "BLOCKED"
    assert blocked["quality"]["status"] == "REJECTED"
    assert blocked["quality"]["stale"] is True
    assert "SOURCE_STALE" in blocked["reasons"]
    assert "source_stale" in [event["kind"] for event in blocked["events"]]
    assert blocked["telemetry"] == snapshots[-2]["telemetry"]
    assert blocked["session_time_us"] == snapshots[-2]["session_time_us"]
    for index in range(100):
        monitor.feed(replace(last, captured_monotonic_s=progress_at + 1.0 + index))
        assert not monitor.snapshot_pending

    receipt = monitor.finish()
    assert monitor.finish() is receipt
    assert receipt.frame_count == 2
    assert receipt.duplicate_frame_count == 102
    assert receipt.snapshot_count == 3
    assert receipt.in_car_snapshot_count == 2
    assert receipt.final_status == "BLOCKED"
    assert receipt.event_receipt.sample_count == 3
    assert dict(receipt.event_receipt.event_kind_counts)["source_stale"] == 1
    assert all(item["snapshot_sha256"] == _snapshot_digest(item) for item in snapshots)
    stream = b"".join(
        json.dumps(
            item, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n"
        for item in snapshots
    )
    assert receipt.snapshots_sha256 == hashlib.sha256(stream).hexdigest()
    material = receipt.to_dict()
    expected_hash = material.pop("receipt_sha256")
    assert expected_hash == hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_frozen_buffer_recovers_only_when_new_samples_resume() -> None:
    monitor = LiveMonitor(source_id="local", session_id="practice", sdk_tick_rate_hz=60)
    last = _frame(100, in_car=True)
    monitor.feed(last)
    monitor.snapshot()
    monitor.advance_time(last.captured_monotonic_s + 0.6)
    assert monitor.snapshot()["status"] == "BLOCKED"
    resumed_at = last.captured_monotonic_s + 0.7
    monitor.feed(replace(_frame(101, in_car=True), captured_monotonic_s=resumed_at))
    assert monitor.snapshot()["status"] == "BLOCKED"
    monitor.feed(replace(_frame(102, in_car=True), captured_monotonic_s=resumed_at + 1 / 60))
    resumed = monitor.snapshot()

    assert resumed["status"] == "READY"
    assert resumed["quality"]["stale"] is False
    assert "source_resumed" in [event["kind"] for event in resumed["events"]]
    receipt = monitor.finish()
    assert receipt.frame_count == 3
    assert receipt.event_receipt.sample_count == 4
    assert dict(receipt.event_receipt.event_kind_counts)["source_stale"] == 1


@pytest.mark.parametrize("clock", [None, True, -1, float("nan"), float("inf")])
def test_monitor_requires_a_finite_observation_clock(clock) -> None:
    monitor = LiveMonitor(source_id="local", session_id="practice", sdk_tick_rate_hz=60)
    with pytest.raises(LiveMonitorError, match="OBSERVATION_TIME_INVALID"):
        monitor.feed(replace(_frame(100, in_car=True), captured_monotonic_s=clock))


def test_timer_observation_clock_cannot_regress() -> None:
    monitor = LiveMonitor(source_id="local", session_id="practice", sdk_tick_rate_hz=60)
    monitor.feed(_frame(100, in_car=True), observed_monotonic_s=10.0)
    with pytest.raises(LiveMonitorError, match="OBSERVATION_TIME_REGRESSION"):
        monitor.advance_time(9.0)


def test_session_time_rejection_matches_snapshot_and_recovers_on_next_frame() -> None:
    monitor = LiveMonitor(source_id="local", session_id="practice", sdk_tick_rate_hz=60)
    monitor.feed(_frame(100, in_car=True))
    monitor.snapshot()
    regressed = _frame(101, in_car=True)
    regressed.values["SessionTime"] = 0.0
    monitor.feed(regressed)
    rejected = monitor.snapshot()

    assert rejected["status"] == "BLOCKED"
    assert rejected["quality"]["status"] == "REJECTED"
    assert "SESSION_TIME_REGRESSION" in rejected["reasons"]
    assert "quality_rejected" in [event["kind"] for event in rejected["events"]]
    monitor.feed(_frame(102, in_car=True))
    recovered = monitor.snapshot()
    assert recovered["status"] == "READY"
    assert "SESSION_TIME_REGRESSION" not in recovered["reasons"]
    assert monitor.finish().event_receipt.rejected_sample_count == 1


def test_source_reset_details_do_not_reveal_raw_identity() -> None:
    monitor = LiveMonitor(
        source_id="racer-private-rig", session_id="private-session", sdk_tick_rate_hz=60
    )
    monitor.feed(_frame(100, in_car=False))
    snapshots = [monitor.snapshot()]
    monitor.feed(_frame(101, in_car=False, sim_mode="replay"))
    snapshots.append(monitor.snapshot())
    reset = next(event for event in snapshots[-1]["events"] if event["kind"] == "source_reset")
    assert reset["details"] == {
        "previous_schema": "normalized-telemetry-v3",
        "previous_source_kind": "SDK_LIVE",
        "reasons": ["SOURCE_KIND_CHANGED"],
    }
    serialized = json.dumps((snapshots, monitor.finish().to_dict()))
    assert "racer-private-rig" not in serialized
    assert "private-session" not in serialized
    assert "source_id" not in serialized
    assert "session_id" not in serialized


@pytest.mark.parametrize("source_id", ["", " padded", "padded ", "bad\nvalue"])
def test_monitor_rejects_ambiguous_source_identifier(source_id: str) -> None:
    with pytest.raises(LiveMonitorError, match="SOURCE_ID_INVALID"):
        LiveMonitor(
            source_id=source_id,
            session_id="test-session",
            sdk_tick_rate_hz=60,
        )


def test_monitor_blocks_stale_session_clock() -> None:
    monitor = LiveMonitor(
        source_id="local-monitor",
        session_id="test-session",
        sdk_tick_rate_hz=60,
    )
    monitor.feed(_frame(100, in_car=True))
    monitor.snapshot()
    monitor.feed(_frame(101, in_car=True, session_tick=100))
    stale = monitor.snapshot()

    assert stale["status"] == "BLOCKED"
    assert stale["quality"]["stale"] is True
    assert "SOURCE_STALE" in stale["quality"]["issues"]


def test_monitor_enforces_expected_source_kind() -> None:
    monitor = LiveMonitor(
        source_id="local-monitor",
        session_id="test-session",
        sdk_tick_rate_hz=60,
        expected_source_kind=SourceKind.SDK_LIVE,
    )

    with pytest.raises(LiveMonitorError, match="SOURCE_KIND_MISMATCH"):
        monitor.feed(_frame(100, in_car=False, sim_mode="replay"))


def test_replay_monitor_remains_wait_car() -> None:
    monitor = LiveMonitor(
        source_id="local-monitor",
        session_id="test-session",
        sdk_tick_rate_hz=60,
        expected_source_kind=SourceKind.REPLAY_SDK_PROXY,
    )
    monitor.feed(_frame(100, in_car=False, sim_mode="replay"))
    snapshot = monitor.snapshot()

    assert snapshot["source_kind"] == "REPLAY_SDK_PROXY"
    assert snapshot["status"] == "WAIT_CAR"
    assert snapshot["context"]["sim_source_mode"] == "REPLAY_FILE"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Transport:
    def __init__(self, frames: list[RawSdkFrame], clock: _Clock) -> None:
        self.frames = frames
        self.clock = clock
        self.index = 0
        self.closed = False
        self.requested_fields: list[tuple[str, ...]] = []
        self.schema = _descriptors(frames[0].values)

    def startup(self, timeout_s: float) -> SimpleNamespace:
        assert timeout_s == 0.0
        return SimpleNamespace(tick_rate_hz=60)

    def descriptors(self) -> tuple[VariableDescriptor, ...]:
        return self.schema

    def read_frozen(self, fields: tuple[str, ...]) -> RawSdkFrame:
        self.requested_fields.append(fields)
        frame = self.frames[self.index]
        self.index += 1
        self.clock.now += 0.004
        return frame

    def sim_mode(self) -> tuple[str, int]:
        frame = self.frames[self.index - 1]
        return str(frame.sim_mode_raw), frame.session_info_update

    @property
    def connected(self) -> bool:
        return not self.closed and self.index < len(self.frames)

    def close(self) -> None:
        self.closed = True


def test_transport_normalizes_every_tick_but_emits_bounded_snapshots() -> None:
    clock = _Clock()
    transport = _Transport(
        [_frame(100, in_car=False), _frame(101, in_car=True)],
        clock,
    )
    snapshots: list[dict[str, object]] = []

    receipt = monitor_live_transport(
        transport,
        emit=snapshots.append,
        source_id="local-monitor",
        session_id="test-session",
        expected_source_kind=SourceKind.SDK_LIVE,
        wait_seconds=0.0,
        duration_s=10.0,
        poll_seconds=0.01,
        snapshot_seconds=0.5,
        stale_after_s=0.5,
        max_reads=2,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert [item["status"] for item in snapshots] == ["WAIT_CAR", "READY"]
    assert receipt.frame_count == 2
    assert receipt.snapshot_count == 2
    assert receipt.in_car_snapshot_count == 1
    assert clock.sleeps == [pytest.approx(0.006)]
    assert transport.closed is True
    assert all("UserName" not in fields for fields in transport.requested_fields)


def test_transport_emits_stale_state_even_without_a_new_buffer_tick() -> None:
    clock = _Clock()
    last = _frame(101, in_car=True)
    transport = _Transport([_frame(100, in_car=True), last, *[last] * 10], clock)
    snapshots: list[dict[str, object]] = []

    receipt = monitor_live_transport(
        transport,
        emit=snapshots.append,
        source_id="local",
        session_id="practice",
        wait_seconds=0,
        duration_s=10,
        poll_seconds=0.1,
        snapshot_seconds=0.05,
        max_reads=len(transport.frames),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert [item["status"] for item in snapshots] == ["DEGRADED", "READY", "BLOCKED"]
    assert receipt.final_status == "BLOCKED"
    assert receipt.frame_count == 2
    assert receipt.duplicate_frame_count == 10
    assert receipt.event_receipt.sample_count == 3
    assert transport.closed


def test_transport_counts_normalization_and_emit_time_against_poll_budget(monkeypatch) -> None:
    clock = _Clock()
    transport = _Transport([_frame(100, in_car=True), _frame(101, in_car=True)], clock)
    original_normalize = live_monitor.normalize_sdk_frame

    def normalize(*args, **kwargs):
        sample = original_normalize(*args, **kwargs)
        clock.now += 0.002
        return sample

    def emit(snapshot):
        clock.now += 0.003

    monkeypatch.setattr(live_monitor, "normalize_sdk_frame", normalize)
    monitor_live_transport(
        transport,
        emit=emit,
        source_id="local",
        session_id="practice",
        wait_seconds=0,
        duration_s=1,
        poll_seconds=0.01,
        max_reads=2,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert clock.sleeps == [pytest.approx(0.001)]
    assert transport.closed


def test_transport_rechecks_staleness_after_final_wait() -> None:
    clock = _Clock()
    transport = _Transport([_frame(100, in_car=True)], clock)
    snapshots: list[dict[str, object]] = []
    receipt = monitor_live_transport(
        transport,
        emit=snapshots.append,
        source_id="local",
        session_id="practice",
        wait_seconds=0,
        duration_s=0.75,
        poll_seconds=1.0,
        snapshot_seconds=1.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert clock.now == 0.75
    assert [item["status"] for item in snapshots] == ["DEGRADED", "BLOCKED"]
    assert receipt.final_status == "BLOCKED"
    assert receipt.frame_count == 1
    assert receipt.in_car_snapshot_count == 1
    assert transport.closed


def test_slow_final_snapshot_sink_cannot_leave_a_ready_terminal_receipt() -> None:
    clock = _Clock()
    transport = _Transport([_frame(100, in_car=True), _frame(101, in_car=True)], clock)
    snapshots: list[dict[str, object]] = []

    def emit(snapshot):
        snapshots.append(snapshot)
        if len(snapshots) == 2:
            clock.now += 1.0

    receipt = monitor_live_transport(
        transport,
        emit=emit,
        source_id="local",
        session_id="practice",
        wait_seconds=0,
        duration_s=10,
        max_reads=2,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert [item["status"] for item in snapshots] == ["DEGRADED", "READY", "BLOCKED"]
    assert receipt.final_status == "BLOCKED"
    assert receipt.frame_count == 2
    assert receipt.event_receipt.sample_count == 3


@dataclass
class _CliReceipt:
    in_car_snapshot_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": LIVE_MONITOR_CONTRACT_VERSION,
            "in_car_snapshot_count": self.in_car_snapshot_count,
        }


def test_monitor_live_cli_streams_jsonl_and_maps_arguments(monkeypatch, capsys) -> None:
    transport = object()
    observed: dict[str, object] = {}
    monkeypatch.setattr(cli, "WindowsPyirsdkTransport", lambda: transport)

    def run(received_transport, **kwargs):
        observed.update({"transport": received_transport, **kwargs})
        kwargs["emit"](
            {
                "contract_version": LIVE_MONITOR_CONTRACT_VERSION,
                "record_type": "live_monitor_snapshot",
                "status": "READY",
            }
        )
        return _CliReceipt(in_car_snapshot_count=1)

    monkeypatch.setattr(cli, "monitor_live_transport", run)

    exit_code = cli.main(
        [
            "monitor-live",
            "--source-id",
            "rig-a",
            "--session-id",
            "session-a",
            "--expected-source-kind",
            "live",
            "--wait-seconds",
            "1",
            "--duration-seconds",
            "4",
            "--poll-seconds",
            "0.02",
            "--snapshot-seconds",
            "0.25",
            "--stale-after-seconds",
            "0.75",
            "--require-in-car",
        ]
    )

    output = capsys.readouterr()
    records = [json.loads(line) for line in output.out.splitlines()]
    assert exit_code == 0
    assert output.err == ""
    assert [item["record_type"] for item in records] == [
        "live_monitor_snapshot",
        "live_monitor_receipt",
    ]
    assert observed == {
        "transport": transport,
        "emit": observed["emit"],
        "source_id": "rig-a",
        "session_id": "session-a",
        "expected_source_kind": SourceKind.SDK_LIVE,
        "wait_seconds": 1.0,
        "duration_s": 4.0,
        "poll_seconds": 0.02,
        "snapshot_seconds": 0.25,
        "stale_after_s": 0.75,
    }


def test_monitor_live_cli_require_in_car_returns_five(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "WindowsPyirsdkTransport", object)
    monkeypatch.setattr(
        cli,
        "monitor_live_transport",
        lambda _transport, **_kwargs: _CliReceipt(in_car_snapshot_count=0),
    )

    exit_code = cli.main(
        [
            "monitor-live",
            "--source-id",
            "rig-a",
            "--session-id",
            "session-a",
            "--require-in-car",
        ]
    )

    assert exit_code == 5
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["record_type"] == "live_monitor_receipt"
    assert receipt["receipt"]["in_car_snapshot_count"] == 0


def test_monitor_live_cli_error_is_one_jsonl_record(monkeypatch, capsys) -> None:
    def unavailable():
        raise SdkProbeUnavailable("simulator absent")

    monkeypatch.setattr(cli, "WindowsPyirsdkTransport", unavailable)

    exit_code = cli.main(
        [
            "monitor-live",
            "--source-id",
            "rig-a",
            "--session-id",
            "session-a",
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 2
    assert output.err.strip() == "simulator absent"
    assert len(output.out.splitlines()) == 1
    error = json.loads(output.out)
    assert error == {
        "contract_version": LIVE_MONITOR_CONTRACT_VERSION,
        "error": "SDK_UNAVAILABLE",
        "message": "simulator absent",
        "record_type": "live_monitor_error",
    }


def test_monitor_live_parser_rejects_nonpositive_snapshot_interval(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._parser().parse_args(
            [
                "monitor-live",
                "--source-id",
                "rig-a",
                "--session-id",
                "session-a",
                "--snapshot-seconds",
                "0",
            ]
        )

    assert exc_info.value.code == 2
    assert "finite number greater than zero" in capsys.readouterr().err
