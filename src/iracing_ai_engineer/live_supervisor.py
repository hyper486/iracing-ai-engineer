"""Single-process R8 live supervisor and protected-install admission boundary.

Production runs only from the fixed administrator-protected embedded runtime.
The runtime user has read/execute access but no write, delete, ownership, or
ACL authority over that tree.  PRECHECK_ONLY, canonical capture, and analysis
all consume caller-owned file handles in one isolated process; a capture path
is never reopened between those phases.

This contract closes disk-code/path TOCTOU and accidental or concurrent file
replacement.  It does *not* claim resistance to Administrator/SYSTEM or to
memory injection into the running user's own process.  Receipts therefore
remain ``SELF_CONSISTENT_NOT_AUTHENTICATED``.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import shutil
import stat
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, NoReturn

SUPERVISOR_CONTRACT_VERSION = "windows-live-supervisor-v1"
INSTALL_CONTRACT_VERSION = "windows-embedded-collector-install-v2"
RUNTIME_CONTRACT_VERSION = "windows-release-runtime-closure-v3"
CODE_TRUST_MODEL = "ADMIN_PROTECTED_READ_EXECUTE_V1"
ATTESTATION_STATUS = "SELF_CONSISTENT_NOT_AUTHENTICATED"
SECURITY_PROFILE = "SYSTEM_AND_BUILTIN_ADMIN_FULL_RUNTIME_USER_RX_V1"
LIVE_ANALYSIS_AUTHORITY_CONTRACT_VERSION = (
    "single-process-live-analysis-authority-v2"
)
EXECUTION_MODE = "PROTECTED_SINGLE_PROCESS_SAME_HANDLE_V1"
CAPTURE_SNAPSHOT_METHOD = "CALLER_OWNED_SINGLE_PROCESS_FILE_HANDLE_V1"
DEV_SMOKE_PROFILE_ID = "development-smoke-unbound-v1"
DEV_SMOKE_PROFILE_BYTE_SIZE = 662
DEV_SMOKE_PROFILE_SHA256 = (
    "7706d831001dfdd1256cbf4101caecbd9e2675028c80e0a0dd69e05ad8423a25"
)
DEV_SMOKE_PROFILE_FILE = "release-inputs/development-smoke-unbound-v1.json"
SUPERVISOR_TASK_ADMITTER_FILE = (
    "release-inputs\\admit_aeis_r8_supervisor_task_v5.ps1"
)
SUPERVISOR_TASK_INSTALLER_FILE = (
    "release-inputs\\install_aeis_r8_supervisor_task_v5.ps1"
)

FIXED_VERSION_ROOT = Path(
    r"C:\Program Files\AEIS\releases\collector-v4-0.1.0-r8"
)
FIXED_INSTALL_DIRECTORY = "collector-v4-0.1.0-r8"
FIXED_INSTALL_ROOT = r"C:\Program Files\AEIS\releases"
FIXED_STATE_ROOT = Path(r"C:\Users\racer\AppData\Local\AEIS\state\r8")
FIXED_RUNTIME_PYTHON = PurePosixPath("runtime/python.exe")
FIXED_INSTALL_MANIFEST = "install-manifest.json"
FIXED_RUNTIME_MANIFEST = "release-runtime-manifest.json"
FIXED_RUNTIME_USER_SID = "S-1-5-21-0-0-0-1001"
BUILTIN_ADMINISTRATORS_SID = "S-1-5-32-544"
LOCAL_SYSTEM_SID = "S-1-5-18"
TRUSTED_INSTALLER_SID = (
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
)
SECURITY_TREE_ALGORITHM = "SHA256_UTF8_CANONICAL_JSON_ASCII_PATHS_LF_V1"
SECURITY_PROFILE_CONTRACT_VERSION = "windows-admin-protected-runtime-acl-v1"
ANCESTOR_ADMISSION_CONTRACT_VERSION = "windows-code-ancestor-admission-v1"
THREAT_BOUNDARY_CONTRACT_VERSION = "windows-code-trust-threat-boundary-v1"
TOKEN_ADMISSION_CONTRACT_VERSION = "windows-runtime-token-admission-v1"
DIRECTORY_SDDL = (
    "O:BAD:P(A;OICI;FA;;;SY)"
    "(A;OICI;0x1200a9;;;S-1-5-21-0-0-0-1001)"
    "(A;OICI;FA;;;BA)"
)
FILE_SDDL = (
    "O:BAD:P(A;;FA;;;SY)"
    "(A;;0x1200a9;;;S-1-5-21-0-0-0-1001)"
    "(A;;FA;;;BA)"
)

_RUNTIME_MANIFEST_MAX_BYTES = 16 * 1024 * 1024
_INSTALL_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
_PROTECTED_FILE_MAX_BYTES = 512 * 1024 * 1024
_MAX_RUNTIME_FILE_COUNT = 20_000
_MAX_RUNTIME_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_DOTNET_TICKS_AT_WINDOWS_EPOCH = 504_911_232_000_000_000
_SIMULATOR_EXECUTABLE = "iRacingSim64DX11.exe"
_SUPERVISOR_MUTEX_NAME = r"Local\AEIS-R8-Live-Supervisor-v1"
_SOURCE_ID = "aeis-r8-windows-sdk-live"
_PREFLIGHT_DURATION_S = 30.0
_CAPTURE_DURATION_S = 1800.0
_SDK_WAIT_SECONDS = 120.0
_POLL_SECONDS = 0.01
_STALE_AFTER_S = 0.5
_CAPTURE_HARD_MAX_BYTES = 8 * 1024**3
_PREFLIGHT_RESIDUE_ALLOWANCE_BYTES = 256 * 1024**2
_STATE_FREE_SPACE_RESERVE_BYTES = 4 * 1024**3
_DANGEROUS_CODE_RIGHTS = (
    0x00000002  # FILE_WRITE_DATA
    | 0x00000004  # FILE_APPEND_DATA
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_WINDOWS_RESERVED = {
    "aux",
    "clock$",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "con",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
    "nul",
    "prn",
}

# V2 is intentionally not backward-compatible.  The final key set is shared
# with the elevated installer and is exact: additions require a new contract.
_INSTALL_V2_KEYS = frozenset(
    {
        "advisor_only",
        "ancestor_admission",
        "code_owner_sid",
        "code_root",
        "code_trust_model",
        "contract_version",
        "dacl_protected",
        "development_smoke_profile_byte_size",
        "development_smoke_profile_file",
        "development_smoke_profile_sha256",
        "embedded_python_file",
        "embedded_python_sha256",
        "import_smoke",
        "install_directory",
        "install_root",
        "installer_identity_admission",
        "live_capture_validated",
        "package_version",
        "project_wheel_file",
        "project_wheel_sha256",
        "python_architecture",
        "python_bits",
        "python_path_config_file",
        "python_path_config_sha256",
        "python_version",
        "record_closure",
        "release_manifest_file",
        "release_manifest_sha256",
        "runtime_closure",
        "runtime_file_count",
        "runtime_manifest_file",
        "runtime_manifest_primitives_file",
        "runtime_manifest_primitives_sha256",
        "runtime_manifest_self_sha256",
        "runtime_manifest_sha256",
        "runtime_manifest_tool_file",
        "runtime_manifest_tool_sha256",
        "runtime_python_file",
        "runtime_total_bytes",
        "runtime_tree_sha256",
        "runtime_user_sid",
        "security_descriptor_profile",
        "security_object_count",
        "security_profile",
        "security_tree_algorithm",
        "security_tree_sha256",
        "supervisor_task_admitter_file",
        "supervisor_task_admitter_sha256",
        "supervisor_task_installer_file",
        "supervisor_task_installer_sha256",
        "target",
        "threat_boundary",
        "threat_model",
        "version_directory",
        "wheel_install_receipt_file",
        "wheel_install_receipt_sha256",
        "wheel_installer_tool_file",
        "wheel_installer_tool_sha256",
        "wheelhouse_manifest_file",
        "wheelhouse_manifest_sha256",
        "writable_state_root",
    }
)
_RUNTIME_MANIFEST_KEYS = frozenset(
    {
        "contract_version",
        "embedded_distribution",
        "file_count",
        "files",
        "generator_binding",
        "layout",
        "manifest_self_sha256",
        "project_version",
        "total_bytes",
        "tree_sha256",
    }
)
_RUNTIME_FILE_KEYS = frozenset({"path", "role", "sha256", "size"})
_RUNTIME_EMBED_KEYS = frozenset(
    {
        "archive_member_count",
        "archive_member_tree_sha256",
        "archive_name",
        "archive_sha256",
        "archive_size",
        "original_python_path_config_sha256",
        "original_uncompressed_bytes",
        "python_version",
        "security_scope",
        "target",
    }
)
_RUNTIME_GENERATOR_KEYS = frozenset(
    {
        "base_primitives_file",
        "base_primitives_sha256",
        "binding_role",
        "generator_file",
        "generator_sha256",
    }
)
_RUNTIME_LAYOUT_KEYS = frozenset(
    {
        "irsdk_module",
        "project_dist_info_directory",
        "project_package_directory",
        "project_record",
        "project_wheel",
        "python_core_dll",
        "python_executable",
        "python_path_config",
        "runtime_directory",
        "site_packages_directory",
        "stdlib_archive",
        "wheel_data_file",
    }
)
_RUNTIME_FILE_ROLES = frozenset(
    {
        "embedded_runtime_file",
        "irsdk_module",
        "project_package_file",
        "project_record",
        "project_wheel",
        "python_core_dll",
        "python_executable",
        "python_path_config",
        "site_packages_file",
        "stdlib_archive",
        "wheel_data_file",
    }
)
_LIVE_ANALYSIS_AUTHORITY_KEYS = frozenset(
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
        "runtime_tree_sha256",
        "runtime_token_admission",
        "security_descriptor_profile",
        "security_tree_object_count",
        "security_tree_sha256",
        "sim_process_id",
        "sim_start_time_utc_ticks",
        "supervisor_contract_version",
        "windows_session_id",
    }
)


class LiveSupervisorError(ValueError):
    """Fail-closed supervisor admission or lifecycle failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise LiveSupervisorError(code, message)


def _canonical_json(value: object, *, newline: bool = False) -> bytes:
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise LiveSupervisorError(
            "CANONICAL_JSON_FAILED", "value is not canonical-JSON-safe"
        ) from exc
    return payload + (b"\n" if newline else b"")


def _strict_json_bytes(payload: bytes, label: str) -> dict[str, object]:
    if not payload or payload.startswith(b"\xef\xbb\xbf"):
        _fail("JSON_INVALID", f"{label} is empty or has a UTF-8 BOM")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        folded: set[str] = set()
        for key, value in pairs:
            if type(key) is not str or key in result or key.casefold() in folded:
                raise ValueError(f"duplicate/case-colliding JSON key: {key!r}")
            result[key] = value
            folded.add(key.casefold())
        return result

    def reject_constant(value: str) -> NoReturn:
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LiveSupervisorError("JSON_INVALID", f"{label} is not strict JSON") from exc
    if type(value) is not dict:
        _fail("JSON_SCHEMA_INVALID", f"{label} must be a JSON object")
    return value


def _exact_object(
    value: object, keys: frozenset[str], label: str
) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail("SCHEMA_INVALID", f"{label} has unexpected or missing fields")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail("SCHEMA_INVALID", f"{label} must be a lowercase SHA-256")
    return value


def _plain_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail("SCHEMA_INVALID", f"{label} must be an integer >= {minimum}")
    return value


def _safe_relative_path(value: object, label: str) -> str:
    if type(value) is not str or not value or "\\" in value:
        _fail("PATH_INVALID", f"{label} must use canonical forward slashes")
    pure = PurePosixPath(value)
    if pure.is_absolute() or str(pure) != value or not pure.parts:
        _fail("PATH_INVALID", f"{label} is not canonical relative form")
    for part in pure.parts:
        stem = part.split(".", 1)[0].rstrip(" .").casefold()
        if (
            part in {"", ".", ".."}
            or part.endswith((" ", "."))
            or ":" in part
            or any(ord(character) < 32 for character in part)
            or any(character in _WINDOWS_FORBIDDEN for character in part)
            or stem in _WINDOWS_RESERVED
        ):
            _fail("PATH_INVALID", f"{label} is unsafe on Windows")
    drive, _ = ntpath.splitdrive(value.replace("/", "\\"))
    if drive or ntpath.isabs(value.replace("/", "\\")):
        _fail("PATH_INVALID", f"{label} is absolute on Windows")
    return value


def _hash_handle(handle: BinaryIO) -> tuple[int, str]:
    handle.flush()
    before = os.fstat(handle.fileno())
    if not stat.S_ISREG(before.st_mode):
        _fail("HANDLE_INVALID", "held object is not a regular file")
    handle.seek(0)
    digest = hashlib.sha256()
    total = 0
    while chunk := handle.read(1024 * 1024):
        if type(chunk) is not bytes:
            _fail("HANDLE_INVALID", "held file is not open in binary mode")
        digest.update(chunk)
        total += len(chunk)
    after = os.fstat(handle.fileno())
    identity_before = (before.st_dev, before.st_ino, before.st_size)
    identity_after = (after.st_dev, after.st_ino, after.st_size)
    if identity_before != identity_after or total != before.st_size:
        _fail("HANDLE_CHANGED", "held file changed while hashing")
    return total, digest.hexdigest()


@dataclass(frozen=True, slots=True)
class SecurityAce:
    sid: str
    rights_mask: int
    inheritance_flags: int
    propagation_flags: int = 0
    inherited: bool = False
    access_type: str = "ALLOW"

    def to_dict(self) -> dict[str, object]:
        return {
            "sid": self.sid,
            "access_type": self.access_type,
            "rights_mask": self.rights_mask,
            "inheritance_flags": self.inheritance_flags,
            "propagation_flags": self.propagation_flags,
            "inherited": self.inherited,
        }


@dataclass(frozen=True, slots=True)
class SecurityObject:
    path: str
    object_type: str
    owner_sid: str
    dacl_protected: bool
    aces: tuple[SecurityAce, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "object_type": self.object_type,
            "owner_sid": self.owner_sid,
            "dacl_protected": self.dacl_protected,
            "aces": [ace.to_dict() for ace in self.aces],
        }


def _expected_aces(object_type: str) -> tuple[SecurityAce, ...]:
    # The installer includes Synchronize in the runtime user's RX mask.
    flags = 0x03 if object_type == "DIRECTORY" else 0
    return (
        SecurityAce(LOCAL_SYSTEM_SID, 0x1F01FF, flags),
        SecurityAce(FIXED_RUNTIME_USER_SID, 0x1200A9, flags),
        SecurityAce(BUILTIN_ADMINISTRATORS_SID, 0x1F01FF, flags),
    )


def _canonical_security_aces(
    aces: Sequence[SecurityAce],
) -> tuple[SecurityAce, ...]:
    """Match the installer's semantic ACL ordering, independent of DACL order."""

    return tuple(
        sorted(
            aces,
            key=lambda ace: (
                ace.sid,
                ace.access_type,
                ace.rights_mask,
                ace.inheritance_flags,
                ace.propagation_flags,
                ace.inherited,
            ),
        )
    )


def _validate_security_tree(
    objects: Sequence[SecurityObject],
    *,
    expected_count: int,
    expected_sha256: str,
) -> tuple[dict[str, object], ...]:
    _sha256(expected_sha256, "security tree SHA-256")
    if type(expected_count) is not int or expected_count < 1:
        _fail("SECURITY_TREE_INVALID", "security tree count is invalid")
    canonical = tuple(
        sorted(
            (item.to_dict() for item in objects),
            key=lambda item: str(item["path"]).upper(),
        )
    )
    if len(canonical) != expected_count:
        _fail("SECURITY_TREE_INVALID", "security tree object count differs")
    _validate_security_tree_profile(canonical)
    observed_sha = hashlib.sha256(_security_tree_bytes(canonical)).hexdigest()
    if observed_sha != expected_sha256:
        _fail("SECURITY_TREE_INVALID", "security tree hash differs")
    return canonical


def _validate_security_tree_profile(
    canonical: Sequence[Mapping[str, object]],
) -> None:
    """Validate the fixed owner/DACL profile without trusting a receipt hash."""

    paths = [str(item["path"]).casefold() for item in canonical]
    if len(paths) != len(set(paths)) or not canonical or canonical[0]["path"] != ".":
        _fail("SECURITY_TREE_INVALID", "security tree paths are not exact")
    for raw in canonical:
        item = _exact_object(
            raw,
            frozenset({"path", "object_type", "owner_sid", "dacl_protected", "aces"}),
            "observed security object",
        )
        path = item["path"]
        if type(path) is not str:
            _fail("SECURITY_TREE_INVALID", "security tree path is not text")
        if path != ".":
            _safe_relative_path(path, f"security object {path}")
        try:
            path.encode("ascii")
        except UnicodeEncodeError:
            _fail("SECURITY_TREE_INVALID", "security tree path is not ASCII")
        object_type = item["object_type"]
        if object_type not in {"DIRECTORY", "FILE"}:
            _fail("SECURITY_TREE_INVALID", f"unsupported object kind: {path}")
        if (
            item["owner_sid"] != BUILTIN_ADMINISTRATORS_SID
            or item["dacl_protected"] is not True
        ):
            _fail("SECURITY_TREE_INVALID", f"owner/protection differs: {path}")
        expected_aces = [ace.to_dict() for ace in _expected_aces(str(object_type))]
        if item["aces"] != expected_aces:
            _fail("SECURITY_TREE_INVALID", f"DACL differs: {path}")


def _security_tree_bytes(objects: Sequence[Mapping[str, object]]) -> bytes:
    try:
        return (
            json.dumps(
                list(objects),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=False,
            )
            + "\n"
        ).encode("ascii")
    except (OverflowError, UnicodeError, TypeError, ValueError) as exc:
        raise LiveSupervisorError(
            "SECURITY_TREE_INVALID", "security tree is not canonical ASCII JSON"
        ) from exc


def _windows_sid_string(sid_pointer: int) -> str:
    import ctypes
    from ctypes import wintypes

    convert = ctypes.windll.advapi32.ConvertSidToStringSidW
    convert.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID))
    convert.restype = wintypes.BOOL
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = (wintypes.HLOCAL,)
    local_free.restype = wintypes.HLOCAL
    text_pointer = wintypes.LPVOID()
    if not convert(sid_pointer, ctypes.byref(text_pointer)):
        _fail("SECURITY_OBSERVATION_FAILED", "cannot render Windows SID")
    try:
        return ctypes.wstring_at(text_pointer.value)
    finally:
        local_free(text_pointer)


def _observe_windows_security_object(
    path: Path,
    *,
    relative_path: str,
    object_type: str,
) -> SecurityObject:
    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "ACL observation is Windows-only")
    import ctypes
    from ctypes import wintypes

    get_named = ctypes.windll.advapi32.GetNamedSecurityInfoW
    get_named.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    )
    get_named.restype = wintypes.DWORD
    get_control = ctypes.windll.advapi32.GetSecurityDescriptorControl
    get_control.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    get_control.restype = wintypes.BOOL
    get_acl_information = ctypes.windll.advapi32.GetAclInformation
    get_acl_information.argtypes = (
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_acl_information.restype = wintypes.BOOL
    get_ace = ctypes.windll.advapi32.GetAce
    get_ace.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    )
    get_ace.restype = wintypes.BOOL
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = (wintypes.HLOCAL,)
    local_free.restype = wintypes.HLOCAL

    class AclSizeInformation(ctypes.Structure):
        _fields_ = (
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        )

    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    error = get_named(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000001 | 0x00000004,  # OWNER | DACL
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if error != 0 or not descriptor.value or not owner.value or not dacl.value:
        _fail(
            "SECURITY_OBSERVATION_FAILED",
            f"GetNamedSecurityInfoW failed for {relative_path}: {error}",
        )
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not get_control(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            _fail(
                "SECURITY_OBSERVATION_FAILED",
                f"cannot read security descriptor control: {relative_path}",
            )
        size = AclSizeInformation()
        if not get_acl_information(
            dacl, ctypes.byref(size), ctypes.sizeof(size), 2
        ):
            _fail(
                "SECURITY_OBSERVATION_FAILED",
                f"cannot read DACL information: {relative_path}",
            )
        aces: list[SecurityAce] = []
        for index in range(int(size.ace_count)):
            pointer = wintypes.LPVOID()
            if not get_ace(dacl, index, ctypes.byref(pointer)) or not pointer.value:
                _fail(
                    "SECURITY_OBSERVATION_FAILED",
                    f"cannot read DACL ACE: {relative_path}",
                )
            address = int(pointer.value)
            ace_type = ctypes.c_ubyte.from_address(address).value
            ace_flags = ctypes.c_ubyte.from_address(address + 1).value
            if ace_type != 0:  # ACCESS_ALLOWED_ACE_TYPE
                _fail(
                    "SECURITY_TREE_INVALID",
                    f"non-ALLOW ACE observed: {relative_path}",
                )
            mask = ctypes.c_uint32.from_address(address + 4).value
            aces.append(
                SecurityAce(
                    sid=_windows_sid_string(address + 8),
                    rights_mask=int(mask),
                    inheritance_flags=int(ace_flags & 0x03),
                    propagation_flags=int(ace_flags & 0x0C),
                    inherited=bool(ace_flags & 0x10),
                )
            )
        return SecurityObject(
            path=relative_path,
            object_type=object_type,
            owner_sid=_windows_sid_string(int(owner.value)),
            dacl_protected=bool(int(control.value) & 0x1000),
            aces=_canonical_security_aces(aces),
        )
    finally:
        local_free(descriptor)


def _observe_windows_security_tree(version_root: Path) -> tuple[SecurityObject, ...]:
    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "security tree observation is Windows-only")
    root = Path(os.path.abspath(version_root))
    if os.path.normcase(str(root)) != os.path.normcase(str(FIXED_VERSION_ROOT)):
        _fail("CODE_ROOT_INVALID", "security tree root is not the fixed release")
    pending: list[tuple[Path, str]] = [(root, ".")]
    records: list[SecurityObject] = []
    seen: set[str] = set()
    while pending:
        current, relative = pending.pop()
        metadata = os.lstat(current)
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x400:
            _fail("SECURITY_TREE_INVALID", f"reparse point observed: {relative}")
        if relative != ".":
            _safe_relative_path(relative, f"security tree {relative}")
        try:
            relative.encode("ascii")
        except UnicodeEncodeError:
            _fail("SECURITY_TREE_INVALID", "security tree path is not ASCII")
        folded = relative.upper()
        if folded in seen:
            _fail("SECURITY_TREE_INVALID", "security tree paths collide")
        seen.add(folded)
        if stat.S_ISDIR(metadata.st_mode):
            object_type = "DIRECTORY"
            try:
                entries = list(os.scandir(current))
            except OSError as exc:
                raise LiveSupervisorError(
                    "SECURITY_OBSERVATION_FAILED",
                    f"cannot enumerate protected directory: {relative}",
                ) from exc
            for entry in entries:
                child_relative = (
                    entry.name if relative == "." else f"{relative}/{entry.name}"
                )
                pending.append((Path(entry.path), child_relative))
        elif stat.S_ISREG(metadata.st_mode):
            object_type = "FILE"
        else:
            _fail("SECURITY_TREE_INVALID", f"unsupported protected object: {relative}")
        records.append(
            _observe_windows_security_object(
                current,
                relative_path=relative,
                object_type=object_type,
            )
        )
    return tuple(sorted(records, key=lambda record: record.path.upper()))


def _windows_effective_rights(path: Path, sid_text: str) -> tuple[str, bool, int]:
    """Return owner, DACL-protected bit, and one SID's effective access mask."""

    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "effective-rights observation is Windows-only")
    import ctypes
    from ctypes import wintypes

    get_named = ctypes.windll.advapi32.GetNamedSecurityInfoW
    get_named.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    )
    get_named.restype = wintypes.DWORD
    get_control = ctypes.windll.advapi32.GetSecurityDescriptorControl
    get_control.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    get_control.restype = wintypes.BOOL
    convert_sid = ctypes.windll.advapi32.ConvertStringSidToSidW
    convert_sid.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID))
    convert_sid.restype = wintypes.BOOL

    class TrusteeW(ctypes.Structure):
        _fields_ = (
            ("multiple_trustee", wintypes.LPVOID),
            ("multiple_trustee_operation", wintypes.DWORD),
            ("trustee_form", wintypes.DWORD),
            ("trustee_type", wintypes.DWORD),
            # TRUSTEE_IS_SID stores a raw PSID in this nominal LPTSTR field.
            ("name", wintypes.LPVOID),
        )

    get_effective = ctypes.windll.advapi32.GetEffectiveRightsFromAclW
    get_effective.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(TrusteeW),
        ctypes.POINTER(wintypes.DWORD),
    )
    get_effective.restype = wintypes.DWORD
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = (wintypes.HLOCAL,)
    local_free.restype = wintypes.HLOCAL

    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    error = get_named(
        str(path),
        1,
        0x00000001 | 0x00000004,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if error or not owner.value or not dacl.value or not descriptor.value:
        _fail("ANCESTOR_ADMISSION_FAILED", f"cannot inspect ancestor ACL: {path}")
    sid = wintypes.LPVOID()
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            _fail("ANCESTOR_ADMISSION_FAILED", f"cannot inspect ancestor flags: {path}")
        if not convert_sid(sid_text, ctypes.byref(sid)) or not sid.value:
            _fail("ANCESTOR_ADMISSION_FAILED", "cannot materialize runtime SID")
        trustee = TrusteeW(
            None,
            0,  # NO_MULTIPLE_TRUSTEE
            0,  # TRUSTEE_IS_SID
            0,  # TRUSTEE_IS_UNKNOWN
            sid,
        )
        rights = wintypes.DWORD()
        error = get_effective(dacl, ctypes.byref(trustee), ctypes.byref(rights))
        if error:
            _fail(
                "ANCESTOR_ADMISSION_FAILED",
                f"cannot compute runtime rights for ancestor: {path}",
            )
        return (
            _windows_sid_string(int(owner.value)),
            bool(int(control.value) & 0x1000),
            int(rights.value),
        )
    finally:
        if sid.value:
            local_free(sid)
        local_free(descriptor)


def _windows_raw_dacl(path: Path) -> tuple[tuple[int, int, int, str], ...]:
    """Read raw ACE type/flags/mask/SID tuples for ancestor policy checks."""

    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "DACL observation is Windows-only")
    import ctypes
    from ctypes import wintypes

    get_named = ctypes.windll.advapi32.GetNamedSecurityInfoW
    get_named.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    )
    get_named.restype = wintypes.DWORD
    get_acl_information = ctypes.windll.advapi32.GetAclInformation
    get_acl_information.argtypes = (
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_acl_information.restype = wintypes.BOOL
    get_ace = ctypes.windll.advapi32.GetAce
    get_ace.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    )
    get_ace.restype = wintypes.BOOL
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = (wintypes.HLOCAL,)
    local_free.restype = wintypes.HLOCAL

    class AclSizeInformation(ctypes.Structure):
        _fields_ = (
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        )

    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    error = get_named(
        str(path), 1, 0x00000004, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
    )
    if error or not dacl.value or not descriptor.value:
        _fail("ANCESTOR_ADMISSION_FAILED", f"cannot inspect ancestor DACL: {path}")
    try:
        size = AclSizeInformation()
        if not get_acl_information(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            _fail("ANCESTOR_ADMISSION_FAILED", f"cannot size ancestor DACL: {path}")
        result: list[tuple[int, int, int, str]] = []
        for index in range(int(size.ace_count)):
            pointer = wintypes.LPVOID()
            if not get_ace(dacl, index, ctypes.byref(pointer)) or not pointer.value:
                _fail("ANCESTOR_ADMISSION_FAILED", f"cannot read ancestor ACE: {path}")
            address = int(pointer.value)
            ace_type = ctypes.c_ubyte.from_address(address).value
            ace_flags = ctypes.c_ubyte.from_address(address + 1).value
            # Standard ACCESS_ALLOWED/DENIED ACEs share mask/SID offsets.  Any
            # object/callback ALLOW form is rejected by the caller rather than
            # guessed because its SID offset is not fixed at +8.
            if ace_type not in {0, 1}:
                result.append((ace_type, ace_flags, 0xFFFFFFFF, "UNSUPPORTED"))
                continue
            mask = ctypes.c_uint32.from_address(address + 4).value
            result.append(
                (ace_type, ace_flags, int(mask), _windows_sid_string(address + 8))
            )
        return tuple(result)
    finally:
        local_free(descriptor)


def _validate_program_files_dacl(
    entries: Sequence[tuple[int, int, int, str]],
) -> None:
    privileged = {
        LOCAL_SYSTEM_SID,
        BUILTIN_ADMINISTRATORS_SID,
        TRUSTED_INSTALLER_SID,
    }
    creator_owner = "S-1-3-0"
    for ace_type, flags, mask, sid in entries:
        if ace_type == 1:  # ACCESS_DENIED never grants danger
            continue
        if ace_type != 0:
            _fail(
                "ANCESTOR_ADMISSION_FAILED",
                "Program Files has an unsupported granting ACE form",
            )
        if sid in privileged or not mask & _DANGEROUS_CODE_RIGHTS:
            continue
        if sid == creator_owner and flags == 0x0B:
            continue
        _fail(
            "ANCESTOR_ADMISSION_FAILED",
            "Program Files grants dangerous rights to an unapproved principal",
        )


def _plain_windows_directory_identity(path: Path, label: str) -> tuple[int, int]:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise LiveSupervisorError(
            "DIRECTORY_ADMISSION_FAILED", f"cannot inspect {label}: {path}"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or int(getattr(metadata, "st_file_attributes", 0)) & 0x400
    ):
        _fail("DIRECTORY_ADMISSION_FAILED", f"{label} is not a plain directory")
    return int(metadata.st_dev), int(metadata.st_ino)


def _assert_plain_windows_directory(path: Path, label: str) -> None:
    _plain_windows_directory_identity(path, label)


def _observe_and_validate_windows_ancestors() -> None:
    """Independently re-admit the fixed protected code-root ancestor chain."""

    paths = (
        Path(r"C:\Program Files"),
        Path(r"C:\Program Files\AEIS"),
        Path(r"C:\Program Files\AEIS\releases"),
        FIXED_VERSION_ROOT,
    )
    expected_owners = (
        TRUSTED_INSTALLER_SID,
        BUILTIN_ADMINISTRATORS_SID,
        BUILTIN_ADMINISTRATORS_SID,
        BUILTIN_ADMINISTRATORS_SID,
    )
    for index, (path, expected_owner) in enumerate(
        zip(paths, expected_owners, strict=True)
    ):
        _assert_plain_windows_directory(path, f"code ancestor {index}")
        owner, protected, _rights = _windows_effective_rights(
            path, FIXED_RUNTIME_USER_SID
        )
        if owner != expected_owner or not protected:
            _fail(
                "ANCESTOR_ADMISSION_FAILED",
                f"protected code ancestor differs: {path}",
            )
        if index == 0:
            _validate_program_files_dacl(_windows_raw_dacl(path))
        if index:
            observed = _observe_windows_security_object(
                path,
                relative_path=".",
                object_type="DIRECTORY",
            )
            if (
                observed.owner_sid != BUILTIN_ADMINISTRATORS_SID
                or not observed.dacl_protected
                or observed.aces != _expected_aces("DIRECTORY")
            ):
                _fail(
                    "ANCESTOR_ADMISSION_FAILED",
                    f"protected code ancestor profile differs: {path}",
                )


def _windows_current_user_sid() -> str:
    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "token identity observation is Windows-only")
    import ctypes
    from ctypes import wintypes

    open_token = ctypes.windll.advapi32.OpenProcessToken
    open_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    open_token.restype = wintypes.BOOL
    get_info = ctypes.windll.advapi32.GetTokenInformation
    get_info.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_info.restype = wintypes.BOOL
    close = ctypes.windll.kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not open_token(ctypes.windll.kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        _fail("RUNTIME_IDENTITY_INVALID", "cannot open current process token")
    try:
        required = wintypes.DWORD()
        get_info(token, 1, None, 0, ctypes.byref(required))  # TokenUser
        if required.value < ctypes.sizeof(wintypes.LPVOID):
            _fail("RUNTIME_IDENTITY_INVALID", "cannot size current token identity")
        buffer = ctypes.create_string_buffer(required.value)
        if not get_info(token, 1, buffer, required, ctypes.byref(required)):
            _fail("RUNTIME_IDENTITY_INVALID", "cannot read current token identity")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        return _windows_sid_string(int(sid_pointer))
    finally:
        close(token)


@dataclass(frozen=True, slots=True)
class RuntimeTokenAdmission:
    current_user_sid: str
    token_is_elevated: bool
    token_elevation_type: str
    administrators_sid_enabled: bool
    integrity_level_rid: int
    least_privilege: str
    contract_version: str = TOKEN_ADMISSION_CONTRACT_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "administrators_sid_enabled": self.administrators_sid_enabled,
            "contract_version": self.contract_version,
            "current_user_sid": self.current_user_sid,
            "integrity_level_rid": self.integrity_level_rid,
            "least_privilege": self.least_privilege,
            "token_elevation_type": self.token_elevation_type,
            "token_is_elevated": self.token_is_elevated,
        }


def _observe_windows_runtime_token() -> RuntimeTokenAdmission:
    """Observe the current Windows token without turning it into admission."""

    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "runtime token admission is Windows-only")
    import ctypes
    from ctypes import wintypes

    open_token = ctypes.windll.advapi32.OpenProcessToken
    open_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    open_token.restype = wintypes.BOOL
    get_info = ctypes.windll.advapi32.GetTokenInformation
    get_info.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_info.restype = wintypes.BOOL
    convert_sid = ctypes.windll.advapi32.ConvertStringSidToSidW
    convert_sid.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID))
    convert_sid.restype = wintypes.BOOL
    check_membership = ctypes.windll.advapi32.CheckTokenMembership
    check_membership.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
    )
    check_membership.restype = wintypes.BOOL
    get_count = ctypes.windll.advapi32.GetSidSubAuthorityCount
    get_count.argtypes = (wintypes.LPVOID,)
    get_count.restype = ctypes.POINTER(ctypes.c_ubyte)
    get_subauthority = ctypes.windll.advapi32.GetSidSubAuthority
    get_subauthority.argtypes = (wintypes.LPVOID, wintypes.DWORD)
    get_subauthority.restype = ctypes.POINTER(wintypes.DWORD)
    close = ctypes.windll.kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = (wintypes.HLOCAL,)
    local_free.restype = wintypes.HLOCAL

    token = wintypes.HANDLE()
    if not open_token(
        ctypes.windll.kernel32.GetCurrentProcess(),
        0x0008,  # TOKEN_QUERY
        ctypes.byref(token),
    ):
        _fail("TOKEN_ADMISSION_FAILED", "cannot open runtime process token")
    admin_sid = wintypes.LPVOID()
    try:
        elevation = wintypes.DWORD()
        returned = wintypes.DWORD()
        if not get_info(
            token,
            20,  # TokenElevation
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(returned),
        ):
            _fail("TOKEN_ADMISSION_FAILED", "cannot read token elevation")
        elevation_type = wintypes.DWORD()
        if not get_info(
            token,
            18,  # TokenElevationType
            ctypes.byref(elevation_type),
            ctypes.sizeof(elevation_type),
            ctypes.byref(returned),
        ):
            _fail("TOKEN_ADMISSION_FAILED", "cannot read token elevation type")
        if not convert_sid(BUILTIN_ADMINISTRATORS_SID, ctypes.byref(admin_sid)):
            _fail("TOKEN_ADMISSION_FAILED", "cannot materialize Administrators SID")
        member = wintypes.BOOL()
        # NULL asks Windows to check the effective token.  Passing a process
        # primary token directly is invalid for CheckTokenMembership.
        if not check_membership(None, admin_sid, ctypes.byref(member)):
            _fail("TOKEN_ADMISSION_FAILED", "cannot check Administrators membership")

        required = wintypes.DWORD()
        get_info(token, 25, None, 0, ctypes.byref(required))  # TokenIntegrityLevel
        if required.value < ctypes.sizeof(wintypes.LPVOID):
            _fail("TOKEN_ADMISSION_FAILED", "cannot size token integrity label")
        buffer = ctypes.create_string_buffer(required.value)
        if not get_info(token, 25, buffer, required, ctypes.byref(returned)):
            _fail("TOKEN_ADMISSION_FAILED", "cannot read token integrity label")
        integrity_sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        count_pointer = get_count(integrity_sid)
        if not count_pointer:
            _fail("TOKEN_ADMISSION_FAILED", "integrity SID has no subauthorities")
        count = int(count_pointer[0])
        if count < 1:
            _fail("TOKEN_ADMISSION_FAILED", "integrity SID is malformed")
        rid_pointer = get_subauthority(integrity_sid, count - 1)
        if not rid_pointer:
            _fail("TOKEN_ADMISSION_FAILED", "integrity RID is unavailable")
        integrity_rid = int(rid_pointer[0])
        current_sid = _windows_current_user_sid()
        is_elevated = bool(elevation.value)
        elevation_text = (
            "LIMITED" if int(elevation_type.value) == 3 else "NOT_LIMITED"
        )
        admin_enabled = bool(member.value)
        admission = RuntimeTokenAdmission(
            current_user_sid=current_sid,
            token_is_elevated=is_elevated,
            token_elevation_type=elevation_text,
            administrators_sid_enabled=admin_enabled,
            integrity_level_rid=integrity_rid,
            least_privilege=(
                "PASS"
                if current_sid == FIXED_RUNTIME_USER_SID
                and not is_elevated
                and elevation_text == "LIMITED"
                and not admin_enabled
                and integrity_rid == 0x2000
                else "BLOCKED"
            ),
        )
        return admission
    finally:
        if admin_sid.value:
            local_free(admin_sid)
        close(token)


def _admit_windows_runtime_token() -> RuntimeTokenAdmission:
    """Require the Scheduled Task's non-elevated, medium-integrity token."""

    admission = _observe_windows_runtime_token()
    _assert_runtime_token_admission(admission)
    return admission


def _assert_runtime_token_admission(admission: RuntimeTokenAdmission) -> None:
    """Reassert the exact token gate before any runtime-user state mutation."""

    expected = RuntimeTokenAdmission(
        current_user_sid=FIXED_RUNTIME_USER_SID,
        token_is_elevated=False,
        token_elevation_type="LIMITED",
        administrators_sid_enabled=False,
        integrity_level_rid=0x2000,
        least_privilege="PASS",
    )
    if admission != expected:
        _fail(
            "TOKEN_ADMISSION_FAILED",
            "runtime token is not the fixed least-privilege medium token",
        )


def _validate_runtime_manifest(
    manifest_bytes: bytes,
    *,
    expected_sha256: str,
) -> dict[str, object]:
    if hashlib.sha256(manifest_bytes).hexdigest() != _sha256(
        expected_sha256, "runtime manifest SHA-256"
    ):
        _fail("RUNTIME_MANIFEST_MISMATCH", "runtime manifest bytes differ")
    manifest = _exact_object(
        _strict_json_bytes(manifest_bytes, "runtime manifest"),
        _RUNTIME_MANIFEST_KEYS,
        "runtime manifest",
    )
    if manifest.get("contract_version") != RUNTIME_CONTRACT_VERSION:
        _fail("RUNTIME_MANIFEST_INVALID", "runtime manifest contract differs")
    if manifest.get("project_version") != "0.1.0":
        _fail("RUNTIME_MANIFEST_INVALID", "runtime project version differs")
    embedded = _exact_object(
        manifest.get("embedded_distribution"),
        _RUNTIME_EMBED_KEYS,
        "embedded distribution",
    )
    embedded_exact = {
        "archive_member_count": 35,
        "archive_name": "python-3.12.10-embed-amd64.zip",
        "archive_sha256": (
            "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
        ),
        "archive_size": 11_133_606,
        "original_python_path_config_sha256": (
            "2820f241bc9d6810d4db21c21cca3845799367fbdf0199620fb37c86a74b945c"
        ),
        "original_uncompressed_bytes": 22_501_299,
        "python_version": "3.12.10",
        "security_scope": (
            "LAST_OFFICIAL_WINDOWS_BINARY_FIXED_IDENTITY_NOT_SECURITY_PATCH_COMPLETENESS"
        ),
        "target": "win_amd64",
    }
    for key, expected_value in embedded_exact.items():
        if embedded.get(key) != expected_value or type(embedded.get(key)) is not type(
            expected_value
        ):
            _fail("RUNTIME_MANIFEST_INVALID", f"embedded field differs: {key}")
    _sha256(
        embedded.get("archive_member_tree_sha256"),
        "embedded archive member tree SHA-256",
    )
    generator = _exact_object(
        manifest.get("generator_binding"),
        _RUNTIME_GENERATOR_KEYS,
        "runtime generator binding",
    )
    generator_exact = {
        "base_primitives_file": "make_release_runtime_manifest.py",
        "base_primitives_sha256": (
            "6eb513774a83ab1aec99a9be4b677567b843fd64248b5765eb3a7b5fddb65d53"
        ),
        "binding_role": "SEMANTIC_IMPLEMENTATION_INPUTS_NOT_EXTERNAL_AUTHORITY",
        "generator_file": "make_release_runtime_manifest_v3.py",
    }
    for key, expected_value in generator_exact.items():
        if generator.get(key) != expected_value:
            _fail("RUNTIME_MANIFEST_INVALID", f"generator field differs: {key}")
    _sha256(generator.get("generator_sha256"), "runtime generator SHA-256")
    layout = _exact_object(
        manifest.get("layout"), _RUNTIME_LAYOUT_KEYS, "runtime layout"
    )
    expected_layout = {
        "irsdk_module": "runtime/Lib/site-packages/irsdk.py",
        "project_dist_info_directory": (
            "runtime/Lib/site-packages/iracing_ai_engineer-0.1.0.dist-info"
        ),
        "project_package_directory": (
            "runtime/Lib/site-packages/iracing_ai_engineer"
        ),
        "project_record": (
            "runtime/Lib/site-packages/iracing_ai_engineer-0.1.0.dist-info/RECORD"
        ),
        "project_wheel": "iracing_ai_engineer-0.1.0-py3-none-any.whl",
        "python_core_dll": "runtime/python312.dll",
        "python_executable": "runtime/python.exe",
        "python_path_config": "runtime/python312._pth",
        "runtime_directory": "runtime",
        "site_packages_directory": "runtime/Lib/site-packages",
        "stdlib_archive": "runtime/python312.zip",
        "wheel_data_file": "runtime/share/man/man1/ttx.1",
    }
    if layout != expected_layout:
        _fail("RUNTIME_MANIFEST_INVALID", "runtime layout differs")
    files = manifest.get("files")
    if type(files) is not list:
        _fail("RUNTIME_MANIFEST_INVALID", "runtime files must be an array")
    count = _plain_int(manifest.get("file_count"), "runtime file_count", minimum=1)
    if count > _MAX_RUNTIME_FILE_COUNT:
        _fail("RUNTIME_MANIFEST_INVALID", "runtime file count is unbounded")
    if len(files) != count:
        _fail("RUNTIME_MANIFEST_INVALID", "runtime file count differs")
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, value in enumerate(files):
        record = _exact_object(
            value, _RUNTIME_FILE_KEYS, f"runtime file record {index}"
        )
        relative = _safe_relative_path(record.get("path"), f"runtime file {index}")
        if relative.casefold() in seen:
            _fail("RUNTIME_MANIFEST_INVALID", "runtime paths collide")
        seen.add(relative.casefold())
        _sha256(record.get("sha256"), f"runtime file {relative} SHA-256")
        _plain_int(record.get("size"), f"runtime file {relative} size")
        if record.get("role") not in _RUNTIME_FILE_ROLES:
            _fail("RUNTIME_MANIFEST_INVALID", f"runtime role is invalid: {relative}")
        normalized.append(dict(record))
    ordered = sorted(
        normalized,
        key=lambda item: (str(item["path"]).casefold(), str(item["path"])),
    )
    if normalized != ordered:
        _fail("RUNTIME_MANIFEST_INVALID", "runtime records are not ordered")
    total_bytes = _plain_int(
        manifest.get("total_bytes"), "runtime total_bytes", minimum=1
    )
    if total_bytes > _MAX_RUNTIME_TOTAL_BYTES or total_bytes != sum(
        int(record["size"]) for record in normalized
    ):
        _fail("RUNTIME_MANIFEST_INVALID", "runtime total bytes differ")
    if hashlib.sha256(_canonical_json(normalized, newline=True)).hexdigest() != _sha256(
        manifest.get("tree_sha256"), "runtime tree SHA-256"
    ):
        _fail("RUNTIME_MANIFEST_INVALID", "runtime tree hash differs")
    without_self = dict(manifest)
    declared_self = _sha256(
        without_self.pop("manifest_self_sha256"), "runtime manifest self SHA-256"
    )
    if (
        hashlib.sha256(_canonical_json(without_self, newline=True)).hexdigest()
        != declared_self
    ):
        _fail("RUNTIME_MANIFEST_INVALID", "runtime manifest self hash differs")
    return manifest


@dataclass(frozen=True, slots=True)
class InstallAdmission:
    install_manifest_sha256: str
    project_wheel_sha256: str
    runtime_manifest_sha256: str
    runtime_manifest_self_sha256: str
    runtime_tree_sha256: str
    security_tree_sha256: str
    security_tree_object_count: int
    code_root: str
    threat_model: str = ATTESTATION_STATUS
    contract_version: str = INSTALL_CONTRACT_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "code_root": self.code_root,
            "contract_version": self.contract_version,
            "install_manifest_sha256": self.install_manifest_sha256,
            "project_wheel_sha256": self.project_wheel_sha256,
            "runtime_manifest_self_sha256": self.runtime_manifest_self_sha256,
            "runtime_manifest_sha256": self.runtime_manifest_sha256,
            "runtime_tree_sha256": self.runtime_tree_sha256,
            "security_tree_object_count": self.security_tree_object_count,
            "security_tree_sha256": self.security_tree_sha256,
            "threat_model": self.threat_model,
        }


@dataclass(frozen=True, slots=True)
class CaptureHandleIdentity:
    filename: str
    byte_size: int
    sha256: str
    volume_serial_number: int
    file_id: str
    snapshot_method: str = CAPTURE_SNAPSHOT_METHOD

    def to_dict(self) -> dict[str, object]:
        return {
            "byte_size": self.byte_size,
            "file_id": self.file_id,
            "filename": self.filename,
            "sha256": self.sha256,
            "snapshot_method": self.snapshot_method,
            "volume_serial_number": self.volume_serial_number,
        }


def _native_handle_numbers(handle: BinaryIO) -> tuple[int, int, int]:
    """Return volume serial, file-index high, and file-index low."""

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
    raw = msvcrt.get_osfhandle(handle.fileno())
    if not query(raw, ctypes.byref(information)):
        _fail("HANDLE_IDENTITY_FAILED", "GetFileInformationByHandle failed")
    if information.attributes & 0x400:
        _fail("HANDLE_IDENTITY_FAILED", "capture handle is a reparse point")
    return (
        int(information.volume_serial_number),
        int(information.file_index_high),
        int(information.file_index_low),
    )


def _capture_handle_identity(
    handle: BinaryIO,
    *,
    filename: str,
    path: Path | None = None,
) -> CaptureHandleIdentity:
    if (
        type(filename) is not str
        or not filename
        or filename != Path(filename).name
        or "/" in filename
        or "\\" in filename
    ):
        _fail("CAPTURE_IDENTITY_INVALID", "capture identity must use a basename")
    byte_size, digest = _hash_handle(handle)
    if byte_size < 1:
        _fail("CAPTURE_IDENTITY_INVALID", "capture handle is empty")
    volume, high, low = _native_handle_numbers(handle)
    if path is not None:
        _assert_handle_matches_path(handle, path, expected_size=byte_size)
    return CaptureHandleIdentity(
        filename=filename,
        byte_size=byte_size,
        sha256=digest,
        volume_serial_number=volume,
        file_id=f"{high:08x}:{low:08x}",
    )


def _assert_handle_matches_path(
    handle: BinaryIO,
    path: Path,
    *,
    expected_size: int | None = None,
) -> None:
    try:
        held = os.fstat(handle.fileno())
        current = os.lstat(path)
    except OSError as exc:
        raise LiveSupervisorError(
            "OUTPUT_PATH_CHANGED", f"cannot bind held output path: {path.name}"
        ) from exc
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or int(getattr(current, "st_file_attributes", 0)) & 0x400
        or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)
        or (expected_size is not None and held.st_size != expected_size)
        or current.st_size != held.st_size
    ):
        _fail("OUTPUT_PATH_CHANGED", f"held output path differs: {path.name}")


def build_live_analysis_authority(
    *,
    run_id: str,
    capture: CaptureHandleIdentity,
    simulator_identity: Mapping[str, object],
    install_admission: InstallAdmission,
    runtime_token_admission: RuntimeTokenAdmission | Mapping[str, object],
    preflight_receipt_sha256: str,
    preflight_production_semantic_digest: str,
) -> dict[str, object]:
    """Build the exact authority consumed by the same-handle live builder."""

    run_id = _validate_run_id(run_id)
    if capture.filename != f"live-{run_id}.jsonl":
        _fail("AUTHORITY_INVALID", "capture filename is not canonical for run_id")
    simulator = _exact_object(
        dict(simulator_identity),
        frozenset(
            {"process_id", "start_time_utc_ticks", "windows_session_id"}
        ),
        "simulator identity",
    )
    process_id = _plain_int(simulator["process_id"], "sim process_id", minimum=1)
    start_ticks = _plain_int(
        simulator["start_time_utc_ticks"], "sim start ticks", minimum=1
    )
    session_id = _plain_int(
        simulator["windows_session_id"], "sim Windows session", minimum=1
    )
    token_value = (
        runtime_token_admission.to_dict()
        if isinstance(runtime_token_admission, RuntimeTokenAdmission)
        else dict(runtime_token_admission)
    )
    token = _exact_object(
        token_value,
        frozenset(
            {
                "administrators_sid_enabled",
                "contract_version",
                "current_user_sid",
                "integrity_level_rid",
                "least_privilege",
                "token_elevation_type",
                "token_is_elevated",
            }
        ),
        "runtime token admission",
    )
    expected_token = RuntimeTokenAdmission(
        current_user_sid=FIXED_RUNTIME_USER_SID,
        token_is_elevated=False,
        token_elevation_type="LIMITED",
        administrators_sid_enabled=False,
        integrity_level_rid=0x2000,
        least_privilege="PASS",
    ).to_dict()
    if token != expected_token:
        _fail("AUTHORITY_INVALID", "runtime token admission differs")
    payload: dict[str, object] = {
        "ancestor_admission": "PASS",
        "attestation_status": ATTESTATION_STATUS,
        "capture_byte_size": capture.byte_size,
        "capture_file": capture.filename,
        "capture_file_id": capture.file_id,
        "capture_sha256": capture.sha256,
        "capture_snapshot_method": capture.snapshot_method,
        "capture_volume_serial_number": capture.volume_serial_number,
        "code_root": install_admission.code_root,
        "code_trust_model": CODE_TRUST_MODEL,
        "contract_version": LIVE_ANALYSIS_AUTHORITY_CONTRACT_VERSION,
        "dev_smoke_profile_byte_size": DEV_SMOKE_PROFILE_BYTE_SIZE,
        "dev_smoke_profile_id": DEV_SMOKE_PROFILE_ID,
        "dev_smoke_profile_sha256": DEV_SMOKE_PROFILE_SHA256,
        "execution_mode": EXECUTION_MODE,
        "install_contract_version": install_admission.contract_version,
        "install_manifest_sha256": install_admission.install_manifest_sha256,
        "preflight_production_semantic_digest": _sha256(
            preflight_production_semantic_digest,
            "preflight production semantic digest",
        ),
        "preflight_receipt_sha256": _sha256(
            preflight_receipt_sha256, "preflight receipt SHA-256"
        ),
        "project_wheel_sha256": install_admission.project_wheel_sha256,
        "run_id": run_id,
        "runtime_manifest_self_sha256": (
            install_admission.runtime_manifest_self_sha256
        ),
        "runtime_manifest_sha256": install_admission.runtime_manifest_sha256,
        "runtime_tree_sha256": install_admission.runtime_tree_sha256,
        "runtime_token_admission": token,
        "security_descriptor_profile": SECURITY_PROFILE,
        "security_tree_object_count": install_admission.security_tree_object_count,
        "security_tree_sha256": install_admission.security_tree_sha256,
        "sim_process_id": process_id,
        "sim_start_time_utc_ticks": start_ticks,
        "supervisor_contract_version": SUPERVISOR_CONTRACT_VERSION,
        "windows_session_id": session_id,
    }
    result = dict(payload)
    result["authority_sha256"] = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if set(result) != _LIVE_ANALYSIS_AUTHORITY_KEYS:
        raise AssertionError("internal authority schema drift")
    return result


def _validate_security_profile_receipt(value: object) -> dict[str, object]:
    keys = frozenset(
        {
            "administrators_sid",
            "contract_version",
            "dacl_protected",
            "directory_aces",
            "directory_sddl",
            "file_aces",
            "file_sddl",
            "owner_sid",
            "root_aces",
            "root_sddl",
            "runtime_user_sid",
            "semantic_ace_records_authoritative",
            "system_sid",
        }
    )
    profile = _exact_object(value, keys, "security profile")
    expected = {
        "administrators_sid": BUILTIN_ADMINISTRATORS_SID,
        "contract_version": SECURITY_PROFILE_CONTRACT_VERSION,
        "dacl_protected": True,
        "directory_aces": [ace.to_dict() for ace in _expected_aces("DIRECTORY")],
        "directory_sddl": DIRECTORY_SDDL,
        "file_aces": [ace.to_dict() for ace in _expected_aces("FILE")],
        "file_sddl": FILE_SDDL,
        "owner_sid": BUILTIN_ADMINISTRATORS_SID,
        "root_aces": [ace.to_dict() for ace in _expected_aces("DIRECTORY")],
        "root_sddl": DIRECTORY_SDDL,
        "runtime_user_sid": FIXED_RUNTIME_USER_SID,
        "semantic_ace_records_authoritative": True,
        "system_sid": LOCAL_SYSTEM_SID,
    }
    if profile != expected:
        _fail("INSTALL_MANIFEST_INVALID", "security profile differs")
    return profile


def _validate_ancestor_admission(value: object) -> dict[str, object]:
    admission = _exact_object(
        value,
        frozenset({"contract_version", "objects", "status"}),
        "ancestor admission",
    )
    if (
        admission.get("contract_version") != ANCESTOR_ADMISSION_CONTRACT_VERSION
        or admission.get("status") != "PASS"
        or type(admission.get("objects")) is not list
    ):
        _fail("INSTALL_MANIFEST_INVALID", "ancestor admission differs")
    paths = (
        r"C:\Program Files",
        r"C:\Program Files\AEIS",
        r"C:\Program Files\AEIS\releases",
        str(FIXED_VERSION_ROOT),
    )
    roles = (
        "SYSTEM_CODE_PARENT",
        "VENDOR_CODE_ROOT",
        "RELEASES_CODE_ROOT",
        "VERSION_CODE_ROOT",
    )
    profiles = (
        "WINDOWS_SYSTEM_PROGRAM_FILES_PARENT_V1",
        SECURITY_PROFILE,
        SECURITY_PROFILE,
        SECURITY_PROFILE,
    )
    owners = (
        TRUSTED_INSTALLER_SID,
        BUILTIN_ADMINISTRATORS_SID,
        BUILTIN_ADMINISTRATORS_SID,
        BUILTIN_ADMINISTRATORS_SID,
    )
    expected_objects = [
        {
            "dacl_protected": True,
            "object_role": role,
            "owner_sid": owner,
            "path": path,
            "profile": profile,
            "reparse_point": False,
            "runtime_dangerous_rights_mask": 0,
            "status": "PASS",
        }
        for path, role, profile, owner in zip(
            paths, roles, profiles, owners, strict=True
        )
    ]
    object_keys = frozenset(expected_objects[0])
    objects = admission["objects"]
    for index, item in enumerate(objects):
        _exact_object(item, object_keys, f"ancestor object {index}")
    if objects != expected_objects:
        _fail("INSTALL_MANIFEST_INVALID", "ancestor object admission differs")
    return admission


def _validate_installer_identity(value: object) -> dict[str, object]:
    identity = _exact_object(
        value,
        frozenset({"elevated", "status", "user_sid"}),
        "installer identity admission",
    )
    if identity != {
        "elevated": True,
        "status": "PASS",
        "user_sid": FIXED_RUNTIME_USER_SID,
    }:
        _fail("INSTALL_MANIFEST_INVALID", "installer identity differs")
    return identity


def _validate_threat_boundary(value: object) -> dict[str, object]:
    boundary = _exact_object(
        value,
        frozenset(
            {
                "contract_version",
                "development_smoke_profile",
                "excludes",
                "live_authenticity",
                "protected_subject_sid",
                "protects",
                "status",
            }
        ),
        "threat boundary",
    )
    expected = {
        "contract_version": THREAT_BOUNDARY_CONTRACT_VERSION,
        "development_smoke_profile": {
            "official_event_rules": False,
            "race_advice_allowed": False,
            "scenario_role": "DEVELOPMENT_SMOKE_CONFIG_NOT_EVENT_TRUTH",
            "trust_role": "PIPELINE_CONTRACT_FIXTURE_NOT_EVENT_TRUTH",
        },
        "excludes": [
            "ADMINISTRATOR_OR_SYSTEM_MODIFICATION",
            "CRYPTOGRAPHIC_EVENT_ORIGIN_ATTESTATION",
            "RUNTIME_USER_PROCESS_MEMORY_INJECTION",
            "SELF_CONSISTENT_DATA_FORGERY_BY_RUNTIME_USER",
        ],
        "live_authenticity": ATTESTATION_STATUS,
        "protected_subject_sid": FIXED_RUNTIME_USER_SID,
        "protects": [
            "DISK_CODE_AND_PATH_TOCTOU_PLUS_ACCIDENTAL_CONCURRENT_REPLACEMENT",
        ],
        "status": "PASS",
    }
    if boundary != expected:
        _fail("INSTALL_MANIFEST_INVALID", "threat boundary differs")
    return boundary


def _validate_install_v2_receipt(
    install_bytes: bytes,
    *,
    expected_install_sha256: str,
) -> dict[str, object]:
    expected = _sha256(expected_install_sha256, "install manifest SHA-256")
    if hashlib.sha256(install_bytes).hexdigest() != expected:
        _fail("INSTALL_MANIFEST_MISMATCH", "install manifest bytes differ")
    install = _exact_object(
        _strict_json_bytes(install_bytes, "install manifest"),
        _INSTALL_V2_KEYS,
        "install manifest v2",
    )
    exact = {
        "advisor_only": True,
        "code_owner_sid": BUILTIN_ADMINISTRATORS_SID,
        "code_root": str(FIXED_VERSION_ROOT),
        "code_trust_model": CODE_TRUST_MODEL,
        "contract_version": INSTALL_CONTRACT_VERSION,
        "dacl_protected": True,
        "development_smoke_profile_byte_size": DEV_SMOKE_PROFILE_BYTE_SIZE,
        "development_smoke_profile_file": DEV_SMOKE_PROFILE_FILE,
        "development_smoke_profile_sha256": DEV_SMOKE_PROFILE_SHA256,
        "import_smoke": "PASS",
        "install_directory": str(FIXED_VERSION_ROOT),
        "install_root": FIXED_INSTALL_ROOT,
        "live_capture_validated": False,
        "package_version": "0.1.0",
        "python_architecture": "AMD64",
        "python_bits": 64,
        "python_path_config_file": "runtime\\python312._pth",
        "python_path_config_sha256": (
            "0971dbaa7c895646919cc695b690af8f135aa50e55b876d9ef46913513966890"
        ),
        "python_version": "3.12.10",
        "record_closure": "PASS",
        "runtime_closure": "PASS",
        "runtime_manifest_file": FIXED_RUNTIME_MANIFEST,
        "runtime_python_file": "runtime\\python.exe",
        "runtime_user_sid": FIXED_RUNTIME_USER_SID,
        "security_descriptor_profile": SECURITY_PROFILE,
        "security_tree_algorithm": SECURITY_TREE_ALGORITHM,
        "supervisor_task_admitter_file": SUPERVISOR_TASK_ADMITTER_FILE,
        "supervisor_task_installer_file": SUPERVISOR_TASK_INSTALLER_FILE,
        "target": "cp312-cp312-win_amd64",
        "threat_model": ATTESTATION_STATUS,
        "version_directory": FIXED_INSTALL_DIRECTORY,
        "writable_state_root": str(FIXED_STATE_ROOT),
    }
    for key, value in exact.items():
        if install.get(key) != value or type(install.get(key)) is not type(value):
            _fail("INSTALL_MANIFEST_INVALID", f"install field differs: {key}")
    for name in (
        "embedded_python_sha256",
        "project_wheel_sha256",
        "release_manifest_sha256",
        "runtime_manifest_primitives_sha256",
        "runtime_manifest_self_sha256",
        "runtime_manifest_sha256",
        "runtime_manifest_tool_sha256",
        "runtime_tree_sha256",
        "security_tree_sha256",
        "supervisor_task_admitter_sha256",
        "supervisor_task_installer_sha256",
        "wheel_install_receipt_sha256",
        "wheel_installer_tool_sha256",
        "wheelhouse_manifest_sha256",
    ):
        _sha256(install.get(name), f"install {name}")
    for name in (
        "development_smoke_profile_file",
        "embedded_python_file",
        "project_wheel_file",
        "release_manifest_file",
        "runtime_manifest_primitives_file",
        "runtime_manifest_tool_file",
        "supervisor_task_admitter_file",
        "supervisor_task_installer_file",
        "wheel_install_receipt_file",
        "wheel_installer_tool_file",
        "wheelhouse_manifest_file",
    ):
        value = install.get(name)
        if type(value) is not str:
            _fail("INSTALL_MANIFEST_INVALID", f"install path is invalid: {name}")
        _safe_relative_path(value.replace("\\", "/"), f"install {name}")
    _plain_int(install.get("runtime_file_count"), "runtime_file_count", minimum=1)
    _plain_int(install.get("runtime_total_bytes"), "runtime_total_bytes", minimum=1)
    _plain_int(
        install.get("security_object_count"),
        "security_object_count",
        minimum=1,
    )
    _validate_security_profile_receipt(install.get("security_profile"))
    _validate_ancestor_admission(install.get("ancestor_admission"))
    _validate_installer_identity(install.get("installer_identity_admission"))
    _validate_threat_boundary(install.get("threat_boundary"))
    return install


def _windows_process_session_id(process_id: int) -> int:
    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "process session observation is Windows-only")
    import ctypes
    from ctypes import wintypes

    value = wintypes.DWORD()
    function = ctypes.windll.kernel32.ProcessIdToSessionId
    function.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    function.restype = wintypes.BOOL
    if not function(process_id, ctypes.byref(value)):
        _fail("SIMULATOR_IDENTITY_FAILED", "cannot observe Windows session id")
    return int(value.value)


def _validate_interactive_session_pair(
    simulator_session_id: object,
    supervisor_session_id: object,
) -> int:
    simulator = _plain_int(
        simulator_session_id, "simulator Windows session", minimum=1
    )
    supervisor = _plain_int(
        supervisor_session_id, "supervisor Windows session", minimum=1
    )
    if simulator != supervisor:
        _fail(
            "SIMULATOR_SESSION_INVALID",
            "simulator and supervisor are not in the same interactive session",
        )
    return simulator


def _windows_process_identity(handle: int) -> tuple[int, int, str]:
    import ctypes
    from ctypes import wintypes

    get_id = ctypes.windll.kernel32.GetProcessId
    get_id.argtypes = (wintypes.HANDLE,)
    get_id.restype = wintypes.DWORD
    process_id = int(get_id(handle))
    if process_id < 1:
        _fail("SIMULATOR_IDENTITY_FAILED", "simulator process handle is invalid")

    class FileTime(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    get_times = ctypes.windll.kernel32.GetProcessTimes
    get_times.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    )
    get_times.restype = wintypes.BOOL
    creation = FileTime()
    exit_time = FileTime()
    kernel = FileTime()
    user = FileTime()
    if not get_times(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        _fail("SIMULATOR_IDENTITY_FAILED", "cannot observe simulator start time")
    start_ticks = (
        (int(creation.high) << 32)
        | int(creation.low)
    ) + _DOTNET_TICKS_AT_WINDOWS_EPOCH

    query_name = ctypes.windll.kernel32.QueryFullProcessImageNameW
    query_name.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    query_name.restype = wintypes.BOOL
    capacity = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(capacity.value)
    if not query_name(handle, 0, buffer, ctypes.byref(capacity)):
        _fail("SIMULATOR_IDENTITY_FAILED", "cannot observe simulator image path")
    image = buffer.value
    if ntpath.basename(image).casefold() != _SIMULATOR_EXECUTABLE.casefold():
        _fail("SIMULATOR_IDENTITY_FAILED", "simulator image name differs")
    return process_id, start_ticks, image


@dataclass(slots=True)
class SimulatorProcess:
    process_id: int
    start_time_utc_ticks: int
    windows_session_id: int
    image_path: str
    _handle: int

    def identity(self) -> dict[str, int]:
        return {
            "process_id": self.process_id,
            "start_time_utc_ticks": self.start_time_utc_ticks,
            "windows_session_id": self.windows_session_id,
        }

    def assert_same_running_process(self) -> None:
        import ctypes
        from ctypes import wintypes

        wait = ctypes.windll.kernel32.WaitForSingleObject
        wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait.restype = wintypes.DWORD
        if int(wait(self._handle, 0)) != 0x00000102:  # WAIT_TIMEOUT
            _fail("SIMULATOR_EXITED", "simulator exited before supervisor completion")
        process_id, start_ticks, image = _windows_process_identity(self._handle)
        if (
            process_id != self.process_id
            or start_ticks != self.start_time_utc_ticks
            or _windows_process_session_id(process_id) != self.windows_session_id
            or _windows_normalized_path(image)
            != _windows_normalized_path(self.image_path)
        ):
            _fail("SIMULATOR_IDENTITY_CHANGED", "simulator identity changed")

    def close(self) -> None:
        if self._handle:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = 0

    def __enter__(self) -> SimulatorProcess:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _open_single_simulator_process() -> SimulatorProcess:
    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "simulator discovery is Windows-only")
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = (
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("process_id", wintypes.DWORD),
            ("default_heap_id", ctypes.c_size_t),
            ("module_id", wintypes.DWORD),
            ("thread_count", wintypes.DWORD),
            ("parent_process_id", wintypes.DWORD),
            ("base_priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
            ("exe_file", wintypes.WCHAR * 260),
        )

    create_snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    create_snapshot.restype = wintypes.HANDLE
    first = ctypes.windll.kernel32.Process32FirstW
    first.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    first.restype = wintypes.BOOL
    next_process = ctypes.windll.kernel32.Process32NextW
    next_process.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    next_process.restype = wintypes.BOOL
    close = ctypes.windll.kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    snapshot = create_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    invalid = ctypes.c_void_p(-1).value
    if snapshot in (None, invalid):
        _fail("SIMULATOR_DISCOVERY_FAILED", "cannot enumerate Windows processes")
    matches: list[int] = []
    try:
        entry = ProcessEntry32W()
        entry.size = ctypes.sizeof(entry)
        present = bool(first(snapshot, ctypes.byref(entry)))
        while present:
            if entry.exe_file.casefold() == _SIMULATOR_EXECUTABLE.casefold():
                matches.append(int(entry.process_id))
            entry.size = ctypes.sizeof(entry)
            present = bool(next_process(snapshot, ctypes.byref(entry)))
    finally:
        close(snapshot)
    if not matches:
        _fail("SIMULATOR_UNAVAILABLE", "iRacing simulator is not running")
    if len(matches) != 1:
        _fail(
            "SIMULATOR_PROCESS_SET_INVALID",
            "exactly one iRacing simulator process is required",
        )
    process_id = matches[0]
    session_id = _windows_process_session_id(process_id)
    current_session = _windows_process_session_id(os.getpid())
    _validate_interactive_session_pair(session_id, current_session)
    open_process = ctypes.windll.kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    raw = open_process(0x00100000 | 0x00001000, False, process_id)
    if not raw:
        _fail("SIMULATOR_OPEN_FAILED", "cannot hold simulator process identity")
    try:
        observed_pid, start_ticks, image = _windows_process_identity(raw)
        if observed_pid != process_id:
            _fail("SIMULATOR_IDENTITY_CHANGED", "simulator PID changed at open")
        result = SimulatorProcess(
            process_id=process_id,
            start_time_utc_ticks=start_ticks,
            windows_session_id=session_id,
            image_path=image,
            _handle=int(raw),
        )
        result.assert_same_running_process()
        return result
    except Exception:
        close(raw)
        raise


class SupervisorPhase(StrEnum):
    STARTING = "STARTING"
    PREFLIGHT = "PREFLIGHT"
    WAIT = "WAIT"
    CAPTURING = "CAPTURING"
    ANALYZING = "ANALYZING"
    READY = "READY"
    FAILED = "FAILED"


class _SupervisorLifecycle:
    """Small terminal-state guard; READY and FAILED are mutually exclusive."""

    __slots__ = ("_phase",)

    def __init__(self) -> None:
        self._phase = SupervisorPhase.STARTING

    @property
    def phase(self) -> SupervisorPhase:
        return self._phase

    def move(self, expected: SupervisorPhase, target: SupervisorPhase) -> None:
        if self._phase is not expected or self._phase in {
            SupervisorPhase.READY,
            SupervisorPhase.FAILED,
            SupervisorPhase.WAIT,
        }:
            _fail(
                "STATE_TRANSITION_INVALID",
                f"cannot move supervisor from {self._phase} to {target}",
            )
        self._phase = target

    def commit_ready(self) -> None:
        self.move(SupervisorPhase.ANALYZING, SupervisorPhase.READY)

    def commit_wait(self) -> None:
        self.move(SupervisorPhase.PREFLIGHT, SupervisorPhase.WAIT)

    def commit_failed(self) -> None:
        if self._phase in {SupervisorPhase.READY, SupervisorPhase.WAIT}:
            _fail("TERMINAL_STATE_CONFLICT", "terminal state is already committed")
        self._phase = SupervisorPhase.FAILED


@dataclass(slots=True)
class SealedArtifact:
    handle: BinaryIO
    path: Path
    byte_size: int
    sha256: str
    device: int
    inode: int
    pending_path: Path | None = None

    def close(self) -> None:
        self.handle.close()
        pending = self.pending_path
        self.pending_path = None
        if pending is None:
            return
        if (
            pending.parent != self.path.parent
            or pending.name != f".pending-{self.path.name}"
        ):
            _fail("TERMINAL_CLEANUP_REFUSED", "pending terminal path is not exact")
        try:
            current = os.lstat(pending)
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or int(getattr(current, "st_file_attributes", 0)) & 0x400
            or (current.st_dev, current.st_ino) != (self.device, self.inode)
        ):
            _fail(
                "TERMINAL_CLEANUP_REFUSED",
                "pending terminal path no longer names the sealed object",
            )
        os.unlink(pending)


def _open_exclusive_binary(path: Path) -> BinaryIO:
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        raw = create_file(
            str(path),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0x00000001,  # FILE_SHARE_READ; deliberately no WRITE/DELETE
            None,
            1,  # CREATE_NEW
            0x00000080 | 0x00200000,  # NORMAL | OPEN_REPARSE_POINT
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if raw in (None, invalid):
            error = ctypes.get_last_error()
            raise LiveSupervisorError(
                "EXCLUSIVE_CREATE_FAILED",
                f"cannot CreateNew protected output {path.name}: winerror={error}",
            )
        try:
            descriptor = msvcrt.open_osfhandle(
                int(raw), os.O_RDWR | int(getattr(os, "O_BINARY", 0))
            )
        except Exception:
            close_handle(raw)
            raise
        return os.fdopen(descriptor, "w+b", buffering=0)

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    for name in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW"):
        flags |= int(getattr(os, name, 0))
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LiveSupervisorError(
            "EXCLUSIVE_CREATE_FAILED", f"cannot CreateNew output: {path.name}"
        ) from exc
    return os.fdopen(descriptor, "w+b", buffering=0)


def _open_held_readonly(path: Path) -> BinaryIO:
    """Open one protected file without write/delete sharing on Windows."""

    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_handle = ctypes.windll.kernel32.CloseHandle
        raw = create_file(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001,  # FILE_SHARE_READ only
            None,
            3,  # OPEN_EXISTING
            0x00000080 | 0x00200000,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if raw in (None, invalid):
            _fail("HELD_OPEN_FAILED", f"cannot hold protected file: {path.name}")
        try:
            descriptor = msvcrt.open_osfhandle(
                int(raw), os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
            )
        except Exception:
            close_handle(raw)
            raise
        return os.fdopen(descriptor, "rb", buffering=0)

    flags = os.O_RDONLY
    for name in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW"):
        flags |= int(getattr(os, name, 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LiveSupervisorError(
            "HELD_OPEN_FAILED", f"cannot hold protected file: {path.name}"
        ) from exc
    return os.fdopen(descriptor, "rb", buffering=0)


def _hold_plain_file(
    stack: ExitStack,
    path: Path,
    *,
    label: str,
    maximum_bytes: int = _PROTECTED_FILE_MAX_BYTES,
    capture: bool = True,
) -> tuple[BinaryIO, bytes | None, str]:
    """Open, read, and retain one no-follow path identity until stack exit."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise LiveSupervisorError(
            "PROTECTED_FILE_MISSING", f"cannot inspect {label}: {path.name}"
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or int(getattr(before, "st_file_attributes", 0)) & 0x400
        or before.st_size < 0
        or before.st_size > maximum_bytes
    ):
        _fail("PROTECTED_FILE_INVALID", f"{label} is not a bounded plain file")
    handle = stack.enter_context(_open_held_readonly(path))
    opened = os.fstat(handle.fileno())
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino, opened.st_size)
        != (before.st_dev, before.st_ino, before.st_size)
    ):
        _fail("PROTECTED_FILE_CHANGED", f"{label} changed at descriptor open")
    handle.seek(0)
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if capture else None
    total = 0
    while chunk := handle.read(1024 * 1024):
        if type(chunk) is not bytes:
            _fail("PROTECTED_FILE_INVALID", f"{label} is not open in binary mode")
        total += len(chunk)
        if total > maximum_bytes:
            _fail("PROTECTED_FILE_INVALID", f"{label} exceeds its byte bound")
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    after = os.fstat(handle.fileno())
    try:
        current = os.lstat(path)
    except OSError as exc:
        raise LiveSupervisorError(
            "PROTECTED_FILE_CHANGED", f"cannot re-inspect {label}"
        ) from exc
    identity = (opened.st_dev, opened.st_ino, opened.st_size)
    if (
        total != opened.st_size
        or identity != (after.st_dev, after.st_ino, after.st_size)
        or identity != (current.st_dev, current.st_ino, current.st_size)
        or stat.S_ISLNK(current.st_mode)
        or int(getattr(current, "st_file_attributes", 0)) & 0x400
    ):
        _fail("PROTECTED_FILE_CHANGED", f"{label} changed while held")
    handle.seek(0)
    return handle, b"".join(chunks) if chunks is not None else None, digest.hexdigest()


def _release_file(version_root: Path, relative: object, label: str) -> tuple[str, Path]:
    safe = _safe_relative_path(
        relative.replace("\\", "/") if type(relative) is str else relative,
        label,
    )
    path = version_root.joinpath(*PurePosixPath(safe).parts)
    return safe, path


def _assert_install_reference_digest(
    relative: str, *, expected: str, observed: str
) -> None:
    if observed != expected:
        _fail("INSTALL_REFERENCE_MISMATCH", f"install reference differs: {relative}")


def _enumerate_plain_runtime_files(version_root: Path) -> set[str]:
    runtime = version_root / "runtime"
    _assert_plain_windows_directory(runtime, "runtime root")
    pending: list[tuple[Path, str]] = [(runtime, "runtime")]
    files: set[str] = set()
    folded: set[str] = set()
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise LiveSupervisorError(
                "RUNTIME_TREE_INVALID", f"cannot enumerate {prefix}"
            ) from exc
        for entry in entries:
            relative = f"{prefix}/{entry.name}"
            _safe_relative_path(relative, f"runtime tree {relative}")
            metadata = os.lstat(entry.path)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or int(getattr(metadata, "st_file_attributes", 0)) & 0x400
            ):
                _fail("RUNTIME_TREE_INVALID", f"runtime reparse point: {relative}")
            key = relative.casefold()
            if key in folded:
                _fail("RUNTIME_TREE_INVALID", "runtime paths case-collide")
            folded.add(key)
            if stat.S_ISDIR(metadata.st_mode):
                pending.append((Path(entry.path), relative))
            elif stat.S_ISREG(metadata.st_mode):
                files.add(relative)
            else:
                _fail("RUNTIME_TREE_INVALID", f"non-plain runtime object: {relative}")
    return files


def _validate_runtime_files_and_hold(
    stack: ExitStack,
    version_root: Path,
    manifest: Mapping[str, object],
    install: Mapping[str, object],
) -> dict[str, tuple[int, str]]:
    records = manifest["files"]
    if type(records) is not list:  # schema validator already rejects this
        raise AssertionError("validated runtime manifest lost files")
    by_path = {str(record["path"]): record for record in records}
    project_wheel = str(manifest["layout"]["project_wheel"])  # type: ignore[index]
    actual = _enumerate_plain_runtime_files(version_root)
    actual.add(project_wheel)
    if set(by_path) != actual:
        _fail("RUNTIME_TREE_INVALID", "runtime manifest does not exactly cover disk")

    expected_singletons = {
        "runtime/python.exe": "python_executable",
        "runtime/python312.dll": "python_core_dll",
        "runtime/python312.zip": "stdlib_archive",
        "runtime/python312._pth": "python_path_config",
        "runtime/Lib/site-packages/irsdk.py": "irsdk_module",
        "runtime/share/man/man1/ttx.1": "wheel_data_file",
        project_wheel: "project_wheel",
    }
    for path, role in expected_singletons.items():
        if by_path.get(path, {}).get("role") != role:
            _fail("RUNTIME_TREE_INVALID", f"runtime singleton role differs: {path}")

    measurements: dict[str, tuple[int, str]] = {}
    for relative, record in by_path.items():
        maximum = (
            256 * 1024 * 1024
            if relative.startswith("runtime/Lib/site-packages/")
            else 64 * 1024 * 1024
        )
        _, path = _release_file(version_root, relative, f"runtime file {relative}")
        held, payload, digest = _hold_plain_file(
            stack,
            path,
            label=f"runtime file {relative}",
            maximum_bytes=maximum,
            capture=False,
        )
        if (
            payload is not None
            or os.fstat(held.fileno()).st_size != record["size"]
            or digest != record["sha256"]
        ):
            _fail("RUNTIME_TREE_INVALID", f"runtime file differs: {relative}")
        measurements[relative] = (int(record["size"]), digest)

    wheel_record = by_path[project_wheel]
    if (
        project_wheel != install["project_wheel_file"].replace("\\", "/")
        or wheel_record["sha256"] != install["project_wheel_sha256"]
    ):
        _fail("RUNTIME_TREE_INVALID", "project wheel binding differs")
    pth = by_path["runtime/python312._pth"]
    if (
        pth["sha256"] != install["python_path_config_sha256"]
        or pth["size"] != 37
    ):
        _fail("RUNTIME_TREE_INVALID", "python312._pth binding differs")
    return measurements


@dataclass(slots=True)
class AdmittedProtectedRuntime:
    admission: InstallAdmission
    install_receipt: dict[str, object]
    runtime_manifest: dict[str, object]
    token_admission: RuntimeTokenAdmission
    _stack: ExitStack

    def close(self) -> None:
        self._stack.close()

    def __enter__(self) -> AdmittedProtectedRuntime:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _windows_normalized_path(value: object) -> str:
    if type(value) is str:
        text = value
    elif isinstance(value, Path):
        text = str(value)
    else:
        _fail("CODE_ROOT_INVALID", "runtime path is not text")
    return ntpath.normcase(ntpath.normpath(text))


def _assert_production_runtime_context() -> RuntimeTokenAdmission:
    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "R8 live supervisor is Windows-only")
    if sys.flags.isolated != 1 or sys.flags.dont_write_bytecode != 1:
        _fail(
            "RUNTIME_ISOLATION_REQUIRED",
            "production supervisor requires protected Windows python -I -B",
        )
    expected_python = FIXED_VERSION_ROOT.joinpath(*FIXED_RUNTIME_PYTHON.parts)
    if _windows_normalized_path(sys.executable) != _windows_normalized_path(
        expected_python
    ):
        _fail("RUNTIME_EXECUTABLE_INVALID", "sys.executable is outside fixed runtime")
    expected_module = FIXED_VERSION_ROOT / (
        "runtime/Lib/site-packages/iracing_ai_engineer/live_supervisor.py"
    )
    if _windows_normalized_path(__file__) != _windows_normalized_path(expected_module):
        _fail("RUNTIME_MODULE_INVALID", "supervisor module is outside fixed runtime")
    expected_sys_path = [
        FIXED_VERSION_ROOT / "runtime/python312.zip",
        FIXED_VERSION_ROOT / "runtime",
        FIXED_VERSION_ROOT / "runtime/Lib/site-packages",
    ]
    if [_windows_normalized_path(item) for item in sys.path] != [
        _windows_normalized_path(item) for item in expected_sys_path
    ]:
        _fail("RUNTIME_PATH_INVALID", "embedded Python sys.path is not exact")
    return _admit_windows_runtime_token()


def admit_installed_runtime() -> AdmittedProtectedRuntime:
    """Admit and hold the exact protected R8 install for one supervisor run.

    No caller supplies a manifest digest.  The fixed Program Files path, fixed
    ACL profile, Scheduled Task identity, exact runtime closure, and retained
    read-only descriptors are the disk-code boundary.  The result still makes
    no cryptographic origin/authenticity claim.
    """

    token_admission = _assert_production_runtime_context()
    _observe_and_validate_windows_ancestors()
    observed_objects = _observe_windows_security_tree(FIXED_VERSION_ROOT)
    observed_dicts = tuple(item.to_dict() for item in observed_objects)
    _validate_security_tree_profile(observed_dicts)

    stack = ExitStack()
    try:
        _, install_bytes, install_sha = _hold_plain_file(
            stack,
            FIXED_VERSION_ROOT / FIXED_INSTALL_MANIFEST,
            label="install manifest",
            maximum_bytes=_INSTALL_MANIFEST_MAX_BYTES,
        )
        if install_bytes is None:  # pragma: no cover - capture is requested
            raise AssertionError("install manifest was not captured")
        install = _validate_install_v2_receipt(
            install_bytes,
            expected_install_sha256=install_sha,
        )
        _validate_security_tree(
            observed_objects,
            expected_count=int(install["security_object_count"]),
            expected_sha256=str(install["security_tree_sha256"]),
        )

        runtime_relative, runtime_path = _release_file(
            FIXED_VERSION_ROOT,
            install["runtime_manifest_file"],
            "runtime manifest path",
        )
        if runtime_relative != FIXED_RUNTIME_MANIFEST:
            _fail("INSTALL_MANIFEST_INVALID", "runtime manifest path differs")
        _, runtime_bytes, runtime_sha = _hold_plain_file(
            stack,
            runtime_path,
            label="runtime manifest",
            maximum_bytes=_RUNTIME_MANIFEST_MAX_BYTES,
        )
        if runtime_bytes is None:  # pragma: no cover - capture is requested
            raise AssertionError("runtime manifest was not captured")
        if runtime_sha != install["runtime_manifest_sha256"]:
            _fail("RUNTIME_MANIFEST_MISMATCH", "install/runtime manifest hash differs")
        runtime_manifest = _validate_runtime_manifest(
            runtime_bytes,
            expected_sha256=runtime_sha,
        )
        if (
            runtime_manifest["manifest_self_sha256"]
            != install["runtime_manifest_self_sha256"]
            or runtime_manifest["tree_sha256"] != install["runtime_tree_sha256"]
            or runtime_manifest["file_count"] != install["runtime_file_count"]
            or runtime_manifest["total_bytes"] != install["runtime_total_bytes"]
        ):
            _fail("RUNTIME_MANIFEST_MISMATCH", "install/runtime aggregates differ")
        generator = runtime_manifest["generator_binding"]
        embedded = runtime_manifest["embedded_distribution"]
        if type(generator) is not dict or type(embedded) is not dict:
            raise AssertionError("validated runtime nested schema drift")
        if (
            generator["generator_sha256"] != install["runtime_manifest_tool_sha256"]
            or generator["base_primitives_sha256"]
            != install["runtime_manifest_primitives_sha256"]
            or embedded["archive_sha256"] != install["embedded_python_sha256"]
        ):
            _fail("RUNTIME_MANIFEST_MISMATCH", "runtime provenance binding differs")

        measurements = _validate_runtime_files_and_hold(
            stack, FIXED_VERSION_ROOT, runtime_manifest, install
        )
        referenced_hash_fields = {
            "development_smoke_profile_file": "development_smoke_profile_sha256",
            "embedded_python_file": "embedded_python_sha256",
            "project_wheel_file": "project_wheel_sha256",
            "release_manifest_file": "release_manifest_sha256",
            "runtime_manifest_primitives_file": "runtime_manifest_primitives_sha256",
            "runtime_manifest_tool_file": "runtime_manifest_tool_sha256",
            "supervisor_task_admitter_file": "supervisor_task_admitter_sha256",
            "supervisor_task_installer_file": "supervisor_task_installer_sha256",
            "wheel_install_receipt_file": "wheel_install_receipt_sha256",
            "wheel_installer_tool_file": "wheel_installer_tool_sha256",
            "wheelhouse_manifest_file": "wheelhouse_manifest_sha256",
        }
        held_references: set[str] = set(measurements)
        for path_field, hash_field in referenced_hash_fields.items():
            relative, path = _release_file(
                FIXED_VERSION_ROOT, install[path_field], f"install {path_field}"
            )
            expected_digest = str(install[hash_field])
            if relative in measurements:
                size, digest = measurements[relative]
            else:
                if relative.casefold() in {item.casefold() for item in held_references}:
                    _fail("INSTALL_MANIFEST_INVALID", "install paths case-collide")
                held, payload, digest = _hold_plain_file(
                    stack,
                    path,
                    label=f"install reference {relative}",
                    capture=False,
                )
                if payload is not None:  # pragma: no cover
                    raise AssertionError("reference capture unexpectedly retained bytes")
                size = os.fstat(held.fileno()).st_size
                held_references.add(relative)
            _assert_install_reference_digest(
                relative,
                expected=expected_digest,
                observed=digest,
            )
            if path_field == "development_smoke_profile_file" and size != int(
                install["development_smoke_profile_byte_size"]
            ):
                _fail("INSTALL_REFERENCE_MISMATCH", "development smoke size differs")

        final_objects = _observe_windows_security_tree(FIXED_VERSION_ROOT)
        final_dicts = tuple(item.to_dict() for item in final_objects)
        if final_dicts != observed_dicts:
            _fail("SECURITY_TREE_CHANGED", "protected security tree changed during admission")
        _validate_security_tree(
            final_objects,
            expected_count=int(install["security_object_count"]),
            expected_sha256=str(install["security_tree_sha256"]),
        )
        admission = InstallAdmission(
            install_manifest_sha256=install_sha,
            project_wheel_sha256=str(install["project_wheel_sha256"]),
            runtime_manifest_sha256=runtime_sha,
            runtime_manifest_self_sha256=str(
                install["runtime_manifest_self_sha256"]
            ),
            runtime_tree_sha256=str(install["runtime_tree_sha256"]),
            security_tree_sha256=str(install["security_tree_sha256"]),
            security_tree_object_count=int(install["security_object_count"]),
            code_root=str(FIXED_VERSION_ROOT),
        )
        return AdmittedProtectedRuntime(
            admission=admission,
            install_receipt=dict(install),
            runtime_manifest=dict(runtime_manifest),
            token_admission=token_admission,
            _stack=stack.pop_all(),
        )
    except Exception:
        stack.close()
        raise


def _write_json_exclusive(
    path: Path,
    value: Mapping[str, object],
    *,
    artifact_path: Path | None = None,
    pending_path: Path | None = None,
) -> SealedArtifact:
    """CreateNew + same-descriptor write/readback/hash; never unlink on error."""

    payload = _canonical_json(dict(value), newline=True)
    handle = _open_exclusive_binary(path)
    try:
        opened = os.fstat(handle.fileno())
        written = handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0)
        readback = handle.read()
        after = os.fstat(handle.fileno())
        current = os.lstat(path)
        if (
            written != len(payload)
            or readback != payload
            or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino)
            or stat.S_ISLNK(current.st_mode)
            or after.st_size != len(payload)
            or current.st_size != len(payload)
        ):
            _fail("RECEIPT_COMMIT_FAILED", "same-descriptor receipt closure differs")
        return SealedArtifact(
            handle=handle,
            path=artifact_path or path,
            byte_size=len(payload),
            sha256=hashlib.sha256(readback).hexdigest(),
            device=after.st_dev,
            inode=after.st_ino,
            pending_path=pending_path,
        )
    except Exception:
        with suppress(Exception):
            handle.close()
        # A failed CreateNew artifact is deliberately retained for inspection.
        raise


def _validate_run_id(value: str) -> str:
    if type(value) is not str or _RUN_ID_RE.fullmatch(value) is None:
        _fail("RUN_ID_INVALID", "run_id must be canonical UTC YYYYMMDDTHHMMSSZ")
    return value


def _fixed_state_directory_chain() -> tuple[Path, ...]:
    return (
        Path(r"C:\Users\racer"),
        Path(r"C:\Users\racer\AppData"),
        Path(r"C:\Users\racer\AppData\Local"),
        Path(r"C:\Users\racer\AppData\Local\AEIS"),
        Path(r"C:\Users\racer\AppData\Local\AEIS\state"),
        FIXED_STATE_ROOT,
    )


def _ensure_plain_directory_chain(
    paths: Sequence[Path], *, create_from: int
) -> Path:
    if not paths or create_from < 0 or create_from >= len(paths):
        raise AssertionError("invalid state directory chain")
    identities: dict[Path, tuple[int, int]] = {}
    for index, path in enumerate(paths):
        if index >= create_from:
            try:
                os.mkdir(path)
            except FileExistsError:
                pass
            except OSError as exc:
                raise LiveSupervisorError(
                    "STATE_ROOT_CREATE_FAILED",
                    f"cannot create writable state directory {index}: {path}",
                ) from exc
        identities[path] = _plain_windows_directory_identity(
            path, f"writable state ancestor {index}"
        )
        for prior_index, prior in enumerate(paths[: index + 1]):
            if (
                _plain_windows_directory_identity(
                    prior, f"writable state ancestor {prior_index}"
                )
                != identities[prior]
            ):
                _fail(
                    "STATE_ROOT_CHANGED",
                    f"writable state ancestor changed during creation: {prior}",
                )
    return paths[-1]


def _ensure_state_root() -> Path:
    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "state-root creation is Windows-only")
    return _ensure_plain_directory_chain(_fixed_state_directory_chain(), create_from=3)


def _admit_state_root() -> Path:
    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "state-root admission is Windows-only")
    expected_parts = _fixed_state_directory_chain()
    for index, path in enumerate(expected_parts):
        _assert_plain_windows_directory(path, f"writable state ancestor {index}")
    return FIXED_STATE_ROOT


def _storage_admission(
    state_root: Path,
    *,
    include_preflight_allowance: bool,
) -> dict[str, object]:
    usage = shutil.disk_usage(state_root)
    required = _CAPTURE_HARD_MAX_BYTES + _STATE_FREE_SPACE_RESERVE_BYTES
    if include_preflight_allowance:
        required += _PREFLIGHT_RESIDUE_ALLOWANCE_BYTES
    free = int(usage.free)
    return {
        "capture_hard_max_bytes": _CAPTURE_HARD_MAX_BYTES,
        "free_bytes": free,
        "preflight_residue_allowance_bytes": (
            _PREFLIGHT_RESIDUE_ALLOWANCE_BYTES
            if include_preflight_allowance
            else 0
        ),
        "required_free_bytes": required,
        "reserve_bytes": _STATE_FREE_SPACE_RESERVE_BYTES,
        "status": "PASS" if free >= required else "WAIT_STORAGE",
    }


@contextmanager
def _single_supervisor_mutex() -> Iterator[None]:
    if os.name != "nt":
        _fail("WINDOWS_REQUIRED", "supervisor mutex is Windows-only")
    import ctypes
    from ctypes import wintypes

    create = ctypes.windll.kernel32.CreateMutexW
    create.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    create.restype = wintypes.HANDLE
    close = ctypes.windll.kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    raw = create(None, False, _SUPERVISOR_MUTEX_NAME)
    if not raw:
        _fail("SUPERVISOR_MUTEX_FAILED", "cannot create R8 supervisor mutex")
    try:
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            _fail("SUPERVISOR_ALREADY_RUNNING", "another R8 supervisor is active")
        yield
    finally:
        close(raw)


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_unique_run_id(state_root: Path) -> str:
    base = datetime.now(UTC).replace(microsecond=0)
    for offset in range(86_400):
        candidate = (base + timedelta(seconds=offset)).strftime("%Y%m%dT%H%M%SZ")
        if not (state_root / f"started-{candidate}.json").exists():
            return _validate_run_id(candidate)
    _fail("RUN_ID_EXHAUSTED", "no unique supervisor run_id is available")


def _preflight_pass_fields(receipt: Mapping[str, object]) -> tuple[str, str]:
    if (
        receipt.get("status") != "PASS"
        or receipt.get("contract_version") != "live-capture-preflight-v3"
        or receipt.get("evidence_class") != "REAL_SDK_PRECHECK_ONLY"
        or receipt.get("can_start_live_capture") is not True
        or receipt.get("admission_recomputed") is not True
        or receipt.get("production_transport_attested") is not True
        or receipt.get("live_acceptance_eligible") is not False
        or receipt.get("advisor_only") is not True
        or receipt.get("vehicle_control_enabled") is not False
        or receipt.get("advice_generated") is not False
        or receipt.get("recommendations") != []
        or receipt.get("wait_reasons") != []
    ):
        _fail("PREFLIGHT_NOT_ADMITTED", "preflight is not an exact REAL PASS")
    digest = _sha256(receipt.get("receipt_sha256"), "preflight receipt SHA-256")
    semantic = _sha256(
        receipt.get("production_semantic_digest"),
        "preflight semantic SHA-256",
    )
    material = dict(receipt)
    material.pop("receipt_sha256", None)
    if hashlib.sha256(_canonical_json(material)).hexdigest() != digest:
        _fail("PREFLIGHT_NOT_ADMITTED", "preflight receipt hash differs")
    return digest, semantic


def _validate_full_collector_receipt(
    receipt: object,
    evidence: Mapping[str, object],
    *,
    source_id: str,
    session_id: str,
) -> None:
    if not hasattr(receipt, "to_dict"):
        _fail("COLLECTOR_NOT_ADMITTED", "collector receipt type is invalid")
    value = receipt.to_dict()
    zero_fields = (
        "duplicate_conflict_count",
        "dropped_tick_count",
        "stale_event_count",
        "session_reset_count",
        "schema_change_count",
    )
    evidence_zero_fields = zero_fields + (
        "capture_clock_regression_count",
        "read_error_field_count",
        "read_error_frame_count",
    )
    if (
        value.get("completion_status") != "COMPLETE"
        or value.get("frame_record_count", 0) < 1
        or type(value.get("duplicate_sample_count")) is not int
        or int(value["duplicate_sample_count"]) < 0
        or type(value.get("event_record_count")) is not int
        or int(value["event_record_count"]) < 0
        or value.get("event_record_count")
        != value.get("duplicate_sample_count")
        or value.get("samples_seen")
        != value.get("frame_record_count", 0) + value.get("duplicate_sample_count", 0)
        or value.get("schema_epoch_count") != 1
        or value.get("session_epoch_count") != 1
        or any(value.get(key) != 0 for key in zero_fields)
        or evidence.get("completion_status") != "COMPLETE"
        or evidence.get("source_kind") != "SDK_LIVE"
        or evidence.get("sim_mode") != "full"
        or evidence.get("source_id") != source_id
        or evidence.get("session_id") != session_id
        or evidence.get("frame_record_count") != value.get("frame_record_count")
        or evidence.get("samples_seen") != value.get("samples_seen")
        or evidence.get("duplicate_sample_count")
        != value.get("duplicate_sample_count")
        or evidence.get("event_record_count") != value.get("event_record_count")
        or evidence.get("event_record_count")
        != evidence.get("duplicate_sample_count")
        or evidence.get("schema_epoch_count") != 1
        or evidence.get("session_epoch_count") != 1
        or any(evidence.get(key) != 0 for key in evidence_zero_fields)
    ):
        _fail("COLLECTOR_NOT_ADMITTED", "canonical live collector quality differs")


def _terminal_payload(
    *,
    status: str,
    run_id: str,
    started_at_utc: str,
    simulator: SimulatorProcess | None,
    runtime: AdmittedProtectedRuntime,
    preflight: Mapping[str, object] | None,
    preflight_artifact: SealedArtifact | None,
    capture: CaptureHandleIdentity | None,
    analysis: Mapping[str, object] | None,
    storage_admission: Mapping[str, object] | None,
    wait_reasons: Sequence[str] = (),
    error: LiveSupervisorError | None = None,
) -> dict[str, object]:
    if status not in {"WAIT", "READY", "FAILED"}:
        raise AssertionError("invalid terminal status")
    payload: dict[str, object] = {
        "advisor_only": True,
        "analysis": dict(analysis) if analysis is not None else None,
        "attestation_status": ATTESTATION_STATUS,
        "capture": capture.to_dict() if capture is not None else None,
        "completed_at_utc": _utc_now_text(),
        "contract_version": "windows-live-supervisor-receipt-v1",
        "error": (
            {"code": error.code, "message": str(error)} if error is not None else None
        ),
        "install_admission": runtime.admission.to_dict(),
        "live_capture_validated": status == "READY",
        "preflight": dict(preflight) if preflight is not None else None,
        "preflight_artifact": (
            {
                "byte_size": preflight_artifact.byte_size,
                "filename": preflight_artifact.path.name,
                "sha256": preflight_artifact.sha256,
            }
            if preflight_artifact is not None
            else None
        ),
        "recommendations": [],
        "run_id": run_id,
        "runtime_token_admission": runtime.token_admission.to_dict(),
        "simulator_identity": simulator.identity() if simulator is not None else None,
        "started_at_utc": started_at_utc,
        "status": status,
        "storage_admission": (
            dict(storage_admission) if storage_admission is not None else None
        ),
        "vehicle_control_enabled": False,
        "wait_reasons": list(wait_reasons),
    }
    return payload


def _revalidate_protected_runtime(runtime: AdmittedProtectedRuntime) -> None:
    _observe_and_validate_windows_ancestors()
    objects = _observe_windows_security_tree(FIXED_VERSION_ROOT)
    _validate_security_tree(
        objects,
        expected_count=runtime.admission.security_tree_object_count,
        expected_sha256=runtime.admission.security_tree_sha256,
    )
    if _admit_windows_runtime_token().to_dict() != runtime.token_admission.to_dict():
        _fail("TOKEN_ADMISSION_CHANGED", "runtime token admission changed")


def _commit_terminal_artifact(
    lifecycle: _SupervisorLifecycle,
    *,
    expected_phase: SupervisorPhase,
    terminal_phase: SupervisorPhase,
    path: Path,
    payload: Mapping[str, object],
) -> SealedArtifact:
    if lifecycle.phase is not expected_phase:
        _fail("STATE_TRANSITION_INVALID", "terminal phase precondition differs")
    pending = path.with_name(f".pending-{path.name}")
    artifact = _write_json_exclusive(
        pending,
        payload,
        artifact_path=path,
        pending_path=pending,
    )

    def published_object_matches() -> bool:
        held = os.fstat(artifact.handle.fileno())
        current = os.lstat(path)
        return bool(
            stat.S_ISREG(held.st_mode)
            and stat.S_ISREG(current.st_mode)
            and not stat.S_ISLNK(current.st_mode)
            and not (int(getattr(current, "st_file_attributes", 0)) & 0x400)
            and (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)
            and held.st_size == current.st_size == artifact.byte_size
        )

    try:
        try:
            # A same-volume hard link is an atomic CreateNew publication of the
            # already fsynced, same-descriptor-validated pending object.
            os.link(pending, path, follow_symlinks=False)
        except Exception:
            # Once the atomic publication call has been entered, an exception
            # cannot distinguish "failed before link" from "linked, then
            # reported failure".  Lock the requested terminal immediately;
            # recovery observation may itself fail and must never authorize an
            # opposite terminal.  This deliberately prefers a pending-only
            # residue over a possible READY+FAILED or WAIT+FAILED pair.
            lifecycle._phase = terminal_phase
            try:
                exact_publish = published_object_matches()
            except Exception:
                exact_publish = False
            if not exact_publish:
                artifact.pending_path = None
                artifact.handle.close()
                raise
        # This assignment is the first operation after the atomic publication
        # call (or its exact same-object recovery) and cannot fail.  All later
        # checks and pending cleanup are therefore unable to produce an
        # opposite terminal receipt.
        lifecycle._phase = terminal_phase
        if not published_object_matches():
            _fail(
                "TERMINAL_PUBLISH_FAILED",
                "published terminal name differs from the held pending object",
            )
        return artifact
    except Exception:
        if lifecycle.phase is terminal_phase:
            with suppress(Exception):
                artifact.close()
        else:
            artifact.pending_path = None
            with suppress(Exception):
                artifact.handle.close()
        raise


def run_live_supervisor() -> dict[str, object]:
    """Run one fixed, zero-argument protected R8 live episode."""

    _assert_production_runtime_context()
    with _single_supervisor_mutex():
        return _run_live_supervisor_episode()


def _run_live_supervisor_episode() -> dict[str, object]:
    from . import live_preflight
    from .adapters import open_collector_jsonl_snapshot
    from .collector import collect_transport_to_jsonl_handle
    from .live_engineer_session import (
        build_live_engineer_session,
        replay_live_engineer_session,
        validate_live_engineer_session,
        write_live_engineer_session_handle,
    )
    from .telemetry import SourceKind

    runtime: AdmittedProtectedRuntime | None = None
    simulator: SimulatorProcess | None = None
    started_artifact: SealedArtifact | None = None
    preflight_handle: BinaryIO | None = None
    preflight_artifact: SealedArtifact | None = None
    capture_handle: BinaryIO | None = None
    analysis_handle: BinaryIO | None = None
    lifecycle = _SupervisorLifecycle()
    run_id = ""
    started_at = ""
    preflight_receipt: dict[str, object] | None = None
    capture_identity: CaptureHandleIdentity | None = None
    analysis_summary: dict[str, object] | None = None
    storage: dict[str, object] | None = None
    state_root: Path | None = None

    def cleanup() -> None:
        for resource in (
            analysis_handle,
            capture_handle,
            preflight_artifact,
            preflight_handle,
            started_artifact,
            simulator,
            runtime,
        ):
            if resource is not None:
                with suppress(Exception):
                    resource.close()

    try:
        runtime = admit_installed_runtime()
        _assert_runtime_token_admission(runtime.token_admission)
        state_root = _ensure_state_root()
        run_id = _new_unique_run_id(state_root)
        started_at = _utc_now_text()
        storage = _storage_admission(
            state_root, include_preflight_allowance=True
        )

        started_payload = {
            "advisor_only": True,
            "attestation_status": ATTESTATION_STATUS,
            "contract_version": "windows-live-supervisor-start-v1",
            "install_admission": runtime.admission.to_dict(),
            "recommendations": [],
            "run_id": run_id,
            "runtime_token_admission": runtime.token_admission.to_dict(),
            "simulator_identity": None,
            "started_at_utc": started_at,
            "status": "STARTED",
            "storage_admission": storage,
            "vehicle_control_enabled": False,
        }
        started_artifact = _write_json_exclusive(
            state_root / f"started-{run_id}.json", started_payload
        )
        lifecycle.move(SupervisorPhase.STARTING, SupervisorPhase.PREFLIGHT)

        if storage["status"] != "PASS":
            wait_payload = _terminal_payload(
                status="WAIT",
                run_id=run_id,
                started_at_utc=started_at,
                simulator=simulator,
                runtime=runtime,
                preflight=None,
                preflight_artifact=None,
                capture=None,
                analysis=None,
                storage_admission=storage,
                wait_reasons=("WAIT_STORAGE",),
            )
            wait_artifact = _commit_terminal_artifact(
                lifecycle,
                expected_phase=SupervisorPhase.PREFLIGHT,
                terminal_phase=SupervisorPhase.WAIT,
                path=state_root / f"wait-{run_id}.json",
                payload=wait_payload,
            )
            with suppress(Exception):
                wait_artifact.close()
            cleanup()
            return wait_payload

        try:
            simulator = _open_single_simulator_process()
        except LiveSupervisorError as exc:
            if exc.code != "SIMULATOR_UNAVAILABLE":
                raise
            wait_payload = _terminal_payload(
                status="WAIT",
                run_id=run_id,
                started_at_utc=started_at,
                simulator=None,
                runtime=runtime,
                preflight=None,
                preflight_artifact=None,
                capture=None,
                analysis=None,
                storage_admission=storage,
                wait_reasons=("WAIT_SIMULATOR",),
            )
            wait_artifact = _commit_terminal_artifact(
                lifecycle,
                expected_phase=SupervisorPhase.PREFLIGHT,
                terminal_phase=SupervisorPhase.WAIT,
                path=state_root / f"wait-{run_id}.json",
                payload=wait_payload,
            )
            with suppress(Exception):
                wait_artifact.close()
            cleanup()
            return wait_payload
        simulator.assert_same_running_process()

        preflight_path = state_root / f"preflight-{run_id}.jsonl"
        preflight_handle = _open_exclusive_binary(preflight_path)
        simulator.assert_same_running_process()
        preflight_receipt = (
            live_preflight._run_windows_live_preflight_handle_supervisor_only(
                preflight_handle,
                capture_filename=preflight_path.name,
                source_id=_SOURCE_ID,
                session_id=f"preflight-{run_id}",
                simulator_identity=simulator.identity(),
                wait_seconds=_SDK_WAIT_SECONDS,
                duration_s=_PREFLIGHT_DURATION_S,
                poll_seconds=_POLL_SECONDS,
                stale_after_s=_STALE_AFTER_S,
            )
        )
        simulator.assert_same_running_process()
        _assert_handle_matches_path(preflight_handle, preflight_path)
        preflight_artifact = _write_json_exclusive(
            state_root / f"preflight-{run_id}-receipt.json",
            preflight_receipt,
        )

        if preflight_receipt.get("status") == "WAIT":
            wait_payload = _terminal_payload(
                status="WAIT",
                run_id=run_id,
                started_at_utc=started_at,
                simulator=simulator,
                runtime=runtime,
                preflight=preflight_receipt,
                preflight_artifact=preflight_artifact,
                capture=None,
                analysis=None,
                storage_admission=storage,
                wait_reasons=tuple(
                    str(reason) for reason in preflight_receipt.get("wait_reasons", [])
                ),
            )
            wait_artifact = _commit_terminal_artifact(
                lifecycle,
                expected_phase=SupervisorPhase.PREFLIGHT,
                terminal_phase=SupervisorPhase.WAIT,
                path=state_root / f"wait-{run_id}.json",
                payload=wait_payload,
            )
            with suppress(Exception):
                wait_artifact.close()
            cleanup()
            return wait_payload

        preflight_receipt_sha, preflight_semantic_sha = _preflight_pass_fields(
            preflight_receipt
        )
        storage = _storage_admission(
            state_root, include_preflight_allowance=False
        )
        if storage["status"] != "PASS":
            wait_payload = _terminal_payload(
                status="WAIT",
                run_id=run_id,
                started_at_utc=started_at,
                simulator=simulator,
                runtime=runtime,
                preflight=preflight_receipt,
                preflight_artifact=preflight_artifact,
                capture=None,
                analysis=None,
                storage_admission=storage,
                wait_reasons=("WAIT_STORAGE",),
            )
            wait_artifact = _commit_terminal_artifact(
                lifecycle,
                expected_phase=SupervisorPhase.PREFLIGHT,
                terminal_phase=SupervisorPhase.WAIT,
                path=state_root / f"wait-{run_id}.json",
                payload=wait_payload,
            )
            with suppress(Exception):
                wait_artifact.close()
            cleanup()
            return wait_payload

        lifecycle.move(SupervisorPhase.PREFLIGHT, SupervisorPhase.CAPTURING)
        simulator.assert_same_running_process()
        live_preflight._assert_frozen_transport_class()
        transport = live_preflight._FROZEN_WINDOWS_TRANSPORT_TYPE()
        live_preflight._assert_frozen_transport_class()
        capture_path = state_root / f"live-{run_id}.jsonl"
        capture_handle = _open_exclusive_binary(capture_path)
        collector_receipt = collect_transport_to_jsonl_handle(
            transport,
            capture_handle,
            source_id=_SOURCE_ID,
            session_id=run_id,
            expected_source_kind=SourceKind.SDK_LIVE,
            wait_seconds=_SDK_WAIT_SECONDS,
            duration_s=_CAPTURE_DURATION_S,
            poll_seconds=_POLL_SECONDS,
            fields=None,
            stale_after_s=_STALE_AFTER_S,
            include_driver_info=False,
            fsync_each_record=True,
            max_output_bytes=_CAPTURE_HARD_MAX_BYTES,
        )
        live_preflight._assert_frozen_transport_class()
        simulator.assert_same_running_process()
        capture_identity = _capture_handle_identity(
            capture_handle,
            filename=capture_path.name,
            path=capture_path,
        )

        lifecycle.move(SupervisorPhase.CAPTURING, SupervisorPhase.ANALYZING)
        authority = build_live_analysis_authority(
            run_id=run_id,
            capture=capture_identity,
            simulator_identity=simulator.identity(),
            install_admission=runtime.admission,
            runtime_token_admission=runtime.token_admission,
            preflight_receipt_sha256=preflight_receipt_sha,
            preflight_production_semantic_digest=preflight_semantic_sha,
        )
        with open_collector_jsonl_snapshot(
            capture_handle, stale_after_s=_STALE_AFTER_S
        ) as run:
            _validate_full_collector_receipt(
                collector_receipt,
                run.evidence.to_dict(),
                source_id=_SOURCE_ID,
                session_id=run_id,
            )
            live_receipt = build_live_engineer_session(
                run,
                capture_handle,
                analysis_authority=authority,
                stale_after_s=_STALE_AFTER_S,
            )
        validated = validate_live_engineer_session(
            live_receipt,
            expected_live_engineer_session_sha256=str(
                live_receipt["live_engineer_session_sha256"]
            ),
            expected_capture_sha256=capture_identity.sha256,
            expected_capture_byte_size=capture_identity.byte_size,
            expected_analysis_authority_sha256=str(authority["authority_sha256"]),
        )
        with open_collector_jsonl_snapshot(
            capture_handle, stale_after_s=_STALE_AFTER_S
        ) as replay_run:
            replayed = replay_live_engineer_session(
                replay_run,
                capture_handle,
                validated,
                stale_after_s=_STALE_AFTER_S,
            )
        if replayed != validated:
            _fail("LIVE_ANALYSIS_MISMATCH", "live engineer-session replay differs")

        analysis_path = state_root / f"analysis-{run_id}.json"
        analysis_handle = _open_exclusive_binary(analysis_path)
        analysis_write = write_live_engineer_session_handle(
            analysis_handle, validated
        )
        _assert_handle_matches_path(
            analysis_handle,
            analysis_path,
            expected_size=int(analysis_write["artifact_byte_size"]),
        )
        analysis_summary = {
            "analysis_authority_sha256": authority["authority_sha256"],
            "artifact_byte_size": analysis_write["artifact_byte_size"],
            "artifact_file": analysis_path.name,
            "artifact_sha256": analysis_write["artifact_sha256"],
            "live_engineer_session_sha256": analysis_write[
                "live_engineer_session_sha256"
            ],
            "recommendation_count": 0,
            "recommendations": [],
            "status": validated["status"],
            "tactical_output_count": 0,
        }

        simulator.assert_same_running_process()
        _revalidate_protected_runtime(runtime)
        _admit_state_root()
        _assert_handle_matches_path(started_artifact.handle, started_artifact.path)
        _assert_handle_matches_path(preflight_handle, preflight_path)
        _assert_handle_matches_path(preflight_artifact.handle, preflight_artifact.path)
        final_capture = _capture_handle_identity(
            capture_handle,
            filename=capture_path.name,
            path=capture_path,
        )
        if final_capture != capture_identity:
            _fail("CAPTURE_CHANGED", "canonical capture changed during analysis")
        _assert_handle_matches_path(
            analysis_handle,
            analysis_path,
            expected_size=int(analysis_write["artifact_byte_size"]),
        )
        ready_payload = _terminal_payload(
            status="READY",
            run_id=run_id,
            started_at_utc=started_at,
            simulator=simulator,
            runtime=runtime,
            preflight=preflight_receipt,
            preflight_artifact=preflight_artifact,
            capture=capture_identity,
            analysis=analysis_summary,
            storage_admission=storage,
            wait_reasons=(),
        )
        ready_artifact = _commit_terminal_artifact(
            lifecycle,
            expected_phase=SupervisorPhase.ANALYZING,
            terminal_phase=SupervisorPhase.READY,
            path=state_root / f"ready-{run_id}.json",
            payload=ready_payload,
        )
        # Only non-failing cleanup follows the terminal commit.
        with suppress(Exception):
            ready_artifact.close()
        cleanup()
        return ready_payload
    except Exception as exc:
        if lifecycle.phase in {SupervisorPhase.READY, SupervisorPhase.WAIT}:
            cleanup()
            raise
        failure = (
            exc
            if isinstance(exc, LiveSupervisorError)
            else LiveSupervisorError(
                f"{type(exc).__name__.upper()}_FAILED",
                str(exc) or type(exc).__name__,
            )
        )
        if runtime is not None and state_root is not None and run_id:
            failed_payload = _terminal_payload(
                status="FAILED",
                run_id=run_id,
                started_at_utc=started_at,
                simulator=simulator,
                runtime=runtime,
                preflight=preflight_receipt,
                preflight_artifact=preflight_artifact,
                capture=capture_identity,
                analysis=analysis_summary,
                storage_admission=storage,
                wait_reasons=(),
                error=failure,
            )
            try:
                failed_artifact = _write_json_exclusive(
                    state_root / f"failed-{run_id}.json", failed_payload
                )
                lifecycle._phase = SupervisorPhase.FAILED
                with suppress(Exception):
                    failed_artifact.close()
            except Exception as receipt_error:
                failure.add_note(
                    "failed receipt commit also failed: "
                    f"{type(receipt_error).__name__}: {receipt_error}"
                )
        cleanup()
        raise failure from (None if failure is exc else exc)


__all__ = [
    "ATTESTATION_STATUS",
    "CODE_TRUST_MODEL",
    "FIXED_STATE_ROOT",
    "FIXED_VERSION_ROOT",
    "INSTALL_CONTRACT_VERSION",
    "InstallAdmission",
    "LiveSupervisorError",
    "SUPERVISOR_CONTRACT_VERSION",
    "run_live_supervisor",
]
