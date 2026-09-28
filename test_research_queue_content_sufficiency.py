from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

            with mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
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

            with mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 1)
            self.assertEqual(queue["insufficient_content_count"], 0)
            self.assertEqual(queue["items"][0]["analysis_content_status"], "READY")
            self.assertEqual(queue["items"][0]["analysis_content_reason"], "TRANSCRIPT")


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

            with mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 1)
            self.assertEqual(queue["insufficient_content_count"], 0)
            packet = queue["items"][0]
            self.assertEqual(packet["analysis_content_status"], "READY")
            self.assertEqual(packet["analysis_content_reason"], "VISIBLE_TEXT")
            self.assertIn("liquidity", packet["analysis_evidence_text"].lower())
            self.assertEqual(packet["visual_text_source"], "GEMINI_VIDEO_VISIBLE_TEXT")


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

            with mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 0)
            self.assertEqual(queue["deferred_extraction_count"], 1)
            self.assertEqual(
                queue["deferred_extraction_items"][0]["queue_id"],
                "ig_story_123",
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
