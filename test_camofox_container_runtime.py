from __future__ import annotations

import json
from unittest import TestCase, main, mock

import tiktok_camofox_sync as sync
from pathlib import Path

BASE = Path(__file__).resolve().parent


class CamofoxContainerRuntimeTests(TestCase):
    def test_dockerfile_pins_reviewed_runtime_and_skips_dynamic_postinstall(self) -> None:
        text = (BASE / "runtime" / "camofox" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM node:26.10.0-trixie-slim", text)
        self.assertIn("CAMOUFOX_VERSION=152.0.4", text)
        self.assertIn("CAMOUFOX_RELEASE=beta.30", text)
        self.assertIn("CAMOUFOX_SHA256=5720d45b894ce1770543de024c6f10d514b38be560fa2dc3226b3d8586caf672", text)
        self.assertIn("/opt/camoufox/version.json", text)
        self.assertIn("test -d /opt/camoufox/fontconfig", text)
        self.assertIn("npm ci --ignore-scripts", text)
        self.assertIn("require('express-rate-limit')", text)
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
        expected = "https://github.com/LurigeLars/camofox-browser/archive/f05fea8b999a262ac6fa9d6d75bcdc4d2b3bf795.tar.gz"
        self.assertEqual(package["dependencies"]["@askjo/camofox-browser"], expected)
        self.assertEqual(lock["packages"][""]["dependencies"]["@askjo/camofox-browser"], expected)
        camofox = lock["packages"]["node_modules/@askjo/camofox-browser"]
        self.assertEqual(camofox["version"], "1.18.1")
        self.assertEqual(camofox["resolved"], expected)
        self.assertNotIn("integrity", camofox)
        self.assertEqual(camofox["dependencies"]["express-rate-limit"], "8.6.1")
        rate_limit = lock["packages"]["node_modules/express-rate-limit"]
        self.assertEqual(rate_limit["version"], "8.6.1")
        self.assertEqual(
            rate_limit["integrity"],
            "sha512-0D493aP61w0TJ2A0wy27riRsO7FMQ7FK+KUHOKCSfPvYo0R55aiC6emCVgFUeShH0fq0ICPVzNcgoS+BsbXQCA==",
        )
        self.assertIsNot(rate_limit.get("optional"), True)
        ip_address = lock["packages"]["node_modules/ip-address"]
        self.assertEqual(ip_address["version"], "10.7.2")
        self.assertIsNot(ip_address.get("optional"), True)
        self.assertEqual(sync.CAMOFOX_CONTAINER_SOURCE_COMMIT, "f05fea8b999a262ac6fa9d6d75bcdc4d2b3bf795")
        self.assertEqual(sync.CAMOFOX_CONTAINER_EXPECTED_CAMOFOX_VERSION, "1.18.1")
        self.assertEqual(sync.CAMOFOX_FALLBACK_EXPECTED_CAMOFOX_VERSION, "1.17.0")

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
        self.assertIn("source: influencerresearch-camofox-secrets", text)
        self.assertIn("target: /run/camofox-secrets", text)
        self.assertIn("/run/camofox-secrets/access_key", text)
        self.assertIn("/run/camofox-secrets/admin_key", text)
        self.assertIn("influencerresearch-secret-holder", text)
        self.assertIn("network_mode: none", text)
        self.assertIn("type: tmpfs", text)
        self.assertNotIn("post_start:", text)

    def test_direct_camofox_matches_ipv4_only_host_runtime(self) -> None:
        text = (BASE / "compose.yaml").read_text(encoding="utf-8")
        start = text.index("  camofox:")
        end = text.index("  camofox-public-proxy:")
        direct = text[start:end]

        self.assertIn("networks:\n      - runtime", direct)
        self.assertNotIn("browser_egress", direct)
        self.assertNotIn("172.64.36.1", direct)
        self.assertNotIn("172.64.36.2", direct)
        self.assertNotIn("enable_ipv6: true", text)

    def test_public_proxy_camofox_is_isolated_and_profile_gated(self) -> None:
        text = (BASE / "compose.yaml").read_text(encoding="utf-8")
        start = text.index("  camofox-public-proxy:")
        proxy = text[start:]
        direct = text[text.index("  camofox:"):start]

        self.assertIn("profiles:\n      - public-proxy", proxy)
        self.assertIn("container_name: influencerresearch-camofox-public-proxy", proxy)
        self.assertIn("PROXY_HOST: p.webshare.io", proxy)
        self.assertIn('PROXY_PORT: "80"', proxy)
        self.assertIn('CAMOUFOX_EXECUTABLE: /opt/camoufox/camoufox-bin', proxy)
        self.assertIn('CAMOUFOX_INSTALL_DIR: /run/camofox-proxy/install', proxy)
        self.assertIn('GEOIP_SETUP_TIMEOUT_MS: "30000"', proxy)
        self.assertNotIn('CAMOUFOX_INSTALL_DIR: /opt/camoufox', proxy)
        self.assertIn(
            'ln -sf /opt/camoufox/version.json /run/camofox-proxy/install/version.json',
            proxy,
        )
        self.assertIn(
            'ln -s /opt/camoufox/fontconfig /run/camofox-proxy/install/fontconfig',
            proxy,
        )
        self.assertIn('test -r /opt/camoufox/version.json', proxy)
        self.assertIn('test -d /opt/camoufox/fontconfig', proxy)
        self.assertIn('expose:\n      - "9377"', proxy)
        self.assertNotIn("ports:", proxy)
        self.assertNotIn("PROXY_HOST:", direct)
        self.assertNotIn("PROXY_USERNAME:", direct)
        self.assertNotIn("PROXY_PASSWORD:", direct)
        self.assertIn("source: influencerresearch-camofox-proxy-secrets", proxy)
        self.assertIn("target: /run/camofox-proxy-secrets", proxy)
        self.assertNotIn("INFLUENCER_PUBLIC_PROXY_USERNAME_SECRET", proxy)
        self.assertNotIn("INFLUENCER_PUBLIC_PROXY_PASSWORD_SECRET", proxy)
        self.assertIn(
            'export PROXY_USERNAME="$$(cat /run/camofox-proxy-secrets/proxy_username)"',
            proxy,
        )
        self.assertIn(
            'export PROXY_PASSWORD="$$(cat /run/camofox-proxy-secrets/proxy_password)"',
            proxy,
        )

    def test_runtime_supervisor_recovers_secret_tmpfs_after_docker_restart(self) -> None:
        runtime = (BASE / "scripts" / "runtime.ps1").read_text(encoding="utf-8")
        self.assertIn('name = "influencerresearch"', runtime)
        self.assertIn('arguments = @("-Action", "Recover")', runtime)
        self.assertIn("required_files = $holderRequired", runtime)
        self.assertIn("recovery_wait_seconds = 150", runtime)
        self.assertIn('Update-RuntimeSupervisorConfig -Enabled $false', runtime)

    def test_recover_waits_for_both_browser_services_before_compose_dependency_gate(self) -> None:
        runtime = (BASE / "scripts" / "runtime.ps1").read_text(encoding="utf-8")
        recover = runtime.split('    if (-not $Build) {', 1)[1].split('    $buildServices =', 1)[0]
        start_browser = 'Compose -ComposeArgs @($profileArgs + @("up", "-d", "--no-deps") + $camofoxServices)'
        wait_browser = 'Wait-ForContainerHealth -Containers $camofoxContainers -TimeoutSeconds 75'
        start_mcp = 'Compose -ComposeArgs @($profileArgs + @("up", "-d"))'
        wait_mcp = 'Wait-ForContainerHealth -Containers @("influencerresearch-mcp") -TimeoutSeconds 30'
        self.assertIn('$camofoxServices = @("camofox")', recover)
        self.assertIn('$camofoxServices += "camofox-public-proxy"', recover)
        self.assertIn('$camofoxContainers += "influencerresearch-camofox-public-proxy"', recover)
        for step in (start_browser, wait_browser, start_mcp, wait_mcp):
            self.assertIn(step, recover)
        self.assertLess(recover.index(start_browser), recover.index(wait_browser))
        self.assertLess(recover.index(wait_browser), recover.index(start_mcp))
        self.assertLess(recover.index(start_mcp), recover.index(wait_mcp))
        self.assertIn('Write-Host "INFLUENCERRESEARCH_RECOVERED_HEALTHY"', recover)
        self.assertNotIn("--force-recreate", recover)
        self.assertNotIn('"build"', recover)

    def test_recovery_health_wait_is_bounded_and_diagnostic(self) -> None:
        runtime = (BASE / "scripts" / "runtime.ps1").read_text(encoding="utf-8")
        helper = runtime.split("function Wait-ForContainerHealth", 1)[1].split(
            "function Invoke-ComposeUp", 1
        )[0]
        self.assertIn("[DateTime]::UtcNow.AddSeconds($TimeoutSeconds)", helper)
        self.assertIn("docker inspect --format", helper)
        self.assertIn('"healthy"', helper)
        self.assertIn("Start-Sleep -Seconds 2", helper)
        self.assertIn('throw "Recovery health timeout', helper)
        self.assertNotIn("docker restart", helper)
        self.assertNotIn("docker exec", helper)

    def test_redeploy_recreates_built_images_without_recreating_secret_holder(self) -> None:
        runtime = (BASE / "scripts/runtime.ps1").read_text(encoding="utf-8")
        self.assertIn('Compose -ComposeArgs @($profileArgs + @("build") + $buildServices)', runtime)
        self.assertIn('Compose -ComposeArgs @($profileArgs + @("up", "-d", "--no-deps", "--force-recreate") + $camofoxServices)', runtime)
        self.assertIn('Compose -ComposeArgs @($profileArgs + @("up", "-d", "influencerresearch"))', runtime)
        self.assertNotIn('"--force-recreate", "secret-holder"', runtime)

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
