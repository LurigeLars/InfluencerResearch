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


APP_VERSION = "0.7.0"
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REEL_ABS_RE = re.compile(r"https?://(?:www\.)?instagram\.com/reel/([A-Za-z0-9_-]+)/?", re.I)
REEL_REL_RE = re.compile(r"(?:^|[\"'\s(=])(/reel/[A-Za-z0-9_-]+/?)", re.I)
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
LANGUAGE_COMBOBOX_RE = re.compile(
    r'combobox "(?:Switch Display Language|Byt visningsspråk)" \[(e\d+)\]',
    re.I,
)
SELECTED_LANGUAGE_RE = re.compile(r'- option "([^"]+)" \[selected\]', re.I)


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


def language_dialog_action(snapshot_obj: Any) -> dict[str, Any]:
    text = snapshot_body_text(snapshot_obj)
    ref_match = LANGUAGE_COMBOBOX_RE.search(text)
    selected_match = SELECTED_LANGUAGE_RE.search(text)
    selected = selected_match.group(1) if selected_match else None
    target = "English" if selected and selected.casefold() == "svenska" else "Svenska"
    return {
        "ref": ref_match.group(1) if ref_match else None,
        "selected": selected,
        "target": target,
    }


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


def dom_probe(tab_id: str, user_id: str, expected_handle: str) -> dict[str, Any]:
    expression = r"""(() => {
      const bodyText = document.body?.innerText || "";
      const links = Array.from(document.querySelectorAll("a[href]"), a => a.href).filter(Boolean);
      const reels = Array.from(new Set(links.filter(href => /instagram\.com\/reel\//i.test(href)))).slice(0, 50);
      const posts = Array.from(new Set(links.filter(href => /instagram\.com\/(?:p|reel)\//i.test(href)))).slice(0, 100);
      const dialogs = Array.from(document.querySelectorAll('dialog,[role="dialog"]')).slice(0, 10).map(el => ({
        role: el.getAttribute("role") || el.tagName.toLowerCase(),
        text: (el.innerText || "").slice(0, 1000),
      }));
      const buttons = Array.from(document.querySelectorAll("button")).slice(0, 100).map(el => ({
        text: (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim(),
        ariaLabel: el.getAttribute("aria-label"),
      })).filter(row => row.text || row.ariaLabel);
      const selects = Array.from(document.querySelectorAll("select")).slice(0, 20).map(el => ({
        ariaLabel: el.getAttribute("aria-label"),
        value: el.value,
        selectedText: el.selectedOptions?.[0]?.text || null,
        options: Array.from(el.options || []).slice(0, 100).map(o => o.text),
      }));
      return {
        title: document.title,
        href: location.href,
        body_text_length: bodyText.length,
        body_text_excerpt: bodyText.slice(0, 5000),
        reel_links: reels,
        media_links: posts,
        dialogs,
        buttons,
        selects,
        cookie_consent_visible: dialogs.some(d =>
          /allow the use of cookies from instagram|vill du tillåta användningen av cookies från instagram/i.test(d.text)
        ),
      };
    })()"""
    response = request_json(
        "POST",
        f"/tabs/{urllib.parse.quote(tab_id)}/evaluate",
        {"userId": user_id, "expression": expression},
        timeout=30,
    )
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected DOM probe response: {response}")
    body_text = str(result.get("body_text_excerpt") or "")
    result["handle_visible"] = expected_handle.casefold() in body_text.casefold()
    return result


def decline_optional_cookies(tab_id: str, user_id: str) -> dict[str, Any]:
    expression = r"""(() => {
      const normalize = value => (value || "").replace(/\s+/g, " ").trim().toLowerCase();
      const acceptedLabels = new Set([
        "decline optional cookies",
        "neka valfria cookies",
      ]);
      const buttons = Array.from(document.querySelectorAll("button"));
      const target = buttons.find(button =>
        acceptedLabels.has(normalize(button.innerText || button.textContent))
      );
      const available = buttons
        .map(button => (button.innerText || button.textContent || "").replace(/\s+/g, " ").trim())
        .filter(Boolean)
        .slice(0, 50);
      if (!target) {
        return {clicked: false, reason: "decline_button_not_found", available_buttons: available};
      }
      const label = (target.innerText || target.textContent || "").replace(/\s+/g, " ").trim();
      target.click();
      return {clicked: true, label, available_buttons: available};
    })()"""
    response = request_json(
        "POST",
        f"/tabs/{urllib.parse.quote(tab_id)}/evaluate",
        {"userId": user_id, "expression": expression},
        timeout=20,
    )
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected cookie-consent response: {response}")
    return result


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
            dom = None
            dom_error = None
            try:
                dom = dom_probe(tab_id, user_id, handle)
                for url in extract_reel_urls(dom.get("reel_links", [])):
                    if url not in all_reels:
                        all_reels.append(url)
            except Exception as exc:
                dom_error = f"{type(exc).__name__}: {exc}"[:1000]

            language_dialog_dismiss_attempted = False
            language_dialog_dismissed = False
            language_dialog_dismiss_error = None
            language_dialog_action_info = None

            cookie_consent_attempted = False
            cookie_consent_action = None
            cookie_consent_error = None
            if isinstance(dom, dict) and dom.get("cookie_consent_visible"):
                cookie_consent_attempted = True
                try:
                    cookie_consent_action = decline_optional_cookies(tab_id, user_id)
                    if cookie_consent_action.get("clicked"):
                        time.sleep(2.0)
                        dom = dom_probe(tab_id, user_id, handle)
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
                        for url in extract_reel_urls(dom.get("reel_links", [])):
                            if url not in all_reels:
                                all_reels.append(url)
                except Exception as exc:
                    cookie_consent_error = f"{type(exc).__name__}: {exc}"[:1000]

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
                    "language_dialog_action": language_dialog_action_info,
                    "language_dialog_dismissed": language_dialog_dismissed,
                    "language_dialog_dismiss_error": language_dialog_dismiss_error,
                    "cookie_consent_attempted": cookie_consent_attempted,
                    "cookie_consent_action": cookie_consent_action,
                    "cookie_consent_error": cookie_consent_error,
                    "links_error": links_error,
                    "snapshot_excerpt": classified["snapshot_excerpt"],
                    "dom_probe": dom,
                    "dom_probe_error": dom_error,
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
        handle_visible = any(
            row["handle_visible"]
            or bool((row.get("dom_probe") or {}).get("handle_visible"))
            for row in rounds
        )
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



def story_dom_probe(tab_id: str, user_id: str, handle: str) -> dict[str, Any]:
    expression = r"""(() => {
      const bodyText = document.body?.innerText || "";
      const href = location.href;
      const normalize = value => (value || "").replace(/\s+/g, " ").trim();
      const buttons = Array.from(document.querySelectorAll("button")).slice(0, 100).map(el => ({
        text: normalize(el.innerText || el.textContent),
        ariaLabel: el.getAttribute("aria-label"),
      })).filter(row => row.text || row.ariaLabel);
      const videos = Array.from(document.querySelectorAll("video")).slice(0, 20).map(el => ({
        src: el.currentSrc || el.src || null,
        poster: el.poster || null,
        paused: el.paused,
      }));
      const images = Array.from(document.querySelectorAll("img")).slice(0, 80).map(el => ({
        src: el.currentSrc || el.src || null,
        alt: el.alt || null,
      }));
      const links = Array.from(document.querySelectorAll("a[href]"), a => a.href).filter(Boolean);
      const storyLinks = Array.from(new Set(
        links.filter(link => /instagram\.com\/stories\//i.test(link))
      )).slice(0, 50);
      const dialogs = Array.from(document.querySelectorAll('dialog,[role="dialog"]')).slice(0, 10).map(el => ({
        role: el.getAttribute("role") || el.tagName.toLowerCase(),
        text: (el.innerText || "").slice(0, 1500),
      }));
      return {
        title: document.title,
        href,
        body_text_length: bodyText.length,
        body_text_excerpt: bodyText.slice(0, 5000),
        buttons,
        videos,
        images,
        story_links: storyLinks,
        dialogs,
        cookie_consent_visible: dialogs.some(d =>
          /allow the use of cookies from instagram|vill du tillåta användningen av cookies från instagram/i.test(d.text)
        ),
      };
    })()"""
    response = request_json(
        "POST",
        f"/tabs/{urllib.parse.quote(tab_id)}/evaluate",
        {"userId": user_id, "expression": expression},
        timeout=30,
    )
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected story DOM probe response: {response}")

    href = str(result.get("href") or "")
    body = str(result.get("body_text_excerpt") or "")
    low = body.casefold()
    story_prefix = f"https://www.instagram.com/stories/{handle.casefold()}/"
    result["story_url_active"] = href.casefold().startswith(story_prefix)
    result["login_surface"] = (
        "/accounts/login" in href.casefold()
        or "log in to instagram" in low
        or "logga in på instagram" in low
        or "log in to see photos and videos" in low
    )
    result["generic_error"] = any(
        needle in low
        for needle in (
            "sorry, something went wrong",
            "we're working on getting this fixed",
            "tyvärr har något gått fel",
        )
    )
    result["view_confirmation_visible"] = any(
        needle in low
        for needle in ("view story", "visa händelse")
    )
    return result


def click_story_view_confirmation(tab_id: str, user_id: str) -> dict[str, Any]:
    expression = r"""(() => {
      const normalize = value => (value || "").replace(/\s+/g, " ").trim().toLowerCase();
      const accepted = new Set(["view story", "visa händelse"]);
      const candidates = Array.from(document.querySelectorAll("button,[role='button']"));
      const target = candidates.find(el =>
        accepted.has(normalize(el.innerText || el.textContent || el.getAttribute("aria-label")))
      );
      const available = candidates
        .map(el => normalize(el.innerText || el.textContent || el.getAttribute("aria-label")))
        .filter(Boolean)
        .slice(0, 80);
      if (!target) {
        return {clicked: false, reason: "view_story_control_not_found", available_controls: available};
      }
      const label = normalize(target.innerText || target.textContent || target.getAttribute("aria-label"));
      target.click();
      return {clicked: true, label, available_controls: available};
    })()"""
    response = request_json(
        "POST",
        f"/tabs/{urllib.parse.quote(tab_id)}/evaluate",
        {"userId": user_id, "expression": expression},
        timeout=20,
    )
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected story-confirmation response: {response}")
    return result


def classify_story_probe(dom: dict[str, Any], handle: str) -> str:
    href = str(dom.get("href") or "").casefold()
    body = str(dom.get("body_text_excerpt") or "").casefold()

    if dom.get("login_surface"):
        return "STORY_REQUIRES_AUTH"
    if dom.get("generic_error"):
        return "STORY_PUBLIC_ACCESS_ERROR"
    story_frame_pattern = re.compile(
        rf"/stories/{re.escape(handle.casefold())}/\d+/?(?:[?#].*)?$",
        re.I,
    )
    if (
        story_frame_pattern.search(href)
        or bool(dom.get("videos"))
        or any(
            story_frame_pattern.search(str(link).casefold())
            for link in dom.get("story_links", [])
        )
    ):
        return "PUBLIC_STORY_ACCESSIBLE"
    if f"/stories/{handle.casefold()}/" not in href:
        return "NO_ACTIVE_STORY_OR_REDIRECTED"
    if any(
        phrase in body
        for phrase in (
            "story isn't available",
            "story is unavailable",
            "händelsen är inte tillgänglig",
        )
    ):
        return "NO_ACTIVE_STORY"
    if dom.get("view_confirmation_visible"):
        return "STORY_VIEW_CONFIRMATION_BLOCKED"
    return "STORY_PUBLIC_ACCESS_INCONCLUSIVE"


def probe_public_story(handle: str) -> dict[str, Any]:
    user_id = "influencerresearch-instagram-public-story-smoke"
    session_key = f"public-story-{handle}-{int(time.time())}"
    story_url = f"https://www.instagram.com/stories/{handle}/"
    tab_id: str | None = None
    cookie_action = None
    confirmation_action = None
    try:
        tab = request_json(
            "POST",
            "/tabs",
            {
                "userId": user_id,
                "sessionKey": session_key,
                "url": story_url,
                "trace": False,
            },
            timeout=60,
        )
        if not isinstance(tab, dict) or not tab.get("tabId"):
            raise RuntimeError(f"Unexpected Camofox create-tab response: {tab}")
        tab_id = str(tab["tabId"])
        time.sleep(5)

        dom = story_dom_probe(tab_id, user_id, handle)

        if dom.get("cookie_consent_visible"):
            cookie_action = decline_optional_cookies(tab_id, user_id)
            if cookie_action.get("clicked"):
                time.sleep(2)
                dom = story_dom_probe(tab_id, user_id, handle)

        if dom.get("view_confirmation_visible"):
            confirmation_action = click_story_view_confirmation(tab_id, user_id)
            if confirmation_action.get("clicked"):
                time.sleep(2)
                dom = story_dom_probe(tab_id, user_id, handle)

        decision = classify_story_probe(dom, handle)
        return {
            "story_url": story_url,
            "decision": decision,
            "cookie_consent_action": cookie_action,
            "view_confirmation_action": confirmation_action,
            "dom_probe": dom,
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
    parser.add_argument(
        "--skip-story-probe",
        action="store_true",
        help="Skip the login-free public Story capability probe.",
    )
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
    story_probe = None
    if not args.skip_story_probe:
        try:
            story_probe = probe_public_story(handle)
        except Exception as exc:
            story_probe = {
                "story_url": f"https://www.instagram.com/stories/{handle}/",
                "decision": "STORY_PUBLIC_PROBE_ERROR",
                "error": f"{type(exc).__name__}: {exc}",
            }

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
        round_row.get("cookie_consent_attempted")
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
        "cookie_consent_interaction_used": interaction_used,
        "interaction_used": interaction_used,
        "runs_requested": runs,
        "successful_runs": successful_runs,
        "blocked_runs": blocked_runs,
        "unique_reels_found": len(unique_reels),
        "results": results,
        "ytdlp_public_reel_probe": ytdlp,
        "story_public_probe": story_probe,
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
