#!/bin/bash
# Install Victor's private Resume Studio as a login service on this Mac.
# Override the code checkout when needed:
#   RESUME_STUDIO_REPO=/path/to/fable-job-search bash scripts/resume-studio-service/install.sh
set -euo pipefail

REPO_DIR="${RESUME_STUDIO_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
PYTHON_BIN="$REPO_DIR/.venv/bin/python"
STUDIO_SCRIPT="$REPO_DIR/scripts/resume_studio.py"
PRIVATE_ROOT="$REPO_DIR/CV/.resume_studio"
LOG_DIR="$PRIVATE_ROOT/logs"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.jobradar.resume-studio.plist"
SERVICE_DOMAIN="gui/$(id -u)"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Resume Studio requires the repository virtual environment: $PYTHON_BIN" >&2
  exit 1
fi
if [ ! -f "$STUDIO_SCRIPT" ] || [ ! -d "$REPO_DIR/CV" ]; then
  echo "Resume Studio code or private CV directory is missing under: $REPO_DIR" >&2
  exit 1
fi

mkdir -p "$PLIST_DIR" "$LOG_DIR"
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jobradar.resume-studio</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$STUDIO_SCRIPT</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>4317</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOG_DIR/service.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/service.log</string>
</dict>
</plist>
PLIST

launchctl bootout "$SERVICE_DOMAIN/com.jobradar.resume-studio" 2>/dev/null || true
launchctl bootstrap "$SERVICE_DOMAIN" "$PLIST_PATH"
launchctl kickstart -k "$SERVICE_DOMAIN/com.jobradar.resume-studio"

echo "Resume Studio service installed: http://127.0.0.1:4317/"
echo "Private logs: $LOG_DIR/service.log"
echo "Remove with: launchctl bootout $SERVICE_DOMAIN/com.jobradar.resume-studio && rm $PLIST_PATH"
