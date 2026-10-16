"""Two v1 ticket decisions at once: one move, the loser re-resolves.

A phone double-tap fires two ``POST /v1/tickets/{id}/decision`` calls for the
same ticket at nearly the same instant. Both calls resolve the ticket *before*
either writer takes the lock, so the losing call's atomic ``update_json`` finds
the source gone — even though the ticket did not vanish, it moved. Before the
fix in :mod:`orchestration.v1.service` that losing tap answered ``404
NOT_FOUND``: a spurious "the ticket is gone" for a ticket the same person had
just accepted.

The service now re-resolves the ticket **where it now lives** (read-only — the
winner's atomic write is already visible and complete) and reproduces the
writer's own classification:

- the same ``idempotency_key`` and request fingerprint replays the original
  outcome (``replayed: true``, same ``decision_id``);
- a genuinely different key/actor is ``ALREADY_DECIDED`` naming who decided;
- the ticket truly absent everywhere is still ``404 NOT_FOUND``.

These tests force the race deterministically: ``ticket_mgr.find_ticket`` is
wrapped so that every racer resolves the pre-move ticket and only then is
released — the exact window a double-tap hits. Each scenario runs 20 times to
catch flakiness, and asserts the ticket carries exactly one decision record
afterwards (the loser re-resolves; it never writes a second move).

Everything runs in the bootstrap vault and removes what it creates.
"""

from __future__ import annotations  # noqa: E402  — the suite runs on 3.9

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import json
import threading
from unittest import mock

from tests._bootstrap import DOMAINS

from capsule_engine.capabilities import mint_key
from tests.test_v1_ticket_gate_decisions import (
    SECTOR,
    TEST_SECRET,
    V1TicketGateTestCase,
)

#: How many times each race is repeated to smoke out flakiness.
ROUNDS = 20


def _key_as(subject: str, *grants: str) -> str:
    """A capability key for ``subject`` (so a race can have distinct actors)."""
    return mint_key(
        subject,
        list(grants),
        secret=TEST_SECRET.encode("utf-8"),
        ttl_seconds=600,
    )


class V1ConcurrencyTestCase(V1TicketGateTestCase):
    """Vault helpers shared by the concurrency races."""

    def _decision_records(self, ticket_id: str) -> list:
        """Every decision record on disk for this ticket, wherever it lives."""
        records: list = []
        tickets_root = DOMAINS / SECTOR / "tickets"
        for path in tickets_root.rglob(f"ticket_{ticket_id}*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            records.extend(data.get("decisions") or [])
        return records


class ConcurrentTicketDecision(V1ConcurrencyTestCase):
    """The double-tap race: exactly one move, the loser re-resolves."""

    # ---- the deterministic race harness ----------------------------------- #

    def _race(self, ticket_id: str, *, action: str, reviewed, actors, keys, reason=None):
        """Run one decision per actor concurrently, all resolving pre-move.

        ``ticket_mgr.find_ticket`` is wrapped so the first ``len(actors)``
        sector-scoped resolves (each writer's pre-update resolve) rendezvous on a
        barrier. Only after every racer has captured the *same* pre-move ticket
        does any of them reach its atomic update — the losing tap's source is
        then already gone, which is precisely the bug window. The recovery path
        re-resolves with a sector too, but is past the first ``len(actors)``
        calls, so it never blocks.
        """
        real_find = self.ticket_mgr.find_ticket
        barrier = threading.Barrier(len(actors))
        counter_lock = threading.Lock()
        counted = {"n": 0}

        def synced_find(tid, sector=None):
            result = real_find(tid, sector)
            if sector is not None:
                with counter_lock:
                    is_racer_resolve = counted["n"] < len(actors)
                    counted["n"] += 1
                if is_racer_resolve:
                    barrier.wait(timeout=30)
            return result

        results: list = [None] * len(actors)

        def go(index: int, subject: str, idem_key: str) -> None:
            headers = {
                "Authorization": "Bearer "
                + _key_as(subject, "factory:read", f"ticket:create:{SECTOR}")
            }
            body = {"action": action, "idempotency_key": idem_key}
            if reviewed is not None:
                body["reviewed"] = reviewed
            if reason is not None:
                body["reason"] = reason
            results[index] = self.client.post(
                f"/v1/tickets/{ticket_id}/decision", headers=headers, json=body
            )

        with mock.patch.object(self.ticket_mgr, "find_ticket", synced_find):
            threads = [
                threading.Thread(target=go, args=(i, actors[i], keys[i]))
                for i in range(len(actors))
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        self.assertTrue(
            all(result is not None for result in results),
            "a racer died without answering",
        )
        return results

    # ---- promote: same key ------------------------------------------------ #

    def test_promote_double_tap_replays_and_moves_exactly_once(self) -> None:
        for _ in range(ROUNDS):
            ticket_id, _ = self._new_ticket()
            reviewed = self._echo(self._review_ticket(ticket_id))

            results = self._race(
                ticket_id,
                action="promote",
                reviewed=reviewed,
                actors=["double-tap", "double-tap"],
                keys=["tap-1", "tap-1"],
            )

            self.assertEqual(
                sorted(r.status_code for r in results), [200, 200],
                [r.text for r in results],
            )
            self.assertEqual(
                sorted(r.json()["replayed"] for r in results), [False, True]
            )
            winner = next(r.json() for r in results if not r.json()["replayed"])
            replay = next(r.json() for r in results if r.json()["replayed"])

            # The replay reports the original outcome, not a fresh one.
            self.assertEqual(replay["decision_id"], winner["decision_id"])
            self.assertEqual(replay["decided_at"], winner["decided_at"])
            self.assertEqual(replay["stage_after"], "approved")

            # Exactly one move, exactly one recorded decision.
            self.assertFalse(self._ticket_path(ticket_id, "scratch").exists())
            self.assertTrue(self._ticket_path(ticket_id, "approved").exists())
            self.assertEqual(len(self._decision_records(ticket_id)), 1)

    # ---- promote: different keys ----------------------------------------- #

    def test_promote_with_different_keys_one_wins_the_rest_is_already_decided(self) -> None:
        for _ in range(ROUNDS):
            ticket_id, _ = self._new_ticket()
            reviewed = self._echo(self._review_ticket(ticket_id))
            actors = ["racer-a", "racer-b"]

            results = self._race(
                ticket_id,
                action="promote",
                reviewed=reviewed,
                actors=actors,
                keys=["key-a", "key-b"],
            )

            winners = [i for i, r in enumerate(results) if r.status_code == 200]
            losers = [i for i, r in enumerate(results) if r.status_code != 200]
            self.assertEqual(len(winners), 1, [r.text for r in results])
            self.assertEqual(len(losers), 1, [r.text for r in results])

            winner = results[winners[0]].json()
            loser = results[losers[0]].json()
            self.assertFalse(winner["replayed"])
            self.assertEqual(loser["error"]["code"], "ALREADY_DECIDED")

            details = loser["error"]["details"]
            self.assertEqual(details["decided_by"], actors[winners[0]])
            self.assertEqual(details["decided_at"], winner["decided_at"])
            self.assertEqual(
                details["decision"]["decision_id"], winner["decision_id"]
            )
            self.assertEqual(details["decision"]["action"], "promote")

            self.assertEqual(len(self._decision_records(ticket_id)), 1)

    # ---- promote: same key, different actor ------------------------------ #

    def test_promote_same_key_different_actor_is_a_reused_key(self) -> None:
        # The actor is part of the request fingerprint, so a key presented by a
        # *different* credential is a reused key, not a replay of someone else's
        # decision — the exact refusal the in-lock writer already makes.
        for _ in range(ROUNDS):
            ticket_id, _ = self._new_ticket()
            reviewed = self._echo(self._review_ticket(ticket_id))

            results = self._race(
                ticket_id,
                action="promote",
                reviewed=reviewed,
                actors=["human-a", "human-b"],
                keys=["shared", "shared"],
            )

            winners = [r for r in results if r.status_code == 200]
            losers = [r for r in results if r.status_code == 409]
            self.assertEqual(len(winners), 1, [r.text for r in results])
            self.assertEqual(len(losers), 1, [r.text for r in results])
            self.assertEqual(losers[0].json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
            self.assertEqual(len(self._decision_records(ticket_id)), 1)

    # ---- demote (send-back): same key ------------------------------------ #

    def test_send_back_double_tap_replays_and_moves_exactly_once(self) -> None:
        for _ in range(ROUNDS):
            ticket_id, _ = self._new_ticket(stage="approved")

            results = self._race(
                ticket_id,
                action="demote",
                reviewed=None,
                reason="Not ready to publish.",
                actors=["double-tap", "double-tap"],
                keys=["back-1", "back-1"],
            )

            self.assertEqual(
                sorted(r.status_code for r in results), [200, 200],
                [r.text for r in results],
            )
            self.assertEqual(
                sorted(r.json()["replayed"] for r in results), [False, True]
            )
            winner = next(r.json() for r in results if not r.json()["replayed"])
            replay = next(r.json() for r in results if r.json()["replayed"])
            self.assertEqual(replay["decision_id"], winner["decision_id"])
            self.assertEqual(replay["decided_at"], winner["decided_at"])

            self.assertFalse(self._ticket_path(ticket_id, "approved").exists())
            self.assertTrue(self._ticket_path(ticket_id, "scratch").exists())
            self.assertEqual(len(self._decision_records(ticket_id)), 1)

    # ---- demote (send-back): different keys ------------------------------ #

    def test_send_back_with_different_keys_one_wins_the_rest_is_already_decided(self) -> None:
        for _ in range(ROUNDS):
            ticket_id, _ = self._new_ticket(stage="approved")
            actors = ["racer-a", "racer-b"]

            results = self._race(
                ticket_id,
                action="demote",
                reviewed=None,
                reason="Needs rework.",
                actors=actors,
                keys=["back-a", "back-b"],
            )

            winners = [i for i, r in enumerate(results) if r.status_code == 200]
            losers = [i for i, r in enumerate(results) if r.status_code != 200]
            self.assertEqual(len(winners), 1, [r.text for r in results])
            self.assertEqual(len(losers), 1, [r.text for r in results])

            winner = results[winners[0]].json()
            loser = results[losers[0]].json()
            self.assertEqual(loser["error"]["code"], "ALREADY_DECIDED")
            self.assertEqual(
                loser["error"]["details"]["decided_by"], actors[winners[0]]
            )
            self.assertEqual(
                loser["error"]["details"]["decision"]["decision_id"],
                winner["decision_id"],
            )
            self.assertEqual(loser["error"]["details"]["decision"]["action"], "demote")

            self.assertEqual(len(self._decision_records(ticket_id)), 1)


class ConcurrentRecoveryIsReadOnly(V1ConcurrencyTestCase):
    """The recovery never writes: a truly absent ticket is still a 404."""

    def test_unknown_ticket_is_still_not_found(self) -> None:
        headers = {
            "Authorization": "Bearer "
            + _key_as("nobody", "factory:read", f"ticket:create:{SECTOR}")
        }
        response = self.client.post(
            "/v1/tickets/T-DOES-NOT-EXIST/decision",
            headers=headers,
            json={
                "action": "promote",
                "reviewed": {
                    "algorithm": "sha256",
                    "elements": {},
                    "unviewed": [],
                    "primary_sha256": None,
                },
                "idempotency_key": "gone",
            },
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")


# --------------------------------------------------------------------------- #
# legacy /api routes are unchanged: still unattested, still idempotent
# --------------------------------------------------------------------------- #


class LegacyTicketRoutesUnchanged(V1ConcurrencyTestCase):
    def test_legacy_promote_and_demote_still_work_unattested(self) -> None:
        ticket_id, _ = self._new_ticket()

        promote = self.client.post(
            f"/api/tickets/{ticket_id}/promote", headers=self.ticket_headers
        )
        self.assertEqual(promote.status_code, 200, promote.text)
        self.assertEqual(promote.json()["status"], "success")

        record = self._decision_records(ticket_id)[-1]
        self.assertFalse(record["attested"], "legacy records stay unattested")
        self.assertIsNone(record["key"], "legacy records carry no idempotency key")

        # A sequential re-promote is the same no-op it always was.
        again = self.client.post(
            f"/api/tickets/{ticket_id}/promote", headers=self.ticket_headers
        )
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["status"], "already_approved")

        # And a legacy send-back still moves it, still unattested.
        demote = self.client.post(
            f"/api/tickets/{ticket_id}/demote", headers=self.ticket_headers
        )
        self.assertEqual(demote.status_code, 200, demote.text)
        self.assertEqual(demote.json()["status"], "success")
        self.assertFalse(self._decision_records(ticket_id)[-1]["attested"])


if __name__ == "__main__":
    import unittest

    unittest.main()
