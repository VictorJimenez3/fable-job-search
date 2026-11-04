"""Bounded Instagram discovery through the user's OpenCLI browser session.

This module deliberately separates social observation from job evidence.  The
collector stores a small, allowlisted post ledger in ``state/social.json``;
the normal crawler can turn only conservative, role-shaped career links into
source-only :class:`~radar.models.Job` leads.  No Instagram cookie, session
token, or private post is ever written to the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from . import state as radar_state
from .config import DATA_DIR, STATE_DIR
from .identity import canonical_url, posting_key
from .models import Job

CONFIG_PATH = DATA_DIR / "social_accounts.yaml"
STATE_PATH = STATE_DIR / "social.json"
STATE_SCHEMA_VERSION = 1
DEFAULT_MAX_POSTS = 12
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_FAILURE_COOLDOWN_SECONDS = 3600
DEFAULT_MAX_STORED_POSTS = 200
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_RUNTIME_SECONDS = 120
MAX_CAPTION_CHARS = 2400
MAX_STORED_CAREER_EXCERPT = 1000
MAX_ERROR_CHARS = 300
MAX_POSTS_PER_ACCOUNT = 50
MAX_ACCOUNTS = 30

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,64}$")
_POST_PATH_RE = re.compile(r"/(?P<kind>p|reel|tv)/(?P<post_id>[A-Za-z0-9_-]+)", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"'`()\[\]{}]+|www\.[^\s<>\"'`()\[\]{}]+", re.I)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_ROLE_RE = re.compile(
    r"\b(?:intern(?:ship)?|co[- ]?op|new[ -]?grad|graduate|software|engineering|"
    r"developer|data scientist|data science|machine learning|artificial intelligence|"
    r"research(?:er)?|analyst|product manager|designer|technical program)\b",
    re.I,
)
_JOB_PATH_RE = re.compile(r"/(?:jobs?|careers?|career|work-with-us|apply)(?:[/_?=-]|$)", re.I)
_TRACKING_QUERY_PREFIXES = ("utm_", "igshid", "fbclid", "mc_")
_SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "code",
    "email", "key", "password", "secret", "session", "token", "user",
    "username", "verification", "verify",
}
_KNOWN_JOB_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "smartrecruiters.com", "workable.com", "jobvite.com", "recruitee.com",
    "successfactors.com", "icims.com", "breezy.hr", "rippling.com",
)
_LOGGER = logging.getLogger(__name__)


class SocialFetchError(RuntimeError):
    """A bounded OpenCLI account read failed with an operational status."""

    def __init__(self, status: str, message: str, retry_seconds: int):
        super().__init__(message)
        self.status = status
        self.retry_seconds = retry_seconds


def _clean_text(value: object, limit: int = MAX_CAPTION_CHARS) -> str:
    if isinstance(value, dict):
        for key in ("text", "content", "value", "title"):
            if value.get(key):
                value = value[key]
                break
    if isinstance(value, list):
        value = " ".join(_clean_text(item, limit) for item in value)
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _safe_username(value: object) -> str:
    username = str(value or "").strip().lstrip("@").lower()
    return username if _USERNAME_RE.fullmatch(username) else ""


def _canonical_instagram_permalink(value: object) -> tuple[str, str]:
    text = str(value or "").strip()
    if text and not text.startswith(("http://", "https://")):
        text = f"https://{text}"
    try:
        parsed = urlsplit(text)
    except ValueError:
        return "", ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in {"instagram.com", "www.instagram.com", "instagr.am"}:
        return "", ""
    match = _POST_PATH_RE.search(parsed.path or "")
    if not match:
        return "", ""
    post_id = match.group("post_id")
    kind = match.group("kind").lower()
    return f"https://www.instagram.com/{kind}/{post_id}/", post_id


def _epoch(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return int(stamp)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y"):
            try:
                return int(datetime.strptime(text, fmt).replace(tzinfo=UTC).timestamp())
            except ValueError:
                continue
        return None


def _username_from(value: object) -> str:
    if isinstance(value, dict):
        for key in ("username", "user_name", "handle", "name"):
            result = _safe_username(value.get(key))
            if result:
                return result
        return ""
    return _safe_username(value)


def _post_candidate(value: object, account: dict) -> dict | None:
    if not isinstance(value, dict):
        return None
    account_username = _safe_username(account.get("username"))
    owner = _username_from(
        value.get("username") or value.get("user") or value.get("owner")
        or value.get("author") or value.get("account")
    )
    if owner and owner != account_username:
        return None

    permalink = ""
    post_id = ""
    for key in ("permalink", "post_url", "url", "link", "href"):
        permalink, post_id = _canonical_instagram_permalink(value.get(key))
        if permalink:
            break
    if not permalink:
        shortcode = str(value.get("shortcode") or value.get("short_code") or "").strip()
        if shortcode and re.fullmatch(r"[A-Za-z0-9_-]{3,128}", shortcode):
            post_id = shortcode
            permalink = f"https://www.instagram.com/p/{shortcode}/"
    if not permalink or not post_id:
        return None

    caption = ""
    for key in ("caption", "text", "description", "content", "title"):
        if value.get(key):
            caption = _clean_text(value[key])
            if caption:
                break
    posted_at = None
    for key in ("posted_at", "taken_at", "timestamp", "created_at", "date", "published_at"):
        posted_at = _epoch(value.get(key))
        if posted_at is not None:
            break
    return {
        "post_id": post_id,
        "username": account_username,
        "permalink": permalink,
        "caption": caption,
        "posted_at": posted_at,
    }


def _adapter_metadata(value: object, account: dict) -> list[dict]:
    """Collect date/caption metadata from adapter rows without guessing order.

    The current OpenCLI Instagram adapter can return useful caption/date rows
    while omitting the post shortcode.  The browser fallback supplies the
    permalink; this side channel is deliberately joined by normalized caption
    text below rather than by row index (the profile may contain pinned posts).
    """
    metadata: list[dict] = []
    account_username = _safe_username(account.get("username"))

    def walk(node: object) -> None:
        if isinstance(node, dict):
            owner = _username_from(
                node.get("username") or node.get("user") or node.get("owner")
                or node.get("author") or node.get("account")
            )
            if owner and owner != account_username:
                return
            caption = ""
            for key in ("caption", "text", "description", "content", "title"):
                if node.get(key):
                    caption = _clean_text(node[key])
                    if caption:
                        break
            posted_at = None
            for key in ("posted_at", "taken_at", "timestamp", "created_at", "date", "published_at"):
                posted_at = _epoch(node.get(key))
                if posted_at is not None:
                    break
            if caption or posted_at is not None:
                metadata.append({"caption": caption, "posted_at": posted_at})
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    # Nested adapter payloads often repeat the same row in a wrapper.  Keep
    # one row per exact caption/date pair so matching remains deterministic.
    unique: list[dict] = []
    seen: set[tuple[str, int | None]] = set()
    for row in metadata:
        key = (row["caption"].casefold(), row["posted_at"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _caption_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", _clean_text(value).casefold())


def _merge_browser_metadata(browser_posts: list[dict], adapter_metadata: list[dict]) -> list[dict]:
    """Join browser links to adapter dates only on a unique caption match."""
    if not adapter_metadata:
        return browser_posts
    result: list[dict] = []
    for post in browser_posts:
        merged = dict(post)
        if merged.get("posted_at") is None:
            key = _caption_key(merged.get("caption"))
            matches = [row for row in adapter_metadata if key and _caption_key(row["caption"]) == key]
            if not matches and len(key) >= 12:
                matches = [
                    row for row in adapter_metadata
                    if len(_caption_key(row["caption"])) >= 12 and (
                        key in _caption_key(row["caption"])
                        or _caption_key(row["caption"]) in key
                    )
                ]
            if len(matches) == 1:
                merged["posted_at"] = matches[0].get("posted_at")
        result.append(merged)
    return result


def _walk_payload(value: object, account: dict):
    if isinstance(value, dict):
        candidate = _post_candidate(value, account)
        if candidate:
            yield candidate
        for child in value.values():
            yield from _walk_payload(child, account)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_payload(child, account)


def parse_opencli_posts(payload: object, account: dict) -> list[dict]:
    """Parse OpenCLI YAML/JSON output into bounded, account-owned post rows."""
    if isinstance(payload, str):
        try:
            payload = yaml.safe_load(payload) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"OpenCLI output is not valid YAML: {exc}") from exc
    seen: set[str] = set()
    posts: list[dict] = []
    for candidate in _walk_payload(payload, account):
        post_id = candidate["post_id"]
        if post_id in seen:
            continue
        seen.add(post_id)
        posts.append(candidate)
    return posts


def _payload_has_linkless_rows(value: object) -> bool:
    """OpenCLI's Instagram adapter currently omits post links from table rows."""
    if isinstance(value, dict):
        has_caption = any(value.get(key) for key in ("caption", "text", "description"))
        has_identity = any(value.get(key) for key in (
            "permalink", "post_url", "url", "link", "href", "shortcode", "short_code",
        ))
        if has_caption and not has_identity:
            return True
        return any(_payload_has_linkless_rows(child) for child in value.values())
    if isinstance(value, list):
        return any(_payload_has_linkless_rows(child) for child in value)
    return False


_BROWSER_POST_SCRIPT = r"""
(async () => {
  await new Promise(resolve => setTimeout(resolve, 250));
  let publicConfirmed = null;
  let publicProof = null;
  try {
    const response = await fetch('/api/v1/users/web_profile_info/?username=USERNAME', {
      credentials: 'include', headers: { 'X-IG-App-ID': '936619743392459' }
    });
    const payload = await response.json();
    let user = payload?.data?.user;
    if (!user) {
      const feed = await fetch('/api/v1/feed/user/USERNAME/username/?count=1', {
        credentials: 'include', headers: { 'X-IG-App-ID': '936619743392459' }
      });
      user = (await feed.json())?.user;
    }
    if (String(user?.username || '').toLowerCase() === 'USERNAME' &&
        typeof user?.is_private === 'boolean') {
      publicConfirmed = !user.is_private;
      publicProof = publicConfirmed ? 'profile_api' : 'private_api';
    }
  } catch {}
  if (/this account is private|account is private/i.test(document.body.innerText || '')) {
    publicConfirmed = false;
    publicProof = 'private_wall';
  }
  const links = [...document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]')]
    .filter((link) => {
      try {
        const url = new URL(link.getAttribute('href'), location.origin);
        const host = (url.hostname || '').toLowerCase();
        return (host === 'instagram.com' || host === 'www.instagram.com') &&
          new RegExp('^/USERNAME/(?:p|reel|tv)/[A-Za-z0-9_-]+/?$', 'i').test(url.pathname);
      } catch { return false; }
    });
  if (publicConfirmed !== true && publicConfirmed !== false) {
    // Instagram's legacy JSON endpoints now return 404.  Ask the same-origin
    // page for an anonymous response (credentials omitted) and inspect only
    // bounded profile metadata.  A public profile exposes an exact OG URL
    // plus follower/following/post counts to logged-out viewers; a private or
    // login-wall response does not qualify.  No response body is persisted.
    try {
      const anonymous = await fetch('/USERNAME/', { credentials: 'omit', cache: 'no-store' });
      const html = (await anonymous.text()).slice(0, 300000);
      const escaped = 'USERNAME'.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const exactProfile = new RegExp(
        'property=["\\\']og:url["\\\'][^>]+content=["\\\']https?://(?:www\\.)?instagram\\.com/' + escaped + '/?["\\\']',
        'i',
      ).test(html);
      const publicCounts = /\b(?:followers|following|posts)\b/i.test(html);
      const privateWall = /private account|this account is private|follow to see/i.test(html);
      let observedPost = false;
      const anonymousHost = (() => {
        try { return new URL(anonymous.url).hostname.toLowerCase(); } catch { return ''; }
      })();
      const anonymousPath = (() => {
        try { return new URL(anonymous.url).pathname; } catch { return ''; }
      })();
      if (anonymous.ok && (anonymousHost === 'instagram.com' || anonymousHost === 'www.instagram.com') &&
          new RegExp('^/USERNAME/?$', 'i').test(anonymousPath) && exactProfile &&
          publicCounts && !privateWall && links.length > 0) {
        const normalize = (value) => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
        const decode = (value) => String(value || '')
          .replace(/&quot;/g, '"').replace(/&#x27;|&#39;/g, "'").replace(/&amp;/g, '&');
        for (const link of links.slice(0, 3)) {
          if (observedPost) break;
          try {
            const postUrl = new URL(link.getAttribute('href'), location.origin);
            const postId = postUrl.pathname.match(/\/(?:p|reel|tv)\/([A-Za-z0-9_-]+)/i)?.[1] || '';
            const expected = normalize(link.querySelector('img')?.alt || link.getAttribute('aria-label') || '');
            const expectedWords = [...new Set(expected.split(' ').filter((word) => word.length >= 4))];
            if (!postId || expected.length < 40 || expectedWords.length < 6) continue;
            const postResponse = await fetch(postUrl.href, {
              credentials: 'omit', cache: 'no-store'
            });
            const postHost = (() => {
              try { return new URL(postResponse.url).hostname.toLowerCase(); } catch { return ''; }
            })();
            const postHtml = (await postResponse.text()).slice(0, 300000);
            const descriptionMatch = postHtml.match(
              /<meta[^>]+property=["']og:description["'][^>]+content=["']([^"']+)/i,
            );
            const description = normalize(decode(descriptionMatch?.[1] || ''));
            const matchedWords = expectedWords.filter((word) => description.includes(word)).length;
            observedPost = Boolean(
              postResponse.ok && (postHost === 'instagram.com' || postHost === 'www.instagram.com') &&
              postHtml.includes(postId) && description.length >= 40 &&
              matchedWords >= Math.min(6, Math.ceil(expectedWords.length * 0.35)) &&
              !/private account|this account is private|follow to see/i.test(postHtml)
            );
          } catch {}
        }
      }
      if (observedPost) {
        publicConfirmed = true;
        publicProof = 'logged_out_post_metadata';
      }
    } catch {}
  }
  return {
    public_confirmed: publicConfirmed === true,
    public_proof: publicProof,
    posts: links.slice(0, LIMIT).map((link, index) => {
    const article = link.closest('article') || link.parentElement;
    const time = article?.querySelector('time') || link.querySelector('time');
    return {
      index: index + 1,
      permalink: new URL(link.getAttribute('href'), location.origin).href,
      caption: (article?.innerText || link.querySelector('img')?.alt ||
        link.getAttribute('aria-label') || '').slice(0, 2400),
      posted_at: time?.getAttribute('datetime') || null,
    };
    })
  };
})()
"""


def _browser_fallback_posts(
    account: dict,
    *,
    max_posts: int,
    timeout: int,
    runner,
) -> list[dict]:
    """Recover direct post links from the public profile DOM.

    OpenCLI's high-level ``instagram user`` adapter exposes caption/date
    columns but currently drops the post shortcode.  The browser fallback
    stays within OpenCLI's existing browser session and extracts only public
    post links plus bounded visible text; it never reads cookies or storage.
    """
    username = _safe_username(account.get("username"))
    session = f"jobradar-social-{username}"
    binary = _opencli_binary()
    opened = False
    try:
        opened_result = runner([
            binary, "browser", "open", f"https://www.instagram.com/{username}/",
            "--session", session, "--window", "background",
        ], timeout=timeout)
        if int(getattr(opened_result, "returncode", 0)) != 0:
            message = _clean_text(
                getattr(opened_result, "stderr", "") or getattr(opened_result, "stdout", ""),
                MAX_ERROR_CHARS,
            )
            status, retry = _classify_failure(message)
            raise SocialFetchError(status, message or "OpenCLI browser session unavailable", retry)
        opened = True
        script = (
            _BROWSER_POST_SCRIPT
            .replace("LIMIT", str(max_posts), 1)
            .replace("USERNAME", username)
        )
        evaluated = runner([
            binary, "browser", "eval", script,
            "--session", session, "--window", "background",
        ], timeout=timeout)
        if int(getattr(evaluated, "returncode", 0)) != 0:
            message = _clean_text(getattr(evaluated, "stderr", "") or getattr(evaluated, "stdout", ""), MAX_ERROR_CHARS)
            status, retry = _classify_failure(message)
            raise SocialFetchError(status, message or "OpenCLI profile extraction failed", retry)
        try:
            payload = yaml.safe_load(getattr(evaluated, "stdout", "") or "") or {}
        except yaml.YAMLError as exc:
            raise SocialFetchError("error", f"OpenCLI profile output is not valid YAML: {exc}", 3600) from exc
        # OpenCLI wraps browser eval output in ``{ok, data}``; depending on
        # the adapter version ``data`` is either the serialized JS result or a
        # nested object.  Unwrap both forms before applying the visibility
        # proof, never treating the wrapper itself as a public profile.
        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
            if isinstance(payload, str):
                try:
                    payload = yaml.safe_load(payload) or {}
                except yaml.YAMLError as exc:
                    raise SocialFetchError(
                        "error", f"OpenCLI browser data is not valid YAML: {exc}", 3600,
                    ) from exc
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            payload = payload["result"]
        if not isinstance(payload, dict) or payload.get("public_confirmed") is not True:
            raise SocialFetchError(
                "unavailable",
                "Instagram profile is private or public visibility was not confirmed",
                86400,
            )
        rows = payload.get("posts")
        if not isinstance(rows, list):
            rows = payload.get("rows", [])
        posts = parse_opencli_posts(rows, account)
        proof = str(payload.get("public_proof") or "").strip()
        if proof:
            for post in posts:
                post["public_visibility_proof"] = proof
        return posts
    finally:
        if opened:
            try:
                runner([
                    binary, "browser", "close", "--session", session,
                ], timeout=min(timeout, 10))
            except Exception:
                # Cleanup must never turn a valid read into a failed account.
                _LOGGER.debug("OpenCLI browser session cleanup failed", exc_info=True)


def _job_url(value: str) -> str:
    text = value.strip().rstrip(".,;:!?)]}>")
    if text.startswith("www."):
        text = f"https://{text}"
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.username or parsed.password:
        return ""
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return ""
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return ""
    host_is_ats = any(host == suffix or host.endswith(f".{suffix}") for suffix in _KNOWN_JOB_HOSTS)
    if not host_is_ats and not _JOB_PATH_RE.search(parsed.path or ""):
        return ""
    query = [
        (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith(_TRACKING_QUERY_PREFIXES)
    ]
    if any(
        key.casefold() in _SENSITIVE_QUERY_KEYS
        or any(token in key.casefold() for token in ("token", "secret", "auth", "password"))
        for key, _ in query
    ):
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _caption_urls(caption: str) -> list[str]:
    urls: list[str] = []
    for match in _URL_RE.findall(caption or ""):
        url = _job_url(match)
        if url and url not in urls:
            urls.append(url)
    return urls


def _host_company(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in (parsed.path or "").split("/") if part]
    for marker in ("jobs", "job", "careers", "career", "apply"):
        if marker in path_parts:
            index = path_parts.index(marker)
            if index > 0:
                candidate = path_parts[index - 1].replace("-", " ").replace("_", " ").strip()
                if candidate and candidate.casefold() not in {"www", "boards", "careers", "jobs"}:
                    return candidate.title()
    labels = [part for part in host.split(".") if part and part not in {"www", "jobs", "careers", "apply", "boards"}]
    if not labels:
        return ""
    value = labels[0].replace("-", " ").replace("_", " ").strip()
    if value.casefold() in {
        "example", "localhost", "instagram", "greenhouse", "lever", "ashby",
        "workday", "smartrecruiters", "workable", "jobvite", "recruitee",
    }:
        return ""
    return value.title()


def _role_and_company(caption: str, url: str) -> tuple[str, str]:
    lines = [line.strip(" -*•\t") for line in (caption or "").splitlines() if line.strip()]
    lines = [re.sub(r"https?://\S+|www\.\S+", "", line).strip(" -*•") for line in lines]
    role_lines = [line for line in lines if _ROLE_RE.search(line)]
    if len(role_lines) != 1:
        return "", ""
    role_line = role_lines[0]

    company = ""
    title = role_line
    match = re.match(r"^(?P<company>[A-Z][\w.&' /-]{1,60}?)\s*(?:[-–—|:]|\bat\b)\s*(?P<title>.+)$", role_line, re.I)
    if match and _ROLE_RE.search(match.group("title")):
        company = match.group("company").strip(" -–—|:")
        title = match.group("title").strip()
    else:
        at_match = re.search(r"\b(?:at|with|@)\s+(?P<company>[A-Z][\w.&' /-]{1,60})", role_line)
        if at_match:
            company = at_match.group("company").strip(" -–—|:,.")
    company = company or _host_company(url)
    if not company or not title or len(title) > 180:
        return "", ""
    return company[:100], title[:180]


def social_posts_to_jobs(posts: list[dict], account: dict, *, max_jobs: int = 50) -> list[Job]:
    """Create source-only Job leads, never an employer-authored description."""
    username = _safe_username(account.get("username"))
    if not username or max_jobs <= 0:
        return []
    seen_urls: set[str] = set()
    jobs: list[Job] = []
    for post in posts:
        if not isinstance(post, dict) or _safe_username(post.get("username")) != username:
            continue
        permalink, post_id = _canonical_instagram_permalink(post.get("permalink"))
        if not permalink:
            continue
        caption = _redact_caption(post.get("caption"))
        urls = list(dict.fromkeys(_caption_urls(caption)))
        # A single post advertising several unrelated URLs is a curator
        # roundup, not a deterministic job sighting. Keep it in the bounded
        # social ledger for review but do not invent a company/role mapping.
        if len(urls) != 1:
            continue
        url = urls[0]
        company, title = _role_and_company(caption, url)
        if not company or not title:
            continue
        identity = posting_key(url) or canonical_url(url)
        if not identity or identity in seen_urls:
            continue
        seen_urls.add(identity)
        jobs.append(Job(
                company=company,
                title=title,
                url=url,
                source="instagram",
                source_url=permalink,
                source_board=f"public:instagram:{username}",
                alternate_urls=[permalink],
                # A social publication date is not an employer posting date.
                # Preserve it in link_resolution instead of making freshness
                # scoring treat a curator timestamp as a job timestamp.
                posted_at=None,
                description="",
                link_resolution={
                    "status": "source_only",
                    "method": "instagram_opencli",
                    "reason": "Instagram career lead; require employer posting evidence",
                    "source_note": caption[:1000],
                    "social_post_id": post_id,
                    "social_permalink": permalink,
                    "social_posted_at": _epoch(post.get("posted_at")),
                },
        ))
        if len(jobs) >= max(0, min(int(max_jobs), MAX_POSTS_PER_ACCOUNT)):
            return jobs
    return jobs


def _load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return value
    except (OSError, json.JSONDecodeError):
        return default


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    temporary.replace(path)


@contextmanager
def _state_lock(path: Path):
    """Coordinate the hourly collector without locking crawler/job state."""
    configured = os.environ.get("RADAR_SOCIAL_LOCK", "").strip()
    if configured:
        lock_path = Path(configured)
    else:
        digest = hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
        lock_path = Path(tempfile.gettempdir()) / f"job-radar-social-{digest}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def load_social_config(path: Path | str = CONFIG_PATH) -> dict:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    defaults = raw.get("defaults") if isinstance(raw, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    accounts = raw.get("accounts") if isinstance(raw, dict) else []
    if not isinstance(accounts, list):
        accounts = []
    normalized: list[dict] = []
    for account in accounts[:MAX_ACCOUNTS]:
        if not isinstance(account, dict) or account.get("enabled", True) is False:
            continue
        username = _safe_username(account.get("username"))
        if not username:
            continue
        normalized.append({**account, "username": username, "enabled": True})
    return {
        "version": int(raw.get("version", 1)) if isinstance(raw, dict) else 1,
        "defaults": defaults,
        "accounts": normalized,
    }


def _setting(defaults: dict, key: str, fallback: int, *, high: int) -> int:
    try:
        value = int(defaults.get(key, fallback))
    except (TypeError, ValueError):
        value = fallback
    return max(1, min(value, high))


def _prune_posts(posts: dict, usernames: set[str], *, now: int, max_per_account: int, retention_days: int) -> None:
    cutoff = now - retention_days * 86400
    for post_id, post in list(posts.items()):
        if not isinstance(post, dict) or _safe_username(post.get("username")) not in usernames:
            posts.pop(post_id, None)
    for username in usernames:
        rows = [
            (post_id, post) for post_id, post in posts.items()
            if isinstance(post, dict) and _safe_username(post.get("username")) == username
        ]
        fresh = [
            (post_id, post) for post_id, post in rows
            if (post.get("posted_at") or post.get("fetched_at") or now) >= cutoff
        ]
        fresh.sort(key=lambda item: (item[1].get("posted_at") or 0, item[1].get("fetched_at") or 0), reverse=True)
        keep = {post_id for post_id, _ in fresh[:max_per_account]}
        for post_id, _ in rows:
            if post_id not in keep:
                posts.pop(post_id, None)


def _classify_failure(text: str) -> tuple[str, int]:
    lowered = (text or "").casefold()
    if any(token in lowered for token in ("429", "rate limit", "too many requests")):
        return "rate_limited", 7200
    if any(token in lowered for token in ("login", "log in", "logged out", "unauth", "sign in", "auth required")):
        return "unauthenticated", 86400
    if any(token in lowered for token in ("extension", "disconnected", "daemon", "chrome", "bridge")):
        return "unavailable", 3600
    return "error", 3600


def _career_excerpt(value: object) -> str:
    """Retain only bounded career evidence in the public post ledger."""
    caption = _redact_caption(value)
    if not caption or not _ROLE_RE.search(caption):
        return ""
    return caption[:MAX_STORED_CAREER_EXCERPT]


def _redact_caption(value: object) -> str:
    """Remove emails and unsafe URLs before any caption enters durable state."""
    caption = _clean_text(value, MAX_CAPTION_CHARS)

    def replace_url(match: re.Match[str]) -> str:
        safe = _job_url(match.group(0))
        return safe or "[link]"

    caption = _URL_RE.sub(replace_url, caption)
    return _EMAIL_RE.sub("[email]", caption)


def _run_opencli(command: list[str], *, timeout: int):
    return subprocess.run(  # noqa: S603 - command is fixed plus validated account name
        command, capture_output=True, text=True, timeout=timeout, check=False,
    )


def _opencli_binary() -> str:
    configured = os.environ.get("OPENCLI_BIN", "").strip()
    if configured:
        return configured
    discovered = shutil.which("opencli")
    if discovered:
        return discovered
    node_root = Path.home() / ".nvm" / "versions" / "node"
    candidates = sorted(node_root.glob("*/bin/opencli"), key=lambda item: item.stat().st_mtime, reverse=True)
    return str(candidates[0]) if candidates else "opencli"


def _fetch_account_posts(account: dict, *, max_posts: int, timeout: int, runner) -> list[dict]:
    """Read an account once, falling back to DOM links when adapter rows omit them."""
    username = _safe_username(account.get("username"))
    result = runner([
        _opencli_binary(), "instagram", "user", username,
        "--limit", str(max_posts), "-f", "yaml",
    ], timeout=timeout)
    output = getattr(result, "stdout", "") or ""
    error = getattr(result, "stderr", "") or ""
    if int(getattr(result, "returncode", 0)) != 0:
        status, retry = _classify_failure(f"{error}\n{output}")
        # OpenCLI 2.0.2 currently targets a retired Instagram feed route
        # (HTTP 404).  Recover through the already-connected public browser
        # session for this narrow route failure; auth/429/bridge failures do
        # not trigger a retry or a second network path.
        if status == "error" and "404" in f"{error}\n{output}":
            return _browser_fallback_posts(
                account, max_posts=max_posts, timeout=timeout, runner=runner,
            )
        raise SocialFetchError(status, _clean_text(error or output, MAX_ERROR_CHARS), retry)
    try:
        payload = yaml.safe_load(output) or {}
    except yaml.YAMLError as exc:
        raise SocialFetchError("error", f"OpenCLI output is not valid YAML: {exc}", 3600) from exc
    posts = parse_opencli_posts(payload, account)
    metadata = _adapter_metadata(payload, account)
    if posts and not _payload_has_linkless_rows(payload):
        # A future OpenCLI adapter may retain links. It still must pass the
        # browser visibility check before its rows enter the public ledger.
        return _merge_browser_metadata(_browser_fallback_posts(
            account, max_posts=max_posts, timeout=timeout, runner=runner,
        ) or posts, metadata)
    if not _payload_has_linkless_rows(payload):
        return posts
    return _merge_browser_metadata(_browser_fallback_posts(
        account, max_posts=max_posts, timeout=timeout, runner=runner,
    ), metadata)


def collect_social_leads(
    *,
    config_path: Path | str = CONFIG_PATH,
    state_path: Path | str | None = None,
    now: int | None = None,
    runner=None,
) -> dict:
    """Poll each enabled account once, preserving state on every failure."""
    config = load_social_config(config_path)
    path = Path(state_path) if state_path is not None else radar_state.STATE_DIR / "social.json"
    timestamp = int(time.time() if now is None else now)
    runner = runner or _run_opencli
    defaults = config["defaults"]
    max_posts = _setting(defaults, "max_posts", DEFAULT_MAX_POSTS, high=MAX_POSTS_PER_ACCOUNT)
    timeout = _setting(defaults, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS, high=60)
    cooldown = _setting(defaults, "failure_cooldown_seconds", DEFAULT_FAILURE_COOLDOWN_SECONDS, high=7 * 86400)
    max_stored_posts = _setting(
        defaults, "max_stored_posts", DEFAULT_MAX_STORED_POSTS,
        high=MAX_ACCOUNTS * DEFAULT_MAX_STORED_POSTS,
    )
    retention_days = _setting(defaults, "retention_days", DEFAULT_RETENTION_DAYS, high=3650)
    max_runtime = _setting(defaults, "max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS, high=600)
    stats = {
        "accounts": len(config["accounts"]), "attempted": 0, "success": 0,
        "posts": 0, "new_posts": 0, "cooldown": 0, "unauthenticated": 0,
        "rate_limited": 0, "unavailable": 0, "errors": 0, "empty": 0, "deferred": 0,
    }
    started = time.monotonic()
    with _state_lock(path):
        state = _load_json(path, {"schema_version": STATE_SCHEMA_VERSION, "accounts": {}, "posts": {}})
        if not isinstance(state, dict):
            state = {"schema_version": STATE_SCHEMA_VERSION, "accounts": {}, "posts": {}}
        accounts_state = state.setdefault("accounts", {})
        posts_state = state.setdefault("posts", {})
        if not isinstance(accounts_state, dict):
            accounts_state = state["accounts"] = {}
        if not isinstance(posts_state, dict):
            posts_state = state["posts"] = {}

        for account in config["accounts"]:
            if time.monotonic() - started >= max_runtime:
                stats["deferred"] += len(config["accounts"]) - stats["attempted"] - stats["cooldown"]
                break
            username = account["username"]
            prior = accounts_state.get(username)
            prior = dict(prior) if isinstance(prior, dict) else {}
            retry_after = _epoch(prior.get("retry_after")) or 0
            if retry_after > timestamp:
                stats["cooldown"] += 1
                accounts_state[username] = prior
                continue
            stats["attempted"] += 1
            prior["last_attempt_at"] = timestamp
            try:
                posts = _fetch_account_posts(
                    account, max_posts=max_posts, timeout=timeout, runner=runner,
                )
            except SocialFetchError as exc:
                status = exc.status
                prior.update({
                    "status": status,
                    "last_error": _clean_text(str(exc), MAX_ERROR_CHARS),
                    "retry_after": timestamp + max(cooldown, exc.retry_seconds),
                })
                stats[status if status in {"unauthenticated", "rate_limited", "unavailable"} else "errors"] += 1
                accounts_state[username] = prior
                continue
            except FileNotFoundError:
                posts = None
                status, multiplier = "unavailable", 3600
                prior.update({
                    "status": status,
                    "last_error": "opencli command is not installed",
                    "retry_after": timestamp + max(cooldown, multiplier),
                })
            except subprocess.TimeoutExpired:
                posts = None
                status, multiplier = "unavailable", 3600
                prior.update({
                    "status": status,
                    "last_error": "opencli timed out",
                    "retry_after": timestamp + max(cooldown, multiplier),
                })
            except (OSError, ValueError, yaml.YAMLError) as exc:
                posts = None
                status, multiplier = "error", 3600
                prior.update({
                    "status": status,
                    "last_error": _clean_text(str(exc), MAX_ERROR_CHARS),
                    "retry_after": timestamp + max(cooldown, multiplier),
                })
            if posts is None:
                stats[status if status in {"unavailable", "errors"} else "errors"] += 1
                accounts_state[username] = prior
                continue

            stats["success"] += 1
            stats["posts"] += len(posts)
            new_posts = 0
            latest = None
            for post in posts:
                post_id = post["post_id"]
                existing = posts_state.get(post_id)
                if not isinstance(existing, dict):
                    new_posts += 1
                    existing = {}
                career_excerpt = _career_excerpt(post.get("caption"))
                normalized = {
                    **existing,
                    **post,
                    "fetched_at": timestamp,
                    "first_seen_at": existing.get("first_seen_at", timestamp),
                }
                # Keep identity/date/cursor metadata for ordinary public posts,
                # but never commit raw lifestyle/finance/travel captions.  A
                # short excerpt is sufficient for a career lead under review.
                if career_excerpt:
                    normalized["caption"] = career_excerpt
                elif _career_excerpt(existing.get("caption")):
                    normalized["caption"] = _career_excerpt(existing.get("caption"))
                else:
                    normalized.pop("caption", None)
                posts_state[post_id] = normalized
                candidate = (post.get("posted_at") or 0, post_id)
                if latest is None or candidate > latest[0]:
                    latest = (candidate, normalized)
            stats["new_posts"] += new_posts
            prior.update({
                "status": "ok" if posts else "empty",
                "last_success_at": timestamp,
                "last_result_count": len(posts),
            })
            if not posts:
                stats["empty"] += 1
            prior.pop("last_error", None)
            prior.pop("retry_after", None)
            if latest is not None:
                prior["cursor"] = {
                    "post_id": latest[1]["post_id"],
                    "posted_at": latest[1].get("posted_at"),
                }
            accounts_state[username] = prior
        _prune_posts(
            posts_state,
            {account["username"] for account in config["accounts"]},
            now=timestamp,
            max_per_account=max_stored_posts,
            retention_days=retention_days,
        )
        state.update({
            "schema_version": STATE_SCHEMA_VERSION,
            "updated_at": timestamp,
            "source_status": (
                "live" if stats["new_posts"] or any(
                    accounts_state.get(account["username"], {}).get("status") == "ok"
                    for account in config["accounts"]
                ) else "empty" if stats["empty"] else "unavailable"
            ),
        })
        _write_json_atomic(path, state)
    return {"stats": stats, "state_path": str(path)}


def fetch_social_leads(
    *,
    config_path: Path | str = CONFIG_PATH,
    state_path: Path | str | None = None,
    max_jobs: int = 50,
    now: int | None = None,
) -> list[Job]:
    """Read the public social ledger for normal crawler ingestion."""
    config = load_social_config(config_path)
    accounts = {account["username"]: account for account in config["accounts"]}
    path = Path(state_path) if state_path is not None else radar_state.STATE_DIR / "social.json"
    state = _load_json(path, {})
    posts = state.get("posts", {}) if isinstance(state, dict) else {}
    account_state = state.get("accounts", {}) if isinstance(state, dict) else {}
    if not isinstance(posts, dict):
        return []
    if not isinstance(account_state, dict):
        return []
    current = int(time.time() if now is None else now)
    retention_days = _setting(
        config["defaults"], "retention_days", DEFAULT_RETENTION_DAYS, high=3650,
    )
    recent_accounts = {
        username for username in accounts
        if isinstance(account_state.get(username), dict)
        and account_state[username].get("status") == "ok"
        and current - (_epoch(account_state[username].get("last_success_at")) or 0)
        <= retention_days * 86400
    }
    rows: list[dict] = []
    for post in posts.values():
        if not isinstance(post, dict):
            continue
        username = _safe_username(post.get("username"))
        account = accounts.get(username)
        fetched_at = _epoch(post.get("fetched_at")) or 0
        if account and username in recent_accounts and current - fetched_at <= retention_days * 86400:
            rows.append(post)
    rows.sort(key=lambda item: (item.get("posted_at") or 0, item.get("fetched_at") or 0), reverse=True)
    jobs: list[Job] = []
    seen: set[str] = set()
    for account in accounts.values():
        for job in social_posts_to_jobs(rows, account, max_jobs=max_jobs):
            identity = posting_key(job.url) or canonical_url(job.url)
            if identity in seen:
                continue
            seen.add(identity)
            jobs.append(job)
            if len(jobs) >= max(0, int(max_jobs)):
                return jobs
    return jobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded OpenCLI Instagram collector")
    parser.add_argument("command", choices=("collect",), nargs="?", default="collect")
    args = parser.parse_args(argv)
    if args.command == "collect":
        print(json.dumps(collect_social_leads(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
