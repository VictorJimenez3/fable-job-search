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
from .score import gates, score
from .sector import infer
from .sources import aggregators, hn
from .sources.ats import FETCHERS


AGG_SOURCES = {
    "simplify": aggregators.fetch_simplify,
    "vansh": aggregators.fetch_vansh,
    "jobright": aggregators.fetch_jobright,
    "speedyapply": aggregators.fetch_speedyapply,
    "hn": hn.fetch_hn,
}


def _fetch_aggregators(disabled: set[str]) -> tuple[list[Job], dict]:
    jobs, stats = [], {}
    with ThreadPoolExecutor(max_workers=5) as ex:
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


def crawl() -> int:
    t0 = time.time()
    now = int(t0)
    p = profile()
    disabled = {s.strip() for s in env("RADAR_DISABLE_SOURCES", "").split(",") if s.strip()}

    registry = state.companies()
    discovery.seed_registry(registry, seeds())
    jobs_state = state.jobs()
    fb = state.feedback()
    seed_sectors = {norm(s["name"]): s.get("sector", "other") for s in seeds()}

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
    dropped = 0
    seen_this_run: set[str] = set()
    for j in agg_jobs + ats_jobs:
        if not j.company or not j.title or not j.url:
            continue
        jid = j.id
        if jid in seen_this_run or jid in jobs_state:
            continue
        seen_this_run.add(jid)
        keep, alert_eligible, reasons = gates(j)
        if not keep:
            dropped += 1
            continue
        if not j.sector:
            j.sector = infer(j.company, seed_sectors)
        j.alert_ok = alert_eligible
        score(j, fb, now)
        j.score_reasons += reasons
        new_jobs.append(j)

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
        rec["first_seen"] = now
        jobs_state[j.id] = rec
    cutoff = now - 120 * 86400
    jobs_state = {k: v for k, v in jobs_state.items() if v.get("first_seen", now) >= cutoff}

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

    from .alerts import post_alerts  # late import: needs GITHUB_TOKEN only here
    url = None
    try:
        url = post_alerts([j.to_record() | {"llm_note": j.llm_note} for j in alerts])
    except Exception as e:
        print(f"alerts: failed to post issue: {e}")
    print(f"crawl done in {time.time() - t0:.1f}s: {len(new_jobs)} new jobs "
          f"({dropped} gated out), {len(alerts)} alerts"
          + (f" → {url}" if url else ""))
    return 0


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
    from .notion_sync import sync_applied
    applied = state.applied()
    n = sync_applied(applied)
    state.save("applied.json", applied)
    print(f"notion-backfill: synced {n}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="radar")
    ap.add_argument("command", choices=["crawl", "applied-sync", "seed", "notion-backfill", "strategist"])
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


if __name__ == "__main__":
    main()
