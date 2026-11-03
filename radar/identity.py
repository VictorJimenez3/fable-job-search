"""Stable identities for records that may be surfaced by several feeds.

The radar ID intentionally includes company, title, and location because those
fields are useful for distinguishing role variants.  A posting URL is a
stronger identity for tracker and ingestion deduplication, though, so this
module provides a conservative URL normalizer that removes only common
tracking parameters.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_KEYS = {
    "ref", "referrer", "source", "src", "job_source", "jobsource",
    "lever-source", "lever_source",
}


def canonical_url(url: str | None) -> str:
    """Return a comparison-safe URL without changing its job identity.

    Only HTTP(S) URLs are normalized.  We remove the fragment and conventional
    campaign/referral parameters, sort the remaining query pairs, and trim a
    trailing path slash.  Provider-specific identifiers such as Greenhouse
    job IDs are deliberately retained.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        # urlsplit defers malformed bracket and port validation until these
        # properties are accessed.  Identity normalization must never raise
        # on untrusted feed data.
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc
            or not host or host.startswith(".")
            or any(char.isspace() or char in "/\\?#" for char in host)):
        return ""

    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.casefold()
        if lower.startswith("utm_") or lower in _TRACKING_KEYS:
            continue
        query.append((key, value))
    query.sort()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path,
                       urlencode(query, doseq=True), ""))


def posting_key(url: str | None) -> str:
    """Return a provider-aware posting identity while retaining safe URLs.

    ATS detail pages commonly expose the same requisition at an application
    suffix, a locale/title/location route, or an alternate Greenhouse host.
    Those routes are one posting even though their canonical URLs differ.  We
    normalize only provider-issued requisition identifiers; unknown hosts keep
    their full canonical URL so a URL variant cannot silently become a fuzzy
    title match.
    """
    normalized = canonical_url(url)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    path = parts.path

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        match = re.fullmatch(r"/([^/]+)/jobs/(\d+)(?:/application)?", path, re.I)
        if match:
            return f"greenhouse:{match[1].lower()}:{match[2]}"

    # Lever and Ashby use a board/tenant plus a posting token.  Current feeds
    # usually expose UUIDs, but older and private boards also use slugs; the
    # application suffix is the only route component we intentionally drop.
    if host in {"jobs.ashbyhq.com", "jobs.lever.co", "jobs.eu.lever.co"}:
        match = re.fullmatch(r"/([^/]+)/([^/]+)(?:/(?:application|apply))?", path, re.I)
        if match:
            return f"{host}:{match[1].lower()}:{match[2].lower()}"

    if host.endswith(".myworkdayjobs.com"):
        # Workday changes locale, site path, location, title, and /apply route
        # between feeds while retaining the tenant-scoped requisition suffix.
        match = re.fullmatch(
            r"/(?:[A-Za-z]{2}[-_][A-Za-z]{2}/)?([^/]+)/job/[^/]+/([^/]+?)(?:/apply)?",
            path, re.I,
        )
        if match:
            requisition = re.search(r"_([A-Za-z]*-?\d[\w-]*)$", match[2])
            if requisition:
                return f"workday:{host}:{match[1].lower()}:{requisition[1].lower()}"

    return normalized
