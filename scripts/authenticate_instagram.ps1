$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$App = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runtime = Join-Path $env:LOCALAPPDATA "InfluencerResearch"
$Venv = Join-Path $Runtime "auth-venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Setup = Join-Path $App "setup_auth.py"

if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is required."
}
if (-not (Test-Path -LiteralPath $Setup -PathType Leaf)) {
    throw "Instagram authentication script not found: $Setup"
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw 'Python launcher "py" was not found.'
    }
    New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
    & py -3.12 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Unable to create Instagram auth virtual environment." }

    & $Python -m pip install --disable-pip-version-check "playwright==1.63.0"
    if ($LASTEXITCODE -ne 0) { throw "Unable to install Playwright for Instagram auth bootstrap." }
}

& $Python $Setup
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Instagram session export created."
Write-Host "If the Docker runtime is already running, import it with:"
Write-Host "  pwsh -NoProfile -File .\scripts\runtime.ps1 -Action ImportInstagramAuth"
exit 0
