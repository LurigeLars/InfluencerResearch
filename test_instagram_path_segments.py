from __future__ import annotations

import unittest

from ephemeral_ingest import normalize_creator_handle
from instagram_ingest import safe_creator


class InstagramPathSegmentTests(unittest.TestCase):
    def test_valid_handles_are_preserved(self) -> None:
        for value, expected in (
            ("@nicholas_crown", "nicholas_crown"),
            ("creator.name", "creator.name"),
            ("A_B.C", "A_B.C"),
        ):
            self.assertEqual(safe_creator(value), expected)
            self.assertEqual(normalize_creator_handle(value), expected)

    def test_traversal_and_windows_device_names_are_rejected(self) -> None:
        for value in (
            ".", "..", "../x", "..\\x", "a/b", "a\\b",
            "creator.", "CON", "nul", "COM1", "LPT9.txt",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safe_creator(value)
                with self.assertRaises(ValueError):
                    normalize_creator_handle(value)


if __name__ == "__main__":
    unittest.main()
