"""Invented service/phase arithmetic only; no SDK, provider or tire acceptance."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from iracing_ai_engineer.live_queries import live_query_intent, render_live_query
from iracing_ai_engineer.live_rejoin import project_rejoin, validated_rejoin
from iracing_ai_engineer.live_strategy import (
    StrategyParameters,
    project_strategy,
    service_cost_options,
    validated_strategy,
)
from iracing_ai_engineer.llm_engineer import EngineerConfig, EngineerService, fallback_plan
from iracing_ai_engineer.llm_evidence import build_live_context
from iracing_ai_engineer.rejoin_projection import project_phase_rejoin


def fixture(name):
    identifier = f"_service_{name}"
    if identifier not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            identifier, Path(__file__).with_name(f"test_{name}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[identifier] = module
        spec.loader.exec_module(module)
    return sys.modules[identifier]


def parameters(**changes):
    return StrategyParameters(**{
        "tank_capacity_l": 50., "refuel_rate_l_per_s": 2.,
        "pit_loss_low_s": 20., "pit_loss_high_s": 24.,
        "tire_change_time_s": 20., "fuel_tire_service_timing": "PARALLEL",
        "other_service_low_s": 3., "other_service_high_s": 5., **changes})


@pytest.mark.parametrize("timing,add,service,extra", [
    ("PARALLEL", 12., 20., 14.), ("SEQUENTIAL", 12., 26., 20.),
    ("PARALLEL", 60., 30., 0.), ("SEQUENTIAL", 60., 50., 20.),
    ("PARALLEL", 0., 20., 20.), ("SEQUENTIAL", 0., 20., 20.),
    ("PARALLEL", 40., 20., 0.),
])
def test_service_overlap_and_serial_arithmetic(timing, add, service, extra):
    fuel, tires = service_cost_options(parameters(fuel_tire_service_timing=timing), add)
    assert fuel["service_option"] == "FUEL_ONLY"
    assert fuel["stationary_service_time_s"] == add / 2
    assert fuel["complete_loss_range_s"] == [23 + add / 2, 29 + add / 2]
    assert tires["service_option"] == "FOUR_TIRES"
    assert tires["stationary_service_time_s"] == service
    assert tires["extra_tire_time_s"] == extra
    assert tires["partial_loss_range_s"] == [20 + service, 24 + service]
    assert tires["complete_loss_range_s"] == [23 + service, 29 + service]


def test_missing_overhead_is_not_zero_or_a_complete_loss():
    partial = service_cost_options(parameters(other_service_low_s=None,
                                             other_service_high_s=None), 12.)
    complete = service_cost_options(parameters(other_service_low_s=0.,
                                              other_service_high_s=0.), 12.)
    for observed, confirmed in zip(partial, complete, strict=True):
        assert observed["complete_loss_range_s"] is None
        assert confirmed["complete_loss_range_s"] == observed["partial_loss_range_s"]
    assert service_cost_options(StrategyParameters(50.), 12.) == []
    assert len(service_cost_options(StrategyParameters(50., 2.), 12.)) == 1


@pytest.mark.parametrize("changes", [
    {"tire_change_time_s": None}, {"tire_change_time_s": 0},
    {"tire_change_time_s": True}, {"tire_change_time_s": float("nan")},
    {"tire_change_time_s": 601}, {"fuel_tire_service_timing": None},
    {"fuel_tire_service_timing": []}, {"fuel_tire_service_timing": "UNKNOWN"},
    {"refuel_rate_l_per_s": None}, {"pit_loss_low_s": None, "pit_loss_high_s": None},
    {"other_service_low_s": None}, {"other_service_high_s": None},
    {"other_service_low_s": -1}, {"other_service_high_s": 601},
    {"other_service_high_s": float("inf")}, {"other_service_low_s": True},
    {"other_service_low_s": 6}, {"other_service_high_s": "5"},
    {"complete_pit_loss_low_s": 30, "complete_pit_loss_high_s": 35,
     "pit_entry_fraction": .8, "pit_exit_fraction": .05},
    {"pit_entry_fraction": .8},
])
def test_partial_contradictory_and_untrusted_service_parameters_rejected(changes):
    with pytest.raises(ValueError, match="STRATEGY_PARAMETERS_INVALID"):
        parameters(**changes)


def rejoin_evidence(**changes):
    value = fixture("live_rejoin").evidence(amount=6.)
    params = parameters(pit_entry_fraction=.8, pit_exit_fraction=.05, **changes)
    revision = value["strategy_configuration"]["revision"]
    value["strategy_configuration"]["inputs"] = asdict(params)
    value["strategy"] = project_strategy(value["monitor"], value["fuel"], "Race", params, revision)
    value["rejoin"] = project_rejoin(value)
    return value


def test_mapped_service_variants_use_exact_mapped_dose_and_own_loss_ranges():
    value = rejoin_evidence()
    result = validated_rejoin(value)
    assert result is not None and result["status"] == "READY"
    assert len(result["scenarios"]) == 4
    assert result["live_acceptance"] is False and result["executable"] is False
    rows = result["scenarios"]
    assert [(r["endpoint"], r["service_option"]) for r in rows] == [
        (endpoint, option) for endpoint in ("early", "late")
        for option in ("FUEL_ONLY", "FOUR_TIRES")]
    for row in rows:
        assert row["loss_basis"] == "USER_COMPONENT_SUM"
        assert row["arrival_fuel_l"] != value["strategy"]["scenarios"][0]["arrival_fuel_l"]
        expected = service_cost_options(parameters(), row["fuel_add_l"])
        cost = next(r for r in expected if r["service_option"] == row["service_option"])
        assert row["complete_loss_range_s"] == cost["complete_loss_range_s"]
        ahead, behind, reasons = project_phase_rejoin(
            value["motion"], exit_progress_laps=row["exit_progress_laps"],
            loss_range_s=row["complete_loss_range_s"])
        assert (row["ahead"], row["behind"], row["reason_codes"]) == (ahead, behind, reasons)
    assert rows[0]["complete_loss_range_s"] == [36., 42.]
    assert rows[1]["complete_loss_range_s"] == [43., 49.]
    assert rows[0]["ahead"] != rows[1]["ahead"]
    context = build_live_context(value)
    answer = render_live_query(context, "rejoin")
    assert len(answer["fact_ids"]) == 6
    assert "仅补油对照" in answer["text"] and "补油加四轮换胎" in answer["text"]
    assert "不是安全留胎建议" in answer["text"]
    assert "不判断留胎" in answer["spoken_text"]
    assert "rejoin.assumptions" in fallback_plan(context, "解释出站比较")["fact_ids"]
    assert not any(key in json.dumps(context) for key in ("lap_profiles", "car_idx", "binding_sha"))


def test_missing_other_cost_cannot_be_used_for_rejoin_and_fixed_mode_is_not_mixed():
    with pytest.raises(ValueError, match="STRATEGY_PARAMETERS_INVALID"):
        parameters(other_service_low_s=None, other_service_high_s=None,
                   pit_entry_fraction=.8, pit_exit_fraction=.05)
    value = fixture("live_rejoin").evidence(amount=6.)
    params = parameters(other_service_low_s=None, other_service_high_s=None,
                        pit_entry_fraction=.8, pit_exit_fraction=.05,
                        complete_pit_loss_low_s=30., complete_pit_loss_high_s=31.)
    value["strategy_configuration"]["inputs"] = asdict(params)
    value["strategy"] = project_strategy(value["monitor"], value["fuel"], "Race", params,
                                         value["strategy_configuration"]["revision"])
    result = project_rejoin(value)
    assert len(result["scenarios"]) == 2
    assert all(row["service_option"] == "UNSPECIFIED" and
               row["loss_basis"] == "USER_COMPLETE_TOTAL" and
               row["complete_loss_range_s"] == [30., 31.] for row in result["scenarios"])


def test_out_of_bounds_service_rejoin_withdrawn_per_variant():
    value = rejoin_evidence(tire_change_time_s=600.)
    fuel, tires, _, _ = value["rejoin"]["scenarios"]
    assert fuel["status"] == "READY"
    assert tires["status"] == "WAIT" and tires["ahead"] is None
    assert tires["reason_codes"] == ["SERVICE_LOSS_OUT_OF_BOUNDS"]


@pytest.mark.parametrize("change", ["cost", "alias", "extra", "provenance", "mode", "source"])
def test_service_facts_recomputed_and_do_not_accept_projection_tampering(change):
    state, _ = fixture("live_strategy").configured(parameters=parameters())
    value = state.snapshot()
    assert validated_strategy(value) is not None
    options = value["strategy"]["scenarios"][0]["service_options"]
    if change == "cost":
        options[1]["extra_tire_time_s"] += 1
    elif change == "alias":
        options[0]["extra_tire_time_s"] = False
    elif change == "extra":
        options[1]["private"] = "SYNTHETIC_PRIVATE_MARKER"
    elif change == "provenance":
        value["strategy"]["parameters_provenance"] = "SDK_DIRECT"
    elif change == "mode":
        value["strategy_configuration"]["inputs"]["fuel_tire_service_timing"] = "SEQUENTIAL"
    else:
        value["monitor"]["sequence"] += 1
    assert validated_strategy(value) is None
    context = build_live_context(value)
    assert not any(item["id"].startswith("strategy.") for item in context["facts"])
    assert "PRIVATE" not in json.dumps(context)


@pytest.mark.parametrize("question", ["换胎会多花多久", "比较换胎耗时", "tire service time"])
def test_service_questions_are_local_short_and_expire_on_config_change(question):
    state, _ = fixture("live_strategy").configured(parameters=parameters())
    service = EngineerService(state.snapshot, EngineerConfig(provider="deepseek"), environ={})
    try:
        assert live_query_intent(question) == "service"
        assert service.submit(question)[0] == 202
        result = service.snapshot()
        answer = result["answer"]
        assert result["requests_used"] == 0 and answer["origin"] == "local_live"
        assert "14.0 秒" in answer["spoken_text"] and "不是换胎建议" in answer["spoken_text"]
        assert len(answer["spoken_text"]) < 90
        assert "43.0–49.0 秒" in answer["text"]
        state.configure_strategy(parameters(fuel_tire_service_timing="SEQUENTIAL"))
        assert service.snapshot()["answer"]["stale"] is True
    finally:
        service.close(wait=True)


def test_missing_service_settings_are_unavailable_not_zero_seconds():
    state, _ = fixture("live_strategy").configured()
    context = build_live_context(state.snapshot())
    answer = render_live_query(context, "service")
    assert answer["fact_ids"] == [] and "未就绪" in answer["spoken_text"]
    assert live_query_intent("假设换胎10秒，换胎会多花多久") is None
    state.configure_strategy(parameters(other_service_low_s=None, other_service_high_s=None))
    context = build_live_context(state.snapshot())
    answer = render_live_query(context, "service")
    assert "不能把此驻站时间当成完整进站损失" in answer["text"]
    assert not any(item["id"].startswith("rejoin.") for item in context["facts"])


def test_rejoin_tire_variant_cannot_be_relabelled_or_modified():
    value = rejoin_evidence()
    for field, changed in (("service_option", "FUEL_ONLY"),
                           ("complete_loss_range_s", [1., 2.]), ("endpoint", "late")):
        copy_ = copy.deepcopy(value)
        copy_["rejoin"]["scenarios"][1][field] = changed
        assert validated_rejoin(copy_) is None
