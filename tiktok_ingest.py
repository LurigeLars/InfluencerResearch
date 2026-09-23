from __future__ import annotations

import argparse
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


APP_VERSION = "0.5.0"
SOURCE_TYPE = "TIKTOK"
DEFAULT_PROFILE = "https://www.tiktok.com/@nicholas_crown"
TIKTOK_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def canonical_tiktok_profile_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or host not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
        raise ValueError("Invalid TikTok profile URL")
    if len(parts) != 1 or not parts[0].startswith("@"):
        raise ValueError("Invalid TikTok profile path")
    handle = parts[0][1:]
    if not TIKTOK_HANDLE_RE.fullmatch(handle):
        raise ValueError("Invalid TikTok creator handle")
    return f"https://www.tiktok.com/@{handle}"


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


def validate_media(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "file_missing"
    size = path.stat().st_size
    if size < 50_000:
        return False, f"file_too_small:{size}"
    try:
        import av
        with av.open(str(path)) as container:
            streams = [s for s in container.streams if s.type == "video"]
            if not streams:
                return False, "no_video_stream"
            return True, f"ok:size={size}:duration={container.duration}"
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc}"


def transcribe_pending(root: Path, creator: str, video_dir: Path) -> dict:
    transcript_dir = root / "output" / creator / "tiktok" / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)

    videos = sorted(video_dir.glob("*.mp4"))
    pending = [
        p for p in videos
        if not (transcript_dir / f"{p.stem}.txt").exists()
    ]

    if not pending:
        return {"attempted": 0, "completed": 0, "errors": []}

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        return {
            "attempted": len(pending),
            "completed": 0,
            "errors": [f"faster-whisper import failed: {exc}"],
        }

    model = WhisperModel("small", device="cpu", compute_type="int8")
    completed = 0
    errors: list[str] = []

    for video_path in pending:
        valid, validation = validate_media(video_path)
        if not valid:
            errors.append(f"{video_path.name}: invalid media: {validation}")
            continue

        try:
            segments, info = model.transcribe(
                str(video_path),
                beam_size=5,
                vad_filter=True,
            )

            rows = []
            parts = []
            for seg in segments:
                text = (seg.text or "").strip()
                if text:
                    parts.append(text)
                rows.append({
                    "start": round(float(seg.start), 3),
                    "end": round(float(seg.end), 3),
                    "text": text,
                })

            txt_path = transcript_dir / f"{video_path.stem}.txt"
            json_path = transcript_dir / f"{video_path.stem}.json"

            txt_path.write_text(
                " ".join(parts).strip() + "\n",
                encoding="utf-8",
            )
            atomic_write_json(json_path, {
                "schema_version": 1,
                "source_type": SOURCE_TYPE,
                "creator": creator,
                "video_id": video_path.stem,
                "video_file": str(video_path.relative_to(root)),
                "language": getattr(info, "language", None),
                "language_probability": getattr(info, "language_probability", None),
                "duration": getattr(info, "duration", None),
                "generated_at": utc_now(),
                "segments": rows,
            })
            completed += 1
        except Exception as exc:
            errors.append(
                f"{video_path.name}: {type(exc).__name__}: {exc}"
            )

    return {
        "attempted": len(pending),
        "completed": completed,
        "errors": errors,
    }


def info_records(video_dir: Path) -> list[dict]:
    rows = []
    for info_path in sorted(video_dir.glob("*.info.json")):
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        vid = str(raw.get("id") or info_path.stem.replace(".info", ""))
        media = None
        for candidate in video_dir.glob(f"{vid}.*"):
            if candidate.suffix.lower() == ".mp4":
                media = candidate
                break

        valid = False
        validation = "media_missing"
        if media:
            valid, validation = validate_media(media)

        rows.append({
            "id": vid,
            "title": raw.get("title"),
            "description": raw.get("description"),
            "webpage_url": raw.get("webpage_url"),
            "timestamp": raw.get("timestamp"),
            "upload_date": raw.get("upload_date"),
            "duration": raw.get("duration"),
            "view_count": raw.get("view_count"),
            "like_count": raw.get("like_count"),
            "comment_count": raw.get("comment_count"),
            "repost_count": raw.get("repost_count"),
            "uploader": raw.get("uploader"),
            "uploader_id": raw.get("uploader_id"),
            "media_file": str(media) if media else None,
            "media_valid": valid,
            "media_validation": validation,
        })
    return rows


def run_ytdlp(
    profile_url: str,
    creator: str,
    root: Path,
    max_videos: int,
) -> dict:
    profile_url = canonical_tiktok_profile_url(profile_url)
    video_dir = root / "output" / creator / "tiktok" / "videos"
    state_dir = root / "state" / "tiktok"
    video_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    archive = state_dir / f"{creator}_archive.txt"
    before = {
        p.name for p in video_dir.glob("*.mp4")
    }

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-progress",
        "--no-warnings",
        "--ignore-config",
        "--playlist-end", str(max_videos),
        "--download-archive", str(archive),
        "--write-info-json",
        "--no-overwrites",
        "--format", "b[ext=mp4]/b",
        "--output", str(video_dir / "%(id)s.%(ext)s"),
        "--",
        profile_url,
    ]

    # URL is strict-canonical TikTok and '--' terminates yt-dlp option parsing.
    # codeql[py/command-line-injection]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
    )

    after = {
        p.name for p in video_dir.glob("*.mp4")
    }
    new_files = sorted(after - before)

    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    detail = stderr if stderr else stdout

    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "profile_url": profile_url,
        "max_videos": max_videos,
        "new_files": new_files,
        "new_count": len(new_files),
        "archive_file": str(archive.relative_to(root)),
        "diagnostic_tail": detail[-4000:] if detail else "",
        "video_dir": video_dir,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--profile-url", default=DEFAULT_PROFILE)
    parser.add_argument("--creator", default="nicholas_crown")
    parser.add_argument("--max-videos", type=int, default=10)
    parser.add_argument("--skip-transcription", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    started = utc_now()
    status_path = root / "state" / "tiktok" / "status.json"
    manifest_path = root / "state" / "tiktok" / "manifest.json"

    atomic_write_json(status_path, {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "state": "RUNNING",
        "source_type": SOURCE_TYPE,
        "creator": args.creator,
        "profile_url": args.profile_url,
        "started_at": started,
    })

    try:
        dl = run_ytdlp(
            profile_url=args.profile_url,
            creator=args.creator,
            root=root,
            max_videos=args.max_videos,
        )

        records = info_records(dl["video_dir"])

        if args.skip_transcription:
            transcription = {
                "attempted": 0,
                "completed": 0,
                "errors": [],
                "skipped": True,
            }
        else:
            transcription = transcribe_pending(
                root,
                args.creator,
                dl["video_dir"],
            )

        valid_records = [r for r in records if r["media_valid"]]
        invalid_records = [r for r in records if not r["media_valid"]]

        manifest = {
            "schema_version": 1,
            "app_version": APP_VERSION,
            "source_type": SOURCE_TYPE,
            "creator": args.creator,
            "profile_url": args.profile_url,
            "generated_at": utc_now(),
            "items": records,
        }
        atomic_write_json(manifest_path, manifest)

        errors = []
        if not dl["ok"]:
            errors.append(
                f"yt-dlp return code {dl['returncode']}: "
                f"{dl['diagnostic_tail']}"
            )
        if invalid_records:
            errors.append(
                f"{len(invalid_records)} downloaded/metadata items failed media validation"
            )
        errors.extend(transcription.get("errors", []))

        status = {
            "schema_version": 1,
            "app_version": APP_VERSION,
            "state": "DONE" if not errors else "DONE_WITH_ERRORS",
            "source_type": SOURCE_TYPE,
            "creator": args.creator,
            "profile_url": args.profile_url,
            "started_at": started,
            "finished_at": utc_now(),
            "download": {
                "ok": dl["ok"],
                "returncode": dl["returncode"],
                "new_count": dl["new_count"],
                "new_files": dl["new_files"],
                "archive_file": dl["archive_file"],
                "diagnostic_tail": dl["diagnostic_tail"],
            },
            "media": {
                "metadata_items": len(records),
                "valid_items": len(valid_records),
                "invalid_items": len(invalid_records),
            },
            "transcription": transcription,
            "total_errors": len(errors),
            "errors": errors,
        }
        atomic_write_json(status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if not errors else 1

    except Exception as exc:
        status = {
            "schema_version": 1,
            "app_version": APP_VERSION,
            "state": "ERROR",
            "source_type": SOURCE_TYPE,
            "creator": args.creator,
            "profile_url": args.profile_url,
            "started_at": started,
            "finished_at": utc_now(),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
        atomic_write_json(status_path, status)
        print(status["traceback"], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
