from __future__ import annotations

import unittest
from pathlib import Path


BASE = Path(__file__).resolve().parent

ACTIVE_NAMESPACE_FILES = (
    "scripts/authenticate_instagram.ps1",
    "scripts/runtime.ps1",
    "setup_auth.py",
    "instagram_ingest.py",
    "ephemeral_ingest.py",
    "tiktok_camofox_sync.py",
)


class LocalRuntimeNamespaceTests(unittest.TestCase):
    def test_active_runtime_uses_influencerresearch_namespace(self) -> None:
        for relative in ACTIVE_NAMESPACE_FILES:
            text = (BASE / relative).read_text(encoding="utf-8")
            self.assertNotIn("InstagramResearch", text, relative)
            self.assertIn("InfluencerResearch", text, relative)

    def test_runtime_cookie_import_uses_dpapi_and_tmpfs(self) -> None:
        text = (BASE / "scripts/runtime.ps1").read_text(encoding="utf-8")
        self.assertIn("instagram_cookies.dpapi", text)
        self.assertIn("/run/influencerresearch-secrets/instagram_cookies.json", text)
        self.assertIn("Ensure-InstagramCookieStore", text)
        self.assertIn("refusing to remove the legacy file", text)
        self.assertNotIn(
            "/runtime/influencerresearch/secrets/instagram_cookies.json",
            text,
        )

    def test_auth_bootstrap_dpapi_protects_temporary_cookie_export(self) -> None:
        text = (BASE / "scripts/authenticate_instagram.ps1").read_text(encoding="utf-8")
        self.assertIn(
            'Join-Path $env:LOCALAPPDATA "InfluencerResearch"',
            text,
        )
        self.assertIn("instagram_cookies.dpapi", text)
        self.assertIn("ConvertFrom-SecureString", text)
        self.assertIn("INFLUENCER_RESEARCH_COOKIE_EXPORT_PATH", text)
        self.assertIn("Remove-Item -LiteralPath $TempExport", text)

    def test_instagram_workers_keep_cookie_material_on_tmpfs(self) -> None:
        for relative in ("instagram_ingest.py", "ephemeral_ingest.py"):
            text = (BASE / relative).read_text(encoding="utf-8")
            self.assertIn('Path("/run/influencerresearch-secrets")', text, relative)
            self.assertIn('"instagram_cookies.json"', text, relative)

    def test_setup_auth_requires_wrapper_selected_temporary_export(self) -> None:
        text = (BASE / "setup_auth.py").read_text(encoding="utf-8")
        self.assertIn("INFLUENCER_RESEARCH_COOKIE_EXPORT_PATH", text)
        self.assertNotIn(
            'return host_runtime_dir() / "secrets" / "instagram_cookies.json"',
            text,
        )

    def test_gemini_bootstrap_uses_dpapi_local_namespace(self) -> None:
        text = (BASE / "scripts/configure_gemini.ps1").read_text(encoding="utf-8")
        self.assertIn('InfluencerResearch\\secrets', text)
        self.assertIn("ConvertFrom-SecureString", text)
        self.assertNotIn("GEMINI_API_KEY=", text)

    def test_gemini_runtime_import_targets_tmpfs_secret(self) -> None:
        runtime = (BASE / "scripts/runtime.ps1").read_text(encoding="utf-8")
        compose = (BASE / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("gemini_api_key.dpapi", runtime)
        self.assertIn("/run/influencerresearch-secrets/gemini_api_key", runtime)
        self.assertIn("/run/influencerresearch-secrets:", compose)

    def test_camofox_runtime_secrets_use_dpapi_and_restart_safe_tmpfs_not_service_env(self) -> None:
        runtime = (BASE / "scripts/runtime.ps1").read_text(encoding="utf-8")
        compose = (BASE / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("camofox_access_key.dpapi", runtime)
        self.assertIn("camofox_admin_key.dpapi", runtime)
        self.assertNotIn("$env:CAMOFOX_ACCESS_KEY", runtime)
        self.assertNotIn("$env:CAMOFOX_ADMIN_KEY", runtime)
        self.assertNotIn("CAMOFOX_ACCESS_KEY: ${CAMOFOX_ACCESS_KEY", compose)
        self.assertNotIn("CAMOFOX_ADMIN_KEY: ${CAMOFOX_ADMIN_KEY", compose)
        self.assertNotIn("post_start:", compose)
        self.assertIn("influencerresearch-secret-holder", compose)
        self.assertIn("network_mode: none", compose)
        self.assertIn("type: tmpfs", compose)
        self.assertIn("influencerresearch-mcp-secrets", compose)
        self.assertIn("influencerresearch-camofox-secrets", compose)
        self.assertIn("influencerresearch-camofox-proxy-secrets", compose)
        self.assertIn("/run/influencerresearch-secrets/camofox_access_key", compose)
        self.assertIn("/run/influencerresearch-secrets/camofox_admin_key", compose)
        self.assertIn("/run/camofox-secrets/access_key", compose)
        self.assertIn("/run/camofox-secrets/admin_key", compose)
        self.assertIn("Write-SecretHolderFile", runtime)
        self.assertIn('arguments = @("-Action", "Recover")', runtime)
        self.assertIn('"Recover" {', runtime)
        self.assertIn("Invoke-ComposeUp -Build $false", runtime)

    def test_smoke_rehydrates_ephemeral_runtime_secrets(self) -> None:
        runtime = (BASE / "scripts/runtime.ps1").read_text(encoding="utf-8")
        self.assertIn("function Import-AvailableRuntimeSecrets", runtime)
        compose_up = runtime.split("function Invoke-ComposeUp", 1)[1].split(
            "function Import-InstagramAuth", 1
        )[0]
        self.assertIn("Import-AvailableRuntimeSecrets", compose_up)
        smoke = runtime.split('"Smoke" {', 1)[1]
        self.assertIn("Invoke-ComposeUp", smoke)

    def test_tiktok_host_fallback_uses_new_namespace(self) -> None:
        text = (BASE / "tiktok_camofox_sync.py").read_text(encoding="utf-8")
        self.assertIn(
            '_trusted_local_appdata() / "InfluencerResearch" / "camofox-poc"',
            text,
        )

    def test_migration_is_explicit_about_old_and_new_roots(self) -> None:
        text = (BASE / "scripts/migrate_local_namespace.ps1").read_text(encoding="utf-8")
        self.assertIn('$OldRoot = Join-Path $env:LOCALAPPDATA "InstagramResearch"', text)
        self.assertIn('$NewRoot = Join-Path $env:LOCALAPPDATA "InfluencerResearch"', text)
        self.assertIn("LOCAL_NAMESPACE_MIGRATION_OK", text)

    def test_readme_documents_one_time_legacy_migration(self) -> None:
        text = (BASE / "README.md").read_text(encoding="utf-8")
        self.assertIn("%LOCALAPPDATA%\\InfluencerResearch", text)
        self.assertIn("%LOCALAPPDATA%\\InstagramResearch", text)
        self.assertIn("migrate_local_namespace.ps1 -Action Apply", text)


if __name__ == "__main__":
    unittest.main()
