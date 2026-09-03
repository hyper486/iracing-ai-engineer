from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from iracing_ai_engineer import adapters, cli
from iracing_ai_engineer.adapters import (
    CollectorInputEvidence,
    IbtInputEvidence,
    TelemetryAdapterError,
)
from iracing_ai_engineer.collector import CollectorSample, collect_samples_to_jsonl
from iracing_ai_engineer.events import EVENT_CONTRACT_VERSION
from iracing_ai_engineer.sdk_probe import RawSdkFrame, VariableDescriptor
from iracing_ai_engineer.telemetry import SourceKind, normalize_sdk_frame


def _samples(source_kind: SourceKind):
    first = normalize_sdk_frame(
        {
            "SessionNum": 1,
            "SessionTick": 100,
            "SessionTime": 10.0,
            "Lap": 4,
            "LapCompleted": 3,
            "LapDistPct": 0.95,
        },
        source_id="fixture-source",
        session_id="fixture-session",
        source_kind=source_kind,
        buffer_tick=1_000,
        captured_monotonic_s=100.0,
    )
    second = normalize_sdk_frame(
        {
            "SessionNum": 1,
            "SessionTick": 101,
            "SessionTime": 11.0,
            "Lap": 5,
            "LapCompleted": 4,
            "LapDistPct": 0.05,
        },
        source_id="fixture-source",
        session_id="fixture-session",
        source_kind=source_kind,
        buffer_tick=1_001,
        captured_monotonic_s=100.1,
        previous=first,
    )
    return iter((first, second))


def _collector_evidence(
    *,
    completion_status: str = "COMPLETE",
    records_sha256: str = "a" * 64,
    source_kind: SourceKind = SourceKind.SDK_LIVE,
) -> CollectorInputEvidence:
    return CollectorInputEvidence(
        source_id="fixture-source",
        session_id="fixture-session",
        source_kind=source_kind,
        sim_mode="replay" if source_kind is SourceKind.REPLAY_SDK_PROXY else "full",
        completion_status=completion_status,
        semantic_record_count=5,
        records_sha256=records_sha256,
        frame_record_count=2,
        event_record_count=0,
        schema_record_count=1,
        session_info_record_count=1,
        samples_seen=2,
        duplicate_sample_count=0,
        duplicate_conflict_count=0,
        dropped_tick_count=0,
        stale_event_count=0,
        session_reset_count=0,
        schema_change_count=0,
        schema_epoch_count=1,
        session_epoch_count=1,
        first_buffer_tick=1_000,
        last_buffer_tick=1_001,
    )


def test_events_ibt_requires_explicit_identity_and_prints_receipt(
    monkeypatch, capsys, tmp_path
):
    path = tmp_path / "fixture.IBT"
    path.write_bytes(b"fixture-ibt-bytes")
    observed: dict[str, object] = {}

    @contextmanager
    def fake_open(received_path, **kwargs):
        observed.update({"path": received_path, **kwargs})
        yield SimpleNamespace(
            evidence=IbtInputEvidence(
                source_id=kwargs["source_id"],
                session_id=kwargs["session_id"],
                source_sha256="c" * 64,
                byte_size=path.stat().st_size,
                record_count=2,
                tick_rate_hz=60,
            ),
            samples=_samples(SourceKind.IBT_OFFLINE),
        )

    monkeypatch.setattr(adapters, "open_ibt_telemetry", fake_open)

    exit_code = cli.main(
        [
            "events",
            str(path),
            "--source-id",
            "public-audi-spa",
            "--session-id",
            "fixture-session",
            "--stale-after-seconds",
            "0.75",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert observed == {
        "path": path,
        "source_id": "public-audi-spa",
        "session_id": "fixture-session",
        "stale_after_s": 0.75,
    }
    assert payload["contract_version"] == cli.EVENT_REPLAY_CONTRACT_VERSION
    assert payload["input_kind"] == "ibt"
    assert payload["event_receipt"]["contract_version"] == EVENT_CONTRACT_VERSION
    assert payload["event_receipt"]["sample_count"] == 2
    assert payload["event_receipt"]["rejected_sample_count"] == 0
    assert payload["input_evidence"]["source_id"] == "public-audi-spa"
    assert payload["input_evidence"]["completion_status"] == "COMPLETE"
    assert payload["normalization"]["stale_after_us"] == 750_000
    assert len(payload["event_replay_sha256"]) == 64
    assert "events" not in payload


@pytest.mark.parametrize(
    ("extra_args", "expected_require_receipt"),
    [([], True), (["--allow-incomplete-collector"], False)],
)
def test_events_collector_uses_bound_identity_and_receipt_policy(
    monkeypatch, capsys, tmp_path, extra_args, expected_require_receipt
):
    path = tmp_path / "capture.ndjson"
    observed: dict[str, object] = {}

    @contextmanager
    def fake_open(received_path, **kwargs):
        observed.update({"path": received_path, **kwargs})
        completion = "COMPLETE" if kwargs["require_receipt"] else "INCOMPLETE_RECOVERY"
        yield SimpleNamespace(
            evidence=_collector_evidence(completion_status=completion),
            samples=_samples(SourceKind.SDK_LIVE),
        )

    monkeypatch.setattr(adapters, "open_collector_jsonl", fake_open)

    exit_code = cli.main(
        [
            "events",
            str(path),
            "--stale-after-seconds",
            "1.25",
            "--include-events",
            *extra_args,
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert observed == {
        "path": path,
        "stale_after_s": 1.25,
        "require_receipt": expected_require_receipt,
    }
    assert payload["input_kind"] == "collector"
    assert payload["event_receipt"]["sample_count"] == 2
    assert payload["input_evidence"]["completion_status"] == (
        "COMPLETE" if expected_require_receipt else "INCOMPLETE_RECOVERY"
    )
    assert payload["quality_gate"] == (
        {"reasons": [], "status": "PASS"}
        if expected_require_receipt
        else {"reasons": ["INCOMPLETE_RECOVERY"], "status": "DEGRADED"}
    )
    assert [event["kind"] for event in payload["events"]] == [
        "source_started",
        "session_started",
        "lap_completed",
        "lap_wrap",
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["events", "capture.ibt"], "requires --source-id and --session-id"),
        (
            [
                "events",
                "capture.jsonl",
                "--source-id",
                "wrong-source",
            ],
            "cannot be relabeled",
        ),
        (
            [
                "events",
                "capture.ibt",
                "--source-id",
                "source",
                "--session-id",
                "session",
                "--allow-incomplete-collector",
            ],
            "valid only for collector input",
        ),
        (["events", "capture.bin"], "cannot infer event input kind"),
    ],
)
def test_events_rejects_ambiguous_or_provenance_unsafe_arguments(
    capsys, arguments, message
):
    exit_code = cli.main(arguments)
    captured = capsys.readouterr()

    assert exit_code == 3
    assert message in captured.err
    assert json.loads(captured.out) == {
        "contract_version": cli.EVENT_REPLAY_CONTRACT_VERSION,
        "error": "EVENT_REPLAY_ERROR",
        "message": captured.err.strip(),
    }


@pytest.mark.parametrize(
    "error",
    [
        TelemetryAdapterError("receipt hash mismatch"),
        OverflowError("integer conversion overflow"),
        RecursionError("JSON nesting too deep"),
    ],
)
def test_events_adapter_failure_is_machine_readable(
    monkeypatch, capsys, tmp_path, error
):
    path = tmp_path / "capture.jsonl"

    @contextmanager
    def fail(*args, **kwargs):
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(adapters, "open_collector_jsonl", fail)

    exit_code = cli.main(["events", str(path)])
    captured = capsys.readouterr()

    assert exit_code == 3
    assert json.loads(captured.out) == {
        "contract_version": cli.EVENT_REPLAY_CONTRACT_VERSION,
        "error": "EVENT_REPLAY_ERROR",
        "message": str(error),
    }
    assert captured.err.strip() == str(error)


def test_events_explicit_input_kind_supports_nonstandard_extension(
    monkeypatch, capsys, tmp_path
):
    path = tmp_path / "recovered.log"

    @contextmanager
    def fake_open(*args, **kwargs):
        yield SimpleNamespace(
            evidence=_collector_evidence(
                source_kind=SourceKind.REPLAY_SDK_PROXY
            ),
            samples=_samples(SourceKind.REPLAY_SDK_PROXY),
        )

    monkeypatch.setattr(adapters, "open_collector_jsonl", fake_open)

    exit_code = cli.main(
        ["events", str(path), "--input-kind", "collector"]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["input_kind"] == "collector"


def test_event_replay_hash_binds_input_completion_and_normalization():
    event_receipt = {
        "accepted_sample_count": 1,
        "contract_version": EVENT_CONTRACT_VERSION,
        "events_sha256": "e" * 64,
        "rejected_sample_count": 0,
        "sample_count": 1,
    }

    complete = cli._event_replay_payload(
        input_kind="collector",
        input_evidence=_collector_evidence().to_dict(),
        stale_after_s=0.5,
        event_receipt=event_receipt,
        events=None,
    )
    different_input = cli._event_replay_payload(
        input_kind="collector",
        input_evidence=_collector_evidence(records_sha256="b" * 64).to_dict(),
        stale_after_s=0.5,
        event_receipt=event_receipt,
        events=None,
    )
    recovery = cli._event_replay_payload(
        input_kind="collector",
        input_evidence=_collector_evidence(
            completion_status="INCOMPLETE_RECOVERY"
        ).to_dict(),
        stale_after_s=0.5,
        event_receipt=event_receipt,
        events=None,
    )
    different_threshold = cli._event_replay_payload(
        input_kind="collector",
        input_evidence=_collector_evidence().to_dict(),
        stale_after_s=1.0,
        event_receipt=event_receipt,
        events=None,
    )

    hashes = {
        payload["event_replay_sha256"]
        for payload in (complete, different_input, recovery, different_threshold)
    }
    assert len(hashes) == 4
    assert complete["quality_gate"]["status"] == "PASS"
    assert recovery["quality_gate"]["reasons"] == ["INCOMPLETE_RECOVERY"]
    assert complete["normalization"]["config_sha256"] != (
        different_threshold["normalization"]["config_sha256"]
    )


@pytest.mark.parametrize(
    ("counts", "expected_reasons"),
    [
        (
            {"sample_count": 1, "accepted_sample_count": 0, "rejected_sample_count": 1},
            ["NO_ACCEPTED_SAMPLES", "NORMALIZED_REJECTED_SAMPLES"],
        ),
        (
            {"sample_count": 0, "accepted_sample_count": 0, "rejected_sample_count": 0},
            ["NO_NORMALIZED_SAMPLES"],
        ),
    ],
)
def test_event_replay_quality_gate_blocks_unusable_normalized_samples(
    counts, expected_reasons
):
    payload = cli._event_replay_payload(
        input_kind="collector",
        input_evidence=_collector_evidence().to_dict(),
        stale_after_s=0.5,
        event_receipt={"contract_version": EVENT_CONTRACT_VERSION, **counts},
        events=None,
    )

    assert payload["quality_gate"] == {
        "reasons": expected_reasons,
        "status": "DEGRADED",
    }


def test_events_cli_runs_real_collector_adapter_and_surfaces_quality_gate(
    capsys, tmp_path
):
    path = tmp_path / "real-collector.jsonl"
    descriptors = (
        VariableDescriptor("SessionNum", 2, "int32", 0, 1, False, "", ""),
        VariableDescriptor("SessionTick", 2, "int32", 4, 1, False, "", ""),
        VariableDescriptor("SessionTime", 4, "float32", 8, 1, False, "s", ""),
        VariableDescriptor("Lap", 2, "int32", 12, 1, False, "", ""),
        VariableDescriptor("LapCompleted", 2, "int32", 16, 1, False, "", ""),
        VariableDescriptor("LapDistPct", 4, "float32", 20, 1, False, "%", ""),
    )
    observations = tuple(
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=buffer_tick,
                session_info_update=1,
                values={
                    "SessionNum": 1,
                    "SessionTick": 100 + index,
                    "SessionTime": 10.0 + index,
                    "Lap": 4 + index,
                    "LapCompleted": 3 + index,
                    "LapDistPct": 0.95 if index == 0 else 0.05,
                },
                sim_mode_raw="full",
                captured_monotonic_s=100.0 + index * 0.1,
            ),
            descriptors=descriptors,
            tick_rate_hz=60,
            session_info={"WeekendInfo": {"SimMode": "full"}},
        )
        for index, buffer_tick in enumerate((10, 12))
    )
    collect_samples_to_jsonl(
        observations,
        path,
        source_id="real-fixture-rig",
        session_id="real-fixture-session",
    )

    exit_code = cli.main(["events", str(path), "--include-events"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["input_evidence"]["completion_status"] == "COMPLETE"
    assert payload["input_evidence"]["dropped_tick_count"] == 1
    assert payload["quality_gate"] == {
        "reasons": ["DROPPED_TICKS"],
        "status": "DEGRADED",
    }
    assert payload["event_receipt"]["event_kind_counts"]["dropped_ticks"] == 1
    assert len(payload["event_replay_sha256"]) == 64


def test_events_help_documents_full_validation_and_crash_recovery(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._parser().parse_args(["events", "--help"])

    assert exc_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Fully validate" in help_text
    assert "crash prefix" in help_text
    assert "cannot be relabeled" not in help_text
    assert "bound by its run record" in help_text


def test_events_rejects_nonpositive_stale_threshold(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._parser().parse_args(
            [
                "events",
                str(Path("capture.jsonl")),
                "--stale-after-seconds",
                "0",
            ]
        )

    assert exc_info.value.code == 2
    assert "finite number greater than zero" in capsys.readouterr().err
