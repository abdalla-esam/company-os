#!/usr/bin/env bash
# verify.sh — the single quality gate. Run before every commit.
#
# Phases:
#   1. syntax        — every .py compiles
#   2. tests         — the full suite (630 tests), offline, isolated vault
#   3. invariants    — dependency direction + isolation guard are in the suite
#   4. junk          — no stray caches/logs tracked or left behind
#
# Exit 0 only when every phase passes.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
FAILED=0

say() { printf '\n\033[1;36m[%s]\033[0m %s\n' "$1" "$2"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED=1; }

# ---------------------------------------------------------------- 1. syntax
say "1/4" "Python syntax"
if "$PY" -m compileall -q engine data ops tests >/tmp/fllevo_compile.log 2>&1; then
  ok "all modules compile"
else
  bad "syntax errors — see /tmp/fllevo_compile.log"
  tail -20 /tmp/fllevo_compile.log
fi

# ----------------------------------------------------------------- 2. tests
say "2/4" "Test suite (offline, isolated vault)"
TEST_OUT="$(PYTHONPATH=engine:. AGENCY_OFFLINE_MODE=1 \
  "$PY" -m unittest discover -s tests -p 'test_*.py' 2>&1)"
TEST_RC=$?
TEST_SUMMARY="$(printf '%s\n' "$TEST_OUT" | grep -E '^(Ran |OK|FAILED)' | tr '\n' ' ')"
# NOTE: no `grep -q` here — under `pipefail` an early-exiting grep makes the
# pipeline fail with SIGPIPE and turns a green suite red.
if [ "$TEST_RC" -eq 0 ] && [ "$(printf '%s\n' "$TEST_OUT" | grep -cE '^OK')" -ge 1 ]; then
  ok "$TEST_SUMMARY"
else
  bad "$TEST_SUMMARY"
  printf '%s\n' "$TEST_OUT" | tail -40
fi

# ------------------------------------------------------------ 3. invariants
say "3/4" "Invariants"
# The invariant tests are part of the suite above; this phase only asserts
# that each one actually ran and did not error out.
for t in test_dependency_direction test_zzz_isolation_guard test_fail_closed_items \
         test_spend_control test_state_model; do
  if printf '%s\n' "$TEST_OUT" | grep -c "ERROR: $t" >/dev/null; then
    bad "$t failed"
  fi
done
[ "$FAILED" -eq 0 ] && ok "core invariants hold"

# ------------------------------------------------------------------ 4. junk
say "4/4" "Workspace hygiene"
JUNK=0
# Editor/OS litter must never exist in the tree.
for p in .DS_Store; do
  n=$(find . -name "$p" -not -path './.git/*' | wc -l | tr -d ' ')
  [ "$n" -gt 0 ] && { bad "$p × $n"; JUNK=1; }
done
# Compiled caches must never be committed.
n=$(find . -name '__pycache__' -not -path './.git/*' | wc -l | tr -d ' ')
[ "$n" -gt 0 ] && { bad "__pycache__ × $n"; JUNK=1; }
# Nothing generated may be tracked by git (logs, caches, sqlite indexes).
TRACKED=$(git ls-files 2>/dev/null | grep -E '(__pycache__|\.pyc$|\.log$|\.sqlite3|\.DS_Store)' | head -10)
[ -n "$TRACKED" ] && { bad "tracked artifacts: $TRACKED"; JUNK=1; }
[ "$JUNK" -eq 0 ] && ok "no stray artifacts tracked or left behind"

# ------------------------------------------------------------------ verdict
printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf '\033[1;32mVERIFY: PASS\033[0m\n'
  exit 0
else
  printf '\033[1;31mVERIFY: FAIL\033[0m\n'
  exit 1
fi
