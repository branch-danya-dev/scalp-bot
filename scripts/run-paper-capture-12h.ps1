param([switch]$Check)
& (Join-Path $PSScriptRoot "run-paper-capture.ps1") -Profile "12h" -Check:$Check
