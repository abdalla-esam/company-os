"""Tests for orchestration.dispatcher: ticket discovery, handoff, and boundaries.

Tests run against an isolated scratch vault with a stub worker (zero LLM / external calls).
Verifies:
1. A ticket in tickets/approved with a dispatchable state is claimed, handed to the
   stub worker, and written back correctly (outcome=done, context updated, lease released).
2. Human-gate boundary:
   - Tickets in scratch/ are NEVER dispatched.
   - Tickets in non-dispatchable states (needs_gate, done) are NEVER dispatched.
3. In the real checkout, DOMAINS_DIR exists and contains real domain directories.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — isolated vault setup

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests._bootstrap import REPO_ROOT
from orchestration import dispatcher


STUB_WORKER_CODE = """#!/usr/bin/env python3
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}

result = {
    "result": "done",
    "notes": f"stub executed {payload.get('ticket_id')}",
    "context": {"worker_ran": True, "ticket_id": payload.get("ticket_id")}
}
print(json.dumps(result))
"""


class TestDispatcher(unittest.TestCase):
    """Dispatcher test suite with isolated vault and deterministic stub worker."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="so_dispatcher_test_")
        self.domains_dir = Path(self.temp_dir) / "domains"
        self.sector = "test_sector"
        self.sector_dir = self.domains_dir / self.sector
        self.tickets_dir = self.sector_dir / "tickets"
        self.approved_dir = self.tickets_dir / "approved"
        self.scratch_dir = self.tickets_dir / "scratch"

        for d in (self.approved_dir, self.scratch_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Deploy executable stub worker
        self.worker_script = self.sector_dir / "worker_script.py"
        self.worker_script.write_text(STUB_WORKER_CODE, encoding="utf-8")
        self.worker_script.chmod(0o755)

        # Point dispatcher to the isolated domains dir
        self._orig_domains_dir = dispatcher.DOMAINS_DIR
        dispatcher.DOMAINS_DIR = str(self.domains_dir)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        dispatcher.DOMAINS_DIR = self._orig_domains_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_2a_approved_dispatchable_ticket_is_claimed_and_executed(self) -> None:
        """A ticket in tickets/approved with state='open' is claimed and processed."""
        ticket_id = "T-DISP-001"
        ticket_path = self.approved_dir / f"ticket_{ticket_id}.json"
        ticket_data = {
            "ticket_id": ticket_id,
            "sector": self.sector,
            "state": "open",
            "request": "Thought leadership post on sovereignty",
            "via": "G-TEST-GATE-1",
            "attempts": 0,
            "context": {"initial_key": "initial_val"},
        }
        ticket_path.write_text(json.dumps(ticket_data, indent=2), encoding="utf-8")

        # Run one dispatcher pass
        args = argparse.Namespace()
        with io.StringIO() as buf, redirect_stdout(buf):
            dispatcher.cmd_run(args)

        # Assertions on disk state
        self.assertTrue(ticket_path.is_file(), "ticket must exist at original path")
        claim_path = Path(str(ticket_path) + ".claim")
        self.assertFalse(claim_path.exists(), "lease claim file must be removed after run")

        updated = json.loads(ticket_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["state"], "done", "worker result 'done' must be recorded")
        self.assertEqual(updated["attempts"], 0)
        self.assertIn("done_at", updated)
        self.assertIn("context", updated)
        self.assertTrue(updated["context"].get("worker_ran"))
        self.assertEqual(updated["context"].get("initial_key"), "initial_val")

    def test_2b_scratch_or_nondispatchable_tickets_are_not_dispatched(self) -> None:
        """Human-gate boundary: scratch and unapproved/nondispatchable tickets are ignored."""
        # 1. Scratch ticket (unapproved work in scratch/)
        scratch_ticket = self.scratch_dir / "ticket_scratch_001.json"
        scratch_data = {
            "ticket_id": "T-SCRATCH-001",
            "sector": self.sector,
            "state": "open",
            "request": "Draft in scratch",
            "attempts": 0,
        }
        scratch_ticket.write_text(json.dumps(scratch_data, indent=2), encoding="utf-8")

        # 2. Approved ticket but in non-dispatchable state: needs_gate
        needs_gate_ticket = self.approved_dir / "ticket_needs_gate_002.json"
        needs_gate_data = {
            "ticket_id": "T-GATE-002",
            "sector": self.sector,
            "state": "needs_gate",
            "request": "Waiting for human gate",
            "attempts": 0,
        }
        needs_gate_ticket.write_text(json.dumps(needs_gate_data, indent=2), encoding="utf-8")

        # 3. Approved ticket already done
        done_ticket = self.approved_dir / "ticket_done_003.json"
        done_data = {
            "ticket_id": "T-DONE-003",
            "sector": self.sector,
            "state": "done",
            "request": "Already finished work",
            "attempts": 0,
        }
        done_ticket.write_text(json.dumps(done_data, indent=2), encoding="utf-8")

        # Run dispatcher
        with io.StringIO() as buf, redirect_stdout(buf):
            dispatcher.cmd_run(argparse.Namespace())

        # Assert NONE of them were touched or changed state
        after_scratch = json.loads(scratch_ticket.read_text(encoding="utf-8"))
        self.assertEqual(after_scratch["state"], "open")
        self.assertNotIn("done_at", after_scratch)

        after_gate = json.loads(needs_gate_ticket.read_text(encoding="utf-8"))
        self.assertEqual(after_gate["state"], "needs_gate")
        self.assertNotIn("done_at", after_gate)

        after_done = json.loads(done_ticket.read_text(encoding="utf-8"))
        self.assertEqual(after_done["state"], "done")
        self.assertNotIn("context", after_done)

    def test_2c_domains_dir_exists_in_real_checkout(self) -> None:
        """Verify DOMAINS_DIR resolves to data/domains in the real repository layout."""
        real_domains_dir = REPO_ROOT / "data" / "domains"
        self.assertTrue(
            real_domains_dir.is_dir(),
            f"Expected real domains directory to exist at {real_domains_dir}",
        )

        # Verify real domain folders exist
        for expected_sector in ("marketing", "ideation", "client_management"):
            sector_dir = real_domains_dir / expected_sector
            self.assertTrue(
                sector_dir.is_dir(),
                f"Expected sector directory {sector_dir} to exist in checkout",
            )

        # Verify that running dispatcher.py standalone in a child process
        # without any DOMAINS_DIR env override resolves DOMAINS_DIR to an existing directory
        clean_env = dict(os.environ)
        clean_env.pop("DOMAINS_DIR", None)
        clean_env.pop("PYTHONPATH", None)

        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "engine" / "orchestration" / "dispatcher.py"),
                "list",
            ],
            cwd=str(REPO_ROOT),
            env=clean_env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Standalone dispatcher execution failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}",
        )
        self.assertIn("marketing", proc.stdout)
        self.assertIn("ideation", proc.stdout)


if __name__ == "__main__":
    unittest.main()
