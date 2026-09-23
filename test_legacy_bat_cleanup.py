from __future__ import annotations

import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent


class LegacyBatCleanupTests(unittest.TestCase):
    def test_repository_contains_no_bat_files(self) -> None:
        bat_files = sorted(p.relative_to(BASE).as_posix() for p in BASE.rglob("*.bat"))
        self.assertEqual(bat_files, [])

    def test_active_text_does_not_reference_bat_entrypoints(self) -> None:
        extensions = {".py", ".ps1", ".md", ".txt", ".yaml", ".yml"}
        refs: list[str] = []
        for path in BASE.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            if ".bat" in text.lower():
                refs.append(path.relative_to(BASE).as_posix())
        self.assertEqual(refs, [])


if __name__ == "__main__":
    unittest.main()
