"""Regression coverage: an Instagram video Story is not a screenshot-only post."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import ephemeral_ingest as ingest
import research_queue


class StoryVideoEvidenceTests(unittest.TestCase):
    def test_detect_video_and_verify_retained_video(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = "STORY:nicholascrown:4007851122504082790"
            item = {
                "source_type": "STORY",
                "media_kind": "VIDEO",
                "story_id": "4007851122504082790",
                "screenshot_sha256": "abc",
                "source_url": "https://www.instagram.com/stories/nicholascrown/4007851122504082790/",
            }
            manifest = {"items": {key: item}}
            capture = {"visited_item_keys": [key]}
            missing = ingest._story_missing_video_ids(root, "nicholascrown", manifest, capture)
            self.assertEqual(missing, ["4007851122504082790"])
            self.assertEqual(
                ingest._story_missing_video_urls(root, "nicholascrown", manifest, capture),
                ["https://www.instagram.com/stories/nicholascrown/4007851122504082790/"],
            )
            video_dir = root / "output" / "nicholascrown" / "stories" / "videos"
            video_dir.mkdir(parents=True)
            (video_dir / "4007851122504082790.mp4").write_bytes(b"video")
            self.assertEqual(
                ingest._story_missing_video_ids(root, "nicholascrown", manifest, capture), []
            )

    def test_cached_success_without_video_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = "STORY:testcreator:123"
            manifest = {"items": {key: {
                "source_type": "STORY", "media_kind": "VIDEO", "story_id": "123",
                "source_url": "https://www.instagram.com/stories/testcreator/123/",
                "screenshot_sha256": "a",
            }}}
            capture = {"visited_item_keys": [key]}
            fingerprint = ingest._story_media_set_fingerprint(manifest, capture)
            self.assertTrue(fingerprint)
            state_path = root / "state" / "ephemeral" / "story_media_download_state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"schema_version": 1, "items": {
                "testcreator": {"status": "SUCCESS", "fingerprint": fingerprint, "files": []}
            }}))
            self.assertIsNone(
                ingest._story_ytdlp_cached_result(root, "testcreator", manifest, capture)
            )

    def test_video_requires_retained_media_and_transcript_not_image_description(self) -> None:
        story = {
            "source_platform": "INSTAGRAM", "source_subtype": "STORY",
            "media_kind": "VIDEO", "screenshot_file": "story.png",
            "visual_description_status": "DONE",
            "visual_description": "A speaker is discussing the semiconductor selloff in front of charts.",
            "transcription_status": "MISSING_TRANSCRIPT",
        }
        verdict = research_queue.assess_analysis_content(story, "")
        self.assertEqual(verdict["status"], "PENDING_EXTRACTION")
        self.assertEqual(verdict["reason"], "STORY_VIDEO_MEDIA_MISSING")

        story["video_file"] = "video.mp4"
        verdict = research_queue.assess_analysis_content(story, "")
        self.assertEqual(verdict["status"], "PENDING_EXTRACTION")
        self.assertEqual(verdict["reason"], "STORY_VIDEO_TRANSCRIPT_MISSING")

        story["media_kind"] = "IMAGE"
        verdict = research_queue.assess_analysis_content(story, "")
        self.assertEqual(verdict["status"], "READY")
        self.assertEqual(verdict["reason"], "VISUAL_DESCRIPTION")

    def test_undetermined_story_type_must_not_claim_video_complete(self) -> None:
        item = {
            "source_platform": "INSTAGRAM", "source_subtype": "STORY",
            "media_kind": "UNKNOWN", "screenshot_file": "frame.png",
            "visual_description_status": "ERROR",
        }
        verdict = research_queue.assess_analysis_content(item, "")
        self.assertNotEqual(verdict["reason"], "TRANSCRIPT")
        self.assertNotEqual(verdict["reason"], "VISUAL_DESCRIPTION")


if __name__ == "__main__":
    unittest.main()
