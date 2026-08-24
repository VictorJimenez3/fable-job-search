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
WORKERS="${RESUME_STUDIO_WORKERS:-2}"

case "$WORKERS" in
  1|2|3|4) ;;
  *)
    echo "RESUME_STUDIO_WORKERS must be an integer from 1 to 4" >&2
    exit 1
    ;;
esac

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
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>RESUME_STUDIO_WORKERS</key><string>$WORKERS</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ExitTimeOut</key><integer>30</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOG_DIR/service.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/service.log</string>
</dict>
</plist>
PLIST

SERVICE_LABEL="$SERVICE_DOMAIN/com.jobradar.resume-studio"
launchctl bootout "$SERVICE_LABEL" 2>/dev/null || true

# launchd can briefly retain the old job after bootout. Retry the bootstrap so
# reinstalling the companion does not leave Resume Studio offline on a transient
# "Input/output error" (exit 5).
bootstrapped=false
for attempt in 1 2 3 4 5; do
  if launchctl bootstrap "$SERVICE_DOMAIN" "$PLIST_PATH" 2>/dev/null; then
    bootstrapped=true
    break
  fi
  launchctl bootout "$SERVICE_LABEL" 2>/dev/null || true
  # launchd removes the old job asynchronously; a one-second retry loop was
  # still racy on a busy login session. Give teardown a little more room.
  sleep 2
done
if [ "$bootstrapped" != true ]; then
  echo "Could not bootstrap Resume Studio with launchd: $PLIST_PATH" >&2
  exit 1
fi

# RunAtLoad starts the freshly bootstrapped job. A non-forcing kickstart keeps
# this reinstall path from turning a graceful shutdown into SIGKILL while
# provider process groups are being reaped.
launchctl kickstart "$SERVICE_LABEL"
launchctl print "$SERVICE_LABEL" >/dev/null

# launchctl can report an active job a moment before Python has bound the
# socket. Do not announce success until the private loopback health endpoint
# answers, so callers can open the UI immediately after installation.
ready=false
for attempt in $(seq 1 40); do
  if curl --silent --show-error --fail --max-time 2 \
    http://127.0.0.1:4317/api/health >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 0.25
done
if [ "$ready" != true ]; then
  echo "Resume Studio launchd job loaded but health check did not become ready" >&2
  exit 1
fi

echo "Resume Studio service installed: http://127.0.0.1:4317/"
echo "Private logs: $LOG_DIR/service.log"
echo "Remove with: launchctl bootout $SERVICE_LABEL && rm $PLIST_PATH"
