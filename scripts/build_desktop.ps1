# Build an unsigned native one-file EXE. No SDK, provider, or browser is started.
[CmdletBinding()]
param(
    [string]$UvPath = 'uv',
    [switch]$SkipSelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'The desktop executable must be built on Windows.'
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$buildDirectory = Join-Path $repoRoot 'build\desktop'
$distDirectory = Join-Path $repoRoot 'dist'
$pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
$specPath = Join-Path $repoRoot 'packaging\desktop.spec'
$exePath = Join-Path $distDirectory 'AEIS-Engineer.exe'
$manifestPath = Join-Path $distDirectory 'AEIS-Engineer.build.json'
$null = New-Item -ItemType Directory -Path $buildDirectory -Force
$null = New-Item -ItemType Directory -Path $distDirectory -Force

# Preserve unrelated installed tools; install only the lockfile's build group.
$previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $repoRoot '.venv'
    & $UvPath sync --project $repoRoot --locked --group desktop-build --inexact `
        --managed-python --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Locked desktop build dependencies could not be installed.' }
} finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'The repository Python environment is unavailable.'
}
& $pythonPath -c "import sys,struct,tkinter,PyInstaller; from pathlib import Path; assert sys.version_info[:2] == (3,12); assert struct.calcsize('P') == 8; assert not (Path(sys.base_prefix) / 'conda-meta').exists(); assert PyInstaller.__version__ == '6.22.3'; tkinter.Tcl()"
if ($LASTEXITCODE -ne 0) {
    throw 'Managed non-Conda Python 3.12 x64, Tcl/Tk and the pinned PyInstaller are required.'
}

# Verification only: missing or modified weights must fail, never auto-download.
& $pythonPath (Join-Path $repoRoot 'scripts\fetch_voice_model.py')
if ($LASTEXITCODE -ne 0) { throw 'Pinned local speech model verification failed.' }

# Always re-analyze binaries after a Python/runtime update; never reuse host DLL cache.
& $pythonPath -m PyInstaller --clean --noconfirm --log-level WARN `
    --distpath $distDirectory --workpath $buildDirectory $specPath
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw 'Native desktop executable build failed.'
}

$selfTestStatus = 'NOT_RUN'
if (-not $SkipSelfTest) {
    # Exclusive random output prevents an earlier receipt from passing this build.
    $receiptPath = Join-Path $buildDirectory ('self-test-' + [guid]::NewGuid().ToString('N') + '.json')
    # Do not let a locally installed Python/Tcl or credential hide missing payloads.
    # ProcessStartInfo works on Windows PowerShell 5 as well as PowerShell 7.
    $startInfo = [Diagnostics.ProcessStartInfo]::new($exePath)
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.WorkingDirectory = $distDirectory
    $startInfo.Arguments = '--self-test --voice-self-test --self-test-output "' + $receiptPath + '"'
    $startInfo.EnvironmentVariables['PATH'] = (Join-Path $env:WINDIR 'System32') + ';' + $env:WINDIR
    foreach ($variable in @(
        'PYTHONPATH', 'PYTHONHOME', 'TCL_LIBRARY', 'TK_LIBRARY', 'TCLLIBPATH',
        'DEEPSEEK_API_KEY', 'CONDA_PREFIX', 'HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN',
        'HF_TOKEN_PATH', 'HF_HOME', 'HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE',
        'TRANSFORMERS_CACHE', 'HF_ENDPOINT', 'HF_HUB_OFFLINE',
        'CUDA_PATH', 'CUDA_HOME', 'CUDNN_PATH', 'LD_LIBRARY_PATH'
    )) {
        $startInfo.EnvironmentVariables.Remove($variable)
    }
    $startInfo.EnvironmentVariables['HF_HUB_OFFLINE'] = '1'
    $startInfo.EnvironmentVariables['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    $process = [Diagnostics.Process]::Start($startInfo)
    try {
        if (-not $process.WaitForExit(120000)) {
            # Kill only the owned frozen process tree (bootloader plus its child).
            $taskkillPath = Join-Path $env:WINDIR 'System32\taskkill.exe'
            & $taskkillPath /pid $process.Id /t /f | Out-Null
            throw 'Frozen desktop self-test timed out.'
        }
        $process.Refresh()
        if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
            throw 'Frozen desktop self-test failed.'
        }
    } finally {
        $process.Dispose()
    }
    if ((Get-Item -LiteralPath $receiptPath).Length -gt 65536) {
        throw 'Frozen desktop self-test receipt exceeded its size limit.'
    }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($receipt.contract_version -cne 'engineer-desktop-self-test-v1' -or
        $receipt.status -cne 'PASS' -or $receipt.source_kind -cne 'SYNTHETIC' -or
        $receipt.native_gui -isnot [bool] -or $receipt.native_gui -ne $true -or
        $receipt.sdk_accessed -isnot [bool] -or $receipt.sdk_accessed -ne $false -or
        $receipt.provider_called -isnot [bool] -or $receipt.provider_called -ne $false -or
        $receipt.live_acceptance -isnot [bool] -or $receipt.live_acceptance -ne $false -or
        @($receipt.checks).Count -eq 0 -or
        @($receipt.checks | Where-Object { $_.status -cne 'PASS' }).Count -ne 0) {
        throw 'Frozen desktop self-test receipt did not satisfy the synthetic-only contract.'
    }
    foreach ($requiredCheck in @(
        'VOICE_IO_IMPORT_ONLY', 'PINNED_LOCAL_STT_MODEL_HASHES', 'CHINESE_SYNTHETIC_TTS_TO_STT',
        'SYNTHETIC_PROXIMITY_TRANSITIONS', 'SYNTHETIC_LEARNED_FUEL_QUERY',
        'SYNTHETIC_REPEATED_CORNER_QUERY', 'SYNTHETIC_FUEL_STOP_QUERY',
        'SYNTHETIC_STINT_OBSERVATION_QUERY', 'SYNTHETIC_RAW_PACE_QUERY',
        'SYNTHETIC_MAPPED_REJOIN_QUERY',
        'SYNTHETIC_PIT_OBSERVATION_DRAFT',
        'SYNTHETIC_RETAINED_STATE_BOUNDS'
    )) {
        if (@($receipt.checks | Where-Object { $_.id -ceq $requiredCheck }).Count -ne 1) {
            throw 'Frozen desktop self-test receipt is missing a required runtime check.'
        }
    }
    $selfTestStatus = 'PASS'
}

$artifact = Get-Item -LiteralPath $exePath
$manifest = [ordered]@{
    contract_version = 'engineer-desktop-build-v1'
    artifact = 'AEIS-Engineer.exe'
    sha256 = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLowerInvariant()
    size_bytes = $artifact.Length
    platform = 'windows-x64'
    packaging = 'onefile-windowed'
    pyinstaller_version = '6.22.3'
    self_test = $selfTestStatus
    voice_self_test = $selfTestStatus
    numerical_self_test = $selfTestStatus
    self_test_environment = $(if ($SkipSelfTest) { 'NOT_RUN' } else { 'SYSTEM_PATH_ONLY' })
    authenticode_signed = ((Get-AuthenticodeSignature -LiteralPath $exePath).Status -eq 'Valid')
    live_acceptance = $false
}
$manifestJson = ($manifest | ConvertTo-Json -Depth 4) + [Environment]::NewLine
[IO.File]::WriteAllText($manifestPath, $manifestJson, [Text.UTF8Encoding]::new($false))
Write-Output ('Built dist/AEIS-Engineer.exe; self-test=' + $selfTestStatus + '; no live acceptance.')
