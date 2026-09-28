from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import tiktok_camofox_sync as sync


class TikTokContentFallbackTests(unittest.TestCase):
    def test_old_insufficient_item_is_retryable_once(self) -> None:
        item = {
            "download_status": "DONE",
            "transcription_status": "DONE",
            "research_status": "INSUFFICIENT_CONTENT",
        }
        self.assertFalse(sync._manifest_item_extraction_complete(item))

    def test_exhausted_insufficient_item_is_not_reprocessed_forever(self) -> None:
        item = {
            "download_status": "DONE",
            "transcription_status": "DONE",
            "research_status": "INSUFFICIENT_CONTENT",
            "content_extraction_version": sync.CONTENT_EXTRACTION_VERSION,
            "content_extraction_exhausted": True,
            "visual_text_status": "NO_VISIBLE_TEXT",
        }
        self.assertTrue(sync._manifest_item_extraction_complete(item))

    def test_visual_failure_remains_retryable(self) -> None:
        item = {
            "download_status": "DONE",
            "transcription_status": "DONE",
            "research_status": "INSUFFICIENT_CONTENT",
            "content_extraction_version": sync.CONTENT_EXTRACTION_VERSION,
            "content_extraction_exhausted": False,
            "visual_text_status": "FAILED",
        }
        self.assertFalse(sync._manifest_item_extraction_complete(item))

    def test_manifest_persists_visible_text_and_extraction_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "state").mkdir(parents=True)
            media = root / "output" / "creator" / "tiktok" / "videos" / "123.mp4"
            transcript = root / "output" / "creator" / "tiktok" / "transcripts" / "123.txt"
            transcript_json = transcript.with_suffix(".json")
            visual = root / "output" / "creator" / "tiktok" / "visual_text" / "123.txt"
            visual_json = visual.with_suffix(".json")
            for path in (media, transcript, transcript_json, visual, visual_json):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            (root / "state" / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "items": {}}),
                encoding="utf-8",
            )

            record = sync.update_main_manifest(
                root,
                creator_key="creator",
                url="https://www.tiktok.com/@creator/video/123",
                download={
                    "video_id": "123",
                    "media_file": media,
                    "info_file": None,
                    "validation": "ok",
                },
                transcription={
                    "text": "",
                    "source": "faster-whisper",
                    "provider": "faster-whisper",
                    "model": "small",
                    "txt": transcript,
                    "json": transcript_json,
                    "transcribed_at": "2026-09-28T00:00:00+00:00",
                },
                visual_text={
                    "ok": True,
                    "text": "Visible subtitle text that contains enough useful content for analysis.",
                    "source": "GEMINI_VIDEO_VISIBLE_TEXT",
                    "provider": "gemini",
                    "model": "gemini-3.8-flash",
                    "processing": "static",
                    "txt": visual,
                    "json": visual_json,
                },
            )

            self.assertEqual(record["visual_text_status"], "DONE")
            self.assertIn("Visible subtitle", record["visible_text"])
            self.assertEqual(record["content_extraction_version"], sync.CONTENT_EXTRACTION_VERSION)
            self.assertTrue(record["content_extraction_exhausted"])


if __name__ == "__main__":
    unittest.main()
