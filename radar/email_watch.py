"""Detect real applications by watching for confirmation emails.

Why this exists: a checkbox on an alert issue can only mean "I intend to" or
"I saved this" — it can't know whether you actually clicked submit. The
company you actually hear back from is the ground truth. So instead of
trusting the checkbox, this polls the inbox by IMAP for messages that look
like application confirmations ("Thank you for applying...", "We've received
your application..."), extracts the company from the sender/subject, matches
it against what's shortlisted (or, failing that, anything the radar has ever
seen), and promotes the match to a real applied-and-synced-to-Notion entry.

This is a separate credential from the Claude<->Notion connector used
interactively in chat: GitHub Actions runs unattended and needs its own
IMAP login, which only the account owner can create (an app password tied
to their email account) — see EMAIL_ADDRESS / EMAIL_APP_PASSWORD in the
README setup section.

Gmail-specific: when EMAIL_IMAP_HOST contains "gmail", search uses Gmail's
X-GM-RAW extension (the same query language as the Gmail search bar) so
filtering happens server-side. Any other IMAP host falls back to a plain
SINCE-date search plus local subject filtering.
"""
from __future__ import annotations

import email
import email.utils
import hashlib
import imaplib
import re
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header

from . import state
from .applied import TRACKER_STAGE_ORDER, record_applied
from .config import env
from .identity import canonical_url
from .models import norm
from .notion_sync import sync_applied

DEFAULT_HOST = "imap.gmail.com"
DEFAULT_LOOKBACK_DAYS = 21
MAX_SEEN_IDS = 4000

CONFIRMATION_RE = re.compile(
    r"thank(?:s| you) for (?:your interest in |applying|your application)|"
    r"we appreciate your interest in|"
    r"application (?:has been )?(?:received|submitted|confirmed)|"
    r"we(?:'ve| have) received your application|"
    r"your application (?:to|for|at|has been received)|"
    r"your application (?:was|is being) (?:successfully )?submitted|"
    r"we have successfully received|"
    r"application confirmation|"
    r"successfully applied", re.I)

REJECTION_RE = re.compile(
    r"unfortunately|we regret to inform|regret to inform you|"
    r"not (?:be )?(?:moving|move|proceeding|proceed) forward|"
    r"decided (?:not )?to (?:move forward|proceed|pursue|advance)|"
    r"(?:pursue|move forward with) other (?:candidates|applicant)|"
    r"no longer (?:be )?(?:under consideration|considering|moving forward)|"
    r"will not be (?:moving|proceeding|advancing)|"
    r"(?:position|role|req(?:uisition)?) (?:has been|is) (?:filled|closed)|"
    r"not (?:be )?(?:selected|selecting you|a match at this time)|"
    r"after (?:careful|thorough) (?:consideration|review)|"
    r"chosen to move forward with other|"
    r"we(?:'ve| have) decided to go in another direction|"
    r"decided to pursue other opportunities|"
    r"not selected for (?:the )?(?:next|final) round|"
    r"unable to move forward with your candidacy|"
    r"your candidacy will not be advancing|"
    r"we will not be advancing your application", re.I)

OA_RE = re.compile(
    r"online assessment|coding (?:challenge|assessment|test|exercise)|"
    r"hackerrank|codesignal|codility|karat|hirevue|"
    r"take[- ]?home (?:assignment|assessment|challenge)|"
    r"technical (?:assessment|screen(?:ing)?)|assessment (?:invitation|link)|"
    r"complete (?:the|your|a) (?:assessment|challenge)|"
    r"coding (?:test|exercise) link|"
    r"pre[- ]?employment assessment", re.I)

INTERVIEW_RE = re.compile(
    r"interview|schedule (?:a |your |some )?(?:time|call|chat|conversation)|"
    r"phone screen|next steps|(?:like|love) to (?:meet|speak|chat|connect) with|"
    r"hiring manager|video call|availability (?:for|to)|book (?:a |your )?time|"
    r"set up (?:a |some )?time|move(?:d)? (?:you )?(?:to|forward to|onto) the|"
    r"invite you to (?:a |an )?(?:conversation|screen)|"
    r"please select a time|meet the team", re.I)


def classify(text: str) -> str | None:
    """Which application-lifecycle event does this email represent?
    Order matters: a post-interview rejection contains 'interview' too, so a
    strong rejection signal wins; OA is checked before generic 'interview'."""
    if REJECTION_RE.search(text):
        return "rejected"
    if OA_RE.search(text):
        return "oa"
    if INTERVIEW_RE.search(text):
        return "interview"
    if CONFIRMATION_RE.search(text):
        return "confirmation"
    return None


# Forward-only pipeline ordering. A late email can never move a job backward
# (e.g. a stray "interview" note after a rejection is ignored). Terminal
# stages sit at the top so any real response can reach them.
STAGE_ORDER = TRACKER_STAGE_ORDER


def can_advance(current: str | None, target: str) -> bool:
    return STAGE_ORDER.get(target, 0) > STAGE_ORDER.get(current or "applied", 1)

NOISE_WORDS = {
    "careers", "career", "recruiting", "recruitment", "talent", "talentacquisition",
    "hr", "humanresources", "jobs", "hiring", "hiringteam", "team", "noreply",
    "no", "reply", "donotreply", "notifications", "notification", "staffing",
    "people", "peopleteam", "recruiter", "recruiters",
}
ATS_DOMAINS = {
    "greenhouse.io", "lever.co", "myworkday.com", "workday.com", "ashbyhq.com",
    "smartrecruiters.com", "icims.com", "taleo.net", "successfactors.com",
    "workable.com", "breezy.hr", "jobvite.com", "bamboohr.com", "recruitee.com",
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com",
    "linkedin.com", "indeed.com", "simplify.jobs", "wellfound.com",
}


def _clean_name(s: str) -> str:
    s = re.sub(r"[^\w& .,'-]", " ", s or "")
    words = [w for w in s.split() if norm(w).replace(" ", "") not in NOISE_WORDS]
    return " ".join(words).strip(" -,.")


def _header(msg: email.message.Message, name: str) -> str:
    """Decode RFC 2047 headers from ATS messages before matching them."""
    raw = msg.get(name, "") or ""
    try:
        return str(make_header(decode_header(str(raw)))).strip()
    except (TypeError, ValueError):
        return str(raw).strip()


def guess_company_candidates(msg: email.message.Message, text: str = "") -> list[str]:
    """Best-effort list of candidate company names, best guess first."""
    candidates = []
    name, addr = email.utils.parseaddr(_header(msg, "From"))
    cleaned_name = _clean_name(name)
    if cleaned_name and norm(cleaned_name):
        candidates.append(cleaned_name)

    domain = addr.split("@")[-1].lower() if "@" in addr else ""
    parts = domain.split(".")
    domain_root = parts[-2] if len(parts) >= 2 else domain
    if domain and domain not in ATS_DOMAINS and domain_root not in {"gmail", "outlook", "yahoo"}:
        candidates.append(domain_root.replace("-", " ").title())

    subject = _header(msg, "Subject")
    for haystack in (subject, text[:2500]):
        for m in re.finditer(
                r"(?:at|from|with|joining|company:)\s+([A-Z][\w&.'-]*"
                r"(?:\s+[A-Z][\w&.'-]*){0,4})", haystack):
            candidate = m.group(1).strip(" .,;:!-|")
            if candidate and candidate not in candidates:
                candidates.append(candidate)

    return candidates


def _token_overlap(a: str, b: str) -> float:
    wa = {w for w in norm(a).split() if len(w) > 2}
    wb = {w for w in norm(b).split() if len(w) > 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def _best_match(candidates: list[str], pool: list[dict], key: str = "company") -> tuple[float, dict | None]:
    best_score, best_item = 0.0, None
    for cand in candidates:
        for item in pool:
            r = _token_overlap(cand, item.get(key, ""))
            if r > best_score:
                best_score, best_item = r, item
    return best_score, best_item


_TITLE_NOISE = {
    "application", "applications", "candidate", "candidacy", "position", "role",
    "job", "jobs", "opportunity", "opportunities", "your", "the", "at", "for",
    "update", "status", "regarding", "next", "steps", "team", "career", "careers",
}


def _meaningful_tokens(text: str) -> set[str]:
    return {token for token in norm(text).split()
            if len(token) > 2 and token not in _TITLE_NOISE}


def _title_match_score(subject: str, body: str, title: str) -> float:
    wanted = _meaningful_tokens(title)
    if not wanted:
        return 0.0
    subject_tokens = _meaningful_tokens(subject)
    body_tokens = _meaningful_tokens(body[:4000])
    subject_score = len(wanted & subject_tokens) / len(wanted)
    body_score = len(wanted & body_tokens) / len(wanted)
    return max(subject_score, body_score)


def match_application(candidates: list[str], subject: str, body: str,
                      pool: list[dict]) -> dict | None:
    """Match an email to one role, requiring enough evidence to be safe.

    Employer evidence is mandatory. A title hit resolves multiple roles at the
    same company; if the message only names a company with several equally
    plausible tracked roles, returning None is safer than changing the wrong
    application.
    """
    scored = []
    for item in pool:
        company_score = max(
            (_token_overlap(candidate, item.get("company", "")) for candidate in candidates),
            default=0.0,
        )
        if company_score < 0.5:
            continue
        title_score = _title_match_score(subject, body, item.get("title", ""))
        scored.append((company_score, title_score, item))
    if not scored:
        return None

    scored.sort(
        key=lambda row: (row[1], row[0], int(row[2].get("applied_at") or 0)),
        reverse=True,
    )
    best = scored[0]
    if len(scored) == 1:
        return best[2]
    second = scored[1]
    if best[1] >= 0.5 and best[1] > second[1]:
        return best[2]
    # A single strong company match is safe when other candidates are only
    # weak token overlaps (for example, Stripe vs Stripe Health).
    if best[0] >= 0.9 and best[0] > second[0] + 0.2:
        return best[2]
    return None


def match_company(candidates: list[str], shortlist: list[dict], jobs: dict) -> dict | None:
    """Prefer a shortlisted job (the user explicitly cared about it); fall
    back to anything the radar has ever seen from a matching company."""
    if not candidates:
        return None
    score, item = _best_match(candidates, shortlist)
    if score >= 0.5:
        return dict(item)

    by_company: dict[str, dict] = {}
    for j in jobs.values():
        key = norm(j["company"])
        if key not in by_company or (j.get("first_seen") or 0) > (by_company[key].get("first_seen") or 0):
            by_company[key] = j
    score, item = _best_match(candidates, list(by_company.values()))
    if score >= 0.5:
        return dict(item)
    return None


def _synthetic_job(company: str, subject: str = "") -> dict:
    # Stable identity prevents a retry or duplicate notification from creating
    # a fresh tracker row every time the watcher runs.
    sid = hashlib.sha1(
        f"email-detected|{norm(company)}|{norm(subject)}".encode(),
        usedforsecurity=False,
    ).hexdigest()[:16]
    title = subject.strip()[:180] or "Application (auto-detected via email)"
    return {"id": sid, "company": company, "title": title,
            "url": "", "locations": [], "score": None, "source": "email-detected"}


def _search_candidate_uids(conn: imaplib.IMAP4_SSL, host: str, lookback_days: int) -> list[bytes]:
    if "gmail" in host.lower():
        # bare terms (no subject:) search the whole message, so rejection /
        # interview / assessment language in the body is caught too.
        query = (
            f'in:anywhere newer_than:{lookback_days}d ('
            'subject:"thank you for applying" OR subject:"thanks for applying" OR '
            'subject:"application received" OR subject:"your application" OR '
            'subject:"application confirmation" OR subject:"applying to" OR '
            'subject:"update on your" OR subject:"regarding your application" OR '
            'subject:interview OR subject:assessment OR subject:"next steps" OR '
            '"successfully applied" OR "unfortunately" OR "move forward with other" OR '
            '"regret to inform" OR "not selected" OR "not moving forward" OR '
            '"online assessment" OR "coding challenge" OR "schedule a" OR '
            '"we received your application" OR "another direction")'
        )
        typ, data = conn.search(None, "X-GM-RAW", f'"{query}"')
    else:
        since = (datetime.now(UTC) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        typ, data = conn.search(None, f"(SINCE {since})")
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _fetch_headers(conn: imaplib.IMAP4_SSL, uid: bytes) -> email.message.Message | None:
    typ, data = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return email.message_from_bytes(data[0][1])


def _fetch_full(conn: imaplib.IMAP4_SSL, uid: bytes) -> email.message.Message | None:
    typ, data = conn.fetch(uid, "(BODY.PEEK[])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return email.message_from_bytes(data[0][1])


def _body_text(msg: email.message.Message, limit: int = 4000) -> str:
    """Plain-text body (first text/plain part, else stripped text/html)."""
    def _decode(part) -> str:
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                return ""
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            return ""

    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not plain:
                plain = _decode(part)
            elif ct == "text/html" and not html:
                html = _decode(part)
    else:
        if msg.get_content_type() == "text/html":
            html = _decode(msg)
        else:
            plain = _decode(msg)
    text = plain or re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _msg_epoch(msg: email.message.Message) -> int | None:
    try:
        dt = email.utils.parsedate_to_datetime(msg.get("Date", ""))
        return int(dt.timestamp()) if dt else None
    except Exception:
        return None


def _connect() -> imaplib.IMAP4_SSL:
    if env("EMAIL_BACKEND").casefold() == "gmail_api":
        from .gmail_api import GmailAPIConnection

        return GmailAPIConnection()
    host = env("EMAIL_IMAP_HOST", DEFAULT_HOST)
    address = env("EMAIL_ADDRESS")
    app_password = env("EMAIL_APP_PASSWORD")
    conn = imaplib.IMAP4_SSL(host, 993)
    conn.login(address, app_password)
    return conn


def verify_connection() -> None:
    """Read-only check: can we log in and see the inbox? Prints a clear
    diagnosis and exits nonzero on failure. Selects INBOX read-only, sends no
    search, marks nothing read — safe to run anytime."""
    if env("EMAIL_BACKEND").casefold() == "gmail_api":
        from .gmail_api import GmailAPIConnection, configured

        if not configured():
            print("FAIL: Gmail API refresh credentials are not configured.")
            raise SystemExit(1)
        try:
            conn = GmailAPIConnection()
            typ, data = conn.select("INBOX", readonly=True)
            count = int(data[0]) if typ == "OK" and data and data[0] else "?"
            print(f"OK: Gmail API read-only inbox access is armed ({count} messages).")
        finally:
            if "conn" in locals():
                conn.logout()
        return
    address = env("EMAIL_ADDRESS")
    app_password = env("EMAIL_APP_PASSWORD")
    if not address or not app_password:
        print("FAIL: EMAIL_ADDRESS / EMAIL_APP_PASSWORD secrets are not set on this repo.")
        raise SystemExit(1)
    host = env("EMAIL_IMAP_HOST", DEFAULT_HOST)
    try:
        conn = imaplib.IMAP4_SSL(host, 993)
    except Exception as e:
        print(f"FAIL: could not reach {host}:993 — {e}")
        raise SystemExit(1) from e
    try:
        conn.login(address, app_password)
    except imaplib.IMAP4.error as e:
        print(f"FAIL: login rejected by {host}: {e}. Fix: confirm 2-Step Verification is "
              "enabled on the account and generate a fresh App Password at "
              "myaccount.google.com/apppasswords. If this is a Google Workspace / school "
              "account, its admin may have App Passwords disabled entirely — see README "
              "for the personal-Gmail-forwarding fallback.")
        raise SystemExit(1) from e
    try:
        typ, data = conn.select("INBOX", readonly=True)
        count = int(data[0]) if typ == "OK" and data and data[0] else "?"
        print(f"OK: logged in to {address} via {host}, INBOX visible ({count} messages). "
              "Email-based applied-detection is armed.")
    finally:
        with suppress(Exception):
            conn.logout()


def _advance(entry: dict, target: str, when: int | None) -> bool:
    """Move an applied entry to a later pipeline stage (never backward).
    Sets .stage (drives Notion) + .status + .responded_at. Returns True if it
    actually changed."""
    if not can_advance(entry.get("stage"), target):
        return False
    entry["stage"] = target
    entry["status"] = target
    entry["responded_at"] = when or int(time.time())
    entry["stage_changed_at"] = entry["responded_at"]
    return True


def _autoclose(applied: list, now: int, days: int) -> int:
    """Applications sitting at 'applied' with no response for `days` → CLOSED."""
    cutoff = now - days * 86400
    closed = 0
    for e in applied:
        if (e.get("stage") == "applied" and not e.get("responded_at")
                and e.get("applied_at", now) < cutoff):
            e["stage"] = "closed"
            e["status"] = "closed"
            e["stage_changed_at"] = now
            e["auto_closed"] = True
            closed += 1
    return closed


def run(lookback_days: int | None = None) -> dict:
    """Run one bounded, idempotent email lifecycle pass."""
    gmail_api = env("EMAIL_BACKEND").casefold() == "gmail_api"
    address = env("EMAIL_ADDRESS")
    app_password = env("EMAIL_APP_PASSWORD")
    if gmail_api:
        from .gmail_api import configured as gmail_configured

        ready = gmail_configured()
    else:
        ready = bool(address and app_password)
    if not ready:
        required = (
            "GMAIL_REFRESH_TOKEN and Google OAuth credentials"
            if gmail_api
            else "EMAIL_ADDRESS/EMAIL_APP_PASSWORD"
        )
        print(f"email_watch: {required} not set — skipping "
              "(applications must be logged manually via applied <url> for now)")
        return {"checked": 0, "matched": 0, "synced": 0}

    if lookback_days is None:
        try:
            lookback_days = int(env("RADAR_EMAIL_LOOKBACK_DAYS", str(DEFAULT_LOOKBACK_DAYS)))
        except ValueError:
            lookback_days = DEFAULT_LOOKBACK_DAYS
    lookback_days = max(1, min(90, lookback_days))

    from .config import profile
    host = env("EMAIL_IMAP_HOST", DEFAULT_HOST)
    seen = state.load("email_watch.json", {"seen_message_ids": [], "last_checked_at": 0})
    seen_ids = set(seen.get("seen_message_ids", []))

    conn = _connect()
    try:
        conn.select("INBOX", readonly=True)
        uids = _search_candidate_uids(conn, host, lookback_days)
        shortlist = state.shortlist()
        jobs = state.jobs()
        applied = state.applied()
        fb = state.feedback()
        review = state.load("email_review.json", [])
        counts = {"confirmation": 0, "oa": 0, "interview": 0, "rejected": 0}

        for uid in uids:
            msg = _fetch_full(conn, uid) or _fetch_headers(conn, uid)
            if msg is None:
                continue
            msg_id = _header(msg, "Message-ID") or (
                f"uid-{uid.decode() if isinstance(uid, bytes) else uid}"
            )
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            subject = _header(msg, "Subject")
            body = _body_text(msg)
            kind = classify(f"{subject}\n{body}")
            if kind is None:
                continue

            candidates = guess_company_candidates(msg, body)
            if not candidates:
                review.append({
                    "message_id": msg_id, "kind": kind, "subject": subject[:240],
                    "from": _header(msg, "From")[:240],
                    "reason": "no employer candidate", "seen_at": int(time.time()),
                })
                continue

            if kind == "confirmation":
                job = (
                    match_application(candidates, subject, body, shortlist)
                    or match_application(candidates, subject, body, list(jobs.values()))
                    or match_company(candidates, shortlist, jobs)
                    or _synthetic_job(candidates[0], subject)
                )
                if record_applied(job, applied, fb, via="email"):
                    counts["confirmation"] += 1
                    url = canonical_url(job.get("url"))
                    shortlist[:] = [
                        s for s in shortlist
                        if s.get("id") != job.get("id")
                        and not (url and canonical_url(s.get("url")) == url)
                    ]
                continue

            # Search applied first, then saved roles and known radar postings.
            # This closes the old gap where an untracked rejection was lost.
            match = match_application(candidates, subject, body, applied)
            changed = _advance(match, kind, _msg_epoch(msg)) if match else False
            if not match or not changed:
                match = (
                    match_application(candidates, subject, body, shortlist)
                    or match_application(candidates, subject, body, list(jobs.values()))
                )
                if match:
                    changed = record_applied(match, applied, fb, via="email", stage=kind)
                    url = canonical_url(match.get("url"))
                    shortlist[:] = [
                        s for s in shortlist
                        if s.get("id") != match.get("id")
                        and not (url and canonical_url(s.get("url")) == url)
                    ]
            if changed:
                counts[kind] += 1
            elif not match:
                review.append({
                    "message_id": msg_id, "kind": kind, "subject": subject[:240],
                    "from": _header(msg, "From")[:240],
                    "reason": "employer matched ambiguously or role was not in radar",
                    "seen_at": int(time.time()),
                })

        closed = _autoclose(
            applied, int(time.time()),
            int(profile()["notion"].get("autoclose_days", 45)),
        )
        synced = sync_applied(applied)
        state.save("applied.json", applied)
        state.save("shortlist.json", shortlist)
        state.save("feedback.json", fb)
        state.save("email_review.json", review[-200:])
        state.save("email_watch.json", {
            "seen_message_ids": list(seen_ids)[-MAX_SEEN_IDS:],
            "last_checked_at": int(time.time()),
        })
        if hasattr(conn, "commit"):
            conn.commit()
        matched = sum(counts.values())
        print(
            f"email_watch: checked {len(uids)} email(s) — "
            f"{counts['confirmation']} applied, {counts['interview']} interview, "
            f"{counts['oa']} OA, {counts['rejected']} rejected, {closed} auto-closed; "
            f"{len(review[-200:])} review item(s), synced {synced} to Notion"
        )
        return {
            "checked": len(uids), "matched": matched, "closed": closed,
            "synced": synced, "review": len(review[-200:]), **counts,
        }
    finally:
        with suppress(Exception):
            conn.logout()
