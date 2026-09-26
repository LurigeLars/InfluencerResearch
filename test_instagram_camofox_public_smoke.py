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

    def test_auth_prompt_is_not_a_hard_block(self) -> None:
        result = smoke.classify_snapshot(
            {"text": "RikaTillsammans. Log in or Sign up to continue."},
            "rikatillsammans",
        )
        self.assertEqual(result["block_hits"], [])
        self.assertIn("log in", result["auth_prompt_hits"])
        self.assertIn("sign up", result["auth_prompt_hits"])

    def test_detects_challenge_as_hard_block(self) -> None:
        result = smoke.classify_snapshot(
            {"text": "RikaTillsammans. Challenge required."},
            "rikatillsammans",
        )
        self.assertIn("challenge", result["block_hits"])

    def test_detects_language_dialog_separately(self) -> None:
        result = smoke.classify_snapshot(
            {"snapshot": 'RikaTillsammans - combobox "Switch Display Language"'},
            "rikatillsammans",
        )
        self.assertTrue(result["language_dialog_visible"])
        self.assertEqual(result["block_hits"], [])

    def test_detects_swedish_language_dialog(self) -> None:
        result = smoke.classify_snapshot(
            {"snapshot": '- dialog:\n  - combobox "Byt visningsspråk"'},
            "rikatillsammans",
        )
        self.assertTrue(result["language_dialog_visible"])
        self.assertIn("byt visningsspråk", result["language_dialog_hits"])

    def test_handle_visibility_ignores_requested_url_metadata(self) -> None:
        result = smoke.classify_snapshot(
            {
                "url": "https://www.instagram.com/rikatillsammans/",
                "snapshot": '- dialog:\n  - combobox "Byt visningsspråk"',
            },
            "rikatillsammans",
        )
        self.assertFalse(result["handle_visible"])


if __name__ == "__main__":
    unittest.main()
