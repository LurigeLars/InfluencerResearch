from __future__ import annotations

import os
import unittest.mock
from types import SimpleNamespace

import instagram_ingest as ig


class InstagramIngestLimitTests(unittest.TestCase):
    def test_explicit_creator_override_limits_run_to_one_creator(self) -> None:
        configured = [
            {"handle": "alpha", "enabled": True},
            {"handle": "beta", "enabled": True},
        ]
        self.assertEqual(ig.resolve_creators(configured, "rikatillsammans"), ["rikatillsammans"])

    def test_configured_creators_are_used_without_override(self) -> None:
        configured = [
            {"handle": "alpha", "enabled": True},
            {"handle": "beta", "enabled": False},
        ]
        self.assertEqual(ig.resolve_creators(configured, None), ["alpha"])

    def test_max_new_override_wins(self) -> None:
        self.assertEqual(
            ig.resolve_max_new_per_creator({"max_new_per_creator": 10}, 1),
            1,
        )

    def test_max_new_rejects_non_positive_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 1"):
            ig.resolve_max_new_per_creator({"max_new_per_creator": 10}, 0)

    def test_new_only_transcription_excludes_old_pending_items(self) -> None:
        manifest = {
            "items": {
                "old": {
                    "download_status": "DONE",
                    "video_file": "output/x/old.mp4",
                    "transcription_status": "PENDING",
                },
                "new": {
                    "download_status": "DONE",
                    "video_file": "output/x/new.mp4",
                    "transcription_status": "PENDING",
                },
            }
        }
        summaries = [{"creator": "x", "new_keys": ["new"]}]
        self.assertEqual(
            ig.select_transcription_keys(manifest, summaries, new_only=True),
            ["new"],
        )

    def test_exact_shortcodes_recover_known_incomplete_items_even_in_new_only_mode(self) -> None:
        manifest = {
            "items": {
                "requested_pending": {
                    "download_status": "DONE",
                    "video_file": "output/x/requested_pending.mp4",
                    "transcription_status": "PENDING",
                },
                "requested_done": {
                    "download_status": "DONE",
                    "video_file": "output/x/requested_done.mp4",
                    "transcription_status": "DONE",
                },
                "unrelated_pending": {
                    "download_status": "DONE",
                    "video_file": "output/x/unrelated_pending.mp4",
                    "transcription_status": "PENDING",
                },
            }
        }
        self.assertEqual(
            ig.select_transcription_keys(
                manifest,
                [],
                new_only=True,
                only_shortcodes={"requested_pending", "requested_done"},
            ),
            ["requested_pending"],
        )

    def test_ephemeral_context_uses_new_context_not_persistent_profile(self) -> None:
        context = unittest.mock.Mock()
        browser = unittest.mock.Mock()
        browser.new_context.return_value = context
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch=unittest.mock.Mock(return_value=browser))
        )

        with (
            unittest.mock.patch.dict(
                os.environ,
                {"INFLUENCER_RESEARCH_CONTAINER": "1"},
                clear=False,
            ),
            unittest.mock.patch.object(ig, "load_instagram_cookies") as load_cookies,
        ):
            actual_browser, actual_context = ig.launch_instagram_ephemeral_context(
                playwright
            )

        self.assertIs(actual_browser, browser)
        self.assertIs(actual_context, context)
        playwright.chromium.launch.assert_called_once_with(headless=True)
        browser.new_context.assert_called_once_with(
            viewport={"width": 1440, "height": 1200}
        )
        load_cookies.assert_called_once_with(context)

    def test_authenticated_discovery_reuses_cached_reel_timestamp(self) -> None:
        class FakePlaywrightContext:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, tb):
                return False

        page = unittest.mock.Mock()
        context = unittest.mock.Mock()
        context.new_page.return_value = page
        browser = unittest.mock.Mock()
        reel_url = "https://www.instagram.com/reel/RECENT123/"

        with (
            unittest.mock.patch.object(
                ig,
                "sync_playwright",
                return_value=FakePlaywrightContext(),
            ),
            unittest.mock.patch.object(
                ig,
                "launch_instagram_ephemeral_context",
                return_value=(browser, context),
            ),
            unittest.mock.patch.object(ig, "verify_logged_in"),
            unittest.mock.patch.object(
                ig,
                "_wait_for_instagram_profile_ready",
                return_value={
                    "ready": True,
                    "attempts": 1,
                    "wait_ms": 10.0,
                    "blocked": False,
                    "media_auth_gated": False,
                },
            ),
            unittest.mock.patch.object(
                ig,
                "_collect_loaded_reel_urls",
                return_value=([reel_url], 1),
            ),
            unittest.mock.patch.object(ig, "_reel_published_at") as live_time,
        ):
            result = ig.discover_reels_authenticated(
                "example",
                max_scan=15,
                known_reel_times={
                    "RECENT123": "2026-09-29T15:10:53+00:00"
                },
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["reel_count"], 1)
        self.assertEqual(
            result["reel_items"][0]["published_at_source"],
            "LOCAL_MANIFEST_CACHE",
        )
        self.assertEqual(result["timings"]["reel_time_cache_hits"], 1)
        self.assertEqual(result["timings"]["reel_time_network_probes"], 0)
        live_time.assert_not_called()
        page.goto.assert_called_once_with(
            "https://www.instagram.com/example/reels/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        context.close.assert_called_once()
        browser.close.assert_called_once()

    def test_instagram_error_page_is_marked_unavailable(self) -> None:
        body = unittest.mock.Mock()
        body.inner_text.return_value = "Sorry, something went wrong"
        page = unittest.mock.Mock()
        page.url = "https://www.instagram.com/example/reels/"
        page.locator.return_value = body

        state = ig._instagram_page_access_state(page, "example")

        self.assertTrue(state["unavailable"])
        self.assertFalse(state["media_auth_gated"])
        self.assertFalse(state["blocked"])

    def test_authenticated_discovery_closes_browser_before_playwright_exit(self) -> None:
        events = []

        class FakePlaywrightContext:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, tb):
                events.append("playwright_exit")
                return False

        page = unittest.mock.Mock()
        context = unittest.mock.Mock()
        context.new_page.return_value = page
        context.close.side_effect = lambda: events.append("context_close")
        browser = unittest.mock.Mock()
        browser.close.side_effect = lambda: events.append("browser_close")

        with (
            unittest.mock.patch.object(
                ig,
                "sync_playwright",
                return_value=FakePlaywrightContext(),
            ),
            unittest.mock.patch.object(
                ig,
                "launch_instagram_ephemeral_context",
                return_value=(browser, context),
            ),
            unittest.mock.patch.object(ig, "verify_logged_in"),
            unittest.mock.patch.object(
                ig,
                "_wait_for_instagram_profile_ready",
                return_value={
                    "ready": True,
                    "attempts": 1,
                    "wait_ms": 1.0,
                    "blocked": False,
                    "media_auth_gated": False,
                    "unavailable": False,
                },
            ),
            unittest.mock.patch.object(
                ig,
                "_collect_loaded_reel_urls",
                return_value=([], 1),
            ),
        ):
            result = ig.discover_reels_authenticated("example", max_scan=15)

        self.assertTrue(result["ok"])
        self.assertEqual(
            events,
            ["context_close", "browser_close", "playwright_exit"],
        )

    def test_authenticated_discovery_fails_closed_without_session(self) -> None:
        class FakePlaywrightContext:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, tb):
                return False

        context = unittest.mock.Mock()
        browser = unittest.mock.Mock()
        with (
            unittest.mock.patch.object(
                ig,
                "sync_playwright",
                return_value=FakePlaywrightContext(),
            ),
            unittest.mock.patch.object(
                ig,
                "launch_instagram_ephemeral_context",
                return_value=(browser, context),
            ),
            unittest.mock.patch.object(
                ig,
                "verify_logged_in",
                side_effect=RuntimeError("not authenticated"),
            ),
            unittest.mock.patch.object(
                ig,
                "_collect_loaded_reel_urls",
            ) as collect,
        ):
            result = ig.discover_reels_authenticated(
                "example",
                max_scan=15,
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["authenticated"])
        self.assertTrue(result["media_auth_gated"])
        self.assertEqual(result["error"], "INSTAGRAM_SESSION_NOT_AUTHENTICATED")
        collect.assert_not_called()
        context.close.assert_called_once()
        browser.close.assert_called_once()

    def test_default_transcription_includes_all_pending_items(self) -> None:
        manifest = {
            "items": {
                "old": {
                    "download_status": "DONE",
                    "video_file": "output/x/old.mp4",
                    "transcription_status": "PENDING",
                },
                "done": {
                    "download_status": "DONE",
                    "video_file": "output/x/done.mp4",
                    "transcription_status": "DONE",
                },
            }
        }
        self.assertEqual(
            ig.select_transcription_keys(manifest, [], new_only=False),
            ["old"],
        )


if __name__ == "__main__":
    unittest.main()
