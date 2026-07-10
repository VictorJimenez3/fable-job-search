"""Weekly strategist: every Monday, turn the week's raw pipeline data into a
strategy memo posted as a GitHub issue.

Deterministic core (works with zero credentials): pipeline stats, momentum,
follow-up nudges, market heat-map from real crawl data, this week's focus
targets. If ANTHROPIC_API_KEY is set, Claude writes a coach's narrative on top.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timezone

import requests

from . import state
from .llm import complete as llm_complete
from .config import env, github_owner, github_repo

API = "https://api.github.com"
WEEK = 7 * 86400


def _headers() -> dict:
    return {"Authorization": f"Bearer {env('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github+json"}


def build_memo() -> str:
    now = int(time.time())
    jobs = state.jobs()
    applied = state.applied()
    alert_history = state.load("alert_history.json", [])
    runs = state.load("runs.json", [])

    week_alerts = [a for a in alert_history if now - a.get("alerted_at", 0) <= WEEK]
    week_applied = [a for a in applied if now - a.get("applied_at", 0) <= WEEK]
    fresh = [j for j in jobs.values() if j.get("posted_at") and now - j["posted_at"] <= WEEK]
    ht_hiring = Counter(j["company"] for j in fresh if j.get("sector") == "healthtech")
    sector_heat = Counter(j.get("sector") or "other" for j in fresh)

    # follow-up nudges: applied 5-21 days ago
    stale = [a for a in applied if 5 * 86400 <= now - a.get("applied_at", 0) <= 21 * 86400]

    # best still-open, un-applied, alertable jobs right now
    applied_ids = {a["id"] for a in applied}
    live = sorted((j for j in jobs.values()
                   if j.get("alert_ok") and j["id"] not in applied_ids
                   and (j.get("posted_at") or now) >= now - 14 * 86400),
                  key=lambda j: -j["score"])[:10]

    apply_rate = f"{len(week_applied)}/{len(week_alerts)}" if week_alerts else "0/0"
    lines = [
        f"_Week of {datetime.now(timezone.utc).strftime('%b %d, %Y')}_",
        "",
        "## 📊 Pipeline",
        f"- **{len(week_alerts)} alerts** fired · **{len(week_applied)} applications** sent "
        f"(alert→apply: {apply_rate})",
        f"- {len(fresh)} eligible roles posted in the last 7 days across the market",
        f"- Sector heat this week: " + ", ".join(f"{s} {n}" for s, n in sector_heat.most_common(5)),
    ]
    if runs:
        ok_runs = sum(1 for r in runs if now - r["ts"] <= WEEK)
        lines.append(f"- Radar health: {ok_runs} crawls this week, "
                     f"{runs[-1].get('registry_size', '?')} companies in registry")
    if stale:
        lines += ["", "## ⏰ Follow up (applied 5–21 days ago, no marked response)"]
        lines += [f"- **{a['company']}** — {a['title'][:70]} "
                  f"(applied {datetime.fromtimestamp(a['applied_at'], timezone.utc).strftime('%b %d')})"
                  for a in stale[:10]]
    if ht_hiring:
        lines += ["", "## 🏥 Healthtech hiring right now",
                  ", ".join(f"**{c}** ({n})" for c, n in ht_hiring.most_common(10))]
    if live:
        lines += ["", "## 🎯 Top open targets you haven't applied to"]
        lines += [f"- `{j['score']}` **{j['company']}** — [{j['title'][:70]}]({j['url']})"
                  for j in live]

    memo = "\n".join(lines)

    narrative = llm_complete(
        "You are a sharp, encouraging job-search strategist for a graduating CS senior "
        "targeting new-grad AI/SWE/DS roles (healthtech first, big tech second). "
        "Based on this week's pipeline report, write a punchy 120-word coach's note: "
        "one observation, one priority for next week, one tactical tip. No preamble.\n\n" + memo,
        max_tokens=400)
    if narrative:
        memo = f"> 🧭 **Coach's note**\n> {narrative.strip()}\n\n" + memo
    return memo


def post_memo() -> str | None:
    token = env("GITHUB_TOKEN")
    memo = build_memo()
    if not token:
        print(memo)
        return None
    title = f"🧭 Weekly strategy — {datetime.now(timezone.utc).strftime('%b %d, %Y')}"
    r = requests.post(f"{API}/repos/{github_repo()}/issues", headers=_headers(), timeout=20,
                      json={"title": title, "body": memo, "labels": ["radar-strategy"],
                            "assignees": [github_owner()]})
    r.raise_for_status()
    return r.json()["html_url"]
