from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path

import ephemeral_ingest as ei


class _PlaywrightContext:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


class StoryPrefetchCaptureOnlyTests(unittest.TestCase):
    def test_capture_only_skips_expensive_story_postprocessing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "state" / "ephemeral").mkdir(parents=True)
            page = unittest.mock.Mock()
            context = unittest.mock.Mock()
            context.pages = [page]

            item_key = "STORY:creator:123"

            def fake_capture(**kwargs):
                manifest = kwargs["manifest"]
                manifest["items"][item_key] = {
                    "source_type": "STORY",
                    "creator": "creator",
                    "research_status": "PENDING",
                    "screenshot_file": "output/creator/stories/screenshots/123.png",
                }
                return {
                    "reason": "OK",
                    "visited_frames": 1,
                    "visited_item_keys": [item_key],
                }

            with (
                unittest.mock.patch.object(ei, "sync_playwright", return_value=_PlaywrightContext()),
                unittest.mock.patch.object(ei, "launch_instagram_context", return_value=context),
                unittest.mock.patch.object(ei, "verify_logged_in"),
                unittest.mock.patch.object(ei, "capture_story_frames", side_effect=fake_capture),
                unittest.mock.patch.object(ei, "run_ytdlp") as ytdlp,
                unittest.mock.patch.object(ei, "enrich_story_visual_evidence") as enrich,
                unittest.mock.patch.object(ei, "transcribe_downloaded_videos") as transcribe,
            ):
                result = ei.run_one(
                    root=root,
                    mode="stories",
                    creator="creator",
                    highlight_label=None,
                    force=False,
                    max_items=3,
                    capture_only=True,
                )

            ytdlp.assert_not_called()
            enrich.assert_not_called()
            transcribe.assert_not_called()
            context.close.assert_called_once()
            self.assertEqual(result["state"], "DONE")
            self.assertTrue(result["capture_only"])
            self.assertTrue(result["video_download"]["skipped"])
            self.assertEqual(
                result["video_download"]["reason"],
                "PREFETCH_CAPTURE_ONLY",
            )
            self.assertEqual(result["visual_enrichment"]["deferred"], 1)
            self.assertTrue(result["transcription"]["skipped"])

            manifest = json.loads(
                (root / "state" / "ephemeral" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            item = manifest["items"][item_key]
            self.assertEqual(item["visual_description_status"], "DEFERRED")
            self.assertEqual(
                item["visual_description_deferred_reason"],
                "PREFETCH_CAPTURE_ONLY",
            )


if __name__ == "__main__":
    unittest.main()
