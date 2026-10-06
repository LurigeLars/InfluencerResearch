from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import creator_evaluation as ce
import mcp_job_worker as worker
import youtube_creator_evaluation as yce
from creator_registry import select_evaluation_source
from evaluation_progress import heartbeat, sample_outcome, terminalize


class CreatorEvaluationPipelineTests(unittest.TestCase):
    def test_seeded_sample_includes_pins_and_fills_to_requested_unique_count(self) -> None:
        seeds = [{"id": "seed1"}, {"id": "seed2"}]
        discovered = [{"id": "seed1"}] + [{"id": f"video{i}"} for i in range(1, 25)]
        selected, duplicates = yce.select_seeded_sample(seeds, discovered, 20)
        self.assertEqual(len(selected), 20)
        self.assertEqual([x["id"] for x in selected[:2]], ["seed1", "seed2"])
        self.assertEqual(len({x["id"] for x in selected}), 20)
        self.assertEqual(duplicates, 1)

    def test_duplicate_items_do_not_consume_sample_budget(self) -> None:
        seeds = [{"id": "a"}, {"id": "b"}]
        discovered = [{"id": "a"}, {"id": "a"}, {"id": "b"}] + [
            {"id": f"x{i}"} for i in range(30)
        ]
        selected, duplicates = yce.select_seeded_sample(seeds, discovered, 20)
        self.assertEqual(len(selected), 20)
        self.assertEqual(len({x["id"] for x in selected}), 20)
        self.assertEqual(duplicates, 3)

    def test_sample_shortfall_is_explicit(self) -> None:
        complete, reason = sample_outcome(20, 2, 2, 0)
        self.assertFalse(complete)
        self.assertEqual(reason, "ONLY_2_ELIGIBLE_ITEMS_AVAILABLE")
        complete, reason = sample_outcome(20, 20, 12, 1)
        self.assertFalse(complete)
        self.assertEqual(reason, "DOWNSTREAM_PROCESSING_FAILURES")

    def test_progress_phases_and_terminal_complete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "status.json"
            path.write_text(
                json.dumps({"schema_version": 1, "state": "RUNNING", "started_at": "2026-10-06T20:00:00+00:00"}),
                encoding="utf-8",
            )
            for phase in (
                "DISCOVERY",
                "SELECTION",
                "INGESTION",
                "TRANSCRIPTION",
                "EVIDENCE",
                "QUEUE_WRITE",
                "FINALIZING",
            ):
                heartbeat(path, phase, completed_count=1)
                status = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(status["progress"]["phase"], phase)
                self.assertIn("heartbeat_at", status["progress"])
            final = terminalize(path, "COMPLETE", completed_count=1)
            self.assertEqual(final["state"], "COMPLETE")
            self.assertEqual(final["progress"]["phase"], "COMPLETE")

    def test_downstream_failure_terminalizes_failed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            argv = [
                "creator_evaluation.py",
                "--root",
                str(root),
                "--profile-url",
                "https://www.tiktok.com/@fixture",
                "--sample-size",
                "20",
                "--creator-name",
                "Fixture",
            ]

            discovery = {
                "status": "VERIFIED",
                "tiktok_handle": "fixture",
                "tiktok_profile": "https://www.tiktok.com/@fixture",
                "verified_video_urls": [],
            }

            def fake_process_source(*args, progress_callback=None, **kwargs):
                if progress_callback:
                    progress_callback("SELECTION", {"selected_count": 1})
                    progress_callback("INGESTION", {"selected_count": 1, "failed_count": 1})
                return {
                    "discovery": {"found": 1},
                    "catalog_after": 1,
                    "candidate_new": 1,
                    "completed_new": 0,
                    "failures": [{"video_id": "1", "stage": "transcription"}],
                }

            with (
                patch.object(sys, "argv", argv),
                patch.object(ce.sync, "start_server", return_value={"ok": True}),
                patch.object(ce, "discover_tiktok", return_value=discovery),
                patch.object(ce, "seed_verified_catalog", return_value={"seeded_urls": 0, "catalog_items": 0}),
                patch.object(ce.sync, "process_source", side_effect=fake_process_source),
                patch.object(ce, "mark_evaluation_items", return_value=[]),
                patch.object(ce.sync, "run_research_queue", return_value={"ok": True}),
            ):
                rc = ce.main()

            status = json.loads((root / "state" / "creator_evaluation_status.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 1)
            self.assertEqual(status["state"], "FAILED")
            self.assertEqual(status["progress"]["phase"], "FAILED")
            self.assertEqual(status["shortfall_reason"], "DOWNSTREAM_PROCESSING_FAILURES")

    def test_successful_discovery_ingestion_reaches_complete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            argv = [
                "creator_evaluation.py",
                "--root",
                str(root),
                "--profile-url",
                "https://www.tiktok.com/@fixture",
                "--sample-size",
                "2",
                "--creator-name",
                "Fixture",
            ]
            discovery = {
                "status": "VERIFIED",
                "tiktok_handle": "fixture",
                "tiktok_profile": "https://www.tiktok.com/@fixture",
                "verified_video_urls": [],
            }

            def fake_process_source(*args, progress_callback=None, **kwargs):
                if progress_callback:
                    progress_callback("SELECTION", {"selected_count": 2})
                    progress_callback("INGESTION", {"ingested_count": 2})
                    progress_callback("TRANSCRIPTION", {"transcribed_count": 2})
                    progress_callback("EVIDENCE", {"evidence_count": 2})
                return {
                    "discovery": {"found": 2},
                    "catalog_after": 2,
                    "candidate_new": 2,
                    "completed_new": 2,
                    "failures": [],
                }

            with (
                patch.object(sys, "argv", argv),
                patch.object(ce.sync, "start_server", return_value={"ok": True}),
                patch.object(ce, "discover_tiktok", return_value=discovery),
                patch.object(ce, "seed_verified_catalog", return_value={"seeded_urls": 0, "catalog_items": 0}),
                patch.object(ce.sync, "process_source", side_effect=fake_process_source),
                patch.object(ce, "mark_evaluation_items", return_value=["tt_1", "tt_2"]),
                patch.object(ce.sync, "run_research_queue", return_value={"ok": True}),
            ):
                rc = ce.main()

            status = json.loads((root / "state" / "creator_evaluation_status.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(status["state"], "COMPLETE")
            self.assertTrue(status["sample_complete"])
            self.assertEqual(status["requested_sample_size"], 2)
            self.assertEqual(status["completed_count"], 2)

    def test_no_progress_watchdog_transition_is_terminal_and_preserves_last_phase(self) -> None:
        now = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)
        stale = {
            "schema_version": 1,
            "state": "RUNNING",
            "started_at": (now - timedelta(minutes=20)).isoformat(),
            "progress": {
                "phase": "TRANSCRIPTION",
                "heartbeat_at": (now - timedelta(minutes=11)).isoformat(),
            },
        }
        failed = worker.evaluation_no_progress_failure(stale, now=now, timeout_seconds=600)
        self.assertIsNotNone(failed)
        assert failed is not None
        self.assertEqual(failed["state"], "FAILED")
        self.assertEqual(failed["error"], "NO_PROGRESS_TIMEOUT")
        self.assertEqual(failed["progress"]["last_phase"], "TRANSCRIPTION")
        self.assertEqual(failed["progress"]["terminal_reason"], "NO_PROGRESS_TIMEOUT")

        fresh = {
            **stale,
            "progress": {
                "phase": "TRANSCRIPTION",
                "heartbeat_at": (now - timedelta(minutes=2)).isoformat(),
            },
        }
        self.assertIsNone(worker.evaluation_no_progress_failure(fresh, now=now, timeout_seconds=600))

    def test_whisper_progress_callback_reports_segment_progress(self) -> None:
        class Segment:
            def __init__(self, index: int) -> None:
                self.start = float(index)
                self.end = float(index + 1)
                self.text = f"segment {index}"

        class Info:
            language = "en"
            language_probability = 1.0
            duration = 12.0

        class Model:
            def transcribe(self, *args, **kwargs):
                return ([Segment(index) for index in range(12)], Info())

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "input.mp4"
            media.write_bytes(b"fixture")
            progress: list[int] = []
            with (
                patch.object(yce, "validate_audio", return_value=(True, "ok")),
                patch.object(
                    yce,
                    "_extract_audio_for_whisper",
                    return_value=(media, {"used": False}),
                ),
            ):
                result = yce.transcribe_whisper(
                    root,
                    "fixture",
                    "video123",
                    media,
                    model_holder={"model": Model()},
                    progress_callback=progress.append,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(progress, [1, 10])

    def test_source_platform_filter_selects_requested_registered_source(self) -> None:
        profile = {
            "monitoring_enabled": True,
            "sources": [
                {
                    "platform": "YOUTUBE",
                    "enabled": True,
                    "evaluation_enabled": True,
                    "priority": 10,
                },
                {
                    "platform": "TIKTOK",
                    "enabled": True,
                    "evaluation_enabled": True,
                    "priority": 20,
                },
            ],
        }
        selected = select_evaluation_source(profile, "TIKTOK")
        self.assertEqual(selected["platform"], "TIKTOK")


if __name__ == "__main__":
    unittest.main()
