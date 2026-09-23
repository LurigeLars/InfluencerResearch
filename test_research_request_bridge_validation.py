from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from research_request_bridge import BRIDGE_NAME, Bridge


def request(action: str, **extra) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "bridge": BRIDGE_NAME,
        "state": "PENDING",
        "request_id": f"test-{action.lower().replace('_', '-')}-0001",
        "action": action,
        "issued_by": "UNIT_TEST",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        **extra,
    }


def validator() -> Bridge:
    bridge = Bridge.__new__(Bridge)
    bridge._validate_registered_creator = (
        lambda creator_key, *, source_platform, require_monitoring: (True, "OK")
    )
    return bridge


class ResearchRequestBridgeValidationTests(unittest.TestCase):
    def test_health_request_is_valid(self) -> None:
        self.assertEqual(validator().validate_request(request("HEALTH")), (True, "HEALTH"))

    def test_unknown_command_field_is_rejected(self) -> None:
        req = request("HEALTH", command="whoami")
        ok, reason = validator().validate_request(req)
        self.assertFalse(ok)
        self.assertEqual(reason, "UNKNOWN_FIELDS:command")

    def test_arbitrary_profile_url_is_rejected_on_evaluation(self) -> None:
        req = request(
            "RUN_CREATOR_EVALUATION",
            creator_key="registered_creator",
            sample_size=1,
            profile_url="https://example.com/evil",
        )
        ok, reason = validator().validate_request(req)
        self.assertFalse(ok)
        self.assertEqual(reason, "UNKNOWN_FIELDS:profile_url")

    def test_bad_registration_host_fails_before_execution(self) -> None:
        req = request(
            "REGISTER_CREATOR",
            creator_key="acceptancebadhost",
            display_name="Acceptance Bad Host",
            verification_methods=["MULTI_SOURCE_CORROBORATION"],
            verification_refs=["https://example.com/evidence"],
            sources=[
                {
                    "platform": "TIKTOK",
                    "profile_url": "https://example.com/@badhost",
                    "evaluation_enabled": True,
                    "monitoring_enabled": False,
                    "priority": 10,
                }
            ],
        )
        ok, reason = validator().validate_request(req)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("REGISTRATION_REJECTED:"), reason)

    def test_path_like_request_id_is_rejected(self) -> None:
        req = request("HEALTH")
        req["request_id"] = "../evil"
        self.assertEqual(validator().validate_request(req), (False, "BAD_REQUEST_ID"))


if __name__ == "__main__":
    unittest.main()
