from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from iracing_ai_engineer.collector import (
    COLLECTOR_CONTRACT_VERSION,
    R8_MAX_CAPTURE_BYTES,
    CollectorConsistencyError,
    CollectorSample,
    JsonlAppendWriter,
    JsonlHandleWriter,
    LiveCollector,
    SessionInfoPayloadScope,
    collect_samples,
    collect_samples_to_jsonl,
    collect_samples_to_jsonl_handle,
    collect_transport_to_jsonl,
    collect_transport_to_jsonl_handle,
    validate_collector_sample,
    validate_variable_descriptors,
)
from iracing_ai_engineer.sdk_probe import RawSdkFrame, VariableDescriptor
from iracing_ai_engineer.telemetry import SourceKind

RUN_IDENTITY = {"source_id": "test-source", "session_id": "test-session"}


def descriptor(name: str, *, offset: int = 0) -> VariableDescriptor:
    return VariableDescriptor(
        name=name,
        type_code=4,
        dtype="float32",
        offset=offset,
        count=1,
        count_as_time=False,
        unit="",
        description=name,
    )


BASE_SCHEMA = tuple(
    descriptor(name, offset=index * 4)
    for index, name in enumerate(("SessionNum", "SessionTick", "SessionTime", "Speed"))
)
EXTENDED_SCHEMA = (*BASE_SCHEMA, descriptor("FuelLevel", offset=16))


def sample(
    tick: int,
    *,
    capture_s: float,
    session_num: int = 1,
    session_tick: int | None = None,
    session_time: float | None = None,
    update: int = 1,
    descriptors: tuple[VariableDescriptor, ...] = BASE_SCHEMA,
    session_info: dict[str, object] | None = None,
    session_info_scope: SessionInfoPayloadScope | str | None = None,
    sim_mode: object = "full",
) -> CollectorSample:
    values: dict[str, object] = {
        "SessionNum": session_num,
        "SessionTick": tick if session_tick is None else session_tick,
        "SessionTime": tick / 60 if session_time is None else session_time,
        "Speed": 50.0,
    }
    if "FuelLevel" in {item.name for item in descriptors}:
        values["FuelLevel"] = 40.0
    return CollectorSample(
        frame=RawSdkFrame(
            buffer_tick=tick,
            session_info_update=update,
            values=values,
            sim_mode_raw=sim_mode,
            captured_monotonic_s=capture_s,
        ),
        descriptors=descriptors,
        tick_rate_hz=60,
        session_info=session_info,
        session_info_scope=session_info_scope,
    )


class MemoryWriter:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        self.records.append(dict(record))


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def test_detects_drop_stale_reset_and_schema_epoch_with_deterministic_receipt():
    private_info = {
        "WeekendInfo": {"TrackName": "Spa"},
        "DriverInfo": {"Drivers": [{"UserName": "Private Person"}]},
    }
    observations = (
        sample(100, capture_s=0.0, session_info=private_info),
        sample(100, capture_s=0.6, session_info=private_info),
        sample(103, capture_s=0.7, session_info=private_info),
        sample(
            1,
            capture_s=0.8,
            session_num=2,
            session_tick=1,
            session_time=0.0,
            update=0,
            descriptors=EXTENDED_SCHEMA,
            session_info={**private_info, "SessionInfo": {"Sessions": []}},
        ),
    )

    first_writer = MemoryWriter()
    first = collect_samples(
        observations, first_writer, stale_after_s=0.5, **RUN_IDENTITY
    )
    second_writer = MemoryWriter()
    second = collect_samples(
        observations, second_writer, stale_after_s=0.5, **RUN_IDENTITY
    )

    assert first == second
    assert first.records_sha256 == second.records_sha256
    assert first.completion_status == "COMPLETE"
    assert first.run_record_count == 1
    assert first.frame_record_count == 3
    assert first.duplicate_sample_count == 1
    assert first.duplicate_conflict_count == 0
    assert first.dropped_tick_count == 2
    assert first.stale_event_count == 1
    assert first.session_reset_count == 1
    assert first.schema_change_count == 1
    assert first.schema_epoch_count == 2
    assert first.session_epoch_count == 2
    assert first.schema_record_count == 2
    assert first.session_info_record_count == 2

    kinds = [
        record["event_kind"]
        for record in first_writer.records
        if record["record_type"] == "event"
    ]
    assert kinds == [
        "source_stale",
        "duplicate_sample",
        "source_resumed",
        "tick_drop",
        "schema_changed",
        "session_reset",
    ]
    reset = next(
        record
        for record in first_writer.records
        if record.get("event_kind") == "session_reset"
    )
    assert set(reset["details"]["reasons"]) >= {
        "BUFFER_TICK_REGRESSION",
        "SESSION_INFO_UPDATE_REGRESSION",
        "SESSION_NUM_CHANGED",
        "SESSIONTICK_REGRESSION",
        "SESSIONTIME_REGRESSION",
    }

    encoded = "\n".join(json.dumps(record) for record in first_writer.records)
    assert "Private Person" not in encoded
    session_records = [
        record for record in first_writer.records if record["record_type"] == "session_info"
    ]
    assert session_records[0]["redacted_paths"] == ["DriverInfo"]
    assert session_records[0]["payload_scope"] == "FULL"
    assert session_records[0]["payload_status"] == "PRESENT"
    assert "DriverInfo" not in session_records[0]["payload"]

    digest = hashlib.sha256()
    semantic_records = first_writer.records[:-1]
    for record in semantic_records:
        payload = canonical_json(record)
        digest.update(len(payload).to_bytes(8, "little"))
        digest.update(payload)
    assert first.semantic_record_count == len(semantic_records)
    assert first.records_sha256 == digest.hexdigest()
    assert first_writer.records[-1] == {
        "collector_contract_version": COLLECTOR_CONTRACT_VERSION,
        "record_type": "collector_receipt",
        "sequence": len(semantic_records),
        "receipt": first.to_dict(),
    }
    assert first_writer.records[0] == {
        "collector_contract_version": "live-collector-v2",
        "record_type": "run",
        "sequence": 0,
        "session_id": "test-session",
        "sim_mode": "full",
        "source_id": "test-source",
        "source_kind": "SDK_LIVE",
    }


def test_duplicate_tick_with_changed_payload_emits_conflict():
    first = sample(50, capture_s=1.0)
    changed_frame = replace(
        first.frame,
        captured_monotonic_s=1.1,
        values={**first.frame.values, "Speed": 55.0},
    )
    writer = MemoryWriter()

    receipt = collect_samples(
        (first, replace(first, frame=changed_frame)), writer, **RUN_IDENTITY
    )

    assert receipt.frame_record_count == 1
    assert receipt.duplicate_sample_count == 1
    assert receipt.duplicate_conflict_count == 1
    assert [
        record["event_kind"]
        for record in writer.records
        if record["record_type"] == "event"
    ] == ["duplicate_sample", "duplicate_tick_conflict"]
    duplicate = next(
        record for record in writer.records if record.get("event_kind") == "duplicate_sample"
    )
    assert duplicate["details"]["conflict"] is True
    assert duplicate["details"]["previous_payload_sha256"]
    assert duplicate["details"]["current_payload_sha256"]


def test_generator_failure_leaves_complete_jsonl_prefix_readable(tmp_path: Path):
    path = tmp_path / "live.jsonl"

    def broken_stream():
        yield sample(10, capture_s=0.0)
        yield sample(11, capture_s=0.01)
        raise RuntimeError("simulated collector crash")

    with pytest.raises(RuntimeError, match="simulated collector crash"):
        collect_samples_to_jsonl(broken_stream(), path, **RUN_IDENTITY)

    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    records = [json.loads(line) for line in raw.splitlines()]
    assert [record["record_type"] for record in records] == [
        "run",
        "schema",
        "session_info",
        "frame",
        "frame",
    ]
    assert all(record["record_type"] != "collector_receipt" for record in records)


def test_jsonl_writer_refuses_existing_path_without_rewriting_bytes(tmp_path: Path):
    path = tmp_path / "append.jsonl"
    path.write_bytes(b'{"old":true}\n')
    before = path.read_bytes()

    with pytest.raises(FileExistsError), JsonlAppendWriter(path) as writer:
        writer.write({"new": True})

    assert path.read_bytes() == before


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows share mode blocks renaming the held file before collection",
)
def test_caller_owned_handle_collection_survives_path_replacement_and_stays_open(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "live.jsonl"
    displaced = tmp_path / "live.displaced.jsonl"
    replacement = b'{"attacker":"replacement path"}\n'
    observations = [sample(1, capture_s=0.0), sample(2, capture_s=0.01)]

    with canonical.open("x+b", buffering=0) as handle:
        canonical.rename(displaced)
        canonical.write_bytes(replacement)
        receipt = collect_samples_to_jsonl_handle(
            observations,
            handle,
            **RUN_IDENTITY,
            fsync_each_record=False,
        )
        assert handle.closed is False
        assert handle.tell() == displaced.stat().st_size

    assert canonical.read_bytes() == replacement
    records = [json.loads(line) for line in displaced.read_text().splitlines()]
    assert records[-1]["record_type"] == "collector_receipt"
    assert receipt.completion_status == "COMPLETE"
    assert receipt.frame_record_count == 2


def test_handle_writer_rejects_nonempty_descriptor_without_closing_or_rewriting(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "not-empty.jsonl"
    original = b"operator bytes\n"
    with capture.open("x+b", buffering=0) as handle:
        handle.write(original)
        handle.seek(0)
        with pytest.raises(CollectorConsistencyError, match="empty"):
            with JsonlHandleWriter(handle):
                pass
        assert handle.closed is False
        handle.seek(0)
        assert handle.read() == original


def test_handle_writer_rejects_buffered_wrapper(tmp_path: Path) -> None:
    capture = tmp_path / "buffered.jsonl"
    with capture.open("x+b") as buffered:
        with pytest.raises(TypeError, match="unbuffered binary"):
            with JsonlHandleWriter(buffered):
                pass
        assert buffered.closed is False
    assert capture.read_bytes() == b""


@pytest.mark.parametrize(
    ("capacity_delta", "accepted"),
    [(-1, False), (0, True), (1, True)],
)
def test_handle_writer_enforces_record_byte_cap_before_write(
    tmp_path: Path,
    capacity_delta: int,
    accepted: bool,
) -> None:
    record = {"record": "one"}
    payload = (
        json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    path = tmp_path / f"cap-{capacity_delta}.jsonl"
    with path.open("x+b", buffering=0) as handle:
        with JsonlHandleWriter(
            handle,
            fsync_each_record=False,
            max_output_bytes=len(payload) + capacity_delta,
        ) as writer:
            if accepted:
                writer.write(record)
            else:
                with pytest.raises(
                    CollectorConsistencyError, match="max_output_bytes"
                ):
                    writer.write(record)
        assert handle.closed is False
        handle.seek(0)
        assert handle.read() == (payload if accepted else b"")


def test_handle_writer_cap_failure_retains_complete_prefix_and_open_handle(
    tmp_path: Path,
) -> None:
    first = {"record": "first"}
    second = {"record": "second"}
    first_payload = (
        json.dumps(
            first,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    path = tmp_path / "capped-prefix.jsonl"
    with path.open("x+b", buffering=0) as handle:
        with pytest.raises(CollectorConsistencyError, match="max_output_bytes"):
            with JsonlHandleWriter(
                handle,
                fsync_each_record=False,
                max_output_bytes=len(first_payload),
            ) as writer:
                writer.write(first)
                writer.write(second)
        assert handle.closed is False
        handle.seek(0)
        assert handle.read() == first_payload
    assert path.exists()


def test_r8_handle_capture_default_is_exactly_eight_gibibytes() -> None:
    assert R8_MAX_CAPTURE_BYTES == 8 * 1024**3


class FakeTransport:
    def __init__(self, frames: list[RawSdkFrame]) -> None:
        self.frames = frames
        self.index = 0
        self.closed = False
        self.requested_fields: list[tuple[str, ...]] = []
        self.descriptor_calls = 0

    def startup(self, timeout_s: float) -> SimpleNamespace:
        assert timeout_s == 0.0
        return SimpleNamespace(tick_rate_hz=60)

    def descriptors(self) -> tuple[VariableDescriptor, ...]:
        self.descriptor_calls += 1
        return BASE_SCHEMA

    def read_frozen(self, fields: tuple[str, ...]) -> RawSdkFrame:
        self.requested_fields.append(fields)
        frame = self.frames[self.index]
        self.index += 1
        return frame

    def sim_mode(self) -> tuple[str, int]:
        return "full", self.frames[self.index - 1].session_info_update

    @property
    def connected(self) -> bool:
        return not self.closed and self.index < len(self.frames)

    def close(self) -> None:
        self.closed = True


def test_fake_transport_runs_on_non_windows_and_is_always_closed(tmp_path: Path):
    observations = [sample(1, capture_s=0.0), sample(2, capture_s=0.01)]
    transport = FakeTransport([item.frame for item in observations])
    path = tmp_path / "transport.jsonl"

    receipt = collect_transport_to_jsonl(
        transport,
        path,
        **RUN_IDENTITY,
        wait_seconds=0.0,
        duration_s=10.0,
        poll_seconds=0.01,
        max_reads=2,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    assert receipt.frame_record_count == 2
    assert transport.closed is True
    assert transport.descriptor_calls == 1
    assert transport.requested_fields == [
        tuple(item.name for item in BASE_SCHEMA),
        tuple(item.name for item in BASE_SCHEMA),
    ]
    records = [json.loads(line) for line in path.read_text().splitlines()]
    frames = [record for record in records if record["record_type"] == "frame"]
    assert [record["buffer_tick"] for record in frames] == [1, 2]
    assert frames[0]["sim_mode_raw"] == "full"
    session_record = next(
        record for record in records if record["record_type"] == "session_info"
    )
    assert session_record["payload"] == {"WeekendInfo": {"SimMode": "full"}}
    assert session_record["payload_scope"] == "PARTIAL"
    assert session_record["payload_status"] == "PRESENT"


def test_fake_transport_can_collect_to_same_caller_owned_handle(tmp_path: Path):
    observations = [sample(1, capture_s=0.0), sample(2, capture_s=0.01)]
    transport = FakeTransport([item.frame for item in observations])
    path = tmp_path / "transport-handle.jsonl"

    with path.open("x+b", buffering=0) as handle:
        receipt = collect_transport_to_jsonl_handle(
            transport,
            handle,
            **RUN_IDENTITY,
            wait_seconds=0.0,
            duration_s=10.0,
            poll_seconds=0.01,
            fsync_each_record=False,
            max_reads=2,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
        )
        assert handle.closed is False
        handle.seek(0)
        records = [json.loads(line) for line in handle.read().splitlines()]

    assert transport.closed is True
    assert receipt.frame_record_count == 2
    assert records[-1]["receipt"]["records_sha256"] == receipt.records_sha256


def test_transport_disconnect_before_deadline_never_writes_complete_receipt(
    tmp_path: Path,
):
    transport = FakeTransport([sample(1, capture_s=0.0).frame])
    path = tmp_path / "disconnected.jsonl"

    with pytest.raises(CollectorConsistencyError, match="disconnected"):
        collect_transport_to_jsonl(
            transport,
            path,
            **RUN_IDENTITY,
            wait_seconds=0.0,
            duration_s=10.0,
            poll_seconds=0.01,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
        )

    assert transport.closed is True
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(record["record_type"] == "frame" for record in records)
    assert all(record["record_type"] != "collector_receipt" for record in records)


def test_unknown_frame_field_fails_closed_without_writing_frame():
    bad = sample(1, capture_s=0.0)
    bad = replace(bad, frame=replace(bad.frame, values={**bad.frame.values, "Mystery": 1}))
    writer = MemoryWriter()

    with pytest.raises(CollectorConsistencyError, match="Mystery"):
        collect_samples((bad,), writer, **RUN_IDENTITY)

    assert writer.records == []


@pytest.mark.parametrize("stale_after_s", [0.0, -1.0, float("inf")])
def test_stale_threshold_must_be_finite_and_positive(stale_after_s: float):
    with pytest.raises(ValueError, match="stale_after_s"):
        LiveCollector(MemoryWriter(), stale_after_s=stale_after_s, **RUN_IDENTITY)


def test_jsonl_writer_fsyncs_each_record_by_default(tmp_path: Path, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr("iracing_ai_engineer.collector.os.fsync", calls.append)
    path = tmp_path / "durable.jsonl"

    with JsonlAppendWriter(path) as writer:
        writer.write({"record": 1})

    assert len(calls) == 1
    assert json.loads(path.read_text()) == {"record": 1}


def test_jsonl_writer_detects_a_short_write():
    class ShortHandle:
        def write(self, payload: bytes) -> int:
            return len(payload) - 1

        def flush(self) -> None:
            raise AssertionError("flush must not hide a short write")

    writer = JsonlAppendWriter("unused.jsonl", fsync_each_record=False)
    writer._handle = ShortHandle()

    with pytest.raises(OSError, match="short JSONL write"):
        writer.write({"record": 1})


@pytest.mark.parametrize(
    ("bad_value", "message"),
    [
        (float("nan"), "non-finite"),
        (float("inf"), "non-finite"),
        (b"raw", "bytes"),
        ({1: "would-collide-with-string-one"}, "plain strings"),
    ],
)
def test_json_ambiguities_fail_before_first_record(bad_value: object, message: str):
    observation = sample(1, capture_s=0.0)
    bad_frame = replace(
        observation.frame,
        values={**observation.frame.values, "Speed": bad_value},
    )
    writer = MemoryWriter()

    with pytest.raises(CollectorConsistencyError, match=message):
        collect_samples((replace(observation, frame=bad_frame),), writer, **RUN_IDENTITY)

    assert writer.records == []


@pytest.mark.parametrize(
    "observation",
    [
        replace(
            sample(1, capture_s=0.0),
            frame=replace(sample(1, capture_s=0.0).frame, buffer_tick=True),
        ),
        replace(
            sample(1, capture_s=0.0),
            frame=replace(sample(1, capture_s=0.0).frame, session_info_update=-1),
        ),
        replace(sample(1, capture_s=0.0), tick_rate_hz=60.0),
    ],
)
def test_sample_clock_metadata_requires_plain_bounded_integers(observation: CollectorSample):
    writer = MemoryWriter()

    with pytest.raises(CollectorConsistencyError):
        validate_collector_sample(observation)
    with pytest.raises(CollectorConsistencyError):
        collect_samples((observation,), writer, **RUN_IDENTITY)

    assert writer.records == []


@pytest.mark.parametrize(
    "bad_descriptor",
    [
        replace(BASE_SCHEMA[0], name="x" * 33),
        replace(BASE_SCHEMA[0], type_code=True),
        replace(BASE_SCHEMA[0], dtype="int32"),
        replace(BASE_SCHEMA[0], offset=True),
        replace(BASE_SCHEMA[0], count=0),
        replace(BASE_SCHEMA[0], count=65_537),
        replace(BASE_SCHEMA[0], count_as_time=1),
    ],
)
def test_descriptor_validator_rejects_ambiguous_or_unreasonable_schema(
    bad_descriptor: VariableDescriptor,
):
    bad_schema = (bad_descriptor, *BASE_SCHEMA[1:])

    with pytest.raises(CollectorConsistencyError):
        validate_variable_descriptors(bad_schema)


def test_descriptor_validator_rejects_overlapping_ranges():
    overlapping = (BASE_SCHEMA[0], replace(BASE_SCHEMA[1], offset=0), *BASE_SCHEMA[2:])

    with pytest.raises(CollectorConsistencyError, match="overlap"):
        validate_variable_descriptors(overlapping)


def test_descriptor_validator_accepts_official_bitfield_dtype():
    session_flags = VariableDescriptor(
        name="SessionFlags",
        type_code=3,
        dtype="uint32_or_bitfield",
        offset=0,
        count=1,
        count_as_time=False,
        unit="",
        description="Session flags",
    )

    validate_variable_descriptors((session_flags,))


def test_descriptor_validator_still_rejects_overlong_dtype():
    overlong = replace(BASE_SCHEMA[0], dtype="x" * 33)

    with pytest.raises(CollectorConsistencyError, match="dtype exceeds 32 UTF-8 bytes"):
        validate_variable_descriptors((overlong,))


@pytest.mark.parametrize("read_errors", [("Mystery",), (1,), ["Speed"]])
def test_read_errors_must_be_unique_schema_field_names(read_errors: object):
    observation = sample(1, capture_s=0.0)
    bad_frame = replace(observation.frame, read_errors=read_errors)
    writer = MemoryWriter()

    with pytest.raises(CollectorConsistencyError, match="read_errors"):
        collect_samples((replace(observation, frame=bad_frame),), writer, **RUN_IDENTITY)

    assert writer.records == []


def test_zero_frame_run_cannot_emit_complete_receipt():
    writer = MemoryWriter()

    with pytest.raises(CollectorConsistencyError, match="without a frame"):
        collect_samples((), writer, **RUN_IDENTITY)

    assert writer.records == []


def test_invalid_run_identity_fails_before_output_file_creation(tmp_path: Path):
    path = tmp_path / "invalid-identity.jsonl"

    with pytest.raises(ValueError, match="source_id"):
        collect_samples_to_jsonl(
            (sample(1, capture_s=0.0),),
            path,
            source_id=" ",
            session_id="valid-session",
        )

    assert not path.exists()


def test_replay_mode_binds_run_source_kind_and_expected_kind():
    writer = MemoryWriter()

    receipt = collect_samples(
        (sample(1, capture_s=0.0, sim_mode="replay"),),
        writer,
        expected_source_kind=SourceKind.REPLAY_SDK_PROXY,
        **RUN_IDENTITY,
    )

    assert receipt.run_record_count == 1
    assert writer.records[0]["source_kind"] == "REPLAY_SDK_PROXY"
    assert writer.records[0]["sim_mode"] == "replay"


@pytest.mark.parametrize("sim_mode", [None, "unknown", 1])
def test_unknown_first_sim_mode_fails_before_first_record(sim_mode: object):
    writer = MemoryWriter()

    with pytest.raises(CollectorConsistencyError, match="sim_mode_raw"):
        collect_samples(
            (sample(1, capture_s=0.0, sim_mode=sim_mode),), writer, **RUN_IDENTITY
        )

    assert writer.records == []


def test_expected_or_later_mixed_source_kind_fails_closed():
    mismatch_writer = MemoryWriter()
    with pytest.raises(CollectorConsistencyError, match="expected_source_kind"):
        collect_samples(
            (sample(1, capture_s=0.0),),
            mismatch_writer,
            expected_source_kind=SourceKind.REPLAY_SDK_PROXY,
            **RUN_IDENTITY,
        )
    assert mismatch_writer.records == []

    mixed_writer = MemoryWriter()
    collector = LiveCollector(mixed_writer, **RUN_IDENTITY)
    collector.ingest(sample(1, capture_s=0.0))
    with pytest.raises(CollectorConsistencyError, match="changed"):
        collector.ingest(sample(2, capture_s=0.1, sim_mode="replay"))
    assert all(record["record_type"] != "collector_receipt" for record in mixed_writer.records)


@pytest.mark.parametrize(
    ("payload", "scope"),
    [
        (None, SessionInfoPayloadScope.FULL),
        ({"WeekendInfo": {}}, SessionInfoPayloadScope.UNAVAILABLE),
    ],
)
def test_session_info_scope_must_match_payload(
    payload: dict[str, object] | None,
    scope: SessionInfoPayloadScope,
):
    writer = MemoryWriter()
    observation = sample(
        1,
        capture_s=0.0,
        session_info=payload,
        session_info_scope=scope,
    )

    with pytest.raises(CollectorConsistencyError, match="SessionInfo"):
        collect_samples((observation,), writer, **RUN_IDENTITY)

    assert writer.records == []


def test_unavailable_session_info_is_explicit_in_record():
    writer = MemoryWriter()

    collect_samples((sample(1, capture_s=0.0),), writer, **RUN_IDENTITY)

    record = next(item for item in writer.records if item["record_type"] == "session_info")
    assert record["payload"] is None
    assert record["payload_scope"] == "UNAVAILABLE"
    assert record["payload_status"] == "UNAVAILABLE"


class SnapshotTransport(FakeTransport):
    def session_info_snapshot(self):
        return (
            {
                "WeekendInfo": {"SimMode": "full", "TrackName": "Spa"},
                "DriverInfo": {"Drivers": [{"UserName": "Private Person"}]},
            },
            self.frames[self.index - 1].session_info_update,
        )


def test_transport_snapshot_provider_is_full_and_redacted(tmp_path: Path):
    transport = SnapshotTransport([sample(1, capture_s=0.0).frame])
    path = tmp_path / "snapshot.jsonl"

    collect_transport_to_jsonl(
        transport,
        path,
        wait_seconds=0.0,
        duration_s=1.0,
        max_reads=1,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
        **RUN_IDENTITY,
    )

    records = [json.loads(line) for line in path.read_text().splitlines()]
    session_record = next(
        record for record in records if record["record_type"] == "session_info"
    )
    assert session_record["payload_scope"] == "FULL"
    assert session_record["payload_status"] == "PRESENT"
    assert session_record["redacted_paths"] == ["DriverInfo"]
    assert "Private Person" not in path.read_text()


class UnavailableSnapshotTransport(FakeTransport):
    def session_info_snapshot(self):
        return None, None


def test_unavailable_full_snapshot_uses_explicit_partial_sim_mode_fallback(tmp_path: Path):
    transport = UnavailableSnapshotTransport([sample(1, capture_s=0.0).frame])
    path = tmp_path / "snapshot-fallback.jsonl"

    collect_transport_to_jsonl(
        transport,
        path,
        wait_seconds=0.0,
        duration_s=1.0,
        max_reads=1,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
        **RUN_IDENTITY,
    )

    records = [json.loads(line) for line in path.read_text().splitlines()]
    session_record = next(
        record for record in records if record["record_type"] == "session_info"
    )
    assert session_record["payload_scope"] == "PARTIAL"


class CloseFailTransport(FakeTransport):
    def close(self) -> None:
        self.closed = True
        raise RuntimeError("simulated close failure")


def test_transport_close_failure_is_visible_after_success(tmp_path: Path):
    transport = CloseFailTransport([sample(1, capture_s=0.0).frame])
    path = tmp_path / "close-failure.jsonl"

    with pytest.raises(RuntimeError, match="simulated close failure"):
        collect_transport_to_jsonl(
            transport,
            path,
            wait_seconds=0.0,
            duration_s=1.0,
            max_reads=1,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
            **RUN_IDENTITY,
        )

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[-1]["record_type"] == "collector_receipt"


class ReadAndCloseFailTransport(CloseFailTransport):
    def read_frozen(self, fields: tuple[str, ...]) -> RawSdkFrame:
        raise ValueError("simulated read failure")


def test_transport_close_failure_is_added_as_note_to_primary_error(tmp_path: Path):
    transport = ReadAndCloseFailTransport([sample(1, capture_s=0.0).frame])

    with pytest.raises(ValueError, match="simulated read failure") as raised:
        collect_transport_to_jsonl(
            transport,
            tmp_path / "read-failure.jsonl",
            wait_seconds=0.0,
            duration_s=1.0,
            max_reads=1,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
            **RUN_IDENTITY,
        )

    assert raised.value.__notes__ == [
        "transport.close() also failed: RuntimeError: simulated close failure"
    ]
