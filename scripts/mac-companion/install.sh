#!/bin/bash
# Job Radar Mac companion installer — run ONCE on the MacBook:
#   curl -fsSL https://raw.githubusercontent.com/VictorJimenez3/fable-job-search/claude/newgrad-job-search-system-9gbj9k/scripts/mac-companion/install.sh | bash
# What it does: clones the repo to ~/.jobradar, ensures Ollama + the model,
# installs a launchd agent that runs an enrichment cycle at login + every 2h.
set -euo pipefail

REPO="VictorJimenez3/fable-job-search"
BRANCH="claude/newgrad-job-search-system-9gbj9k"
BASE="$HOME/.jobradar"
RADAR_DIR="$BASE/fable-job-search"
MODEL="${JOBRADAR_MODEL:-qwen3:14b}"
PLIST="$HOME/Library/LaunchAgents/com.jobradar.enrich.plist"

echo "== Job Radar Mac companion =="

# 1. repo
mkdir -p "$BASE"
if [ -d "$RADAR_DIR/.git" ]; then
  git -C "$RADAR_DIR" fetch -q origin "$BRANCH" && git -C "$RADAR_DIR" checkout -q "$BRANCH"
else
  git clone -q --branch "$BRANCH" "https://github.com/$REPO.git" "$RADAR_DIR"
fi
echo "repo: $RADAR_DIR"

# 2. push access check (companion commits enriched state back)
if ! git -C "$RADAR_DIR" push -q --dry-run origin "$BRANCH" 2>/dev/null; then
  echo "⚠️  git push isn't authorized from this machine yet."
  echo "   Easiest fix: install GitHub CLI (brew install gh) then: gh auth login"
  echo "   Continuing install — enrichment will run read-only until push works."
fi

# 3. ollama + model
if ! command -v ollama >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "installing ollama via homebrew..."
    brew install ollama
  else
    echo "❌ ollama not found and homebrew unavailable."
    echo "   Install from https://ollama.com/download then re-run this script."
    exit 1
  fi
fi
(ollama serve >/dev/null 2>&1 &) ; sleep 3 || true
echo "pulling model $MODEL (one-time, a few GB)..."
ollama pull "$MODEL"

# 4. launchd agent
mkdir -p "$HOME/Library/LaunchAgents" "$BASE/logs"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jobradar.enrich</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$RADAR_DIR/scripts/mac-companion/run.sh</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>RADAR_DIR</key><string>$RADAR_DIR</string>
    <key>LLM_MODEL</key><string>$MODEL</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>7200</integer>
  <key>StandardOutPath</key><string>$BASE/logs/enrich.log</string>
  <key>StandardErrorPath</key><string>$BASE/logs/enrich.log</string>
</dict>
</plist>
PLIST
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✅ installed. First cycle is running now; logs: $BASE/logs/enrich.log"
echo "   Every time this Mac is on, it enriches the radar every 2 hours."
echo "   Uninstall: launchctl unload $PLIST && rm $PLIST"
