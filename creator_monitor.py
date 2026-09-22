from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from creator_registry import get_creator, load_registry, select_monitor_sources
import youtube_creator_evaluation as yte
import tiktok_camofox_sync as tts

MONITOR_VERSION = "0.1.5"
MAX_SEEN_IDS = 1000
VALID_SOURCE_RESULTS = frozenset({"BASELINED", "NO_NEW", "INGESTED", "PARTIAL", "FAILED"})
DEGRADED_SOURCE_RESULTS = frozenset({"PARTIAL", "FAILED"})


def _classify_ingestion_result(
    selected: list[str], completed_ids: set[str] | list[str], had_failure: bool
) -> tuple[str, set[str], list[str]]:
    """Return a fail-closed adapter result for the current selected IDs."""
    selected_set = set(str(x) for x in selected)
    completed_set = set(str(x) for x in completed_ids) & selected_set
    failed_ids = [str(x) for x in selected if str(x) not in completed_set]
    if had_failure or failed_ids:
        return ("PARTIAL" if completed_set else "FAILED"), completed_set, failed_ids
    return "INGESTED", completed_set, []


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def source_key(creator_key: str, source: dict) -> str:
    return f"{creator_key}:{source['platform']}"


def manifest_done_ids(root: Path, platform: str) -> set[str]:
    manifest = load_json(root / "state" / "manifest.json", {"items": {}})
    prefix = "yt_" if platform == "YOUTUBE" else "tt_"
    out = set()
    for key, item in (manifest.get("items") or {}).items():
        if not str(key).startswith(prefix) or not isinstance(item, dict):
            continue
        if item.get("download_status") == "DONE" and item.get("transcription_status") == "DONE":
            if platform != "YOUTUBE" or item.get("visual_evidence_status") == "DONE":
                out.add(str(key)[3:])
    return out


def monitor_youtube(root: Path, profile: dict, source: dict, state_entry: dict, max_new: int) -> tuple[dict, dict]:
    entries, diag = yte.enumerate_channel(source["profile_url"], limit=max(50, max_new * 4))
    current_ids = [str(e["id"]) for e in entries if e.get("id")]
    seen = set(str(x) for x in state_entry.get("seen_ids", []))
    if diag.get("ok") is not True:
        result = "PARTIAL" if current_ids else "FAILED"
        retryable_ids = [x for x in current_ids if x not in seen]
        new_state = {
            **state_entry,
            "last_checked_at": now_iso(),
            "last_result": result,
        }
        return {
            "platform": "YOUTUBE",
            "result": result,
            "feed_items": len(current_ids),
            "new_items": len(retryable_ids) if state_entry.get("initialized_at") else 0,
            "completed_ids": [],
            "failed_ids": retryable_ids[:max_new],
            "returncode": diag.get("returncode"),
            "discovery_error": diag.get("error") or diag.get("diagnostic_tail") or "YouTube discovery did not complete successfully",
            "discovery": diag,
        }, new_state
    if not state_entry.get("initialized_at"):
        new_state = {
            **state_entry,
            "initialized_at": now_iso(),
            "seen_ids": current_ids[:MAX_SEEN_IDS],
            "last_checked_at": now_iso(),
            "last_result": "BASELINED",
        }
        return {"platform": "YOUTUBE", "result": "BASELINED", "feed_items": len(current_ids), "new_items": 0, "discovery": diag}, new_state

    new_ids = [x for x in current_ids if x not in seen]
    if not new_ids:
        new_state = {**state_entry, "last_checked_at": now_iso(), "last_result": "NO_NEW"}
        return {"platform": "YOUTUBE", "result": "NO_NEW", "feed_items": len(current_ids), "new_items": 0, "discovery": diag}, new_state

    selected = new_ids[:max_new]
    already_done = manifest_done_ids(root, "YOUTUBE")
    to_process = [x for x in selected if x not in already_done]
    completed_ids = [x for x in selected if x in already_done]
    eval_status = None
    rc = 0
    if to_process:
        cmd = [
            sys.executable, str(root / "app" / "influencer_evaluation.py"),
            "--root", str(root), "--creator-key", profile["creator_key"],
            "--source-platform", "YOUTUBE", "--sample-size", str(len(to_process)),
            "--only-video-ids", ",".join(to_process),
        ]
        p = subprocess.run(cmd, cwd=str(root / "app"))
        rc = int(p.returncode)
        eval_status = load_json(root / "state" / "creator_evaluation_status.json", {})
        completed_ids.extend([str(x)[3:] for x in eval_status.get("completed", []) if str(x).startswith("yt_")])

    result, completed_set, failed_ids = _classify_ingestion_result(
        selected, completed_ids, had_failure=rc != 0
    )
    updated_seen = seen | completed_set
    merged_seen = [x for x in current_ids if x in updated_seen] + [x for x in seen if x not in set(current_ids)]
    new_state = {
        **state_entry,
        "seen_ids": merged_seen[:MAX_SEEN_IDS],
        "last_checked_at": now_iso(),
        "last_result": result,
    }
    return {
        "platform": "YOUTUBE", "result": result, "feed_items": len(current_ids),
        "new_items": len(new_ids), "selected": selected, "completed_ids": sorted(completed_set),
        "failed_ids": failed_ids, "returncode": rc,
        "evaluation_state": (eval_status or {}).get("state"), "discovery": diag,
    }, new_state


def _tiktok_catalog_ids(root: Path, creator_key: str) -> list[str]:
    p = root / "state" / "tiktok" / f"{creator_key}_catalog.json"
    obj = load_json(p, {"order": []})
    return [str(x) for x in obj.get("order", []) if x]



def _parse_iso_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _tiktok_published_at(video_id: str) -> datetime:
    # TikTok video IDs are 64-bit IDs whose high 32 bits encode Unix creation time.
    value = int(str(video_id))
    return datetime.fromtimestamp(value >> 32, tz=timezone.utc)

def _partition_tiktok_unseen(unseen_ids: list[str], baseline_at: datetime) -> tuple[list[str], list[str], list[str]]:
    """Classify unseen TikTok IDs relative to the monitor baseline.

    IDs published on/before the baseline are historical catalogue gaps and must
    never be treated as newly published monitored content.
    """
    eligible_ids: list[str] = []
    historical_ids: list[str] = []
    invalid_time_ids: list[str] = []
    for video_id in unseen_ids:
        try:
            published_at = _tiktok_published_at(video_id)
        except Exception:
            invalid_time_ids.append(str(video_id))
            continue
        if published_at > baseline_at:
            eligible_ids.append(str(video_id))
        else:
            historical_ids.append(str(video_id))
    return eligible_ids, historical_ids, invalid_time_ids



def monitor_tiktok(root: Path, profile: dict, source: dict, state_entry: dict, max_new: int) -> tuple[dict, dict]:
    handle = source["profile_url"].rstrip("/").split("@")[-1].split("/")[0]
    source_cfg = {
        "creator_key": profile["creator_key"], "handle": handle, "profile_url": source["profile_url"],
        "enabled": True, "discovery_step": int(source.get("discovery_step", 50)),
        "max_catalog": int(source.get("max_catalog", 1000)), "max_new_downloads": max_new,
    }
    tts.start_server()
    discovery = tts.process_source(root, source_cfg, max_new_override=0, discovery_target_override=source_cfg["discovery_step"])
    current_ids = _tiktok_catalog_ids(root, profile["creator_key"])
    seen = set(str(x) for x in state_entry.get("seen_ids", []))
    if not state_entry.get("initialized_at"):
        new_state = {**state_entry, "initialized_at": now_iso(), "seen_ids": current_ids[:MAX_SEEN_IDS], "last_checked_at": now_iso(), "last_result": "BASELINED"}
        return {"platform": "TIKTOK", "result": "BASELINED", "feed_items": len(current_ids), "new_items": 0, "discovery": discovery.get("discovery")}, new_state
    unseen_ids = [x for x in current_ids if x not in seen]
    baseline_at = _parse_iso_utc(state_entry["initialized_at"])
    eligible_ids, historical_ids, invalid_time_ids = _partition_tiktok_unseen(unseen_ids, baseline_at)

    # Pre-baseline catalogue gaps are baseline history, not new monitored content.
    # Mark them seen so later discovery does not repeatedly surface them.
    seen_with_history = seen | set(historical_ids)
    if invalid_time_ids:
        raise RuntimeError(f"Cannot derive TikTok publish time for IDs: {invalid_time_ids[:5]}")

    if not eligible_ids:
        merged_seen = [x for x in current_ids if x in seen_with_history] + [x for x in seen if x not in set(current_ids)]
        new_state = {
            **state_entry,
            "seen_ids": merged_seen[:MAX_SEEN_IDS],
            "last_checked_at": now_iso(),
            "last_result": "NO_NEW",
        }
        return {
            "platform": "TIKTOK",
            "result": "NO_NEW",
            "feed_items": len(current_ids),
            "new_items": 0,
            "unseen_items": len(unseen_ids),
            "historical_ignored": len(historical_ids),
            "historical_ignored_ids": historical_ids[:20],
            "discovery": discovery.get("discovery"),
        }, new_state

    selected = eligible_ids[:max_new]
    already_done = manifest_done_ids(root, "TIKTOK")
    process_ids = set(x for x in selected if x not in already_done)
    completed_ids = set(x for x in selected if x in already_done)
    result_obj = {"failures": []}
    if process_ids:
        result_obj = tts.process_source(root, source_cfg, max_new_override=len(process_ids), include_video_ids=process_ids, discovery_target_override=source_cfg["discovery_step"])
        completed_ids.update(str(x.get("video_id")) for x in result_obj.get("completed", []) if x.get("video_id"))
        if result_obj.get("completed"):
            tts.run_research_queue(root)
    completed_ids &= set(selected)
    ok = not result_obj.get("failures") and set(selected).issubset(completed_ids)
    failed_ids = [x for x in selected if x not in completed_ids]
    result = "INGESTED" if ok else ("PARTIAL" if completed_ids else "FAILED")
    updated_seen = seen_with_history | completed_ids
    merged_seen = [x for x in current_ids if x in updated_seen] + [x for x in seen if x not in set(current_ids)]
    new_state = {**state_entry, "seen_ids": merged_seen[:MAX_SEEN_IDS], "last_checked_at": now_iso(), "last_result": result}
    return {
        "platform": "TIKTOK", "result": result, "feed_items": len(current_ids), "new_items": len(eligible_ids),
        "unseen_items": len(unseen_ids), "historical_ignored": len(historical_ids),
        "historical_ignored_ids": historical_ids[:20],
        "selected": selected, "completed_ids": sorted(completed_ids), "failed_ids": failed_ids,
        "failures": result_obj.get("failures", []),
        "discovery": discovery.get("discovery"),
    }, new_state


def _main_impl() -> int:
    ap = argparse.ArgumentParser(description="InfluencerResearch recurring monitored-creator discovery")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--creator-key", default="")
    ap.add_argument("--max-new", type=int, default=10)
    args = ap.parse_args()
    root = args.root.resolve()
    max_new = max(1, min(int(args.max_new), 20))
    started = now_iso()
    state_path = root / "state" / "creator_monitor_state.json"
    status_path = root / "state" / "creator_monitor_status.json"
    state = load_json(state_path, {"schema_version": 1, "monitor_version": MONITOR_VERSION, "sources": {}})
    state.setdefault("sources", {})
    atomic_json(status_path, {"schema_version": 1, "monitor_version": MONITOR_VERSION, "state": "RUNNING", "started_at": started})

    try:
        if args.creator_key:
            profiles = [get_creator(root, args.creator_key)]
        else:
            registry = load_registry(root)
            profiles = [p for p in registry["creators"].values() if p.get("status") == "ACTIVE" and p.get("monitoring_enabled")]
        results = []
        errors = []
        for profile in profiles:
            for source in select_monitor_sources(profile):
                skey = source_key(profile["creator_key"], source)
                entry = state["sources"].get(skey, {})
                try:
                    if source["platform"] == "YOUTUBE":
                        result, new_entry = monitor_youtube(root, profile, source, entry, max_new)
                    elif source["platform"] == "TIKTOK":
                        result, new_entry = monitor_tiktok(root, profile, source, entry, max_new)
                    else:
                        continue
                    adapter_result = str(result.get("result", "")).upper()
                    if adapter_result not in VALID_SOURCE_RESULTS:
                        raise RuntimeError(f"Invalid adapter result: {adapter_result!r}")
                    result["result"] = adapter_result
                    state["sources"][skey] = {**new_entry, "creator_key": profile["creator_key"], "platform": source["platform"], "profile_url": source["profile_url"]}
                    results.append({"creator_key": profile["creator_key"], **result})
                    if adapter_result in DEGRADED_SOURCE_RESULTS:
                        errors.append({
                            "creator_key": profile["creator_key"],
                            "platform": source["platform"],
                            "result": adapter_result,
                            "failed_ids": list(result.get("failed_ids", [])),
                            "error": f"{source['platform']} adapter returned {adapter_result}",
                        })
                except Exception as exc:
                    errors.append({"creator_key": profile["creator_key"], "platform": source.get("platform"), "error": f"{type(exc).__name__}: {exc}"})
        state["monitor_version"] = MONITOR_VERSION
        state["updated_at"] = now_iso()
        atomic_json(state_path, state)
        nonfailed_results = [x for x in results if x.get("result") != "FAILED"]
        final_state = "COMPLETE" if not errors else ("PARTIAL" if nonfailed_results else "FAILED")
        status = {
            "schema_version": 1, "monitor_version": MONITOR_VERSION, "state": final_state,
            "started_at": started, "finished_at": now_iso(), "creator_filter": args.creator_key or None,
            "result_count": len(results), "results": results, "error_count": len(errors), "errors": errors,
        }
        atomic_json(status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if final_state == "COMPLETE" else 1
    except Exception as exc:
        status = {"schema_version": 1, "monitor_version": MONITOR_VERSION, "state": "FAILED", "started_at": started, "finished_at": now_iso(), "error": f"{type(exc).__name__}: {exc}"}
        atomic_json(status_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 2


def main() -> int:
    try:
        return _main_impl()
    finally:
        tts.stop_server(deadline=time.monotonic() + 8.0)


if __name__ == "__main__":
    raise SystemExit(main())
