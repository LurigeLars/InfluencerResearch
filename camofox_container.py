from __future__ import annotations

import json
import os
from pathlib import Path

CAMOFOX_CONTAINER_SCHEMA_VERSION = 1
CAMOFOX_CONTAINER_BASE_URL = "http://127.0.0.1:9377"
CAMOFOX_CONTAINER_CONFIG_NAME = "camofox-container.json"
CAMOFOX_CONTAINER_TRANSFER_NAME = "camofox-transfer"


def _localappdata_root(env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    raw = str(source.get("LOCALAPPDATA") or "").strip()
    if not raw:
        raise RuntimeError("LOCALAPPDATA is required for the Camofox container runtime")
    return Path(raw).resolve() / "InfluencerResearch"


def config_path(env: dict[str, str] | None = None) -> Path:
    return _localappdata_root(env) / CAMOFOX_CONTAINER_CONFIG_NAME


def transfer_dir(env: dict[str, str] | None = None) -> Path:
    return _localappdata_root(env) / CAMOFOX_CONTAINER_TRANSFER_NAME


def load_config(env: dict[str, str] | None = None) -> dict[str, object]:
    path = config_path(env)
    if not path.is_file():
        raise RuntimeError(
            "Camofox container config is missing. Run "
            "pwsh -NoProfile -File scripts\\camofox_container.ps1 -Action Up"
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

    xfer = transfer_dir(env)
    xfer.mkdir(parents=True, exist_ok=True)
    return {
        "base_url": CAMOFOX_CONTAINER_BASE_URL,
        "access_key": access_key,
        "admin_key": admin_key,
        "transfer_dir": xfer,
        "config_path": path,
    }
