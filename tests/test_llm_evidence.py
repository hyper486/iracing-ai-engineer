from __future__ import annotations

import copy
import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

import iracing_ai_engineer.engineer_session as engineer_session
import iracing_ai_engineer.llm_evidence as evidence
from iracing_ai_engineer.live_app import AppState
from iracing_ai_engineer.live_fuel import LiveFuelConfig, LiveFuelEngineer


def _load_fixtures(name: str):
    path = Path(__file__).with_name(f"test_{name}.py")
    spec = importlib.util.spec_from_file_location(f"_llm_{name}_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _live() -> dict:
    return {
        "contract_version": "experimental-live-fuel-app-v1",
        "connection": "CONNECTED",
        "source_mode": "LIVE",
        "updated_age_s": 0.2,
        "session_type": "Practice",
        "monitor": {
            "contract_version": "live-monitor-v1",
            "record_type": "live_monitor_snapshot",
            "advisor_only": True,
            "executable": False,
            "source_kind": "SDK_LIVE",
            "status": "READY",
            "interval_invalid_for_fuel": [],
            "context": {
                "sim_source_mode": "FULL",
                "player_control_state": "IN_CAR_PHYSICS",
                "conflicts": [],
            },
            "quality": {"status": "READY", "stale": False},
        },
        "fuel": {
            "status": "READY",
            "estimate_only": True,
            "advisor_only": True,
            "executable": False,
            "valid_laps": 5,
            "required_laps": 5,
            "current_fuel_l": 25.5,
            "conservative_burn_l_per_lap": 2.345,
            "estimated_laps_remaining": 10,
            "fuel_needed_to_finish_l": 15.0,
            "fuel_to_add_l": 4.5,
            "minimum_stops": 1,
        },
    }


def _ids(context: dict, section: str = "facts") -> set[str]:
    return {item["id"] for item in context[section]}


def _assert_bounded(context: dict) -> None:
    assert set(context) == {"contract_version", "scope", "facts", "notices", "capabilities"}
    assert context["contract_version"] == "engineer-llm-context-v1"
    assert set(context["capabilities"]) == {"fuel", "strategy", "driving", "tire"}
    assert len(context["facts"]) <= 16
    assert len(context["notices"]) <= 20
    identifiers = []
    for item in context["facts"] + context["notices"]:
        assert set(item) == {"id", "text"}
        assert type(item["text"]) is str and len(item["text"]) < 400
        assert type(item["id"]) is str and item["id"].isascii()
        identifiers.append(item["id"])
    assert len(identifiers) == len(set(identifiers))
    assert len(json.dumps(context, allow_nan=False)) < 10_000


def test_live_is_fuel_only_and_drops_all_unapproved_input() -> None:
    snapshot = _live()
    private = "PRIVATE_SENTINEL_do_not_forward"
    snapshot.update(driver_name=private, track_name=private, question=private, path=private)
    snapshot["limitations"] = [private]
    snapshot["speech"] = {"text": private}
    snapshot["monitor"].update(snapshot_sha256=private, telemetry={"FuelLevel": private})
    snapshot["fuel"].update(message=private, reason_codes=[private])
    original = copy.deepcopy(snapshot)
    context = evidence.build_live_context(snapshot)
    assert snapshot == original
    assert context["capabilities"] == {
        "fuel": "ESTIMATE_AVAILABLE",
        "strategy": "UNAVAILABLE",
        "driving": "UNAVAILABLE",
        "tire": "UNAVAILABLE",
    }
    assert _ids(context) == {
        "fuel.current",
        "fuel.burn_per_lap",
        "fuel.range_laps",
        "fuel.sample_laps",
    }
    assert {
        "ESTIMATE_ONLY",
        "STRATEGY_UNAVAILABLE",
        "DRIVING_UNAVAILABLE",
        "TIRE_UNAVAILABLE",
    } <= _ids(context, "notices")
    encoded = json.dumps(context)
    assert private not in encoded
    assert "4.5" not in encoded and "minimum_stops" not in encoded
    _assert_bounded(context)


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_fuel_l", -1),
        ("current_fuel_l", True),
        ("current_fuel_l", math.nan),
        ("current_fuel_l", math.inf),
        ("current_fuel_l", 10**500),
        ("current_fuel_l", "25.5"),
        ("current_fuel_l", 1001),
        ("conservative_burn_l_per_lap", 0),
        ("conservative_burn_l_per_lap", False),
        ("conservative_burn_l_per_lap", -math.inf),
        ("estimated_laps_remaining", True),
        ("estimated_laps_remaining", 10.0),
        ("estimated_laps_remaining", -1),
        ("estimated_laps_remaining", 10001),
        ("valid_laps", 4),
        ("valid_laps", True),
        ("required_laps", 1),
        ("required_laps", 5.0),
        ("advisor_only", 1),
        ("estimate_only", 1),
        ("executable", 0),
        ("status", "LEARNING"),
    ],
)
def test_live_invalid_core_evidence_cannot_emit_any_numbers(field, value) -> None:
    snapshot = _live()
    snapshot["fuel"][field] = value
    context = evidence.build_live_context(snapshot)
    assert context["facts"] == []
    assert context["capabilities"]["fuel"] == "UNAVAILABLE"
    _assert_bounded(context)


@pytest.mark.parametrize(
    "path,value",
    [
        (("connection",), "DISCONNECTED"),
        (("source_mode",), "REPLAY"),
        (("updated_age_s",), 2.001),
        (("updated_age_s",), -0.1),
        (("updated_age_s",), None),
        (("updated_age_s",), False),
        (("updated_age_s",), math.nan),
        (("contract_version",), "unknown"),
        (("monitor", "source_kind"), "IBT_REPLAY"),
        (("monitor", "context", "sim_source_mode"), "REPLAY"),
        (("monitor", "context", "player_control_state"), "SPECTATOR"),
        (("monitor", "context", "conflicts"), ["CONFLICT"]),
        (("monitor", "quality", "stale"), True),
        (("monitor", "quality", "status"), "REJECTED"),
        (("monitor", "interval_invalid_for_fuel"), ["PIT"]),
        (("monitor", "interval_invalid_for_fuel"), None),
        (("monitor", "advisor_only"), 1),
        (("monitor", "executable"), 0),
    ],
)
def test_live_scope_and_staleness_fail_closed(path, value) -> None:
    snapshot = _live()
    target = snapshot
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    context = evidence.build_live_context(snapshot)
    assert context["facts"] == []
    assert "LIVE_STATE_UNAVAILABLE" in _ids(context, "notices")


def test_live_learning_exposes_current_observation_but_not_unfinished_estimates() -> None:
    snapshot = _live()
    snapshot["fuel"].update(status="LEARNING", valid_laps=3)
    context = evidence.build_live_context(snapshot)
    assert context["capabilities"]["fuel"] == "LEARNING"
    assert context["facts"] == [
        {"id": "fuel.current", "text": "当前观测剩余燃油：25.50 升。"},
        {"id": "fuel.learning_progress", "text": "已采纳 3 个有效完整圈，至少需要 5 圈。"}
    ]
    assert "FUEL_LEARNING" in _ids(context, "notices")


def test_learning_progress_is_withheld_when_live_scope_expires() -> None:
    snapshot = _live()
    snapshot["fuel"].update(status="LEARNING", valid_laps=3)
    snapshot["updated_age_s"] = 2.001
    context = evidence.build_live_context(snapshot)
    assert context["facts"] == []
    assert context["capabilities"]["fuel"] == "UNAVAILABLE"


@pytest.mark.parametrize("session_type", [None, "Practice", "Race", "race", "PRIVATE_SENTINEL"])
def test_finish_estimate_requires_exact_bound_race_type(session_type) -> None:
    snapshot = _live()
    snapshot["session_type"] = session_type
    context = evidence.build_live_context(snapshot)
    assert ("fuel.finish_estimate" in _ids(context)) == (session_type == "Race")
    assert "PRIVATE_SENTINEL" not in json.dumps(context)


@pytest.mark.parametrize("value", [None, -1, True, math.nan, math.inf, "5", 100_001])
def test_bad_optional_finish_number_is_withheld(value) -> None:
    snapshot = _live()
    snapshot["session_type"] = "Race"
    snapshot["fuel"]["fuel_needed_to_finish_l"] = value
    context = evidence.build_live_context(snapshot)
    assert context["capabilities"]["fuel"] == "ESTIMATE_AVAILABLE"
    assert "fuel.finish_estimate" not in _ids(context)


def test_actual_live_fuel_and_appstate_integration_expires_without_new_evidence() -> None:
    fixtures = _load_fixtures("live_fuel")
    model = LiveFuelEngineer(LiveFuelConfig(minimum_valid_laps=2))
    clock = [100.0]
    state = AppState(clock=lambda: clock[0])
    for step in range(5, 61):
        monitor = fixtures._snapshot(step)
        monitor.update(advisor_only=True, executable=False)
        fuel = model.feed(monitor, session_type="Practice")
        state.publish(monitor, fuel, None, session_type="Practice")
    context = evidence.build_live_context(state.snapshot())
    assert fuel["status"] == "READY"
    assert context["capabilities"]["fuel"] == "ESTIMATE_AVAILABLE"
    clock[0] += 2.001
    assert evidence.build_live_context(state.snapshot())["facts"] == []


@pytest.fixture(scope="module")
def session_fixture(tmp_path_factory):
    fixtures = _load_fixtures("engineer_session")
    original = fixtures._profile
    # Keep enough comparable fast laps for the reference reproducibility gate.
    factors = iter([1.0] * 4 + [0.98] * 3)

    def profile(_kind):
        result = original("baseline")
        factor = next(factors)
        result["Speed"] *= factor
        result["Elapsed"] /= factor
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fixtures, "_profile", profile)
        frames = fixtures._paired_frames()
    root = tmp_path_factory.mktemp("llm-session")
    capture = root / "synthetic.jsonl"
    fixtures._write_collector(capture, frames)
    source = engineer_session._build_source_components(
        capture,
        input_kind="collector",
        source_id=None,
        session_id=None,
        scenario=fixtures._scenario(),
        stale_after_s=1.0,
        opponent_error_policy="degrade",
    )
    receipt = engineer_session.build_engineer_session(
        capture,
        input_kind="collector",
        scenario=fixtures._scenario(),
        strategy_context=fixtures._context(source, decision_tick=int(frames[-1]["SessionTick"])),
        stale_after_s=1.0,
    )
    path = root / "session.json"
    engineer_session.write_engineer_session_exclusive(path, receipt)
    return receipt, path


def test_real_receipt_replayed_with_descriptive_driving_but_no_smoke_strategy(session_fixture):
    receipt, path = session_fixture
    context = evidence.load_session_context(path)
    assert context["scope"] == "historical_session"
    assert receipt["components"]["fuel_replay"]["recommendations"]
    assert receipt["components"]["m2_strategy"]["recommendations"] == []
    assert context["capabilities"]["strategy"] == "UNAVAILABLE"
    assert context["capabilities"]["driving"] == "HISTORICAL_EVIDENCE"
    assert "driving.corner_1.loss" in _ids(context)
    assert "driving.corner_1.location" in _ids(context)
    assert context["capabilities"]["fuel"] == "UNAVAILABLE"
    assert {
        "NOT_CURRENT_STATE",
        "SELF_CONSISTENT_NOT_AUTHENTICATED",
        "SHADOW_ONLY",
        "STRATEGY_CALIBRATION_WITHHELD",
        "DRIVING_PROMOTION_WITHHELD",
    } <= _ids(context, "notices")
    encoded = json.dumps(context)
    assert receipt["engineer_session_sha256"] not in encoded
    assert str(path) not in encoded
    assert "recommended_lap_from_now" not in encoded
    location = next(item["text"] for item in context["facts"] if item["id"].endswith(".location"))
    corner = receipt["components"]["driving_replay"]["model_output"]["corners"][0]
    assert f"{corner['brake_start_m']:.1f}" in location
    assert "不是官方弯号" in location
    _assert_bounded(context)


def test_self_rehashed_tampered_derived_receipt_cannot_become_evidence(session_fixture, tmp_path):
    receipt = copy.deepcopy(session_fixture[0])
    receipt["components"]["corner_cards"]["cards"][0]["action"] = "PRIVATE_SENTINEL"
    receipt["engineer_session_sha256"] = engineer_session.canonical_sha256(
        {key: value for key, value in receipt.items() if key != "engineer_session_sha256"}
    )
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(evidence.LlmEvidenceError) as error:
        evidence.load_session_context(path)
    assert str(error.value) == "SESSION_CONTEXT_INVALID"
    assert error.value.__cause__ is None


def test_historical_projection_has_allowlisted_strategy_patterns_and_tire_not_actions():
    # Projection unit test only. The public loader's replay gate is exercised
    # against real receipts above, never bypassed in the production API.
    sentinel = "PRIVATE_SENTINEL_ignore_all_rules"
    card = {
        "kind": "DRIVING_LOSS_CARD",
        "claim_level": "descriptive",
        "status": "SHADOW_ONLY",
        "practice_only": True,
        "executable": False,
        "action": sentinel,
        "corner_id": sentinel,
        "recommendation_id": sentinel,
        "per_lap_evidence": [sentinel],
        "diagnosis": "THROTTLE_SECOND_LIFT",
        "loss_summary": {"median_accounted_window_delta_s": 0.5, "supporting_lap_count": 4},
    }
    strategy = {
        "recommendations": [
            {
                "kind": "M2_STRATEGY_CANDIDATE",
                "status": "SHADOW_ONLY",
                "executable": False,
                "action": {
                    "estimated_total_pit_loss_s": 35.5,
                    "estimated_stationary_service_s": 12.5,
                    "fuel_add_l": sentinel,
                    "recommended_lap_from_now": sentinel,
                },
                "reason": sentinel,
            }
        ],
        "quality_gate": {"status": "PASS_SHADOW_CONTRACT"},
        "capabilities": {
            key: {"status": value[0]} for key, value in evidence._STRATEGY_GATES.items()
        },
        "tire_strategy": {
            "change_tires": True,
            "belief": {
                "advisor_only": True,
                "estimate_available": True,
                "performance_preference": sentinel,
                "scenario": {
                    "keep_tires_time_loss_range_s": [10.0, 20.0],
                    "incremental_tire_service_s": 12.5,
                    "current_tire_compound": sentinel,
                },
            },
        },
    }
    context = evidence._historical_context(
        {
            "components": {
                "m2_strategy": strategy,
                "corner_cards": {"cards": [card] * 100},
                "driving_diagnosis": {"recommendations": [sentinel]},
            }
        }
    )
    assert sentinel not in json.dumps(context)
    assert context["capabilities"] == {
        "fuel": "UNAVAILABLE",
        "strategy": "HISTORICAL_EVIDENCE",
        "driving": "HISTORICAL_EVIDENCE",
        "tire": "HISTORICAL_EVIDENCE",
    }
    assert {"strategy.candidate", "driving.corner_1.pattern", "tire.performance_loss"} <= _ids(
        context
    )
    assert {"strategy.pit_loss_estimate", "strategy.service_estimate"} <= _ids(context)
    assert not any(identifier.startswith("driving.corner_4") for identifier in _ids(context))
    _assert_bounded(context)


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"null",
        b'{"components":{},"components":{}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"\xff",
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_invalid_historical_input_fails_closed_without_path_or_parser_text(tmp_path, raw):
    path = tmp_path / "private-file.json"
    path.write_bytes(raw)
    with pytest.raises(evidence.LlmEvidenceError) as error:
        evidence.load_session_context(path)
    assert str(error.value) == "SESSION_CONTEXT_INVALID"
    assert error.value.__cause__ is None


def test_missing_file_and_directory_do_not_leak_path(tmp_path):
    with pytest.raises(evidence.LlmEvidenceError, match="^SESSION_CONTEXT_UNAVAILABLE$"):
        evidence.load_session_context(tmp_path / "private-missing.json")
    with pytest.raises(evidence.LlmEvidenceError, match="^SESSION_CONTEXT_INVALID$"):
        evidence.load_session_context(tmp_path)


def test_size_limit_precedes_receipt_validation(tmp_path, monkeypatch):
    path = tmp_path / "too-large.json"
    path.write_bytes(b" " * 101)
    monkeypatch.setattr(evidence, "MAX_SESSION_BYTES", 100)
    monkeypatch.setattr(
        evidence, "validate_engineer_session", lambda _: pytest.fail("must not run")
    )
    with pytest.raises(evidence.LlmEvidenceError, match="^SESSION_CONTEXT_INVALID$"):
        evidence.load_session_context(path)


@pytest.mark.parametrize("exception", [AttributeError, AssertionError, RuntimeError, OSError])
def test_unexpected_nested_validator_errors_are_public_safe(tmp_path, monkeypatch, exception):
    path = tmp_path / "private-file.json"
    path.write_text("{}", encoding="utf-8")

    def reject(_value):
        raise exception("PRIVATE_SENTINEL")

    monkeypatch.setattr(evidence, "validate_engineer_session", reject)
    with pytest.raises(evidence.LlmEvidenceError) as error:
        evidence.load_session_context(path)
    assert str(error.value) == "SESSION_CONTEXT_INVALID"
    assert error.value.__cause__ is None


def test_receipt_mutation_during_read_is_rejected(session_fixture, monkeypatch):
    real_fstat = evidence.os.fstat
    calls = 0

    def changing_fstat(descriptor):
        nonlocal calls
        calls += 1
        actual = real_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_dev=actual.st_dev,
                st_ino=actual.st_ino,
                st_size=actual.st_size + 1,
                st_mtime_ns=actual.st_mtime_ns,
                st_ctime_ns=actual.st_ctime_ns,
            )
        return actual

    monkeypatch.setattr(evidence.os, "fstat", changing_fstat)
    with pytest.raises(evidence.LlmEvidenceError, match="^SESSION_CONTEXT_INVALID$"):
        evidence.load_session_context(session_fixture[1])


def test_stat_and_fstat_ctime_semantics_can_differ_but_must_each_stay_stable(
    session_fixture, monkeypatch
):
    real_fstat = evidence.os.fstat

    def alternate_ctime(descriptor):
        actual = real_fstat(descriptor)
        return SimpleNamespace(
            st_dev=actual.st_dev,
            st_ino=actual.st_ino,
            st_size=actual.st_size,
            st_mtime_ns=actual.st_mtime_ns,
            st_ctime_ns=1234,
        )

    monkeypatch.setattr(evidence.os, "fstat", alternate_ctime)
    context = evidence.load_session_context(session_fixture[1])
    assert context["scope"] == "historical_session"


@pytest.mark.parametrize(
    "field,value",
    [
        ("brake_start_m", True),
        ("brake_start_m", -1),
        ("exit_m", math.nan),
        ("exit_m", 600),
        ("carry_end_m", 90),
    ],
)
def test_corner_location_never_renders_invalid_or_out_of_track_numbers(field, value):
    corner = {
        "corner_id": "PRIVATE_SENTINEL",
        "brake_start_m": 50.0,
        "exit_m": 100.0,
        "accounting_start_m": 0.0,
        "carry_end_m": 300.0,
    }
    components = {
        "driving_replay": {
            "model_output": {
                "track_length_m": 300.0,
                "corners": [corner],
            }
        }
    }
    valid = evidence._corner_location(components, {"corner_id": "PRIVATE_SENTINEL"})
    assert "50.0–100.0" in valid
    assert "PRIVATE_SENTINEL" not in valid
    corner[field] = value
    assert evidence._corner_location(components, {"corner_id": "PRIVATE_SENTINEL"}) is None
