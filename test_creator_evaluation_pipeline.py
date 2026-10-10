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


class YoutubeCaptionDeduplicationTests(unittest.TestCase):
    def test_rolling_vtt_cues_do_not_triple_words(self) -> None:
        rows = [
            {"start": 0, "end": 1, "text": "The experience of watching at home I think you"},
            {"start": 0.5, "end": 2, "text": "experience of watching at home I think you know you're watching"},
            {"start": 1.8, "end": 3, "text": "you know you're watching in a room and the lights are on"},
            {"start": 3, "end": 4, "text": "the lights are on but the kids are running around"},
        ]
        self.assertEqual(
            yce.render_caption_transcript(rows),
            "The experience of watching at home I think you know you're watching "
            "in a room and the lights are on but the kids are running around",
        )

    def test_triplicate_phrase_within_caption_is_collapsed(self) -> None:
        phrase = "People watch at home because screens are cheap"
        rows = [{"start": 0, "end": 3, "text": " ".join([phrase] * 3)}]
        self.assertEqual(yce.render_caption_transcript(rows), phrase)

    def test_distant_or_short_legitimate_repetitions_are_preserved(self) -> None:
        rows = [
            {"start": 0, "end": 1, "text": "Yes yes yes I agree"},
            {"start": 10, "end": 12, "text": "Yes yes yes I agree"},
        ]
        self.assertEqual(
            yce.render_caption_transcript(rows),
            "Yes yes yes I agree Yes yes yes I agree",
        )


class CreatorEvaluationPipelineTests(unittest.TestCase):
    def test_youtube_visual_capture_default_ceiling_is_768_mib_and_bounded(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                yce.visual_capture_max_child_rss_bytes(),
                768 * 1024 * 1024,
            )
        with patch.dict(
            "os.environ",
            {"INFLUENCER_RESEARCH_YOUTUBE_VISUAL_CAPTURE_MAX_CHILD_RSS_MB": "9999"},
            clear=True,
        ):
            self.assertEqual(
                yce.visual_capture_max_child_rss_bytes(),
                1024 * 1024 * 1024,
            )

    def test_runtime_image_provides_pinned_node_for_ytdlp(self) -> None:
        dockerfile = (
            Path(__file__).resolve().parent
            / "runtime"
            / "influencerresearch"
            / "Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("FROM node:26.10.0-trixie-slim AS node-runtime", dockerfile)
        self.assertIn(
            "COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node",
            dockerfile,
        )
        self.assertIn('test "$(node --version)" = "v26.10.0"', dockerfile)

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

    def test_unpinned_longform_is_deferred_and_later_items_backfill_sample(self) -> None:
        entries = [
            {"id": "long001", "url": "https://www.youtube.com/watch?v=long001"},
            {"id": "short02", "url": "https://www.youtube.com/watch?v=short02"},
            {"id": "short03", "url": "https://www.youtube.com/watch?v=short03"},
        ]

        def fake_preflight(root, creator_key, entry, **kwargs):
            if entry["id"] == "long001":
                return None, {
                    "video_id": "long001",
                    "reason": "UNPINNED_LONGFORM_NO_CAPTIONS",
                }
            return dict(entry), None

        with patch.object(yce, "preflight_evidence_cost", side_effect=fake_preflight):
            selected, deferred, cursor = yce.select_bounded_evidence_sample(
                Path("."),
                "fixture",
                entries,
                2,
                must_include_ids=set(),
                channel_url="https://www.youtube.com/@fixture",
                required_attribution_term="",
                is_complete_entry=lambda entry: False,
            )

        self.assertEqual([entry["id"] for entry in selected], ["short02", "short03"])
        self.assertEqual(deferred[0]["reason"], "UNPINNED_LONGFORM_NO_CAPTIONS")
        self.assertEqual(cursor, 3)

    def test_pinned_longform_bypasses_evidence_cost_defer(self) -> None:
        entry = {
            "id": "long001",
            "url": "https://www.youtube.com/watch?v=long001",
            "duration_seconds": 7200,
            "caption_track_available": False,
        }
        with patch.object(yce, "probe_exact_video", side_effect=AssertionError("must not probe pinned seed")):
            accepted, deferred = yce.preflight_evidence_cost(
                Path("."),
                "fixture",
                entry,
                channel_url="https://www.youtube.com/@fixture",
                required_attribution_term="",
                must_include=True,
            )
        self.assertIsNone(deferred)
        self.assertEqual(accepted["id"], "long001")
        self.assertTrue(accepted["must_include_seed"])

    def test_unpinned_longform_without_captions_is_deferred_before_whisper(self) -> None:
        entry = {
            "id": "long001",
            "url": "https://www.youtube.com/watch?v=long001",
            "duration_seconds": 7200,
            "caption_track_available": False,
        }
        verified = {
            **entry,
            "title": "Fixture long form",
            "live_status": "was_live",
        }
        with (
            patch.object(yce, "probe_exact_video", return_value=(verified, {"ok": True})),
            patch.object(yce, "fetch_captions", side_effect=AssertionError("no caption fetch when metadata proves absent")),
        ):
            accepted, deferred = yce.preflight_evidence_cost(
                Path("."),
                "fixture",
                entry,
                channel_url="https://www.youtube.com/@fixture",
                required_attribution_term="",
                must_include=False,
            )
        self.assertIsNone(accepted)
        self.assertEqual(deferred["reason"], "UNPINNED_LONGFORM_NO_CAPTIONS")
        self.assertEqual(deferred["duration_seconds"], 7200.0)

    def test_completed_youtube_items_are_queued_before_later_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entries = [
                {"id": "vid001", "url": "https://www.youtube.com/watch?v=vid001", "title": "One"},
                {"id": "vid002", "url": "https://www.youtube.com/watch?v=vid002", "title": "Two"},
                {"id": "vid003", "url": "https://www.youtube.com/watch?v=vid003", "title": "Three"},
            ]
            argv = [
                "youtube_creator_evaluation.py",
                "--root", str(root),
                "--channel-url", "https://www.youtube.com/@fixture",
                "--creator-key", "fixture",
                "--creator-name", "Fixture",
                "--sample-size", "3",
                "--only-video-ids", "vid001,vid002,vid003",
            ]

            visual_calls = {"count": 0}

            def fake_visual(
                root_arg,
                creator_key,
                url,
                video_id,
                *,
                duration_seconds=None,
                progress_callback=None,
            ):
                visual_calls["count"] += 1
                if video_id == "vid003":
                    raise KeyboardInterrupt("simulated cancellation")
                index = root_arg / "output" / creator_key / "youtube" / "frames" / video_id / "visual_index.json"
                index.parent.mkdir(parents=True, exist_ok=True)
                index.write_text("{}\n", encoding="utf-8")
                return {
                    "ok": True,
                    "index": index,
                    "retained_frames": 1,
                    "capture_strategy": "FIXTURE",
                    "agent_visual_bundle": {},
                }

            def fake_captions(root_arg, creator_key, url, video_id):
                transcript_dir = root_arg / "output" / creator_key / "youtube" / "transcripts"
                transcript_dir.mkdir(parents=True, exist_ok=True)
                txt = transcript_dir / f"{video_id}.txt"
                js = transcript_dir / f"{video_id}.json"
                txt.write_text(f"fixture transcript for {video_id}\n", encoding="utf-8")
                js.write_text("{}\n", encoding="utf-8")
                return {
                    "ok": True,
                    "source": "YOUTUBE_CAPTIONS",
                    "txt": txt,
                    "json": js,
                    "transcribed_at": "2026-10-07T00:00:00+00:00",
                    "caption_file": None,
                }

            def fake_reconcile(root_arg, targets):
                queue_path = root_arg / "state" / "research_queue.json"
                if queue_path.exists():
                    queue = json.loads(queue_path.read_text(encoding="utf-8"))
                else:
                    queue = {"items": []}
                existing = {
                    str(item.get("shortcode") or item.get("queue_id") or "")
                    for item in queue["items"]
                }
                dispositions = {}
                for target in targets:
                    if target not in existing:
                        queue["items"].append({
                            "queue_id": target,
                            "shortcode": target,
                            "creator": "fixture",
                            "source_platform": "YOUTUBE",
                            "analysis_status": "PENDING_ANALYSIS",
                        })
                        existing.add(target)
                    dispositions[target] = "QUEUED"
                queue["count"] = len(queue["items"])
                queue_path.parent.mkdir(parents=True, exist_ok=True)
                queue_path.write_text(json.dumps(queue), encoding="utf-8")
                return (
                    {"ok": True, "returncode": 0},
                    queue,
                    {
                        "dispositions": dispositions,
                        "expected_queue": list(targets),
                        "undelivered": [],
                    },
                )

            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    yce,
                    "exact_video_entries",
                    return_value=(entries, {"mode": "DIRECT_EXACT_ID_ALLOWLIST", "ok": True}),
                ),
                patch.object(yce, "capture_visual_evidence", side_effect=fake_visual),
                patch.object(yce, "fetch_captions", side_effect=fake_captions),
                patch.object(
                    yce,
                    "read_info_json",
                    side_effect=lambda root_arg, creator_key, video_id: {
                        "title": video_id,
                        "timestamp": 1791331200,
                    },
                ),
                patch.object(yce, "reconcile_delivery", side_effect=fake_reconcile),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    yce.main()

            manifest = json.loads((root / "state" / "manifest.json").read_text(encoding="utf-8"))
            queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
            self.assertIn("yt_vid001", manifest["items"])
            self.assertIn("yt_vid002", manifest["items"])
            queued = {item["shortcode"] for item in queue["items"]}
            self.assertIn("yt_vid001", queued)
            self.assertIn("yt_vid002", queued)
            self.assertNotIn("yt_vid003", queued)
            self.assertEqual(visual_calls["count"], 3)
            status = json.loads(
                (root / "state" / "creator_evaluation_status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                status["progress"]["selected_queue_ids"],
                ["yt_vid001", "yt_vid002", "yt_vid003"],
            )
            self.assertEqual(status["progress"]["candidate_queue_ids"], status["progress"]["selected_queue_ids"])
            self.assertEqual(status["progress"]["existing_delivery_target_ids"], [])

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
