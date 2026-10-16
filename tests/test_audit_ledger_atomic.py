"""Spool ledger durability: no lost appends, chain always verifies.

``append_ledger_head`` is called from overlapping pulses, so its read of
prev/seq and its append must be one critical section. These tests pin:

- fault injection around the append — the ledger must stay byte-for-byte
  intact and still accept the next record (a torn line is reported, never
  silently extended);
- an N-writer race — concurrent appends from many threads must produce a
  ledger whose chain verifies (unique seqs, intact prev links, valid macs)
  with every writer present.

The secret is always passed explicitly so the tests are independent of any
``AGENCY_AUDIT_SECRET`` in the environment.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths

import json
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from capsule_engine import audit

SECRET = b"ledger-durability-test-secret"


class LedgerBase(unittest.TestCase):
    def setUp(self) -> None:
        self.spool = Path(tempfile.mkdtemp(prefix="so_test_ledger_"))
        self.addCleanup(shutil.rmtree, self.spool, ignore_errors=True)

    def _ledger_bytes(self):
        path = audit.ledger_path(self.spool)
        return path.read_bytes() if path.is_file() else b""

    def _craft_chain(self, count):
        """Write ``count`` valid chained entries directly (fast seed)."""
        entries = []
        prev = audit.GENESIS
        for i in range(count):
            record = {
                "at": "2026-10-01T00:00:00+00:00",
                "capsule_id": "cap-%04d" % i,
                "head": "head-%04d" % i,
                "prev": prev,
                "seq": i,
            }
            mac = audit._keyed_mac(SECRET, audit.canonical_bytes(record, audit.ALGO_KEYED))
            record["mac_algo"] = audit.ALGO_KEYED
            record["mac"] = mac
            entries.append(record)
            prev = mac
        path = audit.ledger_path(self.spool)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in entries),
            "utf-8",
        )
        return entries


class LedgerAppendIsAtomic(LedgerBase):
    def test_append_and_verify_chain(self) -> None:
        for i in range(5):
            record = audit.append_ledger_head(
                self.spool, capsule_id=f"cap-{i}", head=f"head-{i}", secret=SECRET
            )
            self.assertEqual(record["seq"], i)
        entries = audit.read_ledger(self.spool)
        status = audit.verify_ledger(entries, secret=SECRET)
        self.assertTrue(status.ok, status.detail)

    def test_append_is_idempotent(self) -> None:
        first = audit.append_ledger_head(self.spool, capsule_id="cap-1", head="h1", secret=SECRET)
        again = audit.append_ledger_head(self.spool, capsule_id="cap-1", head="h1", secret=SECRET)
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertEqual(len(audit.read_ledger(self.spool)), 1)
        # a NEW head for the same capsule is recorded
        moved = audit.append_ledger_head(self.spool, capsule_id="cap-1", head="h2", secret=SECRET)
        self.assertIsNotNone(moved)
        self.assertEqual(moved["seq"], 1)

    def test_fault_during_append_keeps_ledger_intact(self) -> None:
        audit.append_ledger_head(self.spool, capsule_id="cap-1", head="h1", secret=SECRET)
        before = self._ledger_bytes()
        with mock.patch("shared_kernel.atomic_io.os.write", side_effect=OSError("disk fault")):
            with self.assertRaises(OSError):
                audit.append_ledger_head(self.spool, capsule_id="cap-2", head="h2", secret=SECRET)
        self.assertEqual(self._ledger_bytes(), before, "a failed append must leave no partial line")
        # and the chain still extends cleanly afterwards
        audit.append_ledger_head(self.spool, capsule_id="cap-2", head="h2", secret=SECRET)
        status = audit.verify_ledger(audit.read_ledger(self.spool), secret=SECRET)
        self.assertTrue(status.ok, status.detail)

    def test_torn_tail_line_is_reported_not_extended(self) -> None:
        audit.append_ledger_head(self.spool, capsule_id="cap-1", head="h1", secret=SECRET)
        with audit.ledger_path(self.spool).open("ab") as fh:
            fh.write(b'{"capsule_id": "torn"')  # a writer killed mid-write
        before = self._ledger_bytes()
        with self.assertRaises(ValueError):
            audit.append_ledger_head(self.spool, capsule_id="cap-2", head="h2", secret=SECRET)
        self.assertEqual(self._ledger_bytes(), before)

    def test_append_derives_seq_from_tail_beyond_one_window(self) -> None:
        count = 900  # > one 256 KiB tail window of ~300-byte entries
        self._craft_chain(count)
        record = audit.append_ledger_head(
            self.spool, capsule_id="cap-new", head="head-new", secret=SECRET
        )
        self.assertEqual(record["seq"], count)
        entries = audit.read_ledger(self.spool)
        self.assertEqual(len(entries), count + 1)
        status = audit.verify_ledger(entries, secret=SECRET)
        self.assertTrue(status.ok, status.detail)


class LedgerConcurrentAppends(LedgerBase):
    def test_parallel_appends_lose_no_update_and_chain_verifies(self) -> None:
        writers, per_writer = 6, 10

        def _append(job):
            capsule_id, head = job
            return audit.append_ledger_head(
                self.spool, capsule_id=capsule_id, head=head, secret=SECRET
            )

        jobs = [
            (f"cap-w{w}-{i}", f"head-w{w}-{i}")
            for w in range(writers)
            for i in range(per_writer)
        ]
        with ThreadPoolExecutor(max_workers=writers) as pool:
            records = list(pool.map(_append, jobs))

        self.assertEqual(len(records), writers * per_writer)
        self.assertEqual(sorted(r["seq"] for r in records), list(range(writers * per_writer)),
                         "sequence numbers must be unique and gapless")

        entries = audit.read_ledger(self.spool)
        self.assertEqual(len(entries), writers * per_writer, "no append may be lost")
        status = audit.verify_ledger(entries, secret=SECRET)
        self.assertTrue(status.ok, f"chain broken under contention: {status.detail}")

        recorded = {e["capsule_id"] for e in entries}
        self.assertEqual(recorded, {c for c, _h in jobs})


if __name__ == "__main__":
    unittest.main()
