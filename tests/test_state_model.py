"""Transition tests: one enum + one allowed-transitions table per entity.

Ticket vocabulary lives in shared_kernel (TicketState / TICKET_TRANSITIONS);
the capsule vocabulary is the existing engine enum (capsule_engine.manifest
State / TRANSITIONS), re-exported lazily as CapsuleState / CAPSULE_TRANSITIONS.
"""

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import unittest

from pydantic import ValidationError

import shared_kernel.state_schema as state_schema
from capsule_engine.manifest import State as CapsuleState
from capsule_engine.manifest import TRANSITIONS as CAPSULE_TRANSITIONS
from shared_kernel.state_schema import (
    TICKET_ACTIVE_STATES,
    TICKET_TERMINAL_STATES,
    TICKET_TRANSITIONS,
    Ticket,
    TicketState,
    can_transition,
)


def _ticket(**fields):
    base = {
        "ticket_id": "T-1",
        "client_id": "c",
        "request": "probe request",
    }
    base.update(fields)
    return Ticket(**base)


class TicketStateTests(unittest.TestCase):
    def test_vocabulary_matches_the_writers_on_disk(self):
        self.assertEqual(
            {s.value for s in TicketState},
            {
                "draft",            # api create_ticket (scratch) / ticket_mgr
                "open",             # trackers / dispatcher spawn / ticket_mgr promote
                "pending",          # dispatcher: failed attempt, lease reclaim
                "in_progress",      # dispatcher lease
                "pending_approval", # human gate (auto_ideator, api, ticket_mgr)
                "needs_revision",   # dispatcher / revise
                "approved",         # dispatcher result
                "published",        # dispatcher result
                "done",             # dispatcher result / resolve
                "settled",          # api settle bridge
            },
        )

    def test_pending_and_pending_approval_stay_distinct(self):
        # Decision: NOT merged. `pending` is the dispatcher's retry queue;
        # `pending_approval` is the human gate. They mean different things.
        self.assertNotEqual(TicketState.PENDING, TicketState.PENDING_APPROVAL)
        self.assertIn(TicketState.PENDING, TICKET_ACTIVE_STATES)
        self.assertIn(TicketState.PENDING_APPROVAL, TICKET_ACTIVE_STATES)

    def test_parse_roundtrip_and_rejection(self):
        for state in TicketState:
            self.assertIs(TicketState.parse(state.value), state)
        with self.assertRaises(ValueError):
            TicketState.parse("bogus")

    def test_ticket_model_accepts_states_the_api_actually_writes(self):
        # Regression: the old 8-state regex rejected `draft` (create/scratch,
        # ticket_mgr) and `settled` (settle bridge) — tickets the API writes.
        for state in ("draft", "settled", "open", "pending", "pending_approval"):
            ticket = _ticket(state=state)
            self.assertEqual(ticket.state.value, state)

    def test_ticket_model_rejects_capsule_only_states(self):
        for state in ("stalled", "failed", "archived", "in_review",
                      "pending_qa", "pending_design", "bogus"):
            with self.assertRaises(ValidationError):
                _ticket(state=state)

    def test_validated_state_is_still_a_plain_string(self):
        # On-disk / JSON interop: a str-enum compares and serialises as str.
        ticket = _ticket(state="open")
        self.assertEqual(ticket.state, "open")
        self.assertIn(ticket.state, ("open", "pending"))


class TicketTransitionsTests(unittest.TestCase):
    def test_table_covers_every_state(self):
        self.assertEqual(set(TICKET_TRANSITIONS), set(TicketState))

    def test_targets_are_members_and_never_self_loops(self):
        for src, targets in TICKET_TRANSITIONS.items():
            self.assertIsInstance(src, TicketState)
            for dst in targets:
                self.assertIsInstance(dst, TicketState)
                self.assertNotEqual(dst, src, f"self-loop on {src}")

    def test_terminal_and_active_partition_the_enum(self):
        self.assertEqual(
            TICKET_TERMINAL_STATES | TICKET_ACTIVE_STATES, set(TicketState)
        )
        self.assertEqual(
            TICKET_TERMINAL_STATES & TICKET_ACTIVE_STATES, frozenset()
        )
        self.assertEqual(
            TICKET_TERMINAL_STATES,
            frozenset({TicketState.DONE, TicketState.SETTLED, TicketState.PUBLISHED}),
        )

    def test_observed_edges_are_lawful(self):
        lawful = [
            ("draft", "open"),                  # ticket_mgr promote
            ("draft", "done"),                  # human resolve
            ("open", "in_progress"),            # dispatcher claim
            ("open", "settled"),                # settle bridge
            ("open", "needs_revision"),         # human revise
            ("open", "draft"),                  # cockpit demote
            ("pending", "in_progress"),         # retried
            ("in_progress", "done"),            # worker: done
            ("in_progress", "pending_approval"),# worker: at the human gate
            ("in_progress", "approved"),        # worker: approved
            ("in_progress", "published"),       # worker: published
            ("in_progress", "needs_revision"),  # attempt cap / gate rejection
            ("in_progress", "pending"),         # lease reclaim -> retry queue
            ("needs_revision", "in_progress"),  # re-dispatch after rework
            ("pending_approval", "needs_revision"),
            ("approved", "done"),
            ("approved", "draft"),              # demote
        ]
        for frm, to in lawful:
            self.assertTrue(can_transition(frm, to), f"{frm} -> {to}")

    def test_unlawful_edges_are_refused(self):
        unlawful = [
            ("done", "open"),          # terminal
            ("settled", "open"),       # terminal
            ("published", "done"),     # terminal
            ("open", "published"),     # publish only comes from in_progress
            ("draft", "in_progress"),  # dispatch never claims a scratch draft
            ("bogus", "open"),         # unknown source
            ("open", "bogus"),         # unknown target
        ]
        for frm, to in unlawful:
            self.assertFalse(can_transition(frm, to), f"{frm} -> {to}")


class CapsuleStateTests(unittest.TestCase):
    def test_alias_reuses_the_existing_engine_enum(self):
        # No second capsule enum: shared_kernel re-exports the engine's.
        self.assertIs(state_schema.CapsuleState, CapsuleState)
        self.assertIs(state_schema.CAPSULE_TRANSITIONS, CAPSULE_TRANSITIONS)

    def test_capsule_table_integrity(self):
        self.assertEqual(set(CAPSULE_TRANSITIONS), set(CapsuleState))
        for src, targets in CAPSULE_TRANSITIONS.items():
            self.assertIsInstance(src, CapsuleState)
            for dst in targets:
                self.assertIsInstance(dst, CapsuleState)

    def test_vocabularies_are_separate_entities(self):
        # Capsule-only states must never validate on a Ticket, and the old
        # regex's ticket-only words must never parse as capsule states.
        for state in ("stalled", "pending_qa", "in_review"):
            with self.assertRaises(ValidationError):
                _ticket(state=state)
        for state in ("settled", "open", "pending", "draft2"):
            with self.assertRaises(ValueError):
                CapsuleState.parse(state)


if __name__ == "__main__":
    unittest.main()
