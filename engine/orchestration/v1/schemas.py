#!/usr/bin/env python3
"""schemas.py — the request bodies of the ``/v1`` decision contract.

Two shapes, and the split is deliberate: the *attestation* is what the
reviewer saw (a server-computed SHA-256 per element), and the *decision* is
what they chose. A rejection carries a reason, never an attestation.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class ReviewedAttestation(BaseModel):
    """What the reviewer saw, hashed by the server (echoed back by the client).

    ``elements`` maps a capsule-relative path to the SHA-256 the review
    endpoint reported for it. ``unviewed`` is recorded for audit but never
    relaxes the equality check: the attested element set must still equal the
    gate's bound set exactly. ``primary_sha256`` is the capsule-level digest,
    used only when there is no element binding to compare against.
    """

    algorithm: str = Field(
        "sha256",
        description="Only 'sha256' is accepted; a mismatch is refused (fail closed).",
    )
    elements: Dict[str, str] = Field(
        default_factory=dict,
        description="Capsule-relative path -> SHA-256 the reviewer was shown.",
    )
    unviewed: List[str] = Field(
        default_factory=list,
        description="Paths the reviewer did not open (audit only; never relaxes equality).",
    )
    primary_sha256: Optional[str] = Field(
        None,
        description="Capsule-primary digest; required only when there is no element binding.",
    )


class DecisionRequest(BaseModel):
    """One ``/v1/decisions`` request: approve (attested) or reject (reasoned)."""

    capsule_id: str = Field(..., description="The capsule to decide on (name only).")
    action: str = Field(
        "approve", description="'approve' or 'reject' (case-insensitive)."
    )
    reason: str = Field(
        "", description="Reviewer instruction; mandatory for a rejection."
    )
    reviewed: Optional[ReviewedAttestation] = Field(
        None,
        description=(
            "Mandatory on approve: the hash of exactly what the reviewer saw. "
            "Ignored on reject (a rejection carries a reason, not a hash)."
        ),
    )
    idempotency_key: Optional[str] = Field(
        None,
        max_length=200,
        description="Client replay key: a repeat with the same payload is a no-op.",
    )


class TargetDecisionRequest(BaseModel):
    """One ``/v1`` decision on a named target (a ticket or a gate entry).

    The target id travels in the path, so the body is only the decision: the
    verb, the reason (mandatory when sending back / rejecting), and — for an
    accept / approve — the attestation of exactly what the reviewer saw.

    ``action`` accepts the flow's own vocabulary: ``promote`` / ``demote`` for a
    ticket (``accept`` / ``approve`` alias promote; ``reject`` / ``send_back``
    alias demote) and ``approve`` / ``reject`` for a gate. The service maps it to
    the write verb; an unknown action is refused ``VALIDATION_ERROR``.
    """

    action: str = Field(
        ..., description="promote|demote (or approve|reject) — case-insensitive."
    )
    reason: str = Field(
        "", description="Reviewer instruction; mandatory for a demote/reject."
    )
    reviewed: Optional[ReviewedAttestation] = Field(
        None,
        description=(
            "Mandatory on promote/approve: the hash of exactly what the reviewer "
            "saw. Ignored on demote/reject (a send-back carries a reason, not a hash)."
        ),
    )
    idempotency_key: Optional[str] = Field(
        None,
        max_length=200,
        description="Client replay key: a repeat with the same payload is a no-op.",
    )
