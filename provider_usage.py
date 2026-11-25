"""Bounded, content-free usage events; no network or separate service."""
from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import math
import os
from pathlib import Path
import subprocess
import stat
import time
from datetime import datetime, timezone

LOG_DIR = Path("/research/logs")
DAILY_LIMIT = 4 * 1024 * 1024
_FALLBACK = contextvars.ContextVar("usage_fallback", default=False)
_PROVIDERS = {"gemini", "ollama", "faster-whisper", "tesseract"}
_OPERATIONS = {"transcription", "story_image", "video_text", "video_frame_ocr"}
_OUTCOMES = {"attempt", "completed", "empty", "failed", "timeout", "rate_limited"}


@contextlib.contextmanager
def fallback_usage(active=True):
    token = _FALLBACK.set(bool(active) or _FALLBACK.get())
    try:
        yield
    finally:
        _FALLBACK.reset(token)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not 0 <= value <= 86400 * 7:
        return None
    return round(value, 3)


def _emit(provider, operation, outcome, *, elapsed=None, media_seconds=None):
    if provider not in _PROVIDERS or operation not in _OPERATIONS or outcome not in _OUTCOMES:
        raise ValueError("Invalid usage category")
    # Worker stdout can be discarded. Append only fixed-schema data to the
    # existing operator log mount, never to a request-derived path.
    now = datetime.now(timezone.utc)
    event = {"provider": provider, "operation": operation, "outcome": outcome,
             "fallback": _FALLBACK.get(), "elapsed_seconds": _number(elapsed),
             "media_seconds": _number(media_seconds)}
    line = (json.dumps(event, separators=(",", ":")) + "\n").encode("ascii")
    fd = None
    try:
        path = LOG_DIR / f"provider-usage-{now:%Y-%m-%d}.jsonl"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_size + len(line) <= DAILY_LIMIT:
            os.write(fd, line)
    except OSError:
        # Accounting must not change the result of a research operation.
        pass
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _duration(path):
    # Only for local inputs with no provider duration. No outbound protocols,
    # no filenames, ffprobe output or error text persisted in usage logs.
    try:
        if not isinstance(path, Path) or not path.is_file():
            return None
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
             "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
             str(path.resolve())], capture_output=True, text=True, timeout=5, check=True)
        return _number(float(p.stdout.strip()))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _failure(exc):
    # Inspect codes, not messages except for the existing sanitized worker
    # error wrapper, which loses the typed provider code across the subprocess.
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    cause = exc.__cause__
    if code is None and cause is not None:
        code = getattr(cause, "code", None) or getattr(cause, "status_code", None)
    if str(code) == "429" or (isinstance(exc, RuntimeError) and
        ": Gemini transcription failed code=429" in str(exc)):
        return "rate_limited"
    return "failed"


def measure(provider, operation):
    """Measure logical provider operations, not individual SDK retries."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            start = time.monotonic()
            _emit(provider, operation, "attempt")
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                _emit(provider, operation, _failure(exc), elapsed=time.monotonic() - start)
                raise
            elapsed = time.monotonic() - start
            text = result.get("text") if isinstance(result, dict) else result
            outcome = "completed" if isinstance(text, str) and text.strip() else "empty"
            duration = None
            if operation == "transcription" and outcome == "completed":
                duration = _number(result.get("duration")) if isinstance(result, dict) else None
                if duration is None:
                    duration = _duration(args[0] if args else kwargs.get("video_path"))
            _emit(provider, operation, outcome, elapsed=elapsed, media_seconds=duration)
            return result
        return wrapped
    return decorate
