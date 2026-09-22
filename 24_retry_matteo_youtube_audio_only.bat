@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0.."
set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"
set "SCRIPT=%~dp0youtube_creator_evaluation.py"

if not exist "%PY%" (
  echo ERROR: Python environment not found:
  echo %PY%
  pause
  exit /b 2
)

if not exist "%SCRIPT%" (
  echo ERROR: Missing:
  echo %SCRIPT%
  pause
  exit /b 2
)

echo Matteo Conti YouTube creator evaluation - audio-only v0.4.0
echo Sample size: 20
echo Instagram access: NO
echo.

"%PY%" -c "import importlib.util; p=r'%SCRIPT%'; spec=importlib.util.spec_from_file_location('yce',p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print('COMPONENT VERSION:', m.YOUTUBE_EVAL_VERSION); assert m.YOUTUBE_EVAL_VERSION == '0.4.0'"
if errorlevel 1 (
  echo ERROR: Expected youtube_creator_evaluation.py v0.4.0
  pause
  exit /b 2
)

"%PY%" "%SCRIPT%" --root "%ROOT%" --channel-url "https://www.youtube.com/channel/UCQrLxbpidT8aCG3CFbM_YMA/shorts" --creator-key "matfinog" --creator-name "Matteo Conti" --sample-size 20
set "ERR=%ERRORLEVEL%"

echo.
echo FINAL EXIT CODE: %ERR%
echo Status: %ROOT%\state\creator_evaluation_status.json
echo Queue:  %ROOT%\state\research_queue.json
echo.
pause
exit /b %ERR%
