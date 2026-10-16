#!/bin/bash
# =============================================================================
# install.sh — the FIRST BRICK: put the company engine on a server and run it.
#
#   sudo bash ops/deploy/install.sh                 # install + configure + start
#   sudo bash ops/deploy/install.sh --dry-run       # show what it would do
#   sudo bash ops/deploy/install.sh --no-start      # install and configure only
#   sudo bash ops/deploy/install.sh --prefix /opt/ce
#
# Target: Oracle Linux 8/9/10 (RHEL family, systemd, dnf). It also works on
# any systemd host with python3 + pip; the OS probe only refuses when the
# host has neither systemd nor dnf, because then the units below are
# meaningless and we would be installing something we cannot run.
#
# What it does, in order, and never out of order:
#   1. lay down the tree at $PREFIX (seeded from this checkout if absent)
#   2. create a venv and install requirements.txt
#   3. create .env from .env.example ONLY if it does not exist, generating a
#      real capability secret + audit secret + API token — it never overwrites
#      an existing .env, and never prints a secret
#   4. run ops/deploy/preflight.sh — the gate; a FAIL stops the install
#   5. install the systemd units (python/bash paths resolved to this host)
#   6. enable + start, unless --no-start
#
# Standard library / coreutils only.
# =============================================================================
set -euo pipefail

PREFIX="/srv/company-engine"
DRY=0; START=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --no-start) START=0 ;;
    --prefix) PREFIX="${2:?--prefix needs a path}"; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(dirname "$HERE")"

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s ==\n' "$*"; }
# what we WOULD do in --dry-run, and what we DO otherwise
run()  { if [ "$DRY" = 1 ]; then say "DRY  $*"; else eval "$@"; fi; }

step "1. host"
if [ "$(id -u)" -ne 0 ] && [ "$DRY" = 0 ]; then
  echo "FAIL  run as root: sudo bash ops/deploy/install.sh" >&2; exit 1
fi

# The engine runs as a normal account, never root. `sudo bash install.sh` from
# the deploy user means SUDO_USER; a bare root shell falls back to the usual
# cloud images' default account.
RUNUSER="${SUDO_USER:-$(id -un)}"
if [ "$RUNUSER" = "root" ]; then
  for candidate in ubuntu ec2-user opc debian admin; do
    if id "$candidate" >/dev/null 2>&1; then RUNUSER="$candidate"; break; fi
  done
fi
if [ "$RUNUSER" = "root" ]; then
  say "WARN  no non-root account found — the service would run as root; create a user and re-run"
else
  say "PASS  service account: $RUNUSER"
fi
# A dry run is a PREVIEW and must work on any host — including a laptop with
# no systemd — so host requirements are only enforced when we would actually
# install. Otherwise --dry-run could never be used to read the plan.
if command -v systemctl >/dev/null 2>&1; then
  say "PASS  systemd present"
else
  if [ "$DRY" = 1 ]; then
    say "WARN  no systemd here — real install would refuse (this is a preview)"
  else
    echo "FAIL  no systemd on this host — the units in ops/deploy/ cannot run here" >&2; exit 1
  fi
fi
if command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
  say "PASS  dnf/yum present (RHEL family)"
else
  say "WARN  no dnf/yum — assuming dependencies are already installed"
fi

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "FAIL  python3 not found — dnf install -y python3 python3-pip" >&2; exit 1; }
PYV="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
say "PASS  python3 $PYV at $PY"
if "$PY" -c 'import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,10) else 1)'; then
  say "PASS  >= 3.10 as the kernel documents"
else
  say "WARN  $PYV — the suite passes on 3.9 but install 3.10+ on the server (dnf install -y python3.11)"
fi

step "2. tree at $PREFIX"
if [ -d "$PREFIX" ] && [ -f "$PREFIX/engine/orchestration/api.py" ]; then
  say "PASS  existing install at $PREFIX (leaving source untouched)"
else
  run "mkdir -p '$PREFIX'"
  # Copy the tree, but NEVER the operator's own secrets or runtime state: the
  # user registry and the bootstrap admin password are the two files that must
  # not travel between machines, and the work data should start empty.
  EXCLUDES=(--exclude=.git --exclude=.env --exclude=.company_up
            --exclude=__pycache__ --exclude='*.pyc' --exclude=.DS_Store
            --exclude=config/users.json --exclude=config/.bootstrap_admin_password
            --exclude='config/.*.lock'
            --exclude=data/index --exclude=data/assets/vault --exclude=data/capsules
            --exclude=data/queue --exclude=data/memory
            --exclude='data/domains/*/tickets'
            --exclude=data/governance/approval_gates/gate_queue)
  if [ "$DRY" = 1 ]; then
    say "DRY  tar source -> $PREFIX (excluding .git .env .company_up users.json .bootstrap_admin_password and all runtime data)"
  else
    (cd "$SRC" && tar -cf - "${EXCLUDES[@]}" .) | (cd "$PREFIX" && tar -xf -)
    say "PASS  source laid down (secrets and runtime state excluded)"
  fi
fi

step "3. virtualenv + dependencies"
VENV="$PREFIX/.venv"
run "test -d '$VENV' || $PY -m venv '$VENV'"
VPY="$VENV/bin/python"
if [ "$DRY" = 1 ]; then
  say "DRY  $VPY -m pip install -r $PREFIX/requirements.txt"
else
  "$VPY" -m pip install --quiet --upgrade pip
  "$VPY" -m pip install --quiet -r "$PREFIX/requirements.txt"
  say "PASS  dependencies installed into $VENV"
fi

step "4. secrets (.env)"
if [ -f "$PREFIX/.env" ]; then
  say "PASS  .env already exists — NOT modified"
else
  if [ "$DRY" = 1 ]; then
    say "DRY  cp .env.example .env and generate capability secret + audit secret + api token"
  else
    umask 077
    cp "$PREFIX/.env.example" "$PREFIX/.env"
    CAP_SECRET="$("$VPY" -c 'import secrets;print(secrets.token_urlsafe(48))')"
    AUDIT_SECRET="$("$VPY" -c 'import secrets;print(secrets.token_urlsafe(48))')"
    API_TOKEN="$("$VPY" -c 'import secrets;print(secrets.token_urlsafe(32))')"
    # sed -i in place; the values are never echoed back
    sed -i "s|^AGENCY_CAPABILITY_SECRET=.*|AGENCY_CAPABILITY_SECRET=$CAP_SECRET|" "$PREFIX/.env"
    sed -i "s|^AGENCY_AUDIT_SECRET=.*|AGENCY_AUDIT_SECRET=$AUDIT_SECRET|" "$PREFIX/.env"
    sed -i "s|^AGENCY_API_TOKEN=.*|AGENCY_API_TOKEN=$API_TOKEN|" "$PREFIX/.env"
    # bind localhost by default: a reverse proxy (Caddy/nginx) publishes it.
    grep -q '^HOST=' "$PREFIX/.env" || printf '\nHOST=127.0.0.1\n' >> "$PREFIX/.env"
    # a fresh install has no legacy clients to keep alive — retire it now
    sed -i "s|^AGENCY_LEGACY_ROOT_TOKEN=.*|AGENCY_LEGACY_ROOT_TOKEN=off|" "$PREFIX/.env"
    sed -i "s|^AGENCY_OPERATOR=.*|AGENCY_OPERATOR=$RUNUSER|" "$PREFIX/.env"
    chmod 600 "$PREFIX/.env"
    say "PASS  .env created (0600): capability secret, audit secret, API token, legacy token OFF"
    say "      the token values are in $PREFIX/.env — read them there, not here"
  fi
fi

step "4b. vault layout + read index"
# The service creates these lazily, but a tree that starts empty should still
# start ready: the spool, the gate queue and the SQLite read index are laid
# down here so the first request never has to build them.
if [ "$DRY" = 1 ]; then
  say "DRY  create vault dirs and prime $PREFIX/data/index/.read_index.sqlite3"
else
  mkdir -p "$PREFIX"/data/capsules/spool "$PREFIX"/data/capsules/sandbox \
           "$PREFIX"/data/assets/vault "$PREFIX"/data/domains \
           "$PREFIX"/data/governance/approval_gates/gate_queue "$PREFIX"/data/index \
           "$PREFIX"/data/queue "$PREFIX"/config
  if (cd "$PREFIX" && PYTHONPATH="$PREFIX/engine:$PREFIX" "$VPY" \
        -c 'from orchestration.read_index import rebuild; rebuild()' >/dev/null 2>&1); then
    say "PASS  vault layout ready, read index primed"
  else
    say "WARN  read index not primed — it rebuilds on first use"
  fi
fi

step "5. foundation gate"
if [ "$DRY" = 1 ]; then
  say "DRY  bash $PREFIX/ops/deploy/preflight.sh   (a FAIL aborts the install)"
else
  if bash "$PREFIX/ops/deploy/preflight.sh"; then
    say "PASS  preflight"
  else
    echo "FAIL  preflight refused the host — fix the FAILs above, then re-run" >&2
    exit 1
  fi
fi

step "6. systemd units"
BASH_BIN="$(command -v bash)"
VPY_ABS="$VENV/bin/python3"
[ -x "$VPY_ABS" ] || VPY_ABS="$PY"
RGROUP="$(id -gn "$RUNUSER" 2>/dev/null || echo "$RUNUSER")"
# The units run as the deploy user, not root, and systemd reads the tree from
# that account — so the tree must belong to it.
run "chown -R $RUNUSER:$RGROUP '$PREFIX'"
for unit in company-engine company-engine-pulse; do
  src="$PREFIX/ops/deploy/$unit.service"
  dst="/etc/systemd/system/$unit.service"
  if [ "$DRY" = 1 ]; then
    say "DRY  render $src -> $dst (user=$RUNUSER python=$VPY_ABS bash=$BASH_BIN)"
    continue
  fi
  sed -e "s#/usr/local/bin/python3#$VPY_ABS#g" \
      -e "s#/usr/bin/bash#$BASH_BIN#g" \
      -e "s#/usr/local/bin/bash#$BASH_BIN#g" \
      -e "s#/srv/company-engine#$PREFIX#g" \
      -e "s#^User=.*#User=$RUNUSER#" \
      -e "s#^Group=.*#Group=$RGROUP#" "$src" > "$dst"
  say "PASS  installed $unit.service"
done
if [ "$DRY" = 0 ]; then
  run "systemctl daemon-reload"
  run "systemctl enable company-engine company-engine-pulse"
fi

if [ "$START" = 1 ] && [ "$DRY" = 0 ]; then
  step "7. start"
  run "systemctl restart company-engine"
  run "systemctl restart company-engine-pulse"
  sleep 4
  if systemctl is-active --quiet company-engine; then
    say "PASS  company-engine active"
  else
    echo "FAIL  not active — journalctl -u company-engine -n 60" >&2
    exit 1
  fi
  RUN_PORT="$(sed -n 's/^PORT=//p' "$PREFIX/.env" 2>/dev/null | head -1)"
  RUN_HOST="$(sed -n 's/^HOST=//p' "$PREFIX/.env" 2>/dev/null | head -1)"
  say ""
  say "  engine  : http://${RUN_HOST:-127.0.0.1}:${RUN_PORT:-8000}/"
  say "  health  : curl -s http://127.0.0.1:${RUN_PORT:-8000}/api/health"
  say "  first login user: abdallah  (password in $PREFIX/config/.bootstrap_admin_password)"
  say "  status  : systemctl status company-engine"
  say "  logs    : journalctl -u company-engine -f"
  say ""
  say "  The engine binds ${RUN_HOST:-127.0.0.1}; publish it with a TLS proxy"
  say "  (Caddy/nginx) instead of opening the port, and keep"
  say "  AGENCY_LEGACY_ROOT_TOKEN=off (install.sh sets it to off by default)."
else
  say "not started (--no-start or --dry-run). Next:"
  say "  systemctl restart company-engine && systemctl status company-engine"
fi
