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
RUNTIME_SOURCE_METADATA_KEYS = {
    "discovery_step",
    "max_catalog",
    "discovery_seed_video_urls",
    "discovery_seed_basis",
    "evaluation_video_ids",
    "required_attribution_term",
    "shared_channel",
}
REGISTRATION_SOURCE_KEYS = {
    "platform", "profile_url", "evaluation_enabled", "monitoring_enabled", "priority",
    *RUNTIME_SOURCE_METADATA_KEYS,
}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
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


def _creator_monitoring_enabled(status: str, sources: list[dict]) -> bool:
    """Creator-level monitoring is derived from active source-level monitoring."""
    if str(status or "").upper() != "ACTIVE":
        return False
    return any(
        bool(source.get("enabled", True)) and bool(source.get("monitoring_enabled", False))
        for source in sources
    )


def _creator_state_snapshot(profile: dict) -> dict:
    return {
        "creator_key": profile.get("creator_key"),
        "status": str(profile.get("status", "ACTIVE")).upper(),
        "monitoring_enabled": bool(profile.get("monitoring_enabled", False)),
        "sources": [
            {
                "platform": source.get("platform"),
                "enabled": bool(source.get("enabled", True)),
                "evaluation_enabled": bool(source.get("evaluation_enabled", False)),
                "monitoring_enabled": bool(source.get("monitoring_enabled", False)),
                "priority": int(source.get("priority", 100)),
            }
            for source in sorted(
                profile.get("sources", []),
                key=lambda source: str(source.get("platform") or ""),
            )
        ],
        **({"retirement": profile.get("retirement")} if profile.get("retirement") else {}),
    }


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
        if status not in {"ACTIVE", "DISABLED", "RETIRED"}:
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
            runtime_metadata = _normalize_runtime_source_metadata(platform, url, source)
            normalized_sources.append({
                **source,
                "platform": platform,
                "profile_url": url,
                "enabled": bool(source.get("enabled", True)),
                "evaluation_enabled": bool(source.get("evaluation_enabled", platform in EVALUATION_PLATFORMS)),
                "monitoring_enabled": bool(source.get("monitoring_enabled", False)),
                "priority": int(source.get("priority", 100)),
                "verification_status": "VERIFIED",
                **runtime_metadata,
            })
        normalized[key] = {
            **profile,
            "creator_key": key,
            "display_name": display_name,
            "status": status,
            "monitoring_enabled": _creator_monitoring_enabled(status, normalized_sources),
            "sources": normalized_sources,
        }
    return {**obj, "creators": normalized}


def _normalize_runtime_source_metadata(platform: str, profile_url: str, source: dict) -> dict:
    out: dict = {}
    tiktok_only = {
        "discovery_step", "max_catalog", "discovery_seed_video_urls", "discovery_seed_basis"
    }
    youtube_only = {"evaluation_video_ids", "required_attribution_term", "shared_channel"}

    if platform != "TIKTOK" and any(key in source for key in tiktok_only):
        raise ValueError("TIKTOK_RUNTIME_METADATA_ON_NON_TIKTOK_SOURCE")
    if platform != "YOUTUBE" and any(key in source for key in youtube_only):
        raise ValueError("YOUTUBE_RUNTIME_METADATA_ON_NON_YOUTUBE_SOURCE")

    if platform == "TIKTOK":
        for key, upper in (("discovery_step", 1000), ("max_catalog", 10000)):
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise ValueError(f"BAD_{key.upper()}")
            out[key] = value

        if "discovery_seed_video_urls" in source:
            raw_seeds = source["discovery_seed_video_urls"]
            if not isinstance(raw_seeds, list) or len(raw_seeds) > 8:
                raise ValueError("BAD_DISCOVERY_SEED_VIDEO_URLS")
            expected_handle = urlparse(profile_url).path.strip("/").lstrip("@").casefold()
            seeds: list[str] = []
            for raw in raw_seeds:
                value = str(raw or "").strip()
                try:
                    parsed = urlparse(value)
                except Exception:
                    raise ValueError("BAD_DISCOVERY_SEED_VIDEO_URL")
                host = (parsed.hostname or "").casefold().rstrip(".")
                parts = [part for part in parsed.path.split("/") if part]
                if (
                    parsed.scheme.casefold() != "https"
                    or not (host == "tiktok.com" or host.endswith(".tiktok.com"))
                    or len(parts) != 3
                    or not parts[0].startswith("@")
                    or parts[0][1:].casefold() != expected_handle
                    or parts[1] != "video"
                    or not parts[2].isdigit()
                ):
                    raise ValueError("BAD_DISCOVERY_SEED_VIDEO_URL")
                canonical = f"https://www.tiktok.com/@{parts[0][1:]}/video/{parts[2]}"
                if canonical not in seeds:
                    seeds.append(canonical)
            out["discovery_seed_video_urls"] = seeds

        if "discovery_seed_basis" in source:
            basis = str(source["discovery_seed_basis"] or "").strip()
            if not basis or len(basis) > 200 or any(ord(ch) < 32 for ch in basis):
                raise ValueError("BAD_DISCOVERY_SEED_BASIS")
            out["discovery_seed_basis"] = basis

    if platform == "YOUTUBE":
        if "evaluation_video_ids" in source:
            raw_ids = source["evaluation_video_ids"]
            if not isinstance(raw_ids, list) or len(raw_ids) > 20:
                raise ValueError("BAD_EVALUATION_VIDEO_IDS")
            video_ids: list[str] = []
            for raw in raw_ids:
                video_id = str(raw or "").strip()
                if not VIDEO_ID_RE.fullmatch(video_id):
                    raise ValueError("BAD_EVALUATION_VIDEO_ID")
                if video_id not in video_ids:
                    video_ids.append(video_id)
            out["evaluation_video_ids"] = video_ids

        if "required_attribution_term" in source:
            term = str(source["required_attribution_term"] or "").strip()
            if not term or len(term) > 120 or any(ord(ch) < 32 for ch in term):
                raise ValueError("BAD_REQUIRED_ATTRIBUTION_TERM")
            out["required_attribution_term"] = term

        if "shared_channel" in source:
            if not isinstance(source["shared_channel"], bool):
                raise ValueError("BAD_SHARED_CHANNEL")
            out["shared_channel"] = source["shared_channel"]

        if out.get("shared_channel") and not out.get("required_attribution_term"):
            raise ValueError("SHARED_CHANNEL_ATTRIBUTION_RULE_MISSING")

    return out


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
        runtime_metadata = _normalize_runtime_source_metadata(platform, url, source)
        normalized_sources.append({
            "platform": platform,
            "profile_url": url,
            "enabled": True,
            "evaluation_enabled": evaluation_enabled,
            "monitoring_enabled": monitoring_enabled,
            "priority": priority,
            "verification_status": "VERIFIED",
            "verification_basis": "+".join(normalized_methods),
            **runtime_metadata,
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


def _functional_profile_without_runtime_metadata(profile: dict) -> dict:
    functional = _functional_profile(profile)
    functional["sources"] = [
        {key: value for key, value in source.items() if key not in RUNTIME_SOURCE_METADATA_KEYS}
        for source in functional["sources"]
    ]
    return functional


def _enrich_runtime_source_metadata(existing: dict, candidate: dict) -> dict:
    existing_sources = {str(source.get("platform")): source for source in existing.get("sources", [])}
    candidate_sources = {str(source.get("platform")): source for source in candidate.get("sources", [])}
    enriched_sources = []

    for source in existing.get("sources", []):
        platform = str(source.get("platform"))
        candidate_source = candidate_sources.get(platform, {})
        enriched = dict(source)
        for key in RUNTIME_SOURCE_METADATA_KEYS:
            if key not in candidate_source:
                continue
            if key in source and source[key] != candidate_source[key]:
                raise ValueError(f"RUNTIME_METADATA_CONFLICT:{platform}:{key}")
            enriched[key] = candidate_source[key]
        enriched_sources.append(enriched)

    if set(existing_sources) != set(candidate_sources):
        raise ValueError("CREATOR_KEY_CONFLICT")

    return {**existing, "sources": enriched_sources}


def _extend_existing_profile_sources(existing: dict, candidate: dict) -> dict:
    """Allow a verified creator registration to add new platforms without mutating existing ones."""
    if (
        existing.get("creator_key") != candidate.get("creator_key")
        or existing.get("display_name") != candidate.get("display_name")
        or existing.get("status") != candidate.get("status")
    ):
        raise ValueError("CREATOR_KEY_CONFLICT")

    existing_sources = {
        str(source.get("platform")): source
        for source in existing.get("sources", [])
    }
    candidate_sources = {
        str(source.get("platform")): source
        for source in candidate.get("sources", [])
    }
    if not set(existing_sources) < set(candidate_sources):
        raise ValueError("CREATOR_KEY_CONFLICT")

    immutable_source_keys = {
        "platform",
        "profile_url",
        "enabled",
        "evaluation_enabled",
        "monitoring_enabled",
        "priority",
    }
    merged_sources = []
    for source in existing.get("sources", []):
        platform = str(source.get("platform"))
        incoming = candidate_sources[platform]
        for key in immutable_source_keys:
            if source.get(key) != incoming.get(key):
                raise ValueError(f"CREATOR_SOURCE_CONFLICT:{platform}:{key}")
        enriched = dict(source)
        for key in RUNTIME_SOURCE_METADATA_KEYS:
            if key not in incoming:
                continue
            if key in source and source[key] != incoming[key]:
                raise ValueError(f"RUNTIME_METADATA_CONFLICT:{platform}:{key}")
            enriched[key] = incoming[key]
        merged_sources.append(enriched)

    for source in candidate.get("sources", []):
        if str(source.get("platform")) not in existing_sources:
            merged_sources.append(source)

    existing_verification = existing.get("verification") or {}
    candidate_verification = candidate.get("verification") or {}
    methods = list(existing_verification.get("methods") or [])
    for method in candidate_verification.get("methods") or []:
        if method not in methods:
            methods.append(method)
    refs = list(existing_verification.get("references") or [])
    for ref in candidate_verification.get("references") or []:
        if ref not in refs:
            refs.append(ref)

    verification = {
        **existing_verification,
        "status": "VERIFIED",
        "basis": "+".join(methods),
        "methods": methods,
        "references": refs,
        "references_are_passive": True,
    }
    return {
        **existing,
        "monitoring_enabled": any(bool(source.get("monitoring_enabled")) for source in merged_sources),
        "verification": verification,
        "sources": merged_sources,
    }


def _operational_registration_shape(profile: dict) -> dict:
    source_keys = {
        "platform",
        "profile_url",
        "enabled",
        "evaluation_enabled",
        "monitoring_enabled",
        "priority",
        *RUNTIME_SOURCE_METADATA_KEYS,
    }
    sources = []
    for source in profile.get("sources", []):
        shaped = {key: source.get(key) for key in source_keys if key in source}
        sources.append(shaped)
    sources.sort(key=lambda source: (str(source.get("platform") or ""), str(source.get("profile_url") or "")))
    return {
        "creator_key": profile.get("creator_key"),
        "display_name": profile.get("display_name"),
        "status": profile.get("status"),
        "monitoring_enabled": bool(profile.get("monitoring_enabled", False)),
        "sources": sources,
    }


def _normalize_supersedes_creator_keys(req: dict, canonical_key: str) -> list[str]:
    raw = req.get("supersedes_creator_keys") or []
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError("BAD_SUPERSEDES_CREATOR_KEYS")
    out: list[str] = []
    for value in raw:
        key = str(value or "").strip().lower()
        if not CREATOR_KEY_RE.fullmatch(key):
            raise ValueError("BAD_SUPERSEDES_CREATOR_KEY")
        if key == canonical_key:
            raise ValueError("CREATOR_CANNOT_SUPERSEDE_ITSELF")
        if key not in out:
            out.append(key)
    return out


def _apply_explicit_supersession(
    registry: dict,
    canonical_key: str,
    alias_keys: list[str],
) -> tuple[dict, list[str]]:
    """Disable explicitly named aliases without inferring identity from handles or platforms."""
    if not alias_keys:
        return registry, []

    canonical = registry["creators"].get(canonical_key)
    if not canonical or str(canonical.get("status", "ACTIVE")).upper() != "ACTIVE":
        raise ValueError("CANONICAL_CREATOR_NOT_ACTIVE")

    creators = dict(registry["creators"])
    disabled: list[str] = []
    for alias_key in alias_keys:
        profile = creators.get(alias_key)
        if profile is None:
            raise ValueError(f"SUPERSEDED_CREATOR_NOT_REGISTERED:{alias_key}")

        status = str(profile.get("status", "ACTIVE")).upper()
        if status == "DISABLED":
            if str(profile.get("superseded_by") or "") == canonical_key:
                continue
            raise ValueError(f"CREATOR_ALREADY_DISABLED:{alias_key}")

        creators[alias_key] = {
            **profile,
            "status": "DISABLED",
            "monitoring_enabled": False,
            "superseded_by": canonical_key,
            "disabled_at": now_iso(),
        }
        disabled.append(alias_key)

    if not disabled:
        return registry, []
    return {**registry, "updated_at": now_iso(), "creators": creators}, disabled


def register_creator(root: Path, req: dict) -> dict:
    root = root.resolve()
    path = root / "control" / "creator_registry.json"
    registry = load_registry(root)
    profile = normalize_registration_request(req)
    key = profile["creator_key"]
    supersedes = _normalize_supersedes_creator_keys(req, key)
    existing = registry["creators"].get(key)

    if existing:
        candidate = {
            **profile,
            "verification": {
                **profile["verification"],
                "verified_at": (existing.get("verification") or {}).get(
                    "verified_at",
                    profile["verification"]["verified_at"],
                ),
            },
        }

        # Verification evidence can evolve independently of source configuration.
        # Treat an operationally identical request as a re-assertion, not a conflict.
        if _operational_registration_shape(existing) == _operational_registration_shape(candidate):
            merged, disabled = _apply_explicit_supersession(registry, key, supersedes)
            if disabled:
                merged = validate_registry(merged)
                atomic_json(path, merged)
                return {
                    "result": "SUPERSEDED",
                    "creator_key": key,
                    "registry_changed": True,
                    "source_count": len(existing["sources"]),
                    "superseded_creator_keys": disabled,
                }
            return {
                "result": "ALREADY_REGISTERED",
                "creator_key": key,
                "registry_changed": False,
                "source_count": len(existing["sources"]),
            }

        existing_platforms = {str(source.get("platform")) for source in existing.get("sources", [])}
        candidate_platforms = {str(source.get("platform")) for source in candidate.get("sources", [])}
        if existing_platforms < candidate_platforms:
            extended = _extend_existing_profile_sources(existing, candidate)
            merged = {
                **registry,
                "updated_at": now_iso(),
                "creators": {**registry["creators"], key: extended},
            }
            merged, disabled = _apply_explicit_supersession(merged, key, supersedes)
            merged = validate_registry(merged)
            atomic_json(path, merged)
            return {
                "result": "EXTENDED",
                "creator_key": key,
                "registry_changed": True,
                "source_count": len(extended["sources"]),
                "superseded_creator_keys": disabled,
            }

        if _functional_profile_without_runtime_metadata(existing) != _functional_profile_without_runtime_metadata(candidate):
            raise ValueError("CREATOR_KEY_CONFLICT")

        enriched = _enrich_runtime_source_metadata(existing, candidate)
        merged = {
            **registry,
            "updated_at": now_iso(),
            "creators": {**registry["creators"], key: enriched},
        }
        merged, disabled = _apply_explicit_supersession(merged, key, supersedes)
        if _functional_profile(existing) == _functional_profile(enriched) and not disabled:
            return {
                "result": "ALREADY_REGISTERED",
                "creator_key": key,
                "registry_changed": False,
                "source_count": len(profile["sources"]),
            }
        merged = validate_registry(merged)
        atomic_json(path, merged)
        return {
            "result": "ENRICHED",
            "creator_key": key,
            "registry_changed": True,
            "source_count": len(profile["sources"]),
            "superseded_creator_keys": disabled,
        }

    profile["registration"] = {
        "request_id": str(req.get("request_id") or ""),
        "issued_by": str(req.get("issued_by") or "UNKNOWN")[:100],
        "registered_at": now_iso(),
        "registration_path": "MCP_REGISTER_CREATOR",
    }
    merged = {
        **registry,
        "updated_at": now_iso(),
        "creators": {**registry["creators"], key: profile},
    }
    merged, disabled = _apply_explicit_supersession(merged, key, supersedes)
    merged = validate_registry(merged)
    atomic_json(path, merged)
    return {
        "result": "REGISTERED",
        "creator_key": key,
        "registry_changed": True,
        "source_count": len(profile["sources"]),
        "superseded_creator_keys": disabled,
    }


def load_registry(root: Path) -> dict:
    path = root / "control" / "creator_registry.json"
    if not path.exists():
        return {"schema_version": REGISTRY_SCHEMA_VERSION, "creators": {}}
    return validate_registry(load_json(path))


def update_creator(root: Path, req: dict) -> dict:
    root = root.resolve()
    key = str(req.get("creator_key") or "").strip().lower()
    if not CREATOR_KEY_RE.fullmatch(key):
        raise ValueError("BAD_CREATOR_KEY")

    raw_patches = req.get("sources")
    if not isinstance(raw_patches, list) or not 1 <= len(raw_patches) <= len(SUPPORTED_PLATFORMS):
        raise ValueError("BAD_SOURCE_UPDATES")

    registry = load_registry(root)
    profile = registry["creators"].get(key)
    if profile is None:
        return {
            "result": "NOT_FOUND",
            "creator_key": key,
            "changed": False,
            "timestamp": now_iso(),
        }
    if str(profile.get("status", "ACTIVE")).upper() != "ACTIVE":
        raise ValueError(f"CREATOR_NOT_ACTIVE:{str(profile.get('status') or '').upper()}")

    allowed_fields = {"platform", "enabled", "evaluation_enabled", "monitoring_enabled", "priority"}
    source_by_platform = {
        str(source.get("platform") or "").upper(): dict(source)
        for source in profile.get("sources", [])
    }
    patches_by_platform: dict[str, dict] = {}
    for patch in raw_patches:
        if not isinstance(patch, dict):
            raise ValueError("BAD_SOURCE_UPDATE")
        unknown = set(patch) - allowed_fields
        if unknown:
            raise ValueError("UNKNOWN_SOURCE_UPDATE_FIELDS:" + ",".join(sorted(unknown)))
        platform = str(patch.get("platform") or "").upper()
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError("BAD_SOURCE_PLATFORM")
        if platform in patches_by_platform:
            raise ValueError("DUPLICATE_SOURCE_PLATFORM")
        if platform not in source_by_platform:
            raise ValueError(f"SOURCE_NOT_FOUND:{platform}")

        normalized = {"platform": platform}
        for field in ("enabled", "evaluation_enabled", "monitoring_enabled"):
            if field not in patch:
                continue
            value = patch[field]
            if not isinstance(value, bool):
                raise ValueError(f"BAD_{field.upper()}")
            if field == "evaluation_enabled" and value and platform not in EVALUATION_PLATFORMS:
                raise ValueError("EVALUATION_PLATFORM_NOT_SUPPORTED")
            if field == "monitoring_enabled" and value and platform not in MONITOR_PLATFORMS:
                raise ValueError("MONITOR_PLATFORM_NOT_SUPPORTED")
            normalized[field] = value
        if "priority" in patch:
            priority = patch["priority"]
            if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 1000:
                raise ValueError("BAD_SOURCE_PRIORITY")
            normalized["priority"] = priority
        if len(normalized) == 1:
            raise ValueError("EMPTY_SOURCE_UPDATE")
        patches_by_platform[platform] = normalized

    previous_state = _creator_state_snapshot(profile)
    changed_sources: list[dict] = []
    updated_sources: list[dict] = []
    for source in profile.get("sources", []):
        platform = str(source.get("platform") or "").upper()
        patch = patches_by_platform.get(platform)
        if patch is None:
            updated_sources.append(dict(source))
            continue
        updated = dict(source)
        changes: dict[str, dict] = {}
        for field, value in patch.items():
            if field == "platform":
                continue
            old_value = updated.get(field)
            if old_value != value:
                updated[field] = value
                changes[field] = {"from": old_value, "to": value}
        updated_sources.append(updated)
        if changes:
            changed_sources.append({"platform": platform, "changes": changes})

    if not changed_sources:
        return {
            "result": "NO_OP",
            "creator_key": key,
            "changed": False,
            "previous_state": previous_state,
            "resulting_state": previous_state,
            "changed_sources": [],
            "timestamp": now_iso(),
        }

    changed_at = now_iso()
    updated_profile = {
        **profile,
        "sources": updated_sources,
        "monitoring_enabled": _creator_monitoring_enabled("ACTIVE", updated_sources),
        "last_mutation": {
            "operation": "creator_update",
            "at": changed_at,
            "issued_by": str(req.get("issued_by") or "UNKNOWN")[:100],
            "changed_sources": [item["platform"] for item in changed_sources],
        },
    }
    merged = validate_registry({
        **registry,
        "updated_at": changed_at,
        "creators": {**registry["creators"], key: updated_profile},
    })
    atomic_json(root / "control" / "creator_registry.json", merged)
    resulting = merged["creators"][key]
    return {
        "result": "UPDATED",
        "creator_key": key,
        "changed": True,
        "previous_state": previous_state,
        "resulting_state": _creator_state_snapshot(resulting),
        "changed_sources": changed_sources,
        "timestamp": changed_at,
    }


def retire_creator(root: Path, creator_key: str, reason: str, *, issued_by: str = "UNKNOWN") -> dict:
    root = root.resolve()
    key = str(creator_key or "").strip().lower()
    if not CREATOR_KEY_RE.fullmatch(key):
        raise ValueError("BAD_CREATOR_KEY")
    reason = str(reason or "").strip()
    if not reason or len(reason) > 500 or any(ord(ch) < 32 for ch in reason):
        raise ValueError("BAD_RETIRE_REASON")

    registry = load_registry(root)
    profile = registry["creators"].get(key)
    if profile is None:
        return {
            "result": "NOT_FOUND",
            "creator_key": key,
            "changed": False,
            "timestamp": now_iso(),
        }

    previous_state = _creator_state_snapshot(profile)
    status = str(profile.get("status", "ACTIVE")).upper()
    if status == "RETIRED":
        return {
            "result": "ALREADY_RETIRED",
            "creator_key": key,
            "changed": False,
            "previous_state": previous_state,
            "resulting_state": previous_state,
            "changed_sources": [],
            "timestamp": now_iso(),
            "reason": (profile.get("retirement") or {}).get("reason"),
            "retirement": profile.get("retirement"),
        }
    if status != "ACTIVE":
        raise ValueError(f"CREATOR_NOT_ACTIVE:{status}")

    changed_sources: list[dict] = []
    retired_sources: list[dict] = []
    for source in profile.get("sources", []):
        updated = dict(source)
        changes: dict[str, dict] = {}
        for field in ("monitoring_enabled", "evaluation_enabled", "enabled"):
            old_value = bool(updated.get(field, field == "enabled"))
            if old_value:
                updated[field] = False
                changes[field] = {"from": True, "to": False}
            else:
                updated[field] = False
        retired_sources.append(updated)
        if changes:
            changed_sources.append({
                "platform": str(source.get("platform") or "").upper(),
                "changes": changes,
            })

    retired_at = now_iso()
    retirement = {
        "retired_at": retired_at,
        "reason": reason,
        "retired_by": str(issued_by or "UNKNOWN")[:100],
    }
    retired_profile = {
        **profile,
        "status": "RETIRED",
        "monitoring_enabled": False,
        "sources": retired_sources,
        "retirement": retirement,
        "last_mutation": {
            "operation": "creator_retire",
            "at": retired_at,
            "issued_by": retirement["retired_by"],
            "changed_sources": [item["platform"] for item in changed_sources],
        },
    }
    merged = validate_registry({
        **registry,
        "updated_at": retired_at,
        "creators": {**registry["creators"], key: retired_profile},
    })
    atomic_json(root / "control" / "creator_registry.json", merged)
    resulting = merged["creators"][key]
    return {
        "result": "RETIRED",
        "creator_key": key,
        "changed": True,
        "previous_state": previous_state,
        "resulting_state": _creator_state_snapshot(resulting),
        "changed_sources": changed_sources,
        "timestamp": retired_at,
        "reason": reason,
        "retirement": retirement,
    }


def get_creator(root: Path, creator_key: str, *, include_inactive: bool = False) -> dict:
    key = str(creator_key).strip().lower()
    if not CREATOR_KEY_RE.fullmatch(key):
        raise KeyError("BAD_CREATOR_KEY")
    registry = load_registry(root)
    profile = registry["creators"].get(key)
    if not profile:
        raise KeyError("CREATOR_NOT_REGISTERED")
    if not include_inactive and profile.get("status") != "ACTIVE":
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
