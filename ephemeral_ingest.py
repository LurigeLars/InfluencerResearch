from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from story_capture_readiness import wait_for_story_media_ready
from transcription_backend import (
    DEFAULT_GEMINI_VISUAL_MODEL,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_VISUAL_MODEL,
    OLLAMA_VISUAL_TIMEOUT_SECONDS,
    OLLAMA_VISUAL_MAX_CHARS,
    OLLAMA_VISUAL_NUM_CTX,
    OLLAMA_VISUAL_CONTRACT,
    STORY_GEMINI_HTTP_TIMEOUT_MS,
    extract_image_evidence_gemini,
    extract_image_evidence_ollama,
    gemini_error_metadata,
    gemini_retry_after_seconds,
    safe_gemini_error,
    transcribe_video,
)


APP_VERSION = "0.5.2"
STORY_URL_RE = re.compile(r"/stories/(?P<user>[^/]+)/(?P<id>\d+)/?")
HIGHLIGHT_URL_RE = re.compile(r"/stories/highlights/(?P<id>\d+)/?")
STRICT_STORY_ROOT_PATH_RE = re.compile(r"^/stories/[A-Za-z0-9._-]{1,64}/?$")
STRICT_STORY_PATH_RE = re.compile(r"^/stories/[A-Za-z0-9._-]{1,64}/\d+/?$")
STRICT_HIGHLIGHT_PATH_RE = re.compile(r"^/stories/highlights/\d+/?$")
MAX_STORY_VISUAL_ENRICHMENTS_PER_RUN = 2
MAX_STORY_OLLAMA_ENRICHMENTS_PER_RUN = 2
STORY_GEMINI_RATE_LIMIT_COOLDOWN_SECONDS = 300
STORY_GEMINI_PROVIDER_ERROR_COOLDOWN_SECONDS = 60
STORY_GEMINI_MAX_ADAPTIVE_COOLDOWN_SECONDS = 900
STORY_GEMINI_BACKOFF_EXPONENT_CAP = 4
STORY_OCR_TIMEOUT_SECONDS = 20
STORY_OCR_LANGUAGES = "eng+swe"
STORY_OCR_MIN_WORDS = 8
STORY_OCR_MIN_CHARS = 48
STORY_OCR_MIN_MEANINGFUL_RATIO = 0.60
STORY_OCR_MAX_NOISE_RATIO = 0.30
STORY_OCR_MAX_FRAGMENTED_LINE_RATIO = 0.40


def canonical_instagram_ephemeral_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme != "https" or not (host == "instagram.com" or host.endswith(".instagram.com")):
        raise ValueError("Invalid Instagram story/highlight host")
    if not (
        STRICT_STORY_ROOT_PATH_RE.fullmatch(parsed.path)
        or STRICT_STORY_PATH_RE.fullmatch(parsed.path)
        or STRICT_HIGHLIGHT_PATH_RE.fullmatch(parsed.path)
    ):
        raise ValueError("Invalid Instagram story/highlight path")
    return f"https://www.instagram.com{parsed.path}"


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


def _gemini_health_path(root: Path) -> Path:
    return root / "state" / "provider_health.json"


def story_gemini_adaptive_cooldown_seconds(
    health: dict,
    *,
    base_seconds: int,
    provider_retry_seconds: int | None = None,
) -> tuple[int, int]:
    """Return cooldown seconds and next transient-failure streak.

    Explicit provider Retry-After wins. Otherwise the fallback cooldown grows
    exponentially and is capped so repeated 5xx failures do not get hammered
    every minute forever.
    """
    streak = max(
        0,
        int((health or {}).get("consecutive_transient_failures") or 0),
    ) + 1
    if provider_retry_seconds is not None:
        return max(1, int(provider_retry_seconds)), streak

    exponent = min(
        STORY_GEMINI_BACKOFF_EXPONENT_CAP,
        max(0, streak - 1),
    )
    cooldown = max(1, int(base_seconds)) * (2 ** exponent)
    return min(STORY_GEMINI_MAX_ADAPTIVE_COOLDOWN_SECONDS, cooldown), streak


def get_gemini_provider_health(root: Path) -> dict:
    state = load_json(_gemini_health_path(root), {"schema_version": 1, "gemini": {}})
    gemini = state.get("gemini") if isinstance(state, dict) else {}
    if not isinstance(gemini, dict):
        gemini = {}
    keys = (
        "signal_scope",
        "last_success_at",
        "last_error_at",
        "last_429_at",
        "cooldown_until",
        "last_code",
        "last_status",
        "retry_after_source",
        "story_visual_calls",
        "story_visual_successes",
        "story_visual_deferred",
        "consecutive_transient_failures",
        "last_cooldown_seconds",
    )
    return {key: gemini.get(key) for key in keys if key in gemini}


def update_gemini_provider_health(
    root: Path,
    *,
    calls: int = 0,
    successes: int = 0,
    deferred: int = 0,
    error_meta: dict | None = None,
    cooldown_until: datetime | None = None,
    retry_after_source: str | None = None,
    transient_failure: bool = False,
    cooldown_seconds: int | None = None,
) -> dict:
    path = _gemini_health_path(root)
    state = load_json(path, {"schema_version": 1, "gemini": {}})
    if not isinstance(state, dict):
        state = {"schema_version": 1, "gemini": {}}
    gemini = state.get("gemini")
    if not isinstance(gemini, dict):
        gemini = {}

    gemini["signal_scope"] = "STORY_VISUAL"
    gemini["story_visual_calls"] = int(gemini.get("story_visual_calls") or 0) + max(0, int(calls))
    gemini["story_visual_successes"] = int(gemini.get("story_visual_successes") or 0) + max(0, int(successes))
    gemini["story_visual_deferred"] = int(gemini.get("story_visual_deferred") or 0) + max(0, int(deferred))

    if successes:
        gemini["last_success_at"] = utc_now()
        gemini["consecutive_transient_failures"] = 0
        gemini["last_cooldown_seconds"] = 0
        existing_cooldown = _parse_retry_after(gemini.get("cooldown_until"))
        if existing_cooldown is None or existing_cooldown <= datetime.now(timezone.utc):
            gemini["cooldown_until"] = None
            gemini["retry_after_source"] = None

    if error_meta:
        now_text = utc_now()
        if transient_failure:
            gemini["consecutive_transient_failures"] = (
                int(gemini.get("consecutive_transient_failures") or 0) + 1
            )
        if cooldown_seconds is not None:
            gemini["last_cooldown_seconds"] = max(0, int(cooldown_seconds))
        gemini["last_error_at"] = now_text
        gemini["last_code"] = error_meta.get("code")
        gemini["last_status"] = error_meta.get("status")
        if error_meta.get("code") == 429:
            gemini["last_429_at"] = now_text
        if cooldown_until is not None:
            gemini["cooldown_until"] = cooldown_until.isoformat()
            gemini["retry_after_source"] = retry_after_source

    state["schema_version"] = 1
    state["gemini"] = gemini
    atomic_write_json(path, state)
    return get_gemini_provider_health(root)


def initial_story_gemini_circuit(root: Path) -> dict:
    health = get_gemini_provider_health(root)
    retry_at = _parse_retry_after(health.get("cooldown_until"))
    now = datetime.now(timezone.utc)
    if retry_at is not None and retry_at > now:
        return {
            "open": True,
            "reason": (
                "PROVIDER_RATE_LIMIT"
                if health.get("last_code") == 429
                else "PROVIDER_UNAVAILABLE"
            ),
            "retry_after": retry_at.isoformat(),
            "retry_after_source": health.get("retry_after_source") or "PERSISTED_COOLDOWN",
        }
    return {
        "open": False,
        "reason": None,
        "retry_after": None,
        "retry_after_source": None,
    }


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
    if "sessionid" not in {c.get("name", "") for c in cookies}:
        raise RuntimeError(
            "Instagram session is not authenticated. Run scripts\\authenticate_instagram.ps1 "
            "and then scripts\\runtime.ps1 -Action ImportInstagramAuth."
        )


def write_netscape_cookiefile(context, path: Path) -> None:
    cookies = context.cookies()
    lines = [
        "# Netscape HTTP Cookie File",
        "# Temporary InfluencerResearch cookie export. Never copy to Drive.",
    ]
    for c in cookies:
        domain = str(c.get("domain", ""))
        name = str(c.get("name", ""))
        if not domain or not name:
            continue
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            str(c.get("path") or "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(0 if float(c.get("expires") or 0) <= 0 else int(float(c.get("expires") or 0))),
            name,
            str(c.get("value") or ""),
        ]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def normalize_label(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def normalize_creator_handle(value: str) -> str:
    handle = str(value or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
        raise ValueError("Invalid Instagram creator handle")
    if handle in {".", ".."} or handle.endswith("."):
        raise ValueError("Unsafe Instagram creator path segment")
    if re.fullmatch(r"(?i:(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?)", handle):
        raise ValueError("Reserved Windows creator path segment")
    return handle


def discover_highlight_url(page, creator: str, label: str) -> tuple[str, list[dict]]:
    profile_url = f"https://www.instagram.com/{creator}/"
    page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)

    discovered = page.locator('a[href*="/stories/highlights/"]').evaluate_all(
        """els => els.map(a => ({
            href: a.href,
            text: (a.innerText || a.textContent || '').trim(),
            aria: (a.getAttribute('aria-label') || '').trim(),
            title: (a.getAttribute('title') || '').trim()
        }))"""
    )

    wanted = normalize_label(label)
    for row in discovered:
        hay = " ".join([
            str(row.get("text") or ""),
            str(row.get("aria") or ""),
            str(row.get("title") or ""),
        ])
        if wanted and wanted in normalize_label(hay):
            return str(row["href"]), discovered

    # Fallback: find the visible label and walk to its closest anchor.
    with contextlib.suppress(Exception):
        loc = page.get_by_text(label, exact=True).first
        if loc.count():
            href = loc.evaluate(
                """el => {
                    const a = el.closest('a') || el.parentElement?.closest('a');
                    return a ? a.href : null;
                }"""
            )
            if href and "/stories/highlights/" in href:
                return str(href), discovered

    raise RuntimeError(
        f"Could not resolve highlight label {label!r} on @{creator}. "
        f"Discovered highlight links: {json.dumps(discovered, ensure_ascii=False)[:3000]}"
    )


def _normalized_media_identity_path(value: str | None) -> str | None:
    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.path:
        return None
    return parsed.path


def visible_story_media_url(page) -> str | None:
    """Return the largest visible Story media URL without fetching it."""
    try:
        return page.evaluate(
            """() => {
                const rows = [];
                for (const el of document.querySelectorAll('video, img')) {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    if (
                        rect.width < 180 || rect.height < 180 ||
                        style.display === 'none' || style.visibility === 'hidden' ||
                        Number(style.opacity || '1') === 0
                    ) continue;
                    const values = [];
                    if (el.currentSrc) values.push(el.currentSrc);
                    if (el.src) values.push(el.src);
                    if (el.poster) values.push(el.poster);
                    for (const child of el.querySelectorAll?.('source[src]') || []) {
                        values.push(child.src);
                    }
                    const url = values.find(v => /^https?:\/\//i.test(String(v || '')));
                    if (!url) continue;
                    rows.push({url, area: rect.width * rect.height});
                }
                rows.sort((a, b) => b.area - a.area);
                return rows.length ? rows[0].url : null;
            }"""
        )
    except Exception:
        return None


def _story_navigation_identity(page) -> str | None:
    """Return a stable token for the currently visible Story frame."""
    current_url = str(getattr(page, "url", "") or "")
    match = STORY_URL_RE.search(current_url)
    story_id = match.group("id") if match else None
    media_path = _normalized_media_identity_path(visible_story_media_url(page))
    if not story_id and not media_path:
        return None
    return f"story:{story_id or ''}|media:{media_path or ''}"


def wait_for_story_advance(
    page,
    previous_identity: str | None,
    *,
    max_wait_ms: int = 1000,
    poll_ms: int = 100,
) -> dict[str, Any]:
    """Wait adaptively for the Story viewer to move to the next frame.

    The old path always slept 1000 ms after ArrowRight and another 900 ms at the
    start of the next iteration. This helper keeps the same effective fallback
    budget: if no change is observed within 1000 ms, the existing 900 ms pre-frame
    wait still runs on the next loop.
    """
    started = time.perf_counter()
    attempts = 0
    max_wait_ms = max(0, int(max_wait_ms))
    poll_ms = max(25, int(poll_ms))

    while True:
        attempts += 1
        current_url = str(getattr(page, "url", "") or "")
        if "/stories/" not in current_url:
            return {
                "changed": True,
                "exited": True,
                "timed_out": False,
                "identity": None,
                "attempts": attempts,
                "wait_ms": round((time.perf_counter() - started) * 1000, 1),
            }

        current_identity = _story_navigation_identity(page)
        if current_identity and (
            previous_identity is None
            or current_identity != previous_identity
        ):
            return {
                "changed": True,
                "exited": False,
                "timed_out": False,
                "identity": current_identity,
                "attempts": attempts,
                "wait_ms": round((time.perf_counter() - started) * 1000, 1),
            }

        elapsed_ms = (time.perf_counter() - started) * 1000
        remaining_ms = max_wait_ms - elapsed_ms
        if remaining_ms <= 0:
            return {
                "changed": False,
                "exited": False,
                "timed_out": True,
                "identity": current_identity,
                "attempts": attempts,
                "wait_ms": round((time.perf_counter() - started) * 1000, 1),
            }

        page.wait_for_timeout(min(poll_ms, max(1, int(remaining_ms))))


def extract_story_identity(
    url: str,
    screenshot_bytes: bytes,
    media_url: str | None = None,
) -> tuple[str | None, str | None, str]:
    # IMPORTANT: check Highlight before Story. A highlight URL also matches the generic
    # /stories/<user>/<id>/ pattern with user="highlights", which previously collapsed
    # every highlight frame to the same key.
    m = HIGHLIGHT_URL_RE.search(url)
    if m:
        # Highlight child IDs are not exposed in the route. Keep the legacy visual
        # identity here; the unstable-root bug only concerns ordinary Stories.
        h = hashlib.sha256(screenshot_bytes).hexdigest()[:24]
        return f"highlightframe-{h}", None, "HIGHLIGHT_SCREENSHOT_HASH"

    m = STORY_URL_RE.search(url)
    if m:
        return m.group("id"), m.group("id"), "STORY_URL_ID"

    # Instagram sometimes keeps the browser on /stories/<creator>/ while a concrete
    # Story is visible. Hash the media *path* rather than the whole screenshot:
    # query tokens can rotate, while the CDN path identifies the media asset. Full
    # screenshot hashes are not stable because Story progress/timer UI changes.
    media_path = _normalized_media_identity_path(media_url)
    if media_path:
        h = hashlib.sha256(media_path.encode("utf-8")).hexdigest()[:24]
        return f"media-{h}", None, "VISIBLE_MEDIA_URL_PATH"

    # Fail closed instead of inventing a new Story on every scan from volatile UI.
    return None, None, "UNRESOLVED_STORY_ROOT"


def invalidate_legacy_unstable_story_evidence(manifest: dict) -> int:
    invalidated = 0
    for item in (manifest.get("items") or {}).values():
        if not isinstance(item, dict):
            continue
        if str(item.get("source_type") or "").upper() != "STORY":
            continue
        if item.get("story_id"):
            continue
        evidence_id = str(item.get("evidence_id") or "")
        if not evidence_id.startswith("frame-"):
            continue
        try:
            parsed = urlsplit(str(item.get("source_url") or ""))
        except Exception:
            continue
        if not STRICT_STORY_ROOT_PATH_RE.fullmatch(parsed.path):
            continue
        if str(item.get("research_status") or "").upper() == "INVALID":
            continue
        item["research_status"] = "INVALID"
        item["invalid_reason"] = "LEGACY_UNSTABLE_ROOT_STORY_IDENTITY"
        item["invalidated_at"] = utc_now()
        invalidated += 1
    return invalidated


def backfill_story_identity_metadata(
    item: dict,
    *,
    story_id: str | None,
    identity_basis: str,
    media_identity_path: str | None,
    source_url: str,
) -> bool:
    """Add newly observable identity metadata without rewriting captured evidence."""
    changed = False
    updates = {
        "story_id": story_id,
        "story_identity_basis": identity_basis,
        "media_identity_path": media_identity_path,
    }
    for field, value in updates.items():
        if value is None:
            continue
        if item.get(field) in {None, ""}:
            item[field] = value
            changed = True

    # Prefer a concrete numeric Story URL over an older root URL, but preserve
    # first-observed timestamps/screenshots and all analysis evidence.
    if story_id and str(item.get("source_url") or "") != source_url:
        item["source_url"] = source_url
        changed = True

    if changed:
        item["identity_metadata_updated_at"] = utc_now()
    return changed


def retire_root_media_aliases_for_numeric_story(
    manifest: dict,
    *,
    creator: str,
    source_type: str,
    story_id: str | None,
    media_identity_path: str | None,
) -> list[str]:
    """Retire root-URL media aliases once Instagram exposes the numeric Story id."""
    if not story_id or not media_identity_path or source_type != "STORY":
        return []

    retired: list[str] = []
    canonical_key = f"{source_type}:{creator}:{story_id}"
    for key, item in (manifest.get("items") or {}).items():
        if key == canonical_key or not isinstance(item, dict):
            continue
        if str(item.get("source_type") or "").upper() != "STORY":
            continue
        if str(item.get("creator") or "").casefold() != creator.casefold():
            continue
        if item.get("story_id"):
            continue
        if str(item.get("media_identity_path") or "") != media_identity_path:
            continue
        if str(item.get("research_status") or "").upper() == "INVALID":
            continue

        item["research_status"] = "INVALID"
        item["invalid_reason"] = "SUPERSEDED_BY_NUMERIC_STORY_ID"
        item["superseded_by_evidence_id"] = story_id
        item["superseded_at"] = utc_now()
        retired.append(key)
    return retired


def safe_body_text(page, max_chars: int = 6000) -> str:
    try:
        text = page.locator("body").inner_text(timeout=5000)
        return text[:max_chars]
    except Exception:
        return ""


def instagram_story_error_present(page) -> bool:
    body = safe_body_text(page, max_chars=3000)
    needles = [
        "Sorry, something went wrong.",
        "We're working on getting this fixed as soon as we can.",
    ]
    return any(n in body for n in needles)


def invalidate_legacy_error_evidence(manifest: dict) -> int:
    invalidated = 0
    needles = [
        "Sorry, something went wrong.",
        "We're working on getting this fixed as soon as we can.",
    ]
    for item in manifest.get("items", {}).values():
        body = str(item.get("browser_text") or "")
        if any(n in body for n in needles):
            if item.get("research_status") != "INVALID":
                item["research_status"] = "INVALID"
                item["invalid_reason"] = "INSTAGRAM_ERROR_PAGE_CAPTURED"
                item["invalidated_at"] = utc_now()
                invalidated += 1
    return invalidated


def open_highlight_from_profile(page, source_url: str) -> None:
    """
    Open the highlight by clicking its profile-page anchor.
    Direct navigation to /stories/highlights/<id>/ can produce Instagram's
    generic error page even when the same highlight opens correctly via the UI.
    """
    m = HIGHLIGHT_URL_RE.search(source_url)
    if not m:
        raise RuntimeError(f"Could not parse highlight id from {source_url}")
    hid = m.group("id")
    fragment = f"/stories/highlights/{hid}/"

    loc = page.locator(f'a[href*="{fragment}"]').first
    if not loc.count():
        raise RuntimeError(f"Highlight anchor disappeared before click: {fragment}")

    loc.scroll_into_view_if_needed()
    loc.click(timeout=10000)
    page.wait_for_timeout(2500)

    if instagram_story_error_present(page):
        raise RuntimeError("Instagram returned its generic error page after clicking the highlight.")


def story_view_confirmation_present(page) -> bool:
    labels = ["Visa händelse", "View story"]
    for label in labels:
        with contextlib.suppress(Exception):
            button = page.get_by_role("button", name=label, exact=True)
            if button.count() and button.first.is_visible():
                return True
        with contextlib.suppress(Exception):
            text_loc = page.get_by_text(label, exact=True)
            if text_loc.count() and text_loc.first.is_visible():
                return True
    return False


def invalidate_legacy_confirmation_evidence(manifest: dict) -> int:
    invalidated = 0
    needles = [
        "Visa händelse",
        "View story",
    ]
    legacy_account_prompt = re.compile(r"Visa som [^\r\n?]{1,80}\?")
    for item in manifest.get("items", {}).values():
        body = str(item.get("browser_text") or "")
        if legacy_account_prompt.search(body) or any(n in body for n in needles):
            if item.get("research_status") != "INVALID":
                item["research_status"] = "INVALID"
                item["invalid_reason"] = "VIEW_CONFIRMATION_OVERLAY_CAPTURED"
                item["invalidated_at"] = utc_now()
                invalidated += 1
    return invalidated


def dismiss_story_view_confirmation(page) -> bool:
    """
    Instagram may show a confirmation overlay before a Story/Highlight is revealed,
    e.g. Swedish: "Visa händelse" or English: "View story".
    Click it before screenshots/navigation so the overlay is never stored as evidence.
    """
    labels = [
        "Visa händelse",
        "View story",
    ]
    for label in labels:
        with contextlib.suppress(Exception):
            button = page.get_by_role("button", name=label, exact=True)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=5000)
                page.wait_for_timeout(1200)
                return True

        with contextlib.suppress(Exception):
            text_loc = page.get_by_text(label, exact=True)
            if text_loc.count() and text_loc.first.is_visible():
                text_loc.first.click(timeout=5000)
                page.wait_for_timeout(1200)
                return True

    return False


def capture_story_frames(
    page,
    root: Path,
    manifest: dict,
    creator: str,
    start_url: str,
    source_type: str,
    highlight_label: str | None,
    max_items: int,
    preopened: bool = False,
) -> dict:
    if not preopened:
        page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

    if instagram_story_error_present(page):
        return {
            "captured_new": 0,
            "seen_existing": 0,
            "visited_frames": 0,
            "view_confirmation_was_present": False,
            "view_confirmation_dismissed": False,
            "reason": "INSTAGRAM_ERROR_PAGE",
        }

    confirmation_was_present = story_view_confirmation_present(page)
    confirmation_dismissed = dismiss_story_view_confirmation(page)

    if instagram_story_error_present(page):
        return {
            "captured_new": 0,
            "seen_existing": 0,
            "visited_frames": 0,
            "view_confirmation_was_present": confirmation_was_present,
            "view_confirmation_dismissed": confirmation_dismissed,
            "reason": "INSTAGRAM_ERROR_PAGE_AFTER_CONFIRMATION",
        }

    if story_view_confirmation_present(page):
        return {
            "captured_new": 0,
            "seen_existing": 0,
            "visited_frames": 0,
            "view_confirmation_was_present": confirmation_was_present,
            "view_confirmation_dismissed": False,
            "reason": "BLOCKED_VIEW_CONFIRMATION",
        }

    if "/stories/" not in page.url:
        return {
            "captured_new": 0,
            "seen_existing": 0,
            "visited_frames": 0,
            "view_confirmation_was_present": confirmation_was_present,
            "view_confirmation_dismissed": confirmation_dismissed,
            "reason": "NO_ACTIVE_STORY_OR_STORY_VIEW_REDIRECTED",
        }

    source_dir = "stories" if source_type == "STORY" else "highlights"
    shot_dir = root / "output" / creator / source_dir / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)

    captured_new = 0
    seen_existing = 0
    unstable_identity_skipped = 0
    identity_aliases_retired = 0
    visited_item_keys: list[str] = []
    visited = 0
    consecutive_unchanged = 0
    previous_marker = None
    stop_reason = "OK"
    story_frame_ready = False
    story_advance_wait_ms = 0.0
    story_advance_attempts = 0
    story_advance_ready_count = 0
    story_advance_timeout_count = 0
    story_capture_ready_wait_ms = 0.0
    story_capture_ready_attempts = 0
    story_capture_ready_count = 0
    story_capture_not_ready_count = 0

    for _ in range(max_items):
        if "/stories/" not in page.url:
            stop_reason = "ENDED_OR_EXITED_STORY_VIEW"
            break

        if source_type != "STORY" or not story_frame_ready:
            page.wait_for_timeout(900)
        story_frame_ready = False

        if instagram_story_error_present(page):
            stop_reason = "INSTAGRAM_ERROR_PAGE_DURING_TRAVERSAL"
            break

        if source_type == "STORY":
            capture_ready = wait_for_story_media_ready(
                page,
                max_wait_ms=4000,
                poll_ms=100,
            )
            story_capture_ready_wait_ms += float(capture_ready.get("wait_ms") or 0.0)
            story_capture_ready_attempts += int(capture_ready.get("attempts") or 0)
            if capture_ready.get("ready"):
                story_capture_ready_count += 1
            else:
                story_capture_not_ready_count += 1
                if capture_ready.get("exited"):
                    stop_reason = "ENDED_OR_EXITED_STORY_VIEW"
                    break
                previous_identity = _story_navigation_identity(page)
                page.keyboard.press("ArrowRight")
                advance = wait_for_story_advance(
                    page,
                    previous_identity,
                    max_wait_ms=1000,
                    poll_ms=100,
                )
                story_advance_wait_ms += float(advance.get("wait_ms") or 0.0)
                story_advance_attempts += int(advance.get("attempts") or 0)
                if advance.get("changed"):
                    story_advance_ready_count += 1
                    story_frame_ready = True
                else:
                    story_advance_timeout_count += 1
                continue

        screenshot = page.screenshot(full_page=False)
        media_url = visible_story_media_url(page) if source_type == "STORY" else None
        evidence_id, story_id, identity_basis = extract_story_identity(
            page.url,
            screenshot,
            media_url,
        )
        media_identity_path = _normalized_media_identity_path(media_url)
        navigation_identity = (
            f"story:{story_id or ''}|media:{media_identity_path or ''}"
            if story_id or media_identity_path
            else None
        )
        identity_aliases_retired += len(
            retire_root_media_aliases_for_numeric_story(
                manifest,
                creator=creator,
                source_type=source_type,
                story_id=story_id,
                media_identity_path=media_identity_path,
            )
        )
        content_hash = hashlib.sha256(screenshot).hexdigest()
        marker = f"{page.url}|{content_hash}"
        visited += 1

        if evidence_id is None:
            unstable_identity_skipped += 1
        else:
            key = f"{source_type}:{creator}:{evidence_id}"
            if key not in visited_item_keys:
                visited_item_keys.append(key)
            if key in manifest["items"]:
                existing_item = manifest["items"][key]
                if isinstance(existing_item, dict):
                    backfill_story_identity_metadata(
                        existing_item,
                        story_id=story_id,
                        identity_basis=identity_basis,
                        media_identity_path=media_identity_path,
                        source_url=page.url,
                    )
                seen_existing += 1
            else:
                ext = ".png"
                shot_path = shot_dir / f"{evidence_id}{ext}"
                shot_path.write_bytes(screenshot)
                manifest["items"][key] = {
                    "schema_version": 1,
                    "source_type": source_type,
                    "creator": creator,
                    "highlight_label": highlight_label,
                    "evidence_id": evidence_id,
                    "story_id": story_id,
                    "story_identity_basis": identity_basis,
                    "media_identity_path": media_identity_path,
                    "source_url": page.url,
                    "observed_at": utc_now(),
                    "screenshot_file": str(shot_path.relative_to(root)),
                    "screenshot_sha256": content_hash,
                    "browser_text": safe_body_text(page),
                    "research_status": "PENDING",
                    "visual_review_required": True,
                }
                captured_new += 1

        if marker == previous_marker:
            consecutive_unchanged += 1
        else:
            consecutive_unchanged = 0
            previous_marker = marker

        if consecutive_unchanged >= 2:
            stop_reason = "NAVIGATION_STALLED"
            break

        # Instagram's story viewer normally responds to right-arrow navigation.
        page.keyboard.press("ArrowRight")
        if source_type == "STORY":
            advance = wait_for_story_advance(
                page,
                navigation_identity,
                max_wait_ms=1000,
                poll_ms=100,
            )
            story_advance_wait_ms += float(advance.get("wait_ms") or 0.0)
            story_advance_attempts += int(advance.get("attempts") or 0)
            if advance.get("changed"):
                story_advance_ready_count += 1
                story_frame_ready = True
            else:
                story_advance_timeout_count += 1
        else:
            page.wait_for_timeout(1000)

    return {
        "captured_new": captured_new,
        "seen_existing": seen_existing,
        "unstable_identity_skipped": unstable_identity_skipped,
        "identity_aliases_retired": identity_aliases_retired,
        "visited_item_keys": visited_item_keys,
        "visited_frames": visited,
        "story_advance_wait_ms": round(story_advance_wait_ms, 1),
        "story_advance_attempts": story_advance_attempts,
        "story_advance_ready_count": story_advance_ready_count,
        "story_advance_timeout_count": story_advance_timeout_count,
        "story_capture_ready_wait_ms": round(story_capture_ready_wait_ms, 1),
        "story_capture_ready_attempts": story_capture_ready_attempts,
        "story_capture_ready_count": story_capture_ready_count,
        "story_capture_not_ready_count": story_capture_not_ready_count,
        "view_confirmation_was_present": confirmation_was_present,
        "view_confirmation_dismissed": confirmation_dismissed,
        "reason": stop_reason,
    }


def _parse_retry_after(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _defer_story_visual(
    item: dict,
    *,
    reason: str,
    retry_after: datetime | None,
    safe_error: str | None = None,
) -> None:
    item["visual_description_status"] = "DEFERRED"
    item["visual_description_deferred_reason"] = reason
    item["visual_description_generated_at"] = utc_now()
    if retry_after is not None:
        item["visual_description_retry_after"] = retry_after.isoformat()
    else:
        item.pop("visual_description_retry_after", None)
    if safe_error:
        item["visual_description_error"] = safe_error
    else:
        item.pop("visual_description_error", None)


def _story_evidence_text_sufficient(text: str) -> bool:
    text = str(text or "").strip()
    return len(text.split()) >= STORY_OCR_MIN_WORDS or len(text) >= STORY_OCR_MIN_CHARS


def _story_ocr_fragmented_line_ratio(text: str) -> float:
    """Estimate page-level OCR fragmentation without requiring a language dictionary."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return 1.0

    valid_short_words = {"a", "i", "ai", "us", "uk", "eu", "v", "ma"}
    fragmented = 0
    for line in lines:
        tokens = re.findall(r"\S+", line)
        alpha_tokens: list[str] = []
        suspicious_short = 0
        for token in tokens:
            letters = re.sub(r"[^A-Za-zÅÄÖåäö]", "", token)
            if not letters:
                continue
            alpha_tokens.append(letters)
            if len(letters) <= 2 and letters.casefold() not in valid_short_words:
                suspicious_short += 1

        short_ratio = suspicious_short / max(1, len(alpha_tokens))
        unusual_symbols = sum(
            1
            for char in line
            if not (
                char.isalnum()
                or char.isspace()
                or char in ".,:%+-/@()'’"
            )
        )
        symbol_ratio = unusual_symbols / max(1, len(line))
        if (
            (len(alpha_tokens) >= 2 and short_ratio >= 0.40)
            or symbol_ratio > 0.15
        ):
            fragmented += 1

    return fragmented / len(lines)


def _story_ocr_text_sufficient(text: str) -> bool:
    """Require enough OCR text and reject symbol-heavy / fragmented output."""
    text = str(text or "").strip()
    if not _story_evidence_text_sufficient(text):
        return False

    tokens = re.findall(r"\S+", text)
    if not tokens:
        return False
    meaningful = [
        token for token in tokens
        if re.search(r"[A-Za-zÅÄÖåäö]{2,}", token) or re.search(r"\d", token)
    ]
    noise = [
        token for token in tokens
        if not re.search(r"[A-Za-zÅÄÖåäö0-9]", token)
        or (len(token) == 1 and not token.isalnum())
    ]
    meaningful_ratio = len(meaningful) / len(tokens)
    noise_ratio = len(noise) / len(tokens)
    fragmented_line_ratio = _story_ocr_fragmented_line_ratio(text)
    return (
        meaningful_ratio >= STORY_OCR_MIN_MEANINGFUL_RATIO
        and noise_ratio <= STORY_OCR_MAX_NOISE_RATIO
        and fragmented_line_ratio <= STORY_OCR_MAX_FRAGMENTED_LINE_RATIO
    )


def _story_local_ocr_needs_upgrade(item: dict) -> bool:
    text = str(item.get("visual_description") or "").strip()
    return (
        str(item.get("visual_description_status") or "").upper() == "DONE"
        and str(item.get("visual_description_source") or "").upper() == "LOCAL_OCR"
        and bool(text)
        and not _story_ocr_text_sufficient(text)
    )


def _story_ollama_needs_upgrade(item: dict) -> bool:
    text = str(item.get("visual_description") or "").strip()
    return (
        str(item.get("visual_description_status") or "").upper() == "DONE"
        and str(item.get("visual_description_source") or "").upper()
        == "OLLAMA_STORY_SCREENSHOT_EVIDENCE"
        and bool(text)
        and (
            str(item.get("visual_description_contract") or "") != OLLAMA_VISUAL_CONTRACT
            or "NO_MEANINGFUL_VISUAL_EVIDENCE" in text
            or len(text) > OLLAMA_VISUAL_MAX_CHARS
        )
    )



def _story_visual_needs_enrichment(item: dict) -> bool:
    """Return true for missing, low-quality, or legacy local Story evidence."""
    text = str(item.get("visual_description") or "").strip()
    status = str(item.get("visual_description_status") or "").upper()
    if status != "DONE" or not text:
        return True
    return _story_local_ocr_needs_upgrade(item) or _story_ollama_needs_upgrade(item)



OLLAMA_INSUFFICIENT_CACHE_STATUS = "INSUFFICIENT"


OCR_INSUFFICIENT_CACHE_STATUS = "INSUFFICIENT"


def _story_ocr_attempt_fingerprint(screenshot_path: Path) -> str:
    """Fingerprint exact Story pixels plus the local OCR quality contract."""
    digest = hashlib.sha256()
    digest.update(b"story-ocr-insufficient-cache-v1\0")
    digest.update(STORY_OCR_LANGUAGES.encode("utf-8"))
    digest.update(b"\0psm=6\0")
    digest.update(str(STORY_OCR_MIN_WORDS).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(STORY_OCR_MIN_CHARS).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(STORY_OCR_MIN_MEANINGFUL_RATIO).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(STORY_OCR_MAX_NOISE_RATIO).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(STORY_OCR_MAX_FRAGMENTED_LINE_RATIO).encode("ascii"))
    digest.update(b"\0")
    digest.update(screenshot_path.read_bytes())
    return digest.hexdigest()


def _story_ocr_cached_insufficient(item: dict, fingerprint: str) -> bool:
    return (
        str(item.get("ocr_visual_attempt_status") or "").upper()
        == OCR_INSUFFICIENT_CACHE_STATUS
        and str(item.get("ocr_visual_attempt_fingerprint") or "") == fingerprint
    )


def _record_story_ocr_insufficient(
    item: dict,
    *,
    fingerprint: str,
    text: str,
) -> None:
    item["ocr_visual_attempt_status"] = OCR_INSUFFICIENT_CACHE_STATUS
    item["ocr_visual_attempt_fingerprint"] = fingerprint
    item["ocr_visual_attempt_model"] = STORY_OCR_LANGUAGES
    item["ocr_visual_attempt_psm"] = 6
    item["ocr_visual_attempt_text"] = str(text or "")
    item["ocr_visual_attempted_at"] = utc_now()


def _clear_story_ocr_insufficient(item: dict) -> None:
    for field in (
        "ocr_visual_attempt_status",
        "ocr_visual_attempt_fingerprint",
        "ocr_visual_attempt_model",
        "ocr_visual_attempt_psm",
        "ocr_visual_attempt_text",
        "ocr_visual_attempted_at",
    ):
        item.pop(field, None)


def _story_ollama_attempt_fingerprint(
    screenshot_path: Path,
    *,
    model: str,
    num_ctx: int,
    ocr_hint: str,
) -> str:
    """Fingerprint exact static Story evidence plus the local-model contract."""
    digest = hashlib.sha256()
    digest.update(b"story-ollama-insufficient-cache-v1\0")
    digest.update(str(model).encode("utf-8"))
    digest.update(b"\0")
    digest.update(OLLAMA_VISUAL_CONTRACT.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(max(2048, int(num_ctx))).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(ocr_hint or "").encode("utf-8"))
    digest.update(b"\0")
    digest.update(screenshot_path.read_bytes())
    return digest.hexdigest()


def _story_ollama_cached_insufficient(item: dict, fingerprint: str) -> bool:
    return (
        str(item.get("ollama_visual_attempt_status") or "").upper()
        == OLLAMA_INSUFFICIENT_CACHE_STATUS
        and str(item.get("ollama_visual_attempt_fingerprint") or "") == fingerprint
    )


def _record_story_ollama_insufficient(
    item: dict,
    *,
    fingerprint: str,
    model: str,
    num_ctx: int,
) -> None:
    item["ollama_visual_attempt_status"] = OLLAMA_INSUFFICIENT_CACHE_STATUS
    item["ollama_visual_attempt_fingerprint"] = fingerprint
    item["ollama_visual_attempt_model"] = model
    item["ollama_visual_attempt_contract"] = OLLAMA_VISUAL_CONTRACT
    item["ollama_visual_attempt_num_ctx"] = max(2048, int(num_ctx))
    item["ollama_visual_attempted_at"] = utc_now()


def _clear_story_ollama_insufficient(item: dict) -> None:
    for field in (
        "ollama_visual_attempt_status",
        "ollama_visual_attempt_fingerprint",
        "ollama_visual_attempt_model",
        "ollama_visual_attempt_contract",
        "ollama_visual_attempt_num_ctx",
        "ollama_visual_attempted_at",
    ):
        item.pop(field, None)


def extract_story_text_local_ocr(screenshot_path: Path) -> dict:
    """Extract visible Story text locally with bounded Tesseract OCR."""
    proc = subprocess.run(
        [
            "tesseract",
            str(screenshot_path),
            "stdout",
            "-l",
            STORY_OCR_LANGUAGES,
            "--psm",
            "6",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=STORY_OCR_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        stderr = str(proc.stderr or "").strip()[-500:]
        raise RuntimeError(f"Tesseract OCR failed rc={proc.returncode}: {stderr}")
    text = re.sub(r"[ \t]+", " ", str(proc.stdout or "")).strip()
    return {
        "text": text,
        "source": "LOCAL_OCR",
        "provider": "tesseract",
        "model": STORY_OCR_LANGUAGES,
    }


def enrich_story_visual_evidence(
    root: Path,
    manifest: dict,
    item_keys: list[str],
    *,
    max_attempts: int = MAX_STORY_VISUAL_ENRICHMENTS_PER_RUN,
    circuit_state: dict | None = None,
    ollama_budget_state: dict | None = None,
) -> dict:
    """Backfill readable multimodal evidence for visited Story screenshots.

    Provider 429/5xx failures are transient extraction state, not evidence that the
    Story itself lacks content. A provider-wide failure opens a per-run circuit
    breaker so the scan does not spend additional quota on requests that are
    expected to fail.
    """
    settings = load_json(root / "control" / "settings.json", {})
    tcfg = settings.get("transcription") or {}
    model = str(
        tcfg.get("gemini_visual_model", DEFAULT_GEMINI_VISUAL_MODEL)
        or DEFAULT_GEMINI_VISUAL_MODEL
    ).strip()
    gemini_story_timeout_ms = max(
        5_000,
        min(
            30_000,
            int(
                tcfg.get(
                    "gemini_story_visual_timeout_ms",
                    STORY_GEMINI_HTTP_TIMEOUT_MS,
                )
                or STORY_GEMINI_HTTP_TIMEOUT_MS
            ),
        ),
    )
    ollama_enabled = bool(tcfg.get("ollama_visual_enabled", True))
    ollama_model = str(
        tcfg.get("ollama_visual_model", DEFAULT_OLLAMA_VISUAL_MODEL)
        or DEFAULT_OLLAMA_VISUAL_MODEL
    ).strip()
    ollama_base_url = str(
        tcfg.get("ollama_base_url", DEFAULT_OLLAMA_BASE_URL)
        or DEFAULT_OLLAMA_BASE_URL
    ).strip()
    ollama_timeout_seconds = int(
        tcfg.get("ollama_visual_timeout_seconds", OLLAMA_VISUAL_TIMEOUT_SECONDS)
        or OLLAMA_VISUAL_TIMEOUT_SECONDS
    )
    ollama_num_ctx = max(
        2048,
        int(tcfg.get("ollama_visual_num_ctx", OLLAMA_VISUAL_NUM_CTX) or OLLAMA_VISUAL_NUM_CTX),
    )
    max_ollama_attempts = max(
        0,
        int(tcfg.get("ollama_visual_max_attempts", MAX_STORY_OLLAMA_ENRICHMENTS_PER_RUN)),
    )
    attempted = 0
    completed = 0
    ocr_attempted = 0
    ocr_completed = 0
    ocr_insufficient = 0
    ocr_cached_insufficient = 0
    ocr_errors: list[str] = []
    ollama_attempted = 0
    ollama_completed = 0
    ollama_insufficient = 0
    ollama_cached_insufficient = 0
    ollama_errors: list[str] = []
    provider_events: list[str] = []
    ocr_duration_ms = 0.0
    ollama_duration_ms = 0.0
    gemini_duration_ms = 0.0
    skipped = 0
    deferred = 0
    provider_deferred = 0
    health_deferred_recorded = 0
    errors: list[str] = []
    changed = False
    if circuit_state is None:
        circuit_state = initial_story_gemini_circuit(root)
    if ollama_budget_state is None:
        ollama_budget_state = {"attempted": 0}
    ollama_budget_state.setdefault("attempted", 0)
    ollama_budget_state["limit"] = max_ollama_attempts

    circuit_reason = str(circuit_state.get("reason") or "") or None
    circuit_retry_after = _parse_retry_after(circuit_state.get("retry_after"))
    circuit_retry_after_source = str(circuit_state.get("retry_after_source") or "") or None
    now = datetime.now(timezone.utc)

    for key in item_keys:
        item = (manifest.get("items") or {}).get(key)
        if not isinstance(item, dict):
            continue
        if str(item.get("source_type") or "").upper() != "STORY":
            continue
        if str(item.get("research_status") or "").upper() == "INVALID":
            continue
        if not _story_visual_needs_enrichment(item):
            skipped += 1
            continue

        screenshot_rel = str(item.get("screenshot_file") or "").strip()
        screenshot_path = root / screenshot_rel if screenshot_rel else None
        if screenshot_path is None or not screenshot_path.is_file():
            errors.append(f"{key}: STORY_SCREENSHOT_MISSING")
            continue

        # Local OCR is the cheap first pass. Cache a grounded INSUFFICIENT
        # result for the exact screenshot + OCR quality contract so unchanged
        # Stories do not rerun deterministic Tesseract work on every scan.
        ocr_text = ""
        ocr_fingerprint = ""
        ocr_cache_hit = False
        try:
            ocr_fingerprint = _story_ocr_attempt_fingerprint(screenshot_path)
            ocr_cache_hit = _story_ocr_cached_insufficient(
                item,
                ocr_fingerprint,
            )
        except OSError as exc:
            ocr_errors.append(
                f"{key}: {type(exc).__name__}: OCR cache fingerprint failed"
            )

        if ocr_cache_hit:
            ocr_cached_insufficient += 1
            ocr_text = str(item.get("ocr_visual_attempt_text") or "").strip()
        else:
            ocr_attempted += 1
            ocr_clock = time.perf_counter()
            try:
                ocr_result = extract_story_text_local_ocr(screenshot_path)
                ocr_text = str(ocr_result.get("text") or "").strip()
                if _story_ocr_text_sufficient(ocr_text):
                    item["visual_description"] = ocr_text
                    item["visual_description_status"] = "DONE"
                    item["visual_description_source"] = ocr_result.get("source")
                    item["visual_description_provider"] = ocr_result.get("provider")
                    item["visual_description_model"] = ocr_result.get("model")
                    item.pop("visual_description_contract", None)
                    item["visual_description_generated_at"] = utc_now()
                    item.pop("visual_description_error", None)
                    item.pop("visual_description_deferred_reason", None)
                    item.pop("visual_description_retry_after", None)
                    _clear_story_ocr_insufficient(item)
                    _clear_story_ollama_insufficient(item)
                    changed = True
                    completed += 1
                    ocr_completed += 1
                    continue
                ocr_insufficient += 1
                if ocr_fingerprint:
                    _record_story_ocr_insufficient(
                        item,
                        fingerprint=ocr_fingerprint,
                        text=ocr_text,
                    )
                    changed = True
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                # OCR failure is non-fatal; richer local/remote fallbacks remain available.
                ocr_errors.append(f"{key}: {type(exc).__name__}: {exc}")
            finally:
                ocr_duration_ms += (time.perf_counter() - ocr_clock) * 1000

        # Ollama is the local multimodal second pass. Cache only a grounded
        # INSUFFICIENT result for this exact screenshot + OCR hint + model contract.
        # Transport/provider errors are deliberately never cached.
        ollama_fingerprint = ""
        ollama_cache_hit = False
        if ollama_enabled:
            try:
                ollama_fingerprint = _story_ollama_attempt_fingerprint(
                    screenshot_path,
                    model=ollama_model,
                    num_ctx=ollama_num_ctx,
                    ocr_hint=ocr_text,
                )
                ollama_cache_hit = _story_ollama_cached_insufficient(
                    item,
                    ollama_fingerprint,
                )
            except OSError as exc:
                ollama_errors.append(
                    f"{key}: {type(exc).__name__}: Ollama cache fingerprint failed"
                )

        if ollama_cache_hit:
            ollama_cached_insufficient += 1
        elif (
            ollama_enabled
            and int(ollama_budget_state.get("attempted") or 0) < max_ollama_attempts
        ):
            ollama_attempted += 1
            ollama_budget_state["attempted"] = int(
                ollama_budget_state.get("attempted") or 0
            ) + 1
            ollama_clock = time.perf_counter()
            try:
                ollama_result = extract_image_evidence_ollama(
                    screenshot_path,
                    model=ollama_model,
                    base_url=ollama_base_url,
                    timeout_seconds=ollama_timeout_seconds,
                    num_ctx=ollama_num_ctx,
                    ocr_hint=ocr_text or None,
                )
                ollama_text = str(ollama_result.get("text") or "").strip()
                if _story_evidence_text_sufficient(ollama_text):
                    item["visual_description"] = ollama_text
                    item["visual_description_status"] = "DONE"
                    item["visual_description_source"] = ollama_result.get("source")
                    item["visual_description_provider"] = ollama_result.get("provider")
                    item["visual_description_model"] = ollama_result.get("model")
                    item["visual_description_contract"] = ollama_result.get("contract")
                    item["visual_description_generated_at"] = utc_now()
                    item.pop("visual_description_error", None)
                    item.pop("visual_description_deferred_reason", None)
                    item.pop("visual_description_retry_after", None)
                    _clear_story_ocr_insufficient(item)
                    _clear_story_ollama_insufficient(item)
                    changed = True
                    completed += 1
                    ollama_completed += 1
                    continue
                ollama_insufficient += 1
                if ollama_fingerprint:
                    _record_story_ollama_insufficient(
                        item,
                        fingerprint=ollama_fingerprint,
                        model=ollama_model,
                        num_ctx=ollama_num_ctx,
                    )
                    changed = True
            except Exception as exc:
                # Local model failure must never block the Gemini fallback.
                ollama_errors.append(f"{key}: {type(exc).__name__}: {exc}")
            finally:
                ollama_duration_ms += (time.perf_counter() - ollama_clock) * 1000

        existing_retry_after = _parse_retry_after(item.get("visual_description_retry_after"))
        if (
            str(item.get("visual_description_status") or "").upper() == "DEFERRED"
            and existing_retry_after is not None
            and existing_retry_after > now
        ):
            deferred += 1
            continue

        if circuit_reason is not None:
            _defer_story_visual(
                item,
                reason=circuit_reason,
                retry_after=circuit_retry_after,
            )
            changed = True
            deferred += 1
            provider_deferred += 1
            continue

        if attempted >= max(0, int(max_attempts)):
            _defer_story_visual(
                item,
                reason="PER_RUN_BUDGET",
                retry_after=None,
            )
            changed = True
            deferred += 1
            continue

        attempted += 1
        gemini_clock = time.perf_counter()
        try:
            result = extract_image_evidence_gemini(
                screenshot_path,
                model=model,
                timeout_ms=gemini_story_timeout_ms,
            )
            text = str(result.get("text") or "").strip()
            item["visual_description"] = text
            item["visual_description_status"] = "DONE" if text else "INSUFFICIENT_CONTENT"
            item["visual_description_source"] = result.get("source")
            item["visual_description_provider"] = result.get("provider")
            item["visual_description_model"] = result.get("model")
            item.pop("visual_description_contract", None)
            item["visual_description_generated_at"] = utc_now()
            item.pop("visual_description_error", None)
            item.pop("visual_description_deferred_reason", None)
            item.pop("visual_description_retry_after", None)
            _clear_story_ocr_insufficient(item)
            _clear_story_ollama_insufficient(item)
            changed = True
            if text:
                completed += 1
            else:
                errors.append(f"{key}: NO_MEANINGFUL_VISUAL_EVIDENCE")
            update_gemini_provider_health(
                root,
                calls=1,
                successes=1,
            )
        except Exception as exc:
            safe_error = safe_gemini_error(exc, operation="visual evidence extraction")
            meta = gemini_error_metadata(exc)
            code = meta.get("code")
            transient = code is None or code == 429 or (isinstance(code, int) and code >= 500)

            if transient:
                provider_retry_seconds = gemini_retry_after_seconds(
                    exc,
                    now=datetime.now(timezone.utc),
                )
                if code == 429:
                    reason = "PROVIDER_RATE_LIMIT"
                    fallback_cooldown = STORY_GEMINI_RATE_LIMIT_COOLDOWN_SECONDS
                else:
                    reason = "PROVIDER_UNAVAILABLE"
                    fallback_cooldown = STORY_GEMINI_PROVIDER_ERROR_COOLDOWN_SECONDS
                cooldown, _ = story_gemini_adaptive_cooldown_seconds(
                    get_gemini_provider_health(root),
                    base_seconds=fallback_cooldown,
                    provider_retry_seconds=provider_retry_seconds,
                )
                retry_source = (
                    "PROVIDER_RETRY_AFTER"
                    if provider_retry_seconds is not None
                    else "ADAPTIVE_BACKOFF"
                )
                retry_after = datetime.now(timezone.utc) + timedelta(seconds=cooldown)
                _defer_story_visual(
                    item,
                    reason=reason,
                    retry_after=retry_after,
                    safe_error=safe_error,
                )
                circuit_reason = reason
                circuit_retry_after = retry_after
                circuit_retry_after_source = retry_source
                circuit_state.update({
                    "open": True,
                    "reason": reason,
                    "retry_after": retry_after.isoformat(),
                    "retry_after_source": retry_source,
                })
                provider_deferred += 1
                deferred += 1
                update_gemini_provider_health(
                    root,
                    calls=1,
                    deferred=1,
                    error_meta=meta,
                    cooldown_until=retry_after,
                    retry_after_source=retry_source,
                    transient_failure=True,
                    cooldown_seconds=cooldown,
                )
                health_deferred_recorded += 1
            else:
                item["visual_description_status"] = "ERROR"
                item["visual_description_error"] = safe_error
                item["visual_description_generated_at"] = utc_now()
                item.pop("visual_description_deferred_reason", None)
                item.pop("visual_description_retry_after", None)
                update_gemini_provider_health(
                    root,
                    calls=1,
                    error_meta=meta,
                )

            changed = True
            if transient:
                provider_events.append(f"{key}: {safe_error}")
            else:
                errors.append(f"{key}: {safe_error}")
        finally:
            gemini_duration_ms += (time.perf_counter() - gemini_clock) * 1000

    if provider_deferred > health_deferred_recorded:
        update_gemini_provider_health(
            root,
            deferred=provider_deferred - health_deferred_recorded,
        )

    return {
        "attempted": attempted,
        "completed": completed,
        "ollama_budget_attempted": int(ollama_budget_state.get("attempted") or 0),
        "ollama_budget_limit": max_ollama_attempts,
        "ocr_attempted": ocr_attempted,
        "ocr_completed": ocr_completed,
        "ocr_insufficient": ocr_insufficient,
        "ocr_cached_insufficient": ocr_cached_insufficient,
        "ocr_errors": ocr_errors,
        "ollama_attempted": ollama_attempted,
        "ollama_completed": ollama_completed,
        "ollama_insufficient": ollama_insufficient,
        "ollama_cached_insufficient": ollama_cached_insufficient,
        "ollama_errors": ollama_errors,
        "ollama_model": ollama_model,
        "ollama_max_attempts": max_ollama_attempts,
        "provider_events": provider_events,
        "timings": {
            "ocr_total_ms": round(ocr_duration_ms, 1),
            "ollama_total_ms": round(ollama_duration_ms, 1),
            "gemini_total_ms": round(gemini_duration_ms, 1),
        },
        "skipped": skipped,
        "deferred": deferred,
        "provider_deferred": provider_deferred,
        "max_attempts": max(0, int(max_attempts)),
        "errors": errors,
        "changed": changed,
        "model": model,
        "gemini_story_timeout_ms": gemini_story_timeout_ms,
        "provider_circuit_breaker": {
            "open": circuit_reason is not None,
            "reason": circuit_reason,
            "retry_after": (
                circuit_retry_after.isoformat() if circuit_retry_after is not None else None
            ),
            "retry_after_source": circuit_retry_after_source,
        },
        "provider_health": get_gemini_provider_health(root),
    }


def run_ytdlp(context, root: Path, creator: str, source_type: str, source_url: str) -> dict:
    source_url = canonical_instagram_ephemeral_url(source_url)
    source_dir = "stories" if source_type == "STORY" else "highlights"
    video_dir = root / "output" / creator / source_dir / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)

    fd, cookie_path_raw = tempfile.mkstemp(prefix="instagram_ephemeral_ytdlp_", suffix=".txt")
    os.close(fd)
    cookie_path = Path(cookie_path_raw)
    write_netscape_cookiefile(context, cookie_path)

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",
        "--cookies", str(cookie_path),
        "--no-progress",
        "--no-warnings",
        "--write-info-json",
        "--format", "b[ext=mp4]/b",
        "--output", str(video_dir / "%(id)s.%(ext)s"),
        "--print", "after_move:filepath",
        "--",
        source_url,
    ]
    try:
        # URL is strict-canonical Instagram and '--' terminates yt-dlp option parsing.

        # lgtm[py/command-line-injection]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            shell=False,
        )
    finally:
        with contextlib.suppress(OSError):
            cookie_path.unlink(missing_ok=True)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return {
            "ok": False,
            "returncode": result.returncode,
            "error": detail[-2000:],
            "files": [],
        }

    files = []
    for line in (result.stdout or "").splitlines():
        p = Path(line.strip())
        if p.exists():
            files.append(str(p.relative_to(root)))

    return {"ok": True, "returncode": 0, "files": files, "error": None}


def transcribe_downloaded_videos(root: Path, creator: str, source_type: str) -> dict:
    source_dir = "stories" if source_type == "STORY" else "highlights"
    video_dir = root / "output" / creator / source_dir / "videos"
    transcript_dir = root / "output" / creator / source_dir / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)

    video_files = sorted(video_dir.glob("*.mp4"))
    pending = [
        p for p in video_files
        if not (transcript_dir / f"{p.stem}.txt").exists()
    ]
    if not pending:
        return {"attempted": 0, "completed": 0, "errors": []}

    settings = load_json(root / "control" / "settings.json", {})
    tcfg = settings.get("transcription", {})

    errors = []
    completed = 0

    for video_path in pending:
        try:
            result = transcribe_video(video_path, tcfg)
            txt_path = transcript_dir / f"{video_path.stem}.txt"
            json_path = transcript_dir / f"{video_path.stem}.json"
            full_text = str(result.get("text") or "").strip()
            txt_path.write_text(full_text + ("\n" if full_text else ""), encoding="utf-8")

            metadata = {
                "schema_version": 1,
                "source_type": source_type,
                "creator": creator,
                "video_file": str(video_path.relative_to(root)),
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
            completed += 1
        except Exception as exc:
            errors.append(f"{video_path.name}: {type(exc).__name__}: {exc}")

    return {
        "attempted": len(pending),
        "completed": completed,
        "errors": errors,
    }

def _story_media_download_state_path(root: Path) -> Path:
    return root / "state" / "ephemeral" / "story_media_download_state.json"


def _story_media_set_fingerprint(manifest: dict, capture: dict) -> str | None:
    keys = sorted({
        str(key)
        for key in (capture.get("visited_item_keys") or [])
        if str(key)
    })
    if not keys:
        return None

    digest = hashlib.sha256()
    digest.update(b"story-media-download-cache-v1\0")
    items = manifest.get("items") or {}
    for key in keys:
        item = items.get(key)
        if not isinstance(item, dict):
            return None
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.get("screenshot_sha256") or "").encode("ascii", "ignore"))
        digest.update(b"\0")
        digest.update(str(item.get("source_url") or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _story_media_outputs_available(
    root: Path,
    creator: str,
    files: list[str],
) -> bool:
    if not files:
        return True
    transcript_dir = root / "output" / creator / "stories" / "transcripts"
    for relative in files:
        rel = str(relative or "").strip()
        if not rel:
            return False
        media_path = root / rel
        transcript_path = transcript_dir / f"{Path(rel).stem}.txt"
        if not media_path.is_file() and not transcript_path.is_file():
            return False
    return True


def _story_ytdlp_cached_result(
    root: Path,
    creator: str,
    manifest: dict,
    capture: dict,
) -> dict | None:
    fingerprint = _story_media_set_fingerprint(manifest, capture)
    if not fingerprint:
        return None

    state = load_json(
        _story_media_download_state_path(root),
        {"schema_version": 1, "items": {}},
    )
    row = (state.get("items") or {}).get(creator)
    if not isinstance(row, dict):
        return None
    if str(row.get("status") or "").upper() != "SUCCESS":
        return None
    if str(row.get("fingerprint") or "") != fingerprint:
        return None

    files = [
        str(value)
        for value in (row.get("files") or [])
        if str(value)
    ]
    if not _story_media_outputs_available(root, creator, files):
        return None

    return {
        "ok": True,
        "returncode": 0,
        "files": files,
        "error": None,
        "skipped": True,
        "reason": "UNCHANGED_STORY_SET_ALREADY_DOWNLOADED",
        "cached_at": row.get("updated_at"),
    }


def _record_story_ytdlp_success(
    root: Path,
    creator: str,
    manifest: dict,
    capture: dict,
    result: dict,
) -> None:
    fingerprint = _story_media_set_fingerprint(manifest, capture)
    if not fingerprint or not result.get("ok"):
        return
    path = _story_media_download_state_path(root)
    state = load_json(path, {"schema_version": 1, "items": {}})
    if not isinstance(state, dict):
        state = {"schema_version": 1, "items": {}}
    items = state.get("items")
    if not isinstance(items, dict):
        items = {}
    items[creator] = {
        "status": "SUCCESS",
        "fingerprint": fingerprint,
        "files": [
            str(value)
            for value in (result.get("files") or [])
            if str(value)
        ],
        "updated_at": utc_now(),
    }
    state["schema_version"] = 1
    state["items"] = items
    atomic_write_json(path, state)


def resolve_configured_story_creators(root: Path) -> list[str]:
    cfg = load_json(root / "control" / "ephemeral_sources.json", {"creators": {}})
    creators = []
    for handle, ccfg in cfg.get("creators", {}).items():
        if ccfg.get("stories", {}).get("enabled", False):
            creators.append(normalize_creator_handle(handle))
    return creators


def should_skip_highlight_once(root: Path, creator: str, label: str, force: bool) -> bool:
    if force:
        return False
    state = load_json(
        root / "state" / "ephemeral" / "highlight_ingest_state.json",
        {"schema_version": 1, "items": {}},
    )
    key = f"{creator}:{normalize_label(label)}"
    item = state.get("items", {}).get(key, {})
    return (
        item.get("status") == "DONE"
        and int(item.get("completion_schema") or 0) >= 3
    )


def mark_highlight_done(root: Path, creator: str, label: str, source_url: str, capture: dict) -> None:
    path = root / "state" / "ephemeral" / "highlight_ingest_state.json"
    state = load_json(path, {"schema_version": 1, "items": {}})
    key = f"{creator}:{normalize_label(label)}"
    state["items"][key] = {
        "creator": creator,
        "label": label,
        "source_url": source_url,
        "status": "DONE",
        "completion_schema": 3,
        "app_version": APP_VERSION,
        "completed_at": utc_now(),
        "captured_new": capture.get("captured_new"),
        "seen_existing": capture.get("seen_existing"),
        "visited_frames": capture.get("visited_frames"),
        "view_confirmation_was_present": capture.get("view_confirmation_was_present"),
        "view_confirmation_dismissed": capture.get("view_confirmation_dismissed"),
    }
    atomic_write_json(path, state)


def run_one(
    root: Path,
    mode: str,
    creator: str,
    highlight_label: str | None,
    force: bool,
    max_items: int,
    gemini_circuit: dict | None = None,
    ollama_budget_state: dict | None = None,
) -> dict:
    run_clock = time.perf_counter()
    capture_duration_ms = 0.0
    ytdlp_duration_ms = 0.0
    browser_total_ms = 0.0
    visual_enrichment_duration_ms = 0.0
    transcription_duration_ms = 0.0
    manifest_path = root / "state" / "ephemeral" / "manifest.json"
    manifest = load_json(
        manifest_path,
        {"schema_version": 1, "app_version": APP_VERSION, "items": {}},
    )
    manifest.setdefault("items", {})
    legacy_invalidated = (
        invalidate_legacy_confirmation_evidence(manifest)
        + invalidate_legacy_error_evidence(manifest)
        + invalidate_legacy_unstable_story_evidence(manifest)
    )
    if legacy_invalidated:
        atomic_write_json(manifest_path, manifest)

    if mode == "highlight" and highlight_label and should_skip_highlight_once(
        root, creator, highlight_label, force
    ):
        return {
            "creator": creator,
            "mode": mode,
            "highlight_label": highlight_label,
            "state": "SKIPPED_ALREADY_DONE",
            "errors": [],
        }

    browser_clock = time.perf_counter()
    with sync_playwright() as p:
        context = launch_instagram_context(p)
        try:
            verify_logged_in(context)
            page = context.pages[0] if context.pages else context.new_page()

            if mode == "stories":
                source_type = "STORY"
                source_url = f"https://www.instagram.com/stories/{creator}/"
                discovered = None
            else:
                if not highlight_label:
                    raise RuntimeError("--highlight-label is required for highlight mode.")
                source_type = "HIGHLIGHT"
                source_url, discovered = discover_highlight_url(page, creator, highlight_label)
                open_highlight_from_profile(page, source_url)

            capture_clock = time.perf_counter()
            capture = capture_story_frames(
                page=page,
                root=root,
                manifest=manifest,
                creator=creator,
                start_url=source_url,
                source_type=source_type,
                highlight_label=highlight_label,
                max_items=max_items,
                preopened=(mode == "highlight"),
            )
            capture_duration_ms = (time.perf_counter() - capture_clock) * 1000
            atomic_write_json(manifest_path, manifest)

            ytdlp_clock = time.perf_counter()
            if (
                source_type == "STORY"
                and str(capture.get("reason") or "")
                == "NO_ACTIVE_STORY_OR_STORY_VIEW_REDIRECTED"
                and int(capture.get("visited_frames") or 0) == 0
            ):
                ytdlp = {
                    "ok": True,
                    "returncode": 0,
                    "files": [],
                    "error": None,
                    "skipped": True,
                    "reason": "NO_ACTIVE_STORY",
                }
            elif source_type == "STORY":
                ytdlp = _story_ytdlp_cached_result(
                    root,
                    creator,
                    manifest,
                    capture,
                )
                if ytdlp is None:
                    ytdlp = run_ytdlp(
                        context=context,
                        root=root,
                        creator=creator,
                        source_type=source_type,
                        source_url=source_url,
                    )
                    if ytdlp.get("ok"):
                        _record_story_ytdlp_success(
                            root,
                            creator,
                            manifest,
                            capture,
                            ytdlp,
                        )
            else:
                ytdlp = run_ytdlp(
                    context=context,
                    root=root,
                    creator=creator,
                    source_type=source_type,
                    source_url=source_url,
                )
            ytdlp_duration_ms = (time.perf_counter() - ytdlp_clock) * 1000
        finally:
            context.close()
    browser_total_ms = (time.perf_counter() - browser_clock) * 1000

    story_visual_keys = list(capture.get("visited_item_keys") or [])
    if source_type == "STORY":
        # Also backfill previously captured stable Story screenshots. This matters
        # after upgrades: an older Story may no longer be active in the viewer but
        # its retained screenshot is still within the recent-check evidence window.
        for key, item in (manifest.get("items") or {}).items():
            if not isinstance(item, dict):
                continue
            if str(item.get("source_type") or "").upper() != "STORY":
                continue
            if str(item.get("creator") or "").casefold() != creator.casefold():
                continue
            if str(item.get("research_status") or "").upper() == "INVALID":
                continue
            if not _story_visual_needs_enrichment(item):
                continue
            if key not in story_visual_keys:
                story_visual_keys.append(key)

    visual_enrichment_clock = time.perf_counter()
    visual_enrichment = (
        enrich_story_visual_evidence(
            root,
            manifest,
            story_visual_keys,
            max_attempts=MAX_STORY_VISUAL_ENRICHMENTS_PER_RUN,
            circuit_state=gemini_circuit,
            ollama_budget_state=ollama_budget_state,
        )
        if source_type == "STORY"
        else {
            "attempted": 0,
            "completed": 0,
            "skipped": 0,
            "deferred": 0,
            "max_attempts": MAX_STORY_VISUAL_ENRICHMENTS_PER_RUN,
            "errors": [],
            "changed": False,
        }
    )
    visual_enrichment_duration_ms = (
        time.perf_counter() - visual_enrichment_clock
    ) * 1000
    if visual_enrichment.get("changed"):
        atomic_write_json(manifest_path, manifest)

    transcription_clock = time.perf_counter()
    transcription = transcribe_downloaded_videos(root, creator, source_type)
    transcription_duration_ms = (time.perf_counter() - transcription_clock) * 1000

    errors = []
    if not ytdlp.get("ok"):
        # Story can contain image-only frames, so failed yt-dlp is not fatal if screenshots exist.
        errors.append(f"yt-dlp: {ytdlp.get('error')}")
    errors.extend(visual_enrichment.get("errors", []))
    errors.extend(transcription.get("errors", []))

    if (
        mode == "highlight"
        and highlight_label
        and capture.get("reason") in {"OK", "ENDED_OR_EXITED_STORY_VIEW"}
        and capture.get("visited_frames", 0) > 0
    ):
        # capture_story_frames already fails closed if the confirmation overlay remains.
        mark_highlight_done(root, creator, highlight_label, source_url, capture)

    return {
        "creator": creator,
        "mode": mode,
        "source_url": source_url,
        "highlight_label": highlight_label,
        "capture": capture,
        "visual_enrichment": visual_enrichment,
        "ollama_budget_state": dict(ollama_budget_state or {}),
        "video_download": ytdlp,
        "transcription": transcription,
        "timings": {
            "total_ms": round((time.perf_counter() - run_clock) * 1000, 1),
            "browser_total_ms": round(browser_total_ms, 1),
            "capture_ms": round(capture_duration_ms, 1),
            "story_advance_wait_ms": round(
                float(capture.get("story_advance_wait_ms") or 0.0),
                1,
            ),
            "story_advance_attempts": int(
                capture.get("story_advance_attempts") or 0
            ),
            "story_advance_ready_count": int(
                capture.get("story_advance_ready_count") or 0
            ),
            "story_advance_timeout_count": int(
                capture.get("story_advance_timeout_count") or 0
            ),
            "story_capture_ready_wait_ms": round(
                float(capture.get("story_capture_ready_wait_ms") or 0.0),
                1,
            ),
            "story_capture_ready_attempts": int(
                capture.get("story_capture_ready_attempts") or 0
            ),
            "story_capture_ready_count": int(
                capture.get("story_capture_ready_count") or 0
            ),
            "story_capture_not_ready_count": int(
                capture.get("story_capture_not_ready_count") or 0
            ),
            "ytdlp_ms": round(ytdlp_duration_ms, 1),
            "visual_enrichment_ms": round(visual_enrichment_duration_ms, 1),
            "transcription_ms": round(transcription_duration_ms, 1),
        },
        "state": "DONE" if not errors else "DONE_WITH_ERRORS",
        "errors": errors,
        "discovered_highlights": discovered if mode == "highlight" else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--mode", choices=["stories", "highlight"], required=True)
    parser.add_argument("--creator")
    parser.add_argument("--highlight-label")
    parser.add_argument("--configured", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-items", type=int, default=80)
    args = parser.parse_args()
    root = args.root.resolve()

    started = utc_now()
    status_path = root / "state" / "ephemeral" / "status.json"

    if args.configured:
        if args.mode != "stories":
            raise RuntimeError("--configured currently supports stories mode only.")
        creators = resolve_configured_story_creators(root)
    else:
        if not args.creator:
            raise RuntimeError("--creator is required unless --configured is used.")
        creators = [normalize_creator_handle(args.creator)]

    results = []
    try:
        for creator in creators:
            results.append(run_one(
                root=root,
                mode=args.mode,
                creator=creator,
                highlight_label=args.highlight_label,
                force=args.force,
                max_items=args.max_items,
            ))

        total_errors = sum(len(r.get("errors", [])) for r in results)
        status = {
            "schema_version": 1,
            "app_version": APP_VERSION,
            "state": "DONE" if total_errors == 0 else "DONE_WITH_ERRORS",
            "started_at": started,
            "finished_at": utc_now(),
            "mode": args.mode,
            "results": results,
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
            "mode": args.mode,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
        atomic_write_json(status_path, status)
        print(status["traceback"], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
