"""Tests for read_index archive ticket discovery and directus zero-cost pricing."""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault

import json
import shutil
import unittest
from pathlib import Path

from tests._bootstrap import DOMAINS, VAULT
from orchestration import read_index
from capsule_engine import spend_control


class TestReadIndexArchiveAndDirectus(unittest.TestCase):
    def setUp(self) -> None:
        self._clean_dirs = []

    def tearDown(self) -> None:
        for d in self._clean_dirs:
            shutil.rmtree(d, ignore_errors=True)

    def test_read_index_discovers_dispatcher_monthly_archives(self) -> None:
        """read_index must index tickets placed by dispatcher in <sector>/archive/<month>/."""
        mkt_archive = DOMAINS / "marketing" / "archive" / "2026-10"
        mkt_archive.mkdir(parents=True, exist_ok=True)
        self._clean_dirs.append(DOMAINS / "marketing" / "archive")

        t1 = {
            "schema_version": 1,
            "ticket_id": "ticket_arch_monthly",
            "sector": "marketing",
            "state": "published",
            "request": "Archived monthly ticket",
            "created_at": "2026-10-01T10:00:00Z",
        }
        (mkt_archive / "ticket_arch_monthly.json").write_text(
            json.dumps(t1), encoding="utf-8"
        )

        dev_archive = DOMAINS / "development" / "tickets" / "archive"
        dev_archive.mkdir(parents=True, exist_ok=True)
        self._clean_dirs.append(dev_archive)

        t2 = {
            "schema_version": 1,
            "ticket_id": "ticket_arch_legacy",
            "sector": "development",
            "state": "done",
            "request": "Archived legacy ticket",
            "created_at": "2026-10-02T10:00:00Z",
        }
        (dev_archive / "ticket_arch_legacy.json").write_text(
            json.dumps(t2), encoding="utf-8"
        )

        read_index.rebuild()
        stats = read_index.stats()
        self.assertIsNotNone(stats)
        tickets = stats["tickets"]

        self.assertGreaterEqual(tickets.get("archived", 0), 2)
        by_stage = tickets.get("by_stage", {})
        self.assertGreaterEqual(by_stage.get("archived", 0), 2)

        # Query tickets list to verify individual rows
        rows = read_index.query_tickets(stage="archived")
        found_ids = {r["ticket_id"] for r in rows}
        self.assertIn("ticket_arch_monthly", found_ids)
        self.assertIn("ticket_arch_legacy", found_ids)

        monthly_row = next(r for r in rows if r["ticket_id"] == "ticket_arch_monthly")
        self.assertEqual(monthly_row["sector"], "marketing")
        self.assertEqual(monthly_row["stage"], "archived")

    def test_directus_api_is_priced_at_zero_usd(self) -> None:
        """directus-api in spend.image_costs must resolve to 0.0 USD without depleting budget."""
        cfg = spend_control.load_config(force=True)
        self.assertIn("directus-api", cfg.image_costs)
        self.assertEqual(cfg.image_costs["directus-api"], 0.0)

        estimate, priced, _ = spend_control._estimate(
            spend_control.CATEGORY_TOOL, "directus-api", cfg, None
        )
        self.assertEqual(estimate, 0.0)
        self.assertTrue(priced)


if __name__ == "__main__":
    unittest.main()
