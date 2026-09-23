from __future__ import annotations

import ast
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent


class MCPContractTests(unittest.TestCase):
    def test_expected_tool_surface_only(self) -> None:
        source = (BASE / "influencerresearch_mcp.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        tool_names: list[str] = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "tool"
                ):
                    tool_names.append(node.name)
        self.assertEqual(
            tool_names,
            [
                "creator_list",
                "creator_get",
                "creator_register",
                "creator_evaluate",
                "creator_monitor",
                "creator_recent_check",
                "research_status",
                "research_stop",
            ],
        )
        self.assertNotIn("run_command", source)
        self.assertNotIn("execute_command", source)
        self.assertIn("structured_output=False", source)

    def test_compose_has_separate_internal_camofox(self) -> None:
        text = (BASE / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("  influencerresearch:", text)
        self.assertIn("  camofox:", text)
        self.assertIn('INFLUENCER_RESEARCH_CONTAINER: "1"', text)
        self.assertNotIn("127.0.0.1:9377:9377", text)
        self.assertIn(
            "127.0.0.1:" + "$" + "{INFLUENCER_RESEARCH_MCP_PORT:-8770}:8770",
            text,
        )

    def test_camofox_env_config_is_supported(self) -> None:
        source = (BASE / "camofox_container.py").read_text(encoding="utf-8")
        self.assertIn('"base_url": "http://camofox:9377"', source)
        self.assertIn("CAMOFOX_ACCESS_KEY", source)
        self.assertIn("CAMOFOX_ADMIN_KEY", source)

    def test_job_launcher_uses_fixed_worker_command(self) -> None:
        source = (BASE / "influencerresearch_mcp.py").read_text(encoding="utf-8")
        self.assertIn('APP_DIR / "mcp_job_worker.py"', source)
        self.assertNotIn('"influencer_evaluation.py", args', source)
        self.assertNotIn('"creator_monitor.py", args', source)
        self.assertNotIn('"creator_recent_check.py", args', source)


    def test_legacy_ingress_artifacts_are_retired(self) -> None:
        for path in (
            "research_request_bridge.py",
            "creator_registration.py",
            "compose.camofox.yaml",
            "scripts/research_bridge.ps1",
            "scripts/camofox_container.ps1",
            "scripts/camofox_smoke.ps1",
            "scripts/install.ps1",
        ):
            self.assertFalse((BASE / path).exists(), path)


if __name__ == "__main__":
    unittest.main()
