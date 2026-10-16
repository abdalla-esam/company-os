"""Shared fixtures for the ``/v1`` aggregate-read tests (M1a).

Not a test module (the discovery pattern is ``test_*.py``), so it holds the
vault fixtures the four read test files share:

- two sectors (``marketing``, ``development``) with capsules spread over several
  stages, including **a capsule halted at the revision cap** (built through the
  engine's own ``manifest.transition`` chokepoint, so the escalation and its
  recorded reason are real) and **a capsule at a human gate** (with the gate
  *opened*, so its bound artifact hashes exist);
- two tickets awaiting a decision (one per sector) plus the decision ledgers a
  capsule accumulates;
- one open gate-queue entry per sector;
- one capsule the index **cannot read** (a future schema version), so the
  quarantine path is exercised with a real record;
- one credential per caller shape (reader, scoped approver, wildcard, root,
  engine-wide read only).

Everything happens inside the bootstrap vault and is removed on cleanup.
"""

from __future__ import annotations  # noqa: E402  — the suite runs on 3.9

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import asyncio
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests._bootstrap import DOMAINS, GATE_QUEUE, SPOOL

from capsule_engine import gates as gate_engine
from capsule_engine.capabilities import mint_key
from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import State, load_manifest, save_manifest
from orchestration import access_control, api, read_index
from starlette.requests import Request
from starlette.testclient import TestClient

#: A test-only signing secret (>= the 32-byte minimum mint_key enforces).
TEST_SECRET = "m1a-reads-signing-secret-0123456789abcdef"
TEST_TTL_SECONDS = 600

MARKETING = "marketing"
DEVELOPMENT = "development"

MISSING_CAPSULE = "cap-does-not-exist-m1a"
MISSING_TICKET = "T-DOES-NOT-EXIST-m1a"

_CAPSULE_TEXT = (
    "A plain sentence of sufficient length for the reviewer to accept this "
    "probe capsule without any banned vocabulary."
)

_HUMAN_GATE = (
    {
        "gate_type": "domain_lead_approval",
        "name": "Lead Gate",
        "targets": ("copy.md",),
        "authority": "capsule:approve",
    },
)

#: Every capability a sector-full approver holds, minus engine-wide grants.
_SECTOR_WRITES = (
    "capsule:approve",
    "capsule:reject",
    "ticket:create",
    "gate:approve",
    "gate:reject",
)


def key(subject: str, *grants: str) -> str:
    return mint_key(
        subject,
        list(grants),
        secret=TEST_SECRET.encode("utf-8"),
        ttl_seconds=TEST_TTL_SECONDS,
    )


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def root_request() -> Request:
    """A root principal for fixture creation (creation is not under test here)."""
    principal = access_control.Principal.legacy_root("m1a-fixture")
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/capsules",
            "headers": [],
            "query_string": b"",
            "state": {"principal": principal},
        }
    )


def create_capsule(capsule_id: str, sector: str) -> Path:
    """Create a capsule through the real creation endpoint (seated at a gate)."""
    asyncio.run(
        api.create_capsule_endpoint(
            api.CreateCapsuleRequest(
                text=_CAPSULE_TEXT, capsule_id=capsule_id, sector=sector
            ),
            root_request(),
        )
    )
    return SPOOL / capsule_id


def seed_human_gate(path: Path, *, open_it: bool = False) -> str:
    """Replace a capsule's plan with one human gate (optionally opened/bound)."""
    with CapsuleLock(path):
        manifest = load_manifest(path)
        manifest["gates"] = []
        specs = gate_engine.specs_from_template(_HUMAN_GATE)
        gate_engine.ensure_plan(manifest, specs, by="m1a-fixture", replace_empty=True)
        if open_it:
            gate_id = manifest["gates"][0]["gate_id"]
            gate_engine.open_gate(manifest, gate_id, path, by="m1a-fixture")
        else:
            gate_id = manifest["gates"][0]["gate_id"]
        save_manifest(path, manifest)
    return gate_id


def halt_at_revision_cap(path: Path, cycles: int) -> None:
    """Drive a capsule over the revision cap through the real transition path.

    Each ``needs_revision`` entry increments the counter; one cycle back to
    ``in_progress`` makes the next entry lawful. The entry that crosses the cap
    escalates the capsule to ``failed`` inside the same atomic call, recording
    the engine's own reason string — which is exactly what the attention read
    must surface.
    """
    from capsule_engine.manifest import transition

    with CapsuleLock(path):
        manifest = load_manifest(path)
        for _ in range(cycles):
            transition(manifest, State.NEEDS_REVISION, by="m1a-qa", why="QA rejected")
            if State.parse(manifest["state"]) is State.NEEDS_REVISION:
                transition(manifest, State.IN_PROGRESS, by="m1a-worker", why="rework")
        save_manifest(path, manifest)


def advance_to(path: Path, state: State, *, why: str = "m1a fixture") -> None:
    """Move a capsule along one lawful edge (fixture state setup)."""
    from capsule_engine.manifest import transition

    with CapsuleLock(path):
        manifest = load_manifest(path)
        transition(manifest, state, by="m1a-fixture", why=why, capsule_path=path)
        save_manifest(path, manifest)


def set_metadata(path: Path, **metadata: object) -> None:
    """Stamp metadata (here: the cost estimate the home's tiles read)."""
    with CapsuleLock(path):
        manifest = load_manifest(path)
        target = manifest.get("metadata")
        if not isinstance(target, dict):
            target = {}
            manifest["metadata"] = target
        target.update(metadata)
        save_manifest(path, manifest)


def write_gate_entry(gate_id: str, sector: str, *, state: str = "open") -> Path:
    entry = {
        "gate_id": gate_id,
        "source": {
            "sector": sector,
            "client_id": "m1a-client",
            "request": "Publish this post.",
        },
        "risk_tier": "external",
        "reason": "A human must review this.",
        "opened_at": "2020-01-01T00:00:00+00:00",
        "state": state,
        "decision": None,
        "decided_by": None,
        "decided_at": None,
        "decision_reason": None,
    }
    path = GATE_QUEUE / f"gate_{gate_id}.json"
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def record_trace_steps(path: Path, capsule_id: str, *, steps: int = 3) -> None:
    """Write a real, hash-chained execution trace (the timeline's source)."""
    from capsule_engine.trace import ExecutionTracer

    tracer = ExecutionTracer(path, capsule_id=capsule_id)
    for index in range(steps):
        tracer.record_step(f"m1a-step-{index}", gear="m1a-gear")


def corrupt_capsule(capsule_id: str) -> Path:
    """A capsule whose ``schema_version`` this build does not understand."""
    path = SPOOL / capsule_id
    path.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 99,
        "capsule_id": capsule_id,
        "state": "draft",
        "domain": MARKETING,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "attempt": 0,
        "history": [],
    }
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


class V1ReadsCase(unittest.TestCase):
    """A live client, the two-sector fixture vault, and one key per caller."""

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {"AGENCY_CAPABILITY_SECRET": TEST_SECRET})
        self._env.start()
        self.addCleanup(self._env.stop)

        from ops.tools import ticket_mgr

        self.ticket_mgr = ticket_mgr
        original_root = ticket_mgr.REPO_ROOT
        ticket_mgr.REPO_ROOT = DOMAINS.parent.resolve()
        self.addCleanup(setattr, ticket_mgr, "REPO_ROOT", original_root)

        for sector in (MARKETING, DEVELOPMENT):
            (DOMAINS / sector).mkdir(parents=True, exist_ok=True)

        self.client = TestClient(api.app)
        self._created_capsules: list = []
        self._created_tickets: list = []
        self._gate_paths: list = []
        self._gate_seq = 0
        self.addCleanup(self._cleanup)

        # --- credentials ---------------------------------------------------
        self.marketing_headers = bearer(key("mkt-reader", f"domain:read:{MARKETING}"))
        self.development_headers = bearer(
            key("dev-reader", f"domain:read:{DEVELOPMENT}")
        )
        self.both_headers = bearer(
            key("both-reader", f"domain:read:{MARKETING}", f"domain:read:{DEVELOPMENT}")
        )
        self.wildcard_headers = bearer(key("wild-reader", "domain:read:*"))
        self.root_headers = bearer(key("root-reader", "*"))
        self.factory_read_headers = bearer(key("legacy-reader", "factory:read"))
        self.marketing_approver = bearer(
            key(
                "mkt-approver",
                f"domain:read:{MARKETING}",
                *(f"{cap}:{MARKETING}" for cap in _SECTOR_WRITES),
            )
        )
        self.development_approver = bearer(
            key(
                "dev-approver",
                f"domain:read:{DEVELOPMENT}",
                *(f"{cap}:{DEVELOPMENT}" for cap in _SECTOR_WRITES),
            )
        )
        # Reads both sectors, may decide only one: the shape that separates
        # "I can see it" from "I can act on it".
        self.mixed_headers = bearer(
            key(
                "mixed-approver",
                f"domain:read:{MARKETING}",
                f"domain:read:{DEVELOPMENT}",
                *(f"{cap}:{DEVELOPMENT}" for cap in _SECTOR_WRITES),
            )
        )

        # --- capsules ------------------------------------------------------
        self.gated = self._capsule("m1a_gated", MARKETING)
        self.gated_gate = seed_human_gate(self.gated, open_it=True)
        record_trace_steps(self.gated, self.gated.name, steps=3)

        self.development_capsule = self._capsule("m1a_dev", DEVELOPMENT)
        self.development_gate = seed_human_gate(self.development_capsule)

        self.halted = self._capsule("m1a_halted", MARKETING)
        halt_at_revision_cap(self.halted, 4)

        self.working = self._capsule("m1a_working", MARKETING)
        advance_to(self.working, State.NEEDS_REVISION, why="QA rejected once")
        advance_to(self.working, State.IN_PROGRESS, why="rework started")

        self.published = self._capsule("m1a_published", MARKETING)
        advance_to(self.published, State.NEEDS_REVISION, why="QA rejected once")
        advance_to(self.published, State.IN_PROGRESS, why="rework")
        advance_to(self.published, State.IN_REVIEW, why="artifact ready")
        set_metadata(self.published, estimated_cost_usd=0.42)

        self.quarantined = corrupt_capsule("m1a_quarantine")

        # --- tickets -------------------------------------------------------
        self.marketing_ticket = self._new_ticket(MARKETING)
        self.development_ticket = self._new_ticket(DEVELOPMENT)
        self.gated_ticket = self._new_ticket(MARKETING, stage="approved")

        # --- gate queue ----------------------------------------------------
        self.marketing_gate = self._new_gate(MARKETING)
        self.development_gate_id = self._new_gate(DEVELOPMENT)

        # The index is the read model's source: build it from the fixtures.
        read_index.rebuild()
        self.addCleanup(read_index.rebuild)

    # ---- fixture builders ------------------------------------------------- #

    def _capsule(self, prefix: str, sector: str) -> Path:
        capsule_id = f"{prefix}_{os.getpid()}_{len(self._created_capsules)}"
        path = create_capsule(capsule_id, sector)
        self._created_capsules.append(capsule_id)
        return path

    def _new_ticket(self, sector: str, *, stage: str = "scratch") -> str:
        response = self.ticket_mgr.create_ticket(
            sector=sector,
            title="A request awaiting a decision",
            content="A request body the reviewer would attest to.",
            stage=stage,
        )
        ticket_id = response["ticket_id"]
        self._created_tickets.append((sector, ticket_id))
        return ticket_id

    def _new_gate(self, sector: str) -> str:
        self._gate_seq += 1
        gate_id = f"G-M1A-{os.getpid()}-{self._gate_seq}"
        self._gate_paths.append(write_gate_entry(gate_id, sector))
        return gate_id

    def _cleanup(self) -> None:
        for capsule_id in self._created_capsules:
            shutil.rmtree(SPOOL / capsule_id, ignore_errors=True)
        shutil.rmtree(SPOOL / "m1a_quarantine", ignore_errors=True)
        for gate_path in self._gate_paths:
            Path(gate_path).unlink(missing_ok=True)
        for sector, ticket_id in self._created_tickets:
            tickets_root = DOMAINS / sector / "tickets"
            if tickets_root.is_dir():
                for path in tickets_root.rglob(f"ticket_{ticket_id}*.json"):
                    path.unlink(missing_ok=True)

    # ---- helpers ---------------------------------------------------------- #

    def get(self, path: str, headers: dict, **params: object):
        return self.client.get(path, headers=headers, params=params or None)

    def json_of(self, path: str, headers: dict, **params: object):
        response = self.get(path, headers, **params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def capsule_rel_path(self, capsule_id: str) -> Path:
        return SPOOL / capsule_id

    def capsule_ids(self, body: dict) -> set:
        return {
            item["capsule_id"]
            for item in body.get("items", [])
            if isinstance(item, dict) and item.get("capsule_id")
        }

    def attention_entities(self, body: dict) -> set:
        return {
            (item["entity"]["kind"], item["entity"]["id"])
            for item in body.get("items", [])
        }
