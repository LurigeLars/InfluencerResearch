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
                    num_ctx=3072,
                    ocr_hint="noisy OCR",
                )

            request = urlopen.call_args.args[0]
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(request.full_url, "http://host.docker.internal:11434/api/chat")
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 12)
            self.assertEqual(payload["model"], "gemma3-12b-16k")
            self.assertEqual(payload["options"]["num_ctx"], 3072)
            self.assertFalse(payload["stream"])
            self.assertEqual(len(payload["messages"][0]["images"]), 1)
            self.assertIn("noisy OCR", payload["messages"][0]["content"])
            self.assertEqual(result["provider"], "ollama")
            self.assertEqual(result["source"], "OLLAMA_STORY_SCREENSHOT_EVIDENCE")
            self.assertEqual(result["contract"], tb.OLLAMA_VISUAL_CONTRACT)
            self.assertEqual(result["num_ctx"], 3072)

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

    def test_ollama_drops_model_commentary_and_keeps_visible_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "story.png"
            image_path.write_bytes(b"png-bytes")
            response = io.BytesIO(json.dumps({
                "message": {"content": (
                    "snickarmiljonaren ◉ 25m\n"
                    "Watch full reel >\n"
                    "**Visible Text:**\n"
                    "* **Headline:** NVIDIA Open Agent Safety Platform\n"
                    "The image shows a complex architecture diagram.\n"
                    "Person: a man wearing a suit"
                )}
            }).encode("utf-8"))
            with patch("transcription_backend.urllib_request.urlopen", return_value=response):
                result = tb.extract_image_evidence_ollama(
                    image_path,
                    ocr_hint="NVIDIA Open Agent Safety Platform launched today",
                )
            self.assertEqual(result["text"], "NVIDIA Open Agent Safety Platform")
            self.assertNotIn("image shows", result["text"].lower())
            self.assertNotIn("person:", result["text"].lower())

    def test_ollama_ungrounded_output_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "story.png"
            image_path.write_bytes(b"png-bytes")
            response = io.BytesIO(json.dumps({
                "message": {"content": "Completely unrelated invented company earnings guidance"}
            }).encode("utf-8"))
            with patch("transcription_backend.urllib_request.urlopen", return_value=response):
                result = tb.extract_image_evidence_ollama(
                    image_path,
                    ocr_hint="Micron AMD ARM Intel memory processors payment",
                )
            self.assertEqual(result["text"], "")


if __name__ == "__main__":
    unittest.main()
