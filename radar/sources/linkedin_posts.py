"""LinkedIn hiring-post signal — without scraping LinkedIn.

Logged-in LinkedIn scraping risks the user's account and breaches ToS
(deliberately out of scope, see DECISIONS §13). Instead we use Google's
Programmable Search JSON API (free tier: 100 queries/day) to find public
`linkedin.com/posts` URLs mentioning new-grad hiring. These are *leads*
(a human said "we're hiring"), not postings — they surface in the weekly
strategist memo, never as scored jobs.

Requires free user-creatable secrets: GOOGLE_CSE_KEY + GOOGLE_CSE_ID.
Absent → returns [] and the memo section is omitted.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from ..config import env, profile
from ..http import get_json

API = "https://www.googleapis.com/customsearch/v1"

def current_year() -> int:
    return datetime.now(UTC).year


def candidate_grad_year() -> int:
    """Return the candidate's configured graduation year for public queries."""
    raw = env("RADAR_GRAD_YEAR", "")
    if not raw:
        try:
            raw = profile().get("candidate", {}).get("grad_year", "")
        except (AttributeError, OSError, TypeError):
            raw = ""
    try:
        year = int(raw)
    except (TypeError, ValueError):
        year = current_year()
    return year if 2000 <= year <= 2100 else current_year()


def build_queries(grad_year: int | None = None) -> list[str]:
    year = candidate_grad_year() if grad_year is None else int(grad_year)
    return [
        f'site:linkedin.com/posts "new grad" (hiring OR "open role") {year} '
        '(AI OR "machine learning" OR "software engineer" OR "data scien")',
        f'site:linkedin.com/posts "entry level" hiring {year} '
        '("health tech" OR healthcare) engineer',
    ]


# Compatibility for callers that imported the old constant.  It is generated
# from the profile instead of freezing an obsolete graduation year at import.
QUERIES = build_queries()


def _is_public_post_url(value: object) -> bool:
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme in {"http", "https"}
        and (host == "linkedin.com" or host.endswith(".linkedin.com"))
        and parsed.path.rstrip("/").startswith("/posts/")
    )


def fetch_posts(max_per_query: int = 8, queries: list[str] | None = None) -> list[dict]:
    key, cse = env("GOOGLE_CSE_KEY"), env("GOOGLE_CSE_ID")
    if not key or not cse:
        return []
    out, seen = [], set()
    for q in (queries if queries is not None else build_queries()):
        try:
            data = get_json(API, params={"key": key, "cx": cse, "q": q,
                                         "num": max_per_query, "dateRestrict": "w2"})
        except Exception as e:
            print(f"linkedin_posts: search failed: {e}")
            continue
        for item in data.get("items", []):
            url = item.get("link", "")
            if not _is_public_post_url(url) or url in seen:
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
