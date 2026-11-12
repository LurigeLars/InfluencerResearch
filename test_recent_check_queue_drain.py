from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import mcp_job_worker as worker


def _capped(started_at: str, deferred: int = 5) -> dict:
    return {
        "state": "PARTIAL",
        "started_at": started_at,
        "selected_for_ingestion_count": 10,
        "deferred_due_to_cap_count": deferred,
        "errors": [{"stage": "INGESTION_CAP", "error": f"DEFERRED_DUE_TO_CAP:{deferred}"}],
    }


class QueueDrainTests(unittest.TestCase):
    def _drain(self, batches: list[dict | None], **kwargs) -> tuple[int, dict, int]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            calls = 0

            def run_round() -> int:
                nonlocal calls
                batch = batches[calls]
                calls += 1
                if batch is not None:
                    path.write_text(json.dumps(batch), encoding="utf-8")
                return 0 if batch and batch["state"] == "COMPLETE" else 1

            kwargs.setdefault("timeout_seconds", 3600)
            rc = worker.run_recent_check_drain(
                Path("x.py"), [], status_path=path, run_round=run_round, **kwargs
            )
            return rc, json.loads(path.read_text(encoding="utf-8")), calls

    def test_runs_batches_until_the_cap_holds_nothing_back(self):
        rc, status, calls = self._drain([
            _capped("t1", 19),
            _capped("t2", 9),
            {"state": "COMPLETE", "started_at": "t3", "selected_for_ingestion_count": 9, "errors": []},
        ])
        self.assertEqual(calls, 3)
        self.assertEqual(rc, 0)
        self.assertEqual(status["state"], "COMPLETE")
        self.assertEqual(status["queue_drain"]["stop_reason"], "QUEUE_EMPTY")
        self.assertEqual([r["deferred_due_to_cap_count"] for r in status["queue_drain"]["rounds"]], [19, 9, None])

    def test_a_real_error_stops_the_drain(self):
        failing = _capped("t1")
        failing["errors"].append({"stage": "DISCOVERY", "error": "boom"})
        _, status, calls = self._drain([failing, _capped("t2")])
        self.assertEqual(calls, 1)
        self.assertEqual(status["queue_drain"]["stop_reason"], "BATCH_ERRORS")

    def test_unrepairable_errors_do_not_stop_the_drain(self):
        first = _capped("t1")
        first["errors"].append({"stage": "YOUTUBE_INGESTION", "error": "INSUFFICIENT_CONTENT:no speech"})
        _, status, calls = self._drain([first, {"state": "COMPLETE", "started_at": "t2", "errors": []}])
        self.assertEqual(calls, 2)
        self.assertEqual(status["queue_drain"]["stop_reason"], "QUEUE_EMPTY")

    def test_round_limit_is_a_hard_ceiling(self):
        _, status, calls = self._drain([_capped(f"t{i}") for i in range(10)], max_rounds=3)
        self.assertEqual(calls, 3)
        self.assertEqual(status["queue_drain"]["stop_reason"], "MAX_ROUNDS")

    def test_no_new_batch_when_it_would_not_fit_before_the_watchdog(self):
        ticks = iter(range(0, 10_000, 100))
        _, status, calls = self._drain(
            [_capped(f"t{i}") for i in range(10)], timeout_seconds=500, clock=lambda: next(ticks)
        )
        self.assertEqual(calls, 2)
        self.assertEqual(status["queue_drain"]["stop_reason"], "TIME_BUDGET")

    def test_a_batch_that_wrote_no_status_stops_the_drain(self):
        _, status, calls = self._drain([_capped("t1"), None, _capped("t3")])
        self.assertEqual(calls, 2)
        self.assertEqual(status["started_at"], "t1")
        self.assertEqual(status["queue_drain"]["stop_reason"], "BATCH_WROTE_NO_STATUS")

    def test_stopped_batch_is_not_rerun(self):
        _, status, calls = self._drain([{"state": "STOPPED", "started_at": "t1"}, _capped("t2")])
        self.assertEqual(calls, 1)
        self.assertEqual(status["queue_drain"]["stop_reason"], "BATCH_STOPPED")


if __name__ == "__main__":
    unittest.main()
