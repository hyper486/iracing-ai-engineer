from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import iracing_ai_engineer.pit_stint as _MODULE
from iracing_ai_engineer.adapters import (
    CollectorInputEvidence,
    IbtInputEvidence,
    open_ibt_telemetry,
)
from iracing_ai_engineer.events import process_telemetry_events
from iracing_ai_engineer.telemetry import SourceKind, TelemetrySample, normalize_sdk_frame

PitStintReceiptError = _MODULE.PitStintReceiptError
_build_receipt_from_samples = _MODULE._build_receipt_from_samples
_write_exclusive = _MODULE._write_exclusive
build_pit_stint_receipt = _MODULE.build_pit_stint_receipt
canonical_sha256 = _MODULE.canonical_sha256
main = _MODULE.main
validate_pit_stint_receipt = _MODULE.validate_pit_stint_receipt

SOURCE_ID = "pit-stint-fixture"
SESSION_ID = "pit-stint-session"
SOURCE_SHA = "a" * 64


def _frame(
    index: int,
    *,
    on_road: bool = False,
    in_stall: bool = False,
    active: bool = False,
    fuel_l: float = 10.0,
    session_num: int = 1,
) -> dict[str, object]:
    return {
        "FuelLevel": fuel_l,
        "Lap": 1,
        "LapCompleted": 0,
        "LapDistPct": index / 100.0,
        "OnPitRoad": on_road,
        "PitstopActive": active,
        "PlayerCarInPitStall": in_stall,
        "SessionFlags": 1,
        "SessionNum": session_num,
        "SessionTick": 1_000 + index,
        "SessionTime": index / 10.0,
    }


def _samples(
    frames: list[dict[str, object]],
    *,
    buffer_ticks: list[int] | None = None,
    source_kind: SourceKind = SourceKind.IBT_OFFLINE,
) -> list[TelemetrySample]:
    result: list[TelemetrySample] = []
    previous = None
    selected_ticks = buffer_ticks or list(range(len(frames)))
    for index, raw in enumerate(frames):
        sample = normalize_sdk_frame(
            raw,
            source_id=SOURCE_ID,
            session_id=SESSION_ID,
            source_kind=source_kind,
            buffer_tick=selected_ticks[index],
            previous=previous,
        )
        result.append(sample)
        previous = sample
    return result


def _evidence(count: int) -> IbtInputEvidence:
    return IbtInputEvidence(
        source_id=SOURCE_ID,
        session_id=SESSION_ID,
        source_sha256=SOURCE_SHA,
        byte_size=123_456,
        record_count=count,
        tick_rate_hz=10,
    )


def _collector_evidence(
    count: int,
    *,
    tick_rate_hz: int = 10,
) -> CollectorInputEvidence:
    return CollectorInputEvidence(
        source_id=SOURCE_ID,
        session_id=SESSION_ID,
        source_kind=SourceKind.SDK_LIVE,
        sim_mode="full",
        completion_status="COMPLETE",
        semantic_record_count=count + 2,
        records_sha256=SOURCE_SHA,
        frame_record_count=count,
        event_record_count=0,
        schema_record_count=1,
        session_info_record_count=0,
        samples_seen=count,
        duplicate_sample_count=0,
        duplicate_conflict_count=0,
        dropped_tick_count=0,
        stale_event_count=0,
        session_reset_count=0,
        schema_change_count=0,
        schema_epoch_count=1,
        session_epoch_count=1,
        first_buffer_tick=0,
        last_buffer_tick=count - 1,
        tick_rate_hz_values=(tick_rate_hz,),
    )


def _normalized_sha(samples: list[TelemetrySample]) -> str:
    digest = hashlib.sha256()
    for sample in samples:
        encoded = sample.to_json_line().encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
    return digest.hexdigest()


def _build(samples: list[TelemetrySample]) -> dict[str, object]:
    _, event_receipt = process_telemetry_events(samples)
    return _build_receipt_from_samples(
        iter(samples),
        input_evidence=_evidence(len(samples)),
        expected_source_sha256=SOURCE_SHA,
        expected_normalized_samples_sha256=_normalized_sha(samples),
        expected_event_receipt_sha256=event_receipt.receipt_sha256,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
    )


def _build_collector(
    samples: list[TelemetrySample],
    *,
    evidence: CollectorInputEvidence | None = None,
) -> dict[str, object]:
    selected_evidence = evidence or _collector_evidence(len(samples))
    _, event_receipt = process_telemetry_events(samples)
    return _build_receipt_from_samples(
        iter(samples),
        input_evidence=selected_evidence,
        expected_source_sha256=SOURCE_SHA,
        expected_normalized_samples_sha256=_normalized_sha(samples),
        expected_event_receipt_sha256=event_receipt.receipt_sha256,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
    )


def _complete_cycle_frames() -> list[dict[str, object]]:
    return [
        _frame(0, fuel_l=5.0),
        _frame(1, on_road=True, fuel_l=5.0),
        _frame(2, on_road=True, active=True, fuel_l=5.0),
        _frame(3, on_road=True, in_stall=True, active=True, fuel_l=8.0),
        _frame(4, on_road=True, in_stall=True, fuel_l=10.0),
        _frame(5, on_road=True, fuel_l=10.0),
        _frame(6, fuel_l=10.0),
        _frame(7, fuel_l=9.9),
    ]


def test_complete_edges_build_one_cycle_without_inventing_service_contents():
    receipt = _build(_samples(_complete_cycle_frames()))

    assert receipt["quality_gate"] == {"reasons": [], "status": "PASS"}
    assert receipt["summary"] == {
        "complete_stint_count": 0,
        "partial_stint_count": 2,
        "pit_cycle_count": 1,
        "service_episode_count": 1,
    }
    cycle = receipt["pit_cycles"][0]
    service = cycle["service_episodes"][0]
    assert cycle["pit_road"]["duration_s"] == 0.5
    assert service["active_frame_count"] == 2
    assert service["duration_s"] == 0.2
    assert service["observed_net_tank_change"] == {
        "end_fuel_level_l": 10.0,
        "interpretation": "OBSERVED_ENDPOINT_TANK_LEVEL_DIFFERENCE_NOT_DELIVERED_FUEL",
        "provenance": "SDK_DIRECT_ENDPOINT_DIFFERENCE",
        "start_fuel_level_l": 5.0,
        "value_l": 5.0,
    }
    assert service["stall_support"] == {
        "overlap_duration_s": 0.1,
        "service_starts_before_stall_s": 0.1,
        "stall_extends_after_service_s": 0.1,
        "stall_interval_id": "pit-stall:1",
        "status": "POSITIVE_OVERLAP",
    }
    assert all(
        item["provenance"] == "UNKNOWN"
        and item["status"] == "SKIP_NOT_OBSERVABLE"
        for item in service["service_contents"].values()
    )
    assert "no_tires" not in json.dumps(receipt).casefold()
    first_stint, second_stint = receipt["stints"]
    assert first_stint["observed_laps_completed_delta"] == 0
    assert first_stint["observed_laps_completed_delta_availability"] == "AVAILABLE"
    assert first_stint["observed_start_tank_level_l"] == 5.0
    assert first_stint["observed_end_tank_level_l"] == 5.0
    assert first_stint["observed_tank_level_availability"] == "AVAILABLE"
    assert first_stint["observed_endpoint_provenance"] == "SDK_DIRECT"
    assert second_stint["observed_start_tank_level_l"] == 10.0
    assert second_stint["observed_end_tank_level_l"] == 9.9


def test_service_without_stall_remains_observed_but_has_no_overlap_support():
    frames = _complete_cycle_frames()
    for raw in frames:
        raw["PlayerCarInPitStall"] = False
    receipt = _build(_samples(frames))

    service = receipt["pit_cycles"][0]["service_episodes"][0]
    assert service["stall_support"]["status"] == "NO_POSITIVE_OVERLAP"
    assert service["stall_support"]["overlap_duration_s"] == 0.0
    assert receipt["quality_gate"]["status"] == "PASS"


def test_service_outside_complete_pit_road_is_excluded_and_degraded():
    samples = _samples(
        [
            _frame(0),
            _frame(1, active=True, fuel_l=5.0),
            _frame(2, fuel_l=10.0),
        ]
    )
    receipt = _build(samples)

    assert receipt["pit_cycles"] == []
    assert receipt["summary"]["service_episode_count"] == 0
    assert "SERVICE_OUTSIDE_COMPLETE_PIT_ROAD" in receipt["quality_gate"]["reasons"]
    assert receipt["quality_gate"]["status"] == "DEGRADED"


def test_file_edge_active_interval_is_partial_not_a_complete_service():
    receipt = _build(
        _samples(
            [
                _frame(0),
                _frame(1, on_road=True),
                _frame(2, on_road=True, active=True, fuel_l=5.0),
                _frame(3, on_road=True, active=True, fuel_l=8.0),
            ]
        )
    )

    assert receipt["pit_cycles"] == []
    assert receipt["summary"]["service_episode_count"] == 0
    assert receipt["incomplete_interval_counts"]["service"] == 1
    assert receipt["incomplete_interval_counts"]["pit_road"] == 1


def test_active_at_file_start_cannot_create_a_false_start_edge():
    receipt = _build(
        _samples(
            [
                _frame(0, on_road=True, active=True, fuel_l=5.0),
                _frame(1, on_road=True, fuel_l=10.0),
                _frame(2, fuel_l=10.0),
            ]
        )
    )

    assert receipt["pit_cycles"] == []
    assert receipt["summary"]["service_episode_count"] == 0
    assert receipt["incomplete_interval_counts"]["service"] == 1


def test_missing_pitstop_channel_breaks_continuity_and_prevents_false_episode():
    frames = _complete_cycle_frames()
    del frames[3]["PitstopActive"]
    receipt = _build(_samples(frames))

    assert receipt["summary"]["service_episode_count"] == 0
    assert "REQUIRED_CHANNEL_MISSING_OR_INVALID" in receipt["quality_gate"]["reasons"]
    assert receipt["quality_gate"]["status"] == "DEGRADED"


def test_dropped_tick_breaks_all_open_interval_state():
    samples = _samples(
        _complete_cycle_frames(),
        buffer_ticks=[0, 1, 2, 4, 5, 6, 7, 8],
    )
    receipt = _build(samples)

    assert receipt["summary"]["service_episode_count"] == 0
    assert "DROPPED_TICKS_OR_UNKNOWN" in receipt["quality_gate"]["reasons"]
    assert receipt["quality_gate"]["status"] == "DEGRADED"


def test_session_reset_breaks_open_service_state():
    frames = _complete_cycle_frames()
    for index in range(3, len(frames)):
        frames[index]["SessionNum"] = 2
    receipt = _build(_samples(frames))

    assert receipt["summary"]["service_episode_count"] == 0
    assert "SHARED_EVENT_CONTINUITY_BREAK" in receipt["quality_gate"]["reasons"]
    assert receipt["quality_gate"]["status"] == "DEGRADED"


def test_tire_counters_do_not_become_tire_service_evidence():
    frames = _complete_cycle_frames()
    for raw in frames:
        raw["PlayerTireCompound"] = 0
        raw["TireSetsUsed"] = 1
    receipt = _build(_samples(frames))
    serialized = json.dumps(receipt, allow_nan=False, sort_keys=True)

    tire = receipt["service_contents"]["tire_service"]
    assert tire["estimate_available"] is False
    assert tire["provenance"] == "UNKNOWN"
    assert "TireSetsUsed" not in serialized
    assert "PlayerTireCompound" not in serialized
    assert "no_tires" not in serialized.casefold()


def test_valid_no_pit_recording_waits_for_a_pit_sample_while_quality_passes():
    receipt = _build(
        _samples([_frame(index, fuel_l=10.0 - index * 0.1) for index in range(5)])
    )

    assert receipt["quality_gate"] == {"reasons": [], "status": "PASS"}
    assert receipt["pit_cycles"] == []
    assert receipt["capabilities"]["pit_and_service_detection"] == {
        "reasons": ["NO_COMPLETE_PIT_ROAD_INTERVAL_OBSERVED"],
        "status": "WAIT_PIT_SAMPLE",
    }
    assert receipt["recommendations"] == []


def test_missing_fuel_closes_stint_with_null_endpoint_evidence_and_degrades():
    frames = [_frame(0, fuel_l=10.0), _frame(1, fuel_l=9.9), _frame(2, fuel_l=9.8)]
    del frames[1]["FuelLevel"]
    receipt = _build(_samples(frames))

    broken = next(stint for stint in receipt["stints"] if stint["end"]["frame_index"] == 1)
    assert broken["status"] == "PARTIAL_CONTINUITY"
    assert broken["observed_start_tank_level_l"] is None
    assert broken["observed_end_tank_level_l"] is None
    assert broken["observed_tank_level_availability"] == "UNAVAILABLE"
    assert broken["observed_endpoint_provenance"] == "UNKNOWN"
    assert "REQUIRED_CHANNEL_MISSING_OR_INVALID" in receipt["quality_gate"]["reasons"]
    assert receipt["quality_gate"]["status"] == "DEGRADED"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("expected_source_sha256", "b" * 64, "source digest"),
        ("expected_normalized_samples_sha256", "b" * 64, "normalized stream"),
        ("expected_event_receipt_sha256", "b" * 64, "telemetry-event receipt"),
    ],
)
def test_independent_trust_root_mismatch_fails_closed(field: str, value: str, message: str):
    samples = _samples(_complete_cycle_frames())
    _, event_receipt = process_telemetry_events(samples)
    arguments = {
        "expected_source_sha256": SOURCE_SHA,
        "expected_normalized_samples_sha256": _normalized_sha(samples),
        "expected_event_receipt_sha256": event_receipt.receipt_sha256,
    }
    arguments[field] = value

    with pytest.raises(PitStintReceiptError, match=message):
        _build_receipt_from_samples(
            iter(samples),
            input_evidence=_evidence(len(samples)),
            stale_after_s=0.5,
            opponent_error_policy="degrade",
            **arguments,
        )


@pytest.mark.parametrize(
    "quality_field",
    [
        "duplicate_conflict_count",
        "dropped_tick_count",
        "stale_event_count",
        "schema_change_count",
        "session_reset_count",
        "capture_clock_regression_count",
        "read_error_frame_count",
        "driver_info_key_count",
    ],
)
def test_collector_blocking_sideband_quality_fails_before_samples_are_consumed(
    quality_field: str,
):
    evidence = replace(_collector_evidence(8), **{quality_field: 1})

    def samples_that_must_not_be_consumed():
        raise AssertionError("collector samples were consumed before sideband gating")
        yield  # pragma: no cover

    with pytest.raises(PitStintReceiptError) as raised:
        _build_receipt_from_samples(
            samples_that_must_not_be_consumed(),
            input_evidence=evidence,
            expected_source_sha256=SOURCE_SHA,
            expected_normalized_samples_sha256="b" * 64,
            expected_event_receipt_sha256="c" * 64,
            stale_after_s=0.5,
            opponent_error_policy="degrade",
        )

    assert raised.value.code == "COLLECTOR_SIDEBAND_QUALITY_FAILED"
    assert quality_field in str(raised.value)


def test_collector_exact_duplicates_and_normal_redaction_remain_admissible():
    samples = _samples(
        _complete_cycle_frames(),
        source_kind=SourceKind.SDK_LIVE,
    )
    evidence = replace(
        _collector_evidence(len(samples)),
        duplicate_sample_count=2,
        samples_seen=len(samples) + 2,
        redacted_driver_info_path_count=3,
    )

    receipt = _build_collector(samples, evidence=evidence)

    assert receipt["quality_gate"] == {"reasons": [], "status": "PASS"}
    assert receipt["summary"]["pit_cycle_count"] == 1
    assert receipt["summary"]["service_episode_count"] == 1
    assert receipt["input_evidence"]["duplicate_conflict_count"] == 0
    assert receipt["input_evidence"]["duplicate_sample_count"] == 2
    assert receipt["input_evidence"]["redacted_driver_info_path_count"] == 3


def test_collector_no_pit_partial_stint_uses_declared_60hz_session_time():
    frames = [_frame(index, fuel_l=10.0 - index * 0.1) for index in range(8)]
    for index, frame in enumerate(frames):
        frame["SessionTime"] = index / 60.0
    samples = _samples(frames, source_kind=SourceKind.SDK_LIVE)

    receipt = _build_collector(
        samples,
        evidence=_collector_evidence(len(samples), tick_rate_hz=60),
    )

    assert receipt["quality_gate"] == {"reasons": [], "status": "PASS"}
    assert receipt["pit_cycles"] == []
    assert len(receipt["stints"]) == 1
    stint = receipt["stints"][0]
    assert stint["start_boundary"] == "FILE_START"
    assert stint["end_boundary"] == "FILE_END"
    assert stint["duration_s"] == 0.116667


def test_collector_no_pit_16_67hz_session_time_fails_declared_60hz_closed():
    frames = [_frame(index, fuel_l=10.0 - index * 0.1) for index in range(8)]
    for index, frame in enumerate(frames):
        frame["SessionTime"] = index * 0.06
    samples = _samples(frames, source_kind=SourceKind.SDK_LIVE)

    with pytest.raises(PitStintReceiptError) as raised:
        _build_collector(
            samples,
            evidence=_collector_evidence(len(samples), tick_rate_hz=60),
        )

    assert raised.value.code == "TIMING_RATE_MISMATCH"


def test_public_builder_rejects_unvalidated_sample_iterables():
    with pytest.raises(PitStintReceiptError, match="open validated telemetry"):
        build_pit_stint_receipt(
            _samples(_complete_cycle_frames()),  # type: ignore[arg-type]
            expected_source_sha256=SOURCE_SHA,
            expected_normalized_samples_sha256="b" * 64,
            expected_event_receipt_sha256="c" * 64,
        )


def test_receipt_is_self_bound_advisor_only_and_has_no_recommendations():
    receipt = _build(_samples(_complete_cycle_frames()))
    digest = receipt.pop("pit_stint_receipt_sha256")

    assert canonical_sha256(receipt) == digest
    assert receipt["advisor_only"] is True
    assert receipt["attestation_status"] == "NOT_R7_ATTESTED"
    assert receipt["status"] == "CANDIDATE_NOT_GOLDEN"
    assert receipt["recommendations"] == []
    assert receipt["capabilities"]["pit_and_service_detection"]["status"] == "PASS_DATA"
    assert receipt["capabilities"]["complete_stint_analysis"]["status"] == (
        "WAIT_COMPLETE_STINT"
    )
    assert receipt["capabilities"]["service_contents"]["status"] == (
        "SKIP_NOT_OBSERVABLE"
    )
    assert receipt["capabilities"]["human_validation"]["status"] == (
        "WAIT_HUMAN_LABELS"
    )


@pytest.mark.parametrize("attack", ["input_binding_extra", "human_validation_pass"])
def test_recursive_validator_rejects_fully_rehashed_m1_attacks(attack: str):
    receipt = _build(_samples(_complete_cycle_frames()))
    tampered = copy.deepcopy(receipt)
    if attack == "input_binding_extra":
        tampered["input_binding"]["attacker"] = True
        tampered["input_binding"]["input_lineage_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in tampered["input_binding"].items()
                if key != "input_lineage_sha256"
            }
        )
    else:
        tampered["capabilities"]["human_validation"] = {
            "reasons": [],
            "status": "PASS_DATA",
        }
    tampered["pit_stint_receipt_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in tampered.items()
            if key != "pit_stint_receipt_sha256"
        }
    )

    with pytest.raises(PitStintReceiptError) as raised:
        validate_pit_stint_receipt(
            tampered,
            expected_pit_stint_receipt_sha256=tampered[
                "pit_stint_receipt_sha256"
            ],
        )

    assert raised.value.code in {"CAPABILITY_INVALID", "SCHEMA_INVALID"}


def test_exclusive_writer_never_overwrites_existing_output(tmp_path: Path):
    output = tmp_path / "receipt.json"
    _write_exclusive(output, b"first\n")

    with pytest.raises(PitStintReceiptError) as raised:
        _write_exclusive(output, b"second\n")

    assert raised.value.code == "OUTPUT_CREATE_FAILED"
    assert output.read_bytes() == b"first\n"


def test_main_preserves_existing_output_and_returns_three(tmp_path: Path, monkeypatch, capfd):
    receipt = _build(_samples(_complete_cycle_frames()))

    @contextmanager
    def fake_open(*args, **kwargs):
        yield object()

    monkeypatch.setattr(_MODULE, "open_ibt_telemetry", fake_open)
    monkeypatch.setattr(_MODULE, "build_pit_stint_receipt", lambda *args, **kwargs: receipt)
    output = tmp_path / "receipt.json"
    args = [
        str(tmp_path / "unused.ibt"),
        "--source-id",
        SOURCE_ID,
        "--session-id",
        SESSION_ID,
        "--expected-source-sha256",
        SOURCE_SHA,
        "--expected-normalized-samples-sha256",
        "b" * 64,
        "--expected-event-receipt-sha256",
        "c" * 64,
        "--output",
        str(output),
    ]

    assert main(args) == 0
    first = output.read_bytes()
    capfd.readouterr()
    assert main(args) == 3
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("OUTPUT_CREATE_FAILED:")
    assert output.read_bytes() == first


def test_hash_seed_does_not_change_canonical_receipt():
    code = r'''
import hashlib
import json
import iracing_ai_engineer.pit_stint as module
from iracing_ai_engineer.adapters import IbtInputEvidence
from iracing_ai_engineer.events import process_telemetry_events
from iracing_ai_engineer.telemetry import SourceKind, normalize_sdk_frame
frames = []
for i, road, stall, active, fuel in [
    (0,False,False,False,5.0),(1,True,False,False,5.0),
    (2,True,False,True,5.0),(3,True,True,True,8.0),
    (4,True,True,False,10.0),(5,True,False,False,10.0),
    (6,False,False,False,10.0),(7,False,False,False,9.9),
]:
    frames.append({"FuelLevel":fuel,"Lap":1,"LapCompleted":0,"LapDistPct":i/100,
        "OnPitRoad":road,"PitstopActive":active,"PlayerCarInPitStall":stall,
        "SessionFlags":1,"SessionNum":1,"SessionTick":1000+i,"SessionTime":i/10})
samples=[]
previous=None
for i, frame in enumerate(frames):
    sample=normalize_sdk_frame(frame,source_id="pit-stint-fixture",session_id="pit-stint-session",
        source_kind=SourceKind.IBT_OFFLINE,buffer_tick=i,previous=previous)
    samples.append(sample); previous=sample
digest=hashlib.sha256()
for sample in samples:
    encoded=sample.to_json_line().encode()
    digest.update(len(encoded).to_bytes(8,"little"))
    digest.update(encoded)
_, events=process_telemetry_events(samples)
evidence=IbtInputEvidence(source_id="pit-stint-fixture",session_id="pit-stint-session",
    source_sha256="a"*64,byte_size=123456,record_count=len(samples),tick_rate_hz=10)
receipt=module._build_receipt_from_samples(iter(samples),input_evidence=evidence,
    expected_source_sha256="a"*64,expected_normalized_samples_sha256=digest.hexdigest(),
    expected_event_receipt_sha256=events.receipt_sha256,
    stale_after_s=0.5,opponent_error_policy="degrade")
print(json.dumps(receipt,allow_nan=False,sort_keys=True,separators=(",",":")))
'''
    outputs = []
    for seed in ("1", "987654"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        environment["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path.cwd(),
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]


def test_package_zipimport_isolation_does_not_depend_on_scripts(tmp_path: Path):
    wheel = tmp_path / "iracing_ai_engineer-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(Path("src/iracing_ai_engineer").glob("*.py")):
            archive.write(source, f"iracing_ai_engineer/{source.name}")

    code = """
import json
import iracing_ai_engineer.pit_stint as module
print(json.dumps({
    "contract": module.PIT_STINT_CONTRACT_VERSION,
    "file": module.__file__,
    "builder_module": module.build_pit_stint_receipt.__module__,
}, sort_keys=True))
"""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(wheel)!r});" + code,
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    imported = json.loads(completed.stdout)
    assert imported == {
        "builder_module": "iracing_ai_engineer.pit_stint",
        "contract": "offline-m1-pit-stint-v1",
        "file": str(wheel / "iracing_ai_engineer" / "pit_stint.py"),
    }


def test_pinned_hatchling_build_produces_importable_m1_wheel(tmp_path: Path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the locked Hatchling build check")
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert project["build-system"] == {
        "build-backend": "hatchling.build",
        "requires": ["hatchling==1.31.0"],
    }

    output_dir = tmp_path / "dist"
    environment = os.environ.copy()
    environment["UV_NO_PROGRESS"] = "1"
    completed = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(output_dir)],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheels = sorted(output_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        assert "iracing_ai_engineer/pit_stint.py" in archive.namelist()

    import_environment = os.environ.copy()
    import_environment.pop("PYTHONPATH", None)
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import sys; sys.path.insert(0, {str(wheels[0])!r}); "
                "import iracing_ai_engineer.pit_stint as module; "
                "print(module.PIT_STINT_CONTRACT_VERSION)"
            ),
        ],
        cwd=tmp_path,
        env=import_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert imported.stdout.strip() == "offline-m1-pit-stint-v1"


PUBLIC_MANIFEST = json.loads(Path("data/public_sources.json").read_text(encoding="utf-8"))
PUBLIC_ASSET = PUBLIC_MANIFEST["assets"][0]
PUBLIC_SAMPLE = Path(PUBLIC_ASSET["local_path"])
requires_public_ibt = pytest.mark.skipif(
    not PUBLIC_SAMPLE.is_file(), reason="REQUIRES_DATA: public Audi/Spa IBT absent"
)


@requires_public_ibt
def test_real_audi_spa_pit_service_and_partial_stints_match_frozen_edges():
    shared_event = PUBLIC_ASSET["provisional_normalized_event_receipt"]["receipt"]
    with open_ibt_telemetry(
        PUBLIC_SAMPLE,
        source_id="public-audi-r8-evo2-spa",
        session_id="public-fixture-2023-12-race",
    ) as run:
        receipt = build_pit_stint_receipt(
            run,
            expected_source_sha256=PUBLIC_ASSET["sha256"],
            expected_normalized_samples_sha256=(
                PUBLIC_ASSET["provisional_offline_demo_receipt"][
                    "normalized_input_receipt"
                ]["samples_sha256"]
            ),
            expected_event_receipt_sha256=shared_event["receipt_sha256"],
        )

    assert receipt["quality_gate"] == {"reasons": [], "status": "PASS"}
    assert receipt["normalized_input_receipt"]["sample_count"] == 151_892
    assert receipt["summary"] == {
        "complete_stint_count": 0,
        "partial_stint_count": 2,
        "pit_cycle_count": 1,
        "service_episode_count": 1,
    }
    cycle = receipt["pit_cycles"][0]
    assert cycle["pit_road"]["enter"]["frame_index"] == 105_895
    assert cycle["pit_road"]["enter"]["session_time_s"] == 1787.616667
    assert cycle["pit_road"]["exit"]["frame_index"] == 107_935
    assert cycle["pit_road"]["exit"]["session_time_s"] == 1821.616667
    service = cycle["service_episodes"][0]
    assert service["start"]["frame_index"] == 106_962
    assert service["start"]["session_time_s"] == 1805.4
    assert service["end_edge"]["frame_index"] == 107_522
    assert service["end_edge"]["session_time_s"] == 1814.733333
    assert service["active_frame_count"] == 560
    assert service["duration_s"] == 9.333333
    assert service["observed_net_tank_change"] == {
        "end_fuel_level_l": 25.149794,
        "interpretation": "OBSERVED_ENDPOINT_TANK_LEVEL_DIFFERENCE_NOT_DELIVERED_FUEL",
        "provenance": "SDK_DIRECT_ENDPOINT_DIFFERENCE",
        "start_fuel_level_l": 4.154789,
        "value_l": 20.995004,
    }
    stall = cycle["pit_stall_intervals"][0]
    assert stall["enter"]["frame_index"] == 106_969
    assert stall["exit"]["frame_index"] == 107_575
    assert service["stall_support"] == {
        "overlap_duration_s": 9.216667,
        "service_starts_before_stall_s": 0.116667,
        "stall_extends_after_service_s": 0.883333,
        "stall_interval_id": "pit-stall:1",
        "status": "POSITIVE_OVERLAP",
    }
    assert [stint["status"] for stint in receipt["stints"]] == [
        "PARTIAL_START",
        "PARTIAL_END",
    ]
    for stint in receipt["stints"]:
        assert stint["observed_laps_completed_delta_availability"] == "AVAILABLE"
        assert stint["observed_laps_completed_delta"] == (
            stint["end"]["laps_completed"] - stint["start"]["laps_completed"]
        )
        assert stint["observed_tank_level_availability"] == "AVAILABLE"
        assert stint["observed_endpoint_provenance"] == "SDK_DIRECT"
        assert isinstance(stint["observed_start_tank_level_l"], float)
        assert isinstance(stint["observed_end_tank_level_l"], float)
    assert receipt["recommendations"] == []
    serialized = _MODULE._canonical_json(receipt, newline=True)
    assert len(serialized) == 7_462
    assert hashlib.sha256(serialized).hexdigest() == (
        "9082df792b8b06682a5c0caf8f8ca6b8cbc59e81c0954c4a6fee2eae0d6f0fb0"
    )
    assert receipt["pit_stint_receipt_sha256"] == (
        "76a7cec5cf255cd1d7f8fb9e46847b3cae515c8ad3c14acccfffdb0280b906d9"
    )
