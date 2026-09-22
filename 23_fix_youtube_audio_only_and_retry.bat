@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0.."
set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"
set "SCRIPT=%~dp0youtube_creator_evaluation.py"

echo Fixing YouTube creator evaluation to audio-only v0.4.0...
echo Root: %ROOT%
echo.

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

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p='%SCRIPT%'; $s=[IO.File]::ReadAllText($p);" ^
  "if($s -match 'YOUTUBE_EVAL_VERSION = ""0\.4\.0""'){ Write-Host 'Already v0.4.0'; exit 0 };" ^
  "if($s -notmatch 'YOUTUBE_EVAL_VERSION = ""0\.3\.0""'){ Write-Host 'ERROR: Expected v0.3.0 source not found.'; exit 20 };" ^
  "$bak=$p+'.bak_before_v040_'+(Get-Date -Format 'yyyyMMdd_HHmmss'); Copy-Item -LiteralPath $p -Destination $bak -Force;" ^
  "$s=$s.Replace('YOUTUBE_EVAL_VERSION = ""0.3.0""','YOUTUBE_EVAL_VERSION = ""0.4.0""');" ^
  "$s=[regex]::Replace($s,'(?m)^\s{12}if not videos:\r?\n\s{16}return False, ""no_video_stream""\r?\n','');" ^
  "$s=$s.Replace('# Prefer a real audio+video pair; fall back to a combined A/V format.','# Creator evaluation is transcript-first: use one audio-bearing stream; no ffmpeg merge required.');" ^
  "$s=$s.Replace('""--format"", ""bv*+ba/b""','""--format"", ""ba/b[acodec!=none]""');" ^
  "if($s -notmatch 'YOUTUBE_EVAL_VERSION = ""0\.4\.0""' -or $s -notmatch 'ba/b\[acodec!=none\]'){ Write-Host 'ERROR: Patch verification failed.'; exit 21 };" ^
  "[IO.File]::WriteAllText($p,$s,(New-Object Text.UTF8Encoding($false))); Write-Host ('Patched: '+$p); Write-Host ('Backup: '+$bak);"
if errorlevel 1 (
  echo.
  echo PATCH FAILED. Exit code: %ERRORLEVEL%
  pause
  exit /b %ERRORLEVEL%
)

"%PY%" -c "from pathlib import Path; p=Path(r'%SCRIPT%'); s=p.read_text(encoding='utf-8'); compile(s,str(p),'exec'); assert 'YOUTUBE_EVAL_VERSION = \"0.4.0\"' in s; assert 'ba/b[acodec!=none]' in s; print('READ-BACK VERIFIED: YouTube evaluator v0.4.0 audio-only')"
if errorlevel 1 (
  echo.
  echo READ-BACK VERIFICATION FAILED.
  pause
  exit /b 2
)

echo.
echo Running Matteo Conti YouTube creator evaluation...
echo Sample size: 20
echo Instagram access: NO
echo External ffmpeg merge required: NO
echo.

"%PY%" "%SCRIPT%" --root "%ROOT%" --channel-url "https://www.youtube.com/channel/UCQrLxbpidT8aCG3CFbM_YMA/shorts" --creator-key "matfinog" --creator-name "Matteo Conti" --sample-size 20
set "ERR=%ERRORLEVEL%"

echo.
echo FINAL EXIT CODE: %ERR%
echo Status: %ROOT%\state\creator_evaluation_status.json
echo Queue:  %ROOT%\state\research_queue.json
echo.
pause
exit /b %ERR%
