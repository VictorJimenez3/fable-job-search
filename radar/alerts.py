"""GitHub-issue alert channel.

Why issues: one issue per new alert gives each posting a durable checkbox and
tracking surface. Those issues are intentionally unassigned; the separate
batch issue is the only normal alert notification surface.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import requests

from .config import env, github_repo, profile_id

API = "https://api.github.com"
LABEL = "radar-alerts"
BODY_LIMIT = 60000
REQUEST_TIMEOUT = (5, 20)


def _headers() -> dict:
    return {"Authorization": f"Bearer {env('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def lane_label() -> str:
    return "radar-internships" if profile_id() == "internship" else "radar-new-grad"


def _alert_title(job: dict) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
    industry, what = context(j["company"], j.get("sector") or "", culture_map)
    from .company_info import snapshot
    snapshot_text = snapshot(j["company"], j.get("sector") or "", culture_map)
    snapshot_text = f" · _{snapshot_text}_" if snapshot_text else ""
    from .provenance import info as source_info
    source_label, source_url = source_info(j.get("source", ""), j.get("source_url") or j.get("url", ""))
    found = f" · found via [{source_label}]({source_url})" if source_url else f" · found via {source_label}"
    ptags = summary_tags(j.get("posting"))
    ptags = f" · {ptags}" if ptags else ""
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
            f"{loc}{salary} · `{j['score']}`{ptags}{cohort} · **{industry}** — {what}{snapshot_text}{ctag}{found}{note} "
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


def post_alerts(new_alerts: list[dict]) -> str | None:
    """Create missing silent per-posting tracking issues.

    Delivery runs after the crawl state is committed.  A runner can be
    interrupted after GitHub accepts an issue but before it records anything
    locally, so the issue marker is the idempotency key rather than an
    in-memory "sent" flag.  Closed issues count too: intentionally closing a
    tracking issue must not make a later delivery recreate it.
    """
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
    return last_url


def _existing_alert_ids(repo: str, *, since: int | None = None) -> set[str]:
    """Return durable markers for the delivery window, not all-time history.

    ``since`` is deliberately based on the alert event rather than a mutable
    local cache.  That preserves idempotency if a runner dies after GitHub
    accepts an issue but before any generated state is written.
    """
    found: set[str] = set()
    page = 1
    params = {"labels": LABEL, "state": "all", "per_page": 100}
    if since:
        # Give clock skew and a manually edited issue a small cushion.
        stamp = datetime.fromtimestamp(max(0, since - 300), timezone.utc)
        params["since"] = stamp.isoformat().replace("+00:00", "Z")
    while True:
        response = requests.get(
            f"{API}/repos/{repo}/issues", headers=_headers(), timeout=REQUEST_TIMEOUT,
            params={**params, "page": page},
        )
        response.raise_for_status()
        issues = response.json()
        for issue in issues:
            body = issue.get("body") or ""
            found.update(re.findall(r"<!--radar:([^>]+)-->", body))
        if len(issues) < 100:
            return found
        page += 1
