from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import tiktok_camofox_sync as sync


class CamofoxManifestValidationTests(unittest.TestCase):
    def _write_manifests(
        self,
        root: Path,
        *,
        package_dependencies: dict[str, str] | None = None,
        lock_dependencies: dict[str, str] | None = None,
        bom_crlf: bool = False,
    ) -> None:
        expected = dict(sync.CAMOFOX_ACCEPTED_ROOT_DEPENDENCIES)
        package = {
            "name": sync.CAMOFOX_ACCEPTED_ROOT_PACKAGE_NAME,
            "version": "0.0.0",
            "private": True,
            "dependencies": package_dependencies if package_dependencies is not None else expected,
        }
        lock = {
            "name": sync.CAMOFOX_ACCEPTED_ROOT_PACKAGE_NAME,
            "lockfileVersion": 3,
            "packages": {
                "": {
                    "name": sync.CAMOFOX_ACCEPTED_ROOT_PACKAGE_NAME,
                    "dependencies": lock_dependencies if lock_dependencies is not None else expected,
                }
            },
        }

        for name, value in (("package.json", package), ("package-lock.json", lock)):
            text = json.dumps(value, indent=2) + "\n"
            if bom_crlf:
                text = "\ufeff" + text.replace("\n", "\r\n")
            (root / name).write_text(text, encoding="utf-8")

    def test_accepts_bom_and_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_manifests(root, bom_crlf=True)
            result = sync._verify_camofox_root_manifests(root)
            self.assertEqual(result["dependencies"], sync.CAMOFOX_ACCEPTED_ROOT_DEPENDENCIES)
            self.assertEqual(result["lockfile_version"], 3)

    def test_rejects_extra_package_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deps = dict(sync.CAMOFOX_ACCEPTED_ROOT_DEPENDENCIES)
            deps["unexpected-package"] = "1.0.0"
            self._write_manifests(root, package_dependencies=deps)
            with self.assertRaisesRegex(RuntimeError, "root dependencies"):
                sync._verify_camofox_root_manifests(root)

    def test_rejects_lock_root_dependency_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deps = dict(sync.CAMOFOX_ACCEPTED_ROOT_DEPENDENCIES)
            deps["@askjo/camofox-browser"] = "9.9.9"
            self._write_manifests(root, lock_dependencies=deps)
            with self.assertRaisesRegex(RuntimeError, "package-lock root dependencies"):
                sync._verify_camofox_root_manifests(root)


if __name__ == "__main__":
    unittest.main()
