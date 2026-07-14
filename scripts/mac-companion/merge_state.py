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
import json
import os

ours_jobs = json.load(open(os.environ["MERGE_JOBS"]))
ours_cult = json.load(open(os.environ["MERGE_CULTURE"]))
jobs = json.load(open("state/jobs.json"))
cult = json.load(open("state/culture.json"))

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

json.dump(jobs, open("state/jobs.json", "w"), indent=1, sort_keys=True)
json.dump(cult, open("state/culture.json", "w"), indent=1, sort_keys=True)
print(f"merge_state: kept {qmerged} quality verdict(s), {cadded} culture dossier(s)")
