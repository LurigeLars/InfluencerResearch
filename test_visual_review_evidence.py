from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import influencerresearch_mcp as irmcp
import youtube_creator_evaluation as yte
from mcp.types import ImageContent, TextContent


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


class EvidenceToolTests(unittest.TestCase):
    def test_analysis_evidence_get_returns_actual_image_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            state.mkdir()
            frame = root / "output" / "creator" / "frame.jpg"
            frame.parent.mkdir(parents=True)
            frame.write_bytes(b"jpeg-bytes")
            queue = {
                "items": [{
                    "queue_id": "yt_demo",
                    "creator": "creator",
                    "source_platform": "YOUTUBE",
                    "source_url": "https://www.youtube.com/watch?v=demo",
                    "analysis_mode_recommended": "TRANSCRIPT_PLUS_VISUAL_REVIEW",
                    "visual_review_recommended": True,
                    "visual_review_reason": ["PER_VIDEO_VISUAL_SIGNAL"],
                    "agent_visual_bundle": {
                        "creator_visual_prior": "NEUTRAL",
                        "chart_signal_ratio": 0.5,
                        "chart_signal_frame_count": 2,
                        "representative_frames": [{
                            "timestamp_s": 12.0,
                            "file": "output/creator/frame.jpg",
                            "visual_score": 4.0,
                            "visual_signals": ["CHART_TERMS"],
                            "ocr_text": "SPX resistance",
                        }],
                    },
                }]
            }
            (state / "research_queue.json").write_text(json.dumps(queue), encoding="utf-8")
            with mock.patch.object(irmcp, "ROOT", root), mock.patch.object(irmcp, "STATE_DIR", state):
                content = irmcp.analysis_evidence_get("yt_demo", max_frames=1)
        self.assertTrue(any(isinstance(block, TextContent) for block in content))
        images = [block for block in content if isinstance(block, ImageContent)]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].mime_type, "image/jpeg")


if __name__ == "__main__":
    unittest.main()
