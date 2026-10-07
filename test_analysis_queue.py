from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from analysis_queue import (
    get_analysis_queue_item,
    list_analysis_decisions,
    list_analysis_queue,
    list_creator_evaluation_items,
    mark_analysis_insufficient,
    record_analysis_decision,
    record_analysis_decision_batch,
    summarize_creator_evaluation_run,
)


DECISIONS = ("IGNORE", "RESEARCH", "TEST_CANDIDATE", "BACKLOG_CANDIDATE")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_fixture(root: Path, *, creator_status: str = "ACTIVE", queue_id: str = "tt_123") -> dict:
    transcript_path = root / "output" / "creator" / "tiktok" / "transcripts" / "123.txt"
    evidence_path = root / "output" / "creator" / "tiktok" / "visual" / "123.json"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text("Fixture transcript about a falsifiable market claim.\n", encoding="utf-8")
    evidence_path.write_text('{"fixture":"visual evidence"}\n', encoding="utf-8")

    write_json(
        root / "control" / "creator_registry.json",
        {
            "schema_version": 1,
            "creators": {
                "creator": {
                    "creator_key": "creator",
                    "status": creator_status,
                    "sources": [],
                }
            },
        },
    )
    manifest_item = {
        "schema_version": 1,
        "shortcode": queue_id,
        "source_platform": "TIKTOK",
        "source_type": "VIDEO",
        "source_id": "123",
        "creator": "creator",
        "url": "https://www.tiktok.com/@creator/video/123",
        "caption": "Fixture caption",
        "published_at": "2026-10-01T12:00:00+00:00",
        "transcript_txt": str(transcript_path.relative_to(root)),
        "visual_evidence_index": str(evidence_path.relative_to(root)),
        "evidence_lineage_id": "lineage-fixture-123",
        "downloaded_at": "2026-10-01T12:01:00+00:00",
        "transcribed_at": "2026-10-01T12:02:00+00:00",
        "evaluation_mode": "CREATOR_EVALUATION",
        "evaluation_run_id": "eval-fixture-1",
        "research_status": "PENDING",
    }
    queue_item = {
        "schema_version": 2,
        "queue_id": queue_id,
        "shortcode": queue_id,
        "creator": "creator",
        "source_platform": "TIKTOK",
        "source_id": "123",
        "source_url": manifest_item["url"],
        "published_at": manifest_item["published_at"],
        "analysis_owner": "EKONOMI",
        "analysis_status": "PENDING_ANALYSIS",
        "analysis_content_status": "READY",
        "evidence_lineage_id": "lineage-fixture-123",
        "caption": "Fixture caption",
        "transcript_file": str(transcript_path.relative_to(root)),
        "transcript_text": "Fixture transcript about a falsifiable market claim.",
        "visual_evidence_index": str(evidence_path.relative_to(root)),
        "discovery_tags": ["market", "fixture"],
    }
    write_json(
        root / "state" / "manifest.json",
        {"schema_version": 1, "items": {queue_id: manifest_item}},
    )
    write_json(
        root / "state" / "research_queue.json",
        {
            "items": [queue_item],
            "count": 1,
            "analysis_owner": "EKONOMI",
            "status": "PENDING_ANALYSIS",
            "screen_version": "0.2.0",
        },
    )
    write_json(
        root / "state" / "research_decisions.json",
        {"schema_version": 2, "screen_version": "0.2.0", "items": {}},
    )
    return {
        "queue_id": queue_id,
        "queue_item": queue_item,
        "manifest_item": manifest_item,
        "transcript_path": transcript_path,
        "evidence_path": evidence_path,
    }


def decision_payload(decision: str, *, queue_id: str = "tt_123") -> dict:
    return {
        "queue_id": queue_id,
        "decision": decision,
        "idea_type": "MARKET_STRUCTURE",
        "claim_summary": "A testable fixture claim.",
        "claims": [{"claim": "Fixture claim"}],
        "existing_system_overlap": "None known.",
        "verification_plan": ["Check primary market data."],
        "falsifiable_test": "Reject if primary data contradicts the claim.",
        "main_risk": "Selection bias.",
        "confidence": 0.6,
        "rationale": "Useful enough for fixture validation.",
        "evidence_lineage_id": "lineage-fixture-123",
        "duplicate_of": None,
        "duplicate_basis": None,
    }


class AnalysisQueueTests(unittest.TestCase):
    def test_pending_to_each_canonical_decision(self) -> None:
        for decision in DECISIONS:
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                fixture = build_fixture(root)
                result = record_analysis_decision(root, fixture["queue_id"], decision_payload(decision))
                self.assertEqual(result["result"], "WRITTEN")
                self.assertEqual(result["decision"], decision)

                queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
                ledger = json.loads((root / "state" / "research_decisions.json").read_text(encoding="utf-8"))
                self.assertEqual(queue["count"], 0)
                self.assertEqual(ledger["items"][fixture["queue_id"]]["decision"], decision)

    def test_provenance_transcript_and_evidence_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = build_fixture(root)
            before_manifest = json.loads((root / "state" / "manifest.json").read_text(encoding="utf-8"))
            before_transcript = fixture["transcript_path"].read_bytes()
            before_evidence = fixture["evidence_path"].read_bytes()

            result = record_analysis_decision(root, fixture["queue_id"], decision_payload("IGNORE"))
            self.assertEqual(result["result"], "WRITTEN")

            after_manifest = json.loads((root / "state" / "manifest.json").read_text(encoding="utf-8"))
            before_item = before_manifest["items"][fixture["queue_id"]]
            after_item = after_manifest["items"][fixture["queue_id"]]
            for field in (
                "creator",
                "url",
                "source_platform",
                "source_id",
                "published_at",
                "downloaded_at",
                "transcribed_at",
                "transcript_txt",
                "visual_evidence_index",
                "evidence_lineage_id",
            ):
                self.assertEqual(after_item[field], before_item[field])
            self.assertEqual(fixture["transcript_path"].read_bytes(), before_transcript)
            self.assertEqual(fixture["evidence_path"].read_bytes(), before_evidence)

    def test_identical_replay_is_true_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = build_fixture(root)
            payload = decision_payload("IGNORE")
            first = record_analysis_decision(root, fixture["queue_id"], payload)
            self.assertEqual(first["result"], "WRITTEN")
            paths = [
                root / "state" / "research_queue.json",
                root / "state" / "research_decisions.json",
                root / "state" / "manifest.json",
                root / "state" / "research_followups.json",
            ]
            before = {path: path.read_bytes() for path in paths}

            second = record_analysis_decision(root, fixture["queue_id"], payload)
            self.assertEqual(second["result"], "NO_OP")
            for path in paths:
                self.assertEqual(path.read_bytes(), before[path])

    def test_conflicting_second_decision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = build_fixture(root)
            self.assertEqual(
                record_analysis_decision(root, fixture["queue_id"], decision_payload("IGNORE"))["result"],
                "WRITTEN",
            )
            conflict = record_analysis_decision(
                root,
                fixture["queue_id"],
                decision_payload("RESEARCH"),
            )
            self.assertEqual(conflict["result"], "CONFLICT")
            ledger = json.loads((root / "state" / "research_decisions.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["items"][fixture["queue_id"]]["decision"], "IGNORE")

    def test_unknown_queue_id_and_invalid_decision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            build_fixture(root)
            missing = record_analysis_decision(root, "missing", decision_payload("IGNORE", queue_id="missing"))
            self.assertEqual(missing["result"], "NOT_FOUND")
            invalid = decision_payload("IGNORE")
            invalid["decision"] = "TRADE"
            with self.assertRaisesRegex(ValueError, "BAD_DECISION"):
                record_analysis_decision(root, "tt_123", invalid)

    def test_batch_reports_per_item_results(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            build_fixture(root, queue_id="tt_123")
            second = build_fixture(root, queue_id="tt_456")
            queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
            first_item = {
                **second["queue_item"],
                "queue_id": "tt_123",
                "shortcode": "tt_123",
                "source_id": "123",
                "source_url": "https://www.tiktok.com/@creator/video/123",
            }
            queue["items"] = [first_item, second["queue_item"]]
            queue["count"] = 2
            write_json(root / "state" / "research_queue.json", queue)
            manifest = json.loads((root / "state" / "manifest.json").read_text(encoding="utf-8"))
            manifest["items"]["tt_123"] = {
                **second["manifest_item"],
                "shortcode": "tt_123",
                "source_id": "123",
                "url": "https://www.tiktok.com/@creator/video/123",
            }
            write_json(root / "state" / "manifest.json", manifest)

            first_payload = decision_payload("IGNORE", queue_id="tt_123")
            second_payload = decision_payload("RESEARCH", queue_id="tt_456")
            batch = record_analysis_decision_batch(root, [first_payload, second_payload])
            self.assertEqual(batch["written"], 2)
            self.assertEqual(batch["no_op"], 0)
            self.assertEqual(batch["failed"], 0)
            self.assertEqual([x["result"] for x in batch["results"]], ["WRITTEN", "WRITTEN"])

    def test_mark_insufficient_removes_pending_without_creating_decision_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = build_fixture(root)
            before_transcript = fixture["transcript_path"].read_bytes()
            before_evidence = fixture["evidence_path"].read_bytes()

            first = mark_analysis_insufficient(
                root,
                fixture["queue_id"],
                reason="RETAINED_FRAME_IS_BLANK_LOADING_SCREEN",
                evidence_lineage_id="lineage-fixture-123",
            )
            self.assertEqual(first["result"], "WRITTEN")

            queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
            ledger = json.loads((root / "state" / "research_decisions.json").read_text(encoding="utf-8"))
            manifest = json.loads((root / "state" / "manifest.json").read_text(encoding="utf-8"))
            item = manifest["items"][fixture["queue_id"]]
            self.assertEqual(queue["count"], 0)
            self.assertEqual(ledger["items"], {})
            self.assertEqual(item["research_status"], "INSUFFICIENT_CONTENT")
            self.assertEqual(item["analysis_content_status"], "INSUFFICIENT_CONTENT")
            self.assertEqual(item["analysis_insufficient_reason"], "RETAINED_FRAME_IS_BLANK_LOADING_SCREEN")
            self.assertEqual(fixture["transcript_path"].read_bytes(), before_transcript)
            self.assertEqual(fixture["evidence_path"].read_bytes(), before_evidence)

            paths = [
                root / "state" / "research_queue.json",
                root / "state" / "research_decisions.json",
                root / "state" / "manifest.json",
            ]
            before_replay = {path: path.read_bytes() for path in paths}
            second = mark_analysis_insufficient(
                root,
                fixture["queue_id"],
                reason="RETAINED_FRAME_IS_BLANK_LOADING_SCREEN",
                evidence_lineage_id="lineage-fixture-123",
            )
            self.assertEqual(second["result"], "NO_OP")
            for path in paths:
                self.assertEqual(path.read_bytes(), before_replay[path])

            historical = get_analysis_queue_item(root, fixture["queue_id"])
            self.assertEqual(historical["result"], "INSUFFICIENT_CONTENT")
            self.assertIsNone(historical["decision"])

    def test_mark_insufficient_rejects_lineage_mismatch_and_finalized_decision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = build_fixture(root)
            mismatch = mark_analysis_insufficient(
                root,
                fixture["queue_id"],
                reason="BLANK_FRAME",
                evidence_lineage_id="wrong-lineage",
            )
            self.assertEqual(mismatch["result"], "VALIDATION_ERROR")

            self.assertEqual(
                record_analysis_decision(root, fixture["queue_id"], decision_payload("IGNORE"))["result"],
                "WRITTEN",
            )
            conflict = mark_analysis_insufficient(
                root,
                fixture["queue_id"],
                reason="BLANK_FRAME",
                evidence_lineage_id="lineage-fixture-123",
            )
            self.assertEqual(conflict["result"], "CONFLICT")
            self.assertEqual(conflict["error"], "FINALIZED_DECISION_EXISTS")

    def test_finalized_decision_listing_filters_and_joins_manifest_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = build_fixture(root)
            self.assertEqual(
                record_analysis_decision(root, fixture["queue_id"], decision_payload("RESEARCH"))["result"],
                "WRITTEN",
            )

            listing = list_analysis_decisions(
                root,
                creator_key="creator",
                source_platform="TIKTOK",
                decision="RESEARCH",
                evaluation_run_id="eval-fixture-1",
                screened_after="2020-01-01T00:00:00+00:00",
                screened_before="2100-01-01T00:00:00+00:00",
                limit=10,
                offset=0,
            )
            self.assertEqual(listing["total"], 1)
            row = listing["items"][0]
            self.assertEqual(row["queue_id"], fixture["queue_id"])
            self.assertEqual(row["creator"], "creator")
            self.assertEqual(row["source_platform"], "TIKTOK")
            self.assertEqual(row["evaluation_run_id"], "eval-fixture-1")
            self.assertEqual(row["decision"], "RESEARCH")
            self.assertEqual(row["evidence_lineage_id"], "lineage-fixture-123")

    def test_evaluation_run_membership_joins_reused_items_without_overwriting_origin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_id = "eval-current"
            old_run_id = "eval-older"
            state = root / "state"
            write_json(
                state / "creator_evaluation_status.json",
                {
                    "evaluation_run_id": run_id,
                    "creator": "creator",
                    "source_platform": "YOUTUBE",
                    "progress": {
                        "phase": "EVIDENCE",
                        "selected_queue_ids": ["yt_new", "yt_reused"],
                    },
                },
            )
            common = {
                "creator": "creator",
                "source_platform": "YOUTUBE",
                "source_type": "VIDEO",
                "evaluation_mode": "CREATOR_EVALUATION",
                "download_status": "DONE",
                "transcription_status": "DONE",
                "visual_evidence_status": "DONE",
                "analysis_content_status": "READY",
                "research_status": "PENDING_ANALYSIS",
            }
            write_json(
                state / "manifest.json",
                {
                    "items": {
                        "yt_new": {**common, "source_id": "new", "evaluation_run_id": run_id},
                        "yt_reused": {**common, "source_id": "reused", "evaluation_run_id": old_run_id},
                    }
                },
            )
            write_json(
                state / "research_queue.json",
                {
                    "items": [
                        {"queue_id": "yt_new", "analysis_status": "PENDING_ANALYSIS"},
                        {"queue_id": "yt_reused", "analysis_status": "PENDING_ANALYSIS"},
                    ]
                },
            )
            write_json(state / "research_decisions.json", {"items": {}})

            listing = list_creator_evaluation_items(
                root,
                creator_key="creator",
                source_platform="YOUTUBE",
                evaluation_run_id=run_id,
                completed_only=True,
                limit=20,
                offset=0,
            )
            self.assertEqual(listing["total"], 2)
            rows = {row["queue_id"]: row for row in listing["items"]}
            self.assertEqual(rows["yt_reused"]["evaluation_run_id"], run_id)
            self.assertEqual(rows["yt_reused"]["origin_evaluation_run_id"], old_run_id)
            self.assertTrue(rows["yt_reused"]["selected_via_run_membership"])

            accounting = summarize_creator_evaluation_run(root, run_id)
            self.assertEqual(accounting["analysis_completed_manifest_count"], 2)
            self.assertEqual(accounting["queued_for_analysis_count"], 2)
            self.assertEqual(accounting["analysis_unaccounted_count"], 0)

    def test_retired_creator_is_hidden_from_pending_list_but_history_is_readable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = build_fixture(root, creator_status="RETIRED")
            listing = list_analysis_queue(root)
            self.assertEqual(listing["total"], 0)
            historical = get_analysis_queue_item(root, fixture["queue_id"])
            self.assertEqual(historical["result"], "FOUND")
            self.assertEqual(historical["queue_item"]["creator"], "creator")

    def test_empty_queue_list_reports_empty_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            build_fixture(root, creator_status="RETIRED")
            listing = list_analysis_queue(root)
            self.assertEqual(listing["status"], "EMPTY")
            self.assertEqual(listing["total"], 0)
            self.assertEqual(listing["count"], 0)

    def test_queue_read_filters_and_get_full_analysis_input(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = build_fixture(root)
            listing = list_analysis_queue(
                root,
                creator_key="creator",
                source_platform="TIKTOK",
                published_after="2026-09-30T00:00:00+00:00",
                published_before="2026-10-02T00:00:00+00:00",
                limit=10,
                offset=0,
            )
            self.assertEqual(listing["total"], 1)
            self.assertEqual(listing["items"][0]["queue_id"], fixture["queue_id"])
            full = get_analysis_queue_item(root, fixture["queue_id"])
            self.assertEqual(full["queue_item"]["transcript_text"], fixture["queue_item"]["transcript_text"])
            self.assertEqual(full["queue_item"]["discovery_tags"], ["market", "fixture"])

    def test_queue_listing_tolerates_missing_optional_source_platform(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            build_fixture(root)
            queue_path = root / "state" / "research_queue.json"
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["items"][0].pop("source_platform", None)
            write_json(queue_path, queue)

            listing = list_analysis_queue(root)
            self.assertEqual(listing["total"], 1)
            self.assertNotIn("source_platform", listing["items"][0])

    def test_creator_evaluation_item_list_explains_completed_dispositions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = build_fixture(root)
            run_id = "eval-fixture-1"
            manifest_path = root / "state" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            base = dict(manifest["items"][fixture["queue_id"]])
            base.update({
                "download_status": "DONE",
                "transcription_status": "DONE",
                "visual_evidence_status": "DONE",
            })
            manifest["items"][fixture["queue_id"]] = base
            manifest["items"]["tt_dup"] = {
                **base,
                "shortcode": "tt_dup",
                "source_id": "dup",
                "url": "https://www.tiktok.com/@creator/video/dup",
                "research_status": "DUPLICATE",
                "duplicate_of": fixture["queue_id"],
                "duplicate_basis": "TRANSCRIPT_MATCH",
            }
            manifest["items"]["tt_insufficient"] = {
                **base,
                "shortcode": "tt_insufficient",
                "source_id": "insufficient",
                "url": "https://www.tiktok.com/@creator/video/insufficient",
                "research_status": "INSUFFICIENT_CONTENT",
                "analysis_content_status": "INSUFFICIENT_CONTENT",
                "analysis_content_reason": "NO_USABLE_TEXT_OR_VISUAL",
            }
            manifest["items"]["tt_final"] = {
                **base,
                "shortcode": "tt_final",
                "source_id": "final",
                "url": "https://www.tiktok.com/@creator/video/final",
                "research_status": "ANALYZED",
            }
            manifest["items"]["tt_missing"] = {
                **base,
                "shortcode": "tt_missing",
                "source_id": "missing",
                "url": "https://www.tiktok.com/@creator/video/missing",
                "research_status": "PENDING_ANALYSIS",
                "analysis_content_status": "READY",
            }
            write_json(manifest_path, manifest)
            write_json(
                root / "state" / "research_decisions.json",
                {
                    "schema_version": 2,
                    "items": {
                        "tt_final": {
                            "decision": "RESEARCH",
                            "screened_at": "2026-10-07T00:00:00+00:00",
                            "evidence_lineage_id": "lineage-final",
                        }
                    },
                },
            )

            listing = list_creator_evaluation_items(
                root,
                creator_key="creator",
                source_platform="TIKTOK",
                evaluation_run_id=run_id,
                completed_only=True,
                limit=20,
                offset=0,
            )
            by_id = {row["queue_id"]: row for row in listing["items"]}
            self.assertEqual(by_id[fixture["queue_id"]]["disposition"], "PENDING_ANALYSIS")
            self.assertEqual(by_id["tt_dup"]["disposition"], "DUPLICATE")
            self.assertEqual(by_id["tt_insufficient"]["disposition"], "INSUFFICIENT_CONTENT")
            self.assertEqual(by_id["tt_final"]["disposition"], "FINALIZED_DECISION")
            self.assertEqual(by_id["tt_missing"]["disposition"], "QUEUE_MISSING")

            only_duplicates = list_creator_evaluation_items(
                root,
                evaluation_run_id=run_id,
                disposition="DUPLICATE",
                completed_only=True,
                limit=20,
                offset=0,
            )
            self.assertEqual(only_duplicates["total"], 1)
            self.assertEqual(only_duplicates["items"][0]["queue_id"], "tt_dup")

            accounting = summarize_creator_evaluation_run(root, run_id)
            self.assertEqual(accounting["analysis_completed_manifest_count"], 5)
            self.assertEqual(accounting["queued_for_analysis_count"], 1)
            self.assertEqual(accounting["analysis_finalized_count"], 1)
            self.assertEqual(accounting["analysis_duplicate_count"], 1)
            self.assertEqual(accounting["analysis_insufficient_count"], 1)
            self.assertEqual(accounting["analysis_queue_missing_count"], 1)
            self.assertEqual(accounting["analysis_accounted_count"], 4)
            self.assertEqual(accounting["analysis_unaccounted_count"], 1)
            self.assertEqual(accounting["analysis_unaccounted_items"], ["tt_missing"])


    def test_remaining_pending_counts_only_current_active_pending_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = build_fixture(root)
            queue_path = root / "state" / "research_queue.json"
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["items"].append({
                "queue_id": "legacy_finalized",
                "creator": "creator",
                "analysis_status": "FINALIZED",
            })
            queue["count"] = len(queue["items"])
            write_json(queue_path, queue)

            batch = record_analysis_decision_batch(
                root,
                [decision_payload("IGNORE", queue_id=fixture["queue_id"])],
            )
            self.assertEqual(batch["results"][0]["remaining_pending"], 0)
            self.assertEqual(list_analysis_queue(root)["total"], 0)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = build_fixture(root)
            queue_path = root / "state" / "research_queue.json"
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["items"].append({
                "queue_id": "legacy_finalized",
                "creator": "creator",
                "analysis_status": "FINALIZED",
            })
            queue["count"] = len(queue["items"])
            write_json(queue_path, queue)

            result = mark_analysis_insufficient(
                root,
                fixture["queue_id"],
                reason="RETAINED_FRAME_IS_BLANK_LOADING_SCREEN",
                evidence_lineage_id="lineage-fixture-123",
            )
            self.assertEqual(result["remaining_pending"], 0)
            self.assertEqual(list_analysis_queue(root)["total"], 0)


if __name__ == "__main__":
    unittest.main()
