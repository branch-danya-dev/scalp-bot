param(
    [string]$ExportPath = "",
    [string]$RunsRepoPath = "",
    [string]$RunsRepoUrl = "https://github.com/branch-danya-dev/scalp-bot-runs.git",
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"

if (-not $RunsRepoPath) {
    $RunsRepoPath = Join-Path (Split-Path $PWD -Parent) "scalp-bot-runs"
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is not available in PATH."
}

if (-not (Test-Path $RunsRepoPath)) {
    Write-Host "Runs repository is not cloned locally. Trying: $RunsRepoUrl"
    & git clone $RunsRepoUrl $RunsRepoPath
    if ($LASTEXITCODE -ne 0) {
        throw (
            "Cannot clone $RunsRepoUrl. Create the dedicated repository once " +
            "(for example with: gh repo create branch-danya-dev/scalp-bot-runs " +
            "--public --add-readme), then run this command again."
        )
    }
}

& git -C $RunsRepoPath rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
    throw "RunsRepoPath is not a Git repository: $RunsRepoPath"
}

& git -C $RunsRepoPath switch main
if ($LASTEXITCODE -ne 0) {
    throw "scalp-bot-runs must have a main branch."
}
& git -C $RunsRepoPath pull --ff-only origin main
if ($LASTEXITCODE -ne 0) {
    throw "Cannot fast-forward scalp-bot-runs/main."
}

if (-not $ExportPath) {
    $exportRoot = Join-Path $PWD "data\git-exports"
    $latest = Get-ChildItem $exportRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name.StartsWith("session-") } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $latest) {
        throw "No Git session export found in $exportRoot."
    }
    $ExportPath = $latest.FullName
}

$ExportPath = (Resolve-Path $ExportPath).Path
$manifest = Join-Path $ExportPath "manifest.json"
if (-not (Test-Path $manifest)) {
    throw "Export manifest is missing: $manifest"
}

$rawSessions = Get-ChildItem $ExportPath -Recurse -File |
    Where-Object {
        $_.Name -like "session-*.jsonl" -and
        $_.DirectoryName -eq (Split-Path $ExportPath -Parent)
    }
if ($rawSessions) {
    throw "Refusing to publish a raw session JSONL."
}

$oversized = Get-ChildItem $ExportPath -Recurse -File |
    Where-Object { $_.Length -ge 95MB }
if ($oversized) {
    $names = ($oversized | ForEach-Object { $_.FullName }) -join ", "
    throw "Refusing Git push: files >=95 MB found: $names"
}

$sessionName = Split-Path $ExportPath -Leaf
$runsRoot = Join-Path $RunsRepoPath "runs"
$destination = Join-Path $runsRoot $sessionName
if (Test-Path $destination) {
    throw "Session is already published locally: $destination"
}

New-Item -ItemType Directory -Force -Path $runsRoot | Out-Null
Copy-Item $ExportPath $destination -Recurse

$totalBytes = (
    Get-ChildItem $destination -Recurse -File |
    Measure-Object -Property Length -Sum
).Sum
$totalMb = [math]::Round($totalBytes / 1MB, 1)

$gitRelative = "runs/$sessionName"
& git -C $RunsRepoPath add -- $gitRelative
if ($LASTEXITCODE -ne 0) {
    throw "git add failed for $gitRelative"
}

$status = & git -C $RunsRepoPath status --porcelain -- $gitRelative
if (-not $status) {
    throw "No changes detected for $gitRelative"
}

& git -C $RunsRepoPath commit -m "data: add $sessionName"
if ($LASTEXITCODE -ne 0) {
    throw "git commit failed."
}

if (-not $NoPush) {
    & git -C $RunsRepoPath push origin main
    if ($LASTEXITCODE -ne 0) {
        throw "git push failed."
    }
}

Write-Host ""
Write-Host "Published session: $sessionName"
Write-Host "Analysis export size: $totalMb MB"
Write-Host "Runs repository: $RunsRepoPath"
if ($NoPush) {
    Write-Host "Push skipped (-NoPush)."
}
