$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    throw ".venv is missing. Run .\scripts\setup.ps1 first."
}

Write-Host ""
Write-Host "Scalp Bot — paper-run-v2-10h"
Write-Host "Configured trading duration: 10 hours after pressing Start in the UI."
Write-Host "Auto-stop will close remaining PAPER positions and write run_summary."
Write-Host "Live:   http://127.0.0.1:8000/"
Write-Host "Replay: http://127.0.0.1:8000/replay"
Write-Host ""
Write-Host "Running preflight tests..."
& $venvPython -m pytest -q
if ($LASTEXITCODE -ne 0) {
    throw "Preflight tests failed. 10-hour run was not started."
}

Write-Host ""
Write-Host "Preflight passed. Starting server..."
& $venvPython -m uvicorn scalp_bot.app:app --host 127.0.0.1 --port 8000
