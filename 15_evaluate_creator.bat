@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo ERROR: InfluencerResearch Python environment not found.
  echo Run 01_install.bat first.
  pause
  exit /b 2
)
set "PROFILE="
set /p "PROFILE=Creator profile URL (YouTube/TikTok; Instagram may be used as identifier): "
if "%PROFILE%"=="" (
  echo ERROR: No profile URL supplied.
  pause
  exit /b 3
)
set "NAME="
set /p "NAME=Creator display name [optional]: "
set "SAMPLE=20"
set /p "SAMPLE=Sample size [20]: "
if "%SAMPLE%"=="" set "SAMPLE=20"
echo.
echo InfluencerResearch creator evaluation starting.
echo Profile: %PROFILE%
echo Sample: %SAMPLE%
echo.
if "%NAME%"=="" (
  "%PY%" influencer_evaluation.py --root "%~dp0.." --profile-url "%PROFILE%" --sample-size "%SAMPLE%"
) else (
  "%PY%" influencer_evaluation.py --root "%~dp0.." --profile-url "%PROFILE%" --creator-name "%NAME%" --sample-size "%SAMPLE%"
)
set "ERR=%ERRORLEVEL%"
echo.
echo Exit code: %ERR%
echo Status: ..\state\creator_evaluation_status.json
echo Queue:  ..\state\research_queue.json
pause
exit /b %ERR%
