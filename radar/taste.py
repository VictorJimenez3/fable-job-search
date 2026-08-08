"""Small, auditable preference updates from the owner-facing preference surface.

Saved/applied roles are the implicit positive sample rebuilt by ``score.py``.
This module adds explicit, fixed-category feedback without accepting arbitrary
scoring instructions from the browser. The structured state remains
``state/feedback.json``; ``docs/FEEDBACK.md`` is a human-readable generated
audit for the repository owner.
"""
from __future__ import annotations

import time
from pathlib import Path

from .config import DOCS_DIR
from .models import norm
from .score import FEEDBACK_STOPWORDS, _title_tokens


REASONS = {
    "up": {"company", "role", "both"},
    "down": {"company", "role", "eligibility", "location", "other"},
}
REASON_LABELS = {
    "company": "I like this company",
    "role": "I like this kind of role",
    "both": "I like the company and role",
    "eligibility": "Not eligible / too senior",
    "location": "Location or work authorization mismatch",
    "other": "Not for me",
}


def _token_delta(feedback: dict, title: str, delta: int) -> list[str]:
    boosts = feedback.setdefault("token_boosts", {})
    changed = []
    for token in sorted(_title_tokens(title)):
        old = int(boosts.get(token, 0) or 0)
        new = max(-4, min(4, old + delta))
        if new != old:
            boosts[token] = new
            changed.append(token)
    return changed


def _explicit_token_delta(feedback: dict, title: str, delta: int) -> list[str]:
    """Mirror title feedback into the post-sample explicit signal map."""
    boosts = feedback.setdefault("explicit_token_boosts", {})
    changed = []
    for token in sorted(_title_tokens(title)):
        old = int(boosts.get(token, 0) or 0)
        new = max(-4, min(4, old + delta))
        if new != old:
            boosts[token] = new
            changed.append(token)
    return changed


def record_feedback(feedback: dict, job: dict, vote: str, reason: str) -> bool:
    """Apply one owner feedback event; duplicate submissions are idempotent."""
    vote = str(vote or "").lower().strip()
    reason = str(reason or "").lower().strip()
    if vote not in REASONS or reason not in REASONS[vote]:
        return False
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        return False
    events = feedback.setdefault("taste_events", [])
    if any(e.get("job_id") == job_id and e.get("vote") == vote
           and e.get("reason") == reason for e in events):
        return False

    company = norm(job.get("company", ""))
    effects: list[str] = []
    if vote == "up":
        if reason in {"company", "both"} and company:
            companies = feedback.setdefault("company_boosts", {})
            companies[company] = min(int(companies.get(company, 0) or 0) + 2, 8)
            explicit = feedback.setdefault("explicit_company_boosts", {})
            explicit[company] = min(int(explicit.get(company, 0) or 0) + 2, 8)
            effects.append(f"company +2 ({company})")
        if reason in {"role", "both"}:
            tokens = _token_delta(feedback, job.get("title", ""), 1)
            _explicit_token_delta(feedback, job.get("title", ""), 1)
            if tokens:
                effects.append("title tokens +1 (" + ", ".join(tokens) + ")")
    elif reason == "company" and company:
        negative = feedback.setdefault("negative_companies", [])
        if company not in negative:
            negative.append(company)
            effects.append(f"company -10 ({company})")
    elif reason == "role":
        tokens = _token_delta(feedback, job.get("title", ""), -1)
        _explicit_token_delta(feedback, job.get("title", ""), -1)
        if tokens:
            effects.append("title tokens -1 (" + ", ".join(tokens) + ")")

    events.append({
        "job_id": job_id,
        "company": str(job.get("company", ""))[:200],
        "title": str(job.get("title", ""))[:240],
        "vote": vote,
        "reason": reason,
        "effects": effects,
        "created_at": int(time.time()),
    })
    feedback["taste_events"] = events[-250:]
    return True


def _md(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def render_report(feedback: dict) -> str:
    """Render the public, owner-write-only feedback audit."""
    companies = sorted(
        ((str(k), int(v or 0)) for k, v in feedback.get("company_boosts", {}).items()),
        key=lambda item: (-item[1], item[0]),
    )
    tokens = sorted(
        ((str(k), int(v or 0)) for k, v in feedback.get("token_boosts", {}).items()
         if str(k) not in FEEDBACK_STOPWORDS and int(v or 0)),
        key=lambda item: (-item[1], item[0]),
    )
    negative = sorted(set(str(c) for c in feedback.get("negative_companies", [])))
    events = list(feedback.get("taste_events", []))[-25:][::-1]
    lines = [
        "# Ranking taste feedback",
        "",
        "> Owner-only write path: only the repository owner’s GitHub identity can submit feedback.",
        "> This public repository can still be read by anyone; do not put secrets or private notes here.",
        "",
        "## How ranking changes",
        "",
        "- Saved roles are the implicit positive sample. They add small, capped company/title signals.",
        "- Explicit **more like this** feedback adds a capped company and/or title signal.",
        "- Explicit **less like this** feedback only downranks a company or title when that reason is selected.",
        "- Eligibility and location feedback is recorded for review but never overrides deterministic gates.",
        "- Every change remains visible in each job’s score-reason ledger after the next rescore.",
        "",
        "## Current learned signals",
        "",
        "### Companies",
        "",
        "| company | boost |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {_md(name)} | {value:+d} |" for name, value in companies[:30])
    if not companies:
        lines.append("| none yet | 0 |")
    lines.extend(["", "### Title tokens", "", "| token | signal |", "| --- | ---: |"])
    lines.extend(f"| {_md(name)} | {value:+d} |" for name, value in tokens[:40])
    if not tokens:
        lines.append("| none yet | 0 |")
    lines.extend(["", "### Downranked companies", ""])
    lines.append(", ".join(f"`{_md(name)}`" for name in negative) or "None.")
    lines.extend(["", "## Recent explicit feedback", ""])
    if events:
        lines.extend([
            "| vote | role | reason | effect |",
            "| --- | --- | --- | --- |",
        ])
        for event in events:
            vote = "more like this" if event.get("vote") == "up" else "less like this"
            reason = REASON_LABELS.get(event.get("reason", ""), event.get("reason", ""))
            effect = "; ".join(event.get("effects") or []) or "logged only"
            lines.append(
                f"| {vote} | {_md(event.get('company'))} — {_md(event.get('title'))} | "
                f"{_md(reason)} | {_md(effect)} |"
            )
    else:
        lines.append("No explicit feedback yet. The saved-role sample is still active.")
    lines.append("")
    return "\n".join(lines)


def write_report(feedback: dict, path: Path | None = None) -> Path:
    destination = path or (DOCS_DIR / "FEEDBACK.md")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_report(feedback))
    return destination
