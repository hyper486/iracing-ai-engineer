# ruff: noqa: E501
from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path, PurePosixPath

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "build_offline_session_report",
    Path("scripts/build_offline_session_report.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

LoadedReceipt = _MODULE.LoadedReceipt
SessionReportError = _MODULE.SessionReportError
canonical_sha256 = _MODULE.canonical_sha256
validate_session_report_artifact = _MODULE.validate_session_report_artifact


def _sha(character: str) -> str:
    return character * 64


def _receipt(file_name: str, canonical_field: str, digest: str) -> object:
    return LoadedReceipt(
        value={canonical_field: digest},
        file_name=file_name,
        serialized_sha256=hashlib.sha256(file_name.encode()).hexdigest(),
    )


def _gate(status: str, *, estimate_available: bool | None = None) -> dict[str, object]:
    result: dict[str, object] = {"reasons": [f"{status}_REASON"], "status": status}
    if estimate_available is not None:
        result["estimate_available"] = estimate_available
    return result


def _corner_card(
    *,
    corner_id: str,
    rank: int,
    median: float,
    loss_support_ordinals: list[int],
    loss_counter_ordinals: list[int],
    action_support_ordinals: list[int] | None = None,
    action_counter_ordinals: list[int] | None = None,
) -> dict[str, object]:
    ordinals = [2, 4, 5, 9, 10, 16]
    lap_id = {ordinal: f"lap-{ordinal}" for ordinal in ordinals}
    action_support = action_support_ordinals or []
    action_counter = action_counter_ordinals or []
    has_action = action_support_ordinals is not None
    per_lap = []
    for ordinal in ordinals:
        accounted = median + ordinal / 1000 if ordinal in loss_support_ordinals else -0.02
        per_lap.append(
            {
                "accounted_window_delta_s": accounted,
                "approach_delta_s": 0.0,
                "carry_delta_s": 0.0,
                "lap_id": lap_id[ordinal],
                "lap_ordinal": ordinal,
                "local_delta_s": accounted,
                "phase_closed": True,
                "phase_residual_s": 0.0,
            }
        )
    return {
        "action": "Practice a later brake release." if has_action else None,
        "action_counterexample_lap_ids": [lap_id[item] for item in action_counter],
        "action_evidence_lap_ids": [lap_id[item] for item in action_support],
        "confidence": "LOW",
        "corner_id": corner_id,
        "diagnosis": "LONG_COAST" if has_action else None,
        "executable": False,
        "expected_gain_range_s": [0.1, 0.2] if has_action else None,
        "loss_counterexample_lap_ids": [lap_id[item] for item in loss_counter_ordinals],
        "loss_evidence_lap_ids": [lap_id[item] for item in loss_support_ordinals],
        "loss_summary": {
            "comparison_lap_count": 6,
            "median_accounted_window_delta_s": median,
            "supporting_lap_count": len(loss_support_ordinals),
        },
        "per_lap_evidence": per_lap,
        "rank": rank,
        "status": "SHADOW_ONLY",
    }


def _validated_inputs() -> tuple[object, ...]:
    driving_receipt = _receipt("driving-replay.json", "driving_replay_sha256", _sha("1"))
    corner_receipt = _receipt("corner-cards.json", "corner_cards_sha256", _sha("2"))
    fuel_receipt = _receipt("fuel-replay.json", "fuel_replay_sha256", _sha("3"))
    pit_receipt = _receipt("pit-plan.json", "pit_plan_sha256", _sha("4"))
    evidence = {
        "session_id": "fixture-session",
        "source_id": "fixture-source",
        "source_kind": "IBT_OFFLINE",
        "source_sha256": _sha("5"),
    }
    driving_replay = {
        "capabilities": {
            "current_tire_wear": _gate("SKIP", estimate_available=False),
            "race_coaching": _gate("BLOCKED"),
        },
        "driving_context": {"track_length_mm": 6_930_000},
        "event_receipt": {"event_count": 54, "receipt_sha256": _sha("6")},
        "input_evidence": evidence,
        "model_output": {
            "eligible_lap_ordinals": [2, 4, 5, 9, 10, 11, 16],
            "grid_step_m": 1.0,
            "reference": {"lap_ordinal": 11},
        },
        "normalized_input_receipt": {
            "sample_count": 151_892,
            "samples_sha256": _sha("7"),
        },
    }
    driving = {
        "input_provenance_sha256": _sha("8"),
        "replay": driving_replay,
        "replay_sha256": _sha("1"),
    }
    cards = [
        _corner_card(
            corner_id="C08",
            rank=1,
            median=0.2936675200685386,
            loss_support_ordinals=[2, 4, 5, 9, 16],
            loss_counter_ordinals=[10],
        ),
        _corner_card(
            corner_id="C02",
            rank=2,
            median=0.23019387535648406,
            loss_support_ordinals=[2, 4, 5, 9, 10],
            loss_counter_ordinals=[16],
        ),
        _corner_card(
            corner_id="C01",
            rank=3,
            median=0.16317085494576133,
            loss_support_ordinals=[2, 4, 5, 10],
            loss_counter_ordinals=[9, 16],
            action_support_ordinals=[4, 10],
            action_counter_ordinals=[2, 5, 9, 16],
        ),
    ]
    corners = {
        "cards": cards,
        "reference": {"reference_lap_ordinal": 11},
    }
    fuel = {
        "capabilities": {"fuel_model_shadow": _gate("PASS")},
        "model_output": {
            "burn": {
                "accepted_laps": 15,
                "confidence": "high",
                "conservative_l_per_lap": 3.9986486434936523,
                "conservative_quantile": 0.9,
                "mean_l_per_lap": 3.9180479685465497,
                "rejected_laps": 5,
                "source_label": "observed",
            }
        },
    }
    alternatives = []
    for index, (alternative_id, mode, tires, fuel_add) in enumerate(
        (
            ("fuel_to_end:no_tires", "FUEL_TO_END", False, 20.986486),
            ("fuel_to_end:change_tires", "FUEL_TO_END", True, 20.986486),
            ("full_fuel:no_tires", "FULL_FUEL", False, 115.994595),
            ("full_fuel:change_tires", "FULL_FUEL", True, 115.994595),
        ),
        start=1,
    ):
        alternatives.append(
            {
                "alternative_id": alternative_id,
                "change_tires": tires,
                "fuel_add_l": fuel_add,
                "fuel_mode": mode,
                "fuel_service_time_s": 10.0 + index,
                "provenance": "USER_RULE_DEVELOPMENT_SMOKE",
                "stationary_service_time_s": 20.0 + index,
                "total_pit_loss_s": 45.0 + index,
            }
        )
    pit = {
        "alternatives": alternatives,
        "capabilities": {
            "event_rules": _gate("DEVELOPMENT_SMOKE"),
            "fuel_feasibility": _gate("PASS_DEVELOPMENT_SMOKE"),
        },
        "recommendation": {
            "action": {"service_alternative_id": "fuel_to_end:no_tires"}
        },
    }
    return (
        driving_receipt,
        corner_receipt,
        fuel_receipt,
        pit_receipt,
        driving,
        corners,
        fuel,
        pit,
    )


def _artifact() -> dict[str, object]:
    values = _validated_inputs()
    return _MODULE._build_artifact_from_validated(
        *values[:4],
        driving=values[4],
        corners=values[5],
        fuel=values[6],
        pit=values[7],
    )


def _rehash(artifact: dict[str, object]) -> None:
    package = artifact["package_info"]
    assert isinstance(package, dict)
    package.pop("artifact_sha256", None)
    package["artifact_sha256"] = canonical_sha256(artifact)


def test_report_is_answer_first_bounded_and_preserves_counterexamples() -> None:
    artifact = _artifact()
    validate_session_report_artifact(artifact)
    manifest = artifact["manifest"]
    snapshot = artifact["snapshot"]
    assert isinstance(manifest, dict) and isinstance(snapshot, dict)
    blocks = manifest["blocks"]
    datasets = snapshot["datasets"]
    assert isinstance(blocks, list) and isinstance(datasets, dict)
    assert blocks[1]["id"] == "executive_summary"
    assert len(manifest["charts"]) == 1
    chart = manifest["charts"][0]
    assert chart["type"] == "scatter"
    assert chart["dataset"] == "corner_per_lap"
    assert chart["encodings"]["x"]["field"] == "accounted_window_delta_s"
    assert "label" not in chart["encodings"]
    assert chart["referenceLines"] == [
        {
            "axis": "x",
            "value": 0,
            "label": "Equal to reference lap 11",
            "color": "neutral",
            "lineStyle": "dashed",
        }
    ]
    assert len(datasets["corner_losses"]) == 3
    assert len(datasets["corner_per_lap"]) == 18
    assert "pit_alternatives" not in datasets
    assert datasets["fuel_burn"][0]["admitted_laps"] == 15
    assert datasets["fuel_burn"][0]["mean_l_per_lap"] == pytest.approx(3.9180479685)
    assert datasets["fuel_burn"][0]["conservative_l_per_lap"] == pytest.approx(3.9986486435)
    assert datasets["corner_losses"][0]["loss_support_fraction"] == "5/6"
    assert datasets["corner_losses"][2]["loss_counterexample_laps"] == "9, 16"
    assert datasets["corner_losses"][2]["action_evidence_laps"] == "4, 10"
    assert datasets["corner_losses"][2]["action_counterexample_laps"] == "2, 5, 9, 16"
    assert "expected_gain" not in _MODULE._canonical_json(
        {"manifest": manifest, "snapshot": snapshot}
    ).decode()
    visible = _MODULE._canonical_json(
        {
            "blocks": manifest["blocks"],
            "cards": manifest["cards"],
            "charts": manifest["charts"],
            "tables": manifest["tables"],
        }
    ).decode().lower()
    assert all(token not in visible for token in _MODULE._FORBIDDEN_VISIBLE_PIT_ACTION_TOKENS)
    assert "pit_alternatives_table" not in visible
    executive = next(block["body"] for block in blocks if block["id"] == "executive_summary")
    assert all(token in executive for token in ("SHADOW_ONLY", "LOW", "WAIT_CONDITION_DATA", "BLOCKED"))
    scope = next(block["body"] for block in blocks if block["id"] == "scope_definitions")
    assert all(token in scope for token in ("lap 11", "7 个 clean laps", "6 个 comparison laps", "1 m"))
    next_steps = next(block["body"] for block in blocks if block["id"] == "next_steps")
    assert all(token in next_steps for token in ("C01", "A/B", "练习", "release-to-throttle"))


def test_every_native_source_is_executed_sql_with_safe_bound_paths() -> None:
    artifact = _artifact()
    sources = artifact["sources"]
    snapshot = artifact["snapshot"]
    assert isinstance(sources, list) and isinstance(snapshot, dict)
    for source in sources:
        assert source["query"]["engine"] == "sqlite"
        assert source["query"]["sql"].startswith("WITH reviewed(")
        assert _MODULE._execute_projection(source["query"]["sql"]) == snapshot["datasets"][
            _MODULE._SOURCE_DATASET[source["id"]]
        ]
        path = PurePosixPath(source["path"])
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert source["query"]["tables_used"] == ["reviewed"]
        assert "non-resolvable logical source" in source["query"]["description"]
    inputs = artifact["package_info"]["input_artifacts"]
    for role, item in inputs.items():
        assert item["logical_path"] == (
            f"logical-receipts/{role}/{item['serialized_sha256']}.json"
        )


def test_same_validated_inputs_are_byte_identical() -> None:
    first = _MODULE._canonical_json(_artifact(), newline=True)
    second = _MODULE._canonical_json(_artifact(), newline=True)
    assert first == second


def test_content_identity_does_not_depend_on_input_basename() -> None:
    values = list(_validated_inputs())
    for index in range(4):
        receipt = values[index]
        values[index] = LoadedReceipt(
            value=receipt.value,
            file_name=f"renamed-{index}.json",
            serialized_sha256=receipt.serialized_sha256,
        )
    renamed = _MODULE._build_artifact_from_validated(
        *values[:4],
        driving=values[4],
        corners=values[5],
        fuel=values[6],
        pit=values[7],
    )
    assert _MODULE._canonical_json(renamed, newline=True) == _MODULE._canonical_json(
        _artifact(), newline=True
    )


@pytest.mark.parametrize("unsafe_path", ["/tmp/receipt.json", "receipts/../receipt.json"])
def test_validator_rejects_unsafe_source_paths(unsafe_path: str) -> None:
    artifact = copy.deepcopy(_artifact())
    artifact["sources"][0]["path"] = unsafe_path
    artifact["manifest"]["sources"] = artifact["sources"]
    _rehash(artifact)
    with pytest.raises(SessionReportError, match="safe repo-relative"):
        validate_session_report_artifact(artifact)


def test_validator_rejects_sql_snapshot_mismatch() -> None:
    artifact = copy.deepcopy(_artifact())
    artifact["sources"][0]["query"]["sql"] = "SELECT 1 AS row_order"
    artifact["manifest"]["sources"] = artifact["sources"]
    _rehash(artifact)
    with pytest.raises(SessionReportError, match="reviewed projection|does not reproduce"):
        validate_session_report_artifact(artifact)


def test_validator_rejects_lost_blocked_gate() -> None:
    artifact = copy.deepcopy(_artifact())
    rows = artifact["snapshot"]["datasets"]["capability_gates"]
    for row in rows:
        if row["status"] == "BLOCKED":
            row["status"] = "PASS"
    _rehash(artifact)
    with pytest.raises(SessionReportError, match="WAIT/SKIP/BLOCKED"):
        validate_session_report_artifact(artifact)


def test_validator_rejects_any_visible_development_smoke_action() -> None:
    artifact = copy.deepcopy(_artifact())
    artifact["manifest"]["tables"].append(
        {
            "id": "unsafe_smoke",
            "title": "Development option",
            "dataset": "fuel_burn",
            "sourceId": "fuel_burn_sql",
            "columns": [{"field": "no_tires", "label": "No tires"}],
        }
    )
    _rehash(artifact)
    with pytest.raises(SessionReportError, match="development-smoke action details"):
        validate_session_report_artifact(artifact)


def test_validator_rejects_chart_that_hides_per_lap_counterexamples() -> None:
    artifact = copy.deepcopy(_artifact())
    chart = artifact["manifest"]["charts"][0]
    chart["dataset"] = "corner_losses"
    chart["sourceId"] = "corner_losses_sql"
    _rehash(artifact)
    with pytest.raises(SessionReportError, match="all 18 per-lap"):
        validate_session_report_artifact(artifact)


def test_validator_rejects_resolvable_source_claim_for_logical_identity() -> None:
    artifact = copy.deepcopy(_artifact())
    artifact["sources"][0]["label"] = "Local receipt file"
    artifact["manifest"]["sources"] = artifact["sources"]
    _rehash(artifact)
    with pytest.raises(SessionReportError, match="non-resolvable boundary"):
        validate_session_report_artifact(artifact)


def test_strict_reader_checks_serialized_hash_and_duplicate_keys(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(b'{"value":1}\n')
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    loaded = _MODULE._read_receipt(receipt, expected_sha256=digest, name="fixture")
    assert loaded.value == {"value": 1}
    with pytest.raises(SessionReportError, match="serialized SHA-256 mismatch"):
        _MODULE._read_receipt(receipt, expected_sha256=_sha("0"), name="fixture")

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_bytes(b'{"value":1,"value":2}\n')
    duplicate_digest = hashlib.sha256(duplicate.read_bytes()).hexdigest()
    with pytest.raises(SessionReportError, match="duplicate JSON key"):
        _MODULE._read_receipt(duplicate, expected_sha256=duplicate_digest, name="fixture")


def test_create_new_output_refuses_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "artifact.json"
    _MODULE._exclusive_write(output, b"first\n")
    with pytest.raises(SessionReportError, match="exclusively create"):
        _MODULE._exclusive_write(output, b"second\n")
    assert output.read_bytes() == b"first\n"
