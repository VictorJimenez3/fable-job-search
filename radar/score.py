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
    alert_eligible = aggregator or explicit
    reasons = [] if alert_eligible else ["seniority unclear (dashboard only)"]
    return True, alert_eligible, reasons


# ---------- scoring ----------

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

    job.score = max(0, min(100, round(pts)))
    job.score_reasons = reasons


def update_feedback_from_applied(fb: dict, company: str, title: str) -> dict:
    comp = norm(company)
    fb.setdefault("company_boosts", {})
    fb.setdefault("token_boosts", {})
    fb["company_boosts"][comp] = min(fb["company_boosts"].get(comp, 0) + 2, 8)
    for tok in _title_tokens(title):
        fb["token_boosts"][tok] = min(fb["token_boosts"].get(tok, 0) + 1, 4)
    return fb
