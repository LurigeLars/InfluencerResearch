from __future__ import annotations

import importlib.util
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
        with mock.patch.object(crc.instagram, "discover_reels_authenticated", return_value=probe) as run:
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
            "example",
            max_scan=15,
            known_reel_times={},
        )

    def test_media_auth_gate_with_short_result_is_not_complete_coverage(self) -> None:
        end = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)
        cutoff = end - timedelta(days=1)
        probe = {
            "ok": True,
            "reel_count": 0,
            "blocked": False,
            "media_auth_gated": True,
            "reel_items": [],
            "timings": {},
        }
        with mock.patch.object(
            crc.instagram,
            "discover_reels_authenticated",
            return_value=probe,
        ):
            result = crc.discover_instagram(
                {"creator_key": "creator"},
                {"profile_url": "https://www.instagram.com/example/"},
                cutoff,
                end,
                15,
            )

        self.assertFalse(result["window_complete"])
        self.assertEqual(result["coverage_limited_reason"], "MEDIA_AUTH_GATE")

    def test_hard_block_is_not_complete_coverage(self) -> None:
        end = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)
        cutoff = end - timedelta(days=1)
        probe = {
            "ok": False,
            "reel_count": 0,
            "blocked": True,
            "media_auth_gated": False,
            "reel_items": [],
            "timings": {},
        }
        with mock.patch.object(
            crc.instagram,
            "discover_reels_authenticated",
            return_value=probe,
        ):
            result = crc.discover_instagram(
                {"creator_key": "creator"},
                {"profile_url": "https://www.instagram.com/example/"},
                cutoff,
                end,
                15,
            )

        self.assertFalse(result["window_complete"])
        self.assertEqual(result["coverage_limited_reason"], "HARD_BLOCK")

    def test_instagram_discovery_error_is_not_complete_coverage(self) -> None:
        end = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)
        cutoff = end - timedelta(days=1)
        probe = {
            "ok": False,
            "authenticated": True,
            "reel_count": 0,
            "blocked": False,
            "media_auth_gated": False,
            "reel_items": [],
            "error": "TimeoutError: profile navigation timed out",
            "timings": {},
        }
        with mock.patch.object(
            crc.instagram,
            "discover_reels_authenticated",
            return_value=probe,
        ):
            result = crc.discover_instagram(
                {"creator_key": "creator"},
                {"profile_url": "https://www.instagram.com/example/"},
                cutoff,
                end,
                15,
            )

        self.assertFalse(result["window_complete"])
        self.assertEqual(result["coverage_limited_reason"], "DISCOVERY_ERROR")
        self.assertEqual(
            result["discovery"]["error"],
            "TimeoutError: profile navigation timed out",
        )

    def test_instagram_discovery_reuses_cached_publish_times(self) -> None:
        end = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)
        cutoff = end - timedelta(days=1)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            state.mkdir(parents=True)
            (state / "manifest.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "items": {
                        "RECENT123": {
                            "source_platform": "INSTAGRAM",
                            "source_id": "RECENT123",
                            "published_at": "2026-09-29T15:10:53+00:00",
                            "download_status": "DONE",
                            "transcription_status": "DONE",
                        },
                        "ig_story_999": {
                            "source_platform": "INSTAGRAM",
                            "source_subtype": "STORY",
                            "source_id": "story:999",
                            "published_at": "2026-09-29T16:00:00+00:00",
                        },
                    },
                }),
                encoding="utf-8",
            )
            probe = {
                "ok": True,
                "reel_count": 1,
                "blocked": False,
                "media_auth_gated": False,
                "reel_items": [{
                    "url": "https://www.instagram.com/reel/RECENT123/",
                    "published_at": "2026-09-29T15:10:53+00:00",
                    "error": None,
                }],
                "timings": {"reel_time_cache_hits": 1},
            }
            with mock.patch.object(
                crc.instagram,
                "discover_reels_authenticated",
                return_value=probe,
            ) as run:
                result = crc.discover_instagram(
                    {"creator_key": "creator"},
                    {"profile_url": "https://www.instagram.com/example/"},
                    cutoff,
                    end,
                    15,
                    root=root,
                )

        self.assertEqual(result["items"][0]["source_id"], "RECENT123")
        run.assert_called_once_with(
            "example",
            max_scan=15,
            known_reel_times={"RECENT123": "2026-09-29T15:10:53+00:00"},
        )

    def test_public_probe_uses_cached_reel_time_without_navigation(self) -> None:
        module_path = Path(__file__).with_name("instagram_camofox_public_smoke.py")
        spec = importlib.util.spec_from_file_location(
            "instagram_camofox_public_smoke_perf_test",
            module_path,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)

        reel_url = "https://www.instagram.com/reel/RECENT123/"
        initial_dom = {
            "reel_links": [reel_url],
            "media_links": [reel_url],
            "cookie_consent_visible": False,
            "media_auth_gate_visible": False,
            "handle_visible": True,
            "body_text_length": 1000,
        }
        classified = {
            "handle_visible": True,
            "reels": [reel_url],
            "block_hits": [],
            "auth_prompt_hits": [],
            "language_dialog_visible": False,
            "language_dialog_hits": [],
            "snapshot_excerpt": "",
        }
        calls = []

        def fake_request(method, path, body=None, timeout=30):
            calls.append((method, path))
            if method == "POST" and path == "/tabs":
                return {"tabId": "tab-1"}
            if method == "GET" and "/snapshot?" in path:
                return {}
            if method == "GET" and "/links?" in path:
                return {}
            return {"ok": True}

        with (
            mock.patch.object(
                smoke,
                "_wait_for_profile_ready",
                return_value={
                    "ready": True,
                    "attempts": 1,
                    "wait_ms": 12.0,
                    "dom": initial_dom,
                    "last_error": None,
                },
            ),
            mock.patch.object(
                smoke,
                "classify_snapshot",
                return_value=classified,
            ),
            mock.patch.object(
                smoke,
                "request_json",
                side_effect=fake_request,
            ),
        ):
            result = smoke.probe_public_session(
                "https://www.instagram.com/example/",
                "example",
                1,
                inspect_reel_times=True,
                known_reel_times={"RECENT123": "2026-09-29T15:10:53+00:00"},
            )

        self.assertEqual(
            result["reel_items"][0]["published_at_source"],
            "LOCAL_MANIFEST_CACHE",
        )
        self.assertEqual(result["timings"]["reel_time_cache_hits"], 1)
        self.assertEqual(result["timings"]["reel_time_network_probes"], 0)
        self.assertFalse(any("/navigate" in path for _, path in calls))

    def test_public_probe_does_not_scroll_after_final_empty_round(self) -> None:
        module_path = Path(__file__).with_name("instagram_camofox_public_smoke.py")
        spec = importlib.util.spec_from_file_location(
            "instagram_camofox_public_smoke_final_round_test",
            module_path,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)

        initial_dom = {
            "reel_links": [],
            "media_links": [],
            "cookie_consent_visible": False,
            "media_auth_gate_visible": False,
            "handle_visible": True,
            "body_text_length": 1000,
        }
        classified = {
            "handle_visible": True,
            "reels": [],
            "block_hits": [],
            "auth_prompt_hits": [],
            "language_dialog_visible": False,
            "language_dialog_hits": [],
            "snapshot_excerpt": "",
        }
        calls = []

        def fake_request(method, path, body=None, timeout=30):
            calls.append((method, path))
            if method == "POST" and path == "/tabs":
                return {"tabId": "tab-1"}
            if method == "GET" and "/snapshot?" in path:
                return {}
            if method == "GET" and "/links?" in path:
                return {}
            return {"ok": True}

        with (
            mock.patch.object(
                smoke,
                "_wait_for_profile_ready",
                return_value={
                    "ready": True,
                    "attempts": 1,
                    "wait_ms": 10.0,
                    "dom": initial_dom,
                    "last_error": None,
                },
            ),
            mock.patch.object(smoke, "dom_probe", return_value=initial_dom),
            mock.patch.object(smoke, "classify_snapshot", return_value=classified),
            mock.patch.object(smoke, "request_json", side_effect=fake_request),
            mock.patch.object(smoke.time, "sleep") as sleep,
        ):
            result = smoke.probe_public_session(
                "https://www.instagram.com/example/",
                "example",
                1,
                inspect_reel_times=True,
            )

        scrolls = [
            path
            for method, path in calls
            if method == "POST" and path.endswith("/scroll")
        ]
        self.assertEqual(len(scrolls), 2)
        self.assertEqual(result["timings"]["discovery_rounds"], 3)
        self.assertEqual(result["reel_count"], 0)
        self.assertEqual(sleep.call_count, 2)

    def test_instagram_profile_readiness_exits_without_blind_sleep(self) -> None:
        module_path = Path(__file__).with_name("instagram_camofox_public_smoke.py")
        spec = importlib.util.spec_from_file_location(
            "instagram_camofox_public_smoke_ready_test",
            module_path,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)

        with (
            mock.patch.object(
                smoke,
                "dom_probe",
                return_value={
                    "reel_links": ["https://www.instagram.com/reel/RECENT123/"],
                    "media_links": ["https://www.instagram.com/reel/RECENT123/"],
                    "cookie_consent_visible": False,
                    "media_auth_gate_visible": False,
                    "handle_visible": True,
                    "body_text_length": 1200,
                },
            ) as probe,
            mock.patch.object(smoke.time, "sleep") as sleep,
        ):
            result = smoke._wait_for_profile_ready("tab-1", "user-1", "example")

        self.assertTrue(result["ready"])
        self.assertEqual(result["attempts"], 1)
        probe.assert_called_once()
        sleep.assert_not_called()

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
