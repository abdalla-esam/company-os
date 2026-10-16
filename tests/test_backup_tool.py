"""Backup tool tests: a snapshot the system can actually be restored from.

Covers Part 4 Phase 3 of the architecture plan:

1. **Round trip** — create -> verify -> restore into a temp dir -> every
   restored file hash matches the source (and the manifest).
2. **Concurrent writer** — a writer thread rewrites a capsule under
   ``CapsuleLock`` while the backup runs: the snapshot must stay internally
   consistent (every copied file loads), or the capsule must be reported as
   skipped — never torn.
3. **Corrupt archive** — byte flips inside the tarball are caught by
   ``--verify`` (per-item failures, non-zero exit).
4. **Policy refusals** — restore refuses a non-empty target without
   ``--force``; ``create`` refuses an in-repo output dir and refuses an
   unencrypted archive without ``--allow-plain``.
5. **Encryption** — exercised when a tool is installed; skipped with a
   message when neither age nor gpg exists.

Everything runs against the isolated scratch vault from ``tests._bootstrap``
(env-resolved canonical paths); no real repo file is ever touched, and the
isolation guard (test_zzz) re-verifies that at the end of the suite.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths + vault

from tests._bootstrap import DOMAINS, MEMORY, SPOOL, VAULT

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule_engine.locking import CapsuleLock
from capsule_engine.manifest import new_manifest, save_manifest

import ops.tools.backup as backup

BACKUP_CONFIG = VAULT / "backup_config"
BACKUP_OUT = VAULT / "backup_out"

#: Shared vault trees every test in this module may write into. The vault is
#: shared with the rest of the suite (fixtures for other modules are written
#: at import time), so each test snapshots them and restores them on teardown:
#: no capsule, ticket, gate entry or ledger byte may outlive the test that
#: created it — and the shared read index (INDEX_FILE) is never touched.
_SHARED_TREES = (
    SPOOL, DOMAINS, VAULT / "gate_queue", MEMORY, VAULT / "sandbox", VAULT / "finance"
)


def _snapshot_vault() -> tuple[dict, set]:
    files: dict = {}
    dirs: set = set()
    for root in _SHARED_TREES:
        if not root.is_dir():
            continue
        dirs.add(root)
        for path in root.rglob("*"):
            if path.is_dir():
                dirs.add(path)
            elif path.is_file():
                files[path] = path.read_bytes()
    return files, dirs


def _restore_vault(snapshot: tuple) -> None:
    files, dirs = snapshot
    for root in _SHARED_TREES:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts),
                           reverse=True):
            if path.is_file() and path not in files:
                path.unlink(missing_ok=True)
            elif path.is_dir() and path not in dirs:
                shutil.rmtree(path, ignore_errors=True)
    for path, data in files.items():
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        elif path.read_bytes() != data:
            path.write_bytes(data)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_ticket(domain: str, ticket_id: str, *, stage: str = "approved") -> Path:
    tdir = DOMAINS / domain / "tickets" / stage
    tdir.mkdir(parents=True, exist_ok=True)
    path = tdir / f"ticket_{ticket_id}.json"
    path.write_text(json.dumps({
        "ticket_id": ticket_id,
        "state": "pending",
        "schema_version": 1,
        "request": f"request for {ticket_id}",
        "created_at": "2026-10-13T00:00:00+00:00",
    }), encoding="utf-8")
    return path


class BackupToolBase(unittest.TestCase):
    """Shared isolated env: config dir, gate queue, output dir."""

    def setUp(self) -> None:
        BACKUP_CONFIG.mkdir(parents=True, exist_ok=True)
        BACKUP_OUT.mkdir(parents=True, exist_ok=True)
        (BACKUP_CONFIG / "users.json").write_text(
            json.dumps({"schema_version": 1, "version": 1, "users": [
                {"username": "tester", "role": "admin", "is_active": True,
                 "password_hash": "fake-bcrypt-hash"}
            ]}), encoding="utf-8")
        (BACKUP_CONFIG / "engine.json").write_text("{}", encoding="utf-8")

        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "AGENCY_BACKUP_CONFIG_DIR",
                "AGENCY_BACKUP_DIR",
                backup.AGE_RECIPIENT_ENV,
                backup.AGE_IDENTITY_ENV,
                backup.GPG_PASSPHRASE_ENV,
            )
        }
        os.environ["AGENCY_BACKUP_CONFIG_DIR"] = str(BACKUP_CONFIG)

        self.addCleanup(_restore_vault, _snapshot_vault())
        self.addCleanup(shutil.rmtree, BACKUP_CONFIG, ignore_errors=True)
        self.addCleanup(shutil.rmtree, BACKUP_OUT, ignore_errors=True)

        def _restore_env() -> None:
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(_restore_env)

    # helpers --------------------------------------------------------------- #

    def _make_vault(self) -> dict:
        """Populate the scratch vault with one of everything. Returns sources."""
        cap = SPOOL / "cap_roundtrip"
        cap.mkdir(parents=True, exist_ok=True)
        save_manifest(cap, new_manifest("cap_roundtrip"))
        (cap / "copy.md").write_text("deliverable body", encoding="utf-8")

        ticket = _write_ticket("marketing", "T-RT-1")
        gate_dir = VAULT / "gate_queue"
        gate_dir.mkdir(parents=True, exist_ok=True)
        gate = gate_dir / "gate_rt.json"
        gate.write_text(json.dumps({
            "gate_id": "G-RT", "state": "open", "risk_tier": None,
            "reason": "roundtrip", "opened_at": "2026-10-13T00:00:00+00:00",
            "schema_version": 1,
        }), encoding="utf-8")
        ledger = MEMORY / "feedback_history.jsonl"
        ledger.write_text('{"entry_id": "E-1"}\n{"entry_id": "E-2"}\n',
                          encoding="utf-8")
        return {
            "capsule": cap, "ticket": ticket, "gate": gate, "ledger": ledger,
        }

    def _create(self, *extra: str):
        out_args = ["create", "--out", str(BACKUP_OUT), "--allow-plain", *extra]
        rc = backup.main(out_args)
        archives = sorted(BACKUP_OUT.glob("*.tar.gz"))
        self.assertTrue(archives, "create produced no archive")
        return rc, archives[-1]

    def _source_files(self) -> dict:
        """Every file currently in the backup set, by archive-relative path."""
        result: dict = {}
        for arc, real, _mode in backup.resolve_sources(include_sandbox=False):
            if not real.is_dir():
                continue
            for path in real.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(real).parts
                if backup._excluded(rel):
                    continue
                result["/".join((arc,) + rel)] = path
        return result


class BackupRoundTrip(BackupToolBase):

    def test_backup_verify_restore_compare_hashes(self) -> None:
        sources = self._make_vault()
        rc, archive = self._create()
        self.assertEqual(rc, 0)

        # verify passes on the untouched archive
        self.assertEqual(backup.main(["verify", str(archive)]), 0)

        # restore into a fresh temp dir
        target = Path(tempfile.mkdtemp(prefix="so_test_restore_"))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        rc = backup.main(["restore", str(archive), "--to", str(target)])
        self.assertEqual(rc, 0)

        # every source file was restored byte-for-byte
        src_files = self._source_files()
        self.assertGreaterEqual(len(src_files), 5, src_files.keys())
        for arc, src in src_files.items():
            restored = target / arc
            self.assertTrue(restored.is_file(), f"missing restored file: {arc}")
            self.assertEqual(
                _sha256(restored), _sha256(src),
                f"hash mismatch after round trip: {arc}",
            )

        # restored capsule and ticket actually LOAD under schema rules
        from capsule_engine.manifest import load_manifest
        loaded = load_manifest(target / "data/capsules/spool/cap_roundtrip")
        self.assertEqual(loaded["capsule_id"], "cap_roundtrip")
        ticket = json.loads(
            (target / "data/domains/marketing/tickets/approved/ticket_T-RT-1.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(ticket["ticket_id"], "T-RT-1")
        self.assertEqual(sources["ticket"].read_bytes(),
                         (target / "data/domains/marketing/tickets/approved/"
                          "ticket_T-RT-1.json").read_bytes())

    def test_manifest_records_hashes_and_counts(self) -> None:
        self._make_vault()
        rc, archive = self._create()
        self.assertEqual(rc, 0)
        manifest = json.loads(
            Path(str(archive) + ".manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["backup_schema_version"],
                         backup.BACKUP_SCHEMA_VERSION)
        counts = manifest["counts"]
        self.assertGreaterEqual(counts["capsules"], 1)
        self.assertGreaterEqual(counts["tickets"], 1)
        self.assertGreaterEqual(counts["gate_entries"], 1)
        self.assertGreaterEqual(counts["config"], 1)
        self.assertEqual(counts["skipped"], 0)
        # per-file sha256 present for every listed file
        for entry in manifest["files"]:
            self.assertIn("sha256", entry)
            self.assertEqual(len(entry["sha256"]), 64)

    def test_read_index_sqlite_and_sandbox_excluded_by_default(self) -> None:
        self._make_vault()
        # decoy read index INSIDE a backed-up source tree (spool root) — the
        # exclusion must hold where it can actually be violated. The shared
        # bootstrap INDEX_FILE is never written.
        index = SPOOL / ".read_index.sqlite3"
        index.write_bytes(b"sqlite decoy")
        sandbox_cap = VAULT / "sandbox" / "cap_sand"
        sandbox_cap.mkdir(parents=True, exist_ok=True)
        save_manifest(sandbox_cap, new_manifest("cap_sand"))

        rc, archive = self._create()
        self.assertEqual(rc, 0)
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        self.assertFalse(any("read_index" in n for n in names), names)
        self.assertFalse(any("sandbox" in n for n in names), names)

    def test_keep_last_prunes_old_archives(self) -> None:
        self._make_vault()
        names = []
        for _ in range(3):
            rc, archive = self._create("--keep-last", "2")
            self.assertEqual(rc, 0)
            names.append(archive.name)
            time.sleep(1.1)  # timestamped names must differ
        remaining = sorted(p.name for p in BACKUP_OUT.glob("*.tar.gz"))
        self.assertLessEqual(len(remaining), 2, remaining)
        self.assertIn(names[-1], remaining)


class BackupWhileWriterRuns(BackupToolBase):

    def test_backup_while_writer_updates_capsule(self) -> None:
        """A live writer must never produce a torn snapshot."""
        cap = SPOOL / "cap_live"
        cap.mkdir(parents=True, exist_ok=True)
        save_manifest(cap, new_manifest("cap_live"))

        stop = threading.Event()
        errors: list = []

        def writer() -> None:
            try:
                from capsule_engine.manifest import load_manifest

                while not stop.is_set():
                    with CapsuleLock(cap, purpose="test-writer", timeout=10):
                        current = load_manifest(cap)
                        current["attempt"] = int(current.get("attempt") or 0) + 1
                        save_manifest(cap, current)
                    time.sleep(0.01)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            rc, archive = self._create()
        finally:
            stop.set()
            thread.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertEqual(rc, 0)

        # every copied file must load: extract and check every JSON parses
        # and the capsule manifest validates under schema rules.
        target = Path(tempfile.mkdtemp(prefix="so_test_live_"))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(target)
        from capsule_engine.manifest import load_manifest
        restored_cap = target / "data/capsules/spool/cap_live"
        loaded = load_manifest(restored_cap)
        self.assertEqual(loaded["capsule_id"], "cap_live")
        for path in target.rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))

    def test_busy_capsule_is_skipped_and_reported(self) -> None:
        cap = SPOOL / "cap_busy"
        cap.mkdir(parents=True, exist_ok=True)
        save_manifest(cap, new_manifest("cap_busy"))

        release = threading.Event()

        def holder() -> None:
            with CapsuleLock(cap, purpose="test-holder", timeout=10):
                release.wait(timeout=30)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        try:
            rc = backup.main([
                "create", "--out", str(BACKUP_OUT), "--allow-plain",
                "--lock-timeout", "0.2",
            ])
            self.assertEqual(rc, 0)
        finally:
            release.set()
            thread.join(timeout=15)

        manifest_path = sorted(BACKUP_OUT.glob("*.tar.gz.manifest.json"))[-1]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        skipped_caps = [
            s for s in manifest["skipped"] if "cap_busy" in s["path"]
        ]
        self.assertTrue(
            skipped_caps,
            f"busy capsule must be reported as skipped, got {manifest['skipped']}",
        )
        self.assertIn("lock", skipped_caps[0]["reason"])


class BackupCorruptArchive(BackupToolBase):

    def test_verify_detects_modified_file(self) -> None:
        self._make_vault()
        rc, archive = self._create()
        self.assertEqual(rc, 0)

        # rebuild the archive with one payload file modified in place
        evil = Path(tempfile.mkdtemp(prefix="so_test_evil_"))
        self.addCleanup(shutil.rmtree, evil, ignore_errors=True)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(evil)
        victim = evil / "data/memory/feedback_history.jsonl"
        victim.write_text('{"entry_id": "EVIL"}\n', encoding="utf-8")
        corrupt = BACKUP_OUT / "sovereign-backup-corrupt.tar.gz"
        with tarfile.open(corrupt, "w:gz") as tar:
            for path in sorted(evil.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=str(path.relative_to(evil)))

        rc = backup.main(["verify", str(corrupt)])
        self.assertEqual(rc, 1, "verify must fail on a tampered archive")

    def test_verify_detects_deleted_file(self) -> None:
        self._make_vault()
        rc, archive = self._create()
        self.assertEqual(rc, 0)
        evil = Path(tempfile.mkdtemp(prefix="so_test_evil2_"))
        self.addCleanup(shutil.rmtree, evil, ignore_errors=True)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(evil)
        (evil / "data/capsules/spool/cap_roundtrip/manifest.json").unlink()
        broken = BACKUP_OUT / "sovereign-backup-broken.tar.gz"
        with tarfile.open(broken, "w:gz") as tar:
            for path in sorted(evil.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=str(path.relative_to(evil)))
        self.assertEqual(backup.main(["verify", str(broken)]), 1)

    def test_verify_detects_garbage_bytes(self) -> None:
        self._make_vault()
        rc, archive = self._create()
        self.assertEqual(rc, 0)
        data = bytearray(archive.read_bytes())
        for i in range(len(data) // 3, len(data) // 2):
            data[i] ^= 0xFF
        archive.write_bytes(bytes(data))
        rc = backup.main(["verify", str(archive)])
        self.assertEqual(rc, 2, "garbage archive must be refused with rc=2")


class BackupRestoreRefusals(BackupToolBase):

    def test_restore_refuses_non_empty_dir(self) -> None:
        self._make_vault()
        rc, archive = self._create()
        self.assertEqual(rc, 0)
        target = Path(tempfile.mkdtemp(prefix="so_test_nonempty_"))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        (target / "precious.json").write_text('{"keep": true}', encoding="utf-8")

        rc = backup.main(["restore", str(archive), "--to", str(target)])
        self.assertEqual(rc, 2, "non-empty target must be refused")
        # nothing was touched
        self.assertEqual(
            json.loads((target / "precious.json").read_text(encoding="utf-8")),
            {"keep": True})
        self.assertFalse((target / "data").exists())

    def test_restore_dry_run_writes_nothing(self) -> None:
        self._make_vault()
        rc, archive = self._create()
        self.assertEqual(rc, 0)
        target = Path(tempfile.mkdtemp(prefix="so_test_dry_"))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        rc = backup.main(
            ["restore", str(archive), "--to", str(target), "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(list(target.iterdir()), [], "dry-run must write nothing")

    def test_restore_over_live_vault_refused_without_force(self) -> None:
        # --to <repo root> is the live vault: refused even when empty-check
        # would pass, because --force is missing.
        self._make_vault()
        rc, archive = self._create()
        self.assertEqual(rc, 0)
        rc = backup.main(
            ["restore", str(archive), "--to", str(Path(backup.ROOT))])
        self.assertEqual(rc, 2)

    def test_live_restore_refused_while_server_running(self) -> None:
        # Spec item 5: never restore over the live vault while the server
        # runs — the pid/lock check fires FIRST, with or without --force,
        # and nothing is written.
        self._make_vault()
        rc, archive = self._create()
        self.assertEqual(rc, 0)
        import contextlib
        import io

        for extra in ([], ["--force"]):
            stderr = io.StringIO()
            with mock.patch.object(
                backup, "_server_running", return_value="api server pid 1234 is alive"
            ), contextlib.redirect_stderr(stderr):
                rc = backup.main(
                    ["restore", str(archive), "--to", str(Path(backup.ROOT))]
                    + extra
                )
            self.assertEqual(rc, 2, extra)
            self.assertIn("refusing to restore over the live vault", stderr.getvalue())
            self.assertIn("pid 1234", stderr.getvalue())

    def test_create_refuses_output_inside_repo(self) -> None:
        self._make_vault()
        inside = backup.ROOT / "should_never_backup_here"
        rc = backup.main(
            ["create", "--out", str(inside), "--allow-plain"])
        self.assertEqual(rc, 2)
        self.assertFalse(inside.exists())


class BackupEncryptionPolicy(BackupToolBase):

    def test_plain_refused_without_allow_plain(self) -> None:
        self._make_vault()
        have_tool = shutil.which("age") or shutil.which("gpg")
        saved = {
            k: os.environ.get(k)
            for k in (backup.AGE_RECIPIENT_ENV, backup.GPG_PASSPHRASE_ENV)
        }
        os.environ.pop(backup.AGE_RECIPIENT_ENV, None)
        os.environ.pop(backup.GPG_PASSPHRASE_ENV, None)
        try:
            rc = backup.main(["create", "--out", str(BACKUP_OUT)])
            if have_tool:
                # a tool exists but is unconfigured -> policy refusal (2)
                self.assertEqual(rc, 2)
            else:
                self.assertEqual(rc, 2)
                self.assertEqual(list(BACKUP_OUT.glob("*.tar.gz")), [])
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_encrypted_round_trip_or_skip(self) -> None:
        """age or gpg round trip — skipped with a message when uninstalled."""
        have_age = (shutil.which("age") is not None) and (
            shutil.which("age-keygen") is not None
        )
        have_gpg = shutil.which("gpg") is not None
        if not have_age and not have_gpg:
            self.skipTest(
                "encryption path skipped: neither 'age' nor 'gpg' is installed"
            )
        self._make_vault()
        if have_age:
            age_dir = tempfile.mkdtemp(prefix="so_age_")
            self.addCleanup(shutil.rmtree, age_dir, ignore_errors=True)
            key_path = Path(age_dir) / "key.txt"
            subprocess.run(
                ["age-keygen", "-o", str(key_path)],
                capture_output=True,
                check=True,
            )
            pub = subprocess.run(
                ["age-keygen", "-y", str(key_path)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            os.environ[backup.AGE_RECIPIENT_ENV] = pub
            os.environ[backup.AGE_IDENTITY_ENV] = str(key_path)

            rc = backup.main(
                ["create", "--out", str(BACKUP_OUT), "--encrypt", "age"]
            )
            self.assertEqual(rc, 0)
            archives = sorted(BACKUP_OUT.glob("*.tar.gz.age"))
            self.assertTrue(archives, "no .age archive produced")
            self.assertEqual(backup.main(["verify", str(archives[-1])]), 0)
            target = Path(tempfile.mkdtemp(prefix="so_test_age_"))
            self.addCleanup(shutil.rmtree, target, ignore_errors=True)
            rc = backup.main(
                ["restore", str(archives[-1]), "--to", str(target)]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(
                (target / "data/capsules/spool/cap_roundtrip/manifest.json")
                .is_file()
            )

        if have_gpg:
            os.environ[backup.GPG_PASSPHRASE_ENV] = "so-test-passphrase"
            rc = backup.main(
                ["create", "--out", str(BACKUP_OUT), "--encrypt", "gpg"]
            )
            self.assertEqual(rc, 0)
            archives = sorted(BACKUP_OUT.glob("*.tar.gz.gpg"))
            self.assertTrue(archives, "no .gpg archive produced")
            self.assertEqual(backup.main(["verify", str(archives[-1])]), 0)
            target = Path(tempfile.mkdtemp(prefix="so_test_gpg_"))
            self.addCleanup(shutil.rmtree, target, ignore_errors=True)
            rc = backup.main(
                ["restore", str(archives[-1]), "--to", str(target)]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(
                (target / "data/capsules/spool/cap_roundtrip/manifest.json")
                .is_file()
            )

    def test_backup_includes_and_restores_finance_ledger(self) -> None:
        self._make_vault()
        finance_dir = VAULT / "finance"
        finance_dir.mkdir(parents=True, exist_ok=True)
        ledger = finance_dir / "usage_ledger.jsonl"
        ledger_line = json.dumps({"action": "reserve", "cost": 0.05, "service": "gpt-4o-mini"}) + "\n"
        ledger.write_text(ledger_line, encoding="utf-8")

        rc, archive = self._create()
        self.assertEqual(rc, 0)
        self.assertEqual(backup.main(["verify", str(archive)]), 0)

        target = Path(tempfile.mkdtemp(prefix="so_test_finance_restore_"))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        rc = backup.main(["restore", str(archive), "--to", str(target)])
        self.assertEqual(rc, 0)

        restored_ledger = target / "data" / "finance" / "usage_ledger.jsonl"
        self.assertTrue(restored_ledger.is_file())
        self.assertEqual(restored_ledger.read_text(encoding="utf-8"), ledger_line)


if __name__ == "__main__":
    unittest.main()

