param(
    [ValidateSet("Preflight", "ConnectedPreflight", "Start")][string]$Action = "Preflight",
    [string]$ModelDir = "G:\scalp-bot\data\pr58-completion\model-v2",
    [string]$Output = "",
    [string]$ConfirmPassport = ""
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Create the separate local venv and install .[dev,ml] first." }
if (-not $Output) { $Output = Join-Path $projectRoot ("data\audit-demo-paper-1h-" + (Get-Date -Format "yyyyMMdd-HHmmss")) }
$verb = @{Preflight="preflight"; ConnectedPreflight="connected-preflight"; Start="start"}[$Action]
if ($Action -eq "Start" -and -not $ConfirmPassport) { throw "Run Preflight, review the passport, then supply -ConfirmPassport with its hash." }
# Run directly in this visible Windows terminal. No hidden jobs, restart or live mode.
Push-Location -LiteralPath $projectRoot
try {
    $arguments = @("-m", "scalp_bot.demo_paper", $verb, "--model-dir", $ModelDir, "--output", $Output)
    if ($ConfirmPassport) { $arguments += @("--confirm-passport", $ConfirmPassport) }
    & $pythonPath @arguments
    if ($LASTEXITCODE -ne 0) { throw "Demo/paper command did not complete; inspect the retained local result." }
} finally { Pop-Location }
