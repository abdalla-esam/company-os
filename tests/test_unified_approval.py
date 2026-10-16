"""Unified approval tests (Phase B): one approve path, one reject path.

The three capsule decision routes must reach the same final state and the
same side effects for the same decision — permissions, gate plan, state,
``feedback.txt``, feedback ledger, autonomy demotion, trace entry and event
all flow through ``orchestration.approval`` (the single source of truth).

Also covered here:

- the decision route's state guard, swept over the whole closed state
  vocabulary for both decisions (``StateGuardSweepsEveryState``);
- planless approval refusal + the ``ops/tools/seed_gate_plans.py`` remedy,
  whose ``--dry-run`` only counts/lists and which must never change state;
- the strictest-permission rule (branch capability AND the gate's declared
  authority);
- the decision route's unique handling: ``skip``, the 409 for a named gate
  that is not active, ``in_review`` reject, and the plan-complete-guarded
  ``in_review -> pending_approval`` seat (decision route AND gate runner);
- the gate seat's birth records its source (create vs sandbox promote).

Everything runs in the bootstrap vault and removes what it creates.
"""

from __future__ import annotations  # noqa: E402  — the suite runs on 3.9

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import asyncio
import contextlib
import hashlib
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests._bootstrap import SPOOL, VAULT

# Governance isolation: demotions must never touch the repo's config file.
# The file must EXIST before governance_path() resolves, or a same-named
# fallback would route writes to config/governance.json.
GOV_FILE = VAULT / "governance.json"


def _reset_governance(level: str = "L2", demotions: int = 0) -> None:
    GOV_FILE.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": None,
                "domains": {
                    "marketing": {
                        "level": level,
                        "updated_at": "2026-10-12T09:00:00+00:00",
                        "demotions": demotions,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


_reset_governance()
os.environ["AGENCY_GOVERNANCE_FILE"] = str(GOV_FILE)


def setUpModule() -> None:
    _reset_governance()
    os.environ["AGENCY_GOVERNANCE_FILE"] = str(GOV_FILE)

from capsule_engine import State, gates as gate_engine, trace as trace_engine  # noqa: E402
from capsule_engine.audit import verify_chain  # noqa: E402
from capsule_engine.exceptions import SecurityError, StateTransitionError  # noqa: E402
from capsule_engine.governance import (  # noqa: E402
    apply_rejection,
    record_autonomy_decision,
)
from capsule_engine.locking import CapsuleLock  # noqa: E402
from capsule_engine.manifest import (  # noqa: E402
    ARTIFACT_FILE,
    load_manifest,
    save_manifest,
    transition,
)
from fastapi import HTTPException  # noqa: E402
from orchestration import access_control, api, approval  # noqa: E402
from ops.tools import seed_gate_plans  # noqa: E402
from starlette.requests import Request  # noqa: E402

SANDBOX = VAULT / "sandbox"

#: A single human gate — used to exercise the decision route's named-gate
#: handling without stepping through the automated gates first.
ONE_HUMAN_GATE = (
    {"gate_type": "domain_lead_approval", "name": "Lead Gate", "authority": "capsule:approve"},
)

#: A single AUTOMATED gate — a plan the gate runner can finish on its own,
#: so its seat path (in_review -> pending_approval) is reachable in a test.
ONE_AUTOMATED_GATE = (
    {"gate_type": "qa_review", "name": "Automated QA", "targets": ("copy.md",)},
)

#: copy.md content the automated QA evaluator passes deterministically:
#: at least 40 characters, no placeholders, no banned vocabulary.
QA_PASSING_TEXT = (
    "A plain sentence of sufficient length for the automated reviewer to "
    "accept this probe capsule without any banned vocabulary."
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _rm(capsule_id: str) -> None:
    shutil.rmtree(SPOOL / capsule_id, ignore_errors=True)
    shutil.rmtree(SANDBOX / capsule_id, ignore_errors=True)


def _request(*grants: str, path: str = "/api/capsules") -> Request:
    """A request carrying a principal with exactly these grants (root if none)."""
    if grants:
        principal = access_control.Principal(
            subject="scoped-tester",
            grants=frozenset(access_control.Grant.parse(text) for text in grants),
            key_id="test-key-0123456789abcdef",
        )
    else:
        principal = access_control.Principal.legacy_root("phase-b-tester")
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


def _create(capsule_id: str, text: str = "phase B capsule") -> dict:
    req = api.CreateCapsuleRequest(text=text, capsule_id=capsule_id, sector="marketing")
    return asyncio.run(api.create_capsule_endpoint(req, _request()))


def _approve(capsule_id: str, request: Request | None = None) -> dict:
    _ensure_active_open(capsule_id)
    return asyncio.run(api.approve_capsule(capsule_id, request or _request()))


def _reject(capsule_id: str, reason: str, request: Request | None = None) -> dict:
    req = api.RejectRequest(reason=reason)
    return asyncio.run(api.reject_capsule(capsule_id, req, request or _request()))


def _active_gate_id(capsule_id: str) -> str:
    manifest = load_manifest(SPOOL / capsule_id)
    active = gate_engine.active_record(gate_engine.load_records(manifest))
    assert active is not None, "no active gate in fixture"
    return active.gate_id


def _decide(
    capsule_id: str,
    decision: str,
    *,
    reason: str | None = None,
    gate_id: str | None = None,
    request: Request | None = None,
) -> dict:
    if decision == "approve":
        _ensure_active_open(capsule_id)
    req = api.GateDecisionRequest(decision=decision, reason=reason)
    gate = gate_id if gate_id is not None else _active_gate_id(capsule_id)
    return asyncio.run(
        api.decide_capsule_gate(capsule_id, gate, req, request or _request())
    )


def _set_state(capsule_id: str, state: str) -> None:
    path = SPOOL / capsule_id
    with CapsuleLock(path):
        manifest = load_manifest(path)
        manifest["state"] = state
        save_manifest(path, manifest)


def _replace_plan(capsule_id: str, template) -> None:
    path = SPOOL / capsule_id
    with CapsuleLock(path):
        manifest = load_manifest(path)
        manifest["gates"] = []
        specs = gate_engine.specs_from_template(template)
        gate_engine.ensure_plan(manifest, specs, by="tester", replace_empty=True)
        save_manifest(path, manifest)


def _open_active(capsule_id: str) -> str:
    path = SPOOL / capsule_id
    with CapsuleLock(path):
        manifest = load_manifest(path)
        active = gate_engine.active_record(gate_engine.load_records(manifest))
        assert active is not None, "no active gate in fixture"
        if not active.opened_at:
            gate_engine.open_gate(manifest, active.gate_id, path, by="tester")
        save_manifest(path, manifest)
        return active.gate_id


def _pass_active(capsule_id: str) -> str:
    path = SPOOL / capsule_id
    with CapsuleLock(path):
        manifest = load_manifest(path)
        active = gate_engine.active_record(gate_engine.load_records(manifest))
        assert active is not None, "no active gate in fixture"
        if not active.opened_at:
            gate_engine.open_gate(manifest, active.gate_id, path, by="tester")
        gate_engine.decide(
            manifest, active.gate_id, "approve", by="tester",
            reason="gate passed in test", capsule_path=path,
        )
        save_manifest(path, manifest)
        return active.gate_id


def _pass_gates(capsule_id: str, count: int) -> None:
    for _ in range(count):
        _pass_active(capsule_id)


def _pass_all_gates(capsule_id: str) -> None:
    """Open and pass every gate until none is active (as the runner would)."""
    for _ in range(16):
        path = SPOOL / capsule_id
        with CapsuleLock(path):
            manifest = load_manifest(path)
            active = gate_engine.active_record(gate_engine.load_records(manifest))
            if active is None:
                return
        _pass_active(capsule_id)
    raise AssertionError("gate plan did not complete")


def _ensure_active_open(capsule_id: str) -> None:
    """Open the active gate when one exists — the runner does this before a human decides.

    Graceful on planless capsules (nothing to open): those paths are
    exercised through the service's own planless checks.
    """
    path = SPOOL / capsule_id
    with CapsuleLock(path):
        manifest = load_manifest(path)
        try:
            records = gate_engine.load_records(manifest)
        except Exception:
            return
        active = gate_engine.active_record(records)
        if active is not None and not active.opened_at:
            gate_engine.open_gate(manifest, active.gate_id, path, by="tester")
            save_manifest(path, manifest)


def _events_during(fn):
    """Run ``fn`` and return (its value, the approval events it published)."""
    subscriber = approval.subscribe_events()
    try:
        value = fn()
        return value, approval.drain_events(subscriber)
    finally:
        approval.unsubscribe_events(subscriber)


def _ledger_entries(capsule_id: str) -> list[dict]:
    path = Path(os.environ["AGENCY_MEMORY_FILE"])
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("capsule_id") == capsule_id:
            entries.append(entry)
    return entries


def _approval_trace_steps(capsule_id: str) -> list[dict]:
    read = trace_engine.read_trace(SPOOL / capsule_id)
    return [
        event
        for event in read.events
        if event.get("event") == "step_end" and event.get("gear_invoked") == "approval"
    ]


def _governance() -> dict:
    return json.loads(GOV_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# reject: both reject-capable routes reach the same state and side effects
# --------------------------------------------------------------------------- #


class RejectRoutesShareOnePath(unittest.TestCase):
    REASON = "needs a different angle"

    def _assert_shared_reject_side_effects(self, capsule_id: str, events: list) -> None:
        manifest = load_manifest(SPOOL / capsule_id)
        # 1. same final state, lawful edge recorded
        self.assertEqual(manifest["state"], State.NEEDS_REVISION.value)
        tail = manifest["history"][-1]
        self.assertEqual(tail.get("from_state"), "pending_approval")
        self.assertEqual(tail.get("to_state"), "needs_revision")
        self.assertTrue(verify_chain(manifest["history"]).ok)

        # 2. the active gate failed
        records = gate_engine.load_records(manifest)
        self.assertEqual(records[0].status, gate_engine.STATUS_FAILED)

        # 3. feedback.txt exists and always ends with a newline
        feedback = (SPOOL / capsule_id / "feedback.txt").read_text(encoding="utf-8")
        self.assertEqual(feedback, self.REASON + "\n")

        # 4. the feedback ledger recorded the lesson
        entries = _ledger_entries(capsule_id)
        self.assertTrue(entries, "feedback ledger entry missing")
        self.assertEqual(entries[-1].get("decision"), "rejected")
        self.assertEqual(entries[-1].get("reason"), self.REASON)

        # 5. the domain was demoted immediately (and idempotently)
        gov = _governance()
        entry = gov["domains"]["marketing"]
        self.assertEqual(entry["level"], "L0")
        self.assertEqual(entry["demotions"], 1)
        apply_rejection(manifest)  # what the pulse sweep will do later
        gov_after = _governance()
        self.assertEqual(gov_after["domains"]["marketing"]["demotions"], 1)
        self.assertEqual(gov_after["domains"]["marketing"]["level"], "L0")

        # 6. trace entry (chained) + decision event, both additive
        steps = _approval_trace_steps(capsule_id)
        self.assertTrue(steps, "trace entry missing")
        self.assertIn("rejected", steps[-1].get("step_name", ""))
        self.assertTrue(trace_engine.verify_trace(SPOOL / capsule_id).ok)
        rejected_events = [e for e in events if e.get("event") == "approval.rejected"]
        self.assertEqual(len(rejected_events), 1)
        self.assertEqual(rejected_events[0].get("capsule_id"), capsule_id)
        self.assertEqual(rejected_events[0].get("state"), "needs_revision")
        self.assertFalse(rejected_events[0].get("final"))

    def test_capsule_reject_and_decision_reject_share_side_effects(self):
        # --- route 1: POST /api/capsules/{id}/reject --------------------------
        _reset_governance()
        capsule_a = "cap_reject_route"
        self.addCleanup(_rm, capsule_a)
        _create(capsule_a)
        gate_a = _active_gate_id(capsule_a)

        result_a, events_a = _events_during(
            lambda: _reject(capsule_a, self.REASON)
        )
        self.assertEqual(
            set(result_a),
            {"status", "capsule_id", "new_state", "reason", "message"},
        )
        self.assertEqual(result_a["status"], "rejected")
        self.assertEqual(result_a["new_state"], State.NEEDS_REVISION.value)
        self._assert_shared_reject_side_effects(capsule_a, events_a)
        # route-preserved history wording
        manifest_a = load_manifest(SPOOL / capsule_a)
        self.assertTrue(manifest_a["history"][-1]["why"].startswith("Rejected:"))

        # --- route 2: POST /api/capsules/{id}/gates/{gate_id}/decision --------
        _reset_governance()
        capsule_b = "cap_reject_decision"
        self.addCleanup(_rm, capsule_b)
        _create(capsule_b)
        gate_b = _active_gate_id(capsule_b)

        result_b, events_b = _events_during(
            lambda: _decide(capsule_b, "reject", reason=self.REASON, gate_id=gate_b)
        )
        self.assertEqual(
            set(result_b),
            {"status", "capsule_id", "gate_id", "gate_type", "decision",
             "new_state", "gates", "message"},
        )
        self.assertEqual(result_b["decision"], "reject")
        self.assertEqual(result_b["new_state"], State.NEEDS_REVISION.value)
        self.assertEqual(result_b["gate_id"], gate_b)
        self._assert_shared_reject_side_effects(capsule_b, events_b)
        manifest_b = load_manifest(SPOOL / capsule_b)
        self.assertTrue(
            manifest_b["history"][-1]["why"].startswith(f"gate {gate_b} rejected:")
        )

    def test_ledger_failure_never_blocks_a_reject(self):
        _reset_governance()
        capsule_id = "cap_reject_ledger_down"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)

        with mock.patch.object(
            approval.memory_engine,
            "record_rejection",
            side_effect=OSError("ledger on fire"),
        ):
            with self.assertLogs("orchestration.approval", level="ERROR") as logs:
                result = _reject(capsule_id, "still rejects")

        self.assertEqual(result["new_state"], State.NEEDS_REVISION.value)
        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.NEEDS_REVISION.value)
        self.assertTrue(
            any("ledger" in line for line in logs.output),
            "the ledger failure must be logged loudly",
        )

    def test_decision_reject_from_in_review_is_lawful(self):
        # in_review is the decision route's second home: its reject uses the
        # lawful in_review -> needs_revision edge (the capsule route, which
        # stays pending_approval-only, refuses the same state).
        capsule_id = "cap_reject_in_review"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        _set_state(capsule_id, State.IN_REVIEW.value)
        gate = _active_gate_id(capsule_id)

        result, events = _events_during(
            lambda: _decide(
                capsule_id, "reject", reason="needs another angle", gate_id=gate
            )
        )
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["new_state"], State.NEEDS_REVISION.value)
        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.NEEDS_REVISION.value)
        tail = manifest["history"][-1]
        self.assertEqual(tail.get("from_state"), "in_review")
        self.assertEqual(tail.get("to_state"), "needs_revision")
        self.assertEqual(
            (SPOOL / capsule_id / "feedback.txt").read_text(encoding="utf-8"),
            "needs another angle\n",
        )
        rejected = [e for e in events if e.get("event") == "approval.rejected"]
        self.assertEqual(len(rejected), 1)

        # the capsule route keeps its pending_approval-only contract
        other = "cap_reject_in_review_capsule"
        self.addCleanup(_rm, other)
        _create(other)
        _set_state(other, State.IN_REVIEW.value)
        with self.assertRaises(HTTPException) as ctx:
            _reject(other, "nope")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("not 'pending_approval'", ctx.exception.detail)
        self.assertEqual(
            load_manifest(SPOOL / other)["state"], State.IN_REVIEW.value
        )


# --------------------------------------------------------------------------- #
# approve: both gate-consuming routes share the decision; final approval once
# --------------------------------------------------------------------------- #


class ApproveRoutesShareOnePath(unittest.TestCase):
    def test_gate_decision_identical_across_routes(self):
        # Capsule approve route consumes the active (human) gate...
        capsule_a = "cap_approve_route"
        self.addCleanup(_rm, capsule_a)
        _create(capsule_a)
        _pass_gates(capsule_a, 2)  # qa_review + security_audit
        gate = _active_gate_id(capsule_a)

        result_a, events_a = _events_during(lambda: _approve(capsule_a))
        self.assertEqual(
            set(result_a),
            {"status", "capsule_id", "gate_id", "decision", "new_state",
             "gates", "message"},
        )
        self.assertEqual(result_a["decision"], "approve")
        self.assertEqual(result_a["new_state"], State.PENDING_APPROVAL.value)

        # ...and the decision route does exactly the same to its twin.
        capsule_b = "cap_approve_decision"
        self.addCleanup(_rm, capsule_b)
        _create(capsule_b)
        _pass_gates(capsule_b, 2)
        gate_b = _active_gate_id(capsule_b)
        self.assertEqual(gate, gate_b)

        result_b, events_b = _events_during(
            lambda: _decide(capsule_b, "approve", gate_id=gate_b)
        )
        self.assertEqual(
            set(result_b),
            {"status", "capsule_id", "gate_id", "gate_type", "decision",
             "new_state", "gates", "message"},
        )
        self.assertEqual(result_b["decision"], "approve")

        for capsule_id in (capsule_a, capsule_b):
            manifest = load_manifest(SPOOL / capsule_id)
            self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)
            records = gate_engine.load_records(manifest)
            by_id = {record.gate_id: record for record in records}
            self.assertEqual(by_id[gate].status, gate_engine.STATUS_PASSED)
            # the remaining optional gate is still pending — NOT approved
            self.assertEqual(records[-1].status, gate_engine.STATUS_PENDING)
            self.assertTrue(verify_chain(manifest["history"]).ok)

        for events in (events_a, events_b):
            approved = [e for e in events if e.get("event") == "approval.approved"]
            self.assertEqual(len(approved), 1)
            self.assertFalse(approved[0].get("final"))
            self.assertEqual(approved[0].get("state"), "pending_approval")

        # both routes left a trace entry
        self.assertTrue(_approval_trace_steps(capsule_a))
        self.assertTrue(_approval_trace_steps(capsule_b))

    def test_full_plan_reaches_approved_with_final_event(self):
        capsule_id = "cap_approve_final"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        _pass_all_gates(capsule_id)

        with mock.patch.object(
            api, "run_pulse", side_effect=RuntimeError("pulse disabled in tests")
        ):
            result, events = _events_during(lambda: _approve(capsule_id))

        self.assertEqual(
            set(result),
            {"status", "capsule_id", "new_state", "published", "message"},
        )
        self.assertEqual(result["new_state"], State.APPROVED.value)
        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.APPROVED.value)
        final_events = [
            e for e in events
            if e.get("event") == "approval.approved" and e.get("final")
        ]
        self.assertEqual(len(final_events), 1)
        steps = _approval_trace_steps(capsule_id)
        self.assertIn("approved", steps[-1].get("step_name", ""))


# --------------------------------------------------------------------------- #
# the decision route's unique handling: skip /409 / in_review seat
# --------------------------------------------------------------------------- #


class DecisionRouteUniqueHandling(unittest.TestCase):
    def test_named_gate_must_be_active(self):
        capsule_id = "cap_named_not_active"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        active = _active_gate_id(capsule_id)

        with self.assertRaises(HTTPException) as ctx:
            _decide(capsule_id, "approve", gate_id=f"{active}-bogus")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("is not the active gate", ctx.exception.detail)

    def test_planless_decision_stays_404(self):
        capsule_id = "cap_decision_planless"
        self.addCleanup(_rm, capsule_id)
        _write_planless(capsule_id)

        with self.assertRaises(HTTPException) as ctx:
            _decide(capsule_id, "approve", gate_id="g-no-plan")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("no gate plan", ctx.exception.detail)

    def test_skip_of_optional_gate_and_refusal_of_required(self):
        # skip an optional gate (the sovereign sign-off)
        capsule_id = "cap_skip_optional"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        _pass_gates(capsule_id, 3)  # qa + security + lead
        gate = _active_gate_id(capsule_id)

        result = _decide(capsule_id, "skip", gate_id=gate)
        self.assertEqual(result["decision"], "skip")
        self.assertEqual(result["new_state"], State.PENDING_APPROVAL.value)
        self.assertNotIn("awaiting gate", result["message"])
        manifest = load_manifest(SPOOL / capsule_id)
        records = gate_engine.load_records(manifest)
        self.assertEqual(records[-1].status, gate_engine.STATUS_SKIPPED)
        self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)

        # a required gate may never be skipped
        required_id = "cap_skip_required"
        self.addCleanup(_rm, required_id)
        _create(required_id)
        gate = _active_gate_id(required_id)  # qa_review, required
        with self.assertRaises(HTTPException) as ctx:
            _decide(required_id, "skip", gate_id=gate)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("required and cannot be skipped", ctx.exception.detail)

    def test_plan_completion_seats_in_review_to_pending(self):
        """The seat works: a complete plan seats an in_review capsule.

        Also the regression proof for the old dead path — before the
        ``in_review -> pending_approval`` edge existed, this raise fired
        BEFORE ``save_manifest`` and the gate decision was lost.
        """
        capsule_id = "cap_in_review_seat"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        _replace_plan(capsule_id, ONE_HUMAN_GATE)
        _set_state(capsule_id, State.IN_REVIEW.value)
        gate = _open_active(capsule_id)

        result = _decide(capsule_id, "approve", gate_id=gate)

        self.assertEqual(
            set(result),
            {"status", "capsule_id", "gate_id", "gate_type", "decision",
             "new_state", "gates", "message"},
        )
        self.assertEqual(result["new_state"], State.PENDING_APPROVAL.value)

        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)
        # the gate decision reached disk — the old dead path lost it
        records = gate_engine.load_records(manifest)
        self.assertEqual(records[0].status, gate_engine.STATUS_PASSED)
        self.assertTrue(records[0].closed_at)
        # the seat is a real, chained move (in_review -> pending_approval)
        seat = manifest["history"][-1]
        self.assertEqual(seat.get("from_state"), "in_review")
        self.assertEqual(seat.get("to_state"), "pending_approval")
        self.assertTrue(verify_chain(manifest["history"]).ok)
        # the seat binds the artifact digest for the approval to come
        self.assertTrue((manifest.get("metadata") or {}).get("artifact_sha256"))

    def test_in_review_seat_refused_while_plan_incomplete(self):
        """transition() guards the new edge: no complete plan, no seat."""
        capsule_id = "cap_in_review_incomplete"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        _set_state(capsule_id, State.IN_REVIEW.value)

        path = SPOOL / capsule_id
        with CapsuleLock(path):
            manifest = load_manifest(path)
            with self.assertRaises(StateTransitionError) as ctx:
                transition(
                    manifest,
                    State.PENDING_APPROVAL,
                    by="tester",
                    why="seat attempt with an unfinished plan",
                    capsule_path=path,
                )
            self.assertIn("gate plan is incomplete", str(ctx.exception))
            # nothing was mutated: state and plan untouched
            self.assertEqual(manifest["state"], State.IN_REVIEW.value)

        # a planless in_review capsule may not seat either (no plan, no gate)
        bare_id = "cap_in_review_planless"
        self.addCleanup(_rm, bare_id)
        _write_planless(bare_id)
        _set_state(bare_id, State.IN_REVIEW.value)
        bare_path = SPOOL / bare_id
        with CapsuleLock(bare_path):
            bare = load_manifest(bare_path)
            with self.assertRaises(StateTransitionError) as ctx:
                transition(
                    bare,
                    State.PENDING_APPROVAL,
                    by="tester",
                    why="planless seat attempt",
                    capsule_path=bare_path,
                )
            self.assertIn("gate plan is incomplete", str(ctx.exception))

    def test_capsule_routes_keep_their_existing_state_checks(self):
        # unchanged behaviour: capsule approve/reject demand pending_approval
        approve_id = "cap_refuse_capsule_approve"
        self.addCleanup(_rm, approve_id)
        _create(approve_id)
        _set_state(approve_id, State.IN_REVIEW.value)
        with self.assertRaises(HTTPException) as ctx:
            _approve(approve_id)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("not 'pending_approval'", ctx.exception.detail)

        reject_id = "cap_refuse_capsule_reject"
        self.addCleanup(_rm, reject_id)
        _create(reject_id)
        _set_state(reject_id, State.APPROVED.value)
        with self.assertRaises(HTTPException) as ctx:
            _reject(reject_id, "why")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("not 'pending_approval'", ctx.exception.detail)

    def test_decision_route_can_no_longer_sign_an_automated_gate(self):
        # strictest-of-three: the capsule route's automated-gate 409 now
        # applies to the decision route too (previously it could decide one).
        capsule_id = "cap_refuse_decision_automated"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        gate = _active_gate_id(capsule_id)  # qa_review — automated

        with self.assertRaises(HTTPException) as ctx:
            _decide(capsule_id, "approve", gate_id=gate)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Automated gate", ctx.exception.detail)


class GateRunnerFinishPlanSeatsInReview(unittest.TestCase):
    """gate_runner._finish_plan's seat path: dead before, working now.

    The runner advances an ``in_review`` capsule through its automated gates
    and, when the plan completes, seats it at the approval gate — the exact
    path the missing table edge broke alongside the decision route's seat.
    """

    def test_runner_completes_the_plan_and_seats_the_capsule(self):
        from workers.gate_runner import GateRunnerWorker

        capsule_id = "cap_runner_seat"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id, QA_PASSING_TEXT)
        _replace_plan(capsule_id, ONE_AUTOMATED_GATE)
        _set_state(capsule_id, State.IN_REVIEW.value)

        result = GateRunnerWorker().run(str(SPOOL / capsule_id))

        self.assertEqual(result["state"], State.PENDING_APPROVAL.value)
        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)
        records = gate_engine.load_records(manifest)
        self.assertEqual(records[0].status, gate_engine.STATUS_PASSED)
        seat = manifest["history"][-1]
        self.assertEqual(seat.get("from_state"), "in_review")
        self.assertEqual(seat.get("to_state"), "pending_approval")
        self.assertIn("gate-runner", seat.get("by", ""))
        self.assertTrue(verify_chain(manifest["history"]).ok)


class ApprovalBoundaryGuardsEverySource(unittest.TestCase):
    """EVERY edge into approved/published is an approval boundary.

    For each lawful (source, target) pair: an unbound digest refuses, a
    tampered artifact refuses, the legacy path-less call refuses, and the
    bound+intact flow really reaches the target.
    """

    PAIRS = (
        (State.IN_REVIEW, State.APPROVED),
        (State.PENDING_DESIGN, State.APPROVED),
        (State.PENDING_APPROVAL, State.APPROVED),
        (State.APPROVED, State.PUBLISHED),
        (State.IN_PROGRESS, State.PUBLISHED),
    )

    def _fixture(self, tag: str, source: State) -> tuple[str, Path]:
        capsule_id = f"cap_boundary_{tag}_{source.value}".replace("_", "-")
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        _set_state(capsule_id, source.value)
        return capsule_id, SPOOL / capsule_id

    def _transition(self, capsule_id: str, target: State, *, path: bool = True) -> State:
        cap = SPOOL / capsule_id
        with CapsuleLock(cap):
            manifest = load_manifest(cap)
            result = transition(
                manifest,
                target,
                by="tester",
                why=f"boundary probe -> {target.value}",
                capsule_path=cap if path else None,
            )
            save_manifest(cap, manifest)  # refusals raise before this line
            return result

    def test_every_source_state_is_guarded(self):
        for source, target in self.PAIRS:
            edge = f"{source.value} -> {target.value}"

            with self.subTest(edge=edge, case="unbound-digest"):
                cid, cap = self._fixture("unbound", source)
                with CapsuleLock(cap):
                    manifest = load_manifest(cap)
                    (manifest.get("metadata") or {}).pop("artifact_sha256", None)
                    save_manifest(cap, manifest)
                with self.assertRaises(SecurityError):
                    self._transition(cid, target)
                self.assertEqual(load_manifest(cap)["state"], source.value)

            with self.subTest(edge=edge, case="tampered-artifact"):
                cid, cap = self._fixture("tampered", source)
                (cap / ARTIFACT_FILE).write_text(
                    "tampered after binding", encoding="utf-8"
                )
                with self.assertRaises(SecurityError):
                    self._transition(cid, target)
                self.assertEqual(load_manifest(cap)["state"], source.value)

            with self.subTest(edge=edge, case="legacy-no-path"):
                cid, cap = self._fixture("nopath", source)
                with self.assertRaises(SecurityError) as ctx:
                    self._transition(cid, target, path=False)
                self.assertIn("without a capsule path", str(ctx.exception))
                self.assertEqual(load_manifest(cap)["state"], source.value)

            with self.subTest(edge=edge, case="lawful"):
                cid, cap = self._fixture("lawful", source)
                self._transition(cid, target)
                manifest = load_manifest(cap)
                self.assertEqual(manifest["state"], target.value)
                tail = manifest["history"][-1]
                self.assertEqual(tail.get("from_state"), source.value)
                self.assertEqual(tail.get("to_state"), target.value)
                self.assertTrue(verify_chain(manifest["history"]).ok)


class GovernorHonoursTheGatePlan(unittest.TestCase):
    """The governor checks the gate plan itself — it no longer trusts the router."""

    def _prepare(self, capsule_id: str) -> None:
        _create(capsule_id)
        _reset_governance()  # marketing at L2 — deterministic autonomy
        cap = SPOOL / capsule_id
        with CapsuleLock(cap):
            manifest = load_manifest(cap)
            record_autonomy_decision(manifest, sample=0.99)  # no human required
            save_manifest(cap, manifest)

    def test_refuses_an_incomplete_plan(self):
        from workers.governor import GovernorWorker

        capsule_id = "cap_governor_incomplete"
        self.addCleanup(_rm, capsule_id)
        self._prepare(capsule_id)  # default plan: every gate still pending

        result = GovernorWorker().run(str(SPOOL / capsule_id))

        self.assertEqual(result["state"], State.PENDING_APPROVAL.value)
        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)
        self.assertIn("gate plan incomplete", json.dumps(manifest["history"]))

    def test_approves_when_the_plan_is_complete(self):
        from workers.governor import GovernorWorker

        capsule_id = "cap_governor_complete"
        self.addCleanup(_rm, capsule_id)
        self._prepare(capsule_id)
        _pass_all_gates(capsule_id)

        result = GovernorWorker().run(str(SPOOL / capsule_id))

        self.assertEqual(result["state"], State.APPROVED.value)


# --------------------------------------------------------------------------- #
# planless fixture builder (sweep refusals, seeder tests, the 404 path)
# --------------------------------------------------------------------------- #


def _write_planless(capsule_id: str, text: str = "planless capsule") -> None:
    capsule_dir = SPOOL / capsule_id
    capsule_dir.mkdir(parents=True, exist_ok=True)
    (capsule_dir / ARTIFACT_FILE).write_text(text, encoding="utf-8")
    now = "2026-10-12T09:00:00+00:00"
    save_manifest(
        capsule_dir,
        {
            "capsule_id": capsule_id,
            "domain": "marketing",
            "raw_prompt": text,
            "state": State.PENDING_APPROVAL.value,
            "attempt": 0,
            "created_at": now,
            "updated_at": now,
            "history": [],
            "metadata": {
                "domain": "marketing",
                "topic": capsule_id,
                "artifact_path": ARTIFACT_FILE,
                "artifact_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
        },
    )


class StateGuardSweepsEveryState(unittest.TestCase):
    """ONE sweep over the closed state vocabulary, both decisions.

    The decision route refuses every state except the two gating homes
    (``pending_approval`` and ``in_review``) — proven here for ALL capsule
    states, approve and reject alike; a refusal mutates nothing.
    """

    ALLOWED = frozenset({State.PENDING_APPROVAL.value, State.IN_REVIEW.value})

    def test_every_state_for_approve_and_reject(self):
        for state in sorted(member.value for member in State):
            for decision in ("approve", "reject"):
                with self.subTest(state=state, decision=decision):
                    capsule_id = f"cap_sweep_{decision}_{state.replace('_', '-')}"
                    self.addCleanup(_rm, capsule_id)
                    _create(capsule_id)
                    _set_state(capsule_id, state)

                    try:
                        if decision == "reject":
                            _decide(capsule_id, "reject", reason="sweep probe")
                        else:
                            _decide(capsule_id, "approve")
                        outcome, detail = "performed", ""
                    except HTTPException as exc:
                        refused = (
                            exc.status_code == 400 and "is in state" in str(exc.detail)
                        )
                        outcome = (
                            "state-refused" if refused else f"other:{exc.status_code}"
                        )
                        detail = str(exc.detail)

                    manifest = load_manifest(SPOOL / capsule_id)
                    if state in self.ALLOWED:
                        self.assertNotEqual(
                            outcome, "state-refused",
                            f"{decision} from {state} must not be state-refused "
                            f"({detail})",
                        )
                        if decision == "reject":
                            # both gating homes really perform the rejection
                            self.assertEqual(outcome, "performed", detail)
                            self.assertEqual(
                                manifest["state"], State.NEEDS_REVISION.value)
                    else:
                        self.assertEqual(
                            outcome, "state-refused",
                            f"{decision} from {state} must be refused ({detail})",
                        )
                        # a state refusal mutates nothing
                        self.assertEqual(manifest["state"], state)
                        self.assertFalse(
                            (SPOOL / capsule_id / "feedback.txt").exists())


# --------------------------------------------------------------------------- #
# planless refusal + the operator remedy
# --------------------------------------------------------------------------- #


class PlanlessRefusalAndSeeder(unittest.TestCase):
    def test_approve_refuses_planless_capsule_with_remedy(self):
        capsule_id = "cap_planless_approve"
        self.addCleanup(_rm, capsule_id)
        _write_planless(capsule_id)

        with self.assertRaises(HTTPException) as ctx:
            _approve(capsule_id)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("no gate plan", ctx.exception.detail)
        self.assertIn("fail closed", ctx.exception.detail)
        self.assertIn("seed_gate_plans", ctx.exception.detail)
        # nothing changed
        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)
        self.assertFalse(manifest.get("gates"))

    def test_dry_run_only_counts_and_lists(self):
        capsule_id = "cap_planless_dry_run"
        self.addCleanup(_rm, capsule_id)
        _write_planless(capsule_id)
        before = (SPOOL / capsule_id / "manifest.json").read_bytes()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seed_gate_plans.main(["--dry-run"])
        self.assertEqual(rc, 0)
        printed = out.getvalue()
        self.assertIn("pending capsules without a gate plan:", printed)
        self.assertIn(capsule_id, printed)
        self.assertIn("dry-run: nothing written.", printed)

        after = (SPOOL / capsule_id / "manifest.json").read_bytes()
        self.assertEqual(before, after, "--dry-run must not write anything")
        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)
        self.assertFalse(manifest.get("gates"))

    def test_seeder_fixes_the_capsule_without_touching_state(self):
        capsule_id = "cap_planless_seed"
        self.addCleanup(_rm, capsule_id)
        _write_planless(capsule_id)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seed_gate_plans.main(["--capsule-id", capsule_id])
        self.assertEqual(rc, 0)

        manifest = load_manifest(SPOOL / capsule_id)
        self.assertEqual(
            manifest["state"], State.PENDING_APPROVAL.value,
            "the seeder must never change a capsule's state",
        )
        records = gate_engine.load_records(manifest)
        self.assertTrue(records)
        self.assertTrue(
            all(record.status == gate_engine.STATUS_PENDING for record in records)
        )
        self.assertIn("gate plan initialized", json.dumps(manifest["history"]))

        # the remedy worked: approval now passes the planless refusal and
        # stops at the automated gate instead
        with self.assertRaises(HTTPException) as ctx:
            _approve(capsule_id)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Automated gate", ctx.exception.detail)


# --------------------------------------------------------------------------- #
# strictest permissions: branch capability AND the gate's declared authority
# --------------------------------------------------------------------------- #


class StrictestPermissions(unittest.TestCase):
    def test_gate_authority_required_for_reject_when_declared(self):
        # A rejecter holding only capsule:reject may no longer decide at a
        # gate that DECLARES capsule:approve — stricter than before (report).
        capsule_id = "cap_strict_reject"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        _pass_gates(capsule_id, 2)  # active = domain_lead, authority declared
        gate = _active_gate_id(capsule_id)
        rejecter = _request("capsule:reject:marketing")

        with self.assertRaises(HTTPException) as ctx:
            _decide(capsule_id, "reject", reason="nope", gate_id=gate, request=rejecter)
        self.assertEqual(ctx.exception.status_code, 403)

        with self.assertRaises(HTTPException) as ctx:
            _reject(capsule_id, "nope", request=rejecter)
        self.assertEqual(ctx.exception.status_code, 403)

        # and the capsule is untouched by either refusal
        self.assertEqual(
            load_manifest(SPOOL / capsule_id)["state"], State.PENDING_APPROVAL.value
        )

    def test_scoped_approver_still_decides(self):
        # not weaker than today: a scoped capsule:approve holder works
        capsule_id = "cap_strict_approve"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        _pass_gates(capsule_id, 2)
        approver = _request("capsule:approve:marketing")

        result = _approve(capsule_id, request=approver)
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(result["new_state"], State.PENDING_APPROVAL.value)

    def test_missing_branch_capability_refused(self):
        capsule_id = "cap_strict_missing"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        _pass_gates(capsule_id, 2)
        stranger = _request("capsule:create:marketing")

        with self.assertRaises(HTTPException) as ctx:
            _approve(capsule_id, request=stranger)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("capsule:approve", ctx.exception.detail)


# --------------------------------------------------------------------------- #
# the gate seat's birth records where it came from
# --------------------------------------------------------------------------- #


class SeatBirthRecordsSource(unittest.TestCase):
    def test_create_seat_says_create(self):
        capsule_id = "cap_seat_create"
        self.addCleanup(_rm, capsule_id)
        _create(capsule_id)
        manifest = load_manifest(SPOOL / capsule_id)
        seat = [
            entry for entry in manifest["history"]
            if entry.get("to_state") == "pending_approval"
            and entry.get("from_state") is None
        ]
        self.assertTrue(seat)
        self.assertEqual(seat[-1].get("by"), "human_cockpit")
        self.assertEqual(seat[-1].get("why"), "direct capsule injected via cockpit UI")

    def test_promote_seat_says_promote(self):
        from capsule_engine.sandbox import CapsuleSandbox

        capsule_id = "cap_seat_promote"
        self.addCleanup(_rm, capsule_id)
        capsule_dir = SANDBOX / capsule_id
        capsule_dir.mkdir(parents=True, exist_ok=True)
        text = "sandbox deliverable"
        (capsule_dir / ARTIFACT_FILE).write_text(text, encoding="utf-8")
        now = "2026-10-12T09:00:00+00:00"
        save_manifest(
            capsule_dir,
            {
                "capsule_id": capsule_id,
                "domain": "marketing",
                "raw_prompt": text,
                "state": State.APPROVED.value,  # sandbox auto-approve
                "attempt": 0,
                "created_at": now,
                "updated_at": now,
                "history": [],
                "metadata": {
                    "domain": "marketing",
                    "topic": capsule_id,
                    "is_sandbox": True,
                    "artifact_path": ARTIFACT_FILE,
                    "artifact_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                },
            },
        )
        CapsuleSandbox().promote(capsule_id, by="operator-dana", note="audited")

        manifest = load_manifest(SPOOL / capsule_id)
        seat = [
            entry for entry in manifest["history"]
            if entry.get("to_state") == "pending_approval"
            and entry.get("from_state") is None
        ]
        self.assertTrue(seat)
        self.assertEqual(seat[-1].get("by"), "operator-dana")
        self.assertTrue(
            seat[-1].get("why", "").startswith("Promoted from sandbox to production spool")
        )
        self.assertEqual(manifest["state"], State.PENDING_APPROVAL.value)


if __name__ == "__main__":
    unittest.main()
