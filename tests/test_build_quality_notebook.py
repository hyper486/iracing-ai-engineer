from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "build_quality_notebook",
    Path("scripts/build_quality_notebook.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
LEGACY_QUALITY_INPUT_COUNT = _MODULE.LEGACY_QUALITY_INPUT_COUNT
QualityNotebookManifestError = _MODULE.QualityNotebookManifestError
load_quality_notebook_inputs = _MODULE.load_quality_notebook_inputs


def _fixture_tree(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "workspace"
    raw_directory = root / "data" / "raw"
    raw_directory.mkdir(parents=True)
    source_manifest = json.loads(
        Path("data/manifest.json").read_text(encoding="utf-8")
    )
    manifest = copy.deepcopy(source_manifest)
    assert len(manifest["files"]) == LEGACY_QUALITY_INPUT_COUNT
    for index, entry in enumerate(manifest["files"], start=1):
        payload = f"legacy-fixture-{index}".encode()
        (raw_directory / entry["file_name"]).write_bytes(payload)
        entry["byte_size"] = len(payload)
        entry["record_count"] = index
        entry["sha256"] = hashlib.sha256(payload).hexdigest()
    (root / "data" / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root, manifest


def test_manifest_is_the_only_notebook_input_authority(tmp_path: Path):
    root, manifest = _fixture_tree(tmp_path)
    extra = root / "data" / "raw" / "public-audi-spa.ibt"
    extra.write_bytes(b"unrelated public fixture")

    loaded, paths = load_quality_notebook_inputs(root)

    assert loaded == manifest
    assert [path.name for path in paths] == [
        entry["file_name"] for entry in manifest["files"]
    ]
    assert extra not in paths


def test_notebook_setup_uses_only_the_validated_manifest_paths():
    source = Path("scripts/build_quality_notebook.py").read_text(encoding="utf-8")

    assert 'RAW_DIR.glob("*.ibt")' not in source
    assert "manifest, paths = load_quality_notebook_inputs(ROOT)" in source


def test_manifest_declared_missing_file_is_never_silently_skipped(tmp_path: Path):
    root, manifest = _fixture_tree(tmp_path)
    missing_name = manifest["files"][1]["file_name"]
    (root / "data" / "raw" / missing_name).unlink()

    with pytest.raises(FileNotFoundError, match="manifest-declared IBT file is missing"):
        load_quality_notebook_inputs(root)


def test_manifest_must_remain_the_exact_four_file_legacy_set(tmp_path: Path):
    root, manifest = _fixture_tree(tmp_path)
    manifest["files"].append(copy.deepcopy(manifest["files"][0]))
    (root / "data" / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(QualityNotebookManifestError, match="exactly four legacy"):
        load_quality_notebook_inputs(root)


@pytest.mark.parametrize("field", ["byte_size", "sha256"])
def test_manifest_bytes_are_verified_before_notebook_analysis(
    tmp_path: Path, field: str
):
    root, manifest = _fixture_tree(tmp_path)
    manifest["files"][0][field] = 1 if field == "byte_size" else "0" * 64
    (root / "data" / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(QualityNotebookManifestError, match=f"manifest {field} mismatch"):
        load_quality_notebook_inputs(root)
