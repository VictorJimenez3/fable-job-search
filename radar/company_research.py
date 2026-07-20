"""Evidence-first company research.

Large models do not browse merely because they know many facts.  This module
therefore synthesizes only short evidence excerpts captured from official job
postings the radar already fetches.  Every displayed claim carries source IDs;
missing evidence remains explicitly unknown.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import time
from urllib.parse import parse_qs, quote_plus, urlparse

from . import llm, state
from .config import env, profile
from .http import get
from .models import norm

SCHEMA_V = 1
PROMPT_V = 2
REFRESH_SECONDS = 60 * 86400
FIELDS = ("summary", "products", "customers", "mission", "business_model",
          "size_stage", "technical_work", "locations", "sponsorship_context",
          "why_it_matters", "interview_focus", "industry", "ai_ds_prestige_tier",
          "pace_of_work", "wlb_rating", "culture_vibe", "pto_days", "shutdowns",
          "estimated_new_grad_tech_pay", "rotational_program_name")
PROFILE_FIELDS = ("industry", "ai_ds_prestige_tier", "pace_of_work", "wlb_rating",
                  "culture_vibe", "pto_days", "shutdowns",
                  "estimated_new_grad_tech_pay", "rotational_program_name")
_UNKNOWN = {"", "unknown", "not confirmed", "not stated", "not available"}
_MARKERS = (
    "we are ", " is a ", "our mission", "our purpose", "we build", "we make",
    "we provide", "we help", "our platform", "our product", "our customers",
    "serves ", "leading ", "technology company", "healthcare company",
    "sponsorship", "sponsor", "opt", "cpt", "visa", "paid time off", "vacation",
    "pto", "work-life", "work life", "fast-paced", "culture", "environment",
)
_BOILERPLATE = (
    "equal opportunity", "affirmative action", "reasonable accommodation",
    "salary range", "compensation range", "privacy policy",
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
        if len(chosen) >= 4:
            break
    return " ".join(chosen)


def _source_id(url: str, excerpt: str) -> str:
    return hashlib.sha1(f"{url}|{excerpt}".encode()).hexdigest()[:10]


def _clean_page(raw: str) -> str:
    text = html_lib.unescape(raw or "")
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _search_links(company: str) -> list[str]:
    """Use a public search page only to discover candidate company URLs."""
    try:
        html = get("https://html.duckduckgo.com/html/",
                   params={"q": f'"{company}" company careers benefits mission'},
                   headers={"User-Agent": "JobRadar/1.0 (personal research)"}).text
    except Exception as exc:
        print(f"company research: web search failed for {company}: {exc}")
        return []
    links = []
    for href in re.findall(r'class="result__a"[^>]+href="([^"]+)"', html):
        parsed = urlparse(href)
        if "duckduckgo.com" in parsed.netloc:
            href = parse_qs(parsed.query).get("uddg", [""])[0]
        if href.startswith("http") and href not in links:
            links.append(href)
    return links[:10]


def _source_record(company: str, url: str, title: str, text: str,
                   kind: str, now: int) -> dict | None:
    excerpt = extract_excerpt(company, text, limit=900)
    if not excerpt:
        excerpt = re.sub(r"\s+", " ", text or "").strip()[:900]
    if not excerpt or not url:
        return None
    return {
        "id": _source_id(url, excerpt), "url": url, "title": title[:180],
        "kind": kind, "publisher": company, "retrieved_at": now,
        "excerpt": excerpt, "content_sha": hashlib.sha256(excerpt.encode()).hexdigest()[:16],
    }


def capture_external_into(records: dict, *, company: str, url: str, title: str,
                          text: str, kind: str = "company_web",
                          retrieved_at: int | None = None) -> bool:
    """Capture a bounded non-posting company source with an explicit kind."""
    now = retrieved_at or int(time.time())
    source = _source_record(company, url, title, text, kind, now)
    if not source:
        return False
    record = records.setdefault(norm(company), {
        "schema_v": SCHEMA_V, "name": company, "aliases": [],
        "status": "evidence_only", "sources": [],
    })
    old = list(record.get("sources") or [])
    if any(s.get("url") == url and s.get("content_sha") == source["content_sha"] for s in old):
        return False
    sources = [s for s in old if s.get("url") != url]
    sources.insert(0, source)
    record["sources"] = sources[:6]
    record["evidence_sha"] = evidence_sha(record["sources"])
    record["last_evidence_at"] = now
    record.pop("synthesized_evidence_sha", None)
    record.pop("refresh_after", None)
    return True


def _profile_defaults(record: dict, sector: str = "") -> None:
    """Keep the Company tab useful even while a model/provider is unavailable."""
    label = sector.replace("_", " ") or "technology"
    defaults = {
        "industry": f"{label.title()} / technology (estimated)",
        "ai_ds_prestige_tier": "Tier 3 — not yet ranked from company-specific data (estimated)",
        "pace_of_work": "Moderate-to-fast (estimated)",
        "wlb_rating": "3/5 (estimated)",
        "culture_vibe": "Technology team; company-specific culture research pending (estimated)",
        "pto_days": "15–20 days (estimated)",
        "shutdowns": "None found in research (estimated)",
        "estimated_new_grad_tech_pay": "$85k–$125k base (estimated; US market)",
        "rotational_program_name": "None found in research",
    }
    for field, value in defaults.items():
        claim = record.get(field) or {}
        if not claim or str(claim.get("value", "")).lower() in _UNKNOWN:
            record[field] = _claim(value, confidence="estimated")


def prepare_external_sources(records: dict, company: str, job_urls: list[str],
                             source_urls: list[str] | None = None,
                             sector: str = "") -> bool:
    """Research company/about/careers pages before asking the model to synthesize."""
    record = records.setdefault(norm(company), {
        "schema_v": SCHEMA_V, "name": company, "aliases": [],
        "status": "evidence_only", "sources": [],
    })
    now = int(time.time())
    for source_url in source_urls or []:
        if source_url:
            capture_external_into(
                records, company=company, url=source_url,
                title=f"Discovery source for {company}",
                text=f"This company posting was surfaced through the monitored source: {source_url}.",
                kind="discovery_feed", retrieved_at=now)
    if record.get("web_researched_at", 0) > now - 30 * 86400:
        return False
    links: list[str] = []
    for url in (source_urls or []) + job_urls:
        host = urlparse(url).netloc.lower()
        if url.startswith("http") and host and not any(x in host for x in (
                "greenhouse.io", "ashbyhq.com", "lever.co", "myworkdayjobs.com",
                "oraclecloud.com", "github.com", "jobright", "speedyapply")):
            links.append(f"{urlparse(url).scheme}://{host}")
    links += _search_links(company)
    seen = set()
    changed = False
    for url in links:
        parsed = urlparse(url)
        if parsed.netloc in {"html.duckduckgo.com", "www.google.com"} or url in seen:
            continue
        seen.add(url)
        try:
            response = get(url, headers={"User-Agent": "JobRadar/1.0 (personal research)"})
            if response.status_code >= 400:
                continue
            title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", response.text)
            title = _clean_page(title_match.group(1)) if title_match else f"{company} public company page"
            kind = "company_web"
            lower = f"{url} {title}".lower()
            if any(x in lower for x in ("career", "jobs", "benefit", "culture", "working")):
                kind = "company_careers_or_benefits"
            changed |= capture_external_into(records, company=company, url=url,
                                             title=title, text=_clean_page(response.text),
                                             kind=kind, retrieved_at=now)
        except Exception as exc:
            print(f"company research: page fetch failed for {company} {url}: {exc}")
        if len(seen) >= 5:
            break
    record["web_researched_at"] = now
    record["web_research_status"] = "sources captured" if changed else "no usable public page"
    record["web_search_url"] = f"https://duckduckgo.com/?q={quote_plus(company + ' company careers benefits')}"
    _profile_defaults(record, sector)
    return changed


def prepare_for_jobs(jobs: list[dict], limit: int = 8) -> int:
    """Capture discovery/official web sources for the highest-value companies."""
    if limit <= 0 or not jobs:
        return 0
    records = load()
    grouped: dict[str, list[dict]] = {}
    for job in jobs:
        if job.get("company"):
            grouped.setdefault(norm(job["company"]), []).append(job)
    ordered = sorted(grouped.values(), key=lambda rows: -max((r.get("score", 0) for r in rows), default=0))
    made = 0
    for rows in ordered[:limit]:
        first = rows[0]
        prepare_external_sources(records, first["company"],
                                 [r.get("url", "") for r in rows],
                                 [r.get("source_url", "") for r in rows],
                                 first.get("sector", ""))
        made += 1
    save(records)
    return made


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
    # Keep both job evidence and the external company research together. The
    # source list is intentionally bounded because it is committed state.
    record["sources"] = sources[:6]
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


def _claim(value: str = "unknown", confidence: str = "unknown") -> dict:
    return {"value": value, "source_ids": [], "confidence": confidence}


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
        if confidence not in {"high", "medium", "low", "estimated"}:
            confidence = "low"
        if not isinstance(ids, list):
            ids = []
        ids = list(dict.fromkeys(str(i) for i in ids if str(i) in source_ids))[:3]
        if value.lower() in _UNKNOWN:
            result[field] = _claim("Not confirmed")
        elif not ids and confidence != "estimated":
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

Use the supplied sources first. The first two output items are a plain-English
2-3 sentence explanation of what the company does and how it makes money.
For the profile fields, fill every field. Exact PTO, WLB, pace, and pay are
often not public: give a conservative company-specific estimate and prefix it
with "Estimated:"; never invent a precise policy. For rotational programs,
write "None found in research" if no program source appears. Keep every value
useful to someone unfamiliar with the company. Estimates may have confidence
"estimated" and do not require a source ID; sourced facts must cite IDs.

{blocks}

Return ONLY JSON with exactly these keys: {', '.join(FIELDS)}.
Each value must be an object:
{{"value":"concise text","source_ids":["source id"],"confidence":"high|medium|low|estimated"}}
Every sourced factual value must cite one or more supplied source IDs."""


def _priority(record: dict, jobs: list[dict], priority_ids: set[str], now: int) -> int:
    score = 0
    if any(j.get("id") in priority_ids for j in jobs):
        score += 120
    if any(j.get("alert_ok") and not j.get("closed_at") for j in jobs):
        score += 100
    score += min(70, max((j.get("score", 0) for j in jobs), default=0))
    # Keep the research queue aligned with what the user actually sees first:
    # best fit, then newest posting.  first_seen is only a fallback because an
    # aggregator can discover a posting after it was published.
    newest = max((j.get("posted_at") or j.get("first_seen", 0) for j in jobs), default=0)
    if newest and now - newest <= 24 * 3600:
        score += 55
    elif newest and now - newest <= 72 * 3600:
        score += 35
    elif any(now - j.get("first_seen", 0) <= 72 * 3600 for j in jobs):
        score += 20
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
    # New postings get a company web-research attempt before the AI queue is
    # assembled. One dossier is shared by all roles at that employer.
    web_limit = int(env("RADAR_COMPANY_WEB_RESEARCH_LIMIT", "12"))
    web_done = 0
    for key, jobs in sorted(grouped.items(), key=lambda item: -max((j.get("score", 0) for j in item[1]), default=0)):
        if web_done >= web_limit:
            break
        record = records.setdefault(key, {"schema_v": SCHEMA_V, "name": jobs[0].get("company", key),
                                          "aliases": [], "status": "evidence_only", "sources": []})
        if not record.get("web_researched_at"):
            prepare_external_sources(records, jobs[0].get("company", key),
                                     [j.get("url", "") for j in jobs],
                                     [j.get("source_url", "") for j in jobs],
                                     jobs[0].get("sector", ""))
            web_done += 1
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
        # The dossier has a fixed 20-field schema. 900 tokens truncates valid
        # local-model JSON before the final profile fields; leave enough room
        # while the global AI budget still bounds cost.
        raw = llm.complete(prompt, max_tokens=int(env("RADAR_COMPANY_RESEARCH_MAX_TOKENS", "2200")), timeout=180, json_mode=True,
                           task="company_research",
                           validator=lambda text, ids=ids: parse_synthesis(text, ids) is not None)
        record["last_attempt_at"] = now
        record["attempts"] = int(record.get("attempts", 0)) + 1
        claims = parse_synthesis(raw, ids)
        if claims is None:
            record["error"] = "provider unavailable or response was not source-grounded"
            continue
        record.update(claims)
        # A model may omit a non-public policy even when the rest of its JSON
        # is valid. Keep every Company-tab column populated with an explicitly
        # estimated value rather than silently rendering an empty cell.
        _profile_defaults(record)
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
