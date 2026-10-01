from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import creator_recent_check as recent


class YouTubeIngestionDiagnosticsTests(unittest.TestCase):
    def test_ingest_youtube_returns_failure_stage_and_detail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = recent.Path(td)
            status_path = root / "state" / "creator_evaluation_status.json"
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(
                json.dumps({
                    "state": "FAILED",
                    "completed": [],
                    "failure_count": 1,
                    "failures": [{
                        "video_id": "abc123",
                        "stage": "visual_capture",
                        "detail": "fixture capture failure",
                    }],
                }),
                encoding="utf-8",
            )
            with patch.object(
                recent.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=2),
            ):
                result = recent._ingest_youtube(root, "creator", ["abc123"])

        self.assertEqual(result["returncode"], 2)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["failures"][0]["stage"], "visual_capture")
        self.assertEqual(result["failures"][0]["detail"], "fixture capture failure")


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

    def test_channel_enumeration_respects_one_global_budget(self) -> None:
        calls: list[tuple[str, int]] = []

        def fake_surface(channel_url, *, surface, limit):
            calls.append((surface, limit))
            entries = [
                {
                    "id": f"{surface}_{index:02d}",
                    "url": f"https://www.youtube.com/watch?v={surface}_{index:02d}",
                    "title": surface,
                    "published_at": None,
                    "surface": surface.upper(),
                }
                for index in range(limit)
            ]
            return entries, {
                "surface": surface,
                "ok": True,
                "returncode": 0,
                "requested_limit": limit,
                "entries_found": len(entries),
                "diagnostic_tail": "",
            }

        with patch.object(recent.yte, "_enumerate_channel_surface", side_effect=fake_surface):
            entries, diag = recent.yte.enumerate_channel(
                "https://www.youtube.com/@example",
                limit=15,
            )

        self.assertEqual(len(entries), 15)
        self.assertEqual(diag["requested_limit"], 15)
        self.assertEqual(diag["surface_limits"], {
            "videos": 5,
            "shorts": 5,
            "streams": 5,
        })
        self.assertEqual(sorted(calls), [
            ("shorts", 5),
            ("streams", 5),
            ("videos", 5),
        ])
        self.assertEqual(
            {entry["surface"] for entry in entries},
            {"VIDEOS", "SHORTS", "STREAMS"},
        )

    def test_surface_coverage_does_not_stop_when_one_full_surface_is_still_recent(self) -> None:
        cutoff = recent.parse_iso_utc("2026-09-28T00:00:00+00:00")
        entries = [
            {
                "id": f"video_{index}",
                "surface": "VIDEOS",
                "published_at": "2026-09-28T12:00:00+00:00",
            }
            for index in range(5)
        ]
        diag = {
            "requested_limit": 15,
            "surfaces": {
                "videos": {
                    "returncode": 0,
                    "requested_limit": 5,
                    "entries_found": 5,
                },
                "shorts": {
                    "returncode": 0,
                    "requested_limit": 5,
                    "entries_found": 0,
                },
                "streams": {
                    "returncode": 0,
                    "requested_limit": 5,
                    "entries_found": 0,
                },
            },
        }
        self.assertFalse(
            recent._youtube_surface_coverage_complete(entries, diag, {}, cutoff)
        )
        entries[-1]["published_at"] = "2026-09-27T23:00:00+00:00"
        self.assertTrue(
            recent._youtube_surface_coverage_complete(entries, diag, {}, cutoff)
        )

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
