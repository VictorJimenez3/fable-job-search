"""Hacker News 'Who is hiring' — startup roles that never hit job boards."""
from __future__ import annotations

import html
import re

from ..http import get_json
from ..models import Job

SEARCH = ("https://hn.algolia.com/api/v1/search_by_date"
          "?tags=story,author_whoishiring&query=%22who%20is%20hiring%22&hitsPerPage=3")
ITEM = "https://hn.algolia.com/api/v1/items/{}"

RELEVANT = re.compile(
    r"new ?grad|entry[- ]level|early[- ]career|junior|recent grad|university grad", re.I)
TAG = re.compile(r"<[^>]+>")


def fetch_hn(limit: int = 60) -> list[Job]:
    hits = get_json(SEARCH).get("hits", [])
    story = next((h for h in hits if "who is hiring" in (h.get("title") or "").lower()), None)
    if not story:
        return []
    item = get_json(ITEM.format(story["objectID"]))
    out = []
    for c in item.get("children", []):
        raw = c.get("text") or ""
        text = html.unescape(TAG.sub(" ", raw))
        if not RELEVANT.search(text):
            continue
        first_line = text.strip().split("\n")[0][:160]
        company = first_line.split("|")[0].strip()[:60] or "HN startup"
        out.append(Job(
            company=company,
            title=f"Engineering role (HN Who is Hiring): {first_line[:90]}",
            url=f"https://news.ycombinator.com/item?id={c['id']}",
            source="hn", ats="hn",
            locations=[],
            posted_at=c.get("created_at_i"),
            description=text[:4000],
            remote="remote" in text.lower(),
        ))
        if len(out) >= limit:
            break
    return out
