#!/bin/bash
# Job Radar Mac companion — one enrichment cycle.
# Invoked by launchd (com.jobradar.enrich) at login and every 2 hours.
set -euo pipefail

RADAR_DIR="${RADAR_DIR:-$HOME/.jobradar/fable-job-search}"
# the branch is whatever the clone is on (install.sh checked out the right
# one) — no hardcoding, so forks work unchanged
BRANCH="${RADAR_BRANCH:-$(git -C "$RADAR_DIR" rev-parse --abbrev-ref HEAD)}"
export LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:11434/v1}"
MODEL="${LLM_MODEL:-qwen3:30b}"
VENV_DIR="${JOBRADAR_VENV:-$HOME/.jobradar/venv}"
OLLAMA_BIN="${OLLAMA_BIN:-$(command -v ollama || true)}"
if [ -z "$OLLAMA_BIN" ] && [ -x "/Applications/Ollama.app/Contents/Resources/ollama" ]; then
  OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# A loaded model is the expensive part (~19GB for qwen3:30b), not Ollama's
# small local server. Always release it even when this cycle fails midway.
cleanup() { [ -n "$OLLAMA_BIN" ] && "$OLLAMA_BIN" stop "$MODEL" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# 0. Ollama must be up (Ollama.app serves the API when running; `ollama serve` otherwise)
if ! curl -sf "${LLM_BASE_URL%/v1}/api/tags" >/dev/null 2>&1; then
  if [ -n "$OLLAMA_BIN" ]; then
    log "starting ollama..."
    ("$OLLAMA_BIN" serve >/dev/null 2>&1 &)
    sleep 4
  fi
fi
if ! curl -sf "${LLM_BASE_URL%/v1}/api/tags" >/dev/null 2>&1; then
  log "ollama not reachable at $LLM_BASE_URL — skipping this cycle"
  exit 0
fi

# 1. sync repo (self-heal first: a previous cycle's failed pull can leave a
# rebase/merge in progress, which wedges every later git operation)
cd "$RADAR_DIR"
git rebase --abort >/dev/null 2>&1 || true
git merge --abort  >/dev/null 2>&1 || true
git fetch -q origin "$BRANCH"
git checkout -q "$BRANCH"
git reset -q --hard "origin/$BRANCH"   # companion never has local edits; state is authoritative upstream

# 2. isolated dependencies (created once; avoids modifying macOS Python)
if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install -q -r requirements.txt

# 3. enrich
LLM_MODEL="$MODEL" "$VENV_DIR/bin/python" -m radar.main enrich

# 4. push state back. An enrich cycle takes many minutes, so a CI crawl
# often lands upstream mid-cycle; both sides regenerate jobs.json and the
# docs wholesale, so `git pull --rebase` conflicts and (worse) used to leave
# the clone wedged mid-rebase. Recovery instead re-merges only our LLM
# caches (quality verdicts, culture dossiers — dict-additive) onto fresh
# upstream and rebuilds everything else with a zero-limit enrich (re-applies
# cached verdicts + regenerates docs; no new LLM calls).
commit_state() {
  git add state docs
  git diff --cached --quiet && return 1
  git -c user.name="job-radar-mac" -c user.email="radar-mac@users.noreply.github.com" \
    commit -q -m "radar: mac enrich $(date -u '+%Y-%m-%d %H:%M')"
}
commit_state || { log "no changes to push"; exit 0; }
for i in 1 2 3; do
  git push -q origin "$BRANCH" && { log "pushed"; exit 0; }
  log "push rejected (upstream moved) — re-merging caches onto fresh upstream"
  TMPD="$(mktemp -d)"
  cp state/jobs.json "$TMPD/jobs.json"
  cp state/culture.json "$TMPD/culture.json"
  git fetch -q origin "$BRANCH"
  git rebase --abort >/dev/null 2>&1 || true
  git reset -q --hard "origin/$BRANCH"
  MERGE_JOBS="$TMPD/jobs.json" MERGE_CULTURE="$TMPD/culture.json" \
    "$VENV_DIR/bin/python" scripts/mac-companion/merge_state.py
  rm -rf "$TMPD"
  RADAR_ENRICH_LIMIT=0 RADAR_QUALITY_LIMIT=0 LLM_MODEL="$MODEL" \
    "$VENV_DIR/bin/python" -m radar.main enrich
  commit_state || { log "upstream already had everything"; exit 0; }
  sleep $((i * 2))
done
git push -q origin "$BRANCH"
