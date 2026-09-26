from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from creator_registry import get_creator, load_registry, register_creator


class CreatorRegistryBootstrapTests(unittest.TestCase):
    def test_missing_registry_is_empty_without_creating_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "control" / "creator_registry.json"

            registry = load_registry(root)

            self.assertEqual(registry, {"schema_version": 1, "creators": {}})
            self.assertFalse(path.exists())

    def test_first_registration_bootstraps_registry_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = register_creator(
                root,
                {
                    "creator_key": "example_creator",
                    "display_name": "Example Creator",
                    "sources": [
                        {
                            "platform": "YOUTUBE",
                            "profile_url": "https://www.youtube.com/@example_creator",
                            "evaluation_enabled": True,
                            "monitoring_enabled": False,
                            "priority": 100,
                        }
                    ],
                    "verification_methods": ["MULTI_SOURCE_CORROBORATION"],
                    "verification_refs": ["https://example.com/creator"],
                    "request_id": "bootstrap-test",
                    "issued_by": "unit-test",
                },
            )

            self.assertEqual(result["result"], "REGISTERED")
            path = root / "control" / "creator_registry.json"
            self.assertTrue(path.is_file())

            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 1)
            self.assertIn("example_creator", persisted["creators"])
            self.assertEqual(get_creator(root, "example_creator")["display_name"], "Example Creator")


if __name__ == "__main__":
    unittest.main()
