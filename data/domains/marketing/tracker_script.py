#!/usr/bin/env python3
"""tracker_script.py — persists accepted decisions as tickets (Pydantic fail-closed).

MVP state layer. Every persisted ticket is a strict Pydantic model
(shared_kernel.state_schema.Ticket). A ticket that cannot be decoded or
validated is a broken state: it is left untouched and reported, never
silently repaired. The disk stays the witness.

Bridge (one-way): needs_gate -> approval queue (governance), then a human
decides. settle pulls only this sector's approved gates, keyed by the
routing marker source.sector -> ticket of that sector.

Path bootstrap: the script may run from a copied sandbox or from the
production vault. Resolve `shared_kernel` relative to this file so the
import works either way.
"""

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

try:
    from pydantic import ValidationError
except ImportError:
    ValidationError = Exception

try:
    from tenacity import (
        retry,
        stop_after_attempt,
        wait_exponential,
    )
except ImportError:
    def retry(*args, **kwargs):
        return lambda f: f

    def stop_after_attempt(*args, **kwargs):
        return None

    def wait_exponential(*args, **kwargs):
        return None

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "engine"))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

try:
    from shared_kernel.state_schema import (
        Decision,
        GateEntry,
        GateSource,
        SettleOutput,
        Ticket,
        VaultError,
    )
    from shared_kernel.state_schema import parse_utc as _parse_utc
except ImportError:  # pragma: no cover - fallback if the layout differs
    from state_schema import (
        Decision,
        GateEntry,
        GateSource,
        SettleOutput,
        Ticket,
        VaultError,
    )
    from state_schema import parse_utc as _parse_utc

from agent_script import decide  # single source of truth - no re-classification

# Crash-safe ticket writes come from the shared kernel's one helper. This
# script runs as a subprocess of the API, so bootstrap the engine directory
# from the known repository layout (domain scripts live three levels below
# the root).
_ENGINE_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "engine"))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)
try:
    from shared_kernel.atomic_io import update_json
except ImportError:  # pragma: no cover - stripped environments
    # atomic_io is pure standard library: load it by path so the write-safety
    # guarantee never depends on an installed wheel.
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "shared_kernel_atomic_io",
        os.path.join(_ENGINE_ROOT, "shared_kernel", "atomic_io.py"),
    )
    if _spec is None or _spec.loader is None:
        raise
    _atomic_io = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_atomic_io)
    update_json = _atomic_io.update_json

TICKETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tickets")
ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "archive")
GATE_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "../../governance/approval_gates"))
SECTOR = "marketing"


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    """Read + decode a JSON file. None on any failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path: str, obj: Dict[str, Any]) -> None:
    """Lock-serialized write: the file on disk must remain a ticket.

    Only the keys this call sets are overwritten: a settle that lands on a
    file the API's context PATCH or the dispatcher just rewrote keeps their
    fields, and a torn half-written ticket is impossible.
    """
    update_json(
        path,
        lambda state: {**state, **obj} if isinstance(state, dict) else obj,
        trailing_newline=False,
    )


def raise_gate(payload: Dict[str, Any], decision: Dict[str, Any]) -> str:
    """Route a needs_gate decision to the approval queue (bridge).

    The gate stores the routing key opaque; the sector reads it back.
    External calls here are retried (three attempts, exponential backoff)
    so a flapping external system never kills the settle pass.
    """
    import subprocess

    entry = {
        "source": {"sector": SECTOR,
                   "client_id": payload["client_id"],
                   "request": payload["request"]},
        "risk_tier": decision["risk_tier"],
        "reason": decision["reason"],
    }
    proc = _raise_gate_subprocess(entry)
    return proc["gate_id"]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _raise_gate_subprocess(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Raise a gate queue entry; retry the external call on failure."""
    proc = subprocess.run(
        [sys.executable, os.path.join(GATE_DIR, "gate_script.py"), "raise"],
        input=json.dumps(entry), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "gate raise failed (%s): %s"
            % (proc.returncode, (proc.stderr or "").strip()[-160:]))
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("gate raise spoke non-JSON: %s" % exc)


def settle_gate_entry(entry: Dict[str, Any]) -> Optional[str]:
    """Parse a gate queue entry and return the gate id if this sector may pull.

    Fail-closed: a malformed gate entry never silences the queue - every
    entry is reported, every entry is either this sector's or it is not.
    """
    try:
        model = GateEntry(**entry)
    except ValidationError:
        return None
    if model.source.sector != SECTOR:
        return None
    if model.state != "closed" or model.decision != "approved":
        return None
    return model.gate_id


def settle_gates() -> None:
    """Pull approved gates into tickets. Sector knows the gate; never the reverse."""
    settled = []
    claimed = {}  # gate_id -> the tickets that already claim it

    # Settle memory = every ticket this sector ever pulled, live or archived.
    sector_root = os.path.dirname(TICKETS_DIR)
    for root, _dirs, files in os.walk(sector_root):
        for name in files:
            if not (name.startswith("ticket_") and name.endswith(".json")):
                continue
            path = os.path.join(root, name)
            ticket = _read_json(path)
            if isinstance(ticket, dict):
                via = ticket.get("via")
                if isinstance(via, str):
                    claimed.setdefault(via, []).append(path)

    if os.path.isdir(GATE_DIR):
        queue = os.path.join(GATE_DIR, "gate_queue")
        if os.path.isdir(queue):
            for name in sorted(os.listdir(queue)):
                if not name.startswith("gate_"):
                    continue
                entry_path = os.path.join(queue, name)
                entry = _read_json(entry_path)
                if not isinstance(entry, dict):
                    continue
                gid = settle_gate_entry(entry)
                if gid is None:
                    continue
                if gid in claimed:
                    # Claimed before it was decided: a producer writes its
                    # ticket while the gate is still open, so the approved gate
                    # *is* that ticket's decision — accept it in place instead
                    # of leaving it at the human seat forever.
                    for claimant in claimed[gid]:
                        accepted = accept_claimed_ticket(claimant, gid, entry)
                        if accepted:
                            settled.append(accepted)
                    continue
                reason = "approved via gate %s by %s" % (
                    gid, entry.get("decided_by") or "unknown")
                decision = {
                    "status": "accepted",
                    "risk_tier": entry.get("risk_tier"),
                    "reason": reason,
                }
                path, _ = save_ticket(entry.get("source"), decision, via=gid)
                settled.append(path)

    print(SettleOutput(settled=settled).model_dump_json(ensure_ascii=False))


def accept_claimed_ticket(path: str, gate_id: str, entry: Dict[str, Any]) -> Optional[str]:
    """Accept the ticket that claimed this gate while the gate was still open.

    A producer (``auto_ideator``) writes its ticket *before* a human decides,
    so the ticket holds the human seat: ``pending_approval`` with a
    ``needs_gate`` decision. The approved gate is that ticket's decision, so it
    becomes dispatchable in exactly the shape :func:`save_ticket` writes for a
    settled gate (``state: open`` + ``decision.status: accepted``) — the shape
    the dispatcher and the settle bridge act on. Without this the ticket sits
    at the human seat forever asking for a decision already made, and no
    capsule is ever built for it.

    Anything not waiting on this gate is left byte-for-byte alone.
    """
    ticket = _read_json(path)
    if not isinstance(ticket, dict):
        return None
    if ticket.get("state") not in ("pending_approval", "needs_revision"):
        return None
    decision = ticket.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    if decision.get("status") != "needs_gate":
        return None

    reason = "approved via gate %s by %s" % (
        gate_id, entry.get("decided_by") or "unknown")
    history = ticket.get("history")
    history = list(history) if isinstance(history, list) else []
    history.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "from_state": ticket.get("state"),
        "to_state": "open",
        "by": "settle",
        "why": reason,
    })
    _write_json(path, {
        "state": "open",
        "decision": {
            "status": "accepted",
            "risk_tier": entry.get("risk_tier"),
            "reason": reason,
            "ticket": decision.get("ticket"),
        },
        "history": history,
    })
    return path


def save_ticket(payload: Dict[str, Any], decision: Dict[str, Any],
                via: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Build a ticket, write it atomically, and return (path, model).

    Fail-closed: a ticket that does not validate cannot be written.
    """
    now = datetime.now(timezone.utc)
    ticket_id = "T-%s-%s" % (now.strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:8])
    ticket = {
        "ticket_id": ticket_id,
        "client_id": payload["client_id"],
        "request": payload["request"],
        "decision": decision,
        "created_at": now.isoformat(),
        "state": "open",
        "via": via,
    }
    path = os.path.join(TICKETS_DIR,
                        "ticket_%s_%s.json" % (payload["client_id"], ticket_id))
    # Validate before we commit: a broken ticket is a violated contract.
    Ticket(**ticket)
    _write_json(path, ticket)
    return path, ticket


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "settle":
        settle_gates()
        return

    try:
        payload = json.load(sys.argv[1] if len(sys.argv) > 1 else sys.stdin)
    except (json.JSONDecodeError, IndexError, OSError) as exc:
        print(json.dumps({"status": "rejected", "risk_tier": None,
                          "reason": "invalid_input: %s" % exc}))
        return
    decision = decide(payload)

    if decision["status"] == "accepted":
        path, _ = save_ticket(payload, decision)
        decision["ticket"] = os.path.basename(path)
    elif decision["status"] == "needs_gate":
        decision["gate"] = raise_gate(payload, decision)
    print(json.dumps(decision, ensure_ascii=False))


if __name__ == "__main__":
    main()
