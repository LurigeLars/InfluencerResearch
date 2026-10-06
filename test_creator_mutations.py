from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import creator_recent_check as crc
from creator_registry import (
    get_creator,
    list_creator_summaries,
    load_registry,
    register_creator,
    retire_creator,
    update_creator,
)


def base_request(key: str = "creator") -> dict:
    return {
        "creator_key": key,
        "display_name": key.title(),
        "sources": [
            {
                "platform": "TIKTOK",
                "profile_url": f"https://www.tiktok.com/@{key}",
                "evaluation_enabled": True,
                "monitoring_enabled": True,
                "priority": 10,
            },
            {
                "platform": "YOUTUBE",
                "profile_url": f"https://www.youtube.com/@{key}",
                "evaluation_enabled": True,
                "monitoring_enabled": False,
                "priority": 20,
            },
        ],
        "verification_methods": ["EXISTING_ACCEPTED_SOURCE_CONFIG"],
        "verification_refs": [f"https://www.tiktok.com/@{key}"],
        "request_id": f"register-{key}",
        "issued_by": "unit-test",
    }


class CreatorMutationTests(unittest.TestCase):
    def register(self, root: Path, key: str = "creator") -> dict:
        result = register_creator(root, base_request(key))
        self.assertEqual(result["result"], "REGISTERED")
        return get_creator(root, key)

    def test_source_monitoring_true_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.register(root)

            result = update_creator(
                root,
                {
                    "creator_key": "creator",
                    "issued_by": "unit-test",
                    "sources": [{"platform": "TIKTOK", "monitoring_enabled": False}],
                },
            )

            self.assertEqual(result["result"], "UPDATED")
            self.assertTrue(result["changed"])
            self.assertEqual(result["changed_sources"][0]["platform"], "TIKTOK")
            self.assertEqual(
                result["changed_sources"][0]["changes"]["monitoring_enabled"],
                {"from": True, "to": False},
            )
            source = next(
                source
                for source in get_creator(root, "creator")["sources"]
                if source["platform"] == "TIKTOK"
            )
            self.assertFalse(source["monitoring_enabled"])

    def test_creator_monitoring_is_derived_from_last_active_monitored_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.register(root)
            self.assertTrue(get_creator(root, "creator")["monitoring_enabled"])

            update_creator(
                root,
                {
                    "creator_key": "creator",
                    "sources": [{"platform": "TIKTOK", "monitoring_enabled": False}],
                },
            )

            self.assertFalse(get_creator(root, "creator")["monitoring_enabled"])

    def test_partial_update_preserves_unspecified_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = deepcopy(self.register(root))
            result = update_creator(
                root,
                {
                    "creator_key": "creator",
                    "sources": [{"platform": "TIKTOK", "priority": 11}],
                },
            )
            after = get_creator(root, "creator")

            self.assertEqual(result["result"], "UPDATED")
            before_tiktok = next(x for x in before["sources"] if x["platform"] == "TIKTOK")
            after_tiktok = next(x for x in after["sources"] if x["platform"] == "TIKTOK")
            self.assertEqual(after_tiktok["priority"], 11)
            for field in (
                "profile_url",
                "enabled",
                "evaluation_enabled",
                "monitoring_enabled",
                "verification_status",
            ):
                self.assertEqual(after_tiktok[field], before_tiktok[field])

            before_youtube = next(x for x in before["sources"] if x["platform"] == "YOUTUBE")
            after_youtube = next(x for x in after["sources"] if x["platform"] == "YOUTUBE")
            self.assertEqual(after_youtube, before_youtube)
            self.assertEqual(after["verification"], before["verification"])
            self.assertEqual(after["registration"], before["registration"])

    def test_same_update_is_noop_and_does_not_rewrite_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.register(root)
            first = update_creator(
                root,
                {
                    "creator_key": "creator",
                    "sources": [{"platform": "TIKTOK", "priority": 11}],
                },
            )
            self.assertEqual(first["result"], "UPDATED")
            registry_path = root / "control" / "creator_registry.json"
            before_bytes = registry_path.read_bytes()

            second = update_creator(
                root,
                {
                    "creator_key": "creator",
                    "sources": [{"platform": "TIKTOK", "priority": 11}],
                },
            )

            self.assertEqual(second["result"], "NO_OP")
            self.assertFalse(second["changed"])
            self.assertEqual(registry_path.read_bytes(), before_bytes)

    def test_retire_preserves_historical_creator_record_and_disables_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = deepcopy(self.register(root))
            result = retire_creator(root, "creator", "No longer useful", issued_by="unit-test")

            self.assertEqual(result["result"], "RETIRED")
            self.assertTrue(result["changed"])
            retired = get_creator(root, "creator", include_inactive=True)
            self.assertEqual(retired["status"], "RETIRED")
            self.assertFalse(retired["monitoring_enabled"])
            self.assertEqual(retired["verification"], before["verification"])
            self.assertEqual(retired["registration"], before["registration"])
            self.assertEqual(retired["retirement"]["reason"], "No longer useful")
            for source in retired["sources"]:
                self.assertFalse(source["enabled"])
                self.assertFalse(source["evaluation_enabled"])
                self.assertFalse(source["monitoring_enabled"])

            with self.assertRaisesRegex(KeyError, "CREATOR_NOT_REGISTERED"):
                get_creator(root, "creator")

    def test_retired_creator_is_excluded_from_monitored_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.register(root)
            self.assertEqual(len(crc.select_profiles_and_sources(root, "MONITORED", [])), 1)

            retire_creator(root, "creator", "Retire from active research", issued_by="unit-test")

            self.assertEqual(crc.select_profiles_and_sources(root, "MONITORED", []), [])

    def test_retired_creator_remains_readable_via_historical_get_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.register(root)
            retire_creator(root, "creator", "Historical only", issued_by="unit-test")

            profile = get_creator(root, "creator", include_inactive=True)

            self.assertEqual(profile["creator_key"], "creator")
            self.assertEqual(profile["status"], "RETIRED")

    def test_retire_is_idempotent_without_duplicate_audit_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.register(root)
            first = retire_creator(root, "creator", "First reason", issued_by="unit-test")
            self.assertEqual(first["result"], "RETIRED")
            registry_path = root / "control" / "creator_registry.json"
            before_bytes = registry_path.read_bytes()
            first_retirement = deepcopy(
                load_registry(root)["creators"]["creator"]["retirement"]
            )

            second = retire_creator(root, "creator", "Different retry reason", issued_by="unit-test")

            self.assertEqual(second["result"], "ALREADY_RETIRED")
            self.assertFalse(second["changed"])
            self.assertEqual(registry_path.read_bytes(), before_bytes)
            self.assertEqual(
                load_registry(root)["creators"]["creator"]["retirement"],
                first_retirement,
            )
            self.assertEqual(second["reason"], "First reason")

    def test_unknown_creator_returns_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            update_result = update_creator(
                root,
                {
                    "creator_key": "missing",
                    "sources": [{"platform": "TIKTOK", "monitoring_enabled": False}],
                },
            )
            retire_result = retire_creator(root, "missing", "Missing creator")

            self.assertEqual(update_result["result"], "NOT_FOUND")
            self.assertEqual(retire_result["result"], "NOT_FOUND")

    def test_mutations_do_not_touch_research_evidence_or_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before_profile = deepcopy(self.register(root))
            sentinels = {
                root / "state" / "research_queue.json": b'{"sentinel":"queue"}\n',
                root / "state" / "decisions.json": b'{"sentinel":"decisions"}\n',
                root / "output" / "evidence.json": b'{"sentinel":"evidence"}\n',
            }
            for path, data in sentinels.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

            update_creator(
                root,
                {
                    "creator_key": "creator",
                    "sources": [{"platform": "YOUTUBE", "evaluation_enabled": False}],
                },
            )
            retire_creator(root, "creator", "No further routine use", issued_by="unit-test")

            retired = get_creator(root, "creator", include_inactive=True)
            self.assertEqual(retired["verification"], before_profile["verification"])
            self.assertEqual(retired["registration"], before_profile["registration"])
            for path, data in sentinels.items():
                self.assertEqual(path.read_bytes(), data)

    def test_creator_list_is_deterministic_for_active_vs_retired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.register(root, "zeta")
            self.register(root, "alpha")
            retire_creator(root, "zeta", "Historical only", issued_by="unit-test")

            active = list_creator_summaries(root)
            all_visible = list_creator_summaries(root, include_retired=True)

            self.assertEqual(
                [item["creator_key"] for item in active],
                ["alpha"],
            )
            self.assertEqual(
                [item["creator_key"] for item in all_visible],
                ["alpha", "zeta"],
            )
            self.assertEqual(
                [item["status"] for item in all_visible],
                ["ACTIVE", "RETIRED"],
            )
            retired = all_visible[1]
            self.assertFalse(retired["monitoring_enabled"])
            self.assertEqual(retired["platforms"], [])


if __name__ == "__main__":
    unittest.main()
