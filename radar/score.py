"""Two-stage ranking: hard gates, then additive scoring with learned feedback.

Gates answer "should this ever reach the human". Score answers "how loudly".
Every point awarded is recorded in job.score_reasons so ranking is auditable.
"""
from __future__ import annotations

import re
import time

from .config import profile
from .models import Job, norm

# ---------- hard gates ----------

# Bumped whenever gate rules change; regate() re-applies the current rules to
# every stored job whose rules_v is older (demote/promote alert_ok in place).
RULES_VERSION = 10

SENIOR_RE = re.compile(
    r"\b(senior|staff|principal|lead(er)?|director|head of|sr\.?|vp|chief|"
    r"distinguished|fellow|executive|iii|iv|"
    r"engineer\s+[3-9]|l[5-9]|level\s+[3-9])\b", re.I)
# Generic engineering managers and architects stay hard-gated. The requested
# PM-family titles are kept as dashboard-only research records, including
# entry-level Product/Project Manager and Solutions Architect postings.
MANAGER_RE = re.compile(r"\bmanager\b", re.I)
ARCHITECT_RE = re.compile(r"\barchitect(?:ure)?\b", re.I)
# Typically 1-3 yrs experience: worth seeing on the dashboard, never an alert.
MIDLEVEL_RE = re.compile(r"\b(ii|l4|engineer\s+2|level\s+2|mid([- ]level)?)\b", re.I)
# Roles outside Victor's field. Title-only, demote-only (alert_ok=False, job
# stays on the dashboard) and outranks every auto-alert path incl. marquee.
# Deliberately narrow: "Product Engineer" / "Security Engineer" must NOT match.
OFF_FIELD_RE = re.compile(
    r"\b(safeguards?|trust\s*(&|and)\s*safety|policy|counsel|legal|paralegal|compliance|"
    r"recruit(er|ing)|talent\s+(acquisition|management|operations|partner)|people\s+(ops|operations)|human\s+resources|hr|"
    r"sales|account\s+(executive|manager)|business\s+(development|operations|analyst)|"
    r"go[- ]to[- ]market|gtm|partnerships?|"
    r"marketing|brand|communications?|comms|public\s+relations|editorial|"
    r"finance|financial\s+analyst|accounting|accountant|payroll|procurement|revenue|"
    r"solutions?\s+(engineer|architect|consultant)|sales\s+engineer|field\s+engineer|"
    r"customer\s+(success|support|experience)|technical\s+support|"
    r"support\s+(engineer|specialist)|help\s?desk|"
    r"success\s+engineer|ai\s+governance|governance\s+and\s+advisory|"
    r"(?:technology|technical)\s+consultant|"
    r"(ux|ui|visual|graphic|product)\s+design(er)?|"
    r"(product|program|project)\s+manager|product\s+(owner|marketing)|"
    r"chief\s+of\s+staff|executive\s+assistant|administrative|"
    r"workplace|facilities)\b", re.I)
INTERN_RE = re.compile(r"\b(intern(ship)?|co-?op|apprentice|fellowship|part[- ]?time|contract(or)?)\b", re.I)
PHD_RE = re.compile(r"\bph\.?d\b|postdoc", re.I)
CLEARANCE_RE = re.compile(r"\b(security clearance|ts/sci|polygraph|top secret)\b", re.I)
YEARS_RE = re.compile(r"(?:minimum|at least|requires?)\s+(\d+)\+?\s+years", re.I)

# A new-grad role may say "0-2 years"; a required floor of 1+ years is an
# experienced-hire role for this board. Posting scraping applies the same rule
# after fetching the full JD.
REQUIRED_YEARS_RE = re.compile(
    r"(?:minimum|at\s+least|requires?|must\s+have|need(?:s)?|seeking|looking\s+for)\s+"
    r"(?:of\s+)?(?P<floor>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*\+?\s+years?"
    r"|(?P<plus>\d+)\s*\+\s+years?\s+(?:of\s+)?(?:required\s+)?(?:relevant\s+|professional\s+|industry\s+|software\s+|engineering\s+)?experience"
    r"|(?P<range>[1-9])\s*(?:-|–|to)\s*\d+\s+years?\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+|software\s+|engineering\s+)?experience"
    r"|(?<![\d-])(?P<plain>[1-9]|one|two|three|four|five|six|seven|eight|nine|ten)\s+years?\s+(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+|software\s+|engineering\s+)?experience",
    re.I)
_WORD_YEARS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
               "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

NEW_GRAD_RE = re.compile(
    r"\b(new ?grad|university grad|recent(ly)? grad|early[- ]career|entry[- ]level|"
    r"campus|college grad|20(25|26|27) grad|class of 20(25|26|27)|junior|associate|"
    r"engineer i\b|graduate (software|engineer|program|scheme))\b", re.I)
ENTRY_YEARS_RE = re.compile(r"\b0\s*[-–to ]+\s*[123]\s+years\b", re.I)
STRONG_NEW_GRAD_RE = re.compile(
    r"\b(new\s*grad|university\s+grad|recent(?:ly)?\s+grad|early[- ]career|"
    r"entry[- ]level|college\s+grad|20(?:25|26|27)\s+grad|class\s+of\s+20(?:25|26|27)|"
    r"graduate\s+(?:software|engineer|program|scheme)|rotational\s+program|"
    r"graduate\s+program|emerging\s+talent|future\s+talent)\b", re.I)
TRUSTED_NEW_GRAD_SOURCES = {"simplify", "vansh", "jobright", "jobright_pm", "speedyapply"}

ROLE_BUCKETS: dict[str, re.Pattern] = {
    "ai_ml": re.compile(
        r"machine learning|ml engineer|\bml\b|\bai\b|artificial intelligence|applied scientist|"
        r"research engineer|deep learning|\bllm\b|gen ?ai|generative|nlp|computer vision|perception", re.I),
    "data_science": re.compile(
        r"data scien|decision scien|analytics engineer|data\s+(analyst|analytics)|"
        r"(product|research|business intelligence|bi)\s+analyst|statistic|quantitative", re.I),
    "data_eng": re.compile(r"data engineer|data platform|data infrastructure|etl\b", re.I),
    "swe": re.compile(
        r"software|swe\b|backend|back[- ]end|full[- ]?stack|front[- ]?end|platform engineer|"
        r"infrastructure|site reliability|devops|mobile|\bios\b|android|\bdeveloper\b|systems engineer|"
        r"security engineer|cloud engineer|embedded", re.I),
    # PM roles are intentionally a visible, low-scoring dashboard lane. They
    # never become alerts, even when a source labels them new-grad.
    "pm": re.compile(
        r"\b(?:a?pm|associate\s+product\s+manager|technical\s+product\s+manager|"
        r"product\s+(?:manager|owner|management)|project\s+manager|"
        r"business(?:\s+systems)?\s+analyst|"
        r"(?:ux\s*/\s*ui|ux|ui|user\s+experience|user\s+interface)\s+(?:researcher|research)|"
        r"solutions?\s+architect(?:ure)?)\b", re.I),
}

PROGRAM_RE = re.compile(
    r"\b(leadership\s+(?:development\s+)?program|rotational\s+program|"
    r"graduate\s+program(?:me)?|emerging\s+talent|future\s+talent|"
    r"technology\s+accelerator|tldp|mldp|dsldp|eldp)\b", re.I)
TECH_PROGRAM_RE = re.compile(
    r"\b(technology|information\s+technology|digital|data\s+science|"
    r"data\s+engineering|data\s+analytics|analytics|artificial\s+intelligence|"
    r"machine\s+learning|software\s+engineering|engineering|cloud|devops|"
    r"cybersecurity|automation|\bit\b)\b", re.I)

FOREIGN_HINTS = re.compile(
    r"\b(canada|toronto|vancouver|london|uk\b|united kingdom|ireland|dublin|germany|berlin|munich|"
    r"france|paris|netherlands|amsterdam|india|bangalore|bengaluru|hyderabad|pune|chennai|gurgaon|"
    r"noida|mumbai|singapore|japan|tokyo|china|beijing|shanghai|shenzhen|australia|sydney|melbourne|"
    r"brazil|sao paulo|mexico city|poland|warsaw|krakow|israel|tel aviv|spain|madrid|barcelona|"
    r"portugal|lisbon|switzerland|zurich|sweden|stockholm|estonia|romania|dubai|uae|philippines|"
    r"manila|vietnam|korea|seoul|taiwan|taipei|nigeria|kenya|south africa|argentina|colombia|chile|"
    r"panam[aá]|bangladesh|pakistan)\b", re.I)
US_HINTS = re.compile(
    r"\b(us|usa|u\.s\.|united states|remote)\b|"
    r"\b(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|"
    r"nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc)\b|"
    r"new york|san francisco|seattle|austin|boston|chicago|denver|atlanta|los angeles|san jose|"
    r"palo alto|mountain view|sunnyvale|redmond|bellevue|cambridge|philadelphia|miami|dallas|"
    r"houston|phoenix|portland|salt lake|pittsburgh|raleigh|durham|nashville|minneapolis|detroit|"
    r"washington|arlington|reston|mclean|santa clara|menlo park|cupertino|irvine|san diego|boulder", re.I)


def role_bucket(title: str, description: str = "") -> str | None:
    for bucket in ("ai_ml", "data_science", "data_eng", "swe", "pm"):
        if ROLE_BUCKETS[bucket].search(title):
            return bucket
    if description:
        for bucket in ("ai_ml", "data_science", "swe"):
            if ROLE_BUCKETS[bucket].search(description[:600]):
                return bucket
    return None


def location_ok(job: Job) -> bool:
    if job.remote:
        return True
    if not job.locations:
        return True  # unknown — don't drop, scorer just won't reward it
    blob = " | ".join(job.locations)
    if FOREIGN_HINTS.search(blob):
        # foreign city named — keep only if there's also a strong US signal
        return bool(re.search(r"\b(usa?|u\.s\.|united states)\b|remote \(?us", blob, re.I))
    return True


def _required_years(text: str) -> int | None:
    """Return the required experience floor when the JD states one."""
    m = REQUIRED_YEARS_RE.search(text or "")
    if not m:
        return None
    value = m.group("floor") or m.group("plus") or m.group("range") or m.group("plain")
    if value is None:
        return None
    if value.isdigit():
        return int(value)
    return _WORD_YEARS.get(value.lower())


def leadership_program_signal(company: str, title: str, description: str = "") -> bool:
    """True for a technical/data early-career program, not generic leadership."""
    text = f"{title}\n{description[:1800]}"
    cfg = profile().get("programs", {})
    program_re = (re.compile(r"(?<!\w)(?:" + "|".join(re.escape(v) for v in cfg["keywords"])
                             + r")(?!\w)", re.I)
                  if cfg.get("keywords") else PROGRAM_RE)
    tech_re = (re.compile(r"(?<!\w)(?:" + "|".join(re.escape(v) for v in cfg["technical_keywords"])
                          + r")(?!\w)", re.I)
               if cfg.get("technical_keywords") else TECH_PROGRAM_RE)
    if not program_re.search(text):
        return False
    # J&J's TLDP and comparable data-focused acronyms are documented program
    # names whose short titles may omit the technology words.
    acronym_program = re.search(r"\b(tldp|dsldp|eldp)\b", text, re.I)
    known_company = norm(company) in target_program_companies()
    if not tech_re.search(text) and not (acronym_program and known_company):
        return False
    if OFF_FIELD_RE.search(title):
        return False
    return True


def new_grad_signal(title: str, description: str = "") -> bool:
    """Require explicit early-career evidence instead of source/company guesses."""
    text = f"{title}\n{description[:1800]}"
    return bool(STRONG_NEW_GRAD_RE.search(text) or ENTRY_YEARS_RE.search(text))


def source_new_grad(job: Job) -> bool:
    """Treat dedicated new-grad aggregators as strong provenance evidence."""
    # The broad Zapply board is noisy globally, but this pipeline only admits
    # its PM-family rows. The dedicated Jobright PM board is a new-grad board;
    # both sources give PM rows provisional visibility evidence while the PM
    # gate below still makes them dashboard-only and never alertable.
    return (job.source.lower() in TRUSTED_NEW_GRAD_SOURCES
            or (job.source.lower() in {"zapply_pm"} and role_bucket(job.title) == "pm"))


def gates(job: Job) -> tuple[bool, bool, list[str]]:
    """Returns (keep_at_all, alert_eligible, reasons)."""
    t = job.title
    text = f"{t}\n{job.description[:1500]}"
    bucket = role_bucket(t)
    program = leadership_program_signal(job.company, t, job.description)
    new_grad = new_grad_signal(t, job.description) or source_new_grad(job)
    if INTERN_RE.search(t):
        return False, False, ["intern/co-op/contract"]
    if SENIOR_RE.search(t):
        return False, False, ["senior+ title"]
    if MANAGER_RE.search(t) and bucket != "pm":
        return False, False, ["senior+ title"]
    if ARCHITECT_RE.search(t) and bucket != "pm":
        return False, False, ["senior+ title"]
    if PHD_RE.search(t):
        return False, False, ["PhD-targeted title"]
    if CLEARANCE_RE.search(text):
        return False, False, ["requires clearance"]
    if not location_ok(job):
        return False, False, ["non-US location"]
    years = _required_years(job.description)
    if years is not None and years >= 1:
        return False, False, [f"requires {years}+ years (not new-grad)" ]
    # A description mentioning software/AI must not turn an obviously
    # non-technical title into a target role (e.g. Safety Editor at OpenAI or
    # a Biology Research Associate at Anthropic). Descriptions still inform
    # entry-level and experience gates; role-family eligibility is title-led.
    if bucket is None and not program:
        if OFF_FIELD_RE.search(t):
            return True, False, ["off-field title (dashboard only)"]
        if re.search(r"\banalyst\b", t, re.I):
            return True, False, ["generic analyst title (dashboard only)"]
        return False, False, ["not an AI/SWE/DS role"]

    reasons = []
    alert_eligible = new_grad or program
    if new_grad:
        reasons.append("verified new-grad/early-career evidence")
    if program:
        reasons.append("technical leadership/rotational program")
    if not alert_eligible:
        reasons.append("not verified new-grad/early-career (dashboard only)")
    # Demotions outrank every auto-alert path above, marquee included:
    # dashboard-only, never deleted.
    if OFF_FIELD_RE.search(t):
        alert_eligible = False
        if "off-field title (dashboard only)" not in reasons:
            reasons.append("off-field title (dashboard only)")
    if MIDLEVEL_RE.search(t):
        alert_eligible = False
        if "mid-level title (dashboard only)" not in reasons:
            reasons.append("mid-level title (dashboard only)")
    if not alert_eligible and not reasons:
        reasons.append("seniority unclear (dashboard only)")
    if bucket == "pm":
        alert_eligible = False
        reasons.append("PM-family role (dashboard only)")
    return True, alert_eligible, reasons


def explicit_new_grad(title: str) -> bool:
    """True when the title carries new-grad or technical-program evidence."""
    return new_grad_signal(title)


def early_career_possible(job: Job, posting: dict | None = None) -> bool:
    """Flag a plausible first-role posting without weakening the alert gate.

    This is intentionally a *discovery label*, not new-grad evidence.  It is
    for roles such as Fanatics' AI Engineer: a target technical title and no
    stated experience floor, but no explicit campus/new-grad signal either.
    It never changes ``alert_ok`` and excludes the same clear mismatches that
    the main gates exclude.
    """
    title = job.title or ""
    if role_bucket(title) == "pm":
        return False
    if (new_grad_signal(title, job.description) or source_new_grad(job)
            or leadership_program_signal(job.company, title, job.description)):
        return False
    if (INTERN_RE.search(title) or SENIOR_RE.search(title) or PHD_RE.search(title)
            or CLEARANCE_RE.search(f"{title}\n{job.description[:1500]}")
            or MIDLEVEL_RE.search(title) or OFF_FIELD_RE.search(title)
            or role_bucket(title) is None or not location_ok(job)):
        return False
    stated_years = _required_years(job.description)
    if stated_years is not None and stated_years >= 1:
        return False
    if isinstance(posting, dict) and posting.get("years_min") not in (None, 0):
        return False
    return True


def _strong_role_title(t: str) -> bool:
    """Stricter than role_bucket for the priority-sector auto-alert: a bare
    "<anything> Analyst" title (the data_science bucket's loosest match) is
    too weak to alert on by itself — require a data-flavored analyst title."""
    b = role_bucket(t)
    if b in ("ai_ml", "data_eng", "swe"):
        return True
    if b == "data_science":
        return bool(re.search(r"data|analytics|statistic|quantitative", t, re.I))
    return False


_MARQUEE_CACHE: set | None = None
_PRIORITY_SECTORS: set | None = None
_PROGRAM_COMPANIES_CACHE: set | None = None


def is_marquee(company: str) -> bool:
    """Blockbuster employer per profile.yaml marquee_companies."""
    global _MARQUEE_CACHE
    if _MARQUEE_CACHE is None:
        _MARQUEE_CACHE = {norm(c) for c in profile().get("marquee_companies", [])}
    return norm(company) in _MARQUEE_CACHE


def priority_sectors() -> set:
    """Sectors that auto-alert on a role-fit title (profile.yaml priority_sectors)."""
    global _PRIORITY_SECTORS
    if _PRIORITY_SECTORS is None:
        _PRIORITY_SECTORS = set(profile().get("priority_sectors", []))
    return _PRIORITY_SECTORS


def target_program_companies() -> set:
    """Healthcare employers with recurring technical graduate programs."""
    global _PROGRAM_COMPANIES_CACHE
    if _PROGRAM_COMPANIES_CACHE is None:
        names = profile().get("programs", {}).get("target_healthcare_companies", [])
        _PROGRAM_COMPANIES_CACHE = {norm(c) for c in names}
    return _PROGRAM_COMPANIES_CACHE


_MONEY_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?")


def pays_bank(salary: str) -> bool:
    """True when the posting's salary text reaches the pay_bank threshold."""
    if not salary:
        return False
    floor = int(profile()["thresholds"].get("pay_bank", 150000))
    best = 0.0
    for num, k in _MONEY_RE.findall(salary):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        if k:
            v *= 1000
        best = max(best, v)
    return best >= floor


# ---------- scoring ----------

_CULTURE_CACHE: dict | None = None
_CULTURE_MATCH_CACHE: dict[tuple[int, str], dict | None] = {}
_COMPANY_RESEARCH_CACHE: dict | None = None
_SHPE_CACHE: set | None = None


def _shpe_companies() -> set:
    global _SHPE_CACHE
    if _SHPE_CACHE is None:
        try:
            import yaml

            from .config import DATA_DIR
            with open(DATA_DIR / "conference_shpe.yaml") as f:
                rows = yaml.safe_load(f)["companies"]
            _SHPE_CACHE = {norm(r["name"]) for r in rows}
        except Exception:
            _SHPE_CACHE = set()
    return _SHPE_CACHE


def _culture_cache() -> dict:
    """Load culture dossiers once per process (score() runs per-job in a loop)."""
    global _CULTURE_CACHE
    if _CULTURE_CACHE is None:
        from . import culture as _culture
        _CULTURE_CACHE = _culture.load()
    return _CULTURE_CACHE


def _culture_dossier(company: str) -> dict | None:
    """Memoize loose dossier matching across thousands of roles per company."""
    dossiers = _culture_cache()
    # Include object identity so tests, reloads, and repairs that replace the
    # dossier map cannot receive a match cached against older evidence.
    key = (id(dossiers), norm(company))
    if key not in _CULTURE_MATCH_CACHE:
        from . import culture as _culture
        _CULTURE_MATCH_CACHE[key] = _culture.dossier_for(company, dossiers)
    return _CULTURE_MATCH_CACHE[key]


def _company_research_cache() -> dict:
    """Load optional cited employer evidence without making it a dependency."""
    global _COMPANY_RESEARCH_CACHE
    if _COMPANY_RESEARCH_CACHE is None:
        try:
            from . import state
            value = state.load("company_research.json", {})
            _COMPANY_RESEARCH_CACHE = value if isinstance(value, dict) else {}
        except Exception:
            _COMPANY_RESEARCH_CACHE = {}
    return _COMPANY_RESEARCH_CACHE


def _company_record(company: str) -> dict:
    records = _company_research_cache()
    for key in (company, company.lower(), norm(company)):
        value = records.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _supported_field(record: dict, name: str) -> str:
    field = record.get(name)
    if not isinstance(field, dict):
        return ""
    if field.get("confidence") not in {"high", "medium"} or not field.get("source_ids"):
        return ""
    return str(field.get("value") or "")


def company_momentum_signal(company: str) -> tuple[int, list[str]]:
    """Score cited scale, technical intensity, prestige, and pace generically."""
    record = _company_record(company)
    prestige = _supported_field(record, "ai_ds_prestige_tier")
    scale = _supported_field(record, "size_stage")
    pace = _supported_field(record, "pace_of_work")
    technical = _supported_field(record, "technical_work")
    points = 0
    reasons: list[str] = []
    if re.search(r"\b(top[- ]tier|tier\s*1|world[- ]class|global(?:ly)?\s+(?:recognized|leading)|industry leader)", prestige, re.I):
        points += 4
        reasons.append("cited AI/technical prestige +4")
    elif re.search(r"\b(strong|leading|recognized|highly regarded)\b", prestige, re.I):
        points += 2
        reasons.append("cited technical reputation +2")
    if re.search(r"\b(fast[- ]paced|rapid iteration|high[- ]growth|rapidly growing|high velocity)\b", pace, re.I):
        points += 2
        reasons.append("cited company momentum +2")
    elif re.search(r"\b(slow|bureaucratic|limited innovation)\b", pace, re.I):
        points -= 2
        reasons.append("cited low company momentum -2")
    if re.search(r"\b(frontier|cutting[- ]edge|large[- ]scale|distributed training|core AI|AI/ML infrastructure|research)\b", technical, re.I):
        points += 2
        reasons.append("cited technical intensity +2")
    if re.search(r"\b(global|public company|fortune\s*\d+|over\s+[\d,]+\s+employees)\b", scale, re.I):
        points += 1
        reasons.append("cited operating scale +1")
    return max(-3, min(points, 8)), reasons


# Tokens the taste model must never learn or reward: employment-shape noise,
# leaked location words, and off-field families (boosting "business" or
# "marketing" floods the board with roles outside Victor's field). Filtered
# symmetrically in _title_tokens, so stale entries already sitting in
# state/feedback.json become inert without touching the file.
FEEDBACK_STOPWORDS = {
    "full", "time", "onsite", "hybrid", "remote", "multiple", "positions",
    "available", "united", "states", "level", "mid", "amer", "early", "career",
    "san", "francisco", "nyc", "york", "creek", "fridley", "obispo", "luis",
    "business", "product", "products", "marketing", "solutions", "services",
    "operations", "program", "recruiter", "support", "success", "strategy",
    "partner", "client", "enterprise", "gov", "government", "monetization",
    "planning", "inbound", "shopping", "sharing", "value", "quality", "assurance",
}


def _title_tokens(title: str) -> set[str]:
    stop = {"engineer", "software", "the", "and", "of", "for", "a", "an", "i", "ii",
            "new", "grad"} | FEEDBACK_STOPWORDS
    return {w for w in norm(title).split() if len(w) > 2 and w not in stop}


def _salary_max(salary: str) -> int | None:
    if not salary:
        return None
    values = []
    for number, suffix in _MONEY_RE.findall(salary):
        try:
            value = float(number.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            value *= 1000
        values.append(value)
    if not values:
        return None
    maximum = max(values)
    if re.search(r"/\s*(?:hr|hour)|hourly", salary, re.I) and maximum < 1000:
        maximum *= 2080
    return round(maximum)


def compensation_signal(salary: str) -> tuple[int, str]:
    maximum = _salary_max(salary)
    if maximum is None or maximum < 120_000:
        return 0, ""
    if maximum >= 250_000:
        points = 15
    elif maximum >= 220_000:
        points = 13
    elif maximum >= 190_000:
        points = 10
    elif maximum >= 165_000:
        points = 7
    elif maximum >= 145_000:
        points = 4
    else:
        points = 2
    return points, f"compensation ceiling ${maximum:,} +{points}"


def wording_signal(title: str, description: str = "") -> tuple[int, list[str]]:
    """Posting-specific alignment so one employer's roles do not tie."""
    text = f"{title}\n{description[:2500]}"
    patterns = [
        (r"\b(deep learning|generative AI|large language model|LLMs?)\b", 4, "frontier AI wording"),
        (r"\b(machine learning|artificial intelligence|computer vision|NLP)\b", 3, "AI/ML wording"),
        (r"\b(data science|applied scientist|research engineer)\b", 3, "data/research wording"),
        (r"\b(cloud|distributed systems?|platform|backend|infrastructure)\b", 2, "systems/cloud wording"),
        (r"\b(healthcare|clinical|patient|drug|biomedical|medical)\b", 2, "health mission wording"),
        (r"\b(quality assurance|manual test|test engineer)\b", -3, "lower-priority QA wording"),
    ]
    points = 0
    reasons = []
    for pattern, value, label in patterns:
        if re.search(pattern, text, re.I):
            points += value
            reasons.append(f"{label} {'+' if value > 0 else ''}{value}")
    return max(-4, min(points, 10)), reasons


def calibrate_score(raw_utility: float) -> int:
    """Map uncapped utility onto a stable, non-percentile 0-100 scale."""
    anchors = (
        (0.0, 0.0),
        (35.0, 45.0),
        (55.0, 66.0),
        (70.0, 81.0),
        (85.0, 89.0),
        (100.0, 94.0),
        (115.0, 98.0),
        (125.0, 100.0),
    )
    raw = max(0.0, float(raw_utility))
    if raw >= anchors[-1][0]:
        return 100
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if raw <= x1:
            ratio = (raw - x0) / (x1 - x0)
            return max(0, min(100, round(y0 + ratio * (y1 - y0))))
    return 100


def apply_company_concentration(jobs) -> int:
    """Diversify crowded employers without suppressing their best role.

    This is a ranking adjustment, not a fit judgment. Once a company has at
    least three visible roles, its strongest role is protected; only weaker
    roles from that same company receive -1 or -2. Ties at the raw-utility
    level are left alone, so a genuinely equivalent NVIDIA role is not hidden
    merely because it shares an employer.
    """
    groups: dict[str, list] = {}
    values = jobs.values() if isinstance(jobs, dict) else jobs
    materialized = list(values)
    for job in materialized:
        if isinstance(job, dict):
            prior_adjustment = int(job.get("ranking_adjustment", 0) or 0)
            base_score = int(job.get("score_calibrated", job.get("score", 0) - prior_adjustment))
            job["score"] = base_score
            job["ranking_adjustment"] = 0
            job["score_reasons"] = [
                reason for reason in job.get("score_reasons", [])
                if not str(reason).startswith("company concentration:")
            ]
        else:
            job.score = int(job.score_calibrated)
            job.ranking_adjustment = 0
            job.score_reasons = [
                reason for reason in job.score_reasons
                if not str(reason).startswith("company concentration:")
            ]
        company = norm(getattr(job, "company", "") or job.get("company", "")) if isinstance(job, dict) else norm(job.company)
        groups.setdefault(company, []).append(job)
    changed = 0
    for company, group in groups.items():
        if not company or len(group) < 3:
            continue
        group.sort(key=lambda item: (
            -float(item.get("score_raw", 0) if isinstance(item, dict) else item.score_raw),
            -int(item.get("score", 0) if isinstance(item, dict) else item.score),
            str(item.get("title", "") if isinstance(item, dict) else item.title),
        ))
        best_raw = float(group[0].get("score_raw", 0) if isinstance(group[0], dict) else group[0].score_raw)
        company_name = str(group[0].get("company", company) if isinstance(group[0], dict) else group[0].company)
        for rank, job in enumerate(group):
            raw = float(job.get("score_raw", 0) if isinstance(job, dict) else job.score_raw)
            reasons_before = job.get("score_reasons", []) if isinstance(job, dict) else job.score_reasons
            override_protected = any(
                str(reason).startswith("configured score override:")
                for reason in reasons_before
            )
            penalty = (
                0 if rank == 0 or raw >= best_raw or override_protected
                else min(2, rank)
            )
            reason = None
            if penalty:
                reason = (
                    f"company concentration: {rank + 1} of {len(group)} {company_name} roles; "
                    f"-{penalty} to show stronger alternatives (best role protected)"
                )
            if isinstance(job, dict):
                job["ranking_adjustment"] = -penalty
                job["score"] = max(0, int(job.get("score_calibrated", job.get("score", 0))) - penalty)
                reasons = job.setdefault("score_reasons", [])
            else:
                job.ranking_adjustment = -penalty
                job.score = max(0, int(job.score_calibrated) - penalty)
                reasons = job.score_reasons
            if reason and reason not in reasons:
                reasons.append(reason)
                changed += 1
    return changed


def score(job: Job, feedback: dict, now: int | None = None) -> None:
    """Build uncapped dimension utility, then calibrate it for display."""
    p = profile()
    now = now or int(time.time())
    dimensions = {
        "base": 5,
        "role_fit": 0,
        "eligibility": 0,
        "mission": 0,
        "company_quality": 0,
        "compensation": 0,
        "personal_signal": 0,
        "timing_access": 0,
    }
    reasons = ["base utility +5"]

    bucket = role_bucket(job.title, job.description) or "swe"
    role_pts = p["roles"].get(bucket, 10)
    wording_pts, wording_reasons = wording_signal(job.title, job.description)
    dimensions["role_fit"] = role_pts + wording_pts
    reasons.append(f"role:{bucket} +{role_pts}")
    reasons.extend(wording_reasons)

    configured_sector = p["sectors"].get(job.sector or "other", 0)
    sector_pts = round(configured_sector * 0.7)
    if sector_pts:
        dimensions["mission"] += sector_pts
        reasons.append(f"sector:{job.sector} +{sector_pts} (diminishing return)")

    b = p["bonuses"]
    program = leadership_program_signal(job.company, job.title, job.description)
    new_grad = new_grad_signal(job.title, job.description) or source_new_grad(job)
    if new_grad or program:
        eligibility_pts = int(p.get("scoring_v8", {}).get("eligible_utility", 30))
        dimensions["eligibility"] = eligibility_pts
        evidence = ("trusted new-grad board" if source_new_grad(job)
                    and not new_grad_signal(job.title, job.description)
                    else "new-grad/early-career")
        reasons.append(f"{evidence} priority +{eligibility_pts} (eligibility)")
    elif early_career_possible(job):
        eligibility_pts = int(p.get("scoring_v9", {}).get("early_career_possible_utility", 8))
        dimensions["eligibility"] = eligibility_pts
        reasons.append(
            f"early-career possible +{eligibility_pts} (no experience floor; not new-grad verified)"
        )
    else:
        reasons.append("new-grad evidence absent (below eligible roles)")

    if program:
        program_pts = min(6, b.get("leadership_program", 0))
        dimensions["role_fit"] += program_pts
        reasons.append(f"technical leadership program +{program_pts}")
        if norm(job.company) in target_program_companies():
            target_pts = min(3, b.get("target_program_company", 0))
            dimensions["mission"] += target_pts
            reasons.append(f"target healthcare program company +{target_pts}")

    if is_marquee(job.company):
        marquee_pts = b.get("marquee_company", 0)
        dimensions["company_quality"] += marquee_pts
        reasons.append(f"company tier: marquee +{marquee_pts}")

    goal_companies = {norm(name) for name in p.get("goal_companies", [])}
    if norm(job.company) in goal_companies:
        goal_pts = int(p.get("scoring_v8", {}).get("goal_company_utility", 10))
        dimensions["company_quality"] += goal_pts
        reasons.append(f"explicit goal company +{goal_pts}")

    momentum_pts, momentum_reasons = company_momentum_signal(job.company)
    dimensions["company_quality"] += momentum_pts
    reasons.extend(momentum_reasons)

    pay_pts, pay_reason = compensation_signal(job.salary)
    dimensions["compensation"] = pay_pts
    if pay_reason:
        reasons.append(pay_reason)

    if job.posted_at:
        age_h = (now - job.posted_at) / 3600
        if age_h <= 24:
            fresh = min(4, b["fresh_24h"])
            dimensions["timing_access"] += fresh
            reasons.append(f"posted <24h +{fresh}")
        elif age_h <= 72:
            fresh = min(3, b["fresh_72h"])
            dimensions["timing_access"] += fresh
            reasons.append(f"posted <72h +{fresh}")
        elif age_h <= 168:
            fresh = min(1, b["fresh_7d"])
            dimensions["timing_access"] += fresh
            reasons.append(f"posted <7d +{fresh}")

    if job.remote:
        dimensions["timing_access"] += b["remote"]
        reasons.append(f"remote +{b['remote']}")

    comp = norm(job.company)
    cb = feedback.get("company_boosts", {}).get(comp, 0)
    if cb:
        cb = min(cb, b["feedback_company_max"])
        dimensions["personal_signal"] += cb
        reasons.append(f"you've engaged with {job.company} +{cb}")
    if comp in feedback.get("negative_companies", []):
        dimensions["personal_signal"] -= 10
        reasons.append("previously skipped -10")

    tb = 0
    boosts = feedback.get("token_boosts", {})
    for tok in _title_tokens(job.title):
        tb += boosts.get(tok, 0)
    tb = max(min(tb, b["feedback_tokens_max"]), -6)
    if tb:
        dimensions["personal_signal"] += tb
        reasons.append(f"title matches your history {'+' if tb > 0 else ''}{tb}")

    d = _culture_dossier(job.company)
    if d and d.get("source") == "seed" and d.get("fit") is not None:
        cf = round((d["fit"] - 50) / 50 * 6)
        if cf:
            dimensions["company_quality"] += cf
            reasons.append(f"culture fit {d['fit']}/100 {'+' if cf > 0 else ''}{cf}")

    if norm(job.company) in _shpe_companies():
        dimensions["personal_signal"] += 2
        reasons.append("SHPE 2026 exhibitor +2")

    raw_utility = round(sum(dimensions.values()), 1)
    display = calibrate_score(raw_utility)
    if program:
        floor = int(p["thresholds"].get("alert", 66))
        if display < floor:
            reasons.append(f"technical program display floor +{floor - display}")
            display = floor

    # Explicit, profile-driven favorites are data, not company-name branches
    # in the scorer. PM-family roles stay low per the friend-facing contract.
    for override in p.get("score_overrides", []):
        if (norm(override.get("company", "")) == norm(job.company)
                and override.get("when") == "new_grad"
                and new_grad
                and bucket not in set(override.get("exclude_buckets", []))):
            target = int(override.get("score", display))
            if target != display:
                reasons.append(
                    f"configured score override: {job.company} new-grad -> {target}")
            display = target
            break

    job.score_raw = raw_utility
    job.score_calibrated = display
    job.score_dimensions = dimensions
    job.score = display
    reasons.append(f"raw utility {raw_utility:g}; calibration v{RULES_VERSION} -> {display}/100")
    job.score_reasons = reasons


def _bump_feedback(fb: dict, company: str, title: str, company_delta: int, token_delta: int) -> dict:
    comp = norm(company)
    fb.setdefault("company_boosts", {})
    fb.setdefault("token_boosts", {})
    if company_delta:
        fb["company_boosts"][comp] = min(fb["company_boosts"].get(comp, 0) + company_delta, 8)
    if token_delta:
        for tok in _title_tokens(title):
            fb["token_boosts"][tok] = min(fb["token_boosts"].get(tok, 0) + token_delta, 4)
    return fb


def update_feedback_from_applied(fb: dict, company: str, title: str) -> dict:
    """Strong signal: a confirmed application (checkbox, email, or explicit)."""
    return _bump_feedback(fb, company, title, company_delta=2, token_delta=1)


# ---------- re-gating stored jobs after a rules change ----------


def regate(jobs_state: dict) -> int:
    """Re-run gates() on stored jobs whose rules_v predates RULES_VERSION.

    Title/salary/location only — descriptions are blanked in state. Flips
    alert_ok in place (demote or promote), never deletes a record and never
    re-opens a closed one. Stored LLM quality verdicts and posting-analysis
    effects are re-applied after gating so their suppressions still win.
    Returns how many records flipped.
    """
    from . import posting, quality  # late imports: both import from here
    flipped = 0
    for rec in jobs_state.values():
        if rec.get("rules_v", 1) >= RULES_VERSION or rec.get("closed_at"):
            continue
        job = Job(company=rec.get("company", ""), title=rec.get("title", ""),
                  url=rec.get("url", ""), source=rec.get("source", ""),
                  locations=rec.get("locations", []), salary=rec.get("salary", ""),
                  remote=bool(rec.get("remote")), sector=rec.get("sector", ""))
        keep, alert_eligible, reasons = gates(job)
        new_alert = keep and alert_eligible
        rec["explicit_new_grad"] = (explicit_new_grad(job.title)
                                     or source_new_grad(job))
        rec["rules_v"] = RULES_VERSION
        if bool(rec.get("alert_ok")) != new_alert:
            rec["alert_ok"] = new_alert
            detail = "; ".join(reasons) or ("now alert-eligible" if new_alert else "demoted")
            rec.setdefault("score_reasons", []).append(
                f"re-gate v{RULES_VERSION}: {detail}")
            flipped += 1
        if rec.get("quality"):
            quality.reapply(rec)
        if rec.get("posting"):
            posting.reapply(rec)
    return flipped
