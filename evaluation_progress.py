from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNNING_PHASES = {
    "DISCOVERY",
    "SELECTION",
    "INGESTION",
    "TRANSCRIPTION",
    "EVIDENCE",
    "QUEUE_WRITE",
    "FINALIZING",
}
TERMINAL_PHASES = {"COMPLETE", "FAILED", "STOPPED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def heartbeat(path: Path, phase: str, **metrics: Any) -> dict:
    phase = str(phase or "").upper()
    if phase not in RUNNING_PHASES:
        raise ValueError(f"BAD_EVALUATION_PHASE:{phase}")
    status = _load(path)
    now = utc_now()
    progress = status.get("progress")
    if not isinstance(progress, dict):
        progress = {}
    progress.update(metrics)
    progress["phase"] = phase
    progress["heartbeat_at"] = now
    status["state"] = "RUNNING"
    status["updated_at"] = now
    status["progress"] = progress
    _atomic(path, status)
    return status


def terminalize(path: Path, state: str, *, terminal_reason: str | None = None, **fields: Any) -> dict:
    state = str(state or "").upper()
    if state not in TERMINAL_PHASES and state != "PARTIAL":
        raise ValueError(f"BAD_EVALUATION_TERMINAL_STATE:{state}")
    status = _load(path)
    now = utc_now()
    progress = status.get("progress")
    if not isinstance(progress, dict):
        progress = {}
    progress["phase"] = state
    progress["heartbeat_at"] = now
    if terminal_reason:
        progress["terminal_reason"] = terminal_reason
    status.update(fields)
    status["state"] = state
    status["updated_at"] = now
    status["finished_at"] = fields.get("finished_at") or now
    status["progress"] = progress
    _atomic(path, status)
    return status


def sample_outcome(
    requested_sample_size: int,
    selected_count: int,
    completed_count: int,
    failed_count: int,
) -> tuple[bool, str | None]:
    requested = max(1, int(requested_sample_size))
    selected = max(0, int(selected_count))
    completed = max(0, int(completed_count))
    failed = max(0, int(failed_count))
    if completed >= requested:
        return True, None
    if failed:
        return False, "DOWNSTREAM_PROCESSING_FAILURES"
    if selected < requested:
        return False, f"ONLY_{completed}_ELIGIBLE_ITEMS_AVAILABLE"
    return False, "SAMPLE_NOT_COMPLETE"
