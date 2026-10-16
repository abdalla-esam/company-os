"""Tests for /api/engine/gears endpoint health reporting and governance file resolution.

Verifies:
1. Canonical governance_file path resolution (no fake engine/governance.json path).
2. Dynamic status reporting per gear (healthy, warn, error) instead of hardcoded healthy.
3. Model gear accurately reflects key / offline mode availability.
4. Factory gear and pulse gear reflect underlying asset health.
"""

from tests import _bootstrap  # noqa: F401  — isolated vault environment
from pathlib import Path
from unittest import mock
import asyncio
import os
import unittest

from orchestration import api
from capsule_engine.governance import governance_path


class TestGearsEndpoint(unittest.TestCase):
    def test_gears_status_returns_five_gears(self):
        res = asyncio.run(api.get_gears_status())
        self.assertIn("gears", res)
        gears = res["gears"]
        expected_gears = {
            "pulse_gear",
            "governance_gear",
            "factory_gear",
            "model_gear",
            "publishing_gear",
        }
        self.assertTrue(expected_gears <= set(gears.keys()))

    def test_governance_gear_points_to_real_file_and_is_healthy(self):
        res = asyncio.run(api.get_gears_status())
        gov_gear = res["gears"]["governance_gear"]
        gov_file = Path(gov_gear["governance_file"])
        self.assertTrue(gov_file.is_file(), f"Governance file {gov_file} should exist on disk")
        self.assertEqual(str(gov_file), str(governance_path().resolve()))
        self.assertEqual(gov_gear["status"], "healthy")

    def test_governance_gear_reports_warn_when_file_missing(self):
        with mock.patch("orchestration.api.governance_path", return_value=Path("/tmp/nonexistent_gov_file.json")):
            res = asyncio.run(api.get_gears_status())
            self.assertEqual(res["gears"]["governance_gear"]["status"], "warn")

    def test_governance_gear_reports_error_when_file_corrupt(self):
        with mock.patch("orchestration.api.load_governance", side_effect=ValueError("Corrupt JSON")):
            res = asyncio.run(api.get_gears_status())
            self.assertEqual(res["gears"]["governance_gear"]["status"], "error")

    def test_model_gear_status_warn_without_key_or_offline_mode(self):
        env_patches = {
            "AGENCY_LLM_API_KEY": "",
            "OPENAI_API_KEY": "",
            "AGENCY_OFFLINE_MODE": "0",
        }
        with mock.patch.dict(os.environ, env_patches, clear=False):
            res = asyncio.run(api.get_gears_status())
            self.assertEqual(res["gears"]["model_gear"]["status"], "warn")

    def test_model_gear_status_healthy_in_offline_mode(self):
        env_patches = {
            "AGENCY_LLM_API_KEY": "",
            "OPENAI_API_KEY": "",
            "AGENCY_OFFLINE_MODE": "1",
        }
        with mock.patch.dict(os.environ, env_patches, clear=False):
            res = asyncio.run(api.get_gears_status())
            self.assertEqual(res["gears"]["model_gear"]["status"], "healthy")

    def test_model_gear_status_healthy_with_api_key(self):
        env_patches = {
            "AGENCY_LLM_API_KEY": "sk-test-mock-key-for-test",
            "AGENCY_OFFLINE_MODE": "0",
        }
        with mock.patch.dict(os.environ, env_patches, clear=False):
            res = asyncio.run(api.get_gears_status())
            self.assertEqual(res["gears"]["model_gear"]["status"], "healthy")

    def test_factory_gear_status_error_when_spool_missing(self):
        with mock.patch("orchestration.api.SPOOL_DIR", Path("/tmp/nonexistent_spool_dir_xyz")):
            res = asyncio.run(api.get_gears_status())
            self.assertEqual(res["gears"]["factory_gear"]["status"], "error")

    def test_pulse_gear_status_error_when_cadence_fails(self):
        with mock.patch("domains.ideation.auto_ideator.check_cadence", side_effect=RuntimeError("Cadence disk fault")):
            res = asyncio.run(api.get_gears_status())
            self.assertEqual(res["gears"]["pulse_gear"]["status"], "error")


if __name__ == "__main__":
    unittest.main()
