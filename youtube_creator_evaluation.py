from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

YOUTUBE_EVAL_VERSION = "0.6.2"


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
        try:
            return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
        except Exception:
            pass
    upload_date = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", upload_date):
        try:
            return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
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
        # codeql[py/command-line-injection]
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


def enumerate_channel(channel_url: str, *, limit: int) -> tuple[list[dict], dict]:
    channel_url = canonical_youtube_channel_url(channel_url)
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",
        "--flat-playlist",
        "--playlist-end", str(max(limit, 1)),
        "--dump-json",
        "--",
        channel_url,
    ]
    try:
        # URL is strict-canonical YouTube and '--' terminates yt-dlp option parsing.
        # codeql[py/command-line-injection]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=False)
    except subprocess.TimeoutExpired as exc:
        return [], {"ok": False, "returncode": 124, "diagnostic_tail": str(exc)[-2000:]}

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
        })

    diag = (p.stderr or "").strip()
    return entries, {
        "ok": p.returncode == 0 and bool(entries),
        "returncode": p.returncode,
        "entries_found": len(entries),
        "diagnostic_tail": diag[-2500:],
    }


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
    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).exists():
            return bundled
    except Exception:
        pass
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
        pass
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


def transcribe_whisper(root: Path, creator_key: str, video_id: str, media_path: Path, *, model_holder: dict) -> dict:
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
        for seg in segments:
            text = (seg.text or "").strip()
            if text:
                text_parts.append(text)
            rows.append({"start": round(float(seg.start), 3), "end": round(float(seg.end), 3), "text": text})
    finally:
        if audio_diag.get("used") and whisper_input.exists():
            try:
                whisper_input.unlink()
            except OSError:
                pass

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
                    try:
                        kept[-1]["file"].unlink()
                    except OSError:
                        pass
                    kept[-1] = rec
                else:
                    try:
                        rec["file"].unlink()
                    except OSError:
                        pass
                continue
        kept.append(rec)
    return kept


def capture_visual_evidence(root: Path, creator_key: str, url: str, video_id: str) -> dict:
    evidence_dir = root / "output" / creator_key / "youtube" / "frames" / video_id
    index_path = evidence_dir / "visual_index.json"
    if index_path.exists():
        try:
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            retained = existing.get("frames", [])
            if retained and all((root / Path(x["file"])).exists() for x in retained if x.get("file")):
                return {"ok": True, "source": "existing_visual_evidence", "index": index_path, **existing.get("summary", {})}
        except Exception:
            pass

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
        try:
            yt_err_path.unlink()
        except OSError:
            pass

    ff_stderr = (ff.stderr or b"").decode("utf-8", errors="replace")
    fps_times = _showinfo_times(ff_stderr, "fps")
    scene_times = _showinfo_times(ff_stderr, "scene")
    records = _frame_records(evidence_dir, "fps", fps_times, "ONE_FPS")
    records += _frame_records(evidence_dir, "scene", scene_times, "SCENE_CHANGE")
    candidate_count = len(records)
    records = _merge_frame_records(records)

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
        except Exception:
            pass
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
    }

    manifest_path = root / "state" / "manifest.json"
    manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
    manifest.setdefault("items", {})

    try:
        exact_ids = parse_exact_video_ids(args.only_video_ids)
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
        entries, discovery_diag = enumerate_channel(args.channel_url, limit=max(sample_size * 4, 50))
        if not entries:
            status = {**base_status, "state": "NO_YOUTUBE_ENTRIES", "finished_at": utc_now(), "discovery": discovery_diag}
            atomic_json(status_path, status)
            atomic_json(immutable_status_path, status)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 4

    known = manifest["items"]
    def is_complete_entry(entry: dict) -> bool:
        key = f"yt_{entry['id']}"
        return (
            key in known
            and known[key].get("download_status") == "DONE"
            and known[key].get("transcription_status") == "DONE"
            and known[key].get("visual_evidence_status") == "DONE"
        )

    candidates = [e for e in entries if not is_complete_entry(e)][:sample_size]
    existing_delivery_targets = [
        f"yt_{entry['id']}"
        for entry in entries[:sample_size]
        if is_complete_entry(entry)
    ]

    model_holder = {"model": None}
    completed: list[str] = []
    failures: list[dict] = []
    caption_count = 0
    whisper_count = 0
    visual_frame_count = 0

    for entry in candidates:
        vid = entry["id"]
        url = entry["url"]
        key = f"yt_{vid}"
        old = manifest["items"].get(key, {})

        visual = capture_visual_evidence(root, creator_key, url, vid)
        if not visual.get("ok"):
            failures.append({"video_id": vid, "url": url, "stage": "visual_capture", "detail": visual.get("diagnostic_tail") or visual.get("error")})
            continue

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
                    tr = transcribe_whisper(root, creator_key, vid, Path(dl["media_file"]), model_holder=model_holder)
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
            "candidate_count": 0,
            "completed_count": 0,
            "completed": [],
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
        atomic_json(status_path, status)
        atomic_json(immutable_status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if delivery_ok else 1

    queue_result, queue, delivery = reconcile_delivery(root, completed)
    queue_items = queue.get("items", []) if isinstance(queue, dict) else []
    queued_for_run = [
        item.get("shortcode") for item in queue_items
        if isinstance(item, dict) and item.get("evaluation_run_id") == run_id
    ]

    complete = (
        len(completed) == len(candidates)
        and not failures
        and queue_result.get("ok")
        and not delivery["undelivered"]
    )
    state = "COMPLETE" if complete else ("PARTIAL" if completed else "FAILED")
    status = {
        **base_status,
        "state": state,
        "finished_at": utc_now(),
        "discovery": discovery_diag,
        "entries_discovered": len(entries),
        "candidate_count": len(candidates),
        "completed_count": len(completed),
        "completed": completed,
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
    atomic_json(status_path, status)
    atomic_json(immutable_status_path, status)
    print(json.dumps(status, ensure_ascii=False, indent=2))

    if state == "COMPLETE":
        return 0
    if completed:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
