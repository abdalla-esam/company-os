"""M1a: ``/v1/overview``, ``/v1/attention`` and ``/v1/me`` — the mobile home.

What this module proves, against the real fixtures in
:mod:`tests.v1_reads_fixtures`:

- **scope**: a marketing-only credential sees marketing and nothing else in the
  overview, the attention list, the work list and every detail; development
  objects answer ``404`` byte-identical to a missing id;
- **counts**: every number the home reports equals a brute-force count taken
  straight off the vault files (never from a page size);
- **can_act**: attention shows the same items to a reader and an approver, and
  tells each of them truthfully whether *they* can decide it;
- **honesty**: each response carries ``api_version`` / ``as_of`` / ``source``,
  the pipeline maps states to stages by one table (reporting what it cannot
  place), and the home carries no company-wide finance block.

Read-only throughout: this module never decides anything.
"""

from __future__ import annotations  # noqa: E402 — the suite runs on 3.9

from tests.v1_reads_fixtures import (  # noqa: E402
    DEVELOPMENT,
    MARKETING,
    MISSING_CAPSULE,
    MISSING_TICKET,
    TEST_SECRET,
    V1ReadsCase,
    key,
    seed_human_gate,
)

import json

from capsule_engine.capabilities import verify_key
from capsule_engine.manifest import revision_cap
from orchestration import access_control, approval, read_index
from tests._bootstrap import DOMAINS, GATE_QUEUE, SPOOL
from orchestration.v1 import messages

#: The subject every fixture decision is taken by (so actor assertions are exact).
DECIDING_ACTOR = "m1a-decider"


def _decide(capsule_path, *, sector: str = MARKETING, action: str, reason: str):
    """Take one real decision through the engine's own approval service.

    A fixture step (the HTTP decision routes are exercised in their own
    modules); what matters here is that the capsule's **attested decision
    ledger** gains a real entry with an actor, action, gate and time.
    """
    principal = access_control.Principal.from_key(
        verify_key(
            key(
                DECIDING_ACTOR,
                f"domain:read:{sector}",
                f"capsule:approve:{sector}",
                f"capsule:reject:{sector}",
            ),
            secret=TEST_SECRET.encode("utf-8"),
        )
    )
    common = {
        "principal": principal,
        "reviewer": principal.subject,
        "audit_note": principal.audit_note(),
    }
    if action == "approve":
        return approval.approve(capsule_path, reason=reason, **common)
    return approval.reject(capsule_path, reason=reason, why=reason, **common)


def _count_spool_capsules(sector: str = None) -> dict:
    """Brute-force: every *readable* capsule's state, straight from the files.

    A capsule whose manifest the engine refuses to read (an unknown
    ``schema_version``) is not counted here — and must not be counted by the
    index either, which is precisely what makes this an honest comparison: the
    brute force applies the engine's own schema check rather than trusting the
    JSON to be readable.
    """
    from shared_kernel.schema_version import KIND_MANIFEST, check_schema_version

    states: dict = {}
    for manifest_path in sorted(SPOOL.rglob("manifest.json")):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("state"), str):
            continue
        try:
            check_schema_version(KIND_MANIFEST, data, manifest_path)
        except Exception:  # unknown/unreadable schema — not part of any view
            continue
        if sector is not None and data.get("domain") != sector:
            continue
        states[data["state"]] = states.get(data["state"], 0) + 1
    return states


def _count_tickets_awaiting(sector: str = None) -> int:
    total = 0
    for path in sorted(DOMAINS.rglob("ticket_*.json")):
        if "archive" in path.parts:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if sector is not None and (data.get("sector") or path.parts[-3]) != sector:
            continue
        stage = data.get("stage") or path.parent.name
        state = data.get("state")
        if stage == "scratch" or state in ("pending_approval", "needs_revision"):
            total += 1
    return total


def _count_open_gates(sector: str = None) -> int:
    total = 0
    for path in sorted(GATE_QUEUE.glob("gate_*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("state") != "open":
            continue
        if sector is not None:
            source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
            if source.get("sector") != sector:
                continue
        total += 1
    return total


def _attention_from_files(sector: str = None, *, quarantined: int = 0) -> dict:
    """The home's four counts, recomputed from the vault by brute force."""
    states = _count_spool_capsules(sector)
    return {
        messages.KIND_REQUIRES_DECISION: states.get("pending_approval", 0)
        + states.get("stalled", 0)
        + _count_tickets_awaiting(sector)
        + _count_open_gates(sector),
        messages.KIND_SYSTEM_HALT: states.get("failed", 0),
        messages.KIND_REVISION_CAP: 0,
        messages.KIND_QUARANTINED: quarantined,
    }


class EnvelopeIsOnEveryRead(V1ReadsCase):
    def test_every_read_carries_api_version_as_of_and_source(self) -> None:
        routes = (
            "/v1/me",
            "/v1/overview",
            "/v1/attention",
            "/v1/capsules",
            "/v1/tickets",
            f"/v1/capsules/{self.gated.name}",
            f"/v1/capsules/{self.gated.name}/trace",
            f"/v1/tickets/{self.marketing_ticket}",
        )
        for route in routes:
            with self.subTest(route=route):
                body = self.json_of(route, self.root_headers)
                self.assertEqual(body["api_version"], "v1")
                self.assertIn(body["source"], ("index", "files", "principal"))
                self.assertRegex(str(body["as_of"]), r"^\d{4}-\d{2}-\d{2}T")
                self.assertTrue(str(body["as_of"]).endswith("+00:00"), body["as_of"])

    def test_identity_data_says_principal_and_vault_views_say_index(self) -> None:
        # The envelope's source is the one field whose whole job is to be true.
        self.assertEqual(
            self.json_of("/v1/me", self.marketing_headers)["source"], "principal"
        )
        self.assertEqual(
            self.json_of("/v1/overview", self.marketing_headers)["source"], "index"
        )


class MeReportsTheCredentialOnly(V1ReadsCase):
    def test_subject_sectors_and_capabilities_per_caller(self) -> None:
        reader = self.json_of("/v1/me", self.marketing_headers)
        self.assertEqual(reader["subject"], "mkt-reader")
        self.assertEqual(reader["sectors"], {"all": False, "values": [MARKETING]})
        self.assertEqual(reader["capabilities"]["can_approve_sectors"], [])
        self.assertEqual(reader["capabilities"]["can_create_sectors"], [])
        # No filesystem identity lookup ever happens, so no display name is
        # invented for a token that carries none.
        self.assertNotIn("display_name", reader)

        approver = self.json_of("/v1/me", self.marketing_approver)
        self.assertEqual(approver["capabilities"]["can_approve_sectors"], [MARKETING])
        self.assertEqual(approver["capabilities"]["can_create_sectors"], [MARKETING])
        self.assertEqual(
            approver["capabilities"]["can_decide_gate_sectors"], [MARKETING]
        )

        root = self.json_of("/v1/me", self.root_headers)
        self.assertTrue(root["sectors"]["all"])
        self.assertEqual(root["sectors"]["values"], [])
        self.assertEqual(root["capabilities"]["can_approve_sectors"], "all")

    def test_a_reader_key_only_sees_its_own_sectors(self) -> None:
        body = self.json_of("/v1/me", self.both_headers)
        self.assertFalse(body["sectors"]["all"])
        self.assertEqual(body["sectors"]["values"], [DEVELOPMENT, MARKETING])

    def test_the_legacy_root_token_is_marked_as_such(self) -> None:
        from orchestration import access_control
        from orchestration.v1 import reads

        principal = access_control.Principal.legacy_root("legacy-tester")
        body = reads.me(principal=principal, sees_all=True, sectors=())
        self.assertTrue(body["is_root"])
        self.assertTrue(body["is_legacy_token"])


class AttentionCountsEqualToTheFiles(V1ReadsCase):
    def test_counts_match_a_brute_force_scan(self) -> None:
        body = self.json_of("/v1/overview", self.root_headers)
        counts = body["attention"]["counts"]
        # A capsule over the cap is escalated to `failed` by transition(); the
        # revision_cap bucket only catches a state a *lowered* cap left behind,
        # which the fixtures do not contain.
        self.assertEqual(counts, _attention_from_files(quarantined=1))
        self.assertGreaterEqual(counts[messages.KIND_REQUIRES_DECISION], 5)
        self.assertEqual(counts[messages.KIND_SYSTEM_HALT], 1)
        self.assertEqual(revision_cap(), 3)

    def test_pipeline_counts_add_up_and_report_unknown_states(self) -> None:
        body = self.json_of("/v1/overview", self.root_headers)
        stages = body["pipeline"]["stages"]
        states = _count_spool_capsules()

        self.assertEqual(
            sum(stage["count"] for stage in stages), sum(states.values())
        )
        by_key = {stage["stage"]: stage for stage in stages}
        for state, count in states.items():
            with self.subTest(state=state):
                self.assertEqual(
                    by_key[messages.stage_key(state)]["count"] >= count, True
                )
        self.assertEqual(by_key["halted"]["count"], states.get("failed", 0))
        self.assertEqual(
            by_key["human"]["count"],
            states.get("pending_approval", 0) + states.get("stalled", 0),
        )
        # The mapping is a table, and the response says which states are in it.
        self.assertEqual(body["pipeline"]["unknown_states"], [])
        self.assertEqual(
            {stage["stage"] for stage in stages}, set(messages.STAGE_KEYS)
        )

    def test_attention_lists_exactly_the_fixtures_with_truthful_can_act(self) -> None:
        root = self.json_of("/v1/attention", self.root_headers, limit=100)
        entities = self.attention_entities(root)
        expected_root = {
            ("capsule", self.gated.name),
            ("capsule", self.development_capsule.name),
            ("capsule", self.halted.name),
            ("ticket", self.marketing_ticket),
            ("ticket", self.development_ticket),
            ("ticket", self.gated_ticket),
            ("gate", self.marketing_gate),
            ("gate", self.development_gate_id),
            ("capsule", "m1a_quarantine"),
        }
        self.assertTrue(
            expected_root.issubset(entities),
            sorted(expected_root - entities),
        )
        self.assertEqual(len(entities), len(root["items"]), "each entity appears once")

        by_entity = {
            (item["entity"]["kind"], item["entity"]["id"]): item
            for item in root["items"]
        }
        # The halted capsule is a human-reset position: no /v1 route can act on
        # it, so even root is told can_act is false, and the remedy names the
        # desktop instead of inventing an action.
        halted = by_entity[("capsule", self.halted.name)]
        self.assertEqual(halted["kind"], messages.KIND_SYSTEM_HALT)
        self.assertFalse(halted["can_act"])
        self.assertNotIn("action", halted)
        self.assertEqual(halted["revision_count"], 4)
        self.assertEqual(halted["revision_cap"], revision_cap())
        self.assertIn("revision cap exceeded", halted["reason_en"])
        self.assertTrue(halted["reason"], "the Arabic reason is the default")

        gated = by_entity[("capsule", self.gated.name)]
        self.assertEqual(gated["kind"], messages.KIND_REQUIRES_DECISION)
        self.assertTrue(gated["can_act"])
        self.assertEqual(gated["action"]["path"], "/v1/decisions")
        self.assertIsInstance(gated["age_seconds"], float)
        self.assertEqual(gated["domain"], MARKETING)

        quarantined = by_entity[("capsule", "m1a_quarantine")]
        self.assertEqual(quarantined["kind"], messages.KIND_QUARANTINED)
        self.assertEqual(quarantined["quarantine_code"], "schema_version")
        self.assertIsNone(quarantined["domain"])
        self.assertFalse(quarantined["can_act"])

    def test_can_act_is_per_credential_not_per_item(self) -> None:
        reader = self.json_of("/v1/attention", self.marketing_headers, limit=100)
        approver = self.json_of("/v1/attention", self.marketing_approver, limit=100)

        reader_items = {
            (item["entity"]["kind"], item["entity"]["id"]): item
            for item in reader["items"]
        }
        approver_items = {
            (item["entity"]["kind"], item["entity"]["id"]): item
            for item in approver["items"]
        }
        # The same items, the same order — only can_act differs.
        self.assertEqual(set(reader_items), set(approver_items))
        self.assertEqual(
            [item["entity"] for item in reader["items"]],
            [item["entity"] for item in approver["items"]],
        )

        self.assertFalse(reader_items[("capsule", self.gated.name)]["can_act"])
        self.assertTrue(approver_items[("capsule", self.gated.name)]["can_act"])
        self.assertFalse(reader_items[("ticket", self.marketing_ticket)]["can_act"])
        self.assertTrue(approver_items[("ticket", self.marketing_ticket)]["can_act"])
        self.assertFalse(reader_items[("gate", self.marketing_gate)]["can_act"])
        self.assertTrue(approver_items[("gate", self.marketing_gate)]["can_act"])

        # A credential that may decide in *another* sector still cannot decide
        # here: the read is scoped, so it sees the marketing item and is told
        # can_act is false — no button, and no invented "you can act".
        mixed = self.json_of("/v1/attention", self.mixed_headers, limit=100)
        marketing_gated = next(
            item
            for item in mixed["items"]
            if item["entity"]["id"] == self.gated.name
        )
        self.assertFalse(marketing_gated["can_act"])
        self.assertNotIn("action", marketing_gated)
        development_gated = next(
            item
            for item in mixed["items"]
            if item["entity"]["id"] == self.development_capsule.name
        )
        self.assertTrue(development_gated["can_act"], development_gated)

        # And a sector-only approver does not see the other sector at all.
        development = self.json_of("/v1/attention", self.development_approver, limit=100)
        self.assertNotIn(
            ("capsule", self.gated.name), self.attention_entities(development)
        )

    def test_the_quarantined_record_is_root_only(self) -> None:
        marketing = self.json_of("/v1/attention", self.marketing_headers, limit=100)
        self.assertNotIn(
            ("capsule", "m1a_quarantine"), self.attention_entities(marketing)
        )
        self.assertEqual(
            self.json_of("/v1/overview", self.marketing_headers)["attention"][
                "counts"
            ][messages.KIND_QUARANTINED],
            0,
        )


class MarketingTokenSeesOnlyMarketing(V1ReadsCase):
    def test_overview_is_scoped_to_the_visible_sector(self) -> None:
        body = self.json_of("/v1/overview", self.marketing_headers)
        self.assertEqual([tile["domain"] for tile in body["domains"]], [MARKETING])
        # The same counts, but only over marketing's own files — the other
        # sector's capsules, tickets and gates are simply not there.
        self.assertEqual(body["attention"]["counts"], _attention_from_files(MARKETING))
        self.assertEqual(body["totals"]["capsules"], sum(_count_spool_capsules(MARKETING).values()))

    def test_attention_is_scoped_to_the_visible_sector(self) -> None:
        body = self.json_of("/v1/attention", self.marketing_headers, limit=100)
        entities = self.attention_entities(body)
        self.assertIn(("capsule", self.gated.name), entities)
        self.assertNotIn(("capsule", self.development_capsule.name), entities)
        self.assertNotIn(("ticket", self.development_ticket), entities)
        self.assertNotIn(("gate", self.development_gate_id), entities)
        for item in body["items"]:
            self.assertIn(item["domain"], (MARKETING, None))

    def test_work_list_and_details_are_scoped(self) -> None:
        listing = self.json_of("/v1/capsules", self.marketing_headers, limit=100)
        self.assertIn(self.gated.name, self.capsule_ids(listing))
        self.assertNotIn(self.development_capsule.name, self.capsule_ids(listing))

        tickets = self.json_of("/v1/tickets", self.marketing_headers, limit=100)
        ticket_ids = {item["ticket_id"] for item in tickets["items"]}
        self.assertIn(self.marketing_ticket, ticket_ids)
        self.assertNotIn(self.development_ticket, ticket_ids)

    def test_development_objects_answer_404_identical_to_missing(self) -> None:
        for template, object_id, missing_id in (
            ("/v1/capsules/{}", self.development_capsule.name, MISSING_CAPSULE),
            (
                "/v1/capsules/{}/trace",
                self.development_capsule.name,
                MISSING_CAPSULE,
            ),
            ("/v1/tickets/{}", self.development_ticket, MISSING_TICKET),
        ):
            with self.subTest(template=template):
                invisible = self.get(template.format(object_id), self.marketing_headers)
                missing = self.get(template.format(missing_id), self.marketing_headers)
                self.assertEqual(invisible.status_code, 404, invisible.text)
                self.assertNotEqual(invisible.status_code, 403)
                self.assertEqual(
                    invisible.json()["error"]["code"], missing.json()["error"]["code"]
                )
                self.assertEqual(
                    invisible.json()["detail"].replace(object_id, "<ID>"),
                    missing.json()["detail"].replace(missing_id, "<ID>"),
                    "an invisible object must be indistinguishable from a missing one",
                )

    def test_root_and_every_wide_grant_see_both_sectors(self) -> None:
        # At least the two fixture sectors: other test modules share the vault,
        # so a wide credential may legitimately see a sector this module never
        # created — what it may never do is *miss* one of the two that are here.
        for headers in (self.root_headers, self.wildcard_headers, self.both_headers):
            with self.subTest(headers=headers):
                body = self.json_of("/v1/overview", headers)
                tiles = {tile["domain"] for tile in body["domains"]}
                self.assertTrue(
                    {DEVELOPMENT, MARKETING}.issubset(tiles),
                    sorted(tiles),
                )


class DomainTiles(V1ReadsCase):
    def test_tiles_carry_stage_counts_open_tickets_and_labelled_cost(self) -> None:
        body = self.json_of("/v1/overview", self.root_headers)
        tiles = {tile["domain"]: tile for tile in body["domains"]}
        marketing = tiles[MARKETING]

        self.assertIn("today_cost_usd", marketing)
        self.assertEqual(
            marketing["cost_source"], "capsules.cost_usd by updated_at (index)"
        )
        self.assertAlmostEqual(marketing["today_cost_usd"], 0.42, places=4)
        self.assertGreaterEqual(marketing["tickets_open"], 2)
        self.assertEqual(set(marketing["capsules_by_stage"]), set(messages.STAGE_KEYS))
        self.assertGreaterEqual(marketing["capsules_by_stage"]["halted"], 1)

        # Nothing the data does not have is invented: no monthly figure here.
        self.assertNotIn("month_cost_usd", marketing)
        self.assertNotIn("caps", body)
        self.assertNotIn("kill_switch", body)
        self.assertNotIn("subscriptions", json.dumps(body))

    def test_needs_me_counts_what_this_credential_can_decide(self) -> None:
        market = self.json_of("/v1/overview", self.marketing_approver)
        reader = self.json_of("/v1/overview", self.marketing_headers)
        self.assertGreaterEqual(market["totals"]["needs_me"], 3)
        # A reader can decide nothing, so nothing needs *it*.
        self.assertEqual(reader["totals"]["needs_me"], 0)
        self.assertEqual(
            sum(tile["needs_me"] for tile in market["domains"]),
            market["totals"]["needs_me"],
        )

    def test_the_five_oldest_decisions_are_the_oldest(self) -> None:
        body = self.json_of("/v1/overview", self.root_headers)
        oldest = body["attention"]["oldest_requires_decision"]
        self.assertLessEqual(len(oldest), 5)
        self.assertTrue(oldest, "the fixtures contain items awaiting a decision")
        ages = [item["age_seconds"] for item in oldest]
        self.assertEqual(ages, sorted(ages, reverse=True))
        for item in oldest:
            self.assertEqual(item["kind"], messages.KIND_REQUIRES_DECISION)

    def test_recent_decisions_come_from_the_capsule_decision_ledger(self) -> None:
        # Two real decisions in the home sector — one approval, one rejection —
        # plus one in the other sector, all recorded in each capsule's own
        # attested decision ledger. The feedback ledger only ever holds
        # rejections, so an approval on the home can only come from here.
        approved = self._capsule("m1a_appr", MARKETING)
        approved_gate = seed_human_gate(approved, open_it=True)
        _decide(approved, action="approve", reason="on brand, ship it")

        rejected = self._capsule("m1a_rej", MARKETING)
        rejected_gate = seed_human_gate(rejected, open_it=True)
        _decide(rejected, action="reject", reason="off brand")

        other = self._capsule("m1a_dec_other", DEVELOPMENT)
        seed_human_gate(other, open_it=True)
        _decide(other, sector=DEVELOPMENT, action="approve", reason="dev approves")

        read_index.rebuild()

        body = self.json_of("/v1/overview", self.root_headers)
        self.assertEqual(body["decisions_source"], "capsule_decisions")
        self.assertIsInstance(body["recent_decisions"], list)
        self.assertLessEqual(len(body["recent_decisions"]), 10)

        rows = {row["capsule_id"]: row for row in body["recent_decisions"]}
        self.assertIn(approved.name, rows)
        self.assertIn(rejected.name, rows)
        self.assertEqual(rows[approved.name]["decision"], "approve")
        self.assertEqual(rows[approved.name]["actor"], DECIDING_ACTOR)
        self.assertEqual(rows[approved.name]["gate_id"], approved_gate)
        self.assertEqual(rows[approved.name]["gate_type"], "domain_lead_approval")
        self.assertTrue(rows[approved.name]["at"])
        self.assertEqual(rows[rejected.name]["decision"], "reject")
        self.assertTrue(rows[rejected.name]["at"])
        # An approval is present at all: the feedback ledger could not show one.
        self.assertIn("approve", {row["decision"] for row in body["recent_decisions"]})

        # Scoping: a marketing-only credential sees the marketing decisions and
        # never the development one (and vice versa).
        scoped = self.json_of("/v1/overview", self.marketing_headers)
        ids = {row["capsule_id"] for row in scoped["recent_decisions"]}
        self.assertIn(approved.name, ids)
        self.assertIn(rejected.name, ids)
        self.assertNotIn(other.name, ids)

        # The file-scan fallback answers the same list (same rows, same fields)
        # when the index is switched off — it reads the very same decision ledger.
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"AGENCY_INDEX": "off"}):
            from_files = self.json_of("/v1/overview", self.root_headers)
        # ``decisions_source`` names the provenance (capsule decisions, not the
        # feedback ledger) and is stable; index-vs-files is reported per read.
        self.assertEqual(from_files["source"], "files")
        self.assertEqual(from_files["source_detail"]["decisions"], "files")
        self.assertEqual(body["source_detail"]["decisions"], "index")
        indexed_by_id = {row["capsule_id"]: row for row in body["recent_decisions"]}
        files_by_id = {row["capsule_id"]: row for row in from_files["recent_decisions"]}
        self.assertEqual(files_by_id, indexed_by_id)
        self.assertTrue(read_index.enabled(), "the switch is restored")

    def test_index_off_serves_the_same_overview_from_files(self) -> None:
        import os
        from unittest import mock

        indexed = self.json_of("/v1/overview", self.root_headers)
        with mock.patch.dict(os.environ, {"AGENCY_INDEX": "off"}):
            from_files = self.json_of("/v1/overview", self.root_headers)
        self.assertEqual(indexed["source"], "index")
        self.assertEqual(from_files["source"], "files")
        self.assertEqual(from_files["domains"], indexed["domains"])
        self.assertEqual(
            from_files["pipeline"]["stages"], indexed["pipeline"]["stages"]
        )
        self.assertEqual(from_files["totals"], indexed["totals"])
        self.assertEqual(from_files["attention"]["counts"], indexed["attention"]["counts"])
        self.assertTrue(read_index.enabled(), "the switch is restored")


if __name__ == "__main__":
    import unittest

    unittest.main()
