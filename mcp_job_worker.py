from __future__ import annotations

import json
import os
import runpy
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manifest_store import atomic_write_json


APP_DIR = Path(__file__).resolve().parent
ROOT = Path("/research")
REQUEST_PATH = Path("/research/state/mcp_job_request.json")
RECENT_CHECK_STATUS_PATH = Path("/research/state/creator_recent_check_status.json")
EVALUATION_STATUS_PATH = Path("/research/state/creator_evaluation_status.json")
VIDEO_URL_ANALYSIS_STATUS_PATH = Path("/research/state/video_url_analysis_status.json")
# One recent-check job runs batches until nothing is held back by the cap; the job
# watchdog bounds the whole drain, so it gets room for several batches.
RECENT_CHECK_JOB_TIMEOUT_DEFAULT_SECONDS = 3600
RECENT_CHECK_MAX_ROUNDS = 5
# Errors a rerun cannot repair; mirrors WATERMARK_NONBLOCKING_* in creator_recent_check.
DRAIN_TOLERATED_STAGES = frozenset({"INGESTION_CAP", "STORY_PREFETCH", "STORY_INGESTION"})
DRAIN_TOLERATED_ERROR_PREFIX = "INSUFFICIENT_CONTENT:"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw in {None, ""}:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _load_status(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _kill_worker_group() -> None:
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except Exception:
        os._exit(124)


def evaluation_no_progress_failure(
    status: dict,
    *,
    now: datetime,
    timeout_seconds: int,
) -> dict | None:
    state = str(status.get("state") or "").upper()
    if state in {"COMPLETE", "PARTIAL", "FAILED", "STOPPED"}:
        return None
    progress = status.get("progress")
    if not isinstance(progress, dict):
        progress = {}
    heartbeat_at = (
        progress.get("heartbeat_at")
        or status.get("updated_at")
        or status.get("started_at")
    )
    heartbeat_dt = _parse_iso(heartbeat_at)
    if heartbeat_dt is None:
        return None
    age_seconds = (now.astimezone(timezone.utc) - heartbeat_dt).total_seconds()
    if age_seconds < timeout_seconds:
        return None

    finished_at = now.astimezone(timezone.utc).isoformat()
    last_phase = str(progress.get("phase") or "UNKNOWN")
    progress = dict(progress)
    progress.update({
        "phase": "FAILED",
        "heartbeat_at": finished_at,
        "terminal_reason": "NO_PROGRESS_TIMEOUT",
        "last_phase": last_phase,
        "no_progress_timeout_seconds": timeout_seconds,
    })
    failed = dict(status)
    failed.update({
        "schema_version": int(status.get("schema_version") or 1),
        "state": "FAILED",
        "updated_at": finished_at,
        "finished_at": finished_at,
        "error": "NO_PROGRESS_TIMEOUT",
        "progress": progress,
    })
    transitions = failed.get("transitions")
    if not isinstance(transitions, list):
        transitions = []
    transitions = list(transitions)
    transitions.append({
        "event": "JOB_FINALIZED",
        "stage": "WATCHDOG",
        "at": finished_at,
        "final_state": "FAILED",
        "terminal_reason": "NO_PROGRESS_TIMEOUT",
        "last_phase": last_phase,
    })
    failed["transitions"] = transitions[-200:]
    return failed


def _arm_evaluation_watchdog(request: dict[str, Any]) -> threading.Event | None:
    if request.get("kind") not in {"creator_evaluate", "video_url_analyze"}:
        return None
    status_path = (VIDEO_URL_ANALYSIS_STATUS_PATH
                   if request.get("kind") == "video_url_analyze"
                   else EVALUATION_STATUS_PATH)

    timeout_seconds = _bounded_env_int(
        "INFLUENCER_RESEARCH_EVALUATION_NO_PROGRESS_TIMEOUT_SECONDS",
        600,
        180,
        3600,
    )
    poll_seconds = _bounded_env_int(
        "INFLUENCER_RESEARCH_EVALUATION_WATCHDOG_POLL_SECONDS",
        10,
        5,
        60,
    )
    stop = threading.Event()

    def watchdog() -> None:
        while not stop.wait(poll_seconds):
            status = _load_status(status_path)
            failed = evaluation_no_progress_failure(
                status,
                now=datetime.now(timezone.utc),
                timeout_seconds=timeout_seconds,
            )
            if failed is None:
                if str(status.get("state") or "").upper() in {"COMPLETE", "PARTIAL", "FAILED", "STOPPED"}:
                    return
                continue
            try:
                atomic_write_json(status_path, failed)
            finally:
                _kill_worker_group()

    threading.Thread(
        target=watchdog,
        name="creator-evaluation-no-progress-watchdog",
        daemon=True,
    ).start()
    return stop


def _arm_recent_check_watchdog(request: dict[str, Any]) -> threading.Event | None:
    if request.get("kind") != "creator_recent_check":
        return None

    timeout_seconds = _bounded_env_int(
        "INFLUENCER_RESEARCH_RECENT_CHECK_JOB_TIMEOUT_SECONDS",
        RECENT_CHECK_JOB_TIMEOUT_DEFAULT_SECONDS,
        120,
        7200,
    )
    stop = threading.Event()

    def watchdog() -> None:
        if stop.wait(timeout_seconds):
            return
        status = _load_status(RECENT_CHECK_STATUS_PATH)
        finished_at = _now_iso()
        status.update({
            "schema_version": int(status.get("schema_version") or 1),
            "state": "FAILED",
            "updated_at": finished_at,
            "finished_at": finished_at,
            "error": f"JOB_TIMEOUT:{timeout_seconds}s",
            "progress": {
                "stage": "WATCHDOG",
                "phase": "JOB_TIMEOUT",
                "timeout_seconds": timeout_seconds,
                "wait_reason": "RECENT_CHECK_JOB_EXCEEDED_HARD_DEADLINE",
            },
        })
        transitions = status.get("transitions")
        if not isinstance(transitions, list):
            transitions = []
        transitions.append({
            "event": "JOB_FINALIZED",
            "stage": "WATCHDOG",
            "at": finished_at,
            "final_state": "FAILED",
            "terminal_reason": f"JOB_TIMEOUT:{timeout_seconds}s",
        })
        status["transitions"] = transitions[-200:]
        try:
            atomic_write_json(RECENT_CHECK_STATUS_PATH, status)
        finally:
            _kill_worker_group()

    threading.Thread(
        target=watchdog,
        name="creator-recent-check-job-watchdog",
        daemon=True,
    ).start()
    return stop


def load_request() -> dict[str, Any]:
    obj = json.loads(REQUEST_PATH.read_text(encoding="utf-8-sig"))
    REQUEST_PATH.unlink(missing_ok=True)
    if not isinstance(obj, dict) or obj.get("schema_version") != 1:
        raise RuntimeError("BAD_JOB_REQUEST")
    if not isinstance(obj.get("params"), dict):
        raise RuntimeError("BAD_JOB_PARAMS")
    return obj


def int_in_range(value: Any, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"BAD_{name.upper()}")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise RuntimeError(f"BAD_{name.upper()}")
    return parsed


def build_invocation(request: dict[str, Any]) -> tuple[Path, list[str]]:
    kind = request.get("kind")
    params = request["params"]

    if kind == "creator_evaluate":
        creator_key = str(params.get("creator_key") or "").strip()
        sample_size = int_in_range(params.get("sample_size"), 1, 100, "sample_size")
        source_platform = params.get("source_platform")
        if source_platform not in (None, "YOUTUBE", "TIKTOK"):
            raise RuntimeError("BAD_SOURCE_PLATFORM")
        script = APP_DIR / "influencer_evaluation.py"
        args = ["--root", str(ROOT), "--creator-key", creator_key, "--sample-size", str(sample_size)]
        if source_platform:
            args.extend(["--source-platform", source_platform])
        return script, args

    if kind == "video_url_analyze":
        from single_video_analysis import canonical_video_url
        platform, canonical, _, _ = canonical_video_url(str(params.get("video_url") or ""))
        if params.get("source_platform") != platform:
            raise RuntimeError("VIDEO_PLATFORM_MISMATCH")
        return APP_DIR / "single_video_analysis.py", [
            "--root", str(ROOT), "--video-url", canonical,
        ]

    if kind == "creator_recent_check":
        scope = params.get("scope")
        window = params.get("window")
        if scope not in ("MONITORED", "ALL_REGISTERED"):
            raise RuntimeError("BAD_SCOPE")
        if window not in ("TODAY", "LAST_7_DAYS", "LAST_N_DAYS", "SINCE_LAST_RUN"):
            raise RuntimeError("BAD_WINDOW")
        creator_keys = params.get("creator_keys") or []
        if not isinstance(creator_keys, list) or len(creator_keys) > 50:
            raise RuntimeError("BAD_CREATOR_KEYS")
        normalized_keys = [str(value).strip() for value in creator_keys if str(value).strip()]
        max_items = int_in_range(params.get("max_items"), 1, 20, "max_items")
        script = APP_DIR / "creator_recent_check.py"
        args = ["--root", str(ROOT), "--scope", scope, "--window", window, "--max-items", str(max_items)]
        if normalized_keys:
            args.extend(["--creator-keys", ",".join(normalized_keys)])
        if window == "LAST_N_DAYS":
            lookback_days = int_in_range(params.get("lookback_days"), 1, 90, "lookback_days")
            args.extend(["--lookback-days", str(lookback_days)])
        return script, args

    raise RuntimeError("UNKNOWN_JOB_KIND")


def run_script(script: Path, args: list[str]) -> int:
    previous_argv = sys.argv
    try:
        sys.argv = [str(script), *args]
        try:
            runpy.run_path(str(script), run_name="__main__")
            return 0
        except SystemExit as exc:
            code = exc.code
            return int(code) if isinstance(code, int) else (0 if code is None else 1)
    finally:
        sys.argv = previous_argv


def drain_stop_reason(status: dict) -> str | None:
    """Return why the queue drain stops after this batch, or None to run another batch."""
    state = str(status.get("state") or "").upper()
    if state != "PARTIAL":
        return "QUEUE_EMPTY" if state == "COMPLETE" else f"BATCH_{state or 'UNKNOWN'}"
    if int(status.get("deferred_due_to_cap_count") or 0) <= 0:
        return "NOT_CAP_LIMITED"
    if int(status.get("selected_for_ingestion_count") or 0) <= 0:
        return "NO_PROGRESS"
    if int(status.get("extraction_error_count") or 0) or int(status.get("pending_extraction_count") or 0):
        return "BATCH_ERRORS"
    for error in status.get("errors") or []:
        if not isinstance(error, dict):
            return "BATCH_ERRORS"
        if str(error.get("stage") or "") in DRAIN_TOLERATED_STAGES:
            continue
        if str(error.get("error") or "").startswith(DRAIN_TOLERATED_ERROR_PREFIX):
            continue
        return "BATCH_ERRORS"
    return None


def run_recent_check_drain(
    script: Path,
    args: list[str],
    *,
    status_path: Path = RECENT_CHECK_STATUS_PATH,
    max_rounds: int = RECENT_CHECK_MAX_ROUNDS,
    timeout_seconds: int | None = None,
    run_round=None,
    clock=time.monotonic,
) -> int:
    """Run recent-check batches until the cap holds nothing back, a batch fails, or a bound is hit."""
    if timeout_seconds is None:
        timeout_seconds = _bounded_env_int(
            "INFLUENCER_RESEARCH_RECENT_CHECK_JOB_TIMEOUT_SECONDS",
            RECENT_CHECK_JOB_TIMEOUT_DEFAULT_SECONDS,
            120,
            7200,
        )
    if run_round is None:
        # A fresh process per batch: the ingest modules keep run-scoped globals.
        def run_round() -> int:
            return subprocess.run([sys.executable, str(script), *args], cwd=str(APP_DIR)).returncode

    started = clock()
    longest = 0.0
    rounds: list[dict] = []
    previous_started_at = _load_status(status_path).get("started_at")
    while True:
        round_started = clock()
        rc = run_round()
        longest = max(longest, clock() - round_started)
        status = _load_status(status_path)
        if not status or status.get("started_at") == previous_started_at:
            # The batch died before writing its own status; what is on disk is an older run.
            reason = "BATCH_WROTE_NO_STATUS"
            break
        previous_started_at = status.get("started_at")
        rounds.append({
            "round": len(rounds) + 1,
            "state": status.get("state"),
            "returncode": rc,
            "selected_for_ingestion_count": status.get("selected_for_ingestion_count"),
            "deferred_due_to_cap_count": status.get("deferred_due_to_cap_count"),
        })
        reason = drain_stop_reason(status)
        if reason is None and len(rounds) >= max_rounds:
            reason = "MAX_ROUNDS"
        # Leave room for one more batch as slow as the slowest so far, before the watchdog.
        if reason is None and (clock() - started) + 1.5 * longest >= timeout_seconds:
            reason = "TIME_BUDGET"
        if reason is not None:
            break
    if rounds:
        status["queue_drain"] = {"rounds": rounds, "stop_reason": reason, "max_rounds": max_rounds}
        atomic_write_json(status_path, status)
    return rc


def main() -> int:
    watchdog_stops: list[threading.Event] = []
    try:
        request = load_request()
        for arm in (_arm_recent_check_watchdog, _arm_evaluation_watchdog):
            stop = arm(request)
            if stop is not None:
                watchdog_stops.append(stop)
        script, args = build_invocation(request)
        if request.get("kind") == "creator_recent_check":
            return run_recent_check_drain(script, args)
        return run_script(script, args)
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        for stop in watchdog_stops:
            stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
