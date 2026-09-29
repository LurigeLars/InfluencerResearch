from __future__ import annotations

import argparse
import contextlib
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
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import sync_playwright

from transcription_backend import transcribe_video
from video_visual_evidence import VISUAL_REVIEW_POLICY_VERSION, capture_local_video_visual_evidence


APP_VERSION = "0.3.3"
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
    value = str(handle or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", value):
        raise ValueError("Invalid Instagram creator handle")
    if value in {".", ".."} or value.endswith("."):
        raise ValueError("Unsafe Instagram creator path segment")
    if re.fullmatch(r"(?i:(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?)", value):
        raise ValueError("Reserved Windows creator path segment")
    return value


def runtime_dir() -> Path:
    if os.environ.get("INFLUENCER_RESEARCH_CONTAINER", "").strip() == "1":
        return Path("/runtime/influencerresearch")
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "InfluencerResearch"
    return Path.home() / ".local" / "share" / "InfluencerResearch"


def profile_dir() -> Path:
    return runtime_dir() / "chrome-profile"


def secret_dir() -> Path:
    if os.environ.get("INFLUENCER_RESEARCH_CONTAINER", "").strip() == "1":
        d = Path("/run/influencerresearch-secrets")
    else:
        d = runtime_dir() / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def instagram_cookie_path() -> Path:
    return secret_dir() / "instagram_cookies.json"


def load_instagram_cookies(context) -> None:
    path = instagram_cookie_path()
    if not path.is_file():
        return
    cookies = load_json(path, [])
    if not isinstance(cookies, list) or not any(
        isinstance(cookie, dict) and cookie.get("name") == "sessionid"
        for cookie in cookies
    ):
        raise RuntimeError("Instagram cookie bootstrap is missing a sessionid cookie.")
    context.add_cookies(cookies)


def launch_instagram_context(playwright):
    container_mode = os.environ.get("INFLUENCER_RESEARCH_CONTAINER", "").strip() == "1"
    kwargs = {
        "user_data_dir": str(profile_dir()),
        "headless": container_mode,
    }
    if container_mode:
        kwargs["viewport"] = {"width": 1440, "height": 1200}
    else:
        kwargs.update({
            "channel": "chrome",
            "args": ["--start-maximized"],
            "viewport": None,
        })
    context = playwright.chromium.launch_persistent_context(**kwargs)
    if container_mode:
        load_instagram_cookies(context)
    return context


def verify_logged_in(context) -> None:
    cookies = context.cookies(["https://www.instagram.com/"])
    names = {c.get("name", "") for c in cookies}
    if "sessionid" not in names:
        raise RuntimeError(
            "Instagram session is not authenticated. Run scripts\\authenticate_instagram.ps1 "
            "and then scripts\\runtime.ps1 -Action ImportInstagramAuth."
        )


def launch_instagram_ephemeral_context(playwright):
    """Launch an isolated authenticated browser context without the persistent profile.

    Recent discovery runs concurrently with Story capture, so sharing the persistent
    user_data_dir would create profile-lock races. Cookies are loaded from the same
    tmpfs-backed secret used by the normal Instagram ingestion path.
    """
    container_mode = os.environ.get("INFLUENCER_RESEARCH_CONTAINER", "").strip() == "1"
    browser = playwright.chromium.launch(headless=container_mode)
    context_kwargs: dict[str, Any] = {}
    if container_mode:
        context_kwargs["viewport"] = {"width": 1440, "height": 1200}
    context = browser.new_context(**context_kwargs)
    load_instagram_cookies(context)
    return browser, context


def _instagram_page_access_state(page, creator: str) -> dict[str, Any]:
    current_url = str(getattr(page, "url", "") or "")
    body_text = ""
    with contextlib.suppress(Exception):
        body_text = str(page.locator("body").inner_text(timeout=2500) or "")
    folded = body_text.casefold()
    url_folded = current_url.casefold()
    creator_folded = creator.casefold()

    media_auth_gated = (
        "/accounts/login" in url_folded
        or "log in to see photos and videos" in folded
        or "log in to instagram" in folded
        or "logga in på instagram" in folded
        or "logga in för att se" in folded
    )
    blocked = (
        "/challenge/" in url_folded
        or "/checkpoint/" in url_folded
        or "confirm it's you" in folded
        or "we restrict certain activity" in folded
        or "suspicious login attempt" in folded
    )
    handle_visible = creator_folded in folded
    return {
        "url": current_url,
        "body_text_length": len(body_text),
        "handle_visible": handle_visible,
        "media_auth_gated": media_auth_gated,
        "blocked": blocked,
    }


def _wait_for_instagram_profile_ready(
    page,
    creator: str,
    *,
    max_wait_ms: int = 5000,
    poll_ms: int = 250,
) -> dict[str, Any]:
    started = time.perf_counter()
    attempts = 0
    state: dict[str, Any] = {}
    reel_count = 0
    while True:
        attempts += 1
        state = _instagram_page_access_state(page, creator)
        with contextlib.suppress(Exception):
            reel_count = int(page.locator('a[href*="/reel/"]').count())
        if (
            reel_count > 0
            or state.get("media_auth_gated")
            or state.get("blocked")
            or int(state.get("body_text_length") or 0) > 300
        ):
            break
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms >= max_wait_ms:
            break
        page.wait_for_timeout(min(poll_ms, max_wait_ms - int(elapsed_ms)))

    return {
        **state,
        "ready": bool(
            reel_count > 0
            or state.get("media_auth_gated")
            or state.get("blocked")
            or int(state.get("body_text_length") or 0) > 300
        ),
        "reel_link_count": reel_count,
        "attempts": attempts,
        "wait_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def _collect_loaded_reel_urls(page, max_scan: int) -> tuple[list[str], int]:
    found: dict[str, str] = {}
    stable_rounds = 0
    previous_count = -1
    rounds = 0

    while len(found) < max_scan and stable_rounds < 4:
        rounds += 1
        hrefs = page.locator('a[href*="/reel/"]').evaluate_all(
            "(els) => els.map(e => e.getAttribute('href')).filter(Boolean)"
        )
        for href in hrefs:
            match = REEL_RE.search(str(href or ""))
            if not match:
                continue
            shortcode = match.group(1)
            found.setdefault(
                shortcode,
                urljoin("https://www.instagram.com", str(href)),
            )

        if len(found) == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            previous_count = len(found)

        if len(found) >= max_scan or stable_rounds >= 4:
            break
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1400)

    return list(found.values())[:max_scan], rounds


def _reel_published_at(page, reel_url: str) -> tuple[str | None, str | None, float]:
    started = time.perf_counter()
    try:
        page.goto(reel_url, wait_until="domcontentloaded", timeout=60000)
        value = page.locator("time[datetime]").first.get_attribute(
            "datetime",
            timeout=3000,
        )
        return (
            str(value) if value else None,
            None if value else "MISSING_TIME_ELEMENT",
            round((time.perf_counter() - started) * 1000, 1),
        )
    except Exception as exc:
        return (
            None,
            f"{type(exc).__name__}: {exc}"[:1000],
            round((time.perf_counter() - started) * 1000, 1),
        )


def discover_reels_authenticated(
    creator: str,
    *,
    max_scan: int = 20,
    known_reel_times: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Read-only Instagram Reel discovery using the imported authenticated session."""
    creator = safe_creator(creator)
    max_scan = max(1, min(int(max_scan), 20))
    known_reel_times = {
        str(key): str(value)
        for key, value in (known_reel_times or {}).items()
        if key and value
    }

    started = time.perf_counter()
    browser = None
    context = None
    browser_launch_ms = 0.0
    profile_load_ms = 0.0
    readiness: dict[str, Any] = {}
    discovery_rounds = 0
    reel_time_cache_hits = 0
    reel_time_network_probes = 0
    reel_time_probe_ms = 0.0
    authenticated = False

    try:
        with sync_playwright() as playwright:
            launch_started = time.perf_counter()
            browser, context = launch_instagram_ephemeral_context(playwright)
            browser_launch_ms = round(
                (time.perf_counter() - launch_started) * 1000,
                1,
            )
            try:
                verify_logged_in(context)
                authenticated = True
            except RuntimeError:
                return {
                    "ok": False,
                    "authenticated": False,
                    "blocked": False,
                    "media_auth_gated": True,
                    "reel_count": 0,
                    "reel_items": [],
                    "reel_discovery_ok": False,
                    "error": "INSTAGRAM_SESSION_NOT_AUTHENTICATED",
                    "timings": {
                        "browser_launch_ms": browser_launch_ms,
                        "profile_ready_wait_ms": 0.0,
                        "profile_ready_attempts": 0,
                        "profile_ready": False,
                        "discovery_rounds": 0,
                        "reel_time_cache_hits": 0,
                        "reel_time_network_probes": 0,
                        "reel_time_probe_ms": 0.0,
                        "total_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                }

            page = context.new_page()
            profile_url = f"https://www.instagram.com/{creator}/reels/"
            profile_started = time.perf_counter()
            page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
            profile_load_ms = round(
                (time.perf_counter() - profile_started) * 1000,
                1,
            )
            readiness = _wait_for_instagram_profile_ready(page, creator)

            blocked = bool(readiness.get("blocked"))
            media_auth_gated = bool(readiness.get("media_auth_gated"))
            reel_urls: list[str] = []
            if not blocked and not media_auth_gated:
                reel_urls, discovery_rounds = _collect_loaded_reel_urls(
                    page,
                    max_scan,
                )

            reel_items: list[dict[str, Any]] = []
            for reel_url in reel_urls:
                shortcode = reel_shortcode(reel_url)
                published_at = known_reel_times.get(shortcode)
                error = None
                source = None
                if published_at:
                    reel_time_cache_hits += 1
                    source = "LOCAL_MANIFEST_CACHE"
                else:
                    reel_time_network_probes += 1
                    published_at, error, probe_ms = _reel_published_at(
                        page,
                        reel_url,
                    )
                    reel_time_probe_ms += probe_ms
                    source = "AUTHENTICATED_REEL_TIME_ELEMENT"
                reel_items.append({
                    "url": reel_url,
                    "published_at": published_at,
                    "published_at_source": source,
                    "error": error,
                })

            return {
                "ok": bool(authenticated and not blocked and not media_auth_gated),
                "authenticated": authenticated,
                "blocked": blocked,
                "media_auth_gated": media_auth_gated,
                "reel_count": len(reel_urls),
                "reel_items": reel_items,
                "reel_discovery_ok": bool(
                    authenticated and not blocked and not media_auth_gated
                ),
                "error": None,
                "timings": {
                    "browser_launch_ms": browser_launch_ms,
                    "profile_load_ms": profile_load_ms,
                    "profile_ready_wait_ms": round(
                        float(readiness.get("wait_ms") or 0.0),
                        1,
                    ),
                    "profile_ready_attempts": int(
                        readiness.get("attempts") or 0
                    ),
                    "profile_ready": bool(readiness.get("ready")),
                    "discovery_rounds": discovery_rounds,
                    "reel_time_cache_hits": reel_time_cache_hits,
                    "reel_time_network_probes": reel_time_network_probes,
                    "reel_time_probe_ms": round(reel_time_probe_ms, 1),
                    "total_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            }
    except Exception as exc:
        return {
            "ok": False,
            "authenticated": authenticated,
            "blocked": False,
            "media_auth_gated": False,
            "reel_count": 0,
            "reel_items": [],
            "reel_discovery_ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:1000],
            "timings": {
                "browser_launch_ms": browser_launch_ms,
                "profile_load_ms": profile_load_ms,
                "profile_ready_wait_ms": round(
                    float(readiness.get("wait_ms") or 0.0),
                    1,
                ),
                "profile_ready_attempts": int(
                    readiness.get("attempts") or 0
                ),
                "profile_ready": bool(readiness.get("ready")),
                "discovery_rounds": discovery_rounds,
                "reel_time_cache_hits": reel_time_cache_hits,
                "reel_time_network_probes": reel_time_network_probes,
                "reel_time_probe_ms": round(reel_time_probe_ms, 1),
                "total_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        }
    finally:
        if context is not None:
            with contextlib.suppress(Exception):
                context.close()
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()


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


def canonical_reel_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme != "https" or not (host == "instagram.com" or host.endswith(".instagram.com")):
        raise ValueError("Invalid Instagram Reel host")
    match = re.fullmatch(
        r"/(?:[A-Za-z0-9._]{1,30}/)?reel/([A-Za-z0-9_-]+)/?",
        parsed.path,
    )
    if not match:
        raise ValueError("Invalid Instagram Reel path")
    return f"https://www.instagram.com/reel/{match.group(1)}/"


def reel_shortcode(url: str) -> str:
    m = REEL_RE.search(canonical_reel_url(url))
    if not m:
        raise ValueError(f"Not a Reel URL: {url}")
    return m.group(1)


def write_netscape_cookiefile(context, path: Path) -> None:
    """Export only the dedicated Playwright profile cookies to a temporary local file."""
    cookies = context.cookies()
    lines = [
        "# Netscape HTTP Cookie File",
        "# Temporary InfluencerResearch cookie export. DO NOT copy to Google Drive.",
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
            with contextlib.suppress(OSError):
                p.unlink()


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
    reel_url = canonical_reel_url(reel_url)
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
        "--",
        reel_url,
    ]

    try:
        # URL is strict-canonical Instagram and '--' terminates yt-dlp option parsing.
        # codeql[py/command-line-injection]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        with contextlib.suppress(OSError):
            cookie_path.unlink(missing_ok=True)

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
        with contextlib.suppress(OSError):
            media_path.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded media failed validation: {validation}")

    return {
        "schema_version": 1,
        "shortcode": shortcode,
        "creator": creator,
        "source_platform": "INSTAGRAM",
        "source_id": shortcode,
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
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
    return False


def transcribe_videos(root: Path, manifest: dict, settings: dict, keys: list[str]) -> dict:
    tcfg = settings.get("transcription", {})
    if not tcfg.get("enabled", True) or not keys:
        return {"attempted": 0, "completed": 0, "errors": []}

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
            result = transcribe_video(video_path, tcfg)
            full_text = str(result.get("text") or "").strip()
            txt_path.write_text(full_text + ("\n" if full_text else ""), encoding="utf-8")

            metadata = {
                "schema_version": 1,
                "shortcode": key,
                "creator": creator,
                "source_url": item.get("url"),
                "provider": result.get("provider"),
                "model": result.get("model"),
                "language": result.get("language"),
                "language_probability": result.get("language_probability"),
                "duration": result.get("duration"),
                "generated_at": utc_now(),
                "segments": result.get("segments", []),
            }
            if result.get("fallback_from"):
                metadata["fallback_from"] = result.get("fallback_from")
                metadata["fallback_error"] = result.get("fallback_error")
            atomic_write_json(json_path, metadata)

            item["transcription_status"] = "DONE"
            item["transcription_provider"] = result.get("provider")
            item["transcription_model"] = result.get("model")
            item["transcript_txt"] = str(txt_path.relative_to(root))
            item["transcript_json"] = str(json_path.relative_to(root))
            item["transcribed_at"] = utc_now()
            completed += 1
        except Exception as exc:
            item["transcription_status"] = "ERROR"
            item["transcription_error"] = f"{type(exc).__name__}: {exc}"
            errors.append(f"{key}: {type(exc).__name__}: {exc}")

    return {"attempted": len(keys), "completed": completed, "errors": errors}


def select_visual_evidence_keys(
    manifest: dict,
    summaries: list[dict],
    *,
    new_only: bool,
    only_shortcodes: set[str] | None = None,
) -> list[str]:
    if only_shortcodes is not None:
        candidates = list(only_shortcodes)
    elif new_only:
        candidates = [
            key
            for summary in summaries
            for key in summary.get("new_keys", [])
        ]
    else:
        candidates = list(manifest.get("items", {}).keys())

    selected: list[str] = []
    seen: set[str] = set()
    for key in candidates:
        if key in seen:
            continue
        seen.add(key)
        item = manifest.get("items", {}).get(key, {})
        if (
            item.get("download_status") == "DONE"
            and item.get("video_file")
            and int(item.get("visual_review_policy_version") or 0) < VISUAL_REVIEW_POLICY_VERSION
        ):
            selected.append(key)
    return selected


def enrich_visual_evidence(root: Path, manifest: dict, keys: list[str]) -> dict:
    completed = 0
    fail_open = 0
    errors: list[str] = []
    for key in keys:
        item = manifest.get("items", {}).get(key, {})
        creator = str(item.get("creator") or "")
        video_rel = item.get("video_file")
        if not creator or not video_rel:
            continue
        media_path = root / str(video_rel)
        transcript_text = ""
        transcript_rel = item.get("transcript_txt")
        if transcript_rel:
            transcript_path = root / str(transcript_rel)
            if transcript_path.is_file():
                transcript_text = transcript_path.read_text(encoding="utf-8", errors="replace").strip()
        try:
            result = capture_local_video_visual_evidence(
                root,
                creator,
                "INSTAGRAM",
                key,
                media_path,
                transcript_text=transcript_text,
            )
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}:visual_evidence_unavailable"}

        item["visual_review_policy_version"] = VISUAL_REVIEW_POLICY_VERSION
        if result.get("ok"):
            bundle = result.get("agent_visual_bundle") if isinstance(result.get("agent_visual_bundle"), dict) else {}
            item["visual_evidence_status"] = "DONE"
            item.pop("visual_evidence_error", None)
            if result.get("index"):
                item["visual_evidence_index"] = str(Path(result["index"]).relative_to(root))
            item["visual_frame_count"] = int(result.get("retained_frames") or 0)
            item["visual_capture_strategy"] = result.get("capture_strategy")
            item["agent_visual_bundle"] = bundle or None
            item["analysis_mode_recommended"] = bundle.get("analysis_mode_recommended") or "TRANSCRIPT_ONLY"
            item["visual_review_recommended"] = bool(bundle.get("visual_review_recommended"))
            item["visual_review_reason"] = list(bundle.get("visual_review_reason") or [])
            item["creator_visual_prior"] = bundle.get("creator_visual_prior") or "NEUTRAL"
            completed += 1
        else:
            item["visual_evidence_status"] = "ERROR"
            item["visual_evidence_error"] = str(result.get("error") or "visual_evidence_unavailable")[:500]
            item["analysis_mode_recommended"] = "TRANSCRIPT_ONLY"
            item["visual_review_recommended"] = False
            item["visual_review_reason"] = []
            item["creator_visual_prior"] = "NEUTRAL"
            fail_open += 1
            errors.append(f"{key}: {item['visual_evidence_error']}")
    return {"attempted": len(keys), "completed": completed, "fail_open": fail_open, "errors": errors}


def resolve_creators(creators_cfg: list[dict], override: str | None) -> list[str]:
    if override is not None:
        return [safe_creator(override)]

    creators = [
        safe_creator(str(x.get("handle", "")))
        for x in creators_cfg
        if x.get("enabled", False) and str(x.get("handle", "")).strip()
    ]
    creators = [creator for creator in creators if creator and creator != "CHANGE_ME"]
    if not creators:
        raise RuntimeError("No enabled creators in control\\creators.json.")
    return creators


def resolve_max_new_per_creator(settings: dict, override: int | None) -> int:
    value = int(settings.get("max_new_per_creator", 10)) if override is None else int(override)
    if value < 1:
        raise ValueError("max_new_per_creator must be at least 1")
    return value


def select_transcription_keys(
    manifest: dict,
    summaries: list[dict],
    *,
    new_only: bool,
) -> list[str]:
    if new_only:
        candidates = [
            key
            for summary in summaries
            for key in summary.get("new_keys", [])
        ]
    else:
        candidates = list(manifest.get("items", {}).keys())

    selected = []
    seen = set()
    for key in candidates:
        if key in seen:
            continue
        seen.add(key)
        item = manifest.get("items", {}).get(key, {})
        if (
            item.get("download_status") == "DONE"
            and item.get("video_file")
            and item.get("transcription_status") != "DONE"
        ):
            selected.append(key)
    return selected

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--skip-transcription", action="store_true")
    parser.add_argument(
        "--creator",
        default=None,
        help="Run only one explicit Instagram creator handle.",
    )
    parser.add_argument(
        "--max-new-per-creator",
        type=int,
        default=None,
        help="Override settings.json max_new_per_creator for this run.",
    )
    parser.add_argument(
        "--transcribe-new-only",
        action="store_true",
        help="Transcribe only videos downloaded during this run.",
    )
    parser.add_argument(
        "--only-shortcodes",
        default=None,
        help="Optional comma-separated Reel shortcodes to ingest; maximum 20.",
    )
    args = parser.parse_args()

    only_shortcodes: set[str] | None = None
    if args.only_shortcodes:
        values = [value.strip() for value in str(args.only_shortcodes).split(",") if value.strip()]
        if not 1 <= len(values) <= 20:
            raise ValueError("only_shortcodes must contain 1-20 values")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) for value in values):
            raise ValueError("BAD_ONLY_SHORTCODE")
        only_shortcodes = set(values)

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

    creators = resolve_creators(creators_cfg, args.creator)

    max_scan = int(settings.get("max_scan_per_creator", 50))
    max_new = resolve_max_new_per_creator(settings, args.max_new_per_creator)

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
            context = launch_instagram_context(p)
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
                            if only_shortcodes is not None and key not in only_shortcodes:
                                continue
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
            pending = select_transcription_keys(
                manifest,
                summaries,
                new_only=args.transcribe_new_only,
            )
            transcription = transcribe_videos(root, manifest, settings, pending)
            atomic_write_json(manifest_path, manifest)

        visual_keys = select_visual_evidence_keys(
            manifest,
            summaries,
            new_only=args.transcribe_new_only,
            only_shortcodes=only_shortcodes,
        )
        visual_enrichment = enrich_visual_evidence(root, manifest, visual_keys)
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
            "visual_enrichment": visual_enrichment,
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