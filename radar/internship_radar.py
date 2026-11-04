"""Isolated internship crawler.

The normal radar remains the high-frequency new-grad lane. This module uses
the same normalized Job model and ATS registry primitives, but a different
source set, state namespace, scorer, cadence, and delivery labels.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlsplit

from . import applied as applied_mod
from . import discovery, lifecycle, state
from .config import env, profile, seeds
from .digest import write_outputs
from .identity import canonical_url, posting_key
from .internship import RULES_VERSION, analyze, annotate, gates, score, term_status
from .models import Job, norm
from .score import ranking_key


def _delivery_sort_key(row: dict) -> tuple:
    """Final-score-first ordering for the bounded internship alert cap."""
    return ranking_key(row)


def _job_from_record(record: dict) -> Job:
    """Rehydrate only the fields the deterministic internship scorer needs."""
    return Job(
        company=record.get("company", ""), title=record.get("title", ""),
        url=record.get("url", ""), source=record.get("source", ""),
        source_url=record.get("source_url", ""), locations=record.get("locations", []),
        source_board=record.get("source_board", ""),
        source_board_variants=record.get("source_board_variants", []) or [],
        source_variants=record.get("source_variants", []) or [],
        source_url_variants=record.get("source_url_variants", []) or [],
        alternate_urls=record.get("alternate_urls", []) or [],
        link_resolution=record.get("link_resolution", {}) or {},
        location_eligibility=record.get("location_eligibility", {}) or {},
        posted_at=record.get("posted_at"), description=record.get("description", ""),
        salary=record.get("salary", ""), remote=bool(record.get("remote")),
        ats=record.get("ats", ""), sector=record.get("sector", ""),
        profile="internship",
        internship_eligibility=record.get("internship_eligibility", {}) or {},
        posting=record.get("posting", {}) or {},
    )


def _source_only(job: Job) -> bool:
    return (job.link_resolution or {}).get("status") == "source_only"


def _internship_source_rank(job: Job) -> tuple:
    """Prefer an employer/ATS sighting when a curated lead shares its URL."""
    return (
        0 if _source_only(job) else 1,
        1 if job.description else 0,
        1 if job.ats else 0,
        1 if (job.link_resolution or {}).get("status") == "resolved" else 0,
        len(job.description or ""),
    )


def _dedupe_internship_sightings(jobs: list[Job], merge_sighting) -> tuple[list[Job], int]:
    """Collapse URL variants while keeping official evidence authoritative."""
    selected: dict[str, Job] = {}
    dropped = 0
    for job in jobs:
        key = posting_key(job.url) or canonical_url(job.url)
        if not key:
            continue
        previous = selected.get(key)
        if previous is None:
            selected[key] = job
            continue
        if _internship_source_rank(job) > _internship_source_rank(previous):
            winner, loser = job, previous
        else:
            winner, loser = previous, job
        winner_resolution = dict(winner.link_resolution or {})
        loser_resolution = dict(loser.link_resolution or {})
        winner_description = winner.description
        winner_ats = winner.ats
        winner_eligibility = dict(winner.internship_eligibility or {})
        merge_sighting(winner, loser)
        if not _source_only(winner):
            # A curator lead contributes provenance, never a weaker lifecycle
            # or eligibility verdict over an official/ATS winner.
            merged_provenance = list(
                winner.link_resolution.get("source_only_provenance", [])
                if isinstance(winner.link_resolution, dict) else []
            )
            if loser_resolution.get("status") == "source_only":
                provenance = {
                    key: value for key, value in loser_resolution.items()
                    if key not in {"status", "source_only_provenance"}
                }
                if provenance and provenance not in merged_provenance:
                    merged_provenance.append(provenance)
            winner.link_resolution = winner_resolution
            if merged_provenance:
                winner.link_resolution["source_only_provenance"] = merged_provenance
            winner.description = winner_description
            winner.ats = winner_ats
            winner.internship_eligibility = winner_eligibility
        selected[key] = winner
        dropped += 1
    return list(selected.values()), dropped


def _internship_evidence_pass(new_jobs: list[Job], jobs_state: dict,
                              now: int, budget: int | None = None,
                              domains: dict | None = None) -> dict:
    """Fetch a bounded batch of employer pages for internship discovery leads.

    Curated feeds deliberately enter as ``source_only``.  This pass promotes a
    lead only after its employer URL returns a readable posting, then runs the
    deterministic internship parser again on that text.  Failures are stamped
    on the record so a blocked page is not retried every crawl.
    """
    from . import company_research, posting, quality

    if (env("RADAR_INTERNSHIP_SCRAPE_DISABLE", "").lower() in {"1", "true", "yes"}
            or env("RADAR_SCRAPE_DISABLE", "").lower() in {"1", "true", "yes"}):
        return {"selected": 0, "fetched": 0, "confirmed": 0, "errors": 0}
    try:
        limit = max(0, int(env("RADAR_INTERNSHIP_SCRAPE_LIMIT", "40")
                            if budget is None else budget))
    except (TypeError, ValueError):
        limit = 40
    limit = min(limit, 80)
    if not limit:
        return {"selected": 0, "fetched": 0, "confirmed": 0, "errors": 0}
    try:
        workers = max(1, min(8, int(env("RADAR_INTERNSHIP_SCRAPE_WORKERS", "4"))))
    except (TypeError, ValueError):
        workers = 4

    def source_only(value: Job | dict) -> bool:
        resolution = value.link_resolution if isinstance(value, Job) else value.get("link_resolution")
        return isinstance(resolution, dict) and resolution.get("status") == "source_only"

    def get(value: Job | dict, key: str, default=None):
        return getattr(value, key, default) if isinstance(value, Job) else value.get(key, default)

    def checked_at(value: Job | dict) -> int:
        direct = get(value, "internship_fetch_checked_at", 0)
        evidence = get(value, "internship_eligibility", {}) or {}
        nested = evidence.get("fetch_checked_at", 0) if isinstance(evidence, dict) else 0
        try:
            return max(int(direct or 0), int(nested or 0))
        except (TypeError, ValueError):
            return 0

    retry_after = 3 * 86400

    def credible_url(url: str) -> bool:
        parsed = urlsplit(url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.casefold()
        path = parsed.path.casefold().rstrip("/")
        ats_host = any(host == token or host.endswith("." + token) for token in (
            "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
            "oraclecloud.com", "eightfold.ai", "icims.com", "smartrecruiters.com",
            "dayforcehcm.com", "jobvite.com",
        ))
        parts = [part for part in path.split("/") if part]
        if len(parts) < 2 or parts[-1] in {"careers", "jobs", "internships", "opportunities"}:
            return False
        if ats_host:
            return True
        return (any(token in path for token in ("/job", "/career", "/requisition", "/posting"))
                or bool(re.search(
                    r"(?:intern|role|engineer|developer|scientist|[0-9]{2,})",
                    parts[-1],
                )))

    def title_core_tokens(title: str) -> list[str]:
        ignored = {"intern", "internship", "co", "op", "cooperative", "education",
                   "student", "worker", "summer", "spring", "winter", "fall"}
        return [token for token in re.findall(r"[a-z]{3,}", title.casefold())
                if token not in ignored]

    def body_matches_title(title: str, text: str) -> bool:
        tokens = title_core_tokens(title)
        if not tokens:
            return False
        distinctive = [token for token in tokens if token not in {
            "engineer", "engineering", "developer", "development", "scientist", "science",
        }]
        tokens = distinctive or tokens
        body = text.casefold()
        aliases = {"engineer": "engineering", "scientist": "science",
                   "developer": "development", "analytics": "analytic"}
        matched = sum(
            1 for token in tokens
            if token in body or (token in aliases and aliases[token] in body)
        )
        return matched >= min(2, len(tokens))

    def body_has_position_proof(text: str) -> bool:
        return analyze({"title": "", "description": text}).get("internship_signal") == "body"
    candidates: list[tuple[int, str, Job | dict]] = []
    seen: set[str] = set()
    for value in new_jobs:
        if not get(value, "url") or get(value, "description"):
            continue
        marker = checked_at(value)
        if marker and now - marker < retry_after:
            continue
        key = posting_key(get(value, "url")) or canonical_url(get(value, "url"))
        if not key or key in seen:
            continue
        seen.add(key)
        evidence = get(value, "internship_eligibility", {}) or {}
        signal = str(evidence.get("internship_signal") or "").casefold()
        title_has_intern = bool(re.search(
            r"\b(?:intern(?:ship)?|co[- ]?op|cooperative education|student worker)\b",
            str(get(value, "title", "")), re.I,
        ))
        if (not source_only(value) and signal not in {"title", "body"}
                and not evidence.get("source_signal") and not title_has_intern):
            continue
        if evidence.get("degree_requirement") == "graduate_only":
            continue
        if term_status(evidence, now) == "past":
            continue
        term = str(evidence.get("source_term") or evidence.get("term_start") or "")
        age_bonus = 1000 if not marker else min(100, max(0, (now - marker) // retry_after))
        priority = (1 if source_only(value) else 0,
                    1 if any(token in f"{get(value, 'title', '')} {term}".casefold()
                             for token in ("2027", "spring", "winter", "summer")) else 0,
                    age_bonus, int(get(value, "score", 0) or 0))
        candidates.append((priority[0] * 100000 + priority[1] * 10000 + priority[2] * 100 + priority[3], key, value))

    for value in jobs_state.values():
        if not get(value, "url") or get(value, "description"):
            continue
        if lifecycle.is_terminal(value):
            continue
        if get(value, "posting_status", "open") in {"expired", "filled", "closed"}:
            continue
        marker = checked_at(value)
        if marker and now - marker < retry_after:
            continue
        key = posting_key(get(value, "url")) or canonical_url(get(value, "url"))
        if not key or key in seen:
            continue
        seen.add(key)
        evidence = get(value, "internship_eligibility", {}) or {}
        signal = str(evidence.get("internship_signal") or "").casefold()
        title_has_intern = bool(re.search(
            r"\b(?:intern(?:ship)?|co[- ]?op|cooperative education|student worker)\b",
            str(get(value, "title", "")), re.I,
        ))
        if (not source_only(value) and signal not in {"title", "body"}
                and not evidence.get("source_signal") and not title_has_intern):
            continue
        if evidence.get("degree_requirement") == "graduate_only":
            continue
        if term_status(evidence, now) == "past":
            continue
        term = str(evidence.get("source_term") or evidence.get("term_start") or "")
        age_bonus = 1000 if not marker else min(100, max(0, (now - marker) // retry_after))
        priority = (1 if source_only(value) else 0,
                    1 if any(token in f"{get(value, 'title', '')} {term}".casefold()
                             for token in ("2027", "spring", "winter", "summer")) else 0,
                    age_bonus, int(get(value, "score", 0) or 0))
        candidates.append((priority[0] * 100000 + priority[1] * 10000 + priority[2] * 100 + priority[3], key, value))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    # Keep the review-only backlog moving without allowing it to consume the
    # whole budget: current official ATS sightings get at least half a batch.
    official = [item for item in candidates if not source_only(item[2])]
    curated = [item for item in candidates if source_only(item[2])]
    official_cap = min(len(official), max(1, (limit + 1) // 2))
    selected_items = official[:official_cap] + curated[:limit - official_cap]
    if len(selected_items) < limit:
        selected_items.extend(official[official_cap:limit - len(selected_items) + official_cap])
    selected = [item[2] for item in selected_items[:limit]]
    if not selected:
        return {"selected": 0, "fetched": 0, "confirmed": 0, "errors": 0}

    def fetch(value: Job | dict) -> tuple[Job | dict, bool | None, str]:
        rec = {"url": get(value, "url", ""), "company": get(value, "company", ""),
               "title": get(value, "title", ""), "ats": get(value, "ats", ""),
               "source": get(value, "source", "")}
        if quality.spa_kind(rec):
            alive, text = quality.fetch_posting_spa(rec, domains or {})
        else:
            alive, text = quality.fetch_posting(rec["url"])
        return value, alive, text or ""

    results: list[tuple[Job | dict, bool | None, str]] = []
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {executor.submit(fetch, value): value for value in selected}
    try:
        deadline = min(120, max(1, int(env("RADAR_INTERNSHIP_SCRAPE_DEADLINE", "120"))))
    except (TypeError, ValueError):
        deadline = 120
    done, pending = wait(futures, timeout=deadline)
    for future in done:
        try:
            results.append(future.result())
        except Exception:
            results.append((futures[future], None, ""))
    for future in pending:
        future.cancel()
    executor.shutdown(wait=False, cancel_futures=True)

    confirmed = errors = 0
    deferred = len(pending)
    research = company_research.load()
    research_changed = False
    for value, alive, text in results:
        if isinstance(value, Job):
            record = value
            resolution = dict(record.link_resolution or {})
        else:
            record = _job_from_record(value)
            resolution = dict(value.get("link_resolution") or {})
        if isinstance(value, dict):
            evidence = dict(record.internship_eligibility or {})
            evidence["fetch_checked_at"] = now
            record.internship_eligibility = evidence
            value["internship_eligibility"] = evidence
            value["internship_fetch_checked_at"] = now
        else:
            record.internship_eligibility = dict(record.internship_eligibility or {})
            record.internship_eligibility["fetch_checked_at"] = now
        title = str(record.title or "")
        lower_text = text.casefold()
        posting_markers = ("responsibilities", "qualifications", "requirements",
                           "what you'll do", "how to apply", "apply now")
        role_markers = ("intern", "internship", "co-op", "student", "undergraduate",
                        "summer analyst", "university")
        usable_posting = (len(text.strip()) >= 200
                          and any(marker in lower_text for marker in posting_markers)
                          and (any(marker in lower_text for marker in role_markers)
                               or any(marker in title.casefold() for marker in role_markers))
                          and any(marker in lower_text for marker in (
                              "software", "engineering", "developer", "programming",
                              "computer science", "data science", "machine learning",
                              "analytics", "security", "hardware", "research",
                          )))
        title_relevant = body_matches_title(title, text)
        position_proof = body_has_position_proof(text)
        title_phrase = re.sub(
            r"\b(?:20\d{2}|summer|spring|winter|fall)\b",
            " ", title.casefold(), flags=re.I,
        )
        title_phrase = re.sub(r"[^a-z]+", " ", title_phrase).strip()
        full_title_present = bool(title_phrase and title_phrase in lower_text)
        usable_posting = usable_posting and title_relevant
        if source_only(value):
            usable_posting = usable_posting and (position_proof or full_title_present)
        if source_only(value) and not credible_url(record.url):
            usable_posting = False
        if alive is True and usable_posting:
            record.description = text[:7000]
            if company_research.capture_into(
                    research, company=record.company, title=record.title,
                    url=record.url, text=text, retrieved_at=now):
                research_changed = True
            old_provenance = resolution.get("source_only_provenance")
            if not isinstance(old_provenance, list) and resolution.get("status") == "source_only":
                old_provenance = [{
                    key: item for key, item in resolution.items() if key != "status"
                }]
            resolution = {
                "status": "resolved",
                "method": "internship_employer_fetch",
                "original_url": record.url,
                "resolved_url": record.url,
                "checked_at": now,
            }
            if isinstance(old_provenance, list) and old_provenance:
                resolution["source_only_provenance"] = old_provenance
            record.link_resolution = resolution
            annotate(record)
            keep, alert_ok, reasons = gates(record)
            manually_archived = bool(value.get("manual_archived")) if isinstance(value, dict) else False
            record.alert_ok = bool(keep and alert_ok and not manually_archived)
            score(record, now)
            record.score_reasons.extend(reasons)
            posting_hints = posting.analyze(text)
            if posting_hints:
                hint_record = {"alert_ok": record.alert_ok, "score": record.score,
                                "score_reasons": record.score_reasons}
                posting.apply_record(hint_record, posting_hints, fetched=True, now=now)
                record.posting = hint_record.get("posting", {})
                record.alert_ok = hint_record.get("alert_ok", record.alert_ok)
                record.score = hint_record.get("score", record.score)
                record.score_reasons = hint_record.get("score_reasons", record.score_reasons)
            confirmed += 1
        else:
            errors += 1
            if alive is False:
                lifecycle.mark_terminal(
                    value, lifecycle.status_from_dead_text(text), now,
                    "internship employer page is no longer available",
                )
            if isinstance(value, dict):
                value["internship_fetch_status"] = (
                    "closed" if alive is False else "unreadable" if alive is True else "unavailable"
                )
            else:
                record.internship_eligibility = dict(record.internship_eligibility or {})
                record.internship_eligibility["fetch_status"] = (
                    "closed" if alive is False else "unreadable" if alive is True else "unavailable"
                )
        if isinstance(value, dict) and alive is True and usable_posting:
            # Direct employer evidence refreshes liveness even when this row
            # has fallen out of its curator's bounded discovery feed.
            lifecycle.touch(value, now, "employer page")
            value.update({
                "internship_eligibility": record.internship_eligibility,
                "location_eligibility": record.location_eligibility,
                "score": record.score,
                "evidence_score": record.evidence_score,
                "eligibility": record.eligibility,
                "priority_tier": record.priority_tier,
                "score_raw": record.score_raw,
                "score_calibrated": record.score_calibrated,
                "score_dimensions": record.score_dimensions,
                "score_dimensions_raw": record.score_dimensions_raw,
                "score_reasons": record.score_reasons,
                "alert_ok": record.alert_ok,
                "link_resolution": record.link_resolution,
                "posting": record.posting,
                "rules_v": RULES_VERSION,
                "score_version": RULES_VERSION,
            })
        elif isinstance(value, dict):
            value["internship_eligibility"] = record.internship_eligibility
    if research_changed:
        company_research.save(research)
    stats = {"selected": len(selected), "fetched": len(results),
             "confirmed": confirmed, "errors": errors}
    if deferred:
        stats["deferred"] = deferred
    return stats


def rescore() -> int:
    """Reapply internship gates and scores without touching new-grad state."""
    now = int(time.time())
    jobs_state = state.jobs()
    changed = 0
    alerts = 0
    threshold = int(profile().get("thresholds", {}).get("alert", 60))
    for record in jobs_state.values():
        job = _job_from_record(record)
        # Re-run the parser on every repair so new work-quality evidence is
        # captured. annotate() preserves the last trusted cohort parse when
        # the state record has no persisted job description.
        annotate(job)
        keep, alert_ok, reasons = gates(job)
        score(job, now)
        record.update({
            "profile": "internship",
            "internship_eligibility": job.internship_eligibility,
            "location_eligibility": job.location_eligibility,
            "score": job.score,
            "evidence_score": job.evidence_score,
            "eligibility": job.eligibility,
            "priority_tier": job.priority_tier,
            "score_raw": job.score_raw,
            "score_calibrated": job.score_calibrated,
            "score_dimensions": job.score_dimensions,
            "score_dimensions_raw": job.score_dimensions_raw,
            "score_reasons": job.score_reasons + reasons,
            "alert_ok": bool(keep and alert_ok and not record.get("manual_archived")),
            "rules_v": RULES_VERSION,
            "score_version": RULES_VERSION,
        })
        lifecycle.normalize_record(record, now)
        if lifecycle.is_terminal(record):
            record["alert_ok"] = False
        changed += 1
        if record["alert_ok"] and record["score"] >= threshold:
            alerts += 1
    registry = state.companies()
    runs = state.load("runs.json", [])
    alert_history = state.load("alert_history.json", [])
    write_outputs(jobs_state, registry, runs, alert_history)
    state.save("jobs.json", jobs_state)
    applied = state.applied()
    from .notion_sync import archive_terminal_pages
    archived = archive_terminal_pages(applied, jobs_state)
    if archived:
        state.save("applied.json", applied)
        print(f"internship rescore: archived {archived} terminal tracked page(s) in Notion")
    print(f"internship rescore: rebuilt {changed} stored jobs; {alerts} currently alert-eligible")
    return 0


def regate() -> int:
    """Compatibility command for the isolated internship namespace."""
    return rescore()


def crawl() -> int:
    from .dedupe import build_official_match_index, find_existing_official_match
    from .main import (
        _existing_posting_ids,
        _fetch_aggregators,
        _fetch_ats,
        _has_independent_employer_evidence,
        _job_identity,
        _merge_job_sighting,
        _merge_record_sighting,
        _posting_identity,
        _promote_existing_official_record,
        _repair_duplicate_job_state,
        _state_job_identity,
        _unique_discovered_jobs,
        deliver_alerts,
    )

    t0 = time.time()
    now = int(t0)
    disabled = {s.strip() for s in env("RADAR_DISABLE_SOURCES", "").split(",") if s.strip()}
    registry = state.companies()
    discovery.seed_registry(registry, seeds())
    jobs_state = state.jobs()
    applied = state.applied()
    jobs_state, repaired = _repair_duplicate_job_state(jobs_state, applied)
    if repaired:
        state.save("jobs.json", jobs_state)
    threshold = int(profile().get("thresholds", {}).get("alert", 60))
    previous_alert_ready_ids = {
        str(jid) for jid, row in jobs_state.items()
        if row.get("alert_ok") and row.get("score", 0) >= threshold
        and not lifecycle.is_terminal(row) and not row.get("manual_archived")
    }
    prior_alert_history = state.load("alert_history.json", [])
    prior_alert_ids = {
        str(row.get("id")) for row in prior_alert_history
        if isinstance(row, dict) and row.get("id")
    }
    agg_jobs, agg_stats, healthy_aggregator_boards = _fetch_aggregators(disabled)
    harvested = discovery.harvest(registry, agg_jobs,
                                  max_new=int(env("RADAR_INTERNSHIP_MAX_HARVEST", "100")))
    activated, invalidated = discovery.probe_new(
        registry, budget=int(env("RADAR_INTERNSHIP_PROBE_BUDGET", "20")))
    ats_jobs, ats_stats, healthy_ats_boards = _fetch_ats(registry, disabled)
    discovered, feed_duplicates = _dedupe_internship_sightings(
        agg_jobs + ats_jobs, _merge_job_sighting)
    discovered, generic_duplicates = _unique_discovered_jobs(discovered)
    feed_duplicates += generic_duplicates
    if feed_duplicates:
        print(f"internship hygiene: ignored {feed_duplicates} duplicate sighting(s)")
    print(f"internship aggregators: {agg_stats}")
    print(f"internship discovery: harvested {harvested}, probed {activated} active / {invalidated} invalid")
    print(f"internship ats: {ats_stats}")

    seed_sectors = {norm(s["name"]): s.get("sector", "other") for s in seeds()}
    new_jobs: list[Job] = []
    applied = state.applied()
    manual_upgrades: dict[str, dict] = {}
    authoritative_existing_ids: set[str] = set()
    dropped = 0
    seen: set[str] = set()
    pending_new_jobs: dict[str, Job] = {}
    existing_posting_ids = _existing_posting_ids(jobs_state)
    incoming_batch = []
    for job in discovered:
        sighting = job.to_record()
        sighting["_radar_id"] = _job_identity(job)
        incoming_batch.append(sighting)
    match_index = build_official_match_index(jobs_state, incoming_batch)
    for job in discovered:
        if not job.company or not job.title or not job.url:
            continue
        job.profile = "internship"
        incoming_sighting = job.to_record()
        upgrade_id = find_existing_official_match(
            incoming_sighting, jobs_state, incoming_batch,
            index=match_index, existing_posting_ids=existing_posting_ids)
        if upgrade_id:
            job._radar_id = upgrade_id
            incoming_key = _posting_identity(incoming_sighting.get("url"))
            for batch_record in incoming_batch:
                if _posting_identity(batch_record.get("url")) == incoming_key:
                    batch_record["_radar_id"] = upgrade_id
        if upgrade_id and upgrade_id in jobs_state:
            _promote_existing_official_record(
                jobs_state[upgrade_id], incoming_sighting)
        jid = upgrade_id or _state_job_identity(job, jobs_state, existing_posting_ids)
        if jid in seen:
            if jid in pending_new_jobs:
                _merge_job_sighting(pending_new_jobs[jid], job)
            continue
        job._radar_id = jid
        seen.add(jid)
        existing = jobs_state.get(jid)
        if existing is not None:
            lifecycle.touch(existing, now, job.source or "monitored source")
        if existing is not None and existing.get("source") != "manual":
            if not job.sector:
                job.sector = seed_sectors.get(norm(job.company), "other")
            annotate(job)
            keep, alert_ok, reasons = gates(job)
            job.alert_ok = alert_ok
            sighting = job.to_record()
            authoritative = (
                (job.link_resolution or {}).get("status") != "source_only"
                and _has_independent_employer_evidence(job)
            )
            if authoritative:
                authoritative_existing_ids.add(str(jid))
                sighting["_fresh_employer_evidence"] = True
            _merge_record_sighting(existing, sighting)
            if authoritative:
                score(job, now)
                job.score_reasons += reasons
                existing["internship_eligibility"] = dict(job.internship_eligibility)
                existing["location_eligibility"] = dict(job.location_eligibility)
                existing.update({
                    "profile": "internship",
                    "score": job.score,
                    "evidence_score": job.evidence_score,
                    "eligibility": job.eligibility,
                    "priority_tier": job.priority_tier,
                    "score_raw": job.score_raw,
                    "score_calibrated": job.score_calibrated,
                    "score_dimensions": job.score_dimensions,
                    "score_dimensions_raw": job.score_dimensions_raw,
                    "score_reasons": job.score_reasons,
                    "rules_v": RULES_VERSION,
                    "score_version": RULES_VERSION,
                    "alert_ok": bool(
                        keep and job.alert_ok
                        and not existing.get("manual_archived")
                        and not lifecycle.is_terminal(existing)
                    ),
                })
                if job.posting:
                    existing["posting"] = dict(job.posting)
            continue
        if not job.sector:
            job.sector = seed_sectors.get(norm(job.company), "other")
        annotate(job)
        keep, alert_ok, reasons = gates(job)
        if not keep:
            dropped += 1
            continue
        job.alert_ok = alert_ok
        score(job, now)
        job.score_reasons += reasons
        lifecycle.touch(job, now, job.source or "monitored source")
        if existing is not None:
            manual_upgrades[jid] = existing
        job._radar_id = jid
        new_jobs.append(job)
        pending_new_jobs[jid] = job

    # Internship discovery has a separate bounded employer-evidence pass.
    # Curated leads remain review-only until this pass obtains readable text;
    # the general new-grad posting pass is intentionally not used here so the
    # internship budget is concurrent and includes the stored source-only
    # backlog as well as today's discoveries.
    eightfold_domains = {norm(e.get("name", "")): (e.get("extra") or {}).get("domain")
                         for e in registry.values()
                         if e.get("ats") == "eightfold" and (e.get("extra") or {}).get("domain")}
    # Keep the free inline posting-analysis phase (sponsorship, experience,
    # degree, and company research) while reserving network fetches for the
    # concurrent internship evidence pass below.
    from .posting import scrape_pass
    inline_stats = scrape_pass(new_jobs, jobs_state, eightfold_domains, now, budget=0)
    pstats = _internship_evidence_pass(new_jobs, jobs_state, now, domains=eightfold_domains)
    if pstats:
        print(f"internship employer evidence: selected {pstats['selected']}, "
              f"fetched {pstats['fetched']}, confirmed {pstats['confirmed']}, "
              f"errors {pstats['errors']}, deferred {pstats.get('deferred', 0)}; "
              f"inline {inline_stats.get('inline', 0)}")

    max_age = int(profile().get("thresholds", {}).get("max_posting_age_days", 30)) * 86400
    new_job_ids = {str(job._radar_id or _job_identity(job)) for job in new_jobs}
    for job in new_jobs:
        record = job.to_record()
        record["id"] = job._radar_id or _job_identity(job)
        old = manual_upgrades.get(record["id"])
        old_record = jobs_state.get(record["id"])
        record["first_seen"] = old.get("first_seen", now) if old else now
        if old:
            record["manual_added"] = True
        lifecycle.merge_record_metadata(record, old_record)
        record["rules_v"] = RULES_VERSION
        record["score_version"] = RULES_VERSION
        record["profile"] = "internship"
        jobs_state[record["id"]] = record

    promoted_existing_ids = set(authoritative_existing_ids)
    for jid, row in jobs_state.items():
        resolution = row.get("link_resolution") or {}
        if (resolution.get("status") == "resolved"
                and resolution.get("method") == "internship_employer_fetch"
                and int(resolution.get("checked_at") or 0) == now):
            promoted_existing_ids.add(str(jid))

    lifecycle_stats = lifecycle.reconcile(
        jobs_state, now, seen,
        healthy_source_boards=healthy_aggregator_boards | healthy_ats_boards,
    )
    if lifecycle_stats["expired"] or lifecycle_stats["reopened"]:
        print(f"internship lifecycle: {lifecycle_stats['expired']} auto-expired, "
              f"{lifecycle_stats['reopened']} reopened from current source")
    def alertable(row: dict) -> bool:
        return bool(
            row.get("alert_ok") and row.get("score", 0) >= threshold
            and not lifecycle.is_terminal(row)
            and not row.get("manual_archived")
            and (row.get("posted_at") is None
                 or now - row.get("posted_at", now) <= max_age)
        )

    for jid in sorted(new_job_ids | promoted_existing_ids):
        row = jobs_state.get(jid)
        if not row or not alertable(row) or jid in previous_alert_ready_ids or jid in prior_alert_ids:
            continue
        # Persist before applying the crawl cap: an eligible promotion must
        # not be lost merely because a higher-scoring batch filled this run.
        row.setdefault("internship_alert_pending_at", now)

    alert_rows: list[dict] = []
    for jid, row in jobs_state.items():
        pending_at = int(row.get("internship_alert_pending_at") or 0)
        if not pending_at:
            continue
        if (jid in prior_alert_ids or not alertable(row)
                or not 0 <= now - pending_at <= max_age):
            row.pop("internship_alert_pending_at", None)
            continue
        candidate = dict(row)
        candidate.pop("internship_alert_pending_at", None)
        candidate["id"] = jid
        candidate["alerted_at"] = now
        alert_rows.append(candidate)
    alert_rows.sort(key=_delivery_sort_key)
    alert_rows = alert_rows[:int(env("RADAR_INTERNSHIP_MAX_ALERTS", "35"))]
    for row in alert_rows:
        jobs_state[row["id"]].pop("internship_alert_pending_at", None)
    alerts = alert_rows
    cutoff = lifecycle.history_cutoff(now)
    jobs_state = {key: value for key, value in jobs_state.items()
                  if value.get("first_seen", now) >= cutoff}
    alert_history = prior_alert_history
    alert_history.extend(alert_rows)
    alert_history = alert_history[-800:]
    runs = state.load("runs.json", [])
    runs.append({"ts": now, "took_s": round(time.time() - t0, 1),
                 "new_jobs": len(new_jobs), "dropped_by_gates": dropped,
                 "alerts": len(alerts), "aggregators": agg_stats, "ats": ats_stats,
                 "pending_alerts": sum(bool(row.get("internship_alert_pending_at"))
                                       for row in jobs_state.values()),
                 "employer_evidence": pstats,
                 "registry_size": len(registry), "profile": "internship"})
    runs = runs[-300:]
    state.save("companies.json", registry)
    state.save("jobs.json", jobs_state)
    from .notion_sync import archive_terminal_pages
    tracker_url_updates = applied_mod.refresh_application_urls(applied, jobs_state)
    if tracker_url_updates:
        print(f"internship tracker: promoted {tracker_url_updates} application URL(s)")
    archived = archive_terminal_pages(applied, jobs_state)
    if archived or tracker_url_updates:
        state.save("applied.json", applied)
    if archived:
        print(f"internship lifecycle: archived {archived} terminal tracked page(s) in Notion")
    state.save("alert_history.json", alert_history)
    state.save("runs.json", runs)
    write_outputs(jobs_state, registry, runs, alert_history)

    if env("RADAR_DEFER_DELIVERY", "").lower() in {"1", "true", "yes"}:
        print("internship crawl: delivery deferred until state publication succeeds")
    else:
        deliver_alerts()
    print(f"internship crawl done: {len(new_jobs)} new jobs, {len(alerts)} alerts, {dropped} gated")
    return 0
