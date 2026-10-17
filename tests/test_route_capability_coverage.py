"""Route ↔ capability coverage: the live app and the policy map must agree.

``orchestration/access_control`` documents this test as the thing that makes
``ROUTE_CAPABILITIES`` exhaustive rather than advisory ("the coverage test in
``tests/unit/test_api_access_control.py`` … walks the live ``app.routes``").
That file never existed, so the only assertion over the map was a two-route spot
check in ``tests/test_spend_control.py`` — a route could be added with no
capability entry and nothing failed. The namespace the mobile contract
(``/v1/``) was about to be built under was not guarded at all, which is the same
bug at a larger scale.

This module reads the real FastAPI ``app.routes`` (never a hand-written list)
and enforces four invariants:

1. **Coverage** — every guarded live route is either DECLARED in the map or an
   explicit ``PUBLIC_API_PATHS`` exception. Nothing else may be reachable.
2. **No stale policy** — every declared entry names a route that really exists.
3. **No dead policy** — every declared path is actually guarded; a rule for a
   path nobody guards protects nothing.
4. **Explicit exclusions** — routes that sit outside every guarded prefix are
   pinned by name with a reason, so one cannot appear silently.

The retired ``/cockpit/api/*`` mirror is asserted gone rather than asserted
equal: it doubled the route table and the map for a web cockpit that was never
shipped.

Both pinned sets are frozen and asserted to only ever shrink: a new gap fails
loudly instead of being absorbed, and a fixed one must be struck deliberately.

Read-only: introspection plus the isolation bootstrap's temporary vault.
"""

from __future__ import annotations  # noqa: E402  — the suite runs on 3.9

from tests import _bootstrap  # noqa: F401  — isolated vault before any engine import

import unittest
from typing import Dict, Set, Tuple

from orchestration import access_control, api

RouteKey = Tuple[str, str]


def _live_routes() -> Set[RouteKey]:
    """Every ``(METHOD, path)`` the live FastAPI app really serves.

    Duck-typed on purpose: the app mixes ``fastapi.routing.APIRoute`` (its own
    handlers) with ``starlette.routing.Route`` (the generated ``/docs``,
    ``/redoc``, ``/openapi.json``) and ``Mount`` objects for static files, which
    carry a path but no method vocabulary. ``HEAD`` folds onto ``GET`` through
    the policy module's own normalizer, so this set and ``resolve()`` can never
    disagree about what a method is called.
    """
    pairs: Set[RouteKey] = set()
    for route in api.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not path or not methods:
            continue
        for method in methods:
            pairs.add((access_control._normalize_method(method), path))
    return pairs


def _uncovered_guarded_routes() -> Set[RouteKey]:
    """Guarded live routes reachable with no declared capability and no exception."""
    declared = access_control.declared_rules()
    public = access_control.PUBLIC_API_PATHS
    return {
        key
        for key in _live_routes()
        if access_control.is_guarded(key[1])
        and key not in declared
        and key[1] not in public
    }


#: Guarded live routes that have NO capability entry. **Empty**: the shipped map
#: covers every guarded route, verified below. The set exists so a future gap
#: must be written down here with a reason instead of passing silently, and
#: ``test_known_gaps_are_still_gaps`` fails if a pinned gap stops being one — the
#: set may only shrink.
KNOWN_GAPS: Dict[RouteKey, str] = {}

#: Live routes OUTSIDE every guarded prefix, which therefore require no
#: capability by construction (``is_guarded()`` is False — the middleware never
#: sees them). Pinned by name so a new unguarded route cannot appear unnoticed:
#: that is exactly the failure mode that left ``/v1/`` open before Slice 0.
#: Each entry is asserted to still be live and still be unguarded, and the set
#: may only shrink.
UNGUARDED_BY_DESIGN: Dict[RouteKey, str] = {
    ("GET", "/"): "API root banner; must answer before any credential exists.",
    ("GET", "/docs"): "FastAPI's interactive docs page (pre-existing, reported).",
    ("GET", "/docs/oauth2-redirect"): "FastAPI's oauth2 redirect, mounted with /docs.",
    ("GET", "/openapi.json"): "FastAPI's generated schema (pre-existing, reported).",
    ("GET", "/redoc"): "FastAPI's ReDoc page, mounted with /docs.",
}


class RouteCapabilityMapIsExhaustive(unittest.TestCase):
    """The map's two promises: nothing undeclared, nothing declared in vain."""

    def test_the_live_app_is_actually_walked(self) -> None:
        # A route-less or stub app would make every assertion below vacuous.
        # The floor is a sanity check, not a target: it only has to sit well
        # above a stub. It was 100 while every handler was also dual-mounted
        # under /cockpit/api; that mirror is retired, so the real table is
        # roughly half its former size.
        self.assertGreater(
            len(_live_routes()), 50, "route walk found suspiciously few routes"
        )
        self.assertGreater(
            len(access_control.declared_rules()), 50, "declared rule set collapsed"
        )

    def test_every_guarded_route_is_declared_or_an_explicit_public_exception(self) -> None:
        uncovered = _uncovered_guarded_routes()
        new = sorted(uncovered - set(KNOWN_GAPS))
        self.assertEqual(
            new,
            [],
            "guarded routes with no capability entry — every one of them is "
            "reachable by any credential holding *any* grant, so declare it in "
            "orchestration/access_control.ROUTE_CAPABILITIES (or add it to "
            "PUBLIC_API_PATHS on purpose, and to UNGUARDED_BY_DESIGN if it is "
            f"outside a guarded prefix): {new}",
        )

    def test_known_gaps_are_still_gaps(self) -> None:
        uncovered = _uncovered_guarded_routes()
        stale = sorted(set(KNOWN_GAPS) - uncovered)
        self.assertEqual(
            stale,
            [],
            "KNOWN_GAPS may only shrink: these entries are no longer gaps, so "
            f"strike them deliberately: {stale}",
        )

    def test_no_stale_policy_entries(self) -> None:
        stale = sorted(set(access_control.declared_rules()) - _live_routes())
        self.assertEqual(
            stale,
            [],
            "declared entries with no live route — a rule for a route that no "
            f"longer exists is a door nobody hung, or a renamed route: {stale}",
        )

    def test_no_dead_policy_entries(self) -> None:
        dead = sorted(
            {
                (method, path)
                for (method, path) in access_control.declared_rules()
                if not access_control.is_guarded(path)
            }
        )
        self.assertEqual(
            dead,
            [],
            "declared rules for paths outside every guarded prefix — the "
            "middleware never applies them, so they are policy in name only "
            f"(a mis-derived mirror looks exactly like this): {dead}",
        )

    def test_public_exceptions_name_live_guarded_routes(self) -> None:
        live_paths = {path for (_method, path) in _live_routes()}
        for path in sorted(access_control.PUBLIC_API_PATHS):
            with self.subTest(path=path):
                self.assertIn(
                    path, live_paths, "PUBLIC_API_PATHS names a route that does not exist"
                )
                self.assertTrue(
                    access_control.is_guarded(path),
                    "PUBLIC_API_PATHS is an exception *inside* a guarded prefix; a "
                    "path outside one needs no exception",
                )
                self.assertTrue(
                    access_control.resolve("GET", path).is_public,
                    "a PUBLIC_API_PATHS entry must resolve as public",
                )

    def test_every_guarded_route_resolves_to_a_capability_or_a_public_pass(self) -> None:
        """``resolve()`` agrees with the map for every live guarded route."""
        declared = access_control.declared_rules()
        for method, path in sorted(
            key for key in _live_routes() if access_control.is_guarded(key[1])
        ):
            with self.subTest(method=method, path=path):
                requirement = access_control.resolve(method, path)
                self.assertTrue(
                    requirement.declared,
                    f"{method} {path} resolves as UNDECLARED — the middleware will "
                    "refuse it with 403 (fail closed, but the route is dead)",
                )
                if path in access_control.PUBLIC_API_PATHS:
                    self.assertTrue(requirement.is_public)
                else:
                    self.assertEqual(
                        requirement.capability,
                        declared[(method, path)],
                        f"{method} {path} resolves to a different capability than "
                        "the map declares",
                    )


class TheCockpitMirrorIsRetired(unittest.TestCase):
    """``/cockpit/api/*`` used to be a mechanical twin of every ``/api/*`` route.

    Every handler was dual-mounted, which doubled the route table and the
    capability map for a web cockpit that was never shipped. The prefix is no
    longer guarded, declared, or mounted; these pin that so a reintroduced twin
    has to be argued for here first.
    """

    MIRROR_PATHS = (
        "/cockpit/api/gates",
        "/cockpit/api/auth/login",
        "/cockpit/api/health",
        "/cockpit/api/capsules",
    )

    def test_mirror_prefix_is_not_guarded(self) -> None:
        for path in self.MIRROR_PATHS:
            with self.subTest(path=path):
                self.assertFalse(
                    access_control.is_guarded(path),
                    f"{path} is guarded — the retired cockpit mirror is back",
                )

    def test_no_rule_declares_a_mirror_path(self) -> None:
        leaked = sorted(
            (method, path)
            for (method, path) in access_control.declared_rules()
            if path.startswith("/cockpit/")
        )
        self.assertEqual(
            leaked,
            [],
            "a /cockpit rule survived in the capability map — the map no longer "
            "mirrors anything, so this is dead policy",
        )

    def test_the_api_does_not_mount_a_mirror_route(self) -> None:
        duplicated = sorted(
            (method, path)
            for (method, path) in _live_routes()
            if path.startswith("/cockpit/")
        )
        self.assertEqual(
            duplicated,
            [],
            "the API still mounts /cockpit routes; the duplicate is only half "
            "removed",
        )


class V1NamespaceIsGuardedBeforeItsFirstRoute(unittest.TestCase):
    """The mobile contract's namespace stays closed where no route exists yet.

    Slice 0 added the guard to an *empty* namespace. Slice 1 mounted its first
    routes (``GET /v1/capsules/{id}/review`` and ``POST /v1/decisions``), and the
    M1a reads added the collections, so the sample below is the paths that remain
    **unmounted**: they must still be undeclared and non-public — the default-deny
    property for a route that does not exist. Whether a *mounted* route carries a
    capability is the separate, forward-looking invariant in
    ``test_v1_routes_and_declarations_stay_in_lockstep``.
    """

    SAMPLE_PATHS = (
        "/v1/",
        "/v1/health",
        # Unmounted, and not swallowed by a parameterised sibling: the templates
        # are anchored, so ``/{capsule_id}/review`` cannot claim an ``/outputs``
        # path and this stays undeclared (default deny for a route that does not
        # exist — the same property the namespace had before its first route).
        "/v1/capsules/x/outputs",
        "/v1/deeply/nested/unknown/thing",
    )

    def test_every_v1_path_is_guarded(self) -> None:
        for path in self.SAMPLE_PATHS:
            with self.subTest(path=path):
                self.assertTrue(
                    access_control.is_guarded(path),
                    f"{path} is not guarded — the middleware would not see it at all",
                )

    def test_v1_paths_are_undeclared_and_not_public(self) -> None:
        for path in self.SAMPLE_PATHS:
            for method in ("GET", "POST", "PATCH", "DELETE"):
                with self.subTest(method=method, path=path):
                    requirement = access_control.resolve(method, path)
                    self.assertFalse(
                        requirement.declared,
                        f"{method} {path} claims to be declared without a rule",
                    )
                    self.assertFalse(
                        requirement.is_public,
                        f"{method} {path} resolves as public — /v1/ takes no "
                        "unauthenticated exception",
                    )
                    self.assertIsNone(requirement.capability)

    def test_no_live_route_sits_on_the_guard_boundary(self) -> None:
        # The guarded prefix is "/v1/" with a trailing slash, like "/api/": a
        # route mounted at bare "/v1" (or "/v1x") would fall outside is_guarded()
        # and be served with no credential.
        boundary = sorted(
            {
                path
                for (_method, path) in _live_routes()
                if path.startswith("/v1") and not access_control.is_guarded(path)
            }
        )
        self.assertEqual(
            boundary,
            [],
            "routes sit on the guard boundary and would be unauthenticated "
            f"(mount them under /v1/): {boundary}",
        )

    def test_v1_has_no_public_exception(self) -> None:
        v1_public = sorted(
            path for path in access_control.PUBLIC_API_PATHS if path.startswith("/v1")
        )
        self.assertEqual(
            v1_public,
            [],
            "the mobile namespace takes no unauthenticated exception: "
            f"{v1_public} — every /v1/ path needs a credential",
        )

    def test_v1_routes_and_declarations_stay_in_lockstep(self) -> None:
        """The first ``/v1/`` routes arrived WITH their capabilities."""
        live_v1 = sorted(
            {path for (_method, path) in _live_routes() if path.startswith("/v1/")}
        )
        declared_v1 = sorted(
            {
                path
                for (_method, path) in access_control.declared_rules()
                if path.startswith("/v1/")
            }
        )
        self.assertEqual(
            live_v1,
            declared_v1,
            "a /v1/ route exists without a declared capability (or a rule exists "
            "for a /v1/ route that is not mounted) — the namespace is guarded, "
            "so an undeclared route is dead and an unmounted rule is noise",
        )


class UnguardedRoutesArePinned(unittest.TestCase):
    """Everything outside the guarded prefixes is named, with a reason."""

    def test_pinned_exclusions_are_live_and_really_unguarded(self) -> None:
        live = _live_routes()
        for key, _reason in sorted(UNGUARDED_BY_DESIGN.items()):
            with self.subTest(key=key):
                self.assertIn(key, live, "pinned exclusion is not a live route")
                self.assertFalse(
                    access_control.is_guarded(key[1]),
                    f"{key[1]} is now guarded — strike it from UNGUARDED_BY_DESIGN",
                )

    def test_no_other_route_escapes_the_guard(self) -> None:
        escaped = sorted(
            key for key in _live_routes() if not access_control.is_guarded(key[1])
        )
        self.assertEqual(
            escaped,
            sorted(UNGUARDED_BY_DESIGN),
            "a route outside every guarded prefix appeared or disappeared — such "
            "a route is served with NO credential (is_guarded() is the only "
            "thing that puts a request under the middleware). Add it to "
            "UNGUARDED_BY_DESIGN with a reason, or move it under a guarded "
            f"prefix: {escaped}",
        )

    def test_the_pinned_set_can_only_shrink(self) -> None:
        escaped = {key for key in _live_routes() if not access_control.is_guarded(key[1])}
        stale = sorted(set(UNGUARDED_BY_DESIGN) - escaped)
        self.assertEqual(
            stale,
            [],
            "UNGUARDED_BY_DESIGN may only shrink: these entries are no longer "
            f"live unguarded routes, so strike them deliberately: {stale}",
        )


if __name__ == "__main__":
    unittest.main()
