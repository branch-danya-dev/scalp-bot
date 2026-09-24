param(
    [string]$RawArchiveRoot = "data\raw-archives",
    [string]$RawArchiveMirrorPath = "",
    [string]$RunProfile = "",
    [int]$CompressionLevel = 10,
    [switch]$OverwriteArchive
)

$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw ".venv is missing. Run .\scripts\setup.ps1 first."
}

$botCommit = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $botCommit) {
    throw "Cannot resolve current Git commit."
}

$argsList = @(
    ".\scripts\archive-raw-session.py",
    "--output-root", $RawArchiveRoot,
    "--compression-level", "$CompressionLevel",
    "--bot-commit", $botCommit
)
if ($RawArchiveMirrorPath) {
    $argsList += @(
        "--mirror-root",
        $RawArchiveMirrorPath
    )
}
if ($RunProfile) {
    $argsList += @(
        "--run-profile",
        $RunProfile
    )
}
if ($OverwriteArchive) {
    $argsList += "--overwrite"
}

& $venvPython @argsList
if ($LASTEXITCODE -ne 0) {
    throw "Raw session archival failed."
}
