"""Deterministic offline replay receipts for frozen IBT telemetry."""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .contracts import (
    LAP_ALGORITHM_VERSION,
    QUALITY_PROFILE_VERSION,
    READER_CONTRACT_VERSION,
    REPLAY_CONTRACT_VERSION,
)
from .ibt import IbtReader
from .laps import detect_boundaries, segment_laps

DEFAULT_REPLAY_CHANNELS = (
    "SessionTime",
    "SessionTick",
    "Lap",
    "LapCompleted",
    "LapDistPct",
    "Speed",
    "Throttle",
    "Brake",
    "SteeringWheelAngle",
    "Gear",
    "RPM",
    "FuelLevel",
    "OnPitRoad",
    "PlayerCarInPitStall",
    "PlayerTrackSurface",
    "PlayerCarMyIncidentCount",
)

_CANONICAL_NAN = struct.pack("<Q", 0x7FF8000000000000)


@dataclass(frozen=True)
class ReplayReceipt:
    source_sha256: str
    schema_sha256: str
    canonical_schema_sha256: str
    normalized_frames_sha256: str
    events_sha256: str
    results_sha256: str
    config_sha256: str
    replay_sha256: str
    frame_count: int
    event_count: int
    lap_observation_count: int
    structurally_complete_lap_count: int
    clean_driving_lap_count: int
    channels: tuple[str, ...]
    event_pipeline_mode: str = "batch-v1"
    reader_contract_version: str = READER_CONTRACT_VERSION
    replay_contract_version: str = REPLAY_CONTRACT_VERSION
    lap_algorithm_version: str = LAP_ALGORITHM_VERSION
    quality_profile_version: str = QUALITY_PROFILE_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _length_prefixed(payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + payload


def _canonical_scalar(value: object) -> bytes:
    if isinstance(value, np.generic):
        value = value.item()
    if value is None:
        return b"m"
    if isinstance(value, bool):
        return b"b\x01" if value else b"b\x00"
    if isinstance(value, int):
        return b"i" + struct.pack("<q", value)
    if isinstance(value, float):
        if math.isnan(value):
            return b"f" + _CANONICAL_NAN
        if value == 0.0:
            value = 0.0
        return b"f" + struct.pack("<d", value)
    if isinstance(value, (bytes, bytearray)):
        return b"y" + _length_prefixed(bytes(value))
    if isinstance(value, str):
        return b"s" + _length_prefixed(value.encode("utf-8"))
    if isinstance(value, (list, tuple, np.ndarray)):
        parts = [_canonical_scalar(item) for item in value]
        return b"a" + b"".join(_length_prefixed(item) for item in parts)
    raise TypeError(f"unsupported replay value type: {type(value).__name__}")


def normalized_frames_sha256(
    channels: Mapping[str, np.ndarray],
    *,
    channel_order: Sequence[str],
    chunk_size: int = 4096,
) -> str:
    """Hash a frame stream independently of path, wall clock, and chunking."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    selected = tuple(channel_order)
    if not selected:
        raise ValueError("at least one replay channel is required")
    missing = [name for name in selected if name not in channels]
    if missing:
        raise KeyError(f"missing replay channels: {', '.join(missing)}")
    arrays = {name: np.asarray(channels[name]) for name in selected}
    lengths = {len(array) for array in arrays.values()}
    if len(lengths) != 1:
        raise ValueError("replay channels have inconsistent record counts")
    record_count = lengths.pop()

    digest = hashlib.sha256()
    digest.update(_length_prefixed(REPLAY_CONTRACT_VERSION.encode("ascii")))
    digest.update(_length_prefixed(_canonical_json(selected)))
    for chunk_start in range(0, record_count, chunk_size):
        chunk_end = min(record_count, chunk_start + chunk_size)
        for index in range(chunk_start, chunk_end):
            payload = struct.pack("<Q", index)
            for name in selected:
                payload += _length_prefixed(_canonical_scalar(arrays[name][index]))
            digest.update(_length_prefixed(payload))
    return digest.hexdigest()


def _event_payload(
    channels: Mapping[str, np.ndarray], tick_rate_hz: int
) -> list[dict[str, object]]:
    precedence = {"source_reset": 0, "strong_wrap": 1, "counter_only": 1}
    events = []
    for boundary in detect_boundaries(channels, tick_rate_hz):
        if boundary.kind in {"file_start", "file_end"}:
            continue
        events.append(
            {
                "frame_index": boundary.frame_index,
                "boundary_confidence": boundary.confidence,
                "kind": boundary.kind,
                "lap_algorithm_version": LAP_ALGORITHM_VERSION,
                "precedence": precedence[boundary.kind],
                "sequence": len(events),
                "tags": list(boundary.tags),
                "time_us": round(boundary.time_s * 1_000_000),
            }
        )
    events.sort(key=lambda item: (item["time_us"], item["precedence"], item["sequence"]))
    for sequence, event in enumerate(events):
        event["sequence"] = sequence
    return events


def _result_payload(
    channels: Mapping[str, np.ndarray], tick_rate_hz: int
) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for lap in segment_laps(channels, tick_rate_hz):
        payload.append(
            {
                "boundary_confidence": lap.boundary_confidence,
                "cleanliness_observable": lap.cleanliness_observable,
                "clean_for_driving": lap.clean_for_driving,
                "distance_coverage_ppm": round(lap.distance_coverage_laps * 1_000_000),
                "duration_us": round(lap.duration_s * 1_000_000),
                "end_boundary": lap.end_boundary,
                "end_frame_exclusive": lap.end_frame_exclusive,
                "end_time_us": round(lap.end_time_s * 1_000_000),
                "fuel_burn_ml": None
                if lap.fuel_burn_l is None
                else round(lap.fuel_burn_l * 1_000),
                "fuel_end_ml": None
                if lap.fuel_end_l is None
                else round(lap.fuel_end_l * 1_000),
                "fuel_eligible": lap.fuel_eligible,
                "fuel_start_ml": None
                if lap.fuel_start_l is None
                else round(lap.fuel_start_l * 1_000),
                "incident_delta": lap.incident_delta,
                "invalid_reasons": list(lap.invalid_reasons),
                "lap_algorithm_version": lap.algorithm_version,
                "max_gap_us": round(lap.max_gap_s * 1_000_000),
                "missing_ticks": lap.missing_ticks,
                "off_track_fraction_ppm": None
                if lap.off_track_fraction is None
                else round(lap.off_track_fraction * 1_000_000),
                "on_pit_road_fraction_ppm": round(lap.on_pit_road_fraction * 1_000_000),
                "ordinal": lap.ordinal,
                "quality_complete": lap.quality_complete,
                "source_lap_end": lap.source_lap_end,
                "source_lap_start": lap.source_lap_start,
                "start_boundary": lap.start_boundary,
                "start_frame": lap.start_frame,
                "start_time_us": round(lap.start_time_s * 1_000_000),
                "structurally_complete": lap.structurally_complete,
                "tags": list(lap.tags),
                "tick_coverage_ppm": round(lap.tick_coverage * 1_000_000),
                "tick_regressions": lap.tick_regressions,
                "time_duplicate_steps": lap.duplicate_time_steps,
                "time_regressions": lap.time_regressions,
            }
        )
    return payload


def replay_ibt(
    path: str | Path,
    *,
    channel_order: Sequence[str] = DEFAULT_REPLAY_CHANNELS,
    frame_hash_chunk_size: int = 4096,
) -> ReplayReceipt:
    with IbtReader(path) as reader:
        selected = tuple(name for name in channel_order if name in reader.variable_names)
        required = {"SessionTime", "LapDistPct"}
        if not required.issubset(selected):
            missing = sorted(required - set(selected))
            raise KeyError(f"missing mandatory replay channels: {', '.join(missing)}")
        channels = reader.get_channels(selected)
        frame_digest = normalized_frames_sha256(
            channels, channel_order=selected, chunk_size=frame_hash_chunk_size
        )
        events = _event_payload(channels, reader.metadata.tick_rate_hz)
        results = _result_payload(channels, reader.metadata.tick_rate_hz)
        events_digest = hashlib.sha256(_canonical_json(events)).hexdigest()
        results_digest = hashlib.sha256(_canonical_json(results)).hexdigest()
        config = {
            "channels": selected,
            "event_pipeline_mode": "batch-v1",
            "lap_algorithm_version": LAP_ALGORITHM_VERSION,
            "quality_profile_version": QUALITY_PROFILE_VERSION,
            "reader_contract_version": READER_CONTRACT_VERSION,
            "replay_contract_version": REPLAY_CONTRACT_VERSION,
        }
        config_digest = hashlib.sha256(_canonical_json(config)).hexdigest()
        source_digest = reader.source_sha256
        final_payload = {
            "canonical_schema_sha256": reader.metadata.canonical_schema_sha256,
            "config_sha256": config_digest,
            "events_sha256": events_digest,
            "frame_count": reader.metadata.record_count,
            "normalized_frames_sha256": frame_digest,
            "results_sha256": results_digest,
            "schema_sha256": reader.metadata.schema_sha256,
            "source_sha256": source_digest,
        }
        replay_digest = hashlib.sha256(_canonical_json(final_payload)).hexdigest()
        complete_count = sum(bool(item["structurally_complete"]) for item in results)
        clean_count = sum(bool(item["clean_for_driving"]) for item in results)
        receipt = ReplayReceipt(
            source_sha256=source_digest,
            schema_sha256=reader.metadata.schema_sha256,
            canonical_schema_sha256=reader.metadata.canonical_schema_sha256,
            normalized_frames_sha256=frame_digest,
            events_sha256=events_digest,
            results_sha256=results_digest,
            config_sha256=config_digest,
            replay_sha256=replay_digest,
            frame_count=reader.metadata.record_count,
            event_count=len(events),
            lap_observation_count=len(results),
            structurally_complete_lap_count=complete_count,
            clean_driving_lap_count=clean_count,
            channels=selected,
            event_pipeline_mode="batch-v1",
        )
        reader.verify_source_unchanged()
        return receipt
