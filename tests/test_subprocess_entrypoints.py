"""Subprocess entry point smoke tests: verify every child script runs in production conditions.

In production, ``api.py``, ``pulse.py``, and ``dispatcher.py`` launch child scripts
via ``_run_subprocess`` or ``subprocess.run``. These subprocesses execute with:
- exact production cwd (repo root, domain directory, or engine directory)
- inherited environment (pointing at an isolated vault in tests)
- NO ``PYTHONPATH`` set
- NO parent-process ``sys.path`` inheritance

Every script must bootstrap its own import paths (e.g. to locate ``engine/shared_kernel``).
This test runs EACH script with a harmless command (list/--help/--check/settle/etc.)
and asserts that it executes cleanly without ``ModuleNotFoundError`` or exit failure.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — isolated vault setup

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Mapping, NamedTuple

from tests._bootstrap import (
    REPO_ROOT,
    VAULT,
    isolation_env,
    take_repo_snapshot,
)

#: Child-process paths that MUST resolve inside the temp vault. These are the
#: variables whose silent fallback to the repository previously let a `pulse`
#: beat write the real ``data/capsules/spool/.pulse_scan.json``.
_VAULT_PATHS = (
    "CAPSULES_SPOOL",
    "CAPSULES_SANDBOX",
    "DOMAINS_DIR",
    "READ_INDEX_PATH",
    "AGENCY_INDEX_FILE",
    "GATE_QUEUE_DIR",
    "AGENCY_MEMORY_FILE",
    "AGENCY_GOVERNANCE_FILE",
    "AGENCY_FINANCE_DIR",
    "AGENCY_SPEND_LEDGER",
)


def _real_tree_changes(
    before: dict[str, tuple[int, int, str]],
    after: dict[str, tuple[int, int, str]],
) -> list[str]:
    """Every real config/data file a subprocess touched, as human-readable lines."""
    changes: list[str] = []
    for rel, initial in before.items():
        if rel not in after:
            changes.append(f"deleted {rel}")
        elif after[rel] != initial:
            changes.append(f"modified {rel}")
    for rel in after:
        if rel not in before:
            changes.append(f"created {rel}")
    return changes


#: The content roots a beat works through: what is in them decides how much the
#: beat does. They are redirected (below) so the beat spec runs over an empty
#: vault.
_BEAT_CONTENT_ROOTS: tuple[str, ...] = (
    "CAPSULES_SPOOL",
    "CAPSULES_SANDBOX",
    "DOMAINS_DIR",
    "AGENCY_WORK_QUEUE_DIR",
)


def _empty_vault_env(base: dict[str, str]) -> dict[str, str]:
    """``base`` with every content root pointed at a fresh, empty directory.

    The run vault is shared with every other module, so by the time this test
    runs it holds *their* capsules and tickets — a snapshot taken mid-suite
    found seven leftover capsules, four of them carrying work. A beat over that
    does the real thing: it executed six capsules, called the live copy and
    image providers, and took 67s against a 30s smoke budget. The contract this
    test asserts is that the script bootstraps its imports, resolves its
    production paths, and exits 0 — not that it can process another module's
    fixtures. An empty vault exercises exactly that, and keeps a smoke test from
    spending money on a configured provider (a leak the repo snapshot below
    still catches).

    The redirected roots live *inside* ``VAULT``, so the isolation assertion
    above holds for them too.
    """
    empty_root = VAULT / "beat_empty"
    empty = dict(base)
    for var in _BEAT_CONTENT_ROOTS:
        current = Path(base[var])
        target = empty_root / current.name
        target.mkdir(parents=True, exist_ok=True)
        empty[var] = str(target)
    return empty


class SubprocessSpec(NamedTuple):
    name: str
    script: Path
    args: list[str]
    cwd: Path
    stdin_payload: str | None = None
    expected_rc: int = 0
    env_overrides: Mapping[str, str] | None = None


class TestSubprocessEntryPoints(unittest.TestCase):
    """Ensure every script invoked via subprocess can bootstrap and execute cleanly."""

    def _build_env(self) -> dict[str, str]:
        """Production environment: isolated test vault, strictly NO PYTHONPATH.

        The temp-vault overrides are re-applied explicitly (not merely inherited
        from ``os.environ``): every engine path the child resolves — the spool,
        the index, the memory ledger, the fence/governance files — is pinned to
        the scratch vault, so a child can never reach the repository's real
        ``config/`` or ``data/`` even if an earlier test left the ambient
        environment dirty.
        """
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.update(isolation_env())
        return env

    def test_all_subprocess_entrypoints(self) -> None:
        env = self._build_env()

        # Fail closed on the isolation contract itself: every vault path must
        # resolve inside VAULT before a single child is spawned.
        for var in _VAULT_PATHS:
            with self.subTest(env_var=var):
                value = env.get(var)
                self.assertTrue(value, f"{var} must be set for the child")
                resolved = Path(value).resolve()
                self.assertTrue(
                    resolved == VAULT or VAULT in resolved.parents,
                    f"{var}={value} escapes the temp vault {VAULT}",
                )

        # The real tree is snapshotted (mtime + size + sha) around the run; if a
        # child reaches it, this test fails loudly instead of leaking silently.
        real_before = take_repo_snapshot()

        # Inventory of all scripts invoked via subprocess in api.py, pulse.py, dispatcher.py, and ops/
        specs: list[SubprocessSpec] = [
            # --- 1. Gate script (launched by api.py: create_ticket, approve_gate, reject_gate) ---
            SubprocessSpec(
                name="api -> gate_script (list)",
                script=REPO_ROOT / "data" / "governance" / "approval_gates" / "gate_script.py",
                args=["list"],
                cwd=REPO_ROOT,
            ),
            SubprocessSpec(
                name="api -> gate_script (--help)",
                script=REPO_ROOT / "data" / "governance" / "approval_gates" / "gate_script.py",
                args=["--help"],
                cwd=REPO_ROOT,
            ),

            # --- 2. Domain tracker scripts (launched by api.py: settle and pulse.py: settle) ---
            SubprocessSpec(
                name="api -> marketing tracker_script (settle)",
                script=REPO_ROOT / "data" / "domains" / "marketing" / "tracker_script.py",
                args=["settle"],
                cwd=REPO_ROOT,
            ),
            SubprocessSpec(
                name="pulse -> marketing tracker_script (settle in domain cwd)",
                script=REPO_ROOT / "data" / "domains" / "marketing" / "tracker_script.py",
                args=["settle"],
                cwd=REPO_ROOT / "data" / "domains" / "marketing",
            ),
            SubprocessSpec(
                name="api -> client_management tracker_script (settle)",
                script=REPO_ROOT / "data" / "domains" / "client_management" / "tracker_script.py",
                args=["settle"],
                cwd=REPO_ROOT,
            ),
            SubprocessSpec(
                name="pulse -> client_management tracker_script (settle in domain cwd)",
                script=REPO_ROOT / "data" / "domains" / "client_management" / "tracker_script.py",
                args=["settle"],
                cwd=REPO_ROOT / "data" / "domains" / "client_management",
            ),
            SubprocessSpec(
                name="api -> ideation tracker_script (settle)",
                script=REPO_ROOT / "data" / "domains" / "ideation" / "tracker_script.py",
                args=["settle"],
                cwd=REPO_ROOT,
            ),
            SubprocessSpec(
                name="pulse -> ideation tracker_script (settle in domain cwd)",
                script=REPO_ROOT / "data" / "domains" / "ideation" / "tracker_script.py",
                args=["settle"],
                cwd=REPO_ROOT / "data" / "domains" / "ideation",
            ),

            # --- 3. Domain worker scripts (launched by api.py: ticket bridge and dispatcher.py: handoff) ---
            SubprocessSpec(
                name="api/dispatcher -> marketing worker_script (empty payload)",
                script=REPO_ROOT / "data" / "domains" / "marketing" / "worker_script.py",
                args=[],
                cwd=REPO_ROOT / "data" / "domains" / "marketing",
                stdin_payload="{}",
            ),
            SubprocessSpec(
                name="api/dispatcher -> client_management worker_script (empty payload)",
                script=REPO_ROOT / "data" / "domains" / "client_management" / "worker_script.py",
                args=[],
                cwd=REPO_ROOT / "data" / "domains" / "client_management",
                stdin_payload="{}",
            ),
            SubprocessSpec(
                name="api/dispatcher -> ideation worker_script (empty payload)",
                script=REPO_ROOT / "data" / "domains" / "ideation" / "worker_script.py",
                args=[],
                cwd=REPO_ROOT / "data" / "domains" / "ideation",
                stdin_payload="{}",
            ),
            SubprocessSpec(
                name="api/dispatcher -> design worker_script (empty payload)",
                script=REPO_ROOT / "data" / "domains" / "design" / "worker_script.py",
                args=[],
                cwd=REPO_ROOT / "data" / "domains" / "design",
                stdin_payload="{}",
            ),
            SubprocessSpec(
                name="api/dispatcher -> operations worker_script (empty payload)",
                script=REPO_ROOT / "data" / "domains" / "operations" / "worker_script.py",
                args=[],
                cwd=REPO_ROOT / "data" / "domains" / "operations",
                stdin_payload="{}",
            ),

            # --- 4. Auto ideator (launched by api.py: trigger_auto_ideation and pulse.py) ---
            SubprocessSpec(
                name="api/pulse -> ideation auto_ideator (--check)",
                script=REPO_ROOT / "data" / "domains" / "ideation" / "auto_ideator.py",
                args=["--check"],
                cwd=REPO_ROOT / "data" / "domains" / "ideation",
            ),

            # --- 5. Engine orchestration scripts (launched by api.py and pulse.py) ---
            SubprocessSpec(
                name="api -> pulse (beat)",
                script=REPO_ROOT / "engine" / "orchestration" / "pulse.py",
                args=[],
                cwd=REPO_ROOT,
                # A beat is the one entrypoint here with no harmless verb: with
                # fixtures in the vault it processes them for real. Give it an
                # empty vault so it proves bootstrap + exit 0 and nothing more.
                env_overrides=_empty_vault_env(env),
            ),
            SubprocessSpec(
                name="pulse -> dispatcher (list)",
                script=REPO_ROOT / "engine" / "orchestration" / "dispatcher.py",
                args=["list"],
                cwd=REPO_ROOT / "engine" / "orchestration",
            ),
            SubprocessSpec(
                name="pulse -> capsule_pulse (--help)",
                script=REPO_ROOT / "engine" / "orchestration" / "capsule_pulse.py",
                args=["--help"],
                cwd=REPO_ROOT / "engine",
            ),
            SubprocessSpec(
                name="pulse -> directus_bridge (--help / sweep)",
                script=REPO_ROOT / "engine" / "orchestration" / "directus_bridge.py",
                args=[],
                cwd=REPO_ROOT / "engine",
            ),
        ]

        failures: list[str] = []
        for spec in specs:
            with self.subTest(entrypoint=spec.name):
                self.assertTrue(spec.script.is_file(), f"Script missing: {spec.script}")
                proc = subprocess.run(
                    [sys.executable, str(spec.script), *spec.args],
                    input=spec.stdin_payload,
                    cwd=str(spec.cwd),
                    env={**env, **(spec.env_overrides or {})},
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode != spec.expected_rc:
                    err_msg = (
                        f"{spec.name} failed with rc={proc.returncode} (expected {spec.expected_rc})\n"
                        f"STDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}"
                    )
                    failures.append(err_msg)

        # The isolation assertion runs even when an entrypoint failed: a child
        # that leaked into the real tree is the more serious finding.
        real_changes = _real_tree_changes(real_before, take_repo_snapshot())
        if real_changes:
            failures.append(
                "subprocess(es) touched the repository's real config/ or data/: "
                + "; ".join(real_changes)
            )

        if failures:
            self.fail(f"{len(failures)} entrypoints failed:\n" + "\n---\n".join(failures))


if __name__ == "__main__":
    unittest.main()
