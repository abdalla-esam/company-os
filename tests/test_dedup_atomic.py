"""Dedup index durability: pruning must never swallow an appended fingerprint.

``record`` (append under the chain) and ``compact`` (the prune/rewrite) now
share one sidecar lock, so these tests pin both directions of the race:

- a fault between the temp write and the replace — the old index survives
  byte-for-byte;
- N appender threads racing ``compact`` — every fingerprint that ``record``
  accepted must still be on disk afterwards, with each line's chain hash
  intact.

A policy stub keeps the tests hermetic (no dependency on config/engine.json).
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

from capsule_engine import dedup


class _DedupCfg:
    enabled = True
    window_days = 30.0
    similarity_threshold = 0.7
    shingle_size = 2
    max_shingles = 2000
    max_entries = 20000

    @staticmethod
    def for_domain(domain):
        return None


class _Policy:
    dedup = _DedupCfg()


OLD_AT = "2020-01-01T00:00:00+00:00"
NEW_AT = "2026-10-13T00:00:00+00:00"


class DedupBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="so_test_dedup_"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.ledger = self.dir / dedup.INDEX_NAME

    def _record(self, text, capsule_id, at=NEW_AT):
        entry = dedup.record(
            text,
            capsule_id=capsule_id,
            domain="marketing",
            path=self.ledger,
            policy=_Policy(),
            at=at,
        )
        self.assertIsNotNone(entry, "test setup requires the record to land")
        return entry

    def _lines(self):
        if not self.ledger.is_file():
            return []
        return [l for l in self.ledger.read_text("utf-8").splitlines() if l.strip()]

    def _assert_chain_hashes_intact(self):
        prev = ""
        for raw in self._lines():
            line = json.loads(raw)
            payload = {k: v for k, v in line.items() if k != "hash"}
            self.assertEqual(line["hash"], dedup._chain_hash(line["prev"], payload))
            prev = line["hash"]


class DedupCompactIsSafe(DedupBase):
    def test_compact_drops_expired_keeps_chain_verbatim(self) -> None:
        self._record("old proposal about rockets", "cap-old", at=OLD_AT)
        kept = [
            self._record(f"fresh proposal number {i} about rockets", f"cap-{i}")
            for i in range(3)
        ]
        result = dedup.compact(window_days=30, path=self.ledger, policy=_Policy())
        self.assertEqual(result, 3)
        lines = self._lines()
        self.assertEqual(len(lines), 3)
        self.assertNotIn("cap-old", "".join(lines))
        self._assert_chain_hashes_intact()
        kept_hashes = {e.hash for e in kept}
        self.assertEqual({json.loads(l)["hash"] for l in lines}, kept_hashes)

    def test_compact_missing_index_is_a_no_op(self) -> None:
        self.assertEqual(dedup.compact(window_days=30, path=self.ledger, policy=_Policy()), 0)
        self.assertFalse(self.ledger.exists())

    def test_fault_between_tmp_write_and_replace_keeps_old_index(self) -> None:
        self._record("survivor proposal about durability", "cap-1")
        self._record("expired proposal about durability", "cap-2", at=OLD_AT)
        before = self.ledger.read_bytes()
        with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError("disk fault")):
            with self.assertRaises(OSError):
                dedup.compact(window_days=30, path=self.ledger, policy=_Policy())
        self.assertEqual(self.ledger.read_bytes(), before, "old index must survive a torn prune")
        self.assertEqual(
            [n for n in self.dir.iterdir() if n.name.endswith(".tmp")], []
        )


class DedupRecordRacesCompact(DedupBase):
    def test_appends_racing_prune_lose_no_fingerprint(self) -> None:
        # An expired backlog gives compact something to rewrite.
        for i in range(20):
            self._record(f"expired backlog proposal {i} about durability", f"cap-old-{i}", at=OLD_AT)

        writers, per_writer = 4, 10

        def _append(job):
            w, i = job
            return dedup.record(
                f"live proposal w{w} i{i} about durability and locking",
                capsule_id=f"cap-w{w}-{i}",
                domain="marketing",
                path=self.ledger,
                policy=_Policy(),
                at=NEW_AT,
            )

        jobs = [(w, i) for w in range(writers) for i in range(per_writer)]
        with ThreadPoolExecutor(max_workers=writers) as pool:
            future = pool.map(_append, jobs)
            # Prune repeatedly while the appenders run: the loser of any
            # unsynchronized window would drop a just-appended fingerprint.
            dedup.compact(window_days=30, path=self.ledger, policy=_Policy())
            entries = list(future)
            dedup.compact(window_days=30, path=self.ledger, policy=_Policy())

        recorded = [e for e in entries if e is not None]
        self.assertEqual(len(recorded), writers * per_writer)

        on_disk = {json.loads(l)["hash"] for l in self._lines()}
        missing = [e.capsule_id for e in recorded if e.hash not in on_disk]
        self.assertEqual(missing, [], f"prune swallowed appended fingerprints: {missing}")
        self._assert_chain_hashes_intact()


if __name__ == "__main__":
    unittest.main()
