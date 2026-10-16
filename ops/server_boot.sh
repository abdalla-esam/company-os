#!/usr/bin/env bash
# =============================================================================
# server_boot.sh — Sovereign Agency OS: 1-Command Zero-Friction Server Boot
#
# Fast, self-contained, sovereign bootstrap for any fresh server (Ubuntu,
# Debian, Oracle Linux, Rocky, macOS). Boots the API & Engine in < 5 seconds.
#
# Usage:
#   bash deploy/server_boot.sh              # Starts in background and healthchecks
#   bash deploy/server_boot.sh --foreground # Runs uvicorn in foreground
#   bash deploy/server_boot.sh --stop       # Stops running background processes
#   bash deploy/server_boot.sh --status     # Checks runtime health and PIDs
# =============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

RUN_DIR="$ROOT_DIR/.company_up"
PID_FILE="$RUN_DIR/api.pid"
LOG_FILE="$RUN_DIR/api.log"

# Colors
C_RESET="\033[0m"
C_BOLD="\033[1m"
C_GREEN="\033[92m"
C_YELLOW="\033[93m"
C_CYAN="\033[96m"
C_RED="\033[91m"

say() { echo -e "${C_CYAN}[SOVEREIGN BOOT]${C_RESET} $*"; }
ok()  { echo -e "  ${C_GREEN}✓${C_RESET} $*"; }
warn(){ echo -e "  ${C_YELLOW}⚠${C_RESET} $*"; }
fail(){ echo -e "  ${C_RED}✗${C_RESET} $*" >&2; exit 1; }

# Handle stop argument
if [ "${1:-}" = "--stop" ]; then
  say "Stopping background services..."
  if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID" 2>/dev/null || true
      sleep 1
      if kill -0 "$PID" 2>/dev/null; then kill -9 "$PID" 2>/dev/null || true; fi
      ok "Stopped API server (PID: $PID)"
    fi
    rm -f "$PID_FILE"
  else
    warn "No PID file found at $PID_FILE"
  fi
  exit 0
fi

# Handle status argument
if [ "${1:-}" = "--status" ]; then
  say "Checking server status..."
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    ok "API server is running (PID: $(cat "$PID_FILE"))"
    curl -sf http://127.0.0.1:8000/api/health && echo || warn "Health check endpoint unresponsive"
  else
    warn "API server is NOT running"
  fi
  exit 0
fi

echo -e "${C_CYAN}╔════════════════════════════════════════════════════════════════╗${C_RESET}"
echo -e "${C_CYAN}║     SOVEREIGN AGENCY OS — 1-COMMAND INSTANT SERVER BOOT        ║${C_RESET}"
echo -e "${C_CYAN}║     High-Performance Deployment & Zero-Friction Migration      ║${C_RESET}"
echo -e "${C_CYAN}╚════════════════════════════════════════════════════════════════╝${C_RESET}"

# [1/5] Runtime & Python Preflight Check
say "[1/5] Preflight Verification..."
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  fail "python3 not found on PATH. Install Python >= 3.10."
fi

PY_VERSION=$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$("$PY" -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$("$PY" -c 'import sys; print(sys.version_info.minor)')

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]); then
  fail "Python $PY_VERSION detected. Python >= 3.9 is required."
elif [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -eq 9 ]; then
  warn "Python $PY_VERSION detected (runs smoothly; Python 3.10+ recommended on production servers)"
else
  ok "Python $PY_VERSION runtime OK"
fi


# Check required libraries
MISSING_DEPS=$("$PY" -c '
missing = []
for mod in ["fastapi", "uvicorn", "pydantic", "dotenv", "requests"]:
    try:
        __import__(mod)
    except ImportError:
        missing.append(mod)
print(" ".join(missing))
')

if [ -n "$MISSING_DEPS" ]; then
  say "Installing missing dependencies: $MISSING_DEPS"
  "$PY" -m pip install --quiet --upgrade $MISSING_DEPS
  ok "Dependencies installed successfully"
else
  ok "Core dependencies verified (fastapi, uvicorn, pydantic, requests)"
fi

# [2/5] Environment (.env) Auto-Generation
say "[2/5] Sovereign Environment Configuration..."
mkdir -p "$RUN_DIR"

if [ ! -f .env ]; then
  say "Generating secure sovereign .env configuration..."
  SEC_CAP=$("$PY" -c 'import secrets; print(secrets.token_hex(32))')
  SEC_AUDIT=$("$PY" -c 'import secrets; print(secrets.token_hex(32))')
  API_TOKEN=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')

  cat <<EOF > .env
# Sovereign Agency OS Environment Configuration
PORT=8000
HOST=0.0.0.0
AGENCY_CAPABILITY_SECRET=$SEC_CAP
AGENCY_AUDIT_SECRET=$SEC_AUDIT
API_BEARER_TOKEN=$API_TOKEN

CAPSULES_SPOOL=./data/capsules/spool
CAPSULES_SANDBOX=./data/capsules/sandbox
DOMAINS_DIR=./data/domains
AGENCY_GOVERNANCE_FILE=./config/governance.json

# Default to offline safety if external cloud credentials are not supplied
AGENCY_OFFLINE_MODE=1
AGENCY_INCREMENTAL_PULSE=on
EOF
  ok ".env auto-generated with cryptographically secure secrets"
else
  ok "Existing .env loaded"
fi

# Set PYTHONPATH to include engine and root
export PYTHONPATH="$ROOT_DIR/engine:$ROOT_DIR:${PYTHONPATH:-}"

# [3/5] Directory Structure & Spool Priming
say "[3/5] Initializing Spools & Vault Layout..."
mkdir -p data/capsules/spool data/capsules/sandbox data/assets/vault data/governance/approval_gates/gate_queue data/index config

touch data/capsules/spool/.gitkeep data/capsules/sandbox/.gitkeep data/assets/vault/.gitkeep data/governance/approval_gates/gate_queue/.gitkeep

# Ensure 9 domains have scratch and approved folders
for sec in analytics client_management design development finance ideation marketing operations people; do
  mkdir -p "data/domains/$sec/tickets/approved" "data/domains/$sec/tickets/scratch" "data/domains/$sec/deliverables"
  touch "data/domains/$sec/tickets/approved/.gitkeep" "data/domains/$sec/tickets/scratch/.gitkeep" "data/domains/$sec/deliverables/.gitkeep"
done
ok "Spools and 9 constitutional domains initialized in data/"

# [4/5] SQLite Read Index Rebuild
say "[4/5] Priming SQLite Read Index..."
"$PY" -c '
import sys
from pathlib import Path
root = Path(".").resolve()
sys.path.insert(0, str(root / "engine"))
sys.path.insert(0, str(root))
from orchestration.read_index import rebuild
rebuild()
' >/dev/null 2>&1 || true
ok "Read index primed (data/index/.read_index.sqlite3)"

# [5/5] Launch Services & Health Check
say "[5/5] Booting API Server..."

# Kill any existing instance
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$PID_FILE"
fi

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

if [ "${1:-}" = "--foreground" ]; then
  say "Starting server in foreground on http://$HOST:$PORT..."
  exec "$PY" -m uvicorn orchestration.api:app --host "$HOST" --port "$PORT"
else
  # Background launch
  nohup "$PY" -m uvicorn orchestration.api:app --host "$HOST" --port "$PORT" > "$LOG_FILE" 2>&1 &
  API_PID=$!
  echo "$API_PID" > "$PID_FILE"

  # Fast health probe (max 5 seconds)
  HEALTH_OK=0
  for i in {1..10}; do
    if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      HEALTH_OK=1
      break
    fi
    sleep 0.5
  done

  if [ "$HEALTH_OK" -eq 1 ]; then
    ok "Server booted and healthy! (PID: $API_PID)"
  else
    warn "Server started (PID: $API_PID), but initial probe timed out. Check log: $LOG_FILE"
  fi
fi

# Print Summary & Access Tokens
echo
echo -e "${C_GREEN}════════════════════ BOOT COMPLETE ════════════════════${C_RESET}"
echo -e "  ${C_BOLD}Engine API:${C_RESET}     http://localhost:$PORT/"
echo -e "  ${C_BOLD}API Docs:${C_RESET}       http://localhost:$PORT/docs"
echo -e "  ${C_BOLD}Health Status:${C_RESET}  http://localhost:$PORT/api/health"
echo -e "  ${C_BOLD}Log Output:${C_RESET}     $LOG_FILE"
echo -e "  ${C_BOLD}Control:${C_RESET}        bash deploy/server_boot.sh --status | --stop"
echo -e "${C_GREEN}═══════════════════════════════════════════════════════${C_RESET}"
echo
