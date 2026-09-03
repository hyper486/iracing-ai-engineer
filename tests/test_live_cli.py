from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from iracing_ai_engineer import cli, live_supervisor
from iracing_ai_engineer.collector import (
    COLLECTOR_CONTRACT_VERSION,
    CollectorConsistencyError,
)
from iracing_ai_engineer.sdk_probe import SdkProbeConsistencyError
from iracing_ai_engineer.telemetry import SourceKind


@dataclass(frozen=True)
class _Receipt:
    records_sha256: str = "a" * 64
    completion_status: str = "COMPLETE"

    def to_dict(self) -> dict[str, str]:
        return {
            "completion_status": self.completion_status,
            "records_sha256": self.records_sha256,
        }


def test_collect_live_maps_all_arguments_and_prints_only_receipt(
    monkeypatch, capsys, tmp_path
):
    output = tmp_path / "live.jsonl"
    transport = object()
    observed: dict[str, object] = {}

    monkeypatch.setattr(cli, "WindowsPyirsdkTransport", lambda: transport)

    def collect(received_transport, received_output, **kwargs):
        observed.update(
            {
                "transport": received_transport,
                "output": received_output,
                **kwargs,
            }
        )
        return _Receipt()

    monkeypatch.setattr(cli, "collect_transport_to_jsonl", collect)

    exit_code = cli.main(
        [
            "collect-live",
            str(output),
            "--source-id",
            "rig-a",
            "--session-id",
            "race-42",
            "--wait-seconds",
            "1.5",
            "--duration-seconds",
            "90",
            "--poll-seconds",
            "0.02",
            "--stale-after-seconds",
            "0.75",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert json.loads(captured.out) == _Receipt().to_dict()
    assert observed == {
        "transport": transport,
        "output": output,
        "source_id": "rig-a",
        "session_id": "race-42",
        "expected_source_kind": None,
        "wait_seconds": 1.5,
        "duration_s": 90.0,
        "poll_seconds": 0.02,
        "fields": None,
        "stale_after_s": 0.75,
        "include_driver_info": False,
        "fsync_each_record": True,
    }


@pytest.mark.parametrize(
    ("cli_value", "expected"),
    [
        ("live", SourceKind.SDK_LIVE),
        ("replay", SourceKind.REPLAY_SDK_PROXY),
    ],
)
def test_collect_live_maps_expected_source_kind(
    monkeypatch, capsys, tmp_path, cli_value, expected
):
    observed: dict[str, object] = {}
    monkeypatch.setattr(cli, "WindowsPyirsdkTransport", object)

    def collect(*args, **kwargs):
        observed.update(kwargs)
        return _Receipt()

    monkeypatch.setattr(cli, "collect_transport_to_jsonl", collect)

    exit_code = cli.main(
        [
            "collect-live",
            str(tmp_path / f"{cli_value}.jsonl"),
            "--source-id",
            "rig-a",
            "--session-id",
            "race-42",
            "--expected-source-kind",
            cli_value,
        ]
    )

    assert exit_code == 0
    assert observed["expected_source_kind"] is expected
    assert json.loads(capsys.readouterr().out)["completion_status"] == "COMPLETE"


def test_collect_live_never_overwrites_existing_output(monkeypatch, capsys, tmp_path):
    output = tmp_path / "protected.jsonl"
    output.write_text("user data\n", encoding="utf-8")

    def unexpected_transport():
        raise AssertionError("transport must not be constructed for an existing output")

    monkeypatch.setattr(cli, "WindowsPyirsdkTransport", unexpected_transport)

    exit_code = cli.main(
        [
            "collect-live",
            str(output),
            "--source-id",
            "rig-a",
            "--session-id",
            "race-42",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert json.loads(captured.out) == {
        "contract_version": COLLECTOR_CONTRACT_VERSION,
        "error": "OUTPUT_EXISTS",
        "message": f"collector output already exists: {output}",
    }
    assert "already exists" in captured.err
    assert output.read_text(encoding="utf-8") == "user data\n"


def test_collect_live_is_unavailable_off_windows(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("iracing_ai_engineer.sdk_probe.platform.system", lambda: "Darwin")

    exit_code = cli.main(
        [
            "collect-live",
            str(tmp_path / "live.jsonl"),
            "--source-id",
            "rig-a",
            "--session-id",
            "race-42",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(captured.out)["error"] == "SDK_UNAVAILABLE"
    assert "requires Windows" in captured.err


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (CollectorConsistencyError("bad collector frame"), "COLLECTOR_CONSISTENCY_ERROR"),
        (SdkProbeConsistencyError("bad SDK layout"), "SDK_CONSISTENCY_ERROR"),
        (OSError("disk unavailable"), "IO_ERROR"),
    ],
)
def test_collect_live_runtime_failures_are_machine_readable(
    monkeypatch, capsys, tmp_path, error, error_code
):
    monkeypatch.setattr(cli, "WindowsPyirsdkTransport", object)

    def collect(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli, "collect_transport_to_jsonl", collect)

    exit_code = cli.main(
        [
            "collect-live",
            str(tmp_path / "live.jsonl"),
            "--source-id",
            "rig-a",
            "--session-id",
            "race-42",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert json.loads(captured.out) == {
        "contract_version": COLLECTOR_CONTRACT_VERSION,
        "error": error_code,
        "message": str(error),
    }
    assert captured.err.strip() == str(error)


def test_collect_live_help_explains_windows_console_session(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._parser().parse_args(["collect-live", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    normalized_help = " ".join(help_text.split())
    assert "same logged-in Windows console session" in normalized_help
    assert "existing path is never overwritten" in normalized_help


@pytest.mark.parametrize(
    "option",
    [
        "--duration-seconds",
        "--poll-seconds",
        "--stale-after-seconds",
    ],
)
def test_collect_live_rejects_nonpositive_durations(option, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._parser().parse_args(
            [
                "collect-live",
                str(Path("live.jsonl")),
                "--source-id",
                "rig-a",
                "--session-id",
                "race-42",
                option,
                "0",
            ]
        )

    assert exc_info.value.code == 2
    assert "finite number greater than zero" in capsys.readouterr().err


def test_collect_live_rejects_nonfinite_wait(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._parser().parse_args(
            [
                "collect-live",
                "live.jsonl",
                "--source-id",
                "rig-a",
                "--session-id",
                "race-42",
                "--wait-seconds",
                "nan",
            ]
        )

    assert exc_info.value.code == 2
    assert "finite number" in capsys.readouterr().err


@pytest.mark.parametrize("identifier", ["", " padded", "padded ", "bad\nvalue"])
def test_collect_live_rejects_ambiguous_run_identifiers(identifier, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._parser().parse_args(
            [
                "collect-live",
                "live.jsonl",
                "--source-id",
                identifier,
                "--session-id",
                "race-42",
            ]
        )

    assert exc_info.value.code == 2
    assert "non-empty identifier" in capsys.readouterr().err


@pytest.mark.parametrize(
    "payload",
    [
        {
            "contract_version": "windows-live-supervisor-v1",
            "simulator_identity": None,
            "status": "WAIT",
            "wait_reasons": ["WAIT_SIMULATOR"],
        },
        {
            "analysis_sha256": "a" * 64,
            "contract_version": "windows-live-supervisor-v1",
            "status": "READY",
        },
    ],
)
def test_supervise_r8_is_zero_argument_compact_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    monkeypatch.setattr(live_supervisor, "run_live_supervisor", lambda: payload)
    assert cli.main(["supervise-r8"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def test_supervise_r8_rejects_every_caller_controlled_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parsed = cli._parser().parse_args(["supervise-r8"])
    assert vars(parsed) == {"command": "supervise-r8"}
    with pytest.raises(SystemExit) as raised:
        cli._parser().parse_args(["supervise-r8", "--duration-seconds", "1"])
    assert raised.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_supervise_r8_failure_is_compact_stderr_only_and_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> dict[str, object]:
        raise live_supervisor.LiveSupervisorError(
            "RUNTIME_ISOLATION_REQUIRED",
            "production supervisor requires protected Windows python -I -B",
        )

    monkeypatch.setattr(live_supervisor, "run_live_supervisor", fail)
    assert cli.main(["supervise-r8"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    expected = {
        "contract_version": live_supervisor.SUPERVISOR_CONTRACT_VERSION,
        "error": "RUNTIME_ISOLATION_REQUIRED",
        "message": "production supervisor requires protected Windows python -I -B",
        "status": "FAILED",
    }
    assert captured.err == (
        json.dumps(
            expected,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def test_supervise_r8_rejects_nonterminal_success_payload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        live_supervisor,
        "run_live_supervisor",
        lambda: {"status": "FAILED"},
    )
    assert cli.main(["supervise-r8"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"] == "SUPERVISOR_RESULT_INVALID"
    assert error["status"] == "FAILED"


def test_supervise_r8_unexpected_fatal_is_terminal_json_and_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> dict[str, object]:
        raise RuntimeError("synthetic unexpected supervisor failure")

    monkeypatch.setattr(live_supervisor, "run_live_supervisor", fail)
    assert cli.main(["supervise-r8"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error == {
        "contract_version": live_supervisor.SUPERVISOR_CONTRACT_VERSION,
        "error": "UNEXPECTED_SUPERVISOR_FATAL",
        "message": "synthetic unexpected supervisor failure",
        "status": "FAILED",
    }
    assert captured.err.count("\n") == 1
