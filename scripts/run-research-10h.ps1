$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "run.ps1") -Profile ".env.research-10h"

Write-Host ""
Write-Host "10h runner stopped."
Write-Host "Build the sharded post-run bundle with:"
Write-Host "  .\scripts\build-latest-10h-pack.ps1"
Write-Host "Upload the *-overview.zip first; keep raw JSONL local unless a shard is insufficient."
