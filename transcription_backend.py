from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

GEMINI_SECRET_PATH = Path("/run/influencerresearch-secrets/gemini_api_key")
DEFAULT_GEMINI_MODEL = "gemini-3.5-transcribe"
ALLOWED_PROVIDERS = {"auto", "gemini", "faster-whisper"}
_WHISPER_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}


def read_gemini_api_key(path: Path | None = None) -> str | None:
    """Read the runtime-only Gemini key file. Environment variables are intentionally ignored."""
    secret_path = path or GEMINI_SECRET_PATH
    if not secret_path.is_file():
        return None
    value = secret_path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("Gemini runtime secret file is empty.")
    return value


def _safe_error(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    suffix = f" status={code}" if code is not None else ""
    return f"{type(exc).__name__}: Gemini transcription failed{suffix}"


def _extract_audio(video_path: Path) -> Path:
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    fd, tmp_name = tempfile.mkstemp(prefix=f"{video_path.stem}.gemini.", suffix=".mp3")
    os.close(fd)
    audio_path = Path(tmp_name)

    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            str(audio_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        with contextlib.suppress(OSError):
            audio_path.unlink(missing_ok=True)
        detail = (proc.stderr or "").strip()
        if len(detail) > 1000:
            detail = detail[-1000:]
        raise RuntimeError(f"ffmpeg audio extraction failed code {proc.returncode}: {detail}")
    return audio_path


def transcribe_gemini(
    video_path: Path,
    *,
    model: str = DEFAULT_GEMINI_MODEL,
    secret_path: Path | None = None,
) -> dict[str, Any]:
    api_key = read_gemini_api_key(secret_path)
    if not api_key:
        raise RuntimeError("Gemini runtime secret is not available.")

    from google import genai

    audio_path = _extract_audio(video_path)
    client = genai.Client(api_key=api_key)
    uploaded = None
    try:
        uploaded = client.files.upload(file=str(audio_path))
        interaction = client.interactions.create(
            model=model,
            input=[
                {
                    "type": "audio",
                    "uri": uploaded.uri,
                    "mime_type": uploaded.mime_type,
                }
            ],
        )
        text = (interaction.output_text or "").strip()
        if not text:
            raise RuntimeError("Gemini returned an empty transcript.")
        return {
            "provider": "gemini",
            "model": model,
            "text": text,
            "language": None,
            "language_probability": None,
            "duration": None,
            "segments": [],
        }
    finally:
        if uploaded is not None:
            with contextlib.suppress(Exception):
                client.files.delete(name=uploaded.name)
        with contextlib.suppress(OSError):
            audio_path.unlink(missing_ok=True)


def transcribe_faster_whisper(video_path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from faster_whisper import WhisperModel

    model_size = str(settings.get("model_size", "small"))
    device = str(settings.get("device", "cpu"))
    compute_type = str(settings.get("compute_type", "int8"))
    cache_key = (model_size, device, compute_type)
    model = _WHISPER_MODEL_CACHE.get(cache_key)
    if model is None:
        model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
        )
        _WHISPER_MODEL_CACHE[cache_key] = model
    segments, info = model.transcribe(
        str(video_path),
        beam_size=int(settings.get("beam_size", 5)),
        vad_filter=bool(settings.get("vad_filter", True)),
    )

    rows = []
    parts = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            parts.append(text)
        rows.append(
            {
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": text,
            }
        )

    return {
        "provider": "faster-whisper",
        "model": model_size,
        "text": " ".join(parts).strip(),
        "language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "duration": getattr(info, "duration", None),
        "segments": rows,
    }


def transcribe_video(video_path: Path, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or {}
    provider = str(settings.get("provider", "auto")).strip().lower()
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError(f"Unsupported transcription provider: {provider}")

    gemini_model = str(settings.get("gemini_model", DEFAULT_GEMINI_MODEL)).strip()
    if not gemini_model:
        raise ValueError("gemini_model must not be empty")

    if provider == "gemini":
        return transcribe_gemini(video_path, model=gemini_model)

    if provider == "faster-whisper":
        return transcribe_faster_whisper(video_path, settings)

    # auto: prefer Gemini only when the runtime-only secret is present, then fall back locally.
    if read_gemini_api_key() is not None:
        try:
            return transcribe_gemini(video_path, model=gemini_model)
        except Exception as exc:
            fallback = transcribe_faster_whisper(video_path, settings)
            fallback["fallback_from"] = "gemini"
            fallback["fallback_error"] = _safe_error(exc)
            return fallback

    return transcribe_faster_whisper(video_path, settings)
