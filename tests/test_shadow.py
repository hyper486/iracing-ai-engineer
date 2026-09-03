from __future__ import annotations

import json

import pytest

from iracing_ai_engineer import cli
from iracing_ai_engineer.shadow import (
    FuelScenario,
    ShadowReportError,
    _digest,
    _parse_track_length_m,
    build_shadow_report,
)


def test_track_length_parser_is_strict_and_unit_aware():
    assert _parse_track_length_m("6.93 km") == 6930.0
    assert _parse_track_length_m("1200 m") == 1200.0
    with pytest.raises(ShadowReportError, match="UNSUPPORTED_TRACK_LENGTH"):
        _parse_track_length_m("6.93 miles")


def test_fuel_scenario_preserves_user_rule_provenance():
    scenario = FuelScenario(
        current_fuel_l=20.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
    )

    payload = scenario.to_dict()

    assert payload["current_fuel_l"] == {"value": 20.0, "provenance": "USER_RULE"}
    assert payload["remaining_laps"] == {"value": 10, "provenance": "USER_RULE"}
    assert "remaining_time_s" not in payload


def test_canonical_digest_ignores_mapping_insertion_order():
    assert _digest({"b": 2, "a": [3, 1]}) == _digest({"a": [3, 1], "b": 2})


def test_missing_fuel_scenario_fails_before_source_access(tmp_path):
    with pytest.raises(ShadowReportError, match="FUEL_SCENARIO_REQUIRED"):
        build_shadow_report(tmp_path / "absent.ibt")


def _fake_shadow_report() -> dict[str, object]:
    return {
        "contract_version": "shadow-report-v2",
        "capabilities": {
            "driving_analysis_smoke": {"status": "PASS"},
            "personalized_coaching": {"status": "SKIP"},
        },
        "receipt": {"analysis_sha256": "a" * 64},
    }


def test_shadow_cli_receipt_only_retains_capability_exit_check(monkeypatch, capsys):
    monkeypatch.setattr(
        "iracing_ai_engineer.shadow.build_shadow_report",
        lambda *args, **kwargs: _fake_shadow_report(),
    )

    exit_code = cli.main(
        [
            "shadow",
            "unused.ibt",
            "--analysis",
            "driving",
            "--receipt-only",
            "--require-capability",
            "driving_analysis_smoke",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload == {
        "contract_version": "shadow-report-v2",
        "receipt": {"analysis_sha256": "a" * 64},
    }


def test_shadow_cli_returns_five_when_explicit_capability_is_not_met(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        "iracing_ai_engineer.shadow.build_shadow_report",
        lambda *args, **kwargs: _fake_shadow_report(),
    )

    exit_code = cli.main(
        [
            "shadow",
            "unused.ibt",
            "--analysis",
            "driving",
            "--require-capability",
            "personalized_coaching",
        ]
    )

    assert exit_code == 5
    assert json.loads(capsys.readouterr().out)["capabilities"][
        "personalized_coaching"
    ]["status"] == "SKIP"


def test_shadow_cli_missing_input_is_machine_readable_wait_data(capsys, tmp_path):
    missing = tmp_path / "missing.ibt"

    exit_code = cli.main(
        [
            "shadow",
            str(missing),
            "--analysis",
            "driving",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert json.loads(captured.out) == {
        "contract_version": "shadow-report-v2",
        "error": "WAIT_DATA",
        "message": str(missing),
    }
