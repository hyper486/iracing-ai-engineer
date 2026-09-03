from __future__ import annotations

import json
import math
import struct
from dataclasses import replace
from types import SimpleNamespace
from urllib import error as urlerror

import irsdk
import pytest

from iracing_ai_engineer import cli
from iracing_ai_engineer.sdk_probe import (
    DRIVING_CONTROL_FIELDS,
    ENVIRONMENT_FIELDS,
    FIELD_EXPECTED_TYPES,
    FUEL_FIELDS,
    LAP_POSITION_FIELDS,
    OPPONENT_ARRAY_FIELDS,
    OPPONENT_FIELDS,
    RACE_STRATEGY_FIELDS,
    RACE_STRATEGY_REMAINING_FIELDS,
    REPLAY_FIELDS,
    SDK_TYPE_NAMES,
    SESSION_CLOCK_FIELDS,
    TARGET_FIELDS,
    TIRE_FIELDS,
    ConnectionMeta,
    RawSdkFrame,
    SdkProbeConsistencyError,
    SdkProbeUnavailable,
    VariableDescriptor,
    WindowsPyirsdkTransport,
    _bind_frame_sim_mode,
    _parse_sdk_layout,
    _stable_sdk_layout,
    build_probe_report,
    probe_live_sdk,
)


def descriptor(name: str, count: int = 1) -> VariableDescriptor:
    type_code = min(FIELD_EXPECTED_TYPES[name])
    return VariableDescriptor(
        name=name,
        type_code=type_code,
        dtype=SDK_TYPE_NAMES[type_code],
        offset=0,
        count=count,
        count_as_time=False,
        unit="",
        description=name,
    )


def connection(*, connected: bool = True, variable_count: int = 100) -> ConnectionMeta:
    return ConnectionMeta(
        startup_ok=connected,
        initialized=connected,
        connected=connected,
        header_version=2,
        raw_header_status=1 if connected else 0,
        tick_rate_hz=60,
        variable_count=variable_count,
        buffer_count=3,
        buffer_len=1000,
    )


def frame(
    tick: int, values: dict[str, object], *, sim_mode: str | None = None
) -> RawSdkFrame:
    return RawSdkFrame(
        buffer_tick=tick,
        session_info_update=1,
        values=values,
        sim_mode_raw=sim_mode,
        captured_monotonic_s=tick / 60.0,
    )


def report(
    *,
    sim_mode: str | None,
    names: tuple[str, ...],
    frames: tuple[RawSdkFrame, ...],
    counts: dict[str, int] | None = None,
    ended_connected: bool | None = None,
    propagate_sim_mode: bool = True,
    probe_start_monotonic_s: float | None = None,
    probe_end_monotonic_s: float | None = None,
) -> dict[str, object]:
    counts = counts or {}
    descriptors = tuple(descriptor(name, counts.get(name, 1)) for name in names)
    if propagate_sim_mode:
        frames = tuple(
            replace(item, sim_mode_raw=sim_mode)
            if item.sim_mode_raw is None
            else item
            for item in frames
        )
    if frames:
        if probe_start_monotonic_s is None:
            probe_start_monotonic_s = frames[0].captured_monotonic_s
        if probe_end_monotonic_s is None:
            probe_end_monotonic_s = frames[-1].captured_monotonic_s
    return build_probe_report(
        connection=connection(variable_count=len(descriptors)),
        descriptors=descriptors,
        frames=frames,
        sim_mode_raw=sim_mode,
        sample_duration_s=3.0,
        platform_name="test-platform",
        include_full_schema=False,
        ended_connected=ended_connected,
        probe_start_monotonic_s=probe_start_monotonic_s,
        probe_end_monotonic_s=probe_end_monotonic_s,
    )


def all_target_names() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            REPLAY_FIELDS
            + SESSION_CLOCK_FIELDS
            + LAP_POSITION_FIELDS
            + DRIVING_CONTROL_FIELDS
            + FUEL_FIELDS
            + RACE_STRATEGY_FIELDS
            + RACE_STRATEGY_REMAINING_FIELDS
            + OPPONENT_FIELDS
            + ENVIRONMENT_FIELDS
            + TIRE_FIELDS
            + ("IsOnTrackCar",)
        )
    )


def values_for(names: tuple[str, ...], *, replay: bool, tick: int) -> dict[str, object]:
    values: dict[str, object] = {}
    for name in names:
        type_code = min(FIELD_EXPECTED_TYPES[name])
        if type_code == 1:
            values[name] = False
        elif type_code in {2, 3}:
            values[name] = tick
        else:
            values[name] = float(tick)
    values.update(
        {
            "SessionNum": 1,
            "PlayerCarIdx": 0,
            "IsOnTrack": not replay,
            "IsOnTrackCar": not replay,
            "IsReplayPlaying": replay,
        }
    )
    for name in OPPONENT_ARRAY_FIELDS:
        if name in names:
            if min(FIELD_EXPECTED_TYPES[name]) == 1:
                values[name] = [False, True]
            elif min(FIELD_EXPECTED_TYPES[name]) == 2:
                values[name] = [tick, tick + 1]
            else:
                values[name] = [0.1 + tick, 0.2 + tick]
    return values


def sdk_memory(
    *,
    descriptor_offset: int = 0,
    first_buffer_offset: int = 512,
    session_info_len: int = 32,
    session_info_offset: int = 272,
) -> bytearray:
    memory = bytearray(2048)
    struct_values = (
        2,
        1,
        60,
        1,
        session_info_len,
        session_info_offset,
        1,
        128,
        3,
        128,
    )
    struct.pack_into("<10i", memory, 0, *struct_values)
    memory[44] = 0
    for index, offset in enumerate((first_buffer_offset, 768, 1024)):
        struct.pack_into("<4i", memory, 48 + index * 16, 10 + index, offset, 10 + index, 0)
    struct.pack_into("<3i?", memory, 128, 4, descriptor_offset, 1, False)
    memory[144:149] = b"Speed"
    memory[176:181] = b"speed"
    memory[240:243] = b"m/s"
    return memory


def test_replay_blocks_driving_and_fuel_even_when_every_field_is_readable():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    frames = (
        frame(10, values_for(names, replay=True, tick=10)),
        frame(11, values_for(names, replay=True, tick=11)),
    )

    payload = report(sim_mode="replay", names=names, frames=frames, counts=counts)

    assert payload["context"]["sim_source_mode"] == "REPLAY_FILE"
    assert payload["capabilities"]["sdk_connection"]["status"] == "READY"
    assert payload["capabilities"]["replay_control_only"]["status"] == "READY"
    assert payload["capabilities"]["driving_controls"]["status"] == "BLOCKED"
    assert payload["capabilities"]["fuel_direct"]["status"] == "BLOCKED"
    assert "REPLAY_FIELDS_ARE_PROBE_ONLY_NOT_GROUND_TRUTH" in payload["probe_warnings"]


def test_full_in_car_context_can_make_complete_capabilities_ready():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    frames = (
        frame(20, values_for(names, replay=False, tick=20)),
        frame(21, values_for(names, replay=False, tick=21)),
    )

    payload = report(sim_mode="full", names=names, frames=frames, counts=counts)

    assert payload["context"]["player_control_state"] == "IN_CAR_PHYSICS"
    assert payload["capabilities"]["driving_controls"]["status"] == "READY"
    assert payload["capabilities"]["fuel_direct"]["status"] == "READY"
    assert payload["capabilities"]["opponent_tracking"]["status"] == "READY"
    assert payload["capabilities"]["replay_control_only"]["status"] == "NOT_APPLICABLE"


def test_unknown_context_blocks_privileged_capabilities():
    names = all_target_names()
    frames = (
        frame(30, values_for(names, replay=False, tick=30)),
        frame(31, values_for(names, replay=False, tick=31)),
    )

    payload = report(sim_mode=None, names=names, frames=frames)

    assert payload["context"]["sim_source_mode"] == "UNKNOWN"
    assert payload["capabilities"]["driving_controls"]["status"] == "BLOCKED"
    assert payload["capabilities"]["fuel_direct"]["status"] == "BLOCKED"


def test_initial_full_mode_does_not_authorize_frames_with_missing_mode():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    frames = (
        frame(32, values_for(names, replay=False, tick=32), sim_mode=None),
        frame(33, values_for(names, replay=False, tick=33), sim_mode=None),
    )

    payload = report(
        sim_mode="full",
        names=names,
        frames=frames,
        counts=counts,
        propagate_sim_mode=False,
    )

    assert payload["context"]["sim_source_mode"] == "UNKNOWN"
    assert payload["capabilities"]["sdk_connection"]["status"] == "READY"
    assert payload["capabilities"]["driving_controls"]["status"] == "BLOCKED"
    assert payload["capabilities"]["fuel_direct"]["status"] == "BLOCKED"
    assert payload["capabilities"]["race_strategy_core"]["status"] == "BLOCKED"
    assert payload["capabilities"]["replay_control_only"]["status"] == "BLOCKED"


def test_frame_rejects_sim_mode_from_a_different_session_info_update():
    raw_frame = RawSdkFrame(
        buffer_tick=34,
        session_info_update=10,
        values={"IsOnTrack": True},
    )

    bound = _bind_frame_sim_mode(raw_frame, "full", 11)

    assert bound.sim_mode_raw is None


def test_zero_is_readable_but_non_finite_value_is_invalid():
    names = ("SessionTime", "SessionTick", "SessionNum")
    frames = (
        frame(1, {"SessionTime": 0.0, "SessionTick": 0, "SessionNum": 0}),
        frame(2, {"SessionTime": math.nan, "SessionTick": 1, "SessionNum": 0}),
    )

    payload = report(sim_mode="full", names=names, frames=frames)

    assert payload["field_availability"]["SessionTick"]["first_scalar"] == 0
    assert payload["field_availability"]["SessionTime"]["status"] == "INVALID"
    assert payload["capabilities"]["session_clock"]["status"] == "BLOCKED"


def test_stale_buffer_ticks_block_sdk_connection_even_with_readable_values():
    names = SESSION_CLOCK_FIELDS
    values = {"SessionTime": 1.0, "SessionTick": 60, "SessionNum": 1}
    frames = (frame(99, values), frame(99, values))

    payload = report(sim_mode="full", names=names, frames=frames)

    assert payload["connection"]["stale"] is True
    assert payload["capabilities"]["sdk_connection"]["status"] == "BLOCKED"


def test_opponent_array_mismatch_is_blocked():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    counts["CarIdxLapDistPct"] = 3
    frames = (
        frame(40, values_for(names, replay=False, tick=40)),
        frame(41, values_for(names, replay=False, tick=41)),
    )

    payload = report(sim_mode="full", names=names, frames=frames, counts=counts)

    assert payload["capabilities"]["opponent_tracking"]["status"] == "BLOCKED"
    assert "OPPONENT_ARRAY_LENGTH_MISMATCH" in payload["probe_warnings"]


def test_session_change_blocks_otherwise_ready_capabilities():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    first = values_for(names, replay=False, tick=50)
    second = values_for(names, replay=False, tick=51)
    second["SessionNum"] = 2
    frames = (frame(50, first), frame(51, second))

    payload = report(sim_mode="full", names=names, frames=frames, counts=counts)

    assert "SESSION_IDENTITY_CHANGED_DURING_PROBE" in payload["probe_warnings"]
    assert payload["capabilities"]["driving_controls"]["status"] == "BLOCKED"


def test_same_session_clock_reset_starts_a_new_unstable_epoch():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    first = values_for(names, replay=False, tick=54)
    second = values_for(names, replay=False, tick=55)
    first.update({"SessionNum": 1, "SessionTick": 1000, "SessionTime": 100.0})
    second.update({"SessionNum": 1, "SessionTick": 1, "SessionTime": 0.1})

    payload = report(
        sim_mode="full",
        names=names,
        frames=(frame(54, first), frame(55, second)),
        counts=counts,
    )

    assert "SESSION_CLOCK_RESET_DURING_PROBE" in payload["probe_warnings"]
    assert payload["connection"]["stable_session_epoch_tick_count"] == 1
    assert payload["capabilities"]["session_clock"]["status"] == "BLOCKED"
    assert payload["capabilities"]["race_strategy_core"]["status"] == "BLOCKED"


def test_clock_reset_can_recover_after_two_healthy_new_epoch_ticks():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    values = [values_for(names, replay=False, tick=tick) for tick in (56, 57, 58)]
    values[0].update({"SessionNum": 1, "SessionTick": 1000, "SessionTime": 100.0})
    values[1].update({"SessionNum": 1, "SessionTick": 1, "SessionTime": 0.1})
    values[2].update({"SessionNum": 1, "SessionTick": 2, "SessionTime": 0.2})

    payload = report(
        sim_mode="full",
        names=names,
        frames=tuple(
            frame(tick, item)
            for tick, item in zip((56, 57, 58), values, strict=True)
        ),
        counts=counts,
    )

    assert "SESSION_CLOCK_RESET_DURING_PROBE" in payload["probe_warnings"]
    assert payload["connection"]["stable_session_epoch_tick_count"] == 2
    assert payload["capabilities"]["session_clock"]["status"] == "READY"
    assert payload["capabilities"]["race_strategy_core"]["status"] == "READY"


def test_one_tick_after_context_change_is_not_a_stable_ready_epoch():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    replay_values = values_for(names, replay=True, tick=60)
    live_values = values_for(names, replay=False, tick=61)
    frames = (
        frame(60, replay_values, sim_mode="full"),
        frame(61, live_values, sim_mode="full"),
    )

    payload = report(sim_mode="full", names=names, frames=frames, counts=counts)

    assert "CONTEXT_CHANGED_DURING_PROBE" in payload["probe_warnings"]
    assert payload["connection"]["stable_context_tick_count"] == 1
    assert payload["capabilities"]["sdk_connection"]["status"] == "BLOCKED"
    assert payload["capabilities"]["driving_controls"]["status"] == "BLOCKED"


def test_conflicting_replay_and_on_track_flags_block_driving():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    first = values_for(names, replay=False, tick=70)
    second = values_for(names, replay=False, tick=71)
    first["IsReplayPlaying"] = True
    second["IsReplayPlaying"] = True
    frames = (frame(70, first), frame(71, second))

    payload = report(sim_mode="full", names=names, frames=frames, counts=counts)

    assert "IS_ON_TRACK_WITH_REPLAY_PLAYING" in payload["context"]["conflicts"]
    assert payload["capabilities"]["driving_controls"]["status"] == "BLOCKED"


def test_disconnect_after_advancing_ticks_blocks_all_live_capabilities():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    frames = (
        frame(80, values_for(names, replay=False, tick=80)),
        frame(81, values_for(names, replay=False, tick=81)),
    )

    payload = report(
        sim_mode="full",
        names=names,
        frames=frames,
        counts=counts,
        ended_connected=False,
    )

    assert "DISCONNECTED_DURING_PROBE" in payload["probe_warnings"]
    assert payload["capabilities"]["sdk_connection"]["status"] == "BLOCKED"
    assert payload["capabilities"]["fuel_direct"]["status"] == "BLOCKED"


def test_source_stale_at_probe_end_blocks_ready():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    frames = (
        frame(90, values_for(names, replay=False, tick=90)),
        frame(91, values_for(names, replay=False, tick=91)),
    )

    payload = report(
        sim_mode="full",
        names=names,
        frames=frames,
        counts=counts,
        probe_end_monotonic_s=92.0,
    )

    assert "SOURCE_STALE_AT_END" in payload["probe_warnings"]
    assert payload["capabilities"]["sdk_connection"]["status"] == "BLOCKED"


def test_long_gap_during_probe_blocks_connection_even_when_end_is_fresh():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    frames = (
        replace(
            frame(1, values_for(names, replay=False, tick=1)),
            captured_monotonic_s=0.0,
        ),
        replace(
            frame(200, values_for(names, replay=False, tick=200)),
            captured_monotonic_s=2.9,
        ),
    )

    payload = report(
        sim_mode="full",
        names=names,
        frames=frames,
        counts=counts,
        probe_start_monotonic_s=0.0,
        probe_end_monotonic_s=3.0,
    )

    assert payload["connection"]["max_inter_tick_gap_s"] == 2.9
    assert "SOURCE_GAP_DURING_PROBE" in payload["probe_warnings"]
    assert payload["capabilities"]["sdk_connection"]["status"] == "BLOCKED"
    assert payload["capabilities"]["driving_controls"]["status"] == "BLOCKED"


def test_missing_continuity_boundaries_fail_closed():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    descriptors = tuple(descriptor(name, counts.get(name, 1)) for name in names)
    frames = (
        frame(
            201,
            values_for(names, replay=False, tick=201),
            sim_mode="full",
        ),
        frame(
            202,
            values_for(names, replay=False, tick=202),
            sim_mode="full",
        ),
    )

    payload = build_probe_report(
        connection=connection(variable_count=len(descriptors)),
        descriptors=descriptors,
        frames=frames,
        sim_mode_raw="full",
        sample_duration_s=3.0,
        platform_name="test-platform",
        include_full_schema=False,
    )

    assert payload["connection"]["continuity_checked"] is False
    assert "SOURCE_CONTINUITY_UNVERIFIABLE" in payload["probe_warnings"]
    assert payload["capabilities"]["sdk_connection"]["status"] == "BLOCKED"


def test_wrong_target_type_and_shape_are_invalid_not_ready():
    names = all_target_names()
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    descriptors = tuple(descriptor(name, counts.get(name, 1)) for name in names)
    descriptors = tuple(
        replace(item, type_code=0, dtype="char", count=2)
        if item.name == "Throttle"
        else item
        for item in descriptors
    )
    frames = (
        frame(100, values_for(names, replay=False, tick=100)),
        frame(101, values_for(names, replay=False, tick=101)),
    )

    payload = build_probe_report(
        connection=connection(variable_count=len(descriptors)),
        descriptors=descriptors,
        frames=frames,
        sim_mode_raw="full",
        sample_duration_s=3.0,
        platform_name="test-platform",
        include_full_schema=False,
    )

    assert payload["field_availability"]["Throttle"]["status"] == "INVALID"
    assert payload["capabilities"]["driving_controls"]["status"] == "BLOCKED"


def test_condition_fields_are_live_targets_with_scalar_sdk_types():
    assert set(ENVIRONMENT_FIELDS + TIRE_FIELDS) <= set(TARGET_FIELDS)
    assert all(
        FIELD_EXPECTED_TYPES[name] == frozenset({2})
        for name in ("WeatherType", "WeatherVersion", "Skies") + TIRE_FIELDS
    )
    assert all(
        FIELD_EXPECTED_TYPES[name] == frozenset({4, 5})
        for name in ENVIRONMENT_FIELDS
        if name not in {"WeatherType", "WeatherVersion", "Skies"}
    )

    descriptors = tuple(descriptor(name) for name in ENVIRONMENT_FIELDS + TIRE_FIELDS)
    frames = (
        frame(120, values_for(ENVIRONMENT_FIELDS + TIRE_FIELDS, replay=False, tick=120)),
        frame(121, values_for(ENVIRONMENT_FIELDS + TIRE_FIELDS, replay=False, tick=121)),
    )
    payload = build_probe_report(
        connection=connection(variable_count=len(descriptors)),
        descriptors=descriptors,
        frames=frames,
        sim_mode_raw="full",
        sample_duration_s=3.0,
        platform_name="test-platform",
        include_full_schema=False,
        probe_start_monotonic_s=frames[0].captured_monotonic_s,
        probe_end_monotonic_s=frames[-1].captured_monotonic_s,
    )

    assert all(
        payload["field_availability"][name]["status"]
        == "OBSERVED_SOURCE_ADVANCING"
        for name in ENVIRONMENT_FIELDS + TIRE_FIELDS
    )


def test_race_strategy_requires_a_remaining_distance_field():
    names = tuple(
        name for name in all_target_names() if name not in RACE_STRATEGY_REMAINING_FIELDS
    )
    counts = {name: 2 for name in OPPONENT_ARRAY_FIELDS}
    frames = (
        frame(110, values_for(names, replay=False, tick=110)),
        frame(111, values_for(names, replay=False, tick=111)),
    )

    payload = report(sim_mode="full", names=names, frames=frames, counts=counts)

    assert payload["capabilities"]["race_strategy_core"]["status"] == "BLOCKED"
    assert "NO_REMAINING_DISTANCE_FIELD" in payload["capabilities"]["race_strategy_core"][
        "reasons"
    ]


def test_non_windows_cli_unavailable_stdout_is_one_json_document(monkeypatch, capsys):
    monkeypatch.setattr("iracing_ai_engineer.sdk_probe.platform.system", lambda: "Darwin")

    exit_code = cli.main(
        ["sdk-probe", "--wait-seconds", "0", "--sample-seconds", "0.1"]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(captured.out)["error"] == "SDK_UNAVAILABLE"
    assert "requires Windows" in captured.err


def test_cli_rejects_nan_duration_before_transport(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._parser().parse_args(["sdk-probe", "--wait-seconds", "nan"])

    assert exc_info.value.code == 2
    assert "finite number" in capsys.readouterr().err


def test_transport_rejects_descriptor_outside_frame_buffer():
    memory = sdk_memory(descriptor_offset=128)
    transport = object.__new__(WindowsPyirsdkTransport)
    transport._startup_layout = _stable_sdk_layout(memory)
    transport._client = SimpleNamespace(
        _shared_mem=memory,
        _var_headers_dict={},
    )

    with pytest.raises(SdkProbeConsistencyError, match="invalid SDK variable descriptor"):
        transport.descriptors()


def test_raw_layout_rejects_overlapping_sections():
    with pytest.raises(SdkProbeConsistencyError, match="overlap"):
        _parse_sdk_layout(sdk_memory(first_buffer_offset=128))


def test_raw_layout_rejects_invalid_zero_length_session_info_location():
    with pytest.raises(SdkProbeConsistencyError, match="invalid SDK sampling layout"):
        _parse_sdk_layout(
            sdk_memory(session_info_len=0, session_info_offset=-1)
        )


def test_zero_length_session_info_is_transiently_unavailable():
    with pytest.raises(SdkProbeUnavailable, match="SessionInfo is not ready"):
        _parse_sdk_layout(sdk_memory(session_info_len=0, session_info_offset=272))


def test_sim_mode_accepts_unchanged_cached_weekend_info():
    class CachedWeekendClient:
        session_info_update = 5

        @staticmethod
        def _get_session_info_binary(key):
            assert key == "WeekendInfo"
            return b"WeekendInfo:\n SimMode: full"

        def __getitem__(self, key):
            assert key == "WeekendInfo"
            return {"SimMode": "full"}

    transport = object.__new__(WindowsPyirsdkTransport)
    transport._client = CachedWeekendClient()

    assert transport.sim_mode() == ("full", 5)


def snapshot_transport(raw_session_info: bytes) -> WindowsPyirsdkTransport:
    memory = sdk_memory(session_info_len=len(raw_session_info))
    session_info_offset = 272
    memory[
        session_info_offset : session_info_offset + len(raw_session_info)
    ] = raw_session_info
    transport = object.__new__(WindowsPyirsdkTransport)
    transport._irsdk = irsdk
    transport._startup_layout = _stable_sdk_layout(memory)
    transport._client = SimpleNamespace(_shared_mem=memory)
    return transport


def test_session_info_snapshot_returns_one_stable_full_mapping():
    raw = (
        b"---\n"
        b"WeekendInfo:\n"
        b" Encoding: UTF8\n"
        b" SimMode: full\n"
        b"DriverInfo:\n"
        b' DriverSetupName: setup "A"\n'
        b"SessionInfo:\n"
        b" Sessions: []\n"
    )
    transport = snapshot_transport(raw)

    payload, update = transport.session_info_snapshot()

    assert update == 1
    assert payload == {
        "WeekendInfo": {"Encoding": "UTF8", "SimMode": "full"},
        "DriverInfo": {"DriverSetupName": 'setup "A"'},
        "SessionInfo": {"Sessions": []},
    }


def test_session_info_snapshot_matches_pyirsdk_cp1252_cleaning():
    raw = (
        b"---\n"
        b"WeekendInfo:\n"
        b" SimMode: full\n"
        b"DriverInfo:\n"
        b' UserName: Andr\xe9 "Ace"\n'
        b"\x00\x00"
    )
    transport = snapshot_transport(raw)

    payload, update = transport.session_info_snapshot()

    assert update == 1
    assert payload == {
        "WeekendInfo": {"SimMode": "full"},
        "DriverInfo": {"UserName": 'André "Ace"'},
    }


def test_session_info_snapshot_reuses_only_same_update_mapping():
    raw = b"---\nWeekendInfo:\n SimMode: full\n"
    transport = snapshot_transport(raw)
    transport._client.session_info_update = 1
    parse = transport._parse_session_info_snapshot
    parse_calls = 0

    def counted_parse(payload: bytes):
        nonlocal parse_calls
        parse_calls += 1
        return parse(payload)

    transport._parse_session_info_snapshot = counted_parse

    first = transport.session_info_snapshot()
    second = transport.session_info_snapshot()

    assert first == second
    assert parse_calls == 1


def test_session_info_snapshot_rejects_update_changes_after_finite_retries():
    raw = b"---\nWeekendInfo:\n SimMode: full\n"
    transport = snapshot_transport(raw)
    base = transport._startup_layout
    assert base is not None
    calls = 0

    def changing_layout():
        nonlocal calls
        calls += 1
        return replace(base, session_info_update=1 + calls % 2)

    transport._assert_schema_current = changing_layout

    assert transport.session_info_snapshot() == (None, None)
    assert calls == 9


def test_session_info_snapshot_rejects_byte_changes_after_finite_retries():
    first = b"---\nWeekendInfo:\n SimMode: full\n"
    second = b"---\nWeekendInfo:\n SimMode: race\n"
    assert len(first) == len(second)
    transport = snapshot_transport(first)
    base = transport._startup_layout
    assert base is not None

    class AlternatingSessionMemory:
        def __init__(self, backing: bytearray) -> None:
            self.backing = backing
            self.session_reads = 0

        def __getitem__(self, key):
            if key == slice(base.session_info_offset, base.session_info_offset + len(first)):
                self.session_reads += 1
                return first if self.session_reads % 2 else second
            return self.backing[key]

    memory = AlternatingSessionMemory(transport._client._shared_mem)
    transport._client._shared_mem = memory
    transport._assert_schema_current = lambda: base

    assert transport.session_info_snapshot() == (None, None)
    assert memory.session_reads == 6


@pytest.mark.parametrize(
    "raw",
    (
        b"---\n- not\n- a\n- mapping\n",
        b"---\nWeekendInfo: [unterminated\n",
    ),
)
def test_session_info_snapshot_rejects_invalid_root_or_yaml(raw: bytes):
    transport = snapshot_transport(raw)

    assert transport.session_info_snapshot() == (None, None)


def test_explicit_transport_close_surfaces_resource_failure():
    class SharedMemory:
        def close(self) -> None:
            pass

    class Client:
        def __init__(self) -> None:
            self._data_valid_event = None
            self._shared_mem = SharedMemory()

        def unfreeze_var_buffer_latest(self) -> None:
            pass

        def shutdown(self) -> None:
            raise OSError("close failed")

    transport = object.__new__(WindowsPyirsdkTransport)
    transport._client = Client()
    transport._startup_layout = object()

    with pytest.raises(SdkProbeConsistencyError, match="failed to close"):
        transport.close()

    assert transport._startup_layout is None


def test_frozen_read_always_unfreezes_when_copy_raises():
    class FailingClient:
        def __init__(self) -> None:
            self.unfreeze_calls = 0
            self.session_info_update = 1

        def unfreeze_var_buffer_latest(self) -> None:
            self.unfreeze_calls += 1

    client = FailingClient()
    transport = object.__new__(WindowsPyirsdkTransport)
    transport._client = client
    transport._freeze_stable_latest = lambda: (_ for _ in ()).throw(
        OSError("sim disappeared")
    )

    with pytest.raises(OSError, match="sim disappeared"):
        transport.read_frozen(("Speed",))

    assert client.unfreeze_calls == 1


def test_stable_copy_rejects_writer_update_during_every_copy():
    class UpdatingCandidate:
        def __init__(self) -> None:
            self.tick_count = 10
            self.tick_count_begin = 10
            self._buf_offset = 128
            self.unfreeze_calls = 0

        def freeze(self) -> None:
            self.tick_count_begin += 1
            self.tick_count += 1

        def unfreeze(self) -> None:
            self.unfreeze_calls += 1

    candidate = UpdatingCandidate()
    client = SimpleNamespace(
        _header=SimpleNamespace(var_buf=(candidate,), buf_len=16),
        _shared_mem=bytearray(256),
        _wait_valid_data_event=lambda: True,
        unfreeze_var_buffer_latest=lambda: None,
    )
    transport = object.__new__(WindowsPyirsdkTransport)
    transport._client = client

    with pytest.raises(SdkProbeConsistencyError, match="stable SDK buffer"):
        transport._freeze_stable_latest()

    assert candidate.unfreeze_calls == 3


def test_startup_retries_attach_until_shared_memory_is_ready(monkeypatch):
    class RunningResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @staticmethod
        def read() -> bytes:
            return b"running:1"

    clock = [0.0]
    monkeypatch.setattr(
        "iracing_ai_engineer.sdk_probe.time.monotonic", lambda: clock[0]
    )
    monkeypatch.setattr(
        "iracing_ai_engineer.sdk_probe.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(
        "iracing_ai_engineer.sdk_probe.urlrequest.urlopen",
        lambda *args, **kwargs: RunningResponse(),
    )

    transport = object.__new__(WindowsPyirsdkTransport)
    transport._irsdk = SimpleNamespace(SIM_STATUS_URL="http://127.0.0.1/status")
    transport._client = SimpleNamespace()
    transport._startup_layout = None
    transport._close_client = lambda client: None
    calls = [0]
    header = SimpleNamespace(
        version=2,
        status=1,
        tick_rate=60,
        num_vars=3,
        num_buf=3,
        buf_len=128,
    )
    candidate = SimpleNamespace(_header=header)
    layout = SimpleNamespace()

    def attach_once():
        calls[0] += 1
        if calls[0] == 1:
            raise SdkProbeUnavailable("event not ready")
        return candidate, layout

    transport._attach_once = attach_once

    meta = transport.startup(0.5)

    assert calls[0] == 2
    assert meta.connected is True


def test_unexpected_transport_constructor_error_becomes_consistency_error(monkeypatch):
    def broken_transport():
        raise RuntimeError("broken dependency")

    monkeypatch.setattr(
        "iracing_ai_engineer.sdk_probe.WindowsPyirsdkTransport", broken_transport
    )

    with pytest.raises(SdkProbeConsistencyError, match="RuntimeError"):
        probe_live_sdk(wait_seconds=0, sample_seconds=0.1)


def test_status_timeout_emits_no_dependency_text_to_stdout(monkeypatch, capsys):
    transport = object.__new__(WindowsPyirsdkTransport)
    transport._irsdk = SimpleNamespace(SIM_STATUS_URL="http://127.0.0.1:32034/status")
    transport._client = SimpleNamespace()

    def unavailable(*args, **kwargs):
        raise urlerror.URLError("offline")

    monkeypatch.setattr("iracing_ai_engineer.sdk_probe.urlrequest.urlopen", unavailable)

    with pytest.raises(SdkProbeUnavailable, match="timed out"):
        transport.startup(0)

    assert capsys.readouterr().out == ""
