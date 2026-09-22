from __future__ import annotations

import json
import unittest
from unittest import mock

import tiktok_camofox_sync as sync
from pathlib import Path

BASE = Path(__file__).resolve().parent


class CamofoxContainerRuntimeTests(unittest.TestCase):
    def test_dockerfile_pins_reviewed_runtime_and_skips_dynamic_postinstall(self) -> None:
        text = (BASE / "runtime" / "camofox" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM node:22.23.2-trixie-slim", text)
        self.assertIn("CAMOUFOX_VERSION=152.0.4", text)
        self.assertIn("CAMOUFOX_RELEASE=beta.28", text)
        self.assertIn("/opt/camoufox/version.json", text)
        self.assertIn("test -d /opt/camoufox/fontconfig", text)
        self.assertIn("npm ci --ignore-scripts", text)
        self.assertIn("CAMOFOX_SKIP_DOWNLOAD=1", text)
        self.assertIn("USER node", text)
        self.assertNotIn("releases/latest", text)

    def test_runtime_manifest_pins_required_impit_linux_binding(self) -> None:
        package = json.loads(
            (BASE / 'runtime' / 'camofox' / 'package.json').read_text(encoding='utf-8')
        )
        lock = json.loads(
            (BASE / 'runtime' / 'camofox' / 'package-lock.json').read_text(encoding='utf-8')
        )
        self.assertEqual(package['dependencies']['impit-linux-x64-gnu'], '0.14.5')
        self.assertEqual(lock['packages']['']['dependencies']['impit-linux-x64-gnu'], '0.14.5')
        binding = lock['packages']['node_modules/impit-linux-x64-gnu']
        self.assertEqual(binding['version'], '0.14.5')
        self.assertEqual(binding['os'], ['linux'])
        self.assertEqual(binding['cpu'], ['x64'])
        self.assertIsNot(binding.get('optional'), True)

        dockerfile = (BASE / 'runtime' / 'camofox' / 'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('--omit=optional', dockerfile)
        self.assertIn("impit-linux-x64-gnu':'0.14.5", dockerfile)
        self.assertIn("require('impit')", dockerfile)

    def test_compose_is_loopback_only_and_hardened(self) -> None:
        text = (BASE / "compose.camofox.yaml").read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:9377:9377"', text)
        self.assertIn("read_only: true", text)
        self.assertIn("no-new-privileges:true", text)
        self.assertIn("cap_drop:", text)
        self.assertIn("- ALL", text)
        self.assertIn('CAMOFOX_CRASH_REPORT_ENABLED: "false"', text)
        self.assertIn('CAMOFOX_DISABLE_DEFAULT_ADDONS: "true"', text)
        self.assertIn('CAMOUFOX_INSTALL_DIR: /opt/camoufox', text)
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

    def test_container_runtime_health_does_not_require_child_process(self) -> None:
        original = sync._CAMOFOX_FALLBACK_SERVER
        sync._CAMOFOX_FALLBACK_SERVER = {
            "runtime_mode": "container",
            "proc": None,
        }
        try:
            with mock.patch.object(sync, "_fallback_health", return_value={"status": "ok"}):
                self.assertEqual(sync.health(), {"status": "ok"})
        finally:
            sync._CAMOFOX_FALLBACK_SERVER = original

    def test_stopped_legacy_runtime_is_not_healthy(self) -> None:
        class StoppedProc:
            def poll(self) -> int:
                return 1

        original = sync._CAMOFOX_FALLBACK_SERVER
        sync._CAMOFOX_FALLBACK_SERVER = {
            "runtime_mode": "legacy_local",
            "proc": StoppedProc(),
        }
        try:
            with mock.patch.object(sync, "_fallback_health") as fallback_health:
                self.assertIsNone(sync.health())
                fallback_health.assert_not_called()
        finally:
            sync._CAMOFOX_FALLBACK_SERVER = original


if __name__ == "__main__":
    unittest.main()
