$ErrorActionPreference = "Stop"

$App = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $env:LOCALAPPDATA "InstagramResearch\venv\Scripts\python.exe"
$Setup = Join-Path $App "setup_auth.py"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "InfluencerResearch Python environment not found. Run scripts\install.ps1 first."
}
if (-not (Test-Path -LiteralPath $Setup)) {
    throw "Instagram authentication script not found: $Setup"
}

& $Python $Setup
exit $LASTEXITCODE
