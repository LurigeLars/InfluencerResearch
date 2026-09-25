from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktok_camofox_sync as sync

EVAL_VERSION = "0.4.1"


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


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)

def extract_handle(url: str) -> tuple[str | None, str | None]:
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [p for p in parsed.path.split("/") if p]
    if _host_matches(host, "instagram.com") and parts:
        return "INSTAGRAM", parts[0].lstrip("@").strip()
    if _host_matches(host, "tiktok.com") and parts and parts[0].startswith("@"):
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
            with contextlib.suppress(Exception):
                sync.request_json(
                    "DELETE",
                    f"/tabs/{urllib.parse.quote(tab_id)}?"
                    + urllib.parse.urlencode({"userId": user_id}),
                    timeout=10,
                )
    return result



TIKTOK_VIDEO_URL_RE = re.compile(
    r"^https://(?:www\.)?tiktok\.com/@(?P<handle>[A-Za-z0-9._-]+)/video/(?P<id>\d+)(?:[?#].*)?$",
    re.I,
)
TIKTOK_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{20,256}$")
REGISTERED_CREATOR_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")


def _json_from_stdout(raw: str) -> Any:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        for line in reversed(raw.splitlines()):
            try:
                return json.loads(line)
            except Exception:
                continue
    return None


def _canonical_tiktok_video_url(value: str, *, expected_handle: str) -> str | None:
    value = str(value or "").strip()
    m = TIKTOK_VIDEO_URL_RE.fullmatch(value)
    if not m or m.group("handle").casefold() != expected_handle.casefold().lstrip("@"):
        return None
    return f"https://www.tiktok.com/@{expected_handle.lstrip('@')}/video/{m.group('id')}"


def registered_tiktok_seed_urls(
    root: Path,
    *,
    registered_creator_key: str | None,
    expected_handle: str,
    expected_profile_url: str,
) -> tuple[list[str], dict]:
    """Read bounded internal TikTok discovery seeds from an already-verified registry source.

    These fields are not accepted by the public/Drive bridge registration schema. They are
    technical provenance metadata maintained in the canonical registry.
    """
    if not registered_creator_key:
        return [], {"method": "VERIFIED_REGISTRY_SEEDS", "reason": "NO_REGISTERED_CREATOR_KEY"}

    key = str(registered_creator_key).strip().lower()
    if not REGISTERED_CREATOR_KEY_RE.fullmatch(key):
        return [], {"method": "VERIFIED_REGISTRY_SEEDS", "reason": "BAD_REGISTERED_CREATOR_KEY"}

    registry = load_json(root / "control" / "creator_registry.json", {})
    creators = registry.get("creators") if isinstance(registry, dict) else None
    profile = creators.get(key) if isinstance(creators, dict) else None
    if not isinstance(profile, dict) or str(profile.get("status", "")).upper() != "ACTIVE":
        return [], {"method": "VERIFIED_REGISTRY_SEEDS", "reason": "CREATOR_NOT_ACTIVE"}
    verification = profile.get("verification") or {}
    if not isinstance(verification, dict) or str(verification.get("status", "")).upper() != "VERIFIED":
        return [], {"method": "VERIFIED_REGISTRY_SEEDS", "reason": "CREATOR_NOT_VERIFIED"}

    expected_profile = expected_profile_url.casefold().rstrip("/")
    matching_source = None
    for source in profile.get("sources") or []:
        if not isinstance(source, dict):
            continue
        if str(source.get("platform", "")).upper() != "TIKTOK":
            continue
        if str(source.get("verification_status", "")).upper() != "VERIFIED":
            continue
        if not bool(source.get("enabled", True)) or not bool(source.get("evaluation_enabled", True)):
            continue
        if str(source.get("profile_url", "")).casefold().rstrip("/") != expected_profile:
            continue
        matching_source = source
        break

    if not matching_source:
        return [], {"method": "VERIFIED_REGISTRY_SEEDS", "reason": "NO_MATCHING_VERIFIED_TIKTOK_SOURCE"}

    raw_seeds = matching_source.get("discovery_seed_video_urls", [])
    if not isinstance(raw_seeds, list) or len(raw_seeds) > 8:
        return [], {"method": "VERIFIED_REGISTRY_SEEDS", "reason": "BAD_SEED_LIST"}

    seeds: list[str] = []
    rejected = 0
    for raw in raw_seeds:
        clean = _canonical_tiktok_video_url(str(raw), expected_handle=expected_handle)
        if not clean:
            rejected += 1
            continue
        if clean not in seeds:
            seeds.append(clean)

    return seeds, {
        "method": "VERIFIED_REGISTRY_SEEDS",
        "creator_key": key,
        "source_profile": matching_source.get("profile_url"),
        "seeds_found": len(seeds),
        "seeds_rejected": rejected,
        "basis": matching_source.get("discovery_seed_basis"),
    }


def _canonical_ytdlp_profile_target(value: str, *, expected_handle: str) -> str | None:
    raw = str(value or "").strip()
    if raw.startswith("tiktokuser:"):
        channel_id = raw.split(":", 1)[1]
        if TIKTOK_CHANNEL_ID_RE.fullmatch(channel_id):
            return f"tiktokuser:{channel_id}"
        return None
    platform, handle = extract_handle(raw)
    expected = str(expected_handle or "").casefold().lstrip("@")
    if platform != "TIKTOK" or not handle or handle.casefold() != expected:
        return None
    return f"https://www.tiktok.com/@{handle}"


def yt_dlp_video_identity(video_url: str, *, expected_handle: str) -> tuple[str | None, dict]:
    """Resolve TikTok sec_uid/channel_id only from an exact-handle public seed video."""
    clean = _canonical_tiktok_video_url(video_url, expected_handle=expected_handle)
    if not clean:
        return None, {"method": "YT_DLP_VIDEO_IDENTITY", "ok": False, "reason": "BAD_OR_MISMATCHED_SEED_URL"}

    expected_video_id = clean.rsplit("/", 1)[-1]
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        "--dump-single-json",
        "--skip-download",
        "--",
        clean,
    ]
    try:
        # URL is strict-canonical TikTok video and '--' terminates yt-dlp option parsing.

        # lgtm[py/command-line-injection]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=False)
    except subprocess.TimeoutExpired as exc:
        return None, {
            "method": "YT_DLP_VIDEO_IDENTITY",
            "ok": False,
            "returncode": 124,
            "reason": "TIMEOUT",
            "diagnostic_tail": str(exc)[-1500:],
        }

    data = _json_from_stdout(proc.stdout or "")
    uploader = str(data.get("uploader") or "").strip() if isinstance(data, dict) else ""
    video_id = str(data.get("id") or "").strip() if isinstance(data, dict) else ""
    channel_id = str(data.get("channel_id") or "").strip() if isinstance(data, dict) else ""

    reason = None
    if proc.returncode != 0 or not isinstance(data, dict):
        reason = "VIDEO_METADATA_UNAVAILABLE"
    elif video_id != expected_video_id:
        reason = "SEED_VIDEO_ID_MISMATCH"
    elif uploader.casefold().lstrip("@") != expected_handle.casefold().lstrip("@"):
        reason = "SEED_UPLOADER_MISMATCH"
    elif not TIKTOK_CHANNEL_ID_RE.fullmatch(channel_id):
        reason = "BAD_OR_MISSING_CHANNEL_ID"

    diag = {
        "method": "YT_DLP_VIDEO_IDENTITY",
        "ok": reason is None,
        "returncode": proc.returncode,
        "video_id": video_id or None,
        "uploader": uploader or None,
        "channel_id": channel_id if reason is None else None,
        "reason": reason,
        "diagnostic_tail": (proc.stderr or "")[-1500:],
    }
    return (channel_id if reason is None else None), diag


def yt_dlp_secondary_profile_urls(
    channel_id: str,
    *,
    handle: str,
    target: int,
) -> tuple[list[str], dict]:
    if not TIKTOK_CHANNEL_ID_RE.fullmatch(str(channel_id or "")):
        return [], {"method": "YT_DLP_SECONDARY_USER_ID", "ok": False, "reason": "BAD_CHANNEL_ID"}
    urls, diag = yt_dlp_profile_urls(
        f"tiktokuser:{channel_id}",
        handle=handle,
        target=target,
    )
    diag = {**diag, "method": "YT_DLP_SECONDARY_USER_ID", "channel_id": channel_id}
    return urls, diag


def yt_dlp_profile_urls(profile_url: str, *, handle: str, target: int) -> tuple[list[str], dict]:
    """Enumerate a public TikTok profile with yt-dlp before using browser scraping."""
    profile_target = _canonical_ytdlp_profile_target(profile_url, expected_handle=handle)
    if not profile_target:
        return [], {"method": "YT_DLP_PROFILE", "ok": False, "reason": "BAD_PROFILE_TARGET"}
    limit = max(1, min(int(target), 100))
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",
        "--quiet",
        "--no-warnings",
        "--flat-playlist",
        "--playlist-end", str(limit),
        "--dump-single-json",
        "--skip-download",
        "--",
        profile_target,
    ]
    try:
        # Target is strict-canonical TikTok/tiktokuser and '--' terminates yt-dlp option parsing.

        # lgtm[py/command-line-injection]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=False)
    except subprocess.TimeoutExpired as exc:
        return [], {
            "method": "YT_DLP_PROFILE",
            "ok": False,
            "returncode": 124,
            "reason": "TIMEOUT",
            "diagnostic_tail": str(exc)[-1500:],
        }

    data = _json_from_stdout(proc.stdout or "")

    urls: list[str] = []
    seen: set[str] = set()
    entries = data.get("entries", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        entries = []

    exact = handle.casefold().lstrip("@")
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        candidate = str(entry.get("webpage_url") or entry.get("url") or "").strip()
        m = re.search(r"https?://(?:www\.)?tiktok\.com/@([^/]+)/video/(\d+)", candidate, re.I)
        if m and m.group(1).casefold() == exact:
            clean = f"https://www.tiktok.com/@{handle}/video/{m.group(2)}"
        else:
            video_id = str(entry.get("id") or "").strip()
            if not re.fullmatch(r"\d+", video_id):
                continue
            clean = f"https://www.tiktok.com/@{handle}/video/{video_id}"

        if clean not in seen:
            seen.add(clean)
            urls.append(clean)
        if len(urls) >= limit:
            break

    diag = {
        "method": "YT_DLP_PROFILE",
        "ok": proc.returncode == 0 and bool(urls),
        "returncode": proc.returncode,
        "entries_seen": len(entries),
        "video_urls_found": len(urls),
        "diagnostic_tail": (proc.stderr or "")[-2000:],
    }
    if not urls:
        diag["reason"] = "NO_PROFILE_ENTRIES"
        if "Unable to extract secondary user ID" in diag.get("diagnostic_tail", ""):
            diag["known_site_bug"] = "YT_DLP_TIKTOK_USER_SECONDARY_ID_2026"
            diag["fallback"] = "VERIFIED_SEED_SECONDARY_ID_THEN_CAMOFOX"
    return urls, diag


def camofox_search_video_urls(handle: str, *, target: int, creator_key: str) -> tuple[list[str], dict]:
    """Fallback discovery via TikTok search pages.

    This avoids the currently broken yt-dlp profile extractor path and relies only
    on public TikTok pages. Exact-handle filtering remains mandatory.
    """
    found: list[str] = []
    seen: set[str] = set()
    attempts: list[dict] = []
    limit = max(1, min(int(target), 100))
    search_urls = [
        "https://www.tiktok.com/search/video?" + urllib.parse.urlencode({"q": handle}),
        "https://www.tiktok.com/search?" + urllib.parse.urlencode({"q": handle}),
    ]

    for n, search_url in enumerate(search_urls, start=1):
        user_id = f"instagramresearch-eval-video-search-{creator_key}-{n}"
        session_key = f"eval-video-search-{creator_key}-{n}-{compact_timestamp()}"
        tab_id = None
        diag: dict[str, Any] = {"url": search_url}
        try:
            tab = sync.request_json(
                "POST", "/tabs",
                {"userId": user_id, "sessionKey": session_key, "url": search_url, "trace": False},
                timeout=60,
            )
            if not isinstance(tab, dict) or not tab.get("tabId"):
                raise RuntimeError(f"Unexpected CamoFox create-tab response: {tab}")
            tab_id = str(tab["tabId"])
            time.sleep(3)
            urls, collect_diag = sync.collect_video_urls(
                tab_id, user_id=user_id, handle=handle, target=limit, max_scrolls=35
            )
            diag.update(collect_diag)
            for url in urls:
                if url not in seen:
                    seen.add(url)
                    found.append(url)
                if len(found) >= limit:
                    break
        except Exception as exc:
            diag["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if tab_id:
                with contextlib.suppress(Exception):
                    sync.request_json(
                        "DELETE",
                        f"/tabs/{urllib.parse.quote(tab_id)}?" + urllib.parse.urlencode({"userId": user_id}),
                        timeout=10,
                    )
        attempts.append(diag)
        if len(found) >= limit:
            break

    return found[:limit], {
        "method": "CAMOFOX_TIKTOK_SEARCH",
        "video_urls_found": len(found[:limit]),
        "attempts": attempts,
    }


def verify_tiktok_profile(
    profile_url: str,
    *,
    sample_size: int,
    creator_key: str,
    seed_video_urls: list[str] | None = None,
    seed_diagnostic: dict | None = None,
) -> dict:
    platform, handle = extract_handle(profile_url)
    if platform != "TIKTOK" or not handle:
        return {"status": "UNVERIFIED", "profile_url": profile_url, "reason": "NOT_TIKTOK_PROFILE"}

    yt_urls, yt_diag = yt_dlp_profile_urls(
        f"https://www.tiktok.com/@{handle}",
        handle=handle,
        target=max(1, sample_size),
    )
    if yt_urls:
        return {
            "status": "VERIFIED",
            "profile_url": f"https://www.tiktok.com/@{handle}",
            "handle": handle,
            "video_urls_found": len(yt_urls),
            "sample_urls": yt_urls[: min(5, len(yt_urls))],
            "video_urls": yt_urls,
            "verification_method": "YT_DLP_PROFILE",
            "diagnostic": yt_diag,
        }

    secondary_attempts: list[dict] = []
    if yt_diag.get("known_site_bug") == "YT_DLP_TIKTOK_USER_SECONDARY_ID_2026":
        for seed_url in list(seed_video_urls or [])[:4]:
            channel_id, identity_diag = yt_dlp_video_identity(seed_url, expected_handle=handle)
            attempt: dict[str, Any] = {"seed_url": seed_url, "identity": identity_diag}
            if channel_id:
                secondary_urls, secondary_diag = yt_dlp_secondary_profile_urls(
                    channel_id,
                    handle=handle,
                    target=max(1, sample_size),
                )
                attempt["secondary_profile"] = secondary_diag
                if secondary_urls:
                    secondary_attempts.append(attempt)
                    return {
                        "status": "VERIFIED",
                        "profile_url": f"https://www.tiktok.com/@{handle}",
                        "handle": handle,
                        "video_urls_found": len(secondary_urls),
                        "sample_urls": secondary_urls[: min(5, len(secondary_urls))],
                        "video_urls": secondary_urls,
                        "verification_method": "YT_DLP_SECONDARY_USER_ID",
                        "diagnostic": {
                            "yt_dlp": yt_diag,
                            "registry_seeds": seed_diagnostic,
                            "secondary_attempts": secondary_attempts,
                        },
                    }
            secondary_attempts.append(attempt)

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
        urls, camo_diag = sync.collect_video_urls(
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
                "video_urls": urls,
                "verification_method": "CAMOFOX_PROFILE",
                "diagnostic": {
                    "yt_dlp": yt_diag,
                    "registry_seeds": seed_diagnostic,
                    "secondary_attempts": secondary_attempts,
                    "camofox": camo_diag,
                },
            }

        search_urls, search_diag = camofox_search_video_urls(
            handle, target=max(1, sample_size), creator_key=creator_key
        )
        if search_urls:
            return {
                "status": "VERIFIED",
                "profile_url": f"https://www.tiktok.com/@{handle}",
                "handle": handle,
                "video_urls_found": len(search_urls),
                "sample_urls": search_urls[: min(5, len(search_urls))],
                "video_urls": search_urls,
                "verification_method": "CAMOFOX_TIKTOK_SEARCH",
                "diagnostic": {
                    "yt_dlp": yt_diag,
                    "registry_seeds": seed_diagnostic,
                    "secondary_attempts": secondary_attempts,
                    "camofox_profile": camo_diag,
                    "camofox_search": search_diag,
                },
            }

        return {
            "status": "UNVERIFIED",
            "profile_url": profile_url,
            "handle": handle,
            "video_urls_found": 0,
            "diagnostic": {
                "yt_dlp": yt_diag,
                "registry_seeds": seed_diagnostic,
                "secondary_attempts": secondary_attempts,
                "camofox_profile": camo_diag,
                "camofox_search": search_diag,
            },
            "reason": "NO_EXACT_HANDLE_VIDEO_URLS_FOUND",
        }
    except Exception as exc:
        return {
            "status": "UNVERIFIED",
            "profile_url": profile_url,
            "handle": handle,
            "diagnostic": {"yt_dlp": yt_diag, "registry_seeds": seed_diagnostic, "secondary_attempts": secondary_attempts},
            "reason": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if tab_id:
            with contextlib.suppress(Exception):
                sync.request_json(
                    "DELETE",
                    f"/tabs/{urllib.parse.quote(tab_id)}?"
                    + urllib.parse.urlencode({"userId": user_id}),
                    timeout=10,
                )

def discover_tiktok(
    profile_url: str,
    *,
    sample_size: int,
    creator_name: str | None = None,
    root: Path | None = None,
    registered_creator_key: str | None = None,
) -> dict:
    """Discover a TikTok account without making any request to Instagram.

    An Instagram URL may be supplied only as identifier metadata so we can extract the
    handle. Cross-platform discovery is performed exclusively on TikTok.
    """
    platform, source_handle = extract_handle(profile_url)
    if not platform or not source_handle:
        return {
            "status": "UNVERIFIED",
            "source_profile": profile_url,
            "reason": "UNSUPPORTED_PROFILE_URL",
            "candidates": [],
            "instagram_accessed": False,
            "network_scope": ["TIKTOK"],
        }

    creator_key = creator_key_from_handle(source_handle)
    seed_urls: list[str] = []
    seed_diag: dict | None = None
    if platform == "TIKTOK" and root is not None and registered_creator_key:
        seed_urls, seed_diag = registered_tiktok_seed_urls(
            root,
            registered_creator_key=registered_creator_key,
            expected_handle=source_handle,
            expected_profile_url=f"https://www.tiktok.com/@{source_handle}",
        )
    candidates: list[dict] = []
    seen: set[str] = set()

    def add_candidate(url: str, basis: str) -> None:
        low = url.casefold().rstrip("/")
        if low in seen:
            return
        seen.add(low)
        candidates.append({"url": url, "basis": basis})

    if platform == "TIKTOK":
        add_candidate(f"https://www.tiktok.com/@{source_handle}", "DIRECT_TIKTOK_PROFILE")
    else:
        # SAFETY RULE: Never open/read the Instagram profile here. The URL is metadata only.
        add_candidate(f"https://www.tiktok.com/@{source_handle}", "SAME_HANDLE_PROBE")

        search_queries: list[tuple[str, str]] = [(source_handle, "TIKTOK_SEARCH_HANDLE")]
        if creator_name and creator_name.strip():
            search_queries.append((creator_name.strip(), "TIKTOK_SEARCH_NAME"))

        for query, basis in search_queries:
            search_url = "https://www.tiktok.com/search/user?" + urllib.parse.urlencode({"q": query})
            read = browser_read(
                search_url,
                user_id=f"instagramresearch-eval-tiktok-search-{creator_key}",
                session_key=f"eval-tiktok-search-{creator_key}-{compact_timestamp()}",
            )
            found: list[str] = []
            found.extend(tiktok_candidates(read.get("snapshot")))
            found.extend(tiktok_candidates(read.get("links")))
            for url in found[:20]:
                add_candidate(url, basis)

    attempts = []
    for candidate in candidates:
        verified = verify_tiktok_profile(
            candidate["url"],
            sample_size=sample_size,
            creator_key=creator_key,
            seed_video_urls=seed_urls,
            seed_diagnostic=seed_diag,
        )
        attempts.append({**candidate, "verification": verified})
        if verified.get("status") == "VERIFIED":
            return {
                "status": "VERIFIED",
                "source_platform": platform,
                "source_profile": profile_url,
                "source_handle": source_handle,
                "creator_name": creator_name,
                "tiktok_profile": verified["profile_url"],
                "tiktok_handle": verified["handle"],
                "match_basis": candidate["basis"],
                "verified_video_urls": list(verified.get("video_urls") or []),
                "verification_method": verified.get("verification_method"),
                "attempts": attempts,
                "instagram_accessed": False,
                "network_scope": ["TIKTOK"],
            }

    return {
        "status": "UNVERIFIED",
        "source_platform": platform,
        "source_profile": profile_url,
        "source_handle": source_handle,
        "creator_name": creator_name,
        "reason": "NO_VERIFIED_TIKTOK_PROFILE",
        "attempts": attempts,
        "instagram_accessed": False,
        "network_scope": ["TIKTOK"],
    }



def seed_verified_catalog(root: Path, *, handle: str, profile_url: str, urls: list[str]) -> dict:
    """Persist already verified profile URLs into the creator-specific TikTok catalog."""
    creator_key = creator_key_from_handle(handle)
    state_dir = root / "state" / "tiktok"
    catalog_path = state_dir / f"{creator_key}_catalog.json"
    catalog = load_json(
        catalog_path,
        {
            "schema_version": 1,
            "app_version": sync.APP_VERSION,
            "profile_url": profile_url,
            "order": [],
            "items": {},
        },
    )
    merged = sync.merge_catalog(catalog, urls, profile_url=profile_url)
    atomic_json(catalog_path, merged)
    return {
        "path": str(catalog_path),
        "seeded_urls": len(urls),
        "catalog_items": len(merged.get("items", {})),
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
    parser.add_argument("--creator-name", default=None, help="Optional display name used only for TikTok search")
    parser.add_argument("--registered-creator-key", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    root = args.root.resolve()
    registered_creator_key = str(args.registered_creator_key or "").strip().lower() or None
    if registered_creator_key and not REGISTERED_CREATOR_KEY_RE.fullmatch(registered_creator_key):
        raise SystemExit("Invalid registered creator key.")
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
        "creator_name": args.creator_name,
        "instagram_accessed": False,
        "network_scope": ["TIKTOK"],
        "sample_size": sample_size,
        "started_at": started_at,
    })

    try:
        server = sync.start_server()
        discovery = discover_tiktok(
            args.profile_url,
            sample_size=sample_size,
            creator_name=args.creator_name,
            root=root,
            registered_creator_key=registered_creator_key,
        )
        if discovery.get("status") != "VERIFIED":
            final = {
                "schema_version": 1,
                "evaluation_version": EVAL_VERSION,
                "state": "NO_VERIFIED_TIKTOK",
                "evaluation_run_id": evaluation_run_id,
                "source_profile": args.profile_url,
                "creator_name": args.creator_name,
                "instagram_accessed": False,
                "network_scope": ["TIKTOK"],
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
        verified_urls = [str(x) for x in discovery.get("verified_video_urls", []) if x]
        catalog_seed = seed_verified_catalog(
            root,
            handle=tiktok_handle,
            profile_url=discovery["tiktok_profile"],
            urls=verified_urls,
        ) if verified_urls else {"seeded_urls": 0, "catalog_items": 0}

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
            "creator_name": args.creator_name,
            "instagram_accessed": False,
            "network_scope": ["TIKTOK"],
            "sample_size_requested": sample_size,
            "permanent_source": False,
            "started_at": started_at,
            "finished_at": utc_now(),
            "server": server,
            "discovery": discovery,
            "catalog_seed": catalog_seed,
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
            "creator_name": args.creator_name,
            "instagram_accessed": False,
            "network_scope": ["TIKTOK"],
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
