"""Direct ATS polling — the speed layer. These public JSON APIs expose postings
the moment they go live, hours-to-days before aggregators pick them up.

Each fetcher takes a registry entry {name, ats, token, extra} and returns
list[Job]. A probe() variant makes the cheapest possible validity check.
"""
from __future__ import annotations

import html as _html
import re
import time
from datetime import datetime, timezone

from ..http import get_json, post_json
from ..models import Job


def _plain(html_text: str | None) -> str:
    """HTML (possibly entity-escaped, à la Greenhouse `content`) → plain text."""
    if not html_text:
        return ""
    text = _html.unescape(html_text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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
    # content=true costs one bigger response but delivers every posting's
    # text — the description gates and posting analysis (DECISIONS #35)
    # are blind without it
    data = get_json(f"{_gh_base(entry)}/v1/boards/{entry['token']}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "") or ""
        out.append(Job(
            company=entry["name"], title=j.get("title", ""), url=j.get("absolute_url", ""),
            source="greenhouse", ats="greenhouse",
            source_url=f"https://job-boards.greenhouse.io/{entry['token']}",
            locations=[loc] if loc else [],
            posted_at=_iso_epoch(j.get("first_published") or j.get("updated_at")),
            description=_plain(j.get("content"))[:4000],
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
            description=(j.get("descriptionPlain") or _plain(j.get("descriptionHtml")))[:4000],
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
    for q in (queries or ["new grad", "early career", "leadership development",
                          "graduate program", "rotational program", "emerging talent"]):
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


# ---------------- Eightfold (Netflix and others) ----------------

def fetch_eightfold(entry: dict) -> list[Job]:
    host = entry["extra"].get("host") or f"https://{entry['token']}"
    domain = entry["extra"].get("domain") or f"{entry['token']}.com"
    out = []
    for start in (0, 10, 20):
        data = get_json(f"{host}/api/apply/v2/jobs?domain={domain}&num=10&start={start}"
                        "&sort_by=timestamp")
        positions = data.get("positions") or []
        for j in positions:
            loc = j.get("location") or ""
            locs = [loc] + (j.get("locations") or [])
            posted = j.get("t_create") or j.get("t_update")
            out.append(Job(
                company=entry["name"], title=j.get("name", ""),
                url=j.get("canonicalPositionUrl") or f"{host}/careers/job/{j.get('id')}",
                source="eightfold", ats="eightfold",
                locations=[l for l in dict.fromkeys(locs) if l],
                posted_at=int(posted) if posted else None,
                remote="remote" in loc.lower(),
            ))
        if len(positions) < 10:
            break
    return out


# ---------------- iCIMS ----------------

def fetch_icims(entry: dict) -> list[Job]:
    data = get_json(f"https://{entry['token']}.icims.com/jobs/search?ss=1&format=json",
                    headers={"Accept": "application/json"})
    out = []
    for j in (data.get("jobs") or []):
        # iCIMS nests fields under idOnly/portal-specific keys; be permissive
        title = j.get("jobTitle") or j.get("title") or ""
        jid = j.get("jobId") or j.get("id") or ""
        loc = j.get("jobLocation") or j.get("location") or ""
        url = j.get("jobUrl") or (f"https://{entry['token']}.icims.com/jobs/{jid}/job" if jid else "")
        if not title:
            continue
        out.append(Job(
            company=entry["name"], title=title, url=url,
            source="icims", ats="icims",
            locations=[loc] if loc else [],
            posted_at=None,  # iCIMS search JSON rarely exposes post dates
            remote="remote" in str(loc).lower(),
        ))
    return out


# ---------------- Oracle Cloud Recruiting (ORC) ----------------

def fetch_oracle_orc(entry: dict) -> list[Job]:
    host, site = entry["extra"]["host"], entry["extra"].get("site", "CX_1")
    api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand=requisitionList.secondaryLocations"
           f"&finder=findReqs;siteNumber={site},limit=50,sortBy=POSTING_DATES_DESC")
    data = get_json(api, headers={"Accept": "application/json"})
    out = []
    items = (data.get("items") or [{}])[0].get("requisitionList") or []
    for j in items:
        loc = j.get("PrimaryLocation") or ""
        posted = _iso_epoch(j.get("PostedDate"))
        rid = j.get("Id")
        out.append(Job(
            company=entry["name"], title=j.get("Title", ""),
            url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
            source="oracle_orc", ats="oracle_orc",
            locations=[loc] if loc else [],
            posted_at=posted,
            remote="remote" in loc.lower(),
        ))
    return out


# ---------------- Phenom (J&J, Merck, most big pharma) ----------------

def fetch_phenom(entry: dict, queries: list[str] | None = None) -> list[Job]:
    """Phenom sites expose the same search API their own frontend calls:
    POST {host}/widgets with a refineSearch payload. Needs per-site extra:
    {host, refnum, ddo? } — seed-only, validated by probe."""
    from .bigco import BROWSER_HEADERS  # phenom sites often sit behind the same WAFs
    host = entry["extra"]["host"].rstrip("/")
    refnum = entry["extra"]["refnum"]
    out, seen = [], set()
    for q in (queries or ["new grad", "early career", "entry level",
                          "leadership development", "graduate program",
                          "rotational program", "emerging talent"]):
        payload = {
            "lang": "en_us", "deviceType": "desktop", "country": "us",
            "pageName": "search-results", "ddoKey": "refineSearch",
            "sortBy": "Most recent", "subsearch": "", "from": 0, "jobs": True,
            "counts": True, "all_fields": ["category", "country", "state", "city"],
            "size": 20, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
            "pageId": "page20", "siteType": "external", "keywords": q, "global": True,
            "selected_fields": {}, "locationData": {}, "s": "1",
        }
        data = post_json(f"{host}/widgets", payload, headers=BROWSER_HEADERS)
        jobs = ((data.get("refineSearch") or {}).get("data") or {}).get("jobs") or []
        for j in jobs:
            slug = j.get("jobSeqNo") or j.get("reqId") or ""
            if not slug or slug in seen:
                continue
            seen.add(slug)
            loc = j.get("cityStateCountry") or j.get("location") or ""
            out.append(Job(
                company=entry["name"], title=j.get("title", ""),
                url=j.get("applyUrl") or f"{host}/job/{slug}",
                source="phenom", ats="phenom",
                locations=[loc] if loc else [],
                posted_at=_iso_epoch(j.get("postedDate")),
                remote="remote" in str(loc).lower() or bool(j.get("isRemote")),
            ))
    _ = refnum  # part of the seed contract; some tenants require it in payload
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


from .bigco import BIGCO_FETCHERS  # noqa: E402  (pseudo-ATS entries, same contract)

FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
    "eightfold": fetch_eightfold,
    "icims": fetch_icims,
    "oracle_orc": fetch_oracle_orc,
    "phenom": fetch_phenom,
    **BIGCO_FETCHERS,
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
        elif ats == "eightfold":
            host = entry["extra"].get("host") or f"https://{entry['token']}"
            domain = entry["extra"].get("domain") or f"{entry['token']}.com"
            data = get_json(f"{host}/api/apply/v2/jobs?domain={domain}&num=1&start=0")
            if "positions" not in data:
                return False
        elif ats == "icims":
            data = get_json(f"https://{entry['token']}.icims.com/jobs/search?ss=1&format=json",
                            headers={"Accept": "application/json"})
            if "jobs" not in data:
                return False
        elif ats == "oracle_orc":
            host, site = entry["extra"]["host"], entry["extra"].get("site", "CX_1")
            get_json(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                     f"?onlyData=true&finder=findReqs;siteNumber={site},limit=1",
                     headers={"Accept": "application/json"})
        elif ats in {"phenom", "tesla", "amazon", "microsoft", "apple", "google"}:
            # bespoke/hybrid endpoints: the cheapest reliable check IS a fetch
            return bool(FETCHERS[ats](entry))
        else:
            return False
        return True
    except Exception:
        return False
