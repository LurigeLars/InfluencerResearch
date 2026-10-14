"""Regression coverage: an Instagram video Story is not a screenshot-only post."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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

    def test_authenticated_viewer_recovers_numeric_video_with_allowlisted_cdn(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = "STORY:creator:123"
            manifest = {"items": {key: {
                "media_kind": "VIDEO", "story_id": "123",
            }}}
            capture = {"visited_item_keys": [key]}
            page = mock.Mock()
            response = mock.Mock()
            response.status = 200
            response.headers = {"content-type": "video/mp4", "content-length": "1024"}
            response.body.return_value = b"\\x00\\x00\\x00\\x18ftyp" + b"x" * 512
            ctx = mock.Mock()
            ctx.new_page.return_value = page
            ctx.request.get.return_value = response
            with (
                mock.patch.object(ingest, "dismiss_story_view_confirmation"),
                mock.patch.object(ingest, "wait_for_story_media_ready", return_value={
                    "ready": True,
                    "media": {"kind": "video", "url": "https://scontent.cdninstagram.com/v/t50/story.mp4?token=x"},
                }),
            ):
                result = ingest._recover_missing_story_videos(
                    ctx, root, "creator", manifest, capture,
                )
            self.assertEqual(result["recovered"], ["123"])
            self.assertEqual(result["failures"], {})
            self.assertTrue(
                (root / "output" / "creator" / "stories" / "videos" / "123.mp4").is_file()
            )
            ctx.request.get.assert_called_once()
            self.assertEqual(ctx.request.get.call_args.kwargs["max_redirects"], 0)

    def test_browser_fallback_rejects_non_meta_media_origin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = "STORY:creator:123"
            manifest = {"items": {key: {"media_kind": "VIDEO", "story_id": "123"}}}
            page = mock.Mock()
            ctx = mock.Mock()
            ctx.new_page.return_value = page
            with (
                mock.patch.object(ingest, "dismiss_story_view_confirmation"),
                mock.patch.object(ingest, "wait_for_story_media_ready", return_value={
                    "ready": True,
                    "media": {"kind": "video", "url": "https://example.org/exfil.mp4"},
                }),
            ):
                result = ingest._recover_missing_story_videos(
                    ctx, root, "creator", manifest, {"visited_item_keys": [key]},
                )
            self.assertEqual(
                result["failures"]["123"], "MEDIA_SOURCE_NOT_ALLOWLISTED"
            )
            ctx.request.get.assert_not_called()

    def test_missing_video_is_counted_as_pending_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            control = root / "control"
            state.mkdir()
            control.mkdir()
            screenshot = root / "output" / "nicholascrown" / "stories" / "screenshots" / "4007851122504082790.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(b"retained-screenshot")
            manifest = {"schema_version": 1, "items": {
                "ig_story_4007851122504082790": {
                    "creator": "nicholascrown",
                    "source_platform": "INSTAGRAM",
                    "source_subtype": "STORY",
                    "media_kind": "VIDEO",
                    "source_id": "story:4007851122504082790",
                    "url": "https://www.instagram.com/stories/nicholascrown/4007851122504082790/",
                    "published_at": "2026-10-14T20:16:35+00:00",
                    "download_status": "MISSING_MEDIA",
                    "transcription_status": "MISSING_TRANSCRIPT",
                    "screenshot_file": str(screenshot.relative_to(root)),
                    "visual_description": "SOXX meltdown",
                    "visual_description_status": "DONE",
                    "research_status": "PENDING",
                    "permanent_source": True,
                }
            }}
            (state / "manifest.json").write_text(json.dumps(manifest))
            (state / "research_decisions.json").write_text(json.dumps({"items": {}}))
            (control / "research_screening.json").write_text(
                json.dumps({"max_queue_items": 100, "creators": []})
            )
            with mock.patch("sys.argv", ["research_queue.py", "--root", str(root)]):
                self.assertEqual(research_queue.main(), 0)
            queue = json.loads((state / "research_queue.json").read_text())
            self.assertEqual(queue["count"], 0)
            self.assertEqual(queue["pending_extraction_count"], 1)
            self.assertEqual(
                queue["pending_extraction_items"][0]["queue_id"],
                "ig_story_4007851122504082790",
            )
            self.assertEqual(
                queue["pending_extraction_items"][0]["reason"],
                "STORY_VIDEO_MEDIA_MISSING",
            )

    def test_reel_named_download_is_attributed_to_single_story(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / "output" / "nicholascrown" / "stories" / "videos"
            folder.mkdir(parents=True)
            source = folder / "Deev1yFgaVm.mp4"
            source.write_bytes(b"original-video")
            result = ingest._associate_targeted_story_video(
                root, folder,
                "https://www.instagram.com/stories/nicholascrown/4007851122504082790/",
                [source, source],
            )
            self.assertTrue(result["mapped"])
            self.assertEqual(result["source_id"], "Deev1yFgaVm")
            target = folder / "4007851122504082790.mp4"
            self.assertEqual(target.read_bytes(), b"original-video")
            self.assertEqual(result["story_id"], "4007851122504082790")

    def test_reel_attribution_fails_closed_for_multiple_distinct_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / "output" / "creator" / "stories" / "videos"
            folder.mkdir(parents=True)
            video_a = folder / "ReelA.mp4"
            video_b = folder / "ReelB.mp4"
            video_a.write_bytes(b"A")
            video_b.write_bytes(b"B")
            result = ingest._associate_targeted_story_video(
                root, folder, "https://www.instagram.com/stories/creator/123/",
                [video_a, video_b],
            )
            self.assertFalse(result["mapped"])
            self.assertEqual(result["reason"], "AMBIGUOUS_MEDIA")
            self.assertFalse((folder / "123.mp4").exists())

    def test_yt_dlp_maps_each_numeric_story_url_independently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / "output" / "creator" / "stories" / "videos"
            folder.mkdir(parents=True)
            urls = [
                "https://www.instagram.com/stories/creator/123/",
                "https://www.instagram.com/stories/creator/456/",
            ]
            invoked = []
            def fake_run(cmd, **kwargs):
                invoked.append(cmd[-1])
                reel = "ReelA" if cmd[-1] == urls[0] else "ReelB"
                filename = folder / f"{reel}.mp4"
                filename.write_bytes(reel.encode())
                return SimpleNamespace(
                    returncode=0, stdout=str(filename) + "\\n", stderr="",
                )
            with (
                mock.patch.object(ingest, "write_netscape_cookiefile"),
                mock.patch.object(ingest.subprocess, "run", side_effect=fake_run),
            ):
                result = ingest.run_ytdlp(
                    mock.Mock(), root, "creator", "STORY",
                    "https://www.instagram.com/stories/creator/",
                    story_urls=urls,
                )
            self.assertEqual(invoked, urls)
            self.assertTrue(result["ok"])
            self.assertEqual(
                (folder / "123.mp4").read_bytes(), b"ReelA",
            )
            self.assertEqual(
                (folder / "456.mp4").read_bytes(), b"ReelB",
            )

    def test_undetermined_story_type_must_not_claim_video_complete(self) -> None:
        item = {
            "source_platform": "INSTAGRAM", "source_subtype": "STORY",
            "media_kind": "UNKNOWN", "screenshot_file": "frame.png",
            "visual_description_status": "ERROR",
        }
        verdict = research_queue.assess_analysis_content(item, "")
        self.assertEqual(verdict["status"], "PENDING_EXTRACTION")
        self.assertEqual(verdict["reason"], "STORY_MEDIA_TYPE_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
