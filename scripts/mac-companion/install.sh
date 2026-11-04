#!/bin/bash
# Job Radar Mac companion installer — run ONCE on the MacBook:
#   curl -fsSL https://raw.githubusercontent.com/VictorJimenez3/fable-job-search/claude/newgrad-job-search-system-9gbj9k/scripts/mac-companion/install.sh | bash
# Forks: point it at your fork (branch is auto-detected from the repo):
#   JOBRADAR_REPO="<you>/<repo>" bash install.sh
# What it does: clones the repo to ~/.jobradar, ensures Ollama + the model,
# installs a launchd agent that runs an enrichment cycle at login + every 2h.
set -euo pipefail

REPO="${JOBRADAR_REPO:-VictorJimenez3/fable-job-search}"
BRANCH="${JOBRADAR_BRANCH:-}"
if [ -z "$BRANCH" ]; then
  BRANCH="$(curl -fsSL "https://api.github.com/repos/$REPO" 2>/dev/null \
    | sed -n 's/.*"default_branch": *"\([^"]*\)".*/\1/p')"
fi
BRANCH="${BRANCH:-claude/newgrad-job-search-system-9gbj9k}"
BASE="$HOME/.jobradar"
RADAR_DIR="$BASE/$(basename "$REPO")"
MODEL="${JOBRADAR_MODEL:-qwen3:30b}"
PLIST="$HOME/Library/LaunchAgents/com.jobradar.enrich.plist"
SOCIAL_PLIST="$HOME/Library/LaunchAgents/com.jobradar.social.plist"
VENV_DIR="${JOBRADAR_VENV:-$BASE/venv312}"

echo "== Job Radar Mac companion =="

# 1. repo
mkdir -p "$BASE"
if [ -d "$RADAR_DIR/.git" ]; then
  git -C "$RADAR_DIR" fetch -q origin "$BRANCH" && git -C "$RADAR_DIR" checkout -q "$BRANCH"
else
  git clone -q --branch "$BRANCH" "https://github.com/$REPO.git" "$RADAR_DIR"
fi
echo "repo: $RADAR_DIR"

# 1b. restricted Chrome native host (optional until the unpacked extension is
# loaded). This is what lets the extension invoke Simplify's Alt+Shift+F
# shortcut and keep a plugged-in Mac awake without exposing arbitrary shell
# commands to Chrome.
if [ -n "${JOBRADAR_EXTENSION_ID:-}" ]; then
  "$RADAR_DIR/scripts/mac-companion/install-native-host.sh" "$JOBRADAR_EXTENSION_ID" "$RADAR_DIR"
else
  echo "native host: skipped (set JOBRADAR_EXTENSION_ID after loading the extension)"
fi

# 2. push access check (companion commits enriched state back)
if ! git -C "$RADAR_DIR" push -q --dry-run origin "$BRANCH" 2>/dev/null; then
  echo "⚠️  git push isn't authorized from this machine yet."
  echo "   Easiest fix: install GitHub CLI (brew install gh) then: gh auth login"
  echo "   Continuing install — enrichment will run read-only until push works."
fi

# 3. ollama + model
if ! command -v ollama >/dev/null 2>&1; then
  echo "installing Ollama from ollama.com..."
  curl -fsSL https://ollama.com/install.sh | sh
fi
OLLAMA_BIN="$(command -v ollama || true)"
if [ -z "$OLLAMA_BIN" ] && [ -x "/Applications/Ollama.app/Contents/Resources/ollama" ]; then
  OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
fi
if [ -z "$OLLAMA_BIN" ]; then
  echo "❌ Ollama installed but its CLI could not be found. Open Ollama.app once, then re-run this script."
  exit 1
fi
("$OLLAMA_BIN" serve >/dev/null 2>&1 &) ; sleep 3 || true
echo "pulling model $MODEL (one-time, a few GB)..."
"$OLLAMA_BIN" pull "$MODEL"

# 4. launchd agent
mkdir -p "$HOME/Library/LaunchAgents" "$BASE/logs"
# Both the enrichment and social agents share this isolated environment.  The
# social collector must not depend on whichever Python happens to be selected
# by launchd's minimal PATH.
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

if [ -x "$VENV_DIR/bin/python" ]; then
  if ! "$VENV_DIR/bin/python" -c \
      'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
      >/dev/null 2>&1; then
    echo "❌ $VENV_DIR uses Python older than 3.12; it was left untouched."
    echo "   Set JOBRADAR_VENV=$BASE/venv312 or provision a Python 3.12+ environment, then rerun."
    exit 1
  fi
else
  MODERN_PYTHON="$(find_modern_python)"
  if [ -z "$MODERN_PYTHON" ]; then
    echo "❌ Python 3.12+ is required. Install it (or uv) and rerun the installer."
    exit 1
  fi
  "$MODERN_PYTHON" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install -q -r "$RADAR_DIR/requirements.txt"
# OpenCLI is the only supported Instagram backend.  Install the pinned,
# verified package when npm is already available; the browser extension and
# Instagram login remain deliberately manual Chrome steps.
if command -v npm >/dev/null 2>&1 && ! command -v opencli >/dev/null 2>&1; then
  npm install --global @jackwener/opencli@2.0.2 || \
    echo "⚠️  OpenCLI install failed; Instagram collection will stay unavailable."
fi
# launchd does not source the interactive shell, so preserve the exact Node
# bin directory that supplied OpenCLI (including an NVM installation).  The
# explicit binary also keeps collection working if PATH changes later.
OPENCLI_BIN="$(command -v opencli || true)"
NODE_BIN="$(command -v node || true)"
OPENCLI_DIR=""
NODE_DIR=""
[ -n "$OPENCLI_BIN" ] && OPENCLI_DIR="$(dirname "$OPENCLI_BIN")"
[ -n "$NODE_BIN" ] && NODE_DIR="$(dirname "$NODE_BIN")"
SOCIAL_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
for candidate in "$OPENCLI_DIR" "$NODE_DIR"; do
  if [ -n "$candidate" ] && [ -d "$candidate" ] \
      && [[ ":$SOCIAL_PATH:" != *":$candidate:"* ]]; then
    SOCIAL_PATH="$candidate:$SOCIAL_PATH"
  fi
done
xml_escape() {
  printf '%s' "$1" | sed \
    -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' \
    -e 's/"/\&quot;/g' -e "s/'/\&apos;/g"
}
OPENCLI_BIN_XML="$(xml_escape "$OPENCLI_BIN")"
OPENCLI_PROFILE_XML="$(xml_escape "${OPENCLI_PROFILE:-}")"
SOCIAL_PATH_XML="$(xml_escape "$SOCIAL_PATH")"
VENV_DIR_XML="$(xml_escape "$VENV_DIR")"
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
    <key>JOBRADAR_VENV</key><string>$VENV_DIR_XML</string>
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
cat > "$SOCIAL_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jobradar.social</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV_DIR/bin/python</string>
    <string>$RADAR_DIR/scripts/mac-companion/social.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>RADAR_DIR</key><string>$RADAR_DIR</string>
    <key>RADAR_BRANCH</key><string>$BRANCH</string>
    <key>OPENCLI_BIN</key><string>$OPENCLI_BIN_XML</string>
    <key>OPENCLI_PROFILE</key><string>$OPENCLI_PROFILE_XML</string>
    <key>PATH</key><string>$SOCIAL_PATH_XML</string>
  </dict>
  <key>RunAtLoad</key><false/>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>$BASE/logs/social.log</string>
  <key>StandardErrorPath</key><string>$BASE/logs/social.log</string>
</dict>
</plist>
PLIST
launchctl unload "$SOCIAL_PLIST" 2>/dev/null || true
launchctl load "$SOCIAL_PLIST"
echo "✅ installed. First cycle is running now; logs: $BASE/logs/enrich.log"
echo "   Every time this Mac is on, it enriches the radar every 2 hours."
echo "   Instagram collector: hourly when OpenCLI is installed and connected."
echo "   OpenCLI CLI: npm install -g @jackwener/opencli@2.0.2"
echo "   Then manually install/enable: https://chromewebstore.google.com/detail/opencli/ildkmabpimmkaediidaifkhjpohdnifk"
echo "   Keep Chrome logged into instagram.com; no cookies are copied."
echo "   Uninstall: launchctl unload $PLIST; launchctl unload $SOCIAL_PLIST; rm -f $PLIST $SOCIAL_PLIST"
