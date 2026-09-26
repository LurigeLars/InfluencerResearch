from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import transcription_backend as tb


class TranscriptionBackendTests(unittest.TestCase):
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

    def test_auto_falls_back_when_gemini_fails(self) -> None:
        with patch.object(tb, "read_gemini_api_key", return_value="secret"), patch.object(
            tb,
            "transcribe_gemini",
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

    def test_explicit_gemini_does_not_silently_use_environment_key(self) -> None:
        with patch.object(tb, "read_gemini_api_key", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "runtime secret"):
                tb.transcribe_gemini(Path("video.mp4"))


if __name__ == "__main__":
    unittest.main()
