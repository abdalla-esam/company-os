"""An approved gate must accept the ticket that raised it.

``auto_ideator`` writes its ticket *before* a human decides, so the ticket
holds the human seat (``pending_approval`` + ``needs_gate``) while the gate is
still open. Approving the gate is that ticket's decision — but nothing applied
it: ``settle`` skipped every gate a ticket already claimed, so the idea sat at
the human seat forever. The home kept asking for a decision that had already
been made, and no capsule was ever built for it.

Found on the live trial: a real gate approved by a human at 17:43, its ticket
still ``needs_me`` on the home afterwards, and no code path that could clear
it. The three sector trackers each carry their own copy of ``settle_gates``, so
every test here runs once per sector.

The accept is the shape :func:`save_ticket` writes for a settled gate
(``state: open`` + ``decision.status: accepted``) — the shape the dispatcher
and the settle bridge act on — so the ticket becomes work, not just quiet.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault

from tests._bootstrap import REPO_ROOT

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SECTORS = ("marketing", "client_management", "ideation")
GATE_ID = "G-20260101T000000Z-claimant1"
TICKET_ID = "T-20260101T000000Z-claimant1"


def _load_tracker(sector: str):
    """Import ``data/domains/<sector>/tracker_script.py`` by path.

    The trackers import their own ``agent_script`` sibling (they are subprocess
    scripts, not packages), so the sector directory goes on ``sys.path`` first.
    The sibling is dropped from the module cache before the load: two sectors
    share that name and the second load would otherwise run the first one's.
    """
    path = REPO_ROOT / "data" / "domains" / sector / "tracker_script.py"
    sector_dir = str(path.parent)
    if sector_dir not in sys.path:
        sys.path.insert(0, sector_dir)
    sys.modules.pop("agent_script", None)
    spec = importlib.util.spec_from_file_location(f"settle_tracker_{sector}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SettleAcceptsItsClaimant(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="so_test_settle_")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tickets = self.tmp / "tickets"
        self.gate_dir = self.tmp / "governance"
        self.gate_queue = self.gate_dir / "gate_queue"
        self.tickets.mkdir(parents=True)
        self.gate_queue.mkdir(parents=True)

    # -- fixtures ---------------------------------------------------------- #

    def _reset(self) -> None:
        for directory in (self.tickets, self.gate_queue):
            for stale in directory.iterdir():
                stale.unlink()

    def _tracker(self, sector: str):
        """The tracker with every path it resolves redirected into the temp tree.

        ``settle_gates`` reads the sector's own directory (``_HERE``) and the
        ticket/gate locations; nothing here may reach the repository's real
        ``data/``.
        """
        tracker = _load_tracker(sector)
        tracker._HERE = str(self.tmp)
        tracker.TICKETS_DIR = str(self.tickets)
        tracker.GATE_DIR = str(self.gate_dir)
        return tracker

    def _claimant(self, sector: str, *, state: str = "pending_approval") -> Path:
        """A producer's ticket: written before the gate was decided."""
        ticket = {
            "ticket_id": TICKET_ID,
            "client_id": "auto_ideator",
            "request": "[Proposed Title]: a proposed idea",
            "decision": {
                "status": "needs_gate",
                "risk_tier": "external",
                "reason": "autonomous proposal",
                "ticket": f"ticket_auto_ideator_{TICKET_ID}.json",
            },
            "created_at": "2026-01-01T00:00:00+00:00",
            "state": state,
            "via": GATE_ID,
            "history": [
                {
                    "at": "2026-01-01T00:00:00+00:00",
                    "from_state": None,
                    "to_state": state,
                    "by": "auto_ideator",
                    "why": "autonomous idea generated",
                }
            ],
        }
        path = self.tickets / f"ticket_auto_ideator_{TICKET_ID}.json"
        path.write_text(json.dumps(ticket, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _gate(self, sector: str, *, state: str = "closed", decision: str = "approved") -> Path:
        entry = {
            "gate_id": GATE_ID,
            "source": {
                "sector": sector,
                "client_id": "auto_ideator",
                "request": "[Proposed Title]: a proposed idea",
            },
            "risk_tier": "external",
            "reason": "autonomous proposal",
            "opened_at": "2026-01-01T00:00:00+00:00",
            "state": state,
        }
        if state == "closed":
            entry.update({
                "decision": decision,
                "decided_by": "abdallah",
                "decided_at": "2026-01-01T01:00:00+00:00",
            })
        path = self.gate_queue / f"gate_{GATE_ID}.json"
        path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _settle(self, tracker) -> None:
        # settle_gates prints its SettleOutput; the assertions read the disk.
        with contextlib.redirect_stdout(io.StringIO()):
            tracker.settle_gates()

    # -- tests ------------------------------------------------------------- #

    def test_an_approved_gate_accepts_its_claimant(self) -> None:
        for sector in SECTORS:
            with self.subTest(sector=sector):
                self._reset()
                tracker = self._tracker(sector)
                path = self._claimant(sector)
                self._gate(sector)

                self._settle(tracker)

                on_disk = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(on_disk["state"], "open")
                self.assertEqual(on_disk["decision"]["status"], "accepted")
                self.assertEqual(on_disk["via"], GATE_ID)
                self.assertIn(GATE_ID, on_disk["decision"]["reason"])
                self.assertIn("abdallah", on_disk["decision"]["reason"])
                # the decision is traced, and attributed to the pull not the gate
                self.assertEqual(on_disk["history"][-1]["from_state"], "pending_approval")
                self.assertEqual(on_disk["history"][-1]["to_state"], "open")
                self.assertEqual(on_disk["history"][-1]["by"], "settle")
                self.assertIn(GATE_ID, on_disk["history"][-1]["why"])
                # accepted in place: the claim is not re-settled into a twin
                self.assertEqual([p.name for p in self.tickets.glob("ticket_*.json")], [path.name])

    def test_an_approved_gate_without_a_claimant_still_settles_a_ticket(self) -> None:
        """The behaviour that already worked must keep working."""
        for sector in SECTORS:
            with self.subTest(sector=sector):
                self._reset()
                tracker = self._tracker(sector)
                self._gate(sector)

                self._settle(tracker)

                written = sorted(self.tickets.glob("ticket_*.json"))
                self.assertEqual(len(written), 1)
                settled = json.loads(written[0].read_text(encoding="utf-8"))
                self.assertEqual(settled["state"], "open")
                self.assertEqual(settled["decision"]["status"], "accepted")
                self.assertEqual(settled["via"], GATE_ID)

    def test_a_rejected_gate_leaves_its_claimant_alone(self) -> None:
        for sector in SECTORS:
            with self.subTest(sector=sector):
                self._reset()
                tracker = self._tracker(sector)
                path = self._claimant(sector)
                before = path.read_bytes()
                self._gate(sector, decision="rejected")

                self._settle(tracker)

                self.assertEqual(path.read_bytes(), before)
                self.assertEqual([p.name for p in self.tickets.glob("ticket_*.json")], [path.name])

    def test_an_open_gate_leaves_its_claimant_alone(self) -> None:
        for sector in SECTORS:
            with self.subTest(sector=sector):
                self._reset()
                tracker = self._tracker(sector)
                path = self._claimant(sector)
                before = path.read_bytes()
                self._gate(sector, state="open")

                self._settle(tracker)

                self.assertEqual(path.read_bytes(), before)

    def test_an_already_accepted_claimant_is_not_touched_again(self) -> None:
        """A settled ticket keeps its bytes: the pull is idempotent."""
        for sector in SECTORS:
            with self.subTest(sector=sector):
                self._reset()
                tracker = self._tracker(sector)
                path = self._claimant(sector)
                self._gate(sector)
                self._settle(tracker)

                accepted = path.read_bytes()
                self._settle(tracker)

                self.assertEqual(path.read_bytes(), accepted)


if __name__ == "__main__":
    unittest.main()
