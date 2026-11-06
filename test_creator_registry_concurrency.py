from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import creator_registry as cr


def request(key: str) -> dict:
    return {
        "creator_key": key,
        "display_name": key.title(),
        "sources": [
            {
                "platform": "TIKTOK",
                "profile_url": f"https://www.tiktok.com/@{key}",
                "evaluation_enabled": True,
                "monitoring_enabled": True,
                "priority": 10,
            }
        ],
        "verification_methods": ["EXISTING_ACCEPTED_SOURCE_CONFIG"],
        "verification_refs": [f"https://www.tiktok.com/@{key}"],
        "request_id": f"register-{key}",
        "issued_by": "unit-test",
    }


class RegistryConcurrencyTests(unittest.TestCase):
    def test_parallel_registrations_both_survive(self) -> None:
        # The MCP server runs sync tools in parallel threads. Force the interleaving where
        # both threads read the registry before either writes it.
        barrier = threading.Barrier(2, timeout=0.5)
        real_load = cr.load_registry

        def load_then_wait(root):
            data = real_load(root)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass  # Serialized by the registry lock: the other thread never arrives.
            return data

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results: dict[str, str] = {}

            def register(key: str) -> None:
                results[key] = cr.register_creator(root, request(key))["result"]

            with mock.patch.object(cr, "load_registry", load_then_wait):
                threads = [threading.Thread(target=register, args=(k,)) for k in ("alpha", "bravo")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertEqual(set(results), {"alpha", "bravo"})
            self.assertEqual(sorted(real_load(root)["creators"]), ["alpha", "bravo"])


if __name__ == "__main__":
    unittest.main()
