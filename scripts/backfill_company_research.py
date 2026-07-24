"""Drain the company-research backlog without requiring an interactive CLI.

Run with an API-compatible provider when available, or point LLM_BASE_URL at
the local Ollama endpoint. Every source batch and synthesis batch is saved, so
an interrupted overnight run resumes from the last completed company.
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("RADAR_COMPANY_RESEARCH_MAX_AGE_DAYS", "365")
os.environ.setdefault("RADAR_COMPANY_RESEARCH_WEB_RESEARCH_LIMIT", "0")
os.environ.setdefault("RADAR_AI_MAX_CALLS", "10000")
os.environ.setdefault("RADAR_AI_MAX_REQUESTS", "10000")
os.environ.setdefault("RADAR_AI_TASK_COMPANY_RESEARCH_LIMIT", "10000")
os.environ.setdefault("RADAR_AI_PROVIDER_ATTEMPTS", "2")
# GLM has been the fastest reliable synthesis provider in production. Do not
# rotate a large backlog into known-invalid/slow models before trying it.
os.environ.setdefault("RADAR_AI_ROTATE_PROVIDERS", "0")
os.environ.setdefault("RADAR_COMPANY_RESEARCH_TIMEOUT", "75")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar import company_research, state  # noqa: E402
from radar.models import norm  # noqa: E402


def groups() -> list[list[dict]]:
    """Return employers in the same interest-first order as dossier synthesis.

    The source-collection pass used to be score-only while synthesis correctly
    favoured roles the owner had saved or was already tracking.  That made the
    two halves of one backlog disagree.  Use persisted owner intent here too;
    we deliberately do not try to infer what a visitor might click next.
    """
    jobs = state.load("jobs.json", {})
    applied_ids = {entry.get("id") for entry in state.applied() if entry.get("id")}
    shortlist_ids = {entry.get("id") for entry in state.shortlist() if entry.get("id")}
    web_ids = set(state.load("web_state.json", {}).get("jobs") or {})
    priority_ids = applied_ids | shortlist_ids | web_ids
    now = int(time.time())
    grouped: dict[str, list[dict]] = {}
    for job in jobs.values():
        if job.get("closed_at"):
            continue
        if not job.get("alert_ok") and not company_research.job_is_relevant(job):
            continue
        grouped.setdefault(norm(job.get("company", "")), []).append(job)
    def priority(rows: list[dict]) -> tuple[int, int, int, int]:
        newest = max((row.get("posted_at") or row.get("first_seen", 0) for row in rows), default=0)
        fresh = int(newest and now - newest <= 24 * 3600)
        return (
            int(any(row.get("id") in priority_ids for row in rows)),
            int(any(row.get("alert_ok") for row in rows)),
            max((row.get("score", 0) for row in rows), default=0),
            fresh,
        )

    return sorted(grouped.values(), key=priority, reverse=True)


def ready_count(records: dict) -> int:
    return sum(1 for record in records.values()
               if record.get("status") == "ready"
               and (record.get("summary") or {}).get("value") not in ("", "Not confirmed"))


def pending_count(records: dict) -> int:
    counts = company_research.backlog_counts(records)
    return counts["pending"] + counts["retry_wait"]


def main() -> int:
    web_batch = int(os.environ.get("RADAR_COMPANY_RESEARCH_WEB_BATCH", "40"))
    synth_batch = int(os.environ.get("RADAR_COMPANY_RESEARCH_SYNTH_BATCH", "4"))
    max_age = int(os.environ.get("RADAR_COMPANY_RESEARCH_MAX_AGE_DAYS", "365"))
    print(f"company backfill: web_batch={web_batch} synth_batch={synth_batch} max_age_days={max_age}", flush=True)
    # One cycle is the checkpoint unit. The workflow commits after every
    # cycle, so a cancelled/rate-limited run never loses completed dossiers.
    cycles = int(os.environ.get("RADAR_BACKFILL_CYCLES", "1"))
    made_any = False
    for _ in range(max(1, cycles)):
        now = int(time.time())
        records = company_research.load()
        before_pending = pending_count(records)
        selected = []
        for rows in groups():
            key = norm(rows[0].get("company", ""))
            if records.get(key, {}).get("web_researched_at", 0) <= now - 30 * 86400:
                selected.append(rows)
            if len(selected) >= web_batch:
                break
        for rows in selected:
            first = rows[0]
            try:
                company_research.prepare_external_sources(
                    records, first.get("company", ""),
                    [row.get("url", "") for row in rows],
                    [row.get("source_url", "") for row in rows],
                    first.get("sector", ""))
            except Exception as exc:
                print(f"company backfill: web failed for {first.get('company')}: {exc}", flush=True)
        company_research.save(records)

        before = ready_count(records)
        synthesized = company_research.enrich(
            state.load("jobs.json", {}), state.applied(),
            state.load("web_state.json", {}), limit=synth_batch)
        # This script owns its own process, so persist telemetry as well as the
        # dossier after each bounded cycle.
        from radar import llm
        llm.save_usage()
        after = ready_count(company_research.load())
        final_records = company_research.load()
        after_pending = pending_count(final_records)
        health = company_research.backlog_counts(final_records)
        print(f"company backfill: researched={len(selected)} synthesized={synthesized} "
              f"ready={after} delta={after-before} pending={before_pending}->{after_pending} "
              f"retry_wait={health['retry_wait']} errors={health['errors']} "
              f"records={len(final_records)}", flush=True)
        made_any |= bool(selected or synthesized or after_pending < before_pending)
        if not selected and synthesized == 0:
            break
    print("company backfill: checkpoint complete", flush=True)
    return 0 if made_any else 2


if __name__ == "__main__":
    raise SystemExit(main())
