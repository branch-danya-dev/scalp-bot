$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & (Join-Path $PSScriptRoot "run.ps1") -Profile ".env.research-p0-baseline-12h"
    if ($LASTEXITCODE -ne 0) {
        throw "P0 baseline server exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
