"""Offline protocol and owned-process tests; no simulator or SDK-live evidence."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from iracing_ai_engineer import crewchief_transport as module
from iracing_ai_engineer.collector import collect_transport_to_jsonl
from iracing_ai_engineer.sdk_probe import (
    SDK_TYPE_NAMES,
    SDK_TYPE_SIZES,
    SdkProbeConsistencyError,
    SdkProbeUnavailable,
)


def _packet(*, tick: int = 100, update: int = 7, mode: str = "full") -> dict[str, object]:
    fields = [
        ("SessionTime", 5, 1, tick / 60),
        ("SessionTick", 2, 1, tick),
        ("SessionNum", 2, 1, 0),
        ("FuelLevel", 4, 1, 42.5),
        ("IsOnTrackCar", 1, 1, True),
        ("CarIdxLapCompleted", 2, 2, [2, 1]),
        ("Label", 0, 8, "test"),
        ("SessionFlags", 3, 1, 2**32 - 1),
    ]
    descriptors, values = [], {}
    offset = 0
    for name, kind, count, value in fields:
        descriptors.append({
            "name": name, "type_code": kind, "dtype": SDK_TYPE_NAMES[kind],
            "offset": offset, "count": count, "count_as_time": False,
            "unit": "", "description": "Synthetic test field",
        })
        offset += SDK_TYPE_SIZES[kind] * count
        values[name] = value
    session = (
        f"---\nWeekendInfo:\n Encoding: UTF8\n SimMode: {mode}\n TrackLength: 5 km\n"
        "DriverInfo:\n Drivers:\n - UserName: synthetic-private-name\n"
    ).encode()
    return {
        "protocol": module.PROTOCOL_VERSION, "status": "ok",
        "connection": {
            "header_version": 2, "raw_header_status": 1, "tick_rate_hz": 60,
            "variable_count": len(descriptors), "buffer_count": 3, "buffer_len": offset,
        },
        "buffer_tick": tick, "session_info_update": update,
        "session_info_b64": base64.b64encode(session).decode(),
        "descriptors": descriptors, "values": values, "read_errors": [],
    }


def _line(packet: dict[str, object]) -> bytes:
    return (json.dumps(packet, allow_nan=False) + "\n").encode()


def _transport(*packets: dict[str, object]) -> module.WindowsCrewChiefTransport:
    responses = iter(_line(packet) for packet in packets)
    return module.WindowsCrewChiefTransport(
        Path("synthetic-reader.exe"), _exchange=lambda timeout: next(responses)
    )


def test_startup_first_frame_and_session_cache_are_the_same_packet(monkeypatch) -> None:
    calls = []
    responses = iter([_line(_packet()), _line(_packet(tick=101, update=8, mode="replay"))])
    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def exchange(timeout: float) -> bytes:
        calls.append(timeout)
        return next(responses)

    transport = module.WindowsCrewChiefTransport(Path("synthetic-reader.exe"), _exchange=exchange)
    connection = transport.startup(0)
    assert connection.tick_rate_hz == 60
    assert len(transport.descriptors()) == connection.variable_count == 8
    assert calls == [module.READ_TIMEOUT_S]
    clock[0] = 15.0
    frame = transport.read_frozen(("SessionTick", "CarIdxLapCompleted", "SessionFlags"))
    assert frame.buffer_tick == frame.values["SessionTick"] == 100
    assert frame.values["CarIdxLapCompleted"] == (2, 1)
    assert frame.values["SessionFlags"] == 2**32 - 1
    assert frame.captured_monotonic_s == 10.0  # Must not invent a fresh receive time.
    assert len(calls) == 1
    assert transport.sim_mode() == ("full", 7)
    metadata, update = transport.session_info_snapshot()
    assert update == 7 and "DriverInfo" in metadata
    metadata["WeekendInfo"]["SimMode"] = "mutated"
    assert transport.sim_mode() == ("full", 7)

    next_frame = transport.read_frozen(("SessionTick",))
    assert next_frame.buffer_tick == 101
    assert next_frame.captured_monotonic_s == 15.0
    assert transport.sim_mode() == ("replay", next_frame.session_info_update)
    assert transport.session_info_snapshot()[1] == 8
    transport.close()
    assert not transport.connected
    assert transport.sim_mode() == transport.session_info_snapshot() == (None, None)


def test_startup_retries_native_lowercase_unavailable_packets() -> None:
    unavailable = {
        "protocol": module.PROTOCOL_VERSION, "status": "unavailable",
        "error_code": "mapping_unavailable",
    }
    transport = _transport(unavailable, unavailable, _packet())
    assert transport.startup(0.5).connected is True
    transport.close()


def test_zero_wait_makes_one_bounded_attempt_then_reports_unavailable() -> None:
    unavailable = {
        "protocol": module.PROTOCOL_VERSION, "status": "unavailable",
        "error_code": "sdk_disconnected",
    }
    transport = _transport(unavailable)
    with pytest.raises(SdkProbeUnavailable, match="CREWCHIEF_SDK_CONNECTION_TIMEOUT"):
        transport.startup(0)
    assert not transport.connected


def test_reader_exception_details_are_not_exposed() -> None:
    def exchange(timeout: float) -> bytes:
        raise OSError("synthetic-private-path-or-credential")

    transport = module.WindowsCrewChiefTransport(Path("synthetic.exe"), _exchange=exchange)
    with pytest.raises(SdkProbeUnavailable, match="CREWCHIEF_READER_IO_FAILED") as raised:
        transport.startup(0)
    assert "private" not in str(raised.value)
    assert not transport.connected


@pytest.mark.parametrize("key", ["FuelLevel", "IsOnTrackCar", "CarIdxLapCompleted"])
@pytest.mark.parametrize("keep_null", [False, True])
def test_missing_or_null_values_need_explicit_read_errors(key: str, keep_null: bool) -> None:
    packet = _packet()
    if keep_null:
        packet["values"][key] = None
    else:
        packet["values"].pop(key)
    with pytest.raises(SdkProbeConsistencyError):
        module._decode_snapshot(_line(packet), captured_monotonic_s=1.0)
    packet["read_errors"] = [key]
    decoded = module._decode_snapshot(_line(packet), captured_monotonic_s=1.0)
    assert key not in decoded.frame.values
    assert decoded.frame.read_errors == (key,)


@pytest.mark.parametrize(
    "key,value",
    [
        ("FuelLevel", True), ("FuelLevel", 1e39), ("IsOnTrackCar", 1),
        ("SessionTick", True), ("SessionTick", 2**31), ("SessionFlags", -1),
        ("CarIdxLapCompleted", [1]), ("CarIdxLapCompleted", [1, False]),
        ("Label", "longer-than-eight"), ("Label", "\u0100"), ("Label", [65]),
    ],
)
def test_values_follow_sdk_type_and_cardinality(key: str, value: object) -> None:
    packet = _packet()
    packet["values"][key] = value
    with pytest.raises(SdkProbeConsistencyError):
        module._decode_snapshot(_line(packet), captured_monotonic_s=1.0)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "1e309"])
def test_nonfinite_json_is_rejected(constant: str) -> None:
    line = _line(_packet()).replace(b'"FuelLevel": 42.5', f'"FuelLevel": {constant}'.encode())
    with pytest.raises(SdkProbeConsistencyError):
        module._decode_snapshot(line, captured_monotonic_s=1.0)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(status=[]),
        lambda p: p.update(protocol="other-protocol"),
        lambda p: p.update(extra="private-value"),
        lambda p: p["connection"].update(header_version=3),
        lambda p: p["connection"].update(tick_rate_hz=True),
        lambda p: p["connection"].update(buffer_len=4 * 1024 * 1024 + 1),
        lambda p: p["connection"].update(variable_count=1),
        lambda p: p["descriptors"][1].update(offset=0),
        lambda p: p["descriptors"][1].update(count=True),
        lambda p: p["descriptors"][1].update(count_as_time=1),
        lambda p: p["descriptors"][1].update(dtype="wrong"),
        lambda p: p["values"].update(Unknown=123),
        lambda p: p.update(read_errors=["Unknown"]),
        lambda p: p.update(read_errors=["FuelLevel", "FuelLevel"]),
        lambda p: p.update(read_errors=["FuelLevel"]),
        lambda p: p.update(session_info_update=True),
        lambda p: p.update(buffer_tick=-1),
    ],
)
def test_malformed_protocol_is_fail_closed(mutate) -> None:
    packet = _packet()
    mutate(packet)
    with pytest.raises(SdkProbeConsistencyError):
        module._decode_snapshot(_line(packet), captured_monotonic_s=1.0)


def test_duplicate_keys_and_oversized_line_are_rejected(monkeypatch) -> None:
    line = _line(_packet())
    duplicate = line.replace(b'"status": "ok"', b'"status": "ok", "status": "ok"')
    with pytest.raises(SdkProbeConsistencyError):
        module._decode_snapshot(duplicate, captured_monotonic_s=1.0)
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", len(line) - 1)
    with pytest.raises(SdkProbeConsistencyError):
        module._decode_snapshot(line, captured_monotonic_s=1.0)


@pytest.mark.parametrize("raw", [b"[broken", b"- list-root", b"recursive: &a [*a]", b"x: .nan"])
def test_invalid_or_recursive_session_yaml_is_rejected(raw: bytes) -> None:
    packet = _packet()
    packet["session_info_b64"] = base64.b64encode(raw).decode()
    with pytest.raises(SdkProbeConsistencyError, match="CREWCHIEF_SESSION_INFO_INVALID"):
        module._decode_snapshot(_line(packet), captured_monotonic_s=1.0)


def test_legacy_cp1252_session_parsing_preserves_sdk_cleaning() -> None:
    packet = _packet()
    packet["session_info_b64"] = base64.b64encode(
        b"---\nWeekendInfo:\n SimMode: full\nDriverInfo:\n UserName: Caf\xe9: driver\n"
    ).decode()
    decoded = module._decode_snapshot(_line(packet), captured_monotonic_s=1.0)
    assert decoded.session_info["DriverInfo"]["UserName"] == "Café: driver"


@pytest.mark.parametrize("change_schema", [False, True])
def test_connection_or_schema_change_fails_after_first_packet(change_schema: bool) -> None:
    changed = _packet(tick=101)
    if change_schema:
        changed["descriptors"][3]["unit"] = "different"
    else:
        changed["connection"]["tick_rate_hz"] = 120
    transport = _transport(_packet(), changed)
    transport.startup(0)
    transport.read_frozen(("SessionTick",))
    with pytest.raises(SdkProbeConsistencyError, match="CREWCHIEF_SCHEMA_CHANGED"):
        transport.read_frozen(("SessionTick",))
    assert not transport.connected


def test_mid_capture_disconnect_invalidates_session_cache() -> None:
    disconnected = {
        "protocol": module.PROTOCOL_VERSION, "status": "unavailable",
        "error_code": "sdk_disconnected",
    }
    transport = _transport(_packet(), disconnected)
    transport.startup(0)
    transport.read_frozen(("SessionTick",))
    with pytest.raises(SdkProbeUnavailable):
        transport.read_frozen(("SessionTick",))
    assert not transport.connected
    assert transport.sim_mode() == transport.session_info_snapshot() == (None, None)


def test_native_inconsistent_packet_is_terminal_and_does_not_echo_code() -> None:
    transport = _transport({
        "protocol": module.PROTOCOL_VERSION, "status": "inconsistent",
        "error_code": "snapshot_changed",
    })
    with pytest.raises(SdkProbeConsistencyError, match="CREWCHIEF_SDK_INCONSISTENT"):
        transport.startup(0)
    assert not transport.connected


def test_collector_owns_driver_info_privacy_filter(tmp_path: Path) -> None:
    output = tmp_path / "private-capture.jsonl"
    receipt = collect_transport_to_jsonl(
        _transport(_packet()), output, source_id="synthetic-crewchief-protocol",
        session_id="synthetic-session", wait_seconds=0, duration_s=60, max_reads=1,
        fsync_each_record=False,
    )
    assert receipt.completion_status == "COMPLETE" and receipt.frame_record_count == 1
    text = output.read_text(encoding="utf-8")
    assert "synthetic-private-name" not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows[-1]["receipt"] == asdict(receipt)


class _FakeProcess:
    def __init__(self, output: bytes | None, *, delay_s: float = 0.0) -> None:
        self.stdin = io.BytesIO()
        self.killed = threading.Event()
        self.kill_count = 0
        self.returncode = None
        process = self

        class Output(io.BytesIO):
            def readline(self, size: int = -1) -> bytes:
                if output is None:
                    process.killed.wait(5)
                    return b""
                if delay_s:
                    time.sleep(delay_s)
                return super().readline(size)

        self.stdout = Output(output or b"")

    def poll(self):
        return self.returncode

    def kill(self):
        self.kill_count += 1
        self.returncode = -1
        self.killed.set()

    def wait(self, timeout: float):
        return self.returncode


def test_owned_reader_is_hidden_no_shell_and_close_is_idempotent(monkeypatch) -> None:
    process = _FakeProcess(_line(_packet()))
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    reader = module._ReaderProcess(Path("synthetic.exe"))
    assert reader.exchange(0.5) == _line(_packet())
    assert process.stdin.getvalue() == b"snapshot\n"
    assert calls[0][0] == ["synthetic.exe"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["stderr"] == subprocess.DEVNULL
    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    reader.close()
    reader.close()
    assert process.kill_count == 1
    assert process.stdin.closed and process.stdout.closed
    assert not reader._worker.is_alive()


@pytest.mark.parametrize("output,expected", [(None, "TIMEOUT"), (b"", "EOF")])
def test_hanging_or_eof_reader_is_bounded_and_killed(monkeypatch, output, expected) -> None:
    process = _FakeProcess(output)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    reader = module._ReaderProcess(Path("synthetic.exe"))
    began = time.monotonic()
    with pytest.raises(SdkProbeUnavailable, match=expected):
        reader.exchange(0.02)
    assert time.monotonic() - began < 1.5
    assert process.kill_count == 1 and not reader.alive
    assert not reader._worker.is_alive()


def test_zero_wait_allows_bounded_native_cold_start(monkeypatch, tmp_path: Path) -> None:
    process = _FakeProcess(_line(_packet()), delay_s=0.1)
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    executable = tmp_path / "synthetic.exe"
    executable.touch()
    transport = module.WindowsCrewChiefTransport(executable)
    assert transport.startup(0).connected
    transport.close()
    assert process.kill_count == 1


def test_bad_startup_response_kills_owned_child(monkeypatch, tmp_path: Path) -> None:
    process = _FakeProcess(b'{"private-content":"not a protocol packet"}\n')
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    executable = tmp_path / "synthetic.exe"
    executable.touch()
    transport = module.WindowsCrewChiefTransport(executable)
    with pytest.raises(SdkProbeConsistencyError, match="CREWCHIEF_PROTOCOL_INVALID"):
        transport.startup(0)
    assert process.kill_count == 1
    assert not transport.connected


def test_read_does_not_expose_mutable_cache() -> None:
    transport = _transport(_packet())
    transport.startup(0)
    frame = transport.read_frozen(("FuelLevel",))
    frame.values["FuelLevel"] = 0
    assert transport._latest.frame.values["FuelLevel"] == 42.5
    transport.close()
