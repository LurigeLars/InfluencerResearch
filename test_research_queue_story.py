from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import research_queue


class ResearchQueueStoryTests(unittest.TestCase):
    def test_story_with_visual_description_enters_canonical_queue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            control = root / "control"
            state.mkdir(parents=True)
            control.mkdir(parents=True)

            shot = root / "output" / "creator" / "stories" / "screenshots" / "abc.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(b"png")
            video = root / "output" / "creator" / "stories" / "videos" / "abc.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")

            manifest = {
                "schema_version": 1,
                "items": {
                    "ig_story_abc": {
                        "creator": "creator",
                        "source_platform": "INSTAGRAM",
                        "source_subtype": "STORY",
                        "source_id": "story:abc",
                        "url": "https://www.instagram.com/stories/creator/abc/",
                        "published_at": "2026-09-27T10:00:00+00:00",
                        "published_at_basis": "ACTIVE_STORY_OBSERVED_AT",
                        "observed_at": "2026-09-27T10:00:00+00:00",
                        "download_status": "DONE",
                        "transcription_status": "NOT_APPLICABLE",
                        "browser_text": "creator\\n2h",
                        "visual_description": (
                            "Visible Story text says Brent-WTI spread is 13 dollars. "
                            "A line chart underneath rises sharply into the latest observation."
                        ),
                        "visual_description_status": "DONE",
                        "visual_description_source": "GEMINI_STORY_SCREENSHOT_EVIDENCE",
                        "visual_description_provider": "gemini",
                        "visual_description_model": "gemini-3.8-flash",
                        "screenshot_file": str(shot.relative_to(root)),
                        "video_file": str(video.relative_to(root)),
                        "full_video_persisted": True,
                        "visual_evidence_status": "DONE",
                        "visual_evidence_index": str(shot.relative_to(root)),
                        "visual_frame_count": 1,
                        "visual_capture_strategy": "INSTAGRAM_STORY_SCREENSHOT",
                        "permanent_source": True,
                    }
                },
            }
            (state / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (state / "research_decisions.json").write_text(
                json.dumps({"schema_version": 2, "items": {}}),
                encoding="utf-8",
            )
            (control / "research_screening.json").write_text(
                json.dumps({"max_queue_items": 100, "creators": []}),
                encoding="utf-8",
            )

            with mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads((state / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 1)
            packet = queue["items"][0]
            self.assertEqual(packet["queue_id"], "ig_story_abc")
            self.assertEqual(packet["source_platform"], "INSTAGRAM")
            self.assertEqual(packet["source_subtype"], "STORY")
            self.assertEqual(packet["published_at_basis"], "ACTIVE_STORY_OBSERVED_AT")
            self.assertIsNone(packet["transcript_file"])
            self.assertEqual(packet["transcript_text"], "")
            self.assertEqual(packet["browser_text"], "creator\\n2h")
            self.assertEqual(packet["analysis_content_reason"], "VISUAL_DESCRIPTION")
            self.assertIn("Brent-WTI", packet["visual_description"])
            self.assertIn("Brent-WTI", packet["analysis_evidence_text"])
            self.assertEqual(packet["screenshot_file"], str(shot.relative_to(root)))
            self.assertEqual(packet["video_file"], str(video.relative_to(root)))
            self.assertTrue(packet["raw_media_available"])


    def test_story_visual_provider_error_is_not_mislabeled_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            control = root / "control"
            state.mkdir(parents=True)
            control.mkdir(parents=True)

            shot = root / "output" / "creator" / "stories" / "screenshots" / "stable.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(b"png")
            manifest = {
                "schema_version": 1,
                "items": {
                    "ig_story_stable": {
                        "creator": "creator",
                        "source_platform": "INSTAGRAM",
                        "source_subtype": "STORY",
                        "source_id": "story:stable",
                        "url": "https://www.instagram.com/stories/creator/",
                        "published_at": "2026-09-28T10:00:00+00:00",
                        "download_status": "DONE",
                        "transcription_status": "NOT_APPLICABLE",
                        "browser_text": "creator\\n2h",
                        "story_identity_basis": "VISIBLE_MEDIA_URL_PATH",
                        "screenshot_file": str(shot.relative_to(root)),
                        "visual_evidence_status": "DONE",
                        "visual_frame_count": 1,
                        "visual_description_status": "ERROR",
                        "permanent_source": True,
                    }
                },
            }
            (state / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (state / "research_decisions.json").write_text(
                json.dumps({"schema_version": 2, "items": {}}),
                encoding="utf-8",
            )
            (control / "research_screening.json").write_text(
                json.dumps({"max_queue_items": 100, "creators": []}),
                encoding="utf-8",
            )

            with mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads((state / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 1)
            self.assertEqual(queue["insufficient_content_count"], 0)
            self.assertEqual(queue["extraction_error_count"], 0)
            packet = queue["items"][0]
            self.assertEqual(packet["analysis_content_status"], "READY")
            self.assertEqual(packet["analysis_content_reason"], "AGENT_VISUAL_FALLBACK")
            self.assertEqual(packet["analysis_mode_recommended"], "VISUAL_REVIEW_REQUIRED")
            self.assertTrue(packet["visual_review_recommended"])
            self.assertIn("AGENT_VISUAL_FALLBACK", packet["visual_review_reason"])


    def test_story_rate_limit_is_deferred_not_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            control = root / "control"
            state.mkdir(parents=True)
            control.mkdir(parents=True)

            shot = root / "output" / "creator" / "stories" / "screenshots" / "deferred.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(b"png")
            manifest = {
                "schema_version": 1,
                "items": {
                    "ig_story_deferred": {
                        "creator": "creator",
                        "source_platform": "INSTAGRAM",
                        "source_subtype": "STORY",
                        "source_id": "story:deferred",
                        "url": "https://www.instagram.com/stories/creator/123/",
                        "published_at": "2026-09-28T10:00:00+00:00",
                        "download_status": "DONE",
                        "transcription_status": "NOT_APPLICABLE",
                        "browser_text": "creator\\n2h",
                        "screenshot_file": str(shot.relative_to(root)),
                        "visual_evidence_status": "DONE",
                        "visual_frame_count": 1,
                        "visual_description_status": "DEFERRED",
                        "visual_description_deferred_reason": "PROVIDER_RATE_LIMIT",
                        "visual_description_retry_after": "2026-09-28T10:05:00+00:00",
                        "visual_description_error": (
                            "ClientError: Gemini visual evidence extraction failed "
                            "code=429 status=RESOURCE_EXHAUSTED"
                        ),
                        "permanent_source": True,
                    }
                },
            }
            (state / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (state / "research_decisions.json").write_text(
                json.dumps({"schema_version": 2, "items": {}}),
                encoding="utf-8",
            )
            (control / "research_screening.json").write_text(
                json.dumps({"max_queue_items": 100, "creators": []}),
                encoding="utf-8",
            )

            with mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads((state / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 1)
            self.assertEqual(queue["insufficient_content_count"], 0)
            self.assertEqual(queue["deferred_extraction_count"], 0)
            packet = queue["items"][0]
            self.assertEqual(packet["analysis_content_status"], "READY")
            self.assertEqual(packet["analysis_content_reason"], "AGENT_VISUAL_FALLBACK")
            self.assertEqual(packet["analysis_mode_recommended"], "VISUAL_REVIEW_REQUIRED")
            self.assertTrue(packet["visual_review_recommended"])
            updated = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                updated["items"]["ig_story_deferred"]["research_status"],
                "PENDING_ANALYSIS",
            )


    def test_legacy_root_frame_is_retired_from_queue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            control = root / "control"
            state.mkdir(parents=True)
            control.mkdir(parents=True)

            shot = root / "output" / "creator" / "stories" / "screenshots" / "frame-old.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(b"png")
            manifest = {
                "schema_version": 1,
                "items": {
                    "ig_story_frame-old": {
                        "creator": "creator",
                        "source_platform": "INSTAGRAM",
                        "source_subtype": "STORY",
                        "source_id": "story:frame-old",
                        "url": "https://www.instagram.com/stories/creator/",
                        "published_at": "2026-09-28T10:00:00+00:00",
                        "download_status": "DONE",
                        "transcription_status": "NOT_APPLICABLE",
                        "browser_text": "creator\\n1h",
                        "screenshot_file": str(shot.relative_to(root)),
                        "visual_evidence_status": "DONE",
                        "visual_frame_count": 1,
                        "permanent_source": True,
                    }
                },
            }
            (state / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (state / "research_decisions.json").write_text(
                json.dumps({"schema_version": 2, "items": {}}),
                encoding="utf-8",
            )
            (control / "research_screening.json").write_text(
                json.dumps({"max_queue_items": 100, "creators": []}),
                encoding="utf-8",
            )

            with mock.patch.object(sys, "argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)

            queue = json.loads((state / "research_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue["count"], 0)
            updated = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
            item = updated["items"]["ig_story_frame-old"]
            self.assertEqual(item["research_status"], "INVALID")
            self.assertEqual(
                item["invalid_reason"],
                "LEGACY_UNSTABLE_ROOT_STORY_IDENTITY",
            )


if __name__ == "__main__":
    unittest.main()
