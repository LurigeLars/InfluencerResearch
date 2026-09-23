from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import camofox_container as cc


class CamofoxContainerConfigTests(unittest.TestCase):
    def _env(self, root: Path) -> dict[str, str]:
        return {"LOCALAPPDATA": str(root)}

    def test_loads_fixed_loopback_runtime_without_host_transfer_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            cfg = cc.config_path(self._env(local))
            cfg.parent.mkdir(parents=True)
            cfg.write_text(
                json.dumps({
                    "schema_version": 1,
                    "access_key": "a" * 43,
                    "admin_key": "b" * 43,
                }),
                encoding="utf-8",
            )
            result = cc.load_config(self._env(local))
            self.assertEqual(result["base_url"], "http://127.0.0.1:9377")
            self.assertNotIn("transfer_dir", result)
            self.assertFalse(
                (local.resolve() / "InfluencerResearch" / "camofox-transfer").exists()
            )

    def test_rejects_short_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            cfg = cc.config_path(self._env(local))
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
                cc.load_config(self._env(local))

    def test_rejects_unknown_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            cfg = cc.config_path(self._env(local))
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
                cc.load_config(self._env(local))


if __name__ == "__main__":
    unittest.main()
