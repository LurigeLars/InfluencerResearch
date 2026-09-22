from __future__ import annotations

import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent


class TikTokCamofoxMediaReadinessTests(unittest.TestCase):
    def test_media_probe_polls_and_requeries_video_element(self) -> None:
        text = (BASE / 'tiktok_camofox_sync.py').read_text(encoding='utf-8')
        self.assertIn('const readinessDeadline = Date.now() + 12000;', text)
        self.assertIn('while (Date.now() < readinessDeadline)', text)
        self.assertGreaterEqual(text.count("v = document.querySelector('video');"), 2)
        self.assertIn('v.readyState >= 2', text)
        self.assertIn('video element missing after bounded readiness wait', text)
        self.assertNotIn('await new Promise(r => setTimeout(r, 3500));', text)


if __name__ == '__main__':
    unittest.main()
