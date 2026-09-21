$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    throw ".venv is missing. Run .\scripts\setup.ps1 first."
}

& $venvPython -m uvicorn scalp_bot.app:app --host 127.0.0.1 --port 8000
