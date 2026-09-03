from __future__ import annotations

import json

import pytest

from iracing_ai_engineer import cli
from iracing_ai_engineer.contracts import (
    QUALITY_PROFILE_VERSION,
    REPLAY_CONTRACT_VERSION,
    SHADOW_REPORT_CONTRACT_VERSION,
)
from iracing_ai_engineer.model_replay import FUEL_MODEL_REPLAY_CONTRACT_VERSION


@pytest.mark.parametrize(
    ("arguments", "contract_version"),
    [
        (["inspect", "missing.ibt"], QUALITY_PROFILE_VERSION),
        (["replay", "missing.ibt"], REPLAY_CONTRACT_VERSION),
        (
            [
                "events",
                "missing.ibt",
                "--source-id",
                "missing-source",
                "--session-id",
                "missing-session",
            ],
            cli.EVENT_REPLAY_CONTRACT_VERSION,
        ),
        (
            [
                "fuel-replay",
                "missing.ibt",
                "--source-id",
                "missing-source",
                "--session-id",
                "missing-session",
                "--current-fuel-l",
                "20",
                "--tank-capacity-l",
                "120",
                "--refuel-rate-lps",
                "2",
                "--remaining-laps",
                "10",
            ],
            FUEL_MODEL_REPLAY_CONTRACT_VERSION,
        ),
        (
            ["shadow", "missing.ibt", "--analysis", "driving"],
            SHADOW_REPORT_CONTRACT_VERSION,
        ),
    ],
)
def test_file_commands_report_missing_input_as_machine_readable_wait_data(
    capsys,
    tmp_path,
    arguments,
    contract_version,
):
    missing = tmp_path / arguments[1]
    arguments = [arguments[0], str(missing), *arguments[2:]]

    exit_code = cli.main(arguments)

    captured = capsys.readouterr()
    assert exit_code == 4
    assert json.loads(captured.out) == {
        "contract_version": contract_version,
        "error": "WAIT_DATA",
        "message": str(missing),
    }
    assert captured.err.strip() == str(missing)
