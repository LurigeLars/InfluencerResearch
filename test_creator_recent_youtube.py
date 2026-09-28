from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import creator_recent_check as recent


class YouTubeMetadataProbeTests(unittest.TestCase):
    @staticmethod
    def _entries(count: int) -> list[dict]:
        return [
            {
                "id": f"video_{index:02d}",
                "url": f"https://www.youtube.com/watch?v=video_{index:02d}",
                "published_at": None,
            }
            for index in range(count)
        ]

    def test_missing_metadata_is_bounded_parallel_and_complete(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            urls = [str(value) for value in cmd if str(value).startswith("https://")]
            calls.append(urls)
            stdout = "\n".join(
                json.dumps({
                    "id": url.rsplit("=", 1)[-1],
                    "timestamp": 1790611200,
                })
                for url in urls
            )
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        entries = self._entries(12)
        with patch.object(recent.subprocess, "run", side_effect=fake_run):
            resolved, diag = recent._youtube_probe_missing(entries)

        expected_urls = sorted(str(entry["url"]) for entry in entries)
        self.assertEqual(sorted(url for batch in calls for url in batch), expected_urls)
        self.assertEqual(len(calls), recent.YOUTUBE_METADATA_PROBE_WORKERS)
        self.assertEqual(len(resolved), len(entries))
        self.assertEqual(diag["attempted"], len(entries))
        self.assertEqual(diag["resolved"], len(entries))
        self.assertEqual(diag["worker_count"], recent.YOUTUBE_METADATA_PROBE_WORKERS)
        self.assertEqual(diag["batch_count"], recent.YOUTUBE_METADATA_PROBE_WORKERS)
        self.assertEqual(diag["returncode"], 0)

    def test_successful_batches_survive_one_timeout(self) -> None:
        def fake_run(cmd, **kwargs):
            urls = [str(value) for value in cmd if str(value).startswith("https://")]
            if any(url.endswith("video_00") for url in urls):
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=240)
            stdout = "\n".join(
                json.dumps({
                    "id": url.rsplit("=", 1)[-1],
                    "timestamp": 1790611200,
                })
                for url in urls
            )
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        entries = self._entries(12)
        with patch.object(recent.subprocess, "run", side_effect=fake_run):
            resolved, diag = recent._youtube_probe_missing(entries)

        self.assertEqual(diag["attempted"], len(entries))
        self.assertEqual(diag["returncode"], 124)
        self.assertGreater(len(resolved), 0)
        self.assertLess(len(resolved), len(entries))
        self.assertIn(124, diag["batch_returncodes"])


if __name__ == "__main__":
    unittest.main()
