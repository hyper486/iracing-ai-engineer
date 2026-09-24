"""Offline, source-disjoint evaluation of a frozen reviewed tire-age model.

Caller-reviewed identities/conditions are explicit inputs, not SDK or reviewer
authentication. No live owner imports this report or treats it as physical wear.
"""

from __future__ import annotations

import json
import math
import os
import re
from itertools import pairwise
from pathlib import Path
from statistics import mean, median

from .desktop_settings import SettingsStore
from .engineer_session import canonical_sha256
from .live_worker import payload_size
from .retrieved_live_analysis import (
    TirePerformanceError,
    build_tire_performance_model,
    validate_matched_tire_performance_dataset,
)
from .trial_replay import _plain_file

REQUEST_VERSION = "tire-performance-validation-request-v1"
REPORT_VERSION = "tire-performance-validation-report-v1"
MAX_BYTES = 1024**2
MAX_PAIRS = 64
_LAPS = ("early_lap", "late_lap")
_SPLITS = ("training", "holdout")
_ERRORS = frozenset((
    "TIRE_VALIDATION_INVALID", "TIRE_VALIDATION_LIMIT", "TIRE_VALIDATION_PIN_MISMATCH",
    "TIRE_VALIDATION_DATASET_INVALID", "TIRE_VALIDATION_SCOPE_MISMATCH",
    "TIRE_VALIDATION_REUSED_EVIDENCE", "TIRE_VALIDATION_CONTEXT_MISMATCH",
    "TIRE_VALIDATION_IO_FAILED", "TIRE_VALIDATION_OUTPUT_EXISTS",
    "TIRE_VALIDATION_REPORT_MISMATCH",
))


class TireValidationError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in _ERRORS else "TIRE_VALIDATION_INVALID"
        super().__init__(self.code)


def _require(condition, code="TIRE_VALIDATION_INVALID"):
    if not condition:
        raise TireValidationError(code)


def _keys(value, names):
    _require(type(value) is dict and set(value) == set(names.split()))


def _hex(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _integer(value, maximum=2**53):
    return type(value) is int and 0 <= value <= maximum


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def _json_pairs(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def _constant(_):
    raise TireValidationError("TIRE_VALIDATION_INVALID")


def _copy(value):
    try:
        payload_size(value, limit=16 * MAX_BYTES)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        _require(len(encoded) <= MAX_BYTES, "TIRE_VALIDATION_LIMIT")
        return json.loads(encoded, object_pairs_hook=_json_pairs, parse_constant=_constant)
    except TireValidationError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise TireValidationError("TIRE_VALIDATION_INVALID") from exc


def _dataset(value):
    _require(type(value) is dict and type(value.get("samples")) is list)
    _require(3 <= len(value["samples"]) <= MAX_PAIRS, "TIRE_VALIDATION_LIMIT")
    try:
        dataset = validate_matched_tire_performance_dataset(
            value, expected_dataset_sha256=value.get("dataset_sha256"))
    except (TirePerformanceError, TypeError, OverflowError) as exc:
        raise TireValidationError("TIRE_VALIDATION_DATASET_INVALID") from exc
    for sample in dataset["samples"]:
        for name in _LAPS:
            lap = sample[name]
            _require(_integer(lap["stint_age_laps"], 10_000)
                     and _integer(lap["session_tick"]) and _integer(lap["laps_completed"]),
                     "TIRE_VALIDATION_LIMIT")
    return dataset


def _lineage(dataset):
    """Reject renamed copies of one installation/lap even inside one split."""
    sources, receipts, identifiers, coordinates, installations = set(), set(), set(), set(), set()
    intervals = {}
    for sample in dataset["samples"]:
        source = sample["source_receipt_sha256"]
        sources.add(source)
        origin = (source, sample["tire_installation"]["decision_tick"])
        _require(origin not in installations, "TIRE_VALIDATION_REUSED_EVIDENCE")
        installations.add(origin)
        intervals.setdefault(source, []).append((origin[1], sample["late_lap"]["session_tick"]))
        identifiers.update((sample["sample_id"], sample["stint_id"]))
        receipts.update((sample["label_receipt_sha256"], sample["condition_match_receipt_sha256"],
                         sample["tire_installation"]["label_receipt_sha256"]))
        for name in _LAPS:
            lap = sample[name]
            coordinate = (source, lap["session_tick"])
            _require(coordinate not in coordinates, "TIRE_VALIDATION_REUSED_EVIDENCE")
            coordinates.add(coordinate)
            identifiers.add(lap["lap_id"])
    for stints in intervals.values():
        ordered = sorted(stints)
        _require(all(left[1] < right[0] for left, right in pairwise(ordered)),
                 "TIRE_VALIDATION_REUSED_EVIDENCE")
    return sources, receipts, identifiers, coordinates


def _contexts(value, datasets, scope):
    _require(type(value) is list and len(value) == sum(
        len(dataset["samples"]) * 2 for dataset in datasets))
    expected, contexts = set(), {}
    for split, dataset in zip(_SPLITS, datasets, strict=True):
        for sample in dataset["samples"]:
            for name in _LAPS:
                expected.add((split, sample["source_receipt_sha256"], sample[name]["session_tick"]))
    for row in value:
        _keys(row, "split source_receipt_sha256 session_tick car_model_id setup_sha256 "
              "air_temp_c track_temp_c wind_speed_mps wind_direction_rad precipitation_pct "
              "track_state")
        _require(type(row["split"]) is str and row["split"] in _SPLITS
                 and _hex(row["source_receipt_sha256"]) and _integer(row["session_tick"]))
        key = (row["split"], row["source_receipt_sha256"], row["session_tick"])
        _require(key in expected and key not in contexts, "TIRE_VALIDATION_CONTEXT_MISMATCH")
        _require(_integer(row["car_model_id"]) and _hex(row["setup_sha256"])
                 and row["car_model_id"] == scope["car_model_id"]
                 and row["setup_sha256"] == scope["setup_sha256"], "TIRE_VALIDATION_SCOPE_MISMATCH")
        _require(_number(row["air_temp_c"], -50, 60)
                 and _number(row["track_temp_c"], -50, 100)
                 and _number(row["wind_speed_mps"], 0, 100)
                 and _number(row["wind_direction_rad"], 0, math.tau)
                 and _number(row["precipitation_pct"], 0, 100))
        # The first validation contract deliberately excludes wet transitions.
        _require(row["precipitation_pct"] == 0 and row["track_state"] == "REVIEWED_DRY",
                 "TIRE_VALIDATION_SCOPE_MISMATCH")
        contexts[key] = row
    _require(set(contexts) == expected, "TIRE_VALIDATION_CONTEXT_MISMATCH")
    return contexts


def _features(sample, split, contexts):
    result = {}
    for name in _LAPS:
        lap = sample[name]
        row = contexts[(split, sample["source_receipt_sha256"], lap["session_tick"])]
        result[name] = {
            "stint_age_laps": lap["stint_age_laps"], "fuel_start_l": lap["fuel_start_l"],
            "air_temp_c": row["air_temp_c"], "track_temp_c": row["track_temp_c"],
            "wind_x_mps": row["wind_speed_mps"] * math.cos(row["wind_direction_rad"]),
            "wind_y_mps": row["wind_speed_mps"] * math.sin(row["wind_direction_rad"]),
        }
    result["pair_delta"] = {key: result["late_lap"][key] - result["early_lap"][key]
                            for key in result["early_lap"]}
    return result


def _domain(features):
    return {role: {key: [min(row[role][key] for row in features),
                        max(row[role][key] for row in features)] for key in features[0][role]}
            for role in features[0]}


def build_tire_validation_report(value, *, expected_request_sha256):
    """Rebuild only the training model; never fit or widen it using holdout laps."""
    request = _copy(value)
    _keys(request, "contract_version request_sha256 training_model_sha256 training_dataset "
          "holdout_dataset applicability lap_contexts")
    _require(request["contract_version"] == REQUEST_VERSION)
    _require(_hex(expected_request_sha256) and _hex(request["request_sha256"])
             and _hex(request["training_model_sha256"]))
    _require(request["request_sha256"] == expected_request_sha256 == canonical_sha256(
        {k: v for k, v in request.items() if k != "request_sha256"}),
        "TIRE_VALIDATION_PIN_MISMATCH")
    scope = request["applicability"]
    _keys(scope, "car_model_id setup_sha256 review_receipt_sha256")
    _require(_integer(scope["car_model_id"]) and _hex(scope["setup_sha256"])
             and _hex(scope["review_receipt_sha256"]))
    training, holdout = (_dataset(request[f"{split}_dataset"]) for split in _SPLITS)
    _require(all(training[key] == holdout[key] for key in (
        "event_identity", "tire_compound", "fuel_load_model")), "TIRE_VALIDATION_SCOPE_MISMATCH")
    train_sources, train_receipts, train_ids, _ = _lineage(training)
    test_sources, test_receipts, test_ids, _ = _lineage(holdout)
    # Entire capture sources, not just differently named laps, are held out.
    _require(not train_sources & test_sources and not train_receipts & test_receipts
             and not train_ids & test_ids, "TIRE_VALIDATION_REUSED_EVIDENCE")
    # Fuel calibration cannot declare a held-out tire capture as its own source.
    _require(training["fuel_load_model"]["source_receipt_sha256"] not in test_sources,
             "TIRE_VALIDATION_REUSED_EVIDENCE")
    contexts = _contexts(request["lap_contexts"], (training, holdout), scope)
    try:
        model = build_tire_performance_model(
            training, expected_dataset_sha256=training["dataset_sha256"])
    except TirePerformanceError as exc:
        raise TireValidationError("TIRE_VALIDATION_DATASET_INVALID") from exc
    _require(model["model_sha256"] == request["training_model_sha256"],
             "TIRE_VALIDATION_PIN_MISMATCH")
    domain = _domain([_features(sample, "training", contexts) for sample in training["samples"]])
    beta = training["fuel_load_model"]
    rows = []
    for ordinal, sample in enumerate(holdout["samples"], start=1):
        features = _features(sample, "holdout", contexts)
        violations = [f"{role}.{key}" for role in domain
                      for key, (low, high) in domain[role].items()
                      if not low - 1e-9 <= features[role][key] <= high + 1e-9]
        age_delta = features["pair_delta"]["stint_age_laps"]
        fuel_delta = features["pair_delta"]["fuel_start_l"]
        actual = sample["late_lap"]["lap_time_s"] - sample["early_lap"]["lap_time_s"]
        central = (model["performance_age_slope_s_per_lap"] * age_delta
                   + beta["seconds_per_liter"] * fuel_delta)
        endpoints = [slope * age_delta + effect * fuel_delta
                     for slope in model["performance_age_slope_uncertainty_s_per_lap"]
                     for effect in beta["seconds_per_liter_uncertainty"]]
        low, high = min(endpoints), max(endpoints)
        # The 1e-9 epsilon is arithmetic tolerance, not fitted residual allowance.
        inside = low - 1e-9 <= actual <= high + 1e-9
        rows.append({"ordinal": ordinal, "domain_violations": violations,
            "observed_time_delta_s": actual, "predicted_time_delta_s": central,
            "predicted_interval_s": [low, high], "residual_s": actual - central,
            "inside_interval": inside, "eligible": not violations})
    reasons = []
    if not model["estimate_available"]:
        reasons.append("TRAINING_POSITIVE_DEGRADATION_UNAVAILABLE")
    if any(row["domain_violations"] for row in rows):
        reasons.append("HOLDOUT_OUTSIDE_TRAINING_DOMAIN")
    if any(not row["inside_interval"] for row in rows):
        reasons.append("HOLDOUT_PREDICTION_MISS")
    residuals = [row["residual_s"] for row in rows]
    material = {
        "contract_version": REPORT_VERSION, "request_sha256": request["request_sha256"],
        "training_dataset_sha256": training["dataset_sha256"],
        "holdout_dataset_sha256": holdout["dataset_sha256"], "training_model": model,
        "applicability": scope, "training_domain": domain, "holdout_results": rows,
        "summary": {"holdout_pairs": len(rows), "eligible_pairs": sum(r["eligible"] for r in rows),
            "covered_eligible_pairs": sum(r["eligible"] and r["inside_interval"] for r in rows),
            "median_absolute_residual_s": median(map(abs, residuals)),
            "max_absolute_residual_s": max(map(abs, residuals)),
            "root_mean_square_residual_s": math.sqrt(mean(r * r for r in residuals))},
        "status": "WAIT_REVIEWED_HOLDOUT" if reasons else "PASS_REVIEWED_HOLDOUT",
        "reason_codes": reasons, "advisor_only": True, "live_model_admitted": False,
        "live_acceptance": False, "physical_wear_available": False,
        "source_authenticity": "UNVERIFIED_CALLER_REVIEWED_INPUTS",
        "interval_meaning": "EMPIRICAL_ENVELOPE_NOT_STATISTICAL_COVERAGE",
    }
    return {**material, "report_sha256": canonical_sha256(material)}


def verify_tire_validation_report(value, request, *, expected_report_sha256,
                                  expected_request_sha256):
    """Object-exact reconstruction, including every limitation and failed pair."""
    actual = _copy(value)
    rebuilt = build_tire_validation_report(request, expected_request_sha256=expected_request_sha256)
    _require(_hex(expected_report_sha256) and rebuilt["report_sha256"] == expected_report_sha256
             and canonical_sha256(actual) == canonical_sha256(rebuilt),
             "TIRE_VALIDATION_REPORT_MISMATCH")
    return rebuilt


def _read_json(path):
    try:
        with _plain_file(path, MAX_BYTES) as (handle, size):
            payload = handle.read(size + 1)
            _require(len(payload) == size, "TIRE_VALIDATION_IO_FAILED")
        return json.loads(payload.decode("utf-8"), object_pairs_hook=_json_pairs,
                          parse_constant=_constant)
    except TireValidationError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise TireValidationError("TIRE_VALIDATION_IO_FAILED") from exc


def write_tire_validation_report(request_path, output_path, *, expected_request_sha256):
    """Bounded private input, CreateNew output, flushed/exact named-file readback.

    Invalid requests create no output. An I/O failure may leave an incomplete
    new file, but never a success return or an overwritten existing file.
    """
    request = _read_json(request_path)
    report = build_tire_validation_report(request, expected_request_sha256=expected_request_sha256)
    try:
        output = Path(os.path.abspath(output_path))
        store = SettingsStore(output.parent)
        store._check_root(create=True)
        payload = (json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True,
                              indent=2) + "\n").encode("utf-8")
        _require(len(payload) <= MAX_BYTES, "TIRE_VALIDATION_LIMIT")
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        for name in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW"):
            flags |= getattr(os, name, 0)
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "w+b") as handle:
            _require(handle.write(payload) == len(payload), "TIRE_VALIDATION_IO_FAILED")
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
            _require(handle.read(len(payload) + 1) == payload, "TIRE_VALIDATION_IO_FAILED")
        store._check_root()
        verify_tire_validation_report(_read_json(output), request,
            expected_request_sha256=expected_request_sha256,
            expected_report_sha256=report["report_sha256"])
    except TireValidationError:
        raise
    except FileExistsError as exc:
        raise TireValidationError("TIRE_VALIDATION_OUTPUT_EXISTS") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise TireValidationError("TIRE_VALIDATION_IO_FAILED") from exc
    return report
