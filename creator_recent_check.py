from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from creator_registry import get_creator, load_registry, select_monitor_sources
from creator_monitor import manifest_done_ids, _tiktok_published_at
import youtube_creator_evaluation as yte
import tiktok_camofox_sync as tts

RECENT_CHECK_VERSION = "0.1.5"
SUPPORTED_PLATFORMS = {"YOUTUBE", "TIKTOK"}
MAX_DISCOVERY_PER_SOURCE = 200
MIN_DISCOVERY_PER_SOURCE = 15


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
        # Use the host OS local timezone instead of Python's optional IANA tzdata
        # package. The InfluencerResearch Windows host operates in Europe/Stockholm.
        # time.mktime() delegates local/DST conversion to the Windows timezone rules.
        local_now = datetime.now()
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_epoch = time.mktime(start_local.timetuple())
        start = datetime.fromtimestamp(start_epoch, timezone.utc)
        offset = datetime.now().astimezone().utcoffset()
        offset_minutes = int(offset.total_seconds() // 60) if offset is not None else None
        meta = {
            "window": "TODAY",
            "timezone": "Europe/Stockholm",
            "timezone_source": "WINDOWS_SYSTEM_LOCAL",
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

def _eligible_evaluation_sources(profile: dict) -> list[dict]:
    return sorted(
        [
            s for s in profile.get("sources", [])
            if s.get("enabled")
            and s.get("evaluation_enabled")
            and str(s.get("platform", "")).upper() in SUPPORTED_PLATFORMS
        ],
        key=lambda s: (int(s.get("priority", 100)), str(s.get("platform", ""))),
    )


def select_profiles_and_sources(root: Path, scope: str, creator_keys: list[str]) -> list[tuple[dict, list[dict]]]:
    if creator_keys:
        out = []
        for key in creator_keys:
            profile = get_creator(root, key)
            sources = _eligible_evaluation_sources(profile)
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
                out.append((profile, sources))
    elif mode == "ALL_REGISTERED":
        for profile in profiles:
            sources = _eligible_evaluation_sources(profile)
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
                except Exception:
                    pass

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
        out.append({
            "queue_id": qid,
            "creator_key": item.get("creator"),
            "platform": item.get("source_platform"),
            "source_id": item.get("source_id"),
            "published_at": item.get("published_at"),
            "source_url": item.get("source_url"),
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
        for item in selected_pending:
            grouped[(item["creator_key"], item["platform"])].append(item["source_id"])

        ingestion_results = []
        for (creator_key, platform), ids in grouped.items():
            profile, source = source_map[(creator_key, platform)]
            try:
                if platform == "YOUTUBE":
                    ing = _ingest_youtube(root, creator_key, ids)
                else:
                    ing = _ingest_tiktok(root, profile, source, ids, discovery_limit)
                ingestion_results.append({"creator_key": creator_key, "platform": platform, **ing})
                if int(ing.get("returncode", 0)) != 0:
                    errors.append({"creator_key": creator_key, "platform": platform, "stage": "INGESTION", "error": f"RETURNCODE:{ing.get('returncode')}"})
            except Exception as exc:
                errors.append({"creator_key": creator_key, "platform": platform, "stage": "INGESTION", "error": f"{type(exc).__name__}: {exc}"})

        selected_keys = {x["item_key"] for x in selected_pending}
        analysis_targets = _queue_targets(root, selected_keys)
        completed_keys = {x["queue_id"] for x in analysis_targets}
        for item in selected_pending:
            item["queued_for_analysis"] = item["item_key"] in completed_keys

        incomplete_windows = [
            {"creator_key": d["creator_key"], "platform": d["platform"]}
            for d in discoveries if not d.get("window_complete")
        ]
        if incomplete_windows:
            errors.append({"stage": "COVERAGE", "error": "WINDOW_MAY_BE_TRUNCATED", "sources": incomplete_windows})

        final_state = "COMPLETE" if not errors else ("PARTIAL" if discoveries else "FAILED")
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
            "selected_for_ingestion_count": len(selected_pending),
            "deferred_due_to_cap_count": len(deferred),
            "queued_for_analysis_count": len(analysis_targets),
            "auto_ingest": True,
            "auto_analysis_contract": {
                "enabled": True,
                "owner": "EKONOMI",
                "behavior": "ANALYZE_PENDING_TARGETS_IMMEDIATELY_WITHOUT_EXTRA_USER_CONFIRMATION",
                "note": "Local runtime prepares evidence/queue; the requesting Ekonomi session performs model analysis.",
            },
            "recent_items": all_recent[:100],
            "selected_items": selected_pending,
            "deferred_items": deferred[:100],
            "analysis_targets": analysis_targets,
            "discoveries": discoveries,
            "ingestion_results": ingestion_results,
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
