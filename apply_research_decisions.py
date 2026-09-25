from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCREEN_VERSION = "0.2.0"
ANALYSIS_OWNER = "EKONOMI"
APPLY_VERSION = "0.3.0"
FOLLOWUP_LIFECYCLE_VERSION = "0.1.0"
# Migration boundary: the four RESEARCH items in the 2026-08-22 22:55 UTC batch
# are the first items governed by HANDOFF-005. Earlier RESEARCH decisions remain
# historical and are not silently reopened.
FOLLOWUP_ACTIVATION_SCREENED_AT = "2026-08-22T22:54:59+00:00"

CURRENT_DECISIONS = {"IGNORE", "RESEARCH", "TEST_CANDIDATE", "BACKLOG_CANDIDATE"}
LEGACY_DECISIONS = {"TEST", "BACKLOG"}
ALLOWED_DECISIONS = CURRENT_DECISIONS | LEGACY_DECISIONS
NORMALIZE_DECISION = {
    "TEST": "TEST_CANDIDATE",
    "BACKLOG": "BACKLOG_CANDIDATE",
}
ALLOWED_DUPLICATE_BASIS = {
    None,
    "TRANSCRIPT_MATCH",
    "EXACT_SOURCE_URL",
    "CROSS_PLATFORM_REPOST",
    "POSSIBLE_SEMANTIC_DUPLICATE",
    "OTHER_EXPLICIT",
}
ALLOWED_IDEA_TYPES = {
    "TRADE_IDEA",
    "MARKET_STRUCTURE",
    "MACRO",
    "DATA_TOOL",
    "SYSTEM_AUTOMATION",
    "QUANT_METHOD",
    "PORTFOLIO_RISK",
    "EDUCATION",
    "PROMOTIONAL",
    "OTHER",
}
REQUIRED_FIELDS = {
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
}

FOLLOWUP_STATUSES = {"OPEN_RESEARCH", "IN_PROGRESS", "RESOLVED"}
FOLLOWUP_RESOLUTIONS = {
    None,
    "IGNORE",
    "CONTINUE_RESEARCH",
    "TEST_CANDIDATE",
    "BACKLOG_CANDIDATE",
    "CANDIDATE",
    "OTHER",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_json(path: Path, default: Any = None) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if default is not None:
        return default
    raise FileNotFoundError(path)


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def normalized_decision(value: str) -> str:
    return NORMALIZE_DECISION.get(value, value)


def validate_decision(shortcode: str, d: dict) -> list[str]:
    errors = []
    missing = sorted(REQUIRED_FIELDS - set(d))
    if missing:
        errors.append(f"{shortcode}: missing fields: {', '.join(missing)}")
    if d.get("decision") not in ALLOWED_DECISIONS:
        errors.append(f"{shortcode}: invalid decision {d.get('decision')!r}")
    if d.get("idea_type") not in ALLOWED_IDEA_TYPES:
        errors.append(f"{shortcode}: invalid idea_type {d.get('idea_type')!r}")
    confidence = d.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0 <= float(confidence) <= 1):
        errors.append(f"{shortcode}: confidence must be numeric 0..1")
    if not isinstance(d.get("claims"), list):
        errors.append(f"{shortcode}: claims must be a list")
    if not isinstance(d.get("verification_plan"), list):
        errors.append(f"{shortcode}: verification_plan must be a list")
    if not isinstance(d.get("evidence_lineage_id"), str) or not d.get("evidence_lineage_id"):
        errors.append(f"{shortcode}: evidence_lineage_id must be a non-empty string")
    duplicate_of = d.get("duplicate_of")
    if duplicate_of is not None and not isinstance(duplicate_of, str):
        errors.append(f"{shortcode}: duplicate_of must be string or null")
    if d.get("duplicate_basis") not in ALLOWED_DUPLICATE_BASIS:
        errors.append(f"{shortcode}: invalid duplicate_basis {d.get('duplicate_basis')!r}")
    if duplicate_of and not d.get("duplicate_basis"):
        errors.append(f"{shortcode}: duplicate_basis required when duplicate_of is set")
    if d.get("screened_at") is not None and parse_iso(d.get("screened_at")) is None:
        errors.append(f"{shortcode}: invalid screened_at timestamp")
    return errors


def default_followup_registry() -> dict:
    return {
        "schema_version": 1,
        "lifecycle_version": FOLLOWUP_LIFECYCLE_VERSION,
        "analysis_owner": ANALYSIS_OWNER,
        "activation_screened_at": FOLLOWUP_ACTIVATION_SCREENED_AT,
        "items": {},
    }


def validate_followup_registry(followups: dict, decisions: dict, manifest: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(followups, dict):
        return ["research_followups: root must be an object"]
    if followups.get("schema_version") != 1:
        errors.append("research_followups: schema_version must be 1")
    if followups.get("lifecycle_version") != FOLLOWUP_LIFECYCLE_VERSION:
        errors.append(
            f"research_followups: lifecycle_version must be {FOLLOWUP_LIFECYCLE_VERSION}"
        )
    if followups.get("analysis_owner") != ANALYSIS_OWNER:
        errors.append(f"research_followups: analysis_owner must be {ANALYSIS_OWNER}")
    if parse_iso(followups.get("activation_screened_at")) is None:
        errors.append("research_followups: activation_screened_at must be a valid timestamp")
    items = followups.get("items")
    if not isinstance(items, dict):
        errors.append("research_followups: items must be an object")
        return errors

    decision_items = decisions.get("items", {})
    manifest_items = manifest.get("items", {})
    for shortcode, f in items.items():
        if shortcode not in decision_items:
            errors.append(f"{shortcode}: followup has no matching research_decision")
            continue
        if shortcode not in manifest_items:
            errors.append(f"{shortcode}: followup has no matching manifest item")
            continue
        d = decision_items[shortcode]
        m = manifest_items[shortcode]
        if normalized_decision(str(d.get("decision"))) != "RESEARCH":
            errors.append(f"{shortcode}: followup only allowed for RESEARCH decisions")
        if not isinstance(f, dict):
            errors.append(f"{shortcode}: followup must be an object")
            continue
        required = {
            "source_item",
            "source_url",
            "evidence_lineage_id",
            "research_owner",
            "status",
            "opened_at",
            "updated_at",
            "resolution",
            "resolution_note",
            "reference",
            "requires_system_handoff",
            "system_change_authorized",
        }
        missing = sorted(required - set(f))
        if missing:
            errors.append(f"{shortcode}: followup missing fields: {', '.join(missing)}")
            continue
        if f.get("source_item") != shortcode:
            errors.append(f"{shortcode}: followup source_item mismatch")
        expected_url = d.get("source_url") or m.get("url") or ""
        if f.get("source_url") != expected_url:
            errors.append(f"{shortcode}: followup source_url mismatch")
        if f.get("evidence_lineage_id") != d.get("evidence_lineage_id"):
            errors.append(f"{shortcode}: followup evidence_lineage_id mismatch")
        if f.get("research_owner") != ANALYSIS_OWNER:
            errors.append(f"{shortcode}: followup research_owner must be {ANALYSIS_OWNER}")
        status = f.get("status")
        if status not in FOLLOWUP_STATUSES:
            errors.append(f"{shortcode}: invalid followup status {status!r}")
        if parse_iso(f.get("opened_at")) is None or parse_iso(f.get("updated_at")) is None:
            errors.append(f"{shortcode}: followup timestamps must be valid")
        resolution = f.get("resolution")
        if resolution not in FOLLOWUP_RESOLUTIONS:
            errors.append(f"{shortcode}: invalid followup resolution {resolution!r}")
        if status == "RESOLVED" and resolution is None:
            errors.append(f"{shortcode}: RESOLVED followup requires resolution")
        if status != "RESOLVED" and resolution is not None:
            errors.append(f"{shortcode}: non-RESOLVED followup must have null resolution")
        if f.get("resolution_note") is not None and not isinstance(f.get("resolution_note"), str):
            errors.append(f"{shortcode}: resolution_note must be string or null")
        if f.get("reference") is not None and not isinstance(f.get("reference"), str):
            errors.append(f"{shortcode}: reference must be string or null")
        expected_handoff = resolution in {"TEST_CANDIDATE", "BACKLOG_CANDIDATE"}
        if bool(f.get("requires_system_handoff")) != expected_handoff:
            errors.append(f"{shortcode}: requires_system_handoff inconsistent with resolution")
        if f.get("system_change_authorized") is not False:
            errors.append(f"{shortcode}: system_change_authorized must remain false")
    return errors


def should_open_followup(d: dict, activation: datetime) -> bool:
    if normalized_decision(str(d.get("decision"))) != "RESEARCH":
        return False
    if d.get("duplicate_of"):
        return False
    screened = parse_iso(d.get("screened_at"))
    return screened is not None and screened >= activation


def build_followup(shortcode: str, d: dict, manifest_item: dict) -> dict:
    opened_at = d.get("screened_at") or utc_now()
    return {
        "source_item": shortcode,
        "source_url": d.get("source_url") or manifest_item.get("url") or "",
        "evidence_lineage_id": d["evidence_lineage_id"],
        "research_owner": ANALYSIS_OWNER,
        "status": "OPEN_RESEARCH",
        "opened_at": opened_at,
        "updated_at": opened_at,
        "resolution": None,
        "resolution_note": None,
        "reference": None,
        "requires_system_handoff": False,
        "system_change_authorized": False,
    }


def manifest_research_fields(d: dict, existing: dict) -> dict:
    decision = normalized_decision(d["decision"])
    return {
        "research_status": "ANALYZED",
        "analysis_owner": ANALYSIS_OWNER,
        "research_decision": d["decision"],
        "research_decision_normalized": decision,
        "research_idea_type": d["idea_type"],
        "research_confidence": d["confidence"],
        "research_screened_at": d.get("screened_at") or existing.get("research_screened_at") or utc_now(),
        "research_screen_version": SCREEN_VERSION,
        "evidence_lineage_id": d["evidence_lineage_id"],
        "duplicate_of": d.get("duplicate_of"),
        "duplicate_basis": d.get("duplicate_basis"),
        "requires_system_handoff": decision in {"TEST_CANDIDATE", "BACKLOG_CANDIDATE"},
        "system_change_authorized": False,
    }


def markdown(shortcode: str, d: dict, manifest_item: dict) -> str:
    decision = normalized_decision(d["decision"])
    lines = [
        f"# Research screen — {shortcode}",
        "",
        f"- Analysis owner: {ANALYSIS_OWNER}",
        f"- Creator: @{manifest_item.get('creator', '')}",
        f"- Source: {manifest_item.get('url', '')}",
        f"- Decision: **{decision}**",
        f"- Idea type: `{d['idea_type']}`",
        f"- Confidence: {d['confidence']}",
        f"- Evidence lineage: `{d['evidence_lineage_id']}`",
        f"- Duplicate of: `{d.get('duplicate_of') or ''}`",
        f"- Duplicate basis: `{d.get('duplicate_basis') or ''}`",
        "",
        "## Claim summary",
        str(d["claim_summary"]),
        "",
        "## Claims",
    ]
    for claim in d.get("claims", []):
        if isinstance(claim, dict):
            text = claim.get("claim") or claim.get("text") or json.dumps(claim, ensure_ascii=False)
        else:
            text = str(claim)
        lines.append(f"- {text}")
    lines += [
        "",
        "## Existing system overlap",
        str(d["existing_system_overlap"]),
        "",
        "## Verification plan",
    ]
    for step in d.get("verification_plan", []):
        lines.append(f"- {step}")
    lines += [
        "",
        "## Falsifiable test",
        str(d["falsifiable_test"]),
        "",
        "## Main risk",
        str(d["main_risk"]),
        "",
        "## Rationale",
        str(d["rationale"]),
        "",
        "_Influencer source is discovery only. Ekonomi owns analysis. " +
        "TEST/BACKLOG candidates do not authorize system changes or trades._",
        "",
    ]
    return "\n".join(lines)


def write_apply_status(path: Path, **kwargs: Any) -> None:
    payload = {
        "schema_version": 1,
        "apply_version": APPLY_VERSION,
        "screen_version": SCREEN_VERSION,
        "analysis_owner": ANALYSIS_OWNER,
        "completed_at": utc_now(),
        **kwargs,
    }
    atomic_write_json(path, payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    args = parser.parse_args()
    root = args.root.resolve()

    decisions_path = root / "state" / "research_decisions.json"
    manifest_path = root / "state" / "manifest.json"
    queue_path = root / "state" / "research_queue.json"
    followups_path = root / "state" / "research_followups.json"
    apply_status_path = root / "state" / "research_apply_status.json"

    decisions = load_json(
        decisions_path,
        {"schema_version": 2, "screen_version": SCREEN_VERSION, "items": {}},
    )
    manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
    followups = load_json(followups_path, default_followup_registry())

    all_errors: list[str] = []
    valid_items: dict[str, dict] = {}

    for shortcode, d in decisions.get("items", {}).items():
        if shortcode not in manifest.get("items", {}):
            all_errors.append(f"{shortcode}: not found in manifest")
            continue
        errs = validate_decision(shortcode, d)
        all_errors.extend(errs)
        if not errs:
            valid_items[shortcode] = d

    all_errors.extend(validate_followup_registry(followups, decisions, manifest))

    if all_errors:
        write_apply_status(
            apply_status_path,
            result="FAILED_VALIDATION",
            exit_code=2,
            errors=all_errors,
            applied=0,
            already_applied=0,
            followups_opened=0,
            remaining_queue=(load_json(queue_path, {}).get("count") if queue_path.exists() else None),
            system_changes_authorized=0,
        )
        print("DECISION/FOLLOWUP VALIDATION FAILED")
        for err in all_errors:
            print(f"- {err}")
        print("No manifest, queue or followup changes were applied.")
        return 2

    applied = 0
    already_applied = 0
    sidecars_repaired = 0
    manifest_changed = False

    for shortcode, d in valid_items.items():
        item = manifest["items"][shortcode]
        desired = manifest_research_fields(d, item)
        changed = any(item.get(k) != v for k, v in desired.items())
        if changed:
            item.update(desired)
            applied += 1
            manifest_changed = True
        else:
            already_applied += 1

        creator = str(item.get("creator") or "unknown")
        decision_dir = root / "output" / creator / "research" / "decisions"
        json_path = decision_dir / f"{shortcode}.json"
        md_path = decision_dir / f"{shortcode}.md"
        desired_json = json.dumps(d, ensure_ascii=False, indent=2) + "\n"
        desired_md = markdown(shortcode, d, item)
        if not json_path.exists() or json_path.read_text(encoding="utf-8") != desired_json:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = json_path.with_suffix(json_path.suffix + ".tmp")
            tmp.write_text(desired_json, encoding="utf-8")
            os.replace(tmp, json_path)
            sidecars_repaired += 1
        if not md_path.exists() or md_path.read_text(encoding="utf-8") != desired_md:
            md_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = md_path.with_suffix(md_path.suffix + ".tmp")
            tmp.write_text(desired_md, encoding="utf-8")
            os.replace(tmp, md_path)
            sidecars_repaired += 1

    activation = parse_iso(followups.get("activation_screened_at"))
    assert activation is not None
    followups_opened = 0
    followup_items = followups["items"]
    for shortcode, d in valid_items.items():
        if shortcode in followup_items:
            continue
        if should_open_followup(d, activation):
            followup_items[shortcode] = build_followup(shortcode, d, manifest["items"][shortcode])
            followups_opened += 1

    # Revalidate the generated registry before any source-of-truth writes.
    generated_followup_errors = validate_followup_registry(followups, decisions, manifest)
    if generated_followup_errors:
        write_apply_status(
            apply_status_path,
            result="FAILED_INTERNAL_VALIDATION",
            exit_code=3,
            errors=generated_followup_errors,
            applied=0,
            already_applied=already_applied,
            followups_opened=0,
            remaining_queue=(load_json(queue_path, {}).get("count") if queue_path.exists() else None),
            system_changes_authorized=0,
        )
        print("GENERATED FOLLOWUP VALIDATION FAILED")
        for err in generated_followup_errors:
            print(f"- {err}")
        print("No manifest, queue or followup changes were applied.")
        return 3

    queue_changed = False
    queue = None
    if queue_path.exists():
        queue = load_json(queue_path, {})
        queue_items = [
            x for x in queue.get("items", [])
            if str(x.get("shortcode")) not in valid_items
        ]
        desired_queue = {
            "items": queue_items,
            "count": len(queue_items),
            "analysis_owner": ANALYSIS_OWNER,
            "status": "PENDING_ANALYSIS" if queue_items else "EMPTY",
            "screen_version": SCREEN_VERSION,
        }
        for key, value in desired_queue.items():
            if queue.get(key) != value:
                queue[key] = value
                queue_changed = True
        if queue_changed:
            queue["updated_at"] = utc_now()

    if manifest_changed:
        atomic_write_json(manifest_path, manifest)
    if followups_opened or not followups_path.exists():
        followups["updated_at"] = utc_now()
        atomic_write_json(followups_path, followups)
    if queue_changed and queue is not None:
        atomic_write_json(queue_path, queue)

    remaining_queue = queue.get("count") if queue is not None else None
    result = {
        "result": "DONE",
        "exit_code": 0,
        "applied": applied,
        "already_applied": already_applied,
        "sidecars_repaired": sidecars_repaired,
        "manifest_changed": manifest_changed,
        "followups_opened": followups_opened,
        "followups_total": len(followups["items"]),
        "remaining_queue": remaining_queue,
        "system_changes_authorized": 0,
    }
    write_apply_status(apply_status_path, **result)
    print(json.dumps({
        "apply_version": APPLY_VERSION,
        "screen_version": SCREEN_VERSION,
        "analysis_owner": ANALYSIS_OWNER,
        **result,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
