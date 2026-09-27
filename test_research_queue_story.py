from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import research_queue


class ResearchQueueStoryTests(unittest.TestCase):
    def test_visual_only_story_enters_canonical_queue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            control = root / "control"
            state.mkdir(parents=True)
            control.mkdir(parents=True)

            shot = root / "output" / "creator" / "stories" / "screenshots" / "abc.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(b"png")

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
                        "browser_text": "Story text captured from the browser",
                        "screenshot_file": str(shot.relative_to(root)),
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
            self.assertEqual(packet["transcript_text"], "Story text captured from the browser")
            self.assertEqual(packet["screenshot_file"], str(shot.relative_to(root)))


if __name__ == "__main__":
    unittest.main()
