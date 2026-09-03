from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from iracing_ai_engineer.cli import main as cli_main
from iracing_ai_engineer.retrieved_live_analysis import (
    MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION,
    MATCHED_PIT_CALIBRATION_METHOD_VERSION,
    PitCalibrationError,
    build_matched_pit_calibration_model,
    validate_matched_pit_calibration_dataset,
    validate_matched_pit_calibration_model,
    write_matched_pit_calibration_model_exclusive,
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
        "car_class_id": 5,
        "event_type": "Race",
        "official": True,
        "provenance": "SDK_DIRECT_SAME_SOURCE_CAPTURE",
        "race_week": 3,
        "season_id": 202603,
        "series_id": 451,
        "sim_build": "2026.08.28.01",
        "track_config": "Grand Prix",
        "track_id": 144,
    }


def _dataset() -> dict[str, object]:
    material: dict[str, object] = {
        "contract_version": MATCHED_PIT_CALIBRATION_DATASET_CONTRACT_VERSION,
        "dataset_id": "audi-r8-spa-official-race-week-3",
        "dataset_version": 1,
        "event_identity": _identity(),
        "samples": [
            {
                "fuel_delivered_l": 30.0,
                "fuel_service_elapsed_s": 15.0,
                "label_receipt_sha256": "d" * 64,
                "matched_track_segment_elapsed_s": 30.0,
                "pit_road_elapsed_s": 60.0,
                "sample_id": "matched-stop-001",
                "source_receipt_sha256": "a" * 64,
                "stationary_service_elapsed_s": 20.0,
                "tire_change_elapsed_s": 18.0,
            },
            {
                "fuel_delivered_l": 32.0,
                "fuel_service_elapsed_s": 16.0,
                "label_receipt_sha256": "e" * 64,
                "matched_track_segment_elapsed_s": 31.0,
                "pit_road_elapsed_s": 63.0,
                "sample_id": "matched-stop-002",
                "source_receipt_sha256": "b" * 64,
                "stationary_service_elapsed_s": 21.0,
                "tire_change_elapsed_s": 19.0,
            },
            {
                "fuel_delivered_l": 29.0,
                "fuel_service_elapsed_s": 14.5,
                "label_receipt_sha256": "f" * 64,
                "matched_track_segment_elapsed_s": 32.0,
                "pit_road_elapsed_s": 61.0,
                "sample_id": "matched-stop-003",
                "source_receipt_sha256": "c" * 64,
                "stationary_service_elapsed_s": 19.0,
                "tire_change_elapsed_s": 17.0,
            },
        ],
    }
    return {**material, "dataset_sha256": _sha256(material)}


def _rehash_dataset(dataset: dict[str, object]) -> None:
    dataset["dataset_sha256"] = _sha256(
        {key: value for key, value in dataset.items() if key != "dataset_sha256"}
    )


def test_matched_dataset_builds_exact_m2_calibration_model() -> None:
    dataset = _dataset()

    validated_dataset = validate_matched_pit_calibration_dataset(
        dataset,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )
    model = build_matched_pit_calibration_model(
        dataset,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )

    assert validated_dataset == dataset
    assert model == {
        "identity_sha256": _sha256(_identity()),
        "method_version": MATCHED_PIT_CALIBRATION_METHOD_VERSION,
        "model_sha256": model["model_sha256"],
        "pit_lane_loss_s": 10.0,
        "pit_lane_loss_uncertainty_s": [10.0, 11.0],
        "refuel_rate_l_per_s": 2.0,
        "sample_count": 3,
        "service_labels_available": True,
        "source_receipt_sha256": dataset["dataset_sha256"],
        "status": "CALIBRATED_MATCHED_BASELINE",
        "tire_change_time_s": 18.0,
    }
    assert model["model_sha256"] == _sha256(
        {key: value for key, value in model.items() if key != "model_sha256"}
    )
    assert validate_matched_pit_calibration_model(
        model,
        expected_model_sha256=str(model["model_sha256"]),
        expected_identity_sha256=_sha256(_identity()),
        expected_source_receipt_sha256=str(dataset["dataset_sha256"]),
    ) == model


def test_dataset_requires_independently_pinned_digest() -> None:
    dataset = _dataset()

    with pytest.raises(PitCalibrationError) as raised:
        validate_matched_pit_calibration_dataset(
            dataset,
            expected_dataset_sha256="0" * 64,
        )

    assert raised.value.code == "DATASET_INVALID"
    assert "independent digest" in str(raised.value)


def test_dataset_self_hash_rejects_mutation_even_when_expected_pin_is_stale() -> None:
    dataset = _dataset()
    original_sha256 = str(dataset["dataset_sha256"])
    samples = dataset["samples"]
    assert isinstance(samples, list) and isinstance(samples[0], dict)
    samples[0]["pit_road_elapsed_s"] = 99.0

    with pytest.raises(PitCalibrationError) as raised:
        validate_matched_pit_calibration_dataset(
            dataset,
            expected_dataset_sha256=original_sha256,
        )

    assert raised.value.code == "DATASET_INVALID"
    assert "self hash" in str(raised.value)


def test_dataset_requires_three_independent_matched_samples() -> None:
    dataset = _dataset()
    samples = dataset["samples"]
    assert isinstance(samples, list)
    del samples[2:]
    _rehash_dataset(dataset)

    with pytest.raises(PitCalibrationError) as raised:
        build_matched_pit_calibration_model(
            dataset,
            expected_dataset_sha256=str(dataset["dataset_sha256"]),
        )

    assert raised.value.code == "INSUFFICIENT_MATCHED_SAMPLES"


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("sample_id", "sample ids"),
        ("source_receipt_sha256", "source receipts"),
        ("label_receipt_sha256", "label receipts"),
    ],
)
def test_dataset_rejects_duplicate_sample_lineage(field: str, message: str) -> None:
    dataset = _dataset()
    samples = dataset["samples"]
    assert isinstance(samples, list)
    first = samples[0]
    second = samples[1]
    assert isinstance(first, dict) and isinstance(second, dict)
    second[field] = first[field]
    _rehash_dataset(dataset)

    with pytest.raises(PitCalibrationError) as raised:
        validate_matched_pit_calibration_dataset(
            dataset,
            expected_dataset_sha256=str(dataset["dataset_sha256"]),
        )

    assert raised.value.code == "DATASET_INVALID"
    assert message in str(raised.value)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {
                "matched_track_segment_elapsed_s": 41.0,
                "pit_road_elapsed_s": 60.0,
                "stationary_service_elapsed_s": 20.0,
            },
            "negative counterfactual",
        ),
        (
            {"fuel_service_elapsed_s": 21.0, "stationary_service_elapsed_s": 20.0},
            "fuel service exceeds",
        ),
        (
            {"stationary_service_elapsed_s": 20.0, "tire_change_elapsed_s": 21.0},
            "tire service exceeds",
        ),
        (
            {"fuel_delivered_l": 41.0, "fuel_service_elapsed_s": 2.0},
            "refuel rate is implausibly high",
        ),
    ],
)
def test_dataset_rejects_physically_inconsistent_matches(
    updates: dict[str, float],
    message: str,
) -> None:
    dataset = _dataset()
    samples = dataset["samples"]
    assert isinstance(samples, list) and isinstance(samples[0], dict)
    samples[0].update(updates)
    _rehash_dataset(dataset)

    with pytest.raises(PitCalibrationError) as raised:
        validate_matched_pit_calibration_dataset(
            dataset,
            expected_dataset_sha256=str(dataset["dataset_sha256"]),
        )

    assert raised.value.code == "MATCH_INVALID"
    assert message in str(raised.value)


def test_model_validator_rejects_crossed_identity_source_and_model_pins() -> None:
    dataset = _dataset()
    model = build_matched_pit_calibration_model(
        dataset,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )

    for keyword, overrides in (
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
        with pytest.raises(PitCalibrationError, match=keyword):
            validate_matched_pit_calibration_model(model, **expected)


def test_writer_is_create_new_and_never_overwrites_existing_model(tmp_path: Path) -> None:
    dataset = _dataset()
    dataset_path = tmp_path / "matched.json"
    output_path = tmp_path / "model.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")

    model = write_matched_pit_calibration_model_exclusive(
        dataset_path,
        output_path,
        expected_dataset_sha256=str(dataset["dataset_sha256"]),
    )
    persisted = json.loads(output_path.read_text(encoding="utf-8"))

    assert persisted == model
    before = output_path.read_bytes()
    with pytest.raises(PitCalibrationError) as raised:
        write_matched_pit_calibration_model_exclusive(
            dataset_path,
            output_path,
            expected_dataset_sha256=str(dataset["dataset_sha256"]),
        )
    assert raised.value.code == "OUTPUT_CREATE_FAILED"
    assert output_path.read_bytes() == before


def test_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    dataset_path = tmp_path / "duplicate.json"
    output_path = tmp_path / "model.json"
    dataset_path.write_text('{"contract_version":"a","contract_version":"b"}', encoding="utf-8")

    with pytest.raises(PitCalibrationError) as raised:
        write_matched_pit_calibration_model_exclusive(
            dataset_path,
            output_path,
            expected_dataset_sha256="0" * 64,
        )

    assert raised.value.code == "INPUT_READ_FAILED"
    assert "duplicate JSON key" in str(raised.value)
    assert not output_path.exists()


def test_cli_returns_wait_without_creating_output_for_invalid_dataset(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = _dataset()
    invalid = copy.deepcopy(dataset)
    invalid["dataset_sha256"] = "0" * 64
    dataset_path = tmp_path / "invalid.json"
    output_path = tmp_path / "model.json"
    dataset_path.write_text(json.dumps(invalid), encoding="utf-8")

    result = cli_main(
        [
            "build-pit-calibration",
            str(dataset_path),
            "--expected-dataset-sha256",
            "0" * 64,
            "--output",
            str(output_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 2
    assert payload["code"] == "DATASET_INVALID"
    assert payload["status"] == "WAIT_CALIBRATION"
    assert not output_path.exists()
