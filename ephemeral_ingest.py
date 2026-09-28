from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from transcription_backend import DEFAULT_GEMINI_VISUAL_MODEL, extract_image_evidence_gemini, safe_gemini_error, transcribe_video


APP_VERSION = "0.4.9"
STORY_URL_RE = re.compile(r"/stories/(?P<user>[^/]+)/(?P<id>\d+)/?")
HIGHLIGHT_URL_RE = re.compile(r"/stories/highlights/(?P<id>\d+)/?")
STRICT_STORY_ROOT_PATH_RE = re.compile(r"^/stories/[A-Za-z0-9._-]{1,64}/?$")
STRICT_STORY_PATH_RE = re.compile(r"^/stories/[A-Za-z0-9._-]{1,64}/\d+/?$")
STRICT_HIGHLIGHT_PATH_RE = re.compile(r"^/stories/highlights/\d+/?$")
MAX_STORY_VISUAL_ENRICHMENTS_PER_RUN = 2


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

    for _ in range(max_items):
        if "/stories/" not in page.url:
            stop_reason = "ENDED_OR_EXITED_STORY_VIEW"
            break

        page.wait_for_timeout(900)

        if instagram_story_error_present(page):
            stop_reason = "INSTAGRAM_ERROR_PAGE_DURING_TRAVERSAL"
            break

        screenshot = page.screenshot(full_page=False)
        media_url = visible_story_media_url(page) if source_type == "STORY" else None
        evidence_id, story_id, identity_basis = extract_story_identity(
            page.url,
            screenshot,
            media_url,
        )
        media_identity_path = _normalized_media_identity_path(media_url)
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
        page.wait_for_timeout(1000)

    return {
        "captured_new": captured_new,
        "seen_existing": seen_existing,
        "unstable_identity_skipped": unstable_identity_skipped,
        "identity_aliases_retired": identity_aliases_retired,
        "visited_item_keys": visited_item_keys,
        "visited_frames": visited,
        "view_confirmation_was_present": confirmation_was_present,
        "view_confirmation_dismissed": confirmation_dismissed,
        "reason": stop_reason,
    }


def enrich_story_visual_evidence(
    root: Path,
    manifest: dict,
    item_keys: list[str],
    *,
    max_attempts: int = MAX_STORY_VISUAL_ENRICHMENTS_PER_RUN,
) -> dict:
    """Backfill readable multimodal evidence for visited Story screenshots."""
    settings = load_json(root / "control" / "settings.json", {})
    model = str(
        (settings.get("transcription") or {}).get(
            "gemini_visual_model",
            DEFAULT_GEMINI_VISUAL_MODEL,
        )
        or DEFAULT_GEMINI_VISUAL_MODEL
    ).strip()
    attempted = 0
    completed = 0
    skipped = 0
    deferred = 0
    errors: list[str] = []
    changed = False

    for key in item_keys:
        item = (manifest.get("items") or {}).get(key)
        if not isinstance(item, dict):
            continue
        if str(item.get("source_type") or "").upper() != "STORY":
            continue
        if str(item.get("research_status") or "").upper() == "INVALID":
            continue
        if (
            str(item.get("visual_description_status") or "").upper() == "DONE"
            and str(item.get("visual_description") or "").strip()
        ):
            skipped += 1
            continue

        if attempted >= max(0, int(max_attempts)):
            deferred += 1
            continue

        screenshot_rel = str(item.get("screenshot_file") or "").strip()
        screenshot_path = root / screenshot_rel if screenshot_rel else None
        if screenshot_path is None or not screenshot_path.is_file():
            errors.append(f"{key}: STORY_SCREENSHOT_MISSING")
            continue

        attempted += 1
        try:
            result = extract_image_evidence_gemini(
                screenshot_path,
                model=model,
            )
            text = str(result.get("text") or "").strip()
            item["visual_description"] = text
            item["visual_description_status"] = "DONE" if text else "INSUFFICIENT_CONTENT"
            item["visual_description_source"] = result.get("source")
            item["visual_description_provider"] = result.get("provider")
            item["visual_description_model"] = result.get("model")
            item["visual_description_generated_at"] = utc_now()
            item.pop("visual_description_error", None)
            changed = True
            if text:
                completed += 1
            else:
                errors.append(f"{key}: NO_MEANINGFUL_VISUAL_EVIDENCE")
        except Exception as exc:
            safe_error = safe_gemini_error(exc, operation="visual evidence extraction")
            item["visual_description_status"] = "ERROR"
            item["visual_description_error"] = safe_error
            item["visual_description_generated_at"] = utc_now()
            changed = True
            errors.append(f"{key}: {safe_error}")

    return {
        "attempted": attempted,
        "completed": completed,
        "skipped": skipped,
        "deferred": deferred,
        "max_attempts": max(0, int(max_attempts)),
        "errors": errors,
        "changed": changed,
        "model": model,
    }


def run_ytdlp(context, root: Path, creator: str, source_type: str, source_url: str) -> dict:
    source_url = canonical_instagram_ephemeral_url(source_url)
    source_dir = "stories" if source_type == "STORY" else "highlights"
    video_dir = root / "output" / creator / source_dir / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)

    cookie_path = secret_dir() / "instagram_ephemeral_ytdlp_cookies.txt"
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
) -> dict:
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
            atomic_write_json(manifest_path, manifest)

            ytdlp = run_ytdlp(
                context=context,
                root=root,
                creator=creator,
                source_type=source_type,
                source_url=source_url,
            )
        finally:
            context.close()

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
            if (
                str(item.get("visual_description_status") or "").upper() == "DONE"
                and str(item.get("visual_description") or "").strip()
            ):
                continue
            if key not in story_visual_keys:
                story_visual_keys.append(key)

    visual_enrichment = (
        enrich_story_visual_evidence(
            root,
            manifest,
            story_visual_keys,
            max_attempts=MAX_STORY_VISUAL_ENRICHMENTS_PER_RUN,
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
    if visual_enrichment.get("changed"):
        atomic_write_json(manifest_path, manifest)

    transcription = transcribe_downloaded_videos(root, creator, source_type)

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
        "video_download": ytdlp,
        "transcription": transcription,
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
