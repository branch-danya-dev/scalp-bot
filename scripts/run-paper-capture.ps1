param(
    [ValidateSet("1h", "12h", "24h")][string]$Profile = "1h",
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$savedCaptureEnvironment = @{}
Get-ChildItem Env:SCALP_* | ForEach-Object { $savedCaptureEnvironment[$_.Name] = $_.Value }
$transcriptStarted = $false
Push-Location -LiteralPath $projectRoot
try {
    # Both dotenv and inherited process overrides must be excluded.
    Get-ChildItem Env:SCALP_* | ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" }
    $venvPython = Join-Path $projectRoot ".venv/Scripts/python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) { throw "Run scripts/setup.ps1 first." }
    $profileFile = ".env.paper-capture-$Profile"
    foreach ($file in @(".env.example", $profileFile)) {
        Get-Content -LiteralPath (Join-Path $projectRoot $file) | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
                $parts = $line.Split("=", 2)
                [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
            }
        }
    }
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $suffix = [guid]::NewGuid().ToString("N").Substring(0, 8)
    $captureDirectory = Join-Path $projectRoot "data/paper-captures/$Profile-$stamp-$suffix"
    [Environment]::SetEnvironmentVariable("SCALP_SESSION_DIR", $captureDirectory, "Process")
    & $venvPython (Join-Path $PSScriptRoot "paper-capture-preflight.py")
    if ($LASTEXITCODE -ne 0) { throw "Capture profile preflight failed." }
    if (-not $Check) {
        New-Item -ItemType Directory -Path $captureDirectory | Out-Null
        Start-Transcript -LiteralPath (Join-Path $captureDirectory "terminal.log") -NoClobber | Out-Null
        $transcriptStarted = $true
        Write-Host "One bot. Main UI: http://127.0.0.1:8000/"
        Write-Host "Press Start when market data is ready. After trading stops, capture seals automatically."
        Write-Host "Wait for the saved-capture message in the UI. Charts remain available; Ctrl+C only closes the server."
        Write-Host "Do not edit source files or update dependencies during the capture."
        Write-Host "After capture is saved, verify in another terminal: .\scripts\check-paper-capture.ps1 -Profile $Profile"
        & (Join-Path $PSScriptRoot "run.ps1") -Profile $profileFile -SessionDirectory $captureDirectory
        if ($LASTEXITCODE -ne 0) { throw "Server exited with code $LASTEXITCODE. Preserve $captureDirectory" }
    }
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
    Get-ChildItem Env:SCALP_* | ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" }
    foreach ($name in $savedCaptureEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedCaptureEnvironment[$name], "Process")
    }
    Pop-Location
}
