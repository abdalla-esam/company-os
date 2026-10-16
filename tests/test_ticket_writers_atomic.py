"""Ticket writer durability: no ticket write tears, no writer loses another's.

Every writer of ``ticket_*.json`` now goes through the shared helper
(``shared_kernel.atomic_io``), so these tests hold each one to the same
contract (Phase 1b-i):

- **fault injection between the temp write and the rename** — the previous
  ticket survives byte for byte, nothing half-written is left behind, and no
  ``.tmp`` litter remains;
- **an N-writer race on one ticket** — the API's context PATCH, the
  dispatcher's rewrite and a tracker settle must all land (no lost update);
- **the lock contract** — a busy sidecar raises ``LockTimeoutError`` before
  any write happens, never after.

Writers covered, one subTest per production writer:

- ``orchestration.api``: ticket create (scratch + approved), context PATCH,
  settle-bridge state update, build_post ticket write;
- ``ops.tools.ticket_mgr``: create, promote, demote;
- ``orchestration.dispatcher``: rewrite (``save``/``put``) + spawned-ticket
  create (``process_spawn``);
- the three domain trackers' ``_write_json`` (marketing, client_management,
  ideation) and ``auto_ideator``'s gated-ticket write.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault

from tests._bootstrap import DOMAINS, REPO_ROOT, SPOOL

import asyncio
import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from orchestration import access_control, api, dispatcher
from shared_kernel.atomic_io import (
    LockTimeoutError as SidecarLockTimeoutError,
    sidecar_lock,
    update_json,
)

FAULT = "disk fault injected between tmp and rename"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_tracker(sector: str):
    """Import ``data/domains/<sector>/tracker_script.py`` by path.

    The trackers import their own ``agent_script`` sibling, so the sector
    directory goes on ``sys.path`` first (they are subprocess scripts, not
    packages).
    """
    path = REPO_ROOT / "data" / "domains" / sector / "tracker_script.py"
    sector_dir = str(path.parent)
    if sector_dir not in sys.path:
        sys.path.insert(0, sector_dir)
    return _load_module(f"tracker_script_{sector}_under_test", path)


def _request(path: str = "/api/tickets") -> Request:
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


def _tmp_litter(directory: Path) -> list:
    """Every ``.tmp`` file below ``directory`` (atomic writes leave none)."""
    return sorted(p.name for p in Path(directory).rglob("*.tmp"))


class _SectorFixture(unittest.TestCase):
    """A scratch sector + tickets directories inside the shared vault."""

    SECTOR = "ideation"

    def setUp(self) -> None:
        self.sector_dir = DOMAINS / self.SECTOR
        self.sector_dir.mkdir(parents=True, exist_ok=True)
        self.tickets_dir = self.sector_dir / "tickets"
        self.scratch_dir = self.tickets_dir / "scratch"
        self.approved_dir = self.tickets_dir / "approved"
        for directory in (self.scratch_dir, self.approved_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._created_tickets: list[Path] = []
        self.addCleanup(self._remove_tickets)

    def _remove_tickets(self) -> None:
        for path in self._created_tickets:
            Path(path).unlink(missing_ok=True)

    def _track(self, *paths) -> None:
        self._created_tickets.extend(Path(p) for p in paths)

    def _create_scratch_ticket(self, title: str = "Phase 1b ticket") -> dict:
        req = api.CreateTicketRequest(
            sector=self.SECTOR,
            title=title,
            content="content",
            stage="scratch",
        )
        response = asyncio.run(api.create_ticket(req, _request()))
        path = self.scratch_dir / f"ticket_{response['ticket_id']}.json"
        self._track(path)
        return {"response": response, "path": path}


class ApiTicketWritesAreAtomic(_SectorFixture):
    """``orchestration.api``: create (both stages), PATCH, settle bridge."""

    def test_create_scratch_fault_keeps_old_bytes(self) -> None:
        marker = self.scratch_dir / "ticket_PRE_EXISTING.json"
        marker.write_text('{"ticket_id": "PRE"}', encoding="utf-8")
        self._track(marker)
        before = marker.read_bytes()

        req = api.CreateTicketRequest(
            sector=self.SECTOR, title="t", content="c", stage="scratch"
        )
        with mock.patch(
            "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
        ):
            with self.assertRaises(OSError):
                asyncio.run(api.create_ticket(req, _request()))

        self.assertEqual(marker.read_bytes(), before, "old ticket must survive")
        self.assertEqual(
            _tmp_litter(self.tickets_dir), [], "no torn temp file may remain"
        )

    def test_create_approved_fault_keeps_old_bytes(self) -> None:
        async def _fake_gate(cmd, input_text=None, **kwargs):
            # the real gate raise is a subprocess of the repository's
            # gate_script; the ticket write is what is under test here.
            return 0, json.dumps({"gate_id": "G-FAKE-APPROVED"}), ""

        req = api.CreateTicketRequest(
            sector=self.SECTOR, title="t", content="c", stage="approved"
        )
        with mock.patch.object(api, "_run_subprocess", _fake_gate):
            with mock.patch(
                "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
            ):
                with self.assertRaises(OSError):
                    asyncio.run(api.create_ticket(req, _request()))

        self.assertEqual(
            list(self.approved_dir.glob("ticket_*.json")), [], "no ticket written"
        )
        self.assertEqual(_tmp_litter(self.tickets_dir), [])

    def test_create_approved_writes_the_old_byte_format(self) -> None:
        async def _fake_gate(cmd, input_text=None, **kwargs):
            return 0, json.dumps({"gate_id": "G-FAKE-APPROVED"}), ""

        req = api.CreateTicketRequest(
            sector=self.SECTOR, title="Approved", content="c", stage="approved"
        )
        with mock.patch.object(api, "_run_subprocess", _fake_gate):
            response = asyncio.run(api.create_ticket(req, _request()))

        path = self.approved_dir / f"ticket_human_{response['ticket_id']}.json"
        self._track(path)
        text = path.read_text(encoding="utf-8")
        self.assertFalse(text.endswith("\n"), "byte parity with the old writer")
        data = json.loads(text)
        self.assertEqual(text, json.dumps(data, ensure_ascii=False, indent=2))
        self.assertEqual(data["state"], "pending_approval")
        self.assertEqual(data["gate_id"], "G-FAKE-APPROVED")

    def test_create_approved_exercises_real_gate_subprocess(self) -> None:
        """With GATE_DIR resolved, the real gate subprocess runs cleanly against the test vault."""
        req = api.CreateTicketRequest(
            sector=self.SECTOR, title="Approved Real Subprocess", content="c", stage="approved"
        )
        response = asyncio.run(api.create_ticket(req, _request()))
        path = self.approved_dir / f"ticket_human_{response['ticket_id']}.json"
        self._track(path)
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "pending_approval")
        self.assertTrue(data["gate_id"].startswith("G-"))

    def test_create_writes_the_old_byte_format(self) -> None:
        created = self._create_scratch_ticket(title="Format parity")
        path = created["path"]
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        # Byte parity with the writer this replaced: indent=2, ensure_ascii off,
        # no trailing newline.
        self.assertEqual(
            text, json.dumps(data, ensure_ascii=False, indent=2)
        )
        self.assertFalse(text.endswith("\n"))
        self.assertEqual(data["state"], "draft")
        self.assertEqual(data["title"], "Format parity")
        self.assertEqual(_tmp_litter(self.tickets_dir), [])

    def test_context_patch_fault_keeps_old_bytes(self) -> None:
        created = self._create_scratch_ticket()
        path = created["path"]
        before = path.read_bytes()

        req = api.UpdateTicketContextRequest(context={"owner": "me"}, note="n")
        with mock.patch(
            "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
        ):
            with self.assertRaises(OSError):
                asyncio.run(
                    api.update_ticket_context(
                        created["response"]["ticket_id"],
                        req,
                        _request(),
                        sector=self.SECTOR,
                    )
                )

        self.assertEqual(path.read_bytes(), before, "a torn PATCH must not land")
        self.assertEqual(_tmp_litter(self.tickets_dir), [])

    def test_context_patch_merges_and_keeps_response_shape(self) -> None:
        created = self._create_scratch_ticket()
        ticket_id = created["response"]["ticket_id"]

        req = api.UpdateTicketContextRequest(
            context={"owner": "me"},
            metadata={"priority": "high"},
            note="phase 1b",
        )
        response = asyncio.run(
            api.update_ticket_context(ticket_id, req, _request(), sector=self.SECTOR)
        )

        self.assertEqual(
            set(response),
            {
                "status",
                "ticket_id",
                "sector",
                "stage",
                "context",
                "changelog_entries",
                "message",
            },
        )
        self.assertEqual(response["context"], {"owner": "me"})
        self.assertEqual(response["changelog_entries"], 1)

        on_disk = json.loads(created["path"].read_text(encoding="utf-8"))
        self.assertEqual(on_disk["context"], {"owner": "me"})
        self.assertEqual(on_disk["metadata"]["priority"], "high")
        self.assertEqual(len(on_disk["changelog"]), 1)
        self.assertEqual(on_disk["state"], "draft")  # untouched by a PATCH

    def test_settle_bridge_fault_keeps_old_bytes(self) -> None:
        path = self.tickets_dir / "ticket_BRIDGE.json"
        original = {
            "ticket_id": "T-BRIDGE",
            "state": "open",
            "via": "G-1",
            "decision": {"status": "accepted"},
            "context": {"keep": "me"},
        }
        path.write_text(json.dumps(original, ensure_ascii=False, indent=2), "utf-8")
        self._track(path)
        before = path.read_bytes()

        with mock.patch(
            "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
        ):
            with self.assertRaises(OSError):
                api._mark_ticket_bridged(path, "capsule_x")

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(_tmp_litter(self.tickets_dir), [])

    def test_settle_bridge_updates_only_its_own_fields(self) -> None:
        path = self.tickets_dir / "ticket_BRIDGE2.json"
        original = {
            "ticket_id": "T-BRIDGE2",
            "state": "open",
            "via": "G-2",
            "context": {"keep": "me"},
            "history": [{"at": "t", "by": "tracker"}],
        }
        path.write_text(json.dumps(original, ensure_ascii=False, indent=2), "utf-8")
        self._track(path)

        api._mark_ticket_bridged(path, "capsule_y")

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["state"], "settled")
        self.assertEqual(on_disk["spool_capsule"], "capsule_y")
        self.assertEqual(on_disk["context"], {"keep": "me"})  # survives
        self.assertEqual(on_disk["history"], original["history"])
        self.assertEqual(on_disk["via"], "G-2")

    def test_build_post_fault_writes_no_ticket_and_no_manifest(self) -> None:
        tickets_dir = DOMAINS / "marketing" / "tickets"
        tickets_dir.mkdir(parents=True, exist_ok=True)
        spool_before = {p.name for p in SPOOL.glob("cap_post_*")}
        tickets_before = {p.name for p in tickets_dir.glob("ticket_*.json")}

        request = api.PostBuildRequest(
            topic="Fault Injection", angle="Durability", headline="Fault Injection"
        )
        with mock.patch(
            "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
        ):
            with self.assertRaises(OSError):
                asyncio.run(api.build_post_endpoint(request))

        self.assertEqual(
            {p.name for p in tickets_dir.glob("ticket_*.json")},
            tickets_before,
            "build_post must not leave tickets behind a failed capsule",
        )
        for name in {p.name for p in SPOOL.glob("cap_post_*")} - spool_before:
            shutil.rmtree(SPOOL / name, ignore_errors=True)

        # no half-written capsule survives the refusal
        for capsule in SPOOL.glob("cap_post_*"):
            self.assertFalse((capsule / "manifest.json").exists())


class TicketMgrWritesAreAtomic(unittest.TestCase):
    """``ops.tools.ticket_mgr``: create, promote, demote."""

    SECTOR = "marketing"

    def setUp(self) -> None:
        from ops.tools import ticket_mgr

        self.ticket_mgr = ticket_mgr
        self.sector_dir = DOMAINS / self.SECTOR
        self.sector_dir.mkdir(parents=True, exist_ok=True)
        self.scratch_dir = self.sector_dir / "tickets" / "scratch"
        self.approved_dir = self.sector_dir / "tickets" / "approved"
        for directory in (self.scratch_dir, self.approved_dir):
            directory.mkdir(parents=True, exist_ok=True)
        # ``ticket_mgr`` reports paths relative to the repository; in the test
        # vault the tickets live outside it. Point it at the vault root
        # (resolved: ``shared_kernel.paths`` hands out resolved paths).
        original_root = self.ticket_mgr.REPO_ROOT
        self.ticket_mgr.REPO_ROOT = DOMAINS.parent.resolve()
        self.addCleanup(setattr, self.ticket_mgr, "REPO_ROOT", original_root)
        self._created: list[Path] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for path in self._created:
            Path(path).unlink(missing_ok=True)

    def _create(self, stage: str = "scratch") -> dict:
        response = self.ticket_mgr.create_ticket(
            sector=self.SECTOR, title="mgr ticket", content="body", stage=stage
        )
        directory = self.approved_dir if stage == "approved" else self.scratch_dir
        path = directory / f"ticket_{response['ticket_id']}.json"
        self._created.append(path)
        return {"response": response, "path": path}

    def test_create_fault_keeps_old_bytes(self) -> None:
        marker = self.scratch_dir / "ticket_PRE_MGR.json"
        marker.write_text('{"ticket_id": "PRE_MGR"}', encoding="utf-8")
        self._created.append(marker)
        before = marker.read_bytes()

        with mock.patch(
            "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
        ):
            with self.assertRaises(OSError):
                self.ticket_mgr.create_ticket(
                    sector=self.SECTOR, title="t", content="c", stage="scratch"
                )

        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(_tmp_litter(self.sector_dir / "tickets"), [])

    def test_promote_fault_keeps_ticket_in_place_then_promotes(self) -> None:
        created = self._create(stage="scratch")
        source = created["path"]
        target = self.approved_dir / source.name
        before = source.read_bytes()

        with mock.patch(
            "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
        ):
            with self.assertRaises(OSError):
                self.ticket_mgr.promote_ticket(
                    created["response"]["ticket_id"], self.SECTOR
                )

        self.assertTrue(source.is_file(), "a failed move must not unlink the source")
        self.assertEqual(source.read_bytes(), before, "source bytes must survive")
        self.assertFalse(target.exists(), "target must not exist after the fault")
        self.assertEqual(_tmp_litter(self.sector_dir / "tickets"), [])

        # the sidecar lock was released on the failure path: the retry works
        response = self.ticket_mgr.promote_ticket(
            created["response"]["ticket_id"], self.SECTOR
        )
        self.assertEqual(response["status"], "success")
        self.assertFalse(source.exists())
        self.assertTrue(target.is_file())
        self._created.append(target)
        on_disk = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["stage"], "approved")
        self.assertEqual(on_disk["state"], "open")
        self.assertEqual(on_disk["history"][-1]["to"], "approved")
        self.assertFalse(
            target.read_text(encoding="utf-8").endswith("\n"),
            "byte format must match the writer this replaced",
        )

    def test_demote_fault_keeps_ticket_in_place_then_demotes(self) -> None:
        created = self._create(stage="approved")  # already in approved/
        source = created["path"]
        # park it in approved/ (create_ticket with stage=approved does that)
        target = self.scratch_dir / source.name
        before = source.read_bytes()

        with mock.patch(
            "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
        ):
            with self.assertRaises(OSError):
                self.ticket_mgr.demote_ticket(
                    created["response"]["ticket_id"], self.SECTOR
                )

        self.assertTrue(source.is_file())
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(target.exists())
        self.assertEqual(_tmp_litter(self.sector_dir / "tickets"), [])

        response = self.ticket_mgr.demote_ticket(
            created["response"]["ticket_id"], self.SECTOR
        )
        self.assertEqual(response["status"], "success")
        self.assertFalse(source.exists())
        self._created.append(target)
        on_disk = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["stage"], "scratch")
        self.assertEqual(on_disk["state"], "draft")
        self.assertEqual(on_disk["history"][-1]["to"], "scratch")


class DispatcherWritesAreAtomic(unittest.TestCase):
    """``orchestration.dispatcher``: the rewrite and the spawned-ticket create."""

    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="so_test_dispatcher_"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.path = self.workdir / "ticket_D.json"
        self.original = {
            "ticket_id": "T-DISPATCH-1",
            "client_id": "c1",
            "state": "open",
            "attempts": 0,
            "history": [],
            "context": {"from": "base"},
        }
        self.path.write_text(
            json.dumps(self.original, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def test_save_fault_keeps_old_bytes(self) -> None:
        before = self.path.read_bytes()
        ticket = json.loads(self.path.read_text(encoding="utf-8"))
        base = copy.deepcopy(ticket)
        dispatcher.add_history(ticket, "open", "in_progress", "dispatcher", "claim")

        with mock.patch(
            "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
        ):
            with self.assertRaises(OSError):
                dispatcher.save(str(self.path), ticket, base=base)

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(_tmp_litter(self.workdir), [])

    def test_put_releases_the_claim_even_after_a_fault(self) -> None:
        claim = dispatcher.grab(str(self.path))
        self.assertIsNotNone(claim)
        self.assertFalse(self.path.exists(), "the claim rename is the arbitration")
        before = Path(claim).read_bytes()
        ticket = json.loads(Path(claim).read_text(encoding="utf-8"))
        dispatcher.add_history(ticket, "open", "open", "dispatcher", "rewritten")

        with mock.patch(
            "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
        ):
            with self.assertRaises(OSError):
                dispatcher.put(claim, str(self.path), ticket)

        # the write failed, so the claim is restored (never a lost ticket)
        self.assertFalse(self.path.exists())
        self.assertEqual(Path(claim).read_bytes(), before)

        # and a retry without the fault lands the rewrite
        dispatcher.put(claim, str(self.path), ticket)
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["history"][-1]["by"], "dispatcher")
        self.assertFalse(Path(claim).exists(), "the claim is released")

    def test_save_merges_rather_than_overwrites(self) -> None:
        ticket = json.loads(self.path.read_text(encoding="utf-8"))
        base = copy.deepcopy(ticket)
        dispatcher.add_history(ticket, "open", "open", "dispatcher", "mine")

        # another writer lands between the dispatcher's read and its write
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        on_disk["context"]["concurrent"] = True
        on_disk["changelog"] = [{"note": "from the api"}]
        self.path.write_text(
            json.dumps(on_disk, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        dispatcher.save(str(self.path), ticket, base=base)

        final = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(final["context"]["concurrent"], True, "lost update")
        self.assertEqual(final["changelog"], [{"note": "from the api"}], "lost update")
        self.assertEqual(final["history"][-1]["by"], "dispatcher", "our update lost")

    def test_process_spawn_fault_leaves_no_partial_ticket(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="so_test_spawn_"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        target_tickets = work / "marketing" / "tickets"
        target_tickets.mkdir(parents=True)
        original_domains = dispatcher.DOMAINS_DIR
        dispatcher.DOMAINS_DIR = str(work)
        self.addCleanup(setattr, dispatcher, "DOMAINS_DIR", original_domains)

        ticket = {
            "ticket_id": "T-SPAWN",
            "client_id": "c1",
            "state": "done",
            "spawn_next": {"target_sector": "marketing", "request": "next step"},
        }
        with mock.patch(
            "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
        ):
            event = dispatcher.process_spawn("ideation", str(self.path), ticket)

        self.assertEqual(event["outcome"], "needs_revision")
        self.assertIn("failed to create spawned ticket", event["detail"])
        self.assertEqual(list(target_tickets.glob("ticket_*.json")), [])
        self.assertEqual(_tmp_litter(target_tickets), [])


class DomainTrackerWritesAreAtomic(unittest.TestCase):
    """The three trackers' ``_write_json`` + ``auto_ideator``'s ticket write."""

    SECTORS = ("marketing", "client_management", "ideation")

    def _workdir(self) -> Path:
        workdir = Path(tempfile.mkdtemp(prefix="so_test_tracker_"))
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        return workdir

    def test_write_json_fault_keeps_old_bytes(self) -> None:
        for sector in self.SECTORS:
            with self.subTest(sector=sector):
                tracker = _load_tracker(sector)
                workdir = self._workdir()
                path = workdir / "ticket_TRACKER.json"
                original = {"ticket_id": "T-T", "state": "open", "via": None}
                path.write_text(
                    json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                before = path.read_bytes()

                with mock.patch(
                    "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
                ):
                    with self.assertRaises(OSError):
                        tracker._write_json(
                            str(path), dict(original, state="settled")
                        )

                self.assertEqual(path.read_bytes(), before, f"{sector} lost bytes")
                self.assertEqual(_tmp_litter(workdir), [])

    def test_write_json_merges_and_keeps_byte_format(self) -> None:
        for sector in self.SECTORS:
            with self.subTest(sector=sector):
                tracker = _load_tracker(sector)
                workdir = self._workdir()
                path = workdir / "ticket_MERGE.json"

                # a brand-new ticket: written exactly as given
                tracker._write_json(str(path), {"ticket_id": "T-M", "state": "open"})
                text = path.read_text(encoding="utf-8")
                self.assertEqual(
                    text, json.dumps(json.loads(text), ensure_ascii=False, indent=2)
                )
                self.assertFalse(text.endswith("\n"))

                # a settle landing on a file someone else updated keeps theirs
                tracker._write_json(
                    str(path),
                    {"via": "G-9", "decision": {"status": "accepted"}},
                )
                on_disk = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(on_disk["ticket_id"], "T-M")  # not clobbered
                self.assertEqual(on_disk["via"], "G-9")
                self.assertEqual(on_disk["decision"]["status"], "accepted")

    def test_auto_ideator_ticket_write_is_atomic(self) -> None:
        auto_ideator = _load_module(
            "auto_ideator_under_test",
            REPO_ROOT / "data" / "domains" / "ideation" / "auto_ideator.py",
        )
        workdir = self._workdir()
        auto_ideator.TICKETS_DIR = str(workdir)  # never write the repo vault

        with mock.patch.object(auto_ideator, "raise_gate", return_value="G-FAKE-1"):
            with mock.patch(
                "shared_kernel.atomic_io.os.replace", side_effect=OSError(FAULT)
            ):
                with self.assertRaises(OSError):
                    auto_ideator.save_idea_as_gated_ticket("A sovereign idea")

        self.assertEqual(list(workdir.glob("ticket_*.json")), [])
        self.assertEqual(_tmp_litter(workdir), [])

        with mock.patch.object(auto_ideator, "raise_gate", return_value="G-FAKE-1"):
            result = auto_ideator.save_idea_as_gated_ticket("A sovereign idea")

        written = workdir / result["ticket_file"]
        self.assertTrue(written.is_file())
        text = written.read_text(encoding="utf-8")
        # byte parity: the old writer appended a trailing newline
        self.assertTrue(text.endswith("\n"))
        on_disk = json.loads(text)
        self.assertEqual(on_disk["ticket_id"], result["ticket_id"])
        self.assertEqual(on_disk["via"], "G-FAKE-1")

    def test_auto_ideator_extracts_a_bracketed_topic(self) -> None:
        """The bracketed-title branch is the one a real beat took.

        Topic extraction has two branches and only the ``[Proposed Title]:``
        one reaches ``re.sub``; a plain sentence (what the atomic-write test
        above passes) breaks out at the ``elif`` and never touches it. The
        server hit the untested branch on its first beat and the ideator died
        with ``NameError: name 're' is not defined`` — a missing import that
        no test could see. This drives the branch that ran.
        """
        auto_ideator = _load_module(
            "auto_ideator_topic_under_test",
            REPO_ROOT / "data" / "domains" / "ideation" / "auto_ideator.py",
        )
        workdir = self._workdir()
        auto_ideator.TICKETS_DIR = str(workdir)

        with mock.patch.object(auto_ideator, "raise_gate", return_value="G-FAKE-2"):
            result = auto_ideator.save_idea_as_gated_ticket(
                "[Proposed Title]:    Sovereign ops in the age of agents\n\nBody."
            )

        self.assertEqual(result["topic"], "Sovereign ops in the age of agents")
        written = json.loads((workdir / result["ticket_file"]).read_text(encoding="utf-8"))
        self.assertEqual(written["context"]["topic"], result["topic"])


class TicketNWriterRace(unittest.TestCase):
    """API PATCH vs dispatcher rewrite vs tracker settle — no lost update."""

    SECTOR = "ideation"
    ROUNDS = 5

    def setUp(self) -> None:
        self.tickets_dir = DOMAINS / self.SECTOR / "tickets"
        self.tickets_dir.mkdir(parents=True, exist_ok=True)
        self.tracker = _load_tracker(self.SECTOR)
        self._created: list[Path] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for path in self._created:
            Path(path).unlink(missing_ok=True)

    def test_three_writers_lose_no_update(self) -> None:
        for round_no in range(self.ROUNDS):
            with self.subTest(round=round_no):
                self._race_once(round_no)

    def _race_once(self, round_no: int) -> None:
        ticket_id = f"T-RACE-{round_no}-{os.getpid()}"
        path = self.tickets_dir / f"ticket_{ticket_id}.json"
        self._created.append(path)
        base = {
            "ticket_id": ticket_id,
            "client_id": "race",
            "state": "open",
            "history": [],
            "context": {},
            "metadata": {},
        }
        path.write_text(
            json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        barrier = threading.Barrier(3)
        failures: list = []

        def api_patch() -> None:
            try:
                barrier.wait(timeout=10)
                req = api.UpdateTicketContextRequest(
                    context={"patched": round_no}, note="race"
                )
                asyncio.run(
                    api.update_ticket_context(
                        ticket_id, req, _request(), sector=self.SECTOR
                    )
                )
            except Exception as exc:  # noqa: BLE001 — recorded, asserted below
                failures.append(("api PATCH", repr(exc)))

        def dispatcher_rewrite() -> None:
            try:
                barrier.wait(timeout=10)
                ticket = json.loads(path.read_text(encoding="utf-8"))
                snapshot = copy.deepcopy(ticket)
                dispatcher.add_history(
                    ticket, ticket.get("state"), ticket.get("state"),
                    "dispatcher", "race rewrite",
                )
                dispatcher.save(str(path), ticket, base=snapshot)
            except Exception as exc:  # noqa: BLE001
                failures.append(("dispatcher rewrite", repr(exc)))

        def tracker_settle() -> None:
            try:
                barrier.wait(timeout=10)
                self.tracker._write_json(
                    str(path),
                    {
                        "via": f"G-RACE-{round_no}",
                        "state": "open",
                        "decision": {"status": "accepted", "reason": "race"},
                    },
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(("tracker settle", repr(exc)))

        threads = [
            threading.Thread(target=api_patch, name="api-patch"),
            threading.Thread(target=dispatcher_rewrite, name="dispatcher"),
            threading.Thread(target=tracker_settle, name="tracker"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "a writer deadlocked")

        self.assertEqual(failures, [], "every writer must land")
        final = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            final["context"].get("patched"), round_no, "API PATCH update lost"
        )
        self.assertTrue(
            any(h.get("by") == "dispatcher" for h in final["history"]),
            "dispatcher rewrite lost",
        )
        self.assertEqual(
            final.get("via"), f"G-RACE-{round_no}", "tracker settle lost"
        )
        self.assertEqual(final.get("decision", {}).get("status"), "accepted")
        self.assertEqual(_tmp_litter(self.tickets_dir), [])


class SidecarLockTimeout(unittest.TestCase):
    """A busy sidecar raises before any write — never an unlocked write."""

    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="so_test_sidecar_"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.path = self.workdir / "ticket_LOCK.json"
        self.original = {"ticket_id": "T-LOCK", "state": "open"}
        self.path.write_text(
            json.dumps(self.original, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def test_busy_lock_raises_and_the_writer_never_runs(self) -> None:
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with sidecar_lock(self.path, purpose="other-writer"):
                held.set()
                release.wait(timeout=15)

        holder = threading.Thread(target=hold, name="lock-holder")
        holder.start()
        try:
            self.assertTrue(held.wait(timeout=10), "lock never acquired")

            calls: list = []

            def mutate(state):
                calls.append(state)
                return dict(state, state="corrupted")

            before = self.path.read_bytes()
            with self.assertRaises(SidecarLockTimeoutError):
                update_json(self.path, mutate, timeout=0)
            self.assertEqual(calls, [], "no write may happen while locked out")
            self.assertEqual(self.path.read_bytes(), before, "bytes must survive")
        finally:
            release.set()
            holder.join(timeout=15)

        # the lock is free again: the update lands normally afterwards
        update_json(self.path, lambda state: dict(state, state="done"), timeout=5)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["state"], "done"
        )


if __name__ == "__main__":
    unittest.main()
