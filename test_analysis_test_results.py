from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from analysis_queue import (
    get_analysis_queue_item,
    list_analysis_decisions,
    record_analysis_decision,
    record_test_result,
)
from test_analysis_queue import build_fixture, decision_payload


def result_payload(outcome: str = "REJECTED", **overrides) -> dict:
    payload = {
        "outcome": outcome,
        "method": "Rule as stated, no fitting; 5-min RTH bars, 0.5 bp cost per side.",
        "data": "DTV 5-min, 17 symbols, 2025-11-03..2026-11-11",
        "summary": "No edge: pooled mean 0 to -6 bp per trade.",
        "next_step": "Drop; deep backtest not warranted.",
        "evidence_lineage_id": "lineage-fixture-123",
    }
    payload.update(overrides)
    return payload


class TestResultTests(unittest.TestCase):
    def _root_with(self, td: str, decision: str) -> Path:
        root = Path(td)
        fixture = build_fixture(root)
        self.assertEqual(
            record_analysis_decision(root, fixture["queue_id"], decision_payload(decision))["result"], "WRITTEN"
        )
        return root

    def test_result_is_appended_without_touching_the_decision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with(td, "TEST_CANDIDATE")
            before = (root / "state" / "research_decisions.json").read_text(encoding="utf-8")

            first = record_test_result(root, "tt_123", result_payload())
            replay = record_test_result(root, "tt_123", result_payload())
            retest = record_test_result(root, "tt_123", result_payload("INCONCLUSIVE", data="5 years"))

            self.assertEqual(first["result"], "WRITTEN")
            self.assertEqual(replay["result"], "NO_OP")
            self.assertEqual(retest["test_count"], 2)
            self.assertEqual((root / "state" / "research_decisions.json").read_text(encoding="utf-8"), before)

            got = get_analysis_queue_item(root, "tt_123")
            self.assertEqual([r["outcome"] for r in got["test_results"]], ["REJECTED", "INCONCLUSIVE"])
            self.assertEqual(got["decision"]["decision"], "TEST_CANDIDATE")
            listed = list_analysis_decisions(root)["items"][0]
            self.assertEqual(listed["test_outcome"], "INCONCLUSIVE")

    def test_only_test_or_backlog_candidates_can_get_results(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with(td, "RESEARCH")
            self.assertEqual(record_test_result(root, "tt_123", result_payload())["result"], "CONFLICT")
            self.assertEqual(record_test_result(root, "missing", result_payload())["result"], "NOT_FOUND")
            self.assertFalse((root / "state" / "research_test_results.json").exists())

    def test_validation_rejects_bad_outcome_missing_fields_and_wrong_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with(td, "BACKLOG_CANDIDATE")
            result = record_test_result(
                root, "tt_123", result_payload("MAYBE", summary="", evidence_lineage_id="other")
            )
            self.assertEqual(result["result"], "VALIDATION_ERROR")
            self.assertEqual(
                sorted(result["errors"]), ["BAD_OUTCOME", "EVIDENCE_LINEAGE_MISMATCH", "MISSING_SUMMARY"]
            )
            stored = json.loads((root / "state" / "research_test_results.json").read_text(encoding="utf-8")) \
                if (root / "state" / "research_test_results.json").exists() else {"items": {}}
            self.assertEqual(stored["items"], {})

    def test_decisions_without_results_list_no_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with(td, "TEST_CANDIDATE")
            self.assertIsNone(list_analysis_decisions(root)["items"][0]["test_outcome"])
            self.assertEqual(get_analysis_queue_item(root, "tt_123")["test_results"], [])


if __name__ == "__main__":
    unittest.main()
