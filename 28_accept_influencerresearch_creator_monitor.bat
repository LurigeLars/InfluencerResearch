@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo ERROR: InfluencerResearch Python environment not found.
  echo Run 01_install.bat first.
  pause
  exit /b 2
)
echo InfluencerResearch v0.11 recurring creator-monitor acceptance
"%PY%" creator_monitor_acceptance.py
set "ERR=%ERRORLEVEL%"
echo.
echo FINAL EXIT CODE: %ERR%
echo Status: ..\state\creator_monitor_acceptance_status.json
 pause
exit /b %ERR%
