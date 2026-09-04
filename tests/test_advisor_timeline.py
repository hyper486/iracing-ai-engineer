from __future__ import annotations

import copy
import hashlib
import json
import runpy
from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import cache
from pathlib import Path
from typing import Any

import pytest

import iracing_ai_engineer.advisor_timeline as advisor_module
from iracing_ai_engineer.adapters import (
    ValidatedCollectorRun,
    ValidatedIbtRun,
    open_ibt_telemetry,
)
from iracing_ai_engineer.advisor_timeline import (
    AdvisorTimelineError,
    build_advisor_timeline,
    canonical_sha256,
    validate_advisor_timeline,
)
from iracing_ai_engineer.driving import DrivingAnalysisConfig
from iracing_ai_engineer.driving_diagnosis import build_diagnosis_evidence
from iracing_ai_engineer.driving_model_replay import build_driving_model_replay
from iracing_ai_engineer.speech_policy import SpeechPolicyConfig

M2_PATH = Path("data/derived/audi-spa-offline-m2-strategy-v1.json")
M3_PATH = Path("data/derived/audi-spa-driving-diagnosis-evidence-v1.json")
DRIVING_PATH = Path("data/derived/audi-spa-driving-replay-v1.json")
IBT_PATH = Path("data/raw/audir8lmsevo2gt3_spa up.ibt")

_SHARED = runpy.run_path("tests/test_shared_adapter_e2e.py")


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _serialized(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _rehash_m2(value: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(value)
    result["m2_strategy_receipt_sha256"] = canonical_sha256(
        {key: item for key, item in result.items() if key != "m2_strategy_receipt_sha256"}
    )
    return result


def _rehash_diagnosis(value: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(value)
    result["diagnosis_evidence_sha256"] = canonical_sha256(
        {key: item for key, item in result.items() if key != "diagnosis_evidence_sha256"}
    )
    return result


def _nested_dict_paths(value: object, prefix: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    result: list[tuple[object, ...]] = []
    if type(value) is dict:
        result.append(prefix)
        for key, item in value.items():
            result.extend(_nested_dict_paths(item, (*prefix, key)))
    elif type(value) is list:
        for index, item in enumerate(value):
            result.extend(_nested_dict_paths(item, (*prefix, index)))
    return result


def _at_path(value: object, path: tuple[object, ...]) -> object:
    current = value
    for part in path:
        current = current[part]
    return current


def _wait_m2(
    source_binding: dict[str, object],
    *,
    tick: int,
    revision: int = 1,
    previous_sha256: str | None = None,
    source_epoch: int = 1,
    session_epoch: int = 1,
) -> dict[str, object]:
    result = copy.deepcopy(_wait_template())
    context = result["strategy_context"]
    lifecycle = result["lifecycle"]
    input_binding = result["input_binding"]
    assert isinstance(context, dict)
    assert isinstance(lifecycle, dict)
    assert isinstance(input_binding, dict)
    observation = context["observation"]
    assert isinstance(observation, dict)
    observation.update(
        {
            "decision_tick": tick,
            "session_epoch": session_epoch,
            "source_epoch": source_epoch,
        }
    )
    context["source_binding"] = copy.deepcopy(source_binding)
    context["context_sha256"] = canonical_sha256(
        {key: item for key, item in context.items() if key != "context_sha256"}
    )
    for key, item in source_binding.items():
        input_binding[key] = item
    input_binding["context_sha256"] = context["context_sha256"]
    input_binding["input_lineage_sha256"] = canonical_sha256(
        {key: item for key, item in input_binding.items() if key != "input_lineage_sha256"}
    )
    lifecycle.update(
        {
            "active_recommendation_id": None,
            "events": [
                {
                    "event": "NO_CHANGE",
                    "reason_codes": ["NO_ACTIVE_RECOMMENDATION"],
                    "recommendation_id": None,
                }
            ],
            "observation_point": {
                "decision_tick": tick,
                "session_epoch": session_epoch,
                "source_epoch": source_epoch,
            },
            "previous_state_sha256": previous_sha256,
            "state_revision": revision,
        }
    )
    result["recommendations"] = []
    return _rehash_m2(result)


@cache
def _wait_template() -> dict[str, object]:
    helpers = runpy.run_path("tests/test_offline_m2_strategy_receipt.py")
    fuel, m1 = helpers["upstream"].__wrapped__()
    context = helpers["_context"](fuel, m1, traffic=False)
    return helpers["_build"](fuel, m1, context, rules=None)


@cache
def _candidate_template() -> dict[str, object]:
    helpers = runpy.run_path("tests/test_offline_m2_strategy_receipt.py")
    fuel, m1 = helpers["upstream"].__wrapped__()
    context = helpers["_context"](fuel, m1)
    rules = helpers["_rules"](context["event_identity"])
    return helpers["_build"](fuel, m1, context, rules=rules)


@cache
def _candidate_v2_template() -> dict[str, object]:
    helpers = runpy.run_path("tests/test_offline_m2_strategy_receipt.py")
    fuel, m1 = helpers["upstream"].__wrapped__()
    identity = helpers["_identity"]()
    model = helpers["_tire_performance_model"](identity)
    context = helpers["_context_v2"](fuel, m1, tire_model=model)
    rules = helpers["_rules"](context["event_identity"])
    return helpers["_build"](fuel, m1, context, rules=rules)


def _refresh_candidate_traffic(
    result: dict[str, object],
    *,
    tick: int,
) -> str:
    context = result["strategy_context"]
    recommendation = result["recommendations"][0]
    calibration_output = result["calibration"]
    traffic_output = result["traffic_rejoin"]
    assert isinstance(context, dict)
    assert isinstance(recommendation, dict)
    assert isinstance(calibration_output, dict)
    assert isinstance(traffic_output, dict)
    traffic = context["traffic_rejoin"]
    action = recommendation["action"]
    calibration = calibration_output["calibrated_model"]
    assert isinstance(traffic, dict)
    assert isinstance(action, dict)
    assert isinstance(calibration, dict)
    motion = traffic["motion_context"]
    assert isinstance(motion, dict)
    motion["decision_tick"] = tick
    motion["motion_sha256"] = canonical_sha256(
        {key: item for key, item in motion.items() if key != "motion_sha256"}
    )
    traffic["motion_context_sha256"] = motion["motion_sha256"]
    traffic["observed_at_decision_tick"] = tick
    traffic["traffic_sha256"] = canonical_sha256(
        {key: item for key, item in traffic.items() if key != "traffic_sha256"}
    )
    estimate = advisor_module._M2._build_action_bound_rejoin(
        traffic,
        calibration,
        action,
        fuel_tire_service_timing="SEQUENTIAL",
    )
    assert isinstance(estimate, dict)
    traffic_output["estimate"] = estimate
    recommendation["recommendation_basis"]["rejoin_estimate_semantic_sha256"] = (
        advisor_module._M2._rejoin_semantic_sha256(estimate)
    )
    return str(estimate["estimate_sha256"])


def _candidate_m2(source_binding: dict[str, object], *, tick: int) -> dict[str, object]:
    result = copy.deepcopy(_candidate_template())
    context = result["strategy_context"]
    lifecycle = result["lifecycle"]
    input_binding = result["input_binding"]
    recommendation = result["recommendations"][0]
    assert isinstance(context, dict)
    assert isinstance(lifecycle, dict)
    assert isinstance(input_binding, dict)
    assert isinstance(recommendation, dict)
    observation = context["observation"]
    traffic = context["traffic_rejoin"]
    assert isinstance(observation, dict)
    assert isinstance(traffic, dict)
    observation["decision_tick"] = tick
    rejoin_sha256 = _refresh_candidate_traffic(result, tick=tick)
    context["source_binding"] = copy.deepcopy(source_binding)
    context["context_sha256"] = canonical_sha256(
        {key: item for key, item in context.items() if key != "context_sha256"}
    )
    for key, item in source_binding.items():
        input_binding[key] = item
    input_binding["context_sha256"] = context["context_sha256"]
    input_binding["input_lineage_sha256"] = canonical_sha256(
        {key: item for key, item in input_binding.items() if key != "input_lineage_sha256"}
    )
    basis = recommendation["recommendation_basis"]
    valid_until = recommendation["valid_until"]
    assert isinstance(basis, dict)
    assert isinstance(valid_until, dict)
    basis["source_id"] = source_binding["source_id"]
    basis["session_id"] = source_binding["session_id"]
    recommendation_id = f"m2-strategy:{canonical_sha256(basis)}"
    recommendation["recommendation_id"] = recommendation_id
    recommendation["evidence_ids"] = [
        f"fuel-replay:{input_binding['fuel_replay_sha256']}",
        f"m1-pit-stint:{input_binding['m1_receipt_sha256']}",
        f"strategy-context:{context['context_sha256']}",
        f"rules-profile:{result['rules_binding']['profile_sha256']}",
        f"rejoin-estimate:{rejoin_sha256}",
    ]
    valid_until["context_sha256"] = context["context_sha256"]
    valid_until["recompute_after_decision_tick"] = tick + 1
    lifecycle["active_recommendation_id"] = recommendation_id
    lifecycle["events"] = [
        {
            "event": "ISSUE",
            "recommendation_id": recommendation_id,
            "supersedes_id": None,
        }
    ]
    lifecycle["observation_point"] = {
        "decision_tick": tick,
        "session_epoch": 1,
        "source_epoch": 1,
    }
    return _rehash_m2(result)


def _advance_candidate_m2(
    previous: dict[str, object],
    *,
    tick: int,
    recommended_lap_from_now: int | None = None,
) -> dict[str, object]:
    result = copy.deepcopy(previous)
    context = result["strategy_context"]
    lifecycle = result["lifecycle"]
    input_binding = result["input_binding"]
    recommendation = result["recommendations"][0]
    assert isinstance(context, dict)
    assert isinstance(lifecycle, dict)
    assert isinstance(input_binding, dict)
    assert isinstance(recommendation, dict)
    observation = context["observation"]
    traffic = context["traffic_rejoin"]
    action = recommendation["action"]
    basis = recommendation["recommendation_basis"]
    valid_until = recommendation["valid_until"]
    assert isinstance(observation, dict)
    assert isinstance(traffic, dict)
    assert isinstance(action, dict)
    assert isinstance(basis, dict)
    assert isinstance(valid_until, dict)
    previous_id = str(lifecycle["active_recommendation_id"])
    observation["decision_tick"] = tick
    if recommended_lap_from_now is not None:
        action["recommended_lap_from_now"] = recommended_lap_from_now
        basis_action = basis["action"]
        assert isinstance(basis_action, dict)
        basis_action["recommended_lap_from_now"] = recommended_lap_from_now
    rejoin_sha256 = _refresh_candidate_traffic(result, tick=tick)
    context["context_sha256"] = canonical_sha256(
        {key: item for key, item in context.items() if key != "context_sha256"}
    )
    input_binding["context_sha256"] = context["context_sha256"]
    input_binding["input_lineage_sha256"] = canonical_sha256(
        {key: item for key, item in input_binding.items() if key != "input_lineage_sha256"}
    )
    recommendation_id = f"m2-strategy:{canonical_sha256(basis)}"
    recommendation["recommendation_id"] = recommendation_id
    recommendation["supersedes_id"] = previous_id if recommendation_id != previous_id else None
    evidence_ids = recommendation["evidence_ids"]
    assert isinstance(evidence_ids, list)
    evidence_ids[2] = f"strategy-context:{context['context_sha256']}"
    evidence_ids[4] = f"rejoin-estimate:{rejoin_sha256}"
    valid_until["context_sha256"] = context["context_sha256"]
    valid_until["pit_entry_deadline_laps_completed"] = (
        observation["laps_completed"] + action["recommended_lap_from_now"]
    )
    valid_until["recompute_after_decision_tick"] = tick + 1
    lifecycle["active_recommendation_id"] = recommendation_id
    lifecycle["events"] = (
        [
            {
                "event": "NO_CHANGE",
                "reason_codes": ["ACTIVE_STRATEGY_UNCHANGED"],
                "recommendation_id": recommendation_id,
            }
        ]
        if recommendation_id == previous_id
        else [
            {
                "event": "REVOKE",
                "reason_codes": ["STRATEGY_BASIS_CHANGED"],
                "recommendation_id": previous_id,
            },
            {
                "event": "ISSUE",
                "recommendation_id": recommendation_id,
                "supersedes_id": previous_id,
            },
        ]
    )
    lifecycle["observation_point"] = {
        "decision_tick": tick,
        "session_epoch": 1,
        "source_epoch": 1,
    }
    lifecycle["previous_state_sha256"] = previous["m2_strategy_receipt_sha256"]
    previous_lifecycle = previous["lifecycle"]
    assert isinstance(previous_lifecycle, dict)
    lifecycle["state_revision"] = previous_lifecycle["state_revision"] + 1
    return _rehash_m2(result)


class _Bundle(dict[str, Any]):
    open_run: Callable[[], AbstractContextManager[ValidatedIbtRun | ValidatedCollectorRun]]


def _make_bundle(tmp_path: Path, kind: str) -> _Bundle:
    frames = _SHARED["_paired_frames"]()
    decision_tick = int(frames[-1]["SessionTick"])
    if kind == "ibt":
        source_path = tmp_path / "paired.ibt"

        def open_run() -> AbstractContextManager[ValidatedIbtRun]:
            return _SHARED["_open_ibt"](source_path, frames)

    else:
        source_path = tmp_path / "paired.jsonl"
        _SHARED["_write_collector"](source_path, frames)

        def open_run() -> AbstractContextManager[ValidatedCollectorRun]:
            return _SHARED["_open_collector"](source_path)

    with open_run() as run:
        source_sha, normalized_sha, event_sha = _SHARED["_m1_trust_roots"](run)
        evidence = run.evidence.to_dict()
    sample_count_key = "record_count" if kind == "ibt" else "frame_record_count"
    source_binding = {
        "event_receipt_sha256": event_sha,
        "normalized_samples_sha256": normalized_sha,
        "sample_count": evidence[sample_count_key],
        "session_id": evidence["session_id"],
        "source_id": evidence["source_id"],
        "source_kind": evidence["source_kind"],
        "source_sha256": source_sha,
    }
    m2 = _wait_m2(source_binding, tick=decision_tick)
    with open_run() as run:
        driving = build_driving_model_replay(
            run,
            config=DrivingAnalysisConfig(grid_step_m=3.0),
        )
    assert driving["readiness_status"] == "PASS"
    driving_bytes = _serialized(driving)
    diagnosis = build_diagnosis_evidence(driving_bytes)
    with open_run() as run:
        timeline = build_advisor_timeline(
            run,
            [m2],
            driving_bytes,
            expected_m2_receipt_sha256s=[m2["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(driving_bytes).hexdigest(),
        )
    return _Bundle(
        {
            "clock_sha": timeline["clock_receipt"]["clock_receipt_sha256"],
            "diagnosis": diagnosis,
            "driving_bytes": driving_bytes,
            "frames": frames,
            "m2": m2,
            "open_run": open_run,
            "source_binding": source_binding,
            "timeline": timeline,
        }
    )


@pytest.fixture(scope="module")
def ibt_bundle(tmp_path_factory: pytest.TempPathFactory) -> _Bundle:
    # The paired reader and receipts are synthetic and require no private IBT.
    return _make_bundle(tmp_path_factory.mktemp("advisor-ibt"), "ibt")


@pytest.fixture(scope="module")
def collector_bundle(tmp_path_factory: pytest.TempPathFactory) -> _Bundle:
    return _make_bundle(tmp_path_factory.mktemp("advisor-collector"), "collector")


def _validate_bundle(bundle: _Bundle, timeline: object | None = None) -> dict[str, object]:
    m2 = bundle["m2"]
    return validate_advisor_timeline(
        bundle["timeline"] if timeline is None else timeline,
        [m2],
        bundle["driving_bytes"],
        expected_m2_receipt_sha256s=[m2["m2_strategy_receipt_sha256"]],
        expected_driving_replay_serialized_sha256=hashlib.sha256(
            bundle["driving_bytes"]
        ).hexdigest(),
        expected_clock_receipt_sha256=bundle["clock_sha"],
    )


def test_ibt_and_collector_derive_clock_from_same_active_normalized_stream(
    ibt_bundle: _Bundle, collector_bundle: _Bundle
) -> None:
    ibt = _validate_bundle(ibt_bundle)
    collector = _validate_bundle(collector_bundle)
    assert ibt == ibt_bundle["timeline"]
    assert collector == collector_bundle["timeline"]
    assert ibt["clock_receipt"]["input_kind"] == "ibt"
    assert collector["clock_receipt"]["input_kind"] == "collector"
    assert (
        ibt["clock_receipt"]["bindings"][0]["session_time_us"]
        == collector["clock_receipt"]["bindings"][0]["session_time_us"]
    )
    assert (
        ibt["input_binding"]["normalized_samples_sha256"]
        != collector["input_binding"]["normalized_samples_sha256"]
    )


def test_missing_m2_candidate_yields_one_p3_and_zero_tactical(
    ibt_bundle: _Bundle,
) -> None:
    result = ibt_bundle["timeline"]
    assert result["status"] == "WAIT_DATA"
    assert result["summary"] == {
        "decision_count": 0,
        "final_active_tactical_count": 0,
        "lifecycle_event_count": 1,
        "m2_observation_count": 1,
        "overlay_observation_count": 1,
        "tactical_observation_count": 0,
        "upstream_tactical_candidate_count": 0,
    }
    run = result["speech_policy_run"]
    assert run["decisions"] == []
    assert run["events"][0]["kind"] == "ISSUE"
    assert run["input_records"][0]["payload"]["message_class"] == "overlay_info"
    assert run["input_records"][0]["payload"]["priority"] == "P3"
    assert result["safety"] == {
        "audible": False,
        "executable": False,
        "renderer_present": False,
        "vehicle_control_enabled": False,
    }


def test_valid_upstream_candidate_reaches_policy_while_m3_gates_wait(
    ibt_bundle: _Bundle,
) -> None:
    candidate = _candidate_m2(
        ibt_bundle["source_binding"],
        tick=int(ibt_bundle["frames"][-1]["SessionTick"]),
    )
    with ibt_bundle["open_run"]() as run:
        result = build_advisor_timeline(
            run,
            [candidate],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[candidate["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )
    assert result["status"] == "SHADOW_CANDIDATE_MUTED"
    assert result["summary"]["upstream_tactical_candidate_count"] == 1
    assert result["summary"]["tactical_observation_count"] == 1
    assert result["summary"]["final_active_tactical_count"] == 0
    assert result["bridge_policy"]["diagnosis_promotion_required"] is False
    assert result["bridge_policy"]["driving_diagnosis_ready"] is False
    assert [item["kind"] for item in result["speech_policy_run"]["decisions"]] == [
        "SUPPRESS_MUTED"
    ]
    assert result["speech_policy_run"]["input_records"][0]["payload"]["priority"] != "P3"
    assert all(
        item["audible"] is False and item["executable"] is False
        for item in result["speech_policy_run"]["decisions"]
    )
    assert validate_advisor_timeline(
        result,
        [candidate],
        ibt_bundle["driving_bytes"],
        expected_m2_receipt_sha256s=[candidate["m2_strategy_receipt_sha256"]],
        expected_driving_replay_serialized_sha256=hashlib.sha256(
            ibt_bundle["driving_bytes"]
        ).hexdigest(),
        expected_clock_receipt_sha256=result["clock_receipt"]["clock_receipt_sha256"],
    ) == result


def test_rejects_self_rehashed_recommendation_tamper_with_attacker_digest(
    ibt_bundle: _Bundle,
) -> None:
    candidate = _candidate_m2(
        ibt_bundle["source_binding"],
        tick=int(ibt_bundle["frames"][-1]["SessionTick"]),
    )
    candidate["recommendations"][0]["action"]["recommended_lap_from_now"] += 1
    candidate = _rehash_m2(candidate)
    with (
        ibt_bundle["open_run"]() as run,
        pytest.raises(AdvisorTimelineError, match="M2_RECEIPT_INVALID"),
    ):
        build_advisor_timeline(
            run,
            [candidate],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[candidate["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )


def _replace_rejoin_and_rehash_candidate(
    candidate: dict[str, object], estimate: dict[str, object]
) -> dict[str, object]:
    result = copy.deepcopy(candidate)
    result["traffic_rejoin"]["estimate"] = estimate
    recommendation = result["recommendations"][0]
    basis = recommendation["recommendation_basis"]
    basis["rejoin_estimate_semantic_sha256"] = advisor_module._M2._rejoin_semantic_sha256(estimate)
    recommendation_id = f"m2-strategy:{canonical_sha256(basis)}"
    recommendation["recommendation_id"] = recommendation_id
    recommendation["evidence_ids"][4] = f"rejoin-estimate:{estimate['estimate_sha256']}"
    result["lifecycle"]["active_recommendation_id"] = recommendation_id
    result["lifecycle"]["events"][0]["recommendation_id"] = recommendation_id
    return _rehash_m2(result)


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("recommended_lap_from_now", 0),
        ("fuel_add_l", 21.0),
        ("change_tires", True),
        ("estimated_stationary_service_s", 11.0),
    ],
)
def test_rejects_rehashed_rejoin_for_a_different_action(
    ibt_bundle: _Bundle, field: str, wrong_value: object
) -> None:
    candidate = _candidate_m2(
        ibt_bundle["source_binding"],
        tick=int(ibt_bundle["frames"][-1]["SessionTick"]),
    )
    action = candidate["recommendations"][0]["action"]
    assert action[field] != wrong_value
    wrong_action = {**action, field: wrong_value}
    estimate = advisor_module._M2._build_action_bound_rejoin(
        candidate["strategy_context"]["traffic_rejoin"],
        candidate["calibration"]["calibrated_model"],
        wrong_action,
        fuel_tire_service_timing="SEQUENTIAL",
    )
    assert estimate is not None and estimate["estimate_available"] is True
    # A valid estimate for another action must remain invalid after every
    # dependent hash, recommendation ID, lifecycle ID and external pin agree.
    bad = _replace_rejoin_and_rehash_candidate(candidate, estimate)
    with (
        ibt_bundle["open_run"]() as run,
        pytest.raises(
            AdvisorTimelineError, match="rejoin service does not match recommended action"
        ),
    ):
        build_advisor_timeline(
            run,
            [bad],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[bad["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )


def test_rejects_rehashed_rejoin_timing_inconsistent_with_tire_scenario() -> None:
    candidate = copy.deepcopy(_candidate_v2_template())
    context = candidate["strategy_context"]
    action = candidate["recommendations"][0]["action"]
    assert action["change_tires"] is True
    timing = candidate["traffic_rejoin"]["estimate"]["service_scenario"]["fuel_tire_service_timing"]
    other_timing = "PARALLEL" if timing == "SEQUENTIAL" else "SEQUENTIAL"
    estimate = advisor_module._M2._build_action_bound_rejoin(
        context["traffic_rejoin"],
        candidate["calibration"]["calibrated_model"],
        action,
        fuel_tire_service_timing=other_timing,
    )
    assert estimate is not None and estimate["estimate_available"] is True
    bad = _replace_rejoin_and_rehash_candidate(candidate, estimate)
    with pytest.raises(AdvisorTimelineError, match="rejoin and tire service timing disagree"):
        advisor_module._validate_m2_receipts(
            [bad],
            [bad["m2_strategy_receipt_sha256"]],
            source_binding=context["source_binding"],
            source_epoch=1,
            session_epoch=1,
        )


def test_tactical_same_content_refresh_and_changed_content_supersede(
    ibt_bundle: _Bundle,
) -> None:
    ticks = [int(frame["SessionTick"]) for frame in ibt_bundle["frames"][-3:]]
    first = _candidate_m2(ibt_bundle["source_binding"], tick=ticks[0])
    second = _advance_candidate_m2(first, tick=ticks[1])
    first_action = first["recommendations"][0]["action"]
    assert isinstance(first_action, dict)
    third = _advance_candidate_m2(
        second,
        tick=ticks[2],
        recommended_lap_from_now=int(first_action["recommended_lap_from_now"]) + 1,
    )
    with ibt_bundle["open_run"]() as run:
        result = build_advisor_timeline(
            run,
            [first, second, third],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[
                item["m2_strategy_receipt_sha256"] for item in (first, second, third)
            ],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
            config=SpeechPolicyConfig(muted=False),
        )
    records = result["speech_policy_run"]["input_records"]
    assert [item["input_kind"] for item in records] == [
        "SpeechEnvelope",
        "SpeechRefresh",
        "SpeechEnvelope",
    ]
    first_envelope = records[0]["payload"]
    refresh = records[1]["payload"]
    changed = records[2]["payload"]
    assert refresh["expected_content_revision_sha256"] == first_envelope["content_revision_sha256"]
    assert refresh["previous_envelope_sha256"] == canonical_sha256(first_envelope)
    assert (
        changed["supersedes_content_revision_sha256"] == first_envelope["content_revision_sha256"]
    )
    assert result["status"] == "SHADOW_ACTIVE_CANDIDATE"
    assert result["summary"]["final_active_tactical_count"] == 1
    assert all(
        item["audible"] is False and item["executable"] is False
        for item in result["speech_policy_run"]["decisions"]
    )


def test_default_muted_candidate_status_matches_empty_final_active_state(
    ibt_bundle: _Bundle,
) -> None:
    candidate = _candidate_m2(
        ibt_bundle["source_binding"],
        tick=int(ibt_bundle["frames"][-1]["SessionTick"]),
    )
    with ibt_bundle["open_run"]() as run:
        result = build_advisor_timeline(
            run,
            [candidate],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[candidate["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )
    assert result["status"] == "SHADOW_CANDIDATE_MUTED"
    assert result["summary"]["final_active_tactical_count"] == 0
    assert result["speech_policy_run"]["final_active_envelopes"] == []
    assert [item["kind"] for item in result["speech_policy_run"]["decisions"]] == ["SUPPRESS_MUTED"]


def test_two_wait_observations_use_double_cas_refresh(ibt_bundle: _Bundle) -> None:
    first = _wait_m2(
        ibt_bundle["source_binding"],
        tick=int(ibt_bundle["frames"][-2]["SessionTick"]),
    )
    second = _wait_m2(
        ibt_bundle["source_binding"],
        tick=int(ibt_bundle["frames"][-1]["SessionTick"]),
        revision=2,
        previous_sha256=first["m2_strategy_receipt_sha256"],
    )
    with ibt_bundle["open_run"]() as run:
        result = build_advisor_timeline(
            run,
            [first, second],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[
                first["m2_strategy_receipt_sha256"],
                second["m2_strategy_receipt_sha256"],
            ],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )
    assert [item["input_kind"] for item in result["speech_policy_run"]["input_records"]] == [
        "SpeechEnvelope",
        "SpeechRefresh",
    ]
    refresh = result["speech_policy_run"]["input_records"][1]["payload"]
    first_envelope = result["speech_policy_run"]["input_records"][0]["payload"]
    assert refresh["expected_content_revision_sha256"] == first_envelope["content_revision_sha256"]
    assert refresh["previous_envelope_sha256"] == canonical_sha256(first_envelope)


def test_rejects_self_rehashed_m2_nested_schema_even_with_attacker_digest(
    ibt_bundle: _Bundle,
) -> None:
    bad = copy.deepcopy(ibt_bundle["m2"])
    bad["input_binding"]["unexpected"] = "attacker"
    bad = _rehash_m2(bad)
    with (
        ibt_bundle["open_run"]() as run,
        pytest.raises(AdvisorTimelineError, match="M2_RECEIPT_INVALID|SCHEMA_INVALID"),
    ):
        build_advisor_timeline(
            run,
            [bad],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[bad["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )


@pytest.mark.parametrize("field", ["event_identity", "horizon", "capabilities"])
def test_rejects_self_rehashed_unused_m2_nested_extra_with_attacker_digest(
    ibt_bundle: _Bundle,
    field: str,
) -> None:
    bad = copy.deepcopy(ibt_bundle["m2"])
    bad[field]["attacker"] = True
    bad = _rehash_m2(bad)
    with (
        ibt_bundle["open_run"]() as run,
        pytest.raises(AdvisorTimelineError, match="M2_RECEIPT_INVALID|SCHEMA_INVALID"),
    ):
        build_advisor_timeline(
            run,
            [bad],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[bad["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )


def test_rejects_self_rehashed_m2_quality_reason_with_attacker_digest(
    ibt_bundle: _Bundle,
) -> None:
    bad = copy.deepcopy(ibt_bundle["m2"])
    bad["quality_gate"]["reason_codes"] = ["ATTACKER"]
    bad = _rehash_m2(bad)
    with (
        ibt_bundle["open_run"]() as run,
        pytest.raises(AdvisorTimelineError, match="M2_RECEIPT_INVALID"),
    ):
        build_advisor_timeline(
            run,
            [bad],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[bad["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )


def test_every_populated_m2_nested_object_is_closed_to_unknown_keys(
    ibt_bundle: _Bundle,
) -> None:
    receipts = [copy.deepcopy(ibt_bundle["m2"]), copy.deepcopy(_candidate_template())]
    for receipt in receipts:
        source_binding = copy.deepcopy(receipt["strategy_context"]["source_binding"])
        for path in _nested_dict_paths(receipt):
            if not path:
                continue
            bad = copy.deepcopy(receipt)
            target = _at_path(bad, path)
            assert isinstance(target, dict)
            target["attacker"] = True
            bad = _rehash_m2(bad)
            with pytest.raises(AdvisorTimelineError):
                advisor_module._validate_m2_receipts(
                    [bad],
                    [bad["m2_strategy_receipt_sha256"]],
                    source_binding=source_binding,
                    source_epoch=1,
                    session_epoch=1,
                )


def test_rejects_self_rehashed_m2_context_lineage_even_with_attacker_digest(
    ibt_bundle: _Bundle,
) -> None:
    bad = copy.deepcopy(ibt_bundle["m2"])
    context = bad["strategy_context"]
    binding = bad["input_binding"]
    context["source_binding"]["session_id"] = "different-session"
    context["context_sha256"] = canonical_sha256(
        {key: item for key, item in context.items() if key != "context_sha256"}
    )
    binding["context_sha256"] = context["context_sha256"]
    binding["input_lineage_sha256"] = canonical_sha256(
        {key: item for key, item in binding.items() if key != "input_lineage_sha256"}
    )
    bad = _rehash_m2(bad)
    with ibt_bundle["open_run"]() as run, pytest.raises(AdvisorTimelineError):
        build_advisor_timeline(
            run,
            [bad],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[bad["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )


def test_rejects_self_rehashed_lifecycle_payload(ibt_bundle: _Bundle) -> None:
    bad = copy.deepcopy(ibt_bundle["m2"])
    bad["lifecycle"]["events"][0]["reason_codes"] = ["ATTACKER"]
    bad = _rehash_m2(bad)
    with (
        ibt_bundle["open_run"]() as run,
        pytest.raises(AdvisorTimelineError, match="M2_LIFECYCLE_INVALID"),
    ):
        build_advisor_timeline(
            run,
            [bad],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[bad["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )


def test_v2_tire_strategy_is_audited_and_rehashed_wear_forge_fails() -> None:
    candidate = copy.deepcopy(_candidate_v2_template())
    context = candidate["strategy_context"]
    assert isinstance(context, dict)
    source_binding = context["source_binding"]
    assert isinstance(source_binding, dict)

    admitted = advisor_module._validate_m2_receipts(
        [candidate],
        [candidate["m2_strategy_receipt_sha256"]],
        source_binding=source_binding,
        source_epoch=1,
        session_epoch=1,
    )
    assert admitted == (candidate,)

    forged = copy.deepcopy(candidate)
    tire_strategy = forged["tire_strategy"]
    assert isinstance(tire_strategy, dict)
    belief = tire_strategy["belief"]
    assert isinstance(belief, dict)
    physical_wear = belief["physical_wear"]
    assert isinstance(physical_wear, dict)
    physical_wear["estimate_available"] = True
    belief["belief_sha256"] = canonical_sha256(
        {key: value for key, value in belief.items() if key != "belief_sha256"}
    )
    forged = _rehash_m2(forged)

    with pytest.raises(AdvisorTimelineError, match="physical-wear boundary"):
        advisor_module._validate_m2_receipts(
            [forged],
            [forged["m2_strategy_receipt_sha256"]],
            source_binding=source_binding,
            source_epoch=1,
            session_epoch=1,
        )


def test_rejects_epoch_drift_in_continuation(ibt_bundle: _Bundle) -> None:
    first = _wait_m2(
        ibt_bundle["source_binding"],
        tick=int(ibt_bundle["frames"][-2]["SessionTick"]),
    )
    second = _wait_m2(
        ibt_bundle["source_binding"],
        tick=int(ibt_bundle["frames"][-1]["SessionTick"]),
        revision=2,
        previous_sha256=first["m2_strategy_receipt_sha256"],
        source_epoch=2,
        session_epoch=2,
    )
    with (
        ibt_bundle["open_run"]() as run,
        pytest.raises(AdvisorTimelineError, match="M2_RECEIPT_INVALID|M2_LIFECYCLE_INVALID"),
    ):
        build_advisor_timeline(
            run,
            [first, second],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[
                first["m2_strategy_receipt_sha256"],
                second["m2_strategy_receipt_sha256"],
            ],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )


def test_rejects_self_rehashed_m3_policy_and_synced_timeline_hashes(
    ibt_bundle: _Bundle,
) -> None:
    bad = copy.deepcopy(ibt_bundle["diagnosis"])
    bad["policy"]["attacker"] = True
    bad = _rehash_diagnosis(bad)
    forged_timeline = copy.deepcopy(ibt_bundle["timeline"])
    forged_timeline["diagnosis_evidence_sha256"] = bad["diagnosis_evidence_sha256"]
    forged_timeline["input_binding"]["diagnosis_evidence_sha256"] = bad["diagnosis_evidence_sha256"]
    forged_timeline["advisor_timeline_sha256"] = canonical_sha256(
        {key: item for key, item in forged_timeline.items() if key != "advisor_timeline_sha256"}
    )
    with pytest.raises(AdvisorTimelineError, match="TIMELINE_INVALID"):
        _validate_bundle(
            ibt_bundle,
            forged_timeline,
        )


def test_rejects_self_rehashed_clock_time_against_independent_clock_digest(
    ibt_bundle: _Bundle,
) -> None:
    bad = copy.deepcopy(ibt_bundle["timeline"])
    clock = bad["clock_receipt"]
    clock["bindings"][0]["session_time_us"] += 1_000_000
    clock["clock_receipt_sha256"] = canonical_sha256(
        {key: item for key, item in clock.items() if key != "clock_receipt_sha256"}
    )
    bad["advisor_timeline_sha256"] = canonical_sha256(
        {key: item for key, item in bad.items() if key != "advisor_timeline_sha256"}
    )
    with pytest.raises(AdvisorTimelineError, match="CLOCK_BINDING_INVALID"):
        _validate_bundle(ibt_bundle, bad)


def test_rejects_cross_source_and_normalized_mismatch(
    ibt_bundle: _Bundle, collector_bundle: _Bundle
) -> None:
    m2 = collector_bundle["m2"]
    with ibt_bundle["open_run"]() as run, pytest.raises(AdvisorTimelineError):
        build_advisor_timeline(
            run,
            [m2],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[m2["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )


def test_rejects_timeline_speech_tamper_after_outer_rehash(
    ibt_bundle: _Bundle,
) -> None:
    bad = copy.deepcopy(ibt_bundle["timeline"])
    bad["speech_policy_run"]["events"][0]["reason_codes"] = ["ATTACKER"]
    bad["advisor_timeline_sha256"] = canonical_sha256(
        {key: item for key, item in bad.items() if key != "advisor_timeline_sha256"}
    )
    with pytest.raises(AdvisorTimelineError, match="TIMELINE_INVALID"):
        _validate_bundle(ibt_bundle, bad)


def test_rejects_inactive_or_forged_run(ibt_bundle: _Bundle) -> None:
    forged = object.__new__(ValidatedIbtRun)
    with pytest.raises(AdvisorTimelineError, match="RUN_NOT_ACTIVE"):
        build_advisor_timeline(
            forged,
            [ibt_bundle["m2"]],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[ibt_bundle["m2"]["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )


def test_deterministic_across_fresh_admissions(ibt_bundle: _Bundle) -> None:
    with ibt_bundle["open_run"]() as run:
        repeated = build_advisor_timeline(
            run,
            [ibt_bundle["m2"]],
            ibt_bundle["driving_bytes"],
            expected_m2_receipt_sha256s=[ibt_bundle["m2"]["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(
                ibt_bundle["driving_bytes"]
            ).hexdigest(),
        )
    assert repeated == ibt_bundle["timeline"]


@pytest.mark.skipif(
    not all(path.is_file() for path in (IBT_PATH, M2_PATH, M3_PATH, DRIVING_PATH)),
    reason="REQUIRES_DATA: complete Audi artifacts absent",
)
def test_real_audi_is_source_derived_one_p3_wait_and_zero_tactical() -> None:
    m2 = _load(M2_PATH)
    expected_m3 = _load(M3_PATH)
    driving = DRIVING_PATH.read_bytes()
    with open_ibt_telemetry(
        IBT_PATH,
        source_id=str(m2["input_binding"]["source_id"]),
        session_id=str(m2["input_binding"]["session_id"]),
    ) as run:
        result = build_advisor_timeline(
            run,
            [m2],
            driving,
            expected_m2_receipt_sha256s=[m2["m2_strategy_receipt_sha256"]],
            expected_driving_replay_serialized_sha256=hashlib.sha256(driving).hexdigest(),
        )
    assert result["status"] == "WAIT_DATA"
    assert result["summary"]["tactical_observation_count"] == 0
    assert result["summary"]["final_active_tactical_count"] == 0
    assert result["speech_policy_run"]["decisions"] == []
    assert result["clock_receipt"]["bindings"] == [
        {
            "decision_tick": 332_490,
            "m2_receipt_sha256": m2["m2_strategy_receipt_sha256"],
            "session_time_us": 2_554_216_667,
        }
    ]
    assert result["diagnosis_evidence_sha256"] == expected_m3["diagnosis_evidence_sha256"]
    assert (
        result["clock_receipt"]["clock_receipt_sha256"]
        == "f490e0096080c36232d9a10bc62102a768ad491278be306aa584358c081e0cf5"
    )
    assert (
        result["speech_policy_run"]["receipt"]["receipt_sha256"]
        == "436755fbd09d15a6f24b4f4a29384bfdb6f0dd34518152bc0bc4c4fcdee77eab"
    )
    assert result["contract_version"] == "advisor-timeline-v3"
    assert validate_advisor_timeline(
        result,
        [m2],
        driving,
        expected_m2_receipt_sha256s=[m2["m2_strategy_receipt_sha256"]],
        expected_driving_replay_serialized_sha256=hashlib.sha256(driving).hexdigest(),
        expected_clock_receipt_sha256=result["clock_receipt"]["clock_receipt_sha256"],
    ) == result
    assert result["advisor_timeline_sha256"] == canonical_sha256(
        {key: item for key, item in result.items() if key != "advisor_timeline_sha256"}
    )
