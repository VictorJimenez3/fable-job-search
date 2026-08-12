"""User-controlled review state for private resume evidence.

The evidence graph is derived from local CV sources and public corroboration.
This module adds a small, reversible review layer so a user can confirm,
reject, or supersede individual claims without editing generated graph data.
Review state lives inside the ignored private CV workspace.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


REVIEW_VERSION = "evidence-review-v2"
REVIEW_FILENAME = "evidence_review.json"
REVIEW_STATUSES = {
    "unreviewed", "confirmed", "public_safe", "disputed", "rejected",
    "superseded", "private_do_not_publish",
}
BLOCKING_STATUSES = {"rejected", "superseded", "private_do_not_publish"}


def review_path(studio_dir: Path) -> Path:
    return studio_dir / REVIEW_FILENAME


def load_reviews(studio_dir: Path) -> Dict[str, Any]:
    """Load review overrides, tolerating an absent or malformed private file."""
    try:
        payload = json.loads(review_path(studio_dir).read_text())
    except (OSError, ValueError, TypeError):
        return {"version": REVIEW_VERSION, "claims": {}}
    if not isinstance(payload, dict):
        return {"version": REVIEW_VERSION, "claims": {}}
    claims = payload.get("claims")
    if not isinstance(claims, dict):
        claims = {}
    return {
        # Review state is intentionally forward-compatible: old v1 files
        # still load, but every write/read surface reports the current schema.
        "version": REVIEW_VERSION,
        "updated_at": payload.get("updated_at"),
        "claims": claims,
    }


def _normalized_review(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    status = str(value.get("status") or "unreviewed").lower().strip()
    if status not in REVIEW_STATUSES:
        status = "unreviewed"
    review = {
        "status": status,
        "note": str(value.get("note") or "").strip(),
        "reviewed_by": str(value.get("reviewed_by") or "").strip(),
        "reviewed_at": value.get("reviewed_at"),
    }
    if value.get("claim_allowed") is True:
        review["claim_allowed"] = True
    return review


def apply_reviews(graph: Dict[str, Any], reviews: Dict[str, Any]) -> Dict[str, Any]:
    """Annotate graph nodes and enforce rejected/superseded claims.

    Public sources remain corroboration by default. A user may explicitly
    promote one with ``claim_allowed: true`` after confirming it.
    """
    claims = reviews.get("claims") if isinstance(reviews, dict) else {}
    if not isinstance(claims, dict):
        claims = {}
    for node in graph.get("nodes", []):
        review = _normalized_review(claims.get(str(node.get("id") or "")))
        status = review.get("status", "unreviewed")
        node["review_status"] = status
        if review.get("note"):
            node["review_note"] = review["note"]
        if review.get("reviewed_by"):
            node["reviewed_by"] = review["reviewed_by"]
        if review.get("reviewed_at"):
            node["reviewed_at"] = review["reviewed_at"]
        if status in BLOCKING_STATUSES:
            node["claim_allowed"] = False
            node["blocked_reason"] = review.get("note") or status
        elif status == "disputed":
            node["claim_allowed"] = False
            node["blocked_reason"] = review.get("note") or status
        elif status in {"confirmed", "public_safe"} and review.get("claim_allowed") is True:
            node["claim_allowed"] = True
            node["source_kind"] = "user-confirmed corroboration"
    return graph


def review_summary(graph: Dict[str, Any]) -> Dict[str, Any]:
    counts = {status: 0 for status in REVIEW_STATUSES}
    blocked = 0
    usable = 0
    default_blocked = 0
    for node in graph.get("nodes", []):
        status = str(node.get("review_status") or "unreviewed")
        counts[status] = counts.get(status, 0) + 1
        if status in BLOCKING_STATUSES:
            blocked += 1
        elif node.get("claim_allowed") is True:
            usable += 1
        else:
            default_blocked += 1
    return {
        "version": REVIEW_VERSION,
        "counts": counts,
        "blocked_claims": blocked,
        "usable_claims": usable,
        "default_blocked": default_blocked,
        "nodes": len(graph.get("nodes", [])),
    }
