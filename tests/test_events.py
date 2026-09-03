from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from iracing_ai_engineer.events import (
    EventKind,
    TelemetryEventPipeline,
    process_telemetry_events,
)
from iracing_ai_engineer.telemetry import (
    SourceKind,
    TelemetryField,
    TelemetrySample,
    normalize_sdk_frame,
)


def frame(
    *,
    session_num: int = 1,
    tick: int,
    session_time: float,
    lap: int = 4,
    laps_completed: int = 3,
    lap_distance_pct: float = 0.5,
    on_pit_road: bool = False,
    in_pit_stall: bool = False,
    flags: int = 1,
) -> dict[str, object]:
    return {
        "SessionNum": session_num,
        "SessionTick": tick,
        "SessionTime": session_time,
        "Lap": lap,
        "LapCompleted": laps_completed,
        "LapDistPct": lap_distance_pct,
        "OnPitRoad": on_pit_road,
        "PlayerCarInPitStall": in_pit_stall,
        "SessionFlags": flags,
    }


def normalize_stream(
    frames: list[dict[str, object]],
    *,
    source_id: str = "rig",
    session_id: str = "race",
    source_kind: SourceKind = SourceKind.SDK_LIVE,
    buffer_ticks: list[int] | None = None,
    captures: list[float] | None = None,
) -> list[TelemetrySample]:
    samples: list[TelemetrySample] = []
    previous = None
    selected_buffer_ticks = buffer_ticks or list(range(1_000, 1_000 + len(frames)))
    selected_captures = captures or [100.0 + offset * 0.1 for offset in range(len(frames))]
    for index, raw in enumerate(frames):
        sample = normalize_sdk_frame(
            raw,
            source_id=source_id,
            session_id=session_id,
            source_kind=source_kind,
            buffer_tick=selected_buffer_ticks[index],
            captured_monotonic_s=selected_captures[index],
            previous=previous,
        )
        samples.append(sample)
        previous = sample
    return samples


def kinds(events) -> list[EventKind]:
    return [event.kind for event in events]


def test_detects_lap_pit_stall_and_flag_transitions():
    samples = normalize_stream(
        [
            frame(tick=100, session_time=10.0, lap_distance_pct=0.90),
            frame(
                tick=101,
                session_time=11.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=0.05,
            ),
            frame(
                tick=102,
                session_time=12.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=0.10,
                on_pit_road=True,
            ),
            frame(
                tick=103,
                session_time=13.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=0.15,
                on_pit_road=True,
                in_pit_stall=True,
            ),
            frame(
                tick=104,
                session_time=14.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=0.20,
                on_pit_road=True,
            ),
            frame(
                tick=105,
                session_time=15.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=0.25,
                flags=2,
            ),
        ]
    )

    events, receipt = process_telemetry_events(samples)

    assert kinds(events) == [
        EventKind.SOURCE_STARTED,
        EventKind.SESSION_STARTED,
        EventKind.LAP_COMPLETED,
        EventKind.LAP_WRAP,
        EventKind.PIT_ROAD_ENTERED,
        EventKind.PIT_STALL_ENTERED,
        EventKind.PIT_STALL_EXITED,
        EventKind.PIT_ROAD_EXITED,
        EventKind.FLAG_CHANGED,
    ]
    assert receipt.sample_count == 6
    assert receipt.accepted_sample_count == 6
    assert receipt.rejected_sample_count == 0
    assert receipt.event_count == len(events)
    assert len(receipt.events_sha256) == 64
    assert len(receipt.receipt_sha256) == 64
    completed = next(event for event in events if event.kind is EventKind.LAP_COMPLETED)
    assert completed.to_dict()["details"] == {
        "completed_count": 1,
        "current_laps_completed": 4,
        "previous_laps_completed": 3,
    }


def test_arbitrary_chunks_produce_identical_events_and_receipt_hashes():
    samples = normalize_stream(
        [
            frame(tick=10, session_time=1.0, lap_distance_pct=0.9),
            frame(
                tick=11,
                session_time=2.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=0.1,
            ),
            frame(
                tick=12,
                session_time=3.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=0.2,
                on_pit_road=True,
            ),
            frame(
                tick=13,
                session_time=4.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=0.3,
                flags=4,
            ),
        ]
    )
    batch_events, batch_receipt = process_telemetry_events(samples)

    chunked = TelemetryEventPipeline()
    chunked.feed(samples[:1])
    chunked.feed_chunk(iter(samples[1:3]))
    chunked.feed(samples[3])
    chunked_receipt = chunked.finish()

    assert chunked.events == batch_events
    assert chunked_receipt == batch_receipt
    assert chunked.finish() is chunked_receipt
    with pytest.raises(RuntimeError, match="already finished"):
        chunked.feed(samples[0])


def test_drop_is_reported_and_closes_transition_gap():
    samples = normalize_stream(
        [
            frame(tick=1, session_time=1.0, lap_distance_pct=0.9),
            frame(
                tick=4,
                session_time=4.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=0.1,
                on_pit_road=True,
                flags=2,
            ),
            frame(
                tick=5,
                session_time=5.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=0.2,
                on_pit_road=True,
                flags=2,
            ),
            frame(
                tick=6,
                session_time=6.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=0.3,
                flags=4,
            ),
        ],
        buffer_ticks=[10, 13, 14, 15],
    )

    events, receipt = process_telemetry_events(samples)
    event_kinds = kinds(events)

    assert event_kinds.count(EventKind.DROPPED_TICKS) == 1
    assert EventKind.PIT_ROAD_ENTERED not in event_kinds
    assert EventKind.LAP_COMPLETED not in event_kinds
    assert EventKind.LAP_WRAP not in event_kinds
    assert event_kinds[-2:] == [EventKind.PIT_ROAD_EXITED, EventKind.FLAG_CHANGED]
    drop = next(event for event in events if event.kind is EventKind.DROPPED_TICKS)
    assert drop.to_dict()["details"] == {"count": 2}
    assert receipt.accepted_sample_count == 4


def test_unknown_boolean_and_flag_are_not_treated_as_false():
    raw = [
        frame(tick=1, session_time=1.0),
        frame(tick=2, session_time=2.0),
        frame(tick=3, session_time=3.0, on_pit_road=True, flags=2),
        frame(tick=4, session_time=4.0, flags=4),
    ]
    del raw[1]["OnPitRoad"]
    del raw[1]["SessionFlags"]
    events, _ = process_telemetry_events(normalize_stream(raw))
    event_kinds = kinds(events)

    assert EventKind.PIT_ROAD_ENTERED not in event_kinds
    assert event_kinds.count(EventKind.PIT_ROAD_EXITED) == 1
    assert event_kinds.count(EventKind.FLAG_CHANGED) == 1


def test_negative_lap_distance_sentinel_cannot_emit_lap_wrap():
    samples = normalize_stream(
        [
            frame(tick=1, session_time=1.0, lap_distance_pct=0.9),
            frame(tick=2, session_time=2.0, lap_distance_pct=-1.0),
        ]
    )

    events, _ = process_telemetry_events(samples)

    assert EventKind.LAP_WRAP not in kinds(events)


def test_small_negative_timing_line_overshoot_remains_valid_wrap_evidence():
    samples = normalize_stream(
        [
            frame(tick=1, session_time=1.0, lap_distance_pct=0.999921),
            frame(
                tick=2,
                session_time=2.0,
                lap=5,
                laps_completed=4,
                lap_distance_pct=-0.00002663,
            ),
        ]
    )

    events, _ = process_telemetry_events(samples)

    assert EventKind.LAP_WRAP in kinds(events)


def test_late_session_id_enrichment_preserves_future_identity_change_detection():
    session_ids = (None, "race", "qualifying")
    samples = [
        normalize_sdk_frame(
            frame(tick=index, session_time=float(index)),
            source_id="rig",
            session_id=session_id,
            buffer_tick=100 + index,
            captured_monotonic_s=10.0 + index,
        )
        for index, session_id in enumerate(session_ids, start=1)
    ]

    events, receipt = process_telemetry_events(samples)

    resets = [event for event in events if event.kind is EventKind.SESSION_RESET]
    assert len(resets) == 1
    assert resets[0].to_dict()["details"]["reasons"] == ["SESSION_ID_CHANGED"]
    assert receipt.session_epoch_count == 2


def test_unknown_stale_observation_does_not_split_one_stale_episode():
    samples = normalize_stream(
        [
            frame(tick=1, session_time=1.0),
            frame(tick=1, session_time=1.0),
            frame(tick=2, session_time=2.0),
            frame(tick=2, session_time=2.0),
        ],
        buffer_ticks=[10, 10, 11, 11],
        captures=[100.0, 100.8, 100.9, 101.7],
    )
    samples[2] = replace(
        samples[2],
        quality=replace(
            samples[2].quality,
            stale=TelemetryField.missing(),
            dropped_ticks=TelemetryField.missing(),
        ),
    )

    events, _ = process_telemetry_events(samples)
    event_kinds = kinds(events)

    assert event_kinds.count(EventKind.SOURCE_STALE) == 1
    assert EventKind.SOURCE_RESUMED not in event_kinds


def test_stale_rejected_and_resume_are_explicit_without_other_transitions():
    samples = normalize_stream(
        [
            frame(tick=1, session_time=1.0),
            frame(tick=1, session_time=1.0, on_pit_road=True, flags=2),
            frame(tick=2, session_time=2.0, on_pit_road=True, flags=2),
        ],
        buffer_ticks=[10, 10, 11],
        captures=[100.0, 100.8, 100.9],
    )

    events, receipt = process_telemetry_events(samples)
    event_kinds = kinds(events)

    assert EventKind.QUALITY_REJECTED in event_kinds
    assert EventKind.SOURCE_STALE in event_kinds
    assert EventKind.SOURCE_RESUMED in event_kinds
    assert EventKind.PIT_ROAD_ENTERED not in event_kinds
    assert EventKind.FLAG_CHANGED not in event_kinds
    assert receipt.rejected_sample_count == 1


def test_rejected_sample_never_seeds_later_transition_state():
    raw = [
        frame(tick=1, session_time=1.0),
        frame(tick=2, session_time=float("nan"), on_pit_road=True, flags=2),
        frame(tick=3, session_time=3.0),
    ]

    events, receipt = process_telemetry_events(normalize_stream(raw))
    event_kinds = kinds(events)

    assert EventKind.QUALITY_REJECTED in event_kinds
    assert EventKind.PIT_ROAD_ENTERED not in event_kinds
    assert EventKind.PIT_ROAD_EXITED not in event_kinds
    assert EventKind.FLAG_CHANGED not in event_kinds
    assert receipt.rejected_sample_count == 1


def test_regression_resets_session_and_does_not_bridge_state():
    samples = normalize_stream(
        [
            frame(tick=100, session_time=10.0),
            frame(tick=90, session_time=9.0, on_pit_road=True, flags=2),
            frame(tick=91, session_time=10.0, on_pit_road=True, flags=2),
        ],
        buffer_ticks=[1_000, 900, 901],
        captures=[100.0, 99.0, 99.1],
    )
    events, receipt = process_telemetry_events(samples)
    event_kinds = kinds(events)

    assert EventKind.SESSION_RESET in event_kinds
    assert event_kinds.count(EventKind.SESSION_STARTED) == 2
    assert EventKind.QUALITY_REJECTED in event_kinds
    assert EventKind.PIT_ROAD_ENTERED not in event_kinds
    reset = next(event for event in events if event.kind is EventKind.SESSION_RESET)
    reasons = reset.to_dict()["details"]["reasons"]
    assert {
        "SESSION_TICK_REGRESSION",
        "SESSION_TIME_REGRESSION",
        "BUFFER_TICK_REGRESSION",
        "CAPTURE_TIME_REGRESSION",
    }.issubset(reasons)
    assert receipt.session_epoch_count == 2


def test_reset_with_missing_session_num_starts_epoch_on_next_valid_sample():
    raw = [
        frame(tick=100, session_time=10.0),
        frame(tick=90, session_time=9.0),
        frame(tick=91, session_time=10.0),
    ]
    del raw[1]["SessionNum"]
    samples = normalize_stream(
        raw,
        buffer_ticks=[1_000, 900, 901],
        captures=[100.0, 99.0, 99.1],
    )

    events, receipt = process_telemetry_events(samples)
    event_kinds = kinds(events)

    assert event_kinds.count(EventKind.SESSION_RESET) == 1
    assert event_kinds.count(EventKind.SESSION_STARTED) == 2
    assert receipt.session_epoch_count == 2


def test_source_session_and_schema_boundaries_never_share_transition_state():
    first = normalize_stream(
        [frame(tick=1, session_time=1.0)], source_id="sdk-a"
    )[0]
    changed_source = normalize_stream(
        [frame(tick=1, session_time=1.0, on_pit_road=True)],
        source_id="ibt-b",
        source_kind=SourceKind.IBT_OFFLINE,
    )[0]
    changed_session = normalize_stream(
        [frame(session_num=2, tick=1, session_time=1.0)],
        source_id="ibt-b",
        session_id="qualifying",
        source_kind=SourceKind.IBT_OFFLINE,
    )[0]
    changed_schema = replace(changed_session, contract_version="normalized-telemetry-v4")

    events, receipt = process_telemetry_events(
        [first, changed_source, changed_session, changed_schema]
    )
    event_kinds = kinds(events)

    assert event_kinds.count(EventKind.SOURCE_RESET) == 2
    assert event_kinds.count(EventKind.SOURCE_STARTED) == 3
    assert event_kinds.count(EventKind.SESSION_RESET) == 1
    assert event_kinds.count(EventKind.SESSION_STARTED) == 4
    assert EventKind.PIT_ROAD_ENTERED not in event_kinds
    assert receipt.source_epoch_count == 3
    assert receipt.session_epoch_count == 4

    source_resets = [event for event in events if event.kind is EventKind.SOURCE_RESET]
    assert (source_resets[0].source_id, source_resets[0].source_kind) == (
        "sdk-a",
        SourceKind.SDK_LIVE,
    )
    session_reset = next(
        event for event in events if event.kind is EventKind.SESSION_RESET
    )
    assert (session_reset.session_id, session_reset.session_num) == ("race", 1)

    source_identities: dict[int, set[tuple[str | None, SourceKind | None]]] = {}
    session_identities: dict[
        tuple[int, int], set[tuple[str | None, int | None]]
    ] = {}
    for event in events:
        source_identities.setdefault(event.source_epoch, set()).add(
            (event.source_id, event.source_kind)
        )
        if event.session_epoch is not None:
            session_identities.setdefault(
                (event.source_epoch, event.session_epoch), set()
            ).add((event.session_id, event.session_num))
    assert all(len(identities) == 1 for identities in source_identities.values())
    assert all(len(identities) == 1 for identities in session_identities.values())


def test_live_and_ibt_semantic_frames_share_event_pipeline():
    semantic_frames = [
        frame(tick=20, session_time=10.0, lap_distance_pct=0.9),
        frame(
            tick=21,
            session_time=11.0,
            lap=5,
            laps_completed=4,
            lap_distance_pct=0.1,
            on_pit_road=True,
            flags=8,
        ),
        frame(
            tick=22,
            session_time=12.0,
            lap=5,
            laps_completed=4,
            lap_distance_pct=0.2,
            on_pit_road=True,
            in_pit_stall=True,
            flags=8,
        ),
    ]
    live, _ = process_telemetry_events(
        normalize_stream(
            semantic_frames,
            source_id="windows-live",
            source_kind=SourceKind.SDK_LIVE,
        )
    )
    offline, _ = process_telemetry_events(
        normalize_stream(
            semantic_frames,
            source_id="audi-spa-ibt",
            source_kind=SourceKind.IBT_OFFLINE,
        )
    )

    assert kinds(live) == kinds(offline)
    live_t0 = live[0].session_time_us
    offline_t0 = offline[0].session_time_us
    assert live_t0 is not None and offline_t0 is not None
    assert [event.session_time_us - live_t0 for event in live] == [
        event.session_time_us - offline_t0 for event in offline
    ]
    assert {event.source_id for event in live} == {"windows-live"}
    assert {event.source_id for event in offline} == {"audi-spa-ibt"}


def test_event_and_receipt_are_immutable_and_json_safe():
    events, receipt = process_telemetry_events(
        normalize_stream([frame(tick=1, session_time=1.0)])
    )

    json.loads(events[0].to_json_line())
    json.loads(receipt.to_json_line())
    json.dumps(events[0].to_dict(), allow_nan=False)
    json.dumps(receipt.to_dict(), allow_nan=False)
    with pytest.raises(FrozenInstanceError):
        events[0].sequence = 99
    with pytest.raises(FrozenInstanceError):
        receipt.event_count = 99
