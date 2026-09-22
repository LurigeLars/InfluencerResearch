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

"%PY%" setup_auth.py
set ERR=%ERRORLEVEL%
echo.
if not "%ERR%"=="0" (
  echo Authentication failed with code %ERR%.
) else (
  echo Authentication/session setup complete.
)
pause
exit /b %ERR%
