"""Machine-verifiable acceptance gate for the first Windows live capture."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any

from .adapters import CollectorInputEvidence, open_collector_jsonl
from .collector import CollectorReceipt
from .contracts import NORMALIZATION_PROFILE_VERSION
from .events import EVENT_CONTRACT_VERSION
from .fuel import FuelScenario
from .model_replay import FUEL_MODEL_REPLAY_CONTRACT_VERSION
from .telemetry import TELEMETRY_CONTRACT_VERSION, SourceKind

M0_ACCEPTANCE_CONTRACT_VERSION = "m0-acceptance-v2"
WINDOWS_LAUNCH_CONTRACT_VERSION = "windows-live-launch-v1"
WINDOWS_INSTALL_CONTRACT_VERSION = "windows-collector-install-v3"
PERFORMANCE_CONTRACT_VERSION = "sim-performance-ab-v2"
PERFORMANCE_SERIES_CONTRACT_VERSION = "sim-frame-time-series-v1"
MINIMUM_M0_CAPTURE_SECONDS = 30.0
MAXIMUM_M0_STALE_SECONDS = 0.5
MAXIMUM_P95_REGRESSION_PCT = 5.0

_HEX = frozenset("0123456789abcdef")
_MAX_JSON_BYTES = 8 * 1024 * 1024
_DENIED_PRIVATE_KEYS = frozenset({"custid", "driverinfo", "userid", "username"})
_LAUNCH_KEYS = frozenset(
    {
        "capture_byte_size",
        "capture_file",
        "capture_sha256",
        "collector_elapsed_seconds",
        "collector_process_receipt",
        "completed_at_utc",
        "contract_version",
        "install_contract_version",
        "install_manifest_file",
        "install_manifest_sha256",
        "launcher_file",
        "launcher_sha256",
        "poll_seconds",
        "requested_duration_seconds",
        "session_id",
        "sim_process_id",
        "sim_session_id",
        "source_id",
        "source_kind_request",
        "stale_after_seconds",
        "started_at_utc",
        "wait_seconds",
        "wheel_sha256",
        "windows_session_id",
    }
)
_INSTALL_KEYS = frozenset(
    {
        "binary_wheels_only",
        "cli_help",
        "import_smoke",
        "input_hashes_stable",
        "install_contract_version",
        "installed_at_utc",
        "installed_packages_file",
        "installed_packages_sha256",
        "installer_file",
        "installer_sha256",
        "launcher_file",
        "launcher_sha256",
        "live_capture_validated",
        "package_index_disabled",
        "pip_check",
        "python_architecture",
        "python_bits",
        "python_version",
        "requirements_file",
        "requirements_sha256",
        "wheel_file",
        "wheel_sha256",
        "wheelhouse_contract_version",
        "wheelhouse_manifest_file",
        "wheelhouse_manifest_sha256",
        "wheelhouse_target",
        "wheelhouse_total_bytes",
        "wheelhouse_wheel_count",
    }
)
_PERFORMANCE_KEYS = frozenset(
    {
        "baseline",
        "contract_version",
        "max_p95_regression_pct",
        "measurement_tool",
        "scenario_file",
        "scenario_id",
        "scenario_sha256",
        "sidecar",
        "telemetry_capture_sha256",
    }
)
_PERFORMANCE_SERIES_KEYS = frozenset({"artifact_file", "artifact_sha256"})
_PERFORMANCE_ARTIFACT_KEYS = frozenset(
    {
        "contract_version",
        "frame_times_ms",
        "measurement_tool",
        "role",
        "scenario_id",
        "scenario_sha256",
    }
)
_EVENT_OUTPUT_KEYS = frozenset(
    {
        "contract_version",
        "event_receipt",
        "event_replay_sha256",
        "input_evidence",
        "input_kind",
        "normalization",
        "quality_gate",
    }
)
_MODEL_OUTPUT_KEYS = frozenset(
    {
        "capabilities",
        "contract_version",
        "event_receipt",
        "fuel_replay_sha256",
        "input_evidence",
        "input_kind",
        "lap_receipt",
        "model_output",
        "model_output_sha256",
        "model_semantic_sha256",
        "normalized_input_receipt",
        "pipeline",
        "quality_gate",
        "recommendations",
        "scenario",
        "scenario_sha256",
        "series_evidence",
    }
)
_MODEL_CAPABILITY_KEYS = frozenset(
    {
        "current_tire_wear",
        "fuel_model_shadow",
        "opponent_fuel",
        "race_recommendation",
        "traffic_model",
    }
)
_EVENT_NORMALIZATION_KEYS = frozenset(
    {
        "config_sha256",
        "normalized_telemetry_contract_version",
        "opponent_error_policy",
        "profile_version",
        "stale_after_us",
    }
)
_EVENT_RECEIPT_KEYS = frozenset(
    {
        "accepted_sample_count",
        "config_sha256",
        "contract_version",
        "event_count",
        "event_kind_counts",
        "events_sha256",
        "receipt_sha256",
        "rejected_sample_count",
        "sample_count",
        "session_epoch_count",
        "source_epoch_count",
    }
)
_MODEL_NORMALIZATION_KEYS = frozenset(
    {"opponent_error_policy", "profile_version", "stale_after_us"}
)
_MODEL_PIPELINE_KEYS = frozenset(
    {
        "config_sha256",
        "event_contract_version",
        "feature_pipeline_version",
        "fuel_model_version",
        "lap_algorithm_version",
        "normalization",
        "normalized_telemetry_contract_version",
        "tick_rate_hz",
    }
)
_UNAVAILABLE_CAPABILITY_KEYS = frozenset(
    {
        "blocked_claims",
        "confidence",
        "contract_version",
        "estimate_available",
        "provenance",
        "reasons",
        "status",
    }
)
_FUEL_RECOMMENDATION_KEYS = frozenset(
    {
        "action",
        "claim_level",
        "confidence",
        "confidence_basis",
        "evidence_ids",
        "executable",
        "kind",
        "practice_only",
        "recommendation_id",
        "scenario_sha256",
        "status",
    }
)


class M0AcceptanceError(ValueError):
    """Raised when an M0 input or subprocess violates its contract."""


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise M0AcceptanceError("M0 value is not canonical-JSON-safe") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise M0AcceptanceError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _plain_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise M0AcceptanceError(f"{label} must be a plain integer >= {minimum}")
    return value


def _finite_number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise M0AcceptanceError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        qualifier = "positive" if positive else "non-negative"
        raise M0AcceptanceError(f"{label} must be finite and {qualifier}")
    return result


def _identifier(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise M0AcceptanceError(f"{label} is not a valid identifier")
    return value


def _exact_keys(value: object, expected: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise M0AcceptanceError(f"{label} must be an object")
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise M0AcceptanceError(f"{label} keys are invalid; missing={missing}, unexpected={extra}")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise M0AcceptanceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise M0AcceptanceError(f"non-standard JSON number is forbidden: {value}")


def _decode_json(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except M0AcceptanceError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError) as exc:
        raise M0AcceptanceError(f"{label} is not valid strict JSON") from exc
    if type(value) is not dict:
        raise M0AcceptanceError(f"{label} root must be an object")
    return value


def _read_json_file(path: Path, label: str) -> tuple[dict[str, object], str, int]:
    try:
        if path.is_symlink():
            raise M0AcceptanceError(f"{label} must not be a symbolic link")
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise M0AcceptanceError(f"{label} must be a regular file")
            payload = handle.read(_MAX_JSON_BYTES + 1)
    except M0AcceptanceError:
        raise
    except OSError as exc:
        raise M0AcceptanceError(f"cannot read {label}: {path}") from exc
    if len(payload) > _MAX_JSON_BYTES:
        raise M0AcceptanceError(f"{label} exceeds {_MAX_JSON_BYTES} bytes")
    return _decode_json(payload, label), _sha256_bytes(payload), len(payload)


@contextmanager
def _capture_snapshot(path: Path) -> Iterator[tuple[Path, str, int]]:
    """Copy one open capture into a private file used by every validation pass."""

    if path.is_symlink():
        raise M0AcceptanceError("capture must not be a symbolic link")
    with tempfile.TemporaryDirectory(prefix="iracing-m0-") as directory:
        snapshot = Path(directory) / "capture.jsonl"
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as source, snapshot.open("xb") as target:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise M0AcceptanceError("capture must be a regular file")
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except M0AcceptanceError:
            raise
        except OSError as exc:
            raise M0AcceptanceError(f"cannot snapshot capture: {path}") from exc
        yield snapshot, digest.hexdigest(), size


def _timestamp(value: object, label: str) -> datetime:
    if type(value) is not str:
        raise M0AcceptanceError(f"{label} must be an ISO timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise M0AcceptanceError(f"{label} must be an ISO timestamp") from exc


def _validate_install(
    install: dict[str, object],
    *,
    install_sha256: str,
    install_filename: str,
    launch: Mapping[str, object],
    expected_installer_sha256: str,
    expected_launcher_sha256: str,
    expected_requirements_sha256: str,
    expected_wheel_sha256: str,
    expected_wheelhouse_manifest_sha256: str,
) -> dict[str, object]:
    _exact_keys(install, _INSTALL_KEYS, "install manifest")
    if install["install_contract_version"] != WINDOWS_INSTALL_CONTRACT_VERSION:
        raise M0AcceptanceError("install manifest contract is unsupported")
    if launch["install_contract_version"] != WINDOWS_INSTALL_CONTRACT_VERSION:
        raise M0AcceptanceError("launch receipt install contract is unsupported")
    if launch["install_manifest_file"] != install_filename:
        raise M0AcceptanceError("launch receipt names a different install manifest")
    if _sha256(launch["install_manifest_sha256"], "install manifest SHA") != (install_sha256):
        raise M0AcceptanceError("launch receipt install manifest SHA does not match")
    wheel_sha = _sha256(install["wheel_sha256"], "install wheel SHA")
    requirements_sha = _sha256(install["requirements_sha256"], "install requirements SHA")
    installer_sha = _sha256(install["installer_sha256"], "install installer SHA")
    if _sha256(launch["wheel_sha256"], "launch wheel SHA") != wheel_sha:
        raise M0AcceptanceError("launch and install wheel SHA values do not match")
    if install["package_index_disabled"] is not True:
        raise M0AcceptanceError("install manifest did not disable the package index")
    if install["binary_wheels_only"] is not True:
        raise M0AcceptanceError("install manifest did not require binary wheels")
    if install["pip_check"] != "PASS" or install["import_smoke"] != "PASS":
        raise M0AcceptanceError("install manifest runtime checks did not pass")
    if install["cli_help"] != "PASS" or install["input_hashes_stable"] != "PASS":
        raise M0AcceptanceError("install manifest bundle checks did not pass")
    if install["live_capture_validated"] is not False:
        raise M0AcceptanceError("install manifest must precede live validation")
    if install["wheelhouse_target"] != "cp312-cp312-win_amd64":
        raise M0AcceptanceError("install wheelhouse target is not cp312-win_amd64")
    if install["wheelhouse_contract_version"] != "windows-wheelhouse-manifest-v1":
        raise M0AcceptanceError("install wheelhouse contract is unsupported")
    wheelhouse_manifest_sha = _sha256(
        install["wheelhouse_manifest_sha256"], "wheelhouse manifest SHA"
    )
    launcher_sha = _sha256(install["launcher_sha256"], "install launcher SHA")
    if _sha256(launch["launcher_sha256"], "launch launcher SHA") != launcher_sha:
        raise M0AcceptanceError("launch and install launcher SHA values do not match")
    if launch["launcher_file"] != install["launcher_file"]:
        raise M0AcceptanceError("launch and install launcher filenames do not match")
    expected_hashes = {
        "installer_sha256": _sha256(expected_installer_sha256, "expected installer SHA"),
        "launcher_sha256": _sha256(expected_launcher_sha256, "expected launcher SHA"),
        "requirements_sha256": _sha256(expected_requirements_sha256, "expected requirements SHA"),
        "wheel_sha256": _sha256(expected_wheel_sha256, "expected wheel SHA"),
        "wheelhouse_manifest_sha256": _sha256(
            expected_wheelhouse_manifest_sha256,
            "expected wheelhouse manifest SHA",
        ),
    }
    observed_hashes = {
        "installer_sha256": installer_sha,
        "launcher_sha256": launcher_sha,
        "requirements_sha256": requirements_sha,
        "wheel_sha256": wheel_sha,
        "wheelhouse_manifest_sha256": wheelhouse_manifest_sha,
    }
    if observed_hashes != expected_hashes:
        mismatches = sorted(
            name for name, value in observed_hashes.items() if value != expected_hashes[name]
        )
        raise M0AcceptanceError(
            f"install manifest does not match approved release hashes: {mismatches}"
        )
    _plain_int(install["wheelhouse_wheel_count"], "wheelhouse wheel count", minimum=1)
    _plain_int(install["wheelhouse_total_bytes"], "wheelhouse total bytes", minimum=1)
    return {
        "install_contract_version": WINDOWS_INSTALL_CONTRACT_VERSION,
        "install_manifest_sha256": install_sha256,
        "external_release_hash_status": "MATCHED",
        "installer_sha256": installer_sha,
        "package_index_disabled": True,
        "launcher_sha256": launcher_sha,
        "requirements_sha256": requirements_sha,
        "wheel_sha256": wheel_sha,
        "wheelhouse_manifest_sha256": wheelhouse_manifest_sha,
        "wheelhouse_target": install["wheelhouse_target"],
        "wheelhouse_wheel_count": install["wheelhouse_wheel_count"],
    }


def _validate_launch(
    launch: dict[str, object],
    *,
    capture_name: str,
    capture_sha256: str,
    capture_size: int,
    evidence: CollectorInputEvidence,
    minimum_capture_s: float,
) -> dict[str, object]:
    _exact_keys(launch, _LAUNCH_KEYS, "launch receipt")
    if launch["contract_version"] != WINDOWS_LAUNCH_CONTRACT_VERSION:
        raise M0AcceptanceError("launch receipt contract is unsupported")
    if launch["capture_file"] != capture_name:
        raise M0AcceptanceError("launch receipt names a different capture")
    if _sha256(launch["capture_sha256"], "launch capture SHA") != capture_sha256:
        raise M0AcceptanceError("launch capture SHA does not match the snapshot")
    if _plain_int(launch["capture_byte_size"], "launch capture byte size", minimum=1) != (
        capture_size
    ):
        raise M0AcceptanceError("launch capture byte size does not match")
    if launch["source_kind_request"] != "live":
        raise M0AcceptanceError("M0 requires an explicit live source-kind request")
    if _identifier(launch["source_id"], "launch source_id") != evidence.source_id:
        raise M0AcceptanceError("launch source_id does not match collector evidence")
    if _identifier(launch["session_id"], "launch session_id") != evidence.session_id:
        raise M0AcceptanceError("launch session_id does not match collector evidence")
    windows_session = _plain_int(launch["windows_session_id"], "Windows session ID")
    sim_session = _plain_int(launch["sim_session_id"], "sim session ID")
    if windows_session != sim_session:
        raise M0AcceptanceError("launcher and simulator Windows sessions differ")
    _plain_int(launch["sim_process_id"], "sim process ID", minimum=1)
    requested_duration = _finite_number(
        launch["requested_duration_seconds"],
        "requested duration",
        positive=True,
    )
    if requested_duration < minimum_capture_s:
        raise M0AcceptanceError("requested duration is below the M0 capture threshold")
    elapsed = _finite_number(
        launch["collector_elapsed_seconds"], "collector elapsed", positive=True
    )
    if elapsed < requested_duration * 0.9:
        raise M0AcceptanceError("collector elapsed time is shorter than requested")
    poll = _finite_number(launch["poll_seconds"], "poll seconds", positive=True)
    stale = _finite_number(launch["stale_after_seconds"], "stale threshold", positive=True)
    if stale > MAXIMUM_M0_STALE_SECONDS:
        raise M0AcceptanceError("launcher stale threshold exceeds the M0 maximum")
    _finite_number(launch["wait_seconds"], "wait seconds")
    started = _timestamp(launch["started_at_utc"], "started_at_utc")
    completed = _timestamp(launch["completed_at_utc"], "completed_at_utc")
    if (
        started.tzinfo is None
        or completed.tzinfo is None
        or started.utcoffset().total_seconds() != 0
        or completed.utcoffset().total_seconds() != 0
        or completed < started
    ):
        raise M0AcceptanceError("launch timestamps are not ordered UTC-aware values")
    timestamp_elapsed = (completed - started).total_seconds()
    if abs(timestamp_elapsed - elapsed) > max(5.0, elapsed * 0.1):
        raise M0AcceptanceError("launch timestamps disagree with collector elapsed time")
    capture_span_s = (
        None if evidence.capture_span_us is None else evidence.capture_span_us / 1_000_000
    )
    if capture_span_s is None or capture_span_s < requested_duration * 0.9:
        raise M0AcceptanceError("validated telemetry span is shorter than the requested duration")
    _sha256(launch["launcher_sha256"], "launcher SHA")
    if type(launch["launcher_file"]) is not str or not launch["launcher_file"]:
        raise M0AcceptanceError("launch receipt launcher_file is invalid")

    receipt = _exact_keys(
        launch["collector_process_receipt"],
        frozenset(item.name for item in fields(CollectorReceipt)),
        "collector process receipt",
    )
    evidence_payload = evidence.to_dict()
    expected_receipt = {
        name: (1 if name == "run_record_count" else evidence_payload.get(name)) for name in receipt
    }
    for name, value in receipt.items():
        if expected_receipt[name] != value:
            raise M0AcceptanceError(
                f"collector process receipt disagrees with validated capture: {name}"
            )
    return {
        "capture_sha256": capture_sha256,
        "collector_elapsed_seconds": elapsed,
        "launch_contract_version": WINDOWS_LAUNCH_CONTRACT_VERSION,
        "poll_seconds": poll,
        "requested_duration_seconds": requested_duration,
        "stale_after_seconds": stale,
        "windows_session_id": windows_session,
    }


def _normalized_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _private_key_count(value: object) -> int:
    if isinstance(value, Mapping):
        total = 0
        for key, item in value.items():
            if isinstance(key, str) and _normalized_key(key) in _DENIED_PRIVATE_KEYS:
                total += 1
            total += _private_key_count(item)
        return total
    if isinstance(value, list):
        return sum(_private_key_count(item) for item in value)
    return 0


def _scan_capture_privacy(snapshot: Path) -> dict[str, object]:
    denied = 0
    records = 0
    try:
        with snapshot.open("r", encoding="utf-8", newline="") as handle:
            for line in handle:
                if not line.endswith("\n"):
                    raise M0AcceptanceError("privacy scan saw an incomplete JSONL record")
                value = _decode_json(line.encode("utf-8"), "collector privacy record")
                denied += _private_key_count(value)
                records += 1
    except M0AcceptanceError:
        raise
    except (OSError, UnicodeError) as exc:
        raise M0AcceptanceError("cannot privacy-scan collector snapshot") from exc
    return {
        "denied_private_key_count": denied,
        "record_count": records,
        "status": "PASS" if denied == 0 else "FAIL",
    }


def _capture_gate(
    evidence: CollectorInputEvidence,
    *,
    minimum_capture_s: float,
) -> dict[str, object]:
    reasons: list[str] = []
    if evidence.source_kind is not SourceKind.SDK_LIVE or evidence.sim_mode != "full":
        reasons.append("NOT_LIVE_SIM_MODE")
    if evidence.completion_status != "COMPLETE":
        reasons.append("CAPTURE_NOT_COMPLETE")
    for field, reason in (
        ("duplicate_conflict_count", "DUPLICATE_CONFLICTS"),
        ("dropped_tick_count", "DROPPED_TICKS"),
        ("stale_event_count", "SOURCE_STALE_EVENTS"),
        ("schema_change_count", "SCHEMA_CHANGED"),
        ("session_reset_count", "SESSION_RESET"),
        ("capture_clock_regression_count", "CAPTURE_CLOCK_REGRESSION"),
        ("read_error_frame_count", "SDK_READ_ERRORS"),
        ("driver_info_key_count", "DRIVER_INFO_PERSISTED"),
    ):
        if getattr(evidence, field):
            reasons.append(reason)
    if len(evidence.tick_rate_hz_values) != 1:
        reasons.append("TICK_RATE_NOT_STABLE")
    span_s = None if evidence.capture_span_us is None else evidence.capture_span_us / 1_000_000
    if span_s is None or span_s < minimum_capture_s:
        reasons.append("CAPTURE_TOO_SHORT")
    observed_frame_rate_hz: float | None = None
    if span_s is not None and span_s > 0 and evidence.frame_record_count > 1:
        observed_frame_rate_hz = (evidence.frame_record_count - 1) / span_s
        if (
            len(evidence.tick_rate_hz_values) == 1
            and observed_frame_rate_hz < evidence.tick_rate_hz_values[0] * 0.9
        ):
            reasons.append("OBSERVED_FRAME_RATE_TOO_LOW")
    return {
        "capture_span_seconds": span_s,
        "frame_record_count": evidence.frame_record_count,
        "observed_frame_rate_hz": observed_frame_rate_hz,
        "reasons": list(dict.fromkeys(reasons)),
        "status": "PASS" if not reasons else "FAIL",
        "tick_rate_hz_values": list(evidence.tick_rate_hz_values),
    }


def _fresh_cli_run(
    arguments: list[str],
    *,
    hash_seed: str,
    timeout_s: float,
) -> tuple[bytes, dict[str, object], int]:
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")
    }
    environment["PYTHONHASHSEED"] = hash_seed
    package_root = str(Path(__file__).resolve().parent.parent)
    bootstrap = (
        "import json,runpy,sys; "
        "root=sys.argv[1]; declared_seed=sys.argv[2]; args=sys.argv[3:]; "
        "print(json.dumps({'declared_seed':declared_seed,"
        "'hash_probe':hash('iracing-m0-hash-probe-v1'),"
        "'ignore_environment':sys.flags.ignore_environment,"
        "'no_user_site':sys.flags.no_user_site,"
        "'safe_path':sys.flags.safe_path},sort_keys=True),file=sys.stderr); "
        "sys.path.insert(0,root); sys.argv=['iracing-aie',*args]; "
        "runpy.run_module('iracing_ai_engineer.cli',run_name='__main__')"
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-s",
                "-P",
                "-B",
                "-X",
                "utf8",
                "-c",
                bootstrap,
                package_root,
                hash_seed,
                *arguments,
            ],
            check=False,
            capture_output=True,
            timeout=timeout_s,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise M0AcceptanceError("fresh replay subprocess failed to execute") from exc
    if result.returncode != 0:
        raise M0AcceptanceError(
            f"fresh replay subprocess exited {result.returncode}: "
            + result.stderr.decode("utf-8", errors="replace").strip()
        )
    probe = _decode_json(result.stderr, "fresh replay hash probe")
    if set(probe) != {
        "declared_seed",
        "hash_probe",
        "ignore_environment",
        "no_user_site",
        "safe_path",
    }:
        raise M0AcceptanceError("fresh replay hash probe keys are invalid")
    if (
        probe["declared_seed"] != hash_seed
        or type(probe["hash_probe"]) is not int
        or probe["ignore_environment"] != 0
        or probe["no_user_site"] != 1
        or probe["safe_path"] is not True
    ):
        raise M0AcceptanceError("fresh replay hash seed was not verified")
    if len(result.stdout) > 32 * 1024 * 1024:
        raise M0AcceptanceError("fresh replay subprocess output is too large")
    return (
        result.stdout,
        _decode_json(result.stdout, "fresh replay output"),
        probe["hash_probe"],
    )


def _double_replay(
    arguments: list[str],
    *,
    timeout_s: float,
) -> tuple[dict[str, object], str, dict[str, int]]:
    first_bytes, first, first_probe = _fresh_cli_run(arguments, hash_seed="1", timeout_s=timeout_s)
    second_bytes, second, second_probe = _fresh_cli_run(
        arguments, hash_seed="987654", timeout_s=timeout_s
    )
    if first_probe == second_probe:
        raise M0AcceptanceError("fresh replay processes did not use distinct hash seeds")
    if first_bytes != second_bytes or first != second:
        raise M0AcceptanceError("fresh replay processes were not byte-identical")
    return (
        first,
        _sha256_bytes(first_bytes),
        {"1": first_probe, "987654": second_probe},
    )


def _validate_event_output(
    payload: dict[str, object],
    *,
    evidence: CollectorInputEvidence,
    stale_after_s: float,
) -> dict[str, object]:
    _exact_keys(payload, _EVENT_OUTPUT_KEYS, "event replay output")
    if payload["contract_version"] != "event-replay-v1":
        raise M0AcceptanceError("event replay output contract is unsupported")
    claimed_sha = _sha256(payload["event_replay_sha256"], "event replay SHA")
    binding = {key: value for key, value in payload.items() if key != "event_replay_sha256"}
    if _digest(binding) != claimed_sha:
        raise M0AcceptanceError("event replay composite SHA does not match its output")
    if payload["input_kind"] != "collector":
        raise M0AcceptanceError("event replay did not traverse the collector adapter")
    if payload["input_evidence"] != evidence.to_dict():
        raise M0AcceptanceError("event replay evidence does not match the capture")
    normalization = payload["normalization"]
    if type(normalization) is not dict or normalization.get("stale_after_us") != round(
        stale_after_s * 1_000_000
    ):
        raise M0AcceptanceError("event replay normalization threshold does not match")
    receipt = _validate_event_receipt(payload["event_receipt"])
    if receipt["sample_count"] != evidence.frame_record_count:
        raise M0AcceptanceError("event replay sample count does not match the capture")
    quality = payload["quality_gate"]
    if (
        type(quality) is not dict
        or quality.get("status") not in {"PASS", "DEGRADED"}
        or type(quality.get("reasons")) is not list
    ):
        raise M0AcceptanceError("event replay quality gate is invalid")
    return {
        "event_replay_sha256": claimed_sha,
        "quality_gate": quality,
        "status": "PASS" if quality["status"] == "PASS" else "FAIL",
    }


def _validate_model_output(
    payload: dict[str, object],
    *,
    evidence: CollectorInputEvidence,
    scenario: FuelScenario,
    stale_after_s: float,
) -> dict[str, object]:
    _exact_keys(payload, _MODEL_OUTPUT_KEYS, "fuel model replay output")
    if payload["contract_version"] != FUEL_MODEL_REPLAY_CONTRACT_VERSION:
        raise M0AcceptanceError("fuel model replay output contract is unsupported")
    claimed_sha = _sha256(payload["fuel_replay_sha256"], "fuel replay SHA")
    binding = {key: value for key, value in payload.items() if key != "fuel_replay_sha256"}
    if _digest(binding) != claimed_sha:
        raise M0AcceptanceError("fuel replay composite SHA does not match its output")
    if payload["input_kind"] != "collector":
        raise M0AcceptanceError("fuel replay did not traverse the collector adapter")
    if payload["input_evidence"] != evidence.to_dict():
        raise M0AcceptanceError("fuel replay evidence does not match the capture")
    event_receipt = _validate_event_receipt(payload["event_receipt"])
    if event_receipt["sample_count"] != evidence.frame_record_count:
        raise M0AcceptanceError("fuel event receipt sample count does not match capture")
    scenario_payload = scenario.to_dict()
    if payload["scenario"] != scenario_payload or _sha256(
        payload["scenario_sha256"], "fuel scenario SHA"
    ) != _digest(scenario_payload):
        raise M0AcceptanceError("fuel replay scenario binding does not match")
    if _sha256(payload["model_output_sha256"], "model output SHA") != _digest(
        payload["model_output"]
    ):
        raise M0AcceptanceError("fuel replay model output SHA does not match")
    semantic_binding = {
        "lap_receipt": payload["lap_receipt"],
        "model_output": payload["model_output"],
        "pipeline": payload["pipeline"],
        "scenario": payload["scenario"],
    }
    semantic_sha = _sha256(payload["model_semantic_sha256"], "model semantic SHA")
    if semantic_sha != _digest(semantic_binding):
        raise M0AcceptanceError("fuel replay semantic SHA does not match")
    normalized = _exact_keys(
        payload["normalized_input_receipt"],
        frozenset({"contract_version", "sample_count", "samples_sha256"}),
        "normalized input receipt",
    )
    if normalized["contract_version"] != TELEMETRY_CONTRACT_VERSION:
        raise M0AcceptanceError("normalized input receipt contract is unsupported")
    if (
        _plain_int(normalized["sample_count"], "normalized input sample_count")
        != evidence.frame_record_count
    ):
        raise M0AcceptanceError("normalized input sample count does not match capture")
    _sha256(normalized["samples_sha256"], "normalized samples SHA")
    pipeline = payload["pipeline"]
    if type(pipeline) is not dict:
        raise M0AcceptanceError("fuel replay pipeline receipt is invalid")
    normalization = pipeline.get("normalization")
    if type(normalization) is not dict or normalization.get("stale_after_us") != round(
        stale_after_s * 1_000_000
    ):
        raise M0AcceptanceError("fuel replay normalization threshold does not match")
    quality = _exact_keys(
        payload["quality_gate"],
        frozenset({"reasons", "status"}),
        "fuel model quality gate",
    )
    if quality["status"] not in {"PASS", "DEGRADED"} or type(quality["reasons"]) is not list:
        raise M0AcceptanceError("fuel model quality gate is invalid")
    reasons = quality["reasons"]
    if any(type(reason) is not str or not reason for reason in reasons) or len(reasons) != len(
        set(reasons)
    ):
        raise M0AcceptanceError("fuel model quality reasons are invalid")
    model_output = payload["model_output"]
    if model_output is not None and type(model_output) is not dict:
        raise M0AcceptanceError("fuel model output must be an object or null")
    model_ready = (
        quality["status"] == "PASS"
        and type(model_output) is dict
        and model_output.get("status") == "ready"
    )
    fuel_capability = _validate_model_capabilities(
        payload["capabilities"],
        quality_reasons=reasons,
        model_ready=model_ready,
    )
    _validate_fuel_recommendations(
        payload["recommendations"],
        model_output=model_output,
        model_ready=model_ready,
        normalized_samples_sha256=normalized["samples_sha256"],
        pipeline=pipeline,
        scenario=scenario,
        scenario_sha256=payload["scenario_sha256"],
    )
    if model_ready:
        readiness_status = "PASS"
    elif reasons == ["INSUFFICIENT_VALID_FUEL_LAPS"]:
        readiness_status = "WAIT_DATA"
    else:
        readiness_status = "FAIL"
    return {
        "capability": fuel_capability,
        "fuel_replay_sha256": claimed_sha,
        "model_semantic_sha256": semantic_sha,
        "normalized_input_receipt": normalized,
        "quality_gate": quality,
        "readiness_status": readiness_status,
        "traversal_status": "PASS",
    }


def _validate_event_receipt(value: object) -> dict[str, object]:
    receipt = _exact_keys(value, _EVENT_RECEIPT_KEYS, "event receipt")
    if receipt["contract_version"] != EVENT_CONTRACT_VERSION:
        raise M0AcceptanceError("event receipt contract is unsupported")
    for name in ("config_sha256", "events_sha256", "receipt_sha256"):
        _sha256(receipt[name], f"event receipt {name}")
    counts = {
        name: _plain_int(receipt[name], f"event receipt {name}")
        for name in (
            "accepted_sample_count",
            "event_count",
            "rejected_sample_count",
            "sample_count",
            "session_epoch_count",
            "source_epoch_count",
        )
    }
    if counts["accepted_sample_count"] + counts["rejected_sample_count"] != counts["sample_count"]:
        raise M0AcceptanceError("event receipt accepted/rejected counts do not close")
    event_kind_counts = receipt["event_kind_counts"]
    if type(event_kind_counts) is not dict:
        raise M0AcceptanceError("event receipt kind counts must be an object")
    measured_event_count = 0
    for name, count in event_kind_counts.items():
        _identifier(name, "event receipt kind")
        measured_event_count += _plain_int(count, f"event receipt kind {name}")
    if measured_event_count != counts["event_count"]:
        raise M0AcceptanceError("event receipt kind counts do not close")
    receipt_binding = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    if receipt["receipt_sha256"] != _digest(receipt_binding):
        raise M0AcceptanceError("event receipt SHA does not match")
    return receipt


def _validate_shared_pipeline_outputs(
    event_payload: dict[str, object],
    model_payload: dict[str, object],
) -> dict[str, object]:
    """Prove the isolated event and fuel traversals used one shared pipeline."""

    event_receipt = event_payload.get("event_receipt")
    model_event_receipt = model_payload.get("event_receipt")
    if type(event_receipt) is not dict or type(model_event_receipt) is not dict:
        raise M0AcceptanceError("shared pipeline event receipts must be objects")
    if event_receipt != model_event_receipt:
        raise M0AcceptanceError("standalone event and fuel replay event receipts do not match")

    event_normalization = _exact_keys(
        event_payload.get("normalization"),
        _EVENT_NORMALIZATION_KEYS,
        "event replay normalization",
    )
    event_normalization_binding = {
        key: value for key, value in event_normalization.items() if key != "config_sha256"
    }
    if _sha256(
        event_normalization["config_sha256"],
        "event normalization config SHA",
    ) != _digest(event_normalization_binding):
        raise M0AcceptanceError("event normalization config SHA does not match")

    pipeline = _exact_keys(
        model_payload.get("pipeline"),
        _MODEL_PIPELINE_KEYS,
        "fuel replay pipeline",
    )
    pipeline_binding = {key: value for key, value in pipeline.items() if key != "config_sha256"}
    if _sha256(pipeline["config_sha256"], "fuel pipeline config SHA") != _digest(pipeline_binding):
        raise M0AcceptanceError("fuel pipeline config SHA does not match")
    model_normalization = _exact_keys(
        pipeline["normalization"],
        _MODEL_NORMALIZATION_KEYS,
        "fuel replay normalization",
    )
    expected_normalization = {
        "opponent_error_policy": event_normalization["opponent_error_policy"],
        "profile_version": event_normalization["profile_version"],
        "stale_after_us": event_normalization["stale_after_us"],
    }
    if model_normalization != expected_normalization:
        raise M0AcceptanceError(
            "standalone event and fuel replay normalization configs do not match"
        )
    if (
        event_normalization["normalized_telemetry_contract_version"] != TELEMETRY_CONTRACT_VERSION
        or event_normalization["profile_version"] != NORMALIZATION_PROFILE_VERSION
        or event_normalization["opponent_error_policy"] != "degrade"
        or pipeline["normalized_telemetry_contract_version"] != TELEMETRY_CONTRACT_VERSION
        or pipeline["event_contract_version"] != EVENT_CONTRACT_VERSION
    ):
        raise M0AcceptanceError("shared pipeline contracts are not the frozen versions")

    normalized = _exact_keys(
        model_payload.get("normalized_input_receipt"),
        frozenset({"contract_version", "sample_count", "samples_sha256"}),
        "fuel normalized input receipt",
    )
    if normalized["sample_count"] != event_receipt.get("sample_count"):
        raise M0AcceptanceError(
            "shared event receipt and normalized input sample counts do not match"
        )
    return {
        "event_contract_version": EVENT_CONTRACT_VERSION,
        "event_receipt_sha256": _digest(event_receipt),
        "normalization_config_sha256": _digest(expected_normalization),
        "normalized_input_receipt": normalized,
        "normalized_telemetry_contract_version": TELEMETRY_CONTRACT_VERSION,
        "status": "PASS",
    }


def _validate_model_capabilities(
    value: object,
    *,
    quality_reasons: list[object],
    model_ready: bool,
) -> dict[str, object]:
    capabilities = _exact_keys(value, _MODEL_CAPABILITY_KEYS, "fuel model capabilities")
    expected_unavailable = {
        "current_tire_wear": {
            "blocked_claims": ["CURRENT_TIRE_WEAR_CLAIM"],
            "confidence": "NONE",
            "contract_version": "inference-capability-v1",
            "estimate_available": False,
            "provenance": "UNKNOWN",
            "reasons": ["CURRENT_STINT_TIRE_WEAR_MODEL_NOT_IMPLEMENTED"],
            "status": "SKIP",
        },
        "opponent_fuel": {
            "blocked_claims": ["OPPONENT_FUEL_CLAIM"],
            "confidence": "NONE",
            "contract_version": "inference-capability-v1",
            "estimate_available": False,
            "provenance": "UNKNOWN",
            "reasons": ["OPPONENT_FUEL_NOT_EXPOSED_BY_SDK"],
            "status": "SKIP",
        },
        "traffic_model": {
            "blocked_claims": ["REJOIN_TRAFFIC_CLAIM"],
            "confidence": "NONE",
            "contract_version": "inference-capability-v1",
            "estimate_available": False,
            "provenance": "UNKNOWN",
            "reasons": ["TRAFFIC_MODEL_NOT_IMPLEMENTED"],
            "status": "SKIP",
        },
    }
    for name, expected in expected_unavailable.items():
        record = _exact_keys(
            capabilities[name],
            _UNAVAILABLE_CAPABILITY_KEYS,
            f"fuel model capability {name}",
        )
        if record != expected:
            raise M0AcceptanceError(f"fuel model capability {name} is invalid")
    fuel = _exact_keys(
        capabilities["fuel_model_shadow"],
        frozenset({"reasons", "status"}),
        "fuel model shadow capability",
    )
    if fuel != {
        "reasons": [] if model_ready else quality_reasons,
        "status": "PASS" if model_ready else "FAIL",
    }:
        raise M0AcceptanceError("fuel model shadow capability is inconsistent")
    race = _exact_keys(
        capabilities["race_recommendation"],
        frozenset({"reasons", "status"}),
        "race recommendation capability",
    )
    if race != {
        "reasons": [
            "SHADOW_ONLY",
            "EVENT_RULES_PROFILE_MISSING",
            "TRAFFIC_MODEL_NOT_IMPLEMENTED",
        ],
        "status": "BLOCKED",
    }:
        raise M0AcceptanceError("race recommendation capability is invalid")
    return fuel


def _validate_fuel_recommendations(
    value: object,
    *,
    model_output: dict[str, object] | None,
    model_ready: bool,
    normalized_samples_sha256: object,
    pipeline: dict[str, object],
    scenario: FuelScenario,
    scenario_sha256: object,
) -> None:
    if type(value) is not list:
        raise M0AcceptanceError("fuel recommendations must be a list")
    if not model_ready:
        if value:
            raise M0AcceptanceError("fuel recommendations require a ready shadow model")
        return
    if len(value) != 1:
        raise M0AcceptanceError("ready fuel model must emit exactly one recommendation")
    assert model_output is not None
    recommendation = _exact_keys(value[0], _FUEL_RECOMMENDATION_KEYS, "fuel recommendation")
    burn = model_output.get("burn")
    if type(burn) is not dict or burn.get("confidence") not in {"low", "medium", "high"}:
        raise M0AcceptanceError("ready fuel burn confidence is invalid")
    expected_action = {
        "cumulative_refuel_to_end": model_output.get("cumulative_refuel_to_end_l"),
        "minimum_pit_stops": model_output.get("minimum_pit_stops"),
        "next_pit_window": model_output.get("next_pit_window"),
    }
    expected_basis = {
        "historical_burn_stability": burn["confidence"].upper(),
        "overall_plan": "LOW_BECAUSE_EVENT_RULES_AND_TRAFFIC_ARE_UNAVAILABLE",
        "scenario_inputs": scenario.provenance,
    }
    expected_values = {
        "action": expected_action,
        "claim_level": "scenario_estimate",
        "confidence": "LOW",
        "confidence_basis": expected_basis,
        "executable": False,
        "kind": "FUEL_PLAN_CANDIDATE",
        "practice_only": False,
        "recommendation_id": "fuel:shadow_plan",
        "scenario_sha256": scenario_sha256,
        "status": "SHADOW_ONLY",
    }
    if any(recommendation[key] != expected for key, expected in expected_values.items()):
        raise M0AcceptanceError("fuel recommendation semantics are invalid")
    evidence_ids = recommendation["evidence_ids"]
    if type(evidence_ids) is not list:
        raise M0AcceptanceError("fuel recommendation evidence IDs are invalid")
    accepted_laps = _plain_int(burn.get("accepted_laps"), "accepted fuel laps", minimum=1)
    lap_algorithm = _identifier(pipeline.get("lap_algorithm_version"), "fuel lap algorithm version")
    samples_sha256 = _sha256(normalized_samples_sha256, "normalized samples SHA")
    prefix = f"{samples_sha256}:{lap_algorithm}:lap:"
    if (
        len(evidence_ids) != accepted_laps
        or len(evidence_ids) != len(set(evidence_ids))
        or any(
            type(item) is not str
            or not item.startswith(prefix)
            or not item.removeprefix(prefix).isdigit()
            or int(item.removeprefix(prefix)) <= 0
            for item in evidence_ids
        )
    ):
        raise M0AcceptanceError("fuel recommendation evidence IDs are invalid")


def _scenario_arguments(scenario: FuelScenario) -> list[str]:
    arguments = [
        "--current-fuel-l",
        str(scenario.current_fuel_l),
        "--tank-capacity-l",
        str(scenario.tank_capacity_l),
        "--refuel-rate-lps",
        str(scenario.refuel_rate_l_per_s),
        "--reserve-l",
        str(scenario.reserve_l),
        "--fuel-quantile",
        str(scenario.conservative_quantile),
        "--minimum-fuel-laps",
        str(scenario.minimum_valid_laps),
        "--timed-race-extra-laps",
        str(scenario.timed_race_extra_laps),
    ]
    if scenario.remaining_laps is not None:
        arguments.extend(("--remaining-laps", str(scenario.remaining_laps)))
    if scenario.remaining_time_s is not None:
        arguments.extend(("--remaining-time-s", str(scenario.remaining_time_s)))
    if scenario.reference_lap_time_s is not None:
        arguments.extend(("--reference-lap-time-s", str(scenario.reference_lap_time_s)))
    return arguments


def _artifact_filename(value: object, label: str) -> str:
    filename = _identifier(value, label)
    if filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise M0AcceptanceError(f"{label} must be a plain filename")
    return filename


def _validate_performance_artifact(
    path: Path,
    *,
    expected_sha256: str,
    role: str,
    measurement_tool: str,
    scenario_id: str,
    scenario_sha256: str,
) -> dict[str, object]:
    artifact, artifact_sha, artifact_size = _read_json_file(path, f"performance {role} artifact")
    if artifact_sha != expected_sha256:
        raise M0AcceptanceError(f"performance {role} artifact SHA does not match")
    _exact_keys(
        artifact,
        _PERFORMANCE_ARTIFACT_KEYS,
        f"performance {role} artifact",
    )
    if artifact["contract_version"] != PERFORMANCE_SERIES_CONTRACT_VERSION:
        raise M0AcceptanceError(f"performance {role} artifact contract is unsupported")
    if artifact["role"] != role:
        raise M0AcceptanceError(f"performance {role} artifact role does not match")
    if artifact["measurement_tool"] != measurement_tool:
        raise M0AcceptanceError(f"performance {role} artifact measurement tool does not match")
    if artifact["scenario_id"] != scenario_id:
        raise M0AcceptanceError(f"performance {role} artifact scenario_id does not match")
    if artifact["scenario_sha256"] != scenario_sha256:
        raise M0AcceptanceError(f"performance {role} artifact scenario SHA does not match")
    frame_times = artifact["frame_times_ms"]
    if type(frame_times) is not list or len(frame_times) < 300:
        raise M0AcceptanceError(f"performance {role} artifact requires at least 300 frame times")
    normalized = [
        _finite_number(value, f"performance {role} frame time", positive=True)
        for value in frame_times
    ]
    ordered = sorted(normalized)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    return {
        "artifact_byte_size": artifact_size,
        "measurement_sha256": artifact_sha,
        "p95_frame_time_ms": p95,
        "sample_count": len(normalized),
    }


def validate_performance_receipt(
    receipt: dict[str, object],
    *,
    telemetry_capture_sha256: str,
    artifact_directory: Path,
) -> dict[str, object]:
    _exact_keys(receipt, _PERFORMANCE_KEYS, "performance receipt")
    if receipt["contract_version"] != PERFORMANCE_CONTRACT_VERSION:
        raise M0AcceptanceError("performance receipt contract is unsupported")
    scenario_id = _identifier(receipt["scenario_id"], "performance scenario_id")
    scenario_sha = _sha256(receipt["scenario_sha256"], "performance scenario SHA")
    scenario_filename = _artifact_filename(receipt["scenario_file"], "performance scenario_file")
    _, observed_scenario_sha, scenario_size = _read_json_file(
        artifact_directory / scenario_filename,
        "performance scenario artifact",
    )
    if observed_scenario_sha != scenario_sha:
        raise M0AcceptanceError("performance scenario artifact SHA does not match")
    if (
        _sha256(receipt["telemetry_capture_sha256"], "performance telemetry capture SHA")
        != telemetry_capture_sha256
    ):
        raise M0AcceptanceError("performance receipt is bound to another capture")
    measurement_tool = _identifier(receipt["measurement_tool"], "performance measurement_tool")
    threshold = _finite_number(receipt["max_p95_regression_pct"], "performance threshold")
    if threshold > MAXIMUM_P95_REGRESSION_PCT:
        raise M0AcceptanceError(
            f"performance threshold exceeds {MAXIMUM_P95_REGRESSION_PCT} percent"
        )
    values: dict[str, dict[str, object]] = {}
    artifact_filenames: list[str] = []
    for label in ("baseline", "sidecar"):
        series = _exact_keys(receipt[label], _PERFORMANCE_SERIES_KEYS, f"performance {label}")
        filename = _artifact_filename(series["artifact_file"], f"performance {label} artifact_file")
        artifact_filenames.append(filename.casefold())
        artifact_sha = _sha256(
            series["artifact_sha256"],
            f"performance {label} artifact SHA",
        )
        values[label] = _validate_performance_artifact(
            artifact_directory / filename,
            expected_sha256=artifact_sha,
            role=label,
            measurement_tool=measurement_tool,
            scenario_id=scenario_id,
            scenario_sha256=scenario_sha,
        )
    if len(set(artifact_filenames)) != 2:
        raise M0AcceptanceError("performance baseline and sidecar artifacts must differ")
    baseline = float(values["baseline"]["p95_frame_time_ms"])
    sidecar = float(values["sidecar"]["p95_frame_time_ms"])
    regression = (sidecar - baseline) / baseline * 100
    status = "PASS" if regression <= threshold else "FAIL"
    return {
        "baseline": values["baseline"],
        "max_p95_regression_pct": threshold,
        "measurement_tool": measurement_tool,
        "observed_p95_regression_pct": regression,
        "scenario_id": receipt["scenario_id"],
        "scenario_artifact_byte_size": scenario_size,
        "scenario_sha256": receipt["scenario_sha256"],
        "sidecar": values["sidecar"],
        "status": status,
    }


def accept_m0(
    capture_path: str | Path,
    *,
    launch_receipt_path: str | Path,
    install_manifest_path: str | Path,
    scenario: FuelScenario,
    expected_installer_sha256: str,
    expected_launcher_sha256: str,
    expected_requirements_sha256: str,
    expected_wheel_sha256: str,
    expected_wheelhouse_manifest_sha256: str,
    performance_receipt_path: str | Path | None = None,
    minimum_capture_s: float = MINIMUM_M0_CAPTURE_SECONDS,
    subprocess_timeout_s: float = 600.0,
) -> dict[str, object]:
    """Validate one live capture and return a deterministic acceptance receipt."""

    if not isinstance(scenario, FuelScenario):
        raise M0AcceptanceError("scenario must be a FuelScenario")
    minimum_capture_s = _finite_number(minimum_capture_s, "minimum_capture_s", positive=True)
    if minimum_capture_s < MINIMUM_M0_CAPTURE_SECONDS:
        raise M0AcceptanceError(f"minimum_capture_s cannot be below {MINIMUM_M0_CAPTURE_SECONDS}")
    subprocess_timeout_s = _finite_number(
        subprocess_timeout_s, "subprocess_timeout_s", positive=True
    )
    capture = Path(capture_path)
    launch_path = Path(launch_receipt_path)
    install_path = Path(install_manifest_path)
    launch, launch_sha, launch_size = _read_json_file(launch_path, "launch receipt")
    install, install_sha, install_size = _read_json_file(install_path, "install manifest")

    with _capture_snapshot(capture) as (snapshot, capture_sha, capture_size):
        with open_collector_jsonl(snapshot, require_receipt=True) as run:
            evidence = run.evidence
            # Force the second validation pass to finish before subprocesses
            # reopen the immutable snapshot.
            normalized_sample_count = sum(1 for _ in run.samples)
        if normalized_sample_count != evidence.frame_record_count:
            raise M0AcceptanceError("adapter sample count does not match evidence")

        launch_summary = _validate_launch(
            launch,
            capture_name=capture.name,
            capture_sha256=capture_sha,
            capture_size=capture_size,
            evidence=evidence,
            minimum_capture_s=minimum_capture_s,
        )
        install_summary = _validate_install(
            install,
            install_sha256=install_sha,
            install_filename=install_path.name,
            launch=launch,
            expected_installer_sha256=expected_installer_sha256,
            expected_launcher_sha256=expected_launcher_sha256,
            expected_requirements_sha256=expected_requirements_sha256,
            expected_wheel_sha256=expected_wheel_sha256,
            expected_wheelhouse_manifest_sha256=(expected_wheelhouse_manifest_sha256),
        )
        privacy = _scan_capture_privacy(snapshot)
        capture_quality = _capture_gate(evidence, minimum_capture_s=minimum_capture_s)
        stale_after = float(launch_summary["stale_after_seconds"])
        event_payload, event_stdout_sha, event_hash_probes = _double_replay(
            [
                "events",
                str(snapshot),
                "--input-kind",
                "collector",
                "--stale-after-seconds",
                str(stale_after),
            ],
            timeout_s=subprocess_timeout_s,
        )
        model_payload, model_stdout_sha, model_hash_probes = _double_replay(
            [
                "fuel-replay",
                str(snapshot),
                "--input-kind",
                "collector",
                "--stale-after-seconds",
                str(stale_after),
                *_scenario_arguments(scenario),
            ],
            timeout_s=subprocess_timeout_s,
        )
        event_validation = _validate_event_output(
            event_payload,
            evidence=evidence,
            stale_after_s=stale_after,
        )
        model_validation = _validate_model_output(
            model_payload,
            evidence=evidence,
            scenario=scenario,
            stale_after_s=stale_after,
        )
        shared_pipeline = _validate_shared_pipeline_outputs(
            event_payload,
            model_payload,
        )

        if performance_receipt_path is None:
            performance = {
                "reasons": ["EXTERNAL_FIXED_SCENARIO_AB_RECEIPT_MISSING"],
                "status": "WAIT_PERFORMANCE",
            }
            performance_receipt_sha = None
        else:
            raw_performance, performance_receipt_sha, _ = _read_json_file(
                Path(performance_receipt_path), "performance receipt"
            )
            performance = validate_performance_receipt(
                raw_performance,
                telemetry_capture_sha256=capture_sha,
                artifact_directory=Path(performance_receipt_path).parent,
            )

        core_reasons: list[str] = []
        if capture_quality["status"] != "PASS":
            core_reasons.extend(capture_quality["reasons"])
        if privacy["status"] != "PASS":
            core_reasons.append("PRIVACY_SCAN_FAILED")
        if event_validation["status"] != "PASS":
            core_reasons.append("EVENT_REPLAY_QUALITY_FAILED")
        if model_validation["traversal_status"] != "PASS":
            core_reasons.append("FUEL_MODEL_PIPELINE_TRAVERSAL_FAILED")
        if core_reasons:
            overall_status = "FAIL"
        elif model_validation["readiness_status"] == "FAIL":
            overall_status = "FAIL"
            core_reasons.append("FUEL_MODEL_QUALITY_FAILED")
        elif performance["status"] == "FAIL":
            overall_status = "FAIL_PERFORMANCE"
        elif (
            model_validation["readiness_status"] == "WAIT_DATA"
            and performance["status"] == "WAIT_PERFORMANCE"
        ):
            overall_status = "WAIT_DATA_AND_PERFORMANCE"
        elif model_validation["readiness_status"] == "WAIT_DATA":
            overall_status = "WAIT_DATA"
        elif performance["status"] == "WAIT_PERFORMANCE":
            overall_status = "WAIT_PERFORMANCE"
        else:
            overall_status = "PASS"

        overall_reasons = list(core_reasons)
        if model_validation["readiness_status"] == "WAIT_DATA":
            overall_reasons.extend(model_validation["quality_gate"]["reasons"])
        elif model_validation["readiness_status"] == "FAIL":
            overall_reasons.append("FUEL_MODEL_QUALITY_FAILED")
        if performance["status"] == "WAIT_PERFORMANCE":
            overall_reasons.extend(performance["reasons"])
        elif performance["status"] == "FAIL":
            overall_reasons.append("P95_FRAME_TIME_REGRESSION_EXCEEDED")

        binding: dict[str, Any] = {
            "advisor_only": True,
            "capture": {
                "byte_size": capture_size,
                "evidence": evidence.to_dict(),
                "filename": capture.name,
                "normalized_sample_count": normalized_sample_count,
                "sha256": capture_sha,
            },
            "capture_gate": capture_quality,
            "contract_version": M0_ACCEPTANCE_CONTRACT_VERSION,
            "acceptance_config": {
                "maximum_p95_regression_pct": MAXIMUM_P95_REGRESSION_PCT,
                "maximum_stale_seconds": MAXIMUM_M0_STALE_SECONDS,
                "minimum_capture_seconds": minimum_capture_s,
            },
            "event_replay": {
                **event_validation,
                "fresh_process_count": 2,
                "python_hash_probes": event_hash_probes,
                "python_hash_seeds": ["1", "987654"],
                "python_process_isolation": {
                    "environment_ignored": False,
                    "python_environment_sanitized": True,
                    "safe_path": True,
                    "user_site_disabled": True,
                },
                "stdout_sha256": event_stdout_sha,
            },
            "fuel_model_replay": {
                **model_validation,
                "fresh_process_count": 2,
                "python_hash_probes": model_hash_probes,
                "python_hash_seeds": ["1", "987654"],
                "python_process_isolation": {
                    "environment_ignored": False,
                    "python_environment_sanitized": True,
                    "safe_path": True,
                    "user_site_disabled": True,
                },
                "stdout_sha256": model_stdout_sha,
            },
            "install": install_summary,
            "install_manifest_byte_size": install_size,
            "launch": launch_summary,
            "launch_receipt_byte_size": launch_size,
            "launch_receipt_sha256": launch_sha,
            "overall_gate": {
                "reasons": list(dict.fromkeys(overall_reasons)),
                "status": overall_status,
            },
            "performance_gate": performance,
            "performance_receipt_sha256": performance_receipt_sha,
            "privacy_gate": privacy,
            "scenario": scenario.to_dict(),
            "shared_pipeline_gate": shared_pipeline,
        }
        return {**binding, "m0_receipt_sha256": _digest(binding)}
