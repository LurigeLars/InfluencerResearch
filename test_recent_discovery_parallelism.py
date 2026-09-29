from __future__ import annotations

import threading
import time
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import creator_recent_check as crc


class RecentDiscoveryParallelismTests(unittest.TestCase):
    def test_discovery_worker_env_is_bounded_and_invalid_values_fall_back(self):
        with patch.dict(
            "os.environ",
            {"INFLUENCER_RESEARCH_DISCOVERY_BROWSER_WORKERS": "3"},
            clear=False,
        ):
            self.assertEqual(
                crc._bounded_env_int(
                    "INFLUENCER_RESEARCH_DISCOVERY_BROWSER_WORKERS",
                    2,
                    1,
                    4,
                ),
                3,
            )
        with patch.dict(
            "os.environ",
            {"INFLUENCER_RESEARCH_DISCOVERY_BROWSER_WORKERS": "99"},
            clear=False,
        ):
            self.assertEqual(
                crc._bounded_env_int(
                    "INFLUENCER_RESEARCH_DISCOVERY_BROWSER_WORKERS",
                    2,
                    1,
                    4,
                ),
                4,
            )
        with patch.dict(
            "os.environ",
            {"INFLUENCER_RESEARCH_DISCOVERY_BROWSER_WORKERS": "bad"},
            clear=False,
        ):
            self.assertEqual(
                crc._bounded_env_int(
                    "INFLUENCER_RESEARCH_DISCOVERY_BROWSER_WORKERS",
                    2,
                    1,
                    4,
                ),
                2,
            )

    def test_browser_discovery_is_bounded_and_results_stay_deterministic(self):
        active_browser = 0
        max_browser = 0
        guard = threading.Lock()
        instagram_run_indexes = []
        youtube_overlap = threading.Event()

        selected = [
            (
                {"creator_key": "alpha"},
                [
                    {"platform": "INSTAGRAM", "profile_url": "https://instagram.example/alpha"},
                    {"platform": "YOUTUBE", "profile_url": "https://youtube.example/alpha"},
                ],
            ),
            (
                {"creator_key": "beta"},
                [{"platform": "TIKTOK", "profile_url": "https://tiktok.example/@beta"}],
            ),
            (
                {"creator_key": "gamma"},
                [{"platform": "INSTAGRAM", "profile_url": "https://instagram.example/gamma"}],
            ),
            (
                {"creator_key": "delta"},
                [{"platform": "TIKTOK", "profile_url": "https://tiktok.example/@delta"}],
            ),
        ]

        def browser_result(profile, platform):
            nonlocal active_browser, max_browser
            with guard:
                active_browser += 1
                max_browser = max(max_browser, active_browser)
            try:
                youtube_overlap.wait(timeout=0.2)
                time.sleep(0.03)
                return {
                    "creator_key": profile["creator_key"],
                    "platform": platform,
                    "items": [],
                    "missing_publish_time_ids": [],
                    "window_complete": True,
                }
            finally:
                with guard:
                    active_browser -= 1

        def fake_instagram(
            profile,
            source,
            cutoff,
            end,
            discovery_limit,
            *,
            run_index=1,
            root=None,
        ):
            self.assertIsNotNone(root)
            instagram_run_indexes.append(run_index)
            return browser_result(profile, "INSTAGRAM")

        def fake_tiktok(root, profile, source, cutoff, end, discovery_limit):
            return browser_result(profile, "TIKTOK")

        def fake_youtube(profile, source, cutoff, end, discovery_limit):
            youtube_overlap.set()
            time.sleep(0.02)
            return {
                "creator_key": profile["creator_key"],
                "platform": "YOUTUBE",
                "items": [],
                "missing_publish_time_ids": [],
                "window_complete": True,
                "timings": [],
            }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(crc.tts, "start_server") as start_server,
                patch.object(crc, "discover_instagram", side_effect=fake_instagram),
                patch.object(crc, "discover_tiktok", side_effect=fake_tiktok),
                patch.object(crc, "discover_youtube", side_effect=fake_youtube),
            ):
                result = crc._run_discovery_batch(
                    Path(tmp),
                    selected,
                    datetime(2026, 9, 28, tzinfo=timezone.utc),
                    datetime(2026, 9, 29, tzinfo=timezone.utc),
                    15,
                )

        discoveries, errors, source_map, timings, meta = result
        self.assertEqual(errors, [])
        start_server.assert_called_once()
        self.assertEqual(max_browser, crc.DISCOVERY_BROWSER_WORKERS)
        self.assertEqual(sorted(instagram_run_indexes), [1, 4])
        self.assertEqual(
            [(row["creator_key"], row["platform"]) for row in discoveries],
            [
                ("alpha", "INSTAGRAM"),
                ("alpha", "YOUTUBE"),
                ("beta", "TIKTOK"),
                ("gamma", "INSTAGRAM"),
                ("delta", "TIKTOK"),
            ],
        )
        self.assertEqual(meta["browser_task_count"], 4)
        self.assertEqual(meta["browser_worker_limit"], 2)
        self.assertEqual(meta["network_task_count"], 1)
        self.assertEqual(meta["network_worker_limit"], 1)
        self.assertEqual(len(source_map), 5)
        self.assertEqual(
            len([row for row in timings if row["stage"] == "DISCOVERY"]),
            5,
        )

    def test_story_capture_overlaps_discovery(self):
        story_started = threading.Event()
        discovery_started = threading.Event()

        selected = [
            (
                {"creator_key": "alpha"},
                [{"platform": "INSTAGRAM", "profile_url": "https://www.instagram.com/alpha/"}],
            )
        ]

        def fake_story_batch(*args, **kwargs):
            story_started.set()
            self.assertTrue(discovery_started.wait(timeout=1.0))
            time.sleep(0.02)
            return {
                "results": [],
                "creator_count": 1,
                "wall_duration_ms": 20.0,
            }

        def fake_discovery(*args, **kwargs):
            discovery_started.set()
            self.assertTrue(story_started.wait(timeout=1.0))
            time.sleep(0.02)
            return ([], [], {}, [], {"wall_duration_ms": 20.0})

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(crc, "_run_story_capture_batch", side_effect=fake_story_batch),
                patch.object(crc, "_run_discovery_batch", side_effect=fake_discovery),
            ):
                discovery, story = crc._run_discovery_with_story_prefetch(
                    Path(tmp),
                    selected,
                    datetime(2026, 9, 28, tzinfo=timezone.utc),
                    datetime(2026, 9, 29, tzinfo=timezone.utc),
                    15,
                    10,
                    gemini_circuit={},
                    ollama_budget_state={"attempted": 0},
                )

        self.assertEqual(discovery[0], [])
        self.assertEqual(story["creator_count"], 1)
        self.assertGreaterEqual(story["overlap_saved_estimate_ms"], 10.0)
        self.assertLess(story["join_wait_ms"], 50.0)

    def test_browser_bootstrap_failure_does_not_block_youtube(self):
        selected = [
            (
                {"creator_key": "alpha"},
                [
                    {"platform": "INSTAGRAM", "profile_url": "https://instagram.example/alpha"},
                    {"platform": "YOUTUBE", "profile_url": "https://youtube.example/alpha"},
                ],
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(crc.tts, "start_server", side_effect=RuntimeError("fixture down")),
                patch.object(crc, "discover_instagram") as instagram,
                patch.object(crc, "discover_youtube", return_value={
                    "creator_key": "alpha",
                    "platform": "YOUTUBE",
                    "items": [],
                    "missing_publish_time_ids": [],
                    "window_complete": True,
                    "timings": [],
                }) as youtube,
            ):
                discoveries, errors, _, _, meta = crc._run_discovery_batch(
                    Path(tmp),
                    selected,
                    datetime(2026, 9, 28, tzinfo=timezone.utc),
                    datetime(2026, 9, 29, tzinfo=timezone.utc),
                    15,
                )

        instagram.assert_not_called()
        youtube.assert_called_once()
        self.assertEqual(len(discoveries), 1)
        self.assertEqual(discoveries[0]["platform"], "YOUTUBE")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["platform"], "INSTAGRAM")
        self.assertIn("CAMOFOX_BOOTSTRAP_FAILED", errors[0]["error"])
        self.assertIn("fixture down", meta["browser_bootstrap_error"])


if __name__ == "__main__":
    unittest.main()
