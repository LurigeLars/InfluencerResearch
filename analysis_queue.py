from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apply_research_decisions import (
    ANALYSIS_OWNER,
    CURRENT_DECISIONS,
    SCREEN_VERSION,
    atomic_write_json,
    build_followup,
    default_followup_registry,
    manifest_research_fields,
    markdown,
    parse_iso,
    should_open_followup,
    utc_now,
    validate_decision,
    validate_followup_registry,
)


DECISION_FIELDS = (
    "decision",
    "idea_type",
    "claim_summary",
    "claims",
    "existing_system_overlap",
    "verification_plan",
    "falsifiable_test",
    "main_risk",
    "confidence",
    "rationale",
    "evidence_lineage_id",
    "duplicate_of",
    "duplicate_basis",
)


def load_json(path: Path, default: Any) -> Any:
    import json
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _queue_id(item: dict) -> str:
    return str(item.get("queue_id") or item.get("shortcode") or "")


def _active_creator_keys(root: Path) -> set[str]:
    registry = load_json(root / "control" / "creator_registry.json", {"creators": {}})
    creators = registry.get("creators", {}) if isinstance(registry, dict) else {}
    return {
        str(key)
        for key, value in creators.items()
        if isinstance(value, dict) and str(value.get("status") or "ACTIVE").upper() == "ACTIVE"
    }


def _compact_queue_item(item: dict) -> dict:
    caption = str(item.get("caption") or "")
    if len(caption) > 500:
        caption = caption[:497] + "..."
    return {
        key: value
        for key, value in {
            "queue_id": _queue_id(item),
            "analysis_status": item.get("analysis_status"),
            "analysis_content_status": item.get("analysis_content_status"),
            "creator": item.get("creator"),
            "source_platform": item.get("source_platform"),
            "source_type": item.get("source_type"),
            "source_id": item.get("source_id"),
            "source_url": item.get("source_url") or item.get("url"),
            "published_at": item.get("published_at"),
            "caption": caption,
            "evidence_lineage_id": item.get("evidence_lineage_id"),
            "duplicate_of": item.get("duplicate_of"),
            "duplicate_basis": item.get("duplicate_basis"),
            "analysis_mode_recommended": item.get("analysis_mode_recommended"),
            "visual_review_recommended": item.get("visual_review_recommended"),
        }.items()
        if value is not None
    }


def list_analysis_queue(
    root: Path,
    *,
    creator_key: str | None = None,
    source_platform: str | None = None,
    published_after: str | None = None,
    published_before: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    root = root.resolve()
    queue = load_json(root / "state" / "research_queue.json", {"items": []})
    active = _active_creator_keys(root)
    after = parse_iso(published_after) if published_after else None
    before = parse_iso(published_before) if published_before else None
    if published_after and after is None:
        raise ValueError("BAD_PUBLISHED_AFTER")
    if published_before and before is None:
        raise ValueError("BAD_PUBLISHED_BEFORE")

    filtered: list[dict] = []
    for item in queue.get("items", []) if isinstance(queue.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("analysis_status") or "").upper() != "PENDING_ANALYSIS":
            continue
        creator = str(item.get("creator") or "")
        if creator not in active:
            continue
        if creator_key and creator != creator_key:
            continue
        if source_platform and str(item.get("source_platform") or "").upper() != source_platform:
            continue
        published = parse_iso(item.get("published_at"))
        if after and (published is None or published < after):
            continue
        if before and (published is None or published > before):
            continue
        filtered.append(item)

    filtered.sort(key=lambda x: (str(x.get("published_at") or ""), _queue_id(x)), reverse=True)
    total = len(filtered)
    page = filtered[offset: offset + limit]
    return {
        "status": "PENDING_ANALYSIS",
        "count": len(page),
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_compact_queue_item(item) for item in page],
    }


def get_analysis_queue_item(root: Path, queue_id: str) -> dict:
    root = root.resolve()
    wanted = str(queue_id or "").strip()
    if not wanted:
        raise ValueError("BAD_QUEUE_ID")
    queue = load_json(root / "state" / "research_queue.json", {"items": []})
    decisions = load_json(root / "state" / "research_decisions.json", {"items": {}})
    for item in queue.get("items", []) if isinstance(queue.get("items"), list) else []:
        if isinstance(item, dict) and _queue_id(item) == wanted:
            return {
                "result": "FOUND",
                "queue_item": item,
                "decision": (decisions.get("items") or {}).get(wanted),
            }
    existing = (decisions.get("items") or {}).get(wanted)
    if isinstance(existing, dict):
        manifest = load_json(root / "state" / "manifest.json", {"items": {}})
        return {
            "result": "FINALIZED",
            "queue_item": None,
            "manifest_item": (manifest.get("items") or {}).get(wanted),
            "decision": existing,
        }
    return {"result": "NOT_FOUND", "queue_id": wanted}


def _decision_payload(payload: dict, *, source_url: str, screened_at: str) -> dict:
    record = {field: payload.get(field) for field in DECISION_FIELDS}
    record["decision"] = str(record.get("decision") or "").upper()
    record["source_url"] = source_url
    record["screened_at"] = screened_at
    return record


def _same_decision(existing: dict, payload: dict) -> bool:
    return all(existing.get(field) == payload.get(field) for field in DECISION_FIELDS)


def _apply_record(root: Path, queue_id: str, record: dict) -> dict:
    decisions_path = root / "state" / "research_decisions.json"
    manifest_path = root / "state" / "manifest.json"
    queue_path = root / "state" / "research_queue.json"
    followups_path = root / "state" / "research_followups.json"

    decisions = load_json(
        decisions_path,
        {"schema_version": 2, "screen_version": SCREEN_VERSION, "items": {}},
    )
    manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
    queue = load_json(queue_path, {"items": []})
    followups = load_json(followups_path, default_followup_registry())

    item = (manifest.get("items") or {}).get(queue_id)
    if not isinstance(item, dict):
        return {"result": "NOT_FOUND", "queue_id": queue_id, "error": "MANIFEST_ITEM_NOT_FOUND"}

    existing = (decisions.get("items") or {}).get(queue_id)
    if isinstance(existing, dict):
        if not _same_decision(existing, record):
            return {
                "result": "CONFLICT",
                "queue_id": queue_id,
                "existing_decision": existing.get("decision"),
                "requested_decision": record.get("decision"),
            }
        canonical = existing
        result = "NO_OP"
    else:
        errors = validate_decision(queue_id, record)
        if errors:
            return {"result": "VALIDATION_ERROR", "queue_id": queue_id, "errors": errors}
        decisions.setdefault("items", {})[queue_id] = record
        canonical = record
        result = "WRITTEN"

    desired = manifest_research_fields(canonical, item)
    item.update(desired)

    activation = parse_iso(followups.get("activation_screened_at"))
    if activation is None:
        return {"result": "VALIDATION_ERROR", "queue_id": queue_id, "errors": ["BAD_FOLLOWUP_ACTIVATION"]}
    followup_items = followups.setdefault("items", {})
    if queue_id not in followup_items and should_open_followup(canonical, activation):
        followup_items[queue_id] = build_followup(queue_id, canonical, item)

    followup_errors = validate_followup_registry(followups, decisions, manifest)
    if followup_errors:
        return {"result": "VALIDATION_ERROR", "queue_id": queue_id, "errors": followup_errors}

    old_queue_items = queue.get("items", []) if isinstance(queue.get("items"), list) else []
    queue_items = [
        x for x in old_queue_items
        if not isinstance(x, dict) or _queue_id(x) != queue_id
    ]
    queue["items"] = queue_items
    queue["count"] = len(queue_items)
    queue["analysis_owner"] = ANALYSIS_OWNER
    queue["status"] = "PENDING_ANALYSIS" if queue_items else "EMPTY"
    queue["screen_version"] = SCREEN_VERSION
    queue["updated_at"] = utc_now()

    decisions["screen_version"] = SCREEN_VERSION
    manifest["items"][queue_id] = item
    followups["updated_at"] = utc_now()

    # Existing governance uses these same canonical files. Validate everything first,
    # then write source-of-truth state; identical replay repairs an interrupted apply.
    atomic_write_json(decisions_path, decisions)
    atomic_write_json(manifest_path, manifest)
    atomic_write_json(followups_path, followups)
    atomic_write_json(queue_path, queue)

    creator = str(item.get("creator") or "unknown")
    decision_dir = root / "output" / creator / "research" / "decisions"
    json_path = decision_dir / f"{queue_id}.json"
    md_path = decision_dir / f"{queue_id}.md"
    atomic_write_json(json_path, canonical)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = md_path.with_suffix(md_path.suffix + ".tmp")
    tmp.write_text(markdown(queue_id, canonical, item), encoding="utf-8")
    import os
    os.replace(tmp, md_path)

    return {
        "result": result,
        "queue_id": queue_id,
        "decision": canonical.get("decision"),
        "analysis_status": "FINALIZED",
        "remaining_pending": len(queue_items),
        "screened_at": canonical.get("screened_at"),
    }


def record_analysis_decision(root: Path, queue_id: str, payload: dict) -> dict:
    root = root.resolve()
    wanted = str(queue_id or "").strip()
    if not wanted:
        raise ValueError("BAD_QUEUE_ID")
    decision = str(payload.get("decision") or "").upper()
    if decision not in CURRENT_DECISIONS:
        raise ValueError("BAD_DECISION")

    decisions = load_json(root / "state" / "research_decisions.json", {"items": {}})
    existing = (decisions.get("items") or {}).get(wanted)
    if isinstance(existing, dict):
        probe = _decision_payload(
            payload,
            source_url=str(existing.get("source_url") or ""),
            screened_at=str(existing.get("screened_at") or utc_now()),
        )
        return _apply_record(root, wanted, probe)

    queue = load_json(root / "state" / "research_queue.json", {"items": []})
    queue_item = next(
        (
            item for item in queue.get("items", [])
            if isinstance(item, dict) and _queue_id(item) == wanted
        ),
        None,
    )
    if queue_item is None:
        return {"result": "NOT_FOUND", "queue_id": wanted}
    if str(queue_item.get("analysis_status") or "").upper() != "PENDING_ANALYSIS":
        return {"result": "CONFLICT", "queue_id": wanted, "error": "ITEM_NOT_PENDING"}

    manifest = load_json(root / "state" / "manifest.json", {"items": {}})
    manifest_item = (manifest.get("items") or {}).get(wanted)
    if not isinstance(manifest_item, dict):
        return {"result": "NOT_FOUND", "queue_id": wanted, "error": "MANIFEST_ITEM_NOT_FOUND"}

    expected_lineage = (
        queue_item.get("evidence_lineage_id")
        or manifest_item.get("evidence_lineage_id")
    )
    supplied_lineage = payload.get("evidence_lineage_id")
    if expected_lineage and supplied_lineage != expected_lineage:
        return {
            "result": "VALIDATION_ERROR",
            "queue_id": wanted,
            "errors": ["EVIDENCE_LINEAGE_MISMATCH"],
        }

    source_url = str(
        queue_item.get("source_url")
        or queue_item.get("url")
        or manifest_item.get("url")
        or ""
    )
    record = _decision_payload(payload, source_url=source_url, screened_at=utc_now())
    return _apply_record(root, wanted, record)


def record_analysis_decision_batch(root: Path, items: list[dict]) -> dict:
    if not 1 <= len(items) <= 20:
        raise ValueError("BAD_BATCH_SIZE")
    results = []
    written = no_op = failed = 0
    for index, item in enumerate(items):
        queue_id = str(item.get("queue_id") or "")
        try:
            result = record_analysis_decision(root, queue_id, item)
        except Exception as exc:
            result = {
                "result": "FAILED",
                "queue_id": queue_id,
                "error": f"{type(exc).__name__}:{exc}",
            }
        kind = str(result.get("result") or "")
        if kind == "WRITTEN":
            written += 1
        elif kind == "NO_OP":
            no_op += 1
        else:
            failed += 1
        results.append({"index": index, **result})
    return {
        "written": written,
        "no_op": no_op,
        "failed": failed,
        "results": results,
    }
