from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_VERSION = "0.6.0"
BASE_URL = "http://127.0.0.1:9377"
USER_ID = "instagramresearch-tiktok"
SESSION_KEY = "nicholas-crown-poc"
PROFILE_URL = "https://www.tiktok.com/@nicholas_crown"
VIDEO_RE = re.compile(r'https?://(?:www\.)?tiktok\.com/@nicholas_crown/video/\d+')


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def request_json(method: str, path: str, body: dict | None = None, timeout: int = 30) -> Any:
    url = BASE_URL + path
    payload = None
    headers = {"Accept": "application/json"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def start_server() -> tuple[subprocess.Popen | None, str]:
    if health():
        return None, "already_running"

    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch" / "camofox-poc"
    cli = local / "node_modules" / ".bin" / "camofox-browser.cmd"
    if not cli.exists():
        raise RuntimeError(
            f"CamoFox CLI not installed at {cli}. Run scripts\\install_camofox.ps1 first."
        )

    log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch" / "camofox-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "camofox_server.log"
    log = open(log_path, "ab", buffering=0)

    env = os.environ.copy()
    env["CAMOFOX_PORT"] = "9377"
    env["CAMOFOX_CRASH_REPORT_ENABLED"] = "false"
    env["CAMOFOX_PROFILE_DIR"] = str(
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch" / "camofox-profiles"
    )

    proc = subprocess.Popen(
        [str(cli)],
        cwd=str(local),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    for _ in range(60):
        time.sleep(0.5)
        h = health()
        if h:
            return proc, str(log_path)
        if proc.poll() is not None:
            break

    raise RuntimeError(f"CamoFox server failed to become healthy. Check {log_path}")


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
        # Snapshot is the source-of-truth check that the browser reached real TikTok content.
        snap = request_json(
            "GET",
            f"/tabs/{urllib.parse.quote(tab_id)}/snapshot?"
            + urllib.parse.urlencode({"userId": USER_ID, "format": "text"}),
            timeout=30,
        )

        urls = flatten_links(snap)

        # Official agent guide also exposes a links endpoint. Use it when available,
        # but don't make the POC depend on it because snapshot already carries refs/content.
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


def test_individual_ytdlp(root: Path, urls: list[str], max_items: int = 3) -> list[dict]:
    output = root / "output" / "nicholas_crown" / "tiktok" / "camofox_individual"
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
    status_path = state_dir / "camofox_poc_status.json"
    urls_path = state_dir / "camofox_discovered_urls.json"

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

        individual = test_individual_ytdlp(root, urls, max_items=3) if urls else []

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
        return 0 if enumeration_pass else 1

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
            try:
                request_json(
                    "DELETE",
                    f"/tabs/{urllib.parse.quote(tab_id)}?"
                    + urllib.parse.urlencode({"userId": USER_ID}),
                    timeout=10,
                )
            except Exception:
                pass
        # Intentionally leave the locally started server alive for quick reruns.
        # Persistence is part of the CamoFox architecture under evaluation.


if __name__ == "__main__":
    raise SystemExit(main())
