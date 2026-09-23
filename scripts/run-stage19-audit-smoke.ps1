$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "run.ps1") -Profile ".env.research-stage19-audit-smoke"
