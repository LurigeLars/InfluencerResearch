from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

sys.modules.setdefault("instagram_camofox_public_smoke", ModuleType("instagram_camofox_public_smoke"))
sys.modules.setdefault("ephemeral_ingest", ModuleType("ephemeral_ingest"))

from creator_registry import register_creator
import creator_recent_check as crc


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


if __name__ == "__main__":
    unittest.main()
