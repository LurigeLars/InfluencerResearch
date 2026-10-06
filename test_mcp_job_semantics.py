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


    def test_server_start_recovers_persisted_running_job_as_failed(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "mcp_job_status.json"
            status_path = root / "creator_evaluation_status.json"
            state_path.write_text(
                json.dumps({
                    "job_id": "orphan",
                    "kind": "creator_evaluate",
                    "state": "RUNNING",
                    "started_at": "2026-10-06T20:00:00+00:00",
                }),
                encoding="utf-8",
            )
            status_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "state": "RUNNING",
                    "started_at": "2026-10-06T20:00:00+00:00",
                    "progress": {
                        "phase": "TRANSCRIPTION",
                        "heartbeat_at": "2026-10-06T20:01:00+00:00",
                    },
                }),
                encoding="utf-8",
            )

            with (
                patch.object(irm, "JOB_STATE_PATH", state_path),
                patch.object(
                    irm.JobManager,
                    "STATUS_FILES",
                    {"creator_evaluate": status_path},
                ),
            ):
                manager = irm.JobManager()

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            nested = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["state"], "FAILED")
            self.assertEqual(nested["state"], "FAILED")
            self.assertEqual(
                nested["progress"]["terminal_reason"],
                "ORPHANED_JOB_ON_SERVER_START",
            )
            self.assertEqual(nested["progress"]["last_phase"], "TRANSCRIPTION")
            self.assertEqual(manager.status()["last"]["state"], "FAILED")

    def test_startup_reconciles_last_terminal_evaluation(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status_path = root / "creator_evaluation_status.json"
            state_path = root / "mcp_job_status.json"
            status_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "state": "STOPPED",
                    "evaluation_run_id": "eval-fixture-youtube-1",
                }),
                encoding="utf-8",
            )

            with (
                patch.object(irm, "JOB_STATE_PATH", state_path),
                patch.object(
                    irm.JobManager,
                    "STATUS_FILES",
                    {"creator_evaluate": status_path},
                ),
                patch.object(
                    irm,
                    "_reconcile_completed_creator_evaluation",
                    return_value={"ok": True, "target_count": 2},
                ) as reconcile_mock,
            ):
                irm.JobManager()

            reconcile_mock.assert_called_once_with(status_path)

    def test_terminal_reconciliation_targets_completed_evaluation_items(self) -> None:
        import json
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_dir = root / "state"
            app_dir = root / "app"
            state_dir.mkdir(parents=True)
            app_dir.mkdir(parents=True)

            run_id = "eval-fixture-youtube-1"
            status_path = state_dir / "creator_evaluation_status.json"
            status_path.write_text(
                json.dumps({"evaluation_run_id": run_id, "state": "FAILED"}),
                encoding="utf-8",
            )
            manifest_items = {}
            for queue_id in ("yt_one001", "yt_two002", "yt_three03"):
                manifest_items[queue_id] = {
                    "evaluation_run_id": run_id,
                    "download_status": "DONE",
                    "transcription_status": "DONE",
                    "visual_evidence_status": "DONE",
                }
            manifest_items["yt_incomplete"] = {
                "evaluation_run_id": run_id,
                "download_status": "DONE",
                "transcription_status": "PENDING",
                "visual_evidence_status": "DONE",
            }
            (state_dir / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "items": manifest_items}),
                encoding="utf-8",
            )

            with (
                patch.object(irm, "ROOT", root),
                patch.object(irm, "STATE_DIR", state_dir),
                patch.object(irm, "APP_DIR", app_dir),
                patch.object(
                    irm.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ) as run_mock,
            ):
                result = irm._reconcile_completed_creator_evaluation(status_path)

            self.assertTrue(result["ok"])
            self.assertEqual(result["target_count"], 3)
            cmd = run_mock.call_args.args[0]
            self.assertIn(str(app_dir / "research_queue.py"), cmd)
            self.assertEqual(cmd.count("--must-include-shortcode"), 3)
            self.assertIn("yt_one001", cmd)
            self.assertIn("yt_two002", cmd)
            self.assertIn("yt_three03", cmd)
            self.assertNotIn("yt_incomplete", cmd)


    def test_terminal_reconciliation_uses_explicit_run_membership_for_reused_items(self) -> None:
        import json
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_dir = root / "state"
            app_dir = root / "app"
            state_dir.mkdir(parents=True)
            app_dir.mkdir(parents=True)

            run_id = "eval-current"
            status_path = state_dir / "creator_evaluation_status.json"
            status_path.write_text(
                json.dumps({
                    "evaluation_run_id": run_id,
                    "state": "RUNNING",
                    "creator": "kathylien",
                    "source_platform": "YOUTUBE",
                    "progress": {
                        "phase": "EVIDENCE",
                        "selected_queue_ids": ["yt_new", "yt_reused"],
                    },
                }),
                encoding="utf-8",
            )
            complete = {
                "creator": "kathylien",
                "source_platform": "YOUTUBE",
                "evaluation_mode": "CREATOR_EVALUATION",
                "download_status": "DONE",
                "transcription_status": "DONE",
                "visual_evidence_status": "DONE",
                "analysis_content_status": "READY",
                "research_status": "PENDING_ANALYSIS",
            }
            (state_dir / "manifest.json").write_text(
                json.dumps({
                    "items": {
                        "yt_new": {**complete, "evaluation_run_id": run_id},
                        "yt_reused": {**complete, "evaluation_run_id": "eval-older"},
                    }
                }),
                encoding="utf-8",
            )
            (state_dir / "research_queue.json").write_text(
                json.dumps({"items": []}),
                encoding="utf-8",
            )
            (state_dir / "research_decisions.json").write_text(
                json.dumps({"items": {}}),
                encoding="utf-8",
            )

            with (
                patch.object(irm, "ROOT", root),
                patch.object(irm, "STATE_DIR", state_dir),
                patch.object(irm, "APP_DIR", app_dir),
                patch.object(
                    irm.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ) as run_mock,
            ):
                result = irm._reconcile_completed_creator_evaluation(status_path)

            self.assertTrue(result["ok"])
            self.assertEqual(result["membership_source"], "STATUS_MEMBERSHIP")
            self.assertEqual(result["target_count"], 2)
            cmd = run_mock.call_args.args[0]
            self.assertIn("yt_new", cmd)
            self.assertIn("yt_reused", cmd)

    def test_terminal_reconciliation_persists_analysis_disposition_accounting(self) -> None:
        import json
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_dir = root / "state"
            app_dir = root / "app"
            state_dir.mkdir(parents=True)
            app_dir.mkdir(parents=True)

            run_id = "eval-accounting-1"
            status_path = state_dir / "creator_evaluation_status.json"
            status_path.write_text(
                json.dumps({
                    "evaluation_run_id": run_id,
                    "state": "STOPPED",
                    "progress": {"completed_count": 5},
                }),
                encoding="utf-8",
            )
            common = {
                "evaluation_mode": "CREATOR_EVALUATION",
                "evaluation_run_id": run_id,
                "creator": "kathylien",
                "source_platform": "YOUTUBE",
                "download_status": "DONE",
                "transcription_status": "DONE",
                "visual_evidence_status": "DONE",
                "analysis_content_status": "READY",
            }
            manifest = {
                "schema_version": 1,
                "items": {
                    "yt_pending": {**common, "research_status": "PENDING_ANALYSIS"},
                    "yt_duplicate": {
                        **common,
                        "research_status": "DUPLICATE",
                        "duplicate_of": "yt_pending",
                        "duplicate_basis": "TRANSCRIPT_MATCH",
                    },
                    "yt_insufficient": {
                        **common,
                        "research_status": "INSUFFICIENT_CONTENT",
                        "analysis_content_status": "INSUFFICIENT_CONTENT",
                        "analysis_content_reason": "NO_USABLE_CONTENT",
                    },
                    "yt_final": {**common, "research_status": "ANALYZED"},
                    "yt_missing": {**common, "research_status": "PENDING_ANALYSIS"},
                },
            }
            (state_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (state_dir / "research_queue.json").write_text(
                json.dumps({
                    "items": [{
                        "queue_id": "yt_pending",
                        "analysis_status": "PENDING_ANALYSIS",
                    }]
                }),
                encoding="utf-8",
            )
            (state_dir / "research_decisions.json").write_text(
                json.dumps({
                    "items": {
                        "yt_final": {
                            "decision": "RESEARCH",
                            "screened_at": "2026-10-07T00:00:00+00:00",
                        }
                    }
                }),
                encoding="utf-8",
            )

            with (
                patch.object(irm, "ROOT", root),
                patch.object(irm, "STATE_DIR", state_dir),
                patch.object(irm, "APP_DIR", app_dir),
                patch.object(
                    irm.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ),
            ):
                result = irm._reconcile_completed_creator_evaluation(status_path)

            persisted = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(persisted["queued_for_analysis_count"], 1)
            self.assertEqual(persisted["analysis_finalized_count"], 1)
            self.assertEqual(persisted["analysis_duplicate_count"], 1)
            self.assertEqual(persisted["analysis_insufficient_count"], 1)
            self.assertEqual(persisted["analysis_queue_missing_count"], 1)
            self.assertEqual(persisted["analysis_accounted_count"], 4)
            self.assertEqual(persisted["analysis_unaccounted_count"], 1)
            self.assertEqual(persisted["analysis_unaccounted_items"], ["yt_missing"])
            self.assertEqual(
                persisted["progress"]["analysis_disposition_counts"]["DUPLICATE"],
                1,
            )

    def test_creator_evaluation_terminal_state_overrides_nonzero_exit(self) -> None:
        self.assertEqual(
            irm.resolve_job_state("creator_evaluate", 1, {"state": "PARTIAL"}),
            "PARTIAL",
        )
        self.assertEqual(
            irm.resolve_job_state("creator_evaluate", 124, {"state": "FAILED"}),
            "FAILED",
        )

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
