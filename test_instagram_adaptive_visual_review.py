from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import instagram_ingest as ig
from video_visual_evidence import VISUAL_REVIEW_POLICY_VERSION


class InstagramAdaptiveVisualReviewTests(unittest.TestCase):
    def test_targeted_existing_reel_is_selected_for_visual_backfill(self) -> None:
        manifest = {
            "items": {
                "abc123": {
                    "download_status": "DONE",
                    "transcription_status": "DONE",
                    "video_file": "output/nicholascrown/raw/abc123.mp4",
                }
            }
        }
        selected = ig.select_visual_evidence_keys(
            manifest,
            [],
            new_only=True,
            only_shortcodes={"abc123"},
        )
        self.assertEqual(selected, ["abc123"])

    def test_visual_failure_fails_open_and_records_policy_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "output" / "nicholascrown" / "raw" / "abc123.mp4"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"video-placeholder")
            manifest = {
                "items": {
                    "abc123": {
                        "creator": "nicholascrown",
                        "download_status": "DONE",
                        "transcription_status": "DONE",
                        "video_file": str(media.relative_to(root)),
                    }
                }
            }
            with patch.object(
                ig,
                "capture_local_video_visual_evidence",
                return_value={"ok": False, "error": "test-unavailable"},
            ):
                result = ig.enrich_visual_evidence(root, manifest, ["abc123"])

            item = manifest["items"]["abc123"]
            self.assertEqual(result["fail_open"], 1)
            self.assertEqual(item["visual_evidence_status"], "ERROR")
            self.assertEqual(item["visual_review_policy_version"], VISUAL_REVIEW_POLICY_VERSION)
            self.assertEqual(item["analysis_mode_recommended"], "TRANSCRIPT_ONLY")


if __name__ == "__main__":
    unittest.main()