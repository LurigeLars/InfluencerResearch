#!/usr/bin/env python3
"""Prepare, never deploy, a Camoufox browser binary from reviewed Camofox master.

Only a browser release merged into the Camofox fork's default branch is eligible.
Cross-check BOTH Linux architecture digests with the official GitHub release,
then update IR's independent x86_64 Docker pin, strict test and runtime README.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "runtime/camofox/Dockerfile"
TEST = ROOT / "test_camofox_container_runtime.py"
README = ROOT / "runtime/camofox/README.md"
SHA = re.compile(r"^[a-f0-9]{40}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")


def one_arg(source: str, name: str) -> str:
    values = re.findall(rf"^ARG {re.escape(name)}=([^\s]+)$", source, flags=re.MULTILINE)
    if len(values) != 1:
        raise ValueError(f"Exactly one Docker {name} is required")
    return values[0]


def checked_release(source: str, downloader: str, metadata: dict) -> tuple[str, str, str]:
    version = one_arg(source, "CAMOUFOX_VERSION")
    beta = one_arg(source, "CAMOUFOX_RELEASE")
    amd = one_arg(source, "CAMOUFOX_SHA256_AMD64")
    arm = one_arg(source, "CAMOUFOX_SHA256_ARM64")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"beta\.\d+", beta):
        raise ValueError("Reviewed Camoufox release tag is invalid")
    if not DIGEST.fullmatch(amd) or not DIGEST.fullmatch(arm):
        raise ValueError("Reviewed Camoufox source digests are invalid")
    tag = f"v{version}-{beta}"
    if downloader.count(f"BUNDLED_CAMOUFOX_RELEASE = '{version}-{beta}'") != 1:
        raise ValueError("Fork downloader and approved Docker browser differ")
    if metadata.get("tag_name") != tag or metadata.get("draft"):
        raise ValueError("Official browser release metadata does not match approved fork")
    assets = metadata.get("assets", [])
    for architecture, expected in (("x86_64", amd), ("arm64", arm)):
        name = f"camoufox-{version}-{beta}-lin.{architecture}.zip"
        candidates = [a for a in assets if a.get("name") == name]
        if len(candidates) != 1 or candidates[0].get("digest") != "sha256:" + expected:
            raise ValueError(f"Official SHA256 digest mismatch: {architecture}")
    return version, beta, amd


def _single_replace(content: str, old: str, new: str, filename: str) -> str:
    if content.count(old) != 1:
        raise ValueError(f"{filename}: reviewed value occurs {content.count(old)} times, not once")
    return content.replace(old, new)


def propose(source: str, downloader: str, metadata: dict, source_sha: str) -> bool:
    if not SHA.fullmatch(source_sha):
        raise ValueError("Camofox fork source must be an immutable full commit SHA")
    version, beta, amd = checked_release(source, downloader, metadata)
    current = DOCKERFILE.read_text(encoding="utf-8")
    old_version = one_arg(current, "CAMOUFOX_VERSION")
    old_beta = one_arg(current, "CAMOUFOX_RELEASE")
    old_amd = one_arg(current, "CAMOUFOX_SHA256")
    if not DIGEST.fullmatch(old_amd):
        raise ValueError("IR runtime SHA256 is not reviewed")
    if (old_version, old_beta, old_amd) == (version, beta, amd):
        return False

    test = TEST.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    # Check all edits before touching any file. Preserve exact binary/runtime
    # assertions; never disable tests to make an upgrade appear compatible.
    docker_edits = [(f"ARG CAMOUFOX_VERSION={old_version}", f"ARG CAMOUFOX_VERSION={version}"),
                    (f"ARG CAMOUFOX_RELEASE={old_beta}", f"ARG CAMOUFOX_RELEASE={beta}"),
                    (f"ARG CAMOUFOX_SHA256={old_amd}", f"ARG CAMOUFOX_SHA256={amd}")]
    test_edits = [(f'"CAMOUFOX_VERSION={old_version}"', f'"CAMOUFOX_VERSION={version}"'),
                  (f'"CAMOUFOX_RELEASE={old_beta}"', f'"CAMOUFOX_RELEASE={beta}"'),
                  (f'"CAMOUFOX_SHA256={old_amd}"', f'"CAMOUFOX_SHA256={amd}"')]
    for old, new in docker_edits:
        current = _single_replace(current, old, new, "Dockerfile")
    for old, new in test_edits:
        test = _single_replace(test, old, new, "test_camofox_container_runtime.py")
    readme = _single_replace(readme,
        f"- Camoufox browser baseline: \x60{old_version}\x60 / \x60{old_beta}\x60",
        f"- Camoufox browser baseline: \x60{version}\x60 / \x60{beta}\x60",
        "runtime/camofox/README.md")
    # No change to the separate pinned Camofox server package, runtime secrets,
    # browser profiles, networking or process startup.
    DOCKERFILE.write_text(current, encoding="utf-8")
    TEST.write_text(test, encoding="utf-8")
    README.write_text(readme, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dockerfile", type=Path, required=True)
    parser.add_argument("--source-downloader", type=Path, required=True)
    parser.add_argument("--release-json", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    changed = propose(
        args.source_dockerfile.read_text(encoding="utf-8"),
        args.source_downloader.read_text(encoding="utf-8"),
        json.loads(args.release_json.read_text(encoding="utf-8")),
        args.source_sha,
    )
    print("changed" if changed else "current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
