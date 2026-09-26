$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$savedRunEnvironment = @{}
Get-ChildItem Env:SCALP_* | ForEach-Object {
    $savedRunEnvironment[$_.Name] = $_.Value
}
$feeCredentials = @("SCALP_BYBIT_API_KEY", "SCALP_BYBIT_API_SECRET")
$transcriptStarted = $false

Push-Location -LiteralPath $projectRoot
try {
    # A previous run in this terminal must not change the checked-in profile.
    # Optional process credentials are used only for the account fee lookup.
    Get-ChildItem Env:SCALP_* | Where-Object { $_.Name -notin $feeCredentials } |
        ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" }

    $logDirectory = Join-Path $projectRoot "data/run-logs"
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $logPath = Join-Path $logDirectory "paper-current-1h-$stamp.log"
    Start-Transcript -LiteralPath $logPath -NoClobber | Out-Null
    $transcriptStarted = $true
    Write-Host "One bot, 60 minutes after Start. Main UI: http://127.0.0.1:8000/"
    Write-Host "Session data: data/sessions; terminal log: $logPath"

    & (Join-Path $PSScriptRoot "run.ps1") -Profile ".env.research-1h"
    if ($LASTEXITCODE -ne 0) {
        throw "Paper server exited with code $LASTEXITCODE. See $logPath"
    }
}
catch {
    Write-Host "Run failed: $($_.Exception.Message)"
    throw
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
    Get-ChildItem Env:SCALP_* | ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" }
    foreach ($name in $savedRunEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedRunEnvironment[$name], "Process")
    }
    Pop-Location
}
