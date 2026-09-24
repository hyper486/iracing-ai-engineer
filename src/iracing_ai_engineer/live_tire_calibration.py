"""Native memory-only tire-calibration selection and current-context check.

This is deliberately not current tire age, performance benefit or an admitted
live strategy model. Loading never changes service history or tire assertions.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .live_car_context import _int, _number, validated_car_context
from .tire_model_validation import _read_json, build_tire_validation_report


@dataclass(frozen=True)
class TireCalibration:
    request_sha256: str
    model_sha256: str
    car_model_id: int
    setup_sha256: str
    identity_sha256: str
    tire_compound: int
    condition_bounds: tuple

    def __post_init__(self):
        if (not _int(self.car_model_id, low=1) or not _int(self.tire_compound, high=1000)
            or any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                   for value in (self.request_sha256, self.model_sha256,
                                 self.setup_sha256, self.identity_sha256))
            or type(self.condition_bounds) is not tuple or len(self.condition_bounds) != 4):
            raise ValueError("TIRE_CALIBRATION_INVALID")
        for row, name in zip(self.condition_bounds,
                             ("air_temp_c", "track_temp_c", "wind_x_mps", "wind_y_mps"),
                             strict=True):
            if (type(row) is not tuple or len(row) != 3 or row[0] != name
                    or not all(_number(value, -100, 100) for value in row[1:])
                    or row[1] > row[2]):
                raise ValueError("TIRE_CALIBRATION_INVALID")


def load_tire_calibration(path, *, expected_request_sha256):
    report = build_tire_validation_report(_read_json(path),
        expected_request_sha256=expected_request_sha256)
    if report["status"] != "PASS_REVIEWED_HOLDOUT":
        raise ValueError("TIRE_CALIBRATION_HOLDOUT_WAIT")
    model, scope, domain = (report[key] for key in (
        "training_model", "applicability", "training_domain"))
    keys = ("air_temp_c", "track_temp_c", "wind_x_mps", "wind_y_mps")
    bounds = tuple((key, min(domain[role][key][0] for role in ("early_lap", "late_lap")),
                    max(domain[role][key][1] for role in ("early_lap", "late_lap")))
                   for key in keys)
    return TireCalibration(report["request_sha256"], model["model_sha256"], scope["car_model_id"],
        scope["setup_sha256"], model["identity_sha256"], model["tire_compound"], bounds)


def matches_identity(calibration, snapshot):
    context = validated_car_context(snapshot)
    if type(calibration) is not TireCalibration or context is None:
        return False
    identity = context["identity"]
    return (calibration.car_model_id == identity["car_model_id"]
            and calibration.setup_sha256 == identity["setup_sha256"]
            and calibration.identity_sha256 == identity["identity_sha256"])


def calibration_status(calibration, snapshot, revision):
    result = {"status": "UNLOADED", "reason": "NO_REVIEWED_CALIBRATION", "revision": revision,
              "model_sha256": None, "persisted": False, "live_model_admitted": False,
              "live_acceptance": False}
    if calibration is None:
        return result
    result.update(status="WAIT", model_sha256=calibration.model_sha256,
                  reason="CAR_OR_SETUP_MISMATCH")
    if not matches_identity(calibration, snapshot):
        return result
    context = validated_car_context(snapshot)
    conditions = context.get("conditions")
    if type(conditions) is not dict or not all(_number(conditions.get(key), low, high)
        for key, low, high in (("air_temp_c", -50, 60), ("track_temp_c", -50, 100),
                              ("wind_speed_mps", 0, 100), ("wind_direction_rad", 0, math.tau),
                              ("precipitation_pct", 0, 100))):
        return {**result, "reason": "CONDITIONS_UNAVAILABLE"}
    if (not _int(conditions.get("track_wetness"), low=1, high=1)
            or conditions["precipitation_pct"] != 0):
        return {**result, "reason": "DRY_CONDITIONS_NOT_OBSERVED"}
    telemetry = (snapshot.get("monitor") or {}).get("telemetry") or {}
    compound = telemetry.get("tire_compound")
    if type(compound) is not int or compound != calibration.tire_compound:
        return {**result, "reason": "COMPOUND_MISMATCH"}
    current = {**conditions,
        "wind_x_mps": conditions["wind_speed_mps"] * math.cos(conditions["wind_direction_rad"]),
        "wind_y_mps": conditions["wind_speed_mps"] * math.sin(conditions["wind_direction_rad"])}
    if any(not low - 1e-9 <= current[key] <= high + 1e-9
           for key, low, high in calibration.condition_bounds):
        return {**result, "reason": "OUTSIDE_OBSERVED_CONDITION_RANGES"}
    return {**result, "status": "CONTEXT_MATCH_ONLY", "reason": "WAIT_CURRENT_TIRE_BELIEF"}


def calibration_notice(value):
    reasons = {
        "NO_REVIEWED_CALIBRATION": "尚未选择已通过留出验证的轮胎校准。",
        "CAR_OR_SETUP_MISMATCH": "车型或设置与校准记录不符。",
        "CONDITIONS_UNAVAILABLE": "缺少当前天气／赛道条件。",
        "DRY_CONDITIONS_NOT_OBSERVED": "当前未明确观测到干地无降水。",
        "COMPOUND_MISMATCH": "当前轮胎配方与校准不同或未知。",
        "OUTSIDE_OBSERVED_CONDITION_RANGES": "当前天气超出校准观测范围。",
        "WAIT_CURRENT_TIRE_BELIEF": "车型、设置、配方和当前天气匹配；仍需当前胎龄及策略收益核对。",
    }
    return reasons.get((value or {}).get("reason"), "轮胎校准暂不可用。") + " 未生成换胎建议。"
