param(
    [ValidateSet("1h", "12h", "24h")][string]$Profile = "1h",
    [string]$Directory = ""
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$savedDotenv = [Environment]::GetEnvironmentVariable("SCALP_DISABLE_DOTENV", "Process")
Push-Location -LiteralPath $projectRoot
try {
    $env:SCALP_DISABLE_DOTENV = "1"
    if (-not $Directory) {
        $latest = Get-ChildItem -LiteralPath (Join-Path $projectRoot "data/paper-captures") -Directory |
            Where-Object { $_.Name.StartsWith("$Profile-") } |
            Sort-Object Name -Descending | Select-Object -First 1
        if (-not $latest) { throw "No $Profile capture found." }
        $Directory = $latest.FullName
    }
    Write-Host "Checking capture: $Directory"
    Write-Host "This is an offline baseline replay. No connection to Bybit."
    & (Join-Path $projectRoot ".venv/Scripts/python.exe") (Join-Path $PSScriptRoot "replay-paper-capture.py") $Directory
    if ($LASTEXITCODE -ne 0) { throw "Capture replay failed. Preserve its files for diagnosis." }
}
finally {
    [Environment]::SetEnvironmentVariable("SCALP_DISABLE_DOTENV", $savedDotenv, "Process")
    Pop-Location
}
