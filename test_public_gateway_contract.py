from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent


class PublicGatewayContractTests(unittest.TestCase):
    def test_public_compose_has_no_host_ports(self) -> None:
        text = (BASE / "compose.public.yaml").read_text(encoding="utf-8")
        self.assertIn("influencerresearch-gateway", text)
        self.assertIn("influencer-gateway", text)
        self.assertIn("name: influencerresearch_runtime", text)
        self.assertNotIn("\n    ports:", text)
        self.assertNotIn("cloudflared:", text)

    def test_gateway_has_fixed_upstream_and_access_auth(self) -> None:
        text = (BASE / "public" / "gateway" / "gateway.mjs").read_text(encoding="utf-8")
        self.assertIn("const UPSTREAM_HOST = 'influencerresearch';", text)
        self.assertIn("const UPSTREAM_PORT = 8770;", text)
        self.assertIn("cf-access-jwt-assertion", text)
        self.assertIn("ACCESS_AUD", text)
        self.assertIn("ACCESS_TEAM_DOMAIN", text)
        self.assertNotIn("GATEWAY_SECRET", text)

    def test_public_tool_allowlist_matches_mcp_surface(self) -> None:
        policy = (BASE / "public" / "gateway" / "policy.mjs").read_text(encoding="utf-8")
        match = re.search(r"ALLOWED_TOOLS = new Set\(\[(.*?)\]\);", policy, re.DOTALL)
        self.assertIsNotNone(match)
        public_tools = set(re.findall(r"'([^']+)'", match.group(1)))

        source = (BASE / "influencerresearch_mcp.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        mcp_tools = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
                for decorator in node.decorator_list
            )
        }

        self.assertEqual(public_tools, mcp_tools)

    def test_local_secret_files_are_ignored(self) -> None:
        gitignore = (BASE / ".gitignore").read_text(encoding="utf-8")
        dockerignore = (BASE / ".dockerignore").read_text(encoding="utf-8")
        path = "public/gateway.env"
        self.assertIn(path, gitignore)
        self.assertIn(path, dockerignore)
        self.assertNotIn("public/tunnel.env", gitignore)
        self.assertNotIn("public/tunnel.env", dockerignore)


if __name__ == "__main__":
    unittest.main()
