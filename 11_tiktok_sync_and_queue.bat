@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"
set "CLI=%LOCALAPPDATA%\InstagramResearch\camofox-poc\node_modules\.bin\camofox-browser.cmd"

if not exist "%PY%" (
  echo ERROR: InstagramResearch Python environment not found.
  echo Run 01_install.bat first.
  pause
  exit /b 2
)

if not exist "%CLI%" (
  echo ERROR: CamoFox is not installed.
  echo Run: pwsh -NoProfile -File scripts\install_camofox.ps1
  pause
  exit /b 3
)

echo TikTok production sync v0.7.0
echo ==============================
echo.
echo CamoFox discovers the feed.
echo Only previously unseen videos are downloaded.
echo Valid media is transcribed and merged into the shared Research Screen manifest.
echo Existing videos are never intentionally downloaded again.
echo.

"%PY%" tiktok_camofox_sync.py --root "%~dp0.."
set ERR=%ERRORLEVEL%

echo.
echo Exit code: %ERR%
echo Status: ..\state\tiktok\sync_status.json
echo Catalog: ..\state\tiktok\nicholascrown_catalog.json
echo Research queue: ..\state\research_queue.json
pause
exit /b %ERR%
