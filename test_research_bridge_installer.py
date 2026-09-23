from __future__ import annotations

import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent


class ResearchBridgeInstallerTests(unittest.TestCase):
    def test_simple_installer_replaces_legacy_recovery_harness(self) -> None:
        path = BASE / "scripts" / "research_bridge.ps1"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8-sig")

        self.assertIn('ValidateSet("Install", "Uninstall", "Status")', text)
        self.assertIn("Register-ScheduledTask", text)
        self.assertIn("Start-ScheduledTask", text)
        self.assertIn("Stop-ScheduledTask", text)
        self.assertIn("Unregister-ScheduledTask", text)
        self.assertIn("--prepare-state-upgrade", text)
        self.assertIn("--commit-state-upgrade", text)
        self.assertIn("health_status", text)
        self.assertIn("RESEARCH_BRIDGE_READY", text)

        for forbidden in (
            "BACKLOG_051",
            "RRRA_V1",
            "recovery.guard.lock",
            "backlog_051_",
            "ExecutionPolicy",
            "taskkill.exe",
            "$env:SystemRoot",
        ):
            self.assertNotIn(forbidden, text)

    def test_numbered_legacy_installer_is_gone(self) -> None:
        self.assertFalse((BASE / "12_install_research_bridge.ps1").exists())


if __name__ == "__main__":
    unittest.main()
