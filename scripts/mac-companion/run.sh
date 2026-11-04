#!/bin/bash
# Job Radar Mac companion — one enrichment cycle.
# Invoked by launchd (com.jobradar.enrich) at login and every 2 hours.
set -euo pipefail

RADAR_DIR="${RADAR_DIR:-$HOME/.jobradar/fable-job-search}"
VENV_DIR="${JOBRADAR_VENV:-$HOME/.jobradar/venv312}"
# Serialize every mutating operation in this clone with the social companion.
# Keep this wrapper before branch discovery, Ollama startup, or any git call.
if [ "${JOBRADAR_REPO_LOCK_HELD:-}" != "1" ]; then
  LOCK_PY="$RADAR_DIR/scripts/mac-companion/with_repo_lock.py"
  PYTHON_BIN="$VENV_DIR/bin/python"
  if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 || true)"
  fi
  if [ -z "$PYTHON_BIN" ] || [ ! -f "$LOCK_PY" ]; then
    echo "repo-lock: cannot start companion lock wrapper" >&2
    exit 2
  fi
  exec "$PYTHON_BIN" "$LOCK_PY" --repo "$RADAR_DIR" -- "$0" "$@"
fi
# the branch is whatever the clone is on (install.sh checked out the right
# one) — no hardcoding, so forks work unchanged
BRANCH="${RADAR_BRANCH:-$(git -C "$RADAR_DIR" rev-parse --abbrev-ref HEAD)}"
export LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:11434/v1}"
MODEL="${LLM_MODEL:-qwen3:30b}"
OLLAMA_BIN="${OLLAMA_BIN:-$(command -v ollama || true)}"
if [ -z "$OLLAMA_BIN" ] && [ -x "/Applications/Ollama.app/Contents/Resources/ollama" ]; then
  OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

find_modern_python() {
  local candidate
  for candidate in "${JOBRADAR_PYTHON:-}" "$HOME/.local/bin/python3.12" \
      "/opt/homebrew/bin/python3.12" "/usr/local/bin/python3.12" \
      "$(command -v python3.12 || true)" "$(command -v python3 || true)"; do
    if [ -x "$candidate" ] \
        && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
          >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  if command -v uv >/dev/null 2>&1; then
    uv python find 3.12 --system 2>/dev/null || true
  fi
}

ensure_python_env() {
  if [ -x "$VENV_DIR/bin/python" ]; then
    if ! "$VENV_DIR/bin/python" -c \
        'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
        >/dev/null 2>&1; then
      log "configured JOBRADAR_VENV=$VENV_DIR uses Python older than 3.12; preserving it"
      log "set JOBRADAR_VENV=$HOME/.jobradar/venv312 or provision a Python 3.12+ environment"
      return 1
    fi
    return 0
  fi
  local modern_python
  modern_python="$(find_modern_python)"
  if [ -z "$modern_python" ]; then
    log "Python 3.12+ is required; install it or set JOBRADAR_PYTHON before retrying"
    return 1
  fi
  "$modern_python" -m venv "$VENV_DIR"
}

sync_repo() {
  cd "$RADAR_DIR"
  local rebase_merge rebase_apply merge_head
  if ! rebase_merge="$(git rev-parse --git-path rebase-merge 2>/dev/null)" \
      || ! rebase_apply="$(git rev-parse --git-path rebase-apply 2>/dev/null)" \
      || ! merge_head="$(git rev-parse --git-path MERGE_HEAD 2>/dev/null)"; then
    log "could not inspect git operation state — preserving the checkout"
    return 1
  fi
  if [ -d "$rebase_merge" ] || [ -d "$rebase_apply" ] || [ -f "$merge_head" ]; then
    log "manual git operation is in progress — preserving the checkout"
    return 1
  fi
  # Refresh the remote ref before deciding whether a local social commit is
  # still unpublished. This does not touch the worktree.
  if ! git fetch -q origin "$BRANCH"; then
    log "could not fetch $BRANCH — preserving the checkout"
    return 1
  fi
  local dirty ahead_paths
  if ! dirty="$(git status --porcelain)"; then
    log "could not inspect git status — preserving the checkout"
    return 1
  fi
  if [ -n "$dirty" ]; then
    log "local worktree changes present — preserving them for the next cycle"
    return 1
  fi
  if ! ahead_paths="$(git diff --name-only "origin/$BRANCH..HEAD" 2>/dev/null)"; then
    log "could not inspect local commits — preserving the checkout"
    return 1
  fi
  if [ -n "$ahead_paths" ]; then
    if printf '%s\n' "$ahead_paths" | grep -q '^state/social\.json$'; then
      log "unpublished social state is ahead of origin — deferring enrichment"
    else
      log "local commits are ahead of origin — preserving the checkout"
    fi
    return 1
  fi
  git checkout -q "$BRANCH"
  git reset -q --hard "origin/$BRANCH"
  return 0
}

# This test-only/diagnostic mode exercises repository synchronization without
# starting Ollama or loading the model. It is intentionally read/write-free
# beyond the guarded checkout sync above.
if [ "${JOBRADAR_SYNC_ONLY:-}" = "1" ]; then
  if ! sync_repo; then
    exit 0
  fi
  exit 0
fi

if ! ensure_python_env; then
  exit 1
fi

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
if ! sync_repo; then
  exit 0
fi

# 2. isolated dependencies (created once; avoids modifying macOS Python)
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
  # Job records point at sibling detail shards. Preserve every lane's shard
  # directory before resetting to origin; merge_state.py hydrates the copied
  # snapshot and state.save() recreates a consistent primary + shard revision.
  for details_dir in jobs_details intern_jobs_details; do
    if [ -d "state/$details_dir" ]; then
      cp -R "state/$details_dir" "$TMPD/$details_dir"
    fi
  done
  cp state/culture.json "$TMPD/culture.json"
  RESEARCH_CACHE=""; AI_USAGE_CACHE=""
  if [ -f state/company_research.json ]; then
    cp state/company_research.json "$TMPD/company_research.json"
    RESEARCH_CACHE="$TMPD/company_research.json"
  fi
  if [ -f state/ai_usage.json ]; then
    cp state/ai_usage.json "$TMPD/ai_usage.json"
    AI_USAGE_CACHE="$TMPD/ai_usage.json"
  fi
  git fetch -q origin "$BRANCH"
  git rebase --abort >/dev/null 2>&1 || true
  git reset -q --hard "origin/$BRANCH"
  MERGE_JOBS="$TMPD/jobs.json" MERGE_CULTURE="$TMPD/culture.json" \
    MERGE_RESEARCH="$RESEARCH_CACHE" MERGE_AI_USAGE="$AI_USAGE_CACHE" \
    "$VENV_DIR/bin/python" scripts/mac-companion/merge_state.py
  rm -rf "$TMPD"
  RADAR_ENRICH_LIMIT=0 RADAR_QUALITY_LIMIT=0 RADAR_COMPANY_RESEARCH_LIMIT=0 \
    RADAR_PASTED_LIMIT=0 LLM_MODEL="$MODEL" \
    "$VENV_DIR/bin/python" -m radar.main enrich
  commit_state || { log "upstream already had everything"; exit 0; }
  sleep $((i * 2))
done
git push -q origin "$BRANCH"
