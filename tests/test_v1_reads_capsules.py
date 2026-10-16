"""M1a: ``/v1/capsules`` and ``/v1/capsules/{id}`` — the work list and drill-down.

What this module proves:

- the detail carries **each gate's bound artifact hashes**, and for the gate
  being decided they are exactly the hashes the review endpoint reports — the
  values a decision will later be checked against (serialized in the ``/v1``
  layer, because ``GateRecord.to_view()`` deliberately omits them);
- the **approval chain** reports who approved what, merging the manifest's
  decision ledger with the gate records it does not cover, and marks a decision
  taken before attestation existed as ``attested: false``;
- the list filters (domain, stage, state, needs-me) and pages by opaque cursor:
  a walk that overlaps a concurrent insert neither repeats nor skips a row that
  was present for the whole walk;
- every read is scoped — the other sector's objects answer ``404`` identical to
  a missing id — and mutates nothing.

The sign-offs the chain reports are written by the real approval service in
``setUp``: a fixture step, not an HTTP request under test.
"""

from __future__ import annotations  # noqa: E402 — the suite runs on 3.9

from tests.v1_reads_fixtures import (  # noqa: E402
    DEVELOPMENT,
    MARKETING,
    MISSING_CAPSULE,
    TEST_SECRET,
    V1ReadsCase,
    create_capsule,
    key,
    record_trace_steps,
    seed_human_gate,
)

import json
import os

from capsule_engine import gates as gate_engine
from capsule_engine.capabilities import verify_key
from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import State, load_manifest, save_manifest
from orchestration import access_control, approval, read_index
from orchestration.v1 import messages
from tests._bootstrap import SPOOL


def _approve_directly(path, *, reviewed=None, reason: str = "approved by the fixture"):
    """Sign one capsule off through the real service (fixture, not a request).

    ``reviewed`` is the payload the review endpoint reports (and what a mobile
    client echoes back); omitting it is the pre-attestation decision the legacy
    routes make.
    """
    principal = access_control.Principal.from_key(
        verify_key(
            key(
                "chain-approver",
                f"domain:read:{MARKETING}",
                f"capsule:approve:{MARKETING}",
                f"capsule:reject:{MARKETING}",
            ),
            secret=TEST_SECRET.encode("utf-8"),
        )
    )
    return approval.approve(
        path,
        principal=principal,
        reviewer=principal.subject,
        audit_note=principal.audit_note(),
        attestation=reviewed,
        require_attestation=reviewed is not None,
        reason=reason,
    )


class CapsuleDetail(V1ReadsCase):
    def test_detail_reports_state_stage_and_revision_position(self) -> None:
        body = self.json_of(f"/v1/capsules/{self.halted.name}", self.marketing_approver)
        self.assertEqual(body["capsule_id"], self.halted.name)
        self.assertEqual(body["domain"], MARKETING)
        self.assertEqual(body["state"], "failed")
        self.assertEqual(body["stage"], "halted")
        self.assertEqual(body["stage_label_en"], "Halted")
        self.assertTrue(body["stage_label"], "Arabic is the default label")
        self.assertEqual(body["revision_count"], 4)
        self.assertEqual(body["revision_cap"], 3)
        self.assertEqual(body["source"], "files")
        self.assertEqual(body["links"]["trace"], f"/v1/capsules/{self.halted.name}/trace")
        self.assertIn("tokens", body["accounting"])
        self.assertIn("calls", body["accounting"])
        # Nothing invented, nothing raw: no path, no manifest blob.
        self.assertNotIn("path", json.dumps(body))
        self.assertNotIn("raw_prompt", json.dumps(body))

    def test_a_planless_capsule_reports_no_gates_and_invents_none(self) -> None:
        path = self.working
        with CapsuleLock(path):
            manifest = load_manifest(path)
            manifest["gates"] = []
            save_manifest(path, manifest)
        body = self.json_of(f"/v1/capsules/{path.name}", self.marketing_approver)
        self.assertEqual(body["gates"], [])
        self.assertEqual(body["approval_chain"], [])
        self.assertIsNone(body["gate_plan"])
        self.assertEqual(body["state"], "in_progress"), "the rest still answers"


class BoundArtifactsMatchTheReviewEndpoint(V1ReadsCase):
    def test_the_gate_being_decided_reports_the_hashes_the_decision_checks(self) -> None:
        detail = self.json_of(f"/v1/capsules/{self.gated.name}", self.marketing_approver)
        review = self.json_of(
            f"/v1/capsules/{self.gated.name}/review", self.marketing_approver
        )
        self.assertEqual(review["active_gate"]["gate_id"], self.gated_gate)

        gate = next(
            gate for gate in detail["gates"] if gate["gate_id"] == self.gated_gate
        )
        self.assertTrue(gate["artifacts"], "an opened gate has bound artifacts")
        self.assertEqual(
            gate["artifacts"],
            review["reviewed"]["elements"],
            "the detail must report exactly the hashes a decision is checked against",
        )
        self.assertEqual(gate["authority"], "capsule:approve")
        self.assertEqual(gate["status"], "pending")
        self.assertIn("opened_at", gate)
        self.assertIn("at", gate)

        # The engine's own view helper is untouched: it still omits the hashes.
        manifest = load_manifest(SPOOL / self.gated.name)
        record = next(
            r for r in gate_engine.load_records(manifest) if r.gate_id == self.gated_gate
        )
        self.assertNotIn("artifacts", record.to_view())

    def test_outputs_and_receipts_are_reported_by_reference_only(self) -> None:
        path = self.published
        with CapsuleLock(path):
            manifest = load_manifest(path)
            manifest["outputs"] = [
                {
                    "name": "copy",
                    "path": "copy.md",
                    "kind": "text",
                    "sha256": "c" * 64,
                    "media_type": "text/markdown",
                    "primary": True,
                },
                # A blob the vault would never write on purpose: the projection
                # must still refuse to inline it (an image is fetched through the
                # review endpoint, which is what hashes it for a decision).
                {
                    "name": "cover",
                    "path": "cover.png",
                    "kind": "image",
                    "sha256": "d" * 64,
                    "image_base64": "QUJD",
                },
            ]
            manifest["receipts"] = [
                {
                    "at": "2026-01-04T00:00:00+00:00",
                    "status": "published",
                    "channel": "facebook",
                }
            ]
            save_manifest(path, manifest)

        body = self.json_of(f"/v1/capsules/{path.name}", self.marketing_approver)
        self.assertEqual([item["path"] for item in body["outputs"]], ["copy.md", "cover.png"])
        self.assertEqual(body["outputs"][0]["media_type"], "text/markdown")
        self.assertNotIn(
            "image_base64", json.dumps(body), "an image is referenced, never inlined"
        )
        self.assertNotIn("QUJD", json.dumps(body))
        self.assertEqual(body["receipts"][0]["channel"], "facebook")
        self.assertEqual(body["receipts"][0]["status"], "published")

    def test_a_get_never_opens_a_gate_or_moves_a_mtime(self) -> None:
        path = SPOOL / self.gated.name
        before_manifest = (path / "manifest.json").read_bytes()
        before_mtime = (path / "manifest.json").stat().st_mtime_ns
        self.json_of(f"/v1/capsules/{self.gated.name}", self.marketing_approver)
        self.json_of(f"/v1/capsules/{self.gated.name}/review", self.marketing_approver)
        self.assertEqual((path / "manifest.json").read_bytes(), before_manifest)
        self.assertEqual((path / "manifest.json").stat().st_mtime_ns, before_mtime)


class CapsuleListFilters(V1ReadsCase):
    def test_the_work_list_carries_common_fields_only(self) -> None:
        body = self.json_of("/v1/capsules", self.marketing_approver, limit=100)
        item = next(
            entry for entry in body["items"] if entry["capsule_id"] == self.gated.name
        )
        self.assertEqual(item["domain"], MARKETING)
        self.assertEqual(item["stage"], "human")
        self.assertEqual(item["stage_label_en"], "Waiting for you")
        self.assertTrue(item["awaiting_decision"])
        self.assertTrue(item["needs_me"])
        self.assertTrue(item["can_act"])
        self.assertEqual(item["approver"], "capsule:approve")
        self.assertIn("gates", item)
        self.assertIn("links", item)
        serialized = json.dumps(body)
        for forbidden in ("raw_prompt", "image_base64", "\\\\", "manifest.json"):
            self.assertNotIn(forbidden, serialized)
        # Nothing invented: the vault records no due date, so the field is absent.
        self.assertNotIn("due", item)

    def test_filters_by_domain_stage_state_and_needs_me(self) -> None:
        by_domain = self.json_of(
            "/v1/capsules", self.root_headers, domain=MARKETING, limit=100
        )
        self.assertTrue(by_domain["items"])
        self.assertTrue(all(item["domain"] == MARKETING for item in by_domain["items"]))
        self.assertEqual(by_domain["filters"]["domain"], MARKETING)

        by_stage = self.json_of("/v1/capsules", self.root_headers, stage="halted", limit=100)
        self.assertIn(self.halted.name, self.capsule_ids(by_stage))
        self.assertTrue(all(item["state"] == "failed" for item in by_stage["items"]))

        by_state = self.json_of(
            "/v1/capsules", self.root_headers, state="in_progress", limit=100
        )
        self.assertIn(self.working.name, self.capsule_ids(by_state))
        self.assertTrue(
            all(item["state"] == "in_progress" for item in by_state["items"])
        )

        # The two filters compose rather than override: "halted but in progress"
        # is provably empty, and the list says so instead of ignoring one of them.
        contradictory = self.json_of(
            "/v1/capsules", self.root_headers, stage="halted", state="in_progress"
        )
        self.assertEqual(contradictory["items"], [])

        needs_me = self.json_of(
            "/v1/capsules", self.marketing_approver, needs_me="true", limit=100
        )
        self.assertIn(self.gated.name, self.capsule_ids(needs_me))
        self.assertNotIn(self.halted.name, self.capsule_ids(needs_me))
        self.assertTrue(all(item["needs_me"] for item in needs_me["items"]))

        # A reader can decide nothing, so nothing needs *it* — while the same
        # items still appear (can_act false) in /v1/attention.
        reader = self.json_of(
            "/v1/capsules", self.marketing_headers, needs_me="true", limit=100
        )
        self.assertEqual(reader["items"], [])

        not_needing_me = self.json_of(
            "/v1/capsules", self.marketing_approver, needs_me="false", limit=100
        )
        self.assertIn(self.halted.name, self.capsule_ids(not_needing_me))
        self.assertNotIn(self.gated.name, self.capsule_ids(not_needing_me))

    def test_the_unknown_stage_bucket_is_reachable_and_empty(self) -> None:
        body = self.json_of("/v1/capsules", self.root_headers, stage="unknown", limit=100)
        self.assertEqual(body["items"], [])
        overview = self.json_of("/v1/overview", self.root_headers)
        self.assertEqual(overview["pipeline"]["unknown_states"], [])

    def test_a_scoped_filter_never_widens_visibility(self) -> None:
        # A marketing key asking for the development sector gets an empty page,
        # not the other sector's work.
        body = self.json_of(
            "/v1/capsules", self.marketing_headers, domain=DEVELOPMENT, limit=100
        )
        self.assertEqual(body["items"], [])

    def test_bad_filters_and_cursors_are_refused(self) -> None:
        for route, params in (
            ("/v1/capsules", {"stage": "nope"}),
            ("/v1/capsules", {"state": "nope"}),
            ("/v1/capsules", {"limit": 0}),
            ("/v1/capsules", {"limit": 9999}),
            ("/v1/capsules", {"limit": "many"}),
            ("/v1/capsules", {"cursor": "not-a-cursor!"}),
            ("/v1/capsules", {"needs_me": "maybe"}),
            (f"/v1/capsules/{self.gated.name}/trace", {"cursor": "nonsense!"}),
            (f"/v1/capsules/{self.gated.name}/trace", {"limit": -3}),
        ):
            with self.subTest(route=route, params=params):
                response = self.get(route, self.root_headers, **params)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(
                    response.json()["error"]["code"], "VALIDATION_ERROR"
                )


class CapsulePagination(V1ReadsCase):
    def test_walking_cursors_duplicates_and_gaps_with_a_concurrent_insert(self) -> None:
        present = self.capsule_ids(
            self.json_of("/v1/capsules", self.root_headers, limit=100)
        )
        self.assertGreater(len(present), 3, "need a few rows to page through")

        walked: list = []
        cursor = None
        for page_number in range(40):
            params = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            body = self.json_of("/v1/capsules", self.root_headers, **params)
            self.assertLessEqual(len(body["items"]), 2)
            walked.extend(item["capsule_id"] for item in body["items"])
            self.assertEqual(body["page"]["limit"], 2)
            cursor = body["page"]["next_cursor"]
            if page_number == 0:
                # A concurrent insert, *after* the walk started. It is newer than
                # every row already seen, so it must not shift the walk: a paged
                # read is a position in a result set, not an offset into a file.
                inserted = f"m1a_insert_{os.getpid()}"
                create_capsule(inserted, MARKETING)
                self._created_capsules.append(inserted)
                read_index.rebuild()
            if cursor is None:
                break
        else:
            self.fail("the cursor walk never reached the end")

        self.assertEqual(len(walked), len(set(walked)), "a page repeated a row")
        self.assertTrue(
            set(present).issubset(set(walked)),
            f"a row present for the whole walk was skipped: {sorted(set(present) - set(walked))}",
        )
        # The row that arrived mid-walk is the only one the walk may show and
        # the first page did not: it is a real row of the result set now, and it
        # sorts *after* the cursor, so showing it costs nothing and hides
        # nothing. What must never happen is a repeated or skipped pre-existing
        # row, which the two assertions above pin.
        self.assertTrue(set(walked) - set(present) <= {inserted}, sorted(walked))
        self.assertEqual(len(present), len(set(present)), "the fixtures are distinct")

    def test_a_bad_cursor_is_refused_rather_than_silently_first_page(self) -> None:
        response = self.get("/v1/capsules", self.root_headers, cursor="@@@@")
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")


class CapsuleTrace(V1ReadsCase):
    def test_the_timeline_is_paged_bounded_and_hash_chained(self) -> None:
        first = self.json_of(
            f"/v1/capsules/{self.gated.name}/trace", self.marketing_approver, limit=2
        )
        self.assertEqual(first["capsule_id"], self.gated.name)
        self.assertEqual(first["chain"]["ok"], True, first["chain"])
        self.assertEqual(len(first["items"]), 2)
        self.assertTrue(first["page"]["next_cursor"], "a second page exists")
        for entry in first["items"]:
            self.assertTrue(entry["event"].startswith("m1a-step-"))
            self.assertEqual(entry["by"], "m1a-gear")
            self.assertTrue(entry["at"])
            self.assertTrue(entry["hash"])
            self.assertTrue(entry["why"])

        second = self.json_of(
            f"/v1/capsules/{self.gated.name}/trace",
            self.marketing_approver,
            limit=2,
            cursor=first["page"]["next_cursor"],
        )
        hashes = [entry["hash"] for entry in first["items"] + second["items"]]
        self.assertEqual(len(hashes), len(set(hashes)), "a page repeated an event")
        self.assertEqual(len(hashes), 3)
        self.assertIsNone(second["page"]["next_cursor"])
        # The chain the timeline reports is the engine's own verdict.
        from capsule_engine import trace as trace_engine

        self.assertEqual(
            first["chain"]["events"],
            trace_engine.verify_trace(SPOOL / self.gated.name).events,
        )

    def test_a_cursor_from_another_capsule_is_refused(self) -> None:
        body = self.json_of(
            f"/v1/capsules/{self.gated.name}/trace", self.marketing_approver, limit=1
        )
        stale = body["page"]["next_cursor"]
        # A hash the timeline does not contain is not a position: refuse, never
        # silently replay page one.
        response = self.get(
            f"/v1/capsules/{self.halted.name}/trace",
            self.marketing_approver,
            cursor=stale,
        )
        self.assertEqual(response.status_code, 422, response.text)


class CapsuleScope(V1ReadsCase):
    def test_every_list_item_belongs_to_a_visible_sector(self) -> None:
        for headers, allowed in (
            (self.marketing_headers, {MARKETING}),
            (self.development_headers, {DEVELOPMENT}),
            (self.both_headers, {MARKETING, DEVELOPMENT}),
            (self.mixed_headers, {MARKETING, DEVELOPMENT}),
        ):
            with self.subTest(allowed=allowed):
                body = self.json_of("/v1/capsules", headers, limit=100)
                self.assertTrue(
                    {item["domain"] for item in body["items"]}.issubset(allowed)
                )

    def test_the_other_sector_answers_the_missing_body(self) -> None:
        invisible = self.get(
            f"/v1/capsules/{self.development_capsule.name}", self.marketing_headers
        )
        missing = self.get(f"/v1/capsules/{MISSING_CAPSULE}", self.marketing_headers)
        self.assertEqual(invisible.status_code, 404, invisible.text)
        self.assertNotEqual(invisible.status_code, 403)
        self.assertEqual(
            invisible.json()["error"]["code"], missing.json()["error"]["code"]
        )
        self.assertEqual(
            invisible.json()["detail"].replace(self.development_capsule.name, "<ID>"),
            missing.json()["detail"].replace(MISSING_CAPSULE, "<ID>"),
        )

        trace = self.get(
            f"/v1/capsules/{self.development_capsule.name}/trace", self.marketing_headers
        )
        self.assertEqual(trace.status_code, 404, trace.text)
        self.assertNotEqual(trace.status_code, 403)

    def test_the_stage_table_covers_every_state_the_engine_can_write(self) -> None:
        for state in State:
            with self.subTest(state=state.value):
                stage = messages.stage_key(state.value)
                self.assertIn(stage, messages.STAGE_KEYS)
                self.assertNotEqual(stage, messages.STAGE_UNKNOWN.key)
                self.assertIn(
                    state.value, messages.stage_states(stage), "and back again"
                )
        # The one state the table does not know is reported, never dropped.
        self.assertEqual(messages.stage_key("a_state_from_the_future"), "unknown")


class ApprovalChain(V1ReadsCase):
    """Three sign-offs the chain must report: attested, legacy, record-only."""

    def setUp(self) -> None:
        super().setUp()
        self.attested_capsule = self._capsule("m1a_attested", MARKETING)
        self.attested_gate = seed_human_gate(self.attested_capsule, open_it=True)
        self.legacy_capsule = self._capsule("m1a_legacy", MARKETING)
        seed_human_gate(self.legacy_capsule, open_it=True)
        self.recovered_capsule = self._capsule("m1a_recovered", MARKETING)
        record_trace_steps(self.recovered_capsule, self.recovered_capsule.name, steps=1)
        self.recovered_gate = seed_human_gate(self.recovered_capsule, open_it=True)

    def _ledger(self, body: dict) -> list:
        return [
            entry
            for entry in body["approval_chain"]
            if entry["source"] == "decision_ledger"
        ]

    def test_an_attested_decision_is_reported_with_what_was_seen(self) -> None:
        review = self.json_of(
            f"/v1/capsules/{self.attested_capsule.name}/review", self.marketing_approver
        )
        elements = review["reviewed"]["elements"]
        self.assertTrue(elements, review)
        outcome = _approve_directly(self.attested_capsule, reviewed=review["reviewed"])
        self.assertTrue(outcome.attested)

        body = self.json_of(
            f"/v1/capsules/{self.attested_capsule.name}", self.marketing_approver
        )
        ledger = self._ledger(body)
        self.assertEqual(len(ledger), 1, body["approval_chain"])
        entry = ledger[0]
        self.assertEqual(entry["actor"], "chain-approver")
        self.assertEqual(entry["action"], "approve")
        self.assertEqual(entry["gate"], self.attested_gate)
        self.assertEqual(entry["gate_type"], "domain_lead_approval")
        self.assertTrue(entry["attested"])
        self.assertEqual(entry["elements"], elements)
        self.assertTrue(entry["reviewed_sha256"])
        self.assertTrue(entry["at"])
        self.assertIn("fixture", entry["reason"])
        # The gate itself now reads as decided, by the same actor.
        gate = next(
            g for g in body["gates"] if g["gate_id"] == self.attested_gate
        )
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["decided_by"], "chain-approver")

    def test_a_decision_taken_before_attestation_reads_as_unattested(self) -> None:
        outcome = _approve_directly(self.legacy_capsule, reviewed=None)
        self.assertFalse(outcome.attested)
        body = self.json_of(
            f"/v1/capsules/{self.legacy_capsule.name}", self.marketing_approver
        )
        ledger = self._ledger(body)
        self.assertEqual(len(ledger), 1, body["approval_chain"])
        self.assertEqual(ledger[0]["actor"], "chain-approver")
        self.assertFalse(ledger[0]["attested"], "a legacy decision is not attested")
        self.assertEqual(ledger[0]["elements"], {})
        self.assertIsNone(ledger[0]["reviewed_sha256"])

    def test_a_gate_decided_before_the_ledger_is_recovered_from_its_record(self) -> None:
        # A gate decided by a writer that never appended to the ledger: the chain
        # still names who approved what, from the gate's own record.
        self._write_legacy_gate_record(self.recovered_capsule, self.recovered_gate)
        body = self.json_of(
            f"/v1/capsules/{self.recovered_capsule.name}", self.marketing_approver
        )
        chain = body["approval_chain"]
        self.assertEqual(len(chain), 1, chain)
        self.assertEqual(chain[0]["source"], "gate_record")
        self.assertEqual(chain[0]["actor"], "legacy-approver")
        self.assertEqual(chain[0]["action"], "approve")
        self.assertEqual(chain[0]["gate"], self.recovered_gate)
        self.assertEqual(chain[0]["at"], "2026-01-02T03:04:05+00:00")
        self.assertFalse(chain[0]["attested"])
        gate = next(
            g for g in body["gates"] if g["gate_id"] == self.recovered_gate
        )
        self.assertEqual(gate["decided_by"], "legacy-approver")
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["at"], "2026-01-02T03:04:05+00:00")

        # Once a ledger entry covers that gate, the ledger is the record and the
        # gate's own copy is not reported twice.
        self._append_ledger_entry(self.recovered_capsule, self.recovered_gate)
        body = self.json_of(
            f"/v1/capsules/{self.recovered_capsule.name}", self.marketing_approver
        )
        chain = body["approval_chain"]
        self.assertEqual([entry["source"] for entry in chain], ["decision_ledger"])
        self.assertEqual(chain[0]["actor"], "ledger-approver")
        self.assertTrue(chain[0]["attested"])

    def test_the_chain_keeps_the_order_the_decisions_happened(self) -> None:
        _approve_directly(self.legacy_capsule, reviewed=None, reason="first")
        self._append_ledger_entry(
            self.legacy_capsule, "g1", at="2026-01-03T00:00:00+00:00", actor="second-approver"
        )
        chain = self.json_of(
            f"/v1/capsules/{self.legacy_capsule.name}", self.marketing_approver
        )["approval_chain"]
        self.assertEqual([entry["actor"] for entry in chain], ["second-approver", "chain-approver"])
        self.assertEqual(
            [entry["at"] for entry in chain],
            sorted(entry["at"] for entry in chain),
            "the chain reads oldest → newest",
        )

    # ---- fixture writers --------------------------------------------------- #

    def _write_legacy_gate_record(self, path, gate_id: str) -> None:
        """The shape a gate had before the decision ledger existed."""
        with CapsuleLock(path):
            manifest = load_manifest(path)
            for record in manifest.get("gates", []):
                if record.get("gate_id") != gate_id:
                    continue
                record["status"] = "passed"
                record["decided_by"] = "legacy-approver"
                record["closed_at"] = "2026-01-02T03:04:05+00:00"
                record["reason"] = "approved before attestation existed"
                record["evidence"] = {
                    "attested": False,
                    "algorithm": None,
                    "elements": {},
                }
            manifest.pop("decisions", None)
            save_manifest(path, manifest)

    def _append_ledger_entry(
        self,
        path,
        gate_id: str,
        *,
        at: str = "2026-01-03T00:00:00+00:00",
        actor: str = "ledger-approver",
    ) -> None:
        with CapsuleLock(path):
            manifest = load_manifest(path)
            entries = manifest.get("decisions")
            if not isinstance(entries, list):
                entries = []
            entries.append(
                {
                    "key": "fixture-key",
                    "decision_id": "fixture-decision",
                    "capsule_id": path.name,
                    "action": "approve",
                    "gate_id": gate_id,
                    "gate_type": "domain_lead_approval",
                    "status": "passed",
                    "actor": actor,
                    "attested": True,
                    "algorithm": "sha256",
                    "elements": {"copy.md": "a" * 64},
                    "reviewed_sha256": "b" * 64,
                    "reason": "attested in the ledger",
                    "state_before": "pending_approval",
                    "state_after": "approved",
                    "at": at,
                    "final": True,
                }
            )
            manifest["decisions"] = entries
            save_manifest(path, manifest)


if __name__ == "__main__":
    import unittest

    unittest.main()
