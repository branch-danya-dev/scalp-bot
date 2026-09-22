$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"
$runProfile = Join-Path $PWD ".env.example"

if (-not (Test-Path $venvPython)) {
    throw ".venv is missing. Run .\scripts\setup.ps1 first."
}
if (-not (Test-Path $runProfile)) {
    throw ".env.example run profile is missing."
}

# Load the checked-in run profile into the current process. Process variables
# have precedence over a stale local .env, so the paper run is reproducible.
Get-Content $runProfile | ForEach-Object {
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

Write-Host ""
Write-Host "Scalp Bot — paper-v3-scalp-econ-4h"
Write-Host "Run profile: .env.example (forced over stale local .env values)"
Write-Host "Configured trading duration: 4 hours after pressing Start in the UI."
Write-Host "Auto-stop will close remaining PAPER positions and write run_summary."
Write-Host "Order book: depth 1000 with stale/desync protection."
Write-Host "Scalp economics: risk 0.5%/trade, 5x max position, 10x max portfolio."
Write-Host "Economic gate: max($1, 0.1% equity) net at configured target."
Write-Host "Partial: >=1R AND economically net-positive; runner protected at net breakeven."
Write-Host "Live:   http://127.0.0.1:8000/"
Write-Host "Replay: http://127.0.0.1:8000/replay"
Write-Host ""
Write-Host "Running preflight tests..."
& $venvPython -m pytest -q
if ($LASTEXITCODE -ne 0) {
    throw "Preflight tests failed. 4-hour run was not started."
}

Write-Host ""
Write-Host "Preflight passed. Starting server in this visible terminal..."
& $venvPython -m uvicorn scalp_bot.app:app --host 127.0.0.1 --port 8000
