from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from creator_registry import get_creator, select_evaluation_source

ORCHESTRATOR_VERSION = "0.2.2"




VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")


def trusted_source_video_ids(source: dict) -> list[str]:
    raw = source.get("evaluation_video_ids")
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError("BAD_EVALUATION_VIDEO_IDS")
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        video_id = str(value or "").strip()
        if not VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError("BAD_EVALUATION_VIDEO_ID")
        if video_id not in seen:
            seen.add(video_id)
            out.append(video_id)
    return out


def trusted_attribution_term(source: dict) -> str:
    value = str(source.get("required_attribution_term") or "").strip()
    if len(value) > 120 or any(ord(ch) < 32 for ch in value):
        raise ValueError("BAD_REQUIRED_ATTRIBUTION_TERM")
    return value


def creator_key(name: str, profile_url: str) -> str:
    if name.strip():
        key = re.sub(r"[^a-z0-9]+", "", name.casefold())
        if key:
            return key[:80]
    path = urlparse(profile_url).path.strip("/")
    tail = path.split("/")[-1] if path else "creator"
    key = re.sub(r"[^a-z0-9]+", "", tail.casefold())
    return (key or "creator")[:80]


def is_youtube(url: str) -> bool:
    host = (urlparse(url).netloc or "").casefold()
    return host.endswith("youtube.com") or host.endswith("youtu.be")


def main() -> int:
    ap = argparse.ArgumentParser(description="InfluencerResearch creator-evaluation entry point")
    ap.add_argument("--root", type=Path, required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--creator-key", dest="registered_creator_key")
    mode.add_argument("--profile-url")
    ap.add_argument("--source-platform", choices=["YOUTUBE", "TIKTOK"], default=None)
    ap.add_argument("--creator-name", default="")
    ap.add_argument("--sample-size", type=int, default=20)
    ap.add_argument("--only-video-ids", default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    root = args.root.resolve()
    sample = max(1, min(int(args.sample_size), 100))

    if args.registered_creator_key:
        try:
            profile_obj = get_creator(root, args.registered_creator_key)
            source = select_evaluation_source(profile_obj, args.source_platform)
        except (KeyError, ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
            print(f"InfluencerResearch registry rejection: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 6
        profile = str(source["profile_url"])
        platform = str(source["platform"])
        name = str(profile_obj["display_name"])
        key = str(profile_obj["creator_key"])
        verification_basis = str(source.get("verification_basis") or profile_obj.get("verification", {}).get("basis") or "REGISTERED_VERIFIED_SOURCE")
        try:
            source_video_ids = trusted_source_video_ids(source) if platform == "YOUTUBE" else []
            source_attribution_term = trusted_attribution_term(source) if platform == "YOUTUBE" else ""
        except ValueError as exc:
            print(f"InfluencerResearch registry rejection: {exc}", file=sys.stderr)
            return 6
        print(f"InfluencerResearch {ORCHESTRATOR_VERSION}: registered creator={key} platform={platform}")
    else:
        profile = str(args.profile_url).strip()
        platform = "YOUTUBE" if is_youtube(profile) else "TIKTOK"
        name = args.creator_name.strip() or creator_key("", profile)
        key = creator_key(name, profile)
        verification_basis = "LEGACY_DIRECT_PROFILE_MODE"
        source_video_ids = []
        source_attribution_term = ""

    if platform == "YOUTUBE":
        cmd = [
            sys.executable, str(root / "app" / "youtube_creator_evaluation.py"),
            "--root", str(root),
            "--channel-url", profile,
            "--creator-key", key,
            "--creator-name", name,
            "--sample-size", str(sample),
            "--verification-basis", verification_basis,
        ]
        exact_video_ids = args.only_video_ids.strip()
        if not exact_video_ids and source_video_ids:
            exact_video_ids = ",".join(source_video_ids)
        if exact_video_ids:
            cmd.extend(["--only-video-ids", exact_video_ids])
        if source_attribution_term:
            cmd.extend(["--required-attribution-term", source_attribution_term])
        print(f"InfluencerResearch {ORCHESTRATOR_VERSION}: YOUTUBE adapter -> {profile}")
    else:
        cmd = [
            sys.executable, str(root / "app" / "creator_evaluation.py"),
            "--root", str(root),
            "--profile-url", profile,
            "--sample-size", str(sample),
            "--creator-name", name,
        ]
        if args.registered_creator_key:
            cmd.extend(["--registered-creator-key", key])
        print(f"InfluencerResearch {ORCHESTRATOR_VERSION}: TIKTOK discovery adapter -> {profile}")
        print("Instagram URLs remain identifiers only and are not opened by this path.")

    p = subprocess.run(cmd)
    return int(p.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
