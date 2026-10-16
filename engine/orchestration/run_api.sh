#!/bin/bash
# Cockpit launcher — portable (local or server). Sources .env so the server
# sees AGENCY_API_TOKEN, the model keys and the publishing credentials, then
# starts uvicorn. This is the single env source for the API; api.py never
# loads .env itself (that would leak keys into the test process).
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"
export PYTHONPATH="$ROOT"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec "${PYTHON:-python3}" -m uvicorn orchestration.api:app \
  --host "${HOST:-0.0.0.0}" --port "${PORT:-8090}"
