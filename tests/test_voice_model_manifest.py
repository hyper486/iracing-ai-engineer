"""Download and hash contracts are testable without network or public weights."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location("fetch_voice_model", ROOT / "scripts"
                                                / "fetch_voice_model.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_public_model_manifest_is_pinned_and_allowlisted():
    model = module().manifest()
    assert model["repository"] == "Systran/faster-whisper-medium"
    assert model["revision"] == "08e178d48790749d25932bbc082711ddcfdfbc4f"
    assert model["license"] == "MIT"
    assert all(len(value) == 64 for value in model["files"].values())


def test_verify_rejects_corrupt_and_missing_files(tmp_path):
    fetch = module()
    hashes = {name: hashlib.sha256(b"synthetic").hexdigest() for name in fetch.FILES}
    with pytest.raises(ValueError, match="MISSING"):
        fetch.verify(tmp_path, hashes)
    for name in fetch.FILES:
        (tmp_path / name).write_bytes(b"synthetic")
    fetch.verify(tmp_path, hashes)
    (tmp_path / "model.bin").write_bytes(b"changed")
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        fetch.verify(tmp_path, hashes)


@pytest.mark.parametrize("download", [False, True])
def test_network_is_explicit_and_anonymous(monkeypatch, tmp_path, download):
    fetch = module()
    real_manifest = fetch.manifest()
    calls = []
    monkeypatch.setattr(fetch, "ROOT", tmp_path)
    monkeypatch.setattr(fetch, "manifest", lambda: real_manifest)
    monkeypatch.setattr(fetch, "verify", lambda *args: calls.append("verify"))
    monkeypatch.setattr(fetch.shutil, "which", lambda _: "synthetic-hf")
    monkeypatch.setenv("HF_TOKEN", "SYNTHETIC_DO_NOT_FORWARD")

    def run(command, **kwargs):
        calls.append("download")
        assert command[1] == "download" and "--revision" in command
        assert kwargs["env"]["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"
        assert "HF_TOKEN" not in kwargs["env"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(fetch.subprocess, "run", run)
    assert fetch.main(["--download"] if download else []) == 0
    assert calls == (["download", "verify"] if download else ["verify"])
