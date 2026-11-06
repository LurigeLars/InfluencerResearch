"""Shared read/write of state/manifest.json for the MCP server and the job worker.

Both processes read the whole manifest, change it in memory (sometimes for minutes, e.g.
during transcription) and write it back. A plain write would drop whatever the other process
saved in between. load_manifest() remembers what was read; save_manifest() takes a file lock,
re-reads the file and applies only this writer's own changes (fields that differ from what it
read), then writes atomically. Two writers touching the same field: the later save wins.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

LOCK_TIMEOUT_SECONDS = 60.0


class Manifest(dict):
    """A manifest dict that remembers the state it was loaded from."""

    base: dict


def _read(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return copy.deepcopy(default)


def load_manifest(path: Path, default: dict | None = None) -> Manifest:
    data = _read(path, default if default is not None else {"schema_version": 1, "items": {}})
    manifest = Manifest(data)
    manifest.base = copy.deepcopy(data)
    return manifest


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a+b") as handle:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"manifest lock busy for {LOCK_TIMEOUT_SECONDS:.0f}s: {path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _apply_changes(current: dict, base: dict, mine: dict, *, skip: str | None = None) -> dict:
    """Return `current` with the keys `mine` changed, added or removed relative to `base`."""
    out = dict(current)
    for key, value in mine.items():
        if key != skip and (key not in base or base[key] != value):
            out[key] = value
    for key in base:
        if key != skip and key not in mine:
            out.pop(key, None)
    return out


def merge(current: dict, base: dict, mine: dict) -> dict:
    """Three-way merge at item-field granularity; `current` is what is on disk now."""
    merged = _apply_changes(current, base, mine, skip="items")
    disk_items = dict(current.get("items") or {})
    base_items = base.get("items") or {}
    for key, item in (mine.get("items") or {}).items():
        old = base_items.get(key)
        if not isinstance(item, dict) or not isinstance(old, dict):
            if old != item:
                disk_items[key] = item  # new item, or replaced wholesale
        elif key in disk_items and isinstance(disk_items[key], dict):
            disk_items[key] = _apply_changes(disk_items[key], old, item)
        elif item != old:
            disk_items[key] = item  # removed by the other writer, but changed here: keep ours
    for key in base_items:
        if key not in (mine.get("items") or {}):
            disk_items.pop(key, None)
    merged["items"] = disk_items
    return merged


def _refresh_in_place(manifest: dict, merged: dict) -> None:
    # Callers hold references to manifest["items"] and to single items across saves;
    # keep those objects alive and update them instead of replacing them.
    items = manifest.get("items")
    if not isinstance(items, dict):
        items = {}
    for key in list(items):
        if key not in merged["items"]:
            del items[key]
    for key, value in merged["items"].items():
        if isinstance(items.get(key), dict) and isinstance(value, dict):
            items[key].clear()
            items[key].update(value)
        else:
            items[key] = value
    for key in list(manifest):
        if key != "items" and key not in merged:
            del manifest[key]
    for key, value in merged.items():
        if key != "items":
            manifest[key] = value
    manifest["items"] = items


def save_manifest(path: Path, manifest: dict) -> None:
    """Merge this writer's changes into the manifest on disk, under a cross-process lock."""
    base: Any = getattr(manifest, "base", None)
    if base is None:
        base = {}  # Not loaded via load_manifest: treat everything as ours, never drop others' items.
    with _locked(path):
        # Deep copy: merge() reuses the caller's item objects, which _refresh_in_place then clears.
        merged = copy.deepcopy(merge(_read(path, {"schema_version": 1, "items": {}}), base, manifest))
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    _refresh_in_place(manifest, merged)
    if isinstance(manifest, Manifest):
        manifest.base = copy.deepcopy(merged)
