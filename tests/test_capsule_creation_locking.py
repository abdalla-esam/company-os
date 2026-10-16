"""Capsule creation takes the capsule's own lock — and a busy lock raises.

Phase 1b-i closes the four creation paths that wrote into a capsule directory
without ``CapsuleLock``: the API's ``create_capsule`` and ``build_post``, the
sandbox's ``create_trial_capsule`` and ``ops/tools/seed_gate_plans.py``. Each
now takes the same lock the workers, the approval service and the pulse take,
and — the whole point of the lock — **nothing is written unlocked**:

- a real contention test (``flock`` on two independent file descriptions in
  one process conflicts exactly like two processes do) proves the timeout
  raises ``LockTimeoutError`` *before* the first byte lands;
- a seed success test proves the new lock does not deadlock the command it
  wrapped (acquire → read → write → release, no nested acquisition).
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault

from tests._bootstrap import DOMAINS, SPOOL

import asyncio
import json
import re
import shutil
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from capsule_engine.exceptions import LockTimeoutError
from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import ARTIFACT_FILE, load_manifest, save_manifest
from orchestration import access_control, api

import ops.tools.seed_gate_plans as seed_gate_plans
from capsule_engine.sandbox import CapsuleSandbox
import capsule_engine.sandbox as sandbox_module


def _request(path: str = "/api/capsules") -> Request:
    """A request carrying a root principal (identity from the credential)."""
    principal = access_control.Principal.legacy_root("phase-1b-tester")
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "state": {"principal": principal},
        }
    )


def _zero_timeout(capsule_path, **kwargs):
    """The real lock, but a single probe: contention raises immediately."""
    return CapsuleLock(capsule_path, timeout=0, purpose=kwargs.get("purpose", "waiter"))


class CapsuleLockTimeout(unittest.TestCase):
    """Confirm the primitive: a held lock raises, it does not wait forever."""

    def setUp(self) -> None:
        self.capsule = SPOOL / "cap_lock_probe"
        self.capsule.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.capsule, ignore_errors=True)

    def test_second_acquisition_raises_immediately(self) -> None:
        with CapsuleLock(self.capsule, purpose="holder"):
            with self.assertRaises(LockTimeoutError):
                with CapsuleLock(self.capsule, timeout=0, purpose="waiter"):
                    pass  # pragma: no cover — never reached

    def test_lock_release_lets_the_next_writer_in(self) -> None:
        with CapsuleLock(self.capsule, purpose="holder"):
            pass
        with CapsuleLock(self.capsule, timeout=0, purpose="waiter") as lock:
            self.assertTrue(lock.is_held)


class CreateCapsuleWritesUnderLock(unittest.TestCase):
    """``POST /api/capsules`` — locked, and refuses on timeout."""

    def setUp(self) -> None:
        self.capsule_id = "cap_lock_refuse_create"
        self.capsule_dir = SPOOL / self.capsule_id
        self.capsule_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.capsule_dir, ignore_errors=True)

    def test_busy_capsule_raises_before_any_write(self) -> None:
        req = api.CreateCapsuleRequest(
            text="must never reach disk",
            capsule_id=self.capsule_id,
            sector="marketing",
        )

        with CapsuleLock(self.capsule_dir, purpose="other-writer"):
            with mock.patch.object(api, "CapsuleLock", _zero_timeout):
                with self.assertRaises(LockTimeoutError):
                    asyncio.run(api.create_capsule_endpoint(req, _request()))

        self.assertFalse(
            (self.capsule_dir / "manifest.json").exists(),
            "a timed-out creation must not write a manifest",
        )
        self.assertFalse(
            (self.capsule_dir / ARTIFACT_FILE).exists(),
            "a timed-out creation must not write the artifact",
        )
        self.assertEqual(
            [p.name for p in self.capsule_dir.glob("*.tmp")], [], "no torn temp"
        )


class BuildPostWritesUnderLock(unittest.TestCase):
    """``POST /api/capsules/build_post`` — capsule and tickets behind the lock."""

    TOPIC = "Lock Refusal"

    def setUp(self) -> None:
        self.clean_topic = (
            re.sub(r"[^a-zA-Z0-9_\u0600-\u06FF]+", "_", self.TOPIC).strip("_")[:30]
        )
        self.capsules = [
            SPOOL / f"cap_post_{stamp}_{self.clean_topic}"
            for stamp in self._candidate_stamps()
        ]
        for capsule in self.capsules:
            capsule.mkdir(parents=True, exist_ok=True)
            self.addCleanup(shutil.rmtree, capsule, ignore_errors=True)
        self.tickets_dir = DOMAINS / "marketing" / "tickets"
        self.tickets_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _candidate_stamps() -> list:
        """``build_post`` stamps the id with the current second — cover the
        second it computes and the one after it, so the contention is exact."""
        now = datetime.now(timezone.utc)
        return [
            now.strftime("%Y%m%d_%H%M%S"),
            (now + timedelta(seconds=1)).strftime("%Y%m%d_%H%M%S"),
        ]

    def test_busy_capsule_raises_before_any_write(self) -> None:
        tickets_before = {p.name for p in self.tickets_dir.glob("ticket_*.json")}
        locks = [CapsuleLock(capsule, purpose="other-writer") for capsule in self.capsules]
        for lock in locks:
            lock.acquire()
            self.addCleanup(lock.release)

        request = api.PostBuildRequest(
            topic=self.TOPIC, angle="Durability", headline=self.TOPIC
        )
        with mock.patch.object(api, "CapsuleLock", _zero_timeout):
            with self.assertRaises(LockTimeoutError):
                asyncio.run(api.build_post_endpoint(request))

        for capsule in self.capsules:
            self.assertFalse(
                (capsule / "manifest.json").exists(),
                f"{capsule.name}: timed-out creation wrote a manifest",
            )
            self.assertFalse((capsule / ARTIFACT_FILE).exists())
        self.assertEqual(
            {p.name for p in self.tickets_dir.glob("ticket_*.json")},
            tickets_before,
            "tickets of a refused build must not be written",
        )


class TrialCapsuleWritesUnderLock(unittest.TestCase):
    """``CapsuleSandbox.create_trial_capsule``."""

    TICKET_ID = "T-SBX-LOCK-1"

    def setUp(self) -> None:
        self.tickets_dir = DOMAINS / "ideation" / "tickets"
        self.tickets_dir.mkdir(parents=True, exist_ok=True)
        self.ticket_path = self.tickets_dir / f"ticket_{self.TICKET_ID}.json"
        self.ticket_path.write_text(
            json.dumps(
                {
                    "ticket_id": self.TICKET_ID,
                    "client_id": "lock_tester",
                    "request": "trial request",
                    "state": "open",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.addCleanup(self.ticket_path.unlink, missing_ok=True)

        self.capsule_id = "sbx_lock_refuse_trial"
        self.capsule_dir = CapsuleSandbox().sandbox_root / self.capsule_id
        self.capsule_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.capsule_dir, ignore_errors=True)

    def test_busy_capsule_raises_before_any_write(self) -> None:
        sandbox = CapsuleSandbox()
        with CapsuleLock(self.capsule_dir, purpose="other-writer"):
            with mock.patch.object(sandbox_module, "CapsuleLock", _zero_timeout):
                with self.assertRaises(LockTimeoutError):
                    sandbox.create_trial_capsule(
                        self.TICKET_ID,
                        sector="ideation",
                        capsule_id=self.capsule_id,
                    )

        self.assertFalse(
            (self.capsule_dir / "manifest.json").exists(),
            "a timed-out trial creation must not write a manifest",
        )


class SeedGatePlansWritesUnderLock(unittest.TestCase):
    """``ops/tools/seed_gate_plans.py seed_plan`` — locked, still non-deadlocking."""

    def setUp(self) -> None:
        self.capsule_id = "cap_lock_seed_plan"
        self.capsule_dir = SPOOL / self.capsule_id
        self._create_planless_capsule()
        self.addCleanup(shutil.rmtree, self.capsule_dir, ignore_errors=True)

    def _create_planless_capsule(self) -> None:
        """A real pending capsule with its gate plan stripped (the fixture
        ``seed_gate_plans`` exists to repair)."""
        req = api.CreateCapsuleRequest(
            text="seed plan fixture",
            capsule_id=self.capsule_id,
            sector="marketing",
        )
        asyncio.run(api.create_capsule_endpoint(req, _request()))
        with CapsuleLock(self.capsule_dir):
            manifest = load_manifest(self.capsule_dir)
            manifest["gates"] = []
            save_manifest(self.capsule_dir, manifest)

    def test_busy_capsule_raises_and_the_manifest_is_untouched(self) -> None:
        before = (self.capsule_dir / "manifest.json").read_bytes()

        with CapsuleLock(self.capsule_dir, purpose="other-writer"):
            with mock.patch.object(seed_gate_plans, "CapsuleLock", _zero_timeout):
                with self.assertRaises(LockTimeoutError):
                    seed_gate_plans.seed_plan(self.capsule_dir)

        self.assertEqual(
            (self.capsule_dir / "manifest.json").read_bytes(),
            before,
            "a timed-out seed must not touch the manifest",
        )

    def test_seed_succeeds_under_the_lock_without_deadlock(self) -> None:
        self.assertIn(
            self.capsule_dir,
            seed_gate_plans.candidates(SPOOL, self.capsule_id),
            "fixture must start planless",
        )

        count = seed_gate_plans.seed_plan(self.capsule_dir)

        self.assertGreaterEqual(count, 1, "the policy template must yield gates")
        manifest = load_manifest(self.capsule_dir)
        self.assertEqual(len(manifest.get("gates") or []), count)
        self.assertEqual(
            str(manifest.get("state")), "pending_approval", "seeding never moves state"
        )
        self.assertNotIn(
            self.capsule_dir,
            seed_gate_plans.candidates(SPOOL, self.capsule_id),
            "the capsule is governed now",
        )

    def test_parallel_seeders_lose_no_write(self) -> None:
        """N writers, one capsule: the lock serialises them and the manifest
        never ends up torn, half-seeded or moved out of ``pending_approval``."""
        failures: list = []
        results: list = []

        def _seed() -> None:
            try:
                results.append(seed_gate_plans.seed_plan(self.capsule_dir))
            except Exception as exc:  # noqa: BLE001 — recorded, asserted below
                failures.append(repr(exc))

        threads = [threading.Thread(target=_seed, name=f"seeder-{i}") for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "a seeder deadlocked on the lock")

        self.assertEqual(failures, [], "every seeder must land")
        self.assertTrue(all(count >= 1 for count in results), results)
        manifest = load_manifest(self.capsule_dir)  # parses => never torn
        self.assertGreaterEqual(len(manifest.get("gates") or []), 1)
        self.assertEqual(str(manifest.get("state")), "pending_approval")


if __name__ == "__main__":
    unittest.main()
