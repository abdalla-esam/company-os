"""Slice 2 of the ``/v1`` mobile API: attested tickets + needs_gate decisions.

Slice 1 bound a *capsule* decision to the SHA-256 of exactly what the reviewer
saw, verified **inside the capsule lock**. This module proves the same contract
for the flow's two other small human decisions:

- **tickets** (``GET /v1/tickets/{id}/review`` / ``POST /v1/tickets/{id}/decision``)
  — accept an idea (``promote``) or send it back (``demote``);
- **needs_gate entries** (``GET /v1/gates/{id}/review`` /
  ``POST /v1/gates/{id}/decision``) — ``approve`` or ``reject``.

Both go through one shared service (:mod:`orchestration.v1.service`) that shapes
the mobile contract; the *writers* own the atomic, in-lock verification — the
ticket's sidecar lock (``ops.tools.ticket_mgr``) and the ``gate_script.py``
subprocess lock. The reviewed fields and the canonical digest come from
:mod:`shared_kernel.review_hash`, so the value hashed at review and the value
checked at write can never disagree.

What this module pins, end to end through the real ASGI stack and the real
subprocess:

- ``review`` is a **pure read** — the ticket file / gate entry is byte- and
  mtime-identical after it, no gate opened, nothing bound;
- attestation is **mandatory** on the ``/v1`` accept/approve paths
  (``ATTESTATION_REQUIRED``) and **optional** on the legacy ``/api`` routes,
  which now record ``attested=false`` decisions;
- the attested element set must equal the reviewed set exactly
  (``HASH_SET_MISMATCH``); a byte that changed after review is
  ``STALE_CONTENT_HASH`` and **nothing is written**;
- ``idempotency_key`` replays a repeat and refuses a reused key
  (``IDEMPOTENCY_CONFLICT``); a second, non-replayed decision is
  ``ALREADY_DECIDED`` carrying ``decided_by``/``decided_at``;
- a send-back / rejection carries a **reason**, never a hash;
- ``/v1`` refuses a ticket state transition ``can_transition`` rejects
  (``ILLEGAL_STATE``) — legacy behaviour is untouched;
- scope is enforced on the target's own branch (``INSUFFICIENT_SCOPE``);
- N concurrent gate processes let exactly one decision win.

Static guards (AST scans; frozen inventories that may only change on purpose):

- the callers of ``ticket_mgr.promote_ticket``/``demote_ticket`` are pinned;
- the callers that build a ``gate_script ... decide`` argv are pinned;
- within the ``orchestration/v1`` package only ``service.py`` reaches either
  writer — the router owns no verification;
- the four new ``/v1`` routes keep their capabilities;
- :func:`shared_kernel.review_hash.reviewed_digest` equals
  ``orchestration.approval._finalized_reviewed`` (one attestation shape).

Everything runs in the bootstrap vault and removes what it creates.
"""

from __future__ import annotations  # noqa: E402  — the suite runs on 3.9

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests._bootstrap import DOMAINS, GATE_QUEUE, REPO_ROOT

from capsule_engine.capabilities import mint_key
from orchestration import access_control, api, approval
from shared_kernel import review_hash
from starlette.testclient import TestClient

#: A test-only signing secret (>= the 32-byte minimum mint_key enforces).
TEST_SECRET = "slice2-test-signing-secret-0123456789abcdef"
TEST_TTL_SECONDS = 600

#: The branch under test, plus a second branch a wrong key may be scoped to.
SECTOR = "marketing"
OTHER_SECTOR = "ideation"

#: The real gate script a decision is delegated to (as production runs it).
GATE_SCRIPT = REPO_ROOT / "data" / "governance" / "approval_gates" / "gate_script.py"

#: The reviewed element names the two entities expose.
TICKET_FIELDS = frozenset({"title", "request", "context"})
GATE_FIELDS = frozenset({"source", "reason", "risk_tier"})


def _key(*grants: str) -> str:
    return mint_key(
        "slice2-tester",
        list(grants),
        secret=TEST_SECRET.encode("utf-8"),
        ttl_seconds=TEST_TTL_SECONDS,
    )


def _run_gate(args, *, input_text=None):
    """Run ``gate_script.py`` exactly as production does: a fresh subprocess."""
    env = dict(os.environ)
    env["GATE_QUEUE_DIR"] = str(GATE_QUEUE)
    return subprocess.run(
        [sys.executable, str(GATE_SCRIPT), *args],
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


# --------------------------------------------------------------------------- #
# base case: an isolated vault, a client, and scoped keys
# --------------------------------------------------------------------------- #


class V1TicketGateTestCase(unittest.TestCase):
    """Shared credential environment, vault sector, and cleanup."""

    def setUp(self) -> None:
        from ops.tools import ticket_mgr

        self.ticket_mgr = ticket_mgr
        self._env = mock.patch.dict(
            os.environ, {"AGENCY_CAPABILITY_SECRET": TEST_SECRET}
        )
        self._env.start()
        self.addCleanup(self._env.stop)

        # ``ticket_mgr`` reports paths relative to the repository; in the vault
        # the tickets live outside it, so point it at the vault root (resolved —
        # ``shared_kernel.paths`` hands out resolved paths).
        original_root = ticket_mgr.REPO_ROOT
        ticket_mgr.REPO_ROOT = DOMAINS.parent.resolve()
        self.addCleanup(setattr, ticket_mgr, "REPO_ROOT", original_root)

        (DOMAINS / SECTOR).mkdir(parents=True, exist_ok=True)

        self.client = TestClient(api.app)
        self.read_headers = {"Authorization": "Bearer " + _key(f"domain:read:{SECTOR}")}
        self.ticket_headers = {
            "Authorization": "Bearer "
            + _key("factory:read", f"ticket:create:{SECTOR}")
        }
        self.gate_headers = {
            "Authorization": "Bearer "
            + _key(
                "factory:read",
                f"gate:approve:{SECTOR}",
                f"gate:reject:{SECTOR}",
            )
        }
        self._ticket_ids: list[str] = []
        self._gate_paths: list[Path] = []
        self._gate_seq = 0
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for gate_path in self._gate_paths:
            Path(gate_path).unlink(missing_ok=True)
        tickets_root = DOMAINS / SECTOR / "tickets"
        if tickets_root.is_dir():
            for tid in self._ticket_ids:
                for path in tickets_root.rglob(f"ticket_{tid}*.json"):
                    path.unlink(missing_ok=True)

    # ---- tickets ---------------------------------------------------------- #

    def _ticket_path(self, ticket_id: str, stage: str) -> Path:
        folder = "approved" if stage == "approved" else "scratch"
        return DOMAINS / SECTOR / "tickets" / folder / f"ticket_{ticket_id}.json"

    def _new_ticket(self, *, stage: str = "scratch"):
        response = self.ticket_mgr.create_ticket(
            sector=SECTOR,
            title="Slice 2 idea",
            content="A request body the reviewer will attest to.",
            stage=stage,
        )
        ticket_id = response["ticket_id"]
        self._ticket_ids.append(ticket_id)
        return ticket_id, self._ticket_path(ticket_id, stage)

    def _review_ticket(self, ticket_id: str) -> dict:
        response = self.client.get(
            f"/v1/tickets/{ticket_id}/review", headers=self.read_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _decide_ticket(self, ticket_id: str, **body):
        return self.client.post(
            f"/v1/tickets/{ticket_id}/decision",
            headers=self.ticket_headers,
            json=body,
        )

    # ---- gates ------------------------------------------------------------ #

    def _new_gate(self, *, sector: str = SECTOR, reason: str = "A human must review this."):
        self._gate_seq += 1
        gate_id = f"G-S2-{os.getpid()}-{self._gate_seq}"
        path = GATE_QUEUE / f"gate_{gate_id}.json"
        entry = {
            "gate_id": gate_id,
            "source": {
                "sector": sector,
                "client_id": "slice2-client",
                "request": "Publish this post.",
            },
            "risk_tier": "external",
            "reason": reason,
            "opened_at": "2020-01-01T00:00:00+00:00",
            "state": "open",
            "decision": None,
            "decided_by": None,
            "decided_at": None,
            "decision_reason": None,
        }
        path.write_text(
            json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._gate_paths.append(path)
        return gate_id, path, entry

    def _review_gate(self, gate_id: str) -> dict:
        response = self.client.get(
            f"/v1/gates/{gate_id}/review", headers=self.read_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _decide_gate(self, gate_id: str, **body):
        return self.client.post(
            f"/v1/gates/{gate_id}/decision",
            headers=self.gate_headers,
            json=body,
        )

    # ---- shared shapes ---------------------------------------------------- #

    @staticmethod
    def _echo(review: dict) -> dict:
        """The attestation a client would post back from a review body."""
        reviewed = review["reviewed"]
        return {
            "algorithm": reviewed["algorithm"],
            "elements": dict(reviewed["elements"]),
            "unviewed": list(reviewed.get("unviewed") or []),
            "primary_sha256": reviewed.get("primary_sha256"),
        }

    def _assert_reviewed_digest_is_shared(self, review: dict) -> None:
        self.assertEqual(
            review["reviewed_sha256"],
            approval._finalized_reviewed(review["reviewed"]),
            "the review must report the same digest the decision will record",
        )


# --------------------------------------------------------------------------- #
# review is a pure read
# --------------------------------------------------------------------------- #


class ReviewIsAPureRead(V1TicketGateTestCase):
    """The review endpoints must not mutate what they report on."""

    def test_ticket_review_does_not_touch_the_ticket(self) -> None:
        ticket_id, path = self._new_ticket()
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns

        review = self._review_ticket(ticket_id)

        self.assertEqual(path.read_bytes(), before_bytes, "ticket changed")
        self.assertEqual(
            path.stat().st_mtime_ns, before_mtime, "ticket mtime changed"
        )
        self.assertEqual(review["binding"], "elements")
        self.assertEqual(set(review["reviewed"]["elements"]), TICKET_FIELDS)
        self.assertNotIn("decisions", json.loads(before_bytes))

    def test_gate_review_does_not_touch_the_entry(self) -> None:
        gate_id, path, _entry = self._new_gate()
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns

        review = self._review_gate(gate_id)

        self.assertEqual(path.read_bytes(), before_bytes, "gate entry changed")
        self.assertEqual(
            path.stat().st_mtime_ns, before_mtime, "gate entry mtime changed"
        )
        self.assertEqual(review["binding"], "elements")
        self.assertEqual(set(review["reviewed"]["elements"]), GATE_FIELDS)
        self.assertEqual(json.loads(before_bytes)["state"], "open")


# --------------------------------------------------------------------------- #
# the reviewed surface: exact fields, exact digests
# --------------------------------------------------------------------------- #


class ReviewSurfacesAreExact(V1TicketGateTestCase):
    def test_ticket_review_hashes_title_request_context(self) -> None:
        ticket_id, path = self._new_ticket()
        review = self._review_ticket(ticket_id)

        data = json.loads(path.read_text(encoding="utf-8"))
        expected = review_hash.elements_for(review_hash.ticket_review_fields(data))
        self.assertEqual(review["reviewed"]["elements"], expected)
        self._assert_reviewed_digest_is_shared(review)

    def test_gate_review_hashes_source_reason_risk_tier(self) -> None:
        gate_id, path, _entry = self._new_gate()
        review = self._review_gate(gate_id)

        data = json.loads(path.read_text(encoding="utf-8"))
        expected = review_hash.elements_for(review_hash.gate_review_fields(data))
        self.assertEqual(review["reviewed"]["elements"], expected)
        self._assert_reviewed_digest_is_shared(review)

    def test_review_hash_is_shared_with_the_capsule_digest(self) -> None:
        reviewed = {
            "algorithm": "sha256",
            "elements": {"copy.md": "a" * 64, "hero.png": "b" * 64},
            "unviewed": ["x"],
            "primary_sha256": None,
        }
        self.assertEqual(
            review_hash.reviewed_digest(
                elements=reviewed["elements"],
                algorithm=reviewed["algorithm"],
                unviewed=reviewed["unviewed"],
                primary_sha256=reviewed["primary_sha256"],
            ),
            approval._finalized_reviewed(reviewed),
            "review_hash.reviewed_digest must stay byte-for-byte the capsule "
            "canonical form — the /v1 client treats both attestations the same",
        )


# --------------------------------------------------------------------------- #
# attestation is mandatory on /v1, optional on /api
# --------------------------------------------------------------------------- #


class AttestationIsMandatory(V1TicketGateTestCase):
    def test_ticket_promote_without_reviewed_is_refused(self) -> None:
        ticket_id, path = self._new_ticket()

        response = self._decide_ticket(ticket_id, action="promote")

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "ATTESTATION_REQUIRED")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["stage"], "scratch")
        self.assertNotIn("decisions", data)

    def test_gate_approve_without_reviewed_is_refused(self) -> None:
        gate_id, path, _entry = self._new_gate()
        before = path.read_bytes()

        response = self._decide_gate(gate_id, action="approve")

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "ATTESTATION_REQUIRED")
        self.assertEqual(path.read_bytes(), before, "nothing may be written")

    def test_legacy_ticket_promote_needs_no_attestation(self) -> None:
        ticket_id, _path = self._new_ticket()

        response = self.client.post(
            f"/api/tickets/{ticket_id}/promote", headers=self.ticket_headers
        )

        self.assertEqual(response.status_code, 200, response.text)
        on_disk = json.loads(
            self._ticket_path(ticket_id, "approved").read_text(encoding="utf-8")
        )
        record = on_disk["decisions"][-1]
        self.assertFalse(record["attested"])
        self.assertIsNone(record["reviewed_sha256"])

    def test_legacy_gate_approve_needs_no_attestation(self) -> None:
        gate_id, path, _entry = self._new_gate()

        response = self.client.post(
            f"/api/gates/{gate_id}/approve", headers=self.gate_headers, json={}
        )

        self.assertEqual(response.status_code, 200, response.text)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["state"], "closed")
        self.assertFalse(on_disk["evidence"]["attested"])
        self.assertIsNone(on_disk["evidence"]["reviewed_sha256"])


# --------------------------------------------------------------------------- #
# the attested set must equal the reviewed set exactly
# --------------------------------------------------------------------------- #


class ElementSetMustMatchExactly(V1TicketGateTestCase):
    def test_ticket_extra_element_is_a_set_mismatch(self) -> None:
        ticket_id, path = self._new_ticket()
        reviewed = self._echo(self._review_ticket(ticket_id))
        reviewed["elements"]["notes.txt"] = "0" * 64

        response = self._decide_ticket(
            ticket_id, action="promote", reviewed=reviewed
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "HASH_SET_MISMATCH")
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["stage"], "scratch"
        )

    def test_gate_missing_element_is_a_set_mismatch(self) -> None:
        gate_id, path, _entry = self._new_gate()
        reviewed = self._echo(self._review_gate(gate_id))
        del reviewed["elements"]["reason"]

        response = self._decide_gate(gate_id, action="approve", reviewed=reviewed)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "HASH_SET_MISMATCH")
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["state"], "open"
        )


# --------------------------------------------------------------------------- #
# a byte that changed after review fails closed
# --------------------------------------------------------------------------- #


class StaleContentFailsClosed(V1TicketGateTestCase):
    def test_ticket_edited_after_review_is_stale(self) -> None:
        ticket_id, path = self._new_ticket()
        reviewed = self._echo(self._review_ticket(ticket_id))

        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["request"] = "edited after the reviewer looked"
        path.write_text(
            json.dumps(changed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        before = path.read_bytes()

        response = self._decide_ticket(
            ticket_id, action="promote", reviewed=reviewed
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "STALE_CONTENT_HASH")
        self.assertEqual(path.read_bytes(), before, "nothing may be written")
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["stage"], "scratch"
        )

    def test_gate_edited_after_review_is_stale(self) -> None:
        gate_id, path, _entry = self._new_gate()
        reviewed = self._echo(self._review_gate(gate_id))

        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["reason"] = "edited after the reviewer looked"
        path.write_text(
            json.dumps(changed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        before = path.read_bytes()

        response = self._decide_gate(gate_id, action="approve", reviewed=reviewed)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "STALE_CONTENT_HASH")
        self.assertEqual(path.read_bytes(), before, "nothing may be written")
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["state"], "open"
        )


# --------------------------------------------------------------------------- #
# one tap, one decision: idempotency
# --------------------------------------------------------------------------- #


class IdempotentReplay(V1TicketGateTestCase):
    def test_ticket_repeat_with_same_key_replays(self) -> None:
        ticket_id, _path = self._new_ticket()
        reviewed = self._echo(self._review_ticket(ticket_id))

        first = self._decide_ticket(
            ticket_id, action="promote", reviewed=reviewed, idempotency_key="tap-1"
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["replayed"])

        second = self._decide_ticket(
            ticket_id, action="promote", reviewed=reviewed, idempotency_key="tap-1"
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(second.json()["decision_id"], first.json()["decision_id"])

    def test_ticket_same_key_different_payload_is_a_conflict(self) -> None:
        ticket_id, _path = self._new_ticket()
        reviewed = self._echo(self._review_ticket(ticket_id))

        self._decide_ticket(
            ticket_id, action="promote", reviewed=reviewed, idempotency_key="tap-2"
        )
        changed = dict(reviewed)
        changed["elements"] = {k: "0" * 64 for k in reviewed["elements"]}

        conflict = self._decide_ticket(
            ticket_id, action="promote", reviewed=changed, idempotency_key="tap-2"
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")

    def test_gate_repeat_with_same_key_replays(self) -> None:
        gate_id, _path, _entry = self._new_gate()
        reviewed = self._echo(self._review_gate(gate_id))

        first = self._decide_gate(
            gate_id, action="approve", reviewed=reviewed, idempotency_key="g-1"
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["replayed"])

        second = self._decide_gate(
            gate_id, action="approve", reviewed=reviewed, idempotency_key="g-1"
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(second.json()["decision_id"], first.json()["decision_id"])

    def test_gate_same_key_different_payload_is_a_conflict(self) -> None:
        gate_id, _path, _entry = self._new_gate()
        reviewed = self._echo(self._review_gate(gate_id))

        self._decide_gate(
            gate_id, action="approve", reviewed=reviewed, idempotency_key="g-2"
        )

        conflict = self._decide_gate(
            gate_id,
            action="approve",
            reviewed=reviewed,
            reason="different",
            idempotency_key="g-2",
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")


class AlreadyDecidedIsRecorded(V1TicketGateTestCase):
    def test_ticket_second_decision_is_refused_with_identity(self) -> None:
        ticket_id, _path = self._new_ticket()
        reviewed = self._echo(self._review_ticket(ticket_id))
        self._decide_ticket(
            ticket_id, action="promote", reviewed=reviewed, idempotency_key="first"
        )

        response = self._decide_ticket(
            ticket_id, action="promote", reviewed=reviewed, idempotency_key="second"
        )

        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()["error"]
        self.assertEqual(body["code"], "ALREADY_DECIDED")
        self.assertEqual(body["details"]["decided_by"], "slice2-tester")
        self.assertTrue(body["details"]["decided_at"])

    def test_gate_second_decision_is_refused_with_identity(self) -> None:
        gate_id, _path, _entry = self._new_gate()
        reviewed = self._echo(self._review_gate(gate_id))
        self._decide_gate(gate_id, action="approve", reviewed=reviewed)

        response = self._decide_gate(gate_id, action="approve", reviewed=reviewed)

        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()["error"]
        self.assertEqual(body["code"], "ALREADY_DECIDED")
        self.assertEqual(body["details"]["decided_by"], "slice2-tester")
        self.assertTrue(body["details"]["decided_at"])


# --------------------------------------------------------------------------- #
# send-back / rejection: a reason, never a hash
# --------------------------------------------------------------------------- #


class SendBackAndRejectCarryAReason(V1TicketGateTestCase):
    def test_ticket_demote_with_reason_succeeds_unattested(self) -> None:
        ticket_id, path = self._new_ticket(stage="approved")

        response = self._decide_ticket(
            ticket_id, action="demote", reason="needs another pass"
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["action"], "demote")
        self.assertFalse(body["attested"])
        self.assertEqual(body["stage_after"], "scratch")
        self.assertEqual(body["state_after"], "draft")
        self.assertFalse(path.exists(), "the source is moved out of approved/")
        on_disk = json.loads(
            self._ticket_path(ticket_id, "scratch").read_text(encoding="utf-8")
        )
        self.assertEqual(on_disk["decisions"][-1]["reason"], "needs another pass")
        self.assertFalse(on_disk["decisions"][-1]["attested"])

    def test_ticket_demote_without_reason_is_refused(self) -> None:
        ticket_id, path = self._new_ticket(stage="approved")
        before = path.read_bytes()

        response = self._decide_ticket(ticket_id, action="demote", reason="  ")

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(path.read_bytes(), before)

    def test_gate_reject_with_reason_succeeds_unattested(self) -> None:
        gate_id, path, _entry = self._new_gate()

        response = self._decide_gate(
            gate_id, action="reject", reason="off brand"
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["action"], "reject")
        self.assertFalse(body["attested"])
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["state"], "closed")
        self.assertEqual(on_disk["decision"], "rejected")
        self.assertFalse(on_disk["evidence"]["attested"])

    def test_gate_reject_without_reason_is_refused(self) -> None:
        gate_id, path, _entry = self._new_gate()
        before = path.read_bytes()

        response = self._decide_gate(gate_id, action="reject", reason="  ")

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(path.read_bytes(), before)


# --------------------------------------------------------------------------- #
# /v1 refuses an illegal ticket transition; legacy is untouched
# --------------------------------------------------------------------------- #


class V1UsesCanTransition(V1TicketGateTestCase):
    def test_ticket_demote_of_a_terminal_state_is_illegal(self) -> None:
        ticket_id, path = self._new_ticket(stage="approved")
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["state"] = "done"  # no lawful edge done -> draft
        path.write_text(
            json.dumps(changed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        before = path.read_bytes()

        response = self._decide_ticket(
            ticket_id, action="demote", reason="send it back"
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "ILLEGAL_STATE")
        self.assertEqual(path.read_bytes(), before, "nothing may be written")


# --------------------------------------------------------------------------- #
# scope is enforced on the target's own branch
# --------------------------------------------------------------------------- #


class ScopeIsEnforced(V1TicketGateTestCase):
    def test_unauthenticated_review_is_enveloped(self) -> None:
        ticket_id, _path = self._new_ticket()
        response = self.client.get(f"/v1/tickets/{ticket_id}/review")
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["error"]["code"], "UNAUTHENTICATED")

    def test_ticket_decision_needs_the_write_capability(self) -> None:
        ticket_id, _path = self._new_ticket()
        response = self.client.post(
            f"/v1/tickets/{ticket_id}/decision",
            headers=self.read_headers,
            json={"action": "promote"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "INSUFFICIENT_CAPABILITY"
        )

    def test_ticket_decision_wrong_branch_is_insufficient_scope(self) -> None:
        ticket_id, _path = self._new_ticket()
        wrong = {
            "Authorization": "Bearer "
            + _key("factory:read", f"ticket:create:{OTHER_SECTOR}")
        }
        response = self.client.post(
            f"/v1/tickets/{ticket_id}/decision",
            headers=wrong,
            json={"action": "promote"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "INSUFFICIENT_SCOPE")

    def test_gate_reject_only_key_is_blocked_at_the_coarse_floor(self) -> None:
        # Documented consequence: the gate decision route's coarse capability is
        # ``gate:approve`` (mirroring the capsule precedent), so a reject-only
        # credential is refused before the service proves ``gate:reject``.
        gate_id, _path, _entry = self._new_gate()
        reject_only = {
            "Authorization": "Bearer "
            + _key("factory:read", f"gate:reject:{SECTOR}")
        }
        response = self.client.post(
            f"/v1/gates/{gate_id}/decision",
            headers=reject_only,
            json={"action": "reject", "reason": "no"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "INSUFFICIENT_CAPABILITY"
        )

    def test_gate_decision_wrong_branch_is_insufficient_scope(self) -> None:
        gate_id, _path, _entry = self._new_gate()
        wrong = {
            "Authorization": "Bearer "
            + _key(
                "factory:read",
                f"gate:approve:{OTHER_SECTOR}",
                f"gate:reject:{OTHER_SECTOR}",
            )
        }
        reviewed = self._echo(self._review_gate(gate_id))
        response = self.client.post(
            f"/v1/gates/{gate_id}/decision",
            headers=wrong,
            json={"action": "approve", "reviewed": reviewed},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "INSUFFICIENT_SCOPE")

    def test_unknown_gate_is_not_found(self) -> None:
        response = self.client.get(
            "/v1/gates/G-DOES-NOT-EXIST/review", headers=self.read_headers
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")

    def test_unknown_ticket_is_not_found(self) -> None:
        response = self.client.get(
            "/v1/tickets/T-DOES-NOT-EXIST/review", headers=self.read_headers
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")


# --------------------------------------------------------------------------- #
# N concurrent gate processes: exactly one wins
# --------------------------------------------------------------------------- #


class GateDecisionsAreSerialised(V1TicketGateTestCase):
    def test_parallel_v1_decides_let_exactly_one_win(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        gate_id, _path, entry = self._new_gate()
        expected = review_hash.elements_for(review_hash.gate_review_fields(entry))
        expected_arg = json.dumps(expected, sort_keys=True)
        base = [
            "decide",
            "--gate-id",
            gate_id,
            "--decision",
            "approved",
            "--expected-elements",
            expected_arg,
            "--v1",
        ]

        def _decide(human):
            return human, _run_gate([*base, "--by", human])

        humans = [f"human_{i:02d}" for i in range(6)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(_decide, humans))

        winners = [(h, p) for h, p in results if p.returncode == 0]
        losers = [(h, p) for h, p in results if p.returncode != 0]
        self.assertEqual(len(winners), 1, "exactly one human may decide a gate")
        for _human, proc in losers:
            self.assertIn("already closed", proc.stderr)
            self.assertIn("ALREADY_DECIDED", proc.stderr)

        on_disk = json.loads(Path(self._gate_paths[-1]).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["state"], "closed")
        self.assertEqual(on_disk["decided_by"], winners[0][0])
        self.assertTrue(on_disk["evidence"]["attested"])


# --------------------------------------------------------------------------- #
# static guards: frozen inventories that may only change on purpose
# --------------------------------------------------------------------------- #

_PRODUCTION_ROOTS = (
    REPO_ROOT / "engine",
    REPO_ROOT / "ops",
    REPO_ROOT / "data",
)


def _production_files() -> list[Path]:
    files: list[Path] = []
    for root in _PRODUCTION_ROOTS:
        files.extend(sorted(root.rglob("*.py")))
    return files


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _called_names(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        names.append(
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        )
    return names


def _decide_argv_lists(path: Path) -> int:
    """How many ``gate_script ... decide`` subprocess argv lists a module builds.

    A decide argv carries the subcommand *and* its required ``--gate-id`` flag;
    requiring both keeps an unrelated ``"decide"`` string (``capsule_engine``
    exports a function of that name in ``__all__``) from looking like a caller.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        literals = {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
        if "decide" in literals and "--gate-id" in literals:
            count += 1
    return count


class TicketWriterCallersArePinned(unittest.TestCase):
    """The callers allowed to reach ``promote_ticket``/``demote_ticket``."""

    PINNED = sorted(
        [
            ("engine/orchestration/api.py", "demote_ticket"),
            ("engine/orchestration/api.py", "promote_ticket"),
            ("engine/orchestration/v1/service.py", "demote_ticket"),
            ("engine/orchestration/v1/service.py", "promote_ticket"),
            ("ops/tools/ticket_mgr.py", "demote_ticket"),
            ("ops/tools/ticket_mgr.py", "promote_ticket"),
        ]
    )

    def test_only_the_pinned_callers_reach_the_ticket_writers(self) -> None:
        found: list[tuple[str, str]] = []
        for path in _production_files():
            for name in _called_names(path):
                if name in ("promote_ticket", "demote_ticket"):
                    found.append((_rel(path), name))
        self.assertEqual(
            sorted(found),
            self.PINNED,
            "a new caller reached ticket_mgr.promote_ticket/demote_ticket — the "
            "ticket writers are a decision boundary; route it through the "
            "shared service or pin it here on purpose",
        )


class GateDecideCallersArePinned(unittest.TestCase):
    """The modules allowed to build a ``gate_script ... decide`` argv."""

    PINNED = {
        "engine/orchestration/api.py": 2,
        "engine/orchestration/v1/service.py": 1,
        "ops/tools/post_cycle_trial.py": 1,
    }

    def test_only_the_pinned_callers_invoke_gate_script_decide(self) -> None:
        found = {
            _rel(path): count
            for path in _production_files()
            if (count := _decide_argv_lists(path))
        }
        self.assertEqual(
            found,
            self.PINNED,
            "a new caller invoked gate_script decide — the gate decision is a "
            "human boundary; route it through the shared service or pin it here",
        )


class V1PackageReachesWritesOnlyThroughTheService(unittest.TestCase):
    """Inside ``orchestration/v1`` only ``service.py`` touches the writers."""

    def _v1_files(self) -> list[Path]:
        return sorted((REPO_ROOT / "engine" / "orchestration" / "v1").glob("*.py"))

    def test_only_service_touches_the_ticket_and_gate_writers(self) -> None:
        self.assertTrue(self._v1_files(), "no /v1 source found")
        ticket_offenders: set[str] = set()
        gate_offenders: set[str] = set()
        for path in self._v1_files():
            names = _called_names(path)
            if "promote_ticket" in names or "demote_ticket" in names:
                ticket_offenders.add(path.name)
            if _decide_argv_lists(path):
                gate_offenders.add(path.name)
        self.assertEqual(
            ticket_offenders,
            {"service.py"},
            "the /v1 package may reach the ticket writers only through service.py",
        )
        self.assertEqual(
            gate_offenders,
            {"service.py"},
            "the /v1 package may reach the gate script only through service.py",
        )


class NewV1RoutesArePinnedToCapabilities(unittest.TestCase):
    def test_the_four_new_routes_keep_their_capabilities(self) -> None:
        expected = {
            ("GET", "/v1/tickets/{ticket_id}/review"): "domain:read",
            ("POST", "/v1/tickets/{ticket_id}/decision"): "ticket:create",
            ("GET", "/v1/gates/{gate_id}/review"): "domain:read",
            ("POST", "/v1/gates/{gate_id}/decision"): "gate:approve",
        }
        for key, capability in expected.items():
            self.assertEqual(
                access_control.ROUTE_CAPABILITIES[key],
                capability,
                f"{key} changed capability — a read must never authorise a write",
            )


if __name__ == "__main__":
    unittest.main()
