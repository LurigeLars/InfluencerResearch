from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creator_registry import get_creator, register_creator


def base_request() -> dict:
    return {
        "creator_key": "runtimecreator",
        "display_name": "Runtime Creator",
        "sources": [
            {
                "platform": "TIKTOK",
                "profile_url": "https://www.tiktok.com/@runtimecreator",
                "evaluation_enabled": True,
                "monitoring_enabled": True,
                "priority": 10,
            },
            {
                "platform": "YOUTUBE",
                "profile_url": "https://www.youtube.com/@RuntimeCreator",
                "evaluation_enabled": True,
                "monitoring_enabled": False,
                "priority": 20,
            },
        ],
        "verification_methods": ["EXISTING_ACCEPTED_SOURCE_CONFIG"],
        "verification_refs": ["https://www.tiktok.com/@runtimecreator"],
        "request_id": "runtime-metadata-test",
        "issued_by": "unit-test",
    }


class CreatorRegistryRuntimeMetadataTests(unittest.TestCase):
    def test_existing_creator_can_be_safely_enriched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = register_creator(root, base_request())
            self.assertEqual(first["result"], "REGISTERED")

            enriched_request = base_request()
            enriched_request["sources"][0].update(
                {
                    "discovery_step": 200,
                    "max_catalog": 2500,
                    "discovery_seed_video_urls": [
                        "https://www.tiktok.com/@runtimecreator/video/7642333606219762957"
                    ],
                    "discovery_seed_basis": "EXISTING_ACCEPTED_SOURCE_CONFIG",
                }
            )
            enriched_request["sources"][1].update(
                {
                    "evaluation_video_ids": ["7ZH2isWsdjc", "12DtB9Rxr-g"],
                    "required_attribution_term": "Runtime Creator",
                    "shared_channel": True,
                }
            )

            enriched = register_creator(root, enriched_request)
            self.assertEqual(enriched["result"], "ENRICHED")

            profile = get_creator(root, "runtimecreator")
            tiktok = next(x for x in profile["sources"] if x["platform"] == "TIKTOK")
            youtube = next(x for x in profile["sources"] if x["platform"] == "YOUTUBE")
            self.assertEqual(tiktok["discovery_step"], 200)
            self.assertEqual(tiktok["max_catalog"], 2500)
            self.assertEqual(
                tiktok["discovery_seed_video_urls"],
                ["https://www.tiktok.com/@runtimecreator/video/7642333606219762957"],
            )
            self.assertEqual(youtube["evaluation_video_ids"], ["7ZH2isWsdjc", "12DtB9Rxr-g"])
            self.assertEqual(youtube["required_attribution_term"], "Runtime Creator")
            self.assertTrue(youtube["shared_channel"])

            again = register_creator(root, enriched_request)
            self.assertEqual(again["result"], "ALREADY_REGISTERED")

    def test_runtime_metadata_cannot_overwrite_existing_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = base_request()
            request["sources"][0]["discovery_step"] = 200
            register_creator(root, request)

            conflicting = base_request()
            conflicting["sources"][0]["discovery_step"] = 50
            with self.assertRaisesRegex(ValueError, "RUNTIME_METADATA_CONFLICT:TIKTOK:discovery_step"):
                register_creator(root, conflicting)

    def test_shared_channel_requires_attribution_term(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = base_request()
            request["sources"][1]["shared_channel"] = True
            with self.assertRaisesRegex(ValueError, "SHARED_CHANNEL_ATTRIBUTION_RULE_MISSING"):
                register_creator(root, request)

    def test_platform_specific_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = base_request()
            request["sources"][0]["evaluation_video_ids"] = ["7ZH2isWsdjc"]
            with self.assertRaisesRegex(ValueError, "YOUTUBE_RUNTIME_METADATA_ON_NON_YOUTUBE_SOURCE"):
                register_creator(root, request)


if __name__ == "__main__":
    unittest.main()
