from __future__ import annotations

import unittest
from unittest.mock import patch

import instagram_camofox_public_smoke as smoke


class InstagramPublicCamofoxSmokeTests(unittest.TestCase):
    def test_extracts_absolute_and_relative_reels(self) -> None:
        payload = {
            "a": "https://www.instagram.com/reel/ABC_123/?x=1",
            "b": ["href=/reel/XYZ-789/", '"/reel/QWE_456/"'],
        }
        self.assertEqual(
            smoke.extract_reel_urls(payload),
            [
                "https://www.instagram.com/reel/ABC_123/",
                "https://www.instagram.com/reel/XYZ-789/",
                "https://www.instagram.com/reel/QWE_456/",
            ],
        )

    def test_classifies_public_profile_without_login_block(self) -> None:
        payload = {
            "text": "RikaTillsammans public profile",
            "links": ["https://www.instagram.com/reel/ABC_123/"],
        }
        result = smoke.classify_snapshot(payload, "rikatillsammans")
        self.assertTrue(result["handle_visible"])
        self.assertEqual(result["reel_count"], 1)
        self.assertEqual(result["block_hits"], [])

    def test_auth_prompt_is_not_a_hard_block(self) -> None:
        result = smoke.classify_snapshot(
            {"text": "RikaTillsammans. Log in or Sign up to continue."},
            "rikatillsammans",
        )
        self.assertEqual(result["block_hits"], [])
        self.assertIn("log in", result["auth_prompt_hits"])
        self.assertIn("sign up", result["auth_prompt_hits"])

    def test_detects_challenge_as_hard_block(self) -> None:
        result = smoke.classify_snapshot(
            {"text": "RikaTillsammans. Challenge required."},
            "rikatillsammans",
        )
        self.assertIn("challenge", result["block_hits"])

    def test_detects_language_dialog_separately(self) -> None:
        result = smoke.classify_snapshot(
            {"snapshot": 'RikaTillsammans - combobox "Switch Display Language"'},
            "rikatillsammans",
        )
        self.assertTrue(result["language_dialog_visible"])
        self.assertEqual(result["block_hits"], [])

    def test_detects_swedish_language_dialog(self) -> None:
        result = smoke.classify_snapshot(
            {"snapshot": '- dialog:\n  - combobox "Byt visningsspråk"'},
            "rikatillsammans",
        )
        self.assertTrue(result["language_dialog_visible"])
        self.assertIn("byt visningsspråk", result["language_dialog_hits"])

    def test_extracts_language_combobox_action(self) -> None:
        result = smoke.language_dialog_action(
            {
                "snapshot": (
                    '- dialog:\n'
                    '  - combobox "Byt visningsspråk" [e1]:\n'
                    '    - option "English"\n'
                    '    - option "Svenska" [selected]'
                )
            }
        )
        self.assertEqual(
            result,
            {"ref": "e1", "selected": "Svenska", "target": "English"},
        )

    def test_language_action_targets_swedish_when_english_selected(self) -> None:
        result = smoke.language_dialog_action(
            {
                "snapshot": (
                    '- dialog:\n'
                    '  - combobox "Switch Display Language" [e7]:\n'
                    '    - option "English" [selected]\n'
                    '    - option "Svenska"'
                )
            }
        )
        self.assertEqual(
            result,
            {"ref": "e7", "selected": "English", "target": "Svenska"},
        )

    def test_dom_probe_executes_expression_and_classifies_handle(self) -> None:
        fake_response = {
            "ok": True,
            "result": {
                "title": "RikaTillsammans",
                "href": "https://www.instagram.com/rikatillsammans/",
                "body_text_length": 42,
                "body_text_excerpt": "RikaTillsammans public profile",
                "reel_links": ["https://www.instagram.com/reel/ABC123/"],
                "dialogs": [],
                "selects": [],
            },
        }
        with patch.object(smoke, "request_json", return_value=fake_response) as request:
            result = smoke.dom_probe("tab-1", "user-1", "rikatillsammans")

        self.assertTrue(result["handle_visible"])
        body = request.call_args.args[2]
        self.assertTrue(body["expression"].lstrip().startswith("(() =>"))
        self.assertTrue(body["expression"].rstrip().endswith("})()"))

    def test_decline_optional_cookies_executes_dom_action(self) -> None:
        fake_response = {
            "ok": True,
            "result": {
                "clicked": True,
                "label": "Decline optional cookies",
                "available_buttons": ["Allow all cookies", "Decline optional cookies"],
            },
        }
        with patch.object(smoke, "request_json", return_value=fake_response) as request:
            result = smoke.decline_optional_cookies("tab-1", "user-1")

        self.assertTrue(result["clicked"])
        body = request.call_args.args[2]
        self.assertIn("decline optional cookies", body["expression"].lower())
        self.assertTrue(body["expression"].rstrip().endswith("})()"))

    def test_classify_story_probe_requires_auth(self) -> None:
        result = smoke.classify_story_probe(
            {
                "href": "https://www.instagram.com/accounts/login/",
                "body_text_excerpt": "Log in to Instagram",
                "login_surface": True,
                "generic_error": False,
                "story_url_active": False,
                "videos": [],
                "story_links": [],
            },
            "rikatillsammans",
        )
        self.assertEqual(result, "STORY_REQUIRES_AUTH")

    def test_classify_story_probe_public_accessible(self) -> None:
        result = smoke.classify_story_probe(
            {
                "href": "https://www.instagram.com/stories/rikatillsammans/123456789/",
                "body_text_excerpt": "RikaTillsammans",
                "login_surface": False,
                "generic_error": False,
                "story_url_active": True,
                "videos": [{"src": "https://cdn.example/story.mp4"}],
                "story_links": [],
            },
            "rikatillsammans",
        )
        self.assertEqual(result, "PUBLIC_STORY_ACCESSIBLE")

    def test_classify_story_probe_redirected_without_story(self) -> None:
        result = smoke.classify_story_probe(
            {
                "href": "https://www.instagram.com/rikatillsammans/",
                "body_text_excerpt": "RikaTillsammans",
                "login_surface": False,
                "generic_error": False,
                "story_url_active": False,
                "videos": [],
                "story_links": [],
            },
            "rikatillsammans",
        )
        self.assertEqual(result, "NO_ACTIVE_STORY_OR_REDIRECTED")

    def test_classify_story_probe_numeric_frame_url_is_public(self) -> None:
        result = smoke.classify_story_probe(
            {
                "href": "https://www.instagram.com/stories/rikatillsammans/123456789/",
                "body_text_excerpt": "RikaTillsammans",
                "login_surface": False,
                "generic_error": False,
                "story_url_active": True,
                "videos": [],
                "story_links": [],
                "view_confirmation_visible": False,
            },
            "rikatillsammans",
        )
        self.assertEqual(result, "PUBLIC_STORY_ACCESSIBLE")

    def test_classify_story_probe_root_url_without_media_is_inconclusive(self) -> None:
        result = smoke.classify_story_probe(
            {
                "href": "https://www.instagram.com/stories/rikatillsammans/",
                "body_text_excerpt": "RikaTillsammans",
                "login_surface": False,
                "generic_error": False,
                "story_url_active": True,
                "videos": [],
                "story_links": [],
                "view_confirmation_visible": False,
            },
            "rikatillsammans",
        )
        self.assertEqual(result, "STORY_PUBLIC_ACCESS_INCONCLUSIVE")

    def test_story_dom_probe_detects_swedish_teaser_auth_gate(self) -> None:
        fake_response = {
            "ok": True,
            "result": {
                "title": "Händelser • Instagram",
                "href": "https://www.instagram.com/stories/rikatillsammans/",
                "body_text_excerpt": (
                    "Se den här händelsen innan den försvinner\n"
                    "Kolla in de senaste fotona och videorna från rikatillsammans.\n"
                    "Registrera dig\nLogga in"
                ),
                "buttons": [],
                "videos": [],
                "images": [],
                "story_links": [],
                "dialogs": [],
                "cookie_consent_visible": False,
            },
        }
        with patch.object(smoke, "request_json", return_value=fake_response):
            result = smoke.story_dom_probe("tab-1", "user-1", "rikatillsammans")

        self.assertTrue(result["story_url_active"])
        self.assertTrue(result["story_teaser_auth_gate"])
        self.assertTrue(result["login_surface"])

    def test_story_teaser_gate_requires_auth(self) -> None:
        dom = {
            "href": "https://www.instagram.com/stories/rikatillsammans/",
            "body_text_excerpt": (
                "Se den här händelsen innan den försvinner\n"
                "Kolla in de senaste fotona och videorna från rikatillsammans.\n"
                "Registrera dig\nLogga in"
            ),
            "login_surface": True,
            "generic_error": False,
            "story_url_active": True,
            "videos": [],
            "story_links": [],
            "view_confirmation_visible": False,
        }
        self.assertEqual(
            smoke.classify_story_probe(dom, "rikatillsammans"),
            "STORY_REQUIRES_AUTH",
        )

    def test_dismiss_profile_media_auth_gate_executes_close_action(self) -> None:
        fake_response = {"ok": True, "result": {"clicked": True}}
        with patch.object(smoke, "request_json", return_value=fake_response) as request:
            result = smoke.dismiss_profile_media_auth_gate("tab-1", "user-1")

        self.assertTrue(result["clicked"])
        body = request.call_args.args[2]
        self.assertIn("media_auth_gate_close_not_found", body["expression"])
        self.assertIn("visa foton, videor med mera från", body["expression"].lower())

    def test_handle_visibility_ignores_requested_url_metadata(self) -> None:
        result = smoke.classify_snapshot(
            {
                "url": "https://www.instagram.com/rikatillsammans/",
                "snapshot": '- dialog:\n  - combobox "Byt visningsspråk"',
            },
            "rikatillsammans",
        )
        self.assertFalse(result["handle_visible"])


if __name__ == "__main__":
    unittest.main()
