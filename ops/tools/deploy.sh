#!/bin/bash
# deploy.sh — bring the running API server up to the code that is on disk.
#
# Transport is Syncthing: the files are already here. Static assets
# (HTML/CSS/JS) are served from disk and update with no restart; only the
# Python backend needs the process restarted to load a new orchestration/api.py.
#
# Usage (on the server):
#   bash tools/deploy.sh          # restart + verify
#   bash tools/deploy.sh status   # verify only
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"
PORT="${PORT:-8090}"

status() {
  echo "[deploy] verifying http://127.0.0.1:${PORT}"
  printf "  /api/health  -> %s\n" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/health" || echo 'down')"
  printf "  /api/stats   -> %s (401 = auth on, 200 = auth OFF)\n" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/stats" || echo 'down')"
  if grep -q '^AGENCY_API_TOKEN=..*' .env 2>/dev/null; then
    echo "  AGENCY_API_TOKEN: set"
  else
    echo "  AGENCY_API_TOKEN: MISSING — set it in .env (or the API returns 503)"
  fi
}

if [ "$1" = "status" ]; then
  status
  exit 0
fi

echo "[deploy] root: $ROOT"

# 1. If a systemd unit manages the cockpit, restart it and stop here.
if command -v systemctl >/dev/null 2>&1; then
  for unit in company-engine cockpit sovereign-agency-os agency-os; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${unit}\.service"; then
      echo "[deploy] restarting systemd unit: ${unit}"
      sudo systemctl restart "${unit}"
      sleep 2
      status
      exit 0
    fi
  done
fi

# 2. Otherwise: stop the running uvicorn and relaunch through the launcher.
echo "[deploy] stopping old uvicorn (if any)"
pkill -f "uvicorn orchestration.api:app" 2>/dev/null || true
pkill -f "orchestration/api.py" 2>/dev/null || true
sleep 1

echo "[deploy] starting run_cockpit.sh"
nohup bash orchestration/run_cockpit.sh > /tmp/cockpit.log 2>&1 &
sleep 3

status
echo "[deploy] done. Log: /tmp/cockpit.log"
