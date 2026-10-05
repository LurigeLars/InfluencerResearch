from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import influencerresearch_mcp as irm
from influencerresearch_mcp import resolve_job_state


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

    def test_recent_partial_remains_partial_even_with_nonzero_exit(self) -> None:
        self.assertEqual(
            resolve_job_state("creator_recent_check", 1, {"state": "PARTIAL"}),
            "PARTIAL",
        )

    def test_recent_failed_remains_failed(self) -> None:
        self.assertEqual(
            resolve_job_state("creator_recent_check", 2, {"state": "FAILED"}),
            "FAILED",
        )

    def test_other_jobs_keep_exit_code_semantics(self) -> None:
        self.assertEqual(resolve_job_state("creator_monitor", 0, {"state": "PARTIAL"}), "COMPLETE")
        self.assertEqual(resolve_job_state("creator_monitor", 1, {"state": "PARTIAL"}), "FAILED")


if __name__ == "__main__":
    unittest.main()
