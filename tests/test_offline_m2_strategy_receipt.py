from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from iracing_ai_engineer.adapters import (
    CollectorInputEvidence,
    IbtInputEvidence,
    open_ibt_telemetry,
)
from iracing_ai_engineer.events import process_telemetry_events
from iracing_ai_engineer.fuel import FuelScenario
from iracing_ai_engineer.model_replay import _build_fuel_model_replay_samples
from iracing_ai_engineer.retrieved_live_analysis import (
    MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION,
    build_matched_pit_calibration_model,
    build_tire_performance_belief,
)
from iracing_ai_engineer.telemetry import (
    SourceKind,
    TelemetrySample,
    normalize_sdk_frame,
)


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_M1 = _load_module(
    "test_m2_pit_stint_dependency", "scripts/build_offline_pit_stint_receipt.py"
)
_M2 = _load_module(
    "build_offline_m2_strategy_receipt",
    "scripts/build_offline_m2_strategy_receipt.py",
)

M2StrategyReceiptError = _M2.M2StrategyReceiptError
build_m2_strategy_receipt = _M2.build_m2_strategy_receipt
canonical_sha256 = _M2.canonical_sha256

SOURCE_ID = "fixture-ibt_offline"
SESSION_ID = "shared-model-session"
SOURCE_SHA256 = "a" * 64


def _json_round_trip(value: object):
    return json.loads(json.dumps(value, allow_nan=False))


def _normalized_sha256(samples: list[TelemetrySample]) -> str:
    digest = hashlib.sha256()
    for sample in samples:
        encoded = sample.to_json_line().encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
    return digest.hexdigest()


def _samples(
    *,
    source_kind: SourceKind = SourceKind.IBT_OFFLINE,
    source_id: str = SOURCE_ID,
) -> list[TelemetrySample]:
    result: list[TelemetrySample] = []
    previous = None
    for index in range(9 * 60 + 1):
        on_pit_road = 300 <= index < 330
        in_stall = 309 <= index < 323
        service_active = 310 <= index < 320
        fuel_l = 60.0 - index * (4.0 / 60.0)
        if 310 <= index < 320:
            fuel_l = 40.0 + (index - 310) * 2.0
        elif index >= 320:
            fuel_l = 60.0 - (index - 320) * (4.0 / 60.0)
        raw = {
            "FuelLevel": fuel_l,
            "Lap": 1 + index // 60,
            "LapCompleted": index // 60,
            "LapDistPct": (index % 60) / 60.0,
            "OnPitRoad": on_pit_road,
            "PitstopActive": service_active,
            "PlayerCarInPitStall": in_stall,
            "PlayerTrackSurface": 3,
            "SessionFlags": 1,
            "SessionNum": 1,
            "SessionTick": 10_000 + index,
            "SessionTime": index / 60.0,
            "Speed": 50.0,
        }
        sample = normalize_sdk_frame(
            raw,
            source_id=source_id,
            session_id=SESSION_ID,
            source_kind=source_kind,
            buffer_tick=index,
            captured_monotonic_s=(
                index / 60.0 if source_kind is SourceKind.SDK_LIVE else None
            ),
            previous=previous,
        )
        result.append(sample)
        previous = sample
    return result


@pytest.fixture(scope="module")
def upstream() -> tuple[dict[str, object], dict[str, object]]:
    samples = _samples()
    evidence = IbtInputEvidence(
        source_id=SOURCE_ID,
        session_id=SESSION_ID,
        source_sha256=SOURCE_SHA256,
        byte_size=123_456,
        record_count=len(samples),
        tick_rate_hz=60,
    )
    scenario = FuelScenario(
        current_fuel_l=20.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
        reserve_l=1.0,
        minimum_valid_laps=5,
    )
    fuel = _build_fuel_model_replay_samples(
        iter(samples),
        input_kind="ibt",
        input_evidence=evidence,
        tick_rate_hz=60,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
        scenario=scenario,
    )
    _, event_receipt = process_telemetry_events(samples)
    m1 = _M1._build_receipt_from_samples(
        iter(samples),
        input_evidence=evidence,
        expected_source_sha256=SOURCE_SHA256,
        expected_normalized_samples_sha256=_normalized_sha256(samples),
        expected_event_receipt_sha256=event_receipt.receipt_sha256,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
    )
    # Persisted JSON has arrays, whereas internal Python result objects may
    # still contain tuples.  M2 consumes the persisted receipt contract.
    return _json_round_trip(fuel), _json_round_trip(m1)


@pytest.fixture(scope="module")
def collector_upstream() -> tuple[dict[str, object], dict[str, object]]:
    source_id = "fixture-sdk-live"
    source_sha256 = "c" * 64
    samples = _samples(source_kind=SourceKind.SDK_LIVE, source_id=source_id)
    evidence = CollectorInputEvidence(
        source_id=source_id,
        session_id=SESSION_ID,
        source_kind=SourceKind.SDK_LIVE,
        sim_mode="full",
        completion_status="COMPLETE",
        semantic_record_count=len(samples) + 3,
        records_sha256=source_sha256,
        frame_record_count=len(samples),
        event_record_count=0,
        schema_record_count=1,
        session_info_record_count=1,
        samples_seen=len(samples),
        duplicate_sample_count=0,
        duplicate_conflict_count=0,
        dropped_tick_count=0,
        stale_event_count=0,
        session_reset_count=0,
        schema_change_count=0,
        schema_epoch_count=1,
        session_epoch_count=1,
        first_buffer_tick=0,
        last_buffer_tick=len(samples) - 1,
        tick_rate_hz_values=(60,),
    )
    scenario = FuelScenario(
        current_fuel_l=20.0,
        tank_capacity_l=120.0,
        refuel_rate_l_per_s=2.0,
        remaining_laps=10,
        reserve_l=1.0,
        minimum_valid_laps=5,
    )
    fuel = _build_fuel_model_replay_samples(
        iter(samples),
        input_kind="collector",
        input_evidence=evidence,
        tick_rate_hz=60,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
        scenario=scenario,
    )
    _, event_receipt = process_telemetry_events(samples)
    m1 = _M1._build_receipt_from_samples(
        iter(samples),
        input_evidence=evidence,
        expected_source_sha256=source_sha256,
        expected_normalized_samples_sha256=_normalized_sha256(samples),
        expected_event_receipt_sha256=event_receipt.receipt_sha256,
        stale_after_s=0.5,
        opponent_error_policy="degrade",
    )
    return _json_round_trip(fuel), _json_round_trip(m1)


def _source_binding(
    fuel: dict[str, object], m1: dict[str, object]
) -> dict[str, object]:
    fuel_evidence = fuel["input_evidence"]
    fuel_normalized = fuel["normalized_input_receipt"]
    fuel_event = fuel["event_receipt"]
    assert isinstance(fuel_evidence, dict)
    assert isinstance(fuel_normalized, dict)
    assert isinstance(fuel_event, dict)
    assert fuel_evidence == m1["input_evidence"]
    source_digest_key = (
        "source_sha256" if fuel["input_kind"] == "ibt" else "records_sha256"
    )
    return {
        "event_receipt_sha256": fuel_event["receipt_sha256"],
        "normalized_samples_sha256": fuel_normalized["samples_sha256"],
        "sample_count": fuel_normalized["sample_count"],
        "session_id": fuel_evidence["session_id"],
        "source_id": fuel_evidence["source_id"],
        "source_kind": fuel_evidence["source_kind"],
        "source_sha256": fuel_evidence[source_digest_key],
    }


def _identity(*, official: bool | None = True) -> dict[str, object]:
    return {
        "car_class_id": 5,
        "event_type": "Race",
        "official": official,
        "provenance": "CONTRACT_FIXTURE",
        "race_week": 3,
        "season_id": 2,
        "series_id": 1,
        "sim_build": "fixture-build",
        "track_config": "Grand Prix",
        "track_id": 4,
    }


def _calibration(identity: dict[str, object]) -> dict[str, object]:
    material: dict[str, object] = {
        "identity_sha256": canonical_sha256(identity),
        "method_version": "matched-pit-service-median-v1",
        "pit_lane_loss_s": 25.0,
        "pit_lane_loss_uncertainty_s": [24.0, 26.0],
        "refuel_rate_l_per_s": 2.0,
        "sample_count": 3,
        "service_labels_available": True,
        "source_receipt_sha256": "b" * 64,
        "status": "CALIBRATED_MATCHED_BASELINE",
        "tire_change_time_s": 30.0,
    }
    return {**material, "model_sha256": canonical_sha256(material)}


def _traffic(identity: dict[str, object], *, decision_tick: int) -> dict[str, object]:
    motion_material: dict[str, object] = {
        "availability": "AVAILABLE",
        "contract_version": "traffic-motion-context-v1",
        "decision_tick": decision_tick,
        "identity_sha256": canonical_sha256(identity),
        "observation_window_s": 10.0,
        "opponents": [
            {
                "car_idx": 1,
                "current_signed_lap_delta": 0.5,
                "point_count": 601,
                "rate_laps_per_s": 0.01,
                "rate_range_laps_per_s": [0.0095, 0.0105],
            }
        ],
        "player": {
            "car_idx": 0,
            "point_count": 601,
            "rate_laps_per_s": 0.01,
            "rate_range_laps_per_s": [0.0095, 0.0105],
        },
        "reason_codes": [],
        "source_receipt_sha256": "d" * 64,
        "status": "VERIFIED_TIME_DOMAIN_MOTION",
        "traffic_map_revision_sha256": "c" * 64,
    }
    motion = {
        **motion_material,
        "motion_sha256": canonical_sha256(motion_material),
    }
    material: dict[str, object] = {
        "estimate_available": False,
        "identity_sha256": canonical_sha256(identity),
        "map_revision_sha256": "c" * 64,
        "motion_context": motion,
        "motion_context_sha256": motion["motion_sha256"],
        "observed_at_decision_tick": decision_tick,
        "rejoin_gap_range_s": None,
        "source_receipt_sha256": "d" * 64,
        "status": "OBSERVED_ONLY_WAIT_ACTION_BOUND_REJOIN",
    }
    return {**material, "traffic_sha256": canonical_sha256(material)}


def _context(
    fuel: dict[str, object],
    m1: dict[str, object],
    *,
    identity: dict[str, object] | None = None,
    horizon_kind: str = "LAPS",
    decision_tick: int = 100,
    calibration: bool = True,
    traffic: bool = True,
    pits_open: bool | None = True,
    penalty_state: str | None = "CLEAR",
    stale: bool = False,
) -> dict[str, object]:
    event_identity = copy.deepcopy(identity or _identity())
    if horizon_kind == "LAPS":
        horizon = {
            "kind": "LAPS",
            "laps_remaining": 10,
            "leader_eta_to_next_crossing_s": None,
            "player_is_leader": None,
            "provenance": "CONTRACT_FIXTURE",
            "reference_lap_time_s": None,
            "time_remaining_s": None,
        }
    else:
        horizon = {
            "kind": "TIMED",
            "laps_remaining": None,
            "leader_eta_to_next_crossing_s": 50.0,
            "player_is_leader": True,
            "provenance": "CONTRACT_FIXTURE",
            "reference_lap_time_s": 100.0,
            "time_remaining_s": 950.0,
        }
    material: dict[str, object] = {
        "calibration_model": (_calibration(event_identity) if calibration else None),
        "contract_version": "offline-m2-strategy-context-v1",
        "event_identity": event_identity,
        "horizon": horizon,
        "observation": {
            "decision_tick": decision_tick,
            "laps_completed": 0,
            "penalty_state": penalty_state,
            "pits_open": pits_open,
            "reset": False,
            "schema_changed": False,
            "session_epoch": 1,
            "source_epoch": 1,
            "stale": stale,
        },
        "source_binding": _source_binding(fuel, m1),
        "strategy_policy": {
            "conservative_quantile": 0.9,
            "reserve_l": 1.0,
            "selection_policy": "LATEST_COMMON_FUEL_FEASIBLE",
        },
        "traffic_rejoin": (
            _traffic(event_identity, decision_tick=decision_tick) if traffic else None
        ),
        "vehicle_context": {
            "provenance": "CONTRACT_FIXTURE",
            "tank_capacity_l": 120.0,
        },
    }
    return {**material, "context_sha256": canonical_sha256(material)}


def _tire_stint_context(
    identity: dict[str, object],
    *,
    decision_tick: int,
    age_laps: int = 6,
    compound: int = 0,
) -> dict[str, object]:
    material: dict[str, object] = {
        "availability": "AVAILABLE",
        "contract_version": "tire-stint-context-v1",
        "current_laps_completed": age_laps,
        "current_tire_compound": compound,
        "decision_tick": decision_tick,
        "identity_sha256": canonical_sha256(identity),
        "on_pit_road": False,
        "origin_kind": "OBSERVED_ZERO_COMPLETED_LAPS",
        "origin_laps_completed": 0,
        "origin_tick": 0,
        "physical_wear": copy.deepcopy(_M2._TIRE_PHYSICAL_WEAR_UNAVAILABLE),
        "reason_codes": [],
        "source_receipt_sha256": "d" * 64,
        "status": "AVAILABLE_OBSERVED_STINT_AGE",
        "stint_age_completed_laps": age_laps,
        "tire_sets_used": 1,
    }
    return {**material, "context_sha256": canonical_sha256(material)}


def _tire_performance_model(
    identity: dict[str, object],
    *,
    slope: float = 10.0,
    slope_range: tuple[float, float] = (9.0, 11.0),
    compound: int = 0,
) -> dict[str, object]:
    low, high = slope_range
    status = (
        "PASS_SHADOW_POSITIVE_DEGRADATION"
        if low > 0.0
        else "WAIT_POSITIVE_DEGRADATION_NOT_OBSERVED"
        if high <= 0.0
        else "WAIT_DEGRADATION_SIGN_AMBIGUOUS"
    )
    material: dict[str, object] = {
        "advisor_only": True,
        "contract_version": "tire-performance-model-v1",
        "estimate_available": status == "PASS_SHADOW_POSITIVE_DEGRADATION",
        "fuel_load_model_sha256": "f" * 64,
        "identity_sha256": canonical_sha256(identity),
        "independent_stint_count": 3,
        "max_supported_stint_age_laps": 100,
        "method_version": "fuel-adjusted-disjoint-pair-envelope-v1",
        "pair_count": 3,
        "performance_age_slope_s_per_lap": slope,
        "performance_age_slope_uncertainty_s_per_lap": [low, high],
        "physical_wear": copy.deepcopy(_M2._TIRE_PHYSICAL_WEAR_UNAVAILABLE),
        "source_receipt_sha256": "9" * 64,
        "status": status,
        "tire_compound": compound,
    }
    return {**material, "model_sha256": canonical_sha256(material)}


def _context_v2(
    fuel: dict[str, object],
    m1: dict[str, object],
    *,
    tire_model: dict[str, object] | None,
    decision_tick: int = 100,
) -> dict[str, object]:
    context = _context(fuel, m1, decision_tick=decision_tick)
    identity = context["event_identity"]
    assert isinstance(identity, dict)
    context["contract_version"] = "offline-m2-strategy-context-v2"
    context["tire_performance_model"] = copy.deepcopy(tire_model)
    context["tire_stint_context"] = _tire_stint_context(
        identity,
        decision_tick=decision_tick,
    )
    return _rehash_context(context)


def _rehash_context(context: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(context)
    result["context_sha256"] = canonical_sha256(
        {key: value for key, value in result.items() if key != "context_sha256"}
    )
    return result


def _advance_context(
    context: dict[str, object],
    *,
    decision_tick: int,
    pits_open: bool | None = True,
    penalty_state: str | None = "CLEAR",
    stale: bool = False,
) -> dict[str, object]:
    result = copy.deepcopy(context)
    observation = result["observation"]
    traffic = result["traffic_rejoin"]
    assert isinstance(observation, dict)
    assert isinstance(traffic, dict)
    observation["decision_tick"] = decision_tick
    observation["pits_open"] = pits_open
    observation["penalty_state"] = penalty_state
    observation["stale"] = stale
    traffic["observed_at_decision_tick"] = decision_tick
    motion = traffic["motion_context"]
    assert isinstance(motion, dict)
    motion["decision_tick"] = decision_tick
    motion["motion_sha256"] = canonical_sha256(
        {key: value for key, value in motion.items() if key != "motion_sha256"}
    )
    traffic["motion_context_sha256"] = motion["motion_sha256"]
    traffic["traffic_sha256"] = canonical_sha256(
        {key: value for key, value in traffic.items() if key != "traffic_sha256"}
    )
    tire_stint = result.get("tire_stint_context")
    if isinstance(tire_stint, dict):
        tire_stint["decision_tick"] = decision_tick
        tire_stint["context_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in tire_stint.items()
                if key != "context_sha256"
            }
        )
    return _rehash_context(result)


def _rules(
    identity: dict[str, object], *, finish_rule: str = "LAP_LIMITED"
) -> dict[str, object]:
    selector = {
        key: identity[key]
        for key in (
            "car_class_id",
            "event_type",
            "race_week",
            "season_id",
            "series_id",
            "sim_build",
            "track_config",
            "track_id",
        )
    }
    material: dict[str, object] = {
        "contract_version": "event-rules-profile-v2",
        "official_rules": {
            "finish_rule": finish_rule,
            "fuel_tire_service_timing": "SEQUENTIAL",
            "minimum_pit_stops": 0,
            "no_tire_service_allowed": True,
            "tire_change_required": False,
        },
        "profile_id": "official-fixture-profile",
        "profile_version": 1,
        "selector": selector,
        "source": {
            "authority": "IRACING_OFFICIAL",
            "document_id": "fixture-document",
            "document_sha256": "e" * 64,
        },
    }
    return {**material, "profile_sha256": canonical_sha256(material)}


def _build(
    fuel: dict[str, object],
    m1: dict[str, object],
    context: dict[str, object],
    *,
    rules: dict[str, object] | None,
    previous: dict[str, object] | None = None,
    expected_previous_sha256: str | None = None,
    expected_previous_revision: int | None = None,
) -> dict[str, object]:
    return build_m2_strategy_receipt(
        fuel,
        m1,
        context,
        expected_fuel_replay_sha256=str(fuel["fuel_replay_sha256"]),
        expected_m1_receipt_sha256=str(m1["pit_stint_receipt_sha256"]),
        expected_strategy_context_sha256=str(context["context_sha256"]),
        rules_profile_value=rules,
        expected_rules_profile_sha256=(
            str(rules["profile_sha256"]) if rules is not None else None
        ),
        expected_rules_source_sha256=("e" * 64 if rules is not None else None),
        previous_receipt_value=previous,
        expected_previous_receipt_sha256=expected_previous_sha256,
        expected_previous_revision=expected_previous_revision,
    )


def test_collector_uses_records_digest_as_the_unified_source_content_root(
    collector_upstream,
):
    fuel, m1 = collector_upstream
    context = _context(fuel, m1, calibration=False, traffic=False)

    receipt = _build(fuel, m1, context, rules=None)

    assert receipt["input_binding"]["source_kind"] == "SDK_LIVE"
    assert receipt["input_binding"]["source_sha256"] == "c" * 64
    assert receipt["strategy_context"]["source_binding"]["source_sha256"] == "c" * 64
    assert receipt["recommendations"] == []
    assert receipt["quality_gate"]["status"] == "WAIT_CAPABILITIES"


@pytest.mark.parametrize(
    ("fixture_name", "forbidden_key"),
    (("upstream", "records_sha256"), ("collector_upstream", "source_sha256")),
)
def test_source_kind_specific_digest_alias_injection_fails_closed(
    request,
    fixture_name,
    forbidden_key,
):
    fuel, m1 = copy.deepcopy(request.getfixturevalue(fixture_name))
    context = _context(fuel, m1, calibration=False, traffic=False)
    fuel_evidence = fuel["input_evidence"]
    assert isinstance(fuel_evidence, dict)
    fuel_evidence[forbidden_key] = "f" * 64
    fuel["fuel_replay_sha256"] = canonical_sha256(
        {key: value for key, value in fuel.items() if key != "fuel_replay_sha256"}
    )

    with pytest.raises(M2StrategyReceiptError, match="input_evidence"):
        _build(fuel, m1, context, rules=None)


@pytest.mark.parametrize(
    ("fixture_name", "required_key", "wrong_key"),
    (
        ("upstream", "source_sha256", "records_sha256"),
        ("collector_upstream", "records_sha256", "source_sha256"),
    ),
)
def test_source_digest_missing_or_moved_to_the_other_kind_fails_closed(
    request,
    fixture_name,
    required_key,
    wrong_key,
):
    fuel, m1 = copy.deepcopy(request.getfixturevalue(fixture_name))
    context = _context(fuel, m1, calibration=False, traffic=False)
    fuel_evidence = fuel["input_evidence"]
    assert isinstance(fuel_evidence, dict)
    fuel_evidence[wrong_key] = fuel_evidence.pop(required_key)
    fuel["fuel_replay_sha256"] = canonical_sha256(
        {key: value for key, value in fuel.items() if key != "fuel_replay_sha256"}
    )

    with pytest.raises(M2StrategyReceiptError, match="input_evidence"):
        _build(fuel, m1, context, rules=None)


@pytest.mark.parametrize(
    ("fixture_name", "wrong_input_kind"),
    (("upstream", "collector"), ("collector_upstream", "ibt")),
)
def test_input_kind_cannot_be_crossed_with_the_other_source_kind(
    request,
    fixture_name,
    wrong_input_kind,
):
    fuel, m1 = copy.deepcopy(request.getfixturevalue(fixture_name))
    context = _context(fuel, m1, calibration=False, traffic=False)
    fuel["input_kind"] = wrong_input_kind
    fuel["fuel_replay_sha256"] = canonical_sha256(
        {key: value for key, value in fuel.items() if key != "fuel_replay_sha256"}
    )

    with pytest.raises(M2StrategyReceiptError, match="input_evidence"):
        _build(fuel, m1, context, rules=None)


def test_collector_fuel_and_m1_evidence_must_match_as_exact_objects(
    collector_upstream,
):
    fuel, m1 = copy.deepcopy(collector_upstream)
    context = _context(fuel, m1, calibration=False, traffic=False)
    m1_evidence = m1["input_evidence"]
    assert isinstance(m1_evidence, dict)
    m1_evidence["attacker_field"] = True
    m1["pit_stint_receipt_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in m1.items()
            if key != "pit_stint_receipt_sha256"
        }
    )

    with pytest.raises(
        M2StrategyReceiptError, match="fuel/M1 input evidence objects differ"
    ):
        _build(fuel, m1, context, rules=None)


def test_lap_strategy_contract_is_shadow_only_self_hashed_and_layered(upstream):
    fuel, m1 = upstream
    context = _context(fuel, m1)
    rules = _rules(context["event_identity"])
    receipt = _build(fuel, m1, context, rules=rules)

    assert receipt["quality_gate"] == {
        "reason_codes": [],
        "status": "PASS_SHADOW_CONTRACT",
    }
    assert receipt["advisor_only"] is True
    assert receipt["execution_mode"] == "SHADOW_ONLY"
    assert receipt["attestation_status"] == "NOT_R7_ATTESTED"
    assert receipt["m2_strategy_receipt_sha256"] == canonical_sha256(
        {
            key: value
            for key, value in receipt.items()
            if key != "m2_strategy_receipt_sha256"
        }
    )
    assert receipt["input_binding"]["fuel_replay_sha256"] == fuel["fuel_replay_sha256"]
    assert (
        receipt["input_binding"]["m1_receipt_sha256"] == m1["pit_stint_receipt_sha256"]
    )
    assert receipt["strategy_context"] == context
    assert receipt["rules_binding"]["official_event_rules"] is True
    assert receipt["horizon"]["branches"] == [
        {
            "branch_id": "LAPS_EXACT",
            "condition": "SESSION_LAPS_REMAINING",
            "laps_to_go": 10,
        }
    ]
    assert len(receipt["recommendations"]) == 1
    recommendation = receipt["recommendations"][0]
    assert recommendation["executable"] is False
    assert recommendation["status"] == "SHADOW_ONLY"
    assert recommendation["expected_gain_range_s"] is None
    assert receipt["lifecycle"]["events"][0]["event"] == "ISSUE"
    assert receipt["lifecycle"]["state_revision"] == 1


def test_v2_rule_mandated_tire_change_passes_without_a_performance_model(upstream):
    fuel, m1 = upstream
    context = _context_v2(fuel, m1, tire_model=None)
    rules = _rules(context["event_identity"])
    rules["official_rules"].update(
        {
            "no_tire_service_allowed": False,
            "tire_change_required": True,
        }
    )
    rules["profile_sha256"] = canonical_sha256(
        {key: value for key, value in rules.items() if key != "profile_sha256"}
    )

    receipt = _build(fuel, m1, context, rules=rules)

    assert receipt["contract_version"] == "offline-m2-strategy-receipt-v2"
    assert receipt["tire_strategy"] == {
        "belief": None,
        "change_tires": True,
        "reason_codes": [],
        "status": "PASS_RULE_MANDATED_TIRE_CHANGE",
    }
    assert receipt["capabilities"]["tire_strategy"] == {
        "reason_codes": [],
        "status": "PASS_RULE_MANDATED_TIRE_CHANGE",
    }
    assert receipt["recommendations"][0]["action"]["change_tires"] is True
    assert receipt["quality_gate"]["status"] == "PASS_SHADOW_CONTRACT"


def test_v2_optional_tires_without_model_waits_and_cannot_issue_no_tire_action(
    upstream,
):
    fuel, m1 = upstream
    context = _context_v2(fuel, m1, tire_model=None)

    receipt = _build(
        fuel,
        m1,
        context,
        rules=_rules(context["event_identity"]),
    )

    assert receipt["tire_strategy"] == {
        "belief": None,
        "change_tires": None,
        "reason_codes": ["TIRE_PERFORMANCE_MODEL_REQUIRED"],
        "status": "WAIT_TIRE_PERFORMANCE_MODEL",
    }
    assert receipt["recommendations"] == []
    assert "WAIT_TIRE_PERFORMANCE_MODEL" in receipt["quality_gate"]["reason_codes"]


def test_v2_positive_model_can_select_tires_and_binds_action_belief(upstream):
    fuel, m1 = upstream
    identity = _identity()
    model = _tire_performance_model(identity)
    context = _context_v2(fuel, m1, tire_model=model)
    rules = _rules(context["event_identity"])
    rules["official_rules"]["fuel_tire_service_timing"] = "CONCURRENT"
    rules["profile_sha256"] = canonical_sha256(
        {key: value for key, value in rules.items() if key != "profile_sha256"}
    )

    receipt = _build(fuel, m1, context, rules=rules)

    tire_strategy = receipt["tire_strategy"]
    belief = tire_strategy["belief"]
    assert tire_strategy["status"] == "PASS_MODEL_SELECTED_TIRE_CHANGE"
    assert tire_strategy["change_tires"] is True
    assert belief["status"] == "PASS_SHADOW_CHANGE_TIRES"
    assert belief["scenario"]["fuel_tire_service_timing"] == "PARALLEL"
    assert belief["physical_wear"] == _M2._TIRE_PHYSICAL_WEAR_UNAVAILABLE
    scenario = belief["scenario"]
    tire_stint = context["tire_stint_context"]
    calibration = context["calibration_model"]
    assert isinstance(scenario, dict)
    assert isinstance(tire_stint, dict)
    assert isinstance(calibration, dict)
    assert belief == build_tire_performance_belief(
        model,
        calibration,
        expected_model_sha256=str(model["model_sha256"]),
        expected_model_source_receipt_sha256=str(model["source_receipt_sha256"]),
        expected_calibration_model_sha256=str(calibration["model_sha256"]),
        expected_calibration_source_receipt_sha256=str(
            calibration["source_receipt_sha256"]
        ),
        expected_identity_sha256=str(model["identity_sha256"]),
        current_stint_context_sha256=str(tire_stint["context_sha256"]),
        current_source_receipt_sha256=str(tire_stint["source_receipt_sha256"]),
        current_stint_age_laps=int(tire_stint["stint_age_completed_laps"]),
        current_tire_compound=int(tire_stint["current_tire_compound"]),
        laps_until_pit=int(scenario["laps_until_pit"]),
        laps_after_pit=int(scenario["laps_after_pit"]),
        fuel_add_l=float(scenario["fuel_add_l"]),
        fuel_tire_service_timing=str(scenario["fuel_tire_service_timing"]),
    )
    recommendation = receipt["recommendations"][0]
    assert recommendation["action"]["change_tires"] is True
    assert "tire_strategy_semantic_sha256" in recommendation["recommendation_basis"]
    assert recommendation["evidence_ids"][-1] == (
        f"tire-strategy:{canonical_sha256(tire_strategy)}"
    )


def test_v2_keep_tire_performance_preference_stays_blocked_by_physical_wear(
    upstream,
):
    fuel, m1 = upstream
    identity = _identity()
    model = _tire_performance_model(
        identity,
        slope=0.01,
        slope_range=(0.009, 0.011),
    )
    context = _context_v2(fuel, m1, tire_model=model)

    receipt = _build(
        fuel,
        m1,
        context,
        rules=_rules(context["event_identity"]),
    )

    tire_strategy = receipt["tire_strategy"]
    assert tire_strategy["status"] == "WAIT_PHYSICAL_WEAR_FOR_NO_TIRE_SERVICE"
    assert tire_strategy["change_tires"] is None
    assert tire_strategy["belief"]["performance_preference"] == "KEEP_TIRES"
    assert tire_strategy["belief"]["physical_wear"] == (
        _M2._TIRE_PHYSICAL_WEAR_UNAVAILABLE
    )
    assert receipt["recommendations"] == []


def test_v2_rejects_rehashed_tire_model_and_wear_boundary_tampering(upstream):
    fuel, m1 = upstream
    model = _tire_performance_model(_identity())
    context = _context_v2(fuel, m1, tire_model=model)
    tampered_model_context = copy.deepcopy(context)
    tampered_model = tampered_model_context["tire_performance_model"]
    assert isinstance(tampered_model, dict)
    tampered_model["identity_sha256"] = "1" * 64
    tampered_model["model_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in tampered_model.items()
            if key != "model_sha256"
        }
    )
    tampered_model_context = _rehash_context(tampered_model_context)
    with pytest.raises(M2StrategyReceiptError, match="identity mismatch"):
        _build(
            fuel,
            m1,
            tampered_model_context,
            rules=_rules(context["event_identity"]),
        )

    tampered_wear_context = copy.deepcopy(context)
    tire_stint = tampered_wear_context["tire_stint_context"]
    assert isinstance(tire_stint, dict)
    tire_stint["physical_wear"]["estimate_available"] = True
    tire_stint["context_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in tire_stint.items()
            if key != "context_sha256"
        }
    )
    tampered_wear_context = _rehash_context(tampered_wear_context)
    with pytest.raises(M2StrategyReceiptError, match="physical-wear claim"):
        _build(
            fuel,
            m1,
            tampered_wear_context,
            rules=_rules(context["event_identity"]),
        )


def test_v2_tire_strategy_continuation_is_cas_validated(upstream):
    fuel, m1 = upstream
    model = _tire_performance_model(_identity())
    first_context = _context_v2(fuel, m1, tire_model=model, decision_tick=100)
    rules = _rules(first_context["event_identity"])
    first = _build(fuel, m1, first_context, rules=rules)
    second_context = _advance_context(first_context, decision_tick=101)

    second = _build(
        fuel,
        m1,
        second_context,
        rules=rules,
        previous=first,
        expected_previous_sha256=first["m2_strategy_receipt_sha256"],
        expected_previous_revision=1,
    )

    assert second["lifecycle"]["state_revision"] == 2
    assert second["lifecycle"]["previous_state_sha256"] == (
        first["m2_strategy_receipt_sha256"]
    )
    assert second["lifecycle"]["events"] == [
        {
            "event": "NO_CHANGE",
            "reason_codes": ["ACTIVE_STRATEGY_UNCHANGED"],
            "recommendation_id": first["lifecycle"]["active_recommendation_id"],
        }
    ]


def test_matched_calibration_builder_output_passes_the_m2_gate(upstream):
    fuel, m1 = upstream
    context = _context(fuel, m1)
    identity = context["event_identity"]
    assert isinstance(identity, dict)
    dataset_material: dict[str, object] = {
        "contract_version": MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION,
        "dataset_id": "m2-gate-integration-fixture",
        "dataset_version": 1,
        "event_identity": identity,
        "samples": [
            {
                "fuel_delivered_l": 30.0,
                "fuel_service_elapsed_s": 15.0,
                "label_receipt_sha256": "1" * 64,
                "matched_track_segment_elapsed_s": 30.0,
                "pit_road_elapsed_s": 60.0,
                "sample_id": "stop-1",
                "source_receipt_sha256": "4" * 64,
                "stationary_service_elapsed_s": 20.0,
                "tire_change_elapsed_s": 18.0,
            },
            {
                "fuel_delivered_l": 32.0,
                "fuel_service_elapsed_s": 16.0,
                "label_receipt_sha256": "2" * 64,
                "matched_track_segment_elapsed_s": 31.0,
                "pit_road_elapsed_s": 63.0,
                "sample_id": "stop-2",
                "source_receipt_sha256": "5" * 64,
                "stationary_service_elapsed_s": 21.0,
                "tire_change_elapsed_s": 19.0,
            },
            {
                "fuel_delivered_l": 29.0,
                "fuel_service_elapsed_s": 14.5,
                "label_receipt_sha256": "3" * 64,
                "matched_track_segment_elapsed_s": 32.0,
                "pit_road_elapsed_s": 61.0,
                "sample_id": "stop-3",
                "source_receipt_sha256": "6" * 64,
                "stationary_service_elapsed_s": 19.0,
                "tire_change_elapsed_s": 17.0,
            },
        ],
    }
    dataset = {
        **dataset_material,
        "dataset_sha256": canonical_sha256(dataset_material),
    }
    context["calibration_model"] = build_matched_pit_calibration_model(
        dataset,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )
    context = _rehash_context(context)

    receipt = _build(fuel, m1, context, rules=_rules(identity))

    assert receipt["capabilities"]["pit_loss_calibration"]["status"] == (
        "PASS_CALIBRATED"
    )
    assert receipt["capabilities"]["service_labels"]["status"] == (
        "PASS_SERVICE_LABELS"
    )
    assert receipt["calibration"]["calibrated_model"] == context["calibration_model"]
    assert len(receipt["recommendations"]) == 1


def test_direct_traffic_observation_is_retained_but_cannot_claim_rejoin(upstream):
    fuel, m1 = upstream
    context = _context(fuel, m1)
    traffic = context["traffic_rejoin"]
    assert isinstance(traffic, dict)
    traffic.update(
        {
            "estimate_available": False,
            "motion_context": None,
            "motion_context_sha256": None,
            "rejoin_gap_range_s": None,
            "status": "OBSERVED_ONLY_WAIT_REJOIN_MODEL",
        }
    )
    traffic["traffic_sha256"] = canonical_sha256(
        {key: value for key, value in traffic.items() if key != "traffic_sha256"}
    )
    context = _rehash_context(context)
    rules = _rules(context["event_identity"])

    receipt = _build(fuel, m1, context, rules=rules)

    assert receipt["recommendations"] == []
    assert receipt["traffic_rejoin"] == {
        "estimate": None,
        "input": traffic,
        "status": "WAIT_REJOIN_ESTIMATE",
    }
    assert receipt["capabilities"]["traffic_data"] == {
        "reason_codes": [
            "REJOIN_ESTIMATOR_REQUIRED"
        ],
        "status": "WAIT_REJOIN_ESTIMATE",
    }
    assert "WAIT_REJOIN_ESTIMATE" in receipt["quality_gate"]["reason_codes"]


@pytest.mark.parametrize(
    ("estimate_available", "status", "gap"),
    [
        (False, "AVAILABLE", None),
        (False, "OBSERVED_ONLY_WAIT_PIT_LOSS", None),
        (False, "OBSERVED_ONLY_WAIT_REJOIN_MODEL", [2.0, 3.0]),
        (False, "OBSERVED_ONLY_WAIT_ACTION_BOUND_REJOIN", [2.0, 3.0]),
        (True, "OBSERVED_ONLY_WAIT_ACTION_BOUND_REJOIN", [2.0, 3.0]),
    ],
)
def test_traffic_observation_and_rejoin_claim_cannot_be_crossed(
    upstream,
    estimate_available: bool,
    status: str,
    gap: object,
):
    fuel, m1 = upstream
    context = _context(fuel, m1)
    traffic = context["traffic_rejoin"]
    assert isinstance(traffic, dict)
    traffic.update(
        {
            "estimate_available": estimate_available,
            "rejoin_gap_range_s": gap,
            "status": status,
        }
    )
    traffic["traffic_sha256"] = canonical_sha256(
        {key: value for key, value in traffic.items() if key != "traffic_sha256"}
    )
    context = _rehash_context(context)

    with pytest.raises(M2StrategyReceiptError, match="traffic identity/status"):
        _build(fuel, m1, context, rules=_rules(context["event_identity"]))


def test_timed_horizon_carries_base_and_one_more_and_sizes_for_worst_branch(upstream):
    fuel, m1 = upstream
    context = _context(fuel, m1, horizon_kind="TIMED")
    rules = _rules(context["event_identity"], finish_rule="TIMED_LEADER_CROSSING")
    receipt = _build(fuel, m1, context, rules=rules)

    assert [branch["branch_id"] for branch in receipt["horizon"]["branches"]] == [
        "BASE",
        "ONE_MORE",
    ]
    assert [branch["laps_to_go"] for branch in receipt["horizon"]["branches"]] == [
        10,
        11,
    ]
    assert receipt["capabilities"]["one_more_lap"]["status"] == "PASS_BRANCH_SET"
    basis = receipt["recommendations"][0]["recommendation_basis"]
    assert "traffic_semantic_sha256" in basis
    assert receipt["recommendations"][0]["action"]["fuel_add_l"] > 20.0


def test_calibrated_concurrent_tire_service_duration_is_exact(upstream):
    fuel, m1 = upstream
    context = _context(fuel, m1)
    rules = _rules(context["event_identity"])
    official = rules["official_rules"]
    official.update(
        {
            "fuel_tire_service_timing": "CONCURRENT",
            "no_tire_service_allowed": False,
            "tire_change_required": True,
        }
    )
    rules["profile_sha256"] = canonical_sha256(
        {key: value for key, value in rules.items() if key != "profile_sha256"}
    )
    receipt = _build(fuel, m1, context, rules=rules)
    action = receipt["recommendations"][0]["action"]
    assert action["change_tires"] is True
    assert action["estimated_stationary_service_s"] == 30.0
    assert action["estimated_total_pit_loss_s"] == 55.0


def test_multi_stop_rule_is_an_explicit_unsupported_wait(upstream):
    fuel, m1 = upstream
    context = _context(fuel, m1)
    rules = _rules(context["event_identity"])
    rules["official_rules"]["minimum_pit_stops"] = 2
    rules["profile_sha256"] = canonical_sha256(
        {key: value for key, value in rules.items() if key != "profile_sha256"}
    )
    receipt = _build(fuel, m1, context, rules=rules)
    assert receipt["recommendations"] == []
    assert receipt["capabilities"]["strategy_data"] == {
        "reason_codes": ["MULTI_STOP_RULE_NOT_SUPPORTED"],
        "status": "WAIT_STRATEGY_DATA",
    }


def test_missing_real_style_evidence_is_deterministic_wait_with_required_codes(
    upstream,
):
    fuel, m1 = upstream
    identity = _identity(official=False)
    identity["car_class_id"] = None
    context = _context(
        fuel,
        m1,
        identity=identity,
        horizon_kind="TIMED",
        calibration=False,
        traffic=False,
        pits_open=None,
        penalty_state=None,
    )
    horizon = context["horizon"]
    assert isinstance(horizon, dict)
    horizon.update(
        {
            "leader_eta_to_next_crossing_s": None,
            "player_is_leader": False,
            "reference_lap_time_s": None,
            "time_remaining_s": -1.0,
        }
    )
    context = _rehash_context(context)

    first = _build(fuel, m1, context, rules=None)
    second = _build(fuel, m1, context, rules=None)
    statuses = {item["status"] for item in first["capabilities"].values()}
    assert {
        "WAIT_EVENT_RULES_IDENTITY",
        "WAIT_ONE_MORE_LAP_DATA",
        "WAIT_MATCHED_PIT_LOSS_BASELINE",
        "WAIT_SERVICE_LABELS",
        "WAIT_TRAFFIC_DATA",
        "WAIT_PIT_OPEN_AND_PENALTY_STATE",
    } <= statuses
    assert first == second
    assert first["recommendations"] == []
    assert first["quality_gate"]["status"] == "WAIT_CAPABILITIES"
    assert first["lifecycle"]["events"] == [
        {
            "event": "NO_CHANGE",
            "reason_codes": ["NO_ACTIVE_RECOMMENDATION"],
            "recommendation_id": None,
        }
    ]


def test_rules_official_status_comes_from_exact_selector_and_independent_source(
    upstream,
):
    fuel, m1 = upstream
    # The observed session flag is descriptive only; it cannot promote or
    # demote the independently attested official rule source.
    context = _context(fuel, m1, identity=_identity(official=False))
    rules = _rules(context["event_identity"])
    receipt = _build(fuel, m1, context, rules=rules)
    assert receipt["event_identity"]["official"] is False
    assert receipt["rules_binding"]["official_event_rules"] is True
    assert receipt["rules_binding"]["exact_selector_match"] is True

    promoted = copy.deepcopy(rules)
    promoted["official_event_rules"] = True
    promoted["profile_sha256"] = canonical_sha256(
        {key: value for key, value in promoted.items() if key != "profile_sha256"}
    )
    with pytest.raises(M2StrategyReceiptError, match="rules profile keys"):
        _build(fuel, m1, context, rules=promoted)

    with pytest.raises(M2StrategyReceiptError, match="source digest mismatch"):
        build_m2_strategy_receipt(
            fuel,
            m1,
            context,
            expected_fuel_replay_sha256=str(fuel["fuel_replay_sha256"]),
            expected_m1_receipt_sha256=str(m1["pit_stint_receipt_sha256"]),
            expected_strategy_context_sha256=str(context["context_sha256"]),
            rules_profile_value=rules,
            expected_rules_profile_sha256=str(rules["profile_sha256"]),
            expected_rules_source_sha256="f" * 64,
        )


def test_selector_mismatch_is_wait_not_a_recommendation(upstream):
    fuel, m1 = upstream
    context = _context(fuel, m1)
    rules = _rules(context["event_identity"])
    rules["selector"]["track_id"] = 99
    rules["profile_sha256"] = canonical_sha256(
        {key: value for key, value in rules.items() if key != "profile_sha256"}
    )
    receipt = _build(fuel, m1, context, rules=rules)
    assert receipt["capabilities"]["event_rules_identity"]["status"] == (
        "WAIT_EVENT_RULES_IDENTITY"
    )
    assert receipt["rules_binding"]["reason_codes"] == ["EVENT_SELECTOR_MISMATCH"]
    assert receipt["recommendations"] == []


@pytest.mark.parametrize("target", ["fuel", "m1", "context"])
def test_independent_digest_and_self_hash_fail_closed(upstream, target):
    fuel, m1 = copy.deepcopy(upstream)
    context = _context(fuel, m1)
    rules = _rules(context["event_identity"])
    if target == "fuel":
        fuel["input_evidence"]["source_id"] = "tampered"
    elif target == "m1":
        m1["input_evidence"]["source_id"] = "tampered"
    else:
        context["observation"]["decision_tick"] = 999
    with pytest.raises(M2StrategyReceiptError):
        _build(fuel, m1, context, rules=rules)


@pytest.mark.parametrize(
    ("lineage", "expected_message"),
    [
        ("source_id", "fuel/M1 source_id mismatch"),
        ("session_id", "fuel/M1 session_id mismatch"),
        ("source_kind", "fuel/M1 source_kind mismatch"),
        ("source_sha256", "fuel/M1 source_sha256 mismatch"),
        ("normalized_samples_sha256", "fuel/M1 normalized_samples_sha256 mismatch"),
        ("event_receipt_sha256", "fuel/M1 event_receipt_sha256 mismatch"),
        ("sample_count", "fuel/M1 sample count mismatch"),
    ],
)
def test_cross_receipt_lineage_mismatch_is_rejected_even_after_m1_rehash(
    upstream, lineage, expected_message
):
    fuel, m1 = copy.deepcopy(upstream)
    if lineage in {"source_id", "session_id", "source_kind", "source_sha256"}:
        m1["input_evidence"][lineage] = (
            "f" * 64 if lineage == "source_sha256" else f"different-{lineage}"
        )
    elif lineage == "normalized_samples_sha256":
        m1["normalized_input_receipt"]["samples_sha256"] = "f" * 64
    elif lineage == "event_receipt_sha256":
        m1["upstream_event_receipt"]["receipt_sha256"] = "f" * 64
    else:
        m1["normalized_input_receipt"]["sample_count"] += 1
    m1["pit_stint_receipt_sha256"] = canonical_sha256(
        {key: value for key, value in m1.items() if key != "pit_stint_receipt_sha256"}
    )
    context = _context(fuel, upstream[1])
    rules = _rules(context["event_identity"])
    with pytest.raises(M2StrategyReceiptError, match=expected_message):
        _build(fuel, m1, context, rules=rules)


def test_m1_numbers_remain_observed_sample_only(upstream):
    fuel, m1 = upstream
    context = _context(fuel, m1)
    receipt = _build(fuel, m1, context, rules=_rules(context["event_identity"]))
    observed = receipt["calibration"]["observed_m1"]
    assert observed["status"] == "OBSERVED_SAMPLE_ONLY"
    assert observed["pit_road_elapsed_s"] == [0.5]
    assert observed["stall_elapsed_s"] == [0.233333]
    assert observed["service_active_elapsed_s"] == [0.166667]
    assert observed["observed_net_tank_changes"][0]["value_l"] == 20.0
    assert observed["pit_lane_loss_s"] is None
    assert observed["refuel_rate_l_per_s"] is None
    assert observed["service_content_model"] is None


@pytest.mark.parametrize(
    ("kind", "value"),
    [("LAPS", 32767), ("TIMED", -1.0)],
)
def test_sdk_sentinels_fail_to_wait_instead_of_becoming_distance(kind, value, upstream):
    fuel, m1 = upstream
    context = _context(fuel, m1, horizon_kind=kind)
    horizon = context["horizon"]
    assert isinstance(horizon, dict)
    if kind == "LAPS":
        horizon["laps_remaining"] = value
        rules = _rules(context["event_identity"])
    else:
        horizon["time_remaining_s"] = value
        rules = _rules(context["event_identity"], finish_rule="TIMED_LEADER_CROSSING")
    context = _rehash_context(context)
    receipt = _build(fuel, m1, context, rules=rules)
    assert receipt["capabilities"]["one_more_lap"]["status"] == (
        "WAIT_ONE_MORE_LAP_DATA"
    )
    assert receipt["recommendations"] == []


def test_lifecycle_is_monotonic_reuses_semantic_id_and_revokes_on_dynamic_gate(
    upstream,
):
    fuel, m1 = upstream
    context_1 = _context(fuel, m1, decision_tick=100)
    rules = _rules(context_1["event_identity"])
    first = _build(fuel, m1, context_1, rules=rules)

    context_2 = _advance_context(context_1, decision_tick=101)
    second = _build(
        fuel,
        m1,
        context_2,
        rules=rules,
        previous=first,
        expected_previous_sha256=first["m2_strategy_receipt_sha256"],
        expected_previous_revision=1,
    )
    assert second["lifecycle"]["state_revision"] == 2
    assert (
        second["lifecycle"]["previous_state_sha256"]
        == first["m2_strategy_receipt_sha256"]
    )
    assert second["lifecycle"]["events"] == [
        {
            "event": "NO_CHANGE",
            "reason_codes": ["ACTIVE_STRATEGY_UNCHANGED"],
            "recommendation_id": first["lifecycle"]["active_recommendation_id"],
        }
    ]

    stale_context = _advance_context(context_2, decision_tick=102, stale=True)
    revoked = _build(
        fuel,
        m1,
        stale_context,
        rules=rules,
        previous=second,
        expected_previous_sha256=second["m2_strategy_receipt_sha256"],
        expected_previous_revision=2,
    )
    assert revoked["recommendations"] == []
    assert revoked["lifecycle"]["state_revision"] == 3
    assert revoked["lifecycle"]["events"][0]["event"] == "REVOKE"
    assert (
        "WAIT_PIT_OPEN_AND_PENALTY_STATE"
        in revoked["lifecycle"]["events"][0]["reason_codes"]
    )


@pytest.mark.parametrize(
    ("pits_open", "penalty_state", "expected_status", "expected_reason"),
    [
        (False, "CLEAR", "BLOCKED_PITS_CLOSED", "PITS_CLOSED"),
        (True, "ACTIVE", "BLOCKED_ACTIVE_PENALTY", "ACTIVE_PENALTY"),
    ],
)
def test_closed_pits_and_active_penalty_each_revoke_an_active_candidate(
    upstream, pits_open, penalty_state, expected_status, expected_reason
):
    fuel, m1 = upstream
    context = _context(fuel, m1, decision_tick=100)
    rules = _rules(context["event_identity"])
    active = _build(fuel, m1, context, rules=rules)
    blocked_context = _advance_context(
        context,
        decision_tick=101,
        pits_open=pits_open,
        penalty_state=penalty_state,
    )
    blocked = _build(
        fuel,
        m1,
        blocked_context,
        rules=rules,
        previous=active,
        expected_previous_sha256=active["m2_strategy_receipt_sha256"],
        expected_previous_revision=1,
    )
    assert blocked["recommendations"] == []
    assert blocked["capabilities"]["pit_open_and_penalty_state"]["status"] == (
        expected_status
    )
    assert blocked["capabilities"]["pit_open_and_penalty_state"]["reason_codes"] == [
        expected_reason
    ]
    assert blocked["lifecycle"]["events"][0]["event"] == "REVOKE"


def test_lifecycle_rejects_stale_restore_wrong_cas_and_nonmonotonic_tick(upstream):
    fuel, m1 = upstream
    context = _context(fuel, m1, decision_tick=100)
    rules = _rules(context["event_identity"])
    first = _build(fuel, m1, context, rules=rules)
    later = _advance_context(context, decision_tick=101)

    with pytest.raises(M2StrategyReceiptError, match="optimistic concurrency"):
        _build(
            fuel,
            m1,
            later,
            rules=rules,
            previous=first,
            expected_previous_sha256="f" * 64,
            expected_previous_revision=1,
        )
    with pytest.raises(M2StrategyReceiptError, match="previous revision"):
        _build(
            fuel,
            m1,
            later,
            rules=rules,
            previous=first,
            expected_previous_sha256=first["m2_strategy_receipt_sha256"],
            expected_previous_revision=2,
        )
    with pytest.raises(M2StrategyReceiptError, match="decision tick is not monotonic"):
        _build(
            fuel,
            m1,
            context,
            rules=rules,
            previous=first,
            expected_previous_sha256=first["m2_strategy_receipt_sha256"],
            expected_previous_revision=1,
        )


@pytest.mark.parametrize("attack", ["unknown_lifecycle_key", "raw_lineage_change"])
def test_previous_state_is_fully_revalidated_after_attacker_rehash(upstream, attack):
    fuel, m1 = upstream
    context = _context(fuel, m1, decision_tick=100)
    rules = _rules(context["event_identity"])
    previous = copy.deepcopy(_build(fuel, m1, context, rules=rules))
    if attack == "unknown_lifecycle_key":
        previous["lifecycle"]["attacker_field"] = True
    else:
        previous_input = previous["input_binding"]
        previous_input["source_sha256"] = "f" * 64
        previous_input["input_lineage_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in previous_input.items()
                if key != "input_lineage_sha256"
            }
        )
    previous["m2_strategy_receipt_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in previous.items()
            if key != "m2_strategy_receipt_sha256"
        }
    )
    later = _advance_context(context, decision_tick=101)
    with pytest.raises(M2StrategyReceiptError, match="previous"):
        _build(
            fuel,
            m1,
            later,
            rules=rules,
            previous=previous,
            expected_previous_sha256=previous["m2_strategy_receipt_sha256"],
            expected_previous_revision=1,
        )


def test_changed_fresh_traffic_semantics_revoke_and_issue_with_supersession(upstream):
    fuel, m1 = upstream
    context = _context(fuel, m1, decision_tick=100)
    rules = _rules(context["event_identity"])
    first = _build(fuel, m1, context, rules=rules)
    changed = _advance_context(context, decision_tick=101)
    traffic = changed["traffic_rejoin"]
    assert isinstance(traffic, dict)
    traffic["map_revision_sha256"] = "f" * 64
    motion = traffic["motion_context"]
    assert isinstance(motion, dict)
    motion["traffic_map_revision_sha256"] = "f" * 64
    opponents = motion["opponents"]
    assert isinstance(opponents, list) and isinstance(opponents[0], dict)
    opponents[0]["current_signed_lap_delta"] = 0.6
    motion["motion_sha256"] = canonical_sha256(
        {key: value for key, value in motion.items() if key != "motion_sha256"}
    )
    traffic["motion_context_sha256"] = motion["motion_sha256"]
    traffic["traffic_sha256"] = canonical_sha256(
        {key: value for key, value in traffic.items() if key != "traffic_sha256"}
    )
    changed = _rehash_context(changed)
    second = _build(
        fuel,
        m1,
        changed,
        rules=rules,
        previous=first,
        expected_previous_sha256=first["m2_strategy_receipt_sha256"],
        expected_previous_revision=1,
    )
    assert [event["event"] for event in second["lifecycle"]["events"]] == [
        "REVOKE",
        "ISSUE",
    ]
    old_id = first["lifecycle"]["active_recommendation_id"]
    new_id = second["lifecycle"]["active_recommendation_id"]
    assert old_id != new_id
    assert second["recommendations"][0]["supersedes_id"] == old_id


def test_cli_is_exclusive_and_preserves_non_executable_contract(
    tmp_path: Path, upstream, capfd
):
    fuel, m1 = upstream
    context = _context(fuel, m1)
    rules = _rules(context["event_identity"])
    paths = {
        "fuel": tmp_path / "fuel.json",
        "m1": tmp_path / "m1.json",
        "context": tmp_path / "context.json",
        "rules": tmp_path / "rules.json",
        "output": tmp_path / "receipt.json",
    }
    for name, value in (
        ("fuel", fuel),
        ("m1", m1),
        ("context", context),
        ("rules", rules),
    ):
        paths[name].write_text(json.dumps(value), encoding="utf-8")
    argv = [
        str(paths["fuel"]),
        str(paths["m1"]),
        str(paths["context"]),
        "--expected-fuel-replay-sha256",
        str(fuel["fuel_replay_sha256"]),
        "--expected-m1-receipt-sha256",
        str(m1["pit_stint_receipt_sha256"]),
        "--expected-strategy-context-sha256",
        str(context["context_sha256"]),
        "--rules-profile",
        str(paths["rules"]),
        "--expected-rules-profile-sha256",
        str(rules["profile_sha256"]),
        "--expected-rules-source-sha256",
        "e" * 64,
        "--output",
        str(paths["output"]),
    ]
    assert _M2.main(argv) == 0
    capfd.readouterr()
    persisted = json.loads(paths["output"].read_text(encoding="utf-8"))
    assert persisted["recommendations"][0]["executable"] is False
    assert _M2.main(argv) == 3
    _, error = capfd.readouterr()
    assert "OUTPUT_CREATE_FAILED" in error


PUBLIC_MANIFEST = json.loads(
    Path("data/public_sources.json").read_text(encoding="utf-8")
)
PUBLIC_ASSET = PUBLIC_MANIFEST["assets"][0]
PUBLIC_SAMPLE = Path(PUBLIC_ASSET["local_path"])
requires_public_ibt = pytest.mark.skipif(
    not PUBLIC_SAMPLE.is_file(), reason="REQUIRES_DATA: public Audi/Spa IBT absent"
)


@requires_public_ibt
def test_real_audi_fixture_is_exact_required_wait_and_never_a_recommendation():
    with open_ibt_telemetry(
        PUBLIC_SAMPLE,
        source_id="public-audi-r8-evo2-spa",
        session_id="public-fixture-2023-12-race",
    ) as run:
        fuel = _json_round_trip(
            _build_fuel_model_replay_samples(
                run.samples,
                input_kind="ibt",
                input_evidence=run.evidence,
                tick_rate_hz=run.evidence.tick_rate_hz,
                stale_after_s=run.stale_after_s,
                opponent_error_policy=run.opponent_error_policy,
                scenario=FuelScenario(
                    current_fuel_l=20.0,
                    tank_capacity_l=120.0,
                    refuel_rate_l_per_s=2.0,
                    remaining_laps=10,
                    reserve_l=1.0,
                    minimum_valid_laps=5,
                ),
            )
        )
    assert fuel["fuel_replay_sha256"] == (
        "1f3b642c43dd6b7cd16e433dee3f26335f9aecd0c83950e02b706a2f79c3a65c"
    )
    m1 = json.loads(
        Path("data/derived/audi-spa-offline-pit-stint-v1.json").read_text(
            encoding="utf-8"
        )
    )
    identity = {
        "car_class_id": None,
        "event_type": "Race",
        "official": False,
        "provenance": "SDK_DIRECT_SAME_SOURCE_SESSION_INFO",
        "race_week": 0,
        "season_id": 0,
        "series_id": 0,
        "sim_build": "2023.12.12.04",
        "track_config": "Grand Prix Pit",
        "track_id": 163,
    }
    context = _context(
        fuel,
        m1,
        identity=identity,
        horizon_kind="TIMED",
        decision_tick=332_490,
        calibration=False,
        traffic=False,
        pits_open=True,
        penalty_state=None,
    )
    context["observation"]["laps_completed"] = 17
    context["horizon"].update(
        {
            "laps_remaining": 32767,
            "leader_eta_to_next_crossing_s": None,
            "player_is_leader": False,
            "provenance": "SDK_DIRECT",
            "reference_lap_time_s": None,
            "time_remaining_s": 154.71901666026497,
        }
    )
    context["vehicle_context"]["provenance"] = "USER_RULE"
    context = _rehash_context(context)

    first = _build(fuel, m1, context, rules=None)
    second = _build(fuel, m1, context, rules=None)
    assert first == second
    frozen = json.loads(
        Path("data/derived/audi-spa-offline-m2-strategy-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert first == frozen
    serialized = _M2._canonical_json(first, newline=True)
    assert len(serialized) == 6_060
    assert hashlib.sha256(serialized).hexdigest() == (
        "1726ea834c6c75b794ba17494e529a26387db12b356bcd38d2533cf866ed8cee"
    )
    assert first["m2_strategy_receipt_sha256"] == (
        "72e5265ca6aea84c8d747640bf2cd0a99a2a6430817ccd0163121f4a8a973fb4"
    )
    statuses = {item["status"] for item in first["capabilities"].values()}
    assert {
        "WAIT_EVENT_RULES_IDENTITY",
        "WAIT_ONE_MORE_LAP_DATA",
        "WAIT_MATCHED_PIT_LOSS_BASELINE",
        "WAIT_SERVICE_LABELS",
        "WAIT_TRAFFIC_DATA",
        "WAIT_PIT_OPEN_AND_PENALTY_STATE",
    } <= statuses
    assert first["recommendations"] == []
    observed = first["calibration"]["observed_m1"]
    assert observed["status"] == "OBSERVED_SAMPLE_ONLY"
    assert observed["pit_road_elapsed_s"] == [34.0]
    assert observed["stall_elapsed_s"] == [10.1]
    assert observed["service_active_elapsed_s"] == [9.333333]
    assert observed["observed_net_tank_changes"][0]["value_l"] == 20.995004
