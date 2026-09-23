from __future__ import annotations

import ast
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent


class TikTokMediaTransportTests(unittest.TestCase):
    def test_tiktok_media_transport_is_ytdlp_only(self) -> None:
        source = (BASE / "tiktok_camofox_sync.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        download_one = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "download_one"
        )
        segment = ast.get_source_segment(source, download_one) or ""

        self.assertIn('sys.executable, "-m", "yt_dlp"', segment)
        self.assertNotIn("_download_one_via_camofox", source)
        self.assertNotIn("/downloads?", source)
        self.assertNotIn("--_b042-stage-media", source)
        self.assertNotIn("CAMOFOX_TRANSFER_DIR", source)


if __name__ == "__main__":
    unittest.main()
