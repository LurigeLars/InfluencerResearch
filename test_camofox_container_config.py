from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase, main, mock

import camofox_container as cc


class CamofoxContainerConfigTests(TestCase):
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
            self.assertIsNone(result["proxy_base_url"])
            self.assertNotIn("transfer_dir", result)
            self.assertFalse(
                (local.resolve() / "InfluencerResearch" / "camofox-transfer").exists()
            )

    def test_loads_internal_service_config_from_runtime_secret_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            access_path = root / "camofox_access_key"
            admin_path = root / "camofox_admin_key"
            access_path.write_text("a" * 43, encoding="utf-8")
            admin_path.write_text("b" * 43, encoding="utf-8")
            with (
                mock.patch.object(cc, "CAMOFOX_CONTAINER_ACCESS_SECRET", access_path),
                mock.patch.object(cc, "CAMOFOX_CONTAINER_ADMIN_SECRET", admin_path),
            ):
                result = cc.load_config({
                    "INFLUENCER_RESEARCH_CONTAINER": "1",
                    "CAMOFOX_ACCESS_KEY": "SHOULD_NOT_BE_USED",
                    "CAMOFOX_ADMIN_KEY": "SHOULD_NOT_BE_USED",
                })
        self.assertEqual(result["base_url"], "http://camofox:9377")
        self.assertIsNone(result["proxy_base_url"])
        self.assertEqual(result["access_key"], "a" * 43)
        self.assertEqual(result["admin_key"], "b" * 43)
        self.assertIsNone(result["config_path"])

    def test_container_runtime_can_expose_internal_proxy_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            access_path = root / "camofox_access_key"
            admin_path = root / "camofox_admin_key"
            access_path.write_text("a" * 43, encoding="utf-8")
            admin_path.write_text("b" * 43, encoding="utf-8")
            with (
                mock.patch.object(cc, "CAMOFOX_CONTAINER_ACCESS_SECRET", access_path),
                mock.patch.object(cc, "CAMOFOX_CONTAINER_ADMIN_SECRET", admin_path),
            ):
                result = cc.load_config({
                    "INFLUENCER_RESEARCH_CONTAINER": "1",
                    "INFLUENCER_RESEARCH_CAMOFOX_PROXY_BASE_URL": "http://camofox-public-proxy:9377",
                })
        self.assertEqual(result["base_url"], "http://camofox:9377")
        self.assertEqual(
            result["proxy_base_url"],
            "http://camofox-public-proxy:9377",
        )

    def test_rejects_short_service_secret_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            access_path = root / "camofox_access_key"
            admin_path = root / "camofox_admin_key"
            access_path.write_text("short", encoding="utf-8")
            admin_path.write_text("b" * 43, encoding="utf-8")
            with (
                mock.patch.object(cc, "CAMOFOX_CONTAINER_ACCESS_SECRET", access_path),
                mock.patch.object(cc, "CAMOFOX_CONTAINER_ADMIN_SECRET", admin_path),
                self.assertRaisesRegex(RuntimeError, "too short"),
            ):
                cc.load_config({"INFLUENCER_RESEARCH_CONTAINER": "1"})

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
    main()
