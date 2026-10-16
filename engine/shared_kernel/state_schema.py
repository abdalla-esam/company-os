#!/usr/bin/env python3
"""state_schema.py — Pydantic fail-closed schemas for the file-as-state vault (sector scripts).

Every entity the vault persists is a strict BaseModel. A malformed ticket,
gate entry, or handoff payload is a ValidationError before any business
rule sees it: the disk stays the witness, and the command that read it
reports failure instead of trusting band-aided dicts.

Rules this enforces (mirror architecture.md):
- tickets: ticket_id, client_id, request, decision, created_at, state mandatory
- gates: gate_id, source, reason, opened_at mandatory; decision chain closed
- handoff: dispatcher -> worker contract, reason mandatory on needs_revision
- fail closed: ValidationError -> the caller returns/skips, never guesses
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional

from pydantic import BaseModel, Field, ValidationError, validator


# ---------------- time helpers (datetime covered by Pydantic) ----------------

def parse_utc(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, returning naive UTC for comparison."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---------------- state model (one enum + one transition table per entity) ----------------


class TicketState(str, Enum):
    """Closed lifecycle of a governance Ticket (on-disk ``state``).

    ``str`` mixin keeps the value JSON-serialisable and comparable to raw
    strings read off the disk (``ticket["state"] == "open"`` works for both a
    validated and a lazily-parsed value).

    Vocabulary discipline: this enum lists **ticket** states only. Capsule
    states live in ``capsule_engine.manifest.State`` — re-exported lazily as
    ``CapsuleState`` below. The one regex this replaced mixed both entities
    *and* rejected two states the API itself writes (``draft``, ``settled``).
    """

    DRAFT = "draft"                        # scratch stage; not dispatched yet
    OPEN = "open"                          # accepted; waiting for dispatch
    PENDING = "pending"                    # retry queue: failed attempt / lease reclaimed
    IN_PROGRESS = "in_progress"            # dispatcher holds the lease; worker has the handoff
    PENDING_APPROVAL = "pending_approval"  # human governance gate (HITL)
    NEEDS_REVISION = "needs_revision"      # fail-closed: a human or worker demanded rework
    APPROVED = "approved"                  # gate approved; awaiting publish / spawn
    PUBLISHED = "published"                # delivered by the pipeline
    DONE = "done"                          # terminal: work completed (resolve)
    SETTLED = "settled"                    # terminal: gate settled into a bridged capsule

    @classmethod
    def parse(cls, raw: Any) -> "TicketState":
        """Parse a raw on-disk string into a TicketState, or raise ``ValueError``."""
        try:
            return cls(raw)
        except (ValueError, TypeError):
            known = ", ".join(member.value for member in cls)
            raise ValueError(f"unknown ticket state {raw!r} (known: {known})") from None


#: The lawful ticket edges, mirroring the writers on disk: the dispatcher
#: (handoff results, retry, lease reclaim), ticket_mgr (promote / demote /
#: create), the settle bridge (open -> settled) and the cockpit's resolve /
#: revise commands. Fail-closed by construction: keys and targets are all
#: ``TicketState`` members — see tests/test_state_model.py.
TICKET_TRANSITIONS: Dict[TicketState, FrozenSet[TicketState]] = {
    TicketState.DRAFT: frozenset({
        TicketState.OPEN,            # promote scratch -> approved (ticket_mgr)
        TicketState.DONE,            # human resolve
    }),
    TicketState.OPEN: frozenset({
        TicketState.IN_PROGRESS,     # dispatcher claimed it
        TicketState.SETTLED,         # approved gate bridged into a capsule
        TicketState.DONE,            # human resolve
        TicketState.NEEDS_REVISION,  # human revise
        TicketState.DRAFT,           # cockpit demote back to scratch
    }),
    TicketState.PENDING: frozenset({
        TicketState.IN_PROGRESS,     # retried by the dispatcher
        TicketState.DONE,            # human resolve
        TicketState.NEEDS_REVISION,  # human revise
    }),
    TicketState.IN_PROGRESS: frozenset({
        TicketState.DONE,            # worker reported done
        TicketState.PENDING_APPROVAL,# worker reported: at the human gate
        TicketState.APPROVED,        # worker reported approved
        TicketState.PUBLISHED,       # worker reported published
        TicketState.NEEDS_REVISION,  # attempt cap reached / gate rejection
        TicketState.PENDING,         # failed attempt or lease reclaim -> retry queue
    }),
    TicketState.NEEDS_REVISION: frozenset({
        TicketState.IN_PROGRESS,     # re-dispatched after rework
        TicketState.DONE,            # human resolve
    }),
    TicketState.PENDING_APPROVAL: frozenset({
        TicketState.DONE,            # human resolve
        TicketState.NEEDS_REVISION,  # human revise
        TicketState.DRAFT,           # cockpit demote back to scratch
    }),
    TicketState.APPROVED: frozenset({
        TicketState.DONE,            # human resolve
        TicketState.NEEDS_REVISION,  # human revise
        TicketState.DRAFT,           # cockpit demote back to scratch
    }),
    TicketState.PUBLISHED: frozenset(),  # terminal: cmd_archive moves it off the board
    TicketState.DONE: frozenset(),       # terminal: cmd_archive moves it off the board
    TicketState.SETTLED: frozenset(),    # terminal: work lives on as a capsule
}

#: States with no lawful exit — a dashboard board never counts them as active.
TICKET_TERMINAL_STATES: FrozenSet[TicketState] = frozenset(
    state for state, targets in TICKET_TRANSITIONS.items() if not targets
)

#: States with work still ahead of them — what a dashboard board shows.
TICKET_ACTIVE_STATES: FrozenSet[TicketState] = frozenset(
    state for state, targets in TICKET_TRANSITIONS.items() if targets
)


def can_transition(frm: Any, to: Any) -> bool:
    """True when ``frm -> to`` is a lawful ticket edge (unknown states: False)."""
    try:
        src = frm if isinstance(frm, TicketState) else TicketState.parse(frm)
        dst = to if isinstance(to, TicketState) else TicketState.parse(to)
    except ValueError:
        return False
    return dst in TICKET_TRANSITIONS.get(src, frozenset())


def __getattr__(name: str) -> Any:
    """Lazy aliases for the capsule state model (PEP 562).

    Resolved on first access so importing ``shared_kernel`` never pulls
    ``capsule_engine`` in at module-import time: the engine package imports
    this module (post_builder), so an eager import here would cycle.
    """
    if name == "CapsuleState":
        from capsule_engine.manifest import State as CapsuleState
        return CapsuleState
    if name == "CAPSULE_TRANSITIONS":
        from capsule_engine.manifest import TRANSITIONS as CAPSULE_TRANSITIONS
        return CAPSULE_TRANSITIONS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------- tickets ----------------

class Decision(BaseModel):
    status: str = Field(..., pattern=r"^(accepted|needs_gate|rejected)$")
    risk_tier: Optional[str] = None
    reason: str = Field(..., min_length=1)
    ticket: Optional[str] = None


class HistoryEntry(BaseModel):
    at: Optional[str] = None
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    by: Optional[str] = None
    why: Optional[str] = None


# ---------------- pipeline / spawning ----------------

class SpawnNext(BaseModel):
    target_sector: str = Field(..., min_length=1)
    request: str = Field(..., min_length=1)
    pass_deliverable: bool = Field(default=False)
    spawn_next: Optional[Dict[str, Any]] = None


class Ticket(BaseModel):
    schema_version: Optional[int] = Field(default=None)
    ticket_id: str = Field(..., min_length=1)
    client_id: str = Field(..., min_length=1)
    request: str = Field(..., min_length=1)
    decision: Optional[Decision] = None
    created_at: Optional[str] = None
    state: TicketState = Field(..., description="TicketState: ticket lifecycle only (never capsule states)")
    via: Optional[str] = None
    attempts: int = Field(default=0, ge=0)
    history: List[HistoryEntry] = Field(default_factory=list)
    done_at: Optional[str] = None
    published_at: Optional[str] = None
    revision_note: Optional[Dict[str, Any]] = None
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution: Optional[str] = None
    spawn_next: Optional[SpawnNext] = None
    spawned: Optional[bool] = None
    spawned_ticket_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    capsule_id: Optional[str] = None
    bundle_status: Optional[str] = None
    channels: List[str] = Field(default_factory=list)
    company_context: Optional[Dict[str, Any]] = None
    event_date: Optional[str] = None
    ttl_hours: Optional[int] = None

    # Pydantic v2 aliases for the raw on-disk JSON keys
    class Config:
        populate_by_name = True

    @validator("schema_version", always=True)
    def validate_schema_version(cls, v: Any) -> Optional[int]:  # noqa: N805
        if v is not None:
            from shared_kernel.schema_version import KIND_TICKET, check_schema_version

            check_schema_version(KIND_TICKET, {"schema_version": v}, source="Ticket")
        return v

    @validator("attempts", pre=True)
    def coerce_attempts(cls, v: Any) -> int:  # noqa: N805
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
        if isinstance(v, str):
            return int(v)
        return 0

    @validator("created_at", pre=True)
    def coerce_created(cls, v: Any) -> Optional[str]:  # noqa: N805
        if not v:
            return None
        if isinstance(v, str):
            return v
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v)


# ---------------- gates ----------------

class GateSource(BaseModel):
    sector: str = Field(..., min_length=1)
    client_id: str = Field(..., min_length=1)
    request: Optional[str] = Field(default=None, min_length=1)


class GateEntry(BaseModel):
    schema_version: Optional[int] = Field(default=None)
    gate_id: str = Field(..., min_length=1)
    source: GateSource
    risk_tier: Optional[str] = None
    reason: str = Field(..., min_length=1)
    opened_at: str = Field(..., min_length=1)
    state: str = Field(pattern=r"^(open|closed)$")
    decision: Optional[str] = Field(default=None, pattern=r"^(approved|rejected)$")
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    decision_reason: Optional[str] = None

    class Config:
        populate_by_name = True

    @validator("schema_version", always=True)
    def validate_gate_schema_version(cls, v: Any) -> Optional[int]:  # noqa: N805
        if v is not None:
            from shared_kernel.schema_version import KIND_GATE_ENTRY, check_schema_version

            check_schema_version(KIND_GATE_ENTRY, {"schema_version": v}, source="GateEntry")
        return v


# ---------------- handoff (dispatcher -> worker) ----------------

class HandoffPayload(BaseModel):
    ticket_id: str = Field(..., min_length=1)
    sector: str = Field(..., min_length=1)
    client_id: Optional[str] = None
    request: Optional[str] = None
    via: Optional[str] = None
    revision_note: Optional[Dict[str, Any]] = None
    attempt: Optional[int] = None
    context: Optional[Dict[str, Any]] = None


class HandoffResult(BaseModel):
    result: str = Field(..., pattern=r"^(done|needs_revision|pending_approval|approved|published)$")
    reason: Optional[str] = Field(default=None, min_length=1)
    notes: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    published_at: Optional[str] = None

    @validator("reason", always=True)
    def reason_mandatory_for_revision(cls, v: Any, values: Dict[str, Any]) -> Any:  # noqa: N805
        if values.get("result") == "needs_revision" and not v:
            raise ValueError("reason mandatory for needs_revision")
        return v


# ---------------- settle output ----------------

class SettleOutput(BaseModel):
    settled: List[str] = Field(default_factory=list)


# ---------------- errors ----------------

class VaultError(Exception):
    """Fail-closed error: the vault reported a broken input/state, nothing more."""
