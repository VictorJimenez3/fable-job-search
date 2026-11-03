"""Merge a completed crawl's discoveries onto fresh production state.

Generated job state cannot be text-rebased.  When a long-running crawl loses a
push race to another writer, upstream remains authoritative for existing jobs
while stable-ID discoveries from the completed crawl are added.  Job snapshots
are loaded through :mod:`radar.state` so referenced detail shards are hydrated
before the merge, then written through ``state.save`` so the primary snapshot
and its shards stay one compatible revision.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# This file is also invoked directly by GitHub Actions and the Mac companion.
# A direct script invocation puts ``scripts/mac-companion`` on sys.path rather
# than the repository root, so bootstrap the package before importing radar.
SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_REPO_ROOT))

from radar import state as radar_state


def _configure_state_dir() -> Path:
    """Point the shared writer at the checkout containing this merge."""
    value = os.environ.get("RADAR_STATE_DIR")
    directory = Path(value).expanduser() if value else Path.cwd() / "state"
    radar_state.STATE_DIR = directory.resolve()
    return radar_state.STATE_DIR


STATE_DIR = _configure_state_dir()


def _state_path(name: str) -> Path:
    """Resolve the generated filename for the active lane."""
    lane = os.environ.get("RADAR_PROFILE", "").strip().lower()
    prefix = "intern_" if lane in {"internship", "intern"} else ""
    return STATE_DIR / f"{prefix}{name}"


def load(path: str | Path, default):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _terminal(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    status = str(record.get("posting_status", "")).strip().lower()
    return status in {"expired", "filled", "archived"} or bool(record.get("closed_at"))


# Registry entries are updated in-place while a crawl is polling its bounded
# slice of ATS boards.  A checkpoint can therefore contain newer scheduler
# telemetry than the checkout that won the push race.  Keep this list narrow:
# identity, credentials, and curation fields belong to the winning production
# snapshot, while these fields describe one poll/probe lifecycle.
_REGISTRY_POLL_FIELDS = {
    "last_polled_at", "last_ok", "failures", "status", "probe_attempts",
    "probe_error", "invalidated_at", "hygiene_retry_at",
}


def _int_value(record: dict, key: str) -> int:
    value = record.get(key)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _registry_poll_time(record: dict) -> int:
    """Return the scheduler cursor for a registry entry.

    ``last_polled_at`` is the authoritative cursor.  ``last_ok`` is retained
    as a compatibility fallback for registries written before the cursor was
    introduced.  Missing/invalid values intentionally sort before real polls.
    """
    return _int_value(record, "last_polled_at") or _int_value(record, "last_ok")


def _registry_identity_matches(upstream: dict, checkpoint: dict) -> bool:
    """Whether both rows describe the same canonical ATS board."""
    for key in ("ats", "token"):
        if upstream.get(key) != checkpoint.get(key):
            return False
    return (upstream.get("extra") or {}) == (checkpoint.get("extra") or {})


def _merge_registry_record(upstream: dict, checkpoint: dict) -> dict:
    """Merge one checkpoint registry entry onto the current production row.

    Existing production identity/configuration remains authoritative.  When a
    checkpoint has a strictly newer poll cursor, copy the complete telemetry
    group so ``status``, failure count, and timestamps describe the same poll.
    Historical success/probe timestamps are monotonic and are retained at
    their maximum even when the newest poll failed.  Equal or missing cursors
    keep production telemetry to avoid treating an old checkpoint as newer.
    """
    merged = dict(upstream)
    upstream_time = _registry_poll_time(upstream)
    checkpoint_time = _registry_poll_time(checkpoint)

    same_identity = _registry_identity_matches(upstream, checkpoint)
    if same_identity and checkpoint_time > upstream_time:
        for key in _REGISTRY_POLL_FIELDS:
            if key in checkpoint:
                merged[key] = checkpoint[key]
        # A successful probe is the authoritative replacement for an older
        # probe failure.  Do not leave a stale diagnostic attached to an
        # active row when the checkpoint omitted the old error field.
        if (checkpoint.get("status") in {"active", "new"}
                and _int_value(checkpoint, "last_ok") >= checkpoint_time):
            merged.pop("probe_error", None)

    # Hygiene operations are administrative state transitions, not poll
    # results.  If production stamped one after the checkpoint's poll, keep
    # its invalid/duplicate/new status and related lifecycle counters.
    upstream_hygiene_time = max(
        _int_value(upstream, "invalidated_at"),
        _int_value(upstream, "hygiene_retry_at"),
    )
    if (same_identity and upstream_hygiene_time > checkpoint_time
            and upstream.get("status") in {"dup", "invalid", "new"}):
        for key in _REGISTRY_POLL_FIELDS:
            if key in upstream:
                merged[key] = upstream[key]
    # A successful poll/probe cannot be made older by a failed or stale
    # checkpoint.  Keep these historical markers monotonic regardless of which
    # side supplied the newest cursor.
    if same_identity:
        for key in ("last_ok", "invalidated_at", "hygiene_retry_at"):
            values = [_int_value(upstream, key), _int_value(checkpoint, key)]
            if max(values):
                merged[key] = max(values)

    return merged


def _load_snapshot(path: Path, legacy_history_path: Path | None = None) -> dict:
    """Load a snapshot and hydrate a legacy history file when supplied.

    ``state.load_job_snapshot`` knows the canonical sibling history filename
    and detail-shard contract.  The explicit history path preserves older
    standalone callers/tests that supplied a temporary file named ``history``.
    """
    try:
        value = radar_state.load_job_snapshot(path, {})
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        value = load(path, {})
    if not isinstance(value, dict):
        return {}
    if legacy_history_path is None:
        return value
    try:
        same_path = legacy_history_path.resolve() == path.with_name("jobs_history.json").resolve()
    except OSError:
        same_path = False
    if same_path:
        return value
    history = load(legacy_history_path, {})
    if not isinstance(history, dict):
        return value
    hydrated = dict(value)
    for key, full in history.items():
        summary = value.get(key)
        if not _terminal(summary) or not isinstance(full, dict):
            continue
        if (summary.get("posting_status") != full.get("posting_status")
                or summary.get("posting_status_changed_at")
                != full.get("posting_status_changed_at")):
            continue
        hydrated[key] = {**full, **summary}
    return hydrated


def _merge_aliases(ours_path: str | None) -> tuple[dict, bool]:
    """Add aliases from the completed crawl without replacing upstream ones."""
    upstream_path = _state_path("job_aliases.json")
    aliases = load(upstream_path, {})
    if not isinstance(aliases, dict):
        aliases = {}
    if not ours_path:
        return aliases, upstream_path.exists()
    ours = load(ours_path, {})
    if not isinstance(ours, dict):
        return aliases, True
    for old, new in ours.items():
        if old not in aliases:
            aliases[old] = new
    return aliases, True


ours_history_path = (Path(os.environ["MERGE_CRAWL_JOBS_HISTORY"])
                     if os.environ.get("MERGE_CRAWL_JOBS_HISTORY") else None)
ours_jobs_path = Path(os.environ["MERGE_CRAWL_JOBS"])
ours_jobs = _load_snapshot(ours_jobs_path, ours_history_path)
ours_history = load(ours_history_path, {}) if ours_history_path else {}
if not isinstance(ours_history, dict):
    ours_history = {}

upstream_jobs_path = _state_path("jobs.json")
upstream_jobs = _load_snapshot(upstream_jobs_path)
jobs = dict(upstream_jobs)

upstream_history_path = _state_path("jobs_history.json")
history = load(upstream_history_path, {})
if not isinstance(history, dict):
    history = {}

ours_companies = load(os.environ["MERGE_CRAWL_COMPANIES"], {})
ours_alerts = load(os.environ["MERGE_CRAWL_ALERT_HISTORY"], [])
ours_runs = load(os.environ["MERGE_CRAWL_RUNS"], [])
companies = load(_state_path("companies.json"), {})
alerts = load(_state_path("alert_history.json"), [])
runs = load(_state_path("runs.json"), [])
if not isinstance(ours_companies, dict):
    ours_companies = {}
if not isinstance(companies, dict):
    companies = {}
if not isinstance(ours_alerts, list):
    ours_alerts = []
if not isinstance(alerts, list):
    alerts = []
if not isinstance(ours_runs, list):
    ours_runs = []
if not isinstance(runs, list):
    runs = []

# Stable IDs make discoveries additive. Upstream wins for an existing ID: it
# may contain a newer quality verdict, owner action, or later scrape. New rows
# are already hydrated, including score reasons from their checkpoint shards.
added_jobs = 0
for jid, record in ours_jobs.items():
    if jid not in jobs:
        jobs[jid] = record
        if jid in ours_history:
            history[jid] = ours_history[jid]
        added_jobs += 1

# Registry tokens are likewise additive.  Existing entries need a field-aware
# merge because a completed checkpoint may contain a newer poll than the
# checkout that won the push race; identity and owner configuration remain
# authoritative in that winning checkout.
added_companies = 0
for key, record in ours_companies.items():
    if key not in companies:
        companies[key] = record
        added_companies += 1
    elif isinstance(companies.get(key), dict) and isinstance(record, dict):
        companies[key] = _merge_registry_record(companies[key], record)

# Alert history is durable delivery input. Dedupe by job ID and retain the
# most recent record so post-commit idempotent delivery sees every new job.
history_by_id = {row.get("id"): row for row in alerts if isinstance(row, dict) and row.get("id")}
for row in ours_alerts:
    if not isinstance(row, dict):
        continue
    jid = row.get("id")
    if not jid:
        continue
    if jid not in history_by_id or row.get("alerted_at", 0) > history_by_id[jid].get("alerted_at", 0):
        history_by_id[jid] = row
alerts = sorted(history_by_id.values(), key=lambda row: row.get("alerted_at", 0))[-500:]

# Runs are diagnostics only, but retaining them makes the dashboard truthful.
seen_runs = {(row.get("ts"), row.get("new_jobs"), row.get("alerts"))
             for row in runs if isinstance(row, dict)}
for row in ours_runs:
    if not isinstance(row, dict):
        continue
    marker = (row.get("ts"), row.get("new_jobs"), row.get("alerts"))
    if marker not in seen_runs:
        runs.append(row)
        seen_runs.add(marker)
runs = sorted(runs, key=lambda row: row.get("ts", 0))[-300:]

# Every state write goes through the shard-aware serializer. Keep the old
# history file as a compatibility fallback when either side supplied one; the
# next ordinary save can remove redundant entries after all consumers upgrade.
radar_state.save("jobs.json", jobs)
if history or upstream_history_path.exists() or ours_history:
    radar_state.save("jobs_history.json", history)
radar_state.save("companies.json", companies)
radar_state.save("alert_history.json", alerts)
radar_state.save("runs.json", runs)

aliases, aliases_present = _merge_aliases(os.environ.get("MERGE_CRAWL_ALIASES"))
if aliases_present:
    radar_state.save("job_aliases.json", aliases)

print(f"merge_crawl_state: added {added_jobs} discovery job(s), "
      f"{added_companies} company record(s), {len(alerts)} alert-history row(s)")
