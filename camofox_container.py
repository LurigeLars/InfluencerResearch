from __future__ import annotations

import json
import os
from pathlib import Path


CAMOFOX_CONTAINER_SCHEMA_VERSION = 1
CAMOFOX_CONTAINER_BASE_URL = "http://127.0.0.1:9377"
CAMOFOX_CONTAINER_CONFIG_NAME = "camofox-container.json"


def _localappdata_root() -> Path:
    # Do not let an inherited environment variable redefine the security-sensitive
    # host config root. On Windows this is the user's standard Local AppData tree.
    base = Path.home() / "AppData" / "Local"
    return (base / "InfluencerResearch").resolve()


def config_path() -> Path:
    root = _localappdata_root()
    path = (root / CAMOFOX_CONTAINER_CONFIG_NAME).resolve()
    if path.parent != root:
        raise RuntimeError("Camofox container config path escapes the runtime directory")
    return path


def load_config(env: dict[str, str] | None = None) -> dict[str, object]:
    source = os.environ if env is None else env
    if str(source.get("INFLUENCER_RESEARCH_CONTAINER") or "").strip() == "1":
        service_access_key = str(source.get("CAMOFOX_ACCESS_KEY") or "").strip()
        service_admin_key = str(source.get("CAMOFOX_ADMIN_KEY") or "").strip()
        if len(service_access_key) < 32 or len(service_admin_key) < 32:
            raise RuntimeError("Container Camofox keys are missing or too short")
        return {
            "base_url": "http://camofox:9377",
            "access_key": service_access_key,
            "admin_key": service_admin_key,
            "config_path": None,
        }

    path = config_path()
    if not path.is_file():
        raise RuntimeError(
            "Camofox container config is missing. Start the Docker runtime with "
            "scripts\\runtime.ps1 -Action Up."
        )
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read Camofox container config: {exc}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError("Camofox container config must be a JSON object")
    if obj.get("schema_version") != CAMOFOX_CONTAINER_SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported Camofox container config schema: {obj.get('schema_version')!r}"
        )

    access_key = str(obj.get("access_key") or "").strip()
    admin_key = str(obj.get("admin_key") or "").strip()
    if len(access_key) < 32 or len(admin_key) < 32:
        raise RuntimeError("Camofox container keys are missing or too short")

    return {
        "base_url": CAMOFOX_CONTAINER_BASE_URL,
        "access_key": access_key,
        "admin_key": admin_key,
        "config_path": path,
    }
