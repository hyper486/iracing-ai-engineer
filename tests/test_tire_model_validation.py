"""Invented dry stints and review declarations; no authentic SDK/service evidence."""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path

import pytest
from test_tire_performance_model import _dataset, _rehash_dataset

from iracing_ai_engineer.cli import main
from iracing_ai_engineer.engineer_session import canonical_sha256
from iracing_ai_engineer.retrieved_live_analysis import build_tire_performance_model
from iracing_ai_engineer.tire_model_validation import (
    MAX_BYTES,
    MAX_PAIRS,
    REQUEST_VERSION,
    TireValidationError,
    build_tire_validation_report,
    verify_tire_validation_report,
    write_tire_validation_report,
)


def pin(request, *, model=True):
    for split in ("training", "holdout"):
        _rehash_dataset(request[f"{split}_dataset"])
    if model:
        training = request["training_dataset"]
        request["training_model_sha256"] = build_tire_performance_model(
            training, expected_dataset_sha256=training["dataset_sha256"])["model_sha256"]
    request["request_sha256"] = canonical_sha256(
        {k: v for k, v in request.items() if k != "request_sha256"})
    return request


@pytest.fixture
def request_value():
    training, holdout = _dataset(), _dataset()
    holdout["dataset_id"] = "synthetic-held-out-stints"
    for i, sample in enumerate(holdout["samples"]):
        for key in ("sample_id", "stint_id"):
            sample[key] = "holdout-" + sample[key]
        for key in ("condition_match_receipt_sha256", "label_receipt_sha256",
                    "source_receipt_sha256"):
            sample[key] = canonical_sha256(["invented holdout", i, key])
        sample["tire_installation"]["label_receipt_sha256"] = canonical_sha256(["origin", i])
        for name in ("early_lap", "late_lap"):
            sample[name]["lap_id"] = "holdout-" + sample[name]["lap_id"]
    scope = {"car_model_id": 123, "setup_sha256": "a" * 64,
             "review_receipt_sha256": "b" * 64}
    contexts = []
    for split, dataset in (("training", training), ("holdout", holdout)):
        for i, sample in enumerate(dataset["samples"]):
            for name in ("early_lap", "late_lap"):
                contexts.append({"split": split,
                    "source_receipt_sha256": sample["source_receipt_sha256"],
                    "session_tick": sample[name]["session_tick"],
                    "car_model_id": scope["car_model_id"],
                    "setup_sha256": scope["setup_sha256"], "air_temp_c": 20 + i,
                    "track_temp_c": 30 + i, "wind_speed_mps": 2 + i,
                    "wind_direction_rad": 1.0, "precipitation_pct": 0,
                    "track_state": "REVIEWED_DRY"})
    return pin({"contract_version": REQUEST_VERSION, "training_dataset": training,
                "holdout_dataset": holdout, "applicability": scope, "lap_contexts": contexts})


def build(request):
    return build_tire_validation_report(request, expected_request_sha256=request["request_sha256"])


def write_request(request, tmp_path):
    path = tmp_path / "synthetic-request.json"
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return path


def test_frozen_training_model_and_exact_reconstruction(request_value):
    original = copy.deepcopy(request_value)
    report = build(request_value)
    assert request_value == original
    training = request_value["training_dataset"]
    assert report["training_model"] == build_tire_performance_model(
        training, expected_dataset_sha256=training["dataset_sha256"])
    assert report["status"] == "PASS_REVIEWED_HOLDOUT"
    assert report["summary"]["covered_eligible_pairs"] == report["summary"]["holdout_pairs"] == 3
    assert report["reason_codes"] == []
    assert not report["live_model_admitted"] and not report["live_acceptance"]
    assert not report["physical_wear_available"] and report["advisor_only"]
    assert report["source_authenticity"] == "UNVERIFIED_CALLER_REVIEWED_INPUTS"
    assert report["interval_meaning"] == "EMPIRICAL_ENVELOPE_NOT_STATISTICAL_COVERAGE"
    assert verify_tire_validation_report(report, request_value,
        expected_report_sha256=report["report_sha256"],
        expected_request_sha256=request_value["request_sha256"]) == report
    text = json.dumps(report)
    for sample in request_value["holdout_dataset"]["samples"]:
        assert sample["sample_id"] not in text
        assert sample["early_lap"]["lap_id"] not in text


def test_holdout_miss_never_refits_or_widens_model(request_value):
    baseline = build(request_value)
    request_value["holdout_dataset"]["samples"][0]["late_lap"]["lap_time_s"] += 10
    changed = build(pin(request_value))
    assert changed["status"] == "WAIT_REVIEWED_HOLDOUT"
    assert changed["reason_codes"] == ["HOLDOUT_PREDICTION_MISS"]
    assert changed["training_model"] == baseline["training_model"]
    assert changed["training_domain"] == baseline["training_domain"]
    assert changed["summary"]["covered_eligible_pairs"] == 2
    assert changed["summary"]["max_absolute_residual_s"] == pytest.approx(10)
    assert changed["holdout_results"][0]["predicted_interval_s"] == (
        baseline["holdout_results"][0]["predicted_interval_s"])


def test_prediction_uses_signed_fuel_delta_and_all_bound_endpoints(request_value):
    report = build(request_value)
    slope_low, slope_high = report["training_model"]["performance_age_slope_uncertainty_s_per_lap"]
    first = report["holdout_results"][0]
    assert first["predicted_time_delta_s"] == pytest.approx(0.2 * 19 - 0.03 * 40)
    assert first["predicted_interval_s"] == pytest.approx([
        slope_low * 19 - 0.035 * 40, slope_high * 19 - 0.025 * 40])
    assert first["observed_time_delta_s"] == pytest.approx(2.6)


@pytest.mark.parametrize("fault", ["age", "fuel", "temperature", "wind", "pair_delta"])
def test_no_extrapolation_in_age_fuel_conditions_or_pair_changes(request_value, fault):
    sample = request_value["holdout_dataset"]["samples"][0]
    context = request_value["lap_contexts"][6]
    if fault == "age":
        sample["late_lap"].update(stint_age_laps=23, laps_completed=28)
    elif fault == "fuel":
        sample["early_lap"]["fuel_start_l"] = 90
    elif fault == "temperature":
        context["track_temp_c"] = 35
    elif fault == "wind":
        context["wind_direction_rad"] = 1.8
    else:
        # Both endpoint ages are observed, but this shorter pair span is not.
        sample["early_lap"].update(stint_age_laps=2, laps_completed=7)
    report = build(pin(request_value))
    assert "HOLDOUT_OUTSIDE_TRAINING_DOMAIN" in report["reason_codes"]
    assert not report["holdout_results"][0]["eligible"]
    assert report["summary"]["eligible_pairs"] == 2


def test_circular_wind_direction_is_not_a_false_domain_mismatch(request_value):
    for row in request_value["lap_contexts"]:
        row["wind_direction_rad"] = 0 if row["split"] == "training" else math.tau
    assert build(pin(request_value))["status"] == "PASS_REVIEWED_HOLDOUT"


def test_covered_negative_training_signal_is_not_positive_degradation(request_value):
    for split in ("training", "holdout"):
        for sample in request_value[f"{split}_dataset"]["samples"]:
            early, late = sample["early_lap"], sample["late_lap"]
            late["lap_time_s"] = early["lap_time_s"] - 0.2 * (
                late["stint_age_laps"] - early["stint_age_laps"]) + 0.03 * (
                    late["fuel_start_l"] - early["fuel_start_l"])
    report = build(pin(request_value))
    assert report["reason_codes"] == ["TRAINING_POSITIVE_DEGRADATION_UNAVAILABLE"]
    assert report["summary"]["covered_eligible_pairs"] == 3
    assert report["status"] == "WAIT_REVIEWED_HOLDOUT"


@pytest.mark.parametrize("fault", ["source", "label", "condition", "installation", "sample_id",
                                  "stint_id", "lap_id", "fuel_source"])
def test_source_and_review_lineage_disjoint_across_splits(request_value, fault):
    training, holdout = (request_value[f"{split}_dataset"] for split in ("training", "holdout"))
    first, second = training["samples"][0], holdout["samples"][0]
    if fault in ("source", "label", "condition"):
        key = {"source": "source_receipt_sha256", "label": "label_receipt_sha256",
               "condition": "condition_match_receipt_sha256"}[fault]
        second[key] = first[key]
    elif fault == "installation":
        second["tire_installation"]["label_receipt_sha256"] = first["label_receipt_sha256"]
    elif fault in ("sample_id", "stint_id"):
        second[fault] = first[fault]
    elif fault == "lap_id":
        second["early_lap"]["lap_id"] = first["late_lap"]["lap_id"]
    else:
        for dataset in (training, holdout):
            fuel = dataset["fuel_load_model"]
            fuel["source_receipt_sha256"] = second["source_receipt_sha256"]
            fuel["model_sha256"] = canonical_sha256({k: v for k, v in fuel.items()
                                                    if k != "model_sha256"})
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_REUSED_EVIDENCE"):
        build(pin(request_value))


@pytest.mark.parametrize("fault", ["origin", "lap"])
def test_renaming_same_physical_installation_or_lap_inside_split_fails(request_value, fault):
    first, _, third = request_value["training_dataset"]["samples"]
    third["source_receipt_sha256"] = first["source_receipt_sha256"]
    if fault == "origin":
        third["tire_installation"]["decision_tick"] = first["tire_installation"]["decision_tick"]
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_REUSED_EVIDENCE"):
        build(pin(request_value))


def test_same_source_distinct_but_overlapping_stints_are_not_independent(request_value):
    first, second, _ = request_value["training_dataset"]["samples"]
    second["source_receipt_sha256"] = first["source_receipt_sha256"]
    # No identical lap ticks or installation ticks, but service intervals overlap.
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_REUSED_EVIDENCE"):
        build(pin(request_value))


def test_same_training_source_allows_genuinely_separated_stints(request_value):
    first, second, _ = request_value["training_dataset"]["samples"]
    previous_source = second["source_receipt_sha256"]
    second["source_receipt_sha256"] = first["source_receipt_sha256"]
    second["tire_installation"]["decision_tick"] += 10_000
    for name in ("early_lap", "late_lap"):
        second[name]["session_tick"] += 10_000
    for row in request_value["lap_contexts"]:
        if row["split"] == "training" and row["source_receipt_sha256"] == previous_source:
            row["source_receipt_sha256"] = first["source_receipt_sha256"]
            row["session_tick"] += 10_000
    assert build(pin(request_value))["status"] == "PASS_REVIEWED_HOLDOUT"


@pytest.mark.parametrize("fault", ["car", "setup", "identity", "fuel_model", "compound",
                                  "wet", "rain", "boolean_car"])
def test_wrong_car_setup_event_fuel_or_wet_scope_refused(request_value, fault):
    row = request_value["lap_contexts"][6]
    holdout = request_value["holdout_dataset"]
    if fault == "car":
        row["car_model_id"] += 1
    elif fault == "setup":
        row["setup_sha256"] = "c" * 64
    elif fault == "identity":
        holdout["event_identity"]["car_class_id"] += 1
    elif fault == "fuel_model":
        fuel = holdout["fuel_load_model"]
        fuel["seconds_per_liter"] += 0.001
        fuel["model_sha256"] = canonical_sha256({k: v for k, v in fuel.items()
                                                if k != "model_sha256"})
    elif fault == "compound":
        holdout["tire_compound"] = 1
        for sample in holdout["samples"]:
            sample["tire_installation"]["tire_compound"] = 1
    elif fault == "wet":
        row["track_state"] = "WET"
    elif fault == "rain":
        row["precipitation_pct"] = 1
    else:
        row["car_model_id"] = True
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_SCOPE_MISMATCH"):
        build(pin(request_value))


@pytest.mark.parametrize("fault", ["duplicate", "missing", "tick", "extra", "split", "nan"])
def test_context_rows_must_cover_exact_lap_coordinates(request_value, fault):
    rows = request_value["lap_contexts"]
    if fault == "duplicate":
        rows[-1] = copy.deepcopy(rows[0])
    elif fault == "missing":
        rows.pop()
    elif fault == "tick":
        rows[0]["session_tick"] += 1
    elif fault == "extra":
        rows[0]["unbound_note"] = "not admitted"
    elif fault == "split":
        rows[0]["split"] = []
    else:
        rows[0]["air_temp_c"] = float("nan")
    if fault != "nan":
        pin(request_value)
    with pytest.raises(TireValidationError):
        build(request_value)


@pytest.mark.parametrize("fault", ["request", "model", "dataset"])
def test_pins_must_match_independent_input(request_value, fault):
    if fault == "request":
        request_value["request_sha256"] = "0" * 64
    elif fault == "model":
        request_value["training_model_sha256"] = "0" * 64
        pin(request_value, model=False)
    else:
        request_value["holdout_dataset"]["dataset_sha256"] = "0" * 64
        request_value["request_sha256"] = canonical_sha256(
            {k: v for k, v in request_value.items() if k != "request_sha256"})
    with pytest.raises(TireValidationError):
        build(request_value)


@pytest.mark.parametrize("field", ["live_model_admitted", "live_acceptance",
                                  "physical_wear_available",
                                  "advisor_only", "residual", "coverage", "domain", "extra"])
def test_rehashed_report_changes_never_pass_exact_verification(request_value, field):
    report = build(request_value)
    if field == "residual":
        report["holdout_results"][0]["residual_s"] = 42
    elif field == "coverage":
        report["summary"]["covered_eligible_pairs"] = 999
    elif field == "domain":
        report["training_domain"]["early_lap"]["air_temp_c"] = [-50, 60]
    elif field == "extra":
        report["unknown"] = True
    elif field == "advisor_only":
        report[field] = 1  # Python equality must not confuse this with true.
    else:
        report[field] = True
    report["report_sha256"] = canonical_sha256({k: v for k, v in report.items()
                                              if k != "report_sha256"})
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_REPORT_MISMATCH"):
        verify_tire_validation_report(report, request_value,
            expected_report_sha256=report["report_sha256"],
            expected_request_sha256=request_value["request_sha256"])


def test_private_create_new_and_named_file_readback(request_value, tmp_path):
    source = write_request(request_value, tmp_path)
    output = tmp_path / "reports" / "validation.json"
    result = write_tire_validation_report(source, output,
        expected_request_sha256=request_value["request_sha256"])
    assert json.loads(output.read_text("utf-8")) == result == build(request_value)
    before = output.read_bytes()
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_OUTPUT_EXISTS"):
        write_tire_validation_report(source, output,
            expected_request_sha256=request_value["request_sha256"])
    assert output.read_bytes() == before


@pytest.mark.parametrize("bad_bytes", [b'{"key":1,"key":2}', b'{"key":NaN}', b'\xff',
                                     b'[]', b'[' * 2000, b' ' * (MAX_BYTES + 1)],
                         ids=["duplicates", "nonfinite", "encoding", "array", "deep", "oversized"])
def test_invalid_file_has_sanitized_cli_failure_and_no_output(request_value, tmp_path,
                                                            capsys, bad_bytes):
    source = tmp_path / "private-name-that-must-not-be-echoed.json"
    source.write_bytes(bad_bytes)
    output = tmp_path / "absent" / "report.json"
    assert main(["validate-tire-performance", str(source), "--expected-request-sha256",
                 request_value["request_sha256"], "--output", str(output)]) == 2
    captured = capsys.readouterr()
    assert not captured.err and str(source) not in captured.out
    assert json.loads(captured.out)["code"].startswith("TIRE_VALIDATION_")
    assert not output.parent.exists()


@pytest.mark.parametrize("miss", [False, True])
def test_cli_reports_failed_prediction_without_suppressing_report(request_value, tmp_path,
                                                                 capsys, miss):
    if miss:
        request_value["holdout_dataset"]["samples"][0]["late_lap"]["lap_time_s"] += 10
        pin(request_value)
    source = write_request(request_value, tmp_path)
    output = tmp_path / "report.json"
    assert main(["validate-tire-performance", str(source), "--expected-request-sha256",
                 request_value["request_sha256"], "--output", str(output)]) == (2 if miss else 0)
    response = json.loads(capsys.readouterr().out)
    assert not response["live_model_admitted"] and not response["live_acceptance"]
    assert json.loads(output.read_text("utf-8"))["report_sha256"] == response["report_sha256"]


@pytest.mark.parametrize("count", [2, MAX_PAIRS + 1])
def test_pair_count_bounded_before_model_work(request_value, count):
    request_value["training_dataset"]["samples"] = (
        request_value["training_dataset"]["samples"] * (count // 3 + 1))[:count]
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_LIMIT"):
        build(pin(request_value, model=False))


def test_oversized_age_bounded_before_floating_point_model(request_value):
    late = request_value["training_dataset"]["samples"][0]["late_lap"]
    late.update(stint_age_laps=10**1000, laps_completed=5 + 10**1000)
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_LIMIT"):
        build(pin(request_value, model=False))


def test_legacy_numeric_overflow_is_sanitized(request_value):
    request_value["training_dataset"]["samples"][0]["early_lap"]["fuel_start_l"] = 10**1000
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_DATASET_INVALID"):
        build(pin(request_value, model=False))


def test_maximum_pair_count_is_usable_with_bounded_report(request_value):
    old_contexts = request_value["lap_contexts"]
    contexts = []
    for split in ("training", "holdout"):
        dataset = request_value[f"{split}_dataset"]
        original = dataset["samples"]
        samples = []
        for i in range(MAX_PAIRS):
            sample = copy.deepcopy(original[i % 3])
            source = sample["source_receipt_sha256"]
            for key in ("sample_id", "stint_id"):
                sample[key] += f"-{i}"
            for key in ("condition_match_receipt_sha256", "label_receipt_sha256",
                        "source_receipt_sha256"):
                sample[key] = canonical_sha256([split, key, i])
            sample["tire_installation"]["label_receipt_sha256"] = canonical_sha256(
                [split, "set", i])
            for name in ("early_lap", "late_lap"):
                sample[name]["lap_id"] += f"-{i}"
                context = copy.deepcopy(next(row for row in old_contexts if row["split"] == split
                    and row["source_receipt_sha256"] == source
                    and row["session_tick"] == sample[name]["session_tick"]))
                context["source_receipt_sha256"] = sample["source_receipt_sha256"]
                contexts.append(context)
            samples.append(sample)
        dataset["samples"] = samples
    request_value["lap_contexts"] = contexts
    report = build(pin(request_value))
    assert report["status"] == "PASS_REVIEWED_HOLDOUT"
    assert report["summary"]["covered_eligible_pairs"] == MAX_PAIRS
    assert len(json.dumps(report).encode("utf-8")) < MAX_BYTES


def test_public_checkout_output_refused_without_file_creation(request_value, tmp_path):
    source = write_request(request_value, tmp_path)
    output = Path(__file__).resolve().parents[1] / "never-create-private-validation.json"
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_IO_FAILED"):
        write_tire_validation_report(source, output,
            expected_request_sha256=request_value["request_sha256"])
    assert not output.exists()


def test_linked_input_is_refused(request_value, tmp_path):
    source = write_request(request_value, tmp_path)
    alias = tmp_path / "hardlink.json"
    os.link(source, alias)
    output = tmp_path / "report.json"
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_IO_FAILED"):
        write_tire_validation_report(alias, output,
            expected_request_sha256=request_value["request_sha256"])
    assert not output.exists()


def test_changed_named_output_readback_fails_closed(request_value, tmp_path, monkeypatch):
    import iracing_ai_engineer.tire_model_validation as module

    source = write_request(request_value, tmp_path)
    output = tmp_path / "report.json"
    read = module._read_json

    def changed(path):
        value = read(path)
        if path == output:
            value["live_model_admitted"] = True
        return value

    monkeypatch.setattr(module, "_read_json", changed)
    with pytest.raises(TireValidationError, match="TIRE_VALIDATION_REPORT_MISMATCH"):
        write_tire_validation_report(source, output,
            expected_request_sha256=request_value["request_sha256"])
