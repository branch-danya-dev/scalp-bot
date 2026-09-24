$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    throw ".venv is missing. Run .\scripts\setup.ps1 first."
}

& $venvPython .\scripts\export-git-session.py @args
if ($LASTEXITCODE -ne 0) {
    throw "Git-friendly session export failed."
}
