"""GitHub-hosted job aggregators. These are the breadth layer: community- or
company-maintained lists refreshed hourly-to-daily, fetchable as raw files.
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from html import unescape

from ..http import get_json, get_text
from ..models import Job
from ..provenance import info

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
VANSH_URL = "https://raw.githubusercontent.com/vanshb03/New-Grad-2026/dev/.github/scripts/listings.json"
JOBRIGHT_URLS = [
    "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-New-Grad/master/README.md",
    "https://raw.githubusercontent.com/jobright-ai/2026-Data-Analysis-New-Grad/master/README.md",
]
JOBRIGHT_PM_URL = (
    "https://raw.githubusercontent.com/jobright-ai/2026-Product-Management-New-Grad/"
    "master/README.md"
)
SPEEDY_URL = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md"
ZAPPLY_URL = "https://raw.githubusercontent.com/zapplyjobs/New-Grad-Data-Science-Jobs-2027/main/README.md"
ZAPPLY_PM_URL = "https://raw.githubusercontent.com/zapplyjobs/New-Grad-Jobs-2027/main/README.md"
SIMPLIFY_INTERNSHIP_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
SPEEDY_INTERNSHIP_URL = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md"
ZAPPLY_INTERNSHIP_URL = "https://raw.githubusercontent.com/zapplyjobs/Internships-2027/main/README.md"
DREAMWORK_INTERNSHIP_URL = "https://raw.githubusercontent.com/dreamworkhq/Tech-Internships-2027/main/data/listings.json"
APPLYGUY_INTERNSHIP_URL = "https://raw.githubusercontent.com/ApplyGuy/2027-Internships/main/data/internships.json"
APRAMAYAK_OFFCYCLE_URL = "https://raw.githubusercontent.com/aprameyak/2027-tech-jobs/main/OFFCYCLE.md"

# Aggregator ``active`` is the source's current availability signal. Its
# posted/updated timestamp is often stale (especially for evergreen new-grad
# listings), so retain active rows for the dashboard and let the alert policy
# decide whether an old row is email-worthy.
MAX_AGE_S = 365 * 86400


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
            source_url=info(source)[1],
            locations=locs,
            posted_at=int(posted) if posted else None,
            remote=any("remote" in (location or "").lower() for location in locs),
        ))
    return out


def fetch_simplify() -> list[Job]:
    return _simplify_like(SIMPLIFY_URL, "simplify")


def fetch_vansh() -> list[Job]:
    return _simplify_like(VANSH_URL, "vansh")


def fetch_simplify_internship() -> list[Job]:
    """Parse Simplify's structured internship listings without scraping JDs."""
    jobs = _simplify_like(SIMPLIFY_INTERNSHIP_URL, "simplify_internship")
    for job in jobs:
        job.profile = "internship"
    return jobs


_JR_ROW = re.compile(
    r"^\|\s*\*{0,2}\[?([^\]|*]+)\]?(?:\([^)]*\))?\*{0,2}\s*"   # company
    r"\|\s*\*{0,2}\[([^\]]+)\]\(([^)]+)\)\*{0,2}\s*"           # [title](link)
    r"\|\s*([^|]*)\|\s*([^|]*)\|\s*([A-Z][a-z]{2} \d{2})\s*\|", re.M)


def _md_date_to_epoch(s: str) -> int | None:
    """'Jul 05' → epoch, assuming the most recent occurrence of that date."""
    try:
        now = datetime.now(UTC)
        d = datetime.strptime(f"{s} {now.year}", "%b %d %Y").replace(tzinfo=UTC)
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
                source_url=info("jobright")[1],
                locations=[loc] if loc else [],
                posted_at=posted,
                remote="remote" in f"{loc} {model}".lower(),
            ))
    return out


def fetch_jobright_pm() -> list[Job]:
    """Read Jobright's dedicated, seven-day Product Management board.

    Jobright's PM repository contains a few product-adjacent titles (for
    example product demonstrators). Keep the source useful without making the
    scoring lane broader: only the same explicit PM-family title vocabulary
    used by ``role_bucket`` is admitted here.
    """
    md = get_text(JOBRIGHT_PM_URL)
    out, prev_company = [], ""
    for m in _JR_ROW.finditer(md):
        company, title, link, loc, model, date_s = (g.strip() for g in m.groups())
        if company in _CONTINUATION_GLYPHS:
            company = prev_company
        if not company or company in _CONTINUATION_GLYPHS:
            continue
        prev_company = company
        if not _PM_TITLE.search(title):
            continue
        posted = _md_date_to_epoch(date_s)
        if posted and time.time() - posted > MAX_AGE_S:
            continue
        out.append(Job(
            company=company, title=title, url=link, source="jobright_pm",
            source_url=info("jobright_pm")[1],
            locations=[loc] if loc else [], posted_at=posted,
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
            source_url=info("speedyapply")[1],
            locations=[loc] if loc else [],
            posted_at=now - age * 86400,
            salary=salary,
            remote="remote" in loc.lower(),
        ))
    return out


def fetch_speedyapply_internship() -> list[Job]:
    """Read the internship table embedded in SpeedyApply's public README."""
    md = get_text(SPEEDY_INTERNSHIP_URL)
    now = int(time.time())
    out = []
    for m in _SP_ROW.finditer(md):
        company, title, loc, salary, link, age_d = (g.strip() for g in m.groups())
        age = int(age_d)
        if age * 86400 > MAX_AGE_S:
            continue
        if not re.search(r"\b(intern(ship)?|co-?op|student)\b", title, re.I):
            # The root README also contains a new-grad table. Keep this
            # adapter explicitly internship-only so the lanes cannot bleed.
            continue
        out.append(Job(
            company=company, title=title, url=link, source="speedyapply_internship",
            source_url=info("speedyapply_internship")[1],
            locations=[loc] if loc else [], posted_at=now - age * 86400,
            salary=salary, remote="remote" in loc.lower(), profile="internship",
        ))
    return out


_ZAPPLY_ROW = re.compile(
    r"^\|\s*\*{0,2}([^|*]+?)\*{0,2}\s*\|\s*\*{0,2}([^|*]+?)\*{0,2}\s*\|"
    r"\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|[^|]*\|\s*[^\n]*?\((https?://[^)]+)\)", re.M)

_ZAPPLY_GENERAL_ROW = re.compile(
    r"^\|\s*\*{0,2}([^|*]+?)\*{0,2}\s*\|"
    r"\s*\*{0,2}([^|*]+?)\*{0,2}\s*\|\s*([^|]+?)\s*\|"
    r".*?\((https?://[^)]+)\)", re.M)
_PM_TITLE = re.compile(
    r"\b(?:apm|associate\s+product\s+manager|technical\s+product\s+manager|"
    r"product\s+(?:manager|owner|management)|project\s+manager|"
    r"business(?:\s+systems)?\s+analyst|"
    r"(?:ux\s*/\s*ui|ux|ui|user\s+experience|user\s+interface)\s+(?:researcher|research)|"
    r"solutions?\s+architect(?:ure)?)\b", re.I)


def fetch_zapply() -> list[Job]:
    """Read Zapply's public DS/ML GitHub table; gates remove non-new-grad noise."""
    md = get_text(ZAPPLY_URL)
    out = []
    for company, title, loc, _posted, link in _ZAPPLY_ROW.findall(md):
        if company.strip().strip("-") == "" or title.strip().strip("-") == "":
            continue
        out.append(Job(company=company.strip(), title=title.strip(), url=link.strip(),
                       source="zapply", source_url=info("zapply")[1],
                       locations=[loc.strip()] if loc.strip() else [],
                       remote="remote" in loc.lower()))
    return out


def fetch_zapply_pm() -> list[Job]:
    """Read Zapply's broad GitHub board, retaining only requested PM titles.

    The board also contains experienced and adjacent roles, so it is not a
    trusted new-grad source. The PM gate keeps these records visible without
    making them alertable or emailing them.
    """
    md = get_text(ZAPPLY_PM_URL)
    out, prev_company = [], ""
    for company, title, loc, link in _ZAPPLY_GENERAL_ROW.findall(md):
        company = company.strip()
        title = re.sub(r"<[^>]+>", "", title).strip()
        continuation = company in _CONTINUATION_GLYPHS
        if continuation:
            company = prev_company
        if company:
            prev_company = company
        if not company or not title or not _PM_TITLE.search(title):
            continue
        out.append(Job(company=company, title=title, url=link.strip(), source="zapply_pm",
                       source_url=info("zapply_pm")[1],
                       locations=[loc.strip()] if loc.strip() else [],
                       remote="remote" in loc.lower()))
    return out


def fetch_zapply_internship() -> list[Job]:
    """Read Zapply's broad internship table; the internship gates do the rest."""
    md = get_text(ZAPPLY_INTERNSHIP_URL)
    out = []
    for company, title, loc, link in _ZAPPLY_GENERAL_ROW.findall(md):
        company = company.strip()
        title = re.sub(r"<[^>]+>", "", title).strip()
        if company.strip("-") == "" or not title or not link:
            continue
        out.append(Job(
            company=company, title=title, url=link.strip(), source="zapply_internship",
            source_url=info("zapply_internship")[1],
            locations=[loc.strip()] if loc.strip() else [],
            remote="remote" in loc.lower(), profile="internship",
        ))
    return out


def _iso_epoch(value) -> int | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def fetch_dreamwork_internship() -> list[Job]:
    """Parse Dreamwork's machine-readable verified internship listings."""
    payload = get_json(DREAMWORK_INTERNSHIP_URL)
    rows = payload if isinstance(payload, list) else payload.get("listings", [])
    out = []
    for row in rows:
        company = str(row.get("company") or row.get("companyName") or "").strip()
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or row.get("applyUrl") or "").strip()
        if not company or not title or not url:
            continue
        location = row.get("location") or row.get("locations") or ""
        if isinstance(location, list):
            locations = [str(x).strip() for x in location if str(x).strip()]
        else:
            locations = [str(location).strip()] if str(location).strip() else []
        salary = ""
        if row.get("salaryMin") or row.get("salaryMax"):
            salary = f"${row.get('salaryMin', '?')}–${row.get('salaryMax', '?')}"
        out.append(Job(
            company=company, title=title, url=url, source="dreamwork_internship",
            source_url=info("dreamwork_internship")[1], locations=locations,
            posted_at=_iso_epoch(row.get("postedAt") or row.get("firstIndexedAt")),
            salary=salary, remote="remote" in str(row.get("remoteType") or "").lower(),
            profile="internship",
        ))
    return out


def fetch_applyguy_internship() -> list[Job]:
    """Read ApplyGuy's structured, continuously refreshed internship list."""
    payload = get_json(APPLYGUY_INTERNSHIP_URL)
    rows = payload if isinstance(payload, list) else payload.get("jobs", [])
    out = []
    for row in rows:
        if not row.get("active", True):
            continue
        company = str(row.get("company") or row.get("companyName") or "").strip()
        title = str(row.get("title") or "").strip()
        listing = str(row.get("listingUrl") or row.get("listing_url") or "").strip()
        alternate = str(row.get("url") or row.get("applyUrl") or "").strip()
        url = listing or alternate
        if not company or not title or not url.startswith(("https://", "http://")):
            continue

        posted = _iso_epoch(row.get("posted") or row.get("postedAt") or row.get("posted_at"))
        if posted and time.time() - posted > MAX_AGE_S:
            continue
        raw_locations = row.get("locations") or row.get("location") or []
        locations = ([str(value).strip() for value in raw_locations if str(value).strip()]
                     if isinstance(raw_locations, list)
                     else [str(raw_locations).strip()] if str(raw_locations).strip() else [])
        salary = str(row.get("salary") or row.get("compensation") or "").strip()
        if not salary and (row.get("salaryMin") or row.get("salaryMax")):
            salary = f"${row.get('salaryMin', '?')}–${row.get('salaryMax', '?')}"
        season = str(row.get("season") or row.get("term") or "").strip()
        out.append(Job(
            company=company, title=title, url=url, source="applyguy_internship",
            source_url=info("applyguy_internship")[1], locations=locations,
            posted_at=posted,
            salary=salary,
            remote=bool(row.get("remote")) or "remote" in " ".join(locations).lower(),
            profile="internship",
            internship_eligibility={"source_signal": True, "source_term": season},
            alternate_urls=[alternate] if alternate and alternate != url else [],
        ))
    return out


def _markdown_cell_text(value: str) -> str:
    value = unescape(value or "").strip()
    value = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[*`_]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_aprameyak_offcycle() -> list[Job]:
    """Parse direct application links in the community off-cycle job table."""
    md = get_text(APRAMAYAK_OFFCYCLE_URL)
    out, previous_company = [], ""
    for line in md.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 6 or all(re.fullmatch(r"[:\-\s]+", cell or "-") for cell in cells):
            continue
        company = _markdown_cell_text(cells[0])
        title = _markdown_cell_text(cells[1])
        location = _markdown_cell_text(cells[2])
        season = _markdown_cell_text(cells[3])
        if company in _CONTINUATION_GLYPHS:
            company = previous_company
        if company:
            previous_company = company
        if not company or not title:
            continue

        application_cell = cells[5]
        links = re.findall(r"\[[^\]]*\]\((https?://[^)]+)\)", application_cell)
        if not links:
            links = re.findall(r'<a[^>]+href=["\'](https?://[^"\']+)', application_cell, re.I)
        if not links:
            links = re.findall(r"https?://[^\s|)>]+", application_cell)
        url = next((link.rstrip(".,") for link in links
                    if link.startswith(("https://", "http://"))), "")
        if not url:
            continue
        date_text = cells[6] if len(cells) > 6 else ""
        date_match = re.search(r"([A-Z][a-z]{2}\s+\d{1,2})", date_text)
        posted = _md_date_to_epoch(date_match.group(1)) if date_match else None
        if posted and time.time() - posted > MAX_AGE_S:
            continue
        locations = [location] if location and location.lower() not in {"n/a", "unknown", "-"} else []
        out.append(Job(
            company=company, title=title, url=url, source="aprameyak_offcycle",
            source_url=info("aprameyak_offcycle")[1], locations=locations,
            posted_at=posted, remote="remote" in location.lower(), profile="internship",
            internship_eligibility={"source_signal": True, "source_term": season},
        ))
    return out
