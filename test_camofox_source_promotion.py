"""Offline regression checks for immutable Camofox fork promotion."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import propose_camofox_source_update as promote

OLD = "a" * 40
NEW = "b" * 40


class CamofoxSourcePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.package = root / "package.json"
        self.lock = root / "package-lock.json"
        self.sync = root / "sync.py"
        self.test = root / "runtime.test.py"
        self.readme = root / "README.md"
        archive = f"https://github.com/LurigeLars/camofox-browser/archive/{OLD}.tar.gz"
        self.package.write_text(json.dumps({"dependencies": {"@askjo/camofox-browser": archive}}))
        self.lock.write_text(json.dumps({"packages": {
            "": {"dependencies": {"@askjo/camofox-browser": archive}},
            "node_modules/@askjo/camofox-browser": {"resolved": archive}
        }}))
        for path in (self.sync, self.test, self.readme):
            path.write_text(f"Reviewed source {OLD}.\n")
        self.paths = {
            "PACKAGE": self.package, "LOCK": self.lock,
            "SYNC": self.sync, "TEST": self.test, "README": self.readme,
            "ROOT": root,
        }
        patcher = patch.multiple(promote, **self.paths)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_replaces_all_reviewed_source_references_only(self):
        self.assertTrue(promote.update_pin(NEW))
        self.assertFalse(promote.update_pin(NEW))
        self.assertIn(NEW, self.package.read_text())
        for path in (self.sync, self.test, self.readme):
            self.assertIn(NEW, path.read_text())
            self.assertNotIn(OLD, path.read_text())
        # npm must regenerate this; the helper must NOT fabricate lock integrity.
        self.assertIn(OLD, self.lock.read_text())

    def test_rejects_bad_sha_without_modifying_files(self):
        original = self.package.read_text()
        with self.assertRaises(ValueError):
            promote.update_pin("master")
        self.assertEqual(self.package.read_text(), original)

    def test_rejects_inconsistent_lock_without_writes(self):
        self.lock.write_text("{}")
        original = self.package.read_text()
        with self.assertRaises((ValueError, KeyError)):
            promote.update_pin(NEW)
        self.assertEqual(self.package.read_text(), original)


if __name__ == "__main__":
    unittest.main()
