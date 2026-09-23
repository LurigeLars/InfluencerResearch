from __future__ import annotations

import json
import runpy
import sys
import traceback
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
ROOT = Path("/research")
REQUEST_PATH = Path("/research/state/mcp_job_request.json")


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
    try:
        request = load_request()
        script, args = build_invocation(request)
        return run_script(script, args)
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
