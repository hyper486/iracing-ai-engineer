"""R8 same-handle live engineer-session wrapper.

This module is deliberately narrower than a live strategy product.  It proves
that one already validated SDK-live collector run can traverse the installable
engineer-session pipeline while every unavailable event-rule, calibration,
traffic, tire, and label capability remains fail-closed.

The internal fuel scenario is the frozen development-smoke contract fixture.
It is not event truth and can never produce reader-facing, tactical, audible,
executable, or vehicle-control output through this wrapper.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import NoReturn

from .adapters import ValidatedCollectorRun, open_collector_jsonl_snapshot
from .engineer_session import (
    EngineerSessionError,
    build_engineer_session_from_collector_snapshot,
    canonical_sha256,
    validate_engineer_session,
)
from .fuel import FuelScenario
from .telemetry import (
    Presence,
    Provenance,
    QualityStatus,
    SourceKind,
    TelemetryField,
    TelemetrySample,
)

LIVE_ENGINEER_SESSION_CONTRACT_VERSION = "live-engineer-session-v1"
LIVE_ANALYSIS_AUTHORITY_CONTRACT_VERSION = (
    "single-process-live-analysis-authority-v2"
)
OBSERVED_LIVE_EVIDENCE_CONTRACT_VERSION = "observed-live-evidence-v1"
SUPERVISOR_CONTRACT_VERSION = "windows-live-supervisor-v1"
EXECUTION_MODE = "PROTECTED_SINGLE_PROCESS_SAME_HANDLE_V1"
ATTESTATION_STATUS = "SELF_CONSISTENT_NOT_AUTHENTICATED"
CAPTURE_SNAPSHOT_METHOD = "CALLER_OWNED_SINGLE_PROCESS_FILE_HANDLE_V1"
INSTALL_CONTRACT_VERSION = "windows-embedded-collector-install-v2"
CODE_ROOT = r"C:\Program Files\AEIS\releases\collector-v4-0.1.0-r8"
CODE_TRUST_MODEL = "ADMIN_PROTECTED_READ_EXECUTE_V1"
SECURITY_DESCRIPTOR_PROFILE = (
    "SYSTEM_AND_BUILTIN_ADMIN_FULL_RUNTIME_USER_RX_V1"
)
RUNTIME_TOKEN_ADMISSION_CONTRACT_VERSION = "windows-runtime-token-admission-v1"
FIXED_RUNTIME_USER_SID = (
    "S-1-5-21-0-0-0-1001"
)
DEV_SMOKE_PROFILE_ID = "development-smoke-unbound-v1"
DEV_SMOKE_PROFILE_BYTE_SIZE = 662
DEV_SMOKE_PROFILE_SHA256 = (
    "7706d831001dfdd1256cbf4101caecbd9e2675028c80e0a0dd69e05ad8423a25"
)
SCENARIO_ROLE = "DEVELOPMENT_SMOKE_CONFIG_NOT_EVENT_TRUTH"
FUEL_PIPELINE_ROLE = "FUEL_PIPELINE_CONTRACT_ONLY"

_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
_FILE_ID_RE = re.compile(r"^[0-9a-f]{8}:[0-9a-f]{8}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_AUTHORITY_KEYS = frozenset(
    {
        "ancestor_admission",
        "attestation_status",
        "authority_sha256",
        "capture_byte_size",
        "capture_file",
        "capture_file_id",
        "capture_sha256",
        "capture_snapshot_method",
        "capture_volume_serial_number",
        "code_root",
        "code_trust_model",
        "contract_version",
        "dev_smoke_profile_byte_size",
        "dev_smoke_profile_id",
        "dev_smoke_profile_sha256",
        "execution_mode",
        "install_contract_version",
        "install_manifest_sha256",
        "preflight_production_semantic_digest",
        "preflight_receipt_sha256",
        "project_wheel_sha256",
        "run_id",
        "runtime_manifest_self_sha256",
        "runtime_manifest_sha256",
        "runtime_token_admission",
        "runtime_tree_sha256",
        "security_descriptor_profile",
        "security_tree_object_count",
        "security_tree_sha256",
        "sim_process_id",
        "sim_start_time_utc_ticks",
        "supervisor_contract_version",
        "windows_session_id",
    }
)
_LIVE_KEYS = frozenset(
    {
        "advisor_only",
        "analysis_authority",
        "attestation_status",
        "capability_gates",
        "closure",
        "contract_version",
        "derivation_status",
        "execution_mode",
        "live_engineer_session_sha256",
        "observed_live_evidence",
        "pipeline_proof",
        "recommendations",
        "safety",
        "scenario_boundary",
        "status",
    }
)
_PIPELINE_PROOF_KEYS = frozenset(
    {
        "admission_receipt_sha256",
        "advisor_clock_receipt_sha256",
        "component_hashes",
        "component_statuses",
        "contract_version",
        "decision_clock",
        "engineer_session_sha256",
        "fresh_admission_count",
        "input_lineage",
        "internal_core_persisted",
        "pipeline_proof_sha256",
        "projection_role",
        "semantic_hashes",
        "strategy_context_sha256",
    }
)
_INPUT_LINEAGE_KEYS = frozenset(
    {
        "event_receipt_sha256",
        "input_evidence_sha256",
        "input_kind",
        "input_lineage_sha256",
        "normalized_samples_sha256",
        "sample_count",
        "session_id",
        "source_content_sha256",
        "source_id",
        "source_kind",
    }
)
_OBSERVED_KEYS = frozenset(
    {
        "contract_version",
        "current_fuel",
        "decision_clock",
        "horizon",
        "input_evidence_sha256",
        "laps_completed",
        "observed_live_evidence_sha256",
        "pits_open",
        "source_binding",
    }
)
_SOURCE_BINDING_KEYS = frozenset(
    {
        "records_sha256",
        "session_id",
        "source_id",
        "source_kind",
    }
)
_CLOSURE_KEYS = frozenset(
    {
        "advisor_clock_receipt_sha256",
        "analysis_authority_sha256",
        "capture_byte_size",
        "capture_file_id",
        "capture_sha256",
        "capture_snapshot_method",
        "capture_volume_serial_number",
        "context_sha256",
        "decision_session_time_us",
        "decision_tick",
        "engineer_session_sha256",
        "event_receipt_sha256",
        "input_evidence_sha256",
        "input_lineage_sha256",
        "normalized_samples_sha256",
        "observed_live_evidence_sha256",
        "sample_count",
        "session_id",
        "source_content_sha256",
        "source_id",
        "source_kind",
    }
)

_DEVELOPMENT_SMOKE_SCENARIO = FuelScenario(
    current_fuel_l=20.0,
    tank_capacity_l=120.0,
    refuel_rate_l_per_s=2.0,
    remaining_laps=10,
    reserve_l=1.0,
    minimum_valid_laps=5,
)
_SCENARIO_BOUNDARY = {
    "dev_smoke_profile_byte_size": DEV_SMOKE_PROFILE_BYTE_SIZE,
    "dev_smoke_profile_id": DEV_SMOKE_PROFILE_ID,
    "dev_smoke_profile_sha256": DEV_SMOKE_PROFILE_SHA256,
    "fuel_pipeline_role": FUEL_PIPELINE_ROLE,
    "internal_fuel_scenario_persisted": False,
    "internal_fuel_scenario_role": "FROZEN_M0_PIPELINE_FIXTURE_NOT_EVENT_TRUTH",
    "official_event_rules": False,
    "profile_binding_role": "UNBOUND_CONFIG_BYTES_ONLY",
    "profile_supplies_complete_fuel_scenario": False,
    "race_recommendation": "BLOCKED",
    "scenario_role": SCENARIO_ROLE,
    "values_exposed_to_reader_facing_advice": False,
}
_CAPABILITY_GATES = {
    "current_tire_wear": "SKIP",
    "fuel_scenario": "WAIT_EVENT_FUEL_SCENARIO",
    "human_labels": "WAIT_HUMAN_LABELS",
    "official_rules": "WAIT_EVENT_RULES_IDENTITY",
    "personalized_coaching": "SKIP",
    "pit_loss_calibration": "WAIT_MATCHED_PIT_LOSS_BASELINE",
    "pit_service_contents": "SKIP_NOT_OBSERVABLE",
    "service_labels": "WAIT_SERVICE_LABELS",
    "traffic": "WAIT_TRAFFIC_DATA",
}
_SAFETY = {
    "audio_emitted_count": 0,
    "executable_true_count": 0,
    "live_recommendation_count": 0,
    "tactical_output_count": 0,
    "vehicle_control_enabled": False,
}
_FORBIDDEN_PERSISTED_KEYS = frozenset(
    {
        "action",
        "change_tires",
        "components",
        "current_fuel_l",
        "estimated_stationary_service_s",
        "estimated_total_pit_loss_s",
        "fuel_add_l",
        "minimum_valid_laps",
        "recommendation",
        "recommendation_basis",
        "refuel_rate_l_per_s",
        "remaining_laps",
        "reserve_l",
        "scenario",
        "scenario_values",
        "service_selection",
        "selected_service",
        "strategy_context",
        "tank_capacity_l",
        "valid_until",
        "_validated_scenario_values",
    }
)


class LiveEngineerSessionError(ValueError):
    """Fail-closed error raised by the R8 live wrapper."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise LiveEngineerSessionError(code, message)


def _json_copy(value: object, name: str) -> dict[str, object]:
    try:
        copied = json.loads(
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise LiveEngineerSessionError(
            "SCHEMA_INVALID", f"{name} is not canonical JSON"
        ) from exc
    if type(copied) is not dict:
        _fail("SCHEMA_INVALID", f"{name} must be a plain object")
    return copied


def _mapping(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        _fail("SCHEMA_INVALID", f"{name} must be a plain object")
    return value


def _exact(
    value: object, keys: frozenset[str], name: str
) -> dict[str, object]:
    result = _mapping(value, name)
    if set(result) != keys:
        _fail("SCHEMA_INVALID", f"{name} keys are invalid")
    return result


def _sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail("SCHEMA_INVALID", f"{name} must be a lowercase SHA-256 digest")
    return value


def _plain_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail("SCHEMA_INVALID", f"{name} must be an integer >= {minimum}")
    return value


def _finite_number(
    value: object, name: str, *, minimum: float | None = None
) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (minimum is not None and float(value) < minimum)
    ):
        _fail("SCHEMA_INVALID", f"{name} must be a finite number")
    return value


def _validate_authority(value: object) -> dict[str, object]:
    authority = _exact(
        _json_copy(value, "analysis authority"),
        _AUTHORITY_KEYS,
        "analysis authority",
    )
    fixed = {
        "ancestor_admission": "PASS",
        "attestation_status": ATTESTATION_STATUS,
        "capture_snapshot_method": CAPTURE_SNAPSHOT_METHOD,
        "code_root": CODE_ROOT,
        "code_trust_model": CODE_TRUST_MODEL,
        "contract_version": LIVE_ANALYSIS_AUTHORITY_CONTRACT_VERSION,
        "dev_smoke_profile_byte_size": DEV_SMOKE_PROFILE_BYTE_SIZE,
        "dev_smoke_profile_id": DEV_SMOKE_PROFILE_ID,
        "dev_smoke_profile_sha256": DEV_SMOKE_PROFILE_SHA256,
        "execution_mode": EXECUTION_MODE,
        "install_contract_version": INSTALL_CONTRACT_VERSION,
        "security_descriptor_profile": SECURITY_DESCRIPTOR_PROFILE,
        "supervisor_contract_version": SUPERVISOR_CONTRACT_VERSION,
    }
    for key, expected in fixed.items():
        if authority.get(key) != expected or type(authority.get(key)) is not type(
            expected
        ):
            _fail("AUTHORITY_MISMATCH", f"analysis authority {key} differs")
    run_id = authority.get("run_id")
    if type(run_id) is not str or _RUN_ID_RE.fullmatch(run_id) is None:
        _fail("AUTHORITY_MISMATCH", "analysis authority run_id is invalid")
    if authority.get("capture_file") != f"live-{run_id}.jsonl":
        _fail("AUTHORITY_MISMATCH", "analysis authority capture basename differs")
    file_id = authority.get("capture_file_id")
    if type(file_id) is not str or _FILE_ID_RE.fullmatch(file_id) is None:
        _fail("AUTHORITY_MISMATCH", "analysis authority file id is invalid")
    for key in (
        "install_manifest_sha256",
        "preflight_production_semantic_digest",
        "preflight_receipt_sha256",
        "project_wheel_sha256",
        "runtime_manifest_self_sha256",
        "runtime_manifest_sha256",
        "runtime_tree_sha256",
        "security_tree_sha256",
        "capture_sha256",
    ):
        _sha256(authority.get(key), f"analysis authority {key}")
    for key, minimum in (
        ("capture_byte_size", 1),
        ("capture_volume_serial_number", 0),
        ("security_tree_object_count", 1),
        ("sim_process_id", 1),
        ("sim_start_time_utc_ticks", 1),
        ("windows_session_id", 1),
    ):
        _plain_int(authority.get(key), f"analysis authority {key}", minimum=minimum)
    if int(authority["capture_volume_serial_number"]) > 0xFFFFFFFF:
        _fail("AUTHORITY_MISMATCH", "capture volume serial exceeds uint32")
    token = _mapping(
        authority.get("runtime_token_admission"),
        "runtime token admission",
    )
    if token != {
        "administrators_sid_enabled": False,
        "contract_version": RUNTIME_TOKEN_ADMISSION_CONTRACT_VERSION,
        "current_user_sid": FIXED_RUNTIME_USER_SID,
        "integrity_level_rid": 8192,
        "least_privilege": "PASS",
        "token_elevation_type": "LIMITED",
        "token_is_elevated": False,
    }:
        _fail("AUTHORITY_MISMATCH", "runtime token admission differs")
    stored = _sha256(authority.get("authority_sha256"), "authority SHA-256")
    material = {
        key: item for key, item in authority.items() if key != "authority_sha256"
    }
    if canonical_sha256(material) != stored:
        _fail("AUTHORITY_MISMATCH", "analysis authority self hash differs")
    return authority


def _require_raw_snapshot(handle: object, *, writable: bool) -> io.FileIO:
    if type(handle) is not io.FileIO:
        _fail(
            "CAPTURE_HANDLE_INVALID",
            "capture/output handle must be an exact unbuffered io.FileIO",
        )
    raw = handle
    if raw.closed or not raw.readable() or not raw.seekable():
        _fail("CAPTURE_HANDLE_INVALID", "handle must be open, readable, and seekable")
    if writable and not raw.writable():
        _fail("OUTPUT_HANDLE_INVALID", "output handle must be writable")
    return raw


def _descriptor_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _native_handle_numbers(handle: io.FileIO) -> tuple[int, int, int]:
    metadata = os.fstat(handle.fileno())
    if os.name != "nt":
        inode = int(metadata.st_ino)
        return int(metadata.st_dev) & 0xFFFFFFFF, inode >> 32, inode & 0xFFFFFFFF

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("attributes", wintypes.DWORD),
            ("creation_time", FileTime),
            ("last_access_time", FileTime),
            ("last_write_time", FileTime),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )

    query = ctypes.windll.kernel32.GetFileInformationByHandle
    query.argtypes = (wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation))
    query.restype = wintypes.BOOL
    information = ByHandleFileInformation()
    raw_handle = msvcrt.get_osfhandle(handle.fileno())
    if not query(raw_handle, ctypes.byref(information)):
        _fail("CAPTURE_HANDLE_INVALID", "GetFileInformationByHandle failed")
    if information.attributes & 0x400:
        _fail("CAPTURE_HANDLE_INVALID", "capture handle is a reparse point")
    return (
        int(information.volume_serial_number),
        int(information.file_index_high),
        int(information.file_index_low),
    )


def _hash_raw_handle(handle: io.FileIO) -> tuple[tuple[int, int, int, int, int, int], str]:
    descriptor = handle.fileno()
    try:
        before = os.fstat(descriptor)
        original_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    except OSError as exc:
        raise LiveEngineerSessionError(
            "CAPTURE_HANDLE_INVALID", "cannot inspect capture descriptor"
        ) from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size < 1:
        _fail(
            "CAPTURE_HANDLE_INVALID",
            "capture must be one nonempty singly-linked regular file",
        )
    digest = hashlib.sha256()
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, 1_048_576)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise LiveEngineerSessionError(
            "CAPTURE_HANDLE_INVALID", "cannot hash capture descriptor"
        ) from exc
    finally:
        with suppress(OSError):
            os.lseek(descriptor, original_offset, os.SEEK_SET)
    before_identity = _descriptor_identity(before)
    if _descriptor_identity(after) != before_identity:
        _fail("CAPTURE_CHANGED", "capture metadata changed during full hash")
    return before_identity, digest.hexdigest()


@contextmanager
def _verified_capture_handle(
    capture_handle: object, authority: Mapping[str, object]
) -> Iterator[io.FileIO]:
    handle = _require_raw_snapshot(capture_handle, writable=False)
    before_identity, before_sha = _hash_raw_handle(handle)
    volume, high, low = _native_handle_numbers(handle)
    native_file_id = f"{high:08x}:{low:08x}"
    if (
        before_identity[4] != authority["capture_byte_size"]
        or before_sha != authority["capture_sha256"]
        or volume != authority["capture_volume_serial_number"]
        or native_file_id != authority["capture_file_id"]
    ):
        _fail(
            "CAPTURE_AUTHORITY_MISMATCH",
            "held capture descriptor differs from analysis authority",
        )
    primary: BaseException | None = None
    try:
        yield handle
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            after_identity, after_sha = _hash_raw_handle(handle)
            after_volume, after_high, after_low = _native_handle_numbers(handle)
            if (
                after_identity != before_identity
                or after_sha != before_sha
                or after_volume != volume
                or f"{after_high:08x}:{after_low:08x}" != native_file_id
            ):
                _fail("CAPTURE_CHANGED", "capture changed across live analysis")
        except BaseException:
            if primary is None:
                raise


def _retrieved_descriptor_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    """Identity used for a retrieved cross-host snapshot.

    Windows volume/file-id values remain part of the remote authority.  They
    cannot also identify the newly created Mac file, so the receiving host
    independently seals its local descriptor identity for the whole replay.
    """

    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _hash_retrieved_raw_handle(
    handle: io.FileIO,
) -> tuple[tuple[int, int, int, int, int, int, int], str]:
    descriptor = handle.fileno()
    try:
        before = os.fstat(descriptor)
        original_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    except OSError as exc:
        raise LiveEngineerSessionError(
            "RETRIEVED_CAPTURE_INVALID",
            "cannot inspect retrieved capture descriptor",
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
        or int(getattr(before, "st_file_attributes", 0)) & 0x400
    ):
        _fail(
            "RETRIEVED_CAPTURE_INVALID",
            "retrieved capture must be one nonempty singly-linked regular file",
        )
    digest = hashlib.sha256()
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, 1_048_576)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise LiveEngineerSessionError(
            "RETRIEVED_CAPTURE_INVALID",
            "cannot hash retrieved capture descriptor",
        ) from exc
    finally:
        with suppress(OSError):
            os.lseek(descriptor, original_offset, os.SEEK_SET)
    identity = _retrieved_descriptor_identity(before)
    if _retrieved_descriptor_identity(after) != identity:
        _fail(
            "RETRIEVED_CAPTURE_CHANGED",
            "retrieved capture metadata changed during full hash",
        )
    return identity, digest.hexdigest()


@contextmanager
def _verified_retrieved_capture_handle(
    capture_handle: object,
    *,
    expected_remote_capture_sha256: str,
    expected_remote_capture_byte_size: int,
) -> Iterator[io.FileIO]:
    """Seal a received file without conflating local and remote file IDs."""

    handle = _require_raw_snapshot(capture_handle, writable=False)
    expected_sha = _sha256(
        expected_remote_capture_sha256,
        "expected remote capture SHA-256",
    )
    expected_size = _plain_int(
        expected_remote_capture_byte_size,
        "expected remote capture byte size",
        minimum=1,
    )
    before_identity, before_sha = _hash_retrieved_raw_handle(handle)
    if before_identity[4] != expected_size or before_sha != expected_sha:
        _fail(
            "RETRIEVED_CAPTURE_MISMATCH",
            "retrieved capture bytes differ from remote authority",
        )
    primary: BaseException | None = None
    try:
        yield handle
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            after_identity, after_sha = _hash_retrieved_raw_handle(handle)
            if after_identity != before_identity or after_sha != before_sha:
                _fail(
                    "RETRIEVED_CAPTURE_CHANGED",
                    "retrieved capture changed across cross-host replay",
                )
        except BaseException:
            if primary is None:
                raise


def _required_field(
    field: TelemetryField[object],
    name: str,
    expected_type: type[object],
    *,
    provenance: Provenance,
) -> object:
    if (
        type(field) is not TelemetryField
        or field.presence is not Presence.PRESENT
        or field.provenance is not provenance
        or type(field.value) is not expected_type
    ):
        _fail("LIVE_SOURCE_INVALID", f"{name} is not an exact present field")
    return field.value


def _required_number(
    field: TelemetryField[object],
    name: str,
    *,
    minimum: float = 0.0,
) -> float:
    if (
        type(field) is not TelemetryField
        or field.presence is not Presence.PRESENT
        or field.provenance is not Provenance.SDK_DIRECT
    ):
        _fail("LIVE_SOURCE_INVALID", f"{name} is not SDK-direct")
    return float(_finite_number(field.value, name, minimum=minimum))


def _observed_field(
    field: TelemetryField[object],
    name: str,
    expected_type: type[object] | tuple[type[object], ...],
    *,
    minimum: float | None = None,
    provenance: Provenance = Provenance.SDK_DIRECT,
) -> dict[str, object]:
    if type(field) is not TelemetryField:
        _fail("LIVE_SOURCE_INVALID", f"{name} is not a telemetry field")
    source_fields = list(field.source_fields)
    if field.presence is Presence.PRESENT:
        value = field.value
        expected = expected_type if isinstance(expected_type, tuple) else (expected_type,)
        if (
            field.provenance is not provenance
            or isinstance(value, bool) != (bool in expected)
            or not isinstance(value, expected)
        ):
            _fail("LIVE_SOURCE_INVALID", f"{name} is not exact SDK-direct evidence")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            _finite_number(value, name, minimum=minimum)
        return {
            "availability": "AVAILABLE",
            "presence": field.presence.value,
            "provenance": field.provenance.value,
            "source_fields": source_fields,
            "value": value,
        }
    if field.presence is Presence.MISSING:
        if field.provenance is not Provenance.UNKNOWN or field.value is not None:
            _fail("LIVE_SOURCE_INVALID", f"{name} missing field provenance differs")
        availability = "UNAVAILABLE"
    elif field.presence is Presence.INVALID:
        if field.value is not None:
            _fail("LIVE_SOURCE_INVALID", f"{name} invalid field carries a value")
        availability = "INVALID"
    else:  # pragma: no cover - enum exhaustiveness
        _fail("LIVE_SOURCE_INVALID", f"{name} presence differs")
    return {
        "availability": availability,
        "presence": field.presence.value,
        "provenance": field.provenance.value,
        "source_fields": source_fields,
        "value": None,
    }


def _consume_live_baseline(
    run: ValidatedCollectorRun,
) -> tuple[dict[str, object], TelemetrySample]:
    if type(run) is not ValidatedCollectorRun:
        _fail("LIVE_SOURCE_INVALID", "run must be an active ValidatedCollectorRun")
    try:
        evidence = run.evidence
        # Accessing track_context is an explicit active-adapter token check.
        _ = run.track_context
    except Exception as exc:
        raise LiveEngineerSessionError(
            "LIVE_SOURCE_INVALID", "collector run is not active"
        ) from exc
    evidence_value = evidence.to_dict()
    if (
        evidence.source_kind is not SourceKind.SDK_LIVE
        or evidence.sim_mode != "full"
        or evidence.completion_status != "COMPLETE"
        or evidence.authenticity_status != ATTESTATION_STATUS
        or evidence.frame_record_count < 1
        or evidence.samples_seen
        != evidence.frame_record_count + evidence.duplicate_sample_count
        or evidence.event_record_count != evidence.duplicate_sample_count
        or evidence.duplicate_conflict_count != 0
        or evidence.dropped_tick_count != 0
        or evidence.stale_event_count != 0
        or evidence.session_reset_count != 0
        or evidence.schema_change_count != 0
        or evidence.schema_epoch_count != 1
        or evidence.session_epoch_count != 1
        or evidence.capture_clock_regression_count != 0
        or evidence.read_error_frame_count != 0
        or evidence.read_error_field_count != 0
    ):
        _fail(
            "LIVE_SOURCE_NOT_CLEAN",
            "live wrapper requires one complete clean SDK-live epoch",
        )
    if len(evidence.tick_rate_hz_values) != 1:
        _fail(
            "LIVE_SOURCE_NOT_CLEAN",
            "live wrapper requires one declared SDK tick rate",
        )
    if evidence.frame_record_count >= 2:
        if evidence.capture_span_us is None or evidence.capture_span_us <= 0:
            _fail(
                "LIVE_SOURCE_NOT_CLEAN",
                "live frame-rate admission lacks a monotonic capture span",
            )
        observed_rate = (
            (evidence.frame_record_count - 1)
            * 1_000_000
            / evidence.capture_span_us
        )
        if observed_rate < 0.9 * evidence.tick_rate_hz_values[0]:
            _fail(
                "LIVE_SOURCE_NOT_CLEAN",
                "observed unique-frame rate is below 90% of the declared rate",
            )

    last: TelemetrySample | None = None
    count = 0
    try:
        for sample in run.samples:
            count += 1
            source_id = _required_field(
                sample.source.source_id,
                "source_id",
                str,
                provenance=Provenance.USER_RULE,
            )
            source_kind = _required_field(
                sample.source.source_kind,
                "source_kind",
                SourceKind,
                provenance=Provenance.USER_RULE,
            )
            session_id = _required_field(
                sample.session.session_id,
                "session_id",
                str,
                provenance=Provenance.USER_RULE,
            )
            status = _required_field(
                sample.quality.status,
                "quality status",
                QualityStatus,
                provenance=Provenance.DERIVED,
            )
            issues = _required_field(
                sample.quality.issues,
                "quality issues",
                tuple,
                provenance=Provenance.DERIVED,
            )
            if count == 1:
                first_boundary_is_clean = (
                    sample.quality.stale.presence is Presence.MISSING
                    and sample.quality.stale.provenance is Provenance.UNKNOWN
                    and sample.quality.dropped_ticks.presence is Presence.MISSING
                    and sample.quality.dropped_ticks.provenance
                    is Provenance.UNKNOWN
                    and status is QualityStatus.DEGRADED
                    and issues == ("STALE_UNASSESSED",)
                )
                stale_is_clean = first_boundary_is_clean
                dropped_is_clean = first_boundary_is_clean
                status_is_clean = first_boundary_is_clean
            else:
                stale_is_clean = (
                    _required_field(
                        sample.quality.stale,
                        "quality stale",
                        bool,
                        provenance=Provenance.DERIVED,
                    )
                    is False
                )
                dropped_is_clean = (
                    _required_field(
                        sample.quality.dropped_ticks,
                        "quality dropped_ticks",
                        int,
                        provenance=Provenance.DERIVED,
                    )
                    == 0
                )
                status_is_clean = (
                    status is QualityStatus.READY and issues == ()
                )
            if (
                source_id != evidence.source_id
                or source_kind is not SourceKind.SDK_LIVE
                or session_id != evidence.session_id
                or not stale_is_clean
                or not dropped_is_clean
                or not status_is_clean
            ):
                _fail(
                    "LIVE_SOURCE_NOT_CLEAN",
                    "normalized live sample does not close to clean evidence",
                )
            last = sample
    except LiveEngineerSessionError:
        raise
    except Exception as exc:
        raise LiveEngineerSessionError(
            "LIVE_SOURCE_INVALID", "live sample iteration failed"
        ) from exc
    if last is None or count != evidence.frame_record_count:
        _fail("LIVE_SOURCE_INVALID", "live sample count does not close to evidence")
    return evidence_value, last


def _observed_live_evidence(
    evidence: Mapping[str, object],
    sample: TelemetrySample,
) -> dict[str, object]:
    decision_tick = _required_field(
        sample.session.session_tick,
        "SessionTick",
        int,
        provenance=Provenance.SDK_DIRECT,
    )
    buffer_tick = _required_field(
        sample.session.sdk_buffer_tick,
        "SDK buffer tick",
        int,
        provenance=Provenance.SDK_DIRECT,
    )
    session_time_s = _required_number(
        sample.session.session_time_s, "SessionTime", minimum=0.0
    )
    laps_completed = _required_field(
        sample.lap.laps_completed,
        "LapCompleted",
        int,
        provenance=Provenance.SDK_DIRECT,
    )
    if int(laps_completed) < 0:
        _fail("LIVE_SOURCE_INVALID", "LapCompleted cannot be negative")

    current_fuel = _observed_field(
        sample.fuel.level_l, "FuelLevel", (int, float), minimum=0.0
    )
    pits_open = _observed_field(sample.pit.pits_open, "PitsOpen", bool)
    laps_remaining = _observed_field(
        sample.session.session_laps_remaining,
        "SessionLapsRemainEx",
        int,
    )
    time_remaining = _observed_field(
        sample.session.session_time_remaining_s,
        "SessionTimeRemain",
        (int, float),
    )
    if (
        laps_remaining["availability"] == "AVAILABLE"
        and (
            type(laps_remaining["value"]) is not int
            or int(laps_remaining["value"]) < 0
            or int(laps_remaining["value"]) >= 32767
        )
    ):
        laps_remaining["availability"] = "UNAVAILABLE_SENTINEL"
    if (
        time_remaining["availability"] == "AVAILABLE"
        and float(time_remaining["value"]) < 0.0
    ):
        time_remaining["availability"] = "UNAVAILABLE_SENTINEL"

    source = {
        "records_sha256": evidence["records_sha256"],
        "session_id": evidence["session_id"],
        "source_id": evidence["source_id"],
        "source_kind": evidence["source_kind"],
    }
    material: dict[str, object] = {
        "contract_version": OBSERVED_LIVE_EVIDENCE_CONTRACT_VERSION,
        "current_fuel": current_fuel,
        "decision_clock": {
            "captured_monotonic": _observed_field(
                sample.session.captured_monotonic_s,
                "captured monotonic time",
                (int, float),
                minimum=0.0,
                provenance=Provenance.DERIVED,
            ),
            "decision_tick": decision_tick,
            "sdk_buffer_tick": buffer_tick,
            "session_time_s": session_time_s,
            "session_time_us": int(round(session_time_s * 1_000_000)),
        },
        "horizon": {
            "laps_remaining": laps_remaining,
            "time_remaining_s": time_remaining,
        },
        "input_evidence_sha256": canonical_sha256(evidence),
        "laps_completed": laps_completed,
        "pits_open": pits_open,
        "source_binding": source,
    }
    return {
        **material,
        "observed_live_evidence_sha256": canonical_sha256(material),
    }


def _context_builder(
    observed: Mapping[str, object],
) -> object:
    def build(lineage_value: Mapping[str, object]) -> Mapping[str, object]:
        lineage = _mapping(dict(lineage_value), "input lineage")
        source = _mapping(observed["source_binding"], "observed source binding")
        expected = {
            "records_sha256": lineage["source_content_sha256"],
            "session_id": lineage["session_id"],
            "source_id": lineage["source_id"],
            "source_kind": lineage["source_kind"],
        }
        if source != expected:
            _fail(
                "SOURCE_CLOSURE_MISMATCH",
                "observed run and fresh core admissions differ",
            )
        horizon = _mapping(observed["horizon"], "observed horizon")
        laps = _mapping(horizon["laps_remaining"], "observed laps horizon")
        timed = _mapping(horizon["time_remaining_s"], "observed timed horizon")
        laps_value = (
            laps["value"] if laps["availability"] == "AVAILABLE" else None
        )
        time_value = (
            timed["value"] if timed["availability"] == "AVAILABLE" else None
        )
        decision = _mapping(observed["decision_clock"], "observed decision clock")
        pits = _mapping(observed["pits_open"], "observed pits_open")
        material: dict[str, object] = {
            "calibration_model": None,
            "contract_version": "offline-m2-strategy-context-v1",
            "event_identity": {
                "car_class_id": None,
                "event_type": None,
                "official": None,
                "provenance": "SDK_DIRECT_SAME_SOURCE_SESSION_INFO",
                "race_week": None,
                "season_id": None,
                "series_id": None,
                "sim_build": None,
                "track_config": None,
                "track_id": None,
            },
            "horizon": {
                "kind": "LAPS" if laps_value is not None else "TIMED",
                "laps_remaining": laps_value,
                "leader_eta_to_next_crossing_s": None,
                "player_is_leader": None,
                # The existing M2 schema has no UNKNOWN provenance enum.  Null
                # values keep the capability WAIT; the top observed receipt
                # retains exact MISSING/INVALID provenance without promotion.
                "provenance": "SDK_DIRECT",
                "reference_lap_time_s": None,
                "time_remaining_s": time_value,
            },
            "observation": {
                "decision_tick": decision["decision_tick"],
                "laps_completed": observed["laps_completed"],
                "penalty_state": None,
                "pits_open": (
                    pits["value"] if pits["availability"] == "AVAILABLE" else None
                ),
                "reset": False,
                "schema_changed": False,
                "session_epoch": 1,
                "source_epoch": 1,
                "stale": False,
            },
            "source_binding": {
                "event_receipt_sha256": lineage["event_receipt_sha256"],
                "normalized_samples_sha256": lineage[
                    "normalized_samples_sha256"
                ],
                "sample_count": lineage["sample_count"],
                "session_id": lineage["session_id"],
                "source_id": lineage["source_id"],
                "source_kind": lineage["source_kind"],
                "source_sha256": lineage["source_content_sha256"],
            },
            "strategy_policy": {
                "conservative_quantile": 0.9,
                "reserve_l": 1.0,
                "selection_policy": "LATEST_COMMON_FUEL_FEASIBLE",
            },
            "traffic_rejoin": None,
            "vehicle_context": {
                "provenance": "USER_RULE",
                "tank_capacity_l": 120.0,
            },
        }
        return {**material, "context_sha256": canonical_sha256(material)}

    return build


def _validate_observed(value: object) -> dict[str, object]:
    observed = _exact(
        _json_copy(value, "observed live evidence"),
        _OBSERVED_KEYS,
        "observed live evidence",
    )
    if observed.get("contract_version") != OBSERVED_LIVE_EVIDENCE_CONTRACT_VERSION:
        _fail("OBSERVED_EVIDENCE_INVALID", "observed evidence contract differs")
    stored = _sha256(
        observed.get("observed_live_evidence_sha256"),
        "observed live evidence SHA-256",
    )
    material = {
        key: item
        for key, item in observed.items()
        if key != "observed_live_evidence_sha256"
    }
    if canonical_sha256(material) != stored:
        _fail("OBSERVED_EVIDENCE_INVALID", "observed evidence self hash differs")
    _sha256(observed.get("input_evidence_sha256"), "observed input evidence SHA-256")
    source = _exact(
        observed.get("source_binding"),
        _SOURCE_BINDING_KEYS,
        "observed source binding",
    )
    for key in ("records_sha256",):
        _sha256(source.get(key), f"observed source {key}")
    if source.get("source_kind") != SourceKind.SDK_LIVE.value:
        _fail("OBSERVED_EVIDENCE_INVALID", "observed source is not SDK_LIVE")
    for key in ("source_id", "session_id"):
        if type(source.get(key)) is not str or not source[key]:
            _fail("OBSERVED_EVIDENCE_INVALID", f"observed {key} is invalid")
    clock = _mapping(observed.get("decision_clock"), "observed decision clock")
    if set(clock) != {
        "captured_monotonic",
        "decision_tick",
        "sdk_buffer_tick",
        "session_time_s",
        "session_time_us",
    }:
        _fail("OBSERVED_EVIDENCE_INVALID", "observed clock keys are invalid")
    _plain_int(clock.get("decision_tick"), "observed decision tick")
    _plain_int(clock.get("sdk_buffer_tick"), "observed buffer tick")
    session_time = _finite_number(
        clock.get("session_time_s"), "observed SessionTime", minimum=0.0
    )
    session_time_us = _plain_int(
        clock.get("session_time_us"), "observed SessionTime microseconds"
    )
    if session_time_us != int(round(float(session_time) * 1_000_000)):
        _fail("OBSERVED_EVIDENCE_INVALID", "observed clock conversion differs")
    laps_completed = observed.get("laps_completed")
    if type(laps_completed) is not int or not 0 <= laps_completed < 32767:
        _fail(
            "OBSERVED_EVIDENCE_INVALID",
            "observed laps completed must be an integer from 0 through 32766",
        )
    for name in ("current_fuel", "pits_open"):
        _mapping(observed.get(name), f"observed {name}")
    horizon = _mapping(observed.get("horizon"), "observed horizon")
    if set(horizon) != {"laps_remaining", "time_remaining_s"}:
        _fail("OBSERVED_EVIDENCE_INVALID", "observed horizon keys are invalid")
    observed_fields = (
        (
            "captured_monotonic",
            clock["captured_monotonic"],
            Provenance.DERIVED.value,
            "captured_monotonic_s",
        ),
        (
            "current_fuel",
            observed["current_fuel"],
            Provenance.SDK_DIRECT.value,
            "FuelLevel",
        ),
        (
            "pits_open",
            observed["pits_open"],
            Provenance.SDK_DIRECT.value,
            "PitsOpen",
        ),
        (
            "laps_remaining",
            horizon["laps_remaining"],
            Provenance.SDK_DIRECT.value,
            "SessionLapsRemainEx",
        ),
        (
            "time_remaining_s",
            horizon["time_remaining_s"],
            Provenance.SDK_DIRECT.value,
            "SessionTimeRemain",
        ),
    )
    for name, item, expected_provenance, expected_source_field in observed_fields:
        field = _mapping(item, f"observed {name}")
        if set(field) != {
            "availability",
            "presence",
            "provenance",
            "source_fields",
            "value",
        }:
            _fail("OBSERVED_EVIDENCE_INVALID", "observed field keys are invalid")
        if field["presence"] == Presence.MISSING.value:
            expected_source_fields: list[str] = []
        else:
            expected_source_fields = [expected_source_field]
        if field["source_fields"] != expected_source_fields:
            _fail(
                "OBSERVED_EVIDENCE_INVALID",
                f"observed {name} source fields differ",
            )
        if field["availability"] == "AVAILABLE":
            if (
                field["presence"] != Presence.PRESENT.value
                or field["provenance"] != expected_provenance
                or field["value"] is None
            ):
                _fail(
                    "OBSERVED_EVIDENCE_INVALID",
                    "available observed field metadata differs",
                )
            available_value = field["value"]
            if name in {"captured_monotonic", "current_fuel", "time_remaining_s"}:
                if (
                    isinstance(available_value, bool)
                    or not isinstance(available_value, (int, float))
                    or not math.isfinite(float(available_value))
                    or float(available_value) < 0.0
                ):
                    _fail(
                        "OBSERVED_EVIDENCE_INVALID",
                        f"available observed {name} is not finite and non-negative",
                    )
            elif name == "pits_open":
                if type(available_value) is not bool:
                    _fail(
                        "OBSERVED_EVIDENCE_INVALID",
                        "available observed pits_open is not a boolean",
                    )
            elif name == "laps_remaining" and (
                type(available_value) is not int
                or not 0 <= available_value < 32767
            ):
                _fail(
                    "OBSERVED_EVIDENCE_INVALID",
                    "available observed laps_remaining is outside 0 through 32766",
                )
        elif field["availability"] == "UNAVAILABLE":
            if (
                field["presence"] != Presence.MISSING.value
                or field["provenance"] != Provenance.UNKNOWN.value
                or field["value"] is not None
            ):
                _fail(
                    "OBSERVED_EVIDENCE_INVALID",
                    "missing observed field metadata differs",
                )
        elif field["availability"] == "INVALID":
            if (
                field["presence"] != Presence.INVALID.value
                or field["provenance"] != expected_provenance
                or field["value"] is not None
            ):
                _fail(
                    "OBSERVED_EVIDENCE_INVALID",
                    "invalid observed field metadata differs",
                )
        elif field["availability"] == "UNAVAILABLE_SENTINEL":
            if (
                field["presence"] != Presence.PRESENT.value
                or field["provenance"] != expected_provenance
                or field["value"] is None
            ):
                _fail(
                    "OBSERVED_EVIDENCE_INVALID",
                    "sentinel observed field metadata differs",
                )
        else:
            _fail("OBSERVED_EVIDENCE_INVALID", "observed availability is invalid")
    for item in (
        clock["captured_monotonic"],
        observed["current_fuel"],
        observed["pits_open"],
    ):
        if _mapping(item, "non-sentinel observed field").get("availability") == (
            "UNAVAILABLE_SENTINEL"
        ):
            _fail(
                "OBSERVED_EVIDENCE_INVALID",
                "non-horizon field cannot carry an unavailable sentinel",
            )
    laps_field = _mapping(horizon["laps_remaining"], "laps horizon field")
    if laps_field["availability"] == "UNAVAILABLE_SENTINEL":
        value = laps_field["value"]
        if type(value) is not int or not (value < 0 or value >= 32767):
            _fail("OBSERVED_EVIDENCE_INVALID", "laps sentinel is invalid")
    timed_field = _mapping(horizon["time_remaining_s"], "time horizon field")
    if timed_field["availability"] == "UNAVAILABLE_SENTINEL":
        value = timed_field["value"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) >= 0.0
        ):
            _fail("OBSERVED_EVIDENCE_INVALID", "time sentinel is invalid")
    return observed


def _components(core: Mapping[str, object]) -> tuple[
    dict[str, object], dict[str, object], dict[str, object]
]:
    components = _mapping(core.get("components"), "engineer-session components")
    m2 = _mapping(components.get("m2_strategy"), "M2 component")
    timeline = _mapping(components.get("advisor_timeline"), "advisor timeline")
    clock = _mapping(timeline.get("clock_receipt"), "advisor clock receipt")
    return m2, timeline, clock


def _recursive_true_count(value: object, key_name: str) -> int:
    if type(value) is dict:
        return sum(
            int(key == key_name and item is True)
            + _recursive_true_count(item, key_name)
            for key, item in value.items()
        )
    if type(value) is list:
        return sum(_recursive_true_count(item, key_name) for item in value)
    return 0


def _build_pipeline_proof(core: Mapping[str, object]) -> dict[str, object]:
    lineage = _mapping(core.get("input_lineage"), "engineer-session input lineage")
    m2, timeline, clock = _components(core)
    context = _mapping(m2.get("strategy_context"), "M2 strategy context")
    admission = _mapping(core.get("admission_receipt"), "admission receipt")
    bindings = clock.get("bindings")
    if type(bindings) is not list or len(bindings) != 1:
        _fail("CLOCK_CLOSURE_MISMATCH", "advisor clock needs one decision binding")
    binding = _mapping(bindings[0], "advisor clock decision binding")
    capabilities = _mapping(m2.get("capabilities"), "M2 capabilities")
    statuses = {
        "advisor_timeline": timeline["status"],
        "engineer_session": core["status"],
        "m2_strategy": _mapping(m2["quality_gate"], "M2 quality gate")[
            "status"
        ],
        "official_rules": _mapping(
            capabilities["event_rules_identity"], "M2 rules capability"
        )["status"],
        "pit_loss_calibration": _mapping(
            capabilities["pit_loss_calibration"], "M2 calibration capability"
        )["status"],
        "race_recommendation": _mapping(
            capabilities["race_recommendation"], "M2 race capability"
        )["status"],
        "service_labels": _mapping(
            capabilities["service_labels"], "M2 service capability"
        )["status"],
        "traffic": _mapping(
            capabilities["traffic_data"], "M2 traffic capability"
        )["status"],
    }
    material: dict[str, object] = {
        "admission_receipt_sha256": admission["admission_receipt_sha256"],
        "advisor_clock_receipt_sha256": clock["clock_receipt_sha256"],
        "component_hashes": _json_copy(
            core["component_hashes"], "component hash table"
        ),
        "component_statuses": statuses,
        "contract_version": "live-safe-pipeline-proof-v1",
        "decision_clock": {
            "decision_tick": binding["decision_tick"],
            "session_time_us": binding["session_time_us"],
        },
        "engineer_session_sha256": core["engineer_session_sha256"],
        "fresh_admission_count": admission["fresh_admission_count"],
        "input_lineage": _json_copy(lineage, "input lineage"),
        "internal_core_persisted": False,
        "projection_role": "SAFE_PIPELINE_PROOF_NO_TACTICAL_CONTENT",
        "semantic_hashes": _json_copy(
            core["semantic_hashes"], "semantic hash table"
        ),
        "strategy_context_sha256": context["context_sha256"],
    }
    return {**material, "pipeline_proof_sha256": canonical_sha256(material)}


def _validate_pipeline_proof(value: object) -> dict[str, object]:
    proof = _exact(
        _json_copy(value, "safe pipeline proof"),
        _PIPELINE_PROOF_KEYS,
        "safe pipeline proof",
    )
    if (
        proof.get("contract_version") != "live-safe-pipeline-proof-v1"
        or proof.get("internal_core_persisted") is not False
        or proof.get("projection_role")
        != "SAFE_PIPELINE_PROOF_NO_TACTICAL_CONTENT"
        or proof.get("fresh_admission_count") != 4
    ):
        _fail("PIPELINE_PROOF_INVALID", "safe pipeline proof boundary differs")
    stored = _sha256(
        proof.get("pipeline_proof_sha256"), "safe pipeline proof SHA-256"
    )
    if canonical_sha256(
        {
            key: item
            for key, item in proof.items()
            if key != "pipeline_proof_sha256"
        }
    ) != stored:
        _fail("PIPELINE_PROOF_INVALID", "safe pipeline proof self hash differs")
    for key in (
        "admission_receipt_sha256",
        "advisor_clock_receipt_sha256",
        "engineer_session_sha256",
        "strategy_context_sha256",
    ):
        _sha256(proof.get(key), f"safe pipeline proof {key}")
    component_hashes = _mapping(
        proof.get("component_hashes"), "safe component hashes"
    )
    if set(component_hashes) != {
        "advisor_timeline",
        "corner_cards",
        "driving_diagnosis",
        "driving_replay",
        "fuel_replay",
        "m1_pit_stint",
        "m2_strategy",
    }:
        _fail("PIPELINE_PROOF_INVALID", "safe component hash keys differ")
    for key, digest in component_hashes.items():
        _sha256(digest, f"safe component hash {key}")
    semantic_hashes = _mapping(
        proof.get("semantic_hashes"), "safe semantic hashes"
    )
    if set(semantic_hashes) != {
        "driving_model_semantic_sha256",
        "fuel_model_semantic_sha256",
        "m1_pit_stint_semantic_sha256",
        "source_neutral_sha256",
    }:
        _fail("PIPELINE_PROOF_INVALID", "safe semantic hash keys differ")
    for key, digest in semantic_hashes.items():
        _sha256(digest, f"safe semantic hash {key}")
    statuses = _mapping(proof.get("component_statuses"), "component statuses")
    if statuses != {
        "advisor_timeline": "WAIT_DATA",
        "engineer_session": "WAIT_DATA",
        "m2_strategy": "WAIT_CAPABILITIES",
        "official_rules": "WAIT_EVENT_RULES_IDENTITY",
        "pit_loss_calibration": "WAIT_MATCHED_PIT_LOSS_BASELINE",
        "race_recommendation": "BLOCKED",
        "service_labels": "WAIT_SERVICE_LABELS",
        "traffic": "WAIT_TRAFFIC_DATA",
    }:
        _fail("PIPELINE_PROOF_INVALID", "component WAIT statuses differ")
    lineage = _exact(
        proof.get("input_lineage"),
        _INPUT_LINEAGE_KEYS,
        "safe input lineage",
    )
    if (
        lineage.get("input_kind") != "collector"
        or lineage.get("source_kind") != SourceKind.SDK_LIVE.value
    ):
        _fail("PIPELINE_PROOF_INVALID", "safe input lineage kind differs")
    for key in (
        "event_receipt_sha256",
        "input_evidence_sha256",
        "input_lineage_sha256",
        "normalized_samples_sha256",
        "source_content_sha256",
    ):
        _sha256(lineage.get(key), f"safe input lineage {key}")
    lineage_material = {
        key: item
        for key, item in lineage.items()
        if key != "input_lineage_sha256"
    }
    if canonical_sha256(lineage_material) != lineage["input_lineage_sha256"]:
        _fail(
            "PIPELINE_PROOF_INVALID",
            "safe input lineage self hash differs",
        )
    _plain_int(lineage.get("sample_count"), "safe input sample count", minimum=1)
    for key in ("source_id", "session_id"):
        if type(lineage.get(key)) is not str or not lineage[key]:
            _fail("PIPELINE_PROOF_INVALID", f"safe input {key} differs")
    decision = _mapping(proof.get("decision_clock"), "safe decision clock")
    if set(decision) != {"decision_tick", "session_time_us"}:
        _fail("PIPELINE_PROOF_INVALID", "safe decision clock keys differ")
    _plain_int(decision.get("decision_tick"), "safe decision tick")
    _plain_int(decision.get("session_time_us"), "safe SessionTime microseconds")
    _assert_safe_persisted_projection(proof)
    return proof


def _walk_key_values(value: object) -> Iterator[tuple[str, object]]:
    if type(value) is dict:
        for key, item in value.items():
            yield key, item
            yield from _walk_key_values(item)
    elif type(value) is list:
        for item in value:
            yield from _walk_key_values(item)


def _assert_safe_persisted_projection(value: object) -> None:
    for key, item in _walk_key_values(value):
        if key in _FORBIDDEN_PERSISTED_KEYS:
            _fail(
                "PIPELINE_PROOF_INVALID",
                f"persisted live projection contains forbidden field: {key}",
            )
        if key == "recommendations" and item != []:
            _fail(
                "PIPELINE_PROOF_INVALID",
                "persisted live projection contains a recommendation",
            )


def _derive_closure(
    authority: Mapping[str, object],
    observed: Mapping[str, object],
    proof: Mapping[str, object],
) -> dict[str, object]:
    lineage = _mapping(proof.get("input_lineage"), "safe input lineage")
    binding = _mapping(proof.get("decision_clock"), "safe decision clock")
    observed_clock = _mapping(observed.get("decision_clock"), "observed clock")
    source = _mapping(observed.get("source_binding"), "observed source binding")
    expected_source = {
        "records_sha256": lineage["source_content_sha256"],
        "session_id": lineage["session_id"],
        "source_id": lineage["source_id"],
        "source_kind": lineage["source_kind"],
    }
    if source != expected_source:
        _fail(
            "SOURCE_CLOSURE_MISMATCH",
            "observed evidence and engineer-session source differ",
        )
    if (
        lineage.get("input_kind") != "collector"
        or lineage.get("source_kind") != SourceKind.SDK_LIVE.value
        or observed.get("input_evidence_sha256")
        != lineage.get("input_evidence_sha256")
        or binding.get("decision_tick") != observed_clock.get("decision_tick")
        or binding.get("session_time_us") != observed_clock.get("session_time_us")
    ):
        _fail(
            "CLOCK_CLOSURE_MISMATCH",
            "source, evidence, decision tick, or SessionTime does not close",
        )
    closure = {
        "advisor_clock_receipt_sha256": proof[
            "advisor_clock_receipt_sha256"
        ],
        "analysis_authority_sha256": authority["authority_sha256"],
        "capture_byte_size": authority["capture_byte_size"],
        "capture_file_id": authority["capture_file_id"],
        "capture_sha256": authority["capture_sha256"],
        "capture_snapshot_method": authority["capture_snapshot_method"],
        "capture_volume_serial_number": authority["capture_volume_serial_number"],
        "context_sha256": proof["strategy_context_sha256"],
        "decision_session_time_us": binding["session_time_us"],
        "decision_tick": binding["decision_tick"],
        "engineer_session_sha256": proof["engineer_session_sha256"],
        "event_receipt_sha256": lineage["event_receipt_sha256"],
        "input_evidence_sha256": lineage["input_evidence_sha256"],
        "input_lineage_sha256": lineage["input_lineage_sha256"],
        "normalized_samples_sha256": lineage["normalized_samples_sha256"],
        "observed_live_evidence_sha256": observed[
            "observed_live_evidence_sha256"
        ],
        "sample_count": lineage["sample_count"],
        "session_id": lineage["session_id"],
        "source_content_sha256": lineage["source_content_sha256"],
        "source_id": lineage["source_id"],
        "source_kind": lineage["source_kind"],
    }
    if set(closure) != _CLOSURE_KEYS:
        raise AssertionError("internal live closure schema drift")
    return closure


def _assert_wait_only(core: Mapping[str, object]) -> None:
    m2, timeline, _ = _components(core)
    capabilities = _mapping(m2.get("capabilities"), "M2 capabilities")
    expected = {
        "event_rules_identity": "WAIT_EVENT_RULES_IDENTITY",
        "pit_loss_calibration": "WAIT_MATCHED_PIT_LOSS_BASELINE",
        "service_labels": "WAIT_SERVICE_LABELS",
        "traffic_data": "WAIT_TRAFFIC_DATA",
        "race_recommendation": "BLOCKED",
    }
    for key, status in expected.items():
        capability = _mapping(capabilities.get(key), f"M2 capability {key}")
        if capability.get("status") != status:
            _fail(
                "LIVE_CAPABILITY_PROMOTION",
                f"M2 capability {key} escaped its live WAIT boundary",
            )
    rules = _mapping(m2.get("rules_binding"), "M2 rules binding")
    if (
        rules.get("official_event_rules") is not False
        or m2.get("recommendations") != []
        or _mapping(m2.get("quality_gate"), "M2 quality gate").get("status")
        != "WAIT_CAPABILITIES"
    ):
        _fail(
            "LIVE_CAPABILITY_PROMOTION",
            "M2 rules, recommendations, or quality gate were promoted",
        )
    summary = _mapping(timeline.get("summary"), "advisor timeline summary")
    if (
        timeline.get("status") != "WAIT_DATA"
        or summary.get("final_active_tactical_count") != 0
        or summary.get("tactical_observation_count") != 0
        or summary.get("upstream_tactical_candidate_count") != 0
        or _recursive_true_count(core, "executable") != 0
        or _recursive_true_count(core, "audible") != 0
    ):
        _fail(
            "LIVE_CAPABILITY_PROMOTION",
            "advisor timeline crossed the zero-output live boundary",
        )


def _validate_stale_after_s(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        _fail("SCHEMA_INVALID", "stale_after_s must be finite and positive")
    return float(value)


def _build_live_engineer_session_from_verified_handle(
    run: ValidatedCollectorRun,
    handle: io.FileIO,
    *,
    authority: Mapping[str, object],
    stale_after_s: float,
    report_output: list[tuple[dict[str, object], dict[str, object]]] | None = None,
) -> dict[str, object]:
    """Shared builder after the caller has selected an identity boundary."""

    evidence, last_sample = _consume_live_baseline(run)
    observed = _observed_live_evidence(evidence, last_sample)
    try:
        core = build_engineer_session_from_collector_snapshot(
            handle,
            scenario=_DEVELOPMENT_SMOKE_SCENARIO,
            strategy_context_builder=_context_builder(observed),
            expected_snapshot_sha256=str(authority["capture_sha256"]),
            expected_snapshot_byte_size=int(authority["capture_byte_size"]),
            rules_profile=None,
            stale_after_s=stale_after_s,
        )
        core = validate_engineer_session(
            core,
            expected_engineer_session_sha256=str(core["engineer_session_sha256"]),
        )
    except LiveEngineerSessionError:
        raise
    except EngineerSessionError as exc:
        raise LiveEngineerSessionError(
            "CORE_ENGINEER_SESSION_FAILED",
            f"same-handle engineer-session failed: {exc.code}",
        ) from exc
    _assert_wait_only(core)
    if report_output is not None:
        if report_output:
            _fail("REPORT_OUTPUT_INVALID", "live report output container is not empty")
        from .session_report import build_engineer_session_report

        report = build_engineer_session_report(
            core,
            expected_engineer_session_sha256=str(core["engineer_session_sha256"]),
        )
        report_output.append((report, core))
    proof = _validate_pipeline_proof(_build_pipeline_proof(core))
    closure = _derive_closure(authority, observed, proof)
    base: dict[str, object] = {
        "advisor_only": True,
        "analysis_authority": dict(authority),
        "attestation_status": ATTESTATION_STATUS,
        "capability_gates": dict(_CAPABILITY_GATES),
        "closure": closure,
        "contract_version": LIVE_ENGINEER_SESSION_CONTRACT_VERSION,
        "derivation_status": "M0_SAME_PIPELINE_PROOF_ONLY",
        "execution_mode": EXECUTION_MODE,
        "observed_live_evidence": observed,
        "pipeline_proof": proof,
        "recommendations": [],
        "safety": dict(_SAFETY),
        "scenario_boundary": dict(_SCENARIO_BOUNDARY),
        "status": "WAIT_CAPABILITIES",
    }
    receipt = {**base, "live_engineer_session_sha256": canonical_sha256(base)}
    return validate_live_engineer_session(
        receipt,
        expected_live_engineer_session_sha256=str(
            receipt["live_engineer_session_sha256"]
        ),
        expected_capture_sha256=str(authority["capture_sha256"]),
        expected_capture_byte_size=int(authority["capture_byte_size"]),
        expected_analysis_authority_sha256=str(authority["authority_sha256"]),
    )


def build_live_engineer_session(
    run: ValidatedCollectorRun,
    capture_handle: object,
    *,
    analysis_authority: Mapping[str, object],
    stale_after_s: float = 0.5,
) -> dict[str, object]:
    """Build an R8 WAIT-only wrapper from one active run and held capture.

    No scenario, rules, or strategy-context path is accepted.  The active run
    supplies only same-source direct observations.  The core package session
    then performs four fresh admissions from independent duplicates of the
    same caller-owned immutable FileIO descriptor.
    """

    selected_stale = _validate_stale_after_s(stale_after_s)
    authority = _validate_authority(analysis_authority)
    with _verified_capture_handle(capture_handle, authority) as handle:
        return _build_live_engineer_session_from_verified_handle(
            run,
            handle,
            authority=authority,
            stale_after_s=selected_stale,
        )


def validate_live_engineer_session(
    value: object,
    *,
    expected_live_engineer_session_sha256: str | None = None,
    expected_capture_sha256: str | None = None,
    expected_capture_byte_size: int | None = None,
    expected_analysis_authority_sha256: str | None = None,
) -> dict[str, object]:
    """Validate one persisted safe projection without reconstructing its core.

    Full package-core equivalence requires :func:`replay_live_engineer_session`
    with a fresh active run and the same held capture descriptor.
    """

    payload = _exact(
        _json_copy(value, "live engineer session"),
        _LIVE_KEYS,
        "live engineer session",
    )
    if (
        payload.get("advisor_only") is not True
        or payload.get("attestation_status") != ATTESTATION_STATUS
        or payload.get("contract_version") != LIVE_ENGINEER_SESSION_CONTRACT_VERSION
        or payload.get("derivation_status") != "M0_SAME_PIPELINE_PROOF_ONLY"
        or payload.get("execution_mode") != EXECUTION_MODE
        or payload.get("status") != "WAIT_CAPABILITIES"
        or payload.get("recommendations") != []
        or payload.get("capability_gates") != _CAPABILITY_GATES
        or payload.get("scenario_boundary") != _SCENARIO_BOUNDARY
        or payload.get("safety") != _SAFETY
    ):
        _fail(
            "LIVE_REPLAY_MISMATCH",
            "live wrapper safety, scenario, or status boundary differs",
        )
    stored = _sha256(
        payload.get("live_engineer_session_sha256"),
        "live engineer-session SHA-256",
    )
    if (
        expected_live_engineer_session_sha256 is not None
        and stored
        != _sha256(
            expected_live_engineer_session_sha256,
            "expected live engineer-session SHA-256",
        )
    ):
        _fail(
            "LIVE_REPLAY_MISMATCH",
            "live wrapper failed its independent digest binding",
        )
    material = {
        key: item
        for key, item in payload.items()
        if key != "live_engineer_session_sha256"
    }
    if canonical_sha256(material) != stored:
        _fail("LIVE_REPLAY_MISMATCH", "live wrapper self hash differs")

    authority = _validate_authority(payload.get("analysis_authority"))
    if (
        expected_analysis_authority_sha256 is not None
        and authority["authority_sha256"]
        != _sha256(
            expected_analysis_authority_sha256,
            "expected analysis authority SHA-256",
        )
    ):
        _fail("AUTHORITY_MISMATCH", "analysis authority digest differs")
    if (
        expected_capture_sha256 is not None
        and authority["capture_sha256"]
        != _sha256(expected_capture_sha256, "expected capture SHA-256")
    ):
        _fail("CAPTURE_AUTHORITY_MISMATCH", "capture digest differs")
    if (
        expected_capture_byte_size is not None
        and authority["capture_byte_size"]
        != _plain_int(
            expected_capture_byte_size,
            "expected capture byte size",
            minimum=1,
        )
    ):
        _fail("CAPTURE_AUTHORITY_MISMATCH", "capture byte size differs")

    observed = _validate_observed(payload.get("observed_live_evidence"))
    proof = _validate_pipeline_proof(payload.get("pipeline_proof"))
    closure = _exact(payload.get("closure"), _CLOSURE_KEYS, "live closure")
    if closure != _derive_closure(authority, observed, proof):
        _fail("LIVE_REPLAY_MISMATCH", "live source/session/clock closure differs")
    _assert_safe_persisted_projection(payload)
    return payload


def replay_live_engineer_session(
    run: ValidatedCollectorRun,
    capture_handle: object,
    receipt: Mapping[str, object],
    *,
    stale_after_s: float = 0.5,
) -> dict[str, object]:
    """Rebuild from a fresh active run and the same held capture descriptor."""

    expected = validate_live_engineer_session(
        receipt,
        expected_live_engineer_session_sha256=_sha256(
            _mapping(receipt, "live engineer session").get(
                "live_engineer_session_sha256"
            ),
            "live engineer-session SHA-256",
        ),
    )
    authority = _mapping(expected["analysis_authority"], "analysis authority")
    rebuilt = build_live_engineer_session(
        run,
        capture_handle,
        analysis_authority=authority,
        stale_after_s=stale_after_s,
    )
    if rebuilt != expected:
        _fail(
            "LIVE_REPLAY_MISMATCH",
            "same-handle live rebuild is not object-exact",
        )
    return rebuilt


def replay_retrieved_live_engineer_session(
    capture_handle: object,
    receipt: Mapping[str, object],
    *,
    expected_remote_capture_sha256: str,
    expected_remote_capture_byte_size: int,
    stale_after_s: float = 0.5,
) -> dict[str, object]:
    """Rebuild one downloaded live artifact from the same already-open fd.

    The Windows volume serial and file id remain unchanged in ``receipt`` as
    evidence about the producing handle.  A newly downloaded file necessarily
    has a different host-local id, so this receiving-side API binds it by the
    remote SHA/size and independently requires the local regular-file identity
    (device, inode, mode, link count, size, mtime, and ctime) to remain stable
    before, during, and after the object-exact replay.  No pathname is opened.
    """

    selected_stale = _validate_stale_after_s(stale_after_s)
    expected = validate_live_engineer_session(
        receipt,
        expected_live_engineer_session_sha256=_sha256(
            _mapping(receipt, "live engineer session").get(
                "live_engineer_session_sha256"
            ),
            "live engineer-session SHA-256",
        ),
        expected_capture_sha256=expected_remote_capture_sha256,
        expected_capture_byte_size=expected_remote_capture_byte_size,
    )
    authority = _mapping(expected["analysis_authority"], "analysis authority")
    with _verified_retrieved_capture_handle(
        capture_handle,
        expected_remote_capture_sha256=expected_remote_capture_sha256,
        expected_remote_capture_byte_size=expected_remote_capture_byte_size,
    ) as handle:
        descriptor = -1
        try:
            descriptor = os.dup(handle.fileno())
            os.lseek(descriptor, 0, os.SEEK_SET)
        except OSError as exc:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
            raise LiveEngineerSessionError(
                "RETRIEVED_CAPTURE_INVALID",
                "cannot duplicate retrieved capture descriptor",
            ) from exc
        try:
            with os.fdopen(
                descriptor,
                "r",
                encoding="utf-8",
                errors="strict",
                newline="",
            ) as text:
                descriptor = -1
                with open_collector_jsonl_snapshot(
                    text,
                    stale_after_s=selected_stale,
                ) as run:
                    rebuilt = _build_live_engineer_session_from_verified_handle(
                        run,
                        handle,
                        authority=authority,
                        stale_after_s=selected_stale,
                    )
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
    if rebuilt != expected:
        _fail(
            "LIVE_REPLAY_MISMATCH",
            "retrieved same-fd live rebuild is not object-exact",
        )
    return rebuilt


def write_retrieved_live_engineer_session_report_bundle(
    capture_handle: object,
    receipt: Mapping[str, object],
    artifact_path: str | Path,
    html_path: str | Path,
    *,
    expected_live_engineer_session_sha256: str,
    expected_remote_capture_sha256: str,
    expected_remote_capture_byte_size: int,
    stale_after_s: float = 0.5,
) -> dict[str, object]:
    """Replay a retrieved live proof and persist only its safe report projection.

    The complete engineer session exists only in memory.  The persisted JSON
    and HTML are generated after the rebuilt live wrapper is object-exact with
    the independently bound receipt, and the report excludes development-smoke
    fuel candidates by contract.
    """

    selected_stale = _validate_stale_after_s(stale_after_s)
    expected = validate_live_engineer_session(
        receipt,
        expected_live_engineer_session_sha256=expected_live_engineer_session_sha256,
        expected_capture_sha256=expected_remote_capture_sha256,
        expected_capture_byte_size=expected_remote_capture_byte_size,
    )
    authority = _mapping(expected["analysis_authority"], "analysis authority")
    report_output: list[tuple[dict[str, object], dict[str, object]]] = []
    with _verified_retrieved_capture_handle(
        capture_handle,
        expected_remote_capture_sha256=expected_remote_capture_sha256,
        expected_remote_capture_byte_size=expected_remote_capture_byte_size,
    ) as handle:
        descriptor = -1
        try:
            descriptor = os.dup(handle.fileno())
            os.lseek(descriptor, 0, os.SEEK_SET)
        except OSError as exc:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
            raise LiveEngineerSessionError(
                "RETRIEVED_CAPTURE_INVALID",
                "cannot duplicate retrieved capture descriptor for report",
            ) from exc
        try:
            with os.fdopen(
                descriptor,
                "r",
                encoding="utf-8",
                errors="strict",
                newline="",
            ) as text:
                descriptor = -1
                with open_collector_jsonl_snapshot(
                    text,
                    stale_after_s=selected_stale,
                ) as run:
                    rebuilt = _build_live_engineer_session_from_verified_handle(
                        run,
                        handle,
                        authority=authority,
                        stale_after_s=selected_stale,
                        report_output=report_output,
                    )
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
    if rebuilt != expected:
        _fail(
            "LIVE_REPLAY_MISMATCH",
            "retrieved report rebuild is not object-exact with the live receipt",
        )
    if len(report_output) != 1:
        _fail("REPORT_OUTPUT_INVALID", "live report was not built exactly once")
    report, core = report_output[0]
    proof = _mapping(expected["pipeline_proof"], "live pipeline proof")
    if report["engineer_session_binding"]["engineer_session_sha256"] != proof[
        "engineer_session_sha256"
    ]:
        _fail("REPORT_OUTPUT_INVALID", "report does not bind the live core proof")
    from .session_report import write_engineer_session_report_bundle_exclusive

    write_engineer_session_report_bundle_exclusive(
        artifact_path,
        html_path,
        report,
        core,
        expected_report_sha256=str(report["report_sha256"]),
        expected_engineer_session_sha256=str(core["engineer_session_sha256"]),
    )
    return {
        "advisor_only": True,
        "artifact_path": str(Path(artifact_path)),
        "contract_version": "retrieved-live-session-report-write-v1",
        "engineer_session_sha256": core["engineer_session_sha256"],
        "html_path": str(Path(html_path)),
        "live_engineer_session_sha256": expected[
            "live_engineer_session_sha256"
        ],
        "report_sha256": report["report_sha256"],
        "source_kind": report["engineer_session_binding"]["source_kind"],
        "status": report["status"],
        "vehicle_control_enabled": False,
    }


def _persisted_json(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise LiveEngineerSessionError(
            "OUTPUT_SERIALIZATION_FAILED",
            "live engineer session is not stable JSON",
        ) from exc


def write_live_engineer_session_handle(
    output_handle: object,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    """Write and read back one artifact through the caller-owned CreateNew fd.

    The handle is never closed.  On failure no pathname is opened, replaced, or
    unlinked; a partial same-file residue is intentionally left for inspection.
    """

    validated = validate_live_engineer_session(
        receipt,
        expected_live_engineer_session_sha256=_sha256(
            _mapping(receipt, "live engineer session").get(
                "live_engineer_session_sha256"
            ),
            "live engineer-session SHA-256",
        ),
    )
    payload = _persisted_json(validated)
    handle = _require_raw_snapshot(output_handle, writable=True)
    descriptor = handle.fileno()
    try:
        opened = os.fstat(descriptor)
        offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    except OSError as exc:
        raise LiveEngineerSessionError(
            "OUTPUT_HANDLE_INVALID", "cannot inspect output descriptor"
        ) from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_size != 0
        or offset != 0
    ):
        _fail(
            "OUTPUT_HANDLE_INVALID",
            "output must be an empty singly-linked CreateNew regular FileIO",
        )
    expected_identity = (opened.st_dev, opened.st_ino)
    expected_sha = hashlib.sha256(payload).hexdigest()
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        before_read = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        readback = bytearray()
        while len(readback) < len(payload):
            chunk = os.read(
                descriptor, min(1_048_576, len(payload) - len(readback))
            )
            if not chunk:
                break
            readback.extend(chunk)
        after_read = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise LiveEngineerSessionError(
            "OUTPUT_WRITE_FAILED", "same-descriptor output write/readback failed"
        ) from exc
    if (
        (before_read.st_dev, before_read.st_ino) != expected_identity
        or (after_read.st_dev, after_read.st_ino) != expected_identity
        or before_read.st_nlink != 1
        or after_read.st_nlink != 1
        or before_read.st_size != len(payload)
        or after_read.st_size != len(payload)
        or bytes(readback) != payload
        or hashlib.sha256(readback).hexdigest() != expected_sha
    ):
        _fail(
            "OUTPUT_CONTENT_CHANGED",
            "same-descriptor output closure differs",
        )
    return {
        "artifact_byte_size": len(payload),
        "artifact_sha256": expected_sha,
        "live_engineer_session_sha256": validated[
            "live_engineer_session_sha256"
        ],
    }


def write_live_engineer_session_exclusive(
    path: str | Path,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    """Local-only CreateNew convenience wrapper around the handle writer."""

    output = Path(path)
    if output.name in {"", ".", ".."}:
        _fail("OUTPUT_CREATE_FAILED", "output must name one file")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    for name in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW"):
        flags |= int(getattr(os, name, 0))
    try:
        descriptor = os.open(output, flags, 0o600)
    except OSError as exc:
        raise LiveEngineerSessionError(
            "OUTPUT_CREATE_FAILED", f"cannot CreateNew output: {output.name}"
        ) from exc
    try:
        with os.fdopen(descriptor, "r+b", buffering=0) as handle:
            return write_live_engineer_session_handle(handle, receipt)
    except BaseException:
        # Never unlink a failed artifact: another actor may have changed its
        # directory entry, and the retained residue is useful forensic state.
        raise


__all__ = [
    "ATTESTATION_STATUS",
    "CAPTURE_SNAPSHOT_METHOD",
    "CODE_TRUST_MODEL",
    "DEV_SMOKE_PROFILE_BYTE_SIZE",
    "DEV_SMOKE_PROFILE_ID",
    "DEV_SMOKE_PROFILE_SHA256",
    "EXECUTION_MODE",
    "FUEL_PIPELINE_ROLE",
    "LIVE_ANALYSIS_AUTHORITY_CONTRACT_VERSION",
    "LIVE_ENGINEER_SESSION_CONTRACT_VERSION",
    "LiveEngineerSessionError",
    "SCENARIO_ROLE",
    "build_live_engineer_session",
    "replay_live_engineer_session",
    "replay_retrieved_live_engineer_session",
    "validate_live_engineer_session",
    "write_live_engineer_session_exclusive",
    "write_live_engineer_session_handle",
    "write_retrieved_live_engineer_session_report_bundle",
]
