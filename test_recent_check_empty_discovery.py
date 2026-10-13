"""A source that lists nothing must be flagged, not reported as a complete window."""
from pathlib import Path
import unittest

import creator_recent_check as crc

BASE = Path(__file__).resolve().parent


def _discovery(creator: str, platform: str, count: int, **extra) -> dict:
    return {
        "creator_key": creator,
        "platform": platform,
        "items": [],
        "discovery_count": count,
        "discovery_limit_used": 25,
        "window_complete": True,
        "coverage_limited_reason": None,
        **extra,
    }


class EmptyDiscoveryTests(unittest.TestCase):
    def test_empty_listing_is_flagged_and_no_longer_complete(self) -> None:
        empty = _discovery("kathy", "TIKTOK", 0)
        busy = _discovery("kathy", "INSTAGRAM", 60)

        flagged = crc._flag_empty_discoveries([empty, busy], [])

        self.assertEqual(flagged, [{
            "creator_key": "kathy",
            "platform": "TIKTOK",
            "stage": "DISCOVERY",
            "error": "NO_ITEMS_DISCOVERED",
            "next_action": "VERIFY_SOURCE_OR_DISABLE",
        }])
        self.assertFalse(empty["window_complete"])
        self.assertEqual(empty["coverage_limited_reason"], "NO_ITEMS_DISCOVERED")
        self.assertTrue(busy["window_complete"])
        self.assertIsNone(busy["coverage_limited_reason"])

    def test_a_source_that_already_failed_is_not_reported_twice(self) -> None:
        errored = _discovery("creator", "TIKTOK", 0)
        limited = _discovery("other", "YOUTUBE", 0, coverage_limited_reason="DISCOVERY_ERROR")

        flagged = crc._flag_empty_discoveries(
            [errored, limited],
            [{"creator_key": "creator", "platform": "tiktok", "stage": "DISCOVERY", "error": "boom"}],
        )

        self.assertEqual(flagged, [])
        self.assertTrue(errored["window_complete"])
        self.assertEqual(limited["coverage_limited_reason"], "DISCOVERY_ERROR")

    def test_main_run_reports_empty_sources_as_errors(self) -> None:
        source = (BASE / "creator_recent_check.py").read_text(encoding="utf-8")
        self.assertIn(
            "errors.extend(_flag_empty_discoveries(discoveries, discovery_errors))",
            source,
        )

    def test_an_empty_source_makes_the_run_partial(self) -> None:
        state, ready = crc._recent_check_final_state(
            errors=[{"stage": "DISCOVERY", "error": "NO_ITEMS_DISCOVERED"}],
            discoveries=[_discovery("kathy", "INSTAGRAM", 60)],
            deferred_extraction=[],
            extraction_errors=[],
            pending_extraction=[],
        )
        self.assertEqual(state, "PARTIAL")
        self.assertFalse(ready)


if __name__ == "__main__":
    unittest.main()
