#!/usr/bin/env python3
"""ticket_mgr.py — CLI and library for managing ticket lifecycles and segregation.

Segregation Contract:
- `scratch/`: drafts, exploratory ideas, trial tickets. Excluded from production factory.
- `approved/`: vetted tickets authorized for manufacturing and pulse settlement.
- `archive/`: settled or retired tickets.

Usage:
    python3 -m tools.ticket_mgr list [--sector marketing] [--stage scratch|approved|all]
    python3 -m tools.ticket_mgr create --sector marketing --title "..." --content "..." [--stage scratch|approved]
    python3 -m tools.ticket_mgr promote <ticket_id> [--sector marketing]
    python3 -m tools.ticket_mgr demote <ticket_id> [--sector marketing]
    python3 -m tools.ticket_mgr inspect <ticket_id> [--sector marketing]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
for _p in (str(REPO_ROOT), str(ENGINE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from shared_kernel.paths import DOMAINS_DIR
except Exception:
    DOMAINS_DIR = REPO_ROOT / "data" / "domains"

from shared_kernel.atomic_io import atomic_json, update_json  # noqa: E402
from shared_kernel.review_hash import (  # noqa: E402
    ReviewHashError,
    compare_elements,
    elements_for,
    reviewed_digest,
    ticket_review_fields,
)
from shared_kernel.state_schema import can_transition  # noqa: E402

logger = logging.getLogger("tools.ticket_mgr")

#: Per-item fail-closed: tickets whose schema_version this build does not
#: understand are skipped (and logged), never listed as if they were ours.
LAST_LIST_SKIPPED: List[Dict[str, Any]] = []

#: The bounded, ordered decision ledger a promoted / demoted ticket carries.
#: Optional by design: a ticket written before this slice carries no such key,
#: loads unchanged, and the list is stamped on the next decision write (the same
#: precedent a capsule manifest's ``decisions`` list follows). ``None`` keys and
#: fingerprints are recorded too, so a legacy decision is still auditable.
DECISIONS_KEY = "decisions"
MAX_DECISION_RECORDS = 50


class TicketDecisionError(Exception):
    """A deliberate, fail-closed ticket-decision refusal.

    Carries the ``/v1`` machine-readable ``code`` and the HTTP status the API
    should answer, so the write path can refuse without importing the v1
    envelope. The legacy CLI/API path ignores both (it only reads ``str(exc)``).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int = 409,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details) if details else None


def _decision_records(state: Dict[str, Any]) -> List[Any]:
    records = state.get(DECISIONS_KEY)
    return records if isinstance(records, list) else []


def _find_decision_by_key(state: Dict[str, Any], key: Optional[str]) -> Optional[Dict[str, Any]]:
    """The most recent decision recorded under ``key`` (a replay), or ``None``."""
    if not key:
        return None
    for record in reversed(_decision_records(state)):
        if isinstance(record, dict) and record.get("key") == key:
            return record
    return None


def _last_decision(state: Dict[str, Any], action: str) -> Optional[Dict[str, Any]]:
    for record in reversed(_decision_records(state)):
        if isinstance(record, dict) and record.get("action") == action:
            return record
    return None


def _append_decision(state: Dict[str, Any], record: Dict[str, Any]) -> None:
    records = state.get(DECISIONS_KEY)
    if not isinstance(records, list):
        records = []
    records.append(record)
    state[DECISIONS_KEY] = records[-MAX_DECISION_RECORDS:]


def _already_decided(state: Dict[str, Any], *, ticket_id: str, action: str, verb: str) -> None:
    """Refuse a second, non-replayed decision on a ticket already settled.

    The refusal names who decided, when, and the decision that stands. The
    original record travels in ``details["decision"]`` so a concurrent double
    decision (the losing writer re-resolves a ticket another writer just moved)
    answers with the same information as the sequential path.
    """
    prior = _last_decision(state, action)
    details: Dict[str, Any] = {"ticket_id": ticket_id, "action": action}
    if prior is not None:
        details["decided_by"] = prior.get("actor")
        details["decided_at"] = prior.get("at")
        details["decision"] = {
            "decision_id": prior.get("decision_id"),
            "action": prior.get("action"),
            "stage_before": prior.get("stage_before"),
            "stage_after": prior.get("stage_after"),
            "state_before": prior.get("state_before"),
            "state_after": prior.get("state_after"),
        }
    raise TicketDecisionError(
        f"Ticket '{ticket_id}' is already {verb} — the decision stands (fail closed).",
        code="ALREADY_DECIDED",
        status=409,
        details=details,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs(sector_dir: Path) -> Tuple[Path, Path, Path]:
    tdir = sector_dir / "tickets"
    s_dir = tdir / "scratch"
    a_dir = tdir / "approved"
    arc_dir = tdir / "archive"
    for d in (s_dir, a_dir, arc_dir):
        d.mkdir(parents=True, exist_ok=True)
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()
    return s_dir, a_dir, arc_dir


def find_ticket(ticket_id: str, sector: Optional[str] = None) -> Optional[Tuple[str, str, Path, Dict[str, Any]]]:
    """Find a ticket by ticket_id across domains. Returns (sector, stage, file_path, data)."""
    search_sectors = [sector] if sector else [
        d.name for d in DOMAINS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_"))
    ]

    for sec in search_sectors:
        tdir = DOMAINS_DIR / sec / "tickets"
        if not tdir.is_dir():
            continue

        # Look in scratch, approved, archive, and legacy root
        candidates = [
            ("scratch", tdir / "scratch"),
            ("approved", tdir / "approved"),
            ("archive", tdir / "archive"),
            ("approved", tdir),
        ]

        for stage, folder in candidates:
            if not folder.is_dir():
                continue
            for f in folder.glob("ticket_*.json"):
                if f.name.endswith(".tmp"):
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    try:
                        from shared_kernel.schema_version import KIND_TICKET, check_schema_version

                        check_schema_version(KIND_TICKET, data, f)
                    except ImportError:
                        pass
                    tid = str(data.get("ticket_id") or data.get("id") or f.stem)
                    if tid == ticket_id or ticket_id in f.name:
                        return sec, stage, f, data
                except Exception:
                    continue
    return None


def list_tickets(sector: Optional[str] = None, stage: str = "all") -> List[Dict[str, Any]]:
    """List tickets filtered by sector and stage ('scratch', 'approved', 'archive', 'all')."""
    results: List[Dict[str, Any]] = []
    search_sectors = [sector] if sector else sorted(
        d.name for d in DOMAINS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_"))
    )

    for sec in search_sectors:
        tdir = DOMAINS_DIR / sec / "tickets"
        if not tdir.is_dir():
            continue

        stages_to_check = []
        if stage in ("scratch", "all"):
            stages_to_check.append(("scratch", tdir / "scratch"))
        if stage in ("approved", "all"):
            stages_to_check.append(("approved", tdir / "approved"))
            # Include legacy root tickets as approved
            stages_to_check.append(("approved", tdir))
        if stage in ("archive", "all"):
            stages_to_check.append(("archive", tdir / "archive"))

        seen_tids = set()
        for stg, folder in stages_to_check:
            if not folder.is_dir():
                continue
            for f in sorted(folder.glob("ticket_*.json")):
                if "sync-conflict" in f.name or f.name.endswith(".tmp"):
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    try:
                        from shared_kernel.schema_version import KIND_TICKET, check_schema_version

                        check_schema_version(KIND_TICKET, data, f)
                    except ImportError:
                        pass
                    except Exception as exc:  # SchemaVersionError: skip + report
                        LAST_LIST_SKIPPED.append(
                            {"path": str(f), "reason": str(exc)}
                        )
                        logger.warning("list_tickets: skipped (fail-closed): %s: %s", f, exc)
                        continue
                    tid = str(data.get("ticket_id") or data.get("id") or f.stem)
                    if tid in seen_tids:
                        continue
                    seen_tids.add(tid)
                    try:
                        shown_path = str(f.relative_to(REPO_ROOT))
                    except ValueError:
                        # relocated vault (env override / restore target):
                        # show the absolute path rather than dropping the row
                        shown_path = str(f)
                    results.append({
                        "ticket_id": tid,
                        "sector": sec,
                        "stage": stg,
                        "title": data.get("title") or (data.get("metadata", {}).get("title") if isinstance(data.get("metadata"), dict) else tid),
                        "state": data.get("state", "open"),
                        "created_at": data.get("created_at") or data.get("metadata", {}).get("timestamp", "Unknown"),
                        "path": shown_path,
                    })
                except Exception:
                    continue

    return results


def create_ticket(
    sector: str,
    title: str,
    content: str,
    stage: str = "scratch",
    cadence: str = "weekly_routine",
) -> Dict[str, Any]:
    """Create a new ticket in the specified stage ('scratch' or 'approved')."""
    sec_clean = sector.strip().lower()
    sector_dir = DOMAINS_DIR / sec_clean
    if not sector_dir.is_dir():
        raise ValueError(f"Sector '{sec_clean}' does not exist.")

    s_dir, a_dir, _ = _ensure_dirs(sector_dir)
    target_dir = a_dir if stage == "approved" else s_dir

    now_dt = datetime.now(timezone.utc)
    ts_str = now_dt.strftime("%Y%m%dT%H%M%SZ")
    uid_str = uuid.uuid4().hex[:8]
    prefix = "human" if stage == "approved" else "scratch"
    ticket_id = f"T-{prefix.upper()}-{ts_str}-{uid_str}"

    full_content = f"# {title.strip()}\n\n{content.strip()}"

    ticket_data = {
        "ticket_id": ticket_id,
        "client_id": f"{prefix}_mgr",
        "sector": sec_clean,
        "stage": stage,
        "title": title.strip(),
        "request": full_content,
        "state": "pending_approval" if stage == "approved" else "draft",
        "created_at": now_dt.isoformat(),
        "cadence": cadence,
        "metadata": {
            "title": title.strip(),
            "cadence": cadence,
            "stage": stage,
            "source": "ticket_mgr",
            "timestamp": now_dt.isoformat(),
        },
        "history": [
            {
                "at": now_dt.isoformat(),
                "from": None,
                "to": "draft" if stage == "scratch" else "pending_approval",
                "by": f"ticket_mgr:{prefix}",
                "why": f"ticket created in {stage} stage",
            }
        ],
    }

    try:
        from shared_kernel.schema_version import KIND_TICKET, stamp_schema_version

        stamp_schema_version(KIND_TICKET, ticket_data)
    except ImportError:
        pass
    target_file = target_dir / f"ticket_{ticket_id}.json"
    atomic_json(target_file, ticket_data, trailing_newline=False)
    return {
        "status": "success",
        "ticket_id": ticket_id,
        "sector": sec_clean,
        "stage": stage,
        "path": str(target_file.relative_to(REPO_ROOT)),
    }


def _legacy_target_state(action: str, prev_state: Optional[str]) -> str:
    """The state a legacy promote/demote writes (unchanged legacy behaviour)."""
    if action == "promote":
        return "open" if prev_state == "draft" else prev_state
    return "draft"


def _v1_target_state(action: str, prev_state: Optional[str]) -> str:
    """The ``/v1`` target state, refusing an edge ``can_transition`` rejects.

    Identical to the legacy writes for every lawful case (promote: ``draft ->
    open``, otherwise stage-only; demote: ``-> draft``) but fails closed with
    ``ILLEGAL_STATE`` when the transition table says the edge does not exist.
    """
    if action == "promote":
        target = "open" if prev_state == "draft" else prev_state
        if target != prev_state and not can_transition(prev_state, target):
            raise TicketDecisionError(
                f"Illegal state transition: {prev_state!r} -> {target!r}",
                code="ILLEGAL_STATE",
                status=400,
                details={"from": prev_state, "to": target},
            )
        return target
    if not can_transition(prev_state, "draft"):
        raise TicketDecisionError(
            f"Illegal state transition: {prev_state!r} -> 'draft'",
            code="ILLEGAL_STATE",
            status=400,
            details={"from": prev_state, "to": "draft"},
        )
    return "draft"


def _move_ticket(
    ticket_id: str,
    sector: Optional[str],
    *,
    target_stage: str,
    action: str,
    decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Promote / demote a ticket, verified and recorded inside one lock.

    ``decision is None`` is the legacy call: behaviour is the writer it
    replaced, plus a recorded ``decisions`` entry with ``attested=false`` — the
    legacy ``/api`` and CLI paths keep working, unattested. A ``decision``
    mapping is the ``/v1`` call and adds, all *inside* the ticket's sidecar lock
    and before any write:

    - hash verification of ``expected_elements`` against the reviewed fields
      (a mismatch raises :class:`ReviewHashError` -> ``STALE_CONTENT_HASH`` /
      ``HASH_SET_MISMATCH``, nothing written);
    - an idempotent replay (same ``idempotency_key`` + ``fingerprint``) that
      returns the original record and writes nothing;
    - ``IDEMPOTENCY_CONFLICT`` for a reused key with a different payload;
    - ``ALREADY_DECIDED`` (with ``decided_by``/``decided_at``) when the ticket
      is already settled by a decision that was not this replay;
    - ``ILLEGAL_STATE`` for a transition ``can_transition`` refuses.

    The hash is checked here, not in the API layer: a value verified before the
    lock could change before the write — the exact TOCTOU this check exists to
    prevent.
    """
    verb = "promoted" if target_stage == "approved" else "demoted"
    found = find_ticket(ticket_id, sector)
    if not found:
        raise ValueError(f"Ticket '{ticket_id}' not found.")

    sec, cur_stage, file_path, _data = found

    s_dir, a_dir, _arc = _ensure_dirs(DOMAINS_DIR / sec)
    target_dir = a_dir if target_stage == "approved" else s_dir
    target_path = target_dir / file_path.name

    v1 = decision is not None
    if not v1 and cur_stage == target_stage:
        return {
            "status": "already_approved" if target_stage == "approved" else "already_scratch",
            "ticket_id": ticket_id,
            "sector": sec,
            "path": str(file_path.relative_to(REPO_ROOT)),
        }

    outcome: Dict[str, Any] = {}

    def _commit_move(state):
        if not isinstance(state, dict):
            return None

        if state.get("stage") == target_stage:
            if v1:
                key = decision.get("idempotency_key")
                prior = _find_decision_by_key(state, key)
                if prior is not None:
                    if prior.get("fingerprint") == decision.get("fingerprint"):
                        outcome.update({
                            "replayed": True,
                            "decision_id": prior.get("decision_id"),
                            "state_before": prior.get("state_before"),
                            "state_after": prior.get("state_after"),
                            "attested": bool(prior.get("attested")),
                            "reviewed_sha256": prior.get("reviewed_sha256"),
                            "decided_at": prior.get("at"),
                        })
                        return None
                    raise TicketDecisionError(
                        "Idempotency key reused with a different request payload",
                        code="IDEMPOTENCY_CONFLICT",
                        status=409,
                        details={"idempotency_key": key},
                    )
                _already_decided(state, ticket_id=ticket_id, action=action, verb=verb)
            return None

        prev_stage = state.get("stage")
        prev_state = state.get("state")

        expected = None
        if v1:
            expected = decision.get("expected_elements")
            reviewed = decision.get("reviewed")
            if action == "promote" and (expected is None or reviewed is None):
                raise TicketDecisionError(
                    "Attestation is mandatory to promote a ticket.",
                    code="ATTESTATION_REQUIRED",
                    status=400,
                )
            if expected is not None:
                actual = elements_for(ticket_review_fields(state))
                compare_elements(expected, actual)  # raises ReviewHashError
            target_state = _v1_target_state(action, prev_state)
            attested = expected is not None
            reviewed_sha256 = (
                reviewed_digest(
                    elements=reviewed.get("elements") or {},
                    algorithm=reviewed.get("algorithm") or "sha256",
                    unviewed=reviewed.get("unviewed") or [],
                    primary_sha256=reviewed.get("primary_sha256"),
                )
                if attested
                else None
            )
        else:
            target_state = _legacy_target_state(action, prev_state)
            attested = False
            reviewed_sha256 = None

        decided_at = _now_iso()
        decision_id = uuid.uuid4().hex

        state["stage"] = target_stage
        state["state"] = target_state
        state.setdefault("history", []).append({
            "at": decided_at,
            "from": cur_stage,
            "to": target_stage,
            "by": f"ticket_mgr:{action}",
            "why": (
                "promoted from scratch to approved for manufacturing"
                if action == "promote"
                else "demoted to scratch for further editing/review"
            ),
        })

        record = {
            "key": decision.get("idempotency_key") if v1 else None,
            "fingerprint": decision.get("fingerprint") if v1 else None,
            "decision_id": decision_id,
            "ticket_id": ticket_id,
            "action": action,
            "state_before": prev_state,
            "state_after": target_state,
            "stage_before": prev_stage,
            "stage_after": target_stage,
            "reason": ((decision.get("reason") if v1 else "") or ""),
            "actor": (decision.get("actor") if v1 else f"ticket_mgr:{action}"),
            "attested": attested,
            "algorithm": "sha256" if attested else None,
            "elements": dict(expected) if attested else {},
            "unviewed": (
                list((decision.get("reviewed") or {}).get("unviewed") or [])
                if attested
                else []
            ),
            "primary_sha256": None,
            "reviewed_sha256": reviewed_sha256,
            "at": decided_at,
        }
        _append_decision(state, record)

        try:
            from shared_kernel.schema_version import KIND_TICKET, stamp_schema_version

            stamp_schema_version(KIND_TICKET, state)
        except ImportError:
            pass

        outcome.update({
            "replayed": False,
            "decision_id": decision_id,
            "state_before": prev_state,
            "state_after": target_state,
            "attested": attested,
            "reviewed_sha256": reviewed_sha256,
            "decided_at": decided_at,
            "record": record,
        })

        atomic_json(target_path, state, trailing_newline=False)
        if target_path != file_path:
            file_path.unlink()
        return None

    if update_json(file_path, _commit_move, default=None, trailing_newline=False) is None:
        raise ValueError(f"Ticket '{ticket_id}' not found.")
    if not outcome:
        raise ValueError(f"Ticket '{ticket_id}' not found.")

    return {
        "status": "success",
        "ticket_id": ticket_id,
        "sector": sec,
        "previous_stage": cur_stage,
        "new_stage": target_stage,
        "path": str(target_path.relative_to(REPO_ROOT)),
        "decision_id": outcome.get("decision_id"),
        "state_before": outcome.get("state_before"),
        "state_after": outcome.get("state_after"),
        "replayed": bool(outcome.get("replayed")),
        "attested": bool(outcome.get("attested")),
        "reviewed_sha256": outcome.get("reviewed_sha256"),
        "decided_at": outcome.get("decided_at"),
    }


def promote_ticket(
    ticket_id: str,
    sector: Optional[str] = None,
    *,
    decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Promote a ticket from scratch to approved (accept the idea).

    ``decision=None`` is the legacy call (unchanged); a mapping is the attested
    ``/v1`` call — see :func:`_move_ticket`.
    """
    return _move_ticket(
        ticket_id, sector, target_stage="approved", action="promote", decision=decision
    )


def demote_ticket(
    ticket_id: str,
    sector: Optional[str] = None,
    *,
    decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Demote a ticket from approved to scratch (send the idea back).

    ``decision=None`` is the legacy call (unchanged); a mapping is the ``/v1``
    call — a reason, never a hash.
    """
    return _move_ticket(
        ticket_id, sector, target_stage="scratch", action="demote", decision=decision
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ticket Segregation & Lifecycle Manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # list
    p_list = sub.add_parser("list", help="List tickets by stage")
    p_list.add_argument("--sector", "-s", help="Filter by sector")
    p_list.add_argument("--stage", choices=["scratch", "approved", "archive", "all"], default="all")

    # create
    p_create = sub.add_parser("create", help="Create a ticket")
    p_create.add_argument("--sector", "-s", required=True, help="Domain sector")
    p_create.add_argument("--title", "-t", required=True, help="Ticket title")
    p_create.add_argument("--content", "-c", required=True, help="Ticket content")
    p_create.add_argument("--stage", choices=["scratch", "approved"], default="scratch")

    # promote
    p_promo = sub.add_parser("promote", help="Promote ticket to approved")
    p_promo.add_argument("ticket_id", help="Ticket ID or filename snippet")
    p_promo.add_argument("--sector", "-s", help="Sector")

    # demote
    p_demo = sub.add_parser("demote", help="Demote ticket to scratch")
    p_demo.add_argument("ticket_id", help="Ticket ID or filename snippet")
    p_demo.add_argument("--sector", "-s", help="Sector")

    # inspect
    p_insp = sub.add_parser("inspect", help="Inspect a single ticket")
    p_insp.add_argument("ticket_id", help="Ticket ID")
    p_insp.add_argument("--sector", "-s", help="Sector")

    args = parser.parse_args()

    try:
        if args.cmd == "list":
            tickets = list_tickets(sector=args.sector, stage=args.stage)
            print(f"Total tickets ({args.stage}): {len(tickets)}")
            for t in tickets:
                print(f"  [{t['stage'].upper():8s}] {t['sector']:15s} | {t['ticket_id']:30s} | {t['title'][:40]}")
        elif args.cmd == "create":
            res = create_ticket(args.sector, args.title, args.content, stage=args.stage)
            print(json.dumps(res, indent=2, ensure_ascii=False))
        elif args.cmd == "promote":
            res = promote_ticket(args.ticket_id, sector=args.sector)
            print(json.dumps(res, indent=2, ensure_ascii=False))
        elif args.cmd == "demote":
            res = demote_ticket(args.ticket_id, sector=args.sector)
            print(json.dumps(res, indent=2, ensure_ascii=False))
        elif args.cmd == "inspect":
            found = find_ticket(args.ticket_id, sector=args.sector)
            if not found:
                print(f"Ticket '{args.ticket_id}' not found.", file=sys.stderr)
                sys.exit(1)
            sec, stg, pth, data = found
            print(f"Sector: {sec}")
            print(f"Stage:  {stg}")
            print(f"Path:   {pth}")
            print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
