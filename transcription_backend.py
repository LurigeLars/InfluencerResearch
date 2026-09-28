from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

GEMINI_SECRET_PATH = Path("/run/influencerresearch-secrets/gemini_api_key")
DEFAULT_GEMINI_MODEL = "gemini-3.5-transcribe"
DEFAULT_GEMINI_VISUAL_MODEL = "gemini-3.8-flash"
GEMINI_HTTP_TIMEOUT_MS = 45_000
GEMINI_RETRY_ATTEMPTS = 2
ALLOWED_PROVIDERS = {"auto", "gemini", "faster-whisper"}
_WHISPER_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}


def gemini_http_options() -> dict[str, Any]:
    """Bound all Gemini HTTP operations, including file upload and model calls."""
    return {
        "timeout": GEMINI_HTTP_TIMEOUT_MS,
        "retry_options": {"attempts": GEMINI_RETRY_ATTEMPTS},
    }


def _gemini_client(api_key: str):
    from google import genai

    return genai.Client(
        api_key=api_key,
        http_options=gemini_http_options(),
    )


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

    audio_path = _extract_audio(video_path)
    client = _gemini_client(api_key)
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



def extract_image_evidence_gemini(
    image_path: Path,
    *,
    model: str = DEFAULT_GEMINI_VISUAL_MODEL,
    secret_path: Path | None = None,
) -> dict[str, Any]:
    """Convert a Story screenshot into compact factual text evidence.

    The downstream MCP consumer cannot read a container-local screenshot path, so
    this produces a textual representation of the visible Story content. It must
    stay descriptive: preserve visible text/numbers and describe charts/graphics,
    but do not infer an investment conclusion that is not directly shown.
    """
    api_key = read_gemini_api_key(secret_path)
    if not api_key:
        raise RuntimeError("Gemini runtime secret is not available.")

    client = _gemini_client(api_key)
    uploaded = None
    try:
        uploaded = client.files.upload(file=str(image_path))
        prompt = (
            "Convert this Instagram Story screenshot into compact factual evidence "
            "for downstream financial research. Ignore Instagram viewer chrome such "
            "as username, age, progress bar, reply/share controls. Preserve all "
            "meaningful visible text, tickers, prices, percentages, dates, labels, "
            "chart axes and source names. Then describe any meaningful non-text "
            "visual evidence such as a chart direction, highlighted region, table, "
            "headline screenshot, or asset shown. Do not infer the creator's intent, "
            "do not recommend a trade, and do not add facts that are not visibly "
            "present. Return plain text only. If the Story contains no meaningful "
            "content beyond viewer chrome, output exactly NO_MEANINGFUL_VISUAL_EVIDENCE."
        )
        interaction = client.interactions.create(
            model=model,
            input=[
                {"type": "text", "text": prompt},
                {
                    "type": "image",
                    "uri": uploaded.uri,
                    "mime_type": uploaded.mime_type,
                },
            ],
        )
        text = (interaction.output_text or "").strip()
        if text == "NO_MEANINGFUL_VISUAL_EVIDENCE":
            text = ""
        return {
            "provider": "gemini",
            "model": model,
            "source": "GEMINI_STORY_SCREENSHOT_EVIDENCE",
            "text": text,
        }
    finally:
        if uploaded is not None:
            with contextlib.suppress(Exception):
                client.files.delete(name=uploaded.name)


def extract_visible_text_gemini(
    video_path: Path,
    *,
    model: str = DEFAULT_GEMINI_VISUAL_MODEL,
    secret_path: Path | None = None,
    processing: str = "static",
    processing_timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Extract only text that is visibly present in a video.

    This is a fallback for burned-in subtitles, charts, slide text, tickers and other
    visual text that audio transcription cannot recover. The uploaded file is deleted
    from Gemini storage after the request.
    """
    api_key = read_gemini_api_key(secret_path)
    if not api_key:
        raise RuntimeError("Gemini runtime secret is not available.")
    if processing not in {"static", "agentic"}:
        raise ValueError("processing must be static or agentic")

    client = _gemini_client(api_key)
    uploaded = None
    try:
        uploaded = client.files.upload(file=str(video_path))
        deadline = time.monotonic() + max(10, int(processing_timeout_seconds))
        while True:
            state = str(getattr(getattr(uploaded, "state", None), "name", "") or "").upper()
            if state == "ACTIVE":
                break
            if state == "FAILED":
                raise RuntimeError("Gemini video processing failed.")
            if time.monotonic() >= deadline:
                raise TimeoutError("Gemini video processing timed out.")
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)

        prompt = (
            "Transcribe only text that is actually visible in this video, including "
            "burned-in subtitles, captions, labels, chart annotations, prices, tickers, "
            "percentages and slide text. Preserve important numbers and symbols. "
            "Deduplicate text that remains unchanged across adjacent frames and keep "
            "the result in chronological reading order. Do not infer spoken words that "
            "are not visibly written and do not summarize. If there is no meaningful "
            "visible text, output exactly NO_VISIBLE_TEXT."
        )
        interaction = client.interactions.create(
            model=model,
            input=[
                {
                    "type": "video",
                    "uri": uploaded.uri,
                    "mime_type": uploaded.mime_type,
                    "processing": processing,
                },
                {"type": "text", "text": prompt},
            ],
        )
        text = (interaction.output_text or "").strip()
        if not text or text == "NO_VISIBLE_TEXT":
            text = ""
        return {
            "provider": "gemini",
            "model": model,
            "source": "GEMINI_VIDEO_VISIBLE_TEXT",
            "processing": processing,
            "text": text,
        }
    finally:
        if uploaded is not None:
            with contextlib.suppress(Exception):
                client.files.delete(name=uploaded.name)


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
