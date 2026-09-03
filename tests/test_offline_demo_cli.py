from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import iracing_ai_engineer.offline_demo as offline_demo_module
from iracing_ai_engineer import cli
from iracing_ai_engineer.fuel import FuelScenario
from iracing_ai_engineer.offline_demo import (
    OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION,
    canonical_sha256,
)

_ASSET_ID = "public-audi-r8-evo2-spa"
_SESSION_ID = "public-fixture-2023-12-race"
_SCENARIO_SUMMARY = {
    "current_fuel_l": 20.0,
    "purpose": "DETERMINISTIC_SMOKE_ONLY_NOT_EVENT_TRUTH",
    "refuel_rate_l_per_s": 2.0,
    "remaining_laps": 10,
    "reserve_l": 1.0,
    "tank_capacity_l": 120.0,
}


def _scenario() -> FuelScenario:
    return FuelScenario(
        current_fuel_l=20.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
        reserve_l=1.0,
    )


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, dict[str, object], dict[str, str]]:
    data = tmp_path / "data"
    raw_directory = data / "raw"
    label_directory = data / "labels" / "candidates"
    raw_directory.mkdir(parents=True)
    label_directory.mkdir(parents=True)
    raw_path = raw_directory / "fixture.ibt"
    raw_bytes = b"small isolated offline-demo fixture"
    raw_path.write_bytes(raw_bytes)
    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    component_hashes = {
        "condition_cohort_sha256": "a" * 64,
        "condition_config_sha256": "b" * 64,
        "condition_provenance_sha256": "c" * 64,
        "condition_semantic_sha256": "d" * 64,
        "driving_model_output_sha256": "e" * 64,
        "driving_model_semantic_sha256": "f" * 64,
        "driving_replay_sha256": "0" * 64,
        "fuel_model_output_sha256": "1" * 64,
        "fuel_model_semantic_sha256": "2" * 64,
        "fuel_replay_sha256": "3" * 64,
        "label_artifact_sha256": "4" * 64,
        "label_candidate_payload_sha256": "5" * 64,
        "shadow_analysis_sha256": "6" * 64,
    }
    monkeypatch.setattr(cli, "_PUBLIC_AUDI_SPA_SOURCE_SHA256", source_sha256)
    monkeypatch.setattr(cli, "_PUBLIC_AUDI_SPA_BYTE_SIZE", len(raw_bytes))
    monkeypatch.setattr(
        cli,
        "_PUBLIC_AUDI_SPA_LABEL_ARTIFACT_SHA256",
        component_hashes["label_artifact_sha256"],
    )
    monkeypatch.setattr(
        cli,
        "_PUBLIC_AUDI_SPA_LABEL_CANDIDATE_SHA256",
        component_hashes["label_candidate_payload_sha256"],
    )
    monkeypatch.setattr(cli, "_PUBLIC_AUDI_SPA_COMPONENT_HASHES", component_hashes)
    label_payload = {
        "artifact_sha256": component_hashes["label_artifact_sha256"],
        "candidate_basis": {
            "grid_step_mm": 1000,
            "labeled_lap_ordinal": 11,
            "session_id": _SESSION_ID,
            "source_data_sha256": source_sha256,
            "source_id": _ASSET_ID,
        },
        "candidate_payload_sha256": component_hashes["label_candidate_payload_sha256"],
    }
    label_path = label_directory / "audi-spa-v1.candidate.json"
    label_path.write_text(json.dumps(label_payload), encoding="utf-8")

    manifest = {
        "assets": [
            {
                "asset_id": _ASSET_ID,
                "byte_size": len(raw_bytes),
                "local_path": "data/raw/fixture.ibt",
                "provisional_condition_cohort_receipt": {
                    "condition_cohort_sha256": component_hashes["condition_cohort_sha256"],
                    "condition_config_sha256": component_hashes["condition_config_sha256"],
                    "condition_provenance_sha256": component_hashes["condition_provenance_sha256"],
                    "condition_semantic_sha256": component_hashes["condition_semantic_sha256"],
                    "target_lap_ordinal": 11,
                },
                "provisional_driving_label_candidate": {
                    "artifact_sha256": component_hashes["label_artifact_sha256"],
                    "candidate_payload_sha256": component_hashes["label_candidate_payload_sha256"],
                    "labeled_lap_ordinal": 11,
                },
                "provisional_shadow_receipt": {
                    "receipt": {"analysis_sha256": component_hashes["shadow_analysis_sha256"]}
                },
                "provisional_shared_driving_model_receipt": {
                    "driving_replay_sha256": component_hashes["driving_replay_sha256"],
                    "model_output_sha256": component_hashes["driving_model_output_sha256"],
                    "model_semantic_sha256": component_hashes["driving_model_semantic_sha256"],
                    "model_summary": {"reference_lap_ordinal": 11},
                    "pipeline": {"driving_config": {"grid_step_m": 1.0}},
                    "session_id": _SESSION_ID,
                    "source_id": _ASSET_ID,
                },
                "provisional_shared_fuel_model_receipt": {
                    "fuel_replay_sha256": component_hashes["fuel_replay_sha256"],
                    "model_output_sha256": component_hashes["fuel_model_output_sha256"],
                    "model_semantic_sha256": component_hashes["fuel_model_semantic_sha256"],
                    "scenario": _SCENARIO_SUMMARY,
                    "scenario_sha256": canonical_sha256(_scenario().to_dict()),
                    "session_id": _SESSION_ID,
                    "source_id": _ASSET_ID,
                },
                "sha256": source_sha256,
            }
        ]
    }
    manifest_path = data / "public_sources.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, raw_path, label_payload, component_hashes


def _demo_payload(
    *,
    source_sha256: str,
    byte_size: int,
    component_hashes: dict[str, str],
    condition_status: str = "WAIT_CONDITION_DATA",
    label_status: str = "WAIT_HUMAN_LABELS",
) -> dict[str, object]:
    binding: dict[str, object] = {
        "advisor_only": True,
        "component_hashes": component_hashes,
        "contract_version": OFFLINE_ENGINEER_DEMO_CONTRACT_VERSION,
        "execution_mode": "SHADOW",
        "execution_status": "COMPLETE",
        "gates": {
            "condition_trust": {"reasons": ["NOT_TRUSTED"], "status": condition_status},
            "label_trust": {"reasons": ["NOT_APPROVED"], "status": label_status},
            "offline_demo": {"reasons": [], "status": "PASS"},
        },
        "input_binding": {
            "input_evidence": {
                "byte_size": byte_size,
                "source_sha256": source_sha256,
            }
        },
        "recommendations": {
            "shared_driving": [{"executable": False, "id": "driving:C01"}],
            "shared_fuel": [{"executable": False, "id": "fuel:plan"}],
            "shadow": [],
        },
    }
    return {**binding, "demo_sha256": canonical_sha256(binding)}


@pytest.mark.parametrize(
    ("extra_arguments", "expected_exit"),
    [([], 0), (["--require-trusted"], 5)],
)
def test_public_audi_spa_demo_is_one_command_and_preserves_honest_waits(
    monkeypatch,
    capsys,
    tmp_path,
    extra_arguments,
    expected_exit,
):
    manifest_path, raw_path, label_payload, component_hashes = _fixture(tmp_path, monkeypatch)
    observed: dict[str, object] = {}
    expected = _demo_payload(
        source_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        byte_size=raw_path.stat().st_size,
        component_hashes=component_hashes,
    )

    def fake_build(path, **kwargs):
        observed.update({"path": path, **kwargs})
        return copy.deepcopy(expected)

    monkeypatch.setattr(offline_demo_module, "build_offline_engineer_demo", fake_build)
    output_path = tmp_path / "offline-demo.json"

    exit_code = cli.main(
        [
            "offline-demo",
            "--preset",
            "public-audi-spa",
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
            *extra_arguments,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == expected_exit
    assert captured.err == ""
    assert json.loads(captured.out) == expected
    assert json.loads(output_path.read_text(encoding="utf-8")) == expected
    assert observed["path"] == raw_path
    assert observed["source_id"] == _ASSET_ID
    assert observed["session_id"] == _SESSION_ID
    assert observed["fuel_scenario"] == _scenario()
    assert observed["target_lap_ordinal"] == 11
    assert observed["pending_label_payload"] == label_payload
    assert callable(observed["shadow_builder"])
    assert callable(observed["driving_builder"])
    assert expected["gates"]["offline_demo"]["status"] == "PASS"
    assert expected["gates"]["condition_trust"]["status"] == ("WAIT_CONDITION_DATA")
    assert expected["gates"]["label_trust"]["status"] == "WAIT_HUMAN_LABELS"


def test_require_trusted_can_pass_only_when_all_three_trust_gates_pass(
    monkeypatch,
    capsys,
    tmp_path,
):
    manifest_path, raw_path, _, component_hashes = _fixture(tmp_path, monkeypatch)
    payload = _demo_payload(
        source_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        byte_size=raw_path.stat().st_size,
        component_hashes=component_hashes,
        condition_status="PASS",
        label_status="PASS",
    )
    monkeypatch.setattr(
        offline_demo_module,
        "build_offline_engineer_demo",
        lambda path, **kwargs: copy.deepcopy(payload),
    )

    exit_code = cli.main(
        [
            "offline-demo",
            "--preset",
            "public-audi-spa",
            "--manifest",
            str(manifest_path),
            "--require-trusted",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_offline_demo_never_overwrites_an_existing_output(
    monkeypatch,
    capsys,
    tmp_path,
):
    manifest_path, _, _, _ = _fixture(tmp_path, monkeypatch)
    output_path = tmp_path / "existing.json"
    output_path.write_text("user data\n", encoding="utf-8")
    monkeypatch.setattr(
        offline_demo_module,
        "build_offline_engineer_demo",
        lambda path, **kwargs: pytest.fail("builder must not run"),
    )

    exit_code = cli.main(
        [
            "offline-demo",
            "--preset",
            "public-audi-spa",
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert json.loads(captured.out)["error"] == "OUTPUT_EXISTS"
    assert output_path.read_text(encoding="utf-8") == "user data\n"


def test_offline_demo_rejects_manifest_source_tampering_before_build(
    monkeypatch,
    capsys,
    tmp_path,
):
    manifest_path, raw_path, _, _ = _fixture(tmp_path, monkeypatch)
    raw_path.write_bytes(b"same path, tampered telemetry")
    monkeypatch.setattr(
        offline_demo_module,
        "build_offline_engineer_demo",
        lambda path, **kwargs: pytest.fail("builder must not run"),
    )

    exit_code = cli.main(
        [
            "offline-demo",
            "--preset",
            "public-audi-spa",
            "--manifest",
            str(manifest_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "size and SHA-256" in captured.err
    assert json.loads(captured.out)["error"] == "OFFLINE_DEMO_ERROR"


def test_custom_manifest_cannot_replace_the_package_frozen_trust_root(
    monkeypatch,
    capsys,
    tmp_path,
):
    manifest_path, raw_path, _, _ = _fixture(tmp_path, monkeypatch)
    alternative = b"self-consistent but untrusted replacement"
    raw_path.write_bytes(alternative)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    asset = manifest["assets"][0]
    asset["byte_size"] = len(alternative)
    asset["sha256"] = hashlib.sha256(alternative).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        offline_demo_module,
        "build_offline_engineer_demo",
        lambda path, **kwargs: pytest.fail("builder must not run"),
    )

    exit_code = cli.main(
        [
            "offline-demo",
            "--preset",
            "public-audi-spa",
            "--manifest",
            str(manifest_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "package-frozen public-audi-spa trust root" in captured.err
    assert json.loads(captured.out)["error"] == "OFFLINE_DEMO_ERROR"


def test_offline_demo_rejects_source_change_during_analysis(
    monkeypatch,
    capsys,
    tmp_path,
):
    manifest_path, raw_path, _, component_hashes = _fixture(tmp_path, monkeypatch)
    payload = _demo_payload(
        source_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        byte_size=raw_path.stat().st_size,
        component_hashes=component_hashes,
    )

    def changing_builder(path, **kwargs):
        raw_path.write_bytes(b"telemetry changed while models were running")
        return copy.deepcopy(payload)

    monkeypatch.setattr(
        offline_demo_module,
        "build_offline_engineer_demo",
        changing_builder,
    )
    output_path = tmp_path / "must-not-exist.json"

    exit_code = cli.main(
        [
            "offline-demo",
            "--preset",
            "public-audi-spa",
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "changed while the demo was running" in captured.err
    assert json.loads(captured.out)["error"] == "OFFLINE_DEMO_ERROR"
    assert not output_path.exists()


def test_offline_demo_rejects_executable_recommendations_and_writes_nothing(
    monkeypatch,
    capsys,
    tmp_path,
):
    manifest_path, raw_path, _, component_hashes = _fixture(tmp_path, monkeypatch)
    payload = _demo_payload(
        source_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        byte_size=raw_path.stat().st_size,
        component_hashes=component_hashes,
    )
    payload["recommendations"]["shared_fuel"][0]["executable"] = True
    binding = {key: value for key, value in payload.items() if key != "demo_sha256"}
    payload["demo_sha256"] = canonical_sha256(binding)
    monkeypatch.setattr(
        offline_demo_module,
        "build_offline_engineer_demo",
        lambda path, **kwargs: copy.deepcopy(payload),
    )
    output_path = tmp_path / "must-not-exist.json"

    exit_code = cli.main(
        [
            "offline-demo",
            "--preset",
            "public-audi-spa",
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "is executable" in captured.err
    assert json.loads(captured.out)["error"] == "OFFLINE_DEMO_ERROR"
    assert not output_path.exists()


@pytest.mark.parametrize(
    ("raw_manifest", "message"),
    [
        ('{"assets":[],"assets":[]}', "duplicate JSON"),
        ('{"assets":NaN}', "non-finite JSON"),
    ],
)
def test_offline_demo_manifest_is_strict_json(
    capsys,
    tmp_path,
    raw_manifest,
    message,
):
    data = tmp_path / "data"
    data.mkdir()
    manifest_path = data / "public_sources.json"
    manifest_path.write_text(raw_manifest, encoding="utf-8")

    exit_code = cli.main(
        [
            "offline-demo",
            "--preset",
            "public-audi-spa",
            "--manifest",
            str(manifest_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert message in captured.err
    assert json.loads(captured.out)["error"] == "OFFLINE_DEMO_ERROR"
