from __future__ import annotations

import hashlib
import inspect
import json
import ntpath
import types
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import pytest

from iracing_ai_engineer import collector, live_preflight
from iracing_ai_engineer import live_supervisor as supervisor


def test_windows_normalized_path_accepts_str_and_concrete_path_only():
    concrete_path = Path(r"C:\Program Files\AEIS\releases\collector-v4-0.1.0-r8")
    expected = ntpath.normcase(ntpath.normpath(str(concrete_path)))

    assert supervisor._windows_normalized_path(str(concrete_path)) == expected
    assert supervisor._windows_normalized_path(concrete_path) == expected

    class CustomPathLike:
        def __fspath__(self) -> str:
            return str(concrete_path)

    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor._windows_normalized_path(CustomPathLike())
    assert failure.value.code == "CODE_ROOT_INVALID"


def test_security_ace_order_is_semantic_not_raw_dacl_order():
    expected = supervisor._expected_aces("DIRECTORY")

    assert supervisor._canonical_security_aces(tuple(reversed(expected))) == expected


def _security_objects() -> list[supervisor.SecurityObject]:
    return [
        supervisor.SecurityObject(
            path=".",
            object_type="DIRECTORY",
            owner_sid=supervisor.BUILTIN_ADMINISTRATORS_SID,
            dacl_protected=True,
            aces=supervisor._expected_aces("DIRECTORY"),
        ),
        supervisor.SecurityObject(
            path="runtime/python.exe",
            object_type="FILE",
            owner_sid=supervisor.BUILTIN_ADMINISTRATORS_SID,
            dacl_protected=True,
            aces=supervisor._expected_aces("FILE"),
        ),
    ]


def _security_sha(objects: list[supervisor.SecurityObject]) -> str:
    canonical = sorted(
        (item.to_dict() for item in objects), key=lambda x: x["path"].upper()
    )
    return hashlib.sha256(supervisor._security_tree_bytes(canonical)).hexdigest()


def _security_profile() -> dict[str, object]:
    return {
        "administrators_sid": supervisor.BUILTIN_ADMINISTRATORS_SID,
        "contract_version": supervisor.SECURITY_PROFILE_CONTRACT_VERSION,
        "dacl_protected": True,
        "directory_aces": [
            ace.to_dict() for ace in supervisor._expected_aces("DIRECTORY")
        ],
        "directory_sddl": supervisor.DIRECTORY_SDDL,
        "file_aces": [ace.to_dict() for ace in supervisor._expected_aces("FILE")],
        "file_sddl": supervisor.FILE_SDDL,
        "owner_sid": supervisor.BUILTIN_ADMINISTRATORS_SID,
        "root_aces": [
            ace.to_dict() for ace in supervisor._expected_aces("DIRECTORY")
        ],
        "root_sddl": supervisor.DIRECTORY_SDDL,
        "runtime_user_sid": supervisor.FIXED_RUNTIME_USER_SID,
        "semantic_ace_records_authoritative": True,
        "system_sid": supervisor.LOCAL_SYSTEM_SID,
    }


def _ancestor_admission() -> dict[str, object]:
    paths = [
        r"C:\Program Files",
        r"C:\Program Files\AEIS",
        r"C:\Program Files\AEIS\releases",
        str(supervisor.FIXED_VERSION_ROOT),
    ]
    roles = [
        "SYSTEM_CODE_PARENT",
        "VENDOR_CODE_ROOT",
        "RELEASES_CODE_ROOT",
        "VERSION_CODE_ROOT",
    ]
    profiles = [
        "WINDOWS_SYSTEM_PROGRAM_FILES_PARENT_V1",
        supervisor.SECURITY_PROFILE,
        supervisor.SECURITY_PROFILE,
        supervisor.SECURITY_PROFILE,
    ]
    owners = [
        supervisor.TRUSTED_INSTALLER_SID,
        supervisor.BUILTIN_ADMINISTRATORS_SID,
        supervisor.BUILTIN_ADMINISTRATORS_SID,
        supervisor.BUILTIN_ADMINISTRATORS_SID,
    ]
    return {
        "contract_version": supervisor.ANCESTOR_ADMISSION_CONTRACT_VERSION,
        "objects": [
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
        ],
        "status": "PASS",
    }


def _threat_boundary() -> dict[str, object]:
    return {
        "contract_version": supervisor.THREAT_BOUNDARY_CONTRACT_VERSION,
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
        "live_authenticity": supervisor.ATTESTATION_STATUS,
        "protected_subject_sid": supervisor.FIXED_RUNTIME_USER_SID,
        "protects": [
            "DISK_CODE_AND_PATH_TOCTOU_PLUS_ACCIDENTAL_CONCURRENT_REPLACEMENT",
        ],
        "status": "PASS",
    }


def _install_receipt() -> dict[str, object]:
    sha = "1" * 64
    return {
        key: None for key in supervisor._INSTALL_V2_KEYS
    } | {
        "advisor_only": True,
        "ancestor_admission": _ancestor_admission(),
        "code_owner_sid": supervisor.BUILTIN_ADMINISTRATORS_SID,
        "code_root": str(supervisor.FIXED_VERSION_ROOT),
        "code_trust_model": supervisor.CODE_TRUST_MODEL,
        "contract_version": supervisor.INSTALL_CONTRACT_VERSION,
        "dacl_protected": True,
        "development_smoke_profile_byte_size": 662,
        "development_smoke_profile_file": supervisor.DEV_SMOKE_PROFILE_FILE,
        "development_smoke_profile_sha256": supervisor.DEV_SMOKE_PROFILE_SHA256,
        "embedded_python_file": "release-inputs\\python-embed.zip",
        "embedded_python_sha256": sha,
        "import_smoke": "PASS",
        "install_directory": str(supervisor.FIXED_VERSION_ROOT),
        "install_root": supervisor.FIXED_INSTALL_ROOT,
        "installer_identity_admission": {
            "elevated": True,
            "status": "PASS",
            "user_sid": supervisor.FIXED_RUNTIME_USER_SID,
        },
        "live_capture_validated": False,
        "package_version": "0.1.0",
        "project_wheel_file": "iracing_ai_engineer-0.1.0-py3-none-any.whl",
        "project_wheel_sha256": sha,
        "python_architecture": "AMD64",
        "python_bits": 64,
        "python_path_config_file": "runtime\\python312._pth",
        "python_path_config_sha256": (
            "0971dbaa7c895646919cc695b690af8f135aa50e55b876d9ef46913513966890"
        ),
        "python_version": "3.12.10",
        "record_closure": "PASS",
        "release_manifest_file": "release-inputs\\windows-release-manifest-v2.json",
        "release_manifest_sha256": sha,
        "runtime_closure": "PASS",
        "runtime_file_count": 42,
        "runtime_manifest_file": supervisor.FIXED_RUNTIME_MANIFEST,
        "runtime_manifest_primitives_file": (
            "release-inputs\\make_release_runtime_manifest.py"
        ),
        "runtime_manifest_primitives_sha256": sha,
        "runtime_manifest_self_sha256": sha,
        "runtime_manifest_sha256": sha,
        "runtime_manifest_tool_file": (
            "release-inputs\\make_release_runtime_manifest_v3.py"
        ),
        "runtime_manifest_tool_sha256": sha,
        "runtime_python_file": "runtime\\python.exe",
        "runtime_total_bytes": 1234,
        "runtime_tree_sha256": sha,
        "runtime_user_sid": supervisor.FIXED_RUNTIME_USER_SID,
        "security_descriptor_profile": supervisor.SECURITY_PROFILE,
        "security_object_count": 2,
        "security_profile": _security_profile(),
        "security_tree_algorithm": supervisor.SECURITY_TREE_ALGORITHM,
        "security_tree_sha256": sha,
        "supervisor_task_admitter_file": supervisor.SUPERVISOR_TASK_ADMITTER_FILE,
        "supervisor_task_admitter_sha256": sha,
        "supervisor_task_installer_file": supervisor.SUPERVISOR_TASK_INSTALLER_FILE,
        "supervisor_task_installer_sha256": sha,
        "target": "cp312-cp312-win_amd64",
        "threat_model": supervisor.ATTESTATION_STATUS,
        "threat_boundary": _threat_boundary(),
        "version_directory": supervisor.FIXED_INSTALL_DIRECTORY,
        "wheel_install_receipt_file": "wheel-install-receipt.json",
        "wheel_install_receipt_sha256": sha,
        "wheel_installer_tool_file": "release-inputs\\install_wheels.py",
        "wheel_installer_tool_sha256": sha,
        "wheelhouse_manifest_file": "release-inputs\\wheelhouse-manifest.json",
        "wheelhouse_manifest_sha256": sha,
        "writable_state_root": str(supervisor.FIXED_STATE_ROOT),
    }


def _encoded_install(value: dict[str, object]) -> bytes:
    return supervisor._canonical_json(value)


def _runtime_manifest() -> dict[str, object]:
    layout = {
        "irsdk_module": "runtime/Lib/site-packages/irsdk.py",
        "project_dist_info_directory": (
            "runtime/Lib/site-packages/iracing_ai_engineer-0.1.0.dist-info"
        ),
        "project_package_directory": "runtime/Lib/site-packages/iracing_ai_engineer",
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
    paths_and_roles = {
        "iracing_ai_engineer-0.1.0-py3-none-any.whl": "project_wheel",
        "runtime/Lib/site-packages/irsdk.py": "irsdk_module",
        "runtime/python.exe": "python_executable",
        "runtime/python312._pth": "python_path_config",
        "runtime/python312.dll": "python_core_dll",
        "runtime/python312.zip": "stdlib_archive",
        "runtime/share/man/man1/ttx.1": "wheel_data_file",
    }
    files = [
        {"path": path, "role": role, "sha256": "a" * 64, "size": 1}
        for path, role in sorted(
            paths_and_roles.items(), key=lambda item: (item[0].casefold(), item[0])
        )
    ]
    base: dict[str, object] = {
        "contract_version": supervisor.RUNTIME_CONTRACT_VERSION,
        "embedded_distribution": {
            "archive_member_count": 35,
            "archive_member_tree_sha256": "b" * 64,
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
        },
        "file_count": len(files),
        "files": files,
        "generator_binding": {
            "base_primitives_file": "make_release_runtime_manifest.py",
            "base_primitives_sha256": (
                "6eb513774a83ab1aec99a9be4b677567b843fd64248b5765eb3a7b5fddb65d53"
            ),
            "binding_role": "SEMANTIC_IMPLEMENTATION_INPUTS_NOT_EXTERNAL_AUTHORITY",
            "generator_file": "make_release_runtime_manifest_v3.py",
            "generator_sha256": "c" * 64,
        },
        "layout": layout,
        "project_version": "0.1.0",
        "total_bytes": len(files),
        "tree_sha256": hashlib.sha256(
            supervisor._canonical_json(files, newline=True)
        ).hexdigest(),
    }
    return {
        **base,
        "manifest_self_sha256": hashlib.sha256(
            supervisor._canonical_json(base, newline=True)
        ).hexdigest(),
    }


def test_install_v2_is_exact_and_never_accepts_v1_or_extra_keys():
    receipt = _install_receipt()
    payload = _encoded_install(receipt)
    parsed = supervisor._validate_install_v2_receipt(
        payload,
        expected_install_sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert parsed == receipt

    old = dict(receipt)
    old["contract_version"] = "windows-embedded-collector-install-v1"
    old_payload = _encoded_install(old)
    with pytest.raises(supervisor.LiveSupervisorError) as old_error:
        supervisor._validate_install_v2_receipt(
            old_payload,
            expected_install_sha256=hashlib.sha256(old_payload).hexdigest(),
        )
    assert old_error.value.code == "INSTALL_MANIFEST_INVALID"

    extra = dict(receipt)
    extra["legacy_fallback"] = True
    extra_payload = _encoded_install(extra)
    with pytest.raises(supervisor.LiveSupervisorError) as extra_error:
        supervisor._validate_install_v2_receipt(
            extra_payload,
            expected_install_sha256=hashlib.sha256(extra_payload).hexdigest(),
        )
    assert extra_error.value.code == "SCHEMA_INVALID"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing_role", "SCHEMA_INVALID"),
        ("wrong_role", "INSTALL_MANIFEST_INVALID"),
        ("wrong_path", "INSTALL_MANIFEST_INVALID"),
        ("malformed_hash", "SCHEMA_INVALID"),
    ],
)
def test_install_v2_rejects_invalid_supervisor_task_role_bindings(
    mutation: str, expected_code: str
):
    receipt = _install_receipt()
    if mutation == "missing_role":
        receipt.pop("supervisor_task_admitter_file")
    elif mutation == "wrong_role":
        receipt["supervisor_task_installer_file"] = (
            supervisor.SUPERVISOR_TASK_ADMITTER_FILE
        )
    elif mutation == "wrong_path":
        receipt["supervisor_task_admitter_file"] = (
            "release-inputs\\..\\admit_aeis_r8_supervisor_task_v5.ps1"
        )
    else:
        receipt["supervisor_task_installer_sha256"] = "Z" * 64
    payload = _encoded_install(receipt)
    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor._validate_install_v2_receipt(
            payload,
            expected_install_sha256=hashlib.sha256(payload).hexdigest(),
        )
    assert failure.value.code == expected_code


def test_install_v2_rejects_valid_but_wrong_task_tool_hash_after_held_read():
    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor._assert_install_reference_digest(
            "release-inputs/install_aeis_r8_supervisor_task_v5.ps1",
            expected="1" * 64,
            observed="2" * 64,
        )
    assert failure.value.code == "INSTALL_REFERENCE_MISMATCH"


def test_install_parser_rejects_duplicate_and_case_colliding_keys():
    for payload in (
        b'{"contract_version":"a","contract_version":"b"}',
        b'{"contract_version":"a","Contract_Version":"b"}',
    ):
        with pytest.raises(supervisor.LiveSupervisorError) as failure:
            supervisor._strict_json_bytes(payload, "attack")
        assert failure.value.code == "JSON_INVALID"


def test_runtime_manifest_uses_generator_newline_hash_semantics():
    manifest = _runtime_manifest()
    payload = supervisor._canonical_json(manifest, newline=True)
    assert supervisor._validate_runtime_manifest(
        payload, expected_sha256=hashlib.sha256(payload).hexdigest()
    ) == manifest

    wrong = dict(manifest)
    base = dict(wrong)
    base.pop("manifest_self_sha256")
    wrong["manifest_self_sha256"] = hashlib.sha256(
        supervisor._canonical_json(base)
    ).hexdigest()
    wrong_payload = supervisor._canonical_json(wrong, newline=True)
    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor._validate_runtime_manifest(
            wrong_payload,
            expected_sha256=hashlib.sha256(wrong_payload).hexdigest(),
        )
    assert failure.value.code == "RUNTIME_MANIFEST_INVALID"


def test_security_tree_cross_language_vector_is_frozen():
    # This public vector binds the documented placeholder SID, not a private
    # deployment receipt. Canonical property/ACE ordering is unchanged.
    objects = sorted(
        (item.to_dict() for item in _security_objects()),
        key=lambda item: item["path"].upper(),
    )
    payload = supervisor._security_tree_bytes(objects)
    assert len(payload) == 988
    assert hashlib.sha256(payload).hexdigest() == (
        "798f206af3e42025fb0b90e3f9686339b4af921daf31380a93f772ea9a1bbb3e"
    )


def test_program_files_acl_policy_allows_only_safe_public_and_exact_creator_owner():
    safe = (
        (0, 0, 0x1F01FF, supervisor.TRUSTED_INSTALLER_SID),
        (0, 0, 0x1200A9, "S-1-5-32-545"),
        (0, 0x0B, 0xA0000000, "S-1-5-32-545"),
        (0, 0x0B, 0x10000000, "S-1-3-0"),
    )
    supervisor._validate_program_files_dacl(safe)

    with pytest.raises(supervisor.LiveSupervisorError) as public_write:
        supervisor._validate_program_files_dacl(
            (*safe, (0, 0, 0x00000002, "S-1-1-0"))
        )
    assert public_write.value.code == "ANCESTOR_ADMISSION_FAILED"

    with pytest.raises(supervisor.LiveSupervisorError):
        supervisor._validate_program_files_dacl(
            ((0, 0x03, 0x10000000, "S-1-3-0"),)
        )


def test_security_tree_requires_exact_owner_protected_dacl_and_hash():
    objects = _security_objects()
    expected_sha = _security_sha(objects)
    observed = supervisor._validate_security_tree(
        objects,
        expected_count=2,
        expected_sha256=expected_sha,
    )
    assert len(observed) == 2

    bad_owner = list(objects)
    bad_owner[1] = supervisor.SecurityObject(
        path=bad_owner[1].path,
        object_type=bad_owner[1].object_type,
        owner_sid=supervisor.FIXED_RUNTIME_USER_SID,
        dacl_protected=True,
        aces=bad_owner[1].aces,
    )
    with pytest.raises(supervisor.LiveSupervisorError) as owner_error:
        supervisor._validate_security_tree(
            bad_owner,
            expected_count=2,
            expected_sha256=_security_sha(bad_owner),
        )
    assert owner_error.value.code == "SECURITY_TREE_INVALID"

    broad_write = list(objects)
    broad_write[1] = supervisor.SecurityObject(
        path=broad_write[1].path,
        object_type=broad_write[1].object_type,
        owner_sid=broad_write[1].owner_sid,
        dacl_protected=True,
        aces=(
            *broad_write[1].aces,
            supervisor.SecurityAce("S-1-1-0", 0x2, 0),
        ),
    )
    with pytest.raises(supervisor.LiveSupervisorError) as dacl_error:
        supervisor._validate_security_tree(
            broad_write,
            expected_count=2,
            expected_sha256=_security_sha(broad_write),
        )
    assert dacl_error.value.code == "SECURITY_TREE_INVALID"


def test_same_handle_preflight_snapshot_never_closes_or_reopens_path(tmp_path: Path):
    capture = tmp_path / "preflight-fixture.jsonl"
    with capture.open("x+b", buffering=0) as handle:
        handle.write(b'{"record_type":"fixture"}\n')
        handle.flush()
        with live_preflight._sealed_capture_handle(
            handle, filename=capture.name
        ) as snapshot:
            assert snapshot.capture_identity["filename"] == capture.name
            assert snapshot.snapshot_method == (
                "CALLER_OWNED_SINGLE_PROCESS_FILE_HANDLE_V1"
            )
            assert snapshot.handle.readline().startswith('{"record_type"')
        assert not handle.closed


def test_same_handle_preflight_detects_in_process_mutation(tmp_path: Path):
    capture = tmp_path / "preflight-mutation.jsonl"
    with capture.open("x+b", buffering=0) as handle:
        handle.write(b'{"record_type":"fixture"}\n')
        handle.flush()
        with pytest.raises(live_preflight.LivePreflightError, match="changed"):
            with live_preflight._sealed_capture_handle(
                handle, filename=capture.name
            ) as snapshot:
                snapshot.handle.seek(0, 2)
                snapshot.handle.write("{}\n")


def test_receipt_writer_is_create_new_same_fd_and_failure_leaves_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "receipt.json"
    artifact = supervisor._write_json_exclusive(path, {"status": "WAIT"})
    try:
        assert artifact.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert artifact.byte_size == path.stat().st_size
        assert not artifact.handle.closed
    finally:
        artifact.close()
    with pytest.raises(supervisor.LiveSupervisorError) as exists:
        supervisor._write_json_exclusive(path, {"status": "WAIT"})
    assert exists.value.code == "EXCLUSIVE_CREATE_FAILED"

    failed = tmp_path / "failed-write.json"

    def injected_fsync(_descriptor: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(supervisor.os, "fsync", injected_fsync)
    with pytest.raises(OSError, match="injected"):
        supervisor._write_json_exclusive(failed, {"status": "FAILED"})
    assert failed.exists()
    assert failed.stat().st_size > 0


def test_ready_and_failed_are_mutually_exclusive_even_after_cleanup_failure():
    lifecycle = supervisor._SupervisorLifecycle()
    lifecycle.move(
        supervisor.SupervisorPhase.STARTING,
        supervisor.SupervisorPhase.PREFLIGHT,
    )
    lifecycle.move(
        supervisor.SupervisorPhase.PREFLIGHT,
        supervisor.SupervisorPhase.CAPTURING,
    )
    lifecycle.move(
        supervisor.SupervisorPhase.CAPTURING,
        supervisor.SupervisorPhase.ANALYZING,
    )
    lifecycle.commit_ready()
    assert lifecycle.phase is supervisor.SupervisorPhase.READY


def test_same_descriptor_ready_commit_precedes_any_cleanup_failure(tmp_path: Path):
    lifecycle = supervisor._SupervisorLifecycle()
    lifecycle.move(
        supervisor.SupervisorPhase.STARTING, supervisor.SupervisorPhase.PREFLIGHT
    )
    lifecycle.move(
        supervisor.SupervisorPhase.PREFLIGHT, supervisor.SupervisorPhase.CAPTURING
    )
    lifecycle.move(
        supervisor.SupervisorPhase.CAPTURING, supervisor.SupervisorPhase.ANALYZING
    )
    ready = tmp_path / "ready-20260823T120000Z.json"
    artifact = supervisor._commit_terminal_artifact(
        lifecycle,
        expected_phase=supervisor.SupervisorPhase.ANALYZING,
        terminal_phase=supervisor.SupervisorPhase.READY,
        path=ready,
        payload={"status": "READY"},
    )
    try:
        assert lifecycle.phase is supervisor.SupervisorPhase.READY
        artifact.handle.seek(0)
        assert json.loads(artifact.handle.read().decode("utf-8")) == {
            "status": "READY"
        }
        with pytest.raises(supervisor.LiveSupervisorError) as conflict:
            lifecycle.commit_failed()
        assert conflict.value.code == "TERMINAL_STATE_CONFLICT"
        assert not (tmp_path / "failed-20260823T120000Z.json").exists()
    finally:
        artifact.close()
    with pytest.raises(supervisor.LiveSupervisorError) as conflict:
        lifecycle.commit_failed()
    assert conflict.value.code == "TERMINAL_STATE_CONFLICT"
    assert lifecycle.phase is supervisor.SupervisorPhase.READY


def _analyzing_lifecycle() -> supervisor._SupervisorLifecycle:
    lifecycle = supervisor._SupervisorLifecycle()
    lifecycle.move(
        supervisor.SupervisorPhase.STARTING, supervisor.SupervisorPhase.PREFLIGHT
    )
    lifecycle.move(
        supervisor.SupervisorPhase.PREFLIGHT, supervisor.SupervisorPhase.CAPTURING
    )
    lifecycle.move(
        supervisor.SupervisorPhase.CAPTURING, supervisor.SupervisorPhase.ANALYZING
    )
    return lifecycle


def _consumer_terminal_paths(tmp_path: Path, run_id: str) -> list[Path]:
    return [
        path
        for status in ("ready", "wait", "failed")
        for path in tmp_path.glob(f"{status}-{run_id}.json")
    ]


@pytest.mark.parametrize("fault", ["fstat", "lstat", "constructor"])
def test_pending_post_fsync_failures_publish_only_one_failed_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
):
    run_id = "20260823T120010Z"
    ready = tmp_path / f"ready-{run_id}.json"
    failed = tmp_path / f"failed-{run_id}.json"
    lifecycle = _analyzing_lifecycle()
    armed = False
    injected = False
    real_fsync = supervisor.os.fsync
    real_fstat = supervisor.os.fstat
    real_lstat = supervisor.os.lstat
    real_artifact = supervisor.SealedArtifact

    def arming_fsync(descriptor: int) -> None:
        nonlocal armed
        real_fsync(descriptor)
        armed = True

    def maybe_fail_fstat(descriptor: int):
        nonlocal injected
        if fault == "fstat" and armed and not injected:
            injected = True
            raise OSError("injected pending post-fsync fstat failure")
        return real_fstat(descriptor)

    def maybe_fail_lstat(path):
        nonlocal injected
        if fault == "lstat" and armed and not injected:
            injected = True
            raise OSError("injected pending post-fsync lstat failure")
        return real_lstat(path)

    def maybe_fail_constructor(*args, **kwargs):
        nonlocal injected
        if fault == "constructor" and armed and not injected:
            injected = True
            raise RuntimeError("injected pending post-fsync constructor failure")
        return real_artifact(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(supervisor.os, "fsync", arming_fsync)
        patch.setattr(supervisor.os, "fstat", maybe_fail_fstat)
        patch.setattr(supervisor.os, "lstat", maybe_fail_lstat)
        patch.setattr(supervisor, "SealedArtifact", maybe_fail_constructor)
        with pytest.raises((OSError, RuntimeError), match="injected pending"):
            supervisor._commit_terminal_artifact(
                lifecycle,
                expected_phase=supervisor.SupervisorPhase.ANALYZING,
                terminal_phase=supervisor.SupervisorPhase.READY,
                path=ready,
                payload={"run_id": run_id, "status": "READY"},
            )

    assert lifecycle.phase is supervisor.SupervisorPhase.ANALYZING
    failed_artifact = supervisor._write_json_exclusive(
        failed, {"run_id": run_id, "status": "FAILED"}
    )
    failed_artifact.close()
    lifecycle.commit_failed()
    assert _consumer_terminal_paths(tmp_path, run_id) == [failed]
    assert (tmp_path / f".pending-ready-{run_id}.json").exists()


def test_post_publish_validation_failure_cannot_create_opposite_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_id = "20260823T120011Z"
    ready = tmp_path / f"ready-{run_id}.json"
    failed = tmp_path / f"failed-{run_id}.json"
    lifecycle = _analyzing_lifecycle()
    published = False
    injected = False
    real_link = supervisor.os.link
    real_lstat = supervisor.os.lstat

    def observed_link(*args, **kwargs):
        nonlocal published
        result = real_link(*args, **kwargs)
        published = True
        return result

    def fail_final_lstat_once(path):
        nonlocal injected
        if published and Path(path) == ready and not injected:
            injected = True
            raise OSError("injected post-publish validation failure")
        return real_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(supervisor.os, "link", observed_link)
        patch.setattr(supervisor.os, "lstat", fail_final_lstat_once)
        with pytest.raises(OSError, match="post-publish"):
            supervisor._commit_terminal_artifact(
                lifecycle,
                expected_phase=supervisor.SupervisorPhase.ANALYZING,
                terminal_phase=supervisor.SupervisorPhase.READY,
                path=ready,
                payload={"run_id": run_id, "status": "READY"},
            )

    assert lifecycle.phase is supervisor.SupervisorPhase.READY
    assert _consumer_terminal_paths(tmp_path, run_id) == [ready]
    assert not failed.exists()
    assert json.loads(ready.read_bytes())["status"] == "READY"


def test_pending_cleanup_failure_after_publish_cannot_create_failed_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_id = "20260823T120012Z"
    ready = tmp_path / f"ready-{run_id}.json"
    pending = tmp_path / f".pending-ready-{run_id}.json"
    lifecycle = _analyzing_lifecycle()
    artifact = supervisor._commit_terminal_artifact(
        lifecycle,
        expected_phase=supervisor.SupervisorPhase.ANALYZING,
        terminal_phase=supervisor.SupervisorPhase.READY,
        path=ready,
        payload={"run_id": run_id, "status": "READY"},
    )
    real_unlink = supervisor.os.unlink

    def fail_pending_unlink(path):
        if Path(path) == pending:
            raise OSError("injected pending cleanup failure")
        return real_unlink(path)

    with monkeypatch.context() as patch:
        patch.setattr(supervisor.os, "unlink", fail_pending_unlink)
        with pytest.raises(OSError, match="pending cleanup"):
            artifact.close()

    assert lifecycle.phase is supervisor.SupervisorPhase.READY
    assert _consumer_terminal_paths(tmp_path, run_id) == [ready]
    assert pending.exists()
    pending.unlink()


def test_ambiguous_publish_and_existing_conflict_never_create_two_terminals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_id = "20260823T120013Z"
    ready = tmp_path / f"ready-{run_id}.json"
    lifecycle = _analyzing_lifecycle()
    real_link = supervisor.os.link

    def publish_then_raise(*args, **kwargs):
        real_link(*args, **kwargs)
        raise OSError("injected ambiguous publish result")

    with monkeypatch.context() as patch:
        patch.setattr(supervisor.os, "link", publish_then_raise)
        artifact = supervisor._commit_terminal_artifact(
            lifecycle,
            expected_phase=supervisor.SupervisorPhase.ANALYZING,
            terminal_phase=supervisor.SupervisorPhase.READY,
            path=ready,
            payload={"run_id": run_id, "status": "READY"},
        )
    artifact.close()
    assert lifecycle.phase is supervisor.SupervisorPhase.READY
    assert _consumer_terminal_paths(tmp_path, run_id) == [ready]

    conflict_run = "20260823T120014Z"
    conflict_ready = tmp_path / f"ready-{conflict_run}.json"
    existing = supervisor._write_json_exclusive(
        conflict_ready, {"run_id": conflict_run, "status": "READY"}
    )
    existing.close()
    conflict_lifecycle = _analyzing_lifecycle()
    with pytest.raises(FileExistsError):
        supervisor._commit_terminal_artifact(
            conflict_lifecycle,
            expected_phase=supervisor.SupervisorPhase.ANALYZING,
            terminal_phase=supervisor.SupervisorPhase.READY,
            path=conflict_ready,
            payload={"run_id": conflict_run, "status": "READY", "different": True},
        )
    assert conflict_lifecycle.phase is supervisor.SupervisorPhase.READY
    assert _consumer_terminal_paths(tmp_path, conflict_run) == [conflict_ready]


def test_ambiguous_publish_with_persistent_observation_failure_stays_single_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_id = "20260823T120015Z"
    ready = tmp_path / f"ready-{run_id}.json"
    failed = tmp_path / f"failed-{run_id}.json"
    lifecycle = _analyzing_lifecycle()
    published = False
    real_link = supervisor.os.link
    real_lstat = supervisor.os.lstat

    def publish_then_raise(*args, **kwargs):
        nonlocal published
        real_link(*args, **kwargs)
        published = True
        raise OSError("injected ambiguous publish result")

    def persistent_final_observation_failure(path):
        if published and Path(path) == ready:
            raise OSError("injected persistent final observation failure")
        return real_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(supervisor.os, "link", publish_then_raise)
        patch.setattr(supervisor.os, "lstat", persistent_final_observation_failure)
        with pytest.raises(OSError, match="ambiguous publish"):
            supervisor._commit_terminal_artifact(
                lifecycle,
                expected_phase=supervisor.SupervisorPhase.ANALYZING,
                terminal_phase=supervisor.SupervisorPhase.READY,
                path=ready,
                payload={"run_id": run_id, "status": "READY"},
            )

    # Equivalent to the episode's outer exception guard: a terminal/ambiguous
    # publication latch prohibits creation of the opposite FAILED receipt.
    if lifecycle.phase not in {
        supervisor.SupervisorPhase.READY,
        supervisor.SupervisorPhase.WAIT,
    }:
        failed_artifact = supervisor._write_json_exclusive(
            failed, {"run_id": run_id, "status": "FAILED"}
        )
        failed_artifact.close()
        lifecycle.commit_failed()
    assert lifecycle.phase is supervisor.SupervisorPhase.READY
    assert _consumer_terminal_paths(tmp_path, run_id) == [ready]
    assert not failed.exists()


def test_storage_gate_has_exact_inclusive_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    initial_required = (
        supervisor._CAPTURE_HARD_MAX_BYTES
        + supervisor._PREFLIGHT_RESIDUE_ALLOWANCE_BYTES
        + supervisor._STATE_FREE_SPACE_RESERVE_BYTES
    )
    monkeypatch.setattr(
        supervisor.shutil,
        "disk_usage",
        lambda _path: types.SimpleNamespace(total=initial_required, used=0, free=initial_required),
    )
    exact = supervisor._storage_admission(
        tmp_path, include_preflight_allowance=True
    )
    assert exact["status"] == "PASS"
    assert exact["required_free_bytes"] == initial_required

    monkeypatch.setattr(
        supervisor.shutil,
        "disk_usage",
        lambda _path: types.SimpleNamespace(
            total=initial_required, used=1, free=initial_required - 1
        ),
    )
    assert supervisor._storage_admission(
        tmp_path, include_preflight_allowance=True
    )["status"] == "WAIT_STORAGE"

    post_required = (
        supervisor._CAPTURE_HARD_MAX_BYTES
        + supervisor._STATE_FREE_SPACE_RESERVE_BYTES
    )
    monkeypatch.setattr(
        supervisor.shutil,
        "disk_usage",
        lambda _path: types.SimpleNamespace(total=post_required, used=0, free=post_required),
    )
    post = supervisor._storage_admission(
        tmp_path, include_preflight_allowance=False
    )
    assert post["status"] == "PASS"
    assert post["preflight_residue_allowance_bytes"] == 0


def test_simulator_session_pair_accepts_any_matching_interactive_session():
    assert supervisor._validate_interactive_session_pair(2, 2) == 2
    with pytest.raises(supervisor.LiveSupervisorError) as mismatch:
        supervisor._validate_interactive_session_pair(2, 3)
    assert mismatch.value.code == "SIMULATOR_SESSION_INVALID"
    with pytest.raises(supervisor.LiveSupervisorError):
        supervisor._validate_interactive_session_pair(0, 0)


def test_runtime_token_admission_rejects_elevated_or_enabled_admin(
    monkeypatch: pytest.MonkeyPatch,
):
    safe = supervisor.RuntimeTokenAdmission(
        current_user_sid=supervisor.FIXED_RUNTIME_USER_SID,
        token_is_elevated=False,
        token_elevation_type="LIMITED",
        administrators_sid_enabled=False,
        integrity_level_rid=8192,
        least_privilege="PASS",
    )
    monkeypatch.setattr(supervisor, "_observe_windows_runtime_token", lambda: safe)
    assert supervisor._admit_windows_runtime_token() == safe

    elevated = supervisor.RuntimeTokenAdmission(
        current_user_sid=supervisor.FIXED_RUNTIME_USER_SID,
        token_is_elevated=True,
        token_elevation_type="FULL",
        administrators_sid_enabled=True,
        integrity_level_rid=12288,
        least_privilege="BLOCKED",
    )
    monkeypatch.setattr(
        supervisor, "_observe_windows_runtime_token", lambda: elevated
    )
    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor._admit_windows_runtime_token()
    assert failure.value.code == "TOKEN_ADMISSION_FAILED"


def test_collector_quality_allows_only_benign_same_tick_duplicates():
    receipt_value = {
        "completion_status": "COMPLETE",
        "duplicate_conflict_count": 0,
        "duplicate_sample_count": 4,
        "dropped_tick_count": 0,
        "event_record_count": 4,
        "frame_record_count": 6,
        "samples_seen": 10,
        "schema_change_count": 0,
        "schema_epoch_count": 1,
        "session_epoch_count": 1,
        "session_reset_count": 0,
        "stale_event_count": 0,
    }

    class Receipt:
        def to_dict(self) -> dict[str, object]:
            return dict(receipt_value)

    evidence = {
        **receipt_value,
        "capture_clock_regression_count": 0,
        "read_error_field_count": 0,
        "read_error_frame_count": 0,
        "session_id": "20260823T120000Z",
        "sim_mode": "full",
        "source_id": "source",
        "source_kind": "SDK_LIVE",
    }
    supervisor._validate_full_collector_receipt(
        Receipt(), evidence, source_id="source", session_id="20260823T120000Z"
    )
    receipt_value["event_record_count"] = 5
    evidence["event_record_count"] = 5
    with pytest.raises(supervisor.LiveSupervisorError) as unexpected_event:
        supervisor._validate_full_collector_receipt(
            Receipt(), evidence, source_id="source", session_id="20260823T120000Z"
        )
    assert unexpected_event.value.code == "COLLECTOR_NOT_ADMITTED"
    receipt_value["event_record_count"] = 4
    evidence["event_record_count"] = 4
    receipt_value["duplicate_conflict_count"] = 1
    with pytest.raises(supervisor.LiveSupervisorError) as conflict:
        supervisor._validate_full_collector_receipt(
            Receipt(), evidence, source_id="source", session_id="20260823T120000Z"
        )
    assert conflict.value.code == "COLLECTOR_NOT_ADMITTED"


def test_capture_caps_are_exactly_bound_to_eight_gib_and_preflight_allowance():
    signature = inspect.signature(collector.collect_transport_to_jsonl_handle)
    assert collector.R8_MAX_CAPTURE_BYTES == 8 * 1024**3
    assert signature.parameters["max_output_bytes"].default == 8 * 1024**3
    assert supervisor._CAPTURE_HARD_MAX_BYTES == collector.R8_MAX_CAPTURE_BYTES
    assert live_preflight.SUPERVISOR_PREFLIGHT_MAX_BYTES == 256 * 1024**2


def test_live_analysis_authority_v2_is_exact_and_hash_closed():
    capture = supervisor.CaptureHandleIdentity(
        filename="live-20260823T120000Z.jsonl",
        byte_size=123,
        sha256="2" * 64,
        volume_serial_number=1234,
        file_id="01234567:89abcdef",
    )
    admission = supervisor.InstallAdmission(
        install_manifest_sha256="3" * 64,
        project_wheel_sha256="4" * 64,
        runtime_manifest_sha256="5" * 64,
        runtime_manifest_self_sha256="6" * 64,
        runtime_tree_sha256="7" * 64,
        security_tree_sha256="8" * 64,
        security_tree_object_count=2440,
        code_root=str(supervisor.FIXED_VERSION_ROOT),
    )
    token = supervisor.RuntimeTokenAdmission(
        current_user_sid=supervisor.FIXED_RUNTIME_USER_SID,
        token_is_elevated=False,
        token_elevation_type="LIMITED",
        administrators_sid_enabled=False,
        integrity_level_rid=8192,
        least_privilege="PASS",
    )
    authority = supervisor.build_live_analysis_authority(
        run_id="20260823T120000Z",
        capture=capture,
        simulator_identity={
            "process_id": 123,
            "start_time_utc_ticks": 456,
            "windows_session_id": 2,
        },
        install_admission=admission,
        runtime_token_admission=token,
        preflight_receipt_sha256="9" * 64,
        preflight_production_semantic_digest="a" * 64,
    )
    for invalid_session_id in (0, -1, True):
        with pytest.raises(supervisor.LiveSupervisorError) as invalid_session:
            supervisor.build_live_analysis_authority(
                run_id="20260823T120000Z",
                capture=capture,
                simulator_identity={
                    "process_id": 123,
                    "start_time_utc_ticks": 456,
                    "windows_session_id": invalid_session_id,
                },
                install_admission=admission,
                runtime_token_admission=token,
                preflight_receipt_sha256="9" * 64,
                preflight_production_semantic_digest="a" * 64,
            )
        assert invalid_session.value.code == "SCHEMA_INVALID"
    assert set(authority) == supervisor._LIVE_ANALYSIS_AUTHORITY_KEYS
    declared = authority.pop("authority_sha256")
    assert declared == hashlib.sha256(supervisor._canonical_json(authority)).hexdigest()
    assert authority["contract_version"] == (
        "single-process-live-analysis-authority-v2"
    )
    assert authority["capture_file"] == "live-20260823T120000Z.jsonl"
    assert authority["capture_snapshot_method"] == (
        "CALLER_OWNED_SINGLE_PROCESS_FILE_HANDLE_V1"
    )
    assert authority["windows_session_id"] == 2
    assert authority["attestation_status"] == (
        "SELF_CONSISTENT_NOT_AUTHENTICATED"
    )
    assert authority["dev_smoke_profile_byte_size"] == 662
    assert authority["dev_smoke_profile_sha256"] == (
        "7706d831001dfdd1256cbf4101caecbd9e2675028c80e0a0dd69e05ad8423a25"
    )
    assert authority["runtime_token_admission"] == {
        "administrators_sid_enabled": False,
        "contract_version": "windows-runtime-token-admission-v1",
        "current_user_sid": supervisor.FIXED_RUNTIME_USER_SID,
        "integrity_level_rid": 8192,
        "least_privilege": "PASS",
        "token_elevation_type": "LIMITED",
        "token_is_elevated": False,
    }


def test_live_analysis_authority_rejects_noncanonical_capture_name():
    capture = supervisor.CaptureHandleIdentity(
        filename="other.jsonl",
        byte_size=1,
        sha256="2" * 64,
        volume_serial_number=1,
        file_id="00000000:00000001",
    )
    admission = supervisor.InstallAdmission(
        install_manifest_sha256="3" * 64,
        project_wheel_sha256="4" * 64,
        runtime_manifest_sha256="5" * 64,
        runtime_manifest_self_sha256="6" * 64,
        runtime_tree_sha256="7" * 64,
        security_tree_sha256="8" * 64,
        security_tree_object_count=1,
        code_root=str(supervisor.FIXED_VERSION_ROOT),
    )
    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor.build_live_analysis_authority(
            run_id="20260823T120000Z",
            capture=capture,
            simulator_identity={
                "process_id": 1,
                "start_time_utc_ticks": 2,
                "windows_session_id": 1,
            },
            install_admission=admission,
            runtime_token_admission=supervisor.RuntimeTokenAdmission(
                current_user_sid=supervisor.FIXED_RUNTIME_USER_SID,
                token_is_elevated=False,
                token_elevation_type="LIMITED",
                administrators_sid_enabled=False,
                integrity_level_rid=8192,
                least_privilege="PASS",
            ),
            preflight_receipt_sha256="9" * 64,
            preflight_production_semantic_digest="a" * 64,
        )
    assert failure.value.code == "AUTHORITY_INVALID"


def test_public_supervisor_has_no_transport_or_dependency_injection_and_is_fail_closed():
    parameters = set(inspect.signature(supervisor.run_live_supervisor).parameters)
    assert parameters == set()
    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor.run_live_supervisor()
    assert failure.value.code in {
        "RUNTIME_ISOLATION_REQUIRED",
        "WINDOWS_REQUIRED",
    }


def _fake_admitted_runtime() -> supervisor.AdmittedProtectedRuntime:
    return supervisor.AdmittedProtectedRuntime(
        admission=supervisor.InstallAdmission(
            install_manifest_sha256="1" * 64,
            project_wheel_sha256="2" * 64,
            runtime_manifest_sha256="3" * 64,
            runtime_manifest_self_sha256="4" * 64,
            runtime_tree_sha256="5" * 64,
            security_tree_sha256="6" * 64,
            security_tree_object_count=1,
            code_root=str(supervisor.FIXED_VERSION_ROOT),
        ),
        install_receipt={},
        runtime_manifest={},
        token_admission=supervisor.RuntimeTokenAdmission(
            current_user_sid=supervisor.FIXED_RUNTIME_USER_SID,
            token_is_elevated=False,
            token_elevation_type="LIMITED",
            administrators_sid_enabled=False,
            integrity_level_rid=8192,
            least_privilege="PASS",
        ),
        _stack=ExitStack(),
    )


def _local_state_directory_chain(tmp_path: Path) -> tuple[Path, ...]:
    user = tmp_path / "racer"
    app_data = user / "AppData"
    local = app_data / "Local"
    local.mkdir(parents=True)
    return (
        user,
        app_data,
        local,
        local / "AEIS",
        local / "AEIS" / "state",
        local / "AEIS" / "state" / "r8",
    )


def test_runtime_token_creates_plain_fixed_state_chain_idempotently(tmp_path: Path):
    chain = _local_state_directory_chain(tmp_path)
    assert supervisor._ensure_plain_directory_chain(chain, create_from=3) == chain[-1]
    assert supervisor._ensure_plain_directory_chain(chain, create_from=3) == chain[-1]
    assert all(path.is_dir() and not path.is_symlink() for path in chain)


def test_runtime_token_state_creation_rejects_preexisting_reparse(tmp_path: Path):
    chain = _local_state_directory_chain(tmp_path)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    chain[3].symlink_to(attacker, target_is_directory=True)
    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor._ensure_plain_directory_chain(chain, create_from=3)
    assert failure.value.code == "DIRECTORY_ADMISSION_FAILED"


def test_runtime_token_state_creation_rejects_parent_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    chain = _local_state_directory_chain(tmp_path)
    displaced = tmp_path / "displaced-local"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    real_mkdir = supervisor.os.mkdir
    attacked = False

    def swapping_mkdir(path):
        nonlocal attacked
        if not attacked and Path(path) == chain[3]:
            attacked = True
            chain[2].rename(displaced)
            chain[2].symlink_to(attacker, target_is_directory=True)
        return real_mkdir(path)

    monkeypatch.setattr(supervisor.os, "mkdir", swapping_mkdir)
    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor._ensure_plain_directory_chain(chain, create_from=3)
    assert failure.value.code in {"DIRECTORY_ADMISSION_FAILED", "STATE_ROOT_CHANGED"}
    assert attacked is True


def test_state_creation_gate_rejects_non_least_privilege_token():
    blocked = supervisor.RuntimeTokenAdmission(
        current_user_sid=supervisor.FIXED_RUNTIME_USER_SID,
        token_is_elevated=True,
        token_elevation_type="NOT_LIMITED",
        administrators_sid_enabled=True,
        integrity_level_rid=0x3000,
        least_privilege="BLOCKED",
    )
    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor._assert_runtime_token_admission(blocked)
    assert failure.value.code == "TOKEN_ADMISSION_FAILED"


def test_no_sim_episode_commits_one_wait_terminal_and_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_ids = iter(("20260823T120000Z", "20260823T120001Z"))
    call_order: list[str] = []

    def admitted_runtime():
        call_order.append("TOKEN_AND_INSTALL_ADMISSION")
        return _fake_admitted_runtime()

    def ensure_state_root():
        assert call_order[-1] == "TOKEN_AND_INSTALL_ADMISSION"
        call_order.append("RUNTIME_TOKEN_STATE_CREATE")
        return tmp_path

    monkeypatch.setattr(supervisor, "admit_installed_runtime", admitted_runtime)
    monkeypatch.setattr(supervisor, "_ensure_state_root", ensure_state_root)
    monkeypatch.setattr(supervisor, "_new_unique_run_id", lambda _root: next(run_ids))
    monkeypatch.setattr(
        supervisor,
        "_storage_admission",
        lambda _root, *, include_preflight_allowance: {
            "capture_hard_max_bytes": supervisor._CAPTURE_HARD_MAX_BYTES,
            "free_bytes": 20 * 1024**3,
            "preflight_residue_allowance_bytes": (
                supervisor._PREFLIGHT_RESIDUE_ALLOWANCE_BYTES
                if include_preflight_allowance
                else 0
            ),
            "required_free_bytes": 1,
            "reserve_bytes": supervisor._STATE_FREE_SPACE_RESERVE_BYTES,
            "status": "PASS",
        },
    )

    def no_simulator():
        call_order.append("SIMULATOR_DISCOVERY")
        raise supervisor.LiveSupervisorError(
            "SIMULATOR_UNAVAILABLE", "simulator is not running"
        )

    monkeypatch.setattr(supervisor, "_open_single_simulator_process", no_simulator)
    first = supervisor._run_live_supervisor_episode()
    second = supervisor._run_live_supervisor_episode()
    assert first["status"] == second["status"] == "WAIT"
    assert call_order == [
        "TOKEN_AND_INSTALL_ADMISSION",
        "RUNTIME_TOKEN_STATE_CREATE",
        "SIMULATOR_DISCOVERY",
    ] * 2
    assert first["simulator_identity"] is None
    assert first["wait_reasons"] == ["WAIT_SIMULATOR"]
    for run_id in ("20260823T120000Z", "20260823T120001Z"):
        started_path = tmp_path / f"started-{run_id}.json"
        assert started_path.exists()
        started = json.loads(started_path.read_bytes())
        assert started["runtime_token_admission"]["least_privilege"] == "PASS"
        assert started["runtime_token_admission"]["token_is_elevated"] is False
        assert (tmp_path / f"wait-{run_id}.json").exists()
        assert not (tmp_path / f"live-{run_id}.jsonl").exists()
        assert not (tmp_path / f"ready-{run_id}.json").exists()
        assert not (tmp_path / f"failed-{run_id}.json").exists()


def test_run_id_collision_advances_without_reusing_an_episode(tmp_path: Path, monkeypatch):
    class FrozenDateTime:
        @classmethod
        def now(cls, _timezone):
            return datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(supervisor, "datetime", FrozenDateTime)
    (tmp_path / "started-20260823T120000Z.json").write_bytes(b"occupied")
    assert supervisor._new_unique_run_id(tmp_path) == "20260823T120001Z"


def test_nonrecoverable_sim_discovery_commits_failed_not_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(supervisor, "admit_installed_runtime", _fake_admitted_runtime)
    monkeypatch.setattr(supervisor, "_ensure_state_root", lambda: tmp_path)
    monkeypatch.setattr(
        supervisor, "_new_unique_run_id", lambda _root: "20260823T120002Z"
    )
    monkeypatch.setattr(
        supervisor,
        "_storage_admission",
        lambda *_args, **_kwargs: {"status": "PASS"},
    )

    def ambiguous_simulator():
        raise supervisor.LiveSupervisorError(
            "SIMULATOR_PROCESS_SET_INVALID", "multiple simulators"
        )

    monkeypatch.setattr(
        supervisor, "_open_single_simulator_process", ambiguous_simulator
    )
    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor._run_live_supervisor_episode()
    assert failure.value.code == "SIMULATOR_PROCESS_SET_INVALID"
    run_id = "20260823T120002Z"
    assert (tmp_path / f"started-{run_id}.json").exists()
    assert (tmp_path / f"failed-{run_id}.json").exists()
    assert not (tmp_path / f"wait-{run_id}.json").exists()
    assert not (tmp_path / f"ready-{run_id}.json").exists()


def test_storage_wait_precedes_sim_and_never_creates_canonical_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_id = "20260823T120003Z"
    monkeypatch.setattr(supervisor, "admit_installed_runtime", _fake_admitted_runtime)
    monkeypatch.setattr(supervisor, "_ensure_state_root", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_new_unique_run_id", lambda _root: run_id)
    monkeypatch.setattr(
        supervisor,
        "_storage_admission",
        lambda *_args, **_kwargs: {
            "capture_hard_max_bytes": supervisor._CAPTURE_HARD_MAX_BYTES,
            "free_bytes": 1,
            "preflight_residue_allowance_bytes": (
                supervisor._PREFLIGHT_RESIDUE_ALLOWANCE_BYTES
            ),
            "required_free_bytes": 2,
            "reserve_bytes": supervisor._STATE_FREE_SPACE_RESERVE_BYTES,
            "status": "WAIT_STORAGE",
        },
    )
    monkeypatch.setattr(
        supervisor,
        "_open_single_simulator_process",
        lambda: (_ for _ in ()).throw(AssertionError("sim probe must not run")),
    )
    result = supervisor._run_live_supervisor_episode()
    assert result["status"] == "WAIT"
    assert result["wait_reasons"] == ["WAIT_STORAGE"]
    assert (tmp_path / f"started-{run_id}.json").exists()
    assert (tmp_path / f"wait-{run_id}.json").exists()
    assert not list(tmp_path.glob(f"preflight-{run_id}*"))
    assert not (tmp_path / f"live-{run_id}.jsonl").exists()
    assert not (tmp_path / f"ready-{run_id}.json").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "runtime\\python.exe",
        "runtime/CON/file",
        "runtime/trailing./file",
        "runtime/evil:stream",
    ],
)
def test_runtime_relative_paths_are_windows_safe(path: str):
    with pytest.raises(supervisor.LiveSupervisorError) as failure:
        supervisor._safe_relative_path(path, "attack")
    assert failure.value.code == "PATH_INVALID"


def test_old_powershell_paths_are_explicit_nonproduction_references():
    root = Path(__file__).resolve().parents[1]
    names = (
        "run_aeis_live_preflight_v4.ps1",
        "start_aeis_r8_live_capture.ps1",
        "watch_aeis_r8_live_capture.ps1",
    )
    if not all((root / "scripts" / "windows" / name).is_file() for name in names):
        pytest.skip("PRIVATE_DEPLOYMENT: legacy host-bound PowerShell is not published")
    for name in names:
        source = (root / "scripts" / "windows" / name).read_text(encoding="utf-8")
        guard = source.index("$script:AeisExecutionDisposition")
        stop = source.index(
            "throw 'NON_PRODUCTION_REFERENCE_ONLY: use the protected R8 Python supervisor.'"
        )
        first_function = source.index("function ")
        assert guard < stop < first_function
        assert source.count("NON_PRODUCTION_REFERENCE_ONLY") >= 2
