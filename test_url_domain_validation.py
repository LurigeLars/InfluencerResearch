from __future__ import annotations

import unittest

from creator_evaluation import extract_handle
from influencer_evaluation import is_youtube


class UrlDomainValidationTests(unittest.TestCase):
    def test_extract_handle_accepts_expected_instagram_hosts(self) -> None:
        self.assertEqual(extract_handle("https://instagram.com/example"), ("INSTAGRAM", "example"))
        self.assertEqual(extract_handle("https://www.instagram.com/@example/"), ("INSTAGRAM", "example"))
        self.assertEqual(extract_handle("https://m.instagram.com/example"), ("INSTAGRAM", "example"))

    def test_extract_handle_rejects_lookalike_instagram_hosts(self) -> None:
        self.assertEqual(extract_handle("https://evilinstagram.com/example"), (None, None))
        self.assertEqual(extract_handle("https://instagram.com.evil.test/example"), (None, None))

    def test_extract_handle_accepts_expected_tiktok_hosts(self) -> None:
        self.assertEqual(extract_handle("https://tiktok.com/@example"), ("TIKTOK", "example"))
        self.assertEqual(extract_handle("https://www.tiktok.com/@example"), ("TIKTOK", "example"))

    def test_extract_handle_rejects_lookalike_tiktok_hosts(self) -> None:
        self.assertEqual(extract_handle("https://eviltiktok.com/@example"), (None, None))
        self.assertEqual(extract_handle("https://tiktok.com.evil.test/@example"), (None, None))

    def test_is_youtube_accepts_expected_hosts(self) -> None:
        self.assertTrue(is_youtube("https://youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube("https://m.youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube("https://youtu.be/abc"))

    def test_is_youtube_rejects_lookalike_hosts(self) -> None:
        self.assertFalse(is_youtube("https://evilyoutube.com/watch?v=abc"))
        self.assertFalse(is_youtube("https://youtube.com.evil.test/watch?v=abc"))
        self.assertFalse(is_youtube("https://evilyoutu.be/abc"))


if __name__ == "__main__":
    unittest.main()