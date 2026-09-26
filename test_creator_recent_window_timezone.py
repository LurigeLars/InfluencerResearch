from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import creator_recent_check as crc


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


if __name__ == "__main__":
    unittest.main()
