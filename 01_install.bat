@echo off
setlocal
cd /d "%~dp0"

rem InfluencerResearch logical system name; legacy local runtime path is kept for compatibility.
set "RUNTIME=%LOCALAPPDATA%\InstagramResearch"
set "VENV=%RUNTIME%\venv"
set "TIKTOK_LOCK=requirements_tiktok_impersonation.lock.txt"
set "LOGDIR=%~dp0..\logs"
set "LOG=%LOGDIR%\01_install.log"

if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
>> "%LOG%" echo.
>> "%LOG%" echo ============================================================
>> "%LOG%" echo INSTALL_START %DATE% %TIME%
>> "%LOG%" echo script=%~f0
>> "%LOG%" echo runtime=%RUNTIME%
>> "%LOG%" echo venv=%VENV%

echo [1/6] Creating local runtime folder...
>> "%LOG%" echo STEP_1_CREATE_RUNTIME
if not exist "%RUNTIME%" mkdir "%RUNTIME%" >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
if not exist "%RUNTIME%\chrome-profile" mkdir "%RUNTIME%\chrome-profile" >> "%LOG%" 2>&1
if errorlevel 1 goto :fail

echo [2/6] Creating Python virtual environment...
>> "%LOG%" echo STEP_2_CREATE_VENV
where py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: Python launcher "py" was not found.
  >> "%LOG%" echo ERROR_PY_LAUNCHER_NOT_FOUND
  pause
  exit /b 2
)

if not exist "%VENV%\Scripts\python.exe" (
  py -3 -m venv "%VENV%" >> "%LOG%" 2>&1
  if errorlevel 1 goto :fail
)

echo [3/6] Verifying reviewed Python ABI for BACKLOG_042...
>> "%LOG%" echo STEP_3_VERIFY_ABI
"%VENV%\Scripts\python.exe" -c "import platform,struct,sys; ok=(sys.version_info[:2]==(3,12) and struct.calcsize('P')*8==64 and platform.machine().upper()=='AMD64'); print('Python:',sys.version.split()[0],platform.machine(),str(struct.calcsize('P')*8)+'-bit'); raise SystemExit(0 if ok else 3)" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: BACKLOG_042 reviewed wheel set requires CPython 3.12 x64 AMD64.
  >> "%LOG%" echo ERROR_ABI_MISMATCH
  goto :fail
)

if not exist "%TIKTOK_LOCK%" (
  echo ERROR: Missing %TIKTOK_LOCK%.
  >> "%LOG%" echo ERROR_LOCKFILE_MISSING file=%TIKTOK_LOCK%
  goto :fail
)

echo [4/6] Installing/updating base dependencies...
>> "%LOG%" echo STEP_4_INSTALL_BASE
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 goto :fail

echo [5/6] Installing reviewed TikTok impersonation dependency set...
>> "%LOG%" echo STEP_5_INSTALL_TIKTOK_LOCK
rem Fail closed: wheels only, no dependency resolution, exact hashes, exact reviewed artifacts.
"%VENV%\Scripts\python.exe" -m pip install --only-binary=:all: --no-deps --require-hashes --force-reinstall -r "%TIKTOK_LOCK%" >> "%LOG%" 2>&1
if errorlevel 1 goto :fail

rem Read back exact security-reviewed versions before continuing.
"%VENV%\Scripts\python.exe" -c "from importlib import metadata; exp={'yt-dlp':'2026.8.19','curl_cffi':'0.16.2','cffi':'2.1.1','pycparser':'3.0','certifi':'2026.7.22'}; got={k:metadata.version(k) for k in exp}; print('Reviewed dependency read-back:',got); raise SystemExit(0 if got==exp else 4)" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: BACKLOG_042 dependency read-back did not match the reviewed set.
  >> "%LOG%" echo ERROR_DEPENDENCY_READBACK_MISMATCH
  goto :fail
)

echo [6/6] Verifying Playwright + Whisper + bundled ffmpeg imports...
>> "%LOG%" echo STEP_6_VERIFY_IMPORTS
"%VENV%\Scripts\python.exe" -c "from playwright.sync_api import sync_playwright; import faster_whisper, imageio_ffmpeg; print('OK: dependencies import successfully'); print('Bundled ffmpeg:', imageio_ffmpeg.get_ffmpeg_exe())" >> "%LOG%" 2>&1
if errorlevel 1 goto :fail

>> "%LOG%" echo INSTALL_PASS %DATE% %TIME%
echo.
echo INSTALLATION OK.
echo Log:
echo %LOG%
echo Existing authentication/session state is preserved.
pause
exit /b 0

:fail
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" set "RC=1"
>> "%LOG%" echo INSTALL_FAIL rc=%RC% %DATE% %TIME%
echo.
echo INSTALLATION FAILED. No success state should be assumed.
echo Log:
echo %LOG%
pause
exit /b %RC%
