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
from datetime import datetime, timedelta, timezone

from . import state
from .applied import record_applied
from .config import env
from .models import norm
from .notion_sync import sync_applied

DEFAULT_HOST = "imap.gmail.com"
DEFAULT_LOOKBACK_DAYS = 5
MAX_SEEN_IDS = 4000

CONFIRMATION_RE = re.compile(
    r"thank(?:s| you) for (?:your interest in |applying|your application)|"
    r"application (?:has been )?(?:received|submitted|confirmed)|"
    r"we(?:'ve| have) received your application|"
    r"your application (?:to|for|at|has been received)|"
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
    r"chosen to move forward with other", re.I)

OA_RE = re.compile(
    r"online assessment|coding (?:challenge|assessment|test|exercise)|"
    r"hackerrank|codesignal|codility|karat|hirevue|"
    r"take[- ]?home (?:assignment|assessment|challenge)|"
    r"technical (?:assessment|screen(?:ing)?)|assessment (?:invitation|link)|"
    r"complete (?:the|your|a) (?:assessment|challenge)", re.I)

INTERVIEW_RE = re.compile(
    r"interview|schedule (?:a |your |some )?(?:time|call|chat|conversation)|"
    r"phone screen|next steps|(?:like|love) to (?:meet|speak|chat|connect) with|"
    r"hiring manager|video call|availability (?:for|to)|book (?:a |your )?time|"
    r"set up (?:a |some )?time|move(?:d)? (?:you )?(?:to|forward to|onto) the", re.I)


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
STAGE_ORDER = {"saved": 0, "applied": 1, "oa": 2, "interview": 3,
               "offered": 8, "signed": 9, "rejected": 9, "closed": 9}


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


def guess_company_candidates(msg: email.message.Message) -> list[str]:
    """Best-effort list of candidate company names, best guess first."""
    candidates = []
    name, addr = email.utils.parseaddr(msg.get("From", ""))
    cleaned_name = _clean_name(name)
    if cleaned_name and norm(cleaned_name):
        candidates.append(cleaned_name)

    domain = addr.split("@")[-1].lower() if "@" in addr else ""
    parts = domain.split(".")
    domain_root = parts[-2] if len(parts) >= 2 else domain
    if domain and domain not in ATS_DOMAINS and domain_root not in {"gmail", "outlook", "yahoo"}:
        candidates.append(domain_root.replace("-", " ").title())

    subject = str(msg.get("Subject", "") or "")
    m = re.search(r"(?:at|to|with|from)\s+([A-Z][\w&.,' -]{1,40}?)(?:[!.]|\s+[-|]|\s*$)", subject)
    if m:
        candidates.append(m.group(1).strip())

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
            r = _token_overlap(cand, item[key])
            if r > best_score:
                best_score, best_item = r, item
    return best_score, best_item


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


def _synthetic_job(company: str) -> dict:
    sid = hashlib.sha1(f"email-detected|{norm(company)}|{int(time.time())}".encode()).hexdigest()[:16]
    return {"id": sid, "company": company, "title": "Application (auto-detected via email)",
            "url": "", "locations": [], "score": None, "source": "email-detected"}


def _search_candidate_uids(conn: imaplib.IMAP4_SSL, host: str, lookback_days: int) -> list[bytes]:
    if "gmail" in host.lower():
        # bare terms (no subject:) search the whole message, so rejection /
        # interview / assessment language in the body is caught too.
        query = (
            f'newer_than:{lookback_days}d ('
            'subject:"thank you for applying" OR subject:"thanks for applying" OR '
            'subject:"application received" OR subject:"your application" OR '
            'subject:"application confirmation" OR subject:"applying to" OR '
            'subject:"update on your" OR subject:"regarding your application" OR '
            'subject:interview OR subject:assessment OR subject:"next steps" OR '
            '"successfully applied" OR "unfortunately" OR "move forward with other" OR '
            '"regret to inform" OR "online assessment" OR "coding challenge" OR '
            '"schedule a" OR "we received your application")'
        )
        typ, data = conn.search(None, "X-GM-RAW", f'"{query}"')
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
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
        raise SystemExit(1)
    try:
        conn.login(address, app_password)
    except imaplib.IMAP4.error as e:
        print(f"FAIL: login rejected by {host}: {e}. Fix: confirm 2-Step Verification is "
              "enabled on the account and generate a fresh App Password at "
              "myaccount.google.com/apppasswords. If this is a Google Workspace / school "
              "account, its admin may have App Passwords disabled entirely — see README "
              "for the personal-Gmail-forwarding fallback.")
        raise SystemExit(1)
    try:
        typ, data = conn.select("INBOX", readonly=True)
        count = int(data[0]) if typ == "OK" and data and data[0] else "?"
        print(f"OK: logged in to {address} via {host}, INBOX visible ({count} messages). "
              "Email-based applied-detection is armed.")
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _advance(entry: dict, target: str, when: int | None) -> bool:
    """Move an applied entry to a later pipeline stage (never backward).
    Sets .stage (drives Notion) + .status + .responded_at. Returns True if it
    actually changed."""
    if not can_advance(entry.get("stage"), target):
        return False
    entry["stage"] = target
    entry["status"] = target
    entry["responded_at"] = when or int(time.time())
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
            e["auto_closed"] = True
            closed += 1
    return closed


def run(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    address = env("EMAIL_ADDRESS")
    app_password = env("EMAIL_APP_PASSWORD")
    if not address or not app_password:
        print("email_watch: EMAIL_ADDRESS/EMAIL_APP_PASSWORD not set — skipping "
              "(applications must be logged manually via `applied <url>` for now)")
        return {"checked": 0, "matched": 0, "synced": 0}

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
        counts = {"confirmation": 0, "oa": 0, "interview": 0, "rejected": 0}

        for uid in uids:
            msg = _fetch_full(conn, uid) or _fetch_headers(conn, uid)
            if msg is None:
                continue
            msg_id = msg.get("Message-ID") or f"uid-{uid.decode() if isinstance(uid, bytes) else uid}"
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            subject = msg.get("Subject", "") or ""
            kind = classify(f"{subject}\n{_body_text(msg)}")
            if kind is None:
                continue

            candidates = guess_company_candidates(msg)
            if not candidates:
                continue

            if kind == "confirmation":
                job = match_company(candidates, shortlist, jobs) or _synthetic_job(candidates[0])
                if record_applied(job, applied, fb, via="email"):
                    counts["confirmation"] += 1
                    shortlist[:] = [s for s in shortlist if s["id"] != job["id"]]
            else:
                # a response (oa/interview/rejection) only makes sense for a job
                # already in the pipeline — match against applied, not shortlist
                mscore, match = _best_match(candidates, applied) if applied else (0.0, None)
                if match and mscore >= 0.5 and _advance(match, kind, _msg_epoch(msg)):
                    counts[kind] += 1

        closed = _autoclose(applied, int(time.time()),
                            int(profile()["notion"].get("autoclose_days", 45)))

        synced = sync_applied(applied)
        state.save("applied.json", applied)
        state.save("shortlist.json", shortlist)
        state.save("feedback.json", fb)
        seen_ids_list = list(seen_ids)[-MAX_SEEN_IDS:]
        state.save("email_watch.json", {"seen_message_ids": seen_ids_list,
                                        "last_checked_at": int(time.time())})
        matched = sum(counts.values())
        print(f"email_watch: checked {len(uids)} email(s) — "
              f"{counts['confirmation']} applied, {counts['interview']} interview, "
              f"{counts['oa']} OA, {counts['rejected']} rejected, {closed} auto-closed; "
              f"synced {synced} to Notion")
        return {"checked": len(uids), "matched": matched, "closed": closed,
                "synced": synced, **counts}
    finally:
        try:
            conn.logout()
        except Exception:
            pass
