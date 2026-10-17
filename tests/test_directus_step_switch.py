"""The Directus sweep must be OFF unless a deployment explicitly enables it.

A beat that dials a Directus which does not exist costs nothing in USD, but it
still opens a login attempt on *every* beat and writes the failure into the
money ledger — a 120s interval means ~720 dead calls a day, which buries the
real spend evidence and hides a misconfigured deployment.

So the step defaults to disabled and the module owns the switch: no opt-in, no
socket. ``AGENCY_DIRECTUS_ENABLED=1`` (or true/yes/on) turns the sweep back on
for a deployment that actually runs Directus; anything else — including an
unset variable — leaves it silent.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestration import directus_bridge

REPO_ROOT = Path(directus_bridge.__file__).resolve().parents[2]
SCRIPT = Path(directus_bridge.__file__).resolve()


class DirectusEnabledTruthTable(unittest.TestCase):
    """``directus_enabled`` is the single answer to "may this step run?"."""

    def _enabled(self, value: str | None) -> bool:
        env = {} if value is None else {"AGENCY_DIRECTUS_ENABLED": value}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("AGENCY_DIRECTUS_ENABLED", None) if value is None else None
            return directus_bridge.directus_enabled()

    def test_unset_means_off(self) -> None:
        self.assertFalse(self._enabled(None))

    def test_blank_and_zero_are_off(self) -> None:
        for value in ("", " ", "0", "false", "False", "no", "off", "disabled", "maybe"):
            with self.subTest(value=value):
                self.assertFalse(self._enabled(value))

    def test_explicit_truthy_values_are_on(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on", " 1 ", "On"):
            with self.subTest(value=value):
                self.assertTrue(self._enabled(value))


class TheScriptHonoursTheSwitch(unittest.TestCase):
    """Running the module as the beat runs it: no opt-in, no ledger write."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="directus_switch_")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run(self, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
        # A Directus that cannot answer: if the switch leaked, the attempt
        # would be metered, which is exactly what the ledger assertion catches.
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": f"{REPO_ROOT / 'engine'}{os.pathsep}{REPO_ROOT}",
                "DIRECTUS_INTERNAL_URL": "http://127.0.0.1:1",
                "AGENCY_SPEND_LEDGER": str(self.tmp / "usage_ledger.jsonl"),
                "AGENCY_ENGINE_CONFIG": str(self.tmp / "engine.json"),
            }
        )
        env.pop("AGENCY_DIRECTUS_ENABLED", None)
        env.update(env_overrides)
        shutil.copy2(REPO_ROOT / "config" / "engine.json", self.tmp / "engine.json")
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )

    def _ledger_lines(self) -> list[dict]:
        path = self.tmp / "usage_ledger.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_disabled_script_is_a_no_op(self) -> None:
        result = self._run({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("disabled", (result.stdout + result.stderr).lower())
        self.assertEqual(self._ledger_lines(), [], "a disabled step must not touch a socket")

    def test_enabled_script_attempts_the_sweep(self) -> None:
        """The switch is not decoration: enabled, the attempt is really made."""
        result = self._run({"AGENCY_DIRECTUS_ENABLED": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self._ledger_lines()
        self.assertTrue(events, "an enabled step must be metered like any other paid path")
        self.assertEqual(events[0]["provider"], "directus")
