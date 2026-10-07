from __future__ import annotations

import errno
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

    def test_missing_metadata_skips_ids_already_cached(self) -> None:
        entries = self._entries(3)
        cached = {"video_00": "2026-10-01T00:00:00+00:00"}
        seen: list[str] = []

        def fake_run(cmd, **kwargs):
            urls = [str(value) for value in cmd if str(value).startswith("https://")]
            seen.extend(url.rsplit("=", 1)[-1] for url in urls)
            return SimpleNamespace(
                returncode=0,
                stdout="\n".join(
                    json.dumps({
                        "id": url.rsplit("=", 1)[-1],
                        "timestamp": 1790611200,
                    })
                    for url in urls
                ),
                stderr="",
            )

        with patch.object(recent.subprocess, "run", side_effect=fake_run):
            resolved, diag = recent._youtube_probe_missing(
                entries,
                known_published_at=cached,
            )

        self.assertNotIn("video_00", seen)
        self.assertEqual(set(resolved), {"video_01", "video_02"})
        self.assertEqual(diag["attempted"], 2)
        self.assertEqual(diag["cached"], 1)

    def test_metadata_probe_retries_transient_process_spawn_failure(self) -> None:
        success = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "video_00", "timestamp": 1790611200}),
            stderr="",
        )
        transient = BlockingIOError(
            errno.EAGAIN,
            "Resource temporarily unavailable",
            "/usr/local/bin/python",
        )
        with (
            patch.object(recent.subprocess, "run", side_effect=[transient, success]) as run,
            patch.object(recent.time, "sleep") as sleep,
        ):
            resolved, diag = recent._probe_youtube_metadata_batch(
                ["https://www.youtube.com/watch?v=video_00"]
            )

        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(diag["returncode"], 0)
        self.assertIn("video_00", resolved)

    def test_surface_enumeration_retries_transient_process_spawn_failure(self) -> None:
        success = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "id": "abcdefghijk",
                "title": "fixture",
                "timestamp": 1790611200,
            }),
            stderr="",
        )
        transient = BlockingIOError(
            errno.EAGAIN,
            "Resource temporarily unavailable",
            "/usr/local/bin/python",
        )
        with (
            patch.object(recent.yte.subprocess, "run", side_effect=[transient, success]) as run,
            patch.object(recent.yte.time, "sleep") as sleep,
        ):
            entries, diag = recent.yte._enumerate_channel_surface(
                "https://www.youtube.com/@example",
                surface="videos",
                limit=1,
            )

        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(diag["returncode"], 0)
        self.assertEqual([entry["id"] for entry in entries], ["abcdefghijk"])

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

    def test_channel_surface_enumeration_honors_independent_limits(self) -> None:
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
            entries_by_surface, diag = recent.yte.enumerate_channel_surfaces(
                "https://www.youtube.com/@example",
                surface_limits={"videos": 9, "shorts": 2, "streams": 0},
            )

        self.assertEqual(sorted(calls), [("shorts", 2), ("videos", 9)])
        self.assertEqual(len(entries_by_surface["videos"]), 9)
        self.assertEqual(len(entries_by_surface["shorts"]), 2)
        self.assertEqual(entries_by_surface["streams"], [])
        self.assertEqual(diag["surface_limits"], {
            "videos": 9,
            "shorts": 2,
            "streams": 0,
        })

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

    def test_discover_youtube_reuses_probe_results_between_passes(self) -> None:
        cutoff = recent.parse_iso_utc("2026-09-30T00:00:00+00:00")
        end = recent.parse_iso_utc("2026-10-07T00:00:00+00:00")
        probe_calls: list[list[str]] = []
        enumeration_calls: list[dict[str, int]] = []

        def fake_enumerate_surfaces(url, *, surface_limits):
            enumeration_calls.append(dict(surface_limits))
            entries_by_surface = {
                "videos": [],
                "shorts": [],
                "streams": [],
            }
            surfaces = {}
            for surface in recent.yte.YOUTUBE_CHANNEL_SURFACES:
                requested = int(surface_limits.get(surface, 0))
                if requested <= 0:
                    surfaces[surface] = {
                        "surface": surface,
                        "returncode": 0,
                        "requested_limit": 0,
                        "entries_found": 0,
                    }
                    continue
                if surface == "videos":
                    count = 2 if requested <= 9 else 3
                    entries_by_surface[surface] = [
                        {
                            "id": f"video_{index:02d}",
                            "url": f"https://www.youtube.com/watch?v=video_{index:02d}",
                            "published_at": None,
                            "surface": "VIDEOS",
                            "title": "fixture",
                        }
                        for index in range(count)
                    ]
                    found = requested
                else:
                    found = 0
                surfaces[surface] = {
                    "surface": surface,
                    "returncode": 0,
                    "requested_limit": requested,
                    "entries_found": found,
                }
            return entries_by_surface, {
                "requested_limit": sum(surface_limits.values()),
                "surfaces": surfaces,
                "diagnostic_tail": "",
            }

        def fake_probe(entries, *, known_published_at=None):
            known_published_at = known_published_at or {}
            ids = [
                str(entry["id"])
                for entry in entries
                if str(entry["id"]) not in known_published_at
            ]
            probe_calls.append(ids)
            resolved = {
                video_id: (
                    "2026-10-01T00:00:00+00:00"
                    if video_id != "video_02"
                    else "2026-09-29T23:00:00+00:00"
                )
                for video_id in ids
            }
            return resolved, {
                "attempted": len(ids),
                "resolved": len(ids),
                "returncode": 0,
                "diagnostic_tail": "",
                "worker_count": 1 if ids else 0,
                "batch_count": 1 if ids else 0,
                "cached": len(known_published_at),
            }

        with (
            patch.object(
                recent.yte,
                "enumerate_channel_surfaces",
                side_effect=fake_enumerate_surfaces,
            ),
            patch.object(recent, "_youtube_probe_missing", side_effect=fake_probe),
            patch.object(recent, "YOUTUBE_MAX_DISCOVERY_PER_SURFACE", 50),
        ):
            result = recent.discover_youtube(
                {"creator_key": "dense"},
                {"profile_url": "https://www.youtube.com/@dense"},
                cutoff,
                end,
                25,
            )

        self.assertEqual(probe_calls[0], ["video_00", "video_01"])
        self.assertEqual(probe_calls[1], ["video_02"])
        self.assertTrue(result["window_complete"])
        self.assertEqual(result["metadata_probe"]["cumulative_resolved"], 3)
        self.assertEqual(set(enumeration_calls[0]), {"videos", "shorts", "streams"})
        self.assertEqual(set(enumeration_calls[1]), {"videos"})

    def test_youtube_dense_surface_expands_without_rescanning_sparse_surfaces(self) -> None:
        cutoff = recent.parse_iso_utc("2026-09-30T00:00:00+00:00")
        end = recent.parse_iso_utc("2026-10-07T00:00:00+00:00")
        calls: list[dict[str, int]] = []

        def fake_enumerate_surfaces(url, *, surface_limits):
            calls.append(dict(surface_limits))
            entries_by_surface = {
                "videos": [],
                "shorts": [],
                "streams": [],
            }
            surfaces = {}
            for surface in recent.yte.YOUTUBE_CHANNEL_SURFACES:
                requested = int(surface_limits.get(surface, 0))
                if requested <= 0:
                    surfaces[surface] = {
                        "surface": surface,
                        "returncode": 0,
                        "requested_limit": 0,
                        "entries_found": 0,
                    }
                    continue
                if surface == "videos":
                    published = (
                        "2026-09-29T23:00:00+00:00"
                        if requested >= 100
                        else "2026-10-01T00:00:00+00:00"
                    )
                    entries_by_surface[surface] = [{
                        "id": f"v{requested}",
                        "url": f"https://www.youtube.com/watch?v=v{requested}",
                        "published_at": published,
                        "surface": "VIDEOS",
                        "title": "fixture",
                    }]
                    found = requested
                else:
                    found = 0
                surfaces[surface] = {
                    "surface": surface,
                    "returncode": 0,
                    "requested_limit": requested,
                    "entries_found": found,
                }
            return entries_by_surface, {
                "requested_limit": sum(surface_limits.values()),
                "surfaces": surfaces,
                "diagnostic_tail": "",
            }

        with (
            patch.object(
                recent.yte,
                "enumerate_channel_surfaces",
                side_effect=fake_enumerate_surfaces,
            ),
            patch.object(
                recent,
                "_youtube_probe_missing",
                return_value=({}, {
                    "attempted": 0,
                    "resolved": 0,
                    "returncode": 0,
                    "diagnostic_tail": "",
                    "cached": 0,
                }),
            ),
            patch.object(recent, "YOUTUBE_MAX_DISCOVERY_PER_SURFACE", 200),
        ):
            result = recent.discover_youtube(
                {"creator_key": "dense"},
                {"profile_url": "https://www.youtube.com/@dense"},
                cutoff,
                end,
                25,
            )

        self.assertTrue(result["window_complete"])
        self.assertFalse(result["coverage_limit_reached"])
        self.assertIsNone(result["coverage_limited_reason"])
        video_limits = [call["videos"] for call in calls if "videos" in call]
        self.assertGreater(max(video_limits), 200 // 3)
        self.assertEqual(
            sum(1 for call in calls if "shorts" in call),
            1,
        )
        self.assertEqual(
            sum(1 for call in calls if "streams" in call),
            1,
        )
        self.assertEqual(result["surface_coverage"]["shorts"]["reason"], "SURFACE_EXHAUSTED")
        self.assertEqual(result["surface_coverage"]["streams"]["reason"], "SURFACE_EXHAUSTED")

    def test_youtube_dense_surface_reports_explicit_per_surface_cap_reason(self) -> None:
        cutoff = recent.parse_iso_utc("2026-09-30T00:00:00+00:00")
        end = recent.parse_iso_utc("2026-10-07T00:00:00+00:00")

        def fake_enumerate_surfaces(url, *, surface_limits):
            entries_by_surface = {
                "videos": [],
                "shorts": [],
                "streams": [],
            }
            surfaces = {}
            for surface in recent.yte.YOUTUBE_CHANNEL_SURFACES:
                requested = int(surface_limits.get(surface, 0))
                if requested <= 0:
                    surfaces[surface] = {
                        "surface": surface,
                        "returncode": 0,
                        "requested_limit": 0,
                        "entries_found": 0,
                    }
                    continue
                if surface == "videos":
                    entries_by_surface[surface] = [{
                        "id": f"v{requested}",
                        "url": f"https://www.youtube.com/watch?v=v{requested}",
                        "published_at": "2026-10-01T00:00:00+00:00",
                        "surface": "VIDEOS",
                        "title": "fixture",
                    }]
                    found = requested
                else:
                    found = 0
                surfaces[surface] = {
                    "surface": surface,
                    "returncode": 0,
                    "requested_limit": requested,
                    "entries_found": found,
                }
            return entries_by_surface, {
                "requested_limit": sum(surface_limits.values()),
                "surfaces": surfaces,
                "diagnostic_tail": "",
            }

        with (
            patch.object(
                recent.yte,
                "enumerate_channel_surfaces",
                side_effect=fake_enumerate_surfaces,
            ),
            patch.object(
                recent,
                "_youtube_probe_missing",
                return_value=({}, {
                    "attempted": 0,
                    "resolved": 0,
                    "returncode": 0,
                    "diagnostic_tail": "",
                    "cached": 0,
                }),
            ),
            patch.object(recent, "YOUTUBE_MAX_DISCOVERY_PER_SURFACE", 50),
        ):
            result = recent.discover_youtube(
                {"creator_key": "dense"},
                {"profile_url": "https://www.youtube.com/@dense"},
                cutoff,
                end,
                25,
            )

        self.assertFalse(result["window_complete"])
        self.assertTrue(result["coverage_limit_reached"])
        self.assertEqual(
            result["coverage_limited_reason"],
            "DISCOVERY_LIMIT_REACHED_BEFORE_CUTOFF",
        )
        self.assertEqual(
            result["surface_coverage"]["videos"]["reason"],
            "DISCOVERY_LIMIT_REACHED_BEFORE_CUTOFF",
        )
        self.assertTrue(result["surface_coverage"]["shorts"]["complete"])
        self.assertTrue(result["surface_coverage"]["streams"]["complete"])



if __name__ == "__main__":
    unittest.main()
