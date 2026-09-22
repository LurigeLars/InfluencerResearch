@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"

if not exist "%PY%" (
  echo ERROR: InstagramResearch Python environment not found.
  pause
  exit /b 2
)

echo Updating yt-dlp with its default EJS dependencies...
"%PY%" -m pip install -U "yt-dlp[default]"
if errorlevel 1 (
  echo ERROR: yt-dlp update failed.
  pause
  exit /b 2
)

echo.
echo Node runtime check:
where node
if errorlevel 1 (
  echo Node not found on PATH. The evaluator will report this explicitly.
) else (
  node --version
)

echo.
echo Matteo Conti creator evaluation - YouTube retry v0.2.0
echo Sample size: 20
echo Instagram access: NO
echo.

"%PY%" youtube_creator_evaluation.py --root "%~dp0.." --channel-url "https://www.youtube.com/channel/UCQrLxbpidT8aCG3CFbM_YMA/shorts" --creator-key "matfinog" --creator-name "Matteo Conti" --sample-size 20
set "ERR=%ERRORLEVEL%"

echo.
echo FINAL EXIT CODE: %ERR%
echo Status: ..\state\creator_evaluation_status.json
echo Queue:  ..\state\research_queue.json
echo.
pause
exit /b %ERR%
