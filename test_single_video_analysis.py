from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import single_video_analysis as video
import mcp_job_worker


class VideoUrlAnalysisTests(unittest.TestCase):
    def test_youtube_urls_normalize_to_exact_video_id(self) -> None:
        examples = (
            "https://youtu.be/AbCdEf123_0?t=5",
            "https://www.youtube.com/shorts/AbCdEf123_0",
            "https://youtube.com/watch?v=AbCdEf123_0&list=PLfake",
            "https://m.youtube.com/live/AbCdEf123_0",
        )
        for candidate in examples:
            with self.subTest(url=candidate):
                self.assertEqual(
                    video.canonical_video_url(candidate),
                    ("YOUTUBE", "https://www.youtube.com/watch?v=AbCdEf123_0", "AbCdEf123_0", None),
                )

    def test_tiktok_video_is_exact_and_does_not_require_registration(self) -> None:
        self.assertEqual(
            video.canonical_video_url(
                "https://www.tiktok.com/@some.creator/video/7123456789012345678?is_from_webapp=1"
            ),
            (
                "TIKTOK",
                "https://www.tiktok.com/@some.creator/video/7123456789012345678",
                "7123456789012345678",
                "some.creator",
            ),
        )

    def test_rejects_hosts_redirectors_profiles_and_untrusted_paths(self) -> None:
        invalid = (
            "http://www.youtube.com/watch?v=AbCdEf123_0",
            "https://evil.example/watch?v=AbCdEf123_0",
            "https://youtube.com.evil.example/watch?v=AbCdEf123_0",
            "https://youtube.com@127.0.0.1/watch?v=AbCdEf123_0",
            "https://127.0.0.1/watch?v=AbCdEf123_0",
            "https://www.youtube.com:8443/watch?v=AbCdEf123_0",
            "https://www.youtube.com/@somebody/videos",
            "https://www.youtube.com/playlist?list=PL123",
            "https://vm.tiktok.com/short",
            "https://www.tiktok.com/@creator",
            "https://www.tiktok.com/@creator/video/invalid",
            "https://www.instagram.com/reel/xyz",
            "https://www.youtube.com/watch?v=bad%2fid",
            "https://www.youtube.com/watch?v=AbCdEf123_0#fragment",
            "",
        )
        for candidate in invalid:
            with self.subTest(url=candidate):
                with self.assertRaises(ValueError):
                    video.canonical_video_url(candidate)

    def test_worker_invokes_fixed_script_without_profile_discovery(self) -> None:
        request = {
            "kind": "video_url_analyze",
            "params": {
                "video_url": "https://www.youtube.com/watch?v=AbCdEf123_0",
                "source_platform": "YOUTUBE",
            },
        }
        path, args = mcp_job_worker.build_invocation(request)
        self.assertEqual(path.name, "single_video_analysis.py")
        self.assertEqual(
            args,
            [
                "--root", str(mcp_job_worker.ROOT), "--video-url",
                "https://www.youtube.com/watch?v=AbCdEf123_0",
            ],
        )
        request["params"]["source_platform"] = "TIKTOK"
        with self.assertRaisesRegex(RuntimeError, "VIDEO_PLATFORM_MISMATCH"):
            mcp_job_worker.build_invocation(request)

    def test_youtube_reuses_verified_exact_id_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status = root / "state" / video.STATUS_NAME
            status.parent.mkdir()
            status.write_text('{"state":"RUNNING"}', encoding="utf-8")

            fake = types.ModuleType("youtube_creator_evaluation")
            fake.__file__ = "/safe/youtube_creator_evaluation.py"
            fake._yt_base_args = lambda: (["yt-dlp"], {})
            fake.load_json = lambda path, default: json.loads(path.read_text())
            def fake_main() -> int:
                argv = sys.argv[:]
                self.assertIn("--only-video-ids", argv)
                self.assertEqual(argv[argv.index("--only-video-ids") + 1], "AbCdEf123_0")
                self.assertIn("--status-path", argv)
                self.assertEqual(argv[argv.index("--evaluation-mode") + 1], "SINGLE_VIDEO_URL")
                status.write_text(
                    json.dumps({
                        "state": "COMPLETE", "completed_count": 1,
                        "delivery_dispositions": {"yt_AbCdEf123_0": "QUEUED"},
                    }),
                    encoding="utf-8",
                )
                return 0
            fake.main = fake_main
            proc = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=json.dumps({
                    "id": "AbCdEf123_0",
                    "channel_id": "UC" + "a" * 22,
                    "channel": "Example",
                }),
                stderr="",
            )
            with patch.dict(sys.modules, {"youtube_creator_evaluation": fake}), patch.object(
                video.subprocess, "run", return_value=proc
            ):
                result = video._youtube(root, "https://www.youtube.com/watch?v=AbCdEf123_0", "AbCdEf123_0", status)
            self.assertEqual(result["state"], "COMPLETE")
            self.assertEqual(result["queue_id"], "yt_AbCdEf123_0")
            self.assertEqual(result["disposition"], "QUEUED")

    def test_tiktok_is_preseeded_and_restricted_to_one_video(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status = root / "state" / video.STATUS_NAME
            status.parent.mkdir(parents=True)
            status.write_text('{"state":"RUNNING"}', encoding="utf-8")
            observed = {}

            fake_tiktok = types.ModuleType("tiktok_camofox_sync")
            fake_tiktok.APP_VERSION = "fixture"
            fake_tiktok.load_json = lambda path, default: (
                json.loads(path.read_text()) if path.exists() else default
            )
            def atomic(path, data):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data), encoding="utf-8")
            fake_tiktok.atomic_json = atomic
            def merge(catalog, urls, *, profile_url):
                self.assertEqual(len(urls), 1)
                catalog["items"][urls[0].split("/")[-1]] = {"url": urls[0]}
                catalog["order"] = [urls[0].split("/")[-1]]
                return catalog
            fake_tiktok.merge_catalog = merge
            def process(root_dir, source, **kwargs):
                observed.update(kwargs)
                atomic(root_dir / "state" / "manifest.json", {
                    "schema_version": 1,
                    "items": {"tt_7123456789012345678": {
                        "transcription_status": "DONE", "download_status": "DONE",
                    }},
                })
                return {"completed_new": 1, "failures": []}
            fake_tiktok.process_source = process

            fake_youtube = types.ModuleType("youtube_creator_evaluation")
            def reconcile(root_dir, ids):
                self.assertEqual(ids, ["tt_7123456789012345678"])
                return {"ok": True}, {}, {
                    "dispositions": {"tt_7123456789012345678": "QUEUED"}
                }
            fake_youtube.reconcile_delivery = reconcile

            with patch.dict(sys.modules, {
                "tiktok_camofox_sync": fake_tiktok,
                "youtube_creator_evaluation": fake_youtube,
            }):
                result = video._tiktok(
                    root, "https://www.tiktok.com/@creator/video/7123456789012345678",
                    "7123456789012345678", "creator", status,
                )
            self.assertEqual(result["state"], "COMPLETE")
            self.assertEqual(observed["include_video_ids"], {"7123456789012345678"})
            self.assertEqual(observed["max_new_override"], 1)
            self.assertEqual(observed["discovery_target_override"], 1)
            manifest = json.loads((root / "state" / "manifest.json").read_text())
            item = manifest["items"]["tt_7123456789012345678"]
            self.assertEqual(item["evaluation_mode"], "SINGLE_VIDEO_URL")
            self.assertFalse(item["permanent_source"])


if __name__ == "__main__":
    unittest.main()
