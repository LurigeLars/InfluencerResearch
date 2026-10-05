from __future__ import annotations

import tempfile
import unittest.mock
from pathlib import Path

import tiktok_camofox_sync as sync


class CamoFoxRetryTests(unittest.TestCase):
    def test_tab_create_retries_only_structured_retryable_503(self) -> None:
        transient = sync.CamoFoxHttpError(
            503,
            "/tabs",
            code="admission_rejected",
            retryable=True,
            reason="concurrency_full",
            detail='{"code":"admission_rejected","retryable":true}',
        )
        with (
            unittest.mock.patch.object(sync, "_ensure_fallback_server", return_value={"base_url": "http://camofox"}),
            unittest.mock.patch.object(
                sync,
                "_fallback_request_json",
                side_effect=[transient, {"tabId": "ok"}],
            ) as request,
            unittest.mock.patch.object(sync.time, "sleep") as sleep,
        ):
            result = sync.request_json("POST", "/tabs", {"userId": "u"}, timeout=30)

        self.assertEqual(result, {"tabId": "ok"})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_non_retryable_error_is_not_retried(self) -> None:
        failure = sync.CamoFoxHttpError(
            500,
            "/tabs",
            retryable=False,
            detail='{"error":"Internal server error","retryable":false}',
        )
        with (
            unittest.mock.patch.object(sync, "_ensure_fallback_server", return_value={"base_url": "http://camofox"}),
            unittest.mock.patch.object(sync, "_fallback_request_json", side_effect=failure) as request,
        ):
            with self.assertRaises(sync.CamoFoxHttpError):
                sync.request_json("POST", "/tabs", {"userId": "u"}, timeout=30)

        self.assertEqual(request.call_count, 1)

    def test_profile_readiness_returns_without_blind_sleep_when_links_are_ready(self) -> None:
        with (
            unittest.mock.patch.object(
                sync,
                "request_json",
                return_value={
                    "result": {
                        "readyState": "complete",
                        "bodyTextLength": 1200,
                        "videoLinkCount": 3,
                        "videoLinks": [
                            "https://www.tiktok.com/@nicholas_crown/video/1",
                            "https://www.tiktok.com/@nicholas_crown/video/2",
                            "https://www.tiktok.com/@nicholas_crown/video/3",
                        ],
                        "href": "https://www.tiktok.com/@nicholas_crown",
                    }
                },
            ) as request,
            unittest.mock.patch.object(sync.time, "sleep") as sleep,
        ):
            result = sync._wait_for_tiktok_profile_ready(
                "tab-1",
                user_id="user-1",
                target=3,
            )

        self.assertTrue(result["ready"])
        self.assertTrue(result["target_reached"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(result["last_value"]["videoLinks"]), 3)
        request.assert_called_once()
        sleep.assert_not_called()

    def test_profile_readiness_waits_for_requested_link_target(self) -> None:
        responses = [
            {
                "result": {
                    "readyState": "complete",
                    "bodyTextLength": 1200,
                    "videoLinkCount": 0,
                    "videoLinks": [],
                    "href": "https://www.tiktok.com/@nicholas_crown",
                }
            },
            {
                "result": {
                    "readyState": "complete",
                    "bodyTextLength": 1200,
                    "videoLinkCount": 15,
                    "videoLinks": [
                        f"https://www.tiktok.com/@nicholas_crown/video/{i}"
                        for i in range(15)
                    ],
                    "href": "https://www.tiktok.com/@nicholas_crown",
                }
            },
        ]
        with (
            unittest.mock.patch.object(sync, "request_json", side_effect=responses) as request,
            unittest.mock.patch.object(sync.time, "sleep") as sleep,
        ):
            result = sync._wait_for_tiktok_profile_ready(
                "tab-1",
                user_id="user-1",
                target=15,
            )

        self.assertTrue(result["ready"])
        self.assertTrue(result["target_reached"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(result["last_value"]["videoLinks"]), 15)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once()

    def test_collect_video_urls_uses_readiness_seed_without_browser_rescan(self) -> None:
        initial = [
            "https://www.tiktok.com/@nicholas_crown/video/100",
            "https://www.tiktok.com/@nicholas_crown/video/101",
            "https://www.tiktok.com/@nicholas_crown/video/102",
        ]
        with unittest.mock.patch.object(sync, "request_json") as request:
            found, diag = sync.collect_video_urls(
                "tab-1",
                user_id="user-1",
                handle="nicholas_crown",
                target=3,
                initial_urls=initial,
            )

        self.assertEqual(found, initial)
        self.assertEqual(diag["rounds"], 0)
        self.assertEqual(diag["source"], "readiness_dom")
        self.assertEqual(diag["initial_url_count"], 3)
        request.assert_not_called()

    def test_collect_video_urls_skips_snapshot_when_links_endpoint_reaches_target(self) -> None:
        links = {
            "links": [
                f"https://www.tiktok.com/@nicholas_crown/video/{i}"
                for i in range(15)
            ]
        }
        calls = []

        def fake_request(method: str, path: str, body=None, timeout=30):
            calls.append((method, path))
            if "/links?" in path:
                return links
            raise AssertionError(f"unexpected request: {method} {path}")

        with unittest.mock.patch.object(sync, "request_json", side_effect=fake_request):
            found, diag = sync.collect_video_urls(
                "tab-1",
                user_id="user-1",
                handle="nicholas_crown",
                target=15,
            )

        self.assertEqual(len(found), 15)
        self.assertEqual(diag["links_calls"], 1)
        self.assertEqual(diag["snapshot_calls"], 0)
        self.assertEqual(diag["rounds"], 1)
        self.assertEqual(diag["source"], "browser_scan")
        self.assertFalse(any("/snapshot?" in path for _, path in calls))

    def test_collect_video_urls_filters_foreign_readiness_links(self) -> None:
        initial = [
            "https://www.tiktok.com/@nicholas_crown/video/100",
            "https://www.tiktok.com/@other/video/999",
            "https://example.com/video/123",
        ]
        snapshot = {
            "links": [
                "https://www.tiktok.com/@nicholas_crown/video/101",
            ]
        }
        with unittest.mock.patch.object(
            sync,
            "request_json",
            side_effect=[
                snapshot,
                {"links": []},
            ],
        ):
            found, diag = sync.collect_video_urls(
                "tab-1",
                user_id="user-1",
                handle="nicholas_crown",
                target=2,
                initial_urls=initial,
                max_scrolls=0,
            )

        self.assertEqual(
            found,
            [
                "https://www.tiktok.com/@nicholas_crown/video/100",
                "https://www.tiktok.com/@nicholas_crown/video/101",
            ],
        )
        self.assertEqual(diag["rounds"], 1)
        self.assertEqual(diag["source"], "browser_scan")

    def test_exact_catalog_ids_skip_redundant_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video_id = "7690974941457534222"
            catalog_path = root / "state" / "tiktok" / "nicholascrown_catalog.json"
            sync.atomic_json(catalog_path, {
                "schema_version": 1,
                "profile_url": "https://www.tiktok.com/@nicholas_crown",
                "order": [video_id],
                "items": {
                    video_id: {
                        "video_id": video_id,
                        "url": f"https://www.tiktok.com/@nicholas_crown/video/{video_id}",
                    }
                },
            })
            source = {
                "creator_key": "nicholascrown",
                "handle": "nicholas_crown",
                "profile_url": "https://www.tiktok.com/@nicholas_crown",
                "enabled": True,
                "discovery_step": 3,
                "max_catalog": 10,
                "max_new_downloads": 1,
            }
            with (
                unittest.mock.patch.object(sync, "request_json") as request,
                unittest.mock.patch.object(sync, "collect_video_urls") as collect,
                unittest.mock.patch.object(sync, "download_one", return_value={
                    "video_id": video_id,
                    "url": f"https://www.tiktok.com/@nicholas_crown/video/{video_id}",
                    "ok": False,
                    "diagnostic_tail": "fixture stop after discovery",
                }) as download,
            ):
                result = sync.process_source(
                    root,
                    source,
                    max_new_override=1,
                    include_video_ids={video_id},
                    discovery_target_override=3,
                )

        self.assertTrue(result["discovery_skipped_for_exact_ids"])
        self.assertEqual(result["discovery"]["source"], "existing_catalog_exact_ids")
        self.assertEqual(result["discovery"]["found"], 1)
        request.assert_not_called()
        collect.assert_not_called()
        download.assert_called_once()

    def test_creator_session_cleanup_runs_when_discovery_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls: list[tuple[str, str]] = []

            def fake_request(method: str, path: str, body=None, timeout=30):
                calls.append((method, path))
                if method == "POST" and path == "/tabs":
                    return {"tabId": "tab-1"}
                return {"ok": True}

            source = {
                "creator_key": "nicholascrown",
                "handle": "nicholas_crown",
                "profile_url": "https://www.tiktok.com/@nicholas_crown",
                "enabled": True,
                "discovery_step": 3,
                "max_catalog": 10,
                "max_new_downloads": 1,
            }
            with (
                unittest.mock.patch.object(sync, "request_json", side_effect=fake_request),
                unittest.mock.patch.object(
                    sync,
                    "_wait_for_tiktok_profile_ready",
                    return_value={
                        "ready": True,
                        "attempts": 1,
                        "wait_ms": 0.0,
                    },
                ),
                unittest.mock.patch.object(sync, "collect_video_urls", side_effect=RuntimeError("boom")),
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    sync.process_source(
                        root,
                        source,
                        max_new_override=1,
                        discovery_target_override=3,
                    )

            self.assertIn(("DELETE", "/tabs/tab-1?userId=instagramresearch-tiktok-nicholascrown"), calls)
            self.assertIn(("DELETE", "/sessions/instagramresearch-tiktok-nicholascrown"), calls)


if __name__ == "__main__":
    unittest.main()
