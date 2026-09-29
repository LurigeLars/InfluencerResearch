from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import creator_recent_check as crc


class CreatorRecentVisualReviewStatusTests(unittest.TestCase):
    def test_queue_targets_expose_compact_visual_review_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            state.mkdir()
            (state / "research_queue.json").write_text(
                json.dumps({
                    "items": [{
                        "queue_id": "tt_123",
                        "analysis_owner": "EKONOMI",
                        "analysis_status": "PENDING_ANALYSIS",
                        "creator": "nicholascrown",
                        "source_platform": "TIKTOK",
                        "source_id": "123",
                        "analysis_content_status": "READY",
                        "analysis_content_reason": "TRANSCRIPT",
                        "analysis_mode_recommended": "TRANSCRIPT_PLUS_VISUAL_REVIEW",
                        "visual_review_recommended": True,
                        "visual_review_reason": ["PER_VIDEO_VISUAL_SIGNAL"],
                        "creator_visual_prior": "NEUTRAL",
                        "visual_review_policy_version": 1,
                        "transcript_text": "enough evidence text",
                    }]
                }),
                encoding="utf-8",
            )
            targets = crc._queue_targets(root, {"tt_123"})
            self.assertEqual(len(targets), 1)
            self.assertTrue(targets[0]["visual_review_recommended"])
            self.assertEqual(targets[0]["creator_visual_prior"], "NEUTRAL")
            self.assertEqual(targets[0]["analysis_mode_recommended"], "TRANSCRIPT_PLUS_VISUAL_REVIEW")


if __name__ == "__main__":
    unittest.main()