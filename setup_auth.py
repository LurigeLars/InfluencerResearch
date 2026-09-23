from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


EXPECTED_USERNAME = os.environ.get("INFLUENCER_RESEARCH_INSTAGRAM_USERNAME", "").strip().lstrip("@")


def chrome_profile_dir() -> Path:
    explicit = str(os.environ.get("INFLUENCER_RESEARCH_RUNTIME_DIR") or "").strip()
    if explicit:
        return Path(explicit) / "chrome-profile"
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    return local / "InstagramResearch" / "chrome-profile"


def cookie_export_path() -> Path:
    explicit = str(os.environ.get("INFLUENCER_RESEARCH_RUNTIME_DIR") or "").strip()
    if explicit:
        root = Path(explicit)
    else:
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch"
    return root / "secrets" / "instagram_cookies.json"


def write_cookie_export(cookies: list[dict]) -> Path:
    path = cookie_export_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cookies, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def main() -> int:
    profile_dir = chrome_profile_dir()
    if not EXPECTED_USERNAME:
        print("ERROR: set INFLUENCER_RESEARCH_INSTAGRAM_USERNAME before authenticating.")
        return 2
    profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"Instagram research account: @{EXPECTED_USERNAME}")
    print(f"Dedicated automation profile: {profile_dir}")
    print()
    print("This is a separate Chrome data directory used ONLY by InstagramResearch.")
    print("It does NOT read your normal Chrome profiles or decrypt their cookie databases.")
    print()
    print("A Chrome window will open.")
    print(f"Log in to Instagram as @{EXPECTED_USERNAME}.")
    print("When Instagram is fully logged in, return to this terminal and press Enter.")
    print()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            args=["--start-maximized"],
            viewport=None,
        )
        try:
            pages = context.pages
            page = pages[0] if pages else context.new_page()
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=60000)

            input("Press Enter here AFTER Instagram login is complete... ")

            cookies = context.cookies(["https://www.instagram.com/"])
            names = {c.get("name", "") for c in cookies}
            if "sessionid" not in names:
                print()
                print("ERROR: no Instagram session cookie was detected in the dedicated profile.")
                print("Make sure the account is logged in in the opened Chrome window, then run this file again.")
                return 2

            cookie_path = write_cookie_export(cookies)
            print()
            print("Authentication verified.")
            print(f"Dedicated InstagramResearch Chrome state is stored locally at: {profile_dir}")
            print(f"Portable session-cookie export written locally at: {cookie_path}")
            print("The cookie export is sensitive and must never be committed or copied to Drive.")
            return 0
        finally:
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
