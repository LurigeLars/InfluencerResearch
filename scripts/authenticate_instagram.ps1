$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is required."
}

$App = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runtime = Join-Path $env:LOCALAPPDATA "InfluencerResearch"
$SecretDir = Join-Path $Runtime "secrets"
$SecretPath = Join-Path $SecretDir "instagram_cookies.dpapi"
$Venv = Join-Path $Runtime "auth-venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Setup = Join-Path $App "setup_auth.py"
$TempExport = Join-Path ([IO.Path]::GetTempPath()) ("influencerresearch-instagram-" + [guid]::NewGuid().ToString("N") + ".json")
$TempDpapi = "$SecretPath.tmp"

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

New-Item -ItemType Directory -Force -Path $SecretDir | Out-Null

$exportWasSet = Test-Path Env:INFLUENCER_RESEARCH_COOKIE_EXPORT_PATH
$oldExport = if ($exportWasSet) { $env:INFLUENCER_RESEARCH_COOKIE_EXPORT_PATH } else { $null }
$plain = $null
$secure = $null
$roundTripSecure = $null
$ptr = [IntPtr]::Zero

try {
    $env:INFLUENCER_RESEARCH_COOKIE_EXPORT_PATH = $TempExport

    & $Python $Setup
    if ($LASTEXITCODE -ne 0) {
        throw "Instagram authentication bootstrap failed with exit code $LASTEXITCODE."
    }
    if (-not (Test-Path -LiteralPath $TempExport -PathType Leaf)) {
        throw "Instagram authentication bootstrap did not produce a cookie export."
    }

    $plain = Get-Content -LiteralPath $TempExport -Raw
    $cookies = @($plain | ConvertFrom-Json)
    if (-not ($cookies | Where-Object { [string]$_.name -eq "sessionid" })) {
        throw "Instagram cookie export does not contain sessionid."
    }

    $secure = ConvertTo-SecureString -String $plain -AsPlainText -Force
    $encrypted = ConvertFrom-SecureString -SecureString $secure
    [IO.File]::WriteAllText($TempDpapi, $encrypted, [Text.UTF8Encoding]::new($false))

    $roundTripSecure = ConvertTo-SecureString -String (Get-Content -LiteralPath $TempDpapi -Raw)
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($roundTripSecure)
    $roundTrip = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    if ($roundTrip -ne $plain) {
        throw "Instagram DPAPI round-trip verification failed."
    }

    Move-Item -LiteralPath $TempDpapi -Destination $SecretPath -Force

    Write-Host ""
    Write-Host "Instagram session stored with Windows DPAPI."
    Write-Host "Plaintext cookie export removed."
    Write-Host "If the Docker runtime is already running, import it with:"
    Write-Host "  pwsh -NoProfile -File .\scripts\runtime.ps1 -Action ImportInstagramAuth"
}
finally {
    if ($ptr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
    $plain = $null
    $secure = $null
    $roundTripSecure = $null
    Remove-Item -LiteralPath $TempExport -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $TempDpapi -Force -ErrorAction SilentlyContinue

    if ($exportWasSet) {
        $env:INFLUENCER_RESEARCH_COOKIE_EXPORT_PATH = $oldExport
    }
    else {
        Remove-Item Env:INFLUENCER_RESEARCH_COOKIE_EXPORT_PATH -ErrorAction SilentlyContinue
    }
}
