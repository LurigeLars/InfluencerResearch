from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


class _DummyModel:
    def __init__(self, *args, **kwargs):
        self.__dict__.update(kwargs)


class _DummyMCPServer:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        return lambda fn: fn

    def custom_route(self, *args, **kwargs):
        return lambda fn: fn


mcp_pkg = sys.modules.setdefault("mcp", ModuleType("mcp"))
server_pkg = sys.modules.setdefault("mcp.server", ModuleType("mcp.server"))
mcpserver_mod = sys.modules.setdefault("mcp.server.mcpserver", ModuleType("mcp.server.mcpserver"))
transport_mod = sys.modules.setdefault("mcp.server.transport_security", ModuleType("mcp.server.transport_security"))
types_mod = sys.modules.setdefault("mcp.types", ModuleType("mcp.types"))
mcpserver_mod.MCPServer = _DummyMCPServer
transport_mod.TransportSecuritySettings = _DummyModel
types_mod.ImageContent = _DummyModel
types_mod.TextContent = _DummyModel
types_mod.ToolAnnotations = _DummyModel

pydantic_mod = sys.modules.setdefault("pydantic", ModuleType("pydantic"))


class _DummyBaseModel:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self, **kwargs):
        return dict(self.__dict__)


def _dummy_config_dict(**kwargs):
    return dict(kwargs)


def _dummy_field(default=None, **kwargs):
    return default


pydantic_mod.BaseModel = _DummyBaseModel
pydantic_mod.ConfigDict = _dummy_config_dict
pydantic_mod.Field = _dummy_field

starlette_pkg = sys.modules.setdefault("starlette", ModuleType("starlette"))
starlette_requests = sys.modules.setdefault("starlette.requests", ModuleType("starlette.requests"))
starlette_responses = sys.modules.setdefault("starlette.responses", ModuleType("starlette.responses"))
starlette_requests.Request = _DummyModel
starlette_responses.JSONResponse = _DummyModel

import influencerresearch_mcp as irm


class MCPJobSemanticsTests(unittest.TestCase):
    def test_start_removes_stale_status_before_worker_spawn(self) -> None:
        class FakeProc:
            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status_path = root / "creator_evaluation_status.json"
            request_path = root / "mcp_job_request.json"
            state_path = root / "mcp_job_status.json"
            status_path.write_text('{"state":"FAILED"}', encoding="utf-8")

            manager = irm.JobManager()
            manager.STATUS_FILES = {"creator_evaluate": status_path}
            with patch.object(irm, "JOB_REQUEST_PATH", request_path), patch.object(
                irm, "JOB_STATE_PATH", state_path
            ), patch.object(irm.subprocess, "Popen", return_value=FakeProc()):
                result = manager.start(
                    "creator_evaluate",
                    {"creator_key": "demo", "sample_size": 1, "source_platform": "YOUTUBE"},
                )

            self.assertTrue(result["ok"])
            self.assertFalse(status_path.exists())
            self.assertIsNone(result["job"].get("status"))

    def test_research_stop_persists_nested_stopped_state_atomically(self) -> None:
        class FakeProc:
            pid = 4242

            def __init__(self):
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = -15
                return self.returncode

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status_path = root / "creator_recent_check_status.json"
            request_path = root / "mcp_job_request.json"
            state_path = root / "mcp_job_status.json"
            status_path.write_text(
                '{"schema_version":1,"state":"RUNNING","progress":{"stage":"INGESTION","phase":"GROUP_RUNNING"}}',
                encoding="utf-8",
            )
            manager = irm.JobManager()
            manager.STATUS_FILES = {"creator_recent_check": status_path}
            proc = FakeProc()
            with (
                patch.object(irm, "JOB_REQUEST_PATH", request_path),
                patch.object(irm, "JOB_STATE_PATH", state_path),
                patch.object(irm.subprocess, "Popen", return_value=proc),
                patch.object(irm.os, "killpg"),
            ):
                started = manager.start(
                    "creator_recent_check",
                    {
                        "scope": "ALL_REGISTERED",
                        "creator_keys": ["fixture"],
                        "window": "LAST_N_DAYS",
                        "lookback_days": 1,
                        "max_items": 1,
                    },
                )
                self.assertTrue(started["ok"])
                # JobManager.start intentionally resets stale status; emulate live progress.
                status_path.write_text(
                    '{"schema_version":1,"state":"RUNNING","progress":{"stage":"INGESTION","phase":"GROUP_RUNNING"}}',
                    encoding="utf-8",
                )
                stopped = manager.stop()

            persisted = __import__("json").loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(stopped["job"]["state"], "STOPPED")
            self.assertEqual(stopped["job"]["status"]["state"], "STOPPED")
            self.assertEqual(persisted["state"], "STOPPED")
            self.assertEqual(persisted["progress"]["phase"], "STOPPED")
            self.assertEqual(persisted["transitions"][-1]["final_state"], "STOPPED")


    def test_recent_partial_remains_partial_even_with_nonzero_exit(self) -> None:
        self.assertEqual(
            irm.resolve_job_state("creator_recent_check", 1, {"state": "PARTIAL"}),
            "PARTIAL",
        )

    def test_recent_failed_remains_failed(self) -> None:
        self.assertEqual(
            irm.resolve_job_state("creator_recent_check", 2, {"state": "FAILED"}),
            "FAILED",
        )

    def test_other_jobs_keep_exit_code_semantics(self) -> None:
        self.assertEqual(irm.resolve_job_state("creator_monitor", 0, {"state": "PARTIAL"}), "COMPLETE")
        self.assertEqual(irm.resolve_job_state("creator_monitor", 1, {"state": "PARTIAL"}), "FAILED")


if __name__ == "__main__":
    unittest.main()
