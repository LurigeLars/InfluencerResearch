from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from evaluation_progress import heartbeat, sample_outcome, terminalize
from urllib.parse import urlparse

YOUTUBE_EVAL_VERSION = "0.8.0"
DEFAULT_MAX_UNPINNED_WHISPER_DURATION_SECONDS = 20 * 60


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
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=False)
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


def enumerate_channel(channel_url: str, *, limit: int) -> tuple[list[dict], dict]:
    """Enumerate Videos/Shorts/Streams under one global discovery budget.

    yt-dlp applies --playlist-end independently to nested channel tabs when pointed
    at a channel root. That made limit=15 return up to 45 entries. Split the global
    budget across explicit surfaces instead and merge them round-robin.
    """
    channel_url = canonical_youtube_channel_url(channel_url)
    global_limit = max(1, int(limit))
    surfaces = ("videos", "shorts", "streams")
    base, remainder = divmod(global_limit, len(surfaces))
    surface_limits = {
        surface: base + (1 if index < remainder else 0)
        for index, surface in enumerate(surfaces)
    }

    active = [
        (surface, surface_limits[surface])
        for surface in surfaces
        if surface_limits[surface] > 0
    ]
    if len(active) == 1:
        surface_results = [
            _enumerate_channel_surface(
                channel_url,
                surface=active[0][0],
                limit=active[0][1],
            )
        ]
    else:
        with ThreadPoolExecutor(
            max_workers=len(active),
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

    entries_by_surface = {
        diag["surface"]: entries
        for entries, diag in surface_results
    }
    diagnostics = {
        diag["surface"]: diag
        for _, diag in surface_results
    }

    merged: list[dict] = []
    seen: set[str] = set()
    max_depth = max((len(entries) for entries in entries_by_surface.values()), default=0)
    for index in range(max_depth):
        for surface in surfaces:
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
        "ok": bool(merged) and all(
            int(item.get("returncode") or 0) == 0
            for item in diagnostics.values()
        ),
        "returncode": next(
            (
                int(item.get("returncode") or 0)
                for item in diagnostics.values()
                if int(item.get("returncode") or 0) != 0
            ),
            0,
        ),
        "requested_limit": global_limit,
        "entries_found": len(merged),
        "surface_limits": surface_limits,
        "surfaces": diagnostics,
        "diagnostic_tail": "\n--- surface ---\n".join(
            str(item.get("diagnostic_tail") or "")
            for item in diagnostics.values()
            if str(item.get("diagnostic_tail") or "").strip()
        )[-2500:],
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



VISUAL_OCR_MAX_FRAMES = 24
VISUAL_REPRESENTATIVE_FRAMES = 12
VISUAL_OCR_TIMEOUT_SECONDS = 8
CHART_HEAVY_CREATORS = {"thetradingfraternity"}
CHART_TERMS = {
    "support", "resistance", "breakout", "trend", "vwap", "volume", "price",
    "yield", "spread", "gamma", "delta", "rsi", "macd", "moving average",
    "s&p", "spx", "nasdaq", "qqq", "dow", "dxy", "vix", "btc", "eth",
    "treasury", "crude", "oil", "gold", "copper", "eur", "usd", "jpy",
}


def _sample_visual_records(records: list[dict], limit: int = VISUAL_OCR_MAX_FRAMES) -> list[dict]:
    if len(records) <= limit:
        return list(records)
    scene = [row for row in records if row.get("reason") == "SCENE_CHANGE"]
    selected: list[dict] = []
    seen: set[str] = set()
    for row in scene[: max(1, limit // 2)]:
        key = str(row.get("file") or "")
        if key and key not in seen:
            selected.append(row)
            seen.add(key)
    remaining = max(0, limit - len(selected))
    if remaining:
        step = max(1, len(records) // remaining)
        for row in records[::step]:
            key = str(row.get("file") or "")
            if key and key not in seen:
                selected.append(row)
                seen.add(key)
            if len(selected) >= limit:
                break
    return sorted(selected[:limit], key=lambda row: float(row.get("timestamp_s") or 0.0))


def _run_tesseract_visual_frame(path: Path, psm: int) -> str:
    try:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "eng", "--psm", str(psm)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=VISUAL_OCR_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return re.sub(r"[ \t]+", " ", str(proc.stdout or "")).strip()


def _ocr_visual_frame(path: Path) -> str:
    # Sparse-text mode works well for charts and dashboards. Social-video captions
    # can instead present as one coherent block, so retry with a block layout only
    # when the sparse pass found nothing.
    text = _run_tesseract_visual_frame(path, 11)
    if text:
        return text
    return _run_tesseract_visual_frame(path, 6)


def _score_visual_frame_text(text: str) -> tuple[float, list[str]]:
    normalized = str(text or "").strip()
    lower = normalized.casefold()
    if not normalized:
        return 0.0, []
    reasons: list[str] = []
    term_hits = sum(1 for term in CHART_TERMS if term in lower)
    numeric_hits = len(re.findall(r"(?:[$€£]?\d+(?:[.,]\d+)?%?)", normalized))
    ticker_hits = len(re.findall(r"\b[A-Z]{2,6}\b", normalized))
    score = min(10.0, term_hits * 1.8 + min(numeric_hits, 8) * 0.35 + min(ticker_hits, 6) * 0.25)
    if term_hits:
        reasons.append("CHART_TERMS")
    if numeric_hits >= 3:
        reasons.append("NUMERIC_DENSITY")
    if ticker_hits >= 2:
        reasons.append("TICKER_DENSITY")
    if len(normalized.split()) >= 18:
        score += 0.5
        reasons.append("TEXT_DENSITY")
    return round(min(score, 10.0), 2), reasons


def _make_contact_sheet(ffmpeg: str, evidence_dir: Path, selected: list[dict]) -> Path | None:
    if not selected:
        return None
    staging = evidence_dir / ".contact_sheet"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for idx, row in enumerate(selected[:VISUAL_REPRESENTATIVE_FRAMES]):
            source = Path(row["file"])
            shutil.copyfile(source, staging / f"frame_{idx:02d}.jpg")
        out = evidence_dir / "contact_sheet.jpg"
        try:
            proc = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-framerate", "1", "-i", str(staging / "frame_%02d.jpg"),
                    "-vf", "scale=320:-2,tile=4x3:padding=4:margin=4",
                    "-frames:v", "1", str(out),
                ],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out if proc.returncode == 0 and out.exists() else None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def build_agent_visual_bundle(
    root: Path, creator_key: str, records: list[dict], ffmpeg: str, evidence_dir: Path
) -> dict:
    sampled = _sample_visual_records(records)

    def inspect(row: dict) -> dict:
        text = _ocr_visual_frame(Path(row["file"]))
        score, reasons = _score_visual_frame_text(text)
        return {**row, "ocr_text": text[:1200], "visual_score": score, "visual_signals": reasons}

    if sampled:
        with ThreadPoolExecutor(max_workers=min(4, len(sampled)), thread_name_prefix="visual-ocr") as executor:
            inspected = list(executor.map(inspect, sampled))
    else:
        inspected = []

    strong = [row for row in inspected if float(row.get("visual_score") or 0.0) >= 2.0]
    chart_ratio = (len(strong) / len(inspected)) if inspected else 0.0
    creator_prior = "HIGH" if creator_key.casefold() in CHART_HEAVY_CREATORS else "NEUTRAL"
    content_signal = len(strong) >= 2 or chart_ratio >= 0.20
    visual_review_recommended = bool(inspected) and (creator_prior == "HIGH" or content_signal)
    reasons: list[str] = []
    if creator_prior == "HIGH":
        reasons.append("CREATOR_CHART_PRIOR")
    if content_signal:
        reasons.append("PER_VIDEO_VISUAL_SIGNAL")

    ranked = sorted(
        inspected,
        key=lambda row: (float(row.get("visual_score") or 0.0), row.get("reason") == "SCENE_CHANGE"),
        reverse=True,
    )
    representative = ranked[:VISUAL_REPRESENTATIVE_FRAMES]
    if len(representative) < min(3, len(inspected)):
        representative = inspected[: min(VISUAL_REPRESENTATIVE_FRAMES, len(inspected))]
    representative = sorted(representative, key=lambda row: float(row.get("timestamp_s") or 0.0))
    contact_sheet = _make_contact_sheet(ffmpeg, evidence_dir, representative)

    def public_row(row: dict) -> dict:
        path = Path(row["file"])
        return {
            "timestamp_s": row.get("timestamp_s"),
            "reason": row.get("reason"),
            "file": str(path.relative_to(root)),
            "visual_score": row.get("visual_score"),
            "visual_signals": row.get("visual_signals"),
            "ocr_text": row.get("ocr_text"),
        }

    return {
        "available": bool(representative),
        "analysis_mode_recommended": (
            "TRANSCRIPT_PLUS_VISUAL_REVIEW" if visual_review_recommended else "TRANSCRIPT_ONLY"
        ),
        "visual_review_recommended": visual_review_recommended,
        "visual_review_reason": reasons,
        "creator_visual_prior": creator_prior,
        "sampled_frame_count": len(inspected),
        "chart_signal_frame_count": len(strong),
        "chart_signal_ratio": round(chart_ratio, 3),
        "contact_sheet": str(contact_sheet.relative_to(root)) if contact_sheet else None,
        "representative_frames": [public_row(row) for row in representative],
    }

def capture_visual_evidence(root: Path, creator_key: str, url: str, video_id: str) -> dict:
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
                            root, creator_key, records, ffmpeg, evidence_dir
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

    base, js_diag = _yt_base_args()
    yt_cmd = [
        *base,
        "--format", "bv*[height<=720]/b[height<=720]",
        "--output", "-",
        url,
    ]
    filter_complex = (
        "[0:v]split=2[fpssrc][scsrc];"
        "[fpssrc]fps=1,mpdecimate,showinfo@fps[fpsout];"
        "[scsrc]select='gt(scene\\,0.30)',showinfo@scene[scout]"
    )
    ff_cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "info", "-y",
        "-i", "pipe:0",
        "-filter_complex", filter_complex,
        "-map", "[fpsout]", "-fps_mode", "vfr", "-q:v", "5", str(evidence_dir / "fps_%05d.jpg"),
        "-map", "[scout]", "-fps_mode", "vfr", "-q:v", "5", str(evidence_dir / "scene_%05d.jpg"),
    ]

    with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as yt_err:
        yt_err_path = Path(yt_err.name)
    try:
        yt = None
        with yt_err_path.open("wb") as yt_err_file:
            try:
                yt = subprocess.Popen(yt_cmd, stdout=subprocess.PIPE, stderr=yt_err_file)
                try:
                    ff = subprocess.run(ff_cmd, stdin=yt.stdout, capture_output=True, timeout=600)
                finally:
                    if yt.stdout is not None:
                        yt.stdout.close()
                try:
                    yt_rc = yt.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    yt.kill()
                    yt_rc = yt.wait(timeout=10)
            finally:
                if yt is not None and yt.poll() is None:
                    yt.terminate()
                    try:
                        yt.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        yt.kill()
                        yt.wait(timeout=10)
        yt_stderr = yt_err_path.read_text(encoding="utf-8", errors="replace")
    finally:
        with contextlib.suppress(OSError):
            yt_err_path.unlink()

    ff_stderr = (ff.stderr or b"").decode("utf-8", errors="replace")
    fps_times = _showinfo_times(ff_stderr, "fps")
    scene_times = _showinfo_times(ff_stderr, "scene")
    records = _frame_records(evidence_dir, "fps", fps_times, "ONE_FPS")
    records += _frame_records(evidence_dir, "scene", scene_times, "SCENE_CHANGE")
    candidate_count = len(records)
    records = _merge_frame_records(records)
    agent_visual_bundle = build_agent_visual_bundle(root, creator_key, records, ffmpeg, evidence_dir)

    frames = []
    for rec in records:
        frames.append({
            "timestamp_s": rec["timestamp_s"],
            "reason": rec["reason"],
            "file": str(rec["file"].relative_to(root)),
            "size_bytes": rec["size_bytes"],
        })

    summary = {
        "capture_strategy": "1FPS_PLUS_SCENE_CHANGE_WITH_FFMPEG_MPDECIMATE",
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
            "ffmpeg_returncode": ff.returncode,
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
        ff.returncode != 0
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
    ok = yt_rc == 0 and bool(frames) and (ff.returncode == 0 or benign_empty_scene)
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
            "visual_capture": "1FPS_PLUS_SCENE_CHANGE_WITH_LOCAL_DEDUPE",
            "full_video_persisted": False,
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

    heartbeat(
        status_path,
        "SELECTION",
        requested_sample_size=sample_size,
        discovered_count=len(entries),
        eligible_count=len(entries),
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

    selected_entries = entries[:sample_size]
    candidates = [entry for entry in selected_entries if not is_complete_entry(entry)]
    existing_delivery_targets = [
        f"yt_{entry['id']}"
        for entry in selected_entries
        if is_complete_entry(entry)
    ]
    heartbeat(
        status_path,
        "SELECTION",
        requested_sample_size=sample_size,
        discovered_count=len(entries),
        eligible_count=len(entries),
        selected_count=len(selected_entries),
        duplicate_count=duplicate_count,
    )

    model_holder = {"model": None}
    completed: list[str] = []
    failures: list[dict] = []
    caption_count = 0
    whisper_count = 0
    visual_frame_count = 0

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
        visual = capture_visual_evidence(root, creator_key, url, vid)
        if not visual.get("ok"):
            failures.append({"video_id": vid, "url": url, "stage": "visual_capture", "detail": visual.get("diagnostic_tail") or visual.get("error")})
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

    if not candidates:
        heartbeat(
            status_path,
            "QUEUE_WRITE",
            requested_sample_size=sample_size,
            discovered_count=len(entries),
            eligible_count=len(entries),
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
            "eligible_count": len(entries),
            "selected_count": len(selected_entries),
            "candidate_count": 0,
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
        terminal_state = "COMPLETE" if delivery_ok else "FAILED"
        status = terminalize(
            status_path,
            terminal_state,
            terminal_reason=status.get("shortfall_reason"),
            **{key: value for key, value in status.items() if key not in {"state", "progress"}},
        )
        atomic_json(immutable_status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if delivery_ok else 1

    heartbeat(
        status_path,
        "QUEUE_WRITE",
        requested_sample_size=sample_size,
        discovered_count=len(entries),
        eligible_count=len(entries),
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
        len(completed) == len(candidates)
        and not failures
        and queue_result.get("ok")
        and not delivery["undelivered"]
    )
    state = "COMPLETE" if complete else ("PARTIAL" if completed_count else "FAILED")
    heartbeat(
        status_path,
        "FINALIZING",
        requested_sample_size=sample_size,
        discovered_count=len(entries),
        eligible_count=len(entries),
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
        "eligible_count": len(entries),
        "selected_count": len(selected_entries),
        "candidate_count": len(candidates),
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
