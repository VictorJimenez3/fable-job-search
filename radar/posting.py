"""Deterministic posting-text analysis — no LLM, no keys (DECISIONS #35).

The crawl scrapes real posting text in the cloud every ~30 minutes and
extracts the facts Victor kept having to check by hand: does the role
sponsor visas / take international students, and how many years of
experience it actually wants (and whether internships count). Regex-only,
so it runs identically on CI, the Mac, and forks with zero credentials —
the LLM quality pass layers on top when a provider is available.

Stored on the record as rec["posting"]:
  {analyzed_at, fetched (bool), fetch_status, sponsorship: yes|no|unknown,
   sponsorship_note, years_min, years_note, education_required (bachelors|masters|phd),
   education_note, education_mismatch, intern_counts}
Alert effects (demote-only, reasons logged):
  - years_min >= 1          -> alert_ok False ("wants N+ yrs") and a large
                            score penalty; 5+ years is treated as a major
                            mismatch for a bachelor's new-grad profile
  - master's/PhD required  -> alert_ok False and a larger score penalty
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

DEGREE_TERMS_RE = re.compile(
    r"\b(bachelor(?:'s|s)?|b\.?s\.?|undergraduate|ph\.?d\.?|doctorate|doctoral|master(?:'s|s)?|m\.?s\.?|m\.?sc\.?|"
    r"graduate\s+degree|advanced\s+degree)\b", re.I)
EDUCATION_REQUIRED_RE = re.compile(
    r"\b(required|required for|must have|must hold|minimum of|minimum|"
    r"need(?:s)?|necessary|is a requirement)\b", re.I)
EDUCATION_PREFERRED_RE = re.compile(
    r"\b(preferred|preferred but not required|nice to have|plus|bonus|"
    r"strongly preferred|or equivalent experience)\b", re.I)

_DEGREE_RANK = {"bachelors": 1, "masters": 2, "phd": 3}


def _degree_level(term: str) -> str:
    low = term.lower().replace(".", "")
    if "phd" in low or "doctor" in low:
        return "phd"
    if "bachelor" in low or low in {"bs", "undergraduate"}:
        return "bachelors"
    return "masters"


def _education_requirement(text: str) -> tuple[str, str] | None:
    """Find an explicitly required degree, ignoring preferred-only mentions."""
    for sentence in re.split(r"(?<=[.!?])\s+|\s*[•|]\s*", text or ""):
        if not DEGREE_TERMS_RE.search(sentence) or not EDUCATION_REQUIRED_RE.search(sentence):
            continue
        if EDUCATION_PREFERRED_RE.search(sentence) and not re.search(
                r"\b(required|required for|must have|must hold)\b", sentence, re.I):
            continue
        match = DEGREE_TERMS_RE.search(sentence)
        level = _degree_level(match.group(0)) if match else "masters"
        return level, re.sub(r"\s+", " ", sentence).strip()[:180]
    return None


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
    education = _education_requirement(text)
    if education:
        level, note = education
        out["education_required"] = level
        out["education_note"] = note
        candidate = profile().get("candidate", {}).get("degree", "bachelors")
        out["education_mismatch"] = _DEGREE_RANK.get(level, 2) > _DEGREE_RANK.get(candidate, 1)
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
    rec["posting"] = analysis | {"analyzed_at": now, "fetched": fetched,
                                  "fetch_status": "readable"}
    reapply(rec)


def reapply(rec: dict) -> None:
    """Re-apply posting-analysis alert effects after a re-score/re-gate
    (mirrors quality.reapply — gates don't know about posting facts)."""
    p = rec.get("posting") or {}
    if not p:
        return
    reasons = rec.setdefault("score_reasons", [])
    yrs = p.get("years_min")
    if yrs is not None and yrs >= 1:
        penalty = 35 + min(20, yrs * 4)
        rec["alert_ok"] = False
        line = f"posting: wants {yrs}+ yrs (dashboard only) -{penalty}"
        if line not in reasons:
            rec["score"] = max(0, rec.get("score", 0) - penalty)
            reasons.append(line)
    degree = p.get("education_required")
    if p.get("education_mismatch") and degree:
        penalty = 60 if degree == "phd" else 45
        rec["alert_ok"] = False
        label = "PhD" if degree == "phd" else "master's degree"
        line = f"posting: requires {label} (bachelor's profile) (dashboard only) -{penalty}"
        if line not in reasons:
            before = rec.get("score", 0)
            rec["score"] = max(0, before - penalty)
            # Keep a strong mismatch visible for human review. It remains
            # dashboard-only and the penalty is still substantial; weaker
            # roles are not lifted just because they need a higher degree.
            floor = int(profile().get("thresholds", {}).get("dashboard", 45))
            if before >= floor and rec["score"] < floor:
                lift = floor - rec["score"]
                rec["score"] = floor
                reasons.append(
                    f"posting: degree mismatch visibility floor +{lift} (still dashboard-only)"
                )
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
    from . import company_research, lifecycle, quality, state  # late imports avoid cycles
    if env("RADAR_SCRAPE_DISABLE"):
        return {}
    budget = int(env("RADAR_SCRAPE_LIMIT", "20")) if budget is None else budget
    stats = {"inline": 0, "fetched": 0, "closed": 0, "filled": 0, "demoted": 0,
             "unreadable": 0, "research_sources": 0}
    research = company_research.load()
    research_changed = company_research.prune_irrelevant_sources(research, jobs_state)

    def _fetch_and_apply(target, url_rec, is_job: bool) -> None:
        nonlocal research_changed
        if quality.spa_kind(url_rec):
            alive, text = quality.fetch_posting_spa(url_rec, domains)
        else:
            alive, text = quality.fetch_posting(url_rec.get("url", ""))
        stats["fetched"] += 1
        if alive is False:
            stats["closed"] += 1
            status = lifecycle.status_from_dead_text(text)
            if status == lifecycle.FILLED:
                stats["filled"] += 1
            if is_job:
                lifecycle.mark_terminal(target, status, now,
                                        "posting gone (link checked); definitive dead-page evidence")
            else:
                lifecycle.mark_terminal(
                    target, status, now,
                    "posting gone (link checked); definitive dead-page evidence")
            return
        if alive is None or len(text or "") < 200:
            stats["unreadable"] += 1
            status = "unavailable" if alive is None else "unreadable"
            note = ("could not retrieve the posting page/API" if alive is None
                    else "page loaded but did not contain usable job text")
            target_posting = target.posting if is_job else target.get("posting")
            if not target_posting or not any(k in target_posting for k in
                                             ("years_min", "education_required", "sponsorship")):
                value = {"analyzed_at": now, "fetched": True,
                         "fetch_status": status, "fetch_note": note}
                if is_job:
                    target.posting = value
                else:
                    target["posting"] = value
            return
        company = target.company if is_job else target.get("company", "")
        title = target.title if is_job else target.get("title", "")
        url = target.url if is_job else target.get("url", "")
        if company_research.capture_into(research, company=company, title=title,
                                         url=url, text=text, retrieved_at=now):
            research_changed = True
            stats["research_sources"] += 1
        # Stored jobs can predate the evidence-first research feature. Record
        # this migration check even when a posting contains no useful company
        # excerpt, so a hostile/empty page is not fetched every 30 minutes.
        if not is_job:
            target["research_checked_at"] = now
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
            if company_research.capture_into(research, company=j.company, title=j.title,
                                             url=j.url, text=j.description,
                                             retrieved_at=now):
                research_changed = True
                stats["research_sources"] += 1
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
            else:
                j.posting = {"analyzed_at": now, "fetched": False,
                             "fetch_status": "unreadable",
                             "fetch_note": "ATS text was too short to verify requirements"}

    # B: new alert-eligible jobs without text, best first
    b_candidates = sorted((j for j in new_jobs if j.alert_ok and not j.description
                           and not j.posting),
                          key=lambda j: -j.score)
    for j in b_candidates:
        if stats["fetched"] >= budget:
            break
        _fetch_and_apply(j, _job_as_rec(j), is_job=True)

    # C: stored priority records not yet analyzed, plus a one-time evidence
    # migration for priority jobs analyzed before company research existed.
    if stats["fetched"] < budget:
        applied_ids = {a.get("id") for a in state.load("applied.json", [])
                       if a.get("id")}
        web = state.load("web_state.json", {})
        tracked_ids = applied_ids | set((web.get("jobs") or {}).keys()) \
            | set(web.get("maybe") or [])

        def needs_evidence(rec: dict) -> bool:
            dossier = company_research.dossier_for(rec.get("company", ""), research)
            missing = not dossier or not dossier.get("sources")
            last_check = int(rec.get("research_checked_at") or 0)
            return missing and now - last_check >= 7 * 86400

        stored = [r for r in jobs_state.values()
                  if not lifecycle.is_terminal(r) and r.get("url")
                  and (r.get("alert_ok") or (
                      r.get("id") in tracked_ids
                      and company_research.job_is_relevant(r)
                      ) or (env("RADAR_SCRAPE_DASHBOARD")
                            and r.get("score", 0) >= profile()["thresholds"]["dashboard"]))
                  and (not r.get("posting") or needs_evidence(r)
                       or (r.get("posting") or {}).get("fetch_status") != "readable"
                       or not (r.get("posting") or {}).get("fetched", False))
                  and now - r.get("first_seen", 0) <= 45 * 86400]
        stored.sort(key=lambda r: (
            r.get("id") in tracked_ids,
            bool(r.get("alert_ok")),
            needs_evidence(r),
            r.get("score", 0),
            r.get("first_seen", 0),
        ), reverse=True)
        for rec in stored:
            if stats["fetched"] >= budget:
                break
            _fetch_and_apply(rec, rec, is_job=False)
    if research_changed:
        company_research.save(research)
    return stats


def summary_tags(p: dict | None) -> str:
    """Short human tags for alert lines / UI."""
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
    if p.get("education_mismatch"):
        tags.append("🎓 degree mismatch")
    elif p.get("education_required"):
        tags.append(f"🎓 {p['education_required']} required")
    if p.get("fetch_status") in {"unavailable", "unreadable"}:
        tags.append("⚠️ requirements unverified")
    return " · ".join(tags)
