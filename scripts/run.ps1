param(
    [string]$Profile = ""
)

$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"
$baseProfile = Join-Path $PWD ".env.example"

if (-not (Test-Path $venvPython)) {
    throw ".venv is missing. Run .\scripts\setup.ps1 first."
}
if (-not (Test-Path $baseProfile)) {
    throw ".env.example run profile is missing."
}

function Import-RunProfile([string]$Path) {
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $parts = $line.Split("=", 2)
            [Environment]::SetEnvironmentVariable(
                $parts[0].Trim(),
                $parts[1].Trim(),
                "Process"
            )
        }
    }
}

# Always load the checked-in baseline first so a stale local .env cannot
# silently change the research run. An optional profile then overrides only
# the fields that differ from the baseline.
Import-RunProfile $baseProfile

$profileLabel = ".env.example"
if ($Profile) {
    $resolvedProfile = Join-Path $PWD $Profile
    if (-not (Test-Path $resolvedProfile)) {
        throw "Run profile not found: $Profile"
    }
    Import-RunProfile $resolvedProfile
    $profileLabel = $Profile
}

$runLabel = [Environment]::GetEnvironmentVariable("SCALP_RUN_LABEL", "Process")
$durationSecondsRaw = [Environment]::GetEnvironmentVariable(
    "SCALP_PAPER_RUN_DURATION_SECONDS",
    "Process"
)
$durationSeconds = [double]$durationSecondsRaw
$durationMinutes = [math]::Round($durationSeconds / 60, 2)

Write-Host ""
Write-Host "Scalp Bot — $runLabel"
Write-Host "Run profile: $profileLabel (overrides checked-in .env.example)"
Write-Host "Configured trading duration: $durationMinutes minutes after pressing Start in the UI."
Write-Host "Auto-stop will close remaining PAPER positions and write run_summary."
Write-Host "Order book: depth 1000 with stale/desync protection and depth-aware fills."
Write-Host "Risk: 0.5% all-in planned loss/trade, 5x max position, 10x max portfolio."
Write-Host "Economic gate: max($1, 0.1% equity) net target and net R:R >= 1.15."
Write-Host "Partial: >=1R AND economically net-positive; runner protected at net breakeven."
Write-Host "Live:   http://127.0.0.1:8000/"
Write-Host "Replay: http://127.0.0.1:8000/replay"
Write-Host ""
Write-Host "Running preflight tests..."
& $venvPython -m pytest -q
if ($LASTEXITCODE -ne 0) {
    throw "Preflight tests failed. Paper run was not started."
}

Write-Host ""
Write-Host "Preflight passed. Starting server in this visible terminal..."
& $venvPython -m uvicorn scalp_bot.app:app --host 127.0.0.1 --port 8000
