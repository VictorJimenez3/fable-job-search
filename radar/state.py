"""State lives in small JSON files committed back to the repo by CI.

jobs.json      {job_id: record}                — everything ever seen, with score
companies.json {ats:token: registry entry}     — the self-expanding company registry
feedback.json  {company_boosts, token_boosts, negative} — learned taste
applied.json   [{id, company, title, url, applied_at, notion_synced, ...}]
runs.json      [{ts, new_jobs, alerts, sources: {...}}]  — last 200 run summaries
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import STATE_DIR


def _path(name: str) -> Path:
    return STATE_DIR / name


def load(name: str, default):
    p = _path(name)
    if not p.exists():
        return default
    with open(p) as f:
        return json.load(f)


def save(name: str, obj) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    tmp.replace(p)


def jobs() -> dict:
    return load("jobs.json", {})


def companies() -> dict:
    return load("companies.json", {})


def feedback() -> dict:
    return load("feedback.json", {"company_boosts": {}, "token_boosts": {}, "negative_companies": []})


def applied() -> list:
    return load("applied.json", [])


def shortlist() -> list:
    """Jobs the user checked as 'save for later' — not confirmed applications.
    Promoted to applied.json by email_watch.py when a confirmation email is
    matched, or manually via the `applied <url>` comment command."""
    return load("shortlist.json", [])
