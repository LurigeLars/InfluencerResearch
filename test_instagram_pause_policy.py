"""Regression checks for the operator-controlled Instagram pause."""
import os
import unittest
from unittest.mock import Mock, patch

import instagram_policy


class InstagramPausePolicyTests(unittest.TestCase):
    def test_container_fails_closed_by_default(self):
        with patch.dict(os.environ, {"INFLUENCER_RESEARCH_CONTAINER": "1"}, clear=True):
            self.assertTrue(instagram_policy.instagram_paused())

    def test_explicit_opt_in_required_to_resume(self):
        with patch.dict(os.environ, {
            "INFLUENCER_RESEARCH_CONTAINER": "1",
            instagram_policy.PAUSE_ENV: "0",
        }, clear=True):
            self.assertFalse(instagram_policy.instagram_paused())
        with patch.dict(os.environ, {instagram_policy.PAUSE_ENV: "maybe"}, clear=True):
            self.assertTrue(instagram_policy.instagram_paused())

    def test_source_filter_preserves_other_platforms(self):
        with patch.dict(os.environ, {instagram_policy.PAUSE_ENV: "1"}, clear=True):
            self.assertFalse(instagram_policy.instagram_source_allowed({"platform": "INSTAGRAM"}))
            self.assertTrue(instagram_policy.instagram_source_allowed({"platform": "YOUTUBE"}))
            self.assertTrue(instagram_policy.instagram_source_allowed({"platform": "TIKTOK"}))

    def test_authenticated_browser_does_not_start(self):
        import instagram_ingest
        import ephemeral_ingest
        mock_playwright = Mock()
        with patch.dict(os.environ, {instagram_policy.PAUSE_ENV: "1"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "INSTAGRAM_PAUSED_BY_OPERATOR"):
                instagram_ingest.launch_instagram_context(mock_playwright)
            with self.assertRaisesRegex(RuntimeError, "INSTAGRAM_PAUSED_BY_OPERATOR"):
                instagram_ingest.launch_instagram_ephemeral_context(mock_playwright)
            with self.assertRaisesRegex(RuntimeError, "INSTAGRAM_PAUSED_BY_OPERATOR"):
                ephemeral_ingest.launch_instagram_context(mock_playwright)
        mock_playwright.chromium.launch.assert_not_called()
        mock_playwright.chromium.launch_persistent_context.assert_not_called()

    def test_public_browser_never_receives_request(self):
        import instagram_camofox_public_smoke as public
        with patch.dict(os.environ, {instagram_policy.PAUSE_ENV: "1"}, clear=True):
            with patch.object(public.tts, "request_json") as mock_request:
                with self.assertRaisesRegex(RuntimeError, "INSTAGRAM_PAUSED_BY_OPERATOR"):
                    public.request_json("POST", "/tabs", {"url": "https://www.instagram.com/test/"})
                mock_request.assert_not_called()

    def test_manual_download_paths_are_blocked(self):
        import instagram_ingest
        import ephemeral_ingest
        import instagram_camofox_public_smoke as public
        with patch.dict(os.environ, {instagram_policy.PAUSE_ENV: "1"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "INSTAGRAM_PAUSED_BY_OPERATOR"):
                instagram_ingest.download_with_ytdlp(None, None, "test", "https://www.instagram.com/reel/example/")
            with self.assertRaisesRegex(RuntimeError, "INSTAGRAM_PAUSED_BY_OPERATOR"):
                ephemeral_ingest.run_ytdlp(None, None, "test", "STORY", "https://www.instagram.com/stories/test/")
            with self.assertRaisesRegex(RuntimeError, "INSTAGRAM_PAUSED_BY_OPERATOR"):
                public.ytdlp_probe("https://www.instagram.com/reel/example/")


if __name__ == "__main__":
    unittest.main()
