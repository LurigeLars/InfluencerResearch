from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from creator_registry import get_creator, load_registry, register_creator


SERVER_NAME = "InfluencerResearch"
ROOT = Path("/research")
APP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path("/research/state")
JOB_STATE_PATH = Path("/research/state/mcp_job_status.json")
JOB_REQUEST_PATH = Path("/research/state/mcp_job_request.json")
MCP_PORT = int(os.environ.get("INFLUENCER_RESEARCH_MCP_PORT", "8770"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: dict | None = None) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, json.JSONDecodeError):
        return default or {}


def as_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def summarize_status(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    obj = load_json(path, {})
    allowed = {
        "state",
        "result",
        "started_at",
        "finished_at",
        "creator_key",
        "creator_filter",
        "scope",
        "window",
        "source_count",
        "error_count",
        "errors",
        "completed",
        "completed_ids",
        "failed_ids",
        "failure_count",
        "recent_found_count",
        "selected_for_ingestion_count",
        "queued_for_analysis_count",
        "analysis_targets",
    }
    return {key: obj[key] for key in allowed if key in obj}


class CreatorSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["YOUTUBE", "TIKTOK", "INSTAGRAM"]
    profile_url: str = Field(min_length=8, max_length=500)
    evaluation_enabled: bool | None = None
    monitoring_enabled: bool = False
    priority: int = Field(default=100, ge=1, le=1000)
    discovery_step: int | None = Field(default=None, ge=1, le=1000)
    max_catalog: int | None = Field(default=None, ge=1, le=10000)
    discovery_seed_video_urls: list[str] | None = Field(default=None, max_length=8)
    discovery_seed_basis: str | None = Field(default=None, max_length=200)
    evaluation_video_ids: list[str] | None = Field(default=None, max_length=20)
    required_attribution_term: str | None = Field(default=None, max_length=120)
    shared_channel: bool | None = None


class JobManager:
    STATUS_FILES = {
        "creator_evaluate": Path("/research/state/creator_evaluation_status.json"),
        "creator_monitor": Path("/research/state/creator_monitor_status.json"),
        "creator_recent_check": Path("/research/state/creator_recent_check_status.json"),
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict | None = None
        self._last: dict | None = None

    def _public(self, job: dict) -> dict:
        out = {
            "job_id": job["job_id"],
            "kind": job["kind"],
            "state": job["state"],
            "started_at": job["started_at"],
        }
        if job.get("finished_at"):
            out["finished_at"] = job["finished_at"]
        if job.get("returncode") is not None:
            out["returncode"] = job["returncode"]
        if job.get("status") is not None:
            out["status"] = job["status"]
        return out

    def _cleanup_request(self) -> None:
        try:
            JOB_REQUEST_PATH.unlink(missing_ok=True)
        except OSError:
            # Best-effort cleanup only; a stale request is overwritten before the next job starts.
            return

    def _refresh_locked(self) -> None:
        job = self._active
        if not job:
            return
        proc: subprocess.Popen = job["proc"]
        rc = proc.poll()
        if rc is None:
            return
        self._cleanup_request()
        job["returncode"] = int(rc)
        job["finished_at"] = utc_now()
        job["state"] = "COMPLETE" if rc == 0 else "FAILED"
        job["status"] = summarize_status(job["status_path"])
        public = self._public(job)
        self._last = public
        self._active = None
        atomic_json(JOB_STATE_PATH, public)

    def start(self, kind: str, params: dict) -> dict:
        with self._lock:
            self._refresh_locked()
            if self._active is not None:
                return {
                    "ok": False,
                    "error": "JOB_ALREADY_RUNNING",
                    "job": self._public(self._active),
                }

            if kind not in self.STATUS_FILES:
                return {"ok": False, "error": "UNKNOWN_JOB_KIND"}

            job_id = uuid.uuid4().hex
            request = {
                "schema_version": 1,
                "job_id": job_id,
                "kind": kind,
                "params": params,
            }
            atomic_json(JOB_REQUEST_PATH, request)

            proc = subprocess.Popen(
                [sys.executable, str(APP_DIR / "mcp_job_worker.py")],
                cwd=str(APP_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            job = {
                "job_id": job_id,
                "kind": kind,
                "state": "RUNNING",
                "started_at": utc_now(),
                "finished_at": None,
                "returncode": None,
                "status_path": self.STATUS_FILES[kind],
                "status": None,
                "proc": proc,
            }
            self._active = job
            public = self._public(job)
            atomic_json(JOB_STATE_PATH, public)
            return {"ok": True, "job": public}

    def status(self) -> dict:
        with self._lock:
            self._refresh_locked()
            if self._active is not None:
                job = dict(self._active)
                job["status"] = summarize_status(job["status_path"])
                return {"active": self._public(job), "last": self._last}
            if self._last is not None:
                return {"active": None, "last": self._last}
            persisted = load_json(JOB_STATE_PATH, {})
            return {"active": None, "last": persisted or None}

    def stop(self) -> dict:
        with self._lock:
            self._refresh_locked()
            if self._active is None:
                self._cleanup_request()
                return {"ok": True, "result": "NO_ACTIVE_JOB"}

            job = self._active
            proc: subprocess.Popen = job["proc"]
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=3)
            finally:
                self._cleanup_request()
                job["returncode"] = proc.poll()
                job["finished_at"] = utc_now()
                job["state"] = "STOPPED"
                job["status"] = summarize_status(job["status_path"])
                public = self._public(job)
                self._last = public
                self._active = None
                atomic_json(JOB_STATE_PATH, public)
            return {"ok": True, "job": public}


jobs = JobManager()
atexit.register(jobs.stop)

allowed_hosts = [
    item.strip()
    for item in os.environ.get(
        "INFLUENCER_RESEARCH_ALLOWED_HOSTS",
        "127.0.0.1:*,localhost:*,influencerresearch:*",
    ).split(",")
    if item.strip()
]

security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=allowed_hosts,
    allowed_origins=[],
)

mcp = MCPServer(
    SERVER_NAME,
    instructions="Public-creator research tools. One long-running research job at a time.",
)

READ = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
REGISTER = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
RUN = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
STOP = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)


@mcp.custom_route("/health", methods=["GET"])
async def health_route(_: Request) -> JSONResponse:
    state = jobs.status()
    return JSONResponse({"status": "ok", "active_job": bool(state.get("active"))})


@mcp.tool(description="List registered creators.", annotations=READ, structured_output=False)
def creator_list() -> str:
    registry = load_registry(ROOT)
    creators = []
    for profile in sorted(
        registry["creators"].values(),
        key=lambda value: str(value.get("creator_key")),
    ):
        if profile.get("status") != "ACTIVE":
            continue
        creators.append(
            {
                "creator_key": profile.get("creator_key"),
                "display_name": profile.get("display_name"),
                "monitoring_enabled": bool(profile.get("monitoring_enabled")),
                "platforms": [
                    source.get("platform")
                    for source in profile.get("sources", [])
                    if source.get("enabled")
                ],
            }
        )
    return as_text({"creators": creators})


@mcp.tool(description="Get one registered creator.", annotations=READ, structured_output=False)
def creator_get(creator_key: str) -> str:
    try:
        return as_text({"creator": get_creator(ROOT, creator_key)})
    except Exception as exc:
        return as_text({"error": f"{type(exc).__name__}:{exc}"})


@mcp.tool(description="Register a verified creator.", annotations=REGISTER, structured_output=False)
def creator_register(
    creator_key: str,
    display_name: str,
    sources: list[CreatorSource],
    verification_methods: list[str],
    verification_refs: list[str],
) -> str:
    request = {
        "request_id": "mcp-" + uuid.uuid4().hex,
        "issued_by": "MCP",
        "creator_key": creator_key,
        "display_name": display_name,
        "sources": [source.model_dump(exclude_none=True) for source in sources],
        "verification_methods": verification_methods,
        "verification_refs": verification_refs,
    }
    try:
        return as_text(register_creator(ROOT, request))
    except Exception as exc:
        return as_text(
            {"result": "REJECTED", "error": f"{type(exc).__name__}:{exc}"}
        )


@mcp.tool(description="Start a creator evaluation job.", annotations=RUN, structured_output=False)
def creator_evaluate(
    creator_key: str,
    sample_size: int = 20,
    source_platform: Literal["YOUTUBE", "TIKTOK"] | None = None,
) -> str:
    try:
        profile = get_creator(ROOT, creator_key)
    except Exception as exc:
        return as_text({"ok": False, "error": f"{type(exc).__name__}:{exc}"})
    sample = max(1, min(int(sample_size), 100))
    params = {
        "creator_key": profile["creator_key"],
        "sample_size": sample,
        "source_platform": source_platform,
    }
    return as_text(jobs.start("creator_evaluate", params))


@mcp.tool(
    description="Start monitored-creator discovery and ingestion.",
    annotations=RUN,
    structured_output=False,
)
def creator_monitor(creator_key: str = "", max_new: int = 10) -> str:
    cap = max(1, min(int(max_new), 20))
    normalized = ""
    if creator_key.strip():
        try:
            normalized = str(get_creator(ROOT, creator_key)["creator_key"])
        except Exception as exc:
            return as_text({"ok": False, "error": f"{type(exc).__name__}:{exc}"})
    return as_text(
        jobs.start(
            "creator_monitor",
            {"creator_key": normalized, "max_new": cap},
        )
    )


@mcp.tool(
    description="Start bounded recent-window discovery and ingestion.",
    annotations=RUN,
    structured_output=False,
)
def creator_recent_check(
    scope: Literal["MONITORED", "ALL_REGISTERED"] = "MONITORED",
    creator_keys: list[str] | None = None,
    window: Literal["TODAY", "LAST_7_DAYS", "LAST_N_DAYS"] = "TODAY",
    lookback_days: int | None = None,
    max_items: int = 10,
) -> str:
    cap = max(1, min(int(max_items), 20))
    normalized_keys: list[str] = []
    for value in creator_keys or []:
        if not str(value).strip():
            continue
        try:
            normalized_keys.append(str(get_creator(ROOT, str(value))["creator_key"]))
        except Exception as exc:
            return as_text({"ok": False, "error": f"{type(exc).__name__}:{exc}"})
    normalized_keys = list(dict.fromkeys(normalized_keys))
    lookback: int | None = None
    if window == "LAST_N_DAYS":
        if lookback_days is None:
            return as_text({"ok": False, "error": "LOOKBACK_DAYS_REQUIRED"})
        lookback = max(1, min(int(lookback_days), 90))
    params = {
        "scope": scope,
        "creator_keys": normalized_keys,
        "window": window,
        "lookback_days": lookback,
        "max_items": cap,
    }
    return as_text(jobs.start("creator_recent_check", params))


@mcp.tool(
    description="Get current or last research job status.",
    annotations=READ,
    structured_output=False,
)
def research_status() -> str:
    return as_text(jobs.status())


@mcp.tool(
    description="Stop the active research job.",
    annotations=STOP,
    structured_output=False,
)
def research_stop() -> str:
    return as_text(jobs.stop())


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=MCP_PORT,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
        max_request_body_size=512 * 1024,
    )
