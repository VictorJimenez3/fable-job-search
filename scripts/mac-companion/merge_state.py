"""Re-apply this cycle's LLM caches onto fresh upstream state after a push
race (a CI crawl landed while enrich was computing — git can't merge
regenerated JSON/markdown, but the caches are dict-additive).

Upstream wins everywhere; we only copy over what upstream cannot have:
quality verdicts (rec["quality"]) and culture dossiers it lacks. Effects
(scores, alert_ok, closed_at, docs) are NOT copied — the caller re-runs
`radar.main enrich` with zero limits, which re-applies cached verdicts and
regenerates docs without new LLM calls.

Usage: MERGE_JOBS=<ours_jobs.json> MERGE_CULTURE=<ours_culture.json> python merge_state.py
(run from the repo root, after `git reset --hard origin/<branch>`)
"""
import hashlib
import json
import os

ours_jobs = json.load(open(os.environ["MERGE_JOBS"]))
ours_cult = json.load(open(os.environ["MERGE_CULTURE"]))
ours_research = json.load(open(os.environ["MERGE_RESEARCH"])) if os.environ.get("MERGE_RESEARCH") else {}
ours_usage = json.load(open(os.environ["MERGE_AI_USAGE"])) if os.environ.get("MERGE_AI_USAGE") else {}
jobs = json.load(open("state/jobs.json"))
cult = json.load(open("state/culture.json"))
try:
    research = json.load(open("state/company_research.json"))
except FileNotFoundError:
    research = {}
try:
    usage = json.load(open("state/ai_usage.json"))
except FileNotFoundError:
    usage = {}

qmerged = 0
for jid, rec in jobs.items():
    q = ours_jobs.get(jid, {}).get("quality")
    if q and not rec.get("quality"):
        rec["quality"] = q
        qmerged += 1
cadded = 0
for k, v in ours_cult.items():
    if k not in cult:
        cult[k] = v
        cadded += 1

rmerged = 0
for k, ours in ours_research.items():
    upstream = research.get(k, {})
    sources = {s.get("id"): s for s in (upstream.get("sources") or []) if s.get("id")}
    sources.update({s.get("id"): s for s in (ours.get("sources") or []) if s.get("id")})
    winner = ours if ours.get("generated_at", 0) >= upstream.get("generated_at", 0) else upstream
    merged = dict(winner)
    # Keep the same six-source evidence window used by company research.  This
    # merge is a lossless reconciliation boundary, not a smaller cache.
    merged["sources"] = sorted(sources.values(), key=lambda s: -s.get("retrieved_at", 0))[:6]
    payload = "|".join(sorted(f"{s.get('id')}:{s.get('content_sha')}" for s in merged["sources"]))
    merged["evidence_sha"] = hashlib.sha256(payload.encode()).hexdigest()[:16]
    if merged != upstream:
        research[k] = merged
        rmerged += 1

if ours_usage:
    histories = (usage.get("history") or []) + (ours_usage.get("history") or [])
    latest = ours_usage if ours_usage.get("generated_at", 0) >= usage.get("generated_at", 0) else usage
    usage = dict(latest)
    dedup = {f"{h.get('generated_at')}:{h.get('provider')}": h for h in histories}
    usage["history"] = sorted(dedup.values(), key=lambda h: h.get("generated_at", 0))[-30:]

json.dump(jobs, open("state/jobs.json", "w"), indent=1, sort_keys=True)
json.dump(cult, open("state/culture.json", "w"), indent=1, sort_keys=True)
json.dump(research, open("state/company_research.json", "w"), indent=1, sort_keys=True)
json.dump(usage, open("state/ai_usage.json", "w"), indent=1, sort_keys=True)
print(f"merge_state: kept {qmerged} quality verdict(s), {cadded} culture dossier(s), "
      f"{rmerged} company research record(s)")
