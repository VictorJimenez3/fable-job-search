"""User-controlled review state for private resume evidence.

The evidence graph is derived from local CV sources and public corroboration.
This module adds a small, reversible review layer so a user can confirm,
reject, or supersede individual claims without editing generated graph data.
Review state lives inside the ignored private CV workspace.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict


REVIEW_VERSION = "evidence-review-v3"
REVIEW_FILENAME = "evidence_review.json"
REVIEW_STATUSES = {
    "unreviewed", "confirmed", "public_safe", "disputed", "rejected",
    "superseded", "private_do_not_publish",
}
BLOCKING_STATUSES = {"rejected", "superseded", "private_do_not_publish"}
QUESTION_RESPONSES = {"used", "not_used", "unsure"}


def review_path(studio_dir: Path) -> Path:
    return studio_dir / REVIEW_FILENAME


def _write_reviews(studio_dir: Path, payload: Dict[str, Any]) -> None:
    studio_dir.mkdir(parents=True, exist_ok=True)
    target = review_path(studio_dir)
    temporary = target.with_name(".%s.%s.tmp" % (target.name, hashlib.sha256(
        dt.datetime.now(dt.timezone.utc).isoformat().encode()
    ).hexdigest()[:10]))
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(target)


def load_reviews(studio_dir: Path) -> Dict[str, Any]:
    """Load review overrides, tolerating an absent or malformed private file."""
    try:
        payload = json.loads(review_path(studio_dir).read_text())
    except (OSError, ValueError, TypeError):
        return {"version": REVIEW_VERSION, "claims": {}, "questions": {}}
    if not isinstance(payload, dict):
        return {"version": REVIEW_VERSION, "claims": {}, "questions": {}}
    claims = payload.get("claims")
    if not isinstance(claims, dict):
        claims = {}
    questions = payload.get("questions")
    if not isinstance(questions, dict):
        questions = {}
    return {
        # Review state is intentionally forward-compatible: old v1 files
        # still load, but every write/read surface reports the current schema.
        "version": REVIEW_VERSION,
        "updated_at": payload.get("updated_at"),
        "claims": claims,
        "questions": questions,
    }


def question_id(term: str) -> str:
    """Return one stable key per capability so postings do not create duplicates."""
    normalized = re.sub(r"[^a-z0-9+#.]+", "-", str(term or "").strip().lower()).strip("-")
    digest = hashlib.sha256(str(term or "").strip().lower().encode()).hexdigest()[:8]
    return "capability:%s:%s" % ((normalized or "unknown")[:48], digest)


def upsert_questions(studio_dir: Path, gaps: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Record posting-raised capability questions without repeating a topic."""
    payload = load_reviews(studio_dir)
    questions = payload.setdefault("questions", {})
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    changed = False
    for gap in gaps:
        term = str(gap.get("term") or "").strip().lower()
        if not term:
            continue
        key = question_id(term)
        existing = questions.get(key) if isinstance(questions.get(key), dict) else {}
        triggers = existing.get("triggers") if isinstance(existing.get("triggers"), list) else []
        trigger = {
            field: gap.get(field)
            for field in ("job_id", "company", "title", "url", "importance")
            if gap.get(field) not in (None, "")
        }
        trigger_key = (str(trigger.get("job_id") or ""), str(trigger.get("url") or ""))
        if trigger and not any(
            (str(item.get("job_id") or ""), str(item.get("url") or "")) == trigger_key
            for item in triggers if isinstance(item, dict)
        ):
            triggers.append(trigger)
            changed = True
        questions[key] = {
            **existing,
            "id": key,
            "term": term,
            "question": (
                "Where and when did you use %s? Include the role or project, what you did, "
                "and any result. If you have not used it, mark that and I will stop asking."
            ) % term,
            "status": str(existing.get("status") or "open"),
            "first_seen_at": existing.get("first_seen_at") or now,
            "last_seen_at": now,
            "triggers": triggers[-12:],
        }
    if changed or gaps:
        payload["updated_at"] = now
        _write_reviews(studio_dir, payload)
    return payload


def answer_question(
    studio_dir: Path,
    item_id: str,
    response: str,
    answer: str = "",
    where_when: str = "",
) -> Dict[str, Any]:
    """Save Victor's durable answer; only a concrete `used` answer authorizes claims."""
    payload = load_reviews(studio_dir)
    questions = payload.setdefault("questions", {})
    item = questions.get(str(item_id or ""))
    if not isinstance(item, dict):
        raise ValueError("context question not found")
    response = str(response or "").strip().lower()
    if response not in QUESTION_RESPONSES:
        raise ValueError("response must be used, not_used, or unsure")
    answer = str(answer or "").strip()
    where_when = str(where_when or "").strip()
    if len(answer) > 4000 or len(where_when) > 1000:
        raise ValueError("context answer is too long")
    if response == "used" and (len(answer) < 20 or len(where_when) < 3):
        raise ValueError("used answers need what you did plus where and when")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    item.update({
        "response": response,
        "answer": answer,
        "where_when": where_when,
        "status": "answered" if response in {"used", "not_used"} else "open",
        "answered_by": "Victor",
        "answered_at": now,
    })
    payload["updated_at"] = now
    _write_reviews(studio_dir, payload)
    return payload


def owner_answer_nodes(reviews: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Project confirmed owner Q&A into the evidence graph with explicit provenance."""
    nodes = []
    for item in (reviews.get("questions") or {}).values():
        if not isinstance(item, dict) or item.get("response") != "used":
            continue
        answer = str(item.get("answer") or "").strip()
        where_when = str(item.get("where_when") or "").strip()
        if not answer or not where_when:
            continue
        text = "%s — %s" % (where_when, answer)
        tokens = sorted(set(re.findall(r"[a-z0-9+#.]+", (str(item.get("term") or "") + " " + text).lower())))
        nodes.append({
            "id": "owner-answer:" + str(item.get("id") or question_id(str(item.get("term") or ""))),
            "source": "Victor Q&A",
            "heading": str(item.get("term") or "Owner-confirmed experience"),
            "text": text[:1800],
            "authority": 100,
            "claim_allowed": True,
            "source_kind": "owner-confirmed answer",
            "review_status": "confirmed",
            "reviewed_by": "Victor",
            "reviewed_at": item.get("answered_at"),
            "tokens": tokens,
        })
    return nodes


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
