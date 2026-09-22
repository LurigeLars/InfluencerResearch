from __future__ import annotations

import json
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent


class CamofoxContainerRuntimeTests(unittest.TestCase):
    def test_dockerfile_pins_reviewed_runtime_and_skips_dynamic_postinstall(self) -> None:
        text = (BASE / "runtime" / "camofox" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM node:22.23.2-trixie-slim", text)
        self.assertIn("CAMOUFOX_VERSION=152.0.4", text)
        self.assertIn("CAMOUFOX_RELEASE=beta.28", text)
        self.assertIn("npm ci --ignore-scripts", text)
        self.assertIn("CAMOFOX_SKIP_DOWNLOAD=1", text)
        self.assertIn("USER node", text)
        self.assertNotIn("releases/latest", text)

    def test_compose_is_loopback_only_and_hardened(self) -> None:
        text = (BASE / "compose.camofox.yaml").read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:9377:9377"', text)
        self.assertIn("read_only: true", text)
        self.assertIn("no-new-privileges:true", text)
        self.assertIn("cap_drop:", text)
        self.assertIn("- ALL", text)
        self.assertIn('CAMOFOX_CRASH_REPORT_ENABLED: "false"', text)
        self.assertIn("CAMOFOX_TRANSFER_DIR", text)
        self.assertNotIn("docker.sock", text)
        self.assertNotIn("privileged: true", text)

    def test_camofox_config_disables_unneeded_plugins(self) -> None:
        config = json.loads(
            (BASE / "runtime" / "camofox" / "camofox.config.json").read_text(encoding="utf-8")
        )
        self.assertFalse(config["plugins"]["youtube"]["enabled"])
        self.assertFalse(config["plugins"]["vnc"]["enabled"])
        self.assertTrue(config["plugins"]["persistence"]["enabled"])


if __name__ == "__main__":
    unittest.main()
