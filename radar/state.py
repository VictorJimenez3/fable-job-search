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
import os
from pathlib import Path

from .config import STATE_DIR, profile_id

# GitHub rejects blobs larger than 100 MiB. Leave enough room for a crawl to
# report and recover before a generated commit reaches that hard boundary.
DEFAULT_MAX_JOB_SNAPSHOT_BYTES = 95 * 1024 * 1024

# These fields have the same defaults at every read boundary. Persisting them
# on tens of thousands of historical rows adds megabytes without preserving
# information. Keep this list deliberately narrow and compatibility-tested.
_JOB_DEFAULTS = {
    "description": "",
    "llm_note": "",
    "salary": "",
    "remote": False,
    "alert_ok": False,
    "explicit_new_grad": False,
    "early_career_possible": False,
    "ranking_adjustment": 0,
    "source_url": "",
    "posting_status": "open",
    "profile": "new_grad",
}
_JOB_EMPTY_FIELDS = {
    "alternate_urls",
    "source_url_variants",
    "source_board_variants",
    "locations",
    "link_resolution",
    "posting_identity",
    "posting_family_id",
}

# Startup-stage evidence is a projection of the company-level research cache,
# which is loaded separately by the frontend.  Keeping the same long cited
# claim and explanation on every historical posting caused rescoring to add
# several megabytes without adding new evidence.  Persist the small stage and
# score fields; the UI and issue-delivery surfaces hydrate the cited claim
# from company_research.json.
_JOB_DERIVED_FIELDS = {
    "startup_stage_evidence",
    "startup_stage_reason",
}


def _compact_sponsorship_history(value: object) -> object:
    """Keep per-job DOL context small; coverage is shared by sponsorship.json."""
    if not isinstance(value, dict):
        return value
    compact = dict(value)
    compact.pop("coverage_quarters", None)
    return compact


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


def _compact_job_record(record: object) -> object:
    """Return a sparse, lossless-on-read copy of one generated job record."""
    if not isinstance(record, dict):
        return record
    # Only fully scored crawler output is guaranteed to have every omitted
    # default reconstructible. Keep hand-authored/legacy rows byte-for-byte
    # compatible with callers that still inspect optional keys directly.
    if not record.get("score_version") or record.get("manual_added"):
        return record
    compact = dict(record)
    if compact.get("score_dimensions_raw") == compact.get("score_dimensions"):
        compact.pop("score_dimensions_raw", None)
    for key, default in _JOB_DEFAULTS.items():
        if compact.get(key) == default:
            compact.pop(key, None)
    for key in _JOB_EMPTY_FIELDS:
        if compact.get(key) in (None, "", [], {}):
            compact.pop(key, None)
    for key in _JOB_DERIVED_FIELDS:
        compact.pop(key, None)
    if "sponsorship_history" in compact:
        compact["sponsorship_history"] = _compact_sponsorship_history(
            compact["sponsorship_history"])
    return compact


def _prepared(name: str, obj: object) -> object:
    if name != "jobs.json" or not isinstance(obj, dict):
        return obj
    return {key: _compact_job_record(value) for key, value in obj.items()}


def _max_job_snapshot_bytes() -> int:
    value = os.getenv("RADAR_MAX_JOB_SNAPSHOT_BYTES", "").strip()
    if not value:
        return DEFAULT_MAX_JOB_SNAPSHOT_BYTES
    try:
        return max(1, int(value))
    except ValueError as exc:
        raise ValueError("RADAR_MAX_JOB_SNAPSHOT_BYTES must be an integer") from exc


def save(name: str, obj, namespace: str | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(name, namespace)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        # The jobs snapshot is a committed production artifact. Compact JSON
        # keeps a full score rebuild below GitHub's 100 MB blob limit while
        # remaining ordinary JSON for the web app and Mac companion.
        json.dump(_prepared(name, obj), f, sort_keys=True, ensure_ascii=False,
                  separators=(",", ":"))
        f.write("\n")
    if name == "jobs.json" and tmp.stat().st_size > _max_job_snapshot_bytes():
        size = tmp.stat().st_size
        tmp.unlink()
        raise ValueError(
            f"generated job snapshot is {size:,} bytes; limit is "
            f"{_max_job_snapshot_bytes():,}. Compact or shard state before publishing"
        )
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
        json.dump(obj, f, sort_keys=True, ensure_ascii=False,
                  separators=(",", ":"))
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
