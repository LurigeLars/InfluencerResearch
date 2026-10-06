from __future__ import annotations

import atexit
import base64
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
from mcp.types import (
    ImageContent,
    TextContent,
    ToolAnnotations,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from creator_registry import (
    get_creator,
    load_registry,
    register_creator,
    retire_creator,
    update_creator,
)
from research_status_summary import compact_job_status, summarize_status


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


def _safe_evidence_path(value: str | None) -> Path | None:
    if not value:
        return None
    try:
        root = ROOT.resolve()
        path = (ROOT / Path(str(value))).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path
    except OSError:
        return None


def _video_mime_type(path: Path) -> str:
    return {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
    }.get(path.suffix.casefold(), "application/octet-stream")


def _research_queue_item(queue_id: str) -> dict | None:
    queue = load_json(STATE_DIR / "research_queue.json", {})
    for item in queue.get("items", []):
        if str(item.get("queue_id") or item.get("shortcode") or "") == str(queue_id):
            return item if isinstance(item, dict) else None
    return None


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


class CreatorSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["YOUTUBE", "TIKTOK", "INSTAGRAM"]
    enabled: bool | None = None
    evaluation_enabled: bool | None = None
    monitoring_enabled: bool | None = None
    priority: int | None = Field(default=None, ge=1, le=1000)


def resolve_job_state(kind: str, returncode: int, status: dict | None) -> str:
    if kind == "creator_recent_check" and isinstance(status, dict):
        state = str(status.get("state") or "").upper()
        if state in {"COMPLETE", "PARTIAL", "FAILED", "STOPPED"}:
            return state
    return "COMPLETE" if returncode == 0 else "FAILED"


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
        job["status"] = summarize_status(job["status_path"])
        job["state"] = resolve_job_state(job["kind"], int(rc), job["status"])
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

            # Status files represent the currently running job of each kind.
            # Remove the previous run's file before spawning so research_status()
            # cannot expose stale progress/failure data while the new worker starts.
            status_path = self.STATUS_FILES[kind]
            try:
                status_path.unlink(missing_ok=True)
            except OSError as exc:
                return {
                    "ok": False,
                    "error": f"STATUS_RESET_FAILED:{type(exc).__name__}",
                }

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
                "status_path": status_path,
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

    def _mark_status_stopped(self, job: dict, finished_at: str) -> dict | None:
        status_path = job.get("status_path")
        if not isinstance(status_path, Path):
            return None
        status = load_json(status_path, {})
        if not isinstance(status, dict):
            status = {}
        previous_state = str(status.get("state") or "RUNNING")
        status.update({
            "schema_version": int(status.get("schema_version") or 1),
            "state": "STOPPED",
            "updated_at": finished_at,
            "finished_at": finished_at,
            "stop_reason": "USER_REQUESTED",
        })
        progress = status.get("progress")
        if not isinstance(progress, dict):
            progress = {}
        status["progress"] = {
            **progress,
            "phase": "STOPPED",
            "previous_state": previous_state,
            "terminal_reason": "USER_REQUESTED",
        }
        transitions = status.get("transitions")
        if not isinstance(transitions, list):
            transitions = []
        transitions.append({
            "event": "JOB_FINALIZED",
            "stage": "CANCELLATION",
            "at": finished_at,
            "final_state": "STOPPED",
            "terminal_reason": "USER_REQUESTED",
        })
        status["transitions"] = transitions[-200:]
        atomic_json(status_path, status)
        return summarize_status(status_path)

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
                finished_at = utc_now()
                job["finished_at"] = finished_at
                job["state"] = "STOPPED"
                job["status"] = self._mark_status_stopped(job, finished_at)
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
UPDATE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
RETIRE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
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
def creator_list(include_retired: bool = False) -> str:
    registry = load_registry(ROOT)
    creators = []
    visible_statuses = {"ACTIVE", "RETIRED"} if include_retired else {"ACTIVE"}
    for profile in sorted(
        registry["creators"].values(),
        key=lambda value: str(value.get("creator_key")),
    ):
        status = str(profile.get("status") or "ACTIVE").upper()
        if status not in visible_statuses:
            continue
        creators.append(
            {
                "creator_key": profile.get("creator_key"),
                "display_name": profile.get("display_name"),
                "status": status,
                "monitoring_enabled": bool(profile.get("monitoring_enabled")),
                "platforms": [
                    source.get("platform")
                    for source in profile.get("sources", [])
                    if source.get("enabled")
                ],
            }
        )
    return as_text({"creators": creators, "include_retired": include_retired})


@mcp.tool(description="Get one registered creator, including retired historical records.", annotations=READ, structured_output=False)
def creator_get(creator_key: str) -> str:
    try:
        return as_text({"creator": get_creator(ROOT, creator_key, include_inactive=True)})
    except Exception as exc:
        return as_text({"error": f"{type(exc).__name__}:{exc}"})


@mcp.tool(description="Register a verified creator.", annotations=REGISTER, structured_output=False)
def creator_register(
    creator_key: str,
    display_name: str,
    sources: list[CreatorSource],
    verification_methods: list[str],
    verification_refs: list[str],
    supersedes_creator_keys: list[str] | None = None,
) -> str:
    request = {
        "request_id": "mcp-" + uuid.uuid4().hex,
        "issued_by": "MCP",
        "creator_key": creator_key,
        "display_name": display_name,
        "sources": [source.model_dump(exclude_none=True) for source in sources],
        "verification_methods": verification_methods,
        "verification_refs": verification_refs,
        "supersedes_creator_keys": supersedes_creator_keys or [],
    }
    try:
        return as_text(register_creator(ROOT, request))
    except Exception as exc:
        return as_text(
            {"result": "REJECTED", "error": f"{type(exc).__name__}:{exc}"}
        )


@mcp.tool(
    description="Partially update source-level creator controls. Unspecified fields are preserved and no research job is started.",
    annotations=UPDATE,
    structured_output=False,
)
def creator_update(creator_key: str, sources: list[CreatorSourceUpdate]) -> str:
    request = {
        "issued_by": "MCP",
        "creator_key": creator_key,
        "sources": [source.model_dump(exclude_none=True) for source in sources],
    }
    try:
        return as_text(update_creator(ROOT, request))
    except Exception as exc:
        return as_text({"result": "REJECTED", "error": f"{type(exc).__name__}:{exc}"})


@mcp.tool(
    description="Soft-retire one creator while preserving historical registry/research data and disabling all sources.",
    annotations=RETIRE,
    structured_output=False,
)
def creator_retire(creator_key: str, reason: str) -> str:
    try:
        return as_text(retire_creator(ROOT, creator_key, reason, issued_by="MCP"))
    except Exception as exc:
        return as_text({"result": "REJECTED", "error": f"{type(exc).__name__}:{exc}"})


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
    description=(
        "Return retained evidence for one research-queue item. Use representative frames/screenshots first; "
        "when a Story screenshot remains inconclusive and raw_media_available=true, call mode=RAW_MEDIA for metadata. "
        "Raw video bytes stay outside model context; include_binary is retained for compatibility but never embeds media."
    ),
    annotations=READ,
    structured_output=False,
)
def analysis_evidence_get(
    queue_id: str,
    mode: Literal["REPRESENTATIVE_FRAMES", "CONTACT_SHEET", "RAW_MEDIA"] = "REPRESENTATIVE_FRAMES",
    max_frames: int = 8,
    include_binary: bool = False,
) -> list[TextContent | ImageContent]:
    item = _research_queue_item(queue_id)
    if item is None:
        return [TextContent(type="text", text=as_text({"ok": False, "error": "QUEUE_ITEM_NOT_FOUND", "queue_id": queue_id}))]

    bundle = item.get("agent_visual_bundle") if isinstance(item.get("agent_visual_bundle"), dict) else {}
    metadata = {
        "ok": True,
        "queue_id": queue_id,
        "creator": item.get("creator"),
        "source_platform": item.get("source_platform"),
        "source_url": item.get("source_url"),
        "analysis_mode_recommended": item.get("analysis_mode_recommended"),
        "visual_review_recommended": item.get("visual_review_recommended"),
        "visual_review_reason": item.get("visual_review_reason") or [],
        "creator_visual_prior": bundle.get("creator_visual_prior"),
        "chart_signal_ratio": bundle.get("chart_signal_ratio"),
        "chart_signal_frame_count": bundle.get("chart_signal_frame_count"),
        "raw_media_available": bool(item.get("raw_media_available")),
    }
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=as_text(metadata))
    ]

    if mode == "RAW_MEDIA":
        raw_media = _safe_evidence_path(item.get("video_file"))
        if raw_media is None:
            content.append(
                TextContent(
                    type="text",
                    text=as_text({"warning": "RAW_MEDIA_NOT_AVAILABLE"}),
                )
            )
            return content
        media_bytes = raw_media.stat().st_size
        mime_type = _video_mime_type(raw_media)
        content.append(
            TextContent(
                type="text",
                text=as_text({
                    "fallback": "RAW_MEDIA",
                    "mime_type": mime_type,
                    "bytes": media_bytes,
                    "video_file": item.get("video_file"),
                    "binary_embedded": False,
                    "binary_inline_available": False,
                    "warning": "RAW_MEDIA_BINARY_INLINE_DISABLED" if include_binary else None,
                    "next_step": "Use REPRESENTATIVE_FRAMES or CONTACT_SHEET for bounded model-visible evidence.",
                }),
            )
        )
        return content

    if mode == "CONTACT_SHEET":
        contact = _safe_evidence_path(bundle.get("contact_sheet"))
        if contact is None:
            content.append(TextContent(type="text", text=as_text({"warning": "CONTACT_SHEET_NOT_AVAILABLE"})))
            return content
        content.append(ImageContent(type="image", data=base64.b64encode(contact.read_bytes()).decode("ascii"), mime_type="image/jpeg"))
        return content

    cap = max(1, min(int(max_frames), 12))
    frames = bundle.get("representative_frames") if isinstance(bundle.get("representative_frames"), list) else []
    returned = 0
    for frame in frames:
        if returned >= cap or not isinstance(frame, dict):
            break
        path = _safe_evidence_path(frame.get("file"))
        if path is None:
            continue
        content.append(
            TextContent(
                type="text",
                text=as_text({
                    "timestamp_s": frame.get("timestamp_s"),
                    "visual_score": frame.get("visual_score"),
                    "visual_signals": frame.get("visual_signals") or [],
                    "ocr_text": frame.get("ocr_text"),
                }),
            )
        )
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(path.read_bytes()).decode("ascii"),
                mime_type="image/jpeg",
            )
        )
        returned += 1
    if returned == 0:
        # Instagram Stories may have a single retained screenshot instead of a
        # representative-frame bundle. This is the final model-vision fallback
        # when OCR/Ollama/Gemini extraction could not produce usable text.
        screenshot = _safe_evidence_path(item.get("screenshot_file"))
        if screenshot is not None:
            suffix = screenshot.suffix.casefold()
            mime_type = "image/png" if suffix == ".png" else "image/jpeg"
            content.append(
                TextContent(
                    type="text",
                    text=as_text({
                        "fallback": "STORY_SCREENSHOT",
                        "analysis_content_reason": item.get("analysis_content_reason"),
                        "visual_description_status": item.get("visual_description_status"),
                    }),
                )
            )
            content.append(
                ImageContent(
                    type="image",
                    data=base64.b64encode(screenshot.read_bytes()).decode("ascii"),
                    mime_type=mime_type,
                )
            )
            return content
        content.append(TextContent(type="text", text=as_text({"warning": "REPRESENTATIVE_FRAMES_NOT_AVAILABLE"})))
    return content


@mcp.tool(
    description="Get current or last research job status.",
    annotations=READ,
    structured_output=False,
)
def research_status(include_details: bool = False) -> str:
    """Get current or last research job status. Compact by default; set include_details=true for full retained detail."""
    value = jobs.status()
    return as_text(value if include_details else compact_job_status(value))


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
