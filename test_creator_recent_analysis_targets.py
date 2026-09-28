from __future__ import annotations

import json
import tempfile
import unittest
import sys
from pathlib import Path
from types import ModuleType

# This unit only exercises the queue-target projection. Stub browser-only modules
# so CI does not need Playwright just to import creator_recent_check. Keep the stub
# contract complete enough that later tests in the same unittest process are not
# poisoned by a half-empty module in sys.modules.
instagram_stub = sys.modules.setdefault(
    "instagram_camofox_public_smoke",
    ModuleType("instagram_camofox_public_smoke"),
)
if not hasattr(instagram_stub, "extract_reel_urls"):
    instagram_stub.extract_reel_urls = (
        lambda url: [url] if "/reel/" in str(url) else []
    )
if not hasattr(instagram_stub, "probe_public_session"):
    instagram_stub.probe_public_session = lambda *args, **kwargs: {}

ephemeral_stub = sys.modules.setdefault(
    "ephemeral_ingest",
    ModuleType("ephemeral_ingest"),
)
if not hasattr(ephemeral_stub, "run_one"):
    ephemeral_stub.run_one = lambda *args, **kwargs: {
        "state": "DONE",
        "capture": {"reason": "NO_ACTIVE_STORY_OR_STORY_VIEW_REDIRECTED"},
        "errors": [],
    }

import creator_recent_check as crc


class RecentAnalysisTargetTests(unittest.TestCase):
    def test_queue_targets_include_bounded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            state.mkdir(parents=True)
            evidence = "Liquidity and positioning evidence. " * 300
            queue = {
                "items": [
                    {
                        "queue_id": "tt_123",
                        "creator": "creator",
                        "source_platform": "TIKTOK",
                        "source_id": "123",
                        "published_at": "2026-09-28T00:00:00+00:00",
                        "source_url": "https://www.tiktok.com/@creator/video/123",
                        "caption": "Caption",
                        "analysis_owner": "EKONOMI",
                        "analysis_status": "PENDING_ANALYSIS",
                        "analysis_content_status": "READY",
                        "analysis_content_reason": "TRANSCRIPT",
                        "transcript_source": "gemini",
                        "word_count": 1200,
                        "source_subtype": "STORY",
                        "published_at_basis": "ACTIVE_STORY_OBSERVED_AT",
                        "observed_at": "2026-09-28T00:00:00+00:00",
                        "visual_evidence_status": "DONE",
                        "visual_evidence_index": "output/creator/stories/screenshots/123.png",
                        "visual_frame_count": 1,
                        "visual_capture_strategy": "INSTAGRAM_STORY_SCREENSHOT",
                        "screenshot_file": "output/creator/stories/screenshots/123.png",
                        "media_retention": "EPHEMERAL_CAPTURE",
                        "analysis_evidence_text": evidence,
                    }
                ]
            }
            (state / "research_queue.json").write_text(json.dumps(queue), encoding="utf-8")

            targets = crc._queue_targets(root, {"tt_123"})
            self.assertEqual(len(targets), 1)
            target = targets[0]
            self.assertEqual(target["analysis_content_status"], "READY")
            self.assertEqual(target["analysis_content_reason"], "TRANSCRIPT")
            self.assertEqual(target["source_subtype"], "STORY")
            self.assertEqual(target["visual_evidence_status"], "DONE")
            self.assertEqual(
                target["screenshot_file"],
                "output/creator/stories/screenshots/123.png",
            )
            self.assertEqual(target["visual_frame_count"], 1)
            self.assertTrue(target["analysis_evidence_text"])
            self.assertLessEqual(
                len(target["analysis_evidence_text"]),
                crc.MAX_ANALYSIS_EVIDENCE_CHARS,
            )
            self.assertTrue(target["analysis_evidence_truncated"])


if __name__ == "__main__":
    unittest.main()
