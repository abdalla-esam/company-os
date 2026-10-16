"""Tests verifying that AGENCY_* env overrides never fall back to repo paths when target does not exist.

Session D invariant:
When any AGENCY_*_FILE or *_DIR env override is set and the target does not exist,
code must never fall back to the repo path.
Tested overrides:
1. governance (AGENCY_GOVERNANCE_FILE)
2. users (AGENCY_USERS_FILE)
3. company_profile (AGENCY_COMPANY_PROFILE_FILE / AGENCY_BRAND_PROFILE_FILE)
4. marketing_config (AGENCY_MARKETING_CONFIG_FILE)
5. gear_probes (AGENCY_GEAR_PROBE_CACHE / AGENCY_GEAR_PROBES_FILE)
6. task_types (AGENCY_TASK_TYPES_DIR)
7. assets vault (AGENCY_ASSETS_VAULT_DIR / ASSETS_VAULT_DIR)
8. finance ledger (AGENCY_SPEND_LEDGER / AGENCY_FINANCE_LEDGER_FILE)
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from tests import _bootstrap  # noqa: F401
from tests._bootstrap import REPO_ROOT, VAULT

from capsule_engine.governance import governance_path, load_governance
from shared_kernel.users import users_config_path
from shared_kernel.brand_profile import company_profile_path, load_company_profile
from domains.marketing.marketing_config import marketing_config_path, load_marketing_config
from capsule_engine.gear_registry import _probe_cache_path, _load_probe_cache
from capsule_engine.task_type import task_types_dir, list_task_types
from domains.marketing.asset_vault import get_vault_dir, find_asset_by_sha
from capsule_engine.spend_control import ledger_path


class TestEnvOverridesNoFallback(unittest.TestCase):
    """Ensure nonexistent env targets never silently fall back to real repo files."""

    def test_governance_env_override_no_fallback(self) -> None:
        fake = VAULT / "nonexistent_sub" / "governance.json"
        self.assertFalse(fake.exists())
        repo_gov = (REPO_ROOT / "config" / "governance.json").resolve()

        with mock.patch.dict(os.environ, {"AGENCY_GOVERNANCE_FILE": str(fake)}):
            resolved = governance_path()
            self.assertEqual(resolved, fake.resolve())
            self.assertNotEqual(resolved, repo_gov)
            # Ensure loading from nonexistent path returns empty state, not repo content
            loaded = load_governance()
            self.assertEqual(loaded.get("domains"), {})

    def test_users_env_override_no_fallback(self) -> None:
        fake = VAULT / "nonexistent_sub" / "users.json"
        self.assertFalse(fake.exists())
        repo_users = (REPO_ROOT / "config" / "users.json").resolve()

        with mock.patch.dict(os.environ, {"AGENCY_USERS_FILE": str(fake)}):
            resolved = users_config_path()
            self.assertEqual(resolved, fake.resolve())
            self.assertNotEqual(resolved, repo_users)

    def test_company_profile_env_override_no_fallback(self) -> None:
        fake = VAULT / "nonexistent_sub" / "company_profile.json"
        self.assertFalse(fake.exists())
        repo_profile = (REPO_ROOT / "config" / "company_profile.json").resolve()

        with mock.patch.dict(os.environ, {"AGENCY_COMPANY_PROFILE_FILE": str(fake)}):
            resolved = company_profile_path()
            self.assertEqual(resolved, fake.resolve())
            self.assertNotEqual(resolved, repo_profile)
            # When nonexistent, load_company_profile returns blank default, not repo profile
            loaded = load_company_profile()
            self.assertEqual(loaded.get("company_name"), "")

    def test_marketing_config_env_override_no_fallback(self) -> None:
        fake = VAULT / "nonexistent_sub" / "marketing_config.json"
        self.assertFalse(fake.exists())
        repo_mkt = (REPO_ROOT / "config" / "marketing_config.json").resolve()

        with mock.patch.dict(os.environ, {"AGENCY_MARKETING_CONFIG_FILE": str(fake)}):
            resolved = marketing_config_path()
            self.assertEqual(resolved, fake.resolve())
            self.assertNotEqual(resolved, repo_mkt)
            # When nonexistent, load_marketing_config returns defaults
            loaded = load_marketing_config()
            self.assertIn("cadence_hours", loaded)

    def test_gear_probes_env_override_no_fallback(self) -> None:
        fake = VAULT / "nonexistent_sub" / "gear_probes.json"
        self.assertFalse(fake.exists())
        repo_probes = (REPO_ROOT / "data" / "index" / "gear_probes.json").resolve()

        with mock.patch.dict(os.environ, {"AGENCY_GEAR_PROBE_CACHE": str(fake)}):
            resolved = _probe_cache_path()
            self.assertEqual(resolved, fake.resolve())
            self.assertNotEqual(resolved, repo_probes)
            self.assertEqual(_load_probe_cache(), {})

        with mock.patch.dict(os.environ, {"AGENCY_GEAR_PROBES_FILE": str(fake), "AGENCY_GEAR_PROBE_CACHE": ""}):
            resolved2 = _probe_cache_path()
            self.assertEqual(resolved2, fake.resolve())
            self.assertNotEqual(resolved2, repo_probes)

    def test_task_types_env_override_no_fallback(self) -> None:
        fake = VAULT / "nonexistent_sub" / "task_types"
        self.assertFalse(fake.exists())
        repo_tasks = (REPO_ROOT / "config" / "task_types").resolve()

        with mock.patch.dict(os.environ, {"AGENCY_TASK_TYPES_DIR": str(fake)}):
            resolved = task_types_dir()
            self.assertEqual(resolved, fake)
            self.assertNotEqual(resolved, repo_tasks)
            # List task types must return empty, not repo task types
            self.assertEqual(list_task_types(), ())

    def test_assets_vault_env_override_no_fallback(self) -> None:
        fake = VAULT / "nonexistent_sub" / "assets_vault"
        self.assertFalse(fake.exists())
        repo_vault = (REPO_ROOT / "data" / "assets" / "vault").resolve()

        with mock.patch.dict(os.environ, {"AGENCY_ASSETS_VAULT_DIR": str(fake)}):
            resolved = get_vault_dir()
            self.assertEqual(resolved, fake.resolve())
            self.assertNotEqual(resolved, repo_vault)
            self.assertIsNone(find_asset_by_sha("0" * 64))

    def test_finance_ledger_env_override_no_fallback(self) -> None:
        fake = VAULT / "nonexistent_sub" / "usage_ledger.jsonl"
        self.assertFalse(fake.exists())
        repo_ledger = (REPO_ROOT / "data" / "finance" / "usage_ledger.jsonl").resolve()

        with mock.patch.dict(os.environ, {"AGENCY_SPEND_LEDGER": str(fake)}):
            resolved = ledger_path()
            self.assertEqual(resolved, fake)
            self.assertNotEqual(resolved, repo_ledger)

        with mock.patch.dict(os.environ, {"AGENCY_SPEND_LEDGER": "", "AGENCY_FINANCE_LEDGER_FILE": str(fake)}):
            resolved2 = ledger_path()
            self.assertEqual(resolved2, fake)
            self.assertNotEqual(resolved2, repo_ledger)


if __name__ == "__main__":
    unittest.main()
