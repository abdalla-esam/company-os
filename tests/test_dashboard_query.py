"""Dashboard query tests: /api/radar/tasks and /api/stats show both entities.

Fixtures are written into the bootstrap vault *before* the API module is
imported (and before the SQLite read index is first built), mirroring the
file-as-state layout the engine scans: ``<spool>/<capsule>/manifest.json`` and
``<domains>/<sector>/tickets/[stage/]ticket_*.json``.
"""

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import asyncio
import json
import os
import unittest

from tests._bootstrap import DOMAINS, SPOOL


def _write_capsule(capsule_id, state, updated_at):
    capsule_dir = SPOOL / capsule_id
    capsule_dir.mkdir(parents=True, exist_ok=True)
    (capsule_dir / "manifest.json").write_text(
        json.dumps(
            {
                "capsule_id": capsule_id,
                "state": state,
                "created_at": "2026-10-10T09:00:00+00:00",
                "updated_at": updated_at,
                "attempt": 1,
                "history": [],
                "domain": "marketing",
                "metadata": {"domain": "marketing", "topic": capsule_id},
            }
        ),
        encoding="utf-8",
    )


def _write_ticket(ticket_id, state, stage="", created_at="2026-10-11T08:00:00+00:00"):
    tickets_dir = DOMAINS / "marketing" / "tickets"
    if stage:
        tickets_dir = tickets_dir / stage
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / f"ticket_{ticket_id}.json").write_text(
        json.dumps(
            {
                "ticket_id": ticket_id,
                "client_id": "probe",
                "sector": "marketing",
                "request": f"probe {ticket_id}",
                "state": state,
                "created_at": created_at,
            }
        ),
        encoding="utf-8",
    )


# --- fixtures (written before any engine import touches the vault) -----------
_write_capsule("C-ACTIVE", "pending_approval", "2026-10-12T10:05:00+00:00")
_write_capsule("C-TERMINAL", "published", "2026-10-12T09:00:00+00:00")
_write_ticket("T-OPEN", "open")
_write_ticket("T-DRAFT", "draft", stage="scratch")
_write_ticket("T-DONE", "done", stage="approved")

from orchestration import api  # noqa: E402  — env must be set first


CAPSULE_ROW_FIELDS = {
    "capsule_id",
    "domain",
    "state",
    "updated_at",
    "running",
    "active_step",
    "active_gear",
    "progress_percent",
    "gates_total",
    "gates_passed",
    "active_gate",
    "active_gate_type",
    "checklist",
    "tokens",
    "estimated_cost_usd",
    "path",
}


def _radar():
    return asyncio.run(api.executive_radar_tasks(limit=500))


def _stats():
    return asyncio.run(api.get_system_stats())


class RadarShowsBothEntities(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _radar()
        cls.rows = cls.result["tasks"]

    def test_every_row_carries_a_kind(self):
        self.assertTrue(self.rows)
        for row in self.rows:
            self.assertIn(row["kind"], ("ticket", "capsule"))

    def test_both_entities_are_present(self):
        self.assertEqual({row["kind"] for row in self.rows}, {"ticket", "capsule"})

    def test_capsule_rows_keep_every_existing_field(self):
        # Additive contract: nothing the cockpit already reads was renamed or
        # dropped — a capsule row is the old row plus `kind`.
        for row in self.rows:
            if row["kind"] == "capsule":
                self.assertTrue(CAPSULE_ROW_FIELDS <= set(row), set(row))
                self.assertEqual(row["capsule_id"], "C-ACTIVE")

    def test_capsules_filter_on_capsule_states_only(self):
        # `published` is terminal for a capsule and must stay excluded even
        # though `published` is a valid *ticket* state.
        ids = [r["capsule_id"] for r in self.rows if r["kind"] == "capsule"]
        self.assertEqual(ids, ["C-ACTIVE"])

    def test_tickets_filter_on_ticket_states_only(self):
        ids = [r["ticket_id"] for r in self.rows if r["kind"] == "ticket"]
        # open + draft are active; `done` is terminal for a ticket.
        self.assertCountEqual(ids, ["T-OPEN", "T-DRAFT"])

    def test_additive_ordering_capsules_first_then_tickets(self):
        # Existing capsule ordering is untouched; tickets are appended.
        kinds = [r["kind"] for r in self.rows]
        first_ticket = kinds.index("ticket")
        last_capsule = len(kinds) - 1 - kinds[::-1].index("capsule")
        self.assertLess(last_capsule, first_ticket)

    def test_ticket_rows_mirror_the_capsule_row_shape(self):
        for row in self.rows:
            if row["kind"] == "ticket":
                self.assertTrue({"ticket_id", "kind", "state"} <= set(row))
                # honest zeroed telemetry — a ticket has no tokens/cost/progress
                self.assertEqual(row["progress_percent"], 0.0)
                self.assertEqual(row["tokens"], {"total_tokens": 0, "calls": 0})

    def test_totals_count_both_entities(self):
        self.assertEqual(self.result["totals"]["tasks"], len(self.rows))
        self.assertGreaterEqual(self.result["totals"]["tasks"], 3)


class RadarFallsBackToFilesWhenIndexIsOff(unittest.TestCase):
    def test_file_scan_returns_both_entities_too(self):
        previous = os.environ.get("AGENCY_INDEX")
        os.environ["AGENCY_INDEX"] = "off"
        try:
            result = _radar()
            self.assertEqual(result["source"], "files")
            rows = result["tasks"]
            self.assertEqual({r["kind"] for r in rows}, {"ticket", "capsule"})
            capsule_ids = [r["capsule_id"] for r in rows if r["kind"] == "capsule"]
            ticket_ids = [r["ticket_id"] for r in rows if r["kind"] == "ticket"]
            self.assertEqual(capsule_ids, ["C-ACTIVE"])          # published excluded
            self.assertCountEqual(ticket_ids, ["T-OPEN", "T-DRAFT"])  # done excluded
        finally:
            if previous is None:
                os.environ.pop("AGENCY_INDEX", None)
            else:
                os.environ["AGENCY_INDEX"] = previous


class StatsCountsBothEntities(unittest.TestCase):
    def test_index_backed_stats(self):
        stats = _stats()
        self.assertEqual(stats["index"], "sqlite")
        self.assertEqual(stats["pending_capsules"], 1)
        self.assertEqual(stats["total_tickets"], 3)  # root + scratch + approved

    def test_file_fallback_counts_tickets_in_every_stage_dir(self):
        # Regression: the fallback glob only matched legacy root tickets, so
        # scratch/approved tickets vanished from the dashboard with the index off.
        previous = os.environ.get("AGENCY_INDEX")
        os.environ["AGENCY_INDEX"] = "off"
        try:
            stats = _stats()
            self.assertEqual(stats["index"], "files")
            self.assertEqual(stats["total_tickets"], 3)
            self.assertEqual(stats["pending_capsules"], 1)
        finally:
            if previous is None:
                os.environ.pop("AGENCY_INDEX", None)
            else:
                os.environ["AGENCY_INDEX"] = previous


if __name__ == "__main__":
    unittest.main()
