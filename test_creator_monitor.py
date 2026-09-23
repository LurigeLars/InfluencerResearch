from __future__ import annotations
import contextlib, hashlib, io, json, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import creator_monitor as cm
from creator_monitor import _partition_tiktok_unseen


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


class CreatorMonitorContractTests(unittest.TestCase):
    def test_tiktok_cutoff_partition(self) -> None:
        result = verify_tiktok_cutoff_partition()
        self.assertTrue(result["invalid_id_rejected"])
        self.assertNotEqual(result["historical_id"], result["eligible_id"])

    def test_fail_closed_contract(self) -> None:
        result = verify_fail_closed_contract()
        self.assertEqual(len(result["scenarios"]), 3)
        self.assertTrue(result["failed_ids_retryable"])
        self.assertTrue(result["stale_completed_ids_rejected"])

    def test_adapter_negative_paths(self) -> None:
        result = verify_adapter_negative_paths()
        self.assertEqual(result["state"], "PASS")
        self.assertEqual(result["scenario_count"], 13)
        self.assertTrue(result["failed_ids_retryable"])


if __name__ == "__main__":
    unittest.main()
