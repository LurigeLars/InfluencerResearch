@echo off
setlocal
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"

if not exist "%PY%" (
  echo ERROR: local Python environment not found.
  echo Run 01_install.bat first.
  pause
  exit /b 2
)

echo TikTok POC: @nicholas_crown
echo Public profile, no login or browser automation.
echo Acceptance target: 10 MP4 + metadata + transcripts.
echo.

"%PY%" tiktok_ingest.py --root "%~dp0.." --creator nicholas_crown --profile-url "https://www.tiktok.com/@nicholas_crown" --max-videos 10
set ERR=%ERRORLEVEL%

echo.
echo TikTok POC exit code: %ERR%
echo Check ..\state\tiktok\status.json
pause
exit /b %ERR%
