from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from iracing_ai_engineer.cli import main as cli_main
from iracing_ai_engineer.retrieved_live_analysis import (
    MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION,
    MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION,
    TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION,
    TIRE_PERFORMANCE_BELIEF_METHOD_VERSION,
    TIRE_PERFORMANCE_METHOD_VERSION,
    TIRE_PERFORMANCE_MODEL_CONTRACT_VERSION,
    TIRE_STINT_CONTEXT_CONTRACT_VERSION,
    RetrievedLiveAnalysisError,
    TirePerformanceError,
    build_matched_pit_calibration_model,
    build_tire_performance_belief,
    build_tire_performance_model,
    validate_matched_tire_performance_dataset,
    validate_tire_performance_belief,
    validate_tire_performance_model,
    validate_tire_stint_context,
    write_tire_performance_model_exclusive,
)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity() -> dict[str, object]:
    return {
        "car_class_id": 27,
        "event_type": "Race",
        "official": True,
        "provenance": "SDK_DIRECT_SAME_SOURCE_CAPTURE",
        "race_week": 12,
        "season_id": 601,
        "series_id": 501,
        "sim_build": "2026.08.31.02",
        "track_config": "Grand Prix Pits",
        "track_id": 101,
    }


def _valid_fuel_load_model() -> dict[str, object]:
    material = {
        "seconds_per_liter": 0.03,
        "seconds_per_liter_uncertainty": [0.025, 0.035],
        "source_receipt_sha256": "f" * 64,
        "status": "CALIBRATED_FUEL_LOAD_EFFECT",
    }
    return {**material, "model_sha256": _sha256(material)}


def _pair(
    index: int,
    *,
    slope: float,
    early_age: int,
    late_age: int,
    early_fuel_l: float,
    late_fuel_l: float,
) -> dict[str, object]:
    early_time = 100.0 + index
    age_delta = late_age - early_age
    fuel_delta = late_fuel_l - early_fuel_l
    late_time = early_time + slope * age_delta + 0.03 * fuel_delta
    return {
        "condition_match_receipt_sha256": f"{index + 3:x}" * 64,
        "early_lap": {
            "fuel_start_l": early_fuel_l,
            "lap_id": f"stint-{index}-early",
            "lap_time_s": early_time,
            "stint_age_laps": early_age,
        },
        "label_receipt_sha256": f"{index:x}" * 64,
        "late_lap": {
            "fuel_start_l": late_fuel_l,
            "lap_id": f"stint-{index}-late",
            "lap_time_s": late_time,
            "stint_age_laps": late_age,
        },
        "sample_id": f"matched-stint-pair-{index}",
        "source_receipt_sha256": f"{index + 6:x}" * 64,
        "stint_id": f"observed-stint-{index}",
    }


def _dataset() -> dict[str, object]:
    material: dict[str, object] = {
        "contract_version": MATCHED_TIRE_PERFORMANCE_DATASET_CONTRACT_VERSION,
        "dataset_id": "synthetic-dry-gt3-tire-performance",
        "dataset_version": 1,
        "event_identity": _identity(),
        "fuel_load_model": _valid_fuel_load_model(),
        "samples": [
            _pair(
                1,
                slope=0.20,
                early_age=1,
                late_age=20,
                early_fuel_l=80.0,
                late_fuel_l=40.0,
            ),
            _pair(
                2,
                slope=0.18,
                early_age=2,
                late_age=21,
                early_fuel_l=78.0,
                late_fuel_l=40.0,
            ),
            _pair(
                3,
                slope=0.22,
                early_age=1,
                late_age=22,
                early_fuel_l=82.0,
                late_fuel_l=40.0,
            ),
        ],
        "tire_compound": 0,
    }
    return {**material, "dataset_sha256": _sha256(material)}


def _rehash_dataset(dataset: dict[str, object]) -> None:
    dataset["dataset_sha256"] = _sha256(
        {key: value for key, value in dataset.items() if key != "dataset_sha256"}
    )


def _pit_calibration() -> dict[str, object]:
    samples = []
    for index, (fuel, fuel_s, tire_s) in enumerate(
        ((30.0, 15.0, 18.0), (32.0, 16.0, 19.0), (29.0, 14.5, 17.0)),
        start=1,
    ):
        samples.append(
            {
                "fuel_delivered_l": fuel,
                "fuel_service_elapsed_s": fuel_s,
                "label_receipt_sha256": f"{index:x}" * 64,
                "matched_track_segment_elapsed_s": 30.0 + index - 1,
                "pit_road_elapsed_s": 60.0 + index - 1,
                "sample_id": f"pit-stop-{index}",
                "source_receipt_sha256": f"{index + 3:x}" * 64,
                "stationary_service_elapsed_s": 20.0 + index - 1,
                "tire_change_elapsed_s": tire_s,
            }
        )
    material: dict[str, object] = {
        "contract_version": MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION,
        "dataset_id": "synthetic-pit-calibration",
        "dataset_version": 1,
        "event_identity": _identity(),
        "samples": samples,
    }
    dataset = {**material, "dataset_sha256": _sha256(material)}
    return build_matched_pit_calibration_model(
        dataset,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )


def _model() -> dict[str, object]:
    dataset = _dataset()
    return build_tire_performance_model(
        dataset,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )


def _belief(**overrides: object) -> dict[str, object]:
    model = _model()
    calibration = _pit_calibration()
    arguments: dict[str, object] = {
        "expected_model_sha256": model["model_sha256"],
        "expected_model_source_receipt_sha256": model["source_receipt_sha256"],
        "expected_calibration_model_sha256": calibration["model_sha256"],
        "expected_calibration_source_receipt_sha256": calibration[
            "source_receipt_sha256"
        ],
        "expected_identity_sha256": model["identity_sha256"],
        "current_stint_context_sha256": "a" * 64,
        "current_source_receipt_sha256": "b" * 64,
        "current_stint_age_laps": 4,
        "current_tire_compound": 0,
        "laps_until_pit": 2,
        "laps_after_pit": 4,
        "fuel_add_l": 20.0,
        "fuel_tire_service_timing": "SEQUENTIAL",
        **overrides,
    }
    return build_tire_performance_belief(
        model,
        calibration,
        **arguments,  # type: ignore[arg-type]
    )


def test_matched_dataset_builds_fuel_adjusted_performance_envelope() -> None:
    dataset = _dataset()
    validated = validate_matched_tire_performance_dataset(
        dataset,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )
    model = build_tire_performance_model(
        dataset,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )

    assert validated == dataset
    assert model == {
        "advisor_only": True,
        "contract_version": TIRE_PERFORMANCE_MODEL_CONTRACT_VERSION,
        "estimate_available": True,
        "fuel_load_model_sha256": dataset["fuel_load_model"]["model_sha256"],  # type: ignore[index]
        "identity_sha256": _sha256(_identity()),
        "independent_stint_count": 3,
        "max_supported_stint_age_laps": 22,
        "method_version": TIRE_PERFORMANCE_METHOD_VERSION,
        "model_sha256": model["model_sha256"],
        "pair_count": 3,
        "performance_age_slope_s_per_lap": 0.2,
        "performance_age_slope_uncertainty_s_per_lap": [0.17, 0.23],
        "physical_wear": {
            "estimate_available": False,
            "measured_current_set": False,
            "provenance": "UNKNOWN",
            "reason_codes": [
                "NO_CURRENT_SET_DIRECT_WEAR_MEASUREMENT",
                "PERFORMANCE_SLOPE_IS_NOT_PHYSICAL_WEAR",
            ],
            "status": "SKIP_CURRENT_PHYSICAL_WEAR",
        },
        "source_receipt_sha256": dataset["dataset_sha256"],
        "status": "PASS_SHADOW_POSITIVE_DEGRADATION",
        "tire_compound": 0,
    }
    assert model["model_sha256"] == _sha256(
        {key: value for key, value in model.items() if key != "model_sha256"}
    )
    assert validate_tire_performance_model(
        model,
        expected_model_sha256=str(model["model_sha256"]),
        expected_identity_sha256=str(model["identity_sha256"]),
        expected_source_receipt_sha256=str(dataset["dataset_sha256"]),
    ) == model


def test_dataset_requires_independent_pin_self_hash_and_disjoint_stints() -> None:
    dataset = _dataset()
    with pytest.raises(TirePerformanceError, match="independent digest"):
        validate_matched_tire_performance_dataset(
            dataset,
            expected_dataset_sha256="0" * 64,
        )

    tampered = copy.deepcopy(dataset)
    tampered["tire_compound"] = 1
    with pytest.raises(TirePerformanceError, match="self hash"):
        validate_matched_tire_performance_dataset(
            tampered,
            expected_dataset_sha256=str(dataset["dataset_sha256"]),
        )

    duplicated = copy.deepcopy(dataset)
    samples = duplicated["samples"]
    assert isinstance(samples, list) and isinstance(samples[1], dict)
    assert isinstance(samples[0], dict)
    samples[1]["stint_id"] = samples[0]["stint_id"]
    _rehash_dataset(duplicated)
    with pytest.raises(TirePerformanceError, match="stint ids"):
        validate_matched_tire_performance_dataset(
            duplicated,
            expected_dataset_sha256=str(duplicated["dataset_sha256"]),
        )


def test_dataset_rejects_reused_laps_short_pairs_and_in_stint_refuel() -> None:
    for mutation, message in (
        ("lap", "globally disjoint"),
        ("age", "fewer than two"),
        ("fuel", "gains fuel"),
    ):
        dataset = _dataset()
        samples = dataset["samples"]
        assert isinstance(samples, list)
        first = samples[0]
        second = samples[1]
        assert isinstance(first, dict) and isinstance(second, dict)
        assert isinstance(first["early_lap"], dict)
        assert isinstance(second["early_lap"], dict)
        assert isinstance(second["late_lap"], dict)
        if mutation == "lap":
            second["early_lap"]["lap_id"] = first["early_lap"]["lap_id"]
        elif mutation == "age":
            second["late_lap"]["stint_age_laps"] = (
                second["early_lap"]["stint_age_laps"] + 1
            )
        else:
            second["late_lap"]["fuel_start_l"] = (
                second["early_lap"]["fuel_start_l"] + 1.0
            )
        _rehash_dataset(dataset)
        with pytest.raises(TirePerformanceError, match=message):
            validate_matched_tire_performance_dataset(
                dataset,
                expected_dataset_sha256=str(dataset["dataset_sha256"]),
            )


def test_model_waits_when_positive_degradation_sign_is_not_supported() -> None:
    dataset = _dataset()
    samples = dataset["samples"]
    assert isinstance(samples, list)
    for raw, slope in zip(samples, (-0.01, 0.0, 0.01), strict=True):
        assert isinstance(raw, dict)
        early = raw["early_lap"]
        late = raw["late_lap"]
        assert isinstance(early, dict) and isinstance(late, dict)
        age_delta = late["stint_age_laps"] - early["stint_age_laps"]
        fuel_delta = late["fuel_start_l"] - early["fuel_start_l"]
        late["lap_time_s"] = early["lap_time_s"] + slope * age_delta + 0.03 * fuel_delta
    _rehash_dataset(dataset)

    model = build_tire_performance_model(
        dataset,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )

    assert model["estimate_available"] is False
    assert model["status"] == "WAIT_DEGRADATION_SIGN_AMBIGUOUS"
    bounds = model["performance_age_slope_uncertainty_s_per_lap"]
    assert isinstance(bounds, list) and bounds[0] < 0 < bounds[1]


def test_model_validator_rejects_crossed_pins_and_any_physical_wear_promotion() -> None:
    model = _model()
    for message, overrides in (
        ("model differs", {"expected_model_sha256": "0" * 64}),
        ("identity differs", {"expected_identity_sha256": "1" * 64}),
        ("source receipt differs", {"expected_source_receipt_sha256": "2" * 64}),
    ):
        expected = {
            "expected_model_sha256": str(model["model_sha256"]),
            "expected_identity_sha256": str(model["identity_sha256"]),
            "expected_source_receipt_sha256": str(model["source_receipt_sha256"]),
            **overrides,
        }
        with pytest.raises(TirePerformanceError, match=message):
            validate_tire_performance_model(model, **expected)

    promoted = copy.deepcopy(model)
    physical = promoted["physical_wear"]
    assert isinstance(physical, dict)
    physical["estimate_available"] = True
    promoted["model_sha256"] = _sha256(
        {key: value for key, value in promoted.items() if key != "model_sha256"}
    )
    with pytest.raises(TirePerformanceError, match="physical-wear claim"):
        validate_tire_performance_model(
            promoted,
            expected_model_sha256=str(promoted["model_sha256"]),
            expected_identity_sha256=str(promoted["identity_sha256"]),
            expected_source_receipt_sha256=str(promoted["source_receipt_sha256"]),
        )


def test_sequential_service_prefers_keep_but_waits_for_physical_wear() -> None:
    belief = _belief()

    assert belief["contract_version"] == TIRE_PERFORMANCE_BELIEF_CONTRACT_VERSION
    assert belief["method_version"] == TIRE_PERFORMANCE_BELIEF_METHOD_VERSION
    assert belief["estimate_available"] is True
    assert belief["performance_preference"] == "KEEP_TIRES"
    assert belief["status"] == "WAIT_PHYSICAL_WEAR_FOR_NO_TIRE_SERVICE"
    assert belief["reason_codes"] == ["CURRENT_PHYSICAL_WEAR_REQUIRED_FOR_KEEP_TIRES"]
    scenario = belief["scenario"]
    assert isinstance(scenario, dict)
    assert scenario["incremental_tire_service_s"] == 18.0
    assert scenario["keep_tires_time_loss_range_s"] == [4.08, 5.52]
    assert belief["physical_wear"]["estimate_available"] is False  # type: ignore[index]


def test_concurrent_fuel_service_can_support_shadow_tire_change() -> None:
    belief = _belief(
        fuel_add_l=40.0,
        fuel_tire_service_timing="PARALLEL",
    )

    assert belief["estimate_available"] is True
    assert belief["performance_preference"] == "CHANGE_TIRES"
    assert belief["status"] == "PASS_SHADOW_CHANGE_TIRES"
    assert belief["reason_codes"] == []
    assert belief["scenario"]["incremental_tire_service_s"] == 0.0  # type: ignore[index]


def test_tire_change_passes_only_when_full_loss_interval_exceeds_service_cost() -> None:
    belief = _belief(
        current_stint_age_laps=10,
        laps_until_pit=0,
        laps_after_pit=10,
    )

    assert belief["status"] == "WAIT_PERFORMANCE_SERVICE_TRADEOFF"
    assert belief["performance_preference"] == "AMBIGUOUS"
    assert belief["scenario"]["keep_tires_time_loss_range_s"] == [17.0, 23.0]  # type: ignore[index]

    passing = _belief(
        current_stint_age_laps=11,
        laps_until_pit=0,
        laps_after_pit=11,
    )
    assert passing["status"] == "PASS_SHADOW_CHANGE_TIRES"
    assert passing["scenario"]["keep_tires_time_loss_range_s"] == [20.57, 27.83]  # type: ignore[index]


def test_belief_waits_on_compound_mismatch_extrapolation_and_ambiguous_model() -> None:
    mismatch = _belief(current_tire_compound=1)
    assert mismatch["estimate_available"] is False
    assert mismatch["status"] == "WAIT_TIRE_COMPOUND_MISMATCH"
    assert mismatch["scenario"]["keep_tires_time_loss_range_s"] is None  # type: ignore[index]

    extrapolated = _belief(
        current_stint_age_laps=20,
        laps_until_pit=2,
        laps_after_pit=1,
    )
    assert extrapolated["status"] == "WAIT_TIRE_MODEL_EXTRAPOLATION"

    dataset = _dataset()
    samples = dataset["samples"]
    assert isinstance(samples, list)
    for raw, slope in zip(samples, (-0.01, 0.0, 0.01), strict=True):
        assert isinstance(raw, dict)
        early = raw["early_lap"]
        late = raw["late_lap"]
        assert isinstance(early, dict) and isinstance(late, dict)
        age_delta = late["stint_age_laps"] - early["stint_age_laps"]
        fuel_delta = late["fuel_start_l"] - early["fuel_start_l"]
        late["lap_time_s"] = early["lap_time_s"] + slope * age_delta + 0.03 * fuel_delta
    _rehash_dataset(dataset)
    model = build_tire_performance_model(
        dataset,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )
    calibration = _pit_calibration()
    waiting = build_tire_performance_belief(
        model,
        calibration,
        expected_model_sha256=str(model["model_sha256"]),
        expected_model_source_receipt_sha256=str(model["source_receipt_sha256"]),
        expected_calibration_model_sha256=str(calibration["model_sha256"]),
        expected_calibration_source_receipt_sha256=str(
            calibration["source_receipt_sha256"]
        ),
        expected_identity_sha256=str(model["identity_sha256"]),
        current_stint_context_sha256="a" * 64,
        current_source_receipt_sha256="b" * 64,
        current_stint_age_laps=4,
        current_tire_compound=0,
        laps_until_pit=2,
        laps_after_pit=4,
        fuel_add_l=20.0,
        fuel_tire_service_timing="SEQUENTIAL",
    )
    assert waiting["estimate_available"] is False
    assert waiting["status"] == "WAIT_DEGRADATION_SIGN_AMBIGUOUS"


def test_belief_is_self_hashed_and_rejects_crossed_external_pins() -> None:
    belief = _belief()
    assert belief["belief_sha256"] == _sha256(
        {key: value for key, value in belief.items() if key != "belief_sha256"}
    )
    expected = {
        "expected_belief_sha256": str(belief["belief_sha256"]),
        "expected_model_sha256": str(belief["model_sha256"]),
        "expected_calibration_model_sha256": str(
            belief["calibration_model_sha256"]
        ),
        "expected_identity_sha256": str(belief["identity_sha256"]),
        "expected_source_receipt_sha256": str(belief["source_receipt_sha256"]),
    }
    assert validate_tire_performance_belief(belief, **expected) == belief
    for key in expected:
        crossed = {**expected, key: "0" * 64}
        with pytest.raises(TirePerformanceError, match="differs"):
            validate_tire_performance_belief(belief, **crossed)


def test_writer_and_cli_are_create_new_and_fail_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = _dataset()
    dataset_path = tmp_path / "tire-dataset.json"
    output_path = tmp_path / "tire-model.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")

    result = cli_main(
        [
            "build-tire-performance-model",
            str(dataset_path),
            "--expected-dataset-sha256",
            str(dataset["dataset_sha256"]),
            "--output",
            str(output_path),
        ]
    )
    summary = json.loads(capsys.readouterr().out)
    persisted = json.loads(output_path.read_text(encoding="utf-8"))
    assert result == 0
    assert summary["status"] == "PASS_SHADOW_POSITIVE_DEGRADATION"
    assert summary["model_sha256"] == persisted["model_sha256"]

    before = output_path.read_bytes()
    with pytest.raises(TirePerformanceError) as raised:
        write_tire_performance_model_exclusive(
            dataset_path,
            output_path,
            expected_dataset_sha256=str(dataset["dataset_sha256"]),
        )
    assert raised.value.code == "OUTPUT_CREATE_FAILED"
    assert output_path.read_bytes() == before


def test_loader_rejects_duplicate_json_keys_without_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset_path = tmp_path / "duplicate.json"
    output_path = tmp_path / "model.json"
    dataset_path.write_text(
        '{"contract_version":"a","contract_version":"b"}',
        encoding="utf-8",
    )

    result = cli_main(
        [
            "build-tire-performance-model",
            str(dataset_path),
            "--expected-dataset-sha256",
            "0" * 64,
            "--output",
            str(output_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["code"] == "INPUT_READ_FAILED"
    assert payload["status"] == "WAIT_TIRE_PERFORMANCE_MODEL"
    assert not output_path.exists()


def test_tire_stint_context_is_exactly_bound_and_cannot_claim_wear() -> None:
    material: dict[str, object] = {
        "availability": "AVAILABLE",
        "contract_version": TIRE_STINT_CONTEXT_CONTRACT_VERSION,
        "current_laps_completed": 8,
        "current_tire_compound": 0,
        "decision_tick": 500,
        "identity_sha256": "a" * 64,
        "on_pit_road": False,
        "origin_kind": "OBSERVED_PIT_EXIT",
        "origin_laps_completed": 3,
        "origin_tick": 100,
        "physical_wear": {
            "estimate_available": False,
            "measured_current_set": False,
            "provenance": "UNKNOWN",
            "reason_codes": [
                "NO_CURRENT_SET_DIRECT_WEAR_MEASUREMENT",
                "PERFORMANCE_SLOPE_IS_NOT_PHYSICAL_WEAR",
            ],
            "status": "SKIP_CURRENT_PHYSICAL_WEAR",
        },
        "reason_codes": [],
        "source_receipt_sha256": "b" * 64,
        "status": "AVAILABLE_OBSERVED_STINT_AGE",
        "stint_age_completed_laps": 5,
        "tire_sets_used": 2,
    }
    context = {**material, "context_sha256": _sha256(material)}
    expected = {
        "expected_context_sha256": str(context["context_sha256"]),
        "expected_identity_sha256": "a" * 64,
        "expected_source_receipt_sha256": "b" * 64,
        "expected_decision_tick": 500,
    }
    assert validate_tire_stint_context(context, **expected) == context

    promoted = copy.deepcopy(context)
    physical = promoted["physical_wear"]
    assert isinstance(physical, dict)
    physical["measured_current_set"] = True
    promoted["context_sha256"] = _sha256(
        {key: value for key, value in promoted.items() if key != "context_sha256"}
    )
    with pytest.raises(RetrievedLiveAnalysisError, match="wear claim"):
        validate_tire_stint_context(
            promoted,
            expected_context_sha256=str(promoted["context_sha256"]),
            expected_identity_sha256="a" * 64,
            expected_source_receipt_sha256="b" * 64,
            expected_decision_tick=500,
        )


def test_waiting_tire_stint_context_never_invents_age() -> None:
    material: dict[str, object] = {
        "availability": "UNAVAILABLE",
        "contract_version": TIRE_STINT_CONTEXT_CONTRACT_VERSION,
        "current_laps_completed": 8,
        "current_tire_compound": 0,
        "decision_tick": 500,
        "identity_sha256": "a" * 64,
        "on_pit_road": False,
        "origin_kind": None,
        "origin_laps_completed": None,
        "origin_tick": None,
        "physical_wear": {
            "estimate_available": False,
            "measured_current_set": False,
            "provenance": "UNKNOWN",
            "reason_codes": [
                "NO_CURRENT_SET_DIRECT_WEAR_MEASUREMENT",
                "PERFORMANCE_SLOPE_IS_NOT_PHYSICAL_WEAR",
            ],
            "status": "SKIP_CURRENT_PHYSICAL_WEAR",
        },
        "reason_codes": ["CURRENT_STINT_ORIGIN_NOT_OBSERVED"],
        "source_receipt_sha256": "b" * 64,
        "status": "WAIT_STINT_ORIGIN",
        "stint_age_completed_laps": None,
        "tire_sets_used": 2,
    }
    context = {**material, "context_sha256": _sha256(material)}
    assert validate_tire_stint_context(
        context,
        expected_context_sha256=str(context["context_sha256"]),
        expected_identity_sha256="a" * 64,
        expected_source_receipt_sha256="b" * 64,
        expected_decision_tick=500,
    ) == context
