"""Render outputs: the committed dashboard (docs/DASHBOARD.md) and RSS feed
(docs/feed.xml). Both are plain files served straight off GitHub."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from .config import DOCS_DIR, github_repo, profile


def _age(posted_at, now) -> str:
    if not posted_at:
        return "?"
    h = (now - posted_at) / 3600
    if h < 1:
        return "<1h"
    if h < 24:
        return f"{int(h)}h"
    return f"{int(h / 24)}d"


def _fire(score: int) -> str:
    return "🔥" if score >= 85 else ("⭐" if score >= 75 else "")


def render_dashboard(jobs: dict, registry: dict, runs: list) -> str:
    now = int(time.time())
    p = profile()
    cutoff = now - 30 * 86400
    rows = [j for j in jobs.values()
            if j["score"] >= p["thresholds"]["dashboard"]
            and (j.get("posted_at") or j.get("first_seen", now)) >= cutoff]
    # Alert-eligible roles are the actual user-facing matches. Dashboard-only
    # roles remain available for audit/context, but raw score alone must not
    # let an off-field or experienced role outrank a qualified new-grad role.
    rows.sort(key=lambda j: (0 if j.get("alert_ok") else 1,
                             -j["score"], -(j.get("posted_at") or 0)))
    rows = rows[:150]

    active = sum(1 for e in registry.values() if e["status"] == "active")
    last = runs[-1] if runs else {}
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# 🎯 Job Radar — live dashboard",
        "",
        f"_Last run: **{ts}** · companies polled directly: **{active}** "
        f"(registry {len(registry)}) · jobs tracked: **{len(jobs)}** · "
        f"new this run: **{last.get('new_jobs', 0)}** · alerts this run: **{last.get('alerts', 0)}**_",
        "",
        "Check the box on the [alert issue](../../issues?q=is%3Aissue+label%3Aradar-alerts) "
        "after applying — it logs to Notion automatically.",
        "",
        "| Score | Age | Company | Role | Location | Sector | Src |",
        "|---|---|---|---|---|---|---|",
    ]
    for j in rows:
        loc = (j.get("locations") or [""])[0][:40]
        lines.append(
            f"| {j['score']} {_fire(j['score'])} | {_age(j.get('posted_at'), now)} "
            f"| {j['company'][:38]} | [{j['title'][:70]}]({j['url']}) "
            f"| {loc} | {j.get('sector') or '—'} | {j['source']} |")
    lines += ["", f"_{len(rows)} roles shown (score ≥ {p['thresholds']['dashboard']}, posted ≤30d)._"]
    return "\n".join(lines) + "\n"


def render_rss(alert_history: list) -> str:
    repo = github_repo()
    items = []
    for a in alert_history[-100:][::-1]:
        pub = datetime.fromtimestamp(a.get("alerted_at", 0), timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        title = "[{}] {} — {}".format(a["score"], a["company"], a["title"])
        loc = (a.get("locations") or ["?"])[0]
        desc = "{} · {} · via {}".format(a.get("sector") or "tech", loc, a["source"])
        items.append(
            "<item>"
            f"<title>{escape(title)}</title>"
            f"<link>{escape(a['url'])}</link>"
            f"<guid isPermaLink=\"false\">{a['id']}</guid>"
            f"<pubDate>{pub}</pubDate>"
            f"<description>{escape(desc)}</description>"
            "</item>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        f"<title>Job Radar — new grad alerts</title>"
        f"<link>https://github.com/{repo}</link>"
        f"<description>High-scoring new-grad AI/SWE/DS roles, minutes after posting.</description>"
        + "".join(items) + "</channel></rss>\n")


def write_outputs(jobs: dict, registry: dict, runs: list, alert_history: list) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "DASHBOARD.md").write_text(render_dashboard(jobs, registry, runs))
    (DOCS_DIR / "feed.xml").write_text(render_rss(alert_history))
