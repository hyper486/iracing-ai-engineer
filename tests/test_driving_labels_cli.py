from __future__ import annotations

import copy
import json

import pytest
from test_driving_labels import _approved, _candidate, _replay

import iracing_ai_engineer
from iracing_ai_engineer import cli
from iracing_ai_engineer.driving_labels import (
    DRIVING_LABELS_CONTRACT_VERSION,
    PENDING_HUMAN_REVIEW,
    SELF_ATTESTED_NOT_AUTHENTICATED,
    WAIT_HUMAN_AUTHENTICATION,
    validate_driving_labels,
)


def _write_json(path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def test_propose_writes_strict_pending_artifact_and_returns_wait(capsys, tmp_path):
    replay_path = tmp_path / "driving-replay.json"
    output_path = tmp_path / "candidate.json"
    _write_json(replay_path, _replay())

    exit_code = cli.main(
        [
            "driving-labels",
            "propose",
            str(replay_path),
            "--output",
            str(output_path),
            "--label-set-id",
            "audi-spa-v1",
            "--car-key",
            "audir8lmsevo2gt3",
            "--track-key",
            "spa",
            "--layout-key",
            "grand-prix-pit",
        ]
    )

    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 5
    assert captured.err == ""
    assert receipt == {
        "artifact_path": str(output_path),
        "artifact_sha256": artifact["artifact_sha256"],
        "candidate_payload_sha256": artifact["candidate_payload_sha256"],
        "contract_version": DRIVING_LABELS_CONTRACT_VERSION,
        "reason": "INDEPENDENT_HUMAN_REVIEW_REQUIRED",
        "review_authenticity_status": None,
        "review_status": PENDING_HUMAN_REVIEW,
        "status": "WAIT_HUMAN_LABELS",
    }
    assert validate_driving_labels(artifact) == artifact
    assert artifact["review"]["status"] == PENDING_HUMAN_REVIEW
    assert artifact["human_labels"] == []


def test_propose_never_overwrites_an_existing_candidate(capsys, tmp_path):
    replay_path = tmp_path / "driving-replay.json"
    output_path = tmp_path / "candidate.json"
    _write_json(replay_path, _replay())
    output_path.write_text("user data\n", encoding="utf-8")

    exit_code = cli.main(
        [
            "driving-labels",
            "propose",
            str(replay_path),
            "--output",
            str(output_path),
            "--label-set-id",
            "audi-spa-v1",
            "--car-key",
            "audir8lmsevo2gt3",
            "--track-key",
            "spa",
            "--layout-key",
            "grand-prix-pit",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert json.loads(captured.out)["error"] == "OUTPUT_EXISTS"
    assert output_path.read_text(encoding="utf-8") == "user data\n"


@pytest.mark.parametrize(
    ("artifact_factory", "expected_exit", "expected_status"),
    [
        (_candidate, 5, "WAIT_HUMAN_LABELS"),
        (_approved, 5, WAIT_HUMAN_AUTHENTICATION),
    ],
)
def test_validate_reports_approval_gate_machine_readably(
    capsys,
    tmp_path,
    artifact_factory,
    expected_exit,
    expected_status,
):
    labels_path = tmp_path / "labels.json"
    _write_json(labels_path, artifact_factory())

    exit_code = cli.main(["driving-labels", "validate", str(labels_path)])

    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert exit_code == expected_exit
    assert captured.err == ""
    assert receipt["contract_version"] == DRIVING_LABELS_CONTRACT_VERSION
    assert receipt["status"] == expected_status
    if expected_status == "WAIT_HUMAN_LABELS":
        assert receipt["reason"] == "LABEL_SET_NOT_APPROVED"
    else:
        assert receipt["review_status"] == "APPROVED"
        assert receipt["reason"] == SELF_ATTESTED_NOT_AUTHENTICATED
        assert receipt["review_authenticity_status"] == (
            SELF_ATTESTED_NOT_AUTHENTICATED
        )
        assert receipt["structural_validation_status"] == "PASS"
        assert receipt["trusted_status"] == WAIT_HUMAN_AUTHENTICATION
        assert receipt["labels_content_sha256"] is not None


@pytest.mark.parametrize(
    ("artifact_factory", "expected_exit", "expected_status"),
    [
        (_candidate, 5, "WAIT_HUMAN_LABELS"),
        (_approved, 5, WAIT_HUMAN_AUTHENTICATION),
    ],
)
def test_regress_runs_only_for_approved_labels(
    capsys,
    tmp_path,
    artifact_factory,
    expected_exit,
    expected_status,
):
    labels_path = tmp_path / "labels.json"
    replay_path = tmp_path / "replay.json"
    _write_json(labels_path, artifact_factory())
    _write_json(replay_path, _replay())

    exit_code = cli.main(
        ["driving-labels", "regress", str(labels_path), str(replay_path)]
    )

    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert exit_code == expected_exit
    assert captured.err == ""
    assert receipt["status"] == expected_status
    if expected_status == WAIT_HUMAN_AUTHENTICATION:
        assert receipt["comparator_status"] == "PASS"
        assert receipt["trusted_regression_status"] == WAIT_HUMAN_AUTHENTICATION
        assert receipt["reasons"] == [SELF_ATTESTED_NOT_AUTHENTICATED]
        assert receipt["summary"]["failed_field_count"] == 0
        assert receipt["labels_content_sha256"] is not None
    else:
        assert receipt["reasons"] == ["LABEL_SET_NOT_APPROVED"]


def test_regress_returns_nonzero_for_an_approved_tolerance_failure(capsys, tmp_path):
    labels_path = tmp_path / "labels.json"
    replay_path = tmp_path / "replay.json"
    replay = _replay()
    _write_json(labels_path, _approved(replay))
    changed = copy.deepcopy(replay)
    changed["model_output"]["corner_metrics"][0]["brake_onset_m"] = 120.0
    # Reuse the fixture's production-equivalent hash rebinding helper through
    # a fresh proposal-independent replay construction.
    from test_driving_labels import _rehash_replay

    _write_json(replay_path, _rehash_replay(changed))

    exit_code = cli.main(
        ["driving-labels", "regress", str(labels_path), str(replay_path)]
    )

    receipt = json.loads(capsys.readouterr().out)
    assert exit_code == 5
    assert receipt["status"] == "FAIL"
    assert receipt["summary"]["failed_field_count"] == 1


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "driving-labels",
            "propose",
            "missing-replay.json",
            "--output",
            "candidate.json",
            "--label-set-id",
            "audi-spa-v1",
            "--car-key",
            "audir8lmsevo2gt3",
            "--track-key",
            "spa",
            "--layout-key",
            "grand-prix-pit",
        ],
        ["driving-labels", "validate", "missing-labels.json"],
        [
            "driving-labels",
            "regress",
            "missing-labels.json",
            "missing-replay.json",
        ],
    ],
)
def test_driving_label_file_commands_report_wait_data(capsys, tmp_path, arguments):
    adjusted = [
        str(tmp_path / item) if item.startswith("missing-") or item == "candidate.json" else item
        for item in arguments
    ]

    exit_code = cli.main(adjusted)

    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert exit_code == 4
    assert receipt["contract_version"] == DRIVING_LABELS_CONTRACT_VERSION
    assert receipt["error"] == "WAIT_DATA"


def test_tampered_label_artifact_is_a_machine_readable_contract_error(capsys, tmp_path):
    labels_path = tmp_path / "labels.json"
    labels = _candidate()
    labels["proposals"][0]["ordinal"] = 2
    _write_json(labels_path, labels)

    exit_code = cli.main(["driving-labels", "validate", str(labels_path)])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert json.loads(captured.out)["error"] == "DRIVING_LABELS_ERROR"
    assert "contiguous" in captured.err


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"contract_version":"x","contract_version":"y"}', "duplicate JSON"),
        ('{"value":NaN}', "non-finite JSON"),
        ('{"value":Infinity}', "non-finite JSON"),
    ],
)
def test_label_cli_rejects_ambiguous_or_nonfinite_json(
    capsys, tmp_path, raw, message
):
    labels_path = tmp_path / "ambiguous.json"
    labels_path.write_text(raw, encoding="utf-8")

    exit_code = cli.main(["driving-labels", "validate", str(labels_path)])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert json.loads(captured.out)["error"] == "DRIVING_LABELS_ERROR"
    assert message in captured.err


def test_public_package_exports_label_operations_lazily():
    assert iracing_ai_engineer.build_driving_label_candidate is not None
    assert iracing_ai_engineer.validate_driving_labels is not None
    assert iracing_ai_engineer.regress_driving_labels is not None
    assert iracing_ai_engineer.DrivingLabelsError is not None
