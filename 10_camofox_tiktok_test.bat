@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"
set "CAMOFOX_CONFIG=%LOCALAPPDATA%\InfluencerResearch\camofox-container.json"

if not exist "%PY%" (
  echo ERROR: InstagramResearch Python environment not found.
  echo Run 01_install.bat first.
  pause
  exit /b 2
)

if not exist "%CAMOFOX_CONFIG%" (
  echo ERROR: CamoFox Docker config is missing.
  echo Run: pwsh -NoProfile -File scripts\camofox_container.ps1 -Action Up
  pause
  exit /b 3
)

echo CamoFox + TikTok hybrid POC
echo ===========================
echo.
echo Stage 1: CamoFox opens @nicholas_crown and discovers real video URLs.
echo Stage 2: yt-dlp tests up to 3 individual discovered video URLs.
echo.
echo No TikTok login is required for this first test.
echo CamoFox runs in Docker and is exposed only on host loopback 127.0.0.1:9377.
echo.

"%PY%" camofox_tiktok_poc.py
set ERR=%ERRORLEVEL%

echo.
echo Exit code: %ERR%
echo State: ..\state\tiktok\camofox_poc_status.json
echo URLs:  ..\state\tiktok\camofox_discovered_urls.json
pause
exit /b %ERR%
