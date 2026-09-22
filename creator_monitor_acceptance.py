from __future__ import annotations
import contextlib, hashlib, io, json, os, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from creator_registry import load_registry, select_monitor_sources
import creator_monitor as cm
from creator_monitor import _partition_tiktok_unseen

SUPPORTED_PLATFORMS = {"YOUTUBE": "yt_", "TIKTOK": "tt_"}


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def synthetic_tiktok_id(published_at):
    ts = int(published_at.astimezone(timezone.utc).timestamp())
    return str((ts << 32) | 1)


def verify_tiktok_cutoff_partition():
    baseline = datetime.now(timezone.utc)
    historical_id = synthetic_tiktok_id(baseline - timedelta(days=1))
    eligible_id = synthetic_tiktok_id(baseline + timedelta(seconds=1))
    eligible, historical, invalid = _partition_tiktok_unseen(
        [historical_id, eligible_id, "not-a-tiktok-id"], baseline
    )
    if eligible != [eligible_id] or historical != [historical_id] or invalid != ["not-a-tiktok-id"]:
        raise RuntimeError(
            f"TikTok cutoff partition regression failed: eligible={eligible} historical={historical} invalid={invalid}"
        )
    return {
        "historical_id": historical_id,
        "eligible_id": eligible_id,
        "invalid_id_rejected": True,
    }



def _fixture_adapter(platform, outcome):
    completed = [platform.lower() + "-ok"] if outcome == "INGESTED" else []
    failed = [platform.lower() + "-failed"] if outcome != "INGESTED" else []
    return (
        {
            "platform": platform,
            "result": outcome,
            "selected": completed + failed,
            "completed_ids": completed,
            "failed_ids": failed,
        },
        {
            "initialized_at": "2026-08-24T00:00:00+00:00",
            "seen_ids": completed,
            "last_result": outcome,
        },
    )


def _run_fail_closed_fixture(youtube_outcome, tiktok_outcome):
    with tempfile.TemporaryDirectory() as tmp:
        fixture_root = Path(tmp)
        profile = {"creator_key": "acceptance_fixture"}
        sources = [
            {"platform": "YOUTUBE", "profile_url": "https://youtube.example/acceptance"},
            {"platform": "TIKTOK", "profile_url": "https://tiktok.example/@acceptance"},
        ]
        argv = [
            "creator_monitor.py", "--root", str(fixture_root),
            "--creator-key", "acceptance_fixture",
        ]
        with (
            patch.object(cm, "get_creator", return_value=profile),
            patch.object(cm, "select_monitor_sources", return_value=sources),
            patch.object(cm, "monitor_youtube", return_value=_fixture_adapter("YOUTUBE", youtube_outcome)),
            patch.object(cm, "monitor_tiktok", return_value=_fixture_adapter("TIKTOK", tiktok_outcome)),
            patch.object(sys, "argv", argv),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                returncode = cm.main()
        status = load(fixture_root / "state" / "creator_monitor_status.json", {})
        state = load(fixture_root / "state" / "creator_monitor_state.json", {})
        source_states = {
            key: entry.get("last_result")
            for key, entry in (state.get("sources") or {}).items()
        }
        return returncode, status, source_states


def verify_fail_closed_contract():
    scenarios = [
        ("ONE_SUCCESS_ONE_FAILURE", "INGESTED", "FAILED", "PARTIAL", 1, 1),
        ("ALL_FAILURES", "FAILED", "FAILED", "FAILED", 1, 2),
        ("FULL_SUCCESS", "INGESTED", "INGESTED", "COMPLETE", 0, 0),
    ]
    checks = []
    for name, youtube_outcome, tiktok_outcome, expected_state, expected_rc, expected_errors in scenarios:
        rc, status, source_states = _run_fail_closed_fixture(youtube_outcome, tiktok_outcome)
        if rc != expected_rc or status.get("state") != expected_state or status.get("error_count") != expected_errors:
            raise RuntimeError(
                f"{name} fail-closed regression: rc={rc} status={status} source_states={source_states}"
            )
        if source_states != {
            "acceptance_fixture:YOUTUBE": youtube_outcome,
            "acceptance_fixture:TIKTOK": tiktok_outcome,
        }:
            raise RuntimeError(f"{name} state read-back mismatch: {source_states}")
        checks.append({
            "scenario": name,
            "state": expected_state,
            "returncode": expected_rc,
            "error_count": expected_errors,
            "source_states": source_states,
        })

    partial, completed, failed = cm._classify_ingestion_result(
        ["completed", "retryable"], {"completed", "stale"}, had_failure=True
    )
    total_failure, none_completed, all_failed = cm._classify_ingestion_result(
        ["retryable"], set(), had_failure=True
    )
    if (
        partial != "PARTIAL"
        or completed != {"completed"}
        or failed != ["retryable"]
        or total_failure != "FAILED"
        or none_completed
        or all_failed != ["retryable"]
    ):
        raise RuntimeError(
            "Adapter normalization/retryability regression: "
            f"partial={partial} completed={completed} failed={failed} "
            f"total_failure={total_failure} none_completed={none_completed} all_failed={all_failed}"
        )
    return {
        "scenarios": checks,
        "adapter_outcomes": ["BASELINED", "NO_NEW", "INGESTED", "PARTIAL", "FAILED"],
        "failed_ids_retryable": True,
        "stale_completed_ids_rejected": True,
    }


def _source_status(status):
    results = status.get("results") or []
    if len(results) != 1:
        raise RuntimeError(f"Expected one source result, got: {status}")
    return results[0]


def _run_youtube_adapter_case(
    *, entries, diag, child_returncode=0, completed=None,
    initialized=True, initial_seen=None,
):
    completed = list(completed or [])
    initial_seen = list(initial_seen or [])
    with tempfile.TemporaryDirectory() as tmp:
        fixture_root = Path(tmp)
        (fixture_root / "app").mkdir(parents=True)
        state_path = fixture_root / "state" / "creator_monitor_state.json"
        source_key = "youtube_fixture:YOUTUBE"
        source_entry = {}
        if initialized:
            source_entry = {
                "initialized_at": "2026-08-20T00:00:00+00:00",
                "seen_ids": initial_seen,
                "last_result": "NO_NEW",
            }
        atomic(state_path, {
            "schema_version": 1,
            "monitor_version": cm.MONITOR_VERSION,
            "sources": ({source_key: source_entry} if initialized else {}),
        })
        atomic(fixture_root / "state" / "creator_evaluation_status.json", {
            "state": "COMPLETE" if child_returncode == 0 else "PARTIAL",
            "completed": completed,
        })
        profile = {"creator_key": "youtube_fixture"}
        sources = [{"platform": "YOUTUBE", "profile_url": "https://youtube.example/fixture"}]
        argv = [
            "creator_monitor.py", "--root", str(fixture_root),
            "--creator-key", "youtube_fixture", "--max-new", "2",
        ]
        with (
            patch.object(cm, "get_creator", return_value=profile),
            patch.object(cm, "select_monitor_sources", return_value=sources),
            patch.object(cm.yte, "enumerate_channel", return_value=(entries, diag)),
            patch.object(cm.subprocess, "run", return_value=SimpleNamespace(returncode=child_returncode)),
            patch.object(sys, "argv", argv),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                returncode = cm.main()
        status = load(fixture_root / "state" / "creator_monitor_status.json", {})
        state = load(state_path, {})
        return returncode, status, (state.get("sources") or {}).get(source_key, {})


def _run_tiktok_adapter_case(*, completed_ids, failures):
    baseline = datetime.now(timezone.utc) - timedelta(days=2)
    published = datetime.now(timezone.utc) - timedelta(days=1)
    selected = [
        synthetic_tiktok_id(published),
        synthetic_tiktok_id(published + timedelta(seconds=1)),
    ]
    completed_ids = [selected[index] for index in completed_ids]
    failure_items = [{"video_id": selected[index], "error": "fixture failure"} for index in failures]
    with tempfile.TemporaryDirectory() as tmp:
        fixture_root = Path(tmp)
        (fixture_root / "app").mkdir(parents=True)
        source_key = "tiktok_fixture:TIKTOK"
        state_path = fixture_root / "state" / "creator_monitor_state.json"
        atomic(state_path, {
            "schema_version": 1,
            "monitor_version": cm.MONITOR_VERSION,
            "sources": {
                source_key: {
                    "initialized_at": baseline.isoformat(),
                    "seen_ids": [],
                    "last_result": "NO_NEW",
                }
            },
        })
        catalog_path = fixture_root / "state" / "tiktok" / "tiktok_fixture_catalog.json"
        atomic(catalog_path, {"order": selected})
        queue_path = fixture_root / "state" / "research_queue.json"
        atomic(queue_path, {"schema_version": 1, "items": []})
        queue_hash_before = sha256_file(queue_path)
        discovery = {"discovery": {"target": 2, "found": 2, "fixture": True}}
        ingestion = {
            "completed": [{"video_id": video_id} for video_id in completed_ids],
            "failures": failure_items,
        }
        profile = {"creator_key": "tiktok_fixture"}
        sources = [{"platform": "TIKTOK", "profile_url": "https://tiktok.example/@fixture"}]
        argv = [
            "creator_monitor.py", "--root", str(fixture_root),
            "--creator-key", "tiktok_fixture", "--max-new", "2",
        ]
        with (
            patch.object(cm, "get_creator", return_value=profile),
            patch.object(cm, "select_monitor_sources", return_value=sources),
            patch.object(cm.tts, "start_server"),
            patch.object(cm.tts, "process_source", side_effect=[discovery, ingestion]),
            patch.object(cm.tts, "run_research_queue"),
            patch.object(sys, "argv", argv),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                returncode = cm.main()
        status = load(fixture_root / "state" / "creator_monitor_status.json", {})
        state = load(state_path, {})
        return (
            returncode,
            status,
            (state.get("sources") or {}).get(source_key, {}),
            selected,
            queue_hash_before,
            sha256_file(queue_path),
        )


def verify_adapter_negative_paths():
    checks = []

    youtube_cases = [
        {
            "scenario": "YOUTUBE_DISCOVERY_FAILED_EMPTY",
            "entries": [],
            "diag": {"ok": False, "returncode": 7, "diagnostic_tail": "fixture discovery failure"},
            "initialized": False,
            "expected_result": "FAILED",
            "expected_top": "FAILED",
            "expected_completed": [],
            "expected_failed": [],
        },
        {
            "scenario": "YOUTUBE_DISCOVERY_PARTIAL_ENTRIES",
            "entries": [{"id": "yt-a"}, {"id": "yt-b"}],
            "diag": {"ok": False, "returncode": 9, "diagnostic_tail": "fixture partial discovery"},
            "initialized": False,
            "expected_result": "PARTIAL",
            "expected_top": "PARTIAL",
            "expected_completed": [],
            "expected_failed": ["yt-a", "yt-b"],
        },
        {
            "scenario": "YOUTUBE_DISCOVERY_PARTIAL_PRESERVES_INITIALIZED_STATE",
            "entries": [{"id": "existing"}, {"id": "retryable"}],
            "diag": {"ok": False, "returncode": 10, "diagnostic_tail": "fixture initialized partial discovery"},
            "initialized": True,
            "initial_seen": ["existing"],
            "expected_result": "PARTIAL",
            "expected_top": "PARTIAL",
            "expected_completed": [],
            "expected_failed": ["retryable"],
            "expected_seen": ["existing"],
        },
        {
            "scenario": "YOUTUBE_CHILD_NONZERO_ONE_OF_TWO_COMPLETED",
            "entries": [{"id": "yt-a"}, {"id": "yt-b"}],
            "diag": {"ok": True, "returncode": 0},
            "child_returncode": 4,
            "completed": ["yt_yt-a"],
            "expected_result": "PARTIAL",
            "expected_top": "PARTIAL",
            "expected_completed": ["yt-a"],
            "expected_failed": ["yt-b"],
        },
        {
            "scenario": "YOUTUBE_CHILD_NONZERO_NONE_COMPLETED",
            "entries": [{"id": "yt-a"}, {"id": "yt-b"}],
            "diag": {"ok": True, "returncode": 0},
            "child_returncode": 5,
            "completed": [],
            "expected_result": "FAILED",
            "expected_top": "FAILED",
            "expected_completed": [],
            "expected_failed": ["yt-a", "yt-b"],
        },
        {
            "scenario": "YOUTUBE_CHILD_ZERO_INCOMPLETE",
            "entries": [{"id": "yt-a"}, {"id": "yt-b"}],
            "diag": {"ok": True, "returncode": 0},
            "child_returncode": 0,
            "completed": ["yt_yt-a", "yt_stale"],
            "expected_result": "PARTIAL",
            "expected_top": "PARTIAL",
            "expected_completed": ["yt-a"],
            "expected_failed": ["yt-b"],
        },
        {
            "scenario": "YOUTUBE_CHILD_ZERO_NONE_COMPLETED",
            "entries": [{"id": "yt-a"}, {"id": "yt-b"}],
            "diag": {"ok": True, "returncode": 0},
            "child_returncode": 0,
            "completed": [],
            "expected_result": "FAILED",
            "expected_top": "FAILED",
            "expected_completed": [],
            "expected_failed": ["yt-a", "yt-b"],
        },
        {
            "scenario": "YOUTUBE_FULL_SUCCESS",
            "entries": [{"id": "yt-a"}, {"id": "yt-b"}],
            "diag": {"ok": True, "returncode": 0},
            "child_returncode": 0,
            "completed": ["yt_yt-a", "yt_yt-b"],
            "expected_result": "INGESTED",
            "expected_top": "COMPLETE",
            "expected_completed": ["yt-a", "yt-b"],
            "expected_failed": [],
        },
    ]
    for case in youtube_cases:
        rc, status, state_entry = _run_youtube_adapter_case(
            entries=case["entries"],
            diag=case["diag"],
            child_returncode=case.get("child_returncode", 0),
            completed=case.get("completed", []),
            initialized=case.get("initialized", True),
            initial_seen=case.get("initial_seen", []),
        )
        result = _source_status(status)
        seen = list(state_entry.get("seen_ids", []))
        expected_seen = case.get("expected_seen", case["expected_completed"])
        expected_rc = 0 if case["expected_top"] == "COMPLETE" else 1
        if (
            rc != expected_rc
            or status.get("state") != case["expected_top"]
            or result.get("result") != case["expected_result"]
            or result.get("completed_ids", []) != case["expected_completed"]
            or result.get("failed_ids", []) != case["expected_failed"]
            or set(seen) != set(expected_seen)
        ):
            raise RuntimeError(
                f"{case['scenario']} regression: rc={rc} status={status} state_entry={state_entry}"
            )
        if not case.get("initialized", True) and state_entry.get("initialized_at"):
            raise RuntimeError(f"{case['scenario']} incorrectly initialized baseline: {state_entry}")
        checks.append({
            "platform": "YOUTUBE",
            "scenario": case["scenario"],
            "result": case["expected_result"],
            "top_state": case["expected_top"],
            "returncode": expected_rc,
            "completed_ids": case["expected_completed"],
            "failed_ids": case["expected_failed"],
            "seen_ids": seen,
            "failed_ids_retryable": not (set(case["expected_failed"]) & set(seen)),
        })

    tiktok_cases = [
        ("TIKTOK_ONE_COMPLETED_ONE_FAILED", [0], [1], "PARTIAL", "PARTIAL"),
        ("TIKTOK_ALL_FAILED", [], [0, 1], "FAILED", "FAILED"),
        ("TIKTOK_MISSING_WITHOUT_EXPLICIT_FAILURE", [0], [], "PARTIAL", "PARTIAL"),
        ("TIKTOK_ALL_MISSING_WITHOUT_EXPLICIT_FAILURE", [], [], "FAILED", "FAILED"),
        ("TIKTOK_FULL_SUCCESS", [0, 1], [], "INGESTED", "COMPLETE"),
    ]
    for scenario, completed_indexes, failure_indexes, expected_result, expected_top in tiktok_cases:
        rc, status, state_entry, selected, queue_before, queue_after = _run_tiktok_adapter_case(
            completed_ids=completed_indexes,
            failures=failure_indexes,
        )
        result = _source_status(status)
        completed = [selected[index] for index in completed_indexes]
        failed = [video_id for video_id in selected if video_id not in set(completed)]
        seen = list(state_entry.get("seen_ids", []))
        expected_rc = 0 if expected_top == "COMPLETE" else 1
        if (
            rc != expected_rc
            or status.get("state") != expected_top
            or result.get("result") != expected_result
            or set(result.get("completed_ids", [])) != set(completed)
            or result.get("failed_ids", []) != failed
            or set(seen) != set(completed)
            or queue_before != queue_after
        ):
            raise RuntimeError(
                f"{scenario} regression: rc={rc} status={status} state_entry={state_entry} "
                f"queue_before={queue_before} queue_after={queue_after}"
            )
        checks.append({
            "platform": "TIKTOK",
            "scenario": scenario,
            "result": expected_result,
            "top_state": expected_top,
            "returncode": expected_rc,
            "completed_ids": completed,
            "failed_ids": failed,
            "seen_ids": seen,
            "failed_ids_retryable": not (set(failed) & set(seen)),
            "queue_unchanged": queue_before == queue_after,
        })

    if not all(check["failed_ids_retryable"] for check in checks):
        raise RuntimeError(f"Failed ID retryability regression: {checks}")
    return {
        "state": "PASS",
        "scenario_count": len(checks),
        "checks": checks,
        "failed_ids_retryable": True,
    }


def monitored_sources(root):
    registry = load_registry(root)
    out = []
    for profile in registry["creators"].values():
        if str(profile.get("status", "ACTIVE")).upper() != "ACTIVE" or not profile.get("monitoring_enabled"):
            continue
        for source in select_monitor_sources(profile):
            platform = str(source.get("platform", "")).upper()
            if platform in SUPPORTED_PLATFORMS:
                out.append((profile["creator_key"], platform))
    if not out:
        raise RuntimeError("No ACTIVE monitoring-enabled supported sources in creator registry")
    return sorted(set(out))


def run_monitor(root, creator_key=""):
    cmd = [sys.executable, str(root / "app" / "creator_monitor.py"), "--root", str(root), "--max-new", "1"]
    if creator_key:
        cmd += ["--creator-key", creator_key]
    p = subprocess.run(cmd, cwd=str(root / "app"))
    if p.returncode != 0:
        raise RuntimeError(f"monitor {creator_key or 'ALL_MONITORED'} rc={p.returncode}")
    return load(root / "state" / "creator_monitor_status.json", {})


def source_result(status, creator_key, platform):
    return next((x for x in status.get("results", []) if x.get("creator_key") == creator_key and x.get("platform") == platform), {})


def choose_seen_id(root, creator_key, platform):
    state = load(root / "state" / "creator_monitor_state.json", {})
    skey = f"{creator_key}:{platform}"
    entry = (state.get("sources") or {}).get(skey) or {}
    seen = list(entry.get("seen_ids", []))
    if not seen:
        raise RuntimeError(f"{skey} baseline produced no seen ids")
    manifest = load(root / "state" / "manifest.json", {"items": {}})
    prefix = SUPPORTED_PLATFORMS[platform]
    for vid in seen:
        item = (manifest.get("items") or {}).get(prefix + vid, {})
        full_done = (
            item.get("download_status") == "DONE"
            and item.get("transcription_status") == "DONE"
            and (platform != "YOUTUBE" or item.get("visual_evidence_status") == "DONE")
        )
        if full_done:
            return vid
    return seen[0]


def remove_seen_id(root, creator_key, platform, chosen):
    state_path = root / "state" / "creator_monitor_state.json"
    state = load(state_path, {})
    skey = f"{creator_key}:{platform}"
    entry = state["sources"][skey]
    entry["seen_ids"] = [x for x in entry.get("seen_ids", []) if x != chosen]
    state["sources"][skey] = entry
    atomic(state_path, state)


def main():
    root = Path(__file__).resolve().parent.parent
    expected = monitored_sources(root)
    state_path = root / "state" / "creator_monitor_state.json"
    state = load(state_path, {"schema_version": 1, "monitor_version": "0.1.0", "sources": {}})
    state.setdefault("sources", {})
    for creator_key, platform in expected:
        state["sources"].pop(f"{creator_key}:{platform}", None)
    atomic(state_path, state)

    baseline = run_monitor(root)
    if baseline.get("creator_filter") is not None:
        raise RuntimeError(f"Registry-driven run unexpectedly had creator_filter: {baseline}")
    baseline_checks = []
    for creator_key, platform in expected:
        result = source_result(baseline, creator_key, platform)
        if result.get("result") != "BASELINED":
            raise RuntimeError(f"{creator_key}:{platform} baseline failed: {baseline}")
        baseline_checks.append({"creator_key": creator_key, "platform": platform, "result": "BASELINED"})

    cutoff_partition_check = verify_tiktok_cutoff_partition()
    fail_closed_contract_check = verify_fail_closed_contract()
    adapter_negative_path_checks = verify_adapter_negative_paths()

    representative_checks = []
    by_platform = {}
    for pair in expected:
        by_platform.setdefault(pair[1], pair)
    for platform, (creator_key, _) in sorted(by_platform.items()):
        chosen = choose_seen_id(root, creator_key, platform)
        queue_path = root / "state" / "research_queue.json"
        queue_hash_before = sha256_file(queue_path)
        remove_seen_id(root, creator_key, platform, chosen)
        second = run_monitor(root, creator_key)
        r2 = source_result(second, creator_key, platform)

        if platform == "TIKTOK":
            if chosen not in set(r2.get("historical_ignored_ids", [])):
                raise RuntimeError(
                    f"{creator_key}:{platform} pre-baseline gap was not ignored: chosen={chosen} status={second}"
                )
            if r2.get("new_items") != 0 or r2.get("completed_ids"):
                raise RuntimeError(
                    f"{creator_key}:{platform} historical gap leaked into new-item processing: {second}"
                )
            if sha256_file(queue_path) != queue_hash_before:
                raise RuntimeError(
                    f"{creator_key}:{platform} historical-gap test mutated Research Screen queue"
                )
            checks = ["PRE_BASELINE_GAP_IGNORED", "QUEUE_UNCHANGED"]
        else:
            if chosen not in set(r2.get("completed_ids", [])):
                raise RuntimeError(
                    f"{creator_key}:{platform} exact new-item exercise did not complete {chosen}: {second}"
                )
            checks = ["NEW_ITEM_EXACT_PROCESSING"]

        third = run_monitor(root, creator_key)
        r3 = source_result(third, creator_key, platform)
        if r3.get("result") != "NO_NEW":
            raise RuntimeError(f"{creator_key}:{platform} no-new cycle failed: {third}")
        checks.append("NO_NEW_NO_DUPLICATE")
        representative_checks.append({
            "creator_key": creator_key,
            "platform": platform,
            "chosen_video_id": chosen,
            "checks": checks,
        })

    final = run_monitor(root)
    final_checks = []
    for creator_key, platform in expected:
        result = source_result(final, creator_key, platform)
        if result.get("result") != "NO_NEW":
            raise RuntimeError(f"{creator_key}:{platform} registry-wide no-new failed: {final}")
        final_checks.append({"creator_key": creator_key, "platform": platform, "result": "NO_NEW"})

    out = {
        "schema_version": 1,
        "acceptance": "creator_monitor_v0.1.4_registry_driven_fail_closed",
        "state": "PASS",
        "monitored_sources": [{"creator_key": c, "platform": p} for c, p in expected],
        "baseline_checks": baseline_checks,
        "tiktok_cutoff_partition_check": cutoff_partition_check,
        "fail_closed_contract_check": fail_closed_contract_check,
        "adapter_negative_path_checks": adapter_negative_path_checks,
        "representative_adapter_checks": representative_checks,
        "final_registry_wide_no_new": final_checks,
    }
    atomic(root / "state" / "creator_monitor_acceptance_status.json", out)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
