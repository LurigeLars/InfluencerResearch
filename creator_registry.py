from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

REGISTRY_SCHEMA_VERSION = 1
CREATOR_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")
SUPPORTED_PLATFORMS = {"YOUTUBE", "TIKTOK", "INSTAGRAM"}
EVALUATION_PLATFORMS = {"YOUTUBE", "TIKTOK"}
MONITOR_PLATFORMS = {"YOUTUBE", "TIKTOK"}
ALLOWED_HOST_SUFFIXES = {
    "YOUTUBE": ("youtube.com", "youtu.be"),
    "TIKTOK": ("tiktok.com",),
    "INSTAGRAM": ("instagram.com",),
}
REGISTRATION_VERIFICATION_METHODS = {
    "OFFICIAL_SITE_CROSSLINK",
    "OFFICIAL_LINKTREE_CROSSLINK",
    "CREATOR_CONTROLLED_CROSS_PLATFORM_LINK",
    "HANDLE_BRANDING_BIO_CROSSCHECK",
    "MULTI_SOURCE_CORROBORATION",
    "EXISTING_ACCEPTED_SOURCE_CONFIG",
}
REGISTRATION_SOURCE_KEYS = {
    "platform", "profile_url", "evaluation_enabled", "monitoring_enabled", "priority"
}
RESERVED_INSTAGRAM_PATHS = {
    "p", "reel", "reels", "stories", "explore", "accounts", "direct", "about", "legal"
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _valid_https_url(platform: str, value: str) -> bool:
    try:
        p = urlparse(value)
    except Exception:
        return False
    if p.scheme.lower() != "https" or not p.netloc or p.username or p.password:
        return False
    host = p.netloc.lower().split(":", 1)[0]
    return any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES[platform])


def _valid_passive_https_ref(value: str) -> bool:
    try:
        p = urlparse(value)
    except Exception:
        return False
    return p.scheme.lower() == "https" and bool(p.netloc) and not p.username and not p.password and len(value) <= 500


def _clean_display_name(value: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 120 or any(ord(ch) < 32 for ch in value):
        raise ValueError("BAD_DISPLAY_NAME")
    return value


def canonicalize_profile_url(platform: str, value: str) -> str:
    platform = str(platform or "").upper()
    value = str(value or "").strip()
    if platform not in SUPPORTED_PLATFORMS or not _valid_https_url(platform, value):
        raise ValueError(f"BAD_{platform or 'UNKNOWN'}_PROFILE_URL")
    p = urlparse(value)
    host = p.hostname.lower() if p.hostname else ""
    parts = [x for x in p.path.split("/") if x]

    if platform == "YOUTUBE":
        if host == "youtu.be" or host.endswith(".youtu.be") or not parts:
            raise ValueError("YOUTUBE_PROFILE_URL_REQUIRED")
        first = parts[0]
        if first.startswith("@") and len(first) > 1:
            path = "/" + first
        elif first in {"channel", "c", "user"} and len(parts) >= 2 and parts[1]:
            path = f"/{first}/{parts[1]}"
        else:
            raise ValueError("YOUTUBE_PROFILE_URL_REQUIRED")
        return "https://www.youtube.com" + path

    if platform == "TIKTOK":
        if not parts or not parts[0].startswith("@") or len(parts[0]) <= 1:
            raise ValueError("TIKTOK_PROFILE_URL_REQUIRED")
        return "https://www.tiktok.com/" + parts[0]

    if not parts or parts[0].lower() in RESERVED_INSTAGRAM_PATHS or parts[0].startswith("@"):  # Instagram profile path has no @
        raise ValueError("INSTAGRAM_PROFILE_URL_REQUIRED")
    return "https://www.instagram.com/" + parts[0].strip("/") + "/"


def validate_registry(obj: dict) -> dict:
    if not isinstance(obj, dict) or obj.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise ValueError("creator_registry.json must be schema_version=1 object")
    creators = obj.get("creators")
    if not isinstance(creators, dict):
        raise ValueError("creator_registry.json creators must be an object")
    normalized: dict[str, dict] = {}
    for key, profile in creators.items():
        key = str(key).strip().lower()
        if not CREATOR_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid creator_key: {key}")
        if not isinstance(profile, dict):
            raise ValueError(f"creator profile {key} must be object")
        if str(profile.get("creator_key", key)).strip().lower() != key:
            raise ValueError(f"creator_key mismatch: {key}")
        status = str(profile.get("status", "ACTIVE")).upper()
        if status not in {"ACTIVE", "DISABLED"}:
            raise ValueError(f"invalid status for {key}: {status}")
        display_name = str(profile.get("display_name") or "").strip()
        if not display_name:
            raise ValueError(f"missing display_name for {key}")
        verification = profile.get("verification") or {}
        if str(verification.get("status", "")).upper() != "VERIFIED":
            raise ValueError(f"creator {key} is not VERIFIED")
        sources = profile.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"creator {key} has no sources")
        seen_platforms = set()
        normalized_sources = []
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError(f"bad source for {key}")
            platform = str(source.get("platform") or "").upper()
            if platform not in SUPPORTED_PLATFORMS:
                raise ValueError(f"unsupported platform for {key}: {platform}")
            url = str(source.get("profile_url") or "").strip()
            if not _valid_https_url(platform, url):
                raise ValueError(f"invalid {platform} profile_url for {key}")
            if str(source.get("verification_status", "VERIFIED")).upper() != "VERIFIED":
                raise ValueError(f"unverified source for {key}: {platform}")
            if platform in seen_platforms:
                raise ValueError(f"duplicate platform source for {key}: {platform}")
            seen_platforms.add(platform)
            normalized_sources.append({
                **source,
                "platform": platform,
                "profile_url": url,
                "enabled": bool(source.get("enabled", True)),
                "evaluation_enabled": bool(source.get("evaluation_enabled", platform in EVALUATION_PLATFORMS)),
                "monitoring_enabled": bool(source.get("monitoring_enabled", False)),
                "priority": int(source.get("priority", 100)),
                "verification_status": "VERIFIED",
            })
        normalized[key] = {
            **profile,
            "creator_key": key,
            "display_name": display_name,
            "status": status,
            "monitoring_enabled": bool(profile.get("monitoring_enabled", False)),
            "sources": normalized_sources,
        }
    return {**obj, "creators": normalized}


def normalize_registration_request(req: dict) -> dict:
    if not isinstance(req, dict):
        raise ValueError("REGISTRATION_NOT_OBJECT")
    key = str(req.get("creator_key") or "").strip().lower()
    if not CREATOR_KEY_RE.fullmatch(key):
        raise ValueError("BAD_CREATOR_KEY")
    display_name = _clean_display_name(req.get("display_name"))

    methods = req.get("verification_methods")
    if not isinstance(methods, list) or not 1 <= len(methods) <= 4:
        raise ValueError("BAD_VERIFICATION_METHODS")
    normalized_methods = []
    for method in methods:
        method = str(method or "").upper()
        if method not in REGISTRATION_VERIFICATION_METHODS:
            raise ValueError(f"BAD_VERIFICATION_METHOD:{method}")
        if method not in normalized_methods:
            normalized_methods.append(method)

    refs = req.get("verification_refs")
    if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
        raise ValueError("BAD_VERIFICATION_REFS")
    normalized_refs = []
    for ref in refs:
        ref = str(ref or "").strip()
        if not _valid_passive_https_ref(ref):
            raise ValueError("BAD_VERIFICATION_REF")
        if ref not in normalized_refs:
            normalized_refs.append(ref)

    sources = req.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 5:
        raise ValueError("BAD_SOURCES")
    normalized_sources = []
    seen_platforms = set()
    has_eval_source = False
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("BAD_SOURCE_OBJECT")
        unknown = set(source) - REGISTRATION_SOURCE_KEYS
        if unknown:
            raise ValueError("UNKNOWN_SOURCE_FIELDS:" + ",".join(sorted(unknown)))
        platform = str(source.get("platform") or "").upper()
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError("BAD_SOURCE_PLATFORM")
        if platform in seen_platforms:
            raise ValueError("DUPLICATE_SOURCE_PLATFORM")
        seen_platforms.add(platform)
        url = canonicalize_profile_url(platform, source.get("profile_url"))
        evaluation_enabled = bool(source.get("evaluation_enabled", platform in EVALUATION_PLATFORMS))
        monitoring_enabled = bool(source.get("monitoring_enabled", False))
        if evaluation_enabled and platform not in EVALUATION_PLATFORMS:
            raise ValueError("EVALUATION_PLATFORM_NOT_SUPPORTED")
        if monitoring_enabled and platform not in MONITOR_PLATFORMS:
            raise ValueError("MONITOR_PLATFORM_NOT_SUPPORTED")
        priority = source.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 1000:
            raise ValueError("BAD_SOURCE_PRIORITY")
        has_eval_source = has_eval_source or evaluation_enabled
        normalized_sources.append({
            "platform": platform,
            "profile_url": url,
            "enabled": True,
            "evaluation_enabled": evaluation_enabled,
            "monitoring_enabled": monitoring_enabled,
            "priority": priority,
            "verification_status": "VERIFIED",
            "verification_basis": "+".join(normalized_methods),
        })
    if not has_eval_source:
        raise ValueError("NO_EVALUATION_ENABLED_SOURCE")

    monitoring_enabled = any(s["monitoring_enabled"] for s in normalized_sources)
    verified_at = now_iso()
    return {
        "creator_key": key,
        "display_name": display_name,
        "status": "ACTIVE",
        "monitoring_enabled": monitoring_enabled,
        "verification": {
            "status": "VERIFIED",
            "verified_at": verified_at,
            "basis": "+".join(normalized_methods),
            "methods": normalized_methods,
            "references": normalized_refs,
            "references_are_passive": True,
        },
        "sources": normalized_sources,
    }


def _functional_profile(profile: dict) -> dict:
    verification = profile.get("verification") or {}
    return {
        "creator_key": profile.get("creator_key"),
        "display_name": profile.get("display_name"),
        "status": profile.get("status"),
        "monitoring_enabled": bool(profile.get("monitoring_enabled", False)),
        "verification": {
            "status": verification.get("status"),
            "basis": verification.get("basis"),
            "methods": verification.get("methods", []),
            "references": verification.get("references", []),
        },
        "sources": profile.get("sources", []),
    }


def register_creator(root: Path, req: dict) -> dict:
    root = root.resolve()
    path = root / "control" / "creator_registry.json"
    registry = load_registry(root)
    profile = normalize_registration_request(req)
    key = profile["creator_key"]
    existing = registry["creators"].get(key)
    if existing:
        candidate = {**profile, "verification": {**profile["verification"], "verified_at": (existing.get("verification") or {}).get("verified_at", profile["verification"]["verified_at"])}}
        if _functional_profile(existing) == _functional_profile(candidate):
            return {"result": "ALREADY_REGISTERED", "creator_key": key, "registry_changed": False, "source_count": len(profile["sources"])}
        raise ValueError("CREATOR_KEY_CONFLICT")

    profile["registration"] = {
        "request_id": str(req.get("request_id") or ""),
        "issued_by": str(req.get("issued_by") or "UNKNOWN")[:100],
        "registered_at": now_iso(),
        "registration_path": "MCP_REGISTER_CREATOR",
    }
    merged = {**registry, "updated_at": now_iso(), "creators": {**registry["creators"], key: profile}}
    merged = validate_registry(merged)
    atomic_json(path, merged)
    return {"result": "REGISTERED", "creator_key": key, "registry_changed": True, "source_count": len(profile["sources"])}


def load_registry(root: Path) -> dict:
    path = root / "control" / "creator_registry.json"
    if not path.exists():
        return {"schema_version": REGISTRY_SCHEMA_VERSION, "creators": {}}
    return validate_registry(load_json(path))


def get_creator(root: Path, creator_key: str) -> dict:
    key = str(creator_key).strip().lower()
    if not CREATOR_KEY_RE.fullmatch(key):
        raise KeyError("BAD_CREATOR_KEY")
    registry = load_registry(root)
    profile = registry["creators"].get(key)
    if not profile or profile.get("status") != "ACTIVE":
        raise KeyError("CREATOR_NOT_REGISTERED")
    return profile


def select_evaluation_source(profile: dict, platform: str | None = None) -> dict:
    wanted = str(platform).upper() if platform else None
    sources = [
        s for s in profile.get("sources", [])
        if s.get("enabled") and s.get("evaluation_enabled") and s.get("platform") in EVALUATION_PLATFORMS
        and (wanted is None or s.get("platform") == wanted)
    ]
    if not sources:
        raise KeyError("NO_REGISTERED_EVALUATION_SOURCE")
    return sorted(sources, key=lambda s: (int(s.get("priority", 100)), s.get("platform")))[0]


def select_monitor_sources(profile: dict) -> list[dict]:
    if not profile.get("monitoring_enabled"):
        return []
    return sorted([
        s for s in profile.get("sources", [])
        if s.get("enabled") and s.get("monitoring_enabled") and s.get("platform") in MONITOR_PLATFORMS
    ], key=lambda s: (int(s.get("priority", 100)), s.get("platform")))
