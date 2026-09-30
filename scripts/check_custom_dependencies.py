#!/usr/bin/env python3
"""Signal reviewed Camofox/Camoufox pins that have newer upstream candidates."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "runtime" / "camofox" / "Dockerfile"
PACKAGE_JSON = ROOT / "runtime" / "camofox" / "package.json"

CAMOUFOX_REPO = "daijro/camoufox"
CAMOFOX_BROWSER_REPO = "LurigeLars/camofox-browser"
CAMOFOX_BROWSER_BRANCH = "master"
CAMOUFOX_ISSUE = "Dependency watch: Camoufox upstream release"
CAMOFOX_BROWSER_ISSUE = "Dependency watch: camofox-browser reviewed pin"

TAG_RE = re.compile(r"^v\d+\.\d+\.\d+-(?:alpha|beta)\.\d+$")
ARCHIVE_RE = re.compile(
    r"github\.com/LurigeLars/camofox-browser/archive/([0-9a-f]{40})\.tar\.gz$"
)


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "").strip()


def github_json(path: str, *, method: str = "GET", body: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "InfluencerResearch-dependency-watch",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if _token():
        headers["Authorization"] = f"Bearer {_token()}"
    req = urllib.request.Request(
        f"https://api.github.com{path}", data=data, method=method, headers=headers
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def current_camoufox_tag() -> str:
    text = DOCKERFILE.read_text(encoding="utf-8")
    version = re.search(r"^ARG CAMOUFOX_VERSION=(\S+)$", text, re.MULTILINE)
    release = re.search(r"^ARG CAMOUFOX_RELEASE=(\S+)$", text, re.MULTILINE)
    if not version or not release:
        raise RuntimeError("Camoufox Dockerfile pin could not be parsed")
    return f"v{version.group(1)}-{release.group(1)}"


def latest_camoufox_tag() -> tuple[str, str]:
    releases = github_json(f"/repos/{CAMOUFOX_REPO}/releases?per_page=30")
    for release in releases:
        tag = str(release.get("tag_name") or "")
        if not release.get("draft") and TAG_RE.fullmatch(tag):
            return tag, str(release.get("html_url") or "")
    raise RuntimeError("No Camoufox browser release matching the reviewed tag scheme")


def current_camofox_browser_sha() -> str:
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    spec = str(package["dependencies"]["@askjo/camofox-browser"])
    match = ARCHIVE_RE.search(spec)
    if not match:
        raise RuntimeError("Reviewed camofox-browser archive pin could not be parsed")
    return match.group(1)


def latest_camofox_browser_sha() -> tuple[str, str]:
    commit = github_json(
        f"/repos/{CAMOFOX_BROWSER_REPO}/commits/{CAMOFOX_BROWSER_BRANCH}"
    )
    sha = str(commit.get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("camofox-browser branch head was not a full commit SHA")
    return sha, str(commit.get("html_url") or "")


def _find_issue(repository: str, title: str) -> dict | None:
    issues = github_json(f"/repos/{repository}/issues?state=all&per_page=100")
    for issue in issues:
        if "pull_request" not in issue and issue.get("title") == title:
            return issue
    return None


def _sync_issue(repository: str, title: str, *, outdated: bool, body: str) -> None:
    issue = _find_issue(repository, title)
    if outdated:
        if issue is None:
            github_json(
                f"/repos/{repository}/issues",
                method="POST",
                body={"title": title, "body": body},
            )
        else:
            github_json(
                f"/repos/{repository}/issues/{issue['number']}",
                method="PATCH",
                body={"body": body, "state": "open"},
            )
    elif issue is not None and issue.get("state") == "open":
        github_json(
            f"/repos/{repository}/issues/{issue['number']}",
            method="PATCH",
            body={"body": body + "\n\nStatus: reviewed pin currently matches upstream.", "state": "closed"},
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sync-issues",
        action="store_true",
        help="Create/update one persistent issue per watched upstream when a candidate is newer.",
    )
    args = parser.parse_args()

    current_camoufox = current_camoufox_tag()
    latest_camoufox, latest_camoufox_url = latest_camoufox_tag()
    current_browser = current_camofox_browser_sha()
    latest_browser, latest_browser_url = latest_camofox_browser_sha()

    result = {
        "camoufox": {
            "current": current_camoufox,
            "latest": latest_camoufox,
            "outdated": current_camoufox != latest_camoufox,
            "latest_url": latest_camoufox_url,
        },
        "camofox_browser": {
            "current": current_browser,
            "latest": latest_browser,
            "branch": CAMOFOX_BROWSER_BRANCH,
            "outdated": current_browser != latest_browser,
            "latest_url": latest_browser_url,
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))

    if args.sync_issues:
        repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if not repository or not _token():
            raise RuntimeError("GITHUB_REPOSITORY and GITHUB_TOKEN are required for issue sync")

        _sync_issue(
            repository,
            CAMOUFOX_ISSUE,
            outdated=result["camoufox"]["outdated"],
            body=(
                "A newer Camoufox browser release candidate exists.\n\n"
                f"- reviewed runtime pin: `{current_camoufox}`\n"
                f"- newest upstream browser release: `{latest_camoufox}`\n"
                f"- upstream: {latest_camoufox_url}\n\n"
                "This watcher is signal-only. Promotion requires manual compatibility, "
                "digest and runtime review; do not auto-merge or auto-deploy."
            ),
        )
        _sync_issue(
            repository,
            CAMOFOX_BROWSER_ISSUE,
            outdated=result["camofox_browser"]["outdated"],
            body=(
                "The protected camofox-browser master branch moved beyond the reviewed archive pin.\n\n"
                f"- reviewed pin: `{current_browser}`\n"
                f"- current `{CAMOFOX_BROWSER_BRANCH}` head: `{latest_browser}`\n"
                f"- upstream fork commit: {latest_browser_url}\n\n"
                "This watcher is signal-only. The exact commit remains manual-review-only."
            ),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
