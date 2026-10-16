"""Slice R: sector-scoped reads on ``/v1`` — a user sees only their domains.

``/v1`` reads used to share the engine-wide ``factory:read`` grant: a key that
could read anything read *everything*. This module proves the replacement:

- the route map's floor for every ``/v1`` GET is the **scoped** ``domain:read``,
  so a ``factory:read``-only key is refused outright (``INSUFFICIENT_CAPABILITY``);
- the handler then proves the object's *real* domain against the sectors the
  credential names (``domain:read:<sector>`` / ``domain:read:*`` / root), and a
  domain the caller may not see answers **404 with the missing-object body** —
  never 403, never a hint that the object exists;
- visibility is derived from the credential alone (no ``config/users.json``
  read, no derivation from write grants);
- the decision routes keep enforcing their scoped write capability against the
  object's real domain, so a marketing-only approver cannot decide a
  development object;
- ``mint_user_token`` issues ``domain:read:<sector>`` per authorised sector and
  no longer the engine-wide ``factory:read`` (that is what stops those tokens
  from reading ``/api/stats``, ``/api/radar/tasks`` … across domains);
- ``read_index`` filters ``stats``/``query_feedback``/``query_gated_capsules``
  by a set of sectors, and the file-scan fallback applies the identical filter.

The primary guard is behavioural and forward-looking: it walks the *live*
``app.routes``, and every ``/v1`` GET route must appear in :data:`SCOPE_PROBES`
or the test fails — a new read route cannot be added without deciding its sector
scope. A secondary AST guard proves each ``/v1`` GET handler calls the shared
``orchestration.v1.scope`` helper.

Everything runs in the bootstrap vault and removes what it creates.
"""

from __future__ import annotations  # noqa: E402  — the suite runs on 3.9

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import ast
import asyncio
import json
import os
import re
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests._bootstrap import DOMAINS, GATE_QUEUE, REPO_ROOT, SPOOL

from capsule_engine import gates as gate_engine
from capsule_engine import memory as memory_engine
from capsule_engine.capabilities import mint_key, verify_key
from capsule_engine.locking import CapsuleLock
from orchestration import access_control, api, read_index
from orchestration.v1 import scope as v1_scope
from shared_kernel import users
from starlette.requests import Request
from starlette.testclient import TestClient

#: A test-only signing secret (>= the 32-byte minimum mint_key enforces).
TEST_SECRET = "slicer-test-signing-secret-0123456789abcdef"
TEST_TTL_SECONDS = 600

MARKETING = "marketing"
DEVELOPMENT = "development"

#: An id no fixture uses; both routers clean it the same way a real id is cleaned.
MISSING_ID = "T-DOES-NOT-EXIST-read-scope"

_REVIEW_GET_TEMPLATES = (
    "/v1/capsules/{capsule_id}/review",
    "/v1/tickets/{ticket_id}/review",
    "/v1/gates/{gate_id}/review",
)

#: **The forward-looking guard.** Every ``/v1`` GET route discovered from the
#: live ``app.routes`` must be registered here with the fixture that lives in the
#: *development* sector — whether the route names an object in its path (then
#: that object must be invisible) or returns a collection/aggregate (then the
#: development fixture must not appear anywhere in it). A new read route that is
#: not probed fails the test.
SCOPE_PROBES = {
    ("GET", "/v1/capsules/{capsule_id}/review"): "development_capsule",
    ("GET", "/v1/tickets/{ticket_id}/review"): "development_ticket",
    ("GET", "/v1/gates/{gate_id}/review"): "development_gate",
    # Slice M1a: the aggregate reads. A collection route has no object id in its
    # path, so its probe is the development fixture that must never show up.
    ("GET", "/v1/me"): "development_capsule",
    ("GET", "/v1/overview"): "development_capsule",
    ("GET", "/v1/attention"): "development_capsule",
    ("GET", "/v1/capsules"): "development_capsule",
    ("GET", "/v1/capsules/{capsule_id}"): "development_capsule",
    ("GET", "/v1/capsules/{capsule_id}/trace"): "development_capsule",
    ("GET", "/v1/tickets"): "development_ticket",
    ("GET", "/v1/tickets/{ticket_id}"): "development_ticket",
}

#: The routes that name an object in their path — those are read as that object.
OBJECT_PROBES = {
    key: value for key, value in SCOPE_PROBES.items() if "{" in key[1]
}

#: The routes that return a collection or an aggregate (no object in the path).
COLLECTION_PROBES = {
    key: value for key, value in SCOPE_PROBES.items() if "{" not in key[1]
}

#: The scoped helper names a ``/v1`` GET handler may call to enforce visibility.
_SCOPE_HELPERS = frozenset({"visible_sectors", "sector_is_visible", "require_sector"})

_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")

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


def _key(subject: str, *grants: str) -> str:
    return mint_key(
        subject,
        list(grants),
        secret=TEST_SECRET.encode("utf-8"),
        ttl_seconds=TEST_TTL_SECONDS,
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _url(template: str, object_id: str) -> str:
    return _PLACEHOLDER.sub(lambda _m: object_id, template)


def _root_request() -> Request:
    """A root principal for fixture creation (creation is not under test here)."""
    principal = access_control.Principal.legacy_root("slicer-fixture")
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


def _make_capsule(capsule_id: str, sector: str) -> Path:
    """Create a capsule in ``sector`` and install a one-gate plan (so it is gated)."""
    asyncio.run(
        api.create_capsule_endpoint(
            api.CreateCapsuleRequest(
                text=_CAPSULE_TEXT, capsule_id=capsule_id, sector=sector
            ),
            _root_request(),
        )
    )
    path = SPOOL / capsule_id
    with CapsuleLock(path):
        manifest = api.load_manifest(path)
        manifest["gates"] = []
        specs = gate_engine.specs_from_template(_HUMAN_GATE)
        gate_engine.ensure_plan(manifest, specs, by="tester", replace_empty=True)
        api.save_manifest(path, manifest)
    return path


# --------------------------------------------------------------------------- #
# base cases
# --------------------------------------------------------------------------- #


class SecretOnlyTestCase(unittest.TestCase):
    """A hermetic signing secret, and nothing else."""

    def setUp(self) -> None:
        self._env = mock.patch.dict(
            os.environ, {"AGENCY_CAPABILITY_SECRET": TEST_SECRET}
        )
        self._env.start()
        self.addCleanup(self._env.stop)


class ReadScopingTestCase(SecretOnlyTestCase):
    """A live client, one capsule/ticket/gate in each of two sectors, and keys."""

    def setUp(self) -> None:
        super().setUp()

        from ops.tools import ticket_mgr

        self.ticket_mgr = ticket_mgr
        original_root = ticket_mgr.REPO_ROOT
        ticket_mgr.REPO_ROOT = DOMAINS.parent.resolve()
        self.addCleanup(setattr, ticket_mgr, "REPO_ROOT", original_root)

        for sector in (MARKETING, DEVELOPMENT):
            (DOMAINS / sector).mkdir(parents=True, exist_ok=True)

        self.client = TestClient(api.app)

        # Every kind of credential the task names.
        self.marketing_headers = _bearer(_key("marketing-reader", f"domain:read:{MARKETING}"))
        self.development_headers = _bearer(_key("development-reader", f"domain:read:{DEVELOPMENT}"))
        self.two_domain_headers = _bearer(
            _key("both-reader", f"domain:read:{MARKETING}", f"domain:read:{DEVELOPMENT}")
        )
        self.wildcard_headers = _bearer(_key("wild-reader", "domain:read:*"))
        self.root_headers = _bearer(_key("root-reader", "*"))
        self.factory_read_headers = _bearer(_key("legacy-reader", "factory:read"))

        # Scoped *write* keys (used to prove decisions stay domain-scoped).
        self.capsule_approver = _bearer(_key("cap-approver", f"capsule:approve:{MARKETING}"))
        self.ticket_writer = _bearer(_key("ticket-writer", f"ticket:create:{MARKETING}"))
        self.gate_approver = _bearer(_key("gate-approver", f"gate:approve:{MARKETING}"))

        self._created_capsules: list = []
        self._created_tickets: list = []
        self._gate_paths: list = []
        self._gate_seq = 0
        self.addCleanup(self._cleanup)

        self.marketing_capsule = self._unique("mkt")
        self.development_capsule = self._unique("dev")
        _make_capsule(self.marketing_capsule, MARKETING)
        _make_capsule(self.development_capsule, DEVELOPMENT)
        self._created_capsules += [self.marketing_capsule, self.development_capsule]

        self.marketing_ticket = self._new_ticket(MARKETING)
        self.development_ticket = self._new_ticket(DEVELOPMENT)

        self.marketing_gate = self._new_gate(MARKETING)
        self.development_gate = self._new_gate(DEVELOPMENT)

    # ---- fixture builders ------------------------------------------------- #

    def _unique(self, prefix: str) -> str:
        return f"readscope_{prefix}_{os.getpid()}_{len(self._created_capsules)}"

    def _new_ticket(self, sector: str) -> str:
        response = self.ticket_mgr.create_ticket(
            sector=sector,
            title="Read scope",
            content="A request body the reviewer would attest to.",
            stage="scratch",
        )
        ticket_id = response["ticket_id"]
        self._created_tickets.append((sector, ticket_id))
        return ticket_id

    def _new_gate(self, sector: str) -> str:
        self._gate_seq += 1
        gate_id = f"G-SR-{os.getpid()}-{len(self._created_capsules)}-{self._gate_seq}"
        entry = {
            "gate_id": gate_id,
            "source": {
                "sector": sector,
                "client_id": "slicer-client",
                "request": "Publish this post.",
            },
            "risk_tier": "external",
            "reason": "A human must review this.",
            "opened_at": "2020-01-01T00:00:00+00:00",
            "state": "open",
            "decision": None,
            "decided_by": None,
            "decided_at": None,
            "decision_reason": None,
        }
        path = GATE_QUEUE / f"gate_{gate_id}.json"
        path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        self._gate_paths.append(path)
        return gate_id

    def _cleanup(self) -> None:
        for capsule_id in self._created_capsules:
            shutil.rmtree(SPOOL / capsule_id, ignore_errors=True)
        for gate_path in self._gate_paths:
            Path(gate_path).unlink(missing_ok=True)
        for sector, ticket_id in self._created_tickets:
            tickets_root = DOMAINS / sector / "tickets"
            if tickets_root.is_dir():
                for path in tickets_root.rglob(f"ticket_{ticket_id}*.json"):
                    path.unlink(missing_ok=True)

    # ---- helpers ---------------------------------------------------------- #

    def _live_v1_get_routes(self) -> set:
        found = set()
        for route in api.app.routes:
            path = getattr(route, "path", "") or ""
            methods = getattr(route, "methods", None) or set()
            if path.startswith("/v1/") and "GET" in methods:
                found.add(("GET", path))
        return found


# --------------------------------------------------------------------------- #
# the primary guard: every /v1 read is sector-scoped
# --------------------------------------------------------------------------- #


class EveryV1ReadRouteIsSectorScoped(ReadScopingTestCase):
    def test_every_live_v1_get_route_is_probed(self) -> None:
        self.assertEqual(
            self._live_v1_get_routes(),
            set(SCOPE_PROBES),
            "a /v1 GET route exists with no entry in SCOPE_PROBES — decide its "
            "sector scope and add a development-sector probe before shipping it",
        )

    def test_marketing_token_gets_the_missing_body_for_development_objects(self) -> None:
        remembered = set()
        for (_method, template), attr in OBJECT_PROBES.items():
            with self.subTest(template=template):
                object_id = getattr(self, attr)
                invisible = self.client.get(
                    _url(template, object_id), headers=self.marketing_headers
                )
                missing = self.client.get(
                    _url(template, MISSING_ID), headers=self.marketing_headers
                )

                self.assertEqual(invisible.status_code, 404, invisible.text)
                self.assertEqual(missing.status_code, 404, missing.text)
                self.assertEqual(
                    invisible.json()["error"]["code"],
                    missing.json()["error"]["code"],
                )
                # The two bodies differ only by the id — the invisible object is
                # byte-for-byte what a truly-missing one looks like.
                self.assertEqual(
                    invisible.json()["detail"].replace(object_id, "<ID>"),
                    missing.json()["detail"].replace(MISSING_ID, "<ID>"),
                    "an invisible object must be indistinguishable from a missing one",
                )
                # And it is NOT a 403: existence is never confirmed.
                self.assertNotEqual(invisible.status_code, 403)
                remembered.add(object_id)
        # Every object route probed a real development fixture; several routes
        # name the same object, so the distinct *fixtures* — not the routes —
        # are what must all have been covered by an answered probe.
        self.assertEqual(len(remembered), len(set(OBJECT_PROBES.values())))

    def test_factory_read_only_token_is_refused_on_every_v1_read(self) -> None:
        for (_method, template), attr in SCOPE_PROBES.items():
            with self.subTest(template=template):
                response = self.client.get(
                    _url(template, getattr(self, attr)), headers=self.factory_read_headers
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    response.json()["error"]["code"], "INSUFFICIENT_CAPABILITY"
                )

    def test_collection_routes_never_expose_another_sector(self) -> None:
        """A collection/aggregate route must not leak the other sector's rows.

        The marketing token may read its own sector, so every one of these
        answers ``200`` — and none of them may mention the development capsule
        or ticket (by id, or by naming that domain as a row's own sector). The
        positive control at the end proves the fixtures really are there, so a
        passing assertion is not just an empty vault.
        """
        development_ids = (self.development_capsule, self.development_ticket)
        for (_method, template), attr in COLLECTION_PROBES.items():
            with self.subTest(template=template):
                response = self.client.get(
                    _url(template, getattr(self, attr)), headers=self.marketing_headers
                )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.text
                for object_id in development_ids:
                    self.assertNotIn(
                        object_id,
                        body,
                        f"{template} exposed the development object {object_id}",
                    )
                payload = response.json()
                self.assertNotIn(DEVELOPMENT, _own_domains(payload), payload)

        # Positive control: root sees both sectors, so the fixtures exist.
        root_overview = self.client.get("/v1/overview", headers=self.root_headers)
        self.assertEqual(root_overview.status_code, 200, root_overview.text)
        root_domains = {
            tile["domain"] for tile in root_overview.json()["domains"]
        }
        self.assertIn(DEVELOPMENT, root_domains)
        self.assertIn(MARKETING, root_domains)

    def test_the_right_sector_and_every_wide_grant_can_read(self) -> None:
        capsule_t, ticket_t, gate_t = _REVIEW_GET_TEMPLATES

        def readable(template: str, object_id: str, headers: dict) -> None:
            response = self.client.get(_url(template, object_id), headers=headers)
            self.assertEqual(response.status_code, 200, response.text)

        # The single-sector grant reads its own sector...
        readable(capsule_t, self.marketing_capsule, self.marketing_headers)
        readable(ticket_t, self.marketing_ticket, self.marketing_headers)
        readable(gate_t, self.marketing_gate, self.marketing_headers)
        # ...and not the other (covered above), while the wildcard, the
        # two-domain grant and root read both.
        for headers in (self.two_domain_headers, self.wildcard_headers, self.root_headers):
            readable(capsule_t, self.development_capsule, headers)
            readable(ticket_t, self.development_ticket, headers)
            readable(gate_t, self.development_gate, headers)

    def test_root_and_legacy_root_see_every_sector(self) -> None:
        # The legacy shared token resolves to the same root principal.
        all_sectors, sectors = v1_scope.visible_sectors(
            access_control.Principal.legacy_root("legacy-tester")
        )
        self.assertTrue(all_sectors)
        self.assertEqual(sectors, frozenset())

        # A key with only a write grant reads no sector: the rule is explicit.
        write_only = access_control.Principal.from_key(
            verify_key(_key("write-only", f"capsule:approve:{MARKETING}"),
                       secret=TEST_SECRET.encode("utf-8"))
        )
        all_sectors, sectors = v1_scope.visible_sectors(write_only)
        self.assertFalse(all_sectors)
        self.assertEqual(sectors, frozenset())


def _own_domains(payload: object) -> set:
    """Every sector a response names as a row's own (never a filter name)."""
    found = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("filters", "source_detail", "sectors", "capabilities"):
                continue
            if key in ("domain", "sector") and isinstance(value, str):
                found.add(value)
            else:
                found |= _own_domains(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _own_domains(item)
    return found


class EveryV1GetHandlerUsesTheScopeHelper(unittest.TestCase):
    """A secondary, static guard: no read handler may forget the helper."""

    V1_DIR = REPO_ROOT / "engine" / "orchestration" / "v1"

    @staticmethod
    def _is_router_get(decorator: ast.AST) -> bool:
        node = decorator
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Name)
            and func.value.id == "router"
        )

    @staticmethod
    def _called_names(node: ast.AST) -> set:
        names = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        return names

    def test_every_v1_get_handler_calls_a_scope_helper(self) -> None:
        offenders = []
        seen = 0
        for path in sorted(self.V1_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not any(self._is_router_get(d) for d in node.decorator_list):
                    continue
                seen += 1
                if not (self._called_names(node) & _SCOPE_HELPERS):
                    offenders.append(f"{path.name}:{node.name}")
        self.assertEqual(
            offenders,
            [],
            f"a /v1 GET handler does not call a scope helper: {offenders}",
        )
        self.assertEqual(
            seen,
            len(SCOPE_PROBES),
            "the AST guard and the live-route probe table disagree on how many "
            "/v1 GET handlers exist",
        )

    def test_the_scope_module_exposes_the_documented_helpers(self) -> None:
        self.assertTrue(_SCOPE_HELPERS.issubset(set(v1_scope.__all__)))


class V1ReadRouteFloorIsScopedDomainRead(unittest.TestCase):
    def test_every_v1_get_route_maps_to_domain_read(self) -> None:
        for template in _REVIEW_GET_TEMPLATES:
            self.assertEqual(
                access_control.ROUTE_CAPABILITIES[("GET", template)],
                "domain:read",
                f"{template} must floor at the scoped domain:read",
            )

    def test_every_live_v1_get_route_maps_to_domain_read(self) -> None:
        # Every /v1 GET — not just the review templates — floors at the scoped
        # capability: an engine-wide read grant must never open the mobile
        # namespace, and the aggregate reads must not become an exception.
        for (_method, template) in SCOPE_PROBES:
            with self.subTest(template=template):
                self.assertEqual(
                    access_control.ROUTE_CAPABILITIES[("GET", template)],
                    "domain:read",
                    f"{template} must floor at the scoped domain:read",
                )

    def test_no_mutating_v1_route_maps_to_a_read_capability(self) -> None:
        mutating = {"POST", "PUT", "PATCH", "DELETE"}
        offenders = [
            (method, template, capability)
            for (method, template), capability in access_control.ROUTE_CAPABILITIES.items()
            if template.startswith("/v1/")
            and method in mutating
            and capability in ("factory:read", "domain:read")
        ]
        self.assertEqual(offenders, [], f"a write floored at a read: {offenders}")

    def test_route_coverage_known_gaps_is_empty(self) -> None:
        from tests import test_route_capability_coverage as coverage

        self.assertEqual(coverage.KNOWN_GAPS, {})


# --------------------------------------------------------------------------- #
# decisions stay scoped to the object's real domain (add tests, don't rename)
# --------------------------------------------------------------------------- #


class DecisionsStayInTheirDomain(ReadScopingTestCase):
    def test_capsule_approve_of_a_development_capsule_is_refused(self) -> None:
        response = self.client.post(
            "/v1/decisions",
            headers=self.capsule_approver,
            json={"capsule_id": self.development_capsule, "action": "approve"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "INSUFFICIENT_SCOPE")
        # Nothing was written: the capsule is untouched.
        self.assertEqual(
            api.load_manifest(SPOOL / self.development_capsule)["state"],
            "pending_approval",
        )

    def test_capsule_approve_of_a_marketing_capsule_passes_the_scope_gate(self) -> None:
        # No attestation supplied: the refusal is ATTESTATION_REQUIRED, which
        # proves the *scope* gate was passed (not INSUFFICIENT_SCOPE).
        response = self.client.post(
            "/v1/decisions",
            headers=self.capsule_approver,
            json={"capsule_id": self.marketing_capsule, "action": "approve"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "ATTESTATION_REQUIRED")

    def test_ticket_decision_on_a_development_ticket_is_refused(self) -> None:
        response = self.client.post(
            f"/v1/tickets/{self.development_ticket}/decision",
            headers=self.ticket_writer,
            json={"action": "promote"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "INSUFFICIENT_SCOPE")

    def test_ticket_decision_on_a_marketing_ticket_passes_the_scope_gate(self) -> None:
        response = self.client.post(
            f"/v1/tickets/{self.marketing_ticket}/decision",
            headers=self.ticket_writer,
            json={"action": "promote"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "ATTESTATION_REQUIRED")

    def test_gate_decision_on_a_development_gate_is_refused(self) -> None:
        response = self.client.post(
            f"/v1/gates/{self.development_gate}/decision",
            headers=self.gate_approver,
            json={"action": "approve"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "INSUFFICIENT_SCOPE")

    def test_gate_decision_on_a_marketing_gate_passes_the_scope_gate(self) -> None:
        response = self.client.post(
            f"/v1/gates/{self.marketing_gate}/decision",
            headers=self.gate_approver,
            json={"action": "approve"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "ATTESTATION_REQUIRED")


# --------------------------------------------------------------------------- #
# minting: per-sector reads, never engine-wide
# --------------------------------------------------------------------------- #


class UserTokensCarrySectorReadsOnly(SecretOnlyTestCase):
    def _caps(self, user: dict) -> set:
        token = users.mint_user_token(user, ttl_seconds=TEST_TTL_SECONDS)
        return set(verify_key(token, secret=TEST_SECRET.encode("utf-8")).capabilities)

    def test_non_admin_gets_domain_read_per_sector_and_no_factory_read(self) -> None:
        caps = self._caps(
            {
                "username": "operator1",
                "role": "operator",
                "authorized_sectors": [MARKETING, DEVELOPMENT],
                "capabilities": ["ticket:create"],
            }
        )
        self.assertIn(f"domain:read:{MARKETING}", caps)
        self.assertIn(f"domain:read:{DEVELOPMENT}", caps)
        self.assertIn(f"ticket:create:{MARKETING}", caps)
        self.assertNotIn("factory:read", caps, "engine-wide reads must be gone")

    def test_explicit_factory_read_is_dropped_for_a_non_admin(self) -> None:
        caps = self._caps(
            {
                "username": "operator2",
                "role": "operator",
                "authorized_sectors": [MARKETING],
                "capabilities": ["factory:read", "ticket:create"],
            }
        )
        self.assertNotIn("factory:read", caps)
        self.assertIn(f"domain:read:{MARKETING}", caps)

    def test_admin_gets_the_root_key(self) -> None:
        token = users.mint_user_token(
            {
                "username": "admin1",
                "role": "admin",
                "authorized_sectors": ["*"],
                "capabilities": ["*"],
            },
            ttl_seconds=TEST_TTL_SECONDS,
        )
        self.assertTrue(verify_key(token, secret=TEST_SECRET.encode("utf-8")).is_root)

    def test_adding_domain_read_does_not_invalidate_a_previous_key(self) -> None:
        # A key minted before the vocabulary grew — it must still verify: the
        # change is purely additive (nothing was removed or renamed).
        token = _key(
            "pre-slice-r",
            "factory:read",
            f"capsule:approve:{MARKETING}",
            f"ticket:create:{MARKETING}",
        )
        verified = verify_key(token, secret=TEST_SECRET.encode("utf-8"))
        self.assertIn(f"capsule:approve:{MARKETING}", verified.capabilities)
        self.assertIn("factory:read", verified.capabilities)


# --------------------------------------------------------------------------- #
# read_index: the domains filter, in SQL and in the file-scan fallback
# --------------------------------------------------------------------------- #


class ReadIndexFiltersByDomains(ReadScopingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tag = f"readscope-{os.getpid()}"
        memory_engine.append_feedback(
            capsule_id=f"{self._tag}-mkt",
            reason="off brand marketing note",
            decision="rejected",
            domain=MARKETING,
        )
        memory_engine.append_feedback(
            capsule_id=f"{self._tag}-dev",
            reason="off brand development note",
            decision="rejected",
            domain=DEVELOPMENT,
        )
        read_index.rebuild()
        self.addCleanup(read_index.rebuild)

    def test_stats_sql_equals_the_file_fallback(self) -> None:
        for domains in ([MARKETING], [DEVELOPMENT], [MARKETING, DEVELOPMENT]):
            with self.subTest(domains=domains):
                self.assertEqual(
                    read_index.stats(domains=domains),
                    read_index.stats_from_files(domains=domains),
                )

    def test_stats_subsets_partition_the_two_domain_view(self) -> None:
        both = read_index.stats(domains=[MARKETING, DEVELOPMENT])
        mkt = read_index.stats(domains=[MARKETING])
        dev = read_index.stats(domains=[DEVELOPMENT])

        for group in ("capsules", "tickets", "feedback"):
            with self.subTest(group=group):
                self.assertEqual(
                    mkt[group]["total"] + dev[group]["total"], both[group]["total"]
                )
        self.assertEqual(
            mkt["capsules"]["gated"] + dev["capsules"]["gated"],
            both["capsules"]["gated"],
        )
        # The fixtures are really there, so the partition is not 0 == 0.
        self.assertGreaterEqual(mkt["capsules"]["total"], 1)
        self.assertGreaterEqual(dev["capsules"]["total"], 1)

    def test_empty_domains_is_the_empty_view_never_everything(self) -> None:
        empty = read_index.stats(domains=[])
        self.assertEqual(empty["capsules"]["total"], 0)
        self.assertEqual(empty["tickets"]["total"], 0)
        self.assertEqual(empty["feedback"]["total"], 0)
        self.assertEqual(read_index.query_feedback(domains=[]), [])
        self.assertEqual(read_index.stats_from_files(domains=[])["capsules"]["total"], 0)

    def test_feedback_filter_matches_the_file_fallback(self) -> None:
        sql = {r["entry_id"] for r in read_index.query_feedback(domains=[MARKETING], limit=500)}
        files = {
            r["entry_id"]
            for r in read_index.query_feedback_from_files(domains=[MARKETING], limit=500)
        }
        self.assertEqual(sql, files)
        self.assertTrue(sql, "the marketing ledger view should not be empty")
        dev_ids = {
            r["entry_id"]
            for r in read_index.query_feedback_from_files(domains=[DEVELOPMENT], limit=500)
        }
        self.assertEqual(sql & dev_ids, set(), "the two domains must not overlap")

    def test_gated_capsule_filter_matches_the_file_fallback(self) -> None:
        sql = {r["capsule_id"] for r in read_index.query_gated_capsules(500, domains=[MARKETING])}
        files = {
            r["capsule_id"]
            for r in read_index.query_gated_capsules_from_files(500, domains=[MARKETING])
        }
        self.assertEqual(sql, files)
        dev = {
            r["capsule_id"]
            for r in read_index.query_gated_capsules_from_files(500, domains=[DEVELOPMENT])
        }
        self.assertEqual(sql & dev, set())


# --------------------------------------------------------------------------- #
# hygiene (report-only): users.json must never be committed
# --------------------------------------------------------------------------- #


class UsersFileIsNotCommitted(unittest.TestCase):
    """The sovereign user registry is runtime state, never a committed artifact."""

    def test_users_json_is_git_ignored(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        rules = {line.strip() for line in gitignore if line.strip()}
        self.assertIn("config/users.json", rules)
        self.assertIn("config/.bootstrap_admin_password", rules)


if __name__ == "__main__":
    unittest.main()
