from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_status_summary import compact_job_status, summarize_status


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

    def test_failed_evaluation_exposes_failure_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text(
                json.dumps({
                    "state": "FAILED",
                    "failure_count": 1,
                    "failures": [{
                        "video_id": "abc123",
                        "stage": "visual_capture",
                        "detail": "yt-dlp fixture failure",
                    }],
                    "internal_debug": "must stay private",
                }),
                encoding="utf-8",
            )
            summary = summarize_status(path)

        self.assertEqual(summary["failure_count"], 1)
        self.assertEqual(summary["failures"][0]["stage"], "visual_capture")
        self.assertEqual(summary["failures"][0]["detail"], "yt-dlp fixture failure")
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


    def test_compact_job_status_drops_verbose_detail_lists_but_keeps_counts(self):
        payload = {
            "active": None,
            "last": {
                "job_id": "job-1",
                "kind": "creator_recent_check",
                "state": "COMPLETE",
                "status": {
                    "queued_for_analysis_count": 2,
                    "analysis_targets": [{"id": 1}, {"id": 2}],
                    "story_items": [{"id": "s1"}],
                    "extraction_error_count": 0,
                    "extraction_error_items": [],
                    "provider_health": {
                        "gemini": {
                            "last_status": "OK",
                            "last_success_at": "2026-10-02T00:00:00+00:00",
                            "story_visual_calls": 999,
                        }
                    },
                    "story_visual_enrichment": {
                        "by_creator": [{"creator_key": "demo", "large": "x" * 1000}],
                        "totals": {"completed": 1},
                        "ollama_budget": {"attempted": 0, "limit": 2},
                    },
                    "timings": {
                        "total_duration_ms": 123.4,
                        "stage_totals_ms": {"discovery": 12.0},
                        "slowest_operations": [{"detail": "x" * 1000}],
                    },
                },
            },
        }

        compact = compact_job_status(payload)
        status = compact["last"]["status"]

        self.assertEqual(status["queued_for_analysis_count"], 2)
        self.assertEqual(status["analysis_target_count"], 2)
        self.assertEqual(status["story_item_count"], 1)
        self.assertNotIn("analysis_targets", status)
        self.assertNotIn("story_items", status)
        self.assertNotIn("extraction_error_items", status)
        self.assertEqual(status["provider_health"]["gemini"]["last_status"], "OK")
        self.assertNotIn("story_visual_calls", status["provider_health"]["gemini"])
        self.assertNotIn("by_creator", status["story_visual_enrichment"])
        self.assertNotIn("slowest_operations", status["timings"])


if __name__ == "__main__":
    unittest.main()
