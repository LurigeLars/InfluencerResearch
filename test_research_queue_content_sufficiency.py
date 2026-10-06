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
