from __future__ import annotations

import unittest

from creator_evaluation import _canonical_tiktok_video_url, _canonical_ytdlp_profile_target
from ephemeral_ingest import canonical_instagram_ephemeral_url
from instagram_ingest import canonical_reel_url
from influencer_evaluation import canonical_profile_url
from tiktok_ingest import canonical_tiktok_profile_url
from youtube_creator_evaluation import canonical_youtube_channel_url


class CommandLineHardeningTests(unittest.TestCase):
    def test_tiktok_profile_canonicalization(self) -> None:
        self.assertEqual(
            canonical_tiktok_profile_url("https://m.tiktok.com/@nicholas_crown"),
            "https://www.tiktok.com/@nicholas_crown",
        )
        for bad in ("--config-location=evil", "https://evil.example/@x", "https://tiktok.com.evil.test/@x"):
            with self.assertRaises(ValueError):
                canonical_tiktok_profile_url(bad)

    def test_instagram_reel_canonicalization(self) -> None:
        self.assertEqual(
            canonical_reel_url("https://m.instagram.com/reel/ABC_123/?utm_source=x"),
            "https://www.instagram.com/reel/ABC_123/",
        )
        self.assertEqual(
            canonical_reel_url(
                "https://www.instagram.com/rikatillsammans/reel/DduJ5WpiTTU/"
            ),
            "https://www.instagram.com/reel/DduJ5WpiTTU/",
        )
        with self.assertRaises(ValueError):
            canonical_reel_url("--exec=calc")

    def test_instagram_ephemeral_canonicalization(self) -> None:
        self.assertEqual(
            canonical_instagram_ephemeral_url("https://www.instagram.com/stories/example/123/?x=1"),
            "https://www.instagram.com/stories/example/123/",
        )
        with self.assertRaises(ValueError):
            canonical_instagram_ephemeral_url("https://evil.example/stories/example/123/")

    def test_youtube_channel_canonicalization(self) -> None:
        self.assertEqual(
            canonical_youtube_channel_url("https://m.youtube.com/@Example"),
            "https://www.youtube.com/@example",
        )
        with self.assertRaises(ValueError):
            canonical_youtube_channel_url("--config=evil")

    def test_tiktok_video_and_secondary_target_canonicalization(self) -> None:
        self.assertEqual(
            _canonical_tiktok_video_url(
                "https://www.tiktok.com/@example/video/123456789?x=1",
                expected_handle="example",
            ),
            "https://www.tiktok.com/@example/video/123456789",
        )
        self.assertEqual(
            _canonical_ytdlp_profile_target("tiktokuser:MS4wLjABAAAAExample_123456789", expected_handle="example"),
            "tiktokuser:MS4wLjABAAAAExample_123456789",
        )
        self.assertIsNone(_canonical_ytdlp_profile_target("--config=evil", expected_handle="example"))

    def test_orchestrator_rejects_non_platform_hosts(self) -> None:
        with self.assertRaises(ValueError):
            canonical_profile_url("https://evil.example/@x", "TIKTOK")
        with self.assertRaises(ValueError):
            canonical_profile_url("https://youtube.com.evil.test/@x", "YOUTUBE")


if __name__ == "__main__":
    unittest.main()
