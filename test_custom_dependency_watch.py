"""Regression: custom release watcher must not duplicate pip Dependabot."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from unittest.mock import call, patch

from scripts import check_custom_dependencies as watch


class CustomDependencyWatchTests(unittest.TestCase):
    def test_only_nonstandard_artifact_pins_create_issues(self) -> None:
        sha = "a" * 40
        latest = "b" * 40
        output = io.StringIO()
        with (
            patch.object(watch, "current_camoufox_tag", return_value="v152.0.4-beta.30"),
            patch.object(watch, "latest_camoufox_tag", return_value=("v158.0.1-beta.41", "https://example.invalid/browser")),
            patch.object(watch, "current_camofox_browser_sha", return_value=sha),
            patch.object(watch, "latest_camofox_browser_sha", return_value=(latest, "https://example.invalid/fork")),
            patch.object(watch, "_token", return_value="test"),
            patch.object(watch, "_sync_issue") as sync_issue,
            patch.dict("os.environ", {"GITHUB_REPOSITORY": "LurigeLars/InfluencerResearch"}),
            patch.object(sys, "argv", ["check_custom_dependencies.py", "--sync-issues"]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(watch.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"camoufox", "camofox_browser"})
        self.assertEqual(sync_issue.call_count, 2)
        titles = {c.args[1] for c in sync_issue.call_args_list}
        self.assertEqual(titles, {watch.CAMOUFOX_ISSUE, watch.CAMOFOX_BROWSER_ISSUE})
        self.assertTrue(all(c.kwargs["outdated"] for c in sync_issue.call_args_list))


if __name__ == "__main__":
    unittest.main()
