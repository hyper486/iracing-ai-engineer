from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import iracing_ai_engineer
import iracing_ai_engineer.adapters as adapters_module
import iracing_ai_engineer.condition_cohort as cohort_module
from iracing_ai_engineer import cli
from iracing_ai_engineer.adapters import TelemetryAdapterError
from iracing_ai_engineer.condition_cohort import (
    CONDITION_COHORT_CONTRACT_VERSION,
    HUMAN_TRACK_STATE_ATTESTATION,
    ApprovedTrackStateLabelSet,
    ConditionCohortError,
)


def _labels() -> ApprovedTrackStateLabelSet:
    return ApprovedTrackStateLabelSet.approved(
        source_binding_sha256="a" * 64,
        labels={1: "DRY_STABLE", 2: "DRY_STABLE"},
        reviewer_id="fixture-human-reviewer",
        reviewed_at_utc="2026-08-08T00:00:00Z",
        method="MANUAL_REPLAY_REVIEW",
        evidence_artifact_sha256="b" * 64,
        human_attestation=HUMAN_TRACK_STATE_ATTESTATION,
    )


def _payload(
    *,
    input_kind: str = "ibt",
    readiness_status: str = "WAIT_CONDITION_DATA",
    trusted_readiness_status: str = "WAIT_CONDITION_DATA",
) -> dict[str, object]:
    authenticity_reason = (
        "SELF_ATTESTED_NOT_AUTHENTICATED"
        if readiness_status == "PASS"
        else "APPROVED_TRACK_STATE_LABEL_MISSING"
    )
    authenticity_status = (
        "WAIT_HUMAN_AUTHENTICATION" if readiness_status == "PASS" else "WAIT_CONDITION_DATA"
    )
    return {
        "capabilities": {
            "track_state_authenticity": {
                "authenticated": False,
                "reasons": [authenticity_reason],
                "status": authenticity_status,
            }
        },
        "condition_cohort_sha256": "a" * 64,
        "condition_config_sha256": "b" * 64,
        "condition_provenance_sha256": "c" * 64,
        "condition_semantic_sha256": "d" * 64,
        "contract_version": CONDITION_COHORT_CONTRACT_VERSION,
        "input_kind": input_kind,
        "lap_conditions": [],
        "matched_lap_ordinals": [],
        "matcher_config": {"min_matched_laps": 8},
        "normalized_input_receipt": {
            "contract_version": "normalized-telemetry-v3",
            "sample_count": 0,
            "samples_sha256": "e" * 64,
        },
        "pairs": [],
        "quality_gate": {
            "reasons": [authenticity_reason],
            "status": "DEGRADED",
        },
        "readiness_status": readiness_status,
        "recommendations": [],
        "target_lap_ordinal": 11,
        "trusted_readiness_status": trusted_readiness_status,
    }


def _install_fake_ibt(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    observed_open: dict[str, object] = {}
    observed_build: dict[str, object] = {}
    active = {"value": False}
    run = SimpleNamespace(name="adapter-created-run")

    @contextmanager
    def fake_open(path, **kwargs):
        observed_open.update({"path": path, **kwargs})
        active["value"] = True
        try:
            yield run
        finally:
            active["value"] = False

    def fake_build(received_run, **kwargs):
        assert active["value"] is True
        observed_build.update({"run": received_run, **kwargs})
        return copy.deepcopy(payload)

    monkeypatch.setattr(adapters_module, "open_ibt_telemetry", fake_open)
    monkeypatch.setattr(cohort_module, "build_condition_cohort", fake_build)
    return observed_open, observed_build


def test_condition_cohort_ibt_auto_keeps_run_active_and_uses_default_config(
    monkeypatch,
    capsys,
    tmp_path,
):
    payload = _payload()
    observed_open, observed_build = _install_fake_ibt(monkeypatch, payload)
    path = tmp_path / "fixture.IBT"

    exit_code = cli.main(
        [
            "condition-cohort",
            str(path),
            "--source-id",
            "fixture-source",
            "--session-id",
            "fixture-session",
            "--target-lap-ordinal",
            "11",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert json.loads(captured.out) == payload
    assert observed_open == {
        "path": path,
        "source_id": "fixture-source",
        "session_id": "fixture-session",
        "stale_after_s": 0.5,
    }
    assert observed_build["target_lap_ordinal"] == 11
    assert observed_build["track_state_labels"] is None
    assert "config" not in observed_build


@pytest.mark.parametrize(
    ("extra_args", "expected_exit"),
    [([], 0), (["--require-ready"], 5)],
)
def test_missing_labels_is_normal_wait_unless_trusted_ready_is_required(
    monkeypatch,
    capsys,
    tmp_path,
    extra_args,
    expected_exit,
):
    _install_fake_ibt(monkeypatch, _payload())

    exit_code = cli.main(
        [
            "condition-cohort",
            str(tmp_path / "fixture.ibt"),
            "--source-id",
            "fixture-source",
            "--session-id",
            "fixture-session",
            "--target-lap-ordinal",
            "11",
            *extra_args,
        ]
    )

    receipt = json.loads(capsys.readouterr().out)
    assert exit_code == expected_exit
    assert receipt["readiness_status"] == "WAIT_CONDITION_DATA"
    assert receipt["trusted_readiness_status"] == "WAIT_CONDITION_DATA"
    assert receipt["recommendations"] == []


def test_require_ready_rejects_self_attested_matcher_pass_even_in_receipt_mode(
    monkeypatch,
    capsys,
    tmp_path,
):
    payload = _payload(
        readiness_status="PASS",
        trusted_readiness_status="WAIT_HUMAN_AUTHENTICATION",
    )
    _, observed_build = _install_fake_ibt(monkeypatch, payload)
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(json.dumps(_labels().to_dict()), encoding="utf-8")

    exit_code = cli.main(
        [
            "condition-cohort",
            str(tmp_path / "fixture.ibt"),
            "--source-id",
            "fixture-source",
            "--session-id",
            "fixture-session",
            "--target-lap-ordinal",
            "11",
            "--track-state-labels",
            str(labels_path),
            "--receipt-only",
            "--require-ready",
        ]
    )

    receipt = json.loads(capsys.readouterr().out)
    assert exit_code == 5
    assert receipt["readiness_status"] == "PASS"
    assert receipt["trusted_readiness_status"] == "WAIT_HUMAN_AUTHENTICATION"
    assert receipt["track_state_authenticity"] == {
        "authenticated": False,
        "reasons": ["SELF_ATTESTED_NOT_AUTHENTICATED"],
        "status": "WAIT_HUMAN_AUTHENTICATION",
    }
    assert receipt["matcher_config"]["min_matched_laps"] == 8
    assert receipt["recommendations"] == []
    assert "lap_conditions" not in receipt
    assert "pairs" not in receipt
    assert observed_build["track_state_labels"] == _labels()


def test_condition_cohort_collector_uses_bound_identity_and_complete_receipt(
    monkeypatch,
    capsys,
    tmp_path,
):
    observed: dict[str, object] = {}
    active = {"value": False}
    run = SimpleNamespace(name="collector-run")

    @contextmanager
    def fake_open(path, **kwargs):
        observed.update({"path": path, **kwargs})
        active["value"] = True
        try:
            yield run
        finally:
            active["value"] = False

    def fake_build(received_run, **kwargs):
        assert active["value"] is True
        assert received_run is run
        assert kwargs["target_lap_ordinal"] == 4
        return _payload(input_kind="collector")

    monkeypatch.setattr(adapters_module, "open_collector_jsonl", fake_open)
    monkeypatch.setattr(cohort_module, "build_condition_cohort", fake_build)
    path = tmp_path / "capture.ndjson"

    exit_code = cli.main(["condition-cohort", str(path), "--target-lap-ordinal", "4"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["input_kind"] == "collector"
    assert observed == {
        "path": path,
        "stale_after_s": 0.5,
        "require_receipt": True,
    }


def test_condition_cohort_explicit_input_kind_accepts_nonstandard_extension(
    monkeypatch,
    capsys,
    tmp_path,
):
    observed: dict[str, object] = {}

    @contextmanager
    def fake_open(path, **kwargs):
        observed.update({"path": path, **kwargs})
        yield SimpleNamespace(name="collector-run")

    monkeypatch.setattr(adapters_module, "open_collector_jsonl", fake_open)
    monkeypatch.setattr(
        cohort_module,
        "build_condition_cohort",
        lambda run, **kwargs: _payload(input_kind="collector"),
    )
    path = tmp_path / "capture.log"

    exit_code = cli.main(
        [
            "condition-cohort",
            str(path),
            "--input-kind",
            "collector",
            "--target-lap-ordinal",
            "2",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["input_kind"] == "collector"
    assert observed["require_receipt"] is True


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["condition-cohort", "capture.ibt", "--target-lap-ordinal", "1"],
            "requires --source-id and --session-id",
        ),
        (
            [
                "condition-cohort",
                "capture.jsonl",
                "--source-id",
                "wrong-source",
                "--target-lap-ordinal",
                "1",
            ],
            "cannot be relabeled",
        ),
        (
            ["condition-cohort", "capture.bin", "--target-lap-ordinal", "1"],
            "cannot infer condition-cohort input kind",
        ),
    ],
)
def test_condition_cohort_rejects_unsafe_or_ambiguous_arguments(
    capsys,
    arguments,
    message,
):
    exit_code = cli.main(arguments)

    captured = capsys.readouterr()
    assert exit_code == 3
    assert message in captured.err
    assert json.loads(captured.out)["error"] == "CONDITION_COHORT_ERROR"


def test_track_state_label_from_dict_is_exact_and_hash_bound():
    labels = _labels()

    assert ApprovedTrackStateLabelSet.from_dict(labels.to_dict()) == labels

    extra = labels.to_dict()
    extra["unexpected"] = True
    with pytest.raises(ConditionCohortError, match="keys mismatch"):
        ApprovedTrackStateLabelSet.from_dict(extra)

    missing = labels.to_dict()
    del missing["reviewer_id"]
    with pytest.raises(ConditionCohortError, match="keys mismatch"):
        ApprovedTrackStateLabelSet.from_dict(missing)

    tampered = labels.to_dict()
    tampered["reviewer_id"] = "different-reviewer"
    with pytest.raises(ConditionCohortError, match="label_set_sha256 mismatch"):
        ApprovedTrackStateLabelSet.from_dict(tampered)


def test_track_state_label_from_dict_requires_json_array_shape():
    artifact = _labels().to_dict()
    artifact["lap_labels"] = ((1, "DRY_STABLE"),)

    with pytest.raises(ConditionCohortError, match="JSON array"):
        ApprovedTrackStateLabelSet.from_dict(artifact)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            '{"contract_version":"x","contract_version":"y"}',
            "duplicate JSON",
        ),
        ('{"value":NaN}', "non-finite JSON"),
        ('{"value":Infinity}', "non-finite JSON"),
    ],
)
def test_condition_cohort_cli_rejects_ambiguous_or_nonfinite_label_json(
    capsys,
    tmp_path,
    raw,
    message,
):
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(raw, encoding="utf-8")

    exit_code = cli.main(
        [
            "condition-cohort",
            str(tmp_path / "fixture.ibt"),
            "--source-id",
            "fixture-source",
            "--session-id",
            "fixture-session",
            "--target-lap-ordinal",
            "11",
            "--track-state-labels",
            str(labels_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert message in captured.err
    assert json.loads(captured.out)["error"] == "CONDITION_COHORT_ERROR"


@pytest.mark.parametrize("missing_target", ["input", "labels"])
def test_condition_cohort_missing_files_are_wait_data(
    monkeypatch,
    capsys,
    tmp_path,
    missing_target,
):
    input_path = tmp_path / "missing.ibt"
    arguments = [
        "condition-cohort",
        str(input_path),
        "--source-id",
        "missing-source",
        "--session-id",
        "missing-session",
        "--target-lap-ordinal",
        "11",
    ]
    if missing_target == "labels":

        @contextmanager
        def must_not_open(*args, **kwargs):
            raise AssertionError("adapter must not open before label input exists")
            yield  # pragma: no cover

        monkeypatch.setattr(adapters_module, "open_ibt_telemetry", must_not_open)
        arguments.extend(["--track-state-labels", str(tmp_path / "missing-labels.json")])

    exit_code = cli.main(arguments)

    captured = capsys.readouterr()
    assert exit_code == 4
    assert json.loads(captured.out)["error"] == "WAIT_DATA"


@pytest.mark.parametrize(
    "error",
    [
        TelemetryAdapterError("collector receipt mismatch"),
        OverflowError("integer conversion overflow"),
        RecursionError("JSON nesting too deep"),
    ],
)
def test_condition_cohort_adapter_and_contract_errors_exit_three(
    monkeypatch,
    capsys,
    tmp_path,
    error,
):
    @contextmanager
    def fail(*args, **kwargs):
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(adapters_module, "open_collector_jsonl", fail)

    exit_code = cli.main(
        [
            "condition-cohort",
            str(tmp_path / "capture.jsonl"),
            "--target-lap-ordinal",
            "1",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert captured.err.strip() == str(error)
    assert json.loads(captured.out) == {
        "contract_version": CONDITION_COHORT_CONTRACT_VERSION,
        "error": "CONDITION_COHORT_ERROR",
        "message": str(error),
    }


def test_condition_cohort_label_source_binding_error_is_contract_error(
    monkeypatch,
    capsys,
    tmp_path,
):
    payload = _payload()
    _install_fake_ibt(monkeypatch, payload)

    def reject(*args, **kwargs):
        raise ConditionCohortError("track-state label set is not bound to input evidence")

    monkeypatch.setattr(cohort_module, "build_condition_cohort", reject)
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(json.dumps(_labels().to_dict()), encoding="utf-8")

    exit_code = cli.main(
        [
            "condition-cohort",
            str(tmp_path / "fixture.ibt"),
            "--source-id",
            "fixture-source",
            "--session-id",
            "fixture-session",
            "--target-lap-ordinal",
            "11",
            "--track-state-labels",
            str(labels_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "not bound" in captured.err
    assert json.loads(captured.out)["error"] == "CONDITION_COHORT_ERROR"


def test_condition_cohort_cli_refuses_recommendation_bearing_output(
    monkeypatch,
    capsys,
    tmp_path,
):
    payload = _payload()
    payload["recommendations"] = [{"action": "box"}]
    _install_fake_ibt(monkeypatch, payload)

    exit_code = cli.main(
        [
            "condition-cohort",
            str(tmp_path / "fixture.ibt"),
            "--source-id",
            "fixture-source",
            "--session-id",
            "fixture-session",
            "--target-lap-ordinal",
            "11",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "refuses recommendation-bearing output" in captured.err
    assert json.loads(captured.out)["error"] == "CONDITION_COHORT_ERROR"


def test_condition_cohort_help_has_no_threshold_override(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._parser().parse_args(["condition-cohort", "--help"])

    assert exc_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "fixed minimum of eight laps" in help_text
    assert "trusted_readiness_status" in help_text
    assert "--min-matched-laps" not in help_text


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "condition-cohort",
            "fixture.ibt",
            "--target-lap-ordinal",
            "-1",
        ],
        [
            "condition-cohort",
            "fixture.ibt",
            "--target-lap-ordinal",
            "1",
            "--stale-after-seconds",
            "0",
        ],
        [
            "condition-cohort",
            "fixture.ibt",
            "--target-lap-ordinal",
            "1",
            "--min-matched-laps",
            "2",
        ],
    ],
)
def test_condition_cohort_parser_rejects_invalid_or_threshold_override(
    capsys,
    arguments,
):
    with pytest.raises(SystemExit) as exc_info:
        cli._parser().parse_args(arguments)

    assert exc_info.value.code == 2
    assert capsys.readouterr().err


def test_public_package_exports_condition_cohort_operations_lazily():
    assert iracing_ai_engineer.ApprovedTrackStateLabelSet is not None
    assert iracing_ai_engineer.ConditionCohortConfig is not None
    assert iracing_ai_engineer.ConditionCohortError is not None
    assert iracing_ai_engineer.HUMAN_TRACK_STATE_ATTESTATION == HUMAN_TRACK_STATE_ATTESTATION
    assert iracing_ai_engineer.build_condition_cohort is not None
    assert (
        iracing_ai_engineer.CONDITION_COHORT_CONTRACT_VERSION == CONDITION_COHORT_CONTRACT_VERSION
    )
