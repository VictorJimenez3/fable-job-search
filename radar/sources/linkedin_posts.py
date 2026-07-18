"""LinkedIn hiring-post signal — without scraping LinkedIn.

Logged-in LinkedIn scraping risks the user's account and breaches ToS
(deliberately out of scope, see DECISIONS §13). Instead we use Google's
Programmable Search JSON API (free tier: 100 queries/day) to find public
`linkedin.com/posts` URLs mentioning chemical-engineering internship hiring. These are *leads*
(a human said "we're hiring"), not postings — they surface in the weekly
strategist memo, never as scored jobs.

Requires free user-creatable secrets: GOOGLE_CSE_KEY + GOOGLE_CSE_ID.
Absent → returns [] and the memo section is omitted.
"""
from __future__ import annotations

import time

from ..config import env
from ..http import get_json

API = "https://www.googleapis.com/customsearch/v1"

QUERIES = [
    'site:linkedin.com/posts (internship OR "co-op") hiring ("chemical engineering" OR "process engineering")',
    'site:linkedin.com/posts internship hiring (bioprocess OR manufacturing OR materials OR semiconductor) engineer',
]


def fetch_posts(max_per_query: int = 8) -> list[dict]:
    key, cse = env("GOOGLE_CSE_KEY"), env("GOOGLE_CSE_ID")
    if not key or not cse:
        return []
    out, seen = [], set()
    for q in QUERIES:
        try:
            data = get_json(API, params={"key": key, "cx": cse, "q": q,
                                         "num": max_per_query, "dateRestrict": "w2"})
        except Exception as e:
            print(f"linkedin_posts: search failed: {e}")
            continue
        for item in data.get("items", []):
            url = item.get("link", "")
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({"title": (item.get("title") or "")[:120],
                        "snippet": (item.get("snippet") or "")[:200],
                        "url": url, "found_at": int(time.time())})
    return out


def memo_section() -> list[str]:
    """Markdown lines for the strategist memo; [] when disabled or empty."""
    posts = fetch_posts()
    if not posts:
        return []
    lines = ["", "## 🗣️ Heard on LinkedIn (public posts, last 2 weeks)"]
    lines += [f"- [{p['title']}]({p['url']}) — {p['snippet'][:110]}" for p in posts[:10]]
    return lines
