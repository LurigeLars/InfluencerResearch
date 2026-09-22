from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

BRIDGE = "instagramresearch-control-v1"
EXPECTED_BRIDGE_VERSION = "0.4.0"
TERMINAL = {"DONE", "FAILED", "REJECTED", "REJECTED_BUSY", "REJECTED_PAUSED", "OK", "NO_ACTIVE_JOB", "STOPPED"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def atomic(path: Path, obj: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def req(action, **extra):
    now = datetime.now(timezone.utc)
    rid = f"acc-{action.lower()}-{uuid.uuid4().hex[:12]}"
    return {
        "schema_version": 1,
        "bridge": BRIDGE,
        "state": "PENDING",
        "request_id": rid,
        "action": action,
        "issued_by": "AVANZA_MCP_ACCEPTANCE",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "reason": "InfluencerResearch v0.11.1 acceptance",
        **extra,
    }


def wait(status_path: Path, rid: str, timeout=900):
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        last = load(status_path)
        if last.get("request_id") == rid and last.get("result") in TERMINAL:
            return last
        time.sleep(1)
    raise RuntimeError(f"timeout waiting for {rid}; last={last}")


def queue_has_creator(queue_path: Path, creator_key: str, run_id: str | None = None) -> bool:
    q = load(queue_path)
    for item in q.get("items", []) if isinstance(q, dict) else []:
        if str(item.get("creator", "")).lower() != creator_key.lower():
            continue
        if item.get("analysis_owner") != "EKONOMI" or item.get("analysis_status") != "PENDING_ANALYSIS":
            continue
        if run_id and item.get("evaluation_run_id") != run_id:
            continue
        return True
    return False


def main():
    root = Path(__file__).resolve().parent.parent
    request_path = root / "control" / "research_bridge_request.json"
    registry_path = root / "control" / "creator_registry.json"
    status_path = root / "state" / "research_bridge_status.json"
    eval_path = root / "state" / "creator_evaluation_status.json"
    reg_status_path = root / "state" / "creator_registration_status.json"
    queue_path = root / "state" / "research_queue.json"

    s = load(status_path)
    if s.get("bridge_version") != EXPECTED_BRIDGE_VERSION:
        raise SystemExit(
            f"bridge_version expected {EXPECTED_BRIDGE_VERSION}, got {s.get('bridge_version')}; "
            "run 12_install_research_bridge.bat first"
        )

    checks = []

    # Real, verified onboarding case. RikaTillsammans' own site links the official
    # YouTube @RikaTillsammansTV channel. Verification refs are passive provenance
    # strings only and are never fetched by the bridge/registration child.
    r = req(
        "REGISTER_CREATOR",
        creator_key="rikatillsammans",
        display_name="RikaTillsammans — Jan & Caroline Bolmeson",
        verification_methods=["OFFICIAL_SITE_CROSSLINK"],
        verification_refs=["https://rikatillsammans.se/om-oss/sociala-medier/"],
        sources=[
            {
                "platform": "YOUTUBE",
                "profile_url": "https://www.youtube.com/@RikaTillsammansTV",
                "evaluation_enabled": True,
                "monitoring_enabled": False,
                "priority": 10,
            },
            {
                "platform": "INSTAGRAM",
                "profile_url": "https://www.instagram.com/rikatillsammans/",
                "evaluation_enabled": False,
                "monitoring_enabled": False,
                "priority": 20,
            },
            {
                "platform": "TIKTOK",
                "profile_url": "https://www.tiktok.com/@rikatillsammans.se",
                "evaluation_enabled": True,
                "monitoring_enabled": False,
                "priority": 30,
            },
        ],
    )
    atomic(request_path, r)
    st = wait(status_path, r["request_id"])
    if st.get("result") != "DONE":
        raise SystemExit(f"creator registration failed: {st}")
    rs = load(reg_status_path)
    if rs.get("result") not in {"REGISTERED", "ALREADY_REGISTERED"} or rs.get("creator_key") != "rikatillsammans":
        raise SystemExit(f"registration status invalid: {rs}")
    registry = load(registry_path)
    profile = (registry.get("creators") or {}).get("rikatillsammans")
    if not profile or (profile.get("verification") or {}).get("status") != "VERIFIED":
        raise SystemExit("rikatillsammans missing/invalid after registration")
    checks.append({"safe_creator_registration": "PASS", "result": rs.get("result")})

    # Duplicate request_id must not execute the registration child again.
    finished_at = rs.get("finished_at")
    atomic(request_path, r)
    time.sleep(4)
    rs2 = load(reg_status_path)
    if rs2.get("finished_at") != finished_at:
        raise SystemExit("duplicate registration request_id executed a second registration child")
    checks.append({"registration_duplicate_request_id": "PASS"})

    # A bad external host must fail before any registry write.
    registry_before = registry_path.read_bytes()
    rbad = req(
        "REGISTER_CREATOR",
        creator_key="acceptancebadhost",
        display_name="Acceptance Bad Host",
        verification_methods=["MULTI_SOURCE_CORROBORATION"],
        verification_refs=["https://example.com/evidence"],
        sources=[{
            "platform": "TIKTOK",
            "profile_url": "https://example.com/@badhost",
            "evaluation_enabled": True,
            "monitoring_enabled": False,
            "priority": 10,
        }],
    )
    atomic(request_path, rbad)
    stb = wait(status_path, rbad["request_id"])
    if stb.get("result") != "REJECTED" or "REGISTRATION_REJECTED" not in str(stb.get("detail")):
        raise SystemExit(f"bad host registration did not fail closed: {stb}")
    if registry_path.read_bytes() != registry_before:
        raise SystemExit("bad host registration mutated creator_registry.json")
    checks.append({"bad_registration_zero_write": "PASS"})

    # Unknown/free runtime-style fields are still rejected.
    rfree = req(
        "REGISTER_CREATOR",
        creator_key="acceptancefreefield",
        display_name="Acceptance Free Field",
        verification_methods=["MULTI_SOURCE_CORROBORATION"],
        verification_refs=["https://example.com/evidence"],
        sources=[{
            "platform": "YOUTUBE",
            "profile_url": "https://www.youtube.com/@example",
            "evaluation_enabled": True,
            "monitoring_enabled": False,
            "priority": 10,
        }],
    )
    rfree["command"] = "whoami"
    atomic(request_path, rfree)
    stf = wait(status_path, rfree["request_id"])
    if stf.get("result") != "REJECTED" or "UNKNOWN_FIELDS:command" not in str(stf.get("detail")):
        raise SystemExit(f"free command field not rejected: {stf}")
    checks.append({"arbitrary_command_field": "PASS"})

    # Registered creator evaluation must run end-to-end and reach Ekonomi's queue.
    reval = req(
        "RUN_CREATOR_EVALUATION",
        creator_key="rikatillsammans",
        sample_size=1,
        source_platform="YOUTUBE",
    )
    atomic(request_path, reval)
    ste = wait(status_path, reval["request_id"])
    if ste.get("result") != "DONE":
        raise SystemExit(f"registered evaluation failed: {ste}")
    ev = load(eval_path)
    run_id = ev.get("evaluation_run_id")
    if not queue_has_creator(queue_path, "rikatillsammans", run_id=run_id):
        raise SystemExit(f"registered evaluation did not produce PENDING_ANALYSIS for Ekonomi; run_id={run_id}")
    checks.append({"registered_eval_to_pending_analysis": "PASS", "run_id": run_id})

    # Unregistered creator remains fail-closed.
    r2 = req("RUN_CREATOR_EVALUATION", creator_key="notregistered", sample_size=1)
    atomic(request_path, r2)
    st2 = wait(status_path, r2["request_id"])
    if st2.get("result") != "REJECTED" or "CREATOR_NOT_REGISTERED" not in str(st2.get("detail")):
        raise SystemExit(f"unregistered creator did not fail closed: {st2}")
    checks.append({"unregistered_creator": "PASS"})

    # Arbitrary profile URL remains forbidden on evaluation action.
    r3 = req("RUN_CREATOR_EVALUATION", creator_key="rikatillsammans", sample_size=1)
    r3["profile_url"] = "https://example.com/evil"
    atomic(request_path, r3)
    st3 = wait(status_path, r3["request_id"])
    if st3.get("result") != "REJECTED" or "UNKNOWN_FIELDS:profile_url" not in str(st3.get("detail")):
        raise SystemExit(f"arbitrary evaluation URL field not rejected: {st3}")
    checks.append({"arbitrary_eval_url_field": "PASS"})

    r4 = req("HEALTH")
    atomic(request_path, r4)
    st4 = wait(status_path, r4["request_id"])
    if st4.get("result") != "OK" or EXPECTED_BRIDGE_VERSION not in str(st4.get("detail")):
        raise SystemExit(f"health failed: {st4}")
    checks.append({"health": "PASS"})

    out = {
        "schema_version": 1,
        "acceptance": "creator_bridge_v0.4",
        "state": "PASS",
        "finished_at": now_iso(),
        "checks": checks,
    }
    atomic(root / "state" / "creator_bridge_acceptance_status.json", out)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
