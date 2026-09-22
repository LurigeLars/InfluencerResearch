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

"%PY%" ephemeral_ingest.py --root "%~dp0.." --mode stories --configured
set ERR=%ERRORLEVEL%
echo.
echo Story scan exit code: %ERR%
echo Check ..\state\ephemeral_status.json
pause
exit /b %ERR%
