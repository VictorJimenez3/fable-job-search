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
from .score import FOREIGN_HINTS, company_momentum_signal, role_bucket

RULES_VERSION = 3

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

_MONEY_RE = re.compile(r"(?<!\w)(?:[$€£]\s*)?(\d[\d,]*(?:\.\d+)?)\s*([kK])?")
_WORK_QUALITY_RULES = (
    (re.compile(r"\b(?:mentor(?:ship)?|coaching|learn from|training program|professional development)\b", re.I),
     4, "mentorship or structured learning"),
    (re.compile(r"\b(?:own(?:ership)?|end[- ]to[- ]end|design and implement|ship|deliver|build and launch)\b", re.I),
     4, "hands-on ownership"),
    (re.compile(r"\b(?:production|deploy(?:ment)?|real[- ]world|customer-facing|users|at scale|large[- ]scale)\b", re.I),
     3, "production or user impact"),
    (re.compile(r"\b(?:research|experiment(?:ation)?|prototype|architecture|distributed systems?|performance|technical depth)\b", re.I),
     3, "technical depth"),
    (re.compile(r"\b(?:return offer|full[- ]time offer|conversion to full[- ]time|convert to full[- ]time)\b", re.I),
     4, "return-offer path"),
)


def _scoring_config() -> dict:
    return profile().get("internship_scoring", {}) or {}


def _annualized_pay(salary: str) -> tuple[int | None, str]:
    """Normalize common internship pay periods without guessing stipends."""
    values = []
    for number, suffix in _MONEY_RE.findall(salary or ""):
        try:
            value = float(number.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            value *= 1000
        values.append(value)
    if not values:
        return None, ""
    maximum = max(values)
    lower = (salary or "").lower()
    if re.search(r"(?:/|per\s+)(?:hr|hour)|hourly", lower) and maximum < 1000:
        return round(maximum * 2080), "annualized from hourly pay"
    if re.search(r"(?:/|per\s+)(?:mo|month)|monthly", lower) and maximum < 30000:
        return round(maximum * 12), "annualized from monthly pay"
    if re.search(r"(?:/|per\s+)(?:wk|week)|weekly", lower) and maximum < 5000:
        return round(maximum * 52), "annualized from weekly pay"
    return round(maximum), "published ceiling"


def _compensation_signal(salary: str) -> tuple[int, list[str]]:
    maximum, basis = _annualized_pay(salary)
    if maximum is None:
        return 0, ["compensation not stated +0"]
    configured = _scoring_config().get("compensation_points", {}) or {}
    bands = []
    for threshold, points_value in configured.items():
        try:
            bands.append((float(threshold), int(points_value)))
        except (TypeError, ValueError):
            continue
    if not bands:
        bands = [(120000, 25), (100000, 21), (80000, 17),
                 (60000, 12), (40000, 7), (0, 3)]
    points = next((points_value for threshold, points_value in sorted(bands, reverse=True)
                   if maximum >= threshold), 0)
    return points, [f"compensation ceiling ~${maximum:,}/year ({basis}) +{points}"]


def _matches_company(company: str, configured: str) -> bool:
    name = re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip()
    alias = re.sub(r"[^a-z0-9]+", " ", (configured or "").lower()).strip()
    return bool(alias and (name == alias or name.startswith(alias + " ")))


def _employer_signal(company: str) -> tuple[int, list[str]]:
    """Use a friend-facing employer signal, never Victor's saved preferences."""
    cfg = _scoring_config()
    tiers = cfg.get("prestige_tiers", {}) or {}
    tier_points = cfg.get("prestige_points", {}) or {}
    points = 0
    reasons: list[str] = []
    for tier in (1, 2, 3):
        names = tiers.get(tier, tiers.get(str(tier), [])) or []
        if any(_matches_company(company, value) for value in names):
            points = int(tier_points.get(tier, tier_points.get(str(tier), 0)) or 0)
            if points:
                reasons.append(f"recognized employer tier {tier} +{points}")
            break

    # Existing cited research is a general work/employer signal, not a Victor
    # preference. Keep it bounded so a stale dossier cannot dominate pay.
    cited, _cited_reasons = company_momentum_signal(company)
    cited_bonus = min(4, max(0, int(cited)))
    cap = int(cfg.get("employer_signal_cap", 15))
    applied_cited = max(0, min(cited_bonus, cap - points))
    if applied_cited:
        points += applied_cited
        reasons.append(f"cited employer/work evidence +{applied_cited}")
    return min(cap, points), reasons


def _work_quality_signal(job: Job) -> tuple[int, list[str]]:
    stored = (job.internship_eligibility or {}).get("work_quality")
    text = (job.description or "")[:4000]
    if not text and isinstance(stored, dict):
        return int(stored.get("points", 0) or 0), list(stored.get("reasons") or [])
    if not text:
        return 0, ["work evidence not available +0"]
    points = 0
    reasons: list[str] = []
    for pattern, bonus, label in _WORK_QUALITY_RULES:
        if pattern.search(text):
            points += bonus
            reasons.append(f"{label} +{bonus}")
    cap = int(_scoring_config().get("work_quality_cap", 16))
    if points > cap:
        reasons.append(f"work-quality cap applied -{points - cap}")
        points = cap
    return points, reasons or ["work evidence not available +0"]


def _eligibility_signal(job: Job) -> tuple[int, list[str]]:
    status = (job.internship_eligibility or {}).get("status")
    cfg = _scoring_config()
    if status == "explicit":
        points = int(cfg.get("explicit_eligibility", 14))
        return points, [f"graduation eligibility evidence +{points}"]
    if status == "open":
        points = int(cfg.get("open_eligibility", 8))
        return points, [f"student eligibility is open +{points}"]
    return 0, ["student eligibility unknown +0"]


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
            # A posting title commonly says ``Summer 2027`` while the body
            # says ``class of 2028``. The broad context window is useful for
            # malformed copy, but it must not turn the term year into a
            # graduation year unless graduation language is directly attached
            # to that occurrence.
            if (term_start and int(match.group(0)) == term_start.year
                    and not GRAD_CONTEXT_RE.search(
                        text[max(0, match.start() - 36):match.end() + 36])):
                continue
            nearby.append(int(match.group(0)))
    if nearby:
        return sorted(set(nearby))
    # A fallback for a malformed sentence with global graduation language;
    # do not let a bare internship start year masquerade as graduation.
    return sorted(set(y for y in (int(value) for value in YEAR_RE.findall(text))
                      if not term_start or y != term_start.year))


def _graduation_month_years(text: str) -> list[date]:
    dates = []
    term_start = _term_start(text)
    for match in MONTH_YEAR_RE.finditer(text):
        window = text[max(0, match.start() - 80):match.end() + 80]
        if GRAD_CONTEXT_RE.search(window):
            direct_context = GRAD_CONTEXT_RE.search(
                text[max(0, match.start() - 36):match.end() + 36])
            if (not direct_context and term_start
                    and int(match.group("year")) == term_start.year):
                continue
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
    cfg = _scoring_config()
    flat_role = int(cfg.get("flat_technical_role", 10))
    base = int(cfg.get("base", 17))
    dimensions = {
        "base": base,
        "role_fit": flat_role,
        "eligibility": 0,
        "company_quality": 0,
        "compensation": 0,
        "work_quality": 0,
        "timing_access": 0,
    }
    reasons = [
        f"base internship utility +{base}",
        f"technical internship role (flat across role families) +{flat_role}",
    ]

    eligibility_points, eligibility_reasons = _eligibility_signal(job)
    dimensions["eligibility"] = eligibility_points
    reasons.extend(eligibility_reasons)

    employer_points, employer_reasons = _employer_signal(job.company)
    dimensions["company_quality"] = employer_points
    reasons.extend(employer_reasons)

    pay_points, pay_reasons = _compensation_signal(job.salary)
    dimensions["compensation"] = pay_points
    reasons.extend(pay_reasons)

    work_points, work_reasons = _work_quality_signal(job)
    dimensions["work_quality"] = work_points
    reasons.extend(work_reasons)

    if job.posted_at:
        age = max(0, now - job.posted_at)
        key = "fresh_24h" if age <= 86400 else "fresh_72h" if age <= 3 * 86400 else "fresh_7d" if age <= 7 * 86400 else ""
        if key:
            bonus = int(cfg.get(key, 0))
            dimensions["timing_access"] = bonus
            reasons.append(f"{key.replace('_', ' ')} +{bonus}")
    else:
        reasons.append("posting age unknown +0")

    value = round(sum(dimensions.values()), 1)
    if value > 100:
        reasons.append(f"internship score cap applied -{value - 100:g}")
    job.score_raw = value
    job.score_calibrated = max(0, min(100, round(value)))
    job.score = job.score_calibrated
    job.score_dimensions = dimensions
    job.score_dimensions_raw = dict(dimensions)
    job.profile = "internship"
    job.score_reasons = reasons


def annotate(job: Job) -> dict:
    job.profile = "internship"
    source_evidence = dict(job.internship_eligibility or {})
    parsed = analyze(job)
    # A rescore may rehydrate a record without its description. Preserve the
    # last trusted eligibility parse instead of downgrading it to title-only
    # evidence, while still refreshing it whenever fresh text is available.
    if not job.description:
        for key in ("status", "class_years", "graduation_start", "graduation_end",
                    "term_start", "evidence"):
            if key in source_evidence:
                parsed[key] = source_evidence[key]
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
    work_points, work_reasons = _work_quality_signal(job)
    parsed["work_quality"] = {"points": work_points, "reasons": work_reasons}
    job.internship_eligibility = parsed
    return job.internship_eligibility
