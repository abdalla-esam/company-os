#!/usr/bin/env python3
"""copy_signoff.py — the copy sign-off service (single source of truth).

The design stage is the first step that spends on a generated image, and the
copy is the artifact that decides whether the post is worth making at all. A
domain can therefore require a person to read the words first: the QA worker
records the ask as it routes to design, the router holds the capsule at
``pending_design`` while that ask is open, and this module records the answer.

Every caller goes through :func:`apply` — the HTTP route the app's button hits
(``POST /api/capsules/{id}/copy_signoff``) and the
``ops/tools/post_cycle_trial.py`` runbook, which signs off as its reviewer so an
automated cycle can still finish. The route is a thin wrapper: identity
extraction, the branch permission check and response formatting stay there;
the decision — the state guard, the digest binding, the ``feedback.txt``
write and the state transition — lives here and nowhere else.

Contract (fail closed):

- one :class:`CapsuleLock`, one manifest, one save per decision;
- only a capsule actually parked at ``pending_design`` with an **awaiting**
  record can be decided: a recorded decision is final, and nothing rewrites
  an answer that already exists;
- **anti-TOCTOU**: the record binds ``copy_sha256`` at the moment the ask is
  created. A copy edited afterwards no longer matches, and the decision is
  refused rather than approving words nobody read;
- **approving** leaves the capsule where it is (the router dispatches design
  on the next pass) and writes one history entry; **rejecting** needs a
  reason, writes ``feedback.txt`` and takes the ordinary
  ``pending_design -> needs_revision`` edge, so the author is reached by the
  same rework path a QA rejection uses.

Nothing here knows about HTTP: deliberate refusals raise
:class:`CopySignoffRefused` carrying the status the route should answer, while
domain errors (``LockTimeoutError``, ``StateTransitionError``,
``CapsuleError``) propagate so callers keep the error mapping they have today.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from capsule_engine import State
from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import (
    ARTIFACT_FILE,
    append_history,
    load_manifest,
    save_manifest,
    transition,
)

#: The record's open state: the ask exists and nobody has answered it yet.
AWAITING = "awaiting"

#: The two lawful answers.
DECISIONS = ("approved", "rejected")


class CopySignoffRefused(Exception):
    """A deliberate, fail-closed refusal with the HTTP status to answer.

    Raised only by this service; the route converts it into its
    ``HTTPException`` so response shapes and status codes stay exactly where
    they were.
    """

    def __init__(self, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class CopySignoffOutcome:
    """What the decision did, for the caller to report."""

    capsule_id: str
    decision: str
    decided_by: str
    decided_at: str
    new_state: str
    copy_sha256: str


def record_of(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """The capsule's copy sign-off record, or an empty dict when it has none."""
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    record = metadata.get("copy_signoff")
    return record if isinstance(record, dict) else {}


def is_awaiting(manifest: Mapping[str, Any]) -> bool:
    """Whether this capsule is parked waiting for a copy sign-off decision."""
    return record_of(manifest).get("decision") == AWAITING


def requested_digest(manifest: Mapping[str, Any]) -> str:
    """The copy digest bound when the sign-off was requested."""
    return str(record_of(manifest).get("copy_sha256") or "")


def apply(
    capsule_path: Path,
    *,
    decision: str,
    reviewer: str,
    reason: str = "",
    audit_note: str = "",
) -> CopySignoffOutcome:
    """Record ``reviewer``'s decision on the copy.

    Returns what the decision did. Approving releases the design hold without
    moving the capsule; rejecting returns it to the author.

    Raises:
        CopySignoffRefused: the request is malformed, the capsule owes no
            sign-off, the ask was already answered, or the copy changed since
            it was asked about.
    """
    decision = (decision or "").strip().lower()
    if decision not in DECISIONS:
        raise CopySignoffRefused(
            "decision must be 'approved' or 'rejected'", status_code=400
        )
    reason = (reason or "").strip()
    if decision == "rejected" and not reason:
        raise CopySignoffRefused(
            "a rejection needs a reason for the author", status_code=400
        )

    with CapsuleLock(capsule_path):
        manifest = load_manifest(capsule_path)
        state = manifest.get("state")
        if state != State.PENDING_DESIGN.value:
            raise CopySignoffRefused(
                "no copy sign-off is pending: the capsule is "
                f"{state!r}, and a sign-off is only owed at "
                f"{State.PENDING_DESIGN.value!r}",
                status_code=400,
            )
        if not is_awaiting(manifest):
            raise CopySignoffRefused(
                "the copy sign-off is not awaiting a decision", status_code=409
            )

        copy_file = capsule_path / ARTIFACT_FILE
        if not copy_file.is_file():
            raise CopySignoffRefused(
                "the copy is missing from the capsule", status_code=409
            )
        live_digest = hashlib.sha256(copy_file.read_bytes()).hexdigest()
        bound_digest = requested_digest(manifest)
        if bound_digest and bound_digest != live_digest:
            raise CopySignoffRefused(
                "the copy changed after the sign-off was requested (TOCTOU) "
                "— read it again before deciding",
                status_code=409,
            )

        decided_at = datetime.now(timezone.utc).isoformat()
        note = audit_note or "copy sign-off"
        # The record holds a *decision*, not a capsule state: the capsule's own
        # state only ever moves through ``transition`` below.
        record = record_of(manifest)
        record["decision"] = decision
        record["decided_by"] = reviewer
        record["decided_at"] = decided_at
        record["reason"] = reason
        record["copy_sha256"] = live_digest
        manifest["metadata"]["copy_signoff"] = record

        if decision == "rejected":
            (capsule_path / "feedback.txt").write_text(
                f"{reason}\n", encoding="utf-8"
            )
            transition(
                manifest,
                State.NEEDS_REVISION,
                by=reviewer,
                why=f"copy sign-off rejected: {reason} — {note}",
                capsule_path=capsule_path,
            )
        else:
            append_history(
                manifest,
                by=reviewer,
                why=f"copy signed off: {note}",
                from_state=State.PENDING_DESIGN,
                to_state=State.PENDING_DESIGN,
            )

        save_manifest(capsule_path, manifest)
        new_state = manifest.get("state", state)

    return CopySignoffOutcome(
        capsule_id=capsule_path.name,
        decision=decision,
        decided_by=reviewer,
        decided_at=decided_at,
        new_state=str(new_state),
        copy_sha256=live_digest,
    )
