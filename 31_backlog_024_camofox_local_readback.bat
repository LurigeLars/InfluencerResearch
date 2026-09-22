@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\InstagramResearch\venv"
set "PY=%VENV%\Scripts\python.exe"
set "LOG=%~dp0..\logs\backlog_024_camofox_local_readback.log"

if not exist "%PY%" (
  echo ERROR: Active InfluencerResearch Python runtime not found: "%PY%"
  pause
  exit /b 2
)
if not exist "%~dp0backlog_024_camofox_local_readback.py" (
  echo ERROR: Helper script missing beside this BAT file.
  pause
  exit /b 3
)

if not exist "%~dp0..\logs" mkdir "%~dp0..\logs" >nul 2>nul
(
  echo [%date% %time%] START
  "%PY%" "%~dp0backlog_024_camofox_local_readback.py"
  echo EXIT_CODE=!ERRORLEVEL!
) > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"

type "%LOG%"
echo.
echo Log: %LOG%
echo Exit code: %RC%
pause
exit /b %RC%
