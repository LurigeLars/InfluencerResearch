from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from manifest_store import load_manifest, save_manifest


class ManifestStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state" / "manifest.json"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"schema_version": 1, "items": {
            "a": {"creator": "x", "transcription_status": "NOT_STARTED"},
        }}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def on_disk(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_worker_and_mcp_changes_both_survive(self) -> None:
        # The worker loads early and works for minutes; the MCP server records a decision meanwhile.
        worker = load_manifest(self.path)
        mcp = load_manifest(self.path)
        mcp["items"]["a"]["research_decision"] = "RESEARCH"
        save_manifest(self.path, mcp)

        worker["items"]["a"]["transcription_status"] = "DONE"
        worker["items"]["b"] = {"creator": "y"}
        save_manifest(self.path, worker)

        items = self.on_disk()["items"]
        self.assertEqual(items["a"], {"creator": "x", "transcription_status": "DONE",
                                      "research_decision": "RESEARCH"})
        self.assertEqual(items["b"], {"creator": "y"})
        # The worker's in-memory copy now also sees the MCP decision.
        self.assertEqual(worker["items"]["a"]["research_decision"], "RESEARCH")

    def test_held_item_references_stay_live_across_saves(self) -> None:
        manifest = load_manifest(self.path)
        items, item = manifest["items"], manifest["items"]["a"]
        save_manifest(self.path, manifest)
        item["transcription_status"] = "DONE"
        save_manifest(self.path, manifest)
        self.assertIs(manifest["items"], items)
        self.assertEqual(self.on_disk()["items"]["a"]["transcription_status"], "DONE")

    def test_removed_fields_and_items_are_removed(self) -> None:
        manifest = load_manifest(self.path)
        del manifest["items"]["a"]["transcription_status"]
        save_manifest(self.path, manifest)
        self.assertNotIn("transcription_status", self.on_disk()["items"]["a"])
        del manifest["items"]["a"]
        save_manifest(self.path, manifest)
        self.assertEqual(self.on_disk()["items"], {})

    def test_a_plain_dict_save_never_drops_items_it_did_not_load(self) -> None:
        save_manifest(self.path, {"schema_version": 1, "items": {"b": {"creator": "y"}}})
        self.assertEqual(sorted(self.on_disk()["items"]), ["a", "b"])

    def test_a_plain_dict_keeps_its_own_items_in_memory_after_saving(self) -> None:
        # Regression: merged items once aliased the caller's dicts and were cleared on refresh.
        item = {"creator": "y", "transcription_status": "DONE"}
        manifest = {"schema_version": 1, "items": {"b": item}}
        save_manifest(self.path, manifest)
        self.assertEqual(item, {"creator": "y", "transcription_status": "DONE"})
        save_manifest(self.path, manifest)
        self.assertEqual(self.on_disk()["items"]["b"], {"creator": "y", "transcription_status": "DONE"})

    def test_parallel_writers_keep_every_item(self) -> None:
        barrier = threading.Barrier(8)

        def writer(n: int) -> None:
            manifest = load_manifest(self.path)
            barrier.wait()  # everyone has read before anyone writes
            manifest["items"][f"item-{n}"] = {"creator": str(n)}
            save_manifest(self.path, manifest)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sorted(self.on_disk()["items"]), ["a", *sorted(f"item-{n}" for n in range(8))])


if __name__ == "__main__":
    unittest.main()
