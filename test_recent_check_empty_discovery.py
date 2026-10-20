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
            "diagnostic_classification": None,
            "next_action": "VERIFY_SOURCE_OR_DISABLE",
        }])
        self.assertFalse(empty["window_complete"])
        self.assertEqual(empty["coverage_limited_reason"], "NO_ITEMS_DISCOVERED")
        self.assertTrue(busy["window_complete"])
        self.assertIsNone(busy["coverage_limited_reason"])


    def test_tiktok_page_diagnosis_is_propagated_without_claiming_coverage(self) -> None:
        empty = _discovery(
            "stockrobber", "TIKTOK", 0,
            discovery={"empty_page_diagnostic": {
                "classification": "RENDERED_PAGE_WITHOUT_VIDEO_LINKS",
                "snapshot_text_chars_examined": 410,
                "retry_automatically": False,
            }},
        )
        flagged = crc._flag_empty_discoveries([empty], [])
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["diagnostic_classification"], "RENDERED_PAGE_WITHOUT_VIDEO_LINKS")
        self.assertEqual(flagged[0]["next_action"], "REVIEW_PARSER_OR_SOURCE")
        self.assertFalse(empty["window_complete"])

    def test_tiktok_login_is_not_retried_or_marked_complete(self) -> None:
        empty = _discovery(
            "nicholascrown", "TIKTOK", 0,
            discovery={"empty_page_diagnostic": {"classification": "LOGIN_REQUIRED"}},
        )
        flagged = crc._flag_empty_discoveries([empty], [])
        self.assertEqual(flagged[0]["next_action"], "MANUAL_ACCESS_REVIEW_NO_BYPASS")
        self.assertEqual(empty["coverage_limited_reason"], "NO_ITEMS_DISCOVERED")

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

    def test_an_unknown_count_is_not_treated_as_zero(self) -> None:
        unknown = _discovery("creator", "YOUTUBE", 0)
        del unknown["discovery_count"]

        self.assertEqual(crc._flag_empty_discoveries([unknown], []), [])
        self.assertTrue(unknown["window_complete"])
        self.assertIsNone(unknown["coverage_limited_reason"])

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
