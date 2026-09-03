"""Fail closed on high-confidence privacy and secret leaks before a public push."""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_PUBLIC_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_WINDOWS_USERS = frozenset(
    {"default", "example", "public", "racer", "runneradmin", "test", "testuser"}
)
ALLOWED_MAC_USERS = frozenset({"example", "racer", "runner", "test", "testuser"})
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.invalid", "users.noreply.github.com"}
)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    ("credential-in-url", re.compile(r"https?://[^/\s:@]+:[^@\s]+@")),
    (
        "assigned-secret",
        re.compile(
            r"\b(?:api[_-]?key|access[_-]?secret|client[_-]?secret|password|"
            r"passwd|auth[_-]?token)\b\s*[:=]\s*['\"]?"
            r"[A-Za-z0-9_./+=-]{8,}",
            re.IGNORECASE,
        ),
    ),
)
WINDOWS_HOME_RE = re.compile(
    r"\bC:[\\/]+Users[\\/]+(?P<user>[A-Z0-9_.-]+)", re.IGNORECASE
)
MAC_HOME_RE = re.compile(
    r"(?:^|[\s'\"])/Users/(?P<user>[A-Z0-9_.-]+)", re.IGNORECASE
)
EMAIL_RE = re.compile(
    r"\b[A-Z0-9._%+-]+@(?P<domain>[A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE
)
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SENSITIVE_PATH_RE = re.compile(
    r"(?:^|/)(?:\.env(?:\..*)?|id_rsa|id_ed25519|credentials?|secrets?|"
    r"[^/]+\.(?:key|pem|p12|pfx|etl|dmp|dump|pcap|pcapng))$",
    re.IGNORECASE,
)

# Split these strings so this scanner does not contain the forbidden values it
# is designed to detect.
KNOWN_PRIVATE_MARKERS = ()


@dataclass(frozen=True, order=True)
class Finding:
    rule: str
    location: str


def _run_git(root: Path, *args: str, text: bool = False) -> bytes | str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=text,
    )
    return completed.stdout


def _repository_root() -> Path:
    output = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return Path(output.strip()).resolve()


def _candidate_paths(root: Path) -> list[str]:
    output = _run_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    assert isinstance(output, bytes)
    return sorted({part.decode("utf-8") for part in output.split(b"\0") if part})


def _line_findings(text: str, location: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line_location = f"{location}:{line_number}"
        for rule, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(Finding(rule, line_location))

        if any(marker.lower() in line.lower() for marker in KNOWN_PRIVATE_MARKERS):
            findings.append(Finding("known-private-marker", line_location))

        for match in WINDOWS_HOME_RE.finditer(line):
            if match.group("user").casefold() not in ALLOWED_WINDOWS_USERS:
                findings.append(Finding("windows-user-home", line_location))

        for match in MAC_HOME_RE.finditer(line):
            if match.group("user").casefold() not in ALLOWED_MAC_USERS:
                findings.append(Finding("mac-user-home", line_location))

        if re.search(r"\b[a-z0-9-]+\.[a-z0-9-]+\.ts\.net\b", line, re.IGNORECASE):
            findings.append(Finding("tailscale-hostname", line_location))

        for match in EMAIL_RE.finditer(line):
            domain = match.group("domain").casefold()
            is_example_subdomain = domain.endswith(".example.com") or domain.endswith(
                ".example.invalid"
            )
            if domain not in ALLOWED_EMAIL_DOMAINS and not is_example_subdomain:
                findings.append(Finding("unexpected-email-domain", line_location))

        for match in IPV4_RE.finditer(line):
            try:
                address = ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            cgnat_network = ipaddress.ip_network(".".join(("100", "64", "0", "0")) + "/10")
            if address in cgnat_network:
                findings.append(Finding("cgnat-or-tailscale-address", line_location))
    return findings


def _scan_blob(data: bytes, location: str) -> list[Finding]:
    if len(data) > MAX_PUBLIC_FILE_BYTES:
        return [Finding("file-over-5-mib", location)]
    if b"\0" in data:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [Finding("non-utf8-text-or-binary", location)]
    return _line_findings(text, location)


def scan_worktree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for relative_path in _candidate_paths(root):
        normalized = relative_path.replace("\\", "/")
        if SENSITIVE_PATH_RE.search(normalized):
            findings.append(Finding("sensitive-path-name", relative_path))
        path = root / relative_path
        if path.is_file():
            findings.extend(_scan_blob(path.read_bytes(), relative_path))
    return findings


def scan_history(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    output = _run_git(root, "rev-list", "--objects", "--all", "--no-object-names", text=True)
    assert isinstance(output, str)
    object_ids = sorted(set(output.splitlines()))
    for object_id in object_ids:
        object_type = _run_git(root, "cat-file", "-t", object_id, text=True)
        assert isinstance(object_type, str)
        if object_type.strip() != "blob":
            continue
        data = _run_git(root, "cat-file", "blob", object_id)
        assert isinstance(data, bytes)
        findings.extend(_scan_blob(data, f"history-blob:{object_id[:12]}"))

    emails_output = _run_git(root, "log", "--all", "--format=%ae%n%ce", text=True)
    assert isinstance(emails_output, str)
    for email in sorted(set(emails_output.splitlines())):
        if email and not email.casefold().endswith("@users.noreply.github.com"):
            findings.append(Finding("non-noreply-commit-email", "git-history"))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--include-history",
        action="store_true",
        help="also scan every blob reachable from local refs and commit emails",
    )
    args = parser.parse_args()

    root = _repository_root()
    findings = scan_worktree(root)
    if args.include_history:
        findings.extend(scan_history(root))
    findings = sorted(set(findings))

    if findings:
        for finding in findings:
            print(f"FAIL {finding.rule} {finding.location}")
        print(f"FAIL_PUBLIC_SAFETY findings={len(findings)}")
        return 1

    history_status = "included" if args.include_history else "not-requested"
    print(f"PASS_PUBLIC_SAFETY history={history_status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
