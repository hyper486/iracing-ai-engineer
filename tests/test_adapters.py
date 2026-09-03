from __future__ import annotations

import inspect
import io
import json
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

import iracing_ai_engineer.adapters as adapter_module
from iracing_ai_engineer.adapters import (
    EVENT_IDENTITY_CONTEXT_CONTRACT_VERSION,
    NORMALIZED_SDK_FIELDS,
    TRACK_CONTEXT_CONTRACT_VERSION,
    TRAFFIC_OBSERVATION_CONTEXT_CONTRACT_VERSION,
    EventIdentityAvailability,
    EventIdentityContextEvidence,
    EventIdentityProvenance,
    EventIdentityStatus,
    TelemetryAdapterError,
    TrackContextAvailability,
    TrackContextEvidence,
    TrackContextProvenance,
    TrackContextStatus,
    TrafficObservationAvailability,
    TrafficObservationContextEvidence,
    TrafficObservationProvenance,
    TrafficObservationStatus,
    ValidatedIbtRun,
    get_validated_event_identity_context,
    get_validated_track_context,
    get_validated_traffic_observation_context,
    iter_collector_jsonl_samples,
    iter_ibt_samples,
    open_collector_jsonl,
    open_collector_jsonl_snapshot,
    open_ibt_telemetry,
)
from iracing_ai_engineer.collector import (
    CollectorSample,
    SessionInfoPayloadScope,
    collect_samples_to_jsonl,
)
from iracing_ai_engineer.events import EventKind, process_telemetry_events
from iracing_ai_engineer.sdk_probe import (
    SDK_TYPE_SIZES,
    RawSdkFrame,
    VariableDescriptor,
)
from iracing_ai_engineer.telemetry import Presence, SourceKind


def frame(tick: int, *, fuel: object = 54.5) -> dict[str, object]:
    return {
        "SessionNum": 1,
        "SessionTick": tick,
        "SessionTime": tick / 60,
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
        "FuelLevel": fuel,
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


def descriptor(name: str, value: object, offset: int) -> VariableDescriptor:
    arrays = {
        "CarIdxLap",
        "CarIdxLapCompleted",
        "CarIdxLapDistPct",
        "CarIdxOnPitRoad",
        "CarIdxTrackSurface",
    }
    count = len(value) if name in arrays and isinstance(value, list) else 1
    if isinstance(value, bool) or (
        isinstance(value, list) and value and isinstance(value[0], bool)
    ):
        type_code, dtype = 1, "bool"
    elif isinstance(value, int) or (
        isinstance(value, list) and value and isinstance(value[0], int)
    ):
        type_code, dtype = 2, "int32"
    else:
        type_code, dtype = 4, "float32"
    if name == "SessionFlags":
        type_code, dtype = 3, "uint32_or_bitfield"
    return VariableDescriptor(
        name=name,
        type_code=type_code,
        dtype=dtype,
        offset=offset,
        count=count,
        count_as_time=False,
        unit="",
        description=name,
    )


def descriptors_for(values: dict[str, object]) -> tuple[VariableDescriptor, ...]:
    descriptors: list[VariableDescriptor] = []
    offset = 0
    for name, value in values.items():
        item = descriptor(name, value, offset)
        descriptors.append(item)
        offset += SDK_TYPE_SIZES[item.type_code] * item.count
    return tuple(descriptors)


class StubIbtReader:
    def __init__(
        self,
        frames: list[dict[str, object]],
        *,
        track_length: object | None = "6.93 km",
    ) -> None:
        self.frames = frames
        self.variable_names = tuple(frames[0])
        self.variables = descriptors_for(frames[0])
        self.metadata = SimpleNamespace(
            file_size_bytes=123_456,
            record_count=len(frames),
            tick_rate_hz=60,
        )
        self.source_sha256 = "c" * 64
        self.bulk_calls = 0
        self.context_calls = 0
        self.verify_calls = 0
        self.track_length = track_length

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get_channels(self, names):
        self.bulk_calls += 1
        return {name: [item[name] for item in self.frames] for name in names}

    def get_record(self, *_: object):  # pragma: no cover - regression tripwire
        raise AssertionError("adapter must not read IBT one field/record at a time")

    def public_session_context(self):
        self.context_calls += 1
        return {
            "track_length": self.track_length,
            "private_tripwire": "must-not-be-exported",
        }

    def verify_source_unchanged(self) -> None:
        self.verify_calls += 1


def write_collector(
    path: Path,
    frames: list[dict[str, object]],
    *,
    declared_values: dict[str, object] | None = None,
    source_id: str = "windows-rig-sdk",
    session_id: str = "fixture-session",
    sim_mode: str = "full",
    buffer_ticks: list[int] | None = None,
    track_length: object | None = "6.93 km",
) -> None:
    descriptors = descriptors_for(declared_values or frames[0])
    ticks = buffer_ticks if buffer_ticks is not None else list(range(len(frames)))
    assert len(ticks) == len(frames)
    observations = [
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=buffer_tick,
                session_info_update=1,
                values=values,
                sim_mode_raw=sim_mode,
                captured_monotonic_s=None,
            ),
            descriptors=descriptors,
            tick_rate_hz=60,
            session_info={
                "WeekendInfo": {
                    "TrackName": "Spa",
                    **(
                        {"TrackLength": track_length}
                        if track_length is not None
                        else {}
                    ),
                },
                "DriverInfo": {"Drivers": [{"UserName": "Private Person"}]},
            },
        )
        for index, (buffer_tick, values) in enumerate(zip(ticks, frames, strict=True))
    ]
    collect_samples_to_jsonl(
        observations,
        path,
        source_id=source_id,
        session_id=session_id,
        include_driver_info=True,
    )


def without_source(sample) -> dict[str, object]:
    payload = sample.to_dict()
    del payload["source"]
    return payload


def load_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def store_records(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(f"{json.dumps(item)}\n" for item in records))


def record_of(records: list[dict[str, object]], kind: str) -> dict[str, object]:
    return next(item for item in records if item["record_type"] == kind)


def test_ibt_and_collector_paths_normalize_equivalent_core_samples(tmp_path: Path):
    frames = [frame(100), frame(101, fuel="not-a-number")]
    stub = StubIbtReader(frames)
    ibt_samples = list(
        iter_ibt_samples(
            tmp_path / "fixture.ibt",
            source_id="public-audi-spa-ibt",
            session_id="fixture-session",
            reader_factory=lambda _: stub,
        )
    )
    collector_path = tmp_path / "collector.jsonl"
    write_collector(collector_path, frames)
    live_samples = list(iter_collector_jsonl_samples(collector_path))

    assert stub.bulk_calls == 1
    assert len(ibt_samples) == len(live_samples) == 2
    assert ibt_samples[0].source.source_kind.value is SourceKind.IBT_OFFLINE
    assert live_samples[0].source.source_kind.value is SourceKind.SDK_LIVE
    assert ibt_samples[0].source.source_id.value == "public-audi-spa-ibt"
    assert live_samples[0].source.source_id.value == "windows-rig-sdk"
    assert [without_source(item) for item in ibt_samples] == [
        without_source(item) for item in live_samples
    ]
    assert ibt_samples[0].incidents.player_car_my_incident_count.value == 2
    assert live_samples[0].incidents.player_car_driver_incident_count.value == 3
    assert ibt_samples[0].environment.track_temp_crew_c.value == 26.2
    assert live_samples[0].environment.track_temp_crew_c.value == 26.2
    assert ibt_samples[0].tires.player_tire_compound.value == 0
    assert ibt_samples[1].fuel.level_l.presence is Presence.INVALID
    assert "Private Person" in collector_path.read_text()
    assert "Private Person" not in repr(live_samples)


def test_open_ibt_binds_evidence_and_samples_to_one_reader(tmp_path: Path):
    stub = StubIbtReader([frame(100), frame(101)])
    factory_calls = 0

    def factory(_: object):
        nonlocal factory_calls
        factory_calls += 1
        return stub

    with open_ibt_telemetry(
        tmp_path / "replaceable-link.ibt",
        source_id="public-audi-spa-ibt",
        session_id="fixture-session",
        reader_factory=factory,
    ) as run:
        evidence = run.evidence.to_dict()
        context = run.track_context
        samples = list(run.samples)

    assert factory_calls == 1
    assert stub.bulk_calls == 1
    assert stub.context_calls == 1
    assert stub.verify_calls == 1
    assert evidence["source_sha256"] == "c" * 64
    assert evidence["record_count"] == len(samples) == 2
    assert {sample.source.source_id.value for sample in samples} == {
        evidence["source_id"]
    }
    assert context.track_length_mm == 6_930_000
    assert context.availability is TrackContextAvailability.AVAILABLE
    assert context.status is TrackContextStatus.VERIFIED
    assert context.provenance is TrackContextProvenance.IBT_SAME_HANDLE_SESSION_INFO
    assert context.contract_version == TRACK_CONTEXT_CONTRACT_VERSION
    assert len(context.context_sha256) == 64
    assert "track_length_mm" not in evidence


@pytest.mark.parametrize(
    ("raw_track_length", "expected_status"),
    [
        (None, TrackContextStatus.TRACK_LENGTH_MISSING),
    ],
)
def test_ibt_missing_track_length_is_explicitly_unavailable(
    tmp_path: Path,
    raw_track_length: object | None,
    expected_status: TrackContextStatus,
):
    stub = StubIbtReader([frame(100)], track_length=raw_track_length)

    with open_ibt_telemetry(
        tmp_path / "missing-context.ibt",
        source_id="public-audi-spa-ibt",
        session_id="fixture-session",
        reader_factory=lambda _: stub,
    ) as run:
        context = get_validated_track_context(run)

    assert context.track_length_mm is None
    assert context.availability is TrackContextAvailability.UNAVAILABLE
    assert context.status is expected_status


@pytest.mark.parametrize("raw_track_length", ["-6.93 km", "4.2 miles", "100 m"])
def test_ibt_invalid_track_length_is_rejected(
    tmp_path: Path,
    raw_track_length: object,
):
    stub = StubIbtReader([frame(100)], track_length=raw_track_length)

    with pytest.raises(
        TelemetryAdapterError, match="TrackLength"
    ), open_ibt_telemetry(
        tmp_path / "invalid-context.ibt",
        source_id="public-audi-spa-ibt",
        session_id="fixture-session",
        reader_factory=lambda _: stub,
    ):
        pass


def test_track_context_getter_rejects_forged_or_closed_runs(tmp_path: Path):
    forged = object.__new__(ValidatedIbtRun)
    with pytest.raises(TelemetryAdapterError, match="active adapter-created"):
        get_validated_track_context(forged)

    stub = StubIbtReader([frame(100)])
    with open_ibt_telemetry(
        tmp_path / "closed-context.ibt",
        source_id="public-audi-spa-ibt",
        session_id="fixture-session",
        reader_factory=lambda _: stub,
    ) as run:
        assert isinstance(run.track_context, TrackContextEvidence)
    with pytest.raises(TelemetryAdapterError, match="active adapter-created"):
        get_validated_track_context(run)


def test_open_collector_exposes_bound_self_consistency_evidence(tmp_path: Path):
    path = tmp_path / "evidence.jsonl"
    write_collector(path, [frame(100), frame(101)], buffer_ticks=[10, 12])

    with open_collector_jsonl(path) as run:
        evidence = run.evidence.to_dict()
        context = run.track_context
        samples = list(run.samples)

    assert len(samples) == 2
    assert evidence["completion_status"] == "COMPLETE"
    assert evidence["authenticity_status"] == "SELF_CONSISTENT_NOT_AUTHENTICATED"
    assert evidence["source_id"] == "windows-rig-sdk"
    assert evidence["session_id"] == "fixture-session"
    assert evidence["dropped_tick_count"] == 1
    assert evidence["frame_record_count"] == 2
    assert evidence["tick_rate_hz_values"] == [60]
    assert evidence["capture_span_us"] is None
    assert evidence["driver_info_key_count"] == 1
    assert evidence["redacted_driver_info_path_count"] == 0
    assert evidence["session_info_scope_counts"] == {"FULL": 1}
    assert len(evidence["records_sha256"]) == 64
    assert context.track_length_mm == 6_930_000
    assert context.availability is TrackContextAvailability.AVAILABLE
    assert context.provenance is TrackContextProvenance.COLLECTOR_VALIDATED_SNAPSHOT
    assert "track_length_mm" not in evidence
    assert "Private Person" not in repr(context)


def test_collector_exposes_same_capture_nearest_ahead_and_behind(tmp_path: Path):
    path = tmp_path / "traffic-observation.jsonl"
    write_collector(path, [frame(100), frame(101)])

    with open_collector_jsonl(path) as run:
        traffic = run.traffic_observation_context
        assert traffic is get_validated_traffic_observation_context(run)
        evidence = run.evidence
        track_context_sha256 = run.track_context.context_sha256
        list(run.samples)

    payload = traffic.to_dict()
    assert isinstance(traffic, TrafficObservationContextEvidence)
    assert traffic.contract_version == TRAFFIC_OBSERVATION_CONTEXT_CONTRACT_VERSION
    assert traffic.availability is TrafficObservationAvailability.AVAILABLE
    assert traffic.status is TrafficObservationStatus.VERIFIED
    assert (
        traffic.provenance
        is TrafficObservationProvenance.COLLECTOR_VALIDATED_SNAPSHOT
    )
    assert payload["decision_tick"] == 101
    assert payload["player_car_idx"] == 1
    assert payload["player_lap_position_ppb"] == 250_000_000
    assert payload["track_length_mm"] == 6_930_000
    assert payload["eligible_opponent_count"] == 1
    assert payload["excluded_opponent_count"] == 1
    assert payload["excluded_reason_counts"] == {"NOT_ON_TRACK_SURFACE": 1}
    assert payload["overlap_opponent_count"] == 0
    assert payload["nearest_ahead"] == {
        "car_idx": 0,
        "distance_mm": 6_583_500,
        "lap_position_ppb": 200_000_000,
        "race_lap_delta": 0,
    }
    assert payload["nearest_behind"] == {
        "car_idx": 0,
        "distance_mm": 346_500,
        "lap_position_ppb": 200_000_000,
        "race_lap_delta": 0,
    }
    assert payload["source_binding_sha256"] == (
        adapter_module._evidence_binding_sha256(evidence)  # noqa: SLF001
    )
    assert payload["track_context_sha256"] == track_context_sha256
    assert len(payload["context_sha256"]) == 64
    assert "Private Person" not in repr(traffic)
    with pytest.raises(TelemetryAdapterError, match="active adapter-created"):
        get_validated_traffic_observation_context(run)


def test_collector_traffic_requires_every_direct_position_array(tmp_path: Path):
    path = tmp_path / "traffic-missing-array.jsonl"
    values = frame(100)
    del values["CarIdxOnPitRoad"]
    write_collector(path, [values])

    with open_collector_jsonl(path) as run:
        traffic = run.traffic_observation_context
        list(run.samples)

    assert traffic.availability is TrafficObservationAvailability.UNAVAILABLE
    assert traffic.status is TrafficObservationStatus.OPPONENT_FIELDS_MISSING
    assert traffic.to_dict()["reasons"] == ["REQUIRED_OPPONENT_FIELD_MISSING"]
    assert traffic.nearest_ahead is None
    assert traffic.nearest_behind is None


def test_collector_exposes_complete_privacy_safe_event_identity(tmp_path: Path):
    path = tmp_path / "event-identity.jsonl"
    values = [
        {**frame(100 + index), "PlayerCarClass": 27}
        for index in range(2)
    ]
    descriptors = descriptors_for(values[0])
    session_info = {
        "WeekendInfo": {
            "BuildVersion": "2026.08.31.02",
            "EventType": "Race",
            "Official": 1,
            "RaceWeek": 12,
            "SeasonID": 601,
            "SeriesID": 501,
            "SimMode": "full",
            "TrackConfigName": "Grand Prix Pits",
            "TrackID": 101,
            "TrackLength": "6.93 km",
        },
        "DriverInfo": {
            "DriverCarIdx": 1,
            "DriverUserID": 123456,
            "Drivers": [{"UserName": "Private Person"}],
        },
    }
    observations = [
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=10 + index,
                session_info_update=1,
                values=item,
                sim_mode_raw="full",
            ),
            descriptors=descriptors,
            tick_rate_hz=60,
            session_info=session_info,
        )
        for index, item in enumerate(values)
    ]
    collect_samples_to_jsonl(
        observations,
        path,
        source_id="windows-rig-sdk",
        session_id="fixture-session",
    )

    with open_collector_jsonl(path) as run:
        context = run.event_identity_context
        assert context is get_validated_event_identity_context(run)
        evidence = run.evidence.to_dict()
        list(run.samples)

    payload = context.to_dict()
    assert isinstance(context, EventIdentityContextEvidence)
    assert context.contract_version == EVENT_IDENTITY_CONTEXT_CONTRACT_VERSION
    assert context.availability is EventIdentityAvailability.AVAILABLE
    assert context.status is EventIdentityStatus.VERIFIED
    assert (
        context.provenance
        is EventIdentityProvenance.COLLECTOR_VALIDATED_SNAPSHOT
    )
    assert payload["identity"] == {
        "series_id": 501,
        "season_id": 601,
        "race_week": 12,
        "track_id": 101,
        "car_class_id": 27,
        "event_type": "Race",
        "track_config": "Grand Prix Pits",
        "sim_build": "2026.08.31.02",
        "official": True,
    }
    assert payload["field_statuses"] == {
        name: "PRESENT" for name in payload["identity"]
    }
    assert payload["missing_fields"] == []
    assert payload["invalid_fields"] == []
    assert payload["session_info_scope"] == "FULL"
    assert payload["session_info_update"] == 1
    assert len(payload["context_sha256"]) == 64
    assert len(payload["source_binding_sha256"]) == 64
    assert "Private Person" not in repr(context)
    assert "DriverUserID" not in repr(context)
    assert evidence["driver_info_key_count"] == 0
    assert evidence["redacted_driver_info_path_count"] == 1
    assert "Private Person" not in path.read_text(encoding="utf-8")
    with pytest.raises(TelemetryAdapterError, match="active adapter-created"):
        get_validated_event_identity_context(run)


def test_collector_event_identity_is_explicitly_unavailable_when_fields_are_absent(
    tmp_path: Path,
):
    path = tmp_path / "missing-event-identity.jsonl"
    write_collector(path, [frame(100)])

    with open_collector_jsonl(path) as run:
        context = run.event_identity_context
        list(run.samples)

    payload = context.to_dict()
    assert context.availability is EventIdentityAvailability.UNAVAILABLE
    assert context.status is EventIdentityStatus.FIELDS_MISSING
    assert payload["identity"] == {
        name: None for name in payload["field_statuses"]
    }
    assert payload["missing_fields"] == list(payload["identity"])
    assert payload["invalid_fields"] == []


def test_collector_event_identity_rejects_same_epoch_class_conflict(tmp_path: Path):
    path = tmp_path / "class-conflict.jsonl"
    values = [
        {**frame(100), "PlayerCarClass": 27},
        {**frame(101), "PlayerCarClass": 28},
    ]
    write_collector(path, values)

    with pytest.raises(
        TelemetryAdapterError,
        match="car_class_id changed within one session epoch",
    ), open_collector_jsonl(path):
        pass


def test_open_collector_snapshot_uses_and_preserves_caller_owned_handle(tmp_path: Path):
    path = tmp_path / "snapshot-handle.jsonl"
    write_collector(path, [frame(100), frame(101)])
    handle = io.StringIO(path.read_text(encoding="utf-8"))

    with open_collector_jsonl_snapshot(handle) as run:
        assert run.evidence.to_dict()["frame_record_count"] == 2
        assert len(tuple(run.samples)) == 2

    assert handle.closed is False
    handle.seek(0)
    assert json.loads(handle.readline())["record_type"] == "run"


@pytest.mark.parametrize(
    ("session_info", "scope", "expected_status"),
    [
        (
            {"WeekendInfo": {"TrackName": "Spa"}},
            SessionInfoPayloadScope.FULL,
            TrackContextStatus.TRACK_LENGTH_MISSING,
        ),
        (
            {"WeekendInfo": {"SimMode": "full", "TrackLength": "6.93 km"}},
            SessionInfoPayloadScope.PARTIAL,
            TrackContextStatus.SESSION_INFO_PARTIAL,
        ),
        (
            None,
            SessionInfoPayloadScope.UNAVAILABLE,
            TrackContextStatus.SESSION_INFO_UNAVAILABLE,
        ),
    ],
)
def test_collector_never_trusts_missing_or_partial_track_context(
    tmp_path: Path,
    session_info: dict[str, object] | None,
    scope: SessionInfoPayloadScope,
    expected_status: TrackContextStatus,
):
    values = frame(100)
    observation = CollectorSample(
        frame=RawSdkFrame(
            buffer_tick=10,
            session_info_update=1,
            values=values,
            sim_mode_raw="full",
        ),
        descriptors=descriptors_for(values),
        tick_rate_hz=60,
        session_info=session_info,
        session_info_scope=scope,
    )
    path = tmp_path / f"{scope.value.casefold()}-context.jsonl"
    collect_samples_to_jsonl(
        [observation],
        path,
        source_id="windows-rig-sdk",
        session_id="fixture-session",
    )

    with open_collector_jsonl(path) as run:
        context = run.track_context
        list(run.samples)

    assert context.track_length_mm is None
    assert context.availability is TrackContextAvailability.UNAVAILABLE
    assert context.status is expected_status


def test_collector_rejects_conflicting_full_track_lengths(tmp_path: Path):
    values = [frame(100), frame(101)]
    schema = descriptors_for(values[0])
    observations = [
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=10 + index,
                session_info_update=1 + index,
                values=item,
                sim_mode_raw="full",
            ),
            descriptors=schema,
            tick_rate_hz=60,
            session_info={
                "WeekendInfo": {
                    "TrackName": "Spa",
                    "TrackLength": track_length,
                }
            },
        )
        for index, (item, track_length) in enumerate(
            zip(values, ("6.93 km", "7.00 km"), strict=True)
        )
    ]
    path = tmp_path / "conflicting-context.jsonl"
    collect_samples_to_jsonl(
        observations,
        path,
        source_id="windows-rig-sdk",
        session_id="fixture-session",
    )

    with pytest.raises(
        TelemetryAdapterError, match="track length changed"
    ), open_collector_jsonl(path):
        pass


def test_collector_evidence_tracks_timing_read_errors_and_privacy_redaction(
    tmp_path: Path,
):
    path = tmp_path / "quality-evidence.jsonl"
    values = frame(100)
    descriptors = descriptors_for(values)
    observations = (
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=10 + index,
                session_info_update=1,
                values=values,
                read_errors=("FuelLevel",) if index == 1 else (),
                sim_mode_raw="full",
                captured_monotonic_s=20.0 + index * 0.1,
            ),
            descriptors=descriptors,
            tick_rate_hz=60,
            session_info={
                "WeekendInfo": {"TrackName": "Spa"},
                "DriverInfo": {"Drivers": [{"UserName": "Private Person"}]},
            },
        )
        for index in range(2)
    )
    collect_samples_to_jsonl(
        observations,
        path,
        source_id="windows-rig-sdk",
        session_id="fixture-session",
    )

    with open_collector_jsonl(path) as run:
        evidence = run.evidence.to_dict()
        list(run.samples)

    assert evidence["tick_rate_hz_values"] == [60]
    assert evidence["first_capture_monotonic_us"] == 20_000_000
    assert evidence["last_capture_monotonic_us"] == 20_100_000
    assert evidence["capture_span_us"] == 100_000
    assert evidence["capture_clock_regression_count"] == 0
    assert evidence["read_error_frame_count"] == 1
    assert evidence["read_error_field_count"] == 1
    assert evidence["driver_info_key_count"] == 0
    assert evidence["redacted_driver_info_path_count"] == 1
    assert evidence["session_info_scope_counts"] == {"FULL": 1}


def test_replay_run_binds_proxy_source_and_cannot_be_relabeled(tmp_path: Path):
    path = tmp_path / "replay.jsonl"
    write_collector(
        path,
        [frame(100)],
        source_id="replay-rig",
        session_id="replay-session",
        sim_mode="replay",
    )

    sample = list(iter_collector_jsonl_samples(path))[0]

    assert sample.source.source_id.value == "replay-rig"
    assert sample.session.session_id.value == "replay-session"
    assert sample.source.source_kind.value is SourceKind.REPLAY_SDK_PROXY
    assert "source_id" not in inspect.signature(iter_collector_jsonl_samples).parameters
    assert "session_id" not in inspect.signature(iter_collector_jsonl_samples).parameters
    with pytest.raises(TypeError, match="source_id"):
        iter_collector_jsonl_samples(path, source_id="forged")  # type: ignore[call-arg]


def test_collector_yields_only_the_same_process_snapshot_that_was_validated(
    monkeypatch, tmp_path: Path
):
    honest = tmp_path / "honest.jsonl"
    forged = tmp_path / "forged.jsonl"
    write_collector(
        honest,
        [frame(100, fuel=54.5)],
        source_id="honest-rig",
        session_id="honest-session",
    )
    write_collector(
        forged,
        [frame(100, fuel=99.0)],
        source_id="forged-rig",
        session_id="forged-session",
    )
    payloads = iter(
        (
            honest.read_text(encoding="utf-8"),
            forged.read_text(encoding="utf-8"),
        )
    )
    open_count = 0

    @contextmanager
    def changing_path(_path):
        nonlocal open_count
        open_count += 1
        yield io.StringIO(next(payloads))

    monkeypatch.setattr(adapter_module, "_collector_handle", changing_path)

    samples = list(iter_collector_jsonl_samples(tmp_path / "switchable.jsonl"))

    assert open_count == 1
    assert samples[0].source.source_id.value == "honest-rig"
    assert samples[0].session.session_id.value == "honest-session"
    assert samples[0].fuel.level_l.value == 54.5


def test_both_paths_keep_missing_fields_and_bad_opponent_arrays_fail_closed(
    tmp_path: Path,
):
    malformed = frame(100)
    del malformed["FuelLevelPct"]
    del malformed["PlayerCarTeamIncidentCount"]
    malformed["CarIdxLapDistPct"] = [0.20, 0.25]
    stub = StubIbtReader([malformed])
    stub.variables = descriptors_for(frame(100))

    ibt_sample = list(
        iter_ibt_samples(
            tmp_path / "malformed.ibt",
            source_id="ibt-fixture",
            session_id="fixture-session",
            reader_factory=lambda _: stub,
        )
    )[0]
    collector_path = tmp_path / "malformed.jsonl"
    write_collector(collector_path, [malformed], declared_values=frame(100))
    live_sample = list(iter_collector_jsonl_samples(collector_path))[0]

    for sample in (ibt_sample, live_sample):
        assert sample.fuel.level_pct.presence is Presence.MISSING
        assert (
            sample.incidents.player_car_team_incident_count.presence
            is Presence.MISSING
        )
        assert sample.opponents.presence is Presence.INVALID
        assert "OPPONENT_ARRAY_LENGTH_MISMATCH" in sample.opponents.issues
    assert without_source(ibt_sample) == without_source(live_sample)


@pytest.mark.parametrize(
    "source_id",
    ["", " padded", "padded ", "line\nbreak", "x" * 257],
)
def test_ibt_rejects_unsafe_source_identifiers(tmp_path: Path, source_id: str):
    with pytest.raises(ValueError, match="source_id"):
        list(
            iter_ibt_samples(
                tmp_path / "fixture.ibt",
                source_id=source_id,
                session_id="fixture-session",
                reader_factory=lambda _: StubIbtReader([frame(100)]),
            )
        )


def test_ibt_channel_length_mismatch_is_rejected(tmp_path: Path):
    stub = StubIbtReader([frame(100), frame(101)])
    original = stub.get_channels

    def short_channel(names):
        channels = original(names)
        channels["Speed"] = channels["Speed"][:1]
        return channels

    stub.get_channels = short_channel  # type: ignore[method-assign]
    with pytest.raises(TelemetryAdapterError, match="Speed length"):
        list(
            iter_ibt_samples(
                tmp_path / "short.ibt",
                source_id="ibt-fixture",
                session_id="session-fixture",
                reader_factory=lambda _: stub,
            )
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda records: records[0].update(collector_contract_version="future-v9"), "contract"),
        (lambda records: records[1].update(sequence=99), "sequence"),
        (
            lambda records: record_of(records, "frame").update(schema_epoch=4),
            "unknown schema",
        ),
        (
            lambda records: record_of(records, "frame")["values"].update(
                UnknownChannel=1
            ),
            "absent from schema",
        ),
        (lambda records: records[1].update(record_type="mystery"), "unknown collector"),
        (
            lambda records: record_of(records, "frame").update(sim_mode_raw="replay"),
            "sim_mode_raw",
        ),
        (lambda records: records[0].update(extra="forbidden"), "fields are invalid"),
    ],
)
def test_collector_rejects_contract_sequence_schema_and_field_corruption(
    tmp_path: Path, mutation, message: str
):
    path = tmp_path / "collector.jsonl"
    write_collector(path, [frame(100)])
    records = load_records(path)
    mutation(records)
    store_records(path, records)

    with pytest.raises(TelemetryAdapterError, match=message):
        list(iter_collector_jsonl_samples(path))


def test_full_and_replay_source_kind_pairing_is_enforced(tmp_path: Path):
    path = tmp_path / "collector.jsonl"
    write_collector(path, [frame(100)])
    records = load_records(path)
    records[0]["source_kind"] = "REPLAY_SDK_PROXY"
    store_records(path, records)

    with pytest.raises(TelemetryAdapterError, match="sim_mode and source_kind"):
        list(iter_collector_jsonl_samples(path))


def test_session_info_payload_digest_and_scope_are_enforced(tmp_path: Path):
    path = tmp_path / "collector.jsonl"
    write_collector(path, [frame(100)])
    records = load_records(path)
    session_info = record_of(records, "session_info")
    session_info["payload_scope"] = "UNAVAILABLE"
    store_records(path, records)
    with pytest.raises(TelemetryAdapterError, match="scope/status/payload"):
        list(iter_collector_jsonl_samples(path))

    path = tmp_path / "digest.jsonl"
    write_collector(path, [frame(100)])
    records = load_records(path)
    record_of(records, "session_info")["session_info_sha256"] = "0" * 64
    store_records(path, records)
    with pytest.raises(TelemetryAdapterError, match="session_info_sha256"):
        list(iter_collector_jsonl_samples(path))


def test_collector_rejects_unbounded_capture_timestamp_before_float_conversion(
    tmp_path: Path,
):
    path = tmp_path / "huge-capture-time.jsonl"
    write_collector(path, [frame(100)])
    records = load_records(path)
    record_of(records, "frame")["capture_monotonic_us"] = 10**400
    store_records(path, records)

    with pytest.raises(TelemetryAdapterError, match="signed 64-bit"):
        list(iter_collector_jsonl_samples(path))


def test_duplicate_event_structure_and_receipt_accounting_are_enforced(tmp_path: Path):
    path = tmp_path / "duplicates.jsonl"
    write_collector(path, [frame(100), frame(100)], buffer_ticks=[5, 5])
    records = load_records(path)
    duplicate = next(
        item for item in records if item.get("event_kind") == "duplicate_sample"
    )
    duplicate["details"]["unexpected"] = True
    store_records(path, records)

    with pytest.raises(TelemetryAdapterError, match="event details fields"):
        list(iter_collector_jsonl_samples(path))


@pytest.mark.parametrize(
    "field",
    [
        "records_sha256",
        "completion_status",
        "semantic_record_count",
        "run_record_count",
        "frame_record_count",
        "event_record_count",
        "schema_record_count",
        "session_info_record_count",
        "samples_seen",
        "duplicate_sample_count",
        "duplicate_conflict_count",
        "dropped_tick_count",
        "stale_event_count",
        "session_reset_count",
        "schema_change_count",
        "schema_epoch_count",
        "session_epoch_count",
        "first_buffer_tick",
        "last_buffer_tick",
        "collector_contract_version",
    ],
)
def test_every_v2_receipt_field_is_independently_checked(tmp_path: Path, field: str):
    path = tmp_path / f"receipt-{field}.jsonl"
    write_collector(path, [frame(100), frame(100)], buffer_ticks=[5, 5])
    records = load_records(path)
    receipt = record_of(records, "collector_receipt")["receipt"]
    if field == "records_sha256":
        receipt[field] = "0" * 64
    elif field == "completion_status":
        receipt[field] = "CRASHED"
    elif field == "collector_contract_version":
        receipt[field] = "live-collector-v999"
    else:
        receipt[field] += 1
    store_records(path, records)

    with pytest.raises(TelemetryAdapterError):
        list(iter_collector_jsonl_samples(path))


def test_validation_finishes_before_first_yield_even_for_bad_tail(tmp_path: Path):
    path = tmp_path / "bad-tail.jsonl"
    write_collector(path, [frame(100)])
    with path.open("a") as handle:
        handle.write('{"collector_contract_version":"live-collector-v2"')

    samples = iter_collector_jsonl_samples(path)
    with pytest.raises(TelemetryAdapterError, match="incomplete collector JSON"):
        next(samples)


def test_duplicate_json_keys_and_multiple_run_records_are_rejected(tmp_path: Path):
    duplicate = tmp_path / "duplicate-key.jsonl"
    duplicate.write_text(
        '{"collector_contract_version":"live-collector-v2",'
        '"record_type":"run","record_type":"frame","sequence":0}\n'
    )
    with pytest.raises(TelemetryAdapterError, match="duplicate JSON"):
        list(iter_collector_jsonl_samples(duplicate))

    multiple = tmp_path / "multiple-run.jsonl"
    write_collector(multiple, [frame(100)])
    records = load_records(multiple)
    second_run = dict(records[0])
    second_run["sequence"] = 1
    records[1] = second_run
    store_records(multiple, records)
    with pytest.raises(TelemetryAdapterError, match="more than one run"):
        list(iter_collector_jsonl_samples(multiple))


def test_crash_prefix_requires_explicit_opt_in_and_is_prevalidated(tmp_path: Path):
    path = tmp_path / "crash-prefix.jsonl"
    write_collector(path, [frame(100)])
    records = load_records(path)
    store_records(path, records[:-1])

    default_samples = iter_collector_jsonl_samples(path)
    with pytest.raises(TelemetryAdapterError, match="no terminal receipt"):
        next(default_samples)
    assert len(
        list(iter_collector_jsonl_samples(path, require_receipt=False))
    ) == 1

    with path.open("a") as handle:
        handle.write('{"broken":')
    recovery_samples = iter_collector_jsonl_samples(path, require_receipt=False)
    with pytest.raises(TelemetryAdapterError, match="incomplete collector JSON"):
        next(recovery_samples)


def test_collector_recovery_rejects_dangling_semantic_transaction(tmp_path: Path):
    path = tmp_path / "dangling-tick-drop.jsonl"
    write_collector(path, [frame(100), frame(101)], buffer_ticks=[10, 12])
    records = load_records(path)
    tick_drop_index = next(
        index
        for index, record in enumerate(records)
        if record.get("event_kind") == "tick_drop"
    )
    store_records(path, records[: tick_drop_index + 1])

    samples = iter_collector_jsonl_samples(path, require_receipt=False)
    with pytest.raises(TelemetryAdapterError, match="tick_drop transaction"):
        next(samples)


def test_receipt_must_be_terminal(tmp_path: Path):
    path = tmp_path / "middle-receipt.jsonl"
    write_collector(path, [frame(100)])
    records = load_records(path)
    records.append(dict(records[0], sequence=records[-1]["sequence"] + 1))
    store_records(path, records)

    samples = iter_collector_jsonl_samples(path)
    with pytest.raises(TelemetryAdapterError, match="terminal"):
        next(samples)


def test_session_epoch_boundary_resets_normalizer_previous_sample(tmp_path: Path):
    first = frame(100)
    second = frame(1)
    second["SessionNum"] = 2
    path = tmp_path / "reset.jsonl"
    write_collector(path, [first, second], buffer_ticks=[10, 1])

    samples = list(iter_collector_jsonl_samples(path))

    assert len(samples) == 2
    assert samples[1].quality.stale.presence is Presence.MISSING
    assert samples[1].quality.dropped_ticks.presence is Presence.MISSING
    assert "STALE_UNASSESSED" in samples[1].quality.issues.value
    events, _ = process_telemetry_events(samples)
    reset = next(event for event in events if event.kind is EventKind.SESSION_RESET)
    reasons = reset.to_dict()["details"]["reasons"]
    assert "SESSION_TICK_REGRESSION" in reasons
    assert "SESSION_TIME_REGRESSION" in reasons
    assert "SESSIONTICK_REGRESSION" not in reasons
    assert "SESSIONTIME_REGRESSION" not in reasons
    assert len(reasons) == len(set(reasons))


def test_collector_session_info_update_reset_reaches_shared_event_pipeline(
    tmp_path: Path,
):
    values = [frame(100), frame(101)]
    schema = descriptors_for(values[0])
    observations = [
        CollectorSample(
            frame=RawSdkFrame(
                buffer_tick=10 + index,
                session_info_update=2 - index,
                values=item,
                sim_mode_raw="full",
                captured_monotonic_s=100.0 + index * 0.01,
            ),
            descriptors=schema,
            tick_rate_hz=60,
            session_info={"WeekendInfo": {"SimMode": "full"}},
        )
        for index, item in enumerate(values)
    ]
    path = tmp_path / "session-info-reset.jsonl"
    collect_samples_to_jsonl(
        observations,
        path,
        source_id="windows-rig-sdk",
        session_id="fixture-session",
    )

    samples = list(iter_collector_jsonl_samples(path))
    events, receipt = process_telemetry_events(samples)

    assert "CONTINUITY_BOUNDARY:SESSION_INFO_UPDATE_REGRESSION" in (
        samples[1].quality.issues.value
    )
    reset = next(event for event in events if event.kind is EventKind.SESSION_RESET)
    assert reset.to_dict()["details"]["reasons"] == [
        "SESSION_INFO_UPDATE_REGRESSION"
    ]
    assert receipt.session_epoch_count == 2


def test_collector_buffer_drop_reaches_normalized_quality_and_events(tmp_path: Path):
    path = tmp_path / "buffer-drop.jsonl"
    write_collector(path, [frame(100), frame(101)], buffer_ticks=[10, 12])

    samples = list(iter_collector_jsonl_samples(path))
    events, _ = process_telemetry_events(samples)

    assert samples[1].quality.dropped_ticks.value == 1
    assert "TICK_DELTA_DISAGREEMENT" in samples[1].quality.issues.value
    dropped = next(event for event in events if event.kind is EventKind.DROPPED_TICKS)
    assert dropped.to_dict()["details"] == {"count": 1}


def test_normalized_allowlist_excludes_private_metadata_fields():
    assert "SessionInfo" not in NORMALIZED_SDK_FIELDS
    assert "DriverInfo" not in NORMALIZED_SDK_FIELDS
    assert "PlayerCarDriverIncidentCount" in NORMALIZED_SDK_FIELDS
    assert {
        "SessionTimeOfDay",
        "TrackTemp",
        "TrackTempCrew",
        "AirTemp",
        "WeatherType",
        "WeatherVersion",
        "Skies",
        "WindVel",
        "WindDir",
        "RelativeHumidity",
        "Precipitation",
        "PlayerTireCompound",
        "TireSetsUsed",
    } <= set(NORMALIZED_SDK_FIELDS)
    assert all("userinfo" not in name.casefold() for name in NORMALIZED_SDK_FIELDS)


def test_schema_fixture_matches_collector_descriptor_shape():
    values = frame(100)
    assert [item.name for item in descriptors_for(values)] == list(values)
    assert asdict(descriptors_for(values)[0])["name"] == "SessionNum"
