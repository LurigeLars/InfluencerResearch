from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import transcription_backend as tb


class TranscriptionBackendTests(unittest.TestCase):
    def test_faster_whisper_pyav_compatibility_is_pinned(self) -> None:
        requirements = Path("requirements.txt").read_text(encoding="utf-8")
        self.assertIn("faster-whisper==1.2.1", requirements)
        self.assertIn("av==18.1.0", requirements)

    def test_faster_whisper_reports_segment_progress(self) -> None:
        class Segment:
            def __init__(self, index: int) -> None:
                self.start = float(index)
                self.end = float(index + 1)
                self.text = f"segment {index}"

        class Info:
            language = "en"
            language_probability = 1.0
            duration = 12.0

        class Model:
            def transcribe(self, *args, **kwargs):
                return ([Segment(index) for index in range(12)], Info())

        cache_key = ("small", "cpu", "int8")
        previous = tb._WHISPER_MODEL_CACHE.get(cache_key)
        tb._WHISPER_MODEL_CACHE[cache_key] = Model()
        progress: list[int] = []
        try:
            with patch.dict(
                sys.modules,
                {"faster_whisper": type("FakeWhisperModule", (), {"WhisperModel": object})()},
            ):
                result = tb.transcribe_faster_whisper(
                    Path("fixture.mp4"),
                    {},
                    progress_callback=progress.append,
                )
        finally:
            if previous is None:
                tb._WHISPER_MODEL_CACHE.pop(cache_key, None)
            else:
                tb._WHISPER_MODEL_CACHE[cache_key] = previous

        self.assertEqual(result["provider"], "faster-whisper")
        self.assertEqual(progress, [1, 10])

    def test_gemini_http_options_are_bounded(self) -> None:
        self.assertEqual(
            tb.gemini_http_options(),
            {
                "timeout": 45_000,
                "retry_options": {"attempts": 2},
            },
        )

    def test_story_gemini_http_options_are_stricter(self) -> None:
        self.assertEqual(
            tb.gemini_http_options(
                timeout_ms=tb.STORY_GEMINI_HTTP_TIMEOUT_MS,
                retry_attempts=tb.STORY_GEMINI_RETRY_ATTEMPTS,
            ),
            {
                "timeout": 12_000,
                "retry_options": {"attempts": 1},
            },
        )

    def test_story_gemini_timeout_bounds(self) -> None:
        self.assertEqual(tb.bounded_story_gemini_timeout_ms(), 12_000)
        self.assertEqual(tb.bounded_story_gemini_timeout_ms(7_500), 7_500)
        self.assertEqual(tb.bounded_story_gemini_timeout_ms(1_000), 5_000)
        self.assertEqual(tb.bounded_story_gemini_timeout_ms(90_000), 30_000)

    def test_transcription_provider_limits_are_stricter_than_generic_gemini(self) -> None:
        self.assertEqual(tb.bounded_transcription_gemini_timeout_ms(), 15_000)
        self.assertEqual(tb.bounded_transcription_gemini_timeout_ms(1_000), 5_000)
        self.assertEqual(tb.bounded_transcription_gemini_timeout_ms(90_000), 30_000)
        self.assertEqual(tb.bounded_transcription_audio_timeout_seconds(), 60)
        self.assertEqual(tb.bounded_transcription_audio_timeout_seconds(1), 15)
        self.assertEqual(tb.bounded_transcription_audio_timeout_seconds(999), 120)
        self.assertEqual(tb.bounded_transcription_gemini_total_timeout_seconds(), 75)
        self.assertEqual(tb.bounded_transcription_gemini_total_timeout_seconds(1), 30)
        self.assertEqual(tb.bounded_transcription_gemini_total_timeout_seconds(999), 120)

    def test_story_image_path_uses_inline_bytes_not_files_api(self) -> None:
        source = Path(tb.__file__).read_text(encoding="utf-8")
        start = source.index("def extract_image_evidence_gemini(")
        end = source.index("\ndef extract_visible_text_gemini(", start)
        block = source[start:end]
        self.assertIn("types.Part.from_bytes", block)
        self.assertIn("client.models.generate_content", block)
        self.assertNotIn("client.files.upload", block)
        self.assertNotIn("client.files.delete", block)

    def test_safe_gemini_error_exposes_only_type_code_and_status(self) -> None:
        class FakeGeminiError(Exception):
            code = 429
            status = "RESOURCE_EXHAUSTED"

        exc = FakeGeminiError("SECRET response body with prompt text")
        result = tb.safe_gemini_error(
            exc,
            operation="visual evidence extraction",
        )
        self.assertEqual(
            result,
            "FakeGeminiError: Gemini visual evidence extraction failed "
            "code=429 status=RESOURCE_EXHAUSTED",
        )
        self.assertNotIn("SECRET", result)
        self.assertNotIn("prompt", result)

    def test_safe_gemini_error_rejects_unsafe_status_text(self) -> None:
        class FakeGeminiError(Exception):
            code = 400
            status = "BAD STATUS includes sensitive text"

        result = tb.safe_gemini_error(
            FakeGeminiError("do not leak me"),
            operation="visual evidence extraction",
        )
        self.assertEqual(
            result,
            "FakeGeminiError: Gemini visual evidence extraction failed code=400",
        )
        self.assertNotIn("sensitive", result)

    def test_retry_after_numeric_header_is_honored(self) -> None:
        class FakeResponse:
            headers = {"retry-after": "17"}

        class FakeGeminiError(Exception):
            response = FakeResponse()

        self.assertEqual(
            tb.gemini_retry_after_seconds(FakeGeminiError("hidden")),
            17,
        )

    def test_retry_after_http_date_is_honored(self) -> None:
        class FakeResponse:
            headers = {"retry-after": "Sun, 28 Sep 2026 15:25:30 GMT"}

        class FakeGeminiError(Exception):
            response = FakeResponse()

        now = datetime(2026, 9, 28, 15, 25, 0, tzinfo=timezone.utc)
        self.assertEqual(
            tb.gemini_retry_after_seconds(FakeGeminiError("hidden"), now=now),
            30,
        )

    def test_runtime_secret_path_is_tmpfs_location(self) -> None:
        self.assertEqual(
            tb.GEMINI_SECRET_PATH,
            Path("/run/influencerresearch-secrets/gemini_api_key"),
        )

    def test_gemini_key_environment_variable_is_ignored(self) -> None:
        old = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "SHOULD_NOT_BE_USED"
        try:
            with tempfile.TemporaryDirectory() as td:
                missing = Path(td) / "missing"
                self.assertIsNone(tb.read_gemini_api_key(missing))
        finally:
            if old is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = old

    def test_auto_falls_back_to_local_without_runtime_secret(self) -> None:
        with patch.object(tb, "read_gemini_api_key", return_value=None), patch.object(
            tb,
            "transcribe_faster_whisper",
            return_value={"provider": "faster-whisper", "text": "ok"},
        ) as local:
            result = tb.transcribe_video(Path("video.mp4"), {"provider": "auto"})
        self.assertEqual(result["provider"], "faster-whisper")
        local.assert_called_once()

    def test_auto_passes_bounded_transcription_limits_to_gemini(self) -> None:
        with patch.object(tb, "read_gemini_api_key", return_value="secret"), patch.object(
            tb,
            "transcribe_gemini_bounded",
            return_value={"provider": "gemini", "text": "ok"},
        ) as gemini:
            result = tb.transcribe_video(
                Path("video.mp4"),
                {
                    "provider": "auto",
                    "gemini_transcription_timeout_ms": 999_999,
                    "gemini_transcription_retry_attempts": 99,
                    "audio_extract_timeout_seconds": 999,
                    "gemini_transcription_total_timeout_seconds": 999,
                },
            )

        self.assertEqual(result["provider"], "gemini")
        gemini.assert_called_once_with(
            Path("video.mp4"),
            model=tb.DEFAULT_GEMINI_MODEL,
            timeout_ms=30_000,
            retry_attempts=2,
            audio_timeout_seconds=120,
            total_timeout_seconds=120,
        )

    def test_auto_falls_back_when_gemini_fails(self) -> None:
        with patch.object(tb, "read_gemini_api_key", return_value="secret"), patch.object(
            tb,
            "transcribe_gemini_bounded",
            side_effect=RuntimeError("remote failure"),
        ), patch.object(
            tb,
            "transcribe_faster_whisper",
            return_value={"provider": "faster-whisper", "text": "local"},
        ):
            result = tb.transcribe_video(Path("video.mp4"), {"provider": "auto"})
        self.assertEqual(result["provider"], "faster-whisper")
        self.assertEqual(result["fallback_from"], "gemini")
        self.assertIn("RuntimeError", result["fallback_error"])
        self.assertNotIn("secret", result["fallback_error"])

    def test_bounded_gemini_worker_timeout_removes_audio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "audio.mp3"
            audio.write_bytes(b"fixture")
            with (
                patch.object(tb, "read_gemini_api_key", return_value="secret"),
                patch.object(tb, "_extract_audio", return_value=audio),
                patch.object(
                    tb.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired("worker", 30),
                ) as run,
            ):
                with self.assertRaisesRegex(TimeoutError, "hard provider deadline"):
                    tb.transcribe_gemini_bounded(
                        Path("video.mp4"),
                        total_timeout_seconds=30,
                    )

            self.assertFalse(audio.exists())
            self.assertEqual(run.call_args.kwargs["timeout"], 30)
            self.assertFalse(run.call_args.kwargs["shell"])

    def test_bounded_gemini_worker_accepts_terminal_json_and_cleans_audio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "audio.mp3"
            audio.write_bytes(b"fixture")
            worker_result = {
                "provider": "gemini",
                "model": tb.DEFAULT_GEMINI_MODEL,
                "text": "bounded transcript",
                "language": None,
                "language_probability": None,
                "duration": None,
                "segments": [],
            }
            completed = subprocess.CompletedProcess(
                args=["worker"],
                returncode=0,
                stdout=(
                    "harmless worker diagnostic\n"
                    + tb.json.dumps({"ok": True, "result": worker_result})
                    + "\n"
                ),
                stderr="",
            )
            with (
                patch.object(tb, "read_gemini_api_key", return_value="secret"),
                patch.object(tb, "_extract_audio", return_value=audio),
                patch.object(tb.subprocess, "run", return_value=completed) as run,
            ):
                result = tb.transcribe_gemini_bounded(Path("video.mp4"))

            self.assertEqual(result["text"], "bounded transcript")
            self.assertFalse(audio.exists())
            command = run.call_args.args[0]
            self.assertIn("--gemini-audio-worker", command)
            self.assertNotIn("secret", command)

    def test_extract_audio_timeout_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            temp_audio = Path(td) / "timeout.mp3"
            temp_audio.write_bytes(b"partial")

            with (
                patch.object(
                    tb.tempfile,
                    "mkstemp",
                    return_value=(123, str(temp_audio)),
                ),
                patch.object(tb.os, "close") as close_fd,
                patch.dict(
                    sys.modules,
                    {
                        "imageio_ffmpeg": unittest.mock.Mock(
                            get_ffmpeg_exe=unittest.mock.Mock(return_value="ffmpeg")
                        )
                    },
                ),
                patch.object(
                    tb.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired("ffmpeg", 60),
                ),
            ):
                with self.assertRaisesRegex(TimeoutError, "audio extraction timed out"):
                    tb._extract_audio(Path("video.mp4"), timeout_seconds=60)

            self.assertFalse(temp_audio.exists())
            close_fd.assert_called_once_with(123)

    def test_explicit_gemini_does_not_silently_use_environment_key(self) -> None:
        with patch.object(tb, "read_gemini_api_key", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "runtime secret"):
                tb.transcribe_gemini(Path("video.mp4"))


    def test_visual_extraction_requires_runtime_secret(self) -> None:
        with patch.object(tb, "read_gemini_api_key", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "runtime secret"):
                tb.extract_visible_text_gemini(Path("video.mp4"))

    def test_story_image_extraction_requires_runtime_secret(self) -> None:
        with patch.object(tb, "read_gemini_api_key", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "runtime secret"):
                tb.extract_image_evidence_gemini(Path("story.png"))



if __name__ == "__main__":
    unittest.main()
