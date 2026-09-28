from __future__ import annotations

import sys
import unittest
from types import ModuleType

# This unit only exercises pure path/identity helpers. CI does not install the
# browser runtime, so provide the import surface needed by ephemeral_ingest.
playwright_pkg = sys.modules.setdefault("playwright", ModuleType("playwright"))
playwright_sync = sys.modules.setdefault(
    "playwright.sync_api",
    ModuleType("playwright.sync_api"),
)
if not hasattr(playwright_sync, "sync_playwright"):
    playwright_sync.sync_playwright = lambda: None
playwright_pkg.sync_api = playwright_sync

from ephemeral_ingest import extract_story_identity, invalidate_legacy_unstable_story_evidence, normalize_creator_handle
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


    def test_root_story_uses_stable_media_path_not_screenshot_hash(self) -> None:
        first = extract_story_identity(
            "https://www.instagram.com/stories/example/",
            b"first volatile screenshot",
            "https://scontent.example.net/v/t51.2885-15/abc123.jpg?token=one",
        )
        second = extract_story_identity(
            "https://www.instagram.com/stories/example/",
            b"second volatile screenshot",
            "https://scontent.example.net/v/t51.2885-15/abc123.jpg?token=two",
        )
        self.assertEqual(first, second)
        self.assertTrue(str(first[0]).startswith("media-"))
        self.assertIsNone(first[1])
        self.assertEqual(first[2], "VISIBLE_MEDIA_URL_PATH")

    def test_story_url_id_remains_primary_identity(self) -> None:
        identity = extract_story_identity(
            "https://www.instagram.com/stories/example/3995836448797052519/",
            b"screenshot",
            "https://scontent.example.net/media.jpg",
        )
        self.assertEqual(
            identity,
            ("3995836448797052519", "3995836448797052519", "STORY_URL_ID"),
        )

    def test_unresolved_story_root_fails_closed(self) -> None:
        identity = extract_story_identity(
            "https://www.instagram.com/stories/example/",
            b"volatile screenshot",
            None,
        )
        self.assertEqual(identity, (None, None, "UNRESOLVED_STORY_ROOT"))

    def test_legacy_root_frame_is_invalidated(self) -> None:
        manifest = {
            "items": {
                "STORY:example:frame-old": {
                    "source_type": "STORY",
                    "creator": "example",
                    "evidence_id": "frame-old",
                    "story_id": None,
                    "source_url": "https://www.instagram.com/stories/example/",
                    "research_status": "PENDING",
                }
            }
        }
        self.assertEqual(invalidate_legacy_unstable_story_evidence(manifest), 1)
        item = manifest["items"]["STORY:example:frame-old"]
        self.assertEqual(item["research_status"], "INVALID")
        self.assertEqual(
            item["invalid_reason"],
            "LEGACY_UNSTABLE_ROOT_STORY_IDENTITY",
        )


if __name__ == "__main__":
    unittest.main()
