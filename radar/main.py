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
from . import discovery, state
from .brief import rerank
from .config import env, profile, seeds
from .digest import write_outputs
from .models import Job, norm
from .score import (RULES_VERSION, early_career_possible, explicit_new_grad,
                    gates, regate, score, source_new_grad)
from .sector import infer
from .sources import aggregators, hn
from .sources.ats import FETCHERS


AGG_SOURCES = {
    "simplify": aggregators.fetch_simplify,
    "vansh": aggregators.fetch_vansh,
    "jobright": aggregators.fetch_jobright,
    "speedyapply": aggregators.fetch_speedyapply,
    "zapply": aggregators.fetch_zapply,
    "hn": hn.fetch_hn,
}


def _fetch_aggregators(disabled: set[str]) -> tuple[list[Job], dict]:
    jobs, stats = [], {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fn): name for name, fn in AGG_SOURCES.items() if name not in disabled}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                got = fut.result()
                jobs.extend(got)
                stats[name] = len(got)
            except Exception as e:
                stats[name] = f"error: {type(e).__name__}: {e}"
    return jobs, stats


def _select_companies(registry: dict, cap: int) -> list[dict]:
    active = [e for e in registry.values() if e["status"] == "active"]
    sector_rank = {"healthtech": 0, "ai_lab": 1, "big_tech": 2, "edtech": 3, "fintech": 4}
    active.sort(key=lambda e: (0 if e["origin"] == "seed" else 1,
                               sector_rank.get(e.get("sector", ""), 5),
                               -e.get("last_ok", 0)))
    return active[:cap]


def _fetch_ats(registry: dict, disabled: set[str]) -> tuple[list[Job], dict]:
    if "ats" in disabled:
        return [], {"ats": "disabled"}
    cap = int(env("RADAR_MAX_COMPANIES", "800"))
    entries = _select_companies(registry, cap)
    wd_queries = profile().get("workday_queries")
    jobs, ok, fail = [], 0, 0

    def one(entry: dict) -> list[Job]:
        fn = FETCHERS[entry["ats"]]
        return fn(entry, wd_queries) if entry["ats"] == "workday" else fn(entry)

    with ThreadPoolExecutor(max_workers=int(env("RADAR_WORKERS", "12"))) as ex:
        futs = {ex.submit(one, e): e for e in entries}
        for fut in as_completed(futs):
            entry = futs[fut]
            try:
                got = fut.result()
                discovery.record_result(entry, True)
                # direct-ATS jobs inherit the registry's sector knowledge
                for j in got:
                    j.sector = entry.get("sector", "")
                jobs.extend(got)
                ok += 1
            except Exception:
                discovery.record_result(entry, False)
                fail += 1
    return jobs, {"companies_polled": len(entries), "ok": ok, "failed": fail}


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
    t0 = time.time()
    now = int(t0)
    p = profile()
    disabled = {s.strip() for s in env("RADAR_DISABLE_SOURCES", "").split(",") if s.strip()}

    registry = state.companies()
    discovery.seed_registry(registry, seeds())
    jobs_state = state.jobs()
    dropped_glyphs = scrub_glyph_companies(jobs_state)
    if dropped_glyphs:
        print(f"hygiene: dropped {dropped_glyphs} glyph-company record(s)")
    n_regated = regate(jobs_state)
    if n_regated:
        print(f"re-gate: rules v{RULES_VERSION} flipped alert_ok on {n_regated} stored job(s)")
    fb = state.feedback()
    seed_sectors = {norm(s["name"]): s.get("sector", "other") for s in seeds()}

    from . import culture
    culture.write_outputs()  # sync curated dossiers before scoring reads them

    agg_jobs, agg_stats = _fetch_aggregators(disabled)
    print(f"aggregators: {agg_stats}")

    harvested = discovery.harvest(registry, agg_jobs, max_new=int(env("RADAR_MAX_HARVEST", "200")))
    activated, invalidated = discovery.probe_new(registry, budget=int(env("RADAR_PROBE_BUDGET", "40")))
    print(f"discovery: harvested {harvested} new company candidates, "
          f"probed → {activated} active / {invalidated} invalid "
          f"(registry: {len(registry)})")

    ats_jobs, ats_stats = _fetch_ats(registry, disabled)
    print(f"ats: {ats_stats}")

    # ---- normalize, dedupe, score ----
    new_jobs: list[Job] = []
    # A Pipeline manual entry can precede discovery of the exact same official
    # ATS posting. Keep its tracking marker and stable ID, but replace the
    # manual placeholder with official source/description/eligibility data.
    manual_upgrades: dict[str, dict] = {}
    dropped = 0
    seen_this_run: set[str] = set()
    for j in agg_jobs + ats_jobs:
        if not j.company or not j.title or not j.url:
            continue
        jid = j.id
        existing = jobs_state.get(jid)
        if jid in seen_this_run or (existing is not None and existing.get("source") != "manual"):
            continue
        seen_this_run.add(jid)
        if not j.sector:
            j.sector = infer(j.company, seed_sectors)  # before gates: priority-sector path
        keep, alert_eligible, reasons = gates(j)
        if not keep:
            dropped += 1
            continue
        j.alert_ok = alert_eligible
        score(j, fb, now)
        j.score_reasons += reasons
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
              f"{pstats['demoted']} demoted, {pstats['closed']} closed, "
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
                  and (j.posted_at is None or now - j.posted_at <= max_age)]
    candidates.sort(key=lambda j: -j.score)
    max_alerts = int(env("RADAR_MAX_ALERTS", "25"))
    alerts = candidates[:max_alerts]

    # ---- persist ----
    for j in new_jobs:
        rec = j.to_record()
        old_manual = manual_upgrades.get(j.id)
        rec["first_seen"] = old_manual.get("first_seen", now) if old_manual else now
        if old_manual:
            rec["manual_added"] = True
        rec["rules_v"] = RULES_VERSION
        rec["explicit_new_grad"] = explicit_new_grad(j.title) or source_new_grad(j)
        rec["early_career_possible"] = early_career_possible(j, rec.get("posting"))
        jobs_state[j.id] = rec
    cutoff = now - 365 * 86400
    jobs_state = {k: v for k, v in jobs_state.items() if v.get("first_seen", now) >= cutoff}

    # The equation is not only for newly discovered rows. Rebuild every active
    # stored posting before generated state is published so Vercel, dashboard,
    # alerts, and the Mac companion all see the same current ranking.
    full_rescored, current_alerts = _rebuild_scores(jobs_state, fb, now)
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
            jobs_state, state.applied(), state.load("web_state.json", {}),
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
    recent = [rec for rec in history if rec.get("alerted_at", 0) >= now - 14 * 86400]
    url = None
    try:
        from .alerts import post_alerts
        url = post_alerts(recent)
    except Exception as e:
        print(f"alerts: failed to post issue: {e}")
    try:
        from .board import update_master_board
        update_master_board(state.jobs(), state.applied())
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
    from .notion_sync import sync_applied, sync_from_notion
    applied = state.applied()
    pulled = sync_from_notion(applied)
    n = sync_applied(applied)
    state.save("applied.json", applied)
    print(f"notion-backfill: pulled {pulled} stage change(s), pushed {n}")
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
    fb = state.feedback()
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
        score(j, fb, now)
        if j.score != old:
            rec["score"] = j.score
            rec["score_reasons"] = j.score_reasons
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
    jobs_state = state.jobs()
    flipped = regate(jobs_state)
    state.save("jobs.json", jobs_state)
    registry = state.companies()
    runs = state.load("runs.json", [])
    alert_history = state.load("alert_history.json", [])
    write_outputs(jobs_state, registry, runs, alert_history)
    demoted = sum(1 for r in jobs_state.values()
                  if any(f"re-gate v{RULES_VERSION}" in s and "dashboard only" in s
                         for s in r.get("score_reasons", [])))
    print(f"regate: rules v{RULES_VERSION} flipped {flipped} job(s) "
          f"({demoted} demoted to dashboard), docs refreshed")
    return 0


def rescore_cmd() -> int:
    """Rebuild every stored job score and eligibility decision in place.

    Unlike ``regate``, this is a deliberate full-board repair after profile or
    scoring-priority changes. It preserves the posting and quality evidence,
    then reapplies their audited demotions after deterministic scoring.
    """
    jobs_state = state.jobs()
    fb = state.feedback()
    now = int(time.time())
    changed, alerts = _rebuild_scores(jobs_state, fb, now)

    state.save("jobs.json", jobs_state)
    registry = state.companies()
    runs = state.load("runs.json", [])
    alert_history = state.load("alert_history.json", [])
    write_outputs(jobs_state, registry, runs, alert_history)
    print(f"rescore: rebuilt {changed} stored jobs; {alerts} currently alert-eligible; docs refreshed")
    return 0


def score_health_cmd() -> int:
    """Fail loudly if generated jobs state is not covered by current scoring."""
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


def _rebuild_scores(jobs_state: dict, fb: dict, now: int) -> tuple[int, int]:
    """Apply the current deterministic equation to every active stored job."""
    from . import culture, posting, quality
    import radar.score as score_mod

    culture.write_outputs()
    score_mod._CULTURE_CACHE = None
    changed = 0
    alerts = 0
    for rec in jobs_state.values():
        job = Job(company=rec.get("company", ""), title=rec.get("title", ""),
                  url=rec.get("url", ""), source=rec.get("source", ""),
                  locations=rec.get("locations", []), salary=rec.get("salary", ""),
                  remote=bool(rec.get("remote")), posted_at=rec.get("posted_at"),
                  ats=rec.get("ats", ""), sector=rec.get("sector", ""))
        keep, alert_eligible, gate_reasons = gates(job)
        score(job, fb, now)
        job.score_reasons += gate_reasons
        rec["score"] = job.score
        rec["score_reasons"] = job.score_reasons
        rec["alert_ok"] = bool(keep and alert_eligible)
        rec["explicit_new_grad"] = explicit_new_grad(job.title) or source_new_grad(job)
        rec["rules_v"] = RULES_VERSION
        rec["score_version"] = RULES_VERSION
        if rec.get("quality"):
            quality.reapply(rec)
        if rec.get("posting"):
            posting.reapply(rec)
        rec["early_career_possible"] = early_career_possible(job, rec.get("posting"))
        if rec["early_career_possible"]:
            rec["score_reasons"].append(
                "early-career possible: no stated experience floor (not new-grad verified)")
        changed += 1
        alerts += (not rec.get("closed_at")) and rec["alert_ok"] \
            and rec["score"] >= profile()["thresholds"]["alert"]
    return changed, alerts


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
    write_outputs(jobs_state, registry, state.load("runs.json", []),
                  state.load("alert_history.json", []))
    print(f"rescrape: fetched {stats.get('fetched', 0)}, unreadable {stats.get('unreadable', 0)}, "
          f"closed {stats.get('closed', 0)}, demoted {stats.get('demoted', 0)}")
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
    if action not in {"track", "applied", "untrack", "manual-add"}:
        print(f"web-action: unknown action {action!r}")
        return 0
    jobs = state.jobs()
    hist = {a["id"]: a for a in state.load("alert_history.json", [])}
    job = jobs.get(payload.get("id")) or hist.get(payload.get("id")) \
        or next((j for j in jobs.values() if j.get("url") == payload.get("url")), None)
    applied = state.applied()
    shortlist = state.shortlist()
    fb = state.feedback()
    untracked = set(state.load("untracked.json", []))
    if action == "untrack":
        changed = applied_mod.remove_tracking(payload.get("id") or payload.get("url", ""), applied, untracked)
        shortlist[:] = [s for s in shortlist if s.get("id") != payload.get("id")]
        state.save("applied.json", applied)
        state.save("shortlist.json", shortlist)
        state.save("untracked.json", sorted(untracked))
        print(f"web-action: untrack {payload.get('company')} — changed={changed}")
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
                         ats="greenhouse" if "greenhouse.io" in url else "")
            manual.sector = infer(manual.company, seed_sectors)
            _, _, reasons = gates(manual)
            score(manual, fb, int(time.time()))
            job = manual.to_record()
            job.update({
                "first_seen": int(time.time()), "rules_v": RULES_VERSION,
                "score_version": RULES_VERSION, "explicit_new_grad": False,
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


def main() -> None:
    ap = argparse.ArgumentParser(prog="radar")
    ap.add_argument("command", choices=["crawl", "applied-sync", "seed", "notion-backfill",
                                        "strategist", "notion-verify", "email-watch", "email-verify",
                                        "migrate-checkbox-applied", "promote-shortlist",
                                        "marquee-backfill", "reconcile-checkboxes",
                                        "daily-best", "master-board", "deliver-alerts", "web-action", "enrich",
                                        "email-batch",
                                        "regate", "rescore", "score-health", "rescrape", "repair-feedback"])
    args = ap.parse_args()
    if args.command == "crawl":
        sys.exit(crawl())
    elif args.command == "applied-sync":
        applied_mod.main()
    elif args.command == "seed":
        sys.exit(seed_cmd())
    elif args.command == "notion-backfill":
        sys.exit(notion_backfill())
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
    elif args.command == "score-health":
        sys.exit(score_health_cmd())
    elif args.command == "rescrape":
        sys.exit(rescrape_cmd())
    elif args.command == "repair-feedback":
        sys.exit(repair_feedback())


if __name__ == "__main__":
    main()
