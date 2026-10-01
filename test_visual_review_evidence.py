from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_visual_ocr_timeout_is_nonfatal(self) -> None:
        with mock.patch("youtube_creator_evaluation.subprocess.run", side_effect=TimeoutError("timeout")):
            self.assertEqual(yte._ocr_visual_frame(Path("/tmp/frame.jpg")), "")

    def test_shared_visual_ocr_retries_block_layout_when_sparse_layout_is_empty(self) -> None:
        empty = mock.Mock(returncode=0, stdout="")
        caption = mock.Mock(returncode=0, stdout="Cheap oil doesn't mean cheap energy\n")
        with mock.patch("video_visual_evidence.subprocess.run", side_effect=[empty, caption]) as run:
            text = vve._ocr_visual_frame(Path("/tmp/frame.jpg"))
        self.assertEqual(text, "Cheap oil doesn't mean cheap energy")
        self.assertEqual(run.call_count, 2)
        self.assertIn("11", run.call_args_list[0].args[0])
        self.assertIn("6", run.call_args_list[1].args[0])

    def test_youtube_visual_ocr_retries_block_layout_when_sparse_layout_is_empty(self) -> None:
        empty = mock.Mock(returncode=0, stdout="")
        caption = mock.Mock(returncode=0, stdout="Cheap oil doesn't mean cheap energy\n")
        with mock.patch("youtube_creator_evaluation.subprocess.run", side_effect=[empty, caption]) as run:
            text = yte._ocr_visual_frame(Path("/tmp/frame.jpg"))
        self.assertEqual(text, "Cheap oil doesn't mean cheap energy")
        self.assertEqual(run.call_count, 2)
        self.assertIn("11", run.call_args_list[0].args[0])
        self.assertIn("6", run.call_args_list[1].args[0])

    def test_visual_ocr_does_not_retry_when_sparse_layout_succeeds(self) -> None:
        detected = mock.Mock(returncode=0, stdout="NASDAQ QQQ 500\n")
        with mock.patch("video_visual_evidence.subprocess.run", return_value=detected) as run:
            text = vve._ocr_visual_frame(Path("/tmp/frame.jpg"))
        self.assertEqual(text, "NASDAQ QQQ 500")
        self.assertEqual(run.call_count, 1)
        self.assertIn("11", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["env"]["OMP_THREAD_LIMIT"], "1")

    def test_transcript_visual_cue_skips_ocr_and_keeps_representative_frames(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = self._records(root, count=vve.VISUAL_OCR_MAX_FRAMES)
            with mock.patch.object(vve, "_ocr_visual_frame") as ocr, mock.patch.object(
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
            with mock.patch.object(
                vve,
                "_ocr_visual_frame",
                return_value="plain visual evidence",
            ) as ocr, mock.patch.object(vve, "_make_contact_sheet", return_value=None):
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

            with mock.patch.object(vve, "_ffmpeg_exe", return_value="ffmpeg"), mock.patch.object(
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
            ), mock.patch("video_visual_evidence.subprocess.run") as run:
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
            with mock.patch.object(
                vve,
                "_ocr_visual_frame",
                return_value="The futures contract called Heating Oil is actually",
            ), mock.patch.object(vve, "_make_contact_sheet", return_value=None):
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
            with mock.patch.object(
                yte,
                "_ocr_visual_frame",
                return_value="NASDAQ QQQ 500 resistance support 495 volume 1.8%",
            ), mock.patch.object(yte, "_make_contact_sheet", return_value=None):
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
            with mock.patch.object(
                yte,
                "_ocr_visual_frame",
                return_value="Welcome back everyone today we are discussing a general market topic",
            ), mock.patch.object(yte, "_make_contact_sheet", return_value=None):
                bundle = yte.build_agent_visual_bundle(
                    root, "nicholascrown", records, "ffmpeg", root
                )
        self.assertFalse(bundle["visual_review_recommended"])

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
            with mock.patch.object(yte, "_ffmpeg_exe", return_value="ffmpeg"), mock.patch.object(
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
            with mock.patch.object(yte, "_ocr_visual_frame", return_value="market update"), mock.patch.object(
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
        self.assertIn("include_binary: bool=False", rendered.replace(" ", ""))
        self.assertIn("binary_embedded", rendered)
        self.assertIn("EmbeddedResource", rendered)
        self.assertIn("BlobResourceContents", rendered)
        self.assertIn("video_file", rendered)


if __name__ == "__main__":
    unittest.main()
