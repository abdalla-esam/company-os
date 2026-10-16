"""M1a: ``/v1/tickets`` and ``/v1/tickets/{id}`` — the request list and its drill-down.

The home's ticket tile has to answer "how many requests wait for a human, and
which ones?" for one sector at a time, then let the app open one. This module
pins:

- the list carries the indexed common fields (stage, state, needs-me, can-act)
  and filters by sector, stage and state — refusing a value outside each closed
  vocabulary rather than ignoring it;
- the detail is **one file, read once**, and reports the ticket's own steps and
  its decision ledger as they are written on disk (including the decisions a
  pre-attestation writer left ``attested: false``);
- with the index switched off, the list and the detail answer the same thing
  from the files, and say so in ``source``;
- reads are scoped: the other sector's ticket answers ``404``, identical to a
  missing id, and walking the list neither repeats nor skips a row.
"""

from __future__ import annotations  # noqa: E402 — the suite runs on 3.9

from tests.v1_reads_fixtures import (  # noqa: E402
    DEVELOPMENT,
    MARKETING,
    MISSING_TICKET,
    V1ReadsCase,
)

import json
import os
from unittest import mock

from orchestration import read_index
from tests._bootstrap import DOMAINS


def _ticket_ids(body: dict) -> list:
    return [item["ticket_id"] for item in body["items"]]


def _without_age(value):
    """Drop the fields derived from *when the request was served*."""
    if isinstance(value, list):
        return [_without_age(entry) for entry in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _without_age(entry)
        for key, entry in value.items()
        if key not in ("as_of", "age_seconds", "source")
    }


class TicketsList(V1ReadsCase):
    def test_the_list_carries_common_fields_and_links(self) -> None:
        body = self.json_of("/v1/tickets", self.marketing_approver, limit=100)
        item = next(
            entry for entry in body["items"] if entry["ticket_id"] == self.marketing_ticket
        )
        self.assertEqual(item["sector"], MARKETING)
        self.assertEqual(item["stage"], "scratch")
        self.assertEqual(item["state"], "draft")
        self.assertTrue(item["awaiting_decision"])
        self.assertTrue(item["needs_me"])
        self.assertTrue(item["can_act"])
        self.assertTrue(item["title"])
        self.assertIn("age_seconds", item)
        self.assertEqual(item["links"]["self"], f"/v1/tickets/{self.marketing_ticket}")
        self.assertEqual(body["order"], ["created_at desc", "ticket_id desc"])
        # No path, no whole file: the list is indexed fields plus links.
        serialized = json.dumps(body)
        self.assertNotIn("tickets/scratch", serialized)
        self.assertNotIn("cadence", serialized)

    def test_needs_me_follows_the_credential_not_the_item(self) -> None:
        approver = self.json_of("/v1/tickets", self.marketing_approver, limit=100)
        reader = self.json_of("/v1/tickets", self.marketing_headers, limit=100)
        self.assertEqual(_ticket_ids(approver), _ticket_ids(reader))
        by_id = {item["ticket_id"]: item for item in reader["items"]}
        self.assertFalse(by_id[self.marketing_ticket]["needs_me"])
        self.assertTrue(by_id[self.marketing_ticket]["awaiting_decision"])
        self.assertFalse(by_id[self.marketing_ticket]["can_act"])

    def test_filters_by_sector_stage_and_state(self) -> None:
        by_sector = self.json_of(
            "/v1/tickets", self.root_headers, sector=MARKETING, limit=100
        )
        self.assertTrue(by_sector["items"])
        self.assertTrue(all(item["sector"] == MARKETING for item in by_sector["items"]))
        self.assertEqual(by_sector["filters"]["sector"], MARKETING)

        scratch = self.json_of("/v1/tickets", self.root_headers, stage="scratch", limit=100)
        self.assertIn(self.marketing_ticket, _ticket_ids(scratch))
        self.assertNotIn(self.gated_ticket, _ticket_ids(scratch))

        approved = self.json_of("/v1/tickets", self.root_headers, stage="approved", limit=100)
        self.assertIn(self.gated_ticket, _ticket_ids(approved))
        self.assertNotIn(self.marketing_ticket, _ticket_ids(approved))

        drafts = self.json_of("/v1/tickets", self.root_headers, state="draft", limit=100)
        self.assertTrue(all(item["state"] == "draft" for item in drafts["items"]))
        self.assertIn(self.marketing_ticket, _ticket_ids(drafts))

        empty = self.json_of("/v1/tickets", self.root_headers, stage="archive", limit=100)
        self.assertEqual(empty["items"], [], "nothing archived in the fixtures")

    def test_a_bad_filter_or_cursor_is_refused(self) -> None:
        for params in (
            {"stage": "nope"},
            {"state": "nope"},
            {"limit": 0},
            {"limit": 101},
            {"cursor": "!!!"},
        ):
            with self.subTest(params=params):
                response = self.get("/v1/tickets", self.root_headers, **params)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(
                    response.json()["error"]["code"], "VALIDATION_ERROR"
                )

    def test_every_row_belongs_to_a_visible_sector(self) -> None:
        for headers, allowed in (
            (self.marketing_headers, {MARKETING}),
            (self.development_headers, {DEVELOPMENT}),
            (self.both_headers, {MARKETING, DEVELOPMENT}),
        ):
            with self.subTest(allowed=allowed):
                body = self.json_of("/v1/tickets", headers, limit=100)
                self.assertTrue(
                    {item["sector"] for item in body["items"]}.issubset(allowed)
                )

    def test_a_scoped_filter_never_widens_visibility(self) -> None:
        body = self.json_of(
            "/v1/tickets", self.marketing_headers, sector=DEVELOPMENT, limit=100
        )
        self.assertEqual(body["items"], [])

    def test_paging_walks_the_whole_list_once(self) -> None:
        present = _ticket_ids(self.json_of("/v1/tickets", self.root_headers, limit=100))
        walked: list = []
        cursor = None
        for page_number in range(20):
            params = {"limit": 1}
            if cursor:
                params["cursor"] = cursor
            body = self.json_of("/v1/tickets", self.root_headers, **params)
            walked.extend(_ticket_ids(body))
            cursor = body["page"]["next_cursor"]
            if page_number == 0:
                # A ticket created after the walk starts is newer than every row
                # already seen, so it must not shift the remaining pages.
                late = self.ticket_mgr.create_ticket(
                    sector=MARKETING,
                    title="A late arrival",
                    content="A request created mid-walk.",
                )
                self._created_tickets.append((MARKETING, late["ticket_id"]))
                read_index.rebuild()
            if cursor is None:
                break
        else:
            self.fail("the cursor walk never reached the end")
        self.assertEqual(len(walked), len(set(walked)), "a page repeated a row")
        self.assertEqual(sorted(walked), sorted(present), "and none was skipped")

    def test_index_off_serves_the_same_list_from_files(self) -> None:
        indexed = self.json_of("/v1/tickets", self.root_headers, limit=100)
        with mock.patch.dict(os.environ, {"AGENCY_INDEX": "off"}):
            from_files = self.json_of("/v1/tickets", self.root_headers, limit=100)
            detail_from_files = self.json_of(
                f"/v1/tickets/{self.marketing_ticket}", self.root_headers
            )
        self.assertEqual(indexed["source"], "index")
        self.assertEqual(from_files["source"], "files")
        self.assertEqual(_ticket_ids(indexed), _ticket_ids(from_files))
        # Identical rows, in the same order: only the clock each response was
        # stamped with (and the age derived from it) may differ.
        self.assertEqual(_without_age(indexed["items"]), _without_age(from_files["items"]))

        detail = self.json_of(f"/v1/tickets/{self.marketing_ticket}", self.root_headers)
        self.assertEqual(detail["source"], "index")
        self.assertEqual(detail_from_files["source"], "files")
        self.assertEqual(
            _without_age(detail), _without_age(detail_from_files)
        )
        self.assertTrue(read_index.enabled(), "the switch is restored")


class TicketDetail(V1ReadsCase):
    def test_the_detail_reports_the_file_as_it_is(self) -> None:
        body = self.json_of(f"/v1/tickets/{self.marketing_ticket}", self.marketing_approver)
        self.assertEqual(body["ticket_id"], self.marketing_ticket)
        self.assertEqual(body["sector"], MARKETING)
        self.assertEqual(body["stage"], "scratch")
        self.assertEqual(body["state"], "draft")
        self.assertEqual(body["title"], "A request awaiting a decision")
        self.assertIn("A request body the reviewer would attest to", body["request"])
        self.assertFalse(body["request_truncated"])
        self.assertTrue(body["awaiting_decision"])
        self.assertTrue(body["can_act"])
        self.assertEqual(body["links"]["review"], f"/v1/tickets/{self.marketing_ticket}/review")
        # The creation step the writer recorded, normalised to from/to. The
        # vault's creation entry records the *state* it was born in (``draft``)
        # while a decision entry records the *stage* it moved to; both are
        # reported as written rather than rewritten into one vocabulary.
        self.assertEqual(len(body["steps"]), 1, body["steps"])
        self.assertIsNone(body["steps"][0]["from"])
        self.assertEqual(body["steps"][0]["to"], "draft")
        self.assertTrue(body["steps"][0]["by"])
        self.assertEqual(body["decisions"], [], "no decision was taken")
        self.assertIsNone(body["gate_id"])

    def test_the_detail_shows_the_steps_and_the_decision_ledger(self) -> None:
        ticket_id = self.marketing_ticket
        # A real (pre-attestation) acceptance, written by the ticket writer.
        self.ticket_mgr.promote_ticket(ticket_id, MARKETING)
        read_index.rebuild()

        body = self.json_of(f"/v1/tickets/{ticket_id}", self.marketing_approver)
        self.assertEqual(body["stage"], "approved")
        self.assertEqual(body["state"], "open")
        self.assertFalse(body["awaiting_decision"], "an accepted ticket is not pending")
        self.assertTrue(body["can_act"])
        self.assertNotIn("needs_me", body, "needs-me is a list concern, not a detail one")

        steps = body["steps"]
        self.assertEqual(len(steps), 2, steps)
        self.assertIsNone(steps[0]["from"], "creation has no previous position")
        self.assertEqual(steps[0]["to"], "draft")
        self.assertEqual(steps[-1]["by"], "ticket_mgr:promote")
        self.assertEqual(steps[-1]["from"], "scratch")
        self.assertEqual(steps[-1]["to"], "approved")
        self.assertIn("promoted", steps[-1]["why"])

        decisions = body["decisions"]
        self.assertEqual(len(decisions), 1, decisions)
        self.assertEqual(decisions[0]["action"], "promote")
        self.assertEqual(decisions[0]["ticket_id"], ticket_id)
        self.assertEqual(decisions[0]["stage_before"], "scratch")
        self.assertEqual(decisions[0]["stage_after"], "approved")
        self.assertFalse(decisions[0]["attested"], "the legacy writer attests nothing")
        self.assertEqual(decisions[0]["elements"], {})
        self.assertEqual(decisions[0]["actor"], "ticket_mgr:promote")
        self.assertTrue(decisions[0]["at"])

    def test_the_detail_is_read_only(self) -> None:
        path = _find_ticket_file(MARKETING, self.marketing_ticket)
        before = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        self.json_of(f"/v1/tickets/{self.marketing_ticket}", self.root_headers)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_the_other_sector_answers_the_missing_body(self) -> None:
        invisible = self.get(f"/v1/tickets/{self.development_ticket}", self.marketing_headers)
        missing = self.get(f"/v1/tickets/{MISSING_TICKET}", self.marketing_headers)
        self.assertEqual(invisible.status_code, 404, invisible.text)
        self.assertNotEqual(invisible.status_code, 403)
        self.assertEqual(
            invisible.json()["error"]["code"], missing.json()["error"]["code"]
        )
        self.assertEqual(
            invisible.json()["detail"].replace(self.development_ticket, "<ID>"),
            missing.json()["detail"].replace(MISSING_TICKET, "<ID>"),
        )

    def test_a_missing_id_is_the_same_answer_with_the_index_off(self) -> None:
        with mock.patch.dict(os.environ, {"AGENCY_INDEX": "off"}):
            missing = self.get(f"/v1/tickets/{MISSING_TICKET}", self.root_headers)
            invisible = self.get(f"/v1/tickets/{self.development_ticket}", self.marketing_headers)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(invisible.status_code, 404)
        self.assertEqual(
            missing.json()["error"]["code"], invisible.json()["error"]["code"]
        )

    def test_a_long_request_is_bounded_and_says_so(self) -> None:
        from ops.tools import ticket_mgr

        long_text = "x" * 5000
        created = ticket_mgr.create_ticket(
            sector=MARKETING, title="A long request", content=long_text
        )
        ticket_id = created["ticket_id"]
        self._created_tickets.append((MARKETING, ticket_id))
        read_index.rebuild()
        body = self.json_of(f"/v1/tickets/{ticket_id}", self.marketing_approver)
        self.assertTrue(body["request_truncated"])
        self.assertLessEqual(len(body["request"]), 4000)


def _find_ticket_file(sector: str, ticket_id: str):
    root = DOMAINS / sector / "tickets"
    matches = sorted(root.rglob(f"ticket_{ticket_id}*.json"))
    if not matches:
        raise AssertionError(f"no ticket file for {ticket_id} under {root}")
    return matches[0]


if __name__ == "__main__":
    import unittest

    unittest.main()
