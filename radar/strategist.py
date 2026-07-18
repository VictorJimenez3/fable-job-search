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
from .config import env, github_assignee, github_owner, github_repo, profile

API = "https://api.github.com"
WEEK = 7 * 86400


def _headers() -> dict:
    return {"Authorization": f"Bearer {env('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github+json"}


def response_rates(applied: list, jobs: dict) -> dict:
    """Calculate per-sector and per-source response rates from applied entries.

    A "response" is any stage that indicates the company has acted on the application:
    OA (online assessment), interview, or rejection. The "applied" stage means
    nothing has happened yet - no response.
    """
    now = int(time.time())
    three_weeks_ago = now - 3 * 7 * 86400  # last 3 weeks

    # Include all applications in the window, regardless of current stage
    window = [a for a in applied if a.get("applied_at", 0) >= three_weeks_ago]

    sector_stats: dict[str, dict] = {}
    source_stats: dict[str, dict] = {}

    for entry in window:
        job_info = jobs.get(entry["id"], {})
        sector = (job_info.get("sector") or "other")
        source = (job_info.get("source") or "unknown")

        # Count as applied
        if sector not in sector_stats:
            sector_stats[sector] = {"applied": 0, "responded": 0}
        sector_stats[sector]["applied"] += 1

        if source not in source_stats:
            source_stats[source] = {"applied": 0, "responded": 0}
        source_stats[source]["applied"] += 1

        # Count as responded: OA, interview, or rejection (not "applied" or "saved")
        if entry.get("stage") in {"oa", "interview", "rejected"}:
            sector_stats[sector]["responded"] += 1
            source_stats[source]["responded"] += 1

    return {"by_sector": sector_stats, "by_source": source_stats}


def build_memo() -> str:
    now = int(time.time())
    jobs = state.jobs()
    applied = state.applied()
    alert_history = state.load("alert_history.json", [])
    runs = state.load("runs.json", [])

    week_alerts = [a for a in alert_history if now - a.get("alerted_at", 0) <= WEEK]
    week_applied = [a for a in applied if now - a.get("applied_at", 0) <= WEEK]
    fresh = [j for j in jobs.values() if j.get("posted_at") and now - j["posted_at"] <= WEEK]
    weights = profile().get("sectors", {})
    top_sector = max((s for s in weights if s != "other"), key=weights.get, default="other")
    priority_hiring = Counter(j["company"] for j in fresh if j.get("sector") == top_sector)
    sector_heat = Counter(j.get("sector") or "other" for j in fresh)

    # Response rate analytics (last 3 weeks)
    rates = response_rates(applied, jobs)

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
    # Funnel: where do the applications the email watcher tracks actually stand?
    real = [a for a in applied if a.get("stage") in
            {"applied", "oa", "interview", "rejected", "closed"}]
    if real:
        stages = Counter(a.get("stage") for a in real)
        responded = stages["oa"] + stages["interview"] + stages["rejected"]
        total_appl = len(real)
        resp_rate = f"{round(100 * responded / total_appl)}%" if total_appl else "—"
        lines += [
            "", "## 🔻 Funnel (auto-tracked from your inbox)",
            f"- **{total_appl}** applied · **{stages['oa']}** OA · "
            f"**{stages['interview']}** interview · **{stages['rejected']}** rejected · "
            f"**{stages['closed']}** auto-closed",
            f"- Response rate: **{resp_rate}** · still advancing: "
            f"**{stages['oa'] + stages['interview']}**",
        ]

    # Response rate breakdowns by sector/source (3-week window, min sample 3
    # per bucket to avoid noisy single-application percentages)
    MIN_SAMPLE = 3
    by_sec = [f"{s} {round(100 * st['responded'] / st['applied'])}%"
              for s, st in sorted(rates["by_sector"].items(), key=lambda x: -x[1]["applied"])
              if st["applied"] >= MIN_SAMPLE]
    by_src = [f"{s} {round(100 * st['responded'] / st['applied'])}%"
              for s, st in sorted(rates["by_source"].items(), key=lambda x: -x[1]["applied"])
              if st["applied"] >= MIN_SAMPLE]
    if by_sec or by_src:
        lines += ["", "## 📈 Response rates by sector/source (last 3 weeks)"]
        if by_sec:
            lines.append("- By sector: " + " · ".join(by_sec))
        if by_src:
            lines.append("- By source: " + " · ".join(by_src))

    if stale:
        lines += ["", "## ⏰ Follow up (applied 5–21 days ago, no marked response)"]
        lines += [f"- **{a['company']}** — {a['title'][:70]} "
                  f"(applied {datetime.fromtimestamp(a['applied_at'], timezone.utc).strftime('%b %d')})"
                  for a in stale[:10]]
    if priority_hiring:
        lines += ["", f"## 🧪 {top_sector.replace('_', ' ').title()} hiring right now",
                  ", ".join(f"**{c}** ({n})" for c, n in priority_hiring.most_common(10))]
    if live:
        lines += ["", "## 🎯 Top open targets you haven't applied to"]
        lines += [f"- `{j['score']}` **{j['company']}** — [{j['title'][:70]}]({j['url']})"
                  for j in live]

    from .sources.linkedin_posts import memo_section
    try:
        lines += memo_section()
    except Exception as e:
        print(f"strategist: linkedin section skipped: {e}")

    memo = "\n".join(lines)

    narrative = llm_complete(
        "You are a sharp, encouraging internship-search strategist for an undergraduate "
        "chemical engineering student targeting US internships and co-ops. "
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
                            "assignees": [github_assignee()]})
    r.raise_for_status()
    return r.json()["html_url"]
