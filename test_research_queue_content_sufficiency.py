from __future__ import annotations

import json
import sys
import tempfile
import unittest.mock
from pathlib import Path

import research_queue


class ResearchQueueContentSufficiencyTests(unittest.TestCase):
    def _write_common(self, root: Path, manifest: dict) -> None:
        state = root / "state"
        control = root / "control"
        state.mkdir(parents=True)
        control.mkdir(parents=True)
        (state / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (state / "research_decisions.json").write_text(
            json.dumps({"schema_version": 2, "items": {}}),
            encoding="utf-8",
        )
        (control / "research_screening.json").write_text(
            json.dumps({"max_queue_items": 100, "creators": []}),
            encoding="utf-8",
        )

    def test_short_caption_and_empty_transcript_is_marked_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transcript = root / "output" / "nicholascrown" / "tiktok" / "transcripts" / "7690360065731202317.txt"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "items": {
                    "tt_7690360065731202317": {
                        "creator": "nicholascrown",
                        "source_platform": "TIKTOK",
                        "source_type": "VIDEO",
                        "source_id": "7690360065731202317",
                        "url": "https://www.tiktok.com/@nicholas_crown/video/7690360065731202317",
                        "caption": "This is the layer that took me years to learn.",
                        "published_at": "2026-09-27T23:25:01+00:00",
                        "download_status": "DONE",
                        "transcription_status": "DONE",
                        "transcript_txt": str(transcript.relative_to(root)),
                    }
                },
            }
            self._write_common(root, manifest)

            with unittest.mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 0)
            self.assertEqual(queue["insufficient_content_count"], 1)
            self.assertEqual(
                queue["insufficient_content_items"][0]["queue_id"],
                "tt_7690360065731202317",
            )

            updated = json.loads((root / "state" / "manifest.json").read_text(encoding="utf-8"))
            item = updated["items"]["tt_7690360065731202317"]
            self.assertEqual(item["research_status"], "INSUFFICIENT_CONTENT")
            self.assertEqual(item["analysis_content_status"], "INSUFFICIENT_CONTENT")
            self.assertEqual(
                item["analysis_content_reason"],
                "NO_ANALYZABLE_TEXT_OR_VISUAL_EVIDENCE",
            )

    def test_explicit_marked_insufficient_is_terminal_across_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            screenshot = root / "output" / "creator" / "stories" / "screenshots" / "123.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(b"retained-frame")
            manifest = {
                "schema_version": 1,
                "items": {
                    "ig_story_123": {
                        "creator": "creator",
                        "source_platform": "INSTAGRAM",
                        "source_subtype": "STORY",
                        "media_kind": "IMAGE",
                        "source_id": "story:123",
                        "url": "https://www.instagram.com/stories/creator/123/",
                        "published_at": "2026-10-06T18:00:00+00:00",
                        "download_status": "DONE",
                        "transcription_status": "NOT_APPLICABLE",
                        "screenshot_file": str(screenshot.relative_to(root)),
                        "visual_description_status": "DEFERRED",
                        "research_status": "INSUFFICIENT_CONTENT",
                        "analysis_content_status": "INSUFFICIENT_CONTENT",
                        "analysis_content_reason": "RETAINED_FRAME_IS_BLANK_LOADING_SCREEN",
                        "analysis_insufficient_reason": "RETAINED_FRAME_IS_BLANK_LOADING_SCREEN",
                        "analysis_insufficient_marked_at": "2026-10-07T00:00:00+00:00",
                    }
                },
            }
            self._write_common(root, manifest)

            with unittest.mock.patch.object(
                sys,
                "argv",
                ["research_queue.py", "--root", str(root)],
            ):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads(
                (root / "state" / "research_queue.json").read_text(encoding="utf-8")
            )
            self.assertEqual(queue["count"], 0)
            self.assertEqual(queue["insufficient_content_count"], 1)
            updated = json.loads(
                (root / "state" / "manifest.json").read_text(encoding="utf-8")
            )["items"]["ig_story_123"]
            self.assertEqual(updated["research_status"], "INSUFFICIENT_CONTENT")
            self.assertEqual(updated["analysis_content_status"], "INSUFFICIENT_CONTENT")
            self.assertEqual(
                updated["analysis_insufficient_reason"],
                "RETAINED_FRAME_IS_BLANK_LOADING_SCREEN",
            )


    def test_youtube_retained_frames_route_short_transcript_to_required_visual_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transcript = root / "output" / "matfinog" / "youtube" / "transcripts" / "MBhravgbxxk.txt"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("wow", encoding="utf-8")
            evidence = root / "output" / "matfinog" / "youtube" / "frames" / "MBhravgbxxk"
            evidence.mkdir(parents=True)
            index = evidence / "visual_index.json"
            index.write_text(json.dumps({"frames": [{"timestamp_s": 3.5, "file": "frame.jpg"}], "version": 1, "note": "verified retained visual frames"}), encoding="utf-8")
            frame = evidence / "frame.jpg"
            frame.write_bytes(b"retained-frame-evidence" * 8)
            manifest = {
                "schema_version": 1,
                "items": {
                    "yt_MBhravgbxxk": {
                        "creator": "matfinog",
                        "source_platform": "YOUTUBE",
                        "source_type": "VIDEO",
                        "source_id": "MBhravgbxxk",
                        "url": "https://www.youtube.com/watch?v=MBhravgbxxk",
                        "caption": "Short clip",
                        "published_at": "2026-10-17T14:22:22+00:00",
                        "download_status": "DONE",
                        "transcription_status": "DONE",
                        "transcript_txt": str(transcript.relative_to(root)),
                        "visual_evidence_status": "DONE",
                        "visual_evidence_index": str(index.relative_to(root)),
                        "visual_frame_count": 1,
                        "analysis_mode_recommended": "TRANSCRIPT_ONLY",
                        "agent_visual_bundle": {
                            "representative_frames": [
                                {"file": str(frame.relative_to(root)), "timestamp_s": 3.5}
                            ]
                        },
                    }
                },
            }
            self._write_common(root, manifest)
            for attempt in range(2):
                with unittest.mock.patch.object(
                    sys, "argv", ["research_queue.py", "--root", str(root)]
                ):
                    self.assertEqual(research_queue.main(), 0)
                queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
                self.assertEqual(queue["count"], 1, "idempotent rebuild must not duplicate")
                packet = queue["items"][0]
                self.assertEqual(packet["queue_id"], "yt_MBhravgbxxk")
                self.assertEqual(packet["analysis_content_reason"], "AGENT_VISUAL_FALLBACK")
                self.assertEqual(packet["analysis_mode_recommended"], "VISUAL_REVIEW_REQUIRED")
                self.assertTrue(packet["visual_review_recommended"])
                self.assertEqual(packet["transcript_text"], "wow")
                self.assertIn("VERIFIED_FRAMES_REQUIRE_AGENT_REVIEW", packet["visual_review_reason"])

    def test_youtube_missing_frame_does_not_claim_visual_evidence_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transcript = root / "output" / "matfinog" / "youtube" / "transcripts" / "MBhravgbxxk.txt"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("wow", encoding="utf-8")
            index = root / "output" / "matfinog" / "youtube" / "frames" / "MBhravgbxxk" / "visual_index.json"
            index.parent.mkdir(parents=True)
            index.write_text(json.dumps({"frames": [], "source": "retained"}), encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "items": {
                    "yt_MBhravgbxxk": {
                        "creator": "matfinog",
                        "source_platform": "YOUTUBE",
                        "source_id": "MBhravgbxxk",
                        "url": "https://www.youtube.com/watch?v=MBhravgbxxk",
                        "published_at": "2026-10-17T14:22:22+00:00",
                        "download_status": "DONE",
                        "transcription_status": "DONE",
                        "transcript_txt": str(transcript.relative_to(root)),
                        "visual_evidence_status": "DONE",
                        "visual_evidence_index": str(index.relative_to(root)),
                        "visual_frame_count": 3,
                        "agent_visual_bundle": {
                            "representative_frames": [
                                {"file": "output/matfinog/youtube/frames/MBhravgbxxk/missing.jpg"}
                            ]
                        },
                    }
                },
            }
            self._write_common(root, manifest)
            with unittest.mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)
            queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 0)
            self.assertEqual(queue["pending_extraction_count"], 1)
            self.assertEqual(queue["pending_extraction_items"][0]["reason"], "VISUAL_EXTRACTION_PENDING")


    def test_youtube_watch_canonicalization_preserves_video_identity(self) -> None:
        first = research_queue.canonicalize_url(
            "https://www.youtube.com/watch?v=MBhravgbxxk&t=35s&feature=share"
        )
        second = research_queue.canonicalize_url(
            "https://m.youtube.com/watch?feature=share&v=qUyOFyzZpps"
        )
        same_first = research_queue.canonicalize_url(
            "https://youtube.com/watch?feature=other&v=MBhravgbxxk"
        )
        self.assertEqual(first, "https://www.youtube.com/watch?v=MBhravgbxxk")
        self.assertEqual(second, "https://www.youtube.com/watch?v=qUyOFyzZpps")
        self.assertEqual(first, same_first)
        self.assertNotEqual(first, second)
        self.assertEqual(
            research_queue.canonicalize_url("https://example.com/report?id=1&track=1"),
            "https://example.com/report",
        )

    def test_false_exact_url_duplicate_is_repaired_without_changing_final_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base = root / "output" / "matfinog" / "youtube" / "transcripts"
            base.mkdir(parents=True)
            phrase = (
                "Prices moved in the opening range while traders evaluated "
                "liquidity volatility catalysts and risk across major sectors."
            )
            records = {}
            for video_id in ("MBhravgbxxk", "qUyOFyzZpps"):
                transcript = base / f"{video_id}.txt"
                transcript.write_text(phrase, encoding="utf-8")
                records[f"yt_{video_id}"] = {
                    "creator": "matfinog",
                    "source_platform": "YOUTUBE",
                    "source_type": "VIDEO",
                    "source_id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "published_at": "2026-10-17T14:22:22+00:00",
                    "download_status": "DONE",
                    "transcription_status": "DONE",
                    "transcript_txt": str(transcript.relative_to(root)),
                }
            records["yt_MBhravgbxxk"].update({
                "research_status": "DUPLICATE",
                "duplicate_of": "yt_qUyOFyzZpps",
                "duplicate_basis": "EXACT_SOURCE_URL",
                "evidence_lineage_id": "EL-false-url-identity",
            })
            self._write_common(root, {"schema_version": 1, "items": records})
            decision_file = root / "state" / "research_decisions.json"
            decision_file.write_text(json.dumps({
                "schema_version": 2,
                "items": {
                    "yt_qUyOFyzZpps": {
                        "decision": "IGNORE",
                        "evidence_lineage_id": "EL-analyst-owned",
                    }
                },
            }), encoding="utf-8")

            for _ in range(2):
                with unittest.mock.patch.object(
                    sys, "argv", ["research_queue.py", "--root", str(root)]
                ):
                    self.assertEqual(research_queue.main(), 0)
                queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
                self.assertEqual(queue["count"], 1)
                self.assertEqual(queue["items"][0]["queue_id"], "yt_MBhravgbxxk")
                self.assertIsNone(queue["items"][0]["duplicate_of"])
                manifest = json.loads((root / "state" / "manifest.json").read_text(encoding="utf-8"))
                fixed = manifest["items"]["yt_MBhravgbxxk"]
                self.assertEqual(fixed["research_status"], "PENDING_ANALYSIS")
                self.assertIsNone(fixed["duplicate_of"])
                self.assertIsNone(fixed["duplicate_basis"])
                self.assertNotEqual(fixed["evidence_lineage_id"], "EL-false-url-identity")
                decisions = json.loads(decision_file.read_text(encoding="utf-8"))["items"]
                self.assertEqual(decisions["yt_qUyOFyzZpps"]["decision"], "IGNORE")
                self.assertEqual(decisions["yt_qUyOFyzZpps"]["evidence_lineage_id"], "EL-analyst-owned")

    def test_real_transcript_remains_analysis_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transcript = root / "output" / "creator" / "tiktok" / "transcripts" / "123.txt"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "Liquidity is moving out of small caps and into profitable technology names before earnings.",
                encoding="utf-8",
            )
            manifest = {
                "schema_version": 1,
                "items": {
                    "tt_123": {
                        "creator": "creator",
                        "source_platform": "TIKTOK",
                        "source_type": "VIDEO",
                        "source_id": "123",
                        "url": "https://www.tiktok.com/@creator/video/123",
                        "caption": "Market update",
                        "published_at": "2026-09-27T20:00:00+00:00",
                        "download_status": "DONE",
                        "transcription_status": "DONE",
                        "transcript_txt": str(transcript.relative_to(root)),
                    }
                },
            }
            self._write_common(root, manifest)

            with unittest.mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 1)
            self.assertEqual(queue["insufficient_content_count"], 0)
            self.assertEqual(queue["items"][0]["analysis_content_status"], "READY")
            self.assertEqual(queue["items"][0]["analysis_content_reason"], "TRANSCRIPT")


    def test_exact_must_include_bypasses_screening_allowlist_only_for_that_item(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transcript = root / "output" / "thetradingfraternity" / "youtube" / "transcripts" / "abc.txt"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "Market breadth is weakening while defensive sectors gain relative strength into the close.",
                encoding="utf-8",
            )
            manifest = {
                "schema_version": 1,
                "items": {
                    "yt_abc": {
                        "creator": "thetradingfraternity",
                        "source_platform": "YOUTUBE",
                        "source_type": "VIDEO",
                        "source_id": "abc",
                        "url": "https://www.youtube.com/watch?v=abc",
                        "published_at": "2026-10-05T20:00:00+00:00",
                        "download_status": "DONE",
                        "transcription_status": "DONE",
                        "transcript_txt": str(transcript.relative_to(root)),
                    }
                },
            }
            self._write_common(root, manifest)
            (root / "control" / "research_screening.json").write_text(
                json.dumps({
                    "max_queue_items": 100,
                    "creators": ["nicholascrown"],
                }),
                encoding="utf-8",
            )

            with unittest.mock.patch.object(
                sys,
                "argv",
                ["research_queue.py", "--root", str(root)],
            ):
                self.assertEqual(research_queue.main(), 0)
            queue = json.loads(
                (root / "state" / "research_queue.json").read_text(encoding="utf-8")
            )
            self.assertEqual(queue["count"], 0)

            with unittest.mock.patch.object(
                sys,
                "argv",
                [
                    "research_queue.py",
                    "--root",
                    str(root),
                    "--must-include-shortcode",
                    "yt_abc",
                ],
            ):
                self.assertEqual(research_queue.main(), 0)
            queue = json.loads(
                (root / "state" / "research_queue.json").read_text(encoding="utf-8")
            )
            self.assertEqual(queue["count"], 1)
            self.assertEqual(queue["items"][0]["queue_id"], "yt_abc")

    def test_visible_text_recovers_empty_audio_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transcript = root / "output" / "nicholascrown" / "tiktok" / "transcripts" / "7690360065731202317.txt"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "items": {
                    "tt_7690360065731202317": {
                        "creator": "nicholascrown",
                        "source_platform": "TIKTOK",
                        "source_type": "VIDEO",
                        "source_id": "7690360065731202317",
                        "url": "https://www.tiktok.com/@nicholas_crown/video/7690360065731202317",
                        "caption": "This is the layer that took me years to learn.",
                        "visible_text": (
                            "Layer one is liquidity. Layer two is positioning. "
                            "Layer three is waiting for confirmation before entering the trade."
                        ),
                        "visual_text_status": "DONE",
                        "visual_text_source": "GEMINI_VIDEO_VISIBLE_TEXT",
                        "published_at": "2026-09-27T23:25:01+00:00",
                        "download_status": "DONE",
                        "transcription_status": "DONE",
                        "transcript_txt": str(transcript.relative_to(root)),
                    }
                },
            }
            self._write_common(root, manifest)

            with unittest.mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 1)
            self.assertEqual(queue["insufficient_content_count"], 0)
            packet = queue["items"][0]
            self.assertEqual(packet["analysis_content_status"], "READY")
            self.assertEqual(packet["analysis_content_reason"], "VISIBLE_TEXT")
            self.assertIn("liquidity", packet["analysis_evidence_text"].lower())
            self.assertEqual(packet["visual_text_source"], "GEMINI_VIDEO_VISIBLE_TEXT")


    def test_deferred_story_with_retained_screenshot_uses_agent_visual_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            screenshot = root / "output" / "creator" / "stories" / "screenshots" / "123.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(b"not-a-real-image-but-retained-evidence")
            manifest = {
                "schema_version": 1,
                "items": {
                    "ig_story_123": {
                        "creator": "creator",
                        "source_platform": "INSTAGRAM",
                        "source_subtype": "STORY",
                        "media_kind": "IMAGE",
                        "source_id": "story:123",
                        "url": "https://www.instagram.com/stories/creator/123/",
                        "caption": "",
                        "browser_text": "creator 1h",
                        "published_at": "2026-09-28T18:00:00+00:00",
                        "download_status": "DONE",
                        "transcription_status": "NOT_APPLICABLE",
                        "screenshot_file": str(screenshot.relative_to(root)),
                        "visual_description_status": "DEFERRED",
                        "visual_description_deferred_reason": "PREFETCH_CAPTURE_ONLY",
                    }
                },
            }
            self._write_common(root, manifest)

            with unittest.mock.patch.object(
                sys,
                "argv",
                ["research_queue.py", "--root", str(root)],
            ):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads(
                (root / "state" / "research_queue.json").read_text(encoding="utf-8")
            )
            self.assertEqual(queue["count"], 1)
            self.assertEqual(queue["deferred_extraction_count"], 0)
            item = queue["items"][0]
            self.assertEqual(item["analysis_content_status"], "READY")
            self.assertEqual(
                item["analysis_content_reason"],
                "AGENT_VISUAL_FALLBACK",
            )

    def test_deferred_visual_description_is_not_analysis_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {
                "schema_version": 1,
                "items": {
                    "ig_story_123": {
                        "creator": "creator",
                        "source_platform": "INSTAGRAM",
                        "source_subtype": "STORY",
                        "media_kind": "IMAGE",
                        "source_id": "story:123",
                        "url": "https://www.instagram.com/stories/creator/123/",
                        "caption": "",
                        "browser_text": "creator 1h",
                        "published_at": "2026-09-28T18:00:00+00:00",
                        "download_status": "DONE",
                        "transcription_status": "NOT_APPLICABLE",
                        "screenshot_file": "output/creator/stories/screenshots/123.png",
                        "visual_description": (
                            "Old stale local-model text that is long enough to pass the normal "
                            "readiness threshold but must not be analyzed while extraction is deferred."
                        ),
                        "visual_description_status": "DEFERRED",
                        "visual_description_deferred_reason": "PROVIDER_RATE_LIMIT",
                        "visual_description_retry_after": "2099-01-01T00:00:00+00:00",
                    }
                },
            }
            self._write_common(root, manifest)

            with unittest.mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 0)
            self.assertEqual(queue["deferred_extraction_count"], 1)
            self.assertEqual(
                queue["deferred_extraction_items"][0]["queue_id"],
                "ig_story_123",
            )

    def test_atomic_write_json_if_changed_skips_identical_packet(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "packet.json"
            data = {"queue_id": "x", "value": 1}

            self.assertTrue(research_queue.atomic_write_json_if_changed(path, data))
            first_mtime = path.stat().st_mtime_ns
            self.assertFalse(research_queue.atomic_write_json_if_changed(path, data))
            self.assertEqual(path.stat().st_mtime_ns, first_mtime)

            self.assertTrue(
                research_queue.atomic_write_json_if_changed(
                    path,
                    {"queue_id": "x", "value": 2},
                )
            )
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["value"],
                2,
            )

    def test_build_packet_reuses_supplied_transcript_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transcript = root / "transcript.txt"
            transcript.write_text("disk copy should not be reread", encoding="utf-8")
            item = {
                "creator": "creator",
                "source_platform": "TIKTOK",
                "caption": "Market update",
            }
            with unittest.mock.patch.object(
                research_queue,
                "read_text",
                side_effect=AssertionError("unexpected second transcript read"),
            ):
                packet = research_queue.build_packet(
                    root,
                    "tt_123",
                    item,
                    transcript,
                    evidence_lineage_id="EL-test",
                    duplicate_of=None,
                    duplicate_basis=None,
                    transcript_text="reused transcript from manifest scan",
                )

            self.assertEqual(
                packet["transcript_text"],
                "reused transcript from manifest scan",
            )

    def test_build_packet_excludes_stale_deferred_visual_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            item = {
                "creator": "creator",
                "source_platform": "INSTAGRAM",
                "visual_description": "STALE VISUAL MODEL EVIDENCE MUST NOT LEAK",
                "visual_description_status": "DEFERRED",
                "caption": "Short caption",
            }
            packet = research_queue.build_packet(
                root,
                "ig_story_123",
                item,
                None,
                evidence_lineage_id="EL-test",
                duplicate_of=None,
                duplicate_basis=None,
            )
            self.assertEqual(packet["visual_description"], "")
            self.assertNotIn("STALE VISUAL MODEL EVIDENCE", packet["analysis_evidence_text"])


if __name__ == "__main__":
    unittest.main()
