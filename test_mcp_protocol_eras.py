"""Exercise the real MCP SDK in a separate process from stub-based tests."""
from __future__ import annotations

import atexit
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
from mcp.client import Client

import influencerresearch_mcp as irm

# Importing the server installs a runtime cleanup hook. The test owns only
# synthetic state and must never run cleanup against the real job paths.
atexit.unregister(irm.jobs.stop)


class MCPProtocolErasTests(unittest.TestCase):
    def test_idle_status_and_catalog_across_eras(self):
        async def run(mode):
            async with Client(irm.mcp, mode=mode) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                result = await client.call_tool("research_status", {})
                self.assertFalse(result.is_error)
                return client.protocol_version, names, json.loads(result.content[0].text)

        with tempfile.TemporaryDirectory() as root, patch.object(
            irm, "JOB_STATE_PATH", Path(root) / "status.json"
        ), patch.object(irm, "jobs", irm.JobManager()):
            baseline = None
            for mode in ("auto", "2026-07-28", "legacy"):
                with self.subTest(mode=mode):
                    version, names, payload = anyio.run(run, mode)
                    if mode == "legacy":
                        self.assertNotEqual("2026-07-28", version)
                    else:
                        self.assertEqual("2026-07-28", version)
                    self.assertIn("research_status", names)
                    self.assertEqual({"active": None, "last": None}, payload)
                    if baseline is None:
                        baseline = (names, payload)
                    self.assertEqual(baseline, (names, payload))


if __name__ == "__main__":
    unittest.main()
