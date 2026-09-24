"""Packaging contract checks without building an EXE or touching an SDK."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts" / "run_desktop.py"
SPEC = ROOT / "packaging" / "desktop.spec"
BUILD = ROOT / "scripts" / "build_desktop.ps1"


def _entry():
    spec = importlib.util.spec_from_file_location("_desktop_packaging_entry", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _desktop_stub(monkeypatch):
    calls = []
    module = ModuleType("iracing_ai_engineer.desktop_app")
    module.main = lambda argv: calls.append(argv) or 7
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return calls


def test_source_entry_bootstraps_only_desktop_main(monkeypatch, tmp_path):
    entry = _entry()
    source = tmp_path / "src" / "iracing_ai_engineer"
    source.mkdir(parents=True)
    (source / "desktop_app.py").touch()
    calls = _desktop_stub(monkeypatch)
    monkeypatch.setattr(entry, "__file__", str(tmp_path / "scripts" / "run_desktop.py"))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert entry.main(["--self-test"]) == 7
    assert calls == [["--self-test"]]
    assert sys.path[0] == str(tmp_path / "src")
    assert "iracing_ai_engineer.cli" not in ENTRY.read_text(encoding="utf-8")


def test_frozen_entry_does_not_require_or_search_a_checkout(monkeypatch):
    entry = _entry()
    calls = _desktop_stub(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    before = sys.path.copy()

    def no_filesystem(*args, **kwargs):
        raise AssertionError("FROZEN_ENTRY_MUST_NOT_SEARCH_SOURCE")

    monkeypatch.setattr(entry, "Path", no_filesystem)
    assert entry.main() == 7
    assert calls == [None] and sys.path == before


def test_missing_source_entry_error_is_path_free(monkeypatch, tmp_path):
    entry = _entry()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(entry, "__file__", str(tmp_path / "scripts" / "run_desktop.py"))
    with pytest.raises(RuntimeError, match="^DESKTOP_SOURCE_UNAVAILABLE$"):
        entry.main()


def test_spec_is_single_file_windowed_and_has_no_workspace_data_collection():
    calls = {}

    def analysis(scripts, **kwargs):
        calls["analysis"] = (scripts, kwargs)
        return SimpleNamespace(pure=["pure"], scripts=scripts, binaries=["dll"], datas=["tk"])

    def archive(pure):
        calls["archive"] = pure
        return "archive"

    def executable(*args, **kwargs):
        calls["executable"] = (args, kwargs)
        return None

    namespace = {
        "SPECPATH": str(SPEC.parent),
        "Analysis": analysis,
        "PYZ": archive,
        "EXE": executable,
    }
    exec(compile(SPEC.read_text(encoding="utf-8"), "desktop.spec", "exec"), namespace)
    scripts, options = calls["analysis"]
    assert scripts == [str(ENTRY)]
    assert options["pathex"] == [str(ROOT / "src")]
    model_files = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
    assert options["datas"] == [
        (str(ROOT / "models" / "faster-whisper-medium" / filename), "voice-model")
        for filename in model_files
    ] + [
        (str(ROOT / "packaging" / "voice-model.json"), "voice-model"),
        (str(ROOT / "packaging" / "WHISPER-LICENSE.txt"), "voice-model"),
    ]
    assert all("*" not in source for source, _ in options["datas"])
    assert options["binaries"] == options["runtime_hooks"] == []
    assert options["hookspath"] == [str(ROOT / "packaging" / "hooks")]
    assert {"irsdk", "tkinter", "tkinter.ttk", "tkinter.filedialog"} <= set(
        options["hiddenimports"]
    )
    assert "numpy" not in options["excludes"] and "polars" not in options["excludes"]
    assert {"matplotlib", "PySide6", "PyQt6", "webview"} <= set(options["excludes"])
    assert {"faster_whisper", "ctranslate2", "tokenizers", "av", "sounddevice", "pygame"} <= set(
        options["hiddenimports"]
    )
    assert {
        "torch",
        "torchaudio",
        "tensorflow",
        "transformers",
        "nvidia",
        "cupy",
        "ctranslate2.converters",
        "ctranslate2.specs",
    } <= set(options["excludes"])
    args, options = calls["executable"]
    assert args == ("archive", [str(ENTRY)], ["dll"], ["tk"], [])
    assert options["name"] == "AEIS-Engineer" and options["console"] is False
    assert options["upx"] is False and options["strip"] is False
    assert options["uac_admin"] is False and options["uac_uiaccess"] is False
    assert options["disable_windowed_traceback"] is True


def test_build_dependency_is_pinned_separately_and_locked():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert project["dependency-groups"]["desktop-build"] == ["pyinstaller==6.22.3"]
    assert not any("pyinstaller" in dep.lower() for dep in project["project"]["dependencies"])
    packages = {package["name"]: package for package in lock["package"]}
    assert packages["pyinstaller"]["version"] == "6.22.3"
    assert any("win_amd64" in wheel["url"] for wheel in packages["pyinstaller"]["wheels"])


def test_build_script_bounds_outputs_and_checks_frozen_synthetic_receipt():
    text = BUILD.read_text(encoding="utf-8")
    for required in (
        "'build\\desktop'",
        "'dist'",
        "'AEIS-Engineer.exe'",
        "'AEIS-Engineer.build.json'",
        "--locked --group desktop-build --inexact",
        "--managed-python --python 3.12",
        "(Path(sys.base_prefix) / 'conda-meta').exists()",
        "-m PyInstaller --clean --noconfirm",
        "--self-test",
        "--self-test-output",
        "CreateNoWindow = $true",
        "ProcessWindowStyle]::Hidden",
        "WaitForExit(120000)",
        "engineer-desktop-self-test-v1",
        "WorkingDirectory = $distDirectory",
        "EnvironmentVariables['PATH']",
        "EnvironmentVariables.Remove($variable)",
        "'PYTHONPATH', 'PYTHONHOME', 'TCL_LIBRARY', 'TK_LIBRARY'",
        "'SYSTEM_PATH_ONLY'",
        "$receipt.sdk_accessed -isnot [bool]",
        "$receipt.provider_called -isnot [bool]",
        "$receipt.live_acceptance -isnot [bool]",
        "$receipt.native_gui -isnot [bool]",
        "-cne 'SYNTHETIC'",
        "-cne 'PASS'",
        "Get-FileHash",
        "-Algorithm SHA256",
        "$process.Dispose()",
        "self_test = $selfTestStatus",
        "live_acceptance = $false",
        "--self-test --voice-self-test --self-test-output",
        "voice_self_test = $selfTestStatus",
        "'VOICE_IO_IMPORT_ONLY'",
        "'PINNED_LOCAL_STT_MODEL_HASHES'",
        "'CHINESE_SYNTHETIC_TTS_TO_STT'",
        "'SYNTHETIC_PROXIMITY_TRANSITIONS'",
        "'SYNTHETIC_LEARNED_FUEL_QUERY'",
        "'SYNTHETIC_REPEATED_CORNER_QUERY'",
        "'SYNTHETIC_FUEL_STOP_QUERY'",
        "'SYNTHETIC_STINT_OBSERVATION_QUERY'", "'SYNTHETIC_RAW_PACE_QUERY'",
        "'SYNTHETIC_MAPPED_REJOIN_QUERY'",
        "'SYNTHETIC_RETAINED_STATE_BOUNDS'",
        "numerical_self_test = $selfTestStatus",
        "Where-Object { $_.id -ceq $requiredCheck }",
        "'HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN'",
        "'HF_TOKEN_PATH', 'HF_HOME', 'HF_HUB_CACHE'",
        "EnvironmentVariables['HF_HUB_OFFLINE'] = '1'",
        "EnvironmentVariables['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'",
    ):
        assert required in text
    for forbidden in (
        "Copy-Item",
        "--collect-all",
        "--add-data",
        "--uac-admin",
        "Start-Service",
        "Register-ScheduledTask",
        "private-archives",
        "data/raw",
        "Start-Process http",
    ):
        assert forbidden not in text
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "build/" in ignore and "dist/" in ignore
    verify = "& $pythonPath (Join-Path $repoRoot 'scripts\\fetch_voice_model.py')"
    assert text.index(verify) < text.index("& $pythonPath -m PyInstaller")
    assert "--download" not in text and "Pinned local speech model verification failed" in text


def _hook(monkeypatch, filename, *, libraries=()):
    calls = {"libraries": [], "metadata": []}
    helpers = ModuleType("PyInstaller.utils.hooks")

    def collect_dynamic_libs(package):
        calls["libraries"].append(package)
        return list(libraries)

    def copy_metadata(distribution):
        calls["metadata"].append(distribution)
        return [(f"/installed/{distribution}.dist-info", f"{distribution}.dist-info")]

    helpers.collect_dynamic_libs = collect_dynamic_libs
    helpers.copy_metadata = copy_metadata
    monkeypatch.setitem(sys.modules, helpers.__name__, helpers)
    path = ROOT / "packaging" / "hooks" / filename
    namespace = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return namespace, calls


def test_ctranslate2_hook_allowlists_only_cpu_dlls_and_metadata(monkeypatch):
    libraries = [
        (f"/installed/ctranslate2/{name}", "ctranslate2")
        for name in (
            "ctranslate2.dll",
            "libiomp5md.dll",
            "cudnn64_9.dll",
            "extra-library.dll",
        )
    ]
    hook, calls = _hook(monkeypatch, "hook-ctranslate2.py", libraries=libraries)
    assert calls == {"libraries": ["ctranslate2"], "metadata": ["ctranslate2"]}
    assert hook["binaries"] == libraries[:2]
    assert hook["hiddenimports"] == ["ctranslate2._ext", "ctranslate2.models"]
    assert {"ctranslate2.converters", "ctranslate2.specs", "torch", "transformers"} <= set(
        hook["excludedimports"]
    )


@pytest.mark.parametrize("missing", ["ctranslate2.dll", "libiomp5md.dll"])
def test_ctranslate2_hook_refuses_incomplete_cpu_runtime(monkeypatch, missing):
    libraries = [
        (f"/installed/ctranslate2/{name}", "ctranslate2")
        for name in (
            "ctranslate2.dll",
            "libiomp5md.dll",
        )
        if name != missing
    ]
    with pytest.raises(RuntimeError, match="^DESKTOP_CPU_SPEECH_LIBRARIES_MISSING$"):
        _hook(monkeypatch, "hook-ctranslate2.py", libraries=libraries)


def test_faster_whisper_hook_metadata_only_no_extra_model_or_cache(monkeypatch):
    hook, calls = _hook(monkeypatch, "hook-faster_whisper.py")
    assert calls["libraries"] == []
    assert calls["metadata"] == [
        "faster-whisper",
        "huggingface-hub",
        "tokenizers",
        "av",
        "onnxruntime",
        "sounddevice",
        "pygame",
    ]
    assert len(hook["datas"]) == 7
    assert all(source.endswith(".dist-info") for source, _ in hook["datas"])
    assert "tokenizers.tokenizers" in hook["hiddenimports"]
    for path in (ROOT / "packaging" / "hooks").glob("hook-*.py"):
        text = path.read_text(encoding="utf-8")
        assert "collect_all" not in text and "collect_data_files" not in text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell build script")
def test_build_script_parses_without_execution():
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell unavailable")
    # Parser only: no install, subprocess, SDK, GUI or build action is executed.
    command = (
        "$tokens = $null; $parseErrors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        "'" + str(BUILD).replace("'", "''") + "', [ref]$tokens, [ref]$parseErrors); "
        "if ($parseErrors.Count -gt 0) { exit 1 }; exit 0"
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
