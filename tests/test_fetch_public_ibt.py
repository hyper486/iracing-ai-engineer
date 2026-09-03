from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fetch_public_ibt",
    Path("scripts/fetch_public_ibt.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
PublicIbtError = _MODULE.PublicIbtError
fetch_public_ibt = _MODULE.fetch_public_ibt


def _manifest(project_root: Path, payload: bytes) -> Path:
    data = project_root / "data"
    data.mkdir()
    target = data / "raw" / "fixture.ibt"
    manifest = {
        "assets": [
            {
                "asset_id": "public-audi-r8-evo2-spa",
                "byte_size": len(payload),
                "local_path": "data/raw/fixture.ibt",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "upstream": {
                    "download_url": "https://media.githubusercontent.com/media/example/pinned/fixture.ibt"
                },
            }
        ]
    }
    (data / "public_sources.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return target


class _Response(io.BytesIO):
    def __init__(self, payload: bytes):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def geturl(self) -> str:
        return "https://media.githubusercontent.com/media/example/pinned/fixture.ibt"


def test_fetch_downloads_to_an_exclusive_verified_read_only_target(tmp_path: Path):
    payload = b"pinned telemetry fixture"
    target = _manifest(tmp_path, payload)

    receipt = fetch_public_ibt(
        project_root=tmp_path,
        opener=lambda request, timeout: _Response(payload),
    )

    assert target.read_bytes() == payload
    assert receipt["status"] == "DOWNLOADED_AND_VERIFIED"
    assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
    assert target.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    assert not list(target.parent.glob(".public-ibt-*.part"))


def test_fetch_verifies_existing_asset_without_opening_network(tmp_path: Path):
    payload = b"already present"
    target = _manifest(tmp_path, payload)
    target.parent.mkdir()
    target.write_bytes(payload)

    receipt = fetch_public_ibt(
        project_root=tmp_path,
        opener=lambda request, timeout: pytest.fail("network should not be opened"),
    )

    assert receipt["status"] == "VERIFIED_EXISTING"


def test_fetch_rejects_bad_download_and_leaves_no_target(tmp_path: Path):
    target = _manifest(tmp_path, b"expected")

    with pytest.raises(PublicIbtError, match="Content-Length|pinned"):
        fetch_public_ibt(
            project_root=tmp_path,
            opener=lambda request, timeout: _Response(b"wrong"),
        )

    assert not target.exists()
    assert not list(target.parent.glob(".public-ibt-*.part"))


def test_fetch_rejects_raw_directory_symbolic_link(tmp_path: Path):
    payload = b"outside telemetry"
    target = _manifest(tmp_path, payload)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / target.name).write_bytes(payload)
    target.parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PublicIbtError, match="must not be a symbolic link"):
        fetch_public_ibt(
            project_root=tmp_path,
            verify_only=True,
            opener=lambda request, timeout: pytest.fail("network should not be opened"),
        )


def test_fetch_rehashes_published_inode_and_rolls_back_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payload = b"expected telemetry"
    target = _manifest(tmp_path, payload)
    real_link = _MODULE.os.link

    def tampering_link(source, destination, **kwargs):
        with Path(source).open("r+b") as handle:
            handle.write(b"X")
            handle.flush()
            _MODULE.os.fsync(handle.fileno())
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(_MODULE.os, "link", tampering_link)

    with pytest.raises(PublicIbtError, match="published asset does not match"):
        fetch_public_ibt(
            project_root=tmp_path,
            opener=lambda request, timeout: _Response(payload),
        )

    assert not target.exists()
    assert not list(target.parent.glob(".public-ibt-*.part"))


@pytest.mark.skipif(not hasattr(_MODULE.os, "fchmod"), reason="requires descriptor chmod")
def test_fetch_final_rehash_detects_descriptor_chmod_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payload = b"expected telemetry"
    target = _manifest(tmp_path, payload)
    real_fchmod = _MODULE.os.fchmod

    def tampering_fchmod(descriptor, mode):
        with _MODULE.os.fdopen(_MODULE.os.dup(descriptor), "r+b") as handle:
            handle.write(b"X")
            handle.flush()
            _MODULE.os.fsync(handle.fileno())
        return real_fchmod(descriptor, mode)

    monkeypatch.setattr(_MODULE.os, "fchmod", tampering_fchmod)

    with pytest.raises(PublicIbtError, match="published asset does not match"):
        fetch_public_ibt(
            project_root=tmp_path,
            opener=lambda request, timeout: _Response(payload),
        )

    assert not target.exists()
    assert not list(target.parent.glob(".public-ibt-*.part"))
