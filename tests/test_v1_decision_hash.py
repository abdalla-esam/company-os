"""Slice 1 of the ``/v1`` mobile API: attested decisions (capsules only).

A human approval on ``POST /v1/decisions`` carries the SHA-256 of exactly what
the reviewer saw. The server recomputes its own binding *inside the capsule
lock* and fails closed on any mismatch — a byte that changed after review is
``STALE_CONTENT_HASH`` and nothing is written. This module proves that
end-to-end, through the real ASGI stack, and pins the static properties that
keep it from rotting:

- ``GET /v1/capsules/{id}/review`` is a **pure read** — no gate opened, no
  target bound, no mtime change — so the hashes it reports are trustworthy;
- approval attestation is **mandatory** on ``/v1`` (``ATTESTATION_REQUIRED``)
  and **optional** on the legacy ``/api`` routes (``attested=false``);
- the attested element set must equal the gate's bound set exactly
  (``HASH_SET_MISMATCH``); ``unviewed`` is recorded, never a relaxation;
- a post-review byte change yields ``STALE_CONTENT_HASH``;
- a rejection needs a reason, never a hash;
- ``idempotency_key`` replays a repeat and refuses a reused key
  (``IDEMPOTENCY_CONFLICT``);
- the spend kill switch stops approvals (``KILL_SWITCH_ON``) but not rejects.

Static guards (AST scans, frozen inventories that may only shrink):

- no scanned module but the kernel may write a ``state`` field directly
  (a ``["state"] =`` assignment — literal or computed — ``.update``/
  ``.setdefault``, or ``.state =``); every legitimate exception is pinned
  with a reason;
- the callers of ``transition(State.APPROVED/PUBLISHED)`` are pinned;
- the ``orchestration/v1`` package never imports or calls ``gates.decide`` /
  ``manifest.transition`` (it uses the service, never the raw choke points);
- no mutating ``/v1`` route maps to ``factory:read`` alone.

Everything runs in the bootstrap vault and removes what it creates.
"""

from __future__ import annotations  # noqa: E402  — the suite runs on 3.9

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import ast
import asyncio
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests._bootstrap import REPO_ROOT, SPOOL

from capsule_engine import gates as gate_engine
from capsule_engine.capabilities import mint_key
from capsule_engine.locking import CapsuleLock
from orchestration import access_control, api, approval
from starlette.requests import Request
from starlette.testclient import TestClient

#: A test-only signing secret (>= the 32-byte minimum mint_key enforces).
TEST_SECRET = "slice1-test-signing-secret-0123456789abcdef"
TEST_TTL_SECONDS = 600

BRANCH = "marketing"

#: A single human gate bound to ``copy.md`` — the element the reviewer attests.
HUMAN_GATE = (
    {
        "gate_type": "domain_lead_approval",
        "name": "Lead Gate",
        "targets": ("copy.md",),
        "authority": "capsule:approve",
    },
)

#: A capsule payload long enough that the gate hashing path is exercised.
TEXT = (
    "A plain sentence of sufficient length for the reviewer to accept this "
    "probe capsule without any banned vocabulary."
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _root_request() -> Request:
    """A root principal for fixture creation (creation is not under test here)."""
    principal = access_control.Principal.legacy_root("slice1-fixture")
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


def _key(*grants: str) -> str:
    return mint_key(
        "slice1-tester",
        list(grants),
        secret=TEST_SECRET.encode("utf-8"),
        ttl_seconds=TEST_TTL_SECONDS,
    )


def _make_capsule(capsule_id: str, template=HUMAN_GATE, text: str = TEXT) -> Path:
    """Create a capsule in the vault spool and install ``template`` as its plan."""
    asyncio.run(
        api.create_capsule_endpoint(
            api.CreateCapsuleRequest(
                text=text, capsule_id=capsule_id, sector=BRANCH
            ),
            _root_request(),
        )
    )
    path = SPOOL / capsule_id
    with CapsuleLock(path):
        manifest = api.load_manifest(path)
        manifest["gates"] = []
        specs = gate_engine.specs_from_template(template)
        gate_engine.ensure_plan(manifest, specs, by="tester", replace_empty=True)
        api.save_manifest(path, manifest)
    return path


def _open_active(path: Path) -> str:
    with CapsuleLock(path):
        manifest = api.load_manifest(path)
        active = gate_engine.active_record(gate_engine.load_records(manifest))
        assert active is not None, "fixture has no active gate"
        if not active.opened_at:
            gate_engine.open_gate(manifest, active.gate_id, path, by="tester")
        api.save_manifest(path, manifest)
        return active.gate_id


def _manifest(path: Path) -> dict:
    with CapsuleLock(path):
        return api.load_manifest(path)


# --------------------------------------------------------------------------- #
# base case: a client + a scoped key
# --------------------------------------------------------------------------- #


class V1DecisionTestCase(unittest.TestCase):
    """Shared credential environment, client, and capsule cleanup."""

    APPROVE_KEY = (
        "factory:read",
        "capsule:approve:" + BRANCH,
        "capsule:reject:" + BRANCH,
    )

    def setUp(self) -> None:
        self._env = mock.patch.dict(
            os.environ, {"AGENCY_CAPABILITY_SECRET": TEST_SECRET}
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.client = TestClient(api.app)
        self.headers = {"Authorization": f"Bearer {_key(*self.APPROVE_KEY)}"}
        self.read_headers = {"Authorization": f"Bearer {_key('domain:read:' + BRANCH)}"}
        self._created: list[str] = []
        self._counter = 0
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for capsule_id in self._created:
            shutil.rmtree(SPOOL / capsule_id, ignore_errors=True)

    def _new_capsule(self, template=HUMAN_GATE) -> tuple[str, Path]:
        self._counter += 1
        capsule_id = f"slice1_{self._counter}_{os.getpid()}"
        path = _make_capsule(capsule_id, template)
        self._created.append(capsule_id)
        return capsule_id, path

    def _review(self, capsule_id: str) -> dict:
        response = self.client.get(
            f"/v1/capsules/{capsule_id}/review", headers=self.read_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _decide(self, capsule_id: str, **body) -> object:
        payload = {"capsule_id": capsule_id, **body}
        return self.client.post("/v1/decisions", headers=self.headers, json=payload)

    @staticmethod
    def _echo(review: dict) -> dict:
        """The attestation a client would post back from a review body."""
        reviewed = {
            "algorithm": review["reviewed"]["algorithm"],
            "elements": dict(review["reviewed"]["elements"]),
            "unviewed": list(review["reviewed"].get("unviewed") or []),
            "primary_sha256": review["reviewed"].get("primary_sha256"),
        }
        return reviewed


# --------------------------------------------------------------------------- #
# review is a pure read
# --------------------------------------------------------------------------- #


class ReviewIsPureRead(V1DecisionTestCase):
    """The review endpoint must not mutate the capsule it reports on."""

    def test_review_does_not_touch_the_manifest(self) -> None:
        capsule_id, path = self._new_capsule()
        manifest_file = path / "manifest.json"
        before_bytes = manifest_file.read_bytes()
        before_mtime = manifest_file.stat().st_mtime_ns

        review = self._review(capsule_id)

        self.assertEqual(manifest_file.read_bytes(), before_bytes, "manifest changed")
        self.assertEqual(
            manifest_file.stat().st_mtime_ns, before_mtime, "manifest mtime changed"
        )
        # The gate is reported but was NOT opened by the read.
        self.assertFalse(review["active_gate"]["opened"])
        manifest = _manifest(path)
        record = manifest["gates"][0]
        self.assertIsNone(record["opened_at"])
        self.assertEqual(record["artifacts"], {})

    def test_review_reports_the_bound_elements_after_open(self) -> None:
        capsule_id, path = self._new_capsule()
        _open_active(path)

        review = self._review(capsule_id)

        self.assertEqual(review["binding"], "elements")
        record = _manifest(path)["gates"][0]
        self.assertEqual(
            set(review["reviewed"]["elements"]), set(record["artifacts"])
        )
        for relative, digest in record["artifacts"].items():
            self.assertEqual(review["reviewed"]["elements"][relative], digest)


# --------------------------------------------------------------------------- #
# attestation is mandatory on /v1, optional on /api
# --------------------------------------------------------------------------- #


class AttestationIsMandatory(V1DecisionTestCase):
    def test_approve_without_reviewed_is_refused(self) -> None:
        capsule_id, path = self._new_capsule()

        response = self._decide(capsule_id, action="approve")

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "ATTESTATION_REQUIRED")
        self.assertEqual(_manifest(path)["state"], "pending_approval")
        self.assertNotIn("decisions", _manifest(path))

    def test_legacy_gate_decision_route_needs_no_attestation(self) -> None:
        capsule_id, path = self._new_capsule()
        gate_id = _open_active(path)

        response = self.client.post(
            f"/api/capsules/{capsule_id}/gates/{gate_id}/decision",
            headers=self.headers,
            json={"decision": "approve"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        record = _manifest(path)["decisions"][-1]
        self.assertFalse(record["attested"])
        self.assertIsNone(record["reviewed_sha256"])

    def test_legacy_capsule_approve_route_needs_no_attestation(self) -> None:
        capsule_id, path = self._new_capsule()
        _open_active(path)

        response = self.client.post(
            f"/api/capsules/{capsule_id}/approve", headers=self.headers
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(_manifest(path)["decisions"][-1]["attested"])

    def test_legacy_capsule_reject_route_needs_no_attestation(self) -> None:
        capsule_id, path = self._new_capsule()

        response = self.client.post(
            f"/api/capsules/{capsule_id}/reject",
            headers=self.headers,
            json={"reason": "not on brand"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(_manifest(path)["decisions"][-1]["attested"])

    def test_no_legacy_route_demands_attestation(self) -> None:
        # Static: the optional/required split is a property of the routes, not
        # something a later edit can quietly invert.
        source = (REPO_ROOT / "engine" / "orchestration" / "api.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            "require_attestation",
            source,
            "legacy /api routes must call the service without require_attestation",
        )


# --------------------------------------------------------------------------- #
# the attested set must equal the bound set exactly
# --------------------------------------------------------------------------- #


class ElementSetMustMatchExactly(V1DecisionTestCase):
    def test_missing_or_extra_elements_are_refused(self) -> None:
        capsule_id, path = self._new_capsule()
        reviewed = self._echo(self._review(capsule_id))

        extra = dict(reviewed)
        extra["elements"] = {**reviewed["elements"], "notes.txt": "0" * 64}
        response = self._decide(capsule_id, action="approve", reviewed=extra)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "HASH_SET_MISMATCH")
        self.assertEqual(_manifest(path)["state"], "pending_approval")

    def test_unviewed_does_not_relax_the_set_equality(self) -> None:
        capsule_id, path = self._new_capsule()
        reviewed = self._echo(self._review(capsule_id))

        # Claiming the real element was "unviewed" does not excuse a wrong set.
        relaxed = {
            "algorithm": "sha256",
            "elements": {"other.md": "1" * 64},
            "unviewed": list(reviewed["elements"]),
        }
        response = self._decide(capsule_id, action="approve", reviewed=relaxed)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "HASH_SET_MISMATCH")

    def test_unviewed_is_recorded_on_a_successful_decision(self) -> None:
        capsule_id, path = self._new_capsule()
        reviewed = self._echo(self._review(capsule_id))
        reviewed["unviewed"] = ["hero.png"]  # recorded, but not a bound element

        response = self._decide(capsule_id, action="approve", reviewed=reviewed)

        self.assertEqual(response.status_code, 200, response.text)
        record = _manifest(path)["decisions"][-1]
        self.assertEqual(record["unviewed"], ["hero.png"])

    def test_wrong_hash_for_a_right_path_is_stale(self) -> None:
        capsule_id, path = self._new_capsule()
        reviewed = self._echo(self._review(capsule_id))
        wrong = dict(reviewed)
        wrong["elements"] = {p: "0" * 64 for p in reviewed["elements"]}

        response = self._decide(capsule_id, action="approve", reviewed=wrong)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "STALE_CONTENT_HASH")


# --------------------------------------------------------------------------- #
# a byte that changed after review fails closed
# --------------------------------------------------------------------------- #


class StaleContentFailsClosed(V1DecisionTestCase):
    def test_post_review_change_yields_stale_and_writes_nothing(self) -> None:
        capsule_id, path = self._new_capsule()
        reviewed = self._echo(self._review(capsule_id))

        # The reviewer's screen is now out of date.
        (path / "copy.md").write_text(TEXT + " …edited after review", encoding="utf-8")

        response = self._decide(capsule_id, action="approve", reviewed=reviewed)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "STALE_CONTENT_HASH")
        manifest = _manifest(path)
        self.assertEqual(manifest["state"], "pending_approval")
        self.assertNotIn("decisions", manifest)


# --------------------------------------------------------------------------- #
# the kill switch stops approvals, never rejects
# --------------------------------------------------------------------------- #


class KillSwitchStopsApproval(V1DecisionTestCase):
    def test_approval_is_refused_when_the_kill_switch_is_on(self) -> None:
        capsule_id, path = self._new_capsule()
        reviewed = self._echo(self._review(capsule_id))

        with mock.patch.object(approval.spend_control, "kill_active", return_value=True):
            response = self._decide(capsule_id, action="approve", reviewed=reviewed)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "KILL_SWITCH_ON")
            self.assertEqual(_manifest(path)["state"], "pending_approval")

            rejected = self._decide(capsule_id, action="reject", reason="stop")
            self.assertEqual(rejected.status_code, 200, rejected.text)


# --------------------------------------------------------------------------- #
# one tap, one decision: idempotency
# --------------------------------------------------------------------------- #


class IdempotentReplay(V1DecisionTestCase):
    def test_repeat_with_same_key_replays_the_first_outcome(self) -> None:
        capsule_id, _ = self._new_capsule()
        reviewed = self._echo(self._review(capsule_id))

        first = self._decide(
            capsule_id, action="approve", reviewed=reviewed, idempotency_key="tap-1"
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["replayed"])

        second = self._decide(
            capsule_id, action="approve", reviewed=reviewed, idempotency_key="tap-1"
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(second.json()["decision_id"], first.json()["decision_id"])

    def test_same_key_different_payload_is_refused(self) -> None:
        capsule_id, _ = self._new_capsule()
        reviewed = self._echo(self._review(capsule_id))

        self._decide(
            capsule_id, action="approve", reviewed=reviewed, idempotency_key="tap-2"
        )
        changed = dict(reviewed)
        changed["elements"] = {p: "0" * 64 for p in reviewed["elements"]}

        conflict = self._decide(
            capsule_id, action="approve", reviewed=changed, idempotency_key="tap-2"
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")


# --------------------------------------------------------------------------- #
# reject: a reason, never a hash
# --------------------------------------------------------------------------- #


class RejectCarriesAReason(V1DecisionTestCase):
    def test_reject_with_reason_succeeds_and_is_unattested(self) -> None:
        capsule_id, path = self._new_capsule()

        response = self._decide(capsule_id, action="reject", reason="off brand")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["action"], "reject")
        self.assertFalse(body["attested"])
        self.assertEqual(body["state_after"], "needs_revision")
        self.assertFalse(_manifest(path)["decisions"][-1]["attested"])

    def test_reject_without_reason_is_refused(self) -> None:
        capsule_id, path = self._new_capsule()

        response = self._decide(capsule_id, action="reject", reason="  ")

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(_manifest(path)["state"], "pending_approval")


# --------------------------------------------------------------------------- #
# the /v1 error envelope
# --------------------------------------------------------------------------- #


class V1RefusalsUseTheEnvelope(V1DecisionTestCase):
    def test_unauthenticated_review_is_enveloped(self) -> None:
        capsule_id, _ = self._new_capsule()
        response = self.client.get(f"/v1/capsules/{capsule_id}/review")
        self.assertEqual(response.status_code, 401, response.text)
        body = response.json()
        self.assertEqual(body["error"]["code"], "UNAUTHENTICATED")
        self.assertIn("Unauthorized", body["detail"])

    def test_insufficient_capability_is_enveloped(self) -> None:
        capsule_id, _ = self._new_capsule()
        response = self.client.post(
            "/v1/decisions",
            headers=self.read_headers,
            json={"capsule_id": capsule_id, "action": "approve"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "INSUFFICIENT_CAPABILITY")

    def test_undeclared_v1_path_is_enveloped(self) -> None:
        response = self.client.get("/v1/nope", headers=self.read_headers)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "UNDECLARED_ROUTE")
        self.assertIn("No capability is declared", response.json()["detail"])

    def test_body_validation_error_is_enveloped(self) -> None:
        response = self.client.post("/v1/decisions", headers=self.headers, json={})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_unconfigured_server_stays_legacy_shaped(self) -> None:
        with mock.patch.object(api, "API_TOKEN", ""), mock.patch.dict(
            os.environ, {"AGENCY_CAPABILITY_SECRET": ""}
        ):
            response = self.client.get("/v1/capsules/anything/review")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("Auth not configured", response.json()["detail"])
        self.assertNotIn("error", response.json())

    def test_legacy_refusals_are_unchanged(self) -> None:
        response = self.client.get("/api/stats")
        self.assertEqual(response.status_code, 401, response.text)
        self.assertNotIn("error", response.json())


# --------------------------------------------------------------------------- #
# static guards
# --------------------------------------------------------------------------- #

#: The curated capsule-writer set, mirroring ``tests/test_revision_cap.py``'s
#: ``TestNoBypass``: the kernel, the workers, and the orchestration modules that
#: touch capsules. Ticket state machines (dispatcher) are a different machine.
_SCAN_ROOTS = (
    REPO_ROOT / "engine" / "capsule_engine",
    REPO_ROOT / "engine" / "workers",
)
_SCAN_FILES = (
    REPO_ROOT / "engine" / "orchestration" / "api.py",
    REPO_ROOT / "engine" / "orchestration" / "approval.py",
    REPO_ROOT / "engine" / "orchestration" / "capsule_pulse.py",
    REPO_ROOT / "engine" / "orchestration" / "directus_bridge.py",
)


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        files.extend(sorted(root.glob("*.py")))
    files.extend(_SCAN_FILES)
    return files


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _index(node: ast.AST):
    if isinstance(node, ast.Index):  # pragma: no cover - py<3.9
        return node.value
    return node


def _normalized_segment(source: str, node: ast.AST) -> str:
    """The node's source, whitespace-collapsed to one line (stable as a pin)."""
    segment = ast.get_source_segment(source, node) or ""
    return " ".join(segment.split())[:200]


def _state_writes(path: Path) -> list[tuple[str, str]]:
    """``(relative_file, source)`` for **every** direct write to a ``state`` field.

    A capsule's state may move only through
    :func:`capsule_engine.manifest.transition`; every other write is an
    approval-boundary bypass. This catches, in one pass, each way a dict/object
    field can be written directly:

    - ``x["state"] = <anything>`` (literal **or** computed right-hand side);
    - ``x.update({"state": ...})``;
    - ``x.setdefault("state", ...)``;
    - ``x.state = <anything>`` (attribute assignment).
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    rel = _relative(path)
    hits: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript):
                    index = _index(target.slice)
                    if isinstance(index, ast.Constant) and index.value == "state":
                        hits.append((rel, _normalized_segment(source, node)))
                elif isinstance(target, ast.Attribute) and target.attr == "state":
                    hits.append((rel, _normalized_segment(source, node)))
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name == "update":
                for arg in node.args:
                    if not isinstance(arg, ast.Dict):
                        continue
                    if any(
                        isinstance(key, ast.Constant) and key.value == "state"
                        for key in arg.keys
                    ):
                        hits.append((rel, _normalized_segment(source, node)))
                        break
            elif name == "setdefault":
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "state"
                ):
                    hits.append((rel, _normalized_segment(source, node)))
    return hits


def _approved_published_transitions(path: Path) -> list[tuple[str, str]]:
    """``(relative_file, STATE)`` for every ``transition(…, State.APPROVED|PUBLISHED)``."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    hits: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "transition":
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if (
                isinstance(arg, ast.Attribute)
                and isinstance(arg.value, ast.Name)
                and arg.value.id == "State"
                and arg.attr in ("APPROVED", "PUBLISHED")
            ):
                hits.append((_relative(path), arg.attr))
    return hits


class ApprovalStateWritesArePinned(unittest.TestCase):
    """Only the kernel may write a ``state`` field in the scanned set.

    A capsule's state moves only through ``manifest.transition()`` — the one
    chokepoint that enforces the transition table, the revision cap, and the
    hash-verified approval/publish edges. This scans every capsule-manifest
    writer (the kernel, the workers, and the orchestration modules that touch
    capsules) for **any** direct ``state`` write: a ``["state"] = …`` assignment
    (literal or computed), ``.update({"state": …})``, ``.setdefault("state", …)``
    or ``.state =``.

    ``manifest.py`` is the chokepoint and is skipped. The direct writes that
    remain in the scanned set are legitimate by construction and are pinned by
    name and reason below — the list may only shrink, and every pin must carry
    a reason. ``directus_bridge`` CASE 2 (which approved a capsule straight from
    an external card status with no human decision and no hash check) was
    removed, so its pin is struck.
    """

    #: (relative_file, source_segment) -> one-line reason it may stay.
    PINNED_REASONS: dict[tuple[str, str], str] = {
        (
            "engine/capsule_engine/worker.py",
            'manifest["state"] = State.FAILED.value',
        ): (
            "BaseWorker._record_failure escalates a crashed capsule to "
            "'failed' on purpose, outside the transition matrix (a crash must "
            "never be blocked by the table recording it); it never writes "
            "approved/published."
        ),
        (
            "engine/orchestration/api.py",
            'state["state"] = "settled"',
        ): (
            "settle-bridge write to a governance TICKET's state — a different "
            "entity and state machine from the capsule manifest (out of scope)."
        ),
    }

    def test_no_unpinned_module_writes_a_state_field(self) -> None:
        found: list[tuple[str, str]] = []
        for path in _scanned_files():
            if path.name == "manifest.py":
                continue  # the chokepoint itself
            found.extend(_state_writes(path))
        self.assertEqual(
            sorted(found),
            sorted(self.PINNED_REASONS),
            "a module wrote a state field directly — capsule states may only "
            "move through manifest.transition(); if this is a genuinely "
            "legitimate writer, pin it WITH a reason, otherwise route it "
            "through the kernel",
        )

    def test_every_pin_states_a_reason(self) -> None:
        for pinned, reason in self.PINNED_REASONS.items():
            self.assertTrue(
                reason.strip(), f"pin {pinned} carries no reason"
            )


class ApprovedPublishedTransitionsArePinned(unittest.TestCase):
    """The only callers allowed to move a capsule into approved/published."""

    PINNED: list[tuple[str, str]] = sorted(
        [
            ("engine/capsule_engine/sandbox.py", "APPROVED"),
            ("engine/orchestration/approval.py", "APPROVED"),
            ("engine/orchestration/approval.py", "APPROVED"),
            ("engine/workers/governor.py", "APPROVED"),
            ("engine/workers/publishing.py", "PUBLISHED"),
        ]
    )

    def test_transition_callers_are_exactly_the_pinned_set(self) -> None:
        found: list[tuple[str, str]] = []
        for path in _scanned_files():
            if path.name == "manifest.py":
                continue
            found.extend(_approved_published_transitions(path))
        self.assertEqual(
            sorted(found),
            self.PINNED,
            "a module gained (or lost) a transition() into approved/published — "
            "that is a change to the approval boundary; update the pin on purpose",
        )


class V1PackageNeverBypassesTheService(unittest.TestCase):
    """The ``/v1`` package decides through the service, never the raw kernels."""

    FORBIDDEN_NAMES = frozenset({"decide", "transition"})
    KERNEL_MODULES = frozenset({"capsule_engine.gates", "capsule_engine.manifest"})

    def _v1_files(self) -> list[Path]:
        return sorted((REPO_ROOT / "engine" / "orchestration" / "v1").glob("*.py"))

    def test_v1_never_imports_or_calls_the_choke_points(self) -> None:
        self.assertTrue(self._v1_files(), "no /v1 source found")
        offenders: list[str] = []
        for path in self._v1_files():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in self.KERNEL_MODULES:
                    for alias in node.names:
                        if alias.name in self.FORBIDDEN_NAMES:
                            offenders.append(
                                f"{_relative(path)}:{node.lineno}: from "
                                f"{node.module} import {alias.name}"
                            )
                if isinstance(node, ast.Call):
                    func = node.func
                    name = (
                        func.attr
                        if isinstance(func, ast.Attribute)
                        else getattr(func, "id", "")
                    )
                    if name in self.FORBIDDEN_NAMES:
                        offenders.append(f"{_relative(path)}:{node.lineno}: {name}(…)")
        self.assertEqual(
            offenders,
            [],
            "the /v1 package must go through orchestration.approval, not the "
            f"raw gates.decide()/manifest.transition(): {offenders}",
        )

    def test_v1_router_requires_attestation_on_approve(self) -> None:
        source = (
            REPO_ROOT / "engine" / "orchestration" / "v1" / "router.py"
        ).read_text(encoding="utf-8")
        self.assertIn("require_attestation=True", source)
        self.assertIn("attestation=reviewed", source)


class MutatingV1RoutesNeverMapToReadAlone(unittest.TestCase):
    """A mutating route must require a decision, never a read capability."""

    MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def test_no_mutating_v1_route_is_factory_read(self) -> None:
        offenders = sorted(
            (method, template, capability)
            for (method, template), capability in access_control.ROUTE_CAPABILITIES.items()
            if template.startswith("/v1/")
            and method in self.MUTATING
            and capability == "factory:read"
        )
        self.assertEqual(
            offenders,
            [],
            "a mutating /v1 route maps to factory:read alone — a read grant "
            f"must never authorise a state change: {offenders}",
        )

    def test_the_two_v1_routes_are_pinned_to_their_capabilities(self) -> None:
        self.assertEqual(
            access_control.ROUTE_CAPABILITIES[
                ("GET", "/v1/capsules/{capsule_id}/review")
            ],
            "domain:read",
        )
        self.assertEqual(
            access_control.ROUTE_CAPABILITIES[("POST", "/v1/decisions")],
            "capsule:approve",
        )


if __name__ == "__main__":
    unittest.main()
