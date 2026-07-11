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

SENIOR_RE = re.compile(
    r"\b(senior|staff|principal|lead|director|manager|head of|sr\.?|vp|chief|"
    r"architect|distinguished|fellow|executive|iii|iv)\b", re.I)
INTERN_RE = re.compile(r"\b(intern(ship)?|co-?op|apprentice|fellowship|part[- ]?time|contract(or)?)\b", re.I)
PHD_RE = re.compile(r"\bph\.?d\b|postdoc", re.I)
CLEARANCE_RE = re.compile(r"\b(security clearance|ts/sci|polygraph|top secret)\b", re.I)
YEARS_RE = re.compile(r"(?:minimum|at least|requires?)\s+(\d+)\+?\s+years", re.I)

NEW_GRAD_RE = re.compile(
    r"\b(new ?grad|university grad|recent(ly)? grad|early[- ]career|entry[- ]level|"
    r"campus|college grad|20(25|26|27) grad|class of 20(25|26|27)|junior|associate|"
    r"engineer i\b|graduate (software|engineer|program|scheme))\b", re.I)
ENTRY_YEARS_RE = re.compile(r"\b0\s*[-–to ]+\s*[123]\s+years\b", re.I)

ROLE_BUCKETS: dict[str, re.Pattern] = {
    "ai_ml": re.compile(
        r"machine learning|ml engineer|\bml\b|\bai\b|artificial intelligence|applied scientist|"
        r"research engineer|deep learning|\bllm\b|gen ?ai|generative|nlp|computer vision|perception", re.I),
    "data_science": re.compile(r"data scien|analytics engineer|\banalyst\b|statistic|quantitative", re.I),
    "data_eng": re.compile(r"data engineer|data platform|data infrastructure|etl\b", re.I),
    "swe": re.compile(
        r"software|swe\b|backend|back[- ]end|full[- ]?stack|front[- ]?end|platform engineer|"
        r"infrastructure|site reliability|devops|mobile|ios|android|\bdeveloper\b|systems engineer|"
        r"security engineer|cloud engineer|embedded", re.I),
}

FOREIGN_HINTS = re.compile(
    r"\b(canada|toronto|vancouver|london|uk\b|united kingdom|ireland|dublin|germany|berlin|munich|"
    r"france|paris|netherlands|amsterdam|india|bangalore|bengaluru|hyderabad|pune|chennai|gurgaon|"
    r"noida|mumbai|singapore|japan|tokyo|china|beijing|shanghai|shenzhen|australia|sydney|melbourne|"
    r"brazil|sao paulo|mexico city|poland|warsaw|krakow|israel|tel aviv|spain|madrid|barcelona|"
    r"portugal|lisbon|switzerland|zurich|sweden|stockholm|estonia|romania|dubai|uae|philippines|"
    r"manila|vietnam|korea|seoul|taiwan|taipei|nigeria|kenya|south africa|argentina|colombia|chile)\b", re.I)
US_HINTS = re.compile(
    r"\b(us|usa|u\.s\.|united states|remote)\b|"
    r"\b(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|"
    r"nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc)\b|"
    r"new york|san francisco|seattle|austin|boston|chicago|denver|atlanta|los angeles|san jose|"
    r"palo alto|mountain view|sunnyvale|redmond|bellevue|cambridge|philadelphia|miami|dallas|"
    r"houston|phoenix|portland|salt lake|pittsburgh|raleigh|durham|nashville|minneapolis|detroit|"
    r"washington|arlington|reston|mclean|santa clara|menlo park|cupertino|irvine|san diego|boulder", re.I)


def role_bucket(title: str, description: str = "") -> str | None:
    for bucket in ("ai_ml", "data_science", "data_eng", "swe"):
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


def gates(job: Job) -> tuple[bool, bool, list[str]]:
    """Returns (keep_at_all, alert_eligible, reasons)."""
    t = job.title
    text = f"{t}\n{job.description[:1500]}"
    if INTERN_RE.search(t):
        return False, False, ["intern/co-op/contract"]
    if SENIOR_RE.search(t):
        return False, False, ["senior+ title"]
    if PHD_RE.search(t):
        return False, False, ["PhD-targeted title"]
    if CLEARANCE_RE.search(text):
        return False, False, ["requires clearance"]
    if not location_ok(job):
        return False, False, ["non-US location"]
    m = YEARS_RE.search(job.description)
    if m and int(m.group(1)) >= 3:
        return False, False, [f"requires {m.group(1)}+ years"]
    if role_bucket(t, job.description) is None:
        return False, False, ["not an AI/SWE/DS role"]

    aggregator = job.source in {"simplify", "vansh", "jobright", "speedyapply"}
    explicit = bool(NEW_GRAD_RE.search(text) or ENTRY_YEARS_RE.search(text))
    reasons = []
    alert_eligible = aggregator or explicit
    # The Shams rule: blockbusters get announced regardless of new-grad
    # wording (hard gates above still apply). Likewise a pays-bank salary.
    if not alert_eligible and is_marquee(job.company):
        alert_eligible = True
        reasons.append("marquee company (auto-alert)")
    if not alert_eligible and pays_bank(job.salary):
        alert_eligible = True
        reasons.append("pays bank (auto-alert)")
    if not alert_eligible:
        reasons.append("seniority unclear (dashboard only)")
    return True, alert_eligible, reasons


_MARQUEE_CACHE: set | None = None


def is_marquee(company: str) -> bool:
    """Blockbuster employer per profile.yaml marquee_companies."""
    global _MARQUEE_CACHE
    if _MARQUEE_CACHE is None:
        _MARQUEE_CACHE = {norm(c) for c in profile().get("marquee_companies", [])}
    return norm(company) in _MARQUEE_CACHE


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


def _title_tokens(title: str) -> set[str]:
    stop = {"engineer", "software", "the", "and", "of", "for", "a", "an", "i", "ii", "new", "grad"}
    return {w for w in norm(title).split() if len(w) > 2 and w not in stop}


def score(job: Job, feedback: dict, now: int | None = None) -> None:
    """Mutates job.score / job.score_reasons. Assumes gates already passed."""
    p = profile()
    now = now or int(time.time())
    pts = 40
    reasons = ["base 40"]

    bucket = role_bucket(job.title, job.description) or "swe"
    role_pts = p["roles"].get(bucket, 10)
    pts += role_pts
    reasons.append(f"role:{bucket} +{role_pts}")

    sector_pts = p["sectors"].get(job.sector or "other", 0)
    if sector_pts:
        pts += sector_pts
        reasons.append(f"sector:{job.sector} +{sector_pts}")

    b = p["bonuses"]
    if NEW_GRAD_RE.search(job.title):
        pts += b["explicit_new_grad_title"]
        reasons.append(f"new-grad title +{b['explicit_new_grad_title']}")

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

    # SHPE 2026 exhibitor: the posting doubles as a warm booth intro (Oct 28-31)
    if norm(job.company) in _shpe_companies():
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
