@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"
set "LOG=%ROOT%\logs\backlog_024_influencer_hardening_acceptance.log"
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
>"%LOG%" echo [%date% %time%] START
if not exist "%PY%" (
  >>"%LOG%" echo FAIL: runtime Python missing: %PY%
  echo FAIL: runtime Python missing.
  echo Log: %LOG%
  pause
  exit /b 2
)
"%PY%" "%~dp0backlog_024_influencer_hardening_acceptance.py" >>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
type "%LOG%"
echo.
echo Exit code: %RC%
echo Log: %LOG%
pause
exit /b %RC%
