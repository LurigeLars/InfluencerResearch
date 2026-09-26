from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import camofox_container as camofox_container_config


APP_VERSION = "0.8.0"
USER_ID = "instagramresearch-tiktok"
SESSION_KEY = "nicholas-crown-smoke"
PROFILE_URL = "https://www.tiktok.com/@nicholas_crown"
VIDEO_RE = re.compile(r'https?://(?:www\.)?tiktok\.com/@nicholas_crown/video/\d+')
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def request_json(method: str, path: str, body: dict | None = None, timeout: int = 30) -> Any:
    cfg = camofox_container_config.load_config()
    url = str(cfg["base_url"]) + path
    payload = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {cfg['access_key']}",
    }
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, method=method, headers=headers)
    try:
        with NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype or raw[:1] in (b"{", b"["):
                return json.loads(raw.decode("utf-8", errors="replace"))
            return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {detail[:2000]}") from exc


def health() -> dict | None:
    try:
        value = request_json("GET", "/health", timeout=3)
        return value if isinstance(value, dict) else {"raw": value}
    except Exception:
        return None


def start_server() -> tuple[None, str]:
    if health():
        return None, "docker_container"
    raise RuntimeError(
        "Internal Camofox service is not healthy. Start the complete Compose runtime "
        "with scripts\\runtime.ps1 -Action Up."
    )


def flatten_links(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, str):
        out.extend(VIDEO_RE.findall(obj.replace("\\/", "/")))
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(flatten_links(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(flatten_links(v))
    return out


def collect_video_urls(tab_id: str, target: int = 10) -> tuple[list[str], list[dict]]:
    found: list[str] = []
    diagnostics: list[dict] = []

    for round_idx in range(10):
        snap = request_json(
            "GET",
            f"/tabs/{urllib.parse.quote(tab_id)}/snapshot?"
            + urllib.parse.urlencode({"userId": USER_ID, "format": "text"}),
            timeout=30,
        )

        urls = flatten_links(snap)

        links_result = None
        try:
            links_result = request_json(
                "GET",
                f"/tabs/{urllib.parse.quote(tab_id)}/links?"
                + urllib.parse.urlencode({"userId": USER_ID, "limit": 100}),
                timeout=20,
            )
            urls.extend(flatten_links(links_result))
        except Exception as exc:
            diagnostics.append({"round": round_idx, "links_endpoint_error": str(exc)[:1000]})

        for url in urls:
            url = url.split("?")[0]
            if url not in found:
                found.append(url)

        snap_text = json.dumps(snap, ensure_ascii=False) if not isinstance(snap, str) else snap
        diagnostics.append({
            "round": round_idx,
            "found_total": len(found),
            "snapshot_url_hint": (
                snap.get("url") if isinstance(snap, dict) else None
            ),
            "snapshot_excerpt": snap_text[:1500],
        })

        if len(found) >= target:
            break

        request_json(
            "POST",
            f"/tabs/{urllib.parse.quote(tab_id)}/scroll",
            {"userId": USER_ID, "direction": "down", "amount": 1100},
            timeout=20,
        )
        time.sleep(1.2)

    return found[:target], diagnostics


def test_individual_ytdlp(scratch_root: Path, urls: list[str], max_items: int = 3) -> list[dict]:
    output = scratch_root / "media"
    output.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for url in urls[:max_items]:
        video_id = url.rstrip("/").split("/")[-1]
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--ignore-config",
            "--no-progress",
            "--write-info-json",
            "--format", "b[ext=mp4]/b",
            "--output", str(output / "%(id)s.%(ext)s"),
            "--",
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        mp4 = output / f"{video_id}.mp4"
        rows.append({
            "url": url,
            "video_id": video_id,
            "returncode": result.returncode,
            "mp4_exists": mp4.exists(),
            "mp4_size": mp4.stat().st_size if mp4.exists() else 0,
            "diagnostic_tail": ((result.stderr or result.stdout or "").strip())[-2500:],
        })
    return rows


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    state_dir = root / "state" / "tiktok"
    status_path = state_dir / "camofox_smoke_status.json"
    urls_path = state_dir / "camofox_smoke_discovered_urls.json"

    started = utc_now()
    atomic_json(status_path, {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "state": "RUNNING",
        "started_at": started,
        "profile_url": PROFILE_URL,
    })

    tab_id = None
    started_proc = None
    try:
        started_proc, server_note = start_server()
        h = health()

        tab = request_json("POST", "/tabs", {
            "userId": USER_ID,
            "sessionKey": SESSION_KEY,
            "url": PROFILE_URL,
            "trace": False,
        }, timeout=60)
        if not isinstance(tab, dict) or not tab.get("tabId"):
            raise RuntimeError(f"Unexpected CamoFox create-tab response: {tab}")
        tab_id = str(tab["tabId"])
        time.sleep(4)

        urls, diagnostics = collect_video_urls(tab_id, target=10)
        atomic_json(urls_path, {
            "schema_version": 1,
            "app_version": APP_VERSION,
            "generated_at": utc_now(),
            "profile_url": PROFILE_URL,
            "urls": urls,
        })

        if urls:
            with tempfile.TemporaryDirectory(prefix="influencerresearch-camofox-smoke-") as scratch:
                individual = test_individual_ytdlp(Path(scratch), urls, max_items=3)
        else:
            individual = []

        enumeration_pass = len(urls) >= 3
        individual_successes = sum(1 for r in individual if r["returncode"] == 0 and r["mp4_exists"])

        if enumeration_pass and individual_successes:
            decision = "PASS_HYBRID"
        elif enumeration_pass:
            decision = "BROWSER_ENUMERATION_PASS_DOWNLOAD_FAIL"
        else:
            decision = "BROWSER_ENUMERATION_FAIL"

        status = {
            "schema_version": 1,
            "app_version": APP_VERSION,
            "state": "DONE",
            "started_at": started,
            "finished_at": utc_now(),
            "profile_url": PROFILE_URL,
            "camofox": {
                "server": server_note,
                "health": h,
                "tab_id": tab_id,
            },
            "enumeration": {
                "urls_found": len(urls),
                "urls": urls,
                "pass": enumeration_pass,
                "diagnostics": diagnostics,
            },
            "individual_ytdlp": {
                "tested": len(individual),
                "successes": individual_successes,
                "results": individual,
            },
            "decision": decision,
        }
        atomic_json(status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if decision == "PASS_HYBRID" else 1

    except Exception as exc:
        status = {
            "schema_version": 1,
            "app_version": APP_VERSION,
            "state": "ERROR",
            "started_at": started,
            "finished_at": utc_now(),
            "profile_url": PROFILE_URL,
            "error": f"{type(exc).__name__}: {exc}",
        }
        atomic_json(status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    finally:
        if tab_id:
            with contextlib.suppress(Exception):
                request_json(
                    "DELETE",
                    f"/tabs/{urllib.parse.quote(tab_id)}?"
                    + urllib.parse.urlencode({"userId": USER_ID}),
                    timeout=10,
                )
        with contextlib.suppress(Exception):
            request_json(
                "DELETE",
                f"/sessions/{urllib.parse.quote(USER_ID)}/storage_state",
                timeout=10,
            )


if __name__ == "__main__":
    raise SystemExit(main())
