"""The ``/v1/`` namespace meets the credential layer — behaviour, not policy.

``tests/test_route_capability_coverage.py`` asserts the policy *looks* right
(``is_guarded()``, ``resolve()``). This module drives real HTTP requests through
the whole ASGI stack — in-process, no socket — and asserts what a client
actually receives, which is the interface the mobile app will use.

The failure this closes: before Slice 0 ``is_guarded("/v1/x")`` was False, so
``GET /v1/x`` reached the router and answered **404 with no credential
presented at all**. Every future ``/v1/`` endpoint would have been born
unauthenticated; today the same request is refused by the middleware before
routing.

Ordering, recorded because the task asked which status wins: an *authenticated*
request to an undeclared ``/v1/`` path is refused **403**, not 404 — the
undeclared check runs after authentication and before routing, exactly as it
does for ``/api/``. That ordering is deliberate in both namespaces: an
unauthenticated caller must not be able to probe which paths are misdeclared.
"""

from __future__ import annotations  # noqa: E402  — the suite runs on 3.9

from tests import _bootstrap  # noqa: F401  — isolated vault before any engine import

import os
import unittest
from unittest import mock

from capsule_engine.capabilities import mint_key
from orchestration import api
from starlette.testclient import TestClient

#: A test-only signing secret (>= the 32-byte minimum mint_key enforces).
#: ``capabilities.resolve_secret()`` reads the environment per request and
#: ``access_control.authenticate()`` verifies against it, so patching the
#: variable in makes minting and verification hermetic: the ambient deployment
#: secret is neither read nor required.
TEST_SECRET = "slice0-test-signing-secret-0123456789abcdef"
TEST_TTL_SECONDS = 600

#: Paths that must all be refused without a credential. None of them is mounted:
#: that is the point — the guard must fire on paths that do not exist yet.
UNMOUNTED_V1_PATHS = (
    "/v1/x",
    "/v1/",
    "/v1/health",
    "/v1/overview",
    "/v1/capsules/cap_example/review",
)


def _token(*capabilities: str) -> str:
    """A signed capability key for the test environment's secret."""
    return mint_key(
        "slice0-tester",
        list(capabilities),
        secret=TEST_SECRET.encode("utf-8"),
        ttl_seconds=TEST_TTL_SECONDS,
    )


class V1PrefixGuardTestCase(unittest.TestCase):
    """Shared client and credential environment."""

    def setUp(self) -> None:
        self._env = mock.patch.dict(
            os.environ, {"AGENCY_CAPABILITY_SECRET": TEST_SECRET}
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.client = TestClient(api.app)
        self.read_headers = {"Authorization": f"Bearer {_token('factory:read')}"}


class UnauthenticatedV1IsRefused(V1PrefixGuardTestCase):
    """No credential, no answer — for every /v1/ path, mounted or not."""

    def test_unauthenticated_get_is_refused(self) -> None:
        response = self.client.get("/v1/x")
        self.assertEqual(response.status_code, 401, response.text)
        # The middleware's own refusal, not the router's 404: proof the guard ran.
        self.assertIn("Unauthorized", response.json().get("detail", ""))

    def test_unauthenticated_post_is_refused(self) -> None:
        response = self.client.post("/v1/x", json={"anything": True})
        self.assertEqual(response.status_code, 401, response.text)
        self.assertIn("Unauthorized", response.json().get("detail", ""))

    def test_every_unmounted_v1_path_is_refused(self) -> None:
        for path in UNMOUNTED_V1_PATHS:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(
                    response.status_code,
                    401,
                    f"GET {path} answered {response.status_code}; before the "
                    "guard existed it answered 404 with no credential at all",
                )
                self.assertNotEqual(response.json().get("detail"), "Not Found")

    def test_a_forged_credential_is_refused(self) -> None:
        response = self.client.get(
            "/v1/x", headers={"Authorization": "Bearer cap.bogus.signature"}
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_v1_health_is_not_public(self) -> None:
        # The one path a health-probe reflex would add to PUBLIC_API_PATHS.
        unauthenticated = self.client.get("/v1/health")
        self.assertEqual(unauthenticated.status_code, 401, unauthenticated.text)
        # ... and with a credential it is still not served: no live route, and
        # no declared capability (403 undeclared, never a 200 shell).
        authenticated = self.client.get("/v1/health", headers=self.read_headers)
        self.assertEqual(authenticated.status_code, 403, authenticated.text)


class AuthenticatedButUndeclaredIsRefusedLikeApi(V1PrefixGuardTestCase):
    """A credential is not a licence: undeclared paths fail closed in both namespaces."""

    def test_undeclared_v1_and_undeclared_api_answer_the_same_way(self) -> None:
        v1 = self.client.get("/v1/x", headers=self.read_headers)
        api_unknown = self.client.get("/api/does_not_exist", headers=self.read_headers)

        self.assertEqual(v1.status_code, 403, v1.text)
        self.assertEqual(api_unknown.status_code, 403, api_unknown.text)
        self.assertEqual(v1.status_code, api_unknown.status_code)
        # 403, not 404: the middleware refuses before the router can speak.
        self.assertNotEqual(v1.status_code, 404)
        self.assertIn("No capability is declared", v1.json().get("detail", ""))
        self.assertIn("access_control", v1.json().get("detail", ""))

    def test_undeclared_v1_post_is_refused_too(self) -> None:
        response = self.client.post("/v1/x", headers=self.read_headers, json={})
        self.assertEqual(response.status_code, 403, response.text)

    def test_the_guard_does_not_pre_authorise_anything(self) -> None:
        # No capability entry exists for /v1/ yet, so even a ROOT credential is
        # refused: the namespace is closed until a rule is declared for it.
        root = {"Authorization": f"Bearer {_token('*')}"}
        response = self.client.get("/v1/x", headers=root)
        self.assertEqual(response.status_code, 403, response.text)


class ExistingPublicSurfaceIsUnchanged(V1PrefixGuardTestCase):
    """``/api/`` behaviour is untouched by the new guarded prefix."""

    def test_health_stays_public(self) -> None:
        for path in ("/api/health",):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json().get("status"), "ok")

    def test_login_stays_public_and_reaches_its_handler(self) -> None:
        response = self.client.post(
            "/api/auth/login", json={"username": "nobody", "password": "not-the-password"}
        )
        # The handler's own refusal — not the middleware's "Unauthorized: …".
        self.assertEqual(response.status_code, 401, response.text)
        detail = response.json().get("detail", "")
        self.assertIn("Invalid username or password", detail)
        self.assertNotIn("Unauthorized", detail)

    def test_declared_api_routes_keep_their_credential_contract(self) -> None:
        unauthenticated = self.client.get("/api/stats")
        self.assertEqual(unauthenticated.status_code, 401, unauthenticated.text)

        authenticated = self.client.get("/api/stats", headers=self.read_headers)
        self.assertEqual(authenticated.status_code, 200, authenticated.text)
        self.assertIn("pending_gates", authenticated.json())

    def test_preflight_is_not_rejected_by_the_guard(self) -> None:
        # OPTIONS passes the guard by design (the CORS middleware owns it), so it
        # reaches the router and 404s in both namespaces — parity, not a hole: a
        # preflight carries no credential to check and no data to leak.
        v1 = self.client.options("/v1/x")
        api_unknown = self.client.options("/api/does_not_exist")
        self.assertEqual(v1.status_code, 404, v1.text)
        self.assertEqual(v1.status_code, api_unknown.status_code)


class NeverConfiguredServerFailsClosedEverywhere(V1PrefixGuardTestCase):
    """With no credential mechanism at all, guarded paths answer 503 in parity."""

    def test_v1_and_api_both_refuse_when_no_credential_mechanism_exists(self) -> None:
        with mock.patch.object(api, "API_TOKEN", ""), mock.patch.dict(
            os.environ, {"AGENCY_CAPABILITY_SECRET": ""}
        ):
            for path in ("/v1/x", "/api/stats"):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertIn("Auth not configured", response.json().get("detail", ""))


if __name__ == "__main__":
    unittest.main()
