param(
    [ValidateSet("Paper", "Demo")][string]$Mode = "Paper",
    [ValidateSet("Check", "ConnectedCheck", "Start", "Stop", "Archive")][string]$Action = "Check",
    [string]$Output = "",
    [string]$Credentials = ".env.demo-paper.local",
    [int]$Port = 8010,
    [ValidateRange(30,900)][int]$SmokeSeconds = 60,
    [switch]$Smoke,
    [switch]$Check
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Run .\scripts\setup.ps1 first." }
if ($Check) { $Action = "Check" }
if (-not $Output) { $Output = "data\trading-24h-v1\$($Mode.ToLower())-" + (Get-Date -Format "yyyyMMdd-HHmmss") }
if (-not [IO.Path]::IsPathRooted($Output)) { $Output = Join-Path $projectRoot $Output }
$Output = [IO.Path]::GetFullPath($Output)
Push-Location -LiteralPath $projectRoot
try {
    if ($Action -eq "Archive") {
        $summary = Join-Path $Output "summary.json"
        if (-not (Test-Path -LiteralPath $summary)) { throw "Wait for finalization; summary.json is missing." }
        if (-not (Get-Content -LiteralPath $summary -Raw | ConvertFrom-Json).finalized) { throw "Finalization incomplete; inspect the retained run before archiving." }
        & $pythonPath (Join-Path $PSScriptRoot "archive-trading-run.py") $Output
        if ($LASTEXITCODE -ne 0) { throw "Archive failed; original run files are retained." }
        return
    }
    if ($Smoke -and ($Mode -ne "Paper" -or $Action -ne "Start")) { throw "Smoke is only valid for Paper Start." }
    $verb = @{ Check="check"; ConnectedCheck="connected-check"; Start="start"; Stop="stop" }[$Action]
    $arguments = @("-m", "scalp_bot.trading24h", $verb, "--mode", $Mode.ToLower(), "--output", $Output, "--port", "$Port", "--credentials", $Credentials)
    if ($Smoke) { $arguments += @("--smoke-seconds", "$SmokeSeconds") }
    & $pythonPath @arguments
    if ($LASTEXITCODE -ne 0) { throw "Trading command failed (exit $LASTEXITCODE). Retain diagnostics in $Output." }
} finally { Pop-Location }
