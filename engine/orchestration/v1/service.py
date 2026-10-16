#!/usr/bin/env python3
"""service.py — the shared, attested decision service (ticket + gate).

Slice 2 of the ``/v1`` mobile contract brings the two other small human
decisions in the flow under the same rules a capsule decision already follows:

- **review** (``GET /v1/tickets/{id}/review`` / ``GET /v1/gates/{id}/review``)
  is a pure read: it reports the server's *current* ``{field: sha256}`` binding
  and writes nothing, so the hashes a client echoes back are trustworthy;
- **decision** (``POST .../decision``) verifies that echo **inside the lock that
  performs the write** — the ticket's sidecar lock for a ticket, the
  ``gate_script.py`` subprocess lock for a gate — and fails closed with
  ``STALE_CONTENT_HASH`` / ``HASH_SET_MISMATCH`` when a reviewed byte moved
  after review. A send-back / rejection carries a reason, never a hash.

This module is the single place the two routes reach those writes: the router
shapes HTTP, this service owns identity, scope, hashing and idempotency, and the
writers own the atomic, in-lock verification. The reviewed fields and the
canonical digest are shared with the subprocess via
:mod:`shared_kernel.review_hash`, so the value hashed at review and the value
checked at write can never disagree.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from orchestration.v1.errors import V1Error
from shared_kernel import review_hash

#: The flow's action vocabulary, mapped to the two write verbs. A ticket
#: promote is "accept the idea"; a demote is "send it back". A gate is
#: approved or rejected. Anything else is a validation refusal.
_TICKET_PROMOTE = frozenset({"promote", "accept", "approve"})
_TICKET_DEMOTE = frozenset({"demote", "reject", "send_back", "send-back"})
_GATE_APPROVE = frozenset({"approve"})
_GATE_REJECT = frozenset({"reject"})

#: Gate-script refusal codes the subprocess prints to stderr, in match order.
_GATE_REFUSAL_CODES = (
    "STALE_CONTENT_HASH",
    "HASH_SET_MISMATCH",
    "IDEMPOTENCY_CONFLICT",
    "ALREADY_DECIDED",
)


def _api():
    """The orchestration api module, imported lazily (it mounts this package)."""
    from orchestration import api

    return api


def _approval():
    """The capsule decision service — reused for its canonical hashing/identity."""
    from orchestration import approval

    return approval


def _access_control():
    from orchestration import access_control

    return access_control


def _ticket_mgr():
    """The ticket writer, imported the same way ``api`` imports it."""
    try:
        from ops.tools import ticket_mgr
    except ImportError:  # pragma: no cover - repository path variant
        from tools import ticket_mgr

    return ticket_mgr


def _require_scope(principal: Any, capability: str, resource: str) -> None:
    """The fine, branch-scoped check (the middleware only checked the verb)."""
    denial = _access_control().scope_denial(principal, capability, resource)
    if denial:
        raise V1Error(
            403,
            denial,
            code="INSUFFICIENT_SCOPE",
            details={"capability": capability, "resource": resource},
        )


def _normalized_reviewed(reviewed: Optional[Any]) -> Optional[Dict[str, Any]]:
    """Validate/canonicalize the client's ``reviewed`` payload (fail closed)."""
    if reviewed is None:
        return None
    approval = _approval()
    try:
        return approval._normalize_attestation(reviewed)
    except approval.ApprovalRefused as exc:
        raise V1Error(
            exc.status_code,
            exc.detail,
            code=exc.code or "VALIDATION_ERROR",
            details=exc.details,
            remedy=exc.remedy,
        ) from exc


def _review_block(elements: Dict[str, str]) -> Dict[str, Any]:
    """The shared reviewed-attestation shape (identical to a capsule review)."""
    return {
        "algorithm": review_hash.ALGORITHM,
        "elements": dict(elements),
        "unviewed": [],
        "primary_sha256": None,
    }


def _reviewed_sha256(reviewed: Dict[str, Any]) -> str:
    """The canonical digest of an attestation (shared with the capsule flow)."""
    return review_hash.reviewed_digest(
        elements=reviewed.get("elements") or {},
        algorithm=reviewed.get("algorithm") or review_hash.ALGORITHM,
        unviewed=reviewed.get("unviewed") or [],
        primary_sha256=reviewed.get("primary_sha256"),
    )


# --------------------------------------------------------------------------- #
# tickets
# --------------------------------------------------------------------------- #


def review_ticket(ticket_id: str, sector: Optional[str] = None) -> Dict[str, Any]:
    """Read-only: a ticket's current review surface and element hashes.

    No lock and no write — exactly the capsule-review contract, so the value a
    client posts back is the value the write path will recompute.
    """
    ticket_mgr = _ticket_mgr()
    found = ticket_mgr.find_ticket(ticket_id, sector)
    if not found:
        raise V1Error(404, f"Ticket '{ticket_id}' not found.", code="NOT_FOUND")
    sec, stage, _path, data = found

    elements = review_hash.elements_for(review_hash.ticket_review_fields(data))
    reviewed = _review_block(elements)
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    return {
        "ticket_id": ticket_id,
        "sector": sec,
        "stage": stage,
        "state": data.get("state"),
        "title": data.get("title") or metadata.get("title"),
        "binding": "elements",
        "reviewed": reviewed,
        "reviewed_sha256": _reviewed_sha256(reviewed),
    }


def _ticket_fingerprint(
    verb: str, ticket_id: str, reason: str, actor: str, reviewed: Optional[Dict[str, Any]]
) -> str:
    return _approval()._decision_fingerprint(
        verb, ticket_id, reason, actor, reviewed or {}
    )


def _recover_moved_ticket(
    ticket_mgr: Any,
    ticket_id: str,
    sector: Optional[str],
    verb: str,
    decision: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Classify a decision whose ticket another writer just moved (read-only).

    A phone double-tap sends two v1 decisions for one scratch ticket. Both
    resolve the ticket *before* either write, so the loser's atomic
    ``update_json`` finds the source gone and the writer raises "not found" —
    even though the ticket is not gone, it moved. Re-resolving it where it now
    lives and reproducing the writer's own classification closes that gap:

    - the same ``idempotency_key`` with the same request fingerprint replays the
      original outcome (``replayed: true``);
    - the same key with a different payload is ``IDEMPOTENCY_CONFLICT``;
    - any other decision (another key, or another actor) is
      ``ALREADY_DECIDED`` naming who decided and what stands.

    This reads and never writes, so the losing tap can never become a second
    move, and it takes no lock — the winner's atomic write is already complete
    before the loser's ``update_json`` reports the source gone. Returns ``None``
    only when the ticket is genuinely absent everywhere (the caller answers
    404).
    """
    found = ticket_mgr.find_ticket(ticket_id, sector) or ticket_mgr.find_ticket(
        ticket_id, None
    )
    if not found:
        return None
    current_sector, _stage, _path, state = found
    if not isinstance(state, dict):
        return None

    key = decision.get("idempotency_key")
    prior = ticket_mgr._find_decision_by_key(state, key)
    if prior is not None:
        if prior.get("fingerprint") == decision.get("fingerprint"):
            return {
                "decision_id": prior.get("decision_id"),
                "ticket_id": ticket_id,
                "sector": current_sector or sector,
                "previous_stage": prior.get("stage_before"),
                "new_stage": prior.get("stage_after"),
                "state_before": prior.get("state_before"),
                "state_after": prior.get("state_after"),
                "replayed": True,
                "attested": bool(prior.get("attested")),
                "reviewed_sha256": prior.get("reviewed_sha256"),
                "decided_at": prior.get("at"),
            }
        raise V1Error(
            409,
            "Idempotency key reused with a different request payload",
            code="IDEMPOTENCY_CONFLICT",
            details={"idempotency_key": key},
        )

    try:
        ticket_mgr._already_decided(
            state,
            ticket_id=ticket_id,
            action=verb,
            verb="promoted" if verb == "promote" else "demoted",
        )
    except ticket_mgr.TicketDecisionError as exc:
        raise V1Error(
            exc.status, str(exc), code=exc.code, details=exc.details
        ) from exc
    return None  # pragma: no cover - _already_decided always raises


def decide_ticket(ticket_id: str, body: Any, *, principal: Any) -> Dict[str, Any]:
    """Accept (promote) or send back (demote) a ticket — one fail-closed path."""
    ticket_mgr = _ticket_mgr()

    action = (getattr(body, "action", "") or "").strip().lower()
    if action in _TICKET_PROMOTE:
        verb = "promote"
    elif action in _TICKET_DEMOTE:
        verb = "demote"
    else:
        raise V1Error(
            400,
            f"action must be promote or demote (got {action!r})",
            code="VALIDATION_ERROR",
        )

    found = ticket_mgr.find_ticket(ticket_id, None)
    if not found:
        raise V1Error(404, f"Ticket '{ticket_id}' not found.", code="NOT_FOUND")
    sec = found[0]
    _require_scope(principal, "ticket:create", sec)

    reviewer = principal.subject
    normalized: Optional[Dict[str, Any]] = None
    expected: Optional[Dict[str, str]] = None

    if verb == "demote":
        reason = (getattr(body, "reason", "") or "").strip()
        if not reason:
            raise V1Error(
                400, "A send-back requires a reason", code="VALIDATION_ERROR"
            )
    else:
        if getattr(body, "reviewed", None) is None:
            raise V1Error(
                400,
                "Attestation is mandatory on this route: reviewed.elements (the "
                "sha256 of what the reviewer saw) must accompany an accept.",
                code="ATTESTATION_REQUIRED",
            )
        normalized = _normalized_reviewed(body.reviewed.model_dump())
        expected = dict((normalized or {}).get("elements") or {})
        reason = (getattr(body, "reason", "") or "").strip()

    decision = {
        "action": verb,
        "actor": reviewer,
        "audit_note": principal.audit_note(),
        "reason": reason,
        "idempotency_key": getattr(body, "idempotency_key", None),
        "fingerprint": _ticket_fingerprint(verb, ticket_id, reason, reviewer, normalized),
        "expected_elements": expected,
        "reviewed": normalized,
    }

    try:
        if verb == "promote":
            outcome = ticket_mgr.promote_ticket(ticket_id, sec, decision=decision)
        else:
            outcome = ticket_mgr.demote_ticket(ticket_id, sec, decision=decision)
    except review_hash.ReviewHashError as exc:
        raise V1Error(
            exc.status, str(exc), code=exc.code, details=exc.details
        ) from exc
    except ticket_mgr.TicketDecisionError as exc:
        raise V1Error(
            exc.status, str(exc), code=exc.code, details=exc.details
        ) from exc
    except ValueError as exc:
        # The writer found the source gone: a concurrent decision moved the
        # ticket between our resolve and its atomic update. Classify it where it
        # now lives (without a second write) before concluding "not found".
        recovered = _recover_moved_ticket(ticket_mgr, ticket_id, sec, verb, decision)
        if recovered is None:
            raise V1Error(404, str(exc), code="NOT_FOUND") from exc
        outcome = recovered

    return {
        "decision_id": outcome.get("decision_id"),
        "ticket_id": outcome.get("ticket_id") or ticket_id,
        "sector": outcome.get("sector") or sec,
        "action": verb,
        "stage_before": outcome.get("previous_stage"),
        "stage_after": outcome.get("new_stage"),
        "state_before": outcome.get("state_before"),
        "state_after": outcome.get("state_after"),
        "replayed": bool(outcome.get("replayed")),
        "attested": bool(outcome.get("attested")),
        "reviewed_sha256": outcome.get("reviewed_sha256"),
        "decided_at": outcome.get("decided_at"),
    }


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #


def _gate_entry_or_empty(gate_id: str) -> Dict[str, Any]:
    api = _api()
    path = api.GATE_QUEUE_DIR / f"gate_{gate_id}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return entry if isinstance(entry, dict) else {}


def _load_gate_entry(gate_id: str) -> Dict[str, Any]:
    entry = _gate_entry_or_empty(gate_id)
    if not entry:
        raise V1Error(
            404, f"Gate '{gate_id}' not found or unreadable.", code="NOT_FOUND"
        )
    return entry


def review_gate(gate_id: str) -> Dict[str, Any]:
    """Read-only: a gate entry's current review surface and element hashes."""
    entry = _load_gate_entry(gate_id)
    elements = review_hash.elements_for(review_hash.gate_review_fields(entry))
    reviewed = _review_block(elements)
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    return {
        "gate_id": gate_id,
        "sector": source.get("sector"),
        "state": entry.get("state"),
        "risk_tier": entry.get("risk_tier"),
        "reason": entry.get("reason"),
        "source": entry.get("source"),
        "binding": "elements",
        "reviewed": reviewed,
        "reviewed_sha256": _reviewed_sha256(reviewed),
    }


def _raise_gate_refusal(gate_id: str, stderr: str) -> None:
    """Map a coded ``gate_script`` refusal onto the ``/v1`` envelope."""
    err = (stderr or "").strip()
    code = next((token for token in _GATE_REFUSAL_CODES if token in err), None)
    text = err
    if text.startswith("decide failed:"):
        text = text[len("decide failed:") :].strip()
    if code and text.startswith(code + ":"):
        text = text[len(code) + 1 :].strip()
    if not text:
        text = "the gate decision was refused"

    details: Dict[str, Any] = {"gate_id": gate_id}
    if code == "ALREADY_DECIDED":
        entry = _gate_entry_or_empty(gate_id)
        details["decided_by"] = entry.get("decided_by")
        details["decided_at"] = entry.get("decided_at")
    raise V1Error(
        409 if code else 400, text, code=code or "GATE_DECISION_FAILED", details=details
    )


async def decide_gate(gate_id: str, body: Any, *, principal: Any) -> Dict[str, Any]:
    """Approve (attested) or reject (reasoned) a gate — one fail-closed path."""
    api = _api()
    approval = _approval()

    action = (getattr(body, "action", "") or "").strip().lower()
    if action in _GATE_APPROVE:
        decision_word = "approved"
        capability = "gate:approve"
    elif action in _GATE_REJECT:
        decision_word = "rejected"
        capability = "gate:reject"
    else:
        raise V1Error(
            400, f"action must be approve or reject (got {action!r})",
            code="VALIDATION_ERROR",
        )

    # The branch is authoritative from the gate record (the same source the
    # legacy route authorises from); an unreadable gate has no branch and is
    # refused rather than guessed.
    sector = api._gate_sector(gate_id)
    if sector is None:
        raise V1Error(
            404,
            f"Gate '{gate_id}' not found or unreadable — its branch cannot be "
            "resolved, so the decision is refused (fail closed).",
            code="NOT_FOUND",
        )
    _require_scope(principal, capability, sector)

    reviewer = principal.subject
    normalized: Optional[Dict[str, Any]] = None
    expected: Optional[Dict[str, str]] = None

    if decision_word == "rejected":
        reason = (getattr(body, "reason", "") or "").strip()
        if not reason:
            raise V1Error(400, "A rejection requires a reason", code="VALIDATION_ERROR")
    else:
        if getattr(body, "reviewed", None) is None:
            raise V1Error(
                400,
                "Attestation is mandatory on this route: reviewed.elements (the "
                "sha256 of what the reviewer saw) must accompany an approval.",
                code="ATTESTATION_REQUIRED",
            )
        normalized = _normalized_reviewed(body.reviewed.model_dump())
        expected = dict((normalized or {}).get("elements") or {})
        reason = (getattr(body, "reason", "") or "").strip()

    fingerprint = approval._decision_fingerprint(
        decision_word, gate_id, reason, reviewer, normalized or {}
    )

    gate_script = api.GATE_DIR / "gate_script.py"
    cmd = [
        sys.executable,
        str(gate_script),
        "decide",
        "--gate-id",
        gate_id,
        "--by",
        reviewer,
        "--decision",
        decision_word,
        "--v1",
    ]
    if reason:
        cmd += ["--reason", reason]
    if expected is not None:
        cmd += [
            "--expected-elements",
            json.dumps(dict(expected), ensure_ascii=False, sort_keys=True),
        ]
    if getattr(body, "idempotency_key", None):
        cmd += ["--idempotency-key", body.idempotency_key]
    if fingerprint:
        cmd += ["--fingerprint", fingerprint]

    try:
        rc, out, err = await api._run_subprocess(cmd)
    except api.ScriptTimeoutError as exc:
        raise V1Error(504, f"Gate decision timed out: {exc}", code="UNAVAILABLE") from exc
    except V1Error:
        raise
    except Exception as exc:
        raise V1Error(
            500, f"Gate decision error: {exc}", code="INTERNAL_ERROR"
        ) from exc

    if rc != 0:
        _raise_gate_refusal(gate_id, err)

    try:
        payload = json.loads(out)
    except ValueError as exc:
        raise V1Error(
            500, f"Gate decision produced no readable result: {exc}",
            code="INTERNAL_ERROR",
        ) from exc

    entry = payload.get("entry") if isinstance(payload, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    evidence = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}
    return {
        "decision_id": evidence.get("decision_id"),
        "gate_id": gate_id,
        "sector": sector,
        "action": "approve" if decision_word == "approved" else "reject",
        "decision": entry.get("decision"),
        "state_before": "open",
        "state_after": entry.get("state"),
        "decided_by": entry.get("decided_by"),
        "reason": entry.get("decision_reason"),
        "replayed": bool(payload.get("replayed")) if isinstance(payload, dict) else False,
        "attested": bool(evidence.get("attested")),
        "reviewed_sha256": evidence.get("reviewed_sha256"),
        "decided_at": entry.get("decided_at"),
    }


__all__ = [
    "decide_gate",
    "decide_ticket",
    "review_gate",
    "review_ticket",
]
