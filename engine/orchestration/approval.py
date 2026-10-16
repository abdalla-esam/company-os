#!/usr/bin/env python3
"""approval.py — the unified approve/reject service (single source of truth).

Every cockpit decision about a capsule goes through exactly two functions —
:func:`approve` and :func:`reject`. The three HTTP routes that expose them
(``POST /api/capsules/{id}/approve``, ``POST /api/capsules/{id}/reject`` and
``POST /api/capsules/{id}/gates/{gate_id}/decision``) are thin wrappers:
identity extraction, request validation and response formatting stay in the
routes; the decision — permissions, gate plan, state transition and side
effects — lives here and nowhere else.

Contract (fail closed):

- one :class:`CapsuleLock`, one manifest, one save per decision;
- **permissions (strictest of the three routes)**: ``capsule:approve`` /
  ``capsule:reject`` on the capsule's own branch, **and** the active gate's
  declared authority whenever a gate record is involved;
- **state**: approve/reject only from ``pending_approval`` — except the
  decision route, whose named gate also accepts ``in_review`` (its reject
  uses the lawful ``in_review -> needs_revision`` edge; its approve seats
  the capsule at the gate when the plan completes via ``in_review ->
  pending_approval``, a plan-complete-guarded edge); every state move goes
  through :func:`transition`, so the transition table is enforced and an
  illegal edge refuses;
- **planless capsules are refused for approval** while gates are enabled —
  the refusal names ``ops/tools/seed_gate_plans.py``, the operator command
  that seeds a plan without ever touching state;
- **reject side effects**: ``feedback.txt`` (always newline-terminated), the
  cumulative feedback ledger, and an immediate autonomy demotion
  (:func:`governance.apply_rejection` — verified idempotent, so the pulse
  sweep's later call is a no-op). Ledger/gate/demotion bookkeeping errors are
  logged loudly and never block the rejection itself;
- **additive observability**: one execution-trace entry and one decision
  event on the bus per successful approve/reject.

Nothing here knows about HTTP: deliberate refusals raise
:class:`ApprovalRefused` carrying the status the route should answer, while
domain errors (``SecurityError``, ``LockTimeoutError``,
``StateTransitionError``, ``CapsuleError``) propagate so every route keeps
the error mapping it has today.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, MutableMapping, Optional, Tuple

from capsule_engine import State
from capsule_engine import gates as gate_engine
from capsule_engine import memory as memory_engine
from capsule_engine import spend_control
from capsule_engine import trace as trace_engine
from capsule_engine.exceptions import SecurityError
from capsule_engine.governance import apply_rejection, domain_of
from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import (
    ARTIFACT_FILE,
    artifact_sha256,
    bound_digest,
    load_manifest,
    now_iso,
    save_manifest,
    transition,
)
from capsule_engine.policy import load_policy
from orchestration import access_control
from shared_kernel.atomic_io import atomic_text

logger = logging.getLogger("orchestration.approval")

#: Where an operator repairs a capsule whose plan is missing. Fail-closed
#: refusals name this command, so the remedy travels with the error.
SEED_COMMAND = "python3 ops/tools/seed_gate_plans.py"


class ApprovalRefused(Exception):
    """A deliberate, fail-closed refusal with the HTTP status to answer.

    Raised only by the service; routes convert it into their ``HTTPException``
    so response shapes and status codes stay exactly where they were.

    ``code``/``details``/``remedy`` are additive: the legacy ``/api`` routes
    ignore them (they only read ``detail``/``status_code``), while the ``/v1``
    error envelope uses them to carry the machine-readable refusal code and its
    structured context.
    """

    def __init__(
        self,
        detail: str,
        status_code: int = 400,
        *,
        code: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        remedy: Optional[str] = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.code = code
        self.details = dict(details) if details else None
        self.remedy = remedy


@dataclass
class ApprovalOutcome:
    """What a decision did — enough for every route to shape its response."""

    capsule_id: str
    state: str
    decision: str = "approve"  # approve | skip | reject (the gate verb)
    gate_id: Optional[str] = None
    gate_type: Optional[str] = None
    final: bool = False  # approve only: the capsule reached `approved`
    reason: str = ""
    plan: Dict[str, Any] = field(default_factory=dict)
    checklist: List[Dict[str, Any]] = field(default_factory=list)
    # --- v1 slices (additive; legacy routes ignore them) -------------------
    decision_id: str = ""
    gate_status: Optional[str] = None
    decided_at: Optional[str] = None
    state_before: Optional[str] = None
    replayed: bool = False
    attested: bool = False
    reviewed: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# attestation + idempotency (Slice 1 of the v1 API)
# --------------------------------------------------------------------------- #

#: Manifest key holding the bounded, ordered decision ledger. Optional by
#: design: a capsule written before this slice carries no such key, loads
#: unchanged, and the list is stamped on the next decision write (the same
#: precedent ``revision_count`` follows — absent/empty is valid, never an error).
DECISIONS_KEY = "decisions"
MAX_DECISION_RECORDS = 50

#: The only attestation algorithm this slice accepts.
ATTESTATION_ALGORITHM = "sha256"


def _normalize_attestation(attestation: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Validate and canonicalize a reviewed-attestation payload (fail closed)."""
    if attestation is None:
        return {"present": False}
    if not isinstance(attestation, Mapping):
        raise ApprovalRefused(
            "reviewed attestation must be an object",
            400,
            code="VALIDATION_ERROR",
        )
    algorithm = attestation.get("algorithm")
    if algorithm is None:
        algorithm = ATTESTATION_ALGORITHM
    if not isinstance(algorithm, str) or algorithm.strip().lower() != ATTESTATION_ALGORITHM:
        raise ApprovalRefused(
            f"unsupported attestation algorithm {algorithm!r} (only "
            f"{ATTESTATION_ALGORITHM!r} is accepted)",
            400,
            code="VALIDATION_ERROR",
        )
    raw_elements = attestation.get("elements")
    if raw_elements is None:
        raw_elements = {}
    if not isinstance(raw_elements, Mapping):
        raise ApprovalRefused(
            "reviewed.elements must be an object mapping path -> sha256",
            400,
            code="VALIDATION_ERROR",
        )
    elements: Dict[str, str] = {}
    for key, value in raw_elements.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
            raise ApprovalRefused(
                "reviewed.elements must map string paths to non-empty sha256 "
                "strings",
                400,
                code="VALIDATION_ERROR",
            )
        elements[key] = value.strip().lower()
    raw_unviewed = attestation.get("unviewed")
    if raw_unviewed is None:
        raw_unviewed = []
    if isinstance(raw_unviewed, (str, bytes)) or not isinstance(raw_unviewed, (list, tuple)):
        raise ApprovalRefused(
            "reviewed.unviewed must be a list of paths", 400, code="VALIDATION_ERROR"
        )
    unviewed = [str(item) for item in raw_unviewed if isinstance(item, (str, int))]
    primary = attestation.get("primary_sha256")
    if primary is not None and (not isinstance(primary, str) or not primary.strip()):
        raise ApprovalRefused(
            "reviewed.primary_sha256 must be a non-empty string when present",
            400,
            code="VALIDATION_ERROR",
        )
    return {
        "present": True,
        "algorithm": ATTESTATION_ALGORITHM,
        "elements": elements,
        "unviewed": unviewed,
        "primary_sha256": primary.strip().lower() if isinstance(primary, str) else None,
    }


def _decision_fingerprint(
    action: str,
    named_gate: Optional[str],
    reason: str,
    actor: str,
    reviewed: Mapping[str, Any],
) -> str:
    """A stable digest of one decision request (idempotent replay key).

    The actor is part of the fingerprint on purpose: a *different* credential
    presenting the same idempotency key is a reused key, refused rather than
    replayed as someone else's decision.
    """
    canonical = json.dumps(
        {
            "action": action,
            "gate_id": named_gate,
            "reason": reason,
            "actor": actor,
            "reviewed": {
                "algorithm": reviewed.get("algorithm"),
                "primary_sha256": reviewed.get("primary_sha256"),
                "elements": dict(reviewed.get("elements") or {}),
                "unviewed": list(reviewed.get("unviewed") or []),
            },
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finalized_reviewed(reviewed: Mapping[str, Any]) -> str:
    """Canonical digest of the attestation the server saw (echoed as reviewed_sha256)."""
    canonical = json.dumps(
        {
            "algorithm": reviewed.get("algorithm"),
            "primary_sha256": reviewed.get("primary_sha256"),
            "elements": dict(reviewed.get("elements") or {}),
            "unviewed": list(reviewed.get("unviewed") or []),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fallback_targets(manifest: Mapping[str, Any]) -> Tuple[str, ...]:
    """Legacy/no-target fallback: the recorded artifact path, else copy.md."""
    metadata = manifest.get("metadata")
    if isinstance(metadata, Mapping):
        recorded = metadata.get("artifact_path")
        if isinstance(recorded, str) and recorded.strip():
            return (recorded.strip(),)
    return (ARTIFACT_FILE,)


def _attestation_binding(
    manifest: Mapping[str, Any],
    capsule_path: Path,
    active: Optional[Any],
) -> Dict[str, Any]:
    """The server's current binding for a decision, computed inside the lock.

    Returns either ``{"mode": "elements", "hashes": {path: sha256}}`` — the
    gate's bound artifacts when bound, otherwise the freshly resolved target
    hashes — or ``{"mode": "primary", "primary_sha256": digest}`` when there is
    no element binding and the capsule carries a primary digest, or
    ``{"mode": "none"}`` when there is nothing to attest.
    """
    if active is not None:
        if active.artifacts:
            paths = sorted(active.artifacts.keys())
        elif active.targets:
            resolved, _ = gate_engine.resolve_targets(capsule_path, active.targets)
            paths = sorted(resolved)
        else:
            resolved, _ = gate_engine.resolve_targets(capsule_path, _fallback_targets(manifest))
            paths = sorted(resolved)
        if paths:
            hashes: Dict[str, str] = {}
            for relative in paths:
                hashes[relative] = artifact_sha256(capsule_path, relative)
            return {"mode": "elements", "hashes": hashes}
    digest = bound_digest(manifest)
    if digest:
        return {"mode": "primary", "primary_sha256": digest}
    return {"mode": "none"}


def _verify_attestation(
    manifest: Mapping[str, Any],
    capsule_path: Path,
    active: Optional[Any],
    attestation: Optional[Mapping[str, Any]],
    *,
    require: bool,
) -> Dict[str, Any]:
    """Compare the reviewer's attestation with the server's current binding.

    Runs inside the :class:`CapsuleLock`, before any write. Raises a coded
    :class:`ApprovalRefused` on any mismatch; mutates nothing.
    """
    reviewed = _normalize_attestation(attestation)
    if not reviewed.get("present"):
        if require:
            raise ApprovalRefused(
                "Attestation is mandatory on this route: reviewed.elements (the "
                "sha256 of what the reviewer saw) must accompany an approval.",
                400,
                code="ATTESTATION_REQUIRED",
            )
        return {
            "attested": False,
            "algorithm": None,
            "elements": {},
            "unviewed": [],
            "primary_sha256": None,
            "binding": {},
        }

    binding = _attestation_binding(manifest, capsule_path, active)
    client_elements: Dict[str, str] = reviewed["elements"]

    if binding["mode"] == "none":
        raise ApprovalRefused(
            "Nothing to attest: this approval has no bound artifacts and the "
            "capsule carries no primary digest (fail closed).",
            409,
            code="NOTHING_TO_ATTEST",
        )

    if binding["mode"] == "primary":
        expected = binding["primary_sha256"]
        current = reviewed["primary_sha256"]
        if not current or not hmac.compare_digest(expected, current):
            raise ApprovalRefused(
                "Stale content: reviewed.primary_sha256 does not match the "
                "capsule's primary digest (the artifact changed after review).",
                409,
                code="STALE_CONTENT_HASH",
                details={
                    "expected": {"primary_sha256": expected},
                    "current": {"primary_sha256": current},
                },
            )
    else:
        expected_hashes: Dict[str, str] = binding["hashes"]
        expected_paths = set(expected_hashes)
        client_paths = set(client_elements)
        if client_paths != expected_paths:
            raise ApprovalRefused(
                "Hash set mismatch: the attested elements are not exactly the "
                "targets bound to this gate (missing or extra paths).",
                409,
                code="HASH_SET_MISMATCH",
                details={
                    "expected": sorted(expected_paths),
                    "current": sorted(client_paths),
                    "missing": sorted(expected_paths - client_paths),
                    "extra": sorted(client_paths - expected_paths),
                },
            )
        mismatched: Dict[str, Dict[str, str]] = {}
        for relative in sorted(expected_paths):
            if not hmac.compare_digest(expected_hashes[relative], client_elements[relative]):
                mismatched[relative] = {
                    "expected": expected_hashes[relative],
                    "current": client_elements[relative],
                }
        if mismatched:
            raise ApprovalRefused(
                "Stale content: one or more attested artifacts changed after "
                "review (sha256 mismatch).",
                409,
                code="STALE_CONTENT_HASH",
                details={
                    "expected": expected_hashes,
                    "current": dict(client_elements),
                    "mismatched": mismatched,
                },
            )

    return {
        "attested": True,
        "algorithm": ATTESTATION_ALGORITHM,
        "elements": dict(client_elements),
        "unviewed": list(reviewed["unviewed"]),
        "primary_sha256": reviewed["primary_sha256"],
        "binding": binding,
    }


def _decision_evidence(
    decision_id: str,
    actor: str,
    attestation: Mapping[str, Any],
) -> Dict[str, Any]:
    """The gate evidence record for an attestation (audit trail)."""
    return {
        "decision_id": decision_id,
        "actor": actor,
        "attested": bool(attestation.get("attested")),
        "algorithm": attestation.get("algorithm"),
        "elements": dict(attestation.get("elements") or {}),
        "unviewed": list(attestation.get("unviewed") or []),
        "primary_sha256": attestation.get("primary_sha256"),
    }


def _find_decision(manifest: Mapping[str, Any], key: str) -> Optional[Dict[str, Any]]:
    records = manifest.get(DECISIONS_KEY)
    if not isinstance(records, list):
        return None
    for record in reversed(records):
        if isinstance(record, dict) and record.get("key") == key:
            return record
    return None


def _append_decision(
    manifest: MutableMapping[str, Any], record: Mapping[str, Any]
) -> None:
    records = manifest.get(DECISIONS_KEY)
    if not isinstance(records, list):
        records = []
    records.append(dict(record))
    manifest[DECISIONS_KEY] = records[-MAX_DECISION_RECORDS:]


def _record_decision(
    manifest: MutableMapping[str, Any],
    outcome: ApprovalOutcome,
    *,
    key: Optional[str],
    fingerprint: str,
    action: str,
    actor: str,
    attestation: Mapping[str, Any],
    reviewed_raw: Optional[Mapping[str, Any]],
) -> None:
    reviewed_digest = (
        _finalized_reviewed(_normalize_attestation(reviewed_raw))
        if reviewed_raw is not None
        else None
    )
    _append_decision(
        manifest,
        {
            "key": key,
            "fingerprint": fingerprint,
            "decision_id": outcome.decision_id,
            "capsule_id": outcome.capsule_id,
            "action": action,
            "gate_id": outcome.gate_id,
            "gate_type": outcome.gate_type,
            "status": outcome.gate_status,
            "final": bool(outcome.final),
            "state_before": outcome.state_before,
            "state_after": outcome.state,
            "reason": outcome.reason,
            "actor": actor,
            "attested": bool(attestation.get("attested")),
            "algorithm": attestation.get("algorithm"),
            "elements": dict(attestation.get("elements") or {}),
            "unviewed": list(attestation.get("unviewed") or []),
            "primary_sha256": attestation.get("primary_sha256"),
            "reviewed_sha256": reviewed_digest,
            "at": now_iso(),
            "plan": dict(outcome.plan),
            "checklist": list(outcome.checklist),
        },
    )


def _replay_outcome(record: Mapping[str, Any]) -> ApprovalOutcome:
    """Rebuild the original outcome from a persisted decision record."""
    return ApprovalOutcome(
        capsule_id=str(record.get("capsule_id") or ""),
        state=str(record.get("state_after") or ""),
        decision=str(record.get("action") or "approve"),
        gate_id=record.get("gate_id"),
        gate_type=record.get("gate_type"),
        final=bool(record.get("final")),
        reason=str(record.get("reason") or ""),
        plan=dict(record.get("plan") or {}),
        checklist=list(record.get("checklist") or []),
        decision_id=str(record.get("decision_id") or ""),
        gate_status=record.get("status"),
        decided_at=record.get("at"),
        state_before=record.get("state_before"),
        replayed=True,
        attested=bool(record.get("attested")),
        reviewed={
            "algorithm": record.get("algorithm"),
            "primary_sha256": record.get("primary_sha256"),
            "elements": dict(record.get("elements") or {}),
            "unviewed": list(record.get("unviewed") or []),
            "reviewed_sha256": record.get("reviewed_sha256"),
        },
    )


def _unattested() -> Dict[str, Any]:
    """The empty attestation a legacy (or reject) decision carries."""
    return {
        "attested": False,
        "algorithm": None,
        "elements": {},
        "unviewed": [],
        "primary_sha256": None,
    }


def _reviewed_outcome(
    attestation_result: Mapping[str, Any],
    reviewed_raw: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """The reviewed-attestation echo for an outcome (empty when unattested).

    ``reviewed_sha256`` is the server's canonical digest of the attestation it
    validated — the fingerprint the client can store to prove what it saw.
    """
    if not attestation_result.get("attested"):
        return {}
    digest = None
    if reviewed_raw is not None:
        digest = _finalized_reviewed(_normalize_attestation(reviewed_raw))
    return {
        "algorithm": attestation_result.get("algorithm"),
        "primary_sha256": attestation_result.get("primary_sha256"),
        "elements": dict(attestation_result.get("elements") or {}),
        "unviewed": list(attestation_result.get("unviewed") or []),
        "reviewed_sha256": digest,
    }


def _kill_switch_on() -> bool:
    """True when the spend kill switch is on (a fail-closed approval stop)."""
    try:
        return bool(spend_control.kill_active())
    except Exception:  # pragma: no cover - defensive: an unreadable config stops
        return True



# --------------------------------------------------------------------------- #
# decision event bus (additive: the SSE stream subscribes, tests drain it)
# --------------------------------------------------------------------------- #


class _EventBus:
    """In-process fan-out of decision events: bounded history + subscribers."""

    def __init__(self, history: int = 200) -> None:
        self._lock = threading.Lock()
        self._subscribers: List["queue.Queue[Dict[str, Any]]"] = []
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history)

    def publish(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._history.append(dict(record))
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(dict(record))
            except queue.Full:
                try:  # drop oldest, keep newest — a slow reader never blocks a decision
                    subscriber.get_nowait()
                    subscriber.put_nowait(dict(record))
                except queue.Empty:  # pragma: no cover - defensive
                    pass

    def subscribe(self, maxsize: int = 64) -> "queue.Queue[Dict[str, Any]]":
        subscriber: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def recent(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._history]


BUS = _EventBus()


def subscribe_events() -> "queue.Queue[Dict[str, Any]]":
    """Subscribe this consumer to decision events (``unsubscribe_events`` when done)."""
    return BUS.subscribe()


def unsubscribe_events(subscriber: "queue.Queue[Dict[str, Any]]") -> None:
    BUS.unsubscribe(subscriber)


def drain_events(subscriber: "queue.Queue[Dict[str, Any]]") -> List[Dict[str, Any]]:
    """Non-blocking: everything published to ``subscriber`` since the last drain."""
    drained: List[Dict[str, Any]] = []
    while True:
        try:
            drained.append(subscriber.get_nowait())
        except queue.Empty:
            return drained


def recent_events() -> List[Dict[str, Any]]:
    """The bounded history of decision events (observability + tests)."""
    return BUS.recent()


def _emit(event_type: str, outcome: ApprovalOutcome, *, by: str) -> None:
    try:
        record: Dict[str, Any] = {
            "event": event_type,
            "at": now_iso(),
            "capsule_id": outcome.capsule_id,
            "gate_id": outcome.gate_id,
            "gate_type": outcome.gate_type,
            "decision": outcome.decision,
            "state": outcome.state,
            "final": outcome.final,
            "by": by,
        }
        if outcome.reason:
            record["reason"] = outcome.reason[:200]
        BUS.publish(record)
    except Exception:  # events are additive — never a reason to fail a decision
        logger.error("approval: event emit failed for %s", outcome.capsule_id, exc_info=True)


def _trace(capsule_path: Path, outcome: ApprovalOutcome, *, audit_note: str) -> None:
    try:
        verb = {"approve": "approved", "skip": "skipped", "reject": "rejected"}.get(
            outcome.decision, outcome.decision
        )
        name = f"approval: {verb}"
        if outcome.gate_id:
            name += f" gate {outcome.gate_id}"
        tracer = trace_engine.ExecutionTracer(capsule_path, capsule_id=outcome.capsule_id)
        tracer.record_step(
            f"{name} — {audit_note}"[:120],
            gear="approval",
            status=trace_engine.STATUS_SUCCESS,
        )
    except Exception:  # the trace is additive — never a reason to fail a decision
        logger.error("approval: trace entry failed for %s", outcome.capsule_id, exc_info=True)


def _finish(
    capsule_path: Path,
    outcome: ApprovalOutcome,
    *,
    reviewer: str,
    audit_note: str,
    event: str,
) -> ApprovalOutcome:
    """Post-decision observability: trace entry + bus event (both additive)."""
    _trace(capsule_path, outcome, audit_note=audit_note)
    _emit(event, outcome, by=reviewer)
    return outcome


# --------------------------------------------------------------------------- #
# shared checks (strictest of the three routes)
# --------------------------------------------------------------------------- #


def _scope(principal: Any, capability: str, resource: str) -> None:
    denial = access_control.scope_denial(principal, capability, resource)
    if denial:
        raise ApprovalRefused(denial, 403, code="INSUFFICIENT_SCOPE")


def _gate_authority(principal: Any, record: Any, domain: str) -> None:
    """The gate's *declared* authority, enforced on top of the branch capability."""
    authority = getattr(record, "authority", None)
    if authority:
        denial = access_control.scope_denial(principal, authority, domain)
        if denial:
            raise ApprovalRefused(denial, 403, code="INSUFFICIENT_SCOPE")


def _load_gate_records(manifest: Mapping[str, Any], capsule_id: str) -> Tuple[Any, ...]:
    """Gate plan of the manifest, or the shared 409 (both routes today)."""
    try:
        return gate_engine.load_records(manifest)
    except gate_engine.GateSchemaError as exc:
        raise ApprovalRefused(
            f"gate plan unreadable — human repair required: {exc}",
            409,
            code="GATE_SCHEMA_ERROR",
        ) from exc


def _plan_view(records: Tuple[Any, ...]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if not records:
        return {}, []
    return gate_engine.progress(records), gate_engine.checklist(records)


def _no_active_detail(records: Tuple[Any, ...]) -> str:
    failed = [
        record.gate_id
        for record in records
        if record.status == gate_engine.STATUS_FAILED
    ]
    if failed:
        return "No active gate: rejected gate(s) " + ", ".join(failed)
    return "All gates of this capsule are complete"


def _state_guard(
    manifest: Mapping[str, Any],
    capsule_id: str,
    *,
    allowed: Tuple[str, ...],
    label: str,
    code: str = "INVALID_STATE",
) -> None:
    current = str(manifest.get("state") or "")
    if current not in allowed:
        raise ApprovalRefused(
            f"Capsule '{capsule_id}' is in state '{current}', not {label}",
            400,
            code=code,
            details={"state": current, "allowed": list(allowed)},
        )


# --------------------------------------------------------------------------- #
# approve
# --------------------------------------------------------------------------- #


def _approve_locked(
    capsule_path: Path,
    *,
    principal: Any,
    reviewer: str,
    audit_note: str,
    named_gate: Optional[str],
    skip: bool,
    gate_reason: str,
    seat_pending_on_complete: bool,
    attestation: Optional[Mapping[str, Any]] = None,
    require_attestation: bool = False,
    idempotency_key: Optional[str] = None,
) -> ApprovalOutcome:
    capsule_id = capsule_path.name
    verb = "skip" if skip else "approve"

    with CapsuleLock(capsule_path):
        manifest = load_manifest(capsule_path)
        domain = domain_of(manifest)
        _scope(principal, "capsule:approve", domain)

        normalized = _normalize_attestation(attestation)
        fingerprint = _decision_fingerprint(
            verb, named_gate, gate_reason, reviewer, normalized
        )
        if idempotency_key:
            prior = _find_decision(manifest, idempotency_key)
            if prior is not None:
                if prior.get("fingerprint") == fingerprint:
                    return _replay_outcome(prior)
                raise ApprovalRefused(
                    "Idempotency key reused with a different request payload",
                    409,
                    code="IDEMPOTENCY_CONFLICT",
                    details={"idempotency_key": idempotency_key},
                )

        # v1 fail-closed stop: a spend kill switch refuses every approval
        # before anything is mutated (the reject path stays available).
        if _kill_switch_on():
            raise ApprovalRefused(
                "The spend kill switch is ON — approval refused (fail closed)",
                409,
                code="KILL_SWITCH_ON",
            )

        state_before = str(manifest.get("state") or "")
        decision_id = uuid.uuid4().hex

        # State: the capsule path only acts from the gate; the decision route's
        # gate-only approve also lives in `in_review` (its lawful seat edge).
        if named_gate is None:
            _state_guard(
                manifest,
                capsule_id,
                allowed=(State.PENDING_APPROVAL.value,),
                label="'pending_approval'",
            )
        else:
            _state_guard(
                manifest,
                capsule_id,
                allowed=(State.PENDING_APPROVAL.value, State.IN_REVIEW.value),
                label="'pending_approval' or 'in_review'",
            )

        records = _load_gate_records(manifest, capsule_id)

        if not records:
            if named_gate is not None:
                raise ApprovalRefused(
                    f"Capsule '{capsule_id}' has no gate plan",
                    404,
                    code="NO_GATE_PLAN",
                )
            if load_policy().gates.enabled:
                raise ApprovalRefused(
                    f"Capsule '{capsule_id}' has no gate plan and gates are enabled — "
                    "approval refused (fail closed). Seed the plan with: "
                    f"{SEED_COMMAND} --capsule-id {capsule_id} "
                    f"(or {SEED_COMMAND} --dry-run to list affected capsules).",
                    409,
                    code="PLANLESS_CAPSULE",
                    remedy=f"{SEED_COMMAND} --capsule-id {capsule_id}",
                )
            # Gates disabled by policy: the legacy hash-only approval stands.
            records = ()

        active = gate_engine.active_record(records) if records else None
        if active is None:
            if named_gate is not None:
                raise ApprovalRefused(
                    _no_active_detail(records), 409, code="NO_ACTIVE_GATE"
                )
            # Plan complete (or gates disabled without a plan): final approval.
            # The attestation still fails closed here (primary digest / nothing
            # to attest) — a v1 approval can never finalize on a stale artifact.
            attestation_result = _verify_attestation(
                manifest,
                capsule_path,
                None,
                attestation,
                require=require_attestation and not skip,
            )
            transition(
                manifest,
                State.APPROVED,
                by=reviewer,
                why=f"approved via cockpit UI — {audit_note}",
                capsule_path=capsule_path,
            )
            outcome = ApprovalOutcome(
                capsule_id=capsule_id,
                state=State.APPROVED.value,
                decision=verb,
                final=True,
                reason=gate_reason,
                decision_id=decision_id,
                gate_status=None,
                decided_at=now_iso(),
                state_before=state_before,
                attested=bool(attestation_result.get("attested")),
                reviewed=_reviewed_outcome(attestation_result, attestation),
            )
            _record_decision(
                manifest,
                outcome,
                key=idempotency_key,
                fingerprint=fingerprint,
                action=verb,
                actor=reviewer,
                attestation=attestation_result,
                reviewed_raw=attestation,
            )
            save_manifest(capsule_path, manifest)
            return outcome

        if named_gate is not None and active.gate_id != named_gate:
            raise ApprovalRefused(
                f"Gate '{named_gate}' is not the active gate "
                f"(active: '{active.gate_id}') — fail-closed dual control",
                409,
                code="NOT_ACTIVE_GATE",
                details={"gate_id": named_gate, "active_gate": active.gate_id},
            )

        # Automated gates are the runner's job — a human may not sign them off
        # (the capsule route's check, now applied to every approve).
        if active.automated and not skip:
            raise ApprovalRefused(
                f"Automated gate '{active.gate_id}' ({active.gate_type}) is pending "
                "— the gate runner evaluates it on the next pulse",
                409,
                code="AUTOMATED_GATE_PENDING",
                details={"gate_id": active.gate_id, "gate_type": active.gate_type},
            )

        if skip and active.required:
            raise ApprovalRefused(
                f"Gate '{active.gate_id}' is required and cannot be skipped",
                400,
                code="REQUIRED_GATE",
                details={"gate_id": active.gate_id},
            )

        # Strictest permissions: the branch capability and the gate's own
        # authority must BOTH hold (approve has always demanded both here).
        authority = active.authority or "capsule:approve"
        denial = access_control.scope_denial(principal, authority, domain)
        if denial:
            raise ApprovalRefused(denial, 403, code="INSUFFICIENT_SCOPE")

        # The attestation is checked against the server's *current* binding,
        # recomputed fresh here inside the lock — never against a stored digest.
        attestation_result = _verify_attestation(
            manifest,
            capsule_path,
            active,
            attestation,
            require=require_attestation and not skip,
        )

        # v1: a mandatory attestation may name a gate the runner has not yet
        # opened. Verification ran against the freshly resolved targets; bind
        # them now (inside the lock) so gates.decide's `opened_at` requirement
        # is met. Legacy behaviour is untouched: the auto-open happens only
        # when attestation is required.
        if require_attestation and not skip and not active.opened_at:
            active = gate_engine.open_gate(
                manifest, active.gate_id, capsule_path, by=reviewer
            )

        try:
            updated = gate_engine.decide(
                manifest,
                active.gate_id,
                verb,
                by=reviewer,
                reason=gate_reason,
                capsule_path=capsule_path,
                evidence=_decision_evidence(decision_id, reviewer, attestation_result),
            )
        except gate_engine.GateTargetError as exc:
            if named_gate is not None:
                # The decision route's historical mapping for a TOCTOU refusal.
                raise ApprovalRefused(
                    f"TOCTOU violation: {exc}", 409, code="TOCTOU_VIOLATION"
                ) from exc
            raise  # capsule route: SecurityError -> 400 "TOCTOU Integrity Violation"
        except gate_engine.GateOrderError as exc:
            raise ApprovalRefused(str(exc), 409, code="GATE_ORDER") from exc
        except gate_engine.GateError as exc:
            if named_gate is not None:
                raise ApprovalRefused(str(exc), 400, code="GATE_ERROR") from exc
            raise  # capsule route: CapsuleError -> 400 "Capsule error: ..."

        records_after = _load_gate_records(manifest, capsule_id)
        plan, checklist = _plan_view(records_after)

        def _outcome(*, final: bool, state: str) -> ApprovalOutcome:
            return ApprovalOutcome(
                capsule_id=capsule_id,
                state=state,
                decision=verb,
                gate_id=updated.gate_id,
                gate_type=updated.gate_type,
                final=final,
                reason=gate_reason,
                plan=plan,
                checklist=checklist,
                decision_id=decision_id,
                gate_status=updated.status,
                decided_at=now_iso(),
                state_before=state_before,
                attested=bool(attestation_result.get("attested")),
                reviewed=_reviewed_outcome(attestation_result, attestation),
            )

        if gate_engine.is_complete(records_after):
            if named_gate is None:
                # The plan just completed: the capsule's approval proceeds —
                # the transition table + TOCTOU guard run inside transition().
                transition(
                    manifest,
                    State.APPROVED,
                    by=reviewer,
                    why=f"approved via cockpit UI — {audit_note}",
                    capsule_path=capsule_path,
                )
                outcome = _outcome(final=True, state=State.APPROVED.value)
                _record_decision(
                    manifest,
                    outcome,
                    key=idempotency_key,
                    fingerprint=fingerprint,
                    action=verb,
                    actor=reviewer,
                    attestation=attestation_result,
                    reviewed_raw=attestation,
                )
                save_manifest(capsule_path, manifest)
                return outcome
            # Decision route: it never approves the capsule itself; when the
            # plan completes while the capsule waits in `in_review`, seat it
            # at the approval gate. The edge is guarded inside transition():
            # the gate plan must be complete (is_complete just proved it).
            if seat_pending_on_complete and str(manifest.get("state")) == (
                State.IN_REVIEW.value
            ):
                transition(
                    manifest,
                    State.PENDING_APPROVAL,
                    by=reviewer,
                    why="all gates complete — entering the approval gate",
                    capsule_path=capsule_path,
                )

        outcome = _outcome(final=False, state=str(manifest.get("state")))
        _record_decision(
            manifest,
            outcome,
            key=idempotency_key,
            fingerprint=fingerprint,
            action=verb,
            actor=reviewer,
            attestation=attestation_result,
            reviewed_raw=attestation,
        )
        save_manifest(capsule_path, manifest)
        return outcome


def approve(
    capsule_path: Path,
    *,
    principal: Any,
    reviewer: str,
    audit_note: str,
    named_gate: Optional[str] = None,
    skip: bool = False,
    reason: str = "",
    seat_pending_on_complete: bool = False,
    attestation: Optional[Mapping[str, Any]] = None,
    require_attestation: bool = False,
    idempotency_key: Optional[str] = None,
) -> ApprovalOutcome:
    """The single approve path: decide the gate, then (lawfully) approve.

    Args:
        capsule_path: the confined capsule directory (the route resolves it).
        principal: verified principal for the strictest permission checks.
        reviewer/audit_note: who decides, and the log-safe provenance label.
        named_gate: set by the decision route — must be the active gate (409
            otherwise); ``None`` is the capsule approve route (the active gate
            or, once the plan is complete, the final approval).
        skip: decision-route skip of an *optional* gate; required gates refuse.
        reason: the recorded gate reason (routes keep their exact strings).
        seat_pending_on_complete: decision route only — when the plan
            completes while the capsule sits ``in_review``, seat it at the
            approval gate (``in_review -> pending_approval``, a lawful edge
            whose guard — a complete gate plan — transition() enforces).
        attestation: the reviewer's ``reviewed`` payload (v1). ``None`` on the
            legacy routes: no attestation is demanded and the audit records
            ``attested=false``.
        require_attestation: ``True`` on ``/v1/decisions`` — the approval is
            refused unless the attestation matches the server's current
            binding, and an unopened gate is bound inside the lock so the
            decision may proceed.
        idempotency_key: client-supplied replay key (v1). A repeat with the
            same key and payload returns the original outcome unchanged; a
            different payload under the same key is refused (409).

    Raises:
        ApprovalRefused: any deliberate refusal (status carried).
        SecurityError/LockTimeoutError/StateTransitionError/CapsuleError:
            propagate for each route's existing mapping.

    Returns:
        The decision outcome (routes shape their unchanged responses from it).
    """
    gate_reason = reason or f"{'skip' if skip else 'approve'} via cockpit — {audit_note}"
    outcome = _approve_locked(
        capsule_path,
        principal=principal,
        reviewer=reviewer,
        audit_note=audit_note,
        named_gate=named_gate,
        skip=skip,
        gate_reason=gate_reason,
        seat_pending_on_complete=seat_pending_on_complete,
        attestation=attestation,
        require_attestation=require_attestation,
        idempotency_key=idempotency_key,
    )
    if outcome.replayed:
        # A replay is a read: no new trace entry, no second event — one tap,
        # one decision, however many times the client asks again.
        return outcome
    return _finish(
        capsule_path, outcome, reviewer=reviewer, audit_note=audit_note,
        event="approval.approved",
    )


# --------------------------------------------------------------------------- #
# reject
# --------------------------------------------------------------------------- #


def _reject_locked(
    capsule_path: Path,
    *,
    principal: Any,
    reviewer: str,
    reason: str,
    named_gate: Optional[str],
    transition_why: str,
    idempotency_key: Optional[str] = None,
) -> ApprovalOutcome:
    capsule_id = capsule_path.name

    with CapsuleLock(capsule_path):
        manifest = load_manifest(capsule_path)
        domain = domain_of(manifest)
        _scope(principal, "capsule:reject", domain)

        fingerprint = _decision_fingerprint(
            "reject", named_gate, reason, reviewer, {"present": False}
        )
        if idempotency_key:
            prior = _find_decision(manifest, idempotency_key)
            if prior is not None:
                if prior.get("fingerprint") == fingerprint:
                    return _replay_outcome(prior)
                raise ApprovalRefused(
                    "Idempotency key reused with a different request payload",
                    409,
                    code="IDEMPOTENCY_CONFLICT",
                    details={"idempotency_key": idempotency_key},
                )

        state_before = str(manifest.get("state") or "")
        decision_id = uuid.uuid4().hex

        # The decision route's named gate may also reject from `in_review`
        # (lawful in_review -> needs_revision edge); the capsule route keeps
        # its pre-Phase-B pending_approval-only contract.
        if named_gate is not None:
            _state_guard(
                manifest,
                capsule_id,
                allowed=(State.PENDING_APPROVAL.value, State.IN_REVIEW.value),
                label="'pending_approval' or 'in_review'",
            )
        else:
            _state_guard(
                manifest,
                capsule_id,
                allowed=(State.PENDING_APPROVAL.value,),
                label="'pending_approval'",
            )

        gate_info = None
        records: Tuple[Any, ...] = ()
        if named_gate is not None:
            # Decision route: its gate is load-bearing (the response reports
            # it), so plan validation refuses here exactly as it always has.
            records = _load_gate_records(manifest, capsule_id)
            if not records:
                raise ApprovalRefused(
                    f"Capsule '{capsule_id}' has no gate plan",
                    404,
                    code="NO_GATE_PLAN",
                )
            active = gate_engine.active_record(records)
            if active is None:
                raise ApprovalRefused(
                    _no_active_detail(records), 409, code="NO_ACTIVE_GATE"
                )
            if active.gate_id != named_gate:
                raise ApprovalRefused(
                    f"Gate '{named_gate}' is not the active gate "
                    f"(active: '{active.gate_id}') — fail-closed dual control",
                    409,
                    code="NOT_ACTIVE_GATE",
                    details={"gate_id": named_gate, "active_gate": active.gate_id},
                )
            _gate_authority(principal, active, domain)  # strictest: declared authority
            try:
                gate_info = gate_engine.decide(
                    manifest, active.gate_id, "reject", by=reviewer, reason=reason
                )
            except gate_engine.GateOrderError as exc:
                raise ApprovalRefused(str(exc), 409, code="GATE_ORDER") from exc
            except gate_engine.GateError as exc:
                raise ApprovalRefused(str(exc), 400, code="GATE_ERROR") from exc
        else:
            # Capsule route: gate bookkeeping is best effort — loud, never
            # blocking. Permission checks are NOT bookkeeping: they refuse.
            try:
                records = gate_engine.load_records(manifest)
            except Exception as exc:
                logger.error(
                    "reject: gate plan unreadable for %s — continuing without "
                    "gate bookkeeping",
                    capsule_id,
                    exc_info=True,
                )
                records = ()
            active = gate_engine.active_record(records) if records else None
            if active is not None:
                _gate_authority(principal, active, domain)
                try:
                    gate_info = gate_engine.decide(
                        manifest, active.gate_id, "reject", by=reviewer, reason=reason
                    )
                except Exception as exc:
                    logger.error(
                        "reject: gate bookkeeping failed for %s — continuing",
                        capsule_id,
                        exc_info=True,
                    )
                    gate_info = None

        # The instruction itself — always newline-terminated.
        atomic_text(capsule_path / "feedback.txt", reason + "\n", encoding="utf-8")

        # Cumulative feedback memory — loud, never blocking.
        try:
            memory_engine.record_rejection(
                manifest,
                reason=reason,
                decision=memory_engine.DECISION_REJECTED,
                reviewer=reviewer,
                gate_id=gate_info.gate_id if gate_info else None,
                gate_type=gate_info.gate_type if gate_info else None,
                targets=gate_info.targets if gate_info else (),
            )
        except Exception:
            logger.error(
                "reject: feedback ledger write failed for %s — rejection proceeds",
                capsule_id,
                exc_info=True,
            )

        # The primary action: a lawful pending_approval -> needs_revision edge
        # (the transition table and the TOCTOU guard run inside transition()).
        # The revision counter increments here too — and past the cap the
        # same call escalates to ``failed`` (human decision), which is the
        # state the outcome must report.
        final_state = transition(
            manifest,
            State.NEEDS_REVISION,
            by=reviewer,
            why=transition_why,
            capsule_path=capsule_path,
        )

        try:
            final_records = gate_engine.load_records(manifest)
        except Exception:
            final_records = ()
        plan, checklist = _plan_view(final_records)

        outcome = ApprovalOutcome(
            capsule_id=capsule_id,
            state=final_state.value,
            decision="reject",
            gate_id=gate_info.gate_id if gate_info else None,
            gate_type=gate_info.gate_type if gate_info else None,
            reason=reason,
            plan=plan,
            checklist=checklist,
            decision_id=decision_id,
            gate_status=gate_info.status if gate_info else None,
            decided_at=now_iso(),
            state_before=state_before,
        )
        _record_decision(
            manifest,
            outcome,
            key=idempotency_key,
            fingerprint=fingerprint,
            action="reject",
            actor=reviewer,
            attestation=_unattested(),
            reviewed_raw=None,
        )
        save_manifest(capsule_path, manifest)

        # Immediate autonomy demotion. Verified idempotent: apply_rejection
        # no-ops once the domain sits at L0, so the pulse sweep's later call
        # never double-demotes; a write failure is loud and retried by that sweep.
        try:
            apply_rejection(manifest)
        except OSError:
            logger.error(
                "reject: autonomy demotion failed for %s — the pulse sweep will retry",
                capsule_id,
                exc_info=True,
            )

        return outcome


def reject(
    capsule_path: Path,
    *,
    principal: Any,
    reviewer: str,
    audit_note: str,
    reason: str,
    named_gate: Optional[str] = None,
    why: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> ApprovalOutcome:
    """The single reject path: gate failure, feedback, state, demotion.

    Args:
        capsule_path: the confined capsule directory (the route resolves it).
        principal: verified principal for the strictest permission checks.
        reviewer/audit_note: who decides, and the log-safe provenance label.
        reason: the mandatory rejection reason (recorded verbatim).
        named_gate: decision route — the gate that must be active (its
            plan validation refuses exactly as before); ``None`` is the
            capsule reject route (gate bookkeeping is best effort there).
            The decision route also accepts ``in_review`` (lawful
            ``in_review -> needs_revision``); the capsule route stays
            ``pending_approval``-only.
        why: the history line for the state move (routes keep their exact
            strings); defaults to the capsule route's wording.
        idempotency_key: client-supplied replay key (v1). A reject carries a
            reason, never a hash: the audit records ``attested=false``.

    Raises:
        ApprovalRefused: any deliberate refusal (status carried).
        LockTimeoutError/StateTransitionError/CapsuleError: propagate for
            each route's existing mapping.

    Returns:
        The decision outcome (routes shape their unchanged responses from it).
    """
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        # Routes validate first with their own wording; this is defence in depth.
        raise ApprovalRefused(
            "A rejection requires a reason", 400, code="VALIDATION_ERROR"
        )
    transition_why = why or f"Rejected: {clean_reason} — {audit_note}"
    outcome = _reject_locked(
        capsule_path,
        principal=principal,
        reviewer=reviewer,
        reason=clean_reason,
        named_gate=named_gate,
        transition_why=transition_why,
        idempotency_key=idempotency_key,
    )
    if outcome.replayed:
        # A replay is a read: one rejection, one trace entry, one event.
        return outcome
    return _finish(
        capsule_path, outcome, reviewer=reviewer, audit_note=audit_note,
        event="approval.rejected",
    )
