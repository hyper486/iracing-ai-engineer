"""Explicit pinned public-model download; default mode only verifies local files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


def manifest() -> dict:
    value = json.loads((ROOT / "packaging" / "voice-model.json").read_text(encoding="utf-8"))
    if set(value["files"]) != set(FILES):
        raise ValueError("VOICE_MODEL_MANIFEST_INVALID")
    return value


def verify(directory: Path, hashes: dict[str, str]) -> None:
    for filename in FILES:
        path = directory / filename
        if not path.is_file() or path.is_symlink():
            raise ValueError("VOICE_MODEL_FILE_MISSING")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != hashes[filename]:
            raise ValueError("VOICE_MODEL_HASH_MISMATCH")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args(argv)
    model = manifest()
    destination = ROOT / "models" / "faster-whisper-medium"
    # Fixed destination; reject links, junctions and an escape from this checkout.
    for item in (ROOT / "models", destination):
        if item.is_symlink() or item.is_junction():
            raise ValueError("VOICE_MODEL_UNSAFE_DESTINATION")
    if not destination.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("VOICE_MODEL_UNSAFE_DESTINATION")
    if args.download:
        cli = shutil.which("hf")
        if cli is None:
            raise ValueError("VOICE_MODEL_CLI_MISSING")
        environ = dict(os.environ)
        for variable in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
            environ.pop(variable, None)
        environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        result = subprocess.run(
            [cli, "download", model["repository"], *FILES,
             "--revision", model["revision"], "--local-dir", str(destination), "--quiet"],
            env=environ, check=False, timeout=600,
        )
        if result.returncode:
            raise ValueError("VOICE_MODEL_DOWNLOAD_FAILED")
    verify(destination, model["files"])
    print("Pinned local speech model: four SHA-256 checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
