#!/bin/sh
# The pulse is the spirit that keeps the factory alive: pulse.py stays
# one-pass, the loop belongs to the gear. Run from anywhere — the script
# locates the tree from its own path, so the same file works from a
# container (ops/bootstrap/compose.yml) and from systemd.
INTERVAL="${PULSE_INTERVAL:-120}"

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || exit 1
# engine/ holds the packages; the repo root holds config/ and data/.
PYTHONPATH="$ROOT/engine:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

echo "[pulse] factory alive — one beat every ${INTERVAL}s"
while true; do
  python3 orchestration/pulse.py || echo "[pulse] beat failed — the loop continues"
  sleep "$INTERVAL"
done
