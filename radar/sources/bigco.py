"""Bespoke career-site APIs for giants that don't run a standard ATS.

These are the JSON endpoints each company's own careers SPA calls — public,
no auth, but undocumented and free to drift. Every fetcher here is registered
as a pseudo-ATS in FETCHERS (via ats.py) so the registry's probe/deactivate
machinery treats endpoint drift as a routine dead-board event, never a crash.

Known gap: Meta (auth-gated GraphQL) — covered via aggregators instead.
"""
from __future__ import annotations

import time

from ..http import get_json, post_json
from ..models import Job

TECH_QUERIES = ["new grad", "early career", "university grad"]
PM_QUERIES = [
    "associate product manager",
    "apm",
    "product manager",
    "technical product manager",
    "product owner",
    "project manager",
    "business analyst",
    "business systems analyst",
    "ux researcher",
    "user experience researcher",
    "ui researcher",
    "solutions architect",
    "product management",
]
QUERIES = TECH_QUERIES + PM_QUERIES

# Several big-co WAFs reject non-browser user agents; identify as a browser
# for these bespoke endpoints only (standard ATS APIs keep the honest UA).
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------- Tesla ----------------

def fetch_tesla(entry: dict) -> list[Job]:
    data = get_json("https://www.tesla.com/cua-api/careers/search",
                    headers=BROWSER_HEADERS)
    listings = (data.get("listings") or data.get("results") or [])
    lookup = data.get("lookup") or {}
    dept_lu = lookup.get("departments") or {}
    loc_lu = lookup.get("locations") or {}
    out = []
    for j in listings:
        dept = str(dept_lu.get(str(j.get("dp")), j.get("dp", "")))
        # keep only engineering/AI/data departments to bound volume
        if dept and not any(k in dept.lower() for k in
                            ("engineer", "software", "ai", "autopilot", "data", "it ")):
            continue
        loc = str(loc_lu.get(str(j.get("l")), j.get("l", "")) or "")
        out.append(Job(
            company="Tesla", title=j.get("t", "") or j.get("title", ""),
            url=f"https://www.tesla.com/careers/search/job/{j.get('id')}",
            source="tesla", ats="tesla",
            locations=[loc] if loc else [],
            posted_at=None,
            remote="remote" in loc.lower(),
        ))
    return out


# ---------------- Amazon ----------------

def fetch_amazon(entry: dict) -> list[Job]:
    out, seen = [], set()
    for q in QUERIES:
        params = {"base_query": q, "offset": 0, "result_limit": 40,
                  "country": "USA", "sort": "recent"}
        # Amazon's technical category filters hide PM-family roles. Keep them
        # for the technical search terms, but let the explicit PM searches
        # query the full US board; normal gates still remove senior/off-field
        # noise and the PM score remains zero.
        if q not in PM_QUERIES:
            params["category[]"] = ["software-development",
                                     "machine-learning-science",
                                     "data-science"]
        data = get_json("https://www.amazon.jobs/en/search.json",
                        params=params, headers=BROWSER_HEADERS)
        for j in data.get("jobs") or []:
            jid = j.get("id_icims") or j.get("id")
            if jid in seen:
                continue
            seen.add(jid)
            loc = j.get("normalized_location") or j.get("location") or ""
            posted = j.get("posted_date")  # e.g. "July 9, 2026"
            ts = None
            if posted:
                try:
                    ts = int(time.mktime(time.strptime(posted, "%B %d, %Y")))
                except ValueError:
                    pass
            out.append(Job(
                company="Amazon", title=j.get("title", ""),
                url="https://www.amazon.jobs" + (j.get("job_path") or ""),
                source="amazon", ats="amazon",
                locations=[loc] if loc else [],
                posted_at=ts,
                remote="virtual" in loc.lower() or "remote" in loc.lower(),
            ))
    return out


# ---------------- Microsoft ----------------

def fetch_microsoft(entry: dict) -> list[Job]:
    out, seen = [], set()
    for q in QUERIES:
        data = get_json("https://gcsservices.careers.microsoft.com/search/api/v1/search",
                        params={"q": q, "lc": "United States", "l": "en_us",
                                "pg": 1, "pgSz": 20, "o": "Recent", "flt": "true"},
                        headers=BROWSER_HEADERS)
        jobs = (((data.get("operationResult") or {}).get("result") or {}).get("jobs")
                or data.get("jobs") or [])
        for j in jobs:
            jid = j.get("jobId")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            props = j.get("properties") or {}
            locs = props.get("locations") or ([j.get("primaryLocation")] if j.get("primaryLocation") else [])
            posted = j.get("postingDate")
            ts = None
            if posted:
                try:
                    from datetime import datetime
                    ts = int(datetime.fromisoformat(posted.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    pass
            out.append(Job(
                company="Microsoft", title=j.get("title", ""),
                url=f"https://jobs.careers.microsoft.com/global/en/job/{jid}",
                source="microsoft", ats="microsoft",
                locations=[l for l in locs if l],
                posted_at=ts,
                remote=any("remote" in str(l).lower() for l in locs),
            ))
    return out


# ---------------- Apple ----------------

def _apple_csrf() -> dict:
    """Apple's role-search POST requires a CSRF token minted by a GET first."""
    from ..http import get
    r = get("https://jobs.apple.com/api/csrfToken", headers=BROWSER_HEADERS)
    token = r.headers.get("X-Apple-CSRF-Token", "") or r.text.strip().strip('"')
    h = dict(BROWSER_HEADERS)
    if token:
        h["X-Apple-CSRF-Token"] = token
    if r.cookies:
        h["Cookie"] = "; ".join(f"{c.name}={c.value}" for c in r.cookies)
    return h


def fetch_apple(entry: dict) -> list[Job]:
    headers = _apple_csrf()
    out, seen = [], set()
    for q in QUERIES:
        data = post_json("https://jobs.apple.com/api/role/search",
                         {"query": q, "filters": {"range": {"standardWeeklyHours": {"start": None, "end": None}}},
                          "page": 1, "locale": "en-us", "sort": "newest"},
                         headers=headers)
        for j in (data.get("searchResults") or []):
            jid = j.get("positionId") or j.get("id")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            locs = [l.get("name", "") for l in (j.get("locations") or [])]
            posted = j.get("postingDate")
            ts = None
            if posted:
                try:
                    ts = int(time.mktime(time.strptime(posted[:10], "%Y-%m-%d")))
                except ValueError:
                    pass
            slug = j.get("transformedPostingTitle") or "role"
            out.append(Job(
                company="Apple", title=j.get("postingTitle", ""),
                url=f"https://jobs.apple.com/en-us/details/{jid}/{slug}",
                source="apple", ats="apple",
                locations=[l for l in locs if l],
                posted_at=ts,
                remote=bool(j.get("homeOffice")),
            ))
    return out


# ---------------- Google ----------------

def fetch_google(entry: dict) -> list[Job]:
    """Best-effort: careers.google.com's SPA API. Allowed to die gracefully."""
    out, seen = [], set()
    for q in [
        "early career software engineer",
        "new grad",
        "associate product manager",
        "product manager early career",
        "technical product manager",
        "product owner",
        "business analyst",
        "business systems analyst",
        "ux researcher",
        "user experience researcher",
        "ui researcher",
        "solutions architect early career",
        "product management",
    ]:
        data = get_json("https://careers.google.com/api/v3/search/",
                        params={"q": q, "location": "United States", "page": 1},
                        headers=BROWSER_HEADERS)
        for j in data.get("jobs") or []:
            jid = j.get("id", "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            locs = [l.get("display", "") for l in (j.get("locations") or [])]
            out.append(Job(
                company="Google", title=j.get("title", ""),
                url=j.get("apply_url") or f"https://careers.google.com/jobs/results/{jid.split('/')[-1]}",
                source="google", ats="google",
                locations=[l for l in locs if l],
                posted_at=None,
                remote=any("remote" in l.lower() for l in locs),
            ))
    return out


BIGCO_FETCHERS = {
    "tesla": fetch_tesla,
    "amazon": fetch_amazon,
    "microsoft": fetch_microsoft,
    "apple": fetch_apple,
    "google": fetch_google,
}
