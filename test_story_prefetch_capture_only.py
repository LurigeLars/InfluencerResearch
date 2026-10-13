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


def _capture_only_run(**capture) -> dict:
    return {
        "creator": "creator",
        "mode": "stories",
        "source_url": "https://www.instagram.com/stories/creator/",
        "capture": {
            "reason": "OK",
            "visited_frames": 1,
            "visited_item_keys": ["STORY:creator:123"],
            **capture,
        },
        "visual_enrichment": {"deferred": 1, "prefetch_capture_only": True},
        "video_download": {"skipped": True, "reason": "PREFETCH_CAPTURE_ONLY"},
        "transcription": {"skipped": True, "reason": "PREFETCH_CAPTURE_ONLY"},
        "capture_only": True,
        "timings": {"capture_ms": 12.0},
        "state": "DONE",
        "errors": [],
    }


def _write_deferred_story(root: Path) -> None:
    (root / "state" / "ephemeral").mkdir(parents=True)
    (root / "state" / "ephemeral" / "manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "items": {
                "STORY:creator:123": {
                    "source_type": "STORY",
                    "creator": "creator",
                    "research_status": "PENDING",
                    "screenshot_file": "output/creator/stories/screenshots/123.png",
                    "visual_description_status": "DEFERRED",
                    "visual_description_deferred_reason": "PREFETCH_CAPTURE_ONLY",
                },
                "STORY:creator:older": {
                    "source_type": "STORY",
                    "creator": "creator",
                    "research_status": "PENDING",
                    "screenshot_file": "output/creator/stories/screenshots/older.png",
                    "visual_description_status": "DEFERRED",
                    "visual_description_deferred_reason": "PREFETCH_CAPTURE_ONLY",
                },
                "STORY:someone-else:1": {
                    "source_type": "STORY",
                    "creator": "someone-else",
                    "research_status": "PENDING",
                    "visual_description_status": "DEFERRED",
                },
            },
        }),
        encoding="utf-8",
    )


class FinishStoryCaptureTests(unittest.TestCase):
    """A prefetched Story must get the processing an Instagram post gets."""

    def test_downloads_enriches_and_transcribes_what_prefetch_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_deferred_story(root)
            context = unittest.mock.Mock()
            download = {"ok": True, "returncode": 0, "files": ["v.mp4"], "error": None}
            enrichment = {"completed": 2, "deferred": 0, "errors": [], "changed": False}
            transcripts = {"attempted": 1, "completed": 1, "errors": []}

            with (
                unittest.mock.patch.object(ei, "sync_playwright", return_value=_PlaywrightContext()),
                unittest.mock.patch.object(ei, "launch_instagram_context", return_value=context),
                unittest.mock.patch.object(ei, "capture_story_frames") as capture,
                unittest.mock.patch.object(ei, "run_ytdlp", return_value=download) as ytdlp,
                unittest.mock.patch.object(ei, "_record_story_ytdlp_success") as record,
                unittest.mock.patch.object(
                    ei, "enrich_story_visual_evidence", return_value=enrichment
                ) as enrich,
                unittest.mock.patch.object(
                    ei, "transcribe_downloaded_videos", return_value=transcripts
                ) as transcribe,
            ):
                budget = {"attempted": 0}
                result = ei.finish_story_capture(
                    root,
                    _capture_only_run(),
                    gemini_circuit={"reason": None},
                    ollama_budget_state=budget,
                )

            # The Story page is not opened a second time.
            capture.assert_not_called()
            ytdlp.assert_called_once()
            self.assertEqual(ytdlp.call_args.kwargs["source_type"], "STORY")
            self.assertEqual(
                ytdlp.call_args.kwargs["source_url"],
                "https://www.instagram.com/stories/creator/",
            )
            context.close.assert_called_once()
            record.assert_called_once()

            enrich.assert_called_once()
            # Visited frame first, then this creator's older deferred Story; never another creator's.
            self.assertEqual(
                enrich.call_args.args[2],
                ["STORY:creator:123", "STORY:creator:older"],
            )
            self.assertIs(enrich.call_args.kwargs["ollama_budget_state"], budget)
            self.assertEqual(
                enrich.call_args.kwargs["max_attempts"],
                ei.MAX_STORY_VISUAL_ENRICHMENTS_PER_RUN,
            )
            transcribe.assert_called_once_with(root, "creator", "STORY")

            self.assertFalse(result["capture_only"])
            self.assertEqual(result["state"], "DONE")
            self.assertEqual(result["video_download"], download)
            self.assertEqual(result["visual_enrichment"], enrichment)
            self.assertEqual(result["transcription"], transcripts)
            self.assertEqual(result["timings"]["capture_ms"], 12.0)
            self.assertIn("transcription_ms", result["timings"])

    def test_no_active_story_starts_no_browser_and_no_download(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_deferred_story(root)
            run = _capture_only_run(
                reason="NO_ACTIVE_STORY_OR_STORY_VIEW_REDIRECTED",
                visited_frames=0,
                visited_item_keys=[],
            )
            with (
                unittest.mock.patch.object(ei, "sync_playwright") as playwright,
                unittest.mock.patch.object(ei, "run_ytdlp") as ytdlp,
                unittest.mock.patch.object(
                    ei,
                    "enrich_story_visual_evidence",
                    return_value={"errors": [], "changed": False},
                ) as enrich,
                unittest.mock.patch.object(
                    ei,
                    "transcribe_downloaded_videos",
                    return_value={"attempted": 0, "completed": 0, "errors": []},
                ),
            ):
                result = ei.finish_story_capture(root, run)

            playwright.assert_not_called()
            ytdlp.assert_not_called()
            self.assertEqual(result["video_download"]["reason"], "NO_ACTIVE_STORY")
            # Older deferred Stories of the creator are still worked off.
            self.assertEqual(
                enrich.call_args.args[2],
                ["STORY:creator:123", "STORY:creator:older"],
            )

    def test_already_downloaded_media_set_starts_no_browser(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_deferred_story(root)
            cached = {"ok": True, "files": [], "skipped": True, "error": None}
            with (
                unittest.mock.patch.object(ei, "_story_ytdlp_cached_result", return_value=cached),
                unittest.mock.patch.object(ei, "sync_playwright") as playwright,
                unittest.mock.patch.object(ei, "run_ytdlp") as ytdlp,
                unittest.mock.patch.object(
                    ei,
                    "enrich_story_visual_evidence",
                    return_value={"errors": [], "changed": False},
                ),
                unittest.mock.patch.object(
                    ei,
                    "transcribe_downloaded_videos",
                    return_value={"attempted": 0, "completed": 0, "errors": []},
                ),
            ):
                result = ei.finish_story_capture(root, _capture_only_run())

            playwright.assert_not_called()
            ytdlp.assert_not_called()
            self.assertEqual(result["video_download"], cached)

    def test_failed_download_is_reported_but_text_evidence_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_deferred_story(root)
            context = unittest.mock.Mock()
            with (
                unittest.mock.patch.object(ei, "sync_playwright", return_value=_PlaywrightContext()),
                unittest.mock.patch.object(ei, "launch_instagram_context", return_value=context),
                unittest.mock.patch.object(
                    ei,
                    "run_ytdlp",
                    return_value={"ok": False, "returncode": 1, "error": "boom", "files": []},
                ),
                unittest.mock.patch.object(ei, "_record_story_ytdlp_success") as record,
                unittest.mock.patch.object(
                    ei,
                    "enrich_story_visual_evidence",
                    return_value={"errors": [], "changed": False},
                ) as enrich,
                unittest.mock.patch.object(
                    ei,
                    "transcribe_downloaded_videos",
                    return_value={"attempted": 0, "completed": 0, "errors": []},
                ) as transcribe,
            ):
                result = ei.finish_story_capture(root, _capture_only_run())

            record.assert_not_called()
            context.close.assert_called_once()
            enrich.assert_called_once()
            transcribe.assert_called_once()
            self.assertEqual(result["state"], "DONE_WITH_ERRORS")
            self.assertEqual(result["errors"], ["yt-dlp: boom"])

    def test_a_fully_processed_run_is_returned_untouched(self) -> None:
        run = {**_capture_only_run(), "capture_only": False}
        with (
            unittest.mock.patch.object(ei, "run_ytdlp") as ytdlp,
            unittest.mock.patch.object(ei, "enrich_story_visual_evidence") as enrich,
            unittest.mock.patch.object(ei, "transcribe_downloaded_videos") as transcribe,
        ):
            self.assertIs(ei.finish_story_capture(Path("."), run), run)
        ytdlp.assert_not_called()
        enrich.assert_not_called()
        transcribe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
