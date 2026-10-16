#!/usr/bin/env python3
"""watcher.py — The Watcher (CapsuleWatcher): read-only attention reporting.

Scans ``capsules_spool/`` for Deliverable Capsules and aggregates everything a
human (or the future dashboard) must act on into one ``attention_required.json``
in the project root.

Contract:
- **read-only & lock-free**: never acquires ``.lock``, never writes inside the
  spool. Safe to run while pulses are mid-flight: writers persist manifests
  with ``os.replace`` (atomic rename), so the Watcher only ever observes a
  complete snapshot — the old one or the new one, never a torn one.
- **defensive**: unreadable files, corrupted JSON, oversized files, non-object
  roots, missing critical keys and unknown states are *flagged*, never raised:
  one broken capsule must not kill a report pass.
- **attention model**:
      requires_authorization -> state == "pending_approval"
      system_halt            -> state == "failed"
      revision_cap           -> state == "needs_revision" AND its
                                revision_count is over the configured cap
                                (the loop breaker said "human from here")
      quarantined            -> unreadable / corrupt / missing critical keys
                                 / unusable state value
      healthy                -> any other known state (counted, not listed)
  Entries that stopped on the revision cap (in ``system_halt`` or
  ``revision_cap``) carry ``revision_count`` and an ``attention_reason`` so
  the dashboard shows *why* a human is needed, not just that one is.
- **atomic output**: the report is written temp-file + ``os.replace``, so the
  dashboard never reads a half-written JSON either.

Standard library only: ``argparse``, ``json``, ``os``, ``pathlib``,
``datetime``, ``typing``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = _REPO_ROOT
DEFAULT_SPOOL = _REPO_ROOT / "data" / "capsules" / "spool"
if not DEFAULT_SPOOL.exists() and (_REPO_ROOT / "capsules" / "spool").exists():
    DEFAULT_SPOOL = _REPO_ROOT / "capsules" / "spool"
DEFAULT_OUTPUT = _REPO_ROOT / "attention_required.json"

MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 1 * 1024 * 1024  # parity with capsule_engine.manifest
CRITICAL_KEYS = ("capsule_id", "state")

AUTHORIZATION_STATE = "pending_approval"
# States that need a human decision before anything moves again: the approval
# gate, and the TTL auto-freeze (unfreeze -> pending_approval, or -> draft).
AUTHORIZATION_STATES = frozenset({AUTHORIZATION_STATE, "stalled"})
HALT_STATE = "failed"
REWORK_STATE = "needs_revision"

#: Mirror of ``capsule_engine.manifest.DEFAULT_REVISION_CAP`` / the shipped
#: ``spend.max_revisions_per_capsule`` — kept local on purpose (a reporter
#: must not depend on the engine package) and asserted equal by the tests.
DEFAULT_REVISION_CAP = 3

# Mirror of the capsule state contract (capsule_engine.manifest.State) plus
# the approval-gate state this report watches for. Kept local on purpose: a
# reporter must not depend on the engine package, and an *unknown* state is
# flagged for a human instead of silently counted as healthy (fail visible).
KNOWN_STATES = frozenset(
    {
        "draft",
        "in_progress",
        "in_review",
        "pending_design",
        "pending_qa",
        "needs_revision",
        "approved",
        "published",
        "failed",
        "archived",
        "pending_approval",
        "stalled",
    }
)

ATTENTION_LISTS = ("requires_authorization", "system_halt", "quarantined", "revision_cap")

# Closed vocabulary of quarantine reasons, for dashboard switches:
QUARANTINE_REASONS = (
    "unreadable",     # OSError (permissions, vanished mid-scan)
    "oversized",      # larger than MAX_MANIFEST_BYTES
    "invalid_json",   # not UTF-8, or JSONDecodeError (line/col in detail)
    "not_an_object",  # valid JSON whose root is not an object
    "missing_keys",   # critical keys absent or empty
    "invalid_state",  # state unusable or not in KNOWN_STATES
)


class _Flagged(Exception):
    """Internal control-flow signal: a manifest that must be quarantined."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #

def find_manifests(spool: Path) -> list[Path]:
    """Return every ``manifest.json`` under ``spool``, sorted, or ``[]``.

    Recursive so capsules may be nested (e.g. per client). A partially
    unreadable tree still yields whatever was reachable — a short report
    beats no report.
    """
    if not spool.is_dir():
        return []
    manifests: list[Path] = []
    try:
        for path in spool.rglob(MANIFEST_NAME):
            try:
                if path.is_file():
                    manifests.append(path)
            except OSError:
                continue  # vanished or unreadable entry mid-scan
    except OSError:
        pass  # walk aborted (permissions) — report the partial sweep
    manifests.sort()
    return manifests


def _display_path(path: Path) -> str:
    """Path relative to the project root when possible (portable for the UI)."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except (OSError, ValueError):
        return str(path)


# --------------------------------------------------------------------------- #
# read + categorize (never raises for a broken capsule)
# --------------------------------------------------------------------------- #

def _flagged(path: str, reason: str, detail: str) -> dict[str, Any]:
    if reason not in QUARANTINE_REASONS:  # guard the closed vocabulary
        reason = "invalid_json"
    return {"path": path, "reason": reason, "detail": detail}


def _attention_entry(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "capsule_id": data["capsule_id"],
        "state": data["state"],
        "path": _display_path(path),
        "updated_at": data.get("updated_at")
        if isinstance(data.get("updated_at"), str)
        else None,
    }
    revisions = _revision_count(data)
    if revisions is not None:
        entry["revision_count"] = revisions
    if data["state"] == HALT_STATE:
        attempt = data.get("attempt")
        entry["attempt"] = (
            attempt if isinstance(attempt, int) and not isinstance(attempt, bool)
            else None
        )
        cap = _revision_cap()
        if revisions is not None and revisions > cap:
            entry["attention_reason"] = _cap_reason(revisions, cap)
    return entry


def _revision_count(data: dict[str, Any]) -> int | None:
    """``revision_count`` when usable, ``None`` when absent (old capsule)."""
    raw = data.get("revision_count")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def _cap_reason(revisions: int, cap: int) -> str:
    return (
        f"revision cap exceeded ({revisions} > {cap}) — human decision required "
        "(no worker dispatch)"
    )


def _revision_cap() -> int:
    """``spend.max_revisions_per_capsule`` from config/engine.json, default 3.

    Stdlib-only and fail-safe: an unreadable or malformed config keeps the
    default — a broken file must never lift the cap this report enforces.
    """
    try:
        raw = json.loads((ROOT / "config" / "engine.json").read_text(encoding="utf-8"))
        cap = raw.get("spend", {}).get("max_revisions_per_capsule")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_REVISION_CAP
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        return DEFAULT_REVISION_CAP
    return cap


def inspect_manifest(path: Path) -> tuple[str, dict[str, Any]]:
    """Classify one manifest as ``(category, entry)``.

    Categories: ``requires_authorization`` | ``system_halt`` | ``quarantined``
    | ``healthy``. This is the Watcher's core promise: a broken file becomes a
    ``quarantined`` entry, never an exception.
    """
    display = _display_path(path)
    try:
        size = path.stat().st_size
        if size > MAX_MANIFEST_BYTES:
            raise _Flagged("oversized", f"{size} bytes exceeds {MAX_MANIFEST_BYTES}")
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return "quarantined", _flagged(display, "invalid_json", f"not valid UTF-8: {exc}")
    except _Flagged as exc:
        return "quarantined", _flagged(display, exc.reason, exc.detail)
    except OSError as exc:
        return "quarantined", _flagged(
            display, "unreadable", f"{type(exc).__name__}: {exc}"
        )

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        return "quarantined", _flagged(
            display,
            "invalid_json",
            f"line {exc.lineno} column {exc.colno}: {exc.msg}",
        )

    if not isinstance(data, dict):
        return "quarantined", _flagged(
            display,
            "not_an_object",
            f"root is {type(data).__name__}, expected object",
        )

    missing = [key for key in CRITICAL_KEYS if key not in data]
    if missing:
        return "quarantined", _flagged(
            display, "missing_keys", f"missing critical keys: {', '.join(missing)}"
        )
    capsule_id = data["capsule_id"]
    if not isinstance(capsule_id, str) or not capsule_id.strip():
        return "quarantined", _flagged(
            display, "missing_keys",
            "critical field 'capsule_id' is not a non-empty string",
        )

    state = data["state"]
    if not isinstance(state, str) or not state:
        return "quarantined", _flagged(
            display, "invalid_state", f"'state' must be a non-empty string, got {state!r}"
        )
    if state not in KNOWN_STATES:
        return "quarantined", _flagged(display, "invalid_state", f"unknown state {state!r}")

    if state in AUTHORIZATION_STATES:
        return "requires_authorization", _attention_entry(path, data)
    if state == HALT_STATE:
        return "system_halt", _attention_entry(path, data)
    if state == REWORK_STATE:
        revisions = _revision_count(data)
        cap = _revision_cap()
        # transition() escalates the crossing rejection to ``failed``; this
        # catches the state a lowered cap (or a hand edit) left behind.
        if revisions is not None and revisions > cap:
            entry = _attention_entry(path, data)
            entry["attention_reason"] = _cap_reason(revisions, cap)
            return "revision_cap", entry
    return "healthy", {"path": display}


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_report(spool: Path, manifests: list[Path]) -> dict[str, Any]:
    """Inspect every manifest and assemble the aggregated report payload."""
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in ATTENTION_LISTS}
    healthy = 0
    for manifest_path in manifests:
        category, entry = inspect_manifest(manifest_path)
        if category == "healthy":
            healthy += 1
        else:
            buckets[category].append(entry)
    counts = {name: len(buckets[name]) for name in ATTENTION_LISTS}
    counts["healthy"] = healthy
    return {
        "generated_at": _now_iso(),
        "format_version": 1,
        "spool": str(spool),
        "spool_exists": spool.is_dir(),
        "scanned": len(manifests),
        "counts": counts,
        "requires_authorization": buckets["requires_authorization"],
        "system_halt": buckets["system_halt"],
        "quarantined": buckets["quarantined"],
        "revision_cap": buckets["revision_cap"],
    }


def write_report(output: Path, report: dict[str, Any]) -> None:
    """Atomically persist the report: temp file + fsync + ``os.replace``."""
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, output)
    except OSError:
        tmp.unlink(missing_ok=True)  # never leave litter behind
        raise


def run(spool: Path, output: Path) -> dict[str, Any]:
    """One full Watcher pass: scan -> flag -> write ``attention_required.json``.

    Raises only when the *report itself* cannot be written (an I/O problem at
    the destination) — capsule problems are data, not exceptions.
    """
    manifests = find_manifests(spool)
    report = build_report(spool, manifests)
    write_report(output, report)
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="watcher.py",
        description="Read-only capsule attention report (feeds the dashboard).",
    )
    parser.add_argument(
        "--spool", type=Path, default=DEFAULT_SPOOL,
        help=f"capsule spool directory (default: {DEFAULT_SPOOL})",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help=f"report destination (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args(argv)

    try:
        report = run(args.spool, args.output)
    except OSError as exc:
        print(
            json.dumps(
                {"watcher": "error", "output": str(args.output),
                 "detail": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "watcher": "ok",
                "output": str(args.output),
                "scanned": report["scanned"],
                "counts": report["counts"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
