#!/usr/bin/env python3
"""router.py — the ``/v1`` mobile contract: attested decisions, then the reads.

Slice 1 of the mobile contract is deliberately narrow:

``GET /v1/capsules/{capsule_id}/review``
    A **pure read**. It returns what the reviewer is about to see, with the
    server's current SHA-256 for every element the decision will be checked
    against. It takes no lock and writes nothing: no gate is opened, no target
    is bound, no file's mtime moves. That matters because the decision is
    verified against the hashes this endpoint reported — a review that
    mutated the baseline would be attesting to its own edit.

``POST /v1/decisions``
    One decision, attested. ``approve`` **requires** ``reviewed`` (the hash of
    what the reviewer saw); the service recomputes the server's binding inside
    the capsule lock and fails closed on any mismatch — a byte that changed
    after review yields ``STALE_CONTENT_HASH`` and *nothing is written*. A
    ``reject`` carries a reason, never a hash. The decision service
    (:mod:`orchestration.approval`) is the single source of truth; this router
    only shapes the mobile contract around it.

Identity comes from the verified credential the auth middleware resolved —
never from the body — and every refusal is the ``/v1`` error envelope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Request

from orchestration.v1 import envelope as envelope_module
from orchestration.v1 import messages
from orchestration.v1.errors import V1Error
from orchestration.v1.schemas import DecisionRequest, TargetDecisionRequest

router = APIRouter(prefix="/v1", tags=["v1"])

#: The verbs this endpoint understands. ``skip`` is a gate-route verb and is
#: intentionally absent: skipping is not something a mobile decision does.
_ACTIONS: Tuple[str, ...] = ("approve", "reject")


def _deps() -> Tuple[Any, Any]:
    """The api helpers and the decision service, imported lazily.

    Lazy on purpose: imported at module scope this would form a cycle (``api``
    imports this package at its foot to mount it), and the two names are only
    needed when a request is actually served.
    """
    from orchestration import api, approval

    return api, approval


def _clean(capsule_id: str) -> str:
    return Path(capsule_id).name.strip()


def _scope():
    """The sector-visibility helper, imported lazily (same cycle as ``_deps``)."""
    from orchestration.v1 import scope

    return scope


def _reads():
    """The aggregate read model, imported lazily (same cycle as ``_deps``)."""
    from orchestration.v1 import reads

    return reads


def _principal(request: Request) -> Any:
    """The verified principal, or a refusal — identity never comes from input."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise V1Error(
            401,
            "Unauthenticated: no verified principal on this request.",
            code="UNAUTHENTICATED",
        )
    return principal


def _check_sectors(visible: Tuple[bool, Any]) -> Tuple[bool, Tuple[str, ...]]:
    """Validate the scope helper's answer: ``(sees_all, named sectors)``.

    Every read handler resolves visibility by calling
    ``orchestration.v1.scope`` itself (so the static guard can prove each one
    does) and then hands the raw pair here.
    """
    sees_all, sectors = visible
    if not sees_all and not sectors:
        # The route floor is ``domain:read``, so reaching here with no sector at
        # all means the credential is misconfigured; refusing is the only
        # truthful answer (it can read nothing).
        raise V1Error(
            403,
            "This credential names no readable sector on /v1.",
            code="INSUFFICIENT_SCOPE",
        )
    return sees_all, tuple(sorted(sectors))


def _sector_filter(sees_all: bool, sectors: Tuple[str, ...]) -> Optional[Tuple[str, ...]]:
    """The sector filter to hand the read model: ``None`` means every sector."""
    return None if sees_all else sectors


def _cursor(raw: Optional[str]) -> Optional[Tuple[Optional[str], str]]:
    """Decode a page cursor (a bad one is a 422, never a silent first page)."""
    return envelope_module.parse_cursor(raw)


def _limit(raw: Optional[int]) -> int:
    """Validate ``limit`` (out of range is a refusal, not a clamp)."""
    return envelope_module.parse_limit(raw)


@router.get("/capsules/{capsule_id}/review")
async def review_capsule(capsule_id: str, request: Request) -> Dict[str, Any]:
    """Read-only: the capsule's current review surface and element hashes.

    No lock, no write, no mtime change — the binding is computed fresh from the
    files on disk and reported exactly as the decision will check it.
    """
    api, approval = _deps()
    from capsule_engine import gates as gate_engine
    from capsule_engine.exceptions import SecurityError
    from capsule_engine.manifest import bound_digest, load_manifest

    clean_id = _clean(capsule_id)
    capsule_path = api._confined_capsule_path(clean_id)
    manifest = load_manifest(capsule_path)

    # Sector-scoped read: a domain the caller may not see answers 404 with the
    # missing-capsule body, so an unauthorised read reveals nothing.
    _scope().require_sector(
        getattr(request.state, "principal", None),
        manifest.get("domain"),
        not_found=f"Capsule '{clean_id}' not found in spool",
    )

    try:
        records = gate_engine.load_records(manifest)
        active = gate_engine.active_record(records)
    except gate_engine.GateError as exc:
        raise V1Error(
            409,
            f"gate plan unreadable — human repair required: {exc}",
            code="GATE_SCHEMA_ERROR",
        ) from exc

    try:
        binding = approval._attestation_binding(manifest, capsule_path, active)
    except SecurityError:
        # An element vanished between resolution and hashing. Report the honest
        # "nothing to attest" surface rather than inventing a digest; a decision
        # against it then fails closed with NOTHING_TO_ATTEST.
        binding = {"mode": "none"}

    mode = binding.get("mode", "none")
    reviewed: Dict[str, Any] = {
        "algorithm": approval.ATTESTATION_ALGORITHM,
        "elements": dict(binding.get("hashes") or {}) if mode == "elements" else {},
        "unviewed": [],
        "primary_sha256": binding.get("primary_sha256") if mode == "primary" else None,
    }

    gate_view: Optional[Dict[str, Any]] = None
    if active is not None:
        gate_view = active.to_view()
        gate_view["authority"] = active.authority or "capsule:approve"
        gate_view["opened"] = bool(active.opened_at)

    return {
        "capsule_id": manifest.get("capsule_id") or clean_id,
        "state": manifest.get("state"),
        "domain": manifest.get("domain"),
        "active_gate": gate_view,
        "binding": mode,
        "reviewed": reviewed,
        # The digest of the attestation above: the value the client stores to
        # prove what it saw, and the same digest a decision echoes back.
        "reviewed_sha256": approval._finalized_reviewed(reviewed),
        # Informational: the capsule-level bound digest, if any. Not what the
        # element-mode check compares against.
        "capsule_primary_sha256": bound_digest(manifest),
    }


@router.post("/decisions")
async def decide(request: Request, body: DecisionRequest) -> Dict[str, Any]:
    """Approve (attested) or reject (reasoned) a capsule — one fail-closed path."""
    api, approval = _deps()
    from capsule_engine.exceptions import (
        CapsuleError,
        LockTimeoutError,
        SecurityError,
        StateTransitionError,
    )

    clean_id = _clean(body.capsule_id)
    if not clean_id or clean_id in (".", ".."):
        raise V1Error(400, "capsule_id is required", code="VALIDATION_ERROR")

    action = (body.action or "approve").strip().lower()
    if action not in _ACTIONS:
        raise V1Error(
            400,
            f"action must be one of {', '.join(_ACTIONS)} (got {action!r})",
            code="VALIDATION_ERROR",
        )

    reviewer, audit_note = api._decision_identity(request)
    capsule_path = api._confined_capsule_path(clean_id)
    principal = getattr(request.state, "principal", None)

    try:
        if action == "reject":
            reason = (body.reason or "").strip()
            if not reason:
                raise V1Error(
                    400,
                    "A rejection requires a reason",
                    code="VALIDATION_ERROR",
                )
            outcome = approval.reject(
                capsule_path,
                principal=principal,
                reviewer=reviewer,
                audit_note=audit_note,
                reason=reason,
                idempotency_key=body.idempotency_key,
            )
        else:
            reviewed = body.reviewed.model_dump() if body.reviewed is not None else None
            outcome = approval.approve(
                capsule_path,
                principal=principal,
                reviewer=reviewer,
                audit_note=audit_note,
                reason=body.reason or "",
                attestation=reviewed,
                require_attestation=True,
                idempotency_key=body.idempotency_key,
            )
    except V1Error:
        raise
    except approval.ApprovalRefused as refused:
        raise V1Error(
            refused.status_code,
            refused.detail,
            code=refused.code,
            details=refused.details,
            remedy=refused.remedy,
        ) from refused
    except SecurityError as exc:
        raise V1Error(
            400, f"TOCTOU Integrity Violation: {exc}", code="TOCTOU_VIOLATION"
        ) from exc
    except LockTimeoutError as exc:
        raise V1Error(
            409, f"Capsule is locked by another process: {exc}", code="LOCKED"
        ) from exc
    except StateTransitionError as exc:
        raise V1Error(
            400, f"Illegal state transition: {exc}", code="INVALID_STATE"
        ) from exc
    except CapsuleError as exc:
        raise V1Error(400, f"Capsule error: {exc}", code="CAPSULE_ERROR") from exc

    return _decision_response(outcome, action)


def _decision_response(outcome: Any, action: str) -> Dict[str, Any]:
    """The ``/v1`` decision body: one flat, machine-readable record."""
    reviewed = dict(getattr(outcome, "reviewed", None) or {})
    return {
        "decision_id": outcome.decision_id,
        "capsule_id": outcome.capsule_id,
        "action": action,
        "gate_id": outcome.gate_id,
        "gate_type": outcome.gate_type,
        "gate_status": outcome.gate_status,
        "state_before": outcome.state_before,
        "state_after": outcome.state,
        "final": bool(outcome.final),
        "replayed": bool(outcome.replayed),
        "attested": bool(outcome.attested),
        "reviewed_sha256": reviewed.get("reviewed_sha256"),
        "decided_at": outcome.decided_at,
    }


# --------------------------------------------------------------------------- #
# Slice M1a: the aggregate reads — home, attention, lists, details, trace
# --------------------------------------------------------------------------- #
#
# Every one of these is a **pure read**: no lock, no write, no mtime change.
# Each list/aggregate floors at the scoped ``domain:read`` in the route map and
# filters by the sectors the credential names; each object read proves the
# object's *real* sector with ``require_sector``, so an invisible object answers
# the same 404 as a missing one.

#: The ticket stages the app may filter by (the vault's own vocabulary).
_TICKET_STAGES: Tuple[str, ...] = ("scratch", "approved", "archive")


def _capsule_states():
    """The closed capsule-state vocabulary (validated query filters)."""
    from capsule_engine.manifest import State

    return tuple(sorted(state.value for state in State))


def _ticket_states() -> Tuple[str, ...]:
    from shared_kernel.state_schema import TicketState

    return tuple(sorted(state.value for state in TicketState))


@router.get("/me")
async def me_route(request: Request) -> Dict[str, Any]:
    """Who this credential is, and what it may read and decide."""
    principal = _principal(request)
    sees_all, sectors = _check_sectors(_scope().visible_sectors(principal))
    return _reads().me(principal=principal, sees_all=sees_all, sectors=sectors)


@router.get("/overview")
async def overview_route(request: Request) -> Dict[str, Any]:
    """One call for the home: attention, pipeline, per-domain tiles, totals."""
    principal = _principal(request)
    sees_all, sectors = _check_sectors(_scope().visible_sectors(principal))
    return _reads().overview(
        principal=principal, sectors=_sector_filter(sees_all, sectors)
    )


@router.get("/attention")
async def attention_route(
    request: Request,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Paged list of everything needing a human, with ``can_act`` per item."""
    principal = _principal(request)
    sees_all, sectors = _check_sectors(_scope().visible_sectors(principal))
    return _reads().attention(
        principal=principal,
        sectors=_sector_filter(sees_all, sectors),
        limit=_limit(limit),
        after=_cursor(cursor),
    )


@router.get("/capsules")
async def capsules_route(
    request: Request,
    domain: Optional[str] = None,
    stage: Optional[str] = None,
    state: Optional[str] = None,
    needs_me: Optional[bool] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """The work list: capsules in the caller's sectors, filtered and paged."""
    principal = _principal(request)
    sees_all, sectors = _check_sectors(_scope().visible_sectors(principal))
    stage_filter = envelope_module.require_choice(
        stage, name="stage", allowed=messages.STAGE_KEYS
    )
    state_filter = envelope_module.require_choice(
        state, name="state", allowed=_capsule_states()
    )
    return _reads().capsules(
        principal=principal,
        sectors=_sector_filter(sees_all, sectors),
        domain=domain,
        stage=stage_filter,
        state=state_filter,
        needs_me=needs_me,
        limit=_limit(limit),
        after=_cursor(cursor),
    )


@router.get("/capsules/{capsule_id}")
async def capsule_detail_route(capsule_id: str, request: Request) -> Dict[str, Any]:
    """One capsule in full: plan, bound hashes, approval chain, accounting.

    Reads only — it never opens a gate or binds a baseline, so looking at a
    capsule cannot change what a later decision is checked against.
    """
    clean_id = _clean(capsule_id)
    view = _reads().capsule_detail(clean_id, principal=_principal(request))
    _scope().require_sector(
        getattr(request.state, "principal", None),
        view.get("domain"),
        not_found=f"Capsule '{clean_id}' not found in spool",
    )
    return view


@router.get("/capsules/{capsule_id}/trace")
async def capsule_trace_route(
    capsule_id: str,
    request: Request,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """The capsule's hash-chained history as a bounded, paged timeline."""
    clean_id = _clean(capsule_id)
    view = _reads().capsule_trace(
        clean_id, limit=_limit(limit), after=_cursor(cursor)
    )
    _scope().require_sector(
        getattr(request.state, "principal", None),
        view.get("domain"),
        not_found=f"Capsule '{clean_id}' not found in spool",
    )
    return view


@router.get("/tickets")
async def tickets_route(
    request: Request,
    sector: Optional[str] = None,
    stage: Optional[str] = None,
    state: Optional[str] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """The request list: tickets in the caller's sectors, filtered and paged."""
    principal = _principal(request)
    sees_all, sectors = _check_sectors(_scope().visible_sectors(principal))
    stage_filter = envelope_module.require_choice(
        stage, name="stage", allowed=_TICKET_STAGES
    )
    state_filter = envelope_module.require_choice(
        state, name="state", allowed=_ticket_states()
    )
    return _reads().tickets(
        principal=principal,
        sectors=_sector_filter(sees_all, sectors),
        sector=sector,
        stage=stage_filter,
        state=state_filter,
        limit=_limit(limit),
        after=_cursor(cursor),
    )


@router.get("/tickets/{ticket_id}")
async def ticket_detail_route(ticket_id: str, request: Request) -> Dict[str, Any]:
    """One ticket in full: steps, the decision ledger, and its current stage."""
    clean_id = _clean(ticket_id)
    principal = _principal(request)
    sees_all, sectors = _check_sectors(_scope().visible_sectors(principal))
    view = _reads().ticket_detail(
        clean_id, principal=principal, sectors=_sector_filter(sees_all, sectors)
    )
    _scope().require_sector(
        principal,
        view.get("sector"),
        not_found=f"Ticket '{clean_id}' not found.",
    )
    return view


# --------------------------------------------------------------------------- #
# Slice 2: the two other small human decisions — ticket + gate entry
# --------------------------------------------------------------------------- #
#
# Same contract as the capsule routes: a pure-read review reports the current
# ``{field: sha256}`` binding; a decision echoes it back, and the shared service
# verifies the echo *inside the writer's own lock* (the ticket's sidecar lock,
# the gate script's lock). The router owns no verification — it shapes HTTP.


def _service():
    """The shared decision service, imported lazily (it reaches the writers)."""
    from orchestration.v1 import service

    return service


@router.get("/tickets/{ticket_id}/review")
async def review_ticket_route(ticket_id: str, request: Request) -> Dict[str, Any]:
    """Read-only: the ticket's current review surface and element hashes."""
    clean_id = _clean(ticket_id)
    view = _service().review_ticket(clean_id)
    _scope().require_sector(
        getattr(request.state, "principal", None),
        view.get("sector"),
        not_found=f"Ticket '{clean_id}' not found.",
    )
    return view


@router.post("/tickets/{ticket_id}/decision")
async def decide_ticket_route(
    ticket_id: str, request: Request, body: TargetDecisionRequest
) -> Dict[str, Any]:
    """Accept (promote, attested) or send back (demote, reasoned) a ticket."""
    principal = getattr(request.state, "principal", None)
    return _service().decide_ticket(_clean(ticket_id), body, principal=principal)


@router.get("/gates/{gate_id}/review")
async def review_gate_route(gate_id: str, request: Request) -> Dict[str, Any]:
    """Read-only: the gate entry's current review surface and element hashes."""
    clean_id = _clean(gate_id)
    view = _service().review_gate(clean_id)
    _scope().require_sector(
        getattr(request.state, "principal", None),
        view.get("sector"),
        not_found=f"Gate '{clean_id}' not found or unreadable.",
    )
    return view


@router.post("/gates/{gate_id}/decision")
async def decide_gate_route(
    gate_id: str, request: Request, body: TargetDecisionRequest
) -> Dict[str, Any]:
    """Approve (attested) or reject (reasoned) a ``needs_gate`` entry."""
    principal = getattr(request.state, "principal", None)
    return await _service().decide_gate(_clean(gate_id), body, principal=principal)
