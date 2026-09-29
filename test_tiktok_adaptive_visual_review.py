from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tiktok_camofox_sync as tts
from video_visual_evidence import VISUAL_REVIEW_POLICY_VERSION


class TikTokAdaptiveVisualReviewTests(unittest.TestCase):
    def test_existing_transcript_still_requires_current_visual_policy(self) -> None:
        item = {
            "download_status": "DONE",
            "transcription_status": "DONE",
            "visual_text_status": "NOT_NEEDED",
            "research_status": "PENDING",
        }
        self.assertFalse(tts._manifest_item_extraction_complete(item))
        item["visual_review_policy_version"] = VISUAL_REVIEW_POLICY_VERSION
        self.assertTrue(tts._manifest_item_extraction_complete(item))

    def test_manifest_keeps_transcript_and_adds_per_video_visual_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "state").mkdir()
            media = root / "output" / "nicholascrown" / "tiktok" / "videos" / "1234567890.mp4"
            transcript_txt = root / "output" / "nicholascrown" / "tiktok" / "transcripts" / "1234567890.txt"
            transcript_json = transcript_txt.with_suffix(".json")
            visual_index = root / "output" / "nicholascrown" / "tiktok" / "frames" / "1234567890" / "visual_index.json"
            for path in (media, transcript_txt, transcript_json, visual_index):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")

            record = tts.update_main_manifest(
                root,
                creator_key="nicholascrown",
                url="https://www.tiktok.com/@nicholascrown/video/1234567890",
                download={
                    "video_id": "1234567890",
                    "media_file": media,
                    "info_file": None,
                    "validation": "ok",
                },
                transcription={
                    "txt": transcript_txt,
                    "json": transcript_json,
                    "transcribed_at": "2026-09-29T12:00:00+00:00",
                    "source": "gemini",
                    "provider": "gemini",
                    "model": "test",
                    "text": "This is already a sufficiently long transcript for analysis.",
                },
                visual_text=None,
                visual_evidence={
                    "ok": True,
                    "index": visual_index,
                    "retained_frames": 8,
                    "capture_strategy": "TEST",
                    "agent_visual_bundle": {
                        "available": True,
                        "analysis_mode_recommended": "TRANSCRIPT_PLUS_VISUAL_REVIEW",
                        "visual_review_recommended": True,
                        "visual_review_reason": ["PER_VIDEO_VISUAL_SIGNAL"],
                        "creator_visual_prior": "NEUTRAL",
                        "representative_frames": [],
                    },
                },
            )

            self.assertEqual(record["analysis_mode_recommended"], "TRANSCRIPT_PLUS_VISUAL_REVIEW")
            self.assertTrue(record["visual_review_recommended"])
            self.assertEqual(record["creator_visual_prior"], "NEUTRAL")
            self.assertEqual(record["visual_frame_count"], 8)
            self.assertEqual(record["visual_review_policy_version"], VISUAL_REVIEW_POLICY_VERSION)


if __name__ == "__main__":
    unittest.main()