"""Compile and test the extracted C# reader without opening the simulator map.

The C# harness uses only synthetic byte arrays. It is a separate executable,
never a fixture mode in the production collector and never SDK_LIVE evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from iracing_ai_engineer.collector import CollectorSample, collect_samples_to_jsonl
from iracing_ai_engineer.crewchief_transport import _decode_snapshot
from iracing_ai_engineer.sdk_probe import SdkProbeUnavailable

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native" / "crewchief_reader"


@pytest.fixture(scope="module")
def native_binaries(tmp_path_factory):
    if sys.platform != "win32":
        pytest.skip("native extraction tests require the Windows .NET Framework compiler")
    framework_root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET"
    compiler = next(
        (
            candidate
            for architecture in ("Framework64", "Framework")
            if (candidate := framework_root / architecture / "v4.0.30319" / "csc.exe")
            .is_file()
        ),
        None,
    )
    if compiler is None:
        pytest.skip(".NET Framework compiler is not installed; no automatic download")
    output = tmp_path_factory.mktemp("crewchief-native")
    sources = sorted(NATIVE.glob("*.cs"))
    assert sources, "the checked-in native extraction must be present"
    programs = {}
    for name, entrypoint, extra in (
        ("reader", "Aeis.CrewChiefReader.Program", []),
        (
            "tests",
            "Aeis.CrewChiefReader.Tests.ReaderTests",
            [NATIVE / "tests" / "ReaderTests.cs"],
        ),
    ):
        executable = output / f"{name}.exe"
        result = subprocess.run(
            [
                str(compiler), "/nologo", "/langversion:5", "/target:exe",
                "/platform:anycpu", "/optimize+", "/warnaserror+",
                "/r:System.Web.Extensions.dll", f"/main:{entrypoint}",
                f"/out:{executable}", *(str(path) for path in [*sources, *extra]),
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        programs[name] = executable
    return programs


def test_native_reader_synthetic_snapshot_contract(native_binaries):
    result = subprocess.run(
        [str(native_binaries["tests"])],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS 64 native synthetic test groups" in result.stdout


def test_native_cached_packets_preserve_exact_protocol_bytes(native_binaries):
    result = subprocess.run(
        [str(native_binaries["tests"]), "--emit-cached-snapshots"],
        capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    packets = result.stdout.splitlines(keepends=True)
    assert len(packets) == 3
    assert packets[0] == packets[1] == packets[2]
    decoded = [_decode_snapshot(packet, captured_monotonic_s=0.0) for packet in packets]
    assert decoded[0] == decoded[1] == decoded[2]


def test_native_reader_eof_does_not_connect_or_emit_data(native_binaries):
    result = subprocess.run(
        [str(native_binaries["reader"])], input="",
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_native_reader_has_no_fixture_or_alternate_map_entrypoint(native_binaries):
    result = subprocess.run(
        [str(native_binaries["reader"]), "--fixture"], input="",
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode != 0
    assert "buffer_tick" not in result.stdout


def test_native_failure_packet_matches_python_protocol(native_binaries):
    # Invalid requests cannot open the simulator map, so this is always offline.
    result = subprocess.run(
        [str(native_binaries["reader"])], input=b"invalid\n",
        capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout)["error_code"] == "invalid_request"
    with pytest.raises(SdkProbeUnavailable):
        _decode_snapshot(result.stdout, captured_monotonic_s=0.0)


def test_native_synthetic_packet_reaches_existing_collector(native_binaries, tmp_path):
    result = subprocess.run(
        [str(native_binaries["tests"]), "--emit-snapshot"],
        capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    packet = _decode_snapshot(result.stdout, captured_monotonic_s=0.0)
    sample = CollectorSample(
        frame=packet.frame, descriptors=packet.descriptors,
        tick_rate_hz=packet.connection.tick_rate_hz,
        session_info=packet.session_info,
    )
    output = tmp_path / "synthetic-contract-only.jsonl"
    receipt = collect_samples_to_jsonl(
        [sample], output, source_id="synthetic-native-contract",
        session_id="offline-test", include_driver_info=False,
    )
    assert receipt.completion_status == "COMPLETE"
    assert receipt.frame_record_count == 1
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    frame = next(record for record in records if record["record_type"] == "frame")
    assert frame["buffer_tick"] == packet.frame.buffer_tick
    session = next(record for record in records if record["record_type"] == "session_info")
    assert "DriverInfo" not in session["payload"]


def test_native_reader_sources_retain_license_and_no_control_imports():
    license_text = (NATIVE / "LICENSE.CrewChief").read_text(encoding="utf-8")
    assert "Britton IT Ltd" in license_text
    assert "Permission is hereby granted" in license_text
    assert "150c8107ad03af621afec83712e96109cf2a3a93" in (
        NATIVE / "UPSTREAM.md"
    ).read_text(encoding="utf-8")
    source = "\n".join(path.read_text(encoding="utf-8-sig") for path in NATIVE.glob("*.cs"))
    assert "MemoryMappedFileRights.Read" in source
    assert "MemoryMappedFileAccess.Read" in source
    for forbidden in (
        "DllImport(", "SendNotifyMessage(", "RegisterWindowMessage(",
        "BroadcastMessage(", "Process.Start(", "MemoryMappedFileAccess.ReadWrite",
    ):
        assert forbidden not in source
