"""Approval entry tests: nothing enters the production spool outside the gate.

Phase A covers the two entry routes that could previously skip approval:

- ``POST /api/capsules`` (direct create) — now born through
  ``seat_at_approval_gate``: ``pending_approval``, a **fresh** gate plan and a
  bound artifact digest; when gates are enabled and the plan cannot be seeded,
  creation is refused and nothing is persisted.
- ``POST /api/sandbox/promote/{id}`` — whatever state the sandbox held
  (``approved``/``published`` included, i.e. an ``auto_approve`` trial run),
  the record lands ``pending_approval`` with a fresh plan; sandbox verdicts
  never survive the seat.

Plus the approval route itself: with a live plan, no approve call reaches
``approved`` until every gate has passed. Everything runs in the bootstrap
vault and removes what it creates (the vault is shared by the whole suite).
"""

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import asyncio
import hashlib
import shutil
import unittest
from unittest import mock

from tests._bootstrap import SPOOL, VAULT

from capsule_engine import State, gates as gate_engine
from capsule_engine.audit import verify_chain
from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import ARTIFACT_FILE, load_manifest, save_manifest
from capsule_engine.sandbox import CapsuleSandbox, PromotionError
from fastapi import HTTPException
from orchestration import access_control, api
from starlette.requests import Request

SANDBOX = VAULT / "sandbox"  # CAPSULES_SANDBOX from the bootstrap env

#: A plan of two human gates — used to prove an approve consumes exactly one
#: gate and cannot reach ``approved`` while the plan is incomplete.
TWO_HUMAN_GATES = (
    {"gate_type": "domain_lead_approval", "name": "Lead Gate One", "authority": "capsule:approve"},
    {"gate_type": "domain_lead_approval", "name": "Lead Gate Two", "authority": "capsule:approve"},
)


def _request(path: str = "/api/capsules") -> Request:
    """A request carrying a root principal (identity from the credential)."""
    principal = access_control.Principal.legacy_root("phase-a-tester")
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


def _rm(capsule_id: str) -> None:
    shutil.rmtree(SPOOL / capsule_id, ignore_errors=True)
    shutil.rmtree(SANDBOX / capsule_id, ignore_errors=True)


def _write_sandbox_capsule(capsule_id: str, state: str, *, gates=None) -> None:
    capsule_dir = SANDBOX / capsule_id
    capsule_dir.mkdir(parents=True, exist_ok=True)
    text = f"deliverable for {capsule_id}"
    (capsule_dir / ARTIFACT_FILE).write_text(text, encoding="utf-8")
    manifest = {
        "capsule_id": capsule_id,
        "domain": "marketing",
        "raw_prompt": text,
        "state": state,
        "attempt": 0,
        "created_at": "2026-10-12T09:00:00+00:00",
        "updated_at": "2026-10-12T09:00:00+00:00",
        "history": [],
        "metadata": {
            "domain": "marketing",
            "topic": capsule_id,
            "is_sandbox": True,
            "artifact_path": ARTIFACT_FILE,
            "artifact_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        },
    }
    if gates is not None:
        manifest["gates"] = gates
    save_manifest(capsule_dir, manifest)


def _sandbox_plan_already_passed():
    """A gate plan whose gates all PASSED inside the sandbox (auto-approve run)."""
    specs = gate_engine.specs_from_template(gate_engine.template_for("marketing"))
    return [spec.to_manifest(status=gate_engine.STATUS_PASSED) for spec in specs]


def _create(capsule_id: str, text: str = "phase A capsule") -> dict:
    req = api.CreateCapsuleRequest(text=text, capsule_id=capsule_id, sector="marketing")
    return asyncio.run(api.create_capsule_endpoint(req, _request()))


class PromoteSeatsAtTheApprovalGate(unittest.TestCase):
    """Sandbox promotion always lands pending_approval with a fresh plan."""

    STATES = (
        "draft",
        "in_progress",
        "in_review",
        "pending_qa",
        "pending_design",
        "pending_approval",
        "needs_revision",
        "approved",
        "published",
        "failed",
        "stalled",
    )

    def test_every_sandbox_state_lands_pending_approval_with_fresh_plan(self):
        for state in self.STATES:
            with self.subTest(state=state):
                capsule_id = f"sbx_{state.replace('_', '-')}"
                self.addCleanup(_rm, capsule_id)
                # The sandbox capsule even carries a fully PASSED plan — the
                # seat must discard it, not inherit it.
                _write_sandbox_capsule(capsule_id, state, gates=_sandbox_plan_already_passed())

                result = CapsuleSandbox().promote(capsule_id, by="tester", note="phase A")

                self.assertEqual(result["state"], State.PENDING_APPROVAL.value)
                self.assertNotIn(
                    result["state"],
                    (State.APPROVED.value, State.PUBLISHED.value),
                )
                manifest = load_manifest(SPOOL / capsule_id)
                self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)
                records = gate_engine.load_records(manifest)
                self.assertTrue(records, "a promoted capsule must carry a gate plan")
                self.assertTrue(
                    all(record.status == gate_engine.STATUS_PENDING for record in records),
                    [record.status for record in records],
                )
                self.assertFalse((manifest.get("metadata") or {}).get("is_sandbox"))
                self.assertFalse((SANDBOX / capsule_id).exists())

    def test_promote_keeps_response_shape_and_records_the_seat(self):
        capsule_id = "sbx_response_shape"
        self.addCleanup(_rm, capsule_id)
        _write_sandbox_capsule(capsule_id, "approved", gates=_sandbox_plan_already_passed())

        result = CapsuleSandbox().promote(capsule_id, by="tester", note="audited")

        self.assertEqual(
            set(result),
            {"ok", "capsule_id", "spool_path", "state", "promoted_at", "promoted_by"},
        )
        manifest = load_manifest(SPOOL / capsule_id)
        seat = [h for h in manifest["history"] if h.get("to_state") == "pending_approval"]
        self.assertTrue(seat)
        self.assertIsNone(seat[-1].get("from_state"))  # a birth at the gate
        self.assertIn("Promoted from sandbox", seat[-1].get("why", ""))
        self.assertTrue(verify_chain(manifest["history"]).ok)

    def test_seeding_failure_refuses_promotion_and_keeps_source(self):
        capsule_id = "sbx_seed_fail"
        self.addCleanup(_rm, capsule_id)
        _write_sandbox_capsule(capsule_id, "approved", gates=_sandbox_plan_already_passed())

        with mock.patch.object(
            gate_engine,
            "specs_from_template",
            side_effect=gate_engine.GateConfigError("template broken"),
        ):
            with self.assertRaises(PromotionError):
                CapsuleSandbox().promote(capsule_id, by="tester", note="")

        self.assertFalse((SPOOL / capsule_id).exists())  # rolled back: no ungoverned copy
        self.assertTrue((SANDBOX / capsule_id).exists())  # source kept for a retry


class CreateSeatsAtTheApprovalGate(unittest.TestCase):
    """POST /api/capsules is born through the shared creation helper."""

    def test_create_lands_pending_approval_with_plan_and_bound_digest(self):
        capsule_id = "cap_create_seat"
        self.addCleanup(_rm, capsule_id)
        text = "phase A direct capsule"

        result = _create(capsule_id, text)

        self.assertEqual(set(result), {"status", "capsule_id", "state", "message"})
        self.assertEqual(result["state"], State.PENDING_APPROVAL.value)

        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual((manifest.get("metadata") or {}).get("artifact_sha256"), expected)
        records = gate_engine.load_records(manifest)
        self.assertTrue(records, "a created capsule must carry a gate plan")
        self.assertTrue(
            all(record.status == gate_engine.STATUS_PENDING for record in records),
            [record.status for record in records],
        )
        seat = [h for h in manifest["history"] if h.get("to_state") == "pending_approval"]
        self.assertTrue(seat)
        self.assertIsNone(seat[-1].get("from_state"))
        self.assertTrue(verify_chain(manifest["history"]).ok)

    def test_create_refused_when_plan_cannot_be_seeded(self):
        capsule_id = "cap_create_seed_fail"
        self.addCleanup(_rm, capsule_id)

        with mock.patch.object(
            gate_engine,
            "specs_from_template",
            side_effect=gate_engine.GateConfigError("template broken"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                _create(capsule_id, "ungoverned attempt")

        self.assertEqual(ctx.exception.status_code, 400)
        # Fail closed: nothing was persisted, not even the directory shell.
        self.assertFalse((SPOOL / capsule_id).exists())


class ApproveRequiresGatePass(unittest.TestCase):
    """The approve route cannot reach ``approved`` before the plan passes."""

    def _replace_plan(self, capsule_id: str, template) -> None:
        path = SPOOL / capsule_id
        with CapsuleLock(path):
            manifest = load_manifest(path)
            manifest["gates"] = []
            specs = gate_engine.specs_from_template(template)
            gate_engine.ensure_plan(manifest, specs, by="tester", replace_empty=True)
            save_manifest(path, manifest)

    def _open_active_gate(self, capsule_id: str) -> str:
        path = SPOOL / capsule_id
        with CapsuleLock(path):
            manifest = load_manifest(path)
            active = gate_engine.active_record(gate_engine.load_records(manifest))
            self.assertIsNotNone(active)
            if not active.opened_at:
                gate_engine.open_gate(manifest, active.gate_id, path, by="tester")
            save_manifest(path, manifest)
            return active.gate_id

    def _open_and_pass_active_gate(self, capsule_id: str) -> str:
        path = SPOOL / capsule_id
        with CapsuleLock(path):
            manifest = load_manifest(path)
            active = gate_engine.active_record(gate_engine.load_records(manifest))
            self.assertIsNotNone(active)
            if not active.opened_at:
                gate_engine.open_gate(manifest, active.gate_id, path, by="tester")
            gate_engine.decide(
                manifest,
                active.gate_id,
                "approve",
                by="tester",
                reason="gate passed in test",
                capsule_path=path,
            )
            save_manifest(path, manifest)
            return active.gate_id

    def _pass_all_gates(self, capsule_id: str) -> None:
        """Open and pass every gate of the plan, one at a time, as the runner would."""
        for _ in range(16):
            path = SPOOL / capsule_id
            with CapsuleLock(path):
                manifest = load_manifest(path)
                active = gate_engine.active_record(gate_engine.load_records(manifest))
                if active is None:
                    return
            self._open_and_pass_active_gate(capsule_id)
        self.fail("gate plan did not complete")

    def test_approve_refused_while_automated_gate_is_pending(self):
        capsule_id = "cap_auto_gate"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(api.approve_capsule(capsule_id, _request()))

        self.assertEqual(ctx.exception.status_code, 409)
        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)
        records = gate_engine.load_records(manifest)
        self.assertEqual(records[0].status, gate_engine.STATUS_PENDING)

    def test_approve_consumes_one_gate_without_approving_the_capsule(self):
        capsule_id = "cap_two_human_gates"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        self._replace_plan(capsule_id, TWO_HUMAN_GATES)
        first_gate = self._open_active_gate(capsule_id)

        result = asyncio.run(api.approve_capsule(capsule_id, _request()))

        # Response shape unchanged; the capsule itself is NOT approved.
        self.assertEqual(
            set(result),
            {"status", "capsule_id", "gate_id", "decision", "new_state", "gates", "message"},
        )
        self.assertEqual(result["gate_id"], first_gate)
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(result["new_state"], State.PENDING_APPROVAL.value)

        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)
        records = gate_engine.load_records(manifest)
        self.assertEqual(records[0].gate_id, first_gate)
        self.assertEqual(records[0].status, gate_engine.STATUS_PASSED)
        self.assertEqual(records[1].status, gate_engine.STATUS_PENDING)

    def test_full_gate_pass_reaches_approved(self):
        capsule_id = "cap_full_gate_pass"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        self._pass_all_gates(capsule_id)

        with mock.patch.object(
            api, "run_pulse", side_effect=RuntimeError("pulse disabled in tests")
        ):
            result = asyncio.run(api.approve_capsule(capsule_id, _request()))

        self.assertEqual(set(result), {"status", "capsule_id", "new_state", "published", "message"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["new_state"], State.APPROVED.value)
        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.APPROVED.value)


if __name__ == "__main__":
    unittest.main()
