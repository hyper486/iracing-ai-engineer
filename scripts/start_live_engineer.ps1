param(
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [ValidateRange(1, 12)][int]$Hours = 6,
    [ValidateRange(0, 100)][double]$ReserveLiters = 2,
    [ValidateRange(0, 1000)][double]$TankCapacityLiters = 0,
    [switch]$DeepSeek,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')][string]$DeepSeekModel = 'deepseek-flash',
    [ValidateRange(1, 500)][int]$LlmRequestLimit = 60,
    [ValidatePattern('^[^"\r\n]*$')][string]$SessionArtifact = '',
    [switch]$NoRecording,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$cliPath = Join-Path $PSScriptRoot 'run_local_cli.py'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python environment missing. Run uv sync --python 3.12 in the repository first.'
}
$url = "http://127.0.0.1:$Port/"
$alreadyRunning = $false
try {
    $currentState = Invoke-RestMethod -Uri "${url}api/state" -TimeoutSec 2
    $alreadyRunning = $currentState.contract_version -eq 'experimental-live-fuel-app-v1'
} catch { }
if (-not $alreadyRunning) {
    $privateRoot = Join-Path $env:LOCALAPPDATA 'iRacingAIEngineer'
    $logRoot = Join-Path $privateRoot 'logs'
    [void](New-Item -ItemType Directory -Path $logRoot -Force)
    $runToken = [guid]::NewGuid().ToString('N')
    $arguments = @(('"' + $cliPath + '"'), 'live-app', '--port', "$Port",
        '--duration-seconds', "$($Hours * 3600)", '--reserve-liters',
        $ReserveLiters.ToString([Globalization.CultureInfo]::InvariantCulture))
    if ($TankCapacityLiters -gt 0) {
        $arguments += @('--tank-capacity-liters',
            $TankCapacityLiters.ToString([Globalization.CultureInfo]::InvariantCulture))
    }
    if ($DeepSeek) {
        $arguments += @('--llm-provider', 'deepseek', '--llm-model', $DeepSeekModel,
            '--llm-request-limit', "$LlmRequestLimit")
        if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
            Write-Output 'DeepSeek key missing: local answers remain available. Set DEEPSEEK_API_KEY locally and restart to enable the model.'
        }
    }
    if ($SessionArtifact) {
        $arguments += @('--session-artifact', ('"' + $SessionArtifact + '"'))
    }
    if (-not $NoRecording) {
        $captureRoot = Join-Path $privateRoot 'captures'
        $arguments += @('--record-directory', ('"' + $captureRoot + '"'))
    }
    $worker = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WindowStyle Hidden `
        -WorkingDirectory $projectRoot -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot "$runToken.stdout.log") `
        -RedirectStandardError (Join-Path $logRoot "$runToken.stderr.log")
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 200
        if ($worker.HasExited) { throw 'The engineer app exited. Check the local application logs.' }
        try {
            $state = Invoke-RestMethod -Uri "${url}api/state" -TimeoutSec 1
            if ($state.contract_version -eq 'experimental-live-fuel-app-v1') { $ready = $true; break }
        } catch { }
    }
    if (-not $ready) { throw 'The engineer app is not responding. Check the local application logs.' }
    Write-Output "Engineer process: $($worker.Id). Stops automatically after $Hours hours."
} else {
    Write-Output 'Reusing the running engineer. New configuration parameters were not applied.'
}
if (-not $NoBrowser) { Start-Process -FilePath $url }
Write-Output "Engineer dashboard: $url"
Write-Output 'Experimental estimates only. Race audio stays muted; no simulator controls.'
