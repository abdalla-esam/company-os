#!/usr/bin/env python3
"""backup.py — consistent, verifiable backup / verify / restore of the vault.

Part 4 Phase 3 of the Sovereign Agency OS architecture.

What is backed up (the minimal stateful set — the SQLite read index is
excluded because it is rebuildable from these very files)::

    data/capsules/spool/**       capsule manifests + artifacts
    data/domains/*/tickets/**    scratch / approved / archive tickets
    data/memory/**               JSONL feedback + dedup ledgers
    data/governance/**           approval-gate queue + governance data
    data/queue/**                durable work-queue items
    data/assets/vault/**         binary asset vault
    config/**                    engine/users/governance/marketing configs
                                 (incl. users.json password hashes)

Excluded everywhere: sidecar ``*.lock`` files, ``*.tmp``,
``.read_index.sqlite3*``, ``.pulse_scan.json`` (rebuildable caches) and
``data/capsules/sandbox/**`` unless ``--include-sandbox`` is passed.

Consistency while the server runs (no change to the running system):
- each capsule is copied **while holding its own** ``CapsuleLock`` (with a
  timeout); a capsule whose lock stays busy is SKIPPED and reported, never
  torn;
- JSONL ledgers are read under a shared ``flock`` on the ledger file itself
  (the same inode ``append_jsonl_line`` locks exclusively) with a
  trailing-partial-line check: a torn tail is trimmed once and reported;
- the gate queue, governance and config JSON files are copied under their
  sidecar lock; a busy lock skips + reports that file.

Output: one timestamped ``.tar.gz`` archive plus a manifest with per-file
SHA-256 and counts. The archive may contain secret hashes (users.json,
audit ledgers), so it is ENCRYPTED by default when ``age`` or ``gpg`` is
available and configured; without a tool it stops with a clear message, and
an unencrypted archive requires an explicit ``--allow-plain``. The archive
is always written OUTSIDE the repository.

``--verify <archive>`` decrypts and extracts to a temp dir, re-checks every
checksum, loads every manifest / ticket / gate entry under the schema_version
rules, runs verify_chain / verify_ledger / verify_spool, and rebuilds a read
index in the temp dir. Failures are reported per item — it never stops at
the first one. Note: without ``AGENCY_AUDIT_SECRET`` in the environment the
keyed audit chains CANNOT be verified after a restore (see docs/BACKUP.md).

``restore <archive> --to <dir>`` refuses a non-empty target unless
``--force``; restoring over the LIVE ``data/`` additionally requires the
server to be stopped (checked via ``.company_up/api.pid`` and live lock
probes). ``--dry-run`` only lists what would be restored.

Retention: ``--keep-last N`` prunes older archives in the output directory.

Exit codes: 0 ok · 1 verification/restore failures · 2 usage or policy
refusal (no encryption tool, in-repo archive path, ...).

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Bootstrap the engine + repo paths from the known layout (same pattern as
# verify_audit.py) so the tool runs standalone without caller PYTHONPATH.
ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "engine"
for _p in (str(ROOT), str(ENGINE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from shared_kernel import paths as canon  # noqa: E402
from shared_kernel.atomic_io import sidecar_lock  # noqa: E402
from shared_kernel.atomic_io import LockTimeoutError as SidecarLockTimeoutError  # noqa: E402
from capsule_engine.locking import CapsuleLock  # noqa: E402
from capsule_engine.exceptions import LockTimeoutError as CapsuleLockTimeoutError  # noqa: E402

#: Both lock families mean "a live writer holds this — skip + report".
LOCK_TIMEOUT_ERRORS = (SidecarLockTimeoutError, CapsuleLockTimeoutError)

ARCHIVE_PREFIX = "sovereign-backup-"
ARCHIVE_SUFFIX = ".tar.gz"
MANIFEST_NAME = "backup_manifest.json"
BACKUP_SCHEMA_VERSION = 1

DEFAULT_CAPSULE_LOCK_TIMEOUT = 10.0
DEFAULT_SIDECAR_LOCK_TIMEOUT = 5.0

# Environment variables consulted by the ENCRYPTION layer only. The audit /
# capability secrets are deliberately NOT read here — they are stored
# separately (docs/BACKUP.md) and never end up inside the archive.
AGE_RECIPIENT_ENV = "AGENCY_BACKUP_AGE_RECIPIENT"
AGE_IDENTITY_ENV = "AGENCY_BACKUP_AGE_IDENTITY"
GPG_PASSPHRASE_ENV = "AGENCY_BACKUP_GPG_PASSPHRASE"
BACKUP_DIR_ENV = "AGENCY_BACKUP_DIR"


class BackupPolicyError(SystemExit):
    """A policy refusal (exit code 2) with a human-readable message."""

    def __init__(self, message: str) -> None:
        super().__init__(2)
        self.message = f"error: {message}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def _fail_policy(msg: str) -> None:
    raise BackupPolicyError(msg)


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# source resolution (env-resolvable so tests run against an isolated vault)
# --------------------------------------------------------------------------- #

def _gate_queue_arc() -> str:
    """Archive-relative path for the gate queue (canonical repo layout)."""
    return "data/governance/approval_gates/gate_queue"


def resolve_sources(include_sandbox: bool) -> List[Tuple[str, Path, str]]:
    """Return (arc_path, real_path, mode) triples for the backup set.

    ``mode`` is one of ``capsules`` (CapsuleLock per subdirectory),
    ``sidecar`` (each JSON file under its sidecar lock), or ``plain``
    (direct copy; JSONL files get the trailing-partial-line check).
    """
    sources: List[Tuple[str, Path, str]] = [
        ("data/capsules/spool", canon.CAPSULES_SPOOL, "capsules"),
        ("data/memory", canon.MEMORY_DIR, "plain"),
        ("data/queue", canon.WORK_QUEUE_DIR, "plain"),
        ("data/assets/vault", canon.ASSETS_VAULT, "plain"),
    ]
    finance_dir = getattr(canon, "FINANCE_DIR", canon.DATA_ROOT / "finance")
    if finance_dir.is_dir():
        sources.append(("data/finance", finance_dir, "plain"))
    if include_sandbox:

        sources.append(("data/capsules/sandbox", canon.CAPSULES_SANDBOX, "capsules"))

    # Domain tickets: only the tickets subtree of each domain (never the
    # domain's code or deliverables).
    if canon.DOMAINS_DIR.is_dir():
        for domain in sorted(canon.DOMAINS_DIR.iterdir()):
            tickets = domain / "tickets"
            if domain.is_dir() and tickets.is_dir():
                sources.append(
                    (f"data/domains/{domain.name}/tickets", tickets, "plain")
                )

    # Governance tree (approval gates + any governance data files). Prod:
    # <repo>/data/governance. Custom layouts via AGENCY_BACKUP_GOVERNANCE_DIR.
    gov_dir: Optional[Path] = None
    env_gov = os.environ.get("AGENCY_BACKUP_GOVERNANCE_DIR", "").strip()
    if env_gov:
        gov_dir = Path(env_gov)
    elif canon.GATE_QUEUE_DIR.name == "gate_queue" and \
            canon.GATE_QUEUE_DIR.parent.name == "approval_gates":
        gov_dir = canon.GATE_QUEUE_DIR.parents[1]  # .../data/governance
    gate_queue_captured = False
    if gov_dir is not None and gov_dir.is_dir():
        sources.append(("data/governance", gov_dir, "sidecar"))
        gate_queue_captured = _is_within(canon.GATE_QUEUE_DIR, gov_dir) \
            if canon.GATE_QUEUE_DIR.is_dir() else True
    # Standalone gate queue (custom layouts / test vaults).
    if not gate_queue_captured and canon.GATE_QUEUE_DIR.is_dir():
        sources.append((_gate_queue_arc(), canon.GATE_QUEUE_DIR, "sidecar"))

    # config/** — env override for isolated layouts, else the repo config dir.
    config_dir = Path(os.environ.get("AGENCY_BACKUP_CONFIG_DIR", "").strip()) \
        if os.environ.get("AGENCY_BACKUP_CONFIG_DIR", "").strip() \
        else canon.CONFIG_ROOT
    if config_dir.is_dir():
        sources.append(("config", config_dir, "sidecar"))

    return sources


#: Names excluded from every source (sidecar locks, temp files, rebuildable
#: caches and interpreter litter). Matched against the whole relative path.
def _excluded(rel_parts: Tuple[str, ...]) -> bool:
    name = rel_parts[-1]
    if name.endswith(".lock") or name.endswith(".tmp"):
        return True
    if name.startswith(".read_index.sqlite3"):
        return True
    if name in (".pulse_scan.json", ".DS_Store", ".gitkeep", "__pycache__"):
        return True
    if name.endswith(".pyc"):
        return True
    return False


# --------------------------------------------------------------------------- #
# consistent readers
# --------------------------------------------------------------------------- #

def _read_jsonl_consistent(path: Path) -> Tuple[bytes, Optional[str]]:
    """Read a JSONL file under a shared flock with a torn-tail check.

    ``append_jsonl_line`` takes an exclusive flock on the ledger inode
    itself, so a shared lock here excludes any in-flight append. A missing
    trailing newline while we hold the lock means a crashed writer left a
    permanent torn tail: it is trimmed to the last complete line and
    reported as a warning (the backup stays loadable line-by-line).
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            data = b""
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                data += chunk
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    if data and not data.endswith(b"\n"):
        # No-fcntl platforms: one short retry in case an appender was mid-write.
        if fcntl is None:
            time.sleep(0.2)
            data2 = path.read_bytes()
            if data2.endswith(b"\n"):
                return data2, None
        last_nl = data.rfind(b"\n")
        trimmed = data[: last_nl + 1] if last_nl >= 0 else b""
        return trimmed, (
            "torn trailing JSONL line trimmed (%d bytes dropped)"
            % (len(data) - len(trimmed))
        )
    return data, None


def _copy_capsule(
    capsule_dir: Path,
    dest_dir: Path,
    *,
    lock_timeout: float,
    rel_prefix: str,
    files: List[Dict[str, Any]],
    skipped: List[Dict[str, str]],
) -> None:
    """Copy one capsule while holding its own CapsuleLock."""
    try:
        lock = CapsuleLock(
            capsule_dir, timeout=lock_timeout, purpose="backup:snapshot"
        )
    except Exception as exc:  # missing dir, unreadable lock file, ...
        skipped.append({"path": rel_prefix, "reason": str(exc)})
        return
    try:
        with lock:
            dest_dir.mkdir(parents=True, exist_ok=True)
            for path in sorted(capsule_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel_parts = path.relative_to(capsule_dir).parts
                if _excluded(rel_parts):
                    continue
                target = dest_dir.joinpath(*rel_parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                files.append(
                    {
                        "path": "/".join((rel_prefix,) + rel_parts),
                        "sha256": _sha256_file(target),
                        "size": target.stat().st_size,
                    }
                )
    except LOCK_TIMEOUT_ERRORS as exc:
        skipped.append({"path": rel_prefix, "reason": f"capsule lock busy: {exc}"})
    except OSError as exc:
        skipped.append({"path": rel_prefix, "reason": f"copy failed: {exc}"})


def _copy_one(
    src: Path,
    arc_path: str,
    dest: Path,
    mode: str,
    files: List[Dict[str, Any]],
    skipped: List[Dict[str, str]],
    warnings: List[str],
    *,
    sidecar_timeout: float,
) -> None:
    """Copy a single file honoring its consistency mode."""
    try:
        if src.suffix == ".jsonl":
            data, warn = _read_jsonl_consistent(src)
            if warn:
                warnings.append(f"{arc_path}: {warn}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        elif mode == "sidecar":
            with sidecar_lock(
                src, timeout=sidecar_timeout, purpose="backup:copy"
            ):
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    except LOCK_TIMEOUT_ERRORS as exc:
        skipped.append({"path": arc_path, "reason": f"sidecar lock busy: {exc}"})
        return
    except FileNotFoundError:
        # A rename raced us (e.g. a dispatcher claim): the file is gone from
        # this snapshot; the next backup captures its new location.
        skipped.append({"path": arc_path, "reason": "vanished mid-copy (renamed)"})
        return
    except OSError as exc:
        skipped.append({"path": arc_path, "reason": f"copy failed: {exc}"})
        return
    files.append(
        {
            "path": arc_path,
            "sha256": _sha256_file(dest),
            "size": dest.stat().st_size,
        }
    )


def _category_of(arc_path: str) -> str:
    if arc_path.startswith("data/capsules/"):
        return "capsules"
    if "/tickets/" in arc_path or arc_path.endswith(".json.claim"):
        return "tickets"
    if "gate_" in Path(arc_path).name and "gate_queue" in arc_path:
        return "gate_entries"
    if arc_path.startswith("config/"):
        return "config"
    if arc_path.startswith("data/memory/"):
        return "memory"
    if arc_path.startswith("data/queue/"):
        return "queue"
    if arc_path.startswith("data/assets/"):
        return "assets"
    if arc_path.startswith("data/finance/"):
        return "finance"
    return "other"


# --------------------------------------------------------------------------- #
# snapshot (create)
# --------------------------------------------------------------------------- #

def build_snapshot(
    staging: Path,
    *,
    include_sandbox: bool,
    capsule_lock_timeout: float,
    sidecar_lock_timeout: float,
) -> Dict[str, Any]:
    """Populate ``staging/`` with the vault and return the manifest dict."""
    files: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    warnings: List[str] = []

    for arc_prefix, real_dir, mode in resolve_sources(include_sandbox):
        if not real_dir.is_dir():
            warnings.append(f"source missing, skipped: {arc_prefix} -> {real_dir}")
            continue
        if mode == "capsules":
            # Capsule subdirectories get their own lock; loose files at the
            # spool root (e.g. the .audit_ledger.jsonl) are copied plain.
            for entry in sorted(real_dir.iterdir()):
                rel = entry.relative_to(real_dir).parts
                if _excluded(rel):
                    continue
                arc = "/".join((arc_prefix,) + rel)
                if entry.is_dir():
                    _copy_capsule(
                        entry,
                        staging / arc_prefix / entry.name,
                        lock_timeout=capsule_lock_timeout,
                        rel_prefix=arc,
                        files=files,
                        skipped=skipped,
                    )
                elif entry.is_file():
                    _copy_one(
                        entry,
                        arc,
                        staging / arc,
                        mode,
                        files,
                        skipped,
                        warnings,
                        sidecar_timeout=sidecar_lock_timeout,
                    )
        else:
            for path in sorted(real_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(real_dir).parts
                if _excluded(rel):
                    continue
                arc = "/".join((arc_prefix,) + rel)
                _copy_one(
                    path,
                    arc,
                    staging / arc,
                    mode,
                    files,
                    skipped,
                    warnings,
                    sidecar_timeout=sidecar_lock_timeout,
                )

    counts: Dict[str, int] = {"files": len(files), "bytes": sum(f["size"] for f in files)}
    for entry in files:
        cat = _category_of(entry["path"])
        counts[cat] = counts.get(cat, 0) + 1
    counts["skipped"] = len(skipped)
    counts["warnings"] = len(warnings)

    return {
        "backup_schema_version": BACKUP_SCHEMA_VERSION,
        "kind": "sovereign_agency_os_backup",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "include_sandbox": bool(include_sandbox),
        "counts": counts,
        "files": sorted(files, key=lambda f: f["path"]),
        "skipped": skipped,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# encryption gate
# --------------------------------------------------------------------------- #

def _gpg_passphrase() -> Optional[bytes]:
    value = os.environ.get(GPG_PASSPHRASE_ENV, "")
    return value.encode("utf-8") if value else None


def resolve_encryption(
    requested: str, allow_plain: bool
) -> str:
    """Decide the encryption mode; refuse (exit 2) when policy says no.

    ``requested`` is one of ``auto|age|gpg|none``.
    """
    have_age = shutil.which("age") is not None
    have_gpg = shutil.which("gpg") is not None
    age_recipient = os.environ.get(AGE_RECIPIENT_ENV, "").strip()
    gpg_pass = _gpg_passphrase() is not None

    if requested == "none":
        if not allow_plain:
            _fail_policy(
                "refusing to write an UNENCRYPTED archive (it may contain "
                "secret hashes). Install 'age' or 'gpg' and set "
                f"{AGE_RECIPIENT_ENV} / {GPG_PASSPHRASE_ENV}, or pass "
                "--allow-plain to accept a plaintext archive."
            )
        return "none"
    if requested == "age":
        if not have_age:
            _fail_policy("--encrypt age requested but the 'age' binary is not installed")
        if not age_recipient:
            _fail_policy(f"--encrypt age needs a recipient: set ${AGE_RECIPIENT_ENV}")
        return "age"
    if requested == "gpg":
        if not have_gpg:
            _fail_policy("--encrypt gpg requested but the 'gpg' binary is not installed")
        if not gpg_pass:
            _fail_policy(f"--encrypt gpg needs a passphrase: set ${GPG_PASSPHRASE_ENV}")
        return "gpg"

    # auto
    if have_age and age_recipient:
        return "age"
    if have_gpg and gpg_pass:
        return "gpg"
    if allow_plain:
        print(
            "warning: no encryption tool available/configured — writing a "
            "PLAINTEXT archive because --allow-plain was passed",
            file=sys.stderr,
        )
        return "none"
    _fail_policy(
        "no encryption tool available: install 'age' and set "
        f"${AGE_RECIPIENT_ENV} (or 'gpg' and set ${GPG_PASSPHRASE_ENV}); "
        "alternatively pass --allow-plain to write an unencrypted archive."
    )
    return "none"  # unreachable


def _encrypt_file(plain: Path, final: Path, mode: str) -> None:
    """Encrypt ``plain`` to ``final`` using the chosen tool."""
    if mode == "none":
        shutil.move(str(plain), str(final))
        return
    if mode == "age":
        recipient = os.environ[AGE_RECIPIENT_ENV].strip()
        cmd = ["age", "-r", recipient, "-o", str(final), str(plain)]
    else:  # gpg symmetric; passphrase via fd 0, never on argv
        cmd = [
            "gpg", "--batch", "--yes", "--symmetric",
            "--cipher-algo", "AES256", "--passphrase-fd", "0",
            "-o", str(final), str(plain),
        ]
    proc = subprocess.run(
        cmd,
        input=_gpg_passphrase() or b"",
        capture_output=True,
    )
    if proc.returncode != 0:
        final.unlink(missing_ok=True)
        raise RuntimeError(
            f"encryption failed ({mode}): {proc.stderr.decode('utf-8', 'replace')[-400:]}"
        )


def _decrypt_file(archive: Path, workdir: Path) -> Path:
    """Decrypt ``archive`` if needed; always returns a plain .tar.gz path."""
    name = archive.name
    if name.endswith(".age"):
        identity = os.environ.get(AGE_IDENTITY_ENV, "").strip() or \
            os.environ.get("AGE_IDENTITY_FILE", "").strip()
        if not shutil.which("age"):
            _fail_policy("archive is age-encrypted but the 'age' binary is not installed")
        if not identity:
            _fail_policy(
                f"archive is age-encrypted: set ${AGE_IDENTITY_ENV} to the "
                "identity file that matches the backup recipient"
            )
        out = workdir / "decrypted.tar.gz"
        proc = subprocess.run(
            ["age", "-d", "-i", identity, "-o", str(out), str(archive)],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "age decryption failed: "
                + proc.stderr.decode("utf-8", "replace")[-400:]
            )
        return out
    if name.endswith(".gpg"):
        if not shutil.which("gpg"):
            _fail_policy("archive is gpg-encrypted but the 'gpg' binary is not installed")
        if _gpg_passphrase() is None:
            _fail_policy(
                f"archive is gpg-encrypted: set ${GPG_PASSPHRASE_ENV} to the "
                "backup passphrase"
            )
        out = workdir / "decrypted.tar.gz"
        proc = subprocess.run(
            ["gpg", "--batch", "--decrypt", "--passphrase-fd", "0",
             "-o", str(out), str(archive)],
            input=_gpg_passphrase() or b"",
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "gpg decryption failed: "
                + proc.stderr.decode("utf-8", "replace")[-400:]
            )
        return out
    return archive  # already plain


# --------------------------------------------------------------------------- #
# safe extraction
# --------------------------------------------------------------------------- #

def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract refusing absolute paths and `..` traversal."""
    dest = dest.resolve()
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or name.startswith("\\"):
            _fail_policy(f"archive member has an absolute path: {name}")
        target = (dest / name).resolve()
        if not _is_within(target, dest):
            _fail_policy(f"archive member escapes the extraction dir: {name}")
        if not (member.isfile() or member.isdir()):
            _fail_policy(f"archive member is not a regular file: {name}")
    tar.extractall(dest)


def _extract_archive(archive: Path) -> Tuple[tempfile.TemporaryDirectory, Path]:
    """Decrypt + extract an archive into a fresh temp dir. Caller cleans up."""
    workdir = tempfile.TemporaryDirectory(prefix="sovereign_backup_")
    work = Path(workdir.name)
    plain = _decrypt_file(archive, work)
    try:
        tar = tarfile.open(plain, "r:gz")
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        # torn/garbage gzip streams surface here (EOFError from gzip,
        # BadGzipFile as OSError, zlib.error on raw stream damage)
        workdir.cleanup()
        _fail_policy(f"cannot read archive (corrupt or truncated): {exc}")
    extract_root = work / "extracted"
    extract_root.mkdir()
    try:
        with tar:
            _safe_extract(tar, extract_root)
    except (EOFError, OSError, zlib.error) as exc:  # torn/garbage gzip stream
        workdir.cleanup()
        _fail_policy(f"archive is corrupt or truncated: {exc}")
    return workdir, extract_root


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #

def _load_manifest_strict(path: Path, kind: str) -> Tuple[Optional[dict], Optional[str]]:
    """Load one JSON file under schema_version rules; (data, error)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable JSON: {exc}"
    try:
        from shared_kernel.schema_version import (
            ALL_KINDS,
            check_schema_version,
        )
        if kind in ALL_KINDS:
            check_schema_version(kind, data, path)
    except ImportError:
        pass
    except Exception as exc:  # SchemaVersionError — fail closed, reported
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, None


def cmd_verify(args: argparse.Namespace) -> int:
    from capsule_engine.audit import (
        read_ledger,
        resolve_audit_secret,
        verify_chain,
        verify_ledger,
        verify_spool,
    )
    from shared_kernel.schema_version import (
        KIND_GATE_ENTRY,
        KIND_GOVERNANCE,
        KIND_TICKET,
        KIND_USERS,
    )

    archive = Path(args.archive).resolve()
    if not archive.is_file():
        print(f"error: archive not found: {archive}", file=sys.stderr)
        return 2

    failures: List[str] = []
    checks = 0
    try:
        workdir, root = _extract_archive(archive)
    except BackupPolicyError as exc:
        print(exc.message, file=sys.stderr)
        return 2
    try:
        # 1. manifest + schema of the backup itself
        manifest_path = root / MANIFEST_NAME
        manifest, err = _load_manifest_strict(manifest_path, "")
        if err or manifest is None:
            print(f"error: backup manifest unusable: {err}", file=sys.stderr)
            return 2
        if manifest.get("backup_schema_version") != BACKUP_SCHEMA_VERSION:
            print(
                "error: unsupported backup_schema_version "
                f"{manifest.get('backup_schema_version')!r} "
                f"(supported: {BACKUP_SCHEMA_VERSION})",
                file=sys.stderr,
            )
            return 2

        # 2. per-file checksums (every file, not just the first failure)
        listed = {entry["path"]: entry for entry in manifest.get("files", [])}
        for rel, entry in listed.items():
            checks += 1
            fpath = root / rel
            if not fpath.is_file():
                failures.append(f"missing file: {rel}")
                continue
            actual = _sha256_file(fpath)
            if actual != entry["sha256"]:
                failures.append(
                    f"checksum mismatch: {rel} (manifest {entry['sha256'][:12]}… "
                    f"actual {actual[:12]}…)"
                )
        for path in sorted(root.rglob("*")):
            if path.is_file():
                rel = str(path.relative_to(root))
                if rel != MANIFEST_NAME and rel not in listed:
                    checks += 1
                    failures.append(f"file present but not in manifest: {rel}")

        secret = resolve_audit_secret(None)

        # 3. capsule manifests under schema rules + hash chains
        spool = root / "data" / "capsules" / "spool"
        if spool.is_dir():
            for capsule_dir in sorted(spool.iterdir()):
                if not capsule_dir.is_dir() or capsule_dir.name.startswith("."):
                    continue
                manifest_file = capsule_dir / "manifest.json"
                checks += 1
                if not manifest_file.is_file():
                    failures.append(f"capsule without manifest: {capsule_dir.name}")
                    continue
                data, err = _load_manifest_strict(manifest_file, "manifest")
                if err:
                    failures.append(f"capsule {capsule_dir.name}: {err}")
                    continue
                history = data.get("history")
                if not isinstance(history, list):
                    failures.append(
                        f"capsule {capsule_dir.name}: history is not a list"
                    )
                    continue
                status = verify_chain(history, secret=secret)
                if not status.ok:
                    failures.append(
                        f"capsule {capsule_dir.name}: audit chain broken: {status.detail}"
                    )

        # 4. spool ledger
        if spool.is_dir():
            checks += 1
            entries = read_ledger(spool)
            status = verify_ledger(entries, secret=secret)
            if not status.ok:
                failures.append(f"spool ledger: {status.detail}")

            # 5. whole-spool verification (verify_audit's spool pass)
            checks += 1
            report = verify_spool(spool, secret=secret)
            for finding in report.findings:
                if finding.severity == "critical":
                    failures.append(
                        "verify_spool: "
                        f"[{finding.kind}] {finding.capsule_id or '-'}: {finding.detail}"
                    )

        # 6. tickets + gate entries under schema rules
        for ticket_path in sorted((root / "data").glob("domains/*/tickets/**/ticket_*.json")):
            checks += 1
            _, err = _load_manifest_strict(ticket_path, KIND_TICKET)
            if err:
                failures.append(
                    f"ticket {ticket_path.relative_to(root)}: {err}"
                )
        for gate_path in sorted(root.glob("data/governance/**/gate_*.json")):
            checks += 1
            _, err = _load_manifest_strict(gate_path, KIND_GATE_ENTRY)
            if err:
                failures.append(
                    f"gate entry {gate_path.relative_to(root)}: {err}"
                )
        for cfg_kind, cfg_rel in (
            (KIND_USERS, "config/users.json"),
            (KIND_GOVERNANCE, "config/governance.json"),
        ):
            cfg_path = root / cfg_rel
            if cfg_path.is_file():
                checks += 1
                _, err = _load_manifest_strict(cfg_path, cfg_kind)
                if err:
                    failures.append(f"{cfg_rel}: {err}")

        # 7. rebuild a read index in the temp dir (proves the snapshot is
        #    consumable by the live projection layer)
        checks += 1
        idx_rc = _rebuild_read_index_in(root)
        if idx_rc != 0:
            failures.append(f"read index rebuild in temp dir failed (rc={idx_rc})")

    finally:
        workdir.cleanup()

    print(f"Verify: {archive}")
    print(f"Checks run: {checks}")
    if failures:
        print(f"Status: FAILED ({len(failures)} problem(s))")
        for item in failures:
            print(f"  [!] {item}")
        return 1
    print("Status: INTACT (OK)")
    if secret is None and manifest.get("counts", {}).get("capsules", 0):
        print(
            "note: AGENCY_AUDIT_SECRET not set — keyed audit chains were NOT "
            "cryptographically verified (see docs/BACKUP.md)"
        )
    return 0


def _rebuild_read_index_in(root: Path) -> int:
    """Rebuild the SQLite read index against the extracted tree (subprocess)."""
    data = root / "data"
    env = dict(os.environ)
    env.update(
        {
            "CAPSULES_SPOOL": str(data / "capsules" / "spool"),
            "CAPSULES_SANDBOX": str(data / "capsules" / "sandbox"),
            "DOMAINS_DIR": str(data / "domains"),
            "GATE_QUEUE_DIR": str(data / "governance" / "approval_gates" / "gate_queue"),
            "AGENCY_WORK_QUEUE_DIR": str(data / "queue"),
            "AGENCY_MEMORY_DIR": str(data / "memory"),
            "AGENCY_MEMORY_FILE": str(data / "memory" / "feedback_history.jsonl"),
            "AGENCY_DEDUP_FILE": str(data / "memory" / "dedup_index.jsonl"),
            "ASSETS_VAULT_DIR": str(data / "assets" / "vault"),
            "AGENCY_INDEX": "on",
            "AGENCY_INDEX_FILE": str(root / "read_index.sqlite3"),
            "READ_INDEX_PATH": str(root / "read_index.sqlite3"),
        }
    )
    engine_cfg = root / "config" / "engine.json"
    if engine_cfg.is_file():
        env["AGENCY_ENGINE_CONFIG"] = str(engine_cfg)
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(ENGINE_DIR)!r}); "
        "from orchestration import read_index; "
        "print('REBUILT', read_index.rebuild())"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=str(root), capture_output=True
    )
    if proc.returncode != 0:
        sys.stderr.write(
            proc.stderr.decode("utf-8", "replace")[-800:] + "\n"
        )
    return proc.returncode


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #

def _prune_old_archives(out_dir: Path, keep_last: int) -> List[str]:
    """Delete the oldest matching archives beyond ``keep_last``. Returns names."""
    archives = sorted(
        p for p in out_dir.iterdir()
        if p.is_file()
        and p.name.startswith(ARCHIVE_PREFIX)
        and (p.name.endswith(ARCHIVE_SUFFIX)
             or p.name.endswith(ARCHIVE_SUFFIX + ".age")
             or p.name.endswith(ARCHIVE_SUFFIX + ".gpg"))
    )
    deleted: List[str] = []
    for old in archives[:-keep_last] if keep_last > 0 else []:
        old.unlink(missing_ok=True)
        Path(str(old) + ".manifest.json").unlink(missing_ok=True)
        deleted.append(old.name)
    return deleted


def cmd_create(args: argparse.Namespace) -> int:
    # 1. output directory: OUTSIDE the repo, or refuse.
    out_dir = Path(
        args.out or os.environ.get(BACKUP_DIR_ENV, "").strip()
        or (Path.home() / "sovereign_backups")
    ).expanduser()
    out_dir = out_dir.resolve()
    if _is_within(out_dir, ROOT):
        _fail_policy(
            f"output directory {out_dir} is INSIDE the repository — backups "
            "may contain secret hashes and must live outside the repo (and "
            "therefore outside git). Pass --out elsewhere or set "
            f"${BACKUP_DIR_ENV}."
        )

    # 2. encryption policy gate (before touching anything).
    encrypt_mode = resolve_encryption(args.encrypt, args.allow_plain)

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now_stamp()
    base = out_dir / f"{ARCHIVE_PREFIX}{stamp}{ARCHIVE_SUFFIX}"
    final = Path(str(base) + (f".{encrypt_mode}" if encrypt_mode != "none" else ""))
    counter = 1
    while final.exists() or Path(str(final) + ".manifest.json").exists():
        final = Path(str(base) + f"-{counter}"
                     + (f".{encrypt_mode}" if encrypt_mode != "none" else ""))
        counter += 1

    # 3. snapshot into a staging dir, then tar -> encrypt -> finalize.
    staging_root = tempfile.TemporaryDirectory(prefix="sovereign_backup_stage_")
    staging = Path(staging_root.name) / "tree"
    staging.mkdir(parents=True, exist_ok=True)
    plain_tar: Optional[Path] = None
    try:
        manifest = build_snapshot(
            staging,
            include_sandbox=args.include_sandbox,
            capsule_lock_timeout=args.lock_timeout,
            sidecar_lock_timeout=args.lock_timeout,
        )
        manifest["encryption"] = encrypt_mode

        plain_tar = Path(staging_root.name) / "archive.tar.gz"
        manifest_tmp = Path(staging_root.name) / MANIFEST_NAME
        manifest_tmp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with tarfile.open(plain_tar, "w:gz") as tar:
            tar.add(manifest_tmp, arcname=MANIFEST_NAME)
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=str(path.relative_to(staging)))

        _encrypt_file(plain_tar, final, encrypt_mode)
        plain_tar = None

        # 4. manifest sidecar: full per-file manifest only for plaintext
        #    archives; encrypted archives keep it inside the encryption.
        sidecar = Path(str(final) + ".manifest.json")
        if encrypt_mode == "none":
            sidecar.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            slim = {
                k: manifest[k]
                for k in (
                    "backup_schema_version", "kind", "created_at", "host",
                    "encryption", "include_sandbox", "counts",
                )
            }
            slim["archive"] = final.name
            sidecar.write_text(
                json.dumps(slim, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        # 5. retention
        deleted: List[str] = []
        if args.keep_last is not None and args.keep_last > 0:
            deleted = _prune_old_archives(out_dir, args.keep_last)

        result = {
            "status": "ok",
            "archive": str(final),
            "manifest": str(sidecar),
            "encryption": encrypt_mode,
            "counts": manifest["counts"],
            "skipped": manifest["skipped"],
            "warnings": manifest["warnings"],
            "pruned": deleted,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        staging_root.cleanup()
        if plain_tar is not None:
            plain_tar.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #

def _server_running() -> Optional[str]:
    """Return a description of the running server, or None if stopped."""
    pid_file = ROOT / ".company_up" / "api.pid"
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return f"api server pid {pid} is alive"
        except (ValueError, ProcessLookupError):
            pass
        except PermissionError:
            return f"api server pid {pid} is alive (owned by another user)"
    # Probe live capsule locks without creating any: an exclusively-held
    # .lock means a writer (pulse/worker) is mid-write right now.
    spool = canon.CAPSULES_SPOOL
    if spool.is_dir() and fcntl is not None:
        for lock_file in sorted(spool.glob("*/.lock")):
            try:
                fd = os.open(lock_file, os.O_RDWR)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return f"capsule lock held: {lock_file.parent.name}"
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    return None


def _is_live_target(target: Path) -> bool:
    """True when restoring into ``target`` would touch the live vault."""
    resolved = target.resolve()
    for live in (canon.REPO_ROOT, canon.DATA_ROOT, canon.CONFIG_ROOT):
        if resolved == live.resolve():
            return True
    return False


def cmd_restore(args: argparse.Namespace) -> int:
    archive = Path(args.archive).resolve()
    if not archive.is_file():
        print(f"error: archive not found: {archive}", file=sys.stderr)
        return 2
    target = Path(args.to).expanduser().resolve()

    # Live-vault guard FIRST: never even extract-and-plan over a running
    # system without --force and a stopped server.
    live = _is_live_target(target)
    running = _server_running() if live else None
    if live and running:
        # Whether or not --force was passed: a running server (pid file or a
        # held capsule lock) makes a live restore unsafe either way, and this
        # is the refusal the operator needs to see first.
        print(
            f"error: refusing to restore over the live vault: {running}. "
            "Stop the server first (bash ops/server_boot.sh --stop).",
            file=sys.stderr,
        )
        return 2

    # Non-empty target requires --force.
    if target.is_dir() and any(target.iterdir()) and not args.force:
        print(
            f"error: target {target} is not empty — pass --force to restore "
            "into it anyway (this overwrites matching files).",
            file=sys.stderr,
        )
        return 2

    try:
        workdir, root = _extract_archive(archive)
    except BackupPolicyError as exc:
        print(exc.message, file=sys.stderr)
        return 2
    try:
        manifest, err = _load_manifest_strict(root / MANIFEST_NAME, "")
        if err or manifest is None:
            print(f"error: backup manifest unusable: {err}", file=sys.stderr)
            return 2
        if manifest.get("backup_schema_version") != BACKUP_SCHEMA_VERSION:
            print(
                "error: unsupported backup_schema_version "
                f"{manifest.get('backup_schema_version')!r}",
                file=sys.stderr,
            )
            return 2

        entries = manifest.get("files", [])

        if args.dry_run:
            print(f"Would restore {len(entries)} file(s) into {target}:")
            for entry in entries:
                print(f"  {entry['path']}  ({entry['size']} bytes)")
            if live:
                print("note: target is the LIVE vault directory")
            return 0

        # Verify checksums before writing anything (fail closed on corrupt).
        bad = [
            entry["path"] for entry in entries
            if not (root / entry["path"]).is_file()
            or _sha256_file(root / entry["path"]) != entry["sha256"]
        ]
        if bad:
            print(
                f"error: archive corrupt — {len(bad)} file(s) fail checksum, "
                "refusing to restore:",
                file=sys.stderr,
            )
            for rel in bad[:20]:
                print(f"  [!] {rel}", file=sys.stderr)
            return 1

        restored = 0
        for entry in entries:
            src = root / entry["path"]
            dest = target / entry["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            restored += 1

        print(
            json.dumps(
                {
                    "status": "ok",
                    "restored": restored,
                    "target": str(target),
                    "live_target": live,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if live:
            print(
                "next: restart the server, then rebuild the read index "
                "(POST /api/index/rebuild). Without AGENCY_AUDIT_SECRET the "
                "audit chains cannot be verified — see docs/BACKUP.md."
            )
        return 0
    finally:
        workdir.cleanup()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup.py",
        description="Consistent, verifiable backup / verify / restore of the vault.",
    )
    subparsers = parser.add_subparsers(dest="subcommand")

    p_create = subparsers.add_parser("create", help="take a consistent snapshot")
    p_create.add_argument(
        "--out",
        help=f"output directory OUTSIDE the repo (default: ${BACKUP_DIR_ENV} "
        "or ~/sovereign_backups)",
    )
    p_create.add_argument(
        "--encrypt",
        choices=("auto", "age", "gpg", "none"),
        default="auto",
        help="encryption tool (default: auto — age, then gpg)",
    )
    p_create.add_argument(
        "--allow-plain",
        action="store_true",
        help="accept writing an UNENCRYPTED archive (refused otherwise)",
    )
    p_create.add_argument(
        "--include-sandbox",
        action="store_true",
        help="also back up data/capsules/sandbox (excluded by default)",
    )
    p_create.add_argument(
        "--lock-timeout",
        type=float,
        default=DEFAULT_CAPSULE_LOCK_TIMEOUT,
        help="seconds to wait on a busy capsule/sidecar lock before skipping "
        f"(default: {DEFAULT_CAPSULE_LOCK_TIMEOUT:.0f})",
    )
    p_create.add_argument(
        "--keep-last",
        type=int,
        default=None,
        metavar="N",
        help="retention: keep only the N newest archives in the output dir",
    )
    p_create.set_defaults(handler=cmd_create)

    p_verify = subparsers.add_parser(
        "verify", help="verify an archive end to end (checksums + semantics)"
    )
    p_verify.add_argument("archive", help="path to the backup archive")
    p_verify.set_defaults(handler=cmd_verify)

    p_restore = subparsers.add_parser("restore", help="restore an archive")
    p_restore.add_argument("archive", help="path to the backup archive")
    p_restore.add_argument(
        "--to", required=True, metavar="DIR", help="target directory"
    )
    p_restore.add_argument(
        "--force",
        action="store_true",
        help="allow a non-empty target / the live vault (server must be "
        "stopped for the live vault)",
    )
    p_restore.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be restored, write nothing",
    )
    p_restore.set_defaults(handler=cmd_restore)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Accept the spec's "--verify <archive>" spelling as a synonym.
    if argv and argv[0] == "--verify":
        argv = ["verify"] + argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    try:
        return args.handler(args)
    except BackupPolicyError as exc:
        print(exc.message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
