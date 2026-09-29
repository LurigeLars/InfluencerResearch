from __future__ import annotations

import unittest

import video_visual_evidence as vve


class VideoVisualEvidencePolicyTests(unittest.TestCase):
    def test_nicholas_crown_plain_video_stays_transcript_only(self) -> None:
        result = vve.classify_visual_review(
            "nicholascrown",
            [{"visual_score": 0.5}, {"visual_score": 1.0}],
        )
        self.assertFalse(result["visual_review_recommended"])
        self.assertEqual(result["analysis_mode_recommended"], "TRANSCRIPT_ONLY")
        self.assertEqual(result["creator_visual_prior"], "NEUTRAL")

    def test_nicholas_crown_chart_video_escalates_from_current_frames(self) -> None:
        result = vve.classify_visual_review(
            "nicholascrown",
            [{"visual_score": 4.5}, {"visual_score": 2.2}, {"visual_score": 0.1}],
        )
        self.assertTrue(result["visual_review_recommended"])
        self.assertEqual(result["analysis_mode_recommended"], "TRANSCRIPT_PLUS_VISUAL_REVIEW")
        self.assertIn("PER_VIDEO_VISUAL_SIGNAL", result["visual_review_reason"])
        self.assertEqual(result["creator_visual_prior"], "NEUTRAL")

    def test_nicholas_crown_transcript_visual_cue_escalates_current_video(self) -> None:
        result = vve.classify_visual_review(
            "nicholascrown",
            [{"visual_score": 0.2}],
            transcript_text="I just showed you on my screen right there.",
        )
        self.assertTrue(result["visual_review_recommended"])
        self.assertIn("TRANSCRIPT_VISUAL_CUE", result["visual_review_reason"])
        self.assertEqual(result["creator_visual_prior"], "NEUTRAL")

    def test_trading_fraternity_prior_never_skips_frame_sampling(self) -> None:
        result = vve.classify_visual_review(
            "thetradingfraternity",
            [{"visual_score": 0.0}],
        )
        self.assertTrue(result["visual_review_recommended"])
        self.assertEqual(result["sampled_frame_count"], 1)
        self.assertIn("CREATOR_CHART_PRIOR", result["visual_review_reason"])

    def test_market_chart_text_scores_as_visual_signal(self) -> None:
        score, signals = vve.score_visual_frame_text(
            "SPX 6025 resistance 6100 support 5960 RSI 68.4 volume +12%"
        )
        self.assertGreaterEqual(score, 2.0)
        self.assertIn("CHART_TERMS", signals)
        self.assertIn("NUMERIC_DENSITY", signals)


if __name__ == "__main__":
    unittest.main()