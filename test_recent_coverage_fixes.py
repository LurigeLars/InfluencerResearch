"""Regressions for cross-creator recent-ingestion coverage and browser resource handling."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import unittest.mock

import creator_recent_check as crc
import instagram_ingest as ig


class RecentCoverageFixTests(unittest.TestCase):
    def test_instagram_long_body_is_not_reel_grid_readiness(self):
        page = unittest.mock.Mock()
        page.url = "https://www.instagram.com/example/reels/"
        page.locator.return_value.count.return_value = 0
        page.locator.return_value.inner_text.return_value = "Profile content " * 40
        state = ig._wait_for_instagram_profile_ready(
            page, "example", max_wait_ms=3, poll_ms=1,
        )
        self.assertFalse(state["ready"])
        self.assertEqual(state["reel_link_count"], 0)

    def test_discovery_recovers_reel_from_profile_when_reels_grid_is_empty(self):
        class FakePlaywright:
            def __enter__(self):
                return object()
            def __exit__(self, *args):
                return False

        page = unittest.mock.Mock()
        context = unittest.mock.Mock()
        context.new_page.return_value = page
        browser = unittest.mock.Mock()
        video = "https://www.instagram.com/reel/RECENT123/"
        with (
            unittest.mock.patch.object(ig, "sync_playwright", return_value=FakePlaywright()),
            unittest.mock.patch.object(
                ig, "launch_instagram_ephemeral_context",
                return_value=(browser, context),
            ),
            unittest.mock.patch.object(ig, "verify_logged_in"),
            unittest.mock.patch.object(
                ig, "_wait_for_instagram_profile_ready",
                return_value={"ready": True, "blocked": False,
                              "media_auth_gated": False, "unavailable": False},
            ),
            unittest.mock.patch.object(
                ig, "_collect_loaded_reel_urls",
                side_effect=[([], 1), ([video], 2)],
            ) as collect,
        ):
            result = ig.discover_reels_authenticated(
                "example", known_reel_times={
                    "RECENT123": "2026-10-14T15:30:00+00:00",
                },
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reel_count"], 1)
        self.assertTrue(result["timings"]["profile_fallback_attempted"])
        self.assertEqual(collect.call_count, 2)
        self.assertEqual(
            page.goto.call_args_list[1].args[0],
            "https://www.instagram.com/example/",
        )

    def test_registered_youtube_is_promoted_and_reconciled_into_analysis(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "state").mkdir()
            (root / "state" / "manifest.json").write_text(json.dumps({
                "items": {
                    "yt_ABC12345678": {
                        "creator": "matfinog",
                        "source_platform": "YOUTUBE",
                        "source_id": "ABC12345678",
                        "evaluation_mode": "CREATOR_EVALUATION",
                        "permanent_source": False,
                        "download_status": "DONE",
                        "transcription_status": "DONE",
                    },
                },
            }))
            (root / "state" / "creator_evaluation_status.json").write_text(
                json.dumps({
                    "completed": ["yt_ABC12345678"], "state": "COMPLETE",
                    "failure_count": 0,
                })
            )
            (root / "state" / "research_queue.json").write_text(
                json.dumps({"items": []})
            )
            with (
                unittest.mock.patch.object(
                    crc.subprocess, "run",
                    return_value=SimpleNamespace(returncode=0),
                ),
                unittest.mock.patch.object(
                    crc.tts, "run_research_queue",
                    return_value={"ok": True},
                ) as queue,
                unittest.mock.patch.object(
                    crc, "_analysis_candidate_coverage",
                    return_value={"queued": ["yt_ABC12345678"], "missing": []},
                ),
            ):
                result = crc._ingest_youtube(
                    root, "matfinog", ["ABC12345678"],
                )
            manifest = json.loads(
                (root / "state" / "manifest.json").read_text()
            )
            item = manifest["items"]["yt_ABC12345678"]
            self.assertTrue(item["permanent_source"])
            self.assertEqual(item["evaluation_mode"], "RECENT_MONITORING")
            self.assertEqual(result["completed_ids"], ["ABC12345678"])
            self.assertEqual(result["analysis_coverage"]["missing"], [])
            queue.assert_called_once_with(
                root, must_include=["yt_ABC12345678"],
            )

    def test_youtube_missing_analysis_target_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "state").mkdir()
            (root / "state" / "manifest.json").write_text(json.dumps({
                "items": {"yt_ABC12345678": {
                    "download_status": "DONE",
                    "transcription_status": "DONE",
                }}
            }))
            (root / "state" / "creator_evaluation_status.json").write_text(
                json.dumps({"completed": ["yt_ABC12345678"]})
            )
            with (
                unittest.mock.patch.object(crc.subprocess, "run",
                                  return_value=SimpleNamespace(returncode=0)),
                unittest.mock.patch.object(crc.tts, "run_research_queue",
                                  return_value={"ok": True}),
                unittest.mock.patch.object(crc, "_analysis_candidate_coverage",
                                  return_value={"missing": ["yt_ABC12345678"]}),
            ):
                result = crc._ingest_youtube(root, "matfinog", ["ABC12345678"])
        self.assertIn("YOUTUBE_ANALYSIS_TARGET_MISSING:yt_ABC12345678",
                      result["errors"])


if __name__ == "__main__":
    unittest.main()
