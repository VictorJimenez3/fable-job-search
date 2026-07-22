"""Merge a completed crawl's new discoveries onto fresh production state.

Generated job state cannot be text-rebased.  When a long-running crawl loses a
push race to the company-research backfill, upstream remains authoritative for
existing jobs while stable-ID discoveries from the completed crawl are added.
The caller then runs ``radar.main rescore`` to recreate all derived fields and
documents.  This keeps both writers live without force-pushing or discarding
new postings.
"""
import json
import os


def load(path: str, default):
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


ours_jobs = load(os.environ["MERGE_CRAWL_JOBS"], {})
ours_companies = load(os.environ["MERGE_CRAWL_COMPANIES"], {})
ours_alerts = load(os.environ["MERGE_CRAWL_ALERT_HISTORY"], [])
ours_runs = load(os.environ["MERGE_CRAWL_RUNS"], [])

jobs = load("state/jobs.json", {})
companies = load("state/companies.json", {})
alerts = load("state/alert_history.json", [])
runs = load("state/runs.json", [])

# Stable IDs make discoveries additive. Upstream wins for an existing ID: it
# may contain a newer quality verdict, user action, or a later crawl's scrape.
added_jobs = 0
for jid, record in ours_jobs.items():
    if jid not in jobs:
        jobs[jid] = record
        added_jobs += 1

# Registry tokens are likewise additive; never overwrite a concurrently
# updated probe status or timestamps.
added_companies = 0
for key, record in ours_companies.items():
    if key not in companies:
        companies[key] = record
        added_companies += 1

# Alert history is durable delivery input. Dedupe by job ID and retain the
# most recent record so the post-commit idempotent delivery sees every new job.
history_by_id = {row.get("id"): row for row in alerts if row.get("id")}
for row in ours_alerts:
    jid = row.get("id")
    if not jid:
        continue
    if jid not in history_by_id or row.get("alerted_at", 0) > history_by_id[jid].get("alerted_at", 0):
        history_by_id[jid] = row
alerts = sorted(history_by_id.values(), key=lambda row: row.get("alerted_at", 0))[-500:]

# Runs are diagnostics only, but retaining them makes the dashboard truthful.
seen_runs = {(row.get("ts"), row.get("new_jobs"), row.get("alerts")) for row in runs}
for row in ours_runs:
    marker = (row.get("ts"), row.get("new_jobs"), row.get("alerts"))
    if marker not in seen_runs:
        runs.append(row)
        seen_runs.add(marker)
runs = sorted(runs, key=lambda row: row.get("ts", 0))[-300:]

for name, value in (("jobs.json", jobs), ("companies.json", companies),
                    ("alert_history.json", alerts), ("runs.json", runs)):
    with open(f"state/{name}", "w") as handle:
        json.dump(value, handle, indent=1, sort_keys=True, ensure_ascii=False)
        handle.write("\n")

print(f"merge_crawl_state: added {added_jobs} discovery job(s), "
      f"{added_companies} company record(s), {len(alerts)} alert-history row(s)")
