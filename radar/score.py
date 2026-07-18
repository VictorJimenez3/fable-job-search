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
RULES_VERSION = 5

SENIOR_RE = re.compile(
    r"\b(senior|staff|principal|lead(er)?|director|manager|head of|sr\.?|vp|chief|"
    r"architect|distinguished|fellow|executive|iii|iv|"
    r"engineer\s+[3-9]|l[5-9]|level\s+[3-9])\b", re.I)
# Typically 1-3 yrs experience: worth seeing on the dashboard, never an alert.
MIDLEVEL_RE = re.compile(r"\b(ii|l4|engineer\s+2|level\s+2|mid([- ]level)?)\b", re.I)
# Roles outside this branch's field. Title-only, demote-only (alert_ok=False, job
# stays on the dashboard) and outranks every auto-alert path incl. marquee.
# Deliberately narrow: "Product Engineer" / "Security Engineer" must NOT match.
OFF_FIELD_RE = re.compile(
    r"\b(software|frontend|backend|full[- ]?stack|data\s+(scien|engineer(?:ing)?)|machine\s+learning|\bai\b|"
    r"firmware|computer\s+vision|network\s+engineer|reinforcement\s+learning|"
    r"digital\s+verification|radio\s+frequency|hardware\s+(design|engineer)|electronic\s+design|"
    r"electrical|electronics|mechanical|civil|aerospace|computer\s+engineering|cyber|"
    r"policy|counsel|legal|paralegal|compliance|"
    r"recruit(er|ing)|talent|people\s+(ops|operations)|human\s+resources|hr|"
    r"sales|account\s+(executive|manager)|business\s+(development|operations|analyst)|"
    r"go[- ]to[- ]market|gtm|partnerships?|"
    r"marketing|brand|communications?|comms|public\s+relations|editorial|"
    r"finance|financial\s+analyst|accounting|accountant|payroll|procurement|revenue|"
    r"solutions?\s+(engineer|architect|consultant)|sales\s+engineer|"
    r"customer\s+(success|support|experience)|technical\s+support|"
    r"support\s+(engineer|specialist)|help\s?desk|"
    r"(ux|ui|visual|graphic|product)\s+design(er)?|"
    r"(product|program|project)\s+manager|product\s+(owner|marketing)|"
    r"chief\s+of\s+staff|executive\s+assistant|administrative|"
    r"workplace|facilities)\b", re.I)
ADJACENT_ENGINEERING_RE = re.compile(
    r"\b(electrical|electronics|mechanical|civil|aerospace|industrial)\b", re.I)
GENERIC_ALERT_RE = re.compile(
    r"^(engineering (intern(ship)?|co-?op)|engineer co-?op|r&d (engineer )?(intern|co-?op)|"
    r"research (and|&) development intern|laboratory intern|lab intern|technical intern)\b", re.I)
INTERNSHIP_RE = re.compile(r"\b(intern(ship)?|co-?op|student program|summer student)\b", re.I)
FULL_TIME_RE = re.compile(r"\b(full[- ]?time|new ?grad|university grad|entry[- ]level)\b", re.I)
PHD_RE = re.compile(r"\b(ph\.?d|doctorate|doctoral|postdoc)\b", re.I)
CLEARANCE_RE = re.compile(r"\b(security clearance|ts/sci|polygraph|top secret)\b", re.I)
YEARS_RE = re.compile(r"(?:minimum|at least|requires?)\s+(\d+)\+?\s+years", re.I)

ROLE_BUCKETS: dict[str, re.Pattern] = {
    "chemical_process": re.compile(
        r"chemical engineer|process (engineer|engineering|development|design|technology|safety)|"
        r"process intern|intern.{0,30}\bprocess\b|reaction engineering|separations|"
        r"unit operations|scale[- ]?up", re.I),
    "bioprocess_pharma": re.compile(
        r"bioprocess|biomanufactur|biochemical|fermentation|upstream|downstream|"
        r"process sciences|drug product|drug substance|msat|pharmaceutical", re.I),
    "manufacturing_ops": re.compile(
        r"manufacturing engineer|manufacturing intern|operations engineer|production engineer|"
        r"plant engineer|process improvement|continuous improvement|industrial engineer", re.I),
    "materials_semiconductor": re.compile(
        r"materials? (engineer|engineering|science|intern)|polymer|coatings?|battery|electrochem|"
        r"semiconductor process|wafer|fabrication|thin film|metallurg", re.I),
    "environmental_safety": re.compile(
        r"environmental engineer|environmental engineering|environmental.{0,25}intern|"
        r"water|wastewater|sustainability engineer|ehs\b|hse\b|process safety", re.I),
    "quality_validation": re.compile(
        r"quality engineer|quality engineering|validation engineer|validation intern|"
        r"process validation|quality control|quality assurance", re.I),
    "general_engineering": re.compile(
        r"\b(engineering intern|engineer intern|engineering co-?op|engineer co-?op|"
        r"r&d intern|research (and|&) development intern|laboratory intern|lab intern|technical intern)\b", re.I),
}

FOREIGN_HINTS = re.compile(
    r"\b(canada|toronto|vancouver|london|uk\b|united kingdom|ireland|dublin|belgium|germany|berlin|munich|"
    r"france|paris|netherlands|amsterdam|india|bangalore|bengaluru|hyderabad|pune|chennai|gurgaon|"
    r"noida|mumbai|singapore|japan|tokyo|china|beijing|shanghai|shenzhen|australia|sydney|melbourne|"
    r"brazil|sao paulo|mexico city|poland|warsaw|krakow|israel|tel aviv|spain|madrid|barcelona|"
    r"portugal|lisbon|switzerland|zurich|sweden|stockholm|estonia|romania|dubai|uae|philippines|"
    r"manila|vietnam|korea|seoul|taiwan|taipei|nigeria|kenya|south africa|argentina|colombia|chile|"
    r"panam[aá]|bangladesh|pakistan|malaysia|bintulu|venezuela)\b", re.I)
FOREIGN_ISO3_RE = re.compile(
    r"\b(AFG|ALB|DZA|ARG|ARM|AUS|AUT|AZE|BHR|BGD|BLR|BEL|BOL|BIH|BWA|BRA|BGR|"
    r"KHM|CMR|CAN|CHL|CHN|COL|CRI|HRV|CYP|CZE|DNK|ECU|EGY|EST|ETH|FIN|FRA|"
    r"GEO|DEU|GHA|GRC|HKG|HUN|ISL|IND|IDN|IRL|ISR|ITA|JPN|JOR|KEN|KOR|KWT|"
    r"LVA|LTU|LUX|MYS|MEX|MAR|NLD|NZL|NGA|NOR|PAK|PAN|PER|PHL|POL|PRT|QAT|"
    r"ROU|RUS|SAU|SGP|SVK|SVN|ZAF|ESP|LKA|SWE|CHE|TWN|THA|TUR|UKR|ARE|GBR|"
    r"URY|VEN|VNM)\b")
US_HINTS = re.compile(
    r"\b(us|usa|u\.s\.|united states|remote)\b|"
    r"\b(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|"
    r"nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc)\b|"
    r"new york|san francisco|seattle|austin|boston|chicago|denver|atlanta|los angeles|san jose|"
    r"palo alto|mountain view|sunnyvale|redmond|bellevue|cambridge|philadelphia|miami|dallas|"
    r"houston|phoenix|portland|salt lake|pittsburgh|raleigh|durham|nashville|minneapolis|detroit|"
    r"washington|arlington|reston|mclean|santa clara|menlo park|cupertino|irvine|san diego|boulder", re.I)


def role_bucket(title: str, description: str = "") -> str | None:
    for bucket in ("bioprocess_pharma", "chemical_process", "manufacturing_ops",
                   "materials_semiconductor", "environmental_safety",
                   "quality_validation"):
        if ROLE_BUCKETS[bucket].search(title):
            return bucket
    # Description fallback is intentionally limited to generic engineering
    # internship titles. Company/JD boilerplate must not promote unrelated jobs.
    if description and ROLE_BUCKETS["general_engineering"].search(title):
        for bucket in ("bioprocess_pharma", "chemical_process", "manufacturing_ops",
                       "materials_semiconductor", "environmental_safety",
                       "quality_validation"):
            if ROLE_BUCKETS[bucket].search(description[:600]):
                return bucket
        return "general_engineering"
    if ROLE_BUCKETS["general_engineering"].search(title):
        return "general_engineering"
    return None


def location_ok(job: Job) -> bool:
    if job.remote:
        return True
    if not job.locations:
        return True  # unknown — don't drop, scorer just won't reward it
    blob = " | ".join(job.locations)
    if FOREIGN_HINTS.search(blob) or FOREIGN_ISO3_RE.search(blob):
        # A posting may list several locations. Keep it when at least one is
        # recognizably US; otherwise explicit country names and ISO-3 codes
        # from Workday are stronger evidence than an unknown city name.
        return bool(US_HINTS.search(blob))
    return True


def gates(job: Job) -> tuple[bool, bool, list[str]]:
    """Returns (keep_at_all, alert_eligible, reasons)."""
    t = job.title
    text = f"{t}\n{job.description[:1500]}"
    if SENIOR_RE.search(t):
        return False, False, ["senior+ title"]
    if PHD_RE.search(t):
        return False, False, ["PhD-targeted title"]
    if CLEARANCE_RE.search(text):
        return False, False, ["requires clearance"]
    if not location_ok(job):
        return False, False, ["non-US location"]
    if not INTERNSHIP_RE.search(text):
        return False, False, ["not an internship/co-op"]
    m = YEARS_RE.search(job.description)
    if m and int(m.group(1)) >= 3:
        return False, False, [f"requires {m.group(1)}+ years"]
    bucket = role_bucket(t, job.description)
    # Keep nearby engineering disciplines as optional dashboard exploration,
    # but do not persist clearly unrelated software/business internship noise.
    if OFF_FIELD_RE.search(t) and not ADJACENT_ENGINEERING_RE.search(t):
        return False, False, ["off-field internship"]
    if bucket is None:
        if ADJACENT_ENGINEERING_RE.search(t):
            return True, False, ["off-field internship (dashboard only)"]
        return False, False, ["not a chemical/process engineering role"]

    reasons = ["internship/co-op title"]
    alert_eligible = True
    if OFF_FIELD_RE.search(t):
        alert_eligible = False
        reasons.append("off-field discipline (dashboard only)")
    if bucket == "general_engineering" and job.sector not in priority_sectors():
        alert_eligible = False
        reasons.append("generic engineering internship outside target sectors (dashboard only)")
    elif bucket == "general_engineering" and not GENERIC_ALERT_RE.search(t):
        alert_eligible = False
        reasons.append("generic/adjacent engineering title needs review (dashboard only)")
    if not alert_eligible and not reasons:
        reasons.append("internship fit unclear (dashboard only)")
    return True, alert_eligible, reasons


def explicit_new_grad(title: str) -> bool:
    """Compatibility name: True when the title explicitly says intern/co-op."""
    return bool(INTERNSHIP_RE.search(title))


def _strong_role_title(t: str) -> bool:
    """True for a discipline-specific ChemE title, not generic engineering."""
    return role_bucket(t) not in (None, "general_engineering")


_MARQUEE_CACHE: set | None = None
_PRIORITY_SECTORS: set | None = None


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


# Tokens the taste model must never learn or reward: employment-shape noise,
# leaked location words, and off-field families (boosting "business" or
# "marketing" floods the board with roles outside the candidate's field). Filtered
# symmetrically in _title_tokens, so stale entries already sitting in
# inherited feedback files remain inert because this profile has its own state.
FEEDBACK_STOPWORDS = {
    "full", "time", "onsite", "hybrid", "remote", "multiple", "positions",
    "available", "united", "states", "level", "mid", "amer", "early", "career",
    "san", "francisco", "nyc", "york", "creek", "fridley", "obispo", "luis",
    "business", "marketing", "solutions", "services",
    "program", "recruiter", "support", "success", "strategy",
    "partner", "client", "enterprise", "gov", "government", "monetization",
    "planning", "inbound", "shopping", "sharing", "value",
}


def _title_tokens(title: str) -> set[str]:
    stop = {"engineer", "engineering", "intern", "internship", "coop", "co", "op",
            "the", "and", "of", "for", "a", "an", "i", "ii"} | FEEDBACK_STOPWORDS
    return {w for w in norm(title).split() if len(w) > 2 and w not in stop}


def score(job: Job, feedback: dict, now: int | None = None) -> None:
    """Mutates job.score / job.score_reasons. Assumes gates already passed."""
    p = profile()
    now = now or int(time.time())
    pts = 40
    reasons = ["base 40"]

    bucket = role_bucket(job.title, job.description) or "general_engineering"
    role_pts = p["roles"].get(bucket, 10)
    pts += role_pts
    reasons.append(f"role:{bucket} +{role_pts}")

    sector_pts = p["sectors"].get(job.sector or "other", 0)
    if sector_pts:
        pts += sector_pts
        reasons.append(f"sector:{job.sector} +{sector_pts}")

    b = p["bonuses"]
    if INTERNSHIP_RE.search(job.title):
        bonus = b.get("explicit_internship_title", b.get("explicit_new_grad_title", 0))
        pts += bonus
        reasons.append(f"internship title +{bonus}")

    if job.posted_at:
        age_h = (now - job.posted_at) / 3600
        if age_h <= 24:
            pts += b["fresh_24h"]; reasons.append(f"posted <24h +{b['fresh_24h']}")
        elif age_h <= 72:
            pts += b["fresh_72h"]; reasons.append(f"posted <72h +{b['fresh_72h']}")
        elif age_h <= 168:
            pts += b["fresh_7d"]; reasons.append(f"posted <7d +{b['fresh_7d']}")

    if job.remote:
        pts += b["remote"]; reasons.append(f"remote +{b['remote']}")

    comp = norm(job.company)
    cb = feedback.get("company_boosts", {}).get(comp, 0)
    if cb:
        cb = min(cb, b["feedback_company_max"])
        pts += cb
        reasons.append(f"you've engaged with {job.company} +{cb}")
    if comp in feedback.get("negative_companies", []):
        pts -= 10
        reasons.append("previously skipped -10")

    tb = 0
    boosts = feedback.get("token_boosts", {})
    for tok in _title_tokens(job.title):
        tb += boosts.get(tok, 0)
    tb = max(min(tb, b["feedback_tokens_max"]), -6)
    if tb:
        pts += tb
        reasons.append(f"title matches your history {'+' if tb > 0 else ''}{tb}")

    # culture fit (±6) when a dossier exists for this company
    from . import culture as _culture
    d = _culture.dossier_for(job.company, _culture_cache())
    if d and d.get("fit") is not None:
        cf = round((d["fit"] - 50) / 50 * 6)
        if cf:
            pts += cf
            reasons.append(f"culture fit {d['fit']}/100 {'+' if cf > 0 else ''}{cf}")

    # Optional conference boost; off by default for this generic candidate.
    if p.get("conference_boost_enabled") and norm(job.company) in _shpe_companies():
        pts += 4
        reasons.append("SHPE 2026 exhibitor +4")

    job.score = max(0, min(100, round(pts)))
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
        rec["explicit_internship"] = explicit_new_grad(job.title)
        rec["explicit_new_grad"] = rec["explicit_internship"]
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
