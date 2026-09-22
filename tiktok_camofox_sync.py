from __future__ import annotations

import argparse
import atexit
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import shutil
import subprocess
import sys
import time
import tempfile
import threading
import tarfile
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


APP_VERSION = "0.8.7"
CAMOFOX_FALLBACK_MAX_MEDIA_BYTES = 512 * 1024 * 1024
CAMOFOX_FALLBACK_DURATION_TOLERANCE_S = 2.0
CAMOFOX_FALLBACK_TOTAL_TIMEOUT_SECONDS = 300.0
CAMOFOX_FALLBACK_CLEANUP_RESERVE_SECONDS = 8.0
CAMOFOX_FALLBACK_DOWNLOAD_WAIT_SECONDS = 20.0
CAMOFOX_FALLBACK_DISK_MARGIN_BYTES = 512 * 1024 * 1024
CAMOFOX_FALLBACK_EXPECTED_NODE_VERSION = "v22.23.2"
CAMOFOX_FALLBACK_EXPECTED_CAMOFOX_VERSION = "1.13.1"
CAMOFOX_FALLBACK_EXPECTED_CAMOUFOX_JS_VERSION = "0.11.5"
CAMOFOX_FALLBACK_EXPECTED_GIT_BLOBS = {
    "package.json": "64caef26cda2707567e6f7eba1b9f5f429493990",
    "camofox.config.json": "a789c84806d13e6a977c8984a3a562f5cac44b5b",
    "server.js": "06443a7193aa494c2f4e1f717648444bf87bc970",
    "lib/auth.js": "cc881a14207191ef84382cf940b75bc3e9e8100d",
    "lib/config.js": "7fd9438effa91000182f70936af9763ede035a2d",
    "lib/downloads.js": "e0b8cc9a8c862016f2929f89438ff9f0f9c1517d",
    "lib/persistence.js": "c8c6ffc70bdbac6c8b453ef1c91b6fbc88aded66",
    "lib/plugins.js": "a7ee27ea565c49945c6f14890500765d21584663",
    "lib/launcher.js": "faa50e51a94abdd6c817c116b43542287ef6f039",
    "lib/camoufox-executable.js": "142f16922c6e8ad6a9b0d02488f0350009b0eeee",
    "plugins/persistence/index.js": "7c5199d3b00c39325b660699581f4298a334808e",
}
CAMOFOX_FALLBACK_SOURCE_COMMIT = "af3a2505fc3853e976ad261b2ca0cfc445054d33"
CAMOUFOX_JS_SOURCE_COMMIT = "3fe80d8448653d8dc1a2c186c7506f89e74c4ed4"
CAMOFOX_ACCEPTED_ROOT_PACKAGE_NAME = "influencerresearch-camofox-runtime"
CAMOFOX_ACCEPTED_ROOT_DEPENDENCIES = {
    "@askjo/camofox-browser": CAMOFOX_FALLBACK_EXPECTED_CAMOFOX_VERSION,
}
CAMOFOX_ACCEPTED_NPM_ARTIFACTS = {
    "@askjo/camofox-browser": {
        "version": "1.13.1",
        "resolved": "https://registry.npmjs.org/@askjo/camofox-browser/-/camofox-browser-1.13.1.tgz",
        "integrity": "sha512-qc84dCPoXVPlCjX85EmpBd9xZ/342UVDi5gOHT4V+lwwA5CxOTM1uw0uRevn+EWtztBG+tO4qMXkR+nPx5Uldw==",
        "package_rel": "@askjo/camofox-browser",
    },
    "camoufox-js": {
        "version": "0.11.5",
        "resolved": "https://registry.npmjs.org/camoufox-js/-/camoufox-js-0.11.5.tgz",
        "integrity": "sha512-N7lZazKop9V+KZNcNlCa6unhachjXOSD4ZVo8L8dQ+PULMV810/8p1F3fu8wRWX4fvcsee1kUFyXRv3GxSzg0w==",
        "package_rel": "camoufox-js",
    },
}
CAMOUFOX_BROWSER_VERSION_JSON_SHA256 = "a8ef0d628309b9f86ba9cef4daa3cf5abdf217bbb0a48949f1de57769eac130a"
CAMOUFOX_BROWSER_VERSION_FIELDS = {"version": "152.0.4", "release": "beta.28"}
CAMOUFOX_BROWSER_EXECUTABLE_SHA256 = {
    "camoufox.exe": "ca931ff2b79aa4f4c66b9fae38693eeff7f700cbcd0704f2f8c45268b447065e",
    "nmhproxy.exe": "3c57ba30fe979a979940ca10727da61cd6e5b4e6ffae6e4fd1d7a81713f1bf3b",
    "plugin-container.exe": "182f723cedcaa3ca05dee37835eb555b7ca7200afa52d8e7a266feeef1359a23",
    "private_browsing.exe": "66220b91c9181f109aef95a98d3420365f17577662ff4e02abc0f802b5de5050",
    "desktop-launcher/desktop-launcher.exe": "23f0f0b22a570ae71b5e550564a7fd87eb68b7979f0b219299a422d358e62373",
}
YT_DLP_TIKTOK_WEBPAGE_FAILURE = "Unexpected response from webpage request"

_CAMOFOX_FALLBACK_SERVER: dict[str, Any] | None = None
_CAMOFOX_FALLBACK_LOCK = threading.Lock()
_TIKTOK_RUN_LOCK_HANDLE: Any | None = None
_TIKTOK_RUN_LOCK_GUARD = threading.Lock()
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Any = None) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if default is not None:
        return default
    raise FileNotFoundError(path)


def _tiktok_run_lock_path() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch"
    local.mkdir(parents=True, exist_ok=True)
    return local / "tiktok_runtime_v1.lock"


def _lock_file_nonblocking(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: Any) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


def _acquire_tiktok_run_lock() -> None:
    global _TIKTOK_RUN_LOCK_HANDLE
    with _TIKTOK_RUN_LOCK_GUARD:
        if _TIKTOK_RUN_LOCK_HANDLE is not None:
            return
        path = _tiktok_run_lock_path()
        handle = path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\\0")
                handle.flush()
            _lock_file_nonblocking(handle)
        except Exception as exc:
            handle.close()
            raise RuntimeError(
                "Another InfluencerResearch TikTok run is already active"
            ) from exc
        _TIKTOK_RUN_LOCK_HANDLE = handle


def _release_tiktok_run_lock() -> None:
    global _TIKTOK_RUN_LOCK_HANDLE
    with _TIKTOK_RUN_LOCK_GUARD:
        handle = _TIKTOK_RUN_LOCK_HANDLE
        _TIKTOK_RUN_LOCK_HANDLE = None
        if handle is None:
            return
        try:
            _unlock_file(handle)
        finally:
            handle.close()


def request_json(method: str, path: str, body: dict | None = None, timeout: int = 30) -> Any:
    """Use the single constrained process-owned CamoFox server for discovery and media.

    The public discovery client intentionally shares the same loopback/authenticated,
    sanitized runtime boundary as the media fallback. No shared unauthenticated 9377
    listener is reused or created.
    """
    deadline = time.monotonic() + max(0.1, float(timeout))
    server = _ensure_fallback_server(deadline=deadline)
    return _fallback_request_json(
        server,
        method,
        path,
        body,
        deadline=deadline,
        timeout_cap=max(0.1, float(timeout)),
    )


def health() -> dict | None:
    server = _CAMOFOX_FALLBACK_SERVER
    if not server or server.get("proc") is None or server["proc"].poll() is not None:
        return None
    try:
        value = _fallback_health(server, deadline=time.monotonic() + 3.0)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _server_public_status(server: dict[str, Any], *, started: bool) -> dict[str, Any]:
    return {
        "started": started,
        "health": _fallback_health(server, deadline=time.monotonic() + 3.0),
        "note": "constrained_process_owned",
        "port": int(server["port"]),
        "bind_host": "127.0.0.1",
        "access_key_required": True,
        "source_commit": CAMOFOX_FALLBACK_SOURCE_COMMIT,
    }


def start_server() -> dict:
    """Start/reuse only this application's constrained process-owned CamoFox server."""
    _acquire_tiktok_run_lock()
    try:
        current = _CAMOFOX_FALLBACK_SERVER
        was_healthy = bool(
            current
            and current.get("proc") is not None
            and current["proc"].poll() is None
            and _fallback_health(current, deadline=time.monotonic() + 3.0)
        )
        server = _ensure_fallback_server(deadline=time.monotonic() + 60.0)
        return _server_public_status(server, started=not was_healthy)
    except Exception:
        _release_tiktok_run_lock()
        raise


def stop_server(*, deadline: float | None = None) -> None:
    """Idempotently stop the application-owned CamoFox server and release the run lock."""
    end = deadline if deadline is not None else time.monotonic() + 8.0
    try:
        _stop_fallback_server(force=False, deadline=end)
    finally:
        _release_tiktok_run_lock()

def flatten_video_links(obj: Any, handle: str) -> list[str]:
    """Extract exact-handle TikTok video links from CamoFox payloads.

    TikTok frequently exposes profile/search hrefs as relative paths such as
    /@handle/video/123 rather than absolute URLs. Canonicalize both forms.
    """
    exact = handle.casefold().lstrip("@")
    pattern = re.compile(
        rf'(?:https?://(?:www\.)?tiktok\.com)?/?@({re.escape(handle)})/video/(\d+)',
        re.IGNORECASE,
    )
    out: list[str] = []
    seen: set[str] = set()

    def add_text(text: str) -> None:
        normalized = text.replace("\\/", "/")
        for match in pattern.finditer(normalized):
            if match.group(1).casefold() != exact:
                continue
            url = f"https://www.tiktok.com/@{handle}/video/{match.group(2)}"
            if url not in seen:
                seen.add(url)
                out.append(url)

    def walk(value: Any) -> None:
        if isinstance(value, str):
            add_text(value)
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(obj)
    return out


def collect_video_urls(
    tab_id: str,
    *,
    user_id: str,
    handle: str,
    target: int,
    max_scrolls: int = 120,
) -> tuple[list[str], dict]:
    found: list[str] = []
    seen: set[str] = set()
    stagnant = 0
    rounds = 0
    links_endpoint_errors = 0

    for round_idx in range(max_scrolls + 1):
        rounds += 1
        before = len(found)

        snap = request_json(
            "GET",
            f"/tabs/{urllib.parse.quote(tab_id)}/snapshot?"
            + urllib.parse.urlencode({"userId": user_id, "format": "text"}),
            timeout=30,
        )
        candidates = flatten_video_links(snap, handle)

        try:
            links = request_json(
                "GET",
                f"/tabs/{urllib.parse.quote(tab_id)}/links?"
                + urllib.parse.urlencode({"userId": user_id, "limit": 250}),
                timeout=20,
            )
            candidates.extend(flatten_video_links(links, handle))
        except Exception:
            links_endpoint_errors += 1

        for url in candidates:
            clean = url.split("?")[0].rstrip("/")
            if clean not in seen:
                seen.add(clean)
                found.append(clean)

        if len(found) >= target:
            break

        if len(found) == before:
            stagnant += 1
        else:
            stagnant = 0

        if stagnant >= 5:
            break

        request_json(
            "POST",
            f"/tabs/{urllib.parse.quote(tab_id)}/scroll",
            {"userId": user_id, "direction": "down", "amount": 1200},
            timeout=20,
        )
        time.sleep(1.0)

    return found[:target], {
        "target": target,
        "found": len(found[:target]),
        "rounds": rounds,
        "stagnant_rounds_at_end": stagnant,
        "links_endpoint_errors": links_endpoint_errors,
    }


def video_id_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def validate_media(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    size = path.stat().st_size
    if size < 50_000:
        return False, f"too_small:{size}"
    try:
        import av
        with av.open(str(path)) as container:
            videos = [s for s in container.streams if s.type == "video"]
            if not videos:
                return False, "no_video_stream"
            return True, f"ok:size={size}:duration={container.duration}"
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc}"


def published_iso(info: dict) -> str | None:
    ts = info.get("timestamp")
    if ts is not None:
        try:
            return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
        except Exception:
            pass
    upload_date = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", upload_date):
        try:
            return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return None


def adopt_poc_file(root: Path, video_id: str, video_dir: Path) -> tuple[Path | None, Path | None]:
    poc_dir = root / "output" / "nicholas_crown" / "tiktok" / "camofox_individual"
    src_mp4 = poc_dir / f"{video_id}.mp4"
    src_info = poc_dir / f"{video_id}.info.json"
    if not src_mp4.exists():
        return None, None

    video_dir.mkdir(parents=True, exist_ok=True)
    dst_mp4 = video_dir / src_mp4.name
    dst_info = video_dir / src_info.name

    if not dst_mp4.exists():
        shutil.copy2(src_mp4, dst_mp4)
    if src_info.exists() and not dst_info.exists():
        shutil.copy2(src_info, dst_info)
    return dst_mp4, dst_info if dst_info.exists() else None



def _git_blob_sha1(path: Path) -> str:
    raw = path.read_bytes()
    prefix = f"blob {len(raw)}\0".encode("ascii")
    return hashlib.sha1(prefix + raw).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _npm_cache_candidates() -> list[Path]:
    out: list[Path] = []
    explicit = os.environ.get("npm_config_cache") or os.environ.get("NPM_CONFIG_CACHE")
    if explicit:
        out.append(Path(explicit))
    localapp = os.environ.get("LOCALAPPDATA")
    if localapp:
        out.append(Path(localapp) / "npm-cache")
    home = Path.home()
    out.append(home / "AppData" / "Local" / "npm-cache")
    out.append(home / ".npm")
    unique: list[Path] = []
    seen: set[str] = set()
    for path in out:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _npm_cached_tarball(integrity: str) -> Path:
    algorithm, encoded = integrity.split("-", 1)
    if algorithm.lower() != "sha512":
        raise RuntimeError(f"Unsupported npm integrity algorithm: {algorithm}")
    expected = base64.b64decode(encoded, validate=True)
    hex_digest = expected.hex()
    for cache in _npm_cache_candidates():
        path = (
            cache / "_cacache" / "content-v2" / "sha512"
            / hex_digest[:2] / hex_digest[2:4] / hex_digest[4:]
        )
        if not path.is_file():
            continue
        actual = hashlib.sha512(path.read_bytes()).digest()
        if actual != expected:
            raise RuntimeError(f"npm cache integrity mismatch: {path}")
        return path
    raise RuntimeError("Accepted npm artifact is not present in the local content-addressable cache")


def _tarball_file_hashes(tarball: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with tarfile.open(tarball, mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != "package" or len(parts) == 1:
                raise RuntimeError(f"Unexpected npm tar member: {member.name}")
            rel = "/".join(parts[1:])
            if member.issym() or member.islnk():
                raise RuntimeError(f"Symlink/hardlink not accepted in npm artifact: {rel}")
            if not member.isfile():
                raise RuntimeError(f"Unexpected npm artifact member type: {rel}")
            handle = archive.extractfile(member)
            if handle is None:
                raise RuntimeError(f"Unable to read npm artifact member: {rel}")
            out[rel] = hashlib.sha256(handle.read()).hexdigest()
    return out


def _installed_package_file_hashes(package_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in package_root.rglob("*"):
        rel_path = path.relative_to(package_root)
        if rel_path.parts and rel_path.parts[0] == "node_modules":
            continue
        rel = rel_path.as_posix()
        if path.is_symlink():
            raise RuntimeError(f"Unexpected symlink in installed npm artifact: {rel}")
        if path.is_file():
            out[rel] = _sha256_file(path)
    return out


def _verify_installed_npm_artifact(local: Path, name: str, spec: dict[str, str]) -> dict[str, Any]:
    lock = json.loads((local / "package-lock.json").read_text(encoding="utf-8"))
    lock_key = "node_modules/" + str(spec["package_rel"])
    entry = (lock.get("packages") or {}).get(lock_key)
    if not isinstance(entry, dict):
        raise RuntimeError(f"Missing accepted lock entry for {name}")
    for field in ("version", "resolved", "integrity"):
        if entry.get(field) != spec[field]:
            raise RuntimeError(f"Lock provenance mismatch for {name}.{field}")

    tarball = _npm_cached_tarball(str(spec["integrity"]))
    expected_tree = _tarball_file_hashes(tarball)
    package_root = local / "node_modules" / Path(str(spec["package_rel"]))
    if not package_root.is_dir():
        raise RuntimeError(f"Installed npm package missing: {name}")
    installed_tree = _installed_package_file_hashes(package_root)
    if installed_tree != expected_tree:
        missing = sorted(set(expected_tree) - set(installed_tree))[:5]
        extra = sorted(set(installed_tree) - set(expected_tree))[:5]
        changed = sorted(
            key for key in set(expected_tree) & set(installed_tree)
            if expected_tree[key] != installed_tree[key]
        )[:5]
        raise RuntimeError(
            f"Installed npm artifact tree mismatch for {name}: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    return {
        "version": spec["version"],
        "integrity": spec["integrity"],
        "file_count": len(expected_tree),
        "cache_tarball_sha512": hashlib.sha512(tarball.read_bytes()).hexdigest(),
    }


def _verify_camoufox_browser_cache() -> dict[str, Any]:
    localapp = os.environ.get("LOCALAPPDATA")
    if not localapp:
        raise RuntimeError("LOCALAPPDATA unavailable for accepted Camoufox browser baseline")
    cache = Path(localapp) / "camoufox" / "camoufox" / "Cache"
    version_path = cache / "version.json"
    if not version_path.is_file():
        raise RuntimeError(f"Camoufox version.json missing at {version_path}")
    if _sha256_file(version_path) != CAMOUFOX_BROWSER_VERSION_JSON_SHA256:
        raise RuntimeError("Camoufox version.json hash mismatch")
    version_obj = json.loads(version_path.read_text(encoding="utf-8"))
    for key, expected in CAMOUFOX_BROWSER_VERSION_FIELDS.items():
        if str(version_obj.get(key)) != expected:
            raise RuntimeError(f"Camoufox browser {key} mismatch: {version_obj.get(key)!r}")
    verified: dict[str, str] = {}
    for relative, expected in CAMOUFOX_BROWSER_EXECUTABLE_SHA256.items():
        path = cache / Path(relative)
        if not path.is_file():
            raise RuntimeError(f"Camoufox browser executable missing: {relative}")
        actual = _sha256_file(path)
        if actual != expected:
            raise RuntimeError(f"Camoufox browser executable hash mismatch: {relative}")
        verified[relative] = actual
    return {"version": dict(CAMOUFOX_BROWSER_VERSION_FIELDS), "executables": verified}


def _verify_camofox_root_manifests(local: Path) -> dict[str, Any]:
    package_path = local / "package.json"
    lock_path = local / "package-lock.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8-sig"))
        lock = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read CamoFox runtime manifests: {exc}") from exc

    if not isinstance(package, dict) or not isinstance(lock, dict):
        raise RuntimeError("CamoFox runtime manifests must be JSON objects")
    if package.get("name") != CAMOFOX_ACCEPTED_ROOT_PACKAGE_NAME:
        raise RuntimeError(f"Unexpected CamoFox root package name: {package.get('name')!r}")
    if package.get("private") is not True:
        raise RuntimeError("CamoFox root package must remain private")
    if package.get("dependencies") != CAMOFOX_ACCEPTED_ROOT_DEPENDENCIES:
        raise RuntimeError("CamoFox root dependencies differ from accepted baseline")
    if lock.get("lockfileVersion") != 3:
        raise RuntimeError(f"Unexpected CamoFox package-lock version: {lock.get('lockfileVersion')!r}")

    root_entry = (lock.get("packages") or {}).get("")
    if not isinstance(root_entry, dict):
        raise RuntimeError("CamoFox package-lock root entry missing")
    if root_entry.get("name") != CAMOFOX_ACCEPTED_ROOT_PACKAGE_NAME:
        raise RuntimeError(f"Unexpected CamoFox package-lock root name: {root_entry.get('name')!r}")
    if root_entry.get("dependencies") != CAMOFOX_ACCEPTED_ROOT_DEPENDENCIES:
        raise RuntimeError("CamoFox package-lock root dependencies differ from accepted baseline")

    return {
        "package_name": CAMOFOX_ACCEPTED_ROOT_PACKAGE_NAME,
        "dependencies": dict(CAMOFOX_ACCEPTED_ROOT_DEPENDENCIES),
        "lockfile_version": 3,
    }


def _verify_fallback_camofox_runtime(local: Path) -> dict[str, Any]:
    manifests = _verify_camofox_root_manifests(local)

    package_root = local / "node_modules" / "@askjo" / "camofox-browser"
    if not package_root.is_dir():
        raise RuntimeError(f"CamoFox package missing at {package_root}")

    # Retain exact reviewed-source anchors while the full installed npm artifacts
    # are independently matched byte-for-byte to their accepted cached tarballs.
    source_anchors: dict[str, str] = {}
    for relative, expected_blob in CAMOFOX_FALLBACK_EXPECTED_GIT_BLOBS.items():
        path = package_root / relative
        if not path.is_file():
            raise RuntimeError(f"CamoFox provenance file missing: {relative}")
        actual_blob = _git_blob_sha1(path)
        if actual_blob != expected_blob:
            raise RuntimeError(f"CamoFox source provenance mismatch for {relative}: {actual_blob}")
        source_anchors[relative] = actual_blob

    npm_artifacts = {
        name: _verify_installed_npm_artifact(local, name, spec)
        for name, spec in CAMOFOX_ACCEPTED_NPM_ARTIFACTS.items()
    }
    browser = _verify_camoufox_browser_cache()

    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node executable not found for CamoFox runtime")
    node_version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if node_version.returncode != 0:
        raise RuntimeError("Unable to verify Node runtime for CamoFox")
    actual_node_version = (node_version.stdout or "").strip()
    if actual_node_version != CAMOFOX_FALLBACK_EXPECTED_NODE_VERSION:
        raise RuntimeError(f"Unexpected Node runtime: {actual_node_version!r}")

    return {
        "node_version": actual_node_version,
        "source_commit": CAMOFOX_FALLBACK_SOURCE_COMMIT,
        "camoufox_js_source_commit": CAMOUFOX_JS_SOURCE_COMMIT,
        "source_anchors": source_anchors,
        "npm_artifacts": npm_artifacts,
        "browser": browser,
        "manifests": manifests,
    }


def _remaining_timeout(deadline: float, cap: float | None = None) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("CamoFox fallback total per-video time budget exhausted")
    if cap is not None:
        remaining = min(remaining, cap)
    if remaining <= 0:
        raise TimeoutError("CamoFox fallback total per-video time budget exhausted")
    return remaining


def _sleep_with_deadline(deadline: float, seconds: float) -> None:
    remaining = _remaining_timeout(deadline)
    time.sleep(min(seconds, remaining))
    _remaining_timeout(deadline)


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _safe_fallback_server_env(
    *,
    root: Path,
    port: int,
    access_key: str,
    admin_key: str,
) -> dict[str, str]:
    allowed_parent = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
        "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "LOCALAPPDATA",
        "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in allowed_parent and value
    }

    tmp_dir = root / "tmp"
    profile_dir = root / "profiles"
    cookies_dir = root / "cookies-empty"
    traces_dir = root / "traces"
    for path in (tmp_dir, profile_dir, cookies_dir, traces_dir):
        path.mkdir(parents=True, exist_ok=True)

    env.update({
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "CAMOFOX_PORT": str(port),
        "CAMOFOX_BIND_HOST": "127.0.0.1",
        "CAMOFOX_ACCESS_KEY": access_key,
        "CAMOFOX_ADMIN_KEY": admin_key,
        "CAMOFOX_PROFILE_DIR": str(profile_dir),
        "CAMOFOX_COOKIES_DIR": str(cookies_dir),
        "CAMOFOX_TRACES_DIR": str(traces_dir),
        "CAMOFOX_CRASH_REPORT_ENABLED": "false",
        "CAMOFOX_CRASH_REPORT_URL": "",
        "SENTRY_DSN": "",
        "PROMETHEUS_ENABLED": "false",
        "NODE_ENV": "production",
        "BROWSER_IDLE_TIMEOUT_MS": "60000",
        "SESSION_TIMEOUT_MS": "60000",
        "TAB_INACTIVITY_MS": "60000",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    banned_upper = {
        "CAMOUFOX_EXECUTABLE", "CAMOUFOX_EXECUTABLE_PATH", "CAMOFOX_EXECUTABLE_PATH",
        "PROXY_STRATEGY", "PROXY_PROVIDER", "PROXY_HOST", "PROXY_PORT", "PROXY_PORTS",
        "PROXY_USERNAME", "PROXY_PASSWORD", "PROXY_BACKCONNECT_HOST",
        "PROXY_BACKCONNECT_PORT", "PROXY_COUNTRY", "PROXY_STATE", "PROXY_CITY",
        "PROXY_ZIP", "PROXY_SESSION_DURATION_MINUTES", "HTTP_PROXY", "HTTPS_PROXY",
        "ALL_PROXY", "GITHUB_TOKEN",
    }
    for key in list(env):
        if key.upper() in banned_upper:
            env.pop(key, None)
    return env


def _fallback_request_json(
    server: dict[str, Any],
    method: str,
    path: str,
    body: dict | None = None,
    *,
    deadline: float,
    timeout_cap: float = 10.0,
    token: str | None = None,
) -> Any:
    timeout = _remaining_timeout(deadline, timeout_cap)
    url = str(server["base_url"]) + path
    payload = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token or server['access_key']}",
    }
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, method=method, headers=headers)
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype or raw[:1] in (b"{", b"["):
                return json.loads(raw.decode("utf-8", errors="replace"))
            return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"CamoFox HTTP {exc.code} {path}: {detail[:1000]}") from exc
    except (TimeoutError, socket.timeout) as exc:
        # Do not create a new teardown budget here. The owning operation catches
        # TimeoutError and tears the process down against its original hard deadline.
        raise TimeoutError("CamoFox fallback REST request timed out") from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            raise TimeoutError("CamoFox fallback REST request timed out") from exc
        raise


def _fallback_health(server: dict[str, Any], *, deadline: float) -> dict | None:
    try:
        return _fallback_request_json(
            server, "GET", "/health", deadline=deadline, timeout_cap=2.0
        )
    except Exception:
        return None


def _is_windows_runtime() -> bool:
    return os.name == "nt"


def _taskkill_owned_process(proc: subprocess.Popen[Any], timeout: float) -> None:
    if proc.poll() is not None:
        return
    if timeout <= 0:
        try:
            proc.kill()
        except Exception:
            pass
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            proc.kill()
        except Exception:
            pass


def _remove_owned_root_strict(root: Path, *, deadline: float) -> None:
    last_error: Exception | None = None
    while root.exists():
        _remaining_timeout(deadline)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import shutil,sys; shutil.rmtree(sys.argv[1])",
                str(root),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=_safe_local_worker_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            _, stderr = proc.communicate(timeout=_remaining_timeout(deadline))
        except subprocess.TimeoutExpired as exc:
            _taskkill_owned_process(proc, deadline - time.monotonic())
            raise TimeoutError("CamoFox owned-root cleanup exceeded deadline") from exc
        if proc.returncode == 0 and not root.exists():
            return
        last_error = RuntimeError((stderr or "path still exists").strip()[-1000:])
        if time.monotonic() >= deadline:
            break
        _sleep_with_deadline(deadline, min(0.1, max(0.01, deadline - time.monotonic())))
    detail = f"{type(last_error).__name__}: {last_error}" if last_error else "path still exists"
    raise RuntimeError(f"CamoFox fallback owned temp root remains after teardown: {detail}") from last_error


def _stop_fallback_server(*, force: bool = False, deadline: float | None = None) -> None:
    global _CAMOFOX_FALLBACK_SERVER
    server = _CAMOFOX_FALLBACK_SERVER
    _CAMOFOX_FALLBACK_SERVER = None
    if not server:
        return

    proc = server.get("proc")
    root = Path(server["root"])
    hard_end = deadline if deadline is not None else time.monotonic() + 8.0

    # On Windows, kill the dedicated Node process tree while the parent PID still
    # owns its Camoufox children. A graceful /stop can let the Node parent exit
    # before descendant browser processes release handles under the isolated root.
    if _is_windows_runtime() and proc is not None and proc.poll() is None:
        _taskkill_owned_process(proc, hard_end - time.monotonic())
    else:
        if proc is not None and proc.poll() is None and not force:
            try:
                _fallback_request_json(
                    server,
                    "POST",
                    "/stop",
                    deadline=hard_end,
                    timeout_cap=2.0,
                    token=server["admin_key"],
                )
            except Exception:
                pass
        if proc is not None and proc.poll() is None:
            remaining = min(2.0, hard_end - time.monotonic())
            if remaining > 0:
                try:
                    proc.wait(timeout=remaining)
                except Exception:
                    _taskkill_owned_process(proc, hard_end - time.monotonic())
            else:
                _taskkill_owned_process(proc, 0.0)

    log_handle = server.get("log_handle")
    try:
        if log_handle:
            log_handle.close()
    except Exception:
        pass

    _remove_owned_root_strict(root, deadline=hard_end)

def _atexit_stop_server() -> None:
    try:
        _stop_fallback_server(force=True, deadline=time.monotonic() + 8.0)
    except Exception:
        pass
    finally:
        _release_tiktok_run_lock()


atexit.register(_atexit_stop_server)



def _ensure_fallback_server(*, deadline: float) -> dict[str, Any]:
    global _CAMOFOX_FALLBACK_SERVER
    current = _CAMOFOX_FALLBACK_SERVER
    if (
        current
        and current["proc"].poll() is None
        and _fallback_health(current, deadline=deadline)
    ):
        return current
    if current:
        _stop_fallback_server(force=True, deadline=deadline)

    local = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        / "InstagramResearch"
        / "camofox-poc"
    )
    provenance = _verify_fallback_camofox_runtime(local)
    package_root = local / "node_modules" / "@askjo" / "camofox-browser"
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node executable not found for CamoFox fallback")

    root = Path(tempfile.mkdtemp(prefix="InstagramResearch-B042-camofox-fallback-"))
    port = _allocate_loopback_port()
    access_key = secrets.token_urlsafe(32)
    admin_key = secrets.token_urlsafe(32)
    env = _safe_fallback_server_env(
        root=root, port=port, access_key=access_key, admin_key=admin_key
    )
    log_path = root / "camofox-fallback.log"
    log_handle = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        [node, str(package_root / "server.js")],
        cwd=str(package_root),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    server: dict[str, Any] = {
        "proc": proc,
        "root": root,
        "tmp_dir": root / "tmp",
        "profile_dir": root / "profiles",
        "cookies_dir": root / "cookies-empty",
        "base_url": f"http://127.0.0.1:{port}",
        "port": port,
        "access_key": access_key,
        "admin_key": admin_key,
        "log_handle": log_handle,
        "provenance": provenance,
    }
    _CAMOFOX_FALLBACK_SERVER = server

    try:
        while _remaining_timeout(deadline) > 0:
            if proc.poll() is not None:
                raise RuntimeError(
                    "Constrained CamoFox fallback server exited during startup"
                )
            if _fallback_health(server, deadline=deadline):
                break
            _sleep_with_deadline(deadline, 0.2)

        unauth = urllib.request.Request(
            f"{server['base_url']}/tabs?userId=b042-auth-probe",
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with _NO_PROXY_OPENER.open(
                unauth, timeout=_remaining_timeout(deadline, 2.0)
            ):
                raise RuntimeError(
                    "CamoFox fallback access-key gate is not enforced"
                )
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise RuntimeError(
                    f"Unexpected unauthenticated CamoFox status: {exc.code}"
                ) from exc

        _fallback_request_json(
            server,
            "GET",
            "/tabs?userId=b042-auth-probe",
            deadline=deadline,
            timeout_cap=2.0,
        )
        return server
    except Exception:
        _stop_fallback_server(force=True, deadline=deadline)
        raise


def _fallback_user_profile_dir(server: dict[str, Any], user_id: str) -> Path:
    digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:32]
    return Path(server["profile_dir"]) / digest


def _cleanup_fallback_session(
    server: dict[str, Any],
    *,
    user_id: str,
    deadline: float,
) -> str:
    try:
        _fallback_request_json(
            server,
            "DELETE",
            f"/sessions/{urllib.parse.quote(user_id)}/storage_state",
            deadline=deadline,
            timeout_cap=3.0,
        )
        user_dir = _fallback_user_profile_dir(server, user_id)
        _remove_owned_root_strict(user_dir, deadline=deadline)
        if user_dir.exists():
            raise RuntimeError(
                "CamoFox fallback profile artifact remains after storage reset"
            )
        stale_downloads = list(
            Path(server["tmp_dir"]).glob("camofox-download-*")
        )
        if stale_downloads:
            raise RuntimeError(
                "CamoFox fallback download artifacts remain after storage reset"
            )
        return "storage_reset"
    except Exception as exc:
        root = Path(server["root"])
        _stop_fallback_server(force=True, deadline=deadline)
        if root.exists():
            raise RuntimeError(
                f"CamoFox fallback cleanup failed: {type(exc).__name__}: {exc}"
            ) from exc
        return "server_teardown"


def _check_fallback_disk_floor(video_dir: Path, server: dict[str, Any]) -> None:
    video_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(server["tmp_dir"])
    per_volume = (
        CAMOFOX_FALLBACK_MAX_MEDIA_BYTES + CAMOFOX_FALLBACK_DISK_MARGIN_BYTES
    )
    same_volume = os.stat(video_dir).st_dev == os.stat(tmp_dir).st_dev
    if same_volume:
        required = (
            (2 * CAMOFOX_FALLBACK_MAX_MEDIA_BYTES)
            + CAMOFOX_FALLBACK_DISK_MARGIN_BYTES
        )
        free = shutil.disk_usage(video_dir).free
        if free < required:
            raise RuntimeError(
                f"Insufficient free disk for TikTok fallback: "
                f"free={free} required={required}"
            )
        return
    for path in (video_dir, tmp_dir):
        free = shutil.disk_usage(path).free
        if free < per_volume:
            raise RuntimeError(
                f"Insufficient free disk for TikTok fallback on "
                f"{path.anchor or path}: free={free} required={per_volume}"
            )


def _clear_camofox_downloads(
    server: dict[str, Any],
    tab_id: str,
    *,
    user_id: str,
    deadline: float,
) -> None:
    _camofox_downloads(
        server,
        tab_id,
        user_id=user_id,
        consume=True,
        deadline=deadline,
    )
    remaining = _camofox_downloads(
        server,
        tab_id,
        user_id=user_id,
        consume=False,
        deadline=deadline,
    )
    if remaining:
        raise RuntimeError("CamoFox download pre-clear could not be proven")


def _tiktok_video_identity(url: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"https://www\.tiktok\.com/@([A-Za-z0-9._]+)/video/(\d+)",
        url.strip().rstrip("/"),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1), match.group(2)


def _should_use_camofox_fallback(url: str, diagnostic: str) -> bool:
    return bool(
        _tiktok_video_identity(url)
        and YT_DLP_TIKTOK_WEBPAGE_FAILURE in (diagnostic or "")
    )


def _local_media_stage_command(source: Path, destination: Path) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_b042-stage-media",
        str(source),
        str(destination),
    ]


def _safe_local_worker_env() -> dict[str, str]:
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
        "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "LOCALAPPDATA",
        "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    }
    return {k: v for k, v in os.environ.items() if k.upper() in allowed and v}


def _bounded_local_media_stage(
    source: Path,
    destination: Path,
    *,
    deadline: float,
    cleanup_deadline: float | None = None,
) -> tuple[bool, str, float | None]:
    timeout = _remaining_timeout(deadline)
    cleanup_end = cleanup_deadline if cleanup_deadline is not None else deadline
    proc = subprocess.Popen(
        _local_media_stage_command(source, destination),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_safe_local_worker_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _taskkill_owned_process(proc, cleanup_end - time.monotonic())
        try:
            remaining = cleanup_end - time.monotonic()
            if remaining > 0:
                proc.communicate(timeout=min(0.5, remaining))
            else:
                proc.communicate(timeout=0)
        except Exception:
            pass
        raise TimeoutError("CamoFox fallback local copy/media validation exceeded per-video deadline") from exc
    _remaining_timeout(deadline)
    if proc.returncode != 0:
        raise RuntimeError(f"Local media stage failed: {(stderr or stdout or '').strip()[-1000:]}")
    try:
        payload = json.loads((stdout or "").strip().splitlines()[-1])
    except Exception as exc:
        raise RuntimeError("Local media stage returned invalid JSON") from exc
    return bool(payload.get("valid")), str(payload.get("validation") or ""), payload.get("duration")


def _run_local_media_stage_cli(source: Path, destination: Path) -> int:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        valid, validation = validate_media(destination)
        duration = _media_duration_seconds(destination) if valid else None
        print(json.dumps({"valid": valid, "validation": validation, "duration": duration}))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _media_duration_seconds(path: Path) -> float | None:
    try:
        import av
        with av.open(str(path)) as container:
            if container.duration is None:
                return None
            return float(container.duration) / 1_000_000.0
    except Exception:
        return None


def _camofox_downloads(
    server: dict[str, Any],
    tab_id: str,
    *,
    user_id: str,
    consume: bool,
    deadline: float,
) -> list[dict]:
    payload = _fallback_request_json(
        server,
        "GET",
        f"/tabs/{urllib.parse.quote(tab_id)}/downloads?"
        + urllib.parse.urlencode({
            "userId": user_id,
            "includeData": "false",
            "consume": "true" if consume else "false",
            "maxBytes": str(CAMOFOX_FALLBACK_MAX_MEDIA_BYTES),
        }),
        deadline=deadline,
        timeout_cap=5.0,
    )
    if not isinstance(payload, dict):
        return []
    downloads = payload.get("downloads")
    return downloads if isinstance(downloads, list) else []


def _camofox_local_download_path(
    item: dict,
    *,
    temp_root: Path,
    expected_filename: str,
) -> Path:
    raw_id = str(item.get("id") or "").strip()
    try:
        download_id = str(uuid.UUID(raw_id))
    except Exception as exc:
        raise RuntimeError("CamoFox download record has invalid download id") from exc
    if download_id.casefold() != raw_id.casefold():
        raise RuntimeError("CamoFox download id is not canonical UUID text")
    if str(item.get("suggestedFilename") or "") != expected_filename:
        raise RuntimeError(
            "CamoFox download filename does not match current candidate"
        )

    reported_bytes = item.get("bytes")
    if not isinstance(reported_bytes, int) or reported_bytes < 50_000:
        raise RuntimeError(f"CamoFox download size is invalid: {reported_bytes!r}")
    if reported_bytes > CAMOFOX_FALLBACK_MAX_MEDIA_BYTES:
        raise RuntimeError(
            f"CamoFox download exceeds per-file safety cap: {reported_bytes} bytes"
        )

    temp_root = temp_root.resolve()
    matches = list(
        temp_root.glob(f"camofox-download-{download_id}-{expected_filename}")
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one current CamoFox temp file for "
            f"{download_id}, found {len(matches)}"
        )
    local_path = matches[0].resolve()
    if local_path.parent != temp_root:
        raise RuntimeError(
            "Resolved CamoFox temp file escaped isolated temp directory"
        )
    if not local_path.is_file():
        raise RuntimeError("Resolved CamoFox download is not a regular file")
    actual_bytes = local_path.stat().st_size
    if actual_bytes != reported_bytes:
        raise RuntimeError(
            f"CamoFox download size mismatch: api={reported_bytes} "
            f"local={actual_bytes}"
        )
    return local_path


def _download_one_via_camofox(url: str, video_dir: Path) -> dict:
    identity = _tiktok_video_identity(url)
    if not identity:
        return {
            "video_id": video_id_from_url(url),
            "url": url,
            "ok": False,
            "source": "network",
            "transport": "camofox_browser_disk_handoff_v52",
            "media_file": None,
            "info_file": None,
            "validation": "invalid_tiktok_video_url",
            "returncode": 1,
            "diagnostic_tail": (
                "CamoFox fallback rejected non-canonical TikTok video URL."
            ),
        }

    handle, vid = identity
    video_dir.mkdir(parents=True, exist_ok=True)
    mp4 = video_dir / f"{vid}.mp4"
    temp_path = video_dir / f".{vid}.camofox-fallback.tmp.mp4"
    user_id = f"instagramresearch-tiktok-media-{os.getpid()}-{time.time_ns()}"
    session_key = f"media-{vid}"
    attempts: list[str] = []
    success: dict[str, Any] | None = None
    failure: Exception | None = None
    cleanup_mode: str | None = None

    with _CAMOFOX_FALLBACK_LOCK:
        hard_deadline = (
            time.monotonic() + CAMOFOX_FALLBACK_TOTAL_TIMEOUT_SECONDS
        )
        operation_deadline = (
            hard_deadline - CAMOFOX_FALLBACK_CLEANUP_RESERVE_SECONDS
        )
        server: dict[str, Any] | None = None
        try:
            server = _ensure_fallback_server(deadline=operation_deadline)
            _check_fallback_disk_floor(video_dir, server)
            tab = _fallback_request_json(
                server,
                "POST",
                "/tabs",
                {
                    "userId": user_id,
                    "sessionKey": session_key,
                    "url": url,
                    "trace": False,
                },
                deadline=operation_deadline,
                timeout_cap=30.0,
            )
            if not isinstance(tab, dict) or not tab.get("tabId"):
                raise RuntimeError("CamoFox create-tab response missing tabId")
            tab_id = str(tab["tabId"])
            _sleep_with_deadline(operation_deadline, 4.0)

            inspect_expression = r'''(async () => {
              const v = document.querySelector('video');
              if (v) { try { await v.play(); } catch {} }
              await new Promise(r => setTimeout(r, 3500));
              const canonical =
                document.querySelector('link[rel="canonical"]')?.href ||
                location.href;
              const allowedHost = (hostname) =>
                hostname === 'www.tiktok.com' ||
                hostname.endsWith('.tiktok.com') ||
                hostname.endsWith('.tiktokcdn-eu.com');
              const resources = performance.getEntriesByType('resource')
                .map(e => e.name)
                .filter(n => {
                  try {
                    const u = new URL(n);
                    return u.protocol === 'https:' &&
                      allowedHost(u.hostname) &&
                      (
                        u.searchParams.get('mime_type') === 'video_mp4' ||
                        /v\d+-webapp/i.test(u.hostname)
                      );
                  } catch { return false; }
                })
                .filter((n, i, a) => a.indexOf(n) === i);
              return {
                canonical,
                candidateCount: resources.length,
                video: v ? {
                  readyState: v.readyState,
                  duration: Number.isFinite(v.duration) ? v.duration : null,
                  videoWidth: v.videoWidth,
                  videoHeight: v.videoHeight
                } : null
              };
            })()'''
            inspected = _fallback_request_json(
                server,
                "POST",
                f"/tabs/{urllib.parse.quote(tab_id)}/evaluate",
                {"userId": user_id, "expression": inspect_expression},
                deadline=operation_deadline,
                timeout_cap=15.0,
            )
            state = (
                inspected.get("result")
                if isinstance(inspected, dict)
                else None
            )
            if not isinstance(state, dict):
                raise RuntimeError("CamoFox evaluate response missing result")

            canonical = str(state.get("canonical") or "")
            canonical_identity = _tiktok_video_identity(canonical)
            if (
                not canonical_identity
                or canonical_identity[0].casefold() != handle.casefold()
                or canonical_identity[1] != vid
            ):
                raise RuntimeError("CamoFox canonical identity mismatch")

            video = (
                state.get("video")
                if isinstance(state.get("video"), dict)
                else {}
            )
            if video.get("readyState") != 4:
                raise RuntimeError(
                    f"CamoFox video not ready: "
                    f"readyState={video.get('readyState')}"
                )
            main_duration = video.get("duration")
            if main_duration is None:
                raise RuntimeError("CamoFox video duration unavailable")
            main_duration = float(main_duration)

            candidate_count = int(state.get("candidateCount") or 0)
            if candidate_count <= 0:
                raise RuntimeError(
                    "CamoFox found no allowlisted TikTok MP4 candidates"
                )

            for candidate_index in range(candidate_count):
                _remaining_timeout(operation_deadline)
                _check_fallback_disk_floor(video_dir, server)
                _clear_camofox_downloads(
                    server,
                    tab_id,
                    user_id=user_id,
                    deadline=operation_deadline,
                )

                request_budget_ms = max(
                    1000,
                    int(
                        min(
                            60.0,
                            max(
                                1.0,
                                _remaining_timeout(operation_deadline) - 2.0,
                            ),
                        )
                        * 1000
                    ),
                )
                fetch_expression = r'''(async () => {
                  const idx = __INDEX__;
                  const expectedId = __EXPECTED_ID__;
                  const maxBytes = __MAX_BYTES__;
                  const requestTimeoutMs = __REQUEST_TIMEOUT_MS__;
                  const allowedHost = (hostname) =>
                    hostname === 'www.tiktok.com' ||
                    hostname.endsWith('.tiktok.com') ||
                    hostname.endsWith('.tiktokcdn-eu.com');
                  const resources = performance.getEntriesByType('resource')
                    .map(e => e.name)
                    .filter(n => {
                      try {
                        const u = new URL(n);
                        return u.protocol === 'https:' &&
                          allowedHost(u.hostname) &&
                          (
                            u.searchParams.get('mime_type') === 'video_mp4' ||
                            /v\d+-webapp/i.test(u.hostname)
                          );
                      } catch { return false; }
                    })
                    .filter((n, i, a) => a.indexOf(n) === i);
                  const raw = resources[idx];
                  if (!raw) {
                    return { ok: false, error: 'candidate_missing' };
                  }
                  const rawUrl = new URL(raw);
                  if (
                    rawUrl.protocol !== 'https:' ||
                    !allowedHost(rawUrl.hostname)
                  ) {
                    return {
                      ok: false,
                      error: 'candidate_host_rejected'
                    };
                  }

                  const controller = new AbortController();
                  const timer = setTimeout(
                    () => controller.abort(),
                    requestTimeoutMs
                  );
                  try {
                    const response = await fetch(raw, {
                      method: 'GET',
                      credentials: 'include',
                      cache: 'no-store',
                      redirect: 'manual',
                      signal: controller.signal
                    });
                    if (
                      response.type === 'opaqueredirect' ||
                      (
                        response.status >= 300 &&
                        response.status < 400
                      )
                    ) {
                      controller.abort();
                      return {
                        ok: false,
                        error: 'redirect_blocked'
                      };
                    }
                    if (!response.ok) {
                      return {
                        ok: false,
                        status: response.status
                      };
                    }
                    const responseUrl = new URL(response.url || raw);
                    if (
                      responseUrl.protocol !== 'https:' ||
                      !allowedHost(responseUrl.hostname)
                    ) {
                      controller.abort();
                      return {
                        ok: false,
                        error: 'response_host_rejected'
                      };
                    }

                    const contentLengthRaw =
                      response.headers.get('content-length');
                    const contentLength =
                      contentLengthRaw === null
                        ? null
                        : Number(contentLengthRaw);
                    if (
                      Number.isFinite(contentLength) &&
                      contentLength > maxBytes
                    ) {
                      controller.abort();
                      return {
                        ok: false,
                        error: 'too_large_header',
                        bytes: contentLength
                      };
                    }
                    if (!response.body) {
                      return {
                        ok: false,
                        error: 'missing_response_body'
                      };
                    }

                    const reader = response.body.getReader();
                    const chunks = [];
                    let total = 0;
                    while (true) {
                      const part = await reader.read();
                      if (part.done) break;
                      if (!part.value) continue;
                      total += part.value.byteLength;
                      if (total > maxBytes) {
                        try {
                          await reader.cancel('max_bytes_exceeded');
                        } catch {}
                        controller.abort();
                        return {
                          ok: false,
                          error: 'too_large_stream',
                          bytes: total
                        };
                      }
                      chunks.push(part.value);
                    }
                    if (total < 50000) {
                      return {
                        ok: false,
                        error: 'too_small',
                        bytes: total
                      };
                    }

                    const blob = new Blob(chunks, {
                      type:
                        response.headers.get('content-type') ||
                        'video/mp4'
                    });
                    const objectUrl = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = objectUrl;
                    a.download = `${expectedId}-${idx + 1}.mp4`;
                    a.style.display = 'none';
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                    setTimeout(
                      () => URL.revokeObjectURL(objectUrl),
                      5000
                    );
                    return {
                      ok: true,
                      status: response.status,
                      bytes: total,
                      downloadTriggered: true
                    };
                  } catch (err) {
                    return {
                      ok: false,
                      error:
                        `${err?.name || 'Error'}: ` +
                        `${err?.message || String(err)}`
                    };
                  } finally {
                    clearTimeout(timer);
                  }
                })()'''
                fetch_expression = fetch_expression.replace(
                    "__INDEX__", str(candidate_index)
                )
                fetch_expression = fetch_expression.replace(
                    "__EXPECTED_ID__", json.dumps(vid)
                )
                fetch_expression = fetch_expression.replace(
                    "__MAX_BYTES__",
                    str(CAMOFOX_FALLBACK_MAX_MEDIA_BYTES),
                )
                fetch_expression = fetch_expression.replace(
                    "__REQUEST_TIMEOUT_MS__",
                    str(request_budget_ms),
                )

                triggered = _fallback_request_json(
                    server,
                    "POST",
                    f"/tabs/{urllib.parse.quote(tab_id)}/evaluate",
                    {"userId": user_id, "expression": fetch_expression},
                    deadline=operation_deadline,
                    timeout_cap=(request_budget_ms / 1000.0) + 2.0,
                )
                trigger_result = (
                    triggered.get("result")
                    if isinstance(triggered, dict)
                    else None
                )
                if (
                    not isinstance(trigger_result, dict)
                    or not trigger_result.get("downloadTriggered")
                ):
                    reason = (
                        trigger_result.get("error")
                        if isinstance(trigger_result, dict)
                        else "evaluate_failed"
                    )
                    attempts.append(
                        f"candidate_{candidate_index + 1}:{reason}"
                    )
                    continue

                expected_filename = f"{vid}-{candidate_index + 1}.mp4"
                downloads: list[dict] = []
                download_wait_deadline = min(
                    operation_deadline,
                    time.monotonic()
                    + CAMOFOX_FALLBACK_DOWNLOAD_WAIT_SECONDS,
                )
                while time.monotonic() < download_wait_deadline:
                    _sleep_with_deadline(
                        download_wait_deadline,
                        0.25,
                    )
                    downloads = _camofox_downloads(
                        server,
                        tab_id,
                        user_id=user_id,
                        consume=False,
                        deadline=download_wait_deadline,
                    )
                    if downloads:
                        break
                if not downloads:
                    attempts.append(
                        f"candidate_{candidate_index + 1}:"
                        "download_not_captured"
                    )
                    continue

                matches = [
                    row
                    for row in downloads
                    if isinstance(row, dict)
                    and not row.get("failure")
                    and row.get("suggestedFilename")
                    == expected_filename
                    and isinstance(row.get("id"), str)
                    and isinstance(row.get("bytes"), int)
                ]
                if len(downloads) != 1 or len(matches) != 1:
                    attempts.append(
                        f"candidate_{candidate_index + 1}:"
                        "download_record_not_bound"
                    )
                    _clear_camofox_downloads(
                        server,
                        tab_id,
                        user_id=user_id,
                        deadline=operation_deadline,
                    )
                    continue
                item = matches[0]

                try:
                    captured_path = _camofox_local_download_path(
                        item,
                        temp_root=Path(server["tmp_dir"]),
                        expected_filename=expected_filename,
                    )
                    valid, validation, duration = _bounded_local_media_stage(
                        captured_path,
                        temp_path,
                        deadline=operation_deadline,
                        cleanup_deadline=hard_deadline,
                    )
                finally:
                    _clear_camofox_downloads(
                        server,
                        tab_id,
                        user_id=user_id,
                        deadline=operation_deadline,
                    )

                if not valid or duration is None:
                    temp_path.unlink(missing_ok=True)
                    attempts.append(
                        f"candidate_{candidate_index + 1}:{validation}"
                    )
                    continue

                delta = abs(duration - main_duration)
                if delta > CAMOFOX_FALLBACK_DURATION_TOLERANCE_S:
                    temp_path.unlink(missing_ok=True)
                    attempts.append(
                        f"candidate_{candidate_index + 1}:"
                        f"duration_mismatch:{delta:.3f}s"
                    )
                    continue

                _remaining_timeout(operation_deadline)
                os.replace(temp_path, mp4)
                success = {
                    "video_id": vid,
                    "url": url,
                    "ok": True,
                    "source": "network",
                    "transport": "camofox_browser_disk_handoff_v52",
                    "media_file": mp4,
                    "info_file": None,
                    "validation": validation,
                    "returncode": 0,
                    "diagnostic_tail": (
                        f"CamoFox fallback accepted candidate "
                        f"{candidate_index + 1}/{candidate_count}; "
                        f"duration_delta={delta:.3f}s"
                    ),
                    "camofox_source_commit":
                        CAMOFOX_FALLBACK_SOURCE_COMMIT,
                }
                break

            if success is None:
                raise RuntimeError(
                    "No CamoFox candidate passed media and "
                    "duration validation: "
                    + "; ".join(attempts[-8:])
                )
        except Exception as exc:
            failure = exc
            if isinstance(exc, TimeoutError):
                _stop_fallback_server(
                    force=True,
                    deadline=hard_deadline,
                )
        finally:
            temp_path.unlink(missing_ok=True)
            if server is not None:
                try:
                    cleanup_mode = _cleanup_fallback_session(
                        server,
                        user_id=user_id,
                        deadline=hard_deadline,
                    )
                except Exception as cleanup_exc:
                    failure = cleanup_exc
                    _stop_fallback_server(
                        force=True,
                        deadline=hard_deadline,
                    )

        if failure is None and success is not None:
            success["cleanup_mode"] = cleanup_mode
            return success

        exc = failure or RuntimeError(
            "CamoFox fallback failed without result"
        )
        return {
            "video_id": vid,
            "url": url,
            "ok": False,
            "source": "network",
            "transport": "camofox_browser_disk_handoff_v52",
            "media_file": mp4 if mp4.exists() else None,
            "info_file": None,
            "validation": "failed_before_acceptance" if mp4.exists() else "missing",
            "returncode": 1,
            "diagnostic_tail": (
                f"CamoFox TikTok fallback failed: "
                f"{type(exc).__name__}: {exc}"
            )[-2500:],
            "cleanup_mode": cleanup_mode,
            "camofox_source_commit":
                CAMOFOX_FALLBACK_SOURCE_COMMIT,
        }


def download_one(url: str, video_dir: Path) -> dict:
    vid = video_id_from_url(url)
    video_dir.mkdir(parents=True, exist_ok=True)
    mp4 = video_dir / f"{vid}.mp4"
    info_path = video_dir / f"{vid}.info.json"

    if mp4.exists():
        valid, validation = validate_media(mp4)
        return {
            "video_id": vid,
            "url": url,
            "ok": valid,
            "source": "existing_file",
            "media_file": mp4,
            "info_file": info_path if info_path.exists() else None,
            "validation": validation,
            "returncode": 0 if valid else 1,
            "diagnostic_tail": "",
        }

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",
        "--no-progress",
        "--write-info-json",
        "--no-overwrites",
        "--format", "b[ext=mp4]/b",
        "--output", str(video_dir / "%(id)s.%(ext)s"),
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        valid, validation = validate_media(mp4)
        detail = (result.stderr or result.stdout or "").strip()
        if result.returncode == 0 and valid:
            return {
                "video_id": vid,
                "url": url,
                "ok": True,
                "source": "network",
                "transport": "yt_dlp",
                "media_file": mp4,
                "info_file": info_path if info_path.exists() else None,
                "validation": validation,
                "returncode": 0,
                "diagnostic_tail": detail[-2500:],
            }

        if _should_use_camofox_fallback(url, detail):
            fallback = _download_one_via_camofox(url, video_dir)
            fallback["yt_dlp_returncode"] = result.returncode
            fallback["yt_dlp_diagnostic_tail"] = detail[-2500:]
            if fallback.get("ok"):
                return fallback
            fallback["diagnostic_tail"] = (
                "yt-dlp failed with verified TikTok webpage response error. "
                f"{fallback.get('diagnostic_tail') or ''}"
            )[-2500:]
            return fallback

        return {
            "video_id": vid,
            "url": url,
            "ok": False,
            "source": "network",
            "transport": "yt_dlp",
            "media_file": mp4 if mp4.exists() else None,
            "info_file": info_path if info_path.exists() else None,
            "validation": validation,
            "returncode": result.returncode,
            "diagnostic_tail": detail[-2500:],
        }
    except subprocess.TimeoutExpired as exc:
        # Fail this one video closed, but do NOT abort the creator sync.
        # yt-dlp may leave a .part file; keeping it allows a later run to resume.
        valid, validation = validate_media(mp4)
        detail = ""
        if exc.stderr:
            detail = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr)
        elif exc.stdout:
            detail = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout)

        return {
            "video_id": vid,
            "url": url,
            "ok": bool(valid),
            "source": "network_timeout",
            "media_file": mp4 if mp4.exists() else None,
            "info_file": info_path if info_path.exists() else None,
            "validation": validation,
            "returncode": 124,
            "diagnostic_tail": (
                f"yt-dlp timed out after 180 seconds. "
                f"Partial files are retained for resume. {detail}"
            )[-2500:],
        }


def transcribe(
    root: Path,
    creator_key: str,
    media_path: Path,
    *,
    model_holder: dict,
) -> dict:
    transcript_dir = root / "output" / creator_key / "tiktok" / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    txt_path = transcript_dir / f"{media_path.stem}.txt"
    json_path = transcript_dir / f"{media_path.stem}.json"

    if txt_path.exists() and json_path.exists():
        return {
            "ok": True,
            "source": "existing_transcript",
            "txt": txt_path,
            "json": json_path,
            "transcribed_at": datetime.fromtimestamp(
                txt_path.stat().st_mtime, timezone.utc
            ).isoformat(),
        }

    valid, validation = validate_media(media_path)
    if not valid:
        return {"ok": False, "error": f"invalid_media:{validation}"}

    if model_holder.get("model") is None:
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel
        model_holder["model"] = WhisperModel("small", device="cpu", compute_type="int8")

    model = model_holder["model"]
    segments, info = model.transcribe(
        str(media_path),
        beam_size=5,
        vad_filter=True,
    )

    rows = []
    text_parts = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            text_parts.append(text)
        rows.append({
            "start": round(float(seg.start), 3),
            "end": round(float(seg.end), 3),
            "text": text,
        })

    when = utc_now()
    txt_path.write_text(" ".join(text_parts).strip() + "\n", encoding="utf-8")
    atomic_json(json_path, {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "source_platform": "TIKTOK",
        "creator": creator_key,
        "video_id": media_path.stem,
        "generated_at": when,
        "language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "duration": getattr(info, "duration", None),
        "segments": rows,
    })
    return {
        "ok": True,
        "source": "whisper",
        "txt": txt_path,
        "json": json_path,
        "transcribed_at": when,
    }


def merge_catalog(
    catalog: dict,
    discovered: list[str],
    *,
    profile_url: str,
) -> dict:
    items = catalog.get("items", {})
    now = utc_now()
    for rank, url in enumerate(discovered):
        vid = video_id_from_url(url)
        old = items.get(vid, {})
        items[vid] = {
            "video_id": vid,
            "url": url,
            "profile_url": profile_url,
            "first_seen_at": old.get("first_seen_at") or now,
            "last_seen_at": now,
            "last_seen_rank": rank,
        }

    ordered_ids = [video_id_from_url(u) for u in discovered]
    for vid in catalog.get("order", []):
        if vid not in ordered_ids and vid in items:
            ordered_ids.append(vid)

    return {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "updated_at": now,
        "profile_url": profile_url,
        "order": ordered_ids,
        "items": items,
    }


def update_main_manifest(
    root: Path,
    *,
    creator_key: str,
    url: str,
    download: dict,
    transcription: dict,
) -> dict:
    manifest_path = root / "state" / "manifest.json"
    manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
    manifest.setdefault("items", {})

    vid = download["video_id"]
    info = {}
    info_file = download.get("info_file")
    if info_file and Path(info_file).exists():
        try:
            info = json.loads(Path(info_file).read_text(encoding="utf-8"))
        except Exception:
            info = {}

    media_path = Path(download["media_file"])
    txt_path = Path(transcription["txt"])
    json_path = Path(transcription["json"])
    key = f"tt_{vid}"

    old = manifest["items"].get(key, {})
    incoming_caption = str(
        info.get("description") or info.get("title") or ""
    ).strip()
    caption = incoming_caption or str(old.get("caption") or "")
    incoming_published_at = published_iso(info)
    published_at = (
        incoming_published_at
        if incoming_published_at is not None
        else old.get("published_at")
    )

    manifest["items"][key] = {
        **old,
        "schema_version": 1,
        "source_platform": "TIKTOK",
        "source_type": "VIDEO",
        "source_id": vid,
        "shortcode": key,
        "creator": creator_key,
        "url": url,
        "caption": caption,
        "published_at": published_at,
        "downloaded_at": old.get("downloaded_at") or utc_now(),
        "download_status": "DONE",
        "media_file": str(media_path.relative_to(root)),
        "media_validation": download.get("validation"),
        "transcribed_at": transcription["transcribed_at"],
        "transcription_status": "DONE",
        "transcript_txt": str(txt_path.relative_to(root)),
        "transcript_json": str(json_path.relative_to(root)),
        "research_status": old.get("research_status") or "PENDING",
        "source_class": "INFLUENCER_DISCOVERY_SECONDARY",
    }
    atomic_json(manifest_path, manifest)
    return manifest["items"][key]


def run_research_queue(root: Path) -> dict:
    script = root / "app" / "research_queue.py"
    if not script.exists():
        return {"ok": False, "error": f"missing {script}"}
    result = subprocess.run(
        [sys.executable, str(script), "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout_tail": (result.stdout or "")[-3000:],
        "stderr_tail": (result.stderr or "")[-3000:],
    }


def process_source(root: Path, source: dict, *, max_new_override: int | None = None, include_video_ids: set[str] | None = None, discovery_target_override: int | None = None) -> dict:
    creator_key = str(source["creator_key"])
    handle = str(source["handle"]).lstrip("@")
    profile_url = str(source.get("profile_url") or f"https://www.tiktok.com/@{handle}")
    discovery_step = int(source.get("discovery_step", 200))
    max_catalog = int(source.get("max_catalog", 2500))
    configured_max_new = (
        max_new_override if max_new_override is not None
        else source.get("max_new_downloads")
    )
    max_new_downloads = (
        int(configured_max_new) if configured_max_new is not None else None
    )
    if max_new_downloads is not None and max_new_downloads < 0:
        raise ValueError("max_new_downloads must be >= 0 when explicitly configured")

    state_dir = root / "state" / "tiktok"
    catalog_path = state_dir / f"{creator_key}_catalog.json"
    catalog = load_json(
        catalog_path,
        {
            "schema_version": 1,
            "app_version": APP_VERSION,
            "profile_url": profile_url,
            "order": [],
            "items": {},
        },
    )

    # Seed only the original Nicholas Crown source from the successful v0.6 POC.
    # Generic/evaluation creators must never inherit another creator's discovered URLs.
    if creator_key == "nicholascrown":
        poc_urls = load_json(
            state_dir / "camofox_discovered_urls.json",
            {"urls": []},
        ).get("urls", [])
        if poc_urls:
            catalog = merge_catalog(catalog, [str(x) for x in poc_urls], profile_url=profile_url)

    previous_count = len(catalog.get("items", {}))
    if discovery_target_override is None:
        # Full/evaluation sync may deepen the historical catalog over time.
        discovery_target = min(max_catalog, max(discovery_step, previous_count + discovery_step))
    else:
        # Recurring monitoring only needs a bounded recent window. Re-scanning an
        # ever-growing historical catalog on every monitor cycle is unnecessary.
        discovery_target = min(max_catalog, max(1, int(discovery_target_override)))

    user_id = f"instagramresearch-tiktok-{creator_key}"
    session_key = f"{creator_key}-feed"
    tab_id = None

    try:
        tab = request_json("POST", "/tabs", {
            "userId": user_id,
            "sessionKey": session_key,
            "url": profile_url,
            "trace": False,
        }, timeout=60)
        if not isinstance(tab, dict) or not tab.get("tabId"):
            raise RuntimeError(f"Unexpected CamoFox create-tab response: {tab}")
        tab_id = str(tab["tabId"])
        time.sleep(3)

        discovered, discovery_diag = collect_video_urls(
            tab_id,
            user_id=user_id,
            handle=handle,
            target=discovery_target,
        )
        catalog = merge_catalog(catalog, discovered, profile_url=profile_url)
        atomic_json(catalog_path, catalog)
    finally:
        if tab_id:
            try:
                request_json(
                    "DELETE",
                    f"/tabs/{urllib.parse.quote(tab_id)}?"
                    + urllib.parse.urlencode({"userId": user_id}),
                    timeout=10,
                )
            except Exception:
                pass

    main_manifest = load_json(root / "state" / "manifest.json", {"schema_version": 1, "items": {}})
    main_items = main_manifest.get("items", {})

    ordered_urls = [
        catalog["items"][vid]["url"]
        for vid in catalog.get("order", [])
        if vid in catalog.get("items", {})
    ]
    skipped_known = sum(
        1 for url in ordered_urls
        if f"tt_{video_id_from_url(url)}" in main_items
        and main_items[f"tt_{video_id_from_url(url)}"].get("download_status") == "DONE"
        and main_items[f"tt_{video_id_from_url(url)}"].get("transcription_status") == "DONE"
    )
    candidates = [
        url for url in ordered_urls
        if (include_video_ids is None or video_id_from_url(url) in include_video_ids)
        and not (
            f"tt_{video_id_from_url(url)}" in main_items
            and main_items[f"tt_{video_id_from_url(url)}"].get("download_status") == "DONE"
            and main_items[f"tt_{video_id_from_url(url)}"].get("transcription_status") == "DONE"
        )
    ]
    if max_new_downloads is not None:
        candidates = candidates[:max_new_downloads]

    video_dir = root / "output" / creator_key / "tiktok" / "videos"
    model_holder: dict[str, Any] = {"model": None}
    completed = []
    failures = []
    reused_poc = 0
    downloaded_network = 0

    for url in candidates:
        vid = video_id_from_url(url)

        adopted_mp4, adopted_info = adopt_poc_file(root, vid, video_dir)
        if adopted_mp4:
            valid, validation = validate_media(adopted_mp4)
            download = {
                "video_id": vid,
                "url": url,
                "ok": valid,
                "source": "poc_reuse",
                "media_file": adopted_mp4,
                "info_file": adopted_info,
                "validation": validation,
                "returncode": 0 if valid else 1,
                "diagnostic_tail": "",
            }
            reused_poc += 1
        else:
            download = download_one(url, video_dir)
            if download.get("source") == "network" and download.get("ok"):
                downloaded_network += 1

        if not download.get("ok") or not download.get("media_file"):
            failures.append({
                "video_id": vid,
                "url": url,
                "stage": "download",
                "detail": download.get("diagnostic_tail") or download.get("validation"),
            })
            continue

        try:
            transcription = transcribe(
                root,
                creator_key,
                Path(download["media_file"]),
                model_holder=model_holder,
            )
        except Exception as exc:
            failures.append({
                "video_id": vid,
                "url": url,
                "stage": "transcription",
                "detail": f"{type(exc).__name__}: {exc}",
            })
            continue

        if not transcription.get("ok"):
            failures.append({
                "video_id": vid,
                "url": url,
                "stage": "transcription",
                "detail": transcription.get("error"),
            })
            continue

        record = update_main_manifest(
            root,
            creator_key=creator_key,
            url=url,
            download=download,
            transcription=transcription,
        )
        completed.append({
            "video_id": vid,
            "url": url,
            "download_source": download.get("source"),
            "media_file": record["media_file"],
            "transcript_txt": record["transcript_txt"],
        })

    return {
        "creator_key": creator_key,
        "handle": handle,
        "profile_url": profile_url,
        "catalog_before": previous_count,
        "discovery_target": discovery_target,
        "catalog_after": len(catalog.get("items", {})),
        "discovery": discovery_diag,
        "skipped_known": skipped_known,
        "candidate_new": len(candidates),
        "max_new_downloads_effective": max_new_downloads,
        "completed_new": len(completed),
        "reused_poc_files": reused_poc,
        "downloaded_network": downloaded_network,
        "failures": failures,
        "completed": completed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--max-new", type=int, default=None)
    args = parser.parse_args()
    root = args.root.resolve()

    config_path = root / "control" / "tiktok_sources.json"
    config = load_json(config_path)
    sources = [s for s in config.get("sources", []) if s.get("enabled")]
    if not sources:
        raise RuntimeError("No enabled TikTok sources in control/tiktok_sources.json")

    status_path = root / "state" / "tiktok" / "sync_status.json"
    started = utc_now()
    atomic_json(status_path, {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "state": "RUNNING",
        "started_at": started,
    })

    server = start_server()
    results = []
    top_errors = []
    cleanup_error = None

    try:
        for source in sources:
            try:
                results.append(
                    process_source(root, source, max_new_override=args.max_new)
                )
            except Exception as exc:
                top_errors.append({
                    "creator_key": source.get("creator_key"),
                    "error": f"{type(exc).__name__}: {exc}",
                })
    finally:
        try:
            stop_server(deadline=time.monotonic() + 8.0)
        except Exception as exc:
            cleanup_error = f"{type(exc).__name__}: {exc}"
            top_errors.append({"creator_key": None, "stage": "CAMOFOX_CLEANUP", "error": cleanup_error})

    queue = run_research_queue(root)

    failures = sum(len(r.get("failures", [])) for r in results) + len(top_errors)
    if not queue.get("ok"):
        failures += 1

    status = {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "state": "DONE" if failures == 0 else "DONE_WITH_ERRORS",
        "started_at": started,
        "finished_at": utc_now(),
        "server": server,
        "results": results,
        "research_queue": queue,
        "top_errors": top_errors,
        "total_errors": failures,
    }
    atomic_json(status_path, status)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--_b042-stage-media":
        raise SystemExit(_run_local_media_stage_cli(Path(sys.argv[2]), Path(sys.argv[3])))
    raise SystemExit(main())
