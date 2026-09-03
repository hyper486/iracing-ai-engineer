"""Fail-closed PRECHECK_ONLY semantics before a canonical live capture.

Python recomputes every analytical claim from one sealed collector snapshot.
Its receipt digest is only an integrity checksum; it is not an authenticity
primitive.  The R8 production boundary is one fresh ``python -I -B`` process
inside an administrator-protected embedded runtime.  That supervisor owns the
preflight capture handle from creation through analysis, so no path is reopened
between collection and admission.

The public injected-transport runner is test-only and can never emit REAL
provenance.  The CLI-only production runner additionally freezes the exact
Windows transport class executable namespace so in-process method replacement
fails closed instead of propagating into the SDK collection path.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, TextIO

from .adapters import (
    TrackContextAvailability,
    TrackContextStatus,
    open_collector_jsonl_snapshot,
)
from .collector import (
    COLLECTOR_CONTRACT_VERSION,
    ReadOnlySdkTransport,
    collect_transport_to_jsonl,
)
from .driving_model_replay import build_driving_model_replay
from .events import process_telemetry_events
from .sdk_probe import SdkProbeUnavailable, WindowsPyirsdkTransport
from .telemetry import Presence, SourceKind, TelemetryField, TelemetrySample

LIVE_PREFLIGHT_CONTRACT_VERSION = "live-capture-preflight-v3"
PREFLIGHT_POLICY_CONTRACT_VERSION = "live-preflight-policy-v1"
PRODUCTION_TRANSPORT_ATTESTATION_VERSION = "windows-readonly-sdk-transport-v1"
DEFAULT_PREFLIGHT_DURATION_S = 30.0
SUPERVISOR_PREFLIGHT_MAX_BYTES = 256 * 1024**2
DEFAULT_MINIMUM_FRAME_RATE_RATIO_NUMERATOR = 9
DEFAULT_MINIMUM_FRAME_RATE_RATIO_DENOMINATOR = 10
_SNAPSHOT_MEMORY_LIMIT = 64 * 1024 * 1024
_PREFLIGHT_FILENAME_RE = re.compile(r"^preflight-[A-Za-z0-9][A-Za-z0-9._-]*\.jsonl$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PASS_GATE_NAMES = frozenset(
    {
        "capture_context",
        "collector_quality",
        "driving_pipeline",
        "full_session_context",
        "required_channels",
        "shared_event_pipeline",
        "source_provenance",
    }
)
_ROOT_KEYS = frozenset(
    {
        "admission_recomputed",
        "advice_generated",
        "advisor_only",
        "can_start_live_capture",
        "capture",
        "capture_binding",
        "capture_context",
        "collector_evidence",
        "collector_receipt",
        "contract_version",
        "driving_summary",
        "evidence_class",
        "event_receipt",
        "gates",
        "live_acceptance_eligible",
        "observation",
        "policy",
        "production_semantic_digest",
        "production_transport_attested",
        "receipt_sha256",
        "recommendations",
        "required_field_audit",
        "simulator_identity",
        "snapshot_method",
        "status",
        "track_context",
        "transport_attestation",
        "vehicle_control_enabled",
        "wait_reasons",
        "would_pass_real_gates",
    }
)


class LivePreflightError(ValueError):
    """Raised when PRECHECK_ONLY evidence cannot be admitted truthfully."""


class PreflightEvidenceClass(StrEnum):
    REAL_SDK_PRECHECK_ONLY = "REAL_SDK_PRECHECK_ONLY"
    SYNTHETIC_TEST_ONLY = "SYNTHETIC_TEST_ONLY"


_REQUIRED_FIELDS: tuple[
    tuple[str, Callable[[TelemetrySample], TelemetryField[object]]], ...
] = (
    ("SessionNum", lambda sample: sample.session.session_num),
    ("SessionTick", lambda sample: sample.session.session_tick),
    ("SessionTime", lambda sample: sample.session.session_time_s),
    ("Lap", lambda sample: sample.lap.lap_number),
    ("LapDistPct", lambda sample: sample.lap.lap_distance_pct),
    ("Speed", lambda sample: sample.lap.speed_mps),
    ("Throttle", lambda sample: sample.controls.throttle),
    ("Brake", lambda sample: sample.controls.brake),
    ("SteeringWheelAngle", lambda sample: sample.controls.steering_angle_rad),
    ("FuelLevel", lambda sample: sample.fuel.level_l),
    ("OnPitRoad", lambda sample: sample.pit.on_pit_road),
    ("PlayerCarInPitStall", lambda sample: sample.pit.in_pit_stall),
    ("PlayerTrackSurface", lambda sample: sample.flags.player_track_surface),
    ("IsOnTrack", lambda sample: sample.flags.is_on_track),
    ("IsOnTrackCar", lambda sample: sample.flags.is_on_track_car),
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _with_receipt_hash(payload: dict[str, object]) -> dict[str, object]:
    if "receipt_sha256" in payload:
        raise LivePreflightError("preflight payload already contains receipt_sha256")
    result = dict(payload)
    result["receipt_sha256"] = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return result


def _validate_capture_path(path: Path) -> Path:
    if not _PREFLIGHT_FILENAME_RE.fullmatch(path.name):
        raise LivePreflightError(
            "preflight output must use a noncanonical preflight-*.jsonl filename"
        )
    return path


def _is_reparse(stat_result: os.stat_result) -> bool:
    attribute = getattr(stat_result, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attribute & marker)


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    if os.name == "nt":
        # CPython exposes the Windows creation time as st_ctime for a path
        # stat, while descriptor stat can expose the NTFS change time.  They
        # describe the same file but are not a stable cross-API comparison.
        # The volume/file id pair remains the authoritative Windows identity;
        # size, mtime, and attributes bind the observed state.
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            int(getattr(value, "st_file_attributes", 0)),
        )
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@dataclass(frozen=True, slots=True)
class _SealedCollectorSnapshot:
    capture_identity: dict[str, object]
    handle: TextIO
    snapshot_method: str = "PRIVATE_SPOOLED_SNAPSHOT_FROM_O_NOFOLLOW_FD"


@contextmanager
def _sealed_capture_snapshot(path: Path) -> Iterator[_SealedCollectorSnapshot]:
    """Copy one O_NOFOLLOW descriptor into an anonymous private snapshot."""

    path = _validate_capture_path(path)
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise LivePreflightError("preflight capture cannot be lstat'ed") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or _is_reparse(before)
    ):
        raise LivePreflightError("preflight capture must be a plain non-reparse file")

    flags = os.O_RDONLY
    for flag_name in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW"):
        flags |= int(getattr(os, flag_name, 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LivePreflightError(
            "preflight capture cannot be opened without following"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(after_open)
            or _stat_identity(before) != _stat_identity(opened)
            or _stat_identity(after_open) != _stat_identity(opened)
        ):
            raise LivePreflightError("preflight capture changed while it was opened")

        digest = hashlib.sha256()
        byte_size = 0
        with tempfile.SpooledTemporaryFile(
            mode="w+b", max_size=_SNAPSHOT_MEMORY_LIMIT
        ) as binary_snapshot:
            while chunk := os.read(descriptor, 1024 * 1024):
                binary_snapshot.write(chunk)
                digest.update(chunk)
                byte_size += len(chunk)
            after_read = os.fstat(descriptor)
            path_after_read = os.lstat(path)
            if (
                _stat_identity(after_read) != _stat_identity(opened)
                or _stat_identity(path_after_read) != _stat_identity(opened)
                or byte_size != opened.st_size
                or byte_size <= 0
            ):
                raise LivePreflightError("preflight capture changed during snapshot")
            capture_identity: dict[str, object] = {
                "byte_size": byte_size,
                "filename": path.name,
                "sha256": digest.hexdigest(),
            }
            binary_snapshot.flush()
            binary_snapshot.seek(0)
            text_snapshot = io.TextIOWrapper(
                binary_snapshot,
                encoding="utf-8",
                errors="strict",
                newline="",
            )
            try:
                yield _SealedCollectorSnapshot(capture_identity, text_snapshot)
                text_snapshot.flush()
                source_after_analysis = os.fstat(descriptor)
                path_after_analysis = os.lstat(path)
                if (
                    _stat_identity(source_after_analysis) != _stat_identity(opened)
                    or _stat_identity(path_after_analysis) != _stat_identity(opened)
                    or _is_reparse(path_after_analysis)
                ):
                    raise LivePreflightError(
                        "preflight capture changed during sealed-snapshot analysis"
                    )
            finally:
                with suppress(ValueError):
                    text_snapshot.detach()
            binary_snapshot.seek(0)
            final_digest = hashlib.sha256()
            final_size = 0
            while chunk := binary_snapshot.read(1024 * 1024):
                final_digest.update(chunk)
                final_size += len(chunk)
            if (
                final_size != capture_identity["byte_size"]
                or final_digest.hexdigest() != capture_identity["sha256"]
            ):
                raise LivePreflightError(
                    "private preflight snapshot changed during analysis"
                )
    except UnicodeError as exc:
        raise LivePreflightError("preflight capture is not strict UTF-8") from exc
    finally:
        os.close(descriptor)


@contextmanager
def _sealed_capture_handle(
    handle: BinaryIO,
    *,
    filename: str,
) -> Iterator[_SealedCollectorSnapshot]:
    """Analyze the supervisor's caller-owned capture without reopening a path."""

    if not _PREFLIGHT_FILENAME_RE.fullmatch(filename):
        raise LivePreflightError(
            "preflight handle must have a noncanonical preflight-*.jsonl identity"
        )
    if handle.closed or not handle.readable() or not handle.seekable():
        raise LivePreflightError("preflight capture handle must remain readable/seekable")
    try:
        handle.flush()
        opened = os.fstat(handle.fileno())
    except (OSError, ValueError) as exc:
        raise LivePreflightError("preflight capture handle is unavailable") from exc
    if not stat.S_ISREG(opened.st_mode) or _is_reparse(opened) or opened.st_size <= 0:
        raise LivePreflightError("preflight capture handle is not a nonempty plain file")

    digest = hashlib.sha256()
    byte_size = 0
    try:
        handle.seek(0)
        while chunk := handle.read(1024 * 1024):
            if type(chunk) is not bytes:
                raise LivePreflightError("preflight capture handle is not binary")
            digest.update(chunk)
            byte_size += len(chunk)
        after_hash = os.fstat(handle.fileno())
        if _stat_identity(after_hash) != _stat_identity(opened) or byte_size != opened.st_size:
            raise LivePreflightError("preflight capture handle changed during sealing")
        capture_identity: dict[str, object] = {
            "byte_size": byte_size,
            "filename": filename,
            "sha256": digest.hexdigest(),
        }
        handle.seek(0)
        text_snapshot = io.TextIOWrapper(
            handle,
            encoding="utf-8",
            errors="strict",
            newline="",
            write_through=True,
        )
        try:
            yield _SealedCollectorSnapshot(
                capture_identity,
                text_snapshot,
                "CALLER_OWNED_SINGLE_PROCESS_FILE_HANDLE_V1",
            )
        finally:
            with suppress(ValueError):
                text_snapshot.detach()

        handle.seek(0)
        final_digest = hashlib.sha256()
        final_size = 0
        while chunk := handle.read(1024 * 1024):
            if type(chunk) is not bytes:
                raise LivePreflightError("preflight capture handle is not binary")
            final_digest.update(chunk)
            final_size += len(chunk)
        final = os.fstat(handle.fileno())
        if (
            _stat_identity(final) != _stat_identity(opened)
            or final_size != byte_size
            or final_digest.hexdigest() != capture_identity["sha256"]
        ):
            raise LivePreflightError("preflight capture handle changed during analysis")
    except UnicodeError as exc:
        raise LivePreflightError("preflight capture is not strict UTF-8") from exc


def _seconds_to_us(value: float, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LivePreflightError(f"{label} must be numeric")
    if not math.isfinite(value) or value <= 0:
        raise LivePreflightError(f"{label} must be finite and positive")
    microseconds = round(value * 1_000_000)
    if microseconds <= 0 or not math.isclose(
        value, microseconds / 1_000_000, rel_tol=0.0, abs_tol=1e-12
    ):
        raise LivePreflightError(f"{label} must have integral microsecond precision")
    return microseconds


@dataclass(frozen=True, slots=True)
class _PreflightPolicy:
    requested_duration_us: int
    minimum_capture_span_us: int
    minimum_frame_rate_ratio_numerator: int
    minimum_frame_rate_ratio_denominator: int
    stale_after_us: int
    contract_version: str = PREFLIGHT_POLICY_CONTRACT_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "minimum_capture_span_us": self.minimum_capture_span_us,
            "minimum_frame_rate_ratio_denominator": (
                self.minimum_frame_rate_ratio_denominator
            ),
            "minimum_frame_rate_ratio_numerator": (
                self.minimum_frame_rate_ratio_numerator
            ),
            "requested_duration_us": self.requested_duration_us,
            "stale_after_us": self.stale_after_us,
        }


def _policy(duration_s: float, stale_after_s: float) -> _PreflightPolicy:
    duration_us = _seconds_to_us(duration_s, "duration_s")
    stale_us = _seconds_to_us(stale_after_s, "stale_after_s")
    numerator = DEFAULT_MINIMUM_FRAME_RATE_RATIO_NUMERATOR
    denominator = DEFAULT_MINIMUM_FRAME_RATE_RATIO_DENOMINATOR
    minimum_span = (duration_us * numerator + denominator - 1) // denominator
    return _PreflightPolicy(
        requested_duration_us=duration_us,
        minimum_capture_span_us=minimum_span,
        minimum_frame_rate_ratio_numerator=numerator,
        minimum_frame_rate_ratio_denominator=denominator,
        stale_after_us=stale_us,
    )


def _simulator_identity(
    process_id: int, start_ticks: int, session_id: int
) -> dict[str, int]:
    values = {
        "process_id": process_id,
        "start_time_utc_ticks": start_ticks,
        "windows_session_id": session_id,
    }
    for key, value in values.items():
        if type(value) is not int or value < 1:
            raise LivePreflightError(f"simulator identity {key} is invalid")
    return values


def _gate(reasons: list[str]) -> dict[str, object]:
    unique = list(dict.fromkeys(reasons))
    return {"reasons": unique, "status": "PASS" if not unique else "BLOCKED"}


def _required_field_audit(
    samples: tuple[TelemetrySample, ...],
) -> tuple[dict[str, int], int, bool]:
    missing_or_invalid = {
        name: sum(getter(sample).presence is not Presence.PRESENT for sample in samples)
        for name, getter in _REQUIRED_FIELDS
    }
    in_car_samples = sum(
        sample.flags.is_on_track.presence is Presence.PRESENT
        and sample.flags.is_on_track.value is True
        and sample.flags.is_on_track_car.presence is Presence.PRESENT
        and sample.flags.is_on_track_car.value is True
        for sample in samples
    )
    final_in_car = bool(
        samples
        and samples[-1].flags.is_on_track.presence is Presence.PRESENT
        and samples[-1].flags.is_on_track.value is True
        and samples[-1].flags.is_on_track_car.presence is Presence.PRESENT
        and samples[-1].flags.is_on_track_car.value is True
    )
    return dict(sorted(missing_or_invalid.items())), in_car_samples, final_in_car


def _terminal_collector_receipt(handle: TextIO) -> dict[str, object]:
    handle.seek(0)
    terminal: object = None
    for raw_line in handle:
        terminal = json.loads(raw_line)
    if (
        type(terminal) is not dict
        or terminal.get("record_type") != "collector_receipt"
        or set(terminal)
        != {"collector_contract_version", "record_type", "sequence", "receipt"}
        or type(terminal.get("receipt")) is not dict
    ):
        raise LivePreflightError(
            "sealed capture has no exact terminal collector receipt"
        )
    return dict(terminal["receipt"])


def _collector_receipt_from_evidence(
    evidence: Mapping[str, object]
) -> dict[str, object]:
    keys = (
        "collector_contract_version",
        "completion_status",
        "duplicate_conflict_count",
        "duplicate_sample_count",
        "dropped_tick_count",
        "event_record_count",
        "first_buffer_tick",
        "frame_record_count",
        "last_buffer_tick",
        "records_sha256",
        "samples_seen",
        "schema_change_count",
        "schema_epoch_count",
        "schema_record_count",
        "semantic_record_count",
        "session_epoch_count",
        "session_info_record_count",
        "session_reset_count",
        "stale_event_count",
    )
    result = {key: evidence[key] for key in keys}
    result["run_record_count"] = 1
    return result


@dataclass(frozen=True, slots=True)
class _SnapshotAnalysis:
    capture: dict[str, object]
    capture_binding: dict[str, object]
    capture_context: dict[str, object]
    collector_evidence: dict[str, object]
    collector_receipt: dict[str, object]
    driving_summary: dict[str, object]
    event_receipt: dict[str, object]
    gates: dict[str, dict[str, object]]
    observation: dict[str, object]
    policy: dict[str, object]
    required_field_audit: dict[str, object]
    snapshot_method: str
    track_context: dict[str, object]
    wait_reasons: list[str]
    would_pass_real_gates: bool


def _analyze_snapshot(
    snapshot: _SealedCollectorSnapshot,
    *,
    expected_source_id: str,
    expected_session_id: str,
    policy: _PreflightPolicy,
) -> _SnapshotAnalysis:
    stale_after_s = policy.stale_after_us / 1_000_000
    with open_collector_jsonl_snapshot(
        snapshot.handle, stale_after_s=stale_after_s
    ) as run:
        collector_evidence = run.evidence.to_dict()
        track_context = run.track_context.to_dict()
        samples = tuple(run.samples)
    _, event_value = process_telemetry_events(samples)
    event_receipt = event_value.to_dict()
    with open_collector_jsonl_snapshot(
        snapshot.handle, stale_after_s=stale_after_s
    ) as run:
        driving = build_driving_model_replay(run)
    collector_receipt = _terminal_collector_receipt(snapshot.handle)

    missing_fields, in_car_count, final_in_car = _required_field_audit(samples)
    tick_rates = collector_evidence["tick_rate_hz_values"]
    capture_span_us = collector_evidence["capture_span_us"]
    tick_rate_hz = tick_rates[0] if len(tick_rates) == 1 else None
    frame_interval_count = max(collector_evidence["frame_record_count"] - 1, 0)
    observation = {
        "capture_span_us": capture_span_us,
        "frame_interval_count": frame_interval_count,
        "tick_rate_hz": tick_rate_hz,
    }

    source_reasons: list[str] = []
    if collector_evidence["authenticity_status"] != "SELF_CONSISTENT_NOT_AUTHENTICATED":
        source_reasons.append("AUTHENTICITY_STATUS_INVALID")
    if collector_evidence["collector_contract_version"] != COLLECTOR_CONTRACT_VERSION:
        source_reasons.append("COLLECTOR_CONTRACT_INVALID")
    if collector_evidence["source_id"] != expected_source_id:
        source_reasons.append("SOURCE_ID_MISMATCH")
    if collector_evidence["session_id"] != expected_session_id:
        source_reasons.append("SESSION_ID_MISMATCH")
    if collector_evidence["source_kind"] != SourceKind.SDK_LIVE.value:
        source_reasons.append("NOT_SDK_LIVE")
    if collector_evidence["sim_mode"] != "full":
        source_reasons.append("NOT_FULL_SIM_MODE")
    if collector_evidence["completion_status"] != "COMPLETE":
        source_reasons.append("CAPTURE_NOT_COMPLETE")

    collector_reasons: list[str] = []
    for field, reason in (
        ("capture_clock_regression_count", "CAPTURE_CLOCK_REGRESSION"),
        ("driver_info_key_count", "DRIVER_INFO_PERSISTED"),
        ("dropped_tick_count", "DROPPED_TICKS"),
        ("duplicate_conflict_count", "DUPLICATE_CONFLICTS"),
        ("read_error_field_count", "SDK_READ_FIELD_ERRORS"),
        ("read_error_frame_count", "SDK_READ_ERRORS"),
        ("schema_change_count", "SCHEMA_CHANGED"),
        ("session_reset_count", "SESSION_RESET"),
        ("stale_event_count", "SOURCE_STALE_EVENTS"),
    ):
        if collector_evidence[field] != 0:
            collector_reasons.append(reason)
    if (
        collector_evidence["event_record_count"]
        != collector_evidence["duplicate_sample_count"]
    ):
        collector_reasons.append("UNEXPECTED_COLLECTOR_EVENTS")
    if (
        len(tick_rates) != 1
        or type(tick_rate_hz) is not int
        or not 1 <= tick_rate_hz <= 360
    ):
        collector_reasons.append("TICK_RATE_NOT_EXACT")
    if (
        type(capture_span_us) is not int
        or capture_span_us < policy.minimum_capture_span_us
    ):
        collector_reasons.append("CAPTURE_SPAN_TOO_SHORT")
    if (
        type(capture_span_us) is not int
        or capture_span_us <= 0
        or type(tick_rate_hz) is not int
        or frame_interval_count
        * 1_000_000
        * policy.minimum_frame_rate_ratio_denominator
        < capture_span_us * tick_rate_hz * policy.minimum_frame_rate_ratio_numerator
    ):
        collector_reasons.append("OBSERVED_FRAME_RATE_TOO_LOW")
    if collector_receipt != _collector_receipt_from_evidence(collector_evidence):
        collector_reasons.append("COLLECTOR_RECEIPT_ADAPTER_MISMATCH")

    scopes = collector_evidence["session_info_scope_counts"]
    track_reasons: list[str] = []
    if scopes.get("FULL", 0) < 1:
        track_reasons.append("FULL_SESSION_INFO_MISSING")
    if scopes.get("PARTIAL", 0) != 0 or scopes.get("UNAVAILABLE", 0) != 0:
        track_reasons.append("SESSION_INFO_NOT_ALWAYS_FULL")
    if track_context["availability"] != TrackContextAvailability.AVAILABLE.value:
        track_reasons.append("TRACK_LENGTH_UNAVAILABLE")
    if track_context["status"] != TrackContextStatus.VERIFIED.value:
        track_reasons.append("TRACK_LENGTH_NOT_VERIFIED")
    if type(track_context["track_length_mm"]) is not int:
        track_reasons.append("TRACK_LENGTH_NOT_INTEGER")

    field_reasons = [
        f"MISSING_OR_INVALID:{name}" for name, count in missing_fields.items() if count
    ]
    context_reasons: list[str] = []
    if in_car_count < 2:
        context_reasons.append("TOO_FEW_IN_CAR_SAMPLES")
    if not final_in_car:
        context_reasons.append("FINAL_SAMPLE_NOT_IN_CAR")

    event_reasons: list[str] = []
    frame_count = collector_evidence["frame_record_count"]
    if event_receipt["sample_count"] != frame_count:
        event_reasons.append("EVENT_SAMPLE_COUNT_MISMATCH")
    if event_receipt["accepted_sample_count"] != frame_count:
        event_reasons.append("EVENT_ACCEPTED_COUNT_MISMATCH")
    if event_receipt["rejected_sample_count"] != 0:
        event_reasons.append("EVENT_REJECTED_SAMPLES")
    if event_receipt["source_epoch_count"] != 1:
        event_reasons.append("MULTIPLE_SOURCE_EPOCHS")
    if event_receipt["session_epoch_count"] != 1:
        event_reasons.append("MULTIPLE_SESSION_EPOCHS")

    series = driving["series_evidence"]
    driving_missing = series["missing_channel_sample_counts"]
    driving_reasons: list[str] = []
    if driving["readiness_status"] == "FAIL":
        driving_reasons.append("DRIVING_REPLAY_FAILED")
    if driving["event_receipt"] != event_receipt:
        driving_reasons.append("DRIVING_EVENT_RECEIPT_MISMATCH")
    if any(
        driving_missing.get(name, len(samples))
        for name in (
            "SessionTime",
            "SessionTick",
            "Lap",
            "LapDistPct",
            "Speed",
            "Throttle",
            "Brake",
            "SteeringWheelAngle",
            "OnPitRoad",
            "PlayerTrackSurface",
        )
    ):
        driving_reasons.append("DRIVING_REQUIRED_CHANNELS_MISSING")
    if series["incident_source_field"] is None:
        driving_reasons.append("INCIDENT_CHANNEL_UNOBSERVABLE")
    if series["normalized_dropped_tick_count"] != 0:
        driving_reasons.append("NORMALIZED_DROPPED_TICKS")
    if series["modeled_sample_count"] != frame_count:
        driving_reasons.append("MODELED_SAMPLE_COUNT_MISMATCH")

    gates = {
        "capture_context": _gate(context_reasons),
        "collector_quality": _gate(collector_reasons),
        "driving_pipeline": _gate(driving_reasons),
        "full_session_context": _gate(track_reasons),
        "required_channels": _gate(field_reasons),
        "shared_event_pipeline": _gate(event_reasons),
        "source_provenance": _gate(source_reasons),
    }
    wait_reasons = [
        f"{gate_name}:{reason}"
        for gate_name, gate in gates.items()
        for reason in gate["reasons"]
    ]
    would_pass = all(gate["status"] == "PASS" for gate in gates.values())
    driving_summary = {
        "driving_replay_sha256": driving["driving_replay_sha256"],
        "event_receipt": driving["event_receipt"],
        "incident_source_field": series["incident_source_field"],
        "missing_channel_sample_counts": driving_missing,
        "modeled_sample_count": series["modeled_sample_count"],
        "normalized_dropped_tick_count": series["normalized_dropped_tick_count"],
        "normalized_input_receipt": driving["normalized_input_receipt"],
        "quality_reasons": driving["quality_gate"]["reasons"],
        "readiness_status": driving["readiness_status"],
    }
    return _SnapshotAnalysis(
        capture=snapshot.capture_identity,
        capture_binding={
            "filename": snapshot.capture_identity["filename"],
            "session_id": expected_session_id,
            "source_id": expected_source_id,
        },
        capture_context={
            "final_sample_in_car": final_in_car,
            "in_car_sample_count": in_car_count,
            "sample_count": len(samples),
        },
        collector_evidence=collector_evidence,
        collector_receipt=collector_receipt,
        driving_summary=driving_summary,
        event_receipt=event_receipt,
        gates=gates,
        observation=observation,
        policy=policy.to_dict(),
        required_field_audit={"missing_or_invalid_counts": missing_fields},
        snapshot_method=snapshot.snapshot_method,
        track_context=track_context,
        wait_reasons=wait_reasons,
        would_pass_real_gates=would_pass,
    )


def _analysis_fields(analysis: _SnapshotAnalysis) -> dict[str, object]:
    return {
        "capture": analysis.capture,
        "capture_binding": analysis.capture_binding,
        "capture_context": analysis.capture_context,
        "collector_evidence": analysis.collector_evidence,
        "collector_receipt": analysis.collector_receipt,
        "driving_summary": analysis.driving_summary,
        "event_receipt": analysis.event_receipt,
        "gates": analysis.gates,
        "observation": analysis.observation,
        "policy": analysis.policy,
        "required_field_audit": analysis.required_field_audit,
        "track_context": analysis.track_context,
    }


def _base_payload(
    analysis: _SnapshotAnalysis,
    *,
    evidence_class: PreflightEvidenceClass,
    simulator_identity: dict[str, int] | None,
    transport_attestation: dict[str, object] | None,
) -> dict[str, object]:
    is_real = evidence_class is PreflightEvidenceClass.REAL_SDK_PRECHECK_ONLY
    can_start = is_real and analysis.would_pass_real_gates
    status = (
        ("PASS" if can_start else "WAIT")
        if is_real
        else PreflightEvidenceClass.SYNTHETIC_TEST_ONLY.value
    )
    return {
        "admission_recomputed": can_start,
        "advice_generated": False,
        "advisor_only": True,
        "can_start_live_capture": can_start,
        **_analysis_fields(analysis),
        "contract_version": LIVE_PREFLIGHT_CONTRACT_VERSION,
        "evidence_class": evidence_class.value,
        "live_acceptance_eligible": False,
        "production_semantic_digest": None,
        "production_transport_attested": is_real,
        "recommendations": [],
        "simulator_identity": simulator_identity,
        "snapshot_method": analysis.snapshot_method,
        "status": status,
        "transport_attestation": transport_attestation,
        "vehicle_control_enabled": False,
        "wait_reasons": analysis.wait_reasons,
        "would_pass_real_gates": analysis.would_pass_real_gates,
    }


_SEMANTIC_DIGEST_DOMAIN = b"AEIS_PRECHECK_SEMANTIC_INTEGRITY_V1\0"


def _with_semantic_digest(payload: dict[str, object]) -> dict[str, object]:
    """Close PASS semantics with an unkeyed, recomputable integrity digest.

    This deliberately provides no authenticity.  The external PowerShell
    runtime/helper closure is the only production authenticity boundary.
    """

    if payload["status"] != "PASS":
        return payload
    material = dict(payload)
    material.pop("production_semantic_digest")
    result = dict(payload)
    result["production_semantic_digest"] = hashlib.sha256(
        _SEMANTIC_DIGEST_DOMAIN + _canonical_json(material)
    ).hexdigest()
    return result


def _expected_semantic_payload(
    analysis: _SnapshotAnalysis,
    *,
    simulator_identity: dict[str, int],
    transport_attestation: dict[str, object],
    production_semantic_digest: str,
) -> dict[str, object]:
    expected = _base_payload(
        analysis,
        evidence_class=PreflightEvidenceClass.REAL_SDK_PRECHECK_ONLY,
        simulator_identity=simulator_identity,
        transport_attestation=transport_attestation,
    )
    expected["production_semantic_digest"] = production_semantic_digest
    return expected


def _validate_receipt_against_analysis(
    receipt: object,
    analysis: _SnapshotAnalysis,
    *,
    simulator_identity: dict[str, int],
    transport_attestation: dict[str, object],
) -> dict[str, object]:
    if type(receipt) is not dict or set(receipt) != _ROOT_KEYS:
        raise LivePreflightError(
            "preflight admission receipt has an invalid exact schema"
        )
    semantic = dict(receipt)
    digest = semantic.pop("receipt_sha256")
    if (
        type(digest) is not str
        or not _SHA256_RE.fullmatch(digest)
        or hashlib.sha256(_canonical_json(semantic)).hexdigest() != digest
    ):
        raise LivePreflightError("preflight admission receipt hash does not close")
    semantic_digest = semantic.get("production_semantic_digest")
    if type(semantic_digest) is not str or not _SHA256_RE.fullmatch(semantic_digest):
        raise LivePreflightError("preflight semantic integrity digest is absent")
    material = dict(semantic)
    material.pop("production_semantic_digest")
    expected_digest = hashlib.sha256(
        _SEMANTIC_DIGEST_DOMAIN + _canonical_json(material)
    ).hexdigest()
    if semantic_digest != expected_digest:
        raise LivePreflightError("preflight semantic integrity digest is invalid")
    expected = _expected_semantic_payload(
        analysis,
        simulator_identity=simulator_identity,
        transport_attestation=transport_attestation,
        production_semantic_digest=semantic_digest,
    )
    if semantic != expected:
        raise LivePreflightError(
            "preflight admission receipt differs from sealed-snapshot recomputation"
        )
    if (
        receipt["status"] != "PASS"
        or receipt["can_start_live_capture"] is not True
        or receipt["admission_recomputed"] is not True
        or receipt["evidence_class"]
        != PreflightEvidenceClass.REAL_SDK_PRECHECK_ONLY.value
        or receipt["production_transport_attested"] is not True
        or receipt["live_acceptance_eligible"] is not False
        or receipt["advisor_only"] is not True
        or receipt["vehicle_control_enabled"] is not False
        or receipt["advice_generated"] is not False
        or receipt["recommendations"] != []
        or receipt["wait_reasons"] != []
        or set(receipt["gates"]) != _PASS_GATE_NAMES
    ):
        raise LivePreflightError(
            "preflight result is not an exact recomputed REAL PASS"
        )
    return dict(receipt)


def _transport_attestation(
    transport_type: type[object],
    *,
    authenticity_root: str = "EXTERNAL_POWERSHELL_RUNTIME_AND_HELPER_CLOSURE",
) -> dict[str, object]:
    source_path = Path(__import__(transport_type.__module__, fromlist=["x"]).__file__)
    return {
        "authenticity_root": authenticity_root,
        "bytecode_writes_disabled": True,
        "contract_version": PRODUCTION_TRANSPORT_ATTESTATION_VERSION,
        "event_access": "SYNCHRONIZE_ONLY",
        "implementation_module": transport_type.__module__,
        "implementation_qualname": transport_type.__qualname__,
        "implementation_source_sha256": _sha256_file(source_path),
        "mapping_access": "FILE_MAP_READ_ONLY",
        "python_isolated": True,
        "pyirsdk_distribution_version": importlib.metadata.version("pyirsdk"),
        "receipt_digest_role": "SEMANTIC_INTEGRITY_ONLY",
        "simulator_command_api_exposed": False,
        "shared_memory_write_enabled": False,
    }


def _wait_payload(
    reason: str,
    *,
    capture_binding: dict[str, object],
    policy: _PreflightPolicy,
    simulator_identity: dict[str, int],
    transport_attestation: dict[str, object],
) -> dict[str, object]:
    gate = _gate([reason])
    payload: dict[str, object] = {
        "admission_recomputed": False,
        "advice_generated": False,
        "advisor_only": True,
        "can_start_live_capture": False,
        "capture": None,
        "capture_binding": capture_binding,
        "capture_context": None,
        "collector_evidence": None,
        "collector_receipt": None,
        "contract_version": LIVE_PREFLIGHT_CONTRACT_VERSION,
        "driving_summary": None,
        "evidence_class": PreflightEvidenceClass.REAL_SDK_PRECHECK_ONLY.value,
        "event_receipt": None,
        "gates": {"transport": gate},
        "live_acceptance_eligible": False,
        "observation": None,
        "policy": policy.to_dict(),
        "production_semantic_digest": None,
        "production_transport_attested": False,
        "recommendations": [],
        "required_field_audit": None,
        "simulator_identity": simulator_identity,
        "snapshot_method": None,
        "status": "WAIT",
        "track_context": None,
        "transport_attestation": transport_attestation,
        "vehicle_control_enabled": False,
        "wait_reasons": [f"transport:{reason}"],
        "would_pass_real_gates": False,
    }
    return _with_receipt_hash(payload)


def _run_core(
    transport: ReadOnlySdkTransport,
    capture_path: Path,
    *,
    source_id: str,
    session_id: str,
    evidence_class: PreflightEvidenceClass,
    simulator_identity: dict[str, int] | None,
    transport_attestation: dict[str, object] | None,
    wait_seconds: float,
    duration_s: float,
    poll_seconds: float,
    stale_after_s: float,
    max_reads: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    path = _validate_capture_path(capture_path)
    selected_policy = _policy(duration_s, stale_after_s)
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise LivePreflightError("wait_seconds must be finite and non-negative")
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise LivePreflightError("poll_seconds must be finite and positive")
    collector_receipt = collect_transport_to_jsonl(
        transport,
        path,
        source_id=source_id,
        session_id=session_id,
        expected_source_kind=SourceKind.SDK_LIVE,
        wait_seconds=wait_seconds,
        duration_s=duration_s,
        poll_seconds=poll_seconds,
        fields=None,
        stale_after_s=stale_after_s,
        include_driver_info=False,
        fsync_each_record=True,
        max_reads=max_reads,
        monotonic=monotonic,
        sleep=sleep,
    )
    with _sealed_capture_snapshot(path) as snapshot:
        analysis = _analyze_snapshot(
            snapshot,
            expected_source_id=source_id,
            expected_session_id=session_id,
            policy=selected_policy,
        )
        if collector_receipt.to_dict() != analysis.collector_receipt:
            raise LivePreflightError(
                "collector process receipt differs from sealed-snapshot receipt"
            )
        payload = _base_payload(
            analysis,
            evidence_class=evidence_class,
            simulator_identity=simulator_identity,
            transport_attestation=transport_attestation,
        )
        if evidence_class is PreflightEvidenceClass.REAL_SDK_PRECHECK_ONLY:
            if simulator_identity is None or transport_attestation is None:
                raise LivePreflightError(
                    "REAL preflight requires the isolated production path"
                )
            payload = _with_semantic_digest(payload)
        receipt = _with_receipt_hash(payload)
        if receipt["status"] == "PASS":
            if simulator_identity is None or transport_attestation is None:
                raise LivePreflightError("production preflight context is absent")
            _validate_receipt_against_analysis(
                receipt,
                analysis,
                simulator_identity=simulator_identity,
                transport_attestation=transport_attestation,
            )
        return receipt


def _run_core_handle(
    transport: ReadOnlySdkTransport,
    capture_handle: BinaryIO,
    *,
    capture_filename: str,
    source_id: str,
    session_id: str,
    simulator_identity: dict[str, int],
    transport_attestation: dict[str, object],
    wait_seconds: float,
    duration_s: float,
    poll_seconds: float,
    stale_after_s: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Production-only preflight over one supervisor-owned binary handle."""

    # Imported lazily so the collector owner can freeze the handle API without
    # creating a partially compatible fallback to the path writer.
    from .collector import collect_transport_to_jsonl_handle

    selected_policy = _policy(duration_s, stale_after_s)
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise LivePreflightError("wait_seconds must be finite and non-negative")
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise LivePreflightError("poll_seconds must be finite and positive")
    collector_receipt = collect_transport_to_jsonl_handle(
        transport,
        capture_handle,
        source_id=source_id,
        session_id=session_id,
        expected_source_kind=SourceKind.SDK_LIVE,
        wait_seconds=wait_seconds,
        duration_s=duration_s,
        poll_seconds=poll_seconds,
        fields=None,
        stale_after_s=stale_after_s,
        include_driver_info=False,
        fsync_each_record=True,
        max_output_bytes=SUPERVISOR_PREFLIGHT_MAX_BYTES,
        monotonic=monotonic,
        sleep=sleep,
    )
    with _sealed_capture_handle(
        capture_handle,
        filename=capture_filename,
    ) as snapshot:
        analysis = _analyze_snapshot(
            snapshot,
            expected_source_id=source_id,
            expected_session_id=session_id,
            policy=selected_policy,
        )
        if collector_receipt.to_dict() != analysis.collector_receipt:
            raise LivePreflightError(
                "collector process receipt differs from same-handle analysis"
            )
        payload = _with_semantic_digest(
            _base_payload(
                analysis,
                evidence_class=PreflightEvidenceClass.REAL_SDK_PRECHECK_ONLY,
                simulator_identity=simulator_identity,
                transport_attestation=transport_attestation,
            )
        )
        receipt = _with_receipt_hash(payload)
        _validate_receipt_against_analysis(
            receipt,
            analysis,
            simulator_identity=simulator_identity,
            transport_attestation=transport_attestation,
        )
        return receipt


def run_live_preflight_transport(
    transport: ReadOnlySdkTransport,
    capture_path: str | Path,
    *,
    source_id: str,
    session_id: str,
    wait_seconds: float = 120.0,
    duration_s: float = DEFAULT_PREFLIGHT_DURATION_S,
    poll_seconds: float = 0.01,
    stale_after_s: float = 0.5,
    max_reads: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Exercise an injected fixture, always as ``SYNTHETIC_TEST_ONLY``."""

    if type(transport) is _FROZEN_WINDOWS_TRANSPORT_TYPE:
        raise LivePreflightError(
            "production transport is reserved for the frozen runner"
        )
    return _run_core(
        transport,
        Path(capture_path),
        source_id=source_id,
        session_id=session_id,
        evidence_class=PreflightEvidenceClass.SYNTHETIC_TEST_ONLY,
        simulator_identity=None,
        transport_attestation=None,
        wait_seconds=wait_seconds,
        duration_s=duration_s,
        poll_seconds=poll_seconds,
        stale_after_s=stale_after_s,
        max_reads=max_reads,
        monotonic=monotonic,
        sleep=sleep,
    )


_FROZEN_WINDOWS_TRANSPORT_TYPE = WindowsPyirsdkTransport


def _executable_class_namespace(cls: type[object]) -> tuple[tuple[str, object], ...]:
    return tuple(
        sorted(
            (
                (name, value)
                for name, value in vars(cls).items()
                if callable(value)
                or isinstance(value, (classmethod, property, staticmethod))
            ),
            key=lambda item: item[0],
        )
    )


_FROZEN_WINDOWS_TRANSPORT_EXECUTABLES = _executable_class_namespace(
    _FROZEN_WINDOWS_TRANSPORT_TYPE
)


def _make_transport_class_guard(
    transport_type: type[object],
    frozen_executables: tuple[tuple[str, object], ...],
) -> Callable[[], None]:
    frozen_namespace_reader = _executable_class_namespace

    def guard() -> None:
        current = frozen_namespace_reader(transport_type)
        if len(current) != len(frozen_executables):
            raise LivePreflightError(
                "production transport executable namespace was replaced"
            )
        for (current_name, current_value), (frozen_name, frozen_value) in zip(
            current, frozen_executables, strict=True
        ):
            if current_name != frozen_name or current_value is not frozen_value:
                raise LivePreflightError(
                    "production transport executable namespace was replaced"
                )

    return guard


_assert_frozen_transport_class = _make_transport_class_guard(
    _FROZEN_WINDOWS_TRANSPORT_TYPE,
    _FROZEN_WINDOWS_TRANSPORT_EXECUTABLES,
)
del _make_transport_class_guard


def _require_fresh_isolated_runtime() -> None:
    if sys.flags.isolated != 1 or sys.flags.dont_write_bytecode != 1:
        raise LivePreflightError(
            "REAL preflight requires a fresh externally closed python -I -B process"
        )


def _make_cli_production_runner(
    transport_type: type[WindowsPyirsdkTransport],
) -> Callable[..., dict[str, object]]:
    if transport_type is not _FROZEN_WINDOWS_TRANSPORT_TYPE:
        raise LivePreflightError(
            "production capability requires the frozen transport type"
        )
    attestation_bytes = _canonical_json(_transport_attestation(transport_type))
    frozen_policy = _policy
    frozen_require_isolated = _require_fresh_isolated_runtime
    frozen_run_core = _run_core
    frozen_simulator_identity = _simulator_identity
    frozen_transport_guard = _assert_frozen_transport_class
    frozen_validate_capture_path = _validate_capture_path
    frozen_wait_payload = _wait_payload

    def attestation() -> dict[str, object]:
        value = json.loads(attestation_bytes)
        if type(value) is not dict:  # pragma: no cover
            raise AssertionError("frozen transport attestation is invalid")
        return value

    def run(
        capture_path: str | Path,
        *,
        source_id: str,
        session_id: str,
        expected_sim_process_id: int,
        expected_sim_start_time_utc_ticks: int,
        expected_windows_session_id: int,
        wait_seconds: float = 120.0,
        duration_s: float = DEFAULT_PREFLIGHT_DURATION_S,
        poll_seconds: float = 0.01,
        stale_after_s: float = 0.5,
    ) -> dict[str, object]:
        frozen_require_isolated()
        frozen_transport_guard()
        path = frozen_validate_capture_path(Path(capture_path))
        selected_policy = frozen_policy(duration_s, stale_after_s)
        identity = frozen_simulator_identity(
            expected_sim_process_id,
            expected_sim_start_time_utc_ticks,
            expected_windows_session_id,
        )
        binding = {
            "filename": path.name,
            "session_id": session_id,
            "source_id": source_id,
        }
        try:
            frozen_transport_guard()
            transport = transport_type()
            frozen_transport_guard()
            receipt = frozen_run_core(
                transport,
                path,
                source_id=source_id,
                session_id=session_id,
                evidence_class=PreflightEvidenceClass.REAL_SDK_PRECHECK_ONLY,
                simulator_identity=identity,
                transport_attestation=attestation(),
                wait_seconds=wait_seconds,
                duration_s=duration_s,
                poll_seconds=poll_seconds,
                stale_after_s=stale_after_s,
            )
            frozen_transport_guard()
            return receipt
        except SdkProbeUnavailable:
            frozen_transport_guard()
            if path.exists():
                raise
            return frozen_wait_payload(
                "SDK_UNAVAILABLE",
                capture_binding=binding,
                policy=selected_policy,
                simulator_identity=identity,
                transport_attestation=attestation(),
            )

    return run


_run_windows_live_preflight_cli_only = _make_cli_production_runner(
    _FROZEN_WINDOWS_TRANSPORT_TYPE
)
del _make_cli_production_runner


def _make_supervisor_handle_preflight_runner(
    transport_type: type[WindowsPyirsdkTransport],
) -> Callable[..., dict[str, object]]:
    if transport_type is not _FROZEN_WINDOWS_TRANSPORT_TYPE:
        raise LivePreflightError(
            "supervisor capability requires the frozen transport type"
        )
    attestation_bytes = _canonical_json(
        _transport_attestation(
            transport_type,
            authenticity_root="ADMIN_PROTECTED_SINGLE_PROCESS_SUPERVISOR_V1",
        )
    )
    frozen_policy = _policy
    frozen_require_isolated = _require_fresh_isolated_runtime
    frozen_run_core_handle = _run_core_handle
    frozen_transport_guard = _assert_frozen_transport_class
    frozen_wait_payload = _wait_payload

    def attestation() -> dict[str, object]:
        value = json.loads(attestation_bytes)
        if type(value) is not dict:  # pragma: no cover
            raise AssertionError("frozen transport attestation is invalid")
        return value

    def run(
        capture_handle: BinaryIO,
        *,
        capture_filename: str,
        source_id: str,
        session_id: str,
        simulator_identity: dict[str, int],
        wait_seconds: float = 120.0,
        duration_s: float = DEFAULT_PREFLIGHT_DURATION_S,
        poll_seconds: float = 0.01,
        stale_after_s: float = 0.5,
    ) -> dict[str, object]:
        frozen_require_isolated()
        if os.name != "nt":
            raise LivePreflightError("REAL supervisor preflight is Windows-only")
        frozen_transport_guard()
        identity = _simulator_identity(
            simulator_identity.get("process_id"),
            simulator_identity.get("start_time_utc_ticks"),
            simulator_identity.get("windows_session_id"),
        )
        if (
            capture_handle.closed
            or not capture_handle.readable()
            or not capture_handle.writable()
            or not capture_handle.seekable()
        ):
            raise LivePreflightError(
                "supervisor preflight requires an open caller-owned binary handle"
            )
        initial = os.fstat(capture_handle.fileno())
        if (
            not stat.S_ISREG(initial.st_mode)
            or _is_reparse(initial)
            or initial.st_size != 0
            or capture_handle.tell() != 0
        ):
            raise LivePreflightError(
                "supervisor preflight handle must be a new empty regular file"
            )
        selected_policy = frozen_policy(duration_s, stale_after_s)
        binding = {
            "filename": capture_filename,
            "session_id": session_id,
            "source_id": source_id,
        }
        try:
            frozen_transport_guard()
            transport = transport_type()
            frozen_transport_guard()
            receipt = frozen_run_core_handle(
                transport,
                capture_handle,
                capture_filename=capture_filename,
                source_id=source_id,
                session_id=session_id,
                simulator_identity=identity,
                transport_attestation=attestation(),
                wait_seconds=wait_seconds,
                duration_s=duration_s,
                poll_seconds=poll_seconds,
                stale_after_s=stale_after_s,
            )
            frozen_transport_guard()
            return receipt
        except SdkProbeUnavailable as exc:
            frozen_transport_guard()
            capture_handle.flush()
            if os.fstat(capture_handle.fileno()).st_size != 0:
                raise LivePreflightError(
                    "SDK-unavailable preflight wrote an unadmitted capture prefix"
                ) from exc
            return frozen_wait_payload(
                "SDK_UNAVAILABLE",
                capture_binding=binding,
                policy=selected_policy,
                simulator_identity=identity,
                transport_attestation=attestation(),
            )

    return run


_run_windows_live_preflight_handle_supervisor_only = (
    _make_supervisor_handle_preflight_runner(_FROZEN_WINDOWS_TRANSPORT_TYPE)
)
del _make_supervisor_handle_preflight_runner
