#!/usr/bin/env python3
"""gate_script.py — human decisions for needs_gate entries. No auto-approve, ever.

Every write is crash-safe: entries are written atomically (temp + fsync +
``os.replace``) and ``decide`` runs as a lock-serialized read-modify-write on
a sidecar ``.lock`` file, so two humans deciding the same gate at the same
moment can never both win — the second one is told the gate is already closed.
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

# This script runs as a subprocess (``sys.executable gate_script.py ...``), so
# the shared kernel's crash-safe write helper must be importable without any
# caller-side PYTHONPATH setup: bootstrap the engine directory from the known
# repository layout (gate_script lives three levels below the root).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "engine"))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)
def _load_engine_module(name, filename):
    """Load a pure-stdlib engine module by path when the package import fails.

    ``shared_kernel/__init__`` pulls pydantic-backed schemas, so a stripped
    runtime can lack the package chain even though these helpers are pure
    standard library. The write-safety guarantee (and the reviewed-hash
    canonicalization) must never depend on an installed wheel.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ENGINE_ROOT, "shared_kernel", filename)
    )
    if spec is None or spec.loader is None:
        sys.exit(
            "gate_script: cannot locate shared_kernel/%s — refusing to run unlocked"
            % filename
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    from shared_kernel.atomic_io import atomic_json, update_json
    from shared_kernel.review_hash import (
        ReviewHashError,
        compare_elements,
        elements_for,
        gate_review_fields,
        reviewed_digest,
    )
except ImportError:
    _atomic_io = _load_engine_module("shared_kernel_atomic_io", "atomic_io.py")
    atomic_json = _atomic_io.atomic_json
    update_json = _atomic_io.update_json
    _review_hash = _load_engine_module("shared_kernel_review_hash", "review_hash.py")
    ReviewHashError = _review_hash.ReviewHashError
    compare_elements = _review_hash.compare_elements
    elements_for = _review_hash.elements_for
    gate_review_fields = _review_hash.gate_review_fields
    reviewed_digest = _review_hash.reviewed_digest

# The queue directory: GATE_QUEUE_DIR when set (the same contract
# shared_kernel.paths and the API use), else the repository default beside
# this script.
QUEUE_DIR = os.environ.get("GATE_QUEUE_DIR", "").strip() or os.path.join(_HERE, "gate_queue")


class _GateEntryError(Exception):
    """A decision was refused by the entry's own state (message is user-facing).

    ``code`` is the optional ``/v1`` machine-readable refusal (``/api`` ignores
    it; the v1 service maps it to the error envelope and a status). A refusal
    with no code is a legacy/generic one and keeps the historical wording.
    """

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


#: Distinct exit codes for the coded refusals a ``/v1`` caller must tell apart.
#: The stderr line also carries the code token, so a caller may key on either.
_EXIT_CODES = {
    "STALE_CONTENT_HASH": 3,
    "HASH_SET_MISMATCH": 3,
    "IDEMPOTENCY_CONFLICT": 4,
    "ALREADY_DECIDED": 5,
}


def _refuse_coded(exc):
    """Exit with the coded refusal's token in stderr and its distinct status."""
    code = getattr(exc, "code", None)
    if code:
        sys.stderr.write("decide failed: %s: %s\n" % (code, exc))
        sys.exit(_EXIT_CODES.get(code, 1))
    sys.exit("decide failed: %s" % exc)


def now_utc():
    return datetime.now(timezone.utc)


def new_gate_id():
    return "G-%s-%s" % (now_utc().strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:8])


def entry_path(gate_id):
    s = str(gate_id)
    if s.endswith(".json") or os.sep in s:
        return s
    return os.path.join(QUEUE_DIR, "gate_%s.json" % gate_id)


def load_entry(gate_id):
    path = entry_path(gate_id)
    with open(path, encoding="utf-8") as fh:
        entry = json.load(fh)
    try:
        from shared_kernel.schema_version import KIND_GATE_ENTRY, check_schema_version
        check_schema_version(KIND_GATE_ENTRY, entry, path)
    except ImportError:
        pass
    return entry


def save_entry(entry):
    """Atomically persist one entry (temp + fsync + replace — never torn)."""
    try:
        from shared_kernel.schema_version import KIND_GATE_ENTRY, stamp_schema_version
        stamp_schema_version(KIND_GATE_ENTRY, entry)
    except ImportError:
        pass
    atomic_json(
        entry_path(entry["gate_id"]),
        entry,
        ensure_ascii=False,
        indent=2,
        trailing_newline=False,  # byte-compatible with the historical writer
    )


def cmd_raise(args):
    """Create an open entry from a needs_gate decision (bridge + tests)."""
    source = sys.stdin if args.file == "-" else open(args.file, encoding="utf-8")
    with source:
        payload = json.load(source)
    missing = [f for f in ("source", "reason") if f not in payload]
    if missing:
        sys.exit("raise failed: missing " + ", ".join(missing))
    entry = {
        "gate_id": new_gate_id(),
        "source": payload["source"],
        "risk_tier": payload.get("risk_tier"),
        "reason": payload["reason"],            # why it was raised
        "opened_at": now_utc().isoformat(),
        "state": "open",
        "decision": None,                        # approved | rejected
        "decided_by": None,
        "decided_at": None,
        "decision_reason": None,
    }
    # Optional linkage: a raiser that already knows the ticket/capsule this gate
    # blocks records it here so the cockpit can answer "which capsule is
    # waiting?" straight from the gate record. Absent keys stay absent — the
    # API correlates them from the ticket tree when they are not provided.
    for optional in ("capsule_id", "capsule_title", "ticket_id"):
        value = payload.get(optional)
        if value:
            entry[optional] = value
    save_entry(entry)
    print(json.dumps(entry, ensure_ascii=False))


def cmd_list(args):
    if not os.path.isdir(QUEUE_DIR):
        return
    shown = 0
    skipped = 0
    for name in sorted(os.listdir(QUEUE_DIR)):
        if not name.startswith("gate_"):
            continue
        p = os.path.join(QUEUE_DIR, name)
        try:
            with open(p, encoding="utf-8") as fh:
                entry = json.load(fh)
            from shared_kernel.schema_version import KIND_GATE_ENTRY, check_schema_version

            # Per-item fail closed: an entry this build cannot understand is
            # skipped and reported — it never crashes the whole listing.
            check_schema_version(KIND_GATE_ENTRY, entry, p)
        except ImportError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            skipped += 1
            print("%-28s %-6s %s" % (name, "CORRUPT", "skipped: %s" % exc))
            continue
        except ValueError as exc:  # SchemaVersionError (fail closed, reported)
            skipped += 1
            print("%-28s %-6s %s" % (name, "BAD-SCHEMA", "skipped: %s" % exc))
            continue
        if entry["state"] == "open" or args.all:
            shown += 1
            print("%-28s %-6s %-12s %s"
                  % (entry["gate_id"], entry["state"],
                     entry["risk_tier"] or "null", entry["reason"]))
    if shown == 0 and skipped == 0:
        print("queue empty")
    if skipped:
        print("skipped (fail-closed): %d entr%s"
              % (skipped, "y" if skipped == 1 else "ies"))


def cmd_decide(args):
    """One explicit human decision. Never bulk, never silent.

    The read-check-write runs inside one sidecar-lock critical section
    (:func:`shared_kernel.atomic_io.update_json`), so concurrent decisions on
    the same gate serialize: exactly one wins, the rest are refused with
    "already closed" instead of silently overwriting the winner.

    The ``/v1`` contract (``--v1``) adds two optional, in-lock guarantees on top
    of the legacy behaviour:

    - ``--expected-elements`` — the client's attested ``{field: sha256}`` map.
      The server recomputes the reviewed fields *inside the lock* and refuses
      (``STALE_CONTENT_HASH`` / ``HASH_SET_MISMATCH``, nothing written) on any
      mismatch, so the hash can never be verified against a value the write
      later invalidates;
    - ``--idempotency-key``/``--fingerprint`` — a replay of the *same* decision
      returns the closed entry without rewriting it; the same key with a
      different payload is ``IDEMPOTENCY_CONFLICT``.

    Legacy callers pass none of these and get the byte-identical behaviour
    (plus a recorded ``evidence`` block with ``attested=false``).
    """
    if not args.by or args.by.strip() == "":
        sys.exit("decide failed: --by is required (a human id)")
    if args.by.strip().lower() == "system":
        sys.exit("decide failed: decided_by must be a human, never 'system'")
    if args.decision == "rejected" and not args.reason:
        sys.exit("decide failed: rejections always carry a reason")

    v1 = bool(getattr(args, "v1", False))
    idempotency_key = getattr(args, "idempotency_key", None)
    fingerprint = getattr(args, "fingerprint", None)
    expected_raw = getattr(args, "expected_elements", None)
    expected = None
    if expected_raw:
        try:
            expected = json.loads(expected_raw)
        except (TypeError, ValueError):
            sys.exit("decide failed: --expected-elements must be a JSON object")
        if not isinstance(expected, dict):
            sys.exit("decide failed: --expected-elements must be a JSON object")

    marker = {"replayed": False}

    def _apply(entry):
        if not isinstance(entry, dict):
            raise _GateEntryError("no such gate entry: %s" % args.gate_id)
        p = entry_path(args.gate_id)
        try:
            from shared_kernel.schema_version import (
                KIND_GATE_ENTRY,
                check_schema_version,
                stamp_schema_version,
            )

            check_schema_version(KIND_GATE_ENTRY, entry, p)
            stamp_schema_version(KIND_GATE_ENTRY, entry)
        except ImportError:
            pass
        if entry.get("state") != "open":
            # A repeat with the same key + fingerprint is a replay, not a
            # second decision: report the original entry and write nothing.
            evidence = entry.get("evidence")
            evidence = evidence if isinstance(evidence, dict) else {}
            if (
                idempotency_key
                and evidence.get("idempotency_key") == idempotency_key
            ):
                if evidence.get("fingerprint") == fingerprint:
                    marker["replayed"] = True
                    return None
                raise _GateEntryError(
                    "Idempotency key reused with a different request payload",
                    code="IDEMPOTENCY_CONFLICT",
                )
            raise _GateEntryError(
                "gate %s already closed" % args.gate_id,
                code="ALREADY_DECIDED",
            )

        # Attestation: recompute the reviewed fields *here*, under the lock, and
        # fail closed before anything is mutated. A byte that changed after the
        # reviewer saw it can never be signed.
        if expected is not None:
            actual = elements_for(gate_review_fields(entry))
            compare_elements(expected, actual)

        decided_at = now_utc().isoformat()
        if decided_at < entry["opened_at"]:
            raise _GateEntryError("clock skew — decided_at before opened_at")
        entry.update({
            "state": "closed",
            "decision": args.decision,
            "decided_by": args.by.strip(),
            "decided_at": decided_at,
            "decision_reason": args.reason,
        })
        attested = expected is not None
        entry["evidence"] = {
            "decision_id": uuid.uuid4().hex,
            "actor": args.by.strip(),
            "attested": attested,
            "algorithm": "sha256" if attested else None,
            "elements": dict(expected) if attested else {},
            "unviewed": [],
            "primary_sha256": None,
            "reviewed_sha256": (
                reviewed_digest(elements=dict(expected)) if attested else None
            ),
            "action": args.decision,
            "reason": args.reason,
            "fingerprint": fingerprint,
            "idempotency_key": idempotency_key,
            "at": decided_at,
        }
        return entry

    try:
        entry = update_json(entry_path(args.gate_id), _apply, default=None)
    except ReviewHashError as exc:
        _refuse_coded(exc)
    except _GateEntryError as exc:
        _refuse_coded(exc)
    except ValueError as exc:
        # SchemaVersionError: an unknown/higher schema_version fails closed —
        # the entry is refused with a clean verdict, never a traceback, and
        # nothing is written.
        sys.exit("decide failed (fail-closed): %s" % exc)
    except json.JSONDecodeError:
        # A missing file becomes "no such gate entry" inside _apply (default
        # None); a decode error is the same user-facing verdict. Write-phase
        # OSErrors are NOT mapped here — they propagate, exactly as the old
        # save_entry did, so a failed write is never misreported as a bad id.
        sys.exit("decide failed: no such gate entry: %s" % args.gate_id)

    if v1:
        print(json.dumps(
            {"entry": entry, "replayed": bool(marker["replayed"])},
            ensure_ascii=False,
        ))
    else:
        print(json.dumps(entry, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_raise = sub.add_parser("raise", help="open a gate entry")
    p_raise.add_argument("--file", default="-", help="JSON file or - for stdin")
    p_raise.set_defaults(func=cmd_raise)

    p_list = sub.add_parser("list", help="show the queue")
    p_list.add_argument("--all", action="store_true", help="include closed")
    p_list.set_defaults(func=cmd_list)

    p_dec = sub.add_parser("decide", help="record one human decision")
    p_dec.add_argument("--gate-id", required=True)
    p_dec.add_argument("--by", required=True, help="human id")
    p_dec.add_argument("--decision", required=True,
                       choices=["approved", "rejected"])
    p_dec.add_argument("--reason")
    p_dec.add_argument(
        "--expected-elements",
        default=None,
        help="v1: JSON map of reviewed field -> sha256 (verified under the lock)",
    )
    p_dec.add_argument(
        "--idempotency-key", default=None, help="v1: client replay key"
    )
    p_dec.add_argument(
        "--fingerprint", default=None, help="v1: digest of the decision request"
    )
    p_dec.add_argument(
        "--v1", action="store_true", help="v1: machine-readable output envelope"
    )
    p_dec.set_defaults(func=cmd_decide)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
