"""Stable identities for records that may be surfaced by several feeds.

The radar ID intentionally includes company, title, and location because those
fields are useful for distinguishing role variants.  A posting URL is a
stronger identity for tracker and ingestion deduplication, though, so this
module provides a conservative URL normalizer that removes only common
tracking parameters.
"""
from __future__ import annotations

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
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
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
