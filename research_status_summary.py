from __future__ import annotations

import json
from pathlib import Path


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def summarize_status(path: Path | None) -> dict | None:
    """Return the explicit public subset of one research-job status file."""
    if path is None or not path.is_file():
        return None
    obj = _load_json(path)
    allowed = {
        "state",
        "result",
        "started_at",
        "updated_at",
        "finished_at",
        "progress",
        "creator_key",
        "creator_filter",
        "creator_filters",
        "scope",
        "window",
        "cutoff_at",
        "checked_until",
        "max_items",
        "source_count",
        "discovery_coverage",
        "already_ingested_count",
        "pending_found_count",
        "deferred_due_to_cap_count",
        "error",
        "error_count",
        "stop_reason",
        "transitions",
        "ingestion_groups",
        "errors",
        "completed",
        "requested_sample_size",
        "evaluation_run_id",
        "discovered_count",
        "eligible_count",
        "selected_count",
        "selected_queue_ids",
        "candidate_queue_ids",
        "existing_delivery_target_ids",
        "completed_count",
        "duplicate_count",
        "failed_count",
        "queued_for_analysis_count",
        "analysis_completed_manifest_count",
        "analysis_finalized_count",
        "analysis_duplicate_count",
        "analysis_insufficient_count",
        "analysis_not_ready_count",
        "analysis_queue_missing_count",
        "analysis_unaccounted_count",
        "analysis_accounted_count",
        "analysis_disposition_counts",
        "analysis_unaccounted_items",
        "sample_complete",
        "shortfall_reason",
        "creator",
        "source_platform",
        "completed_ids",
        "failed_ids",
        "failure_count",
        "failures",
        "recent_found_count",
        "selected_for_ingestion_count",
        "selected_standard_ingestion_count",
        "story_current_count",
        "story_newly_promoted_count",
        "story_reused_existing_count",
        "story_reattributed_count",
        "story_identity_aliases_retired_count",
        "queued_for_analysis_count",
        "analysis_accounted_count",
        "analysis_missing_count",
        "analysis_missing_items",
        "analysis_candidate_coverage",
        "analysis_candidate_states",
        "analysis_readiness_complete",
        "story_visual_enrichment",
        "story_items",
        "analysis_targets",
        "insufficient_content_count",
        "insufficient_content_items",
        "deferred_extraction_count",
        "deferred_extraction_items",
        "extraction_error_count",
        "extraction_error_items",
        "pending_extraction_count",
        "pending_extraction_items",
        "provider_circuit_breaker",
        "provider_health",
        "timings",
    }
    return {key: obj[key] for key in allowed if key in obj}


_DETAIL_ARRAY_COUNT_KEYS = {
    "completed_ids": "completed_id_count",
    "failed_ids": "failed_id_count",
    "failures": "failure_detail_count",
    "errors": "error_detail_count",
    "story_items": "story_item_count",
    "analysis_targets": "analysis_target_count",
    "insufficient_content_items": "insufficient_content_count",
    "deferred_extraction_items": "deferred_extraction_count",
    "extraction_error_items": "extraction_error_count",
    "pending_extraction_items": "pending_extraction_count",
    "creator_filters": "creator_filter_count",
    "discovery_coverage": "discovery_coverage_count",
    "transitions": "transition_count",
    "ingestion_groups": "ingestion_group_count",
    "analysis_missing_items": "analysis_missing_count",
    "analysis_candidate_states": "analysis_candidate_state_count",
    "analysis_unaccounted_items": "analysis_unaccounted_count",
    "selected_queue_ids": "selected_queue_id_count",
    "candidate_queue_ids": "candidate_queue_id_count",
    "existing_delivery_target_ids": "existing_delivery_target_count",
}


def compact_status_details(status: dict | None) -> dict | None:
    """Reduce verbose job status for routine MCP polling without losing persisted detail."""
    if not isinstance(status, dict):
        return status

    out: dict = {}
    for key, value in status.items():
        count_key = _DETAIL_ARRAY_COUNT_KEYS.get(key)
        if count_key and isinstance(value, list):
            if count_key not in status and count_key not in out:
                out[count_key] = len(value)
            continue

        if key == "story_visual_enrichment" and isinstance(value, dict):
            compact = {}
            for nested in ("totals", "ollama_budget"):
                if nested in value:
                    compact[nested] = value[nested]
            out[key] = compact
            continue

        if key == "timings" and isinstance(value, dict):
            compact = {}
            for nested in (
                "total_duration_ms",
                "discovery_wall_ms",
                "discovery_parallelism",
                "stage_totals_ms",
            ):
                if nested in value:
                    compact[nested] = value[nested]
            out[key] = compact
            continue

        if key == "provider_health" and isinstance(value, dict):
            compact_providers = {}
            for provider, provider_state in value.items():
                if not isinstance(provider_state, dict):
                    continue
                compact_providers[provider] = {
                    nested: provider_state[nested]
                    for nested in (
                        "last_success_at",
                        "last_error_at",
                        "last_429_at",
                        "cooldown_until",
                        "last_code",
                        "last_status",
                        "retry_after_source",
                        "consecutive_transient_failures",
                    )
                    if nested in provider_state
                }
            out[key] = compact_providers
            continue

        out[key] = value
    return out


def compact_job_status(payload: dict) -> dict:
    """Compact active/last JobManager payloads while preserving job-level lifecycle fields."""
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    for slot in ("active", "last"):
        job = out.get(slot)
        if isinstance(job, dict) and isinstance(job.get("status"), dict):
            job = dict(job)
            job["status"] = compact_status_details(job["status"])
            out[slot] = job
    return out
