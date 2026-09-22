@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo ERROR: InstagramResearch Python environment not found.
  echo Run 01_install.bat first.
  pause
  exit /b 2
)

echo Matteo Conti creator evaluation - TikTok only
echo Web-verified candidate: https://www.tiktok.com/@matfinog
echo Sample size: 20
echo.

"%PY%" creator_evaluation.py --root "%~dp0.." --profile-url "https://www.tiktok.com/@matfinog" --creator-name "Matteo Conti" --sample-size 20
set "ERR=%ERRORLEVEL%"
echo.
echo Exit code: %ERR%
echo Status: ..\state\creator_evaluation_status.json
echo Queue:  ..\state\research_queue.json
echo.
if "%ERR%"=="0" (
  echo CREATOR EVALUATION COMPLETE.
) else (
  echo CREATOR EVALUATION DID NOT COMPLETE SUCCESSFULLY.
)
pause
exit /b %ERR%
