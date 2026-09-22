@echo off
setlocal EnableExtensions

echo CamoFox TikTok POC installer
echo ===========================
echo.
echo This installs a pinned local CamoFox runtime under:
echo %%LOCALAPPDATA%%\InstagramResearch\camofox-poc
echo.
echo Source package: @askjo/camofox-browser@1.13.1
echo Requires Node.js 22 or newer.
echo.

where node >nul 2>nul
if errorlevel 1 (
  echo ERROR: Node.js is not installed or not on PATH.
  echo Install Node.js 22+ first, then rerun this file.
  pause
  exit /b 2
)

for /f "tokens=1 delims=." %%V in ('node -p "process.versions.node"') do set NODEMAJOR=%%V
if %NODEMAJOR% LSS 22 (
  echo ERROR: Node.js %NODEMAJOR% detected. CamoFox requires Node.js 22 or newer.
  node --version
  pause
  exit /b 3
)

where npm >nul 2>nul
if errorlevel 1 (
  echo ERROR: npm is not available on PATH.
  pause
  exit /b 4
)

set "DEST=%LOCALAPPDATA%\InstagramResearch\camofox-poc"
if not exist "%DEST%" mkdir "%DEST%"
cd /d "%DEST%"

if not exist package.json (
  call npm init -y
  if errorlevel 1 goto :fail
)

echo.
echo Installing pinned CamoFox 1.13.1...
call npm install --save-exact @askjo/camofox-browser@1.13.1
if errorlevel 1 goto :fail

if not exist "node_modules\.bin\camofox-browser.cmd" (
  echo ERROR: CamoFox CLI was not installed.
  goto :fail
)

echo.
echo Installed CamoFox:
call "node_modules\.bin\camofox-browser.cmd" --help >nul 2>nul
echo %DEST%\node_modules\.bin\camofox-browser.cmd
echo.
echo Installation complete.
pause
exit /b 0

:fail
echo.
echo INSTALL FAILED with exit code %ERRORLEVEL%.
pause
exit /b 1
