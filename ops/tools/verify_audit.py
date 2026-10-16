#!/usr/bin/env python3
"""verify_audit.py — operator CLI for hash-chain and ledger verification.

This tool verifies the cryptographic integrity of capsule histories and
spool-level audit ledgers.

Commands::

    # Verify the entire spool directory and ledger
    python3 tools/verify_audit.py spool [spool_dir]

    # Verify a single capsule's history chain
    python3 tools/verify_audit.py capsule <capsule_dir>

    # Verify the spool ledger file alone
    python3 tools/verify_audit.py ledger [spool_dir]

Exit codes:
    0: Valid and intact
    1: Tampering, broken chain, or head mismatch detected
    2: Refusal (bad usage, missing file, unreadable data)

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "engine"
for _p in (str(ROOT), str(ENGINE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from capsule_engine.audit import (  # noqa: E402
    AUDIT_SECRET_ENV,
    CAPABILITY_SECRET_ENV,
    head_of,
    read_ledger,
    resolve_audit_secret,
    verify_chain,
    verify_ledger,
    verify_spool,
)


def _default_spool() -> Path:
    """Where the capsules actually live — the verifier's own default.

    ``orchestration/api.py`` and ``orchestration/read_index.py`` both resolve
    ``CAPSULES_SPOOL`` with this exact fallback, so the tool must too. It used
    to point at ``<root>/spool``, a directory that has never existed here: an
    operator who ran the chain verifier got "directory does not exist" and no
    verdict at all, while believing the audit chain had been checked.
    """
    default_dir = ROOT / "data" / "capsules" / "spool"
    if not default_dir.exists() and (ROOT / "capsules" / "spool").exists():
        default_dir = ROOT / "capsules" / "spool"
    return Path(os.environ.get("CAPSULES_SPOOL", default_dir)).resolve()


def cmd_spool(args: argparse.Namespace) -> int:
    spool_dir = Path(args.spool_dir).resolve() if args.spool_dir else _default_spool()
    if not spool_dir.is_dir():
        print(f"error: spool directory does not exist: {spool_dir}", file=sys.stderr)
        return 2

    secret = resolve_audit_secret(args.secret)
    report = verify_spool(spool_dir, secret=secret)

    print(f"Spool Verification: {spool_dir}")
    print(f"Checked Capsules: {report.checked_capsules}")
    print(f"Status: {'INTACT (OK)' if report.ok else 'TAMPERED / CORRUPT'}")

    if report.findings:
        print("\nFindings:")
        for finding in report.findings:
            prefix = "[!]" if finding.severity == "critical" else "[?]"
            cid_str = f" [{finding.capsule_id}]" if finding.capsule_id else ""
            print(f"  {prefix} ({finding.severity.upper()}){cid_str} {finding.kind}: {finding.detail}")

    return 0 if report.ok else 1


def cmd_capsule(args: argparse.Namespace) -> int:
    cap_dir = Path(args.capsule_dir).resolve()
    manifest_p = cap_dir / "manifest.json" if cap_dir.is_dir() else cap_dir
    if not manifest_p.is_file():
        print(f"error: manifest not found at: {manifest_p}", file=sys.stderr)
        return 2

    try:
        manifest_data = json.loads(manifest_p.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"error: cannot parse manifest JSON: {exc}", file=sys.stderr)
        return 2

    history = manifest_data.get("history")
    if not isinstance(history, list):
        print("error: manifest history is missing or not a list", file=sys.stderr)
        return 1

    secret = resolve_audit_secret(args.secret)
    status = verify_chain(history, secret=secret)

    print(f"Capsule: {manifest_data.get('capsule_id', cap_dir.name)}")
    print(f"History Entries: {len(history)} (chained: {status.chained}, unsealed: {status.unsealed})")
    print(f"Head: {head_of(history) or 'none'}")
    print(f"Keyed: {status.keyed} | Sealed: {status.sealed}")
    print(f"Integrity: {'INTACT (OK)' if status.ok else 'BROKEN / TAMPERED'}")
    print(f"Detail: {status.detail}")

    return 0 if status.ok else 1


def cmd_ledger(args: argparse.Namespace) -> int:
    spool_dir = Path(args.spool_dir).resolve() if args.spool_dir else _default_spool()
    secret = resolve_audit_secret(args.secret)

    try:
        entries = read_ledger(spool_dir)
    except Exception as exc:
        print(f"error reading ledger: {exc}", file=sys.stderr)
        return 1

    status = verify_ledger(entries, secret=secret)
    print(f"Ledger Entries: {len(entries)}")
    print(f"Integrity: {'INTACT (OK)' if status.ok else 'BROKEN'}")
    print(f"Detail: {status.detail}")

    return 0 if status.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_audit.py",
        description="Verify cryptographic audit chains and spool ledger.",
    )
    parser.add_argument(
        "--secret",
        metavar="SECRET",
        help=f"Explicit audit secret (defaults to env ${AUDIT_SECRET_ENV} or ${CAPABILITY_SECRET_ENV})",
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # spool
    p_spool = subparsers.add_parser("spool", help="Verify entire spool directory and ledger")
    p_spool.add_argument("spool_dir", nargs="?", help="Path to capsules spool directory")
    p_spool.set_defaults(handler=cmd_spool)

    # capsule
    p_cap = subparsers.add_parser("capsule", help="Verify single capsule history chain")
    p_cap.add_argument("capsule_dir", help="Path to capsule directory or manifest.json")
    p_cap.set_defaults(handler=cmd_capsule)

    # ledger
    p_led = subparsers.add_parser("ledger", help="Verify spool ledger alone")
    p_led.add_argument("spool_dir", nargs="?", help="Path to capsules spool directory")
    p_led.set_defaults(handler=cmd_ledger)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
