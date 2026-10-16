#!/usr/bin/env python3
"""memory.py — the cumulative feedback & learning memory engine.

Every rejection, audit note and corrective instruction, wherever it entered
the system (human cockpit, automated QA, gate runner, CLI), is permanently
appended to one central ledger::

    data/memory/feedback_history.jsonl

One JSON object per line, hash-chained (each entry carries ``prev``/``hash``),
fsync'd, and appended under an exclusive ``flock`` — so concurrent workers on
different capsules never interleave a line, and tampering is detectable.

The ledger is a *file*, not a database: greppable, diffable, inspectable with
``tail -f``. The SQLite read cache indexes it for fast radar queries, but the
file is always the truth.

Two consumption paths:

1. :func:`negative_constraints` / :func:`constraint_block` — the engine
   queries past rejection reasons matching a domain/tag inside the configured
   rolling window and injects them into new prompt payloads as explicit
   negative constraints ("do not repeat these mistakes"). The window and
   budget come from ``config/engine.json`` (``memory.window_days``), never
   from a hardcoded constant.
2. :func:`stats` / :func:`read_entries` — the executive radar and the read
   cache.

Failure policy: appending is best-effort *toward the workflow* — a ledger
write failure is logged and reported (``None``), never allowed to abort the
business action it was meant to remember. Reading is fail-closed toward the
data: malformed lines are counted and skipped, never guessed at, and
:func:`verify_chain` names the first broken link.

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # POSIX advisory locking (the ledger is append-only across processes)
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback: O_APPEND alone
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("capsule_engine.memory")

__all__ = [
    "LEDGER_NAME",
    "DECISIONS",
    "DECISION_REJECTED",
    "DECISION_REVISION_REQUESTED",
    "DECISION_NOTE",
    "MAX_REASON_CHARS",
    "MAX_LINE_BYTES",
    "MAX_CONSTRAINT_CHARS",
    "FeedbackEntry",
    "FeedbackWindow",
    "ChainCheck",
    "ledger_path",
    "append_feedback",
    "read_entries",
    "latest_for_capsule",
    "negative_constraints",
    "constraint_block",
    "record_rejection",
    "derive_constraints",
    "stats",
    "verify_chain",
]

LEDGER_NAME = "feedback_history.jsonl"

#: The closed vocabulary of ledger decisions.
DECISION_REJECTED = "rejected"
DECISION_REVISION_REQUESTED = "revision_requested"
DECISION_NOTE = "note"
DECISIONS: frozenset[str] = frozenset(
    {DECISION_REJECTED, DECISION_REVISION_REQUESTED, DECISION_NOTE}
)
#: Decisions that teach the engine what *not* to do again.
REJECTION_DECISIONS: frozenset[str] = frozenset(
    {DECISION_REJECTED, DECISION_REVISION_REQUESTED}
)

MAX_REASON_CHARS = 4000
MAX_LINE_BYTES = 64 * 1024
MAX_CONSTRAINT_CHARS = 240
MAX_TAGS = 16
MAX_TARGETS = 64


# --------------------------------------------------------------------------- #
# time & canonicalisation helpers
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    """Aware UTC timestamp in strict ISO-8601 (seconds resolution)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical(payload: Mapping[str, Any]) -> bytes:
    """Deterministic bytes for hashing (sorted keys, tight separators)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _chain_hash(prev: str, payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(prev.encode("utf-8"))
    digest.update(b"\n")
    digest.update(_canonical(payload))
    return digest.hexdigest()


def _bounded_text(value: object, *, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _clean_sequence(value: object, *, limit: int) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    cleaned: List[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            continue
        token = item.strip()[:120]
        if token in seen:
            continue
        seen.add(token)
        cleaned.append(token)
        if len(cleaned) >= limit:
            break
    return tuple(cleaned)


# --------------------------------------------------------------------------- #
# entry / window / chain dataclasses
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FeedbackEntry:
    """One immutable line of the cumulative feedback ledger."""

    entry_id: str
    at: str
    capsule_id: str
    domain: Optional[str]
    gate_id: Optional[str]
    gate_type: Optional[str]
    decision: str
    reviewer: str
    reason: str
    tags: Tuple[str, ...] = ()
    targets: Tuple[str, ...] = ()
    constraints: Tuple[str, ...] = ()
    prev: str = ""
    hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """The exact payload persisted (and hashed)."""
        payload: Dict[str, Any] = {
            "entry_id": self.entry_id,
            "at": self.at,
            "capsule_id": self.capsule_id,
            "domain": self.domain,
            "gate_id": self.gate_id,
            "gate_type": self.gate_type,
            "decision": self.decision,
            "reviewer": self.reviewer,
            "reason": self.reason,
            "tags": list(self.tags),
            "targets": list(self.targets),
            "constraints": list(self.constraints),
        }
        if self.prev:
            payload["prev"] = self.prev
        if self.hash:
            payload["hash"] = self.hash
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FeedbackEntry":
        """Parse one ledger line; raises ``ValueError`` on any violation."""
        decision = raw.get("decision")
        if not isinstance(decision, str) or decision not in DECISIONS:
            raise ValueError(f"unknown decision {decision!r}")
        entry_id = raw.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("entry_id must be a non-empty string")
        at = raw.get("at")
        if _parse_ts(at) is None:
            raise ValueError(f"invalid timestamp {at!r}")
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")

        def _opt(key: str) -> Optional[str]:
            value = raw.get(key)
            return value if isinstance(value, str) and value.strip() else None

        return cls(
            entry_id=entry_id.strip(),
            at=str(at),
            capsule_id=_bounded_text(raw.get("capsule_id"), limit=120) or "unknown",
            domain=_opt("domain"),
            gate_id=_opt("gate_id"),
            gate_type=_opt("gate_type"),
            decision=decision,
            reviewer=_bounded_text(raw.get("reviewer"), limit=120) or "unknown",
            reason=reason.strip()[:MAX_REASON_CHARS],
            tags=_clean_sequence(raw.get("tags"), limit=MAX_TAGS),
            targets=_clean_sequence(raw.get("targets"), limit=MAX_TARGETS),
            constraints=_clean_sequence(raw.get("constraints"), limit=32),
            prev=str(raw.get("prev") or ""),
            hash=str(raw.get("hash") or ""),
        )


@dataclass(frozen=True)
class FeedbackWindow:
    """A filtered read of the ledger (newest first)."""

    entries: Tuple[FeedbackEntry, ...]
    total_lines: int
    malformed: int
    path: str = ""
    window_days: Optional[float] = None

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class ChainCheck:
    """Result of verifying the ledger's hash chain."""

    ok: bool
    entries: int
    first_bad_line: Optional[int] = None
    detail: str = ""


# --------------------------------------------------------------------------- #
# path resolution
# --------------------------------------------------------------------------- #

def ledger_path(
    explicit: Optional[str | os.PathLike[str]] = None,
    *,
    policy: Optional[Any] = None,
) -> Path:
    """Resolve the ledger: explicit → policy → env → shared_kernel → default."""
    if explicit is not None:
        return Path(explicit)
    try:
        from capsule_engine.policy import load_policy

        active = policy if policy is not None else load_policy()
        if active.memory.path:
            return Path(active.memory.path)
    except Exception:  # policy layer unavailable: continue down the chain
        pass
    env = os.environ.get("AGENCY_MEMORY_FILE", "").strip()
    if env:
        return Path(env)
    try:
        from shared_kernel.paths import FEEDBACK_LEDGER_PATH

        return Path(FEEDBACK_LEDGER_PATH)
    except Exception:
        return Path(__file__).resolve().parents[2] / "data" / "memory" / LEDGER_NAME


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

def _read_lines(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> Tuple[List[str], bool]:
    """Read complete JSONL lines; a torn trailing fragment is never parsed.

    Returns ``(lines, truncated)``. Oversized ledgers are read from the tail
    (the newest entries matter most for constraints) and flagged.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], False
    truncated = size > max_bytes
    try:
        with path.open("rb") as handle:
            if truncated:
                handle.seek(size - max_bytes)
                handle.readline()  # drop the partial first line
            data = handle.read()
    except OSError:
        return [], truncated
    text = data.decode("utf-8", errors="replace")
    if not text:
        return [], truncated
    if not text.endswith("\n"):
        # A writer mid-append: drop the unfinished fragment (never guessed).
        text = text.rsplit("\n", 1)[0] if "\n" in text else ""
    return [line for line in text.split("\n") if line.strip()], truncated


def _window_since(window_days: Optional[float]) -> Optional[datetime]:
    if window_days is None or window_days <= 0:
        return None
    return datetime.fromtimestamp(
        time.time() - float(window_days) * 86400.0, tz=timezone.utc
    )


def read_entries(
    *,
    path: Optional[str | os.PathLike[str]] = None,
    window_days: Optional[float] = None,
    domain: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
    decisions: Optional[Iterable[str]] = None,
    capsule_id: Optional[str] = None,
    limit: Optional[int] = None,
    newest_first: bool = True,
) -> FeedbackWindow:
    """Read and filter the ledger. Malformed lines are counted, never guessed."""
    ledger = ledger_path(path)
    lines, _truncated = _read_lines(ledger)
    since = _window_since(window_days)
    wanted_tags = {t.strip().lower() for t in (tags or ()) if str(t).strip()}
    wanted_decisions = {
        d.strip().lower() for d in (decisions or ()) if str(d).strip()
    }

    entries: List[FeedbackEntry] = []
    malformed = 0
    for line in lines:
        try:
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                raise ValueError("ledger line is not a JSON object")
            entry = FeedbackEntry.from_dict(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            malformed += 1
            continue
        if since is not None:
            at = _parse_ts(entry.at)
            if at is None or at < since:
                continue
        if domain and (entry.domain or "") != domain:
            continue
        if capsule_id and entry.capsule_id != capsule_id:
            continue
        if wanted_decisions and entry.decision not in wanted_decisions:
            continue
        if wanted_tags and not wanted_tags.intersection(
            {t.lower() for t in entry.tags}
        ):
            continue
        entries.append(entry)

    if newest_first:
        entries.reverse()  # the ledger is append-ordered (oldest first)
    if limit is not None and limit >= 0:
        entries = entries[:limit]
    return FeedbackWindow(
        entries=tuple(entries),
        total_lines=len(lines),
        malformed=malformed,
        path=str(ledger),
        window_days=window_days,
    )


def latest_for_capsule(
    capsule_id: str, *, path: Optional[str | os.PathLike[str]] = None
) -> Optional[FeedbackEntry]:
    """The newest ledger entry for a capsule (idempotency guard for retries)."""
    window = read_entries(
        path=path, capsule_id=capsule_id, limit=1, newest_first=True
    )
    return window.entries[0] if window.entries else None


# --------------------------------------------------------------------------- #
# appending
# --------------------------------------------------------------------------- #

def _tail_state(fd: int) -> Tuple[str, int, set[str]]:
    """Read ``(prev_hash, line_count, recent_entry_ids)`` under the lock.

    Scans at most the last 256 KiB — enough for retry idempotency without
    turning every append into a full-ledger read.
    """
    try:
        fd_size = os.lseek(fd, 0, os.SEEK_END)
    except OSError:
        return "", 0, set()
    start = max(0, fd_size - 262144)
    try:
        os.lseek(fd, start, os.SEEK_SET)
        data = os.read(fd, fd_size - start)
    except OSError:
        return "", 0, set()
    text = data.decode("utf-8", errors="replace")
    if start > 0 and "\n" in text:
        text = text.split("\n", 1)[1]
    lines = [line for line in text.split("\n") if line.strip()]
    prev = ""
    recent: set[str] = set()
    count = 0
    for line in lines:
        count += 1
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, Mapping):
            if isinstance(raw.get("hash"), str):
                prev = str(raw["hash"])
            if isinstance(raw.get("entry_id"), str):
                recent.add(str(raw["entry_id"]))
    return prev, count, recent


def append_feedback(
    *,
    capsule_id: str,
    reason: str,
    decision: str = DECISION_NOTE,
    domain: Optional[str] = None,
    gate_id: Optional[str] = None,
    gate_type: Optional[str] = None,
    reviewer: str = "system",
    tags: Sequence[str] = (),
    targets: Sequence[str] = (),
    constraints: Optional[Sequence[str]] = None,
    at: Optional[str] = None,
    path: Optional[str | os.PathLike[str]] = None,
    enabled: bool = True,
) -> Optional[FeedbackEntry]:
    """Append one feedback entry to the ledger (durable, idempotent, chained).

    Contract:

    - refuses empty reasons and unknown decisions (returns ``None``, logged);
    - computes ``constraints`` from the reason when not supplied, so the
      negative-constraint injection has something to inject;
    - derives a deterministic ``entry_id``; an identical payload appended
      within the same second is **skipped** (retry-safe), never duplicated;
    - appends with ``O_APPEND`` + exclusive ``flock`` + ``fsync`` in a single
      write call — readers never see an interleaved or torn line;
    - a disk failure is logged and returns ``None``: the workflow action it
      records must not be aborted by an unwritable ledger.

    Returns the appended entry, or ``None`` when nothing was written.
    """
    if not enabled:
        return None
    clean_reason = _bounded_text(reason, limit=MAX_REASON_CHARS)
    if not clean_reason:
        logger.warning("memory: refusing an empty feedback reason (capsule=%s)", capsule_id)
        return None
    decision_key = str(decision).strip().lower()
    if decision_key not in DECISIONS:
        logger.warning("memory: unknown decision %r — refused", decision)
        return None

    timestamp = at or now_iso()
    derived = (
        tuple(constraints)
        if constraints is not None
        else derive_constraints(clean_reason)
    )
    clean_targets = _clean_sequence(list(targets), limit=MAX_TARGETS)
    stable = "|".join(
        [
            str(capsule_id)[:120],
            str(gate_id or ""),
            decision_key,
            hashlib.sha256(clean_reason.encode("utf-8")).hexdigest()[:16],
            timestamp[:19],
        ]
    )
    entry_id = "fb-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]

    ledger = ledger_path(path)
    payload_core: Dict[str, Any] = {
        "entry_id": entry_id,
        "at": timestamp,
        "capsule_id": _bounded_text(capsule_id, limit=120) or "unknown",
        "domain": domain,
        "gate_id": gate_id,
        "gate_type": gate_type,
        "decision": decision_key,
        "reviewer": _bounded_text(reviewer, limit=120) or "unknown",
        "reason": clean_reason,
        "tags": list(_clean_sequence(list(tags), limit=MAX_TAGS)),
        "targets": list(clean_targets),
        "constraints": list(derived),
    }

    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(ledger, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o644)
    except OSError as exc:
        logger.warning("memory: cannot open ledger %s: %s", ledger, exc)
        return None

    payload: Dict[str, Any] = {}
    prev = ""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        prev, _count, recent_ids = _tail_state(fd)
        if entry_id in recent_ids:
            return None  # idempotent: the exact same note was already recorded

        payload = dict(payload_core)
        payload["prev"] = prev
        payload["hash"] = _chain_hash(prev, payload)
        line = (
            json.dumps(payload, ensure_ascii=False, sort_keys=False) + "\n"
        ).encode("utf-8")
        if len(line) > MAX_LINE_BYTES:
            logger.warning(
                "memory: feedback entry exceeds %d bytes — refused", MAX_LINE_BYTES
            )
            return None
        os.write(fd, line)
        os.fsync(fd)
    except OSError as exc:
        logger.warning("memory: append to %s failed: %s", ledger, exc)
        return None
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    return FeedbackEntry.from_dict(payload_core | {"prev": prev, "hash": payload["hash"]})


# --------------------------------------------------------------------------- #
# negative constraints (the learning loop)
# --------------------------------------------------------------------------- #

_SENTENCE_SPLIT = re.compile(r"[.!?;\n]+")
_ADVISORY_PREFIXES = ("avoid", "do not", "don't", "never", "must not", "prefer")


def derive_constraints(reason: str, *, limit: int = 5) -> Tuple[str, ...]:
    """Turn a rejection reason into bounded negative-constraint lines.

    Deterministic and domain-agnostic: split into sentences, keep the ones
    with substance, phrase them as ``Avoid repeating: ...``. Reviewers who
    already wrote an instruction (``Do not use ...``) keep their own wording.
    """
    constraints: List[str] = []
    seen: set[str] = set()
    for raw in _SENTENCE_SPLIT.split(reason):
        clause = " ".join(raw.split()).strip(" -:•*")
        if len(clause) < 12:
            continue
        lowered = clause.lower()
        if lowered.startswith(_ADVISORY_PREFIXES):
            line = clause[:MAX_CONSTRAINT_CHARS]
        else:
            line = f"Avoid repeating: {clause}"[:MAX_CONSTRAINT_CHARS]
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        constraints.append(line)
        if len(constraints) >= limit:
            break
    return tuple(constraints)


def negative_constraints(
    *,
    domain: Optional[str] = None,
    tags: Sequence[str] = (),
    limit: Optional[int] = None,
    window_days: Optional[float] = None,
    path: Optional[str | os.PathLike[str]] = None,
    policy: Optional[Any] = None,
) -> Tuple[str, ...]:
    """Past rejection lessons matching ``domain``/``tags``, newest first.

    Window and budget default to ``config/engine.json`` (``memory.window_days``,
    ``memory.max_constraints``) — never hardcoded. Returns unique constraint
    lines, most recent first.
    """
    if policy is None:
        try:
            from capsule_engine.policy import load_policy

            policy = load_policy()
        except Exception:
            policy = None
    if policy is not None and not policy.memory.enabled:
        return ()

    effective_window = (
        window_days
        if window_days is not None
        else (policy.memory.window_days if policy is not None else 180.0)
    )
    effective_limit = (
        limit
        if limit is not None
        else (policy.memory.max_constraints if policy is not None else 12)
    )

    window = read_entries(
        path=path,
        window_days=effective_window,
        domain=domain,
        tags=tags,
        decisions=REJECTION_DECISIONS,
        limit=None,
    )
    out: List[str] = []
    seen: set[str] = set()
    for entry in window.entries:  # newest first
        for constraint in entry.constraints or derive_constraints(entry.reason):
            key = constraint.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(constraint)
            if len(out) >= effective_limit:
                return tuple(out)
    return tuple(out)


def constraint_block(
    *,
    domain: Optional[str] = None,
    tags: Sequence[str] = (),
    limit: Optional[int] = None,
    window_days: Optional[float] = None,
    path: Optional[str | os.PathLike[str]] = None,
    policy: Optional[Any] = None,
    max_chars: int = 4000,
) -> str:
    """A prompt-ready block of negative constraints (``""`` when none).

    This is what workers prepend/append to generation prompts: explicit,
    auditable, and cheap to build.
    """
    constraints = negative_constraints(
        domain=domain,
        tags=tags,
        limit=limit,
        window_days=window_days,
        path=path,
        policy=policy,
    )
    if not constraints:
        return ""
    header = (
        "NEGATIVE CONSTRAINTS (from past human/QA feedback on this domain — "
        "these mistakes were already rejected; do not repeat them):"
    )
    lines = [header]
    length = len(header)
    for constraint in constraints:
        bullet = f"- {constraint}"
        if length + len(bullet) + 1 > max_chars:
            break
        lines.append(bullet)
        length += len(bullet) + 1
    return "\n".join(lines)


def record_rejection(
    manifest: Mapping[str, Any],
    *,
    reason: str,
    decision: str = DECISION_REJECTED,
    reviewer: str = "system",
    gate_id: Optional[str] = None,
    gate_type: Optional[str] = None,
    targets: Sequence[str] = (),
    path: Optional[str | os.PathLike[str]] = None,
    policy: Optional[Any] = None,
    disabled: bool = False,
) -> Optional[FeedbackEntry]:
    """High-level helper: remember a rejection against a capsule's context.

    Derives domain/tags from the manifest (``domain``, ``tags``,
    ``routing_context``), so every caller — worker hook, gate runner, API —
    records the same shape without duplicating manifest archaeology.
    """
    if policy is None:
        try:
            from capsule_engine.policy import load_policy

            policy = load_policy()
        except Exception:
            policy = None
    if disabled or (policy is not None and not policy.memory.enabled):
        return None

    domain = manifest.get("domain")
    tags: List[str] = []
    raw_tags = manifest.get("tags")
    if isinstance(raw_tags, Sequence) and not isinstance(raw_tags, (str, bytes)):
        tags.extend(str(t) for t in raw_tags if isinstance(t, str))
    routing = manifest.get("routing_context")
    if isinstance(routing, Mapping):
        for key in ("topic", "occasion", "channel", "kind"):
            value = routing.get(key)
            if isinstance(value, str) and value.strip():
                tags.append(value.strip())
    channels = manifest.get("channels")
    if isinstance(channels, Sequence) and not isinstance(channels, (str, bytes)):
        tags.extend(str(c) for c in channels if isinstance(c, str))

    return append_feedback(
        capsule_id=str(manifest.get("capsule_id") or "unknown"),
        reason=reason,
        decision=decision,
        domain=str(domain) if isinstance(domain, str) and domain.strip() else None,
        gate_id=gate_id,
        gate_type=gate_type,
        reviewer=reviewer,
        tags=tags,
        targets=targets,
        path=path,
        enabled=True,
    )


# --------------------------------------------------------------------------- #
# verification & stats
# --------------------------------------------------------------------------- #

def verify_chain(
    path: Optional[str | os.PathLike[str]] = None,
) -> ChainCheck:
    """Verify the ledger's hash chain end to end.

    Returns the first broken line (1-based) and a detail string — never
    repairs, never guesses.
    """
    ledger = ledger_path(path)
    lines, _truncated = _read_lines(ledger)
    prev = ""
    for index, line in enumerate(lines, start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            return ChainCheck(
                ok=False,
                entries=index - 1,
                first_bad_line=index,
                detail=f"line {index}: invalid JSON ({exc.msg})",
            )
        if not isinstance(raw, Mapping):
            return ChainCheck(
                ok=False, entries=index - 1, first_bad_line=index,
                detail=f"line {index}: not a JSON object",
            )
        recorded = raw.get("hash")
        claimed_prev = raw.get("prev")
        if not isinstance(recorded, str) or not recorded:
            return ChainCheck(
                ok=False, entries=index - 1, first_bad_line=index,
                detail=f"line {index}: missing 'hash' (unchained entry)",
            )
        if claimed_prev != prev:
            return ChainCheck(
                ok=False, entries=index - 1, first_bad_line=index,
                detail=(
                    f"line {index}: prev={str(claimed_prev)[:12]}... does not "
                    f"follow the previous hash {prev[:12]}..."
                ),
            )
        payload = {k: v for k, v in raw.items() if k != "hash"}
        expected = _chain_hash(prev, payload)
        if expected != recorded:
            return ChainCheck(
                ok=False, entries=index - 1, first_bad_line=index,
                detail=f"line {index}: hash mismatch (tampered or corrupted)",
            )
        prev = recorded
    return ChainCheck(ok=True, entries=len(lines))


def stats(
    *,
    path: Optional[str | os.PathLike[str]] = None,
    window_days: Optional[float] = None,
) -> Dict[str, Any]:
    """Aggregate views for the radar: counts by domain, decision, reviewer."""
    window = read_entries(path=path, window_days=window_days, limit=None)
    by_domain: Dict[str, int] = {}
    by_decision: Dict[str, int] = {}
    by_reviewer: Dict[str, int] = {}
    for entry in window.entries:
        key = entry.domain or "(none)"
        by_domain[key] = by_domain.get(key, 0) + 1
        by_decision[entry.decision] = by_decision.get(entry.decision, 0) + 1
        by_reviewer[entry.reviewer] = by_reviewer.get(entry.reviewer, 0) + 1
    return {
        "path": window.path,
        "window_days": window.window_days,
        "total": len(window.entries),
        "malformed": window.malformed,
        "by_domain": by_domain,
        "by_decision": by_decision,
        "by_reviewer": by_reviewer,
    }
