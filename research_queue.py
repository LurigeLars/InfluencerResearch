from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


SCREEN_VERSION = "0.4.7"
ANALYSIS_OWNER = "EKONOMI"
MIN_TRANSCRIPT_WORDS = 8
MIN_TRANSCRIPT_CHARS = 48
MIN_FALLBACK_TEXT_WORDS = 14
MIN_FALLBACK_TEXT_CHARS = 90
FINAL_DECISIONS = {
    "IGNORE",
    "RESEARCH",
    "TEST_CANDIDATE",
    "BACKLOG_CANDIDATE",
    # Backward compatibility for decisions created before HANDOFF-001.
    "TEST",
    "BACKLOG",
}
CURRENT_DECISIONS = ["IGNORE", "RESEARCH", "TEST_CANDIDATE", "BACKLOG_CANDIDATE"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any = None) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if default is not None:
        return default
    raise FileNotFoundError(path)


def _json_text(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_json_text(data), encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json_if_changed(path: Path, data: dict) -> bool:
    text = _json_text(data)
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def normalize_manifest_path(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    return root / Path(value)


def canonicalize_url(value: str | None) -> str:
    if not value:
        return ""
    try:
        parts = urlsplit(str(value).strip())
        host = parts.netloc.lower()
        path = re.sub(r"/+$", "", parts.path)
        return urlunsplit((parts.scheme.lower(), host, path, "", ""))
    except Exception:
        return str(value).strip().rstrip("/")


def transcript_fingerprint(text: str) -> str | None:
    # Deliberately strict: only near-verbatim reposts should be auto-deduped.
    normalized = re.sub(r"[^\w]+", " ", text.casefold(), flags=re.UNICODE)
    normalized = " ".join(normalized.split())
    if len(normalized.split()) < 40:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def lineage_id(*, transcript_fp: str | None, source_url: str, shortcode: str) -> str:
    if transcript_fp:
        seed = f"transcript:{transcript_fp}"
    elif source_url:
        seed = f"url:{source_url}"
    else:
        seed = f"id:{shortcode}"
    return "EL-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def keyword_tags(text: str) -> list[str]:
    """Low-stakes discovery tags only. These never create a research decision."""
    lower = text.lower()
    groups = {
        "market_structure": [
            "liquidity", "order flow", "market maker", "dealer", "gamma",
            "volatility", "vwap", "volume", "stop run", "liquidation",
        ],
        "macro": [
            "fed", "federal reserve", "inflation", "rates", "yield",
            "treasury", "dollar", "macro", "recession", "cpi",
        ],
        "systematic_quant": [
            "backtest", "factor", "signal", "systematic", "quant",
            "algorithm", "model", "regime", "portfolio construction",
        ],
        "tools_data": [
            "api", "terminal", "platform", "data", "scanner", "tradingview",
            "bloomberg", "openbb", "python", "mcp",
        ],
        "trade_setup": [
            "entry", "stop", "target", "long", "short", "setup",
            "breakout", "reversal", "risk reward",
        ],
        "promotion": [
            "comment", "link in bio", "newsletter", "letter", "course",
            "subscribe", "free guide", "dm me",
        ],
    }
    tags = []
    for tag, words in groups.items():
        if any(word in lower for word in words):
            tags.append(tag)
    return tags



def assess_analysis_content(item: dict, transcript: str) -> dict:
    """Fail closed when the extracted evidence is too thin for model analysis."""
    transcript = str(transcript or "").strip()
    caption = str(item.get("caption") or "").strip()
    browser_text = str(item.get("browser_text") or "").strip()
    visible_text = str(item.get("visible_text") or "").strip()
    visual_status = str(item.get("visual_description_status") or "").upper()
    raw_visual_description = str(item.get("visual_description") or "").strip()
    # Never admit stale visual text while the current extraction is deferred/error.
    visual_description = raw_visual_description if visual_status == "DONE" else ""

    transcript_words = len(transcript.split())
    caption_words = len(caption.split())
    browser_text_words = len(browser_text.split())
    visible_text_words = len(visible_text.split())
    visual_description_words = len(visual_description.split())
    has_visual_evidence = bool(
        item.get("screenshot_file")
        or (
            str(item.get("visual_evidence_status") or "").upper() == "DONE"
            and int(item.get("visual_frame_count") or 0) > 0
        )
    )

    if transcript_words >= MIN_TRANSCRIPT_WORDS or len(transcript) >= MIN_TRANSCRIPT_CHARS:
        status = "READY"
        reason = "TRANSCRIPT"
    elif visual_description_words >= MIN_TRANSCRIPT_WORDS or len(visual_description) >= MIN_TRANSCRIPT_CHARS:
        status = "READY"
        reason = "VISUAL_DESCRIPTION"
    elif visible_text_words >= MIN_TRANSCRIPT_WORDS or len(visible_text) >= MIN_TRANSCRIPT_CHARS:
        status = "READY"
        reason = "VISIBLE_TEXT"
    elif browser_text_words >= MIN_FALLBACK_TEXT_WORDS or len(browser_text) >= MIN_FALLBACK_TEXT_CHARS:
        status = "READY"
        reason = "BROWSER_TEXT"
    elif caption_words >= MIN_FALLBACK_TEXT_WORDS or len(caption) >= MIN_FALLBACK_TEXT_CHARS:
        status = "READY"
        reason = "METADATA_TEXT"
    else:
        if visual_status == "DEFERRED":
            status = "DEFERRED_EXTRACTION"
            reason = str(
                item.get("visual_description_deferred_reason")
                or "VISUAL_EXTRACTION_DEFERRED"
            )
        elif visual_status == "ERROR":
            status = "EXTRACTION_ERROR"
            reason = "VISUAL_EXTRACTION_ERROR"
        elif visual_status == "INSUFFICIENT_CONTENT":
            status = "INSUFFICIENT_CONTENT"
            reason = "NO_MEANINGFUL_VISUAL_EVIDENCE"
        elif has_visual_evidence:
            status = "PENDING_EXTRACTION"
            reason = "VISUAL_EXTRACTION_PENDING"
        else:
            status = "INSUFFICIENT_CONTENT"
            reason = "NO_ANALYZABLE_TEXT_OR_VISUAL_EVIDENCE"

    return {
        "status": status,
        "reason": reason,
        "transcript_word_count": transcript_words,
        "caption_word_count": caption_words,
        "browser_text_word_count": browser_text_words,
        "visible_text_word_count": visible_text_words,
        "visual_description_word_count": visual_description_words,
        "has_visual_evidence": has_visual_evidence,
    }


def build_packet(
    root: Path,
    shortcode: str,
    item: dict,
    transcript_path: Path | None,
    *,
    evidence_lineage_id: str,
    duplicate_of: str | None,
    duplicate_basis: str | None,
    transcript_text: str | None = None,
) -> dict:
    transcript = (
        transcript_text
        if transcript_text is not None
        else (read_text(transcript_path) if transcript_path is not None else "")
    )
    browser_text = str(item.get("browser_text") or "").strip()
    caption = str(item.get("caption") or "").strip()
    visible_text = str(item.get("visible_text") or "").strip()
    visual_status = str(item.get("visual_description_status") or "").upper()
    raw_visual_description = str(item.get("visual_description") or "").strip()
    visual_description = raw_visual_description if visual_status == "DONE" else ""
    visual_bundle = item.get("agent_visual_bundle") if isinstance(item.get("agent_visual_bundle"), dict) else {}
    visual_review_recommended = bool(
        item.get("visual_review_recommended")
        or visual_bundle.get("visual_review_recommended")
    )
    analysis_mode_recommended = str(
        item.get("analysis_mode_recommended")
        or visual_bundle.get("analysis_mode_recommended")
        or ("TRANSCRIPT_PLUS_VISUAL_REVIEW" if visual_review_recommended else "TRANSCRIPT_ONLY")
    )
    visual_review_reason = list(
        item.get("visual_review_reason")
        or visual_bundle.get("visual_review_reason")
        or []
    )
    evidence_parts = [
        x
        for x in (transcript, visual_description, visible_text, browser_text, caption)
        if x
    ]
    analysis_evidence_text = "\n\n".join(evidence_parts)

    return {
        "schema_version": 2,
        "screen_version": SCREEN_VERSION,
        "queue_id": shortcode,
        "shortcode": shortcode,
        "creator": item.get("creator"),
        "source_platform": item.get("source_platform"),
        "source_subtype": item.get("source_subtype"),
        "source_id": item.get("source_id"),
        "source_url": item.get("url"),
        "published_at": item.get("published_at"),
        "published_at_basis": item.get("published_at_basis"),
        "observed_at": item.get("observed_at"),
        "downloaded_at": item.get("downloaded_at"),
        "transcribed_at": item.get("transcribed_at"),
        "source_class": "INFLUENCER_DISCOVERY_SECONDARY",
        "evaluation_mode": item.get("evaluation_mode"),
        "evaluation_run_id": item.get("evaluation_run_id"),
        "evaluation_source_profile": item.get("evaluation_source_profile"),
        "evaluation_sample_size": item.get("evaluation_sample_size"),
        "permanent_source": item.get("permanent_source"),
        "creator_verification": item.get("creator_verification"),
        "analysis_owner": ANALYSIS_OWNER,
        "analysis_status": "PENDING_ANALYSIS",
        "analysis_content_status": item.get("analysis_content_status"),
        "analysis_content_reason": item.get("analysis_content_reason"),
        "analysis_mode_recommended": analysis_mode_recommended,
        "visual_review_recommended": visual_review_recommended,
        "visual_review_reason": visual_review_reason,
        "agent_visual_bundle": visual_bundle or None,
        "evidence_lineage_id": evidence_lineage_id,
        "duplicate_of": duplicate_of,
        "duplicate_basis": duplicate_basis,
        "caption": caption,
        "transcript_file": (
            str(transcript_path.relative_to(root)) if transcript_path is not None else None
        ),
        "transcript_text": transcript,
        "transcript_source": item.get("transcript_source"),
        "browser_text": browser_text,
        "visible_text": visible_text,
        "visual_description": visual_description,
        "visual_description_status": item.get("visual_description_status"),
        "visual_description_source": item.get("visual_description_source"),
        "visual_description_provider": item.get("visual_description_provider"),
        "visual_description_model": item.get("visual_description_model"),
        "visual_description_contract": item.get("visual_description_contract"),
        "visual_text_status": item.get("visual_text_status"),
        "visual_text_source": item.get("visual_text_source"),
        "visual_text_provider": item.get("visual_text_provider"),
        "visual_text_model": item.get("visual_text_model"),
        "analysis_evidence_text": analysis_evidence_text,
        "word_count": len(transcript.split()),
        "visual_evidence_status": item.get("visual_evidence_status"),
        "visual_evidence_index": item.get("visual_evidence_index"),
        "visual_frame_count": item.get("visual_frame_count"),
        "visual_capture_strategy": item.get("visual_capture_strategy"),
        "screenshot_file": item.get("screenshot_file"),
        "full_video_persisted": item.get("full_video_persisted"),
        "media_retention": item.get("media_retention"),
        "discovery_tags": keyword_tags(
            caption + "\n" + transcript + "\n" + visual_description + "\n" + visible_text
        ),
        # Kept as "ai_instruction" for backward compatibility with existing consumers.
        "ai_instruction": {
            "owner_project": ANALYSIS_OWNER,
            "objective": (
                "Treat the influencer as a discovery source, not authority. "
                "Extract the actual claim/idea and decide whether it deserves "
                "IGNORE, RESEARCH, TEST_CANDIDATE, or BACKLOG_CANDIDATE. "
                "Do not create a trade from the video alone. "
                "TEST/BACKLOG are only candidates until Avanza MCP performs "
                "technical peer review through a separate HANDOFF-XXX."
            ),
            "required_decision": CURRENT_DECISIONS,
            "required_fields": [
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
            ],
            "idea_types": [
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
            ],
            "governance": [
                "Influencer content is discovery only.",
                "Material factual claims require primary/original-source verification by Ekonomi.",
                "News or narrative alone cannot create a trade.",
                "Ekonomi owns economic interpretation and classification.",
                "Avanza MCP owns ingestion, provenance, deterministic dedupe, schema/validation and technical overlap review.",
                "Cross-platform reposts are one evidence lineage, not independent confirmations.",
                "For YouTube items, use transcript plus retained timestamped visual evidence when available; visual frames are supporting evidence, not execution truth.",
                "If visual_review_recommended=true, inspect representative visual evidence before concluding; TRANSCRIPT alone is not sufficient for that item.",
                "visual_review_recommended is decided per video. Creator history may bias priority but must never prevent an unflagged creator's chart-heavy video from escalating to visual review.",
                "If semantic duplication is plausible but not deterministically provable, use duplicate_basis=POSSIBLE_SEMANTIC_DUPLICATE and let Ekonomi decide.",
                "A TEST_CANDIDATE or BACKLOG_CANDIDATE does not change system state; material implementation requires a new HANDOFF-XXX.",
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument(
        "--must-include-shortcode",
        action="append",
        default=[],
        help="Bounded creator-evaluation queue admission reservation.",
    )
    args = parser.parse_args()
    root = args.root.resolve()

    must_include: list[str] = []
    seen_must_include: set[str] = set()
    for raw in args.must_include_shortcode:
        shortcode = str(raw or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", shortcode):
            raise ValueError(f"BAD_MUST_INCLUDE_SHORTCODE:{shortcode[:128]}")
        if shortcode not in seen_must_include:
            seen_must_include.add(shortcode)
            must_include.append(shortcode)
    if len(must_include) > 20:
        raise ValueError(f"TOO_MANY_MUST_INCLUDE_SHORTCODES:{len(must_include)}>20")

    manifest_path = root / "state" / "manifest.json"
    queue_path = root / "state" / "research_queue.json"
    decisions_path = root / "state" / "research_decisions.json"
    config_path = root / "control" / "research_screening.json"

    manifest = load_json(manifest_path, {"schema_version": 1, "items": {}})
    config = load_json(config_path, {})
    decisions = load_json(
        decisions_path,
        {"schema_version": 2, "screen_version": SCREEN_VERSION, "items": {}},
    )

    max_items = int(config.get("max_queue_items", 100))
    creators_allow = {
        str(x).strip().lstrip("@").lower()
        for x in config.get("creators", [])
        if str(x).strip()
    }

    # Collect only items with enough extracted evidence for actual analysis.
    # Thin/empty transcripts remain ingested evidence, but fail closed instead of
    # appearing as analysis-ready queue items.
    records: list[dict] = []
    insufficient_content_items: list[dict] = []
    deferred_extraction_items: list[dict] = []
    extraction_error_items: list[dict] = []
    pending_extraction_items: list[dict] = []
    skipped_missing_transcript = 0
    manifest_changed = False
    for shortcode, item in manifest.get("items", {}).items():
        if str(item.get("research_status") or "").upper() == "INVALID":
            continue

        # Older Story captures used a whole-screenshot SHA when Instagram stayed
        # on /stories/<creator>/ without exposing a numeric Story id. Timer/progress
        # UI made that identity change on every scan. Retire those legacy artifacts
        # rather than keeping volatile duplicates in the analysis queue.
        is_legacy_unstable_story = (
            str(item.get("source_platform") or "").upper() == "INSTAGRAM"
            and str(item.get("source_subtype") or "").upper() == "STORY"
            and str(item.get("source_id") or "").startswith("story:frame-")
            and not item.get("story_identity_basis")
        )
        if is_legacy_unstable_story:
            try:
                source_path = urlsplit(str(item.get("url") or "")).path
            except Exception:
                source_path = ""
            if re.fullmatch(r"/stories/[A-Za-z0-9._-]{1,64}/?", source_path):
                item["research_status"] = "INVALID"
                item["invalid_reason"] = "LEGACY_UNSTABLE_ROOT_STORY_IDENTITY"
                item["invalidated_at"] = utc_now()
                manifest_changed = True
                continue

        if item.get("download_status") != "DONE":
            continue
        is_visual_story = (
            str(item.get("source_platform") or "").upper() == "INSTAGRAM"
            and str(item.get("source_subtype") or "").upper() == "STORY"
            and bool(item.get("screenshot_file"))
        )
        if item.get("transcription_status") != "DONE" and not is_visual_story:
            continue
        creator = str(item.get("creator") or "").lower()
        is_creator_evaluation = (
            item.get("evaluation_mode") == "CREATOR_EVALUATION"
            and item.get("permanent_source") is False
        )
        if creators_allow and creator not in creators_allow and not is_creator_evaluation:
            continue

        transcript_path = normalize_manifest_path(root, item.get("transcript_txt"))
        transcript_missing = False
        if transcript_path is None or not transcript_path.exists():
            if is_visual_story:
                # Browser chrome/text is a separate weak fallback source. Do not
                # mislabel it as a transcript or let the lower transcript threshold
                # turn username/timestamp/music chrome into READY evidence.
                transcript_path = None
                transcript = ""
            else:
                skipped_missing_transcript += 1
                transcript_missing = True
                transcript_path = None
                transcript = ""
        else:
            transcript = read_text(transcript_path)

        readiness = assess_analysis_content(item, transcript)
        if transcript_missing and readiness["status"] == "INSUFFICIENT_CONTENT":
            readiness["reason"] = "TRANSCRIPT_FILE_MISSING"

        desired_content_meta = {
            "analysis_content_status": readiness["status"],
            "analysis_content_reason": readiness["reason"],
            "transcript_word_count": readiness["transcript_word_count"],
            "caption_word_count": readiness["caption_word_count"],
            "browser_text_word_count": readiness["browser_text_word_count"],
            "visible_text_word_count": readiness["visible_text_word_count"],
            "visual_description_word_count": readiness["visual_description_word_count"],
            "has_visual_evidence": readiness["has_visual_evidence"],
        }
        for key, value in desired_content_meta.items():
            if item.get(key) != value:
                item[key] = value
                manifest_changed = True

        if readiness["status"] != "READY":
            existing_decision = decisions.get("items", {}).get(shortcode, {})
            if existing_decision.get("decision") not in FINAL_DECISIONS:
                desired_research_status = {
                    "INSUFFICIENT_CONTENT": "INSUFFICIENT_CONTENT",
                    "DEFERRED_EXTRACTION": "PENDING_EXTRACTION",
                    "PENDING_EXTRACTION": "PENDING_EXTRACTION",
                    "EXTRACTION_ERROR": "EXTRACTION_ERROR",
                }.get(readiness["status"], "PENDING_EXTRACTION")
                if item.get("research_status") != desired_research_status:
                    item["research_status"] = desired_research_status
                    manifest_changed = True
                if item.get("analysis_owner") != ANALYSIS_OWNER:
                    item["analysis_owner"] = ANALYSIS_OWNER
                    manifest_changed = True

            nonready = {
                "queue_id": shortcode,
                "creator": item.get("creator"),
                "source_platform": item.get("source_platform"),
                "source_id": item.get("source_id"),
                "source_url": item.get("url"),
                "published_at": item.get("published_at"),
                "visual_description_error": item.get("visual_description_error"),
                "visual_description_retry_after": item.get("visual_description_retry_after"),
                **readiness,
            }
            if readiness["status"] == "INSUFFICIENT_CONTENT":
                insufficient_content_items.append(nonready)
            elif readiness["status"] == "DEFERRED_EXTRACTION":
                deferred_extraction_items.append(nonready)
            elif readiness["status"] == "EXTRACTION_ERROR":
                extraction_error_items.append(nonready)
            else:
                pending_extraction_items.append(nonready)
            continue

        fp = transcript_fingerprint(transcript)
        url_key = canonicalize_url(item.get("url"))
        records.append({
            "shortcode": shortcode,
            "item": item,
            "transcript_path": transcript_path,
            "transcript_text": transcript,
            "transcript_fp": fp,
            "url_key": url_key,
            "lineage_id": lineage_id(
                transcript_fp=fp,
                source_url=url_key,
                shortcode=shortcode,
            ),
        })

    # Exact/near-verbatim deterministic dedupe only. Same normalized transcript
    # receives the same lineage ID. Same exact canonical source URL is also grouped.
    groups: dict[str, list[dict]] = {}
    for rec in records:
        if rec["transcript_fp"]:
            key = f"transcript:{rec['transcript_fp']}"
        elif rec["url_key"]:
            key = f"url:{rec['url_key']}"
        else:
            key = f"id:{rec['shortcode']}"
        groups.setdefault(key, []).append(rec)

    duplicate_meta: dict[str, tuple[str, str | None, str | None]] = {}
    for group in groups.values():
        def canonical_sort(rec: dict) -> tuple:
            d = decisions.get("items", {}).get(rec["shortcode"], {})
            finalized = d.get("decision") in FINAL_DECISIONS
            return (
                0 if finalized else 1,
                str(rec["item"].get("published_at") or "9999"),
                str(rec["shortcode"]),
            )

        ordered = sorted(group, key=canonical_sort)
        canonical = ordered[0]
        group_lineage = canonical["lineage_id"]
        for idx, rec in enumerate(ordered):
            if idx == 0:
                duplicate_meta[rec["shortcode"]] = (group_lineage, None, None)
            else:
                basis = (
                    "TRANSCRIPT_MATCH"
                    if rec["transcript_fp"] and rec["transcript_fp"] == canonical["transcript_fp"]
                    else "EXACT_SOURCE_URL"
                )
                duplicate_meta[rec["shortcode"]] = (
                    group_lineage,
                    canonical["shortcode"],
                    basis,
                )

    items = []
    skipped_finalized = 0
    skipped_duplicates = 0

    for rec in records:
        shortcode = rec["shortcode"]
        item = rec["item"]
        existing_decision = decisions.get("items", {}).get(shortcode, {})

        lineage, duplicate_of, duplicate_basis = duplicate_meta[shortcode]

        # Preserve explicit human/Ekonomi duplicate metadata if it already exists.
        lineage = existing_decision.get("evidence_lineage_id") or item.get("evidence_lineage_id") or lineage
        duplicate_of = existing_decision.get("duplicate_of") or item.get("duplicate_of") or duplicate_of
        duplicate_basis = existing_decision.get("duplicate_basis") or item.get("duplicate_basis") or duplicate_basis

        desired_meta = {
            "evidence_lineage_id": lineage,
            "duplicate_of": duplicate_of,
            "duplicate_basis": duplicate_basis,
        }
        for key, value in desired_meta.items():
            if item.get(key) != value:
                item[key] = value
                manifest_changed = True

        if existing_decision.get("decision") in FINAL_DECISIONS:
            skipped_finalized += 1
            continue

        if duplicate_of:
            if item.get("research_status") != "DUPLICATE":
                item["research_status"] = "DUPLICATE"
                item["analysis_owner"] = ANALYSIS_OWNER
                manifest_changed = True
            skipped_duplicates += 1
            continue

        if item.get("research_status") != "PENDING_ANALYSIS" or item.get("analysis_owner") != ANALYSIS_OWNER:
            item["research_status"] = "PENDING_ANALYSIS"
            item["analysis_owner"] = ANALYSIS_OWNER
            manifest_changed = True

        packet = build_packet(
            root,
            shortcode,
            item,
            rec["transcript_path"],
            evidence_lineage_id=lineage,
            duplicate_of=None,
            duplicate_basis=None,
            transcript_text=rec["transcript_text"],
        )
        items.append(packet)

    items.sort(
        key=lambda x: (
            str(x.get("published_at") or ""),
            str(x.get("shortcode") or ""),
        ),
        reverse=True,
    )

    eligible_by_shortcode = {str(x.get("shortcode") or ""): x for x in items}
    must_include_eligible = [
        shortcode for shortcode in must_include if shortcode in eligible_by_shortcode
    ]
    if len(must_include_eligible) > max_items:
        raise ValueError(
            f"MUST_INCLUDE_EXCEEDS_QUEUE_CAP:{len(must_include_eligible)}>{max_items}"
        )

    if must_include_eligible:
        reserved = [eligible_by_shortcode[shortcode] for shortcode in must_include_eligible]
        reserved_set = set(must_include_eligible)
        ordinary = [
            item for item in items
            if str(item.get("shortcode") or "") not in reserved_set
        ]
        items = reserved + ordinary[: max_items - len(reserved)]
        items.sort(
            key=lambda x: (
                str(x.get("published_at") or ""),
                str(x.get("shortcode") or ""),
            ),
            reverse=True,
        )
    else:
        items = items[:max_items]

    queued_shortcodes = {str(x.get("shortcode") or "") for x in items}
    must_include_queued = [
        shortcode for shortcode in must_include if shortcode in queued_shortcodes
    ]
    must_include_missing = [
        shortcode for shortcode in must_include if shortcode not in queued_shortcodes
    ]

    queue = {
        "schema_version": 2,
        "screen_version": SCREEN_VERSION,
        "generated_at": utc_now(),
        "analysis_owner": ANALYSIS_OWNER,
        "status": "PENDING_ANALYSIS" if items else "EMPTY",
        "count": len(items),
        "skipped_finalized": skipped_finalized,
        "skipped_duplicates": skipped_duplicates,
        "skipped_missing_transcript": skipped_missing_transcript,
        "insufficient_content_count": len(insufficient_content_items),
        "insufficient_content_items": insufficient_content_items,
        "deferred_extraction_count": len(deferred_extraction_items),
        "deferred_extraction_items": deferred_extraction_items,
        "extraction_error_count": len(extraction_error_items),
        "extraction_error_items": extraction_error_items,
        "pending_extraction_count": len(pending_extraction_items),
        "pending_extraction_items": pending_extraction_items,
        "must_include_requested": must_include,
        "must_include_eligible": must_include_eligible,
        "must_include_queued": must_include_queued,
        "must_include_missing": must_include_missing,
        "items": items,
    }
    atomic_write_json(queue_path, queue)
    if manifest_changed:
        atomic_write_json(manifest_path, manifest)

    # Compact per-item packets remain a transport convenience, not an analysis source of truth.
    # Avoid replacing identical packets on every queue refresh; bind-mounted small-file
    # writes are materially more expensive than a byte-for-byte read comparison.
    packet_writes = 0
    packet_write_skips = 0
    for packet in items:
        creator = str(packet.get("creator") or "unknown")
        out_dir = root / "output" / creator / "research" / "pending"
        out_path = out_dir / f"{packet['shortcode']}.json"
        if atomic_write_json_if_changed(out_path, packet):
            packet_writes += 1
        else:
            packet_write_skips += 1

    print(json.dumps({
        "screen_version": SCREEN_VERSION,
        "analysis_owner": ANALYSIS_OWNER,
        "queue_status": queue["status"],
        "queued": queue["count"],
        "skipped_finalized": skipped_finalized,
        "skipped_duplicates": skipped_duplicates,
        "skipped_missing_transcript": skipped_missing_transcript,
        "insufficient_content_count": len(insufficient_content_items),
        "deferred_extraction_count": len(deferred_extraction_items),
        "extraction_error_count": len(extraction_error_items),
        "pending_extraction_count": len(pending_extraction_items),
        "packet_writes": packet_writes,
        "packet_write_skips": packet_write_skips,
        "must_include_requested": must_include,
        "must_include_eligible": must_include_eligible,
        "must_include_queued": must_include_queued,
        "must_include_missing": must_include_missing,
        "queue_file": str(queue_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
