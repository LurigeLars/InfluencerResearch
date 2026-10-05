from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import creator_monitor as cm
import creator_recent_check as crc
from video_visual_evidence import VISUAL_REVIEW_POLICY_VERSION


CROWN_IDS = ["DeGL0GllhcP", "DeFU39ajPDu", "DeDnAVFlZnt", "DeCUm_8jGHS"]
FRATERNITY_IDS = ["DeFu0TMh_sL", "DeCySsxBrDH", "Dd975WDSVax", "Dd7ayOchM-z"]


def recent_item(
    source_id: str,
    *,
    creator: str = "fixture",
    platform: str = "INSTAGRAM",
    already_ingested: bool = False,
) -> dict:
    return {
        "creator_key": creator,
        "platform": platform,
        "source_id": source_id,
        "item_key": source_id if platform == "INSTAGRAM" else (
            f"yt_{source_id}" if platform == "YOUTUBE" else f"tt_{source_id}"
        ),
        "url": f"https://www.instagram.com/reel/{source_id}/",
        "title": "",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "profile_url": "https://www.instagram.com/fixture/",
        "already_ingested": already_ingested,
    }


def write_instagram_manifest(root: Path, ids: list[str], creator: str = "fixture") -> None:
    path = root / "state" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "items": {
                    source_id: {
                        "creator": creator,
                        "source_platform": "INSTAGRAM",
                        "source_id": source_id,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "permanent_source": True,
                        "download_status": "DONE",
                        "transcription_status": "DONE",
                        "visual_review_policy_version": VISUAL_REVIEW_POLICY_VERSION,
                    }
                    for source_id in ids
                },
            }
        ),
        encoding="utf-8",
    )


class CreatorRecentLifecycleTests(unittest.TestCase):
    def test_recent_check_two_sources_new_instagram_items_reaches_complete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "state").mkdir(parents=True)
            (root / "app").mkdir(parents=True)
            (root / "state" / "manifest.json").write_text(
                '{"schema_version":1,"items":{}}',
                encoding="utf-8",
            )
            profile = {
                "creator_key": "fixture",
                "sources": [],
            }
            ig_source = {
                "platform": "INSTAGRAM",
                "profile_url": "https://www.instagram.com/fixture/",
                "enabled": True,
                "evaluation_enabled": True,
            }
            yt_source = {
                "platform": "YOUTUBE",
                "profile_url": "https://www.youtube.com/@fixture",
                "enabled": True,
                "evaluation_enabled": True,
            }
            ids = ["NEWREEL1", "NEWREEL2"]
            discoveries = [
                {
                    "creator_key": "fixture",
                    "platform": "INSTAGRAM",
                    "profile_url": ig_source["profile_url"],
                    "items": [recent_item(value) for value in ids],
                    "window_complete": True,
                    "discovery_limit_used": 15,
                },
                {
                    "creator_key": "fixture",
                    "platform": "YOUTUBE",
                    "profile_url": yt_source["profile_url"],
                    "items": [],
                    "window_complete": True,
                    "discovery_limit_used": 15,
                },
            ]
            source_map = {
                ("fixture", "INSTAGRAM"): (profile, ig_source),
                ("fixture", "YOUTUBE"): (profile, yt_source),
            }
            story_run = {
                "capture": {"reason": "NO_ACTIVE_STORY_OR_STORY_VIEW_REDIRECTED"},
                "visual_enrichment": {"errors": []},
                "timings": {},
                "state": "DONE",
            }

            def fake_discovery(*args, **kwargs):
                transition = kwargs.get("transition_callback")
                if transition is not None:
                    transition(
                        "DISCOVERY_DONE",
                        stage="DISCOVERY",
                        source_count=2,
                        error_count=0,
                        duration_ms=1.0,
                    )
                return (
                    (discoveries, [], source_map, [], {"wall_duration_ms": 1.0}),
                    {
                        "results": [{
                            "creator_key": "fixture",
                            "profile": profile,
                            "source": ig_source,
                            "run": story_run,
                            "error": None,
                            "duration_ms": 1.0,
                        }],
                        "creator_count": 1,
                        "wall_duration_ms": 1.0,
                        "discovery_duration_ms": 1.0,
                        "join_wait_ms": 0.0,
                        "overlap_saved_estimate_ms": 1.0,
                    },
                )

            def fake_launch(_root, payload, **kwargs):
                return {"payload": payload}

            def fake_wait(handle, **kwargs):
                payload = handle["payload"]
                write_instagram_manifest(root, list(payload["ids"]), creator="fixture")
                return {
                    "ok": True,
                    "operation": "INGESTION_GROUP",
                    "timeout": False,
                    "elapsed_ms": 2.0,
                    "ingestion": {
                        "requested_ids": list(payload["ids"]),
                        "completed_ids": list(payload["ids"]),
                        "returncode": 0,
                        "errors": [],
                    },
                }

            def fake_queue(_root):
                queue_path = root / "state" / "research_queue.json"
                queue_path.write_text(
                    json.dumps({
                        "items": [
                            {
                                "queue_id": source_id,
                                "creator": "fixture",
                                "source_platform": "INSTAGRAM",
                                "source_id": source_id,
                                "analysis_owner": "EKONOMI",
                                "analysis_status": "PENDING_ANALYSIS",
                                "transcript_text": "fixture transcript",
                            }
                            for source_id in ids
                        ],
                        "insufficient_content_items": [],
                        "deferred_extraction_items": [],
                        "extraction_error_items": [],
                        "pending_extraction_items": [],
                    }),
                    encoding="utf-8",
                )
                return {"ok": True}

            argv = [
                "creator_recent_check.py",
                "--root", str(root),
                "--scope", "ALL_REGISTERED",
                "--creator-keys", "fixture",
                "--window", "LAST_N_DAYS",
                "--lookback-days", "5",
                "--max-items", "5",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    crc,
                    "select_profiles_and_sources",
                    return_value=[(profile, [ig_source, yt_source])],
                ),
                patch.object(
                    crc,
                    "_run_discovery_with_story_prefetch",
                    side_effect=fake_discovery,
                ),
                patch.object(crc, "_launch_recent_worker", side_effect=fake_launch),
                patch.object(crc, "_wait_recent_worker", side_effect=fake_wait),
                patch.object(
                    crc,
                    "_ingest_instagram_stories",
                    return_value={
                        "promoted": [],
                        "available": [],
                        "reused_existing_count": 0,
                        "reattributed_count": 0,
                        "identity_aliases_retired_count": 0,
                        "conflicts": [],
                        "capture": {"reason": "NO_ACTIVE_STORY_OR_STORY_VIEW_REDIRECTED"},
                        "video_download": {},
                        "visual_enrichment": {},
                        "pipeline_timings": {},
                        "queue": None,
                        "warnings": [],
                        "state": "DONE",
                    },
                ),
                patch.object(crc.tts, "run_research_queue", side_effect=fake_queue),
                patch.object(crc.ephemeral, "initial_story_gemini_circuit", return_value={}),
                patch.object(crc.ephemeral, "get_gemini_provider_health", return_value={}),
            ):
                rc = crc._main_impl()

            status = json.loads(
                (root / "state" / "creator_recent_check_status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(rc, 0)
            self.assertEqual(status["state"], "COMPLETE")
            self.assertEqual(status["queued_for_analysis_count"], 2)
            self.assertEqual(status["ingestion_groups"][0]["completed_count"], 2)
            self.assertEqual(status["ingestion_groups"][0]["failed_count"], 0)
            events = [row["event"] for row in status["transitions"]]
            for expected in (
                "DISCOVERY_DONE",
                "SELECTION_DONE",
                "GROUP_CREATED",
                "GROUP_STARTED",
                "DOWNLOAD_DONE",
                "TRANSCRIPTION_DONE",
                "VISUAL_DONE",
                "PERSIST_DONE",
                "GROUP_DONE",
                "QUEUE_DONE",
                "JOB_FINALIZED",
            ):
                self.assertIn(expected, events)

    def test_all_items_already_ingested_creates_no_empty_group(self) -> None:
        items = [
            recent_item("A", already_ingested=True),
            recent_item("B", already_ingested=True),
        ]
        plan = crc._plan_recent_items(items, 5)
        self.assertEqual(plan["pending"], [])
        self.assertEqual(plan["selected_pending"], [])
        self.assertEqual(len(plan["grouped"]), 0)

    def test_mixed_already_ingested_and_new_items_only_schedules_new(self) -> None:
        items = [
            recent_item("KNOWN", already_ingested=True),
            recent_item("NEW1"),
            recent_item("NEW2"),
        ]
        plan = crc._plan_recent_items(items, 5)
        self.assertEqual(
            plan["grouped"][("fixture", "INSTAGRAM")],
            ["NEW1", "NEW2"],
        )

    def test_single_item_recent_check_plan_creates_one_group(self) -> None:
        plan = crc._plan_recent_items([recent_item("ONLYONE")], 5)
        self.assertEqual(len(plan["grouped"]), 1)
        self.assertEqual(
            plan["grouped"][("fixture", "INSTAGRAM")],
            ["ONLYONE"],
        )

    def test_instagram_ingestion_worker_hang_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            request = root / "request.json"
            result = root / "result.json"
            request.write_text("{}", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            handle = {
                "proc": proc,
                "request_path": request,
                "result_path": result,
                "started_clock": time.monotonic(),
                "timeout_seconds": 1,
                "operation": "INGESTION_GROUP",
            }
            outcome = crc._wait_recent_worker(handle)

            self.assertFalse(outcome["ok"])
            self.assertTrue(outcome["timeout"])
            self.assertIn("TIMEOUT", outcome["error"])
            self.assertIsNotNone(proc.poll())

    def test_transcription_or_visual_provider_failure_cannot_look_complete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "state").mkdir(parents=True)
            (root / "state" / "manifest.json").write_text(
                '{"schema_version":1,"items":{}}',
                encoding="utf-8",
            )
            summary = crc._evaluate_ingestion_group(
                root,
                creator_key="fixture",
                platform="INSTAGRAM",
                ids=["BROKEN"],
                worker_result={
                    "ok": True,
                    "timeout": False,
                    "elapsed_ms": 4.0,
                    "ingestion": {
                        "returncode": 1,
                        "failures": [{"stage": "transcription", "detail": "provider failed"}],
                    },
                },
                skipped_already_ingested_count=0,
            )
            self.assertEqual(summary["result"], "FAILED")
            final_state, readiness = crc._recent_check_final_state(
                errors=[{"stage": "INGESTION", "error": summary["terminal_reason"]}],
                discoveries=[{"creator_key": "fixture", "platform": "INSTAGRAM"}],
                deferred_extraction=[],
                extraction_errors=[],
                pending_extraction=[],
            )
            self.assertEqual(final_state, "PARTIAL")
            self.assertFalse(readiness)

    def test_nicholas_crown_fixture_real_ids_persist_complete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_instagram_manifest(root, CROWN_IDS, creator="nicholascrown")
            summary = crc._evaluate_ingestion_group(
                root,
                creator_key="nicholascrown",
                platform="INSTAGRAM",
                ids=CROWN_IDS,
                worker_result={
                    "ok": True,
                    "timeout": False,
                    "elapsed_ms": 10.0,
                    "ingestion": {"returncode": 0, "errors": []},
                },
                skipped_already_ingested_count=0,
            )
            self.assertEqual(summary["result"], "INGESTED")
            self.assertEqual(summary["completed_count"], 4)
            self.assertEqual(summary["failed_ids"], [])

    def test_trading_fraternity_fixture_real_ids_mixed_plan(self) -> None:
        items = [
            recent_item(value, creator="thetradingfraternity")
            for value in FRATERNITY_IDS
        ]
        items[-1]["already_ingested"] = True
        plan = crc._plan_recent_items(items, 5)
        self.assertEqual(
            plan["grouped"][("thetradingfraternity", "INSTAGRAM")],
            FRATERNITY_IDS[:3],
        )
        self.assertNotIn(FRATERNITY_IDS[-1], plan["selected_metadata"])

    def test_missing_persistence_is_explicit_failure_with_item_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_instagram_manifest(root, [FRATERNITY_IDS[-1]], creator="thetradingfraternity")
            summary = crc._evaluate_ingestion_group(
                root,
                creator_key="thetradingfraternity",
                platform="INSTAGRAM",
                ids=FRATERNITY_IDS[:3],
                worker_result={
                    "ok": True,
                    "timeout": False,
                    "elapsed_ms": 2.0,
                    "ingestion": {"returncode": 0, "errors": []},
                },
                skipped_already_ingested_count=1,
            )
            self.assertEqual(summary["result"], "FAILED")
            self.assertEqual(summary["terminal_reason"], "PERSISTENCE_INCOMPLETE")
            self.assertEqual(summary["failed_ids"], FRATERNITY_IDS[:3])
            self.assertEqual(summary["skipped_already_ingested_count"], 1)

    def test_recent_group_semantics_match_creator_monitor_classifier(self) -> None:
        selected = ["A", "B"]
        monitor_result = cm._classify_ingestion_result(
            selected,
            {"A"},
            had_failure=False,
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_instagram_manifest(root, ["A"])
            recent = crc._evaluate_ingestion_group(
                root,
                creator_key="fixture",
                platform="INSTAGRAM",
                ids=selected,
                worker_result={
                    "ok": True,
                    "timeout": False,
                    "elapsed_ms": 1.0,
                    "ingestion": {"returncode": 0, "errors": []},
                },
                skipped_already_ingested_count=0,
            )
        self.assertEqual(recent["result"], monitor_result[0])
        self.assertEqual(set(recent["completed_ids"]), monitor_result[1])
        self.assertEqual(recent["failed_ids"], monitor_result[2])


if __name__ == "__main__":
    unittest.main()
