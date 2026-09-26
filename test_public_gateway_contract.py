from __future__ import annotations

import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent


class PublicGatewayContractTests(unittest.TestCase):
    def test_public_compose_has_no_host_ports(self) -> None:
        text = (BASE / "compose.public.yaml").read_text(encoding="utf-8")
        self.assertIn("influencerresearch-gateway", text)
        self.assertIn("influencerresearch-cloudflared", text)
        self.assertIn("name: influencerresearch_runtime", text)
        self.assertNotIn("\n    ports:", text)
        self.assertIn('command: ["tunnel", "--no-autoupdate", "run"]', text)

    def test_gateway_has_fixed_upstream_and_access_auth(self) -> None:
        text = (BASE / "public" / "gateway" / "gateway.mjs").read_text(encoding="utf-8")
        self.assertIn("const UPSTREAM_HOST = 'influencerresearch';", text)
        self.assertIn("const UPSTREAM_PORT = 8770;", text)
        self.assertIn("cf-access-jwt-assertion", text)
        self.assertIn("ACCESS_AUD", text)
        self.assertIn("ACCESS_TEAM_DOMAIN", text)
        self.assertNotIn("GATEWAY_SECRET", text)

    def test_public_tool_allowlist_matches_mcp_surface(self) -> None:
        text = (BASE / "public" / "gateway" / "policy.mjs").read_text(encoding="utf-8")
        for name in (
            "creator_list",
            "creator_get",
            "creator_register",
            "creator_evaluate",
            "creator_monitor",
            "creator_recent_check",
            "research_status",
            "research_stop",
        ):
            self.assertIn(f"'{name}'", text)

    def test_local_secret_files_are_ignored(self) -> None:
        gitignore = (BASE / ".gitignore").read_text(encoding="utf-8")
        dockerignore = (BASE / ".dockerignore").read_text(encoding="utf-8")
        for path in ("public/gateway.env", "public/tunnel.env"):
            self.assertIn(path, gitignore)
            self.assertIn(path, dockerignore)


if __name__ == "__main__":
    unittest.main()
