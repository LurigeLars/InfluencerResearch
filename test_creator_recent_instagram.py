from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import creator_recent_check as crc


class RecentInstagramTests(unittest.TestCase):
    def test_registered_instagram_can_be_selected_without_registry_mutation(self) -> None:
        profile = {
            "sources": [
                {
                    "platform": "INSTAGRAM",
                    "profile_url": "https://www.instagram.com/example/",
                    "enabled": True,
                    "evaluation_enabled": False,
                    "priority": 50,
                }
            ]
        }
        self.assertEqual(crc._eligible_evaluation_sources(profile), [])
        selected = crc._eligible_evaluation_sources(
            profile,
            include_registered_instagram=True,
        )
        self.assertEqual([row["platform"] for row in selected], ["INSTAGRAM"])
        self.assertFalse(profile["sources"][0]["evaluation_enabled"])

    def test_instagram_discovery_filters_reels_by_window(self) -> None:
        end = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        cutoff = end - timedelta(days=7)
        probe = {
            "ok": True,
            "reel_count": 2,
            "blocked": False,
            "media_auth_gated": False,
            "reel_items": [
                {
                    "url": "https://www.instagram.com/reel/RECENT123/",
                    "published_at": (end - timedelta(hours=3)).isoformat(),
                    "error": None,
                },
                {
                    "url": "https://www.instagram.com/reel/OLD456/",
                    "published_at": (end - timedelta(days=10)).isoformat(),
                    "error": None,
                },
            ],
        }
        with mock.patch.object(crc.instagram_smoke, "probe_public_session", return_value=probe) as run:
            result = crc.discover_instagram(
                {"creator_key": "creator"},
                {"profile_url": "https://www.instagram.com/example/"},
                cutoff,
                end,
                15,
            )

        self.assertEqual([item["source_id"] for item in result["items"]], ["RECENT123"])
        self.assertTrue(result["window_complete"])
        run.assert_called_once_with(
            "https://www.instagram.com/example/",
            "example",
            run_index=1,
            inspect_reel_times=True,
        )

    def test_story_finalize_reuses_prefetched_capture(self) -> None:
        prefetched = {
            "capture": {"reason": "OK"},
            "visual_enrichment": {"errors": []},
            "timings": {"total_ms": 123.0},
            "state": "DONE",
            "errors": [],
        }
        bridge = {
            "promoted": [],
            "available": [],
            "reused_existing_count": 0,
            "reattributed_count": 0,
            "identity_aliases_retired_count": 0,
            "conflicts": [],
            "manifest_changed": False,
        }

        with tempfile.TemporaryDirectory() as td:
            with (
                mock.patch.object(crc, "_capture_instagram_story_run") as capture,
                mock.patch.object(crc, "_promote_story_items", return_value=bridge),
                mock.patch.object(crc.tts, "run_research_queue") as queue,
            ):
                result = crc._ingest_instagram_stories(
                    Path(td),
                    {"creator_key": "creator"},
                    {
                        "platform": "INSTAGRAM",
                        "profile_url": "https://www.instagram.com/example/",
                    },
                    datetime(2026, 9, 29, tzinfo=timezone.utc),
                    5,
                    precomputed_run=prefetched,
                )

        capture.assert_not_called()
        queue.assert_not_called()
        self.assertEqual(result["state"], "DONE")
        self.assertEqual(result["pipeline_timings"]["total_ms"], 123.0)

    def test_instagram_ingest_is_bounded_to_selected_shortcodes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app").mkdir(parents=True)
            (root / "app" / "instagram_ingest.py").write_text("# test\n", encoding="utf-8")
            (root / "state").mkdir(parents=True)

            def fake_run(cmd, **kwargs):
                manifest = {
                    "schema_version": 1,
                    "items": {
                        "REEL123": {
                            "download_status": "DONE",
                            "transcription_status": "DONE",
                        }
                    },
                }
                (root / "state" / "manifest.json").write_text(
                    json.dumps(manifest),
                    encoding="utf-8",
                )
                fake_run.cmd = cmd
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(crc.subprocess, "run", side_effect=fake_run),
                mock.patch.object(crc.tts, "run_research_queue", return_value={"ok": True}),
            ):
                result = crc._ingest_instagram(
                    root,
                    {"creator_key": "creator"},
                    {"profile_url": "https://www.instagram.com/example/"},
                    ["REEL123"],
                    {"REEL123": "2026-09-27T10:00:00+00:00"},
                )

            self.assertIn("--only-shortcodes", fake_run.cmd)
            self.assertEqual(fake_run.cmd[fake_run.cmd.index("--only-shortcodes") + 1], "REEL123")
            self.assertEqual(result["completed_ids"], ["REEL123"])
            manifest = json.loads((root / "state" / "manifest.json").read_text(encoding="utf-8"))
            item = manifest["items"]["REEL123"]
            self.assertEqual(item["creator"], "creator")
            self.assertEqual(item["source_platform"], "INSTAGRAM")
            self.assertEqual(item["source_id"], "REEL123")
            self.assertEqual(item["published_at"], "2026-09-27T10:00:00+00:00")

    def test_story_promotion_deduplicates_and_preserves_creator_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shot = root / "output" / "example" / "stories" / "screenshots" / "123.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(b"png")
            ephemeral_manifest = {
                "schema_version": 1,
                "items": {
                    "STORY:example:123": {
                        "source_type": "STORY",
                        "creator": "example",
                        "evidence_id": "123",
                        "story_id": "123",
                        "source_url": "https://www.instagram.com/stories/example/123/",
                        "observed_at": "2026-09-27T10:00:00+00:00",
                        "screenshot_file": str(shot.relative_to(root)),
                        "browser_text": "A market observation",
                        "visual_description": "Visible market text",
                        "visual_description_status": "DONE",
                        "visual_description_source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
                        "visual_description_provider": "ollama",
                        "visual_description_model": "gemma3-12b-16k",
                        "visual_description_contract": "VISIBLE_TEXT_V2",
                    }
                },
            }
            ep_path = root / "state" / "ephemeral" / "manifest.json"
            ep_path.parent.mkdir(parents=True)
            ep_path.write_text(json.dumps(ephemeral_manifest), encoding="utf-8")

            first = crc._promote_story_items(
                root,
                {"creator_key": "registered-key"},
                "example",
                datetime(2026, 9, 20, tzinfo=timezone.utc),
                5,
            )
            second = crc._promote_story_items(
                root,
                {"creator_key": "registered-key"},
                "example",
                datetime(2026, 9, 20, tzinfo=timezone.utc),
                5,
            )

            self.assertEqual(len(first["promoted"]), 1)
            self.assertEqual(len(first["available"]), 1)
            self.assertEqual(second["promoted"], [])
            self.assertEqual(len(second["available"]), 1)
            self.assertEqual(second["reused_existing_count"], 1)
            manifest = json.loads((root / "state" / "manifest.json").read_text(encoding="utf-8"))
            item = manifest["items"]["ig_story_123"]
            self.assertEqual(item["creator"], "registered-key")
            self.assertEqual(item["source_platform"], "INSTAGRAM")
            self.assertEqual(item["source_subtype"], "STORY")
            self.assertEqual(item["source_id"], "story:123")
            self.assertEqual(item["published_at_basis"], "ACTIVE_STORY_OBSERVED_AT")
            self.assertEqual(item["visual_evidence_status"], "DONE")
            self.assertEqual(item["visual_description_contract"], "VISIBLE_TEXT_V2")


    def test_existing_story_from_superseded_creator_is_reused_and_reattributed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shot = root / "output" / "example" / "stories" / "screenshots" / "123.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(b"png")

            ep_path = root / "state" / "ephemeral" / "manifest.json"
            ep_path.parent.mkdir(parents=True)
            ep_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "items": {
                            "STORY:example:123": {
                                "source_type": "STORY",
                                "creator": "example",
                                "evidence_id": "123",
                                "story_id": "123",
                                "source_url": "https://www.instagram.com/stories/example/123/",
                                "observed_at": "2026-09-27T10:00:00+00:00",
                                "screenshot_file": str(shot.relative_to(root)),
                                "browser_text": "Story evidence",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            manifest_path = root / "state" / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "items": {
                            "ig_story_123": {
                                "creator": "legacy-key",
                                "source_platform": "INSTAGRAM",
                                "source_subtype": "STORY",
                                "source_id": "story:123",
                                "url": "https://www.instagram.com/stories/example/123/",
                                "published_at": "2026-09-27T10:00:00+00:00",
                                "download_status": "DONE",
                                "transcription_status": "NOT_APPLICABLE",
                                "screenshot_file": str(shot.relative_to(root)),
                                "visual_evidence_status": "DONE",
                                "visual_frame_count": 1,
                                "research_status": "PENDING",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            registry = {
                "schema_version": 1,
                "creators": {
                    "canonical-key": {"status": "ACTIVE"},
                    "legacy-key": {
                        "status": "DISABLED",
                        "superseded_by": "canonical-key",
                    },
                },
            }
            with mock.patch.object(crc, "load_registry", return_value=registry):
                result = crc._promote_story_items(
                    root,
                    {"creator_key": "canonical-key"},
                    "example",
                    datetime(2026, 9, 20, tzinfo=timezone.utc),
                    5,
                )

            self.assertEqual(result["promoted"], [])
            self.assertEqual(len(result["available"]), 1)
            self.assertEqual(result["reused_existing_count"], 1)
            self.assertEqual(result["reattributed_count"], 1)
            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = updated["items"]["ig_story_123"]
            self.assertEqual(item["creator"], "canonical-key")
            self.assertEqual(item["creator_key_history"], ["legacy-key"])


    def test_numeric_story_reuses_root_media_alias_in_canonical_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shots = root / "output" / "example" / "stories" / "screenshots"
            shots.mkdir(parents=True)
            (shots / "media-old.png").write_bytes(b"old")
            (shots / "3996180606061318570.png").write_bytes(b"new")

            ep_path = root / "state" / "ephemeral" / "manifest.json"
            ep_path.parent.mkdir(parents=True)
            ep_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "items": {
                            "STORY:example:media-old": {
                                "source_type": "STORY",
                                "creator": "example",
                                "evidence_id": "media-old",
                                "story_id": None,
                                "story_identity_basis": "VISIBLE_MEDIA_URL_PATH",
                                "media_identity_path": "/shared/story.jpg",
                                "source_url": "https://www.instagram.com/stories/example/",
                                "observed_at": "2026-09-27T10:00:00+00:00",
                                "screenshot_file": str((shots / "media-old.png").relative_to(root)),
                                "browser_text": "example\\n1h",
                                "visual_description": "Oil battleground newsletter image",
                                "visual_description_status": "DONE",
                            },
                            "STORY:example:3996180606061318570": {
                                "source_type": "STORY",
                                "creator": "example",
                                "evidence_id": "3996180606061318570",
                                "story_id": "3996180606061318570",
                                "story_identity_basis": "STORY_URL_ID",
                                "media_identity_path": "/shared/story.jpg",
                                "source_url": "https://www.instagram.com/stories/example/3996180606061318570/",
                                "observed_at": "2026-09-27T10:05:00+00:00",
                                "screenshot_file": str((shots / "3996180606061318570.png").relative_to(root)),
                                "browser_text": "example\\n1h",
                                "visual_description": "Oil battleground newsletter image",
                                "visual_description_status": "DONE",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            manifest_path = root / "state" / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "items": {
                            "ig_story_media-old": {
                                "creator": "registered-key",
                                "source_platform": "INSTAGRAM",
                                "source_subtype": "STORY",
                                "source_id": "story:media-old",
                                "url": "https://www.instagram.com/stories/example/",
                                "published_at": "2026-09-27T10:00:00+00:00",
                                "observed_at": "2026-09-27T10:00:00+00:00",
                                "media_identity_path": "/shared/story.jpg",
                                "download_status": "DONE",
                                "transcription_status": "NOT_APPLICABLE",
                                "screenshot_file": str((shots / "media-old.png").relative_to(root)),
                                "visual_description": "Oil battleground newsletter image",
                                "visual_description_status": "DONE",
                                "research_status": "PENDING",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = crc._promote_story_items(
                root,
                {"creator_key": "registered-key"},
                "example",
                datetime(2026, 9, 20, tzinfo=timezone.utc),
                5,
            )

            self.assertEqual(result["promoted"], [])
            self.assertEqual(len(result["available"]), 1)
            self.assertEqual(result["reused_existing_count"], 1)
            self.assertEqual(result["identity_aliases_retired_count"], 1)
            self.assertEqual(
                result["available"][0]["source_id"],
                "story:3996180606061318570",
            )

            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            old = updated["items"]["ig_story_media-old"]
            self.assertEqual(old["research_status"], "INVALID")
            self.assertEqual(old["invalid_reason"], "SUPERSEDED_BY_NUMERIC_STORY_ID")
            self.assertEqual(old["superseded_by"], "ig_story_3996180606061318570")
            numeric = updated["items"]["ig_story_3996180606061318570"]
            self.assertEqual(numeric["identity_migrated_from"], ["ig_story_media-old"])
            self.assertEqual(
                numeric["published_at"],
                "2026-09-27T10:00:00+00:00",
            )
            self.assertEqual(
                numeric["visual_description"],
                "Oil battleground newsletter image",
            )


if __name__ == "__main__":
    unittest.main()
