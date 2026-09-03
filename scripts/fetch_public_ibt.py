"""Fetch and verify the pinned public Audi/Spa IBT without redistributing it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_ASSET_ID = "public-audi-r8-evo2-spa"
DOWNLOAD_CONTRACT_VERSION = "public-ibt-fetch-v1"
_ALLOWED_DOWNLOAD_HOST = "media.githubusercontent.com"


class PublicIbtError(RuntimeError):
    """Raised when the pinned public-data contract cannot be satisfied."""


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise PublicIbtError(f"asset must be a regular file: {path}")
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _validate_data_directory(project_root: Path, target: Path) -> None:
    data_directory = project_root / "data"
    raw_directory = data_directory / "raw"
    if data_directory.is_symlink() or not data_directory.is_dir():
        raise PublicIbtError(f"data directory must be a real directory: {data_directory}")
    if target.parent != raw_directory:
        raise PublicIbtError("asset target is outside the direct data/raw directory")
    if raw_directory.is_symlink():
        raise PublicIbtError(
            f"asset directory must not be a symbolic link: {raw_directory}"
        )
    if raw_directory.exists() and not raw_directory.is_dir():
        raise PublicIbtError(f"asset directory must be a real directory: {raw_directory}")


def _asset(project_root: Path, asset_id: str) -> dict[str, object]:
    manifest_path = project_root / "data" / "public_sources.json"
    if manifest_path.is_symlink():
        raise PublicIbtError(f"public-data manifest must not be a symbolic link: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicIbtError(f"cannot read public-data manifest: {manifest_path}") from exc
    assets = manifest.get("assets") if isinstance(manifest, dict) else None
    if not isinstance(assets, list):
        raise PublicIbtError("public-data manifest has no asset list")
    matches = [
        item
        for item in assets
        if isinstance(item, dict) and item.get("asset_id") == asset_id
    ]
    if len(matches) != 1:
        raise PublicIbtError(f"expected exactly one manifest asset named {asset_id!r}")
    asset = matches[0]
    required = ("byte_size", "local_path", "sha256", "upstream")
    if any(key not in asset for key in required):
        raise PublicIbtError("public-data asset is incomplete")
    return asset


def _validated_contract(
    project_root: Path, asset: Mapping[str, object]
) -> tuple[Path, str, int, str, str]:
    raw_path = asset["local_path"]
    expected_sha256 = asset["sha256"]
    expected_size = asset["byte_size"]
    upstream = asset["upstream"]
    asset_id = asset.get("asset_id")
    if type(asset_id) is not str or not asset_id:
        raise PublicIbtError("asset_id is invalid")
    if type(raw_path) is not str:
        raise PublicIbtError("local_path is invalid")
    relative = Path(raw_path)
    if (
        relative.is_absolute()
        or relative.parent != Path("data/raw")
        or relative.suffix.casefold() != ".ibt"
    ):
        raise PublicIbtError("local_path must name one direct data/raw .ibt file")
    if (
        type(expected_sha256) is not str
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise PublicIbtError("asset sha256 is invalid")
    if type(expected_size) is not int or expected_size <= 0:
        raise PublicIbtError("asset byte_size is invalid")
    if not isinstance(upstream, Mapping):
        raise PublicIbtError("asset upstream record is invalid")
    download_url = upstream.get("download_url")
    if type(download_url) is not str:
        raise PublicIbtError("asset download_url is invalid")
    parsed = urlparse(download_url)
    if parsed.scheme != "https" or parsed.hostname != _ALLOWED_DOWNLOAD_HOST:
        raise PublicIbtError("asset download_url is outside the pinned HTTPS host")
    return (
        project_root / relative,
        expected_sha256,
        expected_size,
        download_url,
        asset_id,
    )


def _receipt(
    *, target: Path, sha256: str, byte_size: int, asset_id: str, status: str
) -> dict[str, object]:
    return {
        "asset_id": asset_id,
        "byte_size": byte_size,
        "contract_version": DOWNLOAD_CONTRACT_VERSION,
        "local_path": str(target),
        "privacy_notice": "RAW_IBT_MAY_CONTAIN_DRIVER_INFO_DO_NOT_REDISTRIBUTE",
        "sha256": sha256,
        "status": status,
    }


def _prepare_windows_path_for_unlink(path: Path) -> None:
    """Clear the Windows read-only bit before removing one owned hard-link name."""

    if os.name == "nt" and path.exists():
        os.chmod(path, stat.S_IWRITE)


def fetch_public_ibt(
    *,
    project_root: Path,
    asset_id: str = DEFAULT_ASSET_ID,
    verify_only: bool = False,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, object]:
    """Verify an existing asset or exclusively publish one verified download."""

    root = project_root.resolve(strict=True)
    asset = _asset(root, asset_id)
    target, expected_sha256, expected_size, download_url, normalized_id = (
        _validated_contract(root, asset)
    )
    _validate_data_directory(root, target)
    if target.is_symlink():
        raise PublicIbtError(f"refusing symbolic-link asset target: {target}")
    if target.exists():
        actual_sha256, actual_size = _sha256_file(target)
        if (actual_sha256, actual_size) != (expected_sha256, expected_size):
            raise PublicIbtError("existing asset does not match the pinned size and SHA-256")
        return _receipt(
            target=target,
            sha256=actual_sha256,
            byte_size=actual_size,
            asset_id=normalized_id,
            status="VERIFIED_EXISTING",
        )
    if verify_only:
        raise PublicIbtError(f"WAIT_DATA: pinned asset is absent: {target}")

    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    _validate_data_directory(root, target)
    temporary_path: Path | None = None
    published_identity: tuple[int, int] | None = None
    try:
        request = Request(
            download_url,
            headers={"User-Agent": "iracing-ai-engineer-public-data-fetch-v1"},
        )
        with opener(request, timeout=60) as response, tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=".public-ibt-",
            suffix=".part",
            dir=target.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            final_url = urlparse(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != _ALLOWED_DOWNLOAD_HOST:
                raise PublicIbtError("download redirected outside the pinned HTTPS host")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) != expected_size:
                raise PublicIbtError("download Content-Length does not match the manifest")
            digest = hashlib.sha256()
            byte_size = 0
            while chunk := response.read(1024 * 1024):
                byte_size += len(chunk)
                if byte_size > expected_size:
                    raise PublicIbtError("download exceeds the manifest byte_size")
                digest.update(chunk)
                temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
            actual_sha256 = digest.hexdigest()
            if (actual_sha256, byte_size) != (expected_sha256, expected_size):
                raise PublicIbtError(
                    "download does not match the pinned size and SHA-256"
                )
            opened = os.fstat(temporary.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise PublicIbtError("download temporary file identity is invalid")
            published_identity = (opened.st_dev, opened.st_ino)
            try:
                os.link(temporary_path, target, follow_symlinks=False)
            except FileExistsError as exc:
                published_identity = None
                raise PublicIbtError(
                    f"asset target appeared during download: {target}"
                ) from exc
            linked = os.stat(target, follow_symlinks=False)
            current = os.fstat(temporary.fileno())
            if (
                not stat.S_ISREG(linked.st_mode)
                or (linked.st_dev, linked.st_ino) != published_identity
                or (current.st_dev, current.st_ino) != published_identity
            ):
                raise PublicIbtError("published asset identity does not match the download")
            read_only_mode = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
            if hasattr(os, "fchmod"):
                os.fchmod(temporary.fileno(), read_only_mode)
            else:  # pragma: no cover - Windows has no descriptor chmod
                os.chmod(target, read_only_mode)
                linked = os.stat(target, follow_symlinks=False)
                if (linked.st_dev, linked.st_ino) != published_identity:
                    raise PublicIbtError(
                        "published asset identity changed while marking it read-only"
                    )
            os.fsync(temporary.fileno())
            temporary.seek(0)
            published_digest = hashlib.sha256()
            published_size = 0
            while chunk := temporary.read(1024 * 1024):
                published_digest.update(chunk)
                published_size += len(chunk)
            linked = os.stat(target, follow_symlinks=False)
            if (
                (published_digest.hexdigest(), published_size)
                != (expected_sha256, expected_size)
                or (linked.st_dev, linked.st_ino) != published_identity
            ):
                raise PublicIbtError(
                    "published asset does not match the pinned size and SHA-256"
                )
            return _receipt(
                target=target,
                sha256=actual_sha256,
                byte_size=byte_size,
                asset_id=normalized_id,
                status="DOWNLOADED_AND_VERIFIED",
            )
    except Exception:
        if published_identity is not None:
            try:
                linked = os.stat(target, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (linked.st_dev, linked.st_ino) == published_identity:
                    _prepare_windows_path_for_unlink(target)
                    target.unlink()
        raise
    finally:
        if temporary_path is not None:
            _prepare_windows_path_for_unlink(temporary_path)
            temporary_path.unlink(missing_ok=True)
            # On Windows the read-only attribute belongs to the file, not to
            # one hard-link name.  Clearing it to delete the temporary alias
            # also clears it on the surviving target, so restore it there.
            if os.name == "nt" and target.exists():
                try:
                    linked = os.stat(target, follow_symlinks=False)
                except OSError:
                    pass
                else:
                    if (
                        published_identity is not None
                        and (linked.st_dev, linked.st_ino) == published_identity
                    ):
                        os.chmod(target, stat.S_IREAD)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-id", default=DEFAULT_ASSET_ID)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        receipt = fetch_public_ibt(
            project_root=project_root,
            asset_id=args.asset_id,
            verify_only=args.verify_only,
        )
    except (OSError, PublicIbtError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "contract_version": DOWNLOAD_CONTRACT_VERSION,
                    "error": "WAIT_DATA",
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 4
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
