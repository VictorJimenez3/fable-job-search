"""Deterministic posting-text analysis — no LLM, no keys (DECISIONS #35).

The crawl scrapes real posting text in the cloud every ~30 minutes and
extracts the facts Victor kept having to check by hand: does the role
sponsor visas / take international students, and how many years of
experience it actually wants (and whether internships count). Regex-only,
so it runs identically on CI, the Mac, and forks with zero credentials —
the LLM quality pass layers on top when a provider is available.

Stored on the record as rec["posting"]:
  {analyzed_at, fetched (bool), sponsorship: yes|no|unknown,
   sponsorship_note, years_min, years_note, intern_counts}
Alert effects (demote-only, reasons logged, marquee included):
  - years_min >= 3          -> alert_ok False ("wants N+ yrs")
  - sponsorship == "no" and profile candidate.needs_sponsorship
                            -> alert_ok False ("no visa sponsorship")
"""
from __future__ import annotations

import re
import time

from .config import env, profile

# ---------- sponsorship / work authorization ----------

SPONSOR_NO_RE = re.compile(
    r"(not\s+(?:be\s+able\s+to\s+|currently\s+|able\s+to\s+)?sponsor|"
    r"unable\s+to\s+(?:provide\s+|offer\s+)?sponsor|"
    r"cannot\s+(?:provide\s+|offer\s+)?sponsor|"
    r"no\s+(?:visa\s+)?sponsorship|"
    r"sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)|"
    r"will\s+not\s+(?:provide|offer)\s+(?:visa\s+)?sponsorship|"
    r"not\s+eligible\s+for\s+(?:visa\s+)?sponsorship|"
    r"without\s+(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship|"
    r"does\s+not\s+(?:provide|offer)\s+(?:visa\s+|immigration\s+)?sponsorship|"
    r"must\s+be\s+(?:a\s+)?(?:us|u\.s\.)\s+citizen|"
    r"(?:us|u\.s\.)\s+citizens?\s+only|"
    r"citizenship\s+(?:is\s+)?required)", re.I)

SPONSOR_YES_RE = re.compile(
    r"((?:visa\s+)?sponsorship\s+(?:is\s+)?(?:available|offered|provided)|"
    r"willing\s+to\s+sponsor|we\s+(?:will\s+|do\s+)?sponsor|"
    r"can\s+(?:provide|offer)\s+(?:visa\s+)?sponsorship|"
    r"h-?1b\s+sponsorship|"
    r"opt\s*/?\s*cpt\s+(?:welcome|accepted|eligible)|"
    r"international\s+(?:students|candidates)\s+(?:are\s+)?welcome)", re.I)

# ---------- years of experience ----------

_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"

# ordered strongest-signal first; the FIRST match wins so "minimum of 3
# years" beats a stray "10 years" elsewhere in a nice-to-have paragraph
YEARS_PATTERNS = [
    # "0-2 years", "1 to 3 years" — a range: the floor is the requirement
    re.compile(rf"{_NUM}\s*(?:-|–|to)\s*{_NUM}\+?\s+years?", re.I),
    # "minimum of 3 years", "at least three years", "requires 2+ years"
    re.compile(rf"(?:minimum|at\s+least|min\.?|requires?)\s+(?:of\s+)?{_NUM}\s*\+?\s+years?", re.I),
    # "3+ years of (relevant/professional/…) experience"
    re.compile(rf"{_NUM}\s*\+?\s+years?[’']?\s+(?:of\s+)?"
               r"(?:relevant|professional|industry|software|engineering|work|"
               r"hands[- ]on|prior|related|full[- ]time)?\s*experience", re.I),
]

INTERN_COUNTS_RE = re.compile(
    r"(internship(?:s)?\s+(?:experience\s+)?(?:count|counts|included|considered|welcome)|"
    r"including\s+internships?|internship\s+or\s+co-?op\s+experience|"
    r"academic,?\s+internship|internship,?\s+academic)", re.I)


def _num(s: str) -> int:
    s = s.lower()
    return _WORD_NUM.get(s, int(s) if s.isdigit() else 0)


def analyze(text: str) -> dict:
    """Extract sponsorship + experience facts from posting text. Returns {}
    when the text is too short to trust (SPA shells, error pages)."""
    if len(text or "") < 200:
        return {}
    out: dict = {}
    m = SPONSOR_NO_RE.search(text)
    if m:
        out["sponsorship"] = "no"
        out["sponsorship_note"] = re.sub(r"\s+", " ", m.group(0))[:80]
    else:
        m = SPONSOR_YES_RE.search(text)
        if m:
            out["sponsorship"] = "yes"
            out["sponsorship_note"] = re.sub(r"\s+", " ", m.group(0))[:80]
        else:
            out["sponsorship"] = "unknown"
    for pat in YEARS_PATTERNS:
        m = pat.search(text)
        if m:
            out["years_min"] = _num(m.group(1))
            out["years_note"] = re.sub(r"\s+", " ", m.group(0))[:80]
            break
    if INTERN_COUNTS_RE.search(text):
        out["intern_counts"] = True
    return out


def needs_sponsorship() -> bool:
    return bool(profile().get("candidate", {}).get("needs_sponsorship"))


def apply_record(rec: dict, analysis: dict, fetched: bool, now: int | None = None) -> None:
    """Store the analysis on a job record and demote its alert when the
    posting text disqualifies it. Demote-only (dashboard stays), reasons
    logged, idempotent via the stored analysis + reason lines."""
    if not analysis:
        return
    now = now or int(time.time())
    rec["posting"] = analysis | {"analyzed_at": now, "fetched": fetched}
    reapply(rec)


def reapply(rec: dict) -> None:
    """Re-apply posting-analysis alert effects after a re-score/re-gate
    (mirrors quality.reapply — gates don't know about posting facts)."""
    p = rec.get("posting") or {}
    if not p:
        return
    reasons = rec.setdefault("score_reasons", [])
    yrs = p.get("years_min")
    if yrs is not None and yrs >= 3 and rec.get("alert_ok"):
        rec["alert_ok"] = False
        line = f"posting: wants {yrs}+ yrs (dashboard only)"
        if line not in reasons:
            reasons.append(line)
    if p.get("sponsorship") == "no" and needs_sponsorship() and rec.get("alert_ok"):
        rec["alert_ok"] = False
        line = "posting: no visa sponsorship (dashboard only)"
        if line not in reasons:
            reasons.append(line)


def _job_as_rec(j) -> dict:
    return {"url": j.url, "source": j.source, "ats": j.ats, "company": j.company}


def scrape_pass(new_jobs: list, jobs_state: dict, domains: dict,
                now: int, budget: int | None = None) -> dict:
    """The crawl's posting-scrape cycle (DECISIONS #35). Three phases:

    A (free): new jobs whose ATS already returned a description
              (Lever/Greenhouse/Ashby) are analyzed inline.
    B (budgeted): new alert-eligible jobs without text get their posting
              fetched (JSON APIs for SPA hosts, plain GET otherwise).
    C (leftover budget): stored alert-worthy records not yet analyzed —
              48 crawls/day drain the backlog in a few days.

    A dead link found while fetching closes the job (same semantics as the
    quality pass). Budget counts fetches, not successes.
    """
    from . import quality  # late import: quality also imports this module
    if env("RADAR_SCRAPE_DISABLE"):
        return {}
    budget = int(env("RADAR_SCRAPE_LIMIT", "20")) if budget is None else budget
    stats = {"inline": 0, "fetched": 0, "closed": 0, "demoted": 0}

    def _fetch_and_apply(target, url_rec, is_job: bool) -> None:
        if quality.spa_kind(url_rec):
            alive, text = quality.fetch_posting_spa(url_rec, domains)
        else:
            alive, text = quality.fetch_posting(url_rec.get("url", ""))
        stats["fetched"] += 1
        if alive is False:
            stats["closed"] += 1
            if is_job:
                target.alert_ok = False
                target.score_reasons.append("posting gone (link checked)")
            else:
                target["alert_ok"] = False
                target.setdefault("closed_at", now)
                if "posting gone (link checked)" not in target.get("score_reasons", []):
                    target.setdefault("score_reasons", []).append("posting gone (link checked)")
            return
        a = analyze(text)
        if not a:
            return
        if is_job:
            was = target.alert_ok
            rec = {"alert_ok": target.alert_ok, "score_reasons": target.score_reasons}
            apply_record(rec, a, fetched=True, now=now)
            target.posting = rec["posting"]
            target.alert_ok = rec["alert_ok"]
            if was and not target.alert_ok:
                stats["demoted"] += 1
        else:
            was = bool(target.get("alert_ok"))
            apply_record(target, a, fetched=True, now=now)
            if was and not target.get("alert_ok"):
                stats["demoted"] += 1
        time.sleep(0.3)  # politeness between posting fetches

    # A: free inline analysis for jobs that came with text
    for j in new_jobs:
        if j.description and not j.posting:
            a = analyze(j.description)
            if a:
                rec = {"alert_ok": j.alert_ok, "score_reasons": j.score_reasons}
                was = j.alert_ok
                apply_record(rec, a, fetched=False, now=now)
                j.posting = rec["posting"]
                j.alert_ok = rec["alert_ok"]
                stats["inline"] += 1
                if was and not j.alert_ok:
                    stats["demoted"] += 1

    # B: new alert-eligible jobs without text, best first
    b_candidates = sorted((j for j in new_jobs if j.alert_ok and not j.description
                           and not j.posting),
                          key=lambda j: -j.score)
    for j in b_candidates:
        if stats["fetched"] >= budget:
            break
        _fetch_and_apply(j, _job_as_rec(j), is_job=True)

    # C: stored alert-worthy records not yet analyzed
    if stats["fetched"] < budget:
        stored = [r for r in jobs_state.values()
                  if r.get("alert_ok") and not r.get("closed_at")
                  and not r.get("posting")
                  and now - r.get("first_seen", 0) <= 30 * 86400]
        stored.sort(key=lambda r: -r.get("score", 0))
        for rec in stored:
            if stats["fetched"] >= budget:
                break
            _fetch_and_apply(rec, rec, is_job=False)
    return stats


def summary_tags(p: dict | None) -> str:
    """Short human tags for alert lines / UI ('🛂 no sponsorship · ⏳ 2+ yrs')."""
    if not p:
        return ""
    tags = []
    if p.get("sponsorship") == "no":
        tags.append("🛂 no sponsorship")
    elif p.get("sponsorship") == "yes":
        tags.append("🛂 sponsors visas")
    yrs = p.get("years_min")
    if yrs:
        tags.append(f"⏳ {yrs}+ yrs" + (" (interns count)" if p.get("intern_counts") else ""))
    elif p.get("intern_counts"):
        tags.append("⏳ internships count")
    return " · ".join(tags)
