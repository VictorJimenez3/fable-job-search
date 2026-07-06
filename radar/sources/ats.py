"""Direct ATS polling — the speed layer. These public JSON APIs expose postings
the moment they go live, hours-to-days before aggregators pick them up.

Each fetcher takes a registry entry {name, ats, token, extra} and returns
list[Job]. A probe() variant makes the cheapest possible validity check.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from ..http import get_json, post_json
from ..models import Job


def _iso_epoch(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


# ---------------- Greenhouse ----------------

def _gh_base(entry: dict) -> str:
    eu = (entry.get("extra") or {}).get("eu")
    return "https://boards-api.eu.greenhouse.io" if eu else "https://boards-api.greenhouse.io"


def fetch_greenhouse(entry: dict) -> list[Job]:
    data = get_json(f"{_gh_base(entry)}/v1/boards/{entry['token']}/jobs?content=false")
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "") or ""
        out.append(Job(
            company=entry["name"], title=j.get("title", ""), url=j.get("absolute_url", ""),
            source="greenhouse", ats="greenhouse",
            locations=[loc] if loc else [],
            posted_at=_iso_epoch(j.get("first_published") or j.get("updated_at")),
            remote="remote" in loc.lower(),
        ))
    return out


# ---------------- Lever ----------------

def fetch_lever(entry: dict) -> list[Job]:
    data = get_json(f"https://api.lever.co/v0/postings/{entry['token']}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        if (cats.get("commitment") or "").lower().startswith(("intern", "part")):
            continue
        loc = cats.get("location") or ""
        all_locs = [loc] + (j.get("additionalPlainLocations") or [])
        out.append(Job(
            company=entry["name"], title=j.get("text", ""), url=j.get("hostedUrl", ""),
            source="lever", ats="lever",
            locations=[l for l in all_locs if l],
            posted_at=int(j["createdAt"] / 1000) if j.get("createdAt") else None,
            description=(j.get("descriptionPlain") or "")[:4000],
            remote=(j.get("workplaceType") or "").lower() == "remote" or "remote" in loc.lower(),
        ))
    return out


# ---------------- Ashby ----------------

def fetch_ashby(entry: dict) -> list[Job]:
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{entry['token']}")
    out = []
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        if (j.get("employmentType") or "").lower() in {"intern", "internship", "parttime", "contract"}:
            continue
        locs = [j.get("location") or ""]
        locs += [s.get("location", "") for s in (j.get("secondaryLocations") or [])]
        locs = [l for l in locs if l]
        out.append(Job(
            company=entry["name"], title=j.get("title", ""),
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            source="ashby", ats="ashby",
            locations=locs,
            posted_at=_iso_epoch(j.get("publishedAt")),
            remote=bool(j.get("isRemote")) or any("remote" in l.lower() for l in locs),
        ))
    return out


# ---------------- Workday ----------------

_REL = re.compile(r"posted (today|yesterday|(\d+)\+? days ago)", re.I)


def _workday_posted(s: str | None) -> int | None:
    if not s:
        return None
    m = _REL.search(s)
    if not m:
        return None
    now = int(time.time())
    if m.group(1).lower() == "today":
        return now
    if m.group(1).lower() == "yesterday":
        return now - 86400
    return now - int(m.group(2)) * 86400


def fetch_workday(entry: dict, queries: list[str] | None = None) -> list[Job]:
    tenant, host, site = entry["token"], entry["extra"]["host"], entry["extra"]["site"]
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    seen, out = set(), []
    for q in (queries or ["new grad", "early career"]):
        for offset in (0, 20, 40):
            data = post_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q})
            postings = data.get("jobPostings") or []
            for j in postings:
                path = j.get("externalPath", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                loc = j.get("locationsText", "") or ""
                out.append(Job(
                    company=entry["name"], title=j.get("title", ""),
                    url=f"{base}/en-US/{site}{path}" if not path.startswith("http") else path,
                    source="workday", ats="workday",
                    locations=[loc] if loc else [],
                    posted_at=_workday_posted(j.get("postedOn")),
                    remote="remote" in loc.lower(),
                ))
            if len(postings) < 20:
                break
    return out


# ---------------- SmartRecruiters ----------------

def fetch_smartrecruiters(entry: dict) -> list[Job]:
    data = get_json(f"https://api.smartrecruiters.com/v1/companies/{entry['token']}/postings?limit=100")
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        country = (loc.get("country") or "").lower()
        if country and country not in {"us", "usa", "united states"} and not loc.get("remote"):
            continue
        loc_s = ", ".join(x for x in [loc.get("city"), loc.get("region")] if x)
        out.append(Job(
            company=entry["name"], title=j.get("name", ""),
            url=f"https://jobs.smartrecruiters.com/{entry['token']}/{j.get('id')}",
            source="smartrecruiters", ats="smartrecruiters",
            locations=[loc_s] if loc_s else [],
            posted_at=_iso_epoch(j.get("releasedDate")),
            remote=bool(loc.get("remote")),
        ))
    return out


# ---------------- Recruitee ----------------

def fetch_recruitee(entry: dict) -> list[Job]:
    data = get_json(f"https://{entry['token']}.recruitee.com/api/offers/")
    out = []
    for j in data.get("offers", []):
        loc = j.get("location") or ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        out.append(Job(
            company=entry["name"], title=j.get("title", ""),
            url=j.get("careers_url", ""),
            source="recruitee", ats="recruitee",
            locations=[loc] if loc else [],
            posted_at=_iso_epoch(j.get("created_at")),
            remote="remote" in (loc or "").lower() or bool(j.get("remote")),
        ))
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
}


def probe(entry: dict) -> bool:
    """Cheapest validity check for a registry entry. True = board exists."""
    try:
        ats = entry["ats"]
        if ats == "greenhouse":
            get_json(f"{_gh_base(entry)}/v1/boards/{entry['token']}/jobs?content=false")
        elif ats == "lever":
            get_json(f"https://api.lever.co/v0/postings/{entry['token']}?mode=json&limit=1")
        elif ats == "ashby":
            data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{entry['token']}")
            if "jobs" not in data:
                return False
        elif ats == "workday":
            tenant, host, site = entry["token"], entry["extra"]["host"], entry["extra"]["site"]
            post_json(f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                      {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        elif ats == "smartrecruiters":
            get_json(f"https://api.smartrecruiters.com/v1/companies/{entry['token']}/postings?limit=1")
        elif ats == "recruitee":
            get_json(f"https://{entry['token']}.recruitee.com/api/offers/")
        else:
            return False
        return True
    except Exception:
        return False
