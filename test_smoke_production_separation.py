from __future__ import annotations

import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent


class SmokeProductionSeparationTests(unittest.TestCase):
    def test_production_sync_does_not_reuse_smoke_media(self) -> None:
        source = (BASE / "tiktok_camofox_sync.py").read_text(encoding="utf-8")
        for forbidden in (
            "adopt_poc_file",
            "poc_reuse",
            "reused_poc_files",
            "camofox_individual",
            "camofox_smoke",
        ):
            self.assertNotIn(forbidden, source)

    def test_smoke_uses_separate_entrypoint_and_fail_closed_decision(self) -> None:
        source = (BASE / "tiktok_camofox_smoke.py").read_text(encoding="utf-8")
        self.assertIn('SESSION_KEY = "nicholas-crown-smoke"', source)
        self.assertIn('TemporaryDirectory(prefix="influencerresearch-camofox-smoke-")', source)
        self.assertIn('return 0 if decision == "PASS_HYBRID" else 1', source)
        self.assertIn('"--",\n            url,', source)
        self.assertFalse((BASE / "camofox_tiktok_poc.py").exists())


if __name__ == "__main__":
    unittest.main()
