"""Conservative resolution of aggregator posting links.

Aggregator pages are useful discovery evidence but are poor application
targets.  This module follows a small, explicit set of aggregator hosts and
promotes a URL only when the page (or its public job-detail response) exposes
an ATS/company posting link.  A failed lookup is still a valid outcome: the
aggregator URL remains the primary fallback and is never discarded.
"""
from __future__ import annotations

import re
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import http
from .config import env
from .identity import canonical_url

# Keep this list deliberately narrow.  Adding a host here changes which URLs
# the crawler is willing to replace, so each addition needs a parser/test.
AGGREGATOR_HOSTS = frozenset({"jobright.ai", "www.jobright.ai"})
_BLOCKED_HOST_PARTS = (
    "linkedin.com", "facebook.com", "instagram.com", "glassdoor.com",
    "crunchbase.com", "twitter.com", "x.com", "github.com",
)
_ATS_HOST_RULES = (
    ("greenhouse.io", "greenhouse"),
    ("lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
    ("myworkdayjobs.com", "workday"),
    ("oraclecloud.com", "oracle_orc"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("icims.com", "icims"),
    ("avature.net", "avature"),
    ("eightfold.ai", "eightfold"),
    ("successfactors.com", "successfactors"),
    ("workable.com", "workable"),
    ("jobvite.com", "jobvite"),
    ("bamboohr.com", "bamboohr"),
)
_JOB_PATH_RE = re.compile(
    r"/(?:jobs?|careers?|positions?|requisitions?|openings?|apply)"
    r"(?:/|[?_-]|$)", re.I,
)
_DIRECT_KEY_RE = re.compile(
    r"(?:apply(?:url|link)?|original(?:url|link)?|job(?:url|link)?|"
    r"posting(?:url|link)?|external(?:url|link)?)",
    re.I,
)
# Jobright can keep serving a perfectly healthy 200 page after the employer
# closes the role.  This is a visible page-level verdict, not an inferred
# age-based expiry, so it is safe to use before considering application links.
# Bump when the liveness probe changes. Existing no-direct caches are then
# rechecked instead of masking a newly supported page-level signal.
JOBRIGHT_PAGE_SIGNAL_VERSION = 3
_JOBRIGHT_CLOSED_RE = re.compile(
    r"\bthis\s+job\s+has\s+closed\b|"
    r"\bjob\s+posting\s+has\s+closed\b|"
    r"\bthis\s+posting\s+has\s+closed\b",
    re.I,
)


def _host(url: str | None) -> str:
    return (urlsplit(str(url or "")).netloc or "").lower().split("@")[-1].split(":")[0]


def is_aggregator_url(url: str | None) -> bool:
    host = _host(url)
    return host in AGGREGATOR_HOSTS or any(host.endswith("." + base) for base in AGGREGATOR_HOSTS)


def ats_for_url(url: str | None) -> str:
    host = _host(url)
    for marker, ats in _ATS_HOST_RULES:
        if host == marker or host.endswith("." + marker):
            return ats
    return ""


def _job_like_path(url: str) -> bool:
    path = urlsplit(url).path or ""
    if not path or path == "/":
        return False
    if not _JOB_PATH_RE.search(path):
        return False
    # A bare /careers or /jobs index is not an application target.  Require a
    # second path component for generic company sites; ATS hosts can use their
    # own routing conventions and are accepted above.
    pieces = [piece for piece in path.split("/") if piece]
    if len(pieces) == 1 and pieces[0].lower() in {
        "job", "jobs", "career", "careers", "position", "positions",
        "opening", "openings", "apply",
    }:
        return False
    return True


def _candidate_url(value: str | None, page_url: str, evidence: str = "") -> str:
    if not value or not isinstance(value, str):
        return ""
    value = unescape(value).replace("\\/", "/").replace("\\u0026", "&").strip()
    if value.startswith(("javascript:", "mailto:", "tel:", "#")):
        return ""
    absolute = urljoin(page_url, value)
    parts = urlsplit(absolute)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    host = _host(absolute)
    if is_aggregator_url(absolute) or any(blocked in host for blocked in _BLOCKED_HOST_PARTS):
        return ""
    if not ats_for_url(absolute) and not _job_like_path(absolute):
        return ""
    # A generic company URL is only acceptable when the page explicitly labels
    # it as an application/job link.  A bare company homepage is not enough.
    if not ats_for_url(absolute) and not _DIRECT_KEY_RE.search(evidence or ""):
        return ""
    return absolute


class _LinkCollector(HTMLParser):
    """Collect link-like attributes without depending on BeautifulSoup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[tuple[str, str]] = []
        self._anchor_stack: list[tuple[str, str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {str(k).lower(): str(v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "a":
            href = ""
            for key in ("href", "data-href", "data-url", "data-apply-url", "data-apply-link"):
                if data.get(key):
                    href = data[key]
                    self._anchor_stack.append((href, "anchor " + key, []))
                    break
            if not href:
                self._anchor_stack.append(("", "anchor", []))
            return
        elif tag in {"link", "meta"}:
            rel = data.get("rel", "")
            prop = data.get("property", "") or data.get("name", "")
            if tag == "link" and data.get("href"):
                self.values.append((data["href"], "link " + rel))
            if tag == "meta" and data.get("content"):
                self.values.append((data["content"], "meta " + prop))

    def handle_data(self, data: str) -> None:
        if self._anchor_stack:
            self._anchor_stack[-1][2].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._anchor_stack:
            href, evidence, text = self._anchor_stack.pop()
            if href:
                label = " ".join("".join(text).split())[:80]
                self.values.append((href, f"{evidence} text {label}".strip()))


def _html_candidates(page_url: str, body: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    parser = _LinkCollector()
    try:
        parser.feed(body[:1_500_000])
    except Exception:
        pass
    found.extend(parser.values)
    # Job detail data is commonly serialized in Next.js JSON rather than an
    # anchor.  The key is part of the evidence, which prevents a companyURL
    # from being mistaken for a direct posting URL.
    for match in re.finditer(
        r"[\"'](?P<key>apply(?:Url|Link)?|original(?:Url|Link)?|job(?:Url|Link)?|"
        r"posting(?:Url|Link)?|external(?:Url|Link)?)[\"']\s*:\s*[\"']"
        r"(?P<value>.*?)[\"']",
        body[:1_500_000], re.I,
    ):
        found.append((match.group("value"), match.group("key")))
    return found


def _json_candidates(page_url: str, payload) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def walk(value, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str) and (key and _DIRECT_KEY_RE.search(key)):
            found.append((value, key))

    walk(payload)
    return found


def _jobright_metadata_is_closed(payload) -> bool:
    """Recognize Jobright's explicit deleted-posting metadata."""
    if isinstance(payload, dict):
        if payload.get("isDeleted") is True:
            return True
        return any(_jobright_metadata_is_closed(child) for child in payload.values())
    if isinstance(payload, list):
        return any(_jobright_metadata_is_closed(child) for child in payload)
    return False


def _jobright_id(url: str) -> str:
    parts = [part for part in urlsplit(url).path.split("/") if part]
    try:
        return parts[parts.index("info") + 1]
    except (ValueError, IndexError):
        return ""


def _canonical_page_url(url: str) -> str:
    """Drop tracking query/fragment data before a liveness retry."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc or not parts.path:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _public_jobright_detail(url: str):
    job_id = _jobright_id(url)
    if not job_id:
        return None
    endpoint = f"https://swan-api.jobright.ai/swan/share/job/{job_id}"
    try:
        response = http.get(endpoint, timeout=15, headers={"Accept": "application/json"})
        if response.status_code >= 400:
            return None
        return response.json()
    except Exception:
        return None


def resolve_link(url: str, now: int | None = None) -> dict:
    """Return an auditable resolution result without dropping the input URL."""
    now = int(now or time.time())
    result = {
        "status": "not_aggregator",
        "checked_at": now,
        "original_url": url,
    }
    if not is_aggregator_url(url):
        return result

    try:
        response = http.get(url, timeout=15, allow_redirects=True)
    except Exception as exc:
        result.update({"status": "error", "error": type(exc).__name__})
        return result

    final_url = str(getattr(response, "url", "") or "")
    candidates: list[tuple[str, str]] = []
    if final_url and canonical_url(final_url) != canonical_url(url):
        candidates.append((final_url, "http redirect"))
    body = str(getattr(response, "text", "") or "")
    result["page_signal_version"] = JOBRIGHT_PAGE_SIGNAL_VERSION
    if _JOBRIGHT_CLOSED_RE.search(unescape(body)):
        result.update({
            "status": "closed",
            "posting_status": "expired",
            "reason": "Jobright page says: This job has closed.",
        })
        if final_url:
            result["final_url"] = final_url
        return result
    # Jobright feed URLs often carry campaign parameters. A runner or CDN can
    # serve different visitor HTML for that variant, so retry the canonical
    # page path before accepting a no-direct result. This is still a bounded
    # same-host request and only runs when the original URL has query/fragment
    # data.
    canonical_page = _canonical_page_url(final_url or url)
    if canonical_page and canonical_page != (final_url or url):
        try:
            canonical_response = http.get(canonical_page, timeout=15, allow_redirects=True)
        except Exception:
            canonical_response = None
        canonical_body = str(getattr(canonical_response, "text", "") or "")
        if _JOBRIGHT_CLOSED_RE.search(unescape(canonical_body)):
            result.update({
                "status": "closed",
                "posting_status": "expired",
                "reason": "Jobright page says: This job has closed.",
                "final_url": str(getattr(canonical_response, "url", "") or canonical_page),
            })
            return result
    candidates.extend(_html_candidates(final_url or url, body))
    # Jobright's visitor HTML intentionally omits the original application URL
    # for some postings.  Its public share endpoint sometimes includes it, so
    # make that a bounded fallback rather than assuming the aggregator page is
    # the only link available.
    if is_aggregator_url(final_url or url):
        payload = _public_jobright_detail(url)
        if payload is not None:
            if _jobright_metadata_is_closed(payload):
                result.update({
                    "status": "closed",
                    "posting_status": "expired",
                    "reason": "Jobright metadata says the job is deleted.",
                })
                return result
            candidates.extend(_json_candidates(url, payload))

    ranked: list[tuple[int, str, str]] = []
    for value, evidence in candidates:
        candidate = _candidate_url(value, final_url or url, evidence)
        if not candidate:
            continue
        score = 0
        if ats_for_url(candidate):
            score += 100
        if _DIRECT_KEY_RE.search(evidence or ""):
            score += 35
        if "redirect" in evidence:
            score += 25
        if _job_like_path(candidate):
            score += 10
        ranked.append((score, candidate, evidence))
    if ranked:
        _, direct_url, evidence = max(ranked, key=lambda item: (item[0], len(item[1])))
        result.update({
            "status": "resolved",
            "resolved_url": direct_url,
            "evidence": evidence[:80],
            "ats": ats_for_url(direct_url),
        })
        return result

    result["status"] = "checked_no_direct"
    if final_url:
        result["final_url"] = final_url
    if getattr(response, "status_code", 0) in (404, 410):
        result["status"] = "not_found"
    return result


def _cached_resolution(existing: dict | None, primary_url: str, now: int) -> dict | None:
    if not existing:
        return None
    cached = existing.get("link_resolution") or {}
    checked = int(cached.get("checked_at") or 0)
    ttl = max(1, int(env("RADAR_LINK_RESOLVE_TTL_DAYS", "30"))) * 86400
    original = cached.get("original_url") or existing.get("url")
    if not checked or now - checked >= ttl or canonical_url(original) != canonical_url(primary_url):
        return None
    # Older no-direct results were cached before the Jobright closed-banner
    # signal existed. Recheck those once so closed roles do not remain active
    # for another full cache TTL. Resolved/not-found results retain their
    # existing conservative cache behavior.
    if (is_aggregator_url(primary_url)
            and cached.get("status") == "checked_no_direct"
            and cached.get("page_signal_version") != JOBRIGHT_PAGE_SIGNAL_VERSION):
        return None
    return cached


def needs_resolution(existing: dict | None, primary_url: str, now: int | None = None) -> bool:
    """Whether a cached result is absent or outside the retry TTL."""
    return _cached_resolution(existing, primary_url, int(now or time.time())) is None


def resolve_job(job, existing: dict | None = None, now: int | None = None) -> dict:
    """Resolve one ``Job`` in place and return its resolution record."""
    now = int(now or time.time())
    primary = str(getattr(job, "url", "") or "")
    if not is_aggregator_url(primary):
        return getattr(job, "link_resolution", {}) or {}
    cached = _cached_resolution(existing, primary, now)
    result = cached or resolve_link(primary, now)
    job.link_resolution = result
    resolved = result.get("resolved_url")
    if resolved and canonical_url(resolved) != canonical_url(primary):
        if primary not in job.alternate_urls:
            job.alternate_urls.append(primary)
        job.url = resolved
        if not job.ats:
            job.ats = result.get("ats") or ats_for_url(resolved)
    return result
