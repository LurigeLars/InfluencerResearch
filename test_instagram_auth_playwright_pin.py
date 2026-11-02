"""Prevent host Instagram-auth bootstrap from drifting from IR's Playwright pin.

Static checks only: no Instagram requests, browser launches or authentication.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent


class InstagramAuthPlaywrightPinTests(unittest.TestCase):
    def test_instagram_auth_uses_exact_runtime_playwright_pin(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        pins = re.findall(r"(?m)^playwright==([0-9]+\.[0-9]+\.[0-9]+)\s*$", requirements)
        self.assertEqual(len(pins), 1, "requirements.txt must have one exact Playwright pin")

        script = (ROOT / "scripts" / "authenticate_instagram.ps1").read_text(encoding="utf-8")
        self.assertIn('$RequirementsPath = Join-Path $App "requirements.txt"', script)
        self.assertIn('$PlaywrightPins.Count -ne 1', script)
        self.assertIn('"playwright==$ExpectedPlaywrightVersion"', script)
        self.assertIn('$InstalledPlaywrightVersion.Trim() -ne $ExpectedPlaywrightVersion', script)
        self.assertNotRegex(script, r'playwright==\d+\.\d+\.\d+')

    def test_instagram_pause_policy_remains_operator_controlled(self) -> None:
        policy = (ROOT / "instagram_policy.py").read_text(encoding="utf-8")
        self.assertIn("INSTAGRAM_PAUSED_BY_OPERATOR", policy)


if __name__ == "__main__":
    unittest.main()
