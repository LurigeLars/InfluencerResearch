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

"%PY%" research_queue.py --root "%~dp0.."
set ERR=%ERRORLEVEL%
echo.
echo Research queue exit code: %ERR%
echo Check ..\state\research_queue.json
pause
exit /b %ERR%
