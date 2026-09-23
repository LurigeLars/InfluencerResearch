$ErrorActionPreference = "Stop"

$App = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Root = (Resolve-Path (Join-Path $App "..")).Path
$Runtime = Join-Path $env:LOCALAPPDATA "InstagramResearch"
$Venv = Join-Path $Runtime "venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$ChromeProfile = Join-Path $Runtime "chrome-profile"
$TikTokLock = Join-Path $App "requirements_tiktok_impersonation.lock.txt"
$Requirements = Join-Path $App "requirements.txt"
$LogDir = Join-Path $Root "logs"
$Log = Join-Path $LogDir "install.log"

New-Item -ItemType Directory -Force -Path $Runtime, $ChromeProfile, $LogDir | Out-Null

function Write-InstallLog([string]$Message) {
    $line = "{0} {1}" -f ([DateTimeOffset]::UtcNow.ToString("o")), $Message
    Write-Host $line
    Add-Content -LiteralPath $Log -Value $line -Encoding UTF8
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [Parameter(Mandatory=$true)][string[]]$ArgumentList
    )
    Write-InstallLog ("RUN " + $FilePath + " " + ($ArgumentList -join " "))
    & $FilePath @ArgumentList *>> $Log
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
    }
}

Write-InstallLog "INSTALL_START app=$App runtime=$Runtime"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Python launcher "py" was not found.'
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-InstallLog "Creating CPython 3.12 virtual environment"
    Invoke-Checked -FilePath "py" -ArgumentList @("-3.12", "-m", "venv", $Venv)
}

Invoke-Checked -FilePath $Python -ArgumentList @(
    "-c",
    "import platform,struct,sys; ok=(sys.version_info[:2]==(3,12) and struct.calcsize('P')*8==64 and platform.machine().upper()=='AMD64'); print('Python:',sys.version.split()[0],platform.machine(),str(struct.calcsize('P')*8)+'-bit'); raise SystemExit(0 if ok else 3)"
)

if (-not (Test-Path -LiteralPath $Requirements)) {
    throw "Missing requirements file: $Requirements"
}
if (-not (Test-Path -LiteralPath $TikTokLock)) {
    throw "Missing reviewed TikTok lock: $TikTokLock"
}

Write-InstallLog "Installing base Python dependencies"
Invoke-Checked -FilePath $Python -ArgumentList @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Checked -FilePath $Python -ArgumentList @("-m", "pip", "install", "-r", $Requirements)

Write-InstallLog "Installing reviewed TikTok impersonation dependency set"
Invoke-Checked -FilePath $Python -ArgumentList @(
    "-m", "pip", "install",
    "--only-binary=:all:",
    "--no-deps",
    "--require-hashes",
    "--force-reinstall",
    "-r", $TikTokLock
)

Invoke-Checked -FilePath $Python -ArgumentList @(
    "-c",
    "from importlib import metadata; exp={'yt-dlp':'2026.8.19','curl_cffi':'0.16.2','cffi':'2.1.1','pycparser':'3.0','certifi':'2026.7.22'}; got={k:metadata.version(k) for k in exp}; print('Reviewed dependency read-back:',got); raise SystemExit(0 if got==exp else 4)"
)
Invoke-Checked -FilePath $Python -ArgumentList @(
    "-c",
    "from playwright.sync_api import sync_playwright; import faster_whisper, imageio_ffmpeg; print('OK: dependencies import successfully'); print('Bundled ffmpeg:', imageio_ffmpeg.get_ffmpeg_exe())"
)

Write-InstallLog "INSTALL_PASS"
Write-Host ""
Write-Host "INSTALLATION OK"
Write-Host "Python: $Python"
Write-Host "Log:    $Log"
Write-Host "Existing authentication/session state was preserved."
