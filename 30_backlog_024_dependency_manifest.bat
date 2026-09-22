@echo off
setlocal
cd /d "%~dp0"

set "VENV=%LOCALAPPDATA%\InstagramResearch\venv"
set "PY=%VENV%\Scripts\python.exe"
set "LOG=%~dp0..\logs\backlog_024_dependency_manifest.log"

echo ============================================================
echo BACKLOG_024 InfluencerResearch dependency manifest
echo Read-only: installs/upgrades nothing.
echo ============================================================
echo.

if not exist "%PY%" (
  echo ERROR: Active InfluencerResearch venv python not found:
  echo %PY%
  echo.
  echo [%date% %time%] ERROR venv python missing: %PY%>"%LOG%"
  pause
  exit /b 2
)

if not exist "%~dp0..\logs" mkdir "%~dp0..\logs"

echo [%date% %time%] START>"%LOG%"
"%PY%" "%~dp0backlog_024_dependency_manifest.py" >>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] EXIT_CODE=%RC%>>"%LOG%"

echo.
if "%RC%"=="0" (
  echo MANIFEST COMPLETE.
  echo State file: %~dp0..\state\backlog_024_influencer_dependency_manifest.json
) else (
  echo MANIFEST FAILED. Exit code %RC%.
)
echo Log: %LOG%
echo.
pause
exit /b %RC%
