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

import ephemeral_ingest as ephemeral
from ephemeral_ingest import backfill_story_identity_metadata, enrich_story_visual_evidence, extract_story_identity, get_gemini_provider_health, initial_story_gemini_circuit, invalidate_legacy_unstable_story_evidence, normalize_creator_handle, retire_root_media_aliases_for_numeric_story
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


    def test_story_capture_skips_redundant_wait_after_observed_advance(self) -> None:
        class FakeKeyboard:
            def __init__(self, page):
                self.page = page

            def press(self, key):
                self.page.presses.append(key)
                if self.page.url.endswith("/111/"):
                    self.page.url = "https://www.instagram.com/stories/example/222/"
                else:
                    self.page.url = "https://www.instagram.com/example/"

        class FakePage:
            def __init__(self):
                self.url = "https://www.instagram.com/stories/example/111/"
                self.waits = []
                self.presses = []
                self.keyboard = FakeKeyboard(self)

            def wait_for_timeout(self, value):
                self.waits.append(value)

            def screenshot(self, full_page=False):
                return b"frame-111" if self.url.endswith("/111/") else b"frame-222"

        page = FakePage()
        manifest = {"items": {}}

        def media_url(current_page):
            if current_page.url.endswith("/111/"):
                return "https://scontent.example.net/media/111.jpg?token=one"
            if current_page.url.endswith("/222/"):
                return "https://scontent.example.net/media/222.jpg?token=two"
            return None

        with tempfile.TemporaryDirectory() as td:
            with (
                mock.patch.object(
                    ephemeral,
                    "instagram_story_error_present",
                    return_value=False,
                ),
                mock.patch.object(
                    ephemeral,
                    "story_view_confirmation_present",
                    return_value=False,
                ),
                mock.patch.object(
                    ephemeral,
                    "dismiss_story_view_confirmation",
                    return_value=False,
                ),
                mock.patch.object(
                    ephemeral,
                    "visible_story_media_url",
                    side_effect=media_url,
                ),
                mock.patch.object(
                    ephemeral,
                    "safe_body_text",
                    return_value="story",
                ),
            ):
                result = ephemeral.capture_story_frames(
                    page=page,
                    root=Path(td),
                    manifest=manifest,
                    creator="example",
                    start_url="https://www.instagram.com/stories/example/",
                    source_type="STORY",
                    highlight_label=None,
                    max_items=5,
                    preopened=True,
                )

        self.assertEqual(result["visited_frames"], 2)
        self.assertEqual(result["captured_new"], 2)
        self.assertEqual(result["story_advance_ready_count"], 2)
        self.assertEqual(result["story_advance_timeout_count"], 0)
        self.assertEqual(page.presses, ["ArrowRight", "ArrowRight"])
        # Only the first frame needs the legacy 900 ms settle wait. Once the
        # next Story identity is observed, the next iteration starts immediately.
        self.assertEqual(page.waits, [900])

    def test_story_media_download_cache_reuses_identical_set_and_recovers_missing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "output" / "example" / "stories" / "videos" / "123.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")

            key = "STORY:example:123"
            manifest = {
                "items": {
                    key: {
                        "source_type": "STORY",
                        "creator": "example",
                        "screenshot_sha256": "abc123",
                        "source_url": "https://www.instagram.com/stories/example/123/",
                    }
                }
            }
            capture = {"visited_item_keys": [key]}
            result = {
                "ok": True,
                "files": [str(media.relative_to(root))],
            }

            ephemeral._record_story_ytdlp_success(
                root,
                "example",
                manifest,
                capture,
                result,
            )
            cached = ephemeral._story_ytdlp_cached_result(
                root,
                "example",
                manifest,
                capture,
            )
            self.assertIsNotNone(cached)
            self.assertTrue(cached["skipped"])
            self.assertEqual(
                cached["reason"],
                "UNCHANGED_STORY_SET_ALREADY_DOWNLOADED",
            )

            media.unlink()
            transcript = (
                root
                / "output"
                / "example"
                / "stories"
                / "transcripts"
                / "123.txt"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text("transcript", encoding="utf-8")
            self.assertIsNotNone(
                ephemeral._story_ytdlp_cached_result(
                    root,
                    "example",
                    manifest,
                    capture,
                )
            )

            transcript.unlink()
            self.assertIsNone(
                ephemeral._story_ytdlp_cached_result(
                    root,
                    "example",
                    manifest,
                    capture,
                )
            )

            media.write_bytes(b"video")
            manifest["items"][key]["screenshot_sha256"] = "changed"
            self.assertIsNone(
                ephemeral._story_ytdlp_cached_result(
                    root,
                    "example",
                    manifest,
                    capture,
                )
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
            self.assertEqual(
                manifest["items"][keys[2]]["visual_description_status"],
                "DEFERRED",
            )
            self.assertEqual(
                manifest["items"][keys[2]]["visual_description_deferred_reason"],
                "PER_RUN_BUDGET",
            )


    def test_story_ocr_insufficient_cache_reuses_text_and_invalidates_on_pixel_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shot = root / "output" / "story.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(b"first-pixels")

            settings = root / "control" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                '{"transcription":{"ollama_visual_enabled":false}}',
                encoding="utf-8",
            )

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
            circuit = {
                "open": True,
                "reason": "PROVIDER_UNAVAILABLE",
                "retry_after": None,
                "retry_after_source": "TEST",
            }

            with mock.patch(
                "ephemeral_ingest.extract_story_text_local_ocr",
                return_value={
                    "text": "x",
                    "source": "LOCAL_OCR",
                    "provider": "tesseract",
                    "model": "eng+swe",
                },
            ) as ocr:
                first = enrich_story_visual_evidence(
                    root,
                    manifest,
                    [key],
                    circuit_state=dict(circuit),
                )
                second = enrich_story_visual_evidence(
                    root,
                    manifest,
                    [key],
                    circuit_state=dict(circuit),
                )

                shot.write_bytes(b"changed-pixels")
                third = enrich_story_visual_evidence(
                    root,
                    manifest,
                    [key],
                    circuit_state=dict(circuit),
                )

            self.assertEqual(first["ocr_attempted"], 1)
            self.assertEqual(first["ocr_cached_insufficient"], 0)
            self.assertEqual(second["ocr_attempted"], 0)
            self.assertEqual(second["ocr_cached_insufficient"], 1)
            self.assertEqual(third["ocr_attempted"], 1)
            self.assertEqual(third["ocr_cached_insufficient"], 0)
            self.assertEqual(ocr.call_count, 2)
            self.assertEqual(
                manifest["items"][key]["ocr_visual_attempt_text"],
                "x",
            )

    def test_story_gemini_adaptive_backoff_grows_and_caps_without_retry_after(self) -> None:
        health = {}
        observed = []
        for _ in range(6):
            cooldown, streak = ephemeral.story_gemini_adaptive_cooldown_seconds(
                health,
                base_seconds=60,
            )
            observed.append(cooldown)
            health["consecutive_transient_failures"] = streak

        self.assertEqual(observed, [60, 120, 240, 480, 900, 900])

    def test_story_gemini_provider_retry_after_remains_authoritative(self) -> None:
        cooldown, streak = ephemeral.story_gemini_adaptive_cooldown_seconds(
            {"consecutive_transient_failures": 7},
            base_seconds=300,
            provider_retry_seconds=42,
        )
        self.assertEqual(cooldown, 42)
        self.assertEqual(streak, 8)

    def test_story_gemini_success_resets_transient_failure_streak(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = ephemeral.update_gemini_provider_health(
                root,
                calls=1,
                deferred=1,
                error_meta={"code": 503, "status": "UNAVAILABLE"},
                cooldown_until=ephemeral.datetime.now(ephemeral.timezone.utc)
                + ephemeral.timedelta(seconds=60),
                retry_after_source="ADAPTIVE_BACKOFF",
                transient_failure=True,
                cooldown_seconds=60,
            )
            self.assertEqual(first["consecutive_transient_failures"], 1)
            self.assertEqual(first["last_cooldown_seconds"], 60)

            second = ephemeral.update_gemini_provider_health(
                root,
                calls=1,
                successes=1,
            )
            self.assertEqual(second["consecutive_transient_failures"], 0)
            self.assertEqual(second["last_cooldown_seconds"], 0)

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
            item = manifest["items"][key]
            self.assertEqual(item["visual_description_error"], expected)
            self.assertEqual(item["visual_description_status"], "DEFERRED")
            self.assertEqual(
                item["visual_description_deferred_reason"],
                "PROVIDER_RATE_LIMIT",
            )
            self.assertIn("visual_description_retry_after", item)
            self.assertTrue(result["provider_circuit_breaker"]["open"])
            self.assertEqual(
                result["provider_circuit_breaker"]["reason"],
                "PROVIDER_RATE_LIMIT",
            )
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["provider_events"], [f"{key}: {expected}"])
            self.assertNotIn("secret provider body", str(result))



    def test_story_rate_limit_opens_circuit_breaker_for_remaining_items(self) -> None:
        class FakeGeminiError(Exception):
            code = 429
            status = "RESOURCE_EXHAUSTED"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings = root / "control" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{}", encoding="utf-8")
            manifest = {"items": {}}
            keys = []
            for index in range(3):
                shot = root / "output" / f"rate-{index}.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                shot.write_bytes(b"png")
                key = f"STORY:example:rate-{index}"
                keys.append(key)
                manifest["items"][key] = {
                    "source_type": "STORY",
                    "creator": "example",
                    "research_status": "PENDING",
                    "screenshot_file": str(shot.relative_to(root)),
                }

            with mock.patch(
                "ephemeral_ingest.extract_image_evidence_gemini",
                side_effect=FakeGeminiError("provider quota body"),
            ) as extract:
                result = enrich_story_visual_evidence(
                    root,
                    manifest,
                    keys,
                    max_attempts=3,
                )

            self.assertEqual(extract.call_count, 1)
            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["deferred"], 3)
            self.assertEqual(result["provider_deferred"], 3)
            self.assertEqual(result["errors"], [])
            self.assertEqual(len(result["provider_events"]), 1)
            for key in keys:
                self.assertEqual(
                    manifest["items"][key]["visual_description_status"],
                    "DEFERRED",
                )
                self.assertEqual(
                    manifest["items"][key]["visual_description_deferred_reason"],
                    "PROVIDER_RATE_LIMIT",
                )


    def test_story_rate_limit_circuit_is_shared_across_creator_runs(self) -> None:
        class FakeResponse:
            headers = {"retry-after": "42"}

        class FakeGeminiError(Exception):
            code = 429
            status = "RESOURCE_EXHAUSTED"
            response = FakeResponse()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings = root / "control" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{}", encoding="utf-8")
            circuit = {"open": False, "reason": None, "retry_after": None, "retry_after_source": None}

            manifests = []
            keys = []
            for creator in ("first", "second"):
                shot = root / "output" / creator / "story.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                shot.write_bytes(b"png")
                key = f"STORY:{creator}:1"
                keys.append(key)
                manifests.append({
                    "items": {
                        key: {
                            "source_type": "STORY",
                            "creator": creator,
                            "research_status": "PENDING",
                            "screenshot_file": str(shot.relative_to(root)),
                        }
                    }
                })

            with mock.patch(
                "ephemeral_ingest.extract_image_evidence_gemini",
                side_effect=FakeGeminiError("provider body"),
            ) as extract:
                first = enrich_story_visual_evidence(
                    root,
                    manifests[0],
                    [keys[0]],
                    max_attempts=2,
                    circuit_state=circuit,
                )
                second = enrich_story_visual_evidence(
                    root,
                    manifests[1],
                    [keys[1]],
                    max_attempts=2,
                    circuit_state=circuit,
                )

            self.assertEqual(extract.call_count, 1)
            self.assertTrue(circuit["open"])
            self.assertEqual(circuit["reason"], "PROVIDER_RATE_LIMIT")
            self.assertEqual(circuit["retry_after_source"], "PROVIDER_RETRY_AFTER")
            self.assertEqual(first["provider_circuit_breaker"]["retry_after_source"], "PROVIDER_RETRY_AFTER")
            self.assertEqual(second["attempted"], 0)
            self.assertEqual(second["provider_deferred"], 1)
            self.assertEqual(
                manifests[1]["items"][keys[1]]["visual_description_status"],
                "DEFERRED",
            )

            health = get_gemini_provider_health(root)
            self.assertEqual(health["last_code"], 429)
            self.assertEqual(health["last_status"], "RESOURCE_EXHAUSTED")
            self.assertEqual(health["retry_after_source"], "PROVIDER_RETRY_AFTER")
            self.assertEqual(health["story_visual_calls"], 1)
            self.assertEqual(health["story_visual_deferred"], 2)
            self.assertIn("last_429_at", health)
            self.assertIn("cooldown_until", health)

    def test_persisted_cooldown_opens_next_job_circuit(self) -> None:
        class FakeResponse:
            headers = {"retry-after": "120"}

        class FakeGeminiError(Exception):
            code = 429
            status = "RESOURCE_EXHAUSTED"
            response = FakeResponse()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings = root / "control" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{}", encoding="utf-8")
            shot = root / "output" / "creator" / "story.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(b"png")
            key = "STORY:creator:1"
            manifest = {
                "items": {
                    key: {
                        "source_type": "STORY",
                        "creator": "creator",
                        "research_status": "PENDING",
                        "screenshot_file": str(shot.relative_to(root)),
                    }
                }
            }

            with mock.patch(
                "ephemeral_ingest.extract_image_evidence_gemini",
                side_effect=FakeGeminiError("hidden"),
            ):
                enrich_story_visual_evidence(root, manifest, [key], max_attempts=1)

            next_circuit = initial_story_gemini_circuit(root)
            self.assertTrue(next_circuit["open"])
            self.assertEqual(next_circuit["reason"], "PROVIDER_RATE_LIMIT")
            self.assertEqual(next_circuit["retry_after_source"], "PROVIDER_RETRY_AFTER")

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
