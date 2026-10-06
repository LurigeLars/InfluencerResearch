from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ephemeral_ingest as ep


class StoryStageSplitTests(unittest.TestCase):
    def test_postprocess_story_run_finishes_deferred_capture(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_path = root / "state" / "ephemeral" / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                '{"schema_version":1,"items":{"story:1":{"source_type":"STORY","creator":"fixture"}}}',
                encoding="utf-8",
            )
            run = {
                "creator": "fixture",
                "mode": "stories",
                "capture": {"visited_item_keys": ["story:1"]},
                "video_download": {"ok": True},
                "visual_enrichment": {"postprocess_deferred": True, "errors": []},
                "transcription": {"postprocess_deferred": True, "errors": []},
                "timings": {"capture_ms": 12.0},
                "postprocess_deferred": True,
                "state": "DONE",
                "errors": [],
            }
            with (
                patch.object(
                    ep,
                    "enrich_story_visual_evidence",
                    return_value={
                        "attempted": 1,
                        "completed": 1,
                        "errors": [],
                        "changed": False,
                    },
                ),
                patch.object(
                    ep,
                    "transcribe_downloaded_videos",
                    return_value={"attempted": 1, "completed": 1, "errors": []},
                ),
            ):
                result = ep.postprocess_story_run(
                    root,
                    "fixture",
                    run,
                    gemini_circuit={},
                    ollama_budget_state={},
                )

        self.assertFalse(result["postprocess_deferred"])
        self.assertEqual(result["visual_enrichment"]["completed"], 1)
        self.assertEqual(result["transcription"]["completed"], 1)
        self.assertIn("postprocess_ms", result["timings"])
        self.assertEqual(result["state"], "DONE")

    def test_postprocess_story_run_preserves_capture_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_path = root / "state" / "ephemeral" / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                '{"schema_version":1,"items":{}}',
                encoding="utf-8",
            )
            run = {
                "capture": {"visited_item_keys": []},
                "video_download": {"ok": False},
                "timings": {},
                "postprocess_deferred": True,
                "state": "DONE_WITH_ERRORS",
                "errors": ["yt-dlp: fixture failure"],
            }
            with (
                patch.object(
                    ep,
                    "enrich_story_visual_evidence",
                    return_value={
                        "attempted": 0,
                        "completed": 0,
                        "errors": [],
                        "changed": False,
                    },
                ),
                patch.object(
                    ep,
                    "transcribe_downloaded_videos",
                    return_value={"attempted": 0, "completed": 0, "errors": []},
                ),
            ):
                result = ep.postprocess_story_run(root, "fixture", run)

        self.assertEqual(result["state"], "DONE_WITH_ERRORS")
        self.assertIn("yt-dlp: fixture failure", result["errors"])


if __name__ == "__main__":
    unittest.main()
