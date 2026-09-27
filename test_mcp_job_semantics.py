from __future__ import annotations

import unittest

from influencerresearch_mcp import resolve_job_state


class MCPJobSemanticsTests(unittest.TestCase):
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
