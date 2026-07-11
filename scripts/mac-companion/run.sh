#!/bin/bash
# Job Radar Mac companion — one enrichment cycle.
# Invoked by launchd (com.jobradar.enrich) at login and every 2 hours.
set -euo pipefail

RADAR_DIR="${RADAR_DIR:-$HOME/.jobradar/fable-job-search}"
BRANCH="claude/newgrad-job-search-system-9gbj9k"
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

# 1. sync repo
cd "$RADAR_DIR"
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

# 4. push state back (retry loop, same pattern as CI)
git add state docs
if git diff --cached --quiet; then
  log "no changes to push"
  exit 0
fi
git -c user.name="job-radar-mac" -c user.email="radar-mac@users.noreply.github.com" \
  commit -q -m "radar: mac enrich $(date -u '+%Y-%m-%d %H:%M')"
for i in 1 2 3 4; do
  git push -q origin "$BRANCH" && { log "pushed"; exit 0; }
  git pull -q --rebase origin "$BRANCH" || true
  sleep $((i * 2))
done
git push -q origin "$BRANCH"
