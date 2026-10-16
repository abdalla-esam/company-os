"""shared_kernel — Core Pydantic state schemas and domain contracts.

Provides immutable schemas for tickets, decisions, gates, and handoff protocols
enforcing fail-closed validation across all business sectors.
"""

from shared_kernel.state_schema import (
    Decision,
    GateEntry,
    HandoffResult,
    HistoryEntry,
    SpawnNext,
    Ticket,
    TICKET_ACTIVE_STATES,
    TICKET_TERMINAL_STATES,
    TICKET_TRANSITIONS,
    TicketState,
    can_transition,
    parse_utc,
)

__all__ = [
    "Decision",
    "GateEntry",
    "HandoffResult",
    "HistoryEntry",
    "SpawnNext",
    "Ticket",
    # State model: tickets get their own enum + transition table; the capsule
    # counterpart (CapsuleState / CAPSULE_TRANSITIONS) is exposed lazily from
    # shared_kernel.state_schema so importing shared_kernel never cycles into
    # capsule_engine.
    "TicketState",
    "TICKET_TRANSITIONS",
    "TICKET_TERMINAL_STATES",
    "TICKET_ACTIVE_STATES",
    "can_transition",
    "parse_utc",
]
