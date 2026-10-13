from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import creator_recent_check as crc
import mcp_job_worker


class RecentWindowTimezoneTests(unittest.TestCase):
    def test_today_uses_stockholm_summer_time(self) -> None:
        fixed = datetime(2026, 9, 26, 0, 30, tzinfo=timezone.utc)
        with patch.object(crc, "now_utc", return_value=fixed):
            start, end, meta = crc.resolve_window("TODAY", None)

        self.assertEqual(end, fixed)
        self.assertEqual(start, datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc))
        self.assertEqual(meta["calendar_date"], "2026-09-26")
        self.assertEqual(meta["timezone"], "Europe/Stockholm")
        self.assertEqual(meta["timezone_source"], "IANA_ZONEINFO")
        self.assertEqual(meta["system_utc_offset_minutes"], 120)

    def test_today_uses_stockholm_winter_time(self) -> None:
        fixed = datetime(2026, 1, 15, 0, 30, tzinfo=timezone.utc)
        with patch.object(crc, "now_utc", return_value=fixed):
            start, end, meta = crc.resolve_window("TODAY", None)

        self.assertEqual(end, fixed)
        self.assertEqual(start, datetime(2026, 1, 14, 23, 0, tzinfo=timezone.utc))
        self.assertEqual(meta["calendar_date"], "2026-01-15")
        self.assertEqual(meta["system_utc_offset_minutes"], 60)

    def test_last_n_days_remains_utc_relative(self) -> None:
        fixed = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        with patch.object(crc, "now_utc", return_value=fixed):
            start, end, meta = crc.resolve_window("LAST_N_DAYS", 3)

        self.assertEqual(end, fixed)
        self.assertEqual(start, datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(meta, {"window": "LAST_N_DAYS", "timezone": "UTC", "lookback_days": 3})


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def decide(**overrides) -> dict:
    args = {
        "scope": "MONITORED",
        "creator_keys": [],
        "window": "SINCE_LAST_RUN",
        "cutoff": NOW - timedelta(days=1),
        "end": NOW,
        "previous": NOW - timedelta(days=1),
        "errors": [],
        "extraction_errors": [],
        "pending_extraction": [],
    }
    return crc._watermark_decision(**{**args, **overrides})


class CatchUpWindowTests(unittest.TestCase):
    def resolve(self, watermark: datetime | None) -> tuple[datetime, dict]:
        with patch.object(crc, "now_utc", return_value=NOW):
            start, end, meta = crc.resolve_window("SINCE_LAST_RUN", None, watermark)
        self.assertEqual(end, NOW)
        return start, meta

    def test_resumes_from_the_watermark(self) -> None:
        watermark = NOW - timedelta(days=3, hours=5)
        start, meta = self.resolve(watermark)

        self.assertEqual(start, watermark)
        self.assertEqual(meta["basis"], "WATERMARK")
        self.assertEqual(meta["watermark"], watermark.isoformat())

    def test_never_reaches_further_back_than_the_ceiling(self) -> None:
        start, meta = self.resolve(NOW - timedelta(days=90))

        self.assertEqual(start, NOW - timedelta(days=crc.MAX_CATCH_UP_DAYS))
        self.assertEqual(meta["basis"], "WATERMARK_OLDER_THAN_MAX_CATCH_UP")

    def test_without_a_watermark_it_looks_one_day_back_and_says_so(self) -> None:
        start, meta = self.resolve(None)

        self.assertEqual(start, NOW - timedelta(days=1))
        self.assertEqual(meta["basis"], "NO_WATERMARK_LAST_24H")
        self.assertIsNone(meta["watermark"])

    def test_a_watermark_in_the_future_gives_an_empty_window_not_a_negative_one(self) -> None:
        start, _ = self.resolve(NOW + timedelta(hours=2))

        self.assertEqual(start, NOW)

    def test_unreadable_watermark_file_counts_as_no_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(crc.load_watermark(root))
            path = crc._watermark_path(root)
            path.parent.mkdir(parents=True)
            for content in ("not json", "[]", '{"covered_until": "yesterday"}'):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    self.assertIsNone(crc.load_watermark(root))
            path.write_text(f'{{"covered_until": "{NOW.isoformat()}"}}', encoding="utf-8")
            self.assertEqual(crc.load_watermark(root), NOW)


class WatermarkDecisionTests(unittest.TestCase):
    def test_clean_full_run_moves_the_watermark_to_its_end(self) -> None:
        result = decide()

        self.assertTrue(result["advanced"])
        self.assertEqual(result["covered_until"], NOW.isoformat())
        self.assertEqual(result["blocked_by"], [])

    def test_anything_left_unlisted_capped_or_unfetched_holds_it_back(self) -> None:
        previous = NOW - timedelta(days=1)
        cases = {
            "DISCOVERY": {"errors": [{"stage": "DISCOVERY", "error": "NO_ITEMS_DISCOVERED"}]},
            "COVERAGE": {"errors": [{"stage": "COVERAGE", "error": "WINDOW_MAY_BE_TRUNCATED"}]},
            "INGESTION_CAP": {"errors": [{"stage": "INGESTION_CAP", "error": "DEFERRED_DUE_TO_CAP:5"}]},
            "INGESTION": {"errors": [{"stage": "INGESTION", "error": "TIMEOUT"}]},
            "ANALYSIS_QUEUE": {"errors": [{"stage": "ANALYSIS_QUEUE", "error": "ANALYSIS_TARGET_MISSING:1"}]},
            "RESEARCH_QUEUE": {"errors": [{"stage": "RESEARCH_QUEUE", "error": "QUEUE_REFRESH_FAILED:x"}]},
            # An error kind nobody has classified holds it back too.
            "SOMETHING_NEW": {"errors": [{"stage": "SOMETHING_NEW", "error": "x"}]},
            "UNKNOWN": {"errors": ["not a dict"]},
            "EXTRACTION_ERROR": {"extraction_errors": [{"item_key": "yt_a"}]},
            "PENDING_EXTRACTION": {"pending_extraction": [{"item_key": "yt_a"}]},
        }
        for blocker, overrides in cases.items():
            with self.subTest(blocker=blocker):
                result = decide(**overrides)

                self.assertFalse(result["advanced"])
                self.assertEqual(result["blocked_by"], [blocker])
                self.assertEqual(result["covered_until"], previous.isoformat())

    def test_problems_a_later_window_cannot_repair_do_not_hold_it_back(self) -> None:
        result = decide(errors=[
            {"stage": "STORY_PREFETCH", "error": "TIMEOUT:120s"},
            {"stage": "STORY_INGESTION", "error": "x"},
            {"stage": "CONTENT_EXTRACTION", "error": "INSUFFICIENT_CONTENT:2"},
        ])

        self.assertTrue(result["advanced"])

    def test_only_a_full_monitored_run_moves_it(self) -> None:
        for overrides in ({"scope": "ALL_REGISTERED"}, {"creator_keys": ["kathylien"]}):
            with self.subTest(overrides=overrides):
                result = decide(**overrides)

                self.assertFalse(result["advanced"])
                self.assertEqual(result["blocked_by"], ["NOT_A_FULL_MONITORED_RUN"])

    def test_a_fixed_window_that_leaves_a_gap_does_not_move_it(self) -> None:
        # Watermark yesterday 18:00, TODAY starts at midnight: 18:00-00:00 was never read.
        result = decide(
            window="TODAY",
            previous=NOW - timedelta(hours=18),
            cutoff=NOW - timedelta(hours=12),
        )

        self.assertFalse(result["advanced"])
        self.assertEqual(result["blocked_by"], ["WINDOW_STARTS_AFTER_WATERMARK"])

    def test_a_fixed_window_that_reaches_the_watermark_moves_it(self) -> None:
        result = decide(window="LAST_7_DAYS", cutoff=NOW - timedelta(days=7))

        self.assertTrue(result["advanced"])

    def test_first_clean_run_creates_the_watermark(self) -> None:
        result = decide(window="TODAY", previous=None, cutoff=NOW - timedelta(hours=12))

        self.assertTrue(result["advanced"])
        self.assertIsNone(result["previous"])
        self.assertEqual(result["covered_until"], NOW.isoformat())

    def test_catch_up_past_the_ceiling_still_moves_it(self) -> None:
        # Otherwise a watermark older than the ceiling could never move again.
        result = decide(
            previous=NOW - timedelta(days=90),
            cutoff=NOW - timedelta(days=crc.MAX_CATCH_UP_DAYS),
        )

        self.assertTrue(result["advanced"])

    def test_job_worker_passes_the_catch_up_window_through(self) -> None:
        _, args = mcp_job_worker.build_invocation({
            "kind": "creator_recent_check",
            "params": {"scope": "MONITORED", "window": "SINCE_LAST_RUN", "max_items": 20},
        })

        self.assertEqual(args[args.index("--window") + 1], "SINCE_LAST_RUN")
        self.assertNotIn("--lookback-days", args)

    def test_the_run_wires_the_decision_into_status_and_state(self) -> None:
        source = Path(crc.__file__).read_text(encoding="utf-8")
        self.assertIn("previous_watermark = load_watermark(root)", source)
        self.assertIn('if watermark["advanced"]:', source)
        self.assertIn('"watermark": watermark,', source)


if __name__ == "__main__":
    unittest.main()
