from __future__ import annotations

import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
LEGACY_BATS = {
    "01_install.bat",
    "02_authenticate.bat",
    "03_run_once.bat",
    "04_build_research_queue.bat",
    "04_run_without_transcription.bat",
    "05_apply_research_decisions.bat",
    "06_scan_stories.bat",
    "07_crown_macro_once.bat",
    "08_tiktok_nicholas_crown_test.bat",
    "10_camofox_tiktok_test.bat",
    "11_tiktok_sync_and_queue.bat",
    "12_install_research_bridge.bat",
    "13_uninstall_research_bridge.bat",
    "15_evaluate_creator.bat",
}


class LegacyBatCleanupTests(unittest.TestCase):
    def test_repository_contains_no_bat_files(self) -> None:
        bat_files = sorted(p.relative_to(BASE).as_posix() for p in BASE.rglob("*.bat"))
        self.assertEqual(bat_files, [])

    def test_active_text_does_not_reference_removed_bat_entrypoints(self) -> None:
        extensions = {".py", ".ps1", ".md", ".txt", ".yaml", ".yml"}
        refs: list[str] = []
        for path in BASE.rglob("*"):
            if (
                not path.is_file()
                or path.suffix.lower() not in extensions
                or path.name == Path(__file__).name
            ):
                continue
            text = path.read_text(encoding="utf-8-sig", errors="replace").lower()
            if any(name.lower() in text for name in LEGACY_BATS):
                refs.append(path.relative_to(BASE).as_posix())
        self.assertEqual(refs, [])


if __name__ == "__main__":
    unittest.main()
