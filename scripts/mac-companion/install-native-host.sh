#!/bin/zsh
set -euo pipefail

# Install the restricted native host used by the unpacked Job Radar extension.
# Usage: install-native-host.sh <extension-id> [repo-root]
EXTENSION_ID="${1:-${JOB_RADAR_EXTENSION_ID:-}}"
REPO_ROOT="${2:-${JOB_RADAR_REPO_ROOT:-$(cd "${0:A:h}/../.." && pwd)}}"
if [[ -z "$EXTENSION_ID" || ! "$EXTENSION_ID" =~ '^[a-z]{32}$' ]]; then
  print -u2 "Pass the 32-character unpacked Job Radar extension ID."
  exit 2
fi
HOST_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
HOST_PATH="$REPO_ROOT/browser-extension/native_host.py"
MANIFEST_PATH="$HOST_DIR/com.jobradar.application_agent.json"
mkdir -p "$HOST_DIR"
chmod 755 "$HOST_PATH"
python3 - "$MANIFEST_PATH" "$HOST_PATH" "$EXTENSION_ID" <<'PY'
import json, pathlib, sys
manifest, host, extension_id = sys.argv[1:]
pathlib.Path(manifest).write_text(json.dumps({
    "name": "com.jobradar.application_agent",
    "description": "Restricted Job Radar Simplify bridge",
    "path": host,
    "type": "stdio",
    "allowed_origins": [f"chrome-extension://{extension_id}/"],
}, indent=2) + "\n")
PY
print "Installed Job Radar native host at $MANIFEST_PATH"
