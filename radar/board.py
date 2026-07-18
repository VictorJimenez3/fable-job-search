"""Consolidated views: the master-board issue and the daily-best issue.

Why: weekly alert issues fill up (GitHub caps an issue body at ~64KB) and
The candidate shouldn't bounce between them to check boxes. The master board is ONE
stable issue holding every open, alert-worthy role — body plus bot comments
as extra pages, rewritten in place each crawl. Its checkboxes carry the same
``<!--radar:ID-->`` markers, so the existing applied-sync workflow tracks
them to Notion (comment-checkbox edits included). The daily-best issue is a
fresh top-10 each evening; because it's assigned to the repository owner, GitHub's own
notification email doubles as the requested daily "best of" email with zero
mail credentials.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from .alerts import API, LABEL, PROFILE_LABEL, _headers, format_line
from .config import env, github_assignee, github_repo, profile

MASTER_TITLE = "🧪 ChemE Job Radar — master board"
MASTER_LABEL = "radar-cheme-master"
DAILY_LABEL = "radar-cheme-daily"
PAGE_LIMIT = 55000  # per body/comment, under GitHub's 65536 cap

MASTER_HEADER = (
    "Every alert-worthy open role the radar currently knows, best first — "
    "**{count} roles**, refreshed {stamp}. Extra pages live in the comments "
    "below. ChemE notifications: @ak2943\n\n"
    "☑️ Checking a box here works exactly like the weekly issues: the job "
    "lands in your Notion tracker as not-applied; flip its status in Notion "
    "when you apply. Already-tracked jobs show up pre-checked.\n")


def _open_rows(jobs_state: dict, now: int, max_age_days: int = 30) -> list[dict]:
    thr = profile()["thresholds"]["alert"]
    rows = [r for r in jobs_state.values()
            if r.get("alert_ok") and r.get("score", 0) >= thr
            and now - r.get("first_seen", now) <= max_age_days * 86400]
    rows.sort(key=lambda r: -r.get("score", 0))
    return rows


def _paginate(lines: list[str], limit: int = PAGE_LIMIT) -> list[str]:
    pages, cur, size = [], [], 0
    for ln in lines:
        if size + len(ln) + 1 > limit and cur:
            pages.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    pages.append("\n".join(cur))
    return pages


def update_master_board(jobs_state: dict, applied: list) -> str | None:
    """Rewrite the single master-board issue in place. Returns its URL."""
    token = env("GITHUB_TOKEN")
    if not token:
        print("board: GITHUB_TOKEN not set — skipping master board")
        return None
    repo = github_repo()
    now = int(time.time())
    from .culture import load as culture_load
    culture_map = culture_load()
    applied_ids = {a["id"] for a in applied}

    lines = []
    for r in _open_rows(jobs_state, now):
        line = format_line(r, culture_map)
        if r["id"] in applied_ids:
            line = line.replace("- [ ]", "- [x]", 1)
        lines.append(line)
    pages = _paginate(lines)
    stamp = datetime.now(timezone.utc).strftime("%a %b %d, %H:%M UTC")
    body = MASTER_HEADER.format(count=len(lines), stamp=stamp) + "\n" + pages[0]

    r = requests.get(f"{API}/repos/{repo}/issues",
                     params={"labels": MASTER_LABEL, "state": "open", "per_page": 5},
                     headers=_headers(), timeout=20)
    r.raise_for_status()
    found = r.json()
    if found:
        issue = found[0]
        requests.patch(f"{API}/repos/{repo}/issues/{issue['number']}",
                       headers=_headers(), timeout=20,
                       json={"body": body}).raise_for_status()
    else:
        r = requests.post(f"{API}/repos/{repo}/issues", headers=_headers(), timeout=20,
                          json={"title": MASTER_TITLE, "body": body,
                                "labels": [LABEL, PROFILE_LABEL, MASTER_LABEL],
                                "assignees": [github_assignee()]})
        r.raise_for_status()
        issue = r.json()

    # pages 2..n live in the bot's own comments, edited in place
    cr = requests.get(issue["comments_url"], params={"per_page": 100},
                      headers=_headers(), timeout=20)
    cr.raise_for_status()
    own = [c for c in cr.json() if c["user"]["login"].endswith("[bot]")]
    extra_pages = pages[1:]
    for i, page in enumerate(extra_pages):
        text = f"**Page {i + 2}**\n\n{page}"
        if i < len(own):
            requests.patch(f"{API}/repos/{repo}/issues/comments/{own[i]['id']}",
                           headers=_headers(), timeout=20,
                           json={"body": text}).raise_for_status()
        else:
            requests.post(issue["comments_url"], headers=_headers(), timeout=20,
                          json={"body": text}).raise_for_status()
    for c in own[len(extra_pages):]:
        requests.patch(f"{API}/repos/{repo}/issues/comments/{c['id']}",
                       headers=_headers(), timeout=20,
                       json={"body": "_(page currently unused)_"}).raise_for_status()
    return issue["html_url"]


def post_daily_best(jobs_state: dict, top_n: int = 10) -> str | None:
    """Create today's 🏆 best-of issue (idempotent; closes yesterday's)."""
    token = env("GITHUB_TOKEN")
    if not token:
        print("board: GITHUB_TOKEN not set — skipping daily best")
        return None
    repo = github_repo()
    now = int(time.time())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = f"🧪 ChemE best of {today}"

    rows = [r for r in jobs_state.values()
            if r.get("alert_ok") and now - r.get("first_seen", 0) <= 86400]
    rows.sort(key=lambda r: -r.get("score", 0))
    rows = rows[:top_n]
    if not rows:
        print("daily-best: no new alert-worthy roles in the last 24h")
        return None

    r = requests.get(f"{API}/repos/{repo}/issues",
                     params={"labels": DAILY_LABEL, "state": "open", "per_page": 20},
                     headers=_headers(), timeout=20)
    r.raise_for_status()
    open_dailies = r.json()
    if any(i["title"] == title for i in open_dailies):
        print("daily-best: today's issue already exists")
        return next(i["html_url"] for i in open_dailies if i["title"] == title)
    for old in open_dailies:  # yesterday's digest has served its purpose
        requests.patch(f"{API}/repos/{repo}/issues/{old['number']}",
                       headers=_headers(), timeout=20,
                       json={"state": "closed"}).raise_for_status()

    from .culture import load as culture_load
    culture_map = culture_load()
    body = (f"The {len(rows)} strongest roles the radar found in the last 24 hours. "
            "Check a box to track it in Notion.\n\n"
            + "\n".join(format_line(x, culture_map) for x in rows) + "\n")
    r = requests.post(f"{API}/repos/{repo}/issues", headers=_headers(), timeout=20,
                      json={"title": title, "body": body,
                            "labels": [LABEL, PROFILE_LABEL, DAILY_LABEL],
                            "assignees": [github_assignee()]})
    r.raise_for_status()
    return r.json()["html_url"]
