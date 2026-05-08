#!/usr/bin/env bash
# Start the Sentinel overlay server in standalone mode on a remote machine.
#
# This script is for the overlay-host scenario: the user wants the overlay
# UI to live on a different machine than the Hermes agent. In that case the
# agent is running on host A; on host B you run this script to bring up
# *just* the FastAPI overlay server (no audio capture, no agent loop) and
# point your browser at http://<this-host>:18765.
#
# Usage:
#   ./start-remote.sh                    # bind 0.0.0.0:18765, no api key
#   PORT=8080 HOST=0.0.0.0 ./start-remote.sh
#   API_KEY="$(openssl rand -hex 16)" ./start-remote.sh

set -euo pipefail

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-18765}"
API_KEY="${API_KEY:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVE_DIR="${SCRIPT_DIR}"

echo "Sentinel remote overlay on http://${HOST}:${PORT}"
echo "Serving from ${SERVE_DIR}"
if [[ -n "${API_KEY}" ]]; then
  echo "API key required (set via ?api_key=… or Authorization: Bearer …)"
fi

# If python's overlay_api module is available, prefer it (full WS + REST).
if command -v python3 >/dev/null && python3 -c "import sys; sys.path.insert(0, '${PLUGIN_DIR}'); import sentinel.overlay_api" 2>/dev/null; then
  exec python3 - <<PYEOF
import sys, time, signal
sys.path.insert(0, "${PLUGIN_DIR}")
from sentinel import config as cfgmod
from sentinel.overlay_api import ensure_overlay_server

cfg = cfgmod.load()
cfg.overlay.host = "${HOST}"
cfg.overlay.port = ${PORT}
cfg.overlay.api_key = "${API_KEY}"
cfg.overlay.enabled = True

ensure_overlay_server(cfg)

def _stop(*_):
    print("\nshutting down")
    sys.exit(0)

signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)
while True:
    time.sleep(3600)
PYEOF
fi

# Static fallback — serve the standalone HTML so a browser can at least
# reach the overlay UI even if the Python backend isn't available locally.
echo "(falling back to a static file server — no live WebSocket from this host)"
cd "${SERVE_DIR}"
exec python3 -m http.server "${PORT}" --bind "${HOST}"
