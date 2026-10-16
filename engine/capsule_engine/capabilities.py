#!/usr/bin/env python3
"""capabilities.py — capability-based keys: authority by signed grant, not by title.

The engine's last omnipotent secret is the shared ``AGENCY_API_TOKEN``: one
string that opens every door — approve, publish, create branches, lower the
autonomy ladder. This module introduces the alternative the constitution asks
for: **a key that carries its own capabilities**, signed by the server, so the
server stores no per-user state (the file system stays the database) and any
tampering is arithmetic, not guesswork.

A key is::

    cap.<base64url(payload)>.<base64url(hmac_sha256(secret, payload))>

where ``payload`` is compact, key-sorted JSON::

    {"caps":["capsule:approve:marketing"],"exp":1790000000,"iat":1787000000,
     "jti":"3f9a1c22b7e04d55","sub":"abdallah","v":1}

Grants are ``resource:action`` for engine-wide decisions, and
``resource:action:scope`` for decisions that act on a branch. A scoped
capability with no scope is not representable at all — see :class:`Grant`.

Design rules (every one of them fail closed):

- **the vocabulary is closed** — a capability outside :data:`ALL_CAPABILITIES`
  (or the explicit root grant :data:`CAPABILITY_ALL`) invalidates the *whole*
  key. Unknown grants are never silently dropped, because dropping them would
  turn a forged key into a usable weaker one;
- **the signature covers the exact bytes that arrive** — the digest is checked
  *before* the payload is parsed, so attacker-controlled JSON is never decoded
  by a key that has not proven itself;
- **verification is stateless and offline** — no database, no revocation file,
  no clock service: ``exp`` (expiry) closes the window, and the clock only
  moves forward;
- **a missing secret refuses everything** — no secret configured is a refusal,
  not a bypass (mirror of the API's fail-closed 503);
- **keys expire** — :func:`mint_key` refuses a non-positive or absurd TTL, so a
  key that never dies cannot be minted by accident.

Where this sits: this module is pure policy, the twin of
:mod:`capsule_engine.governance` (which decides *how much* autonomy a domain
has earned). Governance answers "may an agent act?"; capabilities answer "may
*this* caller act, and on what?". Both default to refusal when unsure.

Standard library only: ``base64``, ``hmac``, ``hashlib``, ``json``, ``os``,
``re``, ``secrets``, ``time``, ``dataclasses``, ``typing``. Python 3.10+.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from capsule_engine.exceptions import CapabilityError

__all__ = [
    "CAPABILITY_ALL",
    "CAPABILITY_ENV",
    "DEFAULT_TTL_SECONDS",
    "GLOBAL_CAPABILITIES",
    "KEY_VERSION",
    "KNOWN_CAPABILITIES",
    "ALL_CAPABILITIES",
    "MAX_KEY_BYTES",
    "MAX_TTL_SECONDS",
    "MIN_SECRET_BYTES",
    "SCOPED_CAPABILITIES",
    "SCOPE_ALL",
    "TOKEN_PREFIX",
    "CapabilityKey",
    "Grant",
    "has_capability",
    "mint_key",
    "mint_root_key",
    "resolve_secret",
    "verify_key",
]

# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

# The environment variable holding the signing secret. Explicit arguments
# always win over the environment (mirror of AGENCY_GOVERNANCE_FILE).
CAPABILITY_ENV = "AGENCY_CAPABILITY_SECRET"

TOKEN_PREFIX = "cap"
KEY_VERSION = 1

DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
MAX_TTL_SECONDS = 10 * 365 * 24 * 60 * 60  # 10 years — a key, not an eternity
MIN_SECRET_BYTES = 32  # enforced when minting; see resolve_secret()
MAX_KEY_BYTES = 8 * 1024  # a capability key is a line, not a document

_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# --------------------------------------------------------------------------- #
# the closed vocabulary
# --------------------------------------------------------------------------- #
#
# Resource:action. One capability per *decision* a caller can make — not one per
# endpoint — so the map stays readable and a new endpoint either reuses an
# existing grant or forces a deliberate new capability (added here, in the
# kernel). The API's route map (orchestration/api.py) is the only consumer.

CAPABILITY_READ = "factory:read"                 # any non-mutating view (engine-wide)
CAPABILITY_DOMAIN_READ = "domain:read"           # read one sector's /v1 surface
CAPABILITY_CAPSULE_APPROVE = "capsule:approve"   # human sign-off on a deliverable
CAPABILITY_CAPSULE_REJECT = "capsule:reject"     # send a deliverable back for rework
CAPABILITY_CAPSULE_CREATE = "capsule:create"     # spawn a capsule directly
CAPABILITY_CAPSULE_BUILD = "capsule:build"       # assemble a post bundle
CAPABILITY_GATE_APPROVE = "gate:approve"         # approve a needs_gate entry
CAPABILITY_GATE_REJECT = "gate:reject"           # reject a needs_gate entry
CAPABILITY_TICKET_CREATE = "ticket:create"       # raise work into a branch
CAPABILITY_TICKET_SETTLE = "ticket:settle"       # pull gates into dispatchable work
CAPABILITY_DOMAIN_CREATE = "domain:create"       # create a business branch
CAPABILITY_PULSE_RUN = "pulse:run"               # advance the factory one pass
CAPABILITY_ENGINE_BEAT = "engine:beat"           # run the whole heartbeat turn
CAPABILITY_QUEUE_MANAGE = "queue:manage"         # schedule/cancel/drain queued work
CAPABILITY_IDEATION_RUN = "ideation:run"         # trigger the auto-ideator
CAPABILITY_INDEX_REBUILD = "index:rebuild"         # rebuild the read-only index
CAPABILITY_TOOLS_INVOKE = "tools:invoke"         # dev tools (QA inspect, image test)
CAPABILITY_FINANCE_CONTROL = "finance:control"   # flip the spend kill switch

ALL_CAPABILITIES: frozenset[str] = frozenset(
    {
        CAPABILITY_READ,
        CAPABILITY_DOMAIN_READ,
        CAPABILITY_CAPSULE_APPROVE,
        CAPABILITY_CAPSULE_REJECT,
        CAPABILITY_CAPSULE_CREATE,
        CAPABILITY_CAPSULE_BUILD,
        CAPABILITY_GATE_APPROVE,
        CAPABILITY_GATE_REJECT,
        CAPABILITY_TICKET_CREATE,
        CAPABILITY_TICKET_SETTLE,
        CAPABILITY_DOMAIN_CREATE,
        CAPABILITY_PULSE_RUN,
        CAPABILITY_ENGINE_BEAT,
        CAPABILITY_QUEUE_MANAGE,
        CAPABILITY_IDEATION_RUN,
        CAPABILITY_INDEX_REBUILD,
        CAPABILITY_TOOLS_INVOKE,
        CAPABILITY_FINANCE_CONTROL,
    }
)

# The explicit root grant. Intended for exactly one key: the bootstrap key
# derived from the legacy shared token during migration. A root key satisfies
# every capability check, so it is a deliberate, auditable exception — never a
# default, and never implicit.
CAPABILITY_ALL = "*"

KNOWN_CAPABILITIES: frozenset[str] = ALL_CAPABILITIES | {CAPABILITY_ALL}

# --------------------------------------------------------------------------- #
# resource scoping
# --------------------------------------------------------------------------- #
#
# Some decisions act *on a branch*: approving a deliverable, rejecting one,
# opening work in a sector. For those, a capability that names only the verb is
# not enough — it says "may approve" without saying *what*. Those capabilities
# are SCOPED, and a grant for one must carry a resource:
#
#     capsule:approve:marketing      this branch only
#     capsule:approve:*              every branch, declared out loud
#     capsule:approve                impossible to construct (see Grant)
#
# Everything else is a decision about the *engine* — running a pulse, reading
# the factory, creating a branch — and a resource would be meaningless.

SCOPE_ALL = "*"

SCOPED_CAPABILITIES: frozenset[str] = frozenset(
    {
        CAPABILITY_DOMAIN_READ,
        CAPABILITY_CAPSULE_APPROVE,
        CAPABILITY_CAPSULE_REJECT,
        CAPABILITY_CAPSULE_CREATE,
        CAPABILITY_GATE_APPROVE,
        CAPABILITY_GATE_REJECT,
        CAPABILITY_TICKET_CREATE,
    }
)

GLOBAL_CAPABILITIES: frozenset[str] = ALL_CAPABILITIES - SCOPED_CAPABILITIES

if SCOPED_CAPABILITIES | GLOBAL_CAPABILITIES != ALL_CAPABILITIES:  # pragma: no cover
    raise CapabilityError(
        "capabilities: the scoped/global split does not cover the vocabulary"
    )


def _valid_scope(value: object) -> str | None:
    """Accept a resource name, or ``None`` when it is unusable.

    Scopes are free-form on purpose: branches are created at runtime, so a
    scope cannot be checked against a fixed list. A typo therefore grants
    nothing (fail closed) rather than being rejected at mint time. What *is*
    rejected is a scope that is empty, whitespace, or carries another
    separator — those are malformed, not misspelled.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or ":" in text or any(ch.isspace() for ch in text):
        return None
    return text


@dataclass(frozen=True)
class Grant:
    """One capability grant: a verb, plus the resource it is confined to.

    The type enforces the grammar, which is what closes the hole: a scoped
    capability with no scope cannot be *represented*, so it can never be
    minted, verified, stored, or silently downgraded into "every branch".
    """

    capability: str
    scope: str | None = None

    def __post_init__(self) -> None:
        if self.capability == CAPABILITY_ALL:
            if self.scope is not None:
                raise CapabilityError("the root grant (*) cannot carry a scope")
            return
        if self.capability in SCOPED_CAPABILITIES:
            if self.scope is None:
                raise CapabilityError(
                    f"capability {self.capability!r} acts on a branch and needs an "
                    f"explicit scope: write '{self.capability}:<domain>' for one "
                    f"branch, or '{self.capability}:{SCOPE_ALL}' for every branch. "
                    "A scoped capability with no scope grants nothing, so it is "
                    "refused rather than accepted as universal."
                )
            if _valid_scope(self.scope) is None:
                raise CapabilityError(
                    f"scope {self.scope!r} for {self.capability!r} is malformed "
                    "(must be non-empty, without ':' or whitespace)"
                )
            return
        if self.capability in GLOBAL_CAPABILITIES:
            if self.scope is not None:
                raise CapabilityError(
                    f"capability {self.capability!r} is engine-wide and takes no "
                    f"scope (got {self.scope!r})"
                )
            return
        known = ", ".join(sorted(KNOWN_CAPABILITIES))
        raise CapabilityError(
            f"unknown capability {self.capability!r} — refusing to grant it "
            f"(known: {known})"
        )

    @property
    def text(self) -> str:
        """The on-the-wire form: ``capsule:approve:marketing`` or ``factory:read``."""
        return f"{self.capability}:{self.scope}" if self.scope else self.capability

    @property
    def covers_every_resource(self) -> bool:
        """True when this grant is ``*``-scoped (or engine-wide)."""
        return self.capability == CAPABILITY_ALL or self.scope in (None, SCOPE_ALL)

    def as_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "scope": self.scope}

    @classmethod
    def parse(cls, text: object) -> "Grant":
        """Parse a grant string; raise :class:`CapabilityError` on any breach.

        A bare ``str`` is *not* special-cased here: an empty or whitespace
        value is refused like any other malformed input.
        """
        if not isinstance(text, str) or not text.strip():
            raise CapabilityError("capability grants must be non-empty strings")
        raw = text.strip()
        if raw == CAPABILITY_ALL:
            return cls(CAPABILITY_ALL)
        parts = raw.split(":")
        if len(parts) == 2:
            return cls(f"{parts[0].strip()}:{parts[1].strip()}")
        if len(parts) == 3:
            capability = f"{parts[0].strip()}:{parts[1].strip()}"
            scope = _valid_scope(parts[2])
            if scope is None:
                raise CapabilityError(
                    f"scope {parts[2]!r} in grant {raw!r} is malformed "
                    "(must be non-empty, without ':' or whitespace)"
                )
            return cls(capability, scope)
        raise CapabilityError(
            f"malformed capability grant {raw!r} (expected 'resource:action' or "
            "'resource:action:scope')"
        )


# --------------------------------------------------------------------------- #
# the verified key
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CapabilityKey:
    """One verified capability key (immutable, JSON-friendly).

    Instances exist only after :func:`verify_key` proved the signature, the
    vocabulary, the grammar, and the validity window — an unverified key has no
    object. The raw signed string is deliberately *not* retained: nothing
    downstream can log or re-emit the credential by accident.
    """

    subject: str
    grants: frozenset[Grant]
    issued_at: int
    expires_at: int
    key_id: str

    @property
    def capabilities(self) -> frozenset[str]:
        """The raw grant strings, for audit lines and display."""
        return frozenset(grant.text for grant in self.grants)

    @property
    def is_root(self) -> bool:
        """True when this key holds the explicit root grant."""
        return any(grant.capability == CAPABILITY_ALL for grant in self.grants)

    def has(self, capability: str, *, resource: str | None = None) -> bool:
        """True when this key authorises ``capability`` on ``resource``.

        Precision matters here, so the three cases are kept distinct:

        - the root grant authorises anything, on any resource;
        - an engine-wide capability ignores ``resource`` entirely;
        - a scoped grant must *cover* the resource — and asking a scoped
          capability about no resource at all is a refusal, because "which
          branch?" is exactly the question the grant exists to answer.
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
                return True  # engine-wide: the resource is irrelevant
            if resource is not None and grant.scope in (SCOPE_ALL, resource):
                return True
        return False

    def has_any_scope(self, capability: str) -> bool:
        """True when the *verb* is granted somewhere, on any resource.

        The coarse question the middleware asks before a route runs: a caller
        who does not hold the verb in any scope can be rejected immediately,
        without the expensive resource lookup. Holding it somewhere is not
        permission — the endpoint still proves the resource.
        """
        if not isinstance(capability, str) or not capability:
            return False
        if capability == CAPABILITY_ALL:
            return self.is_root
        return any(
            grant.capability in (CAPABILITY_ALL, capability) for grant in self.grants
        )

    def audit_note(self) -> str:
        """A short, log-safe label for history/audit lines.

        Names the holder and the key — never the credential material — so a
        decision recorded on disk says *which key* signed it, which is exactly
        what the legacy shared token could not tell us.
        """
        return f"{self.subject} (capability key {self.key_id[:8]}…)"

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly claims (no signature, no secret)."""
        return {
            "subject": self.subject,
            "capabilities": sorted(self.capabilities),
            "grants": [g.as_dict() for g in sorted(self.grants, key=lambda g: g.text)],
            "is_root": self.is_root,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "key_id": self.key_id,
        }


# --------------------------------------------------------------------------- #
# secret resolution
# --------------------------------------------------------------------------- #

def resolve_secret(secret: str | bytes | None = None) -> bytes | None:
    """Resolve the signing secret: explicit argument > env var > ``None``.

    Returns ``None`` when nothing is configured — callers must treat that as a
    refusal (fail closed), never as "no check". An empty or whitespace-only
    value is treated as unconfigured, because an empty HMAC key is a trap: it
    signs happily and protects nothing.
    """
    if secret is None:
        raw = os.environ.get(CAPABILITY_ENV, "")
        if not isinstance(raw, str):  # pragma: no cover - os.environ is str->str
            return None
        return raw.encode("utf-8") if raw.strip() else None
    if isinstance(secret, bytes):
        return secret or None
    if isinstance(secret, str):
        return secret.encode("utf-8") if secret.strip() else None
    raise CapabilityError(
        f"secret must be str or bytes, got {type(secret).__name__}"
    )


# --------------------------------------------------------------------------- #
# encoding primitives
# --------------------------------------------------------------------------- #

def _b64encode(raw: bytes) -> str:
    """Base64url without padding (URL/header/copy-paste safe)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str, *, field: str) -> bytes:
    """Strict base64url decode: refuse any character outside the alphabet."""
    if not _B64_RE.match(value):
        raise CapabilityError(f"capability key {field} is not base64url")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:  # binascii.Error is a ValueError
        raise CapabilityError(f"capability key {field} is not decodable: {exc}") from exc


def _sign(secret: bytes, payload_bytes: bytes) -> bytes:
    """HMAC-SHA256 over the exact payload bytes that travel in the key."""
    return hmac.new(secret, payload_bytes, hashlib.sha256).digest()


def _int_field(payload: Mapping[str, Any], name: str) -> int:
    """Read a required integer claim; bools and floats are refused."""
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapabilityError(f"capability key claim {name!r} must be an integer")
    return value


# --------------------------------------------------------------------------- #
# minting
# --------------------------------------------------------------------------- #

def _normalize_grants(capabilities: Iterable[str] | str) -> frozenset[Grant]:
    """Validate a grant list into :class:`Grant` objects (the grammar gate).

    A bare ``str`` is refused explicitly: iterating it would silently turn
    ``"capsule:approve:marketing"`` into one grant per *character* — exactly the
    kind of quiet misreading this module exists to prevent. Grammar and
    vocabulary are delegated to :meth:`Grant.parse`, so there is one rulebook.
    """
    if isinstance(capabilities, (str, bytes)):
        raise CapabilityError(
            "capabilities must be an iterable of grant strings, not a bare "
            'string (pass ["capsule:approve:marketing"], or [CAPABILITY_ALL] '
            "for a root key)"
        )
    try:
        items = list(capabilities)
    except TypeError as exc:
        raise CapabilityError(f"capabilities is not iterable: {exc}") from exc
    if not items:
        raise CapabilityError("capabilities must grant at least one capability")
    return frozenset(Grant.parse(item) for item in items)


def mint_key(
    subject: str,
    capabilities: Iterable[str],
    *,
    secret: str | bytes | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | float | None = None,
    key_id: str | None = None,
) -> str:
    """Mint a signed capability key; returns the token string.

    Args:
        subject: who holds the key (recorded in audit lines).
        capabilities: the granted capability names — the closed vocabulary in
            :data:`ALL_CAPABILITIES`, or ``[CAPABILITY_ALL]`` for a root key.
        secret: signing secret; ``None`` falls back to ``AGENCY_CAPABILITY_SECRET``.
        ttl_seconds: validity window in seconds (> 0, <= :data:`MAX_TTL_SECONDS`).
        now: issue time (epoch seconds); defaults to the current time. Tests
            inject it (with ``key_id``) to get byte-identical keys.
        key_id: explicit key identifier; defaults to a random hex id.

    Raises:
        CapabilityError: no/weak secret, empty subject, empty or unknown
            capabilities, or a non-positive/absurd TTL — nothing is minted.
    """
    resolved = resolve_secret(secret)
    if resolved is None:
        raise CapabilityError(
            f"no signing secret configured — set {CAPABILITY_ENV} or pass "
            "secret= (fail closed: refusing to mint an unsigned key)"
        )
    if len(resolved) < MIN_SECRET_BYTES:
        raise CapabilityError(
            f"signing secret is {len(resolved)} bytes; at least "
            f"{MIN_SECRET_BYTES} are required (generate one with: python3 -c "
            '"import secrets; print(secrets.token_urlsafe(48))")'
        )

    if not isinstance(subject, str) or not subject.strip():
        raise CapabilityError("subject must be a non-empty string")

    grants = _normalize_grants(capabilities)

    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise CapabilityError("ttl_seconds must be an integer")
    if ttl_seconds <= 0:
        raise CapabilityError(
            "ttl_seconds must be > 0 — a key that never expires cannot be "
            "minted (fail closed)"
        )
    if ttl_seconds > MAX_TTL_SECONDS:
        raise CapabilityError(
            f"ttl_seconds {ttl_seconds} exceeds the {MAX_TTL_SECONDS}s maximum"
        )

    issued = int(time.time() if now is None else now)
    identifier = key_id if key_id else secrets.token_hex(8)
    if not isinstance(identifier, str) or not identifier.strip():
        raise CapabilityError("key_id must be a non-empty string")

    payload = {
        "caps": sorted(grant.text for grant in grants),
        "exp": issued + ttl_seconds,
        "iat": issued,
        "jti": identifier.strip(),
        "sub": subject.strip(),
        "v": KEY_VERSION,
    }
    payload_bytes = json.dumps(
        payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    signature = _sign(resolved, payload_bytes)
    return f"{TOKEN_PREFIX}.{_b64encode(payload_bytes)}.{_b64encode(signature)}"


def mint_root_key(
    subject: str,
    *,
    secret: str | bytes | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | float | None = None,
    key_id: str | None = None,
) -> str:
    """:func:`mint_key` with the explicit root grant (migration/bootstrap only)."""
    return mint_key(
        subject,
        [CAPABILITY_ALL],
        secret=secret,
        ttl_seconds=ttl_seconds,
        now=now,
        key_id=key_id,
    )


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

def verify_key(
    raw: object,
    *,
    secret: str | bytes | None = None,
    now: int | float | None = None,
    leeway: int = 0,
) -> CapabilityKey:
    """Verify a capability key and return its claims — or refuse.

    The order is the security property: length and shape, then the HMAC over
    the **exact bytes carried in the key**, and only then JSON parsing and
    claim validation. A forged key never reaches the JSON decoder.

    Args:
        raw: the token string as presented by the caller.
        secret: signing secret; ``None`` falls back to the env var.
        now: current time (epoch seconds); defaults to the wall clock.
        leeway: seconds of tolerated clock skew, applied to both bounds
            (a key is not-yet-valid only if ``iat > now + leeway`` and expired
            only if ``now >= exp + leeway``).

    Raises:
        CapabilityError: the key is missing, oversized, misshapen, not
            base64url, has a bad signature, carries a malformed or unknown
            capability, or lies outside its validity window. Every one of
            those is a refusal, never a downgrade.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise CapabilityError("capability key is missing (fail closed)")
    token = raw.strip()
    if len(token.encode("utf-8")) > MAX_KEY_BYTES:
        raise CapabilityError(
            f"capability key exceeds {MAX_KEY_BYTES} bytes — refusing to parse it"
        )

    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise CapabilityError(
            "capability key is malformed (expected "
            f"'{TOKEN_PREFIX}.<payload>.<signature>')"
        )

    payload_bytes = _b64decode(parts[1], field="payload")
    signature = _b64decode(parts[2], field="signature")

    secret_bytes = resolve_secret(secret)
    if secret_bytes is None:
        raise CapabilityError(
            f"no signing secret configured — set {CAPABILITY_ENV} or pass "
            "secret= (fail closed: refusing to verify without a key)"
        )

    expected = _sign(secret_bytes, payload_bytes)
    if not hmac.compare_digest(expected, signature):
        raise CapabilityError("capability key signature is invalid (fail closed)")

    # Only now is the payload trusted enough to be parsed.
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapabilityError(
            f"capability key payload is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise CapabilityError(
            f"capability key payload must be an object, got "
            f"{type(payload).__name__}"
        )

    version = _int_field(payload, "v")
    if version != KEY_VERSION:
        raise CapabilityError(
            f"unsupported capability key version {version} (expected {KEY_VERSION})"
        )

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise CapabilityError("capability key claim 'sub' must be a non-empty string")

    raw_caps = payload.get("caps")
    if not isinstance(raw_caps, list) or not raw_caps:
        raise CapabilityError("capability key claim 'caps' must be a non-empty list")
    # Grammar and vocabulary are enforced per grant, and a single bad grant
    # refuses the WHOLE key. Silently dropping it would let a malformed (or
    # forged) key be used as a weaker, "valid" one — the no-downgrade rule.
    try:
        grants = frozenset(Grant.parse(item) for item in raw_caps)
    except CapabilityError as exc:
        raise CapabilityError(
            f"capability key contains an invalid grant — refusing the whole "
            f"key (fail closed): {exc}"
        ) from exc

    key_id = payload.get("jti")
    if not isinstance(key_id, str) or not key_id.strip():
        raise CapabilityError("capability key claim 'jti' must be a non-empty string")

    issued_at = _int_field(payload, "iat")
    expires_at = _int_field(payload, "exp")

    if isinstance(leeway, bool) or not isinstance(leeway, int) or leeway < 0:
        raise CapabilityError("leeway must be a non-negative integer")
    current = int(time.time() if now is None else now)

    if issued_at > current + leeway:
        raise CapabilityError(
            f"capability key is not yet valid (issued_at {issued_at}, now {current})"
        )
    if current >= expires_at + leeway:
        raise CapabilityError(
            f"capability key expired at {expires_at} (now {current}) — fail closed"
        )

    return CapabilityKey(
        subject=subject.strip(),
        grants=grants,
        issued_at=issued_at,
        expires_at=expires_at,
        key_id=key_id.strip(),
    )


# --------------------------------------------------------------------------- #
# convenience
# --------------------------------------------------------------------------- #

def has_capability(
    key: CapabilityKey | None, capability: str, *, resource: str | None = None
) -> bool:
    """True only when ``key`` is verified and grants ``capability`` on ``resource``.

    ``None`` (no credential at all) is a refusal, never an implicit pass — so a
    caller can write ``if not has_capability(key, CAP, resource=branch)`` and be
    correct whether or not a key was presented. For a scoped capability, pass
    the resource; omitting it is a refusal.
    """
    return isinstance(key, CapabilityKey) and key.has(capability, resource=resource)
