from __future__ import annotations

import json
import os
import runpy
import signal
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
ROOT = Path("/research")
REQUEST_PATH = Path("/research/state/mcp_job_request.json")
RECENT_CHECK_STATUS_PATH = Path("/research/state/creator_recent_check_status.json")
EVALUATION_STATUS_PATH = Path("/research/state/creator_evaluation_status.json")


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


def _atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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


def _arm_evaluation_watchdog(request: dict[str, Any]) -> threading.Event | None:
    if request.get("kind") != "creator_evaluate":
        return None

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
            status = _load_status(EVALUATION_STATUS_PATH)
            state = str(status.get("state") or "").upper()
            if state in {"COMPLETE", "PARTIAL", "FAILED", "STOPPED"}:
                return

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
                continue
            age_seconds = (datetime.now(timezone.utc) - heartbeat_dt).total_seconds()
            if age_seconds < timeout_seconds:
                continue

            finished_at = _now_iso()
            last_phase = str(progress.get("phase") or "UNKNOWN")
            progress.update({
                "phase": "FAILED",
                "heartbeat_at": finished_at,
                "terminal_reason": "NO_PROGRESS_TIMEOUT",
                "last_phase": last_phase,
                "no_progress_timeout_seconds": timeout_seconds,
            })
            status.update({
                "schema_version": int(status.get("schema_version") or 1),
                "state": "FAILED",
                "updated_at": finished_at,
                "finished_at": finished_at,
                "error": "NO_PROGRESS_TIMEOUT",
                "progress": progress,
            })
            transitions = status.get("transitions")
            if not isinstance(transitions, list):
                transitions = []
            transitions.append({
                "event": "JOB_FINALIZED",
                "stage": "WATCHDOG",
                "at": finished_at,
                "final_state": "FAILED",
                "terminal_reason": "NO_PROGRESS_TIMEOUT",
                "last_phase": last_phase,
            })
            status["transitions"] = transitions[-200:]
            try:
                _atomic_json(EVALUATION_STATUS_PATH, status)
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
        1800,
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
            _atomic_json(RECENT_CHECK_STATUS_PATH, status)
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

    if kind == "creator_monitor":
        creator_key = str(params.get("creator_key") or "").strip()
        max_new = int_in_range(params.get("max_new"), 1, 20, "max_new")
        script = APP_DIR / "creator_monitor.py"
        args = ["--root", str(ROOT), "--max-new", str(max_new)]
        if creator_key:
            args.extend(["--creator-key", creator_key])
        return script, args

    if kind == "creator_recent_check":
        scope = params.get("scope")
        window = params.get("window")
        if scope not in ("MONITORED", "ALL_REGISTERED"):
            raise RuntimeError("BAD_SCOPE")
        if window not in ("TODAY", "LAST_7_DAYS", "LAST_N_DAYS"):
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


def main() -> int:
    watchdog_stops: list[threading.Event] = []
    try:
        request = load_request()
        for arm in (_arm_recent_check_watchdog, _arm_evaluation_watchdog):
            stop = arm(request)
            if stop is not None:
                watchdog_stops.append(stop)
        script, args = build_invocation(request)
        return run_script(script, args)
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        for stop in watchdog_stops:
            stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
