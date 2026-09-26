param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $IsWindows) {
    throw "Gemini DPAPI bootstrap is supported on Windows only."
}
if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is required."
}

$SecretDir = Join-Path $env:LOCALAPPDATA "InfluencerResearch\secrets"
$SecretPath = Join-Path $SecretDir "gemini_api_key.dpapi"
New-Item -ItemType Directory -Force -Path $SecretDir | Out-Null

$secureKey = Read-Host "Klistra in Gemini API key" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    if ([string]::IsNullOrWhiteSpace($plain)) {
        throw "Gemini API key must not be empty."
    }
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    $plain = $null
}

$encrypted = ConvertFrom-SecureString -SecureString $secureKey
$tmp = "$SecretPath.tmp"
[IO.File]::WriteAllText($tmp, $encrypted, [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $tmp -Destination $SecretPath -Force

Write-Host "GEMINI_KEY_STORED_DPAPI"
Write-Host "Path: $SecretPath"
Write-Host "The key is encrypted with Windows DPAPI for the current user and must never be committed."
