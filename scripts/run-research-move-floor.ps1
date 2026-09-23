$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "run.ps1") -Profile ".env.research-move-floor"
