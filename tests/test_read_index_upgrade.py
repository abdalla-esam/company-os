"""The read index is a *derived* read model: it must never be a single point of
failure for the reads that depend on it.

Two upgrade hazards are proven here, both against a real index file the build
itself wrote (or a stand-in for the one an interrupted writer left behind):

- **an old index** — a file carrying a schema version this build has moved past
  — is rebuilt automatically on the next read, and queries then answer from the
  new columns (no operator has to delete anything);
- **a corrupt index** — a file SQLite refuses to open at all (garbage, a torn
  non-atomic write) — is *discarded and rebuilt* rather than propagated as a
  ``DatabaseError`` into every request. Before this, ``ensure()`` let the error
  escape, so a corrupt index took the whole read surface down.

Nothing here touches the repository's real ``data/``: the index path is
redirected with ``AGENCY_INDEX_FILE`` into the bootstrap vault.
"""

from __future__ import annotations  # noqa: E402 — the suite runs on 3.9

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestration import read_index
from tests._bootstrap import SPOOL


def _write_capsule(capsule_id: str, *, domain: str = "marketing") -> Path:
    """A minimal but *valid* capsule manifest the index can project."""
    path = SPOOL / capsule_id
    path.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "capsule_id": capsule_id,
        "state": "draft",
        "domain": domain,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "attempt": 0,
        "history": [],
    }
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


class ReadIndexUpgradeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="so_index_upgrade_"))
        self.index_file = self._tmp / "index.sqlite3"
        # Every read in this module resolves the index through the override.
        env = mock.patch.dict(
            os.environ, {"AGENCY_INDEX_FILE": str(self.index_file), "AGENCY_INDEX": "on"}
        )
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, ignore_errors=True))

    def _capsule_paths(self) -> set:
        return {row["capsule_id"] for row in read_index.query_capsules()}

    def _on_disk_schema_version(self) -> str:
        conn = sqlite3.connect(self.index_file)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else ""


class TestAnOldIndexRebuilds(ReadIndexUpgradeCase):
    def test_a_stale_schema_index_is_rebuilt_and_queries_are_correct(self) -> None:
        real = _write_capsule("upgrade_real")
        self.addCleanup(lambda: __import__("shutil").rmtree(real, ignore_errors=True))

        # Stand in for what an older build left on disk: a database whose meta
        # says schema 2, holding a row this build would never project.
        conn = sqlite3.connect(self.index_file)
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE capsules (capsule_id TEXT);"
            "INSERT INTO meta VALUES ('schema_version','2');"
            "INSERT INTO capsules VALUES ('stale_from_v2');"
        )
        conn.commit()
        conn.close()

        # The very first read rebuilds the old index in place — nobody deletes it.
        self.assertTrue(read_index.ensure())
        self.assertEqual(self._on_disk_schema_version(), str(read_index.SCHEMA_VERSION))

        ids = self._capsule_paths()
        self.assertIn("upgrade_real", ids)
        self.assertNotIn("stale_from_v2", ids, "the old rows were dropped, not served")

        # The queries answer from the new v4 decision columns: the projection is
        # complete, not a partially-migrated table.
        row = next(r for r in read_index.query_capsules() if r["capsule_id"] == "upgrade_real")
        self.assertIn("last_decision_actor", row)
        self.assertIsNone(row["last_decision_action"])

    def test_a_missing_index_is_built_on_first_read(self) -> None:
        real = _write_capsule("missing_real")
        self.addCleanup(lambda: __import__("shutil").rmtree(real, ignore_errors=True))

        self.assertFalse(self.index_file.exists())
        self.assertTrue(read_index.ensure())
        self.assertTrue(self.index_file.exists())
        self.assertIn("missing_real", self._capsule_paths())


class TestACorruptIndexIsDiscardedAndRebuilt(ReadIndexUpgradeCase):
    def test_a_non_database_file_is_recovered_not_propagated(self) -> None:
        real = _write_capsule("corrupt_real")
        self.addCleanup(lambda: __import__("shutil").rmtree(real, ignore_errors=True))

        # Not SQLite by any stretch: an interrupted non-atomic writer could leave
        # exactly this, and SQLite refuses to open it.
        self.index_file.write_bytes(b"this is not a sqlite database" * 64)
        with self.assertRaises(sqlite3.DatabaseError):
            sqlite3.connect(self.index_file).execute("PRAGMA journal_mode=WAL")

        # A read recovers: the corrupt file is discarded and a fresh index built.
        self.assertTrue(read_index.ensure())
        self.assertIn("corrupt_real", self._capsule_paths())
        self.assertEqual(self._on_disk_schema_version(), str(read_index.SCHEMA_VERSION))

    def test_the_corrupt_file_is_replaced_on_disk(self) -> None:
        real = _write_capsule("corrupt_real2")
        self.addCleanup(lambda: __import__("shutil").rmtree(real, ignore_errors=True))
        self.index_file.write_bytes(b"\x00\x01\x02 not sqlite \xff\xfe" * 32)

        read_index.ensure()

        # The bytes on disk are now a real SQLite database, not the garbage.
        self.assertTrue(self.index_file.read_bytes().startswith(b"SQLite format 3\x00"))
        self.assertIn("corrupt_real2", self._capsule_paths())


if __name__ == "__main__":
    import unittest

    unittest.main()
