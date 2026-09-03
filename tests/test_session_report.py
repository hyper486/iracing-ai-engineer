from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import iracing_ai_engineer.cli as cli
import iracing_ai_engineer.engineer_session as engineer_session
from iracing_ai_engineer.session_report import (
    ENGINEER_SESSION_REPORT_CONTRACT_VERSION,
    EngineerSessionReportError,
    build_engineer_session_report,
    render_engineer_session_report_html,
    validate_engineer_session_report,
    write_engineer_session_report_bundle_exclusive,
)


def _load_engineer_session_fixtures() -> ModuleType:
    path = Path(__file__).with_name("test_engineer_session.py")
    spec = importlib.util.spec_from_file_location("_session_report_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_FIXTURES = _load_engineer_session_fixtures()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


@pytest.fixture(scope="module")
def report_fixture(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    root = tmp_path_factory.mktemp("session-report")
    capture = root / "sdk-live.jsonl"
    frames = _FIXTURES._paired_frames()
    _FIXTURES._write_collector(capture, frames)
    source = engineer_session._build_source_components(
        capture,
        input_kind="collector",
        source_id=None,
        session_id=None,
        scenario=_FIXTURES._scenario(),
        stale_after_s=1.0,
        opponent_error_policy="degrade",
    )
    session = engineer_session.build_engineer_session(
        capture,
        input_kind="collector",
        scenario=_FIXTURES._scenario(),
        strategy_context=_FIXTURES._context(
            source,
            decision_tick=int(frames[-1]["SessionTick"]),
        ),
        stale_after_s=1.0,
    )
    report = build_engineer_session_report(
        session,
        expected_engineer_session_sha256=session["engineer_session_sha256"],
    )
    session_path = root / "engineer-session.json"
    engineer_session.write_engineer_session_exclusive(session_path, session)
    return {
        "report": report,
        "root": root,
        "session": session,
        "session_path": session_path,
    }


def test_report_is_bound_sdk_live_projection_without_smoke_strategy(
    report_fixture: dict[str, object],
) -> None:
    session = report_fixture["session"]
    report = report_fixture["report"]
    assert isinstance(session, dict)
    assert isinstance(report, dict)

    assert report["contract_version"] == ENGINEER_SESSION_REPORT_CONTRACT_VERSION
    assert report["advisor_only"] is True
    assert report["status"] == "WAIT_DATA"
    assert report["answer_first"]["category"] == "WAIT_DATA"
    assert report["engineer_session_binding"] == {
        "engineer_session_contract_version": "engineer-session-v1",
        "engineer_session_sha256": session["engineer_session_sha256"],
        "input_evidence_sha256": session["input_lineage"]["input_evidence_sha256"],
        "input_kind": "collector",
        "sample_count": session["input_lineage"]["sample_count"],
        "session_id": session["input_lineage"]["session_id"],
        "source_content_sha256": session["input_lineage"]["source_content_sha256"],
        "source_id": session["input_lineage"]["source_id"],
        "source_kind": "SDK_LIVE",
    }

    assert session["components"]["fuel_replay"]["recommendations"]
    assert session["components"]["m2_strategy"]["recommendations"] == []
    assert report["sections"]["strategy"]["recommendations"] == []
    assert report["sections"]["strategy"]["authoritative_component"] == "m2_strategy"
    assert report["sections"]["fuel"]["strategy_numbers_exposed"] is False
    assert "recommendations" not in report["sections"]["fuel"]
    assert "model_output" not in report["sections"]["fuel"]

    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert "current_fuel_l" not in encoded
    assert "conservative_fuel_to_end_l" not in encoded
    assert "minimum_stop" not in encoded
    assert report["safety"] == {
        "advisor_only": True,
        "development_smoke_fuel_values_exposed": False,
        "html_self_contained": True,
        "network_accessed": False,
        "pit_black_box_control_enabled": False,
        "recommendations_executable": False,
        "script_execution_enabled": False,
        "source_recommendation_policy": "M2_STRATEGY_ONLY",
        "telemetry_read_only": True,
        "vehicle_control_enabled": False,
    }
    assert report["blockers"]


def test_report_replays_exactly_and_rejects_total_rehash(
    report_fixture: dict[str, object],
) -> None:
    session = report_fixture["session"]
    report = report_fixture["report"]
    assert isinstance(session, dict)
    assert isinstance(report, dict)
    assert validate_engineer_session_report(
        report,
        session,
        expected_report_sha256=report["report_sha256"],
        expected_engineer_session_sha256=session["engineer_session_sha256"],
    ) == report

    attack = copy.deepcopy(report)
    attack["answer_first"]["headline"] = "Pit now; forged report"
    attack["report_sha256"] = _canonical_sha256(
        {key: value for key, value in attack.items() if key != "report_sha256"}
    )
    with pytest.raises(EngineerSessionReportError) as replay_error:
        validate_engineer_session_report(attack, session)
    assert replay_error.value.code == "REPORT_REPLAY_MISMATCH"

    with pytest.raises(EngineerSessionReportError) as independent_error:
        validate_engineer_session_report(
            report,
            session,
            expected_report_sha256="0" * 64,
        )
    assert independent_error.value.code == "REPORT_SHA256_MISMATCH"


def test_report_html_is_deterministic_self_contained_and_script_free(
    report_fixture: dict[str, object],
) -> None:
    session = report_fixture["session"]
    report = report_fixture["report"]
    assert isinstance(session, dict)
    assert isinstance(report, dict)
    first = render_engineer_session_report_html(
        report,
        session,
        expected_report_sha256=report["report_sha256"],
        expected_engineer_session_sha256=session["engineer_session_sha256"],
    )
    second = render_engineer_session_report_html(
        report,
        session,
        expected_report_sha256=report["report_sha256"],
        expected_engineer_session_sha256=session["engineer_session_sha256"],
    )
    assert first == second
    assert first.startswith(b"<!doctype html>\n")
    assert b'data-report-sha256="' + report["report_sha256"].encode() + b'"' in first
    assert b"SDK_LIVE" in first
    assert b"Only authoritative M2 recommendations are shown" in first
    assert b"cannot steer" in first
    lowered = first.lower()
    assert b"<script" not in lowered
    assert b"javascript:" not in lowered
    assert b"http://" not in lowered
    assert b"https://" not in lowered
    assert b"default-src 'none'" in lowered


def test_report_bundle_is_create_new_and_read_back_verified(
    report_fixture: dict[str, object], tmp_path: Path
) -> None:
    session = report_fixture["session"]
    report = report_fixture["report"]
    assert isinstance(session, dict)
    assert isinstance(report, dict)
    artifact = tmp_path / "report.json"
    rendered = tmp_path / "report.html"
    write_engineer_session_report_bundle_exclusive(
        artifact,
        rendered,
        report,
        session,
        expected_report_sha256=report["report_sha256"],
        expected_engineer_session_sha256=session["engineer_session_sha256"],
    )
    assert json.loads(artifact.read_text(encoding="utf-8")) == report
    assert rendered.read_bytes() == render_engineer_session_report_html(report, session)
    for path in (artifact, rendered):
        metadata = path.stat()
        assert stat.S_ISREG(metadata.st_mode)
        if os.name == "nt":
            assert not int(getattr(metadata, "st_file_attributes", 0)) & 0x400
        else:
            assert stat.S_IMODE(metadata.st_mode) == 0o600

    before = (artifact.read_bytes(), rendered.read_bytes())
    with pytest.raises(EngineerSessionReportError) as exists:
        write_engineer_session_report_bundle_exclusive(
            artifact,
            rendered,
            report,
            session,
        )
    assert exists.value.code == "OUTPUT_CREATE_FAILED"
    assert (artifact.read_bytes(), rendered.read_bytes()) == before

    same = tmp_path / "same-output"
    with pytest.raises(EngineerSessionReportError) as identical:
        write_engineer_session_report_bundle_exclusive(
            same,
            same,
            report,
            session,
        )
    assert identical.value.code == "OUTPUT_PATH_INVALID"
    assert not same.exists()


def test_session_report_cli_writes_both_outputs_and_can_require_advice(
    report_fixture: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = report_fixture["session"]
    session_path = report_fixture["session_path"]
    assert isinstance(session, dict)
    assert isinstance(session_path, Path)
    artifact = tmp_path / "cli-report.json"
    rendered = tmp_path / "cli-report.html"
    common = [
        "session-report",
        str(session_path),
        "--expected-engineer-session-sha256",
        session["engineer_session_sha256"],
        "--artifact-output",
        str(artifact),
        "--html-output",
        str(rendered),
    ]
    assert cli.main(common) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "WAIT_DATA"
    assert output["report_sha256"] == report_fixture["report"]["report_sha256"]
    assert artifact.is_file() and rendered.is_file()

    required_artifact = tmp_path / "required-report.json"
    required_html = tmp_path / "required-report.html"
    required = [
        *common[:5],
        str(required_artifact),
        "--html-output",
        str(required_html),
        "--require-advice",
    ]
    assert cli.main(required) == 5
    capsys.readouterr()
    assert required_artifact.is_file() and required_html.is_file()

    wrong_artifact = tmp_path / "wrong-report.json"
    wrong_html = tmp_path / "wrong-report.html"
    wrong = [
        "session-report",
        str(session_path),
        "--expected-engineer-session-sha256",
        "0" * 64,
        "--artifact-output",
        str(wrong_artifact),
        "--html-output",
        str(wrong_html),
    ]
    assert cli.main(wrong) == 3
    error_output = capsys.readouterr()
    assert "independent digest binding" in error_output.err
    assert json.loads(error_output.out)["error"] == "SESSION_REPORT_ERROR"
    assert not wrong_artifact.exists() and not wrong_html.exists()


def test_report_is_hash_seed_deterministic(report_fixture: dict[str, object]) -> None:
    session = report_fixture["session"]
    session_path = report_fixture["session_path"]
    assert isinstance(session, dict)
    assert isinstance(session_path, Path)
    program = """
import hashlib
import json
import sys
from pathlib import Path
from iracing_ai_engineer.session_report import (
    build_engineer_session_report,
    render_engineer_session_report_html,
)
session = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
report = build_engineer_session_report(
    session,
    expected_engineer_session_sha256=sys.argv[2],
)
rendered = render_engineer_session_report_html(
    report,
    session,
    expected_report_sha256=report['report_sha256'],
    expected_engineer_session_sha256=sys.argv[2],
)
print(json.dumps({
    'html_sha256': hashlib.sha256(rendered).hexdigest(),
    'report_sha256': report['report_sha256'],
}, sort_keys=True))
"""
    observed: list[str] = []
    for seed in ("1", "987654"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                program,
                str(session_path),
                session["engineer_session_sha256"],
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
        )
        observed.append(result.stdout.strip())
    assert observed[0] == observed[1]


@pytest.mark.skipif(
    not Path("data/raw/audir8lmsevo2gt3_spa up.ibt").is_file(),
    reason="REQUIRES_DATA: public Audi/Spa IBT absent",
)
def test_real_audi_session_projects_practice_action_without_strategy_promotion() -> None:
    m2_frozen = json.loads(
        Path("data/derived/audi-spa-offline-m2-strategy-v1.json").read_text(
            encoding="utf-8"
        )
    )
    session = engineer_session.build_engineer_session(
        Path("data/raw/audir8lmsevo2gt3_spa up.ibt"),
        input_kind="ibt",
        source_id="public-audi-r8-evo2-spa",
        session_id="public-fixture-2023-12-race",
        scenario=_FIXTURES._scenario(),
        strategy_context=m2_frozen["strategy_context"],
        stale_after_s=0.5,
    )
    report = build_engineer_session_report(session)
    assert report["status"] == "PRACTICE_AVAILABLE"
    assert report["answer_first"]["headline"] == "Practice priority: C01."
    assert report["sections"]["strategy"]["recommendations"] == []
    assert all(
        card["executable"] is False and card["practice_only"] is True
        for card in report["sections"]["driving"]["cards"]
    )
