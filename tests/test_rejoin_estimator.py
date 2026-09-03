from __future__ import annotations

import copy

import pytest

from iracing_ai_engineer.engineer_session import canonical_sha256
from iracing_ai_engineer.retrieved_live_analysis import (
    MATCHED_PIT_CALIBRATION_METHOD_VERSION,
    TIME_DOMAIN_REJOIN_ESTIMATE_CONTRACT_VERSION,
    TIME_DOMAIN_REJOIN_METHOD_VERSION,
    TRAFFIC_MOTION_CONTEXT_CONTRACT_VERSION,
    RetrievedLiveAnalysisError,
    build_time_domain_rejoin_estimate,
    validate_time_domain_rejoin_estimate,
    validate_traffic_motion_context,
)


def _identity_sha256() -> str:
    return canonical_sha256(
        {
            "car_class_id": 5,
            "event_type": "Race",
            "official": True,
            "provenance": "CONTRACT_FIXTURE",
            "race_week": 3,
            "season_id": 2,
            "series_id": 1,
            "sim_build": "fixture-build",
            "track_config": "Grand Prix",
            "track_id": 4,
        }
    )


def _calibration() -> dict[str, object]:
    material: dict[str, object] = {
        "identity_sha256": _identity_sha256(),
        "method_version": MATCHED_PIT_CALIBRATION_METHOD_VERSION,
        "pit_lane_loss_s": 25.0,
        "pit_lane_loss_uncertainty_s": [24.0, 26.0],
        "refuel_rate_l_per_s": 2.0,
        "sample_count": 3,
        "service_labels_available": True,
        "source_receipt_sha256": "b" * 64,
        "status": "CALIBRATED_MATCHED_BASELINE",
        "tire_change_time_s": 18.0,
    }
    return {**material, "model_sha256": canonical_sha256(material)}


def _motion() -> dict[str, object]:
    material: dict[str, object] = {
        "availability": "AVAILABLE",
        "contract_version": TRAFFIC_MOTION_CONTEXT_CONTRACT_VERSION,
        "decision_tick": 12345,
        "identity_sha256": _identity_sha256(),
        "observation_window_s": 10.0,
        "opponents": [
            {
                "car_idx": 1,
                "current_signed_lap_delta": 0.5,
                "point_count": 601,
                "rate_laps_per_s": 0.01,
                "rate_range_laps_per_s": [0.0095, 0.0105],
            },
            {
                "car_idx": 2,
                "current_signed_lap_delta": -0.1,
                "point_count": 601,
                "rate_laps_per_s": 0.01,
                "rate_range_laps_per_s": [0.0095, 0.0105],
            },
            {
                "car_idx": 3,
                "current_signed_lap_delta": -0.5,
                "point_count": 601,
                "rate_laps_per_s": 0.01,
                "rate_range_laps_per_s": [0.009, 0.011],
            },
        ],
        "player": {
            "car_idx": 0,
            "point_count": 601,
            "rate_laps_per_s": 0.01,
            "rate_range_laps_per_s": [0.0095, 0.0105],
        },
        "reason_codes": [],
        "source_receipt_sha256": "a" * 64,
        "status": "VERIFIED_TIME_DOMAIN_MOTION",
        "traffic_map_revision_sha256": "c" * 64,
    }
    return {**material, "motion_sha256": canonical_sha256(material)}


def _build(*, change_tires: bool = False) -> dict[str, object]:
    motion = _motion()
    calibration = _calibration()
    return build_time_domain_rejoin_estimate(
        motion,
        calibration,
        expected_motion_sha256=str(motion["motion_sha256"]),
        expected_motion_source_receipt_sha256="a" * 64,
        expected_traffic_map_revision_sha256="c" * 64,
        expected_calibration_model_sha256=str(calibration["model_sha256"]),
        expected_calibration_source_receipt_sha256="b" * 64,
        expected_identity_sha256=_identity_sha256(),
        expected_decision_tick=12345,
        fuel_add_l=20.0,
        change_tires=change_tires,
        fuel_tire_service_timing="SEQUENTIAL",
    )


def test_motion_context_and_action_specific_rejoin_are_exact_and_self_hashed() -> None:
    motion = _motion()
    assert validate_traffic_motion_context(
        motion,
        expected_motion_sha256=str(motion["motion_sha256"]),
        expected_source_receipt_sha256="a" * 64,
        expected_traffic_map_revision_sha256="c" * 64,
        expected_identity_sha256=_identity_sha256(),
        expected_decision_tick=12345,
    ) == motion

    estimate = _build()

    assert estimate["contract_version"] == (
        TIME_DOMAIN_REJOIN_ESTIMATE_CONTRACT_VERSION
    )
    assert estimate["method_version"] == TIME_DOMAIN_REJOIN_METHOD_VERSION
    assert estimate["estimate_available"] is True
    assert estimate["status"] == "AVAILABLE_STABLE_BRACKET"
    assert estimate["service_scenario"] == {
        "change_tires": False,
        "fuel_add_l": 20.0,
        "fuel_tire_service_timing": "SEQUENTIAL",
        "stationary_service_s": 10.0,
        "total_pit_loss_range_s": [34.0, 36.0],
    }
    assert estimate["nearest_ahead"] == {
        "car_idx": 2,
        "gap_range_s": [23.473684, 26.47619],
    }
    assert estimate["nearest_behind"] == {
        "car_idx": 3,
        "gap_range_s": [9.454545, 21.555556],
    }
    assert estimate["estimate_sha256"] == canonical_sha256(
        {
            key: value
            for key, value in estimate.items()
            if key != "estimate_sha256"
        }
    )
    assert validate_time_domain_rejoin_estimate(
        estimate,
        expected_estimate_sha256=str(estimate["estimate_sha256"]),
        expected_identity_sha256=_identity_sha256(),
        expected_motion_sha256=str(motion["motion_sha256"]),
        expected_calibration_model_sha256=str(_calibration()["model_sha256"]),
    ) == estimate


def test_tire_service_that_crosses_a_car_returns_wait_without_neighbor_claims() -> None:
    estimate = _build(change_tires=True)

    assert estimate["estimate_available"] is False
    assert estimate["status"] == "WAIT_AMBIGUOUS_REJOIN_ORDER"
    assert estimate["reason_codes"] == [
        "REJOIN_ZERO_CROSSING_WITHIN_UNCERTAINTY"
    ]
    assert estimate["nearest_ahead"] is None
    assert estimate["nearest_behind"] is None
    assert estimate["service_scenario"]["total_pit_loss_range_s"] == [52.0, 54.0]


@pytest.mark.parametrize(
    ("field", "expected_name"),
    [
        ("identity_sha256", "expected_identity_sha256"),
        ("motion_sha256", "expected_motion_sha256"),
        ("source_receipt_sha256", "expected_source_receipt_sha256"),
        ("traffic_map_revision_sha256", "expected_traffic_map_revision_sha256"),
    ],
)
def test_motion_context_rejects_crossed_independent_pins(
    field: str,
    expected_name: str,
) -> None:
    motion = _motion()
    kwargs: dict[str, object] = {
        "expected_motion_sha256": motion["motion_sha256"],
        "expected_source_receipt_sha256": "a" * 64,
        "expected_traffic_map_revision_sha256": "c" * 64,
        "expected_identity_sha256": _identity_sha256(),
        "expected_decision_tick": 12345,
    }
    kwargs[expected_name] = "f" * 64

    with pytest.raises(RetrievedLiveAnalysisError, match="differs"):
        validate_traffic_motion_context(motion, **kwargs)


def test_rejoin_estimate_total_rehash_still_needs_the_external_digest() -> None:
    estimate = _build()
    changed = copy.deepcopy(estimate)
    changed["status"] = "WAIT_AMBIGUOUS_REJOIN_ORDER"
    changed["estimate_available"] = False
    changed["nearest_ahead"] = None
    changed["nearest_behind"] = None
    changed["reason_codes"] = ["ATTACKER_RELABELED_OUTPUT"]
    changed["estimate_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in changed.items()
            if key != "estimate_sha256"
        }
    )

    with pytest.raises(RetrievedLiveAnalysisError, match="independent digest"):
        validate_time_domain_rejoin_estimate(
            changed,
            expected_estimate_sha256=str(estimate["estimate_sha256"]),
            expected_identity_sha256=_identity_sha256(),
            expected_motion_sha256=str(_motion()["motion_sha256"]),
            expected_calibration_model_sha256=str(_calibration()["model_sha256"]),
        )


def test_rejoin_estimator_rejects_unavailable_motion() -> None:
    motion = _motion()
    material = {
        **{
            key: value
            for key, value in motion.items()
            if key != "motion_sha256"
        },
        "availability": "UNAVAILABLE",
        "observation_window_s": None,
        "opponents": [],
        "player": None,
        "reason_codes": ["NO_OPPONENT_RATE_AVAILABLE"],
        "status": "WAIT_TIME_DOMAIN_MOTION",
    }
    unavailable = {**material, "motion_sha256": canonical_sha256(material)}
    calibration = _calibration()

    with pytest.raises(RetrievedLiveAnalysisError) as raised:
        build_time_domain_rejoin_estimate(
            unavailable,
            calibration,
            expected_motion_sha256=str(unavailable["motion_sha256"]),
            expected_motion_source_receipt_sha256="a" * 64,
            expected_traffic_map_revision_sha256="c" * 64,
            expected_calibration_model_sha256=str(calibration["model_sha256"]),
            expected_calibration_source_receipt_sha256="b" * 64,
            expected_identity_sha256=_identity_sha256(),
            expected_decision_tick=12345,
            fuel_add_l=20.0,
            change_tires=False,
            fuel_tire_service_timing="SEQUENTIAL",
        )
    assert raised.value.code == "REJOIN_MOTION_UNAVAILABLE"
