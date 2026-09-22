@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo ERROR: InstagramResearch Python environment not found.
  pause
  exit /b 2
)

echo Updating yt-dlp to current stable...
"%PY%" -m pip install --disable-pip-version-check --quiet --upgrade "yt-dlp>=2026.8.19,<2027"
if errorlevel 1 (
  echo ERROR: yt-dlp update failed.
  pause
  exit /b 3
)

"%PY%" -c "import creator_evaluation as c; print('Creator Evaluation:', c.EVAL_VERSION); assert c.EVAL_VERSION == '0.3.0'"
if errorlevel 1 (
  echo ERROR: creator_evaluation.py v0.3.0 is not active yet.
  pause
  exit /b 4
)

echo.
echo Retrying Matteo Conti using yt-dlp profile discovery first...
call "%~dp016_evaluate_matteo_conti.bat"
set "ERR=%ERRORLEVEL%"
echo.
echo FINAL EXIT CODE: %ERR%
pause
exit /b %ERR%
