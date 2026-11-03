"""Isolated internship crawler.

The normal radar remains the high-frequency new-grad lane. This module uses
the same normalized Job model and ATS registry primitives, but a different
source set, state namespace, scorer, cadence, and delivery labels.
"""
from __future__ import annotations

import time

from . import discovery, lifecycle, state
from .config import env, profile, seeds
from .digest import write_outputs
from .identity import canonical_url, posting_key
from .internship import RULES_VERSION, annotate, gates, score
from .models import Job, norm


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
    from .main import (
        _existing_posting_ids,
        _fetch_aggregators,
        _fetch_ats,
        _has_independent_employer_evidence,
        _job_identity,
        _merge_record_sighting,
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
    agg_jobs, agg_stats, healthy_aggregator_boards = _fetch_aggregators(disabled)
    harvested = discovery.harvest(registry, agg_jobs,
                                  max_new=int(env("RADAR_INTERNSHIP_MAX_HARVEST", "100")))
    activated, invalidated = discovery.probe_new(
        registry, budget=int(env("RADAR_INTERNSHIP_PROBE_BUDGET", "20")))
    ats_jobs, ats_stats, healthy_ats_boards = _fetch_ats(registry, disabled)
    from .main import _merge_job_sighting
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
    dropped = 0
    seen: set[str] = set()
    existing_posting_ids = _existing_posting_ids(jobs_state)
    for job in discovered:
        if not job.company or not job.title or not job.url:
            continue
        job.profile = "internship"
        jid = _state_job_identity(job, jobs_state, existing_posting_ids)
        if jid in seen:
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
                sighting["_fresh_employer_evidence"] = True
            _merge_record_sighting(existing, sighting)
            if authoritative:
                score(job, now)
                job.score_reasons += reasons
                existing["internship_eligibility"] = dict(job.internship_eligibility)
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

    from .posting import scrape_pass
    eightfold_domains = {norm(e.get("name", "")): (e.get("extra") or {}).get("domain")
                         for e in registry.values()
                         if e.get("ats") == "eightfold" and (e.get("extra") or {}).get("domain")}
    pstats = scrape_pass(new_jobs, jobs_state, eightfold_domains, now,
                         budget=int(env("RADAR_INTERNSHIP_SCRAPE_LIMIT", "10")))
    if pstats:
        print(f"internship posting scrape: {pstats['fetched']} fetched, "
              f"{pstats['closed']} closed ({pstats.get('filled', 0)} filled)")

    max_age = int(profile().get("thresholds", {}).get("max_posting_age_days", 30)) * 86400
    threshold = int(profile().get("thresholds", {}).get("alert", 60))
    alerts = [job for job in new_jobs if job.alert_ok and not lifecycle.is_terminal(job) and
              job.score >= threshold and
              (job.posted_at is None or now - job.posted_at <= max_age)]
    alerts.sort(key=lambda job: -job.score)
    alerts = alerts[:int(env("RADAR_INTERNSHIP_MAX_ALERTS", "35"))]

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

    lifecycle_stats = lifecycle.reconcile(
        jobs_state, now, seen,
        healthy_source_boards=healthy_aggregator_boards | healthy_ats_boards,
    )
    if lifecycle_stats["expired"] or lifecycle_stats["reopened"]:
        print(f"internship lifecycle: {lifecycle_stats['expired']} auto-expired, "
              f"{lifecycle_stats['reopened']} reopened from current source")
    cutoff = lifecycle.history_cutoff(now)
    jobs_state = {key: value for key, value in jobs_state.items()
                  if value.get("first_seen", now) >= cutoff}
    alert_history = state.load("alert_history.json", [])
    for job in alerts:
        record = job.to_record()
        record["id"] = job._radar_id or _job_identity(job)
        record["alerted_at"] = now
        alert_history.append(record)
    alert_history = alert_history[-800:]
    runs = state.load("runs.json", [])
    runs.append({"ts": now, "took_s": round(time.time() - t0, 1),
                 "new_jobs": len(new_jobs), "dropped_by_gates": dropped,
                 "alerts": len(alerts), "aggregators": agg_stats, "ats": ats_stats,
                 "registry_size": len(registry), "profile": "internship"})
    runs = runs[-300:]
    state.save("companies.json", registry)
    state.save("jobs.json", jobs_state)
    from .notion_sync import archive_terminal_pages
    archived = archive_terminal_pages(applied, jobs_state)
    if archived:
        state.save("applied.json", applied)
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
