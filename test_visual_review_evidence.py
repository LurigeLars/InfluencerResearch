from __future__ import annotations

import ast
import json
import tempfile
import unittest.mock
from pathlib import Path

import video_visual_evidence as vve
import youtube_creator_evaluation as yte


class VisualReviewClassifierTests(unittest.TestCase):
    def _records(self, root: Path, count: int = 4) -> list[dict]:
        rows = []
        for idx in range(count):
            path = root / f"frame_{idx}.jpg"
            path.write_bytes(b"fake-jpeg")
            rows.append({
                "timestamp_s": float(idx * 10),
                "reason": "SCENE_CHANGE" if idx % 2 else "ONE_FPS",
                "file": path,
                "size_bytes": path.stat().st_size,
            })
        return rows

    def test_youtube_visual_frame_budget_spans_known_duration(self) -> None:
        self.assertEqual(yte.VISUAL_CAPTURE_MAX_FRAMES_PER_BRANCH, 120)
        self.assertEqual(yte.visual_capture_sample_fps(60), 1.0)
        self.assertAlmostEqual(
            yte.visual_capture_sample_fps(720),
            120 / 720,
            places=6,
        )
        self.assertEqual(
            yte.visual_capture_sample_fps(None),
            yte.VISUAL_CAPTURE_FALLBACK_SAMPLE_FPS,
        )

    def test_youtube_visual_transport_prefers_direct_before_hls_formats(self) -> None:
        selector = yte.YOUTUBE_VISUAL_FORMAT_SELECTOR
        direct = "bv*[height<=720][protocol!=m3u8_native][protocol!=m3u8]"
        hls = "bv*[height<=720][protocol=m3u8_native]"
        self.assertIn(direct, selector)
        self.assertIn(hls, selector)
        self.assertLess(selector.index(direct), selector.index(hls))

    def test_youtube_seek_timestamps_are_bounded_and_spread(self) -> None:
        short = yte.visual_capture_seek_timestamps(60)
        long = yte.visual_capture_seek_timestamps(900)
        self.assertEqual(len(short), yte.VISUAL_CAPTURE_SEEK_MIN_FRAMES)
        self.assertEqual(len(long), yte.VISUAL_CAPTURE_SEEK_MAX_FRAMES)
        self.assertEqual(short, sorted(short))
        self.assertGreater(short[0], 0)
        self.assertLess(short[-1], 60)
        self.assertLess(long[-1], 900)

    def test_youtube_seeked_capture_does_not_persist_signed_stream_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence_dir = root / "output" / "creator" / "youtube" / "frames" / "vid001"
            evidence_dir.mkdir(parents=True)
            index_path = evidence_dir / "visual_index.json"
            progress: list[dict] = []
            signed_url = "https://media.example/video.m3u8?signature=secret"

            def fake_snapshot(
                _ffmpeg,
                stream_url,
                timestamp_s,
                output_path,
                *,
                http_headers=None,
            ):
                self.assertEqual(stream_url, signed_url)
                self.assertEqual(http_headers, {"User-Agent": "fixture-agent"})
                output_path.write_bytes(b"jpeg")
                return {"ok": True, "returncode": 0, "diagnostic_tail": ""}

            with unittest.mock.patch.object(
                yte,
                "_resolve_visual_stream_url",
                return_value={
                    "ok": True,
                    "returncode": 0,
                    "stream_url": signed_url,
                    "http_headers": {"User-Agent": "fixture-agent"},
                    "diagnostic_tail": "",
                    "js_runtime": {"enabled": True},
                },
            ), unittest.mock.patch.object(
                yte,
                "_capture_visual_snapshot",
                side_effect=fake_snapshot,
            ), unittest.mock.patch.object(
                yte,
                "build_agent_visual_bundle",
                return_value={
                    "available": True,
                    "analysis_mode_recommended": "TRANSCRIPT_PLUS_VISUAL_REVIEW",
                    "visual_review_recommended": True,
                    "visual_review_reason": ["CREATOR_CHART_PRIOR"],
                    "creator_visual_prior": "HIGH",
                    "representative_frames": [],
                },
            ):
                result = yte._capture_seeked_visual_evidence(
                    root,
                    "creator",
                    "https://www.youtube.com/watch?v=vid001",
                    "vid001",
                    "ffmpeg",
                    evidence_dir,
                    index_path,
                    duration_seconds=60,
                    progress_callback=progress.append,
                )

            persisted = index_path.read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["capture_strategy"], "SEEKED_TIMELINE_SNAPSHOTS")
        self.assertEqual(result["retained_frames"], yte.VISUAL_CAPTURE_SEEK_MIN_FRAMES)
        self.assertNotIn(signed_url, persisted)
        self.assertEqual(
            progress[-1]["visual_capture_seek_completed"],
            yte.VISUAL_CAPTURE_SEEK_MIN_FRAMES,
        )

    def test_youtube_seeked_capture_has_total_time_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence_dir = root / "frames"
            evidence_dir.mkdir()
            index_path = evidence_dir / "visual_index.json"
            with unittest.mock.patch.object(
                yte,
                "_resolve_visual_stream_url",
                return_value={
                    "ok": True,
                    "returncode": 0,
                    "stream_url": "https://media.example/video.mp4?signature=secret",
                    "diagnostic_tail": "",
                    "js_runtime": {"enabled": True},
                },
            ), unittest.mock.patch.object(
                yte.time,
                "monotonic",
                side_effect=[0.0, yte.VISUAL_CAPTURE_SEEK_TOTAL_TIMEOUT_SECONDS + 1.0],
            ), unittest.mock.patch.object(
                yte,
                "_capture_visual_snapshot",
            ) as snapshot:
                result = yte._capture_seeked_visual_evidence(
                    root,
                    "creator",
                    "https://www.youtube.com/watch?v=vid001",
                    "vid001",
                    "ffmpeg",
                    evidence_dir,
                    index_path,
                    duration_seconds=900,
                    progress_callback=None,
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "SEEKED_VISUAL_INSUFFICIENT_FRAMES")
        self.assertIn("SEEKED_VISUAL_TOTAL_TIMEOUT", result["diagnostic_tail"])
        snapshot.assert_not_called()

    def test_youtube_stream_resolver_keeps_safe_headers_in_memory(self) -> None:
        signed_url = "https://media.example/video.mp4?signature=secret"
        proc = unittest.mock.Mock(
            returncode=0,
            stdout=json.dumps({
                "url": signed_url,
                "http_headers": {
                    "User-Agent": "fixture-agent",
                    "Referer": "https://www.youtube.com/",
                    "Cookie": "secret-cookie",
                    "Authorization": "Bearer secret",
                    "Bad\\r\\nHeader": "ignored",
                },
            }),
            stderr="selected " + signed_url,
        )
        with unittest.mock.patch.object(
            yte,
            "_yt_base_args",
            return_value=(["yt-dlp"], {"enabled": True}),
        ), unittest.mock.patch(
            "youtube_creator_evaluation.subprocess.run",
            return_value=proc,
        ):
            result = yte._resolve_visual_stream_url(
                "https://www.youtube.com/watch?v=vid001"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["stream_url"], signed_url)
        self.assertEqual(
            result["http_headers"],
            {
                "User-Agent": "fixture-agent",
                "Referer": "https://www.youtube.com/",
            },
        )
        self.assertNotIn(signed_url, result["diagnostic_tail"])
        self.assertIn("<stream-url>", result["diagnostic_tail"])

    def test_youtube_snapshot_redacts_signed_stream_url_from_diagnostics(self) -> None:
        signed_url = "https://media.example/video.mp4?signature=secret"
        proc = unittest.mock.Mock(returncode=1, stderr=f"failed {signed_url}".encode())
        with unittest.mock.patch(
            "youtube_creator_evaluation.subprocess.run",
            return_value=proc,
        ) as run:
            result = yte._capture_visual_snapshot(
                "ffmpeg",
                signed_url,
                12.5,
                Path("/tmp/nonexistent-frame.jpg"),
                http_headers={
                    "User-Agent": "fixture-agent",
                    "Referer": "https://www.youtube.com/",
                },
            )
        self.assertFalse(result["ok"])
        self.assertNotIn(signed_url, result["diagnostic_tail"])
        self.assertIn("<stream-url>", result["diagnostic_tail"])
        cmd = run.call_args.args[0]
        self.assertIn("-headers", cmd)
        headers_arg = cmd[cmd.index("-headers") + 1]
        self.assertIn("User-Agent: fixture-agent\r\n", headers_arg)
        self.assertIn("Referer: https://www.youtube.com/\r\n", headers_arg)

    def test_visual_ocr_timeout_is_nonfatal(self) -> None:
        with unittest.mock.patch("youtube_creator_evaluation.subprocess.run", side_effect=TimeoutError("timeout")):
            self.assertEqual(yte._ocr_visual_frame(Path("/tmp/frame.jpg")), "")

    def test_shared_visual_ocr_retries_block_layout_when_sparse_layout_is_empty(self) -> None:
        empty = unittest.mock.Mock(returncode=0, stdout="")
        caption = unittest.mock.Mock(returncode=0, stdout="Cheap oil doesn't mean cheap energy\n")
        with unittest.mock.patch("video_visual_evidence.subprocess.run", side_effect=[empty, caption]) as run:
            text = vve._ocr_visual_frame(Path("/tmp/frame.jpg"))
        self.assertEqual(text, "Cheap oil doesn't mean cheap energy")
        self.assertEqual(run.call_count, 2)
        self.assertIn("11", run.call_args_list[0].args[0])
        self.assertIn("6", run.call_args_list[1].args[0])

    def test_youtube_visual_ocr_retries_block_layout_when_sparse_layout_is_empty(self) -> None:
        empty = unittest.mock.Mock(returncode=0, stdout="")
        caption = unittest.mock.Mock(returncode=0, stdout="Cheap oil doesn't mean cheap energy\n")
        with unittest.mock.patch("youtube_creator_evaluation.subprocess.run", side_effect=[empty, caption]) as run:
            text = yte._ocr_visual_frame(Path("/tmp/frame.jpg"))
        self.assertEqual(text, "Cheap oil doesn't mean cheap energy")
        self.assertEqual(run.call_count, 2)
        self.assertIn("11", run.call_args_list[0].args[0])
        self.assertIn("6", run.call_args_list[1].args[0])

    def test_visual_ocr_does_not_retry_when_sparse_layout_succeeds(self) -> None:
        detected = unittest.mock.Mock(returncode=0, stdout="NASDAQ QQQ 500\n")
        with unittest.mock.patch("video_visual_evidence.subprocess.run", return_value=detected) as run:
            text = vve._ocr_visual_frame(Path("/tmp/frame.jpg"))
        self.assertEqual(text, "NASDAQ QQQ 500")
        self.assertEqual(run.call_count, 1)
        self.assertIn("11", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["env"]["OMP_THREAD_LIMIT"], "1")

    def test_transcript_visual_cue_skips_ocr_and_keeps_representative_frames(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = self._records(root, count=vve.VISUAL_OCR_MAX_FRAMES)
            with unittest.mock.patch.object(vve, "_ocr_visual_frame") as ocr, unittest.mock.patch.object(
                vve, "_make_contact_sheet", return_value=None
            ):
                bundle = vve.build_agent_visual_bundle(
                    root,
                    "nicholascrown",
                    records,
                    "ffmpeg",
                    root,
                    transcript_text="You can see this on my screen right now.",
                )
        ocr.assert_not_called()
        self.assertEqual(bundle["sampled_frame_count"], vve.VISUAL_REPRESENTATIVE_FRAMES)
        self.assertEqual(bundle["ocr_sampled_frame_count"], 0)
        self.assertTrue(bundle["ocr_skipped"])
        self.assertEqual(bundle["ocr_worker_limit"], 0)
        self.assertEqual(
            bundle["ocr_skip_reason"],
            "POLICY_ALREADY_REQUIRES_VISUAL_REVIEW",
        )
        self.assertEqual(
            len(bundle["representative_frames"]),
            vve.VISUAL_REPRESENTATIVE_FRAMES,
        )
        self.assertTrue(bundle["visual_review_recommended"])
        self.assertIn("TRANSCRIPT_VISUAL_CUE", bundle["visual_review_reason"])

    def test_neutral_video_keeps_full_ocr_detection_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = self._records(root, count=vve.VISUAL_OCR_MAX_FRAMES)
            with unittest.mock.patch.object(
                vve,
                "_ocr_visual_frame",
                return_value="plain visual evidence",
            ) as ocr, unittest.mock.patch.object(vve, "_make_contact_sheet", return_value=None):
                bundle = vve.build_agent_visual_bundle(
                    root,
                    "nicholascrown",
                    records,
                    "ffmpeg",
                    root,
                    transcript_text="A general market discussion without visual cues.",
                )
        self.assertEqual(ocr.call_count, vve.VISUAL_OCR_MAX_FRAMES)
        self.assertFalse(bundle["ocr_skipped"])
        self.assertEqual(bundle["ocr_sampled_frame_count"], vve.VISUAL_OCR_MAX_FRAMES)
        self.assertEqual(bundle["ocr_worker_limit"], vve.VISUAL_OCR_WORKERS)

    def test_policy_refresh_reuses_existing_frames_without_ffmpeg_capture(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "output" / "nicholascrown" / "tiktok" / "videos" / "demo.mp4"
            evidence_dir = root / "output" / "nicholascrown" / "tiktok" / "frames" / "demo"
            media.parent.mkdir(parents=True)
            evidence_dir.mkdir(parents=True)
            media.write_bytes(b"video")
            frame = evidence_dir / "fps_00001.jpg"
            frame.write_bytes(b"jpeg")
            index_path = evidence_dir / "visual_index.json"
            index_path.write_text(json.dumps({
                "schema_version": 1,
                "visual_review_policy_version": vve.VISUAL_REVIEW_POLICY_VERSION - 1,
                "source_platform": "TIKTOK",
                "video_id": "demo",
                "summary": {
                    "candidate_frames": 1,
                    "retained_frames": 1,
                    "capture_strategy": "legacy",
                },
                "frames": [{
                    "timestamp_s": 1.0,
                    "reason": "ONE_FPS",
                    "file": str(frame.relative_to(root)),
                    "size_bytes": frame.stat().st_size,
                }],
            }), encoding="utf-8")

            with unittest.mock.patch.object(vve, "_ffmpeg_exe", return_value="ffmpeg"), unittest.mock.patch.object(
                vve,
                "build_agent_visual_bundle",
                return_value={
                    "available": True,
                    "analysis_mode_recommended": "TRANSCRIPT_ONLY",
                    "visual_review_recommended": False,
                    "visual_review_reason": [],
                    "creator_visual_prior": "NEUTRAL",
                    "representative_frames": [],
                },
            ), unittest.mock.patch("video_visual_evidence.subprocess.run") as run:
                result = vve.capture_local_video_visual_evidence(
                    root,
                    "nicholascrown",
                    "TIKTOK",
                    "demo",
                    media,
                    transcript_text="plain transcript",
                )

            updated = json.loads(index_path.read_text(encoding="utf-8"))

        run.assert_not_called()
        self.assertEqual(result["source"], "reused_frames_visual_policy_refresh")
        self.assertTrue(result["frames_reused_for_policy_refresh"])
        self.assertEqual(result["timings_ms"]["frame_capture"], 0.0)
        self.assertEqual(updated["visual_capture_version"], vve.VISUAL_CAPTURE_VERSION)
        self.assertEqual(
            updated["visual_review_policy_version"],
            vve.VISUAL_REVIEW_POLICY_VERSION,
        )

    def test_transcript_caption_terms_do_not_count_as_chart_signal(self) -> None:
        score, reasons = vve.score_visual_frame_text(
            "Gube futures contract called Heating Oilis aetually",
            transcript_text="The futures contract called heating oil is actually ultra low sulfur diesel.",
        )
        self.assertEqual(score, 0.0)
        self.assertEqual(reasons, ["TRANSCRIPT_CAPTION_OVERLAP"])

    def test_structured_market_data_survives_transcript_overlap_filter(self) -> None:
        score, reasons = vve.score_visual_frame_text(
            "QQQ support resistance 500 495 1.8%",
            transcript_text="QQQ support resistance is important here.",
        )
        self.assertGreaterEqual(score, 2.0)
        self.assertIn("NUMERIC_DENSITY", reasons)

    def test_shared_bundle_does_not_escalate_from_burned_in_captions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = self._records(root)
            transcript = "The futures contract called heating oil is actually ultra low sulfur diesel."
            with unittest.mock.patch.object(
                vve,
                "_ocr_visual_frame",
                return_value="The futures contract called Heating Oil is actually",
            ), unittest.mock.patch.object(vve, "_make_contact_sheet", return_value=None):
                bundle = vve.build_agent_visual_bundle(
                    root,
                    "nicholascrown",
                    records,
                    "ffmpeg",
                    root,
                    transcript_text=transcript,
                )
        self.assertFalse(bundle["visual_review_recommended"])
        self.assertEqual(bundle["chart_signal_frame_count"], 0)
        self.assertTrue(all(
            row["visual_signals"] == ["TRANSCRIPT_CAPTION_OVERLAP"]
            for row in bundle["representative_frames"]
        ))

    def test_nicholas_crown_escalates_per_video_when_frames_are_chart_heavy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = self._records(root)
            with unittest.mock.patch.object(
                yte,
                "_ocr_visual_frame",
                return_value="NASDAQ QQQ 500 resistance support 495 volume 1.8%",
            ), unittest.mock.patch.object(yte, "_make_contact_sheet", return_value=None):
                bundle = yte.build_agent_visual_bundle(
                    root, "nicholascrown", records, "ffmpeg", root
                )
        self.assertEqual(bundle["creator_visual_prior"], "NEUTRAL")
        self.assertTrue(bundle["visual_review_recommended"])
        self.assertIn("PER_VIDEO_VISUAL_SIGNAL", bundle["visual_review_reason"])

    def test_nicholas_crown_plain_video_does_not_force_visual_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = self._records(root)
            with unittest.mock.patch.object(
                yte,
                "_ocr_visual_frame",
                return_value="Welcome back everyone today we are discussing a general market topic",
            ), unittest.mock.patch.object(yte, "_make_contact_sheet", return_value=None):
                bundle = yte.build_agent_visual_bundle(
                    root, "nicholascrown", records, "ffmpeg", root
                )
        self.assertFalse(bundle["visual_review_recommended"])

    def test_youtube_visual_capture_monitor_enforces_memory_ceiling_and_heartbeats(self) -> None:
        class FakeProc:
            def __init__(self, pid: int) -> None:
                self.pid = pid
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as td:
            evidence_dir = Path(td)
            (evidence_dir / "fps_00001.jpg").write_bytes(b"jpeg")
            ff = FakeProc(1001)
            yt = FakeProc(1002)
            progress: list[dict] = []
            with unittest.mock.patch.object(
                yte,
                "_process_tree_rss_bytes",
                return_value=400 * 1024 * 1024,
            ):
                result = yte._wait_visual_capture(
                    ff,
                    yt,
                    evidence_dir,
                    progress_callback=progress.append,
                    memory_limit_bytes=512 * 1024 * 1024,
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "VISUAL_CAPTURE_MEMORY_LIMIT")
        self.assertGreaterEqual(result["child_rss_peak_mib"], 800.0)
        self.assertTrue(ff.terminated)
        self.assertTrue(yt.terminated)
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]["visual_capture_frame_files"], 1)
        self.assertGreaterEqual(progress[0]["visual_capture_child_rss_mib"], 800.0)

    def test_youtube_visual_bundle_reports_ocr_progress(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = self._records(root, count=4)
            progress: list[dict] = []
            with unittest.mock.patch.object(
                yte,
                "_ocr_visual_frame",
                return_value="NASDAQ QQQ 500 support resistance 495",
            ), unittest.mock.patch.object(yte, "_make_contact_sheet", return_value=None):
                bundle = yte.build_agent_visual_bundle(
                    root,
                    "nicholascrown",
                    records,
                    "ffmpeg",
                    root,
                    progress_callback=progress.append,
                )

        ocr_events = [
            event for event in progress
            if event.get("visual_postprocess_phase") == "OCR"
        ]
        self.assertEqual(ocr_events[0]["visual_ocr_completed"], 0)
        self.assertEqual(ocr_events[-1]["visual_ocr_completed"], 4)
        self.assertEqual(ocr_events[-1]["visual_ocr_total"], 4)
        self.assertEqual(progress[-2]["visual_postprocess_phase"], "CONTACT_SHEET")
        self.assertEqual(progress[-1]["visual_postprocess_phase"], "DONE")
        self.assertTrue(bundle["available"])

    def test_existing_visual_index_backfills_agent_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence_dir = root / "output" / "nicholascrown" / "youtube" / "frames" / "demo"
            evidence_dir.mkdir(parents=True)
            frame = evidence_dir / "fps_00001.jpg"
            frame.write_bytes(b"fake-jpeg")
            index = {
                "summary": {"retained_frames": 1, "capture_strategy": "existing"},
                "frames": [{
                    "timestamp_s": 1.0,
                    "reason": "ONE_FPS",
                    "file": str(frame.relative_to(root)),
                    "size_bytes": frame.stat().st_size,
                }],
            }
            (evidence_dir / "visual_index.json").write_text(json.dumps(index), encoding="utf-8")
            with unittest.mock.patch.object(yte, "_ffmpeg_exe", return_value="ffmpeg"), unittest.mock.patch.object(
                yte, "build_agent_visual_bundle",
                return_value={"available": True, "visual_review_recommended": True},
            ):
                result = yte.capture_visual_evidence(
                    root,
                    "nicholascrown",
                    "https://www.youtube.com/watch?v=demo",
                    "demo",
                )
            updated = json.loads((evidence_dir / "visual_index.json").read_text(encoding="utf-8"))
        self.assertTrue(result["agent_visual_bundle"]["available"])
        self.assertTrue(updated["agent_visual_bundle"]["visual_review_recommended"])

    def test_trading_fraternity_has_high_creator_prior(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = self._records(root)
            with unittest.mock.patch.object(yte, "_ocr_visual_frame", return_value="market update"), unittest.mock.patch.object(
                yte, "_make_contact_sheet", return_value=None
            ):
                bundle = yte.build_agent_visual_bundle(
                    root, "thetradingfraternity", records, "ffmpeg", root
                )
        self.assertEqual(bundle["creator_visual_prior"], "HIGH")
        self.assertTrue(bundle["visual_review_recommended"])
        self.assertIn("CREATOR_CHART_PRIOR", bundle["visual_review_reason"])


class EvidenceToolContractTests(unittest.TestCase):
    def test_analysis_evidence_get_returns_image_content_contract(self) -> None:
        source = Path("influencerresearch_mcp.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "analysis_evidence_get"
        )
        rendered = ast.unparse(fn)
        self.assertIn("ImageContent", rendered)
        self.assertIn("base64.b64encode", rendered)
        self.assertIn("REPRESENTATIVE_FRAMES", rendered)
        self.assertIn("CONTACT_SHEET", rendered)
        self.assertIn("max_frames", rendered)
        self.assertIn("screenshot_file", rendered)
        self.assertIn("STORY_SCREENSHOT", rendered)
        self.assertIn("RAW_MEDIA", rendered)
        self.assertIn("include_binary", rendered)
        self.assertIn("include_binary:bool=False", rendered.replace(" ", ""))
        self.assertIn("binary_embedded", rendered)
        self.assertIn("RAW_MEDIA_BINARY_INLINE_DISABLED", rendered)
        self.assertNotIn("EmbeddedResource", rendered)
        self.assertNotIn("BlobResourceContents", rendered)
        self.assertIn("video_file", rendered)


if __name__ == "__main__":
    unittest.main()
