from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creator_registry import (
    get_creator,
    register_creator,
    select_monitor_sources,
    update_creator,
)
import creator_recent_check as crc


def _register(root: Path, *, tiktok_monitored: bool, instagram_monitored: bool) -> None:
    register_creator(
        root,
        {
            "creator_key": "creator",
            "display_name": "Creator",
            "sources": [
                {
                    "platform": "TIKTOK",
                    "profile_url": "https://www.tiktok.com/@creator",
                    "evaluation_enabled": True,
                    "monitoring_enabled": tiktok_monitored,
                    "priority": 10,
                },
                {
                    "platform": "INSTAGRAM",
                    "profile_url": "https://www.instagram.com/creator/",
                    "evaluation_enabled": False,
                    "monitoring_enabled": instagram_monitored,
                    "priority": 50,
                },
            ],
            "verification_methods": ["HANDLE_BRANDING_BIO_CROSSCHECK"],
            "verification_refs": [
                "https://www.tiktok.com/@creator",
                "https://www.instagram.com/creator/",
            ],
            "request_id": "test",
            "issued_by": "unit-test",
        },
    )


def _selected_platforms(root: Path) -> list[set[str]]:
    return [
        {source["platform"] for source in sources}
        for _, sources in crc.select_profiles_and_sources(root, "MONITORED", [])
    ]


class MonitoredInstagramSourceTests(unittest.TestCase):
    def test_monitored_creator_recent_check_includes_registered_instagram(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            register_creator(
                root,
                {
                    "creator_key": "creator",
                    "display_name": "Creator",
                    "sources": [
                        {
                            "platform": "TIKTOK",
                            "profile_url": "https://www.tiktok.com/@creator",
                            "evaluation_enabled": True,
                            "monitoring_enabled": True,
                            "priority": 10,
                        },
                        {
                            "platform": "INSTAGRAM",
                            "profile_url": "https://www.instagram.com/creator/",
                            "evaluation_enabled": False,
                            "monitoring_enabled": False,
                            "priority": 50,
                        },
                    ],
                    "verification_methods": ["HANDLE_BRANDING_BIO_CROSSCHECK"],
                    "verification_refs": [
                        "https://www.tiktok.com/@creator",
                        "https://www.instagram.com/creator/",
                    ],
                    "request_id": "test",
                    "issued_by": "unit-test",
                },
            )

            selected = crc.select_profiles_and_sources(root, "MONITORED", [])
            self.assertEqual(len(selected), 1)
            profile, sources = selected[0]
            self.assertEqual(profile["creator_key"], "creator")
            self.assertEqual(
                {source["platform"] for source in sources},
                {"TIKTOK", "INSTAGRAM"},
            )

    def test_instagram_alone_is_enough_to_be_monitored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _register(root, tiktok_monitored=False, instagram_monitored=True)

            self.assertTrue(get_creator(root, "creator")["monitoring_enabled"])
            # Only the monitored source is read; the unmonitored TikTok is left alone.
            self.assertEqual(_selected_platforms(root), [{"INSTAGRAM"}])

    def test_creator_survives_losing_its_only_youtube_or_tiktok_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _register(root, tiktok_monitored=True, instagram_monitored=False)

            update_creator(
                root,
                {
                    "creator_key": "creator",
                    "sources": [
                        {"platform": "INSTAGRAM", "monitoring_enabled": True},
                        {"platform": "TIKTOK", "enabled": False, "monitoring_enabled": False},
                    ],
                    "request_id": "test-update",
                    "issued_by": "unit-test",
                },
            )

            self.assertTrue(get_creator(root, "creator")["monitoring_enabled"])
            self.assertEqual(_selected_platforms(root), [{"INSTAGRAM"}])

    def test_no_monitored_source_at_all_still_excludes_the_creator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _register(root, tiktok_monitored=False, instagram_monitored=False)

            self.assertFalse(get_creator(root, "creator")["monitoring_enabled"])
            self.assertEqual(_selected_platforms(root), [])

    def test_persistent_monitor_adapter_still_gets_only_youtube_and_tiktok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _register(root, tiktok_monitored=True, instagram_monitored=True)
            profile = get_creator(root, "creator")

            self.assertEqual(
                [source["platform"] for source in select_monitor_sources(profile)],
                ["TIKTOK"],
            )
            self.assertEqual(
                {source["platform"] for source in select_monitor_sources(profile, crc.SUPPORTED_PLATFORMS)},
                {"TIKTOK", "INSTAGRAM"},
            )


if __name__ == "__main__":
    unittest.main()
