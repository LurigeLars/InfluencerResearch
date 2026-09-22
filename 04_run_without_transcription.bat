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

"%PY%" instagram_ingest.py --root "%~dp0.." --skip-transcription
set ERR=%ERRORLEVEL%
echo.
echo Pipeline exit code: %ERR%
echo Check ..\state\status.json for authoritative run status.
pause
exit /b %ERR%
