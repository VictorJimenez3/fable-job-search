"""GitHub-issue alert channel.

Why issues: assigning an issue to you triggers GitHub's native notification
pipeline (mobile push + email) with zero extra credentials, and the issue body
doubles as the tracking UI — checking a job adds it to the owner's Notion
Applications database right away with the not-yet-applied status; he flips it
to Applied in Notion when he actually applies. One issue per ISO week keeps
noise down; each run appends a timestamped section.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from .config import env, github_owner, github_repo

API = "https://api.github.com"
LABEL = "radar-alerts"
BODY_LIMIT = 60000


def _headers() -> dict:
    return {"Authorization": f"Bearer {env('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _week_title() -> str:
    now = datetime.now(timezone.utc)
    return f"🎯 Job Radar alerts — week {now.strftime('%G-W%V')}"


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
    ptags = summary_tags(j.get("posting"))
    ptags = f" · {ptags}" if ptags else ""
    return (f"- [ ] {fire}**{j['company']}** — [{j['title'][:80]}]({j['url']}) · "
            f"{loc}{salary} · `{j['score']}`{ptags} · **{industry}** — {what}{ctag}{note} "
            f"<!--radar:{j['id']}-->")


HEADER = (
    "New high-scoring roles appear below as they're detected (every ~30 min).\n\n"
    "**☑️ Check a box to track a job** — it appears in your Notion "
    "Applications database immediately (status: not applied yet). When you "
    "actually apply, change its status in Notion. Comment `applied <url>` to "
    "log an application directly, including jobs found outside the radar.\n"
    "Comment `skip <company>` to downrank similar roles.\n")


def post_alerts(new_alerts: list[dict]) -> str | None:
    """Append alert lines to this week's issue (create if needed). Returns issue URL."""
    if not new_alerts:
        return None
    token = env("GITHUB_TOKEN")
    if not token:
        print("alerts: GITHUB_TOKEN not set — skipping issue creation")
        return None
    repo = github_repo()
    title = _week_title()

    r = requests.get(f"{API}/repos/{repo}/issues",
                     params={"labels": LABEL, "state": "open", "per_page": 50},
                     headers=_headers(), timeout=20)
    r.raise_for_status()
    issue = next((i for i in r.json() if i["title"] == title), None)

    from .culture import load as culture_load
    culture_map = culture_load()
    ts = datetime.now(timezone.utc).strftime("%a %b %d, %H:%M UTC")
    section = [f"\n### {ts} — {len(new_alerts)} new", ""]
    section += [format_line(j, culture_map) for j in new_alerts]
    section_md = "\n".join(section) + "\n"

    if issue is None:
        body = HEADER + section_md
        r = requests.post(f"{API}/repos/{repo}/issues", headers=_headers(), timeout=20,
                          json={"title": title, "body": body, "labels": [LABEL],
                                "assignees": [github_owner()]})
        r.raise_for_status()
        return r.json()["html_url"]

    body = issue["body"] or ""
    if len(body) + len(section_md) > BODY_LIMIT:
        # body is full: post the section as a comment on the same issue.
        # Comments notify subscribers (body edits don't), checkbox ticks in
        # comments are tracked, and it avoids continuation-issue clutter.
        r = requests.post(f"{API}/repos/{repo}/issues/{issue['number']}/comments",
                          headers=_headers(), timeout=20, json={"body": section_md})
        r.raise_for_status()
        return issue["html_url"]

    r = requests.patch(f"{API}/repos/{repo}/issues/{issue['number']}",
                       headers=_headers(), timeout=20, json={"body": body + section_md})
    r.raise_for_status()
    return issue["html_url"]
