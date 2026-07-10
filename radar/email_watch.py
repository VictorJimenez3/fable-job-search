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

    subject = msg.get("Subject", "") or ""
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
        query = (
            f'newer_than:{lookback_days}d ('
            'subject:"thank you for applying" OR subject:"thanks for applying" OR '
            'subject:"application received" OR subject:"we received your application" OR '
            'subject:"your application" OR subject:"application confirmation" OR '
            'subject:"applying to" OR subject:"application to" OR '
            'subject:"successfully applied")'
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


def run(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    address = env("EMAIL_ADDRESS")
    app_password = env("EMAIL_APP_PASSWORD")
    if not address or not app_password:
        print("email_watch: EMAIL_ADDRESS/EMAIL_APP_PASSWORD not set — skipping "
              "(applications must be logged manually via `applied <url>` for now)")
        return {"checked": 0, "matched": 0, "synced": 0}

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
        matched = 0

        for uid in uids:
            msg = _fetch_headers(conn, uid)
            if msg is None:
                continue
            msg_id = msg.get("Message-ID") or f"uid-{uid.decode() if isinstance(uid, bytes) else uid}"
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            subject = msg.get("Subject", "") or ""
            if not CONFIRMATION_RE.search(subject):
                continue

            candidates = guess_company_candidates(msg)
            if not candidates:
                continue

            job = match_company(candidates, shortlist, jobs) or _synthetic_job(candidates[0])
            if record_applied(job, applied, fb, via="email"):
                matched += 1
                shortlist[:] = [s for s in shortlist if s["id"] != job["id"]]

        synced = sync_applied(applied)
        state.save("applied.json", applied)
        state.save("shortlist.json", shortlist)
        state.save("feedback.json", fb)
        seen_ids_list = list(seen_ids)[-MAX_SEEN_IDS:]
        state.save("email_watch.json", {"seen_message_ids": seen_ids_list,
                                        "last_checked_at": int(time.time())})
        print(f"email_watch: checked {len(uids)} candidate email(s), matched {matched} "
              f"application(s), synced {synced} to Notion")
        return {"checked": len(uids), "matched": matched, "synced": synced}
    finally:
        try:
            conn.logout()
        except Exception:
            pass
