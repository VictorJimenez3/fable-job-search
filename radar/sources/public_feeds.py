"""Bounded, anonymous public job feeds.

These adapters are intentionally discovery-only.  Headcount Labs and YubHub
provide public snapshots with direct employer URLs; Zero2Sudo's public Notion
table is a curated social lead list that remains source-only because it does
not provide trustworthy posting dates or liveness evidence.

The fetch functions use :mod:`radar.http`, whose redirect and DNS checks block
private-network SSRF.  Callers should still validate employer URLs through the
normal posting/liveness path before alerting.
"""
from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

import yaml

from ..config import DATA_DIR
from ..http import get_json, get_text, post_json
from ..identity import canonical_url, posting_key
from ..models import Job

CONFIG_PATH = DATA_DIR / "public_job_sources.yaml"
NOTION_PAGE_URL = (
    "https://zero2sudo.notion.site/"
    "SWE-Intern-Learning-Journal-by-zero2sudo-4b5bf5492a86418c8dc701bad692df99"
)
NOTION_ENDPOINT = "https://zero2sudo.notion.site/api/v3/loadCachedPageChunkV2"
NOTION_PAGE_ID = "4b5bf549-2a86-418c-8dc7-01bad692df99"
YUBHUB_ATTRIBUTION = "Data by YubHub (https://yubhub.co)"

_LINK_RE = re.compile(r"href=[\"'](https?://[^\"']+)[\"']", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_UPDATED_RE = re.compile(
    r"\bupdated\s+(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+20\d{2})\b", re.I)
_NOTION_SKIP_LABELS = {"event", "contest", "sign up form", "sign-up form"}
_NOTION_HEADER_LABELS = {
    "job posting", "event", "contest", "sign up form", "sign-up form",
    "link", "item", "quick notes",
}


def _clean_text(value: object) -> str:
    value = html.unescape(str(value or ""))
    value = _TAG_RE.sub(" ", value)
    value = value.replace("\xa0", " ")
    return _SPACE_RE.sub(" ", value).strip()


def _safe_public_http_url(value: object) -> str:
    """Return a syntactically safe public URL; network safety is in radar.http."""
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return ""
    return parsed.geturl()


def _epoch(value: object) -> int | None:
    """Parse only an explicit source timestamp; never infer from fetch time."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _headcount_anchor(markdown: str) -> date | None:
    match = _UPDATED_RE.search(markdown or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group("date"), "%B %d, %Y").date()
    except ValueError:
        try:
            return datetime.strptime(match.group("date"), "%b %d, %Y").date()
        except ValueError:
            return None


def _headcount_posted(value: object, anchor: date | None) -> int | None:
    direct = _epoch(value)
    if direct is not None:
        return direct
    text = _clean_text(value)
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=UTC).timestamp())
        except ValueError:
            pass
    if anchor is None:
        return None
    for fmt in ("%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(text, fmt).date().replace(year=anchor.year)
        except ValueError:
            continue
        # A January README can contain late-December postings from the prior
        # year; otherwise the table's month/day is in the update year.
        if parsed > anchor:
            parsed = parsed.replace(year=anchor.year - 1)
        return int(datetime.combine(parsed, datetime.min.time(), tzinfo=UTC).timestamp())
    return None


def _location_values(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            text = _clean_text(item)
            if text and text not in out:
                out.append(text)
            continue
        address = item.get("address") if isinstance(item.get("address"), dict) else item
        bits = [
            address.get("addressLocality"), address.get("addressRegion"),
            address.get("addressCountry"),
        ]
        text = ", ".join(_clean_text(bit) for bit in bits if _clean_text(bit))
        if text and text not in out:
            out.append(text)
    return out


def _dedupe(jobs: list[Job], max_jobs: int) -> list[Job]:
    limit = max(0, int(max_jobs))
    if limit == 0:
        return []
    seen: set[str] = set()
    out: list[Job] = []
    for job in jobs:
        key = posting_key(job.url) or canonical_url(job.url)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(job)
        if len(out) >= limit:
            break
    return out


def parse_yubhub_payload(
    payload: object,
    *,
    source: str = "yubhub_graduate_engineering",
    source_url: str = "",
    max_jobs: int = 100,
) -> list[Job]:
    """Parse YubHub's Schema.org JobPosting JSON without making dates up."""
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("YubHub payload must contain a jobs list")
    out: list[Job] = []
    for row in payload["jobs"]:
        if not isinstance(row, dict):
            continue
        url = _safe_public_http_url(row.get("url") or row.get("applicationUrl"))
        organization = row.get("hiringOrganization") or {}
        if isinstance(organization, list):
            organization = organization[0] if organization else {}
        company = _clean_text(organization.get("name") if isinstance(organization, dict) else "")
        title = _clean_text(row.get("title"))
        if not url or not company or not title:
            continue
        locations = _location_values(row.get("jobLocation"))
        workplace = _clean_text(row.get("jobLocationType"))
        remote = "remote" in workplace.casefold() or workplace.casefold() == "telecommute"
        salary = ""
        base_salary = row.get("baseSalary")
        if isinstance(base_salary, dict):
            value = base_salary.get("value")
            if isinstance(value, dict):
                low, high = value.get("minValue"), value.get("maxValue")
                currency = _clean_text(base_salary.get("currency"))
                if low is not None or high is not None:
                    salary = f"{low or '?'}–{high or '?'} {currency}".strip()
        out.append(Job(
            company=company,
            title=title,
            url=url,
            source=source,
            source_url=source_url,
            source_board=f"public:{source}",
            locations=locations,
            posted_at=_epoch(row.get("datePosted") or row.get("datePublished")),
            description=_clean_text(row.get("description"))[:4000],
            salary=salary,
            remote=remote,
        ))
    return _dedupe(out, max_jobs)


def _markdown_section(markdown: str, section: str | list[str] | tuple[str, ...]) -> list[str]:
    lines = markdown.splitlines()
    wanted = {str(value).casefold().strip() for value in section} if isinstance(
        section, (list, tuple, set)
    ) else {str(section).casefold().strip()}
    active = False
    out: list[str] = []
    for line in lines:
        heading = _HEADING_RE.match(line.strip())
        if heading:
            active = heading.group(1).casefold().strip() in wanted
            continue
        if active:
            out.append(line)
    return out


def _markdown_cell(value: str) -> str:
    return _clean_text(value).strip("* ")


def parse_headcount_markdown(
    markdown: str,
    *,
    section: str | list[str] = "Software engineering",
    source: str = "headcount_engineering",
    source_url: str = "",
    max_jobs: int = 100,
) -> list[Job]:
    """Parse a generated Headcount Labs table, retaining only apply URLs."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("Headcount payload must be non-empty markdown")
    wanted = {str(value).casefold().strip() for value in section} if isinstance(
        section, (list, tuple, set)
    ) else {str(section).casefold().strip()}
    if not any(
        heading.group(1).casefold().strip() in wanted
        for line in markdown.splitlines()
        if (heading := _HEADING_RE.match(line.strip()))
    ):
        raise ValueError(f"Headcount section missing: {section}")
    out: list[Job] = []
    source_only = "internship" in str(source).casefold()
    anchor = _headcount_anchor(markdown)
    for line in _markdown_section(markdown, section):
        if not line.lstrip().startswith("|"):
            continue
        cells = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(cells) < 7 or all(set(_markdown_cell(cell)) <= {"-", ":", " "} for cell in cells):
            continue
        company, title, location, remote_text, salary = (_markdown_cell(cell) for cell in cells[:5])
        posted_text = _markdown_cell(cells[5]) if len(cells) >= 6 else ""
        links = [_safe_public_http_url(url) for url in _LINK_RE.findall(line)]
        links = [url for url in links if url]
        apply_url = next((url for url in links if "headcountlabs.com/catalog/" not in url), "")
        if not company or not title or not apply_url:
            continue
        locations = [_clean_text(location)] if _clean_text(location) else []
        out.append(Job(
            company=company,
            title=title,
            url=apply_url,
            source=source,
            source_url=source_url,
            source_board=f"public:{source}",
            locations=locations,
            posted_at=_headcount_posted(posted_text, anchor),
            salary=salary,
            remote="remote" in remote_text.casefold(),
            link_resolution=(
                {
                    "status": "source_only",
                    "reason": "public Headcount discovery; require employer posting confirmation",
                    "source_note": "Headcount Labs public dataset; employer URL retained for verification",
                }
                if source_only else {}
            ),
        ))
    return _dedupe(out, max_jobs)


def _notion_rich_text(value: object) -> tuple[list[str], list[str]]:
    texts: list[str] = []
    links: list[str] = []
    if isinstance(value, str):
        return [value], []
    if not isinstance(value, list):
        return texts, links
    if len(value) >= 2 and value[0] == "a" and isinstance(value[1], str):
        return [], [value[1]]
    for item in value:
        child_text, child_links = _notion_rich_text(item)
        texts.extend(child_text)
        links.extend(child_links)
    return texts, links


def parse_zero2sudo_notion(payload: object, *, max_jobs: int = 25) -> list[Job]:
    """Parse public Notion table rows as non-alertable source-only leads."""
    if not isinstance(payload, dict) or not isinstance(payload.get("recordMap"), dict):
        raise ValueError("Zero2Sudo Notion payload is missing recordMap")
    blocks = payload["recordMap"].get("block")
    if not isinstance(blocks, dict):
        raise ValueError("Zero2Sudo Notion payload is missing recordMap.block")
    out: list[Job] = []
    for record in blocks.values():
        value = record.get("value", {}).get("value", {}) if isinstance(record, dict) else {}
        if not isinstance(value, dict) or value.get("type") != "table_row":
            continue
        properties = value.get("properties")
        if not isinstance(properties, dict):
            continue
        cells: list[str] = []
        links: list[str] = []
        for cell in properties.values():
            cell_text, cell_links = _notion_rich_text(cell)
            cells.append(_clean_text(" ".join(cell_text)))
            links.extend(cell_links)
        labels = {cell.casefold() for cell in cells if cell.casefold() in _NOTION_HEADER_LABELS}
        if labels and labels.issubset(_NOTION_SKIP_LABELS):
            continue
        candidate_text = [cell for cell in cells if cell and cell.casefold() not in _NOTION_HEADER_LABELS]
        title_candidates = [cell for cell in candidate_text if "—" in cell or "–" in cell]
        title = min(title_candidates or candidate_text, key=len, default="")
        url = next((_safe_public_http_url(link) for link in links if _safe_public_http_url(link)), "")
        if not title or not url:
            continue
        company, job_title = title, title
        match = re.split(r"\s+[—–]\s+", title, maxsplit=1)
        if len(match) == 2:
            company, job_title = (part.strip() for part in match)
        notes = max((cell for cell in candidate_text if cell != title), key=len, default="")
        out.append(Job(
            company=company,
            title=job_title,
            url=url,
            source="zero2sudo_notion",
            source_url=NOTION_PAGE_URL,
            source_board="public:zero2sudo_notion:source_only",
            # Curator notes are never an employer-authored JD. Keep them in
            # source metadata for review while leaving Job.description empty
            # so scoring/posting analysis cannot treat them as authoritative.
            description="",
            link_resolution={
                "status": "source_only",
                "reason": "curated social lead; require employer posting, date, and liveness evidence",
                "source_note": notes[:1000],
            },
        ))
    return _dedupe(out, max_jobs)


def _sources() -> dict:
    try:
        with CONFIG_PATH.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data.get("sources", {}) if isinstance(data.get("sources"), dict) else {}


def _source(name: str) -> dict:
    value = _sources().get(name, {})
    return value if isinstance(value, dict) else {}


def fetch_headcount_engineering() -> list[Job]:
    return _fetch_headcount("headcount_engineering")


def fetch_headcount_data_ai() -> list[Job]:
    return _fetch_headcount("headcount_data_ai")


def fetch_headcount_engineering_internships() -> list[Job]:
    """Fetch Headcount's public engineering-internship discovery list."""
    return _fetch_headcount("headcount_engineering_internships")


def fetch_headcount_data_ai_internships() -> list[Job]:
    """Fetch Headcount's public data/AI internship discovery list."""
    return _fetch_headcount("headcount_data_ai_internships")


def _fetch_headcount(name: str) -> list[Job]:
    source = _source(name)
    if source.get("enabled") is False:
        return []
    jobs = parse_headcount_markdown(
        get_text(str(source.get("url", ""))),
        section=source.get("sections") or str(source.get("section", "Software engineering")),
        source=str(source.get("source", name)),
        source_url=str(source.get("url", "")),
        max_jobs=int(source.get("max_jobs", 100)),
    )
    attribution = _clean_text(source.get("attribution"))
    if attribution:
        for job in jobs:
            job.source_board = f"{job.source_board}; {attribution}"
    return jobs


def fetch_yubhub_graduate_engineering() -> list[Job]:
    source = _source("yubhub_graduate_engineering")
    if source.get("enabled") is False:
        return []
    jobs = parse_yubhub_payload(
        get_json(str(source.get("url", ""))),
        source=str(source.get("source", "yubhub_graduate_engineering")),
        source_url=str(source.get("url", "")),
        max_jobs=int(source.get("max_jobs", 100)),
    )
    attribution = str(source.get("attribution") or YUBHUB_ATTRIBUTION)
    for job in jobs:
        job.source_board = f"{job.source_board}; {attribution}"
    return jobs


def fetch_zero2sudo_notion(*, enabled: bool | None = None) -> list[Job]:
    source = _source("zero2sudo_notion")
    active = bool(source.get("enabled", False)) if enabled is None else bool(enabled)
    if not active:
        return []
    payload = post_json(
        str(source.get("url") or NOTION_ENDPOINT),
        {
            "pageId": str(source.get("page_id") or NOTION_PAGE_ID),
            "limit": 100,
            "cursor": {"stack": []},
            "chunkNumber": 0,
            "verticalColumns": False,
        },
    )
    return parse_zero2sudo_notion(payload, max_jobs=int(source.get("max_jobs", 25)))


PUBLIC_FETCHERS = {
    "headcount_engineering": fetch_headcount_engineering,
    "headcount_data_ai": fetch_headcount_data_ai,
    "yubhub_graduate_engineering": fetch_yubhub_graduate_engineering,
    "zero2sudo_notion": fetch_zero2sudo_notion,
}

PUBLIC_INTERNSHIP_FETCHERS = {
    "headcount_engineering_internships": fetch_headcount_engineering_internships,
    "headcount_data_ai_internships": fetch_headcount_data_ai_internships,
}
