"""Deterministic internship gates, cohort parsing, and ranking.

Internships are deliberately not a special case inside the new-grad scorer.
This module owns the second lane so a missing graduation requirement is an
honest unknown instead of a hidden rejection or a new-grad false positive.
"""
from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime

from .config import profile
from .identity import canonical_url
from .models import Job
from .score import FOREIGN_HINTS, company_momentum_signal, role_bucket
from .sector import infer as infer_sector

RULES_VERSION = 8

# These locations occur in the internship ATS feed but are absent from the
# older shared location hints. Remote within a named foreign country is not
# evidence that a US applicant can work there.
INTERNSHIP_FOREIGN_HINTS = re.compile(
    FOREIGN_HINTS.pattern + r"|\b(?:denmark|aarhus|serbia|belgrade|qatar|doha|"
    r"saudi arabia|austria|belgium|luxembourg|italy|greece|hungary|czechia|czech republic|"
    r"norway|finland|iceland|lithuania|latvia|croatia|slovakia|slovenia|bulgaria|"
    r"new zealand|hong kong|indonesia|malaysia|thailand|egypt|morocco|turkey|türkiye)\b", re.I)
EXPLICIT_US_LOCATION = re.compile(r"\b(?:united states|usa?|u\.s\.)\b", re.I)

INTERNSHIP_RE = re.compile(r"\b(intern(ship)?|co-?op|undergraduate|student worker|summer analyst)\b", re.I)
INTERNSHIP_TITLE_RE = re.compile(
    r"\b(?:intern(?:ship)?|co[- ]?op|cooperative education|student worker|summer analyst)\b",
    re.I,
)
EXPERIENCED_TITLE_RE = re.compile(
    r"\b(?:sr\.?|senior|staff|principal|lead|director|manager|head of|vp|chief|"
    r"ph\.?d|postdoc)\b",
    re.I,
)
INTERNSHIP_SOURCE_NAMES = {
    "simplify_internship", "speedyapply_internship", "zapply_internship", "dreamwork_internship",
    "applyguy_internship", "aprameyak_offcycle",
    "zero2sudo_notion",
}
FULL_TIME_RE = re.compile(r"\bfull[- ]time\b|\bpermanent\s+(?:employee|role|position)\b|\bregular\s+employee\b", re.I)
RETURN_OFFER_RE = re.compile(r"\b(?:return|full[- ]time|conversion)\s+(?:offer|offers|to\s+full[- ]time)\b", re.I)
DEFENSE_ROLE_RE = re.compile(
    r"\b(?:defen[cs]e|military|national security|classified|weapons?|munitions?|battlefield|clearance)\b", re.I)
STUDENT_RE = re.compile(r"\b(current|rising|enrolled|undergraduate|college|university)\s+students?\b", re.I)
CLASS_RE = re.compile(
    r"\b(?P<rising>rising\s+)?(?P<class>freshman|freshmen|first[- ]year|"
    r"sophomore|sophomores|second[- ]year|junior|juniors|third[- ]year|"
    r"senior|seniors|fourth[- ]year)\b", re.I)
TERM_RE = re.compile(
    r"\b(?:(?P<term>summer|fall|spring|winter)\s*(?P<year>20\d{2})|"
    r"(?P<year_rev>20\d{2})\s*(?P<term_rev>summer|fall|spring|winter))\b", re.I)
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
STUDENT_CONTEXT_RE = re.compile(
    r"\b(?:student(?:s)?|undergraduate(?:s)?|college students?|university students?|"
    r"applicants?|candidates?|open to|eligible|enrolled|rising)\b",
    re.I,
)
SHARED_TERM_RE = re.compile(
    r"\b(?P<terms>(?:summer|fall|spring|winter)\s*[/,&]\s*"
    r"(?:summer|fall|spring|winter))\s*(?P<year>20\d{2})\b", re.I)
GRADUATE_TERM_RE = re.compile(r"\b(?:ph\.?d|doctoral|graduate|master(?:'s|s)?)\b", re.I)
GRADUATE_REQUIRED_RE = re.compile(
    r"\b(?:must|required|required to|only|minimum|currently)\b[^.\n]{0,90}"
    r"\b(?:ph\.?d|doctoral|graduate student|graduate students|master(?:'s|s)? degree|m\.?s\.? degree)\b"
    r"|\b(?:ph\.?d|doctoral|graduate student|graduate students|master(?:'s|s)? degree|m\.?s\.? degree)\b[^.\n]{0,90}"
    r"\b(?:must|required|required to|only|minimum)\b"
    r"|\bgraduate students?\s+only\b",
    re.I,
)
GRADUATE_EDUCATION_RE = re.compile(
    r"\b(?:education|degree|program)\s*:\s*[^.\n]{0,35}\b(?:ph\.?d|doctoral|m\.?s\.?|master(?:'s|s)?)\b"
    r"|\b(?:ph\.?d|doctoral)\s+in\s+[a-z]",
    re.I,
)
GRADUATE_PREFERRED_RE = re.compile(
    r"\b(?:ph\.?d|doctoral|graduate students?|master(?:'s|s)?|m\.?s\.?)\b[^.\n]{0,45}\bpreferred\b",
    re.I,
)
UNDERGRAD_RE = re.compile(
    r"\b(?:undergraduate|bachelor(?:'s|s)?|b\.?s\.?|college student|current student|"
    r"enrolled student|class of|rising (?:freshman|sophomore|junior|senior))\b",
    re.I,
)
STUDENT_CANDIDATE_RE = re.compile(
    r"\b(?:open to|eligible for|accepting|seeking|looking for)\b[^.\n]{0,80}"
    r"\b(?:students?|undergraduates?|college|university|applicants?|candidates?)\b"
    r"|\b(?:students?|undergraduates?|applicants?|candidates?)\b[^.\n]{0,80}"
    r"\b(?:may|can|should|must|eligible|graduat(?:e|ing)|enrolled)\b"
    r"|\b(?:students?|undergraduates?|applicants?|candidates?)\s+"
    r"(?:may|can|should|must)\s+apply\b(?!\s+to\s+(?:college|university|school))",
    re.I,
)
INTERNSHIP_CANDIDATE_RE = re.compile(
    r"\b(?:interns?|co-ops?)\b[^.\n]{0,80}\b(?:will|work|build|join|receive|are|must|should|can|may|apply)\b"
    r"|\b(?:as an? intern|intern(?:ship)? (?:role|position|opportunity)|co-op (?:role|position|opportunity))\b",
    re.I,
)
TECHNICAL_DUTY_RE = re.compile(
    r"\b(?:you|interns?|researchers?|candidates?|responsibilit(?:y|ies)|will|"
    r"build|develop|design|implement|analy[sz]e|write|code|train|debug|test|model)\b",
    re.I,
)
RECRUITING_ROLE_RE = re.compile(
    r"\b(?:early[- ]?careers?|campus recruiting|talent acquisition|recruit(?:er|ing)|"
    r"interns? specialist|careers? specialist|program coordinator)\b",
    re.I,
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


def _prestige_signal(company: str) -> tuple[int, list[str]]:
    """Score broad technical-employer prestige, never Victor's preferences."""
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
                reasons.append(f"prestige tier {tier} (crackedness signal) +{points}")
            break

    return max(0, points), reasons


def _sector_preference_signal(job: Job) -> tuple[int, list[str]]:
    """Apply the owner's small healthcare and finance preferences."""
    cfg = _scoring_config().get("sector_adjustments", {}) or {}
    sector = infer_sector(job.company, {})
    if sector == "other" and job.sector and job.sector != "other":
        sector = job.sector

    healthcare_companies = cfg.get("healthcare_companies", []) or []
    finance_companies = cfg.get("financial_companies", []) or []
    if sector in {"healthtech", "healthcare"} or any(
            _matches_company(job.company, name) for name in healthcare_companies):
        points = int(cfg.get("healthcare", 4))
        return points, [f"healthcare preference {points:+d}"] if points else []
    if sector in {"fintech", "financial_services", "financial services"} or any(
            _matches_company(job.company, name) for name in finance_companies):
        points = int(cfg.get("fintech_and_banks", -4))
        return points, [f"fintech/bank preference {points:+d}"] if points else []
    return 0, []


def _defense_signal(job: Job) -> tuple[bool, str]:
    """Identify defense employers and clearly defense-focused postings."""
    cfg = _scoring_config()
    companies = cfg.get("defense_companies", []) or []
    matched = next((str(name) for name in companies
                    if _matches_company(job.company, str(name))), "")
    if matched:
        return True, f"defense employer: {matched}"
    text = f"{job.title}\n{job.description or ''}"
    role_terms = cfg.get("defense_role_terms", []) or []
    for term in role_terms:
        if re.search(rf"\b{re.escape(str(term))}\b", text, re.I):
            return True, f"defense-focused posting: {term}"
    if DEFENSE_ROLE_RE.search(text):
        match = DEFENSE_ROLE_RE.search(text)
        return True, f"defense-focused posting: {match.group(0)}"
    return False, ""


def _employer_signal(company: str) -> tuple[int, list[str]]:
    """Use only bounded cited employer/work evidence, never preferences."""
    cfg = _scoring_config()
    points = 0
    reasons: list[str] = []

    # Existing cited research is a general work/employer signal, not a Victor
    # preference. Keep it separate and bounded so a stale dossier cannot
    # masquerade as broad prestige or dominate pay.
    cited, _cited_reasons = company_momentum_signal(company)
    cited_bonus = min(4, max(0, int(cited)))
    cap = int(cfg.get("employer_signal_cap", 8))
    applied_cited = max(0, min(cited_bonus, cap))
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
    options = _term_options(text)
    return options[0] if options else None


def _term_options(text: str) -> list[date]:
    """Return distinct internship starts, accepting both ``Summer 2027`` and
    ``2027 Summer`` forms.  Shared-year copy such as ``Spring/Summer 2027`` is
    intentionally split into two starts by the adjacent matches."""
    found: list[date] = []
    for match in SHARED_TERM_RE.finditer(text or ""):
        year = int(match.group("year"))
        for term in re.split(r"\s*[/,&]\s*", match.group("terms")):
            month = {"winter": 1, "spring": 1, "summer": 6, "fall": 9}[term.lower()]
            found.append(date(year, month, 1))
    for match in TERM_RE.finditer(text or ""):
        term = (match.group("term") or match.group("term_rev") or "").lower()
        year = match.group("year") or match.group("year_rev")
        if not year:
            continue
        month = {"winter": 1, "spring": 1, "summer": 6, "fall": 9}[term]
        found.append(date(int(year), month, 1))
    return _dedupe_dates(found)


def term_status(eligibility: dict, now: int | None = None) -> str:
    """Classify one or more terms without persisting a clock-dependent value."""
    options = eligibility.get("term_options") or []
    if not options and eligibility.get("term_start"):
        options = [eligibility["term_start"]]
    starts: list[date] = []
    for value in options:
        try:
            starts.append(date.fromisoformat(str(value)[:10]))
        except (TypeError, ValueError):
            continue
    if not starts:
        return "unknown"
    current_day = datetime.fromtimestamp(now or time.time(), UTC).date()
    current = False
    upcoming = False
    for start in starts:
        if start.month in (1,):
            end = date(start.year, 5, 31)
        elif start.month == 6:
            end = date(start.year, 8, 31)
        else:
            end = date(start.year, 12, 31)
        if start <= current_day <= end:
            current = True
        elif current_day < start:
            upcoming = True
    if current:
        return "current"
    if upcoming:
        return "upcoming"
    return "past"


def _dedupe_dates(values: list[date]) -> list[date]:
    return sorted(set(values))


def _graduation_years(text: str, term_start: date | None) -> list[int]:
    """Keep years tied to graduation language, including ``class of`` years."""
    nearby: list[int] = []
    # Keep the phrase boundary tight. A title's ``Summer 2027`` and a later
    # ``class of 2025`` requirement must produce only 2025.
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if not GRAD_CONTEXT_RE.search(sentence):
            continue
        for match in YEAR_RE.finditer(sentence):
            local = sentence[max(0, match.start() - 42):match.end() + 42]
            if GRAD_CONTEXT_RE.search(local):
                year = int(match.group(0))
                if not term_start or year != term_start.year or GRAD_CONTEXT_RE.search(local):
                    nearby.append(year)
    return sorted(set(nearby))


def _graduation_month_years(text: str) -> list[date]:
    dates: list[date] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if not GRAD_CONTEXT_RE.search(sentence):
            continue
        for match in MONTH_YEAR_RE.finditer(sentence):
            local = sentence[max(0, match.start() - 42):match.end() + 42]
            if GRAD_CONTEXT_RE.search(local):
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


def _has_student_or_internship_evidence(text: str) -> bool:
    """Require position-specific evidence, rather than company boilerplate."""
    title, _, body = (text or "").partition("\n")
    if RECRUITING_ROLE_RE.search(text or ""):
        return False
    return _title_internship_signal(title) or _has_body_student_or_internship_evidence(body)


def _has_body_student_or_internship_evidence(body: str) -> bool:
    """Check description language without treating its first line as a title."""
    if INTERNSHIP_CANDIDATE_RE.search(body or "") or STUDENT_CANDIDATE_RE.search(body or ""):
        return True
    for match in CLASS_RE.finditer(body or ""):
        window = (body or "")[max(0, match.start() - 55):match.end() + 75]
        if STUDENT_CONTEXT_RE.search(window):
            return True
    return False


def _title_internship_signal(title: str) -> bool:
    title = title or ""
    if INTERNSHIP_TITLE_RE.search(title):
        return True
    # A seasonal year title is an ATS convention; a season in body boilerplate
    # does not qualify an ordinary role.
    return bool(_term_options(title) and role_bucket(title, ""))


def _degree_requirement(text: str) -> str:
    # A list such as "bachelor's, master's, or PhD" is inclusive; the
    # appearance of PhD alone must not turn it into a graduate-only role.
    inclusive_degree = re.search(
        r"\b(?:bachelor(?:'s|s)?|b\.s\.?|undergraduate)\b[^.\n]{0,100}"
        r"\b(?:master(?:'s|s)?|m\.s\.?|ph\.?d|doctoral)\b",
        text or "", re.I,
    )
    if inclusive_degree:
        return "undergraduate_compatible"
    if (GRADUATE_REQUIRED_RE.search(text or "")
            or (GRADUATE_EDUCATION_RE.search(text or "")
                and not GRADUATE_PREFERRED_RE.search(text or ""))):
        return "graduate_only"
    if UNDERGRAD_RE.search(text or ""):
        return "undergraduate_compatible"
    if GRADUATE_PREFERRED_RE.search(text or ""):
        return "undergraduate_compatible"
    return "unknown"


def _target_role_bucket(title: str, description: str = "") -> str | None:
    """Use title fit first, then a bounded technical-duty fallback for generic
    internship titles such as ``Research Intern``."""
    direct = role_bucket(title, "")
    if direct:
        return direct
    if not _title_internship_signal(title):
        return None
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", description or ""):
        sentence = sentence.strip()
        if not sentence or re.match(r"^(?:about us|our company|we are|who we are)\b", sentence, re.I):
            continue
        if TECHNICAL_DUTY_RE.search(sentence):
            bucket = role_bucket("", sentence)
            if bucket:
                return bucket
    return None


def analyze(job: Job | dict, description: str | None = None) -> dict:
    title = job.title if isinstance(job, Job) else job.get("title", "")
    desc = description if description is not None else (
        job.description if isinstance(job, Job) else job.get("description", ""))
    text = f"{title}\n{desc or ''}"
    parsed_terms = _term_options(text)
    source_evidence = (job.internship_eligibility if isinstance(job, Job)
                       else job.get("internship_eligibility", {})) or {}
    source_term = source_evidence.get("source_term")
    source_terms = _term_options(str(source_term or ""))
    # Employer text is authoritative; feed metadata only fills a missing term.
    term_options = parsed_terms or source_terms
    term_start = term_options[0] if term_options else None
    full_time_only = bool(FULL_TIME_RE.search(text)
                          and not RETURN_OFFER_RE.search(text)
                          and not _has_student_or_internship_evidence(text))

    # Only years near graduation language count as explicit graduation
    # evidence. A summer term year by itself is the internship start year.
    grad_dates = _graduation_month_years(text) if GRAD_CONTEXT_RE.search(text) else []
    grad_years = [d.year for d in grad_dates]
    if not grad_years and GRAD_CONTEXT_RE.search(text):
        grad_years = _graduation_years(text, term_start)
    grad_years = sorted(set(grad_years))

    class_years: list[str] = []
    for match in CLASS_RE.finditer(text):
        context = text[max(0, match.start() - 55):match.end() + 75]
        if not STUDENT_CONTEXT_RE.search(context):
            continue
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
    title_signal = _title_internship_signal(title)
    body_signal = bool(
        _has_body_student_or_internship_evidence(desc or "")
        and not RECRUITING_ROLE_RE.search(text)
    )
    open_student = bool(body_signal)
    status = "explicit" if explicit else ("open" if open_student or title_signal else "unknown")
    evidence = _evidence(text, [
        GRAD_CONTEXT_RE, GRADUATE_REQUIRED_RE, GRADUATE_EDUCATION_RE,
        GRADUATE_PREFERRED_RE, CLASS_RE, TERM_RE, INTERNSHIP_RE,
        STUDENT_CONTEXT_RE,
    ])
    degree_requirement = _degree_requirement(text)
    # Degree-specific internship titles are requirements even when the body
    # was not retained. Do not mistake a BS/MS alternative for graduate-only.
    if (re.search(r"\b(?:MBA|MS|MSc|Master['’]?s)\b", title)
            and not UNDERGRAD_RE.search(title)):
        degree_requirement = "graduate_only"
    signal_reason = (
        "experienced title" if EXPERIENCED_TITLE_RE.search(title)
        else "explicit internship/co-op title" if INTERNSHIP_TITLE_RE.search(title)
        else "seasonal technical internship title" if title_signal
        else "position-specific student/internship evidence" if body_signal
        else "no position-specific internship evidence"
    )
    internship_signal = (
        "experienced_title" if EXPERIENCED_TITLE_RE.search(title)
        else "title" if title_signal
        else "body" if body_signal
        else "unknown"
    )
    return {
        "status": status,
        "class_years": class_years,
        "graduation_start": grad_start,
        "graduation_end": grad_end,
        "term_start": term_start.isoformat() if term_start else None,
        "term_options": [value.isoformat() for value in term_options],
        "source_term": source_term,
        "employment_signal": "full_time_only" if full_time_only else (
            "internship_or_student" if _has_student_or_internship_evidence(text)
            else "unknown"),
        "internship_signal": internship_signal,
        "signal_reason": signal_reason,
        "degree_requirement": degree_requirement,
        "evidence_version": 2,
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
    classes = set(eligibility.get("class_years") or [])
    terms = eligibility.get("term_options") or ([eligibility.get("term_start")]
                                                   if eligibility.get("term_start") else [])
    if terms and classes:
        standings = []
        for term in terms:
            try:
                standings.append(_class_at_start(expected, date.fromisoformat(str(term)[:10])))
            except (TypeError, ValueError):
                continue
        if standings:
            return "match" if any(standing in classes for standing in standings) else "mismatch"
    if eligibility.get("status") == "open":
        return "open"
    return "unknown"


def gates(job: Job, now: int | None = None) -> tuple[bool, bool, list[str]]:
    reasons: list[str] = []
    invalid_url = not canonical_url(job.url)
    if not job.profile:
        job.profile = "internship"
    locations = " | ".join(job.locations or [])
    if INTERNSHIP_FOREIGN_HINTS.search(locations) and not EXPLICIT_US_LOCATION.search(locations):
        return False, False, ["non-US-only internship"]
    if EXPERIENCED_TITLE_RE.search(job.title):
        return False, False, ["experienced or doctoral internship title"]
    # Role fit is title-led.  Company boilerplate can mention AI, data, or
    # software while the actual opening is an electrician or event role.
    bucket = _target_role_bucket(job.title, job.description or "")
    if not bucket:
        return False, False, ["not a target technical internship role"]
    if (job.internship_eligibility or {}).get("employment_signal") == "full_time_only":
        reasons = ["explicit full-time-only signal: review only"]
        if invalid_url:
            reasons.append("invalid posting URL (dashboard/review only)")
        return True, False, reasons
    signal = (job.internship_eligibility or {}).get("internship_signal", "unknown")
    if signal in {"unknown", "source_only"}:
        reasons = ["internship signal review: no positive title or posting evidence"]
        if invalid_url:
            reasons.append("invalid posting URL (dashboard/review only)")
        return True, False, reasons
    if signal == "title":
        reasons.append("internship title evidence")
    else:
        reasons.append("internship/student posting evidence")
    requirement = (job.internship_eligibility or {}).get("degree_requirement", "unknown")
    if requirement == "graduate_only":
        return True, False, reasons + ["graduate-only requirement: review only"]
    current_term = term_status(job.internship_eligibility or {}, now)
    if current_term == "past":
        return True, False, reasons + ["past internship term: review only"]
    if job.internship_eligibility.get("status") == "explicit":
        reasons.append("graduation/class-year eligibility evidence")
    elif job.internship_eligibility.get("status") == "unknown":
        reasons.append("internship eligibility not stated")
    if invalid_url:
        return True, False, reasons + ["invalid posting URL (dashboard/review only)"]
    return True, True, reasons


def score(job: Job, now: int) -> None:
    cfg = _scoring_config()
    flat_role = int(cfg.get("flat_technical_role", 4))
    base = int(cfg.get("base", 4))
    dimensions = {
        "base": base,
        "role_fit": flat_role,
        "eligibility": 0,
        "prestige": 0,
        "sector_preference": 0,
        "company_quality": 0,
        "compensation": 0,
        "work_quality": 0,
        "timing_access": 0,
        "defense_penalty": 0,
    }
    reasons = [
        f"base internship utility +{base}",
        f"technical internship role (flat across role families) +{flat_role}",
    ]

    eligibility_points, eligibility_reasons = _eligibility_signal(job)
    dimensions["eligibility"] = eligibility_points
    reasons.extend(eligibility_reasons)

    prestige_points, prestige_reasons = _prestige_signal(job.company)
    dimensions["prestige"] = prestige_points
    reasons.extend(prestige_reasons)

    sector_points, sector_reasons = _sector_preference_signal(job)
    dimensions["sector_preference"] = sector_points
    reasons.extend(sector_reasons or ["sector preference neutral +0"])

    defense, defense_reason = _defense_signal(job)
    if defense:
        penalty = abs(int(cfg.get("defense_penalty", 70)))
        dimensions["defense_penalty"] = -penalty
        reasons.append(f"{defense_reason}; automatic low-priority penalty -{penalty}")

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

    raw_value = round(sum(dimensions.values()), 1)
    value = raw_value
    if value > 100:
        reasons.append(f"internship score cap applied -{value - 100:g}")
    if (job.internship_eligibility or {}).get("employment_signal") == "full_time_only":
        review_cap = int(cfg.get("full_time_review_cap", 34))
        if value > review_cap:
            reasons.append(f"full-time review cap applied -{value - review_cap:g}")
            value = review_cap
    degree_requirement = (job.internship_eligibility or {}).get("degree_requirement", "unknown")
    current_term = term_status(job.internship_eligibility or {}, now)
    if degree_requirement == "graduate_only":
        reasons.append("graduate-only requirement: review cap applied")
        value = min(value, int(cfg.get("graduate_only_review_cap", 34)))
    if current_term == "past":
        reasons.append("past internship term: review cap applied")
        value = min(value, int(cfg.get("past_term_review_cap", 34)))
    if defense:
        defense_cap = max(0, int(cfg.get("defense_score_cap", 12)))
        if value > defense_cap:
            reasons.append(f"defense score cap applied -{value - defense_cap:g}")
            value = defense_cap
    job.score_raw = raw_value
    job.score_calibrated = max(0, min(100, round(value)))
    job.score = job.score_calibrated
    job.evidence_score = job.score_calibrated
    signal = (job.internship_eligibility or {}).get("internship_signal", "unknown")
    full_time_only = (
        (job.internship_eligibility or {}).get("employment_signal") == "full_time_only"
    )
    keep, gate_alert, _ = gates(job, now)
    job.eligibility = (
        "eligible"
        if keep and gate_alert and signal in {"title", "body"} and not full_time_only
        and degree_requirement != "graduate_only" and current_term != "past"
        else "review"
    )
    job.priority_tier = "recommended" if job.evidence_score >= 60 else "explore"
    job.score_dimensions = dimensions
    job.score_dimensions_raw = dict(dimensions)
    job.profile = "internship"
    job.score_reasons = reasons


def annotate(job: Job) -> dict:
    job.profile = "internship"
    source_evidence = dict(job.internship_eligibility or {})
    parsed = analyze(job)
    # A rescore may rehydrate a record without its description. Replay only the
    # bounded proof snippets written by evidence v2. Older snapshots have no
    # trustworthy provenance for body-derived verdicts and must fall back to
    # the current title/source facts instead of resurrecting stale signals.
    if not job.description:
        proof = source_evidence.get("evidence") or []
        if proof:
            # Reparse v1 snippets on first migration too. The old verdict is
            # never trusted; only the new position-specific parser result is
            # carried forward into v2.
            title_key = re.sub(r"\s+", " ", job.title or "").strip().casefold()
            replay_lines = [
                str(item) for item in proof
                if re.sub(r"\s+", " ", str(item)).strip().casefold() != title_key
            ]
            replayed = analyze(job, description="\n".join(replay_lines))
            parsed.update(replayed)
        elif source_evidence.get("source_term"):
            parsed["source_term"] = source_evidence["source_term"]
    # ATS adapters can know that a posting came from an internship-specific
    # commitment/search even when the title and description are terse. Keep
    # that provenance through the parser instead of trusting every posting in
    # an internship crawl equally.
    source_signal = bool(source_evidence.get("source_signal")) or job.source in INTERNSHIP_SOURCE_NAMES
    if source_signal:
        parsed["source_signal"] = True
        if not job.description and source_evidence.get("evidence_version", 1) >= 2:
            parsed["evidence"] = list(dict.fromkeys(
                (source_evidence.get("evidence") or []) + parsed.get("evidence", [])))[:4]
        if parsed["status"] == "unknown":
            parsed["status"] = "open"
        if parsed.get("internship_signal") == "unknown":
            parsed["internship_signal"] = "source_only"
    if (job.link_resolution or {}).get("status") == "source_only":
        # Curated public leads can have an internship-looking title, but their
        # title and curator notes are not employer evidence. Keep the row for
        # review while preventing it from entering internship alerts.
        parsed["source_signal"] = True
        parsed["internship_signal"] = "source_only"
        if parsed.get("status") == "unknown":
            parsed["status"] = "open"
    work_points, work_reasons = _work_quality_signal(job)
    parsed["work_quality"] = {"points": work_points, "reasons": work_reasons}
    # Employer-page enrichment uses these fields to enforce retry cooldowns.
    # Keep them through every annotate/rescore pass; dropping them causes the
    # crawler to refetch the same source on every run.
    for key in ("fetch_checked_at", "fetch_status", "fetch_note", "fetch_error"):
        value = source_evidence.get(key)
        if key == "fetch_checked_at" and isinstance(value, (int, float)):
            parsed[key] = int(value)
        elif key != "fetch_checked_at" and isinstance(value, str):
            parsed[key] = value
    job.internship_eligibility = parsed
    return job.internship_eligibility
