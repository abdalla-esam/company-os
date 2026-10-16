#!/usr/bin/env python3
"""review_hash.py — the canonical SHA-256 of exactly what a human reviewed.

The ``/v1`` decision contract binds a human decision to the *bytes they saw*.
For a capsule that binding is a path -> sha256 map over the gate's bound
artifacts (Slice 1). Slice 2 extends the same shape to the two other small
human decisions in the flow: promoting/demoting a **ticket** (accept / send
back an idea) and deciding a ``needs_gate`` **gate entry**.

The binding is computed by the server, never sent by the client. To keep the
review endpoint (engine) and the lock that performs the write (``ops/tools``
in-process for tickets, ``gate_script.py`` as a subprocess for gates) in exact
agreement, this module is the single source of truth for:

- **which fields** are reviewed and immutable during review
  (:data:`TICKET_REVIEW_FIELDS`, :data:`GATE_REVIEW_FIELDS`);
- **how** a value is canonicalized into a digest — sorted keys, UTF-8,
  compact separators, so JSON key order and whitespace can never change the
  number a reviewer attests to;
- **how** a client's attested element set is compared with the server's
  current one — set equality (``HASH_SET_MISMATCH``) then per-element
  constant-time digest equality (``STALE_CONTENT_HASH``);
- **the ``reviewed_sha256``** digest of a whole attestation, byte-for-byte the
  same canonical form ``orchestration.approval._finalized_reviewed`` produces
  (a test pins the two equal), so the client-side shape is shared with the
  capsule decision.

Pure standard library on purpose: ``gate_script.py`` runs as a subprocess and
must import this without the engine package chain, exactly as it already does
for :mod:`shared_kernel.atomic_io`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict, Iterable, Mapping, Optional

__all__ = [
    "ALGORITHM",
    "GATE_REVIEW_FIELDS",
    "TICKET_REVIEW_FIELDS",
    "ReviewHashError",
    "compare_elements",
    "element_sha256",
    "elements_for",
    "gate_review_fields",
    "reviewed_digest",
    "ticket_review_fields",
]

#: The only attestation algorithm this contract accepts (fail closed).
ALGORITHM = "sha256"

#: The ticket fields a human reviews when accepting an idea, and which do not
#: change during review. ``history``/``state``/``stage``/``metadata.timestamp``
#: mutate on promotion (and ``decision`` is written by the settle bridge), so
#: they are deliberately excluded — hashing them would invalidate the
#: reviewer's own decision, the same reason the capsule manifest is not hashed.
TICKET_REVIEW_FIELDS = ("title", "request", "context")

#: The gate fields a human reviews for a ``needs_gate`` decision. ``state``,
#: ``decision``, ``decided_by``, ``decided_at`` and ``decision_reason`` are the
#: fields the decision itself writes; the reviewed source/request, the reason
#: and the risk tier are what the human actually read and must not move.
GATE_REVIEW_FIELDS = ("source", "reason", "risk_tier")


class ReviewHashError(Exception):
    """A fail-closed refusal raised from *inside* the writer's lock.

    Carries the ``/v1`` machine-readable ``code`` (``STALE_CONTENT_HASH`` or
    ``HASH_SET_MISMATCH``) and the HTTP status the API should answer, so a
    writer can refuse without importing the framework or the v1 envelope.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details) if details else None


def _canonical_bytes(value: Any) -> bytes:
    """A value as a stable UTF-8 byte string (sorted keys, no whitespace variance)."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def element_sha256(value: Any) -> str:
    """SHA-256 over the canonical serialization of one reviewed field."""
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def elements_for(fields: Mapping[str, Any]) -> Dict[str, str]:
    """``{field_name: sha256}`` for every reviewed field of an entity."""
    return {name: element_sha256(value) for name, value in fields.items()}


def ticket_review_fields(ticket: Mapping[str, Any]) -> Dict[str, Any]:
    """The three reviewed ticket fields, normalized so review and write agree.

    ``title``/``request`` fall back to the ``metadata``/legacy spellings the
    two ticket writers use; ``context`` is an object (an absent context is the
    empty object, never ``null``), so both sides always hash the same shape.
    """
    metadata = ticket.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    title = ticket.get("title")
    if title is None:
        title = metadata.get("title")
    request = ticket.get("request")
    if request is None:
        request = ticket.get("content")
    return {
        "title": title if title is not None else "",
        "request": request if request is not None else "",
        "context": ticket.get("context") or {},
    }


def gate_review_fields(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """The three reviewed gate fields (source/request, reason, risk tier)."""
    return {
        "source": entry.get("source"),
        "reason": entry.get("reason"),
        "risk_tier": entry.get("risk_tier"),
    }


def compare_elements(
    expected: Optional[Mapping[str, str]],
    actual: Mapping[str, str],
) -> Dict[str, str]:
    """Compare a client's attested elements with the server's current ones.

    Runs inside the writer's lock. Returns the normalized (lower-cased)
    expected map on success; raises :class:`ReviewHashError` on any mismatch:

    - a missing or extra *field* is a set mismatch (``HASH_SET_MISMATCH``);
    - a present field whose digest differs is stale content
      (``STALE_CONTENT_HASH``) — a byte changed after the review.

    Comparing element *names* first means a client that attests to the wrong
    surface gets a distinct, actionable refusal from one attesting to the
    right surface with an out-of-date digest.
    """
    normalized: Dict[str, str] = {}
    for key, value in (expected or {}).items():
        if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
            raise ReviewHashError(
                "HASH_SET_MISMATCH",
                "Attested elements must map field names to non-empty sha256 strings.",
                details={"expected": sorted(map(str, (expected or {}).keys()))},
            )
        normalized[key] = value.strip().lower()

    current = dict(actual)
    expected_names = set(normalized)
    current_names = set(current)
    if expected_names != current_names:
        raise ReviewHashError(
            "HASH_SET_MISMATCH",
            "Hash set mismatch: the attested fields are not exactly the fields "
            "the server reviews (missing or extra names).",
            details={
                "expected": sorted(current_names),
                "current": sorted(expected_names),
                "missing": sorted(current_names - expected_names),
                "extra": sorted(expected_names - current_names),
            },
        )

    mismatched: Dict[str, Dict[str, str]] = {}
    for name in sorted(current_names):
        if not hmac.compare_digest(current[name], normalized[name]):
            mismatched[name] = {
                "expected": current[name],
                "current": normalized[name],
            }
    if mismatched:
        raise ReviewHashError(
            "STALE_CONTENT_HASH",
            "Stale content: one or more reviewed fields changed after review "
            "(sha256 mismatch).",
            details={"mismatched": mismatched},
        )
    return normalized


def reviewed_digest(
    *,
    elements: Mapping[str, str],
    algorithm: str = ALGORITHM,
    unviewed: Iterable[Any] = (),
    primary_sha256: Optional[str] = None,
) -> str:
    """The canonical digest of a whole attestation (the ``reviewed_sha256``).

    Deliberately identical to ``orchestration.approval._finalized_reviewed``
    so the ``/v1`` client can treat a capsule attestation and a ticket/gate
    attestation the same way; ``test_v1_ticket_gate_decisions`` pins the two
    implementations equal.
    """
    canonical = json.dumps(
        {
            "algorithm": algorithm,
            "primary_sha256": primary_sha256,
            "elements": dict(elements or {}),
            "unviewed": list(unviewed or []),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
