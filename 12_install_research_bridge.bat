@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp012_install_research_bridge.ps1"
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
    echo ERROR: InstagramResearch bridge installation/upgrade failed. Exit code %ERR%.
    echo See ..\logs\research_bridge_install.log
    pause
    exit /b %ERR%
)

echo Installation/upgrade complete and bridge v0.2.0 validated.
echo Scheduled Task autostart is configured for Windows logon.
pause
exit /b 0
