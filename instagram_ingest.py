from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright


APP_VERSION = "0.3.1"
REEL_RE = re.compile(r"/reel/([A-Za-z0-9_-]+)/?")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, data: dict) -> None:
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


def safe_creator(handle: str) -> str:
    return handle.strip().lstrip("@").replace("/", "_").replace("\\", "_")


def profile_dir() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    return local / "InstagramResearch" / "chrome-profile"


def secret_dir() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    d = local / "InstagramResearch" / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def verify_logged_in(context) -> None:
    cookies = context.cookies(["https://www.instagram.com/"])
    names = {c.get("name", "") for c in cookies}
    if "sessionid" not in names:
        raise RuntimeError(
            "Dedicated InstagramResearch Chrome profile is not authenticated. "
            "Run app\\02_authenticate.bat first."
        )


def collect_reel_urls(page, creator: str, max_scan: int) -> list[str]:
    url = f"https://www.instagram.com/{creator}/reels/"
    print(f"Scanning @{creator}: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)

    found: dict[str, str] = {}
    stable_rounds = 0
    previous_count = -1

    while len(found) < max_scan and stable_rounds < 4:
        hrefs = page.locator('a[href*="/reel/"]').evaluate_all(
            "(els) => els.map(e => e.getAttribute('href')).filter(Boolean)"
        )
        for href in hrefs:
            m = REEL_RE.search(href)
            if not m:
                continue
            shortcode = m.group(1)
            found.setdefault(shortcode, urljoin("https://www.instagram.com", href))

        if len(found) == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            previous_count = len(found)

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1400)

    return list(found.values())[:max_scan]


def reel_shortcode(url: str) -> str:
    m = REEL_RE.search(url)
    if not m:
        raise ValueError(f"Not a Reel URL: {url}")
    return m.group(1)


def write_netscape_cookiefile(context, path: Path) -> None:
    """Export only the dedicated Playwright profile cookies to a temporary local file."""
    cookies = context.cookies()
    lines = [
        "# Netscape HTTP Cookie File",
        "# Temporary InstagramResearch cookie export. DO NOT copy to Google Drive.",
    ]
    for c in cookies:
        domain = str(c.get("domain", ""))
        if not domain:
            continue
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        cookie_path = str(c.get("path") or "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        if not name:
            continue
        lines.append(
            "\t".join([
                domain,
                include_subdomains,
                cookie_path,
                secure,
                str(expires),
                name,
                value,
            ])
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_media(path: Path) -> tuple[bool, str]:
    """Fail closed: only accept a file PyAV can open with a video stream."""
    if not path.exists():
        return False, "file_missing"
    size = path.stat().st_size
    if size < 50_000:
        return False, f"file_too_small:{size}"
    try:
        import av
        with av.open(str(path)) as container:
            video_streams = [s for s in container.streams if s.type == "video"]
            if not video_streams:
                return False, "no_video_stream"
            duration = container.duration
            return True, f"ok:size={size}:duration={duration}"
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc}"


def remove_shortcode_files(raw_dir: Path, shortcode: str) -> None:
    for p in raw_dir.glob(f"{shortcode}.*"):
        if p.is_file():
            try:
                p.unlink()
            except Exception:
                pass


def download_with_ytdlp(
    context,
    root: Path,
    creator: str,
    reel_url: str,
) -> dict:
    """
    Use yt-dlp for the media extractor, but authenticate it with cookies exported
    from the isolated Playwright profile. The temporary cookie file never enters Drive.
    """
    shortcode = reel_shortcode(reel_url)
    raw_dir = root / "output" / creator / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Remove files from earlier failed downloader attempts for this shortcode.
    remove_shortcode_files(raw_dir, shortcode)

    cookie_path = secret_dir() / "instagram_ytdlp_cookies.txt"
    write_netscape_cookiefile(context, cookie_path)

    output_template = str(raw_dir / f"{shortcode}.%(ext)s")
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--cookies", str(cookie_path),
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--format", "b[ext=mp4]/b",
        "--output", output_template,
        "--print", "after_move:filepath",
        reel_url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        try:
            cookie_path.unlink(missing_ok=True)
        except Exception:
            pass

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if len(detail) > 1000:
            detail = detail[-1000:]
        raise RuntimeError(f"yt-dlp failed code {result.returncode}: {detail}")

    printed = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    candidates: list[Path] = []
    for line in reversed(printed):
        p = Path(line)
        if p.exists():
            candidates.append(p)
            break
    if not candidates:
        candidates = sorted(
            [p for p in raw_dir.glob(f"{shortcode}.*") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    if not candidates:
        raise RuntimeError("yt-dlp completed but no output media file was found.")

    media_path = candidates[0]
    valid, validation = validate_media(media_path)
    if not valid:
        try:
            media_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(f"downloaded media failed validation: {validation}")

    return {
        "schema_version": 1,
        "shortcode": shortcode,
        "creator": creator,
        "url": reel_url,
        "video_file": str(media_path.relative_to(root)),
        "downloaded_at": utc_now(),
        "download_engine": "yt-dlp_via_isolated_playwright_cookies",
        "download_status": "DONE",
        "media_validation": validation,
        "transcription_status": "PENDING",
        "bytes": media_path.stat().st_size,
    }


def existing_item_is_valid(root: Path, item: dict) -> bool:
    if item.get("download_status") != "DONE":
        return False
    rel = item.get("video_file")
    if not rel:
        return False
    path = root / rel
    valid, validation = validate_media(path)
    if valid:
        item["media_validation"] = validation
        return True
    item["download_status"] = "INVALID"
    item["media_validation"] = validation
    item["transcription_status"] = "NOT_STARTED"
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def transcribe_videos(root: Path, manifest: dict, settings: dict, keys: list[str]) -> dict:
    tcfg = settings.get("transcription", {})
    if not tcfg.get("enabled", True) or not keys:
        return {"attempted": 0, "completed": 0, "errors": []}

    # Silence Hugging Face's Windows symlink warning; cache still works without symlinks.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        return {
            "attempted": len(keys),
            "completed": 0,
            "errors": [f"faster-whisper import failed: {exc}"],
        }

    model = WhisperModel(
        str(tcfg.get("model_size", "small")),
        device=str(tcfg.get("device", "cpu")),
        compute_type=str(tcfg.get("compute_type", "int8")),
    )
    beam_size = int(tcfg.get("beam_size", 5))
    vad_filter = bool(tcfg.get("vad_filter", True))

    completed = 0
    errors: list[str] = []

    for key in keys:
        item = manifest["items"].get(key, {})
        creator = item.get("creator", "")
        video_rel = item.get("video_file")
        if not video_rel:
            continue

        video_path = root / video_rel
        valid, validation = validate_media(video_path)
        if not valid:
            item["download_status"] = "INVALID"
            item["media_validation"] = validation
            item["transcription_status"] = "NOT_STARTED"
            errors.append(f"{key}: invalid media before transcription: {validation}")
            continue

        transcript_dir = root / "output" / creator / "transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        txt_path = transcript_dir / f"{key}.txt"
        json_path = transcript_dir / f"{key}.json"

        if txt_path.exists() and json_path.exists():
            item["transcription_status"] = "DONE"
            item["transcript_txt"] = str(txt_path.relative_to(root))
            item["transcript_json"] = str(json_path.relative_to(root))
            continue

        try:
            segments, info = model.transcribe(
                str(video_path),
                beam_size=beam_size,
                vad_filter=vad_filter,
            )
            rows = []
            parts = []
            for seg in segments:
                text = (seg.text or "").strip()
                if text:
                    parts.append(text)
                rows.append({
                    "start": round(float(seg.start), 3),
                    "end": round(float(seg.end), 3),
                    "text": text,
                })

            full_text = " ".join(parts).strip()
            txt_path.write_text(full_text + ("\n" if full_text else ""), encoding="utf-8")
            atomic_write_json(json_path, {
                "schema_version": 1,
                "shortcode": key,
                "creator": creator,
                "source_url": item.get("url"),
                "language": getattr(info, "language", None),
                "language_probability": getattr(info, "language_probability", None),
                "duration": getattr(info, "duration", None),
                "generated_at": utc_now(),
                "segments": rows,
            })
            item["transcription_status"] = "DONE"
            item["transcript_txt"] = str(txt_path.relative_to(root))
            item["transcript_json"] = str(json_path.relative_to(root))
            item["transcribed_at"] = utc_now()
            completed += 1
        except Exception as exc:
            item["transcription_status"] = "ERROR"
            item["transcription_error"] = f"{type(exc).__name__}: {exc}"
            errors.append(f"{key}: {type(exc).__name__}: {exc}")

    return {"attempted": len(keys), "completed": completed, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--skip-transcription", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    settings = load_json(root / "control" / "settings.json")
    creators_cfg = load_json(root / "control" / "creators.json", default=[])
    manifest_path = root / "state" / "manifest.json"
    status_path = root / "state" / "status.json"
    manifest = load_json(
        manifest_path,
        default={"schema_version": 1, "items": {}},
    )
    manifest.setdefault("items", {})

    creators = [
        safe_creator(str(x.get("handle", "")))
        for x in creators_cfg
        if x.get("enabled", False) and str(x.get("handle", "")).strip()
    ]
    creators = [c for c in creators if c and c != "CHANGE_ME"]
    if not creators:
        raise RuntimeError("No enabled creators in control\\creators.json.")

    max_scan = int(settings.get("max_scan_per_creator", 50))
    max_new = int(settings.get("max_new_per_creator", 10))

    started = utc_now()
    atomic_write_json(status_path, {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "state": "RUNNING",
        "started_at": started,
        "creators": creators,
    })

    summaries = []

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir()),
                channel="chrome",
                headless=False,
                args=["--start-maximized"],
                viewport=None,
            )
            try:
                verify_logged_in(context)
                page = context.pages[0] if context.pages else context.new_page()

                for creator in creators:
                    summary = {
                        "creator": creator,
                        "scanned": 0,
                        "downloaded": 0,
                        "skipped_known": 0,
                        "invalid_retried": 0,
                        "errors": [],
                        "new_keys": [],
                    }
                    try:
                        urls = collect_reel_urls(page, creator, max_scan)
                        summary["scanned"] = len(urls)

                        for reel_url in urls:
                            if summary["downloaded"] >= max_new:
                                break

                            key = reel_shortcode(reel_url)
                            existing = manifest["items"].get(key)

                            if existing and existing_item_is_valid(root, existing):
                                summary["skipped_known"] += 1
                                continue

                            if existing:
                                summary["invalid_retried"] += 1

                            try:
                                item = download_with_ytdlp(
                                    context,
                                    root,
                                    creator,
                                    reel_url,
                                )
                                manifest["items"][key] = item
                                summary["downloaded"] += 1
                                summary["new_keys"].append(key)
                                atomic_write_json(manifest_path, manifest)
                                time.sleep(1.0)
                            except Exception as exc:
                                err = f"{key}: {type(exc).__name__}: {exc}"
                                summary["errors"].append(err)
                                if existing:
                                    existing["download_status"] = "ERROR"
                                    existing["download_error"] = err
                                    existing["transcription_status"] = "NOT_STARTED"
                                atomic_write_json(manifest_path, manifest)

                    except Exception as exc:
                        summary["errors"].append(f"{type(exc).__name__}: {exc}")

                    summaries.append(summary)
            finally:
                context.close()

        if args.skip_transcription:
            transcription = {
                "attempted": 0,
                "completed": 0,
                "errors": [],
                "skipped": True,
            }
        else:
            pending = [
                key
                for key, item in manifest["items"].items()
                if item.get("download_status") == "DONE"
                and item.get("video_file")
                and item.get("transcription_status") != "DONE"
            ]
            transcription = transcribe_videos(root, manifest, settings, pending)
            atomic_write_json(manifest_path, manifest)

        total_errors = (
            sum(len(s["errors"]) for s in summaries)
            + len(transcription.get("errors", []))
        )

        status = {
            "schema_version": 1,
            "app_version": APP_VERSION,
            "state": "DONE" if total_errors == 0 else "DONE_WITH_ERRORS",
            "started_at": started,
            "finished_at": utc_now(),
            "creator_summaries": summaries,
            "transcription": transcription,
            "total_new_videos": sum(s["downloaded"] for s in summaries),
            "total_errors": total_errors,
        }
        atomic_write_json(status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if total_errors == 0 else 1

    except Exception as exc:
        status = {
            "schema_version": 1,
            "app_version": APP_VERSION,
            "state": "ERROR",
            "started_at": started,
            "finished_at": utc_now(),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
        atomic_write_json(status_path, status)
        print(status["traceback"], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
