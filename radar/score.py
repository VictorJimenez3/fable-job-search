"""Two-stage ranking: hard gates, then additive scoring with learned feedback.

Gates answer "should this ever reach the human". Score answers "how loudly".
Every point awarded is recorded in job.score_reasons so ranking is auditable.
"""
from __future__ import annotations

import math
import re
import time

from .config import profile
from .models import Job, norm

# ---------- hard gates ----------

# Bumped whenever gate rules or the deterministic ranking equation changes;
# stored jobs are rebuilt from source evidence under the new version.
RULES_VERSION = 13

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
        r"\b(?:apm|associate\s+product\s+manager|technical\s+product\s+manager|"
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

SCORE_DIMENSIONS = (
    "base", "role_fit", "eligibility", "mission", "company_quality",
    "compensation", "personal_signal", "timing_access",
)
# Baseline and early-career eligibility are not optional preferences: removing
# either would make a score stop answering the product's core question.
CONFIGURABLE_SCORE_DIMENSIONS = (
    "role_fit", "mission", "company_quality", "compensation",
    "personal_signal", "timing_access",
)
SCORE_PREFERENCES_VERSION = 1


def default_score_preferences() -> dict:
    return {
        "version": SCORE_PREFERENCES_VERSION,
        "enabled_dimensions": {name: True for name in SCORE_DIMENSIONS},
    }


def normalize_score_preferences(value: dict | None) -> dict:
    """Return the safe owner configuration used by every scorer entrypoint."""
    result = default_score_preferences()
    if not isinstance(value, dict):
        return result
    enabled = value.get("enabled_dimensions")
    if isinstance(enabled, dict):
        for name in CONFIGURABLE_SCORE_DIMENSIONS:
            if name in enabled:
                result["enabled_dimensions"][name] = bool(enabled[name])
    return result


def load_score_preferences() -> dict:
    """Load owner controls without making state required for library callers."""
    try:
        from . import state
        return normalize_score_preferences(state.load("score_preferences.json", {}))
    except Exception:
        return default_score_preferences()


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


def _supported_number(record: dict, name: str, low: float, high: float) -> float | None:
    """Read a bounded numeric claim only when its dossier evidence is cited."""
    field = record.get(name)
    if not isinstance(field, dict):
        return None
    if field.get("confidence") not in {"high", "medium"} or not field.get("source_ids"):
        return None
    try:
        value = float(str(field.get("value") or "").strip())
    except (TypeError, ValueError):
        return None
    return value if low <= value <= high else None


def company_momentum_signal(company: str) -> tuple[int, list[str]]:
    """Score cited company context; pace requires the objective 1–5 measure."""
    record = _company_record(company)
    prestige = _supported_field(record, "ai_ds_prestige_tier")
    scale = _supported_field(record, "size_stage")
    technical = _supported_field(record, "technical_work")
    points = 0
    reasons: list[str] = []
    if re.search(r"\b(top[- ]tier|tier\s*1|world[- ]class|global(?:ly)?\s+(?:recognized|leading)|industry leader)", prestige, re.I):
        points += 4
        reasons.append("cited AI/technical prestige +4")
    elif re.search(r"\b(strong|leading|recognized|highly regarded)\b", prestige, re.I):
        points += 2
        reasons.append("cited technical reputation +2")
    pace_score = _supported_number(record, "pace_score", 1, 5)
    if pace_score is not None:
        pace_points = {1: -2, 2: -1, 3: 0, 4: 2, 5: 3}[round(pace_score)]
        band = {
            1: "deliberate", 2: "measured", 3: "mixed/moderate",
            4: "fast", 5: "very fast",
        }[round(pace_score)]
        if pace_points:
            points += pace_points
        reasons.append(
            f"cited pace measure {round(pace_score)}/5 ({band}) "
            f"{'+' if pace_points > 0 else ''}{pace_points}"
        )
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


_SIBLING_TITLE_STOPWORDS = {
    "engineer", "engineering", "software", "developer", "development",
    "scientist", "science", "analyst", "new", "grad", "graduate", "college",
    "early", "career", "campus", "class", "full", "time", "role", "roles",
    "position", "positions", "year", "years", "level", "i", "ii", "iii",
}


def _sibling_title_tokens(title: str) -> set[str]:
    """Return distinctive title words for near-duplicate role comparison.

    This deliberately ignores employment-shape and early-career wording. The
    comparison is only used inside one employer and role bucket, so shared
    domain words such as ``inference`` or ``compiler`` identify a meaningful
    sibling without treating every software role as a duplicate.
    """
    return {
        token for token in norm(title).split()
        if len(token) > 2
        and not any(char.isdigit() for char in token)
        and token not in _SIBLING_TITLE_STOPWORDS
        and token not in FEEDBACK_STOPWORDS
    }


def _title_similarity(left: str, right: str) -> float:
    """Return a conservative token-overlap score for role siblings."""
    a = _sibling_title_tokens(left)
    b = _sibling_title_tokens(right)
    if not a or not b:
        return 0.0
    shared = len(a & b)
    if shared < 2:
        return 0.0
    # Containment catches a longer, more specific requisition beside its
    # shorter sibling; Jaccard keeps broad shared wording from matching.
    containment = shared / min(len(a), len(b))
    jaccard = shared / len(a | b)
    if containment >= 0.8 or jaccard >= 0.5:
        return max(containment, jaccard)
    return 0.0


# The owner-facing saved/applied list is a positive sample, not a second set
# of hard rules.  Keep its influence bounded and explain every contribution.
# This model deliberately uses only structured title/company/sector fields;
# posting prose is too repetitive to be a reliable preference label.
PREFERENCE_MODEL_VERSION = 1
PREFERENCE_MIN_SAMPLE = 8
_PREFERENCE_ROLES = ("ai_ml", "data_science", "swe", "data_eng")
_PREFERENCE_SECTORS = ("healthtech", "sports", "video games", "ai lab",
                       "big tech", "edtech", "fintech")
_PREFERENCE_STAGE_WEIGHTS = {
    "saved": 1.0,
    "applied": 2.0,
    "oa": 2.5,
    "interview": 3.0,
    "rejected": 1.5,
    "closed": 0.0,
}
_PREFERENCE_TITLE_STOPWORDS = {
    "software", "engineer", "engineering", "developer", "scientist",
    "analyst", "associate", "junior", "new", "grad", "graduate",
    "early", "career", "campus", "college", "class", "level", "full",
    "time", "remote", "onsite", "hybrid", "multiple", "position",
    "positions", "role", "roles", "year", "years", "united", "states",
    "america", "us", "usa",
}


def _preference_title_tokens(title: str) -> set[str]:
    """Return meaningful title concepts for the implicit positive sample."""
    return {
        token for token in norm(title).split()
        if len(token) > 2 and not any(char.isdigit() for char in token)
        and token not in _PREFERENCE_TITLE_STOPWORDS
        and token not in FEEDBACK_STOPWORDS
    }


def _sample_weight(entry: dict) -> float:
    stage = str(entry.get("stage") or "saved").lower().strip()
    return _PREFERENCE_STAGE_WEIGHTS.get(stage, 1.0)


def build_preference_profile(sample: list[dict] | None,
                             jobs_state: dict | None = None) -> dict:
    """Build a bounded, deterministic profile from saved/applied roles.

    The profile is rebuilt from source state on every crawl/rescore, so
    removing a saved role actually removes its learned influence.  Confirmed
    applications and later funnel stages count more than a simple save, while
    closed records contribute nothing.  ``jobs_state`` supplies sector values
    that are intentionally not duplicated in ``applied.json`` history.
    """
    jobs_state = jobs_state or {}
    roles: dict[str, float] = {}
    sectors: dict[str, float] = {}
    companies: dict[str, float] = {}
    title_tokens: dict[str, float] = {}
    sample_count = 0
    weighted_count = 0.0
    for entry in sample or []:
        if not isinstance(entry, dict):
            continue
        weight = _sample_weight(entry)
        if weight <= 0:
            continue
        record = jobs_state.get(str(entry.get("id") or ""), {})
        if not isinstance(record, dict):
            record = {}
        title = str(entry.get("title") or record.get("title") or "").strip()
        company = norm(entry.get("company") or record.get("company") or "")
        if not title or not company:
            continue
        sample_count += 1
        weighted_count += weight
        companies[company] = companies.get(company, 0.0) + weight
        bucket = role_bucket(title)
        if bucket in _PREFERENCE_ROLES:
            roles[bucket] = roles.get(bucket, 0.0) + weight
        sector = norm(entry.get("sector") or record.get("sector") or "")
        if sector:
            sectors[sector] = sectors.get(sector, 0.0) + weight
        for token in _preference_title_tokens(title):
            title_tokens[token] = title_tokens.get(token, 0.0) + weight
    return {
        "version": PREFERENCE_MODEL_VERSION,
        "sample_count": sample_count,
        "weighted_count": round(weighted_count, 2),
        "roles": roles,
        "sectors": sectors,
        "companies": companies,
        "title_tokens": title_tokens,
    }


def preference_signal(job: Job, preference_profile: dict | None) -> tuple[int, list[str]]:
    """Return the capped score lift from the owner's positive role sample."""
    if not preference_profile or int(preference_profile.get("sample_count", 0)) < PREFERENCE_MIN_SAMPLE:
        return 0, []
    sample_count = int(preference_profile["sample_count"])
    roles = preference_profile.get("roles", {})
    role_total = sum(float(value or 0) for value in roles.values())
    sectors = preference_profile.get("sectors", {})
    companies = preference_profile.get("companies", {})
    title_tokens = preference_profile.get("title_tokens", {})
    parts: list[int] = []
    reasons: list[str] = []

    bucket = role_bucket(job.title, job.description)
    # Learned enthusiasm must never make an off-field or PM research row look
    # like a target technical role. Gates decide visibility; this model only
    # refines rows already in one of the four target families.
    if bucket not in _PREFERENCE_ROLES:
        return 0, []
    configured_roles = profile().get("roles", {})
    configured_role_total = sum(
        max(0.0, float(configured_roles.get(name, 0) or 0))
        for name in _PREFERENCE_ROLES
    )
    if bucket in _PREFERENCE_ROLES and role_total and configured_role_total:
        observed = float(roles.get(bucket, 0) or 0) / role_total
        expected = max(0.0, float(configured_roles.get(bucket, 0) or 0)) / configured_role_total
        # A sparse positive sample is evidence for what Victor chose, not
        # proof that an underrepresented field is unwanted. Explicit
        # "less like this" feedback is the downrank path.
        role_points = max(0, min(3, round((observed - expected) * 12)))
        if role_points:
            parts.append(role_points)
            reasons.append(
                f"learned role preference: {bucket} {round(observed * 100)}% of sample "
                f"{'+' if role_points > 0 else ''}{role_points}"
            )

    configured_sectors = profile().get("sectors", {})
    recognized_total = sum(float(sectors.get(name, 0) or 0) for name in _PREFERENCE_SECTORS)
    configured_sector_total = sum(
        max(0.0, float(configured_sectors.get(name.replace(" ", "_"), 0) or 0))
        for name in _PREFERENCE_SECTORS
    )
    sector = norm(job.sector or "")
    if sector in _PREFERENCE_SECTORS and recognized_total and configured_sector_total:
        observed = float(sectors.get(sector, 0) or 0) / recognized_total
        expected = max(0.0, float(configured_sectors.get(sector.replace(" ", "_"), 0) or 0)) / configured_sector_total
        sector_points = max(0, min(3, round((observed - expected) * 10)))
        if sector_points:
            parts.append(sector_points)
            reasons.append(
                f"learned sector preference: {sector} {round(observed * 100)}% of recognized sample "
                f"{'+' if sector_points > 0 else ''}{sector_points}"
            )

    company = norm(job.company)
    company_count = float(companies.get(company, 0) or 0)
    if company_count:
        company_points = min(5, max(1, int(math.log2(company_count + 1))))
        parts.append(company_points)
        reasons.append(
            f"learned company preference: {job.company} appears in "
            f"{round(company_count)} saved/applied roles +{company_points}"
        )

    matched_tokens = sorted(
        ((token, float(title_tokens.get(token, 0) or 0))
         for token in _preference_title_tokens(job.title)
         if float(title_tokens.get(token, 0) or 0) >= 2),
        key=lambda item: (-item[1], item[0]),
    )
    if matched_tokens:
        token_points = round(min(
            4.0,
            sum(min(1.25, 0.25 * math.log2(count + 1) + 0.25)
                for _, count in matched_tokens[:5]),
        ))
        if token_points:
            parts.append(token_points)
            labels = ", ".join(token for token, _ in matched_tokens[:3])
            reasons.append(f"learned title signals: {labels} +{token_points}")

    if not parts:
        return 0, []
    points = max(-8, min(12, sum(parts)))
    reasons.insert(0, f"learned from {sample_count} saved/applied roles")
    return points, reasons


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
        (30.0, 40.0),
        (50.0, 55.0),
        (65.0, 70.0),
        (80.0, 80.0),
        (95.0, 88.0),
        (110.0, 93.0),
        (125.0, 97.0),
        (140.0, 99.0),
        (150.0, 100.0),
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
    """Diversify crowded employers while preserving true duplicate variants.

    The adjustment is a ranking aid, not a fit judgment. Exact same-company,
    same-title postings are location/requisition variants and tie with the
    strongest variant. For non-identical titles, only a weaker near-duplicate
    is nudged, then the existing broad company guard keeps a crowded employer
    from filling the whole top of the board. Every change is recorded.
    """
    groups: dict[str, list] = {}
    exact_groups: dict[tuple[str, str], list] = {}
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
                if not str(reason).startswith((
                    "company concentration:",
                    "similar role sibling:",
                    "duplicate role variant:",
                ))
            ]
        else:
            job.score = int(job.score_calibrated)
            job.ranking_adjustment = 0
            job.score_reasons = [
                reason for reason in job.score_reasons
                if not str(reason).startswith((
                    "company concentration:",
                    "similar role sibling:",
                    "duplicate role variant:",
                ))
            ]
        company = norm(getattr(job, "company", "") or job.get("company", "")) if isinstance(job, dict) else norm(job.company)
        title = str(job.get("title", "") if isinstance(job, dict) else job.title)
        groups.setdefault(company, []).append(job)
        exact_groups.setdefault((company, norm(title)), []).append(job)

    changed = 0
    exact_duplicate_jobs: set[int] = set()
    # A same-title posting at another location/requisition is not a weaker
    # sibling. Tie its displayed score to the strongest variant, but keep each
    # posting so the owner can choose the location that works.
    for (company, title), duplicate_group in exact_groups.items():
        if not company or not title or len(duplicate_group) < 2:
            continue
        best_score = max(
            int(item.get("score_calibrated", item.get("score", 0)) if isinstance(item, dict)
                else item.score_calibrated)
            for item in duplicate_group
        )
        locations = set()
        for item in duplicate_group:
            exact_duplicate_jobs.add(id(item))
            locations_value = item.get("locations", []) if isinstance(item, dict) else item.locations
            locations.update(str(location) for location in (locations_value or []) if location)
        company_name = str(duplicate_group[0].get("company", company)
                           if isinstance(duplicate_group[0], dict) else duplicate_group[0].company)
        reason = (
            f"duplicate role variant: {len(duplicate_group)} {company_name} postings share this title; "
            f"tied at {best_score}/100 across {max(1, len(locations))} location set(s)"
        )
        for item in duplicate_group:
            if isinstance(item, dict):
                item["score"] = best_score
                item["ranking_adjustment"] = 0
                reasons = item.setdefault("score_reasons", [])
            else:
                item.score = best_score
                item.ranking_adjustment = 0
                reasons = item.score_reasons
            if reason not in reasons:
                reasons.append(reason)
                changed += 1

    # Near-duplicate sibling penalties are intentionally small. A 1–4 raw
    # point edge gets -1; a more material 5–10 point edge gets -2; only a much
    # stronger sibling reaches -3. This leaves adjacent roles close while
    # stopping a family of almost-identical postings from occupying the top.
    sibling_penalties: dict[int, tuple[int, str]] = {}
    for company, group in groups.items():
        if not company or len(group) < 2:
            continue
        for current in group:
            if id(current) in exact_duplicate_jobs:
                continue
            current_title = str(current.get("title", "") if isinstance(current, dict) else current.title)
            current_bucket = role_bucket(current_title)
            if not current_bucket:
                continue
            current_reasons = current.get("score_reasons", []) if isinstance(current, dict) else current.score_reasons
            if any(str(reason).startswith("configured score override:") for reason in current_reasons):
                continue
            current_raw = float(current.get("score_raw", 0) if isinstance(current, dict) else current.score_raw)
            stronger = []
            for candidate in group:
                if candidate is current:
                    continue
                candidate_title = str(candidate.get("title", "") if isinstance(candidate, dict) else candidate.title)
                if role_bucket(candidate_title) != current_bucket:
                    continue
                candidate_raw = float(candidate.get("score_raw", 0) if isinstance(candidate, dict) else candidate.score_raw)
                if candidate_raw <= current_raw:
                    continue
                similarity = _title_similarity(current_title, candidate_title)
                if similarity:
                    stronger.append((candidate_raw, similarity, candidate_title, candidate))
            if not stronger:
                continue
            best_raw, similarity, best_title, _ = max(stronger, key=lambda item: (item[0], item[1], item[2]))
            gap = best_raw - current_raw
            penalty = 1 if gap <= 4 else 2 if gap <= 10 else 3
            sibling_penalties[id(current)] = (penalty, best_title)

    # Apply sibling adjustments even when the company only has two visible
    # postings. Google-style configured favorites remain protected; their
    # explicit override is a stronger signal than diversity spacing.
    for job in materialized:
        penalty, sibling_title = sibling_penalties.get(id(job), (0, ""))
        if not penalty:
            continue
        reasons = job.get("score_reasons", []) if isinstance(job, dict) else job.score_reasons
        if any(str(reason).startswith("configured score override:") for reason in reasons):
            continue
        if isinstance(job, dict):
            job["ranking_adjustment"] = -penalty
            job["score"] = max(0, int(job.get("score_calibrated", job.get("score", 0))) - penalty)
            reasons = job.setdefault("score_reasons", [])
        else:
            job.ranking_adjustment = -penalty
            job.score = max(0, int(job.score_calibrated) - penalty)
            reasons = job.score_reasons
        sibling_reason = (
            f"similar role sibling: stronger {sibling_title}; -{penalty} "
            f"to separate the near-duplicate"
        )
        if sibling_reason not in reasons:
            reasons.append(sibling_reason)
            changed += 1

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
            if id(job) in exact_duplicate_jobs:
                # The duplicate pass already tied these postings. Do not let
                # the employer-level pass undo that tie.
                if isinstance(job, dict):
                    job["ranking_adjustment"] = 0
                else:
                    job.ranking_adjustment = 0
                continue
            raw = float(job.get("score_raw", 0) if isinstance(job, dict) else job.score_raw)
            reasons_before = job.get("score_reasons", []) if isinstance(job, dict) else job.score_reasons
            override_protected = any(
                str(reason).startswith("configured score override:")
                for reason in reasons_before
            )
            broad_penalty = (
                0 if id(job) in exact_duplicate_jobs or rank == 0 or raw >= best_raw or override_protected
                else min(2, rank)
            )
            sibling_penalty = sibling_penalties.get(id(job), (0, ""))[0]
            if override_protected:
                sibling_penalty = 0
            penalty = broad_penalty + sibling_penalty
            reasons = reasons_before
            if broad_penalty:
                reason = (
                    f"company concentration: {rank + 1} of {len(group)} {company_name} roles; "
                    f"-{broad_penalty} to show stronger alternatives (best role protected)"
                )
            else:
                reason = None
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


def score(job: Job, feedback: dict, now: int | None = None,
          preference_profile: dict | None = None,
          score_preferences: dict | None = None) -> None:
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
    midlevel = bool(MIDLEVEL_RE.search(job.title))
    if midlevel:
        # Keep the posting for research, but make the dashboard score reflect
        # the same reality as the gate: a level-II/L4 role is not a realistic
        # new-grad target even when its title also says "early career".
        midlevel_penalty = int(
            p.get("scoring_v10", {}).get("midlevel_utility_penalty", -28))
        dimensions["eligibility"] = midlevel_penalty
        reasons.append(
            f"mid-level title penalty {midlevel_penalty} (dashboard only; no new-grad target)"
        )
    elif new_grad or program:
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
    use_preference_model = bool(
        preference_profile
        and int(preference_profile.get("sample_count", 0)) >= PREFERENCE_MIN_SAMPLE
    )
    if use_preference_model:
        learned_points, learned_reasons = preference_signal(job, preference_profile)
        dimensions["personal_signal"] += learned_points
        reasons.extend(learned_reasons)

        # Explicit feedback is kept separate from the implicit sample.  The
        # legacy company/token maps are intentionally not added here: older
        # versions populated them for every save, which would double-count the
        # same 253-role sample and would survive after a role is untracked.
        explicit_company = feedback.get("explicit_company_boosts", {}).get(comp, 0)
        if explicit_company:
            explicit_company = max(-b["feedback_company_max"],
                                   min(explicit_company, b["feedback_company_max"]))
            dimensions["personal_signal"] += explicit_company
            reasons.append(
                f"explicit company feedback {'+' if explicit_company > 0 else ''}{explicit_company}"
            )
        explicit_tokens = feedback.get("explicit_token_boosts", {})
        explicit_title = sum(explicit_tokens.get(tok, 0) for tok in _title_tokens(job.title))
        explicit_title = max(min(explicit_title, b["feedback_tokens_max"]), -6)
        if explicit_title:
            dimensions["personal_signal"] += explicit_title
            reasons.append(
                f"explicit title feedback {'+' if explicit_title > 0 else ''}{explicit_title}"
            )
    else:
        # Compatibility path for library callers and older state repairs that
        # have not supplied the source sample yet.
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

    if use_preference_model and comp in feedback.get("negative_companies", []):
        dimensions["personal_signal"] -= 10
        reasons.append("previously skipped -10")

    d = _culture_dossier(job.company)
    if d and d.get("source") == "seed" and d.get("fit") is not None:
        cf = round((d["fit"] - 50) / 50 * 6)
        if cf:
            dimensions["company_quality"] += cf
            reasons.append(f"culture fit {d['fit']}/100 {'+' if cf > 0 else ''}{cf}")

    if norm(job.company) in _shpe_companies():
        dimensions["personal_signal"] += 2
        reasons.append("SHPE 2026 exhibitor +2")

    # Keep the unweighted contributions for audit/debugging, then apply the
    # owner's optional section switches. The stored score_dimensions remain
    # the actual points used in raw utility, so the equation is inspectable.
    raw_dimensions = dict(dimensions)
    score_preferences = normalize_score_preferences(score_preferences)
    enabled = score_preferences["enabled_dimensions"]
    for name in CONFIGURABLE_SCORE_DIMENSIONS:
        if enabled.get(name, True):
            continue
        points = dimensions.get(name, 0)
        if points:
            reasons.append(
                f"score section disabled: {name} ({'+' if points > 0 else ''}{points} excluded)"
            )
        dimensions[name] = 0

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
                and not midlevel
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
    job.score_dimensions_raw = raw_dimensions
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
