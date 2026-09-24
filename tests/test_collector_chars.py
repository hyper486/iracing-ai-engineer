"""Schema-bound char bytes survive private capture without broad bytes coercion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from iracing_ai_engineer.adapters import open_collector_jsonl
from iracing_ai_engineer.capture_replay import replay_capture
from iracing_ai_engineer.collector import (
    CollectorConsistencyError,
    CollectorSample,
    LiveCollector,
    _json_safe,
    _SchemaCache,
    validate_collector_sample,
)
from iracing_ai_engineer.live_app_recording import AppRecorder
from iracing_ai_engineer.sdk_probe import RawSdkFrame, VariableDescriptor
from iracing_ai_engineer.synthetic_sdk import synthetic_sdk


def sample(value, *, count=1, tick=1):
    descriptors = (
        VariableDescriptor("SessionNum", 2, "int32", 0, 1, False, "", ""),
        VariableDescriptor("SessionTick", 2, "int32", 4, 1, False, "", ""),
        VariableDescriptor("SessionTime", 5, "float64", 8, 1, False, "s", ""),
        VariableDescriptor("ExtraChar", 0, "char", 16, count, False, "", ""),
    )
    return CollectorSample(
        RawSdkFrame(buffer_tick=tick, session_info_update=1,
                    values={"SessionNum": 0, "SessionTick": tick,
                            "SessionTime": tick / 60, "ExtraChar": value},
                    sim_mode_raw="full", captured_monotonic_s=tick / 60),
        descriptors, 60,
    )


class Writer:
    def __init__(self):
        self.records = []

    def write(self, row):
        self.records.append(row)


@pytest.mark.parametrize("shape", ["packed", "list", "tuple", "scalar_nul", "scalar_high"])
def test_char_octets_are_losslessly_encoded_without_modifying_transport(shape):
    octets = b"\0" if shape == "scalar_nul" else b"\xff" if shape == "scalar_high" else (
        bytes(range(256)) + b"\0\0")
    value = [bytes([item]) for item in octets] if shape in ("list", "tuple") else octets
    if shape == "tuple":
        value = tuple(value)
    observation = sample(value, count=len(octets))
    original = observation.frame.values.copy()
    validate_collector_sample(observation)  # The uncached validator uses the same encoding.
    writer = Writer()
    collector = LiveCollector(writer, source_id="synthetic", session_id="synthetic")
    collector.ingest(observation)
    receipt = collector.finish()
    [frame] = [row for row in writer.records if row["record_type"] == "frame"]
    assert frame["values"]["ExtraChar"].encode("latin-1") == octets
    assert receipt.collector_contract_version == "live-collector-v2"
    assert observation.frame.values == original and observation.frame.values["ExtraChar"] is value
    assert json.loads(json.dumps(frame))["values"]["ExtraChar"].encode("latin-1") == octets


@pytest.mark.parametrize("value,count", [
    (b"ab", 1), (b"", 1), ([b"a"], 2), ([b"a", b"bc"], 2),
    ([b"a", "b"], 2), ([b"a", 1], 2), ({"nested": b"a"}, 1),
    (bytearray(b"a"), 1), (memoryview(b"a"), 1),
])
def test_bad_char_shapes_fail_before_any_record(value, count):
    writer = Writer()
    collector = LiveCollector(writer, source_id="synthetic", session_id="synthetic")
    with pytest.raises(CollectorConsistencyError):
        collector.ingest(sample(value, count=count))
    assert writer.records == []


@pytest.mark.parametrize("where", ["numeric_field", "metadata", "writer", "undeclared_field"])
def test_char_encoding_does_not_relax_other_bytes_guards(where):
    observation = sample(b"x")
    if where == "numeric_field":
        observation = replace(observation, frame=replace(observation.frame,
            values={**observation.frame.values, "SessionNum": b"x"}))
    elif where == "metadata":
        observation = replace(observation, session_info={"ExtraChar": b"x"})
    elif where == "undeclared_field":
        observation = replace(observation, frame=replace(observation.frame,
            values={**observation.frame.values, "Undeclared": b"x"}))
    writer = Writer()
    collector = LiveCollector(writer, source_id="synthetic", session_id="synthetic")
    with pytest.raises(CollectorConsistencyError, match="bytes"):
        if where == "writer":
            _json_safe({"ExtraChar": b"x"})
        else:
            collector.ingest(observation)
    assert writer.records == []


def test_char_cache_tracks_changed_descriptor_content_not_object_identity():
    observation = sample(b"x")
    cache = _SchemaCache()
    original = cache.prepare(observation.descriptors)
    assert original.char_counts == (("ExtraChar", 1),)
    object.__setattr__(observation.descriptors[-1], "count", 2)
    changed = cache.prepare(observation.descriptors)
    assert changed.char_counts == (("ExtraChar", 2),) and changed.digest != original.digest
    object.__setattr__(observation.descriptors[-1], "type_code", 2)
    object.__setattr__(observation.descriptors[-1], "dtype", "int32")
    assert cache.prepare(observation.descriptors).char_counts == ()


def test_legacy_json_chars_keep_identical_records_and_receipt():
    def collect(value):
        writer = Writer()
        collector = LiveCollector(writer, source_id="synthetic", session_id="synthetic")
        collector.ingest(sample(value, count=4))
        return writer.records, collector.finish()

    assert collect(b"a\x80\0\0") == collect("a\x80\0\0")
    # Older Crew Chief strings can have trimmed NUL padding; do not invent it.
    records, _ = collect("a")
    [frame] = [row for row in records if row["record_type"] == "frame"]
    assert frame["values"]["ExtraChar"] == "a"


def test_legacy_json_char_capture_matches_unmodified_5145c5e_bytes():
    # Measured by executing the previous committed collector on these invented
    # values. This is a compatibility golden, never authentic telemetry evidence.
    writer = Writer()
    collector = LiveCollector(writer, source_id="synthetic", session_id="synthetic")
    for tick, value in [(1, "a\x80\0\0"), (1, "a\x80\0\0"),
                        (1, "b\x80\0\0"), (2, "b\x80\0\0")]:
        collector.ingest(sample(value, count=4, tick=tick))
    receipt = collector.finish()
    encoded = b"".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode() + b"\n" for row in writer.records)
    assert len(encoded) == 3855
    assert hashlib.sha256(encoded).hexdigest() == (
        "a3e6af4b62065bcdbaf59d3a166b5469e851bd9b39b213bf79b6d2518ede0449"
    )
    assert receipt.records_sha256 == (
        "7327d47b66dbaf8f97ee0d3bade753ece5feee5fbea7b7e74c8dcff4b21dc129"
    )


def test_char_duplicate_digests_and_replay_are_consistent(tmp_path):
    recorder = AppRecorder(tmp_path, source_id="synthetic", session_id="synthetic")
    try:
        recorder.ingest(sample([b"x", b"\0"], count=2))
        recorder.ingest(sample(b"x\0", count=2))
        recorder.ingest(sample([b"y", b"\0"], count=2))
        recorder.ingest(sample([b"y", b"\0"], count=2, tick=2))
        receipt = recorder.finish()
    finally:
        recorder.close()
    assert receipt["duplicate_sample_count"] == 2
    assert receipt["duplicate_conflict_count"] == 1
    [path] = tmp_path.glob("capture-*.jsonl")
    before = path.read_bytes()
    rows = [json.loads(line) for line in before.splitlines()]
    first_frame = next(row for row in rows if row["record_type"] == "frame")
    payload = {key: first_frame[key] for key in ("buffer_tick", "read_errors", "values")}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    duplicates = [row for row in rows if row.get("event_kind") == "duplicate_sample"]
    assert duplicates[0]["details"]["current_payload_sha256"] == digest
    assert duplicates[1]["details"]["current_payload_sha256"] != digest
    with open_collector_jsonl(path) as run:
        assert len(list(run.samples)) == 2
    report = replay_capture(path)
    assert report["frames"] == 2 and report["live_acceptance"] is False
    assert path.read_bytes() == before


def test_all_six_pinned_sdk_types_reach_a_complete_private_capture(tmp_path):
    with synthetic_sdk(fields=12, arrays=6) as (transport, descriptors):
        frame = transport.read_frozen(tuple(item.name for item in descriptors))
        # Add invented session clock fields solely for the replay normalizer.
        core = sample(b"x")
        descriptors = (*core.descriptors[:3], *(replace(item, offset=item.offset + 16)
                                               for item in descriptors))
        frame = replace(frame, buffer_tick=1, sim_mode_raw="full",
                        captured_monotonic_s=1 / 60,
                        values={**{key: value for key, value in core.frame.values.items()
                                   if key != "ExtraChar"}, **frame.values})
    recorder = AppRecorder(tmp_path, source_id="synthetic", session_id="synthetic")
    try:
        recorder.ingest(CollectorSample(frame, descriptors, 60))
        receipt = recorder.finish()
    finally:
        recorder.close()
    assert receipt["completion_status"] == "COMPLETE"
    [path] = tmp_path.glob("capture-*.jsonl")
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    saved = next(row["values"] for row in rows if row["record_type"] == "frame")
    assert saved["SyntheticField0000"].encode("latin-1") == b"".join(
        frame.values["SyntheticField0000"])
    assert saved["SyntheticField0006"].encode("latin-1") == b"\x80"
    for name, value in frame.values.items():
        if name not in ("SyntheticField0000", "SyntheticField0006"):
            assert saved[name] == value
    report = replay_capture(path)
    assert report["status"] == "RECOMPUTED" and report["frames"] == 1
    assert report["sdk_accessed"] is report["live_acceptance"] is False
    assert "SyntheticField" not in json.dumps(report)
