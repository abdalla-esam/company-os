#!/usr/bin/env python3
"""access_control.py — which door, which grant: the HTTP capability policy.

Brick 1 built the locks (:mod:`capsule_engine.capabilities`). This module is
where they are hung on the doors: the **route → capability map** and the
credential → :class:`Principal` resolution the API middleware executes.

Two rules make this layer safe rather than decorative:

1. **Default deny.** The map is exhaustive, not advisory. A guarded route with
   no declared capability is *refused* — ``resolve()`` reports it as
   ``undeclared`` and the middleware fails closed. Forgetting an entry
   therefore cannot open a hole; it closes a door and fails the coverage test
   in ``tests/test_route_capability_coverage.py``, which walks the live
   ``app.routes`` and demands an entry for every one of them.
2. **Reads are not free.** ``factory:read`` guards every non-mutating route.
   The consequence is deliberate: a cockpit user needs ``factory:read`` *plus*
   whatever decisions they make. A key that can approve but not read is a key
   that cannot see what it is approving.

Route keys are canonical ``/api/*`` templates; the ``/cockpit/api/*`` mirror
the API also mounts is derived mechanically, so the two can never drift.

The legacy shared token survives the migration as an explicit, switchable
**root principal** (:data:`LEGACY_ROOT_ENV`), recorded in every audit line as
``legacy-token`` — an omnipotent credential that is visible in the log is
strictly better than one that is anonymous, and the switch is what ends it.

Standard library only apart from the kernel it enforces; Python 3.10+.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from capsule_engine.capabilities import (
    ALL_CAPABILITIES,
    CAPABILITY_ALL,
    SCOPE_ALL,
    SCOPED_CAPABILITIES,
    TOKEN_PREFIX,
    CapabilityKey,
    Grant,
    verify_key,
)
from capsule_engine.exceptions import CapabilityError

logger = logging.getLogger("orchestration.access_control")

__all__ = [
    "CANONICAL_API_PREFIX",
    "COCKPIT_MIRROR_PREFIX",
    "GUARDED_PREFIXES",
    "LEGACY_ROOT_ENV",
    "LEGACY_ROOT_SUBJECT",
    "PUBLIC_API_PATHS",
    "ROUTE_CAPABILITIES",
    "Principal",
    "Requirement",
    "authenticate",
    "declared_rules",
    "is_guarded",
    "legacy_root_enabled",
    "may_attempt",
    "resolve",
    "scope_denial",
]

# Only these prefixes are capability-guarded. The cockpit shell, its static
# assets, and ``/`` must stay reachable before anyone has a credential —
# otherwise there is no page left to log in from.
#
# ``/v1/`` is the mobile contract's namespace. It is guarded **before its first
# route exists**, deliberately: a prefix added later would leave every endpoint
# mounted in the meantime served with no credential at all, because
# ``is_guarded()`` is the only thing that puts a request under the middleware in
# the first place — an unguarded path is not "checked later", it is not checked
# at all. Guarding the namespace costs nothing while it is empty, and it makes
# the default deny cover every route from the moment it is mounted.
GUARDED_PREFIXES: tuple[str, ...] = ("/api/", "/cockpit/api/", "/v1/")

# Reachable without a credential: a health probe that must answer while the
# operator is still configuring the server, and the login endpoint.
PUBLIC_API_PATHS: frozenset[str] = frozenset(
    {"/api/health", "/cockpit/api/health", "/api/auth/login", "/cockpit/api/auth/login"}
)

COCKPIT_MIRROR_PREFIX = "/cockpit"

#: The canonical namespace whose ``/cockpit/api/*`` twin is derived mechanically
#: (``api.py`` decorates both spellings on every handler). Only templates under
#: this prefix have a mirror; other guarded namespaces are their own routes.
CANONICAL_API_PREFIX = "/api/"

# Migration switch for the legacy shared token (see module docstring).
LEGACY_ROOT_ENV = "AGENCY_LEGACY_ROOT_TOKEN"
LEGACY_ROOT_SUBJECT = "legacy-token"

_KEY_PREFIX = f"{TOKEN_PREFIX}."


# --------------------------------------------------------------------------- #
# the map
# --------------------------------------------------------------------------- #
#
# One entry per *decision*, not per endpoint: three dev tools share
# ``tools:invoke`` because they are one decision ("let this caller poke the
# workshop"). Reads share ``factory:read`` for the same reason. A new endpoint
# that fits an existing decision reuses its capability; one that does not
# forces a new name into the kernel's closed vocabulary, on purpose.

ROUTE_CAPABILITIES: Mapping[tuple[str, str], str] = {
    # --- identity & session -------------------------------------------------
    ("GET", "/api/auth/me"): "factory:read",
    ("POST", "/api/auth/logout"): "factory:read",
    # --- reads: one capability, every non-mutating view ---------------------
    ("GET", "/api/events"): "factory:read",
    ("GET", "/api/stats"): "factory:read",
    ("GET", "/api/metrics"): "factory:read",
    ("GET", "/api/pillars"): "factory:read",
    ("GET", "/api/domains"): "factory:read",
    # Task-type catalogue (config/task_types/): read-only introspection.
    ("GET", "/api/task-types"): "factory:read",
    ("GET", "/api/task-types/{task_type_id}"): "factory:read",
    ("GET", "/api/tickets"): "factory:read",
    ("GET", "/api/tickets/{ticket_id}"): "factory:read",
    ("GET", "/api/gates"): "factory:read",
    ("GET", "/api/cadence"): "factory:read",
    ("GET", "/api/agents"): "factory:read",
    ("GET", "/api/engine/gears"): "factory:read",
    ("GET", "/api/engine/gears/health"): "factory:read",
    ("GET", "/api/telemetry/costs"): "factory:read",
    ("GET", "/api/telemetry/reviewers"): "factory:read",
    # --- finance: the spend gate (spend_control) -------------------------
    # Status is a read; flipping the kill switch is its own decision, one no
    # other capability implies (it stops the whole factory's spend).
    ("GET", "/api/finance/status"): "factory:read",
    ("POST", "/api/finance/kill"): "finance:control",
    ("GET", "/api/capsules"): "factory:read",
    ("GET", "/api/capsules/pending"): "factory:read",
    ("GET", "/api/capsules/{capsule_id}"): "factory:read",
    ("GET", "/api/capsules/{capsule_id}/image"): "factory:read",
    ("GET", "/api/config/marketing"): "factory:read",
    ("POST", "/api/config/marketing"): "capsule:build",
    ("GET", "/api/config/brand"): "factory:read",
    ("POST", "/api/config/brand"): "capsule:build",
    ("PATCH", "/api/config/brand"): "capsule:build",
    ("GET", "/api/config/company"): "factory:read",
    ("GET", "/api/config/company/schema"): "factory:read",
    ("POST", "/api/config/company"): "capsule:build",
    ("PATCH", "/api/config/company"): "capsule:build",
    ("GET", "/api/audit/spool"): "factory:read",
    # --- in-flight telemetry, radar, gates, trace, previews (web + Flutter) --
    ("GET", "/api/tasks/in-flight"): "factory:read",
    ("GET", "/api/radar/tasks"): "factory:read",
    ("GET", "/api/memory/feedback"): "factory:read",
    ("GET", "/api/capsules/{capsule_id}/outputs"): "factory:read",
    ("GET", "/api/capsules/{capsule_id}/outputs/{output_name}"): "factory:read",
    ("GET", "/api/capsules/{capsule_id}/gates"): "factory:read",
    ("GET", "/api/capsules/{capsule_id}/trace"): "factory:read",
    ("GET", "/api/capsules/{capsule_id}/preview"): "factory:read",
    # --- human decisions ----------------------------------------------------
    ("POST", "/api/capsules/{capsule_id}/approve"): "capsule:approve",
    ("POST", "/api/capsules/{capsule_id}/reject"): "capsule:reject",
    ("POST", "/api/capsules/{capsule_id}/gates/{gate_id}/open"): "factory:read",
    ("POST", "/api/capsules/{capsule_id}/gates/{gate_id}/decision"): "capsule:approve",
    ("POST", "/api/capsules/{capsule_id}/viewed"): "factory:read",
    ("POST", "/api/gates/{gate_id}/approve"): "gate:approve",
    ("POST", "/api/gates/{gate_id}/reject"): "gate:reject",
    # --- creating & modifying work ------------------------------------------
    ("POST", "/api/capsules"): "capsule:create",
    ("POST", "/api/capsules/build_post"): "capsule:build",
    ("POST", "/api/tickets"): "ticket:create",
    ("PATCH", "/api/tickets/{ticket_id}/context"): "ticket:create",
    ("POST", "/api/tickets/{ticket_id}/promote"): "ticket:create",
    ("POST", "/api/tickets/{ticket_id}/demote"): "ticket:create",
    ("POST", "/api/tickets/settle"): "ticket:settle",
    ("POST", "/api/domains"): "domain:create",
    # --- moving the engine --------------------------------------------------
    ("POST", "/api/pulse/run"): "pulse:run",
    ("POST", "/api/engine/beat"): "engine:beat",
    ("GET", "/api/queue"): "factory:read",
    ("POST", "/api/queue"): "queue:manage",
    ("POST", "/api/queue/drain"): "queue:manage",
    ("POST", "/api/queue/{item_id}/cancel"): "queue:manage",
    ("POST", "/api/ideation/run"): "ideation:run",
    ("POST", "/api/index/rebuild"): "index:rebuild",
    # --- workshop tools & sandbox -------------------------------------------
    ("GET", "/api/sandbox/capsules"): "factory:read",
    ("GET", "/api/sandbox/capsules/{capsule_id}"): "factory:read",
    ("POST", "/api/sandbox/trial"): "tools:invoke",
    ("POST", "/api/sandbox/promote/{capsule_id}"): "capsule:approve",
    ("POST", "/api/sandbox/clean"): "tools:invoke",
    ("POST", "/api/tools/qa_inspect"): "tools:invoke",
    ("POST", "/api/tools/prompt_distill"): "tools:invoke",
    ("POST", "/api/tools/test_image"): "tools:invoke",
    # --- the mobile contract (/v1) -----------------------------------------
    # Its own namespace, its own keys. The review is a **sector-scoped read**:
    # its floor is ``domain:read`` (a scoped capability, so the middleware only
    # checks that *some* sector is readable) and the handler then proves the
    # object's real domain inside the service — an unreadable sector answers 404
    # with the missing-object body, never 403. The decision is the same
    # branch-scoped ``capsule:approve`` the cockpit's routes require (a reject is
    # enforced to ``capsule:reject`` on the resource inside the service's lock).
    # ``_mirror()`` leaves these templates alone — ``/v1`` has no ``/cockpit``
    # twin.
    ("GET", "/v1/capsules/{capsule_id}/review"): "domain:read",
    ("POST", "/v1/decisions"): "capsule:approve",
    # --- Slice M1a: the aggregate reads (home, attention, lists, details) ----
    # All eight floor at the same scoped ``domain:read`` as the review routes:
    # the middleware only proves that *some* sector is readable, and the handler
    # then filters every row in SQL by the sectors the credential names, so a
    # ``factory:read``-only key (engine-wide) is refused outright while a
    # sector-scoped key sees exactly its own sectors. Detail reads prove the
    # object's real sector with ``require_sector`` (404, never 403).
    ("GET", "/v1/me"): "domain:read",
    ("GET", "/v1/overview"): "domain:read",
    ("GET", "/v1/attention"): "domain:read",
    ("GET", "/v1/capsules"): "domain:read",
    ("GET", "/v1/capsules/{capsule_id}"): "domain:read",
    ("GET", "/v1/capsules/{capsule_id}/trace"): "domain:read",
    ("GET", "/v1/tickets"): "domain:read",
    ("GET", "/v1/tickets/{ticket_id}"): "domain:read",
    # Slice 2: ticket accept/send-back and needs_gate decisions. The ticket
    # decision is the same branch-scoped ``ticket:create`` the legacy promote /
    # demote routes require; the gate decision's coarse floor is
    # ``gate:approve`` (a branch-scoped reject is enforced to ``gate:reject`` on
    # the resource inside the service — the same split ``POST /v1/decisions``
    # uses for capsule:approve/reject). Both reads are sector-scoped like the
    # capsule review above.
    ("GET", "/v1/tickets/{ticket_id}/review"): "domain:read",
    ("POST", "/v1/tickets/{ticket_id}/decision"): "ticket:create",
    ("GET", "/v1/gates/{gate_id}/review"): "domain:read",
    ("POST", "/v1/gates/{gate_id}/decision"): "gate:approve",
}


def _mirror(template: str) -> str:
    """The ``/cockpit/api/*`` twin the API mounts alongside a canonical route.

    Only the canonical ``/api/*`` namespace is mirrored, because only it is
    dual-mounted. A template from another guarded namespace (``/v1/*``, the
    mobile contract) is already its own route and is returned unchanged:
    prefixing it would invent a ``/cockpit/v1/*`` rule for a path the API never
    mounts — dead policy that looks like coverage and quietly sidesteps the real
    question (does this route have a capability of its own?).
    """
    if not template.startswith(CANONICAL_API_PREFIX):
        return template
    return COCKPIT_MIRROR_PREFIX + template


def declared_rules() -> dict[tuple[str, str], str]:
    """Every declared rule, canonical plus cockpit mirror.

    The coverage test compares this against the application's real routes in
    both directions, so a rule for a route that no longer exists is caught
    just as loudly as a route with no rule.
    """
    rules: dict[tuple[str, str], str] = {}
    for (method, template), capability in ROUTE_CAPABILITIES.items():
        rules[(method, template)] = capability
        rules[(method, _mirror(template))] = capability
    return rules


def _compile_template(template: str) -> "re.Pattern[str]":
    """Turn ``/api/gates/{gate_id}/approve`` into an anchored regex.

    ``{param}`` becomes ``[^/]+``: one path segment, never empty, never a
    traversal. Built by splitting on the placeholders rather than escaping and
    re-substituting, so a literal ``{`` or ``.`` in a template can never be
    reinterpreted as a quantifier.
    """
    parts: list[str] = []
    position = 0
    for match in re.finditer(r"\{[A-Za-z_][A-Za-z0-9_]*\}", template):
        parts.append(re.escape(template[position : match.start()]))
        parts.append("[^/]+")
        position = match.end()
    parts.append(re.escape(template[position:]))
    return re.compile("^" + "".join(parts) + "$")


# Compiled once, from the *full* rule set (canonical plus cockpit mirror), and
# keyed by METHOD as well as path: ``POST /api/capsules`` and
# ``GET /api/capsules`` are different decisions on the same URL. Fewer
# placeholders first, so a literal path always wins over a parameterised
# sibling and ``/api/capsules/pending`` can never be swallowed by a template.
_COMPILED: tuple[tuple[str, "re.Pattern[str]", str], ...] = tuple(
    sorted(
        (
            (method, _compile_template(template), capability)
            for (method, template), capability in declared_rules().items()
        ),
        key=lambda rule: rule[1].pattern.count("[^/]+"),
    )
)


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #

def is_guarded(path: str) -> bool:
    """True when ``path`` falls under a capability-guarded prefix."""
    return isinstance(path, str) and any(
        path.startswith(prefix) for prefix in GUARDED_PREFIXES
    )


@dataclass(frozen=True)
class Requirement:
    """What one request needs: the capability, and whether it is covered.

    ``declared=False`` is the fail-closed signal — the route is not in the map,
    which is a configuration bug, and the middleware refuses it rather than
    guessing that it is safe.
    """

    capability: str | None
    declared: bool

    @property
    def is_public(self) -> bool:
        """Declared, and requiring no capability (health probes, OPTIONS)."""
        return self.declared and self.capability is None


def _normalize_method(method: str) -> str:
    """HEAD is GET with no body; everything else is upper-cased."""
    upper = (method or "").upper()
    return "GET" if upper == "HEAD" else upper


def resolve(method: str, path: str) -> Requirement:
    """Resolve one request into a :class:`Requirement`.

    Order of decision, each step fail-closed:

    1. paths outside the guarded prefixes need nothing (the cockpit shell must
       load before a credential exists);
    2. explicitly public paths need nothing;
    3. CORS preflight (``OPTIONS``) needs nothing here — the CORS middleware
       decides it, and a preflight carries no credentials to check anyway;
    4. a matching template names its capability;
    5. anything else is **undeclared** — refused by the caller.
    """
    method = _normalize_method(method)
    if not is_guarded(path):
        return Requirement(None, True)
    if path in PUBLIC_API_PATHS:
        return Requirement(None, True)
    if method == "OPTIONS":
        return Requirement(None, True)
    for rule_method, pattern, capability in _COMPILED:
        if rule_method == method and pattern.match(path):
            return Requirement(capability, True)
    return Requirement(None, False)


# --------------------------------------------------------------------------- #
# the authenticated caller
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Principal:
    """A verified caller: who they are and what they may do *on what*.

    Built only by :func:`authenticate` — never from request data. Both the
    capability-key path and the legacy-token path produce one, so downstream
    code has a single shape to read and no reason to look at the raw
    credential again.
    """

    subject: str
    grants: frozenset[Grant]
    key_id: str
    is_legacy: bool = False

    @classmethod
    def from_key(cls, key: CapabilityKey) -> "Principal":
        """Adapt a kernel-verified :class:`CapabilityKey`."""
        return cls(
            subject=key.subject,
            grants=key.grants,
            key_id=key.key_id,
            is_legacy=False,
        )

    @classmethod
    def legacy_root(cls, subject: str) -> "Principal":
        """The migration principal behind the legacy shared token.

        Holds the explicit root grant and is marked ``is_legacy`` so every
        audit line it writes says so — an omnipotent credential that cannot be
        told apart from a scoped one is the problem, not the solution.
        """
        return cls(
            subject=(subject or LEGACY_ROOT_SUBJECT).strip() or LEGACY_ROOT_SUBJECT,
            grants=frozenset({Grant(CAPABILITY_ALL)}),
            key_id=LEGACY_ROOT_SUBJECT,
            is_legacy=True,
        )

    @property
    def capabilities(self) -> frozenset[str]:
        """The raw grant strings, for audit lines and display."""
        return frozenset(grant.text for grant in self.grants)

    @property
    def is_root(self) -> bool:
        """True when this principal holds the explicit root grant."""
        return any(grant.capability == CAPABILITY_ALL for grant in self.grants)

    def has(self, capability: str, *, resource: str | None = None) -> bool:
        """True when ``capability`` is authorised on ``resource``.

        The precise question — see :meth:`CapabilityKey.has` for the three
        cases. Use this at the point of action, where the resource is known.
        """
        if not isinstance(capability, str) or not capability:
            return False
        if capability == CAPABILITY_ALL:
            return self.is_root
        for grant in self.grants:
            if grant.capability == CAPABILITY_ALL:
                return True
            if grant.capability != capability:
                continue
            if grant.scope is None:
                return True
            if resource is not None and grant.scope in (SCOPE_ALL, resource):
                return True
        return False

    def has_any_scope(self, capability: str) -> bool:
        """True when the verb is granted on *some* resource (coarse check)."""
        if not isinstance(capability, str) or not capability:
            return False
        if capability == CAPABILITY_ALL:
            return self.is_root
        return any(
            grant.capability in (CAPABILITY_ALL, capability) for grant in self.grants
        )

    def audit_note(self) -> str:
        """A log-safe provenance label: who decided, and under which key."""
        if self.is_legacy:
            return f"{self.subject} (legacy shared token)"
        return f"{self.subject} (capability key {self.key_id[:8]}…)"

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view (no credential material)."""
        return {
            "subject": self.subject,
            "capabilities": sorted(self.capabilities),
            "grants": [g.as_dict() for g in sorted(self.grants, key=lambda g: g.text)],
            "is_root": self.is_root,
            "is_legacy": self.is_legacy,
            "key_id": self.key_id,
        }


def may_attempt(principal: Principal | None, capability: str) -> bool:
    """The **coarse** gate. Is this verb held at all, on any resource?

    The middleware's question, and deliberately not an authorisation: a
    scoped capability says "may approve *somewhere*", which is a good reason
    to reject a credential immediately but never a reason to let the request
    through. The endpoint still proves the resource — under the capsule lock,
    where it cannot change between the check and the act.
    """
    if not isinstance(principal, Principal):
        return False
    if capability in SCOPED_CAPABILITIES:
        return principal.has_any_scope(capability)
    return principal.has(capability)


def scope_denial(
    principal: Principal | None, capability: str, resource: str
) -> str | None:
    """The **fine** gate: ``None`` when authorised, else a refusal message.

    Returns text rather than raising so this module stays framework-free; the
    API layer turns it into a 403. The message names the resource, the grant
    that was missing, and — importantly — what the credential *does* cover, so
    an operator can tell a wrong key from a wrong scope without reading code.
    """
    if not isinstance(principal, Principal):
        return "Unauthenticated: no verified principal — refusing (fail closed)."
    if principal.has(capability, resource=resource):
        return None
    held = sorted(grant.text for grant in principal.grants)
    return (
        f"Insufficient scope: '{capability}' is not granted for resource "
        f"{resource!r}. This credential covers {held}. Mint a key that includes "
        f"'{capability}:{resource}' (or '{capability}:{SCOPE_ALL}' for every "
        f"branch): python3 tools/mint_capability.py mint --subject <who> "
        f"--capabilities {capability} --scope {resource}"
    )


def legacy_root_enabled() -> bool:
    """Is the legacy shared token still accepted? (read at call time)

    Unset means **enabled**: that is the documented migration default, so an
    existing deployment keeps working. Any *unrecognised* value means
    **disabled** with a warning — a typo must never silently keep an
    omnipotent credential alive, which is the whole point of a kill switch.
    """
    raw = os.environ.get(LEGACY_ROOT_ENV, "").strip().lower()
    if raw == "":
        return True
    if raw in ("on", "1", "true", "yes", "enabled"):
        return True
    if raw in ("off", "0", "false", "no", "disabled"):
        return False
    logger.warning(
        "access_control: unrecognised %s=%r — treating the legacy token as "
        "DISABLED (fail closed); use 'on' or 'off'",
        LEGACY_ROOT_ENV,
        raw,
    )
    return False


def authenticate(
    presented: object,
    *,
    expected_token: str = "",
    legacy_subject: str = "",
) -> Principal:
    """Resolve a presented credential into a :class:`Principal`.

    A capability key is verified by the kernel (signature, vocabulary, expiry).
    Anything else is only accepted when it is the configured legacy token *and*
    the kill switch still allows it.

    Raises:
        CapabilityError: missing credential, forged/expired/malformed key,
            legacy token disabled, no shared token configured, or a plain
            mismatch. Every branch refuses; none downgrades to "anonymous".
    """
    token = presented.strip() if isinstance(presented, str) else ""
    if not token:
        raise CapabilityError("missing credential (fail closed)")

    if token.startswith(_KEY_PREFIX):
        return Principal.from_key(verify_key(token))

    if not legacy_root_enabled():
        raise CapabilityError(
            f"a capability key is required ({LEGACY_ROOT_ENV} disables the "
            "legacy shared token)"
        )
    if not expected_token:
        raise CapabilityError("no shared token is configured on the server")
    # compare_digest needs matching types and ASCII-safe bytes; encoding both
    # sides keeps a non-ASCII probe from raising instead of refusing.
    if not hmac.compare_digest(
        token.encode("utf-8", "replace"), expected_token.encode("utf-8", "replace")
    ):
        raise CapabilityError("invalid credential")
    return Principal.legacy_root(legacy_subject)


# Guard the vocabulary at import time: a typo in the map above would otherwise
# only surface when that one route is hit. The coverage test asserts this too,
# but failing at import is the honest place for a configuration error.
_unknown = sorted(
    {cap for cap in ROUTE_CAPABILITIES.values() if cap not in ALL_CAPABILITIES}
)
if _unknown:  # pragma: no cover - a mis-typed map is caught before it ships
    raise CapabilityError(
        f"access_control: route map declares capabilities outside the closed "
        f"vocabulary: {_unknown}"
    )
