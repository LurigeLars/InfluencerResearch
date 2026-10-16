"""Operator-controlled, fail-closed Instagram network pause for IR runtime.

No cookies, tokens, or profile data are inspected by this policy.
"""
from __future__ import annotations

import os

PAUSE_ENV = "INFLUENCER_RESEARCH_INSTAGRAM_PAUSED"
PAUSE_REASON = "INSTAGRAM_PAUSED_BY_OPERATOR"


def instagram_paused() -> bool:
    raw = os.environ.get(PAUSE_ENV)
    if raw is None:
        # Container runtime fails closed even if the Compose setting is omitted.
        return os.environ.get("INFLUENCER_RESEARCH_CONTAINER", "") == "1"
    return raw.strip().casefold() not in {"0", "false", "off", "no"}


def require_instagram_access() -> None:
    if instagram_paused():
        raise RuntimeError(PAUSE_REASON)


def instagram_source_allowed(source: dict) -> bool:
    return not (
        instagram_paused()
        and str(source.get("platform") or "").upper() == "INSTAGRAM"
    )
