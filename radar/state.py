"""State lives in small JSON files committed back to the repo by CI.

jobs.json      {job_id: record}                — everything ever seen, with score
companies.json {ats:token: registry entry}     — the self-expanding company registry
feedback.json  {company_boosts, token_boosts, explicit_*, negative, taste_events}
              — explicit feedback; the implicit positive sample is applied.json
applied.json   [{id, company, title, url, applied_at, notion_synced, ...}]
score_preferences.json {enabled_dimensions, version, updated_at}
              — owner-selected optional score sections
runs.json      [{ts, new_jobs, alerts, sources: {...}}]  — last 200 run summaries
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import STATE_DIR, profile_id


def _prefix(namespace: str | None = None) -> str:
    """Keep the legacy new-grad filenames, prefixing only new lanes."""
    mode = namespace or profile_id()
    return "intern_" if mode == "internship" else ""


def _path(name: str, namespace: str | None = None, *, shared: bool = False) -> Path:
    return STATE_DIR / (name if shared else f"{_prefix(namespace)}{name}")


def load(name: str, default, namespace: str | None = None):
    p = _path(name, namespace)
    if not p.exists():
        return default
    with open(p) as f:
        return json.load(f)


def save(name: str, obj, namespace: str | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(name, namespace)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    tmp.replace(p)


def load_shared(name: str, default):
    p = _path(name, shared=True)
    if not p.exists():
        return default
    with open(p) as f:
        return json.load(f)


def save_shared(name: str, obj) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(name, shared=True)
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
    return load("feedback.json", {"company_boosts": {}, "token_boosts": {},
                                    "explicit_company_boosts": {},
                                    "explicit_token_boosts": {},
                                    "negative_companies": [], "taste_events": []})


def score_preferences() -> dict:
    return load("score_preferences.json", {
        "version": 1,
        "enabled_dimensions": {
            "base": True, "role_fit": True, "eligibility": True,
            "mission": True, "company_quality": True, "compensation": True,
            "personal_signal": True, "timing_access": True,
        },
    })


def applied() -> list:
    return load("applied.json", [])


def shortlist() -> list:
    """Jobs the user checked as 'save for later' — not confirmed applications.
    Promoted to applied.json by email_watch.py when a confirmation email is
    matched, or manually via the `applied <url>` comment command."""
    return load("shortlist.json", [])
