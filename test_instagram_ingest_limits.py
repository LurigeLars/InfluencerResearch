from __future__ import annotations

import unittest

from instagram_ingest import resolve_max_new_per_creator, select_transcription_keys


class InstagramIngestLimitTests(unittest.TestCase):
    def test_max_new_override_wins(self) -> None:
        self.assertEqual(
            resolve_max_new_per_creator({"max_new_per_creator": 10}, 1),
            1,
        )

    def test_max_new_rejects_non_positive_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 1"):
            resolve_max_new_per_creator({"max_new_per_creator": 10}, 0)

    def test_new_only_transcription_excludes_old_pending_items(self) -> None:
        manifest = {
            "items": {
                "old": {
                    "download_status": "DONE",
                    "video_file": "output/x/old.mp4",
                    "transcription_status": "PENDING",
                },
                "new": {
                    "download_status": "DONE",
                    "video_file": "output/x/new.mp4",
                    "transcription_status": "PENDING",
                },
            }
        }
        summaries = [{"creator": "x", "new_keys": ["new"]}]
        self.assertEqual(
            select_transcription_keys(manifest, summaries, new_only=True),
            ["new"],
        )

    def test_default_transcription_includes_all_pending_items(self) -> None:
        manifest = {
            "items": {
                "old": {
                    "download_status": "DONE",
                    "video_file": "output/x/old.mp4",
                    "transcription_status": "PENDING",
                },
                "done": {
                    "download_status": "DONE",
                    "video_file": "output/x/done.mp4",
                    "transcription_status": "DONE",
                },
            }
        }
        self.assertEqual(
            select_transcription_keys(manifest, [], new_only=False),
            ["old"],
        )


if __name__ == "__main__":
    unittest.main()
