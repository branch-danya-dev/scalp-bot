param(
    [string]$RawArchiveRoot = "data\\raw-archives",
    [string]$RawArchiveMirrorPath = "",
    [string]$RunProfile = "",
    [string]$BotCommit = "",
    [int]$CompressionLevel = 10,
    [string]$RunsRepoPath = "",
    [string]$RunsRepoUrl = "https://github.com/branch-danya-dev/scalp-bot-runs.git",
    [double]$ShardMinutes = 30,
    [double]$MaxFileMb = 20,
    [switch]$OverwriteArchive,
    [switch]$OverwriteExport,
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "Step 1/2: preserving lossless raw session..."

$archiveArgs = @{
    RawArchiveRoot = $RawArchiveRoot
    CompressionLevel = $CompressionLevel
}
if ($RawArchiveMirrorPath) {
    $archiveArgs["RawArchiveMirrorPath"] = $RawArchiveMirrorPath
}
if ($RunProfile) {
    $archiveArgs["RunProfile"] = $RunProfile
}
if ($BotCommit) {
    $archiveArgs["BotCommit"] = $BotCommit
}
if ($OverwriteArchive) {
    $archiveArgs["OverwriteArchive"] = $true
}

& (Join-Path $PSScriptRoot "archive-latest-session.ps1") @archiveArgs
if ($LASTEXITCODE -ne 0) {
    throw "Raw preservation failed. Analysis export was not attempted."
}

Write-Host ""
Write-Host "Step 2/2: building and publishing bounded analysis export..."

$publishArgs = @{
    RunsRepoUrl = $RunsRepoUrl
    ShardMinutes = $ShardMinutes
    MaxFileMb = $MaxFileMb
}
if ($RunsRepoPath) {
    $publishArgs["RunsRepoPath"] = $RunsRepoPath
}
if ($OverwriteExport) {
    $publishArgs["OverwriteExport"] = $true
}
if ($NoPush) {
    $publishArgs["NoPush"] = $true
}

& (Join-Path $PSScriptRoot "export-and-publish-latest-session.ps1") @publishArgs
if ($LASTEXITCODE -ne 0) {
    throw ("Analysis export/publish failed, but the raw session archive " +
        "was already preserved successfully.")
}

Write-Host ""
Write-Host "Session finalization complete."
