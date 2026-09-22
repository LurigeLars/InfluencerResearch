@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\InstagramResearch\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo ERROR: InstagramResearch Python environment not found.
  echo Expected: %PY%
  echo Run 01_install.bat first.
  pause
  exit /b 2
)

if not exist "tiktok_camofox_sync.py" (
  echo ERROR: Put this installer in InstagramResearch\app\ before running it.
  pause
  exit /b 3
)
if not exist "research_queue.py" (
  echo ERROR: research_queue.py not found beside installer.
  pause
  exit /b 4
)

set "INSTALLER_SELF=%~f0"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%I"

echo Installing Creator Evaluation v0.1.0...
echo Root: %~dp0..
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$raw=[IO.File]::ReadAllText($env:INSTALLER_SELF);" ^
  "$files=@('tiktok_camofox_sync.py.new','research_queue.py.new','creator_evaluation.py.new','15_evaluate_creator.bat.new','16_evaluate_josh_oldmixon.bat.new');" ^
  "$names=@('tiktok_camofox_sync.py','research_queue.py','creator_evaluation.py','15_evaluate_creator.bat','16_evaluate_josh_oldmixon.bat');" ^
  "for($i=0;$i -lt $files.Count;$i++){ $name=$names[$i]; $pattern='(?ms)^###BEGIN_FILE:'+[regex]::Escape($name)+'\r?\n(.*?)^###END_FILE:'+[regex]::Escape($name)+'\s*$'; $m=[regex]::Match($raw,$pattern); if(-not $m.Success){ throw 'Embedded payload missing: '+$name }; [IO.File]::WriteAllText((Join-Path (Get-Location) $files[$i]),$m.Groups[1].Value,(New-Object Text.UTF8Encoding($false))) }"
if errorlevel 1 (
  echo ERROR: Could not extract embedded files.
  pause
  exit /b 5
)

"%PY%" -c "import py_compile; [py_compile.compile(p,doraise=True) for p in ['tiktok_camofox_sync.py.new','research_queue.py.new','creator_evaluation.py.new']]"
if errorlevel 1 (
  echo ERROR: Python validation failed. Existing files were NOT replaced.
  del /q *.new 2>nul
  pause
  exit /b 6
)

copy /y "tiktok_camofox_sync.py" "tiktok_camofox_sync.py.bak_creator_eval_%STAMP%" >nul
if errorlevel 1 goto :backup_fail
copy /y "research_queue.py" "research_queue.py.bak_creator_eval_%STAMP%" >nul
if errorlevel 1 goto :backup_fail

move /y "tiktok_camofox_sync.py.new" "tiktok_camofox_sync.py" >nul
move /y "research_queue.py.new" "research_queue.py" >nul
move /y "creator_evaluation.py.new" "creator_evaluation.py" >nul
move /y "15_evaluate_creator.bat.new" "15_evaluate_creator.bat" >nul
move /y "16_evaluate_josh_oldmixon.bat.new" "16_evaluate_josh_oldmixon.bat" >nul

"%PY%" -c "import sys; sys.path.insert(0,'.'); import tiktok_camofox_sync as t, research_queue as q, creator_evaluation as e; assert t.APP_VERSION=='0.8.0'; assert q.SCREEN_VERSION=='0.3.0'; assert e.EVAL_VERSION=='0.1.0'; print('Verified:',t.APP_VERSION,q.SCREEN_VERSION,e.EVAL_VERSION)"
if errorlevel 1 (
  echo ERROR: Read-back verification failed.
  echo Backups are preserved with timestamp %STAMP%.
  pause
  exit /b 7
)

echo.
echo INSTALL VERIFIED.
echo Created:
echo   %~dp015_evaluate_creator.bat
echo   %~dp016_evaluate_josh_oldmixon.bat
echo.
echo No creator was added to control\tiktok_sources.json.
echo No permanent scanner source was changed.
echo.
echo Next: run 16_evaluate_josh_oldmixon.bat
pause
exit /b 0

:backup_fail
echo ERROR: Backup failed. Existing files were NOT intentionally replaced.
del /q *.new 2>nul
pause
exit /b 8

rem Embedded payloads below. Do not edit manually.
goto :eof

###BEGIN_FILE:tiktok_camofox_sync.py
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_VERSION = "0.8.0"
BASE_URL = "http://127.0.0.1:9377"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Any = None) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if default is not None:
        return default
    raise FileNotFoundError(path)


def request_json(method: str, path: str, body: dict | None = None, timeout: int = 30) -> Any:
    url = BASE_URL + path
    payload = None
    headers = {"Accept": "application/json"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype or raw[:1] in (b"{", b"["):
                return json.loads(raw.decode("utf-8", errors="replace"))
            return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {detail[:2000]}") from exc


def health() -> dict | None:
    try:
        value = request_json("GET", "/health", timeout=3)
        return value if isinstance(value, dict) else {"raw": value}
    except Exception:
        return None


def start_server() -> dict:
    h = health()
    if h:
        return {"started": False, "health": h, "note": "already_running"}

    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch" / "camofox-poc"
    cli = local / "node_modules" / ".bin" / "camofox-browser.cmd"
    if not cli.exists():
        raise RuntimeError(
            f"CamoFox CLI not installed at {cli}. Run app\\09_install_camofox_poc.bat first."
        )

    log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch" / "camofox-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "camofox_server.log"
    log = open(log_path, "ab", buffering=0)

    env = os.environ.copy()
    env["CAMOFOX_PORT"] = "9377"
    env["CAMOFOX_CRASH_REPORT_ENABLED"] = "false"
    env["CAMOFOX_PROFILE_DIR"] = str(
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch" / "camofox-profiles"
    )

    subprocess.Popen(
        [str(cli)],
        cwd=str(local),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    for _ in range(60):
        time.sleep(0.5)
        h = health()
        if h:
            return {"started": True, "health": h, "note": str(log_path)}
    raise RuntimeError(f"CamoFox server failed to become healthy. Check {log_path}")


def flatten_video_links(obj: Any, handle: str) -> list[str]:
    pattern = re.compile(
        rf'https?://(?:www\.)?tiktok\.com/@{re.escape(handle)}/video/\d+',
        re.IGNORECASE,
    )
    out: list[str] = []
    if isinstance(obj, str):
        out.extend(pattern.findall(obj.replace("\\/", "/")))
    elif isinstance(obj, dict):
        for value in obj.values():
            out.extend(flatten_video_links(value, handle))
    elif isinstance(obj, list):
        for value in obj:
            out.extend(flatten_video_links(value, handle))
    return out


def collect_video_urls(
    tab_id: str,
    *,
    user_id: str,
    handle: str,
    target: int,
    max_scrolls: int = 120,
) -> tuple[list[str], dict]:
    found: list[str] = []
    seen: set[str] = set()
    stagnant = 0
    rounds = 0
    links_endpoint_errors = 0

    for round_idx in range(max_scrolls + 1):
        rounds += 1
        before = len(found)

        snap = request_json(
            "GET",
            f"/tabs/{urllib.parse.quote(tab_id)}/snapshot?"
            + urllib.parse.urlencode({"userId": user_id, "format": "text"}),
            timeout=30,
        )
        candidates = flatten_video_links(snap, handle)

        try:
            links = request_json(
                "GET",
                f"/tabs/{urllib.parse.quote(tab_id)}/links?"
                + urllib.parse.urlencode({"userId": user_id, "limit": 250}),
                timeout=20,
            )
            candidates.extend(flatten_video_links(links, handle))
        except Exception:
            links_endpoint_errors += 1

        for url in candidates:
            clean = url.split("?")[0].rstrip("/")
            if clean not in seen:
                seen.add(clean)
                found.append(clean)

        if len(found) >= target:
            break

        if len(found) == before:
            stagnant += 1
        else:
            stagnant = 0

        if stagnant >= 5:
            break

        request_json(
            "POST",
            f"/tabs/{urllib.parse.quote(tab_id)}/scroll",
            {"userId": user_id, "direction": "down", "amount": 1200},
            timeout=20,
        )
        time.sleep(1.0)

    return found[:target], {
        "target": target,
        "found": len(found[:target]),
        "rounds": rounds,
        "stagnant_rounds_at_end": stagnant,
        "links_endpoint_errors": links_endpoint_errors,
    }


def video_id_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def validate_media(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    size = path.stat().st_size
    if size < 50_000:
        return False, f"too_small:{size}"
    try:
        import av
        with av.open(str(path)) as container:
            videos = [s for s in container.streams if s.type == "video"]
            if not videos:
                return False, "no_video_stream"
            return True, f"ok:size={size}:duration={container.duration}"
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc}"


def published_iso(info: dict) -> str | None:
    ts = info.get("timestamp")
    if ts is not None:
        try:
            return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
        except Exception:
            pass
    upload_date = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", upload_date):
        try:
            return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return None


def adopt_poc_file(root: Path, video_id: str, video_dir: Path) -> tuple[Path | None, Path | None]:
    poc_dir = root / "output" / "nicholas_crown" / "tiktok" / "camofox_individual"
    src_mp4 = poc_dir / f"{video_id}.mp4"
    src_info = poc_dir / f"{video_id}.info.json"
    if not src_mp4.exists():
        return None, None

    video_dir.mkdir(parents=True, exist_ok=True)
    dst_mp4 = video_dir / src_mp4.name
    dst_info = video_dir / src_info.name

    if not dst_mp4.exists():
        shutil.copy2(src_mp4, dst_mp4)
    if src_info.exists() and not dst_info.exists():
        shutil.copy2(src_info, dst_info)
    return dst_mp4, dst_info if dst_info.exists() else None


def download_one(url: str, video_dir: Path) -> dict:
    vid = video_id_from_url(url)
    video_dir.mkdir(parents=True, exist_ok=True)
    mp4 = video_dir / f"{vid}.mp4"
    info_path = video_dir / f"{vid}.info.json"

    if mp4.exists():
        valid, validation = validate_media(mp4)
        return {
            "video_id": vid,
            "url": url,
            "ok": valid,
            "source": "existing_file",
            "media_file": mp4,
            "info_file": info_path if info_path.exists() else None,
            "validation": validation,
            "returncode": 0 if valid else 1,
            "diagnostic_tail": "",
        }

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",
        "--no-progress",
        "--write-info-json",
        "--no-overwrites",
        "--format", "b[ext=mp4]/b",
        "--output", str(video_dir / "%(id)s.%(ext)s"),
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        valid, validation = validate_media(mp4)
        detail = (result.stderr or result.stdout or "").strip()
        return {
            "video_id": vid,
            "url": url,
            "ok": result.returncode == 0 and valid,
            "source": "network",
            "media_file": mp4 if mp4.exists() else None,
            "info_file": info_path if info_path.exists() else None,
            "validation": validation,
            "returncode": result.returncode,
            "diagnostic_tail": detail[-2500:],
        }
    except subprocess.TimeoutExpired as exc:
        # Fail this one video closed, but do NOT abort the creator sync.
        # yt-dlp may leave a .part file; keeping it allows a later run to resume.
        valid, validation = validate_media(mp4)
        detail = ""
        if exc.stderr:
            detail = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr)
        elif exc.stdout:
            detail = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout)

        return {
            "video_id": vid,
            "url": url,
            "ok": bool(valid),
            "source": "network_timeout",
            "media_file": mp4 if mp4.exists() else None,
            "info_file": info_path if info_path.exists() else None,
            "validation": validation,
            "returncode": 124,
            "diagnostic_tail": (
                f"yt-dlp timed out after 180 seconds. "
                f"Partial files are retained for resume. {detail}"
            )[-2500:],
        }


def transcribe(
    root: Path,
    creator_key: str,
    media_path: Path,
    *,
    model_holder: dict,
) -> dict:
    transcript_dir = root / "output" / creator_key / "tiktok" / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    txt_path = transcript_dir / f"{media_path.stem}.txt"
    json_path = transcript_dir / f"{media_path.stem}.json"

    if txt_path.exists() and json_path.exists():
        return {
            "ok": True,
            "source": "existing_transcript",
            "txt": txt_path,
            "json": json_path,
            "transcribed_at": datetime.fromtimestamp(
                txt_path.stat().st_mtime, timezone.utc
            ).isoformat(),
        }

    valid, validation = validate_media(media_path)
    if not valid:
        return {"ok": False, "error": f"invalid_media:{validation}"}

    if model_holder.get("model") is None:
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel
        model_holder["model"] = WhisperModel("small", device="cpu", compute_type="int8")

    model = model_holder["model"]
    segments, info = model.transcribe(
        str(media_path),
        beam_size=5,
        vad_filter=True,
    )

    rows = []
    text_parts = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            text_parts.append(text)
        rows.append({
            "start": round(float(seg.start), 3),
            "end": round(float(seg.end), 3),
            "text": text,
        })

    when = utc_now()
    txt_path.write_text(" ".join(text_parts).strip() + "\n", encoding="utf-8")
    atomic_json(json_path, {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "source_platform": "TIKTOK",
        "creator": creator_key,
        "video_id": media_path.stem,
        "generated_at": when,
        "language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "duration": getattr(info, "duration", None),
        "segments": rows,
    })
    return {
        "ok": True,
        "source": "whisper",
        "txt": txt_path,
        "json": json_path,
        "transcribed_at": when,
    }


def merge_catalog(
    catalog: dict,
    discovered: list[str],
    *,
    profile_url: str,
) -> dict:
    items = catalog.get("items", {})
    now = utc_now()
    for rank, url in enumerate(discovered):
        vid = video_id_from_url(url)
        old = items.get(vid, {})
        items[vid] = {
            "video_id": vid,
            "url": url,
            "profile_url": profile_url,
            "first_seen_at": old.get("first_seen_at") or now,
            "last_seen_at": now,
            "last_seen_rank": rank,
        }

    ordered_ids = [video_id_from_url(u) for u in discovered]
    for vid in catalog.get("order", []):
        if vid not in ordered_ids and vid in items:
            ordered_ids.append(vid)

    return {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "updated_at": now,
        "profile_url": profile_url,
        "order": ordered_ids,
        "items": items,
    }


def update_main_manifest(
    root: Path,
    *,
    creator_key: str,
    url: str,
    download: dict,
    transcription: dict,
) -> dict:
    manifest_path = root / "state" / "manifest.json"
    manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
    manifest.setdefault("items", {})

    vid = download["video_id"]
    info = {}
    info_file = download.get("info_file")
    if info_file and Path(info_file).exists():
        try:
            info = json.loads(Path(info_file).read_text(encoding="utf-8"))
        except Exception:
            info = {}

    media_path = Path(download["media_file"])
    txt_path = Path(transcription["txt"])
    json_path = Path(transcription["json"])
    key = f"tt_{vid}"

    old = manifest["items"].get(key, {})
    caption = str(info.get("description") or info.get("title") or "").strip()

    manifest["items"][key] = {
        **old,
        "schema_version": 1,
        "source_platform": "TIKTOK",
        "source_type": "VIDEO",
        "source_id": vid,
        "shortcode": key,
        "creator": creator_key,
        "url": url,
        "caption": caption,
        "published_at": published_iso(info),
        "downloaded_at": old.get("downloaded_at") or utc_now(),
        "download_status": "DONE",
        "media_file": str(media_path.relative_to(root)),
        "media_validation": download.get("validation"),
        "transcribed_at": transcription["transcribed_at"],
        "transcription_status": "DONE",
        "transcript_txt": str(txt_path.relative_to(root)),
        "transcript_json": str(json_path.relative_to(root)),
        "research_status": old.get("research_status") or "PENDING",
        "source_class": "INFLUENCER_DISCOVERY_SECONDARY",
    }
    atomic_json(manifest_path, manifest)
    return manifest["items"][key]


def run_research_queue(root: Path) -> dict:
    script = root / "app" / "research_queue.py"
    if not script.exists():
        return {"ok": False, "error": f"missing {script}"}
    result = subprocess.run(
        [sys.executable, str(script), "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout_tail": (result.stdout or "")[-3000:],
        "stderr_tail": (result.stderr or "")[-3000:],
    }


def process_source(root: Path, source: dict, *, max_new_override: int | None = None) -> dict:
    creator_key = str(source["creator_key"])
    handle = str(source["handle"]).lstrip("@")
    profile_url = str(source.get("profile_url") or f"https://www.tiktok.com/@{handle}")
    discovery_step = int(source.get("discovery_step", 200))
    max_catalog = int(source.get("max_catalog", 2500))
    max_new_downloads = int(
        max_new_override if max_new_override is not None
        else source.get("max_new_downloads", 20)
    )

    state_dir = root / "state" / "tiktok"
    catalog_path = state_dir / f"{creator_key}_catalog.json"
    catalog = load_json(
        catalog_path,
        {
            "schema_version": 1,
            "app_version": APP_VERSION,
            "profile_url": profile_url,
            "order": [],
            "items": {},
        },
    )

    # Seed only the original Nicholas Crown source from the successful v0.6 POC.
    # Generic/evaluation creators must never inherit another creator's discovered URLs.
    if creator_key == "nicholascrown":
        poc_urls = load_json(
            state_dir / "camofox_discovered_urls.json",
            {"urls": []},
        ).get("urls", [])
        if poc_urls:
            catalog = merge_catalog(catalog, [str(x) for x in poc_urls], profile_url=profile_url)

    previous_count = len(catalog.get("items", {}))
    discovery_target = min(max_catalog, max(discovery_step, previous_count + discovery_step))

    user_id = f"instagramresearch-tiktok-{creator_key}"
    session_key = f"{creator_key}-feed"
    tab_id = None

    try:
        tab = request_json("POST", "/tabs", {
            "userId": user_id,
            "sessionKey": session_key,
            "url": profile_url,
            "trace": False,
        }, timeout=60)
        if not isinstance(tab, dict) or not tab.get("tabId"):
            raise RuntimeError(f"Unexpected CamoFox create-tab response: {tab}")
        tab_id = str(tab["tabId"])
        time.sleep(3)

        discovered, discovery_diag = collect_video_urls(
            tab_id,
            user_id=user_id,
            handle=handle,
            target=discovery_target,
        )
        catalog = merge_catalog(catalog, discovered, profile_url=profile_url)
        atomic_json(catalog_path, catalog)
    finally:
        if tab_id:
            try:
                request_json(
                    "DELETE",
                    f"/tabs/{urllib.parse.quote(tab_id)}?"
                    + urllib.parse.urlencode({"userId": user_id}),
                    timeout=10,
                )
            except Exception:
                pass

    main_manifest = load_json(root / "state" / "manifest.json", {"schema_version": 1, "items": {}})
    main_items = main_manifest.get("items", {})

    ordered_urls = [
        catalog["items"][vid]["url"]
        for vid in catalog.get("order", [])
        if vid in catalog.get("items", {})
    ]
    skipped_known = sum(
        1 for url in ordered_urls
        if f"tt_{video_id_from_url(url)}" in main_items
        and main_items[f"tt_{video_id_from_url(url)}"].get("download_status") == "DONE"
        and main_items[f"tt_{video_id_from_url(url)}"].get("transcription_status") == "DONE"
    )
    candidates = [
        url for url in ordered_urls
        if not (
            f"tt_{video_id_from_url(url)}" in main_items
            and main_items[f"tt_{video_id_from_url(url)}"].get("download_status") == "DONE"
            and main_items[f"tt_{video_id_from_url(url)}"].get("transcription_status") == "DONE"
        )
    ][:max_new_downloads]

    video_dir = root / "output" / creator_key / "tiktok" / "videos"
    model_holder: dict[str, Any] = {"model": None}
    completed = []
    failures = []
    reused_poc = 0
    downloaded_network = 0

    for url in candidates:
        vid = video_id_from_url(url)

        adopted_mp4, adopted_info = adopt_poc_file(root, vid, video_dir)
        if adopted_mp4:
            valid, validation = validate_media(adopted_mp4)
            download = {
                "video_id": vid,
                "url": url,
                "ok": valid,
                "source": "poc_reuse",
                "media_file": adopted_mp4,
                "info_file": adopted_info,
                "validation": validation,
                "returncode": 0 if valid else 1,
                "diagnostic_tail": "",
            }
            reused_poc += 1
        else:
            download = download_one(url, video_dir)
            if download.get("source") == "network" and download.get("ok"):
                downloaded_network += 1

        if not download.get("ok") or not download.get("media_file"):
            failures.append({
                "video_id": vid,
                "url": url,
                "stage": "download",
                "detail": download.get("diagnostic_tail") or download.get("validation"),
            })
            continue

        try:
            transcription = transcribe(
                root,
                creator_key,
                Path(download["media_file"]),
                model_holder=model_holder,
            )
        except Exception as exc:
            failures.append({
                "video_id": vid,
                "url": url,
                "stage": "transcription",
                "detail": f"{type(exc).__name__}: {exc}",
            })
            continue

        if not transcription.get("ok"):
            failures.append({
                "video_id": vid,
                "url": url,
                "stage": "transcription",
                "detail": transcription.get("error"),
            })
            continue

        record = update_main_manifest(
            root,
            creator_key=creator_key,
            url=url,
            download=download,
            transcription=transcription,
        )
        completed.append({
            "video_id": vid,
            "url": url,
            "download_source": download.get("source"),
            "media_file": record["media_file"],
            "transcript_txt": record["transcript_txt"],
        })

    return {
        "creator_key": creator_key,
        "handle": handle,
        "profile_url": profile_url,
        "catalog_before": previous_count,
        "discovery_target": discovery_target,
        "catalog_after": len(catalog.get("items", {})),
        "discovery": discovery_diag,
        "skipped_known": skipped_known,
        "candidate_new": len(candidates),
        "completed_new": len(completed),
        "reused_poc_files": reused_poc,
        "downloaded_network": downloaded_network,
        "failures": failures,
        "completed": completed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--max-new", type=int, default=None)
    args = parser.parse_args()
    root = args.root.resolve()

    config_path = root / "control" / "tiktok_sources.json"
    config = load_json(config_path)
    sources = [s for s in config.get("sources", []) if s.get("enabled")]
    if not sources:
        raise RuntimeError("No enabled TikTok sources in control/tiktok_sources.json")

    status_path = root / "state" / "tiktok" / "sync_status.json"
    started = utc_now()
    atomic_json(status_path, {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "state": "RUNNING",
        "started_at": started,
    })

    server = start_server()
    results = []
    top_errors = []

    for source in sources:
        try:
            results.append(
                process_source(root, source, max_new_override=args.max_new)
            )
        except Exception as exc:
            top_errors.append({
                "creator_key": source.get("creator_key"),
                "error": f"{type(exc).__name__}: {exc}",
            })

    queue = run_research_queue(root)

    failures = sum(len(r.get("failures", [])) for r in results) + len(top_errors)
    if not queue.get("ok"):
        failures += 1

    status = {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "state": "DONE" if failures == 0 else "DONE_WITH_ERRORS",
        "started_at": started,
        "finished_at": utc_now(),
        "server": server,
        "results": results,
        "research_queue": queue,
        "top_errors": top_errors,
        "total_errors": failures,
    }
    atomic_json(status_path, status)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
###END_FILE:tiktok_camofox_sync.py

###BEGIN_FILE:research_queue.py
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


SCREEN_VERSION = "0.3.0"
ANALYSIS_OWNER = "EKONOMI"
FINAL_DECISIONS = {
    "IGNORE",
    "RESEARCH",
    "TEST_CANDIDATE",
    "BACKLOG_CANDIDATE",
    # Backward compatibility for decisions created before HANDOFF-001.
    "TEST",
    "BACKLOG",
}
CURRENT_DECISIONS = ["IGNORE", "RESEARCH", "TEST_CANDIDATE", "BACKLOG_CANDIDATE"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any = None) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if default is not None:
        return default
    raise FileNotFoundError(path)


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def normalize_manifest_path(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    return root / Path(value)


def canonicalize_url(value: str | None) -> str:
    if not value:
        return ""
    try:
        parts = urlsplit(str(value).strip())
        host = parts.netloc.lower()
        path = re.sub(r"/+$", "", parts.path)
        return urlunsplit((parts.scheme.lower(), host, path, "", ""))
    except Exception:
        return str(value).strip().rstrip("/")


def transcript_fingerprint(text: str) -> str | None:
    # Deliberately strict: only near-verbatim reposts should be auto-deduped.
    normalized = re.sub(r"[^\w]+", " ", text.casefold(), flags=re.UNICODE)
    normalized = " ".join(normalized.split())
    if len(normalized.split()) < 40:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def lineage_id(*, transcript_fp: str | None, source_url: str, shortcode: str) -> str:
    if transcript_fp:
        seed = f"transcript:{transcript_fp}"
    elif source_url:
        seed = f"url:{source_url}"
    else:
        seed = f"id:{shortcode}"
    return "EL-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def keyword_tags(text: str) -> list[str]:
    """Low-stakes discovery tags only. These never create a research decision."""
    lower = text.lower()
    groups = {
        "market_structure": [
            "liquidity", "order flow", "market maker", "dealer", "gamma",
            "volatility", "vwap", "volume", "stop run", "liquidation",
        ],
        "macro": [
            "fed", "federal reserve", "inflation", "rates", "yield",
            "treasury", "dollar", "macro", "recession", "cpi",
        ],
        "systematic_quant": [
            "backtest", "factor", "signal", "systematic", "quant",
            "algorithm", "model", "regime", "portfolio construction",
        ],
        "tools_data": [
            "api", "terminal", "platform", "data", "scanner", "tradingview",
            "bloomberg", "openbb", "python", "mcp",
        ],
        "trade_setup": [
            "entry", "stop", "target", "long", "short", "setup",
            "breakout", "reversal", "risk reward",
        ],
        "promotion": [
            "comment", "link in bio", "newsletter", "letter", "course",
            "subscribe", "free guide", "dm me",
        ],
    }
    tags = []
    for tag, words in groups.items():
        if any(word in lower for word in words):
            tags.append(tag)
    return tags


def build_packet(
    root: Path,
    shortcode: str,
    item: dict,
    transcript_path: Path,
    *,
    evidence_lineage_id: str,
    duplicate_of: str | None,
    duplicate_basis: str | None,
) -> dict:
    transcript = read_text(transcript_path)
    caption = str(item.get("caption") or "").strip()

    return {
        "schema_version": 2,
        "screen_version": SCREEN_VERSION,
        "queue_id": shortcode,
        "shortcode": shortcode,
        "creator": item.get("creator"),
        "source_platform": item.get("source_platform"),
        "source_id": item.get("source_id"),
        "source_url": item.get("url"),
        "published_at": item.get("published_at"),
        "downloaded_at": item.get("downloaded_at"),
        "transcribed_at": item.get("transcribed_at"),
        "source_class": "INFLUENCER_DISCOVERY_SECONDARY",
        "evaluation_mode": item.get("evaluation_mode"),
        "evaluation_run_id": item.get("evaluation_run_id"),
        "evaluation_source_profile": item.get("evaluation_source_profile"),
        "evaluation_sample_size": item.get("evaluation_sample_size"),
        "permanent_source": item.get("permanent_source"),
        "creator_verification": item.get("creator_verification"),
        "analysis_owner": ANALYSIS_OWNER,
        "analysis_status": "PENDING_ANALYSIS",
        "evidence_lineage_id": evidence_lineage_id,
        "duplicate_of": duplicate_of,
        "duplicate_basis": duplicate_basis,
        "caption": caption,
        "transcript_file": str(transcript_path.relative_to(root)),
        "transcript_text": transcript,
        "word_count": len(transcript.split()),
        "discovery_tags": keyword_tags(caption + "\n" + transcript),
        # Kept as "ai_instruction" for backward compatibility with existing consumers.
        "ai_instruction": {
            "owner_project": ANALYSIS_OWNER,
            "objective": (
                "Treat the influencer as a discovery source, not authority. "
                "Extract the actual claim/idea and decide whether it deserves "
                "IGNORE, RESEARCH, TEST_CANDIDATE, or BACKLOG_CANDIDATE. "
                "Do not create a trade from the video alone. "
                "TEST/BACKLOG are only candidates until Avanza MCP performs "
                "technical peer review through a separate HANDOFF-XXX."
            ),
            "required_decision": CURRENT_DECISIONS,
            "required_fields": [
                "decision",
                "idea_type",
                "claim_summary",
                "claims",
                "existing_system_overlap",
                "verification_plan",
                "falsifiable_test",
                "main_risk",
                "confidence",
                "rationale",
                "evidence_lineage_id",
                "duplicate_of",
                "duplicate_basis",
            ],
            "idea_types": [
                "TRADE_IDEA",
                "MARKET_STRUCTURE",
                "MACRO",
                "DATA_TOOL",
                "SYSTEM_AUTOMATION",
                "QUANT_METHOD",
                "PORTFOLIO_RISK",
                "EDUCATION",
                "PROMOTIONAL",
                "OTHER",
            ],
            "governance": [
                "Influencer content is discovery only.",
                "Material factual claims require primary/original-source verification by Ekonomi.",
                "News or narrative alone cannot create a trade.",
                "Ekonomi owns economic interpretation and classification.",
                "Avanza MCP owns ingestion, provenance, deterministic dedupe, schema/validation and technical overlap review.",
                "Cross-platform reposts are one evidence lineage, not independent confirmations.",
                "If semantic duplication is plausible but not deterministically provable, use duplicate_basis=POSSIBLE_SEMANTIC_DUPLICATE and let Ekonomi decide.",
                "A TEST_CANDIDATE or BACKLOG_CANDIDATE does not change system state; material implementation requires a new HANDOFF-XXX.",
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    args = parser.parse_args()
    root = args.root.resolve()

    manifest_path = root / "state" / "manifest.json"
    queue_path = root / "state" / "research_queue.json"
    decisions_path = root / "state" / "research_decisions.json"
    config_path = root / "control" / "research_screening.json"

    manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
    config = load_json(config_path, {})
    decisions = load_json(
        decisions_path,
        {"schema_version": 2, "screen_version": SCREEN_VERSION, "items": {}},
    )

    max_items = int(config.get("max_queue_items", 100))
    creators_allow = {
        str(x).strip().lstrip("@").lower()
        for x in config.get("creators", [])
        if str(x).strip()
    }

    # Collect every valid transcribed item first so deterministic repost clusters
    # can be resolved before anything is queued.
    records: list[dict] = []
    skipped_missing_transcript = 0
    for shortcode, item in manifest.get("items", {}).items():
        if item.get("download_status") != "DONE":
            continue
        if item.get("transcription_status") != "DONE":
            continue
        creator = str(item.get("creator") or "").lower()
        is_creator_evaluation = (
            item.get("evaluation_mode") == "CREATOR_EVALUATION"
            and item.get("permanent_source") is False
        )
        if creators_allow and creator not in creators_allow and not is_creator_evaluation:
            continue

        transcript_path = normalize_manifest_path(root, item.get("transcript_txt"))
        if transcript_path is None or not transcript_path.exists():
            skipped_missing_transcript += 1
            continue

        transcript = read_text(transcript_path)
        fp = transcript_fingerprint(transcript)
        url_key = canonicalize_url(item.get("url"))
        records.append({
            "shortcode": shortcode,
            "item": item,
            "transcript_path": transcript_path,
            "transcript_fp": fp,
            "url_key": url_key,
            "lineage_id": lineage_id(
                transcript_fp=fp,
                source_url=url_key,
                shortcode=shortcode,
            ),
        })

    # Exact/near-verbatim deterministic dedupe only. Same normalized transcript
    # receives the same lineage ID. Same exact canonical source URL is also grouped.
    groups: dict[str, list[dict]] = {}
    for rec in records:
        if rec["transcript_fp"]:
            key = f"transcript:{rec['transcript_fp']}"
        elif rec["url_key"]:
            key = f"url:{rec['url_key']}"
        else:
            key = f"id:{rec['shortcode']}"
        groups.setdefault(key, []).append(rec)

    duplicate_meta: dict[str, tuple[str, str | None, str | None]] = {}
    for group in groups.values():
        def canonical_sort(rec: dict) -> tuple:
            d = decisions.get("items", {}).get(rec["shortcode"], {})
            finalized = d.get("decision") in FINAL_DECISIONS
            return (
                0 if finalized else 1,
                str(rec["item"].get("published_at") or "9999"),
                str(rec["shortcode"]),
            )

        ordered = sorted(group, key=canonical_sort)
        canonical = ordered[0]
        group_lineage = canonical["lineage_id"]
        for idx, rec in enumerate(ordered):
            if idx == 0:
                duplicate_meta[rec["shortcode"]] = (group_lineage, None, None)
            else:
                basis = (
                    "TRANSCRIPT_MATCH"
                    if rec["transcript_fp"] and rec["transcript_fp"] == canonical["transcript_fp"]
                    else "EXACT_SOURCE_URL"
                )
                duplicate_meta[rec["shortcode"]] = (
                    group_lineage,
                    canonical["shortcode"],
                    basis,
                )

    items = []
    skipped_finalized = 0
    skipped_duplicates = 0
    manifest_changed = False

    for rec in records:
        shortcode = rec["shortcode"]
        item = rec["item"]
        existing_decision = decisions.get("items", {}).get(shortcode, {})

        lineage, duplicate_of, duplicate_basis = duplicate_meta[shortcode]

        # Preserve explicit human/Ekonomi duplicate metadata if it already exists.
        lineage = existing_decision.get("evidence_lineage_id") or item.get("evidence_lineage_id") or lineage
        duplicate_of = existing_decision.get("duplicate_of") or item.get("duplicate_of") or duplicate_of
        duplicate_basis = existing_decision.get("duplicate_basis") or item.get("duplicate_basis") or duplicate_basis

        desired_meta = {
            "evidence_lineage_id": lineage,
            "duplicate_of": duplicate_of,
            "duplicate_basis": duplicate_basis,
        }
        for key, value in desired_meta.items():
            if item.get(key) != value:
                item[key] = value
                manifest_changed = True

        if existing_decision.get("decision") in FINAL_DECISIONS:
            skipped_finalized += 1
            continue

        if duplicate_of:
            if item.get("research_status") != "DUPLICATE":
                item["research_status"] = "DUPLICATE"
                item["analysis_owner"] = ANALYSIS_OWNER
                manifest_changed = True
            skipped_duplicates += 1
            continue

        if item.get("research_status") != "PENDING_ANALYSIS" or item.get("analysis_owner") != ANALYSIS_OWNER:
            item["research_status"] = "PENDING_ANALYSIS"
            item["analysis_owner"] = ANALYSIS_OWNER
            manifest_changed = True

        packet = build_packet(
            root,
            shortcode,
            item,
            rec["transcript_path"],
            evidence_lineage_id=lineage,
            duplicate_of=None,
            duplicate_basis=None,
        )
        items.append(packet)

    items.sort(
        key=lambda x: (
            str(x.get("published_at") or ""),
            str(x.get("shortcode") or ""),
        ),
        reverse=True,
    )
    items = items[:max_items]

    queue = {
        "schema_version": 2,
        "screen_version": SCREEN_VERSION,
        "generated_at": utc_now(),
        "analysis_owner": ANALYSIS_OWNER,
        "status": "PENDING_ANALYSIS" if items else "EMPTY",
        "count": len(items),
        "skipped_finalized": skipped_finalized,
        "skipped_duplicates": skipped_duplicates,
        "skipped_missing_transcript": skipped_missing_transcript,
        "items": items,
    }
    atomic_write_json(queue_path, queue)
    if manifest_changed:
        atomic_write_json(manifest_path, manifest)

    # Compact per-item packets remain a transport convenience, not an analysis source of truth.
    for packet in items:
        creator = str(packet.get("creator") or "unknown")
        out_dir = root / "output" / creator / "research" / "pending"
        out_path = out_dir / f"{packet['shortcode']}.json"
        atomic_write_json(out_path, packet)

    print(json.dumps({
        "screen_version": SCREEN_VERSION,
        "analysis_owner": ANALYSIS_OWNER,
        "queue_status": queue["status"],
        "queued": queue["count"],
        "skipped_finalized": skipped_finalized,
        "skipped_duplicates": skipped_duplicates,
        "skipped_missing_transcript": skipped_missing_transcript,
        "queue_file": str(queue_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
###END_FILE:research_queue.py

###BEGIN_FILE:creator_evaluation.py
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktok_camofox_sync as sync

EVAL_VERSION = "0.1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Any = None) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if default is not None:
        return default
    raise FileNotFoundError(path)


def extract_handle(url: str) -> tuple[str | None, str | None]:
    parsed = urllib.parse.urlsplit(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    parts = [p for p in parsed.path.split("/") if p]
    if host.endswith("instagram.com") and parts:
        return "INSTAGRAM", parts[0].lstrip("@").strip()
    if host.endswith("tiktok.com") and parts and parts[0].startswith("@"):
        return "TIKTOK", parts[0][1:].strip()
    return None, None


def creator_key_from_handle(handle: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "", handle.casefold())
    if not key:
        raise ValueError(f"Could not derive creator key from handle: {handle!r}")
    return key[:80]


def iter_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from iter_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_strings(value)


def tiktok_candidates(obj: Any) -> list[str]:
    pattern = re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9._-]+)", re.I)
    out: list[str] = []
    seen: set[str] = set()
    for text in iter_strings(obj):
        normalized = text.replace("\\/", "/")
        for match in pattern.finditer(normalized):
            handle = match.group(1).rstrip("./")
            url = f"https://www.tiktok.com/@{handle}"
            low = url.casefold()
            if low not in seen:
                seen.add(low)
                out.append(url)
    return out


def browser_read(url: str, *, user_id: str, session_key: str) -> dict:
    tab_id = None
    result: dict[str, Any] = {"url": url, "snapshot": None, "links": None, "errors": []}
    try:
        tab = sync.request_json(
            "POST",
            "/tabs",
            {"userId": user_id, "sessionKey": session_key, "url": url, "trace": False},
            timeout=60,
        )
        if not isinstance(tab, dict) or not tab.get("tabId"):
            raise RuntimeError(f"Unexpected CamoFox create-tab response: {tab}")
        tab_id = str(tab["tabId"])
        time.sleep(3)
        try:
            result["snapshot"] = sync.request_json(
                "GET",
                f"/tabs/{urllib.parse.quote(tab_id)}/snapshot?"
                + urllib.parse.urlencode({"userId": user_id, "format": "text"}),
                timeout=30,
            )
        except Exception as exc:
            result["errors"].append(f"snapshot:{type(exc).__name__}:{exc}")
        try:
            result["links"] = sync.request_json(
                "GET",
                f"/tabs/{urllib.parse.quote(tab_id)}/links?"
                + urllib.parse.urlencode({"userId": user_id, "limit": 250}),
                timeout=20,
            )
        except Exception as exc:
            result["errors"].append(f"links:{type(exc).__name__}:{exc}")
    finally:
        if tab_id:
            try:
                sync.request_json(
                    "DELETE",
                    f"/tabs/{urllib.parse.quote(tab_id)}?"
                    + urllib.parse.urlencode({"userId": user_id}),
                    timeout=10,
                )
            except Exception:
                pass
    return result


def verify_tiktok_profile(profile_url: str, *, sample_size: int, creator_key: str) -> dict:
    platform, handle = extract_handle(profile_url)
    if platform != "TIKTOK" or not handle:
        return {"status": "UNVERIFIED", "profile_url": profile_url, "reason": "NOT_TIKTOK_PROFILE"}

    user_id = f"instagramresearch-eval-verify-{creator_key}-{re.sub(r'[^a-z0-9]', '', handle.casefold())[:30]}"
    session_key = f"eval-verify-{creator_key}-{compact_timestamp()}"
    tab_id = None
    try:
        tab = sync.request_json(
            "POST",
            "/tabs",
            {"userId": user_id, "sessionKey": session_key, "url": profile_url, "trace": False},
            timeout=60,
        )
        if not isinstance(tab, dict) or not tab.get("tabId"):
            raise RuntimeError(f"Unexpected CamoFox create-tab response: {tab}")
        tab_id = str(tab["tabId"])
        time.sleep(3)
        urls, diag = sync.collect_video_urls(
            tab_id,
            user_id=user_id,
            handle=handle,
            target=max(1, sample_size),
            max_scrolls=20,
        )
        if urls:
            return {
                "status": "VERIFIED",
                "profile_url": f"https://www.tiktok.com/@{handle}",
                "handle": handle,
                "video_urls_found": len(urls),
                "sample_urls": urls[: min(5, len(urls))],
                "diagnostic": diag,
            }
        return {
            "status": "UNVERIFIED",
            "profile_url": profile_url,
            "handle": handle,
            "video_urls_found": 0,
            "diagnostic": diag,
            "reason": "NO_EXACT_HANDLE_VIDEO_URLS_FOUND",
        }
    except Exception as exc:
        return {
            "status": "UNVERIFIED",
            "profile_url": profile_url,
            "handle": handle,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if tab_id:
            try:
                sync.request_json(
                    "DELETE",
                    f"/tabs/{urllib.parse.quote(tab_id)}?"
                    + urllib.parse.urlencode({"userId": user_id}),
                    timeout=10,
                )
            except Exception:
                pass


def discover_tiktok(profile_url: str, *, sample_size: int) -> dict:
    platform, source_handle = extract_handle(profile_url)
    if not platform or not source_handle:
        return {
            "status": "UNVERIFIED",
            "source_profile": profile_url,
            "reason": "UNSUPPORTED_PROFILE_URL",
            "candidates": [],
        }

    creator_key = creator_key_from_handle(source_handle)
    candidates: list[dict] = []

    if platform == "TIKTOK":
        candidates.append({"url": f"https://www.tiktok.com/@{source_handle}", "basis": "DIRECT_TIKTOK_PROFILE"})
    else:
        read = browser_read(
            profile_url,
            user_id=f"instagramresearch-eval-discovery-{creator_key}",
            session_key=f"eval-discovery-{creator_key}-{compact_timestamp()}",
        )
        explicit: list[str] = []
        explicit.extend(tiktok_candidates(read.get("snapshot")))
        explicit.extend(tiktok_candidates(read.get("links")))
        seen: set[str] = set()
        for url in explicit:
            low = url.casefold()
            if low in seen:
                continue
            seen.add(low)
            candidates.append({"url": url, "basis": "INSTAGRAM_PROFILE_LINK"})

        same_handle = f"https://www.tiktok.com/@{source_handle}"
        if same_handle.casefold() not in seen:
            candidates.append({"url": same_handle, "basis": "SAME_HANDLE_PROBE"})

    attempts = []
    for candidate in candidates:
        verified = verify_tiktok_profile(
            candidate["url"], sample_size=sample_size, creator_key=creator_key
        )
        attempts.append({**candidate, "verification": verified})
        if verified.get("status") == "VERIFIED":
            return {
                "status": "VERIFIED",
                "source_platform": platform,
                "source_profile": profile_url,
                "source_handle": source_handle,
                "tiktok_profile": verified["profile_url"],
                "tiktok_handle": verified["handle"],
                "match_basis": candidate["basis"],
                "attempts": attempts,
            }

    return {
        "status": "UNVERIFIED",
        "source_platform": platform,
        "source_profile": profile_url,
        "source_handle": source_handle,
        "reason": "NO_VERIFIED_TIKTOK_PROFILE",
        "attempts": attempts,
    }


def mark_evaluation_items(
    root: Path,
    *,
    result: dict,
    evaluation_run_id: str,
    source_profile: str,
    sample_size: int,
    verification: dict,
) -> list[str]:
    manifest_path = root / "state" / "manifest.json"
    manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
    items = manifest.setdefault("items", {})
    marked: list[str] = []
    for completed in result.get("completed", []):
        vid = str(completed.get("video_id") or "")
        key = f"tt_{vid}"
        item = items.get(key)
        if not isinstance(item, dict):
            continue
        item.update({
            "evaluation_mode": "CREATOR_EVALUATION",
            "evaluation_run_id": evaluation_run_id,
            "evaluation_source_profile": source_profile,
            "evaluation_sample_size": sample_size,
            "permanent_source": False,
            "creator_verification": {
                "status": verification.get("status"),
                "match_basis": verification.get("match_basis"),
                "tiktok_profile": verification.get("tiktok_profile"),
            },
        })
        marked.append(key)
    atomic_json(manifest_path, manifest)
    return marked


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot creator evaluation for InstagramResearch")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--profile-url", required=True)
    parser.add_argument("--sample-size", type=int, default=20)
    args = parser.parse_args()

    root = args.root.resolve()
    sample_size = max(1, min(int(args.sample_size), 100))
    platform, source_handle = extract_handle(args.profile_url)
    if not platform or not source_handle:
        raise SystemExit("Unsupported creator profile URL. Use an Instagram or TikTok profile URL.")

    creator_key = creator_key_from_handle(source_handle)
    evaluation_run_id = f"eval-{creator_key}-{compact_timestamp()}"
    status_path = root / "state" / "creator_evaluation_status.json"
    started_at = utc_now()

    atomic_json(status_path, {
        "schema_version": 1,
        "evaluation_version": EVAL_VERSION,
        "state": "RUNNING",
        "evaluation_run_id": evaluation_run_id,
        "source_profile": args.profile_url,
        "sample_size": sample_size,
        "started_at": started_at,
    })

    try:
        server = sync.start_server()
        discovery = discover_tiktok(args.profile_url, sample_size=sample_size)
        if discovery.get("status") != "VERIFIED":
            final = {
                "schema_version": 1,
                "evaluation_version": EVAL_VERSION,
                "state": "NO_VERIFIED_TIKTOK",
                "evaluation_run_id": evaluation_run_id,
                "source_profile": args.profile_url,
                "sample_size": sample_size,
                "started_at": started_at,
                "finished_at": utc_now(),
                "server": server,
                "discovery": discovery,
                "permanent_source": False,
            }
            atomic_json(status_path, final)
            atomic_json(root / "state" / "evaluations" / f"{evaluation_run_id}.json", final)
            print(json.dumps(final, ensure_ascii=False, indent=2))
            return 4

        tiktok_handle = str(discovery["tiktok_handle"])
        creator_key = creator_key_from_handle(tiktok_handle)
        source = {
            "creator_key": creator_key,
            "handle": tiktok_handle,
            "profile_url": discovery["tiktok_profile"],
            "enabled": True,
            "discovery_step": max(sample_size, 40),
            "max_catalog": max(200, sample_size * 5),
            "max_new_downloads": sample_size,
        }

        ingest = sync.process_source(root, source, max_new_override=sample_size)
        marked = mark_evaluation_items(
            root,
            result=ingest,
            evaluation_run_id=evaluation_run_id,
            source_profile=args.profile_url,
            sample_size=sample_size,
            verification=discovery,
        )
        queue = sync.run_research_queue(root)

        state = "DONE"
        if not queue.get("ok") or ingest.get("failures"):
            state = "DONE_WITH_ERRORS"
        if not marked:
            state = "NO_EVALUATION_ITEMS"

        final = {
            "schema_version": 1,
            "evaluation_version": EVAL_VERSION,
            "state": state,
            "evaluation_run_id": evaluation_run_id,
            "source_profile": args.profile_url,
            "sample_size_requested": sample_size,
            "permanent_source": False,
            "started_at": started_at,
            "finished_at": utc_now(),
            "server": server,
            "discovery": discovery,
            "ingest": ingest,
            "evaluation_items_marked": marked,
            "evaluation_items_count": len(marked),
            "research_queue": queue,
        }
        atomic_json(status_path, final)
        atomic_json(root / "state" / "evaluations" / f"{evaluation_run_id}.json", final)
        print(json.dumps(final, ensure_ascii=False, indent=2))
        return 0 if state == "DONE" else 1
    except Exception as exc:
        final = {
            "schema_version": 1,
            "evaluation_version": EVAL_VERSION,
            "state": "FAILED",
            "evaluation_run_id": evaluation_run_id,
            "source_profile": args.profile_url,
            "sample_size": sample_size,
            "permanent_source": False,
            "started_at": started_at,
            "finished_at": utc_now(),
            "error": f"{type(exc).__name__}: {exc}",
        }
        atomic_json(status_path, final)
        atomic_json(root / "state" / "evaluations" / f"{evaluation_run_id}.json", final)
        print(json.dumps(final, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
###END_FILE:creator_evaluation.py

###BEGIN_FILE:15_evaluate_creator.bat
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
set "PROFILE="
set /p "PROFILE=Creator profile URL (Instagram or TikTok): "
if "%PROFILE%"=="" (
  echo ERROR: No profile URL supplied.
  pause
  exit /b 3
)
set "SAMPLE=20"
set /p "SAMPLE=Sample size [20]: "
if "%SAMPLE%"=="" set "SAMPLE=20"
echo.
echo Creator evaluation starting...
echo Profile: %PROFILE%
echo Sample:  %SAMPLE%
echo.
"%PY%" creator_evaluation.py --root "%~dp0.." --profile-url "%PROFILE%" --sample-size "%SAMPLE%"
set "ERR=%ERRORLEVEL%"
echo.
echo Exit code: %ERR%
echo Status: ..\state\creator_evaluation_status.json
echo Queue:  ..\state\research_queue.json
pause
exit /b %ERR%
###END_FILE:15_evaluate_creator.bat

###BEGIN_FILE:16_evaluate_josh_oldmixon.bat
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
echo Josh Oldmixon creator evaluation
echo =================================
echo Source: https://www.instagram.com/josholdmixon/
echo Requested sample: 20 videos
echo Permanent source: NO
echo.
"%PY%" creator_evaluation.py --root "%~dp0.." --profile-url "https://www.instagram.com/josholdmixon/" --sample-size 20
set "ERR=%ERRORLEVEL%"
echo.
echo Exit code: %ERR%
echo Status: ..\state\creator_evaluation_status.json
echo Queue:  ..\state\research_queue.json
pause
exit /b %ERR%
###END_FILE:16_evaluate_josh_oldmixon.bat
