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
        "already_ingested_count",
        "pending_found_count",
        "deferred_due_to_cap_count",
        "error",
        "error_count",
        "errors",
        "completed",
        "completed_ids",
        "failed_ids",
        "failure_count",
        "recent_found_count",
        "selected_for_ingestion_count",
        "selected_standard_ingestion_count",
        "story_current_count",
        "story_newly_promoted_count",
        "story_reused_existing_count",
        "story_reattributed_count",
        "story_identity_aliases_retired_count",
        "queued_for_analysis_count",
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
