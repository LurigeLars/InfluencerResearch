from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VISUAL_REVIEW_POLICY_VERSION = 1
VISUAL_OCR_MAX_FRAMES = 24
VISUAL_REPRESENTATIVE_FRAMES = 12
VISUAL_OCR_TIMEOUT_SECONDS = 8
CHART_HEAVY_CREATORS = {"thetradingfraternity"}
CHART_TERMS = {
    "support", "resistance", "breakout", "trend", "vwap", "volume", "price",
    "yield", "spread", "gamma", "delta", "rsi", "macd", "moving average",
    "s&p", "spx", "nasdaq", "qqq", "dow", "dxy", "vix", "btc", "eth",
    "treasury", "crude", "oil", "gold", "copper", "eur", "usd", "jpy",
    "ratio", "index", "futures", "calls", "puts", "strike", "open interest",
}
TRANSCRIPT_VISUAL_CUES = {
    "on my screen", "shown on my screen", "showed you on my screen",
    "this chart", "this graph", "this table", "this ratio", "this index",
    "you can see", "as you can see", "look at this", "shown here",
    "on the chart", "on this chart", "on the screen",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


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


def _showinfo_times(stderr: str, instance: str) -> list[float]:
    times: list[float] = []
    marker = f"showinfo@{instance}"
    for line in str(stderr or "").splitlines():
        if marker not in line:
            continue
        match = re.search(r"pts_time:([0-9]+(?:\.[0-9]+)?)", line)
        if match:
            times.append(float(match.group(1)))
    return times


def _frame_records(frame_dir: Path, prefix: str, times: list[float], reason: str) -> list[dict]:
    files = sorted(frame_dir.glob(f"{prefix}_*.jpg"))
    rows: list[dict] = []
    for idx, path in enumerate(files):
        timestamp = times[idx] if idx < len(times) else None
        rows.append({
            "timestamp_s": round(timestamp, 3) if timestamp is not None else None,
            "reason": reason,
            "file": path,
            "size_bytes": path.stat().st_size,
        })
    return rows


def _merge_frame_records(records: list[dict]) -> list[dict]:
    records = sorted(
        records,
        key=lambda row: (
            float(row["timestamp_s"]) if row.get("timestamp_s") is not None else 1e18,
            str(row.get("reason") or ""),
        ),
    )
    kept: list[dict] = []
    for row in records:
        timestamp = row.get("timestamp_s")
        if timestamp is not None and kept and kept[-1].get("timestamp_s") is not None:
            if abs(float(timestamp) - float(kept[-1]["timestamp_s"])) <= 0.35:
                if row.get("reason") == "SCENE_CHANGE" and kept[-1].get("reason") != "SCENE_CHANGE":
                    with contextlib.suppress(OSError):
                        Path(kept[-1]["file"]).unlink()
                    kept[-1] = row
                else:
                    with contextlib.suppress(OSError):
                        Path(row["file"]).unlink()
                continue
        kept.append(row)
    return kept


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


def _ocr_visual_frame(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "eng", "--psm", "11"],
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


def score_visual_frame_text(text: str) -> tuple[float, list[str]]:
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


def classify_visual_review(
    creator_key: str,
    inspected: list[dict],
    *,
    transcript_text: str = "",
) -> dict:
    strong = [row for row in inspected if float(row.get("visual_score") or 0.0) >= 2.0]
    chart_ratio = (len(strong) / len(inspected)) if inspected else 0.0
    creator_prior = "HIGH" if creator_key.casefold() in CHART_HEAVY_CREATORS else "NEUTRAL"
    content_signal = len(strong) >= 2 or chart_ratio >= 0.20
    transcript_lower = str(transcript_text or "").casefold()
    transcript_visual_cue = any(cue in transcript_lower for cue in TRANSCRIPT_VISUAL_CUES)
    recommended = bool(inspected) and (creator_prior == "HIGH" or content_signal or transcript_visual_cue)
    reasons: list[str] = []
    if creator_prior == "HIGH":
        reasons.append("CREATOR_CHART_PRIOR")
    if content_signal:
        reasons.append("PER_VIDEO_VISUAL_SIGNAL")
    if transcript_visual_cue:
        reasons.append("TRANSCRIPT_VISUAL_CUE")
    return {
        "analysis_mode_recommended": "TRANSCRIPT_PLUS_VISUAL_REVIEW" if recommended else "TRANSCRIPT_ONLY",
        "visual_review_recommended": recommended,
        "visual_review_reason": reasons,
        "creator_visual_prior": creator_prior,
        "sampled_frame_count": len(inspected),
        "chart_signal_frame_count": len(strong),
        "chart_signal_ratio": round(chart_ratio, 3),
        "transcript_visual_cue": transcript_visual_cue,
    }


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
    root: Path,
    creator_key: str,
    records: list[dict],
    ffmpeg: str,
    evidence_dir: Path,
    *,
    media_path: Path | None = None,
    transcript_text: str = "",
) -> dict:
    sampled = _sample_visual_records(records)

    def inspect(row: dict) -> dict:
        text = _ocr_visual_frame(Path(row["file"]))
        score, reasons = score_visual_frame_text(text)
        return {**row, "ocr_text": text[:1200], "visual_score": score, "visual_signals": reasons}

    if sampled:
        with ThreadPoolExecutor(max_workers=min(4, len(sampled)), thread_name_prefix="visual-ocr") as executor:
            inspected = list(executor.map(inspect, sampled))
    else:
        inspected = []

    policy = classify_visual_review(creator_key, inspected, transcript_text=transcript_text)
    ranked = sorted(
        inspected,
        key=lambda row: (
            float(row.get("visual_score") or 0.0),
            row.get("reason") == "SCENE_CHANGE",
        ),
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

    raw_video = None
    if media_path is not None and media_path.exists():
        with contextlib.suppress(ValueError):
            raw_video = str(media_path.relative_to(root))

    return {
        "available": bool(representative),
        "visual_review_policy_version": VISUAL_REVIEW_POLICY_VERSION,
        **policy,
        "contact_sheet": str(contact_sheet.relative_to(root)) if contact_sheet else None,
        "representative_frames": [public_row(row) for row in representative],
        "raw_video_available": bool(raw_video),
        "raw_video_file": raw_video,
        "fallback_order": ["CONTACT_SHEET", "REPRESENTATIVE_FRAMES", "RAW_VIDEO_LOCAL"],
    }


def capture_local_video_visual_evidence(
    root: Path,
    creator_key: str,
    source_platform: str,
    video_id: str,
    media_path: Path,
    *,
    transcript_text: str = "",
) -> dict:
    """Capture bounded local visual evidence for one retained video.

    This path is deliberately provider-free. Frame extraction and OCR failures fail open
    into transcript-only analysis; the source video remains available as the last local
    fallback for a downstream agent or future transport.
    """
    platform_dir = source_platform.strip().lower()
    evidence_dir = root / "output" / creator_key / platform_dir / "frames" / video_id
    index_path = evidence_dir / "visual_index.json"

    if index_path.exists():
        with contextlib.suppress(OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            if int(existing.get("visual_review_policy_version") or 0) >= VISUAL_REVIEW_POLICY_VERSION:
                retained = existing.get("frames") or []
                if retained and all((root / Path(row["file"])).exists() for row in retained if row.get("file")):
                    summary = dict(existing.get("summary") or {})
                    return {
                        "ok": True,
                        "source": "existing_visual_evidence",
                        "index": index_path,
                        **summary,
                    }

    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return {
            "ok": False,
            "error": "ffmpeg_missing_system_or_bundled",
            "visual_review_policy_version": VISUAL_REVIEW_POLICY_VERSION,
        }
    if not media_path.exists():
        return {
            "ok": False,
            "error": "media_file_missing",
            "visual_review_policy_version": VISUAL_REVIEW_POLICY_VERSION,
        }

    shutil.rmtree(evidence_dir, ignore_errors=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    filter_complex = (
        "[0:v]split=2[fpssrc][scsrc];"
        "[fpssrc]fps=1,mpdecimate,showinfo@fps[fpsout];"
        "[scsrc]select='gt(scene\\,0.30)',showinfo@scene[scout]"
    )
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "info", "-y",
        "-i", str(media_path),
        "-filter_complex", filter_complex,
        "-map", "[fpsout]", "-fps_mode", "vfr", "-q:v", "5", str(evidence_dir / "fps_%05d.jpg"),
        "-map", "[scout]", "-fps_mode", "vfr", "-q:v", "5", str(evidence_dir / "scene_%05d.jpg"),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}:visual_frame_capture_failed",
            "visual_review_policy_version": VISUAL_REVIEW_POLICY_VERSION,
        }

    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    fps_times = _showinfo_times(stderr, "fps")
    scene_times = _showinfo_times(stderr, "scene")
    records = _frame_records(evidence_dir, "fps", fps_times, "ONE_FPS")
    records += _frame_records(evidence_dir, "scene", scene_times, "SCENE_CHANGE")
    candidate_count = len(records)
    records = _merge_frame_records(records)
    if proc.returncode != 0 and not records:
        return {
            "ok": False,
            "error": f"ffmpeg_visual_frame_capture_failed:{proc.returncode}",
            "visual_review_policy_version": VISUAL_REVIEW_POLICY_VERSION,
        }

    bundle = build_agent_visual_bundle(
        root,
        creator_key,
        records,
        ffmpeg,
        evidence_dir,
        media_path=media_path,
        transcript_text=transcript_text,
    )
    frames = [
        {
            "timestamp_s": row.get("timestamp_s"),
            "reason": row.get("reason"),
            "file": str(Path(row["file"]).relative_to(root)),
            "size_bytes": row.get("size_bytes"),
        }
        for row in records
    ]
    summary = {
        "capture_strategy": "1FPS_PLUS_SCENE_CHANGE_WITH_FFMPEG_MPDECIMATE_LOCAL_MEDIA",
        "candidate_frames": candidate_count,
        "retained_frames": len(frames),
        "scene_change_frames": sum(1 for row in frames if row.get("reason") == "SCENE_CHANGE"),
        "one_fps_frames": sum(1 for row in frames if row.get("reason") == "ONE_FPS"),
        "video_persisted": True,
        "agent_visual_bundle": bundle,
    }
    index = {
        "schema_version": 1,
        "visual_review_policy_version": VISUAL_REVIEW_POLICY_VERSION,
        "source_platform": source_platform.upper(),
        "video_id": video_id,
        "media_file": str(media_path.relative_to(root)),
        "generated_at": utc_now(),
        "summary": summary,
        "frames": frames,
        "agent_visual_bundle": bundle,
        "diagnostics": {"ffmpeg_returncode": proc.returncode},
    }
    atomic_json(index_path, index)
    return {
        "ok": True,
        "source": "local_video_visual_evidence",
        "index": index_path,
        **summary,
    }