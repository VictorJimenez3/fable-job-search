"""Pipeline entrypoints.

  python -m radar.main crawl          # full discovery → score → alert cycle
  python -m radar.main applied-sync   # process a GitHub issue event (CI)
  python -m radar.main seed           # initialize registry + taste from seeds
  python -m radar.main notion-backfill  # retry queued Notion syncs
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import applied as applied_mod
from . import discovery, lifecycle, link_resolver, sponsorship, state
from .brief import rerank
from .config import env, profile, profile_id, seeds
from .digest import write_outputs
from .identity import canonical_url, posting_key
from .internship import RULES_VERSION as INTERNSHIP_RULES_VERSION
from .models import Job, norm
from .score import (
    RULES_VERSION,
    apply_company_concentration,
    build_preference_profile,
    career_priority,
    early_career_possible,
    explicit_new_grad,
    gates,
    load_score_preferences,
    normalize_score_preferences,
    regate,
    score,
)
from .sector import infer
from .sources import aggregators, hn, public_feeds
from .sources.ats import FETCHERS, PM_SEARCH_QUERIES

AGG_SOURCES = {
    "simplify": aggregators.fetch_simplify,
    "vansh": aggregators.fetch_vansh,
    "jobright": aggregators.fetch_jobright,
    "jobright_pm": aggregators.fetch_jobright_pm,
    "speedyapply": aggregators.fetch_speedyapply,
    "zapply": aggregators.fetch_zapply,
    "zapply_pm": aggregators.fetch_zapply_pm,
    "hn": hn.fetch_hn,
    **public_feeds.PUBLIC_FETCHERS,
}

AGG_SOURCES_INTERNSHIP = {
    "simplify_internship": aggregators.fetch_simplify_internship,
    "speedyapply_internship": aggregators.fetch_speedyapply_internship,
    "zapply_internship": aggregators.fetch_zapply_internship,
    "dreamwork_internship": aggregators.fetch_dreamwork_internship,
    "applyguy_internship": aggregators.fetch_applyguy_internship,
    "aprameyak_offcycle": aggregators.fetch_aprameyak_offcycle,
    "zero2sudo_notion": public_feeds.fetch_zero2sudo_notion,
    **public_feeds.PUBLIC_INTERNSHIP_FETCHERS,
}

PM_BACKFILL_ATS = {"workday", "phenom"}


def _posting_identity(url: str | None) -> str:
    """Return the strongest safe identity available for a posting URL."""
    return posting_key(url) or canonical_url(url)


def _job_identity(job: Job) -> str:
    """Use a collision-safe ingestion ID without changing ``Job.id``."""
    return str(getattr(job, "_radar_id", "") or job.id)


def _disambiguated_job_identity(job: Job) -> str:
    """Assign a deterministic URL suffix for a role-label collision.

    The legacy company/title/location ID remains the first posting's identity;
    only a distinct URL that collides with that label gets a new key. This
    keeps old stored IDs and references stable while making new requisitions
    distinct at ingestion.
    """
    identity = _posting_identity(job.url)
    if not identity:
        return job.id
    digest = hashlib.sha1(identity.encode(), usedforsecurity=False).hexdigest()[:12]
    value = f"{job.id}-{digest}"
    job._radar_id = value
    return value


def _existing_posting_ids(jobs_state: dict) -> dict[str, str]:
    """Index current and preserved URL variants without rewriting IDs."""
    result: dict[str, str] = {}
    for job_id, record in jobs_state.items():
        urls = [record.get("url"), *(record.get("alternate_urls") or [])]
        resolution = record.get("link_resolution") or {}
        urls.extend((resolution.get("resolved_url"), resolution.get("original_url")))
        for url in urls:
            identity = _posting_identity(url)
            if identity:
                result.setdefault(identity, str(job_id))
    return result


def _state_job_identity(job: Job, jobs_state: dict,
                        existing_posting_ids: dict[str, str]) -> str:
    """Resolve a fresh sighting to existing URL identity or a safe new key."""
    identity = _posting_identity(job.url)
    existing_id = existing_posting_ids.get(identity) if identity else None
    if existing_id:
        job._radar_id = existing_id
        return existing_id
    candidate = _job_identity(job)
    current = jobs_state.get(candidate)
    current_identity = _posting_identity((current or {}).get("url"))
    if identity and current_identity and identity != current_identity:
        return _disambiguated_job_identity(job)
    return candidate


def _discovered_job_priority(job: Job) -> tuple:
    source = str(job.source or "").casefold()
    aggregator = source in {
        "simplify", "vansh", "jobright", "jobright_pm", "speedyapply",
        "zapply", "zapply_pm", "hn",
    }
    return (
        1 if job.ats or not aggregator else 0,
        1 if job.description else 0,
        len(job.description or ""),
        1 if job.posted_at else 0,
        source,
    )


def _append_unique(values: list, items: list) -> bool:
    changed = False
    for item in items:
        if item and item not in values:
            values.append(item)
            changed = True
    return changed


def _source_only_provenance(sighting: Job | dict) -> dict | None:
    """Extract curator-only metadata without treating it as job evidence."""
    resolution = sighting.get("link_resolution") if isinstance(sighting, dict) else sighting.link_resolution
    if not isinstance(resolution, dict) or resolution.get("status") != "source_only":
        return None
    get = sighting.get if isinstance(sighting, dict) else lambda key, default=None: getattr(sighting, key, default)
    payload = {
        "source": get("source", "") or "",
        "source_url": get("source_url", "") or "",
        "source_board": get("source_board", "") or "",
    }
    for key in ("source_note", "reason"):
        if resolution.get(key):
            payload[key] = resolution[key]
    return {key: value for key, value in payload.items() if value}


def _has_independent_employer_evidence(record: Job | dict | None) -> bool:
    """Return whether a row has evidence beyond a curator/source-only lead.

    A direct URL or ATS label alone is not enough: source-only feeds often
    carry a guessed application URL.  A resolved link, a freshly fetched
    employer description, or persisted facts extracted from such a posting
    are the bounded facts that may clear a source-only marker.
    """
    if record is None:
        return False
    get = record.get if isinstance(record, dict) else lambda key, default=None: getattr(record, key, default)
    resolution = get("link_resolution", {}) or {}
    if isinstance(resolution, dict) and resolution.get("status") == "resolved":
        return True
    if isinstance(resolution, dict) and resolution.get("status") == "source_only":
        return False
    if str(get("description", "") or "").strip():
        return True
    # Descriptions are deliberately omitted from persisted records. Their
    # evaluated evidence must still protect a previously verified posting
    # when a later curator-only sighting contributes another source link.
    if isinstance(record, dict):
        evidence = get("eligibility_evidence", {}) or {}
        if (isinstance(evidence, dict)
                and evidence.get("source") in {
                    "title", "description", "cohort-conflict", "contradictory-posting"}
                and evidence.get("status") in {"verified", "review"}):
            return True
        internship = get("internship_eligibility", {}) or {}
        if (isinstance(internship, dict)
                and internship.get("internship_signal") in {"title", "body"}):
            return True
    posting = get("posting", {}) or {}
    if not isinstance(posting, dict):
        return False
    # These fields are emitted by the posting scraper and are not present in
    # a bare public/curator lead.  Require a direct/official-ish row as well
    # so an aggregator's own ranking excerpt cannot promote itself.
    source = str(get("source", "") or "").casefold()
    ats = str(get("ats", "") or "").casefold()
    url = str(get("url", "") or "")
    aggregator = link_resolver.is_aggregator_url(url)
    scraped_keys = {
        "ranking_evidence", "years_min", "years_preferred_min",
        "experience_conflict", "experience_alternative", "years_note",
    }
    return bool((ats or not aggregator) and source not in {
        "zero2sudo_notion", "zero2sudo", "linkedin_posts",
    } and any(posting.get(key) not in (None, "", {}, []) for key in scraped_keys))


def _keep_source_only_provenance(current: Job | dict | None,
                                 sighting: Job | dict) -> dict:
    """Keep source-only notes while leaving lifecycle/eligibility status intact."""
    payload = _source_only_provenance(sighting)
    if isinstance(current, Job):
        current_resolution = current.link_resolution
        evidence_record = current
    elif isinstance(current, dict) and any(
            key in current for key in ("url", "description", "posting", "ats")):
        # A full state record lets us distinguish an unverified row from one
        # whose resolution field was simply omitted by the compact serializer.
        current_resolution = current.get("link_resolution") or {}
        evidence_record = current
    else:
        current_resolution = current
        evidence_record = current
    result = dict(current_resolution or {})
    if payload is None:
        return result
    previous = result.get("source_only_provenance")
    provenance = list(previous) if isinstance(previous, list) else []
    if payload not in provenance:
        provenance.append(payload)
    result["source_only_provenance"] = provenance
    # Do not let a source-only lead become an apparently verified posting just
    # because the target had no link_resolution field yet.  Conversely, keep
    # an independently fetched employer posting's existing status intact.
    if not _has_independent_employer_evidence(evidence_record):
        if not result.get("status"):
            result["status"] = "source_only"
        source_resolution = (
            sighting.get("link_resolution") if isinstance(sighting, dict)
            else sighting.link_resolution
        ) or {}
        for key in ("reason", "source_note"):
            if source_resolution.get(key) and not result.get(key):
                result[key] = source_resolution[key]
    return result


def _merge_job_sighting(winner: Job, sighting: Job) -> None:
    """Keep every feed/source link when one posting wins a feed dedupe."""
    _append_unique(winner.locations, sighting.locations)
    _append_unique(winner.source_board_variants,
                   [winner.source_board, sighting.source_board,
                    *sighting.source_board_variants])
    _append_unique(winner.source_variants, [winner.source, sighting.source])
    _append_unique(winner.source_url_variants,
                   [winner.source_url, sighting.source_url,
                    *sighting.source_url_variants])
    winner_url = canonical_url(winner.url)
    for url in [sighting.url, *sighting.alternate_urls]:
        if url and canonical_url(url) != winner_url:
            _append_unique(winner.alternate_urls, [url])
    if (sighting.link_resolution or {}).get("status") == "source_only":
        winner.link_resolution = _keep_source_only_provenance(
            winner, sighting)
    elif sighting.link_resolution:
        # A closed aggregator copy is definitive for that aggregator URL, but
        # an official ATS variant gets its own liveness check. Do not carry a
        # stale Jobright banner onto a stronger direct posting winner.
        closed_aggregator = (
            sighting.link_resolution.get("status") == "closed" and
            link_resolver.is_aggregator_url(winner.url) is False
        )
        if not closed_aggregator and (
                not winner.link_resolution or
                winner.link_resolution.get("status") != "resolved"):
            winner.link_resolution = dict(sighting.link_resolution)


def _merge_discovered_variant_sighting(winner: Job, sighting: Job) -> None:
    """Merge a location-family feed sighting while retaining rich crawl data."""
    _merge_job_sighting(winner, sighting)
    _append_unique(winner.locations, sighting.locations)
    if not winner.description and sighting.description:
        winner.description = sighting.description
    if not winner.salary and sighting.salary:
        winner.salary = sighting.salary
    if not winner.ats and sighting.ats:
        winner.ats = sighting.ats
    if not winner.sector and sighting.sector:
        winner.sector = sighting.sector
    if not winner.posted_at or (sighting.posted_at and sighting.posted_at < winner.posted_at):
        winner.posted_at = sighting.posted_at
    winner.remote = winner.remote or sighting.remote


def _collapse_discovered_variant_jobs(discovered: list[Job]) -> tuple[list[Job], int]:
    """Apply the durable family rules to one fresh feed batch in memory.

    The durable repair runs at the beginning of every crawl.  This companion
    pass prevents a newly arrived official row plus its location-specific
    aggregator sightings from producing duplicate alerts before the next run.
    """
    if len(discovered) < 2:
        return discovered, 0
    from .dedupe import collapse_cross_source_jobs, collapse_location_variants, resolve_alias

    by_id = {_job_identity(job): job for job in discovered}
    records = {}
    for job in discovered:
        key = _job_identity(job)
        record = job.to_record()
        record["id"] = key
        records[key] = record
    records, aliases, first_merged = collapse_cross_source_jobs(records)
    records, more_aliases, second_merged = collapse_location_variants(records)
    aliases.update(more_aliases)
    if not aliases:
        return discovered, 0
    for old in list(aliases):
        aliases[old] = resolve_alias(aliases[old], aliases, existing=records)
    for old, new in aliases.items():
        loser = by_id.get(old)
        winner = by_id.get(new)
        if loser is not None and winner is not None:
            _merge_discovered_variant_sighting(winner, loser)
        by_id.pop(old, None)
    # The merge logic operates on records so it can share the durable repair
    # rules. Copy its audit metadata back onto the surviving Job before the
    # normal crawl serializer runs; otherwise the first fresh merge would be
    # visible only after a later repair cycle.
    for key, record in records.items():
        winner = by_id.get(key)
        if winner is None:
            continue
        winner.posting_family_id = str(record.get("posting_family_id") or "")
        winner.posting_identity = dict(record.get("posting_identity") or {})
    return [by_id[key] for key in records if key in by_id], first_merged + second_merged


def _merge_record_sighting(target: dict, sighting: dict) -> bool:
    """Merge provenance from a new sighting into an existing state record."""
    changed = False
    source_boards = target.setdefault("source_board_variants", [])
    changed |= _append_unique(
        source_boards,
        [target.get("source_board"), sighting.get("source_board"),
         *sighting.get("source_board_variants", [])],
    )
    if not target.get("source_board") and sighting.get("source_board"):
        target["source_board"] = sighting["source_board"]
        changed = True
    source_variants = target.setdefault("source_variants", [])
    changed |= _append_unique(source_variants,
                              [target.get("source"), sighting.get("source"),
                               *sighting.get("source_variants", [])])
    source_urls = target.setdefault("source_url_variants", [])
    changed |= _append_unique(source_urls,
                              [target.get("source_url"), sighting.get("source_url"),
                               *sighting.get("source_url_variants", [])])
    locations = target.setdefault("locations", [])
    before_locations = len(locations)
    _append_unique(locations, sighting.get("locations") or [])
    changed |= len(locations) != before_locations
    for key in ("posting_family_id", "posting_identity"):
        if not target.get(key) and sighting.get(key):
            target[key] = sighting[key]
            changed = True
    target_url = canonical_url(target.get("url"))
    target_identity = _posting_identity(target.get("url"))
    resolution = sighting.get("link_resolution") or {}
    evidence = (sighting.get("posting") or {}).get("ranking_evidence")
    if (target_identity and target_identity == _posting_identity(sighting.get("url")) and evidence
            and not (target.get("posting") or {}).get("ranking_evidence")):
        target.setdefault("posting", {})["ranking_evidence"] = evidence
        changed = True
    # ``to_record`` deliberately scrubs the description, so the crawl loop
    # marks a sighting whose employer text was just evaluated.  Copy the full
    # compact evidence object, including negative/contradictory experience
    # floors; retaining only a positive new-grad flag would resurrect stale
    # eligibility on the next state-wide re-gate.
    fresh_evidence = sighting.get("eligibility_evidence")
    fresh_employer_evidence = bool(sighting.get("_fresh_employer_evidence"))
    if (fresh_employer_evidence
            and (sighting.get("link_resolution") or {}).get("status") != "source_only"
            and isinstance(fresh_evidence, dict) and fresh_evidence):
        if target.get("eligibility_evidence") != fresh_evidence:
            target["eligibility_evidence"] = dict(fresh_evidence)
            changed = True
        explicit = (bool(sighting["explicit_new_grad"])
                    if "explicit_new_grad" in sighting
                    else fresh_evidence.get("status") == "verified")
        if target.get("explicit_new_grad") != explicit:
            target["explicit_new_grad"] = explicit
            changed = True
    current_resolution = target.get("link_resolution") or {}
    source_only_current = current_resolution.get("status") == "source_only"
    authoritative_fresh = bool(
        fresh_employer_evidence
        and resolution.get("status") != "source_only"
    )
    if source_only_current and authoritative_fresh:
        # A direct ATS sighting has now supplied the independent employer
        # facts.  Keep the curator trail, but do not let its old status gate
        # the verified record forever.
        retained = {
            key: value for key, value in current_resolution.items()
            if key not in {"status", "reason", "source_note"}
        }
        if "source_only_provenance" not in retained:
            prior = _source_only_provenance(target)
            if prior:
                retained["source_only_provenance"] = [prior]
        if retained != current_resolution:
            target["link_resolution"] = retained
            changed = True
        current_resolution = retained
        source_only_current = False
    alternate_urls = target.setdefault("alternate_urls", [])
    for url in [sighting.get("url"), *sighting.get("alternate_urls", [])]:
        if url and canonical_url(url) != target_url:
            changed |= _append_unique(alternate_urls, [url])
    if resolution.get("status") == "source_only":
        preserved = _keep_source_only_provenance(target, sighting)
        if preserved != (target.get("link_resolution") or {}):
            target["link_resolution"] = preserved
            changed = True
        return changed
    if source_only_current:
        # A direct URL label without current employer facts must not clear the
        # source-only gate.  Provenance and aliases above are still retained.
        return changed
    closed_aggregator = (
        resolution.get("status") == "closed" and
        link_resolver.is_aggregator_url(target.get("url")) is False
    )
    if (resolution and not closed_aggregator and
            (not target.get("link_resolution") or
             target.get("link_resolution", {}).get("status") != "resolved")):
        replacement_resolution = dict(resolution)
        prior_provenance = current_resolution.get("source_only_provenance")
        if prior_provenance and "source_only_provenance" not in replacement_resolution:
            replacement_resolution["source_only_provenance"] = prior_provenance
        target["link_resolution"] = replacement_resolution
        changed = True
    return changed


def _unique_discovered_jobs(discovered: list[Job]) -> tuple[list[Job], int]:
    """Deduplicate posting identities while preserving distinct requisitions."""
    selected: dict[str, Job] = {}
    passthrough: list[Job] = []
    dropped = 0
    for job in discovered:
        _append_unique(job.source_variants, [job.source])
        _append_unique(job.source_url_variants, [job.source_url])
        key = _posting_identity(job.url)
        if not key:
            passthrough.append(job)
            continue
        previous = selected.get(key)
        if previous is None:
            selected[key] = job
        elif _discovered_job_priority(job) > _discovered_job_priority(previous):
            _merge_job_sighting(job, previous)
            selected[key] = job
            dropped += 1
        else:
            _merge_job_sighting(previous, job)
            dropped += 1
    # ``Job.id`` is intentionally company/title/location based. Multiple real
    # requisitions can therefore share it; retain the best candidate under the
    # legacy key and assign deterministic URL-derived keys to the rest. The
    # URL identity, not input order, chooses which posting keeps the old key.
    role_groups: dict[str, list[Job]] = {}
    role_passthrough: list[Job] = []
    for job in list(selected.values()) + passthrough:
        role_key = job.id if _posting_identity(job.url) else ""
        if role_key:
            role_groups.setdefault(role_key, []).append(job)
        else:
            role_passthrough.append(job)
    by_role: list[Job] = []
    for candidates in role_groups.values():
        ordered = sorted(
            candidates,
            key=lambda job: (_discovered_job_priority(job), _posting_identity(job.url)),
            reverse=True,
        )
        by_role.append(ordered[0])
        for job in ordered[1:]:
            _disambiguated_job_identity(job)
            by_role.append(job)
    variant_jobs, variant_dropped = _collapse_discovered_variant_jobs(
        by_role + role_passthrough)
    return variant_jobs, dropped + variant_dropped


def _resolve_discovered_links(discovered: list[Job], jobs_state: dict, now: int) -> tuple[list[Job], dict]:
    """Resolve a bounded batch of aggregator links before identity matching."""
    stats = {"attempted": 0, "resolved": 0, "closed": 0, "unchanged": 0, "errors": 0}
    if env("RADAR_DISABLE_LINK_RESOLUTION", "").lower() in {"1", "true", "yes"}:
        return discovered, stats
    limit = max(0, int(env("RADAR_LINK_RESOLVE_LIMIT", "25")))
    existing_posting_ids = _existing_posting_ids(jobs_state)
    candidates = []
    for job in discovered:
        if not link_resolver.is_aggregator_url(job.url):
            continue
        existing_id = existing_posting_ids.get(_posting_identity(job.url))
        existing = jobs_state.get(existing_id or _job_identity(job))
        # A direct primary URL already stored for this stable role identity is
        # stronger than a fresh aggregator sighting; only merge its provenance.
        if existing and not link_resolver.is_aggregator_url(existing.get("url")):
            continue
        if existing and not link_resolver.needs_resolution(existing, job.url, now):
            continue
        candidates.append((job, existing))
    candidates.sort(key=lambda pair: (
        1 if (pair[1] or {}).get("alert_ok") else 0,
        int((pair[1] or {}).get("score") or 0),
        int(pair[0].posted_at or 0),
    ), reverse=True)
    for job, existing in candidates[:limit]:
        stats["attempted"] += 1
        result = link_resolver.resolve_job(job, existing=existing, now=now)
        status = result.get("status")
        if status == "resolved":
            stats["resolved"] += 1
        elif status == "closed":
            stats["closed"] += 1
        elif status in {"error", "not_found"}:
            stats["errors"] += 1
        else:
            stats["unchanged"] += 1
    return discovered, stats


def _repair_duplicate_job_state(jobs_state: dict, applied: list) -> tuple[dict, bool]:
    """Repair URL duplicates and migrate durable references to the survivor."""
    from .dedupe import (
        collapse_cross_source_jobs,
        collapse_jobs,
        collapse_location_variants,
        remap_entry_ids,
        remap_web_jobs,
        resolve_alias,
    )

    jobs_state, aliases, exact_merged = collapse_jobs(jobs_state)
    jobs_state, cross_aliases, cross_merged = collapse_cross_source_jobs(jobs_state)
    aliases.update(cross_aliases)
    jobs_state, family_aliases, family_merged = collapse_location_variants(jobs_state)
    aliases.update(family_aliases)
    if not aliases:
        return jobs_state, False

    applied_changed = remap_entry_ids(applied, aliases, jobs_state)
    tracker_merged = applied_mod.deduplicate_entries(applied)
    shortlist = state.shortlist()
    shortlist_changed = remap_entry_ids(shortlist, aliases, jobs_state)
    # A shortlist can contain two feed variants of one URL even when the
    # durable applied list does not. Keep the first owner selection.
    seen_shortlist = set()
    compact_shortlist = []
    for entry in shortlist:
        key = _posting_identity(entry.get("url")) or f"id:{entry.get('id', '')}"
        if key in seen_shortlist:
            shortlist_changed += 1
            continue
        seen_shortlist.add(key)
        compact_shortlist.append(entry)
    shortlist[:] = compact_shortlist

    history = state.load("alert_history.json", [])
    history_changed = remap_entry_ids(history, aliases, jobs_state)
    untracked = {
        resolve_alias(str(job_id), aliases, existing=jobs_state)
        for job_id in state.load("untracked.json", [])
    }
    web = state.load("web_state.json", {})
    web_changed = remap_web_jobs(web, aliases, existing=jobs_state)

    prior_aliases = state.load("job_aliases.json", {})
    prior_aliases.update(aliases)
    for old in list(prior_aliases):
        prior_aliases[old] = resolve_alias(
            prior_aliases[old], prior_aliases, existing=jobs_state)

    state.save("job_aliases.json", prior_aliases)
    state.save("applied.json", applied)
    state.save("shortlist.json", shortlist)
    state.save("untracked.json", sorted(untracked))
    state.save("alert_history.json", history)
    if web_changed:
        state.save("web_state.json", web)
    print(
        f"hygiene: merged {exact_merged} exact-URL + {cross_merged} "
        f"high-confidence aggregator/ATS + {family_merged} location-family "
        f"duplicate posting record(s); "
        f"migrated {applied_changed + shortlist_changed + history_changed + web_changed} "
        f"reference(s), {tracker_merged} duplicate tracker row(s)"
    )
    return jobs_state, True


def _fetch_aggregators(disabled: set[str]) -> tuple[list[Job], dict, set[str]]:
    jobs, stats = [], {}
    healthy_boards: set[str] = set()
    sources = AGG_SOURCES_INTERNSHIP if profile_id() == "internship" else AGG_SOURCES
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fn): name for name, fn in sources.items() if name not in disabled}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                got = fut.result()
                board = f"aggregator:{name}"
                for job in got:
                    # Keep the canonical board token stable for lifecycle
                    # reconciliation; adapter attribution belongs in variants.
                    original_board = job.source_board
                    variants = list(job.source_board_variants or [])
                    if original_board and original_board != board and original_board not in variants:
                        variants.append(original_board)
                    job.source_board = board
                    job.source_board_variants = variants
                jobs.extend(got)
                stats[name] = len(got)
                healthy_boards.add(board)
            except Exception as e:
                stats[name] = f"error: {type(e).__name__}: {e}"
    return jobs, stats, healthy_boards


def _company_poll_at(entry: dict) -> int:
    """Return the scheduler cursor, with a legacy-state fallback."""
    return int(entry.get("last_polled_at") or entry.get("last_ok") or
               entry.get("first_seen") or 0)


def _select_companies(registry: dict, cap: int) -> list[dict]:
    """Choose a bounded, fair slice of active direct boards.

    ``last_ok`` is a liveness timestamp, not a scheduling cursor.  Sorting by
    it descending made the same successful boards win every crawl, leaving
    the tail of a registry unpolled forever.  A board is therefore marked
    with ``last_polled_at`` by :func:`discovery.record_result`; boards that
    have missed a rotation window are considered first by oldest poll time,
    while the existing PM/seed/sector ordering prioritizes fresh slots.

    The overdue lane is what gives this function a bounded fairness guarantee:
    after a board misses ``RADAR_COMPANY_ROTATION_SECONDS`` it can no longer
    be displaced indefinitely by a recently successful priority board.  The
    timestamp fallback keeps old registries compatible without a migration.
    """
    cap = max(0, int(cap))
    if not cap:
        return []
    try:
        rotation_seconds = max(1, int(env("RADAR_COMPANY_ROTATION_SECONDS", "7200")))
    except (TypeError, ValueError):
        rotation_seconds = 7200

    # Keep the registry key alongside each entry so equal timestamps still
    # produce a stable, deterministic rotation rather than depending on JSON
    # insertion order.
    active = [(str(key), entry) for key, entry in registry.items()
              if entry.get("status") == "active"]
    sector_rank = {"healthtech": 0, "ai_lab": 1, "big_tech": 2, "edtech": 3, "fintech": 4}
    now = int(time.time())

    def rank(item: tuple[str, dict]) -> tuple:
        registry_key, entry = item
        polled = _company_poll_at(entry)
        overdue = now - polled >= rotation_seconds
        if overdue:
            # Once overdue, age is the only scheduling priority.  Putting PM
            # or seed preference before age here recreates the original
            # starvation bug when every board is overdue at crawl start.
            return (0, polled, registry_key)
        return (1,
                0 if entry.get("pm_interest") else 1,
                0 if entry.get("origin") == "seed" else 1,
                sector_rank.get(entry.get("sector", ""), 5),
                polled,
                registry_key)

    active.sort(key=rank)
    return [entry for _, entry in active[:cap]]


def _pm_backfill_ids(entries: list[dict], cap: int) -> set[int]:
    """Bound the expensive PM query fan-out to prioritized query-driven ATSs."""
    candidates = [e for e in entries if e.get("ats") in PM_BACKFILL_ATS]
    return {id(e) for e in candidates[:max(0, cap)]}


def _fetch_ats(registry: dict, disabled: set[str]) -> tuple[list[Job], dict, set[str]]:
    if "ats" in disabled:
        return [], {"ats": "disabled"}, set()
    cap = int(env("RADAR_MAX_COMPANIES", "800"))
    entries = _select_companies(registry, cap)
    selection_now = int(time.time())
    selected_poll_times = [_company_poll_at(entry) for entry in entries]
    active_unpolled = sum(
        1 for entry in registry.values()
        if entry.get("status") == "active" and not (
            entry.get("last_polled_at") or entry.get("last_ok")
        )
    )
    wd_queries = list(dict.fromkeys(
        profile().get("workday_queries") or []
    ))
    pm_queries = list(dict.fromkeys(wd_queries + PM_SEARCH_QUERIES))
    pm_backfill_ids = _pm_backfill_ids(
        entries, int(env("RADAR_PM_BACKFILL_COMPANIES", "200")))
    jobs, ok, fail = [], 0, 0
    healthy_boards: set[str] = set()

    def one(entry: dict) -> list[Job]:
        fn = FETCHERS[entry["ats"]]
        if entry["ats"] in PM_BACKFILL_ATS:
            queries = pm_queries if id(entry) in pm_backfill_ids else wd_queries
            return fn(entry, queries)
        return fn(entry)

    with ThreadPoolExecutor(max_workers=int(env("RADAR_WORKERS", "12"))) as ex:
        futs = {ex.submit(one, e): e for e in entries}
        for fut in as_completed(futs):
            entry = futs[fut]
            try:
                got = fut.result()
                discovery.record_result(entry, True)
                registry_key = discovery.key(
                    entry["ats"], entry["token"], entry.get("extra")
                )
                board = f"ats:{registry_key}"
                # direct-ATS jobs inherit the registry's sector knowledge
                for j in got:
                    j.sector = entry.get("sector", "")
                    j.source_board = board
                jobs.extend(got)
                healthy_boards.add(board)
                ok += 1
            except Exception:
                discovery.record_result(entry, False)
                fail += 1
    return jobs, {
        "companies_polled": len(entries), "ok": ok, "failed": fail,
        "selected_oldest_age_s": (
            max(0, selection_now - min(selected_poll_times))
            if selected_poll_times else 0
        ),
        "active_unpolled": active_unpolled,
    }, healthy_boards


def scrub_glyph_companies(jobs_state: dict) -> int:
    """Drop records whose employer is a scraped continuation glyph ("↳",
    pre-2026-07 jobright parses) — unidentifiable, and their re-crawled twins
    exist under the real company name (different id). Runs every crawl;
    idempotent and cheap once the backlog is gone."""
    corrupt = [k for k, r in jobs_state.items()
               if r.get("company", "").strip() in {"↳", "&#8627;", "&#x21B3;", ""}]
    for k in corrupt:
        del jobs_state[k]
    return len(corrupt)


def crawl() -> int:
    if profile_id() == "internship":
        from .internship_radar import crawl as internship_crawl
        return internship_crawl()
    t0 = time.time()
    now = int(t0)
    p = profile()
    disabled = {s.strip() for s in env("RADAR_DISABLE_SOURCES", "").split(",") if s.strip()}

    registry = state.companies()
    discovery.seed_registry(registry, seeds())
    jobs_state = state.jobs()
    applied = state.applied()
    jobs_state, _ = _repair_duplicate_job_state(jobs_state, applied)
    dropped_glyphs = scrub_glyph_companies(jobs_state)
    if dropped_glyphs:
        print(f"hygiene: dropped {dropped_glyphs} glyph-company record(s)")
    n_regated = regate(jobs_state)
    if n_regated:
        print(f"re-gate: rules v{RULES_VERSION} flipped alert_ok on {n_regated} stored job(s)")
    fb = state.feedback()
    preference_profile = build_preference_profile(applied, jobs_state)
    score_preferences = load_score_preferences()
    print(f"preferences: learned from {preference_profile['sample_count']} saved/applied role(s)")
    seed_sectors = {norm(s["name"]): s.get("sector", "other") for s in seeds()}

    from . import culture
    culture.write_outputs()  # sync curated dossiers before scoring reads them

    agg_jobs, agg_stats, healthy_aggregator_boards = _fetch_aggregators(disabled)
    print(f"aggregators: {agg_stats}")

    harvested = discovery.harvest(registry, agg_jobs, max_new=int(env("RADAR_MAX_HARVEST", "200")))
    activated, invalidated = discovery.probe_new(registry, budget=int(env("RADAR_PROBE_BUDGET", "40")))
    print(f"discovery: harvested {harvested} new company candidates, "
          f"probed → {activated} active / {invalidated} invalid "
          f"(registry: {len(registry)})")

    ats_jobs, ats_stats, healthy_ats_boards = _fetch_ats(registry, disabled)
    print(f"ats: {ats_stats}")

    # ---- normalize, dedupe, score ----
    new_jobs: list[Job] = []
    # A Pipeline manual entry can precede discovery of the exact same official
    # ATS posting. Keep its tracking marker and stable ID, but replace the
    # manual placeholder with official source/description/eligibility data.
    manual_upgrades: dict[str, dict] = {}
    dropped = 0
    discovered, feed_duplicates = _unique_discovered_jobs(agg_jobs + ats_jobs)
    if feed_duplicates:
        print(f"hygiene: ignored {feed_duplicates} duplicate feed sighting(s) this run")
    discovered, link_stats = _resolve_discovered_links(discovered, jobs_state, now)
    if link_stats["attempted"]:
        print(f"link resolution: checked {link_stats['attempted']}, "
              f"promoted {link_stats['resolved']} direct link(s), "
              f"closed {link_stats['closed']} Jobright posting(s), "
              f"kept {link_stats['unchanged']} aggregator fallback(s), "
              f"errors/not found {link_stats['errors']}")
    # Resolution can turn two previously different aggregator URLs into one
    # direct URL, so run the cheap in-memory feed dedupe once more.
    discovered, resolved_feed_duplicates = _unique_discovered_jobs(discovered)
    if resolved_feed_duplicates:
        print(f"hygiene: ignored {resolved_feed_duplicates} duplicate sighting(s) after link resolution")
    existing_url_ids = _existing_posting_ids(jobs_state)
    seen_this_run: set[str] = set()

    def _prepared_existing_sighting(job: Job) -> tuple[dict, bool, bool, bool, list[str]]:
        """Evaluate fresh employer text before serializing an existing row.

        State records intentionally omit descriptions.  Running the
        deterministic gate first ensures that a newly fetched ATS posting can
        replace missing/stale evidence even when this sighting is not itself a
        new alert candidate.  The marker is ephemeral and never persisted;
        it only authorizes ``_merge_record_sighting`` to replace compact
        eligibility facts.
        """
        source_only = (job.link_resolution or {}).get("status") == "source_only"
        fresh_employer = bool(
            not source_only and (
                bool(job.description) or
                (bool(job.posting) and _has_independent_employer_evidence(job))
            )
        )
        keep = True
        alert_eligible = False
        reasons: list[str] = []
        if fresh_employer:
            if not job.sector:
                job.sector = infer(job.company, seed_sectors)
            keep, alert_eligible, reasons = gates(job)
        sighting = job.to_record()
        if fresh_employer:
            sighting["_fresh_employer_evidence"] = True
        return sighting, fresh_employer, keep, alert_eligible, reasons

    for j in discovered:
        if not j.company or not j.title or not j.url:
            continue
        jid = _state_job_identity(j, jobs_state, existing_url_ids)
        link_result = j.link_resolution or {}
        if link_result.get("status") == "closed":
            # Do this before ``touch``: touch deliberately reopens any
            # terminal role that reappears in a feed, while this page signal
            # is the stronger, same-run liveness verdict.
            existing = jobs_state.get(jid)
            if existing is not None:
                if (link_resolver.is_aggregator_url(j.url) and
                        not link_resolver.is_aggregator_url(existing.get("url"))):
                    # A closed fallback page is provenance, not employer
                    # liveness. Keep it as an alternate URL without closing
                    # an official record that may still be accepting apps.
                    _merge_record_sighting(existing, _prepared_existing_sighting(j)[0])
                else:
                    existing["link_resolution"] = dict(link_result)
                    lifecycle.mark_terminal(
                        existing,
                        link_result.get("posting_status") or lifecycle.EXPIRED,
                        now,
                        link_result.get("reason") or "Jobright page says the posting is closed",
                    )
                seen_this_run.add(jid)
            continue
        existing_url_id = existing_url_ids.get(_posting_identity(j.url))
        if existing_url_id and existing_url_id != jid:
            # The durable state already has this URL under a title/location
            # variant. Keep that identity instead of reintroducing a duplicate
            # on every crawl; the next lifecycle/scoring pass still refreshes
            # the surviving record.
            existing = jobs_state[existing_url_id]
            lifecycle.touch(existing, now, j.source or "monitored source")
            _merge_record_sighting(existing, _prepared_existing_sighting(j)[0])
            seen_this_run.add(existing_url_id)
            continue
        existing = jobs_state.get(jid)
        if jid in seen_this_run:
            # ``_unique_discovered_jobs`` normally handles this before the
            # crawl loop. Keep a defensive state merge for edge cases without
            # replacing an already-scored in-memory job with a raw sighting.
            if existing is not None:
                _merge_record_sighting(existing, _prepared_existing_sighting(j)[0])
            continue
        seen_this_run.add(jid)
        if existing is not None:
            # A feed sighting can reopen an automatically closed record. The
            # liveness fetch below still has authority to close it again.
            lifecycle.touch(existing, now, j.source or "monitored source")
        prepared_keep = True
        prepared_alert_eligible = False
        prepared_reasons: list[str] = []
        prepared = False
        if existing is not None and existing.get("source") != "manual":
            sighting, prepared, prepared_keep, prepared_alert_eligible, prepared_reasons = (
                _prepared_existing_sighting(j)
            )
            _merge_record_sighting(existing, sighting)
            resolved_upgrade = (
                bool(j.link_resolution.get("status") == "resolved") and
                not link_resolver.is_aggregator_url(j.url) and
                (link_resolver.is_aggregator_url(existing.get("url")) or
                 _posting_identity(j.url) != _posting_identity(existing.get("url")))
            )
            if not resolved_upgrade:
                continue
        if prepared:
            keep, alert_eligible, reasons = (
                prepared_keep, prepared_alert_eligible, prepared_reasons
            )
        else:
            if not j.sector:
                j.sector = infer(j.company, seed_sectors)  # before gates: priority-sector path
            keep, alert_eligible, reasons = gates(j)
        if not keep:
            dropped += 1
            continue
        j.alert_ok = alert_eligible
        score(j, fb, now, preference_profile, score_preferences)
        j.score_reasons += reasons
        lifecycle.touch(j, now, j.source or "monitored source")
        if existing is not None:
            manual_upgrades[jid] = existing
        new_jobs.append(j)

    # ---- posting scrape: real description text, no LLM needed ----
    from .posting import scrape_pass
    eightfold_domains = {norm(e.get("name", "")): (e.get("extra") or {}).get("domain")
                         for e in registry.values()
                         if e.get("ats") == "eightfold" and (e.get("extra") or {}).get("domain")}
    pstats = scrape_pass(new_jobs, jobs_state, eightfold_domains, now)
    if pstats:
        print(f"posting scrape: {pstats['inline']} inline, {pstats['fetched']} fetched, "
              f"{pstats['demoted']} demoted, {pstats['closed']} closed "
              f"({pstats.get('filled', 0)} filled), "
              f"{pstats.get('research_sources', 0)} research sources")

    # ---- optional LLM pass on borderline/alert candidates ----
    thr = p["thresholds"]
    band = p["llm"]["rerank_band"]
    borderline = sorted((j for j in new_jobs if j.alert_ok and j.score >= thr["alert"] - band),
                        key=lambda j: (-career_priority(j), -j.score))
    rerank(borderline)

    # ---- decide alerts ----
    max_age = thr["max_posting_age_days"] * 86400
    candidates = [j for j in new_jobs
                  if j.alert_ok and j.score >= thr["alert"]
                  and not lifecycle.is_terminal(j)
                  and (j.posted_at is None or now - j.posted_at <= max_age)]
    candidates.sort(key=lambda j: (-career_priority(j), -j.score))
    max_alerts = int(env("RADAR_MAX_ALERTS", "25"))
    alerts = candidates[:max_alerts]

    # ---- persist ----
    for j in new_jobs:
        jid = _job_identity(j)
        rec = j.to_record()
        rec["id"] = jid
        old_record = jobs_state.get(jid)
        old_manual = manual_upgrades.get(jid)
        rec["first_seen"] = (
            (old_manual or old_record or {}).get("first_seen", now)
        )
        if old_manual:
            rec["manual_added"] = True
        if old_record:
            _merge_record_sighting(rec, old_record)
            for key in ("posting", "quality", "company_research", "posting_family_id",
                        "posting_identity"):
                if old_record.get(key) and not rec.get(key):
                    rec[key] = old_record[key]
        lifecycle.merge_record_metadata(rec, old_record)
        if old_record and old_record.get("manual_archived"):
            rec["manual_archived"] = True
            rec["closed_at"] = old_record.get("closed_at", now)
            rec["archived_at"] = old_record.get("archived_at", rec["closed_at"])
            rec["archived_by"] = old_record.get("archived_by", "owner")
            rec["archive_reason"] = old_record.get("archive_reason", "owner archived")
        rec["rules_v"] = RULES_VERSION
        rec["eligibility_evidence"] = j.eligibility_evidence
        rec["explicit_new_grad"] = explicit_new_grad(j.title, evidence=j.eligibility_evidence)
        rec["early_career_possible"] = early_career_possible(j, rec.get("posting"))
        if rec["early_career_possible"] and not rec.get("career_priority"):
            rec["career_priority"] = 1
        jobs_state[jid] = rec
        for url in [j.url, *rec.get("alternate_urls", [])]:
            identity = _posting_identity(url)
            if identity:
                existing_url_ids[identity] = jid
    lifecycle_stats = lifecycle.reconcile(
        jobs_state, now, seen_this_run,
        healthy_source_boards=healthy_aggregator_boards | healthy_ats_boards,
    )
    if lifecycle_stats["expired"] or lifecycle_stats["reopened"]:
        print(f"lifecycle: {lifecycle_stats['expired']} auto-expired, "
              f"{lifecycle_stats['reopened']} reopened from current source")
    cutoff = lifecycle.history_cutoff(now)
    jobs_state = {k: v for k, v in jobs_state.items() if v.get("first_seen", now) >= cutoff}

    # The equation is not only for newly discovered rows. Rebuild every active
    # stored posting before generated state is published so Vercel, dashboard,
    # alerts, and the Mac companion all see the same current ranking.
    full_rescored, current_alerts = _rebuild_scores(
        jobs_state, fb, now, preference_profile)
    print(f"scoring: rebuilt {full_rescored} active posting(s) with rules v{RULES_VERSION}; "
          f"{current_alerts} currently alert-eligible")

    from . import company_research
    research_records = []
    for j in new_jobs:
        record = j.to_record()
        record["id"] = _job_identity(j)
        research_records.append(record)
    researched_companies = company_research.prepare_for_jobs(
        research_records, limit=int(env("RADAR_CRAWL_WEB_RESEARCH_LIMIT", "6")))
    if researched_companies:
        print(f"company research: captured external sources for {researched_companies} new companies")
    from . import llm
    if llm.available("company_research"):
        synthesized = company_research.enrich(
            jobs_state, applied, state.load("web_state.json", {}),
            limit=int(env("RADAR_CRAWL_COMPANY_RESEARCH_LIMIT", "2")))
        if synthesized:
            print(f"company research: synthesized {synthesized} new company brief(s)")

    alert_history = state.load("alert_history.json", [])
    for j in alerts:
        rec = j.to_record()
        rec["id"] = _job_identity(j)
        rec["alerted_at"] = now
        alert_history.append(rec)
    alert_history = alert_history[-500:]

    runs = state.load("runs.json", [])
    runs.append({"ts": now, "took_s": round(time.time() - t0, 1),
                 "new_jobs": len(new_jobs), "dropped_by_gates": dropped,
                 "alerts": len(alerts), "aggregators": agg_stats, "ats": ats_stats,
                 "registry_size": len(registry)})
    runs = runs[-300:]

    state.save("companies.json", registry)
    state.save("jobs.json", jobs_state)
    from .notion_sync import archive_terminal_pages
    archived_notion = archive_terminal_pages(applied, jobs_state)
    if archived_notion:
        state.save("applied.json", applied)
        print(f"lifecycle: archived {archived_notion} terminal tracked page(s) in Notion")
    state.save("alert_history.json", alert_history)
    state.save("runs.json", runs)

    write_outputs(jobs_state, registry, runs, alert_history)

    # Workflow crawls publish generated state before making external GitHub
    # writes.  A rejected state push can then be rebuilt from fresh upstream
    # without duplicated issues or a master board that points at uncommitted
    # jobs.  Local/manual crawls retain the convenient immediate delivery.
    deferred_delivery = env("RADAR_DEFER_DELIVERY", "").lower() in {"1", "true", "yes"}
    url = None
    if deferred_delivery:
        print("crawl: delivery deferred until state publication succeeds")
    else:
        url = deliver_alerts()
    print(f"crawl done in {time.time() - t0:.1f}s: {len(new_jobs)} new jobs "
          f"({dropped} gated out), {len(alerts)} alerts"
          + (f" → {url}" if url else ""))
    return 0


def deliver_alerts() -> str | None:
    """Idempotently publish recent tracking issues and refresh the master board.

    This intentionally does not write generated state.  It is safe to invoke
    after every successful crawl, or to replay after an interrupted delivery.
    """
    now = int(time.time())
    history = state.load("alert_history.json", [])
    jobs = state.jobs()
    aliases = state.load("job_aliases.json", {})
    from .dedupe import resolve_alias
    threshold = profile()["thresholds"]["alert"]
    recent = []
    for history_record in history:
        alerted_at = int(history_record.get("alerted_at", 0) or 0)
        if alerted_at < now - 14 * 86400:
            continue
        job_id = resolve_alias(
            str(history_record.get("id") or ""), aliases, existing=jobs)
        current = jobs.get(job_id)
        if (current is None or lifecycle.is_terminal(current) or
                not current.get("alert_ok") or
                int(current.get("score") or 0) < threshold):
            continue
        hydrated = dict(current)
        hydrated["id"] = job_id
        hydrated["alerted_at"] = alerted_at
        recent.append(hydrated)
    errors = []
    url = None
    try:
        from .alerts import post_alerts
        url = post_alerts(recent, jobs_state=jobs)
    except Exception as e:
        print(f"alerts: failed to post issue: {e}")
        errors.append(("alerts", e))
    try:
        from .board import update_master_board
        update_master_board(jobs, state.applied())
    except Exception as e:
        print(f"board: failed to update master board: {e}")
        errors.append(("board", e))
    if errors:
        details = "; ".join(f"{name}: {error}" for name, error in errors)
        raise RuntimeError(f"delivery failed ({details})")
    print(f"delivery: checked {len(recent)} recent alert(s)" + (f" → {url}" if url else ""))
    return url


def seed_cmd() -> int:
    registry = state.companies()
    added = discovery.seed_registry(registry, seeds())
    state.save("companies.json", registry)

    # Taste seed: companies from the user's real Notion application history.
    history = ["amazon", "nvidia", "bank of america", "oracle", "pinterest",
               "capital one", "verizon", "bny", "adobe", "ixl", "microsoft",
               "commure", "google", "adp", "dv trading", "mastercard",
               "openai", "chase"]
    fb = state.feedback()
    for comp in history:
        fb["company_boosts"][comp] = max(fb["company_boosts"].get(comp, 0), 4)
    state.save("feedback.json", fb)
    print(f"seeded {added} companies, {len(history)} taste priors")
    return 0


def notion_backfill() -> int:
    from .notion_sync import archive_terminal_pages, sync_applied, sync_from_notion
    applied = state.applied()
    jobs = state.jobs()
    pulled = sync_from_notion(applied)
    n = sync_applied(applied)
    archived = archive_terminal_pages(applied, jobs)
    state.save("applied.json", applied)
    state.save("autopilot_choices.json", applied_mod.autopilot_choices(applied, jobs))
    print(f"notion-backfill: pulled {pulled} stage change(s), pushed {n}, "
          f"archived {archived} terminal page(s)")
    return 0


def tracker_sync() -> int:
    """Continuously reconcile local tracker state in both directions.

    This is intentionally separate from issue events: Notion edits and missed
    webhook runs should converge on their own within one scheduled cycle.
    """
    from .notion_sync import archive_terminal_pages, sync_applied, sync_from_notion

    applied = state.applied()
    jobs = state.jobs()
    pulled = sync_from_notion(applied)
    pushed = sync_applied(applied)
    archived = archive_terminal_pages(applied, jobs)
    state.save("applied.json", applied)
    state.save("autopilot_choices.json", applied_mod.autopilot_choices(applied, jobs))
    print(
        f"tracker-sync: pulled {pulled} stage change(s), pushed {pushed}, "
        f"archived {archived} terminal/duplicate page(s)"
    )
    return 0


def resolve_links_cmd() -> int:
    """Bounded backfill for aggregator URLs already present in state.

    Normal crawls do this opportunistically. This command is the explicit
    repair knob when Victor wants to accelerate an existing backlog without
    making an unbounded number of third-party requests. Network resolution is
    parallel, but state mutation stays sequential so a slow or failed request
    cannot leave a half-written record behind.
    """
    now = int(time.time())
    jobs_state = state.jobs()
    terminal_statuses = {lifecycle.EXPIRED, lifecycle.FILLED}
    candidates = [
        (jid, record) for jid, record in jobs_state.items()
        if link_resolver.is_aggregator_url(record.get("url"))
        and record.get("posting_status", lifecycle.OPEN) not in terminal_statuses
        and link_resolver.needs_resolution(record, record.get("url", ""), now)
    ]
    candidates.sort(key=lambda pair: (
        1 if pair[1].get("alert_ok") else 0,
        1 if not pair[1].get("link_resolution") else 0,
        int(pair[1].get("score") or 0),
        int(pair[1].get("last_seen_at") or pair[1].get("first_seen") or 0),
    ), reverse=True)
    limit = max(0, int(env("RADAR_LINK_RESOLVE_LIMIT", "200")))
    workers = max(1, min(32, int(env("RADAR_LINK_RESOLVE_WORKERS", "12"))))

    def prepare(candidate: tuple[str, dict]):
        jid, record = candidate
        job = Job(
            company=record.get("company", ""), title=record.get("title", ""),
            url=record.get("url", ""), source=record.get("source", ""),
            source_url=record.get("source_url", ""),
            locations=record.get("locations", []), ats=record.get("ats", ""),
            posted_at=record.get("posted_at"),
        )
        job.source_variants = list(record.get("source_variants") or [])
        job.source_url_variants = list(record.get("source_url_variants") or [])
        job.alternate_urls = list(record.get("alternate_urls") or [])
        job.link_resolution = dict(record.get("link_resolution") or {})
        result = link_resolver.resolve_job(job, existing=record, now=now)
        return jid, record, job, result

    resolved_results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(prepare, candidate) for candidate in candidates[:limit]]
        for future in as_completed(futures):
            try:
                resolved_results.append(future.result())
            except Exception as exc:
                # Keep one unexpected parser/transport failure from cancelling
                # the entire maintenance batch. The normal resolver already
                # turns expected HTTP failures into an auditable result.
                resolved_results.append((None, None, None, {
                    "status": "error", "error": type(exc).__name__,
                }))

    attempted = resolved = closed = unchanged = errors = 0
    for _jid, record, job, result in resolved_results:
        if record is None or job is None:
            errors += 1
            continue
        attempted += 1
        if result.get("status") == "closed":
            record["link_resolution"] = result
            lifecycle.mark_terminal(
                record,
                result.get("posting_status") or lifecycle.EXPIRED,
                now,
                result.get("reason") or "Jobright page says the posting is closed",
            )
            closed += 1
        elif result.get("status") == "resolved":
            record["url"] = job.url
            if job.ats:
                record["ats"] = job.ats
            _merge_record_sighting(record, job.to_record())
            resolved += 1
        elif result.get("status") in {"error", "not_found"}:
            record["link_resolution"] = result
            errors += 1
        else:
            record["link_resolution"] = result
            unchanged += 1

    applied = state.applied()
    jobs_state, repaired = _repair_duplicate_job_state(jobs_state, applied)
    state.save("jobs.json", jobs_state)
    if repaired:
        state.save("applied.json", applied)
    write_outputs(jobs_state, state.companies(), state.load("runs.json", []),
                  state.load("alert_history.json", []))
    print(f"link-resolve: checked {attempted}, promoted {resolved}, closed {closed}, kept {unchanged}, "
          f"errors/not found {errors}; repair={'yes' if repaired else 'no'}")
    return 0


def lifecycle_cmd() -> int:
    """Reconcile stale postings without running source discovery.

    This is the safe manual repair/backfill command; scheduled crawls run the
    same reconciliation automatically after source sightings and liveness
    checks.
    """
    now = int(time.time())
    jobs_state = state.jobs()
    # A repair command has no fresh per-board evidence. It can normalize and
    # archive definitive closures, but must never infer source-gap expiry.
    stats = lifecycle.reconcile(
        jobs_state, now, set(), allow_source_gap_expiry=False
    )
    cutoff = lifecycle.history_cutoff(now)
    jobs_state = {
        jid: record for jid, record in jobs_state.items()
        if int(record.get("first_seen", now) or now) >= cutoff
    }
    applied = state.applied()
    from .notion_sync import archive_terminal_pages
    archived = archive_terminal_pages(applied, jobs_state)
    state.save("jobs.json", jobs_state)
    if archived:
        state.save("applied.json", applied)
    registry = state.companies()
    runs = state.load("runs.json", [])
    alert_history = state.load("alert_history.json", [])
    write_outputs(jobs_state, registry, runs, alert_history)
    print(f"lifecycle: {stats['expired']} expired, {stats['filled']} filled, "
          f"{stats['reopened']} reopened, {archived} Notion page(s) archived")
    return 0


def enrich() -> int:
    """Bounded AI enrichment, ordered by user value and safe to re-run.

    Pasted JDs and tracked roles go first, then a small batch of grounded
    company research and fresh job-quality checks. Legacy ungrounded culture
    generation is off by default. Deterministic scoring/output always remains
    authoritative.
    """
    from . import company_research, culture, llm, quality
    from .digest import write_outputs as digest_write
    if not llm.available():
        print("enrich: no LLM provider configured (set ANTHROPIC_API_KEY or "
              "LLM_BASE_URL, e.g. http://localhost:11434/v1 for Ollama) — nothing to do")
        return 0
    jobs_state = state.jobs()
    applied = state.applied()
    preference_profile = build_preference_profile(applied, jobs_state)
    web = state.load("web_state.json", {})
    now = int(time.time())

    # Explicit user input gets the first reserved calls. Unchanged paste hashes
    # cost nothing on later cycles.
    pasted = 0
    pasted_limit = int(env("RADAR_PASTED_LIMIT", "4"))
    for jid, workspace in (web.get("jobs") or {}).items():
        if pasted >= pasted_limit:
            break
        rec = jobs_state.get(jid)
        if rec and (workspace.get("jd") or "").strip():
            pasted += quality.verify_pasted(rec, workspace["jd"])
    if pasted:
        print(f"enrich: graded {pasted} pasted JD(s) from the platform")

    researched = company_research.enrich(jobs_state, applied, web)
    print(f"enrich: synthesized {researched} source-grounded company brief(s) via "
          f"{llm.provider()}")

    # Stop creating new model-memory culture guesses. Existing estimates stay
    # visible but no longer affect ranking; an explicit nonzero env value is a
    # backwards-compatibility escape hatch.
    made = culture.enrich_missing(limit=int(env("RADAR_ENRICH_LIMIT", "0")))
    if made:
        print(f"enrich: generated {made} legacy culture estimate(s)")

    # re-score recent jobs with the enriched culture data
    import radar.score as score_mod
    score_mod._CULTURE_CACHE = None  # force reload
    score_mod._COMPANY_RESEARCH_CACHE = None
    fb = state.feedback()
    score_preferences = score_mod.load_score_preferences()
    sponsorship_db = sponsorship.load()
    rescored = 0
    for rec in jobs_state.values():
        if now - rec.get("first_seen", 0) > 14 * 86400:
            continue
        j = Job(company=rec["company"], title=rec["title"], url=rec["url"],
                source=rec["source"], locations=rec.get("locations", []),
                posted_at=rec.get("posted_at"), salary=rec.get("salary", ""),
                remote=rec.get("remote", False), ats=rec.get("ats", ""),
                sector=rec.get("sector", ""), posting=rec.get("posting") or {},
                source_board=rec.get("source_board", ""),
                link_resolution=rec.get("link_resolution") or {},
                eligibility_evidence=rec.get("eligibility_evidence") or {})
        old = rec.get("score", 0)
        score(j, fb, now, preference_profile, score_preferences)
        rec["sector"] = j.sector
        rec["score_raw"] = j.score_raw
        rec["score_calibrated"] = j.score_calibrated
        rec["evidence_score"] = j.evidence_score
        rec["eligibility"] = j.eligibility
        rec["priority_tier"] = j.priority_tier
        rec["score_dimensions"] = j.score_dimensions
        rec["score_dimensions_raw"] = j.score_dimensions_raw
        rec["score_version"] = RULES_VERSION
        rec["score_reasons"] = j.score_reasons
        rec["eligibility_evidence"] = j.eligibility_evidence
        rec["explicit_new_grad"] = explicit_new_grad(j.title, evidence=j.eligibility_evidence)
        if j.startup_stage == "unknown":
            for key in ("startup_stage", "startup_score", "startup_stage_evidence",
                        "startup_stage_reason"):
                rec.pop(key, None)
        else:
            rec["startup_stage"] = j.startup_stage
            rec["startup_score"] = j.startup_score
            rec["startup_stage_evidence"] = j.startup_stage_evidence
            rec["startup_stage_reason"] = j.startup_stage_reason
        if j.career_priority:
            rec["career_priority"] = j.career_priority
        else:
            rec.pop("career_priority", None)
        sponsorship.annotate_record(rec, sponsorship_db)
        if j.score != old:
            rec["score"] = j.score
            rescored += 1

    # quality pass: link liveness + new-grad/role-fit verification (cached
    # verdicts re-applied first, since score() above rebuilds from scratch)
    registry = state.companies()
    eightfold_domains = {norm(e.get("name", "")): (e.get("extra") or {}).get("domain")
                         for e in registry.values()
                         if e.get("ats") == "eightfold" and (e.get("extra") or {}).get("domain")}
    priority_ids = {a.get("id") for a in applied if a.get("id")}
    priority_ids |= set(web.get("jobs") or {})
    default_quality_limit = "25" if llm.provider() == "local" else "6"
    quality_limit = int(env("RADAR_QUALITY_LIMIT", default_quality_limit))
    reapplied, verified = quality.run(jobs_state, limit=quality_limit,
                                      domains=eightfold_domains,
                                      priority_ids=priority_ids)
    print(f"enrich: quality pass re-applied {reapplied} verdict(s), "
          f"verified {verified} new job(s)")

    from .notion_sync import archive_terminal_pages
    archived_notion = archive_terminal_pages(applied, jobs_state)
    if archived_notion:
        state.save("applied.json", applied)
        print(f"enrich: archived {archived_notion} terminal tracked page(s) in Notion")

    state.save("jobs.json", jobs_state)
    registry = state.companies()
    runs = state.load("runs.json", [])
    alert_history = state.load("alert_history.json", [])
    digest_write(jobs_state, registry, runs, alert_history)
    culture.write_outputs()
    print(f"enrich: re-scored {rescored} recent job(s), docs refreshed")

    # weekly research pass: healthcare/wearables employers are easy for
    # aggregators to miss (WHOOP was), so the local model scouts candidates
    # for the crawl's live probe to validate
    scout = state.load("scout.json", {"last_run": 0})
    if now - scout.get("last_run", 0) >= 7 * 86400:
        from . import discovery as disc
        registry = state.companies()
        found = disc.llm_scout(registry)
        state.save("companies.json", registry)
        scout["last_attempt"] = now
        if found:
            scout["last_run"] = now
        state.save("scout.json", scout)
        print(f"enrich: scout queued {found} company candidate(s) for probing")

    # monthly registry hygiene: retry dead boards, prune stale invalids,
    # park duplicate employer entries (no LLM involved)
    if now - scout.get("last_hygiene", 0) >= 30 * 86400:
        from . import discovery as disc
        registry = state.companies()
        stats = disc.hygiene(registry, now)
        state.save("companies.json", registry)
        scout["last_hygiene"] = now
        state.save("scout.json", scout)
        print(f"enrich: registry hygiene — {stats['dead_retried']} dead retried, "
              f"{stats['invalid_pruned']} stale invalids pruned, "
              f"{stats['dups_parked']} duplicate entries parked")
    usage = llm.save_usage()
    print(f"enrich: AI budget used {usage['logical_calls']}/{usage['limits']['logical_calls']} "
          f"logical calls, {usage['requests']}/{usage['limits']['requests']} provider requests")
    return 0


def migrate_checkbox_applied() -> int:
    """One-time fix: checkbox ticks used to mean 'applied' and got synced to
    Notion as such. They actually meant 'shortlisted'. Archive those Notion
    pages and move the entries into shortlist.json instead. Idempotent: only
    touches entries still tagged via='issue-checkbox' in applied.json."""
    from .notion_sync import archive_page, page_id_from_url
    token = env("NOTION_TOKEN")
    applied = state.applied()
    shortlist = state.shortlist()
    keep, moved, archive_failed = [], 0, []
    for entry in applied:
        if entry.get("via") != "issue-checkbox":
            keep.append(entry)
            continue
        page_id = page_id_from_url(entry.get("notion_page", ""))
        if page_id and token:
            try:
                archive_page(token, page_id)
            except Exception as e:
                archive_failed.append(entry["company"])
                print(f"migrate: could not archive Notion page for {entry['company']}: {e}")
        shortlist.append({
            "id": entry["id"], "company": entry["company"], "title": entry["title"],
            "url": entry.get("url", ""), "locations": entry.get("locations", []),
            "score": entry.get("score"), "source": entry.get("source"),
            "shortlisted_at": entry.get("applied_at", int(time.time())),
        })
        moved += 1
    state.save("applied.json", keep)
    state.save("shortlist.json", shortlist)
    print(f"migrate: moved {moved} checkbox-applied entries back to shortlist"
         + (f" ({len(archive_failed)} Notion pages could not be archived: {archive_failed})"
            if archive_failed else " (all Notion pages archived)"))
    return 0


def promote_shortlist_applications() -> int:
    """One-time correction for the shortlisting detour.

    Every existing checkbox selection becomes a tracked entry: a Notion page
    is created with the not-yet-applied status, and Victor advances the ones
    he actually applied to inside Notion. Idempotent through
    ``record_applied``.
    """
    applied = state.applied()
    shortlist = state.shortlist()
    fb = state.feedback()
    moved = 0
    for entry in shortlist:
        moved += applied_mod.record_applied(entry, applied, fb,
                                            via="checkbox-migration", stage="saved")
    from .notion_sync import sync_applied
    synced = sync_applied(applied)
    state.save("applied.json", applied)
    state.save("shortlist.json", [])
    state.save("feedback.json", fb)
    print(f"promote-shortlist: moved {moved} checkbox selection(s), synced {synced} to Notion")
    return 0


def marquee_backfill() -> int:
    """Compatibility alias for the old marquee-only repair command.

    Marquee employers are competitive context now, not a gate bypass. A full
    rescore is the safe repair for records created under the old policy.
    """
    return rescore_cmd()


def regate_cmd() -> int:
    """Apply the current gate rules (score.RULES_VERSION) to stored jobs.

    The scheduled crawl does this automatically; the command exists for
    local dry runs (point RADAR_STATE_DIR at a scratch copy) and manual
    repairs. Rewrites jobs.json and the generated docs.
    """
    if profile_id() == "internship":
        from .internship_radar import regate as internship_regate
        return internship_regate()
    jobs_state = state.jobs()
    flipped = regate(jobs_state)
    applied = state.applied()
    from .notion_sync import archive_terminal_pages
    archived = archive_terminal_pages(applied, jobs_state)
    state.save("jobs.json", jobs_state)
    if archived:
        state.save("applied.json", applied)
    registry = state.companies()
    runs = state.load("runs.json", [])
    alert_history = state.load("alert_history.json", [])
    write_outputs(jobs_state, registry, runs, alert_history)
    demoted = sum(1 for r in jobs_state.values()
                  if any(f"re-gate v{RULES_VERSION}" in s and "dashboard only" in s
                         for s in r.get("score_reasons", [])))
    print(f"regate: rules v{RULES_VERSION} flipped {flipped} job(s) "
          f"({demoted} demoted to dashboard), archived {archived} terminal "
          f"Notion page(s), docs refreshed")
    return 0


def rescore_cmd() -> int:
    """Rebuild every stored job score and eligibility decision in place.

    Unlike ``regate``, this is a deliberate full-board repair after profile or
    scoring-priority changes. It preserves the posting and quality evidence,
    then reapplies their audited demotions after deterministic scoring.
    """
    if profile_id() == "internship":
        from .internship_radar import rescore as internship_rescore
        return internship_rescore()
    jobs_state = state.jobs()
    fb = state.feedback()
    preference_profile = build_preference_profile(state.applied(), jobs_state)
    now = int(time.time())
    changed, alerts = _rebuild_scores(jobs_state, fb, now, preference_profile)
    applied = state.applied()
    from .notion_sync import archive_terminal_pages
    archived = archive_terminal_pages(applied, jobs_state)

    state.save("jobs.json", jobs_state)
    if archived:
        state.save("applied.json", applied)
    registry = state.companies()
    runs = state.load("runs.json", [])
    alert_history = state.load("alert_history.json", [])
    write_outputs(jobs_state, registry, runs, alert_history)
    print(f"rescore: rebuilt {changed} stored jobs; {alerts} currently alert-eligible; "
          f"archived {archived} terminal Notion page(s); docs refreshed")
    return 0


def score_health_cmd() -> int:
    """Fail loudly if generated jobs state is not covered by current scoring."""
    if profile_id() == "internship":
        internship_rules = INTERNSHIP_RULES_VERSION
        jobs_state = state.jobs()
        missing = [jid for jid, rec in jobs_state.items()
                   if rec.get("score_version") != internship_rules
                   or rec.get("rules_v") != internship_rules
                   or not isinstance(rec.get("score_reasons"), list)]
        if missing:
            print(f"score-health: FAIL {len(missing)} record(s) need internship rules v{internship_rules}; "
                  f"first ids: {', '.join(missing[:10])}")
            return 1
        print(f"score-health: PASS {len(jobs_state)} record(s) covered by internship rules v{internship_rules}")
        return 0
    jobs_state = state.jobs()
    missing = [jid for jid, rec in jobs_state.items()
               if rec.get("score_version") != RULES_VERSION
               or rec.get("rules_v") != RULES_VERSION
               or not isinstance(rec.get("score_reasons"), list)]
    if missing:
        print(f"score-health: FAIL {len(missing)} record(s) need rules v{RULES_VERSION}; "
              f"first ids: {', '.join(missing[:10])}")
        return 1
    print(f"score-health: PASS {len(jobs_state)} record(s) covered by rules v{RULES_VERSION}")
    return 0


def _rebuild_scores(jobs_state: dict, fb: dict, now: int,
                    preference_profile: dict | None = None) -> tuple[int, int]:
    """Apply the current deterministic equation to every active stored job."""
    import radar.score as score_mod

    from . import culture, posting, quality

    culture.write_outputs()
    score_mod._CULTURE_CACHE = None
    score_mod._CULTURE_MATCH_CACHE = {}
    score_mod._COMPANY_RESEARCH_CACHE = None
    score_preferences = score_mod.load_score_preferences()
    sponsorship_db = sponsorship.load()
    changed = 0
    alerts = 0
    for rec in jobs_state.values():
        # Company concentration is applied after these verdicts. Reset only
        # its own prior adjustment while retaining the freshly rebuilt score
        # as the source of truth for later ranking nudges.
        rec["ranking_adjustment"] = 0
        job = Job(company=rec.get("company", ""), title=rec.get("title", ""),
                  url=rec.get("url", ""), source=rec.get("source", ""),
                  locations=rec.get("locations", []), salary=rec.get("salary", ""),
                  remote=bool(rec.get("remote")), posted_at=rec.get("posted_at"),
                  ats=rec.get("ats", ""), sector=rec.get("sector", ""),
                  posting=rec.get("posting") or {},
                  source_board=rec.get("source_board", ""),
                  link_resolution=rec.get("link_resolution") or {},
                  eligibility_evidence=rec.get("eligibility_evidence") or {})
        keep, alert_eligible, gate_reasons = gates(job)
        score(job, fb, now, preference_profile, score_preferences)
        rec["sector"] = job.sector
        job.score_reasons += gate_reasons
        rec["score"] = job.score
        rec["score_raw"] = job.score_raw
        rec["score_calibrated"] = job.score_calibrated
        rec["evidence_score"] = job.evidence_score
        rec["eligibility"] = job.eligibility
        rec["priority_tier"] = job.priority_tier
        rec["score_dimensions"] = job.score_dimensions
        rec["score_dimensions_raw"] = job.score_dimensions_raw
        rec["score_reasons"] = job.score_reasons
        if job.startup_stage == "unknown":
            for key in ("startup_stage", "startup_score", "startup_stage_evidence",
                        "startup_stage_reason"):
                rec.pop(key, None)
        else:
            rec["startup_stage"] = job.startup_stage
            rec["startup_score"] = job.startup_score
            rec["startup_stage_evidence"] = job.startup_stage_evidence
            rec["startup_stage_reason"] = job.startup_stage_reason
        if job.career_priority:
            rec["career_priority"] = job.career_priority
        else:
            rec.pop("career_priority", None)
        rec["alert_ok"] = bool(keep and alert_eligible)
        rec["eligibility_evidence"] = job.eligibility_evidence
        rec["explicit_new_grad"] = explicit_new_grad(job.title, evidence=job.eligibility_evidence)
        rec["rules_v"] = RULES_VERSION
        rec["score_version"] = RULES_VERSION
        if rec.get("quality"):
            quality.reapply(rec)
        if rec.get("posting"):
            posting.reapply(rec)
        lifecycle.normalize_record(rec, now)
        sponsorship.annotate_record(rec, sponsorship_db)
        rec["early_career_possible"] = early_career_possible(job, rec.get("posting"))
        if rec["early_career_possible"] and not rec.get("career_priority"):
            rec["career_priority"] = 1
        if rec["early_career_possible"]:
            rec["score_reasons"].append(
                "early-career possible: no stated experience floor (not new-grad verified)")
        if rec.get("manual_archived"):
            rec["alert_ok"] = False
            line = f"owner archive: {rec.get('archive_reason', 'removed from active board')}"
            if line not in rec["score_reasons"]:
                rec["score_reasons"].append(line)
        if lifecycle.is_terminal(rec):
            rec["alert_ok"] = False
            line = (f"posting lifecycle: {lifecycle.status_of(rec)} — "
                    f"{lifecycle.lifecycle_reason(rec)}")
            if line not in rec["score_reasons"]:
                rec["score_reasons"].append(line)
        changed += 1
    apply_company_concentration(jobs_state)
    alerts = sum(
        (not lifecycle.is_terminal(rec)) and rec.get("alert_ok")
        and rec.get("score", 0) >= profile()["thresholds"]["alert"]
        for rec in jobs_state.values()
    )
    return changed, alerts


def sponsorship_refresh_cmd() -> int:
    """Refresh official DOL LCA history and rebuild the auditable score view."""
    from . import sponsorship as sponsorship_mod
    database = sponsorship_mod.build_alias_index(sponsorship_mod.refresh())
    state.save("sponsorship.json", database)
    print(
        "sponsorship: "
        f"{database['stats']['companies_with_certified_history']} companies with "
        f"certified DOL history across {', '.join(database['coverage_quarters'])}; "
        f"{database['stats']['rows_read']} rows read"
    )
    return rescore_cmd()


def create_google_tracker_cmd() -> int:
    """Create a new, formatted Google Sheets tracker without overwriting one."""
    from .google_sheets import create_tracker
    try:
        tracker = create_tracker()
    except Exception as exc:
        print(f"google-tracker: failed — {exc}")
        return 1
    print(f"google-tracker: created {tracker['title']}\n"
          f"  spreadsheet ID: {tracker['spreadsheet_id']}\n"
          f"  URL: {tracker['url']}\n"
          "  Set GOOGLE_SHEET_ID to this ID before syncing.")
    return 0


def rescrape_cmd() -> int:
    """Fetch full posting text for current visible jobs using free ATS/HTML APIs."""
    from . import posting
    jobs_state = state.jobs()
    registry = state.companies()
    domains = {norm(e.get("name", "")): (e.get("extra") or {}).get("domain")
               for e in registry.values()
               if e.get("ats") == "eightfold" and (e.get("extra") or {}).get("domain")}
    stats = posting.scrape_pass([], jobs_state, domains, int(time.time()),
                                budget=int(env("RADAR_RESCRAPE_LIMIT", "100")))
    state.save("jobs.json", jobs_state)
    applied = state.applied()
    from .notion_sync import archive_terminal_pages
    archived = archive_terminal_pages(applied, jobs_state)
    if archived:
        state.save("applied.json", applied)
    write_outputs(jobs_state, registry, state.load("runs.json", []),
                  state.load("alert_history.json", []))
    print(f"rescrape: fetched {stats.get('fetched', 0)}, unreadable {stats.get('unreadable', 0)}, "
          f"closed {stats.get('closed', 0)}, filled {stats.get('filled', 0)}, "
          f"demoted {stats.get('demoted', 0)}, Notion archived {archived}")
    return 0


def repair_feedback() -> int:
    """One-time repair: drop learned token boosts that FEEDBACK_STOPWORDS now
    filters at read time (business/marketing/product/... plus location noise).
    Documented repair per CLI_HANDOFF — feedback.json is otherwise generated."""
    from .score import FEEDBACK_STOPWORDS
    fb = state.feedback()
    tokens = fb.get("token_boosts", {})
    removed = sorted(t for t in tokens if t in FEEDBACK_STOPWORDS)
    for t in removed:
        del tokens[t]
    state.save("feedback.json", fb)
    print(f"repair-feedback: removed {len(removed)} stopworded token boost(s)"
          + (f": {', '.join(removed)}" if removed else ""))
    return 0


def web_action() -> int:
    """Handle a platform repository_dispatch.

    Track/applied use an existing radar record; manual-add creates a clearly
    labeled dashboard-only record before sending it through that same saved /
    Notion path. A manual role is never treated as new-grad alert evidence.
    """
    import json as _json
    path = env("GITHUB_EVENT_PATH")
    if not path:
        print("web-action: GITHUB_EVENT_PATH not set")
        return 1
    with open(path) as f:
        payload = _json.load(f).get("client_payload") or {}
    action = payload.get("action")
    supported_actions = {
        "track", "applied", "stage", "untrack", "manual-add",
        "research-company", "feedback", "archive", "score-preferences",
        "notification-preference",
    }
    if action not in supported_actions:
        print(f"web-action: unknown action {action!r}")
        return 0
    if action == "score-preferences":
        preferences = normalize_score_preferences(payload.get("preferences"))
        preferences["updated_at"] = int(time.time())
        state.save("score_preferences.json", preferences)
        print("web-action: score preferences saved; rebuilding the score board")
        return rescore_cmd()
    if action == "notification-preference":
        key = str(payload.get("key") or "").strip()
        if key not in {"new_grad_email", "internship_email"} or not isinstance(payload.get("enabled"), bool):
            print("web-action: invalid notification preference")
            return 1
        preferences = state.load_shared("notification_preferences.json", {
            "new_grad_email": True, "internship_email": True,
        })
        preferences[key] = payload["enabled"]
        preferences["updated_at"] = int(time.time())
        preferences["updated_by"] = "owner"
        state.save_shared("notification_preferences.json", preferences)
        print(f"web-action: notification {key}={payload['enabled']}")
        return 0
    jobs = state.jobs()
    hist = {a["id"]: a for a in state.load("alert_history.json", [])}
    job = jobs.get(payload.get("id")) or hist.get(payload.get("id")) \
        or next((j for j in jobs.values() if j.get("url") == payload.get("url")), None)
    applied = state.applied()
    shortlist = state.shortlist()
    fb = state.feedback()
    if action == "feedback":
        from . import taste
        job = jobs.get(payload.get("id")) or hist.get(payload.get("id")) \
            or next((j for j in jobs.values() if j.get("url") == payload.get("url")), None)
        if job is None:
            print(f"web-action: feedback job {payload.get('id')!r} not found")
            return 0
        changed = taste.record_feedback(
            fb, job, payload.get("vote"), payload.get("reason"), payload.get("description"))
        if changed:
            state.save("feedback.json", fb)
            report_path = taste.write_report(fb)
        else:
            report_path = None
        print(f"web-action: feedback {job['company']} — changed={changed}"
              + (f" ({report_path})" if report_path else ""))
        return 0
    if action == "archive":
        if job is None:
            print(f"web-action: archive job {payload.get('id')!r} not found")
            return 0
        reason = str(payload.get("reason") or "owner archived").strip()[:120]
        now = int(time.time())
        lifecycle.mark_terminal(job, lifecycle.status_from_reason(reason), now,
                                f"owner archive: {reason}")
        job.update({"manual_archived": True, "closed_at": now, "archived_at": now,
                    "archived_by": "owner", "archive_reason": reason, "alert_ok": False})
        line = f"owner archive: {reason}"
        reasons = job.setdefault("score_reasons", [])
        if line not in reasons:
            reasons.append(line)
        state.save("jobs.json", jobs)
        from .notion_sync import archive_terminal_pages
        archived_notion = archive_terminal_pages(applied, jobs)
        if archived_notion:
            state.save("applied.json", applied)
        print(f"web-action: archive {job['company']} — {reason}"
              f"; notion archived={archived_notion}")
        return 0
    untracked = set(state.load("untracked.json", []))
    if action == "untrack":
        changed = applied_mod.remove_tracking(payload.get("id") or payload.get("url", ""), applied, untracked)
        shortlist[:] = [s for s in shortlist if s.get("id") != payload.get("id")]
        state.save("applied.json", applied)
        state.save("shortlist.json", shortlist)
        state.save("untracked.json", sorted(untracked))
        print(f"web-action: untrack {payload.get('company')} — changed={changed}")
        return 0
    if action == "research-company":
        requested_ids = payload.get("ids") if isinstance(payload.get("ids"), list) else []
        requested_ids = [str(jid) for jid in requested_ids if str(jid)][:5]
        if payload.get("id") and payload["id"] not in requested_ids:
            requested_ids.insert(0, payload["id"])
        requested_ids = requested_ids[:5]
        selected = []
        for jid in requested_ids:
            candidate = jobs.get(jid) or hist.get(jid)
            if candidate and candidate not in selected:
                selected.append(candidate)
        if not selected and job is not None:
            selected = [job]
        if not selected:
            print(f"web-action: research job {payload.get('id')!r} not found")
            return 0
        # This action deliberately runs only in the owner-authorized workflow.
        # The browser dispatches a job ID; provider credentials stay in GitHub
        # Actions secrets and no public visitor can spend the research budget.
        from . import company_research, llm
        records = company_research.load()
        sources_changed = 0
        for index, selected_job in enumerate(selected):
            sources_changed += bool(company_research.prepare_external_sources(
                records, selected_job["company"], [selected_job.get("url", "")],
                [selected_job.get("source_url", "")], selected_job.get("sector", ""),
                # The clicked employer is refreshed; warmups reuse evidence
                # already collected by the normal crawler/backfill.
                force=index == 0))
        company_research.save(records)
        # The opened job wins the existing priority queue ahead of speculative
        # warmups, without persisting a fake user action in web state.
        web = state.load("web_state.json", {})
        web = {**web, "jobs": {**(web.get("jobs") or {}),
                                selected[0]["id"]: {"research_requested": True}}}
        synthesized = company_research.enrich(
            {selected_job["id"]: selected_job for selected_job in selected}, applied, web,
            limit=len(selected))
        llm.save_usage()
        print(f"web-action: research-company {selected[0]['company']} + {len(selected) - 1} prefetch — "
              f"sources_changed={sources_changed}, synthesized={synthesized}")
        return 0
    if action == "manual-add":
        company = str(payload.get("company") or "").strip()[:200]
        title = str(payload.get("title") or "").strip()[:240]
        url = str(payload.get("url") or "").strip()[:2000]
        location = str(payload.get("location") or "").strip()[:240]
        if not company or not title or not url.startswith(("https://", "http://")):
            print("web-action: invalid manual-add payload")
            return 1
        # URL lookup above makes a repeat click idempotent. Otherwise derive a
        # stable ID from employer/title/location just like crawler records.
        if job is None:
            seed_sectors = {norm(s["name"]): s.get("sector", "other") for s in seeds()}
            manual = Job(company=company, title=title, url=url, source="manual",
                         source_url=url, locations=[location] if location else [],
                         ats="greenhouse" if "greenhouse.io" in url else "",
                         profile=profile_id())
            manual.sector = infer(manual.company, seed_sectors)
            if profile_id() == "internship":
                from .internship import annotate as annotate_internship
                from .internship import gates as internship_gates
                from .internship import score as internship_score
                annotate_internship(manual)
                _, _, reasons = internship_gates(manual)
                internship_score(manual, int(time.time()))
            else:
                _, _, reasons = gates(manual)
                preference_profile = build_preference_profile(state.applied(), jobs)
                score(manual, fb, int(time.time()), preference_profile,
                      load_score_preferences())
            job = manual.to_record()
            rules_version = INTERNSHIP_RULES_VERSION if profile_id() == "internship" else RULES_VERSION
            job.update({
                "first_seen": int(time.time()), "rules_v": rules_version,
                "score_version": rules_version, "explicit_new_grad": False,
                "alert_ok": False, "manual_added": True,
                "score_reasons": job["score_reasons"] + reasons + [
                    "manual entry: user-added; never alert eligible"],
            })
            jobs[job["id"]] = job
            state.save("jobs.json", jobs)
        else:
            # An existing crawler record keeps its authoritative score and
            # provenance; a manual save simply sends it to the tracker.
            job["manual_added"] = True
            state.save("jobs.json", jobs)
    if job is None:
        print(f"web-action: job {payload.get('id')!r} not found")
        return 0
    if action == "stage":
        stage = str(payload.get("stage") or "").strip().lower()
        if stage not in {
            "maybe", "saved", "to_tailor", "applied", "oa", "interview",
            "rejected", "closed", "not_pursuing",
        }:
            print(f"web-action: unsupported stage {stage!r}")
            return 1
        untracked.discard(job["id"])
        existing = next((entry for entry in applied if entry.get("id") == job["id"]), None)
        if existing is None:
            changed = applied_mod.record_applied(job, applied, fb, via="platform", stage=stage)
            existing = next((entry for entry in applied if entry.get("id") == job["id"]), None)
        else:
            changed = existing.get("stage", "applied") != stage
            existing["stage"] = stage
            existing["via"] = "platform"
            if stage == "applied":
                existing["applied_at"] = int(time.time())
        if existing is not None and stage == "to_tailor":
            existing["tailored_at"] = int(payload.get("tailored_at") or existing.get("tailored_at") or time.time())
        if existing is not None and (changed or stage == "closed"):
            existing["stage_changed_at"] = int(time.time())
        from .notion_sync import sync_applied
        synced = sync_applied(applied)
        shortlist[:] = [s for s in shortlist if s["id"] != job["id"]]
        state.save("applied.json", applied)
        state.save("shortlist.json", shortlist)
        state.save("feedback.json", fb)
        state.save("untracked.json", sorted(untracked))
        print(f"web-action: stage {job['company']} → {stage} — changed={changed}, notion synced={synced}")
        return 0
    untracked.discard(job["id"])
    changed = applied_mod.record_applied(
        job, applied, fb, via="manual-platform" if action == "manual-add" else "platform",
        stage="saved" if action in {"track", "manual-add"} else "applied")
    from .notion_sync import sync_applied
    synced = sync_applied(applied)
    shortlist[:] = [s for s in shortlist if s["id"] != job["id"]]
    state.save("applied.json", applied)
    state.save("shortlist.json", shortlist)
    state.save("feedback.json", fb)
    state.save("untracked.json", sorted(untracked))
    print(f"web-action: {action} {job['company']} — changed={changed}, notion synced={synced}")
    return 0


def report_sync() -> int:
    """Record a community stale-posting report from a GitHub issue."""
    path = env("GITHUB_EVENT_PATH")
    if not path:
        print("report-sync: GITHUB_EVENT_PATH not set")
        return 1
    from . import reports
    return reports.handle_event(path)


def main() -> None:
    ap = argparse.ArgumentParser(prog="radar")
    ap.add_argument("command", choices=["crawl", "applied-sync", "seed", "notion-backfill",
                                        "tracker-sync",
                                        "strategist", "notion-verify", "email-watch", "email-verify",
                                        "migrate-checkbox-applied", "promote-shortlist",
                                        "marquee-backfill", "reconcile-checkboxes",
                                        "daily-best", "master-board", "deliver-alerts", "web-action", "enrich",
                                        "email-batch",
                                        "regate", "rescore", "lifecycle", "score-health",
                                        "rescrape", "resolve-links", "repair-feedback",
                                        "taste-report", "report-sync",
                                        "sponsorship-refresh", "create-google-tracker"])
    args = ap.parse_args()
    if args.command == "crawl":
        sys.exit(crawl())
    elif args.command == "applied-sync":
        applied_mod.main()
    elif args.command == "seed":
        sys.exit(seed_cmd())
    elif args.command == "notion-backfill":
        sys.exit(notion_backfill())
    elif args.command == "tracker-sync":
        sys.exit(tracker_sync())
    elif args.command == "strategist":
        from .strategist import post_memo
        url = post_memo()
        print(f"strategist: memo posted → {url}" if url else "strategist: printed (no GITHUB_TOKEN)")
    elif args.command == "notion-verify":
        from .notion_sync import verify_connection
        verify_connection()
    elif args.command == "email-watch":
        from .email_watch import run
        run()
    elif args.command == "email-verify":
        from .email_watch import verify_connection as email_verify
        email_verify()
    elif args.command == "migrate-checkbox-applied":
        sys.exit(migrate_checkbox_applied())
    elif args.command == "promote-shortlist":
        sys.exit(promote_shortlist_applications())
    elif args.command == "marquee-backfill":
        sys.exit(marquee_backfill())
    elif args.command == "reconcile-checkboxes":
        from .applied import reconcile_checkboxes
        sys.exit(reconcile_checkboxes())
    elif args.command == "daily-best":
        from .board import post_daily_best
        url = post_daily_best(state.jobs())
        print(f"daily-best: {url or 'nothing posted'}")
    elif args.command == "email-batch":
        from .board import post_email_batch
        url = post_email_batch(state.load("alert_history.json", []))
        print(f"email-batch: {url or 'nothing posted'}")
    elif args.command == "master-board":
        from .board import update_master_board
        url = update_master_board(state.jobs(), state.applied())
        print(f"master-board: {url or 'not updated'}")
    elif args.command == "deliver-alerts":
        url = deliver_alerts()
        print(f"deliver-alerts: {url or 'nothing new'}")
    elif args.command == "web-action":
        sys.exit(web_action())
    elif args.command == "enrich":
        sys.exit(enrich())
    elif args.command == "regate":
        sys.exit(regate_cmd())
    elif args.command == "rescore":
        sys.exit(rescore_cmd())
    elif args.command == "lifecycle":
        sys.exit(lifecycle_cmd())
    elif args.command == "score-health":
        sys.exit(score_health_cmd())
    elif args.command == "rescrape":
        sys.exit(rescrape_cmd())
    elif args.command == "resolve-links":
        sys.exit(resolve_links_cmd())
    elif args.command == "repair-feedback":
        sys.exit(repair_feedback())
    elif args.command == "taste-report":
        from .taste import write_report
        write_report(state.feedback())
        print("taste-report: wrote docs/FEEDBACK.md")
    elif args.command == "report-sync":
        sys.exit(report_sync())
    elif args.command == "sponsorship-refresh":
        sys.exit(sponsorship_refresh_cmd())
    elif args.command == "create-google-tracker":
        sys.exit(create_google_tracker_cmd())


if __name__ == "__main__":
    main()
