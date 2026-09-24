"""Native memory-only tire-calibration selection and current-context check.

Loading retains a validated historical model for a separate conditional
comparison; it is not current tire age or live strategy admission. Loading
never changes service history or tire assertions.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from .live_car_context import _int, _number, validated_car_context
from .live_strategy import _map, _same_typed_tree
from .retrieved_live_analysis import validate_tire_performance_model
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
    model_json: str | None = None
    age_bounds: tuple | None = None
    fuel_bounds: tuple | None = None

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
        if (self.model_json, self.age_bounds, self.fuel_bounds) == (None, None, None):
            return
        if (type(self.model_json) is not str or len(self.model_json) > 8192
                or type(self.age_bounds) is not tuple or len(self.age_bounds) != 2
                or not all(_int(n, high=10_000) for n in self.age_bounds)
                or not self.age_bounds[0] <= self.age_bounds[1]
                or type(self.fuel_bounds) is not tuple or len(self.fuel_bounds) != 2
                or not all(_number(n, 0, 1000) for n in self.fuel_bounds)
                or not self.fuel_bounds[0] <= self.fuel_bounds[1]):
            raise ValueError("TIRE_CALIBRATION_INVALID")
        model = json.loads(self.model_json)
        if type(model) is not dict:
            raise ValueError("TIRE_CALIBRATION_INVALID")
        model = validate_tire_performance_model(model, expected_model_sha256=self.model_sha256,
            expected_identity_sha256=self.identity_sha256,
            expected_source_receipt_sha256=model.get("source_receipt_sha256"))
        low, high = model["performance_age_slope_uncertainty_s_per_lap"]
        if (not model["estimate_available"] or not 0 < low <= high <= 5
                or model["tire_compound"] != self.tire_compound
                or model["max_supported_stint_age_laps"] != self.age_bounds[1]):
            raise ValueError("TIRE_CALIBRATION_INVALID")

    def projection(self):
        """Fixed numeric/hash surface, never raw calibration samples or names."""
        return {"request_sha256": self.request_sha256, "model_sha256": self.model_sha256,
            "car_model_id": self.car_model_id, "setup_sha256": self.setup_sha256,
            "identity_sha256": self.identity_sha256, "tire_compound": self.tire_compound,
            "condition_bounds": [list(row) for row in self.condition_bounds],
            "model": json.loads(self.model_json) if self.model_json is not None else None,
            "age_bounds": list(self.age_bounds) if self.age_bounds is not None else None,
            "fuel_bounds": list(self.fuel_bounds) if self.fuel_bounds is not None else None}


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
        scope["setup_sha256"], model["identity_sha256"], model["tire_compound"], bounds,
        json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False),
        (min(domain[role]["stint_age_laps"][0] for role in ("early_lap", "late_lap")),
         max(domain[role]["stint_age_laps"][1] for role in ("early_lap", "late_lap"))),
        (min(domain[role]["fuel_start_l"][0] for role in ("early_lap", "late_lap")),
         max(domain[role]["fuel_start_l"][1] for role in ("early_lap", "late_lap"))))


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
              "live_acceptance": False, "selection": None}
    if calibration is None:
        return result
    result.update(status="WAIT", model_sha256=calibration.model_sha256,
                  reason="CAR_OR_SETUP_MISMATCH", selection=calibration.projection())
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


def validated_calibration(snapshot):
    """Reconstruct the internal selection; a model hash alone is insufficient."""
    value = _map(snapshot.get("tire_calibration"))
    data = value.get("selection")
    if type(data) is not dict or set(data) != {
        "request_sha256", "model_sha256", "car_model_id", "setup_sha256", "identity_sha256",
        "tire_compound", "condition_bounds", "model", "age_bounds", "fuel_bounds",
    } or not _int(value.get("revision")):
        return None
    try:
        # Bound before serializing, including malicious nested/nonfinite values.
        from .live_worker import payload_size
        # Retained Python-object accounting includes large per-node overhead;
        # it is not the JSON byte count (the model itself is capped at 8 KiB).
        payload_size(data, limit=65_536)
        if type(data["condition_bounds"]) is not list:
            return None
        calibration = TireCalibration(*(data[key] for key in (
            "request_sha256", "model_sha256", "car_model_id", "setup_sha256",
            "identity_sha256", "tire_compound")),
            tuple(tuple(row) for row in data["condition_bounds"]),
            json.dumps(data["model"], sort_keys=True, separators=(",", ":"), allow_nan=False)
                if data["model"] is not None else None,
            tuple(data["age_bounds"]) if type(data["age_bounds"]) is list else None,
            tuple(data["fuel_bounds"]) if type(data["fuel_bounds"]) is list else None)
        expected = calibration_status(calibration, snapshot, value["revision"])
        if expected["status"] == "CONTEXT_MATCH_ONLY" and _same_typed_tree(value, expected):
            return calibration
    except (TypeError, ValueError, OverflowError, RecursionError):
        pass
    return None


def calibration_notice(value):
    reasons = {
        "NO_REVIEWED_CALIBRATION": "尚未选择已通过留出验证的轮胎校准。",
        "CAR_OR_SETUP_MISMATCH": "车型或设置与校准记录不符。",
        "CONDITIONS_UNAVAILABLE": "缺少当前天气／赛道条件。",
        "DRY_CONDITIONS_NOT_OBSERVED": "当前未明确观测到干地无降水。",
        "COMPOUND_MISMATCH": "当前轮胎配方与校准不同或未知。",
        "OUTSIDE_OBSERVED_CONDITION_RANGES": "当前天气超出校准观测范围。",
        "WAIT_CURRENT_TIRE_BELIEF": ("车型、设置、配方和当前天气匹配；"
                                    "收益比较另需确认胎组、燃油及服务假设。"),
    }
    return reasons.get((value or {}).get("reason"), "轮胎校准暂不可用。") + " 未生成换胎建议。"
