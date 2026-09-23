from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXPECTED_PYTHON = {
    "playwright": "1.63.0",
    "yt-dlp": "2026.8.19",
    "yt-dlp-ejs": "0.8.0",
    "faster-whisper": "1.2.1",
    "imageio-ffmpeg": "0.6.0",
}
EXPECTED_REQUIREMENTS = [f"{k}=={v}" for k, v in EXPECTED_PYTHON.items()]
EXPECTED_CAMOFOX = "1.13.1"
EXPECTED_CAMOUFOX_JS = "0.11.5"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def safe_version(cmd: list[str]) -> dict[str, Any]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        text = (p.stdout or p.stderr or "").strip().splitlines()
        return {"exit_code": p.returncode, "first_line": text[0] if text else ""}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def file_evidence(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False}
    return {
        "present": True,
        "size": path.stat().st_size,
        "sha256": sha256(path),
    }


def atomic_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


app_dir = Path(__file__).resolve().parent
root = app_dir.parent
state_dir = root / "state"
requirements_path = app_dir / "requirements.txt"
youtube_path = app_dir / "youtube_creator_evaluation.py"

localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
camo_root = localappdata / "InstagramResearch" / "camofox-poc"
node_pkg = camo_root / "node_modules" / "@askjo" / "camofox-browser"
camoufox_pkg = camo_root / "node_modules" / "camoufox-js"
cache_root = localappdata / "camoufox" / "camoufox" / "Cache"

result: dict[str, Any] = {
    "schema_version": 1,
    "purpose": "BACKLOG_024 local sync verification + CamoFox read-only inventory",
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "security": {
        "camofox_executed": False,
        "npm_install_executed": False,
        "npx_executed": False,
        "browser_started": False,
        "network_required": False,
        "cookies_or_tokens_captured": False,
        "environment_values_captured": False,
    },
}

# 1) Verify Drive->local sync of the InfluencerResearch hardening.
req_lines: list[str] = []
if requirements_path.is_file():
    req_lines = [line.strip() for line in requirements_path.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#")]

youtube_text = youtube_path.read_text(encoding="utf-8-sig") if youtube_path.is_file() else ""
installed_python: dict[str, str | None] = {}
for name in EXPECTED_PYTHON:
    try:
        installed_python[name] = md.version(name)
    except md.PackageNotFoundError:
        installed_python[name] = None

result["influencer_sync"] = {
    "requirements_path": str(requirements_path),
    "requirements_sha256": sha256(requirements_path) if requirements_path.is_file() else None,
    "requirements_exact_match": req_lines == EXPECTED_REQUIREMENTS,
    "requirements_lines": req_lines,
    "youtube_path": str(youtube_path),
    "youtube_sha256": sha256(youtube_path) if youtube_path.is_file() else None,
    "no_remote_components_present": '"--no-remote-components"' in youtube_text,
    "remote_components_github_absent": "ejs:github" not in youtube_text and '"--remote-components"' not in youtube_text,
    "installed_python": installed_python,
    "installed_python_exact_match": installed_python == EXPECTED_PYTHON,
    "python_executable": sys.executable,
    "python_version": sys.version.split()[0],
}

# 2) Read CamoFox/npm state from disk only. Do NOT execute any CamoFox JS/package.
package_json = camo_root / "package.json"
package_lock = camo_root / "package-lock.json"
camo_package_json = node_pkg / "package.json"
camo_postinstall = node_pkg / "scripts" / "postinstall.js"
camoufox_package_json = camoufox_pkg / "package.json"

camo: dict[str, Any] = {
    "root": str(camo_root),
    "root_present": camo_root.is_dir(),
    "node_version": safe_version([shutil.which("node") or "node", "--version"]),
    "npm_version": safe_version([shutil.which("npm") or "npm", "--version"]),
    "files": {
        "package.json": file_evidence(package_json),
        "package-lock.json": file_evidence(package_lock),
        "node_modules/@askjo/camofox-browser/package.json": file_evidence(camo_package_json),
        "node_modules/@askjo/camofox-browser/scripts/postinstall.js": file_evidence(camo_postinstall),
        "node_modules/camoufox-js/package.json": file_evidence(camoufox_package_json),
    },
}

root_pkg = read_json(package_json) if package_json.is_file() else {}
lock = read_json(package_lock) if package_lock.is_file() else {}
camo_pkg = read_json(camo_package_json) if camo_package_json.is_file() else {}
camoufox = read_json(camoufox_package_json) if camoufox_package_json.is_file() else {}

camo["root_dependency"] = (root_pkg.get("dependencies") or {}).get("@askjo/camofox-browser")
camo["installed_camofox_version"] = camo_pkg.get("version")
camo["installed_camoufox_js_version"] = camoufox.get("version")
camo["camofox_postinstall_script"] = (camo_pkg.get("scripts") or {}).get("postinstall")
camo["camofox_declared_camoufox_js"] = (camo_pkg.get("dependencies") or {}).get("camoufox-js")

packages = lock.get("packages") if isinstance(lock, dict) else {}
if not isinstance(packages, dict):
    packages = {}
for lock_path, key in [
    ("node_modules/@askjo/camofox-browser", "camofox_browser"),
    ("node_modules/camoufox-js", "camoufox_js"),
]:
    entry = packages.get(lock_path)
    if isinstance(entry, dict):
        camo.setdefault("lock_entries", {})[key] = {
            "version": entry.get("version"),
            "resolved": entry.get("resolved"),
            "integrity": entry.get("integrity"),
            "has_install_script": entry.get("hasInstallScript"),
        }

# Cache provenance evidence: filenames/sizes/hashes only under the dedicated Camoufox cache.
cache: dict[str, Any] = {
    "path": str(cache_root),
    "present": cache_root.is_dir(),
    "version_json": file_evidence(cache_root / "version.json"),
    "executables": [],
}
if cache_root.is_dir():
    version_path = cache_root / "version.json"
    if version_path.is_file():
        try:
            v = read_json(version_path)
            cache["version_json_fields"] = v if isinstance(v, (dict, list, str, int, float, bool, type(None))) else str(type(v))
        except Exception as exc:
            cache["version_json_parse_error"] = f"{type(exc).__name__}: {exc}"
    exe_count = 0
    for p in cache_root.rglob("*.exe"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(cache_root))
        cache["executables"].append({"relative_path": rel, "size": p.stat().st_size, "sha256": sha256(p)})
        exe_count += 1
        if exe_count >= 20:
            cache["executables_truncated"] = True
            break
camo["browser_cache"] = cache

camo["exact_baseline_match"] = (
    camo.get("installed_camofox_version") == EXPECTED_CAMOFOX
    and camo.get("installed_camoufox_js_version") == EXPECTED_CAMOUFOX_JS
    and camo_package_json.is_file()
    and camo_postinstall.is_file()
    and package_lock.is_file()
)
result["camofox_readback"] = camo

sync_ok = all([
    result["influencer_sync"]["requirements_exact_match"],
    result["influencer_sync"]["no_remote_components_present"],
    result["influencer_sync"]["remote_components_github_absent"],
    result["influencer_sync"]["installed_python_exact_match"],
])
result["status"] = "READBACK_OK" if sync_ok and camo["exact_baseline_match"] else "READBACK_INCOMPLETE"

out = state_dir / "backlog_024_camofox_local_manifest.json"
atomic_json(out, result)
print(f"MANIFEST_WRITTEN={out}")
print(f"STATUS={result['status']}")
print(f"INFLUENCER_SYNC_OK={str(sync_ok).lower()}")
print(f"CAMOFOX_BASELINE_MATCH={str(camo['exact_baseline_match']).lower()}")
print("NO_CAMOFOX_BROWSER_NPM_INSTALL_OR_NPX_EXECUTED")
