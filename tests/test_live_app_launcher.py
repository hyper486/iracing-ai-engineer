"""Windows launcher argument checks with all process/filesystem actions mocked."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path, PureWindowsPath

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "start_live_engineer.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="Windows PowerShell launcher integration",
)
MARKER = "LAUNCHER_TEST_JSON:"


def _launch_with_mocks(*, no_recording: bool, deepseek: bool = False) -> dict[str, object]:
    quoted_script = str(SCRIPT).replace("'", "''")
    option = " -NoRecording" if no_recording else ""
    if deepseek:
        option += (
            " -DeepSeek -DeepSeekModel deepseek-v4-pro -LlmRequestLimit 7"
            r" -SessionArtifact 'C:\Users\racer\Private Data\session.json'"
        )
    command = r"""
$ErrorActionPreference = 'Stop'
$env:LOCALAPPDATA = 'C:\Users\racer\AppData\Local\Private Data'
$env:DEEPSEEK_API_KEY = 'SYNTHETIC_ONLY_KEY'
$script:launchCalls = 0
$script:requestCalls = 0
$script:launchArguments = @()
$script:launchWindowStyle = ''
function Test-Path {
    param([string]$LiteralPath)
    return $true
}
function New-Item {
    param([string]$ItemType, [string]$Path, [switch]$Force)
    return [pscustomobject]@{ MockOnly = $true }
}
function Start-Sleep {
    param([int]$Milliseconds)
}
function Invoke-RestMethod {
    param([string]$Uri, [int]$TimeoutSec)
    $script:requestCalls++
    if ($script:requestCalls -eq 1) { throw 'Synthetic service not running' }
    return [pscustomobject]@{ contract_version = 'experimental-live-fuel-app-v1' }
}
function Start-Process {
    param(
        [string]$FilePath, [object[]]$ArgumentList, [string]$WindowStyle,
        [string]$WorkingDirectory, [switch]$PassThru,
        [string]$RedirectStandardOutput, [string]$RedirectStandardError
    )
    $script:launchCalls++
    $script:launchArguments = @($ArgumentList | ForEach-Object { [string]$_ })
    $script:launchWindowStyle = $WindowStyle
    return [pscustomobject]@{ Id = 4242; HasExited = $false }
}
"""
    command += (
        f"\n. '{quoted_script}' -NoBrowser -Port 9123 -Hours 1 "
        f"-ReserveLiters 3.5 -TankCapacityLiters 90{option}\n"
    )
    command += r"""
$result = [ordered]@{
    calls = $script:launchCalls
    requests = $script:requestCalls
    arguments = @($script:launchArguments)
    window_style = $script:launchWindowStyle
}
Write-Output ('LAUNCHER_TEST_JSON:' + (ConvertTo-Json -InputObject $result -Compress))
"""
    completed = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith(MARKER)]
    assert len(lines) == 1
    return json.loads(lines[0].removeprefix(MARKER))


@pytest.mark.parametrize("no_recording", [False, True])
def test_launcher_preserves_each_argument_and_quoted_paths_without_padding(no_recording):
    result = _launch_with_mocks(no_recording=no_recording)
    expected = [
        f'"{SCRIPT.parent / "run_local_cli.py"}"',
        "live-app", "--port", "9123", "--duration-seconds", "3600",
        "--reserve-liters", "3.5", "--tank-capacity-liters", "90",
    ]
    if not no_recording:
        capture = PureWindowsPath(
            r"C:\Users\racer\AppData\Local\Private Data\iRacingAIEngineer\captures"
        )
        expected.extend(["--record-directory", f'"{capture}"'])
    assert result["calls"] == 1  # The browser and a real worker are never launched.
    assert result["requests"] == 2
    assert result["window_style"] == "Hidden"
    assert result["arguments"] == expected
    assert all(argument.strip() == argument for argument in result["arguments"])
    for argument in result["arguments"]:
        if argument.startswith('"'):
            assert argument.endswith('"')
            assert argument[1:-1] == argument[1:-1].strip()
    if no_recording:
        assert "--record-directory" not in result["arguments"]


def test_deepseek_launcher_passes_config_and_quoted_receipt_but_never_key():
    result = _launch_with_mocks(no_recording=True, deepseek=True)
    arguments = result["arguments"]
    index = arguments.index("--llm-provider")
    assert arguments[index:index + 6] == [
        "--llm-provider", "deepseek", "--llm-model", "deepseek-v4-pro", "--llm-request-limit", "7",
    ]
    assert arguments[-2:] == [
        "--session-artifact", r'"C:\Users\racer\Private Data\session.json"',
    ]
    assert "SYNTHETIC_ONLY_KEY" not in str(arguments)
