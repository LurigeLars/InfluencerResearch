from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
            self.assertTrue(target["analysis_evidence_text"])
            self.assertLessEqual(
                len(target["analysis_evidence_text"]),
                crc.MAX_ANALYSIS_EVIDENCE_CHARS,
            )
            self.assertTrue(target["analysis_evidence_truncated"])


if __name__ == "__main__":
    unittest.main()
