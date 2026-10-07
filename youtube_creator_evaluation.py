from __future__ import annotations

import argparse
import contextlib
import errno
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from evaluation_progress import heartbeat, sample_outcome, terminalize
import video_visual_evidence as vve
from urllib.parse import urlparse

YOUTUBE_EVAL_VERSION = "0.8.6"
DEFAULT_MAX_UNPINNED_WHISPER_DURATION_SECONDS = 20 * 60
DEFAULT_VISUAL_CAPTURE_MAX_CHILD_RSS_MB = 768
VISUAL_CAPTURE_MAX_FRAMES_PER_BRANCH = 120
VISUAL_CAPTURE_FALLBACK_SAMPLE_FPS = 0.1
VISUAL_CAPTURE_SEEK_MAX_FRAMES = 24
VISUAL_CAPTURE_SEEK_MIN_FRAMES = 6
VISUAL_CAPTURE_SEEK_TIMEOUT_SECONDS = 25
VISUAL_CAPTURE_SEEK_TOTAL_TIMEOUT_SECONDS = 120
VISUAL_STAGE_DOWNLOAD_TIMEOUT_SECONDS = 120
VISUAL_STAGE_MAX_BYTES = 192 * 1024 * 1024
YOUTUBE_VISUAL_STAGE_FORMAT_SELECTOR = (
    "bv*[height<=480][ext=mp4][protocol=https]/"
    "bv*[height<=480][protocol=https]/"
    "b[height<=360][ext=mp4][protocol=https]/"
    "b[height<=360][protocol=https]"
)
YOUTUBE_VISUAL_FORMAT_SELECTOR = (
    "bv*[height<=720][protocol!=m3u8_native][protocol!=m3u8]/"
    "b[height<=720][protocol!=m3u8_native][protocol!=m3u8]/"
    "bv*[height<=720][protocol=m3u8_native]/"
    "bv*[height<=720][protocol=m3u8]"
)
VISUAL_CAPTURE_POLL_SECONDS = 0.25
VISUAL_CAPTURE_HEARTBEAT_SECONDS = 5.0
VISUAL_CAPTURE_TIMEOUT_SECONDS = 600
YOUTUBE_SURFACE_WORKERS = 2
YOUTUBE_SUBPROCESS_SPAWN_RETRIES = 2
YOUTUBE_SUBPROCESS_SPAWN_RETRY_BASE_SECONDS = 0.25


def _is_transient_spawn_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in {
        errno.EAGAIN,
        errno.ENOMEM,
    }


def _run_youtube_subprocess_with_spawn_retry(cmd: list[str], **kwargs):
    for attempt in range(YOUTUBE_SUBPROCESS_SPAWN_RETRIES + 1):
        try:
            return subprocess.run(cmd, **kwargs)
        except OSError as exc:
            if (
                not _is_transient_spawn_error(exc)
                or attempt >= YOUTUBE_SUBPROCESS_SPAWN_RETRIES
            ):
                raise
            time.sleep(YOUTUBE_SUBPROCESS_SPAWN_RETRY_BASE_SECONDS * (2 ** attempt))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def visual_capture_max_child_rss_bytes() -> int:
    raw = os.environ.get(
        "INFLUENCER_RESEARCH_YOUTUBE_VISUAL_CAPTURE_MAX_CHILD_RSS_MB",
        str(DEFAULT_VISUAL_CAPTURE_MAX_CHILD_RSS_MB),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_VISUAL_CAPTURE_MAX_CHILD_RSS_MB
    return max(256, min(value, 1024)) * 1024 * 1024


def visual_capture_sample_fps(duration_seconds: Any) -> float:
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        return VISUAL_CAPTURE_FALLBACK_SAMPLE_FPS
    if duration <= 0:
        return VISUAL_CAPTURE_FALLBACK_SAMPLE_FPS
    return min(1.0, VISUAL_CAPTURE_MAX_FRAMES_PER_BRANCH / duration)


def _process_tree_rss_bytes(pid: int) -> int:
    """Best-effort Linux RSS sum for one process tree.

    The production runtime is Linux-in-Docker. Returning zero when /proc is
    unavailable keeps non-Linux unit/dev environments functional without
    pretending that an RSS sample exists.
    """
    pending = [int(pid)]
    seen: set[int] = set()
    total = 0
    while pending:
        current = pending.pop()
        if current <= 0 or current in seen:
            continue
        seen.add(current)
        status_path = Path(f"/proc/{current}/status")
        children_path = Path(f"/proc/{current}/task/{current}/children")
        with contextlib.suppress(OSError, ValueError):
            for line in status_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        total += int(parts[1]) * 1024
                    break
        with contextlib.suppress(OSError, ValueError):
            child_text = children_path.read_text(encoding="utf-8", errors="replace").strip()
            if child_text:
                pending.extend(int(value) for value in child_text.split())
    return total


def _terminate_process(proc: Any) -> None:
    if proc is None or proc.poll() is not None:
        return
    with contextlib.suppress(OSError, ProcessLookupError):
        proc.terminate()
    try:
        proc.wait(timeout=2)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    with contextlib.suppress(OSError, ProcessLookupError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=2)


def _tail_text_file(path: Path, max_bytes: int = 8192) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), os.SEEK_SET)
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _redact_urls(text: str) -> str:
    return re.sub(
        r"https?://[^\s\"'<>]+",
        "<url>",
        str(text or ""),
    )


def _showinfo_times_file(path: Path, instance: str) -> list[float]:
    marker = f"showinfo@{instance}"
    times: list[float] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if marker not in line:
                    continue
                match = re.search(r"pts_time:([0-9]+(?:\.[0-9]+)?)", line)
                if match:
                    times.append(float(match.group(1)))
    except OSError:
        return []
    return times


def _wait_visual_capture(
    ff_proc: Any,
    yt_proc: Any,
    evidence_dir: Path,
    *,
    progress_callback: Callable[[dict], None] | None = None,
    timeout_seconds: float = VISUAL_CAPTURE_TIMEOUT_SECONDS,
    memory_limit_bytes: int | None = None,
) -> dict:
    memory_limit = int(memory_limit_bytes or visual_capture_max_child_rss_bytes())
    started = time.monotonic()
    last_heartbeat: float | None = None
    peak_rss = 0

    while True:
        now = time.monotonic()
        rss = _process_tree_rss_bytes(int(getattr(ff_proc, "pid", 0))) + _process_tree_rss_bytes(
            int(getattr(yt_proc, "pid", 0))
        )
        peak_rss = max(peak_rss, rss)
        elapsed = max(0.0, now - started)

        if progress_callback is not None and (
            last_heartbeat is None or now - last_heartbeat >= VISUAL_CAPTURE_HEARTBEAT_SECONDS
        ):
            frame_count = sum(1 for _ in evidence_dir.glob("*.jpg"))
            progress_callback({
                "visual_capture_mode": "BOUNDED_FULL_STREAM_FALLBACK",
                "visual_capture_elapsed_seconds": round(elapsed, 1),
                "visual_capture_child_rss_mib": round(rss / (1024 * 1024), 1),
                "visual_capture_child_rss_peak_mib": round(peak_rss / (1024 * 1024), 1),
                "visual_capture_frame_files": frame_count,
            })
            last_heartbeat = now

        if rss > memory_limit:
            _terminate_process(ff_proc)
            _terminate_process(yt_proc)
            return {
                "ok": False,
                "error": "VISUAL_CAPTURE_MEMORY_LIMIT",
                "elapsed_seconds": round(elapsed, 1),
                "child_rss_peak_mib": round(peak_rss / (1024 * 1024), 1),
                "memory_limit_mib": round(memory_limit / (1024 * 1024), 1),
            }

        if elapsed > float(timeout_seconds):
            _terminate_process(ff_proc)
            _terminate_process(yt_proc)
            return {
                "ok": False,
                "error": "VISUAL_CAPTURE_TIMEOUT",
                "elapsed_seconds": round(elapsed, 1),
                "child_rss_peak_mib": round(peak_rss / (1024 * 1024), 1),
                "memory_limit_mib": round(memory_limit / (1024 * 1024), 1),
            }

        returncode = ff_proc.poll()
        if returncode is not None:
            return {
                "ok": True,
                "returncode": int(returncode),
                "elapsed_seconds": round(elapsed, 1),
                "child_rss_peak_mib": round(peak_rss / (1024 * 1024), 1),
                "memory_limit_mib": round(memory_limit / (1024 * 1024), 1),
            }
        time.sleep(VISUAL_CAPTURE_POLL_SECONDS)


def published_iso(info: dict) -> str | None:
    ts = info.get("timestamp")
    if ts is not None:
        with contextlib.suppress(TypeError, ValueError, OverflowError, OSError):
            return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
    upload_date = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", upload_date):
        with contextlib.suppress(ValueError):
            return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc).isoformat()
    return None




VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
EXACT_VIDEO_ID_CAP = 20


def parse_exact_video_ids(value: str, *, cap: int = EXACT_VIDEO_ID_CAP) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in str(value or "").split(","):
        video_id = raw.strip()
        if not video_id:
            continue
        if not VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError(f"BAD_VIDEO_ID:{video_id[:80]}")
        if video_id not in seen:
            seen.add(video_id)
            out.append(video_id)
    if len(out) > cap:
        raise ValueError(f"TOO_MANY_VIDEO_IDS:{len(out)}>{cap}")
    return out


def max_unpinned_whisper_duration_seconds() -> int:
    raw = os.environ.get(
        "INFLUENCER_RESEARCH_YOUTUBE_MAX_UNPINNED_WHISPER_DURATION_SECONDS",
        str(DEFAULT_MAX_UNPINNED_WHISPER_DURATION_SECONDS),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_UNPINNED_WHISPER_DURATION_SECONDS
    return max(300, min(value, 3600))


def _duration_seconds(info: dict) -> float | None:
    raw = info.get("duration")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _preferred_caption_availability(info: dict) -> bool | None:
    sources_present = False
    for field in ("subtitles", "automatic_captions"):
        tracks = info.get(field)
        if not isinstance(tracks, dict):
            continue
        sources_present = True
        for language, variants in tracks.items():
            lang = str(language or "").casefold()
            if not (lang == "en" or lang.startswith("en-") or lang == "sv" or lang.startswith("sv-")):
                continue
            if isinstance(variants, list) and variants:
                return True
    return False if sources_present else None


def _deferred_entry(entry: dict, reason: str, **extra: Any) -> dict:
    return {
        "video_id": str(entry.get("id") or ""),
        "url": entry.get("url"),
        "surface": entry.get("surface"),
        "reason": reason,
        **{key: value for key, value in extra.items() if value is not None},
    }


def select_seeded_sample(
    seed_entries: list[dict],
    discovered_entries: list[dict],
    sample_size: int,
) -> tuple[list[dict], int]:
    """Return must-include seeds first, then discovery fill, deduped before the sample cap."""
    selected_pool: list[dict] = []
    seen: set[str] = set()
    duplicate_count = 0
    for entry in [*seed_entries, *discovered_entries]:
        video_id = str(entry.get("id") or "")
        if not video_id:
            continue
        if video_id in seen:
            duplicate_count += 1
            continue
        seen.add(video_id)
        selected_pool.append(entry)
    return selected_pool[:max(1, int(sample_size))], duplicate_count


def preflight_evidence_cost(
    root: Path,
    creator_key: str,
    entry: dict,
    *,
    channel_url: str,
    required_attribution_term: str,
    must_include: bool,
) -> tuple[dict | None, dict | None]:
    """Return an enriched accepted entry or a bounded-cost defer record."""
    accepted = dict(entry)
    accepted["must_include_seed"] = bool(must_include)
    if must_include:
        return accepted, None

    limit_seconds = max_unpinned_whisper_duration_seconds()
    duration = _duration_seconds({"duration": accepted.get("duration_seconds")})
    caption_available = accepted.get("caption_track_available")

    if duration is None or duration > limit_seconds:
        verified, diag = probe_exact_video(
            str(accepted.get("id") or ""),
            channel_url=channel_url,
            required_attribution_term=required_attribution_term,
        )
        if verified is None:
            return None, _deferred_entry(
                accepted,
                "EVIDENCE_COST_PREFLIGHT_UNAVAILABLE",
                detail=diag.get("reason"),
            )
        accepted = {**accepted, **verified, "must_include_seed": False}
        duration = _duration_seconds({"duration": accepted.get("duration_seconds")})
        caption_available = accepted.get("caption_track_available")

    if duration is None:
        return None, _deferred_entry(
            accepted,
            "UNPINNED_DURATION_UNKNOWN",
            live_status=accepted.get("live_status"),
        )

    if duration <= limit_seconds:
        return accepted, None

    if caption_available is not True:
        return None, _deferred_entry(
            accepted,
            "UNPINNED_LONGFORM_NO_CAPTIONS",
            duration_seconds=round(duration, 3),
            max_unpinned_whisper_duration_seconds=limit_seconds,
        )

    # Metadata can advertise subtitle tracks that later fail to materialize. For
    # expensive long-form candidates, prove that a usable caption transcript is
    # actually retrievable before admitting the item to the sample.
    caption_probe = fetch_captions(
        root,
        creator_key,
        str(accepted.get("url") or ""),
        str(accepted.get("id") or ""),
    )
    if not caption_probe.get("ok"):
        return None, _deferred_entry(
            accepted,
            "UNPINNED_LONGFORM_CAPTIONS_UNUSABLE",
            duration_seconds=round(duration, 3),
            max_unpinned_whisper_duration_seconds=limit_seconds,
            detail=caption_probe.get("source") or caption_probe.get("diagnostic_tail"),
        )
    accepted["caption_preflight_verified"] = True
    return accepted, None


def select_bounded_evidence_sample(
    root: Path,
    creator_key: str,
    entries: list[dict],
    sample_size: int,
    *,
    must_include_ids: set[str],
    channel_url: str,
    required_attribution_term: str,
    is_complete_entry: Callable[[dict], bool],
) -> tuple[list[dict], list[dict], int]:
    selected: list[dict] = []
    deferred: list[dict] = []
    cursor = 0
    target = max(1, int(sample_size))
    while cursor < len(entries) and len(selected) < target:
        entry = entries[cursor]
        cursor += 1
        video_id = str(entry.get("id") or "")
        if not video_id:
            continue
        if is_complete_entry(entry):
            selected.append(dict(entry))
            continue
        accepted, defer = preflight_evidence_cost(
            root,
            creator_key,
            entry,
            channel_url=channel_url,
            required_attribution_term=required_attribution_term,
            must_include=video_id in must_include_ids,
        )
        if defer is not None:
            deferred.append(defer)
            continue
        if accepted is not None:
            selected.append(accepted)
    return selected, deferred, cursor


def _youtube_profile_identity(channel_url: str) -> tuple[str, str]:
    parsed = urlparse(str(channel_url or "").strip())
    host = (parsed.hostname or "").casefold()
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        raise ValueError("BAD_YOUTUBE_CHANNEL_HOST")
    parts = [x for x in parsed.path.split("/") if x]
    if not parts:
        raise ValueError("BAD_YOUTUBE_CHANNEL_PATH")
    first = parts[0]
    if first.startswith("@") and len(first) > 1:
        return "HANDLE", first[1:].casefold()
    if first == "channel" and len(parts) >= 2 and parts[1]:
        return "CHANNEL_ID", parts[1]
    raise ValueError("UNSUPPORTED_YOUTUBE_CHANNEL_IDENTITY")


def canonical_youtube_channel_url(channel_url: str) -> str:
    kind, identity = _youtube_profile_identity(channel_url)
    if kind == "HANDLE":
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", identity):
            raise ValueError("BAD_YOUTUBE_HANDLE")
        return f"https://www.youtube.com/@{identity}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", identity):
        raise ValueError("BAD_YOUTUBE_CHANNEL_ID")
    return f"https://www.youtube.com/channel/{identity}"


def metadata_matches_channel(info: dict, channel_url: str) -> bool:
    kind, expected = _youtube_profile_identity(channel_url)
    if kind == "CHANNEL_ID":
        return str(info.get("channel_id") or "").strip() == expected

    uploader_id = str(info.get("uploader_id") or "").strip().casefold().lstrip("@")
    if uploader_id == expected:
        return True
    for field in ("uploader_url", "channel_url"):
        value = str(info.get(field) or "").strip()
        if not value:
            continue
        try:
            parsed = urlparse(value)
        except Exception:
            continue
        if (parsed.hostname or "").casefold() not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            continue
        parts = [x for x in parsed.path.split("/") if x]
        if parts and parts[0].casefold().lstrip("@") == expected:
            return True
    return False


def metadata_has_attribution(info: dict, required_term: str) -> bool:
    term = str(required_term or "").strip().casefold()
    if not term:
        return True
    # Shared-channel attribution must come from the video title. Descriptions may
    # contain channel-wide boilerplate naming multiple creators and are therefore
    # not accepted as independent creator attribution.
    return term in str(info.get("title") or "").casefold()


def probe_exact_video(video_id: str, *, channel_url: str, required_attribution_term: str = "") -> tuple[dict | None, dict]:
    if not VIDEO_ID_RE.fullmatch(video_id):
        return None, {"video_id": video_id, "ok": False, "reason": "BAD_VIDEO_ID"}
    url = f"https://www.youtube.com/watch?v={video_id}"
    base, js_diag = _yt_base_args()
    cmd = [*base, "--skip-download", "--dump-single-json", "--", url]
    try:
        # URL is derived from VIDEO_ID_RE-validated input and is not user-selected executable syntax.

        # lgtm[py/command-line-injection]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=False)
    except subprocess.TimeoutExpired as exc:
        return None, {
            "video_id": video_id, "ok": False, "reason": "METADATA_TIMEOUT",
            "diagnostic_tail": str(exc)[-2000:], "js_runtime": js_diag,
        }
    if p.returncode != 0:
        return None, {
            "video_id": video_id, "ok": False, "reason": "METADATA_UNAVAILABLE",
            "returncode": p.returncode, "diagnostic_tail": (p.stderr or p.stdout or "")[-2500:],
            "js_runtime": js_diag,
        }
    try:
        info = json.loads((p.stdout or "").strip())
    except Exception as exc:
        return None, {
            "video_id": video_id, "ok": False, "reason": "BAD_METADATA_JSON",
            "diagnostic_tail": f"{type(exc).__name__}:{exc}", "js_runtime": js_diag,
        }
    actual_id = str(info.get("id") or "").strip()
    if actual_id != video_id:
        return None, {"video_id": video_id, "ok": False, "reason": "VIDEO_ID_MISMATCH", "actual_id": actual_id}
    try:
        channel_ok = metadata_matches_channel(info, channel_url)
    except ValueError as exc:
        return None, {"video_id": video_id, "ok": False, "reason": str(exc)}
    if not channel_ok:
        return None, {
            "video_id": video_id, "ok": False, "reason": "CHANNEL_IDENTITY_MISMATCH",
            "uploader_id": str(info.get("uploader_id") or ""),
            "uploader_url": str(info.get("uploader_url") or ""),
            "channel_id": str(info.get("channel_id") or ""),
            "channel_url": str(info.get("channel_url") or ""),
        }
    if not metadata_has_attribution(info, required_attribution_term):
        return None, {
            "video_id": video_id, "ok": False, "reason": "CREATOR_ATTRIBUTION_MISSING",
            "required_term": required_attribution_term,
            "title": str(info.get("title") or "")[:300],
        }
    entry = {
        "id": video_id,
        "url": url,
        "title": str(info.get("title") or "").strip(),
        "published_at": published_iso(info),
        "duration_seconds": _duration_seconds(info),
        "caption_track_available": _preferred_caption_availability(info),
        "live_status": info.get("live_status"),
        "attribution": {
            "channel_identity_verified": True,
            "required_term": str(required_attribution_term or "").strip() or None,
            "required_term_verified": bool(str(required_attribution_term or "").strip()),
            "attribution_surface": "TITLE" if str(required_attribution_term or "").strip() else None,
            "uploader_id": str(info.get("uploader_id") or "") or None,
            "uploader_url": str(info.get("uploader_url") or "") or None,
            "channel_id": str(info.get("channel_id") or "") or None,
            "channel_url": str(info.get("channel_url") or "") or None,
        },
    }
    return entry, {"video_id": video_id, "ok": True, "reason": "VERIFIED_EXACT_VIDEO", "js_runtime": js_diag}


def exact_video_entries(channel_url: str, video_ids: list[str], *, required_attribution_term: str = "") -> tuple[list[dict], dict]:
    entries: list[dict] = []
    probes: list[dict] = []
    for video_id in video_ids:
        entry, diag = probe_exact_video(
            video_id, channel_url=channel_url, required_attribution_term=required_attribution_term)
        probes.append(diag)
        if entry is not None:
            entries.append(entry)
    return entries, {
        "mode": "DIRECT_EXACT_ID_ALLOWLIST",
        "ok": len(entries) == len(video_ids) and bool(video_ids),
        "requested_count": len(video_ids),
        "verified_count": len(entries),
        "probes": probes,
    }


def _enumerate_channel_surface(
    channel_url: str,
    *,
    surface: str,
    limit: int,
) -> tuple[list[dict], dict]:
    """Enumerate one YouTube channel surface with a strict local item cap."""
    if surface not in {"videos", "shorts", "streams"}:
        raise ValueError("BAD_YOUTUBE_SURFACE")
    channel_url = canonical_youtube_channel_url(channel_url)
    requested = max(0, int(limit))
    if requested <= 0:
        return [], {
            "surface": surface,
            "ok": True,
            "returncode": 0,
            "requested_limit": 0,
            "entries_found": 0,
            "diagnostic_tail": "",
        }

    surface_url = f"{channel_url}/{surface}"
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",
        "--flat-playlist",
        "--playlist-end", str(requested),
        "--dump-json",
        "--",
        surface_url,
    ]
    try:
        # URL is strict-canonical YouTube and '--' terminates yt-dlp option parsing.

        # lgtm[py/command-line-injection]
        p = _run_youtube_subprocess_with_spawn_retry(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        return [], {
            "surface": surface,
            "ok": False,
            "returncode": 124,
            "requested_limit": requested,
            "entries_found": 0,
            "diagnostic_tail": str(exc)[-2000:],
        }

    entries: list[dict] = []
    seen: set[str] = set()
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        vid = str(obj.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid) or vid in seen:
            continue
        seen.add(vid)
        entries.append({
            "id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": str(obj.get("title") or "").strip(),
            "published_at": published_iso(obj),
            "surface": surface.upper(),
            "duration_seconds": _duration_seconds(obj),
            "caption_track_available": _preferred_caption_availability(obj),
            "live_status": obj.get("live_status"),
        })
        if len(entries) >= requested:
            break

    diag = (p.stderr or "").strip()
    return entries, {
        "surface": surface,
        "ok": p.returncode == 0,
        "returncode": int(p.returncode),
        "requested_limit": requested,
        "entries_found": len(entries),
        "diagnostic_tail": diag[-2500:],
    }


YOUTUBE_CHANNEL_SURFACES = ("videos", "shorts", "streams")


def enumerate_channel_surfaces(
    channel_url: str,
    *,
    surface_limits: dict[str, int],
) -> tuple[dict[str, list[dict]], dict]:
    """Enumerate explicit YouTube channel surfaces under independent local caps."""
    channel_url = canonical_youtube_channel_url(channel_url)
    limits = {
        surface: max(0, int(surface_limits.get(surface, 0)))
        for surface in YOUTUBE_CHANNEL_SURFACES
    }
    active = [
        (surface, limits[surface])
        for surface in YOUTUBE_CHANNEL_SURFACES
        if limits[surface] > 0
    ]

    surface_results: list[tuple[list[dict], dict]] = []
    if len(active) == 1:
        surface_results = [
            _enumerate_channel_surface(
                channel_url,
                surface=active[0][0],
                limit=active[0][1],
            )
        ]
    elif active:
        with ThreadPoolExecutor(
            max_workers=min(YOUTUBE_SURFACE_WORKERS, len(active)),
            thread_name_prefix="youtube-surface",
        ) as executor:
            futures = [
                executor.submit(
                    _enumerate_channel_surface,
                    channel_url,
                    surface=surface,
                    limit=surface_limit,
                )
                for surface, surface_limit in active
            ]
            surface_results = [future.result() for future in futures]

    entries_by_surface: dict[str, list[dict]] = {
        surface: [] for surface in YOUTUBE_CHANNEL_SURFACES
    }
    diagnostics: dict[str, dict] = {
        surface: {
            "surface": surface,
            "ok": True,
            "returncode": 0,
            "requested_limit": limits[surface],
            "entries_found": 0,
            "diagnostic_tail": "",
        }
        for surface in YOUTUBE_CHANNEL_SURFACES
    }
    for entries, diag in surface_results:
        surface = str(diag.get("surface") or "").lower()
        if surface not in entries_by_surface:
            continue
        entries_by_surface[surface] = entries
        diagnostics[surface] = diag

    nonzero = [
        diagnostics[surface]
        for surface in YOUTUBE_CHANNEL_SURFACES
        if limits[surface] > 0
    ]
    diag = {
        "ok": bool(active) and all(
            int(item.get("returncode") or 0) == 0 for item in nonzero
        ),
        "returncode": next(
            (
                int(item.get("returncode") or 0)
                for item in nonzero
                if int(item.get("returncode") or 0) != 0
            ),
            0,
        ),
        "requested_limit": sum(limits.values()),
        "entries_found": sum(len(entries_by_surface[s]) for s in YOUTUBE_CHANNEL_SURFACES),
        "surface_limits": limits,
        "surfaces": diagnostics,
        "diagnostic_tail": "\n--- surface ---\n".join(
            str(item.get("diagnostic_tail") or "")
            for item in nonzero
            if str(item.get("diagnostic_tail") or "").strip()
        )[-2500:],
    }
    return entries_by_surface, diag


def enumerate_channel(channel_url: str, *, limit: int) -> tuple[list[dict], dict]:
    """Enumerate Videos/Shorts/Streams under one global discovery budget.

    This compatibility wrapper preserves the historical global-budget behavior.
    Recent-window discovery uses enumerate_channel_surfaces() so dense surfaces can
    expand independently without borrowing budget from unrelated surfaces.
    """
    channel_url = canonical_youtube_channel_url(channel_url)
    global_limit = max(1, int(limit))
    base, remainder = divmod(global_limit, len(YOUTUBE_CHANNEL_SURFACES))
    surface_limits = {
        surface: base + (1 if index < remainder else 0)
        for index, surface in enumerate(YOUTUBE_CHANNEL_SURFACES)
    }

    entries_by_surface, diag = enumerate_channel_surfaces(
        channel_url,
        surface_limits=surface_limits,
    )

    merged: list[dict] = []
    seen: set[str] = set()
    max_depth = max((len(entries) for entries in entries_by_surface.values()), default=0)
    for index in range(max_depth):
        for surface in YOUTUBE_CHANNEL_SURFACES:
            entries = entries_by_surface.get(surface, [])
            if index >= len(entries):
                continue
            entry = entries[index]
            vid = str(entry.get("id") or "")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            merged.append(entry)
            if len(merged) >= global_limit:
                break
        if len(merged) >= global_limit:
            break

    diag = {
        **diag,
        "requested_limit": global_limit,
        "entries_found": len(merged),
        "surface_limits": surface_limits,
    }
    return merged, diag


def _node_runtime_arg() -> tuple[list[str], dict]:
    node = shutil.which("node") or shutil.which("node.exe")
    diag = {"node_path": node, "node_version": None, "enabled": False}
    if not node:
        return [], diag
    try:
        p = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
        version = (p.stdout or p.stderr or "").strip()
        diag["node_version"] = version
        m = re.search(r"(\d+)", version)
        if p.returncode == 0 and m and int(m.group(1)) >= 22:
            diag["enabled"] = True
            return ["--js-runtimes", f"node:{node}"], diag
    except Exception as exc:
        diag["error"] = f"{type(exc).__name__}:{exc}"
    return [], diag


def _yt_base_args() -> tuple[list[str], dict]:
    js_args, js_diag = _node_runtime_arg()
    return [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",
        "--no-progress",
        *js_args,
        "--no-remote-components",
    ], js_diag




def _ffmpeg_exe() -> str | None:
    system = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if system:
        return system
    with contextlib.suppress(Exception):
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).exists():
            return bundled
    return None

def _clean_caption_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _vtt_seconds(value: str) -> float | None:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return float(h) * 3600 + float(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return float(m) * 60 + float(s)
    except ValueError:
        return None
    return None


def parse_vtt(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    rows: list[dict] = []
    idx = 0
    timestamp_re = re.compile(
        r"^\s*((?:\d{2}:)?\d{2}:\d{2}[\.,]\d{3})\s+-->\s+((?:\d{2}:)?\d{2}:\d{2}[\.,]\d{3})"
    )
    while idx < len(lines):
        m = timestamp_re.match(lines[idx])
        if not m:
            idx += 1
            continue
        start = _vtt_seconds(m.group(1))
        end = _vtt_seconds(m.group(2))
        idx += 1
        text_lines: list[str] = []
        while idx < len(lines) and lines[idx].strip():
            text_lines.append(lines[idx])
            idx += 1
        text = _clean_caption_text(" ".join(text_lines))
        if text and start is not None and end is not None:
            if rows and rows[-1]["text"] == text:
                rows[-1]["end"] = round(max(float(rows[-1]["end"]), end), 3)
            else:
                rows.append({"start": round(start, 3), "end": round(end, 3), "text": text})
        idx += 1
    return rows


def _caption_candidates(caption_dir: Path, video_id: str) -> list[Path]:
    candidates = []
    for path in caption_dir.glob(f"{video_id}*.vtt"):
        if path.is_file() and path.stat().st_size > 20:
            candidates.append(path)

    def rank(path: Path) -> tuple[int, str]:
        name = path.name.casefold()
        if ".en." in name or ".en-" in name or ".en_" in name:
            return (0, name)
        if ".sv." in name or ".sv-" in name or ".sv_" in name:
            return (1, name)
        return (2, name)

    return sorted(candidates, key=rank)


def fetch_captions(root: Path, creator_key: str, url: str, video_id: str) -> dict:
    caption_dir = root / "output" / creator_key / "youtube" / "captions"
    caption_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir = root / "output" / creator_key / "youtube" / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    txt_path = transcript_dir / f"{video_id}.txt"
    json_path = transcript_dir / f"{video_id}.json"

    if txt_path.exists() and json_path.exists():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
            source = str(existing.get("transcript_source") or existing.get("source") or "existing_transcript")
        except Exception:
            source = "existing_transcript"
        return {
            "ok": True,
            "source": source,
            "txt": txt_path,
            "json": json_path,
            "transcribed_at": datetime.fromtimestamp(txt_path.stat().st_mtime, timezone.utc).isoformat(),
            "caption_file": None,
            "diagnostic_tail": "",
        }

    base, js_diag = _yt_base_args()
    cmd = [
        *base,
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-format", "vtt",
        "--sub-langs", "en.*,en,sv.*,sv",
        "--write-info-json",
        "--output", str(caption_dir / "%(id)s.%(ext)s"),
        url,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "source": "caption_timeout", "diagnostic_tail": str(exc)[-2500:], "js_runtime": js_diag}

    candidates = _caption_candidates(caption_dir, video_id)
    best_rows: list[dict] = []
    best_path: Path | None = None
    for candidate in candidates:
        try:
            rows = parse_vtt(candidate)
        except Exception:
            continue
        if sum(len(r.get("text", "")) for r in rows) > sum(len(r.get("text", "")) for r in best_rows):
            best_rows = rows
            best_path = candidate

    if not best_rows or best_path is None:
        return {
            "ok": False,
            "source": "no_usable_captions",
            "returncode": p.returncode,
            "diagnostic_tail": (p.stderr or p.stdout or "")[-2500:],
            "js_runtime": js_diag,
        }

    text = " ".join(row["text"] for row in best_rows).strip()
    when = utc_now()
    txt_path.write_text(text + "\n", encoding="utf-8")
    atomic_json(json_path, {
        "schema_version": 1,
        "app_version": YOUTUBE_EVAL_VERSION,
        "source_platform": "YOUTUBE",
        "creator": creator_key,
        "video_id": video_id,
        "generated_at": when,
        "transcript_source": "YOUTUBE_CAPTIONS",
        "caption_file": str(best_path.relative_to(root)),
        "segments": best_rows,
    })
    return {
        "ok": True,
        "source": "YOUTUBE_CAPTIONS",
        "txt": txt_path,
        "json": json_path,
        "transcribed_at": when,
        "caption_file": best_path,
        "returncode": p.returncode,
        "diagnostic_tail": (p.stderr or p.stdout or "")[-2500:],
        "js_runtime": js_diag,
    }


def validate_audio(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    size = path.stat().st_size
    if size < 10_000:
        return False, f"too_small:{size}"
    try:
        import av
        with av.open(str(path)) as container:
            audios = [stream for stream in container.streams if stream.type == "audio"]
            if audios:
                return True, f"ok:size={size}:audio={len(audios)}"
            return False, "no_audio_stream"
    except Exception as exc:
        # Faster-whisper commonly brings PyAV; if it is unavailable, retain the
        # previous transcript-first fallback and let transcription perform the
        # final decode check rather than requiring an external ffprobe binary.
        return True, f"size_only:{size}:validation_warning={type(exc).__name__}"


def download_audio_temp(url: str, video_id: str, temp_dir: Path) -> dict:
    base, js_diag = _yt_base_args()
    cmd = [
        *base,
        "--force-overwrites",
        "--format", "ba/b[acodec!=none]",
        "--output", str(temp_dir / "%(id)s.%(ext)s"),
        url,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "source": "audio_timeout", "diagnostic_tail": str(exc)[-2500:], "js_runtime": js_diag}

    found: Path | None = None
    validation = "missing"
    for path in sorted(temp_dir.glob(f"{video_id}.*")):
        if not path.is_file() or path.name.endswith((".part", ".ytdl", ".json")):
            continue
        valid, detail = validate_audio(path)
        validation = detail
        if valid:
            found = path
            break
    return {
        "ok": p.returncode == 0 and found is not None,
        "media_file": found,
        "validation": validation,
        "returncode": p.returncode,
        "diagnostic_tail": (p.stderr or p.stdout or "")[-2500:],
        "js_runtime": js_diag,
    }


def _extract_audio_for_whisper(media_path: Path) -> tuple[Path, dict]:
    ffmpeg = _ffmpeg_exe()
    diag = {"ffmpeg": ffmpeg, "used": False}
    if not ffmpeg:
        return media_path, diag

    wav_path = media_path.with_suffix(media_path.suffix + ".whisper.wav")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(media_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(wav_path),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=False)
    if p.returncode != 0 or not wav_path.exists() or wav_path.stat().st_size < 10_000:
        detail = (p.stderr or p.stdout or "")[-2500:]
        raise RuntimeError(f"ffmpeg_audio_extract_failed:{detail}")
    diag["used"] = True
    diag["wav_path"] = str(wav_path)
    return wav_path, diag


def transcribe_whisper(
    root: Path,
    creator_key: str,
    video_id: str,
    media_path: Path,
    *,
    model_holder: dict,
    progress_callback: Callable[[int], None] | None = None,
) -> dict:
    transcript_dir = root / "output" / creator_key / "youtube" / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    txt_path = transcript_dir / f"{video_id}.txt"
    json_path = transcript_dir / f"{video_id}.json"

    if txt_path.exists() and json_path.exists():
        return {
            "ok": True,
            "source": "existing_transcript",
            "txt": txt_path,
            "json": json_path,
            "transcribed_at": datetime.fromtimestamp(txt_path.stat().st_mtime, timezone.utc).isoformat(),
        }

    valid, validation = validate_audio(media_path)
    if not valid:
        return {"ok": False, "error": f"invalid_audio:{validation}"}

    if model_holder.get("model") is None:
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel
        model_holder["model"] = WhisperModel("small", device="cpu", compute_type="int8")

    model = model_holder["model"]
    whisper_input, audio_diag = _extract_audio_for_whisper(media_path)
    try:
        segments, info = model.transcribe(str(whisper_input), beam_size=5, vad_filter=True)
        rows = []
        text_parts = []
        for segment_index, seg in enumerate(segments, start=1):
            text = (seg.text or "").strip()
            if text:
                text_parts.append(text)
            rows.append({"start": round(float(seg.start), 3), "end": round(float(seg.end), 3), "text": text})
            if progress_callback is not None and (segment_index == 1 or segment_index % 10 == 0):
                progress_callback(segment_index)
    finally:
        if audio_diag.get("used") and whisper_input.exists():
            with contextlib.suppress(OSError):
                whisper_input.unlink()

    when = utc_now()
    txt_path.write_text(" ".join(text_parts).strip() + "\n", encoding="utf-8")
    atomic_json(json_path, {
        "schema_version": 1,
        "app_version": YOUTUBE_EVAL_VERSION,
        "source_platform": "YOUTUBE",
        "creator": creator_key,
        "video_id": video_id,
        "generated_at": when,
        "transcript_source": "FASTER_WHISPER_FALLBACK",
        "language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "duration": getattr(info, "duration", None),
        "audio_normalization": audio_diag,
        "segments": rows,
    })
    return {
        "ok": True,
        "source": "FASTER_WHISPER_FALLBACK",
        "txt": txt_path,
        "json": json_path,
        "transcribed_at": when,
        "audio_normalization": audio_diag,
    }


def _showinfo_times(stderr: str, instance: str) -> list[float]:
    times: list[float] = []
    marker = f"showinfo@{instance}"
    for line in stderr.splitlines():
        if marker not in line:
            continue
        m = re.search(r"pts_time:([0-9]+(?:\.[0-9]+)?)", line)
        if m:
            times.append(float(m.group(1)))
    return times


def _frame_records(frame_dir: Path, prefix: str, times: list[float], reason: str) -> list[dict]:
    files = sorted(frame_dir.glob(f"{prefix}_*.jpg"))
    rows = []
    for idx, path in enumerate(files):
        ts = times[idx] if idx < len(times) else None
        rows.append({
            "timestamp_s": round(ts, 3) if ts is not None else None,
            "reason": reason,
            "file": path,
            "size_bytes": path.stat().st_size,
        })
    return rows


def _merge_frame_records(records: list[dict]) -> list[dict]:
    records = sorted(records, key=lambda r: (float(r["timestamp_s"]) if r.get("timestamp_s") is not None else 1e18, r["reason"]))
    kept: list[dict] = []
    for rec in records:
        ts = rec.get("timestamp_s")
        if ts is not None and kept and kept[-1].get("timestamp_s") is not None:
            if abs(float(ts) - float(kept[-1]["timestamp_s"])) <= 0.35:
                # Prefer scene-change evidence over the periodic frame at the same moment.
                if rec["reason"] == "SCENE_CHANGE" and kept[-1]["reason"] != "SCENE_CHANGE":
                    with contextlib.suppress(OSError):
                        kept[-1]["file"].unlink()
                    kept[-1] = rec
                else:
                    with contextlib.suppress(OSError):
                        rec["file"].unlink()
                continue
        kept.append(rec)
    return kept



# Visual-review policy and OCR/post-processing live in video_visual_evidence.py.
# Keep these aliases/wrappers for backward-compatible tests and callers while
# avoiding a second drifting implementation in the YouTube evaluator.
VISUAL_OCR_MAX_FRAMES = vve.VISUAL_OCR_MAX_FRAMES
VISUAL_REPRESENTATIVE_FRAMES = vve.VISUAL_REPRESENTATIVE_FRAMES
VISUAL_OCR_TIMEOUT_SECONDS = vve.VISUAL_OCR_TIMEOUT_SECONDS
CHART_HEAVY_CREATORS = vve.CHART_HEAVY_CREATORS
CHART_TERMS = vve.CHART_TERMS


def _sample_visual_records(records: list[dict], limit: int = VISUAL_OCR_MAX_FRAMES) -> list[dict]:
    return vve._sample_visual_records(records, limit=limit)


def _run_tesseract_visual_frame(path: Path, psm: int) -> str:
    return vve._run_tesseract_visual_frame(path, psm)


def _ocr_visual_frame(path: Path) -> str:
    return vve._ocr_visual_frame(path)


def _score_visual_frame_text(text: str) -> tuple[float, list[str]]:
    return vve.score_visual_frame_text(text)


def _make_contact_sheet(
    ffmpeg: str,
    evidence_dir: Path,
    selected: list[dict],
    *,
    progress_callback: Callable[[dict], None] | None = None,
) -> Path | None:
    return vve._make_contact_sheet(
        ffmpeg,
        evidence_dir,
        selected,
        progress_callback=progress_callback,
    )


def build_agent_visual_bundle(
    root: Path,
    creator_key: str,
    records: list[dict],
    ffmpeg: str,
    evidence_dir: Path,
    *,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    return vve.build_agent_visual_bundle(
        root,
        creator_key,
        records,
        ffmpeg,
        evidence_dir,
        progress_callback=progress_callback,
    )


def visual_capture_seek_timestamps(
    duration_seconds: Any,
    *,
    max_frames: int = VISUAL_CAPTURE_SEEK_MAX_FRAMES,
) -> list[float]:
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        return []
    if duration <= 0:
        return []
    bounded_max = max(1, int(max_frames))
    requested = max(
        VISUAL_CAPTURE_SEEK_MIN_FRAMES,
        int(duration // 15.0) + 1,
    )
    count = min(bounded_max, requested)
    usable_end = max(0.0, duration - 0.5)
    if count == 1:
        return [round(usable_end / 2.0, 3)]
    return [
        round(((index + 0.5) / count) * usable_end, 3)
        for index in range(count)
    ]


def _visual_stage_artifacts(evidence_dir: Path) -> list[Path]:
    return [
        path
        for path in evidence_dir.glob("visual_source.*")
        if path.is_file()
    ]


def _visual_stage_bytes(evidence_dir: Path) -> int:
    total = 0
    for path in _visual_stage_artifacts(evidence_dir):
        with contextlib.suppress(OSError):
            total += int(path.stat().st_size)
    return total


def _cleanup_visual_stage(evidence_dir: Path) -> None:
    for path in _visual_stage_artifacts(evidence_dir):
        with contextlib.suppress(OSError):
            path.unlink()


def _download_visual_stage_source(
    url: str,
    evidence_dir: Path,
    *,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    base, js_diag = _yt_base_args()
    output_template = str(evidence_dir / "visual_source.%(ext)s")
    cmd = [
        *base,
        "--no-playlist",
        "--format", YOUTUBE_VISUAL_STAGE_FORMAT_SELECTOR,
        "--max-filesize", "192M",
        "--output", output_template,
        "--",
        url,
    ]

    with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as err_file:
        err_path = Path(err_file.name)

    proc = None
    started = time.monotonic()
    last_heartbeat: float | None = None
    try:
        with err_path.open("wb") as stderr_file:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                )
                while proc.poll() is None:
                    now = time.monotonic()
                    elapsed = max(0.0, now - started)
                    staged_bytes = _visual_stage_bytes(evidence_dir)

                    if progress_callback is not None and (
                        last_heartbeat is None
                        or now - last_heartbeat >= VISUAL_CAPTURE_HEARTBEAT_SECONDS
                    ):
                        progress_callback({
                            "visual_capture_mode": "LOCAL_STAGE_DOWNLOAD",
                            "visual_capture_elapsed_seconds": round(elapsed, 1),
                            "visual_stage_bytes": staged_bytes,
                            "visual_stage_max_bytes": VISUAL_STAGE_MAX_BYTES,
                            "visual_capture_frame_files": 0,
                        })
                        last_heartbeat = now

                    if staged_bytes > VISUAL_STAGE_MAX_BYTES:
                        _terminate_process(proc)
                        return {
                            "ok": False,
                            "error": "VISUAL_STAGE_SIZE_LIMIT",
                            "staged_bytes": staged_bytes,
                            "max_bytes": VISUAL_STAGE_MAX_BYTES,
                            "elapsed_seconds": round(elapsed, 1),
                            "js_runtime": js_diag,
                        }
                    if elapsed > VISUAL_STAGE_DOWNLOAD_TIMEOUT_SECONDS:
                        _terminate_process(proc)
                        return {
                            "ok": False,
                            "error": "VISUAL_STAGE_DOWNLOAD_TIMEOUT",
                            "staged_bytes": staged_bytes,
                            "max_bytes": VISUAL_STAGE_MAX_BYTES,
                            "elapsed_seconds": round(elapsed, 1),
                            "js_runtime": js_diag,
                        }
                    time.sleep(VISUAL_CAPTURE_POLL_SECONDS)
            finally:
                _terminate_process(proc)

        returncode = int(proc.poll() if proc is not None and proc.poll() is not None else -1)
        diagnostic_tail = _redact_urls(_tail_text_file(err_path, 2000))
        candidates = [
            path
            for path in _visual_stage_artifacts(evidence_dir)
            if not path.name.endswith((".part", ".ytdl", ".tmp"))
        ]
        source = max(candidates, key=lambda path: path.stat().st_size) if candidates else None
        if source is None or not source.exists():
            return {
                "ok": False,
                "error": "VISUAL_STAGE_DOWNLOAD_FAILED",
                "returncode": returncode,
                "diagnostic_tail": diagnostic_tail,
                "js_runtime": js_diag,
            }
        source_bytes = int(source.stat().st_size)
        if source_bytes > VISUAL_STAGE_MAX_BYTES:
            return {
                "ok": False,
                "error": "VISUAL_STAGE_SIZE_LIMIT",
                "returncode": returncode,
                "staged_bytes": source_bytes,
                "max_bytes": VISUAL_STAGE_MAX_BYTES,
                "diagnostic_tail": diagnostic_tail,
                "js_runtime": js_diag,
            }
        if returncode != 0:
            return {
                "ok": False,
                "error": "VISUAL_STAGE_DOWNLOAD_FAILED",
                "returncode": returncode,
                "staged_bytes": source_bytes,
                "diagnostic_tail": diagnostic_tail,
                "js_runtime": js_diag,
            }
        return {
            "ok": True,
            "returncode": returncode,
            "source_path": source,
            "staged_bytes": source_bytes,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "diagnostic_tail": diagnostic_tail,
            "js_runtime": js_diag,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}:VISUAL_STAGE_DOWNLOAD_FAILED",
            "diagnostic_tail": _redact_urls(_tail_text_file(err_path, 2000)),
            "js_runtime": js_diag,
        }
    finally:
        with contextlib.suppress(OSError):
            err_path.unlink()


def _capture_local_visual_snapshot(
    ffmpeg: str,
    source_path: Path,
    timestamp_s: float,
    output_path: Path,
) -> dict:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", f"{max(0.0, float(timestamp_s)):.3f}",
        "-i", str(source_path),
        "-an",
        "-frames:v", "1",
        "-q:v", "5",
        str(output_path),
    ]
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            timeout=VISUAL_CAPTURE_SEEK_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}:VISUAL_SNAPSHOT_FAILED",
        }
    ok = p.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0
    return {
        "ok": ok,
        "returncode": int(p.returncode),
        "diagnostic_tail": (p.stderr or b"")[-1200:].decode("utf-8", errors="replace"),
    }


def _capture_seeked_visual_evidence(
    root: Path,
    creator_key: str,
    url: str,
    video_id: str,
    ffmpeg: str,
    evidence_dir: Path,
    index_path: Path,
    *,
    duration_seconds: float | None,
    progress_callback: Callable[[dict], None] | None,
) -> dict:
    timestamps = visual_capture_seek_timestamps(duration_seconds)
    if not timestamps:
        return {"ok": False, "error": "SEEKED_VISUAL_DURATION_UNAVAILABLE"}

    started = time.monotonic()
    staged = _download_visual_stage_source(
        url,
        evidence_dir,
        progress_callback=progress_callback,
    )
    source_path = staged.get("source_path")
    if not staged.get("ok") or not isinstance(source_path, Path):
        _cleanup_visual_stage(evidence_dir)
        return {
            "ok": False,
            "error": staged.get("error") or "VISUAL_STAGE_DOWNLOAD_FAILED",
            "diagnostic_tail": staged.get("diagnostic_tail"),
        }

    records: list[dict] = []
    failures: list[dict] = []
    minimum = min(
        len(timestamps),
        max(3, (len(timestamps) + 1) // 2),
    )

    try:
        for index, timestamp_s in enumerate(timestamps, start=1):
            elapsed_before = time.monotonic() - started
            if elapsed_before > VISUAL_CAPTURE_SEEK_TOTAL_TIMEOUT_SECONDS:
                failures.append({
                    "timestamp_s": round(float(timestamp_s), 3),
                    "error": "SEEKED_VISUAL_TOTAL_TIMEOUT",
                })
                break

            remaining_after_this = len(timestamps) - index
            if len(records) + remaining_after_this + 1 < minimum:
                failures.append({
                    "timestamp_s": round(float(timestamp_s), 3),
                    "error": "SEEKED_VISUAL_MINIMUM_NO_LONGER_REACHABLE",
                })
                break

            frame_path = evidence_dir / f"timeline_{index:03d}.jpg"
            result = _capture_local_visual_snapshot(
                ffmpeg,
                source_path,
                timestamp_s,
                frame_path,
            )
            if result.get("ok"):
                records.append({
                    "timestamp_s": round(float(timestamp_s), 3),
                    "reason": "TIMELINE_SAMPLE",
                    "file": frame_path,
                    "size_bytes": frame_path.stat().st_size,
                })
            else:
                failures.append({
                    "timestamp_s": round(float(timestamp_s), 3),
                    "error": result.get("error") or "VISUAL_SNAPSHOT_FAILED",
                    "returncode": result.get("returncode"),
                    "diagnostic_tail": result.get("diagnostic_tail"),
                })

            if progress_callback is not None:
                progress_callback({
                    "visual_capture_mode": "LOCAL_STAGED_TIMELINE_SNAPSHOTS",
                    "visual_capture_elapsed_seconds": round(time.monotonic() - started, 1),
                    "visual_capture_seek_total": len(timestamps),
                    "visual_capture_seek_completed": index,
                    "visual_capture_frame_files": len(records),
                    "visual_stage_bytes": staged.get("staged_bytes"),
                    "visual_stage_max_bytes": VISUAL_STAGE_MAX_BYTES,
                })

        if len(records) < minimum:
            return {
                "ok": False,
                "error": "SEEKED_VISUAL_INSUFFICIENT_FRAMES",
                "captured_frames": len(records),
                "required_frames": minimum,
                "diagnostic_tail": str(failures[-3:])[-2000:],
            }

        # The temporary source is no longer needed once the snapshots exist.
        _cleanup_visual_stage(evidence_dir)

        agent_visual_bundle = build_agent_visual_bundle(
            root,
            creator_key,
            records,
            ffmpeg,
            evidence_dir,
            progress_callback=progress_callback,
        )

        frames = [
            {
                "timestamp_s": row["timestamp_s"],
                "reason": row["reason"],
                "file": str(Path(row["file"]).relative_to(root)),
                "size_bytes": row["size_bytes"],
            }
            for row in records
        ]
        elapsed = round(time.monotonic() - started, 1)
        summary = {
            "capture_strategy": "LOCAL_STAGED_TIMELINE_SNAPSHOTS",
            "visual_capture_duration_seconds": duration_seconds,
            "visual_capture_seek_samples_requested": len(timestamps),
            "visual_capture_seek_samples_completed": len(records),
            "visual_capture_seek_timeout_seconds": VISUAL_CAPTURE_SEEK_TIMEOUT_SECONDS,
            "visual_capture_seek_total_timeout_seconds": VISUAL_CAPTURE_SEEK_TOTAL_TIMEOUT_SECONDS,
            "visual_stage_download_timeout_seconds": VISUAL_STAGE_DOWNLOAD_TIMEOUT_SECONDS,
            "visual_stage_max_bytes": VISUAL_STAGE_MAX_BYTES,
            "visual_stage_bytes": staged.get("staged_bytes"),
            "visual_stage_format_selector": YOUTUBE_VISUAL_STAGE_FORMAT_SELECTOR,
            "visual_capture_elapsed_seconds": elapsed,
            "visual_capture_child_rss_peak_mib": None,
            "visual_capture_memory_limit_mib": round(
                visual_capture_max_child_rss_bytes() / (1024 * 1024),
                1,
            ),
            "candidate_frames": len(records),
            "retained_frames": len(frames),
            "scene_change_frames": 0,
            "one_fps_frames": 0,
            "timeline_sample_frames": len(frames),
            "video_persisted": False,
            "agent_visual_bundle": agent_visual_bundle,
        }
        index = {
            "schema_version": 1,
            "app_version": YOUTUBE_EVAL_VERSION,
            "source_platform": "YOUTUBE",
            "source_url": url,
            "video_id": video_id,
            "generated_at": utc_now(),
            "summary": summary,
            "frames": frames,
            "agent_visual_bundle": agent_visual_bundle,
            "diagnostics": {
                "stage_download_returncode": staged.get("returncode"),
                "stage_download_tail": staged.get("diagnostic_tail"),
                "stage_download_js_runtime": staged.get("js_runtime"),
                "snapshot_failures": failures,
            },
        }
        atomic_json(index_path, index)
        return {
            "ok": True,
            "index": index_path,
            **summary,
        }
    finally:
        _cleanup_visual_stage(evidence_dir)


def _seeked_fallback_progress(seeked: dict) -> dict:
    return {
        "visual_capture_mode": "BOUNDED_FULL_STREAM_FALLBACK",
        "visual_capture_seek_total": None,
        "visual_capture_seek_completed": None,
        "visual_capture_elapsed_seconds": 0.0,
        "visual_capture_frame_files": 0,
        "visual_capture_child_rss_mib": 0.0,
        "visual_capture_child_rss_peak_mib": 0.0,
        "seeked_sampling_fallback_reason": seeked.get("error"),
        "seeked_sampling_captured_frames": seeked.get("captured_frames"),
        "seeked_sampling_required_frames": seeked.get("required_frames"),
        "seeked_sampling_diagnostic_tail": str(
            seeked.get("diagnostic_tail") or ""
        )[-1000:],
    }


def capture_visual_evidence(
    root: Path,
    creator_key: str,
    url: str,
    video_id: str,
    *,
    duration_seconds: float | None = None,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    evidence_dir = root / "output" / creator_key / "youtube" / "frames" / video_id
    index_path = evidence_dir / "visual_index.json"
    if index_path.exists():
        with contextlib.suppress(OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            retained = existing.get("frames", [])
            if retained and all((root / Path(x["file"])).exists() for x in retained if x.get("file")):
                summary = dict(existing.get("summary") or {})
                agent_visual_bundle = existing.get("agent_visual_bundle") or summary.get("agent_visual_bundle")
                if not isinstance(agent_visual_bundle, dict):
                    ffmpeg = _ffmpeg_exe()
                    if ffmpeg:
                        records = [
                            {
                                **row,
                                "file": root / Path(row["file"]),
                                "size_bytes": int(row.get("size_bytes") or (root / Path(row["file"])).stat().st_size),
                            }
                            for row in retained
                            if row.get("file")
                        ]
                        agent_visual_bundle = build_agent_visual_bundle(
                            root,
                            creator_key,
                            records,
                            ffmpeg,
                            evidence_dir,
                            progress_callback=progress_callback,
                        )
                        existing["agent_visual_bundle"] = agent_visual_bundle
                        summary["agent_visual_bundle"] = agent_visual_bundle
                        existing["summary"] = summary
                        atomic_json(index_path, existing)
                return {"ok": True, "source": "existing_visual_evidence", "index": index_path, **summary}

    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return {"ok": False, "error": "ffmpeg_missing_system_or_bundled"}

    if evidence_dir.exists():
        shutil.rmtree(evidence_dir, ignore_errors=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    seeked = _capture_seeked_visual_evidence(
        root,
        creator_key,
        url,
        video_id,
        ffmpeg,
        evidence_dir,
        index_path,
        duration_seconds=duration_seconds,
        progress_callback=progress_callback,
    )
    if seeked.get("ok"):
        return seeked

    # Fail closed on the optimized path, then preserve the existing bounded
    # full-stream capture as a compatibility fallback. Explicitly reset seek
    # progress fields so merged heartbeat state reflects the active mode.
    if progress_callback is not None:
        progress_callback(_seeked_fallback_progress(seeked))
    shutil.rmtree(evidence_dir, ignore_errors=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    base, js_diag = _yt_base_args()
    sample_fps = visual_capture_sample_fps(duration_seconds)
    yt_cmd = [
        *base,
        "--format", YOUTUBE_VISUAL_FORMAT_SELECTOR,
        "--output", "-",
        url,
    ]
    filter_complex = (
        "[0:v]split=2[fpssrc][scsrc];"
        f"[fpssrc]fps={sample_fps:.8f},mpdecimate,showinfo@fps[fpsout];"
        "[scsrc]select='gt(scene\\,0.30)',showinfo@scene[scout]"
    )
    ff_cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "info", "-y",
        "-i", "pipe:0",
        "-filter_complex", filter_complex,
        "-map", "[fpsout]",
        "-frames:v", str(VISUAL_CAPTURE_MAX_FRAMES_PER_BRANCH),
        "-fps_mode", "vfr", "-q:v", "5", str(evidence_dir / "fps_%05d.jpg"),
        "-map", "[scout]",
        "-frames:v", str(VISUAL_CAPTURE_MAX_FRAMES_PER_BRANCH),
        "-fps_mode", "vfr", "-q:v", "5", str(evidence_dir / "scene_%05d.jpg"),
    ]

    with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as yt_err:
        yt_err_path = Path(yt_err.name)
    with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as ff_err:
        ff_err_path = Path(ff_err.name)

    monitor: dict = {}
    yt_rc = -1
    ff_rc = -1
    yt_stderr = ""
    ff_stderr = ""
    fps_times: list[float] = []
    scene_times: list[float] = []
    try:
        yt = None
        ff = None
        with yt_err_path.open("wb") as yt_err_file, ff_err_path.open("wb") as ff_err_file:
            try:
                yt = subprocess.Popen(
                    yt_cmd,
                    stdout=subprocess.PIPE,
                    stderr=yt_err_file,
                )
                if yt.stdout is None:
                    raise RuntimeError("yt_dlp_stdout_pipe_missing")
                ff = subprocess.Popen(
                    ff_cmd,
                    stdin=yt.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=ff_err_file,
                )
                # The ffmpeg child owns the read end now. Closing the parent's
                # duplicate avoids keeping yt-dlp alive if ffmpeg exits early.
                yt.stdout.close()
                monitor = _wait_visual_capture(
                    ff,
                    yt,
                    evidence_dir,
                    progress_callback=progress_callback,
                )
                ff_rc = int(ff.poll() if ff.poll() is not None else -1)
                if monitor.get("ok"):
                    try:
                        yt_rc = int(yt.wait(timeout=30))
                    except subprocess.TimeoutExpired:
                        _terminate_process(yt)
                        yt_rc = int(yt.poll() if yt.poll() is not None else -1)
                else:
                    yt_rc = int(yt.poll() if yt.poll() is not None else -1)
            finally:
                _terminate_process(ff)
                _terminate_process(yt)
        yt_stderr = _tail_text_file(yt_err_path)
        ff_stderr = _tail_text_file(ff_err_path)
        fps_times = _showinfo_times_file(ff_err_path, "fps")
        scene_times = _showinfo_times_file(ff_err_path, "scene")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        monitor = {
            "ok": False,
            "error": f"{type(exc).__name__}:VISUAL_CAPTURE_PROCESS_FAILED",
        }
        yt_stderr = _tail_text_file(yt_err_path)
        ff_stderr = _tail_text_file(ff_err_path)
    finally:
        with contextlib.suppress(OSError):
            yt_err_path.unlink()
        with contextlib.suppress(OSError):
            ff_err_path.unlink()

    if not monitor.get("ok"):
        shutil.rmtree(evidence_dir, ignore_errors=True)
        return {
            "ok": False,
            "error": monitor.get("error") or "VISUAL_CAPTURE_FAILED",
            "diagnostic_tail": (yt_stderr + "\n" + ff_stderr)[-2500:],
            "visual_capture_elapsed_seconds": monitor.get("elapsed_seconds"),
            "visual_capture_child_rss_peak_mib": monitor.get("child_rss_peak_mib"),
            "visual_capture_memory_limit_mib": monitor.get("memory_limit_mib"),
        }
    records = _frame_records(evidence_dir, "fps", fps_times, "ONE_FPS")
    records += _frame_records(evidence_dir, "scene", scene_times, "SCENE_CHANGE")
    candidate_count = len(records)
    records = _merge_frame_records(records)
    agent_visual_bundle = build_agent_visual_bundle(
        root,
        creator_key,
        records,
        ffmpeg,
        evidence_dir,
        progress_callback=progress_callback,
    )

    frames = []
    for rec in records:
        frames.append({
            "timestamp_s": rec["timestamp_s"],
            "reason": rec["reason"],
            "file": str(rec["file"].relative_to(root)),
            "size_bytes": rec["size_bytes"],
        })

    summary = {
        "capture_strategy": "BOUNDED_TIMELINE_PLUS_SCENE_CHANGE_WITH_FFMPEG_MPDECIMATE",
        "visual_capture_sample_fps": round(sample_fps, 6),
        "visual_capture_duration_seconds": duration_seconds,
        "visual_capture_max_frames_per_branch": VISUAL_CAPTURE_MAX_FRAMES_PER_BRANCH,
        "visual_capture_max_candidate_frames": VISUAL_CAPTURE_MAX_FRAMES_PER_BRANCH * 2,
        "visual_capture_format_selector": YOUTUBE_VISUAL_FORMAT_SELECTOR,
        "seeked_sampling_fallback_reason": seeked.get("error"),
        "seeked_sampling_fallback_diagnostic_tail": seeked.get("diagnostic_tail"),
        "visual_capture_elapsed_seconds": monitor.get("elapsed_seconds"),
        "visual_capture_child_rss_peak_mib": monitor.get("child_rss_peak_mib"),
        "visual_capture_memory_limit_mib": monitor.get("memory_limit_mib"),
        "candidate_frames": candidate_count,
        "retained_frames": len(frames),
        "scene_change_frames": sum(1 for x in frames if x["reason"] == "SCENE_CHANGE"),
        "one_fps_frames": sum(1 for x in frames if x["reason"] == "ONE_FPS"),
        "video_persisted": False,
        "agent_visual_bundle": agent_visual_bundle,
    }
    index = {
        "schema_version": 1,
        "app_version": YOUTUBE_EVAL_VERSION,
        "source_platform": "YOUTUBE",
        "source_url": url,
        "video_id": video_id,
        "generated_at": utc_now(),
        "summary": summary,
        "frames": frames,
        "agent_visual_bundle": agent_visual_bundle,
        "diagnostics": {
            "yt_dlp_returncode": yt_rc,
            "ffmpeg_returncode": ff_rc,
            "visual_capture_monitor": monitor,
            "yt_dlp_tail": yt_stderr[-2000:],
            "ffmpeg_tail": ff_stderr[-2000:],
            "js_runtime": js_diag,
        },
    }
    # Recent ffmpeg builds can return -22 when the optional scene-change image2
    # branch receives zero packets even though the 1-fps branch completed and
    # produced valid frames. Treat only that specific empty-scene condition as
    # non-fatal; other ffmpeg failures still fail closed.
    benign_empty_scene = (
        ff_rc != 0
        and bool(frames)
        and any(x.get("reason") == "ONE_FPS" for x in frames)
        and "Nothing was written into output file" in ff_stderr
        and "received no packets" in ff_stderr
        and "Could not open encoder before EOF" in ff_stderr
    )
    summary["ffmpeg_nonzero_accepted"] = bool(benign_empty_scene)
    if benign_empty_scene:
        summary["visual_capture_warning"] = "EMPTY_SCENE_BRANCH_NO_PACKETS"
    index["summary"] = summary
    index["diagnostics"]["benign_empty_scene"] = bool(benign_empty_scene)
    atomic_json(index_path, index)
    ok = yt_rc == 0 and bool(frames) and (ff_rc == 0 or benign_empty_scene)
    return {"ok": ok, "index": index_path, **summary, "diagnostic_tail": (yt_stderr + "\n" + ff_stderr)[-2500:]}


def read_info_json(root: Path, creator_key: str, video_id: str) -> dict:
    caption_dir = root / "output" / creator_key / "youtube" / "captions"
    info_path = caption_dir / f"{video_id}.info.json"
    if info_path.exists():
        try:
            return json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def run_research_queue(root: Path, *, must_include: list[str] | None = None) -> dict:
    script = root / "app" / "research_queue.py"
    cmd = [sys.executable, str(script), "--root", str(root)]
    for shortcode in must_include or []:
        cmd.extend(["--must-include-shortcode", shortcode])
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=False)
    return {
        "ok": p.returncode == 0,
        "returncode": p.returncode,
        "stdout_tail": (p.stdout or "")[-3000:],
        "stderr_tail": (p.stderr or "")[-3000:],
    }


def delivery_dispositions(
    completed: list[str],
    *,
    manifest: dict,
    decisions: dict,
    queue: dict,
) -> dict:
    final_decisions = {
        "IGNORE", "RESEARCH", "TEST_CANDIDATE", "BACKLOG_CANDIDATE",
        "TEST", "BACKLOG",
    }
    queue_items = queue.get("items", []) if isinstance(queue, dict) else []
    queued = {
        str(item.get("shortcode") or "")
        for item in queue_items
        if isinstance(item, dict)
    }
    manifest_items = manifest.get("items", {}) if isinstance(manifest, dict) else {}
    decision_items = decisions.get("items", {}) if isinstance(decisions, dict) else {}

    dispositions: dict[str, str] = {}
    expected_queue: list[str] = []
    undelivered: list[str] = []
    for shortcode in completed:
        item = manifest_items.get(shortcode, {}) if isinstance(manifest_items, dict) else {}
        decision = decision_items.get(shortcode, {}) if isinstance(decision_items, dict) else {}
        if isinstance(decision, dict) and decision.get("decision") in final_decisions:
            dispositions[shortcode] = "FINALIZED"
            continue
        if isinstance(item, dict) and item.get("duplicate_of"):
            dispositions[shortcode] = "DUPLICATE"
            continue
        expected_queue.append(shortcode)
        if shortcode in queued:
            dispositions[shortcode] = "QUEUED"
        else:
            dispositions[shortcode] = "UNDELIVERED"
            undelivered.append(shortcode)
    return {
        "dispositions": dispositions,
        "expected_queue": expected_queue,
        "undelivered": undelivered,
    }


def reconcile_delivery(root: Path, targets: list[str]) -> tuple[dict, dict, dict]:
    queue_path = root / "state" / "research_queue.json"
    manifest_path = root / "state" / "manifest.json"
    decisions_path = root / "state" / "research_decisions.json"

    def snapshot() -> tuple[dict, dict]:
        queue = load_json(queue_path, {"items": []})
        manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
        decisions = load_json(decisions_path, {"schema_version": 2, "items": {}})
        return queue, delivery_dispositions(
            targets, manifest=manifest, decisions=decisions, queue=queue
        )

    queue, delivery = snapshot()
    if not delivery["undelivered"]:
        return {
            "ok": True,
            "returncode": 0,
            "skipped": "DELIVERY_ALREADY_REACHABLE",
        }, queue, delivery

    queue_result = run_research_queue(root, must_include=targets)
    queue, delivery = snapshot()
    return queue_result, queue, delivery


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--channel-url", required=True)
    ap.add_argument("--creator-key", required=True)
    ap.add_argument("--creator-name", required=True)
    ap.add_argument("--sample-size", type=int, default=20)
    ap.add_argument("--verification-basis", default="WEB_VERIFIED_CHANNEL")
    ap.add_argument("--only-video-ids", default="", help=argparse.SUPPRESS)
    ap.add_argument("--seed-video-ids", default="", help=argparse.SUPPRESS)
    ap.add_argument("--required-attribution-term", default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    root = args.root.resolve()
    creator_key = re.sub(r"[^a-z0-9]+", "", args.creator_key.casefold())[:80]
    sample_size = max(1, min(int(args.sample_size), 100))
    run_id = f"eval-{creator_key}-youtube-{compact_timestamp()}"
    started = utc_now()
    status_path = root / "state" / "creator_evaluation_status.json"
    immutable_status_path = root / "state" / "evaluations" / f"{run_id}.json"

    base_status = {
        "schema_version": 1,
        "evaluation_version": YOUTUBE_EVAL_VERSION,
        "system_name": "InfluencerResearch",
        "evaluation_mode": "CREATOR_EVALUATION",
        "evaluation_run_id": run_id,
        "creator": creator_key,
        "creator_name": args.creator_name,
        "source_platform": "YOUTUBE",
        "source_profile": args.channel_url,
        "sample_size": sample_size,
        "permanent_source": False,
        "instagram_accessed": False,
        "network_scope": ["YOUTUBE"],
        "started_at": started,
        "creator_verification": {
            "status": "WEB_VERIFIED",
            "match_basis": str(args.verification_basis or "WEB_VERIFIED_CHANNEL"),
            "youtube_channel": args.channel_url,
        },
        "evidence_policy": {
            "transcript_order": ["YOUTUBE_CAPTIONS", "FASTER_WHISPER_FALLBACK"],
            "visual_capture": "BOUNDED_TIMELINE_PLUS_SCENE_CHANGE",
            "visual_capture_max_frames_per_branch": VISUAL_CAPTURE_MAX_FRAMES_PER_BRANCH,
            "visual_capture_max_candidate_frames": VISUAL_CAPTURE_MAX_FRAMES_PER_BRANCH * 2,
            "visual_capture_format_selector": YOUTUBE_VISUAL_FORMAT_SELECTOR,
            "visual_capture_max_child_rss_mb": round(
                visual_capture_max_child_rss_bytes() / (1024 * 1024)
            ),
            "visual_capture_heartbeat_seconds": VISUAL_CAPTURE_HEARTBEAT_SECONDS,
            "full_video_persisted": False,
            "max_unpinned_whisper_duration_seconds": max_unpinned_whisper_duration_seconds(),
            "longform_policy": "DEFER_UNPINNED_NO_CAPTIONS_AND_BACKFILL",
        },
        "requested_sample_size": sample_size,
    }
    atomic_json(status_path, {**base_status, "state": "RUNNING", "updated_at": started})
    heartbeat(status_path, "DISCOVERY", requested_sample_size=sample_size)

    manifest_path = root / "state" / "manifest.json"
    manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
    manifest.setdefault("items", {})

    try:
        exact_ids = parse_exact_video_ids(args.only_video_ids)
        seed_ids = parse_exact_video_ids(args.seed_video_ids)
    except ValueError as exc:
        status = {**base_status, "state": "BAD_VIDEO_ID_FILTER", "finished_at": utc_now(), "error": str(exc)}
        atomic_json(status_path, status)
        atomic_json(immutable_status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 6

    required_attribution_term = str(args.required_attribution_term or "").strip()
    if len(required_attribution_term) > 120 or any(ord(ch) < 32 for ch in required_attribution_term):
        status = {**base_status, "state": "BAD_ATTRIBUTION_TERM", "finished_at": utc_now()}
        atomic_json(status_path, status)
        atomic_json(immutable_status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 6

    duplicate_count = 0
    if exact_ids:
        entries, discovery_diag = exact_video_entries(
            args.channel_url, exact_ids, required_attribution_term=required_attribution_term)
        if len(entries) != len(exact_ids):
            status = {
                **base_status,
                "state": "EXACT_VIDEO_IDENTITY_REJECTED",
                "finished_at": utc_now(),
                "discovery": discovery_diag,
                "entries_discovered": len(entries),
            }
            atomic_json(status_path, status)
            atomic_json(immutable_status_path, status)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 6
    else:
        channel_entries, channel_diag = enumerate_channel(
            args.channel_url,
            limit=max(sample_size * 4, 50),
        )
        seed_entries: list[dict] = []
        seed_diag: dict = {"mode": "NO_SEEDS", "requested": 0, "accepted": 0}
        if seed_ids:
            seed_entries, seed_diag = exact_video_entries(
                args.channel_url,
                seed_ids,
                required_attribution_term=required_attribution_term,
            )

        entries, duplicate_count = select_seeded_sample(
            seed_entries,
            channel_entries,
            max(sample_size * 4, 50),
        )

        discovery_diag = {
            "mode": "SEEDED_CHANNEL_ENUMERATION" if seed_ids else "CHANNEL_ENUMERATION",
            "seed": seed_diag,
            "channel": channel_diag,
            "seed_requested": len(seed_ids),
            "seed_resolved": len(seed_entries),
            "unique_entries": len(entries),
            "duplicate_count": duplicate_count,
        }
        if not entries:
            status = {
                **base_status,
                "state": "NO_YOUTUBE_ENTRIES",
                "finished_at": utc_now(),
                "discovery": discovery_diag,
            }
            atomic_json(status_path, status)
            atomic_json(immutable_status_path, status)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 4

    eligible_count = len(entries)
    heartbeat(
        status_path,
        "SELECTION",
        requested_sample_size=sample_size,
        discovered_count=len(entries),
        eligible_count=eligible_count,
        duplicate_count=duplicate_count,
    )

    known = manifest["items"]
    def is_complete_entry(entry: dict) -> bool:
        key = f"yt_{entry['id']}"
        return (
            key in known
            and known[key].get("download_status") == "DONE"
            and known[key].get("transcription_status") == "DONE"
            and known[key].get("visual_evidence_status") == "DONE"
        )

    must_include_ids = set(exact_ids or seed_ids)
    selected_entries, deferred_items, _selection_cursor = select_bounded_evidence_sample(
        root,
        creator_key,
        entries,
        sample_size,
        must_include_ids=must_include_ids,
        channel_url=args.channel_url,
        required_attribution_term=required_attribution_term,
        is_complete_entry=is_complete_entry,
    )
    candidates = [entry for entry in selected_entries if not is_complete_entry(entry)]
    selected_queue_ids = [f"yt_{entry['id']}" for entry in selected_entries]
    candidate_queue_ids = [f"yt_{entry['id']}" for entry in candidates]
    existing_delivery_targets = [
        f"yt_{entry['id']}"
        for entry in selected_entries
        if is_complete_entry(entry)
    ]
    eligible_count = max(0, len(entries) - len(deferred_items))
    heartbeat(
        status_path,
        "SELECTION",
        requested_sample_size=sample_size,
        discovered_count=len(entries),
        eligible_count=eligible_count,
        selected_count=len(selected_entries),
        deferred_count=len(deferred_items),
        duplicate_count=duplicate_count,
        selected_queue_ids=selected_queue_ids,
        candidate_queue_ids=candidate_queue_ids,
        existing_delivery_target_ids=existing_delivery_targets,
    )

    model_holder = {"model": None}
    completed: list[str] = []
    failures: list[dict] = []
    caption_count = 0
    whisper_count = 0
    visual_frame_count = 0
    incremental_delivery_failures: list[dict] = []

    for index, entry in enumerate(candidates, start=1):
        vid = entry["id"]
        url = entry["url"]
        key = f"yt_{vid}"
        old = manifest["items"].get(key, {})

        heartbeat(
            status_path,
            "EVIDENCE",
            current_index=index,
            current_source_id=vid,
            selected_count=len(selected_entries),
            completed_count=len(completed) + len(existing_delivery_targets),
            failed_count=len(failures),
        )
        def visual_capture_progress(metrics: dict) -> None:
            heartbeat(
                status_path,
                "EVIDENCE",
                current_index=index,
                current_source_id=vid,
                selected_count=len(selected_entries),
                completed_count=len(completed) + len(existing_delivery_targets),
                failed_count=len(failures),
                **metrics,
            )

        visual = capture_visual_evidence(
            root,
            creator_key,
            url,
            vid,
            duration_seconds=_duration_seconds({"duration": entry.get("duration_seconds")}),
            progress_callback=visual_capture_progress,
        )
        if not visual.get("ok"):
            failures.append({
                "video_id": vid,
                "url": url,
                "stage": "visual_capture",
                "detail": visual.get("error") or visual.get("diagnostic_tail"),
                "visual_capture_child_rss_peak_mib": visual.get("visual_capture_child_rss_peak_mib"),
                "visual_capture_memory_limit_mib": visual.get("visual_capture_memory_limit_mib"),
            })
            continue

        heartbeat(
            status_path,
            "TRANSCRIPTION",
            current_index=index,
            current_source_id=vid,
            selected_count=len(selected_entries),
            completed_count=len(completed) + len(existing_delivery_targets),
            failed_count=len(failures),
        )
        tr = fetch_captions(root, creator_key, url, vid)
        if tr.get("ok"):
            transcript_source = str(tr.get("source") or "YOUTUBE_CAPTIONS")
            if transcript_source == "YOUTUBE_CAPTIONS":
                caption_count += 1
        else:
            with tempfile.TemporaryDirectory(prefix=f"influencerresearch_{vid}_") as td:
                dl = download_audio_temp(url, vid, Path(td))
                if not dl.get("ok") or not dl.get("media_file"):
                    failures.append({"video_id": vid, "url": url, "stage": "audio_fallback_download", "detail": dl.get("diagnostic_tail") or dl.get("validation")})
                    continue
                try:
                    def whisper_progress(segment_count: int) -> None:
                        heartbeat(
                            status_path,
                            "TRANSCRIPTION",
                            current_index=index,
                            current_source_id=vid,
                            selected_count=len(selected_entries),
                            completed_count=len(completed) + len(existing_delivery_targets),
                            failed_count=len(failures),
                            transcription_segment_count=segment_count,
                        )

                    tr = transcribe_whisper(
                        root,
                        creator_key,
                        vid,
                        Path(dl["media_file"]),
                        model_holder=model_holder,
                        progress_callback=whisper_progress,
                    )
                except Exception as exc:
                    failures.append({"video_id": vid, "url": url, "stage": "transcription", "detail": f"{type(exc).__name__}:{exc}"})
                    continue
            if not tr.get("ok"):
                failures.append({"video_id": vid, "url": url, "stage": "transcription", "detail": tr.get("error")})
                continue
            transcript_source = "FASTER_WHISPER_FALLBACK"
            whisper_count += 1

        info = read_info_json(root, creator_key, vid)
        caption = str(info.get("description") or info.get("title") or entry.get("title") or "").strip()
        txt_path = Path(tr["txt"])
        json_path = Path(tr["json"])
        visual_index = Path(visual["index"])

        manifest["items"][key] = {
            **old,
            "schema_version": 1,
            "system_name": "InfluencerResearch",
            "source_platform": "YOUTUBE",
            "source_type": "VIDEO",
            "source_id": vid,
            "shortcode": key,
            "creator": creator_key,
            "url": url,
            "caption": caption,
            "published_at": published_iso(info),
            # download_status remains DONE for backward compatibility: evidence ingestion completed.
            "downloaded_at": old.get("downloaded_at") or utc_now(),
            "download_status": "DONE",
            "media_file": None,
            "media_retention": "EPHEMERAL_ONLY",
            "full_video_persisted": False,
            "transcribed_at": tr["transcribed_at"],
            "transcription_status": "DONE",
            "transcript_source": transcript_source,
            "transcript_txt": str(txt_path.relative_to(root)),
            "transcript_json": str(json_path.relative_to(root)),
            "caption_file": str(Path(tr["caption_file"]).relative_to(root)) if tr.get("caption_file") else None,
            "visual_evidence_status": "DONE",
            "visual_evidence_index": str(visual_index.relative_to(root)),
            "visual_frame_count": int(visual.get("retained_frames") or 0),
            "visual_capture_strategy": visual.get("capture_strategy"),
            "agent_visual_bundle": visual.get("agent_visual_bundle"),
            "analysis_mode_recommended": (visual.get("agent_visual_bundle") or {}).get("analysis_mode_recommended"),
            "visual_review_recommended": bool((visual.get("agent_visual_bundle") or {}).get("visual_review_recommended")),
            "visual_review_reason": (visual.get("agent_visual_bundle") or {}).get("visual_review_reason") or [],
            "research_status": old.get("research_status") or "PENDING",
            "source_class": "INFLUENCER_DISCOVERY_SECONDARY",
            "evaluation_mode": "CREATOR_EVALUATION",
            "evaluation_run_id": run_id,
            "evaluation_source_profile": args.channel_url,
            "evaluation_sample_size": sample_size,
            "permanent_source": False,
            "creator_verification": base_status["creator_verification"],
            "evaluation_discovery_mode": discovery_diag.get("mode", "CHANNEL_ENUMERATION"),
            "shared_channel_attribution": entry.get("attribution"),
        }
        visual_frame_count += int(visual.get("retained_frames") or 0)
        completed.append(key)
        atomic_json(manifest_path, manifest)

        # Make each completed evidence item analysis-reachable before moving to
        # the next expensive candidate. A later STOP/watchdog must not strand
        # already-persisted work outside the canonical queue.
        incremental_queue_result, _, incremental_delivery = reconcile_delivery(root, [key])
        if not incremental_queue_result.get("ok") or incremental_delivery["undelivered"]:
            incremental_delivery_failures.append({
                "queue_id": key,
                "returncode": incremental_queue_result.get("returncode"),
                "undelivered": incremental_delivery["undelivered"],
            })

        # research_queue may add canonical lineage/research metadata to the
        # manifest. Reload it before the next item so the evaluator never
        # overwrites those queue-side updates with stale in-memory state.
        manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
        manifest.setdefault("items", {})
        known = manifest["items"]

    if not candidates:
        heartbeat(
            status_path,
            "QUEUE_WRITE",
            requested_sample_size=sample_size,
            discovered_count=len(entries),
            eligible_count=eligible_count,
            selected_count=len(selected_entries),
            completed_count=len(existing_delivery_targets),
            duplicate_count=duplicate_count,
            failed_count=0,
        )
        queue_result, queue, delivery = reconcile_delivery(root, existing_delivery_targets)
        delivery_ok = queue_result.get("ok") and not delivery["undelivered"]
        state = "NO_NEW_CONTENT" if delivery_ok else "DELIVERY_INCOMPLETE"
        delivery_queued = [
            shortcode for shortcode, disposition in delivery["dispositions"].items()
            if disposition == "QUEUED"
        ]
        status = {
            **base_status,
            "state": state,
            "finished_at": utc_now(),
            "discovery": discovery_diag,
            "entries_discovered": len(entries),
            "discovered_count": len(entries),
            "eligible_count": eligible_count,
            "selected_count": len(selected_entries),
            "selected_queue_ids": selected_queue_ids,
            "candidate_queue_ids": candidate_queue_ids,
            "existing_delivery_target_ids": existing_delivery_targets,
            "candidate_count": 0,
            "deferred_count": len(deferred_items),
            "deferred": deferred_items[:50],
            "completed_count": len(existing_delivery_targets),
            "completed": existing_delivery_targets,
            "duplicate_count": duplicate_count,
            "failed_count": 0,
            "requested_sample_size": sample_size,
            "sample_complete": sample_outcome(
                sample_size,
                len(selected_entries),
                len(existing_delivery_targets),
                0,
            )[0],
            "shortfall_reason": sample_outcome(
                sample_size,
                len(selected_entries),
                len(existing_delivery_targets),
                0,
            )[1],
            "transcript_sources": {"youtube_captions": 0, "faster_whisper_fallback": 0},
            "retained_visual_frames": 0,
            "failure_count": 0,
            "failures": [],
            "queue": queue_result,
            "queued_for_run_count": 0,
            "queued_for_run": [],
            "delivery_target_count": len(existing_delivery_targets),
            "delivery_targets": existing_delivery_targets,
            "delivery_expected_queue_count": len(delivery["expected_queue"]),
            "delivery_expected_queue": delivery["expected_queue"],
            "delivery_queued_count": len(delivery_queued),
            "delivery_queued": delivery_queued,
            "delivery_undelivered_count": len(delivery["undelivered"]),
            "delivery_undelivered": delivery["undelivered"],
            "delivery_dispositions": delivery["dispositions"],
        }
        sample_complete = bool(status.get("sample_complete"))
        terminal_state = (
            "COMPLETE"
            if delivery_ok and sample_complete
            else ("PARTIAL" if delivery_ok else "FAILED")
        )
        status = terminalize(
            status_path,
            terminal_state,
            terminal_reason=status.get("shortfall_reason"),
            **{key: value for key, value in status.items() if key not in {"state", "progress"}},
        )
        atomic_json(immutable_status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if terminal_state == "COMPLETE" else 1

    heartbeat(
        status_path,
        "QUEUE_WRITE",
        requested_sample_size=sample_size,
        discovered_count=len(entries),
        eligible_count=eligible_count,
        selected_count=len(selected_entries),
        completed_count=len(completed) + len(existing_delivery_targets),
        duplicate_count=duplicate_count,
        failed_count=len(failures),
    )
    delivery_targets = list(dict.fromkeys([*existing_delivery_targets, *completed]))
    queue_result, queue, delivery = reconcile_delivery(root, delivery_targets)
    queue_items = queue.get("items", []) if isinstance(queue, dict) else []
    queued_for_run = [
        item.get("shortcode") for item in queue_items
        if isinstance(item, dict) and item.get("evaluation_run_id") == run_id
    ]

    completed_count = len(completed) + len(existing_delivery_targets)
    sample_complete, shortfall_reason = sample_outcome(
        sample_size,
        len(selected_entries),
        completed_count,
        len(failures),
    )

    complete = (
        sample_complete
        and len(completed) == len(candidates)
        and not failures
        and queue_result.get("ok")
        and not delivery["undelivered"]
    )
    state = "COMPLETE" if complete else ("PARTIAL" if completed_count or deferred_items else "FAILED")
    heartbeat(
        status_path,
        "FINALIZING",
        requested_sample_size=sample_size,
        discovered_count=len(entries),
        eligible_count=eligible_count,
        selected_count=len(selected_entries),
        completed_count=completed_count,
        duplicate_count=duplicate_count,
        failed_count=len(failures),
        queued_count=sum(1 for x in delivery["dispositions"].values() if x == "QUEUED"),
        sample_complete=sample_complete,
        shortfall_reason=shortfall_reason,
    )
    status = {
        **base_status,
        "state": state,
        "finished_at": utc_now(),
        "discovery": discovery_diag,
        "entries_discovered": len(entries),
        "discovered_count": len(entries),
        "eligible_count": eligible_count,
        "selected_count": len(selected_entries),
        "selected_queue_ids": selected_queue_ids,
        "candidate_queue_ids": candidate_queue_ids,
        "existing_delivery_target_ids": existing_delivery_targets,
        "candidate_count": len(candidates),
        "deferred_count": len(deferred_items),
        "deferred": deferred_items[:50],
        "completed_count": completed_count,
        "completed": delivery_targets,
        "duplicate_count": duplicate_count,
        "failed_count": len(failures),
        "requested_sample_size": sample_size,
        "sample_complete": sample_complete,
        "shortfall_reason": shortfall_reason,
        "transcript_sources": {"youtube_captions": caption_count, "faster_whisper_fallback": whisper_count},
        "retained_visual_frames": visual_frame_count,
        "failure_count": len(failures),
        "failures": failures[:50],
        "incremental_delivery_failure_count": len(incremental_delivery_failures),
        "incremental_delivery_failures": incremental_delivery_failures[:50],
        "queue": queue_result,
        "queued_for_run_count": len(queued_for_run),
        "queued_for_run": queued_for_run,
        "delivery_target_count": len(completed),
        "delivery_targets": completed,
        "delivery_expected_queue_count": len(delivery["expected_queue"]),
        "delivery_expected_queue": delivery["expected_queue"],
        "delivery_queued_count": sum(1 for x in delivery["dispositions"].values() if x == "QUEUED"),
        "delivery_queued": [x for x, disposition in delivery["dispositions"].items() if disposition == "QUEUED"],
        "delivery_undelivered_count": len(delivery["undelivered"]),
        "delivery_undelivered": delivery["undelivered"],
        "delivery_dispositions": delivery["dispositions"],
    }
    status = terminalize(
        status_path,
        state,
        terminal_reason=shortfall_reason if state == "COMPLETE" and not sample_complete else None,
        **{key: value for key, value in status.items() if key not in {"state", "progress"}},
    )
    atomic_json(immutable_status_path, status)
    print(json.dumps(status, ensure_ascii=False, indent=2))

    if state == "COMPLETE":
        return 0
    if completed:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
