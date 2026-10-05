from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tiktok_camofox_sync as sync


class PublicProxyFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        with sync._CAMOFOX_PROXY_ROUTE_LOCK:
            sync._CAMOFOX_PROXY_USERS.clear()
        sync._CAMOFOX_PUBLIC_PROXY_SERVER = None

    def tearDown(self) -> None:
        with sync._CAMOFOX_PROXY_ROUTE_LOCK:
            sync._CAMOFOX_PROXY_USERS.clear()
        sync._CAMOFOX_PUBLIC_PROXY_SERVER = None

    def _metric_patch(self, root: Path):
        return unittest.mock.patch.object(
            sync,
            "_public_proxy_metrics_path",
            return_value=root / "proxy-metrics.json",
        )

    def test_direct_success_never_touches_proxy(self) -> None:
        direct = {"base_url": "http://direct"}
        with (
            tempfile.TemporaryDirectory() as td,
            self._metric_patch(Path(td)),
            unittest.mock.patch.object(sync, "_ensure_fallback_server", return_value=direct),
            unittest.mock.patch.object(sync, "_ensure_public_proxy_server") as ensure_proxy,
            unittest.mock.patch.object(
                sync,
                "_fallback_request_json",
                return_value={
                    "tabId": "direct-tab",
                    "httpStatus": 200,
                    "navigationOk": True,
                },
            ) as request,
        ):
            result = sync.request_json(
                "POST",
                "/tabs",
                {
                    "userId": "u",
                    "sessionKey": "s",
                    "url": "https://www.tiktok.com/@creator",
                },
            )

        self.assertEqual(result["tabId"], "direct-tab")
        ensure_proxy.assert_not_called()
        self.assertEqual(request.call_count, 1)

    def test_direct_429_switches_session_to_proxy(self) -> None:
        direct = {"base_url": "http://direct"}
        proxy = {"base_url": "http://proxy"}
        calls: list[tuple[str, str, str]] = []

        def fake_request(server, method, path, body=None, **_kwargs):
            label = "proxy" if server is proxy else "direct"
            calls.append((label, method, path))
            if method == "POST" and path == "/tabs":
                if server is direct:
                    return {"tabId": "direct-tab", "httpStatus": 429, "navigationOk": False}
                return {"tabId": "proxy-tab", "httpStatus": 200, "navigationOk": True}
            if method == "GET" and "/snapshot?" in path:
                self.assertIs(server, proxy)
                return {"snapshot": "ok"}
            return {"ok": True}

        with (
            tempfile.TemporaryDirectory() as td,
            self._metric_patch(Path(td)),
            unittest.mock.patch.object(sync, "_ensure_fallback_server", return_value=direct),
            unittest.mock.patch.object(sync, "_ensure_public_proxy_server", return_value=proxy),
            unittest.mock.patch.object(sync, "_fallback_request_json", side_effect=fake_request),
        ):
            result = sync.request_json(
                "POST",
                "/tabs",
                {
                    "userId": "u",
                    "sessionKey": "s",
                    "url": "https://www.tiktok.com/@creator",
                },
            )
            follow_up = sync.request_json(
                "GET",
                "/tabs/proxy-tab/snapshot?userId=u&format=text",
            )
            metrics = sync._read_public_proxy_metrics()

        self.assertEqual(result["tabId"], "proxy-tab")
        self.assertEqual(follow_up, {"snapshot": "ok"})
        self.assertIn(("direct", "DELETE", "/sessions/u"), calls)
        self.assertEqual(metrics["direct_blocked"], 1)
        self.assertEqual(metrics["proxy_attempts"], 1)
        self.assertEqual(metrics["proxy_success"], 1)
        self.assertEqual(metrics["proxy_exhausted"], 0)

    def test_non_allowlisted_429_stays_direct(self) -> None:
        direct = {"base_url": "http://direct"}
        with (
            tempfile.TemporaryDirectory() as td,
            self._metric_patch(Path(td)),
            unittest.mock.patch.object(sync, "_ensure_fallback_server", return_value=direct),
            unittest.mock.patch.object(sync, "_ensure_public_proxy_server") as ensure_proxy,
            unittest.mock.patch.object(
                sync,
                "_fallback_request_json",
                return_value={"tabId": "direct-tab", "httpStatus": 429, "navigationOk": False},
            ),
        ):
            result = sync.request_json(
                "POST",
                "/tabs",
                {
                    "userId": "u",
                    "sessionKey": "s",
                    "url": "https://example.com/",
                },
            )

        self.assertEqual(result["tabId"], "direct-tab")
        ensure_proxy.assert_not_called()

    def test_proxy_attempts_are_bounded_and_exhausted(self) -> None:
        direct = {"base_url": "http://direct"}
        proxy = {"base_url": "http://proxy"}
        proxy_posts = 0

        def fake_request(server, method, path, body=None, **_kwargs):
            nonlocal proxy_posts
            if method == "POST" and path == "/tabs":
                if server is direct:
                    return {"tabId": "direct-tab", "httpStatus": 403, "navigationOk": False}
                proxy_posts += 1
                return {
                    "tabId": f"proxy-tab-{proxy_posts}",
                    "httpStatus": 429,
                    "navigationOk": False,
                }
            return {"ok": True}

        with (
            tempfile.TemporaryDirectory() as td,
            self._metric_patch(Path(td)),
            unittest.mock.patch.dict(
                sync.os.environ,
                {"INFLUENCER_RESEARCH_CAMOFOX_PROXY_ATTEMPTS": "3"},
                clear=False,
            ),
            unittest.mock.patch.object(sync, "_ensure_fallback_server", return_value=direct),
            unittest.mock.patch.object(sync, "_ensure_public_proxy_server", return_value=proxy),
            unittest.mock.patch.object(sync, "_fallback_request_json", side_effect=fake_request),
        ):
            result = sync.request_json(
                "POST",
                "/tabs",
                {
                    "userId": "u",
                    "sessionKey": "s",
                    "url": "https://www.instagram.com/creator/",
                },
            )
            metrics = sync._read_public_proxy_metrics()

        self.assertEqual(proxy_posts, 3)
        self.assertEqual(result["tabId"], "proxy-tab-3")
        self.assertEqual(metrics["proxy_attempts"], 3)
        self.assertEqual(metrics["proxy_exhausted"], 1)
        self.assertEqual(metrics["proxy_success"], 0)

    def test_proxy_unavailable_preserves_direct_blocked_session(self) -> None:
        direct = {"base_url": "http://direct"}
        calls: list[tuple[str, str]] = []

        def fake_request(_server, method, path, body=None, **_kwargs):
            calls.append((method, path))
            return {"tabId": "direct-tab", "httpStatus": 429, "navigationOk": False}

        with (
            tempfile.TemporaryDirectory() as td,
            self._metric_patch(Path(td)),
            unittest.mock.patch.object(sync, "_ensure_fallback_server", return_value=direct),
            unittest.mock.patch.object(
                sync,
                "_ensure_public_proxy_server",
                side_effect=RuntimeError("proxy down"),
            ),
            unittest.mock.patch.object(sync, "_fallback_request_json", side_effect=fake_request),
        ):
            result = sync.request_json(
                "POST",
                "/tabs",
                {
                    "userId": "u",
                    "sessionKey": "s",
                    "url": "https://www.tiktok.com/@creator",
                },
            )
            metrics = sync._read_public_proxy_metrics()

        self.assertEqual(result["tabId"], "direct-tab")
        self.assertEqual(calls, [("POST", "/tabs")])
        self.assertEqual(metrics["proxy_unavailable"], 1)

    def test_proxy_session_delete_clears_sticky_route(self) -> None:
        direct = {"base_url": "http://direct"}
        proxy = {"base_url": "http://proxy"}
        sync._set_proxy_user("u", True)

        with (
            unittest.mock.patch.object(sync, "_ensure_fallback_server", return_value=direct) as ensure_direct,
            unittest.mock.patch.object(sync, "_ensure_public_proxy_server", return_value=proxy),
            unittest.mock.patch.object(sync, "_fallback_request_json", return_value={"ok": True}) as request,
        ):
            result = sync.request_json("DELETE", "/sessions/u")

        self.assertEqual(result, {"ok": True})
        self.assertFalse(sync._proxy_user_enabled("u"))
        ensure_direct.assert_not_called()
        self.assertIs(request.call_args.args[0], proxy)

    def test_metrics_persist_only_aggregate_numeric_counters(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self._metric_patch(Path(td)):
                sync._increment_public_proxy_metric("direct_blocked")
                sync._increment_public_proxy_metric("proxy_attempts", 2)
                path = sync._public_proxy_metrics_path()
                raw = path.read_text(encoding="utf-8")
                parsed = json.loads(raw)

        self.assertEqual(set(parsed), set(sync._CAMOFOX_PROXY_METRIC_KEYS))
        self.assertTrue(all(isinstance(value, int) for value in parsed.values()))
        for forbidden in (
            "tiktok.com",
            "instagram.com",
            "http://",
            "https://",
            "username",
            "password",
            "cookie",
            "authorization",
        ):
            self.assertNotIn(forbidden, raw.casefold())


if __name__ == "__main__":
    unittest.main()
