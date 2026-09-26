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
from pathlib import Path
from typing import Any

import camofox_container as camofox_container_config


MEDIA_RE = re.compile(
    r"(?:https?://(?:www\.)?instagram\.com)?/(?P<kind>reel|p)/(?P<id>[A-Za-z0-9_-]+)/?",
    re.IGNORECASE,
)
BLOCK_MARKERS = (
    "challenge",
    "checkpoint",
    "suspicious activity",
    "automated behavior",
    "try again later",
    "something went wrong",
    "sorry, this page isn't available",
)
LOGIN_MARKERS = (
    "log in",
    "sign up",
    "log into instagram",
    "login",
)
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def canonical_profile_url(value: str) -> str:
    parsed = urllib.parse.urlparse(str(value or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme != "https" or not (host == "instagram.com" or host.endswith(".instagram.com")):
        raise ValueError("Instagram HTTPS profile URL required")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1 or parts[0].startswith("@"):
        raise ValueError("Instagram profile URL must contain exactly one profile handle")
    handle = parts[0]
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
        raise ValueError("Invalid Instagram profile handle")
    return f"https://www.instagram.com/{handle}/"


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
        raise RuntimeError(f"HTTP {exc.code}: {detail[:500]}") from exc


def flatten_strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        out: list[str] = []
        for value in obj.values():
            out.extend(flatten_strings(value))
        return out
    if isinstance(obj, list):
        out: list[str] = []
        for value in obj:
            out.extend(flatten_strings(value))
        return out
    return []


def extract_media_links(obj: Any) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in flatten_strings(obj):
        text = raw.replace("\\/", "/")
        for match in MEDIA_RE.finditer(text):
            url = f"https://www.instagram.com/{match.group('kind').lower()}/{match.group('id')}/"
            if url not in seen:
                seen.add(url)
                found.append(url)
    return found


def compact_text(*objects: Any) -> str:
    chunks = []
    for obj in objects:
        chunks.extend(flatten_strings(obj))
    return " ".join(chunks).casefold()


def classify(snapshot: Any, links: Any, *, handle: str) -> tuple[str, list[str], list[str]]:
    media = extract_media_links(snapshot) + extract_media_links(links)
    media = list(dict.fromkeys(media))
    text = compact_text(snapshot, links)

    blocks = [marker for marker in BLOCK_MARKERS if marker in text]
    logins = [marker for marker in LOGIN_MARKERS if marker in text]

    if blocks:
        return "BLOCK_OR_CHALLENGE", media, blocks
    if media:
        return "PUBLIC_CONTENT", media, []
    if handle.casefold() in text and any(token in text for token in ("followers", "following", "posts")):
        return "PUBLIC_PROFILE_NO_MEDIA_LINKS", media, []
    if logins:
        return "LOGIN_WALL", media, logins
    return "LOADED_NO_PUBLIC_SIGNAL", media, []


def probe_target(url: str, *, attempt: int, label: str, handle: str) -> dict[str, Any]:
    user_id = f"influencerresearch-instagram-public-smoke-{attempt}-{label}"
    session_key = f"instagram-public-smoke-{attempt}-{label}"
    tab_id: str | None = None
    try:
        tab = request_json(
            "POST",
            "/tabs",
            {
                "userId": user_id,
                "sessionKey": session_key,
                "url": url,
                "trace": False,
            },
            timeout=60,
        )
        if not isinstance(tab, dict) or not tab.get("tabId"):
            raise RuntimeError("Camofox create-tab returned no tabId")
        tab_id = str(tab["tabId"])
        time.sleep(4)

        snapshot = request_json(
            "GET",
            f"/tabs/{urllib.parse.quote(tab_id)}/snapshot?"
            + urllib.parse.urlencode({"userId": user_id, "format": "text"}),
            timeout=30,
        )
        try:
            links = request_json(
                "GET",
                f"/tabs/{urllib.parse.quote(tab_id)}/links?"
                + urllib.parse.urlencode({"userId": user_id, "limit": 100}),
                timeout=20,
            )
        except Exception as exc:
            links = {"links_endpoint_error": f"{type(exc).__name__}: {exc}"}

        classification, media, markers = classify(snapshot, links, handle=handle)

        # One modest scroll only when no public-content/block/login signal appeared.
        if classification == "LOADED_NO_PUBLIC_SIGNAL":
            request_json(
                "POST",
                f"/tabs/{urllib.parse.quote(tab_id)}/scroll",
                {"userId": user_id, "direction": "down", "amount": 900},
                timeout=20,
            )
            time.sleep(1.5)
            snapshot2 = request_json(
                "GET",
                f"/tabs/{urllib.parse.quote(tab_id)}/snapshot?"
                + urllib.parse.urlencode({"userId": user_id, "format": "text"}),
                timeout=30,
            )
            classification, media, markers = classify(snapshot2, links, handle=handle)

        return {
            "attempt": attempt,
            "target": label,
            "requested_url": url,
            "classification": classification,
            "media_link_count": len(media),
            "media_links": media[:10],
            "markers": markers,
        }
    except Exception as exc:
        return {
            "attempt": attempt,
            "target": label,
            "requested_url": url,
            "classification": "ERROR",
            "media_link_count": 0,
            "media_links": [],
            "markers": [f"{type(exc).__name__}: {exc}"],
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
        # Explicitly remove any persisted state so attempts remain anonymous/independent.
        with contextlib.suppress(Exception):
            request_json(
                "DELETE",
                f"/sessions/{urllib.parse.quote(user_id)}/storage_state",
                timeout=10,
            )


def probe_ytdlp(url: str | None) -> dict[str, Any]:
    if not url:
        return {"tested": False, "reason": "NO_DISCOVERED_MEDIA_URL"}
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--skip-download",
        "--dump-single-json",
        "--no-warnings",
        "--",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"tested": True, "ok": False, "returncode": 124, "diagnostic": "timeout"}
    parsed: dict[str, Any] = {}
    if result.returncode == 0:
        with contextlib.suppress(Exception):
            parsed = json.loads(result.stdout)
    detail = (result.stderr or "").strip()
    return {
        "tested": True,
        "ok": result.returncode == 0,
        "returncode": int(result.returncode),
        "id": parsed.get("id"),
        "extractor": parsed.get("extractor"),
        "diagnostic": detail[-800:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Anonymous public Instagram smoke test through the existing Camofox service"
    )
    parser.add_argument(
        "--profile",
        default="https://www.instagram.com/rikatillsammans/",
    )
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()

    profile = canonical_profile_url(args.profile)
    handle = profile.rstrip("/").split("/")[-1]
    attempts = max(1, min(int(args.attempts), 3))
    targets = (
        ("profile", profile),
        ("reels", profile.rstrip("/") + "/reels/"),
    )

    health = request_json("GET", "/health", timeout=5)
    rows: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        for label, url in targets:
            rows.append(probe_target(url, attempt=attempt, label=label, handle=handle))

    public_attempts = sorted({
        row["attempt"]
        for row in rows
        if row["classification"] in {"PUBLIC_CONTENT", "PUBLIC_PROFILE_NO_MEDIA_LINKS"}
    })
    blocked = [row for row in rows if row["classification"] == "BLOCK_OR_CHALLENGE"]
    login_only = [row for row in rows if row["classification"] == "LOGIN_WALL"]
    discovered = []
    for row in rows:
        discovered.extend(row.get("media_links") or [])
    discovered = list(dict.fromkeys(discovered))

    if blocked:
        decision = "BLOCKED_OR_CHALLENGED"
    elif len(public_attempts) >= 2:
        decision = "CAMOFOX_PUBLIC_ACCESS_STABLE"
    elif public_attempts:
        decision = "CAMOFOX_PUBLIC_ACCESS_INTERMITTENT"
    elif login_only:
        decision = "LOGIN_WALL_ONLY"
    else:
        decision = "INCONCLUSIVE"

    result = {
        "profile": profile,
        "authenticated": False,
        "cookie_imported": False,
        "attempts": attempts,
        "camofox_health": health,
        "results": rows,
        "unique_media_links_found": len(discovered),
        "ytdlp_direct_media_probe": probe_ytdlp(discovered[0] if discovered else None),
        "decision": decision,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if decision == "CAMOFOX_PUBLIC_ACCESS_STABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
