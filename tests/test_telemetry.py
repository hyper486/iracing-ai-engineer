from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError

import pytest

from iracing_ai_engineer.contracts import NORMALIZATION_PROFILE_VERSION
from iracing_ai_engineer.telemetry import (
    TELEMETRY_CONTRACT_VERSION,
    TELEMETRY_PARQUET_SCHEMA_VERSION,
    Presence,
    Provenance,
    QualityStatus,
    SourceKind,
    TelemetryField,
    TelemetryNormalizationError,
    normalize_sdk_frame,
    telemetry_parquet_schema,
)


def complete_frame(*, tick: int = 100, session_time: float = 10.0) -> dict[str, object]:
    return {
        "SessionNum": 1,
        "SessionTick": tick,
        "SessionTime": session_time,
        "SessionTimeRemain": 3_590.0,
        "SessionLapsRemainEx": 20,
        "Lap": 4,
        "LapCompleted": 3,
        "LapDistPct": 0.25,
        "Speed": 71.5,
        "Throttle": 0.8,
        "Brake": 0.0,
        "Clutch": 0.0,
        "SteeringWheelAngle": -0.12,
        "Gear": 5,
        "RPM": 7_200.0,
        "FuelLevel": 54.5,
        "FuelLevelPct": 0.545,
        "FuelUsePerHour": 122.0,
        "SessionTimeOfDay": 59_422.0,
        "TrackTemp": 26.1,
        "TrackTempCrew": 26.2,
        "AirTemp": 25.3,
        "WeatherType": 3,
        "WeatherVersion": 0,
        "Skies": 1,
        "WindVel": 0.894,
        "WindDir": 6.283185307179586,
        "RelativeHumidity": 0.4565,
        "Precipitation": 0.0,
        "PlayerTireCompound": 0,
        "TireSetsUsed": 1,
        "OnPitRoad": False,
        "PlayerCarInPitStall": False,
        "PitstopActive": False,
        "PitsOpen": True,
        "SessionFlags": 0x80000000,
        "PlayerTrackSurface": 3,
        "IsOnTrack": True,
        "IsOnTrackCar": True,
        "PlayerCarMyIncidentCount": 2,
        "PlayerCarDriverIncidentCount": 3,
        "PlayerCarTeamIncidentCount": 4,
        "PlayerCarIdx": 1,
        "CarIdxLap": [4, 4, 3],
        "CarIdxLapCompleted": [3, 3, 2],
        "CarIdxLapDistPct": [0.20, 0.25, 0.90],
        "CarIdxOnPitRoad": [False, False, True],
        "CarIdxTrackSurface": [3, 3, 2],
    }


def normalize(
    frame: dict[str, object],
    *,
    tick: int = 1_000,
    captured: float = 100.0,
    previous=None,
    source_id: str = "windows-rig-sdk",
    session_id: str | None = "subsession-80628890",
    source_kind: SourceKind = SourceKind.SDK_LIVE,
    **kwargs,
):
    return normalize_sdk_frame(
        frame,
        source_id=source_id,
        session_id=session_id,
        source_kind=source_kind,
        buffer_tick=tick,
        captured_monotonic_s=captured,
        previous=previous,
        **kwargs,
    )


def test_contract_is_immutable_and_carries_presence_and_provenance():
    sample = normalize(complete_frame())

    assert sample.source.source_kind.value is SourceKind.SDK_LIVE
    assert sample.session.session_time_s.presence is Presence.PRESENT
    assert sample.session.session_time_s.provenance is Provenance.SDK_DIRECT
    assert sample.quality.status.value is QualityStatus.DEGRADED
    assert "STALE_UNASSESSED" in sample.quality.issues.value
    with pytest.raises(FrozenInstanceError):
        sample.lap.lap_number.value = 99


def test_incident_counters_preserve_three_distinct_raw_channels():
    sample = normalize(complete_frame())

    assert TELEMETRY_CONTRACT_VERSION == "normalized-telemetry-v3"
    assert TELEMETRY_PARQUET_SCHEMA_VERSION == "normalized-telemetry-parquet-v3"
    assert NORMALIZATION_PROFILE_VERSION == "normalized-sdk-adapter-v3"
    assert sample.contract_version == TELEMETRY_CONTRACT_VERSION
    assert sample.incidents.player_car_my_incident_count == TelemetryField.present(
        2, Provenance.SDK_DIRECT, "PlayerCarMyIncidentCount"
    )
    assert sample.incidents.player_car_driver_incident_count.value == 3
    assert sample.incidents.player_car_team_incident_count.value == 4


def test_environment_and_tire_channels_preserve_direct_sdk_lineage():
    sample = normalize(complete_frame())

    assert sample.environment.session_time_of_day_s == TelemetryField.present(
        59_422.0, Provenance.SDK_DIRECT, "SessionTimeOfDay"
    )
    assert sample.environment.track_temp_c.value == 26.1
    assert sample.environment.track_temp_crew_c == TelemetryField.present(
        26.2, Provenance.SDK_DIRECT, "TrackTempCrew"
    )
    assert sample.environment.air_temp_c.value == 25.3
    assert sample.environment.weather_type.value == 3
    assert sample.environment.weather_version.value == 0
    assert sample.environment.skies.value == 1
    assert sample.environment.wind_velocity_mps.value == 0.894
    assert sample.environment.wind_direction_rad.value == 6.283185307179586
    assert sample.environment.relative_humidity_fraction.value == 0.4565
    assert sample.environment.precipitation_fraction.value == 0.0
    assert sample.tires.player_tire_compound == TelemetryField.present(
        0, Provenance.SDK_DIRECT, "PlayerTireCompound"
    )
    assert sample.tires.tire_sets_used == TelemetryField.present(
        1, Provenance.SDK_DIRECT, "TireSetsUsed"
    )


@pytest.mark.parametrize(
    ("sdk_name", "group", "attribute"),
    [
        ("SessionTimeOfDay", "environment", "session_time_of_day_s"),
        ("TrackTemp", "environment", "track_temp_c"),
        ("TrackTempCrew", "environment", "track_temp_crew_c"),
        ("AirTemp", "environment", "air_temp_c"),
        ("WeatherType", "environment", "weather_type"),
        ("WeatherVersion", "environment", "weather_version"),
        ("Skies", "environment", "skies"),
        ("WindVel", "environment", "wind_velocity_mps"),
        ("WindDir", "environment", "wind_direction_rad"),
        ("RelativeHumidity", "environment", "relative_humidity_fraction"),
        ("Precipitation", "environment", "precipitation_fraction"),
        ("PlayerTireCompound", "tires", "player_tire_compound"),
        ("TireSetsUsed", "tires", "tire_sets_used"),
    ],
)
def test_missing_condition_channel_stays_missing_without_substitution(
    sdk_name: str,
    group: str,
    attribute: str,
):
    frame = complete_frame()
    del frame[sdk_name]

    sample = normalize(frame)
    field = getattr(getattr(sample, group), attribute)

    assert field == TelemetryField.missing()
    assert f"INVALID_FIELD:{sdk_name}" not in sample.quality.issues.value


@pytest.mark.parametrize(
    ("sdk_name", "group", "attribute"),
    [
        ("TrackTemp", "environment", "track_temp_c"),
        ("TrackTempCrew", "environment", "track_temp_crew_c"),
        ("AirTemp", "environment", "air_temp_c"),
        ("WindDir", "environment", "wind_direction_rad"),
    ],
)
def test_temperature_and_wind_direction_accept_any_finite_value(
    sdk_name: str,
    group: str,
    attribute: str,
):
    frame = complete_frame()
    frame[sdk_name] = -12.5

    sample = normalize(frame)

    assert getattr(getattr(sample, group), attribute) == TelemetryField.present(
        -12.5, Provenance.SDK_DIRECT, sdk_name
    )


@pytest.mark.parametrize(
    ("sdk_name", "group", "attribute"),
    [
        ("WeatherType", "environment", "weather_type"),
        ("WeatherVersion", "environment", "weather_version"),
        ("Skies", "environment", "skies"),
        ("PlayerTireCompound", "tires", "player_tire_compound"),
        ("TireSetsUsed", "tires", "tire_sets_used"),
    ],
)
def test_negative_condition_integer_is_invalid(
    sdk_name: str,
    group: str,
    attribute: str,
):
    frame = complete_frame()
    frame[sdk_name] = -1

    sample = normalize(frame)

    assert getattr(getattr(sample, group), attribute) == TelemetryField.invalid(
        Provenance.SDK_DIRECT, sdk_name
    )
    assert f"INVALID_FIELD:{sdk_name}" in sample.quality.issues.value


@pytest.mark.parametrize("sdk_name", ["SessionTimeOfDay", "WindVel"])
def test_negative_nonnegative_environment_channel_is_invalid(sdk_name: str):
    frame = complete_frame()
    frame[sdk_name] = -0.001

    sample = normalize(frame)
    attribute = {
        "SessionTimeOfDay": "session_time_of_day_s",
        "WindVel": "wind_velocity_mps",
    }[sdk_name]

    assert getattr(sample.environment, attribute) == TelemetryField.invalid(
        Provenance.SDK_DIRECT, sdk_name
    )


@pytest.mark.parametrize(
    ("sdk_name", "attribute"),
    [
        ("SessionTimeOfDay", "session_time_of_day_s"),
        ("TrackTemp", "track_temp_c"),
        ("TrackTempCrew", "track_temp_crew_c"),
        ("AirTemp", "air_temp_c"),
        ("WindVel", "wind_velocity_mps"),
        ("WindDir", "wind_direction_rad"),
        ("RelativeHumidity", "relative_humidity_fraction"),
        ("Precipitation", "precipitation_fraction"),
    ],
)
def test_non_finite_environment_channel_is_invalid(
    sdk_name: str,
    attribute: str,
):
    frame = complete_frame()
    frame[sdk_name] = math.nan

    sample = normalize(frame)

    assert getattr(sample.environment, attribute) == TelemetryField.invalid(
        Provenance.SDK_DIRECT, sdk_name
    )
    assert f"INVALID_FIELD:{sdk_name}" in sample.quality.issues.value


@pytest.mark.parametrize(
    ("sdk_name", "attribute"),
    [
        ("PlayerCarMyIncidentCount", "player_car_my_incident_count"),
        ("PlayerCarDriverIncidentCount", "player_car_driver_incident_count"),
        ("PlayerCarTeamIncidentCount", "player_car_team_incident_count"),
    ],
)
def test_missing_incident_counter_stays_missing_without_zero_substitution(
    sdk_name: str,
    attribute: str,
):
    frame = complete_frame()
    del frame[sdk_name]

    sample = normalize(frame)
    field = getattr(sample.incidents, attribute)

    assert field == TelemetryField.missing()
    assert field.value is None
    assert sample.quality.status.value is QualityStatus.DEGRADED
    assert "STALE_UNASSESSED" in sample.quality.issues.value
    assert f"INVALID_FIELD:{sdk_name}" not in sample.quality.issues.value


@pytest.mark.parametrize(
    ("sdk_name", "attribute"),
    [
        ("PlayerCarMyIncidentCount", "player_car_my_incident_count"),
        ("PlayerCarDriverIncidentCount", "player_car_driver_incident_count"),
        ("PlayerCarTeamIncidentCount", "player_car_team_incident_count"),
    ],
)
def test_negative_incident_counter_is_invalid_not_zero(
    sdk_name: str,
    attribute: str,
):
    frame = complete_frame()
    frame[sdk_name] = -1

    sample = normalize(frame)
    field = getattr(sample.incidents, attribute)

    assert field == TelemetryField.invalid(Provenance.SDK_DIRECT, sdk_name)
    assert field.value is None
    assert f"INVALID_FIELD:{sdk_name}" in sample.quality.issues.value


@pytest.mark.parametrize(
    ("sdk_name", "raw", "group", "attribute", "expected"),
    [
        ("Throttle", -1e-7, "controls", "throttle", 0.0),
        ("Brake", 1.0 + 1e-7, "controls", "brake", 1.0),
        ("Clutch", -1e-7, "controls", "clutch", 0.0),
        ("FuelLevelPct", 1.0 + 1e-7, "fuel", "level_pct", 1.0),
        (
            "RelativeHumidity",
            -1e-7,
            "environment",
            "relative_humidity_fraction",
            0.0,
        ),
        (
            "Precipitation",
            1.0 + 1e-7,
            "environment",
            "precipitation_fraction",
            1.0,
        ),
    ],
)
def test_unit_interval_float32_boundary_noise_is_canonicalized(
    sdk_name: str,
    raw: float,
    group: str,
    attribute: str,
    expected: float,
):
    frame = complete_frame()
    frame[sdk_name] = raw

    sample = normalize(frame)
    field = getattr(getattr(sample, group), attribute)

    assert field == TelemetryField.present(expected, Provenance.SDK_DIRECT, sdk_name)
    assert f"INVALID_FIELD:{sdk_name}" not in sample.quality.issues.value


def test_material_unit_interval_violation_remains_invalid():
    frame = complete_frame()
    frame["Throttle"] = -1e-3

    sample = normalize(frame)

    assert sample.controls.throttle == TelemetryField.invalid(
        Provenance.SDK_DIRECT, "Throttle"
    )
    assert "INVALID_FIELD:Throttle" in sample.quality.issues.value


@pytest.mark.parametrize(
    ("sdk_name", "attribute", "raw"),
    [
        ("RelativeHumidity", "relative_humidity_fraction", -2e-6),
        ("Precipitation", "precipitation_fraction", 1.0 + 2e-6),
    ],
)
def test_material_environment_fraction_violation_remains_invalid(
    sdk_name: str,
    attribute: str,
    raw: float,
):
    frame = complete_frame()
    frame[sdk_name] = raw

    sample = normalize(frame)

    assert getattr(sample.environment, attribute) == TelemetryField.invalid(
        Provenance.SDK_DIRECT, sdk_name
    )


@pytest.mark.parametrize(
    ("sdk_name", "path"),
    [
        ("Throttle", ("controls", "throttle")),
        ("FuelLevel", ("fuel", "level_l")),
        ("OnPitRoad", ("pit", "on_pit_road")),
        ("SessionFlags", ("flags", "session_flags")),
        ("LapDistPct", ("lap", "lap_distance_pct")),
    ],
)
def test_missing_dynamic_schema_fields_are_not_filled(
    sdk_name: str, path: tuple[str, str]
):
    frame = complete_frame()
    del frame[sdk_name]

    sample = normalize(frame)
    field = getattr(getattr(sample, path[0]), path[1])

    assert field.value is None
    assert field.presence is Presence.MISSING
    assert field.provenance is Provenance.UNKNOWN
    payload = sample.to_dict()
    serialized = payload[path[0]][path[1]]
    assert serialized == {
        "value": None,
        "presence": "MISSING",
        "provenance": "UNKNOWN",
        "source_fields": [],
    }


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_non_finite_core_clock_is_invalid_and_rejected(invalid: float):
    frame = complete_frame()
    frame["SessionTime"] = invalid

    sample = normalize(frame)

    assert sample.session.session_time_s.presence is Presence.INVALID
    assert sample.session.session_time_s.value is None
    assert sample.quality.status.value is QualityStatus.REJECTED
    assert "CORE_INVALID:SessionTime" in sample.quality.issues.value
    assert "NaN" not in sample.to_json_line()
    assert "Infinity" not in sample.to_json_line()


def test_non_finite_optional_value_is_invalid_without_becoming_zero():
    frame = complete_frame()
    frame["FuelLevel"] = math.nan

    sample = normalize(frame)

    assert sample.fuel.level_l == TelemetryField.invalid(
        Provenance.SDK_DIRECT, "FuelLevel"
    )
    assert sample.quality.status.value is QualityStatus.DEGRADED
    assert "INVALID_FIELD:FuelLevel" in sample.quality.issues.value


@pytest.mark.parametrize("name", ["Lap", "LapCompleted"])
def test_negative_lap_sentinel_is_invalid_not_transition_evidence(name: str):
    frame = complete_frame()
    frame[name] = -1

    sample = normalize(frame)
    field = sample.lap.lap_number if name == "Lap" else sample.lap.laps_completed

    assert field.presence is Presence.INVALID
    assert field.value is None
    assert f"INVALID_FIELD:{name}" in sample.quality.issues.value


@pytest.mark.parametrize("name", ["SessionNum", "SessionTick", "SessionTime"])
def test_missing_core_clock_field_is_fail_closed(name: str):
    frame = complete_frame()
    del frame[name]

    sample = normalize(frame)

    assert sample.quality.status.value is QualityStatus.REJECTED
    assert f"CORE_MISSING:{name}" in sample.quality.issues.value


def test_sequential_sample_is_fresh_and_ready():
    first = normalize(complete_frame(tick=100), tick=1_000, captured=100.0)
    second = normalize(
        complete_frame(tick=101, session_time=10.01),
        tick=1_001,
        captured=100.01,
        previous=first,
    )

    assert second.quality.stale.value is False
    assert second.quality.stale.presence is Presence.PRESENT
    assert second.quality.dropped_ticks.value == 0
    assert second.quality.status.value is QualityStatus.READY
    assert second.quality.issues.value == ()


def test_unchanged_ticks_are_stale_and_rejected():
    first = normalize(complete_frame(tick=100), tick=1_000, captured=100.0)
    second = normalize(
        complete_frame(tick=100, session_time=10.0),
        tick=1_000,
        captured=100.1,
        previous=first,
    )

    assert second.quality.stale.value is True
    assert second.quality.status.value is QualityStatus.REJECTED
    assert "SOURCE_STALE" in second.quality.issues.value


def test_large_capture_gap_is_stale_even_when_ticks_advance():
    first = normalize(complete_frame(tick=100), tick=1_000, captured=100.0)
    second = normalize(
        complete_frame(tick=101, session_time=10.01),
        tick=1_001,
        captured=100.75,
        previous=first,
        stale_after_s=0.5,
    )

    assert second.quality.stale.value is True
    assert second.quality.status.value is QualityStatus.REJECTED


def test_tick_gap_records_exact_drop_and_degrades_sample():
    first = normalize(complete_frame(tick=100), tick=1_000, captured=100.0)
    second = normalize(
        complete_frame(tick=104, session_time=10.04),
        tick=1_004,
        captured=100.04,
        previous=first,
    )

    assert second.quality.stale.value is False
    assert second.quality.dropped_ticks.value == 3
    assert second.quality.dropped_ticks.provenance is Provenance.DERIVED
    assert second.quality.status.value is QualityStatus.DEGRADED
    assert "DROPPED_TICKS:3" in second.quality.issues.value


def test_sdk_buffer_gap_is_preserved_when_session_tick_underreports_drop():
    first = normalize(complete_frame(tick=100), tick=1_000, captured=100.0)
    second = normalize(
        complete_frame(tick=101, session_time=10.01),
        tick=1_002,
        captured=100.01,
        previous=first,
    )

    assert second.quality.dropped_ticks.value == 1
    assert second.quality.dropped_ticks.source_fields == (
        "SessionTick",
        "buffer_tick",
    )
    assert "TICK_DELTA_DISAGREEMENT" in second.quality.issues.value
    assert "DROPPED_TICKS:1" in second.quality.issues.value


def test_tick_regression_rejects_without_reporting_a_negative_drop():
    first = normalize(complete_frame(tick=100), tick=1_000, captured=100.0)
    second = normalize(
        complete_frame(tick=99, session_time=9.99),
        tick=999,
        captured=100.01,
        previous=first,
    )

    assert second.quality.stale.value is True
    assert second.quality.dropped_ticks.presence is Presence.INVALID
    assert second.quality.dropped_ticks.value is None
    assert second.quality.status.value is QualityStatus.REJECTED
    assert "SESSION_TICK_REGRESSION" in second.quality.issues.value


def test_session_boundary_does_not_invent_drop_or_freshness():
    first = normalize(complete_frame(tick=100), tick=1_000, captured=100.0)
    next_frame = complete_frame(tick=1, session_time=0.0)
    next_frame["SessionNum"] = 2

    second = normalize(next_frame, tick=1, captured=100.01, previous=first)

    assert second.quality.stale.presence is Presence.MISSING
    assert second.quality.dropped_ticks.presence is Presence.MISSING
    assert second.quality.status.value is QualityStatus.DEGRADED
    assert "SESSION_BOUNDARY" in second.quality.issues.value


def test_explicit_session_id_change_is_boundary_even_when_session_num_matches():
    first = normalize(complete_frame(tick=100), tick=1_000, captured=100.0)
    second = normalize(
        complete_frame(tick=1, session_time=0.0),
        session_id="subsession-next",
        tick=1,
        captured=100.01,
        previous=first,
    )

    assert second.quality.stale.presence is Presence.MISSING
    assert second.quality.dropped_ticks.presence is Presence.MISSING
    assert second.quality.status.value is QualityStatus.DEGRADED
    assert second.quality.issues.value == ("SESSION_BOUNDARY",)


def test_source_kind_change_is_boundary_not_tick_regression():
    first = normalize(complete_frame(tick=100), tick=1_000, captured=100.0)
    second = normalize(
        complete_frame(tick=1, session_time=0.0),
        source_kind=SourceKind.REPLAY_SDK_PROXY,
        tick=1,
        captured=100.01,
        previous=first,
    )

    assert second.quality.stale.presence is Presence.MISSING
    assert second.quality.status.value is QualityStatus.DEGRADED
    assert second.quality.issues.value == ("SOURCE_BOUNDARY",)


def test_opponent_mapping_excludes_player_and_preserves_car_indices():
    sample = normalize(complete_frame())

    assert sample.opponents.presence is Presence.PRESENT
    assert sample.opponents.player_car_idx.value == 1
    assert tuple(entry.car_idx.value for entry in sample.opponents.entries) == (0, 2)
    assert tuple(entry.lap_distance_pct.value for entry in sample.opponents.entries) == (
        0.20,
        0.90,
    )
    assert sample.opponents.entries[1].on_pit_road.value is True


def test_mismatched_opponent_arrays_degrade_without_emitting_shifted_entries():
    frame = complete_frame()
    frame["CarIdxLapDistPct"] = [0.20, 0.25]

    sample = normalize(frame)

    assert sample.opponents.presence is Presence.INVALID
    assert sample.opponents.entries == ()
    assert "OPPONENT_ARRAY_LENGTH_MISMATCH" in sample.opponents.issues
    assert sample.quality.status.value is QualityStatus.DEGRADED


def test_mismatched_opponent_arrays_can_be_rejected_strictly():
    frame = complete_frame()
    frame["CarIdxLapDistPct"] = [0.20, 0.25]

    with pytest.raises(
        TelemetryNormalizationError, match="OPPONENT_ARRAY_LENGTH_MISMATCH"
    ):
        normalize(frame, opponent_error_policy="reject")


@pytest.mark.parametrize("player_idx", [-1, 3, 99])
def test_out_of_range_player_car_idx_invalidates_opponent_mapping(player_idx: int):
    frame = complete_frame()
    frame["PlayerCarIdx"] = player_idx

    sample = normalize(frame)

    assert sample.opponents.presence is Presence.INVALID
    assert sample.opponents.entries == ()
    assert "PLAYER_CAR_IDX_OUT_OF_RANGE" in sample.opponents.issues


def test_missing_opponent_schema_is_explicitly_missing():
    frame = complete_frame()
    for name in tuple(frame):
        if name.startswith("CarIdx"):
            del frame[name]

    sample = normalize(frame)

    assert sample.opponents.presence is Presence.MISSING
    assert sample.opponents.provenance is Provenance.UNKNOWN
    assert sample.opponents.entries == ()


def test_bad_opponent_element_does_not_shift_other_car_indices():
    frame = complete_frame()
    frame["CarIdxLapDistPct"] = [math.nan, 0.25, 0.90]

    sample = normalize(frame)

    assert sample.opponents.presence is Presence.PRESENT
    assert tuple(entry.car_idx.value for entry in sample.opponents.entries) == (0, 2)
    assert sample.opponents.entries[0].lap_distance_pct.presence is Presence.INVALID
    assert sample.opponents.entries[1].lap_distance_pct.value == 0.90
    assert "INVALID_OPPONENT_VALUE:CarIdxLapDistPct[0]" in sample.opponents.issues
    assert sample.quality.status.value is QualityStatus.DEGRADED


def test_each_opponent_array_length_is_checked_against_declared_count():
    for name in (
        "CarIdxLap",
        "CarIdxLapCompleted",
        "CarIdxLapDistPct",
        "CarIdxOnPitRoad",
        "CarIdxTrackSurface",
    ):
        frame = complete_frame()
        frame[name] = frame[name][:-1]
        sample = normalize(frame)
        assert sample.opponents.presence is Presence.INVALID, name
        assert "OPPONENT_ARRAY_LENGTH_MISMATCH" in sample.opponents.issues


def test_opponent_arrays_are_checked_against_expected_car_count():
    sample = normalize(complete_frame(), expected_car_count=4)

    assert sample.opponents.presence is Presence.INVALID
    assert "OPPONENT_ARRAY_EXPECTED_LENGTH_MISMATCH" in sample.opponents.issues


def test_serialization_is_deterministic_across_mapping_order_and_negative_zero():
    first_frame = complete_frame()
    first_frame["Brake"] = -0.0
    second_frame = dict(reversed(tuple(first_frame.items())))
    second_frame["Brake"] = 0.0

    first = normalize(first_frame)
    second = normalize(second_frame)

    assert first.to_json_line() == second.to_json_line()
    assert first.to_jsonl() == f"{first.to_json_line()}\n"
    assert first.to_parquet_row() == second.to_parquet_row()
    assert json.loads(first.to_json_line())["controls"]["brake"]["value"] == 0.0


def test_parquet_row_contains_only_scalar_values_and_explicit_metadata():
    row = normalize(complete_frame()).to_parquet_row()

    assert row["fuel.level_l__value"] == 54.5
    assert row["fuel.level_l__presence"] == "PRESENT"
    assert row["fuel.level_l__provenance"] == "SDK_DIRECT"
    assert row["fuel.level_l__source_fields_json"] == '["FuelLevel"]'
    assert row["incidents.player_car_my_incident_count__value"] == 2
    assert row["environment.track_temp_crew_c__value"] == 26.2
    assert row["environment.track_temp_crew_c__source_fields_json"] == '["TrackTempCrew"]'
    assert row["environment.relative_humidity_fraction__value"] == 0.4565
    assert row["tires.player_tire_compound__value"] == 0
    assert row["tires.tire_sets_used__value"] == 1
    assert row["parquet_schema_version"] == TELEMETRY_PARQUET_SCHEMA_VERSION
    assert isinstance(row["opponents__entries_json"], str)
    assert all(
        value is None or isinstance(value, (str, int, float, bool))
        for value in row.values()
    )


def test_versioned_parquet_schema_stabilizes_null_first_row(tmp_path):
    import polars as pl

    first = normalize(complete_frame(tick=100), tick=1_000, captured=100.0)
    second = normalize(
        complete_frame(tick=101, session_time=10.01),
        tick=1_001,
        captured=100.01,
        previous=first,
    )
    schema = telemetry_parquet_schema()
    table = pl.DataFrame(
        [first.to_parquet_row(), second.to_parquet_row()],
        schema=schema,
        orient="row",
        strict=True,
    )

    assert table.schema["quality.stale__value"] == pl.Boolean
    assert table.schema["quality.dropped_ticks__value"] == pl.Int64
    assert table.schema["environment.track_temp_crew_c__value"] == pl.Float64
    assert table.schema["environment.weather_type__value"] == pl.Int64
    assert table.schema["tires.player_tire_compound__value"] == pl.Int64
    assert table["quality.stale__value"].to_list() == [None, False]
    path = tmp_path / "telemetry.parquet"
    table.write_parquet(path)
    restored = pl.read_parquet(path)
    assert restored.schema == schema


def test_field_constructor_rejects_non_finite_present_values():
    with pytest.raises(ValueError, match="NaN or infinity"):
        TelemetryField.present(math.nan, Provenance.SDK_DIRECT, "Speed")


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("stale_after_s", 0.0),
        ("stale_after_s", math.nan),
        ("expected_car_count", 0),
        ("opponent_error_policy", "guess"),
    ],
)
def test_invalid_normalizer_configuration_is_rejected(parameter: str, value: object):
    with pytest.raises((TypeError, ValueError)):
        normalize(complete_frame(), **{parameter: value})
