"""governance.json durability: demotions and level changes can lose no update.

``set_level``/``demote`` are read-modify-write on one shared policy file; a
lost update means a domain keeps autonomy a human just revoked. These tests
pin the two failure modes:

- fault injection between the temp write and the rename — the previous policy
  file must survive byte-for-byte;
- an N-writer race — concurrent demotions of different domains must all land,
  and a duplicated demotion of one domain must count exactly once.

Isolation follows the existing test convention: the governance file must
EXIST before ``governance_path()`` resolves, or a same-named fallback would
route writes to ``config/governance.json``.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths

from tests._bootstrap import VAULT

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from capsule_engine import governance

GOV_FILE = VAULT / "gov durability.json"


def _seed(domains):
    payload = {
        "version": 1,
        "updated_at": "2026-10-01T00:00:00+00:00",
        "domains": {
            name: {"level": level, "updated_at": "2026-10-01T00:00:00+00:00"}
            for name, level in domains.items()
        },
    }
    GOV_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")


class GovernanceAtomicBase(unittest.TestCase):
    def setUp(self) -> None:
        _seed({"marketing": "L2"})
        self._old_env = os.environ.get(governance.GOVERNANCE_ENV)
        os.environ[governance.GOVERNANCE_ENV] = str(GOV_FILE)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._old_env is None:
            os.environ.pop(governance.GOVERNANCE_ENV, None)
        else:
            os.environ[governance.GOVERNANCE_ENV] = self._old_env

    def _read(self):
        return json.loads(GOV_FILE.read_text("utf-8"))


class GovernanceWritesAreAtomic(GovernanceAtomicBase):
    def test_demote_then_set_level_roundtrip(self) -> None:
        result = governance.demote("marketing", reason="human said no")
        self.assertTrue(result.changed)
        self.assertEqual(result.previous_level, "L2")
        state = self._read()
        entry = state["domains"]["marketing"]
        self.assertEqual(entry["level"], "L0")
        self.assertEqual(entry["demotions"], 1)
        self.assertEqual(entry["demoted_from"], "L2")
        self.assertEqual(entry["last_demotion_reason"], "human said no")

        governance.set_level("marketing", "L2")
        entry = self._read()["domains"]["marketing"]
        self.assertEqual(entry["level"], "L2")
        self.assertEqual(entry["last_change"], {"from": "L0", "to": "L2", "at": entry["last_change"]["at"]})

    def test_demote_is_idempotent(self) -> None:
        first = governance.demote("marketing")
        second = governance.demote("marketing")
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(self._read()["domains"]["marketing"]["demotions"], 1)

    def test_fault_between_tmp_write_and_rename_keeps_old_file(self) -> None:
        before = GOV_FILE.read_bytes()
        with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError("disk fault")):
            with self.assertRaises(OSError):
                governance.set_level("marketing", "L0")
        self.assertEqual(GOV_FILE.read_bytes(), before, "old policy must survive a torn write")

        with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError("disk fault")):
            with self.assertRaises(OSError):
                governance.demote("marketing")
        self.assertEqual(GOV_FILE.read_bytes(), before)

    def test_corrupt_file_keeps_legacy_fail_closed_behavior(self) -> None:
        GOV_FILE.write_text("{ not json", "utf-8")
        # Phase 2: corrupt file refuses all writes (demote and set_level raise)
        # and the corrupt file is NEVER silently overwritten or rebuilt from empty.
        with self.assertRaises(governance.GovernanceCorruptError):
            governance.demote("marketing")
        self.assertEqual(GOV_FILE.read_text("utf-8"), "{ not json")

        with self.assertRaises(governance.GovernanceCorruptError):
            governance.set_level("marketing", "L2")
        self.assertEqual(GOV_FILE.read_text("utf-8"), "{ not json")


class GovernanceConcurrentWrites(GovernanceAtomicBase):
    def test_parallel_demotions_lose_no_update(self) -> None:
        domains = ["d%02d" % i for i in range(8)]
        _seed({name: "L2" for name in domains})

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda d: governance.demote(d, reason="race"), domains))

        self.assertTrue(all(r.changed for r in results))
        state = self._read()
        for name in domains:
            entry = state["domains"][name]
            self.assertEqual(entry["level"], "L0", f"{name} lost its demotion")
            self.assertEqual(entry["demotions"], 1, f"{name} demotion counter is wrong")

    def test_parallel_demotions_of_one_domain_count_once(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: governance.demote("marketing"), range(8)))
        changed = [r for r in results if r.changed]
        self.assertEqual(len(changed), 1, "the lock must serialize same-domain demotions")
        self.assertEqual(self._read()["domains"]["marketing"]["demotions"], 1)


if __name__ == "__main__":
    unittest.main()
