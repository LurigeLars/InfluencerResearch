from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from creator_registry import get_creator, load_registry, select_monitor_sources
from creator_monitor import manifest_done_ids, _tiktok_published_at
import youtube_creator_evaluation as yte
import tiktok_camofox_sync as tts
import instagram_camofox_public_smoke as instagram_smoke
import ephemeral_ingest as ephemeral

RECENT_CHECK_VERSION = "0.2.4"
SUPPORTED_PLATFORMS = {"YOUTUBE", "TIKTOK", "INSTAGRAM"}
MAX_DISCOVERY_PER_SOURCE = 200
MIN_DISCOVERY_PER_SOURCE = 15
MAX_ANALYSIS_EVIDENCE_CHARS = 6000
STOCKHOLM_TZ = ZoneInfo("Europe/Stockholm")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_iso_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_window(window: str, lookback_days: int | None) -> tuple[datetime, datetime, dict]:
    end = now_utc()
    w = str(window or "TODAY").upper()
    if w == "TODAY":
        local_now = end.astimezone(STOCKHOLM_TZ)
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = start_local.astimezone(timezone.utc)
        offset = local_now.utcoffset()
        offset_minutes = int(offset.total_seconds() // 60) if offset is not None else None
        meta = {
            "window": "TODAY",
            "timezone": "Europe/Stockholm",
            "timezone_source": "IANA_ZONEINFO",
            "calendar_date": local_now.date().isoformat(),
            "system_utc_offset_minutes": offset_minutes,
        }
    elif w == "LAST_7_DAYS":
        start = end - timedelta(days=7)
        meta = {"window": "LAST_7_DAYS", "timezone": "UTC", "lookback_days": 7}
    elif w == "LAST_N_DAYS":
        days = int(lookback_days or 0)
        if not 1 <= days <= 30:
            raise ValueError("BAD_LOOKBACK_DAYS")
        start = end - timedelta(days=days)
        meta = {"window": "LAST_N_DAYS", "timezone": "UTC", "lookback_days": days}
    else:
        raise ValueError("BAD_WINDOW")
    return start, end, meta



def _next_discovery_limit(current: int) -> int:
    """Grow discovery geometrically while keeping a hard per-source safety cap."""
    current = max(1, int(current))
    return min(MAX_DISCOVERY_PER_SOURCE, max(current + MIN_DISCOVERY_PER_SOURCE, current * 2))


def _coverage_complete(*, discovered_count: int, requested_limit: int, known_times: list[datetime], cutoff: datetime) -> bool:
    """A window is proven complete when discovery exhausts or crosses the time cutoff."""
    return discovered_count < requested_limit or any(x < cutoff for x in known_times)

def _eligible_evaluation_sources(profile: dict, *, include_registered_instagram: bool = False) -> list[dict]:
    return sorted(
        [
            s for s in profile.get("sources", [])
            if s.get("enabled")
            and str(s.get("platform", "")).upper() in SUPPORTED_PLATFORMS
            and (
                s.get("evaluation_enabled")
                or (
                    include_registered_instagram
                    and str(s.get("platform", "")).upper() == "INSTAGRAM"
                )
            )
        ],
        key=lambda s: (int(s.get("priority", 100)), str(s.get("platform", ""))),
    )


def select_profiles_and_sources(root: Path, scope: str, creator_keys: list[str]) -> list[tuple[dict, list[dict]]]:
    if creator_keys:
        out = []
        for key in creator_keys:
            profile = get_creator(root, key)
            sources = _eligible_evaluation_sources(profile, include_registered_instagram=True)
            if not sources:
                raise ValueError(f"NO_REGISTERED_ELIGIBLE_SOURCE:{key}")
            out.append((profile, sources))
        return out

    registry = load_registry(root)
    profiles = [p for p in registry["creators"].values() if p.get("status") == "ACTIVE"]
    mode = str(scope or "MONITORED").upper()
    out: list[tuple[dict, list[dict]]] = []
    if mode == "MONITORED":
        for profile in profiles:
            sources = select_monitor_sources(profile)
            if sources:
                # Instagram Stories are an ephemeral companion source for a monitored
                # creator even though the persistent creator_monitor adapter currently
                # supports only YouTube/TikTok. Include any verified registered
                # Instagram profile in recent-check runs so Stories are captured.
                seen_platforms = {str(source.get("platform", "")).upper() for source in sources}
                for source in _eligible_evaluation_sources(profile, include_registered_instagram=True):
                    platform = str(source.get("platform", "")).upper()
                    if platform == "INSTAGRAM" and platform not in seen_platforms:
                        sources.append(source)
                        seen_platforms.add(platform)
                sources = sorted(
                    sources,
                    key=lambda source: (
                        int(source.get("priority", 100)),
                        str(source.get("platform", "")),
                    ),
                )
                out.append((profile, sources))
    elif mode == "ALL_REGISTERED":
        for profile in profiles:
            sources = _eligible_evaluation_sources(profile, include_registered_instagram=True)
            if sources:
                out.append((profile, sources))
    else:
        raise ValueError("BAD_SCOPE")
    return out


def _youtube_probe_missing(entries: list[dict]) -> tuple[dict[str, str], dict]:
    missing = [e for e in entries if not e.get("published_at")]
    if not missing:
        return {}, {"attempted": 0, "resolved": 0, "returncode": 0, "diagnostic_tail": ""}
    urls = [str(e["url"]) for e in missing]
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config", "--skip-download", "--no-playlist", "--dump-json",
        *urls,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired as exc:
        return {}, {"attempted": len(urls), "resolved": 0, "returncode": 124, "diagnostic_tail": str(exc)[-2000:]}
    resolved: dict[str, str] = {}
    for line in (p.stdout or "").splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        vid = str(obj.get("id") or "").strip()
        published = yte.published_iso(obj)
        if vid and published:
            resolved[vid] = published
    return resolved, {
        "attempted": len(urls),
        "resolved": len(resolved),
        "returncode": int(p.returncode),
        "diagnostic_tail": (p.stderr or "")[-2000:],
    }


def discover_youtube(profile: dict, source: dict, cutoff: datetime, end: datetime, discovery_limit: int) -> dict:
    target = max(1, int(discovery_limit))
    entries: list[dict] = []
    shared_channel = bool(source.get("shared_channel"))
    required_attribution_term = str(source.get("required_attribution_term") or "").strip()
    if shared_channel and not required_attribution_term:
        raise ValueError("SHARED_CHANNEL_ATTRIBUTION_RULE_MISSING")
    if len(required_attribution_term) > 120 or any(ord(ch) < 32 for ch in required_attribution_term):
        raise ValueError("BAD_REQUIRED_ATTRIBUTION_TERM")
    diag: dict = {}
    probed: dict[str, str] = {}
    probe_diag: dict = {}
    attempts: list[dict] = []

    while True:
        entries, diag = yte.enumerate_channel(source["profile_url"], limit=target)
        probed, probe_diag = _youtube_probe_missing(entries)

        known_times: list[datetime] = []
        for entry in entries:
            raw = entry.get("published_at") or probed.get(str(entry.get("id") or ""))
            if raw:
                try:
                    known_times.append(parse_iso_utc(str(raw)))
                except (TypeError, ValueError, OverflowError):
                    continue

        window_complete = _coverage_complete(
            discovered_count=len(entries),
            requested_limit=target,
            known_times=known_times,
            cutoff=cutoff,
        )
        attempts.append({
            "requested_limit": target,
            "discovered_count": len(entries),
            "oldest_known_published_at": min(known_times).isoformat() if known_times else None,
            "window_complete": window_complete,
            "enumeration": diag,
            "metadata_probe": probe_diag,
        })
        if window_complete or target >= MAX_DISCOVERY_PER_SOURCE:
            break
        target = _next_discovery_limit(target)

    items = []
    missing_time = []
    attribution_excluded_ids = []
    for entry in entries:
        vid = str(entry.get("id") or "")
        if shared_channel and not yte.metadata_has_attribution(entry, required_attribution_term):
            attribution_excluded_ids.append(vid)
            continue
        published_raw = entry.get("published_at") or probed.get(vid)
        if not published_raw:
            missing_time.append(vid)
            continue
        try:
            published = parse_iso_utc(str(published_raw))
        except Exception:
            missing_time.append(vid)
            continue
        if cutoff <= published <= end + timedelta(minutes=5):
            items.append({
                "creator_key": profile["creator_key"],
                "platform": "YOUTUBE",
                "source_id": vid,
                "item_key": f"yt_{vid}",
                "url": str(entry.get("url") or f"https://www.youtube.com/watch?v={vid}"),
                "title": str(entry.get("title") or ""),
                "published_at": published.isoformat(),
                "profile_url": source["profile_url"],
            })

    return {
        "creator_key": profile["creator_key"],
        "platform": "YOUTUBE",
        "profile_url": source["profile_url"],
        "items": items,
        "discovery_count": len(entries),
        "discovery_limit_used": target,
        "discovery_passes": attempts,
        "window_complete": window_complete,
        "coverage_limit_reached": bool(not window_complete and target >= MAX_DISCOVERY_PER_SOURCE),
        "missing_publish_time_ids": missing_time,
        "shared_channel": shared_channel,
        "required_attribution_term": required_attribution_term or None,
        "attribution_excluded_count": len(attribution_excluded_ids),
        "attribution_excluded_ids": attribution_excluded_ids[:100],
        "discovery": diag,
        "metadata_probe": probe_diag,
    }


def _instagram_handle(source: dict) -> str:
    value = str(source.get("profile_url") or "").rstrip("/")
    handle = value.rsplit("/", 1)[-1].strip().lstrip("@")
    if not handle:
        raise ValueError("INSTAGRAM_PROFILE_URL_REQUIRED")
    return handle


def discover_instagram(profile: dict, source: dict, cutoff: datetime, end: datetime, discovery_limit: int) -> dict:
    target = min(20, max(1, int(discovery_limit)))
    handle = _instagram_handle(source)
    probe = instagram_smoke.probe_public_session(
        source["profile_url"],
        handle,
        run_index=1,
        inspect_reel_times=True,
    )
    entries = list(probe.get("reel_items") or [])
    known_times: list[datetime] = []
    items: list[dict] = []
    missing_time: list[str] = []

    for entry in entries:
        urls = instagram_smoke.extract_reel_urls(str(entry.get("url") or ""))
        if not urls:
            continue
        shortcode = urls[0].rstrip("/").rsplit("/", 1)[-1]
        raw = entry.get("published_at")
        if not raw:
            missing_time.append(shortcode)
            continue
        try:
            published = parse_iso_utc(str(raw))
        except Exception:
            missing_time.append(shortcode)
            continue
        known_times.append(published)
        if cutoff <= published <= end + timedelta(minutes=5):
            items.append({
                "creator_key": profile["creator_key"],
                "platform": "INSTAGRAM",
                "source_id": shortcode,
                "item_key": shortcode,
                "url": urls[0],
                "title": "",
                "published_at": published.isoformat(),
                "profile_url": source["profile_url"],
            })

    window_complete = _coverage_complete(
        discovered_count=len(entries),
        requested_limit=target,
        known_times=known_times,
        cutoff=cutoff,
    )
    return {
        "creator_key": profile["creator_key"],
        "platform": "INSTAGRAM",
        "profile_url": source["profile_url"],
        "items": items,
        "discovery_count": len(entries),
        "discovery_limit_used": target,
        "window_complete": window_complete,
        "coverage_limit_reached": False,
        "missing_publish_time_ids": missing_time,
        "discovery": {
            "ok": bool(probe.get("ok")),
            "reel_count": int(probe.get("reel_count") or 0),
            "blocked": bool(probe.get("blocked")),
            "media_auth_gated": bool(probe.get("media_auth_gated")),
        },
    }


def _tiktok_catalog(root: Path, creator_key: str) -> dict:
    return load_json(root / "state" / "tiktok" / f"{creator_key}_catalog.json", {"order": [], "items": {}})


def discover_tiktok(root: Path, profile: dict, source: dict, cutoff: datetime, end: datetime, discovery_limit: int) -> dict:
    handle = source["profile_url"].rstrip("/").split("@")[-1].split("/")[0]
    target = max(1, int(discovery_limit))
    discovery: dict = {}
    scanned_ids: list[str] = []
    valid_times: list[datetime] = []
    invalid_ids: list[str] = []
    attempts: list[dict] = []

    while True:
        source_cfg = {
            "creator_key": profile["creator_key"],
            "handle": handle,
            "profile_url": source["profile_url"],
            "enabled": True,
            "discovery_step": target,
            "max_catalog": int(source.get("max_catalog", 1000)),
            "max_new_downloads": 0,
        }
        tts.start_server()
        run = tts.process_source(
            root,
            source_cfg,
            max_new_override=0,
            discovery_target_override=target,
        )
        discovery = run.get("discovery") or {}
        found_count = int(discovery.get("found", target) or 0)

        catalog = _tiktok_catalog(root, profile["creator_key"])
        scan_count = min(found_count, len(catalog.get("order") or []))
        scanned_ids = [str(x) for x in (catalog.get("order") or [])[:scan_count] if x]
        valid_times = []
        invalid_ids = []
        for vid in scanned_ids:
            try:
                valid_times.append(_tiktok_published_at(vid))
            except Exception:
                invalid_ids.append(vid)

        window_complete = _coverage_complete(
            discovered_count=found_count,
            requested_limit=target,
            known_times=valid_times,
            cutoff=cutoff,
        )
        attempts.append({
            "requested_limit": target,
            "discovered_count": found_count,
            "oldest_known_published_at": min(valid_times).isoformat() if valid_times else None,
            "window_complete": window_complete,
            "discovery": discovery,
        })
        if window_complete or target >= MAX_DISCOVERY_PER_SOURCE:
            break
        target = _next_discovery_limit(target)

    catalog = _tiktok_catalog(root, profile["creator_key"])
    items = []
    for vid in scanned_ids:
        try:
            published = _tiktok_published_at(vid)
        except Exception:
            continue
        if cutoff <= published <= end + timedelta(minutes=5):
            catalog_item = (catalog.get("items") or {}).get(vid) or {}
            items.append({
                "creator_key": profile["creator_key"],
                "platform": "TIKTOK",
                "source_id": vid,
                "item_key": f"tt_{vid}",
                "url": str(catalog_item.get("url") or f"https://www.tiktok.com/@{handle}/video/{vid}"),
                "title": "",
                "published_at": published.isoformat(),
                "profile_url": source["profile_url"],
            })

    return {
        "creator_key": profile["creator_key"],
        "platform": "TIKTOK",
        "profile_url": source["profile_url"],
        "items": items,
        "discovery_count": len(scanned_ids),
        "discovery_limit_used": target,
        "discovery_passes": attempts,
        "window_complete": window_complete,
        "coverage_limit_reached": bool(not window_complete and target >= MAX_DISCOVERY_PER_SOURCE),
        "missing_publish_time_ids": invalid_ids,
        "discovery": discovery,
    }


def _ingest_youtube(root: Path, creator_key: str, ids: list[str]) -> dict:
    if not ids:
        return {"requested": 0, "completed_ids": [], "returncode": 0}
    cmd = [
        sys.executable, str(root / "app" / "influencer_evaluation.py"),
        "--root", str(root), "--creator-key", creator_key,
        "--source-platform", "YOUTUBE", "--sample-size", str(len(ids)),
        "--only-video-ids", ",".join(ids),
    ]
    p = subprocess.run(cmd, cwd=str(root / "app"))
    status = load_json(root / "state" / "creator_evaluation_status.json", {})
    completed = [str(x)[3:] for x in status.get("completed", []) if str(x).startswith("yt_")]
    return {"requested": len(ids), "completed_ids": completed, "returncode": int(p.returncode), "evaluation_state": status.get("state")}


def _ingest_instagram(
    root: Path,
    profile: dict,
    source: dict,
    ids: list[str],
    published_by_id: dict[str, str],
) -> dict:
    handle = _instagram_handle(source)
    if not ids:
        return {"requested_ids": [], "completed_ids": [], "errors": [], "queue": None}

    script = root / "app" / "instagram_ingest.py"
    cmd = [
        sys.executable,
        str(script),
        "--root", str(root),
        "--creator", handle,
        "--max-new-per-creator", str(len(ids)),
        "--transcribe-new-only",
        "--only-shortcodes", ",".join(ids),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)

    manifest_path = root / "state" / "manifest.json"
    manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
    changed = False
    completed: list[str] = []
    for source_id in ids:
        item = (manifest.get("items") or {}).get(source_id)
        if not isinstance(item, dict):
            continue
        desired = {
            "creator": profile["creator_key"],
            "source_platform": "INSTAGRAM",
            "source_id": source_id,
            "published_at": published_by_id.get(source_id),
            "permanent_source": True,
        }
        for key, value in desired.items():
            if item.get(key) != value:
                item[key] = value
                changed = True
        if item.get("download_status") == "DONE" and item.get("transcription_status") == "DONE":
            completed.append(source_id)
    if changed:
        atomic_json(manifest_path, manifest)

    queue = tts.run_research_queue(root)
    errors = []
    if p.returncode != 0:
        errors.append(f"instagram_ingest exit {p.returncode}: {(p.stderr or p.stdout or '')[-1500:]}")
    if not queue.get("ok"):
        errors.append(f"research_queue failed: {queue}")
    return {
        "requested_ids": ids,
        "completed_ids": completed,
        "errors": errors,
        "queue": queue,
        "returncode": int(p.returncode),
        "stdout_tail": (p.stdout or "")[-2000:],
        "stderr_tail": (p.stderr or "")[-2000:],
    }


def _story_transcript_path(root: Path, handle: str, identity: str) -> Path | None:
    path = root / "output" / handle / "stories" / "transcripts" / f"{identity}.txt"
    return path if path.exists() else None


def _promote_story_items(
    root: Path,
    profile: dict,
    handle: str,
    cutoff: datetime,
    max_new: int,
) -> dict:
    """Bridge current Story evidence into the canonical manifest.

    Fresh evidence is promoted once. Existing evidence is still returned as an
    available analysis candidate so repeated recent-check runs can expose the
    pending target without re-ingesting it. Evidence owned by an explicitly
    superseded creator key is re-attributed to the canonical key in place.
    """
    empty = {
        "promoted": [],
        "available": [],
        "reused_existing_count": 0,
        "reattributed_count": 0,
        "identity_aliases_retired_count": 0,
        "conflicts": [],
        "manifest_changed": False,
    }
    if max_new <= 0:
        return empty

    ephemeral_manifest = load_json(
        root / "state" / "ephemeral" / "manifest.json",
        {"schema_version": 1, "items": {}},
    )
    manifest_path = root / "state" / "manifest.json"
    manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
    manifest.setdefault("items", {})

    canonical_key = str(profile["creator_key"])
    registry = load_registry(root)
    superseded_keys = {
        str(key)
        for key, candidate in (registry.get("creators") or {}).items()
        if isinstance(candidate, dict)
        and str(candidate.get("status") or "").upper() == "DISABLED"
        and str(candidate.get("superseded_by") or "") == canonical_key
    }

    candidates: list[tuple[datetime, dict]] = []
    for item in (ephemeral_manifest.get("items") or {}).values():
        if not isinstance(item, dict):
            continue
        if str(item.get("source_type") or "").upper() != "STORY":
            continue
        if str(item.get("research_status") or "").upper() == "INVALID":
            continue
        if str(item.get("creator") or "").casefold() != handle.casefold():
            continue
        try:
            observed = parse_iso_utc(str(item.get("observed_at") or ""))
        except Exception:
            continue
        if observed < cutoff:
            continue
        candidates.append((observed, item))

    # A root-URL media identity is only a temporary fallback. If the same
    # media asset is also observed with Instagram's numeric Story id, prefer the
    # numeric identity and suppress the fallback candidate in this run.
    numeric_media_paths = {
        str(item.get("media_identity_path") or "")
        for _, item in candidates
        if item.get("story_id") and item.get("media_identity_path")
    }
    candidates = [
        (observed, item)
        for observed, item in candidates
        if not (
            not item.get("story_id")
            and str(item.get("evidence_id") or "").startswith("media-")
            and str(item.get("media_identity_path") or "") in numeric_media_paths
        )
    ]

    candidates.sort(key=lambda row: row[0], reverse=True)
    promoted: list[dict] = []
    available: list[dict] = []
    conflicts: list[dict] = []
    reused_existing_count = 0
    reattributed_count = 0
    identity_aliases_retired_count = 0
    changed = False

    def descriptor(key: str, identity: str, manifest_item: dict, observed: datetime) -> dict:
        return {
            "creator_key": canonical_key,
            "platform": "INSTAGRAM",
            "source_id": str(manifest_item.get("source_id") or f"story:{identity}"),
            "item_key": key,
            "url": manifest_item.get("url"),
            "title": "",
            "published_at": str(manifest_item.get("published_at") or observed.isoformat()),
            "profile_url": f"https://www.instagram.com/{handle}/",
            "source_subtype": "STORY",
        }

    for observed, item in candidates:
        if len(available) >= max_new:
            break
        identity = str(item.get("story_id") or item.get("evidence_id") or "").strip()
        if not identity:
            continue
        key = f"ig_story_{identity}"

        screenshot_rel = str(item.get("screenshot_file") or "").strip()
        screenshot_path = root / screenshot_rel if screenshot_rel else None
        transcript_path = _story_transcript_path(root, handle, identity)
        has_screenshot = bool(screenshot_path and screenshot_path.exists())
        has_transcript = transcript_path is not None
        if not has_screenshot and not has_transcript:
            continue

        matching_aliases: list[tuple[str, dict]] = []
        media_identity_path = str(item.get("media_identity_path") or "")
        if item.get("story_id") and media_identity_path:
            for alias_key, alias_item in manifest["items"].items():
                if alias_key == key or not isinstance(alias_item, dict):
                    continue
                if str(alias_item.get("source_platform") or "").upper() != "INSTAGRAM":
                    continue
                if str(alias_item.get("source_subtype") or "").upper() != "STORY":
                    continue
                alias_creator = str(alias_item.get("creator") or "")
                if alias_creator != canonical_key and alias_creator not in superseded_keys:
                    continue
                if not str(alias_item.get("source_id") or "").startswith("story:media-"):
                    continue
                if str(alias_item.get("media_identity_path") or "") != media_identity_path:
                    continue
                if str(alias_item.get("research_status") or "").upper() == "INVALID":
                    continue
                matching_aliases.append((alias_key, alias_item))

            for alias_key, alias_item in matching_aliases:
                alias_item["research_status"] = "INVALID"
                alias_item["invalid_reason"] = "SUPERSEDED_BY_NUMERIC_STORY_ID"
                alias_item["superseded_by"] = key
                alias_item["superseded_at"] = now_iso()
                changed = True
                identity_aliases_retired_count += 1

        existing = manifest["items"].get(key)
        if isinstance(existing, dict):
            existing_creator = str(existing.get("creator") or "")
            if existing_creator != canonical_key:
                if existing_creator in superseded_keys:
                    history = [
                        str(value)
                        for value in (existing.get("creator_key_history") or [])
                        if str(value)
                    ]
                    if existing_creator not in history:
                        history.append(existing_creator)
                    existing["creator_key_history"] = history
                    existing["creator"] = canonical_key
                    existing["creator_reattributed_at"] = now_iso()
                    changed = True
                    reattributed_count += 1
                else:
                    conflicts.append({
                        "item_key": key,
                        "existing_creator": existing_creator or None,
                        "canonical_creator": canonical_key,
                    })
                    continue

            evidence_updates = {
                "story_identity_basis": item.get("story_identity_basis"),
                "media_identity_path": item.get("media_identity_path"),
                "browser_text": str(item.get("browser_text") or ""),
                "visual_description": str(item.get("visual_description") or ""),
                "visual_description_status": item.get("visual_description_status"),
                "visual_description_source": item.get("visual_description_source"),
                "visual_description_provider": item.get("visual_description_provider"),
                "visual_description_model": item.get("visual_description_model"),
                "visual_description_generated_at": item.get("visual_description_generated_at"),
                "visual_description_error": item.get("visual_description_error"),
            }
            for field, value in evidence_updates.items():
                if existing.get(field) != value:
                    existing[field] = value
                    changed = True

            available.append(descriptor(key, identity, existing, observed))
            reused_existing_count += 1
            continue

        alias_source = matching_aliases[0][1] if matching_aliases else {}
        manifest_item = {
            "schema_version": 1,
            "creator": canonical_key,
            "source_platform": "INSTAGRAM",
            "source_subtype": "STORY",
            "source_id": f"story:{identity}",
            "url": item.get("source_url"),
            "published_at": str(alias_source.get("published_at") or observed.isoformat()),
            "published_at_basis": "ACTIVE_STORY_OBSERVED_AT",
            "observed_at": str(alias_source.get("observed_at") or observed.isoformat()),
            "identity_migrated_from": [alias_key for alias_key, _ in matching_aliases],
            "story_identity_basis": item.get("story_identity_basis"),
            "media_identity_path": item.get("media_identity_path"),
            "download_status": "DONE",
            "downloaded_at": observed.isoformat(),
            "transcription_status": "DONE" if has_transcript else "NOT_APPLICABLE",
            "transcript_txt": (
                str(transcript_path.relative_to(root)) if transcript_path is not None else None
            ),
            "transcript_source": "STORY_VIDEO" if has_transcript else None,
            "browser_text": str(item.get("browser_text") or alias_source.get("browser_text") or ""),
            "visual_description": str(item.get("visual_description") or alias_source.get("visual_description") or ""),
            "visual_description_status": item.get("visual_description_status") or alias_source.get("visual_description_status"),
            "visual_description_source": item.get("visual_description_source") or alias_source.get("visual_description_source"),
            "visual_description_provider": item.get("visual_description_provider") or alias_source.get("visual_description_provider"),
            "visual_description_model": item.get("visual_description_model") or alias_source.get("visual_description_model"),
            "visual_description_generated_at": item.get("visual_description_generated_at") or alias_source.get("visual_description_generated_at"),
            "visual_description_error": item.get("visual_description_error") or alias_source.get("visual_description_error"),
            "screenshot_file": screenshot_rel or None,
            "visual_evidence_status": "DONE" if has_screenshot else "NOT_AVAILABLE",
            "visual_evidence_index": screenshot_rel or None,
            "visual_frame_count": 1 if has_screenshot else 0,
            "visual_capture_strategy": "INSTAGRAM_STORY_SCREENSHOT",
            "full_video_persisted": bool(has_transcript),
            "media_retention": "EPHEMERAL_CAPTURE",
            "permanent_source": True,
            "research_status": "PENDING",
            "source_class": "INFLUENCER_DISCOVERY_SECONDARY",
        }
        manifest["items"][key] = manifest_item
        changed = True
        row = descriptor(key, identity, manifest_item, observed)
        if matching_aliases:
            reused_existing_count += 1
        else:
            promoted.append(row)
        available.append(row)

    if changed:
        atomic_json(manifest_path, manifest)

    return {
        "promoted": promoted,
        "available": available,
        "reused_existing_count": reused_existing_count,
        "reattributed_count": reattributed_count,
        "identity_aliases_retired_count": identity_aliases_retired_count,
        "conflicts": conflicts,
        "manifest_changed": changed,
    }


def _ingest_instagram_stories(
    root: Path,
    profile: dict,
    source: dict,
    cutoff: datetime,
    max_new: int,
) -> dict:
    handle = _instagram_handle(source)
    if max_new <= 0:
        return {"promoted": [], "capture": None, "queue": None, "warnings": []}

    run = ephemeral.run_one(
        root=root,
        mode="stories",
        creator=handle,
        highlight_label=None,
        force=False,
        max_items=min(6, max(1, max_new + 2)),
    )
    bridge = _promote_story_items(root, profile, handle, cutoff, max_new)
    promoted = list(bridge.get("promoted") or [])
    available = list(bridge.get("available") or [])
    queue = tts.run_research_queue(root) if bridge.get("manifest_changed") else None

    warnings: list[str] = []
    capture = run.get("capture") or {}
    reason = str(capture.get("reason") or "")
    benign_reasons = {
        "OK",
        "ENDED_OR_EXITED_STORY_VIEW",
        "NO_ACTIVE_STORY_OR_STORY_VIEW_REDIRECTED",
    }
    if run.get("state") == "DONE_WITH_ERRORS" and reason not in benign_reasons:
        warnings.extend(str(x) for x in (run.get("errors") or []))
    visual_errors = list((run.get("visual_enrichment") or {}).get("errors") or [])
    if visual_errors:
        warnings.extend(f"visual_enrichment: {error}" for error in visual_errors)
    if queue is not None and not queue.get("ok"):
        warnings.append(f"research_queue failed: {queue}")
    if bridge.get("conflicts"):
        warnings.append(
            f"STORY_ITEM_OWNERSHIP_CONFLICT:{len(bridge.get('conflicts') or [])}"
        )

    return {
        "promoted": promoted,
        "available": available,
        "reused_existing_count": int(bridge.get("reused_existing_count") or 0),
        "reattributed_count": int(bridge.get("reattributed_count") or 0),
        "identity_aliases_retired_count": int(
            bridge.get("identity_aliases_retired_count") or 0
        ),
        "conflicts": list(bridge.get("conflicts") or []),
        "capture": capture,
        "queue": queue,
        "warnings": warnings,
        "state": run.get("state"),
    }


def _ingest_tiktok(root: Path, profile: dict, source: dict, ids: list[str], discovery_limit: int) -> dict:
    if not ids:
        return {"requested": 0, "completed_ids": [], "returncode": 0, "failures": []}
    handle = source["profile_url"].rstrip("/").split("@")[-1].split("/")[0]
    source_cfg = {
        "creator_key": profile["creator_key"], "handle": handle, "profile_url": source["profile_url"],
        "enabled": True, "discovery_step": discovery_limit,
        "max_catalog": int(source.get("max_catalog", 1000)), "max_new_downloads": len(ids),
    }
    tts.start_server()
    obj = tts.process_source(
        root,
        source_cfg,
        max_new_override=len(ids),
        include_video_ids=set(ids),
        discovery_target_override=discovery_limit,
    )
    if obj.get("completed"):
        tts.run_research_queue(root)
    completed = [str(x.get("video_id")) for x in obj.get("completed", []) if x.get("video_id")]
    failures = obj.get("failures", []) if isinstance(obj.get("failures"), list) else []
    return {"requested": len(ids), "completed_ids": completed, "returncode": 0 if not failures else 1, "failures": failures}


def _queue_targets(root: Path, item_keys: set[str]) -> list[dict]:
    queue = load_json(root / "state" / "research_queue.json", {"items": []})
    out = []
    for item in queue.get("items", []) if isinstance(queue.get("items"), list) else []:
        qid = str(item.get("queue_id") or item.get("shortcode") or "")
        if qid not in item_keys:
            continue
        if str(item.get("analysis_owner", "")).upper() != "EKONOMI":
            continue
        if str(item.get("analysis_status", "")).upper() != "PENDING_ANALYSIS":
            continue
        evidence = str(
            item.get("analysis_evidence_text")
            or item.get("transcript_text")
            or item.get("visual_description")
            or item.get("visible_text")
            or item.get("caption")
            or ""
        ).strip()
        out.append({
            "queue_id": qid,
            "creator_key": item.get("creator"),
            "platform": item.get("source_platform"),
            "source_id": item.get("source_id"),
            "published_at": item.get("published_at"),
            "published_at_basis": item.get("published_at_basis"),
            "observed_at": item.get("observed_at"),
            "source_url": item.get("source_url"),
            "source_subtype": item.get("source_subtype"),
            "caption": item.get("caption"),
            "analysis_content_status": item.get("analysis_content_status"),
            "analysis_content_reason": item.get("analysis_content_reason"),
            "transcript_source": item.get("transcript_source"),
            "word_count": item.get("word_count"),
            "visual_description": item.get("visual_description"),
            "visual_description_status": item.get("visual_description_status"),
            "visual_description_source": item.get("visual_description_source"),
            "visual_description_provider": item.get("visual_description_provider"),
            "visual_description_model": item.get("visual_description_model"),
            "visual_evidence_status": item.get("visual_evidence_status"),
            "visual_evidence_index": item.get("visual_evidence_index"),
            "visual_frame_count": item.get("visual_frame_count"),
            "visual_capture_strategy": item.get("visual_capture_strategy"),
            "screenshot_file": item.get("screenshot_file"),
            "media_retention": item.get("media_retention"),
            "analysis_evidence_text": evidence[:MAX_ANALYSIS_EVIDENCE_CHARS],
            "analysis_evidence_truncated": len(evidence) > MAX_ANALYSIS_EVIDENCE_CHARS,
        })
    return out


def _main_impl() -> int:
    ap = argparse.ArgumentParser(description="InfluencerResearch recent-window discovery + automatic ingestion")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--scope", choices=["MONITORED", "ALL_REGISTERED"], default="MONITORED")
    ap.add_argument("--creator-keys", default="")
    ap.add_argument("--window", choices=["TODAY", "LAST_7_DAYS", "LAST_N_DAYS"], default="TODAY")
    ap.add_argument("--lookback-days", type=int, default=None)
    ap.add_argument("--max-items", type=int, default=10)
    args = ap.parse_args()

    root = args.root.resolve()
    max_items = max(1, min(int(args.max_items), 20))
    creator_keys = [x.strip().lower() for x in str(args.creator_keys or "").split(",") if x.strip()]
    cutoff, end, window_meta = resolve_window(args.window, args.lookback_days)
    discovery_limit = min(MAX_DISCOVERY_PER_SOURCE, max(MIN_DISCOVERY_PER_SOURCE, max_items + 5))
    started = now_iso()
    status_path = root / "state" / "creator_recent_check_status.json"
    atomic_json(status_path, {
        "schema_version": 1,
        "recent_check_version": RECENT_CHECK_VERSION,
        "state": "RUNNING",
        "started_at": started,
        "scope": args.scope,
        "creator_filters": creator_keys or None,
        "window": window_meta,
        "cutoff_at": cutoff.isoformat(),
        "max_items": max_items,
        "auto_ingest": True,
        "auto_analysis_owner": "EKONOMI",
    })

    discoveries = []
    errors = []
    source_map: dict[tuple[str, str], tuple[dict, dict]] = {}
    try:
        selected = select_profiles_and_sources(root, args.scope, creator_keys)
        for profile, sources in selected:
            for source in sources:
                platform = str(source.get("platform", "")).upper()
                source_map[(str(profile["creator_key"]), platform)] = (profile, source)
                try:
                    if platform == "YOUTUBE":
                        d = discover_youtube(profile, source, cutoff, end, discovery_limit)
                    elif platform == "TIKTOK":
                        d = discover_tiktok(root, profile, source, cutoff, end, discovery_limit)
                    elif platform == "INSTAGRAM":
                        d = discover_instagram(profile, source, cutoff, end, discovery_limit)
                    else:
                        continue
                    discoveries.append(d)
                    if d.get("missing_publish_time_ids"):
                        errors.append({
                            "creator_key": profile["creator_key"], "platform": platform,
                            "stage": "PUBLISH_TIME", "error": f"UNRESOLVED_IDS:{len(d['missing_publish_time_ids'])}",
                        })
                except Exception as exc:
                    errors.append({"creator_key": profile["creator_key"], "platform": platform, "stage": "DISCOVERY", "error": f"{type(exc).__name__}: {exc}"})

        all_recent = [item for d in discoveries for item in d.get("items", [])]
        all_recent.sort(key=lambda x: x["published_at"], reverse=True)
        done_by_platform = {p: manifest_done_ids(root, p) for p in SUPPORTED_PLATFORMS}
        for item in all_recent:
            item["already_ingested"] = item["source_id"] in done_by_platform[item["platform"]]

        pending = [x for x in all_recent if not x["already_ingested"]]
        selected_pending = pending[:max_items]
        deferred = pending[max_items:]

        grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
        selected_metadata: dict[tuple[str, str, str], dict] = {}
        for item in selected_pending:
            grouped[(item["creator_key"], item["platform"])].append(item["source_id"])
            selected_metadata[(item["creator_key"], item["platform"], item["source_id"])] = item

        ingestion_results = []
        for (creator_key, platform), ids in grouped.items():
            profile, source = source_map[(creator_key, platform)]
            try:
                if platform == "YOUTUBE":
                    ing = _ingest_youtube(root, creator_key, ids)
                elif platform == "TIKTOK":
                    ing = _ingest_tiktok(root, profile, source, ids, discovery_limit)
                elif platform == "INSTAGRAM":
                    ing = _ingest_instagram(
                        root,
                        profile,
                        source,
                        ids,
                        {
                            source_id: str(
                                selected_metadata[(creator_key, platform, source_id)].get("published_at") or ""
                            )
                            for source_id in ids
                        },
                    )
                else:
                    continue
                ingestion_results.append({"creator_key": creator_key, "platform": platform, **ing})
                if int(ing.get("returncode", 0)) != 0:
                    errors.append({"creator_key": creator_key, "platform": platform, "stage": "INGESTION", "error": f"RETURNCODE:{ing.get('returncode')}"})
            except Exception as exc:
                errors.append({"creator_key": creator_key, "platform": platform, "stage": "INGESTION", "error": f"{type(exc).__name__}: {exc}"})

        story_results = []
        story_selected: list[dict] = []
        story_available: list[dict] = []
        story_reused_existing_count = 0
        story_reattributed_count = 0
        story_identity_aliases_retired_count = 0
        remaining_story_slots = max(0, max_items - len(selected_pending))
        if remaining_story_slots:
            seen_instagram_creators: set[str] = set()
            for profile, sources in selected:
                if remaining_story_slots <= 0:
                    break
                instagram_sources = [
                    source for source in sources
                    if str(source.get("platform") or "").upper() == "INSTAGRAM"
                ]
                if not instagram_sources:
                    continue
                creator_key = str(profile["creator_key"])
                if creator_key in seen_instagram_creators:
                    continue
                seen_instagram_creators.add(creator_key)
                try:
                    story_result = _ingest_instagram_stories(
                        root,
                        profile,
                        instagram_sources[0],
                        cutoff,
                        remaining_story_slots,
                    )
                    promoted = list(story_result.get("promoted") or [])
                    available = list(story_result.get("available") or [])
                    story_selected.extend(promoted)
                    story_available.extend(available)
                    story_reused_existing_count += int(
                        story_result.get("reused_existing_count") or 0
                    )
                    story_reattributed_count += int(
                        story_result.get("reattributed_count") or 0
                    )
                    story_identity_aliases_retired_count += int(
                        story_result.get("identity_aliases_retired_count") or 0
                    )
                    remaining_story_slots -= len(promoted)
                    story_results.append({"creator_key": creator_key, **story_result})
                    if story_result.get("warnings"):
                        errors.append({
                            "creator_key": creator_key,
                            "platform": "INSTAGRAM",
                            "stage": "STORY_INGESTION",
                            "error": "; ".join(story_result["warnings"])[:2000],
                        })
                except Exception as exc:
                    errors.append({
                        "creator_key": creator_key,
                        "platform": "INSTAGRAM",
                        "stage": "STORY_INGESTION",
                        "error": f"{type(exc).__name__}: {exc}",
                    })

        # Rebuild the queue even when all recent items were already ingested. This
        # migrates older evidence through the current content-readiness gate instead
        # of silently treating an empty/weak transcript as analysis-ready.
        queue_refresh = tts.run_research_queue(root)
        if not queue_refresh.get("ok"):
            errors.append({
                "stage": "RESEARCH_QUEUE",
                "error": f"QUEUE_REFRESH_FAILED:{queue_refresh}",
            })
        queue_snapshot = load_json(
            root / "state" / "research_queue.json",
            {
                "items": [],
                "insufficient_content_items": [],
                "deferred_extraction_items": [],
                "extraction_error_items": [],
                "pending_extraction_items": [],
            },
        )

        def by_queue_id(field: str) -> dict[str, dict]:
            return {
                str(item.get("queue_id") or ""): item
                for item in (queue_snapshot.get(field) or [])
                if isinstance(item, dict) and item.get("queue_id")
            }

        insufficient_by_key = by_queue_id("insufficient_content_items")
        deferred_extraction_by_key = by_queue_id("deferred_extraction_items")
        extraction_error_by_key = by_queue_id("extraction_error_items")
        pending_extraction_by_key = by_queue_id("pending_extraction_items")

        # Pending analysis can include a recent item ingested by an earlier run.
        # Return those targets too; do not require a fresh download in this run.
        # Stories captured by an earlier run remain valid candidates while they
        # are active/current. Fresh promotion is not required for target exposure.
        story_available_by_key = {
            str(item["item_key"]): item
            for item in story_available
            if item.get("item_key")
        }
        story_available = list(story_available_by_key.values())
        analysis_candidate_keys = {x["item_key"] for x in all_recent}
        analysis_candidate_keys.update(x["item_key"] for x in story_available)
        analysis_targets = _queue_targets(root, analysis_candidate_keys)
        completed_keys = {x["queue_id"] for x in analysis_targets}

        def annotate_content_state(item: dict) -> None:
            item["queued_for_analysis"] = item["item_key"] in completed_keys
            qid = item["item_key"]
            candidates = (
                ("INSUFFICIENT_CONTENT", insufficient_by_key.get(qid)),
                ("DEFERRED_EXTRACTION", deferred_extraction_by_key.get(qid)),
                ("EXTRACTION_ERROR", extraction_error_by_key.get(qid)),
                ("PENDING_EXTRACTION", pending_extraction_by_key.get(qid)),
            )
            for status_name, state_item in candidates:
                if state_item:
                    item["analysis_content_status"] = status_name
                    item["analysis_content_reason"] = state_item.get("reason")
                    item["analysis_content_retry_after"] = state_item.get(
                        "visual_description_retry_after"
                    )
                    item["analysis_content_error"] = state_item.get(
                        "visual_description_error"
                    )
                    break

        for item in all_recent:
            annotate_content_state(item)
        for item in story_available:
            annotate_content_state(item)

        recent_content_items = all_recent + story_available
        insufficient_recent = [
            {
                "item_key": item["item_key"],
                "creator_key": item.get("creator_key"),
                "platform": item.get("platform"),
                "source_id": item.get("source_id"),
                "published_at": item.get("published_at"),
                "reason": item.get("analysis_content_reason"),
            }
            for item in recent_content_items
            if item.get("analysis_content_status") == "INSUFFICIENT_CONTENT"
        ]

        def compact_nonready(status_name: str) -> list[dict]:
            return [
                {
                    "item_key": item["item_key"],
                    "creator_key": item.get("creator_key"),
                    "platform": item.get("platform"),
                    "source_id": item.get("source_id"),
                    "published_at": item.get("published_at"),
                    "reason": item.get("analysis_content_reason"),
                    "retry_after": item.get("analysis_content_retry_after"),
                    "error": item.get("analysis_content_error"),
                }
                for item in recent_content_items
                if item.get("analysis_content_status") == status_name
            ]

        deferred_extraction_recent = compact_nonready("DEFERRED_EXTRACTION")
        extraction_error_recent = compact_nonready("EXTRACTION_ERROR")
        pending_extraction_recent = compact_nonready("PENDING_EXTRACTION")
        if insufficient_recent:
            errors.append({
                "stage": "CONTENT_EXTRACTION",
                "error": f"INSUFFICIENT_CONTENT:{len(insufficient_recent)}",
                "items": insufficient_recent[:100],
            })
        if extraction_error_recent:
            errors.append({
                "stage": "CONTENT_EXTRACTION",
                "error": f"EXTRACTION_ERROR:{len(extraction_error_recent)}",
                "items": extraction_error_recent[:100],
            })

        incomplete_windows = [
            {"creator_key": d["creator_key"], "platform": d["platform"]}
            for d in discoveries if not d.get("window_complete")
        ]
        if incomplete_windows:
            errors.append({"stage": "COVERAGE", "error": "WINDOW_MAY_BE_TRUNCATED", "sources": incomplete_windows})

        extraction_pending = bool(
            deferred_extraction_recent
            or extraction_error_recent
            or pending_extraction_recent
        )
        final_state = (
            "COMPLETE"
            if not errors and not extraction_pending
            else ("PARTIAL" if discoveries else "FAILED")
        )
        status = {
            "schema_version": 1,
            "recent_check_version": RECENT_CHECK_VERSION,
            "state": final_state,
            "started_at": started,
            "finished_at": now_iso(),
            "scope": args.scope,
            "creator_filters": creator_keys or None,
            "window": window_meta,
            "cutoff_at": cutoff.isoformat(),
            "checked_until": end.isoformat(),
            "max_items": max_items,
            "discovery_limit_per_source": discovery_limit,
            "max_discovery_per_source": MAX_DISCOVERY_PER_SOURCE,
            "max_discovery_used_per_source": max(
                [int(d.get("discovery_limit_used") or discovery_limit) for d in discoveries] or [discovery_limit]
            ),
            "source_count": len(discoveries),
            "recent_found_count": len(all_recent),
            "already_ingested_count": sum(1 for x in all_recent if x["already_ingested"]),
            "pending_found_count": len(pending),
            "selected_for_ingestion_count": len(selected_pending) + len(story_selected),
            "selected_standard_ingestion_count": len(selected_pending),
            "story_current_count": len(story_available),
            "story_newly_promoted_count": len(story_selected),
            "story_reused_existing_count": story_reused_existing_count,
            "story_reattributed_count": story_reattributed_count,
            "story_identity_aliases_retired_count": story_identity_aliases_retired_count,
            "deferred_due_to_cap_count": len(deferred),
            "queued_for_analysis_count": len(analysis_targets),
            "insufficient_content_count": len(insufficient_recent),
            "insufficient_content_items": insufficient_recent[:100],
            "deferred_extraction_count": len(deferred_extraction_recent),
            "deferred_extraction_items": deferred_extraction_recent[:100],
            "extraction_error_count": len(extraction_error_recent),
            "extraction_error_items": extraction_error_recent[:100],
            "pending_extraction_count": len(pending_extraction_recent),
            "pending_extraction_items": pending_extraction_recent[:100],
            "auto_ingest": True,
            "auto_analysis_contract": {
                "enabled": True,
                "owner": "EKONOMI",
                "behavior": "ANALYZE_PENDING_TARGETS_IMMEDIATELY_WITHOUT_EXTRA_USER_CONFIRMATION",
                "note": "Local runtime prepares evidence/queue; the requesting Ekonomi session performs model analysis.",
            },
            "recent_items": all_recent[:100],
            "selected_items": selected_pending + story_selected,
            "story_items": story_available[:100],
            "deferred_items": deferred[:100],
            "analysis_targets": analysis_targets,
            "discoveries": discoveries,
            "ingestion_results": ingestion_results,
            "story_ingestion_results": story_results,
            "error_count": len(errors),
            "errors": errors,
        }
        atomic_json(status_path, status)
        print(json.dumps(status, ensure_ascii=True, indent=2))
        return 0 if final_state == "COMPLETE" else (1 if final_state == "PARTIAL" else 2)
    except Exception as exc:
        status = {
            "schema_version": 1,
            "recent_check_version": RECENT_CHECK_VERSION,
            "state": "FAILED",
            "started_at": started,
            "finished_at": now_iso(),
            "scope": args.scope,
            "creator_filters": creator_keys or None,
            "window": window_meta,
            "cutoff_at": cutoff.isoformat(),
            "error": f"{type(exc).__name__}: {exc}",
        }
        atomic_json(status_path, status)
        print(json.dumps(status, ensure_ascii=True, indent=2))
        return 2


def main() -> int:
    try:
        return _main_impl()
    finally:
        tts.stop_server(deadline=time.monotonic() + 8.0)


if __name__ == "__main__":
    raise SystemExit(main())
