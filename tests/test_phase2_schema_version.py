"""Tests for Phase 2: schema_version with fail-closed loader rules and corrupt governance protection.

Verifies:
1. Missing schema_version is accepted as v1 and logged once per file kind.
2. Higher or unknown schema_version fails closed for critical files:
   - manifests, tickets, gate entries, governance.json, users.json.
3. Higher or unknown schema_version logs warning and uses defaults for configs:
   - company_profile, marketing_config.
4. Schema version is stamped ON WRITE (never mass-rewrite old files).
5. Corrupt or unreadable governance.json refuses writes (set_level/demote raise GovernanceCorruptError)
   and leaves corrupt file untouched on disk.
6. Stamping schema_version does not break cryptographic chaining, history MAC,
   artifact_sha256, or spool audit ledger.
7. Pre-Phase-2 fixtures (files without schema_version) load cleanly across all loaders.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths

import json
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule_engine import audit, governance, manifest
from capsule_engine.governance import GovernanceCorruptError
from data.domains.marketing import marketing_config
from data.governance.approval_gates import gate_script
from ops.tools import ticket_mgr
from orchestration import dispatcher
from shared_kernel import brand_profile, users
from shared_kernel.atomic_io import atomic_json
from shared_kernel.schema_version import (
    CURRENT_VERSIONS,
    KIND_COMPANY_PROFILE,
    KIND_GATE_ENTRY,
    KIND_GOVERNANCE,
    KIND_MANIFEST,
    KIND_MARKETING_CONFIG,
    KIND_TICKET,
    KIND_USERS,
    SchemaVersionError,
    check_schema_version,
    reset_warned_kinds,
    stamp_schema_version,
)
from shared_kernel.state_schema import GateEntry, Ticket


class TestPhase2SchemaVersion(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase2_test_"))
        self.addCleanup(shutil.rmtree, str(self.temp_dir), ignore_errors=True)
        reset_warned_kinds()

    # -------------------------------------------------------------------------
    # 1. Missing version -> accepted as v1 and logged once per kind
    # -------------------------------------------------------------------------
    def test_missing_version_treated_as_v1_and_logged(self) -> None:
        with self.assertLogs("shared_kernel.schema_version", level="WARNING") as log_ctx:
            v_manifest = check_schema_version(KIND_MANIFEST, {"capsule_id": "c1"}, "test.json")
            self.assertEqual(v_manifest, 1)

        log_output = " ".join(log_ctx.output)
        self.assertIn("schema_version missing in manifest", log_output)
        self.assertIn("treating as v1", log_output)

        # Re-check doesn't spam log for the same kind
        with self.assertRaises(AssertionError):  # no logs emitted
            with self.assertLogs("shared_kernel.schema_version", level="WARNING"):
                v2 = check_schema_version(KIND_MANIFEST, {"capsule_id": "c2"}, "test2.json")
                self.assertEqual(v2, 1)

    # -------------------------------------------------------------------------
    # 2. Unknown or higher version -> FAIL CLOSED for critical files
    # -------------------------------------------------------------------------
    def test_higher_version_fails_closed_for_critical_files(self) -> None:
        critical_kinds = [
            KIND_MANIFEST,
            KIND_TICKET,
            KIND_GATE_ENTRY,
            KIND_GOVERNANCE,
            KIND_USERS,
        ]

        for kind in critical_kinds:
            with self.subTest(kind=kind):
                # Version 2 (higher than current supported 1)
                data = {"schema_version": 2, "id": "test"}
                with self.assertRaises(SchemaVersionError) as ctx:
                    check_schema_version(kind, data, f"dummy_{kind}.json")
                err_msg = str(ctx.exception)
                self.assertIn(kind, err_msg)
                self.assertIn("2", err_msg)
                self.assertIn("Supported versions: [1]", err_msg)

    def test_validate_manifest_fails_closed_on_higher_version(self) -> None:
        bad_manifest = {
            "schema_version": 99,
            "capsule_id": "cap-0001",
            "state": "draft",
            "history": [],
        }
        with self.assertRaises(SchemaVersionError) as ctx:
            manifest.validate_manifest(bad_manifest, source="cap-0001/manifest.json")
        self.assertIn("manifest", str(ctx.exception))
        self.assertIn("99", str(ctx.exception))

    def test_load_users_fails_closed_on_higher_version(self) -> None:
        p = self.temp_dir / "users_v99.json"
        p.write_text(json.dumps({"schema_version": 99, "users": []}), "utf-8")
        with self.assertRaises(SchemaVersionError) as ctx:
            users.load_users(p)
        self.assertIn("users", str(ctx.exception))
        self.assertIn("99", str(ctx.exception))

    def test_load_governance_fails_closed_on_higher_version(self) -> None:
        p = self.temp_dir / "gov_v99.json"
        p.write_text(json.dumps({"schema_version": 99, "domains": {}}), "utf-8")
        with self.assertRaises(SchemaVersionError) as ctx:
            governance.load_governance(p)
        self.assertIn("governance", str(ctx.exception))
        self.assertIn("99", str(ctx.exception))

    def test_load_gate_entry_fails_closed_on_higher_version(self) -> None:
        p = self.temp_dir / "gate_v99.json"
        p.write_text(json.dumps({"schema_version": 99, "gate_id": "gate-1"}), "utf-8")
        with self.assertRaises(SchemaVersionError) as ctx:
            gate_script.load_entry(p)
        self.assertIn("gate_entry", str(ctx.exception))
        self.assertIn("99", str(ctx.exception))

    def test_ticket_pydantic_fails_closed_on_higher_version(self) -> None:
        raw = {
            "ticket_id": "t-1",
            "schema_version": 99,
            "sector": "marketing",
            "state": "open",
            "client_id": "c-1",
            "request": "req-1",
        }
        with self.assertRaises(ValueError) as ctx:
            Ticket.model_validate(raw)
        self.assertIn("Unsupported schema_version 99 for ticket", str(ctx.exception))

    def test_gate_entry_pydantic_fails_closed_on_higher_version(self) -> None:
        raw = {
            "gate_id": "gate-1",
            "schema_version": 99,
            "source": {"sector": "marketing", "client_id": "c1", "request": "r1"},
            "reason": "testing gate entry higher version",
            "opened_at": "2026-10-01T00:00:00+00:00",
            "state": "open",
        }
        with self.assertRaises(ValueError) as ctx:
            GateEntry.model_validate(raw)
        self.assertIn("Unsupported schema_version 99 for gate_entry", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 3. Higher version warns and falls back to defaults for configs
    # -------------------------------------------------------------------------
    def test_higher_version_company_profile_falls_back_to_defaults(self) -> None:
        p = self.temp_dir / "company_profile_v99.json"
        p.write_text(json.dumps({"schema_version": 99, "company_name": "Future Corp"}), "utf-8")

        with self.assertLogs("shared_kernel.schema_version", level="WARNING") as log_ctx:
            loaded = brand_profile.load_company_profile(p)

        self.assertEqual(loaded, brand_profile.DEFAULT_PROFILE)
        self.assertIn("Unsupported schema_version 99", " ".join(log_ctx.output))

    def test_higher_version_marketing_config_falls_back_to_defaults(self) -> None:
        p = self.temp_dir / "marketing_config_v99.json"
        p.write_text(json.dumps({"schema_version": 99, "cadence": {"daily": 100}}), "utf-8")

        with self.assertLogs("shared_kernel.schema_version", level="WARNING") as log_ctx:
            loaded = marketing_config.load_marketing_config(p)

        self.assertEqual(loaded, marketing_config.DEFAULT_MARKETING_CONFIG)
        self.assertIn("Unsupported schema_version 99", " ".join(log_ctx.output))

    # -------------------------------------------------------------------------
    # 4. Stamp on write only (reconciliation and preservation)
    # -------------------------------------------------------------------------
    def test_stamp_schema_version_on_write(self) -> None:
        # Critical kind stamps
        manifest_data: dict[str, object] = {"capsule_id": "c-100"}
        stamp_schema_version(KIND_MANIFEST, manifest_data)
        self.assertEqual(manifest_data["schema_version"], 1)

        # Users stamps both schema_version and version without conflict
        user_state: dict[str, object] = {"users": {}}
        stamp_schema_version(KIND_USERS, user_state)
        self.assertEqual(user_state["schema_version"], 1)
        self.assertEqual(user_state["version"], 1)

        # Governance stamps both schema_version and version
        gov_state: dict[str, object] = {"domains": {}}
        stamp_schema_version(KIND_GOVERNANCE, gov_state)
        self.assertEqual(gov_state["schema_version"], 1)
        self.assertEqual(gov_state["version"], 1)

    def test_users_save_and_load_roundtrip_stamping(self) -> None:
        p = self.temp_dir / "users.json"
        # Seed pre-phase-2 users file without schema_version
        p.write_text(json.dumps({"version": 1, "users": []}), "utf-8")
        st = users.load_users(p)
        self.assertEqual(st, [])

        # Save stamps schema_version
        users.save_users([{"username": "tester"}], p)
        saved = json.loads(p.read_text("utf-8"))
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["version"], 1)

    def test_company_profile_save_roundtrip_stamping(self) -> None:
        p = self.temp_dir / "company_profile.json"
        brand_profile.save_company_profile({"company_name": "Test Co"}, p)
        saved = json.loads(p.read_text("utf-8"))
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["company_name"], "Test Co")

    def test_marketing_config_save_roundtrip_stamping(self) -> None:
        p = self.temp_dir / "marketing_config.json"
        marketing_config.save_marketing_config({"weekly_budget": 500}, p)
        saved = json.loads(p.read_text("utf-8"))
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["weekly_budget"], 500)

    # -------------------------------------------------------------------------
    # 5. Corrupt governance.json: refuse to write, keep corrupt file untouched
    # -------------------------------------------------------------------------
    def test_corrupt_governance_refuses_write_and_leaves_file_untouched(self) -> None:
        gov_file = self.temp_dir / "corrupt_governance.json"
        corrupt_content = '{"domains": { INVALID JSON'
        gov_file.write_text(corrupt_content, "utf-8")

        old_env = os.environ.get(governance.GOVERNANCE_ENV)
        try:
            os.environ[governance.GOVERNANCE_ENV] = str(gov_file)

            # set_level must refuse to write
            with self.assertRaises(GovernanceCorruptError) as ctx:
                governance.set_level("marketing", "L2")
            self.assertIn("invalid JSON", str(ctx.exception))

            # Content on disk must be completely untouched
            self.assertEqual(gov_file.read_text("utf-8"), corrupt_content)

            # demote must also refuse to write
            with self.assertRaises(GovernanceCorruptError) as ctx:
                governance.demote("marketing", reason="test demotion")
            self.assertIn("invalid JSON", str(ctx.exception))

            # Content on disk remains untouched
            self.assertEqual(gov_file.read_text("utf-8"), corrupt_content)
        finally:
            if old_env is None:
                os.environ.pop(governance.GOVERNANCE_ENV, None)
            else:
                os.environ[governance.GOVERNANCE_ENV] = old_env

    def test_non_dict_governance_refuses_write(self) -> None:
        gov_file = self.temp_dir / "non_dict_governance.json"
        content = '["this", "is", "a", "list", "not", "a", "dict"]\n'
        gov_file.write_text(content, "utf-8")

        old_env = os.environ.get(governance.GOVERNANCE_ENV)
        try:
            os.environ[governance.GOVERNANCE_ENV] = str(gov_file)
            with self.assertRaises(GovernanceCorruptError):
                governance.set_level("marketing", "L1")
            self.assertEqual(gov_file.read_text("utf-8"), content)
        finally:
            if old_env is None:
                os.environ.pop(governance.GOVERNANCE_ENV, None)
            else:
                os.environ[governance.GOVERNANCE_ENV] = old_env

    # -------------------------------------------------------------------------
    # 6. Cryptographic chains, SHA verification & audit ledger
    # -------------------------------------------------------------------------
    def test_chains_and_sha_verification_pass_on_stamped_manifests(self) -> None:
        cap_id = "test-cap-0001"
        cap_dir = self.temp_dir / cap_id
        cap_dir.mkdir(parents=True, exist_ok=True)

        old_secret = os.environ.get(audit.AUDIT_SECRET_ENV)
        try:
            os.environ[audit.AUDIT_SECRET_ENV] = "test-phase2-secret-32-bytes-long"

            m = manifest.new_manifest(cap_id)
            # Stamped on write via save_manifest
            manifest.save_manifest(cap_dir, m)
            loaded = manifest.load_manifest(cap_dir)
            self.assertEqual(loaded.get("schema_version"), 1)

            # Verify initial history chain
            chain_stat = audit.verify_chain(loaded["history"])
            self.assertTrue(chain_stat.ok)

            # Append another history step
            manifest.append_history(
                loaded,
                to_state="in_progress",
                by="dispatcher",
                why="moved to in_progress",
            )
            manifest.save_manifest(cap_dir, loaded)
            loaded2 = manifest.load_manifest(cap_dir)
            self.assertEqual(loaded2.get("schema_version"), 1)

            chain_stat2 = audit.verify_chain(loaded2["history"])
            self.assertTrue(chain_stat2.ok)
            self.assertEqual(chain_stat2.chained, 2)

            # Deliverable SHA-256 binding
            deliv_file = cap_dir / "copy.md"
            deliv_file.write_text("# Deliverable content for verification\n", "utf-8")

            manifest.bind_artifact(loaded2, cap_dir, relative="copy.md")
            manifest.save_manifest(cap_dir, loaded2)

            loaded3 = manifest.load_manifest(cap_dir)
            self.assertEqual(loaded3.get("schema_version"), 1)

            actual_hash = manifest.verify_artifact(loaded3, cap_dir, relative="copy.md")
            self.assertTrue(isinstance(actual_hash, str) and len(actual_hash) == 64)

            # Spool audit ledger head append
            spool_dir = self.temp_dir / "spool"
            spool_dir.mkdir(parents=True, exist_ok=True)
            head_mac = audit.head_of(loaded3["history"])
            res = audit.append_ledger_head(spool_dir, capsule_id=cap_id, head=head_mac)
            self.assertIsNotNone(res)
            self.assertEqual(res.get("seq"), 0)

            # Verify ledger file
            entries = audit.read_ledger(spool_dir)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["capsule_id"], cap_id)
            ledger_stat = audit.verify_ledger(entries)
            self.assertTrue(ledger_stat.ok)
        finally:
            if old_secret is None:
                os.environ.pop(audit.AUDIT_SECRET_ENV, None)
            else:
                os.environ[audit.AUDIT_SECRET_ENV] = old_secret

    # -------------------------------------------------------------------------
    # 7. Pre-Phase-2 fixtures: test loading one fixture of each file type
    # -------------------------------------------------------------------------
    def test_pre_phase2_fixtures_load_cleanly(self) -> None:
        # Fixture 1: Pre-Phase-2 Manifest (no schema_version)
        old_manifest = {
            "capsule_id": "cap-legacy-1",
            "sector": "marketing",
            "state": "draft",
            "created_at": "2026-10-01T00:00:00+00:00",
            "updated_at": "2026-10-01T00:00:00+00:00",
            "attempt": 0,
            "history": [],
        }
        validated_manifest = manifest.validate_manifest(old_manifest, source="cap-legacy-1/manifest.json")
        self.assertEqual(validated_manifest["capsule_id"], "cap-legacy-1")
        self.assertNotIn("schema_version", validated_manifest)

        # Fixture 2: Pre-Phase-2 Ticket (no schema_version)
        old_ticket_data = {
            "ticket_id": "t-legacy-1",
            "sector": "marketing",
            "stage": "scratch",
            "state": "open",
            "client_id": "c-legacy",
            "title": "Legacy pre-phase 2 ticket",
            "request": "do something",
            "created_at": "2026-10-01T00:00:00+00:00",
        }
        t = Ticket.model_validate(old_ticket_data)
        self.assertEqual(t.ticket_id, "t-legacy-1")
        self.assertIsNone(t.schema_version)

        # Fixture 3: Pre-Phase-2 Gate Entry (no schema_version)
        old_gate_data = {
            "gate_id": "gate-legacy-1",
            "source": {
                "sector": "marketing",
                "client_id": "c-legacy",
                "request": "do something",
            },
            "reason": "gate required for legacy test",
            "opened_at": "2026-10-01T00:00:00+00:00",
            "state": "open",
        }
        g = GateEntry.model_validate(old_gate_data)
        self.assertEqual(g.gate_id, "gate-legacy-1")
        self.assertIsNone(g.schema_version)

        # Test loading old gate entry via gate_script.load_entry
        old_gate_file = self.temp_dir / "old_gate.json"
        old_gate_file.write_text(json.dumps(old_gate_data), "utf-8")
        loaded_gate = gate_script.load_entry(old_gate_file)
        self.assertEqual(loaded_gate["gate_id"], "gate-legacy-1")

        # Fixture 4: Pre-Phase-2 governance.json (has "version": 1, no "schema_version")
        old_gov_file = self.temp_dir / "old_governance.json"
        old_gov_file.write_text(
            json.dumps({
                "version": 1,
                "updated_at": "2026-10-01T00:00:00+00:00",
                "domains": {
                    "marketing": {"level": "L2", "updated_at": "2026-10-01T00:00:00+00:00"}
                },
            }),
            "utf-8",
        )
        loaded_gov = governance.load_governance(old_gov_file)
        self.assertEqual(loaded_gov["domains"]["marketing"]["level"], "L2")

        # Fixture 5: Pre-Phase-2 users.json (has "version": 1, no "schema_version")
        old_users_file = self.temp_dir / "old_users.json"
        old_users_file.write_text(
            json.dumps({
                "version": 1,
                "users": [
                    {
                        "username": "admin",
                        "password_hash": "scrypt$xyz",
                        "created_at": "2026-10-01T00:00:00+00:00",
                    }
                ],
            }),
            "utf-8",
        )
        loaded_users = users.load_users(old_users_file)
        self.assertEqual(len(loaded_users), 1)
        self.assertEqual(loaded_users[0]["username"], "admin")

        # Fixture 6: Pre-Phase-2 company_profile.json (no schema_version)
        old_company_file = self.temp_dir / "old_company.json"
        old_company_file.write_text(
            json.dumps({"company_name": "Legacy Enterprise"}),
            "utf-8",
        )
        loaded_company = brand_profile.load_company_profile(old_company_file)
        self.assertEqual(loaded_company["company_name"], "Legacy Enterprise")

        # Fixture 7: Pre-Phase-2 marketing_config.json (no schema_version)
        old_mkt_file = self.temp_dir / "old_marketing.json"
        old_mkt_file.write_text(
            json.dumps({"cadence": {"mode": "manual"}}),
            "utf-8",
        )
        loaded_mkt = marketing_config.load_marketing_config(old_mkt_file)
        self.assertEqual(loaded_mkt["cadence"]["mode"], "manual")


if __name__ == "__main__":
    unittest.main()
