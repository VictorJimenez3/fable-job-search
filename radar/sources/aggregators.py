"""GitHub-hosted internship aggregators. These are a breadth layer; ChemE
precision comes from the role gates and direct polling of the curated employers.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from ..http import get_json, get_text
from ..models import Job

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json"
VANSH_URL = "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/main/.github/scripts/listings.json"
JOBRIGHT_URLS = [
    "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-New-Grad/master/README.md",
    "https://raw.githubusercontent.com/jobright-ai/2026-Data-Analysis-New-Grad/master/README.md",
]
SPEEDY_URL = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md"

MAX_AGE_S = 45 * 86400  # ignore aggregator entries older than 45 days entirely


def _simplify_like(url: str, source: str) -> list[Job]:
    now = time.time()
    out = []
    for row in get_json(url):
        if not row.get("active", True) or not row.get("is_visible", True):
            continue
        posted = row.get("date_posted") or row.get("date_updated")
        if posted and now - posted > MAX_AGE_S:
            continue
        locs = row.get("locations") or []
        out.append(Job(
            company=row.get("company_name", "").strip(),
            title=row.get("title", "").strip(),
            url=row.get("url", ""),
            source=source,
            locations=locs,
            posted_at=int(posted) if posted else None,
            remote=any("remote" in (l or "").lower() for l in locs),
        ))
    return out


def fetch_simplify() -> list[Job]:
    return _simplify_like(SIMPLIFY_URL, "simplify")


def fetch_vansh() -> list[Job]:
    return _simplify_like(VANSH_URL, "vansh")


_JR_ROW = re.compile(
    r"^\|\s*\*{0,2}\[?([^\]|*]+)\]?(?:\([^)]*\))?\*{0,2}\s*"   # company
    r"\|\s*\*{0,2}\[([^\]]+)\]\(([^)]+)\)\*{0,2}\s*"           # [title](link)
    r"\|\s*([^|]*)\|\s*([^|]*)\|\s*([A-Z][a-z]{2} \d{2})\s*\|", re.M)


def _md_date_to_epoch(s: str) -> int | None:
    """'Jul 05' → epoch, assuming the most recent occurrence of that date."""
    try:
        now = datetime.now(timezone.utc)
        d = datetime.strptime(f"{s} {now.year}", "%b %d %Y").replace(tzinfo=timezone.utc)
        if d > now:  # e.g. seeing "Dec 30" in January
            d = d.replace(year=now.year - 1)
        return int(d.timestamp())
    except ValueError:
        return None


_CONTINUATION_GLYPHS = {"↳", "&#8627;", "&#x21B3;"}


def fetch_jobright() -> list[Job]:
    out = []
    for url in JOBRIGHT_URLS:
        md = get_text(url)
        prev_company = ""
        for m in _JR_ROW.finditer(md):
            company, title, link, loc, model, date_s = (g.strip() for g in m.groups())
            if company in _CONTINUATION_GLYPHS:  # continuation row: same employer as above
                company = prev_company
            if not company or company in _CONTINUATION_GLYPHS:
                continue  # unresolvable (e.g. table truncated above a continuation)
            prev_company = company
            posted = _md_date_to_epoch(date_s)
            if posted and time.time() - posted > MAX_AGE_S:
                continue
            out.append(Job(
                company=company, title=title, url=link, source="jobright",
                locations=[loc] if loc else [],
                posted_at=posted,
                remote="remote" in f"{loc} {model}".lower(),
            ))
    return out


_SP_ROW = re.compile(
    r'^\|\s*<a href="[^"]*"><strong>([^<]+)</strong></a>\s*'    # company
    r"\|\s*([^|]+?)\s*"                                          # position
    r"\|\s*([^|]+?)\s*"                                          # location
    r"\|\s*([^|]*?)\s*"                                          # salary
    r'\|\s*<a href="([^"]+)">.*?'                                # posting url
    r"\|\s*(\d+)d\s*\|", re.M)


def fetch_speedyapply() -> list[Job]:
    md = get_text(SPEEDY_URL)
    now = int(time.time())
    out = []
    for m in _SP_ROW.finditer(md):
        company, title, loc, salary, link, age_d = (g.strip() for g in m.groups())
        age = int(age_d)
        if age * 86400 > MAX_AGE_S:
            continue
        out.append(Job(
            company=company, title=title, url=link, source="speedyapply",
            locations=[loc] if loc else [],
            posted_at=now - age * 86400,
            salary=salary,
            remote="remote" in loc.lower(),
        ))
    return out
