# Build only the extracted, advisor-only reader. No downloads or simulator launch.
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$compilerPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compilerPath -PathType Leaf)) {
    $compilerPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $compilerPath -PathType Leaf)) {
    throw 'The Windows .NET Framework C# compiler is required; nothing was downloaded.'
}
$sourceDirectory = Join-Path $repoRoot 'native\crewchief_reader'
$sourceFiles = @(Get-ChildItem -LiteralPath $sourceDirectory -Filter '*.cs' -File |
    Sort-Object Name | ForEach-Object { $_.FullName })
if ($sourceFiles.Count -eq 0) {
    throw 'Crew Chief reader source is missing.'
}
$buildDirectory = Join-Path $repoRoot 'build\crewchief-reader'
$null = New-Item -ItemType Directory -Path $buildDirectory -Force
$outputPath = Join-Path $buildDirectory 'CrewChiefReader.exe'
& $compilerPath /nologo /langversion:5 /target:exe /platform:anycpu /optimize+ /debug- `
    /r:System.Web.Extensions.dll "/out:$outputPath" $sourceFiles
if ($LASTEXITCODE -ne 0) {
    throw 'Crew Chief reader compilation failed.'
}
foreach ($notice in @('LICENSE.CrewChief', 'UPSTREAM.md')) {
    Copy-Item -LiteralPath (Join-Path $sourceDirectory $notice) `
        -Destination (Join-Path $buildDirectory $notice)
}
Write-Output 'Built build/crewchief-reader/CrewChiefReader.exe (read-only, no simulator launch).'
