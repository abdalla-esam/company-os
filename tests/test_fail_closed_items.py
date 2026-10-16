"""Per-item fail closed: an unknown schema_version never crashes a listing,
a rebuild or a dispatcher pass — the item is SKIPPED and REPORTED.

Before Part 4 Phase 3, a ticket/gate entry/capsule carrying a schema_version
this build does not understand crashed the whole surface:

- ``dispatcher.cmd_run`` / ``cmd_list`` raised ``SchemaVersionError`` out of
  the loop (one bad ticket killed the pass and the board);
- ``gate_script.py list`` died with a traceback on one bad gate entry;
- ``read_index.rebuild`` silently INDEXED the bad ticket (no check at all).

Each surface now fails closed per item: bad records are skipped, reported
(skipped list / BAD-SCHEMA row / last_rebuild_skipped()) and everything else
still processes. The unknown file itself is never modified.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault

from tests._bootstrap import DOMAINS, GATE_QUEUE, SPOOL, VAULT

import argparse
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests._bootstrap import REPO_ROOT
from capsule_engine.manifest import new_manifest, save_manifest
from orchestration import dispatcher, read_index
from shared_kernel import schema_version

_GATE_SCRIPT = REPO_ROOT / "data" / "governance" / "approval_gates" / "gate_script.py"
_spec = importlib.util.spec_from_file_location("gate_script_failclosed", _GATE_SCRIPT)
assert _spec is not None and _spec.loader is not None
gate_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_script)

import ops.tools.ticket_mgr as ticket_mgr  # noqa: E402


def _write_ticket(domain: str, name: str, payload: dict, *, stage: str = "approved") -> Path:
    tdir = DOMAINS / domain / "tickets" / stage
    tdir.mkdir(parents=True, exist_ok=True)
    path = tdir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _ticket(ticket_id: str, schema_version_value=None) -> dict:
    data = {
        "ticket_id": ticket_id,
        "state": "pending",
        "request": f"req {ticket_id}",
        "created_at": "2026-10-13T00:00:00+00:00",
    }
    if schema_version_value is not None:
        data["schema_version"] = schema_version_value
    return data


class DispatcherFailClosed(unittest.TestCase):

    def setUp(self) -> None:
        self.tdir = DOMAINS / "marketing" / "tickets" / "approved"
        self.tdir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, DOMAINS / "marketing", ignore_errors=True)
        # deterministic worker absence: dispatcher never reaches a handoff
        self._saved_domains = dispatcher.DOMAINS_DIR
        dispatcher.DOMAINS_DIR = str(DOMAINS)
        self.addCleanup(setattr, dispatcher, "DOMAINS_DIR", self._saved_domains)

    def _run(self) -> dict:
        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatcher.cmd_run(argparse.Namespace())
        return json.loads(buf.getvalue())

    def test_cmd_run_survives_unknown_schema_and_reports_skip(self) -> None:
        _write_ticket("marketing", "ticket_bad.json", _ticket("T-BAD", 99))
        _write_ticket("marketing", "ticket_good.json", _ticket("T-GOOD", 1))

        report = self._run()  # must NOT raise
        skipped = [e for e in report["events"] if e["outcome"] == "skipped"]
        self.assertTrue(
            any("schema_version" in e.get("detail", "") for e in skipped),
            f"unknown-schema ticket must be reported as skipped: {report}",
        )
        # the good ticket was still processed (dispatched to its fate)
        bad_names = {"ticket_bad.json"}
        self.assertTrue(
            any(e["ticket"] not in bad_names for e in report["events"]),
            f"the good ticket must still be processed: {report}",
        )
        # the unknown file is untouched (fail closed = never rewritten)
        on_disk = json.loads(
            (self.tdir / "ticket_bad.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["schema_version"], 99)

    def test_cmd_list_survives_and_marks_bad_schema(self) -> None:
        _write_ticket("marketing", "ticket_bad.json", _ticket("T-BAD", 99))
        _write_ticket("marketing", "ticket_good.json", _ticket("T-GOOD", 1))

        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatcher.cmd_list(argparse.Namespace())
        out = buf.getvalue()
        self.assertIn("BAD-SCHEMA", out)
        self.assertIn("T-GOOD", out)

    def test_reclaim_does_not_crash_on_unknown_schema(self) -> None:
        _write_ticket("marketing", "ticket_bad.json", _ticket("T-BAD", 99))
        events = []
        dispatcher.reclaim_stale(events)  # must NOT raise
        self.assertTrue(
            any("fail-closed" in e.get("detail", "") for e in events),
            events,
        )


class GateListFailClosed(unittest.TestCase):

    def setUp(self) -> None:
        GATE_QUEUE.mkdir(parents=True, exist_ok=True)
        self._saved_queue = gate_script.QUEUE_DIR
        gate_script.QUEUE_DIR = str(GATE_QUEUE)
        self.addCleanup(setattr, gate_script, "QUEUE_DIR", self._saved_queue)
        self.addCleanup(shutil.rmtree, GATE_QUEUE, ignore_errors=True)
        GATE_QUEUE.mkdir(parents=True, exist_ok=True)

    def test_cmd_list_survives_unknown_schema_and_reports(self) -> None:
        (GATE_QUEUE / "gate_bad.json").write_text(json.dumps({
            "gate_id": "G-BAD", "state": "open", "risk_tier": None,
            "reason": "r", "opened_at": "2026-10-13T00:00:00+00:00",
            "schema_version": 99,
        }), encoding="utf-8")
        (GATE_QUEUE / "gate_good.json").write_text(json.dumps({
            "gate_id": "G-GOOD", "state": "open", "risk_tier": None,
            "reason": "r", "opened_at": "2026-10-13T00:00:00+00:00",
            "schema_version": 1,
        }), encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            gate_script.cmd_list(argparse.Namespace(all=True))
        out = buf.getvalue()
        self.assertIn("BAD-SCHEMA", out)
        self.assertIn("G-GOOD", out)
        self.assertIn("skipped (fail-closed)", out)

    def test_cmd_list_subprocess_rc0(self) -> None:
        (GATE_QUEUE / "gate_bad.json").write_text(json.dumps({
            "gate_id": "G-BAD", "state": "open", "risk_tier": None,
            "reason": "r", "opened_at": "2026-10-13T00:00:00+00:00",
            "schema_version": 99,
        }), encoding="utf-8")
        env = dict(os.environ)
        env["GATE_QUEUE_DIR"] = str(GATE_QUEUE)
        proc = subprocess.run(
            [sys.executable, str(_GATE_SCRIPT), "list", "--all"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("BAD-SCHEMA", proc.stdout)


class ReadIndexFailClosed(unittest.TestCase):

    def setUp(self) -> None:
        self.addCleanup(shutil.rmtree, SPOOL, ignore_errors=True)
        self.addCleanup(shutil.rmtree, DOMAINS, ignore_errors=True)
        SPOOL.mkdir(parents=True, exist_ok=True)
        DOMAINS.mkdir(parents=True, exist_ok=True)

    def test_rebuild_skips_and_reports_unknown_schema(self) -> None:
        # one good capsule + one future-schema capsule
        good = SPOOL / "cap_good"
        good.mkdir(parents=True, exist_ok=True)
        save_manifest(good, new_manifest("cap_good"))
        bad = SPOOL / "cap_bad"
        bad.mkdir(parents=True, exist_ok=True)
        bad_manifest = new_manifest("cap_bad")
        bad_manifest["schema_version"] = 99
        (bad / "manifest.json").write_text(json.dumps(bad_manifest), encoding="utf-8")

        # one good ticket + one future-schema ticket
        _write_ticket("marketing", "ticket_good.json", _ticket("T-GOOD", 1))
        _write_ticket("marketing", "ticket_bad.json", _ticket("T-BAD", 99))

        count = read_index.rebuild()  # must NOT raise
        self.assertEqual(count, 1, "only the good capsule is indexed")

        skipped = read_index.last_rebuild_skipped()
        kinds = {item["kind"] for item in skipped}
        self.assertIn("capsule", kinds, skipped)
        self.assertIn("ticket", kinds, skipped)
        for item in skipped:
            self.assertIn("schema_version", item["detail"], item)

        rows = read_index.query_tickets(limit=100, offset=0)
        ids = {row["ticket_id"] for row in rows}
        self.assertIn("T-GOOD", ids)
        self.assertNotIn("T-BAD", ids, "unknown-schema ticket must not be indexed")

    def test_rebuild_report_resets_between_runs(self) -> None:
        good = SPOOL / "cap_good2"
        good.mkdir(parents=True, exist_ok=True)
        save_manifest(good, new_manifest("cap_good2"))
        read_index.rebuild()
        self.assertEqual(read_index.last_rebuild_skipped(), [])


class TicketMgrFailClosed(unittest.TestCase):

    def setUp(self) -> None:
        self._saved_domains = ticket_mgr.DOMAINS_DIR
        ticket_mgr.DOMAINS_DIR = DOMAINS
        self.addCleanup(setattr, ticket_mgr, "DOMAINS_DIR", self._saved_domains)
        self.addCleanup(shutil.rmtree, DOMAINS, ignore_errors=True)

    def test_list_tickets_skips_unknown_schema(self) -> None:
        _write_ticket("marketing", "ticket_good.json", _ticket("T-GOOD", 1))
        _write_ticket("marketing", "ticket_bad.json", _ticket("T-BAD", 99))

        ticket_mgr.LAST_LIST_SKIPPED.clear()
        results = ticket_mgr.list_tickets(stage="approved")
        ids = {r["ticket_id"] for r in results}
        self.assertIn("T-GOOD", ids)
        self.assertNotIn("T-BAD", ids)
        self.assertTrue(
            any("T-BAD" in s["path"] or "ticket_bad" in s["path"]
                for s in ticket_mgr.LAST_LIST_SKIPPED),
            ticket_mgr.LAST_LIST_SKIPPED,
        )


if __name__ == "__main__":
    unittest.main()
