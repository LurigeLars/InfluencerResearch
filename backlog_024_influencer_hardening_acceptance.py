from __future__ import annotations

import ast
import hashlib
import importlib.metadata as md
import re
import sys
from pathlib import Path

EXPECTED = {
    "playwright": "1.62.0",
    "yt-dlp": "2026.8.19",
    "yt-dlp-ejs": "0.8.0",
    "faster-whisper": "1.2.1",
    "imageio-ffmpeg": "0.6.0",
}

def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)

def main() -> int:
    target = Path(__file__).resolve().parent / "ephemeral_ingest.py"
    if not target.is_file():
        fail(f"missing target: {target}")
    raw = target.read_bytes()
    text = raw.decode("utf-8-sig")
    try:
        tree = ast.parse(text, filename=str(target))
    except SyntaxError as exc:
        fail(f"syntax: {exc}")

    app_version = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "APP_VERSION" for t in node.targets):
            if isinstance(node.value, ast.Constant):
                app_version = node.value.value
                break
    if app_version != "0.4.4":
        fail(f"APP_VERSION={app_version!r}, expected '0.4.4'")

    validator = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "normalize_creator_handle"), None)
    if validator is None:
        fail("normalize_creator_handle missing")
    ns = {"re": re}
    exec(compile(ast.Module(body=[validator], type_ignores=[]), "<validator>", "exec"), ns)
    norm = ns["normalize_creator_handle"]
    if norm("@nicholascrown") != "nicholascrown":
        fail("canonical creator normalization failed")
    for bad in ("../x", "..\\x", "a/b", "a\\b", "a b", "x" * 31):
        try:
            norm(bad)
        except ValueError:
            pass
        else:
            fail(f"unsafe creator accepted: {bad!r}")

    needle = 'sys.executable, "-m", "yt_dlp",\n        "--ignore-config",\n        "--cookies", str(cookie_path),'
    if needle not in text:
        fail("yt-dlp authenticated path is not fail-closed with --ignore-config before cookies")
    if 'creators.append(normalize_creator_handle(handle))' not in text:
        fail("configured creator path is not normalized")
    if 'creators = [normalize_creator_handle(args.creator)]' not in text:
        fail("direct creator path is not normalized")

    for name, expected in EXPECTED.items():
        try:
            actual = md.version(name)
        except md.PackageNotFoundError:
            fail(f"package missing: {name}")
        if actual != expected:
            fail(f"package drift: {name}={actual}, expected {expected}")
        print(f"PACKAGE_OK {name}=={actual}")

    print(f"TARGET_SHA256={hashlib.sha256(raw).hexdigest()}")
    print(f"APP_VERSION={app_version}")
    print("NO_NETWORK_USED_BY_ACCEPTANCE_HELPER")
    print("NO_COOKIES_OR_TOKENS_READ_BY_ACCEPTANCE_HELPER")
    print("PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
