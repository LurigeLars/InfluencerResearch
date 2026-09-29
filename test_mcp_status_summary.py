from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_status_summary import summarize_status


class McpStatusSummaryTests(unittest.TestCase):
    def test_failed_job_exposes_sanitized_top_level_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text(
                json.dumps({
                    "state": "FAILED",
                    "error": "RuntimeError: fixture failure",
                    "internal_debug": "must stay private",
                }),
                encoding="utf-8",
            )
            summary = summarize_status(path)

        self.assertEqual(summary["state"], "FAILED")
        self.assertEqual(summary["error"], "RuntimeError: fixture failure")
        self.assertNotIn("internal_debug", summary)

    def test_timings_preserve_compact_ingestion_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text(
                json.dumps({
                    "state": "COMPLETE",
                    "timings": {
                        "ingestion_pipeline": [{
                            "creator_key": "nicholascrown",
                            "platform": "TIKTOK",
                            "requested": 3,
                            "completed_count": 3,
                            "discovery_skipped_for_exact_ids": True,
                            "timings_ms": {
                                "discovery": 0.1,
                                "visual_evidence": 1234.5,
                                "total": 1500.0,
                            },
                        }]
                    },
                    "ingestion_results": [{"private": "verbose"}],
                }),
                encoding="utf-8",
            )
            summary = summarize_status(path)

        self.assertEqual(
            summary["timings"]["ingestion_pipeline"][0]["platform"],
            "TIKTOK",
        )
        self.assertTrue(
            summary["timings"]["ingestion_pipeline"][0][
                "discovery_skipped_for_exact_ids"
            ]
        )
        self.assertNotIn("ingestion_results", summary)


if __name__ == "__main__":
    unittest.main()
