"""Regression coverage for assigning monitored YouTube clips from adhoc evidence."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import creator_recent_check as crc


class YoutubeAttributionTests(unittest.TestCase):
    def _root(self, td):
        root = Path(td)
        (root / "state").mkdir(parents=True)
        (root / "output" / "adhoc" / "youtube").mkdir(parents=True)
        transcript = root / "output" / "adhoc" / "youtube" / "clip.txt"
        transcript.write_text("Verified transcript from the original source")
        return root, transcript

    def test_verified_ad_hoc_video_is_reattributed_with_preserved_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            root, transcript = self._root(td)
            original = {
                "source_platform": "YOUTUBE",
                "source_id": "YSfXxO5P2II",
                "creator": "adhoc30d778bf61cb",
                "evaluation_mode": "SINGLE_VIDEO_URL",
                "permanent_source": False,
                "download_status": "DONE",
                "transcription_status": "DONE",
                "transcript_txt": str(transcript.relative_to(root)),
                "transcript_source": "FASTER_WHISPER_FALLBACK",
                "research_status": "PENDING",
            }
            path = root / "state" / "manifest.json"
            path.write_text(json.dumps({"items": {"yt_YSfXxO5P2II": original}}))
            result = crc._reattribute_monitored_youtube_evidence(root, [{
                "platform": "YOUTUBE",
                "source_id": "YSfXxO5P2II",
                "creator_key": "thetradingfraternity",
                "published_at": "2026-10-13T23:53:31+00:00",
            }])
            updated = json.loads(path.read_text())["items"]["yt_YSfXxO5P2II"]
            self.assertEqual(result["updated"], ["yt_YSfXxO5P2II"])
            self.assertEqual(updated["creator"], "thetradingfraternity")
            self.assertTrue(updated["permanent_source"])
            self.assertEqual(updated["evaluation_mode"], "RECENT_MONITORING")
            self.assertEqual(updated["transcript_txt"], original["transcript_txt"])
            self.assertEqual(updated["transcript_source"], original["transcript_source"])
            self.assertEqual(updated["published_at"], "2026-10-13T23:53:31+00:00")

    def test_refuses_reassignment_from_another_registered_creator(self):
        with tempfile.TemporaryDirectory() as td:
            root, transcript = self._root(td)
            path = root / "state" / "manifest.json"
            entry = {
                "source_platform": "YOUTUBE", "creator": "matfinog",
                "download_status": "DONE", "transcription_status": "DONE",
                "transcript_txt": str(transcript.relative_to(root)),
                "evaluation_mode": "RECENT_MONITORING",
                "permanent_source": True,
            }
            path.write_text(json.dumps({"items": {"yt_YSfXxO5P2II": entry}}))
            result = crc._reattribute_monitored_youtube_evidence(root, [{
                "platform": "YOUTUBE", "source_id": "YSfXxO5P2II",
                "creator_key": "thetradingfraternity",
            }])
            self.assertEqual(result["conflicts"], ["yt_YSfXxO5P2II"])
            self.assertEqual(
                json.loads(path.read_text())["items"]["yt_YSfXxO5P2II"]["creator"],
                "matfinog",
            )

    def test_missing_transcript_does_not_claim_completed_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root, _ = self._root(td)
            path = root / "state" / "manifest.json"
            path.write_text(json.dumps({"items": {
                "yt_YSfXxO5P2II": {
                    "source_platform": "YOUTUBE",
                    "creator": "adhoc123",
                    "evaluation_mode": "SINGLE_VIDEO_URL",
                    "permanent_source": False,
                    "download_status": "DONE", "transcription_status": "DONE",
                    "transcript_txt": "missing.txt",
                },
            }}))
            result = crc._reattribute_monitored_youtube_evidence(root, [{
                "platform": "YOUTUBE", "source_id": "YSfXxO5P2II",
                "creator_key": "thetradingfraternity",
            }])
            self.assertEqual(result["updated"], [])
            self.assertFalse(
                json.loads(path.read_text())["items"]["yt_YSfXxO5P2II"]["permanent_source"]
            )


if __name__ == "__main__":
    unittest.main()
