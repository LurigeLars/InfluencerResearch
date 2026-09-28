import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import transcription_backend as tb


class StoryOllamaBackendTests(unittest.TestCase):
    def test_ollama_image_request_is_bounded_and_multimodal(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "story.png"
            image_path.write_bytes(b"png-bytes")
            response = io.BytesIO(json.dumps({
                "message": {"content": "Visible chart title and price 123.45 with supporting labels"}
            }).encode("utf-8"))

            with patch("transcription_backend.urllib_request.urlopen", return_value=response) as urlopen:
                result = tb.extract_image_evidence_ollama(
                    image_path,
                    model="gemma3-12b-16k",
                    base_url="http://host.docker.internal:11434",
                    timeout_seconds=12,
                    ocr_hint="noisy OCR",
                )

            request = urlopen.call_args.args[0]
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(request.full_url, "http://host.docker.internal:11434/api/chat")
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 12)
            self.assertEqual(payload["model"], "gemma3-12b-16k")
            self.assertFalse(payload["stream"])
            self.assertEqual(len(payload["messages"][0]["images"]), 1)
            self.assertIn("noisy OCR", payload["messages"][0]["content"])
            self.assertEqual(result["provider"], "ollama")
            self.assertEqual(result["source"], "OLLAMA_STORY_SCREENSHOT_EVIDENCE")

    def test_ollama_embedded_no_content_marker_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "story.png"
            image_path.write_bytes(b"png-bytes")
            response = io.BytesIO(json.dumps({
                "message": {"content": "Visible headline: NVIDIA AI agents\nNO_MEANINGFUL_VISUAL_EVIDENCE"}
            }).encode("utf-8"))
            with patch("transcription_backend.urllib_request.urlopen", return_value=response):
                result = tb.extract_image_evidence_ollama(image_path)
            self.assertEqual(result["text"], "Visible headline: NVIDIA AI agents")

    def test_ollama_oversized_output_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "story.png"
            image_path.write_bytes(b"png-bytes")
            response = io.BytesIO(json.dumps({
                "message": {"content": "x" * (tb.OLLAMA_VISUAL_MAX_CHARS + 1)}
            }).encode("utf-8"))
            with patch("transcription_backend.urllib_request.urlopen", return_value=response):
                result = tb.extract_image_evidence_ollama(image_path)
            self.assertEqual(result["text"], "")

    def test_ollama_no_content_marker_becomes_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "story.png"
            image_path.write_bytes(b"png-bytes")
            response = io.BytesIO(json.dumps({
                "message": {"content": "NO_MEANINGFUL_VISUAL_EVIDENCE"}
            }).encode("utf-8"))
            with patch("transcription_backend.urllib_request.urlopen", return_value=response):
                result = tb.extract_image_evidence_ollama(image_path)
            self.assertEqual(result["text"], "")


if __name__ == "__main__":
    unittest.main()
