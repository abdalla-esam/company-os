#!/usr/bin/env python3
"""Hash-chained audit log and spool-level ledger — brick 4, kernel layer.

This module provides cryptographic tamper-evidence for capsule history records
and spool-wide capsule lifecycles.

Core Guarantees:
- **Hash-Chained History**: Each history record carries the cryptographic MAC
  (keyed HMAC-SHA256 or unkeyed SHA-256) of its predecessor (``prev``) and its
  own sequential position (``seq``). Any modification, deletion, reordering,
  or insertion breaks the chain.
- **Fail-Closed Verification**: A broken chain, a downgraded algorithm, an
  unverifiable keyed chain, or a missing secret fails closed.
- **Explicit Legacy Sealing**: Pre-chain history records are explicitly sealed
  with a Genesis marker that counts unverified entries without pretending to
  vouch for them.
- **Spool-Level Head Ledger**: Records the latest cryptographic head of each
  capsule into an append-only, chained ledger (``.audit_ledger.jsonl``) to
  detect deleted capsules (tombstone attacks) and state rollbacks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from shared_kernel.atomic_io import append_jsonl_line, sidecar_lock

# --------------------------------------------------------------------------- #
# constants & algorithms
# --------------------------------------------------------------------------- #

GENESIS: str = "0" * 64
ALGO_KEYED: str = "hmac-sha256"
ALGO_UNKEYED: str = "sha256"

AUDIT_SECRET_ENV: str = "AGENCY_AUDIT_SECRET"
CAPABILITY_SECRET_ENV: str = "AGENCY_CAPABILITY_SECRET"
LEDGER_FILENAME: str = ".audit_ledger.jsonl"

#: How much of the ledger tail one append reads (instead of the whole file).
#: ~256 KiB covers hundreds of recent entries — enough to recover the chain
#: head (prev mac, next seq) and the most recent entry per capsule.
LEDGER_TAIL_BYTES: int = 256 * 1024


# --------------------------------------------------------------------------- #
# data structures
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ChainStatus:
    """Outcome of verifying a capsule history chain."""

    ok: bool
    sealed: bool
    keyed: bool
    chained: int
    unsealed: int
    broken_at: int | None
    detail: str


@dataclass(frozen=True)
class Finding:
    """A discrepancy found during spool verification."""

    kind: str
    severity: str  # "critical", "warning", "info"
    capsule_id: str | None
    detail: str


@dataclass(frozen=True)
class SpoolReport:
    """Aggregate outcome of verifying an entire spool directory and ledger."""

    ok: bool
    findings: list[Finding]
    checked_capsules: int = 0


# --------------------------------------------------------------------------- #
# secret & serialization helpers
# --------------------------------------------------------------------------- #

def resolve_audit_secret(secret_str: str | bytes | None = None) -> bytes | None:
    """Resolve an audit secret key defensively.

    Precedence:
    1. Explicit non-empty argument ``secret_str``
    2. ``AGENCY_AUDIT_SECRET`` environment variable
    3. ``AGENCY_CAPABILITY_SECRET`` environment variable
    """
    if secret_str is not None:
        if isinstance(secret_str, bytes):
            return secret_str if secret_str else None
        if isinstance(secret_str, str):
            stripped = secret_str.strip()
            return stripped.encode("utf-8") if stripped else None

    from_env = os.environ.get(AUDIT_SECRET_ENV) or os.environ.get(CAPABILITY_SECRET_ENV)
    if from_env:
        stripped = from_env.strip()
        if stripped:
            return stripped.encode("utf-8")
    return None


def canonical_bytes(payload: Mapping[str, Any], algo: str = ALGO_KEYED) -> bytes:
    """Serialize a mapping into canonical, deterministic UTF-8 JSON bytes."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _unkeyed_mac(data: bytes) -> str:
    """Compute an unkeyed SHA-256 hex digest."""
    return hashlib.sha256(data).hexdigest()


def _keyed_mac(secret: bytes, data: bytes) -> str:
    """Compute an HMAC-SHA256 hex digest with the secret."""
    return hmac.new(secret, data, hashlib.sha256).hexdigest()


def _now_iso() -> str:
    """Return an aware UTC timestamp in ISO-8601."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# history chain operations
# --------------------------------------------------------------------------- #

def head_of(history: Sequence[Mapping[str, Any]] | None) -> str | None:
    """Return the cryptographic ``mac`` of the newest history entry, or ``None``."""
    if not history:
        return None
    latest = history[-1]
    if isinstance(latest, dict):
        mac = latest.get("mac")
        if isinstance(mac, str) and mac:
            return mac
    return None


def _is_chained_entry(entry: Mapping[str, Any]) -> bool:
    """Return True if ``entry`` carries audit chain fields."""
    return (
        isinstance(entry, dict)
        and "seq" in entry
        and "prev" in entry
        and "mac" in entry
        and "mac_algo" in entry
    )


def append_chained(
    history: list[dict[str, Any]],
    entry: Mapping[str, Any],
    *,
    secret: bytes | None = None,
) -> list[dict[str, Any]]:
    """Append a chained copy of ``entry`` to ``history``, sealing legacy entries if needed.

    Does not mutate ``entry``. Mutates and returns ``history``.
    """
    resolved_secret = resolve_audit_secret(secret)
    algo = ALGO_KEYED if resolved_secret is not None else ALGO_UNKEYED

    # If entry is already the last item in history without a mac, pop it so we can chain it
    if history and history[-1] is entry and not _is_chained_entry(history[-1]):
        history.pop()

    # Detect legacy (unsealed / unchained) records
    has_chained = any(_is_chained_entry(e) for e in history)
    if history and not has_chained:
        unsealed_count = len(history)
        marker_payload: dict[str, Any] = {
            "at": entry.get("at") if isinstance(entry.get("at"), str) else _now_iso(),
            "by": "system",
            "from_state": None,
            "prev": GENESIS,
            "seq": 0,
            "to_state": None,
            "why": f"Sealed {unsealed_count} unverified legacy history records into audit chain",
        }
        marker_bytes = canonical_bytes(marker_payload, algo)
        marker_mac = (
            _keyed_mac(resolved_secret, marker_bytes)
            if resolved_secret is not None
            else _unkeyed_mac(marker_bytes)
        )
        marker: dict[str, Any] = dict(marker_payload)
        marker["mac_algo"] = algo
        marker["mac"] = marker_mac
        history.append(marker)

    # Determine sequence number and predecessor
    if not history:
        seq = 0
        prev = GENESIS
    else:
        last_entry = history[-1]
        if _is_chained_entry(last_entry):
            seq = int(last_entry["seq"]) + 1
            prev = str(last_entry["mac"])
        else:
            seq = 0
            prev = GENESIS

    # Prepare new chained record
    new_record = dict(entry)
    new_record["seq"] = seq
    new_record["prev"] = prev

    payload = {k: v for k, v in new_record.items() if k not in ("mac", "mac_algo")}
    payload_bytes = canonical_bytes(payload, algo)
    computed_mac = (
        _keyed_mac(resolved_secret, payload_bytes)
        if resolved_secret is not None
        else _unkeyed_mac(payload_bytes)
    )

    new_record["mac_algo"] = algo
    new_record["mac"] = computed_mac
    history.append(new_record)
    return history


def verify_chain(
    history: Sequence[Mapping[str, Any]],
    *,
    secret: bytes | None = None,
    expected_head: str | None = None,
) -> ChainStatus:
    """Verify cryptographic integrity of a capsule history chain."""
    resolved_secret = resolve_audit_secret(secret)

    if not history:
        return ChainStatus(
            ok=True,
            sealed=True,
            keyed=resolved_secret is not None,
            chained=0,
            unsealed=0,
            broken_at=None,
            detail="empty history",
        )

    # Find where the chain begins
    chain_start = None
    for idx, entry in enumerate(history):
        if _is_chained_entry(entry):
            chain_start = idx
            break

    if chain_start is None:
        return ChainStatus(
            ok=True,
            sealed=False,
            keyed=False,
            chained=0,
            unsealed=len(history),
            broken_at=None,
            detail=f"{len(history)} unsealed legacy history records",
        )

    unsealed_count = chain_start
    chained_entries = history[chain_start:]

    # Check if any keyed records exist without having a secret supplied
    has_keyed = any(e.get("mac_algo") == ALGO_KEYED for e in chained_entries)
    if has_keyed and resolved_secret is None:
        return ChainStatus(
            ok=False,
            sealed=True,
            keyed=False,
            chained=len(chained_entries),
            unsealed=unsealed_count,
            broken_at=None,
            detail="keyed records cannot be verified without an audit secret (fail closed)",
        )

    expected_prev = GENESIS
    expected_seq = 0
    chain_algo = chained_entries[0].get("mac_algo")

    for idx, entry in enumerate(chained_entries):
        actual_index = chain_start + idx

        if not _is_chained_entry(entry):
            return ChainStatus(
                ok=False,
                sealed=True,
                keyed=has_keyed,
                chained=idx,
                unsealed=unsealed_count,
                broken_at=actual_index,
                detail=f"entry at index {actual_index} is unchained (hand-edited or corrupt)",
            )

        entry_seq = entry.get("seq")
        entry_prev = entry.get("prev")
        entry_algo = entry.get("mac_algo")
        entry_mac = entry.get("mac")

        # Check for downgrade attack or mixed algorithm in same chain
        if entry_algo != chain_algo:
            return ChainStatus(
                ok=False,
                sealed=True,
                keyed=has_keyed,
                chained=idx,
                unsealed=unsealed_count,
                broken_at=actual_index,
                detail=f"algorithm downgrade or mismatch at index {actual_index}: expected {chain_algo}, got {entry_algo}",
            )

        if entry_seq != expected_seq:
            return ChainStatus(
                ok=False,
                sealed=True,
                keyed=has_keyed,
                chained=idx,
                unsealed=unsealed_count,
                broken_at=actual_index,
                detail=f"sequence gap at index {actual_index}: expected seq {expected_seq}, got {entry_seq}",
            )

        if not isinstance(entry_prev, str) or not hmac.compare_digest(entry_prev, expected_prev):
            return ChainStatus(
                ok=False,
                sealed=True,
                keyed=has_keyed,
                chained=idx,
                unsealed=unsealed_count,
                broken_at=actual_index,
                detail=f"prev link mismatch at index {actual_index}",
            )

        payload = {k: v for k, v in entry.items() if k not in ("mac", "mac_algo")}
        payload_bytes = canonical_bytes(payload, str(entry_algo))

        if entry_algo == ALGO_KEYED:
            if resolved_secret is None:
                computed_mac = ""
            else:
                computed_mac = _keyed_mac(resolved_secret, payload_bytes)
        elif entry_algo == ALGO_UNKEYED:
            computed_mac = _unkeyed_mac(payload_bytes)
        else:
            return ChainStatus(
                ok=False,
                sealed=True,
                keyed=has_keyed,
                chained=idx,
                unsealed=unsealed_count,
                broken_at=actual_index,
                detail=f"unknown mac_algo {entry_algo!r} at index {actual_index}",
            )

        if not isinstance(entry_mac, str) or not hmac.compare_digest(entry_mac, computed_mac):
            return ChainStatus(
                ok=False,
                sealed=True,
                keyed=has_keyed,
                chained=idx,
                unsealed=unsealed_count,
                broken_at=actual_index,
                detail=f"mac verification failed at index {actual_index}",
            )

        expected_prev = entry_mac
        expected_seq += 1

    # Check expected head if provided
    current_head = chained_entries[-1].get("mac")
    if expected_head is not None:
        if not isinstance(current_head, str) or not hmac.compare_digest(current_head, expected_head):
            return ChainStatus(
                ok=False,
                sealed=True,
                keyed=has_keyed,
                chained=len(chained_entries),
                unsealed=unsealed_count,
                broken_at=None,
                detail=f"head mismatch: expected {expected_head}, got {current_head}",
            )

    detail_msg = f"{len(chained_entries)} chained records verified"
    if unsealed_count > 0:
        detail_msg += f" ({unsealed_count} pre-chain records unsealed)"

    return ChainStatus(
        ok=True,
        sealed=True,
        keyed=has_keyed and resolved_secret is not None,
        chained=len(chained_entries),
        unsealed=unsealed_count,
        broken_at=None,
        detail=detail_msg,
    )


# --------------------------------------------------------------------------- #
# spool ledger operations
# --------------------------------------------------------------------------- #

def ledger_path(spool_dir: str | os.PathLike[str]) -> Path:
    """Return the path to the spool-level ledger file."""
    return Path(spool_dir).resolve() / LEDGER_FILENAME


def read_ledger(spool_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Read the spool-level audit ledger entries."""
    path = ledger_path(spool_dir)
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line_str = line.strip()
                if not line_str:
                    continue
                entries.append(json.loads(line_str))
    except Exception as exc:
        raise ValueError(f"ledger file unreadable or corrupt: {exc}") from exc
    return entries


def _read_ledger_tail(
    path: Path, *, max_bytes: int = LEDGER_TAIL_BYTES
) -> list[dict[str, Any]]:
    """Parse complete JSONL lines from the tail of the ledger.

    An append only needs the *recent* chain head — the last mac, the last
    seq, the most recent entry per capsule — so it reads the tail instead of
    the whole file (the full read made every append O(n²) over the ledger's
    life). A torn line inside the window (a writer killed mid-write) is
    reported as corruption, exactly like the full-file reader does.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    start = max(0, size - max_bytes)
    try:
        with path.open("rb") as handle:
            if start:
                handle.seek(start)
                handle.readline()  # drop the partial first line at the window edge
            data = handle.read()
    except OSError as exc:
        raise ValueError(f"ledger tail unreadable: {exc}") from exc
    entries: list[dict[str, Any]] = []
    for raw_line in data.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"ledger tail corrupt: {exc}") from exc
    return entries


def append_ledger_head(
    spool_dir: str | os.PathLike[str],
    *,
    capsule_id: str,
    head: str | None,
    secret: bytes | None = None,
) -> dict[str, Any] | None:
    """Append a capsule head to the spool ledger (idempotent).

    The prev/seq read and the append run inside ONE sidecar-lock critical
    section (:class:`shared_kernel.atomic_io`), so concurrent pulses can
    never compute the same seq, chain against different prevs, or lose each
    other's lines — previously the read and the append were unlocked and two
    simultaneous appends produced a broken chain (duplicate seq, dangling
    prev) or a silently lost record.

    Only the ledger tail (:data:`LEDGER_TAIL_BYTES`) is read. The
    idempotence check therefore inspects the most recent window of entries —
    hundreds — instead of the whole file.
    """
    resolved_secret = resolve_audit_secret(secret)
    algo = ALGO_KEYED if resolved_secret is not None else ALGO_UNKEYED

    spool = Path(spool_dir).resolve()
    spool.mkdir(parents=True, exist_ok=True)
    path = ledger_path(spool)

    with sidecar_lock(path, purpose="audit:append_ledger_head"):
        entries = _read_ledger_tail(path)

        # Idempotence: do not re-record the identical head for the same capsule
        for entry in reversed(entries):
            if entry.get("capsule_id") == capsule_id:
                if entry.get("head") == head:
                    return None
                break

        if entries:
            last = entries[-1]
            prev = last["mac"] if "mac" in last else GENESIS
            last_seq = last.get("seq")
            if not isinstance(last_seq, int) or isinstance(last_seq, bool):
                raise ValueError("ledger tail corrupt: last entry has no seq")
            seq = last_seq + 1  # appends only — the tail's last seq is the count
        else:
            prev = GENESIS
            seq = 0

        record: dict[str, Any] = {
            "at": _now_iso(),
            "capsule_id": str(capsule_id),
            "head": head,
            "prev": prev,
            "seq": seq,
        }

        payload = {k: v for k, v in record.items() if k not in ("mac", "mac_algo")}
        payload_bytes = canonical_bytes(payload, algo)
        computed_mac = (
            _keyed_mac(resolved_secret, payload_bytes)
            if resolved_secret is not None
            else _unkeyed_mac(payload_bytes)
        )

        record["mac_algo"] = algo
        record["mac"] = computed_mac

        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        append_jsonl_line(path, line)  # O_APPEND + flock + fsync under the sidecar lock

    return record


def verify_ledger(
    entries: Sequence[Mapping[str, Any]],
    *,
    secret: bytes | None = None,
) -> ChainStatus:
    """Verify cryptographic integrity of the spool ledger entries."""
    resolved_secret = resolve_audit_secret(secret)
    if not entries:
        return ChainStatus(
            ok=True,
            sealed=True,
            keyed=resolved_secret is not None,
            chained=0,
            unsealed=0,
            broken_at=None,
            detail="empty ledger",
        )

    has_keyed = any(e.get("mac_algo") == ALGO_KEYED for e in entries)
    if has_keyed and resolved_secret is None:
        return ChainStatus(
            ok=False,
            sealed=True,
            keyed=False,
            chained=len(entries),
            unsealed=0,
            broken_at=None,
            detail="keyed ledger records cannot be verified without secret",
        )

    expected_prev = GENESIS
    for idx, entry in enumerate(entries):
        if not _is_chained_entry(entry):
            return ChainStatus(
                ok=False,
                sealed=False,
                keyed=has_keyed,
                chained=idx,
                unsealed=0,
                broken_at=idx,
                detail=f"ledger entry at index {idx} is unchained",
            )

        if entry.get("seq") != idx:
            return ChainStatus(
                ok=False,
                sealed=True,
                keyed=has_keyed,
                chained=idx,
                unsealed=0,
                broken_at=idx,
                detail=f"ledger sequence gap at index {idx}",
            )

        if not isinstance(entry.get("prev"), str) or not hmac.compare_digest(entry["prev"], expected_prev):
            return ChainStatus(
                ok=False,
                sealed=True,
                keyed=has_keyed,
                chained=idx,
                unsealed=0,
                broken_at=idx,
                detail=f"ledger prev link mismatch at index {idx}",
            )

        algo = entry.get("mac_algo")
        payload = {k: v for k, v in entry.items() if k not in ("mac", "mac_algo")}
        payload_bytes = canonical_bytes(payload, str(algo))

        if algo == ALGO_KEYED:
            computed = _keyed_mac(resolved_secret, payload_bytes) if resolved_secret else ""
        elif algo == ALGO_UNKEYED:
            computed = _unkeyed_mac(payload_bytes)
        else:
            return ChainStatus(
                ok=False,
                sealed=True,
                keyed=has_keyed,
                chained=idx,
                unsealed=0,
                broken_at=idx,
                detail=f"unknown ledger mac_algo {algo!r}",
            )

        entry_mac = entry.get("mac")
        if not isinstance(entry_mac, str) or not hmac.compare_digest(entry_mac, computed):
            return ChainStatus(
                ok=False,
                sealed=True,
                keyed=has_keyed,
                chained=idx,
                unsealed=0,
                broken_at=idx,
                detail=f"ledger mac verification failed at index {idx}",
            )

        expected_prev = entry_mac

    return ChainStatus(
        ok=True,
        sealed=True,
        keyed=has_keyed and resolved_secret is not None,
        chained=len(entries),
        unsealed=0,
        broken_at=None,
        detail=f"{len(entries)} ledger entries verified",
    )


def verify_spool(
    spool_dir: str | os.PathLike[str],
    *,
    secret: bytes | None = None,
) -> SpoolReport:
    """Verify an entire spool directory: ledger integrity + capsule chains + rollback detection."""
    spool = Path(spool_dir).resolve()
    resolved_secret = resolve_audit_secret(secret)
    findings: list[Finding] = []

    ledger_p = ledger_path(spool)
    ledger_entries: list[dict[str, Any]] = []

    if ledger_p.is_file():
        try:
            ledger_entries = read_ledger(spool)
        except Exception as exc:
            findings.append(
                Finding(
                    kind="ledger_broken",
                    severity="critical",
                    capsule_id=None,
                    detail=f"ledger file is corrupt: {exc}",
                )
            )
        else:
            has_keyed = any(e.get("mac_algo") == ALGO_KEYED for e in ledger_entries)
            if has_keyed and resolved_secret is None:
                findings.append(
                    Finding(
                        kind="ledger_unverifiable",
                        severity="critical",
                        capsule_id=None,
                        detail="ledger contains keyed entries but no secret was supplied",
                    )
                )
            else:
                ledger_status = verify_ledger(ledger_entries, secret=resolved_secret)
                if not ledger_status.ok:
                    findings.append(
                        Finding(
                            kind="ledger_broken",
                            severity="critical",
                            capsule_id=None,
                            detail=ledger_status.detail,
                        )
                    )

    # Collect latest head for each capsule from ledger
    latest_ledger_heads: dict[str, str | None] = {}
    for entry in ledger_entries:
        cid = entry.get("capsule_id")
        if isinstance(cid, str):
            latest_ledger_heads[cid] = entry.get("head")

    # Check for missing capsules that the ledger knows about
    for cid, expected_head in latest_ledger_heads.items():
        cap_dir = spool / cid
        man_path = cap_dir / "manifest.json"
        if not cap_dir.is_dir() or not man_path.is_file():
            findings.append(
                Finding(
                    kind="capsule_missing",
                    severity="critical",
                    capsule_id=cid,
                    detail=f"capsule {cid} is recorded in ledger but missing from disk",
                )
            )

    # Check all existing capsules on disk
    checked = 0
    if spool.is_dir():
        for item in sorted(spool.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue
            man_path = item / "manifest.json"
            if not man_path.is_file():
                continue

            checked += 1
            cid = item.name
            try:
                manifest_data = json.loads(man_path.read_text(encoding="utf-8"))
            except Exception as exc:
                findings.append(
                    Finding(
                        kind="manifest_corrupt",
                        severity="critical",
                        capsule_id=cid,
                        detail=f"manifest unreadable: {exc}",
                    )
                )
                continue

            history = manifest_data.get("history") if isinstance(manifest_data, dict) else None
            if not isinstance(history, list):
                findings.append(
                    Finding(
                        kind="manifest_corrupt",
                        severity="critical",
                        capsule_id=cid,
                        detail="manifest history is not a list",
                    )
                )
                continue

            chain_status = verify_chain(history, secret=resolved_secret)
            if not chain_status.ok:
                findings.append(
                    Finding(
                        kind="chain_broken",
                        severity="critical",
                        capsule_id=cid,
                        detail=chain_status.detail,
                    )
                )
                continue

            current_head = head_of(history)
            if cid in latest_ledger_heads:
                ledger_head = latest_ledger_heads[cid]
                if ledger_head is not None:
                    all_macs = [e.get("mac") for e in history if isinstance(e, dict) and e.get("mac")]
                    if current_head != ledger_head:
                        if ledger_head in all_macs:
                            findings.append(
                                Finding(
                                    kind="capsule_ahead",
                                    severity="warning",
                                    capsule_id=cid,
                                    detail=f"capsule {cid} has advanced ahead of ledger head",
                                )
                            )
                        else:
                            findings.append(
                                Finding(
                                    kind="head_mismatch",
                                    severity="critical",
                                    capsule_id=cid,
                                    detail=f"capsule {cid} head does not match ledger record",
                                )
                            )

    is_ok = not any(f.severity == "critical" for f in findings)
    return SpoolReport(ok=is_ok, findings=findings, checked_capsules=checked)
