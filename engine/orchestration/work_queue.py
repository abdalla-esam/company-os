#!/usr/bin/env python3
"""work_queue.py — the durable priority queue: explicit work, ordered, restart-proof.

The pulse sweeps the spool on a clock; this module is the *explicit* channel
that sits beside it. A caller enqueues a unit of work with a priority, an
optional delay and a retry budget; a pulse pass (or the API, or a human at
the CLI) drains it. Everything is plain JSON files under one directory — the
filesystem is the queue: no broker, no daemon, no in-memory state. A process
death between two operations loses nothing but a lease, which the next drain
reclaims.

Layout under the queue root (default ``data/queue``, override with
``AGENCY_WORK_QUEUE_DIR``)::

    <root>/pending/<id>.json     waiting (may be delayed: next_attempt_at)
    <root>/claimed/<id>.json     leased to one drainer; lease_until bounds it
    <root>/done/<id>.json        completed (retained for audit, prunable)
    <root>/failed/<id>.json      attempts exhausted — dead letter
    <root>/cancelled/<id>.json   cancelled before completion
    <root>/quarantine/<state>/   unreadable — moved aside, never deleted

**Atomicity.** Every state change is an atomic rename inside one filesystem:
the winning claim is the rename that finds the pending file, and the loser
observes ``FileNotFoundError`` and moves on to the next candidate. Two pulses
(or two servers sharing the directory) therefore race safely with no lock and
no daemon. Content is patched *after* the move, so a crash between the two
steps leaves the item in the destination state with stale bookkeeping — never
two live copies.

**Priority.** Integer; higher runs first. Ties are FIFO by enqueue time.

**Retry.** A claim *is* an attempt and consumes one of ``max_attempts``; a
failed attempt returns the item to ``pending`` with exponential backoff
(``base * 2 ** (attempts - 1)``, capped) recorded in ``next_attempt_at``.
When attempts are exhausted the item moves to ``failed/`` — a dead letter for
human repair, never a silent drop. A lease that expires returns the item to
pending *without* refunding the attempt (the run may have partially
happened); :class:`QueueDefer` refunds it, because the handler proved nothing
happened.

**Cancellation is best-effort for in-flight work.** Cancelling a pending item
is final; cancelling a claimed item prevents every future attempt but cannot
un-run a handler that already started (its handle on the file is gone, so a
late ``complete``/``fail`` observes ``False`` and reports nothing).

**Policy-free.** The queue stores and moves items; what an item *means* is
the handler's business (``capsule_pulse`` registers ``capsule.advance``).

Tunables (env, all optional): ``AGENCY_QUEUE_LEASE_SECONDS`` (900),
``AGENCY_QUEUE_CLAIM_GRACE_SECONDS`` (300 — a claimed file with no lease and
an older mtime is reclaimable, covering a crash in the claim's patch
window), ``AGENCY_QUEUE_MAX_ATTEMPTS`` (3), ``AGENCY_QUEUE_BACKOFF_BASE``
(30 s), ``AGENCY_QUEUE_BACKOFF_MAX`` (3600 s), ``AGENCY_QUEUE_MAX_PENDING``
(1000), ``AGENCY_QUEUE_DRAIN_MAX`` (16 items per drain).

Standard library only; Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:  # normal import (repo root on sys.path)
    from shared_kernel.paths import WORK_QUEUE_DIR
except Exception:  # odd-cwd execution — mirror the module's own layout
    WORK_QUEUE_DIR = Path(__file__).resolve().parents[2] / "data" / "queue"

logger = logging.getLogger("orchestration.work_queue")

__all__ = [
    "ACTIVE_STATES",
    "STATES",
    "QueueDefer",
    "QueueError",
    "QueueFullError",
    "QueueItemError",
    "cancel",
    "claim",
    "complete",
    "defer",
    "drain",
    "enqueue",
    "fail",
    "get_item",
    "list_items",
    "prune",
    "queue_root",
    "reclaim_expired",
    "stats",
]

# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #

STATES: tuple[str, ...] = (
    "pending",
    "claimed",
    "done",
    "failed",
    "cancelled",
    "quarantine",
)
ACTIVE_STATES: tuple[str, ...] = ("pending", "claimed")
TERMINAL_STATES: tuple[str, ...] = ("done", "failed", "cancelled")

DEFAULT_LEASE_SECONDS = 900.0
DEFAULT_CLAIM_GRACE_SECONDS = 300.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 30.0
DEFAULT_BACKOFF_MAX_SECONDS = 3600.0
DEFAULT_MAX_PENDING = 1000
DEFAULT_DRAIN_MAX_ITEMS = 16

#: History is evidence, not a diary: keep the tail bounded.
MAX_HISTORY = 50
#: A payload is a pointer (capsule path, ticket id…), not a document store.
MAX_PAYLOAD_BYTES = 64 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_KIND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_MAX_KEY_CHARS = 200


class QueueError(Exception):
    """Base class for every queue refusal."""


class QueueFullError(QueueError):
    """The pending shelf is full — fail closed rather than grow the disk."""


class QueueItemError(QueueError):
    """A malformed item or a failed attempt (retried per policy)."""


class QueueDefer(QueueError):
    """Handler-controlled requeue: nothing happened, run again later.

    Conserves the attempt — for transient conditions (a lock held elsewhere,
    a rate limit) that must not consume the retry budget.
    """

    def __init__(self, retry_after_seconds: float, reason: str | None = None) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        self.reason = reason or "deferred by handler"
        super().__init__(self.reason)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dt(value: Any) -> datetime | None:
    """Parse a stored timestamp; naive values are read as UTC, junk as None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("work_queue: invalid %s=%r — using %s", name, raw, default)
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("work_queue: invalid %s=%r — using %s", name, raw, default)
        return default
    return value if value >= minimum else default


def queue_root(root: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the queue root (explicit argument wins over the env/default)."""
    return Path(root) if root is not None else WORK_QUEUE_DIR


def _ensure_layout(root: Path) -> None:
    for state in STATES:
        (root / state).mkdir(parents=True, exist_ok=True)


def _item_path(root: Path, state: str, item_id: str) -> Path:
    return root / state / f"{item_id}.json"


def _write_item(root: Path, state: str, item: Mapping[str, Any]) -> Path:
    """Atomically persist one item inside ``state`` (tmp + rename)."""
    directory = root / state
    directory.mkdir(parents=True, exist_ok=True)
    path = _item_path(root, state, str(item["id"]))
    tmp = path.with_name(f".{item['id']}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(dict(item), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return path


def _quarantine(path: Path, root: Path) -> None:
    """Move an unreadable item aside, preserving which state it came from."""
    target_dir = root / "quarantine" / path.parent.name
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        os.replace(path, target_dir / path.name)
        logger.warning("work_queue: quarantined unreadable item %s", path)
    except OSError as exc:  # never fatal: the item simply stays put
        logger.warning("work_queue: could not quarantine %s: %s", path, exc)


def _load_item(path: Path, *, root: Path, quarantine: bool = True) -> dict[str, Any] | None:
    """Read one item; the containing directory is the truth for ``state``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("work_queue: unreadable item %s: %s", path, exc)
        if quarantine and path.exists():
            _quarantine(path, root)
        return None
    if not isinstance(raw, dict) or not raw.get("id"):
        logger.warning("work_queue: malformed item %s", path)
        if quarantine and path.exists():
            _quarantine(path, root)
        return None
    raw["state"] = path.parent.name
    return raw


def _scan(root: Path, state: str, *, quarantine: bool = False) -> list[dict[str, Any]]:
    directory = root / state
    if not directory.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        item = _load_item(path, root=root, quarantine=quarantine)
        if item is not None:
            items.append(item)
    return items


def _append_history(
    item: Mapping[str, Any], stamp: datetime, event: str, detail: str | None = None
) -> list[dict[str, Any]]:
    history = list(item.get("history") or [])
    history.append({"at": _iso(stamp), "event": event, "detail": detail})
    return history[-MAX_HISTORY:]


def _active_sort_key(item: Mapping[str, Any]) -> tuple[int, str, str]:
    """Higher priority first, then FIFO by enqueue time, then id (stable)."""
    try:
        priority = int(item.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    return (-priority, str(item.get("enqueued_at") or ""), str(item.get("id") or ""))


def _archive_sort_key(item: Mapping[str, Any]) -> tuple[str, str]:
    return (str(item.get("updated_at") or item.get("enqueued_at") or ""), str(item.get("id") or ""))


def _due(item: Mapping[str, Any], now: datetime) -> bool:
    scheduled = _as_dt(item.get("next_attempt_at"))
    return scheduled is None or scheduled <= now


def _lease_expired(item: Mapping[str, Any], path: Path, now: datetime) -> bool:
    """True when a claimed item may be reclaimed.

    An explicit lease past its moment is definitive. A claimed file with no
    lease at all can only mean the process died between the claim's rename and
    its bookkeeping patch — reclaimable once the file is older than the grace
    period, so a crash never strands an item forever.
    """
    lease = _as_dt(item.get("lease_until"))
    if lease is not None:
        return lease < now
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    grace = _env_float("AGENCY_QUEUE_CLAIM_GRACE_SECONDS", DEFAULT_CLAIM_GRACE_SECONDS)
    return (now - mtime).total_seconds() > grace


def _validate_kind(kind: Any) -> str:
    if not isinstance(kind, str):
        raise QueueItemError("kind must be a string")
    kind = kind.strip()
    if not _KIND_RE.match(kind):
        raise QueueItemError(
            f"invalid kind {kind!r} (letters, digits, '.', '_', ':' or '-' required)"
        )
    return kind


def _validate_payload(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise QueueItemError("payload must be a JSON object")
    data = dict(payload)
    try:
        encoded = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise QueueItemError(f"payload is not JSON-serializable: {exc}") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise QueueItemError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    return data


def _summarize_result(result: Any) -> Any:
    """Store a compact, JSON-safe echo of a handler's return value."""
    if result is None:
        return None
    try:
        json.dumps(result)
    except (TypeError, ValueError):
        return str(result)[:500]
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > 4096:  # a result is a receipt, not a payload store
        return {"truncated": True, "preview": text[:4000]}
    return result


def _find_item(root: Path, item_id: str) -> dict[str, Any] | None:
    for state in STATES:
        path = _item_path(root, state, item_id)
        if path.is_file():
            item = _load_item(path, root=root)
            if item is not None:
                return item
    return None


def _find_active_by_key(root: Path, key: str) -> dict[str, Any] | None:
    for state in ACTIVE_STATES:
        for item in _scan(root, state):
            if item.get("idempotency_key") == key:
                return item
    return None


def backoff_seconds(attempts: int) -> float:
    """Exponential backoff for the *next* attempt after ``attempts`` tries."""
    base = _env_float("AGENCY_QUEUE_BACKOFF_BASE", DEFAULT_BACKOFF_BASE_SECONDS)
    cap = _env_float("AGENCY_QUEUE_BACKOFF_MAX", DEFAULT_BACKOFF_MAX_SECONDS)
    delay = base * (2 ** max(0, int(attempts) - 1))
    return float(min(delay, cap))


# --------------------------------------------------------------------------- #
# producers
# --------------------------------------------------------------------------- #

def enqueue(
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    priority: int = 0,
    delay_seconds: float = 0.0,
    max_attempts: int | None = None,
    idempotency_key: str | None = None,
    item_id: str | None = None,
    history_note: str | None = None,
    root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Put one unit of work on the queue and return the stored item.

    Refusals are deliberate, never silent: an invalid kind/payload, a full
    pending shelf (:class:`QueueFullError`) and a duplicate explicit id all
    raise :class:`QueueItemError`/:class:`QueueFullError` before anything is
    written. ``idempotency_key`` de-duplicates against *active* items only —
    once an item is terminal, the key is free again.
    """
    root_path = queue_root(root)
    _ensure_layout(root_path)
    kind = _validate_kind(kind)
    data = _validate_payload(payload)

    if isinstance(priority, bool) or not isinstance(priority, int):
        raise QueueItemError("priority must be an integer")
    if (
        isinstance(delay_seconds, bool)
        or not isinstance(delay_seconds, (int, float))
        or delay_seconds < 0
    ):
        raise QueueItemError("delay_seconds must be a non-negative number")
    if max_attempts is None:
        max_attempts = _env_int("AGENCY_QUEUE_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise QueueItemError("max_attempts must be an integer >= 1")
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise QueueItemError("idempotency_key must be a non-empty string")
        idempotency_key = idempotency_key.strip()[:_MAX_KEY_CHARS]

    if idempotency_key:
        existing = _find_active_by_key(root_path, idempotency_key)
        if existing is not None:
            return existing  # already scheduled: hand back the live item

    max_pending = _env_int("AGENCY_QUEUE_MAX_PENDING", DEFAULT_MAX_PENDING)
    if len(_scan(root_path, "pending")) >= max_pending:
        raise QueueFullError(
            f"queue pending shelf is full ({max_pending} items) — drain or cancel first"
        )

    if item_id is None:
        item_id = uuid.uuid4().hex
    elif not isinstance(item_id, str) or not _ID_RE.match(item_id):
        raise QueueItemError(f"invalid item id {item_id!r}")
    if _find_item(root_path, item_id) is not None:
        raise QueueItemError(f"item id {item_id!r} already exists")

    stamp = _now() if now is None else now
    item: dict[str, Any] = {
        "id": item_id,
        "kind": kind,
        "payload": data,
        "priority": priority,
        "state": "pending",
        "attempts": 0,
        "max_attempts": max_attempts,
        "enqueued_at": _iso(stamp),
        "updated_at": _iso(stamp),
        "next_attempt_at": _iso(stamp + timedelta(seconds=float(delay_seconds))),
        "claimed_by": None,
        "claimed_at": None,
        "lease_until": None,
        "idempotency_key": idempotency_key,
        "last_error": None,
        "result": None,
        "history": [{"at": _iso(stamp), "event": "enqueued", "detail": history_note}],
    }
    _write_item(root_path, "pending", item)
    return item


# --------------------------------------------------------------------------- #
# the claim
# --------------------------------------------------------------------------- #

def claim(
    *,
    worker_id: str | None = None,
    lease_seconds: float | None = None,
    kinds: Sequence[str] | None = None,
    item_id: str | None = None,
    root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Atomically claim the best due item, or ``None`` when nothing is due.

    Best = highest priority, then FIFO. Delayed items (``next_attempt_at`` in
    the future) are skipped, so backoff is enforced without a timer anywhere.
    The rename ``pending -> claimed`` is the arbitration: exactly one caller
    can win it for a given file, and everyone else gets ``FileNotFoundError``
    and tries the next candidate. With ``item_id`` the caller targets one
    specific item (still subject to its schedule).
    """
    root_path = queue_root(root)
    _ensure_layout(root_path)
    stamp = _now() if now is None else now
    lease = (
        float(lease_seconds)
        if lease_seconds is not None
        else _env_float("AGENCY_QUEUE_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)
    )
    if lease <= 0:
        lease = DEFAULT_LEASE_SECONDS
    if not isinstance(worker_id, str) or not worker_id.strip():
        worker_id = f"drainer:{os.getpid()}"
    wanted = {str(k) for k in kinds} if kinds else None

    if item_id is not None:
        candidates: list[str] = []
        path = _item_path(root_path, "pending", item_id)
        if path.is_file():
            item = _load_item(path, root=root_path)
            if (
                item is not None
                and (wanted is None or item.get("kind") in wanted)
                and _due(item, stamp)
            ):
                candidates = [item_id]
    else:
        candidates = [
            str(item["id"])
            for item in sorted(_scan(root_path, "pending", quarantine=True), key=_active_sort_key)
            if (wanted is None or item.get("kind") in wanted) and _due(item, stamp)
        ]

    for candidate_id in candidates:
        pending_path = _item_path(root_path, "pending", candidate_id)
        claimed_path = _item_path(root_path, "claimed", candidate_id)
        try:
            os.rename(pending_path, claimed_path)  # the atomic arbitration
        except FileNotFoundError:
            continue  # lost the race — another drainer owns it now
        except OSError as exc:
            logger.warning("work_queue: claim rename failed for %s: %s", candidate_id, exc)
            continue

        item = _load_item(claimed_path, root=root_path, quarantine=True)
        if item is None:
            continue  # unreadable: quarantined, try the next candidate
        attempts = int(item.get("attempts") or 0) + 1
        item.update(
            {
                "state": "claimed",
                "attempts": attempts,
                "claimed_by": worker_id,
                "claimed_at": _iso(stamp),
                "lease_until": _iso(stamp + timedelta(seconds=lease)),
                "updated_at": _iso(stamp),
            }
        )
        item["history"] = _append_history(
            item, stamp, "claimed", f"attempt {attempts}/{item.get('max_attempts')} by {worker_id}"
        )
        try:
            _write_item(root_path, "claimed", item)
        except OSError as exc:  # still ours; the lease just isn't recorded
            logger.warning("work_queue: claim bookkeeping not persisted for %s: %s", candidate_id, exc)
        return item
    return None


# --------------------------------------------------------------------------- #
# terminal moves
# --------------------------------------------------------------------------- #

def _move_claimed(root: Path, item_id: str, target_state: str) -> dict[str, Any] | None:
    """Move an item out of ``claimed`` (single atomic rename) and return it."""
    source = _item_path(root, "claimed", item_id)
    if not source.is_file():
        return None  # cancelled, reclaimed by a peer, or already finished
    item = _load_item(source, root=root)
    if item is None:
        return None
    try:
        os.replace(source, _item_path(root, target_state, item_id))
    except OSError as exc:
        logger.warning("work_queue: %s -> %s move failed for %s: %s", "claimed", target_state, item_id, exc)
        return None
    item["state"] = target_state
    return item


def complete(
    item_id: str,
    *,
    result: Any = None,
    root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> bool:
    """Mark a claimed item done. ``False`` = it was no longer ours to finish."""
    root_path = queue_root(root)
    _ensure_layout(root_path)
    stamp = _now() if now is None else now
    item = _move_claimed(root_path, item_id, "done")
    if item is None:
        return False
    item.update(
        {
            "result": _summarize_result(result),
            "lease_until": None,
            "updated_at": _iso(stamp),
        }
    )
    item["history"] = _append_history(item, stamp, "completed", None)
    try:
        _write_item(root_path, "done", item)
    except OSError as exc:
        logger.warning("work_queue: completion receipt not persisted for %s: %s", item_id, exc)
    return True


def fail(
    item_id: str,
    *,
    error: str,
    root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Record a failed attempt: retry with backoff, or dead-letter it.

    The attempt was consumed at claim time. Returns the final item (its
    ``state`` says whether it landed in ``pending`` again or in ``failed``),
    or ``None`` when the item was no longer claimed by us.
    """
    root_path = queue_root(root)
    _ensure_layout(root_path)
    stamp = _now() if now is None else now
    source = _item_path(root_path, "claimed", item_id)
    if not source.is_file():
        return None
    probe = _load_item(source, root=root_path)
    if probe is None:
        return None
    attempts = int(probe.get("attempts") or 0)
    max_attempts = int(probe.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
    exhausted = attempts >= max_attempts
    target = "failed" if exhausted else "pending"
    item = _move_claimed(root_path, item_id, target)
    if item is None:
        return None
    item.update(
        {
            "last_error": str(error)[:1000],
            "claimed_by": None,
            "claimed_at": None,
            "lease_until": None,
            "updated_at": _iso(stamp),
        }
    )
    if exhausted:
        item["history"] = _append_history(
            item, stamp, "dead_letter", f"{attempts}/{max_attempts} attempts: {str(error)[:200]}"
        )
    else:
        delay = backoff_seconds(attempts)
        item["next_attempt_at"] = _iso(stamp + timedelta(seconds=delay))
        item["history"] = _append_history(
            item, stamp, "retry", f"attempt {attempts}/{max_attempts} failed; retry in {delay:.0f}s"
        )
    try:
        _write_item(root_path, target, item)
    except OSError as exc:
        logger.warning("work_queue: failure receipt not persisted for %s: %s", item_id, exc)
    return item


def defer(
    item_id: str,
    *,
    retry_after_seconds: float,
    reason: str | None = None,
    root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a claimed item to pending *without* consuming its attempt."""
    root_path = queue_root(root)
    _ensure_layout(root_path)
    stamp = _now() if now is None else now
    delay = max(0.0, float(retry_after_seconds))
    item = _move_claimed(root_path, item_id, "pending")
    if item is None:
        return None
    item.update(
        {
            "attempts": max(0, int(item.get("attempts") or 0) - 1),  # refund: nothing ran
            "next_attempt_at": _iso(stamp + timedelta(seconds=delay)),
            "claimed_by": None,
            "claimed_at": None,
            "lease_until": None,
            "updated_at": _iso(stamp),
        }
    )
    item["history"] = _append_history(
        item, stamp, "deferred", f"{reason or 'deferred'}; retry in {delay:.0f}s"
    )
    try:
        _write_item(root_path, "pending", item)
    except OSError as exc:
        logger.warning("work_queue: defer receipt not persisted for %s: %s", item_id, exc)
    return item


def reclaim_expired(
    *,
    reason: str = "lease expired",
    root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return every expired lease to ``pending``; a crash strands nothing.

    The attempt stands (the run may have partially happened) — only
    :func:`defer` refunds one.
    """
    root_path = queue_root(root)
    _ensure_layout(root_path)
    stamp = _now() if now is None else now
    reclaimed: list[dict[str, Any]] = []
    claimed_dir = root_path / "claimed"
    if not claimed_dir.is_dir():
        return reclaimed
    for path in sorted(claimed_dir.glob("*.json")):
        item = _load_item(path, root=root_path)
        if item is None:
            continue  # quarantined
        if not _lease_expired(item, path, stamp):
            continue
        try:
            os.replace(path, _item_path(root_path, "pending", str(item["id"])))
        except OSError as exc:
            logger.warning("work_queue: reclaim failed for %s: %s", path, exc)
            continue
        item.update(
            {
                "state": "pending",
                "claimed_by": None,
                "claimed_at": None,
                "lease_until": None,
                "updated_at": _iso(stamp),
            }
        )
        item["history"] = _append_history(item, stamp, "lease_expired", f"{reason} — returned to pending")
        try:
            _write_item(root_path, "pending", item)
        except OSError as exc:
            logger.warning("work_queue: reclaim receipt not persisted for %s: %s", item.get("id"), exc)
        reclaimed.append(item)
    return reclaimed


def cancel(
    item_id: str,
    *,
    reason: str | None = None,
    root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Cancel a pending or claimed item. ``None`` = not found / already terminal."""
    if not isinstance(item_id, str) or not _ID_RE.match(item_id):
        return None
    root_path = queue_root(root)
    _ensure_layout(root_path)
    stamp = _now() if now is None else now
    for state in ACTIVE_STATES:
        source = _item_path(root_path, state, item_id)
        if not source.is_file():
            continue
        item = _load_item(source, root=root_path)
        if item is None:
            return None
        try:
            os.replace(source, _item_path(root_path, "cancelled", item_id))
        except OSError as exc:
            logger.warning("work_queue: cancel failed for %s: %s", item_id, exc)
            return None
        item.update(
            {
                "state": "cancelled",
                "claimed_by": None,
                "claimed_at": None,
                "lease_until": None,
                "updated_at": _iso(stamp),
            }
        )
        item["history"] = _append_history(item, stamp, "cancelled", reason or f"cancelled while {state}")
        try:
            _write_item(root_path, "cancelled", item)
        except OSError as exc:
            logger.warning("work_queue: cancel receipt not persisted for %s: %s", item_id, exc)
        return item
    return None


# --------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------- #

def get_item(item_id: str, *, root: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    """Fetch one item by id from any state, or ``None``."""
    return _find_item(queue_root(root), item_id)


def list_items(
    *,
    state: str = "active",
    kind: str | None = None,
    limit: int = 200,
    root: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """List items in one state (or ``active`` / ``all``), ordered for humans.

    Active states read best-first (priority, then FIFO); archives read
    newest-first. A pure read: unreadable files are reported, never moved.
    """
    root_path = queue_root(root)
    wanted: tuple[str, ...]
    if state in ("active", "*"):
        wanted = ACTIVE_STATES
    elif state == "all":
        wanted = STATES
    elif state in STATES:
        wanted = (state,)
    else:
        raise QueueItemError(f"unknown state {state!r} (one of: active, all, {', '.join(STATES)})")
    limit = max(1, int(limit))
    collected: list[dict[str, Any]] = []
    for one in wanted:
        items = _scan(root_path, one)
        if kind is not None:
            items = [item for item in items if item.get("kind") == kind]
        if one in ACTIVE_STATES:
            items.sort(key=_active_sort_key)
        else:
            items.sort(key=_archive_sort_key, reverse=True)
        collected.extend(items)
    return collected[:limit]


def stats(*, root: str | os.PathLike[str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Depth by state, by kind, due-vs-delayed, and lease health."""
    root_path = queue_root(root)
    stamp = _now() if now is None else now
    by_state = {state: _scan(root_path, state) for state in STATES}
    pending = by_state["pending"]
    claimed = by_state["claimed"]

    due = 0
    oldest: float | None = None
    by_kind: dict[str, dict[str, int]] = {}
    for item in pending:
        if _due(item, stamp):
            due += 1
        enqueued = _as_dt(item.get("enqueued_at"))
        if enqueued is not None:
            age = (stamp - enqueued).total_seconds()
            if oldest is None or age > oldest:
                oldest = age
        bucket = by_kind.setdefault(str(item.get("kind")), {"pending": 0, "claimed": 0})
        bucket["pending"] += 1
    expired = 0
    for item in claimed:
        bucket = by_kind.setdefault(str(item.get("kind")), {"pending": 0, "claimed": 0})
        bucket["claimed"] += 1
        path = _item_path(root_path, "claimed", str(item.get("id")))
        if _lease_expired(item, path, stamp):
            expired += 1

    return {
        "root": str(root_path),
        "counts": {state: len(items) for state, items in by_state.items()},
        "active": {"pending": len(pending), "claimed": len(claimed)},
        "due": due,
        "delayed": len(pending) - due,
        "expired_leases": expired,
        "oldest_pending_seconds": oldest,
        "by_kind": by_kind,
        "max_pending": _env_int("AGENCY_QUEUE_MAX_PENDING", DEFAULT_MAX_PENDING),
    }


def prune(
    *,
    keep: int = 500,
    max_age_seconds: float | None = None,
    root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Trim the terminal archives (done/failed/cancelled/quarantine).

    Keeps the newest ``keep`` items per archive and/or everything younger than
    ``max_age_seconds``. Terminal states only, explicit call only — active
    work is never touched.
    """
    root_path = queue_root(root)
    stamp = _now() if now is None else now
    keep = max(0, int(keep))
    removed: dict[str, int] = {}
    released_bytes = 0
    for state in ("done", "failed", "cancelled", "quarantine"):
        items = sorted(_scan(root_path, state), key=_archive_sort_key)
        doomed: set[str] = set()
        if len(items) > keep:
            doomed.update(str(item["id"]) for item in items[: len(items) - keep])
        if max_age_seconds is not None:
            for item in items:
                when = _as_dt(item.get("updated_at") or item.get("enqueued_at"))
                if when is not None and (stamp - when).total_seconds() > float(max_age_seconds):
                    doomed.add(str(item["id"]))
        count = 0
        for item_id in sorted(doomed):
            path = _item_path(root_path, state, item_id)
            try:
                released_bytes += path.stat().st_size
                path.unlink()
                count += 1
            except OSError as exc:
                logger.warning("work_queue: prune failed for %s: %s", path, exc)
        removed[state] = count
    return {"removed": removed, "released_bytes": released_bytes}


# --------------------------------------------------------------------------- #
# the drain
# --------------------------------------------------------------------------- #

def drain(
    handlers: Mapping[str, Callable[[dict[str, Any]], Any]],
    *,
    kinds: Sequence[str] | None = None,
    limit: int | None = None,
    lease_seconds: float | None = None,
    concurrency: int = 1,
    worker_id: str | None = None,
    reclaim: bool = True,
    root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim due items and run them through ``handlers`` until dry or capped.

    ``handlers`` maps a kind to its callable; only kinds present here are ever
    claimed, so the queue can hold work for another consumer without this
    drain eating it. A handler returning normally means success; raising
    anything means failure (:func:`fail` — retry with backoff, then dead
    letter); raising :class:`QueueDefer` returns the item to pending with a
    delay and no attempt consumed.

    Concurrency claims items one at a time from each worker thread — the
    atomic rename arbitrates, so two threads never run the same item. The
    budget (``limit`` / ``AGENCY_QUEUE_DRAIN_MAX``) caps one pass; leftover due
    items wait for the next drain, which is what keeps one busy queue from
    monopolizing a pulse.
    """
    root_path = queue_root(root)
    _ensure_layout(root_path)
    stamp = _now() if now is None else now
    if not handlers:
        raise QueueItemError("drain needs at least one handler")
    wanted = sorted({str(k) for k in (kinds if kinds is not None else handlers) if str(k) in handlers})
    if not wanted:
        raise QueueItemError("drain has no handler for the requested kinds")
    budget = int(limit) if limit is not None else _env_int("AGENCY_QUEUE_DRAIN_MAX", DEFAULT_DRAIN_MAX_ITEMS)
    budget = max(1, budget)
    pool_size = max(1, int(concurrency))
    prefix = worker_id if isinstance(worker_id, str) and worker_id.strip() else f"drainer:{os.getpid()}"

    reclaimed = reclaim_expired(root=root_path, now=stamp) if reclaim else []
    report: dict[str, Any] = {
        "root": str(root_path),
        "claimed": 0,
        "completed": 0,
        "retried": 0,
        "dead": 0,
        "deferred": 0,
        "reclaimed": len(reclaimed),
        "items": [],
        "errors": [],
    }
    lock = threading.Lock()
    claimed_total = 0

    def _one_item() -> None:
        nonlocal claimed_total
        while True:
            with lock:
                if claimed_total >= budget:
                    return
            item = claim(
                worker_id=f"{prefix}:{threading.get_ident()}",
                lease_seconds=lease_seconds,
                kinds=wanted,
                root=root_path,
                now=stamp,
            )
            if item is None:
                return
            with lock:
                claimed_total += 1
                report["claimed"] += 1
            item_id = str(item.get("id"))
            entry: dict[str, Any] = {
                "id": item_id,
                "kind": item.get("kind"),
                "attempts": item.get("attempts"),
                "priority": item.get("priority"),
            }
            handler = handlers.get(str(item.get("kind")))
            try:
                result = handler(item)  # type: ignore[misc]
            except QueueDefer as exc:
                defer(item_id, retry_after_seconds=exc.retry_after_seconds, reason=exc.reason, root=root_path, now=stamp)
                with lock:
                    report["deferred"] += 1
                    report["items"].append({**entry, "outcome": "deferred", "reason": exc.reason})
            except Exception as exc:  # a failed attempt, not a crash of the drain
                final = fail(item_id, error=f"{type(exc).__name__}: {exc}", root=root_path, now=stamp)
                outcome = "retry" if (final is not None and final.get("state") == "pending") else "dead"
                with lock:
                    report["retried" if outcome == "retry" else "dead"] += 1
                    report["items"].append({**entry, "outcome": outcome, "error": str(exc)[:300]})
            else:
                complete(item_id, result=result, root=root_path, now=stamp)
                with lock:
                    report["completed"] += 1
                    report["items"].append({**entry, "outcome": "done"})

    if pool_size <= 1 or budget == 1:
        _one_item()
    else:
        with ThreadPoolExecutor(
            max_workers=min(pool_size, budget), thread_name_prefix="queue-drain"
        ) as pool:
            list(pool.map(lambda _: _one_item(), range(min(pool_size, budget))))
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    """Ops surface: stats, list, enqueue, cancel, reclaim, prune — JSON out."""
    parser = argparse.ArgumentParser(
        prog="work_queue.py",
        description="Inspect and operate the durable work queue (data/queue).",
    )
    parser.add_argument("--root", default=None, help="queue root (default: AGENCY_WORK_QUEUE_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="depth by state/kind, due vs delayed, lease health")

    p_list = sub.add_parser("list", help="list items")
    p_list.add_argument("--state", default="active", help="active | all | pending | claimed | ...")
    p_list.add_argument("--kind", default=None)
    p_list.add_argument("--limit", type=int, default=50)

    p_enqueue = sub.add_parser("enqueue", help="schedule one item")
    p_enqueue.add_argument("--kind", required=True)
    p_enqueue.add_argument("--payload", default="{}", help="JSON object")
    p_enqueue.add_argument("--priority", type=int, default=0)
    p_enqueue.add_argument("--delay", type=float, default=0.0, help="seconds")
    p_enqueue.add_argument("--max-attempts", type=int, default=None)
    p_enqueue.add_argument("--key", default=None, help="idempotency key")

    p_cancel = sub.add_parser("cancel", help="cancel a pending/claimed item")
    p_cancel.add_argument("--id", required=True)
    p_cancel.add_argument("--reason", default=None)

    sub.add_parser("reclaim", help="return expired leases to pending")

    p_prune = sub.add_parser("prune", help="trim terminal archives")
    p_prune.add_argument("--keep", type=int, default=500)
    p_prune.add_argument("--max-age", type=float, default=None, help="seconds")

    args = parser.parse_args(argv)
    try:
        if args.command == "stats":
            output: Any = stats(root=args.root)
        elif args.command == "list":
            output = {"items": list_items(state=args.state, kind=args.kind, limit=args.limit, root=args.root)}
        elif args.command == "enqueue":
            try:
                payload = json.loads(args.payload)
            except json.JSONDecodeError as exc:
                print(json.dumps({"error": f"payload is not valid JSON: {exc}"}))
                return 2
            output = {
                "item": enqueue(
                    args.kind,
                    payload,
                    priority=args.priority,
                    delay_seconds=args.delay,
                    max_attempts=args.max_attempts,
                    idempotency_key=args.key,
                    root=args.root,
                )
            }
        elif args.command == "cancel":
            item = cancel(args.id, reason=args.reason, root=args.root)
            output = {"cancelled": item is not None, "item": item}
        elif args.command == "reclaim":
            output = {"reclaimed": reclaim_expired(root=args.root)}
        else:  # prune
            output = prune(keep=args.keep, max_age_seconds=args.max_age, root=args.root)
    except QueueError as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
