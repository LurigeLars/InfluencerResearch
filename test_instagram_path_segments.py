from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

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

from ephemeral_ingest import backfill_story_identity_metadata, enrich_story_visual_evidence, extract_story_identity, invalidate_legacy_unstable_story_evidence, normalize_creator_handle, retire_root_media_aliases_for_numeric_story
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


    def test_numeric_story_retires_matching_root_media_alias(self) -> None:
        manifest = {
            "items": {
                "STORY:example:media-old": {
                    "source_type": "STORY",
                    "creator": "example",
                    "evidence_id": "media-old",
                    "story_id": None,
                    "media_identity_path": "/v/t51.2885-15/shared.jpg",
                    "research_status": "PENDING",
                },
                "STORY:example:other": {
                    "source_type": "STORY",
                    "creator": "example",
                    "evidence_id": "other",
                    "story_id": None,
                    "media_identity_path": "/v/t51.2885-15/other.jpg",
                    "research_status": "PENDING",
                },
            }
        }
        retired = retire_root_media_aliases_for_numeric_story(
            manifest,
            creator="example",
            source_type="STORY",
            story_id="3996180606061318570",
            media_identity_path="/v/t51.2885-15/shared.jpg",
        )
        self.assertEqual(retired, ["STORY:example:media-old"])
        old = manifest["items"]["STORY:example:media-old"]
        self.assertEqual(old["research_status"], "INVALID")
        self.assertEqual(old["invalid_reason"], "SUPERSEDED_BY_NUMERIC_STORY_ID")
        self.assertEqual(
            old["superseded_by_evidence_id"],
            "3996180606061318570",
        )
        self.assertEqual(
            manifest["items"]["STORY:example:other"]["research_status"],
            "PENDING",
        )


    def test_story_visual_enrichment_is_bounded_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {"items": {}}
            keys = []
            for index in range(3):
                shot = root / "output" / f"{index}.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                shot.write_bytes(b"png")
                key = f"STORY:example:{index}"
                keys.append(key)
                manifest["items"][key] = {
                    "source_type": "STORY",
                    "creator": "example",
                    "research_status": "PENDING",
                    "screenshot_file": str(shot.relative_to(root)),
                }

            settings = root / "control" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{}", encoding="utf-8")

            with mock.patch(
                "ephemeral_ingest.extract_image_evidence_gemini",
                return_value={
                    "text": "Readable Story evidence with enough factual detail.",
                    "source": "GEMINI_STORY_SCREENSHOT_EVIDENCE",
                    "provider": "gemini",
                    "model": "gemini-3.8-flash",
                },
            ) as extract:
                result = enrich_story_visual_evidence(
                    root,
                    manifest,
                    keys,
                    max_attempts=2,
                )

            self.assertEqual(result["attempted"], 2)
            self.assertEqual(result["completed"], 2)
            self.assertEqual(result["deferred"], 1)
            self.assertEqual(result["max_attempts"], 2)
            self.assertEqual(extract.call_count, 2)
            self.assertIsNone(
                manifest["items"][keys[2]].get("visual_description_status")
            )


    def test_story_visual_failure_records_safe_code_and_status(self) -> None:
        class FakeGeminiError(Exception):
            code = 429
            status = "RESOURCE_EXHAUSTED"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shot = root / "output" / "story.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(b"png")
            settings = root / "control" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{}", encoding="utf-8")

            key = "STORY:example:123"
            manifest = {
                "items": {
                    key: {
                        "source_type": "STORY",
                        "creator": "example",
                        "research_status": "PENDING",
                        "screenshot_file": str(shot.relative_to(root)),
                    }
                }
            }

            with mock.patch(
                "ephemeral_ingest.extract_image_evidence_gemini",
                side_effect=FakeGeminiError("secret provider body"),
            ):
                result = enrich_story_visual_evidence(
                    root,
                    manifest,
                    [key],
                    max_attempts=1,
                )

            expected = (
                "FakeGeminiError: Gemini visual evidence extraction failed "
                "code=429 status=RESOURCE_EXHAUSTED"
            )
            self.assertEqual(
                manifest["items"][key]["visual_description_error"],
                expected,
            )
            self.assertEqual(result["errors"], [f"{key}: {expected}"])
            self.assertNotIn("secret provider body", str(result))



    def test_existing_numeric_story_backfills_media_identity_without_rewriting_evidence(self) -> None:
        item = {
            "source_type": "STORY",
            "creator": "example",
            "evidence_id": "3995836448797052519",
            "story_id": "3995836448797052519",
            "source_url": "https://www.instagram.com/stories/example/3995836448797052519/",
            "observed_at": "2026-09-28T08:19:07+00:00",
            "screenshot_file": "output/example/stories/screenshots/3995836448797052519.png",
            "visual_description": "Existing readable evidence",
        }

        changed = backfill_story_identity_metadata(
            item,
            story_id="3995836448797052519",
            identity_basis="STORY_URL_ID",
            media_identity_path="/v/t51.2885-15/shared.jpg",
            source_url="https://www.instagram.com/stories/example/3995836448797052519/",
        )

        self.assertTrue(changed)
        self.assertEqual(item["story_identity_basis"], "STORY_URL_ID")
        self.assertEqual(item["media_identity_path"], "/v/t51.2885-15/shared.jpg")
        self.assertEqual(item["visual_description"], "Existing readable evidence")
        self.assertEqual(item["observed_at"], "2026-09-28T08:19:07+00:00")
        self.assertIn("identity_metadata_updated_at", item)

    def test_identity_backfill_does_not_overwrite_existing_media_path(self) -> None:
        item = {
            "story_id": "123",
            "story_identity_basis": "STORY_URL_ID",
            "media_identity_path": "/original.jpg",
            "source_url": "https://www.instagram.com/stories/example/123/",
        }

        changed = backfill_story_identity_metadata(
            item,
            story_id="123",
            identity_basis="STORY_URL_ID",
            media_identity_path="/different.jpg",
            source_url="https://www.instagram.com/stories/example/123/",
        )

        self.assertFalse(changed)
        self.assertEqual(item["media_identity_path"], "/original.jpg")


if __name__ == "__main__":
    unittest.main()
