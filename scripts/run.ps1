$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    throw ".venv is missing. Run .\scripts\setup.ps1 first."
}

# The run profile is forced here so an old local .env cannot silently restore
# the previous 10-hour duration or label.
$env:SCALP_RUN_LABEL = "paper-v3-4h"
$env:SCALP_PAPER_RUN_DURATION_SECONDS = "14400"

Write-Host ""
Write-Host "Scalp Bot — paper-run-v3-4h"
Write-Host "Configured trading duration: 4 hours after pressing Start in the UI."
Write-Host "Auto-stop will close remaining PAPER positions and write run_summary."
Write-Host "Order book: depth 1000 with stale/desync protection."
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
