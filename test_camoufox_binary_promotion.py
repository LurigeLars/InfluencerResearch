"""Offline tests: only exact reviewed Camoufox browser digests can enter IR."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import propose_camoufox_binary_update as promote

OLD_AMD = "a" * 64
NEW_AMD = "c" * 64
NEW_ARM = "d" * 64
SOURCE = "e" * 40


def fork_source(amd=NEW_AMD):
    return "\n".join((
        "ARG CAMOUFOX_VERSION=158.0.1",
        "ARG CAMOUFOX_RELEASE=beta.41",
        "ARG CAMOUFOX_SHA256_AMD64=" + amd,
        "ARG CAMOUFOX_SHA256_ARM64=" + NEW_ARM,
    ))


def official():
    return {
        "tag_name": "v158.0.1-beta.41",
        "draft": False,
        "assets": [
            {"name": "camoufox-158.0.1-beta.41-lin.x86_64.zip", "digest": "sha256:" + NEW_AMD},
            {"name": "camoufox-158.0.1-beta.41-lin.arm64.zip", "digest": "sha256:" + NEW_ARM},
        ],
    }


class ApprovedBrowserUpdateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.docker = root / "Dockerfile"
        self.test = root / "test_runtime.py"
        self.readme = root / "README.md"
        self.docker.write_text("\n".join((
            "FROM node:26.13.0-trixie-slim",
            "ARG CAMOUFOX_VERSION=152.0.4",
            "ARG CAMOUFOX_RELEASE=beta.30",
            "ARG CAMOUFOX_SHA256=" + OLD_AMD,
        )))
        self.test.write_text("\n".join((
            'self.assertIn("CAMOUFOX_VERSION=152.0.4", text)',
            'self.assertIn("CAMOUFOX_RELEASE=beta.30", text)',
            f'self.assertIn("CAMOUFOX_SHA256={OLD_AMD}", text)',
        )))
        self.readme.write_text("- Camoufox browser baseline: " + chr(96) + "152.0.4" + chr(96) + " / " + chr(96) + "beta.30" + chr(96))
        p = patch.multiple(promote, DOCKERFILE=self.docker, TEST=self.test, README=self.readme)
        p.start()
        self.addCleanup(p.stop)

    def downloader(self, tag="158.0.1-beta.41"):
        return f"export const BUNDLED_CAMOUFOX_RELEASE = '{tag}';"

    def test_promotes_only_official_digest_and_all_independent_ir_pins(self):
        self.assertTrue(promote.propose(fork_source(), self.downloader(), official(), SOURCE))
        self.assertIn(f"ARG CAMOUFOX_SHA256={NEW_AMD}", self.docker.read_text())
        self.assertIn("CAMOUFOX_VERSION=158.0.1", self.test.read_text())
        self.assertIn("158.0.1", self.readme.read_text())
        self.assertFalse(promote.propose(fork_source(), self.downloader(), official(), SOURCE))

    def test_bad_digest_or_downloader_rejected_without_changes(self):
        old = self.docker.read_text()
        for source, downloader, meta in [
            (fork_source(amd="0" * 64), self.downloader(), official()),
            (fork_source(), self.downloader("152.0.4-beta.30"), official()),
            (fork_source(), self.downloader(), {**official(), "tag_name": "v158.0.2-beta.41"}),
            (fork_source(), self.downloader(), {**official(), "draft": True}),
        ]:
            with self.subTest(source=source, downloader=downloader):
                with self.assertRaises(ValueError):
                    promote.propose(source, downloader, meta, SOURCE)
                self.assertEqual(self.docker.read_text(), old)

    def test_missing_existing_assertion_prevents_partial_write(self):
        old = self.docker.read_text()
        self.test.write_text("unexpected test code")
        with self.assertRaises(ValueError):
            promote.propose(fork_source(), self.downloader(), official(), SOURCE)
        self.assertEqual(self.docker.read_text(), old)

    def test_rejects_floating_source_refs(self):
        with self.assertRaises(ValueError):
            promote.propose(fork_source(), self.downloader(), official(), "master")


if __name__ == "__main__":
    unittest.main()
