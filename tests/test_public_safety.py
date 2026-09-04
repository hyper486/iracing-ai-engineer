from __future__ import annotations

import runpy
import subprocess
from pathlib import Path

import pytest

SCANNER = runpy.run_path("scripts/check_public_safety.py")


def _account_sid() -> str:
    # An invented non-placeholder account exercises the generic detector.
    return "-".join(("S", "1", "5", "21", "11", "22", "33", "1001"))


def test_account_sid_is_detected_without_echoing_the_identifier() -> None:
    identifier = _account_sid()
    findings = SCANNER["_line_findings"](f"user={identifier}", "example.py")
    assert [item.rule for item in findings] == ["windows-account-sid"]
    assert identifier not in repr(findings)


def test_documented_placeholder_and_builtin_sids_are_allowed() -> None:
    assert SCANNER["_line_findings"](
        "S-1-5-21-0-0-0-1001 S-1-5-18 S-1-5-32-544", "example.py"
    ) == []


def test_lowercase_machine_sid_without_account_rid_is_detected() -> None:
    machine_sid = _account_sid().rsplit("-", 1)[0].lower()
    findings = SCANNER["_line_findings"](machine_sid, "example.py")
    assert [item.rule for item in findings] == ["windows-account-sid"]


@pytest.mark.parametrize("suffix", ["ibt", "rpy", "jsonl", "parquet"])
def test_telemetry_paths_are_blocked_even_when_contents_are_text(suffix: str) -> None:
    assert SCANNER["SENSITIVE_PATH_RE"].search(f"uploads/session.{suffix}")


def test_binary_content_is_not_an_implicit_privacy_exemption() -> None:
    findings = SCANNER["_scan_blob"](b"\0Private SessionInfo", "renamed.dat")
    assert [item.rule for item in findings] == ["unreviewed-binary"]


def test_removed_account_sid_is_still_found_in_reachable_history(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    git("config", "user.name", "Review Fixture")
    git("config", "user.email", "fixture@users.noreply.github.com")
    source = tmp_path / "example.py"
    source.write_text(f"user = {_account_sid()!r}\n", encoding="utf-8")
    git("add", "example.py")
    git("commit", "-m", "Add invented account fixture")
    source.write_text("user = 'public-placeholder'\n", encoding="utf-8")
    git("add", "example.py")
    git("commit", "-m", "Remove account from current tree")
    assert SCANNER["scan_worktree"](tmp_path) == []
    findings = SCANNER["scan_history"](tmp_path)
    assert {item.rule for item in findings} == {"windows-account-sid"}


def test_removed_telemetry_path_and_commit_metadata_are_scanned(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    git("config", "user.name", "Review Fixture")
    git("config", "user.email", "fixture@users.noreply.github.com")
    source = tmp_path / "session.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    git("add", "session.jsonl")
    git("commit", "-m", f"Invented metadata identity {_account_sid()}")
    git("rm", "session.jsonl")
    git("commit", "-m", "Remove telemetry file")
    assert SCANNER["scan_worktree"](tmp_path) == []
    findings = SCANNER["scan_history"](tmp_path)
    assert {item.rule for item in findings} == {"windows-account-sid", "sensitive-path-name"}
