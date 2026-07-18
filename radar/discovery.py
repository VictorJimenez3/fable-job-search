"""Self-expanding company discovery.

Every job URL that flows through the pipeline (from any aggregator) is mined
for ATS identifiers. New identifiers become registry candidates; a live probe
promotes them to active. The registry therefore grows continuously with zero
manual curation — every company any aggregator has ever linked to becomes a
company we poll directly (and faster than the aggregator) from then on.
"""
from __future__ import annotations

import re
import time

from .config import profile
from .models import norm
from .sources.ats import probe

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse_eu", re.compile(r"(?:job-boards|boards)\.eu\.greenhouse\.io/([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)/")),
    ("recruitee", re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com")),
    ("workday", re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:([a-z]{2}-[A-Z]{2})/)?([A-Za-z0-9_-]+)(?:/job/|/details/|$)")),
    ("icims", re.compile(r"https?://(?:careers-)?([a-z0-9-]+)\.icims\.com/jobs/")),
    ("oracle_orc", re.compile(r"https?://([a-z0-9.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z_]+/sites/([A-Za-z0-9_]+)/")),
    ("eightfold", re.compile(r"https?://((?:[a-z0-9-]+\.)?(?:jobs|careers|explore\.jobs)\.[a-z0-9-]+\.(?:com|net))/(?:careers|pcsx)[^\s\"]*domain=([a-z0-9.-]+)")),
]

BAD_TOKENS = {"embed", "job", "jobs", "wday", "en-us", "external", "www", "api"}


def extract(url: str) -> tuple[str, str, dict] | None:
    """URL → (ats, token, extra) or None."""
    if not url:
        return None
    for ats, pat in PATTERNS:
        m = pat.search(url)
        if not m:
            continue
        if ats == "workday":
            tenant, host, _lang, site = m.groups()
            if tenant.lower() in BAD_TOKENS or site.lower() in BAD_TOKENS:
                continue
            return "workday", tenant.lower(), {"host": host, "site": site}
        if ats == "icims":
            tenant = m.group(1)
            if tenant.lower() in BAD_TOKENS:
                continue
            # iCIMS career subdomains are commonly "careers-{tenant}"
            prefix = "careers-" if "careers-" in url else ""
            return "icims", f"{prefix}{tenant}", {}
        if ats == "oracle_orc":
            host, site = m.groups()
            return "oracle_orc", host.split(".")[0], {"host": host, "site": site}
        if ats == "eightfold":
            host, domain = m.groups()
            return "eightfold", domain.split(".")[0], {"host": f"https://{host}", "domain": domain}
        token = m.group(1)
        if token.lower() in BAD_TOKENS:
            continue
        if ats == "greenhouse_eu":
            return "greenhouse", token, {"eu": True}
        return ats, token, {}
    return None


def key(ats: str, token: str, extra: dict | None = None) -> str:
    if ats == "workday" and extra:
        return f"workday:{token}:{extra.get('site', '')}"
    if ats == "oracle_orc" and extra:
        return f"oracle_orc:{token}:{extra.get('site', '')}"
    return f"{ats}:{token}"


def seed_registry(registry: dict, seeds: list[dict]) -> int:
    added = 0
    for s in seeds:
        k = key(s["ats"], s["token"], s.get("extra"))
        if k not in registry:
            registry[k] = {
                "name": s["name"], "ats": s["ats"], "token": s["token"],
                "extra": s.get("extra", {}), "sector": s.get("sector", ""),
                "status": "new", "origin": "seed",
                "first_seen": int(time.time()), "failures": 0, "last_ok": 0,
            }
            added += 1
    return added


def harvest(registry: dict, jobs: list, max_new: int = 200) -> int:
    """Mine ATS identifiers from job URLs; add unseen ones as candidates."""
    if max_new <= 0:
        return 0
    added = 0
    for job in jobs:
        found = extract(job.url)
        if not found:
            continue
        ats, token, extra = found
        k = key(ats, token, extra)
        if k in registry:
            continue
        registry[k] = {
            "name": job.company or token, "ats": ats, "token": token,
            "extra": extra, "sector": "",
            "status": "new", "origin": f"harvest:{job.source}",
            "first_seen": int(time.time()), "failures": 0, "last_ok": 0,
        }
        added += 1
        if added >= max_new:
            break
    return added


def probe_new(registry: dict, budget: int = 30) -> tuple[int, int]:
    """Validate up to `budget` candidates. Returns (activated, invalidated).
    Leftover budget re-probes 'invalid' entries up to 3 total attempts —
    endpoints get fixed, WAFs relent, and code improves between runs."""
    ok = bad = 0
    candidates = [e for e in registry.values() if e["status"] == "new"]
    candidates.sort(key=lambda e: e["first_seen"])
    retries = [e for e in registry.values()
               if e["status"] == "invalid" and e.get("probe_attempts", 1) < 3]
    for e in (candidates + retries)[:budget]:
        e["probe_attempts"] = e.get("probe_attempts", 0) + 1
        if probe(e):
            e["status"] = "active"
            e["last_ok"] = int(time.time())
            ok += 1
        else:
            e["status"] = "invalid"
            e.setdefault("invalidated_at", int(time.time()))
            bad += 1
    return ok, bad


def hygiene(registry: dict, now: int | None = None) -> dict:
    """Monthly registry maintenance (ROADMAP "Registry hygiene job"):

    - dead boards (5+ consecutive poll failures) get one fresh probe cycle
      every 30 days — ATS migrations resolve and WAFs relent
    - non-seed entries invalid for 90+ days are dropped: scout/harvest
      guesses that never validated (seeds are curated and stay; entries
      predating the invalidated_at stamp start their clock now)
    - duplicate employers: when the same company is active under several
      registry entries (e.g. migrated Greenhouse → Workday), siblings that
      returned nothing for 30+ days while another produced are parked as
      status="dup" — reclaims polling budget, never touches producers
    """
    now = now or int(time.time())
    stats = {"dead_retried": 0, "invalid_pruned": 0, "dups_parked": 0}
    for e in registry.values():
        if e["status"] == "dead" and now - e.get("hygiene_retry_at", 0) >= 30 * 86400:
            e.update(status="new", failures=0, probe_attempts=0, hygiene_retry_at=now)
            stats["dead_retried"] += 1
    for k in [k for k, e in registry.items() if e["status"] == "invalid"]:
        e = registry[k]
        if e.get("origin") == "seed":
            continue
        if not e.get("invalidated_at"):
            e["invalidated_at"] = now
        elif now - e["invalidated_at"] >= 90 * 86400:
            del registry[k]
            stats["invalid_pruned"] += 1
    by_name: dict[str, list[dict]] = {}
    for e in registry.values():
        if e["status"] == "active":
            by_name.setdefault(norm(e["name"]), []).append(e)
    for entries in by_name.values():
        fresh = [e for e in entries if now - e.get("last_ok", 0) <= 30 * 86400]
        if len(entries) < 2 or not fresh or len(fresh) == len(entries):
            continue
        for e in entries:
            if e not in fresh:
                e["status"] = "dup"
                stats["dups_parked"] += 1
    return stats


def record_result(entry: dict, success: bool, deactivate_after: int = 5) -> None:
    if success:
        entry["failures"] = 0
        entry["last_ok"] = int(time.time())
    else:
        entry["failures"] = entry.get("failures", 0) + 1
        if entry["failures"] >= deactivate_after:
            entry["status"] = "dead"


def dedupe_name(registry: dict, company: str) -> dict | None:
    n = norm(company)
    for e in registry.values():
        if norm(e["name"]) == n:
            return e
    return None


SCOUT_PROMPT = (
    "You are researching US employers for an undergraduate chemical engineering "
    "student seeking internships and co-ops. List up to 15 employers that job "
    "aggregators may miss, especially chemicals/materials, pharma/biotech, energy, "
    "semiconductors, consumer manufacturing, environmental engineering, and "
    "engineering consulting. For each, give your best guess of its careers ATS.\n"
    'Reply with ONLY JSON: {"companies": [{"name": "Example Chemicals", '
    '"ats": "greenhouse|lever|ashby|smartrecruiters|recruitee", '
    '"token": "board token guess", "sector": "chemicals_materials|pharma_biotech|energy|semiconductors|consumer_manufacturing|environmental|engineering_consulting"}]}')


def llm_scout(registry: dict, limit: int = 10) -> int:
    """Weekly research pass: ask the local LLM for employers the aggregators
    may never surface (plant and lab employers often use fragmented career sites),
    guess their ATS tokens, and queue them for the normal live probe. A wrong
    guess is harmless: the probe marks it invalid and never fetches it again.
    """
    import json as _json

    from . import llm
    if not llm.available():
        return 0
    text = llm.complete(SCOUT_PROMPT, max_tokens=900, json_mode=True)
    if not text:
        return 0
    try:
        rows = _json.loads(text[text.index("{"):text.rindex("}") + 1]).get("companies", [])
    except (ValueError, _json.JSONDecodeError, AttributeError):
        return 0
    added = 0
    allowed_sectors = set(profile().get("registry_sectors") or [])
    for row in rows:
        ats = str(row.get("ats", "")).lower().strip()
        token = re.sub(r"[^a-z0-9_-]", "", str(row.get("token", "")).lower())
        name = str(row.get("name", "")).strip()
        sector = str(row.get("sector", "")).strip()
        if ats not in {"greenhouse", "lever", "ashby", "smartrecruiters", "recruitee"} \
                or not token or not name or token in BAD_TOKENS \
                or (allowed_sectors and sector not in allowed_sectors):
            continue
        k = key(ats, token, None)
        if k in registry or dedupe_name(registry, name):
            continue
        registry[k] = {"name": name, "ats": ats, "token": token, "extra": {},
                       "sector": sector, "status": "new",
                       "origin": "scout", "first_seen": int(time.time()),
                       "failures": 0, "last_ok": 0}
        added += 1
        if added >= limit:
            break
    return added
