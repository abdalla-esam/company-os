#!/usr/bin/env bash
# Sovereign Agency OS — Root Boot Wrapper
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/ops/server_boot.sh" "$@"
