#!/bin/bash
# =============================================================================
# preflight.sh — the foundation gate.
#
#   bash ops/deploy/preflight.sh           # verify the current directory
#   bash ops/deploy/preflight.sh /srv/app  # verify an installed tree
#
# "It starts" and "it is safe to start" are different questions. This script
# answers the second one: every check is one line of PASS / WARN / FAIL, and
# the exit status is non-zero when a FAIL remains — so it can gate a systemd
# `ExecStartPre=`, a CI job, or a human running it by hand.
#
#   PASS  ready
#   WARN  runs, but a real risk is left on the table (read it before launch)
#   FAIL  will not run safely — fix it before the server takes traffic
#
# Standard library only; no dependencies beyond the project itself.
# =============================================================================
set -u

ROOT="${1:-.}"
cd "$ROOT" 2>/dev/null || { echo "FAIL  cannot cd to $ROOT"; exit 1; }

PASS=0; WARN=0; FAIL=0
out()  { printf '%-5s %s\n' "$1" "$2"; case "$1" in PASS) PASS=$((PASS+1));; WARN) WARN=$((WARN+1));; FAIL) FAIL=$((FAIL+1));; esac; }
fail() { out FAIL "$@"; }
warn() { out WARN "$@"; }
pass() { out PASS "$@"; }

# Read .env WITHOUT printing values: only presence, length and equality are
# ever reported, so no credential can leak into a log file or a terminal scroll.
env_get() {
  [ -f .env ] || return 1
  # match "KEY=value", strip everything up to the first '='
  sed -n "s/^$1=//p" .env | head -1 | tr -d '\r'
}
env_present() { [ -f .env ] && grep -q "^$1=" .env; }

echo "== 1. runtime =="
PY="${PYTHON:-python3}"
if command -v "$PY" >/dev/null 2>&1; then
  pyver="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  maj="$("$PY" -c 'import sys;print(sys.version_info[0])')"
  min="$("$PY" -c 'import sys;print(sys.version_info[1])')"
  if [ "$maj" -gt 3 ] || { [ "$maj" -eq 3 ] && [ "$min" -ge 10 ]; }; then
    pass "python $pyver (>= 3.10, as the kernel documents)"
  elif [ "$maj" -eq 3 ] && [ "$min" -ge 9 ]; then
    warn "python $pyver — the suite passes on 3.9 but the kernel documents 3.10+; install 3.10+ on the server"
  else
    fail "python $pyver is too old — need 3.10+"
  fi
else
  fail "python3 not found on PATH"
fi

echo
echo "== 2. dependencies =="
for mod in fastapi uvicorn pydantic dotenv requests; do
  if "$PY" -c "import $mod" >/dev/null 2>&1; then
    pass "import $mod"
  else
    fail "missing python package '$mod' (pip install -r requirements.txt)"
  fi
done

echo
echo "== 3. contract files =="
for f in engine/capsule_engine/bridge.py \
         engine/shared_kernel/schemas/tool_bridge.request.schema.json \
         engine/shared_kernel/schemas/tool_bridge.response.schema.json \
         config/governance.json \
         engine/orchestration/api.py; do
  if [ -f "$f" ]; then pass "present $f"; else fail "missing $f (incomplete checkout?)"; fi
done

echo
echo "== 4. configuration =="
if [ -f .env ]; then
  pass ".env present"
else
  fail ".env missing — cp .env.example .env and fill it in"
fi

# --- signing secret: without it no capability key can ever be minted or
# verified, which silently reduces the whole permission system to the legacy
# shared token. This is the single most important check in the file.
if env_present AGENCY_CAPABILITY_SECRET; then
  sec="$(env_get AGENCY_CAPABILITY_SECRET)"
  slen=$(printf '%s' "$sec" | wc -c | tr -d ' ')
  case "$sec" in change-me*|xxxxxxxx*|"") fail "AGENCY_CAPABILITY_SECRET is a placeholder — generate: python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"" ;;
    *) if [ "$slen" -ge 32 ]; then pass "AGENCY_CAPABILITY_SECRET set ($slen bytes) — scoped keys can be minted"
       else fail "AGENCY_CAPABILITY_SECRET is only $slen bytes — need >= 32"; fi ;;
  esac
else
  fail "AGENCY_CAPABILITY_SECRET absent — capability keys cannot be minted; every permission collapses to the legacy token"
fi

# --- the omnipotent token's kill switch
if env_present AGENCY_LEGACY_ROOT_TOKEN; then
  sw="$(env_get AGENCY_LEGACY_ROOT_TOKEN | tr '[:upper:]' '[:lower:]')"
  case "$sw" in
    off|0|false|no|disabled) pass "AGENCY_LEGACY_ROOT_TOKEN=off — only signed capability keys are accepted" ;;
    on|1|true|yes|enabled)   warn "AGENCY_LEGACY_ROOT_TOKEN=$sw — one shared token satisfies EVERY check; mint keys, then set it to 'off'" ;;
    *)                       warn "AGENCY_LEGACY_ROOT_TOKEN='$sw' is unrecognised (treated as OFF, fail closed)" ;;
  esac
else
  warn "AGENCY_LEGACY_ROOT_TOKEN unset — runtime default is ENABLED: one token answers every route"
fi

# --- the legacy token itself must not be the shipped placeholder
if env_present AGENCY_API_TOKEN; then
  tok="$(env_get AGENCY_API_TOKEN)"
  if [ -z "$tok" ]; then
    warn "AGENCY_API_TOKEN empty — no shared fallback; capability keys are the only way in"
  elif [ "$tok" = "change-me-to-a-long-random-token" ]; then
    fail "AGENCY_API_TOKEN is the template placeholder — generate: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
  else
    tlen=$(printf '%s' "$tok" | wc -c | tr -d ' ')
    if [ "$tlen" -ge 24 ]; then pass "AGENCY_API_TOKEN set ($tlen bytes)"
    else fail "AGENCY_API_TOKEN is only $tlen bytes — need >= 24"; fi
  fi
else
  warn "AGENCY_API_TOKEN absent — the cockpit has no credential at all"
fi

# --- audit signing secret; falls back to the capability secret, so a missing
# one is a hygiene warning (one key signing two different things), not a stop.
if env_present AGENCY_AUDIT_SECRET && [ -n "$(env_get AGENCY_AUDIT_SECRET)" ]; then
  pass "AGENCY_AUDIT_SECRET set — the audit trail has its own key"
else
  warn "AGENCY_AUDIT_SECRET unset — audit entries fall back to AGENCY_CAPABILITY_SECRET"
fi

if env_present AGENCY_OPERATOR; then
  op="$(env_get AGENCY_OPERATOR)"
  if [ -n "$op" ]; then pass "AGENCY_OPERATOR set — decisions will be attributed to '$op'"
  else warn "AGENCY_OPERATOR empty — decisions will be attributed to 'legacy-token'"; fi
else
  warn "AGENCY_OPERATOR unset — decisions attributed to 'legacy-token'"
fi

echo
echo "== 5. tool gears (closed verbs, open tools) =="
if env_present AGENCY_BRIDGE_URL && [ -n "$(env_get AGENCY_BRIDGE_URL)" ]; then
  pass "AGENCY_BRIDGE_URL set — every verb has a default endpoint"
elif env_present AGENCY_BRIDGE_URL_SOCIAL_PUBLISH && [ -n "$(env_get AGENCY_BRIDGE_URL_SOCIAL_PUBLISH)" ]; then
  warn "only AGENCY_BRIDGE_URL_SOCIAL_PUBLISH set — publishing works, other verbs stay unconfigured"
elif [ "$(env_get AGENCY_OFFLINE_MODE)" = "1" ]; then
  pass "AGENCY_OFFLINE_MODE=1 — deterministic local gears (no network, no cost)"
else
  warn "no AGENCY_BRIDGE_URL* and no AGENCY_OFFLINE_MODE — every gear call will fail closed (correct, but nothing will run)"
fi

echo
echo "== 6. data & ports =="
for d in config data \
         data/capsules/spool data/capsules/sandbox data/assets/vault \
         data/domains data/governance/approval_gates/gate_queue data/index data/queue \
         .company_up; do
  if [ -d "$d" ]; then
    if [ -w "$d" ]; then pass "writable $d/"
    else fail "$d exists but is not writable by $(id -un)"; fi
  else
    mkdir -p "$d" 2>/dev/null && pass "created $d/" || fail "cannot create $d/"
  fi
done

# PORT may be exported as 0 by the environment (up.sh guards this too), and an
# empty/absent PORT in .env means the launcher default — resolve both before
# probing, or we would probe the wrong socket and report a false free port.
port="${PORT:-8000}"
if [ -f .env ] && grep -q '^PORT=' .env; then p="$(env_get PORT)"; [ -n "$p" ] && port="$p"; fi
[ "${port:-}" = "0" ] && port=8000
if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$port" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  warn "port $port is already in use (a previous run?) — 'bash ops/bootstrap/up.sh down' first"
elif command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$port "; then
  warn "port $port is already in use (a previous run?) — 'bash ops/bootstrap/up.sh down' first"
else
  pass "port $port free"
fi

# HOST decides whether the API is reachable from the network at all: 127.0.0.1
# means "only a reverse proxy on this host can talk to it", 0.0.0.0 means
# "exposed". Say which one this tree resolves to instead of guessing.
host="${HOST:-}"; [ -f .env ] && grep -q '^HOST=' .env && host="$(env_get HOST)"
if [ -z "$host" ]; then
  warn "HOST unset in .env — server_boot.sh binds 0.0.0.0 while the systemd unit falls back to 127.0.0.1; set HOST explicitly so the server cannot silently bind the wrong one"
else
  case "$host" in
    0.0.0.0)     pass "bind host 0.0.0.0 (all interfaces — put nginx/TLS in front)" ;;
    127.0.0.1)   pass "bind host 127.0.0.1 (reverse proxy on the same host)" ;;
    *)           pass "bind host $host" ;;
  esac
fi

echo
echo "== 7. import contract =="
# The exact import the service performs on start (PYTHONPATH=engine:root). A
# tree that cannot import its own API must never be reported as ready.
if "$PY" -c 'import sys; sys.path[:0] = ["engine", "."]; import orchestration.api' >/dev/null 2>&1; then
  pass "engine imports cleanly (orchestration.api)"
else
  fail "engine fails to import — check PYTHONPATH (engine/) and requirements.txt: $PY -c 'import orchestration.api'"
fi

echo
echo "----------------------------------------------------------------"
printf '%s PASS · %s WARN · %s FAIL\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo "NOT READY — fix every FAIL above before the server takes traffic."
  exit 1
fi
if [ "$WARN" -gt 0 ]; then
  echo "READY WITH RISKS — reads each WARN; it runs, but not as safely as it could."
else
  echo "READY — this host satisfies the foundation gate."
fi
exit 0
