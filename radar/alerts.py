"""GitHub-issue alert channel.

Why issues: assigning one issue per new alert to you triggers GitHub's native
notification pipeline (mobile push + email) with zero extra credentials.
The separate master board is the manual all-at-once view; it is intentionally
not assigned and does not generate notifications.
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


def _alert_title(job: dict) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"🎯 {job['company']} — {job['title'][:72]} · {stamp}"


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
    ptags = summary_tags(j.get("posting"))
    ptags = f" · {ptags}" if ptags else ""
    return (f"- [ ] {fire}**{j['company']}** — [{j['title'][:80]}]({j['url']}) · "
            f"{loc}{salary} · `{j['score']}`{ptags} · **{industry}** — {what}{snapshot_text}{ctag}{note} "
            f"<!--radar:{j['id']}-->")


HEADER = (
    "New high-scoring roles appear below as they're detected (every ~30 min).\n\n"
    "**☑️ Check a box to track a job** — it appears in your Notion "
    "Applications database immediately (status: not applied yet). When you "
    "actually apply, change its status in Notion. Comment `applied <url>` to "
    "log an application directly, including jobs found outside the radar.\n"
    "Comment `skip <company>` to downrank similar roles.\n")


def post_alerts(new_alerts: list[dict]) -> str | None:
    """Create one assigned issue per new alert so each alert can notify Victor."""
    if not new_alerts:
        return None
    token = env("GITHUB_TOKEN")
    if not token:
        print("alerts: GITHUB_TOKEN not set — skipping issue creation")
        return None
    repo = github_repo()
    from .culture import load as culture_load
    culture_map = culture_load()
    last_url = None
    for job in new_alerts:
        r = requests.post(
            f"{API}/repos/{repo}/issues", headers=_headers(), timeout=20,
            json={"title": _alert_title(job),
                  "body": HEADER + format_line(job, culture_map) + "\n",
                  "labels": [LABEL], "assignees": [github_owner()]})
        r.raise_for_status()
        last_url = r.json().get("html_url") or last_url
    return last_url
