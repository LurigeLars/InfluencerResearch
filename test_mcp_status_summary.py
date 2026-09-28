from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from influencerresearch_mcp import summarize_status


class McpStatusSummaryTests(unittest.TestCase):
    def test_failed_job_exposes_sanitized_top_level_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text(
                json.dumps({
                    "state": "FAILED",
                    "error": "RuntimeError: fixture failure",
                    "internal_debug": "must stay private",
                }),
                encoding="utf-8",
            )
            summary = summarize_status(path)

        self.assertEqual(summary["state"], "FAILED")
        self.assertEqual(summary["error"], "RuntimeError: fixture failure")
        self.assertNotIn("internal_debug", summary)


if __name__ == "__main__":
    unittest.main()
