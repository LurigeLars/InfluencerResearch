from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import camofox_container as cc


class CamofoxContainerConfigTests(unittest.TestCase):
    def _runtime_root(self, root: Path):
        return mock.patch.object(
            cc, "_localappdata_root", return_value=root.resolve() / "InfluencerResearch"
        )

    def test_loads_fixed_loopback_runtime_without_host_transfer_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            with self._runtime_root(local):
                cfg = cc.config_path()
                cfg.parent.mkdir(parents=True)
                cfg.write_text(
                    json.dumps({
                        "schema_version": 1,
                        "access_key": "a" * 43,
                        "admin_key": "b" * 43,
                    }),
                    encoding="utf-8",
                )
                result = cc.load_config({})
            self.assertEqual(result["base_url"], "http://127.0.0.1:9377")
            self.assertNotIn("transfer_dir", result)
            self.assertFalse(
                (local.resolve() / "InfluencerResearch" / "camofox-transfer").exists()
            )

    def test_loads_internal_service_config_from_environment(self) -> None:
        result = cc.load_config({
            "INFLUENCER_RESEARCH_CONTAINER": "1",
            "CAMOFOX_ACCESS_KEY": "a" * 43,
            "CAMOFOX_ADMIN_KEY": "b" * 43,
        })
        self.assertEqual(result["base_url"], "http://camofox:9377")
        self.assertEqual(result["access_key"], "a" * 43)
        self.assertEqual(result["admin_key"], "b" * 43)
        self.assertIsNone(result["config_path"])

    def test_rejects_short_service_keys(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "too short"):
            cc.load_config({
                "INFLUENCER_RESEARCH_CONTAINER": "1",
                "CAMOFOX_ACCESS_KEY": "short",
                "CAMOFOX_ADMIN_KEY": "short",
            })

    def test_rejects_short_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            with self._runtime_root(local):
                cfg = cc.config_path()
                cfg.parent.mkdir(parents=True)
                cfg.write_text(
                    json.dumps({
                        "schema_version": 1,
                        "access_key": "short",
                        "admin_key": "short",
                    }),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(RuntimeError, "too short"):
                    cc.load_config({})

    def test_rejects_unknown_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            with self._runtime_root(local):
                cfg = cc.config_path()
                cfg.parent.mkdir(parents=True)
                cfg.write_text(
                    json.dumps({
                        "schema_version": 2,
                        "access_key": "a" * 43,
                        "admin_key": "b" * 43,
                    }),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                    cc.load_config({})


if __name__ == "__main__":
    unittest.main()
