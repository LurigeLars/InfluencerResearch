@echo off
setlocal
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "SCRIPT=%~dp012_install_research_bridge.ps1"

if not exist "%PS%" (
  echo ERROR: expected Windows PowerShell executable not found: %PS% 1>&2
  exit /b 2
)
if not exist "%SCRIPT%" (
  echo ERROR: reviewed installer/uninstaller script not found: %SCRIPT% 1>&2
  exit /b 2
)

"%PS%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%SCRIPT%" -Mode Uninstall
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo ERROR: verified InstagramResearch bridge uninstall failed with exit code %RC%. 1>&2
  exit /b %RC%
)

echo InstagramResearch bridge uninstall verified complete.
exit /b 0
