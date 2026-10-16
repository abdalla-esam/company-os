"""Guard test: verifies complete isolation of tests from real config/ and data/ directories.

Runs as the last test (alphabetical discovery via `test_zzz_`) to ensure that
running the suite touches zero real repository files, including gitignored ones.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._bootstrap import INITIAL_REPO_SNAPSHOT, VAULT, take_repo_snapshot


def _snapshot_diff(
    initial: dict[str, tuple[int, int, str]],
    current: dict[str, tuple[int, int, str]],
) -> tuple[list[str], list[str], list[str]]:
    """``(modified, added, deleted)`` between two ``take_repo_snapshot`` results.

    A file counts as modified when *any* of its recorded fields changed —
    content digest, modification time, or size. mtime is deliberately part of
    the identity: a rewrite that restores identical bytes still proves the tree
    was touched.
    """
    modified: list[str] = []
    deleted: list[str] = []
    added: list[str] = []

    for rel, init in initial.items():
        if rel not in current:
            deleted.append(rel)
            continue
        mtime_ns, size, sha = current[rel]
        init_mtime, init_size, init_sha = init
        problems = []
        if sha != init_sha:
            problems.append(f"sha {init_sha[:8]} -> {sha[:8]}")
        if mtime_ns != init_mtime:
            problems.append("mtime changed")
        if size != init_size:
            problems.append(f"size {init_size} -> {size}")
        if problems:
            modified.append(f"{rel} ({'; '.join(problems)})")

    for rel in current:
        if rel not in initial:
            added.append(rel)

    return modified, added, deleted


class TestIsolationGuard(unittest.TestCase):
    """Guard verifying that the test suite is 100% hermetic and isolated."""

    def test_all_canonical_paths_resolve_inside_temp_vault(self) -> None:
        """Verify that shared_kernel.paths exports paths strictly pointing inside VAULT."""
        from shared_kernel import paths

        checked_paths = {
            "USERS_CONFIG_PATH": paths.USERS_CONFIG_PATH,
            "DOMAINS_DIR": paths.DOMAINS_DIR,
            "CAPSULES_SPOOL": paths.CAPSULES_SPOOL,
            "CAPSULES_SANDBOX": paths.CAPSULES_SANDBOX,
            "ASSETS_VAULT": paths.ASSETS_VAULT,
            "GATE_QUEUE_DIR": paths.GATE_QUEUE_DIR,
            "READ_INDEX_PATH": paths.READ_INDEX_PATH,
            "WORK_QUEUE_DIR": paths.WORK_QUEUE_DIR,
            "MEMORY_DIR": paths.MEMORY_DIR,
            "FEEDBACK_LEDGER_PATH": paths.FEEDBACK_LEDGER_PATH,
            "DEDUP_INDEX_PATH": paths.DEDUP_INDEX_PATH,
            "ENGINE_CONFIG_PATH": paths.ENGINE_CONFIG_PATH,
            "FINANCE_DIR": paths.FINANCE_DIR,
            "SPEND_LEDGER_PATH": paths.SPEND_LEDGER_PATH,
        }

        outside: dict[str, Path] = {}
        for name, p in checked_paths.items():
            resolved = Path(p).resolve()
            if resolved != VAULT and VAULT not in resolved.parents:
                outside[name] = resolved

        self.assertEqual(
            outside,
            {},
            f"The following canonical paths resolve outside the temp vault ({VAULT}): {outside}",
        )

    def test_an_mtime_only_rewrite_is_detected(self) -> None:
        """A byte-identical rewrite (same sha/size, new mtime) must still fail.

        This is the exact shape of the leak this guard was strengthened for: the
        pulse scan-state is rewritten as ``{}`` → ``{}`` while its mtime moves,
        which a hash-only comparison cannot see.
        """
        initial = {"data/capsules/spool/.pulse_scan.json": (100, 2, "deadbeef")}
        # Same content and size; only the modification time advanced.
        current = {"data/capsules/spool/.pulse_scan.json": (200, 2, "deadbeef")}
        modified, added, deleted = _snapshot_diff(initial, current)
        self.assertEqual(added, [])
        self.assertEqual(deleted, [])
        self.assertEqual(len(modified), 1, modified)
        self.assertIn("mtime changed", modified[0])

    def test_no_real_config_or_data_files_were_modified_added_or_deleted(self) -> None:
        """Compare every real config/ and data/ file with the pre-run snapshot.

        The snapshot records ``(mtime_ns, size, sha256)``. Content alone is not
        enough: a writer that rewrites a file with byte-identical content leaves
        the digest untouched while proving it touched the tree — which is exactly
        how the leaked subprocess entrypoint rewrote the real spool's
        ``data/capsules/spool/.pulse_scan.json`` (``{}`` → ``{}``) yet passed a
        hash-only guard. mtime is therefore compared as well.

        No real file is exempt unless it is provably not the engine's; the sole
        exemptions live in ``tests._bootstrap._is_ignorable_real_file`` and are
        argued there (``*.tmp`` staging names and macOS ``.DS_Store``). Notably
        ``.pulse_scan.json`` is *not* exempt: the leak that motivated this guard
        rewrote precisely that file, and the subprocess entrypoint test now runs
        its children inside the vault so it can no longer happen.
        """
        modified, added, deleted = _snapshot_diff(
            INITIAL_REPO_SNAPSHOT, take_repo_snapshot()
        )

        errors = []
        if modified:
            errors.append(f"Modified files under config/ or data/: {modified}")
        if added:
            errors.append(f"Newly created files under config/ or data/: {added}")
        if deleted:
            errors.append(f"Deleted files under config/ or data/: {deleted}")

        self.assertEqual(
            errors,
            [],
            "Test isolation violation! Real files were touched during test execution:\n"
            + "\n".join(errors),
        )


if __name__ == "__main__":
    unittest.main()
