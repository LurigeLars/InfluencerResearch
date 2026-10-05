from __future__ import annotations

import time
from typing import Any


def _visible_candidates(page) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for selector, kind in (("video", "video"), ("img", "image")):
        locator = page.locator(selector)
        count = min(locator.count(), 24)
        for index in range(count):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue
                box = item.bounding_box()
                if not box or float(box.get("width") or 0) < 180 or float(box.get("height") or 0) < 180:
                    continue
                if kind == "video":
                    state = item.evaluate(
                        "el => ({url: el.currentSrc || el.src || el.poster || null, "
                        "readyState: Number(el.readyState || 0), "
                        "width: Number(el.videoWidth || 0), "
                        "height: Number(el.videoHeight || 0)})"
                    )
                    ready = (
                        bool(state.get("url"))
                        and int(state.get("readyState") or 0) >= 2
                        and int(state.get("width") or 0) >= 180
                        and int(state.get("height") or 0) >= 180
                    )
                else:
                    state = item.evaluate(
                        "el => ({url: el.currentSrc || el.src || null, "
                        "complete: Boolean(el.complete), "
                        "width: Number(el.naturalWidth || 0), "
                        "height: Number(el.naturalHeight || 0)})"
                    )
                    ready = (
                        bool(state.get("url"))
                        and bool(state.get("complete"))
                        and int(state.get("width") or 0) >= 180
                        and int(state.get("height") or 0) >= 180
                    )
                rows.append({
                    "kind": kind,
                    "ready": ready,
                    "url": state.get("url"),
                    "area": float(box["width"]) * float(box["height"]),
                    "intrinsic_width": int(state.get("width") or 0),
                    "intrinsic_height": int(state.get("height") or 0),
                    "ready_state": state.get("readyState"),
                })
            except Exception:
                continue
    rows.sort(key=lambda row: float(row.get("area") or 0), reverse=True)
    return rows


def wait_for_story_media_ready(
    page,
    *,
    max_wait_ms: int = 4000,
    poll_ms: int = 100,
) -> dict[str, Any]:
    """Wait for decoded Story media before a screenshot is persisted."""
    started = time.perf_counter()
    attempts = 0
    last_media: dict[str, Any] | None = None
    max_wait_ms = max(0, int(max_wait_ms))
    poll_ms = max(25, int(poll_ms))

    while True:
        attempts += 1
        current_url = str(getattr(page, "url", "") or "")
        if "/stories/" not in current_url:
            return {
                "ready": False,
                "exited": True,
                "timed_out": False,
                "attempts": attempts,
                "wait_ms": round((time.perf_counter() - started) * 1000, 1),
                "media": last_media,
            }

        rows = _visible_candidates(page)
        last_media = rows[0] if rows else None
        if last_media and bool(last_media.get("ready")):
            return {
                "ready": True,
                "exited": False,
                "timed_out": False,
                "attempts": attempts,
                "wait_ms": round((time.perf_counter() - started) * 1000, 1),
                "media": last_media,
            }

        elapsed_ms = (time.perf_counter() - started) * 1000
        remaining_ms = max_wait_ms - elapsed_ms
        if remaining_ms <= 0:
            return {
                "ready": False,
                "exited": False,
                "timed_out": True,
                "attempts": attempts,
                "wait_ms": round((time.perf_counter() - started) * 1000, 1),
                "media": last_media,
            }

        page.wait_for_timeout(min(poll_ms, max(1, int(remaining_ms))))
