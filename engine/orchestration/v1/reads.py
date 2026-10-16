#!/usr/bin/env python3
"""reads.py — the ``/v1`` aggregate reads: the mobile home and its drill-downs.

Slice M1a of the mobile contract. These reads are **pure**: no lock is taken, no
gate is opened, no baseline is bound, and no file's mtime moves — a ``GET`` here
cannot change the factory it describes. Every read is sector-scoped through
:mod:`orchestration.v1.scope`: the router proves an object's *real* sector with
``require_sector`` (an invisible object answers the same ``404`` as a missing
one), and every list and aggregate filters by the caller's visible sectors.

Where the numbers come from
---------------------------

- **Capsules, tickets and feedback** are read from the SQLite read index when it
  is on, and from the identical file-scan twins when it is off. Both paths apply
  the same predicate, and the response says which one ran (``source``), so a
  client is never told "index" while the truth came from a file scan.
- **The gate queue is always read from files.** There is no index for it, and a
  gate is the one object whose sector is written once and never changes; the
  response labels it (``source_detail.gates``).
- **Manifest reads**: list rows are built with **zero** manifest reads on the
  index path (a counting test enforces it), and every *detail* read opens
  exactly the one manifest it serves.

Attention is **recomputed**, never taken from ``attention_required.json``: that
report is written into the repository root, carries no sector attribution, and
is only as fresh as the last Watcher pass — it could not honour "never return
another sector's items", and a stale report is a lie with a timestamp on it. So
this module applies the engine's *own* attention predicate (the one
``watcher.inspect_manifest`` uses) to the scoped, indexed view.

Home is built from **common fields only** (domain, stage, needs-me, cost,
revision position, approver), never from task-type-specific content, so a new
task type can never change its shape. Payload rules: no raw manifest blob, no
internal path, no inline image — images are referenced by element path.

Standard library plus the kernel it reads; Python 3.10+.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from capsule_engine.capabilities import (
    CAPABILITY_CAPSULE_APPROVE,
    CAPABILITY_CAPSULE_REJECT,
    CAPABILITY_GATE_APPROVE,
    CAPABILITY_GATE_REJECT,
    CAPABILITY_TICKET_CREATE,
)
from capsule_engine.manifest import parse_ts
from capsule_engine.manifest import revision_cap as _revision_cap
from orchestration import read_index
from orchestration.v1 import messages
from orchestration.v1.envelope import (
    DEFAULT_LIMIT,
    SOURCE_FILES,
    SOURCE_INDEX,
    SOURCE_PRINCIPAL,
    encode_cursor,
    envelope,
    page,
)
from orchestration.v1.errors import V1Error
from shared_kernel.state_schema import TICKET_TERMINAL_STATES

__all__ = [
    "attention",
    "capsule_detail",
    "capsule_trace",
    "capsules",
    "me",
    "overview",
    "ticket_awaiting",
    "ticket_detail",
    "tickets",
]

#: Capsule states that put a deliverable in front of a human at a *gate*. The
#: ``failed`` human-reset position is reported as ``system_halt`` instead.
GATE_STATES: Tuple[str, ...] = ("pending_approval", "stalled")

#: A ticket awaits a decision in the scratch stage (accept or send back), or at
#: a human gate / rework position. Archived work is retired, never pending.
TICKET_AWAITING_STATES: Tuple[str, ...] = ("pending_approval", "needs_revision")
TICKET_AWAITING_STAGE = "scratch"
TICKET_ARCHIVE_STAGE = "archive"

#: Terminal ticket states — the source of truth is ``shared_kernel.state_schema``.
TICKET_TERMINAL: Tuple[str, ...] = tuple(
    sorted(state.value for state in TICKET_TERMINAL_STATES)
)

#: The capabilities that make a decision possible, per entity kind. Holding any
#: one is enough for ``can_act``; the app chooses which verb to offer.
CAPSULE_DECIDING: Tuple[str, ...] = (CAPABILITY_CAPSULE_APPROVE, CAPABILITY_CAPSULE_REJECT)
TICKET_DECIDING: Tuple[str, ...] = (CAPABILITY_TICKET_CREATE,)
GATE_DECIDING: Tuple[str, ...] = (CAPABILITY_GATE_APPROVE, CAPABILITY_GATE_REJECT)

#: The cursor id prefixes — the same ones the read model's keyset SQL uses, so
#: the merged attention ordering and the per-kind queries cannot disagree.
CAPSULE_PREFIX = "capsule:"
TICKET_PREFIX = "ticket:"
GATE_PREFIX = "gate:"
QUARANTINED_PREFIX = "quarantined:"

#: Bounded text a ticket detail may embed (the rest is flagged as truncated).
TICKET_TEXT_CHARS = 4000
#: Bounded decision / history / gate entries a detail may embed.
MAX_LEDGER_ENTRIES = 50


def _api():
    """The orchestration api module (path helpers), imported lazily (cycle)."""
    from orchestration import api

    return api


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(at: Optional[str], now: datetime) -> Optional[float]:
    """Seconds since an ISO timestamp, or ``None`` when it cannot be read.

    The index's ``updated_at`` is the last write that moved the record — its
    approximation of "time in this state", because the exact figure lives in the
    manifest's history and reading that per row is exactly the N+1 this slice
    exists to avoid. The field is ``None`` (never zero) when unreadable.
    """
    parsed = parse_ts(at) if isinstance(at, str) else None
    if parsed is None:
        return None
    return round(max(0.0, (now - parsed).total_seconds()), 3)


def _served_by_index() -> bool:
    """Is the read index the active source? (the documented kill switch)"""
    return read_index.enabled()


# --------------------------------------------------------------------------- #
# the read model: one function per data family, index or file twin
# --------------------------------------------------------------------------- #


def _capsule_rows(domains: Optional[Sequence[str]], **filters: Any) -> Tuple[List[Dict[str, Any]], str]:
    if _served_by_index():
        return read_index.query_capsules(domains=domains, **filters), SOURCE_INDEX
    return read_index.query_capsules_from_files(domains=domains, **filters), SOURCE_FILES


def _ticket_rows(sectors: Optional[Sequence[str]], **filters: Any) -> Tuple[List[Dict[str, Any]], str]:
    if _served_by_index():
        return read_index.query_tickets(sectors=sectors, **filters), SOURCE_INDEX
    return read_index.query_tickets_from_files(sectors=sectors, **filters), SOURCE_FILES


def _rollup_rows(
    domains: Optional[Sequence[str]], *, day: Optional[str], cap: int
) -> Tuple[Dict[str, Any], str]:
    if _served_by_index():
        return read_index.domain_rollup(domains=domains, day=day, cap=cap), SOURCE_INDEX
    return (
        read_index.domain_rollup_from_files(domains=domains, day=day, cap=cap),
        SOURCE_FILES,
    )


def _decision_rows(domains: Optional[Sequence[str]], limit: int) -> Tuple[List[Dict[str, Any]], str]:
    """The newest capsule decisions (approvals *and* rejections), sector-scoped.

    Sourced from the index's projection of each capsule's own decision ledger —
    the attested record of who decided what — never from the feedback ledger,
    which only ever holds rejections, revision requests and notes. The index
    columns are filled at rebuild time, so this read opens no manifest.
    """
    if _served_by_index():
        return read_index.recent_decisions(domains=domains, limit=limit), SOURCE_INDEX
    return read_index.recent_decisions_from_files(domains=domains, limit=limit), SOURCE_FILES


def _quarantined_rows() -> List[Dict[str, Any]]:
    if _served_by_index():
        rows = read_index.quarantined_items()
        if rows is not None:
            return rows
    return read_index.quarantined_items_from_files()


# --------------------------------------------------------------------------- #
# credentials -> what this caller may decide
# --------------------------------------------------------------------------- #


def _can(principal: Any, capabilities: Sequence[str], sector: Optional[str]) -> bool:
    """Does ``principal`` hold any of ``capabilities`` on ``sector``?"""
    if principal is None:
        return False
    return any(principal.has(cap, resource=sector) for cap in capabilities)


def _capability_scope(
    principal: Any, capabilities: Sequence[str], *, sees_all: bool, sectors: Sequence[str]
) -> Any:
    """``"all"``, or the explicit sectors a principal may act on."""
    if sees_all:
        return "all" if any(principal.has_any_scope(cap) for cap in capabilities) else []
    return sorted(sector for sector in sectors if _can(principal, capabilities, sector))


def _scope_split(
    principal: Any,
    capabilities: Sequence[str],
    *,
    sees_all: bool,
    sectors: Sequence[str],
) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """``(decidable, undecidable)`` sector lists for one verb.

    ``None`` means *every* sector (an unrestricted caller); a list is exactly
    the sectors named. The pair is what makes a ``needs_me`` filter expressible
    in SQL at all: "awaiting a decision I may make" is a state filter *and* a
    sector filter, and the complement is the union of the two halves.
    """
    if sees_all:
        holds = any(principal.has_any_scope(cap) for cap in capabilities)
        return (None, None) if holds else ([], None)
    decidable = [sector for sector in sectors if _can(principal, capabilities, sector)]
    undecidable = [sector for sector in sectors if sector not in set(decidable)]
    return decidable, undecidable


def _narrow(visible: Optional[Sequence[str]], subset: Optional[Sequence[str]]) -> Optional[List[str]]:
    """``visible ∩ subset`` where ``None`` on either side means "all"."""
    if subset is None:
        return None if visible is None else sorted(visible)
    if visible is None:
        return sorted(subset)
    allowed = set(subset)
    return sorted(sector for sector in visible if sector in allowed)


def me(*, principal: Any, sees_all: bool, sectors: Sequence[str]) -> Dict[str, Any]:
    """``GET /v1/me`` — who the key is, what it may read, what it may decide.

    Identity comes from the verified credential alone: **no** ``config/users.json``
    lookup, no filesystem identity, no secret. A display name is reported only
    if the token itself carries one; otherwise the field is omitted rather than
    invented from a username.
    """
    body: Dict[str, Any] = {
        "subject": getattr(principal, "subject", None),
        "is_root": bool(getattr(principal, "is_root", False)),
        "is_legacy_token": bool(getattr(principal, "is_legacy", False)),
        "sectors": {"all": bool(sees_all), "values": sorted(sectors)},
        "capabilities": {
            "can_approve_sectors": _capability_scope(
                principal, CAPSULE_DECIDING, sees_all=sees_all, sectors=sectors
            ),
            "can_create_sectors": _capability_scope(
                principal, TICKET_DECIDING, sees_all=sees_all, sectors=sectors
            ),
            "can_decide_gate_sectors": _capability_scope(
                principal, GATE_DECIDING, sees_all=sees_all, sectors=sectors
            ),
        },
    }
    display_name = getattr(principal, "display_name", None)
    if isinstance(display_name, str) and display_name.strip():
        body["display_name"] = display_name.strip()
    return envelope(source=SOURCE_PRINCIPAL, **body)


# --------------------------------------------------------------------------- #
# attention items
# --------------------------------------------------------------------------- #


def _twin(body: Dict[str, Any], code: str, **params: Any) -> Dict[str, Any]:
    """Attach the ``(arabic, english)`` twin of one message code to a payload."""
    ar, en = messages.pair(code, **params)
    body["reason"], body["reason_en"] = ar, en
    return body


def _capsule_can_act(principal: Any, domain: Optional[str], kind: str) -> bool:
    """Can *this* credential decide *this* capsule item, through ``/v1``?

    A halted capsule and an over-cap rework position have no ``/v1`` decision
    route at all (they are desktop human-reset positions), so no scope can make
    them actionable from the phone; the remedy says so instead.
    """
    if kind != messages.KIND_REQUIRES_DECISION:
        return False
    return _can(principal, CAPSULE_DECIDING, domain)


def _capsule_item(
    row: Dict[str, Any],
    *,
    kind: str,
    principal: Any,
    cap: int,
    now: datetime,
) -> Dict[str, Any]:
    """One capsule attention item, from indexed common fields only."""
    domain = row.get("domain")
    state = row.get("state")
    revisions = row.get("revision_count")
    if not isinstance(revisions, int) or isinstance(revisions, bool):
        revisions = None
    stage_ar, stage_en = messages.stage_label(state)
    item: Dict[str, Any] = {
        "kind": kind,
        "entity": {"kind": "capsule", "id": row.get("capsule_id")},
        "domain": domain,
        "stage": messages.stage_key(state),
        "stage_label": stage_ar,
        "stage_label_en": stage_en,
        "title": messages.text(f"title.{kind}.capsule"),
        "title_en": messages.text_en(f"title.{kind}.capsule"),
        "age_seconds": _age_seconds(row.get("updated_at"), now),
    }
    if revisions is not None:
        item["revision_count"] = revisions
        item["revision_cap"] = int(cap)
        summary = messages.pair(
            "summary.capsule",
            domain=domain,
            stage=stage_ar,
            revision_count=revisions,
            revision_cap=cap,
        )
    else:
        summary = messages.pair(
            "summary.capsule_no_revisions", domain=domain, stage=stage_ar
        )
    item["summary"], item["summary_en"] = summary[0], summary[1]
    if row.get("active_gate_authority"):
        item["approver"] = row.get("active_gate_authority")

    if kind == messages.KIND_SYSTEM_HALT:
        stored = row.get("last_reason")
        if isinstance(stored, str) and stored.strip():
            _twin(item, "reason.system_halt.capsule", state=state, last_reason=stored.strip())
        else:
            _twin(item, "reason.system_halt.capsule_no_reason", state=state)
    elif kind == messages.KIND_REVISION_CAP:
        _twin(
            item,
            "reason.revision_cap.capsule",
            revision_count=revisions,
            revision_cap=cap,
        )
    else:
        _twin(item, "reason.requires_decision.capsule", state=state)

    can_act = _capsule_can_act(principal, domain, kind)
    item["can_act"] = can_act
    if can_act:
        remedy_code = "remedy.review_deliverable"
    elif kind == messages.KIND_REQUIRES_DECISION:
        # Decidable in principle, but not by *this* key: name the real remedy
        # rather than sending the operator to the desktop for a phone decision.
        remedy_code = "remedy.need_scoped_key"
    else:
        remedy_code = "remedy.desktop_human_reset"
    ar, en = messages.pair(remedy_code)
    item["remedy"], item["remedy_en"] = ar, en
    if can_act:
        item["action"] = {
            "method": "POST",
            "path": "/v1/decisions",
            "body": {"capsule_id": row.get("capsule_id")},
            "capabilities": [
                name for name in CAPSULE_DECIDING if principal.has(name, resource=domain)
            ],
        }
    return item


def _ticket_item(row: Dict[str, Any], *, principal: Any, now: datetime) -> Dict[str, Any]:
    """One ticket attention item (a request awaiting accept / send-back)."""
    sector = row.get("sector")
    stage = row.get("stage")
    state = row.get("state")
    title_ar, title_en = messages.pair("title.requires_decision.ticket")
    item: Dict[str, Any] = {
        "kind": messages.KIND_REQUIRES_DECISION,
        "entity": {"kind": "ticket", "id": row.get("ticket_id")},
        "domain": sector,
        "stage": stage,
        "title": title_ar,
        "title_en": title_en,
        "age_seconds": _age_seconds(row.get("created_at"), now),
    }
    summary = messages.pair("summary.ticket", sector=sector, stage=stage)
    item["summary"], item["summary_en"] = summary[0], summary[1]
    _twin(item, "reason.requires_decision.ticket", stage=stage, state=state)
    ar, en = messages.pair("remedy.review_ticket")
    item["remedy"], item["remedy_en"] = ar, en
    can_act = _can(principal, TICKET_DECIDING, sector)
    item["can_act"] = can_act
    if can_act:
        item["action"] = {
            "method": "POST",
            "path": f"/v1/tickets/{row.get('ticket_id')}/decision",
            "capabilities": [
                name for name in TICKET_DECIDING if principal.has(name, resource=sector)
            ],
        }
    return item


def _gate_item(entry: Dict[str, Any], *, principal: Any, now: datetime) -> Dict[str, Any]:
    """One open gate-queue entry awaiting a human decision."""
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    sector = source.get("sector")
    risk = entry.get("risk_tier")
    gate_id = entry.get("gate_id")
    title_ar, title_en = messages.pair("title.requires_decision.gate")
    item: Dict[str, Any] = {
        "kind": messages.KIND_REQUIRES_DECISION,
        "entity": {"kind": "gate", "id": gate_id},
        "domain": sector,
        "title": title_ar,
        "title_en": title_en,
        "age_seconds": _age_seconds(entry.get("opened_at"), now),
        "risk_tier": risk,
    }
    summary = messages.pair("summary.gate", sector=sector, risk_tier=risk)
    item["summary"], item["summary_en"] = summary[0], summary[1]
    _twin(item, "reason.requires_decision.gate", opened_at=entry.get("opened_at"))
    ar, en = messages.pair("remedy.review_gate")
    item["remedy"], item["remedy_en"] = ar, en
    can_act = _can(principal, GATE_DECIDING, sector)
    item["can_act"] = can_act
    if can_act:
        item["action"] = {
            "method": "POST",
            "path": f"/v1/gates/{gate_id}/decision",
            "capabilities": [
                name for name in GATE_DECIDING if principal.has(name, resource=sector)
            ],
        }
    return item


def _quarantined_item(entry: Dict[str, Any], *, now: datetime) -> Dict[str, Any]:
    """One vault record the last index build refused to read (fail visible).

    Its sector is *unknown* — that is why it was refused — so it carries no
    ``domain`` and is shown only to a caller who may see every sector. Guessing
    a sector from an unvalidated file would be an access decision taken from the
    very record the engine declined to trust.
    """
    kind = str(entry.get("kind") or "record")
    code = str(entry.get("code") or "unreadable")
    title_ar, title_en = messages.pair("title.quarantined.record")
    item: Dict[str, Any] = {
        "kind": messages.KIND_QUARANTINED,
        "entity": {"kind": kind, "id": entry.get("name")},
        "domain": None,
        "title": title_ar,
        "title_en": title_en,
        "age_seconds": None,
        "quarantine_code": code,
    }
    summary = messages.pair("summary.quarantined", kind=kind, name=entry.get("name"))
    item["summary"], item["summary_en"] = summary[0], summary[1]
    _twin(item, f"reason.quarantined.{code}")
    ar, en = messages.pair("remedy.desktop_repair")
    item["remedy"], item["remedy_en"] = ar, en
    item["can_act"] = False
    return item


def _quarantined_items(
    sectors: Optional[Sequence[str]], *, now: datetime
) -> List[Dict[str, Any]]:
    """Quarantined records visible to this caller (sees-all callers only)."""
    if sectors is not None:
        return []
    return [
        _quarantined_item(entry, now=now) for entry in _quarantined_rows()
    ]


# --------------------------------------------------------------------------- #
# the entity streams that feed attention
# --------------------------------------------------------------------------- #


def _read_gate_entries(sectors: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    """Every *open* gate entry the caller may see (one bounded directory scan)."""
    gate_dir = getattr(_api(), "GATE_QUEUE_DIR", None)
    entries: List[Dict[str, Any]] = []
    if not gate_dir or not Path(gate_dir).is_dir():
        return entries
    for path in sorted(Path(gate_dir).glob("gate_*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("state") != "open":
            continue
        source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
        sector = source.get("sector")
        if sectors is not None and sector not in set(sectors):
            continue
        entries.append(entry)
    return entries


def _stream_items(
    rows: Sequence[Dict[str, Any]],
    *,
    builder: Any,
    at_key: str,
    prefix: str,
    id_key: str,
) -> List[Dict[str, Any]]:
    """Attach the private ``_at`` / ``_id`` sort key a merged page needs."""
    items: List[Dict[str, Any]] = []
    for row in rows:
        item = builder(row)
        at = row.get(at_key)
        item["_at"] = at if isinstance(at, str) else None
        item["_id"] = f"{prefix}{row.get(id_key)}"
        items.append(item)
    return items


def _capsule_stream(
    domains: Optional[Sequence[str]],
    *,
    states: Optional[Sequence[str]] = None,
    states_not: Optional[Sequence[str]] = None,
    state: Optional[str] = None,
    needs_human_only: bool = False,
    kind: str,
    principal: Any,
    cap: int,
    now: datetime,
    limit: int,
    after: Optional[Tuple[Optional[str], str]] = None,
    ascending: bool = False,
) -> Tuple[List[Dict[str, Any]], str]:
    rows, source = _capsule_rows(
        domains,
        states=states,
        states_not=states_not,
        state=state,
        needs_human_only=needs_human_only,
        cap=cap,
        after=after,
        cursor_prefix=CAPSULE_PREFIX,
        ascending=ascending,
        limit=limit,
    )
    items = _stream_items(
        rows,
        builder=lambda row: _capsule_item(
            row, kind=kind, principal=principal, cap=cap, now=now
        ),
        at_key="updated_at",
        prefix=CAPSULE_PREFIX,
        id_key="capsule_id",
    )
    return items, source


def _ticket_stream(
    sectors: Optional[Sequence[str]],
    *,
    principal: Any,
    now: datetime,
    limit: int,
    after: Optional[Tuple[Optional[str], str]] = None,
    ascending: bool = False,
) -> Tuple[List[Dict[str, Any]], str]:
    """Tickets awaiting a decision: scratch stage, or at a human gate / rework.

    Two bounded queries (the two spellings of "awaiting") merged and de-duped by
    ticket id — a ticket is one row, never two.
    """
    rows, source = _ticket_rows(
        sectors,
        stage=TICKET_AWAITING_STAGE,
        after=after,
        cursor_prefix=TICKET_PREFIX,
        ascending=ascending,
        limit=limit,
    )
    more_rows, _source = _ticket_rows(
        sectors,
        states=TICKET_AWAITING_STATES,
        after=after,
        cursor_prefix=TICKET_PREFIX,
        ascending=ascending,
        limit=limit,
    )
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for row in list(rows) + list(more_rows):
        ticket_id = row.get("ticket_id")
        if ticket_id in seen or row.get("stage") == TICKET_ARCHIVE_STAGE:
            continue
        seen.add(ticket_id)
        unique.append(row)
    items = _stream_items(
        unique,
        builder=lambda row: _ticket_item(row, principal=principal, now=now),
        at_key="created_at",
        prefix=TICKET_PREFIX,
        id_key="ticket_id",
    )
    return items, source


def _gate_stream(
    sectors: Optional[Sequence[str]], *, principal: Any, now: datetime
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for entry in _read_gate_entries(sectors):
        item = _gate_item(entry, principal=principal, now=now)
        opened = entry.get("opened_at")
        item["_at"] = opened if isinstance(opened, str) else None
        item["_id"] = f"{GATE_PREFIX}{entry.get('gate_id')}"
        items.append(item)
    return items


def _attention_candidates(
    sectors: Optional[Sequence[str]],
    *,
    principal: Any,
    cap: int,
    now: datetime,
    limit: int,
    after: Optional[Tuple[Optional[str], str]] = None,
    ascending: bool = False,
    with_quarantined: bool = True,
) -> Tuple[List[Dict[str, Any]], str]:
    """Every attention stream, merged into one candidate list.

    Each index-backed stream is fetched with a bounded ``LIMIT`` — and, when a
    cursor is in play, with the keyset condition applied *in SQL* — so paging
    never scans rows it will not return. The gate queue is the one file-backed
    stream (bounded by the number of open gates).
    """
    items: List[Dict[str, Any]] = []
    gate_items = _gate_stream(sectors, principal=principal, now=now)
    capsule_items, source = _capsule_stream(
        sectors,
        states=GATE_STATES,
        kind=messages.KIND_REQUIRES_DECISION,
        principal=principal,
        cap=cap,
        now=now,
        limit=limit + 1,
        after=after,
        ascending=ascending,
    )
    items += capsule_items
    over_cap, _ = _capsule_stream(
        sectors,
        state="needs_revision",
        needs_human_only=True,
        kind=messages.KIND_REVISION_CAP,
        principal=principal,
        cap=cap,
        now=now,
        limit=limit + 1,
        after=after,
        ascending=ascending,
    )
    items += over_cap
    halted, _ = _capsule_stream(
        sectors,
        states=("failed",),
        kind=messages.KIND_SYSTEM_HALT,
        principal=principal,
        cap=cap,
        now=now,
        limit=limit + 1,
        after=after,
        ascending=ascending,
    )
    items += halted
    ticket_items, _ = _ticket_stream(
        sectors, principal=principal, now=now, limit=limit + 1, after=after,
        ascending=ascending,
    )
    items += ticket_items
    items += gate_items
    if with_quarantined:
        for item in _quarantined_items(sectors, now=now):
            item["_at"] = None
            item["_id"] = f"{QUARANTINED_PREFIX}{item['entity'].get('kind')}:{item['entity'].get('id')}"
            items.append(item)
    return items, source


def _sort_key(item: Dict[str, Any]) -> Tuple[bool, str, str]:
    return (
        item.get("_at") is not None,
        item.get("_at") or "",
        item.get("_id") or "",
    )


def _strip_private(item: Dict[str, Any]) -> Dict[str, Any]:
    body = dict(item)
    body.pop("_at", None)
    body.pop("_id", None)
    return body


def _page_merged(
    items: Sequence[Dict[str, Any]],
    *,
    limit: int,
    after: Optional[Tuple[Optional[str], str]],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Filter by the cursor, sort newest-first, cut exactly one page.

    A row is kept only when it sorts strictly after the cursor position, applied
    here with the same predicate the per-stream SQL used, so no row can appear
    twice across pages or be skipped while it was present for the whole walk.
    """
    candidates = [
        item
        for item in items
        if read_index.keyset_keep(item.get("_at"), item.get("_id"), after)
    ]
    candidates.sort(key=_sort_key, reverse=True)
    window = candidates[:limit]
    next_cursor = None
    if len(candidates) > limit and window:
        next_cursor = encode_cursor((window[-1].get("_at"), window[-1].get("_id")))
    return [_strip_private(item) for item in window], next_cursor


def _oldest(
    items: Sequence[Dict[str, Any]], *, limit: int
) -> List[Dict[str, Any]]:
    """The oldest items overall — an unknown age is never the oldest."""
    candidates = [item for item in items if item.get("_at")]
    candidates.sort(key=_sort_key)
    return [_strip_private(item) for item in candidates[:limit]]


def attention(
    *,
    principal: Any,
    sectors: Optional[Sequence[str]],
    limit: int = DEFAULT_LIMIT,
    after: Optional[Tuple[Optional[str], str]] = None,
) -> Dict[str, Any]:
    """``GET /v1/attention`` — everything that needs a human, paged."""
    now = _utc_now()
    cap = _revision_cap()
    items, source = _attention_candidates(
        sectors, principal=principal, cap=cap, now=now, limit=limit, after=after
    )
    window, next_cursor = _page_merged(items, limit=limit, after=after)
    return envelope(
        source=source,
        **page(window, limit=limit, next_cursor=next_cursor),
        kinds=list(messages.KINDS),
        source_detail={
            "capsules": source,
            "tickets": source,
            "gates": SOURCE_FILES,
        },
    )


# --------------------------------------------------------------------------- #
# overview
# --------------------------------------------------------------------------- #


def _stage_totals(
    rollup: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Pipeline counts per stage, plus any state the stage table does not know.

    Nothing is dropped silently: an unlisted state is reported in
    ``unknown_states`` *and* folded into the ``unknown`` stage count, so the
    pipeline total always equals the number of capsules actually counted.
    """
    per_stage: Dict[str, int] = {key: 0 for key in messages.STAGE_KEYS}
    unknown: Dict[str, int] = {}
    for row in rollup.get("capsules") or []:
        state = row.get("state")
        key = messages.stage_key(state)
        count = int(row.get("count") or 0)
        per_stage[key] = per_stage.get(key, 0) + count
        if key == messages.STAGE_UNKNOWN.key:
            unknown[str(state)] = unknown.get(str(state), 0) + count
    stages = [
        {**stage, "count": per_stage.get(stage["stage"], 0)}
        for stage in messages.stages_payload()
    ]
    return stages, [
        {"state": state, "count": count}
        for state, count in sorted(unknown.items(), key=lambda kv: kv[0])
    ]


def _rollup_kinds(rollup: Dict[str, Any]) -> Dict[str, int]:
    """The rollup's own attention buckets, summed (exact counts, not page sizes)."""
    totals = {"at_gate": 0, "halted": 0, "over_cap": 0}
    for row in rollup.get("attention") or []:
        kind = row.get("kind")
        if kind in totals:
            totals[kind] += int(row.get("count") or 0)
    return totals


def _rollup_kinds_by_domain(rollup: Dict[str, Any], kind: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rollup.get("attention") or []:
        if row.get("kind") == kind:
            out[row.get("domain")] = out.get(row.get("domain"), 0) + int(row.get("count") or 0)
    return out


def _tickets_awaiting_by_sector(rollup: Dict[str, Any]) -> Dict[str, int]:
    """Ticket counts awaiting a decision, per sector — from the rollup's counts."""
    out: Dict[str, int] = {}
    for row in rollup.get("tickets") or []:
        if ticket_awaiting(row.get("stage"), row.get("state")):
            sector = row.get("sector")
            out[sector] = out.get(sector, 0) + int(row.get("count") or 0)
    return out


def _tickets_open_total(rollup: Dict[str, Any]) -> int:
    return sum(
        int(row.get("count") or 0)
        for row in rollup.get("tickets") or []
        if row.get("stage") != TICKET_ARCHIVE_STAGE
        and row.get("state") not in TICKET_TERMINAL
    )


def _domain_tiles(
    *,
    rollup: Dict[str, Any],
    sectors: Optional[Sequence[str]],
    principal: Any,
    gate_counts: Dict[str, int],
) -> List[Dict[str, Any]]:
    """One tile per visible sector: stages, open tickets, needs-me, today's cost.

    ``today_cost_usd`` is the index's capsule-level cost estimate for capsules
    last updated today, and it says so (``cost_source``); the engine's real
    day-by-day spend lives in the finance ledger, which this slice deliberately
    does not read. Nothing is zero-filled — what the data does not have (a
    monthly figure, for instance) is simply absent from the payload.
    """
    at_gate = _rollup_kinds_by_domain(rollup, "at_gate")
    awaiting_tickets = _tickets_awaiting_by_sector(rollup)
    cost_today = {
        row.get("domain"): float(row.get("cost_usd") or 0.0)
        for row in rollup.get("today_cost") or []
    }

    domains: List[str] = []
    for value in list(sectors or []):
        domains.append(value)
    for family in ("capsules", "attention", "today_cost"):
        for row in rollup.get(family) or []:
            value = row.get("domain")
            if isinstance(value, str) and value:
                domains.append(value)
    for row in rollup.get("tickets") or []:
        value = row.get("sector")
        if isinstance(value, str) and value:
            domains.append(value)
    for value in gate_counts:
        if isinstance(value, str) and value:
            domains.append(value)

    ordered: List[str] = []
    for value in domains:
        if value not in ordered and (sectors is None or value in set(sectors)):
            ordered.append(value)

    tiles: List[Dict[str, Any]] = []
    for domain in ordered:
        stage_counts: Dict[str, int] = {key: 0 for key in messages.STAGE_KEYS}
        capsules_total = 0
        for row in rollup.get("capsules") or []:
            if row.get("domain") != domain:
                continue
            count = int(row.get("count") or 0)
            stage_counts[messages.stage_key(row.get("state"))] += count
            capsules_total += count
        tickets_open = sum(
            int(row.get("count") or 0)
            for row in rollup.get("tickets") or []
            if row.get("sector") == domain
            and row.get("stage") != TICKET_ARCHIVE_STAGE
            and row.get("state") not in TICKET_TERMINAL
        )
        can_capsule = _can(principal, CAPSULE_DECIDING, domain)
        can_ticket = _can(principal, TICKET_DECIDING, domain)
        can_gate = _can(principal, GATE_DECIDING, domain)
        needs_me = 0
        if can_capsule:
            needs_me += int(at_gate.get(domain, 0))
        if can_ticket:
            needs_me += int(awaiting_tickets.get(domain, 0))
        if can_gate:
            needs_me += int(gate_counts.get(domain, 0))
        tiles.append(
            {
                "domain": domain,
                "capsules_total": capsules_total,
                "capsules_by_stage": stage_counts,
                "tickets_open": tickets_open,
                "needs_me": needs_me,
                "today_cost_usd": round(cost_today.get(domain, 0.0), 4),
                "cost_source": "capsules.cost_usd by updated_at (index)",
                "can_act": {
                    "capsules": bool(can_capsule),
                    "tickets": bool(can_ticket),
                    "gates": bool(can_gate),
                },
            }
        )
    return tiles


def overview(*, principal: Any, sectors: Optional[Sequence[str]]) -> Dict[str, Any]:
    """``GET /v1/overview`` — one call for the mobile home.

    Counts come from aggregate statements, never from the size of a page, so the
    headline numbers stay exact at any vault size. There is deliberately **no**
    company finance block here (caps, kill switch, subscriptions): that data is
    not part of the sector-scoped mobile slice.
    """
    now = _utc_now()
    today = now.date().isoformat()
    cap = _revision_cap()
    rollup, source = _rollup_rows(sectors, day=today, cap=cap)
    buckets = _rollup_kinds(rollup)
    gate_items = _gate_stream(sectors, principal=principal, now=now)
    quarantined = _quarantined_items(sectors, now=now)
    awaiting_tickets = sum(_tickets_awaiting_by_sector(rollup).values())

    counts = {
        messages.KIND_REQUIRES_DECISION: buckets["at_gate"]
        + awaiting_tickets
        + len(gate_items),
        messages.KIND_SYSTEM_HALT: buckets["halted"],
        messages.KIND_REVISION_CAP: buckets["over_cap"],
        messages.KIND_QUARANTINED: len(quarantined),
    }

    gate_counts: Dict[str, int] = {}
    for item in gate_items:
        domain = item.get("domain")
        gate_counts[domain] = gate_counts.get(domain, 0) + 1

    stages, unknown_states = _stage_totals(rollup)
    tiles = _domain_tiles(
        rollup=rollup, sectors=sectors, principal=principal, gate_counts=gate_counts
    )

    oldest_candidates, _ = _attention_candidates(
        sectors,
        principal=principal,
        cap=cap,
        now=now,
        limit=5,
        ascending=True,
        with_quarantined=False,
    )
    oldest = _oldest(
        [item for item in oldest_candidates if item.get("kind") == messages.KIND_REQUIRES_DECISION],
        limit=5,
    )

    decisions, decisions_source = _decision_rows(sectors, 10)
    recent = [
        {
            "at": row.get("at"),
            "actor": row.get("actor"),
            "decision": row.get("action"),
            "domain": row.get("domain"),
            "capsule_id": row.get("capsule_id"),
            "gate_id": row.get("gate_id"),
            "gate_type": row.get("gate_type"),
            "reason": row.get("reason"),
        }
        for row in decisions
    ]

    capsules_total = sum(int(row.get("count") or 0) for row in rollup.get("capsules") or [])
    today_cost = round(
        sum(float(row.get("cost_usd") or 0.0) for row in rollup.get("today_cost") or []), 4
    )
    return envelope(
        source=source,
        attention={"counts": counts, "oldest_requires_decision": oldest},
        pipeline={"stages": stages, "unknown_states": unknown_states},
        domains=tiles,
        totals={
            "capsules": capsules_total,
            "tickets_open": _tickets_open_total(rollup),
            "needs_me": sum(int(tile.get("needs_me") or 0) for tile in tiles),
            "today_cost_usd": today_cost,
        },
        recent_decisions=recent,
        # The list is the capsules' own attested decisions (approvals included),
        # projected into the index at rebuild time — not the feedback ledger.
        decisions_source="capsule_decisions",
        source_detail={
            "capsules": source,
            "tickets": source,
            "decisions": decisions_source,
            "gates": SOURCE_FILES,
        },
    )


# --------------------------------------------------------------------------- #
# capsules
# --------------------------------------------------------------------------- #


def _capsule_list_item(
    row: Dict[str, Any], *, principal: Any, cap: int, now: datetime
) -> Dict[str, Any]:
    """One work-list row, from indexed common fields only (no manifest read)."""
    state = row.get("state")
    domain = row.get("domain")
    revisions = row.get("revision_count")
    if not isinstance(revisions, int) or isinstance(revisions, bool):
        revisions = None
    stage_ar, stage_en = messages.stage_label(state)
    awaiting = state in GATE_STATES
    can_act = _can(principal, CAPSULE_DECIDING, domain)
    item: Dict[str, Any] = {
        "capsule_id": row.get("capsule_id"),
        "domain": domain,
        "state": state,
        "stage": messages.stage_key(state),
        "stage_label": stage_ar,
        "stage_label_en": stage_en,
        "awaiting_decision": awaiting,
        "needs_me": awaiting and can_act,
        "can_act": can_act,
        "revision_cap": int(cap),
        "has_image": bool(row.get("has_image")),
        "tokens": row.get("total_tokens"),
        "calls": row.get("calls"),
        "gates": {
            "total": row.get("gates_total"),
            "passed": row.get("gates_passed"),
            "active": row.get("active_gate"),
            "active_type": row.get("active_gate_type"),
        },
        "progress_percent": row.get("progress_percent"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "age_seconds": _age_seconds(row.get("updated_at"), now),
        "links": {
            "self": f"/v1/capsules/{row.get('capsule_id')}",
            "review": f"/v1/capsules/{row.get('capsule_id')}/review",
            "trace": f"/v1/capsules/{row.get('capsule_id')}/trace",
        },
    }
    if revisions is not None:
        item["revision_count"] = revisions
    if row.get("cost_usd") is not None:
        item["cost_usd"] = row.get("cost_usd")
    if row.get("active_gate_authority"):
        item["approver"] = row.get("active_gate_authority")
    return item


def _capsule_streams(
    *,
    principal: Any,
    sectors: Optional[Sequence[str]],
    domain: Optional[str],
    stage: Optional[str],
    state: Optional[str],
    needs_me: Optional[bool],
) -> List[Dict[str, Any]]:
    """The capsule filter(s) a request maps to — one, or two for ``needs_me=false``.

    ``needs_me`` is answered as *"this item needs ME"*: awaiting a human
    decision **and** decidable by this credential. That is expressible in SQL
    because the deciding capability is scoped per sector, so the filter is a
    state condition plus a sector condition. The complement (``needs_me=false``)
    is the union of its two halves, kept as two streams so the page stays exact
    and the cursor keeps its meaning.
    """
    visible = _narrow(sectors, None)
    base: Dict[str, Any] = {"domains": visible}
    if domain:
        base["domain"] = domain

    # The request's own filters, kept as a set so the needs-me restriction can
    # be *intersected* with them instead of overwriting them, and so that
    # ``stage`` and ``state`` compose: a state outside the named stage's bucket
    # is a request for nothing — never a licence to drop one of the two.
    wanted: Optional[set] = None
    unknown_stage = stage == messages.STAGE_UNKNOWN.key
    if state:
        wanted = {state}
    elif stage and not unknown_stage:
        wanted = set(messages.stage_states(stage))
    if state and stage:
        wanted = {state} & (
            set() if unknown_stage else set(messages.stage_states(stage))
        )

    def state_clause(states: set) -> Dict[str, Any]:
        if not states:
            return {"states": ()}  # matches nothing, never everything
        return {"states": tuple(sorted(states))}

    if unknown_stage and needs_me is not None:
        # Every gate state is a *known* state, so a "needs me" filter inside the
        # unknown bucket is provably empty — say so, in SQL.
        return [{**base, "states": ()}]
    if unknown_stage:
        return [{**base, "states_not": tuple(sorted(messages.STATE_TO_STAGE))}]
    if needs_me is None:
        return [base] if wanted is None else [{**base, **state_clause(wanted)}]

    decidable, undecidable = _scope_split(
        principal, CAPSULE_DECIDING, sees_all=sectors is None, sectors=list(sectors or [])
    )
    gate_states = set(GATE_STATES)
    if needs_me:
        allowed = gate_states if wanted is None else (wanted & gate_states)
        return [
            {**base, "domains": _narrow(sectors, decidable), **state_clause(allowed)}
        ]
    # needs_me=false: everything that is not (awaiting AND decidable) — the
    # union of "not awaiting" and "awaiting in a sector I cannot decide".
    streams: List[Dict[str, Any]] = []
    if wanted is None:
        # The raw complement, so a capsule in a state the stage table does not
        # know is still listed (it is certainly not awaiting a gate decision).
        streams.append({**base, "states_not": GATE_STATES})
        still_awaiting: set = gate_states
    else:
        if wanted - gate_states:
            streams.append({**base, **state_clause(wanted - gate_states)})
        still_awaiting = wanted & gate_states
    if still_awaiting:
        streams.append(
            {
                **base,
                "domains": _narrow(visible, undecidable),
                **state_clause(still_awaiting),
            }
        )
    return streams


def capsules(
    *,
    principal: Any,
    sectors: Optional[Sequence[str]],
    domain: Optional[str] = None,
    stage: Optional[str] = None,
    state: Optional[str] = None,
    needs_me: Optional[bool] = None,
    limit: int = DEFAULT_LIMIT,
    after: Optional[Tuple[Optional[str], str]] = None,
) -> Dict[str, Any]:
    """``GET /v1/capsules`` — the work list, paged and sector-scoped.

    The filter runs **in SQL**, before the limit, so walking the cursor neither
    duplicates nor skips: every row that is in the result set for the whole walk
    is returned exactly once.
    """
    now = _utc_now()
    cap = _revision_cap()
    streams = _capsule_streams(
        principal=principal,
        sectors=sectors,
        domain=domain,
        stage=stage,
        state=state,
        needs_me=needs_me,
    )
    items: List[Dict[str, Any]] = []
    source = SOURCE_FILES
    for stream in streams:
        # ``limit + 1``: one row past the page proves there is a next page, so a
        # walk never stops early and never invents a cursor for the last one.
        # (Each stream is keyset-filtered in SQL, so this is not a scan.)
        rows, source = _capsule_rows(
            stream.pop("domains"), after=after, cursor_prefix=CAPSULE_PREFIX,
            limit=limit + 1, **stream
        )
        items += _stream_items(
            rows,
            builder=lambda row: _capsule_list_item(row, principal=principal, cap=cap, now=now),
            at_key="updated_at",
            prefix=CAPSULE_PREFIX,
            id_key="capsule_id",
        )
    window, next_cursor = _page_merged(items, limit=limit, after=after)
    return envelope(
        source=source,
        **page(window, limit=limit, next_cursor=next_cursor),
        filters={"domain": domain, "stage": stage, "state": state, "needs_me": needs_me},
        order=["updated_at desc", "capsule_id desc"],
    )


def _gate_view(record: Any) -> Dict[str, Any]:
    """One gate of the plan, **with its bound artifact hashes**.

    ``GateRecord.to_view()`` deliberately omits ``artifacts``; the mobile detail
    needs them — they are the hashes the review endpoint reports for the gate
    being decided — so they are serialized here, in the ``/v1`` layer, without
    changing the engine's view helper.
    """
    return {
        "gate_id": record.gate_id,
        "gate_type": record.gate_type,
        "name": record.name,
        "status": record.status,
        "required": record.required,
        "passed": record.passed,
        "automated": record.automated,
        "human": record.human,
        "authority": record.authority
        or (CAPABILITY_CAPSULE_APPROVE if record.human else None),
        "decided_by": record.decided_by,
        "opened_at": record.opened_at,
        "closed_at": record.closed_at,
        # ``at`` is the key the mobile contract names: when the gate was decided
        # (closed_at) or, while it still waits, when it opened. Both source
        # fields travel alongside, so nothing is hidden behind the alias.
        "at": record.closed_at or record.opened_at,
        "reason": record.reason,
        "targets": list(record.targets),
        "artifacts": dict(record.artifacts),
    }


def _approval_chain(manifest: Dict[str, Any], records: Sequence[Any]) -> List[Dict[str, Any]]:
    """Who approved what: the decision ledger, plus gate records it does not hold.

    The manifest's bounded decision ledger is the attested record — actor,
    action, gate, time, reason, whether it was attested, the echoed element
    hashes and the digest of what was seen. A *legacy* decision (written before
    attestation existed) appears there with ``attested: false`` and no hashes,
    and a gate decided by a route that predates the ledger is recovered from its
    own record, so the chain still names who approved.
    """
    chain: List[Dict[str, Any]] = []
    covered: set = set()
    ledger = manifest.get("decisions")
    if isinstance(ledger, list):
        for record in ledger[-MAX_LEDGER_ENTRIES:]:
            if not isinstance(record, dict):
                continue
            gate_id = record.get("gate_id")
            if gate_id:
                covered.add(gate_id)
            chain.append(
                {
                    "source": "decision_ledger",
                    "actor": record.get("actor"),
                    "action": record.get("action"),
                    "gate": gate_id,
                    "gate_type": record.get("gate_type"),
                    "at": record.get("at"),
                    "reason": record.get("reason"),
                    "attested": bool(record.get("attested")),
                    "decision_id": record.get("decision_id"),
                    "reviewed_sha256": record.get("reviewed_sha256"),
                    "elements": dict(record.get("elements") or {}),
                    "state_before": record.get("state_before"),
                    "state_after": record.get("state_after"),
                    "final": bool(record.get("final")),
                }
            )
    for record in list(records)[:MAX_LEDGER_ENTRIES]:
        if not record.decided_by or record.gate_id in covered:
            continue
        evidence = dict(record.evidence) if isinstance(record.evidence, dict) else {}
        chain.append(
            {
                "source": "gate_record",
                "actor": record.decided_by,
                "action": "approve" if record.passed else "reject",
                "gate": record.gate_id,
                "gate_type": record.gate_type,
                "at": record.closed_at,
                "reason": record.reason,
                "attested": bool(evidence.get("attested")),
                "decision_id": evidence.get("decision_id"),
                "reviewed_sha256": evidence.get("reviewed_sha256"),
                "elements": dict(evidence.get("elements") or {}),
            }
        )
    chain.sort(key=lambda entry: (entry.get("at") or ""))
    return chain


def _outputs_projection(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Declared outputs by **element path** — never an inlined image blob.

    An image is referenced, not embedded: the app fetches it through the review
    endpoint, which is the same route that hashes it for a decision.
    """
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list):
        return []
    projection: List[Dict[str, Any]] = []
    for entry in outputs[:MAX_LEDGER_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        item = {
            key: entry.get(key)
            for key in ("path", "kind", "sha256", "media_type", "channels")
            if entry.get(key) is not None
        }
        if item:
            projection.append(item)
    return projection


def _receipts(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Publish receipts, when the manifest actually carries them."""
    receipts = manifest.get("receipts")
    if not isinstance(receipts, list):
        return []
    return [
        dict(entry) for entry in receipts[:MAX_LEDGER_ENTRIES] if isinstance(entry, dict)
    ]


def _capsule_detail_view(
    manifest: Dict[str, Any], *, principal: Any, cap: int, now: datetime
) -> Dict[str, Any]:
    """Shape one manifest into the mobile detail payload (read-only)."""
    from capsule_engine import gates as gate_engine

    capsule_id = manifest.get("capsule_id")
    state = manifest.get("state")
    domain = manifest.get("domain")
    revisions = manifest.get("revision_count")
    if not isinstance(revisions, int) or isinstance(revisions, bool) or revisions < 0:
        revisions = 0
    stage_ar, stage_en = messages.stage_label(state)

    records: Tuple[Any, ...] = ()
    gates_error: Optional[str] = None
    try:
        records = gate_engine.load_records(manifest)
    except gate_engine.GateError as exc:
        # A corrupt plan is reported honestly (and the rest of the detail still
        # answers); it is never rendered as "this capsule has no gates".
        gates_error = str(exc)

    plan = None
    if records:
        try:
            plan = gate_engine.progress(records)
        except gate_engine.GateError as exc:  # pragma: no cover - defensive
            gates_error = str(exc)

    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    accounting: Dict[str, Any] = {
        "tokens": {
            key: int(usage.get(key) or 0)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if key in usage
        },
        "calls": int(usage.get("calls") or 0),
        "source": "capsule metadata.usage (files)",
    }
    cost = metadata.get("estimated_cost_usd", usage.get("estimated_cost_usd"))
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        accounting["cost_usd"] = round(float(cost), 6)

    body: Dict[str, Any] = {
        "capsule_id": capsule_id,
        "domain": domain,
        "state": state,
        "stage": messages.stage_key(state),
        "stage_label": stage_ar,
        "stage_label_en": stage_en,
        "attempt": manifest.get("attempt"),
        "revision_count": revisions,
        "revision_cap": int(cap),
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "age_seconds": _age_seconds(manifest.get("updated_at"), now),
        "can_act": _can(principal, CAPSULE_DECIDING, domain),
        "gates": [_gate_view(record) for record in records],
        "gate_plan": plan,
        "approval_chain": _approval_chain(manifest, records),
        "accounting": accounting,
        "outputs": _outputs_projection(manifest),
        "receipts": _receipts(manifest),
        "links": {
            "self": f"/v1/capsules/{capsule_id}",
            "review": f"/v1/capsules/{capsule_id}/review",
            "trace": f"/v1/capsules/{capsule_id}/trace",
        },
    }
    if gates_error:
        body["gates_error"] = gates_error
    return body


def capsule_detail(capsule_id: str, *, principal: Any) -> Dict[str, Any]:
    """``GET /v1/capsules/{id}`` — one manifest, read once, nothing written.

    The router proves the manifest's *real* domain against the caller's sectors
    after this returns, so an invisible capsule is refused with the
    missing-capsule body and none of this payload leaves the server.
    """
    from capsule_engine.manifest import load_manifest

    clean_id = Path(capsule_id).name.strip()
    path = _api()._confined_capsule_path(clean_id)
    manifest = load_manifest(path)
    now = _utc_now()
    return envelope(
        source=SOURCE_FILES,
        **_capsule_detail_view(manifest, principal=principal, cap=_revision_cap(), now=now),
    )


def capsule_trace(
    capsule_id: str,
    *,
    sectors: Optional[Sequence[str]] = None,
    limit: int = DEFAULT_LIMIT,
    after: Optional[Tuple[Optional[str], str]] = None,
) -> Dict[str, Any]:
    """``GET /v1/capsules/{id}/trace`` — the hash-chained history as a timeline.

    The trace is append-only, so the cursor is the **hash of the last event
    returned**: a later page resumes exactly after it and an event appended
    meanwhile can never shift, duplicate or hide an earlier one. The chain is
    verified on read and the verdict travels with the timeline.
    """
    from capsule_engine import trace as trace_engine
    from capsule_engine.manifest import load_manifest

    clean_id = Path(capsule_id).name.strip()
    path = _api()._confined_capsule_path(clean_id)
    manifest = load_manifest(path)
    state = manifest.get("state")
    stage_ar, stage_en = messages.stage_label(state)

    read = trace_engine.read_trace(path, limit=None)
    events = [dict(event) for event in read.events]
    start = 0
    if after is not None:
        wanted = after[1]
        found: Optional[int] = None
        for index, event in enumerate(events):
            if event.get("hash") == wanted:
                found = index + 1
                break
        if found is None:
            raise V1Error(
                422,
                messages.text("error.cursor"),
                code="VALIDATION_ERROR",
                details={"cursor": wanted},
            )
        start = found
    window = events[start : start + limit]
    next_cursor = None
    if window and start + len(window) < len(events):
        next_cursor = encode_cursor((None, str(window[-1].get("hash"))))

    chain = trace_engine.verify_trace(path)
    timeline = [
        {
            "event": event.get("step_name") or event.get("event"),
            "by": event.get("gear_invoked") or event.get("actor"),
            "at": event.get("finished_at") or event.get("at") or event.get("started_at"),
            "why": event.get("reason") or event.get("status"),
            "status": event.get("status"),
            "hash": event.get("hash"),
            "prev": event.get("prev"),
        }
        for event in window
    ]
    return envelope(
        source=SOURCE_FILES,
        capsule_id=clean_id,
        domain=manifest.get("domain"),
        state=state,
        stage=messages.stage_key(state),
        stage_label=stage_ar,
        stage_label_en=stage_en,
        **page(timeline, limit=limit, next_cursor=next_cursor),
        malformed=read.malformed,
        chain={
            "ok": chain.ok,
            "events": chain.events,
            "first_bad_line": chain.first_bad_line,
            "detail": chain.detail,
        },
    )


# --------------------------------------------------------------------------- #
# tickets
# --------------------------------------------------------------------------- #


def ticket_awaiting(stage: Optional[str], state: Optional[str]) -> bool:
    """Does this ticket await a human decision? (accept/send-back or a gate)"""
    if stage == TICKET_ARCHIVE_STAGE:
        return False
    return stage == TICKET_AWAITING_STAGE or state in TICKET_AWAITING_STATES


def _ticket_list_item(
    row: Dict[str, Any], *, principal: Any, now: datetime
) -> Dict[str, Any]:
    """One request-list row, from indexed fields only (no file read)."""
    sector = row.get("sector")
    stage = row.get("stage")
    state = row.get("state")
    awaiting = ticket_awaiting(stage, state)
    can_act = _can(principal, TICKET_DECIDING, sector)
    return {
        "ticket_id": row.get("ticket_id"),
        "sector": sector,
        "stage": stage,
        "state": state,
        "title": row.get("title"),
        "awaiting_decision": awaiting,
        "needs_me": awaiting and can_act,
        "can_act": can_act,
        "settled": bool(row.get("settled")),
        "gate_id": row.get("gate_id"),
        "created_at": row.get("created_at"),
        "age_seconds": _age_seconds(row.get("created_at"), now),
        "links": {
            "self": f"/v1/tickets/{row.get('ticket_id')}",
            "review": f"/v1/tickets/{row.get('ticket_id')}/review",
        },
    }


def tickets(
    *,
    principal: Any,
    sectors: Optional[Sequence[str]],
    sector: Optional[str] = None,
    stage: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    after: Optional[Tuple[Optional[str], str]] = None,
) -> Dict[str, Any]:
    """``GET /v1/tickets`` — the request list, paged and sector-scoped."""
    now = _utc_now()
    # ``limit + 1``: the extra row is the proof that a next page exists (see
    # ``capsules``); without it the first page would look like the whole set.
    rows, source = _ticket_rows(
        sectors,
        sector=sector,
        stage=stage,
        state=state,
        after=after,
        cursor_prefix=TICKET_PREFIX,
        limit=limit + 1,
    )
    items = _stream_items(
        rows,
        builder=lambda row: _ticket_list_item(row, principal=principal, now=now),
        at_key="created_at",
        prefix=TICKET_PREFIX,
        id_key="ticket_id",
    )
    window, next_cursor = _page_merged(items, limit=limit, after=after)
    return envelope(
        source=source,
        **page(window, limit=limit, next_cursor=next_cursor),
        filters={"sector": sector, "stage": stage, "state": state},
        order=["created_at desc", "ticket_id desc"],
    )


def _ticket_history(entries: Any) -> List[Dict[str, Any]]:
    """The ticket's own steps, normalised to one shape (nothing invented).

    The vault writes ``from``/``to`` on creation and ``from_state``/``to_state``
    from the decision writers, so both spellings are read and reported as
    ``from``/``to`` — a display normalisation of fields that are really there.
    """
    if not isinstance(entries, list):
        return []
    steps: List[Dict[str, Any]] = []
    for entry in entries[-MAX_LEDGER_ENTRIES:]:
        if not isinstance(entry, dict):
            continue
        steps.append(
            {
                "at": entry.get("at"),
                "from": entry.get("from_state", entry.get("from")),
                "to": entry.get("to_state", entry.get("to")),
                "by": entry.get("by"),
                "why": entry.get("why"),
            }
        )
    return steps


def ticket_detail(ticket_id: str, *, principal: Any, sectors: Optional[Sequence[str]]) -> Dict[str, Any]:
    """``GET /v1/tickets/{id}`` — one ticket file, read once.

    The index supplies the path and sector, so the detail opens exactly one file
    instead of walking the sector; the file is then re-read as the truth and
    re-checked (id and sector) before anything is served. With the index off,
    the file-scan resolution runs instead — same filters, same answer.
    """
    clean_id = Path(ticket_id).name.strip()
    row: Optional[Dict[str, Any]] = None
    source = SOURCE_FILES
    if _served_by_index():
        row = read_index.query_ticket_by_id(clean_id, sectors=sectors)
        if row is not None:
            source = SOURCE_INDEX
    if row is None:
        row = read_index.query_ticket_by_id_from_files(clean_id, sectors=sectors)
    if row is None:
        raise V1Error(404, f"Ticket '{clean_id}' not found.", code="NOT_FOUND")

    path = Path(str(row.get("path") or ""))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise V1Error(404, f"Ticket '{clean_id}' not found.", code="NOT_FOUND") from exc
    if not isinstance(data, dict):
        raise V1Error(404, f"Ticket '{clean_id}' not found.", code="NOT_FOUND")

    # The file is the truth: if it names another id, the index row was stale and
    # this is a miss — never a licence to serve the wrong ticket.
    file_id = str(data.get("ticket_id") or data.get("id") or path.stem)
    if file_id != clean_id:
        raise V1Error(404, f"Ticket '{clean_id}' not found.", code="NOT_FOUND")
    sector = data.get("sector") or row.get("sector")
    if sectors is not None and sector not in set(sectors):
        raise V1Error(404, f"Ticket '{clean_id}' not found.", code="NOT_FOUND")

    now = _utc_now()
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    request = data.get("request") or data.get("content") or ""
    truncated = False
    if isinstance(request, str) and len(request) > TICKET_TEXT_CHARS:
        request = request[:TICKET_TEXT_CHARS]
        truncated = True
    stage = data.get("stage") or row.get("stage")
    state = data.get("state") or row.get("state")
    created_at = data.get("created_at") or metadata.get("timestamp")
    decisions = data.get("decisions")
    ledger = [entry for entry in decisions if isinstance(entry, dict)][-MAX_LEDGER_ENTRIES:] \
        if isinstance(decisions, list) else []

    return envelope(
        source=source,
        ticket_id=clean_id,
        sector=sector,
        stage=stage,
        state=state,
        title=data.get("title") or metadata.get("title"),
        awaiting_decision=ticket_awaiting(stage, state),
        can_act=_can(principal, TICKET_DECIDING, sector),
        created_at=created_at,
        age_seconds=_age_seconds(created_at, now),
        attempts=data.get("attempts"),
        gate_id=data.get("gate_id") or data.get("via") or metadata.get("gate_id"),
        request=request,
        request_truncated=truncated,
        steps=_ticket_history(data.get("history")),
        decisions=ledger,
        links={
            "self": f"/v1/tickets/{clean_id}",
            "review": f"/v1/tickets/{clean_id}/review",
        },
    )
