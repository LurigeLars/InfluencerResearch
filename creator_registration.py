from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from creator_registry import atomic_json, register_creator

REGISTRATION_VERSION = "0.1.0"
BRIDGE_NAME = "instagramresearch-control-v1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> int:
    ap = argparse.ArgumentParser(description="InfluencerResearch safe creator registration")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--request-id", required=True)
    args = ap.parse_args()

    root = args.root.resolve()
    request_path = root / "control" / "research_bridge_request.json"
    status_path = root / "state" / "creator_registration_status.json"
    started = now_iso()
    atomic_json(status_path, {
        "schema_version": 1,
        "registration_version": REGISTRATION_VERSION,
        "state": "RUNNING",
        "request_id": args.request_id,
        "started_at": started,
    })

    try:
        req = load_json(request_path)
        if not isinstance(req, dict):
            raise ValueError("REQUEST_NOT_OBJECT")
        if req.get("bridge") != BRIDGE_NAME:
            raise ValueError("BAD_BRIDGE")
        if str(req.get("action", "")).upper() != "REGISTER_CREATOR":
            raise ValueError("REQUEST_ACTION_CHANGED")
        if str(req.get("request_id", "")) != args.request_id:
            raise ValueError("REQUEST_ID_CHANGED")
        result = register_creator(root, req)
        out = {
            "schema_version": 1,
            "registration_version": REGISTRATION_VERSION,
            "state": "COMPLETE",
            "request_id": args.request_id,
            "started_at": started,
            "finished_at": now_iso(),
            **result,
        }
        atomic_json(status_path, out)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        out = {
            "schema_version": 1,
            "registration_version": REGISTRATION_VERSION,
            "state": "FAILED",
            "request_id": args.request_id,
            "started_at": started,
            "finished_at": now_iso(),
            "result": "REJECTED",
            "error": f"{type(exc).__name__}: {exc}",
        }
        atomic_json(status_path, out)
        print(json.dumps(out, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
