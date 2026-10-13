"""A cap deferral must be named as such, not reported as a missing analysis target."""
from pathlib import Path
import unittest

import creator_recent_check as crc

BASE = Path(__file__).resolve().parent


def _state(item_key: str, analysis_state: str) -> dict:
    return {
        "item_key": item_key,
        "creator_key": "creator",
        "platform": "TIKTOK",
        "source_id": item_key,
        "published_at": "2026-10-09T16:54:36+02:00",
        "analysis_state": analysis_state,
    }


class CapDeferralTests(unittest.TestCase):
    def test_cap_deferred_items_are_not_missing_targets(self) -> None:
        states = [
            _state("queued", "QUEUED"),
            _state("duplicate", "DUPLICATE"),
            _state("postponed", "MISSING"),
            _state("lost", "MISSING"),
        ]
        cap_deferred, missing = crc._split_unaccounted_candidates(
            states,
            [{"item_key": "postponed"}],
        )

        self.assertEqual([item["item_key"] for item in cap_deferred], ["postponed"])
        self.assertEqual(cap_deferred[0]["analysis_state"], "DEFERRED_DUE_TO_CAP")
        self.assertEqual(cap_deferred[0]["reason"], "INGESTION_CAP_REACHED")
        self.assertEqual([item["item_key"] for item in missing], ["lost"])
        self.assertEqual(missing[0]["analysis_state"], "MISSING")
        self.assertEqual(
            missing[0]["reason"],
            "NOT_QUEUED_NOT_NONREADY_NOT_FINALIZED_NOT_DUPLICATE",
        )
        # The per-item state list that the status file publishes is updated in place.
        self.assertEqual(
            {item["item_key"]: item["analysis_state"] for item in states},
            {
                "queued": "QUEUED",
                "duplicate": "DUPLICATE",
                "postponed": "DEFERRED_DUE_TO_CAP",
                "lost": "MISSING",
            },
        )

    def test_nothing_deferred_leaves_missing_untouched(self) -> None:
        states = [_state("lost", "MISSING"), _state("queued", "QUEUED")]
        cap_deferred, missing = crc._split_unaccounted_candidates(states, [])
        self.assertEqual(cap_deferred, [])
        self.assertEqual([item["item_key"] for item in missing], ["lost"])

    def test_cap_deferral_is_reported_with_its_own_reason_and_next_step(self) -> None:
        source = (BASE / "creator_recent_check.py").read_text(encoding="utf-8")
        self.assertIn('"error": f"DEFERRED_DUE_TO_CAP:{len(cap_deferred_items)}"', source)
        self.assertIn('"next_action": "RERUN_SAME_WINDOW"', source)
        # A real gap keeps its original name.
        self.assertIn('"error": f"ANALYSIS_TARGET_MISSING:{len(analysis_missing_items)}"', source)

    def test_a_run_that_only_hit_the_cap_stays_partial(self) -> None:
        state, ready = crc._recent_check_final_state(
            errors=[{"stage": "INGESTION_CAP", "error": "DEFERRED_DUE_TO_CAP:5"}],
            discoveries=[{"creator_key": "creator"}],
            deferred_extraction=[],
            extraction_errors=[],
            pending_extraction=[],
        )
        self.assertEqual(state, "PARTIAL")
        self.assertFalse(ready)


if __name__ == "__main__":
    unittest.main()
