from __future__ import annotations

import argparse
import contextlib
import json
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

import camofox_container as camofox_container_config


APP_VERSION = "0.3.0"
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REEL_ABS_RE = re.compile(r"https?://(?:www\.)?instagram\.com/reel/([A-Za-z0-9_-]+)/?", re.I)
REEL_REL_RE = re.compile(r"(?:^|[\"'\s(])(/reel/[A-Za-z0-9_-]+/?)", re.I)
HARD_BLOCK_PATTERNS = (
    "challenge",
    "checkpoint",
    "something went wrong",
    "sorry, this page isn't available",
    "page isn't available",
    "please wait a few minutes",
    "we restrict certain activity",
    "automated behavior",
)
AUTH_PROMPT_PATTERNS = (
    "log in",
    "login",
    "sign up",
)
LANGUAGE_DIALOG_PATTERNS = (
    "switch display language",
    "byt visningsspråk",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        raise RuntimeError(f"HTTP {exc.code} {url}: {detail[:1500]}") from exc


def flatten_strings(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for value in obj.values():
            out.extend(flatten_strings(value))
    elif isinstance(obj, list):
        for value in obj:
            out.extend(flatten_strings(value))
    return out


def extract_reel_urls(obj: Any) -> list[str]:
    found: list[str] = []
    for text in flatten_strings(obj):
        normalized = text.replace("\\/", "/")
        for match in REEL_ABS_RE.finditer(normalized):
            url = f"https://www.instagram.com/reel/{match.group(1)}/"
            if url not in found:
                found.append(url)
        for match in REEL_REL_RE.finditer(normalized):
            url = "https://www.instagram.com" + match.group(1)
            if not url.endswith("/"):
                url += "/"
            if url not in found:
                found.append(url)
    return found


def snapshot_body_text(obj: Any) -> str:
    if isinstance(obj, dict) and isinstance(obj.get("snapshot"), str):
        return str(obj["snapshot"])
    return "\n".join(flatten_strings(obj))


def classify_snapshot(
    snapshot_obj: Any,
    expected_handle: str,
    links_obj: Any = None,
) -> dict[str, Any]:
    text = snapshot_body_text(snapshot_obj)
    low = text.casefold()
    block_hits = sorted({p for p in HARD_BLOCK_PATTERNS if p in low})
    auth_prompt_hits = sorted({p for p in AUTH_PROMPT_PATTERNS if p in low})
    language_dialog_hits = sorted({p for p in LANGUAGE_DIALOG_PATTERNS if p in low})
    handle_visible = expected_handle.casefold() in low
    reels = extract_reel_urls({"snapshot": snapshot_obj, "links": links_obj})
    return {
        "handle_visible": handle_visible,
        "reel_count": len(reels),
        "reels": reels,
        "block_hits": block_hits,
        "auth_prompt_hits": auth_prompt_hits,
        "language_dialog_visible": bool(language_dialog_hits),
        "language_dialog_hits": language_dialog_hits,
        "snapshot_excerpt": text[:1200],
    }


def probe_public_session(profile_url: str, handle: str, run_index: int) -> dict[str, Any]:
    user_id = f"influencerresearch-instagram-public-smoke-{run_index}"
    session_key = f"public-{handle}-{run_index}-{int(time.time())}"
    tab_id: str | None = None
    rounds: list[dict[str, Any]] = []
    all_reels: list[str] = []

    try:
        tab = request_json(
            "POST",
            "/tabs",
            {
                "userId": user_id,
                "sessionKey": session_key,
                "url": profile_url,
                "trace": False,
            },
            timeout=60,
        )
        if not isinstance(tab, dict) or not tab.get("tabId"):
            raise RuntimeError(f"Unexpected Camofox create-tab response: {tab}")
        tab_id = str(tab["tabId"])
        time.sleep(5)

        for round_index in range(3):
            snap = request_json(
                "GET",
                f"/tabs/{urllib.parse.quote(tab_id)}/snapshot?"
                + urllib.parse.urlencode({"userId": user_id, "format": "text"}),
                timeout=30,
            )

            links = None
            links_error = None
            try:
                links = request_json(
                    "GET",
                    f"/tabs/{urllib.parse.quote(tab_id)}/links?"
                    + urllib.parse.urlencode({"userId": user_id, "limit": 120}),
                    timeout=20,
                )
            except Exception as exc:
                links_error = f"{type(exc).__name__}: {exc}"[:1000]

            classified = classify_snapshot(snap, handle, links)
            language_dialog_dismiss_attempted = False
            language_dialog_dismissed = False
            language_dialog_dismiss_error = None

            if classified["language_dialog_visible"]:
                language_dialog_dismiss_attempted = True
                try:
                    request_json(
                        "POST",
                        f"/tabs/{urllib.parse.quote(tab_id)}/press",
                        {"userId": user_id, "key": "Escape"},
                        timeout=10,
                    )
                    time.sleep(0.75)
                    snap = request_json(
                        "GET",
                        f"/tabs/{urllib.parse.quote(tab_id)}/snapshot?"
                        + urllib.parse.urlencode({"userId": user_id, "format": "text"}),
                        timeout=30,
                    )
                    try:
                        links = request_json(
                            "GET",
                            f"/tabs/{urllib.parse.quote(tab_id)}/links?"
                            + urllib.parse.urlencode({"userId": user_id, "limit": 120}),
                            timeout=20,
                        )
                    except Exception as exc:
                        links_error = f"{type(exc).__name__}: {exc}"[:1000]
                    classified = classify_snapshot(snap, handle, links)
                    language_dialog_dismissed = not classified["language_dialog_visible"]
                except Exception as exc:
                    language_dialog_dismiss_error = f"{type(exc).__name__}: {exc}"[:1000]

            for url in classified["reels"]:
                if url not in all_reels:
                    all_reels.append(url)

            rounds.append(
                {
                    "round": round_index + 1,
                    "handle_visible": classified["handle_visible"],
                    "reel_count_total": len(all_reels),
                    "block_hits": classified["block_hits"],
                    "auth_prompt_hits": classified["auth_prompt_hits"],
                    "language_dialog_visible": classified["language_dialog_visible"],
                    "language_dialog_hits": classified["language_dialog_hits"],
                    "language_dialog_dismiss_attempted": language_dialog_dismiss_attempted,
                    "language_dialog_dismissed": language_dialog_dismissed,
                    "language_dialog_dismiss_error": language_dialog_dismiss_error,
                    "links_error": links_error,
                    "snapshot_excerpt": classified["snapshot_excerpt"],
                }
            )

            if all_reels:
                break

            request_json(
                "POST",
                f"/tabs/{urllib.parse.quote(tab_id)}/scroll",
                {"userId": user_id, "direction": "down", "amount": 1100},
                timeout=20,
            )
            time.sleep(1.5)

        blocked = any(row["block_hits"] for row in rounds)
        handle_visible = any(row["handle_visible"] for row in rounds)
        return {
            "run": run_index,
            "ok": bool(handle_visible and not blocked),
            "handle_visible": handle_visible,
            "blocked": blocked,
            "reel_count": len(all_reels),
            "reels": all_reels[:20],
            "reel_discovery_ok": bool(all_reels),
            "rounds": rounds,
        }
    finally:
        if tab_id:
            with contextlib.suppress(Exception):
                request_json(
                    "DELETE",
                    f"/tabs/{urllib.parse.quote(tab_id)}?"
                    + urllib.parse.urlencode({"userId": user_id}),
                    timeout=10,
                )
        with contextlib.suppress(Exception):
            request_json(
                "DELETE",
                f"/sessions/{urllib.parse.quote(user_id)}/storage_state",
                timeout=10,
            )


def ytdlp_probe(url: str) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--skip-download",
        "--no-playlist",
        "--no-warnings",
        "--dump-json",
        "--",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        return {
            "url": url,
            "returncode": 124,
            "ok": False,
            "error": f"timeout: {exc}",
        }

    parsed = None
    if result.returncode == 0 and result.stdout.strip():
        try:
            parsed = json.loads(result.stdout.splitlines()[-1])
        except json.JSONDecodeError:
            parsed = None

    return {
        "url": url,
        "returncode": int(result.returncode),
        "ok": result.returncode == 0 and isinstance(parsed, dict),
        "id": parsed.get("id") if isinstance(parsed, dict) else None,
        "extractor": parsed.get("extractor") if isinstance(parsed, dict) else None,
        "diagnostic_tail": ((result.stderr or result.stdout or "").strip())[-1800:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Public, login-free Instagram Camofox smoke")
    parser.add_argument("--profile-url", default="https://www.instagram.com/rikatillsammans/")
    parser.add_argument("--handle", default="rikatillsammans")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    runs = max(1, min(int(args.runs), 5))
    profile_url = str(args.profile_url).strip()
    handle = str(args.handle).strip().lstrip("@")

    if not profile_url.startswith("https://www.instagram.com/") or not handle:
        raise SystemExit("Only canonical public instagram.com profile URLs are allowed.")

    started = utc_now()
    results: list[dict[str, Any]] = []
    for index in range(1, runs + 1):
        try:
            results.append(probe_public_session(profile_url, handle, index))
        except Exception as exc:
            results.append(
                {
                    "run": index,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "handle_visible": False,
                    "blocked": False,
                    "reel_count": 0,
                    "reels": [],
                    "reel_discovery_ok": False,
                }
            )
        if index != runs:
            time.sleep(2)

    unique_reels: list[str] = []
    for row in results:
        for url in row.get("reels", []):
            if url not in unique_reels:
                unique_reels.append(url)

    ytdlp = ytdlp_probe(unique_reels[0]) if unique_reels else None
    successful_runs = sum(1 for row in results if row.get("ok"))
    blocked_runs = sum(1 for row in results if row.get("blocked"))

    if successful_runs == runs and unique_reels:
        decision = "CAMOFOX_PUBLIC_DISCOVERY_STABLE"
    elif blocked_runs == runs:
        decision = "CAMOFOX_PUBLIC_ACCESS_BLOCKED"
    elif successful_runs >= max(1, runs - 1) and not unique_reels:
        decision = "CAMOFOX_PROFILE_VISIBLE_REELS_NOT_DISCOVERED"
    elif successful_runs >= max(1, runs - 1):
        decision = "CAMOFOX_PROFILE_ACCESS_MOSTLY_STABLE"
    elif successful_runs:
        decision = "CAMOFOX_PUBLIC_ACCESS_UNSTABLE"
    else:
        decision = "CAMOFOX_PUBLIC_ACCESS_INCONCLUSIVE"

    interaction_used = any(
        round_row.get("language_dialog_dismiss_attempted")
        for result_row in results
        for round_row in result_row.get("rounds", [])
    )

    status = {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "started_at": started,
        "finished_at": utc_now(),
        "profile_url": profile_url,
        "handle": handle,
        "auth_used": False,
        "cookies_used": False,
        "interaction_used": interaction_used,
        "runs_requested": runs,
        "successful_runs": successful_runs,
        "blocked_runs": blocked_runs,
        "unique_reels_found": len(unique_reels),
        "results": results,
        "ytdlp_public_reel_probe": ytdlp,
        "decision": decision,
    }

    root = Path(__file__).resolve().parent.parent
    path = root / "state" / "instagram" / "public_camofox_smoke_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if successful_runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
