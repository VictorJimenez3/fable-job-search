"""Pipeline entrypoints.

  python -m radar.main crawl          # full discovery → score → alert cycle
  python -m radar.main applied-sync   # process a GitHub issue event (CI)
  python -m radar.main seed           # initialize registry + taste from seeds
  python -m radar.main notion-backfill  # retry queued Notion syncs
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import applied as applied_mod
from . import discovery, lifecycle, link_resolver, sponsorship, state
from .brief import rerank
from .config import env, profile, profile_id, seeds
from .digest import write_outputs
from .identity import canonical_url
from .internship import RULES_VERSION as INTERNSHIP_RULES_VERSION
from .models import Job, norm
from .score import (
    RULES_VERSION,
    apply_company_concentration,
    build_preference_profile,
    early_career_possible,
    explicit_new_grad,
    gates,
    load_score_preferences,
    normalize_score_preferences,
    regate,
    score,
    source_new_grad,
)
from .sector import infer
from .sources import aggregators, hn
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
}

AGG_SOURCES_INTERNSHIP = {
    "simplify_internship": aggregators.fetch_simplify_internship,
    "speedyapply_internship": aggregators.fetch_speedyapply_internship,
    "zapply_internship": aggregators.fetch_zapply_internship,
    "dreamwork_internship": aggregators.fetch_dreamwork_internship,
}

PM_BACKFILL_ATS = {"workday", "phenom"}


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


def _merge_job_sighting(winner: Job, sighting: Job) -> None:
    """Keep every feed/source link when one posting wins a feed dedupe."""
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
    if sighting.link_resolution:
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
    target_url = canonical_url(target.get("url"))
    alternate_urls = target.setdefault("alternate_urls", [])
    for url in [sighting.get("url"), *sighting.get("alternate_urls", [])]:
        if url and canonical_url(url) != target_url:
            changed |= _append_unique(alternate_urls, [url])
    resolution = sighting.get("link_resolution") or {}
    closed_aggregator = (
        resolution.get("status") == "closed" and
        link_resolver.is_aggregator_url(target.get("url")) is False
    )
    if (resolution and not closed_aggregator and
            (not target.get("link_resolution") or
             target.get("link_resolution", {}).get("status") != "resolved")):
        target["link_resolution"] = resolution
        changed = True
    return changed


def _unique_discovered_jobs(discovered: list[Job]) -> tuple[list[Job], int]:
    """Keep one best-provenance candidate per stable role identity per run."""
    selected: dict[str, Job] = {}
    passthrough: list[Job] = []
    dropped = 0
    for job in discovered:
        _append_unique(job.source_variants, [job.source])
        _append_unique(job.source_url_variants, [job.source_url])
        key = canonical_url(job.url)
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
    # ``Job.id`` is intentionally company/title/location based, so two feeds
    # can still collide after their URLs differ. Keep the best primary link
    # under that stable identity and retain the other URL as provenance rather
    # than letting the crawl's later ID guard discard it silently.
    by_role: dict[str, Job] = {}
    role_passthrough: list[Job] = []
    for job in list(selected.values()) + passthrough:
        role_key = job.id if canonical_url(job.url) else ""
        if not role_key:
            role_passthrough.append(job)
            continue
        previous = by_role.get(role_key)
        if previous is None:
            by_role[role_key] = job
        elif _discovered_job_priority(job) > _discovered_job_priority(previous):
            _merge_job_sighting(job, previous)
            by_role[role_key] = job
            dropped += 1
        else:
            _merge_job_sighting(previous, job)
            dropped += 1
    return list(by_role.values()) + role_passthrough, dropped


def _resolve_discovered_links(discovered: list[Job], jobs_state: dict, now: int) -> tuple[list[Job], dict]:
    """Resolve a bounded batch of aggregator links before identity matching."""
    stats = {"attempted": 0, "resolved": 0, "closed": 0, "unchanged": 0, "errors": 0}
    if env("RADAR_DISABLE_LINK_RESOLUTION", "").lower() in {"1", "true", "yes"}:
        return discovered, stats
    limit = max(0, int(env("RADAR_LINK_RESOLVE_LIMIT", "25")))
    candidates = []
    for job in discovered:
        if not link_resolver.is_aggregator_url(job.url):
            continue
        existing = jobs_state.get(job.id)
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
        remap_entry_ids,
        remap_web_jobs,
        resolve_alias,
    )

    jobs_state, aliases, exact_merged = collapse_jobs(jobs_state)
    jobs_state, cross_aliases, cross_merged = collapse_cross_source_jobs(jobs_state)
    aliases.update(cross_aliases)
    if not aliases:
        return jobs_state, False

    applied_changed = remap_entry_ids(applied, aliases)
    tracker_merged = applied_mod.deduplicate_entries(applied)
    shortlist = state.shortlist()
    shortlist_changed = remap_entry_ids(shortlist, aliases)
    # A shortlist can contain two feed variants of one URL even when the
    # durable applied list does not. Keep the first owner selection.
    seen_shortlist = set()
    compact_shortlist = []
    for entry in shortlist:
        key = canonical_url(entry.get("url")) or f"id:{entry.get('id', '')}"
        if key in seen_shortlist:
            shortlist_changed += 1
            continue
        seen_shortlist.add(key)
        compact_shortlist.append(entry)
    shortlist[:] = compact_shortlist

    history = state.load("alert_history.json", [])
    history_changed = remap_entry_ids(history, aliases)
    untracked = {
        resolve_alias(str(job_id), aliases)
        for job_id in state.load("untracked.json", [])
    }
    web = state.load("web_state.json", {})
    web_changed = remap_web_jobs(web, aliases)

    prior_aliases = state.load("job_aliases.json", {})
    prior_aliases.update(aliases)
    for old in list(prior_aliases):
        prior_aliases[old] = resolve_alias(prior_aliases[old], prior_aliases)

    state.save("job_aliases.json", prior_aliases)
    state.save("applied.json", applied)
    state.save("shortlist.json", shortlist)
    state.save("untracked.json", sorted(untracked))
    state.save("alert_history.json", history)
    if web_changed:
        state.save("web_state.json", web)
    print(
        f"hygiene: merged {exact_merged} exact-URL + {cross_merged} "
        f"high-confidence aggregator/ATS duplicate posting record(s); "
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
                    job.source_board = board
                jobs.extend(got)
                stats[name] = len(got)
                healthy_boards.add(board)
            except Exception as e:
                stats[name] = f"error: {type(e).__name__}: {e}"
    return jobs, stats, healthy_boards


def _select_companies(registry: dict, cap: int) -> list[dict]:
    active = [e for e in registry.values() if e["status"] == "active"]
    sector_rank = {"healthtech": 0, "ai_lab": 1, "big_tech": 2, "edtech": 3, "fintech": 4}
    active.sort(key=lambda e: (0 if e.get("pm_interest") else 1,
                               0 if e["origin"] == "seed" else 1,
                               sector_rank.get(e.get("sector", ""), 5),
                               -e.get("last_ok", 0)))
    return active[:cap]


def _pm_backfill_ids(entries: list[dict], cap: int) -> set[int]:
    """Bound the expensive PM query fan-out to prioritized query-driven ATSs."""
    candidates = [e for e in entries if e.get("ats") in PM_BACKFILL_ATS]
    return {id(e) for e in candidates[:max(0, cap)]}


def _fetch_ats(registry: dict, disabled: set[str]) -> tuple[list[Job], dict, set[str]]:
    if "ats" in disabled:
        return [], {"ats": "disabled"}, set()
    cap = int(env("RADAR_MAX_COMPANIES", "800"))
    entries = _select_companies(registry, cap)
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
    return jobs, {"companies_polled": len(entries), "ok": ok, "failed": fail}, healthy_boards


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
    existing_url_ids = {
        canonical_url(record.get("url")): jid
        for jid, record in jobs_state.items()
        if canonical_url(record.get("url"))
    }
    seen_this_run: set[str] = set()
    for j in discovered:
        if not j.company or not j.title or not j.url:
            continue
        jid = j.id
        link_result = j.link_resolution or {}
        if link_result.get("status") == "closed":
            # Do this before ``touch``: touch deliberately reopens any
            # terminal role that reappears in a feed, while this page signal
            # is the stronger, same-run liveness verdict.
            existing = jobs_state.get(jid)
            if existing is not None:
                existing["link_resolution"] = dict(link_result)
                lifecycle.mark_terminal(
                    existing,
                    link_result.get("posting_status") or lifecycle.EXPIRED,
                    now,
                    link_result.get("reason") or "Jobright page says the posting is closed",
                )
                seen_this_run.add(jid)
            continue
        canonical = canonical_url(j.url)
        existing_url_id = existing_url_ids.get(canonical) if canonical else None
        if existing_url_id and existing_url_id != jid:
            # The durable state already has this URL under a title/location
            # variant. Keep that identity instead of reintroducing a duplicate
            # on every crawl; the next lifecycle/scoring pass still refreshes
            # the surviving record.
            existing = jobs_state[existing_url_id]
            lifecycle.touch(existing, now, j.source or "monitored source")
            _merge_record_sighting(existing, j.to_record())
            seen_this_run.add(existing_url_id)
            continue
        existing = jobs_state.get(jid)
        if jid in seen_this_run:
            # ``_unique_discovered_jobs`` normally handles this before the
            # crawl loop. Keep a defensive state merge for edge cases without
            # replacing an already-scored in-memory job with a raw sighting.
            if existing is not None:
                _merge_record_sighting(existing, j.to_record())
            continue
        seen_this_run.add(jid)
        if existing is not None:
            # A feed sighting can reopen an automatically closed record. The
            # liveness fetch below still has authority to close it again.
            lifecycle.touch(existing, now, j.source or "monitored source")
        if existing is not None and existing.get("source") != "manual":
            sighting = j.to_record()
            _merge_record_sighting(existing, sighting)
            resolved_upgrade = (
                bool(j.link_resolution.get("status") == "resolved") and
                canonical_url(j.url) != canonical_url(existing.get("url")) and
                not link_resolver.is_aggregator_url(j.url)
            )
            if not resolved_upgrade:
                continue
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
                        key=lambda j: -j.score)
    rerank(borderline)

    # ---- decide alerts ----
    max_age = thr["max_posting_age_days"] * 86400
    candidates = [j for j in new_jobs
                  if j.alert_ok and j.score >= thr["alert"]
                  and not lifecycle.is_terminal(j)
                  and (j.posted_at is None or now - j.posted_at <= max_age)]
    candidates.sort(key=lambda j: -j.score)
    max_alerts = int(env("RADAR_MAX_ALERTS", "25"))
    alerts = candidates[:max_alerts]

    # ---- persist ----
    for j in new_jobs:
        rec = j.to_record()
        old_record = jobs_state.get(j.id)
        old_manual = manual_upgrades.get(j.id)
        rec["first_seen"] = (
            (old_manual or old_record or {}).get("first_seen", now)
        )
        if old_manual:
            rec["manual_added"] = True
        if old_record:
            _merge_record_sighting(rec, old_record)
            for key in ("posting", "quality", "company_research"):
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
        rec["explicit_new_grad"] = explicit_new_grad(j.title) or source_new_grad(j)
        rec["early_career_possible"] = early_career_possible(j, rec.get("posting"))
        jobs_state[j.id] = rec
        if canonical_url(j.url):
            existing_url_ids[canonical_url(j.url)] = j.id
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
    researched_companies = company_research.prepare_for_jobs(
        [j.to_record() for j in new_jobs],
        limit=int(env("RADAR_CRAWL_WEB_RESEARCH_LIMIT", "6")))
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
    recent = [rec for rec in history
              if rec.get("alerted_at", 0) >= now - 14 * 86400
              and jobs.get(rec.get("id")) is not None
              and not lifecycle.is_terminal(jobs[rec.get("id")])]
    url = None
    try:
        from .alerts import post_alerts
        url = post_alerts(recent)
    except Exception as e:
        print(f"alerts: failed to post issue: {e}")
    try:
        from .board import update_master_board
        update_master_board(jobs, state.applied())
    except Exception as e:
        print(f"board: failed to update master board: {e}")
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
                sector=rec.get("sector", ""))
        old = rec.get("score", 0)
        score(j, fb, now, preference_profile, score_preferences)
        rec["score_raw"] = j.score_raw
        rec["score_calibrated"] = j.score_calibrated
        rec["evidence_score"] = j.evidence_score
        rec["eligibility"] = j.eligibility
        rec["priority_tier"] = j.priority_tier
        rec["score_dimensions"] = j.score_dimensions
        rec["score_dimensions_raw"] = j.score_dimensions_raw
        rec["score_version"] = RULES_VERSION
        rec["score_reasons"] = j.score_reasons
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
                  ats=rec.get("ats", ""), sector=rec.get("sector", ""))
        keep, alert_eligible, gate_reasons = gates(job)
        score(job, fb, now, preference_profile, score_preferences)
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
        rec["alert_ok"] = bool(keep and alert_eligible)
        rec["explicit_new_grad"] = explicit_new_grad(job.title) or source_new_grad(job)
        rec["rules_v"] = RULES_VERSION
        rec["score_version"] = RULES_VERSION
        if rec.get("quality"):
            quality.reapply(rec)
        if rec.get("posting"):
            posting.reapply(rec)
        lifecycle.normalize_record(rec, now)
        sponsorship.annotate_record(rec, sponsorship_db)
        rec["early_career_possible"] = early_career_possible(job, rec.get("posting"))
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
            "new_grad_email": True, "internship_email": False,
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
        changed = taste.record_feedback(fb, job, payload.get("vote"), payload.get("reason"))
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
        if stage not in {"maybe", "saved", "applied", "oa", "interview", "rejected", "closed", "not_pursuing"}:
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
