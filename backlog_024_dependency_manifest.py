from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

TARGETS = ["playwright", "yt-dlp", "faster-whisper", "imageio-ffmpeg"]


def sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def dist_record(name: str) -> dict:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return {"name": name, "installed": False}

    files = list(dist.files or [])
    metadata_path = None
    record_path = None
    direct_url_present = False
    for rel in files:
        s = str(rel).replace("\\", "/")
        if s.endswith(".dist-info/METADATA"):
            metadata_path = Path(dist.locate_file(rel))
        elif s.endswith(".dist-info/RECORD"):
            record_path = Path(dist.locate_file(rel))
        elif s.endswith(".dist-info/direct_url.json"):
            direct_url_present = True

    return {
        "name": name,
        "installed": True,
        "version": dist.version,
        "metadata_sha256": sha256_file(metadata_path) if metadata_path else None,
        "record_sha256": sha256_file(record_path) if record_path else None,
        "direct_url_metadata_present": direct_url_present,
    }


def run_version(argv: list[str]) -> dict:
    try:
        cp = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
            check=False,
        )
        first = next((line.strip() for line in cp.stdout.splitlines() if line.strip()), "")
        return {"exit_code": cp.returncode, "first_line": first}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def find_playwright_browsers() -> list[dict]:
    roots = []
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured and configured != "0":
        roots.append(Path(configured))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "ms-playwright")

    seen = set()
    items = []
    for root in roots:
        try:
            root = root.resolve()
        except OSError:
            pass
        key = str(root).lower()
        if key in seen or not root.exists():
            continue
        seen.add(key)
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            entry = {"artifact_dir": child.name}
            exe_candidates = []
            for pattern in ("**/chrome.exe", "**/msedge.exe", "**/firefox.exe"):
                try:
                    exe_candidates.extend(child.glob(pattern))
                except OSError:
                    pass
            exe = next((p for p in exe_candidates if p.is_file()), None)
            if exe:
                entry["executable_name"] = exe.name
                entry["executable_sha256"] = sha256_file(exe)
                entry["version_probe"] = run_version([str(exe), "--version"])
            items.append(entry)
    return items


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    root_dir = app_dir.parent
    state_dir = root_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    out = state_dir / "backlog_024_influencer_dependency_manifest.json"

    installed = {}
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if name:
            installed[name.lower()] = dist.version

    ffmpeg = {"available": False}
    try:
        import imageio_ffmpeg  # type: ignore

        exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
        ffmpeg = {
            "available": exe.exists(),
            "executable_name": exe.name,
            "executable_sha256": sha256_file(exe) if exe.exists() else None,
            "version_probe": run_version([str(exe), "-version"]) if exe.exists() else None,
        }
    except Exception as exc:
        ffmpeg = {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    manifest = {
        "schema_version": 1,
        "purpose": "BACKLOG_024 InfluencerResearch installed dependency/provenance read-back",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "python_version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "venv_active": bool(os.environ.get("VIRTUAL_ENV")),
            "expected_runtime_suffix": "InstagramResearch\\venv",
            "runtime_matches_expected_suffix": "instagramresearch\\venv" in str(Path(sys.executable).parent.parent).lower(),
        },
        "direct_dependencies": [dist_record(name) for name in TARGETS],
        "all_installed_distributions": dict(sorted(installed.items())),
        "bundled_ffmpeg": ffmpeg,
        "playwright_browser_artifacts": find_playwright_browsers(),
        "security": {
            "environment_values_captured": False,
            "cookies_or_tokens_captured": False,
            "network_access_required": False,
            "packages_installed_or_modified": False,
        },
    }

    out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"MANIFEST_WRITTEN={out}")
    print("NO_PACKAGES_INSTALLED_OR_MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
