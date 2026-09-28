import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ephemeral_ingest as ei


class StoryLocalOcrTests(unittest.TestCase):
    def test_extract_story_text_local_ocr_uses_bounded_tesseract(self):
        shot = Path("/tmp/story.png")
        with patch("ephemeral_ingest.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "  Oil   battleground\n123  "
            run.return_value.stderr = ""
            result = ei.extract_story_text_local_ocr(shot)

        self.assertEqual(result["text"], "Oil battleground\n123")
        self.assertEqual(result["source"], "LOCAL_OCR")
        self.assertEqual(result["provider"], "tesseract")
        args, kwargs = run.call_args
        self.assertIn("tesseract", args[0])
        self.assertEqual(kwargs["timeout"], ei.STORY_OCR_TIMEOUT_SECONDS)

    def _manifest(self, root: Path, *, deferred=False):
        shot = root / "shot.png"
        shot.write_bytes(b"not-a-real-png")
        item = {
            "source_type": "STORY",
            "research_status": "NEW",
            "screenshot_file": "shot.png",
        }
        if deferred:
            item.update({
                "visual_description_status": "DEFERRED",
                "visual_description_deferred_reason": "PROVIDER_RATE_LIMIT",
                "visual_description_retry_after": "2099-01-01T00:00:00+00:00",
            })
        return {"items": {"story-1": item}}

    def test_sufficient_local_ocr_skips_gemini(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root)
            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "one two three four five six seven eight",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_gemini") as gemini:
                result = ei.enrich_story_visual_evidence(root, manifest, ["story-1"])

            item = manifest["items"]["story-1"]
            self.assertEqual(item["visual_description_status"], "DONE")
            self.assertEqual(item["visual_description_source"], "LOCAL_OCR")
            self.assertEqual(item["visual_description_provider"], "tesseract")
            self.assertEqual(result["ocr_completed"], 1)
            self.assertEqual(result["attempted"], 0)
            gemini.assert_not_called()

    def test_insufficient_local_ocr_falls_back_to_gemini(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root)
            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "tiny",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_gemini", return_value={
                "text": "rich visual evidence with enough information",
                "source": "GEMINI_STORY_SCREENSHOT_EVIDENCE",
                "provider": "gemini",
                "model": "test-model",
            }) as gemini:
                result = ei.enrich_story_visual_evidence(root, manifest, ["story-1"])

            self.assertEqual(result["ocr_insufficient"], 1)
            self.assertEqual(result["attempted"], 1)
            self.assertEqual(manifest["items"]["story-1"]["visual_description_provider"], "gemini")
            gemini.assert_called_once()

    def test_local_ocr_can_recover_during_gemini_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root, deferred=True)
            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "one two three four five six seven eight",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_gemini") as gemini:
                result = ei.enrich_story_visual_evidence(root, manifest, ["story-1"], circuit_state={
                    "open": True,
                    "reason": "PROVIDER_RATE_LIMIT",
                    "retry_after": "2099-01-01T00:00:00+00:00",
                    "retry_after_source": "FALLBACK",
                })

            item = manifest["items"]["story-1"]
            self.assertEqual(item["visual_description_status"], "DONE")
            self.assertNotIn("visual_description_retry_after", item)
            self.assertEqual(result["ocr_completed"], 1)
            gemini.assert_not_called()


if __name__ == "__main__":
    unittest.main()
