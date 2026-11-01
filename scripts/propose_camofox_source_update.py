#!/usr/bin/env python3
"""Prepare a review-only IR update from an immutable approved Camofox fork commit.

This script never merges, builds, deploys or touches a running browser.
The GitHub Actions workflow owns network lookups, git and PR creation.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "runtime" / "camofox" / "package.json"
LOCK = ROOT / "runtime" / "camofox" / "package-lock.json"
SYNC = ROOT / "tiktok_camofox_sync.py"
TEST = ROOT / "test_camofox_container_runtime.py"
README = ROOT / "runtime" / "camofox" / "README.md"
ARCHIVE_PATTERN = re.compile(
    r"^https://github[.]com/LurigeLars/camofox-browser/archive/([0-9a-f]{40})[.]tar[.]gz$"
)


def _replace_pinned_sha(path: Path, old: str, new: str) -> None:
    data = path.read_text(encoding="utf-8")
    if old not in data:
        raise ValueError(f"Reviewed pin is not present: {path.relative_to(ROOT)}")
    path.write_text(data.replace(old, new), encoding="utf-8")


def update_pin(new: str) -> bool:
    if not re.fullmatch("[0-9a-f]{40}", new):
        raise ValueError("Upstream pin must be a full, lowercase SHA-1 commit id")
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    old_spec = package["dependencies"]["@askjo/camofox-browser"]
    match = ARCHIVE_PATTERN.fullmatch(old_spec)
    if not match:
        raise ValueError("Camofox dependency is not an immutable approved fork archive")
    previous = match.group(1)
    if previous == new:
        return False

    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if lock["packages"][""]["dependencies"]["@askjo/camofox-browser"] != old_spec:
        raise ValueError("Package manifest and lock root are already out of sync")
    if lock["packages"]["node_modules/@askjo/camofox-browser"]["resolved"] != old_spec:
        raise ValueError("Nested lock entry does not match reviewed source")

    # Validate EVERY expected occurrence before writing any file.
    for path in (SYNC, TEST, README):
        if previous not in path.read_text(encoding="utf-8"):
            raise ValueError(f"Cannot locate old pin in {path.relative_to(ROOT)}")

    package["dependencies"]["@askjo/camofox-browser"] = old_spec.replace(previous, new)
    PACKAGE.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
    for path in (SYNC, TEST, README):
        _replace_pinned_sha(path, previous, new)
    # npm install --package-lock-only --ignore-scripts --no-audit --no-fund
    # runs after this script. Do NOT manually mutate nested package data:
    # npm must resolve and integrity-check the transitive dependency graph.
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-sha", required=True)
    args = parser.parse_args()
    changed = update_pin(args.candidate_sha)
    print("changed" if changed else "current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
