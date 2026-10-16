#!/bin/sh
# The pulse is the spirit that keeps the container alive (bootstrap/architecture.md).
# pulse.py stays one-pass; the loop belongs to the gear.
INTERVAL="${PULSE_INTERVAL:-120}"
echo "[pulse] factory alive — one beat every ${INTERVAL}s"
while true; do
  python3 orchestration/pulse.py || echo "[pulse] beat failed — the loop continues"
  sleep "$INTERVAL"
done
