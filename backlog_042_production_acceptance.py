from __future__ import annotations

import hashlib
import json
import shutil
import sys
from importlib import metadata
from pathlib import Path

if len(sys.argv) != 4:
    raise SystemExit("usage: probe.py <root> <evidence> <temp_root>")

root = Path(sys.argv[1])
evidence = Path(sys.argv[2])
temp_root = Path(sys.argv[3])
evidence.mkdir(parents=True, exist_ok=True)

expected_versions = {
    "yt-dlp": "2026.8.19",
    "curl_cffi": "0.16.2",
    "cffi": "2.1.1",
    "pycparser": "3.0",
    "certifi": "2026.7.22",
}

controls = [
    ("7676684064174247182", "https://www.tiktok.com/@nicholas_crown/video/7676684064174247182"),
    ("7674813163648322830", "https://www.tiktok.com/@nicholas_crown/video/7674813163648322830"),
]

def tree_hash(path: Path) -> str:
    h = hashlib.sha256()
    if not path.exists():
        h.update(b"<MISSING>")
        return h.hexdigest()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = file.relative_to(path).as_posix().encode("utf-8")
        h.update(len(rel).to_bytes(8, "big"))
        h.update(rel)
        with file.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()

versions = {}
version_ok = True
for name, expected in expected_versions.items():
    try:
        actual = metadata.version(name)
    except metadata.PackageNotFoundError:
        actual = None
    versions[name] = actual
    version_ok = version_ok and (actual == expected)

state_dir = root / "state"
state_before = tree_hash(state_dir)

sys.path.insert(0, str(root / "app"))
import tiktok_camofox_sync as tt

results = []
all_ok = version_ok

try:
    shutil.rmtree(temp_root, ignore_errors=True)
    temp_root.mkdir(parents=True, exist_ok=True)

    for video_id, url in controls:
        video_dir = temp_root / video_id
        result = tt.download_one(url, video_dir)

        media_raw = result.get("media_file")
        info_raw = result.get("info_file")
        media = Path(media_raw) if media_raw else None
        info_path = Path(info_raw) if info_raw else None

        media_exists = bool(media and media.exists())
        media_size = media.stat().st_size if media_exists else 0

        info = {}
        if info_path and info_path.exists():
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except Exception as exc:
                info = {"_parse_error": f"{type(exc).__name__}:{exc}"}

        info_id = str(info.get("id") or "")
        uploader = str(info.get("uploader") or "").lstrip("@").lower()

        identity_ok = info_id == video_id and uploader == "nicholas_crown"
        download_ok = bool(result.get("ok")) and media_exists and media_size >= 50_000
        control_ok = download_ok and identity_ok
        all_ok = all_ok and control_ok

        results.append({
            "video_id": video_id,
            "url": url,
            "ok": control_ok,
            "download_ok": download_ok,
            "identity_ok": identity_ok,
            "source": result.get("source"),
            "returncode": result.get("returncode"),
            "validation": result.get("validation"),
            "media_exists": media_exists,
            "media_size_bytes": media_size,
            "info_id": info_id,
            "uploader": uploader,
            "diagnostic_tail": result.get("diagnostic_tail"),
        })
finally:
    shutil.rmtree(temp_root, ignore_errors=True)

temp_removed = not temp_root.exists()
state_after = tree_hash(state_dir)
state_unchanged = state_before == state_after
all_ok = all_ok and temp_removed and state_unchanged

summary = {
    "state": "PRODUCTION_PATH_ACCEPTANCE_PASS" if all_ok else "PRODUCTION_PATH_ACCEPTANCE_FAIL",
    "versions": versions,
    "version_ok": version_ok,
    "controls_tested": len(controls),
    "controls_passed": sum(1 for x in results if x["ok"]),
    "results": results,
    "state_tree_sha256_before": state_before,
    "state_tree_sha256_after": state_after,
    "state_unchanged": state_unchanged,
    "temp_removed": temp_removed,
    "production_path": "app/tiktok_camofox_sync.py::download_one",
}

summary_path = evidence / "production-acceptance.json"
summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
raise SystemExit(0 if all_ok else 1)
