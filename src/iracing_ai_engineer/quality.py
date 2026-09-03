"""Capability-oriented data-quality checks for iRacing telemetry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .contracts import QUALITY_PROFILE_VERSION
from .ibt import IbtMetadata, IbtReader
from .laps import LapObservation, segment_laps

LAP_REQUIRED = ("SessionTime", "LapDistPct", "Speed")
FUEL_REQUIRED = ("FuelLevel", "OnPitRoad", "PlayerCarInPitStall")
DRIVING_CONTROL_REQUIRED = (
    "Throttle",
    "Brake",
    "SteeringWheelAngle",
    "Speed",
    "Gear",
    "RPM",
)
DRIVING_REQUIRED = DRIVING_CONTROL_REQUIRED + ("OnPitRoad", "PlayerTrackSurface")
INCIDENT_CHANNELS = (
    "PlayerCarMyIncidentCount",
    "PlayerCarDriverIncidentCount",
    "PlayerCarTeamIncidentCount",
)
DRIVING_RANGES = {
    "Throttle": (-0.1, 1.1),
    "Brake": (-0.1, 1.1),
    "SteeringWheelAngle": (-20.0, 20.0),
    "Speed": (-1.0, 150.0),
    "Gear": (-1.0, 20.0),
    "RPM": (-100.0, 30_000.0),
}

PROFILE_CHANNELS = tuple(
    dict.fromkeys(
        (
            "SessionTime",
            "SessionTick",
            "Lap",
            "LapCompleted",
            "LapDistPct",
            "Speed",
            "Throttle",
            "ThrottleRaw",
            "Brake",
            "BrakeRaw",
            "SteeringWheelAngle",
            "Gear",
            "RPM",
            "FuelLevel",
            "FuelLevelPct",
            "FuelUsePerHour",
            "OnPitRoad",
            "PlayerCarInPitStall",
            "PitstopActive",
            "IsOnTrack",
            "IsOnTrackCar",
            "PlayerTrackSurface",
            "PlayerCarMyIncidentCount",
            "PlayerCarDriverIncidentCount",
            "PlayerCarTeamIncidentCount",
        )
        + DRIVING_REQUIRED
    )
)


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    scope: str
    status: str
    metric: str
    threshold: str
    reason: str


@dataclass(frozen=True)
class TimingStats:
    median_step_s: float | None
    p99_step_s: float | None
    max_step_s: float | None
    duplicate_time_steps: int
    time_regressions: int
    tick_regressions: int | None
    dropped_ticks: int | None


@dataclass(frozen=True)
class FieldStat:
    name: str
    present: bool
    count: int
    finite_fraction: float | None
    minimum: float | None
    maximum: float | None


@dataclass(frozen=True)
class CapabilityMatrix:
    replay_readable: bool
    lap_ready: bool
    fuel_ready: bool
    driving_ready: bool
    coaching_evidence_ready: bool


@dataclass(frozen=True)
class QualityReport:
    source_path: str
    source_sha256: str
    metadata: IbtMetadata
    session_context: dict[str, Any]
    timing: TimingStats
    field_stats: tuple[FieldStat, ...]
    laps: tuple[LapObservation, ...]
    gates: tuple[GateResult, ...]
    capabilities: CapabilityMatrix
    quality_profile_version: str = QUALITY_PROFILE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_row(self) -> dict[str, Any]:
        complete = sum(item.structurally_complete for item in self.laps)
        quality_complete = sum(item.quality_complete for item in self.laps)
        clean = sum(item.clean_for_driving for item in self.laps)
        fuel = sum(item.fuel_eligible for item in self.laps)
        return {
            "file": Path(self.source_path).name,
            "size_mb": round(self.metadata.file_size_bytes / 1_000_000, 3),
            "records": self.metadata.record_count,
            "tick_rate_hz": self.metadata.tick_rate_hz,
            "duration_s": round(self.metadata.declared_duration_s, 3),
            "declared_laps": self.metadata.declared_lap_count,
            "structural_laps": complete,
            "quality_complete_laps": quality_complete,
            "clean_driving_laps": clean,
            "fuel_eligible_laps": fuel,
            "dropped_ticks": self.timing.dropped_ticks,
            "replay_readable": self.capabilities.replay_readable,
            "lap_ready": self.capabilities.lap_ready,
            "fuel_ready": self.capabilities.fuel_ready,
            "driving_ready": self.capabilities.driving_ready,
            "coaching_evidence_ready": self.capabilities.coaching_evidence_ready,
        }


def _field_stat(name: str, channels: dict[str, np.ndarray]) -> FieldStat:
    if name not in channels:
        return FieldStat(name, False, 0, None, None, None)
    values = np.asarray(channels[name])
    if values.ndim != 1 or values.dtype.kind not in "biuf":
        return FieldStat(name, True, len(values), None, None, None)
    numeric = values.astype(np.float64, copy=False)
    finite = np.isfinite(numeric)
    if not np.any(finite):
        return FieldStat(name, True, len(values), 0.0, None, None)
    return FieldStat(
        name=name,
        present=True,
        count=len(values),
        finite_fraction=float(np.mean(finite)),
        minimum=float(np.min(numeric[finite])),
        maximum=float(np.max(numeric[finite])),
    )


def _timing_stats(channels: dict[str, np.ndarray]) -> TimingStats:
    if "SessionTime" not in channels:
        return TimingStats(None, None, None, 0, 0, None, None)
    time_s = np.asarray(channels["SessionTime"], dtype=np.float64)
    delta = np.diff(time_s)
    positive = delta[np.isfinite(delta) & (delta > 0)]
    median = float(np.median(positive)) if len(positive) else None
    p99 = float(np.quantile(positive, 0.99)) if len(positive) else None
    maximum = float(np.max(positive)) if len(positive) else None
    duplicate = int(np.sum(np.isfinite(delta) & (delta == 0)))
    regressions = int(np.sum(~np.isfinite(delta) | (delta < 0)))
    tick_regressions: int | None = None
    dropped_ticks: int | None = None
    if "SessionTick" in channels:
        tick_delta = np.diff(np.asarray(channels["SessionTick"], dtype=np.int64))
        tick_regressions = int(np.sum(tick_delta <= 0))
        dropped_ticks = int(np.sum(np.maximum(0, tick_delta - 1)))
    return TimingStats(
        median, p99, maximum, duplicate, regressions, tick_regressions, dropped_ticks
    )


def _missing(names: tuple[str, ...], available: set[str]) -> list[str]:
    return sorted(set(names) - available)


def analyze_ibt(path: str | Path) -> QualityReport:
    with IbtReader(path) as reader:
        available = set(reader.variable_names)
        selected = tuple(name for name in PROFILE_CHANNELS if name in available)
        channels = reader.get_channels(selected)
        metadata = reader.metadata
        session_context = reader.public_session_context()

        timing = _timing_stats(channels)
        stats = tuple(_field_stat(name, channels) for name in PROFILE_CHANNELS)
        stat_map = {item.name: item for item in stats}
        record_lengths_ok = all(
            np.asarray(values).ndim >= 1 and len(np.asarray(values)) == metadata.record_count
            for values in channels.values()
        )
        lap_missing = _missing(LAP_REQUIRED, available)
        if not ({"Lap", "LapCompleted"} & available):
            lap_missing.append("Lap|LapCompleted")
        laps = (
            segment_laps(channels, metadata.tick_rate_hz)
            if not lap_missing
            else tuple()
        )
        complete_count = sum(item.structurally_complete for item in laps)
        quality_complete_count = sum(item.quality_complete for item in laps)
        clean_count = sum(item.clean_for_driving for item in laps)
        fuel_count = sum(item.fuel_eligible for item in laps)

        session_time = stat_map["SessionTime"]
        session_time_ok = (
            session_time.present
            and session_time.finite_fraction == 1.0
            and timing.time_regressions == 0
        )
        expected_step = 1.0 / metadata.tick_rate_hz
        session_values = np.asarray(channels.get("SessionTime", []), dtype=np.float64)
        sampling_consistent = bool(
            timing.median_step_s is not None
            and abs(timing.median_step_s - expected_step) <= expected_step * 0.05
            and len(session_values) >= 2
            and abs(session_values[0] - metadata.session_start_time_s) <= 0.1
            and abs(session_values[-1] - metadata.session_end_time_s) <= 0.1
        )
        replay_readable = bool(
            session_time_ok
            and sampling_consistent
            and record_lengths_ok
            and metadata.trailing_bytes == 0
        )

        lap_distance_valid = False
        if "LapDistPct" in channels:
            lap_distance = np.asarray(channels["LapDistPct"], dtype=np.float64)
            if "IsOnTrackCar" in channels:
                active = np.asarray(channels["IsOnTrackCar"], dtype=bool)
            elif "IsOnTrack" in channels:
                active = np.asarray(channels["IsOnTrack"], dtype=bool)
            else:
                active = np.ones(len(lap_distance), dtype=bool)
            active_distance = lap_distance[active]
            lap_distance_valid = bool(
                len(active_distance)
                and np.mean(np.isfinite(active_distance)) >= 0.9999
                and np.all(active_distance[np.isfinite(active_distance)] >= -0.02)
                and np.all(active_distance[np.isfinite(active_distance)] <= 1.02)
            )
        lap_ready = (
            replay_readable
            and not lap_missing
            and lap_distance_valid
            and quality_complete_count >= 1
        )
        fuel_missing = _missing(FUEL_REQUIRED, available)
        fuel_stat = stat_map["FuelLevel"]
        fuel_values_valid = bool(
            fuel_stat.present
            and fuel_stat.finite_fraction is not None
            and fuel_stat.finite_fraction >= 0.999
            and fuel_stat.minimum is not None
            and fuel_stat.maximum is not None
            and fuel_stat.minimum >= -0.1
            and fuel_stat.maximum <= 500.0
        )
        fuel_ready = (
            lap_ready and not fuel_missing and fuel_values_valid and fuel_count >= 2
        )
        driving_missing = _missing(DRIVING_REQUIRED, available)
        if not (set(INCIDENT_CHANNELS) & available):
            driving_missing.append("incident_channel")

        driving_values_valid = not driving_missing
        driving_range_violations: list[str] = []
        for name in DRIVING_CONTROL_REQUIRED:
            stat = stat_map[name]
            if (
                not stat.present
                or stat.finite_fraction is None
                or stat.finite_fraction < 0.999
            ):
                driving_values_valid = False
        for name, (minimum, maximum) in DRIVING_RANGES.items():
            stat = stat_map[name]
            if (
                stat.minimum is None
                or stat.maximum is None
                or stat.minimum < minimum
                or stat.maximum > maximum
            ):
                driving_values_valid = False
                driving_range_violations.append(name)
        driving_ready = (
            lap_ready and driving_values_valid and clean_count >= 3
        )
        # Cohort matching (weather, track state, fuel, tire state, and traffic)
        # is intentionally not implemented in Stage 0. Never promote raw clean
        # lap count into a personalized coaching evidence claim.
        coaching_ready = False

        gates: list[GateResult] = [
            GateResult(
                "record_layout",
                "source",
                "PASS" if metadata.trailing_bytes == 0 else "WARN",
                f"trailing_bytes={metadata.trailing_bytes}",
                "0 trailing bytes",
                "The declared fixed-width record area matches the file boundary."
                if metadata.trailing_bytes == 0
                else "Extra bytes remain after the declared record area.",
            ),
            GateResult(
                "channel_shapes",
                "source",
                "PASS" if record_lengths_ok else "FAIL",
                f"all_selected_channels_match_records={record_lengths_ok}",
                f"each selected channel has {metadata.record_count} records",
                "All profiled channels share the declared record grain."
                if record_lengths_ok
                else "At least one channel does not match the declared record count.",
            ),
            GateResult(
                "session_time",
                "source",
                "PASS"
                if session_time_ok and timing.duplicate_time_steps == 0
                else "WARN"
                if session_time_ok
                else "FAIL",
                (
                    f"finite={session_time.finite_fraction}, "
                    f"duplicates={timing.duplicate_time_steps}, "
                    f"regressions={timing.time_regressions}"
                ),
                "100% finite, no regressions; duplicates reported",
                "SessionTime and record order support deterministic replay."
                if session_time_ok and timing.duplicate_time_steps == 0
                else "Duplicate timestamps are retained and ordered by record/tick."
                if session_time_ok
                else "SessionTime cannot support a single ordered replay segment.",
            ),
            GateResult(
                "sampling_consistency",
                "source",
                "PASS" if sampling_consistent else "FAIL",
                (
                    f"declared_hz={metadata.tick_rate_hz}, "
                    f"median_step_s={timing.median_step_s}"
                ),
                "median SessionTime step within 5% of 1/tick_rate and endpoints match header",
                "Header timing and telemetry timing agree."
                if sampling_consistent
                else "Declared sampling metadata is inconsistent with telemetry records.",
            ),
            GateResult(
                "tick_continuity",
                "source",
                "PASS"
                if timing.tick_regressions == 0 and timing.dropped_ticks == 0
                else "WARN",
                (
                    f"regressions={timing.tick_regressions}, "
                    f"dropped={timing.dropped_ticks}"
                ),
                "0 regressions and 0 dropped ticks",
                "Tick continuity is exact."
                if timing.tick_regressions == 0 and timing.dropped_ticks == 0
                else "Replay remains possible, but affected laps need quality labels.",
            ),
            GateResult(
                "lap_channels",
                "capability",
                "PASS" if not lap_missing else "SKIP",
                "missing=" + (",".join(lap_missing) if lap_missing else "none"),
                "SessionTime, LapDistPct, Speed, and Lap or LapCompleted",
                "Minimum lap channels are present."
                if not lap_missing
                else "Lap analysis is disabled rather than imputing channels.",
            ),
            GateResult(
                "lap_distance_validity",
                "capability",
                "PASS" if lap_distance_valid else "FAIL" if not lap_missing else "SKIP",
                f"active_lap_distance_valid={lap_distance_valid}",
                ">=99.99% finite active values within [-0.02, 1.02]",
                "Active lap-distance values satisfy the positional contract."
                if lap_distance_valid
                else "Lap-distance validity is insufficient for lap analysis.",
            ),
            GateResult(
                "structural_laps",
                "capability",
                "PASS" if complete_count >= 1 else "FAIL",
                f"complete_laps={complete_count}",
                ">=1 boundary-to-boundary lap",
                "At least one complete lap can be segmented."
                if complete_count >= 1
                else "No conservative boundary-to-boundary lap was found.",
            ),
            GateResult(
                "quality_complete_laps",
                "capability",
                "PASS" if quality_complete_count >= 1 else "FAIL",
                f"quality_complete_laps={quality_complete_count}",
                ">=1 structural lap with >=99.5% ticks and <=0.25 s max gap",
                "At least one lap meets the Stage 0 timing-quality contract."
                if quality_complete_count >= 1
                else "No structural lap has sufficient timing quality.",
            ),
            GateResult(
                "fuel_evidence",
                "capability",
                "PASS" if fuel_ready else "FAIL" if not fuel_missing else "SKIP",
                (
                    f"eligible_laps={fuel_count}, values_valid={fuel_values_valid}, "
                    f"missing={','.join(fuel_missing) or 'none'}"
                ),
                ">=2 complete no-pit laps and valid FuelLevel values",
                "Enough initial evidence for a simple fuel estimate."
                if fuel_ready
                else "Keep fuel modeling disabled until more eligible laps exist.",
            ),
            GateResult(
                "driving_smoke_evidence",
                "capability",
                "PASS"
                if driving_ready
                else "FAIL"
                if not driving_missing
                else "SKIP",
                (
                    f"clean_laps={clean_count}, values_valid={driving_values_valid}, "
                    f"range_violations={','.join(driving_range_violations) or 'none'}, "
                    f"missing={','.join(driving_missing) or 'none'}"
                ),
                ">=3 clean laps and valid control ranges",
                "Enough matched laps for an algorithm smoke test."
                if driving_ready
                else "Do not generate comparative driving advice yet.",
            ),
            GateResult(
                "coaching_evidence",
                "capability",
                "SKIP",
                (
                    f"clean_laps={clean_count}, cohort_receipt_attached=false, "
                    f"missing={','.join(driving_missing) or 'none'}"
                ),
                "attached trusted cohort receipt and >=8 matched clean laps",
                "This standalone quality pass does not attach weather, track-state, "
                "fuel, tire, and traffic cohort evidence.",
            ),
        ]

        report = QualityReport(
            source_path=str(Path(path).expanduser().resolve()),
            source_sha256=reader.source_sha256,
            metadata=metadata,
            session_context=session_context,
            timing=timing,
            field_stats=stats,
            laps=laps,
            gates=tuple(gates),
            capabilities=CapabilityMatrix(
                replay_readable=replay_readable,
                lap_ready=lap_ready,
                fuel_ready=fuel_ready,
                driving_ready=driving_ready,
                coaching_evidence_ready=coaching_ready,
            ),
        )
        reader.verify_source_unchanged()
        return report
