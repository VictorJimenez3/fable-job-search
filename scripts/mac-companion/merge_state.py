"""Re-apply this cycle's additive AI caches onto fresh upstream state.

The enrichment process can run for several minutes while a crawl lands.  On a
push race, upstream remains authoritative for existing job records; quality
verdicts, culture dossiers, cited company research, usage history, and any
newly observed rows from the completed cycle are retained additively.  Job
snapshots are hydrated through ``radar.state`` and persisted through
``state.save`` so detail pointers and shards cannot be left out of a reset.

Usage::

    MERGE_JOBS=<ours_jobs.json> MERGE_CULTURE=<ours_culture.json> \
      python scripts/mac-companion/merge_state.py

The script is intentionally executable from a temporary standalone checkout;
it bootstraps the repository package before importing ``radar.state``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_REPO_ROOT))

from radar import state as radar_state


def _configure_state_dir() -> Path:
    value = os.environ.get("RADAR_STATE_DIR")
    directory = Path(value).expanduser() if value else Path.cwd() / "state"
    radar_state.STATE_DIR = directory.resolve()
    return radar_state.STATE_DIR


STATE_DIR = _configure_state_dir()


def load(path: str | Path, default):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def load_snapshot(path: Path) -> dict:
    try:
        value = radar_state.load_job_snapshot(path, {})
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        value = load(path, {})
    return value if isinstance(value, dict) else {}


def merge_aliases() -> None:
    ours_path = os.environ.get("MERGE_ALIASES")
    upstream_path = STATE_DIR / "job_aliases.json"
    if not ours_path and not upstream_path.exists():
        return
    aliases = load(upstream_path, {})
    if not isinstance(aliases, dict):
        aliases = {}
    if ours_path:
        ours = load(ours_path, {})
        if isinstance(ours, dict):
            for old, new in ours.items():
                if old not in aliases:
                    aliases[old] = new
    radar_state.save("job_aliases.json", aliases)


ours_jobs = load_snapshot(Path(os.environ["MERGE_JOBS"]))
ours_cult = load(os.environ.get("MERGE_CULTURE", ""), {})
ours_research = load(os.environ.get("MERGE_RESEARCH", ""), {}) if os.environ.get("MERGE_RESEARCH") else {}
ours_usage = load(os.environ.get("MERGE_AI_USAGE", ""), {}) if os.environ.get("MERGE_AI_USAGE") else {}
if not isinstance(ours_cult, dict):
    ours_cult = {}
if not isinstance(ours_research, dict):
    ours_research = {}
if not isinstance(ours_usage, dict):
    ours_usage = {}

jobs = load_snapshot(STATE_DIR / "jobs.json")
cult = load(STATE_DIR / "culture.json", {})
research = load(STATE_DIR / "company_research.json", {})
usage = load(STATE_DIR / "ai_usage.json", {})
if not isinstance(cult, dict):
    cult = {}
if not isinstance(research, dict):
    research = {}
if not isinstance(usage, dict):
    usage = {}

qmerged = 0
added_jobs = 0
for jid, ours in ours_jobs.items():
    if jid not in jobs:
        jobs[jid] = ours
        added_jobs += 1
        if isinstance(ours, dict) and ours.get("quality"):
            qmerged += 1
        continue
    rec = jobs[jid]
    if not isinstance(rec, dict) or not isinstance(ours, dict):
        continue
    quality = ours.get("quality")
    if quality and not rec.get("quality"):
        rec["quality"] = quality
        qmerged += 1

cadded = 0
for key, value in ours_cult.items():
    if key not in cult:
        cult[key] = value
        cadded += 1

rmerged = 0
for key, ours in ours_research.items():
    if not isinstance(ours, dict):
        continue
    upstream = research.get(key, {})
    if not isinstance(upstream, dict):
        upstream = {}
    sources = {s.get("id"): s for s in (upstream.get("sources") or [])
               if isinstance(s, dict) and s.get("id")}
    sources.update({s.get("id"): s for s in (ours.get("sources") or [])
                    if isinstance(s, dict) and s.get("id")})
    winner = ours if ours.get("generated_at", 0) >= upstream.get("generated_at", 0) else upstream
    merged = dict(winner)
    # Keep the same six-source evidence window used by company research. This
    # boundary is lossless reconciliation, not a smaller cache.
    merged["sources"] = sorted(sources.values(), key=lambda source: -source.get("retrieved_at", 0))[:6]
    payload = "|".join(sorted(
        f"{source.get('id')}:{source.get('content_sha')}"
        for source in merged["sources"]
    ))
    merged["evidence_sha"] = hashlib.sha256(payload.encode()).hexdigest()[:16]
    if merged != upstream:
        research[key] = merged
        rmerged += 1

if ours_usage:
    histories = (usage.get("history") or []) + (ours_usage.get("history") or [])
    latest = ours_usage if ours_usage.get("generated_at", 0) >= usage.get("generated_at", 0) else usage
    usage = dict(latest)
    dedup = {f"{entry.get('generated_at')}:{entry.get('provider')}": entry
             for entry in histories if isinstance(entry, dict)}
    usage["history"] = sorted(dedup.values(), key=lambda entry: entry.get("generated_at", 0))[-30:]

# ``state.save`` is the compatibility boundary. In particular, saving a
# hydrated job map recreates all referenced detail shards and validates their
# checksums before replacing the primary snapshot.
radar_state.save("jobs.json", jobs)
radar_state.save("culture.json", cult)
radar_state.save("company_research.json", research)
radar_state.save("ai_usage.json", usage)
merge_aliases()
print(f"merge_state: kept {qmerged} quality verdict(s), added {added_jobs} job(s), "
      f"{cadded} culture dossier(s), {rmerged} company research record(s)")
