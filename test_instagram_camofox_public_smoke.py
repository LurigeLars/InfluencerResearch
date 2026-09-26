from __future__ import annotations

import unittest

import instagram_camofox_public_smoke as smoke


class InstagramPublicCamofoxSmokeTests(unittest.TestCase):
    def test_extracts_absolute_and_relative_reels(self) -> None:
        payload = {
            "a": "https://www.instagram.com/reel/ABC_123/?x=1",
            "b": ["href=/reel/XYZ-789/", '"/reel/QWE_456/"'],
        }
        self.assertEqual(
            smoke.extract_reel_urls(payload),
            [
                "https://www.instagram.com/reel/ABC_123/",
                "https://www.instagram.com/reel/XYZ-789/",
                "https://www.instagram.com/reel/QWE_456/",
            ],
        )

    def test_classifies_public_profile_without_login_block(self) -> None:
        payload = {
            "text": "RikaTillsammans public profile",
            "links": ["https://www.instagram.com/reel/ABC_123/"],
        }
        result = smoke.classify_snapshot(payload, "rikatillsammans")
        self.assertTrue(result["handle_visible"])
        self.assertEqual(result["reel_count"], 1)
        self.assertEqual(result["block_hits"], [])

    def test_detects_login_or_challenge_surface(self) -> None:
        result = smoke.classify_snapshot(
            {"text": "Log in to continue. Challenge required."},
            "rikatillsammans",
        )
        self.assertIn("log in", result["block_hits"])
        self.assertIn("challenge", result["block_hits"])


if __name__ == "__main__":
    unittest.main()
