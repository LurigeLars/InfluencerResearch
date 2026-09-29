from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
