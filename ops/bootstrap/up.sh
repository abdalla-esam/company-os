#!/bin/bash
# =============================================================================
# up.sh — Single command to boot the company engine (Cockpit + Pulse) and stop it
#
#   bash bootstrap/up.sh          # up   (default): Server + Periodic Pulse
#   bash bootstrap/up.sh status   # status of both + latest beat + gear memory
#   bash bootstrap/up.sh down     # stop both
#
# - Server: uvicorn orchestration.api:app (reads .env via this script)
# - Pulse : bootstrap/entrypoint.sh loop (standard container loop)
# - Logs & PIDs: .company_up/ (excluded from Git)
# =============================================================================
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT" || exit 1
export PYTHONPATH="$ROOT"

RUN_DIR="$ROOT/.company_up"
mkdir -p "$RUN_DIR"

# ---- Environment settings (read from .env and sanitized) --------------------
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PORT="${PORT:-8090}"
[ "$PORT" = "0" ] && PORT=8090
HOST="${HOST:-127.0.0.1}"
INTERVAL="${PULSE_INTERVAL:-120}"
BASE="http://127.0.0.1:$PORT"

COCKPIT_PID_FILE="$RUN_DIR/cockpit.pid"
PULSE_PID_FILE="$RUN_DIR/pulse.pid"

alive() { [ -f "$1" ] && kill -0 "$(cat "$1" 2>/dev/null)" 2>/dev/null; }
health_code() { curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$BASE/api/health" 2>/dev/null || true; }

wait_health() {
  local i
  for i in $(seq 1 30); do
    [ "$(health_code)" = "200" ] && return 0
    sleep 1
  done
  return 1
}

stop_pid() {
  local file="$1" name="$2"
  if alive "$file"; then
    local pid; pid="$(cat "$file")"
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -9 "$pid" 2>/dev/null
    rm -f "$file"
    echo "[down] $name stopped (pid $pid)"
  fi
}

do_up() {
  echo "[up] company boot — root=$ROOT port=$PORT interval=${INTERVAL}s"

  # 1) Cockpit UI + REST API
  if alive "$COCKPIT_PID_FILE" || [ "$(health_code)" = "200" ]; then
    echo "[up] cockpit already up → $BASE"
  else
    nohup python3 -m uvicorn orchestration.api:app --host "$HOST" --port "$PORT" \
      >"$RUN_DIR/cockpit.log" 2>&1 &
    local cpid=$!
    echo "$cpid" > "$COCKPIT_PID_FILE"
    disown "$cpid" 2>/dev/null || true
    echo "[up] cockpit starting (pid $(cat "$COCKPIT_PID_FILE")) → $BASE"
  fi
  if wait_health; then
    echo "[up] cockpit health 200 ✓"
  else
    echo "[up] cockpit did NOT answer in 30s — log tail:" >&2
    tail -15 "$RUN_DIR/cockpit.log" >&2
    exit 1
  fi

  # 2) Pulse Loop
  if alive "$PULSE_PID_FILE"; then
    echo "[up] pulse loop already running (pid $(cat "$PULSE_PID_FILE"))"
  else
    PULSE_INTERVAL="$INTERVAL" nohup sh "$HERE/entrypoint.sh" \
      >>"$RUN_DIR/pulse.log" 2>&1 &
    local ppid=$!
    echo "$ppid" > "$PULSE_PID_FILE"
    disown "$ppid" 2>/dev/null || true
    echo "[up] pulse loop started (pid $(cat "$PULSE_PID_FILE")) — beat every ${INTERVAL}s"
  fi


  # 3) Delivery gear reachability (the verb, not the vendor: whichever tool
  #    answers social:publish is reached through the bridge endpoint).
  local gear="${AGENCY_BRIDGE_URL_SOCIAL_PUBLISH:-${AGENCY_BRIDGE_URL:-}}"
  if [ -n "$gear" ]; then
    local origin; origin="$(printf '%s' "$gear" | sed -E 's#(https?://[^/]+).*#\1#')"
    if curl -s -o /dev/null --max-time 3 "$origin" 2>/dev/null; then
      echo "[up] delivery gear reachable ✓ ($origin)"
    else
      echo "[up] delivery gear NOT reachable ($origin) — publishing will wait for OFFLINE mode"
    fi
  else
    echo "[up] no delivery gear configured (AGENCY_BRIDGE_URL) — set AGENCY_OFFLINE_MODE=1 to trial offline"
  fi

  echo "[up] ready → Open $BASE and enter AGENCY_API_TOKEN"
}

do_status() {
  echo "== cockpit =="
  if alive "$COCKPIT_PID_FILE" || [ "$(health_code)" = "200" ]; then
    echo "   pid=$(cat "$COCKPIT_PID_FILE" 2>/dev/null) health=$(health_code) → $BASE"
  else
    echo "   stopped"
  fi
  echo "== pulse =="
  if alive "$PULSE_PID_FILE"; then
    echo "   running (pid $(cat "$PULSE_PID_FILE")) every ${INTERVAL}s"
    echo "   last beat: $(tail -1 "$RUN_DIR/pulse.log" 2>/dev/null | head -c 200)"
  else
    echo "   stopped"
  fi
  echo "== logs =="
  echo "   $RUN_DIR/cockpit.log  $RUN_DIR/pulse.log"
}

do_down() {
  stop_pid "$PULSE_PID_FILE" "pulse loop"
  stop_pid "$COCKPIT_PID_FILE" "cockpit"
  local i code=000
  for i in 1 2 3 4 5; do
    code="$(health_code)"
    [ "$code" = "000" ] && break
    sleep 1
  done
  [ "$code" = "000" ] || echo "[down] warning: port $PORT still answers (health=$code)"
  echo "[down] done"
}

case "${1:-up}" in
  up)     do_up ;;
  status) do_status ;;
  down)   do_down ;;
  *)      echo "usage: $0 [up|status|down]" >&2; exit 2 ;;
esac
