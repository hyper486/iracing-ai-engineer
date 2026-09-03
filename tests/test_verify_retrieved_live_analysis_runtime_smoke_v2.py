from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import stat
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROOF_ROOT = (
    PROJECT_ROOT.parent
    / "deliverable"
    / "retrieved-live-analysis-runtime-smoke-20260902-v2"
)
VERIFIER_PATH = (
    PROJECT_ROOT
    / "scripts"
    / "verify_retrieved_live_analysis_runtime_smoke_v2.py"
)
VERIFIER_SHA256 = (
    "8fc32784073503ab2af684ebd77cf712d26053856198a8ac5fa8db8c12f764e4"
)
IDENTITY_SHA256 = (
    "a60c00570239a0ee0026ab8a964f6c128aea6b7b260411c5f74586eb867eb923"
)


def _load(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _make_writable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IWRITE)


@pytest.fixture(scope="module")
def verifier() -> types.ModuleType:
    if not VERIFIER_PATH.is_file():
        pytest.skip("PRIVATE_DEPLOYMENT: frozen Aeis runtime verifier is not published")
    return _load(VERIFIER_PATH, "_test_runtime_smoke_proof_v2_verifier")


def _require_proof() -> None:
    if not PROOF_ROOT.is_dir():
        pytest.skip("REQUIRES_FROZEN_RUNTIME_SMOKE_PROOF_V2")


def test_exact_frozen_proof_passes_object_exact_verification(
    verifier: types.ModuleType,
) -> None:
    _require_proof()
    frozen_verifier = PROOF_ROOT / verifier.VERIFIER_NAME
    identity = PROOF_ROOT / verifier.IDENTITY_NAME
    assert _sha(VERIFIER_PATH.read_bytes()) == VERIFIER_SHA256
    assert frozen_verifier.read_bytes() == VERIFIER_PATH.read_bytes()
    assert _sha(identity.read_bytes()) == IDENTITY_SHA256

    result = verifier.verify_runtime_proof(
        PROOF_ROOT,
        expected_self_sha256=VERIFIER_SHA256,
        expected_identity_file_sha256=IDENTITY_SHA256,
    )
    assert result == {
        "contract_version": verifier.PROOF_CONTRACT,
        "identity_file_sha256": IDENTITY_SHA256,
        "receipt_sha256": verifier.EXPECTED_RECEIPT_SHA256,
        "runtime_smoke_sha256": verifier.EXPECTED_RUNTIME_SMOKE_SHA256,
        "status": "PASS_RUNTIME_PROOF_VERIFIED_WAIT_TARGET_AND_LIVE",
        "vehicle_control_enabled": False,
        "verification": "PASS_OBJECT_EXACT_RUNTIME_PROOF_CLOSURE",
    }


def test_wrong_self_pin_fails_before_proof_read(
    tmp_path: Path,
    verifier: types.ModuleType,
) -> None:
    missing = tmp_path / "missing-proof"
    with pytest.raises(verifier.RuntimeProofVerificationError) as raised:
        verifier.verify_runtime_proof(
            missing,
            expected_self_sha256="0" * 64,
            expected_identity_file_sha256=IDENTITY_SHA256,
        )
    assert raised.value.code == "VERIFIER_INVALID"


def test_wrong_external_identity_pin_fails_closed(
    verifier: types.ModuleType,
) -> None:
    _require_proof()
    with pytest.raises(verifier.RuntimeProofVerificationError) as raised:
        verifier.verify_runtime_proof(
            PROOF_ROOT,
            expected_self_sha256=VERIFIER_SHA256,
            expected_identity_file_sha256="0" * 64,
        )
    assert raised.value.code == "IDENTITY_INVALID"


def test_payload_tamper_fails_even_when_file_set_is_unchanged(
    tmp_path: Path,
    verifier: types.ModuleType,
) -> None:
    _require_proof()
    changed = tmp_path / "changed-proof"
    shutil.copytree(PROOF_ROOT, changed)
    receipt_path = changed / verifier.RECEIPT_NAME
    _make_writable(receipt_path)
    receipt_path.write_bytes(receipt_path.read_bytes() + b"\n")

    with pytest.raises(verifier.RuntimeProofVerificationError) as raised:
        verifier.verify_runtime_proof(
            changed,
            expected_self_sha256=VERIFIER_SHA256,
            expected_identity_file_sha256=IDENTITY_SHA256,
        )
    assert raised.value.code == "IDENTITY_INVALID"


def test_fully_rehashed_unsafe_semantics_still_fail_closed(
    tmp_path: Path,
    verifier: types.ModuleType,
) -> None:
    _require_proof()
    changed = tmp_path / "rehashed-unsafe-proof"
    shutil.copytree(PROOF_ROOT, changed)

    receipt_path = changed / verifier.RECEIPT_NAME
    _make_writable(receipt_path)
    receipt = json.loads(receipt_path.read_bytes())
    receipt["safety"]["vehicle_control_enabled"] = True
    semantic = dict(receipt)
    semantic.pop("runtime_smoke_sha256")
    changed_runtime_sha = _sha(verifier._canonical_json(semantic))
    receipt["runtime_smoke_sha256"] = changed_runtime_sha
    receipt_payload = verifier._canonical_json(receipt)
    receipt_path.write_bytes(receipt_payload)

    identity_path = changed / verifier.IDENTITY_NAME
    _make_writable(identity_path)
    identity = json.loads(identity_path.read_bytes())
    identity["result"]["runtime_smoke_sha256"] = changed_runtime_sha
    identity["safety"]["vehicle_control_enabled"] = True
    for item in identity["payload_files"]:
        if item["name"] == verifier.RECEIPT_NAME:
            item["byte_size"] = len(receipt_payload)
            item["sha256"] = _sha(receipt_payload)
            break
    identity_payload = verifier._canonical_json(identity)
    identity_path.write_bytes(identity_payload)

    with pytest.raises(verifier.RuntimeProofVerificationError) as raised:
        verifier.verify_runtime_proof(
            changed,
            expected_self_sha256=VERIFIER_SHA256,
            expected_identity_file_sha256=_sha(identity_payload),
        )
    assert raised.value.code == "IDENTITY_INVALID"


def test_unexpected_proof_member_fails_closed(
    tmp_path: Path,
    verifier: types.ModuleType,
) -> None:
    _require_proof()
    changed = tmp_path / "extra-member-proof"
    shutil.copytree(PROOF_ROOT, changed)
    (changed / "unreviewed.txt").write_text("unreviewed\n", encoding="utf-8")

    with pytest.raises(verifier.RuntimeProofVerificationError) as raised:
        verifier.verify_runtime_proof(
            changed,
            expected_self_sha256=VERIFIER_SHA256,
            expected_identity_file_sha256=IDENTITY_SHA256,
        )
    assert raised.value.code == "PROOF_INVALID"


def test_verifier_cli_emits_only_verified_wait_state(
    verifier: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_proof()
    assert (
        verifier.main(
            [
                str(PROOF_ROOT),
                "--expected-self-sha256",
                VERIFIER_SHA256,
                "--expected-identity-file-sha256",
                IDENTITY_SHA256,
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["status"] == "PASS_RUNTIME_PROOF_VERIFIED_WAIT_TARGET_AND_LIVE"
    assert result["verification"] == "PASS_OBJECT_EXACT_RUNTIME_PROOF_CLOSURE"
    assert result["vehicle_control_enabled"] is False
