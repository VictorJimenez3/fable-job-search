"""Job-quality pass: link liveness + LLM new-grad / role-fit verification.

Runs inside `enrich` (the Mac companion, or any configured LLM provider).
Layered, cheapest first — ROADMAP "Job-quality LLM pass":

  Layer 0  HTTP liveness: a posting that 404s or says "no longer accepting
           applications" is closed (closed_at stamped, alert_ok dropped).
Layer 1  New-grad verification: one JSON question against the posting
           text — a "new grad" board listing that actually wants 1+ years
           loses alert eligibility.
  Layer 2  Role-fit: right company, wrong role (sales/support/analyst-in-
           name-only) is demoted below the alert bar.

Design rules (DECISIONS #30, amended by #31):
- Verdicts adjust alert_ok/score with a reason logged in score_reasons —
  nothing is ever silently deleted from state.
- Marquee companies are suppressed by verdicts like everyone else since
  DECISIONS #31: the Shams rule bypasses only the new-grad-wording
  requirement, not field fit or verified seniority.
- The verdict is cached on the job record (rec["quality"]) so each job costs
  at most `_MAX_ATTEMPTS` fetch+LLM calls, and `reapply()` re-applies stored
  verdicts after every re-score (score() rebuilds from scratch).
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit

from . import http, lifecycle, llm
from .config import env

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
_PENALTY_DEGREE_MISMATCH = 45

PROMPT = """You screen job postings for a computer-science new grad (class of 2026, US).
Job: {title} — {company}
Posting text (may be truncated or noisy):
---
{text}
---
Answer STRICT JSON only, no other text:
{{"years_required": <integer or null>, "degree_required": "bachelors"|"masters"|"phd"|"unclear", "new_grad": "yes"|"no"|"unclear", "role_family": "swe"|"data"|"ml"|"security"|"other-technical"|"non-technical", "reason": "<15 words max>"}}
Rules: new_grad="no" if the text clearly requires 1+ years of professional
experience or is explicitly senior/staff/lead. Internships are new_grad="no".
degree_required must reflect an explicitly required degree, not a preferred or
nice-to-have degree. A master's/PhD requirement is a major mismatch because
the candidate profile has a bachelor's degree.
role_family="non-technical" means sales, support, recruiting, marketing,
project coordination — not an engineering/data/science role."""


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _free_ats_api(url: str) -> tuple[bool | None, str] | None:
    """Try public ATS JSON endpoints before generic HTML scraping.

    These endpoints require no key and usually expose the complete JD even
    when a browser page is a JavaScript shell or bot-protected. ``None`` means
    the URL is not one of these ATS shapes or the API was inconclusive, so the
    caller can still try the public page.
    """
    parts = urlsplit(url.split("?")[0].split("#")[0])
    bits = [b for b in parts.path.split("/") if b]
    try:
        if ("greenhouse.io" in parts.netloc and "jobs" in bits
                and bits.index("jobs") + 1 < len(bits)):
            board = bits[bits.index("jobs") - 1]
            job_id = bits[bits.index("jobs") + 1]
            api = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
            r = http.get(api, timeout=15, headers={"Accept": "application/json"})
            if r.status_code in (404, 410):
                return None
            if r.status_code >= 400:
                return None
            body = r.json()
            return True, _strip_html(" ".join(str(body.get(k) or "")
                                              for k in ("content", "title", "location")))[:7000]
        if "ashbyhq.com" in parts.netloc and len(bits) >= 2:
            api = f"https://api.ashbyhq.com/posting/{bits[-1]}"
            r = http.get(api, timeout=15, headers={"Accept": "application/json"})
            if r.status_code >= 400:
                return None
            body = r.json()
            return True, _strip_html(" ".join(str(body.get(k) or "")
                                              for k in ("descriptionHtml", "description", "title")))[:7000]
        if "lever.co" in parts.netloc and len(bits) >= 2:
            api = f"https://api.lever.co/v0/postings/{bits[-2]}/{bits[-1]}?mode=json"
            r = http.get(api, timeout=15, headers={"Accept": "application/json"})
            if r.status_code >= 400:
                return None
            body = r.json()
            text = str(body.get("descriptionPlain") or body.get("description") or "")
            for item in body.get("lists") or []:
                text += " " + str(item.get("content") or "")
            return True, _strip_html(text)[:7000]
    except Exception:
        return None
    return None


def fetch_posting(url: str) -> tuple[bool | None, str]:
    """Returns (alive, text). alive=None means "couldn't tell" (network
    hiccup, bot wall) — callers must treat that as alive; only a definitive
    dead signal closes a job."""
    api_result = _free_ats_api(url)
    if api_result and len(api_result[1]) >= 200:
        return api_result
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


_WD_URL = re.compile(
    r"^https://(?P<tenant>[^.]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/]+)(?P<path>/job/[^?#]+)")
_ORC_URL = re.compile(
    r"^https://(?P<host>[^/]+)/hcmUI/CandidateExperience/[^/]+/sites/"
    r"(?P<site>[^/]+)/(?:job|requisitions/preview)/(?P<rid>\d+)")


def spa_kind(rec: dict) -> str | None:
    """Which JSON-API strategy covers this JS-shell posting, if any."""
    url = rec.get("url") or ""
    if ".myworkdayjobs.com" in url:
        return "workday"
    if "oraclecloud.com" in url and "/hcmUI/" in url:
        return "oracle"
    if (rec.get("ats") == "eightfold" or rec.get("source") == "eightfold"
            or ".eightfold.ai" in url):
        return "eightfold"
    return None


def fetch_posting_spa(rec: dict, domains: dict | None = None) -> tuple[bool | None, str]:
    """Posting text for SPA hosts (Workday/Oracle ORC/Eightfold) via the same
    JSON APIs their frontends call — a plain GET only returns the JS shell.
    Same contract as fetch_posting: (alive, text), alive=None = can't tell."""
    url = (rec.get("url") or "").split("?")[0].split("#")[0]
    kind = spa_kind(rec)
    try:
        if kind == "workday":
            m = _WD_URL.match(url)
            if not m:
                return None, ""
            api = (f"https://{m['tenant']}.{m['wd']}.myworkdayjobs.com"
                   f"/wday/cxs/{m['tenant']}/{m['site']}{m['path']}")
            r = http.get(api, timeout=15, headers={"Accept": "application/json"})
            if r.status_code in (404, 410):
                return False, ""
            if r.status_code >= 400:
                return None, ""
            info = r.json().get("jobPostingInfo") or {}
            return True, _strip_html(info.get("jobDescription") or "")[:7000]
        if kind == "oracle":
            m = _ORC_URL.match(url)
            if not m:
                return None, ""
            api = (f"https://{m['host']}/hcmRestApi/resources/latest/"
                   f"recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                   f"&finder=ById;siteNumber={m['site']},Id=%22{m['rid']}%22")
            r = http.get(api, timeout=15, headers={"Accept": "application/json"})
            if r.status_code in (404, 410):
                return False, ""
            if r.status_code >= 400:
                return None, ""
            items = r.json().get("items") or []
            if not items:
                return False, ""    # requisition no longer served = closed
            text = " ".join(_strip_html(items[0].get(k) or "") for k in
                            ("ExternalDescriptionStr", "ExternalQualificationsStr",
                             "ExternalResponsibilitiesStr", "CorporateDescriptionStr"))
            return True, text.strip()[:7000]
        if kind == "eightfold":
            from urllib.parse import urlsplit
            parts = urlsplit(url)
            pid = parts.path.rstrip("/").rsplit("/", 1)[-1]
            if not pid.isdigit():
                return None, ""
            from .models import norm
            domain = (domains or {}).get(norm(rec.get("company", "")))
            api = f"{parts.scheme}://{parts.netloc}/api/apply/v2/jobs/{pid}"
            if domain:
                api += f"?domain={domain}"
            r = http.get(api, timeout=15, headers={"Accept": "application/json"})
            if r.status_code in (404, 410):
                return False, ""
            if r.status_code >= 400:
                return None, ""
            return True, _strip_html(r.json().get("job_description") or "")[:7000]
    except Exception:
        return None, ""
    return None, ""


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
    if v.get("degree_required") not in ("bachelors", "masters", "phd", "unclear", None):
        return None
    if v.get("role_family") not in ("swe", "data", "ml", "security",
                                      "other-technical", "non-technical"):
        return None
    years = v.get("years_required")
    if years is not None and (not isinstance(years, int) or isinstance(years, bool)
                              or not 0 <= years <= 50):
        return None
    if not isinstance(v.get("reason", ""), str):
        return None
    return v


def _effects(q: dict) -> list[tuple[str, int, bool]]:
    """(reason_line, score_penalty, suppress_alert) for a stored verdict."""
    out = []
    # Deterministic posting extraction owns this penalty when usable text was
    # retrieved; avoid double-counting the same requirement from the LLM.
    posting = q.get("_posting") or {}
    degree = q.get("degree_required")
    if degree in ("masters", "phd") and not posting.get("education_mismatch"):
        penalty = 60 if degree == "phd" else _PENALTY_DEGREE_MISMATCH
        label = "PhD" if degree == "phd" else "master's degree"
        out.append((f"quality: requires {label} (bachelor's profile) -{penalty}",
                    penalty, True))
    if q.get("new_grad") == "no":
        yrs = q.get("years_required")
        if not posting.get("years_min"):
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
    if q.get("live") is False:
        rec["alert_ok"] = False
        rec.setdefault("closed_at", q.get("checked_at", int(time.time())))
        reasons = rec.setdefault("score_reasons", [])
        line = "quality: posting gone (link checked)"
        if line not in reasons:
            reasons.append(line)
        lifecycle.mark_terminal(
            rec, q.get("dead_status") or lifecycle.EXPIRED,
            q.get("checked_at", int(time.time())), line)
        return
    reasons = rec.setdefault("score_reasons", [])
    q["_posting"] = rec.get("posting") or {}
    for line, penalty, suppress in _effects(q):
        if line not in reasons:
            reasons.append(line)
            rec["score"] = max(0, rec.get("score", 0) - penalty)
        if suppress:
            rec["alert_ok"] = False


def verify(rec: dict, domains: dict | None = None) -> bool:
    """Fetch + judge one job. Stores the verdict in rec["quality"] and applies
    it. Returns True if a verdict (even 'unclear') was recorded."""
    now = int(time.time())
    q = rec.setdefault("quality", {})
    q["attempts"] = q.get("attempts", 0) + 1
    if spa_kind(rec):
        alive, text = fetch_posting_spa(rec, domains)
    else:
        alive, text = fetch_posting(rec.get("url", ""))
    if alive is False:
        q.update({"checked_at": now, "live": False,
                  "dead_status": lifecycle.status_from_dead_text(text)})
        reapply(rec)
        return True
    if alive is None or len(text) < 200:
        q["fetch_status"] = "unavailable" if alive is None else "unreadable"
        q["fetch_note"] = ("could not retrieve usable posting text" if alive is None
                            else "page loaded without enough usable posting text")
        # can't judge what we can't read; leave retryable until attempts cap
        if q["attempts"] >= _MAX_ATTEMPTS:
            q.update({"checked_at": now, "new_grad": "unclear"})
            return True
        return False
    # deterministic facts first — they need no LLM and stick even when the
    # verdict below fails (no provider / rate limit / garbage output)
    from . import posting as posting_mod
    posting_mod.apply_record(rec, posting_mod.analyze(text), fetched=True, now=now)
    raw = llm.complete(
        PROMPT.format(title=rec.get("title", ""), company=rec.get("company", ""),
                      text=text[:6000]),
        max_tokens=240, timeout=120, json_mode=True, task="quality",
        validator=lambda text: _parse_verdict(text) is not None)
    v = _parse_verdict(raw)
    if v is None:
        # A provider outage/budget skip is not evidence about the posting and
        # must not permanently consume one of its two semantic attempts.
        if raw is None:
            q["attempts"] = max(0, q.get("attempts", 1) - 1)
            q["ai_deferred_at"] = now
            return False
        if q["attempts"] >= _MAX_ATTEMPTS:
            q.update({"checked_at": now, "new_grad": "unclear"})
            return True
        return False
    q.update({
        "checked_at": now, "live": True,
        "fetch_status": "readable",
        "new_grad": v.get("new_grad"),
        "years_required": v.get("years_required"),
        "degree_required": v.get("degree_required") or "unclear",
        "role_family": v.get("role_family"),
        "reason": (v.get("reason") or "")[:120],
    })
    reapply(rec)
    return True


def verify_pasted(rec: dict, jd_text: str) -> bool:
    """Judge a job from a user-pasted description (the platform's JD box in
    state/web_state.json) — for postings the fetch layer can't read (SPA
    hosts, bot walls). Idempotent per paste: the text's hash is stored as
    quality.jd_sha, so an unchanged paste never burns another LLM call. A
    pasted verdict overwrites a fetched one (fresher, human-supplied text;
    logged via source="pasted"). Returns True when a verdict was recorded."""
    import hashlib
    jd_text = (jd_text or "").strip()
    if len(jd_text) < 100:                 # too short to judge
        return False
    sha = hashlib.sha1(jd_text.encode()).hexdigest()[:12]
    q = rec.setdefault("quality", {})
    if q.get("jd_sha") == sha:
        return False
    from . import posting as posting_mod
    posting_mod.apply_record(rec, posting_mod.analyze(jd_text), fetched=False)
    raw = llm.complete(
        PROMPT.format(title=rec.get("title", ""), company=rec.get("company", ""),
                      text=jd_text[:6000]),
        max_tokens=240, timeout=120, json_mode=True, task="pasted_jd",
        validator=lambda text: _parse_verdict(text) is not None)
    v = _parse_verdict(raw)
    if v is None:
        return False
    q.update({
        "checked_at": int(time.time()), "live": True,
        "fetch_status": "readable",
        "jd_sha": sha, "source": "pasted",
        "new_grad": v.get("new_grad"),
        "years_required": v.get("years_required"),
        "role_family": v.get("role_family"),
        "reason": (v.get("reason") or "")[:120],
    })
    reapply(rec)
    return True


# aggregator listings are where verification pays: their links go stale and
# their "new grad" labels are the least reliable. Direct-ATS jobs are
# re-confirmed alive every poll and usually say "new grad" in the title.
AGGREGATOR_SOURCES = ("jobright", "simplify", "speedyapply", "vansh", "hn")


def _candidates(jobs_state: dict, now: int,
                priority_ids: set[str] | None = None) -> list[dict]:
    """Alert-worthy recent jobs, most visible + least trustworthy first —
    these are the ones on the master board / alerts, where a bad entry costs
    real application time. SPA hosts (Workday/Oracle/Eightfold) are included
    since 2026-07-16 — fetch_posting_spa reads them via their JSON APIs."""
    priority_ids = priority_ids or set()
    minimum = int(env("RADAR_QUALITY_MIN_SCORE", "60"))
    rows = [r for r in jobs_state.values()
            if r.get("alert_ok")
            and r.get("score", 0) >= minimum
            and not lifecycle.is_terminal(r)
            and now - r.get("first_seen", 0) <= 30 * 86400]
    rows.sort(key=lambda r: (
        r.get("id") not in priority_ids,
        now - r.get("first_seen", 0) > 72 * 3600,
        r.get("source") not in AGGREGATOR_SOURCES,
        -r.get("score", 0),
        -(r.get("posted_at") or r.get("first_seen") or 0),
    ))
    return rows


def run(jobs_state: dict, limit: int | None = None,
        domains: dict | None = None,
        priority_ids: set[str] | None = None) -> tuple[int, int]:
    """One quality cycle: re-apply all stored verdicts, then verify up to
    `limit` unverified jobs. `domains` maps norm(company) → careers domain
    (Eightfold's detail API wants it; built from the registry in enrich).
    Returns (reapplied, newly_verified)."""
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
    for rec in _candidates(jobs_state, now, priority_ids):
        if tried >= limit:
            break
        q = rec.get("quality") or {}
        if q.get("checked_at") or q.get("attempts", 0) >= _MAX_ATTEMPTS:
            continue
        tried += 1
        if verify(rec, domains):
            verified += 1
        time.sleep(0.4)   # politeness between posting fetches
    return reapplied, verified
