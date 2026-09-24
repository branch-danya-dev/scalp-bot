param(
    [string]$RunsRepoPath = "",
    [string]$RunsRepoUrl = "https://github.com/branch-danya-dev/scalp-bot-runs.git",
    [double]$ShardMinutes = 30,
    [double]$MaxFileMb = 20,
    [switch]$OverwriteExport,
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw ".venv is missing. Run .\scripts\setup.ps1 first."
}

$argsList = @(
    ".\scripts\export-git-session.py",
    "--shard-minutes", "$ShardMinutes",
    "--max-file-mb", "$MaxFileMb"
)
if ($OverwriteExport) {
    $argsList += "--overwrite"
}

Write-Host "Building Git-friendly export from latest session..."
$output = & $venvPython @argsList
if ($LASTEXITCODE -ne 0) {
    throw "Git-friendly session export failed."
}
$exportPath = (
    $output |
    Where-Object { $_ -and $_.Trim() } |
    Select-Object -Last 1
).Trim()

$publishArgs = @{
    ExportPath = $exportPath
    RunsRepoUrl = $RunsRepoUrl
}
if ($RunsRepoPath) {
    $publishArgs["RunsRepoPath"] = $RunsRepoPath
}
if ($NoPush) {
    $publishArgs["NoPush"] = $true
}

& (Join-Path $PSScriptRoot "publish-session-export.ps1") @publishArgs
