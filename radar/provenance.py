"""Human-readable provenance for every job discovery path."""
from __future__ import annotations

from urllib.parse import urlsplit

SOURCE_INFO = {
    "simplify": ("SimplifyJobs New-Grad-Positions", "https://github.com/SimplifyJobs/New-Grad-Positions"),
    "vansh": ("Vansh New-Grad-2026", "https://github.com/vanshb03/New-Grad-2026"),
    "jobright": ("Jobright new-grad GitHub board", "https://github.com/jobright-ai/2026-Software-Engineer-New-Grad"),
    "jobright_pm": ("Jobright Product Management new-grad board", "https://github.com/jobright-ai/2026-Product-Management-New-Grad"),
    "speedyapply": ("SpeedyApply college jobs", "https://github.com/speedyapply/2027-SWE-College-Jobs"),
    "zapply": ("Zapply new-grad data science / ML board", "https://github.com/zapplyjobs/New-Grad-Data-Science-Jobs-2027"),
    "zapply_pm": ("Zapply new-grad jobs PM board", "https://github.com/zapplyjobs/New-Grad-Jobs-2027"),
    "simplify_internship": ("Simplify internship board", "https://github.com/SimplifyJobs/Summer2027-Internships"),
    "speedyapply_internship": ("SpeedyApply internship board", "https://github.com/speedyapply/2027-SWE-College-Jobs"),
    "zapply_internship": ("Zapply internship board", "https://github.com/zapplyjobs/Internships-2027"),
    "dreamwork_internship": ("Dreamwork tech internship board", "https://github.com/dreamworkhq/Tech-Internships-2027"),
    "hn": ("Hacker News Who Is Hiring", "https://news.ycombinator.com/item?id=40789211"),
}


def info(source: str, fallback_url: str = "") -> tuple[str, str]:
    """Return (label, link), preferring the actual discovery source."""
    return SOURCE_INFO.get(source, ("Direct company / ATS monitoring", fallback_url))


def _url_label(url: str) -> str:
    host = (urlsplit(url).netloc or "").lower()
    if "jobright.ai" in host:
        return "Jobright"
    if "greenhouse.io" in host:
        return "Greenhouse"
    if "myworkdayjobs.com" in host:
        return "Workday"
    if "lever.co" in host:
        return "Lever"
    if "ashbyhq.com" in host:
        return "Ashby"
    if "oraclecloud.com" in host:
        return "Oracle Careers"
    if "smartrecruiters.com" in host:
        return "SmartRecruiters"
    return host or "alternate source"


def source_links(record: dict) -> list[tuple[str, str]]:
    """Return visible discovery-board links, deduplicated and ordered."""
    links: list[tuple[str, str]] = []
    sources = [record.get("source"), *(record.get("source_variants") or [])]
    source_urls = [record.get("source_url"), *(record.get("source_url_variants") or [])]
    primary_source = record.get("source", "")
    primary_label, primary_default = info(primary_source, record.get("source_url", ""))
    if record.get("source_url") or primary_default:
        links.append((primary_label, record.get("source_url") or primary_default))
    for source in sources[1:]:
        if not source:
            continue
        label, default = info(source, "")
        if default and all(url != default for _, url in links):
            links.append((label, default))
    for url in source_urls:
        if url and all(existing_url != url for _, existing_url in links):
            links.append((_url_label(url), url))
    return links


def alternate_link_label(url: str) -> str:
    """Short label for a secondary posting/application URL."""
    return f"{_url_label(url)} fallback"
