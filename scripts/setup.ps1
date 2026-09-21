$ErrorActionPreference = "Stop"

Write-Host "Scalp Bot setup"

$python = $null

try {
    & py -3.12 --version | Out-Null
    $python = @("py", "-3.12")
} catch {
    try {
        & python --version | Out-Null
        $python = @("python")
    } catch {
        throw "Python 3.12+ was not found. Install Python and make sure 'py' or 'python' is available in PATH."
    }
}

if (-not (Test-Path ".venv")) {
    if ($python.Count -eq 2) {
        & $python[0] $python[1] -m venv .venv
    } else {
        & $python[0] -m venv .venv
    }
}

$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e ".[dev]"

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example"
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Run: powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1"
