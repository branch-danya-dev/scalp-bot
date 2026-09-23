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

function RunEnv([string]$Name) {
    return [Environment]::GetEnvironmentVariable($Name, "Process")
}

$riskPct = [math]::Round([double](RunEnv "SCALP_RISK_FRACTION") * 100, 3)
$allInPct = [math]::Round(
    [double](RunEnv "SCALP_MAX_TRADE_ALL_IN_LOSS_FRACTION") * 100,
    3
)
$positionLev = RunEnv "SCALP_MAX_POSITION_LEVERAGE"
$portfolioLev = RunEnv "SCALP_MAX_LEVERAGE"
$moveFloorPct = [math]::Round(
    [double](RunEnv "SCALP_MIN_FIRST_TAKE_MOVE_PCT") * 100,
    3
)
$moveFloorGate = RunEnv "SCALP_ENFORCE_MIN_FIRST_TAKE_MOVE_GATE"
$winnerGate = RunEnv "SCALP_ENFORCE_WINNER_COST_SHARE_GATE"
$netProfitGate = RunEnv "SCALP_ENFORCE_MIN_NET_PROFIT_GATE"
$rrGate = RunEnv "SCALP_ENFORCE_NET_REWARD_RISK_GATE"
$staged = RunEnv "SCALP_STAGED_ENTRIES_ENABLED"
$breakoutProbePct = [math]::Round(
    [double](RunEnv "SCALP_BREAKOUT_PROBE_RISK_FRACTION") * 100,
    1
)
$rejectionProbePct = [math]::Round(
    [double](RunEnv "SCALP_WEAK_LEVEL_REJECTION_PROBE_RISK_FRACTION") * 100,
    1
)
$idleEval = RunEnv "SCALP_EVALUATION_IDLE_INTERVAL_SECONDS"
$engagedEval = RunEnv "SCALP_EVALUATION_ENGAGED_INTERVAL_SECONDS"

Write-Host ""
Write-Host "Scalp Bot — $runLabel"
Write-Host "Run profile: $profileLabel (overrides checked-in .env.example)"
Write-Host "Configured trading duration: $durationMinutes minutes after pressing Start in the UI."
Write-Host "Auto-stop will close remaining PAPER positions and write run_summary."
Write-Host "Order book: depth 1000 with stale/desync protection and depth-aware fills."
Write-Host "Risk: base $riskPct% structural; $allInPct% max all-in loss; ${positionLev}x position / ${portfolioLev}x portfolio caps."
Write-Host "Economics gates: movement >=$moveFloorPct% ($moveFloorGate); winner-cost=$winnerGate; min-net=$netProfitGate; net-RR=$rrGate."
Write-Host "Staged entries: $staged; breakout probe ${breakoutProbePct}% / rejection probe ${rejectionProbePct}% of setup risk."
Write-Host "Evaluation cadence: idle ${idleEval}s / engaged ${engagedEval}s."
Write-Host "Execution: density/rejection can use PostOnly maker entry; staged adds preserve the same setup risk envelope."
Write-Host "Lifecycle: strategy-specific partial size and no-follow timeout; runner protected at net breakeven."
Write-Host "Live:   http://127.0.0.1:8000/"
Write-Host "Replay: http://127.0.0.1:8000/replay"
Write-Host ""
Write-Host "Running preflight tests..."
& $venvPython .\scripts\test_preflight.py
if ($LASTEXITCODE -ne 0) {
    throw "Preflight tests failed. Paper run was not started."
}

Write-Host ""
Write-Host "Checking live Bybit market data..."
& $venvPython .\scripts\market_preflight.py
if ($LASTEXITCODE -ne 0) {
    throw "Live Bybit market preflight failed. Server was not started."
}

Write-Host ""
Write-Host "Preflight passed. Starting server in this visible terminal..."
& $venvPython -m uvicorn scalp_bot.app:app --host 127.0.0.1 --port 8000
