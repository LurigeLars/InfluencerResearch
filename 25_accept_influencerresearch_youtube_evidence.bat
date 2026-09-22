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

echo InfluencerResearch v0.10 acceptance - YouTube low-token evidence pipeline
echo Test creator: Matteo Conti / web-verified YouTube channel
echo Sample size: 3 existing/new items without visual evidence
echo Full video persistence: NO
echo.

"%PY%" youtube_creator_evaluation.py --root "%~dp0.." --channel-url "https://www.youtube.com/channel/UCQrLxbpidT8aCG3CFbM_YMA/shorts" --creator-key "matfinog" --creator-name "Matteo Conti" --sample-size 3
set "ERR=%ERRORLEVEL%"

echo.
echo FINAL EXIT CODE: %ERR%
echo Status: ..\state\creator_evaluation_status.json
echo Visual evidence: ..\output\matfinog\youtube\frames\
echo.
pause
exit /b %ERR%
