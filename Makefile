# Makefile — short commands for the quality gate and the runtime.
# Every target is a thin wrapper; the logic lives in scripts/ and docs/.

PY ?= python3
ENV_OFFLINE := PYTHONPATH=engine:. AGENCY_OFFLINE_MODE=1

.PHONY: help test verify run stop status cycle pulse clean docs

help:
	@echo "make test     — full test suite (offline)"
	@echo "make verify   — syntax + tests + invariants + hygiene (the gate)"
	@echo "make run      — boot the API server"
	@echo "make stop     — stop the API server"
	@echo "make status   — health + pid"
	@echo "make cycle    — one full capsule cycle, offline"
	@echo "make pulse    — one factory beat (manual only)"
	@echo "make clean    — remove caches and stray artifacts"
	@echo "make docs     — list the knowledge base"

test:
	@$(ENV_OFFLINE) $(PY) -m unittest discover -s tests -p "test_*.py"

verify:
	@bash scripts/verify.sh

run:
	@bash server_boot.sh

stop:
	@bash server_boot.sh --stop

status:
	@bash server_boot.sh --status

cycle:
	@AGENCY_OFFLINE_MODE=1 $(PY) ops/tools/post_cycle_trial.py

pulse:
	@PYTHONPATH=engine:. $(PY) engine/orchestration/pulse.py

clean:
	@find . -name ".DS_Store" -not -path "./.git/*" -delete
	@find . -name "__pycache__" -not -path "./.git/*" -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.pyc" -not -path "./.git/*" -delete
	@rm -rf .pytest_cache
	@rm -f engine/orchestration/pulse.log engine/orchestration/pulse_stderr.log
	@echo "cleaned"

docs:
	@ls -1 docs/
