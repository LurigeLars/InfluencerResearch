from __future__ import annotations

"""Bounded single-video ingestion. No creator registration or profile discovery."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from evaluation_progress import heartbeat, terminalize

STATUS_NAME = "video_url_analysis_status.json"
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
TIKTOK_HANDLE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
TIKTOK_ID = re.compile(r"[0-9]{8,24}\Z")
YOUTUBE_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
READY_DISPOSITIONS = frozenset({"QUEUED", "FINALIZED", "DUPLICATE"})


def canonical_video_url(value: str) -> tuple[str, str, str, str | None]:
    """Return (platform, canonical_url, video_id, TikTok_handle).

    Only full, public YouTube and TikTok video links are accepted. No redirects,
    arbitrary hosts, shortened TikTok links, playlists, profiles or URL credentials.
    """
    raw = str(value or "").strip()
    if not raw or len(raw) > 500 or any(ord(c) < 33 for c in raw):
        raise ValueError("BAD_VIDEO_URL")
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.port is not None:
            raise ValueError("BAD_VIDEO_URL_PORT")
    except ValueError as exc:
        raise ValueError("BAD_VIDEO_URL") from exc
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("BAD_VIDEO_URL")
    parts = parsed.path.strip("/").split("/")
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        if host == "youtu.be" and len(parts) == 1:
            video_id = parts[0]
        elif host != "youtu.be" and parts == ["watch"]:
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif host != "youtu.be" and len(parts) == 2 and parts[0] in {"shorts", "live"}:
            video_id = parts[1]
        else:
            raise ValueError("UNSUPPORTED_VIDEO_URL")
        if not YOUTUBE_ID.fullmatch(video_id):
            raise ValueError("BAD_YOUTUBE_VIDEO_ID")
        return "YOUTUBE", f"https://www.youtube.com/watch?v={video_id}", video_id, None
    if host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
        if (len(parts) != 3 or not parts[0].startswith("@")
                or parts[1] != "video"):
            raise ValueError("UNSUPPORTED_VIDEO_URL")
        handle, video_id = parts[0][1:], parts[2]
        if not TIKTOK_HANDLE.fullmatch(handle) or not TIKTOK_ID.fullmatch(video_id):
            raise ValueError("BAD_TIKTOK_VIDEO_ID")
        return "TIKTOK", f"https://www.tiktok.com/@{handle}/video/{video_id}", video_id, handle
    raise ValueError("UNSUPPORTED_VIDEO_HOST")


def _youtube(root: Path, url: str, video_id: str, status_path: Path) -> dict:
    import youtube_creator_evaluation as youtube

    heartbeat(status_path, "METADATA", source_platform="YOUTUBE", source_id=video_id)
    base_args, _ = youtube._yt_base_args()
    # Fixed executable/options; only the strict-canonical URL is passed after --.
    proc = subprocess.run(
        [*base_args, "--skip-download", "--dump-single-json", "--", url],
        capture_output=True, text=True, timeout=120, shell=False,
    )
    if proc.returncode:
        raise RuntimeError("YOUTUBE_METADATA_UNAVAILABLE")
    try:
        metadata = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("YOUTUBE_METADATA_INVALID") from exc
    if str(metadata.get("id")) != video_id:
        raise RuntimeError("YOUTUBE_VIDEO_ID_MISMATCH")
    channel_id = str(metadata.get("channel_id") or "")
    if not YOUTUBE_CHANNEL_ID.fullmatch(channel_id):
        raise RuntimeError("YOUTUBE_CHANNEL_ID_UNVERIFIED")
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    creator_key = "adhoc" + hashlib.sha256(channel_id.encode()).hexdigest()[:12]
    title = str(metadata.get("channel") or metadata.get("uploader") or "YouTube creator")[:120]

    # Delegate captions, Whisper fallback, visual evidence, dedupe and queue
    # reconciliation to the established exact-ID evaluation path.
    previous_argv = sys.argv
    try:
        sys.argv = [
            str(Path(youtube.__file__)), "--root", str(root),
            "--channel-url", channel_url, "--creator-key", creator_key,
            "--creator-name", title, "--sample-size", "1",
            "--only-video-ids", video_id, "--status-path", str(status_path),
            "--evaluation-mode", "SINGLE_VIDEO_URL",
        ]
        returncode = youtube.main()
    finally:
        sys.argv = previous_argv

    detail = youtube.load_json(status_path, {})
    disposition = (detail.get("delivery_dispositions") or {}).get(f"yt_{video_id}")
    complete = detail.get("state") in {"COMPLETE", "NO_NEW_CONTENT"} and disposition in READY_DISPOSITIONS
    return {
        "state": "COMPLETE" if complete else ("PARTIAL" if detail.get("completed_count") else "FAILED"),
        "queue_id": f"yt_{video_id}",
        "disposition": disposition or "UNAVAILABLE",
        "creator_key": creator_key,
        "adapter_state": detail.get("state"),
        "adapter_returncode": returncode,
        "error": detail.get("shortfall_reason") if not complete else None,
    }


def _tiktok(root: Path, url: str, video_id: str, handle: str, status_path: Path) -> dict:
    import tiktok_camofox_sync as tiktok
    from youtube_creator_evaluation import reconcile_delivery

    creator_key = "adhoc" + hashlib.sha256(handle.casefold().encode()).hexdigest()[:12]
    profile = f"https://www.tiktok.com/@{handle}"
    catalog_path = root / "state" / "tiktok" / f"{creator_key}_catalog.json"
    catalog = tiktok.load_json(catalog_path, {
        "schema_version": 1, "app_version": tiktok.APP_VERSION,
        "profile_url": profile, "order": [], "items": {},
    })
    # Preseed the exact URL. process_source skips profile browsing when the
    # requested ID is present in the catalog, and processes no other videos.
    catalog = tiktok.merge_catalog(catalog, [url], profile_url=profile)
    tiktok.atomic_json(catalog_path, catalog)

    def progress(phase: str, metrics: dict) -> None:
        heartbeat(status_path, phase, source_platform="TIKTOK", source_id=video_id, **metrics)

    ingest = tiktok.process_source(
        root,
        {
            "creator_key": creator_key, "handle": handle, "profile_url": profile,
            "discovery_step": 1, "max_catalog": max(1, len(catalog["items"])),
        },
        max_new_override=1,
        include_video_ids={video_id},
        discovery_target_override=1,
        progress_callback=progress,
    )
    queue_id = f"tt_{video_id}"
    manifest_path = root / "state" / "manifest.json"
    manifest = tiktok.load_json(manifest_path, {"schema_version": 1, "items": {}})
    item = manifest.get("items", {}).get(queue_id)
    if not isinstance(item, dict) or item.get("transcription_status") != "DONE":
        failures = ingest.get("failures") or []
        return {
            "state": "FAILED", "queue_id": queue_id,
            "disposition": "UNAVAILABLE",
            "error": str(failures[0].get("stage") or "VIDEO_NOT_INGESTED") if failures else "VIDEO_NOT_INGESTED",
        }

    item["evaluation_mode"] = "SINGLE_VIDEO_URL"
    item["permanent_source"] = False
    item["evaluation_source_profile"] = profile
    tiktok.atomic_json(manifest_path, manifest)

    heartbeat(status_path, "QUEUE_WRITE", source_platform="TIKTOK", source_id=video_id)
    queue_result, _, delivery = reconcile_delivery(root, [queue_id])
    disposition = delivery["dispositions"].get(queue_id, "UNAVAILABLE")
    complete = bool(queue_result.get("ok")) and disposition in READY_DISPOSITIONS
    return {
        "state": "COMPLETE" if complete else "PARTIAL",
        "queue_id": queue_id, "creator_key": creator_key,
        "disposition": disposition,
        "error": None if complete else "EVIDENCE_NOT_ANALYSIS_READY",
        "ingested": bool(ingest.get("completed_new") or item.get("download_status") == "DONE"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze one public video URL")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--video-url", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    status_path = root / "state" / STATUS_NAME
    from datetime import datetime, timezone
    from youtube_creator_evaluation import utc_now, atomic_json

    started = datetime.now(timezone.utc).isoformat()
    try:
        platform, url, video_id, handle = canonical_video_url(args.video_url)
    except ValueError as exc:
        atomic_json(status_path, {
            "schema_version": 1, "state": "FAILED", "started_at": started,
            "finished_at": utc_now(), "error": str(exc),
        })
        return 2

    atomic_json(status_path, {
        "schema_version": 1, "state": "RUNNING", "started_at": started,
        "updated_at": started, "source_platform": platform,
        "source_id": video_id, "video_url": url, "queue_id": f"{'yt' if platform == 'YOUTUBE' else 'tt'}_{video_id}",
    })
    heartbeat(status_path, "METADATA", source_platform=platform, source_id=video_id)
    try:
        if platform == "YOUTUBE":
            result = _youtube(root, url, video_id, status_path)
        else:
            result = _tiktok(root, url, video_id, handle or "", status_path)
        state = result.pop("state")
        terminalize(
            status_path, state,
            video_url=url, source_platform=platform, source_id=video_id,
            **result,
        )
        return 0 if state == "COMPLETE" else 1
    except Exception as exc:
        # No source page or subprocess stderr is returned to the client.
        terminalize(
            status_path, "FAILED", video_url=url,
            source_platform=platform, source_id=video_id,
            error=f"{type(exc).__name__}:{str(exc)[:160]}",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
