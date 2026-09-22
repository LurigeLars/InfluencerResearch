@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo ERROR: InstagramResearch Python environment not found.
  pause
  exit /b 2
)
echo Matteo Conti TikTok evaluation - CamoFox link/search fallback
echo Profile: https://www.tiktok.com/@matfinog
echo Sample size: 20
echo Instagram access: DISABLED
echo.
"%PY%" creator_evaluation.py --root "%~dp0.." --profile-url "https://www.tiktok.com/@matfinog" --creator-name "Matteo Conti" --sample-size 20
set "ERR=%ERRORLEVEL%"
echo.
echo FINAL EXIT CODE: %ERR%
echo Status: ..\state\creator_evaluation_status.json
echo Queue:  ..\state\research_queue.json
echo.
pause
exit /b %ERR%
