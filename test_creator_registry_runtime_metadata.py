from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creator_registry import get_creator, load_registry, register_creator


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


    def test_existing_creator_can_add_verified_instagram_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = {
                "creator_key": "nicholascrown",
                "display_name": "Nicholas Crown",
                "sources": [
                    {
                        "platform": "TIKTOK",
                        "profile_url": "https://www.tiktok.com/@nicholas_crown",
                        "evaluation_enabled": True,
                        "monitoring_enabled": True,
                        "priority": 10,
                    }
                ],
                "verification_methods": ["EXISTING_ACCEPTED_SOURCE_CONFIG"],
                "verification_refs": ["https://www.tiktok.com/@nicholas_crown"],
                "request_id": "initial",
                "issued_by": "unit-test",
            }
            self.assertEqual(register_creator(root, initial)["result"], "REGISTERED")

            extended = {
                "creator_key": "nicholascrown",
                "display_name": "Nicholas Crown",
                "sources": [
                    {
                        "platform": "TIKTOK",
                        "profile_url": "https://www.tiktok.com/@nicholas_crown",
                        "evaluation_enabled": True,
                        "monitoring_enabled": True,
                        "priority": 10,
                    },
                    {
                        "platform": "INSTAGRAM",
                        "profile_url": "https://www.instagram.com/nicholascrown/",
                        "evaluation_enabled": False,
                        "monitoring_enabled": False,
                        "priority": 50,
                    },
                ],
                "verification_methods": [
                    "HANDLE_BRANDING_BIO_CROSSCHECK",
                    "MULTI_SOURCE_CORROBORATION",
                ],
                "verification_refs": [
                    "https://www.tiktok.com/@nicholas_crown",
                    "https://www.instagram.com/nicholascrown/",
                    "https://www.nicholascrown.com/",
                ],
                "request_id": "extend",
                "issued_by": "unit-test",
            }
            result = register_creator(root, extended)
            self.assertEqual(result["result"], "EXTENDED")
            profile = get_creator(root, "nicholascrown")
            self.assertEqual(
                {source["platform"] for source in profile["sources"]},
                {"TIKTOK", "INSTAGRAM"},
            )
            instagram = next(source for source in profile["sources"] if source["platform"] == "INSTAGRAM")
            self.assertFalse(instagram["monitoring_enabled"])
            self.assertTrue(profile["monitoring_enabled"])
            self.assertIn("HANDLE_BRANDING_BIO_CROSSCHECK", profile["verification"]["methods"])

    def test_additive_registration_cannot_mutate_existing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = base_request()
            register_creator(root, initial)

            changed = base_request()
            changed["sources"][0]["priority"] = 999
            changed["sources"].append(
                {
                    "platform": "INSTAGRAM",
                    "profile_url": "https://www.instagram.com/runtimecreator/",
                    "evaluation_enabled": False,
                    "monitoring_enabled": False,
                    "priority": 50,
                }
            )
            with self.assertRaisesRegex(ValueError, "CREATOR_SOURCE_CONFLICT:TIKTOK:priority"):
                register_creator(root, changed)



    def test_reregistered_canonical_key_disables_exact_duplicate_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = {
                "creator_key": "nicholascrown",
                "display_name": "Nicholas Crown",
                "sources": [
                    {
                        "platform": "TIKTOK",
                        "profile_url": "https://www.tiktok.com/@nicholas_crown",
                        "evaluation_enabled": True,
                        "monitoring_enabled": True,
                        "priority": 10,
                    },
                    {
                        "platform": "INSTAGRAM",
                        "profile_url": "https://www.instagram.com/nicholascrown/",
                        "evaluation_enabled": False,
                        "monitoring_enabled": False,
                        "priority": 50,
                    },
                ],
                "verification_methods": ["HANDLE_BRANDING_BIO_CROSSCHECK"],
                "verification_refs": [
                    "https://www.tiktok.com/@nicholas_crown",
                    "https://www.instagram.com/nicholascrown/",
                ],
                "request_id": "canonical",
                "issued_by": "unit-test",
            }
            alias = {
                **canonical,
                "creator_key": "nicholascrown_ingest",
                "request_id": "alias",
            }

            self.assertEqual(register_creator(root, canonical)["result"], "REGISTERED")
            self.assertEqual(register_creator(root, alias)["result"], "REGISTERED")

            result = register_creator(root, canonical)
            self.assertEqual(result["result"], "DEDUPLICATED")
            self.assertEqual(result["disabled_duplicate_keys"], ["nicholascrown_ingest"])

            registry = load_registry(root)
            self.assertEqual(registry["creators"]["nicholascrown"]["status"], "ACTIVE")
            disabled = registry["creators"]["nicholascrown_ingest"]
            self.assertEqual(disabled["status"], "DISABLED")
            self.assertFalse(disabled["monitoring_enabled"])
            self.assertEqual(disabled["superseded_by"], "nicholascrown")



    def test_dedupe_ignores_mixed_source_verification_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            initial = {
                "creator_key": "nicholascrown",
                "display_name": "Nicholas Crown",
                "sources": [
                    {
                        "platform": "TIKTOK",
                        "profile_url": "https://www.tiktok.com/@nicholas_crown",
                        "evaluation_enabled": True,
                        "monitoring_enabled": True,
                        "priority": 10,
                        "discovery_step": 200,
                        "max_catalog": 2500,
                    }
                ],
                "verification_methods": ["EXISTING_ACCEPTED_SOURCE_CONFIG"],
                "verification_refs": ["https://www.tiktok.com/@nicholas_crown"],
                "request_id": "initial",
                "issued_by": "unit-test",
            }
            self.assertEqual(register_creator(root, initial)["result"], "REGISTERED")

            extended = {
                "creator_key": "nicholascrown",
                "display_name": "Nicholas Crown",
                "sources": [
                    {
                        "platform": "TIKTOK",
                        "profile_url": "https://www.tiktok.com/@nicholas_crown",
                        "evaluation_enabled": True,
                        "monitoring_enabled": True,
                        "priority": 10,
                        "discovery_step": 200,
                        "max_catalog": 2500,
                    },
                    {
                        "platform": "INSTAGRAM",
                        "profile_url": "https://www.instagram.com/nicholascrown/",
                        "evaluation_enabled": False,
                        "monitoring_enabled": False,
                        "priority": 50,
                    },
                ],
                "verification_methods": [
                    "HANDLE_BRANDING_BIO_CROSSCHECK",
                    "MULTI_SOURCE_CORROBORATION",
                ],
                "verification_refs": [
                    "https://www.tiktok.com/@nicholas_crown",
                    "https://www.instagram.com/nicholascrown/",
                    "https://www.nicholascrown.com/",
                ],
                "request_id": "extend",
                "issued_by": "unit-test",
            }
            self.assertEqual(register_creator(root, extended)["result"], "EXTENDED")

            alias = {
                "creator_key": "nicholascrown_ingest",
                "display_name": "Nicholas Crown",
                "sources": [
                    {
                        "platform": "TIKTOK",
                        "profile_url": "https://www.tiktok.com/@nicholas_crown",
                        "evaluation_enabled": True,
                        "monitoring_enabled": False,
                        "priority": 10,
                        "discovery_step": 200,
                        "max_catalog": 2500,
                    },
                    {
                        "platform": "INSTAGRAM",
                        "profile_url": "https://www.instagram.com/nicholascrown/",
                        "evaluation_enabled": False,
                        "monitoring_enabled": False,
                        "priority": 50,
                    },
                ],
                "verification_methods": ["HANDLE_BRANDING_BIO_CROSSCHECK"],
                "verification_refs": [
                    "https://www.instagram.com/nicholascrown/",
                    "https://www.tiktok.com/@nicholas_crown",
                    "https://www.nicholascrown.com/",
                ],
                "request_id": "alias",
                "issued_by": "unit-test",
            }
            self.assertEqual(register_creator(root, alias)["result"], "REGISTERED")

            reassert = {
                "creator_key": "nicholascrown",
                "display_name": "Nicholas Crown",
                "sources": extended["sources"],
                "verification_methods": [
                    "EXISTING_ACCEPTED_SOURCE_CONFIG",
                    "HANDLE_BRANDING_BIO_CROSSCHECK",
                    "MULTI_SOURCE_CORROBORATION",
                ],
                "verification_refs": [
                    "https://www.tiktok.com/@nicholas_crown",
                    "https://www.instagram.com/nicholascrown/",
                    "https://www.nicholascrown.com/",
                ],
                "request_id": "reassert",
                "issued_by": "unit-test",
            }
            result = register_creator(root, reassert)
            self.assertEqual(result["result"], "DEDUPLICATED")
            self.assertEqual(result["disabled_duplicate_keys"], ["nicholascrown_ingest"])

            registry = load_registry(root)
            canonical = registry["creators"]["nicholascrown"]
            self.assertEqual(canonical["status"], "ACTIVE")
            tiktok = next(source for source in canonical["sources"] if source["platform"] == "TIKTOK")
            instagram = next(source for source in canonical["sources"] if source["platform"] == "INSTAGRAM")
            self.assertEqual(tiktok["verification_basis"], "EXISTING_ACCEPTED_SOURCE_CONFIG")
            self.assertEqual(
                instagram["verification_basis"],
                "HANDLE_BRANDING_BIO_CROSSCHECK+MULTI_SOURCE_CORROBORATION",
            )
            disabled = registry["creators"]["nicholascrown_ingest"]
            self.assertEqual(disabled["status"], "DISABLED")
            self.assertEqual(disabled["superseded_by"], "nicholascrown")



if __name__ == "__main__":
    unittest.main()
