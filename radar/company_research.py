"""Evidence-first company research.

Large models do not browse merely because they know many facts.  This module
therefore synthesizes only short evidence excerpts captured from official job
postings the radar already fetches.  Every displayed claim carries source IDs;
missing evidence remains explicitly unknown.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from . import llm, state
from .config import env, profile
from .models import norm

SCHEMA_V = 1
PROMPT_V = 1
REFRESH_SECONDS = 60 * 86400
FIELDS = ("summary", "products", "customers", "mission", "business_model",
          "size_stage", "technical_work", "locations", "sponsorship_context",
          "why_it_matters", "interview_focus")
_UNKNOWN = {"", "unknown", "not confirmed", "not stated", "not available"}
_MARKERS = (
    "we are ", " is a ", "our mission", "our purpose", "we build", "we make",
    "we provide", "we help", "our platform", "our product", "our customers",
    "serves ", "leading ", "technology company", "healthcare company",
    "sponsorship", "sponsor", "opt", "cpt", "visa",
)
_BOILERPLATE = (
    "equal opportunity", "affirmative action", "reasonable accommodation",
    "benefits include", "salary range", "compensation range", "privacy policy",
    "background check", "all qualified applicants",
)


def load() -> dict:
    return state.load("company_research.json", {})


def save(records: dict) -> None:
    state.save("company_research.json", records)


def _sentences(text: str) -> list[str]:
    clean = re.sub(r"\s+", " ", text or " ").strip()
    return [s.strip(" -•\t") for s in re.split(r"(?<=[.!?])\s+|\s*[•|]\s*", clean)]


def extract_excerpt(company: str, text: str, limit: int = 600) -> str:
    """Select small company/mission/product/visa evidence, not the whole JD."""
    company_words = [w for w in norm(company).split() if len(w) >= 4]
    ranked: list[tuple[int, int, str]] = []
    for i, sentence in enumerate(_sentences(text)):
        low = sentence.lower()
        if not 35 <= len(sentence) <= 420 or any(x in low for x in _BOILERPLATE):
            continue
        marker_hits = sum(m in low for m in _MARKERS)
        company_hit = any(w in norm(sentence).split() for w in company_words)
        if not marker_hits and not company_hit:
            continue
        # Earlier About-company paragraphs tend to outrank requirements.
        ranked.append((marker_hits * 3 + int(company_hit), -i, sentence))
    ranked.sort(reverse=True)
    chosen, used = [], 0
    for _, _, sentence in ranked:
        if sentence in chosen:
            continue
        extra = len(sentence) + (1 if chosen else 0)
        if used + extra > limit:
            continue
        chosen.append(sentence)
        used += extra
        if len(chosen) >= 5:
            break
    return " ".join(chosen)


def _source_id(url: str, excerpt: str) -> str:
    return hashlib.sha1(f"{url}|{excerpt}".encode()).hexdigest()[:10]


def capture_into(records: dict, *, company: str, title: str, url: str,
                 text: str, retrieved_at: int | None = None) -> bool:
    """Add/update one bounded official-posting source in an in-memory store."""
    excerpt = extract_excerpt(company, text)
    if not excerpt or not url:
        return False
    key = norm(company)
    now = retrieved_at or int(time.time())
    record = records.setdefault(key, {
        "schema_v": SCHEMA_V, "name": company, "aliases": [],
        "status": "evidence_only", "sources": [],
    })
    sid = _source_id(url, excerpt)
    source = {
        "id": sid,
        "url": url,
        "title": f"Official job posting — {title}"[:180],
        "kind": "official_job",
        "publisher": company,
        "retrieved_at": now,
        "excerpt": excerpt,
        "content_sha": hashlib.sha256(excerpt.encode()).hexdigest()[:16],
    }
    old_sources = list(record.get("sources") or [])
    if any(s.get("url") == url and s.get("content_sha") == source["content_sha"]
           for s in old_sources):
        return False
    sources = [s for s in old_sources if s.get("url") != url]
    sources.insert(0, source)
    # Three short sources are enough to ground a useful overview while
    # keeping this public repository small.
    record["sources"] = sources[:3]
    record["evidence_sha"] = evidence_sha(record["sources"])
    record["last_evidence_at"] = now
    if not record.get("summary"):
        record["status"] = "evidence_only"
    return record["sources"] != old_sources


def evidence_sha(sources: list[dict]) -> str:
    payload = "|".join(sorted(f"{s.get('id')}:{s.get('content_sha')}" for s in sources))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def dossier_for(company: str, records: dict | None = None) -> dict | None:
    """Exact identity or explicit alias only; never shared-first-word matching."""
    records = records if records is not None else load()
    key = norm(company)
    if key in records:
        return records[key]
    for record in records.values():
        if key in {norm(a) for a in record.get("aliases") or []}:
            return record
    return None


def job_is_relevant(job: dict) -> bool:
    """Re-run the active profile's title/location gates for stored jobs.

    Branches inherit historical state, and a saved ID from one profile must
    never make another profile research an unrelated posting.
    """
    if job.get("closed_at"):
        return False
    from .models import Job
    from .score import gates
    candidate = Job(
        company=job.get("company", ""), title=job.get("title", ""),
        url=job.get("url", ""), source=job.get("source", ""),
        locations=job.get("locations", []), salary=job.get("salary", ""),
        remote=bool(job.get("remote")), sector=job.get("sector", ""),
    )
    keep, _, _ = gates(candidate)
    return keep


def prune_irrelevant_sources(records: dict, jobs_state: dict) -> bool:
    """Remove official-job evidence rejected by the active branch profile."""
    relevant_by_url: dict[str, bool] = {}
    for job in jobs_state.values():
        url = job.get("url", "")
        if url:
            relevant_by_url[url] = relevant_by_url.get(url, False) or job_is_relevant(job)
    changed = False
    for key in list(records):
        record = records[key]
        old_sources = list(record.get("sources") or [])
        sources = [s for s in old_sources
                   if s.get("url") not in relevant_by_url
                   or relevant_by_url[s.get("url")]]
        if sources == old_sources:
            continue
        changed = True
        if not sources:
            del records[key]
            continue
        record["sources"] = sources
        record["evidence_sha"] = evidence_sha(sources)
        record["status"] = "evidence_only"
        record.pop("synthesized_evidence_sha", None)
        record.pop("refresh_after", None)
        for field in FIELDS:
            record.pop(field, None)
    return changed


def _claim(value: str = "unknown") -> dict:
    return {"value": value, "source_ids": [], "confidence": "unknown"}


def parse_synthesis(raw: str | None, source_ids: set[str]) -> dict | None:
    """Validate synthesis and downgrade uncited claims to explicit unknowns."""
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        body = json.loads(match.group(0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    result = {}
    valid_claims = 0
    for field in FIELDS:
        item = body.get(field)
        if not isinstance(item, dict):
            result[field] = _claim()
            continue
        value = str(item.get("value") or "unknown").strip()[:500]
        ids = item.get("source_ids") or []
        confidence = str(item.get("confidence") or "low").lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        if not isinstance(ids, list):
            ids = []
        ids = list(dict.fromkeys(str(i) for i in ids if str(i) in source_ids))[:3]
        if value.lower() in _UNKNOWN:
            result[field] = _claim("Not confirmed")
        elif not ids:
            result[field] = _claim("Not confirmed")
        else:
            result[field] = {"value": value, "source_ids": ids, "confidence": confidence}
            valid_claims += 1
    # A response that cannot ground even a summary plus one detail should not
    # replace a previous good dossier.
    return result if valid_claims >= 2 and result["summary"]["value"] != "Not confirmed" else None


def _prompt(record: dict) -> str:
    criteria = profile().get("culture_criteria", "")
    blocks = "\n\n".join(
        f"SOURCE {s['id']} ({s['title']}, retrieved {time.strftime('%Y-%m-%d', time.gmtime(s['retrieved_at']))})\n"
        f"URL: {s['url']}\nEXCERPT: {s['excerpt']}"
        for s in record.get("sources") or [])
    return f"""Build a factual employer brief for a job candidate.
Company identity: {record['name']}
Candidate priorities (use only for why_it_matters/interview_focus): {criteria}

Use ONLY the numbered official sources below. Do not use model memory. If a
fact is absent, set value to "Not confirmed" with no source IDs. Job-posting
marketing copy can support what the company says it does, but cannot prove WLB,
company size, compensation, or sponsorship unless explicitly stated. Keep each
value concise and useful to someone unfamiliar with the company.

{blocks}

Return ONLY JSON with exactly these keys: {', '.join(FIELDS)}.
Each value must be an object:
{{"value":"text or Not confirmed","source_ids":["source id"],"confidence":"high|medium|low"}}
Every factual non-unknown value must cite one or more supplied source IDs."""


def _priority(record: dict, jobs: list[dict], priority_ids: set[str], now: int) -> int:
    score = 0
    if any(j.get("id") in priority_ids for j in jobs):
        score += 120
    if any(j.get("alert_ok") and not j.get("closed_at") for j in jobs):
        score += 100
    score += min(70, max((j.get("score", 0) for j in jobs), default=0))
    if any(now - j.get("first_seen", 0) <= 72 * 3600 for j in jobs):
        score += 40
    if record.get("status") != "ready":
        score += 35
    if record.get("refresh_after", 0) <= now:
        score += 25
    if record.get("synthesized_evidence_sha") != record.get("evidence_sha"):
        score += 25
    return score


def enrich(jobs_state: dict, applied: list | None = None, web: dict | None = None,
           limit: int | None = None) -> int:
    """Synthesize a small priority batch; unchanged fresh evidence costs zero."""
    if env("RADAR_COMPANY_RESEARCH_DISABLE") or not llm.available("company_research"):
        return 0
    limit = int(env("RADAR_COMPANY_RESEARCH_LIMIT", "3")) if limit is None else limit
    if limit <= 0:
        return 0
    records = load()
    priority_ids = {a.get("id") for a in (applied or []) if a.get("id")}
    priority_ids |= set((web or {}).get("jobs") or {})
    now = int(time.time())
    grouped: dict[str, list[dict]] = {}
    prune_irrelevant_sources(records, jobs_state)
    for job in jobs_state.values():
        if (not job.get("alert_ok") and not job_is_relevant(job)
                or now - job.get("first_seen", 0) > 45 * 86400):
            continue
        grouped.setdefault(norm(job.get("company", "")), []).append(job)
    queue = []
    for key, jobs in grouped.items():
        record = records.get(key)
        if not record or not record.get("sources"):
            continue
        changed = record.get("synthesized_evidence_sha") != record.get("evidence_sha")
        stale = record.get("refresh_after", 0) <= now
        if not changed and not stale and record.get("schema_v") == SCHEMA_V:
            continue
        queue.append((_priority(record, jobs, priority_ids, now), key, record))
    queue.sort(reverse=True, key=lambda row: row[0])
    made = 0
    for _, key, record in queue[:limit]:
        ids = {s.get("id") for s in record.get("sources") or [] if s.get("id")}
        prompt = _prompt(record)
        raw = llm.complete(prompt, max_tokens=900, timeout=150, json_mode=True,
                           task="company_research",
                           validator=lambda text, ids=ids: parse_synthesis(text, ids) is not None)
        record["last_attempt_at"] = now
        record["attempts"] = int(record.get("attempts", 0)) + 1
        claims = parse_synthesis(raw, ids)
        if claims is None:
            record["error"] = "provider unavailable or response was not source-grounded"
            continue
        record.update(claims)
        record.update({
            "schema_v": SCHEMA_V,
            "prompt_v": PROMPT_V,
            "status": "ready",
            "generated_at": now,
            "refresh_after": now + REFRESH_SECONDS,
            "synthesized_evidence_sha": record.get("evidence_sha"),
            "error": "",
            "generator": {"task": "company_research"},
        })
        event = (llm.usage_report().get("events") or [{}])[-1]
        record["generator"].update({"endpoint": event.get("endpoint", ""),
                                    "model": event.get("model", "")})
        records[key] = record
        made += 1
    save(records)
    return made


def claim_text(record: dict | None, field: str) -> str:
    claim = (record or {}).get(field) or {}
    return claim.get("value") or "Not confirmed"
