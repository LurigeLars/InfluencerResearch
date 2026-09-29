from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            mock.patch.object(sync, "_ensure_fallback_server", return_value={"base_url": "http://camofox"}),
            mock.patch.object(
                sync,
                "_fallback_request_json",
                side_effect=[transient, {"tabId": "ok"}],
            ) as request,
            mock.patch.object(sync.time, "sleep") as sleep,
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
            mock.patch.object(sync, "_ensure_fallback_server", return_value={"base_url": "http://camofox"}),
            mock.patch.object(sync, "_fallback_request_json", side_effect=failure) as request,
        ):
            with self.assertRaises(sync.CamoFoxHttpError):
                sync.request_json("POST", "/tabs", {"userId": "u"}, timeout=30)

        self.assertEqual(request.call_count, 1)

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
                mock.patch.object(sync, "request_json") as request,
                mock.patch.object(sync, "collect_video_urls") as collect,
                mock.patch.object(sync, "download_one", return_value={
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
                mock.patch.object(sync, "request_json", side_effect=fake_request),
                mock.patch.object(sync, "collect_video_urls", side_effect=RuntimeError("boom")),
                mock.patch.object(sync.time, "sleep"),
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
