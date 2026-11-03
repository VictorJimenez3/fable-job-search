"""GitHub-issue alert channel.

Why issues: one issue per new alert gives each posting a durable checkbox and
tracking surface. Those issues are intentionally unassigned; the separate
batch issue is the only normal alert notification surface.
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime

import requests

from . import lifecycle, state
from .config import env, github_repo, profile, profile_id
from .identity import posting_key
from .models import norm

API = "https://api.github.com"
LABEL = "radar-alerts"
BODY_LIMIT = 60000
REQUEST_TIMEOUT = (5, 20)
_RESEARCH_CACHE: dict | None = None


def _load_job_aliases() -> dict[str, str]:
    raw = state.load("job_aliases.json", {})
    return ({str(key): str(value) for key, value in raw.items()}
            if isinstance(raw, dict) else {})


def _resolve_job_alias(job_id: str, aliases: dict[str, str] | None = None) -> str:
    aliases = _load_job_aliases() if aliases is None else aliases
    current = str(job_id)
    visited: set[str] = set()
    while current in aliases and current not in visited:
        visited.add(current)
        current = str(aliases[current])
    return current


def _expand_marker_ids(marker_ids: set[str], aliases: dict[str, str] | None = None) -> set[str]:
    """Include both sides of persisted ID aliases in durable marker lookups."""
    aliases = _load_job_aliases() if aliases is None else aliases
    expanded = {str(marker) for marker in marker_ids if marker}
    resolved = {_resolve_job_alias(marker, aliases) for marker in expanded}
    expanded.update(resolved)
    for alias in aliases:
        if _resolve_job_alias(alias, aliases) in resolved:
            expanded.add(str(alias))
    return expanded


def _posting_marker(job: dict) -> str:
    key = posting_key(job.get("url"))
    return f"posting:{key}" if key else f"id:{job.get('id', '')}"


def _headers() -> dict:
    return {"Authorization": f"Bearer {env('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def lane_label() -> str:
    return "radar-internships" if profile_id() == "internship" else "radar-new-grad"


def _research_records() -> dict:
    """Load the shared company cache once for a multi-row delivery render."""
    global _RESEARCH_CACHE
    if _RESEARCH_CACHE is None:
        try:
            from . import company_research
            _RESEARCH_CACHE = company_research.load()
        except Exception:
            _RESEARCH_CACHE = {}
    return _RESEARCH_CACHE


def _startup_evidence(job: dict) -> dict:
    """Hydrate compacted job rows from the cited company research cache."""
    evidence = job.get("startup_stage_evidence") or {}
    if evidence.get("value") and evidence.get("source_ids"):
        return evidence
    claim = (_research_records().get(norm(job.get("company", ""))) or {}).get("size_stage")
    if not isinstance(claim, dict) or not claim.get("value") or not claim.get("source_ids"):
        return evidence
    return {"value": claim["value"], "confidence": claim.get("confidence", "unknown"),
            "source_ids": claim["source_ids"]}


def _alert_title(job: dict) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    prefix = "🎓 Internship" if profile_id() == "internship" else "🎯"
    return f"{prefix} {job['company']} — {job['title'][:72]} · {stamp}"


def format_line(j: dict, culture_map: dict | None = None) -> str:
    loc = (j.get("locations") or ["?"])[0][:40]
    fire = "🔥 " if j["score"] >= 85 else ""
    note = f" — _{j['llm_note']}_" if j.get("llm_note") else ""
    salary = f" · {j['salary']}" if j.get("salary") else ""
    ctag = ""
    if culture_map is not None:
        from .culture import alert_tag
        t = alert_tag(j["company"], culture_map)
        ctag = f" {t}" if t else ""
    from .company_info import context
    from .posting import summary_tags
    research_records = _research_records()
    industry, what = context(j["company"], j.get("sector") or "", culture_map,
                              research_records)
    from .company_info import snapshot
    snapshot_text = snapshot(j["company"], j.get("sector") or "", culture_map,
                             research_records)
    snapshot_text = f" · _{snapshot_text}_" if snapshot_text else ""
    from .provenance import alternate_link_label, source_links
    provenance = source_links(j)
    if provenance:
        source_label, source_url = provenance[0]
        found = f" · found via [{source_label}]({source_url})"
        for label, url in provenance[1:3]:
            found += f" · also via [{label}]({url})"
    else:
        found = ""
    for url in (j.get("alternate_urls") or [])[:2]:
        if url:
            found += f" · [{alternate_link_label(url)}]({url})"
    ptags = summary_tags(j.get("posting"))
    ptags = f" · {ptags}" if ptags else ""
    try:
        career_tier = int(j.get("career_priority") or 0)
    except (TypeError, ValueError):
        career_tier = 0
    career = ""
    if career_tier >= 2:
        career = " · **career:** explicit new-grad priority"
    elif career_tier == 1:
        career = " · **career:** early-career compatible"
    startup = ""
    if j.get("startup_stage") and j.get("startup_stage") != "unknown":
        evidence = _startup_evidence(j)
        sources = ", ".join(str(x) for x in evidence.get("source_ids") or []) or "none"
        try:
            startup_points = int(j.get("startup_score") or 0)
        except (TypeError, ValueError):
            startup_points = 0
        startup = (f" · **startup:** {j['startup_stage']} (+{startup_points}; "
                   f"{evidence.get('confidence', 'unknown')} confidence; source IDs: {sources})")
    cohort = ""
    if profile_id() == "internship":
        eligibility = j.get("internship_eligibility") or {}
        classes = ", ".join(eligibility.get("class_years") or [])
        grad = ""
        if eligibility.get("graduation_start"):
            grad = f"classes {eligibility['graduation_start'][:4]}–{eligibility['graduation_end'][:4]}"
        cohort_text = ", ".join(x for x in (classes, grad) if x) or "eligibility not stated"
        cohort = f" · **cohort:** {cohort_text}"
    return (f"- [ ] {fire}**{j['company']}** — [{j['title'][:80]}]({j['url']}) · "
            f"{loc}{salary} · `{j['score']}`{career}{startup}{ptags}{cohort} · **{industry}** — {what}{snapshot_text}{ctag}{found}{note} "
            f"<!--radar:{j['id']}-->")


HEADER = (
    "New high-scoring roles appear below as they're detected (every ~30 min).\n\n"
    "**☑️ Check a box to track a job** — it appears in your Notion "
    "Applications database immediately (status: not applied yet). When you "
    "actually apply, change its status in Notion. Comment `applied <url>` to "
    "log an application directly, including jobs found outside the radar.\n"
    "Comment `skip <company>` to downrank similar roles.\n")


def header() -> str:
    if profile_id() == "internship":
        return (
            "Internship roles appear in the separate Internship lane.\n\n"
            "**☑️ Check a box to track a role** — it lands in the internship pipeline. "
            "Internship email batches are disabled unless the board owner opts in.\n"
        )
    return HEADER


def _dedupe_alert_rows(rows: list[dict]) -> list[dict]:
    """Dedupe by durable job ID and provider-aware posting identity."""
    chosen: list[dict] = []
    by_id: dict[str, int] = {}
    by_posting: dict[str, int] = {}
    positions: dict[int, tuple[int, int]] = {}
    for position, row in enumerate(rows):
        job_id = str(row.get("id") or "")
        if not job_id:
            continue
        indexes = [index for index in (by_id.get(job_id), by_posting.get(_posting_marker(row)))
                   if index is not None]
        previous_index = indexes[0] if indexes else None
        if previous_index is not None:
            previous = chosen[previous_index]
            previous_order = positions[previous_index]
            current_order = (int(row.get("alerted_at", 0) or 0), position)
            if current_order < previous_order:
                continue
            chosen[previous_index] = row
            positions[previous_index] = current_order
            old_id = str(previous.get("id") or "")
            old_posting = _posting_marker(previous)
            if by_id.get(old_id) == previous_index:
                by_id.pop(old_id, None)
            if by_posting.get(old_posting) == previous_index:
                by_posting.pop(old_posting, None)
            by_id[job_id] = previous_index
            by_posting[_posting_marker(row)] = previous_index
            continue
        index = len(chosen)
        chosen.append(row)
        positions[index] = (int(row.get("alerted_at", 0) or 0), position)
        by_id[job_id] = index
        by_posting[_posting_marker(row)] = index
    return chosen


def _current_alert_rows(rows: list[dict], jobs_state: dict | None) -> list[dict]:
    """Hydrate and revalidate rows when the caller supplies current state."""
    if jobs_state is None:
        return [row for row in rows if not lifecycle.is_terminal(row)]
    aliases = _load_job_aliases()
    threshold = int(profile()["thresholds"]["alert"])
    current_rows: list[dict] = []
    for history_row in rows:
        job_id = str(history_row.get("id") or "")
        current_id = _resolve_job_alias(job_id, aliases)
        current = jobs_state.get(current_id) or jobs_state.get(job_id)
        if (current is None or lifecycle.is_terminal(current)
                or not current.get("alert_ok")
                or int(current.get("score", 0) or 0) < threshold):
            continue
        row = dict(current)
        row["alerted_at"] = history_row.get("alerted_at", 0)
        current_rows.append(row)
    return current_rows


def post_alerts(new_alerts: list[dict], jobs_state: dict | None = None) -> str | None:
    """Create missing silent per-posting tracking issues.

    Delivery runs after the crawl state is committed.  A runner can be
    interrupted after GitHub accepts an issue but before it records anything
    locally, so the issue marker is the idempotency key rather than an
    in-memory "sent" flag.  Closed issues count too: intentionally closing a
    tracking issue must not make a later delivery recreate it.  When
    ``jobs_state`` is supplied, it is authoritative for current score,
    eligibility, and lifecycle; an empty mapping therefore suppresses all
    history rows rather than falling back to stale alert records.
    """
    # Alias repair can leave two history entries pointing at the same survivor.
    # Collapse those in-memory before posting: the GitHub lookup is performed
    # once per delivery call, so a duplicate input would otherwise create two
    # issues before the newly-created marker can be observed.
    new_alerts = _dedupe_alert_rows(_current_alert_rows(new_alerts, jobs_state))
    if not new_alerts:
        return None
    token = env("GITHUB_TOKEN")
    if not token:
        print("alerts: GITHUB_TOKEN not set — skipping issue creation")
        return None
    repo = github_repo()
    from .culture import load as culture_load
    culture_map = culture_load()
    # Delivery only ever considers the recent alert-history window.  Looking
    # through every historical tracking issue on each crawl made this phase
    # grow forever with the board, even when there were no new jobs.  GitHub's
    # `since` filter remains safe for the crash case: an issue created for one
    # of these jobs is necessarily updated after its alert timestamp.
    since = min((int(j.get("alerted_at", time.time())) for j in new_alerts),
                default=int(time.time()))
    existing_ids = _existing_alert_ids(repo, since=since)
    print(f"alerts: checking {len(new_alerts)} recent job(s); found {len(existing_ids)} durable issue marker(s)")
    last_url = None
    for job in new_alerts:
        if job["id"] in existing_ids:
            continue
        r = requests.post(
            f"{API}/repos/{repo}/issues", headers=_headers(), timeout=REQUEST_TIMEOUT,
            json={"title": _alert_title(job),
                  "body": header() + format_line(job, culture_map) + "\n",
                  "labels": [LABEL, lane_label()], "assignees": []})
        r.raise_for_status()
        last_url = r.json().get("html_url") or last_url
        # Protect this call from repeated history aliases even if a caller
        # bypasses the normalization above in a future delivery path.
        existing_ids.add(job["id"])
    return last_url


def _existing_alert_ids(repo: str, *, since: int | None = None) -> set[str]:
    """Return durable markers for the delivery window, not all-time history.

    ``since`` is deliberately based on the alert event rather than a mutable
    local cache.  That preserves idempotency if a runner dies after GitHub
    accepts an issue but before any generated state is written.
    """
    issues = _marker_issues(repo, since=since, labels=LABEL)
    current_lane = lane_label()
    eligible: set[str] = set()
    for issue in issues:
        labels = {
            str(label.get("name") or "") if isinstance(label, dict) else str(label)
            for label in issue.get("labels", [])
        }
        body = issue.get("body") or ""
        title = issue.get("title") or ""
        if current_lane in labels:
            eligible.add(body)
            continue
        # Older new-grad issues predate the lane label. They remain valid
        # receipts, but an unlabelled internship issue must never suppress a
        # new-grad delivery. Internship titles/bodies carried an explicit
        # marker even before the lane label was added.
        legacy_internship = (title.startswith("🎓 Internship")
                             or "Internship roles appear" in body)
        if (not labels.intersection({"radar-new-grad", "radar-internships"})
                and ((current_lane == "radar-new-grad" and not legacy_internship)
                     or (current_lane == "radar-internships" and legacy_internship))):
            eligible.add(body)
    found: set[str] = set()
    for body in eligible:
        found.update(re.findall(r"<!--radar:([^>]+)-->", body))
    return _expand_marker_ids(found)


def _existing_marker_ids(repo: str, *, since: int | None = None,
                         labels: str = LABEL) -> set[str]:
    """Return radar markers from bounded GitHub issue history.

    Issue bodies are the durable receipt for delivery.  ``labels`` lets the
    batch lane query only its own issues, so a retry after a state-write crash
    cannot create a second batch while the lookup remains bounded to the
    active alert window.
    """
    found: set[str] = set()
    for issue in _marker_issues(repo, since=since, labels=labels):
        found.update(re.findall(r"<!--radar:([^>]+)-->", issue.get("body") or ""))
    return _expand_marker_ids(found)


def _marker_issues(repo: str, *, since: int | None = None,
                   labels: str = LABEL) -> list[dict]:
    """Fetch bounded issue records, retaining labels for legacy lane checks."""
    found: list[dict] = []
    page = 1
    params = {"labels": labels, "state": "all", "per_page": 100}
    if since:
        # Give clock skew and a manually edited issue a small cushion.
        stamp = datetime.fromtimestamp(max(0, since - 300), UTC)
        params["since"] = stamp.isoformat().replace("+00:00", "Z")
    while True:
        response = requests.get(
            f"{API}/repos/{repo}/issues", headers=_headers(), timeout=REQUEST_TIMEOUT,
            params={**params, "page": page},
        )
        response.raise_for_status()
        issues = response.json()
        found.extend(issue for issue in issues if isinstance(issue, dict))
        if len(issues) < 100:
            return found
        page += 1
