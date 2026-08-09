"""Deterministic internship gates, cohort parsing, and ranking.

Internships are deliberately not a special case inside the new-grad scorer.
This module owns the second lane so a missing graduation requirement is an
honest unknown instead of a hidden rejection or a new-grad false positive.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from .config import profile
from .models import Job
from .score import FOREIGN_HINTS, role_bucket

RULES_VERSION = 1

INTERNSHIP_RE = re.compile(r"\b(intern(ship)?|co-?op|undergraduate|student worker|summer analyst)\b", re.I)
STUDENT_RE = re.compile(r"\b(current|rising|enrolled|undergraduate|college|university)\s+students?\b", re.I)
CLASS_RE = re.compile(
    r"\b(?P<rising>rising\s+)?(?P<class>freshman|freshmen|first[- ]year|"
    r"sophomore|sophomores|second[- ]year|junior|juniors|third[- ]year|"
    r"senior|seniors|fourth[- ]year)\b", re.I)
TERM_RE = re.compile(r"\b(?P<term>summer|fall|spring|winter)\s*(?P<year>20\d{2})\b", re.I)
YEAR_RE = re.compile(r"\b20\d{2}\b")
MONTH_YEAR_RE = re.compile(
    r"\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+(?P<year>20\d{2})\b", re.I)
GRAD_CONTEXT_RE = re.compile(
    r"\b(?:graduat(?:e|es|ed|ing|ion)|class\s+of|expected\s+to\s+finish|"
    r"degree\s+completion)\b", re.I)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
    "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_CLASS_NAMES = {
    "freshman": "freshman", "freshmen": "freshman", "first-year": "freshman",
    "sophomore": "sophomore", "sophomores": "sophomore", "second-year": "sophomore",
    "junior": "junior", "juniors": "junior", "third-year": "junior",
    "senior": "senior", "seniors": "senior", "fourth-year": "senior",
}
_CLASS_MONTHS = (("senior", 0, 11), ("junior", 12, 23),
                 ("sophomore", 24, 35), ("freshman", 36, 47))


def _term_start(text: str) -> date | None:
    m = TERM_RE.search(text)
    if not m:
        return None
    month = {"winter": 1, "spring": 1, "summer": 6, "fall": 9}[m.group("term").lower()]
    return date(int(m.group("year")), month, 1)


def _dedupe_dates(values: list[date]) -> list[date]:
    return sorted(set(values))


def _graduation_years(text: str, term_start: date | None) -> list[int]:
    """Keep years tied to graduation language, including ``class of`` years."""
    nearby = []
    for match in YEAR_RE.finditer(text):
        window = text[max(0, match.start() - 80):match.end() + 80]
        if GRAD_CONTEXT_RE.search(window):
            nearby.append(int(match.group(0)))
    if nearby:
        return sorted(set(nearby))
    # A fallback for a malformed sentence with global graduation language;
    # do not let a bare internship start year masquerade as graduation.
    return sorted(set(y for y in (int(value) for value in YEAR_RE.findall(text))
                      if not term_start or y != term_start.year))


def _graduation_month_years(text: str) -> list[date]:
    dates = []
    for match in MONTH_YEAR_RE.finditer(text):
        window = text[max(0, match.start() - 80):match.end() + 80]
        if GRAD_CONTEXT_RE.search(window):
            dates.append(date(int(match.group("year")),
                             _MONTHS[match.group("month").lower()], 1))
    return _dedupe_dates(dates)


def _evidence(text: str, patterns: list[re.Pattern]) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    found: list[str] = []
    for line in lines:
        if line and any(p.search(line) for p in patterns):
            found.append(line[:240])
    if not found:
        for p in patterns:
            m = p.search(text)
            if m:
                found.append(text[max(0, m.start() - 70):m.end() + 100].strip()[:240])
    return list(dict.fromkeys(found))[:4]


def analyze(job: Job | dict, description: str | None = None) -> dict:
    title = job.title if isinstance(job, Job) else job.get("title", "")
    desc = description if description is not None else (
        job.description if isinstance(job, Job) else job.get("description", ""))
    text = f"{title}\n{desc or ''}"
    term_start = _term_start(text)

    # Only years near graduation language count as explicit graduation
    # evidence. A summer term year by itself is the internship start year.
    grad_dates = _graduation_month_years(text) if GRAD_CONTEXT_RE.search(text) else []
    grad_years = [d.year for d in grad_dates]
    if not grad_years and GRAD_CONTEXT_RE.search(text):
        grad_years = _graduation_years(text, term_start)
    grad_years = sorted(set(grad_years))

    class_years: list[str] = []
    for match in CLASS_RE.finditer(text):
        # ``first year`` and ``first-year`` are equivalent in real postings;
        # normalize the separator before looking up the auditable standing.
        raw_class = match.group("class").lower().replace(" ", "-")
        label = _CLASS_NAMES[raw_class]
        if label not in class_years:
            class_years.append(label)

    if grad_dates:
        grad_start = grad_dates[0].isoformat()
        grad_end = grad_dates[-1].isoformat()
    elif grad_years:
        grad_start = date(min(grad_years), 1, 1).isoformat()
        grad_end = date(max(grad_years), 12, 1).isoformat()
    else:
        grad_start = grad_end = None

    explicit = bool(grad_years or class_years)
    open_student = bool(STUDENT_RE.search(text))
    status = "explicit" if explicit else ("open" if open_student or INTERNSHIP_RE.search(text) else "unknown")
    evidence = _evidence(text, [GRAD_CONTEXT_RE, CLASS_RE, TERM_RE, INTERNSHIP_RE])
    return {
        "status": status,
        "class_years": class_years,
        "graduation_start": grad_start,
        "graduation_end": grad_end,
        "term_start": term_start.isoformat() if term_start else None,
        "evidence": evidence,
    }


def _class_at_start(expected: date, start: date) -> str | None:
    months = (expected.year - start.year) * 12 + expected.month - start.month
    for name, low, high in _CLASS_MONTHS:
        if low <= months <= high:
            return name
    return None


def match(eligibility: dict, expected_graduation: str | date | None) -> str:
    """Return match/mismatch/unknown/open for one viewer's grad date."""
    if not expected_graduation:
        return "unknown"
    if isinstance(expected_graduation, date):
        expected = expected_graduation
    else:
        try:
            expected = datetime.strptime(str(expected_graduation)[:7], "%Y-%m").date().replace(day=1)
        except ValueError:
            return "unknown"

    start = eligibility.get("graduation_start")
    end = eligibility.get("graduation_end")
    if start and end:
        return "match" if start[:7] <= expected.strftime("%Y-%m") <= end[:7] else "mismatch"
    term = eligibility.get("term_start")
    classes = set(eligibility.get("class_years") or [])
    if term and classes:
        role_start = date.fromisoformat(term)
        standing = _class_at_start(expected, role_start)
        return "match" if standing in classes else "mismatch"
    if eligibility.get("status") == "open":
        return "open"
    return "unknown"


def gates(job: Job) -> tuple[bool, bool, list[str]]:
    text = f"{job.title}\n{job.description or ''}"
    reasons: list[str] = []
    if not job.profile:
        job.profile = "internship"
    if job.remote:
        pass
    elif job.locations and FOREIGN_HINTS.search(" | ".join(job.locations)):
        return False, False, ["non-US-only internship"]
    if re.search(r"\b(senior|staff|principal|lead|director|manager|head of|vp|chief|ph\.?d|postdoc)\b", job.title, re.I):
        return False, False, ["experienced or doctoral internship title"]
    bucket = role_bucket(job.title, job.description or "")
    if not bucket:
        return False, False, ["not a target technical internship role"]
    source_signal = bool(job.internship_eligibility.get("source_signal")) or job.source in {
        "simplify_internship", "speedyapply_internship", "zapply_internship", "dreamwork_internship"
    }
    if not (INTERNSHIP_RE.search(text) or source_signal):
        return True, False, ["student/internship evidence not stated: dashboard only"]
    if INTERNSHIP_RE.search(job.title):
        reasons.append("explicit internship/co-op title")
    else:
        reasons.append("internship source evidence")
    if job.internship_eligibility.get("status") == "explicit":
        reasons.append("graduation/class-year eligibility evidence")
    elif job.internship_eligibility.get("status") == "unknown":
        reasons.append("internship eligibility not stated")
    return True, True, reasons


def score(job: Job, now: int) -> None:
    cfg = profile()
    bucket = role_bucket(job.title, job.description or "") or "swe"
    weights = cfg.get("roles", {})
    value = float(weights.get(bucket, 0))
    reasons = [f"role:{bucket} +{int(value)}"]
    dimensions = {"role_fit": value, "eligibility": 0.0, "freshness": 0.0}
    if INTERNSHIP_RE.search(job.title):
        bonus = float(cfg.get("bonuses", {}).get("explicit_internship_title", 0))
        value += bonus
        reasons.append(f"explicit internship title +{int(bonus)}")
    elif job.source.startswith(("simplify_internship", "speedyapply_internship", "zapply_internship", "dreamwork_internship")):
        bonus = float(cfg.get("bonuses", {}).get("curated_source", 0))
        value += bonus
        reasons.append(f"curated internship source +{int(bonus)}")
    if job.internship_eligibility.get("status") == "explicit":
        bonus = float(cfg.get("bonuses", {}).get("student_eligibility", 0))
        value += bonus
        dimensions["eligibility"] = bonus
        reasons.append(f"graduation eligibility +{int(bonus)}")
    if job.remote:
        bonus = float(cfg.get("bonuses", {}).get("remote", 0))
        value += bonus
        reasons.append(f"remote +{int(bonus)}")
    if job.posted_at:
        age = max(0, now - job.posted_at)
        key = "fresh_24h" if age <= 86400 else "fresh_72h" if age <= 3 * 86400 else "fresh_7d" if age <= 7 * 86400 else ""
        if key:
            bonus = float(cfg.get("bonuses", {}).get(key, 0))
            value += bonus
            dimensions["freshness"] = bonus
            reasons.append(f"{key.replace('_', ' ')} +{int(bonus)}")
    job.score_raw = value
    job.score_calibrated = max(0, min(100, round(value)))
    job.score = job.score_calibrated
    job.score_dimensions = dimensions
    job.score_reasons = reasons


def annotate(job: Job) -> dict:
    job.profile = "internship"
    source_evidence = job.internship_eligibility or {}
    parsed = analyze(job)
    # ATS adapters can know that a posting came from an internship-specific
    # commitment/search even when the title and description are terse. Keep
    # that provenance through the parser instead of trusting every posting in
    # an internship crawl equally.
    if source_evidence.get("source_signal"):
        parsed["source_signal"] = True
        parsed["evidence"] = list(dict.fromkeys(
            (source_evidence.get("evidence") or []) + parsed.get("evidence", [])))[:4]
        if parsed["status"] == "unknown":
            parsed["status"] = "open"
    job.internship_eligibility = parsed
    return job.internship_eligibility
