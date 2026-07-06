"""GitHub-issue alert channel.

Why issues: assigning an issue to you triggers GitHub's native notification
pipeline (mobile push + email) with zero extra credentials, and the issue body
doubles as the applied-logging UI — check a job's box after you apply and the
applied workflow logs it to Notion. One issue per ISO week keeps noise down;
each run appends a timestamped section.
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


def format_line(j: dict) -> str:
    loc = (j.get("locations") or ["?"])[0][:40]
    fire = "🔥 " if j["score"] >= 85 else ""
    note = f" — _{j['llm_note']}_" if j.get("llm_note") else ""
    salary = f" · {j['salary']}" if j.get("salary") else ""
    return (f"- [ ] {fire}**{j['company']}** — [{j['title'][:80]}]({j['url']}) · "
            f"{loc}{salary} · `{j['score']}` `{j.get('sector') or 'tech'}`{note} "
            f"<!--radar:{j['id']}-->")


HEADER = (
    "New high-scoring roles appear below as they're detected (every ~30 min).\n\n"
    "**✅ Check a job's box after you apply** — it will be logged to your Notion "
    "Applications tracker automatically (and boosts similar roles in ranking).\n"
    "Comment `skip <id>` to downrank similar roles, or `applied <url>` for jobs "
    "found elsewhere.\n")


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

    ts = datetime.now(timezone.utc).strftime("%a %b %d, %H:%M UTC")
    section = [f"\n### {ts} — {len(new_alerts)} new", ""]
    section += [format_line(j) for j in new_alerts]
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
        # start a continuation issue for the rest of the week
        r = requests.post(f"{API}/repos/{repo}/issues", headers=_headers(), timeout=20,
                          json={"title": title + f" (cont. {int(time.time()) % 1000})",
                                "body": HEADER + section_md, "labels": [LABEL],
                                "assignees": [github_owner()]})
        r.raise_for_status()
        return r.json()["html_url"]

    r = requests.patch(f"{API}/repos/{repo}/issues/{issue['number']}",
                       headers=_headers(), timeout=20, json={"body": body + section_md})
    r.raise_for_status()
    return issue["html_url"]
