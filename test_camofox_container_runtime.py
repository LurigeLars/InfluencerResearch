from __future__ import annotations

import json
from unittest import TestCase, main, mock

import tiktok_camofox_sync as sync
from pathlib import Path

BASE = Path(__file__).resolve().parent


class CamofoxContainerRuntimeTests(TestCase):
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

    def test_runtime_manifest_pins_patched_camofox_commit(self) -> None:
        package = json.loads(
            (BASE / "runtime" / "camofox" / "package.json").read_text(encoding="utf-8")
        )
        lock = json.loads(
            (BASE / "runtime" / "camofox" / "package-lock.json").read_text(encoding="utf-8")
        )
        expected = "https://github.com/LurigeLars/camofox-browser/archive/011faad7a88797e780556321d328bdd00b8f68b7.tar.gz"
        self.assertEqual(package["dependencies"]["@askjo/camofox-browser"], expected)
        self.assertEqual(lock["packages"][""]["dependencies"]["@askjo/camofox-browser"], expected)
        camofox = lock["packages"]["node_modules/@askjo/camofox-browser"]
        self.assertEqual(camofox["version"], "1.17.0")
        self.assertEqual(camofox["resolved"], expected)
        self.assertEqual(camofox["integrity"], "sha512-6wRwkXJeIwTZsTAOuN1uAvKrtwL3fs40fw1BJQiLX3gEfxBM2NuAr7ts49Hw5YjmM80RJ1OBtDkZC8bCRTAI0Q==")
        self.assertEqual(sync.CAMOFOX_CONTAINER_SOURCE_COMMIT, "011faad7a88797e780556321d328bdd00b8f68b7")

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

    def test_compose_is_internal_only_and_hardened(self) -> None:
        text = (BASE / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("  camofox:", text)
        self.assertIn('INFLUENCER_RESEARCH_CONTAINER: "1"', text)
        self.assertIn('expose:', text)
        self.assertIn('- "9377"', text)
        self.assertNotIn("127.0.0.1:9377:9377", text)
        self.assertIn("read_only: true", text)
        self.assertIn("no-new-privileges:true", text)
        self.assertIn("cap_drop:", text)
        self.assertIn("- ALL", text)
        self.assertIn('CAMOFOX_CRASH_REPORT_ENABLED: "false"', text)
        self.assertIn('CAMOFOX_DISABLE_DEFAULT_ADDONS: "true"', text)
        self.assertIn('CAMOUFOX_INSTALL_DIR: /opt/camoufox', text)
        self.assertIn('HOME: /run/camofox', text)
        self.assertNotIn("CAMOFOX_TRANSFER_DIR", text)
        self.assertNotIn("/transfer", text)
        self.assertNotIn("docker.sock", text)
        self.assertNotIn("privileged: true", text)
        self.assertIn("mem_limit: 2g", text)
        self.assertIn("cpus: 2.0", text)
        self.assertIn("pids_limit: 256", text)
        self.assertNotIn("CAMOFOX_ACCESS_KEY: ${CAMOFOX_ACCESS_KEY", text)
        self.assertNotIn("CAMOFOX_ADMIN_KEY: ${CAMOFOX_ADMIN_KEY", text)
        self.assertIn("/run/camofox-secrets:rw,nosuid,nodev,noexec", text)
        self.assertIn("/run/camofox-secrets/access_key", text)
        self.assertIn("/run/camofox-secrets/admin_key", text)
        self.assertIn("post_start:", text)

    def test_public_proxy_camofox_is_isolated_and_profile_gated(self) -> None:
        text = (BASE / "compose.yaml").read_text(encoding="utf-8")
        start = text.index("  camofox-public-proxy:")
        proxy = text[start:]
        direct = text[text.index("  camofox:"):start]

        self.assertIn("profiles:\n      - public-proxy", proxy)
        self.assertIn("container_name: influencerresearch-camofox-public-proxy", proxy)
        self.assertIn("PROXY_HOST: p.webshare.io", proxy)
        self.assertIn('PROXY_PORT: "80"', proxy)
        self.assertIn('expose:\n      - "9377"', proxy)
        self.assertNotIn("ports:", proxy)
        self.assertNotIn("PROXY_HOST:", direct)
        self.assertNotIn("PROXY_USERNAME:", direct)
        self.assertNotIn("PROXY_PASSWORD:", direct)
        self.assertIn('test -n "$$INFLUENCER_PUBLIC_PROXY_USERNAME_SECRET"', proxy)
        self.assertIn('test -n "$$INFLUENCER_PUBLIC_PROXY_PASSWORD_SECRET"', proxy)
        self.assertIn(
            'export PROXY_USERNAME="$$(cat /run/camofox-proxy-secrets/proxy_username)"',
            proxy,
        )
        self.assertIn(
            'export PROXY_PASSWORD="$$(cat /run/camofox-proxy-secrets/proxy_password)"',
            proxy,
        )

    def test_runtime_reuses_dpapi_webshare_secrets_without_copying_api_key(self) -> None:
        text = (BASE / "scripts" / "runtime.ps1").read_text(encoding="utf-8")
        self.assertIn(
            'FirecrawlLocal\\secrets\\public_proxy_username.dpapi',
            text,
        )
        self.assertIn(
            'FirecrawlLocal\\secrets\\public_proxy_password.dpapi',
            text,
        )
        self.assertIn('EndsWith("-rotate"', text)
        self.assertIn('--profile", "public-proxy"', text)
        self.assertNotIn("proxy.webshare.io/api/", text)
        self.assertNotIn("Webshare API key", text)

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
    main()
