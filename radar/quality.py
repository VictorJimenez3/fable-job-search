"""Job-quality pass: link liveness + LLM new-grad / role-fit verification.

Runs inside `enrich` (the Mac companion, or any configured LLM provider).
Layered, cheapest first — ROADMAP "Job-quality LLM pass":

  Layer 0  HTTP liveness: a posting that 404s or says "no longer accepting
           applications" is closed (closed_at stamped, alert_ok dropped).
  Layer 1  New-grad verification: one JSON question against the posting
           text — a "new grad" board listing that actually wants 3+ years
           loses alert eligibility.
  Layer 2  Role-fit: right company, wrong role (sales/support/analyst-in-
           name-only) is demoted below the alert bar.

Design rules (DECISIONS #30):
- Verdicts adjust alert_ok/score with a reason logged in score_reasons —
  nothing is ever silently deleted from state.
- Marquee companies (the Shams rule) are never alert-suppressed by an LLM
  verdict; the verdict is stored as information only. A dead link closes
  them like anyone else.
- The verdict is cached on the job record (rec["quality"]) so each job costs
  at most `_MAX_ATTEMPTS` fetch+LLM calls, and `reapply()` re-applies stored
  verdicts after every re-score (score() rebuilds from scratch).
"""
from __future__ import annotations

import json
import re
import time

from . import http, llm
from .config import env
from .score import is_marquee

# phrases that mean the posting is gone even when the page returns 200
DEAD_PHRASES = (
    "no longer accepting applications",
    "this job is no longer available",
    "this position is no longer available",
    "job you are looking for is no longer open",
    "position has been filled",
    "posting has expired",
    "this job has closed",
    "job posting has closed",
    "job not found",
)

_MAX_ATTEMPTS = 2          # LLM/fetch tries per job before marking unclear
_PENALTY_NOT_NEW_GRAD = 20
_PENALTY_WRONG_ROLE = 25
_PENALTY_SOME_EXPERIENCE = 10

PROMPT = """You screen job postings for a computer-science new grad (class of 2026, US).
Job: {title} — {company}
Posting text (may be truncated or noisy):
---
{text}
---
Answer STRICT JSON only, no other text:
{{"years_required": <integer or null>, "new_grad": "yes"|"no"|"unclear", "role_family": "swe"|"data"|"ml"|"security"|"other-technical"|"non-technical", "reason": "<15 words max>"}}
Rules: new_grad="no" ONLY if the text clearly requires 3+ years of professional
experience or is explicitly senior/staff/lead. Internships are new_grad="no".
role_family="non-technical" means sales, support, recruiting, marketing,
project coordination — not an engineering/data/science role."""


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def fetch_posting(url: str) -> tuple[bool | None, str]:
    """Returns (alive, text). alive=None means "couldn't tell" (network
    hiccup, bot wall) — callers must treat that as alive; only a definitive
    dead signal closes a job."""
    try:
        r = http.get(url, timeout=15, allow_redirects=True)
    except Exception:
        return None, ""
    if r.status_code in (404, 410):
        return False, ""
    if r.status_code >= 400:
        return None, ""
    text = _strip_html(r.text)[:7000]
    low = text.lower()
    if any(p in low for p in DEAD_PHRASES):
        return False, text
    return True, text


def _parse_verdict(raw: str | None) -> dict | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except ValueError:
        return None
    if v.get("new_grad") not in ("yes", "no", "unclear"):
        return None
    return v


def _effects(q: dict) -> list[tuple[str, int, bool]]:
    """(reason_line, score_penalty, suppress_alert) for a stored verdict."""
    out = []
    if q.get("new_grad") == "no":
        yrs = q.get("years_required")
        if yrs is not None and 0 < yrs < 3:
            # the LLM is stricter than the house rule (hard gate is 3+ yrs):
            # 1-2 yrs postings are still worth a new grad's application —
            # rank them lower, but never hide them
            out.append((f"quality: wants {yrs}+ yrs (LLM-verified) "
                        f"-{_PENALTY_SOME_EXPERIENCE}",
                        _PENALTY_SOME_EXPERIENCE, False))
        else:
            out.append((f"quality: not new-grad ({f'{yrs}+ yrs' if yrs else 'senior'}"
                        f" — LLM-verified) -{_PENALTY_NOT_NEW_GRAD}",
                        _PENALTY_NOT_NEW_GRAD, True))
    if q.get("role_family") == "non-technical":
        out.append((f"quality: non-technical role ({(q.get('reason') or '')[:40]}"
                    f" — LLM-verified) -{_PENALTY_WRONG_ROLE}",
                    _PENALTY_WRONG_ROLE, True))
    return out


def reapply(rec: dict) -> None:
    """Re-apply a stored verdict after score() rebuilt score/score_reasons.
    Idempotent: the penalty is applied only when its reason line is absent
    (score() wipes the reasons when it rebuilds the score, so penalty and
    reason always travel together — no compounding on jobs that weren't
    re-scored)."""
    q = rec.get("quality")
    if not q:
        return
    marquee = is_marquee(rec.get("company", ""))
    if q.get("live") is False:
        rec["alert_ok"] = False
        rec.setdefault("closed_at", q.get("checked_at", int(time.time())))
        reasons = rec.setdefault("score_reasons", [])
        line = "quality: posting gone (link checked)"
        if line not in reasons:
            reasons.append(line)
        return
    reasons = rec.setdefault("score_reasons", [])
    for line, penalty, suppress in _effects(q):
        if line not in reasons:
            reasons.append(line)
            rec["score"] = max(0, rec.get("score", 0) - penalty)
        if suppress and not marquee:
            rec["alert_ok"] = False


def verify(rec: dict) -> bool:
    """Fetch + judge one job. Stores the verdict in rec["quality"] and applies
    it. Returns True if a verdict (even 'unclear') was recorded."""
    now = int(time.time())
    q = rec.setdefault("quality", {})
    q["attempts"] = q.get("attempts", 0) + 1
    alive, text = fetch_posting(rec.get("url", ""))
    if alive is False:
        q.update({"checked_at": now, "live": False})
        reapply(rec)
        return True
    if alive is None or len(text) < 200:
        # can't judge what we can't read; leave retryable until attempts cap
        if q["attempts"] >= _MAX_ATTEMPTS:
            q.update({"checked_at": now, "new_grad": "unclear"})
            return True
        return False
    raw = llm.complete(
        PROMPT.format(title=rec.get("title", ""), company=rec.get("company", ""),
                      text=text[:6000]),
        max_tokens=300, timeout=120, json_mode=True)
    v = _parse_verdict(raw)
    if v is None:
        if q["attempts"] >= _MAX_ATTEMPTS:
            q.update({"checked_at": now, "new_grad": "unclear"})
            return True
        return False
    q.update({
        "checked_at": now, "live": True,
        "new_grad": v.get("new_grad"),
        "years_required": v.get("years_required"),
        "role_family": v.get("role_family"),
        "reason": (v.get("reason") or "")[:120],
    })
    reapply(rec)
    return True


# JS-shell hosts a plain GET can't read (SPA renders client-side) — fetching
# them wastes the budget, and these are ATS-polled jobs whose liveness the
# crawl re-confirms every ~30 min anyway.
SPA_HOSTS = ("myworkdayjobs.com", "eightfold.ai", "oraclecloud.com")

# aggregator listings are where verification pays: their links go stale and
# their "new grad" labels are the least reliable. Direct-ATS jobs are
# re-confirmed alive every poll and usually say "new grad" in the title.
AGGREGATOR_SOURCES = ("jobright", "simplify", "speedyapply", "vansh", "hn")


def _candidates(jobs_state: dict, now: int) -> list[dict]:
    """Alert-worthy recent jobs, most visible + least trustworthy first —
    these are the ones on the master board / alerts, where a bad entry costs
    real application time."""
    rows = [r for r in jobs_state.values()
            if r.get("alert_ok")
            and not r.get("closed_at")
            and now - r.get("first_seen", 0) <= 30 * 86400
            and not any(h in (r.get("url") or "") for h in SPA_HOSTS)]
    rows.sort(key=lambda r: (r.get("source") not in AGGREGATOR_SOURCES,
                             -r.get("score", 0), -(r.get("posted_at") or 0)))
    return rows


def run(jobs_state: dict, limit: int | None = None) -> tuple[int, int]:
    """One quality cycle: re-apply all stored verdicts, then verify up to
    `limit` unverified jobs. Returns (reapplied, newly_verified)."""
    if env("RADAR_QUALITY_DISABLE"):
        return 0, 0
    limit = int(env("RADAR_QUALITY_LIMIT", "25")) if limit is None else limit
    now = int(time.time())
    reapplied = 0
    for rec in jobs_state.values():
        if rec.get("quality", {}).get("checked_at"):
            reapply(rec)
            reapplied += 1
    # `limit` budgets ATTEMPTS (fetch+LLM work, i.e. cycle time), not
    # successes — an unlucky stretch of unreadable pages must not turn into
    # an unbounded fetch spree (first live cycle: 131 fetches for 15
    # verdicts before this cap).
    tried = verified = 0
    for rec in _candidates(jobs_state, now):
        if tried >= limit:
            break
        q = rec.get("quality") or {}
        if q.get("checked_at") or q.get("attempts", 0) >= _MAX_ATTEMPTS:
            continue
        tried += 1
        if verify(rec):
            verified += 1
        time.sleep(0.4)   # politeness between posting fetches
    return reapplied, verified
